"""Decode ArcGIS coded-value domains and subtypes into readable attributes.

An ArcGIS layer stores codes, not words. `material` comes back as `ABS`,
`lifecyclestatus` as `4`, `assettype` as `41`. The words live in the layer's
metadata, in two places:

* a **domain** on a field - one code list for the whole layer;
* a **subtype** - the layer is split by a subtype field, and each subtype can
  override the domain on any field.

The second one is why this cannot be a single lookup table. In the Esri utility
model `assetgroup` is the subtype and `assettype` is domained per subtype, so
code 41 means one thing on a distribution main and something else entirely on a
service. Decoding `assettype` without looking at that row's `assetgroup` produces
labels that are confidently wrong, which is worse than leaving the codes alone.

Decoded values replace the codes in place, because that is what anyone reading
the output wants. The raw code is kept alongside as `<field>_code`, since it is
what other systems key on and what a WHERE clause has to be written against.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

CODE_SUFFIX = "_code"


def coded_values(domain: dict[str, Any] | None) -> dict[Any, str]:
    """{code: name} for a coded-value domain, empty for range or no domain."""
    if not domain or domain.get("type") != "codedValue":
        return {}
    mapping: dict[Any, str] = {}
    for entry in domain.get("codedValues") or []:
        if "code" in entry:
            mapping[entry["code"]] = str(entry.get("name", entry["code"]))
    return mapping


def layer_domains(meta_fields: list[dict[str, Any]]) -> dict[str, dict[Any, str]]:
    """Layer-wide domains, keyed by field name."""
    domains: dict[str, dict[Any, str]] = {}
    for field in meta_fields or []:
        name = field.get("name")
        values = coded_values(field.get("domain"))
        if name and values:
            domains[name] = values
    return domains


def subtype_domains(
    subtypes: list[dict[str, Any]],
) -> tuple[dict[Any, str], dict[Any, dict[str, dict[Any, str]]]]:
    """Return ({subtype code: name}, {subtype code: {field: {code: name}}})."""
    names: dict[Any, str] = {}
    per_subtype: dict[Any, dict[str, dict[Any, str]]] = {}
    for subtype in subtypes or []:
        code = subtype.get("code")
        if code is None:
            continue
        names[code] = str(subtype.get("subtypeName") or subtype.get("name") or code)
        overrides: dict[str, dict[Any, str]] = {}
        for field_name, domain in (subtype.get("domains") or {}).items():
            values = coded_values(domain)
            if values:
                overrides[field_name] = values
        per_subtype[code] = overrides
    return names, per_subtype


def describe(meta: dict[str, Any]) -> dict[str, Any]:
    """Everything needed to decode this layer, read once from its metadata."""
    fields = meta.get("fields") or []
    subtype_field = (meta.get("subtypeField") or "").strip() or None
    names, per_subtype = subtype_domains(meta.get("subtypes") or [])
    return {
        "layer_domains": layer_domains(fields),
        "subtype_field": subtype_field,
        "subtype_names": names,
        "subtype_domains": per_subtype,
    }


def _decode_column(values: pd.Series, mapping: dict[Any, str]) -> pd.Series:
    """Map codes to names, leaving anything unmapped exactly as it was.

    An unrecognised code is left alone rather than blanked. A code the metadata
    does not cover is a real thing in the data, and turning it into null loses
    it; leaving it visible lets someone notice the gap.
    """
    if not mapping:
        return values
    # Codes arrive as int, float or str depending on the field type and the
    # transport, so match on the string form as well as the declared type.
    text_mapping = {str(code): name for code, name in mapping.items()}
    decoded = values.map(mapping)
    fallback = values.astype("object").map(lambda v: text_mapping.get(str(v)))
    return decoded.combine_first(fallback).combine_first(values.astype("object"))


def decode(
    frame: pd.DataFrame,
    meta: dict[str, Any],
    *,
    keep_codes: bool = True,
    progress: Any = None,
) -> pd.DataFrame:
    """Replace coded values with their descriptions, subtype by subtype."""
    plan = describe(meta)
    layer_map = plan["layer_domains"]
    subtype_field = plan["subtype_field"]
    subtype_names = plan["subtype_names"]
    per_subtype = plan["subtype_domains"]

    fields_to_decode = set(layer_map)
    for overrides in per_subtype.values():
        fields_to_decode.update(overrides)
    fields_to_decode = {name for name in fields_to_decode if name in frame.columns}
    if subtype_field and subtype_field in frame.columns and subtype_names:
        fields_to_decode.add(subtype_field)
    if not fields_to_decode:
        return frame

    result = frame.copy()
    if keep_codes:
        for name in sorted(fields_to_decode):
            result[f"{name}{CODE_SUFFIX}"] = result[name]

    decoded_report: list[str] = []

    # The subtype field itself has one meaning for the whole layer.
    if subtype_field and subtype_field in result.columns and subtype_names:
        result[subtype_field] = _decode_column(result[subtype_field], subtype_names)
        decoded_report.append(f"{subtype_field} ({len(subtype_names)} subtypes)")

    use_subtypes = bool(
        subtype_field and subtype_field in frame.columns and any(per_subtype.values())
    )
    for name in sorted(fields_to_decode - {subtype_field}):
        if use_subtypes:
            # Decode per subtype, since the same code can mean different things.
            column = result[name].astype("object").copy()
            codes = frame[subtype_field]
            touched = 0
            for subtype_code, overrides in per_subtype.items():
                mapping = overrides.get(name) or layer_map.get(name) or {}
                if not mapping:
                    continue
                rows = codes == subtype_code
                if not bool(rows.any()):
                    continue
                column.loc[rows] = _decode_column(frame.loc[rows, name], mapping)
                touched += int(rows.sum())
            # Rows whose subtype the metadata never mentions still get the
            # layer-wide domain, which is better than leaving them coded.
            leftover = ~codes.isin(list(per_subtype))
            if bool(leftover.any()) and layer_map.get(name):
                column.loc[leftover] = _decode_column(
                    frame.loc[leftover, name], layer_map[name]
                )
            result[name] = column
            if touched:
                decoded_report.append(f"{name} (per subtype)")
        elif layer_map.get(name):
            result[name] = _decode_column(result[name], layer_map[name])
            decoded_report.append(f"{name} ({len(layer_map[name])} values)")

    if progress and decoded_report:
        progress(f"Decoded {len(decoded_report)} coded fields: {', '.join(decoded_report)}")
    return result


def codebook(meta: dict[str, Any]) -> pd.DataFrame:
    """Every code and its meaning, as a table worth keeping next to the export."""
    plan = describe(meta)
    rows: list[dict[str, Any]] = []

    for code, name in plan["subtype_names"].items():
        rows.append(
            {
                "field": plan["subtype_field"] or "",
                "subtype": "",
                "code": code,
                "description": name,
                "source": "subtype",
            }
        )
    for field_name, mapping in plan["layer_domains"].items():
        for code, name in mapping.items():
            rows.append(
                {
                    "field": field_name,
                    "subtype": "",
                    "code": code,
                    "description": name,
                    "source": "layer domain",
                }
            )
    for subtype_code, overrides in plan["subtype_domains"].items():
        subtype_name = plan["subtype_names"].get(subtype_code, subtype_code)
        for field_name, mapping in overrides.items():
            for code, name in mapping.items():
                rows.append(
                    {
                        "field": field_name,
                        "subtype": subtype_name,
                        "code": code,
                        "description": name,
                        "source": "subtype domain",
                    }
                )
    columns = ["field", "subtype", "code", "description", "source"]
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns).sort_values(
        ["field", "subtype", "code"], ignore_index=True, key=lambda s: s.astype(str)
    )
