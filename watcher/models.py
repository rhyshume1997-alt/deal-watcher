"""The one data shape every source produces."""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import date


@dataclass
class Offer:
    source: str
    title: str
    url: str
    retailer: str | None = None
    category: str | None = None
    product: str | None = None
    price: float | None = None
    was_price: float | None = None
    code: str | None = None
    cashback_gbp: float | None = None
    amex_credit_gbp: float | None = None
    avios: int | None = None
    expires: date | None = None
    flags: list[str] = field(default_factory=list)
    heat: float | None = None  # community score (HotUKDeals temperature)
    product_key: str | None = None
    financial_bonus_gbp: float | None = None
    competition_prize_gbp: float | None = None
    summary: str = ""
    one_line: str = ""
    planned_id: int | None = None
    source_id: str | None = None
    enriched: bool = False

    @property
    def discount_gbp(self) -> float:
        if self.price is not None and self.was_price and self.was_price > self.price:
            return round(self.was_price - self.price, 2)
        return 0.0

    @property
    def discount_pct(self) -> float:
        if self.price is not None and self.was_price and self.was_price > 0:
            return max(0.0, 1 - self.price / self.was_price)
        return 0.0

    @property
    def cash_saving(self) -> float:
        """Discount + cashback + Amex credit. Avios are never converted to cash."""
        return round(self.discount_gbp + (self.cashback_gbp or 0) + (self.amex_credit_gbp or 0), 2)

    @property
    def effective_pct(self) -> float:
        base = self.was_price or self.price
        if not base:
            return 0.0
        return self.cash_saving / base

    def fingerprint(self) -> str:
        key = self.source_id or self.url or self.title
        norm = re.sub(r"[^a-z0-9]+", " ", f"{self.source}|{key}".lower()).strip()
        return hashlib.sha256(norm.encode()).hexdigest()[:40]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["expires"] = self.expires.isoformat() if self.expires else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Offer":
        d = dict(d)
        if d.get("expires"):
            d["expires"] = date.fromisoformat(d["expires"])
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
