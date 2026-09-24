"""Database schema and helpers.

Works on Postgres (Supabase in production) and SQLite (local runs and tests).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON, Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, MetaData,
    String, Table, Text, UniqueConstraint, create_engine, insert, select, text, update,
)
from sqlalchemy.engine import Engine

metadata = MetaData()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


transactions = Table(
    "transactions", metadata,
    Column("id", Integer, primary_key=True),
    Column("date", Date, nullable=False, index=True),
    Column("description", Text, nullable=False),
    Column("retailer", String(120), index=True),
    Column("category", String(40), index=True),
    Column("amount", Float, nullable=False),  # positive = spend, negative = refund/credit
    Column("currency", String(3), default="GBP"),
    Column("source", String(20)),  # amex_csv | amex_pdf | gmail
    Column("ext_ref", String(200), unique=True),  # dedupe key
)

purchases = Table(
    "purchases", metadata,
    Column("id", Integer, primary_key=True),
    Column("date", Date, nullable=False),
    Column("retailer", String(120)),
    Column("item", Text),
    Column("category", String(40)),
    Column("price", Float),
    Column("url", Text),
    Column("product_key", String(200), index=True),
    Column("return_deadline", Date),
    Column("warranty_end", Date),
    Column("reminded", JSON, default=dict),  # {"return": "2026-10-01", ...}
    Column("source_ref", String(200), unique=True),
    Column("created_at", DateTime(timezone=True), default=utcnow),
)

subscriptions = Table(
    "subscriptions", metadata,
    Column("id", Integer, primary_key=True),
    Column("service", String(120), unique=True),
    Column("category", String(40), default="subscriptions"),
    Column("amount", Float),
    Column("previous_amount", Float),
    Column("cycle", String(20)),  # monthly | annual | unknown
    Column("next_renewal", Date),
    Column("notes", Text),
    Column("reminded_for", Date),
    Column("updated_at", DateTime(timezone=True), default=utcnow),
)

trips = Table(
    "trips", metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", String(20)),  # hotel | flight | car | parking | rail | package | event
    Column("provider", String(120)),
    Column("name", Text),
    Column("destination", String(120)),
    Column("start_date", Date, index=True),
    Column("end_date", Date),
    Column("price", Float),
    Column("currency", String(3)),
    Column("free_cancel_until", Date),
    Column("ref", String(120)),
    Column("url", Text),
    Column("reminded", JSON, default=dict),
    Column("source_ref", String(200), unique=True),
)

planned = Table(
    "planned", metadata,
    Column("id", Integer, primary_key=True),
    Column("raw", Text, nullable=False),
    Column("query", Text),
    Column("keywords", JSON, default=list),
    Column("category", String(40)),
    Column("target_price", Float),
    Column("max_price", Float),
    Column("url", Text),
    Column("product_key", String(200)),
    Column("details", JSON, default=dict),  # travel: origin/destination/dates etc.
    Column("status", String(20), default="active"),  # active | bought | cancelled
    Column("created_at", DateTime(timezone=True), default=utcnow),
)

products = Table(
    "products", metadata,
    Column("key", String(200), primary_key=True),  # e.g. "amazon:B0XXXX" or "url:<hash>"
    Column("title", Text),
    Column("url", Text),
    Column("retailer", String(120)),
    Column("category", String(40)),
    Column("last_price", Float),
    Column("in_stock", Boolean),
    Column("last_checked", DateTime(timezone=True)),
)

price_history = Table(
    "price_history", metadata,
    Column("id", Integer, primary_key=True),
    Column("product_key", String(200), index=True, nullable=False),
    Column("price", Float, nullable=False),
    Column("in_stock", Boolean),
    Column("observed_at", DateTime(timezone=True), default=utcnow, index=True),
)

offers = Table(
    "offers", metadata,
    Column("id", Integer, primary_key=True),
    Column("fingerprint", String(64), unique=True, nullable=False),
    Column("source", String(40)),
    Column("title", Text),
    Column("url", Text),
    Column("retailer", String(120)),
    Column("category", String(40)),
    Column("price", Float),
    Column("was_price", Float),
    Column("data", JSON, default=dict),  # full normalised Offer
    Column("seen_at", DateTime(timezone=True), default=utcnow, index=True),
)

alerts = Table(
    "alerts", metadata,
    Column("id", Integer, primary_key=True),
    Column("offer_id", Integer, ForeignKey("offers.id")),
    Column("kind", String(30)),  # deal | planned | post_purchase | renewal | trip | f1 | finance
    Column("tier", String(10), index=True),  # instant | digest | drop
    Column("verdict", String(10)),  # BUY NOW | WAIT | IGNORE
    Column("score", Float),
    Column("title", Text),
    Column("retailer", String(120)),
    Column("category", String(40)),
    Column("est_saving", Float),
    Column("url", Text),
    Column("payload", JSON, default=dict),
    Column("dedupe_key", String(200), index=True),
    Column("created_at", DateTime(timezone=True), default=utcnow, index=True),
    Column("sent_at", DateTime(timezone=True)),
    Column("clicked_at", DateTime(timezone=True)),
    Column("feedback", String(12)),  # useful | not_useful
    Column("acted", Boolean, default=False),  # a matching purchase followed
)

alert_events = Table(
    "alert_events", metadata,
    Column("id", Integer, primary_key=True),
    Column("alert_id", Integer, ForeignKey("alerts.id"), index=True),
    Column("event", String(20)),  # click | useful | not_useful
    Column("at", DateTime(timezone=True), default=utcnow),
)

cooldowns = Table(
    "cooldowns", metadata,
    Column("id", Integer, primary_key=True),
    Column("scope", String(20)),  # category | retailer | product
    Column("key", String(200)),
    Column("until", Date),
    Column("reason", Text),
    UniqueConstraint("scope", "key", name="uq_cooldown"),
)

learning = Table(
    "learning", metadata,
    Column("scope", String(20), primary_key=True),  # category | retailer | kind | source
    Column("key", String(200), primary_key=True),
    Column("useful", Integer, default=0),
    Column("not_useful", Integer, default=0),
    Column("clicks", Integer, default=0),
    Column("ignored", Integer, default=0),
)

state = Table(
    "state", metadata,
    Column("key", String(100), primary_key=True),
    Column("value", JSON),
)

ALL_TABLES = [t.name for t in metadata.sorted_tables]


def make_engine(url: str) -> Engine:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return create_engine(url, future=True, pool_pre_ping=True)


def setup(engine: Engine) -> None:
    metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        # Only the service role (the watcher and the tracking function) may touch the data.
        with engine.begin() as conn:
            for name in ALL_TABLES:
                conn.execute(text(f'ALTER TABLE "{name}" ENABLE ROW LEVEL SECURITY'))


# ---------- small helpers ----------

def get_state(conn, key: str, default: Any = None) -> Any:
    row = conn.execute(select(state.c.value).where(state.c.key == key)).first()
    return default if row is None else row[0]


def set_state(conn, key: str, value: Any) -> None:
    value = json.loads(json.dumps(value, default=str))
    if conn.execute(select(state.c.key).where(state.c.key == key)).first():
        conn.execute(update(state).where(state.c.key == key).values(value=value))
    else:
        conn.execute(insert(state).values(key=key, value=value))


def insert_ignore(conn, table: Table, values: dict, unique_col: str) -> int | None:
    """Insert unless a row with the same unique column exists. Returns new id or None."""
    col = table.c[unique_col]
    if conn.execute(select(col).where(col == values[unique_col])).first():
        return None
    res = conn.execute(insert(table).values(**values))
    pk = res.inserted_primary_key
    return pk[0] if pk else None
