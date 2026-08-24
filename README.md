# Tender Desk

Digital procurement intelligence across Pakistani federal, provincial and
multilateral tender portals. Scans four times a day, classifies everything for
digital and Jazz relevance, serves a filterable web dashboard behind a
passphrase.

No API keys. No AI dependency. Pure scraping plus rules.

---

## Quick start

```bash
pip install -r requirements.txt

python run_scrape.py --only EPMS        # populate from the proven source
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 and enter the passphrase. Default is `jazz2026`,
change it with the `PASSPHRASE` environment variable.

On Windows use `py` in place of `python3`.

---

## The four tabs

**Tender feed.** Every digital tender across all sources, grouped by date
advertised. Filter by source, jurisdiction, digital domain, Jazz relevance,
closing window, and open or closed status. Full-text search across titles,
descriptions and buyers. Each row carries a closing gauge: red under five days,
amber under fourteen.

**Tender detail.** Click any tender and it opens as its own tab. Up to four stay
open at once for side-by-side comparison, which is the multi-tender view you
asked for. Each detail shows the published record, why the classifier tagged it
as it did, any changes since first seen, and which other portals carry the same
tender.

**Institution contacts.** Buyers ranked by digital tender volume, with published
contact details and their average bidding window. That last figure tells you how
much lead time each institution typically gives.

**Recommended.** Present but marked as mock. Fit scoring and the pursuit list
are deliberately held back from any hosted build, because they reveal what Jazz
is chasing. Turn them on only once this sits on Jazz infrastructure.

---

## Sources

| Adapter | Tier | Jurisdiction | Confidence |
|---|---|---|---|
| `EPMS` | Federal | Federal | **proven** |
| `EPADS` | Federal | Federal | needs-js |
| `PPRA-Punjab` | Provincial | Punjab | drafted |
| `SPPRA-Sindh` | Provincial | Sindh | drafted |
| `KPPRA-KP` | Provincial | Khyber Pakhtunkhwa | drafted |
| `BPPRA-Balochistan` | Provincial | Balochistan | drafted |
| `WorldBank` | Multilateral | International | drafted |
| `ADB` | Multilateral | International | drafted |
| `UNGM` | Multilateral | International | drafted |

**Read the confidence column honestly.** Only EPMS has been run against live
HTML and verified. The rest were written from observed page structure but could
not be tested, because the build environment has no network access to those
domains. Expect some to return zero rows on first run and need a selector fix.

That is why every adapter is isolated and why source health is on the dashboard.
A broken adapter shows as failed rather than quietly implying no new tenders.

**To verify one:**

```bash
python run_scrape.py --only PPRA-Punjab --debug --max-pages 2
```

`--debug` prints every URL fetched and the response size, which distinguishes
"wrong selector" from "page never reached". Paste the output and the fix is
usually a few lines in that adapter's class in `sources.py`.

**To list all adapters:**

```bash
python run_scrape.py --list
```

---

## Scanning

Runs automatically at 09:00, 12:00, 15:00 and 18:00 Pakistan time. Change with:

```bash
export SCAN_HOURS=9,12,15,18
export TZ_OFFSET=5
```

Manual scan from the command line or the **Scan now** button in the dashboard:

```bash
python run_scrape.py                    # all enabled sources
python run_scrape.py --only WorldBank   # one source
```

Enable or disable sources without touching code:

```bash
export ENABLED_SOURCES=EPMS,EPADS,PPRA-Punjab,WorldBank
```

Start with `EPMS` alone until the others are verified.

---

## What counts as digital

Defined broadly and deliberately, in `classify.py`. Seven domains:

Connectivity, Cloud and Data Centre, Cybersecurity, Data and AI, Software and
Platforms, Digital Services, ICT Hardware.

Three design decisions worth knowing:

**Portal categories are ignored.** On PPRA, fibre tenders sit under
"Miscellaneous" and data centre servers under "Electrical Items". Every
classification is made from title and description text.

**False positives are actively suppressed.** Power transmission "towers",
electrical "switchgear", and HT/LT power "cables" are rejected, because
otherwise every DISCO tender floods the Connectivity domain. The guards are in
`NEGATIVE_CONTEXT`.

**Every tag is auditable.** Each classified tender stores the exact keywords
that triggered it, shown in the detail view under "Why this was classified".
When it gets something wrong, you can see why and fix the rule.

Jazz relevance is layered on top. **High** means a core capability keyword
(fibre, spectrum, SMS gateway, IoT, base station) or a named account
(NTC, USF, PTA, NADRA, PITB). **Medium** means digital but not core telecom.

Tune the keyword lists in `classify.py` as you see misfires. That file is meant
to be edited.

---

## Deduplication

The same tender appears on EPADS, on a provincial portal, and on an aggregator
under three different reference numbers. Rows are hashed on a normalised
title, buyer and closing date triple. The first source to publish it wins, and
the others are recorded as alternates, shown in the detail view.

Without this the feed looks noisy and people stop trusting it.

---

## Deployment

Any host that runs Python works. Railway, Render, Fly.io, or a Jazz VM.

```bash
# Procfile
web: uvicorn app:app --host 0.0.0.0 --port $PORT
```

Environment variables:

```
PASSPHRASE       = something-better-than-the-default
DB_PATH          = tenders.db
SCAN_HOURS       = 9,12,15,18
TZ_OFFSET        = 5
ENABLED_SOURCES  = EPMS
SHOW_FIT         = 0
```

**On the passphrase.** It stops casual discovery of a public URL. It is not
access control. Anyone who has it, has everything. Keep `SHOW_FIT=0` and the
pursuit list disabled until this moves onto Jazz infrastructure with proper
authentication.

**On SQLite.** Fine for this volume and for a single instance. If you deploy
where the filesystem resets, mount a persistent volume and point `DB_PATH` at
it, or switch to Postgres. Only `db.py` would change.

---

## Files

| File | Purpose |
|---|---|
| `classify.py` | Digital taxonomy and Jazz relevance rules. Edit this often. |
| `db.py` | Schema, dedupe, change tracking, query layer |
| `sources.py` | One adapter per portal. Add a portal by adding a class. |
| `app.py` | Web service, API, scheduler, passphrase gate |
| `ui.html` | The dashboard |
| `run_scrape.py` | Command line scanning and adapter debugging |

---

## Adding a source

Add a class to `sources.py`:

```python
@register
class MySource(Source):
    name = "MyPortal"
    tier = "Provincial"
    jurisdiction = "Punjab"
    confidence = "drafted"
    home = "https://example.gov.pk"

    def fetch(self):
        sp = self.soup(self.home + "/tenders")
        for tr in sp.select("table tbody tr"):
            tds = tr.find_all("td")
            yield {
                "ref": clean(tds[0].get_text()),
                "title": clean(tds[1].get_text()),
                "buyer": clean(tds[2].get_text()),
                "advertised": parse_date(tds[3].get_text()),
                "closing": parse_date(tds[4].get_text()),
                "url": self.home + "/tenders",
            }
```

Classification, deduplication, storage and health tracking are handled for you.
Then add the name to `ENABLED_SOURCES`.

---

## Known limits

1. Only EPMS is verified. The rest need a debugging pass each.
2. EPADS renders client side. The adapter tries its JSON endpoints first and
   falls back to page one of HTML. A browser fallback would be needed for full
   coverage.
3. Tender history depth is whatever each portal serves. Depth compounds with
   every scan, since rows are never deleted.
4. Currency conversion uses a fixed USD rate in `sources.py`. Update `CUR` or
   wire a rate feed if precision matters.
5. Contact details are only as good as what each portal publishes. Many rows
   will show "Not published".

---

## Historical backfill

Live scans keep the feed current. They deliberately read only the newest
pages, so history needs a separate one-time sweep. All sources below are
official government or multilateral portals.

```bash
# EPADS: 74,000+ sequential public procurement IDs, the largest single source
python backfill.py --source EPADS --from-top

# resumable: interrupt with Ctrl+C, rerun to continue from the checkpoint
python backfill.py --source EPADS

# a timed run, for example overnight
python backfill.py --source EPADS --max-batches 400

# every other archive in one pass
python backfill.py --source all-archives --pages 200

# World Bank history specifically
python backfill.py --source WorldBank --pages 80

# progress
python backfill.py --status
```

Notes on the EPADS sweep. Expect roughly ten hours for the full range at the
default rate. Four workers with a 0.35 second delay each is deliberately
polite; raise `--workers` only if you have reason to. Progress is written
after every batch, so a restart, redeploy or dropped connection costs one
batch rather than the whole run. The sweep stops automatically after 600
consecutive empty IDs, which marks the bottom of the range.

Awards are stored in a separate table and drive the Intelligence tab. Run
`--source EPMS-Awards` early: renewal forecasting needs award history, and
depth compounds only from the day collection begins.
