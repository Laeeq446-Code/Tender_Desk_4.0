"""
forecast.py — The intelligence layer. What the award history predicts.

WHAT THIS IS
    Detection tells you what is open now. This tells you what is coming and
    who to watch. It reads the awards table only, never writes, and turns a
    flat log of past awards into three forward signals.

    RENEWALS   A contract with a known tenure expires on a known date, and the
               retender opens some months before that. This is the highest
               value signal and the closest thing to certainty in the pipeline.
    CADENCE    Which institutions award digital work, how often, at what value,
               and how long since their last award. A buyer that awards every
               quarter and last awarded four months ago is overdue.
    VELOCITY   Which product lines and buyers are accelerating, by award count
               and value over time. This is the demand trend beneath the noise.

HONEST STATE OF THE DATA  (read this before trusting the renewals view)
    Renewal forecasting needs contract tenure and it is present on almost no
    rows today, because the EPMS-Awards scrape does not yet capture the tenure
    or the winning bidder. Until that adapter is fixed, RENEWALS runs on the
    handful of rows that carry a tenure and reports its own coverage honestly.
    CADENCE and VELOCITY work now, on award_date, value and buyer, which are
    well populated.

    THE ONE UPSTREAM FIX that unlocks renewals: extend the EPMS-Awards adapter
    in sources.py to read contract period and awarded bidder from the award
    notice, and store them in tenure_months and winner. Depth then compounds
    from the day it ships, so ship it early even if imperfect.

USAGE
    python forecast.py                 # full brief to stdout
    python forecast.py --renewals 180  # renewal windows opening within N days
    python forecast.py --cadence       # buyer cadence table
    python forecast.py --velocity      # demand trend by month and product line
    DB_PATH=tenders.db python forecast.py

    Every function also returns plain dicts, so app.py can expose them as
    /api/intelligence endpoints without change.
"""

import os
import sqlite3
import argparse
from datetime import date, datetime, timedelta
from collections import defaultdict
from statistics import median

DB_PATH = os.environ.get("DB_PATH", "tenders.db")

# Jazz-relevant lanes. NONE-lane awards are civil works, fuel, printing and
# the like. They are demand signal for the country, not for Jazz, so the
# intelligence layer excludes them by default.
RELEVANT_LANES = ("Core", "Partner-led", "Signal")

# How early a retender typically opens ahead of contract expiry. Public buyers
# in Pakistan generally float the successor two to four months out; three is a
# defensible planning midpoint. Override per your own observation.
RENEWAL_LEAD_MONTHS = 3


def _conn():
    c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _pdate(s):
    if not s:
        return None
    s = str(s)[:10]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            d = datetime.strptime(s, fmt).date()
            # award scrape occasionally mangles a year ("0206"). Anything
            # outside a sane procurement window is a parse artefact, not data.
            if 2015 <= d.year <= date.today().year + 1:
                return d
            return None
        except ValueError:
            continue
    return None


def _add_months(d, n):
    m = d.month - 1 + int(n)
    y = d.year + m // 12
    return date(y, m % 12 + 1, min(d.day, 28))


def _rows(relevant_only=True):
    with _conn() as c:
        rows = c.execute("SELECT * FROM awards").fetchall()
    if relevant_only:
        rows = [r for r in rows if (r["lane"] or "") in RELEVANT_LANES]
    return rows


# ── RENEWALS ────────────────────────────────────────────────────────────────
def upcoming_renewals(within_days=180, relevant_only=True):
    """Contracts whose retender window opens within the horizon.

    Returns {coverage, horizon_days, items:[...]}. Coverage names how many
    relevant awards carried a tenure, so a thin list is never mistaken for a
    quiet market.
    """
    rows = _rows(relevant_only)
    with_tenure = [r for r in rows if r["tenure_months"]]
    today = date.today()
    horizon = today + timedelta(days=within_days)
    items = []
    for r in with_tenure:
        ad = _pdate(r["award_date"])
        if not ad:
            continue
        expiry = _add_months(ad, r["tenure_months"])
        window = _add_months(expiry, -RENEWAL_LEAD_MONTHS)
        if today <= window <= horizon or today <= expiry <= horizon:
            items.append({
                "buyer": r["buyer"],
                "title": r["title"],
                "product_line": r["product_line"],
                "value_num": r["value_num"],
                "award_date": r["award_date"],
                "tenure_months": r["tenure_months"],
                "expiry": expiry.isoformat(),
                "window_opens": window.isoformat(),
                "days_to_window": (window - today).days,
            })
    items.sort(key=lambda x: x["window_opens"])
    return {
        "coverage": f"{len(with_tenure)} of {len(rows)} relevant awards carry a tenure",
        "horizon_days": within_days,
        "items": items,
    }


# ── CADENCE ───────────────────────────────────────────────────────────────��─
def buyer_cadence(relevant_only=True, min_awards=1):
    """Per-buyer award rhythm and how overdue each buyer is for its next one."""
    rows = _rows(relevant_only)
    by_buyer = defaultdict(list)
    for r in rows:
        d = _pdate(r["award_date"])
        if r["buyer"] and d:
            by_buyer[r["buyer"]].append((d, r["value_num"] or 0.0, r["product_line"]))

    today = date.today()
    out = []
    for buyer, awards in by_buyer.items():
        if len(awards) < min_awards:
            continue
        awards.sort()
        dates = [a[0] for a in awards]
        values = [a[1] for a in awards if a[1]]
        span_days = (dates[-1] - dates[0]).days
        n = len(awards)
        # mean interval between awards, only meaningful with two or more
        interval = round(span_days / (n - 1)) if n > 1 else None
        since_last = (today - dates[-1]).days
        lines = defaultdict(int)
        for a in awards:
            lines[a[2] or "Unspecified"] += 1
        out.append({
            "buyer": buyer,
            "awards": n,
            "total_value": round(sum(values)) if values else None,
            "median_value": round(median(values)) if values else None,
            "first_award": dates[0].isoformat(),
            "last_award": dates[-1].isoformat(),
            "mean_interval_days": interval,
            "days_since_last": since_last,
            # overdue if a regular buyer has been silent longer than its rhythm
            "overdue": bool(interval and since_last > interval * 1.25),
            "top_line": max(lines, key=lines.get),
        })
    out.sort(key=lambda x: (x["awards"], x["total_value"] or 0), reverse=True)
    return out


# ── VELOCITY ──────────────────────────────────────────────────────────────��─
def demand_velocity(relevant_only=True):
    """Award count and value by month, and the split by product line."""
    rows = _rows(relevant_only)
    by_month = defaultdict(lambda: {"n": 0, "value": 0.0})
    by_line = defaultdict(lambda: {"n": 0, "value": 0.0})
    for r in rows:
        d = _pdate(r["award_date"])
        v = r["value_num"] or 0.0
        if d:
            k = d.strftime("%Y-%m")
            by_month[k]["n"] += 1
            by_month[k]["value"] += v
        line = r["product_line"] or "Unspecified"
        by_line[line]["n"] += 1
        by_line[line]["value"] += v
    months = [{"month": k, "awards": v["n"], "value": round(v["value"])}
              for k, v in sorted(by_month.items())]
    lines = [{"product_line": k, "awards": v["n"], "value": round(v["value"])}
             for k, v in sorted(by_line.items(), key=lambda kv: kv[1]["n"], reverse=True)]
    return {"by_month": months, "by_line": lines}


# ── BRIEF ─────────────────────────────────────────────────────────────────��─
def _money(n):
    if not n:
        return "-"
    if n >= 1e9:
        return f"Rs {n/1e9:.2f}bn"
    if n >= 1e6:
        return f"Rs {n/1e6:.1f}m"
    return f"Rs {n:,.0f}"


def brief(horizon=180):
    out = []
    total = len(_rows(relevant_only=False))
    rel = len(_rows(relevant_only=True))
    out.append(f"AWARD INTELLIGENCE  ({rel} Jazz-relevant of {total} total awards)\n")

    r = upcoming_renewals(horizon)
    out.append(f"RENEWALS opening within {horizon} days")
    out.append(f"  coverage: {r['coverage']}")
    if r["items"]:
        for it in r["items"][:15]:
            out.append(f"  {it['window_opens']}  {it['buyer'][:32]:32}  "
                       f"{_money(it['value_num'])}  {it['product_line']}")
    else:
        out.append("  none forecastable yet. Fix EPMS-Awards tenure capture to populate this.")
    out.append("")

    out.append("BUYER CADENCE  (digital-relevant awards, most active first)")
    out.append(f"  {'buyer':34}{'n':>3}  {'interval':>9}  {'since':>6}  {'total':>12}  flag")
    for b in buyer_cadence()[:15]:
        iv = f"{b['mean_interval_days']}d" if b["mean_interval_days"] else "-"
        out.append(f"  {b['buyer'][:33]:34}{b['awards']:>3}  {iv:>9}  "
                   f"{b['days_since_last']:>5}d  {_money(b['total_value']):>12}  "
                   f"{'OVERDUE' if b['overdue'] else ''}")
    out.append("")

    v = demand_velocity()
    out.append("DEMAND BY PRODUCT LINE")
    for l in v["by_line"]:
        out.append(f"  {l['product_line'][:28]:28}{l['awards']:>4}  {_money(l['value'])}")
    out.append("")
    out.append("DEMAND BY MONTH")
    for m in v["by_month"][-6:]:
        out.append(f"  {m['month']}  {m['awards']:>3} awards  {_money(m['value'])}")
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Award intelligence for Tender Desk.")
    ap.add_argument("--renewals", type=int, metavar="DAYS", nargs="?", const=180)
    ap.add_argument("--cadence", action="store_true")
    ap.add_argument("--velocity", action="store_true")
    ap.add_argument("--all-lanes", action="store_true",
                    help="include NONE-lane awards (whole-of-government view)")
    a = ap.parse_args()
    rel = not a.all_lanes

    if a.renewals is not None:
        r = upcoming_renewals(a.renewals, relevant_only=rel)
        print(r["coverage"])
        for it in r["items"]:
            print(f"{it['window_opens']}  {it['buyer'][:36]:36}  "
                  f"{_money(it['value_num'])}  {it['product_line']}")
    elif a.cadence:
        for b in buyer_cadence(relevant_only=rel):
            print(f"{b['buyer'][:36]:36}  n={b['awards']:<3} "
                  f"interval={b['mean_interval_days']}  since={b['days_since_last']}d  "
                  f"{'OVERDUE' if b['overdue'] else ''}")
    elif a.velocity:
        v = demand_velocity(relevant_only=rel)
        for l in v["by_line"]:
            print(f"{l['product_line'][:30]:30} {l['awards']:>4}  {_money(l['value'])}")
    else:
        print(brief())
