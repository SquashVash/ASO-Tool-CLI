from __future__ import annotations

import asyncio
import time

import httpx
import pytest
import respx
from tenacity import wait_none

from aso.config import Settings
from aso.http import Fetcher, FetchError, TokenBucket

URL = "https://itunes.apple.com/search"


def fast_fetcher(**overrides) -> Fetcher:
    """A Fetcher with pacing and backoff removed, so tests run instantly."""
    config = Settings(**{"rate_limit_per_min": 15, "retry_attempts": 4, **overrides})
    fetcher = Fetcher(config)
    fetcher.bucket = TokenBucket(rate_per_minute=600_000)
    fetcher.retry_wait = wait_none()
    return fetcher


# --- token bucket ----------------------------------------------------------


async def test_bucket_paces_requests() -> None:
    # 600/min = 0.1s between tokens. Burst of 1 means the first is free and
    # the next two each wait a full interval.
    bucket = TokenBucket(rate_per_minute=600, capacity=1)
    start = time.monotonic()
    for _ in range(3):
        await bucket.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.2


async def test_bucket_allows_a_burst_up_to_capacity() -> None:
    bucket = TokenBucket(rate_per_minute=60, capacity=5)
    start = time.monotonic()
    for _ in range(5):
        await bucket.acquire()
    assert time.monotonic() - start < 0.5


async def test_bucket_serializes_concurrent_waiters() -> None:
    bucket = TokenBucket(rate_per_minute=600, capacity=1)
    start = time.monotonic()
    await asyncio.gather(*(bucket.acquire() for _ in range(4)))
    # 4 requests, 1 free, 3 paced at 0.1s.
    assert time.monotonic() - start >= 0.3


def test_bucket_rejects_a_nonsense_rate() -> None:
    with pytest.raises(ValueError):
        TokenBucket(rate_per_minute=0)


# --- retry behaviour -------------------------------------------------------


@respx.mock
async def test_retries_403_then_succeeds() -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(403, text="rate limited"),
            httpx.Response(403, text="rate limited"),
            httpx.Response(200, text="ok"),
        ]
    )
    async with fast_fetcher() as fetcher:
        assert await fetcher.get_text(URL, {"term": "x"}) == "ok"
    assert route.call_count == 3
    assert fetcher.requests_made == 3
    assert fetcher.retries == 2


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 408])
@respx.mock
async def test_retries_every_transient_status(status: int) -> None:
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(status), httpx.Response(200, text="ok")]
    )
    async with fast_fetcher() as fetcher:
        assert await fetcher.get_text(URL, {}) == "ok"
    assert route.call_count == 2


@respx.mock
async def test_gives_up_after_configured_attempts() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(403, text="nope"))
    async with fast_fetcher(retry_attempts=4) as fetcher:
        with pytest.raises(FetchError) as excinfo:
            await fetcher.get_text(URL, {})
    assert route.call_count == 4
    assert excinfo.value.status == 403
    assert excinfo.value.attempts == 4
    # The failure carries enough context for the pipeline to record it.
    assert "403" in str(excinfo.value)


@respx.mock
async def test_permanent_errors_are_not_retried() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(404, text="gone"))
    async with fast_fetcher() as fetcher:
        with pytest.raises(FetchError) as excinfo:
            await fetcher.get_text(URL, {})
    assert route.call_count == 1
    assert excinfo.value.status == 404


@respx.mock
async def test_transport_errors_are_retried() -> None:
    route = respx.get(URL).mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, text="ok")]
    )
    async with fast_fetcher() as fetcher:
        assert await fetcher.get_text(URL, {}) == "ok"
    assert route.call_count == 2


@respx.mock
async def test_errors_are_never_swallowed_into_an_empty_body() -> None:
    respx.get(URL).mock(return_value=httpx.Response(403))
    async with fast_fetcher(retry_attempts=1) as fetcher:
        with pytest.raises(FetchError):
            await fetcher.get_text(URL, {})


# --- concurrency -----------------------------------------------------------


@respx.mock
async def test_semaphore_caps_requests_in_flight() -> None:
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return httpx.Response(200, text="ok")

    respx.get(URL).mock(side_effect=handler)
    async with fast_fetcher(max_concurrency=3) as fetcher:
        await asyncio.gather(*(fetcher.get_text(URL, {"i": str(i)}) for i in range(10)))
    assert peak <= 3


async def test_client_requires_context_manager() -> None:
    fetcher = fast_fetcher()
    with pytest.raises(RuntimeError):
        _ = fetcher.client
