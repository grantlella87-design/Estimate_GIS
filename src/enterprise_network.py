from __future__ import annotations

import os

PROXY_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
)
NO_PROXY_DEFAULT = "localhost,127.0.0.1,::1"
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
    existing_no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
    if existing_no_proxy:
        merged = existing_no_proxy
        for value in NO_PROXY_DEFAULT.split(","):
            if value not in merged:
                merged = f"{merged},{value}"
    else:
        merged = NO_PROXY_DEFAULT
    os.environ["NO_PROXY"] = merged
    os.environ["no_proxy"] = merged

def configure_enterprise_network() -> None:
    """Apply enterprise TLS and proxy configuration in one place."""
    configure_enterprise_ssl()
    configure_zscaler_proxy()
