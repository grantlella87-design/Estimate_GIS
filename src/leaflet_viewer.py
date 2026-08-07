"""Build a standalone Leaflet viewer with an attribute table from any GeoDataFrame.

Deliberately knows nothing about ledge, mains or dates. It takes layers, an
optional summary dictionary and optional plain tables, and writes one HTML file
that opens by double-clicking - no server, no Python, no ArcGIS licence at the
other end. That is what makes it reusable for the next question someone asks,
which is the point of building it here instead of inside the analysis script.

The attribute table is generic: columns come from whatever properties the
features carry, so a layer this module has never seen still gets a sortable,
searchable, exportable table.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import mapping

from arcgis_rest_geopandas import progress

TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "leaflet_viewer.html"

LEAFLET_VERSION = "1.9.4"
LEAFLET_CSS_URL = f"https://unpkg.com/leaflet@{LEAFLET_VERSION}/dist/leaflet.css"
LEAFLET_JS_URL = f"https://unpkg.com/leaflet@{LEAFLET_VERSION}/dist/leaflet.js"

WGS84 = 4326

DEFAULT_BASEMAPS: list[dict[str, Any]] = [
    {
        "name": "OpenStreetMap",
        "url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": "&copy; OpenStreetMap contributors",
        "max_zoom": 19,
    },
    {
        "name": "Carto Light",
        "url": "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
        "attribution": "&copy; OpenStreetMap contributors, &copy; CARTO",
        "max_zoom": 20,
    },
    {
        "name": "Esri Imagery",
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "attribution": "Esri, Maxar, Earthstar Geographics",
        "max_zoom": 19,
        "subdomains": "",
    },
    {
        "name": "Esri Topographic",
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        "attribution": "Esri, HERE, Garmin, USGS",
        "max_zoom": 19,
        "subdomains": "",
    },
]


# --- Value cleaning ---------------------------------------------------------

def clean_value(value: Any) -> Any:
    """Turn a pandas/numpy/shapely value into something JSON and a table can hold."""
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, (np.floating,)):
        number = float(value)
        return None if math.isnan(number) or math.isinf(number) else round(number, 6)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (pd.Timestamp, datetime)):
        if pd.isna(value):
            return None
        # Midnight almost always means "a date was stored", not "midnight".
        if value.hour == 0 and value.minute == 0 and value.second == 0:
            return value.date().isoformat()
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _round_coords(obj: Any, precision: int) -> Any:
    if isinstance(obj, (list, tuple)):
        if obj and isinstance(obj[0], (int, float)):
            return [round(float(value), precision) for value in obj]
        return [_round_coords(item, precision) for item in obj]
    return obj


# --- Layers -----------------------------------------------------------------

def layer_to_geojson(
    gdf: gpd.GeoDataFrame,
    fields: Sequence[str] | None = None,
    *,
    precision: int = 6,
    simplify_tolerance: float | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Reproject to WGS84 and emit GeoJSON, returning it with its field order.

    Coordinates are rounded because six decimal places is ~0.1 m, far finer than
    the source data, and the extra digits are pure file size in a viewer that is
    often emailed around.
    """
    if gdf.crs is None:
        raise ValueError(
            "Cannot build a viewer from a layer with no CRS: Leaflet needs "
            "WGS84 and there is no way to know what to convert from."
        )
    working = gdf.to_crs(WGS84)
    if simplify_tolerance:
        working = working.copy()
        working["geometry"] = working.geometry.simplify(
            simplify_tolerance, preserve_topology=True
        )

    geometry_name = working.geometry.name
    if fields is None:
        fields = [column for column in working.columns if column != geometry_name]
    fields = [name for name in fields if name in working.columns and name != geometry_name]

    records = working[list(fields)].to_dict("records") if fields else [{}] * len(working)
    features = []
    for record, geometry in zip(records, working.geometry, strict=False):
        if geometry is None or geometry.is_empty:
            continue
        shape = mapping(geometry)
        shape["coordinates"] = _round_coords(shape.get("coordinates"), precision)
        features.append(
            {
                "type": "Feature",
                "geometry": shape,
                "properties": {
                    str(key): clean_value(value) for key, value in record.items()
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}, [str(f) for f in fields]


def make_layer(
    gdf: gpd.GeoDataFrame,
    name: str,
    *,
    layer_id: str | None = None,
    style: dict[str, Any] | None = None,
    style_rules: Sequence[dict[str, Any]] | None = None,
    legend: Sequence[dict[str, str]] | None = None,
    popup_fields: Sequence[str] | None = None,
    fields: Sequence[str] | None = None,
    visible: bool = True,
    precision: int = 6,
    simplify_tolerance: float | None = None,
) -> dict[str, Any]:
    """Package one GeoDataFrame as a viewer layer.

    `style_rules` are checked in order and the first match wins, so a layer can
    be coloured by an attribute without the caller pre-computing a colour column.
    Each rule is {"field": ..., "value": ...} or {"field": ..., "min": ..., "max": ...}
    plus a "style" to merge over the base style.
    """
    geojson, field_order = layer_to_geojson(
        gdf, fields, precision=precision, simplify_tolerance=simplify_tolerance
    )
    return {
        "id": layer_id or name.lower().replace(" ", "_"),
        "name": name,
        "style": style or {"color": "#1d6fd0", "weight": 2, "opacity": 0.9, "fillOpacity": 0.25},
        "style_rules": list(style_rules or []),
        "legend": list(legend or []),
        "popup_fields": list(popup_fields or []),
        "fields": field_order,
        "visible": visible,
        "geojson": geojson,
    }


def make_table(name: str, frame: pd.DataFrame, table_id: str | None = None) -> dict[str, Any]:
    """Package a plain (non-spatial) DataFrame as an extra tab in the table panel."""
    columns = [str(column) for column in frame.columns]
    rows = [
        {str(key): clean_value(value) for key, value in record.items()}
        for record in frame.to_dict("records")
    ]
    return {
        "id": table_id or name.lower().replace(" ", "_"),
        "name": name,
        "columns": columns,
        "rows": rows,
    }


# --- Assembly ---------------------------------------------------------------

def _leaflet_assets(inline: bool) -> tuple[str, str]:
    """Return the <style>/<script> blocks for Leaflet, from CDN or embedded."""
    if not inline:
        return (
            f'<link rel="stylesheet" href="{LEAFLET_CSS_URL}"/>',
            f'<script src="{LEAFLET_JS_URL}"></script>',
        )
    import requests

    progress("Embedding Leaflet into the viewer for offline use.")
    css = requests.get(LEAFLET_CSS_URL, timeout=60)
    css.raise_for_status()
    js = requests.get(LEAFLET_JS_URL, timeout=60)
    js.raise_for_status()
    return (
        "<style>" + css.text + "</style>",
        "<script>" + js.text.replace("</script", "<\\/script") + "</script>",
    )


def _json_for_script(payload: dict[str, Any]) -> str:
    """JSON safe to drop inside a <script> element."""
    return json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("</", "<\\/")


def build_viewer(
    output_path: str | Path,
    layers: Sequence[dict[str, Any]],
    *,
    title: str = "GIS Viewer",
    subtitle: str = "",
    stats: dict[str, Any] | None = None,
    stats_title: str = "Summary",
    tables: Sequence[dict[str, Any]] | None = None,
    basemaps: Sequence[dict[str, Any]] | None = None,
    page_size: int = 200,
    active_tab: str | None = None,
    inline_leaflet: bool = False,
    external_data: bool = False,
) -> Path:
    """Write the viewer and return the HTML path.

    With `external_data` the payload goes to viewer_data.js beside the HTML.
    Browsers block `fetch` on file:// URLs, so it is a plain script include: the
    two files have to travel together, which is why it is not the default.
    """
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "layers": list(layers),
        "tables": list(tables or []),
        "stats": stats,
        "stats_title": stats_title,
        "basemaps": list(basemaps if basemaps is not None else DEFAULT_BASEMAPS),
        "page_size": page_size,
        # Draw order and reading order differ: a polygon backdrop has to be added
        # to the map first to sit underneath, but it is rarely the table anyone
        # wants open.
        "active_tab": active_tab,
    }
    data_json = _json_for_script(payload)

    if external_data:
        data_path = output.parent / f"{output.stem}_data.js"
        data_path.write_text(
            "window.__VIEWER_DATA__ = " + data_json + ";", encoding="utf-8"
        )
        data_block = f'<script src="{data_path.name}"></script>'
    else:
        data_block = "<script>window.__VIEWER_DATA__ = " + data_json + ";</script>"

    css_block, js_block = _leaflet_assets(inline_leaflet)
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    for placeholder, value in (
        ("__TITLE__", title),
        ("__SUBTITLE__", subtitle),
        ("__LEAFLET_CSS__", css_block),
        ("__LEAFLET_JS__", js_block),
        ("__DATA_BLOCK__", data_block),
    ):
        html = html.replace(placeholder, value)
    output.write_text(html, encoding="utf-8")

    feature_count = sum(len(layer["geojson"]["features"]) for layer in layers)
    size_mb = output.stat().st_size / (1024 * 1024)
    progress(f"Viewer written: {output} ({feature_count:,} features, {size_mb:.1f} MB)")
    return output


def cap_features(
    gdf: gpd.GeoDataFrame, limit: int | None, order_by: Iterable[str] | None = None
) -> tuple[gpd.GeoDataFrame, str | None]:
    """Trim a layer to the largest `limit` features, reporting what was dropped.

    A browser will try to draw everything handed to it, so a statewide pull can
    produce a viewer that never finishes loading. Trimming with a stated note
    beats a file that looks broken.
    """
    if not limit or len(gdf) <= limit:
        return gdf, None
    working = gdf
    for column in order_by or ():
        if column in gdf.columns:
            working = gdf.sort_values(column, ascending=False)
            break
    note = (
        f"The map shows the largest {limit:,} of {len(gdf):,} features. "
        "Every summary number is computed from the full set."
    )
    progress(f"Viewer capped to {limit:,} of {len(gdf):,} features.")
    return working.head(limit).copy(), note
