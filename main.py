from __future__ import annotations

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
# Paste the ArcGIS REST layer URL here. It should usually end in /FeatureServer/<n>
# or /MapServer/<n>, not the Instant App URL.
LAYER_URL = ""

# SQL where clause used by the ArcGIS REST API.
WHERE = "1=1"

# Fields to request. Use "*" for all fields.
OUT_FIELDS = "*"

# Output GeoPackage path.
OUT_GPKG = REPO_ROOT / "outputs" / "export.gpkg"

# GeoPackage layer name.
LAYER_NAME = "export"

# Optional token environment variable. Keep as None for public layers.
TOKEN_ENV: str | None = None

# Batch/concurrency settings.
WORKERS = 8
BATCH_SIZE = 1000
# =============================================================================

def main() -> int:
    if not LAYER_URL.strip():
        print("Set LAYER_URL at the top of main.py before running.")
        print("Example layer URL format: https://.../arcgis/rest/services/.../FeatureServer/0")
        return 2
    OUT_GPKG.parent.mkdir(parents=True, exist_ok=True)
    token = os.environ.get(TOKEN_ENV) if TOKEN_ENV else None
    print(f"Layer URL: {LAYER_URL}")
    print(f"Where: {WHERE}")
    print(f"Output: {OUT_GPKG}")
    gdf = export_layer_to_geopackage(
        layer_url=LAYER_URL,
        where=WHERE,
        out_fields=OUT_FIELDS,
        out_gpkg=OUT_GPKG,
        layer_name=LAYER_NAME,
        token=token,
        workers=WORKERS,
        batch_size=BATCH_SIZE,
    )
    print(f"Export complete. Rows: {len(gdf)}")
    print(f"GeoPackage: {OUT_GPKG}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
