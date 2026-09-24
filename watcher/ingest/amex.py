"""Import American Express statements (CSV export preferred, PDF supported)."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from .. import db, llm
from ..merchants import categorise

log = logging.getLogger(__name__)

_DATE_FORMATS = ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d %b %Y", "%d %B %Y")


def _parse_date(s: str) -> date | None:
    s = (s or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(s: str) -> float | None:
    s = (s or "").replace("£", "").replace(",", "").strip()
    credit = s.upper().endswith("CR")
    s = s.rstrip("CRcr ").strip()
    try:
        v = float(s)
    except ValueError:
        return None
    return -abs(v) if credit else v


def parse_csv(text: str) -> list[dict]:
    """Handles the current Amex UK export (with header) and the older headerless 4-column one."""
    text = text.lstrip("﻿")
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return []
    header = [c.strip().lower() for c in rows[0]]
    out = []
    if "date" in header and "amount" in header:
        ix = {name: header.index(name) for name in header}

        def col(r, *names):
            for n in names:
                if n in ix and ix[n] < len(r):
                    return r[ix[n]]
            return ""

        for r in rows[1:]:
            d = _parse_date(col(r, "date"))
            amt = _parse_amount(col(r, "amount"))
            if d is None or amt is None:
                continue
            desc = col(r, "description", "appears on your statement as")
            out.append({"date": d, "description": desc.strip(), "amount": amt,
                        "amex_category": col(r, "category"),
                        "ref": col(r, "reference").strip("' ")})
    else:
        # legacy: Date, Reference, Amount, Description[, ...]
        for r in rows:
            if len(r) < 4:
                continue
            d, amt = _parse_date(r[0]), _parse_amount(r[2])
            if d is None or amt is None:
                continue
            out.append({"date": d, "description": r[3].strip(), "amount": amt,
                        "amex_category": "", "ref": r[1].strip()})
    return out


_PDF_LINE = re.compile(
    r"^(?P<d1>[A-Z][a-z]{2} ?\d{1,2})\s+(?:(?P<d2>[A-Z][a-z]{2} ?\d{1,2})\s+)?(?P<desc>.+?)\s+"
    r"(?P<amt>-?[\d,]+\.\d{2})(?P<cr>\s*CR)?$")


def _statement_date(text: str) -> date | None:
    m = re.search(r"Statement (?:Date|date)\D{0,20}(\d{1,2}[ /.][A-Za-z0-9]{2,9}[ /.]\d{2,4})", text)
    if m:
        return _parse_date(m.group(1).replace(".", "/"))
    # Amex UK: "Prepared for Membership Number Date / NAME xxxx-xxxxxx-11000 28/04/26"
    m = re.search(r"x{4}-x{6}-\d+\s+(\d{2}/\d{2}/\d{2,4})", text)
    return _parse_date(m.group(1)) if m else None


def parse_pdf_text(text: str) -> list[dict]:
    """Regex pass over Amex UK statement text. Returns [] if the layout isn't recognised."""
    sd = _statement_date(text)
    year = sd.year if sd else date.today().year
    lines = [ln.strip() for ln in text.splitlines()]
    out = []
    for i, line in enumerate(lines):
        mm = _PDF_LINE.match(line)
        if not mm:
            continue
        try:
            d = datetime.strptime(f"{mm['d1'].replace(' ', '')} {year}", "%b%d %Y").date()
        except ValueError:
            continue
        # a December transaction on a January statement belongs to the previous year
        if sd and d > sd + timedelta(days=3):
            d = d.replace(year=year - 1)
        amt = float(mm["amt"].replace(",", ""))
        # "CR" can sit on the next line, or at the end of a continuation line ("GOODS CR")
        follow = []
        for ln in lines[i + 1:i + 4]:
            if _PDF_LINE.match(ln) or ln.lower().startswith("total"):
                break
            follow.append(ln)
        if mm["cr"] or any(ln == "CR" or ln.endswith(" CR") for ln in follow):
            amt = -abs(amt)
        desc = mm["desc"].strip()
        if re.search(r"payment received|thank you", desc, re.I):
            amt = -abs(amt)
        out.append({"date": d, "description": desc, "amount": amt, "amex_category": "",
                    "ref": f"{d}|{desc}|{amt:.2f}|{i}"})
    return out


def parse_pdf(conn, data: bytes) -> list[dict]:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    rows = parse_pdf_text(text)
    if len(rows) >= 3:
        return rows
    log.info("PDF layout not recognised by regex; asking Claude to read it")
    try:
        items = llm.extract_statement_pdf(conn, base64.b64encode(data).decode())
    except llm.LLMUnavailable as e:
        log.warning("could not read PDF statement: %s", e)
        return []
    out = []
    for t in items:
        d = _parse_date(t.get("date", ""))
        if d:
            out.append({"date": d, "description": t["description"], "amount": float(t["amount"]),
                        "amex_category": "", "ref": ""})
    return out


def store(conn, rows: list[dict], source: str) -> int:
    added = 0
    for r in rows:
        desc = r["description"]
        if re.search(r"payment received|direct debit received|thank you|disputed charge", desc, re.I):
            continue  # card payments and dispute holds/reversals aren't spending
        retailer, cat = categorise(desc, r.get("amex_category", ""))
        ext = r.get("ref") or hashlib.sha1(
            f"{r['date']}|{desc}|{r['amount']:.2f}".encode()).hexdigest()
        if db.insert_ignore(conn, db.transactions, {
            "date": r["date"], "description": desc, "retailer": retailer, "category": cat,
            "amount": r["amount"], "currency": "GBP", "source": source, "ext_ref": f"{source}:{ext}",
        }, "ext_ref"):
            added += 1
    return added


def import_file(conn, path: Path | str, data: bytes | None = None) -> int:
    path = Path(path)
    data = data if data is not None else path.read_bytes()
    if path.suffix.lower() == ".pdf":
        return store(conn, parse_pdf(conn, data), "amex_pdf")
    if path.suffix.lower() in (".csv", ".txt"):
        return store(conn, parse_csv(data.decode("utf-8", errors="replace")), "amex_csv")
    if path.suffix.lower() in (".xlsx", ".xls"):
        import openpyxl  # optional dependency

        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        buf = io.StringIO()
        w = csv.writer(buf)
        for row in wb.active.iter_rows(values_only=True):
            w.writerow([(c.strftime("%d/%m/%Y") if isinstance(c, (date, datetime)) else c) or "" for c in row])
        return store(conn, parse_csv(buf.getvalue()), "amex_csv")
    raise ValueError(f"unsupported statement file: {path.name}")
