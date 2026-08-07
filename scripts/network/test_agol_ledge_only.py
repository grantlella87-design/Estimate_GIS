from __future__ import annotations
import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
try:
    import enterprise_network
    enterprise_network.configure_enterprise_network()
except Exception as exc:
    print(f"[AGOL TEST] enterprise_network setup warning: {type(exc).__name__}: {exc}")
DEFAULT_URL = "https://arcgisserver.digital.mass.gov/arcgisserver/rest/services/AGOL/SurfGeo24k/MapServer/1"
DEFAULT_BOUNDS = (225000.0, 890000.0, 245000.0, 910000.0)
DEFAULT_BOUNDS_CRS = "EPSG:26986"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/127.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://massgis.maps.arcgis.com/",
}
def get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS, method="GET")
    with urllib.request.urlopen(req, timeout=120) as resp:
        text = resp.read().decode("utf-8", errors="replace")
        print(f"[AGOL TEST] HTTP {resp.status} | {resp.headers.get('content-type')}")
        return json.loads(text)
def query_url(base_url: str, params: dict[str, object]) -> str:
    return base_url.rstrip("/") + "/query?" + urllib.parse.urlencode(params)
def raw_rest_tests(base_url: str, class_field: str) -> None:
    print("[AGOL TEST] Raw REST metadata")
    metadata = get_json(base_url.rstrip("/") + "?f=pjson")
    print("[AGOL TEST] name =", metadata.get("name"))
    print("[AGOL TEST] geometryType =", metadata.get("geometryType"))
    print("[AGOL TEST] maxRecordCount =", metadata.get("maxRecordCount"))
    print("[AGOL TEST] extent spatialReference =", metadata.get("extent", {}).get("spatialReference"))
    print("[AGOL TEST] Raw REST count")
    count = get_json(query_url(base_url, {"where": "1=1", "returnCountOnly": "true", "f": "pjson"}))
    print("[AGOL TEST] count =", count.get("count"))
    print("[AGOL TEST] Raw REST sample without geometry")
    sample = get_json(query_url(base_url, {"where": "1=1", "outFields": f"OBJECTID,{class_field}", "returnGeometry": "false", "resultRecordCount": "5", "f": "pjson"}))
    features = sample.get("features") or []
    print("[AGOL TEST] sample feature count =", len(features))
    for feature in features[:5]:
        print("[AGOL TEST] sample attributes =", feature.get("attributes"))
def vector_source_test(base_url: str, class_field: str, bounds: tuple[float, float, float, float], bounds_crs: str) -> None:
    print("[AGOL TEST] vector_source.read_source bounded test")
    print("[AGOL TEST] bounds =", bounds)
    print("[AGOL TEST] bounds_crs =", bounds_crs)
    import vector_source
    gdf = vector_source.read_source(
        base_url,
        layer=None,
        bounds=bounds,
        bounds_crs=bounds_crs,
        workers=1,
        batch_size=25,
        sign_in=False,
    )
    print("[AGOL TEST] GeoDataFrame rows =", len(gdf))
    print("[AGOL TEST] GeoDataFrame CRS =", gdf.crs)
    print("[AGOL TEST] Columns =", list(gdf.columns))
    if class_field in gdf.columns:
        print("[AGOL TEST] Unique class values preview =", sorted(str(x) for x in gdf[class_field].dropna().unique())[:20])
    if not gdf.empty:
        print("[AGOL TEST] First row attributes =", gdf.drop(columns="geometry", errors="ignore").head(1).to_dict("records"))
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AGOL-only ledge test. Does not pull distribution mains.")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--ledge-class-field", default="MAPUNIT")
    parser.add_argument("--bounds", default=",".join(str(x) for x in DEFAULT_BOUNDS), help="minx,miny,maxx,maxy in --bounds-crs")
    parser.add_argument("--bounds-crs", default=DEFAULT_BOUNDS_CRS)
    parser.add_argument("--skip-vector-source", action="store_true")
    return parser.parse_args()
def main() -> int:
    args = parse_args()
    parts = [float(part.strip()) for part in args.bounds.split(",")]
    if len(parts) != 4:
        raise SystemExit("--bounds must be minx,miny,maxx,maxy")
    bounds = tuple(parts)
    print("[AGOL TEST] URL =", args.url)
    print("[AGOL TEST] token/sign-in = False")
    raw_rest_tests(args.url, args.ledge_class_field)
    if not args.skip_vector_source:
        vector_source_test(args.url, args.ledge_class_field, bounds, args.bounds_crs)
    print("[AGOL TEST] complete")
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
