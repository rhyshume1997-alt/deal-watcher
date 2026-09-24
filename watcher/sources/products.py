"""Price checks for specific products (planned purchases and things you've bought).

Amazon → Keepa API (with its own long price history).
Anything else → the page's schema.org Product data, which most retailers publish for Google.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median

from bs4 import BeautifulSoup
from sqlalchemy import insert, select, update

from .. import db
from ..config import settings
from . import http

log = logging.getLogger(__name__)

ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})")


@dataclass
class PriceCheck:
    key: str
    title: str | None
    price: float | None
    in_stock: bool | None
    keepa_stats: dict | None = None  # {"avg90":…, "avg180":…, "min180":…} when from Keepa


def product_key(url: str) -> str:
    m = ASIN_RE.search(url or "")
    if m and "amazon." in url:
        return f"amazon:{m.group(1)}"
    clean = re.sub(r"[?#].*$", "", url or "")
    return "url:" + hashlib.sha1(clean.encode()).hexdigest()[:16]


def _keepa(asin: str) -> PriceCheck:
    key = settings().keepa_api_key
    if not key:
        raise RuntimeError("KEEPA_API_KEY not set")
    r = http.client().get("https://api.keepa.com/product",
                          params={"key": key, "domain": 2, "asin": asin, "stats": 180, "history": 0})
    r.raise_for_status()
    p = (r.json().get("products") or [None])[0]
    if not p:
        return PriceCheck(f"amazon:{asin}", None, None, None)
    stats = p.get("stats") or {}

    def pick(arr, i):
        try:
            v = arr[i]
            v = v[1] if isinstance(v, list) else v
            return v / 100 if v and v > 0 else None
        except (IndexError, TypeError):
            return None

    cur = stats.get("current") or []
    # 0 = sold by Amazon, 1 = lowest new from any seller
    price = pick(cur, 0) or pick(cur, 1)
    ks = {
        "avg90": pick(stats.get("avg90") or [], 0) or pick(stats.get("avg90") or [], 1),
        "avg180": pick(stats.get("avg") or [], 0) or pick(stats.get("avg") or [], 1),
        "min180": pick(stats.get("min") or [], 0) or pick(stats.get("min") or [], 1),
    }
    return PriceCheck(f"amazon:{asin}", p.get("title"), price, price is not None, ks)


def _walk_ld(obj):
    if isinstance(obj, list):
        for x in obj:
            yield from _walk_ld(x)
    elif isinstance(obj, dict):
        yield obj
        for k in ("@graph", "mainEntity", "itemListElement"):
            if k in obj:
                yield from _walk_ld(obj[k])


def parse_product_html(html: str) -> tuple[str | None, float | None, bool | None]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _walk_ld(data):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if "Product" not in types:
                continue
            offers = node.get("offers")
            offers = offers if isinstance(offers, list) else [offers] if offers else []
            for o in _walk_ld(offers):
                price = o.get("price") or o.get("lowPrice")
                if price in (None, ""):
                    continue
                try:
                    price = float(str(price).replace(",", ""))
                except ValueError:
                    continue
                avail = str(o.get("availability", ""))
                in_stock = None if not avail else ("InStock" in avail or "LimitedAvailability" in avail)
                return node.get("name"), price, in_stock
    meta = soup.find("meta", attrs={"property": re.compile(r"(product|og):price:amount")})
    if meta and meta.get("content"):
        title = soup.find("meta", attrs={"property": "og:title"})
        try:
            return (title.get("content") if title else None), float(meta["content"]), None
        except ValueError:
            pass
    return None, None, None


def check(url: str) -> PriceCheck:
    key = product_key(url)
    if key.startswith("amazon:") and settings().keepa_api_key:
        return _keepa(key.split(":", 1)[1])
    r = http.get(url)
    title, price, in_stock = parse_product_html(r.text)
    return PriceCheck(key, title, price, in_stock)


def record(conn, url: str, retailer: str | None = None, category: str | None = None) -> PriceCheck | None:
    """Check a product's price now and store it. Returns None when the check failed."""
    try:
        pc = check(url)
    except Exception as e:  # network, robots, parse – never fatal
        log.info("price check failed %s: %s", url, e)
        return None
    if pc.price is None:
        return pc
    now = db.utcnow()
    existing = conn.execute(select(db.products.c.key).where(db.products.c.key == pc.key)).first()
    vals = dict(title=pc.title, url=url, last_price=pc.price, in_stock=pc.in_stock, last_checked=now)
    if retailer:
        vals["retailer"] = retailer
    if category:
        vals["category"] = category
    if existing:
        conn.execute(update(db.products).where(db.products.c.key == pc.key).values(**vals))
    else:
        conn.execute(insert(db.products).values(key=pc.key, **vals))
    conn.execute(insert(db.price_history).values(product_key=pc.key, price=pc.price,
                                                 in_stock=pc.in_stock, observed_at=now))
    return pc


@dataclass
class History:
    n: int = 0
    median90: float | None = None
    min180: float | None = None
    previous: float | None = None
    was_out_of_stock: bool = False


def history(conn, key: str, keepa_stats: dict | None = None) -> History:
    since = datetime.now(timezone.utc) - timedelta(days=180)
    rows = conn.execute(select(db.price_history.c.price, db.price_history.c.observed_at,
                               db.price_history.c.in_stock)
                        .where(db.price_history.c.product_key == key, db.price_history.c.observed_at >= since)
                        .order_by(db.price_history.c.observed_at)).all()
    h = History(n=len(rows))
    if rows:
        cutoff90 = datetime.now(timezone.utc) - timedelta(days=90)

        def aware(t):
            return t if t.tzinfo else t.replace(tzinfo=timezone.utc)

        p90 = [r.price for r in rows if aware(r.observed_at) >= cutoff90] or [r.price for r in rows]
        h.median90 = median(p90)
        h.min180 = min(r.price for r in rows)
        if len(rows) >= 2:
            h.previous = rows[-2].price
            h.was_out_of_stock = rows[-2].in_stock is False and rows[-1].in_stock is True
    if keepa_stats:  # Keepa knows far more history than we do
        h.median90 = keepa_stats.get("avg90") or h.median90
        h.min180 = min(x for x in [keepa_stats.get("min180"), h.min180] if x) if (keepa_stats.get("min180") or h.min180) else None
        h.n = max(h.n, 30)
    return h
