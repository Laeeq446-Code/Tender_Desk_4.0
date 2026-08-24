"""
enrich.py — Catalogue awards properly from text already scraped.

The EPMS-Awards board is shallow by nature, roughly 350 recent contracts and no
deep archive, so this does not chase depth. It makes the rows that exist clean
and complete. Three extractions, all from the title and description the scrape
already captured, so they apply to stored data with no re-scrape.

    WINNER   Pakistani award notices name the vendor in the "M/s." convention,
             for example "M/s. Daim Associates Karachi". The adapter looked for
             a winner column these pages do not have. This reads it from the
             text instead, which covers the large majority of rows.
    TENURE   Service contracts state a term, "for a period of three (03) years",
             "36 months". Goods purchases do not, so this stays empty on them by
             fact, not by failure.
    DATE     A malformed award date such as "2025-26-00" is rejected rather than
             stored, so it can never poison the headline or a forecast.

Used by the awards adapter at scrape time and by the reprocess pass over rows
already in the database.
"""

import re

# Cities and address markers that follow a firm name and should be trimmed off.
_CITY = (r"Islamabad|Karachi|Lahore|Rawalpindi|Gujranwala|Multan|Faisalabad|"
         r"Peshawar|Quetta|Sialkot|Hyderabad|Sukkur|Bahawalpur|Sargodha|Gujrat|"
         r"Abbottabad|Mardan|Sahiwal|Wah|Kohat|Mirpur|Gilgit|Muzaffarabad")

_STOP = (r"National\s+Tender|Provincial\s+Tender|TS\d|TN\d|P\.?\s?No|Plot|"
         r"E-\d|Survey|Near|Phase|Block|Street|Road\b|Sector|House|Shop|"
         r"Floor|Building|Market|Town|Colony|Scheme|Chowk|Bazar|Bazaar|"
         r"Office|Suite|Room|Flat|Gali|Mouza|Tehsil|District|Opposite|"
         r"\d{1,2}(?:st|nd|rd|th)\b|\bNo\.")

# Scope and verb words that signal we have run past the firm name into the
# description of the work. Used both to stop capture and to reject a bad grab.
_SCOPE = (r"along with|works?\b|services?\b|repair|providing|fixing|supply|"
          r"installation|construction|procurement|maintenance|provision|"
          r"including|complete|various|miscellaneous|equipment|material")

_FIRM_SUFFIX = re.compile(
    r"(enterprise|trader|associate|international|pvt|private|ltd|limited|"
    r"corporation|industries|company|\bco\b|&\s?co|builder|engineer|"
    r"solution|system|technolog|tech\b|services|store|paint|mills|hardware|"
    r"traders|brothers|sons|agencies|agency|impex|traders|construction)", re.I)

# M/s. <Firm>, stopping at a comma, address marker, city, or scope word.
_WINNER = re.compile(
    r"M/?s\.?\s+"
    r"([A-Z][A-Za-z0-9&.\-' ]{2,55}?)"
    r"(?=,|\s+(?:" + _STOP + r"|" + _CITY + r"|" + _SCOPE + r")\b|\s*$)",
    re.I)

_WINNER2 = re.compile(
    r"(?:awarded\s+to|contractor|supplier|vendor)\s*[:\-]?\s+"
    r"([A-Z][A-Za-z0-9&.\-' ]{2,55}?)"
    r"(?=,|\s+(?:" + _STOP + r"|" + _CITY + r"|" + _SCOPE + r")\b|\s*$)",
    re.I)

_SCOPE_RE = re.compile(_SCOPE, re.I)

_NUMWORD = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
            "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12,
            "eighteen": 18, "twenty four": 24, "thirty six": 36}


def extract_winner(*texts):
    """Return the awarded firm, or '' if not stated in the text."""
    for t in texts:
        if not t:
            continue
        m = _WINNER.search(t) or _WINNER2.search(t)
        if not m:
            continue
        w = re.sub(r"\s{2,}", " ", m.group(1)).strip(" .,-")
        w = re.sub(r"\s+(?:" + _CITY + r")$", "", w, flags=re.I).strip()
        if len(w) < 4 or not re.search(r"[A-Za-z]", w):
            continue
        # a firm name is not a fragment of the scope of work
        if _SCOPE_RE.search(w):
            continue
        # collapse an accidental repeat like "Abacus Abacus Consulting ... Abacus"
        seen, dedup = set(), []
        for tok in w.split():
            key = tok.lower()
            if key not in seen or len(key) <= 3:
                dedup.append(tok)
                seen.add(key)
        w = " ".join(dedup)
        w = re.sub(r"\s+\d[\w\-]*$", "", w).strip()  # trailing plot/number tails
        words = w.split()
        # accept if it carries a company suffix, or reads as a 2 to 5 word name
        if _FIRM_SUFFIX.search(w) or (2 <= len(words) <= 5):
            return w[:80]
    return ""


def extract_tenure_months(*texts):
    """Return contract term in months if the wording states one, else None."""
    for t in texts:
        if not t:
            continue
        s = t.lower()
        # "for a period of three (03) years" / "03 years" / "3-year"
        m = re.search(r"(?:period of\s+)?(\d{1,2})\s*\(?\d{0,2}\)?\s*year", s)
        if m:
            return int(m.group(1)) * 12
        m = re.search(r"(?:period of\s+)?(\d{1,3})\s*\(?\d{0,3}\)?\s*month", s)
        if m:
            return int(m.group(1))
        for word, n in _NUMWORD.items():
            if re.search(r"period of\s+" + word + r"\b.*year", s):
                return n * 12
            if re.search(r"period of\s+" + word + r"\b.*month", s):
                return n
    return None


def clean_date(s):
    """Return a valid ISO date or None. Rejects impossible values."""
    if not s:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", str(s))
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def enrich(row):
    """Fill winner, tenure and a clean award_date on an award row, in place.

    Never overwrites a value that is already present, so a real scraped column
    always wins over a text guess.
    """
    title = row.get("title") or ""
    desc = row.get("description") or ""
    if not row.get("winner"):
        w = extract_winner(title, desc)
        if w:
            row["winner"] = w
    if not row.get("tenure_months"):
        tm = extract_tenure_months(title, desc)
        if tm:
            row["tenure_months"] = tm
    cd = clean_date(row.get("award_date"))
    row["award_date"] = cd  # None if it was malformed, better than a fake date
    return row
