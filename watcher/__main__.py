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
    if not settings().link_secret:
        with engine.begin() as conn:
            settings().link_secret = db.load_link_secret(conn)

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
    """Check credentials and every source. Safe to run any time; sends nothing.

    Exits 1 only when something required is broken (database, Gmail, Claude). A deal source
    failing is a warning: the watcher keeps running on the others.
    """
    from . import llm
    from .config import settings, sources
    from .sources import feeds, http

    s = settings()
    required_ok = True

    def line(status: str, name: str, detail: str = ""):
        print(f"{status:<4} {name} {detail}".rstrip())

    def need(ok: bool, name: str, detail: str = ""):
        nonlocal required_ok
        required_ok &= ok
        line("OK" if ok else "FAIL", name, detail)

    def optional(ok: bool, name: str, detail: str = ""):
        line("OK" if ok else "WARN", name, detail)

    print("Required")
    need(True, "database", engine.dialect.name)
    need(bool(s.gmail_address and s.gmail_app_password), "gmail credentials", s.gmail_address or "(missing)")
    imap = None
    if s.gmail_address and s.gmail_app_password:
        import imaplib

        try:
            imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
            imap.login(s.gmail_address, s.gmail_app_password)
            need(True, "gmail login")
        except Exception as e:
            imap = None
            need(False, "gmail login", str(e)[:120])
    if s.anthropic_api_key:
        try:
            with engine.begin() as conn:
                llm.ping(conn)
            need(True, "claude", f"model {s.llm_model} answered")
        except llm.LLMUnavailable as e:
            need(False, "claude", f"model {s.llm_model}: {e}")
    else:
        need(False, "claude", "ANTHROPIC_API_KEY missing")
    need(bool(s.track_base_url), "tracking url", s.track_base_url or "(missing)")

    print("\nOptional (a failure here only means fewer deals)")
    optional(bool(s.keepa_api_key), "keepa", "set" if s.keepa_api_key else "not set: Amazon prices come from page data")
    cfg = sources()
    for kind in ("deal_feed", "blog_feed"):
        for c in cfg.get(kind, []):
            if c.get("enabled") is False:
                line("OFF", c["id"])
                continue
            try:
                url, feed = feeds.fetch_feed(c["urls"])
                optional(True, c["id"], f"{len(feed.entries)} items via {url}")
            except Exception as e:
                optional(False, c["id"], str(e).splitlines()[0][:150])
    for c in cfg.get("page_watch", []):
        if c.get("enabled") is False:
            line("OFF", c["id"])
            continue
        try:
            url = feeds.resolve_page(c)
            http.get(url)
            optional(True, c["id"], url)
        except Exception as e:
            optional(False, c["id"], str(e).splitlines()[0][:150])
    for c in cfg.get("newsletter", []):
        if imap is None:
            optional(False, c["id"], "can't check without Gmail")
            continue
        try:
            imap.select('"[Gmail]/All Mail"', readonly=True)
            senders = " OR ".join(c["senders"])
            typ, data = imap.uid("SEARCH", "X-GM-RAW", f'"from:({senders}) newer_than:14d"')
            n = len(data[0].split()) if typ == "OK" and data and data[0] else 0
            optional(n > 0, c["id"], f"{n} emails in the last 14 days" if n
                     else f"no emails from {', '.join(c['senders'])} yet: subscribe to get these deals")
        except Exception as e:
            optional(False, c["id"], str(e)[:150])
    if imap is not None:
        try:
            imap.logout()
        except Exception:
            pass
    print("\n" + ("All required checks passed." if required_ok else "A required check failed - see FAIL above."))
    return 0 if required_ok else 1


if __name__ == "__main__":
    sys.exit(main())
