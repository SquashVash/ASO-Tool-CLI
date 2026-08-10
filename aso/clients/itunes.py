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
import sqlite3
from dataclasses import dataclass, field

from .. import cache
from ..config import ITUNES_SEARCH_LIMIT, ITUNES_SEARCH_URL, Settings
from ..config import settings as default_settings
from ..db import utcnow
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


def upsert_apps(conn: sqlite3.Connection, apps: list[AppRecord], country: str) -> None:
    """Cache app metadata, keyed per storefront.

    This is the 7-day app cache: `apps.fetched_at` is refreshed on every write,
    and readers check it against `settings.app_ttl_days`.
    """
    now = utcnow()
    conn.executemany(
        """
        INSERT INTO apps (
            track_id, country, track_name, subtitle, seller_name,
            user_rating_count, average_user_rating,
            current_version_release_date, price, genres, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (track_id, country) DO UPDATE SET
            track_name                   = excluded.track_name,
            subtitle                     = excluded.subtitle,
            seller_name                  = excluded.seller_name,
            user_rating_count            = excluded.user_rating_count,
            average_user_rating          = excluded.average_user_rating,
            current_version_release_date = excluded.current_version_release_date,
            price                        = excluded.price,
            genres                       = excluded.genres,
            fetched_at                   = excluded.fetched_at
        """,
        [
            (
                app.track_id,
                country,
                app.track_name,
                app.subtitle,
                app.seller_name,
                app.user_rating_count,
                app.average_user_rating,
                app.current_version_release_date,
                app.price,
                json.dumps(app.genres),
                now,
            )
            for app in apps
        ],
    )


def load_app(conn: sqlite3.Connection, track_id: int, country: str) -> AppRecord | None:
    """Read a cached app record, ignoring its age.

    Age only matters when deciding whether to refetch; display paths want
    whatever is on hand.
    """
    row = conn.execute(
        "SELECT * FROM apps WHERE track_id = ? AND country = ?", (track_id, country)
    ).fetchone()
    if row is None:
        return None
    try:
        genres = json.loads(row["genres"]) if row["genres"] else []
    except json.JSONDecodeError:
        genres = []
    return AppRecord(
        track_id=row["track_id"],
        track_name=row["track_name"],
        subtitle=row["subtitle"],
        seller_name=row["seller_name"],
        user_rating_count=row["user_rating_count"],
        average_user_rating=row["average_user_rating"],
        current_version_release_date=row["current_version_release_date"],
        price=row["price"],
        genres=genres if isinstance(genres, list) else [],
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
        conn: sqlite3.Connection,
        config: Settings | None = None,
    ) -> None:
        self.fetcher = fetcher
        self.conn = conn
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
        key = cache.serp_key(term, country, limit)

        if not force:
            cached = cache.get(self.conn, key, self.settings.serp_ttl_days)
            if cached is not None:
                logger.debug("serp cache hit for %r (%s)", term, country)
                serp = parse_search_response(
                    cached.body,
                    term,
                    country,
                    captured_at=cached.fetched_at,
                    from_cache=True,
                )
                # Deliberately no app upsert on a cache hit: the cached body's
                # metadata is as old as the cache entry, and rewriting
                # apps.fetched_at would make stale data look freshly fetched.
                return serp

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
        cache.put(self.conn, key, CACHE_KIND, body)
        upsert_apps(self.conn, serp.apps, country)
        logger.info(
            "fetched serp for %r (%s): %d results", term, country, len(serp.apps)
        )
        return serp
