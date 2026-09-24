"""The value test. Decides INSTANT / DIGEST / DROP and BUY NOW / WAIT / IGNORE for each offer."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select

from . import db
from .config import category_rules, retailers, sale_events
from .models import Offer
from .profile import SpendProfile
from .sources.products import History

# Categories you told me you care about. Used as a floor before statements are imported.
STATED_INTERESTS = {
    "coffee": 0.5, "homeware": 0.5, "diy": 0.5, "clothes": 0.45, "footwear": 0.45, "supplements": 0.5,
    "travel": 0.6, "groceries": 0.35, "restaurants": 0.4, "takeaway": 0.35, "subscriptions": 0.4,
    "furniture": 0.45, "electronics": 0.4, "large_electronics": 0.35, "f1": 1.0, "finance": 0.45,
    "christmas": 0.35, "gifts": 0.35, "events": 0.3,
}

JUNK_FLAGS = {"clickbait", "spend_to_save", "low_value"}


@dataclass
class Decision:
    tier: str = "drop"          # instant | digest | drop
    verdict: str = "IGNORE"     # BUY NOW | WAIT | IGNORE
    score: float = 0.0
    relevance: float = 0.0
    reasons: list[str] = field(default_factory=list)      # "why it matters" lines
    history_note: str | None = None
    urgency_note: str | None = None
    drop_reason: str | None = None
    planned_hit: bool = False
    components: dict = field(default_factory=dict)        # shown separately in the email

    def drop(self, why: str) -> "Decision":
        self.tier, self.verdict, self.drop_reason = "drop", "IGNORE", why
        return self


def _known_retailer(name: str | None) -> dict | None:
    if not name:
        return None
    n = name.lower()
    for r in retailers():
        if n == r["name"].lower() or n in [a.lower() for a in r.get("aliases", [])]:
            return r
    return None


def learned_multiplier(conn, category: str | None, retailer: str | None, source: str | None) -> float:
    rows = conn.execute(select(db.learning).where(
        ((db.learning.c.scope == "category") & (db.learning.c.key == (category or ""))) |
        ((db.learning.c.scope == "retailer") & (db.learning.c.key == (retailer or ""))) |
        ((db.learning.c.scope == "source") & (db.learning.c.key == (source or ""))))).mappings().all()
    mult = 1.0
    for r in rows:
        u, d, c, ig = r["useful"] or 0, r["not_useful"] or 0, r["clicks"] or 0, r["ignored"] or 0
        # Beta-style estimate of "useful", centred on 0.5; clicks are a weak positive.
        p = (u + 0.3 * c + 1) / (u + 0.3 * c + d + 2)
        mult *= 0.5 + p                     # 0.5 .. 1.5
        mult *= 0.97 ** min(ig, 15)         # repeatedly ignored → slowly quieter
    return max(0.3, min(1.8, mult))


def active_cooldown(conn, category: str | None, retailer: str | None, product_key: str | None,
                    today: date) -> str | None:
    rows = conn.execute(select(db.cooldowns).where(db.cooldowns.c.until >= today)).mappings().all()
    for r in rows:
        if (r["scope"] == "category" and r["key"] == category) or \
           (r["scope"] == "retailer" and retailer and r["key"].lower() == retailer.lower()) or \
           (r["scope"] == "product" and product_key and r["key"] == product_key):
            return r["reason"] or f"recent {r['key']} purchase"
    return None


def upcoming_sale(category: str | None, today: date) -> str | None:
    for ev in sale_events():
        if category not in ev["categories"]:
            continue
        for yr in (today.year, today.year + 1):
            try:
                d = date(yr, ev["month"], ev["day"])
            except ValueError:
                continue
            if 0 < (d - today).days <= ev["wait_days"]:
                return f"{ev['name']} starts in {(d - today).days} days"
    return None


def match_planned(conn, offer: Offer) -> dict | None:
    items = conn.execute(select(db.planned).where(db.planned.c.status == "active")).mappings().all()
    title = f" {(offer.title or '').lower()} {(offer.product or '').lower()} "
    for p in items:
        if offer.planned_id == p["id"]:
            return dict(p)
        kws = [k.lower() for k in (p["keywords"] or []) if k]
        if kws and all(k in title for k in kws):
            return dict(p)
    return None


def evaluate(conn, offer: Offer, profile: SpendProfile, today: date | None = None,
             hist: History | None = None, upcoming_destinations: list[str] | None = None) -> Decision:
    today = today or date.today()
    d = Decision()
    flags = set(offer.flags or [])
    known = _known_retailer(offer.retailer)
    if known:
        offer.retailer = known["name"]
    category = offer.category or (known["category"] if known else None) or "other"
    offer.category = category
    rules = category_rules(category)

    if flags & JUNK_FLAGS:
        return d.drop("junk: " + ", ".join(sorted(flags & JUNK_FLAGS)))

    # ---------------- relevance
    r_ret = profile.retailer_affinity(offer.retailer)
    if known:
        r_ret = max(r_ret, 0.35)
    r_cat = max(profile.category_affinity(category), STATED_INTERESTS.get(category, 0.1)) * 0.85
    relevance = max(r_ret, r_cat)
    if r_ret >= 0.45:
        spent = profile.by_retailer.get(offer.retailer or "", 0)
        d.reasons.append(f"You spent £{spent:,.0f} at {offer.retailer} in the last year" if spent
                         else f"You shop at {offer.retailer}")
    elif profile.category_affinity(category) >= 0.4:
        d.reasons.append(f"You buy {category.replace('_', ' ')} regularly")

    planned = match_planned(conn, offer)
    if planned:
        relevance = 1.0
        d.reasons.insert(0, f"On your list: {planned['raw']}")
    if "f1" in flags or category == "f1":
        relevance = 1.0
    if upcoming_destinations and category == "travel":
        text = f"{offer.title} {offer.summary}".lower()
        hit = next((x for x in upcoming_destinations if x and x.lower() in text), None)
        if hit:
            relevance = max(relevance, 0.9)
            d.reasons.append(f"Relevant to your {hit} trip")
    relevance *= learned_multiplier(conn, category, offer.retailer, offer.source)
    d.relevance = relevance = min(relevance, 1.2)

    # ---------------- price history
    lowest = unusual = fake = False
    if hist and hist.median90 and offer.price is not None and offer.price > 0:
        if hist.n >= 5 and offer.was_price and offer.price >= 0.97 * hist.median90:
            fake = True
        if offer.price <= hist.median90 * 0.85:
            unusual = True
        if hist.min180 and offer.price <= hist.min180 * 1.005 and hist.n >= 5:
            lowest = True
        # measure the discount against the real usual price, not the "was" price
        if not offer.was_price or offer.was_price > hist.median90 * 1.1:
            offer.was_price = round(hist.median90, 2) if hist.median90 > offer.price else offer.was_price
        if lowest:
            d.history_note = "Lowest price in 6 months"
        elif unusual:
            d.history_note = f"{int(round(100 * (1 - offer.price / hist.median90)))}% below its usual £{hist.median90:,.2f}"
        elif fake:
            d.history_note = f"Fake discount – it's usually £{hist.median90:,.2f} anyway"
        else:
            d.history_note = f"Normal price (usually £{hist.median90:,.2f})"
    if fake:
        return d.drop("fake discount")

    # ---------------- components (shown separately; Avios never converted)
    comp = {}
    if offer.discount_gbp:
        comp["Retailer discount"] = offer.discount_gbp
    if offer.amex_credit_gbp:
        comp["Amex credit"] = offer.amex_credit_gbp
    if offer.cashback_gbp:
        comp["Cashback"] = offer.cashback_gbp
    d.components = comp
    stack_count = len(comp) + (1 if offer.avios else 0) + (1 if offer.code else 0)
    if stack_count >= 2:
        d.reasons.append("Stacks: " + " + ".join(
            list(comp) + (["Avios"] if offer.avios else []) + (["code"] if offer.code else [])))
    saving = offer.cash_saving
    pct = offer.effective_pct
    heat = offer.heat or 0

    # ---------------- urgency
    urgent = False
    if offer.expires:
        days = (offer.expires - today).days
        if days < 0:
            return d.drop("expired")
        if days <= 2:
            urgent = True
            d.urgency_note = "Ends today" if days == 0 else ("Ends tomorrow" if days == 1 else f"Ends {offer.expires:%A}")
        else:
            d.urgency_note = f"Ends {offer.expires:%a %d %b}"
    if flags & {"low_stock", "restock"}:
        urgent = True
        d.urgency_note = d.urgency_note or ("Back in stock" if "restock" in flags else "Low stock")

    # ---------------- value gates by offer type
    passes, force_instant = False, False
    if "f1" in flags or category == "f1":
        passes, force_instant = True, True
        d.reasons.insert(0, "F1 tickets/experiences – these go fast")
    elif "price_error" in flags:
        passes = relevance >= 0.2 or heat >= 300
        force_instant = passes
        d.reasons.insert(0, "Possible price error – may be cancelled, but costs nothing to try")
    elif flags & {"freebie", "sample"}:
        passes = heat >= 200 or relevance >= 0.45
        force_instant = passes and heat >= 800
        d.reasons.append("Free")
    elif "competition" in flags:
        passes = (offer.competition_prize_gbp or 0) >= 500 and relevance >= 0.5
    elif "financial" in flags or category == "finance":
        bonus = offer.financial_bonus_gbp or saving
        passes = bonus >= rules["min_saving"]
        if passes:
            d.reasons.append(f"£{bonus:,.0f} bonus")
            comp.setdefault("Bonus", bonus)
    elif "amex_offer" in flags:
        credit = offer.amex_credit_gbp or 0
        passes = relevance >= 0.3 and (credit >= 10 or (offer.avios or 0) >= 1000)
        force_instant = passes and relevance >= 0.6 and credit >= 20
        d.reasons.append("Amex Offer – add it to your card before it fills up")
    elif "free_trial" in flags:
        passes = relevance >= 0.45
    elif offer.price is None and not offer.was_price:
        # editorial post with no numbers: only keep concrete travel/Avios/Amex news
        passes = bool(flags & {"avios", "travel", "amex_offer", "stackable"}) and relevance >= 0.5 and bool(offer.one_line)
    else:
        min_saving, min_pct = rules["min_saving"], rules["min_pct"]
        if saving >= min_saving and pct >= min_pct:
            passes = True
        elif saving == 0 and heat >= 500 and relevance >= 0.5:
            passes = True  # the community rates it highly even though we can't see the discount
            d.reasons.append(f"Very popular deal ({int(heat)}° on HotUKDeals)")
        elif planned and offer.price is not None and planned.get("target_price") and offer.price <= planned["target_price"]:
            passes = True
        else:
            return d.drop(f"below value test (saving £{saving:.0f}, {pct:.0%}; needs £{min_saving} and {min_pct:.0%})")

    if not passes:
        return d.drop("did not pass the value test for its type")

    # ---------------- planned purchase targets
    if planned and offer.price is not None:
        tp, mp = planned.get("target_price"), planned.get("max_price")
        if mp and offer.price > mp:
            return d.drop("above your max price")
        if tp and offer.price <= tp:
            d.planned_hit = True
            d.reasons.insert(0, f"Hit your target price (£{tp:,.0f})")

    # ---------------- cooldown after a purchase
    cd = active_cooldown(conn, category, offer.retailer, offer.product_key, today)
    if cd and not (lowest or pct >= 0.4 or planned or force_instant):
        return d.drop(f"cooldown: {cd}")

    # ---------------- score
    strength = 0.5 + min(pct, 0.6) * 1.5 + 0.12 * max(0, stack_count - 1) + min(0.3, heat / 3000)
    if lowest:
        strength += 0.25
    elif unusual:
        strength += 0.15
    if urgent:
        strength += 0.1
    d.score = round(100 * relevance * min(strength, 1.8), 1)

    # ---------------- route
    instant = force_instant or d.planned_hit or \
        (urgent and relevance >= 0.5 and d.score >= 55) or \
        (stack_count >= 2 and saving >= 30 and relevance >= 0.5) or \
        (lowest and relevance >= 0.6 and saving >= rules["min_saving"] * 2) or \
        d.score >= 110
    if instant:
        d.tier = "instant"
    elif d.score >= 35:
        d.tier = "digest"
    else:
        return d.drop(f"score {d.score} below digest threshold")

    # ---------------- verdict
    wait_for = upcoming_sale(category, today)
    if wait_for and not (lowest or urgent or d.planned_hit or force_instant or unusual):
        d.verdict = "WAIT"
        d.reasons.append(f"{wait_for} – usually cheaper then")
        if d.tier == "instant":
            d.tier = "digest"
    else:
        d.verdict = "BUY NOW"
    return d
