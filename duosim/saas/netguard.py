"""Outbound URL policy for user-supplied endpoints (webhooks, custom model endpoints): no SSRF.

A URL is allowed only if it is https (http only for explicitly allowed private development hosts) and every
address its host resolves to is a public unicast address - not loopback, private, link-local (cloud
metadata at 169.254.169.254), multicast, reserved or unspecified.  DUOSIM_ALLOW_PRIVATE_URLS=1 lifts the
address check for self-hosted installs that call e.g. a local Ollama.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit


class URLRejected(ValueError):
    pass


def _private_allowed() -> bool:
    return os.environ.get("DUOSIM_ALLOW_PRIVATE_URLS") == "1"


def is_public_ip(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not (a.is_multicast or a.is_reserved or a.is_loopback or a.is_link_local)


def check_url(url: str, *, resolve=socket.getaddrinfo) -> str:
    if not isinstance(url, str) or len(url) > 2000:
        raise URLRejected("URL must be text of at most 2000 characters")
    u = urlsplit(url.strip())
    private_ok = _private_allowed()
    if u.scheme != "https" and not (private_ok and u.scheme == "http"):
        raise URLRejected("URL must use https")
    if not u.hostname or u.username or u.password:
        raise URLRejected("URL must have a host and no credentials")
    if private_ok:
        return url.strip()
    try:
        infos = resolve(u.hostname, u.port or 443, proto=socket.IPPROTO_TCP)
    except OSError:
        raise URLRejected(f"cannot resolve {u.hostname}") from None
    addrs = {i[4][0] for i in infos}
    if not addrs or not all(is_public_ip(a) for a in addrs):
        raise URLRejected(f"{u.hostname} resolves to a private or reserved address")
    return url.strip()
