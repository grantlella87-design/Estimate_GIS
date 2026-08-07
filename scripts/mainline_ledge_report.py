"""Intersect main lines with MassGIS ledge and report the post-cutoff share.

    # The whole thing, defaults everywhere: MA main lines, MassGIS ledge, 2022-01-01
    python scripts/mainline_ledge_report.py --where "citycode = 'WORCESTER'"

    # Scope by extent instead of by attribute
    python scripts/mainline_ledge_report.py --extent 200000,890000,215000,905000 --extent-crs 26986

    # No VPN? Run the same analysis against an export, ledge still comes from MassGIS
    python scripts/mainline_ledge_report.py --mainlines outputs/mains.gpkg --mainlines-layer mains

    # Any other line layer, any other polygon layer, any other date
    python scripts/mainline_ledge_report.py \
        --mainlines https://host/arcgis/rest/services/X/MapServer/3 \
        --ledge some_polygons.gpkg --ledge-class-field ROCK_TYPE --since 2020-07-01

Writes a GeoPackage, a summary in JSON and CSV, and a standalone Leaflet viewer
with an attribute table.

Every default is a flag, so nothing about the question being asked today is
baked into the code: the main line layer, the ledge definition, the cutoff date
and the date fields can all be pointed somewhere else.
"""
from __future__ import annotations

import argparse
import html
import importlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import geopandas as gpd
import pandas as pd

enterprise_network = importlib.import_module("enterprise_network")
enterprise_network.configure_enterprise_network()

import leaflet_viewer
import ledge_analysis
import massgis_ledge
import service_auth
import vector_source
from arcgis_rest_geopandas import progress

DEFAULT_MAINLINE_URL = (
    "https://gis.nationalgrid.com/arcgis/rest/services/MA/Material_View_MA/MapServer/341"
)
DEFAULT_CUTOFF = "2022-01-01"

# Viewer styling. Post-cutoff mains in ledge are the answer to the question, so
# they are the only thing drawn in a colour that pulls the eye.
LEDGE_FILL = "#8c6d46"
MAIN_PRE = "#4b5563"
MAIN_POST = "#d64545"
LEDGE_SEGMENT = "#111827"

def _clean_source_value(value: object) -> str:
    """Return a plain URL/path even if a Teams/HTML anchor was pasted into PowerShell."""
    if value is None:
        return ""
    cleaned = str(value).strip()
    if "<a " in cleaned.lower() and "href=" in cleaned.lower():
        match = re.search(r"href=[\"']([^\"']+)", cleaned, flags=re.IGNORECASE)
        if match:
            cleaned = match.group(1)
        else:
            cleaned = re.sub(r"<[^>]+>", "", cleaned)
        cleaned = html.unescape(cleaned)
    cleaned = cleaned.strip().strip('"').strip("'")
    return cleaned

def _source_requires_sign_in(value: object) -> bool:
    """Only National Grid/internal sources should receive the token from auth.py.

    The host list lives in src/service_auth.py, which is also what decides
    whether a token is attached to the request itself. Keeping a second list here
    would mean a new internal host had to be added in two places, and the one
    that was missed would fail quietly.
    """
    return service_auth.requires_token(_clean_source_value(value))

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )

    lines = parser.add_argument_group("main lines")
    lines.add_argument(
        "--mainlines",
        default=DEFAULT_MAINLINE_URL,
        help="ArcGIS layer URL or a local vector file. Default: the MA main line layer.",
    )
    lines.add_argument("--mainlines-layer", help="Layer name, when the source is a GeoPackage.")
    lines.add_argument(
        "--where",
        default="1=1",
        help="SQL WHERE for a service, or a pandas query for a file. Scope the run with this.",
    )
    lines.add_argument("--out-fields", default="*", help="Fields to request. Default: all.")
    lines.add_argument(
        "--extent",
        help="Limit the run to minx,miny,maxx,maxy. Applies to both layers.",
    )
    lines.add_argument(
        "--extent-crs", default="26986", help="CRS of --extent. Default: EPSG:26986."
    )

    ledge = parser.add_argument_group("ledge")
    ledge.add_argument(
        "--ledge",
        default="massgis",
        help="'massgis' for the MassGIS surficial geology ledge polygons, or a URL or file.",
    )
    ledge.add_argument(
        "--ledge-profile",
        default=massgis_ledge.DEFAULT_LEDGE_PROFILE,
        choices=sorted(massgis_ledge.LEDGE_PROFILES),
        help=(
            "Which MassGIS classes count as ledge. "
            "outcrop: bedrock outcrops only. "
            "standard: outcrops plus areas of abundant outcrop or shallow bedrock. "
            "broad: adds thin till and talus."
        ),
    )
    ledge.add_argument("--ledge-layer", help="Layer name, when --ledge is a GeoPackage.")
    ledge.add_argument(
        "--ledge-where",
        default="1=1",
        help="Filter for a custom --ledge source. Ignored for the MassGIS preset.",
    )
    ledge.add_argument(
        "--ledge-cache-dir",
        default="",
        help="Cache MassGIS ledge here and reuse it. Default: <out-dir>/ledge_cache.",
    )
    ledge.add_argument(
        "--no-ledge-cache", action="store_true", help="Always re-download the ledge."
    )
    ledge.add_argument(
        "--refresh-ledge-cache",
        action="store_true",
        help="Re-download the ledge and replace what is cached.",
    )
    ledge.add_argument(
        "--allow-empty-ledge",
        action="store_true",
        help="Carry on when no ledge is found, instead of stopping.",
    )
    ledge.add_argument(
        "--ledge-class-field",
        default="ledge_class",
        help="Field naming the ledge type, used to break the summary down by class.",
    )
    ledge.add_argument(
        "--ledge-buffer-ft",
        type=float,
        default=0.0,
        help="Grow ledge polygons by this many feet before intersecting, for mapping slop.",
    )

    dates = parser.add_argument_group("dates")
    dates.add_argument(
        "--since",
        default=DEFAULT_CUTOFF,
        help=f"Cutoff date, YYYY-MM-DD. On or after it counts. Default: {DEFAULT_CUTOFF}.",
    )
    dates.add_argument(
        "--install-fields",
        default=",".join(ledge_analysis.DEFAULT_INSTALL_FIELDS),
        help="Comma-separated candidate installation date fields, first match wins.",
    )
    dates.add_argument(
        "--creation-fields",
        default=",".join(ledge_analysis.DEFAULT_CREATION_FIELDS),
        help="Comma-separated candidate creation date fields, first match wins.",
    )

    output = parser.add_argument_group("output")
    output.add_argument("--out-dir", default="outputs", help="Output folder. Default: outputs.")
    output.add_argument(
        "--basename", default="mainline_ledge", help="Base name for every output file."
    )
    output.add_argument(
        "--analysis-crs",
        default="auto",
        help="CRS lengths are measured in. Default: auto (the layer's own projected CRS).",
    )
    output.add_argument("--no-viewer", action="store_true", help="Skip the Leaflet viewer.")
    output.add_argument("--no-gpkg", action="store_true", help="Skip the GeoPackage.")
    output.add_argument(
        "--max-viewer-features",
        type=int,
        default=40000,
        help="Cap features drawn in the viewer. 0 for no cap. Default: 40000.",
    )
    output.add_argument(
        "--simplify-ft",
        type=float,
        default=0.0,
        help="Simplify viewer geometry by this tolerance in feet, to shrink the file.",
    )
    output.add_argument(
        "--inline-leaflet",
        action="store_true",
        help="Embed Leaflet in the HTML so the viewer works with no internet.",
    )
    output.add_argument(
        "--external-data",
        action="store_true",
        help="Write viewer data to a sidecar .js instead of inlining it.",
    )

    runtime = parser.add_argument_group("runtime")
    runtime.add_argument("--workers", type=int, default=8, help="Parallel download workers.")
    runtime.add_argument("--batch-size", type=int, default=500, help="OBJECTID batch size.")
    runtime.add_argument(
        "--anonymous",
        action="store_true",
        help=(
            "Never sign in, even for one of our own services. Sign-in is already "
            "skipped for public hosts, so this is only needed for an unattended run."
        ),
    )
    runtime.add_argument(
        "--count-only",
        action="store_true",
        help="Report how many main lines match and stop, downloading nothing.",
    )
    return parser.parse_args(argv)

def parse_extent(args) -> tuple[tuple[float, float, float, float] | None, str | None]:
    if not args.extent:
        return None, None
    parts = [part.strip() for part in str(args.extent).split(",")]
    if len(parts) != 4:
        raise SystemExit("--extent must be minx,miny,maxx,maxy")
    return tuple(float(part) for part in parts), args.extent_crs

def load_ledge(args, bounds, bounds_crs, sign_in: bool = True) -> tuple[gpd.GeoDataFrame, str, str]:
    """Load ledge polygons and describe where the definition came from."""
    ledge_source = _clean_source_value(args.ledge)
    if ledge_source.lower() in ("massgis", "massgis:surfgeo", "default"):
        cache_dir = None
        if not args.no_ledge_cache:
            cache_dir = Path(args.ledge_cache_dir or (Path(args.out_dir) / "ledge_cache"))
        gdf = massgis_ledge.fetch_ledge_polygons(
            profile=args.ledge_profile,
            bounds=bounds,
            bounds_crs=bounds_crs,
            workers=args.workers,
            batch_size=args.batch_size,
            cache_dir=cache_dir,
            refresh_cache=args.refresh_ledge_cache,
        )
        return (
            gdf,
            args.ledge_profile,
            "MassGIS Surficial Geology (1:24,000): "
            + massgis_ledge.describe_profile(args.ledge_profile),
        )
    gdf = vector_source.read_source(
        ledge_source,
        layer=args.ledge_layer,
        where=args.ledge_where,
        bounds=bounds,
        bounds_crs=bounds_crs,
        workers=args.workers,
        batch_size=args.batch_size,
        sign_in=_source_requires_sign_in(ledge_source) if sign_in else False,
    )
    return gdf, "custom", f"Ledge polygons from {ledge_source}"

def buffer_ledge(ledge: gpd.GeoDataFrame, buffer_ft: float) -> gpd.GeoDataFrame:
    if not buffer_ft or ledge.empty or ledge.crs is None:
        return ledge
    from pyproj import CRS

    crs = CRS.from_user_input(ledge.crs)
    if not crs.is_projected:
        progress("Ledge buffer skipped: the ledge layer is not in a projected CRS.")
        return ledge
    units_per_foot = 1.0 / ledge_analysis.unit_to_feet(crs)
    progress(f"Buffering ledge polygons by {buffer_ft:g} ft.")
    buffered = ledge.copy()
    buffered["geometry"] = buffered.geometry.buffer(buffer_ft * units_per_foot)
    return buffered

def flat_stats_rows(stats: dict, prefix: str = "") -> list[dict]:
    """Flatten the nested summary into metric/value rows for a CSV."""
    rows = []
    for key, value in stats.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            rows.extend(flat_stats_rows(value, f"{name}."))
        elif isinstance(value, list):
            if value and isinstance(value[0], dict):
                continue
            rows.append({"metric": name, "value": " | ".join(str(item) for item in value)})
        else:
            rows.append({"metric": name, "value": value})
    return rows

def viewer_fields(lines: gpd.GeoDataFrame, result) -> list[str]:
    """Column order for the attribute table: the answer first, the source after.

    The raw date fields are left out because they arrive as epoch milliseconds
    and read as eleven-digit noise next to the parsed dates that replace them.
    They stay in the GeoPackage and the CSV, where the raw value is the point.
    """
    lead = [
        ledge_analysis.UID_COLUMN,
        ledge_analysis.EFFECTIVE_DATE_COLUMN,
        ledge_analysis.POST_CUTOFF_COLUMN,
        ledge_analysis.LENGTH_COLUMN,
        ledge_analysis.LEDGE_LENGTH_COLUMN,
        ledge_analysis.LEDGE_PCT_COLUMN,
        ledge_analysis.IN_LEDGE_COLUMN,
        ledge_analysis.LEDGE_CLASSES_COLUMN,
        "date_installed",
        "date_created",
    ]
    dropped = {lines.geometry.name, "__vid"}
    dropped.update(name for name in (result.dates.install_column, result.dates.creation_column) if name)
    ordered = [name for name in lead if name in lines.columns]
    ordered += [
        name for name in lines.columns if name not in dropped and name not in ordered
    ]
    return ordered

def build_viewer(args, result, out_dir: Path, cutoff: str) -> Path | None:
    mainlines = result.mainlines
    simplify = None
    if args.simplify_ft:
        simplify = args.simplify_ft / ledge_analysis.unit_to_feet(result.analysis_crs)

    cap = args.max_viewer_features or None
    lines_for_map, line_note = leaflet_viewer.cap_features(
        mainlines, cap, order_by=[ledge_analysis.LENGTH_COLUMN]
    )
    ledge_for_map, ledge_note = leaflet_viewer.cap_features(
        result.ledge, cap, order_by=[]
    )

    fields = viewer_fields(lines_for_map, result)

    layers = [
        leaflet_viewer.make_layer(
            ledge_for_map,
            "Ledge (MassGIS)",
            layer_id="ledge",
            style={
                "color": LEDGE_FILL,
                "weight": 1,
                "opacity": 0.85,
                "fillColor": LEDGE_FILL,
                "fillOpacity": 0.35,
            },
            legend=[{"label": "Ledge / shallow bedrock", "color": LEDGE_FILL}],
            simplify_tolerance=simplify,
        ),
        leaflet_viewer.make_layer(
            lines_for_map,
            "Main lines",
            layer_id="mainlines",
            fields=fields,
            style={"color": MAIN_PRE, "weight": 2, "opacity": 0.85},
            style_rules=[
                {
                    "field": ledge_analysis.POST_CUTOFF_COLUMN,
                    "value": True,
                    "style": {"color": MAIN_POST, "weight": 3, "opacity": 0.95},
                }
            ],
            legend=[
                {"label": f"Main line, on/after {cutoff}", "color": MAIN_POST},
                {"label": f"Main line, before {cutoff}", "color": MAIN_PRE},
            ],
            simplify_tolerance=simplify,
        ),
    ]

    if not result.ledge_segments.empty:
        segments_for_map, _ = leaflet_viewer.cap_features(
            result.ledge_segments, cap, order_by=["segment_length_ft"]
        )
        layers.append(
            leaflet_viewer.make_layer(
                segments_for_map,
                "Main line in ledge",
                layer_id="in_ledge",
                style={"color": LEDGE_SEGMENT, "weight": 5, "opacity": 0.9},
                legend=[{"label": "Main line inside ledge", "color": LEDGE_SEGMENT}],
                simplify_tolerance=simplify,
            )
        )

    stats = dict(result.stats)
    notes = list(result.warnings)
    notes += [note for note in (line_note, ledge_note) if note]
    if not result.by_class.empty:
        notes.append(
            "Ledge classes overlap, so the by-class rows add up to more than the "
            "de-duplicated ledge total."
        )
    stats["notes"] = notes

    tables = []
    if not result.by_class.empty:
        tables.append(leaflet_viewer.make_table("Ledge by class", result.by_class, "by_class"))

    return leaflet_viewer.build_viewer(
        out_dir / f"{args.basename}_viewer.html",
        layers,
        title="Main lines vs ledge",
        subtitle=(
            f"Cutoff {cutoff} - {result.stats['totals']['line_count']:,} lines - "
            f"measured in {result.analysis_crs.name}"
        ),
        stats=stats,
        stats_title=f"Ledge and age summary (cutoff {cutoff})",
        tables=tables,
        active_tab="mainlines",
        inline_leaflet=args.inline_leaflet,
        external_data=args.external_data,
    )

def main(argv=None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    extent, extent_crs = parse_extent(args)

    # A URL copied out of Teams or a browser arrives wrapped in an HTML anchor,
    # which reaches argparse as one long unusable string. The ledge source was
    # already cleaned; the main line source is pasted the same way and needs it
    # just as much.
    args.mainlines = _clean_source_value(args.mainlines) or args.mainlines

    # --anonymous is the only global switch. Beyond it, each source is judged on
    # its own host, so a public main line layer and an internal ledge layer in
    # the same run each get what they need and nothing more.
    allow_sign_in = not args.anonymous
    mainlines_sign_in = allow_sign_in and _source_requires_sign_in(args.mainlines)

    if args.count_only:
        count = vector_source.count_features(
            args.mainlines, args.where, sign_in=mainlines_sign_in
        )
        if count is None:
            progress("A local file has no server-side count; read it to count rows.")
            return 0
        progress(f"{count:,} main lines match {args.where!r}.")
        return 0

    progress("--- Main lines ---")
    mainlines = vector_source.read_source(
        args.mainlines,
        layer=args.mainlines_layer,
        where=args.where,
        out_fields=args.out_fields,
        bounds=extent,
        bounds_crs=extent_crs,
        workers=args.workers,
        batch_size=args.batch_size,
        sign_in=mainlines_sign_in,
    )
    if mainlines.empty:
        progress("No main lines matched. Nothing to analyse.")
        return 1
    progress(f"Main lines: {len(mainlines):,} features, CRS {mainlines.crs}")

    progress("--- Ledge ---")
    bounds = extent
    bounds_crs = extent_crs
    if bounds is None:
        bounds = tuple(mainlines.total_bounds)
        bounds_crs = mainlines.crs
    if mainlines.crs is None:
        progress(
            "The main lines have no CRS, so there is no way to look up ledge for "
            "their extent. Set one with --analysis-crs on the source export, or "
            "check the service metadata."
        )
        return 1
    # Printed every run: an extent that disagrees with the mainline coordinates
    # is the usual reason a ledge lookup comes back empty.
    progress(f"Ledge lookup extent: {tuple(round(float(v), 1) for v in bounds)} in {bounds_crs}")
    ledge, profile, ledge_description = load_ledge(args, bounds, bounds_crs, allow_sign_in)

    if ledge.empty and not args.allow_empty_ledge:
        progress("")
        progress("=== No ledge found, stopping ===")
        progress("Every ledge number would be zero, which reads as an answer and is not one.")
        progress("Check the ledge source on its own:")
        progress("  python scripts/fetch_massgis_ledge.py --out outputs/ledge.gpkg --self-test")
        progress("Then, if that works, re-run with a wider definition or this flag:")
        progress("  --ledge-profile broad     widen what counts as ledge")
        progress("  --allow-empty-ledge       report zeros anyway")
        return 1

    ledge = buffer_ledge(ledge, args.ledge_buffer_ft)

    progress("--- Analysis ---")
    result = ledge_analysis.analyze(
        mainlines,
        ledge,
        cutoff=args.since,
        analysis_crs=args.analysis_crs,
        install_fields=[part.strip() for part in args.install_fields.split(",") if part.strip()],
        creation_fields=[part.strip() for part in args.creation_fields.split(",") if part.strip()],
        class_field=args.ledge_class_field,
        ledge_profile=profile,
        ledge_description=ledge_description,
    )

    for warning in result.warnings:
        progress(f"NOTE: {warning}")

    summary_json = out_dir / f"{args.basename}_summary.json"
    summary_json.write_text(json.dumps(result.stats, indent=2), encoding="utf-8")
    pd.DataFrame(flat_stats_rows(result.stats)).to_csv(
        out_dir / f"{args.basename}_summary.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(result.stats["crosstab"]).to_csv(
        out_dir / f"{args.basename}_crosstab.csv", index=False, encoding="utf-8-sig"
    )
    if not result.by_class.empty:
        result.by_class.to_csv(
            out_dir / f"{args.basename}_by_ledge_class.csv", index=False, encoding="utf-8-sig"
        )
    result.mainlines.drop(columns=result.mainlines.geometry.name).to_csv(
        out_dir / f"{args.basename}_mainlines.csv", index=False, encoding="utf-8-sig"
    )

    if not args.no_gpkg:
        gpkg = out_dir / f"{args.basename}.gpkg"
        if gpkg.exists():
            gpkg.unlink()
        result.mainlines.to_file(gpkg, layer="mainlines", driver="GPKG")
        if not result.ledge.empty:
            result.ledge.to_file(gpkg, layer="ledge", driver="GPKG")
        if not result.ledge_segments.empty:
            result.ledge_segments.to_file(gpkg, layer="mainline_in_ledge", driver="GPKG")
        progress(f"GeoPackage: {gpkg}")

    viewer_path = None
    if not args.no_viewer:
        progress("--- Viewer ---")
        viewer_path = build_viewer(args, result, out_dir, result.stats["cutoff_date"])

    progress("")
    progress("=== Result ===")
    for line in result.stats["headline"]:
        progress(f"  {line}")
    progress("")
    progress(f"Ledge definition: {ledge_description}")
    progress(f"Date fields used: {result.dates.describe()}")
    progress(f"Summary: {summary_json}")
    if viewer_path:
        progress(f"Viewer:  {viewer_path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
