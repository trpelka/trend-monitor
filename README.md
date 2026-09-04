# trend-monitor

A small, self-hosted tool that watches a set of RSS feeds weekly, flags topics
that repeat across **multiple different sources** (the actual "this is a real
trend, not just one outlet's pet story" signal), and opens a GitHub Issue
with the digest — no email/Slack setup required.

## How it works

1. `.github/workflows/trend-monitor.yml` runs every Monday (and on-demand via
   the Actions tab → "Run workflow").
2. `monitor.py` pulls every feed listed in `sources.yaml`, looks at articles
   from the last 7 days, and:
   - Finds two-word phrases that show up across **2+ different sources** —
     that cross-source repetition is the actual trend signal.
   - Flags anything matching your `watchlist` keywords (Poland, CEE, tender,
     procurement, etc.), regardless of repetition.
3. Writes a markdown digest to `digests/YYYY-MM-DD.md` (and `digests/latest.md`),
   commits it, and opens a GitHub Issue with the same content so it shows up
   in your GitHub notifications.
4. Also appends to two CSVs in `data/`, so you build a growing historical
   dataset instead of only ever seeing "this week":
   - **`data/articles.csv`** — every article ever seen, one row each:
     `collected_date, source, title, link, matched_watchlist_terms`.
     Deduplicated by link, so re-runs never create duplicate rows.
   - **`data/trends.csv`** — every cross-source repeat ever flagged, one row
     per phrase per week: `digest_date, phrase, source_count, sources`.
     Not deduplicated across weeks on purpose — if "CEE expansion" shows up
     as a trend four weeks running, that's exactly the pattern worth seeing
     when you sort this file by `phrase` in Excel/Sheets.

   Open either CSV directly in Excel, or load with pandas
   (`pd.read_csv("data/trends.csv")`) to spot which phrases keep recurring
   over time — a single week's digest can't show you that, but the CSV can.

## Setup

```bash
git clone https://github.com/YOUR_USERNAME/trend-monitor.git
cd trend-monitor
pip install -r requirements.txt
python monitor.py            # run it once locally to see the digest
```

Then push to GitHub — the scheduled workflow needs no secrets or extra setup
beyond the repo's default `GITHUB_TOKEN`, which Actions provides automatically.

## Customizing what it watches

Everything you'd want to change lives in `sources.yaml`, not the code:

```yaml
watchlist:
  - Poland
  - CEE
  - tender

feeds:
  - name: "Tech.eu"
    url: "https://tech.eu/feed/"
```

Add/remove feeds and keywords freely. If a feed URL is wrong or goes down,
the script logs it under "⚠️ Feeds that failed to load" in the digest and
keeps running — one dead feed never blocks the rest.

**Feeds worth adding once you find their RSS URLs** (not pre-verified in this
repo, since paywalled or JS-heavy sites sometimes don't expose clean RSS):
AIN Capital (CEE), Vestbee (CEE investment), AHK Polska news, TED (EU tenders
— has its own structured API, better suited to a dedicated script than RSS).

## Why phrase-repetition instead of an LLM call

This deliberately uses simple word/bigram counting, not an AI summarizer:
it's free, instant, and fully transparent — you can see exactly why
something got flagged. If you outgrow this, the natural upgrade is to pipe
`digests/latest.md` into a Claude API call for actual summarization/ranking,
while keeping the fetch-and-store mechanics exactly as they are.

## Adjusting the lookback window

Default is 7 days. Override by setting the `LOOKBACK_DAYS` env var, e.g. in
the workflow file:

```yaml
- name: Run trend monitor
  run: python monitor.py
  env:
    LOOKBACK_DAYS: '14'
```

## License

MIT
