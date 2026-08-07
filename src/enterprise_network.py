from __future__ import annotations

import os

PROXY_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
)
NO_PROXY_DEFAULT = "localhost,127.0.0.1,::1"


def _internal_no_proxy_hosts() -> str:
    """Our own hosts, which reach us directly and must not be sent to a proxy.

    Interception for these is transparent, so they work today with no proxy set.
    Once one is set for the public services, they have to be excluded explicitly
    or a fix for the half that fails breaks the half that works.
    """
    try:
        import service_auth

        suffixes = service_auth.internal_host_suffixes()
    except ImportError:
        suffixes = ("nationalgrid.com",)
    entries = []
    for suffix in suffixes:
        entries.extend([suffix, f".{suffix}"])
    return ",".join(entries)
ZSCALER_PROXY_ENV = "ESTIMATE_GIS_PROXY_URL"

def configure_enterprise_ssl() -> None:
    """Use the Windows trust store for enterprise TLS interception certificates."""
    try:
        import truststore
    except ImportError:
        return
    try:
        truststore.inject_into_ssl()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"WARNING: Could not enable Windows trust store: {exc}")

def detect_system_proxy() -> str:
    """The proxy Windows itself is configured to use, if any.

    requests only reads proxies from environment variables. Windows keeps its
    proxy in the registry, where Internet Explorer/Edge settings live and where
    Zscaler and most corporate agents write theirs, and Python exposes it through
    urllib. Without this bridge, internal hosts reachable directly keep working
    while everything that needs the proxy is refused at the network - which
    arrives as ConnectionResetError 10054 on the first request, never as
    anything mentioning a proxy.
    """
    try:
        from urllib.request import getproxies

        proxies = getproxies()
    except (ImportError, OSError):
        return ""
    return (proxies.get("https") or proxies.get("http") or "").strip()


def configure_zscaler_proxy() -> None:
    """Centralize proxy environment setup for National Grid/Zscaler network use.

    Zscaler at National Grid normally performs transparent TLS interception, so this function
    does not invent a proxy host. If an explicit proxy is needed, set ESTIMATE_GIS_PROXY_URL
    before running, and this function will map it to HTTP_PROXY and HTTPS_PROXY.
    """
    explicit_proxy = os.environ.get(ZSCALER_PROXY_ENV, "").strip()
    if explicit_proxy:
        os.environ["HTTP_PROXY"] = explicit_proxy
        os.environ["HTTPS_PROXY"] = explicit_proxy
        os.environ["http_proxy"] = explicit_proxy
        os.environ["https_proxy"] = explicit_proxy
        print(f"Configured proxy variables from {ZSCALER_PROXY_ENV}.")
    else:
        inherited_proxy = next((os.environ.get(name, "").strip() for name in PROXY_ENV_NAMES if os.environ.get(name, "").strip()), "")
        if inherited_proxy:
            os.environ.setdefault("HTTP_PROXY", inherited_proxy)
            os.environ.setdefault("HTTPS_PROXY", inherited_proxy)
            os.environ.setdefault("http_proxy", inherited_proxy)
            os.environ.setdefault("https_proxy", inherited_proxy)
            print("Using existing proxy environment variables.")
        else:
            # Nothing in the environment, so ask Windows. Interception is
            # transparent for internal hosts, which is why this went unnoticed:
            # gis.nationalgrid.com works without a proxy and the public services
            # do not, so only the public half fails and it fails as a reset.
            system_proxy = detect_system_proxy()
            if system_proxy:
                os.environ["HTTP_PROXY"] = system_proxy
                os.environ["HTTPS_PROXY"] = system_proxy
                os.environ["http_proxy"] = system_proxy
                os.environ["https_proxy"] = system_proxy
                print(f"Using the proxy Windows is configured with: {system_proxy}")
            else:
                print(
                    "No proxy configured. If public services are refused with "
                    "ConnectionResetError 10054, set ESTIMATE_GIS_PROXY_URL."
                )
    bypass = f"{NO_PROXY_DEFAULT},{_internal_no_proxy_hosts()}"
    existing_no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
    if existing_no_proxy:
        merged = existing_no_proxy
        for value in bypass.split(","):
            if value and value not in merged.split(","):
                merged = f"{merged},{value}"
    else:
        merged = bypass
    os.environ["NO_PROXY"] = merged
    os.environ["no_proxy"] = merged

def configure_enterprise_network() -> None:
    """Apply enterprise TLS and proxy configuration in one place."""
    configure_enterprise_ssl()
    configure_zscaler_proxy()
