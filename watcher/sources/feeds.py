"""RSS-based sources: community deal feeds, editorial blogs, Google News searches, page watches."""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote_plus

import feedparser
from bs4 import BeautifulSoup

from .. import db
from ..models import Offer
from . import http

log = logging.getLogger(__name__)

PRICE_RE = re.compile(r"£\s?(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")
WAS_RE = re.compile(r"(?:was|rrp|usually|normally|reduced from)\s*£\s?(\d[\d,]*(?:\.\d{1,2})?)", re.I)


def _money(s: str) -> float:
    return float(s.replace(",", ""))


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(html or "", "html.parser").get_text(" ")).strip()


def _entry_value(entry, *needles: str):
    """Pull a value from feedparser keys like 'pepper_merchant' / 'pepper_temperature'."""
    for k, v in entry.items():
        lk = k.lower()
        if any(n in lk for n in needles):
            if isinstance(v, dict):
                return v.get("name") or v.get("value") or next(iter(v.values()), None)
            return v
    return None


def parse_deal_title(title: str) -> dict:
    """'Nespresso Vertuo Pop £49.99 @ Amazon' → product, price, merchant."""
    out: dict = {"merchant": None, "price": None, "product": title}
    m = re.search(r"\s@\s*([^@]+)$", title)
    if m:
        out["merchant"] = m.group(1).strip()
        title = title[: m.start()]
    if re.search(r"\bfree\b", title, re.I) and not PRICE_RE.search(title):
        out["price"] = 0.0
    prices = PRICE_RE.findall(title)
    if prices:
        out["price"] = _money(prices[0])
    out["product"] = PRICE_RE.sub("", title).strip(" -|,")
    return out


def _published(entry) -> datetime | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


def _fresh(entry, max_age_h: int = 72) -> bool:
    p = _published(entry)
    return p is None or p > datetime.now(timezone.utc) - timedelta(hours=max_age_h)


def fetch_feed(urls: list[str]):
    url, r = http.get_first(urls)
    feed = feedparser.parse(r.content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"not a feed: {url}")
    return url, feed


def deal_feed(cfg: dict) -> list[Offer]:
    _, feed = fetch_feed(cfg["urls"])
    offers = []
    min_heat = cfg.get("min_heat")
    for e in feed.entries:
        if not _fresh(e):
            continue
        title = e.get("title", "")
        parsed = parse_deal_title(title)
        summary = _text(e.get("summary", ""))
        heat = _entry_value(e, "temperature", "heat")
        try:
            heat = float(re.sub(r"[^\d.\-]", "", str(heat))) if heat is not None else None
        except ValueError:
            heat = None
        if min_heat is not None and heat is not None and heat < min_heat:
            continue
        merchant = _entry_value(e, "merchant") or parsed["merchant"]
        price = parsed["price"]
        pv = _entry_value(e, "price")
        if pv is not None:
            try:
                price = _money(str(pv).replace("£", ""))
            except ValueError:
                pass
        was = WAS_RE.search(summary)
        flags = ["freebie"] if price == 0 else []
        offers.append(Offer(
            source=cfg["id"], title=title, url=e.get("link", ""), retailer=merchant,
            product=parsed["product"], price=price, was_price=_money(was.group(1)) if was else None,
            heat=heat, summary=summary[:800], flags=flags, source_id=e.get("id") or e.get("link"),
        ))
    return offers


def _keyword_hit(text: str, keywords: list[str]) -> bool:
    t = f" {text.lower()} "
    return any(k.lower() in t for k in keywords)


def blog_feed(cfg: dict) -> list[Offer]:
    _, feed = fetch_feed(cfg["urls"])
    out = []
    for e in feed.entries:
        if not _fresh(e, 96):
            continue
        title, summary = e.get("title", ""), _text(e.get("summary", ""))
        if cfg.get("keywords") and not _keyword_hit(f"{title} {summary}", cfg["keywords"]):
            continue
        out.append(Offer(source=cfg["id"], title=title, url=e.get("link", ""), summary=summary[:1200],
                         category=cfg.get("category"), source_id=e.get("id") or e.get("link")))
    return out


def news_query(cfg: dict) -> list[Offer]:
    url = f"https://news.google.com/rss/search?q={quote_plus(cfg['q'])}+when:3d&hl=en-GB&gl=GB&ceid=GB:en"
    _, feed = fetch_feed([url])
    out = []
    for e in feed.entries:
        if not _fresh(e, 72):
            continue
        title, summary = e.get("title", ""), _text(e.get("summary", ""))
        if cfg.get("keywords") and not _keyword_hit(f"{title} {summary}", cfg["keywords"]):
            continue
        flags = ["f1"] if cfg.get("category") == "f1" else []
        out.append(Offer(source=cfg["id"], title=title, url=e.get("link", ""), summary=summary[:600],
                         category=cfg.get("category"), flags=flags, source_id=e.get("id") or e.get("link")))
    return out


def page_watch(conn, cfg: dict) -> list[Offer]:
    """Alert when a watched phrase newly appears on a page (e.g. 'on sale now', 'presale')."""
    r = http.get(cfg["url"])
    text = _text(r.text).lower()
    phrases = sorted({p for p in cfg["alert_on"] if p.lower() in text})
    key = f"page_watch:{cfg['id']}"
    prev = db.get_state(conn, key)
    db.set_state(conn, key, {"phrases": phrases, "hash": hashlib.sha1(text.encode()).hexdigest(),
                             "checked": datetime.now(timezone.utc).isoformat()})
    if prev is None:
        return []  # first run just records a baseline
    new = [p for p in phrases if p not in prev.get("phrases", [])]
    if not new:
        return []
    return [Offer(source=cfg["id"], title=f"{cfg['title']}: now shows “{', '.join(new)}”", url=cfg["url"],
                  category=cfg.get("category"), flags=["f1"] if cfg.get("category") == "f1" else ["restock"],
                  summary=f"Page changed. New phrases: {', '.join(new)}", enriched=True,
                  source_id=f"{cfg['id']}:{date.today()}:{'|'.join(new)}")]


def hukd_search(query: str, planned_id: int) -> list[Offer]:
    """Deal-site search for a planned purchase."""
    urls = [f"https://www.hotukdeals.com/rss/search?q={quote_plus(query)}",
            f"https://www.hotukdeals.com/rss/keyword-alarm?q={quote_plus(query)}"]
    offers = deal_feed({"id": "hukd_search", "urls": urls})
    for o in offers:
        o.planned_id = planned_id
    return offers
