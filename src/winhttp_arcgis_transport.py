from __future__ import annotations
"""WinHTTP-backed requests transport for public ArcGIS hosts that fail through Python ssl.

Narrow interception for arcgisserver.digital.mass.gov. Uses Windows WinHTTP with
automatic proxy discovery, which worked in the office/Zscaler path.
"""
import ctypes
from ctypes import wintypes
import os
import urllib.parse
from typing import Any

_TARGET_HOSTS = {"arcgisserver.digital.mass.gov"}
_DISABLE_ENV = "ESTIMATE_GIS_DISABLE_WINHTTP_TRANSPORT"

_winhttp = ctypes.WinDLL("winhttp", use_last_error=True)

HINTERNET = wintypes.HANDLE
DWORD = wintypes.DWORD
WORD = wintypes.WORD
BOOL = wintypes.BOOL
LPCWSTR = wintypes.LPCWSTR
LPVOID = wintypes.LPVOID
LPDWORD = ctypes.POINTER(DWORD)

WINHTTP_ACCESS_TYPE_NAMED_PROXY = 3
WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY = 4
WINHTTP_NO_PROXY_NAME = None
WINHTTP_NO_PROXY_BYPASS = None
WINHTTP_FLAG_SECURE = 0x00800000
WINHTTP_QUERY_STATUS_CODE = 19
WINHTTP_QUERY_FLAG_NUMBER = 0x20000000

_winhttp.WinHttpOpen.argtypes = [LPCWSTR, DWORD, LPCWSTR, LPCWSTR, DWORD]
_winhttp.WinHttpOpen.restype = HINTERNET
_winhttp.WinHttpConnect.argtypes = [HINTERNET, LPCWSTR, WORD, DWORD]
_winhttp.WinHttpConnect.restype = HINTERNET
_winhttp.WinHttpOpenRequest.argtypes = [HINTERNET, LPCWSTR, LPCWSTR, LPCWSTR, LPCWSTR, ctypes.POINTER(LPCWSTR), DWORD]
_winhttp.WinHttpOpenRequest.restype = HINTERNET
_winhttp.WinHttpSendRequest.argtypes = [HINTERNET, LPCWSTR, DWORD, LPVOID, DWORD, DWORD, DWORD]
_winhttp.WinHttpSendRequest.restype = BOOL
_winhttp.WinHttpReceiveResponse.argtypes = [HINTERNET, LPVOID]
_winhttp.WinHttpReceiveResponse.restype = BOOL
_winhttp.WinHttpQueryHeaders.argtypes = [HINTERNET, DWORD, LPCWSTR, LPVOID, LPDWORD, LPDWORD]
_winhttp.WinHttpQueryHeaders.restype = BOOL
_winhttp.WinHttpQueryDataAvailable.argtypes = [HINTERNET, LPDWORD]
_winhttp.WinHttpQueryDataAvailable.restype = BOOL
_winhttp.WinHttpReadData.argtypes = [HINTERNET, LPVOID, DWORD, LPDWORD]
_winhttp.WinHttpReadData.restype = BOOL
_winhttp.WinHttpCloseHandle.argtypes = [HINTERNET]
_winhttp.WinHttpCloseHandle.restype = BOOL
_winhttp.WinHttpSetTimeouts.argtypes = [HINTERNET, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
_winhttp.WinHttpSetTimeouts.restype = BOOL

def _last_error() -> int:
    return ctypes.get_last_error()

def _check_handle(handle, label: str):
    if not handle:
        raise OSError(_last_error(), f"{label} failed")
    return handle

def _check_bool(ok, label: str) -> None:
    if not ok:
        raise OSError(_last_error(), f"{label} failed")

def _target_host(url: str) -> bool:
    if os.environ.get(_DISABLE_ENV, "").strip().lower() in {"1", "true", "yes", "y"}:
        return False
    return urllib.parse.urlparse(str(url)).netloc.lower() in _TARGET_HOSTS

def _append_params(url: str, params: Any) -> str:
    if not params:
        return str(url)
    if isinstance(params, bytes):
        query = params.decode("utf-8", errors="replace")
    elif isinstance(params, str):
        query = params
    else:
        query = urllib.parse.urlencode(params, doseq=True)
    return str(url) + ("&" if "?" in str(url) else "?") + query

def _open_session() -> HINTERNET:
    explicit_proxy = os.environ.get("ESTIMATE_GIS_PROXY_URL", "").strip()
    if explicit_proxy:
        session = _check_handle(
            _winhttp.WinHttpOpen("EstimateGIS-WinHTTP/1.0", WINHTTP_ACCESS_TYPE_NAMED_PROXY, explicit_proxy, WINHTTP_NO_PROXY_BYPASS, 0),
            "WinHttpOpen named proxy",
        )
    else:
        session = _check_handle(
            _winhttp.WinHttpOpen("EstimateGIS-WinHTTP/1.0", WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY, WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0),
            "WinHttpOpen automatic proxy",
        )
    _check_bool(_winhttp.WinHttpSetTimeouts(session, 10000, 10000, 60000, 120000), "WinHttpSetTimeouts session")
    return session

def _read_winhttp(full_url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    parsed = urllib.parse.urlparse(full_url)
    host = parsed.netloc
    path = parsed.path + (("?" + parsed.query) if parsed.query else "")
    session = _open_session()
    try:
        connect = _check_handle(_winhttp.WinHttpConnect(session, host, WORD(443), 0), "WinHttpConnect")
        try:
            request = _check_handle(_winhttp.WinHttpOpenRequest(connect, "GET", path, None, None, None, WINHTTP_FLAG_SECURE), "WinHttpOpenRequest")
            try:
                _check_bool(_winhttp.WinHttpSetTimeouts(request, 10000, 10000, 60000, 120000), "WinHttpSetTimeouts request")
                header_text = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
                _check_bool(_winhttp.WinHttpSendRequest(request, header_text, len(header_text), None, 0, 0, 0), "WinHttpSendRequest")
                _check_bool(_winhttp.WinHttpReceiveResponse(request, None), "WinHttpReceiveResponse")
                status = DWORD(0)
                status_len = DWORD(ctypes.sizeof(status))
                index = DWORD(0)
                _check_bool(_winhttp.WinHttpQueryHeaders(request, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, None, ctypes.byref(status), ctypes.byref(status_len), ctypes.byref(index)), "WinHttpQueryHeaders status")
                data = bytearray()
                while True:
                    available = DWORD(0)
                    _check_bool(_winhttp.WinHttpQueryDataAvailable(request, ctypes.byref(available)), "WinHttpQueryDataAvailable")
                    if available.value == 0:
                        break
                    buf = ctypes.create_string_buffer(available.value)
                    read = DWORD(0)
                    _check_bool(_winhttp.WinHttpReadData(request, buf, available.value, ctypes.byref(read)), "WinHttpReadData")
                    if read.value:
                        data.extend(buf.raw[:read.value])
                return int(status.value), bytes(data)
            finally:
                _winhttp.WinHttpCloseHandle(request)
        finally:
            _winhttp.WinHttpCloseHandle(connect)
    finally:
        _winhttp.WinHttpCloseHandle(session)

def _winhttp_response(requests_module: Any, method: str, url: str, **kwargs: Any):
    if (method or "GET").upper() != "GET":
        raise RuntimeError("WinHTTP transport currently supports GET only")
    full_url = _append_params(url, kwargs.get("params"))
    headers = dict(kwargs.get("headers") or {})
    headers.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    headers.setdefault("Accept", "application/json,text/plain,*/*")
    status, body = _read_winhttp(full_url, headers)
    response = requests_module.Response()
    response.url = full_url
    response.status_code = status
    response._content = body
    response.encoding = "utf-8"
    response.headers["Content-Type"] = "application/json"
    return response

def install_winhttp_transport(requests_module: Any) -> None:
    if getattr(requests_module, "_estimate_gis_winhttp_transport_installed", False):
        return
    original = requests_module.sessions.Session.request
    def patched(self, method, url, **kwargs):
        if _target_host(str(url)):
            return _winhttp_response(requests_module, method, str(url), **kwargs)
        return original(self, method, url, **kwargs)
    requests_module.sessions.Session.request = patched
    requests_module._estimate_gis_winhttp_transport_installed = True
