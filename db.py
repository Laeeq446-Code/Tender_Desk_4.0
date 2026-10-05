"""
db.py — SQLite storage, deduplication, and the query layer behind the UI.

WHY DEDUPE MATTERS
    The same tender appears on EPADS, on a provincial portal, and on an
    aggregator with three different reference numbers. Without a dedupe key
    the feed looks noisy and people stop trusting it. We hash a normalised
    (title + buyer + closing date) triple and keep the first source that saw
    it, recording the others as alternates.

SOURCE HEALTH
    Every scrape run writes a row per source. When a portal changes its HTML
    and an adapter silently starts returning zero, the UI shows it as stale
    rather than implying "no new tenders".
"""

import hashlib
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.getenv("DB_PATH", "tenders.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenders (
    uid              TEXT PRIMARY KEY,   -- source + native ref
    dedupe_key       TEXT,               -- cross-source identity hash
    source           TEXT NOT NULL,      -- EPMS, EPADS, PPRA-Punjab, WorldBank...
    source_tier      TEXT,               -- Federal | Provincial | Multilateral
    jurisdiction     TEXT,               -- Federal, Punjab, Sindh, KP, Balochistan, International
    ref              TEXT,
    title            TEXT,
    description      TEXT,
    buyer            TEXT,
    sector_label     TEXT,
    category         TEXT,               -- Goods | Works | Services | Consultancy
    status           TEXT,
    advertised       TEXT,               -- ISO date
    closing          TEXT,               -- ISO date
    closing_time     TEXT,
    value_text       TEXT,               -- as published
    value_num        REAL,               -- normalised to PKR where derivable
    currency         TEXT,
    is_digital       INTEGER DEFAULT 0,
    is_opportunity   INTEGER DEFAULT 0,
    lane             TEXT,
    product_line     TEXT,
    fit_score        INTEGER DEFAULT 0,
    digital_domain   TEXT,
    digital_evidence TEXT,
    relevance        TEXT,
    rationale        TEXT,
    url              TEXT,
    doc_url          TEXT,
    contact_name     TEXT,
    contact_email    TEXT,
    contact_phone    TEXT,
    scope            TEXT,
    eligibility      TEXT,
    bid_security     TEXT,
    tender_fee       TEXT,
    est_value_low    REAL,
    est_value_high   REAL,
    prebid           TEXT,
    opening          TEXT,
    venue            TEXT,
    attachments      TEXT,
    detail_fetched   TEXT,
    repeat_of        TEXT,
    dup_of           TEXT,
    needs_review     INTEGER DEFAULT 0,
    raw              TEXT,
    first_seen       TEXT,
    last_seen        TEXT,
    is_new           INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS awards (
    uid          TEXT PRIMARY KEY,
    source       TEXT,
    ref          TEXT,
    title        TEXT,
    buyer        TEXT,
    winner       TEXT,
    value_text   TEXT,
    value_num    REAL,
    bids_received INTEGER,
    award_date   TEXT,
    tenure_months INTEGER,      -- inferred from wording
    renewal_due  TEXT,          -- award_date + tenure
    lane         TEXT,
    product_line TEXT,
    is_opportunity INTEGER DEFAULT 0,
    url          TEXT,
    first_seen   TEXT
);

CREATE INDEX IF NOT EXISTS ix_aw_renewal ON awards(renewal_due);
CREATE INDEX IF NOT EXISTS ix_aw_winner  ON awards(winner);
CREATE INDEX IF NOT EXISTS ix_aw_buyer   ON awards(buyer);

CREATE TABLE IF NOT EXISTS alternates (
    dedupe_key TEXT, source TEXT, ref TEXT, url TEXT,
    PRIMARY KEY (dedupe_key, source, ref)
);

CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT, field TEXT, old_value TEXT, new_value TEXT, seen_at TEXT
);

CREATE TABLE IF NOT EXISTS source_health (
    source TEXT PRIMARY KEY,
    last_run TEXT, last_ok TEXT, rows_last_run INTEGER,
    status TEXT, message TEXT, total_rows INTEGER, avg_ms INTEGER
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started TEXT, finished TEXT, sources_ok INTEGER, sources_failed INTEGER,
    rows_seen INTEGER, rows_new INTEGER, trigger TEXT
);

CREATE INDEX IF NOT EXISTS ix_close   ON tenders(closing);
CREATE INDEX IF NOT EXISTS ix_dig     ON tenders(is_digital);
CREATE INDEX IF NOT EXISTS ix_rel     ON tenders(relevance);
CREATE INDEX IF NOT EXISTS ix_src     ON tenders(source);
CREATE INDEX IF NOT EXISTS ix_juris   ON tenders(jurisdiction);
CREATE INDEX IF NOT EXISTS ix_new     ON tenders(is_new);
CREATE INDEX IF NOT EXISTS ix_dedupe  ON tenders(dedupe_key);
CREATE INDEX IF NOT EXISTS ix_lane    ON tenders(lane);
CREATE INDEX IF NOT EXISTS ix_opp     ON tenders(is_opportunity);
CREATE INDEX IF NOT EXISTS ix_fit     ON tenders(fit_score);
"""

FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS tenders_fts USING fts5(
    title, description, buyer, ref,
    content='tenders', content_rowid='rowid'
);
"""

TRACKED_FIELDS = ["closing", "status", "value_text", "title"]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def conn(readonly=False):
    if readonly:
        c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    else:
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
    c.row_factory = sqlite3.Row
    try:
        yield c
        if not readonly:
            c.commit()
    finally:
        c.close()


# Columns added after the original schema. init() adds any that are missing,
# so a schema change never again requires wiping the volume.
MIGRATIONS = {
    "tenders": [("buyer_norm", "TEXT"), ("buyer_sector", "TEXT"),
                ("tender_type", "TEXT"), ("dup_of", "TEXT"),
                ("needs_review", "INTEGER DEFAULT 0")],
    "awards": [("buyer_norm", "TEXT"), ("buyer_sector", "TEXT"),
               ("tenure_basis", "TEXT"), ("signing_date", "TEXT")],
}

OPEN_SQL = ("(closing >= date('now') OR (closing IS NULL AND "
            "COALESCE(advertised, substr(first_seen,1,10)) >= date('now','-45 day')))")


def _ensure_dir():
    d_ = os.path.dirname(os.path.abspath(DB_PATH))
    if d_ and not os.path.isdir(d_):
        os.makedirs(d_, exist_ok=True)


def init():
    _ensure_dir()
    with conn() as c:
        c.executescript(SCHEMA)
        c.executescript(FTS)
        c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            for name, typ in cols:
                if name not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
        c.execute("CREATE INDEX IF NOT EXISTS ix_bnorm ON tenders(buyer_norm)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_sector ON tenders(buyer_sector)")


def meta_get(k):
    with conn(readonly=True) as c:
        r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else None


def meta_set(k, v):
    with conn() as c:
        c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                  (k, str(v)))


def fts_rebuild():
    with conn() as c:
        c.execute("INSERT INTO tenders_fts(tenders_fts) VALUES('rebuild')")


def _fts_quote(q):
    """Make free text safe for FTS5.

    Raw input went straight into MATCH, so any slash, apostrophe, hyphen or
    unbalanced quote raised an error: searching "fibre-optic" or "IT/Internet"
    crashed the feed. Each word is quoted as a literal; '*' stays a prefix.
    """
    toks = re.findall(r"[A-Za-z0-9]+\*?", str(q or ""))
    if not toks:
        return None
    return " AND ".join(f'"{t[:-1]}"*' if t.endswith("*") else f'"{t}"' for t in toks)


CLASS_FIELDS = ("lane", "product_line", "is_digital", "is_opportunity", "fit_score",
                "digital_domain", "digital_evidence", "relevance", "rationale",
                "buyer_norm", "buyer_sector", "tender_type")


def _class_update(c, uid, r):
    c.execute(f"UPDATE tenders SET {', '.join(f + '=?' for f in CLASS_FIELDS)} WHERE uid=?",
              tuple(r.get(f) for f in CLASS_FIELDS) + (uid,))


def reclassify_all(force=False):
    """Re-run the current classifier over every stored row.

    Rows previously kept the lane assigned when first scraped, so classifier
    fixes never reached existing data. Runs whenever the classifier version
    changes; cheap, because it is local computation only.
    """
    from classify import classify, CLASSIFIER_VERSION, normalize_buyer, buyer_sector
    if not force and meta_get("classifier_version") == CLASSIFIER_VERSION:
        return 0
    n = 0
    with conn() as c:
        rows = c.execute("SELECT uid, title, description, buyer, sector_label FROM tenders").fetchall()
        for r in rows:
            _class_update(c, r["uid"], classify(r["title"], r["description"],
                                                r["buyer"], r["sector_label"]))
            n += 1
        for a in c.execute("SELECT uid, title, buyer FROM awards").fetchall():
            cl = classify(a["title"], "", a["buyer"])
            c.execute("""UPDATE awards SET lane=?, product_line=?, is_opportunity=?,
                         buyer_norm=?, buyer_sector=? WHERE uid=?""",
                      (cl["lane"], cl["product_line"], cl["is_opportunity"],
                       cl["buyer_norm"], cl["buyer_sector"], a["uid"]))
    meta_set("classifier_version", CLASSIFIER_VERSION)
    fts_rebuild()
    return n


_STOP = re.compile(r"\b(supply|installation|procurement|of|for|the|and|services?|"
                   r"tender|notice|invitation|bids?|works?|providing|hiring)\b", re.I)


def make_dedupe_key(title, buyer, closing):
    """Normalised identity across portals."""
    t = _STOP.sub(" ", str(title or "").lower())
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    t = " ".join(sorted(t.split()))[:180]
    b = re.sub(r"[^a-z0-9]+", "", str(buyer or "").lower())[:40]
    return hashlib.sha1(f"{t}|{b}|{closing or ''}".encode()).hexdigest()[:20]


def repair_dates():
    """Null out impossible publication dates already stored.

    Rows scraped before the parser fix kept a closing date in the advertised
    column, and the update path could not clear it because a NULL from the
    parser was treated as 'no new information'. This corrects them in place,
    so no rescrape is needed.
    """
    with conn() as c:
        cur = c.execute("""UPDATE tenders SET advertised=NULL
                           WHERE advertised IS NOT NULL
                             AND (advertised > date('now')
                                  OR (closing IS NOT NULL AND advertised > closing))""")
        return cur.rowcount


def _sane_advertised(adv, closing):
    """A publication date cannot be in the future, and cannot fall after the
    deadline. Either case means the parser picked up the wrong cell."""
    if not adv:
        return None
    today = datetime.now(timezone.utc).date().isoformat()
    if adv > today:
        return None
    if closing and adv > closing:
        return None
    return adv


def upsert(rows, source, rebuild_fts=True):
    """Insert or update scraped rows. Returns (seen, new, changed)."""
    if not rows:
        return 0, 0, 0
    ts = now()
    new = changed = 0

    with conn() as c:
        for r in rows:
            r["advertised"] = _sane_advertised(r.get("advertised"), r.get("closing"))
            uid = f"{source}:{r.get('ref') or r.get('url') or r.get('title','')[:60]}"
            dk = make_dedupe_key(r.get("title"), r.get("buyer"), r.get("closing"))
            existing = c.execute("SELECT * FROM tenders WHERE uid=?", (uid,)).fetchone()

            if existing:
                for f in TRACKED_FIELDS:
                    old, nv = existing[f], r.get(f)
                    if nv and old and str(old) != str(nv):
                        c.execute(
                            "INSERT INTO changes(uid,field,old_value,new_value,seen_at)"
                            " VALUES(?,?,?,?,?)", (uid, f, str(old), str(nv), ts))
                        changed += 1
                # advertised must be refreshed too. Without this, rows scraped
                # before a parser fix keep their wrong dates for ever, however
                # many times they are re-scanned.
                c.execute("""UPDATE tenders SET status=?, closing=?, closing_time=?,
                             value_text=?, value_num=?,
                             advertised=COALESCE(?, advertised),
                             last_seen=?, is_new=0 WHERE uid=?""",
                          (r.get("status"), r.get("closing"), r.get("closing_time"),
                           r.get("value_text"), r.get("value_num"),
                           r.get("advertised"), ts, uid))
                if r.get("lane") is not None:
                    _class_update(c, uid, r)
                continue

            # cross-source duplicate?
            # NOTHING IS EVER DISCARDED. A cross-source duplicate is stored
            # in full and merely tagged, so the Everything view always shows
            # the complete raw harvest. Deduping only hides rows from the
            # curated scopes.
            dup = c.execute(
                "SELECT uid, source FROM tenders WHERE dedupe_key=? LIMIT 1", (dk,)).fetchone()
            dup_of = None
            if dup and dup["source"] != source:
                c.execute("INSERT OR IGNORE INTO alternates VALUES (?,?,?,?)",
                          (dk, source, r.get("ref"), r.get("url")))
                dup_of = dup["uid"]

            c.execute("""INSERT INTO tenders
                (uid,dedupe_key,source,source_tier,jurisdiction,ref,title,description,
                 buyer,sector_label,category,status,advertised,closing,closing_time,
                 value_text,value_num,currency,is_digital,is_opportunity,lane,product_line,
                 fit_score,digital_domain,digital_evidence,
                 relevance,rationale,url,doc_url,contact_name,contact_email,contact_phone,
                 raw,dup_of,needs_review,first_seen,last_seen,is_new)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (uid, dk, source, r.get("source_tier"), r.get("jurisdiction"), r.get("ref"),
                 r.get("title"), r.get("description"), r.get("buyer"), r.get("sector_label"),
                 r.get("category"), r.get("status"), r.get("advertised"), r.get("closing"),
                 r.get("closing_time"), r.get("value_text"), r.get("value_num"),
                 r.get("currency"), r.get("is_digital", 0), r.get("is_opportunity", 0),
                 r.get("lane"), r.get("product_line"), r.get("fit_score", 0),
                 r.get("digital_domain"),
                 r.get("digital_evidence"), r.get("relevance"), r.get("rationale"),
                 r.get("url"), r.get("doc_url"), r.get("contact_name"),
                 r.get("contact_email"), r.get("contact_phone"), r.get("raw"), dup_of, r.get("needs_review", 0), ts, ts))
            if r.get("lane") is not None:
                _class_update(c, uid, r)
            new += 1

        # A full FTS rebuild per call made a 70,000-row backfill quadratic.
        # Bulk callers pass rebuild_fts=False and rebuild once at the end.
        if rebuild_fts:
            c.execute("INSERT INTO tenders_fts(tenders_fts) VALUES('rebuild')")
    return len(rows), new, changed


def record_health(source, ok, rows, message="", ms=0):
    ts = now()
    with conn() as c:
        total = c.execute("SELECT COUNT(*) n FROM tenders WHERE source=?", (source,)).fetchone()["n"]
        prev = c.execute("SELECT last_ok FROM source_health WHERE source=?", (source,)).fetchone()
        last_ok = ts if ok else (prev["last_ok"] if prev else None)
        c.execute("""INSERT INTO source_health
                     (source,last_run,last_ok,rows_last_run,status,message,total_rows,avg_ms)
                     VALUES(?,?,?,?,?,?,?,?)
                     ON CONFLICT(source) DO UPDATE SET
                       last_run=excluded.last_run, last_ok=excluded.last_ok,
                       rows_last_run=excluded.rows_last_run, status=excluded.status,
                       message=excluded.message, total_rows=excluded.total_rows,
                       avg_ms=excluded.avg_ms""",
                  (source, ts, last_ok, rows,
                   "ok" if ok else "failed", message[:300], total, ms))


def record_run(started, ok_n, fail_n, seen, new, trigger):
    with conn() as c:
        c.execute("INSERT INTO runs(started,finished,sources_ok,sources_failed,"
                  "rows_seen,rows_new,trigger) VALUES(?,?,?,?,?,?,?)",
                  (started, now(), ok_n, fail_n, seen, new, trigger))


def clear_new_flags(hours=24):
    with conn() as c:
        c.execute("UPDATE tenders SET is_new=0 WHERE datetime(first_seen) < datetime('now', ?)",
                  (f"-{hours} hour",))


# ───────────────────────────────────────────────── query layer
def search(q=None, sources=None, domains=None, relevance=None, jurisdictions=None,
           status=None, closing_within=None, value_min=None, value_max=None,
           digital_only=True, new_only=False, sort="closing", limit=200, offset=0,
           lanes=None, scope="opportunity", date_on=None, date_from=None,
           date_to=None, sectors=None, ttypes=None):
    where, args = [], []

    if lanes:
        where.append(f"t.lane IN ({','.join('?'*len(lanes))})"); args += lanes
    elif scope == "opportunity":
        where.append("t.is_opportunity=1")
    elif scope == "relevant":
        where.append("t.lane IN ('Core','Partner-led','Signal')")
    if scope != "all":
        where.append("t.dup_of IS NULL")
    # scope == 'all' shows literally every stored row
    if new_only:
        where.append("t.is_new=1")
    if sources:
        where.append(f"t.source IN ({','.join('?'*len(sources))})"); args += sources
    if domains:
        where.append(f"t.product_line IN ({','.join('?'*len(domains))})"); args += domains
    if sectors:
        where.append(f"t.buyer_sector IN ({','.join('?'*len(sectors))})"); args += sectors
    if ttypes:
        where.append(f"t.tender_type IN ({','.join('?'*len(ttypes))})"); args += ttypes
    if relevance:
        where.append(f"t.relevance IN ({','.join('?'*len(relevance))})"); args += relevance
    if jurisdictions:
        where.append(f"t.jurisdiction IN ({','.join('?'*len(jurisdictions))})"); args += jurisdictions
    if status == "open":
        where.append("(t.closing >= date('now') OR (t.closing IS NULL AND COALESCE(t.advertised, substr(t.first_seen,1,10)) >= date('now','-45 day')))")
    elif status == "closed":
        where.append("t.closing < date('now')")
    if date_on:
        where.append("(t.advertised = ? OR t.closing = ?)"); args += [date_on, date_on]
    if date_from:
        where.append("t.advertised >= ?"); args.append(date_from)
    if date_to:
        where.append("t.advertised <= ?"); args.append(date_to)
    if closing_within:
        where.append("t.closing BETWEEN date('now') AND date('now', ?)")
        args.append(f"+{int(closing_within)} day")
    if value_min is not None:
        where.append("t.value_num >= ?"); args.append(value_min)
    if value_max is not None:
        where.append("t.value_num <= ?"); args.append(value_max)

    join = ""
    fq = _fts_quote(q) if q else None
    if fq:
        join = "JOIN tenders_fts f ON f.rowid = t.rowid"
        where.append("tenders_fts MATCH ?")
        args.append(fq)

    order = {
        "closing":  "t.closing IS NULL, t.closing ASC",
        "newest":   "t.advertised DESC, t.first_seen DESC",
        "value":    "t.value_num IS NULL, t.value_num DESC",
        "relevance": "t.fit_score DESC, t.closing",
        "fit": "t.fit_score DESC, t.closing",
    }.get(sort, "t.closing IS NULL, t.closing ASC")

    sql = f"""SELECT t.* FROM tenders t {join}
              {'WHERE ' + ' AND '.join(where) if where else ''}
              ORDER BY {order} LIMIT ? OFFSET ?"""
    with conn(readonly=True) as c:
        rows = [dict(r) for r in c.execute(sql, args + [limit, offset])]
        cnt_sql = f"SELECT COUNT(*) n FROM tenders t {join} {'WHERE ' + ' AND '.join(where) if where else ''}"
        total = c.execute(cnt_sql, args).fetchone()["n"]
    return rows, total


def get(uid):
    with conn(readonly=True) as c:
        r = c.execute("SELECT * FROM tenders WHERE uid=?", (uid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["alternates"] = [dict(x) for x in c.execute(
            "SELECT source, ref, url FROM alternates WHERE dedupe_key=?", (d["dedupe_key"],))]
        d["changes"] = [dict(x) for x in c.execute(
            "SELECT field, old_value, new_value, seen_at FROM changes "
            "WHERE uid=? ORDER BY seen_at DESC LIMIT 12", (uid,))]
        return d


def facets():
    with conn(readonly=True) as c:
        f = lambda sql: [dict(r) for r in c.execute(sql)]
        return {
            "sources": f("""SELECT source k, COUNT(*) n FROM tenders
                            GROUP BY source ORDER BY n DESC"""),
            "domains": f("""SELECT product_line k, COUNT(*) n FROM tenders
                            WHERE is_opportunity=1 AND product_line!='' GROUP BY 1 ORDER BY n DESC"""),
            "lanes": f("""SELECT lane k, COUNT(*) n FROM tenders
                          WHERE lane IS NOT NULL AND lane!='None' GROUP BY 1"""),
            "jurisdictions": f("""SELECT jurisdiction k, COUNT(*) n FROM tenders
                                  GROUP BY 1 ORDER BY n DESC"""),
            "sectors": f("""SELECT buyer_sector k, COUNT(*) n FROM tenders
                            WHERE buyer_sector IS NOT NULL AND buyer_sector != ''
                            GROUP BY 1 ORDER BY n DESC"""),
            "ttypes": f("""SELECT tender_type k, COUNT(*) n FROM tenders
                           WHERE tender_type IS NOT NULL AND tender_type != ''
                           GROUP BY 1 ORDER BY n DESC"""),
            "relevance": f("""SELECT relevance k, COUNT(*) n FROM tenders
                              WHERE is_opportunity=1 GROUP BY 1"""),
            "stats": dict(c.execute("""SELECT
                    COUNT(*) total,
                    SUM(is_opportunity) digital,
                    SUM(CASE WHEN lane='Core' THEN 1 ELSE 0 END) core_n,
                    SUM(CASE WHEN lane='Partner-led' THEN 1 ELSE 0 END) partner_n,
                    SUM(CASE WHEN lane='Signal' THEN 1 ELSE 0 END) signal_n,
                    SUM(CASE WHEN is_opportunity=1 AND (closing >= date('now') OR (closing IS NULL AND COALESCE(advertised, substr(first_seen,1,10)) >= date('now','-45 day'))) THEN 1 ELSE 0 END) open_digital,
                    SUM(CASE WHEN is_opportunity=1 AND closing BETWEEN date('now') AND date('now','+7 day') THEN 1 ELSE 0 END) closing_week,
                    SUM(CASE WHEN is_opportunity=1 AND is_new=1 THEN 1 ELSE 0 END) new_digital,
                    SUM(CASE WHEN is_opportunity=1 AND relevance='High' THEN 1 ELSE 0 END) high_rel
                    FROM tenders""").fetchone()),
            "health": f("SELECT * FROM source_health ORDER BY source"),
            "last_run": dict(c.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone() or {}),
        }


def buyers(limit=60):
    with conn(readonly=True) as c:
        return [dict(r) for r in c.execute("""
            SELECT COALESCE(NULLIF(buyer_norm,''), buyer) buyer,
                   MAX(buyer_sector) sector, MAX(jurisdiction) jurisdiction,
                   MAX(source) source,
                   COUNT(*) tenders,
                   SUM(is_opportunity) digital, SUM(is_digital) any_digital,
                   SUM(CASE WHEN closing>=date('now') THEN 1 ELSE 0 END) open_now,
                   MAX(contact_name) contact_name, MAX(contact_email) contact_email,
                   MAX(contact_phone) contact_phone,
                   ROUND(AVG(julianday(closing)-julianday(advertised)),0) avg_window
            FROM tenders WHERE buyer IS NOT NULL AND buyer!=''
            GROUP BY COALESCE(NULLIF(buyer_norm,''), buyer) HAVING SUM(is_opportunity) > 0
            ORDER BY digital DESC, tenders DESC LIMIT ?""", (limit,))]


def overview():
    """Aggregates for the Overview tab."""
    with conn(readonly=True) as c:
        f = lambda sql, a=(): [dict(r) for r in c.execute(sql, a)]
        return {
            "by_domain": f("""SELECT product_line k, COUNT(*) n,
                                     SUM(CASE WHEN closing>=date('now') THEN 1 ELSE 0 END) open_n
                              FROM tenders WHERE is_opportunity=1 AND product_line!=''
                              GROUP BY 1 ORDER BY n DESC"""),
            "by_lane": f("""SELECT lane k, COUNT(*) n FROM tenders
                            WHERE lane IS NOT NULL AND lane!='None' GROUP BY 1"""),
            "by_source": f("""SELECT source k, COUNT(*) n, SUM(is_opportunity) dig
                              FROM tenders GROUP BY 1 ORDER BY n DESC"""),
            "by_sector": f("""SELECT buyer_sector k, COUNT(*) n, SUM(is_opportunity) dig
                              FROM tenders WHERE buyer_sector IS NOT NULL
                              GROUP BY 1 ORDER BY dig DESC, n DESC"""),
            "by_type": f("""SELECT tender_type k, COUNT(*) n, SUM(is_opportunity) dig
                            FROM tenders WHERE is_opportunity=1 AND tender_type IS NOT NULL
                            GROUP BY 1 ORDER BY n DESC"""),
            "by_buyer": f("""SELECT COALESCE(NULLIF(buyer_norm,''), buyer) k, COUNT(*) n,
                             SUM(is_opportunity) dig
                             FROM tenders WHERE buyer!='' GROUP BY 1
                             HAVING dig>0 ORDER BY dig DESC LIMIT 12"""),
            "calendar": f("""SELECT closing k, COUNT(*) n
                             FROM tenders WHERE is_opportunity=1
                               AND closing BETWEEN date('now') AND date('now','+30 day')
                             GROUP BY 1 ORDER BY 1"""),
            "monthly": f("""SELECT substr(advertised,1,7) k, COUNT(*) n, SUM(is_opportunity) dig
                            FROM tenders WHERE advertised IS NOT NULL
                            GROUP BY 1 ORDER BY 1 DESC LIMIT 12"""),
            "value_bands": f("""SELECT CASE
                                  WHEN value_num IS NULL THEN 'Not disclosed'
                                  WHEN value_num < 1e6 THEN 'Under 1M'
                                  WHEN value_num < 1e7 THEN '1M - 10M'
                                  WHEN value_num < 5e7 THEN '10M - 50M'
                                  WHEN value_num < 5e8 THEN '50M - 500M'
                                  ELSE 'Above 500M' END k,
                                COUNT(*) n FROM tenders WHERE is_opportunity=1
                                GROUP BY 1 ORDER BY n DESC"""),
        }


def save_detail(uid, d):
    """Store lazily-fetched detail-page fields."""
    with conn() as c:
        c.execute("""UPDATE tenders SET scope=?, eligibility=?, bid_security=?,
                     tender_fee=?, est_value_low=?, est_value_high=?, prebid=?,
                     opening=?, venue=?, attachments=?, contact_name=COALESCE(?,contact_name),
                     contact_email=COALESCE(?,contact_email),
                     contact_phone=COALESCE(?,contact_phone),
                     detail_fetched=? WHERE uid=?""",
                  (d.get("scope"), d.get("eligibility"), d.get("bid_security"),
                   d.get("tender_fee"), d.get("est_value_low"), d.get("est_value_high"),
                   d.get("prebid"), d.get("opening"), d.get("venue"),
                   d.get("attachments"), d.get("contact_name"), d.get("contact_email"),
                   d.get("contact_phone"), now(), uid))


def find_repeats(min_gap_days=120):
    """Flag tenders that re-issue an earlier one by the same buyer.

    The previous key included the closing date, so a re-issue (which always
    has a new closing date) could never match its predecessor and nothing was
    ever flagged. Identity is now normalised title plus organisation.
    """
    from datetime import date as _d
    with conn() as c:
        rows = c.execute("""SELECT uid, title, buyer, buyer_norm,
                                   COALESCE(advertised, closing) d
                            FROM tenders WHERE is_opportunity=1
                              AND COALESCE(advertised, closing) IS NOT NULL""").fetchall()
        groups = {}
        for r in rows:
            t = _STOP.sub(" ", str(r["title"] or "").lower())
            t = " ".join(sorted(re.sub(r"[^a-z0-9]+", " ", t).split()))[:160]
            b = re.sub(r"[^a-z0-9]", "", str(r["buyer_norm"] or r["buyer"] or "").lower())[:40]
            if t:
                groups.setdefault((t, b), []).append(r)
        n = 0
        for g in groups.values():
            if len(g) < 2:
                continue
            g = sorted(g, key=lambda x: x["d"])
            for prev, cur in zip(g, g[1:]):
                try:
                    gap = (_d.fromisoformat(cur["d"][:10]) - _d.fromisoformat(prev["d"][:10])).days
                except Exception:
                    continue
                if gap >= min_gap_days:
                    c.execute("UPDATE tenders SET repeat_of=? WHERE uid=?", (prev["uid"], cur["uid"]))
                    n += 1
        return n


def _norm(s):
    return re.sub(r"[^a-z0-9 ]+", " ", str(s or "").lower())


_STEM_RULES = (("ization", "ise"), ("isation", "ise"), ("ization", "ise"),
               ("ising", "ise"), ("izing", "ise"), ("ements", "ement"),
               ("ications", "ication"), ("ing", ""), ("ed", ""), ("es", ""),
               ("s", ""))

_SPELL = {"fiber": "fibre", "fibre": "fibre", "centre": "center",
          "licence": "license", "programme": "program", "catalogue": "catalog",
          "digitalization": "digitise", "digitization": "digitise",
          "digitisation": "digitise", "digitalisation": "digitise"}


def _stem(w):
    """Crude but effective normalisation so word variants collapse together.

    services/service, digitization/digitisation/digitalization, fiber/fibre,
    installation/installations. Without this, the same tender written two
    ways scores far lower than it should.
    """
    w = _SPELL.get(w, w)
    if len(w) <= 4:
        return w
    for suf, rep in _STEM_RULES:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[:-len(suf)] + rep
    return w


# Function words are not content, however long they are. Without this,
# "Request for Propoosals" reads as a three-word title and passes the
# vague-title test it should fail.
_FUNC = {"for", "and", "the", "with", "from", "its", "per", "any", "all",
         "are", "was", "has", "have", "not", "but", "our", "out", "may",
         "can", "will", "shall", "into", "upon", "under", "over", "each",
         "than", "then", "that", "this", "these", "those", "such", "also",
         "vide", "etc", "nos", "sub", "via", "who", "whom", "your"}


def _terms(s):
    """Content tokens. These carry the information mass, so nothing
    speculative belongs here: extra tokens inflate the denominator and
    depress every comparison."""
    return [_stem(w) for w in _norm(s).split()
            if len(w) > 2 and w not in _FUNC]


def _aliases(s):
    """Spelling variants used ONLY to detect overlap, never to weigh it.

    Portals write "prequalification" where internal lists write
    "pre-qualification"; splitting on the hyphen yields two tokens matching
    neither. Joined forms are therefore matchable but carry no mass.
    """
    out = set()
    for a, b in re.findall(r"([a-z]{2,})[-\u2010-\u2015]([a-z]{3,})",
                           str(s or "").lower()):
        out.add(_stem(a + b))
    return out


_REF_PREFIX = re.compile(
    r"^\s*(?:tender|nit|bid|rfp|rfq|eoi)\s*(?:no\.?|number|#)?\s*[:\-]?\s*"
    r"[0-9a-z\-/]{0,12}\s*[:\-.]?\s*", re.I)


def _strip_ref(s):
    """Remove leading reference noise such as 'TENDER NO. 35' which dilutes
    the comparison without carrying meaning."""
    return _REF_PREFIX.sub("", str(s or "")).strip() or str(s or "")


def _clauses(s):
    """Split a bundled description into its parts.

    Internal lists often record one row covering several items, while the
    portal publishes them separately. Scoring the whole string against a
    narrower title understates the match, so each clause is scored too.
    """
    parts = re.split(r"[,;/()]|\s&\s|\band\b", str(s or ""))
    return [p.strip() for p in parts if len(p.strip()) >= 14]


def _fuzzy_pairs(a_terms, b_terms, cutoff=0.86):
    """Tokens that are near-identical but not equal: typos, transliterations,
    hyphen splits. Catches 'commissioning'/'comissioning' style near misses."""
    from difflib import SequenceMatcher
    unmatched_a = [t for t in a_terms if t not in b_terms]
    unmatched_b = [t for t in b_terms if t not in a_terms]
    pairs = []
    for x in unmatched_a:
        for y in unmatched_b:
            if abs(len(x) - len(y)) > 3:
                continue
            if SequenceMatcher(None, x, y).ratio() >= cutoff:
                pairs.append((x, y))
                break
    return pairs


def _seq_ratio(a, b):
    """Whole-string similarity. Complements bag-of-words: catches cases where
    wording is nearly identical but token weighting undersells it."""
    from difflib import SequenceMatcher
    a, b = " ".join(sorted(_terms(a))), " ".join(sorted(_terms(b)))
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


_CORP = ("of", "and", "the", "for", "pvt", "private")
_SUFFIX = ("limited", "ltd", "company", "corporation", "authority",
           "department", "government", "pakistan")


def _initialism(name):
    """Acronym variants for an organisation name, UPPERCASE.

    'Pakistan Revenue Automation Limited' must yield both PRA and PRAL,
    because organisations abbreviate with and without the corporate suffix.
    """
    all_w = [w for w in _norm(name).split() if w not in _CORP]
    core = [w for w in all_w if w not in _SUFFIX]
    out = set()
    for words in (core, all_w):
        if len(words) >= 2:
            ini = "".join(w[0] for w in words).upper()
            out.add(ini)
            if len(ini) > 3:
                out.update({ini[:4], ini[:3]})
            if len(ini) >= 3:
                out.add(ini[:len(ini) - 1])
    return {x for x in out if len(x) >= 2}


def _buyer_score(q_buyer, db_buyer):
    """Buyer agreement, tolerant of acronyms and name variants."""
    a, b = _norm(q_buyer).strip(), _norm(db_buyer).strip()
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    ta, tb = set(_terms(a)), set(_terms(b))
    if not ta or not tb:
        return 0.0
    au = {t.upper() for t in _norm(q_buyer).split()}
    bu = {t.upper() for t in _norm(db_buyer).split()}
    if _initialism(db_buyer) & au or _initialism(q_buyer) & bu:
        return 0.95
    return len(ta & tb) / min(len(ta), len(tb))


def _date_delta(a, b):
    """Days between two ISO dates, or None if either is missing."""
    if not a or not b:
        return None
    try:
        from datetime import date
        pa = date(*map(int, str(a)[:10].split("-")))
        pb = date(*map(int, str(b)[:10].split("-")))
        return abs((pa - pb).days)
    except Exception:
        return None


def _bigrams(s):
    t = _terms(s)
    return [f"{a}_{b}" for a, b in zip(t, t[1:])]


def _build_idf():
    """IDF over titles AND descriptions.

    The corpus decides what counts as boilerplate. Words appearing in a large
    share of tenders (procurement, equipment, supply, services, tender) get
    near-zero weight; rare, discriminating words carry the score.
    """
    import math
    with conn(readonly=True) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT uid, ref, title, buyer, source, advertised, closing, lane, "
            "url, description, scope FROM tenders WHERE dup_of IS NULL")]
    df, docs = {}, []
    for r in rows:
        t_terms = set(_terms(r["title"]))
        # our own description and any fetched scope text are evidence too
        body = " ".join(filter(None, (r.get("description") or "",
                                      r.get("scope") or "")))[:1200]
        b_terms = set(_terms(body)) - t_terms
        docs.append((r, t_terms, set(_bigrams(r["title"])), b_terms,
                     _aliases(r["title"])))
        for t in (t_terms | b_terms):
            df[t] = df.get(t, 0) + 1
    n = max(1, len(docs))
    idf = {t: math.log(1 + n / (1 + c_)) for t, c_ in df.items()}
    return docs, idf, n, df


# Evidence is measured in DISTINCTIVE shared terms.
#
# A term is boilerplate when it appears in a large SHARE OF DOCUMENTS, which
# is the only scale-free way to say it. An earlier version used the median of
# IDF values across the vocabulary: in a real corpus most words occur once, so
# that median sat near the maximum and ordinary content words such as
# "computer" or "hosting" were wrongly treated as generic.
BOILER_DF = 0.06            # in >6% of tenders -> boilerplate
RARE_DF = 0.004             # in <0.4% of tenders -> highly distinctive
MIN_DISTINCT_TERMS = 2
MIN_RARE_TERMS = 1


def coverage_start():
    """The earliest date this database could have observed anything.

    Portals serve a rolling window, so a tender that closed before scanning
    began was never collectable. Reporting those as failures inflates the
    denominator and hides the real miss rate.
    """
    with conn(readonly=True) as c:
        r = c.execute("SELECT MIN(first_seen) a, MIN(advertised) b "
                      "FROM tenders").fetchone()
    fs = (r["a"] or "")[:10] or None
    adv = (r["b"] or "")[:10] or None
    return {"first_scan": fs, "earliest_advertised": adv}


def match_rows(items, top_k=3, coverage_from=None):
    # Every row is scored. Skipping rows by date was wrong: portal history
    # pages carry tenders that closed well before scanning began.
    """Match an uploaded list against the database.

    SHARED INFORMATION, NOT SIMILARITY. Two titles sharing "procurement of IT
    equipment" are highly similar and share almost no information, because
    those words appear in hundreds of tenders. Scoring therefore rests on the
    absolute IDF mass of shared rare terms, not on a ratio that treats
    boilerplate and distinctive vocabulary alike.

    Evidence assessed independently:
        SUBJECT  IDF-weighted overlap against our title, and separately
                 against our title plus stored description and scope text.
                 The stronger wins; description evidence is discounted since
                 portal descriptions carry more filler than titles.
        BUYER    scored separately, acronyms resolved from the full name.
        DATE     a tiebreaker only. Agencies cluster bid openings on common
                 days, so a shared date is weak evidence, and a differing one
                 is routine because internal lists carry internal deadlines.
    """
    docs, idf, n, df = _build_idf()
    out = []

    def info(terms):
        return sum(idf.get(t, 1.0) for t in terms)

    def distinctive(terms):
        """Split shared words into those carrying information and those that
        are procurement boilerplate, by share of documents containing them."""
        d_ = [t for t in terms if df.get(t, 1) / n <= BOILER_DF]
        r_ = [t for t in d_ if df.get(t, 1) / n <= RARE_DF]
        return d_, r_

    for it in items:
        desc = _strip_ref(it.get("description") or "")
        clauses = _clauses(desc)[:4]
        clause_terms = [set(_terms(c)) for c in clauses]
        q_terms = set(_terms(desc))
        q_alias = _aliases(desc)
        q_bi = set(_bigrams(desc))
        sd = str(it.get("submission_date") or "")[:10]
        dept = it.get("department") or ""

        if len(q_terms) < 3 or _norm(desc).strip() in (
                "description", "tender", "subject", "title"):
            out.append({"input": it, "status": "insufficient", "confidence": 0.0,
                        "match": None, "candidates": [],
                        "why": "Description too short to match reliably"})
            continue

        q_info = info(q_terms) or 1.0
        scored = []

        for r, t_terms, t_bi, b_terms, t_alias in docs:
            shared_t = (q_terms & t_terms) | (q_alias & t_terms) | (q_terms & t_alias)
            shared_b = (q_terms & b_terms) - shared_t
            fuzzy = _fuzzy_pairs(q_terms, t_terms) if shared_t else []
            if not shared_t and not shared_b and not fuzzy:
                continue

            # ── absolute information gate
            info_t = info(shared_t) + sum(0.75 * idf.get(x, 1.0) for x, _ in fuzzy)
            info_b = 0.55 * info(shared_b)      # description evidence discounted
            shared_info = info_t + info_b
            title_info = info(t_terms)

            # ── coverage, computed on title evidence and on title+body
            def cov(shared_mass, doc_mass):
                cq = shared_mass / q_info
                ct = shared_mass / max(doc_mass, 1.0)
                return (2 * cq * ct) / (cq + ct) if (cq + ct) else 0.0

            sub_title = cov(info_t, title_info)
            sub_body = cov(info_t + info_b, title_info + 0.6 * info(b_terms))
            subject = max(sub_title, sub_body)

            # a single clause of a bundled description may match the whole
            # of a narrower portal title
            for ct in clause_terms:
                sh = ct & t_terms
                if len(sh) < 2:
                    continue
                m_ = info(sh)
                cq = m_ / max(info(ct), 1.0)
                ct_ = m_ / max(title_info, 1.0)
                cl_score = (2 * cq * ct_) / (cq + ct_) if (cq + ct_) else 0.0
                subject = max(subject, 0.92 * cl_score)

            # sequence similarity is a SECONDARY opinion, never dominant
            seq = _seq_ratio(desc, r["title"])
            subject = max(subject, 0.75 * seq)
            if q_bi & t_bi:
                subject = min(1.0, subject + 0.08 * min(3, len(q_bi & t_bi)))

            bs = _buyer_score(dept, r.get("buyer"))
            dd = _date_delta(sd, r.get("closing"))

            dist, rare = distinctive(shared_t | {x for x, _ in fuzzy})
            dist_b, _ = distinctive(shared_b)
            enough_info = (len(dist) >= MIN_DISTINCT_TERMS
                           or len(rare) >= MIN_RARE_TERMS
                           or (len(dist) >= 1 and len(dist_b) >= 2))
            # A portal title is usable evidence when it is long enough to say
            # something AND carries at least one distinctive word. "Request for
            # Propoosals" fails on length even though the typo reads as rare.
            t_dist, _ = distinctive(t_terms)
            rich_doc = len(t_terms) >= 3 and len(t_dist) >= 1
            strong_subject = subject >= 0.62 and enough_info and rich_doc
            strong_buyer = bs >= 0.60
            same_date = dd is not None and dd <= 1
            near_date = dd is not None and dd <= 7
            far_date = dd is not None and dd > 30
            # A missing buyer on our side is absence of evidence, not
            # evidence of conflict. Only penalise when both names exist and
            # genuinely disagree.
            conflict_buyer = (bool(dept) and bool(str(r.get("buyer") or "").strip())
                              and bs < 0.20)

            score = 0.62 * subject + 0.22 * min(bs, 1.0)
            if same_date:
                score += 0.08          # tiebreaker, not evidence
            elif near_date:
                score += 0.04
            elif far_date:
                score -= 0.06
            if conflict_buyer:
                score -= 0.28
            if not enough_info:
                score = min(score, 0.38)   # shared words carry no information
            if not rich_doc:
                score = min(score, 0.42)   # portal title says almost nothing

            # PROMOTION. Subject agreement is mandatory and must be
            # information-bearing. Buyer or date can corroborate it. Neither
            # can create a match on its own.
            if strong_subject and (strong_buyer or same_date or near_date) \
                    and not conflict_buyer:
                score = max(score, 0.74)
            if subject >= 0.85 and enough_info and rich_doc and not conflict_buyer:
                score = max(score, 0.72)   # near-verbatim needs no corroboration
            if not strong_subject:
                score = min(score, 0.55)

            # A title made entirely of generic words cannot be confirmed on
            # wording. But near-identical wording plus the right buyer and a
            # matching date is exactly the case a person should adjudicate,
            # so it belongs in 'possible' rather than being dismissed.
            if (not enough_info) and subject >= 0.85 and strong_buyer \
                    and (same_date or near_date):
                score = max(score, 0.52)

            why = []
            if strong_subject:
                why.append(f"subject {subject:.2f}")
            elif subject >= 0.5:
                why.append(f"weak subject {subject:.2f}")
            if shared_b and info_b > 1:
                why.append("description overlap")
            if not enough_info:
                why.append("shared words are generic")
            elif dist:
                why.append("on: " + ", ".join(sorted(dist, key=lambda t: -idf.get(t, 0))[:3]))
            if not rich_doc:
                why.append("portal title too vague")
            if strong_buyer:
                why.append("buyer match")
            if conflict_buyer:
                why.append("different organisation")
            if same_date:
                why.append("same closing date")
            elif far_date:
                why.append(f"closing differs by {dd}d")

            scored.append((max(0.0, min(score, 1.0)), r, round(bs, 2),
                           round(subject, 2), dd, round(shared_info, 1),
                           ", ".join(why)))

        scored.sort(key=lambda x: -x[0])
        cands = [{"score": round(s, 2), "buyer_score": b, "subject": sub,
                  "date_gap": dd, "shared_info": si, "why": why,
                  **{k: r.get(k) for k in ("uid", "ref", "title", "buyer",
                                           "source", "closing", "lane")}}
                 for s, r, b, sub, dd, si, why in scored[:top_k] if s > 0.15]

        best = cands[0]["score"] if cands else 0.0
        status = ("found" if best >= 0.66 else
                  "possible" if best >= 0.44 else "missing")
        out.append({"input": it, "status": status, "confidence": best,
                    "match": ({k: cands[0][k] for k in
                               ("uid", "ref", "title", "buyer", "source",
                                "closing", "lane")} if status != "missing" else None),
                    "why": cands[0]["why"] if cands else "",
                    "candidates": cands})
    return out


def unmatched_opportunities(matched_uids, date_from=None, date_to=None,
                            lanes=("Core", "Partner-led"), limit=400):
    """The inverse audit: Jazz-relevant tenders the portals published that do
    NOT appear on the uploaded list. This is the miss report.

    Scoped to the date window of the uploaded list so it compares like with
    like, and to biddable lanes so it stays decision-useful.
    """
    where = ["t.dup_of IS NULL", "t.lane IN ({})".format(
        ",".join("?" * len(lanes)))]
    args = list(lanes)
    if date_from:
        where.append("(t.closing >= ? OR t.closing IS NULL)"); args.append(date_from)
    if date_to:
        where.append("(t.closing <= ? OR t.closing IS NULL)"); args.append(date_to)
    sql = ("SELECT uid, ref, title, buyer, source, jurisdiction, lane, "
           "product_line, fit_score, advertised, closing, value_text, url "
           "FROM tenders t WHERE " + " AND ".join(where) +
           " ORDER BY t.fit_score DESC, t.closing LIMIT ?")
    args.append(limit * 3)
    with conn(readonly=True) as c:
        rows = [dict(r) for r in c.execute(sql, args)]
    seen = set(matched_uids or [])
    rows = [r for r in rows if r["uid"] not in seen]

    # collapse near-identical notices: the same tender is often listed once
    # per lot or re-advertised, and a list of visible duplicates reads as noise
    out, fingerprints = [], set()
    for r in rows:
        fp = (re.sub(r"[^a-z0-9]", "", str(r.get("title") or "").lower())[:70],
              re.sub(r"[^a-z0-9]", "", str(r.get("buyer") or "").lower())[:30])
        if fp in fingerprints:
            continue
        fingerprints.add(fp)
        out.append(r)
        if len(out) >= limit:
            break
    # group by buyer so it reads as an account-level gap, not a flat list
    out.sort(key=lambda r: (str(r.get("buyer") or "zzz").lower(),
                            -(r.get("fit_score") or 0)))
    return out


def brief(days_new=2, closing_days=7, limit=10):
    """The daily brief: what changed, what is about to close, what moved.

    Deliberately narrow. An executive does not browse a feed, they need a
    fixed-shape answer to "what changed and what do I do about it". Only
    biddable lanes appear, capped at a handful of rows per section, and an
    empty brief is a valid answer rather than a failure.
    """
    with conn(readonly=True) as c:
        f = lambda sql, a=(): [dict(r) for r in c.execute(sql, a)]
        cols = ("uid, ref, title, buyer, source, lane, product_line, fit_score, "
                "advertised, closing, value_text, url")

        # A brief that lists closed tenders destroys trust in the whole tool.
        new_rows = f(f"""SELECT {cols} FROM tenders
                         WHERE is_opportunity=1 AND dup_of IS NULL
                           AND datetime(first_seen) >= datetime('now', ?)
                           AND closing IS NOT NULL
                           AND closing >= date('now')
                         ORDER BY fit_score DESC, closing LIMIT ?""",
                     (f"-{days_new} day", limit))

        closing = f(f"""SELECT {cols},
                        CAST(julianday(closing) - julianday('now') AS INTEGER) days_left
                        FROM tenders
                        WHERE is_opportunity=1 AND dup_of IS NULL
                          AND closing BETWEEN date('now') AND date('now', ?)
                        ORDER BY closing, fit_score DESC LIMIT ?""",
                    (f"+{closing_days} day", limit))

        moved = f(f"""SELECT t.uid, t.ref, t.title, t.buyer, t.source, t.lane,
                             t.fit_score, t.closing, t.url,
                             ch.field, ch.old_value, ch.new_value, ch.seen_at
                      FROM changes ch JOIN tenders t ON t.uid = ch.uid
                      WHERE t.is_opportunity=1
                        AND (t.closing >= date('now') OR (t.closing IS NULL AND COALESCE(t.advertised, substr(t.first_seen,1,10)) >= date('now','-45 day')))
                        AND datetime(ch.seen_at) >= datetime('now', ?)
                      ORDER BY ch.seen_at DESC LIMIT ?""",
                  (f"-{days_new} day", 8))

        stats = dict(c.execute("""SELECT
            SUM(CASE WHEN is_opportunity=1 AND dup_of IS NULL
                     AND (closing >= date('now') OR (closing IS NULL AND COALESCE(advertised, substr(first_seen,1,10)) >= date('now','-45 day')))
                THEN 1 ELSE 0 END) open_opps,
            SUM(CASE WHEN lane='Core' AND dup_of IS NULL
                     AND (closing >= date('now') OR (closing IS NULL AND COALESCE(advertised, substr(first_seen,1,10)) >= date('now','-45 day')))
                THEN 1 ELSE 0 END) open_core,
            COUNT(*) tracked FROM tenders""").fetchone())

        last = dict(c.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone() or {})

    return {"new": new_rows, "closing": closing, "moved": moved,
            "stats": stats, "last_run": last,
            "window": {"new_days": days_new, "closing_days": closing_days}}


# ─────────────────────────────────────────── awards and forecasting

TENURE = [
    (r"\b(?:five|5)\s*\(?0?5?\)?\s*year", 60),
    (r"\b(?:four|4)\s*\(?0?4?\)?\s*year", 48),
    (r"\b(?:three|3)\s*\(?0?3?\)?\s*year", 36),
    (r"\b(?:two|2)\s*\(?0?2?\)?\s*year", 24),
    (r"\b(?:one|1)\s*\(?0?1?\)?\s*year", 12),
    (r"\bannual|\bper annum|\byearly|\bfy\s*20", 12),
    (r"\b(?:six|6)\s*month", 6),
    (r"\bframework agreement", 12),
    (r"\bamc\b|\bannual maintenance", 12),
]


SERVICE_RX = re.compile(r"service|hiring|provision|subscription|maintenance|support|"
                        r"\bamc\b|bandwidth|connectivity|internet|cellular|sms|"
                        r"hosting|licen[cs]e|renewal|rental|outsourc|managed|call cent", re.I)


def infer_tenure(text):
    """Contract length from the wording of the notice.

    'for a period of three (03) years' is the single most common phrasing in
    Pakistani public procurement, and it is what makes renewal dates
    predictable years ahead of the re-tender.
    """
    t = str(text or "").lower()
    for pat, months in TENURE:
        if re.search(pat, t):
            return months
    return None


def _add_months(iso, months):
    if not iso or not months:
        return None
    try:
        y, m, dd = [int(x) for x in str(iso)[:10].split("-")]
    except Exception:
        return None
    m += months
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    dd = min(dd, 28)
    return f"{y:04d}-{m:02d}-{dd:02d}"


def upsert_awards(rows, source):
    """Store contract awards. Winner, value and tenure are what make the
    forward view possible; tender notices alone cannot produce it."""
    if not rows:
        return 0, 0
    ts = now()
    new = 0
    with conn() as c:
        for r in rows:
            uid = f"{source}:AW:{r.get('ref') or r.get('title','')[:60]}"
            from classify import normalize_buyer, buyer_sector
            tenure = r.get("tenure_months") or infer_tenure(
                f"{r.get('title','')} {r.get('description','')}")
            basis = "stated" if tenure else None
            # Most award titles omit the term. Recurring service contracts in
            # biddable lanes are overwhelmingly annual, so assume 12 months and
            # label it. One-off supply contracts do not renew, so get no date.
            if not tenure and r.get("lane") in ("Core", "Partner-led") and \
                    SERVICE_RX.search(str(r.get("title") or "")):
                tenure, basis = 12, "assumed"
            renewal = _add_months(r.get("signing_date") or r.get("award_date"), tenure)
            bn = normalize_buyer(r.get("buyer"))
            exists = c.execute("SELECT 1 FROM awards WHERE uid=?", (uid,)).fetchone()
            if exists:
                c.execute("""UPDATE awards SET winner=COALESCE(?,winner),
                             value_text=COALESCE(?,value_text),
                             value_num=COALESCE(?,value_num),
                             bids_received=COALESCE(?,bids_received),
                             signing_date=COALESCE(?,signing_date),
                             tenure_months=?, tenure_basis=?, renewal_due=?,
                             lane=?, product_line=?, is_opportunity=?,
                             buyer_norm=?, buyer_sector=?
                             WHERE uid=?""",
                          (r.get("winner"), r.get("value_text"), r.get("value_num"),
                           r.get("bids_received"), r.get("signing_date"),
                           tenure, basis, renewal, r.get("lane"), r.get("product_line"),
                           r.get("is_opportunity", 0), bn, buyer_sector(f"{r.get('buyer')} {bn}"),
                           uid))
                continue
            c.execute("""INSERT INTO awards
                (uid,source,ref,title,buyer,winner,value_text,value_num,
                 bids_received,award_date,tenure_months,renewal_due,lane,
                 product_line,is_opportunity,url,first_seen)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (uid, source, r.get("ref"), r.get("title"), r.get("buyer"),
                 r.get("winner"), r.get("value_text"), r.get("value_num"),
                 r.get("bids_received"), r.get("award_date"), tenure, renewal,
                 r.get("lane"), r.get("product_line"), r.get("is_opportunity", 0),
                 r.get("url"), ts))
            c.execute("""UPDATE awards SET tenure_basis=?, buyer_norm=?, buyer_sector=?,
                         signing_date=? WHERE uid=?""",
                      (basis, bn, buyer_sector(f"{r.get('buyer')} {bn}"),
                       r.get("signing_date"), uid))
            new += 1
    return len(rows), new


# Our own identity in award records. Buyers spell it several ways, and
# hardcoding one string is why the earlier "at risk" view was so thin.
OUR_ALIASES = [
    "jazz", "pmcl", "pakistan mobile communications", "mobilink", "veon",
    "jazzcash", "warid",
]

# The competitive set worth tracking by name. Everything else is "other".
RIVALS = {
    "PTCL": ["ptcl", "pakistan telecommunication company"],
    "Ufone": ["ufone", "pak telecom mobile"],
    "Zong": ["zong", "cmpak", "china mobile pak"],
    "Telenor": ["telenor"],
    "Nayatel": ["nayatel"],
    "Transworld": ["transworld", "twa"],
    "Wateen": ["wateen"],
    "Multinet": ["multinet"],
    "Cybernet": ["cybernet"],
    "Systems Ltd": ["systems limited", "systems ltd"],
    "NTC": ["national telecommunication corporation"],
    "Inbox": ["inbox business"],
    "Techaccess": ["techaccess", "tech access"],
    "Nexus": ["nexus"],
}


def _alias_sql(col, aliases):
    return "(" + " OR ".join(f"LOWER({col}) LIKE '%{a}%'" for a in aliases) + ")"


def _who(winner):
    w = str(winner or "").lower()
    if any(a in w for a in OUR_ALIASES):
        return "Jazz"
    for name, pats in RIVALS.items():
        if any(p in w for p in pats):
            return name
    return None


def intelligence(horizon_days=540, min_fit_lane=("Core", "Partner-led")):
    """Account and competitor analytics built on public award records.

    The question a feed cannot answer is not "what is out there" but "where do
    we stand". These aggregates answer it four ways: share of each buyer's
    spend, who holds what and at what price, when every contract returns to
    market, and which buyers are overdue to tender based on their own history.
    """
    lanes_sql = ",".join("?" * len(min_fit_lane))
    lanes = tuple(min_fit_lane)
    OURS = _alias_sql("winner", OUR_ALIASES)

    with conn(readonly=True) as c:
        f = lambda sql, a=(): [dict(r) for r in c.execute(sql, a)]

        # ── ACCOUNT MAP. Share of wallet per buyer, the core strategic view.
        accounts = f(f"""
            SELECT COALESCE(NULLIF(buyer_norm,''), buyer) buyer,
                   MAX(buyer_sector)                         sector,
                   COUNT(*)                                  awards,
                   SUM(COALESCE(value_num,0))                total_value,
                   SUM(CASE WHEN {OURS} THEN COALESCE(value_num,0) ELSE 0 END) our_value,
                   SUM(CASE WHEN {OURS} THEN 1 ELSE 0 END)   our_wins,
                   COUNT(DISTINCT winner)                    suppliers,
                   ROUND(AVG(bids_received),1)               avg_bids,
                   MIN(award_date)                           first_award,
                   MAX(award_date)                           last_award,
                   MIN(CASE WHEN renewal_due >= date('now') THEN renewal_due END) next_renewal
            FROM awards
            WHERE buyer IS NOT NULL AND buyer != '' AND lane IN ({lanes_sql})
            GROUP BY COALESCE(NULLIF(buyer_norm,''), buyer)
            ORDER BY total_value DESC
            LIMIT 25""", lanes)
        for a in accounts:
            tv = a.get("total_value") or 0
            a["our_share"] = round(100 * (a.get("our_value") or 0) / tv, 1) if tv else 0.0
            a["gap_value"] = round(tv - (a.get("our_value") or 0))
            a["status"] = ("Held" if a["our_share"] >= 60 else
                           "Contested" if a["our_share"] > 0 else "Not won")

        # ── COMPETITOR PROFILES, with head to head against us per buyer.
        rivals = f(f"""
            SELECT winner,
                   COUNT(*) wins,
                   SUM(COALESCE(value_num,0)) value,
                   COUNT(DISTINCT buyer) buyers,
                   ROUND(AVG(bids_received),1) avg_bids,
                   SUM(CASE WHEN bids_received=1 THEN 1 ELSE 0 END) uncontested,
                   MAX(award_date) last_win
            FROM awards
            WHERE winner IS NOT NULL AND winner != '' AND lane IN ({lanes_sql})
            GROUP BY winner ORDER BY value DESC, wins DESC LIMIT 20""", lanes)
        # Suppliers spell their own names several ways across notices, so raw
        # grouping splits one competitor into many. Consolidate on identity.
        merged = {}
        for r in rivals:
            who = _who(r["winner"]) or r["winner"][:40]
            m = merged.setdefault(who, {
                "who": who, "is_us": _who(r["winner"]) == "Jazz",
                "wins": 0, "value": 0.0, "buyers": 0, "uncontested": 0,
                "last_win": "", "_bidsum": 0.0, "_bidn": 0, "names": set()})
            m["wins"] += r["wins"] or 0
            m["value"] += r["value"] or 0
            m["buyers"] = max(m["buyers"], r["buyers"] or 0)
            m["uncontested"] += r["uncontested"] or 0
            m["last_win"] = max(m["last_win"], r["last_win"] or "")
            m["names"].add(r["winner"])
            if r.get("avg_bids"):
                m["_bidsum"] += float(r["avg_bids"]) * (r["wins"] or 1)
                m["_bidn"] += (r["wins"] or 1)
        rivals = []
        for m in merged.values():
            m["avg_bids"] = round(m["_bidsum"] / m["_bidn"], 1) if m["_bidn"] else None
            m["aliases"] = len(m["names"])
            for k in ("_bidsum", "_bidn", "names"):
                m.pop(k, None)
            rivals.append(m)
        rivals.sort(key=lambda x: (-x["value"], -x["wins"]))

        # ── RENEWAL CALENDAR. Split defend from attack, bucketed by month.
        cal = f(f"""
            SELECT substr(renewal_due,1,7) month,
                   COUNT(*) n,
                   SUM(COALESCE(value_num,0)) value,
                   SUM(CASE WHEN {OURS} THEN 1 ELSE 0 END) ours_n,
                   SUM(CASE WHEN {OURS} THEN COALESCE(value_num,0) ELSE 0 END) ours_value
            FROM awards
            WHERE renewal_due IS NOT NULL AND renewal_due >= date('now')
              AND renewal_due <= date('now', ?) AND lane IN ({lanes_sql})
            GROUP BY 1 ORDER BY 1""", (f"+{horizon_days} day",) + lanes)

        cols = ("ref, title, COALESCE(NULLIF(buyer_norm,''), buyer) buyer, winner, "
                "value_text, value_num, award_date, tenure_months, tenure_basis, "
                "renewal_due, lane, product_line, bids_received, url")

        # ── DEFEND. Ours, with what it took to win it and how contested.
        defend = f(f"""SELECT {cols},
                   CAST(julianday(renewal_due)-julianday('now') AS INTEGER) days_out
                   FROM awards
                   WHERE renewal_due >= date('now') AND {OURS}
                   ORDER BY renewal_due""")

        # ── ATTACK. Held by someone else, ranked by value and how weakly contested.
        attack = f(f"""SELECT {cols},
                   CAST(julianday(renewal_due)-julianday('now') AS INTEGER) days_out
                   FROM awards
                   WHERE renewal_due >= date('now')
                     AND renewal_due <= date('now', ?)
                     AND NOT {OURS} AND lane IN ({lanes_sql})
                   ORDER BY COALESCE(value_num,0) DESC, renewal_due
                   LIMIT 40""", (f"+{horizon_days} day",) + lanes)
        for r in attack:
            r["incumbent"] = _who(r["winner"]) or "Other"

        # ── OVERDUE. Buyers whose own cadence says a tender is due now.
        #    This is prediction from behaviour rather than from a notice.
        cadence = f(f"""
            SELECT COALESCE(NULLIF(buyer_norm,''), buyer) buyer, product_line, COUNT(*) n,
                   MIN(award_date) first_award, MAX(award_date) last_award,
                   CAST((julianday(MAX(award_date)) - julianday(MIN(award_date)))
                        / NULLIF(COUNT(*)-1,0) AS INTEGER) avg_gap_days,
                   CAST(julianday('now') - julianday(MAX(award_date)) AS INTEGER) since_last
            FROM awards
            WHERE award_date IS NOT NULL AND buyer != '' AND lane IN ({lanes_sql})
            GROUP BY COALESCE(NULLIF(buyer_norm,''), buyer), product_line
            HAVING n >= 2""", lanes)
        overdue = []
        for r in cadence:
            gap, since = r.get("avg_gap_days") or 0, r.get("since_last") or 0
            if gap >= 120 and since > gap * 1.05:
                r["overdue_by"] = since - gap
                r["confidence"] = ("high" if r["n"] >= 4 else
                                   "medium" if r["n"] == 3 else "low")
                overdue.append(r)
        overdue.sort(key=lambda x: -x["overdue_by"])
        overdue = overdue[:20]

        # ── PRICE BANDS and DENSITY per product line.
        price = f(f"""SELECT product_line, COUNT(*) n,
                   ROUND(MIN(value_num)) low, ROUND(AVG(value_num)) avg,
                   ROUND(MAX(value_num)) high, ROUND(AVG(bids_received),1) avg_bids,
                   SUM(CASE WHEN bids_received=1 THEN 1 ELSE 0 END) uncontested
                   FROM awards
                   WHERE value_num > 10000 AND product_line != ''
                     AND lane IN ({lanes_sql})
                   GROUP BY 1 ORDER BY SUM(value_num) DESC""", lanes)

        # ── OPEN PIPELINE, for sizing what is live right now.
        pipeline = f("""SELECT product_line, COUNT(*) n,
                   SUM(COALESCE(value_num, est_value_low, 0)) v
                   FROM tenders
                   WHERE is_opportunity=1 AND dup_of IS NULL
                     AND (closing >= date('now') OR (closing IS NULL AND COALESCE(advertised, substr(first_seen,1,10)) >= date('now','-45 day')))
                   GROUP BY 1 ORDER BY n DESC""")

        sectors = f(f"""SELECT COALESCE(buyer_sector,'Other') sector, COUNT(*) n,
                   SUM(COALESCE(value_num,0)) value,
                   SUM(CASE WHEN {OURS} THEN COALESCE(value_num,0) ELSE 0 END) ours
                   FROM awards WHERE lane IN ({lanes_sql})
                   GROUP BY 1 ORDER BY value DESC""", lanes)

        stats = dict(c.execute(f"""SELECT
                   COUNT(*) awards,
                   SUM(COALESCE(value_num,0)) total_value,
                   SUM(CASE WHEN {OURS} THEN COALESCE(value_num,0) ELSE 0 END) our_value,
                   SUM(CASE WHEN {OURS} THEN 1 ELSE 0 END) our_wins,
                   COUNT(DISTINCT winner) suppliers,
                   COUNT(DISTINCT buyer) buyers,
                   MIN(award_date) history_from
                   FROM awards""").fetchone())
        tv = stats.get("total_value") or 0
        stats["our_share"] = round(100 * (stats.get("our_value") or 0) / tv, 1) if tv else 0.0
        stats["defend_value"] = round(sum(x.get("value_num") or 0 for x in defend))
        stats["attack_value"] = round(sum(x.get("value_num") or 0 for x in attack))

    return {"accounts": accounts, "rivals": rivals, "calendar": cal,
            "sectors": sectors,
            "defend": defend, "attack": attack, "overdue": overdue,
            "price": price, "pipeline": pipeline, "stats": stats,
            "horizon_days": horizon_days}


def analytics(months=18):
    """Aggregates behind the Analytics dashboard.

    Every series is computed from stored rows, so each chart can be traced to
    the records that produced it. Value is shown only where a figure exists;
    disclosure rates are reported alongside so gaps are visible, not hidden.
    """
    O = OPEN_SQL
    with conn(readonly=True) as c:
        f = lambda sql, a=(): [dict(r) for r in c.execute(sql, a)]
        one = lambda sql, a=(): dict(c.execute(sql, a).fetchone() or {})

        kpi = one(f"""SELECT
            SUM(CASE WHEN is_opportunity=1 AND dup_of IS NULL AND {O} THEN 1 ELSE 0 END) open_opps,
            SUM(CASE WHEN lane='Core' AND dup_of IS NULL AND {O} THEN 1 ELSE 0 END) open_core,
            SUM(CASE WHEN is_opportunity=1 AND dup_of IS NULL AND {O}
                     THEN COALESCE(value_num, est_value_low, 0) ELSE 0 END) open_value,
            SUM(CASE WHEN is_opportunity=1 AND dup_of IS NULL
                      AND datetime(first_seen) >= datetime('now','-7 day') THEN 1 ELSE 0 END) new_7d,
            SUM(CASE WHEN is_opportunity=1 AND dup_of IS NULL
                      AND datetime(first_seen) <  datetime('now','-7 day')
                      AND datetime(first_seen) >= datetime('now','-14 day') THEN 1 ELSE 0 END) new_prev_7d,
            SUM(CASE WHEN is_opportunity=1 AND dup_of IS NULL AND {O}
                      AND (value_num IS NOT NULL OR est_value_low IS NOT NULL) THEN 1 ELSE 0 END) valued,
            COUNT(*) tracked,
            COUNT(DISTINCT COALESCE(NULLIF(buyer_norm,''), buyer)) buyers
            FROM tenders""")

        windows = [r["w"] for r in c.execute(
            """SELECT CAST(julianday(closing)-julianday(advertised) AS INTEGER) w
               FROM tenders WHERE is_opportunity=1 AND advertised IS NOT NULL
                 AND closing IS NOT NULL AND closing > advertised""")]
        windows = sorted(w for w in windows if 0 < w < 150)
        kpi["median_window"] = windows[len(windows) // 2] if windows else None
        buckets = [(0, 7, "under 7d"), (7, 14, "7-13d"), (14, 21, "14-20d"),
                   (21, 30, "21-29d"), (30, 999, "30d+")]
        window_hist = [{"k": lbl, "n": sum(1 for w in windows if lo <= w < hi)}
                       for lo, hi, lbl in buckets]

        monthly = f("""SELECT substr(COALESCE(advertised, first_seen),1,7) m, lane, COUNT(*) n
                       FROM tenders
                       WHERE lane IN ('Core','Partner-led','Signal') AND dup_of IS NULL
                         AND COALESCE(advertised, first_seen) >= date('now', ?)
                       GROUP BY 1,2 ORDER BY 1""", (f"-{months} month",))

        weeks = f(f"""SELECT strftime('%Y-%W', closing) wk, MIN(closing) wk_start, lane, COUNT(*) n
                      FROM tenders
                      WHERE is_opportunity=1 AND dup_of IS NULL
                        AND closing BETWEEN date('now') AND date('now','+56 day')
                      GROUP BY 1,3 ORDER BY 1""")

        heat = f("""SELECT COALESCE(buyer_sector,'Other') s, product_line p, COUNT(*) n
                    FROM tenders WHERE is_opportunity=1 AND dup_of IS NULL AND product_line != ''
                    GROUP BY 1,2""")

        types = f(f"""SELECT tender_type k, COUNT(*) n,
                      SUM(CASE WHEN {O} THEN 1 ELSE 0 END) open_n
                      FROM tenders WHERE is_opportunity=1 AND dup_of IS NULL
                        AND tender_type IS NOT NULL GROUP BY 1 ORDER BY n DESC""")

        juris = f("""SELECT COALESCE(jurisdiction,'Unknown') k, COUNT(*) n
                     FROM tenders WHERE is_opportunity=1 AND dup_of IS NULL
                     GROUP BY 1 ORDER BY n DESC""")

        top_buyers = f(f"""SELECT COALESCE(NULLIF(buyer_norm,''), buyer) k,
                           MAX(buyer_sector) sector, COUNT(*) n,
                           SUM(CASE WHEN {O} THEN 1 ELSE 0 END) open_n,
                           SUM(CASE WHEN lane='Core' THEN 1 ELSE 0 END) core_n
                           FROM tenders WHERE is_opportunity=1 AND dup_of IS NULL
                           GROUP BY 1 ORDER BY n DESC LIMIT 12""")

        # award side
        aw_month = f("""SELECT substr(COALESCE(signing_date, award_date),1,7) m,
                        COUNT(*) n, SUM(COALESCE(value_num,0)) v
                        FROM awards WHERE COALESCE(signing_date, award_date) IS NOT NULL
                          AND COALESCE(signing_date, award_date) >= date('now', ?)
                        GROUP BY 1 ORDER BY 1""", (f"-{months} month",))
        bids = f("""SELECT CASE WHEN bids_received >= 6 THEN '6+'
                               ELSE CAST(bids_received AS TEXT) END k, COUNT(*) n
                    FROM awards WHERE bids_received IS NOT NULL AND bids_received > 0
                    GROUP BY 1 ORDER BY MIN(bids_received)""")
        sup = f("""SELECT winner k, SUM(COALESCE(value_num,0)) v, COUNT(*) n
                   FROM awards WHERE winner IS NOT NULL AND winner != ''
                     AND lane IN ('Core','Partner-led')
                   GROUP BY 1 ORDER BY v DESC""")
        tot = sum(x["v"] for x in sup) or 0
        # Herfindahl-Hirschman Index on supplier value shares, 0 to 10,000
        hhi = round(sum((100 * x["v"] / tot) ** 2 for x in sup)) if tot else None
        aw_stats = one("""SELECT COUNT(*) n, SUM(COALESCE(value_num,0)) v,
                          ROUND(AVG(bids_received),1) avg_bids,
                          SUM(CASE WHEN bids_received=1 THEN 1 ELSE 0 END) single
                          FROM awards""")

    return {"kpi": kpi, "window_hist": window_hist, "monthly": monthly,
            "weeks": weeks, "heat": heat, "types": types, "juris": juris,
            "top_buyers": top_buyers, "aw_month": aw_month, "bids": bids,
            "suppliers": sup[:8], "supplier_total": tot, "hhi": hhi,
            "aw_stats": aw_stats}
