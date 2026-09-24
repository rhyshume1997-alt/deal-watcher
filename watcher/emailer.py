"""Render and send the three email types: instant alert, daily digest, monthly summary."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import ROOT, settings
from .links import link

log = logging.getLogger(__name__)

_env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html"]))


def card_view(alert_row: dict) -> dict:
    """Alert DB row → the fields a card template needs."""
    p = alert_row.get("payload") or {}
    aid = alert_row["id"]
    return {
        "verdict": alert_row.get("verdict") or "BUY NOW",
        "headline": p.get("headline") or alert_row.get("retailer") or alert_row.get("title"),
        "offer_line": p.get("offer_line") or alert_row.get("title"),
        "reasons": p.get("reasons") or [],
        "history_note": p.get("history_note"),
        "urgency_note": p.get("urgency_note"),
        "cash_saving": alert_row.get("est_saving") or 0,
        "components": p.get("components") or {},
        "avios": p.get("avios"),
        "code": p.get("code"),
        "view_link": link(aid, "click", alert_row.get("url") or ""),
        "useful_link": link(aid, "useful"),
        "not_useful_link": link(aid, "not_useful"),
    }


def _text_version(cards: list[dict]) -> str:
    out = []
    for a in cards:
        lines = [a["verdict"], a["headline"], a["offer_line"], *a["reasons"][:3]]
        if a.get("history_note"):
            lines.append(a["history_note"])
        if a["cash_saving"]:
            lines.append(f"Cash saving: £{a['cash_saving']:.2f}")
        for k, v in a["components"].items():
            lines.append(f"{k}: £{v:.2f}")
        if a.get("avios"):
            lines.append(f"Avios: {a['avios']:,}")
        if a.get("urgency_note"):
            lines.append(a["urgency_note"])
        lines += [f"View offer: {a['view_link']}", f"Useful: {a['useful_link']}",
                  f"Not useful: {a['not_useful_link']}"]
        out.append("\n".join(x for x in lines if x))
    return "\n\n----\n\n".join(out)


def send(subject: str, html: str, text: str) -> bool:
    s = settings()
    if s.dry_run or not (s.gmail_address and s.gmail_app_password):
        log.warning("DRY RUN email: %s\n%s", subject, text[:2000])
        return s.dry_run  # pretend success only in explicit dry-run mode
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Deal watcher <{s.gmail_address}>"
    msg["To"] = s.recipient
    if s.watch_address:
        msg["Reply-To"] = s.watch_address  # replying adds a planned purchase
    msg["Message-ID"] = make_msgid(domain="deal-watcher")
    msg["X-Deal-Watcher"] = "1"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(s.gmail_address, s.gmail_app_password)
        smtp.send_message(msg)
    return True


def subject_for(card: dict) -> str:
    bits = [card["verdict"], card["headline"]]
    if card["offer_line"] and card["offer_line"] != card["headline"]:
        bits.append(card["offer_line"])
    return " · ".join(b for b in bits if b)[:140]


def send_instant(alert_row: dict) -> bool:
    c = card_view(alert_row)
    html = _env.get_template("instant.html").render(a=c)
    return send(subject_for(c), html, _text_version([c]))


def send_digest(alert_rows: list[dict], reminder_rows: list[dict], suppressed: int) -> bool:
    cards = [card_view(a) for a in alert_rows]
    rem = [card_view(a) for a in reminder_rows]
    n = len(cards) + len(rem)
    intro = f"{len(cards)} worth a look" + (f", {len(rem)} reminder{'s' if len(rem) != 1 else ''}" if rem else "")
    html = _env.get_template("digest.html").render(alerts=cards, reminders=rem, intro=intro, suppressed=suppressed)
    subject = f"Daily digest · {intro}"
    if cards:
        subject += f" · top: {cards[0]['headline']}"
    return n > 0 and send(subject[:140], html, _text_version(cards + rem))


def send_monthly(month: str, stats: dict) -> bool:
    html = _env.get_template("monthly.html").render(month=month, s=stats)
    text = (f"{month} summary\nEstimated saved: £{stats['saved']:.0f}\nActed on: {stats['acted']}\n"
            f"Alerts: {stats['instant']} instant / {stats['digest']} digest\nIgnored: {stats['ignored']}\n"
            f"Suppressed: {stats['suppressed']}\n" + "\n".join(stats.get("learning", [])))
    return send(f"Deal watcher · {month}: ~£{stats['saved']:.0f} saved", html, text)
