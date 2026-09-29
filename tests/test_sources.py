from email.message import EmailMessage
from types import SimpleNamespace

from sqlalchemy import select

from watcher import config, db, llm
from watcher.ingest import gmail
from watcher.sources import feeds

F1 = {"keywords_all": [["ticket", "tickets", "hospitality"], ["on sale", "presale", "sold out"]]}


def test_keywords_all_needs_every_group():
    assert feeds.keep_post("British GP tickets go on sale Friday", F1)
    assert not feeds.keep_post("Norris wins British GP", F1)          # no ticket word
    assert not feeds.keep_post("How to get tickets to Silverstone", F1)  # no sale event
    assert feeds.keep_post("anything", {})


def test_find_link_follows_matching_race(monkeypatch):
    html = '<a href="/en/f1-111-monaco">Monaco</a><a href="/en/f1-222-great-britain">Great Britain 2027</a>'
    monkeypatch.setattr(feeds.http, "get", lambda url: SimpleNamespace(text=html, url="https://tickets.formula1.com/en"))
    cfg = {"listing_url": "https://tickets.formula1.com/en", "find_link": ["great britain"]}
    assert feeds.resolve_page(cfg) == "https://tickets.formula1.com/en/f1-222-great-britain"


def test_find_link_missing_raises(monkeypatch):
    monkeypatch.setattr(feeds.http, "get", lambda url: SimpleNamespace(text="<a href='/x'>Spa</a>", url=url))
    try:
        feeds.resolve_page({"listing_url": "https://a.b/", "find_link": ["great britain"]})
    except LookupError:
        return
    raise AssertionError("expected LookupError")


def test_empty_env_uses_default(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "")
    assert config.Settings().llm_model == "claude-opus-5"


def test_newsletter_email_becomes_scored_offers(conn, sent, monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "extract_newsletter", lambda c, s, b: [{
        "title": "Nationwide £175 switch bonus", "url": "https://x/nw", "retailer": "Nationwide",
        "category": "finance", "product": None, "price": None, "was_price": None, "code": None,
        "cashback_gbp": None, "amex_credit_gbp": None, "avios": None, "expires": None,
        "flags": ["financial"], "financial_bonus_gbp": 175, "competition_prize_gbp": None,
        "one_line": "£175 to switch bank"}])
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "MSE <weekly@moneysavingexpert.com>", "me@gmail.com", "MSE weekly"
    m.set_content("Deals of the week ...")
    out = gmail.handle_message(conn, m, "newsletter", "9", [5])
    assert out.startswith("newsletter:") and "digest" in out
    row = conn.execute(select(db.alerts).where(db.alerts.c.tier == "digest")).mappings().first()
    assert row["retailer"] == "Nationwide"


def test_find_link_matches_address_when_card_has_no_text(monkeypatch):
    html = '<a href="/en/f1-111-monaco"><img alt=""></a><a href="/en/f1-3310-great-britain"><img alt=""></a>'
    monkeypatch.setattr(feeds.http, "get", lambda url: SimpleNamespace(text=html, url="https://tickets.formula1.com/en"))
    cfg = {"listing_url": "https://tickets.formula1.com/en", "find_link": ["great britain"]}
    assert feeds.resolve_page(cfg) == "https://tickets.formula1.com/en/f1-3310-great-britain"


def test_workspace_header_sent_when_configured(monkeypatch):
    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.setattr(llm, "settings", lambda: SimpleNamespace(anthropic_api_key="k", anthropic_workspace_id="wrkspc_1"))
    c = llm._client_()
    assert c._custom_headers.get("anthropic-workspace-id") == "wrkspc_1"
    monkeypatch.setattr(llm, "_client", None)
