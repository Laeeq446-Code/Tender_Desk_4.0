"""
app.py — Web service. Serves the dashboard, the JSON API, and runs the scans.

    pip install -r requirements.txt
    python run_scrape.py --only EPMS          # populate first
    uvicorn app:app --host 0.0.0.0 --port 8000

Then open http://localhost:8000 and enter the passphrase (default: jazz2026).

SCANS run at 09:00, 12:00, 15:00 and 18:00 Pakistan time. Override with
SCAN_HOURS="9,12,15,18" and TZ_OFFSET=5.

AUTH is a shared passphrase, set with PASSPHRASE. It stops casual discovery of
a public URL. It is not real access control. Keep the pursuit list and fit
scores off any public deployment until this sits on Jazz infrastructure.
"""

import os
import secrets
import threading
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import db
import sources as S

PASSPHRASE = os.getenv("PASSPHRASE", "jazz2026")
SCAN_HOURS = [int(h) for h in os.getenv("SCAN_HOURS", "9,12,15,18").split(",")]
TZ_OFFSET = int(os.getenv("TZ_OFFSET", "5"))          # PKT = UTC+5
ENABLED = [s.strip() for s in os.getenv(
    "ENABLED_SOURCES", "EPMS,EPMS-Awards,EPADS,PPRA-Punjab,SPPRA-Sindh,KPPRA-KP,"
    "BPPRA-Balochistan,Other,WorldBank,ADB,UNGM").split(",") if s.strip()]
SHOW_FIT = os.getenv("SHOW_FIT", "0") == "1"          # keep off for public
UI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")

app = FastAPI(title="Tender Desk")
_tokens = set()
_lock = threading.Lock()
_state = {"running": False, "last": None}

db.init()
try:
    _fixed = db.repair_dates()
    if _fixed:
        print(f"[startup] corrected {_fixed} impossible advertised dates")
except Exception as _e:
    print(f"[startup] date repair skipped: {_e}")

# Catalogue awards already in the database: winner from the "M/s." text, tenure
# where stated, valid dates. Text-only and idempotent, so deploying this update
# cleans existing rows immediately, before any scan runs.
try:
    _en = db.reprocess_awards()
    if _en:
        print(f"[startup] catalogued {_en} awards (winner, tenure, dates)")
except Exception as _e:
    print(f"[startup] award enrichment skipped: {_e}")


# ───────────────────────────────────────────── scanning
def scan(trigger="manual", only=None, max_pages=None, debug=False):
    if _state["running"]:
        return {"ok": False, "error": "scan already running"}
    with _lock:
        _state["running"] = True
    started = db.now()
    ok_n = fail_n = seen = new = 0
    detail = []
    try:
        targets = [only] if only else ENABLED
        for name in targets:
            src = S.get_source(name, debug=debug, max_pages=max_pages)
            if not src:
                continue
            t0 = datetime.now()
            try:
                rows = src.run()
                if name.endswith("Awards"):
                    s, n = db.upsert_awards(rows, name)
                    try:
                        db.reprocess_awards()   # keep the catalogue clean
                    except Exception:
                        pass
                    ch = 0
                else:
                    s, n, ch = db.upsert(rows, name)
                ms = int((datetime.now() - t0).total_seconds() * 1000)
                db.record_health(name, True, len(rows), f"{n} new, {ch} changed", ms)
                seen += s
                new += n
                ok_n += 1
                detail.append({"source": name, "ok": True, "rows": len(rows),
                               "new": n, "changed": ch, "ms": ms})
                print(f"  [{name}] {len(rows)} rows, {n} new, {ch} changed ({ms}ms)")
            except Exception as e:
                ms = int((datetime.now() - t0).total_seconds() * 1000)
                db.record_health(name, False, 0, f"{type(e).__name__}: {e}", ms)
                fail_n += 1
                detail.append({"source": name, "ok": False, "error": str(e)[:200]})
                print(f"  [{name}] FAILED: {type(e).__name__}: {e}")
        db.clear_new_flags(24)

        # Newspapers ride the same schedule as the portals. This ingests any
        # e-paper files dropped in the watched folder, and downloads the
        # configured papers only if fetch is explicitly enabled. It never
        # raises, so a newspaper problem cannot fail a scan.
        try:
            import newspaper_fetch
            np = newspaper_fetch.auto_ingest()
            if np.get("rows"):
                detail.append({"source": "Newspapers", "ok": True,
                               "rows": np["rows"], "new": np["rows"], "notes": np["notes"]})
                new += np["rows"]
                print(f"  [Newspapers] {np['rows']} rows ({np['notes']})")
        except Exception as e:
            print(f"  [Newspapers] skipped: {type(e).__name__}: {e}")

        try:
            db.repair_dates()
        except Exception:
            pass
        db.record_run(started, ok_n, fail_n, seen, new, trigger)
        _state["last"] = {"at": db.now(), "new": new, "seen": seen,
                          "ok": ok_n, "failed": fail_n, "detail": detail}
        return {"ok": True, "sources_ok": ok_n, "sources_failed": fail_n,
                "rows_seen": seen, "rows_new": new, "detail": detail}
    finally:
        with _lock:
            _state["running"] = False


scheduler = BackgroundScheduler(daemon=True)


@app.on_event("startup")
def _start():
    if os.getenv("DISABLE_SCHEDULER", "").lower() in ("1", "true", "yes"):
        print("[scheduler] disabled")
        return
    for h in SCAN_HOURS:
        utc_h = (h - TZ_OFFSET) % 24
        scheduler.add_job(scan, CronTrigger(hour=utc_h, minute=0),
                          kwargs={"trigger": f"scheduled-{h:02d}00"},
                          id=f"scan_{h}", replace_existing=True, misfire_grace_time=1800)
    scheduler.start()
    print(f"[scheduler] scans at {SCAN_HOURS} local (UTC+{TZ_OFFSET}) | sources: {ENABLED}")
    # catch-up: process restarts and sleeping hosts miss cron ticks
    try:
        last = db.facets().get("last_run") or {}
        stale = True
        if last.get("finished"):
            dt = datetime.fromisoformat(last["finished"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            stale = (datetime.now(timezone.utc) - dt).total_seconds() > 5 * 3600
        if stale:
            print("[scheduler] last scan stale or missing, launching catch-up scan")
            threading.Thread(target=scan, kwargs={"trigger": "startup-catchup"},
                             daemon=True).start()
    except Exception as e:
        print(f"[scheduler] catch-up check failed: {e}")


# ───────────────────────────────────────────── auth
def authed(request: Request):
    return request.cookies.get("td_token") in _tokens


def require(request: Request):
    if not authed(request):
        raise HTTPException(401, "Not authorised")


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    if (body.get("passphrase") or "").strip() != PASSPHRASE:
        raise HTTPException(401, "Incorrect passphrase")
    tok = secrets.token_urlsafe(24)
    _tokens.add(tok)
    r = JSONResponse({"ok": True})
    r.set_cookie("td_token", tok, max_age=60 * 60 * 12, httponly=True, samesite="lax")
    return r


@app.get("/api/me")
def me(request: Request):
    return {"authed": authed(request), "show_fit": SHOW_FIT}


# ───────────────────────────────────────────── data API
@app.get("/api/facets")
def api_facets(request: Request):
    require(request)
    f = db.facets()
    f["sources_available"] = [
        {"name": n, "tier": S.REGISTRY[n].tier,
         "jurisdiction": S.REGISTRY[n].jurisdiction,
         "confidence": S.REGISTRY[n].confidence,
         "enabled": n in ENABLED}
        for n in S.all_sources()]
    f["scan_hours"] = SCAN_HOURS
    f["running"] = _state["running"]
    return f


@app.get("/api/tenders")
def api_tenders(request: Request, q: str = None, sources: str = None,
                domains: str = None, relevance: str = None, jurisdictions: str = None,
                status: str = "open", closing_within: int = None,
                value_min: float = None, value_max: float = None,
                digital_only: int = 1, new_only: int = 0,
                lanes: str = None, scope: str = "opportunity",
                date_on: str = None, date_from: str = None, date_to: str = None,
                tiers: str = None,
                sort: str = "closing", limit: int = 100, offset: int = 0):
    require(request)
    sp = lambda s: [x for x in s.split(",") if x] if s else None
    rows, total = db.search(
        lanes=sp(lanes), scope=scope, date_on=date_on,
        date_from=date_from, date_to=date_to, tiers=sp(tiers),
        q=q, sources=sp(sources), domains=sp(domains), relevance=sp(relevance),
        jurisdictions=sp(jurisdictions), status=status, closing_within=closing_within,
        value_min=value_min, value_max=value_max, digital_only=bool(digital_only),
        new_only=bool(new_only), sort=sort, limit=min(limit, 300), offset=offset)
    today = datetime.now(timezone.utc).date()
    for r in rows:
        if r.get("closing"):
            try:
                r["days_left"] = (datetime.fromisoformat(r["closing"]).date() - today).days
            except Exception:
                r["days_left"] = None
        else:
            r["days_left"] = None
    return {"rows": rows, "total": total, "offset": offset}


@app.get("/api/tender/{uid:path}")
def api_tender(uid: str, request: Request):
    require(request)
    t = db.get(uid)
    if not t:
        raise HTTPException(404, "Not found")
    return t


@app.get("/api/buyers")
def api_buyers(request: Request, limit: int = 60):
    require(request)
    return {"rows": db.buyers(limit)}


@app.post("/api/scan")
async def api_scan(request: Request):
    require(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    if _state["running"]:
        return {"ok": False, "error": "A scan is already running"}
    threading.Thread(target=scan, kwargs={
        "trigger": "manual-ui", "only": body.get("only"),
        "max_pages": body.get("max_pages")}, daemon=True).start()
    return {"ok": True, "started": True}


@app.get("/api/scan-status")
def api_scan_status(request: Request):
    require(request)
    return {"running": _state["running"], "last": _state["last"]}


# ── Backfill from the browser, no shell needed. Runs in a background thread so
# it never blocks the page, and backfill.py checkpoints after every batch, so
# closing the tab or a host restart resumes rather than restarts.
_bf = {"running": False, "source": None, "note": None}


def _run_backfill(source, pages):
    _bf.update(running=True, source=source, note="starting")
    try:
        import backfill
        if source == "EPADS":
            backfill.sweep_epads(end=1)
        elif source == "WorldBank":
            backfill.sweep_worldbank(pages=pages)
        else:
            backfill.sweep_generic(source, pages=pages)
        _bf["note"] = "complete"
    except Exception as e:
        _bf["note"] = f"error: {type(e).__name__}: {e}"[:200]
        print(f"[backfill] {source} failed: {e}")
    finally:
        _bf["running"] = False


@app.post("/api/backfill")
async def api_backfill(request: Request):
    require(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    source = (body.get("source") or "EPMS-Awards").strip()
    pages = int(body.get("pages") or 200)
    if _bf["running"]:
        return {"ok": False, "error": f"backfill already running ({_bf['source']})"}
    threading.Thread(target=_run_backfill, args=(source, pages), daemon=True).start()
    return {"ok": True, "started": source, "pages": pages}


@app.get("/api/backfill-status")
def api_backfill_status(request: Request):
    require(request)
    rows = []
    try:
        import backfill
        backfill.init()
        with db.conn(readonly=True) as c:
            rows = [dict(r) for r in c.execute(
                "SELECT source, cursor, highest, lowest, rows_added, done, note, updated "
                "FROM backfill ORDER BY updated DESC")]
    except Exception as e:
        rows = [{"source": "error", "note": str(e)[:120]}]
    return {"running": _bf["running"], "source": _bf["source"],
            "note": _bf["note"], "checkpoints": rows}


# ── Import a curated list, an Excel or CSV your team maintains or a newspaper
# extract. Column names are matched loosely, every row is classified on the way
# in, and rows append to the feed. Upload again any time and it keeps updating,
# deduped on the same key as a scrape.
_COL = {
    "title": ["description", "title", "subject", "tender", "detail", "scope"],
    "buyer": ["department", "buyer", "agency", "organization", "organisation",
              "procuring", "client", "entity", "ministry"],
    "closing": ["submission date", "closing", "closing date", "due date",
                "deadline", "submission", "last date"],
    "location": ["location", "region", "jurisdiction", "province", "city"],
    "value_text": ["value", "amount", "estimated value", "cost"],
    "url": ["url", "link", "source url"],
}


def _pick(headers, keys):
    low = {str(h or "").strip().lower(): i for i, h in enumerate(headers)}
    for want in keys:
        for h, i in low.items():
            if want == h or want in h:
                return i
    return None


def _rows_from_upload(path, suffix):
    """Yield dict rows from an uploaded xlsx or csv, across all sheets."""
    out = []
    if suffix in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        sheets = [(s, list(wb[s].iter_rows(values_only=True))) for s in wb.sheetnames]
    else:
        import csv, io
        with open(path, newline="", encoding="utf-8-sig", errors="ignore") as fh:
            sheets = [("csv", [tuple(r) for r in csv.reader(fh)])]
    for _, rows in sheets:
        if not rows or len(rows) < 2:
            continue
        head = rows[0]
        ci = {k: _pick(head, v) for k, v in _COL.items()}
        if ci["title"] is None:
            continue
        for r in rows[1:]:
            if not r or ci["title"] >= len(r):
                continue
            title = r[ci["title"]]
            if not title or not str(title).strip():
                continue
            def g(k):
                i = ci.get(k)
                return str(r[i]).strip() if i is not None and i < len(r) and r[i] is not None else ""
            out.append({
                "title": str(title).strip()[:300],
                "buyer": g("buyer"),
                "closing": _norm_date(g("closing")),
                "jurisdiction": g("location") or "Import",
                "value_text": g("value_text"),
                "url": g("url"),
            })
    return out


def _norm_date(s):
    import re as _re
    from datetime import datetime as _dt
    s = (s or "").strip()
    if not s:
        return None
    s = _re.split(r"\s+at\s+", s, flags=_re.I)[0].strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return _dt.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# ── Newspaper feed controls. Probe reports which papers resolve today, fetch
# downloads and OCRs them in the background, status reports both. All feed into
# the same tenders table, tagged source_tier=Newspaper, so the Newspaper feed
# view queries them like any other tender.
_np = {"running": False, "note": None, "probe": [], "last": None}


def _run_np_fetch():
    _np.update(running=True, note="fetching")
    try:
        import newspaper_fetch
        n = newspaper_fetch.run_fetch()
        _np["note"] = f"done, {n} rows"
        _np["last"] = _np["note"]
    except Exception as e:
        _np["note"] = f"error: {type(e).__name__}"
    finally:
        _np["running"] = False


def _run_np_probe():
    _np.update(running=True, note="probing")
    try:
        import newspaper_fetch
        _np["probe"] = newspaper_fetch.probe_sources()
        ok = sum(1 for p in _np["probe"] if p["ok"])
        _np["note"] = f"{ok} of {len(_np['probe'])} papers resolved"
    except Exception as e:
        _np["note"] = f"error: {type(e).__name__}"
    finally:
        _np["running"] = False


@app.post("/api/newspaper-probe")
async def api_np_probe(request: Request):
    require(request)
    if _np["running"]:
        return {"ok": False, "error": "a newspaper task is already running"}
    threading.Thread(target=_run_np_probe, daemon=True).start()
    return {"ok": True, "started": "probe"}


@app.post("/api/newspaper-fetch")
async def api_np_fetch(request: Request):
    require(request)
    if os.environ.get("NEWSPAPER_FETCH_ENABLED", "1") != "1":
        return {"ok": False, "error": "Automated fetch is off. Set NEWSPAPER_FETCH_ENABLED=1 to enable."}
    if _np["running"]:
        return {"ok": False, "error": "a newspaper task is already running"}
    threading.Thread(target=_run_np_fetch, daemon=True).start()
    return {"ok": True, "started": "fetch"}


# Sample notices for the proof-of-concept view. Real Pakistani-style tender
# adverts, so the demo shows the actual parser and classifier at work rather
# than a mockup. The pipeline that runs on these is the same one that runs on a
# scanned page.
_NP_SAMPLES = {
    "ict": ("PITB — IT procurement",
        "PUNJAB INFORMATION TECHNOLOGY BOARD\nTENDER NOTICE\n"
        "Sealed bids are invited from eligible firms for the Supply, Installation "
        "and Commissioning of SD-WAN Enabled Next Generation Firewalls and Layer-2 "
        "Switches for the Punjab Government Data Centre.\n"
        "Tender No. PITB/IT/2026/114. Estimated cost Rs. 45 million.\n"
        "Bid security 2%. Last date for submission: 12-09-2026 at 11:00 AM."),
    "connectivity": ("PAA — connectivity",
        "PAKISTAN AIRPORTS AUTHORITY\nINVITATION FOR BIDS\n"
        "Provision of Internet Connectivity and MPLS Services at Jinnah "
        "International Airport, Karachi, for a period of three years.\n"
        "Reference PAA-IT-88. Single Stage Two Envelope. "
        "Closing date: 05-09-2026 at 03:00 PM."),
    "software": ("NADRA — systems",
        "NATIONAL DATABASE & REGISTRATION AUTHORITY\nNOTICE INVITING TENDER\n"
        "Procurement of an Enterprise Document Management System with licensing, "
        "support and integration with existing infrastructure.\n"
        "Tender ID NADRA-HQ-DMS-31. Bid security Rs. 500,000. "
        "Submission by 28-08-2026."),
    "civil": ("C&W — civil works",
        "COMMUNICATION & WORKS DEPARTMENT\nTENDER NOTICE\n"
        "Construction of a boundary wall and allied civil works at the district "
        "complex. Estimated cost Rs. 18 million. Bid security 2%. "
        "Last date 09-09-2026. (Included to show what the filter correctly drops.)"),
}


@app.get("/api/newspaper-samples")
def api_np_samples(request: Request):
    require(request)
    return {"samples": [{"id": k, "label": v[0]} for k, v in _NP_SAMPLES.items()]}


@app.post("/api/newspaper-extract")
async def api_np_extract(request: Request):
    require(request)
    body = await request.json()
    text = (body.get("text") or "").strip()
    sample = body.get("sample")
    if sample and sample in _NP_SAMPLES:
        text = _NP_SAMPLES[sample][1]
    if not text:
        return {"ok": False, "error": "Paste a notice or pick a sample."}
    import newspaper, classify
    rows = []
    for b in newspaper.blocks(text):
        r = newspaper.parse_block(b, "Sample", 1)
        if not r:
            continue
        c = classify.classify(r.get("title", ""), "", r.get("buyer", ""))
        rows.append({
            "buyer": r.get("buyer", ""), "title": r.get("title", ""),
            "closing": r.get("closing"), "ref": r.get("ref", ""),
            "value_text": r.get("value_text", ""),
            "lane": c["lane"], "product_line": c.get("product_line", ""),
            "is_opportunity": c["is_opportunity"], "why": c.get("why", ""),
        })
    # if the block splitter found nothing, still classify the whole text so the
    # demo always shows a result
    if not rows:
        c = classify.classify(text[:200], "", "")
        rows.append({"buyer": "", "title": text[:120], "closing": None, "ref": "",
                     "lane": c["lane"], "product_line": c.get("product_line", ""),
                     "is_opportunity": c["is_opportunity"], "why": c.get("why", "")})
    return {"ok": True, "raw": text, "rows": rows}


@app.get("/newspaper", response_class=HTMLResponse)
def newspaper_page():
    pth = os.path.join(os.path.dirname(os.path.abspath(__file__)), "newspaper.html")
    with open(pth, encoding="utf-8") as f:
        return f.read()


@app.get("/api/newspaper-status")
def api_np_status(request: Request):
    require(request)
    with db.conn(readonly=True) as c:
        cnt = c.execute("SELECT COUNT(*) FROM tenders WHERE source_tier='Newspaper'").fetchone()[0]
    return {"running": _np["running"], "note": _np["note"], "probe": _np["probe"],
            "enabled": os.environ.get("NEWSPAPER_FETCH_ENABLED", "0") == "1",
            "rows": cnt}


@app.post("/api/import")
async def api_import(request: Request, file: UploadFile = File(...)):
    require(request)
    name = (file.filename or "").lower()
    suffix = ".xlsx" if name.endswith((".xlsx", ".xlsm")) else \
             ".csv" if name.endswith((".csv", ".tsv", ".txt")) else None
    if not suffix:
        return {"ok": False, "error": "Upload an .xlsx or .csv file."}
    import tempfile, os as _os
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(await file.read())
    tmp.close()
    try:
        rows = _rows_from_upload(tmp.name, suffix)
    finally:
        _os.unlink(tmp.name)
    if not rows:
        return {"ok": False, "error": "No rows found. Need a header row with a description or title column."}
    import classify
    kept = []
    for r in rows:
        c = classify.classify(r["title"], "", r.get("buyer"))
        r.update(c)
        r["source_tier"] = "Import"
        r["ref"] = "IMP-" + str(abs(hash(r["title"] + (r.get("buyer") or ""))) % 10**10)
        kept.append(r)
    seen, new, changed = db.upsert(kept, source="Import")
    opps = sum(1 for r in kept if r.get("is_opportunity"))
    return {"ok": True, "rows": len(kept), "new": new, "updated": changed,
            "opportunities": opps,
            "message": f"{len(kept)} rows read, {new} new, {opps} Jazz-relevant."}


@app.post("/api/tender-detail/{uid:path}")
def api_tender_detail(uid: str, request: Request):
    """Fetch and cache the source page's extra fields for one tender."""
    require(request)
    t = db.get(uid)
    if not t:
        raise HTTPException(404, "Not found")
    if t.get("detail_fetched") or not t.get("url"):
        return t
    d = S.fetch_detail(t["url"])
    if d.get("error"):
        return {**t, "detail_error": d["error"]}
    db.save_detail(uid, d)
    return db.get(uid)


@app.post("/api/reconcile")
async def reconcile(request: Request, file: UploadFile = File(...),
                    format: str = "json"):
    """Check an uploaded tender list against what the portals published.

    Real spreadsheets are messy: headers rarely start at row 1, columns are
    named with spaces or capitals, and dates arrive in several formats with
    stray unicode dashes. This reader finds the header row by scoring rows
    against expected column names, then normalises everything.
    """
    require(request)
    import csv, io, re as _re
    raw = await file.read()
    name = (file.filename or "").lower()

    WANT = ("region", "department", "description", "submission", "closing",
            "title", "buyer", "agency", "date")

    def norm_key(s):
        s = _re.sub(r"[^a-z0-9]+", "_", str(s or "").strip().lower())
        return s.strip("_")

    def header_score(cells):
        vals = [norm_key(c) for c in cells if c not in (None, "")]
        return sum(1 for v in vals if any(w in v for w in WANT))

    rows = []
    sheets_read = []
    if name.endswith((".xlsx", ".xlsm")):
        try:
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            # Trackers are usually one sheet per weekly batch. Reading only the
            # first sheet silently discards most of the list, so every sheet is
            # read and its own header row detected independently.
            for ws in wb.worksheets:
                srows = [list(r) for r in ws.iter_rows(values_only=True)]
                if len(srows) < 2:
                    continue
                hj, bestj = 0, -1
                for i, rr in enumerate(srows[:15]):
                    sc = header_score(rr)
                    if sc > bestj:
                        hj, bestj = i, sc
                if bestj < 2:
                    continue
                sheets_read.append(ws.title)
                rows.append(srows[hj])          # header
                rows.extend(srows[hj + 1:])     # body
            if rows:
                rows = [rows[0]] + [r for i, r in enumerate(rows[1:], 1)
                                    if header_score(r) < 2]
        except ImportError:
            return {"results": [], "error": "openpyxl missing on server; save as CSV"}
        except Exception as e:
            return {"results": [], "error": f"Could not read workbook: {e}"}
    else:
        txt = raw.decode("utf-8-sig", "ignore")
        rows = [r for r in csv.reader(io.StringIO(txt))]

    if not rows:
        return {"results": [], "error": "File appears empty"}

    # header row = best-scoring row within the first 15
    hi, best = 0, -1
    for i, r in enumerate(rows[:15]):
        sc = header_score(r)
        if sc > best:
            hi, best = i, sc
    if best < 2:
        return {"results": [], "error":
                "Could not find a header row. Expected columns like Region, "
                "Department, Description, Submission Date."}

    heads = [norm_key(h) for h in rows[hi]]
    items = []
    for r in rows[hi + 1:]:
        if not any(x not in (None, "") for x in r):
            continue
        items.append({h: ("" if v is None else str(v))
                      for h, v in zip(heads, r) if h})

    def pick(it, *frags):
        for k, v in it.items():
            if v and any(f in k for f in frags):
                return v
        return ""

    DASH = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2212"), "-")

    def clean_date(v):
        s = str(v or "").translate(DASH).strip()
        s = _re.sub(r"\s*at\s*\d{1,2}:\d{2}\s*[APMapm\.]*", "", s)
        s = _re.sub(r"\s+00:00:00$", "", s)
        m = _re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
        if m:
            return m.group(0)
        m = _re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})", s)
        if m:
            d, mo, y = m.groups()
            if len(y) == 2:
                y = "20" + y
            return f"{y}-{int(mo):02d}-{int(d):02d}"
        # two-digit years are the norm in these trackers: 10-Jun-26
        m = _re.search(r"(\d{1,2})[-\s]([A-Za-z]{3,9})[-\s](\d{2})(?!\d)", s)
        if m:
            from datetime import datetime as _dt
            for fmt in ("%d %b %Y", "%d %B %Y"):
                try:
                    return _dt.strptime(
                        f"{m.group(1)} {m.group(2)[:9]} 20{m.group(3)}",
                        fmt).date().isoformat()
                except ValueError:
                    pass
        m = _re.search(r"(\d{1,2})[-\s]([A-Za-z]{3,9})[-\s](\d{4})", s)
        if m:
            from datetime import datetime as _dt
            for fmt in ("%d %b %Y", "%d %B %Y"):
                try:
                    return _dt.strptime(f"{m.group(1)} {m.group(2)[:9]} {m.group(3)}",
                                        fmt).date().isoformat()
                except ValueError:
                    pass
        return ""

    # Some cells hold several tenders: "TENDER NO. 34 ... TENDER NO. 35 ...",
    # or numbered lists. Each is a distinct procurement and must be scored
    # separately, otherwise one input is compared against one record and
    # structurally cannot match.
    SPLIT = _re.compile(r"(?=TENDER\s*NO\.?\s*\d+)|(?=LOT\s*[-#]?\s*\d+\b)"
                        r"|(?=\n\s*0?[1-9]\s*[.)]\s+[A-Z])", _re.I)

    def explode(desc):
        parts = [p.strip(" \n\t-–—:;") for p in SPLIT.split(str(desc or "")) if p]
        parts = [p for p in parts if len(p) >= 18]
        return parts if len(parts) > 1 else [str(desc or "")]

    norm = []
    for it in items[:600]:
        desc = pick(it, "description", "title", "subject", "tender", "scope")
        if not desc:
            continue
        region = pick(it, "region", "province", "city", "zone")
        dept = pick(it, "department", "buyer", "organization",
                    "organisation", "agency", "entity")
        sdate = clean_date(pick(it, "submission", "closing",
                                "due", "deadline", "date"))
        for part in explode(desc):
            norm.append({"region": region, "department": dept,
                         "description": part, "submission_date": sdate})

    if not norm:
        return {"results": [], "error": "No usable rows found under the header"}

    cov = db.coverage_start()
    res = db.match_rows(norm, coverage_from=cov.get("first_scan"))
    summary = {s: sum(1 for r in res if r["status"] == s)
               for s in ("found", "possible", "missing", "insufficient")}
    # miss rate over what was actually collectable
    scoreable = summary["found"] + summary["possible"] + summary["missing"]
    summary["collectable"] = scoreable
    summary["hit_rate"] = (round(100 * (summary["found"] + summary["possible"])
                                 / scoreable) if scoreable else 0)

    # inverse audit: what the portals carried that the list does not
    matched = [r["match"]["uid"] for r in res if r.get("match")]
    dates = sorted(d for d in (x["submission_date"] for x in norm) if d)
    missed = db.unmatched_opportunities(
        matched, date_from=dates[0] if dates else None,
        date_to=dates[-1] if dates else None)

    payload = {"results": res, "summary": summary, "count": len(res),
               "header_row": hi + 1, "columns": [h for h in heads if h],
               "missed": missed, "missed_count": len(missed),
               "coverage": cov,
               "sheets_read": sheets_read,
               "rows_expanded": len(norm) - len(items[:600]),
               "window": {"from": dates[0] if dates else None,
                          "to": dates[-1] if dates else None}}

    if format.lower() in ("xlsx", "excel"):
        return _reconcile_workbook(payload, file.filename or "list")
    return payload


def _reconcile_workbook(p, source_name):
    """Build a three-sheet reconciliation workbook.

    Summary     the counts, so the result is readable at a glance
    Your list   every uploaded row with its status, confidence and match
    Missed      biddable tenders in the window that were not on the list
    """
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    HEAD_FILL = PatternFill("solid", fgColor="0B4F5F")
    HEAD_FONT = Font(color="FFFFFF", bold=True, size=10)
    STATUS_FILL = {"found": PatternFill("solid", fgColor="D8F0EA"),
                   "possible": PatternFill("solid", fgColor="FBEFD6"),
                   "missing": PatternFill("solid", fgColor="F6DEDB"),
                   "insufficient": PatternFill("solid", fgColor="E8E8E8"),
                   "before coverage": PatternFill("solid", fgColor="DCE6F1")}

    def style_header(ws, ncols):
        for c in range(1, ncols + 1):
            cell = ws.cell(row=1, column=c)
            cell.fill = HEAD_FILL
            cell.font = HEAD_FONT
            cell.alignment = Alignment(vertical="center")
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    def widths(ws, sizes):
        for i, w in enumerate(sizes, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

    wb = Workbook()

    # ── Summary
    ws = wb.active
    ws.title = "Summary"
    s = p["summary"]
    w = p.get("window") or {}
    rows = [
        ("Reconciliation report", ""),
        ("Source file", source_name),
        ("Generated", datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")),
        ("Comparison window", f"{w.get('from') or '-'} to {w.get('to') or '-'}"),
        ("Database coverage begins", (p.get("coverage") or {}).get("first_scan") or "-"),
        ("Rows added by splitting bundles", p.get("rows_expanded", 0)),
        ("Sheets read", ", ".join(p.get("sheets_read") or []) or "single sheet"),
        ("", ""),
        ("Rows checked", p["count"]),
        ("Found on portals", s.get("found", 0)),
        ("Possible match, verify", s.get("possible", 0)),
        ("Not located", s.get("missing", 0)),
        ("Too vague to match", s.get("insufficient", 0)),
        ("", ""),
        ("Collectable rows", s.get("collectable", 0)),
        ("Hit rate on collectable rows", f"{s.get('hit_rate', 0)}%"),
        ("", ""),
        ("Biddable tenders NOT on your list", p.get("missed_count", 0)),
        ("", ""),
        ("How to read this", ""),
        ("Found", "Confidence 0.60 or above. Treat as confirmed."),
        ("Possible", "Confidence 0.32 to 0.62. Subject wording did not agree strongly "
                     "enough to confirm, even where the buyer or date did. Check the "
                     "'Why matched' and 'Other candidates' columns."),
        ("How found is decided", "Matching rests on SHARED INFORMATION, not similarity. "
                                 "Two titles sharing 'procurement of IT equipment' are similar "
                                 "but share no information, since those words appear in "
                                 "hundreds of tenders. A match must share distinctive words "
                                 "(above the corpus median), the portal title must itself be "
                                 "informative, and the buyer or closing date must corroborate. "
                                 "Your description is compared against our title and our stored "
                                 "description. The closing date is a tiebreaker only, because "
                                 "agencies cluster bid openings on common days and internal "
                                 "lists carry internal deadlines."),
        ("Too vague", "Under three meaningful words, or a stray header row. Not scored, "
                      "because such text matches almost anything at random."),
        ("Not located", "Scored below the possible threshold against everything "
                        "collected. Either absent from the scanned sources, or the "
                        "description differs too much from the published title."),

        ("Missed sheet", "Core and Partner-led tenders inside the window that do not "
                         "appear on your list. This is the coverage gap."),
    ]
    for r in rows:
        ws.append(list(r))
    ws["A1"].font = Font(bold=True, size=14)
    ws["A13"].font = Font(bold=True, size=11)
    for rr in (6, 7, 8, 9, 11):
        ws.cell(row=rr, column=1).font = Font(bold=True)
        ws.cell(row=rr, column=2).font = Font(bold=True)
    ws.cell(row=11, column=2).font = Font(bold=True, color="0B4F5F", size=12)
    widths(ws, [34, 78])

    # ── Your list
    ws = wb.create_sheet("Your list")
    cols = ["Status", "Confidence", "Region", "Department", "Description",
            "Submission date", "Matched title", "Portal ref", "Source",
            "Lane", "Closing on portal", "Why matched", "Matched on (key words)",
            "Buyer agreement", "Date gap (days)", "Other candidates to check", "Link"]
    ws.append(cols)
    for r in p["results"]:
        i, m = r["input"], (r.get("match") or {})
        cands = r.get("candidates") or []
        alts = " || ".join(
            f"[{c.get('score')}] {str(c.get('ref') or '')} {str(c.get('title') or '')[:70]}"
            for c in cands[1:3])
        ws.append([r["status"], r["confidence"], i.get("region", ""),
                   i.get("department", ""), i.get("description", ""),
                   i.get("submission_date", ""), m.get("title", ""),
                   m.get("ref", ""), m.get("source", ""), m.get("lane", ""),
                   m.get("closing", ""), r.get("why", ""),
                   (r.get("why", "").split("on: ")[1].split(",")[0:3]
                    and ", ".join(r.get("why", "").split("on: ")[1].split(", ")[:3])
                    if "on: " in r.get("why", "") else ""),
                   cands[0].get("buyer_score") if cands else "",
                   cands[0].get("date_gap") if cands else "",
                   alts, m.get("url", "")])
    for row in ws.iter_rows(min_row=2, max_col=1):
        f = STATUS_FILL.get(row[0].value)
        if f:
            row[0].fill = f
    style_header(ws, len(cols))
    widths(ws, [13, 11, 12, 32, 56, 15, 46, 16, 12, 12, 15, 40, 26, 15, 14, 60, 40])

    # ── Missed
    ws = wb.create_sheet("Missed by us")
    cols = ["Fit", "Lane", "Product line", "Title", "Buyer", "Source",
            "Jurisdiction", "Advertised", "Closing", "Value", "Ref", "Link"]
    ws.append(cols)
    for m in p.get("missed", []):
        ws.append([m.get("fit_score"), m.get("lane"), m.get("product_line"),
                   m.get("title"), m.get("buyer"), m.get("source"),
                   m.get("jurisdiction"), m.get("advertised"), m.get("closing"),
                   m.get("value_text"), m.get("ref"), m.get("url")])
    style_header(ws, len(cols))
    widths(ws, [7, 13, 20, 56, 36, 12, 14, 12, 12, 16, 16, 40])

    buf = io.BytesIO()
    wb.save(buf)
    fname = f"reconciliation_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.xlsx"
    return Response(
        buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/api/brief")
def api_brief(request: Request, days: int = 2, closing_days: int = 7):
    require(request)
    return db.brief(days_new=days, closing_days=closing_days)


@app.post("/api/reconcile-export")
async def reconcile_export(request: Request):
    """Build the workbook from results already computed in the browser.

    The earlier version re-sent the file, which failed whenever the file
    input had been cleared by navigation. This takes the JSON payload
    instead, so the download never depends on browser state.
    """
    require(request)
    payload = await request.json()
    return _reconcile_workbook(payload, payload.get("source_name") or "list")


@app.post("/api/newspaper")
async def newspaper_ingest(request: Request, file: UploadFile = File(...),
                           paper: str = "Newspaper", commit: int = 0):
    """Read tender advertisements from a newspaper page.

    Preview by default. Rows are only written when commit=1, and they are
    stored with needs_review set, because OCR of a broadsheet is noisier
    than a portal scrape and should be checked before it drives a decision.
    """
    require(request)
    import newspaper as NP
    import tempfile, os as _os
    suffix = _os.path.splitext(file.filename or "page.pdf")[1] or ".pdf"
    raw = await file.read()
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(raw); tmp.close()
    try:
        rows, diag = NP.ingest(tmp.path if hasattr(tmp, "path") else tmp.name,
                               paper=paper)
    except Exception as e:
        return {"rows": [], "error": f"{type(e).__name__}: {e}"}
    finally:
        try:
            _os.unlink(tmp.name)
        except Exception:
            pass
    if diag.get("error"):
        return {"rows": [], "error": diag["error"]}
    saved = 0
    if commit:
        for r in rows:
            r["needs_review"] = 1
        _s, saved, _c = db.upsert(rows, f"News-{paper}")
        db.record_health(f"News-{paper}", True, len(rows),
                         f"{saved} new from {file.filename}", 0)
    return {"rows": rows, "diagnostics": diag, "saved": saved,
            "committed": bool(commit)}


@app.get("/api/intelligence")
def api_intelligence(request: Request, horizon: int = 540):
    require(request)
    return db.intelligence(horizon_days=horizon)


@app.get("/api/analytics")
def api_analytics(request: Request, limit: int = 4000):
    """Point-level data for the Analytics view: one row per tender and award,
    with a date, a value, and the dimensions you can colour by. The front end
    does the plotting, so this stays a clean data feed the dashboard can lean on
    as the underlying tables keep updating."""
    require(request)
    out = {"tenders": [], "awards": []}
    with db.conn(readonly=True) as c:
        for r in c.execute(f"""
            SELECT COALESCE(advertised, first_seen, closing) d, value_num v,
                   product_line cat, lane, buyer, source, is_opportunity opp,
                   substr(title,1,90) title
            FROM tenders
            WHERE dup_of IS NULL AND COALESCE(advertised, first_seen, closing) IS NOT NULL
            ORDER BY d DESC LIMIT ?""", (limit,)):
            out["tenders"].append(dict(r))
        for r in c.execute(f"""
            SELECT award_date d, value_num v, product_line cat, lane,
                   buyer, winner, source, is_opportunity opp, substr(title,1,90) title
            FROM awards
            WHERE award_date IS NOT NULL
            ORDER BY award_date DESC LIMIT ?""", (limit,)):
            out["awards"].append(dict(r))
    # light server-side rollups so the cards render instantly
    def rollup(rows, dim):
        agg = {}
        for r in rows:
            k = (r.get(dim) or "Unclassified") if dim != "buyer" else (r.get("buyer") or "Unknown")
            a = agg.setdefault(k, {"n": 0, "value": 0.0})
            a["n"] += 1
            a["value"] += r.get("v") or 0
        return sorted(([k, x["n"], round(x["value"])] for k, x in agg.items()),
                      key=lambda z: z[1], reverse=True)
    classified = [r for r in out["tenders"] if (r.get("cat") or "").strip()]
    out["meta"] = {
        "tender_count": len(out["tenders"]),
        "classified_count": len(classified),
        "award_count": len(out["awards"]),
        "by_category": rollup(classified, "cat")[:10],
        "by_lane": rollup(out["tenders"], "lane"),
        "top_buyers": rollup(out["tenders"], "buyer")[:10],
        "award_value": round(sum(r.get("v") or 0 for r in out["awards"])),
    }
    return out


@app.get("/analytics", response_class=HTMLResponse)
def analytics_page():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analytics.html")
    with open(p, encoding="utf-8") as f:
        return f.read()


@app.get("/api/overview")
def api_overview(request: Request):
    require(request)
    return db.overview()


@app.get("/api/export")
def api_export(request: Request, q: str = None, sources: str = None,
               domains: str = None, relevance: str = None, jurisdictions: str = None,
               status: str = "open", closing_within: int = None,
               digital_only: int = 1, sort: str = "closing",
               lanes: str = None, scope: str = "opportunity"):
    """CSV of the current filtered set."""
    require(request)
    import csv, io
    sp = lambda s: [x for x in s.split(",") if x] if s else None
    rows, _ = db.search(lanes=sp(lanes), scope=scope, digital_only=False,
                        q=q, sources=sp(sources), domains=sp(domains),
                        relevance=sp(relevance), jurisdictions=sp(jurisdictions),
                        status=status, closing_within=closing_within,
                        sort=sort, limit=5000)
    cols = ["source", "ref", "title", "buyer", "jurisdiction", "lane", "product_line",
            "fit_score", "digital_domain",
            "relevance", "advertised", "closing", "value_text", "value_num",
            "status", "url", "digital_evidence"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=tenders.csv"})


@app.get("/api/export-history")
def api_export_history(request: Request, days: int = 365):
    """Historical awards and tenders as a forecasting workbook.

    Awards are the forecasting backbone: they carry value, buyer and date.
    Tenders are the demand signal. Both are included over a window of at least
    one year, enforced so the export is always a usable modelling base.
    """
    require(request)
    days = max(365, int(days or 365))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    return _history_workbook(since, days)


def _history_workbook(since, days):
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    HEAD_FILL = PatternFill("solid", fgColor="0B4F5F")
    HEAD_FONT = Font(color="FFFFFF", bold=True, size=10)

    def sheet(ws, cols, rows):
        ws.append(cols)
        for c in range(1, len(cols) + 1):
            cell = ws.cell(row=1, column=c)
            cell.fill = HEAD_FILL
            cell.font = HEAD_FONT
            cell.alignment = Alignment(vertical="center")
        for r in rows:
            ws.append([r.get(k) for k in cols])
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = ws.dimensions
        for i, k in enumerate(cols, start=1):
            width = 42 if k == "title" else 26 if k in ("buyer", "winner") else 14
            ws.column_dimensions[get_column_letter(i)].width = width

    award_cols = ["source", "ref", "title", "buyer", "winner", "value_text",
                  "value_num", "bids_received", "award_date", "tenure_months",
                  "renewal_due", "lane", "product_line", "is_opportunity", "url"]
    tender_cols = ["source", "ref", "title", "buyer", "jurisdiction", "lane",
                   "product_line", "fit_score", "relevance", "advertised", "closing",
                   "value_text", "value_num", "status", "url", "first_seen"]

    with db.conn(readonly=True) as c:
        awards = [dict(r) for r in c.execute(
            f"""SELECT {','.join(award_cols)} FROM awards
                WHERE award_date >= ? OR award_date IS NULL
                ORDER BY award_date DESC""", (since,))]
        tenders = [dict(r) for r in c.execute(
            f"""SELECT {','.join(tender_cols)} FROM tenders
                WHERE first_seen >= ? AND dup_of IS NULL
                ORDER BY first_seen DESC""", (since,))]

    wb = Workbook()
    ws = wb.active
    ws.title = "Readme"
    for row in [
        ("Tender Desk — historical export", ""),
        ("Generated", datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")),
        ("Window", f"{since} to today ({days} days)"),
        ("Awards rows", len(awards)),
        ("Tenders rows", len(tenders)),
        ("", ""),
        ("For forecasting", "Awards are the modelling backbone: value_num, buyer, award_date."),
        ("Renewals", "renewal_due and tenure_months are populated only where the award notice stated a term."),
        ("Demand", "The Tenders sheet is the leading signal: what is being sought, by whom, when."),
    ]:
        ws.append(row)
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 70

    sheet(wb.create_sheet("Awards"), award_cols, awards)
    sheet(wb.create_sheet("Tenders"), tender_cols, tenders)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"tender_desk_history_{datetime.now(timezone.utc).strftime('%Y%m%d')}.xlsx"
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})




# ─────────────────────────────────────────── document filler
import shutil

TPL_DIR = os.path.join(os.path.dirname(os.path.abspath(db.DB_PATH)) or ".", "templates")
os.makedirs(TPL_DIR, exist_ok=True)


@app.get("/api/templates")
def list_templates(request: Request):
    require(request)
    out = []
    for f in sorted(os.listdir(TPL_DIR)):
        if f.lower().endswith(".docx"):
            p = os.path.join(TPL_DIR, f)
            out.append({"name": f, "size": os.path.getsize(p)})
    return {"templates": out}


@app.post("/api/templates")
async def upload_template(request: Request, file: UploadFile = File(...)):
    require(request)
    name = os.path.basename(file.filename or "template.docx")
    if not name.lower().endswith(".docx"):
        raise HTTPException(400, "Only .docx templates are supported")
    dest = os.path.join(TPL_DIR, name)
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"ok": True, "name": name}


@app.delete("/api/templates/{name}")
def delete_template(name: str, request: Request):
    require(request)
    p = os.path.join(TPL_DIR, os.path.basename(name))
    if os.path.exists(p):
        os.remove(p)
    return {"ok": True}


FILL_FIELDS = ["title", "ref", "buyer", "source", "jurisdiction", "advertised",
               "closing", "closing_time", "value_text", "status", "url",
               "contact_name", "contact_email", "contact_phone", "product_line",
               "lane", "description"]


@app.get("/api/fill")
def fill_document(request: Request, uid: str, template: str):
    """Fill a .docx template's {{placeholders}} with one tender's fields.

    Works by rewriting word/document.xml inside the docx zip. Placeholders
    must be typed in one go in Word (no formatting change mid-placeholder),
    otherwise Word splits them across runs and the replace cannot see them.
    """
    require(request)
    import zipfile, io
    from datetime import date as _d
    t = db.get(uid)
    if not t:
        raise HTTPException(404, "Tender not found")
    src = os.path.join(TPL_DIR, os.path.basename(template))
    if not os.path.exists(src):
        raise HTTPException(404, "Template not found")

    def x(s):
        return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))

    repl = {f"{{{{{k}}}}}": x(t.get(k)) for k in FILL_FIELDS}
    repl["{{today}}"] = _d.today().strftime("%d %B %Y")
    repl["{{value}}"] = x(t.get("value_text") or "")

    out = io.BytesIO()
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename in ("word/document.xml", "word/header1.xml",
                                 "word/footer1.xml"):
                s = data.decode("utf-8", "ignore")
                for k, v in repl.items():
                    s = s.replace(k, v)
                data = s.encode("utf-8")
            zout.writestr(item, data)
    fname = f"filled_{(t.get('ref') or 'tender').replace('/', '-')}.docx"
    return Response(out.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ─────────────────────────────────────────── LLM layer (Claude)
LLM_MODEL = os.getenv("LLM_MODEL", "claude-opus-4-6")


@app.post("/api/ask")
async def ask(request: Request):
    """Answer a question about the tender database using Claude.

    The API key is NOT stored server-side: the UI sends it per request and
    it lives only in the user's browser localStorage. Context is built
    server-side from real rows, so answers are grounded in the data.
    """
    require(request)
    body = await request.json()
    q = (body.get("question") or "").strip()
    key = (body.get("api_key") or os.getenv("ANTHROPIC_API_KEY", "")).strip()
    if not q:
        return {"answer": "Ask a question about the tenders."}
    if not key:
        return {"answer": "No API key set. Paste your Anthropic key in the field above."}

    # Ground the model in the actual pipeline, not one keyword query. The
    # previous version searched on words from the question, found nothing for
    # phrasings like "what should Jazz look at", and the model then honestly
    # reported that there was nothing.
    import re as _re, requests as _rq, json as _json
    f = db.facets()

    core, _ = db.search(lanes=["Core"], status="open", sort="fit",
                        digital_only=False, limit=30)
    partner, _ = db.search(lanes=["Partner-led"], status="open", sort="fit",
                           digital_only=False, limit=15)
    soon, _ = db.search(scope="opportunity", status="open", closing_within=14,
                        digital_only=False, sort="closing", limit=15)

    hits = []
    words = [w for w in _re.findall(r"[a-zA-Z]{4,}", q)
             if w.lower() not in ("what", "which", "tenders", "tender", "show",
                                  "many", "have", "does", "with", "that", "from",
                                  "open", "closing", "list", "give", "should",
                                  "look", "jazz", "jazzworld", "there", "about")]
    for w in words[:4]:
        try:
            r_, _n = db.search(q=w, scope="all", digital_only=False,
                               status="", limit=10)
            hits.extend(r_)
        except Exception:
            pass

    def slim(rows):
        seen, out_ = set(), []
        for r in rows:
            if r.get("uid") in seen:
                continue
            seen.add(r.get("uid"))
            out_.append({k: r.get(k) for k in
                         ("source", "ref", "title", "buyer", "lane", "product_line",
                          "fit_score", "advertised", "closing", "value_text",
                          "jurisdiction")})
        return out_

    system = ("You are the analyst behind Tender Desk, a Pakistani government "
              "procurement intelligence tool built for Jazz (a telecom operator). "
              "Lanes: Core = Jazz can bid directly (connectivity, IoT, messaging, "
              "DFS, cloud). Partner-led = consortium needed. Signal = not biddable "
              "but shows digitisation budget. Answer ONLY from the data provided. "
              "You are given the current open pipeline in full, so answer from it "
              "directly. Name specific tenders with buyer and closing date. Only say "
              "nothing is relevant if the lists provided are genuinely empty. Be "
              "concise, lead with the answer, use plain prose, no bullet dumps. "
              "When a comparison across categories, buyers, sources or time would "
              "read better as a chart, append AFTER your prose a single fenced "
              "block exactly like ```chart {\"type\":\"bar\",\"x\":[\"A\",\"B\"],"
              "\"y\":[10,20],\"title\":\"...\"}``` using only numbers that appear "
              "in the data provided. Keep x and y to at most 10 items. Omit the "
              "block entirely when a chart would not add anything.")
    context = (
        f"DATABASE STATS: {_json.dumps(dict(f['stats']))}\n"
        f"LANE COUNTS: {_json.dumps(f.get('lanes'))}\n"
        f"PRODUCT LINES: {_json.dumps(f.get('domains'))}\n"
        f"SOURCES: {_json.dumps(f.get('sources'))}\n\n"
        f"OPEN CORE OPPORTUNITIES, best fit first ({len(core)}):\n"
        f"{_json.dumps(slim(core), default=str)}\n\n"
        f"OPEN PARTNER-LED OPPORTUNITIES ({len(partner)}):\n"
        f"{_json.dumps(slim(partner), default=str)}\n\n"
        f"CLOSING WITHIN 14 DAYS ({len(soon)}):\n"
        f"{_json.dumps(slim(soon), default=str)}\n\n"
        f"KEYWORD MATCHES FOR THIS QUESTION ({len(hits)}):\n"
        f"{_json.dumps(slim(hits)[:20], default=str)}")
    try:
        r = _rq.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": LLM_MODEL, "max_tokens": 900, "system": system,
                  "messages": [{"role": "user",
                                "content": f"{context}\n\nQUESTION: {q}"}]},
            timeout=60)
        d = r.json()
        if r.status_code != 200:
            msg = d.get("error", {}).get("message", "API error")
            return {"answer": f"Claude API error: {msg[:200]}"}
        text = "".join(b.get("text", "") for b in d.get("content", []))
        chart = None
        m = _re.search(r"```chart\s*(\{.*?\})\s*```", text, _re.S)
        if m:
            try:
                chart = _json.loads(m.group(1))
            except Exception:
                chart = None
            text = text[:m.start()].rstrip()
        return {"answer": text, "chart": chart}
    except Exception as e:
        return {"answer": f"Could not reach the Claude API: {type(e).__name__}"}


@app.get("/api/cron")
def cron(key: str = ""):
    """For external pingers (e.g. cron-job.org). Hitting this wakes the
    service on sleep-prone hosts and triggers a scan if none is running.
    Uses the passphrase as the key so the URL is not anonymous."""
    if key != PASSPHRASE:
        raise HTTPException(401, "Bad key")
    if _state["running"]:
        return {"ok": True, "already_running": True}
    threading.Thread(target=scan, kwargs={"trigger": "external-cron"},
                     daemon=True).start()
    return {"ok": True, "started": True}


@app.get("/api/debug")
def api_debug(request: Request, key: str = ""):
    """Ground truth: what the DB holds vs what queries return. For diagnosis."""
    if key != PASSPHRASE:
        require(request)
    with db.conn(readonly=True) as c:
        per_source = [dict(r) for r in c.execute(
            "SELECT source, COUNT(*) rows, SUM(is_opportunity) opps, "
            "SUM(CASE WHEN closing IS NULL THEN 1 ELSE 0 END) no_closing, "
            "SUM(CASE WHEN closing < date('now') THEN 1 ELSE 0 END) past_closing "
            "FROM tenders GROUP BY source")]
        wb = [dict(r) for r in c.execute(
            "SELECT ref, substr(title,1,60) title, advertised, closing, lane "
            "FROM tenders WHERE source='WorldBank' LIMIT 3")]
        total = c.execute("SELECT COUNT(*) n FROM tenders").fetchone()["n"]
    q_all, t_all = db.search(scope="all", digital_only=False, status="", limit=1)
    q_wb, t_wb = db.search(sources=["WorldBank"], scope="all",
                           digital_only=False, status="", limit=1)
    return {"table_total": total, "per_source": per_source,
            "query_scope_all_total": t_all,
            "query_worldbank_all_total": t_wb,
            "worldbank_sample": wb}


@app.get("/health")
def health():
    try:
        f = db.facets()
        return {"ok": True, "tenders": f["stats"]["total"],
                "digital": f["stats"]["digital"], "last_run": f.get("last_run")}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/", response_class=HTMLResponse)
def index():
    with open(UI_FILE, encoding="utf-8") as f:
        return f.read()
