"""Smoke tests for the Lifos watcher.

The offline tests guard the two things that fail silently: extracting a
document's identity from a feed title, and deciding that one version supersedes
another. Both are string handling over text a government publishes for humans,
and both produce a plausible-looking wrong answer when they break.

The live tests need no credentials — Lifos is open — so they are marked rather
than skipped, and CI can select them or not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sweden_legal_mcp.lifos import kb as kbscan
from sweden_legal_mcp.lifos.client import LifosClient, LifosError, parse_feed
from sweden_legal_mcp.lifos.ledger import Ledger
from sweden_legal_mcp.lifos.models import DocumentKind, Feed, FeedItem, is_newer

# A real title from the legal feed, 18 Aug 2026.
REAL_TITLE = (
    "Rättsligt ställningstagande. Uppehållstillstånd för besök "
    "- RS/001/2024 (version 3.0)"
)

SAMPLE_RSS = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Aktuellt från Lifos</title>
  <item>
    <pubDate>Tue, 18 Aug 2026 14:35:14 +0200</pubDate>
    <title>{REAL_TITLE}</title>
    <link>http://lifos.migrationsverket.se/rattsavd-nyhetsarkiv/a.html</link>
  </item>
  <item>
    <pubDate>Mon, 13 Jul 2026 09:00:00 +0200</pubDate>
    <title>Rättslig kommentar. EU-domstolens dom C-19/21 - RK/005/2026 (version 1.0)</title>
    <link>http://lifos.migrationsverket.se/rattsavd-nyhetsarkiv/b.html</link>
  </item>
</channel></rss>
"""


def test_title_yields_identity_and_version():
    """The whole connector rests on this line of text parsing correctly."""
    item = FeedItem.from_title(Feed.LEGAL, REAL_TITLE, "http://x", "2026-08-18")
    assert item.document_id == "RS/001/2024"
    assert item.version == "3.0"
    assert item.kind is DocumentKind.STANDPOINT


def test_commentary_is_distinguished_from_standpoint():
    item = FeedItem.from_title(
        Feed.LEGAL,
        "Rättslig kommentar. EU-domstolens dom C-19/21 - RK/005/2026 (version 1.0)",
        "http://x",
        "2026-07-13",
    )
    assert item.document_id == "RK/005/2026"
    assert item.kind is DocumentKind.COMMENT


def test_title_without_a_version_is_not_invented():
    item = FeedItem.from_title(Feed.LEGAL, "Utvecklingen i nordöstra Syrien", "http://x", None)
    assert item.document_id is None
    assert item.version is None
    assert item.key.startswith("untitled:")


def test_feed_parses_dates_to_iso():
    items = parse_feed(Feed.LEGAL, SAMPLE_RSS)
    assert [i.published for i in items] == ["2026-08-18", "2026-07-13"]
    assert [i.document_id for i in items] == ["RS/001/2024", "RK/005/2026"]


def test_malformed_feed_names_the_likely_cause():
    with pytest.raises(LifosError) as exc:
        parse_feed(Feed.LEGAL, "<html>not rss</html>")
    assert "portlet" in str(exc.value).lower()


@pytest.mark.parametrize(
    ("candidate", "baseline", "expected"),
    [
        ("3.0", "2.0", True),
        ("2.0", "3.0", False),
        ("2.0", "2.0", False),
        ("1.10", "1.9", True),  # not a float comparison
        ("2", "1.9", True),
        (None, "2.0", False),
        ("3.0", None, False),
        ("", "2.0", False),
        ("draft", "2.0", False),
    ],
)
def test_version_comparison(candidate, baseline, expected):
    """The regression that matters.

    An unknown or unparseable version must never read as newer. A false "your
    knowledge base is stale" costs a lawyer more than a missed one costs us: the
    missed one surfaces at the next review, the false one teaches the firm to
    ignore every future alert.
    """
    assert is_newer(candidate, baseline) is expected


def test_second_poll_reports_nothing(tmp_path: Path):
    items = parse_feed(Feed.LEGAL, SAMPLE_RSS)

    ledger = Ledger.load(tmp_path / "state.json")
    first = [e for i in items if (e := ledger.observe(i))]
    assert len(first) == len(items)
    assert all(e["event"] == "new_document" for e in first)
    ledger.save()

    reloaded = Ledger.load(tmp_path / "state.json")
    assert [e for i in items if (e := reloaded.observe(i))] == []


def test_republication_at_a_higher_version_is_an_event(tmp_path: Path):
    ledger = Ledger.load(tmp_path / "state.json")
    ledger.observe(
        FeedItem.from_title(
            Feed.LEGAL, "Rättsligt ställningstagande. X - RS/001/2024 (version 2.0)", "u", "2026-01-01"
        )
    )
    event = ledger.observe(FeedItem.from_title(Feed.LEGAL, REAL_TITLE, "u", "2026-08-18"))

    assert event is not None
    assert event["event"] == "new_version"
    assert event["previous_version"] == "2.0"
    assert ledger.known("RS/001/2024")["latest_version"] == "3.0"
    assert len(ledger.known("RS/001/2024")["history"]) == 2


def _write_kb(root: Path) -> None:
    (root / "mig_uppehallstillstand-besok.md").write_text(
        "# Byråns ståndpunkt — uppehållstillstånd för besök\n\n"
        "Grundas på Migrationsverkets rättsliga ställningstagande RS/001/2024 (version 2.0).\n",
        encoding="utf-8",
    )
    (root / "mig_sakerhetsavdelning.md").write_text(
        "Se RS/004/2026 — vi har inte skrivit ut någon version här.\n", encoding="utf-8"
    )


def test_scan_finds_citations_and_reports_unpinned(tmp_path: Path):
    _write_kb(tmp_path)
    rows = kbscan.scan(tmp_path)

    by_doc = {r["document_id"]: r for r in rows}
    assert by_doc["RS/001/2024"]["cited_version"] == "2.0"
    assert by_doc["RS/001/2024"]["line"] == 3
    assert by_doc["RS/004/2026"]["cited_version"] is None


def test_unpinned_citation_is_never_reported_stale(tmp_path: Path):
    """An entry that names no version has no baseline. Reporting it stale would
    be a guess dressed up as a finding."""
    kb_root = tmp_path / "kb"
    kb_root.mkdir()
    _write_kb(kb_root)

    ledger = Ledger.load(tmp_path / "state.json")
    for item in parse_feed(Feed.LEGAL, SAMPLE_RSS):
        ledger.observe(item)
    ledger.observe(
        FeedItem.from_title(
            Feed.LEGAL,
            "Rättsligt ställningstagande. Y - RS/004/2026 (version 1.0)",
            "u",
            "2026-07-21",
        )
    )
    for row in kbscan.scan(kb_root):
        ledger.track(row["document_id"], row["kb_file"], row["cited_version"])

    stale = ledger.stale()
    assert [s["document_id"] for s in stale] == ["RS/001/2024"]
    assert stale[0]["kb_cites_version"] == "2.0"
    assert stale[0]["current_version"] == "3.0"


def test_ledger_survives_a_round_trip(tmp_path: Path):
    ledger = Ledger.load(tmp_path / "state.json")
    for item in parse_feed(Feed.LEGAL, SAMPLE_RSS):
        ledger.observe(item)
    ledger.track("RS/001/2024", "kb/x.md", "2.0")
    ledger.save()

    on_disk = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert "RS/001/2024" in on_disk["documents"]
    assert on_disk["tracked"]["RS/001/2024"][0]["cited_version"] == "2.0"
    # Swedish characters stay readable — someone in the firm may open this file.
    assert "ställningstagande" in (tmp_path / "state.json").read_text(encoding="utf-8")


def test_untrack_removes_only_the_named_entry(tmp_path: Path):
    ledger = Ledger.load(tmp_path / "state.json")
    ledger.track("RS/001/2024", "kb/a.md", "2.0")
    ledger.track("RS/001/2024", "kb/b.md", "2.0")

    assert ledger.untrack("RS/001/2024", "kb/a.md") == 1
    assert [e["kb_file"] for e in ledger.data["tracked"]["RS/001/2024"]] == ["kb/b.md"]
    assert ledger.untrack("RS/001/2024") == 1
    assert "RS/001/2024" not in ledger.data["tracked"]


async def test_document_fetch_refuses_other_hosts():
    """A feed link is the only thing that should ever reach this tool."""
    async with LifosClient() as client:
        with pytest.raises(LifosError) as exc:
            await client.get_page("https://example.com/not-lifos")
    assert "lifos.migrationsverket.se" in str(exc.value)


@pytest.mark.live
async def test_live_legal_feed_carries_versioned_identifiers():
    """If this fails, the portlet URL has moved and the connector is blind."""
    async with LifosClient() as client:
        items = await client.read_feed(Feed.LEGAL)

    assert items, "the legal feed returned no items"
    identified = [i for i in items if i.document_id]
    assert identified, f"no RS/RK identifiers parsed from: {[i.title for i in items]}"
    assert any(i.version for i in identified), "no version numbers parsed — the signal is gone"


@pytest.mark.live
async def test_live_country_feed_is_a_different_feed():
    async with LifosClient() as client:
        legal = await client.read_feed(Feed.LEGAL)
        country = await client.read_feed(Feed.COUNTRY)

    assert {i.title for i in legal} != {i.title for i in country}
