"""
trend-monitor: pulls RSS feeds, finds topics that repeat across multiple
sources (the actual "trend" signal), and flags anything matching a
watchlist of keywords you care about.

Design choices, briefly:
- Feed failures never crash the run — a dead feed just gets logged and
  skipped, so one broken URL doesn't kill the whole weekly digest.
- "Trend" = a phrase (bigram) or watchlist term that shows up in articles
  from 2+ DIFFERENT sources within the lookback window. A phrase repeating
  within a single source isn't a trend, it's just that outlet's beat.
- No ML/LLM call here on purpose — it's cheap, fast, and transparent. If
  you want smarter clustering later, swap the phrase-counting step for an
  API call to Claude/GPT and keep everything else the same.
"""

import csv
import os
import re
from calendar import timegm
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import feedparser
import yaml

STOPWORDS = set(
    """a an the and or but if then else for of in on to from with by at as is are was were
    be been being this that these those it its it's their his her our your my we you they
    i he she not no yes will would can could should may might just about into over under
    after before during between more most less least than so such new latest europe
    european company companies startup startups million billion round funding raises
    raised said says according per year years week month report reports data""".split()
)

SOURCES_FILE = os.path.join(os.path.dirname(__file__), "sources.yaml")
DIGESTS_DIR = os.path.join(os.path.dirname(__file__), "digests")
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
ARTICLES_CSV = os.path.join(DATA_DIR, "articles.csv")
TRENDS_CSV = os.path.join(DATA_DIR, "trends.csv")


def load_sources(path=SOURCES_FILE):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def clean_words(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    words = re.findall(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'-]{2,}", text)
    return [w.lower() for w in words if w.lower() not in STOPWORDS]


def bigrams(words):
    return [f"{a} {b}" for a, b in zip(words, words[1:])]


def within_window(entry, days):
    """Include the entry if it's within the lookback window, OR if it has
    no date at all (better to show an undated item once than silently
    drop content forever)."""
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            entry_time = datetime.fromtimestamp(timegm(t), tz=timezone.utc)
            return entry_time > datetime.now(timezone.utc) - timedelta(days=days)
    return True


def fetch_articles(feeds, days, failed):
    articles = []
    for src in feeds:
        try:
            parsed = feedparser.parse(src["url"])
            if parsed.bozo and not parsed.entries:
                failed.append(f"{src['name']} (couldn't parse feed)")
                continue
            for entry in parsed.entries:
                if not within_window(entry, days):
                    continue
                articles.append(
                    {
                        "source": src["name"],
                        "title": entry.get("title", "(no title)"),
                        "summary": entry.get("summary", "") or entry.get("description", ""),
                        "link": entry.get("link", ""),
                    }
                )
        except Exception as e:  # noqa: BLE001 - one bad feed must never kill the run
            failed.append(f"{src['name']} ({e})")
    return articles


def find_cross_source_repeats(articles, watchlist):
    """Returns phrases (bigrams + watchlist hits) that appear across 2+
    distinct sources — the actual repetition signal worth reading."""
    phrase_sources = defaultdict(set)
    for art in articles:
        text = f"{art['title']} {art['summary']}"
        words = clean_words(text)
        phrases = set(bigrams(words))
        text_lower = text.lower()
        for term in watchlist:
            if term.lower() in text_lower:
                phrases.add(term.lower())
        for phrase in phrases:
            phrase_sources[phrase].add(art["source"])

    trends = [(p, s) for p, s in phrase_sources.items() if len(s) >= 2]
    trends.sort(key=lambda x: -len(x[1]))
    return trends


def find_watchlist_hits(articles, watchlist):
    hits = defaultdict(list)
    for art in articles:
        text_lower = f"{art['title']} {art['summary']}".lower()
        for term in watchlist:
            if term.lower() in text_lower:
                hits[term].append(art)
    return hits


def build_report(articles, trends, watch_hits, failed, days, total_feeds):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"# Trend digest — {today}", ""]
    ok_feeds = total_feeds - len(failed)
    lines.append(
        f"Window: last {days} days · {len(articles)} articles from {ok_feeds}/{total_feeds} sources"
    )
    if failed:
        lines.append(f"\n⚠️ Feeds that failed to load: {', '.join(failed)}")
    lines.append("")

    lines.append("## Cross-source repeats (2+ different sources — likely a real signal)")
    if trends:
        for phrase, srcs in trends[:15]:
            lines.append(f"- **{phrase}** — {', '.join(sorted(srcs))}")
    else:
        lines.append("_None this window._")
    lines.append("")

    lines.append("## Watchlist keyword hits")
    if watch_hits:
        for term, arts in watch_hits.items():
            lines.append(f"\n### {term} ({len(arts)})")
            for a in arts[:5]:
                lines.append(f"- [{a['title']}]({a['link']}) — {a['source']}")
    else:
        lines.append("_None this window._")
    lines.append("")

    lines.append("## All articles this window")
    by_source = defaultdict(list)
    for a in articles:
        by_source[a["source"]].append(a)
    for src, arts in by_source.items():
        lines.append(f"\n### {src} ({len(arts)})")
        for a in arts:
            lines.append(f"- [{a['title']}]({a['link']})")

    return "\n".join(lines), today


def matched_terms(article, watchlist):
    text_lower = f"{article['title']} {article['summary']}".lower()
    return [t for t in watchlist if t.lower() in text_lower]


def load_existing_links(path):
    """So re-runs don't duplicate rows for articles already logged."""
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8", newline="") as f:
        return {row["link"] for row in csv.DictReader(f)}


def write_articles_csv(articles, watchlist, today):
    os.makedirs(DATA_DIR, exist_ok=True)
    existing_links = load_existing_links(ARTICLES_CSV)
    is_new_file = not os.path.exists(ARTICLES_CSV)

    new_rows = [
        {
            "collected_date": today,
            "source": a["source"],
            "title": a["title"],
            "link": a["link"],
            "matched_watchlist_terms": "; ".join(matched_terms(a, watchlist)),
        }
        for a in articles
        if a["link"] not in existing_links
    ]

    with open(ARTICLES_CSV, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["collected_date", "source", "title", "link", "matched_watchlist_terms"]
        )
        if is_new_file:
            writer.writeheader()
        writer.writerows(new_rows)

    return len(new_rows)


def write_trends_csv(trends, today):
    os.makedirs(DATA_DIR, exist_ok=True)
    is_new_file = not os.path.exists(TRENDS_CSV)

    with open(TRENDS_CSV, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["digest_date", "phrase", "source_count", "sources"])
        if is_new_file:
            writer.writeheader()
        for phrase, srcs in trends:
            writer.writerow(
                {
                    "digest_date": today,
                    "phrase": phrase,
                    "source_count": len(srcs),
                    "sources": "; ".join(sorted(srcs)),
                }
            )


def main():
    days = int(os.environ.get("LOOKBACK_DAYS", "7"))
    config = load_sources()
    watchlist = config.get("watchlist", [])
    feeds = config.get("feeds", [])

    failed = []
    articles = fetch_articles(feeds, days, failed)
    trends = find_cross_source_repeats(articles, watchlist)
    watch_hits = find_watchlist_hits(articles, watchlist)

    report, today = build_report(articles, trends, watch_hits, failed, days, len(feeds))

    os.makedirs(DIGESTS_DIR, exist_ok=True)
    out_path = os.path.join(DIGESTS_DIR, f"{today}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    with open(os.path.join(DIGESTS_DIR, "latest.md"), "w", encoding="utf-8") as f:
        f.write(report)

    new_article_count = write_articles_csv(articles, watchlist, today)
    write_trends_csv(trends, today)

    print(report)
    print(f"\n[csv] {new_article_count} new article rows appended to {ARTICLES_CSV}")
    print(f"[csv] {len(trends)} trend rows appended to {TRENDS_CSV}")

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"digest_path={out_path}\n")
            f.write(f"digest_date={today}\n")
            f.write(f"trend_count={len(trends)}\n")


if __name__ == "__main__":
    main()
