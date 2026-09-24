"""Signed links for 'View offer', 'Useful' and 'Not useful' so clicks can be counted safely."""
from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlencode

from .config import settings


def sign(alert_id: int, event: str) -> str:
    msg = f"{alert_id}:{event}".encode()
    return hmac.new(settings().link_secret.encode(), msg, hashlib.sha256).hexdigest()[:24]


def verify(alert_id: int, event: str, sig: str) -> bool:
    return hmac.compare_digest(sign(alert_id, event), sig or "")


def link(alert_id: int, event: str, fallback_url: str = "") -> str:
    base = settings().track_base_url
    if not base or not alert_id:
        return fallback_url or "#"
    return f"{base}?{urlencode({'a': alert_id, 'e': event, 's': sign(alert_id, event)})}"
