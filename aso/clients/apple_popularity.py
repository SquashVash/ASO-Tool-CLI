"""Apple's keyword popularity index — the 5-100 number, from the source.

    POST https://app-ads.apple.com/reporting/graphql
        Cookie: <a signed-in Apple Ads dashboard session>
        {"operationName": "getRecommendedKeywordsGql",
         "variables": {"adamId": ..., "text": <seed>, "storefronts": ["US"]}}

VERIFIED, AND NOT WHERE YOU WOULD EXPECT
----------------------------------------
Captured live from the Apple Ads dashboard on 2026-08-09. Three things about it
contradict the obvious guess, and all three were guessed wrong first:

1. **It is not on `api.searchads.apple.com`.** It lives on the web dashboard's
   own host, `app-ads.apple.com`.
2. **It is GraphQL, not REST.** One operation, `getRecommendedKeywordsGql`,
   under a `recommendationV2` root.
3. **It authenticates with a session cookie, not the ASA bearer token.** The
   JWT flow in `clients/asa.py` has no standing here. This is the awkward part
   and it is not papered over: see `ASO_APPLE_COOKIE`.

Schema introspection is disabled on the endpoint, so the query below is the
captured one, reproduced verbatim. Do not "tidy" it.

HOW A SEED BECOMES A MEASUREMENT
--------------------------------
The operation takes one seed `text` and returns up to ~100 related keywords,
each with a popularity. It is a recommendation tool, not a lookup — so reading
a specific keyword's popularity means asking for that keyword and finding it in
its own results.

That indirection turns out to be exactly the right shape, because Apple omits
terms below its reporting threshold. Observed live:

    instagram      -> 99      habit tracker -> 52
    insta          -> 73      habit         -> 47
    finsta         -> ABSENT FROM ITS OWN RESULTS

`finsta` measures 5/100 at AppFigures and scores 92 on this tool's proxy — it
is the worked example of the proxy's documented failure. Apple declining to
name it is a measurement of low demand, and `scoring.blend` uses it as the
upper bound it is.

THE BYCATCH IS WORTH MORE THAN THE CATCH
----------------------------------------
Every seed returns ~100 scored related keywords. Asking about 231 tracked
keywords yielded several hundred *additional* terms with Apple popularity
attached, for no extra request. `related` carries them out so the caller can
store them too — that is keyword discovery and demand measurement in one call.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field

from .. import cache
from ..config import (
    APPLE_POPULARITY_CEILING,
    APPLE_POPULARITY_FLOOR,
    Settings,
)
from ..config import settings as default_settings
from .apple_transport import (
    ApplePopularityError,
    ApplePopularitySessionExpired,
    PopularityTransport,
)

logger = logging.getLogger(__name__)

CACHE_KIND = "popularity"
SOURCE = "apple"

# Re-exported so callers keep importing their errors from one place even though
# the transports raise them.
__all__ = [
    "ApplePopularityClient",
    "ApplePopularityDisabled",
    "ApplePopularityError",
    "ApplePopularityNotConfigured",
    "ApplePopularitySessionExpired",
    "PopularityResult",
    "PopularityRow",
    "SOURCE",
    "normalize_term",
    "parse_recommendations",
]


class ApplePopularityDisabled(ApplePopularityError):
    """Called with `ASO_APPLE_POPULARITY_ENABLED` unset or false."""

    def __init__(self) -> None:
        super().__init__(
            "Apple keyword popularity is disabled. It calls an undocumented "
            "endpoint, so it is opt-in: set ASO_APPLE_POPULARITY_ENABLED=true "
            "in .env. With it off, demand scores fall back to the autocomplete "
            "and SERP proxy."
        )


class ApplePopularityNotConfigured(ApplePopularityError):
    """No session cookie or no app id."""


def normalize_term(value: str) -> str:
    """Fold a term for comparison.

    NFC first, because Apple returns composed and decomposed forms of the same
    keyword from different storefronts — `i̇nstagram` is in the tracked set here
    with a combining dot, and without normalization it would never match its
    own result and would be recorded as censored. That is a silent
    false measurement, which is the one outcome worth extra code to avoid.
    """
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


@dataclass(frozen=True)
class PopularityRow:
    """One keyword's reading.

    Three states, not two: a value, `censored` (Apple answered and did not name
    the term), and simply never asked — which is represented by the absence of
    a row, never by a row with a zero in it.
    """

    keyword: str
    country: str
    popularity: float | None
    censored: bool = False
    from_cache: bool = False

    @property
    def measured(self) -> bool:
        return self.popularity is not None and not self.censored


@dataclass
class PopularityResult:
    """What one pull learned, including the bycatch."""

    rows: list[PopularityRow] = field(default_factory=list)
    # Keywords that came back attached to somebody else's seed. Real Apple
    # measurements for terms nobody asked about.
    related: dict[str, float] = field(default_factory=dict)


def _coerce_popularity(value: object) -> float | None:
    """Read a popularity number, or None if absent or unusable.

    Clamped into Apple's stated 5-100 band rather than trusted. A value outside
    it means the field found is not the field intended, and a stray 0 would
    land in `demand_observations` looking like measured demand.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number <= 0:
        return None
    return max(APPLE_POPULARITY_FLOOR, min(APPLE_POPULARITY_CEILING, number))


def parse_recommendations(payload: object) -> list[tuple[str, float | None]]:
    """Pull (name, popularity) out of one GraphQL response.

    Raises on a shape that is not this endpoint's — a login redirect, an HTML
    error page, a GraphQL `errors` block. Returning an empty list for those
    would be indistinguishable from "Apple knows no keywords like this", and
    the caller would write a censored observation for every keyword in the run.
    """
    if not isinstance(payload, dict):
        raise ApplePopularityError(
            f"popularity response was not a JSON object: {type(payload).__name__}"
        )

    if payload.get("errors"):
        messages = "; ".join(
            str(e.get("message", e)) for e in payload["errors"] if isinstance(e, dict)
        )
        raise ApplePopularityError(f"GraphQL error: {messages or payload['errors']}")

    node = payload.get("data")
    for key in ("recommendationV2", "getRecommendedKeywords"):
        if not isinstance(node, dict):
            break
        node = node.get(key)

    if not isinstance(node, list):
        raise ApplePopularityError(
            "popularity response had no "
            "data.recommendationV2.getRecommendedKeywords list"
        )

    out: list[tuple[str, float | None]] = []
    for entry in node:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        out.append((name.strip(), _coerce_popularity(entry.get("popularity"))))
    return out


class ApplePopularityClient:
    """Reads Apple's popularity index through whichever transport it is given.

    Deliberately ignorant of how the session is held. Swapping a pasted cookie
    for a persistent browser profile changes nothing below this line, which is
    the property that makes the durable path worth building separately from the
    quick one.
    """

    def __init__(
        self,
        transport: PopularityTransport,
        conn: sqlite3.Connection,
        config: Settings | None = None,
    ) -> None:
        self.transport = transport
        self.conn = conn
        self.settings = config or default_settings

    async def popularity(
        self, keywords: Sequence[str], country: str, *, force: bool = False
    ) -> PopularityResult:
        """Readings for `keywords`, plus every related term seen along the way."""
        if not self.settings.apple_popularity_enabled:
            raise ApplePopularityDisabled()

        country = country.lower()
        ttl = self.settings.apple_popularity_ttl_days
        result = PopularityResult()

        for keyword in keywords:
            key = cache.popularity_key(keyword, country)
            cached = None if force else cache.get(self.conn, key, ttl)
            if cached is not None:
                stored = json.loads(cached.body)
                result.rows.append(
                    PopularityRow(
                        keyword=keyword,
                        country=country,
                        popularity=stored.get("popularity"),
                        censored=stored.get("popularity") is None,
                        from_cache=True,
                    )
                )
                continue

            payload = await self.transport.query(keyword, country)
            pairs = parse_recommendations(payload)

            needle = normalize_term(keyword)
            own: float | None = None
            for name, value in pairs:
                normalized = normalize_term(name)
                if normalized == needle:
                    own = value
                elif value is not None:
                    result.related[normalized] = value

            result.rows.append(
                PopularityRow(
                    keyword=keyword,
                    country=country,
                    popularity=own,
                    # Apple answered and did not name the term: below threshold.
                    censored=own is None,
                )
            )
            cache.put(self.conn, key, CACHE_KIND, json.dumps({"popularity": own}))

        logger.info(
            "popularity for %d keyword(s) in %s: %d scored, %d censored, "
            "%d related terms picked up",
            len(keywords),
            country,
            sum(1 for r in result.rows if r.measured),
            sum(1 for r in result.rows if r.censored),
            len(result.related),
        )
        return result


def build_transport(
    config: Settings, *, fetcher=None, headless: bool = True
) -> PopularityTransport:
    """Pick a transport from settings, and explain any refusal concretely.

    "auto" prefers a saved browser profile and falls back to a pasted cookie,
    because that is the order of durability — a profile that exists is one
    somebody deliberately created by signing in.
    """
    from pathlib import Path

    from ..config import (
        APPLE_TRANSPORT_BROWSER,
        APPLE_TRANSPORT_COOKIE,
    )
    from .apple_transport import BrowserTransport, CookieTransport

    if not config.apple_adam_id:
        raise ApplePopularityNotConfigured(
            "ASO_APPLE_ADAM_ID must be set in .env — it is your app's numeric "
            "App Store id, which the endpoint requires on every request."
        )

    wanted = config.apple_transport
    use_browser = wanted == APPLE_TRANSPORT_BROWSER or (
        wanted not in (APPLE_TRANSPORT_BROWSER, APPLE_TRANSPORT_COOKIE)
        and config.apple_profile_exists
    )

    if use_browser:
        if not config.apple_profile_exists:
            raise ApplePopularityNotConfigured(
                f"No saved Apple Ads session at {config.apple_profile_dir}.\n"
                "Run `aso apple login` once and sign in — the session is then "
                "reused headlessly and renews itself with use."
            )
        return BrowserTransport(
            Path(config.apple_profile_dir),
            adam_id=config.apple_adam_id,
            headless=headless,
        )

    if not config.apple_cookie:
        raise ApplePopularityNotConfigured(
            "No Apple Ads session available.\n"
            "  durable:  `aso apple login` (needs the `browser` extra)\n"
            "  quick:    paste a `Cookie` header into ASO_APPLE_COOKIE\n"
            "The endpoint takes no bearer token, so one of these is required."
        )
    if fetcher is None:
        raise ApplePopularityNotConfigured(
            "The cookie transport needs a Fetcher."
        )
    return CookieTransport(
        fetcher, cookie=config.apple_cookie, adam_id=config.apple_adam_id
    )
