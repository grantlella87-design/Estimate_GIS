"""Download MassGIS ledge polygons to a GeoPackage. Nothing else.

This is the ledge half of the analysis on its own, with no National Grid layer,
no sign-in and no VPN involved. Run it when the report is not producing ledge and
you need to know which half is at fault:

    # Statewide. ~80,000 polygons, a minute or two on a good connection.
    python scripts/fetch_massgis_ledge.py --out outputs/ledge.gpkg

    # Just enough to prove the connection works, in seconds
    python scripts/fetch_massgis_ledge.py --out outputs/ledge_test.gpkg --self-test

    # One area, given as minx,miny,maxx,maxy
    python scripts/fetch_massgis_ledge.py --out outputs/ledge.gpkg \
        --extent 200000,890000,215000,905000

Then hand the file to the report, which will not touch MassGIS at all:

    python scripts/mainline_ledge_report.py --ledge outputs/ledge.gpkg
"""
from __future__ import annotations

import argparse
import importlib
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

enterprise_network = importlib.import_module("enterprise_network")
enterprise_network.configure_enterprise_network()

import massgis_ledge
from arcgis_rest_geopandas import progress

# Small enough to finish in seconds, and in an area with mapped bedrock, so an
# empty result means something is wrong rather than that there is no ledge here.
SELF_TEST_EXTENT = (200000.0, 890000.0, 205000.0, 895000.0)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", default="outputs/ledge.gpkg", help="Output GeoPackage.")
    parser.add_argument("--layer-name", default="ledge", help="Layer name inside it.")
    parser.add_argument(
        "--profile",
        default=massgis_ledge.DEFAULT_LEDGE_PROFILE,
        choices=sorted(massgis_ledge.LEDGE_PROFILES),
        help="Which MassGIS classes count as ledge.",
    )
    parser.add_argument(
        "--extent", help="Limit to minx,miny,maxx,maxy. Omit for statewide."
    )
    parser.add_argument(
        "--extent-crs", default="26986", help="CRS of --extent. Default: EPSG:26986."
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Fetch a small known-good area and report whether MassGIS is reachable.",
    )
    parser.add_argument(
        "--agol",
        action="store_true",
        help=(
            "Use the 1:250,000 copy on ArcGIS Online instead of the 1:24,000 "
            "MassGIS service. Coarser, but on a host most networks allow."
        ),
    )
    parser.add_argument(
        "--workers", type=int, default=6, help="Parallel download workers. Default: 6."
    )
    parser.add_argument(
        "--batch-size", type=int, default=500, help="OBJECTID batch size. Default: 500."
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    extent = None
    extent_crs = args.extent_crs
    if args.self_test:
        extent, extent_crs = SELF_TEST_EXTENT, 26986
        progress("Self-test: fetching a small area of Worcester known to have ledge.")
    elif args.extent:
        parts = [part.strip() for part in args.extent.split(",")]
        if len(parts) != 4:
            raise SystemExit("--extent must be minx,miny,maxx,maxy")
        extent = tuple(float(part) for part in parts)

    if args.agol:
        progress("Source: ArcGIS Online, 1:250,000 (Till or Bedrock)")
        progress(f"Service: {massgis_ledge.AGOL_SURFICIAL_GEOLOGY_LAYER}")
    else:
        progress(f"Profile: {args.profile} ({massgis_ledge.describe_profile(args.profile)})")
        progress(f"Service: {massgis_ledge.SURFICIAL_GEOLOGY_SERVICE}")
    if extent is None:
        progress("Extent: statewide. This is the slow one; --extent is much faster.")

    started = time.time()
    try:
        if args.agol:
            gdf = massgis_ledge.fetch_agol_ledge_polygons(
                bounds=extent,
                bounds_crs=extent_crs if extent is not None else None,
                workers=args.workers,
                batch_size=args.batch_size,
            )
        else:
            gdf = massgis_ledge.fetch_ledge_polygons(
                profile=args.profile,
                bounds=extent,
                bounds_crs=extent_crs if extent is not None else None,
                workers=args.workers,
                batch_size=args.batch_size,
            )
    except Exception as exc:  # noqa: BLE001 - the whole point is to report the failure
        progress("")
        progress("=== MassGIS fetch FAILED ===")
        progress(f"{type(exc).__name__}: {exc}")
        progress("")
        import service_auth

        progress(service_auth.connection_hint(massgis_ledge.MAP_UNIT_LAYER_URL))
        progress("")
        if not args.agol:
            progress("The quickest way round a blocked MassGIS host is the copy on")
            progress("ArcGIS Online, which most networks allow. It is coarser:")
            progress(f"  python {Path(__file__).name} --out {args.out} --agol --self-test")
        progress("Other things worth checking:")
        progress("  - TLS interception. Estimate_GIS uses the Windows trust store")
        progress("    through truststore; confirm it is installed: pip show truststore")
        progress("  - Rate limiting, ruled out with:  --workers 1 --batch-size 250")
        return 1
    elapsed = time.time() - started

    if gdf.empty:
        progress("")
        progress("=== The service answered, but returned no ledge ===")
        progress("The connection works. Either the extent is outside Massachusetts,")
        progress("or it covers ground with no mapped bedrock. Prove the connection")
        progress("with a known-good area:  --self-test")
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    gdf.to_file(out, layer=args.layer_name, driver="GPKG")

    progress("")
    progress("=== Ledge downloaded ===")
    progress(f"  polygons: {len(gdf):,}")
    progress(f"  acres:    {gdf.area.sum() / 4046.86:,.0f}")
    progress(f"  crs:      {gdf.crs}")
    progress(f"  seconds:  {elapsed:.0f}")
    progress(f"  file:     {out}  (layer {args.layer_name})")
    for (source, code), count in gdf.groupby(["ledge_source", "ledge_code"]).size().items():
        progress(f"    {code:<4} {source:<28} {count:,}")
    progress("")
    progress("Use it without touching MassGIS again:")
    progress(f"  python scripts/mainline_ledge_report.py --ledge {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
