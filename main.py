from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

def configure_enterprise_ssl() -> None:
    """Use the Windows trust store for corporate TLS interception certificates."""
    try:
        import truststore
    except ImportError:
        return
    try:
        truststore.inject_into_ssl()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"WARNING: Could not enable Windows trust store: {exc}")

REPO_ROOT = Path(__file__).resolve().parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

configure_enterprise_ssl()

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
BATCH_SIZE = 2000
# =============================================================================

def disable_explicit_proxy_for_arcgis() -> None:
    """Avoid forcing National Grid ArcGIS traffic through an explicit proxy."""
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ.pop(key, None)
    no_proxy_hosts = [
        "gis.nationalgrid.com",
        ".nationalgrid.com",
        "localhost",
        "127.0.0.1",
    ]
    existing = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    combined = [item for item in existing.split(",") if item]
    for host in no_proxy_hosts:
        if host not in combined:
            combined.append(host)
    os.environ["NO_PROXY"] = ",".join(combined)
    os.environ["no_proxy"] = os.environ["NO_PROXY"]

def call_exporter() -> object:
    disable_explicit_proxy_for_arcgis()
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
