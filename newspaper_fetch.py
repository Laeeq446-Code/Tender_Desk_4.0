"""
newspaper_fetch.py — Acquire newspaper e-papers automatically, then OCR them.

THE CAPABILITY, BUILT
    newspaper.py already turns a page into structured tender rows. This is the
    missing half: it fetches the page on its own. No upload, no manual step.

HOW IT GETS THE PAGE
    For each configured paper it builds candidate URLs for today (and yesterday
    as a fallback), tries each in turn, and stops at the first that returns a
    file. A URL may point straight at a PDF or image, or at a landing page; in
    the landing-page case the resolver reads the HTML and follows the first real
    PDF or drive link on it. Whatever comes back goes through newspaper.ingest,
    so classification, date parsing and the needs_review flag behave exactly as
    a hand upload would.

    Configured out of the box: a free daily-PDF source that publishes the major
    English and Urdu papers (Nation, Express, Dawn, The News, Business Recorder)
    at dated URLs, plus slots for each paper's own e-paper. Fill or change these
    freely; they are data, not logic.

RELIABILITY AND HONESTY
    E-paper sites move their URLs and rate-limit. This engine probes several
    patterns and degrades to yesterday's edition rather than failing outright,
    and `--probe` reports exactly which sources resolved so a broken pattern is
    a one-line config fix, not a mystery. Terms of use are the operator's call,
    so automated fetch stays behind NEWSPAPER_FETCH_ENABLED=1. Folder ingestion
    of licensed copies is always on.

USAGE
    python newspaper_fetch.py --probe                 # which sources resolve today
    NEWSPAPER_FETCH_ENABLED=1 python newspaper_fetch.py --fetch
    NEWSPAPER_FETCH_ENABLED=1 python newspaper_fetch.py --fetch --only Nation
    python newspaper_fetch.py --folder ./epapers      # licensed copies
    python newspaper_fetch.py --list
"""

import os
import re
import sys
import argparse
import tempfile
from datetime import date, timedelta

import newspaper
import db

FETCH_ENABLED = os.environ.get("NEWSPAPER_FETCH_ENABLED", "0") == "1"
IMG_EXT = (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ── Source configuration ────────────────────────────────────────────────────
# Each source builds a list of candidate URLs for a given date. The first that
# resolves to a file wins. A candidate may be a direct PDF or a landing page.
# Slugs and patterns are the parts most likely to drift; keep them here.
def _pnp(slug):
    # pakistan-newspaper-pdf.com landing-page pattern (resolver follows to PDF)
    def build(d):
        return [
            f"https://www.pakistan-newspaper-pdf.com/{d:%Y}/{d:%m}/"
            f"download-{slug}-pdf-{d:%d-%m-%Y}.html",
            f"https://www.pakistan-newspaper-pdf.com/{d:%Y}/{d:%m}/"
            f"download-{slug}-pdf-{d:%d-%m}.html",
            f"https://www.pakistan-newspaper-pdf.com/{d:%Y}/{d:%m}/"
            f"download-{slug}-newspaper-pdf-{d:%d-%m}.html",
        ]
    return build

SOURCES = {
    "Nation":          {"enabled": True,  "build": _pnp("daily-nation")},
    "Express":         {"enabled": True,  "build": _pnp("express-newspaper")},
    "TheNews":         {"enabled": True,  "build": _pnp("the-news")},
    "BusinessRecorder":{"enabled": True,  "build": _pnp("business-recorder")},
    "Dawn":            {"enabled": True,  "build": _pnp("dawn")},
    # Add a paper's own e-paper here, e.g.:
    # "MyPaper": {"enabled": True, "build": lambda d: [f"https://epaper.x.pk/{d:%Y-%m-%d}/tenders.pdf"]},
}


def _session():
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "*/*"})
    return s


def _looks_like_file(resp):
    ct = resp.headers.get("Content-Type", "").lower()
    return ("pdf" in ct or "image" in ct or "octet-stream" in ct)


def _find_pdf_link(html, base):
    """From a landing page, return the first real PDF or drive download link."""
    from urllib.parse import urljoin
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        hrefs = [a.get("href", "") for a in soup.find_all("a", href=True)]
    except Exception:
        hrefs = re.findall(r'href=["\']([^"\']+)["\']', html)
    for h in hrefs:
        low = h.lower()
        if low.endswith(".pdf") or "drive.google.com/uc" in low or "/download" in low \
           or "dropbox.com" in low and "dl=1" in low:
            return urljoin(base, h)
    # google drive "view" links -> convert to direct download
    for h in hrefs:
        m = re.search(r"drive\.google\.com/file/d/([\w-]+)", h)
        if m:
            return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    return None


def _download(sess, url, dest, depth=0):
    """Fetch url to dest. Follows a landing page to its PDF once."""
    r = sess.get(url, timeout=90, stream=True, allow_redirects=True)
    r.raise_for_status()
    if _looks_like_file(r):
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(65536):
                fh.write(chunk)
        return dest
    if depth == 0 and "html" in r.headers.get("Content-Type", "").lower():
        link = _find_pdf_link(r.text, url)
        if link:
            return _download(sess, link, dest, depth + 1)
    raise ValueError("no file at URL (and no PDF link on the page)")


def resolve(name, entry, day, sess):
    """Try today's candidates, then yesterday's. Return a saved path or None."""
    for d in (day, day - timedelta(days=1)):
        for url in entry["build"](d):
            dest = os.path.join(tempfile.gettempdir(), f"np_{name}_{d:%Y%m%d}.bin")
            try:
                _download(sess, url, dest)
                return dest, url, d
            except Exception:
                continue
    return None, None, None


def _write(rows, paper):
    for r in rows:
        r["needs_review"] = 1
        r.setdefault("source_tier", "Newspaper")
        r.setdefault("jurisdiction", "Newspaper")
    return db.upsert(rows, source=f"Newspaper-{paper}")


def ingest_file(path, paper, dry_run=False):
    rows, diag = newspaper.ingest(path, paper=paper)
    if diag.get("error"):
        print(f"  ! {paper}: {diag['error']}")
        return 0
    if dry_run:
        print(f"  {paper}: {diag}")
        return len(rows)
    seen, new, ch = _write(rows, paper)
    print(f"  {paper}: {diag.get('mode')}, {diag.get('tenders_found')} tenders, {new} new")
    return len(rows)


def run_fetch(only=None, day=None, dry_run=False, probe=False):
    if not (FETCH_ENABLED or probe):
        print("Fetch is off. Enable with NEWSPAPER_FETCH_ENABLED=1 once the")
        print("terms-of-use decision is made. --probe works without enabling.")
        return 0
    day = day or date.today()
    sess = _session()
    total = 0
    active = {k: v for k, v in SOURCES.items()
              if v.get("enabled") and (not only or k == only)}
    if not active:
        print("no matching enabled sources.")
        return 0
    for name, entry in active.items():
        path, url, d = resolve(name, entry, day, sess)
        if not path:
            print(f"  {name}: no edition resolved (patterns may have moved)")
            continue
        size = os.path.getsize(path)
        print(f"  {name}: got {d:%Y-%m-%d} ({size//1024} KB) from {url[:60]}…")
        if probe:
            os.unlink(path)
            continue
        try:
            total += ingest_file(path, name, dry_run)
        finally:
            try: os.unlink(path)
            except OSError: pass
    return total


def run_folder(folder, paper=None, dry_run=False, archive=True):
    if not os.path.isdir(folder):
        return 0
    files = [f for f in sorted(os.listdir(folder)) if f.lower().endswith(IMG_EXT)]
    if not files:
        return 0
    print(f"FOLDER {folder}: {len(files)} file(s)")
    total = 0
    done = os.path.join(folder, "_processed")
    for f in files:
        p = paper or re.split(r"[-_.\s]", f)[0].title()
        total += ingest_file(os.path.join(folder, f), p, dry_run)
        if archive and not dry_run:
            os.makedirs(done, exist_ok=True)
            try: os.replace(os.path.join(folder, f), os.path.join(done, f))
            except OSError: pass
    return total


def auto_ingest(dry_run=False):
    """Unattended entry point for the scheduler. Never raises."""
    drop = os.environ.get("NEWSPAPER_DROP",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "epapers"))
    total, notes = 0, []
    try:
        os.makedirs(drop, exist_ok=True)
        n = run_folder(drop, dry_run=dry_run); total += n or 0; notes.append(f"folder:{n or 0}")
    except Exception as e:
        notes.append(f"folder-error:{type(e).__name__}")
    if FETCH_ENABLED:
        try:
            n = run_fetch(dry_run=dry_run); total += n or 0; notes.append(f"fetch:{n or 0}")
        except Exception as e:
            notes.append(f"fetch-error:{type(e).__name__}")
    return {"rows": total, "notes": ", ".join(notes), "drop_folder": drop}


def probe_sources(only=None, day=None):
    """Diagnostic: which papers resolve to a file right now. No writing."""
    day = day or date.today()
    sess = _session()
    out = []
    for name, entry in SOURCES.items():
        if not entry.get("enabled") or (only and name != only):
            continue
        path, url, d = resolve(name, entry, day, sess)
        if path:
            try:
                kb = os.path.getsize(path) // 1024
                os.unlink(path)
            except OSError:
                kb = 0
            out.append({"paper": name, "ok": True,
                        "detail": f"{d:%Y-%m-%d}, {kb} KB"})
        else:
            out.append({"paper": name, "ok": False,
                        "detail": "no edition resolved (URL pattern may have moved)"})
    return out


def main():
    ap = argparse.ArgumentParser(description="Acquire and OCR newspaper tenders.")
    ap.add_argument("--fetch", action="store_true", help="download configured papers")
    ap.add_argument("--probe", action="store_true", help="report which sources resolve today")
    ap.add_argument("--folder", metavar="DIR", help="ingest a folder of licensed files")
    ap.add_argument("--only", metavar="NAME", help="restrict --fetch/--probe to one paper")
    ap.add_argument("--paper", metavar="NAME", help="force paper name for --folder")
    ap.add_argument("--dry-run", action="store_true", help="parse, do not write")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        print(f"FETCH enabled: {FETCH_ENABLED}")
        for k, v in SOURCES.items():
            print(f"  {k:18} enabled={v.get('enabled')}")
        return
    if not a.dry_run and not a.probe:
        db.init()
    if a.probe:
        run_fetch(only=a.only, probe=True)
    elif a.folder:
        run_folder(a.folder, paper=a.paper, dry_run=a.dry_run)
    elif a.fetch:
        run_fetch(only=a.only, dry_run=a.dry_run)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
