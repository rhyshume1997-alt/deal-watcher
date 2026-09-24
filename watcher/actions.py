"""What happens when we learn something: a purchase, a booking, a renewal, a new wish-list line."""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta

from sqlalchemy import insert, select, update

from . import db, llm
from .config import category_rules, retailers
from .merchants import match_retailer
from .sources import products

log = logging.getLogger(__name__)


def _d(s) -> date | None:
    if isinstance(s, date):
        return s
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def set_cooldown(conn, scope: str, key: str, days: int, reason: str, today: date) -> None:
    if days <= 0 or not key:
        return
    until = today + timedelta(days=days)
    row = conn.execute(select(db.cooldowns).where(db.cooldowns.c.scope == scope, db.cooldowns.c.key == key)).mappings().first()
    if row:
        if row["until"] < until:
            conn.execute(update(db.cooldowns).where(db.cooldowns.c.id == row["id"]).values(until=until, reason=reason))
    else:
        conn.execute(insert(db.cooldowns).values(scope=scope, key=key, until=until, reason=reason))


def record_purchase(conn, info: dict, source_ref: str, today: date | None = None) -> int:
    """info = LLM email extraction with kind 'purchase'. Returns number of items stored."""
    today = today or date.today()
    order_date = _d(info.get("order_date")) or today
    rname = info.get("retailer")
    r = match_retailer(rname or "") if rname else None
    retailer = r["name"] if r else rname
    return_days = info.get("return_days") or (r or {}).get("return_days") or 0
    items = info.get("items") or [{"name": rname or "order", "price": info.get("total"),
                                   "category": (r or {}).get("category", "other"), "url": None}]
    stored = 0
    for i, it in enumerate(items):
        cat = it.get("category") or (r or {}).get("category") or "other"
        rules = category_rules(cat)
        price = it.get("price")
        url = it.get("url")
        vals = {
            "date": order_date, "retailer": retailer, "item": it.get("name"), "category": cat, "price": price,
            "url": url, "product_key": products.product_key(url) if url else None,
            "return_deadline": order_date + timedelta(days=return_days) if return_days else None,
            "warranty_end": (order_date + timedelta(days=30 * rules["warranty_months"])) if rules.get("warranty_months") and (price or 0) >= 100 else None,
            "reminded": {}, "source_ref": f"{source_ref}:{i}",
        }
        if db.insert_ignore(conn, db.purchases, vals, "source_ref"):
            stored += 1
            reason = f"you bought {it.get('name') or cat} on {order_date:%d %b}"
            set_cooldown(conn, "category", cat, rules["cooldown_days"], reason, order_date)
            if url:
                set_cooldown(conn, "product", vals["product_key"], max(rules["cooldown_days"], 30), reason, order_date)
            _mark_planned_bought(conn, it.get("name") or "")
            _mark_alert_acted(conn, retailer, cat, order_date)
    return stored


def _mark_planned_bought(conn, item_name: str) -> None:
    name = f" {item_name.lower()} "
    for p in conn.execute(select(db.planned).where(db.planned.c.status == "active")).mappings():
        kws = [k.lower() for k in (p["keywords"] or [])]
        if kws and all(k in name for k in kws):
            conn.execute(update(db.planned).where(db.planned.c.id == p["id"]).values(status="bought"))


def _mark_alert_acted(conn, retailer: str | None, category: str, when: date) -> None:
    """A purchase shortly after a clicked alert for the same retailer/category counts as acted on."""
    since = when - timedelta(days=10)
    rows = conn.execute(select(db.alerts).where(db.alerts.c.clicked_at.is_not(None),
                                                db.alerts.c.acted.is_(False))).mappings().all()
    for a in rows:
        created = a["created_at"].date() if a["created_at"] else None
        if created and since <= created <= when + timedelta(days=1) and (
                (retailer and a["retailer"] == retailer) or a["category"] == category):
            conn.execute(update(db.alerts).where(db.alerts.c.id == a["id"]).values(acted=True))


def record_booking(conn, info: dict, source_ref: str) -> bool:
    b = info.get("booking") or {}
    if not b:
        return False
    return db.insert_ignore(conn, db.trips, {
        "kind": b.get("kind"), "provider": info.get("retailer"), "name": b.get("name"),
        "destination": b.get("destination"), "start_date": _d(b.get("start_date")), "end_date": _d(b.get("end_date")),
        "price": b.get("price"), "currency": b.get("currency") or "GBP",
        "free_cancel_until": _d(b.get("free_cancel_until")), "ref": b.get("ref"), "reminded": {},
        "source_ref": source_ref,
    }, "source_ref") is not None


def record_renewal(conn, info: dict) -> dict | None:
    """Upsert a subscription/contract. Returns it if it's a price rise worth flagging."""
    rn = info.get("renewal") or {}
    service = rn.get("service") or info.get("retailer")
    if not service:
        return None
    existing = conn.execute(select(db.subscriptions).where(db.subscriptions.c.service == service)).mappings().first()
    new, old = rn.get("new_price"), rn.get("old_price")
    if old is None and existing:
        old = existing["amount"]
    vals = {"amount": new or (existing or {}).get("amount"), "previous_amount": old,
            "cycle": rn.get("cycle") or (existing or {}).get("cycle") or "unknown",
            "next_renewal": _d(rn.get("renewal_date")) or (existing or {}).get("next_renewal"),
            "updated_at": db.utcnow()}
    if existing:
        conn.execute(update(db.subscriptions).where(db.subscriptions.c.id == existing["id"]).values(**vals))
    else:
        conn.execute(insert(db.subscriptions).values(service=service, **vals))
    if new and old and new > old + 0.5:
        return {"service": service, **vals}
    return None


# ------------------------------------------------------------ planned purchases

URL_RE = re.compile(r"https?://\S+")
PRICE_RE = re.compile(r"£\s?(\d[\d,]*(?:\.\d{1,2})?)")


def parse_planned_fallback(line: str) -> dict:
    prices = [float(p.replace(",", "")) for p in PRICE_RE.findall(line)]
    words = [w for w in re.findall(r"[a-z0-9]+", PRICE_RE.sub("", URL_RE.sub("", line.lower())))
             if w not in {"under", "below", "max", "for", "a", "an", "the", "new", "buy", "want", "around", "up", "to"}
             and not w.isdigit() or (w.isdigit() and len(w) <= 3)]
    return {"query": URL_RE.sub("", PRICE_RE.sub("", line)).strip(), "keywords": words[:4], "category": "other",
            "target_price": prices[0] if prices else None, "max_price": None, "travel": None}


def add_planned(conn, line: str) -> dict:
    line = line.strip().splitlines()[0][:300]
    low = line.lower()
    m = re.match(r"^(stop|cancel|bought|remove)\s+(.+)$", low)
    if m:
        needle = m.group(2)
        for p in conn.execute(select(db.planned).where(db.planned.c.status == "active")).mappings():
            if needle in p["raw"].lower() or all(k in needle for k in (p["keywords"] or [])[:2]):
                conn.execute(update(db.planned).where(db.planned.c.id == p["id"]).values(
                    status="bought" if m.group(1) == "bought" else "cancelled"))
                return {"action": "stopped", "raw": p["raw"]}
        return {"action": "not_found", "raw": needle}
    try:
        parsed = llm.parse_planned(conn, line)
    except llm.LLMUnavailable:
        parsed = parse_planned_fallback(line)
    url = (URL_RE.search(line) or [None])[0]
    key = products.product_key(url) if url else None
    pid = conn.execute(insert(db.planned).values(
        raw=line, query=parsed.get("query") or line, keywords=[k.lower() for k in parsed.get("keywords") or []],
        category=parsed.get("category"), target_price=parsed.get("target_price"),
        max_price=parsed.get("max_price"), url=url, product_key=key,
        details={"travel": parsed.get("travel")} if parsed.get("travel") else {}, status="active",
    )).inserted_primary_key[0]
    if url:
        products.record(conn, url, category=parsed.get("category"))
    return {"action": "added", "id": pid, "raw": line, **parsed}


def known_retailer_names() -> list[str]:
    return [r["name"] for r in retailers()]
