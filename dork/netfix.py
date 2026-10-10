"""DNS fallback for unreliable routers.

Some Wi-Fi routers fail to resolve certain domains. When the system resolver fails,
ask Cloudflare / Google DNS-over-HTTPS directly (by IP, so no DNS is needed to reach
them). TLS still verifies the real hostname, so this changes only *how the address is
found*, not who Dork talks to. Installed once at startup, for Dork's process only."""

import json
import logging
import socket
import ssl
import threading
import time
import urllib.request

log = logging.getLogger("dork.net")

DOH = ("https://1.1.1.1/dns-query?name={}&type=A", "https://8.8.8.8/resolve?name={}&type=A")
TTL = 600

_system_getaddrinfo = socket.getaddrinfo
_cache: dict[str, tuple[float, list[str]]] = {}
_lock = threading.Lock()
_ctx = ssl.create_default_context()


def _doh(host: str) -> list[str]:
    with _lock:
        hit = _cache.get(host)
        if hit and hit[0] > time.time():
            return hit[1]
    for url in DOH:
        try:
            req = urllib.request.Request(url.format(host), headers={"Accept": "application/dns-json"})
            with urllib.request.urlopen(req, timeout=4, context=_ctx) as r:
                answers = json.load(r).get("Answer", [])
            ips = [a["data"] for a in answers if a.get("type") == 1]
            if ips:
                with _lock:
                    _cache[host] = (time.time() + TTL, ips)
                return ips
        except Exception:  # noqa: BLE001 — try the next resolver
            continue
    return []


def _from_ips(ips, port, type, proto, flags):
    out = []
    for ip in ips:
        out += _system_getaddrinfo(ip, port, socket.AF_INET, type, proto, flags | socket.AI_NUMERICHOST)
    return out


def _getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    # A host the router recently failed on: go straight to the cached DoH answer.
    with _lock:
        hit = _cache.get(host) if isinstance(host, str) else None
    if hit and hit[0] > time.time():
        return _from_ips(hit[1], port, type, proto, flags)
    try:
        return _system_getaddrinfo(host, port, family, type, proto, flags)
    except socket.gaierror:
        if not isinstance(host, str) or host == "localhost" or family == socket.AF_INET6:
            raise
        ips = _doh(host)
        if not ips:
            raise
        log.info("resolved %s via DNS-over-HTTPS (router DNS failed)", host)
        return _from_ips(ips, port, type, proto, flags)


def install() -> None:
    socket.getaddrinfo = _getaddrinfo
