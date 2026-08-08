"""Print the codebook for an ArcGIS layer: every coded value and what it means.

The words behind the codes only exist in the layer's metadata. This prints them,
so a coded export can be read without guessing and the mapping can be handed to
someone who is not running Python.

    # What do the codes in the MA main line layer mean?
    python scripts/describe_layer_domains.py

    # Any other layer, and save it next to the data
    python scripts/describe_layer_domains.py --layer-url https://host/.../MapServer/3 \
        --out outputs/codebook.csv

    # Just one field
    python scripts/describe_layer_domains.py --field assettype

Two kinds of coding are reported, and the difference matters. A **layer domain**
is one code list for the whole layer. A **subtype domain** applies only to rows
of one subtype, so the same code can mean different things in different rows -
in the Esri utility model, assettype 41 is a Main under one assetgroup and a
Service Line under another.
"""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

enterprise_network = importlib.import_module("enterprise_network")
enterprise_network.configure_enterprise_network()

import arcgis_domains
import service_auth
from arcgis_rest_geopandas import layer_metadata, make_session, progress

DEFAULT_LAYER_URL = (
    "https://gis.nationalgrid.com/arcgis/rest/services/MA/Material_View_MA/MapServer/341"
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--layer-url", default=DEFAULT_LAYER_URL, help="ArcGIS layer URL.")
    parser.add_argument("--field", help="Show only this field, case-insensitively.")
    parser.add_argument("--out", help="Also write the codebook to this CSV.")
    parser.add_argument(
        "--max-values",
        type=int,
        default=40,
        help="Values printed per field before truncating. 0 for all. Default: 40.",
    )
    parser.add_argument(
        "--anonymous", action="store_true", help="Do not sign in, for public layers."
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    url = args.layer_url

    token = service_auth.token_for(url, allow_sign_in=not args.anonymous)
    service_auth.report_once(url)
    meta = layer_metadata(make_session(token), url)

    plan = arcgis_domains.describe(meta)
    book = arcgis_domains.codebook(meta)

    progress(f"Layer: {meta.get('name') or url}")
    if plan["subtype_field"]:
        progress(
            f"Subtype field: {plan['subtype_field']} "
            f"({len(plan['subtype_names'])} subtypes)"
        )
    else:
        progress("Subtype field: none")

    if book.empty:
        progress("")
        progress("This layer has no coded-value domains and no subtypes.")
        progress("Its attributes are stored as-is; there is nothing to decode.")
        return 0

    wanted = args.field.lower() if args.field else None
    shown = book if wanted is None else book[book["field"].str.lower() == wanted]
    if shown.empty:
        coded = sorted(book["field"].unique())
        progress(f"No coded field called {args.field!r}. Coded fields: {coded}")
        return 1

    for (field, subtype), group in shown.groupby(["field", "subtype"], sort=True):
        heading = field if not subtype else f"{field}  [subtype: {subtype}]"
        progress("")
        progress(heading)
        progress("-" * len(heading))
        rows = group if not args.max_values else group.head(args.max_values)
        for _, row in rows.iterrows():
            progress(f"  {str(row['code']):<14} {row['description']}")
        hidden = len(group) - len(rows)
        if hidden > 0:
            progress(f"  ... {hidden:,} more (raise --max-values to see them)")

    progress("")
    progress(f"{len(book):,} coded values across {book['field'].nunique():,} fields.")
    if plan["subtype_domains"]:
        progress(
            "Fields listed under a subtype are coded per subtype: the same code "
            "means different things in rows of different subtypes."
        )

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        book.to_csv(out, index=False, encoding="utf-8-sig")
        progress(f"Codebook written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
