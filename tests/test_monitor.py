import csv
import time
from datetime import datetime, timedelta, timezone

import monitor


def article(title, source="Alpha", link=None, summary=""):
    return {
        "source": source,
        "title": title,
        "summary": summary,
        "link": link or f"https://example.com/{abs(hash((title, source)))}",
    }


def test_normalize_link_strips_tracking_and_fragment():
    url = "https://Example.com/story/?utm_source=x&id=7#comments"
    assert monitor.normalize_link(url) == "https://example.com/story?id=7"


def test_normalize_link_treats_trailing_slash_as_same_page():
    assert monitor.normalize_link("https://example.com/a/") == monitor.normalize_link(
        "https://example.com/a"
    )


def test_dedupe_articles_drops_same_story_from_second_feed():
    shared = "https://example.com/one-story"
    articles = [
        article("Shared story", source="Sifted", link=shared),
        article("Shared story", source="Sifted CEE", link=shared + "?utm_source=rss"),
    ]
    assert len(monitor.dedupe_articles(articles)) == 1


def test_dedupe_articles_keeps_distinct_links():
    articles = [
        article("One", link="https://example.com/1"),
        article("Two", link="https://example.com/2"),
    ]
    assert len(monitor.dedupe_articles(articles)) == 2


def test_clean_words_removes_filler():
    assert monitor.clean_words("Startup has secured pre-seed funding") == ["pre-seed"]


def test_clean_words_keeps_names_and_places():
    words = monitor.clean_words("Czech medtech Kardi Ai raises EUR 5.3M")
    assert "czech" in words
    assert "medtech" in words
    assert "kardi" in words
    assert "raises" not in words


def test_find_cross_source_repeats_requires_two_sources():
    articles = [
        article("Czech medtech expands", source="Alpha"),
        article("Czech medtech raises", source="Beta"),
    ]
    phrases = [phrase for phrase, _ in monitor.find_cross_source_repeats(articles, [])]
    assert "czech medtech" in phrases


def test_find_cross_source_repeats_ignores_single_source():
    articles = [
        article("Czech medtech expands", source="Alpha"),
        article("Czech medtech raises", source="Alpha"),
    ]
    assert monitor.find_cross_source_repeats(articles, []) == []


def test_find_cross_source_repeats_drops_generic_filler_phrases():
    articles = [
        article("Company has secured funding", source="Alpha"),
        article("Startup has secured investment", source="Beta"),
    ]
    assert monitor.find_cross_source_repeats(articles, []) == []


def test_find_cross_source_repeats_keeps_watchlist_terms():
    articles = [
        article("New factory in Poland", source="Alpha"),
        article("Poland opens a market", source="Beta"),
    ]
    phrases = [phrase for phrase, _ in monitor.find_cross_source_repeats(articles, ["Poland"])]
    assert "poland" in phrases


def test_drop_subphrases_keeps_the_specific_phrase():
    trends = [
        ("czech", {"Alpha", "Beta"}),
        ("czech medtech", {"Alpha", "Beta"}),
        ("unrelated phrase", {"Alpha", "Gamma"}),
    ]
    kept = monitor.drop_subphrases(trends)
    assert ("czech", {"Alpha", "Beta"}) not in kept
    assert ("czech medtech", {"Alpha", "Beta"}) in kept
    assert ("unrelated phrase", {"Alpha", "Gamma"}) in kept


def test_write_trends_csv_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(monitor, "TRENDS_CSV", str(tmp_path / "trends.csv"))
    trends = [("czech medtech", {"Alpha", "Beta"})]

    assert monitor.write_trends_csv(trends, "2026-11-11") == 1
    assert monitor.write_trends_csv(trends, "2026-11-11") == 0

    with open(tmp_path / "trends.csv", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["source_count"] == "2"


def test_write_articles_csv_does_not_duplicate_links(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(monitor, "ARTICLES_CSV", str(tmp_path / "articles.csv"))
    articles = [article("One", link="https://example.com/1?a=1&utm_source=x")]

    assert monitor.write_articles_csv(articles, [], "2026-11-11") == 1
    assert monitor.write_articles_csv(articles, [], "2026-11-11") == 0


def test_build_report_flags_a_single_dominant_source():
    articles = [
        article("One", source="Sifted"),
        article("Two", source="Sifted"),
        article("Three", source="Sifted"),
        article("Four", source="Tech.eu"),
    ]
    report, _ = monitor.build_report(articles, [], {}, [], 7, 4)
    assert "A single-source window cannot produce a real cross-source trend." in report


def test_build_report_explains_when_too_few_sources_loaded():
    articles = [article("Only one", source="Sifted")]
    report, _ = monitor.build_report(articles, [], {}, [], 7, 5)
    assert "Not enough independent sources loaded" in report


def test_within_window_drops_old_entries_and_keeps_undated_ones():
    old = time.gmtime((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
    fresh = time.gmtime(datetime.now(timezone.utc).timestamp())
    assert monitor.within_window({"published_parsed": old}, days=7) is False
    assert monitor.within_window({"published_parsed": fresh}, days=7) is True
    assert monitor.within_window({}, days=7) is True
