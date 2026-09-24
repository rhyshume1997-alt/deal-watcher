"""Polite HTTP: one client, robots.txt respected, per-host spacing, short timeouts."""
from __future__ import annotations

import logging
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (compatible; personal-deal-watcher/1.0; low-frequency, single user)"
MIN_GAP_S = 3.0

_client: httpx.Client | None = None
_robots: dict[str, RobotFileParser | None] = {}
_last_hit: dict[str, float] = {}


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(headers={"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"},
                               timeout=20, follow_redirects=True)
    return _client


def allowed(url: str) -> bool:
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = RobotFileParser()
        try:
            r = client().get(base + "/robots.txt")
            if r.status_code >= 400:
                _robots[base] = None  # no robots.txt → allowed
            else:
                rp.parse(r.text.splitlines())
                _robots[base] = rp
        except httpx.HTTPError:
            _robots[base] = None
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(UA, url)


def get(url: str, check_robots: bool = True) -> httpx.Response:
    host = urlparse(url).netloc
    if check_robots and not allowed(url):
        raise PermissionError(f"robots.txt disallows {url}")
    wait = MIN_GAP_S - (time.monotonic() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.monotonic()
    r = client().get(url)
    r.raise_for_status()
    return r


def get_first(urls: list[str], check_robots: bool = True) -> tuple[str, httpx.Response]:
    last: Exception | None = None
    for u in urls:
        try:
            return u, get(u, check_robots)
        except (httpx.HTTPError, PermissionError) as e:
            last = e
            log.info("source url failed %s: %s", u, e)
    raise last or RuntimeError("no urls")
