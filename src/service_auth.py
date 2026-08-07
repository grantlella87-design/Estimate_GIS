"""Decide which ArcGIS hosts get our token, and fetch one only when it is needed.

Every ArcGIS query in this repository ran through the same token path, which
meant a public service got asked for a portal sign-in it never wanted, and our
token was attached to requests leaving for hosts that are not ours. Both are
fixed by answering one question from the URL: is this our server?

The rule is deliberately narrow. A host is internal if it is, or sits under,
one of INTERNAL_HOST_SUFFIXES - National Grid by default. Internal hosts get the
token from `auth.py`. Everything else is treated as public and is sent no token
at all, even if the caller has one in hand. That direction matters more than the
other: a public host that turns out to need credentials fails loudly with the
service's own error, while a token sent somewhere it does not belong is a
credential disclosed to a third party and nothing says so.

Add hosts with the environment variable rather than by editing this file:

    ESTIMATE_GIS_INTERNAL_HOSTS=nationalgrid.com,gis.internal.example
"""
from __future__ import annotations

import importlib
import os
import threading
from urllib.parse import urlparse

DEFAULT_INTERNAL_HOST_SUFFIXES = ("nationalgrid.com",)
INTERNAL_HOSTS_ENV = "ESTIMATE_GIS_INTERNAL_HOSTS"

_token_lock = threading.Lock()
_cached_token: str | None = None
_reported_hosts: set[str] = set()


def internal_host_suffixes() -> tuple[str, ...]:
    """Host suffixes whose services get our token."""
    configured = os.environ.get(INTERNAL_HOSTS_ENV, "").strip()
    if not configured:
        return DEFAULT_INTERNAL_HOST_SUFFIXES
    suffixes = tuple(
        part.strip().lower().lstrip(".")
        for part in configured.split(",")
        if part.strip()
    )
    return suffixes or DEFAULT_INTERNAL_HOST_SUFFIXES


def host_of(url: str) -> str:
    """Hostname of a URL, lowercased and without a port. Empty for a file path."""
    if not url:
        return ""
    parsed = urlparse(str(url))
    if not parsed.scheme.startswith("http"):
        return ""
    return (parsed.hostname or "").lower()


def is_internal_service(url: str) -> bool:
    """True when the URL points at one of our own ArcGIS servers.

    Suffixes are matched on label boundaries, not as substrings: a host called
    ``notnationalgrid.com`` must not inherit the trust of ``nationalgrid.com``.
    """
    host = host_of(url)
    if not host:
        return False
    return any(
        host == suffix or host.endswith("." + suffix)
        for suffix in internal_host_suffixes()
    )


def requires_token(url: str) -> bool:
    """Whether a request to this URL should carry an ArcGIS token."""
    return is_internal_service(url)


def cached_token(refresh: bool = False) -> str:
    """The ArcGIS token from `auth.py`, fetched once per process.

    `auth` is imported here rather than at module scope so a run that touches
    only public services never loads keyring or the portal configuration - which
    is what lets the MassGIS half of an analysis work with no VPN and no sign-in.
    """
    global _cached_token
    with _token_lock:
        if _cached_token and not refresh:
            return _cached_token
        auth = importlib.import_module("auth")
        _cached_token = auth.get_token()
        return _cached_token


def clear_cached_token() -> None:
    global _cached_token
    with _token_lock:
        _cached_token = None


def token_for(
    url: str, explicit: str | None = None, *, allow_sign_in: bool = True
) -> str | None:
    """The token to send with a request to `url`, or None to send none.

    An explicit token is honoured for an internal host and dropped for a public
    one, so passing a token around cannot leak it off-site by accident.
    """
    if not requires_token(url):
        return None
    if explicit:
        return explicit
    if not allow_sign_in:
        return None
    return cached_token()


def describe(url: str) -> str:
    """One line for the log saying how this URL will be authenticated."""
    host = host_of(url) or "local file"
    if is_internal_service(url):
        return f"{host}: internal service, signing in with src/auth.py"
    return f"{host}: public service, no token sent"


def report_once(url: str) -> None:
    """Say how a host is being treated, the first time it is used in a run."""
    from arcgis_rest_geopandas import progress

    host = host_of(url)
    if not host or host in _reported_hosts:
        return
    _reported_hosts.add(host)
    progress(describe(url))


def token_hint(url: str) -> str:
    """How this host is treated and what to do about it.

    Phrased as the current policy rather than as something that already
    happened, so it reads correctly both when a token was never obtained and
    when one was sent and rejected.
    """
    host = host_of(url)
    if is_internal_service(url):
        return (
            f"{host} is an internal service, so it needs a token. "
            "Sign in and check it works: python src/auth.py --force"
        )
    return (
        f"{host} is not in {INTERNAL_HOSTS_ENV} "
        f"(currently {', '.join(internal_host_suffixes())}), so no token is sent. "
        f"If this host is ours, add it: {INTERNAL_HOSTS_ENV}={host}"
    )
