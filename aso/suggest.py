"""Keyword discovery: read the completions instead of discarding them.

The scorer asks Apple, up to twelve times per keyword, "what do people search
that starts with this?" — and keeps three integers from the answer. This module
keeps the answer.

WHY THIS DOES NOT REUSE `search.observe`
----------------------------------------
`observe` walks prefixes longest-first and stops at the first miss: once the
keyword no longer appears in its own suggestions, nothing shorter will surface
it either, so probing on buys the score nothing.

That is right for scoring and backwards for discovery. The keywords worth
researching are the ones Apple never echoes back — `insta` is the standing
example, with real demand and a ladder that stops after one probe. Reusing
`observe` would return the fewest candidates exactly where the most are needed.

So discovery shares `prefix_lengths()`, the function that *defines* the ladder,
and probes every rung it returns. The rung arithmetic lives in one place; the
early stop stays where it belongs.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
Score anything. Pricing forty suggestions is forty SERP fetches plus forty
ladders — hours at the paced rate. This returns names; `aso check` and
`POST /lookup` price the ones worth pricing. Keeping the two apart is what
makes discovery cheap enough to run on a whim.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .clients.hints import HintsClient, UnknownStorefront, storefront_header
from .config import settings
from .http import FetchError, Fetcher
from .scoring import search
from .store import Store, normalize_keyword

Probe = Callable[[str], Awaitable[list[str]]]

# A probe failure that describes one rung rather than a broken run. Mirrors
# `pipeline.RECOVERABLE`: ValueError covers an unparseable plist.
RECOVERABLE = (FetchError, ValueError, UnknownStorefront)


@dataclass(frozen=True)
class Candidate:
    """One term Apple offered, and the strongest claim we saw for it."""

    term: str
    # The DEEPEST (longest) prefix that surfaced this term, and the reason it
    # is that one rather than the shortest.
    #
    # The shortest prefix is the stronger *demand* claim — that is what
    # `SEARCH_DEPTH_WEIGHT` encodes, and it is why the scorer cares about
    # depth. Discovery is asking a different question, and on a prefix ladder
    # the two are in direct opposition: the shallow rungs return whatever is
    # most popular in the whole storefront. Seeded with "habit", prefix "h"
    # offers hinge, hbo max, hulu and home depot — high demand, no relevance.
    #
    # A term still being offered once you have typed five characters is about
    # your seed. One that died at "h" is a popular app sharing a letter.
    prefix: str
    # 1-based position in that prefix's list. Apple orders by demand within a
    # single list; across two different prefixes ranks are not comparable.
    rank: int
    # How many rungs mentioned the term at all. Dedup would otherwise discard
    # this silently, and a term offered at nine rungs is not the same finding
    # as one offered at a single long prefix.
    surfaced_by: int
    tracked: bool


@dataclass(frozen=True)
class SuggestResult:
    keyword: str
    country: str
    candidates: list[Candidate] = field(default_factory=list)
    prefixes_probed: list[str] = field(default_factory=list)
    requests_made: int = 0
    failed: bool = False
    error: str | None = None


@dataclass
class _Seen:
    """A term while it is still being collected, before it is frozen."""

    term: str
    prefix: str
    rank: int
    surfaced_by: int


async def collect(
    keyword: str,
    country: str,
    *,
    probe: Probe,
    include_tracked: bool = False,
    store: Store | None = None,
) -> SuggestResult:
    """Probe every rung of the ladder and gather what Apple offered.

    `probe(prefix)` supplies the suggestion list; everything about how it is
    fetched, cached or rate-limited belongs to the caller — the same contract
    `search.observe` uses.

    A probe failure part-way down returns the candidates already collected,
    with `failed` and the error text. Eight rungs of suggestions and an honest
    error beat an exception that discards them.
    """
    keyword = keyword.strip()
    country = country.strip().lower()
    if not keyword:
        raise ValueError("keyword cannot be blank")
    if not country:
        raise ValueError("country cannot be blank")

    normalized = normalize_keyword(keyword)
    lengths = search.prefix_lengths(normalized)

    live = store if store is not None else Store.load()
    tracked = {
        record["keyword"]
        for record in live.records
        if record["country"] == country
    }

    seen: dict[str, _Seen] = {}
    probed: list[str] = []
    failed = False
    error: str | None = None

    for length in lengths:
        prefix = normalized[:length]
        try:
            terms = await probe(prefix)
        except RECOVERABLE as exc:
            failed, error = True, str(exc)
            break
        probed.append(prefix)

        for rank, term in enumerate(terms, start=1):
            canonical = normalize_keyword(term)
            # Offering back the term that was asked about is not a discovery.
            if not canonical or canonical == normalized:
                continue
            existing = seen.get(canonical)
            if existing is None:
                # Rungs run longest-first, so the FIRST sighting is the deepest
                # one — the strongest relevance claim. Later, shallower
                # sightings only add to the count.
                seen[canonical] = _Seen(term.strip(), prefix, rank, 1)
            else:
                existing.surfaced_by += 1

    candidates = [
        Candidate(
            term=entry.term,
            prefix=entry.prefix,
            rank=entry.rank,
            surfaced_by=entry.surfaced_by,
            tracked=canonical in tracked,
        )
        for canonical, entry in seen.items()
        if include_tracked or canonical not in tracked
    ]
    # Relevance leads, rank breaks ties: deepest prefix first, then Apple's own
    # order within that prefix's list.
    candidates.sort(key=lambda c: (-len(c.prefix), c.rank))

    return SuggestResult(
        keyword=keyword,
        country=country,
        candidates=candidates,
        prefixes_probed=probed,
        requests_made=len(probed),
        failed=failed,
        error=error,
    )


async def suggest_async(
    keyword: str,
    country: str,
    *,
    force: bool = False,
    include_tracked: bool = False,
    fetcher: Fetcher | None = None,
    store: Store | None = None,
) -> SuggestResult:
    """Walk the ladder against Apple. Writes nothing, anywhere.

    `fetcher` lets a long-lived caller — the API — supply the one `Fetcher` its
    process owns, so discovery draws on the same token bucket a refresh is
    using rather than opening a second one against the same IP.

    Costs `min(len(keyword), SEARCH_MAX_PREFIX_QUERIES)` requests, at most 12,
    at roughly four seconds each: about 20s for a five-character keyword and
    48s for a long one. Free on repeat inside the 3-day hints TTL. No chart
    index is built, so unlike `lookup` there is no 48-request first-call-of-
    the-day penalty.
    """
    keyword = keyword.strip()
    country = country.strip().lower()
    if not keyword:
        raise ValueError("keyword cannot be blank")
    if not country:
        raise ValueError("country cannot be blank")
    # Raises `UnknownStorefront` before a single request goes out. Researching
    # the wrong market quietly is worse than an error.
    storefront_header(country)

    async def run(active: Fetcher) -> SuggestResult:
        hints = HintsClient(active, config=settings)

        async def probe(prefix: str) -> list[str]:
            return (await hints.suggest(prefix, country, force=force)).terms

        return await collect(
            keyword,
            country,
            probe=probe,
            include_tracked=include_tracked,
            store=store,
        )

    if fetcher is not None:
        return await run(fetcher)
    async with Fetcher(settings) as owned:
        return await run(owned)


def suggest(
    keyword: str,
    country: str,
    *,
    force: bool = False,
    include_tracked: bool = False,
) -> SuggestResult:
    """Synchronous `suggest_async`, for a caller with no event loop to borrow.

    Runs its own loop via `asyncio.run`, so it must not be called from inside
    one.
    """
    return asyncio.run(
        suggest_async(
            keyword, country, force=force, include_tracked=include_tracked
        )
    )
