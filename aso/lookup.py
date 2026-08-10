"""Ad-hoc keyword lookup: score a keyword without tracking it.

The same thing `aso check` does, packaged for two callers. `lookup_async` is
the core; the API awaits it directly, so it can hand over the one `Fetcher` its
process owns. `lookup` wraps it in `asyncio.run` for a caller that cannot use
asyncio directly.

**Why this is a module and not four lines inside the API route.** The scoring
call is rate-limited against Apple and must stay callable from a caller with no
event loop of its own, so it lives here rather than in whichever presentation
layer happens to want it. `lookup` is the synchronous wrapper for that caller;
`lookup_async` is what the API awaits.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from . import pipeline
from .clients.charts import ChartIndex
from .clients.hints import HintsClient
from .clients.itunes import ITunesClient
from .config import settings
from .http import Fetcher
from .store import Store


@dataclass(frozen=True)
class LookupResult:
    """A scored keyword plus the context needed to judge the number."""

    scored: pipeline.ScoredKeyword
    requests_made: int
    tracked: bool
    # Where this opportunity score falls among the keywords already tracked,
    # 0-100. None when nothing is tracked yet, or when scoring failed — an
    # unscored keyword has no rank, and 0 would read as "the worst one".
    percentile: float | None
    compared_against: int

    @property
    def outcome(self) -> pipeline.KeywordOutcome:
        return self.scored.outcome


def opportunity_percentile(
    store: Store, score: float | None
) -> tuple[float | None, int]:
    """What fraction of tracked keywords this score beats, as 0-100.

    "Opportunity 34" means nothing on its own — the scale is ordinal and its
    absolute values shift every time the mapping is recalibrated. Against your
    own tracked set it means something checkable: better than 80% of what you
    are already watching, or worse than half of it.

    Returns `(percentile, n_compared)`. `n_compared` is part of the answer, not
    diagnostics: a percentile over four keywords is not a percentile.
    """
    if score is None:
        return None, 0
    rows = store.latest_scores(sort="opportunity", include_unscored=False)
    values = [
        row["opportunity_score"]
        for row in rows
        if row["opportunity_score"] is not None
    ]
    if not values:
        return None, 0
    beaten = sum(1 for value in values if value < score)
    return 100.0 * beaten / len(values), len(values)


async def lookup_async(
    keyword: str,
    country: str,
    *,
    force: bool = False,
    fetcher: Fetcher | None = None,
    charts: ChartIndex | None = None,
) -> LookupResult:
    """Score `keyword` live. Writes no keyword, no snapshot, no SERP.

    The HTTP response cache *is* written, because that is a cache rather than a
    record — it makes repeating a lookup free, which matters when the caller is
    a UI someone will click twice.

    `fetcher` lets a long-lived caller — the API — supply the one `Fetcher` its
    process owns, so an ad-hoc lookup draws on the same token bucket a refresh
    is using rather than opening a second one against the same IP.

    `charts` is what makes the resulting competition score comparable to a
    stored one. Without it `comp_app_power` is None and `combine()`
    renormalizes over the rest — and that component carries 0.625 of the fitted
    weight, more than every other combined. `aso check` declines to pay 48
    requests for a one-off; a server that holds the index all day should not.
    """
    keyword = keyword.strip()
    country = country.strip().lower()
    if not keyword:
        raise ValueError("keyword cannot be blank")
    if not country:
        raise ValueError("country cannot be blank")

    async def run(active: Fetcher) -> tuple[pipeline.ScoredKeyword, int]:
        # A supplied fetcher is long-lived, so its counter is a running total
        # for the process. `requests_made` on the result means this lookup.
        before = active.requests_made
        itunes = ITunesClient(active, config=settings)
        hints = HintsClient(active, config=settings)
        scored = await pipeline.score_keyword(
            keyword,
            country,
            itunes=itunes,
            hints=hints,
            force=force,
            charts=charts,
        )
        # An ad-hoc check must report the same numbers a tracked keyword
        # would, on both axes. `score_keyword` cannot do this itself — it
        # reads no stored data by design — so both adjustments happen here.
        pipeline.bridge_outcome(scored.outcome)
        pipeline.blend_outcome(scored.outcome)
        return scored, active.requests_made - before

    if fetcher is not None:
        scored, requests_made = await run(fetcher)
    else:
        async with Fetcher(settings) as owned:
            scored, requests_made = await run(owned)

    store = Store.load()
    existing = store.get_keyword(keyword, country)
    percentile, compared = opportunity_percentile(
        store, scored.outcome.opportunity_score
    )

    return LookupResult(
        scored=scored,
        requests_made=requests_made,
        tracked=existing is not None,
        percentile=percentile,
        compared_against=compared,
    )


def lookup(keyword: str, country: str, *, force: bool = False) -> LookupResult:
    """Synchronous `lookup_async`, for a caller with no event loop to borrow.

    Runs its own loop via `asyncio.run`, so it must not be called from inside
    one.
    """
    return asyncio.run(lookup_async(keyword, country, force=force))
