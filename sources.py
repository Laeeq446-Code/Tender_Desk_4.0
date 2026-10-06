"""
sources.py — One adapter per portal. Add a portal by adding a class.

CONFIDENCE LEVELS (be honest about these)
    proven     — tested end to end against live HTML, known to work
    drafted    — written from observed page structure, NOT yet run live
    needs-js   — portal renders via JavaScript, needs the browser fallback

Every adapter is isolated. If one throws, the run continues and the failure is
recorded in source_health so the UI shows the source as stale instead of
implying "no new tenders". Run `python run_scrape.py --only EPMS --debug` to
work on a single adapter.
"""

import re
import time
from datetime import datetime
from urllib.parse import urljoin

import ssl
import requests
import urllib3
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class LegacyTLSAdapter(HTTPAdapter):
    """Some Pakistani government portals (eproc.punjab.gov.pk notably) run
    TLS so old that modern OpenSSL refuses the handshake outright
    (RECORD_LAYER_FAILURE). This adapter lowers the security floor and skips
    certificate verification for those hosts only. Acceptable trade-off:
    the data is public read-only tender notices, and the alternative is no
    data at all."""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        try:
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        except ssl.SSLError:
            pass
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1
        except (ValueError, AttributeError):
            pass
        ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)

from classify import classify

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

REGISTRY = {}


def register(cls):
    REGISTRY[cls.name] = cls
    return cls


# ───────────────────────────────────────────── helpers
def clean(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


DATE_PATTERNS = [
    (r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", "%d/%m/%Y"),
    (r"(\d{4})-(\d{2})-(\d{2})", "%Y-%m-%d"),
]
MONTHS = ("jan feb mar apr may jun jul aug sep oct nov dec")


def parse_date(s):
    """Return ISO date string or None. Handles the many portal formats."""
    s = clean(s)
    if not s:
        return None
    # "Jul 21, 2026" / "July 21, 2026" / "21 Jul 2026" / "21-Jul-2026"
    m = re.search(r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})", s)
    if m:
        for fmt in ("%b %d %Y", "%B %d %Y"):
            try:
                return datetime.strptime(f"{m.group(1)[:9]} {m.group(2)} {m.group(3)}",
                                         fmt).date().isoformat()
            except ValueError:
                pass
    m = re.search(r"(\d{1,2})[-\s]([A-Za-z]{3,9})[-\s](\d{4})", s)
    if m:
        for fmt in ("%d %b %Y", "%d %B %Y"):
            try:
                return datetime.strptime(f"{m.group(1)} {m.group(2)[:9]} {m.group(3)}",
                                         fmt).date().isoformat()
            except ValueError:
                pass
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", s)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).date().isoformat()
        except ValueError:
            return None
    return None


def parse_time(s):
    m = re.search(r"(\d{1,2}:\d{2}\s*(?:[APap]\.?[Mm]\.?)?)", clean(s))
    return m.group(1) if m else None


CUR = {"pkr": 1.0, "rs": 1.0, "usd": 278.0, "$": 278.0, "eur": 302.0, "gbp": 355.0}


def parse_money(s):
    """Return (display_text, pkr_value, currency). USD converted at a fixed rate."""
    s = clean(s)
    if not s:
        return None, None, None
    m = re.search(r"(rs\.?|pkr|usd|us\$|\$|eur|gbp)?\s*([\d,]+(?:\.\d+)?)\s*"
                  r"(million|billion|crore|lakh|m\b|bn\b)?", s, re.I)
    if not m:
        return s, None, None
    cur_raw = (m.group(1) or "pkr").lower().strip(".")
    cur_raw = "usd" if cur_raw in ("us$", "$") else cur_raw
    try:
        val = float(m.group(2).replace(",", ""))
    except ValueError:
        return s, None, None
    mult = {"million": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9,
            "crore": 1e7, "lakh": 1e5}.get((m.group(3) or "").lower(), 1)
    rate = CUR.get(cur_raw, 1.0)
    cur = "USD" if cur_raw == "usd" else ("EUR" if cur_raw == "eur"
          else ("GBP" if cur_raw == "gbp" else "PKR"))
    return s, val * mult * rate, cur


class Source:
    """Base adapter. Subclasses implement fetch() and yield raw dicts."""
    name = "BASE"
    tier = "Federal"
    jurisdiction = "Federal"
    confidence = "drafted"
    home = ""
    delay = 0.7

    def __init__(self, debug=False, max_pages=None):
        self.debug = debug
        self.max_pages = max_pages
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

    def soup(self, url, **kw):
        r = self.s.get(url, timeout=40, **kw)
        r.raise_for_status()
        if self.debug:
            print(f"    GET {r.url} -> {r.status_code} {len(r.text)}b")
        return BeautifulSoup(r.text, "lxml")

    def total_pages(self, soup, default=1):
        m = re.search(r"Page\s+\d+\s+of\s+(\d+)", soup.get_text())
        if m:
            return int(m.group(1))
        nums = [int(a.get_text().strip()) for a in soup.find_all("a")
                if a.get_text().strip().isdigit()]
        return max(nums) if nums else default

    def fetch(self):
        raise NotImplementedError

    def run(self):
        """Fetch, classify, and stamp source metadata. Returns list of rows."""
        out = []
        for r in self.fetch():
            if not r.get("title"):
                continue
            c = classify(r.get("title"), r.get("description"),
                         r.get("buyer"), r.get("sector_label"))
            r.update(c)
            r.setdefault("source_tier", self.tier)
            r.setdefault("jurisdiction", self.jurisdiction)
            r["source"] = self.name
            out.append(r)
        return out


# ═══════════════════════════════════════════════ FEDERAL
@register
class EPMS(Source):
    """PPRA EPMS public portal. Paginates via ?page=N. Tested working."""
    name = "EPMS"
    tier = "Federal"
    jurisdiction = "Federal"
    confidence = "proven"
    home = "https://epms.ppra.gov.pk"

    LISTS = {
        "active": "/public/tenders/active-tenders",
        "history": "/public/tenders/tenders-history",
    }
    # set by backfill to page far deeper than a routine scan needs
    DEEP = False
    SECTORS = ["Info and Comm Tech", "Electrical Items", "Civil Works", "Civil Goods",
               "Petroleum, Oil & Lubricants", "Health/Medicines", "Clothing/Uniform",
               "Consumable Items", "Miscellaneous", "Services", "Vehicles",
               "Chemical Items", "Equipments", "Repair/Maintenance", "Furniture/Fixture",
               "Mechanical/Machinery", "Printing", "Stationery", "Leasing", "Food Items",
               "Mining", "Research & Development", "Appliances", "Ammunition"]

    STATUSES = ["Published Corrigendum", "Under Evaluation", "Published",
                "Cancelled", "Closed", "Corrigendum", "Awarded"]

    def row(self, tr):
        """Parse by CONTENT, not position. EPMS changed its column layout,
        which put status text into value and produced garbage dates."""
        tds = tr.find_all("td")
        if len(tds) < 5:
            return None
        cells = [clean(td.get_text(" ")) for td in tds]

        ref = None
        for c in cells:
            m = re.search(r"TS\w{6,}", c)
            if m:
                ref = m.group(0); break
        if not ref:
            return None

        di = max(range(len(tds)), key=lambda i: len(cells[i]))
        det = tds[di]
        ds = det.find(["strong", "b"])
        title = clean(ds.get_text()) if ds else cells[di][:150]
        dtext = cells[di]
        sector = next((x for x in self.SECTORS if x.lower() in dtext.lower()), "")

        status = next((c for c in cells for st in self.STATUSES
                       if c == st or c.startswith(st)), "Published")

        dates, times = [], []
        for c in cells:
            d = parse_date(c)
            if d:
                dates.append(d); times.append(parse_time(c))
        # With a single date on the row, that date is the DEADLINE. Copying it
        # into advertised made the feed group tenders under the wrong day and
        # showed August dates as if they were publication dates.
        if len(dates) >= 2:
            adv, close = dates[0], dates[-1]
            ctime = times[-1]
            if close < adv:
                adv, close = close, adv
        elif len(dates) == 1:
            adv, close, ctime = None, dates[0], times[0]
        else:
            adv = close = ctime = None

        vt = vn = cur = None
        for i, c in enumerate(cells):
            if i == di or parse_date(c):
                continue
            if re.search(r"(Rs\.?|PKR)\s*[\d,]{4,}", c, re.I):
                vt, vn, cur = parse_money(c); break

        buyer = ""
        for i, c in enumerate(cells):
            if i == di or c == status or parse_date(c) or not c:
                continue
            if re.search(r"TS\w{6,}", c) or re.search(r"Rs\.?\s*[\d,]{4,}", c, re.I):
                continue
            if c.startswith(tuple(self.STATUSES)):
                continue
            if len(c) > len(buyer):
                buyer = c

        a = tr.find("a", href=re.compile(r"/tender-details/"))
        return {
            "ref": ref, "title": title,
            "description": clean(dtext.replace(title, "", 1))[:900],
            "sector_label": sector, "buyer": buyer[:300],
            "status": status, "advertised": adv, "closing": close,
            "closing_time": ctime, "value_text": vt, "value_num": vn,
            "currency": cur,
            "url": urljoin(self.home, a["href"]) if a else self.home,
        }

    def fetch(self):
        for key, path in self.LISTS.items():
            url = self.home + path
            try:
                sp = self.soup(url, params={"page": 1})
            except Exception as e:
                if self.debug:
                    print(f"    [{key}] first page failed: {e}")
                continue
            n = self.total_pages(sp)
            if self.max_pages:
                n = min(n, self.max_pages)
            print(f"    [{self.name}/{key}] {n} pages")
            for p in range(1, n + 1):
                if p > 1:
                    time.sleep(self.delay)
                    try:
                        sp = self.soup(url, params={"page": p})
                    except Exception:
                        continue
                tb = sp.find("table")
                if not tb:
                    continue
                for tr in (tb.find("tbody") or tb).find_all("tr"):
                    r = self.row(tr)
                    if r:
                        yield r


@register
class EPADS(Source):
    """
    EPADS 2.0 federal board. The application itself (eprocure.gov.pk) is a
    pure JavaScript app, but epads.gov.pk server-renders page 1 of the public
    listing with links to /opportunities/federal/procurements/{id}, and those
    DETAIL pages are plain HTML.

    STRATEGY: harvest every ID on page 1, then walk the ID range downward
    from the highest seen, fetching detail pages directly. New tenders take
    higher IDs, so four scans a day converges on the full live set within a
    couple of days and stays current afterwards. EPADS_ID_SPAN controls how
    far each scan walks (default 250 IDs).
    """
    name = "EPADS"
    tier = "Federal"
    jurisdiction = "Federal"
    confidence = "drafted"
    home = "https://epads.gov.pk"
    DETAIL = "/opportunities/federal/procurements/{}"
    delay = 0.5

    CHROME = re.compile(
        r"(Login as (Vendor|Procuring Agency|PA)|Total Views\s*\d*|Go Back|"
        r"e-?Pak Acquisition (and|&) Disposal System|EPADS|Procurement Details\s*\||"
        r"Items? Without Lots?|Skip to main content|Toggle navigation)", re.I)

    def parse_detail(self, sp, url, pid):
        # remove obvious page furniture before reading anything
        for t in sp.find_all(["nav", "header", "footer", "script", "style", "title", "button"]):
            t.decompose()
        for a in sp.find_all("a"):
            if re.search(r"login|go back|register", a.get_text(" "), re.I):
                a.decompose()

        text = clean(sp.get_text(" "))
        text = clean(self.CHROME.sub(" ", text))
        if len(text) < 60:
            return None

        def grab(label):
            m = re.search(label + r"\s*[:\-]\s*([^:]{3,180}?)(?=\s+[A-Z][A-Za-z ]{2,24}\s*[:\-]|$)", text)
            return clean(m.group(1)) if m else None

        ref = grab(r"(?:Reference|Tender)\s*(?:No\.?|Number)") or f"EPADS-{pid}"
        title = grab(r"(?:Title|Subject|Name of Procurement)")
        if not title:
            # first substantial sentence of the cleaned content
            m = re.search(r"([A-Z][^.|]{20,200})", text)
            title = clean(m.group(1)) if m else text[:150]

        buyer = grab(r"(?:Procuring\s*Agency|Organization|Department|Ministry)") or ""
        adv = parse_date(grab(r"(?:Publish(?:ed)?\s*(?:Date|On)|Advertised?(?:\s*Date)?)") or "")
        close = parse_date(grab(r"(?:Closing|Submission|Last)\s*Date(?:\s*&?\s*Time)?") or "")
        if not (adv or close):
            ds = [d for d in (parse_date(x) for x in re.findall(
                r"\d{1,2}[-/\s][A-Za-z0-9]{2,9}[-/\s]\d{4}", text)) if d]
            adv = ds[0] if ds else None
            close = ds[-1] if len(ds) > 1 else None
        mv = re.search(r"(?:Estimated\s*(?:Cost|Value)|Bid\s*Security)\s*[:\-]?\s*"
                       r"((?:Rs\.?|PKR|USD)?\s*[\d,]+(?:\.\d+)?)", text, re.I)
        vt, vn, cur = parse_money(mv.group(1) if mv else "")
        return {
            "ref": ref[:60], "title": title[:300], "description": text[:900],
            "buyer": buyer[:300],
            "category": grab(r"(?:Procurement\s*)?Method") or "",
            "advertised": adv, "closing": close, "status": "Published",
            "value_text": vt, "value_num": vn, "currency": cur, "url": url,
        }

    def fetch(self):
        import os as _os
        span = int(_os.getenv("EPADS_ID_SPAN", "250"))

        ids = set()
        try:
            sp = self.soup(self.home)
            for a in sp.find_all("a", href=True):
                m = re.search(r"/procurements/(\d+)", a["href"])
                if m:
                    ids.add(int(m.group(1)))
        except Exception as e:
            if self.debug:
                print(f"    [{self.name}] root: {e}")

        if not ids:
            print(f"    [{self.name}] no IDs on root page; portal may have changed")
            return
        top = max(ids)
        targets = sorted(ids, reverse=True)
        targets += [i for i in range(top - 1, top - span, -1) if i not in ids]
        if self.max_pages:
            targets = targets[: self.max_pages * 25]
        print(f"    [{self.name}] top id {top}, walking {len(targets)} ids")

        misses = 0
        for pid in targets:
            url = self.home + self.DETAIL.format(pid)
            try:
                r = self.s.get(url, timeout=25)
                if r.status_code != 200:
                    misses += 1
                    if misses > 40:
                        break
                    continue
                row = self.parse_detail(BeautifulSoup(r.text, "lxml"), url, pid)
                if row:
                    misses = 0
                    yield row
                else:
                    misses += 1
            except Exception:
                misses += 1
                if misses > 40:
                    break
            time.sleep(self.delay)


# ═══════════════════════════════════════════════ PROVINCIAL
@register
class PunjabPPRA(Source):
    """
    Punjab e-procurement lives at eproc.punjab.gov.pk (ASP.NET), not the
    Drupal information site. ActiveTenders.aspx lists open tenders in a
    GridView table. ASP.NET pagination is a __doPostBack round-trip; we try
    querystring paging first and fall back to page one, which still carries
    the newest notices each scan.
    """
    name = "PPRA-Punjab"
    tier = "Provincial"
    jurisdiction = "Punjab"
    confidence = "drafted"
    home = "http://eproc.punjab.gov.pk"
    PAGES = ["/ActiveTenders.aspx"]

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.s.mount("https://", LegacyTLSAdapter())
        self.s.verify = False

    def parse_table(self, sp, page_url):
        tb = None
        for t in sp.find_all("table"):
            if len(t.find_all("tr")) > 3:
                tb = t if tb is None or len(t.find_all("tr")) > len(tb.find_all("tr")) else tb
        if tb is None:
            return
        heads = [clean(th.get_text()).lower() for th in tb.find_all("th")]
        if not heads:
            first = tb.find("tr")
            if first and len(first.find_all("td")) >= 4:
                heads = [clean(td.get_text()).lower() for td in first.find_all("td")]
        for tr in tb.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 4 or tr.find("th"):
                continue
            cells = [clean(td.get_text(" ")) for td in tds]
            rec = dict(zip(heads, cells)) if heads else {}

            def by(*keys):
                for k in keys:
                    for h, v in rec.items():
                        if k in h and v:
                            return v
                return None

            title = by("title", "subject", "description", "tender detail", "work")
            if not title:
                title = max(cells, key=len)
            if len(title) < 10 or title.lower() in ("active tenders",):
                continue
            dates = [d for d in (parse_date(c) for c in cells) if d]
            vt, vn, cur = parse_money(by("cost", "value", "amount", "estimate") or "")
            a = tr.find("a", href=True)
            yield {
                "ref": by("tender no", "reference", "tdr", "id", "no.") or cells[0][:40],
                "title": title[:300],
                "description": " | ".join(cells)[:900],
                "buyer": by("department", "agency", "organization", "procuring") or "",
                "advertised": dates[0] if dates else None,
                "closing": dates[-1] if len(dates) > 1 else (dates[0] if dates else None),
                "status": by("status") or "Published",
                "value_text": vt, "value_num": vn, "currency": cur,
                "url": urljoin(self.home + page_url, a["href"]) if a else self.home + page_url,
            }

    def fetch(self):
        for path in self.PAGES:
            try:
                sp = self.soup(self.home + path)
            except Exception as e:
                if self.debug:
                    print(f"    [{self.name}] {path}: {e}")
                continue
            got = 0
            for row in self.parse_table(sp, path) or []:
                got += 1
                yield row
            if self.debug:
                print(f"    [{self.name}] {path}: {got} rows (page 1; "
                      f"ASP.NET postback paging not crawled)")
            # querystring paging attempt, harmless if ignored by the server
            for p in range(2, (self.max_pages or 4) + 1):
                try:
                    sp = self.soup(self.home + path, params={"page": p})
                except Exception:
                    break
                more = list(self.parse_table(sp, path) or [])
                new = [r for r in more]
                if not new:
                    break
                for r in new:
                    yield r
                time.sleep(self.delay)
            return


@register
class SPPRA(Source):
    """Sindh Public Procurement Regulatory Authority."""
    name = "SPPRA-Sindh"
    tier = "Provincial"
    jurisdiction = "Sindh"
    confidence = "drafted"
    home = "https://pprasindh.gov.pk"
    PAGES = ["/tenders", "/active-tenders", "/tender-notices"]

    fetch = PunjabPPRA.fetch
    parse_table = PunjabPPRA.parse_table


@register
class KPPRA(Source):
    """Khyber Pakhtunkhwa PPRA."""
    name = "KPPRA-KP"
    tier = "Provincial"
    jurisdiction = "Khyber Pakhtunkhwa"
    confidence = "drafted"
    home = "http://www.kppra.gov.pk"
    PAGES = ["/kppra/activetenders", "/tenders", "/active-tenders"]

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.s.mount("https://", LegacyTLSAdapter())
        self.s.verify = False

    fetch = PunjabPPRA.fetch
    parse_table = PunjabPPRA.parse_table


@register
class BPPRA(Source):
    """Balochistan PPRA."""
    name = "BPPRA-Balochistan"
    tier = "Provincial"
    jurisdiction = "Balochistan"
    confidence = "drafted"
    home = "https://bppra.gob.pk"
    PAGES = ["/tenders", "/active-tenders"]

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.s.mount("https://", LegacyTLSAdapter())
        self.s.verify = False

    fetch = PunjabPPRA.fetch
    parse_table = PunjabPPRA.parse_table


# ═══════════════════════════════════════════════ MULTILATERAL
@register
class WorldBank(Source):
    """
    World Bank procurement notices. LESSON LEARNED: the API silently ignores
    unknown filter params and returns the GLOBAL feed, so we (a) send several
    country param spellings, (b) hard-filter rows to Pakistan afterwards, and
    (c) never trust submission_date as a deadline when it merely echoes the
    notice date.
    """
    name = "WorldBank"
    tier = "Multilateral"
    jurisdiction = "International"
    confidence = "proven"
    home = "https://search.worldbank.org"
    API = "https://search.worldbank.org/api/v2/procnotices"

    def fetch(self):
        rows = 100
        kept = seen = 0
        for page in range(0, (self.max_pages or 5)):
            try:
                r = self.s.get(self.API, timeout=40, params={
                    "format": "json", "rows": rows, "os": page * rows,
                    "countryname_exact": "Pakistan",
                    "countryshortname_exact": "Pakistan",
                    "qterm": "Pakistan",
                    "srt": "noticedate", "order": "desc"})
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                if self.debug:
                    print(f"    [{self.name}] page {page}: {e}")
                return
            items = data.get("procnotices") or data.get("documents") or []
            if isinstance(items, dict):
                items = list(items.values())
            if not items:
                break
            for it in items:
                if not isinstance(it, dict):
                    continue
                seen += 1
                blob = str(it).lower()
                if "pakistan" not in blob:
                    continue          # the API filter cannot be trusted
                kept += 1
                g = lambda *k: next((it[x] for x in k if it.get(x)), None)
                adv = parse_date(str(g("noticedate") or ""))
                close = parse_date(str(g("submission_deadline_date", "bid_deadline",
                                         "deadline", "submission_date") or ""))
                if close and adv and close <= adv:
                    close = None      # echoed notice date, not a real deadline
                vt, vn, cur = parse_money(str(g("estimated_cost", "contract_value") or ""))
                yield {
                    "ref": str(g("id", "noticeno", "bid_reference_no") or ""),
                    "title": clean(g("bid_description", "project_name", "noticetitle") or "")[:300],
                    "description": clean(g("bid_description", "project_name") or "")[:900],
                    "buyer": clean(g("project_ctr_name", "borrower", "agency") or
                                   "World Bank financed"),
                    "category": clean(g("procurement_type", "notice_type") or ""),
                    "advertised": adv, "closing": close, "status": "Published",
                    "value_text": vt, "value_num": vn, "currency": cur,
                    "contact_email": clean(g("email") or ""),
                    "url": (g("url") if str(g("url") or "").startswith("http")
                            else "https://projects.worldbank.org/en/projects-operations/"
                                 "procurement?searchTerm=" +
                                 str(g("bid_reference_no", "noticeno", "id") or "")),
                }
            time.sleep(self.delay)
        if self.debug:
            print(f"    [{self.name}] kept {kept} Pakistan rows of {seen} fetched")


@register
class ADB(Source):
    """Asian Development Bank tender notices, Pakistan filtered."""
    name = "ADB"
    tier = "Multilateral"
    jurisdiction = "International"
    confidence = "drafted"
    home = "https://www.adb.org"
    LIST = "https://www.adb.org/projects/tenders"

    def fetch(self):
        for page in range(0, (self.max_pages or 4)):
            try:
                sp = self.soup(self.LIST, params={
                    "country[]": "PAK", "country": "Pakistan", "page": page})
            except Exception as e:
                if self.debug:
                    print(f"    [{self.name}] page {page}: {e}")
                return
            items = sp.select(".item, .views-row, article")
            if not items:
                return
            found = 0
            for it in items:
                a = it.find("a", href=True)
                if not a:
                    continue
                title = clean(a.get_text())
                if len(title) < 12:
                    continue
                txt = clean(it.get_text(" "))
                dates = [d for d in (parse_date(x) for x in re.findall(
                    r"[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}", txt)) if d]
                yield {
                    "ref": clean((re.search(r"\b\d{5}-\d{3}\b", txt) or
                                  re.search(r"\b\d{5}\b", txt) or [""])[0])
                           if re.search(r"\d{5}", txt) else "",
                    "title": title,
                    "description": txt[:900],
                    "buyer": "ADB financed",
                    "advertised": dates[0] if dates else None,
                    "closing": dates[-1] if len(dates) > 1 else None,
                    "status": "Published",
                    "url": urljoin(self.home, a["href"]),
                }
                found += 1
            if not found:
                return
            time.sleep(self.delay)


@register
class UNGM(Source):
    """
    UN Global Marketplace. Covers UNDP, UNICEF, WFP, IOM and others operating
    in Pakistan. UNDB shut down in March 2025, so UNGM is now the live route.
    """
    name = "UNGM"
    tier = "Multilateral"
    jurisdiction = "International"
    confidence = "drafted"
    home = "https://www.ungm.org"
    SEARCH = "https://www.ungm.org/Public/Notice/Search"

    def fetch(self):
        payload = {
            "PageIndex": 0, "PageSize": 100, "Title": "", "Description": "",
            "Reference": "", "PublishedFrom": "", "PublishedTo": "",
            "DeadlineFrom": "", "DeadlineTo": "", "Countries": ["PAK"],
            "NoticeTypes": [], "UNSPSCs": [], "Agencies": [],
            "SortField": "DatePublished", "SortAscending": False,
        }
        for page in range(0, (self.max_pages or 3)):
            payload["PageIndex"] = page
            try:
                r = self.s.post(self.SEARCH, json=payload, timeout=40,
                                headers={"Content-Type": "application/json",
                                         "X-Requested-With": "XMLHttpRequest"})
                r.raise_for_status()
            except Exception as e:
                if self.debug:
                    print(f"    [{self.name}] page {page}: {e}")
                return
            try:
                sp = BeautifulSoup(r.text, "lxml")
            except Exception:
                return
            rows = sp.select(".tableRow, tr")
            found = 0
            for tr in rows:
                cells = [clean(td.get_text(" ")) for td in tr.find_all(["td", "div"], recursive=False)]
                cells = [c for c in cells if c]
                if len(cells) < 3:
                    continue
                a = tr.find("a", href=True)
                title = clean(a.get_text()) if a else cells[0]
                if len(title) < 12:
                    continue
                dates = [d for d in (parse_date(c) for c in cells) if d]
                yield {
                    "ref": next((c for c in cells if re.match(r"^[A-Z0-9/\-]{6,}$", c)), ""),
                    "title": title,
                    "description": " | ".join(cells)[:900],
                    "buyer": next((c for c in cells if re.search(
                        r"UNDP|UNICEF|WFP|IOM|UNHCR|UNOPS|FAO|WHO", c, re.I)), "UN agency"),
                    "advertised": dates[0] if dates else None,
                    "closing": dates[-1] if len(dates) > 1 else None,
                    "status": "Published",
                    "url": urljoin(self.home, a["href"]) if a else self.SEARCH,
                }
                found += 1
            if not found:
                return
            time.sleep(self.delay)


@register
class OtherOrgs(Source):
    """
    Buyers outside PPRA's remit: universities, autonomous bodies, banks and
    large corporates. They run the same digitisation programmes as federal
    ministries but publish on their own websites.

    DISCOVERY, NOT FIXED URLS. Institutional sites move their tender page
    constantly, which is why a single hardcoded path fails for most of them.
    Each organisation is given several candidate paths; the adapter takes the
    first that responds, and if that page is only a hub of links it follows
    one level down to the actual listing. Rows are accepted from tables, from
    list markup, and from direct document links.
    """
    name = "Other"
    tier = "Other"
    jurisdiction = "Other"
    confidence = "drafted"
    home = ""
    delay = 0.6

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        # institutional sites often run expired or legacy certificates and
        # reject non-browser clients outright
        self.s.mount("https://", LegacyTLSAdapter())
        self.s.verify = False
        self.s.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        })

    # (label, base, [candidate paths])
    ORGS = [
        # ── Federal digital and autonomous bodies
        ("NITB", "https://nitb.gov.pk", ["/tenders", "/Tenders", "/procurement", "/"]),
        ("Ignite (NTF)", "https://ignite.org.pk", ["/tenders", "/procurement", "/"]),
        ("PSEB", "https://www.pseb.org.pk", ["/tenders", "/procurement", "/"]),
        ("PTA", "https://www.pta.gov.pk", ["/en/tenders", "/tenders", "/"]),
        ("USF", "https://www.usf.org.pk", ["/tenders", "/procurement", "/"]),
        ("NADRA", "https://www.nadra.gov.pk", ["/tenders", "/procurement", "/"]),
        ("HEC", "https://www.hec.gov.pk", ["/english/pages/tenders.aspx", "/tenders", "/"]),
        ("PITB", "https://www.pitb.gov.pk", ["/tenders", "/procurement", "/"]),
        ("SIFC", "https://www.sifc.gov.pk", ["/tenders", "/"]),
        ("Pakistan Post", "https://www.pakpost.gov.pk", ["/tenders", "/"]),
        ("CDA", "https://www.cda.gov.pk", ["/tenders", "/public/tenders", "/"]),
        ("NHA", "https://nha.gov.pk", ["/tenders", "/en/tenders", "/"]),
        # ── Universities
        ("NUST", "https://nust.edu.pk", ["/tender/", "/tenders", "/downloads/tender-document/"]),
        ("IBA Karachi", "https://www.iba.edu.pk", ["/tenders.php", "/tenders", "/procurement"]),
        ("Virtual University", "https://www.vu.edu.pk", ["/tenders", "/Tenders"]),
        ("NAVTTC", "https://navttc.gov.pk", ["/tenders", "/procurement"]),
        ("Shaikh Ayaz University", "https://saus.edu.pk", ["/tenders", "/tender"]),
        ("Rawalpindi Institute of Cardiology", "https://ric.gop.pk", ["/tenders", "/"]),
        ("PCRWR", "https://pcrwr.gov.pk", ["/tenders", "/procurement"]),
        ("NESPAK", "https://www.nespak.com.pk", ["/tenders", "/procurement"]),
        ("OGDCL", "https://www.ogdcl.com", ["/tenders", "/procurement/tenders"]),
        ("U Microfinance Bank", "https://www.umicrofinancebank.com.pk", ["/tenders", "/"]),
        ("PPHI Balochistan", "https://pphibalochistan.org.pk", ["/tenders", "/"]),
        ("Quaid-i-Azam University", "http://qau.edu.pk", ["/tenders", "/tender", "/"]),
        ("COMSATS Islamabad", "https://islamabad.comsats.edu.pk",
         ["/Tenders.aspx", "/tenders", "/"]),
        ("IIUI", "https://www.iiu.edu.pk", ["/tenders", "/tender", "/"]),
        ("Air University", "https://au.edu.pk", ["/Pages/Tenders.aspx", "/tenders", "/"]),
        ("Bahria University", "https://bahria.edu.pk", ["/tenders", "/tender", "/"]),
        ("NUML", "https://numl.edu.pk", ["/tenders", "/tender", "/"]),
        ("PIEAS", "https://www.pieas.edu.pk", ["/tenders", "/tender", "/"]),
        ("Arid Agriculture University", "https://uaar.edu.pk",
         ["/tenders.php", "/tenders", "/"]),
        ("AIOU", "https://aiou.edu.pk", ["/tenders", "/tender", "/"]),
        ("FAST NUCES", "https://www.nu.edu.pk", ["/Tenders", "/tenders", "/"]),
        ("UET Taxila", "https://web.uettaxila.edu.pk", ["/tenders", "/"]),
        ("Punjab University", "http://pu.edu.pk", ["/tenders", "/page/tenders", "/"]),
        ("University of Karachi", "https://uok.edu.pk", ["/tenders", "/"]),
        ("NED University", "https://www.neduet.edu.pk", ["/tenders", "/"]),
        ("Aga Khan University", "https://www.aku.edu", ["/tenders", "/"]),
        ("LUMS", "https://lums.edu.pk", ["/tenders", "/procurement", "/"]),
        ("GIKI", "https://giki.edu.pk", ["/tenders", "/"]),
        # ── Banks
        ("HBL", "https://www.hbl.com", ["/tenders", "/about-us/tenders", "/"]),
        ("UBL", "https://www.ubldigital.com", ["/tenders", "/"]),
        ("Bank Alfalah", "https://www.bankalfalah.com", ["/tenders/", "/tender", "/"]),
        ("Meezan Bank", "https://www.meezanbank.com", ["/tenders/", "/tender", "/"]),
        ("Faysal Bank", "https://www.faysalbank.com", ["/en/tenders/", "/tenders", "/"]),
        ("Askari Bank", "https://www.askaribank.com", ["/tenders/", "/tender", "/"]),
        ("Bank of Punjab", "https://www.bop.com.pk", ["/Tenders", "/tenders", "/"]),
        ("JS Bank", "https://www.jsbl.com", ["/tenders", "/"]),
        ("Bank Al Habib", "https://www.bankalhabib.com", ["/tenders", "/"]),
        # ── Corporates and utilities
        ("PTCL", "https://ptcl.com.pk", ["/Tender", "/tenders", "/"]),
        ("K-Electric", "https://www.ke.com.pk", ["/tenders/", "/tender", "/"]),
        ("PSX", "https://www.psx.com.pk", ["/psx/resources-and-tools/tenders", "/"]),
        ("Pakistan Railways", "https://www.pakrail.gov.pk", ["/tenders", "/"]),
        ("PIA", "https://www.piac.com.pk", ["/tenders", "/procurement", "/"]),
    ]

    HINT = re.compile(
        r"tender|bid(?:ding)?\b|rfp|rfq|eoi|expression of interest|quotation|"
        r"procurement|invitation to bid|prequalif|pre-qualif|notice inviting", re.I)
    NAV = re.compile(r"^(home|about|contact|login|apply|admission|news|events?|"
                     r"gallery|faculty|alumni|library)$", re.I)

    WP_TYPES = ("tender", "tenders", "tender-notice", "procurement")

    def wp_rows(self, base, org):
        """WordPress REST API. Institutional sites overwhelmingly run
        WordPress, and it exposes custom post types as JSON. This removes the
        URL guesswork that made the fixed-path approach fail: we ask the site
        what post types it has, then pull the tender one directly.
        """
        found = []
        try:
            r = self.s.get(base.rstrip("/") + "/wp-json/wp/v2/types", timeout=20)
            if r.status_code != 200:
                return found
            types = r.json()
        except Exception:
            return found

        cands = [k for k in types
                 if any(t in k.lower() for t in ("tender", "procure", "bid"))]
        if not cands:
            cands = [k for k in ("post",) if k in types]
        for t in cands[:2]:
            rest = (types.get(t, {}) or {}).get("rest_base") or t
            for page in (1, 2):
                try:
                    rr = self.s.get(f"{base.rstrip('/')}/wp-json/wp/v2/{rest}",
                                    params={"per_page": 50, "page": page,
                                            "orderby": "date", "order": "desc"},
                                    timeout=25)
                    if rr.status_code != 200:
                        break
                    items = rr.json()
                except Exception:
                    break
                if not isinstance(items, list) or not items:
                    break
                for it in items:
                    title = clean(BeautifulSoup(
                        (it.get("title") or {}).get("rendered", ""), "lxml").get_text())
                    body = clean(BeautifulSoup(
                        (it.get("content") or {}).get("rendered", ""), "lxml").get_text())
                    if len(title) < 12:
                        continue
                    if t == "post" and not self.HINT.search(title):
                        continue
                    blob = f"{title} {body}"
                    dates = [d for d in (parse_date(x) for x in re.findall(
                        r"\d{1,2}[-/\s][A-Za-z0-9]{2,9}[-/\s]\d{2,4}|\d{4}-\d{2}-\d{2}",
                        blob)) if d]
                    vt, vn, cur = parse_money(blob)
                    found.append({
                        "ref": f"{re.sub(r'[^A-Za-z]', '', org)[:10]}-{it.get('id')}",
                        "title": title[:300], "description": blob[:900],
                        "buyer": org,
                        "advertised": parse_date(str(it.get("date") or "")) or
                                      (dates[0] if dates else None),
                        "closing": dates[-1] if dates else None,
                        "status": "Published",
                        "value_text": vt, "value_num": vn, "currency": cur,
                        "url": it.get("link") or base,
                    })
                if len(items) < 50:
                    break
            if found:
                break
        return found

    def rows_from(self, sp, page_url, org):
        """Pull candidate tenders from tables, lists, and document links."""
        out, seen = [], set()

        def add(title, href, extra=""):
            title = clean(title)
            if (len(title) < 14 or title.lower() in seen or self.NAV.match(title)
                    or not self.HINT.search(title + " " + extra)):
                return
            seen.add(title.lower())
            blob = f"{title} {extra}"
            dates = [d for d in (parse_date(x) for x in re.findall(
                r"\d{1,2}[-/\s][A-Za-z0-9]{2,9}[-/\s]\d{2,4}|\d{4}-\d{2}-\d{2}",
                blob)) if d]
            vt, vn, cur = parse_money(blob)
            out.append({
                "ref": f"{re.sub(r'[^A-Za-z]', '', org)[:10]}-{abs(hash(title)) % 1000000}",
                "title": title[:300], "description": clean(blob)[:900],
                "buyer": org,
                "advertised": dates[0] if dates else None,
                "closing": dates[-1] if len(dates) > 1 else None,
                "status": "Published",
                "value_text": vt, "value_num": vn, "currency": cur,
                "url": urljoin(page_url, href) if href else page_url,
            })

        for tr in sp.select("table tr"):
            cells = [clean(td.get_text(" ")) for td in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if len(cells) < 2:
                continue
            a = tr.find("a", href=True)
            add(max(cells, key=len), a["href"] if a else None, " ".join(cells))

        for li in sp.select("li, .card, .post, article, .tender, .media"):
            txt = clean(li.get_text(" "))
            if not (20 < len(txt) < 400):
                continue
            a = li.find("a", href=True)
            add(txt[:200], a["href"] if a else None, txt)

        for a in sp.find_all("a", href=True):
            h = a["href"]
            label = clean(a.get_text())
            ctx = clean(a.find_parent().get_text(" ")) if a.find_parent() else ""
            if re.search(r"\.(pdf|docx?|xlsx?)(\?|$)", h, re.I):
                add(label or ctx[:120], h, ctx)
            elif self.HINT.search(label):
                add(label, h, ctx)
        return out

    def fetch(self):
        for org, base, paths in self.ORGS:
            got = self.wp_rows(base, org)
            if got and self.debug:
                print(f"    [{self.name}] {org}: {len(got)} via WordPress API")
            if got:
                for r in got[:80]:
                    yield r
                time.sleep(self.delay)
                continue
            for path in paths:
                url = base.rstrip("/") + path
                try:
                    sp = self.soup(url)
                except Exception as e:
                    if self.debug:
                        print(f"    [{self.name}] {org} {path}: {type(e).__name__}")
                    continue

                got = self.rows_from(sp, url, org)
                if got:
                    break

                # hub page: follow one level to a link that looks like a listing
                nxt = None
                for a in sp.find_all("a", href=True):
                    if self.HINT.search(clean(a.get_text()) or "") or \
                       self.HINT.search(a["href"]):
                        cand = urljoin(url, a["href"])
                        if cand.rstrip("/") != url.rstrip("/"):
                            nxt = cand
                            break
                hops, cur = 0, nxt
                while cur and hops < 2:
                    hops += 1
                    try:
                        sp2 = self.soup(cur)
                    except Exception as e:
                        if self.debug:
                            print(f"    [{self.name}] {org} hop{hops}: {type(e).__name__}")
                        break
                    got = self.rows_from(sp2, cur, org)
                    if got:
                        if self.debug:
                            print(f"    [{self.name}] {org}: followed -> {cur}")
                        break
                    nxt2 = None
                    for a2 in sp2.find_all("a", href=True):
                        if self.HINT.search(clean(a2.get_text()) or "") or \
                           self.HINT.search(a2["href"]):
                            cand2 = urljoin(cur, a2["href"])
                            if cand2.rstrip("/") not in (cur.rstrip("/"), url.rstrip("/")):
                                nxt2 = cand2
                                break
                    cur = nxt2
                if got:
                    break
            if self.debug:
                print(f"    [{self.name}] {org}: {len(got)}")
            for r in got[:80]:
                yield r
            time.sleep(self.delay)


BIDSEC_LO, BIDSEC_HI = 0.05, 0.02   # bid security is conventionally 2-5% of value


def fetch_detail(url):
    """Fetch one tender's source page lazily: bid security, fees, attachments,
    contacts. Value band is back-calculated from bid security because most
    listings hide the value but publish the security."""
    ses = requests.Session()
    ses.headers.update({"User-Agent": UA})
    ses.mount("https://", LegacyTLSAdapter())
    ses.verify = False
    try:
        r = ses.get(url, timeout=12)
        r.raise_for_status()
        sp = BeautifulSoup(r.text, "lxml")
    except Exception as e:
        return {"error": str(e)[:180]}
    for t in sp.find_all(["nav", "header", "footer", "script", "style"]):
        t.decompose()
    text = clean(sp.get_text(" "))

    def grab(pat):
        m = re.search(pat, text, re.I)
        return clean(m.group(1)) if m else None

    atts = []
    for a in sp.find_all("a", href=True):
        h = a["href"]
        if re.search(r"\.(pdf|docx?|xlsx?|zip)(\?|$)", h, re.I) or "download" in h.lower():
            full = urljoin(url, h)
            if full not in [x["url"] for x in atts]:
                lbl = clean(a.get_text())[:80] or "Document"
                atts.append({"label": lbl, "url": full})

    sec = grab(r"Bid\s*Security[:\s]*((?:Rs\.?|PKR)?\s*[\d,]+(?:\.\d+)?)")
    _, sv, _ = parse_money(sec or "")
    lo = hi = None
    if sv and sv > 1000:
        lo, hi = sv / BIDSEC_LO, sv / BIDSEC_HI

    return {
        "bid_security": sec,
        "tender_fee": grab(r"(?:Tender|Document)\s*Fee[:\s]*((?:Rs\.?|PKR)?\s*[\d,]+)"),
        "est_value_low": lo, "est_value_high": hi,
        "scope": None, "eligibility":
            grab(r"(?:Eligibility|Qualification)[:\s]*(.{30,500}?)(?:Bid Security|Tender Fee|$)"),
        "prebid": grab(r"Pre-?bid[^:]{0,20}[:\s]*([A-Za-z0-9,:\s/]{6,40})"),
        "opening": grab(r"Opening[^:]{0,20}[:\s]*([A-Za-z0-9,:\s/]{6,40})"),
        "venue": None,
        "contact_email": grab(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"),
        "contact_phone": grab(r"((?:\+92|0)\d{2,4}[-\s]?\d{6,8})"),
        "attachments": json.dumps(atts[:10]) if atts else None,
    }


class ArchiveMixin:
    """Shared paging behaviour for portal archive views.

    Live adapters read the first page or two because that is where new
    notices appear. Archives need the opposite: page until the portal stops
    serving rows. Kept separate so a deep sweep can never slow a routine scan.
    """
    ARCHIVE_PATHS = []
    max_archive_pages = 400

    def fetch(self):
        for path in self.ARCHIVE_PATHS:
            url = self.home.rstrip("/") + path
            got_any = False
            for p in range(1, (self.max_pages or self.max_archive_pages) + 1):
                try:
                    sp = self.soup(url, params={"page": p})
                except Exception as e:
                    if self.debug:
                        print(f"    [{self.name}] {path} p{p}: {type(e).__name__}")
                    break
                rows = list(self.parse_table(sp, path) or [])
                if not rows:
                    break
                got_any = True
                for r in rows:
                    yield r
                if self.debug and p % 20 == 0:
                    print(f"    [{self.name}] {path}: page {p}")
                time.sleep(self.delay)
            if got_any:
                return


@register
class PunjabArchive(ArchiveMixin, PunjabPPRA):
    name = "PPRA-Punjab-Archive"
    confidence = "drafted"
    ARCHIVE_PATHS = ["/ClosedTenders.aspx", "/TenderArchive.aspx",
                     "/ActiveTenders.aspx"]


@register
class SindhArchive(ArchiveMixin, SPPRA):
    name = "SPPRA-Sindh-Archive"
    confidence = "drafted"
    ARCHIVE_PATHS = ["/tenders/archive", "/closed-tenders", "/tenders"]


@register
class KPArchive(ArchiveMixin, KPPRA):
    name = "KPPRA-KP-Archive"
    confidence = "drafted"
    ARCHIVE_PATHS = ["/kppra/closedtenders", "/kppra/tenderarchive",
                     "/kppra/activetenders"]


@register
class EPMSAwards(Source):
    """PPRA contract awards from epms.ppra.gov.pk/public/contracts.

    Ported from the Phase 1 notebook, which is the only awards parser that
    has run successfully against the live portal. The earlier version
    guessed paths and generic headers and never returned a row, which is why
    the Intelligence tab stayed empty.

    Listing rows carry the contract number, ministry, title, winner,
    executing organisation, value and date. For biddable lanes the detail
    page adds bidders received, tender value and signing date.
    """
    name = "EPMS-Awards"
    tier = "Federal"
    jurisdiction = "Federal"
    confidence = "proven"
    home = "https://epms.ppra.gov.pk"
    LIST = "/public/contracts"
    delay = 0.7
    months_back = 2          # scans re-sweep two months; backfill sets more
    detail_limit = 120       # detail pages fetched per run, biddable lanes only

    def parse_row(self, tr):
        tds = tr.find_all("td")
        if len(tds) < 6:
            return None
        cells = [clean(td.get_text(" ")) for td in tds]
        pcn = next((m.group(0) for c in cells for m in [re.search(r"PCN-\d+", c)] if m), None)
        if not pcn:
            return None
        di = max(range(len(tds)), key=lambda i: len(cells[i]))
        det = tds[di]
        bolds = [clean(b.get_text()) for b in det.find_all(["strong", "b"])]
        ministry = bolds[0] if bolds else ""
        title = bolds[1] if len(bolds) > 1 else (bolds[0] if bolds else cells[di][:200])
        det_text = cells[di]
        winner = ""
        try:
            after = det_text.split(title, 1)[1]
            winner = clean(re.split(r"\bNational\b|\bInternational\b|Tender:", after)[0])
            winner = winner.strip(" -\u2013")[:160]
        except Exception:
            pass
        tn = re.search(r"TS\d+E", det_text)
        org = clean(tds[di + 1].get_text(" ")) if di + 1 < len(tds) else ""
        money = next((c for c in cells if re.search(r"(rs\.?|pkr)\s*[\d,]{3,}", c, re.I)), "")
        if not money and di + 2 < len(tds):
            money = cells[di + 2]
        vt, vn, cur = parse_money(money)
        if vn is not None and vn < 10000:
            vn = None              # portal data errors such as Rs 2
        dates = [d for d in (parse_date(c) for c in cells) if d]
        a = tr.find("a", href=re.compile(r"/contract-details/"))
        return {
            "ref": pcn, "tender_no": tn.group(0) if tn else "",
            "title": title[:300], "description": det_text[:600],
            "buyer": f"{ministry} {org}".strip() or ministry,
            "winner": winner, "value_text": vt, "value_num": vn,
            "award_date": dates[0] if dates else None,
            "url": urljoin(self.home, a["href"]) if a else self.home + self.LIST,
        }

    def detail(self, url):
        try:
            text = clean(self.soup(url).get_text(" "))
        except Exception:
            return {}

        def grab(p):
            m = re.search(p, text, re.I)
            return clean(m.group(1)) if m else None
        bids = grab(r"Bids Received:\s*(\d+)")
        return {
            "bids_received": int(bids) if bids else None,
            "signing_date": parse_date(grab(r"Contract Signing Date:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})") or ""),
            "tender_value": parse_money(grab(r"Tender Value:\s*(Rs\.?[\s\d,\.]+)") or "")[1],
        }

    def _pages(self, params):
        try:
            sp = self.soup(self.home + self.LIST, params={**params, "page": 1})
        except Exception as e:
            if self.debug:
                print(f"    [{self.name}] {params}: {type(e).__name__}")
            return
        n = self.total_pages(sp)
        if self.max_pages:
            n = min(n, self.max_pages)
        for p in range(1, n + 1):
            if p > 1:
                time.sleep(self.delay)
                try:
                    sp = self.soup(self.home + self.LIST, params={**params, "page": p})
                except Exception:
                    break
            tb = sp.find("table")
            if not tb:
                continue
            for tr in (tb.find("tbody") or tb).find_all("tr"):
                r = self.parse_row(tr)
                if r:
                    yield r

    def fetch(self):
        from datetime import date as _date
        seen, rows = set(), []
        windows = [{}]
        today = _date.today()
        for i in range(1, (self.months_back or 0) + 1):
            y, m = divmod((today.year * 12 + today.month - 1) - i, 12)
            m += 1
            d_from = _date(y, m, 1)
            d_to = _date(y + (m == 12), (m % 12) + 1, 1)
            windows.append({"date_from": d_from.isoformat(), "date_to": d_to.isoformat()})
        for w in windows:
            got = 0
            for r in self._pages(w):
                if r["ref"] in seen:
                    continue
                seen.add(r["ref"])
                rows.append(r)
                got += 1
            if self.debug or w:
                print(f"    [{self.name}] {w.get('date_from', 'current view')}: +{got}")
        # enrich only the rows that analytics actually uses
        from classify import classify
        fetched = 0
        for r in rows:
            c = classify(r["title"], "", r["buyer"])
            if c["lane"] in ("Core", "Partner-led") and fetched < self.detail_limit:
                time.sleep(self.delay)
                r.update({k: v for k, v in self.detail(r["url"]).items() if v})
                fetched += 1
            yield r


def all_sources():
    return list(REGISTRY.keys())


def get_source(name, **kw):
    cls = REGISTRY.get(name)
    return cls(**kw) if cls else None
