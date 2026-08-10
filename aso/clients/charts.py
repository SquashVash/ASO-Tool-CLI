"""Top-charts client — how big are the apps holding a keyword's top 10?

    GET https://itunes.apple.com/{cc}/rss/{feed}/limit=100/genre={id}/json

WHY THIS EXISTS
---------------
Every commercial ASO tool builds difficulty on a per-app strength number —
AppTweak calls it App Power, Appfigures lists "downloads, ranks" first among
the inputs to its Competitiveness index. This model had no equivalent: it could
see how many ratings an app had but not whether the app was a chart-topper, so
the hard end of the scale was empty and the whole column read low.

Chart rank is the closest free proxy for downloads and revenue. Apple publishes
it, unauthenticated, and it costs 48 requests per storefront per day.

WHAT THIS IS NOT
----------------
It is not a downloads estimate. It is an ordinal position in one of Apple's
published charts, and the mapping from position to installs is category- and
day-dependent and unknown to us. `comp_app_power` treats it as ordinal
throughout, which is all it can support.

Nor is absence from the charts a missing measurement. We pull the full top 100
of every app genre, so an app absent from the index is one we looked for and
did not find — a measured "not a top-100 app in its category". That is a fact,
and `app_power_component` scores it 0 rather than dropping it. Contrast a
missing *index* (the feed failed, or was never built), which is genuinely
unknown and makes the whole component `None`.

WHY THE LEGACY FEED
-------------------
See `config.CHARTS_URL_TEMPLATE`. The newer v2 feed cannot serve genre charts,
which is where all of the coverage is, and the store-wide charts it does serve
cover 8.2% of the top-10 apps we care about against the genre charts' 40.2%.

Both endpoints are undocumented. This one is also unversioned and old enough to
be retired without notice, so every parse failure here degrades to an empty
index rather than raising: a competition score missing one component is a far
better outcome than a refresh that dies because Apple changed a feed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from ..cache import Cache, default_cache, charts_key
from ..config import (
    CHART_FEEDS,
    CHART_GENRES,
    CHART_LIMIT,
    CHARTS_TTL_DAYS,
    CHARTS_URL_TEMPLATE,
    Settings,
)
from ..config import settings as default_settings
from ..http import FetchError, Fetcher

logger = logging.getLogger(__name__)

CACHE_KIND = "charts"


@dataclass(frozen=True)
class ChartIndex:
    """Best chart position per app, across every chart pulled for a storefront.

    `ranks` maps `track_id` to the *best* (numerically lowest) position the app
    holds in any chart. Best rather than an average across charts: the question
    the component asks is "how big is this app", and an app sitting at #3 in
    Finance and #90 in Productivity is a #3 app that also happens to chart
    elsewhere. Averaging would penalise breadth, which is backwards.
    """

    country: str
    ranks: dict[int, int] = field(default_factory=dict)
    # How many chart feeds actually parsed. Zero means the index is empty
    # because everything failed, which must not look like "nothing charts".
    charts_loaded: int = 0
    # The depth each chart was pulled to, so the component's normalization
    # divides by the real ceiling rather than a constant that might drift.
    depth: int = CHART_LIMIT

    def rank_of(self, track_id: int) -> int | None:
        """Best chart position, or `None` if the app charts nowhere."""
        return self.ranks.get(track_id)

    def __bool__(self) -> bool:
        """An index nothing loaded into is falsey — it proves nothing."""
        return self.charts_loaded > 0

    def __len__(self) -> int:
        return len(self.ranks)


def parse_feed(body: str) -> list[int]:
    """Track ids from one chart feed, in chart order.

    Returns `[]` for anything it cannot read. The legacy feed is undocumented
    and unversioned; see the module docstring for why a shape change must not
    raise. An empty list from a genuinely empty genre (Catalogs has no chart)
    and an empty list from a broken payload are the same to the caller, which
    is correct — neither contributes ranks.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        logger.warning("chart feed was not JSON")
        return []
    if not isinstance(payload, dict):
        return []

    feed = payload.get("feed")
    if not isinstance(feed, dict):
        return []

    entries = feed.get("entry")
    # A one-entry feed comes back as a bare object rather than a list.
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return []

    ids: list[int] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        identifier = entry.get("id")
        attributes = identifier.get("attributes") if isinstance(identifier, dict) else None
        raw = attributes.get("im:id") if isinstance(attributes, dict) else None
        try:
            ids.append(int(raw))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return ids


class ChartsClient:
    """Cached, rate-limited access to Apple's top-charts feeds.

    Shares the caller's `Fetcher`, and therefore the iTunes rate limiter, for
    the same reason `ITunesClient` does: these requests go to the same host and
    count against the same unpublished per-IP threshold.
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

    async def index(self, country: str, *, force: bool = False) -> ChartIndex:
        """Build the chart index for one storefront.

        Pulls every (feed, genre) pair, each cached independently so a failure
        part-way through costs only the feeds that failed. Never raises: a feed
        that cannot be fetched is logged and skipped, and if every one of them
        fails the returned index is falsey, which
        `competition.app_power_component` reads as "unknown" rather than as
        "nothing charts".
        """
        country = country.lower()
        ranks: dict[int, int] = {}
        loaded = 0

        for feed in CHART_FEEDS:
            for genre in CHART_GENRES:
                body = await self._feed_body(country, feed, genre, force=force)
                if body is None:
                    continue
                ids = parse_feed(body)
                if not ids:
                    continue
                loaded += 1
                for position, track_id in enumerate(ids, start=1):
                    current = ranks.get(track_id)
                    if current is None or position < current:
                        ranks[track_id] = position

        logger.info(
            "chart index for %s: %d apps from %d/%d feeds",
            country,
            len(ranks),
            loaded,
            len(CHART_FEEDS) * len(CHART_GENRES),
        )
        return ChartIndex(
            country=country, ranks=ranks, charts_loaded=loaded, depth=CHART_LIMIT
        )

    async def _feed_body(
        self, country: str, feed: str, genre: int, *, force: bool
    ) -> str | None:
        key = charts_key(country, feed, genre)
        if not force:
            cached = self.cache.get(key, CHARTS_TTL_DAYS)
            if cached is not None:
                return cached.body

        url = CHARTS_URL_TEMPLATE.format(
            country=country, feed=feed, limit=CHART_LIMIT, genre=genre
        )
        try:
            body = await self.fetcher.get_text(url, {})
        except FetchError as exc:
            # Not fatal, and not even unusual: some genres have no chart and
            # the feed is old enough that individual URLs come and go.
            logger.warning("chart feed %s/%s failed for %s: %s", feed, genre, country, exc)
            return None

        self.cache.put(key, CACHE_KIND, body)
        return body
