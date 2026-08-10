from __future__ import annotations


import httpx
import pytest
import respx

from aso import cache
from aso.clients import hints
from aso.http import FetchError

from .conftest import FIXTURES
from .test_http import fast_fetcher

HINTS_URL = "https://search.itunes.apple.com/WebObjects/MZSearchHints.woa/wa/hints"
REAL = FIXTURES / "hints_candlestick_us.plist"
EMPTY = FIXTURES / "hints_empty_us.plist"


def plist(terms: list[str]) -> str:
    entries = "".join(
        f"<dict><key>term</key><string>{t}</string>"
        f"<key>url</key><string>https://example.test</string></dict>"
        for t in terms
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<plist version="1.0"><dict><key>title</key><string>Suggestions</string>'
        f"<key>hints</key><array>{entries}</array></dict></plist>"
    )


def client(store: store_module.Store, fetcher) -> hints.HintsClient:
    return hints.HintsClient(fetcher)


# --- storefront mapping ----------------------------------------------------


def test_storefront_header_has_apples_format() -> None:
    assert hints.storefront_header("us") == "143441-1,29"
    assert hints.storefront_header("DE") == "143443-1,29"


def test_each_country_maps_to_a_distinct_storefront() -> None:
    ids = list(hints.STOREFRONTS.values())
    assert len(ids) == len(set(ids))


def test_unknown_country_raises_instead_of_defaulting_to_us() -> None:
    """Silently researching the wrong market is worse than failing."""
    with pytest.raises(hints.UnknownStorefront) as excinfo:
        hints.storefront_header("zz")
    assert "STOREFRONTS" in str(excinfo.value)


async def test_unknown_country_fails_before_any_request(store: store_module.Store) -> None:
    async with fast_fetcher() as fetcher:
        with pytest.raises(hints.UnknownStorefront):
            await client(store, fetcher).suggest("can", "zz")
        assert fetcher.requests_made == 0


# --- parsing ---------------------------------------------------------------


def test_parses_the_real_captured_plist() -> None:
    terms = hints.parse_hints(REAL.read_bytes())
    assert terms[0] == "candlestick patterns"
    assert len(terms) == hints.MAX_HINTS
    assert all(isinstance(t, str) and t for t in terms)


def test_real_empty_response_parses_to_no_terms() -> None:
    """A prefix with no suggestions is a valid answer, not an error."""
    assert hints.parse_hints(EMPTY.read_bytes()) == []


def test_parse_preserves_apples_ordering() -> None:
    ordered = ["b term", "a term", "c term"]
    assert hints.parse_hints(plist(ordered)) == ordered


def test_parse_accepts_str_or_bytes() -> None:
    body = REAL.read_bytes()
    assert hints.parse_hints(body) == hints.parse_hints(body.decode("utf-8"))


def test_blank_and_malformed_entries_are_skipped() -> None:
    assert hints.parse_hints(plist(["good", "  ", ""])) == ["good"]


@pytest.mark.parametrize(
    "body",
    [
        "<html>503 Service Unavailable</html>",
        "",
        '<?xml version="1.0"?><plist version="1.0"><array/></plist>',
        '<?xml version="1.0"?><plist version="1.0"><dict><key>title</key>'
        "<string>x</string></dict></plist>",
    ],
)
def test_malformed_bodies_raise_rather_than_reading_as_no_volume(body: str) -> None:
    with pytest.raises(ValueError):
        hints.parse_hints(body)


# --- HintList --------------------------------------------------------------


def test_rank_is_one_based_in_apples_order() -> None:
    hint_list = hints.HintList("can", "us", ["candle", "candlestick patterns", "candy"])
    assert hint_list.rank_of("candle") == 1
    assert hint_list.rank_of("candlestick patterns") == 2
    assert hint_list.rank_of("candy") == 3


def test_rank_ignores_case_and_spacing() -> None:
    hint_list = hints.HintList("can", "us", ["Candlestick   Patterns"])
    assert hint_list.rank_of("candlestick patterns") == 1


def test_rank_requires_an_exact_term_not_a_substring() -> None:
    """A substring match has no meaningful rank."""
    hint_list = hints.HintList("can", "us", ["candlestick patterns pro"])
    assert hint_list.rank_of("candlestick patterns") is None


def test_rank_of_absent_term_is_none() -> None:
    assert hints.HintList("can", "us", ["candle"]).rank_of("forex") is None


# --- client ----------------------------------------------------------------


@respx.mock
async def test_suggest_sends_the_storefront_header(store: store_module.Store) -> None:
    """Without this header the endpoint answers 200 with an empty array."""
    route = respx.get(HINTS_URL).mock(
        return_value=httpx.Response(200, text=REAL.read_text(encoding="utf-8"))
    )
    async with fast_fetcher() as fetcher:
        await client(store, fetcher).suggest("candlestick", "de")
    request = route.calls[0].request
    assert request.headers["X-Apple-Store-Front"] == "143443-1,29"
    assert request.url.params["q"] == "candlestick"
    assert request.url.params["clientApplication"] == "Software"


@respx.mock
async def test_suggest_caches_by_prefix_and_storefront(store: store_module.Store) -> None:
    route = respx.get(HINTS_URL).mock(
        return_value=httpx.Response(200, text=REAL.read_text(encoding="utf-8"))
    )
    async with fast_fetcher() as fetcher:
        api = client(store, fetcher)
        first = await api.suggest("candlestick", "us")
        second = await api.suggest("candlestick", "us")
        await api.suggest("candlestick", "gb")

    assert route.call_count == 2, "same prefix + storefront must not refetch"
    assert first.from_cache is False
    assert second.from_cache is True
    assert first.terms == second.terms


@respx.mock
async def test_force_bypasses_the_cache(store: store_module.Store) -> None:
    route = respx.get(HINTS_URL).mock(
        return_value=httpx.Response(200, text=REAL.read_text(encoding="utf-8"))
    )
    async with fast_fetcher() as fetcher:
        api = client(store, fetcher)
        await api.suggest("candlestick", "us")
        await api.suggest("candlestick", "us", force=True)
    assert route.call_count == 2


@respx.mock
async def test_empty_suggestions_are_cached_as_a_real_answer(store: store_module.Store) -> None:
    route = respx.get(HINTS_URL).mock(
        return_value=httpx.Response(200, text=EMPTY.read_text(encoding="utf-8"))
    )
    async with fast_fetcher() as fetcher:
        api = client(store, fetcher)
        result = await api.suggest("zzqxwvj", "us")
        await api.suggest("zzqxwvj", "us")
    assert result.terms == []
    assert route.call_count == 1


@respx.mock
async def test_failed_fetch_propagates_and_caches_nothing(store: store_module.Store) -> None:
    """A 403 must never be read as 'this prefix has no suggestions'."""
    respx.get(HINTS_URL).mock(return_value=httpx.Response(403, text="rate limited"))
    async with fast_fetcher(retry_attempts=2) as fetcher:
        with pytest.raises(FetchError):
            await client(store, fetcher).suggest("candlestick", "us")
    assert cache.default_cache.stats() == {}


@respx.mock
async def test_unparseable_response_is_not_cached(store: store_module.Store) -> None:
    respx.get(HINTS_URL).mock(return_value=httpx.Response(200, text="<html>oops</html>"))
    async with fast_fetcher() as fetcher:
        with pytest.raises(ValueError):
            await client(store, fetcher).suggest("candlestick", "us")
    assert cache.default_cache.stats() == {}


@respx.mock
async def test_hints_and_itunes_share_one_rate_limiter(store: store_module.Store) -> None:
    """Apple's limit is per IP, so both clients must draw on the same bucket."""
    from aso.clients.itunes import ITunesClient

    respx.get(HINTS_URL).mock(
        return_value=httpx.Response(200, text=REAL.read_text(encoding="utf-8"))
    )
    respx.get("https://itunes.apple.com/search").mock(
        return_value=httpx.Response(
            200,
            text=(FIXTURES / "itunes_search_candlestick_us.json").read_text(
                encoding="utf-8"
            ),
        )
    )
    async with fast_fetcher() as fetcher:
        await client(store, fetcher).suggest("candlestick", "us")
        await ITunesClient(fetcher).search("candlestick patterns", "us")
        assert fetcher.requests_made == 2
