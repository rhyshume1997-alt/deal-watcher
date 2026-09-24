"""What you actually spend on. Drives relevance scoring and subscription detection."""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median

from sqlalchemy import select

from . import db
from .merchants import clean_merchant


@dataclass
class SpendProfile:
    total: float = 0.0
    by_category: dict[str, float] = field(default_factory=dict)
    by_retailer: dict[str, float] = field(default_factory=dict)
    count_by_retailer: dict[str, int] = field(default_factory=dict)
    last_by_retailer: dict[str, date] = field(default_factory=dict)
    last_by_category: dict[str, date] = field(default_factory=dict)
    months: float = 1.0

    def _affinity(self, spend: float) -> float:
        """Map annualised spend to 0..1 on a log scale (£20/yr ≈ 0.25, £300 ≈ 0.6, £2000+ ≈ 1)."""
        if spend <= 0:
            return 0.0
        yearly = spend * 12 / max(self.months, 1)
        return max(0.0, min(1.0, (math.log10(yearly) - 0.7) / 2.6))

    def retailer_affinity(self, retailer: str | None) -> float:
        return self._affinity(self.by_retailer.get(retailer or "", 0.0))

    def category_affinity(self, category: str | None) -> float:
        return self._affinity(self.by_category.get(category or "", 0.0))


def build(conn, days: int = 365, today: date | None = None) -> SpendProfile:
    today = today or date.today()
    since = today - timedelta(days=days)
    rows = conn.execute(select(db.transactions).where(db.transactions.c.date >= since)).mappings().all()
    p = SpendProfile()
    if not rows:
        return p
    first = min(r["date"] for r in rows)
    p.months = max(1.0, (today - first).days / 30.4)
    cat, ret, cnt = defaultdict(float), defaultdict(float), defaultdict(int)
    for r in rows:
        if r["amount"] <= 0:
            continue
        p.total += r["amount"]
        cat[r["category"] or "other"] += r["amount"]
        if r["category"]:
            p.last_by_category[r["category"]] = max(p.last_by_category.get(r["category"], r["date"]), r["date"])
        key = r["retailer"] or clean_merchant(r["description"])
        ret[key] += r["amount"]
        cnt[key] += 1
        p.last_by_retailer[key] = max(p.last_by_retailer.get(key, r["date"]), r["date"])
    p.by_category, p.by_retailer, p.count_by_retailer = dict(cat), dict(ret), dict(cnt)
    return p


def detect_recurring(conn, today: date | None = None) -> list[dict]:
    """Find charges that repeat monthly or yearly at a similar amount (subscriptions, insurance)."""
    today = today or date.today()
    rows = conn.execute(select(db.transactions).where(
        db.transactions.c.date >= today - timedelta(days=400), db.transactions.c.amount > 0)).mappings().all()
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[r["retailer"] or clean_merchant(r["description"])].append(r)
    found = []
    for name, txs in groups.items():
        txs.sort(key=lambda r: r["date"])
        if len(txs) < 2:
            continue
        gaps = [(b["date"] - a["date"]).days for a, b in zip(txs, txs[1:])]
        amounts = [t["amount"] for t in txs]
        med_amt = median(amounts)
        similar = all(abs(a - med_amt) <= max(2.0, 0.15 * med_amt) for a in amounts[-3:])
        med_gap = median(gaps)
        cycle = None
        if len(txs) >= 3 and 26 <= med_gap <= 35 and similar:
            cycle, step = "monthly", 30
        elif 350 <= med_gap <= 380 and similar:
            cycle, step = "annual", 365
        if not cycle:
            continue
        last = txs[-1]
        prev = txs[-2]["amount"]
        found.append({
            "service": name, "category": last["category"] or "subscriptions", "amount": last["amount"],
            "previous_amount": prev if abs(prev - last["amount"]) > 0.01 else None,
            "cycle": cycle, "next_renewal": last["date"] + timedelta(days=step),
        })
    return found


def sync_subscriptions(conn, today: date | None = None) -> list[dict]:
    """Upsert detected recurring charges. Returns ones whose price went up."""
    rises = []
    for s in detect_recurring(conn, today):
        existing = conn.execute(select(db.subscriptions).where(
            db.subscriptions.c.service == s["service"])).mappings().first()
        if existing:
            vals = {"amount": s["amount"], "cycle": s["cycle"], "next_renewal": s["next_renewal"],
                    "updated_at": db.utcnow()}
            if existing["amount"] and s["amount"] > existing["amount"] + 0.01:
                vals["previous_amount"] = existing["amount"]
                rises.append({**s, "previous_amount": existing["amount"]})
            conn.execute(db.subscriptions.update().where(db.subscriptions.c.id == existing["id"]).values(**vals))
        else:
            conn.execute(db.subscriptions.insert().values(**s))
            if s["previous_amount"] and s["amount"] > s["previous_amount"] + 0.01:
                rises.append(s)
    return rises


def report(conn) -> str:
    """Plain-text spending summary (used by `python -m watcher profile`)."""
    p = build(conn)
    if not p.total:
        return "No transactions yet. Import a statement first: python -m watcher import <file>"
    lines = [f"Spend in the last {p.months:.0f} months: £{p.total:,.0f} (≈ £{p.total / p.months:,.0f}/month)", "",
             "By category:"]
    for c, v in sorted(p.by_category.items(), key=lambda x: -x[1]):
        lines.append(f"  {c:<18} £{v:>9,.0f}  {100 * v / p.total:4.1f}%")
    lines += ["", "Top merchants:"]
    for r, v in sorted(p.by_retailer.items(), key=lambda x: -x[1])[:40]:
        lines.append(f"  {r[:30]:<30} £{v:>9,.0f}  x{p.count_by_retailer[r]:<3} last {p.last_by_retailer[r]}")
    rec = detect_recurring(conn)
    if rec:
        lines += ["", "Recurring charges:"]
        for s in sorted(rec, key=lambda s: -s["amount"]):
            lines.append(f"  {s['service'][:30]:<30} £{s['amount']:>8,.2f} {s['cycle']:<8} next ≈ {s['next_renewal']}")
    return "\n".join(lines)
