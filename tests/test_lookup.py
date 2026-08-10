"""Ad-hoc lookup: the path behind the dashboard's Find screen.

The promise being tested is narrow and load-bearing: scoring a keyword this way
must leave the keyword list, the snapshot history and the SERP tables exactly
as it found them. A lookup that quietly tracked things would put rows in the
trend history that no `aso refresh` produced.
"""

from __future__ import annotations

import sqlite3

import pytest

from aso import lookup as lookup_module
from aso import repository as repo

from .test_repository import days_ago


def snapshot_with(conn: sqlite3.Connection, keyword: str, opportunity: float) -> int:
    repo.add_keyword(conn, keyword, "us")
    row = repo.require_keyword(conn, keyword, "us")
    repo.write_snapshot(
        conn,
        repo.SnapshotWrite(
            keyword_id=row["id"],
            captured_at=days_ago(0),
            search_score=50.0,
            competition_score=50.0,
            opportunity_score=opportunity,
        ),
    )
    return row["id"]


# --- percentile context ----------------------------------------------------


def test_percentile_is_none_when_nothing_is_tracked(conn: sqlite3.Connection) -> None:
    """A percentile against an empty set is not 0, it is unanswerable."""
    percentile, compared = lookup_module.opportunity_percentile(conn, 40.0)
    assert percentile is None
    assert compared == 0


def test_percentile_is_none_for_an_unscored_keyword(conn: sqlite3.Connection) -> None:
    snapshot_with(conn, "a", 10.0)
    assert lookup_module.opportunity_percentile(conn, None) == (None, 0)


def test_percentile_counts_how_many_it_beats(conn: sqlite3.Connection) -> None:
    for index, value in enumerate([10.0, 20.0, 30.0, 40.0]):
        snapshot_with(conn, f"kw{index}", value)

    percentile, compared = lookup_module.opportunity_percentile(conn, 35.0)
    assert compared == 4
    assert percentile == pytest.approx(75.0)


def test_a_score_below_everything_is_zero_not_none(conn: sqlite3.Connection) -> None:
    """Zero is a real answer here; None means "no basis for an answer"."""
    snapshot_with(conn, "a", 50.0)
    percentile, compared = lookup_module.opportunity_percentile(conn, 1.0)
    assert percentile == pytest.approx(0.0)
    assert compared == 1


def test_unscored_keywords_are_excluded_from_the_comparison(
    conn: sqlite3.Connection,
) -> None:
    """A keyword that was never refreshed is not a keyword you beat."""
    snapshot_with(conn, "scored", 10.0)
    repo.add_keyword(conn, "never-refreshed", "us")

    percentile, compared = lookup_module.opportunity_percentile(conn, 20.0)
    assert compared == 1
    assert percentile == pytest.approx(100.0)


# --- input validation, before any request is made --------------------------


@pytest.mark.parametrize("keyword", ["", "   ", "\n"])
def test_blank_keywords_are_rejected_without_fetching(keyword: str) -> None:
    with pytest.raises(ValueError, match="blank"):
        lookup_module.lookup(keyword, "us")


def test_blank_country_is_rejected_without_fetching() -> None:
    with pytest.raises(ValueError, match="blank"):
        lookup_module.lookup("forex", "  ")


# --- the write-nothing guarantee -------------------------------------------


def test_lookup_writes_no_keyword_snapshot_or_serp(monkeypatch, tmp_path) -> None:
    """The whole point of the Find screen: score it, store nothing.

    `score_keyword` is stubbed so this exercises `lookup`'s own persistence
    behaviour rather than the scoring pipeline's, which has its own tests.
    """
    from aso import db, pipeline

    async def fake_score(keyword, country, **kwargs):
        outcome = pipeline.KeywordOutcome(
            keyword_id=0, keyword=keyword, country=country
        )
        outcome.search_score = 60.0
        outcome.competition_score = 40.0
        outcome.opportunity_score = 36.0
        return pipeline.ScoredKeyword(
            outcome=outcome, comp_result=None, observation=None, serp=None
        )

    monkeypatch.setattr(lookup_module.pipeline, "score_keyword", fake_score)

    with db.session() as conn:
        db.migrate(conn)

    result = lookup_module.lookup("untracked term", "us")

    assert result.tracked is False
    assert result.outcome.opportunity_score == pytest.approx(36.0)

    with db.session() as conn:
        assert repo.get_keyword(conn, "untracked term", "us") is None
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM serps").fetchone()[0] == 0


def test_lookup_reports_an_already_tracked_keyword(monkeypatch) -> None:
    """So the UI can offer "track this" only when it would do something."""
    from aso import db, pipeline

    async def fake_score(keyword, country, **kwargs):
        outcome = pipeline.KeywordOutcome(
            keyword_id=0, keyword=keyword, country=country
        )
        outcome.opportunity_score = 12.0
        return pipeline.ScoredKeyword(
            outcome=outcome, comp_result=None, observation=None, serp=None
        )

    monkeypatch.setattr(lookup_module.pipeline, "score_keyword", fake_score)

    with db.session() as conn:
        db.migrate(conn)
        repo.add_keyword(conn, "forex", "us")

    assert lookup_module.lookup("forex", "us").tracked is True


def test_lookup_normalizes_country_case(monkeypatch) -> None:
    from aso import db, pipeline

    seen: dict[str, str] = {}

    async def fake_score(keyword, country, **kwargs):
        seen["country"] = country
        outcome = pipeline.KeywordOutcome(
            keyword_id=0, keyword=keyword, country=country
        )
        return pipeline.ScoredKeyword(
            outcome=outcome, comp_result=None, observation=None, serp=None
        )

    monkeypatch.setattr(lookup_module.pipeline, "score_keyword", fake_score)
    with db.session() as conn:
        db.migrate(conn)

    lookup_module.lookup("forex", "US")
    assert seen["country"] == "us"


async def test_lookup_async_uses_a_caller_supplied_fetcher(monkeypatch) -> None:
    """The API owns one Fetcher for its process. A lookup that built its own
    would draw from a second token bucket against the same per-IP limit."""
    from aso import db, pipeline

    from .test_http import fast_fetcher

    seen: dict[str, object] = {}

    async def fake_score(keyword, country, *, itunes, hints, **kwargs):
        seen["itunes_fetcher"] = itunes.fetcher
        seen["hints_fetcher"] = hints.fetcher
        seen["charts"] = kwargs.get("charts")
        outcome = pipeline.KeywordOutcome(
            keyword_id=0, keyword=keyword, country=country
        )
        return pipeline.ScoredKeyword(
            outcome=outcome, comp_result=None, observation=None, serp=None
        )

    monkeypatch.setattr(lookup_module.pipeline, "score_keyword", fake_score)
    with db.session() as conn:
        db.migrate(conn)

    async with fast_fetcher() as fetcher:
        await lookup_module.lookup_async("forex", "us", fetcher=fetcher)

    assert seen["itunes_fetcher"] is fetcher
    assert seen["hints_fetcher"] is fetcher


async def test_lookup_async_passes_the_chart_index_through(monkeypatch) -> None:
    """Without it `comp_app_power` is None and `combine()` renormalizes over
    the rest — and that component carries 0.625 of the fitted weight."""
    from aso import db, pipeline
    from aso.clients.charts import ChartIndex

    from .test_http import fast_fetcher

    seen: dict[str, object] = {}

    async def fake_score(keyword, country, *, itunes, hints, **kwargs):
        seen["charts"] = kwargs.get("charts")
        outcome = pipeline.KeywordOutcome(
            keyword_id=0, keyword=keyword, country=country
        )
        return pipeline.ScoredKeyword(
            outcome=outcome, comp_result=None, observation=None, serp=None
        )

    monkeypatch.setattr(lookup_module.pipeline, "score_keyword", fake_score)
    with db.session() as conn:
        db.migrate(conn)

    index = ChartIndex(country="us", ranks={111: 3})
    async with fast_fetcher() as fetcher:
        await lookup_module.lookup_async("forex", "us", fetcher=fetcher, charts=index)

    assert seen["charts"] is index
