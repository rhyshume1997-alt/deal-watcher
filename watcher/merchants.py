"""Turn messy merchant strings ("AMAZON.CO.UK*AB12C LUXEMBOURG") into retailer + category."""
from __future__ import annotations

import re
from functools import lru_cache

from .config import retailers

# Fallback keyword rules, checked in order, for merchants not in retailers.yaml.
KEYWORD_RULES: list[tuple[str, str]] = [
    (r"\b(deliveroo|just ?eat|uber ?eats|domino|papa john|pizza hut|kfc|mcdonald|burger king|five guys|greggs)\b", "takeaway"),
    (r"\b(restaurant|grill|bistro|brasserie|kitchen|trattoria|pizzeria|steak|sushi|ramen|tapas|cafe|caff[eè]|coffee|costa|starbucks|pret|nando|wagamama|bar &|pub\b|tavern|inn\b)", "restaurants"),
    (r"\b(airways|airlines|ryanair|easyjet|jet2|loganair|emirates|klm|lufthansa|jetstar|wizz|hotel|hotels|resort|hostel|airbnb|booking\.com|expedia|trivago|agoda|car hire|hertz|avis|europcar|enterprise rent|parking|lounge|trainline|lner|avanti|scotrail|eurostar|viaggiare)\b", "travel"),
    (r"\b(tesco|sainsbury|asda|morrisons|aldi|lidl|co-?op|waitrose|iceland|farmfoods|m&s food|ocado|costco)\b", "groceries"),
    (r"\b(protein|nutrition|supplement|bulk|myprotein|per4m|pitstop|grenade|optimum)\b", "supplements"),
    (r"\b(nespresso|coffee beans|pact coffee)\b", "coffee"),
    (r"\b(currys|argos|apple store|samsung|dyson|ao\.com|very\.co|richer sounds|box\.co)\b", "electronics"),
    (r"\b(ikea|dfs|sofology|furniture|oak furnitureland|made\.com|wayfair)\b", "furniture"),
    (r"\b(b&q|bandq|wickes|screwfix|toolstation|homebase|travis perkins|diy)\b", "diy"),
    (r"\b(dunelm|the range|b&m|home bargains|john lewis|habitat|h&m home|homesense|tk ?maxx)\b", "homeware"),
    (r"\b(asos|zara|primark|h&m|uniqlo|next\b|river island|tala|gymshark|boohoo|mango|cos\b|allsaints|superdry|sports direct)\b", "clothes"),
    (r"\b(jd sports|new balance|nike|adidas|footlocker|foot locker|office shoes|schuh|size\?)\b", "footwear"),
    (r"\b(spotify|netflix|disney|canva|google|youtube|apple\.com/bill|icloud|adobe|microsoft|chatgpt|openai|anthropic|claude|patreon|audible|kindle unlimited|now tv|dazn|sky\b)", "subscriptions"),
    (r"\b(ticketmaster|see tickets|eventbrite|axs|gigantic|skiddle|festival)\b", "events"),
    (r"\b(shell|bp\b|esso|texaco|gulf|jet petrol|fuel|petrol|ev charg|insurance|confused\.com|admiral|aviva|direct line|halfords|kwik fit)\b", "fuel_car"),
]

# Amex CSV "Category" column → our categories (prefix match, lower-cased).
AMEX_CATEGORY_MAP: list[tuple[str, str]] = [
    ("entertainment-restaurants", "restaurants"),
    ("entertainment-bars", "restaurants"),
    ("entertainment-events", "events"),
    ("travel-airline", "travel"),
    ("travel-lodging", "travel"),
    ("travel-travel agencies", "travel"),
    ("travel-vehicle rental", "travel"),
    ("travel-", "travel"),
    ("general purchases-groceries", "groceries"),
    ("general purchases-supermarkets", "groceries"),
    ("general purchases-clothing", "clothes"),
    ("general purchases-department stores", "homeware"),
    ("general purchases-computer", "electronics"),
    ("general purchases-electronics", "electronics"),
    ("general purchases-home", "homeware"),
    ("general purchases-hardware", "diy"),
    ("transportation-fuel", "fuel_car"),
    ("communications", "subscriptions"),
    ("business services-internet", "subscriptions"),
]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


@lru_cache
def _alias_index() -> list[tuple[str, dict]]:
    idx = []
    for r in retailers():
        for a in [r["name"], *r.get("aliases", [])]:
            idx.append((_norm(a), r))
    # longest alias first so "marks & spencer" beats "m&s" style partials
    idx.sort(key=lambda x: -len(x[0]))
    return idx


def _alias_hit(alias: str, text: str) -> bool:
    if len(alias) <= 4 or not alias.replace(" ", "").isalnum():
        # short or symbol-heavy aliases (e.g. "thg", "b&m") need word boundaries
        return re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text) is not None
    return alias in text


def match_retailer(text: str) -> dict | None:
    t = _norm(text)
    for alias, r in _alias_index():
        if _alias_hit(alias, t):
            return r
    return None


def retailer_by_domain(email_or_domain: str) -> dict | None:
    d = email_or_domain.lower().split("@")[-1].strip(">").strip()
    for r in retailers():
        for dom in r.get("domains", []):
            if d == dom or d.endswith("." + dom):
                return r
    return None


def categorise(description: str, amex_category: str = "") -> tuple[str | None, str]:
    """Return (retailer name or None, category)."""
    r = match_retailer(description)
    if r:
        return r["name"], r["category"]
    t = _norm(description)
    for pattern, cat in KEYWORD_RULES:
        if re.search(pattern, t):
            return None, cat
    ac = _norm(amex_category)
    for prefix, cat in AMEX_CATEGORY_MAP:
        if ac.startswith(prefix):
            return None, cat
    return None, "other"


def clean_merchant(description: str) -> str:
    """Best-effort readable merchant name from a statement line."""
    s = re.sub(r"[*#].*$", "", description or "")
    s = re.sub(r"\b(www\.|\.co\.uk|\.com)\b", " ", s, flags=re.I)
    s = re.sub(r"\s{2,}.*$", "", s)  # Amex pads location after 2+ spaces
    s = re.sub(r"\b\d{3,}\b", "", s)
    return re.sub(r"\s+", " ", s).strip().title()[:60] or description[:60]
