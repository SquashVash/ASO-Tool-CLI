from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
import respx

from aso.clients import itunes
from aso.http import FetchError

from .conftest import FIXTURES
from .test_http import URL, fast_fetcher

REAL_FIXTURE = FIXTURES / "itunes_search_candlestick_us.json"


def fixture_body() -> str:
    return REAL_FIXTURE.read_text(encoding="utf-8")


def client(conn: sqlite3.Connection, fetcher) -> itunes.ITunesClient:
    return itunes.ITunesClient(fetcher, conn)


# --- parsing ---------------------------------------------------------------


def test_parses_the_real_captured_response() -> None:
    serp = itunes.parse_search_response(
        fixture_body(),
        "candlestick patterns",
        "us",
        captured_at="2026-08-08T00:00:00Z",
        from_cache=False,
    )
    assert len(serp.apps) > 10
    assert serp.result_count == len(serp.apps)
    first = serp.apps[0]
    assert first.track_id > 0
    assert first.track_name
    assert first.seller_name
    assert isinstance(first.genres, list) and first.genres


def test_real_response_carries_no_subtitle() -> None:
    """Guards the docstring claim: the Search API has no subtitle field.

    If Apple ever starts returning one, this fails and `comp_exact_match`
    should be revisited — it currently degrades to a title-only match.
    """
    payload = json.loads(fixture_body())
    assert all("subtitle" not in result for result in payload["results"])


def test_top_n_slices_in_rank_order() -> None:
    serp = itunes.parse_search_response(
        fixture_body(), "k", "us", captured_at="t", from_cache=False
    )
    assert serp.top(10) == serp.apps[:10]
    assert len(serp.top(10)) == 10


def test_missing_optional_fields_parse_as_none() -> None:
    app = itunes.parse_app({"trackId": 42})
    assert app is not None
    assert app.track_id == 42
    assert app.track_name is None
    assert app.average_user_rating is None
    assert app.user_rating_count is None
    assert app.genres == []


def test_entries_without_a_track_id_are_dropped() -> None:
    assert itunes.parse_app({"trackName": "no id"}) is None
    serp = itunes.parse_search_response(
        json.dumps({"resultCount": 2, "results": [{"trackId": 1}, {"trackName": "x"}]}),
        "k",
        "us",
        captured_at="t",
        from_cache=False,
    )
    assert len(serp.apps) == 1
    # resultCount stays the API's larger figure rather than silently shrinking.
    assert serp.result_count == 2


def test_junk_field_types_do_not_crash_parsing() -> None:
    app = itunes.parse_app(
        {
            "trackId": "77",
            "trackName": "",
            "averageUserRating": "not a number",
            "userRatingCount": None,
            "genres": "Finance",
            "price": "0.00",
        }
    )
    assert app is not None
    assert app.track_id == 77
    assert app.track_name is None
    assert app.average_user_rating is None
    assert app.genres == []
    assert app.price == 0.0


def test_empty_result_set_parses_to_an_empty_serp() -> None:
    serp = itunes.parse_search_response(
        json.dumps({"resultCount": 0, "results": []}),
        "zzzz",
        "us",
        captured_at="t",
        from_cache=False,
    )
    assert len(serp) == 0
    assert serp.result_count == 0


def test_malformed_body_raises_rather_than_returning_empty() -> None:
    with pytest.raises(ValueError):
        itunes.parse_search_response(
            "<html>503</html>", "k", "us", captured_at="t", from_cache=False
        )
    with pytest.raises(ValueError):
        itunes.parse_search_response("[]", "k", "us", captured_at="t", from_cache=False)


# --- client + caching ------------------------------------------------------


@respx.mock
async def test_search_fetches_then_serves_from_cache(conn: sqlite3.Connection) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, text=fixture_body()))
    async with fast_fetcher() as fetcher:
        api = client(conn, fetcher)
        first = await api.search("candlestick patterns", "us")
        second = await api.search("candlestick patterns", "us")

    assert route.call_count == 1, "second call should not hit the network"
    assert first.from_cache is False
    assert second.from_cache is True
    assert [a.track_id for a in first.apps] == [a.track_id for a in second.apps]


@respx.mock
async def test_search_sends_the_documented_parameters(conn: sqlite3.Connection) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, text=fixture_body()))
    async with fast_fetcher() as fetcher:
        await client(conn, fetcher).search("day trading", "de", limit=25)
    params = route.calls[0].request.url.params
    assert params["term"] == "day trading"
    assert params["entity"] == "software"
    assert params["country"] == "de"
    assert params["limit"] == "25"


@respx.mock
async def test_cache_is_scoped_per_storefront(conn: sqlite3.Connection) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, text=fixture_body()))
    async with fast_fetcher() as fetcher:
        api = client(conn, fetcher)
        await api.search("forex", "us")
        await api.search("forex", "gb")
    assert route.call_count == 2


@respx.mock
async def test_force_bypasses_the_cache(conn: sqlite3.Connection) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, text=fixture_body()))
    async with fast_fetcher() as fetcher:
        api = client(conn, fetcher)
        await api.search("forex", "us")
        await api.search("forex", "us", force=True)
    assert route.call_count == 2


@respx.mock
async def test_cache_survives_a_new_connection(tmp_path) -> None:
    """The cache is on disk, which is what makes a run resumable."""
    from aso import db

    path = tmp_path / "resume.db"
    first_conn = db.connect(path)
    db.migrate(first_conn)
    route = respx.get(URL).mock(return_value=httpx.Response(200, text=fixture_body()))
    async with fast_fetcher() as fetcher:
        await client(first_conn, fetcher).search("forex", "us")
    first_conn.close()

    second_conn = db.connect(path)
    async with fast_fetcher() as fetcher:
        serp = await client(second_conn, fetcher).search("forex", "us")
    second_conn.close()

    assert route.call_count == 1
    assert serp.from_cache is True


@respx.mock
async def test_search_caches_app_metadata(conn: sqlite3.Connection) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text=fixture_body()))
    async with fast_fetcher() as fetcher:
        serp = await client(conn, fetcher).search("candlestick patterns", "us")

    stored = conn.execute("SELECT COUNT(*) FROM apps WHERE country = 'us'").fetchone()[0]
    assert stored == len(serp.apps)

    app = itunes.load_app(conn, serp.apps[0].track_id, "us")
    assert app is not None
    assert app.track_name == serp.apps[0].track_name
    assert app.genres == serp.apps[0].genres


@respx.mock
async def test_app_metadata_is_stored_per_storefront(conn: sqlite3.Connection) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text=fixture_body()))
    async with fast_fetcher() as fetcher:
        api = client(conn, fetcher)
        await api.search("forex", "us")
        await api.search("forex", "jp")
    countries = {
        row["country"] for row in conn.execute("SELECT DISTINCT country FROM apps")
    }
    assert countries == {"us", "jp"}


@respx.mock
async def test_cache_hit_does_not_refresh_app_fetched_at(conn: sqlite3.Connection) -> None:
    """A cached body is as old as the cache entry; refreshing fetched_at on a
    cache hit would keep the 7-day app cache looking fresh forever."""
    respx.get(URL).mock(return_value=httpx.Response(200, text=fixture_body()))
    async with fast_fetcher() as fetcher:
        api = client(conn, fetcher)
        await api.search("forex", "us")
        conn.execute("UPDATE apps SET fetched_at = '2020-01-01T00:00:00Z'")
        await api.search("forex", "us")
    stamps = {row["fetched_at"] for row in conn.execute("SELECT fetched_at FROM apps")}
    assert stamps == {"2020-01-01T00:00:00Z"}


@respx.mock
async def test_failed_fetch_propagates_and_caches_nothing(conn: sqlite3.Connection) -> None:
    respx.get(URL).mock(return_value=httpx.Response(403, text="rate limited"))
    async with fast_fetcher(retry_attempts=2) as fetcher:
        with pytest.raises(FetchError):
            await client(conn, fetcher).search("forex", "us")
    assert conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0] == 0


@respx.mock
async def test_unparseable_response_is_not_cached(conn: sqlite3.Connection) -> None:
    """Otherwise a bad response poisons the cache for three days."""
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>oops</html>"))
    async with fast_fetcher() as fetcher:
        with pytest.raises(ValueError):
            await client(conn, fetcher).search("forex", "us")
    assert conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0] == 0
