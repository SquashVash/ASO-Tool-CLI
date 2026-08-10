"""App Store autocomplete client — the search-volume proxy.

    GET https://search.itunes.apple.com/WebObjects/MZSearchHints.woa/wa/hints
        ?q={prefix}&clientApplication=Software
        X-Apple-Store-Front: {storefront_id}-1,29

TWO CORRECTIONS TO THE OBVIOUS REQUEST SHAPE
--------------------------------------------
Both established by probing the live endpoint, not from documentation:

1. **The `X-Apple-Store-Front` header is required.** Without it the endpoint
   answers 200 with an empty `hints` array — a silent empty result, not an
   error. Every suggestion this tool has ever seen depends on that header
   being present.

2. **The `country` query parameter does nothing.** The storefront comes from
   the header alone. Sending `country=us` alongside a German storefront header
   returns German suggestions. That is why `STOREFRONTS` below exists: a
   two-letter code has to be translated into Apple's numeric storefront id or
   the request is silently answered for the wrong market.

`clientApplication=Software` does matter — it selects the iOS App Store index.
`MacSoftware`, `iTunes` and `Music` all return music results instead.

Response is an XML plist: `{"title": "Suggestions", "hints": [{"term", "url"}]}`,
at most 10 entries, empty array for a prefix with no suggestions.

WHAT THE ORDERING MEANS
-----------------------
The suggestion list is ordered, and that order is the entire volume signal:
Apple surfaces high-demand completions first. It is still only a proxy — an
ordinal hint about demand, never an impression count. See
`scoring/search.py` for the mapping and for `calibrate()`, which will fit it
against real Apple Search Ads data.
"""

from __future__ import annotations

import logging
import plistlib
from dataclasses import dataclass, field

from ..cache import Cache, default_cache, hints_key
from ..config import HINTS_URL, Settings
from ..config import settings as default_settings
from ..files import utcnow
from ..http import Fetcher

logger = logging.getLogger(__name__)

CACHE_KIND = "hints"

# Apple storefront ids, keyed by ISO country code. These are long-standing and
# stable. The ones this tool has actually exercised against the live endpoint
# are marked; the rest follow Apple's published numbering.
#
# A country that isn't here raises rather than silently falling back to the US
# storefront — quietly researching the wrong market is worse than an error.
STOREFRONTS: dict[str, int] = {
    "us": 143441,  # verified
    "gb": 143444,  # verified
    "de": 143443,  # verified
    "jp": 143462,  # verified
    "ca": 143455,
    "au": 143460,
    "fr": 143442,
    "it": 143450,
    "es": 143454,
    "nl": 143452,
    "se": 143456,
    "dk": 143458,
    "no": 143457,
    "fi": 143447,
    "ie": 143449,
    "at": 143445,
    "be": 143446,
    "ch": 143459,
    "pt": 143453,
    "gr": 143448,
    "pl": 143478,
    "cz": 143489,
    "hu": 143482,
    "ro": 143487,
    "ru": 143469,
    "tr": 143480,
    "il": 143491,
    "ae": 143481,
    "sa": 143479,
    "za": 143472,
    "in": 143467,
    "kr": 143466,
    "cn": 143465,
    "hk": 143463,
    "tw": 143470,
    "sg": 143464,
    "my": 143473,
    "th": 143475,
    "id": 143476,
    "ph": 143474,
    "vn": 143471,
    "nz": 143461,
    "br": 143503,
    "mx": 143468,
    "ar": 143505,
    "cl": 143483,
    "co": 143501,
    "pe": 143507,
}

# The ",29" suffix is the classic iTunes client identifier. Apple accepts the
# header as "{storefront}-{language},{client}"; -1 selects the storefront's
# default language.
STOREFRONT_SUFFIX = "-1,29"

CLIENT_APPLICATION = "Software"

# Apple returns at most this many suggestions per prefix.
MAX_HINTS = 10


class UnknownStorefront(KeyError):
    """Raised for a country with no known storefront id."""

    def __init__(self, country: str) -> None:
        self.country = country
        known = ", ".join(sorted(STOREFRONTS))
        super().__init__(
            f"No App Store storefront id known for country {country!r}. "
            f"Add it to aso.clients.hints.STOREFRONTS. Known: {known}"
        )


def storefront_header(country: str) -> str:
    """Build the `X-Apple-Store-Front` value for a two-letter country code."""
    code = country.strip().lower()
    if code not in STOREFRONTS:
        raise UnknownStorefront(code)
    return f"{STOREFRONTS[code]}{STOREFRONT_SUFFIX}"


@dataclass(frozen=True)
class HintList:
    """Autocomplete suggestions for one prefix, in Apple's order."""

    prefix: str
    country: str
    terms: list[str] = field(default_factory=list)
    captured_at: str = ""
    from_cache: bool = False

    def rank_of(self, term: str) -> int | None:
        """1-based position of `term`, or None if absent.

        Exact match after normalization, not substring: the rank of a partial
        match would be meaningless.
        """
        needle = normalize_term(term)
        for index, candidate in enumerate(self.terms, start=1):
            if normalize_term(candidate) == needle:
                return index
        return None

    def extensions_of(self, term: str) -> int:
        """How many suggestions extend `term` without being equal to it.

        Apple offers completions, never the query echoed back, so a keyword
        that is a stem of popular longer terms is invisible to `rank_of` at
        every rung of its ladder. `insta` is the clearest case: it has real
        demand, no prefix of it ever surfaces it, and all ten of its
        suggestions extend it. Counting them recovers exactly the demand the
        ladder structurally cannot see.

        Prefix match on the normalized forms, so `habit` counts
        `habit tracker` but not `the habit burger grill`.
        """
        needle = normalize_term(term)
        if not needle:
            return 0
        return sum(
            1
            for candidate in self.terms
            if (normalized := normalize_term(candidate)) != needle
            and normalized.startswith(needle)
        )

    def __len__(self) -> int:
        return len(self.terms)


def normalize_term(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.split()).casefold()


def parse_hints(body: str | bytes) -> list[str]:
    """Parse the XML plist into an ordered list of suggestion terms.

    Raises `ValueError` on a malformed body. An empty `hints` array is a valid
    response meaning "no suggestions" and returns `[]` — distinct from a parse
    failure, which must never be mistaken for "this keyword has no volume".
    """
    raw = body.encode("utf-8") if isinstance(body, str) else body
    try:
        payload = plistlib.loads(raw)
    except Exception as exc:  # plistlib raises a variety of parse errors
        raise ValueError(f"autocomplete response was not a valid plist: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("autocomplete plist was not a dictionary")

    hints = payload.get("hints")
    if hints is None:
        raise ValueError("autocomplete plist had no 'hints' key")
    if not isinstance(hints, list):
        raise ValueError("autocomplete 'hints' was not an array")

    terms: list[str] = []
    for entry in hints:
        term = entry.get("term") if isinstance(entry, dict) else entry
        if isinstance(term, str) and term.strip():
            terms.append(term.strip())
    return terms


class HintsClient:
    """Cached, rate-limited access to App Store autocomplete.

    Shares its `Fetcher` — and therefore its rate limit — with the iTunes
    client, because Apple's limit is per IP.
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

    async def suggest(
        self, prefix: str, country: str, *, force: bool = False
    ) -> HintList:
        """Fetch (or read from cache) the suggestions for `prefix`.

        Propagates `FetchError` on give-up so the caller can record the
        failure rather than reading it as an absence of suggestions.
        """
        country = country.lower()
        header = storefront_header(country)  # raises before any network use
        key = hints_key(prefix, country)

        if not force:
            cached = self.cache.get(key, self.settings.hints_ttl_days)
            if cached is not None:
                logger.debug("hints cache hit for %r (%s)", prefix, country)
                return HintList(
                    prefix=prefix,
                    country=country,
                    terms=parse_hints(cached.body),
                    captured_at=cached.fetched_at,
                    from_cache=True,
                )

        body = await self.fetcher.get_text(
            HINTS_URL,
            {"q": prefix, "clientApplication": CLIENT_APPLICATION},
            {"X-Apple-Store-Front": header},
        )
        # Parse before caching so a malformed body is never stored as if good.
        terms = parse_hints(body)
        self.cache.put(key, CACHE_KIND, body)
        logger.info(
            "fetched hints for %r (%s): %d suggestions", prefix, country, len(terms)
        )
        return HintList(
            prefix=prefix,
            country=country,
            terms=terms,
            captured_at=utcnow(),
            from_cache=False,
        )
