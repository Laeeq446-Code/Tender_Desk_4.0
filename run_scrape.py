#!/usr/bin/env python3
"""
run_scrape.py — Run scans from the command line.

    python run_scrape.py                          # all enabled sources
    python run_scrape.py --only EPMS              # one source
    python run_scrape.py --only PPRA-Punjab --debug --max-pages 2
    python run_scrape.py --list                   # show adapters + confidence

Use --debug when building a new adapter. It prints every URL fetched and the
response size, which is how you tell "wrong selector" from "page not reached".
"""

import argparse

import db
import sources as S
from app import scan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="single source name")
    ap.add_argument("--max-pages", type=int, help="cap pages per source (fast test)")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--list", action="store_true", help="list adapters and exit")
    a = ap.parse_args()

    if a.list:
        print(f"{'SOURCE':<22}{'TIER':<15}{'JURISDICTION':<22}CONFIDENCE")
        print("-" * 74)
        for n in S.all_sources():
            c = S.REGISTRY[n]
            print(f"{n:<22}{c.tier:<15}{c.jurisdiction:<22}{c.confidence}")
        print("\nproven   = tested against live HTML")
        print("drafted  = written from page structure, verify with --debug")
        print("needs-js = portal renders client side, limited without a browser")
        return

    db.init()
    out = scan(trigger="cli", only=a.only, max_pages=a.max_pages, debug=a.debug)
    print("\n" + "=" * 60)
    if not out.get("ok"):
        print("ERROR:", out.get("error"))
        return
    print(f"sources ok {out['sources_ok']} | failed {out['sources_failed']}")
    print(f"rows seen  {out['rows_seen']} | new {out['rows_new']}")
    f = db.facets()
    st = f["stats"]
    print(f"\nDB: {st['total']} tenders, {st['digital']} digital, "
          f"{st['open_digital']} open digital, {st['high_rel']} high relevance")
    print("\nBy source:")
    for h in f["health"]:
        flag = "ok " if h["status"] == "ok" else "FAIL"
        print(f"  {flag} {h['source']:<20} {h['total_rows']:>5} rows   {h['message'][:60]}")


if __name__ == "__main__":
    main()
