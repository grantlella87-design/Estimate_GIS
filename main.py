from __future__ import annotations
import inspect
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

def configure_enterprise_ssl() -> None:
    try:
        import truststore
    except ImportError:
        return
    try:
        truststore.inject_into_ssl()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"WARNING: Could not enable Windows trust store: {exc}")

configure_enterprise_ssl()
from arcgis_rest_geopandas import export_layer_to_geopackage  # noqa: E402

DEFAULT_PORTAL_URL = "https://gis.nationalgrid.com/portal"
DEFAULT_CLIENT_ID = "48XCGWtLoUxA3klq"
DEFAULT_REDIRECT_URI = "http://localhost:8080/"
TOKEN_ENV: str | None = "ARCGIS_TOKEN"

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

def _extract_token(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("access_token", "token", "value"):
            token = value.get(key)
            if isinstance(token, str) and token.strip():
                return token.strip()
    return None

def _call_auth_function(function: object) -> str | None:
    signature = inspect.signature(function)
    kwargs: dict[str, object] = {}
    for name, parameter in signature.parameters.items():
        if parameter.default is not inspect._empty:
            continue
        lower_name = name.lower()
        if lower_name in {"portal_url", "portal", "base_url"}:
            kwargs[name] = os.environ.get("ARCGIS_PORTAL_URL", DEFAULT_PORTAL_URL)
        elif lower_name in {"client_id", "appid", "app_id"}:
            kwargs[name] = os.environ.get("ARCGIS_CLIENT_ID", DEFAULT_CLIENT_ID)
        elif lower_name in {"redirect_uri", "redirect_url", "callback_url"}:
            kwargs[name] = os.environ.get("ARCGIS_REDIRECT_URI", DEFAULT_REDIRECT_URI)
        else:
            return None
    return _extract_token(function(**kwargs))

def resolve_token() -> str | None:
    env_names = [name for name in (TOKEN_ENV, "ARCGIS_TOKEN", "PORTAL_TOKEN", "GIS_TOKEN") if name]
    for name in env_names:
        token = os.environ.get(name)
        if token:
            print(f"Using ArcGIS token from environment variable: {name}")
            return token
    try:
        import auth
    except ImportError as exc:
        raise RuntimeError(f"Could not import src/auth.py: {exc}") from exc
    for function_name in (
        "get_access_token",
        "get_token",
        "access_token",
        "portal_access_token",
        "interactive_access_token",
    ):
        function = getattr(auth, function_name, None)
        if callable(function):
            token = _call_auth_function(function)
            if token:
                print(f"Using ArcGIS token from auth.py::{function_name}")
                return token
    raise RuntimeError("auth.py did not expose a usable ArcGIS token function.")

def call_exporter() -> object:
    token = resolve_token()
    candidate_kwargs = {
        "layer_url": LAYER_URL,
        "where": WHERE,
        "out_fields": OUT_FIELDS,
        "output_gpkg": OUT_GPKG,
        "layer_name": LAYER_NAME,
        "token": token,
        "workers": WORKERS,
        "objectid_batch_size": BATCH_SIZE,
    }
    signature = inspect.signature(export_layer_to_geopackage)
    supported_kwargs = {name: value for name, value in candidate_kwargs.items() if name in signature.parameters}
    print("Exporter signature:", signature)
    print("Using kwargs:", ", ".join(sorted(supported_kwargs)))
    return export_layer_to_geopackage(**supported_kwargs)

def main() -> int:
    os.environ.setdefault("ESTIMATE_GIS_PROGRESS", "1")
    os.environ.setdefault("ESTIMATE_GIS_OBJECTID_BATCH_SIZE", str(BATCH_SIZE))
    if not LAYER_URL.strip():
        print("Set LAYER_URL at the top of main.py before running.")
        return 2
    OUT_GPKG.parent.mkdir(parents=True, exist_ok=True)
    print(f"Layer URL: {LAYER_URL}")
    print(f"Where: {WHERE}")
    print(f"Output: {OUT_GPKG}")
    gdf = call_exporter()
    print(f"Export complete. Rows: {len(gdf)}")
    print(f"GeoPackage: {OUT_GPKG}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
