from __future__ import annotations

import json

import httpx
import pytest
import respx

from aso import cache, config
from aso.clients import charts
from aso.clients.charts import ChartIndex, ChartsClient

from .conftest import FIXTURES
from .test_http import fast_fetcher

CHARTS_BODY = (FIXTURES / "charts_finance_free_us.json").read_text(encoding="utf-8")
URL_PATTERN = r"https://itunes\.apple\.com/[a-z]{2}/rss/.+/json"
FEED_COUNT = len(config.CHART_FEEDS) * len(config.CHART_GENRES)


# --- parse_feed ------------------------------------------------------------


def test_parse_feed_reads_ids_in_chart_order() -> None:
    assert charts.parse_feed(CHARTS_BODY) == [1245684460, 111111111, 6739166773]


def test_a_one_entry_feed_arrives_as_a_bare_object() -> None:
    """Apple collapses a single-entry list into the object itself."""
    body = json.dumps(
        {"feed": {"entry": {"id": {"attributes": {"im:id": "42"}}}}}
    )
    assert charts.parse_feed(body) == [42]


@pytest.mark.parametrize(
    "body",
    [
        "not json at all",
        "[]",
        '{"feed": null}',
        '{"feed": {}}',
        '{"feed": {"entry": "surprise"}}',
        '{"feed": {"entry": [{"id": {}}]}}',
        '{"feed": {"entry": [{"id": {"attributes": {"im:id": "not-a-number"}}}]}}',
    ],
)
def test_a_shape_change_returns_empty_rather_than_raising(body: str) -> None:
    """The feed is undocumented, unversioned, and old enough to be retired.

    A competition score missing one component beats a refresh that dies
    because Apple changed a payload.
    """
    assert charts.parse_feed(body) == []


def test_unreadable_entries_are_skipped_not_fatal() -> None:
    """One bad entry must not discard the ninety-nine good ones."""
    body = json.dumps(
        {
            "feed": {
                "entry": [
                    {"id": {"attributes": {"im:id": "1"}}},
                    {"no": "id here"},
                    {"id": {"attributes": {"im:id": "3"}}},
                ]
            }
        }
    )
    assert charts.parse_feed(body) == [1, 3]


# --- ChartIndex ------------------------------------------------------------


def test_index_reports_the_best_rank_across_charts() -> None:
    """An app at #3 in one chart and #90 in another is a #3 app.

    Averaging would penalise charting in several places, which is backwards.
    """
    index = ChartIndex(country="us", ranks={7: 3}, charts_loaded=2)
    assert index.rank_of(7) == 3


def test_an_uncharted_app_has_no_rank() -> None:
    index = ChartIndex(country="us", ranks={7: 3}, charts_loaded=2)
    assert index.rank_of(999) is None


def test_an_index_nothing_loaded_into_is_falsey() -> None:
    """Empty-because-everything-failed must not read as 'nothing charts'."""
    assert not ChartIndex(country="us", ranks={}, charts_loaded=0)
    assert ChartIndex(country="us", ranks={1: 1}, charts_loaded=1)


def test_an_index_that_loaded_but_found_nothing_is_still_truthy() -> None:
    """A storefront whose charts are genuinely empty is a measurement."""
    assert ChartIndex(country="us", ranks={}, charts_loaded=5)


# --- ChartsClient ----------------------------------------------------------


@respx.mock
async def test_index_pulls_every_feed_and_genre(store: store_module.Store) -> None:
    route = respx.get(url__regex=URL_PATTERN).mock(
        return_value=httpx.Response(200, text=CHARTS_BODY)
    )
    async with fast_fetcher() as fetcher:
        index = await ChartsClient(fetcher).index("us")

    assert route.call_count == FEED_COUNT
    assert index.charts_loaded == FEED_COUNT
    assert index.rank_of(1245684460) == 1
    assert index.rank_of(6739166773) == 3


@respx.mock
async def test_the_second_call_is_served_from_cache(store: store_module.Store) -> None:
    route = respx.get(url__regex=URL_PATTERN).mock(
        return_value=httpx.Response(200, text=CHARTS_BODY)
    )
    async with fast_fetcher() as fetcher:
        client = ChartsClient(fetcher)
        await client.index("us")
        cold = route.call_count
        await client.index("us")
        assert route.call_count == cold, "48 requests a day, not 48 per keyword"


@respx.mock
async def test_one_failing_feed_costs_only_that_feed(store: store_module.Store) -> None:
    """Some genres have no chart, and the feed drops URLs without notice."""
    calls = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            return httpx.Response(404)
        return httpx.Response(200, text=CHARTS_BODY)

    respx.get(url__regex=URL_PATTERN).mock(side_effect=responder)
    async with fast_fetcher(retry_attempts=1) as fetcher:
        index = await ChartsClient(fetcher).index("us")

    assert 0 < index.charts_loaded < FEED_COUNT
    assert index, "a partial index is still an index"
    assert index.rank_of(1245684460) == 1


@respx.mock
async def test_total_failure_yields_an_unknown_index_not_an_empty_one(
    store: store_module.Store,
) -> None:
    respx.get(url__regex=URL_PATTERN).mock(return_value=httpx.Response(500))
    async with fast_fetcher(retry_attempts=1) as fetcher:
        index = await ChartsClient(fetcher).index("us")

    assert index.charts_loaded == 0
    assert not index, "must renormalize away, not score the storefront as easy"


@respx.mock
async def test_a_failed_feed_is_never_cached(store: store_module.Store) -> None:
    """Caching a 500 would poison the index for a whole day."""
    respx.get(url__regex=URL_PATTERN).mock(return_value=httpx.Response(500))
    async with fast_fetcher(retry_attempts=1) as fetcher:
        await ChartsClient(fetcher).index("us")

    key = cache.charts_key("us", config.CHART_FEEDS[0], next(iter(config.CHART_GENRES)))
    assert cache.default_cache.get(key, config.CHARTS_TTL_DAYS) is None


@respx.mock
async def test_country_is_normalized_into_the_url_and_the_key(
    store: store_module.Store,
) -> None:
    route = respx.get(url__regex=URL_PATTERN).mock(
        return_value=httpx.Response(200, text=CHARTS_BODY)
    )
    async with fast_fetcher() as fetcher:
        index = await ChartsClient(fetcher).index("GB")

    assert index.country == "gb"
    assert all("/gb/rss/" in str(call.request.url) for call in route.calls)
