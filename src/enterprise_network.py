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


def _set_proxy(url: str) -> None:
    """Set every spelling requests looks at, so nothing depends on which it reads."""
    for name in PROXY_ENV_NAMES:
        os.environ[name] = url


def configure_zscaler_proxy() -> None:
    """Turn the proxy on for requests, and keep our own hosts off it.

    requests reads proxies from environment variables and nowhere else. Zscaler
    intercepts internal traffic transparently, so gis.nationalgrid.com works with
    no proxy set at all, and only public services fail - as a connection reset on
    the first packet, which never mentions a proxy. Setting the variables here is
    the whole fix.

    Three sources, in order of how deliberate they are:

    1. ESTIMATE_GIS_PROXY_URL, when someone has named the proxy explicitly;
    2. proxy variables already in the environment, left alone;
    3. what Windows itself is configured with, read out of the registry.

    Our own hosts go into NO_PROXY either way. They are reached directly today,
    and a proxy is not guaranteed to have a route to them, so turning the proxy
    on for the half that fails must not break the half that works.
    """
    explicit_proxy = os.environ.get(ZSCALER_PROXY_ENV, "").strip()
    inherited_proxy = next(
        (os.environ.get(name, "").strip() for name in PROXY_ENV_NAMES if os.environ.get(name, "").strip()),
        "",
    )
    if explicit_proxy:
        _set_proxy(explicit_proxy)
        print(f"Proxy set from {ZSCALER_PROXY_ENV}: {explicit_proxy}")
    elif inherited_proxy:
        _set_proxy(inherited_proxy)
        print(f"Proxy already in the environment: {inherited_proxy}")
    else:
        system_proxy = detect_system_proxy()
        if system_proxy:
            _set_proxy(system_proxy)
            print(f"Proxy read from the Windows configuration: {system_proxy}")
        else:
            print(
                "No proxy set, so public services will be reached directly. If they "
                "are refused with ConnectionResetError 10054, that is the network "
                f"requiring a proxy: set {ZSCALER_PROXY_ENV} to it and run again."
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

def describe_proxy() -> str:
    """What requests will actually do, in one line, for the startup log."""
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if not proxy:
        return "No proxy set. Public services will be reached directly."
    bypass = os.environ.get("NO_PROXY", "")
    return f"Proxy for public hosts: {proxy} (bypassed for {bypass})"


def configure_enterprise_network() -> None:
    """Apply enterprise TLS and proxy configuration in one place."""
    configure_enterprise_ssl()
    configure_zscaler_proxy()
