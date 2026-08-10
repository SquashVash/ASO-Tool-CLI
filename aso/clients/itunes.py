"""iTunes Search API client — the competitor set behind the competition score.

    GET https://itunes.apple.com/search
        ?term={keyword}&entity=software&country={cc}&limit=200

READ THIS BEFORE TRUSTING THE ORDERING
--------------------------------------
This endpoint is **not** the App Store search index. It is a separate, older
content-search service. Its ordering correlates with App Store ranking but is
not the same ranking: it is not personalized, does not reflect Apple's current
relevance model, ignores Search Ads placements, and drifts from what a user
actually sees in the App Store app.

What it is good for: reading the *shape* of the competitive field around a
term — how much review mass sits on it, whether incumbents target it in their
titles, whether one publisher owns the space. That is exactly what the
competition score consumes.

What it must never be presented as: true App Store rank. The `serps` table
stores this ordering because tracking movement in it is still informative, but
anything surfaced to a user has to be labelled as iTunes Search order, not App
Store rank.

Two further honesty notes about the response shape:

* **No subtitle.** The Search API does not return an app's App Store subtitle.
  `AppRecord.subtitle` is parsed defensively (in case a storefront or a future
  API version includes it) but is `None` in practice, which means the
  `comp_exact_match` component degrades to a title-only match. That
  understates competition for terms incumbents target in the subtitle.
* **`resultCount` is a floor, not a total.** It counts what was returned, so
  it saturates at `limit`, which is now 200 — the API's documented maximum. The
  `comp_breadth` component therefore separates thin niches from crowded ones
  but still cannot distinguish "200 results" from "5000 results". The
  difference from before is that 200 is a wide enough field for most keywords
  to land inside it rather than pinned against the ceiling; see
  `config.ITUNES_SEARCH_LIMIT`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from ..cache import Cache, default_cache, serp_key
from ..config import ITUNES_SEARCH_LIMIT, ITUNES_SEARCH_URL, Settings
from ..config import settings as default_settings
from ..files import utcnow
from ..http import Fetcher

logger = logging.getLogger(__name__)

CACHE_KIND = "serp"


@dataclass(frozen=True)
class AppRecord:
    """One app as the Search API describes it.

    Every field except `track_id` is optional. `averageUserRating` is missing
    for apps below Apple's rating threshold, `sellerName` is occasionally
    absent, and `subtitle` is never present (see module docstring). Scoring
    must treat these as unknown, never as zero.
    """

    track_id: int
    track_name: str | None = None
    subtitle: str | None = None
    seller_name: str | None = None
    user_rating_count: int | None = None
    average_user_rating: float | None = None
    current_version_release_date: str | None = None
    price: float | None = None
    genres: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Serp:
    """A parsed search response for one (keyword, country)."""

    keyword: str
    country: str
    apps: list[AppRecord]
    result_count: int
    captured_at: str
    from_cache: bool

    def top(self, n: int) -> list[AppRecord]:
        return self.apps[:n]

    def __len__(self) -> int:
        return len(self.apps)


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_str(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def parse_app(raw: dict) -> AppRecord | None:
    """Parse one result entry. Returns None if it has no usable trackId."""
    track_id = _as_int(raw.get("trackId"))
    if track_id is None:
        return None
    genres = raw.get("genres")
    return AppRecord(
        track_id=track_id,
        track_name=_as_str(raw.get("trackName")),
        subtitle=_as_str(raw.get("subtitle")),
        # sellerName is the App Store publisher line; artistName is the
        # fallback the API uses for some older records.
        seller_name=_as_str(raw.get("sellerName")) or _as_str(raw.get("artistName")),
        user_rating_count=_as_int(raw.get("userRatingCount")),
        average_user_rating=_as_float(raw.get("averageUserRating")),
        current_version_release_date=_as_str(raw.get("currentVersionReleaseDate")),
        price=_as_float(raw.get("price")),
        genres=[g for g in genres if isinstance(g, str)] if isinstance(genres, list) else [],
    )


def parse_search_response(
    body: str, keyword: str, country: str, *, captured_at: str, from_cache: bool
) -> Serp:
    """Parse a raw search response body into a `Serp`.

    Raises `ValueError` on malformed JSON rather than returning an empty
    result — a parse failure and a genuinely empty SERP mean different things
    and must not collapse into the same value.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"iTunes search response was not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("iTunes search response was not a JSON object")

    raw_results = payload.get("results")
    raw_results = raw_results if isinstance(raw_results, list) else []
    apps = [app for app in (parse_app(r) for r in raw_results if isinstance(r, dict)) if app]

    # Trust the parsed length over the reported count; they disagree when an
    # entry is missing a trackId.
    reported = _as_int(payload.get("resultCount"))
    result_count = max(reported or 0, len(apps))

    return Serp(
        keyword=keyword,
        country=country,
        apps=apps,
        result_count=result_count,
        captured_at=captured_at,
        from_cache=from_cache,
    )


class ITunesClient:
    """Cached, rate-limited access to the iTunes Search API.

    The `Fetcher` is passed in rather than created here so that iTunes and
    autocomplete traffic share one rate limiter — which is the only way the
    per-IP limit is actually respected.
    """

    def __init__(
        self,
        fetcher: Fetcher,
        cache_store: Cache | None = None,
        config: Settings | None = None,
    ) -> None:
        self.fetcher = fetcher
        self.cache = cache_store if cache_store is not None else default_cache
        self.settings = config or default_settings

    async def search(
        self,
        term: str,
        country: str,
        *,
        limit: int = ITUNES_SEARCH_LIMIT,
        force: bool = False,
    ) -> Serp:
        """Fetch (or read from cache) the SERP for `term` in `country`.

        Propagates `FetchError` on give-up; the pipeline turns that into a
        failed-fetch marker on the snapshot.
        """
        country = country.lower()
        key = serp_key(term, country, limit)

        if not force:
            cached = self.cache.get(key, self.settings.serp_ttl_days)
            if cached is not None:
                logger.debug("serp cache hit for %r (%s)", term, country)
                return parse_search_response(
                    cached.body,
                    term,
                    country,
                    captured_at=cached.fetched_at,
                    from_cache=True,
                )

        body = await self.fetcher.get_text(
            ITUNES_SEARCH_URL,
            {
                "term": term,
                "entity": "software",
                "country": country,
                "limit": str(limit),
            },
        )
        # Parse before caching so a malformed body is never stored as if good.
        serp = parse_search_response(
            body, term, country, captured_at=utcnow(), from_cache=False
        )
        self.cache.put(key, CACHE_KIND, body)
        logger.info(
            "fetched serp for %r (%s): %d results", term, country, len(serp.apps)
        )
        return serp
