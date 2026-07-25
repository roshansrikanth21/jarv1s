"""Browse / navigation SSRF policy — http(s) only, no private/loopback/metadata."""
from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from typing import Iterable, Optional

BLOCKED_SCHEMES = frozenset({
    "file", "about", "data", "javascript", "chrome", "chrome-extension",
    "view-source", "ftp", "blob", "ws", "wss",
})

_METADATA_HOSTS = frozenset({
    "metadata.google.internal", "metadata",
    "kubernetes.default", "kubernetes.default.svc",
})


def ip_unsafe(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for addresses that must never be reachable via browse (SSRF / metadata)."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(
        ip.is_loopback or ip.is_private or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def check_url(url: str, *, allowlist: Optional[Iterable[str]] = None) -> str | None:
    """Return None if ``url`` may be navigated; else a denial reason."""
    u = (url or "").strip()
    if not u:
        return "navigate needs a 'url'."
    try:
        parsed = urllib.parse.urlparse(u)
    except Exception:
        return f"Could not parse URL: {url!r}"
    scheme = (parsed.scheme or "").lower()
    if scheme in BLOCKED_SCHEMES:
        return f"Blocked URL scheme '{scheme}:' for safety."
    if scheme not in ("http", "https"):
        return "Only http:// and https:// URLs can be browsed."
    host = (parsed.hostname or "").lower()
    if not host:
        return "URL has no host."
    if host == "localhost" or host.endswith(".localhost") or host == "localhost.localdomain":
        return "Blocked navigation to localhost."
    if host in _METADATA_HOSTS:
        return "Blocked navigation to a metadata endpoint."
    try:
        ip = ipaddress.ip_address(host)
        if ip_unsafe(ip):
            return "Blocked navigation to a private/loopback address."
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            return f"Could not resolve '{host}' for browse safety check."
        for info in infos:
            addr = info[4][0]
            try:
                resolved = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if ip_unsafe(resolved):
                return ("Blocked navigation to a host that resolves to a "
                        "private/loopback address.")
    allowed = {a.strip().lower() for a in (allowlist or []) if a and str(a).strip()}
    if allowed and not any(host == a or host.endswith("." + a) for a in allowed):
        return f"'{host}' is not in the browse allowlist (JARVIS_BROWSE_ALLOWLIST)."
    return None
