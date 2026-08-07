import json
import urllib.parse
import urllib.request

BASE_URL = "https://arcgisserver.digital.mass.gov/arcgisserver/rest/services/AGOL/SurfGeo24k/FeatureServer/0"

def get_json(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        text = resp.read().decode("utf-8", errors="replace")
        print("STATUS:", resp.status)
        print("CONTENT-TYPE:", resp.headers.get("content-type"))
        return json.loads(text)

metadata = get_json(BASE_URL + "?f=pjson")
print("Layer name:", metadata.get("name"))
print("Geometry type:", metadata.get("geometryType"))
print("Max record count:", metadata.get("maxRecordCount"))
print("Supported query formats:", metadata.get("supportedQueryFormats"))

params = {
    "where": "1=1",
    "returnCountOnly": "true",
    "f": "pjson",
}
count_url = BASE_URL + "/query?" + urllib.parse.urlencode(params)
count_result = get_json(count_url)
print("Count result:", count_result)