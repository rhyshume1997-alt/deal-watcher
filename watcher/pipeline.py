"""The run loop: collect → enrich → score → alert. Called every 30 minutes by the scheduler."""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, insert, select, update

from . import db, emailer, llm, profile as profile_mod
from .config import TZ, category_rules, sources as source_cfg
from .models import Offer
from .scoring import evaluate
from .sources import feeds, products

log = logging.getLogger(__name__)

ENRICH_BATCH = 15
MAX_ENRICH_PER_TICK = 90


# ------------------------------------------------------------------ helpers

def _now_local() -> datetime:
    return datetime.now(TZ)


def _today() -> date:
    return _now_local().date()


def _due(conn, key: str, minutes: int) -> bool:
    last = db.get_state(conn, f"last_run:{key}")
    if last:
        try:
            if datetime.fromisoformat(last) > datetime.now(timezone.utc) - timedelta(minutes=minutes - 2):
                return False
        except ValueError:
            pass
    return True


def _mark_run(conn, key: str) -> None:
    db.set_state(conn, f"last_run:{key}", datetime.now(timezone.utc).isoformat())


def _bump(conn, scope: str, key: str | None, col: str, n: int = 1) -> None:
    if not key:
        return
    row = conn.execute(select(db.learning).where(db.learning.c.scope == scope, db.learning.c.key == key)).first()
    if row:
        conn.execute(update(db.learning).where(db.learning.c.scope == scope, db.learning.c.key == key)
                     .values(**{col: getattr(db.learning.c, col) + n}))
    else:
        vals = {"useful": 0, "not_useful": 0, "clicks": 0, "ignored": 0, col: n}
        conn.execute(insert(db.learning).values(scope=scope, key=key, **vals))


def upcoming_destinations(conn, today: date) -> list[str]:
    rows = conn.execute(select(db.trips.c.destination).where(
        db.trips.c.start_date >= today, db.trips.c.start_date <= today + timedelta(days=365))).all()
    return sorted({r[0] for r in rows if r[0]})


# ------------------------------------------------------------------ offers

def _enrich(conn, fresh: list[Offer]) -> None:
    """Ask Claude to normalise deals in batches (retailer, category, was-price, flags)."""
    todo = [o for o in fresh if not o.enriched][:MAX_ENRICH_PER_TICK]
    for i in range(0, len(todo), ENRICH_BATCH):
        batch = todo[i:i + ENRICH_BATCH]
        try:
            res = llm.classify_deals(conn, [{"title": o.title, "summary": o.summary, "merchant": o.retailer,
                                             "price": o.price} for o in batch])
        except llm.LLMUnavailable as e:
            log.info("enrichment skipped: %s", e)
            return
        for r in res:
            idx = r.get("i")
            if idx is None or not 0 <= idx < len(batch):
                continue
            o = batch[idx]
            o.retailer = r.get("retailer") or o.retailer
            o.category = r.get("category") or o.category
            o.product = r.get("product") or o.product
            o.price = r["price"] if r.get("price") is not None else o.price
            o.was_price = r["was_price"] if r.get("was_price") is not None else o.was_price
            o.code = r.get("code") or o.code
            o.cashback_gbp = r.get("cashback_gbp") or o.cashback_gbp
            o.amex_credit_gbp = r.get("amex_credit_gbp") or o.amex_credit_gbp
            o.avios = r.get("avios") or o.avios
            if r.get("expires"):
                try:
                    o.expires = date.fromisoformat(r["expires"])
                except ValueError:
                    pass
            o.flags = sorted(set(o.flags) | set(r.get("flags") or []))
            o.financial_bonus_gbp = r.get("financial_bonus_gbp")
            o.competition_prize_gbp = r.get("competition_prize_gbp")
            o.one_line = r.get("one_line") or o.one_line
            o.enriched = True


def _offer_line(o: Offer) -> str:
    if o.one_line:
        base = o.one_line
    elif o.price is not None and o.discount_pct >= 0.05:
        base = f"£{o.price:,.2f} ({o.discount_pct:.0%} off)"
    elif o.price is not None:
        base = "Free" if o.price == 0 else f"£{o.price:,.2f}"
    else:
        base = o.title
    if o.avios:
        base += f" + {o.avios:,} Avios"
    return base[:120]


def ingest_offers(conn, offers: list[Offer], today: date | None = None) -> dict:
    today = today or _today()
    stats = Counter()
    fresh = []
    for o in offers:
        fp = o.fingerprint()
        if conn.execute(select(db.offers.c.id).where(db.offers.c.fingerprint == fp)).first():
            stats["dupe"] += 1
            continue
        fresh.append(o)
    if not fresh:
        return dict(stats)
    _enrich(conn, fresh)
    prof = profile_mod.build(conn, today=today)
    dests = upcoming_destinations(conn, today)
    for o in fresh:
        hist = None
        if o.product_key:
            hist = products.history(conn, o.product_key)
        d = evaluate(conn, o, prof, today=today, hist=hist, upcoming_destinations=dests)
        oid = conn.execute(insert(db.offers).values(
            fingerprint=o.fingerprint(), source=o.source, title=o.title, url=o.url, retailer=o.retailer,
            category=o.category, price=o.price, was_price=o.was_price, data=o.to_dict())).inserted_primary_key[0]
        stats[d.tier] += 1
        if d.tier == "drop":
            # keep a light record of relevant-ish drops for the monthly "suppressed" stats
            if d.relevance >= 0.3:
                conn.execute(insert(db.alerts).values(
                    offer_id=oid, kind="deal", tier="drop", verdict="IGNORE", score=d.score, title=o.title,
                    retailer=o.retailer, category=o.category, url=o.url, payload={"drop_reason": d.drop_reason}))
            continue
        dedupe = f"deal:{(o.retailer or '').lower()}:{(o.product or o.title).lower()[:60]}"
        if _recently_alerted(conn, dedupe, days=7):
            stats["repeat"] += 1
            continue
        _create_alert(conn, kind="planned" if d.planned_hit else ("f1" if o.category == "f1" else "deal"),
                      tier=d.tier, verdict=d.verdict, score=d.score, offer_id=oid, dedupe_key=dedupe,
                      title=o.title, retailer=o.retailer, category=o.category, url=o.url,
                      est_saving=o.cash_saving, payload={
                          "headline": o.retailer or (o.product or o.title)[:50],
                          "offer_line": _offer_line(o) if o.retailer else (o.one_line or o.title)[:120],
                          "reasons": d.reasons, "history_note": d.history_note, "urgency_note": d.urgency_note,
                          "components": d.components, "avios": o.avios, "code": o.code})
    return dict(stats)


def _recently_alerted(conn, dedupe: str, days: int) -> bool:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    return conn.execute(select(db.alerts.c.id).where(
        db.alerts.c.dedupe_key == dedupe, db.alerts.c.tier != "drop", db.alerts.c.created_at >= since)).first() is not None


def _create_alert(conn, *, kind, tier, verdict, score, title, retailer, category, url, est_saving, payload,
                  dedupe_key, offer_id=None) -> int:
    aid = conn.execute(insert(db.alerts).values(
        offer_id=offer_id, kind=kind, tier=tier, verdict=verdict, score=score, title=title, retailer=retailer,
        category=category, url=url, est_saving=est_saving or 0, payload=payload, dedupe_key=dedupe_key,
        created_at=db.utcnow())).inserted_primary_key[0]
    if tier == "instant":
        row = conn.execute(select(db.alerts).where(db.alerts.c.id == aid)).mappings().first()
        if emailer.send_instant(dict(row)):
            conn.execute(update(db.alerts).where(db.alerts.c.id == aid).values(sent_at=db.utcnow()))
    return aid


def queue_reminder(conn, kind: str, dedupe: str, *, headline: str, offer_line: str, reasons: list[str],
                   urgency_note: str | None = None, category: str | None = None, url: str = "",
                   est_saving: float = 0, instant: bool = False, verdict: str = "BUY NOW") -> int | None:
    """Post-purchase / renewal / trip reminders. Each dedupe key fires once."""
    if conn.execute(select(db.alerts.c.id).where(db.alerts.c.dedupe_key == dedupe)).first():
        return None
    return _create_alert(conn, kind=kind, tier="instant" if instant else "digest", verdict=verdict, score=100,
                         title=f"{headline}: {offer_line}", retailer=headline, category=category, url=url,
                         est_saving=est_saving, dedupe_key=dedupe,
                         payload={"headline": headline, "offer_line": offer_line, "reasons": reasons,
                                  "urgency_note": urgency_note, "components": {}, "reminder": True})


# ------------------------------------------------------------------ sources

def run_sources(conn, only: str | None = None) -> dict:
    cfg = source_cfg()
    out = {}
    runners = [("deal_feed", lambda c: feeds.deal_feed(c)), ("blog_feed", lambda c: feeds.blog_feed(c)),
               ("news_query", lambda c: feeds.news_query(c)), ("page_watch", lambda c: feeds.page_watch(conn, c))]
    for kind, fn in runners:
        for c in cfg.get(kind, []):
            if only and c["id"] != only:
                continue
            if not only and not _due(conn, c["id"], c.get("interval_min", 60)):
                continue
            try:
                offers = fn(c)
                out[c["id"]] = ingest_offers(conn, offers)
                db.set_state(conn, f"source_ok:{c['id']}", {"at": db.utcnow().isoformat(), "n": len(offers)})
            except Exception as e:
                log.warning("source %s failed: %s", c["id"], e)
                out[c["id"]] = {"error": str(e)[:200]}
                db.set_state(conn, f"source_err:{c['id']}", {"at": db.utcnow().isoformat(), "error": str(e)[:300]})
            _mark_run(conn, c["id"])
    return out


def run_planned(conn) -> dict:
    """Every 3 hours: check tracked product URLs and deal-site searches for each planned purchase."""
    if not _due(conn, "planned", 180):
        return {}
    stats = Counter()
    for p in conn.execute(select(db.planned).where(db.planned.c.status == "active")).mappings().all():
        if p["url"]:
            pc = products.record(conn, p["url"], category=p["category"])
            if pc and pc.price is not None:
                h = products.history(conn, pc.key, pc.keepa_stats)
                o = Offer(source="tracker", title=pc.title or p["raw"], url=p["url"], price=pc.price,
                          was_price=h.median90 if h.median90 and h.median90 > pc.price else None,
                          category=p["category"], product_key=pc.key, planned_id=p["id"], enriched=True,
                          product=pc.title, source_id=f"tracker:{pc.key}:{pc.price}",
                          flags=["restock"] if h.was_out_of_stock else [])
                stats.update(ingest_offers(conn, [o]))
        try:
            stats.update(ingest_offers(conn, feeds.hukd_search(p["query"] or p["raw"], p["id"])))
        except Exception as e:
            log.info("planned search failed for %s: %s", p["raw"], e)
    _mark_run(conn, "planned")
    return dict(stats)


def run_post_purchase(conn, today: date | None = None) -> dict:
    """Daily: price drops inside return windows, return deadlines, warranty expiry, free-cancel cut-offs."""
    today = today or _today()
    if not _due(conn, "post_purchase", 60 * 20):
        return {}
    n = Counter()
    rows = conn.execute(select(db.purchases).where(
        (db.purchases.c.return_deadline >= today) | (db.purchases.c.warranty_end >= today))).mappings().all()
    for p in rows:
        rules = category_rules(p["category"])
        price = p["price"] or 0
        if price < rules["post_purchase_min"]:
            continue
        rd = p["return_deadline"]
        # price drop since purchase (only while it could still be returned or price-matched)
        if p["url"] and rd and rd >= today:
            pc = products.record(conn, p["url"], p["retailer"], p["category"])
            if pc and pc.price is not None and price - pc.price >= max(10, 0.1 * price):
                drop = round(price - pc.price, 2)
                queue_reminder(conn, "post_purchase", f"drop:{p['id']}:{pc.price}", headline=p["retailer"] or "Price drop",
                               offer_line=f"{p['item']} now £{pc.price:,.2f} (you paid £{price:,.2f})",
                               reasons=["Price has dropped since you bought it",
                                        "Ask for a price match, or return and rebuy"],
                               urgency_note=f"Return window ends {rd:%a %d %b}", category=p["category"],
                               url=p["url"], est_saving=drop, instant=True)
                n["drop"] += 1
        if rd and 0 <= (rd - today).days <= 3:
            queue_reminder(conn, "post_purchase", f"return:{p['id']}", headline=p["retailer"] or "Return window",
                           offer_line=f"Last chance to return {p['item']}",
                           reasons=[f"Bought {p['date']:%d %b} for £{price:,.2f}"],
                           urgency_note=f"Return deadline {rd:%a %d %b}", category=p["category"], url=p["url"] or "")
            n["return"] += 1
        we = p["warranty_end"]
        if we and 0 <= (we - today).days <= 30:
            queue_reminder(conn, "post_purchase", f"warranty:{p['id']}", headline=p["retailer"] or "Warranty",
                           offer_line=f"Warranty on {p['item']} ends {we:%d %b}",
                           reasons=["Check it works properly and report any faults before then",
                                    "Amex purchase protection / Consumer Rights Act may still help after"],
                           category=p["category"], url=p["url"] or "")
            n["warranty"] += 1
    for t in conn.execute(select(db.trips).where(db.trips.c.free_cancel_until >= today)).mappings().all():
        fc = t["free_cancel_until"]
        if (fc - today).days <= 5:
            queue_reminder(conn, "trip", f"freecancel:{t['id']}", headline=t["name"] or t["provider"] or "Booking",
                           offer_line=f"Free cancellation ends {fc:%a %d %b}",
                           reasons=[f"{t['destination'] or ''} {t['start_date'] or ''}".strip(),
                                    "Last chance to check for a cheaper rate and rebook"],
                           urgency_note=f"Free cancellation ends {fc:%a %d %b}", category="travel",
                           instant=(fc - today).days <= 2)
            n["trip"] += 1
    for s in conn.execute(select(db.subscriptions).where(db.subscriptions.c.next_renewal >= today)).mappings().all():
        nr = s["next_renewal"]
        big = (s["amount"] or 0) >= (100 if s["cycle"] != "monthly" else 30)
        if big and (nr - today).days <= 28 and s["reminded_for"] != nr:
            queue_reminder(conn, "renewal", f"renew:{s['service']}:{nr}", headline=s["service"],
                           offer_line=f"Renews {nr:%d %b} at £{s['amount']:,.2f}",
                           reasons=["Compare prices before it auto-renews – loyalty rarely pays",
                                    "Ask for a retention deal if staying"],
                           urgency_note=f"Renews {nr:%a %d %b}", category=s["category"])
            conn.execute(update(db.subscriptions).where(db.subscriptions.c.id == s["id"]).values(reminded_for=nr))
            n["renewal"] += 1
    for rise in profile_mod.sync_subscriptions(conn, today):
        queue_reminder(conn, "renewal", f"rise:{rise['service']}:{rise['amount']}", headline=rise["service"],
                       offer_line=f"Went up £{rise['previous_amount']:.2f} → £{rise['amount']:.2f}",
                       reasons=["Price rise spotted on your statement"], category=rise["category"])
        n["rise"] += 1
    _mark_run(conn, "post_purchase")
    return dict(n)


# ------------------------------------------------------------------ digest & monthly

def run_digest(conn, force: bool = False) -> dict:
    now = _now_local()
    if not force and (now.hour < 7 or (now.hour == 7 and now.minute < 30)):
        return {}
    today = now.date().isoformat()
    if not force and db.get_state(conn, "digest_sent") == today:
        return {}
    pending = conn.execute(select(db.alerts).where(db.alerts.c.tier == "digest", db.alerts.c.sent_at.is_(None))
                           .order_by(db.alerts.c.score.desc())).mappings().all()
    deals = [dict(a) for a in pending if not (a["payload"] or {}).get("reminder")][:12]
    reminders = [dict(a) for a in pending if (a["payload"] or {}).get("reminder")]
    since = datetime.now(timezone.utc) - timedelta(days=1)
    suppressed = conn.execute(select(func.count()).select_from(db.offers).where(db.offers.c.seen_at >= since)).scalar() or 0
    suppressed = max(0, suppressed - len(deals))
    sent = False
    if deals or reminders:
        sent = emailer.send_digest(deals, reminders, suppressed)
        if sent:
            ids = [a["id"] for a in pending]  # anything not shown today (over 12) is dropped, not carried over
            conn.execute(update(db.alerts).where(db.alerts.c.id.in_(ids)).values(sent_at=db.utcnow()))
    db.set_state(conn, "digest_sent", today)
    _record_ignores(conn)
    return {"deals": len(deals), "reminders": len(reminders), "sent": sent}


def _record_ignores(conn) -> None:
    """Alerts sent 3+ days ago with no click or feedback count as ignored (once)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=3)
    rows = conn.execute(select(db.alerts).where(
        db.alerts.c.sent_at.is_not(None), db.alerts.c.sent_at <= cutoff, db.alerts.c.clicked_at.is_(None),
        db.alerts.c.feedback.is_(None))).mappings().all()
    for a in rows:
        if (a["payload"] or {}).get("ignored_counted"):
            continue
        _bump(conn, "category", a["category"], "ignored")
        _bump(conn, "retailer", a["retailer"], "ignored")
        conn.execute(update(db.alerts).where(db.alerts.c.id == a["id"]).values(
            payload={**(a["payload"] or {}), "ignored_counted": True}))


def apply_feedback(conn) -> int:
    """Fold new click/useful/not-useful events (written by the tracking function) into learning."""
    last = db.get_state(conn, "feedback_cursor", 0) or 0
    rows = conn.execute(select(db.alert_events, db.alerts.c.category, db.alerts.c.retailer, db.alerts.c.offer_id)
                        .join(db.alerts, db.alerts.c.id == db.alert_events.c.alert_id)
                        .where(db.alert_events.c.id > last).order_by(db.alert_events.c.id)).mappings().all()
    for r in rows:
        col = {"click": "clicks", "useful": "useful", "not_useful": "not_useful"}.get(r["event"])
        if col:
            _bump(conn, "category", r["category"], col)
            _bump(conn, "retailer", r["retailer"], col)
            if r["offer_id"]:
                src = conn.execute(select(db.offers.c.source).where(db.offers.c.id == r["offer_id"])).scalar()
                _bump(conn, "source", src, col)
        last = r["id"]
    db.set_state(conn, "feedback_cursor", last)
    return len(rows)


def monthly_stats(conn, start: date, end: date) -> dict:
    s0 = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    s1 = datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc)
    rows = conn.execute(select(db.alerts).where(db.alerts.c.created_at >= s0, db.alerts.c.created_at < s1)).mappings().all()
    sent = [r for r in rows if r["tier"] in ("instant", "digest") and r["sent_at"]]
    acted = [r for r in sent if r["acted"] or r["feedback"] == "useful" and r["clicked_at"]]
    useful_cats = Counter(r["category"] for r in sent if r["feedback"] == "useful" or r["acted"])
    drop_reasons = Counter(((r["payload"] or {}).get("drop_reason") or "other").split(":")[0].split("(")[0].strip()
                           for r in rows if r["tier"] == "drop")
    total_offers = conn.execute(select(func.count()).select_from(db.offers)
                                .where(db.offers.c.seen_at >= s0, db.offers.c.seen_at < s1)).scalar() or 0
    learning_lines = []
    for r in conn.execute(select(db.learning)).mappings():
        u, d = r["useful"] or 0, r["not_useful"] or 0
        if u + d >= 2:
            if d > u:
                learning_lines.append(f"Fewer {r['key']} alerts ({d} not useful vs {u} useful)")
            elif u > d:
                learning_lines.append(f"More {r['key']} alerts ({u} useful)")
    subs = conn.execute(select(db.subscriptions).order_by(db.subscriptions.c.amount.desc())).mappings().all()
    return {
        "saved": sum(r["est_saving"] or 0 for r in acted),
        "acted": len(acted),
        "instant": sum(1 for r in sent if r["tier"] == "instant"),
        "digest": sum(1 for r in sent if r["tier"] == "digest"),
        "clicked": sum(1 for r in sent if r["clicked_at"]),
        "useful": sum(1 for r in sent if r["feedback"] == "useful"),
        "not_useful": sum(1 for r in sent if r["feedback"] == "not_useful"),
        "ignored": sum(1 for r in sent if not r["clicked_at"] and not r["feedback"]),
        "suppressed": max(0, total_offers - len(sent)),
        "avios": sum((r["payload"] or {}).get("avios") or 0 for r in acted),
        "top_categories": useful_cats.most_common(5),
        "top_suppressed": drop_reasons.most_common(4),
        "learning": learning_lines[:8],
        "subscriptions": [dict(s) for s in subs[:12]],
    }


def run_monthly(conn, force: bool = False) -> dict:
    now = _now_local()
    first = now.date().replace(day=1)
    prev_start = (first - timedelta(days=1)).replace(day=1)
    key = prev_start.strftime("%Y-%m")
    if not force and (now.day != 1 or now.hour < 8 or db.get_state(conn, "monthly_sent") == key):
        return {}
    stats = monthly_stats(conn, prev_start, first)
    ok = emailer.send_monthly(prev_start.strftime("%B %Y"), stats)
    if ok:
        db.set_state(conn, "monthly_sent", key)
    return {"sent": ok, **{k: stats[k] for k in ("saved", "acted", "instant", "digest")}}


# ------------------------------------------------------------------ tick

def tick(engine) -> dict:
    """Everything that's due. Each stage commits separately so one failure can't block the rest."""
    from .ingest import gmail  # local import keeps imaplib out of tests that don't need it

    report = {}
    stages = [("feedback", apply_feedback), ("gmail", gmail.sync), ("sources", run_sources),
              ("planned", run_planned), ("post_purchase", run_post_purchase), ("digest", run_digest),
              ("monthly", run_monthly)]
    for name, fn in stages:
        try:
            with engine.begin() as conn:
                report[name] = fn(conn)
        except Exception as e:
            log.exception("stage %s failed", name)
            report[name] = {"error": str(e)[:300]}
    return report
