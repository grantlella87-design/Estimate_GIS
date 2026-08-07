
from __future__ import annotations
import json
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(r"C:\Users\lellag\Downloads\GISportal")
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

URL = "https://arcgisserver.digital.mass.gov/arcgisserver/rest/services/AGOL/SurfGeo24k/MapServer/1"
BOUNDS = (225000.0, 890000.0, 245000.0, 910000.0)
BOUNDS_CRS = "EPSG:26986"
CLASS_FIELD = "MAPUNIT"
HOST = "arcgisserver.digital.mass.gov"

try:
    import enterprise_network
    enterprise_network.configure_enterprise_network()
except Exception as exc:
    print(f"[AGOL TEST] enterprise_network warning: {type(exc).__name__}: {exc}")

import requests

_ORIGINAL_REQUEST = requests.sessions.Session.request

def _append_params(url: str, params) -> str:
    if not params:
        return str(url)
    if isinstance(params, bytes):
        query = params.decode("utf-8", errors="replace")
    elif isinstance(params, str):
        query = params
    else:
        query = urllib.parse.urlencode(params, doseq=True)
    return str(url) + ("&" if "?" in str(url) else "?") + query

def _invoke_restmethod(method: str, url: str, **kwargs):
    if (method or "GET").upper() != "GET":
        raise RuntimeError("This AGOL test fallback only supports GET")
    full_url = _append_params(url, kwargs.get("params"))
    headers = dict(kwargs.get("headers") or {})
    headers.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    headers.setdefault("Accept", "application/json,text/plain,*/*")
    timeout = int(kwargs.get("timeout") or 180)
    spec = {"url": full_url, "headers": headers, "timeout": timeout}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(spec, handle)
        spec_path = handle.name
    ps_lines = [
        "$ErrorActionPreference = 'Stop'",
        "$spec = Get-Content -Raw -LiteralPath $args[0] | ConvertFrom-Json",
        "$headers = @{}",
        "foreach ($p in $spec.headers.PSObject.Properties) { $headers[$p.Name] = [string]$p.Value }",
        "$r = Invoke-RestMethod -Uri ([string]$spec.url) -Method Get -Headers $headers -TimeoutSec ([int]$spec.timeout)",
        "$r | ConvertTo-Json -Depth 100 -Compress",
    ]
    ps = "\n".join(ps_lines)
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps, spec_path],
            capture_output=True,
            text=True,
            timeout=timeout + 30,
        )
    finally:
        try:
            Path(spec_path).unlink(missing_ok=True)
        except Exception:
            pass
    response = requests.Response()
    response.url = full_url
    response.encoding = "utf-8"
    response.headers["Content-Type"] = "application/json"
    response.status_code = 200 if proc.returncode == 0 else 599
    response._content = (proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout)).encode("utf-8", errors="replace")
    print(f"[AGOL TEST] PowerShell transport GET {full_url}")
    return response

def _patched_request(self, method, url, **kwargs):
    host = urllib.parse.urlparse(str(url)).netloc.lower()
    if host == HOST:
        return _invoke_restmethod(method, str(url), **kwargs)
    return _ORIGINAL_REQUEST(self, method, url, **kwargs)

requests.sessions.Session.request = _patched_request
print("[AGOL TEST] requests transport patched for", HOST)
print("[AGOL TEST] no distribution/service/mainline query will run")
print("[AGOL TEST] URL:", URL)
print("[AGOL TEST] bounds:", BOUNDS)
print("[AGOL TEST] bounds_crs:", BOUNDS_CRS)

session = requests.Session()
metadata = session.get(URL, params={"f": "pjson"}, timeout=180).json()
print("[AGOL TEST] metadata name:", metadata.get("name"))
print("[AGOL TEST] geometryType:", metadata.get("geometryType"))
print("[AGOL TEST] maxRecordCount:", metadata.get("maxRecordCount"))

query_url = URL.rstrip("/") + "/query"
count = session.get(
    query_url,
    params={
        "f": "pjson",
        "where": "1=1",
        "geometry": ",".join(str(int(v)) for v in BOUNDS),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "26986",
        "spatialRel": "esriSpatialRelIntersects",
        "returnCountOnly": "true",
    },
    timeout=180,
).json()
print("[AGOL TEST] extent count:", count.get("count"))

sample = session.get(
    query_url,
    params={
        "f": "pjson",
        "where": "1=1",
        "geometry": ",".join(str(int(v)) for v in BOUNDS),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "26986",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": f"OBJECTID,{CLASS_FIELD}",
        "returnGeometry": "false",
        "resultRecordCount": "5",
    },
    timeout=180,
).json()
features = sample.get("features") or []
print("[AGOL TEST] sample features:", len(features))
for feature in features[:5]:
    print("[AGOL TEST] sample attributes:", feature.get("attributes"))

print("[AGOL TEST] importing vector_source and running exact ledge read_source path")
import vector_source

gdf = vector_source.read_source(
    URL,
    layer=None,
    bounds=BOUNDS,
    bounds_crs=BOUNDS_CRS,
    workers=1,
    batch_size=25,
    sign_in=False,
)
print("[AGOL TEST] vector_source rows:", len(gdf))
print("[AGOL TEST] vector_source crs:", gdf.crs)
print("[AGOL TEST] vector_source columns:", list(gdf.columns))
if CLASS_FIELD in gdf.columns:
    vals = sorted(str(x) for x in gdf[CLASS_FIELD].dropna().unique())[:20]
    print("[AGOL TEST] MAPUNIT preview:", vals)
if not gdf.empty:
    print("[AGOL TEST] first rows:")
    print(gdf.drop(columns="geometry", errors="ignore").head().to_string())
print("[AGOL TEST] complete")
