"""Read your Gmail (IMAP + app password) for receipts, bookings, renewals, offers and wish-list lines.

Nothing is ever deleted, moved or marked read: the mailbox is opened read-only.
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
from datetime import date
from email.header import decode_header, make_header
from email.message import Message
from email.policy import default as default_policy
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from bs4 import BeautifulSoup

from .. import actions, db, llm
from ..config import retailers, settings
from ..models import Offer
from . import amex

log = logging.getLogger(__name__)

MAX_LLM_PER_TICK = 40

TRANSACTIONAL = ('(order OR receipt OR "booking" OR confirmed OR confirmation OR renewal OR renew OR '
                 'subscription OR "price change" OR "price increase" OR invoice OR refund OR dispatched OR '
                 'itinerary OR e-ticket OR reservation) -category:promotions -category:social')


def _retailer_domains() -> list[str]:
    return sorted({d for r in retailers() for d in r.get("domains", [])})


def _offer_query() -> str:
    doms = " OR ".join(["americanexpress.com", *_retailer_domains()])
    return f'from:({doms}) (offer OR "% off" OR sale OR cashback OR avios OR "extra points" OR voucher OR code)'


def _hdr(msg: Message, name: str) -> str:
    v = msg.get(name, "")
    try:
        return str(make_header(decode_header(v)))
    except Exception:
        return str(v)


def body_text(msg: Message) -> str:
    plain, html = [], []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        ctype = part.get_content_type()
        try:
            content = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", errors="replace")
        if ctype == "text/plain":
            plain.append(content)
        elif ctype == "text/html":
            html.append(content)
    if plain and (not html or sum(len(p) for p in plain) > 200):
        text = "\n".join(plain)
    else:
        soup = BeautifulSoup("\n".join(html), "html.parser")
        for t in soup(["style", "script", "head"]):
            t.decompose()
        text = soup.get_text("\n")
    text = re.sub(r"[ \t ‌͏]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(">"):
            continue
        if re.match(r"^On .+ wrote:$", line):
            break
        return line
    return ""


def _is_own(msg: Message) -> bool:
    return bool(msg.get("X-Deal-Watcher"))


def handle_message(conn, msg: Message, kind: str, uid: str, llm_left: list[int]) -> str:
    """Process one email. Returns a short description of what was done (for logs/tests)."""
    if _is_own(msg):
        return "own alert"
    s = settings()
    subject, sender = _hdr(msg, "Subject"), _hdr(msg, "From")
    to = " ".join(_hdr(msg, h) for h in ("To", "Delivered-To", "X-Original-To")).lower()
    sent = _hdr(msg, "Date")
    ref = f"gmail:{msg.get('Message-ID') or uid}"

    # 1. commands and files sent to you+watch@
    if kind == "watch" or (s.watch_address and s.watch_address.lower() in to):
        done = []
        for part in msg.walk():
            fn = part.get_filename()
            if fn and Path(fn).suffix.lower() in (".csv", ".pdf", ".xlsx", ".xls"):
                n = amex.import_file(conn, fn, part.get_payload(decode=True))
                done.append(f"imported {n} transactions from {fn}")
        if done:
            return "; ".join(done)
        text = first_line(body_text(msg)) or re.sub(r"^(re|fwd?):\s*", "", subject, flags=re.I)
        if subject.lower().startswith("fwd") and len(body_text(msg)) > 400:
            kind = "offer"  # a forwarded offer email: read it like any other
        elif text:
            res = actions.add_planned(conn, text)
            return f"planned: {res['action']} {res.get('raw')}"

    # 2. everything else goes through Claude (capped per run)
    if llm_left[0] <= 0 or not llm.available():
        raise _Defer()
    llm_left[0] -= 1
    info = llm.extract_email(conn, subject, sender, sent, body_text(msg))
    k = info.get("kind")
    if k == "purchase":
        return f"purchase: {actions.record_purchase(conn, info, ref)} items"
    if k == "booking":
        actions.record_booking(conn, info, ref)
        return "booking"
    if k in ("renewal", "price_rise", "subscription_change"):
        rise = actions.record_renewal(conn, info)
        if rise:
            from ..pipeline import queue_reminder
            queue_reminder(conn, "renewal", f"renewal:{rise['service']}:{rise.get('next_renewal')}",
                           headline=rise["service"],
                           offer_line=f"Price rising £{rise['previous_amount']:.2f} → £{rise['amount']:.2f}",
                           reasons=["Price rise – worth a quick call or a switch"],
                           urgency_note=f"Renews {rise['next_renewal']:%d %b}" if rise.get("next_renewal") else None,
                           category="subscriptions", instant=True)
        return "renewal"
    if k in ("amex_offer", "retailer_offer") and info.get("offer"):
        o = info["offer"]
        flags = ["amex_offer"] if k == "amex_offer" else []
        if o.get("avios"):
            flags.append("avios")
        offer = Offer(source="gmail", title=o["summary"], url=o.get("url") or "", retailer=info.get("retailer"),
                      category=o.get("category"), amex_credit_gbp=o.get("credit") if k == "amex_offer" else None,
                      avios=o.get("avios"), expires=actions._d(o.get("expires")), flags=flags,
                      price=o.get("spend"), was_price=None, one_line=o["summary"], enriched=True, source_id=ref)
        if o.get("pct_off") and o.get("spend"):
            offer.was_price = o["spend"]
            offer.price = round(o["spend"] * (1 - o["pct_off"] / 100), 2)
        from ..pipeline import ingest_offers
        ingest_offers(conn, [offer])
        return f"offer: {o['summary']}"
    return f"ignored ({k})"


class _Defer(Exception):
    """Out of LLM budget for this run – leave the message for next time."""


def _search(imap, criteria: str, since_uid: int) -> list[int]:
    quoted = '"' + criteria.replace("\\", "\\\\").replace('"', '\\"') + '"'
    typ, data = imap.uid("SEARCH", "CHARSET", "UTF-8", f"UID {since_uid + 1}:*", "X-GM-RAW", quoted.encode())
    if typ != "OK" or not data or not data[0]:
        return []
    return [int(x) for x in data[0].split() if int(x) > since_uid]


def sync(conn) -> dict:
    s = settings()
    if not (s.gmail_address and s.gmail_app_password):
        return {"skipped": "no Gmail credentials"}
    imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    imap.login(s.gmail_address, s.gmail_app_password)
    try:
        imap.select('"[Gmail]/All Mail"', readonly=True)
        uidvalidity = imap.response("UIDVALIDITY")[1][0]
        uidvalidity = int(uidvalidity) if uidvalidity else 0
        st = db.get_state(conn, "gmail") or {}
        if st.get("uidvalidity") != uidvalidity:
            st = {"uidvalidity": uidvalidity, "last_uid": 0}
        last = st["last_uid"]
        window = f"newer_than:{s.gmail_backfill_days}d" if last == 0 else "newer_than:14d"
        todo: dict[int, str] = {}
        if s.watch_address:
            for u in _search(imap, f"to:{s.watch_address} {window}", last):
                todo[u] = "watch"
        for u in _search(imap, f"{TRANSACTIONAL} {window}", last):
            todo.setdefault(u, "transactional")
        for u in _search(imap, f"{_offer_query()} newer_than:14d", last):
            todo.setdefault(u, "offer")
        results = {"seen": len(todo), "done": 0, "deferred": 0}
        llm_left = [MAX_LLM_PER_TICK]
        for uid in sorted(todo):
            typ, data = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                st["last_uid"] = uid
                continue
            msg = email.message_from_bytes(data[0][1], policy=default_policy)
            try:
                what = handle_message(conn, msg, todo[uid], str(uid), llm_left)
                log.info("gmail %s [%s] %s → %s", uid, todo[uid], _hdr(msg, "Subject")[:60], what)
                results["done"] += 1
            except _Defer:
                results["deferred"] = len([u for u in todo if u >= uid])
                break  # keep last_uid so this and later messages are retried next run
            except llm.LLMUnavailable as e:
                log.warning("gmail %s skipped: %s", uid, e)
            except Exception:
                log.exception("gmail %s failed; skipping", uid)
            st["last_uid"] = uid
            db.set_state(conn, "gmail", st)
        if not todo:
            # nothing matched; still move the cursor to the newest UID so we don't rescan
            typ, data = imap.status('"[Gmail]/All Mail"', "(UIDNEXT)")
            m = re.search(rb"UIDNEXT (\d+)", data[0]) if typ == "OK" and data else None
            if m:
                st["last_uid"] = max(st["last_uid"], int(m.group(1)) - 1)
        db.set_state(conn, "gmail", st)
        return results
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def email_date(msg: Message) -> date | None:
    try:
        return parsedate_to_datetime(msg.get("Date")).date()
    except Exception:
        return None


__all__ = ["sync", "handle_message", "body_text", "first_line", "parseaddr", "email_date"]
