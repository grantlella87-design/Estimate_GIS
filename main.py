from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from arcgis_rest_geopandas import export_layer_to_geopackage

# =============================================================================
# USER EDIT SECTION
# =============================================================================
LAYER_URL = "https://gis.nationalgrid.com/arcgis/rest/services/MA/Material_View_MA/MapServer/341"
WHERE = "1=1"
OUT_FIELDS = "*"
OUT_GPKG = REPO_ROOT / "outputs" / "export.gpkg"
LAYER_NAME = "export"
TOKEN_ENV: str | None = None
WORKERS = 8
BATCH_SIZE = 1000
# =============================================================================

def call_exporter() -> object:
    token = os.environ.get(TOKEN_ENV) if TOKEN_ENV else None
    candidate_kwargs = {
        "layer_url": LAYER_URL,
        "url": LAYER_URL,
        "where": WHERE,
        "out_fields": OUT_FIELDS,
        "output_gpkg": OUT_GPKG,
        "out_gpkg": OUT_GPKG,
        "gpkg_path": OUT_GPKG,
        "layer_name": LAYER_NAME,
        "token": token,
        "workers": WORKERS,
        "max_workers": WORKERS,
        "batch_size": BATCH_SIZE,
        "object_id_batch_size": BATCH_SIZE,
    }
    signature = inspect.signature(export_layer_to_geopackage)
    supported_kwargs = {
        name: value
        for name, value in candidate_kwargs.items()
        if name in signature.parameters
    }
    print("Exporter signature:", signature)
    print("Using kwargs:", ", ".join(sorted(supported_kwargs)))
    return export_layer_to_geopackage(**supported_kwargs)

def main() -> int:
    if not LAYER_URL.strip():
        print("Set LAYER_URL at the top of main.py before running.")
        return 2
    OUT_GPKG.parent.mkdir(parents=True, exist_ok=True)
    print(f"Layer URL: {LAYER_URL}")
    print(f"Where: {WHERE}")
    print(f"Output: {OUT_GPKG}")
    gdf = call_exporter()
    if hasattr(gdf, "__len__"):
        print(f"Export complete. Rows: {len(gdf)}")
    else:
        print("Export complete.")
    print(f"GeoPackage: {OUT_GPKG}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
