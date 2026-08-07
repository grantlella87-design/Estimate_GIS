from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from enterprise_network import configure_enterprise_network

configure_enterprise_network()
import service_auth
from arcgis_rest_geopandas import export_layer_to_geopackage

# =============================================================================
# USER EDIT SECTION
# =============================================================================
LAYER_URL = "https://gis.nationalgrid.com/arcgis/rest/services/MA/Material_View_MA/MapServer/341"
WHERE = "1=1"
OUT_FIELDS = "*"
OUT_GPKG = REPO_ROOT / "outputs" / "export.gpkg"
LAYER_NAME = "export"
WORKERS = 8
BATCH_SIZE = 2000
# =============================================================================

def main() -> int:
    os.environ.setdefault("ESTIMATE_GIS_PROGRESS", "1")
    os.environ.setdefault("ESTIMATE_GIS_OBJECTID_BATCH_SIZE", str(BATCH_SIZE))
    OUT_GPKG.parent.mkdir(parents=True, exist_ok=True)
    print(f"Layer URL: {LAYER_URL}")
    print(f"Where: {WHERE}")
    print(f"Output: {OUT_GPKG}")
    # Whether this layer needs a token is read off its host, so pointing
    # LAYER_URL at a public service needs no other edit here.
    print(service_auth.describe(LAYER_URL))
    gdf = export_layer_to_geopackage(
        layer_url=LAYER_URL,
        output_gpkg=OUT_GPKG,
        layer_name=LAYER_NAME,
        where=WHERE,
        out_fields=OUT_FIELDS,
        objectid_batch_size=BATCH_SIZE,
        workers=WORKERS,
    )
    print(f"Export complete. Rows: {len(gdf)}")
    print(f"GeoPackage: {OUT_GPKG}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
