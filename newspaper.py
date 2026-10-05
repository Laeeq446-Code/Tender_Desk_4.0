"""
newspaper.py — Turn newspaper tender advertisements into structured rows.

WHY THIS EXISTS
    Commercial aggregators differentiate on newspaper coverage, not portals.
    Their operation is largely manual: staff read the papers and key the ads
    in. This module automates the English-language half of that.

WHAT IT DOES
    Accepts an e-paper page as PDF or image. If the PDF carries a text layer
    it is read directly, which is fast and accurate. Otherwise the page is
    rasterised and passed through Tesseract. The text is then segmented into
    advertisement blocks, filtered to those that look like tender notices,
    and parsed for buyer, subject, reference, dates and contact.

HONEST LIMITS
    English only. Urdu OCR quality is poor enough that wrong closing dates
    would enter the database, which is worse than a missing tender.
    Every row is written with needs_review set, because OCR of a broadsheet
    page is inherently noisier than a portal scrape. This is an assistive
    pipeline, not an unattended one.

    Only use this on material you are licensed to hold. Most e-paper terms
    prohibit automated extraction from their own site; a copy your team has
    lawfully obtained is a different matter.
"""

import os
import re
import subprocess
import tempfile

from classify import classify

# A block must look like a procurement notice, not a display advertisement.
TENDER_CUE = re.compile(
    r"tender notice|invitation (?:to|for) bid|invitation for bids|notice inviting|"
    r"expression of interest|request for proposal|request for quotation|"
    r"pre-?qualification|prequalification|sealed bids|bids are invited|"
    r"quotations are invited|tender enquiry|e-?tender|nit no", re.I)

ORG_CUE = re.compile(
    r"(government of|ministry of|department of|directorate|authority|"
    r"corporation|commission|university|institute|board|company limited|"
    r"\bltd\b|\bpvt\b|hospital|college|council|bank|division|secretariat|"
    r"municipal|cantonment|wapda|nadra|ppra|pta|ptcl|sngpl|ssgc|discos?)\b", re.I)

REF = re.compile(
    r"(?:tender|nit|bid|rfp|rfq|eoi|enquiry)\s*(?:no\.?|number|#)\s*[:\-]?\s*"
    r"([A-Za-z0-9][A-Za-z0-9/\-]{2,24}\d[A-Za-z0-9/\-]*)", re.I)

DATE = re.compile(
    r"(\d{1,2}[-/.\s](?:\d{1,2}|[A-Za-z]{3,9})[-/.\s]\d{2,4})", re.I)

MONEY = re.compile(r"(?:rs\.?|pkr)\s*([\d,]+(?:\.\d+)?)\s*(million|billion|m|bn)?", re.I)

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?:\+92|0)\d{2,4}[-\s]?\d{6,8}")

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _iso(raw, default_year=None):
    s = re.sub(r"\s+", " ", str(raw or "")).strip()
    m = re.match(r"(\d{1,2})[-/.\s]([A-Za-z]{3,9})[-/.\s](\d{2,4})", s)
    if m:
        mon = MONTHS.get(m.group(2)[:3].lower())
        if mon:
            y = m.group(3)
            y = ("20" + y) if len(y) == 2 else y
            return f"{y}-{mon:02d}-{int(m.group(1)):02d}"
    m = re.match(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", s)
    if m:
        d, mo, y = m.groups()
        y = ("20" + y) if len(y) == 2 else y
        try:
            if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
                return f"{y}-{int(mo):02d}-{int(d):02d}"
        except ValueError:
            return None
    return None


def extract_text(path, dpi=300, max_pages=8):
    """Text layer if the PDF has one, OCR otherwise. Returns list of pages."""
    ext = os.path.splitext(path)[1].lower()
    pages = []

    if ext == ".pdf":
        try:
            out = subprocess.run(["pdftotext", "-layout", path, "-"],
                                 capture_output=True, timeout=120)
            txt = out.stdout.decode("utf-8", "ignore")
            if len(re.sub(r"\s", "", txt)) > 400:
                return [p for p in txt.split("\f") if p.strip()][:max_pages], "text-layer"
        except Exception:
            pass
        # scanned: rasterise then OCR
        with tempfile.TemporaryDirectory() as td:
            try:
                subprocess.run(["pdftoppm", "-r", str(dpi), "-png",
                                "-l", str(max_pages), path,
                                os.path.join(td, "pg")],
                               capture_output=True, timeout=600)
            except Exception as e:
                return [], f"rasterise failed: {e}"
            for f in sorted(os.listdir(td)):
                if f.endswith(".png"):
                    pages.append(_ocr(os.path.join(td, f)))
        return pages, "ocr"

    return [_ocr(path)], "ocr"


def _ocr(img_path):
    try:
        import pytesseract
        from PIL import Image
        im = Image.open(img_path)
        if im.mode != "L":
            im = im.convert("L")
        # broadsheet pages are multi-column; psm 3 handles automatic layout
        return pytesseract.image_to_string(im, lang="eng", config="--psm 3")
    except Exception as e:
        return f""


def blocks(text):
    """Split a page into candidate advertisements.

    Newspaper ads are separated by whitespace runs rather than markup, so
    consecutive blank lines are the only reliable boundary available.
    """
    chunks = re.split(r"\n\s*\n\s*\n|\n\s*[-=_*]{6,}\s*\n", text)
    out = []
    for c in chunks:
        c = re.sub(r"[ \t]+", " ", c).strip()
        if 90 < len(c) < 4000:
            out.append(c)
    # OCR flattens the whitespace that separates advertisements, so a single
    # chunk often holds several notices. Each notice carries exactly one
    # heading cue, which is a more reliable boundary than blank lines.
    split2 = []
    for c in out:
        lines = c.split("\n")
        starts = [i for i, l in enumerate(lines) if TENDER_CUE.search(l)]
        if len(starts) < 2:
            split2.append(c)
            continue
        # a notice begins a line or two above its heading, at the buyer name
        bounds = []
        for i in starts:
            j = i
            while j > 0 and lines[j - 1].strip() and (i - j) < 3:
                j -= 1
            bounds.append(j)
        bounds = sorted(set([0] + bounds + [len(lines)]))
        for a_, b_ in zip(bounds, bounds[1:]):
            seg = "\n".join(lines[a_:b_]).strip()
            if len(seg) > 90:
                split2.append(seg)
    out = split2

    # merge tiny fragments that clearly belong together
    merged, buf = [], ""
    for c in out:
        if len(c) < 200 and buf and len(buf) < 1500:
            buf += "\n" + c
        else:
            if buf:
                merged.append(buf)
            buf = c
    if buf:
        merged.append(buf)
    return merged


def parse_block(text, paper, page_no):
    """One advertisement to one structured row, or None."""
    if not TENDER_CUE.search(text):
        return None

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(lines) < 2:
        return None

    # buyer: the first line naming an institution, else the longest of the top
    buyer = ""
    for l in lines[:8]:
        if ORG_CUE.search(l) and 8 < len(l) < 120:
            buyer = l
            break
    if not buyer:
        buyer = max(lines[:4], key=len)[:120]

    # subject: the work being procured, which follows the notice heading.
    # All-caps institutional headers are never the subject, however long.
    def is_header(l):
        letters = [ch for ch in l if ch.isalpha()]
        upper = sum(1 for ch in letters if ch.isupper()) / max(1, len(letters))
        return upper > 0.75 or bool(TENDER_CUE.search(l)) and len(l) < 60

    cue_at = next((i for i, l in enumerate(lines) if TENDER_CUE.search(l)), -1)
    ordered = lines[cue_at + 1:] + lines[:max(cue_at, 0)] if cue_at >= 0 else lines
    subject = ""
    for l in ordered:
        if l == buyer or len(l) < 20 or is_header(l):
            continue
        if re.match(r"^(sr|s\.?no|lot|item|contact|email|ph|tel|bid security|"
                    r"last date|closing|tender no|nit no|estimated)\b", l, re.I):
            continue
        subject = l
        break
    # stitch a following continuation line so the subject is not truncated
    if subject:
        i = ordered.index(subject)
        if i + 1 < len(ordered):
            nxt = ordered[i + 1]
            if (len(nxt) > 18 and not is_header(nxt)
                    and not re.match(r"^(tender no|nit no|bid security|last date|"
                                     r"closing|contact|email|ph|tel|estimated)\b",
                                     nxt, re.I)
                    and subject.rstrip()[-1] not in ".;:"):
                subject = subject + " " + nxt
    if not subject:
        subject = " ".join(lines[1:3])[:200]
    subject = re.sub(r"^(subject|description|name of work)\s*[:\-]\s*", "",
                     subject, flags=re.I)
    if len(subject) < 14:
        return None

    m = REF.search(text)
    ref = m.group(1).strip(" .:") if m else None

    dates = []
    for dm in DATE.finditer(text):
        iso = _iso(dm.group(1))
        if iso and "2024" <= iso[:4] <= "2030":
            dates.append(iso)
    dates = sorted(set(dates))

    vt = vn = None
    mm = MONEY.search(text)
    if mm:
        try:
            v = float(mm.group(1).replace(",", ""))
            mult = {"million": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9}.get(
                (mm.group(2) or "").lower(), 1)
            vn = v * mult
            vt = mm.group(0)
        except ValueError:
            pass

    em = EMAIL.search(text)
    ph = PHONE.search(text)

    row = {
        "ref": ref or f"NP-{paper[:6]}-{abs(hash(subject)) % 1000000}",
        "title": subject[:300],
        "description": text[:1200],
        "buyer": buyer,
        "advertised": None,
        "closing": dates[-1] if dates else None,
        "status": "Published",
        "value_text": vt, "value_num": vn,
        "currency": "PKR" if vn else None,
        "contact_email": em.group(0) if em else None,
        "contact_phone": ph.group(0) if ph else None,
        "sector_label": f"{paper} p{page_no}",
        "jurisdiction": "Newspaper",
        "source_tier": "Newspaper",
        "url": "",
    }
    row.update(classify(subject, text, buyer))
    return row


def ingest(path, paper="Newspaper", max_pages=8):
    """Full pipeline for one file. Returns (rows, diagnostics)."""
    pages, mode = extract_text(path, max_pages=max_pages)
    if isinstance(mode, str) and mode.startswith("rasterise failed"):
        return [], {"error": mode}
    rows, seen, nblocks = [], set(), 0
    for i, page in enumerate(pages, 1):
        for b in blocks(page):
            nblocks += 1
            r = parse_block(b, paper, i)
            if not r:
                continue
            key = re.sub(r"[^a-z0-9]", "", r["title"].lower())[:60]
            if key in seen:
                continue
            seen.add(key)
            rows.append(r)
    return rows, {"mode": mode, "pages": len(pages),
                  "blocks_examined": nblocks, "tenders_found": len(rows)}
