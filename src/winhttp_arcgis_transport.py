"""Route public ArcGIS requests through WinHTTP so they follow the Zscaler proxy.

Python's `requests` reads proxy settings from environment variables only. Zscaler
publishes its proxy through the Windows configuration - autodetect, a PAC URL, or
a named proxy - which `requests` never sees. Internal National Grid hosts are
reached directly and are unaffected, so the failure looks selective and
mysterious: `gis.nationalgrid.com` works, every public service is refused with
ConnectionResetError 10054 on the very first packet.

WinHTTP is the Windows HTTP stack. It resolves the proxy the same way the rest of
the machine does and validates TLS against the Windows certificate store, which
is where the Zscaler root already lives. Sending public ArcGIS traffic through it
makes those requests behave like every other program on the workstation.

Scope, deliberately narrow:

* only when a proxy is actually configured - with no proxy in play, normal
  `requests` is the better path and is left alone;
* only on Windows;
* only for hosts that are not ours. Internal traffic keeps using `requests`,
  since it works today and reaches hosts a proxy may not be able to.

Set ESTIMATE_GIS_DISABLE_WINHTTP_TRANSPORT=1 to turn it off, or
ESTIMATE_GIS_FORCE_WINHTTP_TRANSPORT=1 to use it even without a detected proxy.
"""
from __future__ import annotations

import ctypes
import os
import sys
import urllib.parse
from typing import Any

DISABLE_ENV = "ESTIMATE_GIS_DISABLE_WINHTTP_TRANSPORT"
FORCE_ENV = "ESTIMATE_GIS_FORCE_WINHTTP_TRANSPORT"
PROXY_ENV = "ESTIMATE_GIS_PROXY_URL"

IS_WINDOWS = sys.platform == "win32"

# Loading winhttp on anything but Windows raises, so the whole module has to be
# importable without it. Callers check `available()`.
_winhttp: Any = None
if IS_WINDOWS:
    try:
        _winhttp = ctypes.WinDLL("winhttp", use_last_error=True)
    except (OSError, AttributeError):
        _winhttp = None

if _winhttp is not None:
    from ctypes import wintypes

    HINTERNET = wintypes.HANDLE
    DWORD = wintypes.DWORD
    WORD = wintypes.WORD
    BOOL = wintypes.BOOL
    LPCWSTR = wintypes.LPCWSTR
    LPWSTR = wintypes.LPWSTR
    LPVOID = wintypes.LPVOID
    LPDWORD = ctypes.POINTER(DWORD)

    WINHTTP_ACCESS_TYPE_NAMED_PROXY = 3
    WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY = 4
    WINHTTP_FLAG_SECURE = 0x00800000
    WINHTTP_QUERY_STATUS_CODE = 19
    WINHTTP_QUERY_RAW_HEADERS_CRLF = 22
    WINHTTP_QUERY_FLAG_NUMBER = 0x20000000
    ERROR_INSUFFICIENT_BUFFER = 122

    class WINHTTP_CURRENT_USER_IE_PROXY_CONFIG(ctypes.Structure):
        _fields_ = [
            ("fAutoDetect", BOOL),
            ("lpszAutoConfigUrl", LPWSTR),
            ("lpszProxy", LPWSTR),
            ("lpszProxyBypass", LPWSTR),
        ]

    _winhttp.WinHttpOpen.argtypes = [LPCWSTR, DWORD, LPCWSTR, LPCWSTR, DWORD]
    _winhttp.WinHttpOpen.restype = HINTERNET
    _winhttp.WinHttpConnect.argtypes = [HINTERNET, LPCWSTR, WORD, DWORD]
    _winhttp.WinHttpConnect.restype = HINTERNET
    _winhttp.WinHttpOpenRequest.argtypes = [
        HINTERNET, LPCWSTR, LPCWSTR, LPCWSTR, LPCWSTR, ctypes.POINTER(LPCWSTR), DWORD
    ]
    _winhttp.WinHttpOpenRequest.restype = HINTERNET
    _winhttp.WinHttpSendRequest.argtypes = [
        HINTERNET, LPCWSTR, DWORD, LPVOID, DWORD, DWORD, DWORD
    ]
    _winhttp.WinHttpSendRequest.restype = BOOL
    _winhttp.WinHttpReceiveResponse.argtypes = [HINTERNET, LPVOID]
    _winhttp.WinHttpReceiveResponse.restype = BOOL
    _winhttp.WinHttpQueryHeaders.argtypes = [
        HINTERNET, DWORD, LPCWSTR, LPVOID, LPDWORD, LPDWORD
    ]
    _winhttp.WinHttpQueryHeaders.restype = BOOL
    _winhttp.WinHttpQueryDataAvailable.argtypes = [HINTERNET, LPDWORD]
    _winhttp.WinHttpQueryDataAvailable.restype = BOOL
    _winhttp.WinHttpReadData.argtypes = [HINTERNET, LPVOID, DWORD, LPDWORD]
    _winhttp.WinHttpReadData.restype = BOOL
    _winhttp.WinHttpCloseHandle.argtypes = [HINTERNET]
    _winhttp.WinHttpCloseHandle.restype = BOOL
    _winhttp.WinHttpSetTimeouts.argtypes = [
        HINTERNET, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int
    ]
    _winhttp.WinHttpSetTimeouts.restype = BOOL
    _winhttp.WinHttpGetIEProxyConfigForCurrentUser.argtypes = [
        ctypes.POINTER(WINHTTP_CURRENT_USER_IE_PROXY_CONFIG)
    ]
    _winhttp.WinHttpGetIEProxyConfigForCurrentUser.restype = BOOL


def available() -> bool:
    """Whether this transport can run at all."""
    return _winhttp is not None


def describe_proxy() -> str:
    """How Windows is configured to reach the internet, for the log."""
    explicit = os.environ.get(PROXY_ENV, "").strip()
    if explicit:
        return f"named proxy from {PROXY_ENV}: {explicit}"
    if not available():
        return "not available (WinHTTP loads on Windows only)"
    config = WINHTTP_CURRENT_USER_IE_PROXY_CONFIG()
    if not _winhttp.WinHttpGetIEProxyConfigForCurrentUser(ctypes.byref(config)):
        return "no Windows proxy configuration readable"
    parts = []
    if config.fAutoDetect:
        parts.append("autodetect (WPAD)")
    if config.lpszAutoConfigUrl:
        parts.append(f"PAC {config.lpszAutoConfigUrl}")
    if config.lpszProxy:
        parts.append(f"proxy {config.lpszProxy}")
    return ", ".join(parts) if parts else "direct, no proxy configured"


def proxy_is_active() -> bool:
    """True when this machine reaches the internet through a proxy.

    Any of the three Windows mechanisms counts, because Zscaler uses all of them
    depending on how a site is set up: WPAD autodetect, a PAC URL, or a fixed
    proxy. An explicit ESTIMATE_GIS_PROXY_URL counts too.
    """
    if os.environ.get(PROXY_ENV, "").strip():
        return True
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        if os.environ.get(name, "").strip():
            return True
    if not available():
        return False
    config = WINHTTP_CURRENT_USER_IE_PROXY_CONFIG()
    if not _winhttp.WinHttpGetIEProxyConfigForCurrentUser(ctypes.byref(config)):
        return False
    return bool(config.fAutoDetect or config.lpszAutoConfigUrl or config.lpszProxy)


def should_intercept(url: str) -> bool:
    """Whether this URL should go through WinHTTP instead of requests.

    Our own hosts are excluded: they work today over direct connections, and a
    proxy is not guaranteed to have a route to an internal address.
    """
    if os.environ.get(DISABLE_ENV, "").strip().lower() in {"1", "true", "yes", "y"}:
        return False
    if not available():
        return False
    parsed = urllib.parse.urlparse(str(url))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        import service_auth

        if service_auth.is_internal_service(str(url)):
            return False
    except ImportError:
        pass
    return True


def _check_handle(handle: Any, label: str) -> Any:
    if not handle:
        raise OSError(ctypes.get_last_error(), f"{label} failed")
    return handle


def _check_bool(ok: Any, label: str) -> None:
    if not ok:
        raise OSError(ctypes.get_last_error(), f"{label} failed")


def _open_session() -> Any:
    explicit_proxy = os.environ.get(PROXY_ENV, "").strip()
    if explicit_proxy:
        # Strip the scheme: WinHTTP wants host:port, not a URL.
        named = explicit_proxy.split("://", 1)[-1].rstrip("/")
        session = _check_handle(
            _winhttp.WinHttpOpen(
                "EstimateGIS-WinHTTP/1.0", WINHTTP_ACCESS_TYPE_NAMED_PROXY, named, None, 0
            ),
            "WinHttpOpen named proxy",
        )
    else:
        session = _check_handle(
            _winhttp.WinHttpOpen(
                "EstimateGIS-WinHTTP/1.0", WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY, None, None, 0
            ),
            "WinHttpOpen automatic proxy",
        )
    _check_bool(
        _winhttp.WinHttpSetTimeouts(session, 10000, 10000, 60000, 120000),
        "WinHttpSetTimeouts session",
    )
    return session


def _query_headers(request: Any) -> dict[str, str]:
    """Real response headers, so callers see the true content type."""
    size = DWORD(0)
    index = DWORD(0)
    _winhttp.WinHttpQueryHeaders(
        request, WINHTTP_QUERY_RAW_HEADERS_CRLF, None, None,
        ctypes.byref(size), ctypes.byref(index),
    )
    if ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER or size.value == 0:
        return {}
    buffer = ctypes.create_unicode_buffer(size.value // ctypes.sizeof(ctypes.c_wchar) + 1)
    index = DWORD(0)
    if not _winhttp.WinHttpQueryHeaders(
        request, WINHTTP_QUERY_RAW_HEADERS_CRLF, None, buffer,
        ctypes.byref(size), ctypes.byref(index),
    ):
        return {}
    headers: dict[str, str] = {}
    for line in buffer.value.split("\r\n"):
        if ":" in line:
            name, _, value = line.partition(":")
            headers[name.strip()] = value.strip()
    return headers


def _send(
    method: str, full_url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, bytes, dict[str, str]]:
    parsed = urllib.parse.urlparse(full_url)
    secure = parsed.scheme == "https"
    port = parsed.port or (443 if secure else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    session = _open_session()
    try:
        connect = _check_handle(
            _winhttp.WinHttpConnect(session, parsed.hostname, WORD(port), 0),
            "WinHttpConnect",
        )
        try:
            request = _check_handle(
                _winhttp.WinHttpOpenRequest(
                    connect, method.upper(), path, None, None, None,
                    WINHTTP_FLAG_SECURE if secure else 0,
                ),
                "WinHttpOpenRequest",
            )
            try:
                _check_bool(
                    _winhttp.WinHttpSetTimeouts(request, 10000, 10000, 60000, 120000),
                    "WinHttpSetTimeouts request",
                )
                header_text = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                payload = body or b""
                _check_bool(
                    _winhttp.WinHttpSendRequest(
                        request,
                        header_text or None,
                        len(header_text),
                        ctypes.c_char_p(payload) if payload else None,
                        len(payload),
                        len(payload),
                        0,
                    ),
                    "WinHttpSendRequest",
                )
                _check_bool(
                    _winhttp.WinHttpReceiveResponse(request, None), "WinHttpReceiveResponse"
                )
                status = DWORD(0)
                status_len = DWORD(ctypes.sizeof(status))
                index = DWORD(0)
                _check_bool(
                    _winhttp.WinHttpQueryHeaders(
                        request,
                        WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                        None, ctypes.byref(status),
                        ctypes.byref(status_len), ctypes.byref(index),
                    ),
                    "WinHttpQueryHeaders status",
                )
                data = bytearray()
                while True:
                    available_bytes = DWORD(0)
                    _check_bool(
                        _winhttp.WinHttpQueryDataAvailable(
                            request, ctypes.byref(available_bytes)
                        ),
                        "WinHttpQueryDataAvailable",
                    )
                    if available_bytes.value == 0:
                        break
                    chunk = ctypes.create_string_buffer(available_bytes.value)
                    read = DWORD(0)
                    _check_bool(
                        _winhttp.WinHttpReadData(
                            request, chunk, available_bytes.value, ctypes.byref(read)
                        ),
                        "WinHttpReadData",
                    )
                    if not read.value:
                        break
                    data.extend(chunk.raw[: read.value])
                return int(status.value), bytes(data), _query_headers(request)
            finally:
                _winhttp.WinHttpCloseHandle(request)
        finally:
            _winhttp.WinHttpCloseHandle(connect)
    finally:
        _winhttp.WinHttpCloseHandle(session)


def encode_query(url: str, params: Any) -> str:
    """Fold requests-style `params` into the URL, the way requests would."""
    if not params:
        return str(url)
    if isinstance(params, bytes):
        query = params.decode("utf-8", errors="replace")
    elif isinstance(params, str):
        query = params
    else:
        query = urllib.parse.urlencode(params, doseq=True)
    if not query:
        return str(url)
    return f"{url}{'&' if '?' in str(url) else '?'}{query}"


def encode_body(kwargs: dict[str, Any]) -> tuple[bytes | None, str | None]:
    """Turn requests-style `data`/`json` into bytes and a content type.

    POST matters here: object-id lookups and every feature batch are POSTed, so a
    GET-only transport gets through the layer metadata and then fails on the real
    work - which reads as the proxy fix not having helped at all.
    """
    if kwargs.get("json") is not None:
        import json as json_module

        return json_module.dumps(kwargs["json"]).encode("utf-8"), "application/json"
    data = kwargs.get("data")
    if data is None:
        return None, None
    if isinstance(data, bytes):
        return data, "application/x-www-form-urlencoded"
    if isinstance(data, str):
        return data.encode("utf-8"), "application/x-www-form-urlencoded"
    return (
        urllib.parse.urlencode(data, doseq=True).encode("utf-8"),
        "application/x-www-form-urlencoded",
    )


def _response(requests_module: Any, method: str, url: str, kwargs: dict[str, Any]) -> Any:
    full_url = encode_query(url, kwargs.get("params"))
    body, content_type = encode_body(kwargs)

    headers = {str(k): str(v) for k, v in (kwargs.get("headers") or {}).items()}
    headers.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    headers.setdefault("Accept", "application/json,text/plain,*/*")
    if content_type:
        headers.setdefault("Content-Type", content_type)

    status, content, response_headers = _send(method, full_url, headers, body)

    response = requests_module.Response()
    response.url = full_url
    response.status_code = status
    response._content = content
    response.encoding = "utf-8"
    for name, value in response_headers.items():
        response.headers[name] = value
    response.headers.setdefault("Content-Type", "application/json")
    return response


def install_winhttp_transport(requests_module: Any, force: bool = False) -> bool:
    """Send public ArcGIS traffic through WinHTTP. Returns whether it was installed.

    Idempotent, and a no-op when there is nothing to gain: no WinHTTP, or no
    proxy configured, in which case plain `requests` already works and is the
    better path.
    """
    if getattr(requests_module, "_estimate_gis_winhttp_transport_installed", False):
        return True
    if not available():
        return False
    if os.environ.get(DISABLE_ENV, "").strip().lower() in {"1", "true", "yes", "y"}:
        return False
    forced = force or os.environ.get(FORCE_ENV, "").strip().lower() in {"1", "true", "yes", "y"}
    if not forced and not proxy_is_active():
        return False

    original = requests_module.sessions.Session.request

    def patched(self, method, url, **kwargs):
        if should_intercept(str(url)):
            try:
                return _response(requests_module, method, str(url), kwargs)
            except OSError as exc:
                # Fall back rather than fail: a WinHTTP problem should not be
                # worse than not having tried it.
                raise requests_module.exceptions.ConnectionError(
                    f"WinHTTP request to {url} failed: {exc}"
                ) from exc
        return original(self, method, url, **kwargs)

    requests_module.sessions.Session.request = patched
    requests_module._estimate_gis_winhttp_transport_installed = True
    return True
