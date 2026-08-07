from __future__ import annotations
"""
diagnose_agol_python_tls.py
Pure-Python diagnostics for why Python cannot reach the public MassGIS ArcGIS endpoint
while PowerShell Invoke-WebRequest can.

This script does NOT query National Grid distribution/service/mainline pipes.
It only tests the MassGIS SurfGeo24k MapServer/1 metadata and a small extent query.
"""
import json
import os
import socket
import ssl
import sys
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

URL = "https://arcgisserver.digital.mass.gov/arcgisserver/rest/services/AGOL/SurfGeo24k/MapServer/1"
QUERY_URL = URL.rstrip("/") + "/query"
HOST = "arcgisserver.digital.mass.gov"
BOUNDS = "225000,890000,245000,910000"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json,text/plain,*/*",
}


def section(title: str) -> None:
    print("\n" + "=" * 88)
    print(title)
    print("=" * 88)


def print_env() -> None:
    section("Python and environment")
    print("sys.executable:", sys.executable)
    print("sys.version:", sys.version.replace("\n", " "))
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "NO_PROXY",
        "no_proxy",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
        "PYTHONHTTPSVERIFY",
        "ESTIMATE_GIS_PROXY_URL",
        "ESTIMATE_GIS_DISABLE_POWERSHELL_TRANSPORT",
    ):
        print(f"{name}=", os.environ.get(name, ""))
    try:
        import certifi
        print("certifi.where():", certifi.where())
    except Exception as exc:
        print("certifi import failed:", repr(exc))
    try:
        import truststore
        print("truststore:", getattr(truststore, "__version__", "installed"))
    except Exception as exc:
        print("truststore import failed:", repr(exc))
    try:
        import requests
        print("requests:", requests.__version__)
    except Exception as exc:
        print("requests import failed:", repr(exc))


def dns_test() -> list[str]:
    section("DNS resolution")
    addresses: list[str] = []
    try:
        infos = socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)
        for info in infos:
            ip = info[4][0]
            if ip not in addresses:
                addresses.append(ip)
                print("resolved:", ip)
    except Exception:
        traceback.print_exc()
    return addresses


def tcp_test(addresses: list[str]) -> None:
    section("TCP connect test")
    for ip in addresses:
        try:
            with socket.create_connection((ip, 443), timeout=15):
                print("TCP OK:", ip)
        except Exception as exc:
            print("TCP FAILED:", ip, type(exc).__name__, exc)


def tls_handshake_test(addresses: list[str]) -> None:
    section("Direct Python ssl handshake test")
    contexts = []
    default_ctx = ssl.create_default_context()
    contexts.append(("default context", default_ctx))
    tls12_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls12_ctx.check_hostname = True
    tls12_ctx.verify_mode = ssl.CERT_REQUIRED
    tls12_ctx.load_default_certs()
    try:
        tls12_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        tls12_ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    except Exception:
        pass
    contexts.append(("forced TLS 1.2", tls12_ctx))
    no_verify_ctx = ssl._create_unverified_context()
    contexts.append(("NO VERIFY diagnostic only", no_verify_ctx))
    for label, ctx in contexts:
        print("\n--", label)
        for ip in addresses:
            try:
                raw = socket.create_connection((ip, 443), timeout=15)
                with raw:
                    with ctx.wrap_socket(raw, server_hostname=HOST) as sock:
                        print("TLS OK:", ip, "version=", sock.version(), "cipher=", sock.cipher())
                        try:
                            cert = sock.getpeercert()
                            print("cert subject:", cert.get("subject"))
                            print("cert issuer:", cert.get("issuer"))
                        except Exception as cert_exc:
                            print("cert read failed:", cert_exc)
            except Exception as exc:
                print("TLS FAILED:", ip, type(exc).__name__, exc)


def requests_test(label: str, verify, trust_env: bool = True) -> None:
    section(f"requests test: {label}")
    try:
        import requests
        session = requests.Session()
        session.trust_env = trust_env
        r = session.get(URL, params={"f": "pjson"}, headers=HEADERS, timeout=30, verify=verify)
        print("status:", r.status_code)
        print("content-type:", r.headers.get("content-type"))
        print("bytes:", len(r.content))
        print("preview:", r.text[:500].replace("\r", " ").replace("\n", " "))
        r.raise_for_status()
        data = r.json()
        print("json name:", data.get("name"))
    except Exception:
        traceback.print_exc()


def urllib_test(label: str, context: ssl.SSLContext | None = None) -> None:
    section(f"urllib test: {label}")
    try:
        final = URL + "?" + urllib.parse.urlencode({"f": "pjson"})
        req = urllib.request.Request(final, headers=HEADERS)
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context)) if context else urllib.request.build_opener()
        with opener.open(req, timeout=30) as resp:
            body = resp.read()
            text = body.decode("utf-8", errors="replace")
            print("status:", resp.status)
            print("content-type:", resp.headers.get("content-type"))
            print("bytes:", len(body))
            print("preview:", text[:500].replace("\r", " ").replace("\n", " "))
            data = json.loads(text)
            print("json name:", data.get("name"))
    except Exception:
        traceback.print_exc()


def arcgis_extent_test_requests() -> None:
    section("requests ArcGIS extent query")
    try:
        import requests
        params = {
            "f": "pjson",
            "where": "1=1",
            "geometry": BOUNDS,
            "geometryType": "esriGeometryEnvelope",
            "inSR": "26986",
            "spatialRel": "esriSpatialRelIntersects",
            "returnCountOnly": "true",
        }
        r = requests.get(QUERY_URL, params=params, headers=HEADERS, timeout=30)
        print("url:", r.url)
        print("status:", r.status_code)
        print("content-type:", r.headers.get("content-type"))
        print("bytes:", len(r.content))
        print("preview:", r.text[:500].replace("\r", " ").replace("\n", " "))
        print("json:", r.json())
    except Exception:
        traceback.print_exc()


def main() -> int:
    print_env()
    addresses = dns_test()
    tcp_test(addresses)
    tls_handshake_test(addresses)
    urllib_test("default")
    urllib_test("NO VERIFY diagnostic only", ssl._create_unverified_context())
    requests_test("default verify / trust_env=True", verify=True, trust_env=True)
    requests_test("default verify / trust_env=False", verify=True, trust_env=False)
    requests_test("NO VERIFY diagnostic only / trust_env=True", verify=False, trust_env=True)
    arcgis_extent_test_requests()
    print("\nDONE. If TCP succeeds but every TLS handshake fails in Python, the failure is below HTTP and ArcGIS never sees the request.")
    print("If NO VERIFY also fails, it is not a certificate-validation problem. It is connection/TLS interception/reset.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
