from datetime import date
from pathlib import Path

from sqlalchemy import select

from watcher import db, profile
from watcher.ingest import amex
from watcher.merchants import categorise

FIX = Path(__file__).parent / "fixtures"


def test_modern_csv_parses_and_categorises(conn):
    n = amex.import_file(conn, FIX / "amex_modern.csv")
    assert n == 8  # payment line skipped
    rows = {r["description"]: r for r in conn.execute(select(db.transactions)).mappings()}
    assert rows["NESPRESSO UK LONDON"]["retailer"] == "Nespresso"
    assert rows["BANDQ UK"]["category"] == "diy"
    assert rows["DISHOOM GLASGOW"]["category"] == "restaurants"
    assert rows["AMAZON.CO.UK*AB12C LUXEMBOURG"]["amount"] == -19.99
    # re-import is idempotent
    assert amex.import_file(conn, FIX / "amex_modern.csv") == 0


def test_legacy_csv(conn):
    assert amex.import_file(conn, FIX / "amex_legacy.csv") == 2
    r = conn.execute(select(db.transactions).where(db.transactions.c.retailer == "Myprotein")).mappings().first()
    assert r["category"] == "supplements" and r["date"] == date(2026, 9, 4)


def test_pdf_text_lines():
    text = """Statement Date 12/01/2026
Dec 28 Dec 29 TESCO STORES 3175 GLASGOW 23.10
Jan 03 Jan 04 AMAZON.CO.UK*AB12C AMAZON.CO.UK 12.99
Jan 05 Jan 06 REFUND ASOS 30.00 CR"""
    rows = amex.parse_pdf_text(text)
    assert [r["amount"] for r in rows] == [23.10, 12.99, -30.0]
    assert rows[0]["date"] == date(2025, 12, 28)  # previous year on a January statement


def test_short_aliases_need_word_boundaries():
    assert categorise("THG NUTRITION")[0] == "Myprotein"
    assert categorise("SMITHGATE PHARMACY")[0] is None
    assert categorise("B&M RETAIL LTD")[0] == "B&M"


def test_profile_and_recurring(conn):
    amex.import_file(conn, FIX / "amex_modern.csv")
    p = profile.build(conn, today=date(2026, 9, 24))
    assert p.retailer_affinity("B&Q") > p.retailer_affinity("Spotify") > 0
    rec = {r["service"]: r for r in profile.detect_recurring(conn, today=date(2026, 9, 24))}
    assert rec["Spotify"]["cycle"] == "monthly"
    assert "Spending" not in profile.report(conn) and "diy" in profile.report(conn)


def test_amex_uk_statement_layout():
    """Mirrors the real Amex UK PDF text: no space in dates, CR on the next or a continuation line."""
    text = """Prepared for Membership Number Date
A PERSON xxxx-xxxxxx-12345 28/01/26
Transaction Process
Date Date Transaction Details Foreign Spend Amount £
Jan11 Jan11 PAYMENT RECEIVED - THANK YOU 13.00
CR
Dec29 Dec30 SHOP ONE GLASGOW 7.00
GOODS CR
Jan10 Jan11 NAME-CHEAP.COM* FIXBMS PHOENIX 8.81
11.48
UNITED STATES DOLLAR
Jan12 Jan12 B&Q DARNLEY 1246 DARNLEY 37.00
DRN698 - Darnley
Jan28 Jan28 MEMBERSHIP FEE 300.00
Total of other account transactions 200.00
CR"""
    rows = amex.parse_pdf_text(text)
    got = [(r["date"].isoformat(), r["amount"]) for r in rows]
    assert got == [("2026-01-11", -13.0), ("2025-12-29", -7.0), ("2026-01-10", 8.81),
                   ("2026-01-12", 37.0), ("2026-01-28", 300.0)]
