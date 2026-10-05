"""
backfill.py — Populate history once, then let the scans keep it current.

WHY
    EPADS assigns sequential integer IDs to every federal procurement, and
    each has a plain HTML detail page. The live adapter walks only the newest
    few hundred, which keeps the feed current but leaves years of public
    history unread. This sweeps the full range.

DESIGN
    Resumable and checkpointed. A long sweep will be interrupted: the process
    restarts, the host redeploys, the network drops. Progress is written to the
    database after every batch, so a restart continues where it stopped rather
    than beginning again.

    Rate limited by default. This is a government portal and there is no
    deadline; politeness costs nothing.

USE
    python backfill.py --source EPADS --from-top --workers 4
    python backfill.py --source EPADS --start 74221 --end 1
    python backfill.py --source WorldBank --pages 60
    python backfill.py --status
"""

import argparse
import concurrent.futures as cf
import sqlite3
import sys
import time

import db
import sources as S

MONTHS_BACK = 24

CHECKPOINT_SQL = """
CREATE TABLE IF NOT EXISTS backfill (
    source     TEXT PRIMARY KEY,
    cursor     INTEGER,
    lowest     INTEGER,
    highest    INTEGER,
    done       INTEGER DEFAULT 0,
    rows_added INTEGER DEFAULT 0,
    misses     INTEGER DEFAULT 0,
    updated    TEXT,
    note       TEXT
);
"""


def init():
    db.init()
    with db.conn() as c:
        c.executescript(CHECKPOINT_SQL)


def get_cp(source):
    with db.conn(readonly=True) as c:
        r = c.execute("SELECT * FROM backfill WHERE source=?", (source,)).fetchone()
    return dict(r) if r else None


def set_cp(source, **kw):
    cur = get_cp(source) or {"source": source, "cursor": None, "lowest": None,
                             "highest": None, "done": 0, "rows_added": 0,
                             "misses": 0, "note": ""}
    cur.update(kw)
    cur["updated"] = db.now()
    with db.conn() as c:
        c.execute("""INSERT INTO backfill
            (source,cursor,lowest,highest,done,rows_added,misses,updated,note)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source) DO UPDATE SET
              cursor=excluded.cursor, lowest=excluded.lowest,
              highest=excluded.highest, done=excluded.done,
              rows_added=excluded.rows_added, misses=excluded.misses,
              updated=excluded.updated, note=excluded.note""",
                  (source, cur["cursor"], cur["lowest"], cur["highest"],
                   cur["done"], cur["rows_added"], cur["misses"],
                   cur["updated"], cur["note"]))


def status():
    init()
    with db.conn(readonly=True) as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM backfill")]
        tot = c.execute("SELECT COUNT(*) n FROM tenders").fetchone()["n"]
        per = [dict(r) for r in c.execute(
            "SELECT source, COUNT(*) n FROM tenders GROUP BY source ORDER BY n DESC")]
    print(f"tenders in database: {tot}")
    for p in per:
        print(f"  {p['source']:<22}{p['n']:>7}")
    if not rows:
        print("\nno backfill has been started")
        return
    print()
    for r in rows:
        span = (r["highest"] or 0) - (r["lowest"] or 0)
        seen = (r["highest"] or 0) - (r["cursor"] or r["highest"] or 0)
        pct = int(100 * seen / span) if span else 0
        print(f"{r['source']}: cursor {r['cursor']} of {r['highest']}..{r['lowest']} "
              f"({pct}% swept) rows_added {r['rows_added']} "
              f"{'DONE' if r['done'] else 'in progress'} @ {r['updated']}")


# ─────────────────────────────────────────── EPADS sweep
def sweep_epads(start=None, end=1, batch=60, workers=4, delay=0.35,
                stop_after_misses=600, max_batches=None, verbose=True):
    """Walk the ID range downward, writing a checkpoint after every batch."""
    init()
    src = S.get_source("EPADS")
    if src is None:
        print("EPADS adapter not registered")
        return

    cp = get_cp("EPADS")
    if start is None:
        if cp and cp.get("cursor"):
            start = int(cp["cursor"])
            if verbose:
                print(f"resuming from checkpoint at id {start}")
        else:
            start = discover_top(src)
            if not start:
                print("could not determine the highest id; pass --start")
                return
            if verbose:
                print(f"highest id on the board: {start}")

    highest = (cp or {}).get("highest") or start
    added_total = (cp or {}).get("rows_added") or 0
    misses = 0
    cursor = start
    batches = 0
    t0 = time.time()

    while cursor >= end:
        ids = list(range(cursor, max(end - 1, cursor - batch), -1))
        if not ids:
            break
        rows = fetch_ids(src, ids, workers, delay)
        if rows:
            _seen, new, _ch = db.upsert(rows, "EPADS", rebuild_fts=False)
            added_total += new
            misses = 0
        else:
            misses += len(ids)

        cursor = ids[-1] - 1
        batches += 1
        if batches % 25 == 0:
            db.fts_rebuild()
        set_cp("EPADS", cursor=cursor, lowest=end, highest=highest,
               rows_added=added_total, misses=misses,
               note=f"batch {batches}")

        if verbose:
            rate = (highest - cursor) / max(1, time.time() - t0)
            left = max(0, cursor - end) / rate if rate else 0
            print(f"  id {cursor:>7} | +{len(rows):>3} rows | total {added_total:>6} "
                  f"| {rate:.1f} id/s | ~{left/3600:.1f}h remaining")

        if misses >= stop_after_misses:
            print(f"stopping: {misses} consecutive ids returned nothing, "
                  f"the range below here is probably empty")
            break
        if max_batches and batches >= max_batches:
            print("batch limit reached; rerun to continue from the checkpoint")
            return

    db.fts_rebuild()
    db.find_repeats()
    set_cp("EPADS", cursor=cursor, lowest=end, highest=highest,
           rows_added=added_total, done=1 if cursor < end else 0,
           note="complete" if cursor < end else "paused")
    print(f"\nsweep finished at id {cursor}. rows added this run and previously: "
          f"{added_total}")


def discover_top(src):
    import re
    try:
        sp = src.soup(src.home)
    except Exception as e:
        print(f"root page unreachable: {e}")
        return None
    ids = set()
    for a in sp.find_all("a", href=True):
        m = re.search(r"/procurements/(\d+)", a["href"])
        if m:
            ids.add(int(m.group(1)))
    return max(ids) if ids else None


def fetch_ids(src, ids, workers, delay):
    """Fetch a batch of detail pages in parallel, politely."""
    from bs4 import BeautifulSoup
    out = []

    def one(pid):
        time.sleep(delay)
        url = src.home + src.DETAIL.format(pid)
        try:
            r = src.s.get(url, timeout=25)
            if r.status_code != 200:
                return None
            row = src.parse_detail(BeautifulSoup(r.text, "lxml"), url, pid)
            if not row or not row.get("title"):
                return None
            from classify import classify
            row.update(classify(row.get("title"), row.get("description"),
                                row.get("buyer")))
            row.setdefault("source_tier", src.tier)
            row.setdefault("jurisdiction", src.jurisdiction)
            row["source"] = "EPADS"
            return row
        except Exception:
            return None

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(one, ids):
            if r:
                out.append(r)
    return out


# ─────────────────────────────────────────── World Bank history
def sweep_worldbank(pages=60):
    """The live adapter caps at 5 pages to stay quick. History needs depth."""
    init()
    src = S.get_source("WorldBank", max_pages=pages)
    if src is None:
        print("WorldBank adapter not registered")
        return
    rows = src.run()
    seen, new, ch = db.upsert(rows, "WorldBank")
    set_cp("WorldBank", cursor=0, lowest=0, highest=pages,
           rows_added=new, done=1, note=f"{pages} pages")
    print(f"WorldBank: {seen} rows seen, {new} new")


def sweep_generic(name, pages=200):
    """Deep sweep for any paging adapter. Awards go to their own table."""
    init()
    src = S.get_source(name, max_pages=pages)
    if src is None:
        print(f"{name}: no such adapter")
        return
    if name == "EPMS-Awards":
        src.months_back = MONTHS_BACK      # sweep award history month by month
        src.detail_limit = 2000
    print(f"[{name}] sweeping up to {pages} pages")
    try:
        rows = src.run()
    except Exception as e:
        print(f"[{name}] failed: {type(e).__name__}: {e}")
        set_cp(name, cursor=0, done=0, note=f"failed: {e}"[:120])
        return
    if name.endswith("Awards"):
        seen, new = db.upsert_awards(rows, name)
        ch = 0
    else:
        seen, new, ch = db.upsert(rows, name, rebuild_fts=False)
        db.fts_rebuild()
    set_cp(name, cursor=0, lowest=0, highest=pages, rows_added=new, done=1,
           note=f"{seen} rows seen")
    print(f"[{name}] {seen} rows seen, {new} new")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="EPADS",
                    help="EPADS, WorldBank, all-archives, or any adapter name")
    ap.add_argument("--start", type=int, help="highest id to begin from")
    ap.add_argument("--end", type=int, default=1, help="lowest id to stop at")
    ap.add_argument("--from-top", action="store_true",
                    help="ignore the checkpoint and rediscover the highest id")
    ap.add_argument("--batch", type=int, default=60)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--delay", type=float, default=0.35,
                    help="seconds each worker waits before a request")
    ap.add_argument("--max-batches", type=int,
                    help="stop after N batches, for a timed run")
    ap.add_argument("--pages", type=int, default=60, help="WorldBank pages")
    ap.add_argument("--months", type=int, default=24,
                    help="EPMS-Awards: months of history to sweep")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()

    global MONTHS_BACK
    MONTHS_BACK = a.months
    if a.status:
        status()
        return
    if a.source == "WorldBank":
        sweep_worldbank(pages=a.pages)
        return
    if a.source in ("all-archives", "archives"):
        for name in ("PPRA-Punjab-Archive", "SPPRA-Sindh-Archive",
                     "KPPRA-KP-Archive", "EPMS", "EPMS-Awards", "ADB", "UNGM"):
            sweep_generic(name, pages=a.pages)
        sweep_worldbank(pages=a.pages)
        return
    if a.source != "EPADS":
        sweep_generic(a.source, pages=a.pages)
        return

    start = a.start
    if a.from_top:
        start = None
        set_cp("EPADS", cursor=None)
    sweep_epads(start=start, end=a.end, batch=a.batch, workers=a.workers,
                delay=a.delay, max_batches=a.max_batches)


if __name__ == "__main__":
    main()
