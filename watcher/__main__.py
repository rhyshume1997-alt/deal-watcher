"""Command line.

  python -m watcher setup-db               create tables
  python -m watcher tick                   do everything that's due (the scheduler runs this)
  python -m watcher import FILE [FILE…]    import Amex statements (CSV/PDF/XLSX)
  python -m watcher profile                show spending analysis
  python -m watcher add "65in OLED TV under £1100"
  python -m watcher list                   show what's being watched
  python -m watcher doctor                 check every source and credential
  python -m watcher digest --force         send the digest now
  python -m watcher monthly --force        send last month's summary now
  python -m watcher test-email             send a sample alert
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from sqlalchemy import select

from . import db


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="watcher")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup-db")
    sub.add_parser("tick")
    imp = sub.add_parser("import")
    imp.add_argument("files", nargs="+")
    sub.add_parser("profile")
    add = sub.add_parser("add")
    add.add_argument("line", nargs="+")
    sub.add_parser("list")
    sub.add_parser("doctor")
    for name in ("digest", "monthly"):
        p = sub.add_parser(name)
        p.add_argument("--force", action="store_true")
    sub.add_parser("test-email")
    src = sub.add_parser("source")
    src.add_argument("id")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    from .config import settings

    engine = db.make_engine(settings().database_url)
    if args.cmd == "setup-db":
        db.setup(engine)
        print("tables ready")
        return 0
    db.metadata.create_all(engine)  # harmless if they exist

    from . import actions, pipeline, profile

    if args.cmd == "tick":
        print(json.dumps(pipeline.tick(engine), default=str, indent=1))
    elif args.cmd == "import":
        from .ingest import amex

        with engine.begin() as conn:
            for f in args.files:
                print(f"{f}: {amex.import_file(conn, f)} new transactions")
            profile.sync_subscriptions(conn)
        with engine.begin() as conn:
            print(profile.report(conn))
    elif args.cmd == "profile":
        with engine.begin() as conn:
            print(profile.report(conn))
    elif args.cmd == "add":
        with engine.begin() as conn:
            print(json.dumps(actions.add_planned(conn, " ".join(args.line)), default=str, indent=1))
    elif args.cmd == "list":
        with engine.begin() as conn:
            for p in conn.execute(select(db.planned).where(db.planned.c.status == "active")).mappings():
                tp = f" target £{p['target_price']:,.0f}" if p["target_price"] else ""
                print(f"#{p['id']} {p['raw']}{tp}  keywords={p['keywords']}")
    elif args.cmd == "doctor":
        return doctor(engine)
    elif args.cmd == "digest":
        with engine.begin() as conn:
            print(pipeline.run_digest(conn, force=args.force))
    elif args.cmd == "monthly":
        with engine.begin() as conn:
            print(pipeline.run_monthly(conn, force=args.force))
    elif args.cmd == "test-email":
        from . import emailer

        ok = emailer.send_instant({
            "id": 0, "verdict": "BUY NOW", "retailer": "Nespresso", "title": "test", "url": "https://www.nespresso.com/uk/en/",
            "est_saving": 14.0, "payload": {
                "headline": "Nespresso", "offer_line": "20% off capsules + 3x Avios",
                "reasons": ["You buy coffee regularly", "Stacks: Retailer discount + Amex credit + Avios"],
                "history_note": "Lowest price seen recently", "urgency_note": "Ends Sunday",
                "components": {"Retailer discount": 9.0, "Amex credit": 5.0}, "avios": 450}})
        print("sent" if ok else "not sent (check GMAIL_ADDRESS / GMAIL_APP_PASSWORD)")
    elif args.cmd == "source":
        with engine.begin() as conn:
            print(json.dumps(pipeline.run_sources(conn, only=args.id), default=str, indent=1))
    return 0


def doctor(engine) -> int:
    """Check credentials and every source. Safe to run any time; sends nothing."""
    from .config import settings, sources
    from .sources import feeds, http

    s = settings()
    ok = True

    def line(status: bool, name: str, detail: str = ""):
        nonlocal ok
        ok &= status
        print(f"{'OK  ' if status else 'FAIL'} {name} {detail}")

    line(True, "database", engine.dialect.name)
    line(bool(s.gmail_address and s.gmail_app_password), "gmail credentials", s.gmail_address or "(missing)")
    if s.gmail_address and s.gmail_app_password:
        import imaplib

        try:
            m = imaplib.IMAP4_SSL("imap.gmail.com", 993)
            m.login(s.gmail_address, s.gmail_app_password)
            m.logout()
            line(True, "gmail login")
        except Exception as e:
            line(False, "gmail login", str(e)[:120])
    line(bool(s.anthropic_api_key), "anthropic key", f"model {s.llm_model}")
    line(bool(s.track_base_url), "tracking url", s.track_base_url or "(links will go straight to offers; no feedback)")
    print("keepa key", "set" if s.keepa_api_key else "not set (Amazon prices use page data instead)")
    cfg = sources()
    for kind in ("deal_feed", "blog_feed"):
        for c in cfg.get(kind, []):
            try:
                url, feed = feeds.fetch_feed(c["urls"])
                line(True, c["id"], f"{len(feed.entries)} items via {url}")
            except Exception as e:
                line(False, c["id"], str(e)[:150])
    for c in cfg.get("news_query", []):
        try:
            n = len(feeds.news_query(c))
            line(True, c["id"], f"{n} recent matches")
        except Exception as e:
            line(False, c["id"], str(e)[:150])
    for c in cfg.get("page_watch", []):
        try:
            http.get(c["url"])
            line(True, c["id"])
        except Exception as e:
            line(False, c["id"], str(e)[:150])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
