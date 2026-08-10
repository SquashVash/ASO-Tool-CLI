"""Ad-hoc lookup: score a keyword without tracking it.

The promise being tested is narrow and load-bearing: scoring a keyword this way
must leave the keyword list exactly as it found it. A lookup that quietly
tracked things would put scores in the list that no `aso refresh` produced.
"""

from __future__ import annotations


import pytest

from aso import lookup as lookup_module
from aso import store as store_module

from .conftest import days_ago


def scored_keyword(store: store_module.Store, keyword: str, opportunity: float) -> int:
    """Track `keyword` and give it scores, as one refresh would."""
    keyword_id, _ = store.add_keyword(keyword, "us")
    store.write_scores(
        keyword_id,
        captured_at=days_ago(0),
        search_score=50.0,
        competition_score=50.0,
        opportunity_score=opportunity,
    )
    store.save()
    return keyword_id


# --- percentile context ----------------------------------------------------


def test_percentile_is_none_when_nothing_is_tracked(store: store_module.Store) -> None:
    """A percentile against an empty set is not 0, it is unanswerable."""
    percentile, compared = lookup_module.opportunity_percentile(store, 40.0)
    assert percentile is None
    assert compared == 0


def test_percentile_is_none_for_an_unscored_keyword(store: store_module.Store) -> None:
    scored_keyword(store, "a", 10.0)
    assert lookup_module.opportunity_percentile(store, None) == (None, 0)


def test_percentile_counts_how_many_it_beats(store: store_module.Store) -> None:
    for index, value in enumerate([10.0, 20.0, 30.0, 40.0]):
        scored_keyword(store, f"kw{index}", value)

    percentile, compared = lookup_module.opportunity_percentile(store, 35.0)
    assert compared == 4
    assert percentile == pytest.approx(75.0)


def test_a_score_below_everything_is_zero_not_none(store: store_module.Store) -> None:
    """Zero is a real answer here; None means "no basis for an answer"."""
    scored_keyword(store, "a", 50.0)
    percentile, compared = lookup_module.opportunity_percentile(store, 1.0)
    assert percentile == pytest.approx(0.0)
    assert compared == 1


def test_unscored_keywords_are_excluded_from_the_comparison(
    store: store_module.Store,
) -> None:
    """A keyword that was never refreshed is not a keyword you beat."""
    scored_keyword(store, "scored", 10.0)
    store.add_keyword("never-refreshed", "us")

    percentile, compared = lookup_module.opportunity_percentile(store, 20.0)
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


def test_lookup_writes_nothing(monkeypatch, tmp_path) -> None:
    """The whole point of an ad-hoc check: score it, store nothing.

    `score_keyword` is stubbed so this exercises `lookup`'s own persistence
    behaviour rather than the scoring pipeline's, which has its own tests.
    """
    from aso import pipeline

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

    result = lookup_module.lookup("untracked term", "us")

    assert result.tracked is False
    assert result.outcome.opportunity_score == pytest.approx(36.0)

    with store_module.session() as store:
        assert store.get_keyword("untracked term", "us") is None
        assert store.records == []


def test_lookup_reports_an_already_tracked_keyword(monkeypatch) -> None:
    """So the UI can offer "track this" only when it would do something."""
    from aso import pipeline

    async def fake_score(keyword, country, **kwargs):
        outcome = pipeline.KeywordOutcome(
            keyword_id=0, keyword=keyword, country=country
        )
        outcome.opportunity_score = 12.0
        return pipeline.ScoredKeyword(
            outcome=outcome, comp_result=None, observation=None, serp=None
        )

    monkeypatch.setattr(lookup_module.pipeline, "score_keyword", fake_score)

    with store_module.session() as store:
        store.add_keyword("forex", "us")

    assert lookup_module.lookup("forex", "us").tracked is True


def test_lookup_normalizes_country_case(monkeypatch) -> None:
    from aso import pipeline

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
    lookup_module.lookup("forex", "US")
    assert seen["country"] == "us"


async def test_lookup_async_uses_a_caller_supplied_fetcher(monkeypatch) -> None:
    """The API owns one Fetcher for its process. A lookup that built its own
    would draw from a second token bucket against the same per-IP limit."""
    from aso import pipeline

    from .test_http import fast_fetcher

    seen: dict[str, object] = {}

    async def fake_score(keyword, country, *, itunes, hints, **kwargs):
        seen["itunes_fetcher"] = itunes.fetcher
        seen["hints_fetcher"] = hints.fetcher
        outcome = pipeline.KeywordOutcome(
            keyword_id=0, keyword=keyword, country=country
        )
        return pipeline.ScoredKeyword(
            outcome=outcome, comp_result=None, observation=None, serp=None
        )

    monkeypatch.setattr(lookup_module.pipeline, "score_keyword", fake_score)
    async with fast_fetcher() as fetcher:
        await lookup_module.lookup_async("forex", "us", fetcher=fetcher)

    assert seen["itunes_fetcher"] is fetcher
    assert seen["hints_fetcher"] is fetcher


async def test_lookup_async_passes_the_chart_index_through(monkeypatch) -> None:
    """Without it `comp_app_power` is None and `combine()` renormalizes over
    the rest — and that component carries 0.625 of the fitted weight."""
    from aso import pipeline
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
    index = ChartIndex(country="us", ranks={111: 3})
    async with fast_fetcher() as fetcher:
        await lookup_module.lookup_async("forex", "us", fetcher=fetcher, charts=index)

    assert seen["charts"] is index
