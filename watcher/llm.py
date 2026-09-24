"""Claude calls for the fuzzy bits: reading emails, judging deals, parsing one-line requests.

Every call returns structured JSON (output_config json_schema). Calls are capped per month
so a bug can never run up a bill. If no API key is set, callers fall back to rules.
"""
from __future__ import annotations

import json
import logging
from datetime import date

import anthropic

from . import db
from .config import settings

log = logging.getLogger(__name__)

# Server-side refusal fallback is only offered on these models.
_FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5-1"}

CATEGORIES = [
    "coffee", "groceries", "supplements", "clothes", "footwear", "homeware", "furniture", "diy",
    "electronics", "large_electronics", "travel", "restaurants", "takeaway", "subscriptions",
    "finance", "events", "f1", "gifts", "christmas", "fuel_car", "other",
]


class LLMUnavailable(Exception):
    pass


_client: anthropic.Anthropic | None = None


def available() -> bool:
    return bool(settings().anthropic_api_key)


def _client_() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings().anthropic_api_key, max_retries=3, timeout=120)
    return _client


def _budget_ok(conn) -> bool:
    key = f"llm_calls:{date.today():%Y-%m}"
    used = db.get_state(conn, key, 0) or 0
    if used >= settings().llm_monthly_call_cap:
        return False
    db.set_state(conn, key, used + 1)
    return True


def call_json(conn, system: str, user_content, schema: dict, max_tokens: int = 4000,
              effort: str = "low") -> dict:
    """One structured call. Raises LLMUnavailable when disabled, over budget or refused."""
    if not available():
        raise LLMUnavailable("no ANTHROPIC_API_KEY")
    if not _budget_ok(conn):
        raise LLMUnavailable("monthly LLM call cap reached")
    s = settings()
    kwargs: dict = dict(
        model=s.llm_model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_content}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    )
    if s.llm_model in _FALLBACK_MODELS:
        kwargs["extra_headers"] = {"anthropic-beta": "server-side-fallback-2026-07-01"}
        kwargs["extra_body"] = {"fallbacks": "default"}
    try:
        resp = _client_().messages.create(**kwargs)
    except anthropic.BadRequestError as e:
        raise LLMUnavailable(f"bad request: {e.message}") from e
    except anthropic.AuthenticationError as e:
        raise LLMUnavailable("invalid ANTHROPIC_API_KEY") from e
    except anthropic.RateLimitError as e:
        raise LLMUnavailable("rate limited") from e
    except anthropic.APIStatusError as e:
        raise LLMUnavailable(f"API error {e.status_code}") from e
    except anthropic.APIConnectionError as e:
        raise LLMUnavailable("network error") from e
    if resp.stop_reason == "refusal":
        raise LLMUnavailable("refused")
    if resp.stop_reason == "max_tokens":
        raise LLMUnavailable("output truncated")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return json.loads(text)


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props,
            "required": required if required is not None else list(props), "additionalProperties": False}


_NUM = {"type": ["number", "null"]}
_STR = {"type": ["string", "null"]}
_DATE = {"type": ["string", "null"], "description": "YYYY-MM-DD"}
_CAT = {"type": "string", "enum": CATEGORIES}

# ---------------------------------------------------------------- emails

EMAIL_SCHEMA = _obj({
    "kind": {"type": "string", "enum": [
        "purchase", "booking", "renewal", "subscription_change", "price_rise",
        "amex_offer", "retailer_offer", "refund", "other"]},
    "retailer": _STR,
    "order_date": _DATE,
    "total": _NUM,
    "currency": _STR,
    "items": {"type": "array", "items": _obj({
        "name": {"type": "string"}, "price": _NUM, "category": _CAT, "url": _STR})},
    "return_days": {"type": ["integer", "null"]},
    "booking": {"anyOf": [{"type": "null"}, _obj({
        "kind": {"type": "string", "enum": ["hotel", "flight", "car", "parking", "rail", "package", "event", "other"]},
        "name": _STR, "destination": _STR, "start_date": _DATE, "end_date": _DATE,
        "price": _NUM, "currency": _STR, "free_cancel_until": _DATE, "ref": _STR})]},
    "renewal": {"anyOf": [{"type": "null"}, _obj({
        "service": {"type": "string"}, "new_price": _NUM, "old_price": _NUM,
        "cycle": {"type": "string", "enum": ["monthly", "annual", "unknown"]}, "renewal_date": _DATE})]},
    "offer": {"anyOf": [{"type": "null"}, _obj({
        "summary": {"type": "string"}, "spend": _NUM, "credit": _NUM, "avios": {"type": ["integer", "null"]},
        "pct_off": _NUM, "expires": _DATE, "url": _STR, "category": _CAT})]},
})

EMAIL_SYSTEM = (
    "You read one email from a UK consumer's inbox and extract structured facts for a personal "
    "savings tracker. Only extract what the email states; use null when unsure. Dates as YYYY-MM-DD; "
    "infer the year from the email date when only day/month are given. Prices in the email's currency "
    "as plain numbers. 'purchase' = an order/receipt the person paid for. 'booking' = travel, hotel, "
    "parking, rail or event booking. 'renewal' = a contract/insurance/subscription renewal notice. "
    "'amex_offer' = an American Express offer (spend X get Y back, extra Avios). 'retailer_offer' = a "
    "marketing email with a specific strong offer. Marketing emails with nothing concrete are 'other'."
)


def extract_email(conn, subject: str, sender: str, sent: str, body: str) -> dict:
    content = f"From: {sender}\nDate: {sent}\nSubject: {subject}\n\n{body[:12000]}"
    return call_json(conn, EMAIL_SYSTEM, content, EMAIL_SCHEMA, max_tokens=3000)


# ---------------------------------------------------------------- deals

DEAL_SCHEMA = _obj({"deals": {"type": "array", "items": _obj({
    "i": {"type": "integer"},
    "retailer": _STR,
    "category": _CAT,
    "product": {"type": "string", "description": "short product name, no price"},
    "price": _NUM,
    "was_price": {"type": ["number", "null"], "description": "genuine usual price if stated or clearly implied"},
    "code": _STR,
    "cashback_gbp": _NUM,
    "amex_credit_gbp": _NUM,
    "avios": {"type": ["integer", "null"]},
    "expires": _DATE,
    "flags": {"type": "array", "items": {"type": "string", "enum": [
        "price_error", "freebie", "sample", "free_trial", "competition", "restock", "low_stock",
        "new_release", "seasonal", "stackable", "f1", "travel", "financial", "amex_offer", "avios",
        "clickbait", "spend_to_save", "low_value"]}},
    "financial_bonus_gbp": {"type": ["number", "null"], "description": "cash bonus for bank switch / card offer"},
    "competition_prize_gbp": _NUM,
    "one_line": {"type": "string", "description": "the offer in under 8 words"},
})}})

DEAL_SYSTEM = (
    "You normalise UK deal posts for a personal deal filter. For each numbered post return the retailer, "
    "category, prices in GBP, any code, cashback, Amex Offer credit, Avios, expiry and flags. Be sceptical: "
    "flag 'clickbait' for vague 'up to X% off' sales, 'spend_to_save' for offers that only work if you spend "
    "more than you otherwise would, and 'low_value' for trivial savings. Flag 'price_error' only when the "
    "price is implausibly low versus normal price (roughly 60%+ off a mainstream item). Use 'f1' for any "
    "Formula 1 tickets, presales, hospitality or F1 travel packages."
)


def classify_deals(conn, posts: list[dict]) -> list[dict]:
    lines = []
    for i, p in enumerate(posts):
        lines.append(f"[{i}] {p.get('title','')}\n{(p.get('summary') or '')[:600]}\nmerchant hint: "
                     f"{p.get('merchant') or '-'}; price hint: {p.get('price') or '-'}")
    out = call_json(conn, DEAL_SYSTEM, "\n\n".join(lines), DEAL_SCHEMA, max_tokens=6000)
    return out.get("deals", [])


# ---------------------------------------------------------------- planned purchases

PLANNED_SCHEMA = _obj({
    "query": {"type": "string", "description": "search phrase for deal sites"},
    "keywords": {"type": "array", "items": {"type": "string"},
                 "description": "2-5 lowercase words that must appear in a matching deal title"},
    "category": _CAT,
    "target_price": _NUM,
    "max_price": _NUM,
    "travel": {"anyOf": [{"type": "null"}, _obj({
        "origin": _STR, "destination": _STR, "depart_from": _DATE, "depart_to": _DATE,
        "nights": {"type": ["integer", "null"]}, "type": {"type": "string", "enum": ["flight", "hotel", "package", "car", "other"]}})]},
})

PLANNED_SYSTEM = (
    "The user typed one line describing something they plan to buy. Turn it into a watch rule. "
    "target_price = the price they said they'd happily pay (or null); max_price = an upper bound if given. "
    "Keywords must be specific enough to avoid unrelated deals (e.g. ['65', 'oled'] not ['tv']). "
    "Today is {today}. The user lives near Glasgow, UK and flies from GLA or EDI."
)


def parse_planned(conn, line: str) -> dict:
    return call_json(conn, PLANNED_SYSTEM.format(today=date.today()), line, PLANNED_SCHEMA, max_tokens=1500)


# ---------------------------------------------------------------- statements (PDF fallback)

STATEMENT_SCHEMA = _obj({"transactions": {"type": "array", "items": _obj({
    "date": {"type": "string", "description": "YYYY-MM-DD"},
    "description": {"type": "string"},
    "amount": {"type": "number", "description": "positive for spend, negative for credits/refunds/payments"},
})}})


def extract_statement_pdf(conn, pdf_b64: str) -> list[dict]:
    content = [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
        {"type": "text", "text": "List every card transaction on this American Express statement."},
    ]
    out = call_json(conn, "You extract transactions from UK credit card statements.", content,
                    STATEMENT_SCHEMA, max_tokens=16000)
    return out.get("transactions", [])
