from datetime import date, timedelta
from email.message import EmailMessage
from pathlib import Path

from sqlalchemy import insert, select

from watcher import db, emailer, pipeline
from watcher.ingest import gmail
from watcher.models import Offer
from watcher.sources.feeds import parse_deal_title
from watcher.sources.products import parse_product_html

FIX = Path(__file__).parent / "fixtures"


def test_parse_deal_title():
    p = parse_deal_title("Nespresso Vertuo Pop £49.99 @ Amazon")
    assert p == {"merchant": "Amazon", "price": 49.99, "product": "Nespresso Vertuo Pop"}
    assert parse_deal_title("Free Lindt sample @ Tesco")["price"] == 0.0


def test_json_ld_price():
    html = """<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"Product",
    "name":"Sofa","offers":{"@type":"Offer","price":"499.00","availability":"https://schema.org/InStock"}}]}</script>"""
    assert parse_product_html(html) == ("Sofa", 499.0, True)


def test_ingest_sends_instant_and_queues_digest(conn, sent):
    offers = [
        Offer(source="f1_british_gp", title="Silverstone 2027 tickets on general sale", url="https://x/f1",
              category="f1", flags=["f1"], enriched=True),
        Offer(source="hukd_hot", title="Nespresso pods", url="https://x/n", retailer="Nespresso", price=30,
              was_price=40, enriched=True),
        Offer(source="hukd_hot", title="Random gadget", url="https://x/r", retailer="Unknown Shop", price=9,
              was_price=10, enriched=True),
    ]
    stats = pipeline.ingest_offers(conn, offers, today=date(2026, 9, 24))
    assert stats.get("instant") == 1 and stats.get("drop") == 1
    assert len(sent) == 1 and sent[0][0].startswith("BUY NOW")
    assert "functions/v1/track?a=" in sent[0][1]  # tracked links
    # same offers again → deduped
    assert pipeline.ingest_offers(conn, offers, today=date(2026, 9, 24)) == {"dupe": 3}
    res = pipeline.run_digest(conn, force=True)
    assert res["sent"] and len(sent) == 2 and "Nespresso" in sent[1][1]
    # digest items are marked sent
    assert pipeline.run_digest(conn, force=True)["deals"] == 0


def test_watch_email_adds_planned(conn):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "me@gmail.com", "me+watch@gmail.com", "x"
    m.set_content("Garden furniture set under £400\n\n> quoted reply")
    out = gmail.handle_message(conn, m, "watch", "1", [0])
    assert out.startswith("planned: added")
    p = conn.execute(select(db.planned)).mappings().first()
    assert p["target_price"] == 400 and "garden" in p["keywords"]


def test_watch_email_with_statement_attachment(conn):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "me@gmail.com", "me+watch@gmail.com", "statement"
    m.set_content("here")
    m.add_attachment((FIX / "amex_modern.csv").read_bytes(), maintype="text", subtype="csv", filename="activity.csv")
    assert "imported 8 transactions" in gmail.handle_message(conn, m, "watch", "2", [0])


def test_stop_command(conn):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "me@gmail.com", "me+watch@gmail.com", "x"
    m.set_content("new sofa under £800")
    gmail.handle_message(conn, m, "watch", "1", [0])
    m2 = EmailMessage()
    m2["From"], m2["To"], m2["Subject"] = "me@gmail.com", "me+watch@gmail.com", "x"
    m2.set_content("bought sofa")
    assert "stopped" in gmail.handle_message(conn, m2, "watch", "2", [0])


def test_post_purchase_reminders(conn, sent):
    today = date(2026, 9, 24)
    conn.execute(insert(db.purchases).values(date=today - timedelta(days=27), retailer="Currys", item="Soundbar",
                                             category="electronics", price=300, return_deadline=today + timedelta(days=2),
                                             warranty_end=today + timedelta(days=338), reminded={}, source_ref="p1"))
    conn.execute(insert(db.trips).values(kind="hotel", name="Hotel Resol Trinity Kyoto", destination="Kyoto",
                                         start_date=date(2027, 4, 13), free_cancel_until=today + timedelta(days=1),
                                         reminded={}, source_ref="t1"))
    res = pipeline.run_post_purchase(conn, today=today)
    assert res == {"return": 1, "trip": 1}
    assert len(sent) == 1 and "Free cancellation" in sent[0][0] + sent[0][2]  # trip is instant, return goes to digest


def test_feedback_folds_into_learning(conn):
    aid = conn.execute(insert(db.alerts).values(kind="deal", tier="digest", verdict="BUY NOW", title="t",
                                                retailer="ASOS", category="clothes", payload={})).inserted_primary_key[0]
    conn.execute(insert(db.alert_events).values(alert_id=aid, event="not_useful"))
    assert pipeline.apply_feedback(conn) == 1
    row = conn.execute(select(db.learning).where(db.learning.c.key == "ASOS")).mappings().first()
    assert row["not_useful"] == 1
    assert pipeline.apply_feedback(conn) == 0


def test_monthly_summary(conn, sent):
    conn.execute(insert(db.alerts).values(kind="deal", tier="instant", verdict="BUY NOW", title="t", retailer="Nespresso",
                                          category="coffee", est_saving=14, payload={"avios": 450}, acted=True,
                                          created_at=db.utcnow(), sent_at=db.utcnow(), clicked_at=db.utcnow()))
    today = date.today()
    stats = pipeline.monthly_stats(conn, today.replace(day=1), today + timedelta(days=1))
    assert stats["saved"] == 14 and stats["acted"] == 1 and stats["avios"] == 450
    assert emailer.send_monthly("Test", stats)
