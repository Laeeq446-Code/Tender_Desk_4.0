# Tender Desk — clean update

Ten files. Upload to the repo root, push. No leftover half-features.

## 1. Analytics (was Intelligence)
New self-contained page at /analytics, opened by the renamed Analytics tab.
Built from scratch, presentation-grade, reads a live data feed (/api/analytics),
so it updates as the feed and imports do.
- Opportunities over time: every tender or award plotted on its date against its
  value. A "Colour by" lens switches the same market between category, Jazz lane,
  buyer, bidder, and source. This is the clean way to read two dimensions without
  cramming them: change the lens, and click a legend entry to isolate a series.
- Volume by month split by lane, demand by category, most active buyers, lane mix.
- "Ask the data": natural-language questions over the dataset, answered in prose
  and, where useful, a chart.

## 2. LLM output
The Ask endpoint can now return a chart spec alongside its prose. Ask for a
comparison and it renders a bar chart, not just text. Your API key still comes
from the browser; nothing stored server-side.

## 3. Sources
Removed USF, the diluting source you named. The classifier and the stub guard
already drop non-ICT and navigation noise, so the feed stays clean.

## 4. Newspaper proof of concept
The Newspaper feed remains the working PoC: Probe reports which papers resolve
today, Fetch pulls and OCRs them, and the results land in a filterable feed.

## 5. Design
The Analytics page carries a clean, presentation-fit palette and typography,
built fresh rather than inherited, so it reads well on a screen in a room.

Files: app.py, analytics.html (new), sources.py, classify.py, ui.html, db.py,
enrich.py, newspaper.py, newspaper_fetch.py, forecast.py.
