from datetime import date, timedelta

from sqlalchemy import insert

from watcher import db
from watcher.models import Offer
from watcher.profile import SpendProfile
from watcher.scoring import evaluate
from watcher.sources.products import History

TODAY = date(2026, 9, 24)


def prof(**by_retailer):
    p = SpendProfile(total=5000, months=12)
    p.by_retailer = {k.replace("_", " "): v for k, v in by_retailer.items()}
    p.by_category = {"coffee": 600, "diy": 1500, "clothes": 400}
    return p


def test_strong_relevant_deal_passes(conn):
    o = Offer(source="hukd_hot", title="Nespresso Vertuo capsules", url="u", retailer="Nespresso",
              price=40, was_price=55, avios=450, amex_credit_gbp=5)
    d = evaluate(conn, o, prof(Nespresso=600), today=TODAY)
    assert d.tier in ("instant", "digest") and d.verdict == "BUY NOW"
    assert d.components == {"Retailer discount": 15, "Amex credit": 5}
    assert any("Nespresso" in r for r in d.reasons)


def test_small_discount_dropped(conn):
    o = Offer(source="hukd_hot", title="Kettle", url="u", retailer="Argos", price=27, was_price=30)
    assert evaluate(conn, o, prof(), today=TODAY).tier == "drop"


def test_junk_dropped(conn):
    o = Offer(source="hukd_hot", title="Up to 70% off", url="u", retailer="ASOS", flags=["clickbait"])
    assert evaluate(conn, o, prof(), today=TODAY).drop_reason.startswith("junk")


def test_fake_discount_detected(conn):
    o = Offer(source="hukd_hot", title="Drill", url="u", retailer="B&Q", price=99, was_price=180)
    h = History(n=20, median90=100, min180=95)
    d = evaluate(conn, o, prof(B_Q=1500), today=TODAY, hist=h)
    assert d.tier == "drop" and d.drop_reason == "fake discount"


def test_lowest_in_6_months(conn):
    o = Offer(source="tracker", title="Drill", url="u", retailer="B&Q", price=70)
    h = History(n=20, median90=100, min180=72)
    d = evaluate(conn, o, prof(B_Q=1500), today=TODAY, hist=h)
    assert d.history_note == "Lowest price in 6 months" and d.tier != "drop"
    assert o.was_price == 100  # discount measured against the real usual price


def test_f1_always_instant(conn):
    o = Offer(source="f1_british_gp", title="British GP tickets presale opens", url="u", flags=["f1"], category="f1")
    d = evaluate(conn, o, SpendProfile(), today=TODAY)
    assert d.tier == "instant"


def test_planned_target_hit(conn):
    conn.execute(insert(db.planned).values(raw="65in OLED TV under £1100", keywords=["65", "oled"],
                                           target_price=1100, category="large_electronics", status="active"))
    o = Offer(source="hukd_hot", title="LG 65 inch OLED C5 £999 @ Currys", url="u", retailer="Currys",
              price=999, was_price=1299)
    d = evaluate(conn, o, prof(), today=TODAY)
    assert d.planned_hit and d.tier == "instant"


def test_cooldown_after_purchase(conn):
    conn.execute(insert(db.cooldowns).values(scope="category", key="clothes", until=TODAY + timedelta(days=10),
                                             reason="you bought a jacket"))
    o = Offer(source="hukd_hot", title="Jeans", url="u", retailer="Zara", price=30, was_price=45)
    d = evaluate(conn, o, prof(Zara=400), today=TODAY)
    assert d.tier == "drop" and d.drop_reason.startswith("cooldown")


def test_wait_before_black_friday(conn):
    o = Offer(source="hukd_hot", title="Headphones", url="u", retailer="Currys", category="electronics",
              price=150, was_price=200)
    d = evaluate(conn, o, prof(Currys=900), today=date(2026, 11, 5))
    assert d.verdict == "WAIT" and d.tier == "digest"


def test_expired_dropped(conn):
    o = Offer(source="gmail", title="x", url="u", retailer="M&S", price=10, was_price=20, expires=TODAY - timedelta(days=1))
    assert evaluate(conn, o, prof(), today=TODAY).drop_reason == "expired"


def test_feedback_lowers_score(conn):
    o1 = Offer(source="hukd_hot", title="Hoodie", url="u", retailer="TALA", price=30, was_price=50)
    before = evaluate(conn, o1, prof(TALA=300), today=TODAY).score
    conn.execute(insert(db.learning).values(scope="retailer", key="TALA", useful=0, not_useful=4, clicks=0, ignored=0))
    o2 = Offer(source="hukd_hot", title="Hoodie", url="u", retailer="TALA", price=30, was_price=50)
    after = evaluate(conn, o2, prof(TALA=300), today=TODAY).score
    assert after < before
