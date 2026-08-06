from __future__ import annotations
import inspect
import json
import os
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import requests

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
TOKEN_CACHE_SERVICE = "Estimate_GIS"
TOKEN_CACHE_USERNAME = "ArcGISPortalToken"
TOKEN_CACHE_EXPIRY_BUFFER_SECONDS = 300

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

def read_cached_token() -> str | None:
    try:
        import keyring
    except ImportError:
        return None
    try:
        cached_text = keyring.get_password(TOKEN_CACHE_SERVICE, TOKEN_CACHE_USERNAME)
    except RuntimeError as exc:
        print(f"Keyring read failed: {exc}")
        return None
    if not cached_text:
        return None
    try:
        cached = json.loads(cached_text)
    except json.JSONDecodeError:
        return None
    token = cached.get("token")
    expires_at = float(cached.get("expires_at", 0) or 0)
    if not token:
        return None
    if expires_at and time.time() >= expires_at - TOKEN_CACHE_EXPIRY_BUFFER_SECONDS:
        print("Cached ArcGIS token is expired or near expiry; refreshing interactively.")
        return None
    return str(token)

def write_cached_token(token: str, token_data: dict[str, object] | None = None) -> None:
    try:
        import keyring
    except ImportError:
        return
    token_data = token_data or {}
    expires_at = 0.0
    expires_in = token_data.get("expires_in")
    expires = token_data.get("expires") or token_data.get("expiration")
    try:
        if expires_in is not None:
            expires_at = time.time() + float(expires_in)
        elif expires is not None:
            expires_value = float(expires)
            expires_at = expires_value / 1000 if expires_value > 9999999999 else expires_value
    except (TypeError, ValueError):
        expires_at = 0.0
    if not expires_at:
        expires_at = time.time() + 55 * 60
    payload = json.dumps({"token": token, "expires_at": expires_at})
    try:
        keyring.set_password(TOKEN_CACHE_SERVICE, TOKEN_CACHE_USERNAME, payload)
        print("Stored ArcGIS token in Windows keyring cache.")
    except RuntimeError as exc:
        print(f"Keyring write failed: {exc}")

def validate_token(token: str) -> bool:
    try:
        response = requests.get(LAYER_URL, params={"f": "json", "token": token}, timeout=60)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"Cached token validation failed: {exc}")
        return False
    error = data.get("error") if isinstance(data, dict) else None
    if error:
        print(f"Cached token rejected: {error}")
        return False
    return True

def capture_loopback_authorization_code(authorize_url: str, port: int = 8080) -> str:
    result: dict[str, str] = {}
    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed_url = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed_url.query)
            code = query.get("code", [""])[0]
            error = query.get("error", [""])[0]
            if code:
                result["code"] = code
            if error:
                result["error"] = error
            html = """
<html>
<head><title>ArcGIS OAuth Complete</title></head>
<body>
<p>ArcGIS sign-in complete. This tab can close.</p>
<script>window.open('', '_self'); window.close();</script>
</body>
</html>
""".strip().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        def log_message(self, format: str, *args: object) -> None:
            return
    server = HTTPServer(("localhost", port), CallbackHandler)
    server.timeout = 300
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    print(f"Opening browser for ArcGIS sign-in on localhost:{port}...")
    webbrowser.open(authorize_url)
    thread.join(305)
    server.server_close()
    if result.get("error"):
        raise RuntimeError(f"ArcGIS OAuth error: {result['error']}")
    if not result.get("code"):
        raise RuntimeError("ArcGIS OAuth did not return an authorization code.")
    return result["code"]

def interactive_loopback_access_token() -> str | None:
    portal_url = os.environ.get("ARCGIS_PORTAL_URL", DEFAULT_PORTAL_URL).rstrip("/")
    client_id = os.environ.get("ARCGIS_CLIENT_ID", DEFAULT_CLIENT_ID)
    redirect_uri = os.environ.get("ARCGIS_REDIRECT_URI", DEFAULT_REDIRECT_URI)
    authorize_params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "expiration": "1440",
    }
    authorize_url = f"{portal_url}/sharing/rest/oauth2/authorize?" + urllib.parse.urlencode(authorize_params)
    code = capture_loopback_authorization_code(authorize_url, port=8080)
    token_url = f"{portal_url}/sharing/rest/oauth2/token"
    token_response = requests.post(
        token_url,
        data={
            "f": "json",
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=60,
    )
    token_response.raise_for_status()
    token_data = token_response.json()
    if "error" in token_data:
        raise RuntimeError(json.dumps(token_data["error"], indent=2))
    token = token_data.get("access_token") or token_data.get("token")
    if not token:
        raise RuntimeError(f"ArcGIS token response did not include a token: {token_data}")
    os.environ["ARCGIS_TOKEN"] = str(token)
    write_cached_token(str(token), token_data)
    return str(token)

def resolve_token() -> str | None:
    cached_token = read_cached_token()
    if cached_token and validate_token(cached_token):
        os.environ["ARCGIS_TOKEN"] = cached_token
        print("Using validated ArcGIS token from auth/keyring cache")
        return cached_token
    env_names = [name for name in (TOKEN_ENV, "ARCGIS_TOKEN", "PORTAL_TOKEN", "GIS_TOKEN") if name]
    for name in env_names:
        token = os.environ.get(name)
        if token:
            print(f"Using token from environment variable: {name}")
            return token
    try:
        token = interactive_loopback_access_token()
    except (OSError, RuntimeError, ValueError, requests.RequestException) as exc:
        print(f"Interactive ArcGIS token flow failed: {exc}")
        return None
    if token:
        print("Using token from interactive OAuth loopback flow")
        return token
    return None

def call_exporter() -> object:
    token = resolve_token()
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
        "objectid_batch_size": BATCH_SIZE,
        "object_id_batch_size": BATCH_SIZE,
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
    if hasattr(gdf, "__len__"):
        print(f"Export complete. Rows: {len(gdf)}")
    else:
        print("Export complete.")
    print(f"GeoPackage: {OUT_GPKG}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
