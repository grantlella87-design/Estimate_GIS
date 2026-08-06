
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from arcgis_rest_geopandas import export_layer_to_geopackage


def main() -> int:
    parser = argparse.ArgumentParser(description="Fast ArcGIS REST export to GeoPackage using GeoPandas.")
    parser.add_argument("--layer-url", required=True, help="ArcGIS MapServer/FeatureServer layer URL without /query.")
    parser.add_argument("--where", default="1=1", help="ArcGIS WHERE clause. The same WHERE is used for ID selection and download.")
    parser.add_argument("--out-fields", default="*", help="Comma-separated field list or *.")
    parser.add_argument("--out-gpkg", required=True, help="Output GeoPackage path.")
    parser.add_argument("--layer-name", default="arcgis_export", help="GeoPackage layer name.")
    parser.add_argument("--token-env", default="ARCGIS_TOKEN", help="Environment variable containing ArcGIS token.")
    parser.add_argument("--workers", type=int, default=8, help="Parallel object-id download workers.")
    parser.add_argument("--batch-size", type=int, default=500, help="OBJECTID batch size.")
    args = parser.parse_args()
    token = os.environ.get(args.token_env)
    gdf = export_layer_to_geopackage(
        layer_url=args.layer_url,
        output_gpkg=args.out_gpkg,
        layer_name=args.layer_name,
        where=args.where,
        out_fields=args.out_fields,
        token=token,
        objectid_batch_size=args.batch_size,
        workers=args.workers,
    )
    print(f"Exported {len(gdf):,} features to {args.out_gpkg} layer {args.layer_name}", flush=True)
    print(f"CRS: {gdf.crs}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

