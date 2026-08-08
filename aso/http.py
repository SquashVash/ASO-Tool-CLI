"""Shared, rate-limited HTTP layer.

Every request to Apple — iTunes search and autocomplete alike — goes through a
single `Fetcher`. That's the point: the rate limit is per IP, so it has to be
shared across clients, not per client. Construct one `Fetcher` per run and hand
the same instance to every client.

Two mechanisms stack:

* a **token bucket** paces requests to `rate_limit_per_min` (default 15/min,
  under the ~20/min where iTunes starts returning 403);
* an **`asyncio.Semaphore`** caps requests in flight at `max_concurrency`.

With the default burst of 1 the bucket is the binding constraint and the
semaphore is a backstop — it matters if you raise `RATE_LIMIT_BURST`, or if
Apple is slow enough that several requests overlap.

Failures are never swallowed. Transient statuses (403/429/5xx) and transport
errors retry with exponential backoff and jitter; after `retry_attempts` the
`FetchError` propagates so the caller can record a failed-fetch marker.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import TracebackType

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .config import (
    RATE_LIMIT_BURST,
    RETRY_INITIAL_WAIT_SECONDS,
    RETRY_JITTER_SECONDS,
    RETRY_MAX_WAIT_SECONDS,
    USER_AGENT,
    Settings,
    settings as default_settings,
)

logger = logging.getLogger(__name__)

# Statuses worth trying again. 403 is on this list because iTunes uses it for
# rate limiting rather than for genuine authorization failures.
TRANSIENT_STATUSES = frozenset({403, 408, 429, 500, 502, 503, 504})


class FetchError(Exception):
    """A request that could not be completed, after any retries."""

    def __init__(
        self,
        url: str,
        *,
        status: int | None = None,
        attempts: int = 1,
        detail: str = "",
    ) -> None:
        self.url = url
        self.status = status
        self.attempts = attempts
        self.detail = detail
        status_part = f"HTTP {status}" if status is not None else "transport error"
        super().__init__(
            f"{status_part} for {url} after {attempts} attempt(s)"
            + (f": {detail}" if detail else "")
        )


class TransientFetchError(FetchError):
    """A `FetchError` that was worth retrying. Raised again if retries run out."""


class TokenBucket:
    """Async token bucket.

    `capacity` is the burst size; tokens refill continuously at
    `rate_per_minute / 60` per second. `acquire()` holds an internal lock
    across its sleep, so waiters are served in arrival order and never wake up
    together to fire a burst.
    """

    def __init__(self, rate_per_minute: int, capacity: int = RATE_LIMIT_BURST) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        self.interval = 60.0 / rate_per_minute
        self.capacity = max(1, capacity)
        self._tokens = float(self.capacity)
        self._updated: float | None = None
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                if self._updated is None:
                    self._updated = now
                self._tokens = min(
                    float(self.capacity),
                    self._tokens + (now - self._updated) / self.interval,
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) * self.interval)


class Fetcher:
    """Rate-limited async GET, shared by every client in a run."""

    def __init__(
        self,
        config: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = config or default_settings
        self.bucket = TokenBucket(self.settings.rate_limit_per_min)
        self.semaphore = asyncio.Semaphore(self.settings.max_concurrency)
        # Exposed as an attribute so tests can swap in `wait_none()` instead of
        # actually sleeping through the backoff.
        self.retry_wait = wait_exponential_jitter(
            initial=RETRY_INITIAL_WAIT_SECONDS,
            max=RETRY_MAX_WAIT_SECONDS,
            jitter=RETRY_JITTER_SECONDS,
        )
        self._client = client
        self._owns_client = client is None
        # Counters, surfaced by `aso refresh` so a run's HTTP cost is visible.
        self.requests_made = 0
        self.retries = 0

    async def __aenter__(self) -> "Fetcher":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.settings.http_timeout_seconds,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Fetcher must be used as an async context manager")
        return self._client

    async def get_text(self, url: str, params: dict[str, str]) -> str:
        """GET `url`, returning the body as text.

        Raises `FetchError` on give-up. Never returns a partial or empty result
        to paper over a failure — an empty SERP and a failed fetch must stay
        distinguishable downstream.
        """
        attempts = 0

        async def attempt() -> str:
            nonlocal attempts
            attempts += 1
            return await self._request(url, params)

        retrying = AsyncRetrying(
            stop=stop_after_attempt(self.settings.retry_attempts),
            wait=self.retry_wait,
            retry=retry_if_exception_type(TransientFetchError),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )

        try:
            return await retrying(attempt)
        except TransientFetchError as exc:
            logger.error(
                "giving up on %s after %d attempts: %s", url, attempts, exc.detail
            )
            raise FetchError(
                url, status=exc.status, attempts=attempts, detail=exc.detail
            ) from exc
        except RetryError as exc:  # pragma: no cover - reraise=True makes this rare
            raise FetchError(url, attempts=attempts, detail=str(exc)) from exc
        finally:
            if attempts > 1:
                self.retries += attempts - 1

    async def _request(self, url: str, params: dict[str, str]) -> str:
        await self.bucket.acquire()
        async with self.semaphore:
            self.requests_made += 1
            try:
                response = await self.client.get(url, params=params)
            except httpx.HTTPError as exc:
                raise TransientFetchError(
                    url, attempts=1, detail=f"{type(exc).__name__}: {exc}"
                ) from exc

        if response.status_code in TRANSIENT_STATUSES:
            logger.warning(
                "transient HTTP %s from %s (params=%s)",
                response.status_code,
                url,
                params,
            )
            raise TransientFetchError(
                url,
                status=response.status_code,
                detail=response.text[:200],
            )
        if response.status_code >= 400:
            # Permanent: a bad request won't get better by asking again.
            logger.error("HTTP %s from %s (params=%s)", response.status_code, url, params)
            raise FetchError(
                url, status=response.status_code, detail=response.text[:200]
            )
        return response.text
