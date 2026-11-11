# trend-monitor

A self-hosted RSS monitor that finds topics repeated across **independent
sources** in European tech and startup news, then publishes one daily digest as
a Markdown file, a committed Git history and a single pinned GitHub Issue.

The point is not "what happened today". It is "which story is more than one
outlet's press release" - the weakest useful form of trend evidence, computed
transparently and without an LLM.

## What it actually claims

- **A trend is a phrase in headlines from 2+ different feeds in the last 7
  days.** Nothing more. It is a reading aid, not an analyst.
- **Headlines only.** Summaries add boilerplate ("has secured", "the company")
  that produces fake repeats, so phrase detection ignores them.
- **Articles are de-duplicated by link** before scoring. Two feeds carrying the
  same story cannot fake a cross-source signal.
- **Phrases are filtered.** Common headline filler ("has", "secures",
  "funding", "startup", "million") is removed, and when a longer phrase covers
  the same sources as a shorter one, only the specific phrase is kept.
- **One syndicated press release can still look like a repeat.** Link
  de-duplication catches literal copies, not rewritten ones. Treat the output
  as a reading list, not proof.

## How it works

1. `.github/workflows/trend-monitor.yml` runs daily at 07:00 UTC (and on demand
   from the Actions tab).
2. `monitor.py` pulls every feed in `sources.yaml`, keeps the last 7 days,
   de-duplicates by link and removes tracking parameters.
3. For each headline it builds content-word bigrams and matches the watchlist.
   A phrase qualifies only if it appears in **2+ different sources**.
4. It writes `digests/YYYY-MM-DD.md` and `digests/latest.md`, appends to two
   CSVs, commits them, and creates or updates **one** GitHub Issue (it does not
   open a new issue every day).

## Output files

- **`data/articles.csv`** - every article ever seen:
  `collected_date, source, title, link, matched_watchlist_terms`.
  De-duplicated by normalized link, so re-runs never inflate it.
- **`data/trends.csv`** - every phrase flagged:
  `digest_date, phrase, source_count, sources`.
  Re-running the same day does not duplicate rows; the same phrase appearing
  on several days is kept, because that repetition is the signal.
- **`digests/latest.md`** - the current digest, always overwritten.

Open either CSV in Excel or with pandas to see which phrases keep recurring:

```python
import pandas as pd
trends = pd.read_csv("data/trends.csv")
print(trends.groupby("phrase").size().sort_values(ascending=False).head(20))
```

## Setup

```bash
git clone https://github.com/trpelka/trend-monitor.git
cd trend-monitor
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python monitor.py
```

On Windows the script reconfigures stdout to UTF-8 so headlines containing
`€` or `£` do not crash the run; the same bug used to kill the process after
the digest had already been written.

The scheduled workflow needs no secrets beyond the default `GITHUB_TOKEN`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
ruff check .
```

The tests cover link normalization, cross-feed de-duplication, filler removal,
single-source rejection, subphrase collapsing and CSV idempotency.

## Customizing what it watches

Everything lives in `sources.yaml`:

```yaml
watchlist:
  - Poland
  - CEE
  - tender

feeds:
  - name: "Tech.eu"
    url: "https://tech.eu/feed/"
```

A dead feed is reported in the digest as `HTTP <status>` or `no entries`, and
the run continues. Do not list the same publication under two feed names: the
articles are de-duplicated, so the second entry adds nothing.

Candidates tested and rejected on 11 Nov 2026: AIN Capital (HTTP 403 to
automated clients), Vestbee (HTTP 301 loop). TED has a structured API and
belongs in a procurement tool, not an RSS monitor.

## Why phrase counting instead of an LLM

It is free, instant and fully explainable: every flagged phrase can be traced
to the headlines that produced it. An LLM summarizer would produce better
prose and worse auditability. If you need summaries later, keep this
fetch-and-store layer and add the model on top; do not replace the evidence.

## Limitations

- English-language headlines only. The tokenizer keeps Latin-1 letters, but the
  stopword and filler lists are English.
- No entity resolution: "sennder" and "Sennder GmbH" are different tokens.
- No sentiment, no deduplication of rewritten syndicated copies, no ranking
  beyond source count and article count.
- A single-source window is reported explicitly instead of being presented as
  a trend.

## License

MIT - see [LICENSE](LICENSE).
