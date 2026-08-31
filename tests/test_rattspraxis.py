"""Smoke tests for the case-law monitor.

The offline tests guard what fails quietly: mapping the API's two differently
shaped record types onto one row, keeping the FTS index in step with the table,
and reporting each watch hit exactly once.

The live tests need no credentials — Domstolsverket is open — so they are
marked rather than skipped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sweden_legal_mcp.rattspraxis import watch as watchlib
from sweden_legal_mcp.rattspraxis.client import PAGE_SIZE, RattspraxisClient
from sweden_legal_mcp.rattspraxis.models import Decision, Significance, extract_lagrum
from sweden_legal_mcp.rattspraxis.store import Store

# A real MÖD record: subject-tagged, PDF attached, no lagrumLista — but the
# statutory reference is sitting in the summary text.
MOD_RECORD = {
    "id": "92c73bc8-ce47-4d80-941c-ead75f202f95",
    "avgorandedatum": "2026-08-26",
    "publiceringstid": "2026-08-26T08:32:00",
    "domstol": {"domstolKod": "MMOD", "domstolNamn": "Mark- och miljööverdomstolen"},
    "malNummerLista": ["P 17088-25"],
    "rattsomradeLista": ["Mål enligt plan- och bygglagen"],
    "lagrumLista": [],
    "nyckelordLista": [],
    "bilagaLista": [{"fillagringId": "100/be/35/x", "filnamn": "P 17088-25 Dom.pdf"}],
    "sammanfattning": (
        "MÖD har fastställt nämndens beslut att ge negativt förhandsbesked för nybyggnad "
        "av enbostadshus och garage med hänvisning till detaljplanekravet i 4 kap. 2 § PBL."
    ),
    "typ": "EJ_VAGLEDANDE",
    "publiceringsform": "DOM_ELLER_BESLUT",
    "referatNummerLista": [],
}

# A curated archive referat: the mirror image — lagrum and keywords, no subject tag.
REFERAT_RECORD = {
    "id": "aaaa1111-0000-0000-0000-000000000001",
    "avgorandedatum": "2019-05-14",
    "publiceringstid": "2019-06-01T09:00:00",
    "domstol": {"domstolKod": "HD", "domstolNamn": "Högsta domstolen"},
    "malNummerLista": ["T 1234-18"],
    "rattsomradeLista": [],
    "lagrumLista": [
        {"referens": "13 kap. 29 § sjölagen (1994:1009)", "sfsNummer": "1994:1009"}
    ],
    "nyckelordLista": ["Sjörätt", "Preskription"],
    "bilagaLista": [],
    "sammanfattning": "Fråga om preskription av fordran på ersättning.",
    "typ": "PREJUDIKAT",
    "publiceringsform": "REFERAT",
    "referatNummerLista": ["NJA 2019 s. 341"],
}


def test_lagrum_falls_back_to_the_summary():
    """MÖD reasons in prose. If this stops working, roughly a quarter of the
    statutory references in recent decisions vanish silently."""
    assert extract_lagrum(MOD_RECORD) == ["4 kap. 2 § PBL"]


def test_structured_lagrum_wins_over_the_summary():
    assert extract_lagrum(REFERAT_RECORD) == ["13 kap. 29 § sjölagen (1994:1009)"]


def test_no_lagrum_is_not_invented():
    record = dict(MOD_RECORD, sammanfattning="MÖD har bedömt att riktvärden för buller ska vara vägledande.")
    assert extract_lagrum(record) == []


def test_both_record_shapes_map_onto_one_row():
    mod = Decision.from_api(MOD_RECORD)
    ref = Decision.from_api(REFERAT_RECORD)

    assert mod.court_code == "MMOD"
    assert mod.subjects == ["Mål enligt plan- och bygglagen"]
    assert mod.attachments == 1
    assert mod.significance is Significance.EJ_VAGLEDANDE

    assert ref.significance is Significance.PREJUDIKAT
    assert ref.sfs == ["1994:1009"]
    assert ref.referat == ["NJA 2019 s. 341"]
    assert ref.subjects == []


def test_unknown_typ_does_not_crash():
    d = Decision.from_api(dict(MOD_RECORD, typ="SOMETHING_NEW"))
    assert d.significance is Significance.UNKNOWN


def _store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "db.sqlite3")
    s.upsert([Decision.from_api(MOD_RECORD), Decision.from_api(REFERAT_RECORD)])
    return s


def test_search_matches_swedish_text_and_prefixes(tmp_path: Path):
    s = _store(tmp_path)
    assert len(s.search(text="detaljplanekravet")) == 1
    assert len(s.search(text="förhandsbesked")) == 1
    assert len(s.search(text="preskription")) == 1
    assert len(s.search(text="detaljplan*")) == 1
    assert s.search(text="strandskydd") == []


def test_filters(tmp_path: Path):
    s = _store(tmp_path)
    assert len(s.search(court="MMOD")) == 1
    assert len(s.search(court="mmod")) == 1  # case-insensitive
    assert len(s.search(subject="plan- och bygglagen")) == 1
    assert len(s.search(lagrum="PBL")) == 1
    assert len(s.search(lagrum="1994:1009")) == 1
    assert len(s.search(decided_from="2020-01-01")) == 1


def test_bad_fts_syntax_explains_itself(tmp_path: Path):
    s = _store(tmp_path)
    with pytest.raises(ValueError) as exc:
        s.search(text='unbalanced "quote')
    assert "FTS5" in str(exc.value)


def test_reindex_stays_in_step_on_update(tmp_path: Path):
    """The FTS index is external-content, so a stale trigger would leave search
    returning rows whose text no longer matches. Silent and poisonous."""
    s = Store(tmp_path / "db.sqlite3")
    s.upsert([Decision.from_api(MOD_RECORD)])
    assert len(s.search(text="detaljplanekravet")) == 1

    revised = dict(MOD_RECORD, sammanfattning="MÖD har prövat en fråga om strandskyddsdispens.")
    new, updated = s.upsert([Decision.from_api(revised)])
    assert (new, updated) == (0, 1)
    assert s.search(text="detaljplanekravet") == []
    assert len(s.search(text="strandskyddsdispens")) == 1


def test_first_seen_survives_a_refresh(tmp_path: Path):
    """`first_seen` is what "new to this firm" means. A republication that
    merely touches a record must not make it look new again."""
    s = Store(tmp_path / "db.sqlite3")
    s.upsert([Decision.from_api(MOD_RECORD)])
    before = s.db.execute("SELECT first_seen FROM decisions").fetchone()["first_seen"]
    s.upsert([Decision.from_api(dict(MOD_RECORD, sammanfattning="Reviderad."))])
    assert s.db.execute("SELECT first_seen FROM decisions").fetchone()["first_seen"] == before


def test_a_watch_reports_each_decision_once(tmp_path: Path):
    s = _store(tmp_path)
    s.add_watch(name="Detaljplanekrav", text="detaljplanekravet", court=None,
                subject=None, lagrum=None, matter="Projekt Hedemora")

    first = watchlib.check_all(s)
    assert first["total_hits"] == 1
    assert first["matched"][0]["watch"]["matter"] == "Projekt Hedemora"

    assert watchlib.check_all(s)["total_hits"] == 0


def test_dry_run_does_not_mark(tmp_path: Path):
    s = _store(tmp_path)
    s.add_watch(name="w", text="preskription", court=None, subject=None, lagrum=None, matter=None)
    assert watchlib.check_all(s, mark=False)["total_hits"] == 1
    assert watchlib.check_all(s, mark=False)["total_hits"] == 1


def test_removing_a_watch_clears_its_history(tmp_path: Path):
    s = _store(tmp_path)
    s.add_watch(name="w", text="preskription", court=None, subject=None, lagrum=None, matter=None)
    watchlib.check_all(s)
    assert s.remove_watch("w") == 1
    s.add_watch(name="w", text="preskription", court=None, subject=None, lagrum=None, matter=None)
    assert watchlib.check_all(s)["total_hits"] == 1


def test_coverage_on_an_empty_mirror(tmp_path: Path):
    assert Store(tmp_path / "db.sqlite3").coverage() == {"total": 0}


def test_coverage_reports_field_gaps(tmp_path: Path):
    c = _store(tmp_path).coverage()
    assert c["total"] == 2
    assert c["with_subject"] == 1   # only the MÖD record is tagged
    assert c["with_lagrum"] == 2    # one structured, one recovered from prose


@pytest.mark.live
async def test_live_page_shape_and_size():
    async with RattspraxisClient() as client:
        total = await client.total_count()
        page = await client.page(0)
    assert total > 10_000, f"corpus unexpectedly small: {total}"
    assert len(page) == PAGE_SIZE
    assert {"id", "domstol", "sammanfattning"} <= set(page[0])


@pytest.mark.live
async def test_live_ordering_is_newest_first():
    """The delta sync depends on this. If ordering ever changes, a daily delta
    would stop early and quietly miss decisions."""
    async with RattspraxisClient() as client:
        first, second = await client.page(0), await client.page(1)
    newest = [r["avgorandedatum"] for r in first if r.get("avgorandedatum")]
    older = [r["avgorandedatum"] for r in second if r.get("avgorandedatum")]
    assert newest == sorted(newest, reverse=True)
    assert min(newest) >= max(older)


@pytest.mark.live
async def test_live_sync_then_search(tmp_path: Path):
    s = Store(tmp_path / "db.sqlite3")
    async with RattspraxisClient() as client:
        async for _, records in client.pages(max_pages=3):
            s.upsert([Decision.from_api(r) for r in records])
    c = s.coverage()
    assert c["total"] == 30
    assert s.search(court="MMOD") or s.search(text="MÖD"), "expected Mark- och miljööverdomstolen"
