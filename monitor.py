"""
trend-monitor: pulls RSS feeds, finds topics that repeat across multiple
sources (the actual "trend" signal), and flags anything matching a
watchlist of keywords you care about.

Design choices, briefly:
- Feed failures never crash the run - a dead feed is logged and skipped,
  so one broken URL does not kill the daily digest.
- "Trend" = a phrase (bigram) or watchlist term that shows up in the
  headlines of 2+ DIFFERENT sources within the lookback window. A phrase
  repeating inside one outlet is that outlet's beat, not a trend.
- Headlines only, no summaries, for phrase detection. Summaries contain
  boilerplate ("has secured", "the company") that produces meaningless
  repeats.
- Articles are de-duplicated by link before scoring. Two feeds that carry
  the same story (for example a main and a regional Sifted feed) must not
  make one story look like a cross-source signal.
- No ML/LLM call here on purpose: cheap, fast and fully explainable. You
  can see exactly why a phrase was flagged.
"""

import csv
import os
import re
import sys
from calendar import timegm
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import yaml

USER_AGENT = "trend-monitor/1.0 (+https://github.com/trpelka/trend-monitor)"
MIN_SOURCES = 2

STOPWORDS = set(
    """a an the and or but if then else for of in on to from with by at as is are was were
    be been being this that these those it its it's their his her our your my we you they
    i he she not no yes will would can could should may might just about into over under
    after before during between more most less least than so such new latest europe
    european company companies startup startups million billion round funding raises
    raised said says according per year years week month report reports data""".split()
)

# Headline filler. A phrase made of these words carries no signal, even if
# it technically appears in two sources.
FILLER = set(
    """has have had get gets got make makes made take takes taken see sees seen
    look looks looking back backs backing close closes closed secure secures secured
    announce announces announced launch launches launched unveil unveils unveiled
    raise raising fund funds funded invest invests invested investment investor
    deal deals join joins joined name names named appoint appoints appointed
    could would might must also still even just now today week month year
    first second third fourth fifth one two three four five six seven eight nine ten
    best top new next last more most less few many much very well big small large
    global international world europe european tech technology business market""".split()
)

SOURCES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sources.yaml")
DIGESTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "digests")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
ARTICLES_CSV = os.path.join(DATA_DIR, "articles.csv")
TRENDS_CSV = os.path.join(DATA_DIR, "trends.csv")

ARTICLES_FIELDS = ["collected_date", "source", "title", "link", "matched_watchlist_terms"]
TRENDS_FIELDS = ["digest_date", "phrase", "source_count", "sources"]


def load_sources(path=SOURCES_FILE):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def clean_words(text):
    """Lowercase content words from a headline, without punctuation or filler."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    words = re.findall(r"[A-Za-z\u00c0-\u00ff][A-Za-z\u00c0-\u00ff'-]*", text)
    cleaned = []
    for word in words:
        word = word.strip("'-").lower()
        if len(word) < 3 or word in STOPWORDS or word in FILLER:
            continue
        cleaned.append(word)
    return cleaned


def bigrams(words):
    return [f"{a} {b}" for a, b in zip(words, words[1:], strict=False)]


def within_window(entry, days):
    """Include the entry if it is inside the lookback window, or if it has no
    date at all (better to show an undated item once than to drop it forever)."""
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            entry_time = datetime.fromtimestamp(timegm(value), tz=timezone.utc)
            return entry_time > datetime.now(timezone.utc) - timedelta(days=days)
    return True


def normalize_link(link):
    """Stable article identity: no tracking parameters, fragment or trailing slash."""
    link = (link or "").strip()
    if not link:
        return ""
    parts = urlsplit(link)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith(("utm_", "gclid", "fbclid", "mc_", "ref"))
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def dedupe_articles(articles):
    """Keep one row per article link.

    The same story can appear in several feeds (main + regional, or a
    syndicated copy). Counting it twice would fake a cross-source trend,
    so the first occurrence wins and later copies are dropped.
    """
    seen = set()
    unique = []
    for article in articles:
        key = normalize_link(article.get("link")) or article.get("title", "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(article)
    return unique


def fetch_articles(feeds, days, failed):
    articles = []
    for source in feeds:
        try:
            parsed = feedparser.parse(
                source["url"],
                agent=USER_AGENT,
                request_headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
                },
            )
            if not parsed.entries:
                status = getattr(parsed, "status", None)
                reason = f"HTTP {status}" if status else "no entries"
                failed.append(f"{source['name']} ({reason})")
                continue
            for entry in parsed.entries:
                if not within_window(entry, days):
                    continue
                articles.append(
                    {
                        "source": source["name"],
                        "title": entry.get("title", "(no title)"),
                        "summary": entry.get("summary", "") or entry.get("description", ""),
                        "link": entry.get("link", ""),
                    }
                )
        except Exception as exc:  # noqa: BLE001 - one bad feed must never kill the run
            failed.append(f"{source['name']} ({exc})")
    return dedupe_articles(articles)


def drop_subphrases(trends):
    """Remove a phrase when a longer phrase covers the same sources.

    "czech" and "czech medtech" carry the same evidence; keep the specific one.
    """
    kept = []
    for phrase, sources in trends:
        tokens = set(phrase.split())
        covered = False
        for other, other_sources in trends:
            if other == phrase or sources != other_sources:
                continue
            other_tokens = set(other.split())
            if tokens < other_tokens:
                covered = True
                break
        if not covered:
            kept.append((phrase, sources))
    return kept


def find_cross_source_repeats(articles, watchlist, min_sources=MIN_SOURCES):
    """Return (phrase, sources) pairs found in headlines from 2+ sources.

    Watchlist terms are always candidates; other phrases must be bigrams made
    of content words. Phrases are ranked by source count, then by how many
    separate articles mention them.
    """
    phrase_sources = defaultdict(set)
    phrase_articles = defaultdict(set)

    for index, article in enumerate(articles):
        title = article.get("title", "")
        phrases = set(bigrams(clean_words(title)))
        title_lower = title.lower()
        for term in watchlist:
            if term.lower() in title_lower:
                phrases.add(term.lower())
        for phrase in phrases:
            phrase_sources[phrase].add(article["source"])
            phrase_articles[phrase].add(index)

    candidates = [
        (phrase, sources)
        for phrase, sources in phrase_sources.items()
        if len(sources) >= min_sources
    ]
    candidates = drop_subphrases(candidates)
    candidates.sort(key=lambda item: (-len(item[1]), -len(phrase_articles[item[0]]), item[0]))
    return candidates


def find_watchlist_hits(articles, watchlist):
    hits = defaultdict(list)
    for article in articles:
        haystack = f"{article.get('title', '')} {article.get('summary', '')}".lower()
        for term in watchlist:
            if term.lower() in haystack:
                hits[term].append(article)
    return hits


def matched_terms(article, watchlist):
    haystack = f"{article.get('title', '')} {article.get('summary', '')}".lower()
    return [term for term in watchlist if term.lower() in haystack]


def build_report(articles, trends, watch_hits, failed, days, total_feeds):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    working_feeds = {article["source"] for article in articles}
    lines = [f"# Trend digest - {today}", ""]
    lines.append(
        f"Window: last {days} days - {len(articles)} articles from "
        f"{len(working_feeds)}/{total_feeds} feeds"
    )
    if failed:
        lines.append(f"\nFeeds that failed to load: {', '.join(failed)}")
    if articles:
        counts = defaultdict(int)
        for article in articles:
            counts[article["source"]] += 1
        top_source, top_count = max(counts.items(), key=lambda item: item[1])
        if top_count / len(articles) > 0.6 and len(counts) > 1:
            lines.append(
                f"\nNote: {top_count}/{len(articles)} articles come from {top_source}. "
                "A single-source window cannot produce a real cross-source trend."
            )
    lines.append("")

    lines.append("## Cross-source repeats (2+ different sources)")
    if trends:
        for phrase, sources in trends[:15]:
            lines.append(f"- **{phrase}** - {', '.join(sorted(sources))}")
    elif len(working_feeds) < MIN_SOURCES:
        lines.append("_Not enough independent sources loaded - no trend signal this window._")
    else:
        lines.append("_None this window._")
    lines.append("")

    lines.append("## Watchlist keyword hits")
    if watch_hits:
        for term, hits in watch_hits.items():
            lines.append(f"\n### {term} ({len(hits)})")
            for article in hits[:5]:
                lines.append(f"- [{article['title']}]({article['link']}) - {article['source']}")
    else:
        lines.append("_None this window._")
    lines.append("")

    lines.append("## All articles this window")
    by_source = defaultdict(list)
    for article in articles:
        by_source[article["source"]].append(article)
    for source in sorted(by_source):
        source_articles = by_source[source]
        lines.append(f"\n### {source} ({len(source_articles)})")
        for article in source_articles:
            lines.append(f"- [{article['title']}]({article['link']})")

    return "\n".join(lines), today


def _read_csv(path, fieldnames):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, fieldnames=fieldnames))


def load_existing_links(path=None):
    """So re-runs never duplicate article rows."""
    path = path or ARTICLES_CSV
    return {row["link"] for row in _read_csv(path, ARTICLES_FIELDS) if row.get("link")}


def load_trend_keys(path=None):
    """(date, phrase) pairs already written, so re-runs stay idempotent."""
    path = path or TRENDS_CSV
    return {
        (row["digest_date"], row["phrase"])
        for row in _read_csv(path, TRENDS_FIELDS)
        if row.get("digest_date") and row.get("phrase")
    }


def write_articles_csv(articles, watchlist, today):
    os.makedirs(DATA_DIR, exist_ok=True)
    existing_links = {normalize_link(link) for link in load_existing_links()}
    is_new_file = not os.path.exists(ARTICLES_CSV)

    new_rows = [
        {
            "collected_date": today,
            "source": article["source"],
            "title": article["title"],
            "link": article["link"],
            "matched_watchlist_terms": "; ".join(matched_terms(article, watchlist)),
        }
        for article in articles
        if normalize_link(article["link"]) not in existing_links
    ]

    with open(ARTICLES_CSV, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ARTICLES_FIELDS)
        if is_new_file:
            writer.writeheader()
        writer.writerows(new_rows)

    return len(new_rows)


def write_trends_csv(trends, today):
    os.makedirs(DATA_DIR, exist_ok=True)
    is_new_file = not os.path.exists(TRENDS_CSV)
    existing_keys = load_trend_keys()

    rows = [
        {
            "digest_date": today,
            "phrase": phrase,
            "source_count": len(sources),
            "sources": "; ".join(sorted(sources)),
        }
        for phrase, sources in trends
        if (today, phrase) not in existing_keys
    ]

    with open(TRENDS_CSV, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRENDS_FIELDS)
        if is_new_file:
            writer.writeheader()
        writer.writerows(rows)

    return len(rows)


def configure_stdout():
    """Windows consoles default to cp1250 and crash on headlines with GBP/EUR signs."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:  # pragma: no cover - older Python or redirected stream
        pass


def main():
    configure_stdout()
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

    new_articles = write_articles_csv(articles, watchlist, today)
    new_trends = write_trends_csv(trends, today)

    print(report)
    print(f"\n[csv] {new_articles} new article rows appended to {ARTICLES_CSV}")
    print(f"[csv] {new_trends} new trend rows appended to {TRENDS_CSV}")

    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as f:
            f.write(f"digest_path={out_path}\n")
            f.write(f"digest_date={today}\n")
            f.write(f"trend_count={len(trends)}\n")


if __name__ == "__main__":
    main()
