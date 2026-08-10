"""Ad-hoc keyword lookup: score a keyword without tracking it.

The same thing `aso check` does, packaged for a caller that cannot use asyncio
directly — which in practice means Streamlit, whose script model reruns the
whole module on every widget interaction and offers no event loop of its own.

**Why this is a module and not four lines inside `dashboard.py`.** The
dashboard was built strictly read-only, and one of the two reasons was that a
dashboard which fires rate-limited requests on every rerun is a good way to get
403ed mid-refresh. Adding a lookup screen means giving it network access, so the
protection has to move rather than disappear: the network call lives here, is
synchronous and explicit, and the dashboard may only reach it from a submitted
form whose result it caches. Keeping it out of the presentation module is what
makes that rule testable at all.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass

from . import pipeline, repository
from .clients.hints import HintsClient
from .clients.itunes import ITunesClient
from .config import settings
from .db import session
from .http import Fetcher


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
    conn: sqlite3.Connection, score: float | None
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
    rows = repository.latest_scores(conn, sort="opportunity", include_unscored=False)
    values = [
        row["opportunity_score"]
        for row in rows
        if row["opportunity_score"] is not None
    ]
    if not values:
        return None, 0
    beaten = sum(1 for value in values if value < score)
    return 100.0 * beaten / len(values), len(values)


def lookup(keyword: str, country: str, *, force: bool = False) -> LookupResult:
    """Score `keyword` live. Writes no keyword, no snapshot, no SERP.

    The HTTP response cache *is* written, because that is a cache rather than a
    record — it makes repeating a lookup free, which matters when the caller is
    a UI someone will click twice.

    Runs its own event loop via `asyncio.run`, so it must not be called from
    inside one. That is the whole point: Streamlit has no loop to borrow.
    """
    keyword = keyword.strip()
    country = country.strip().lower()
    if not keyword:
        raise ValueError("keyword cannot be blank")
    if not country:
        raise ValueError("country cannot be blank")

    async def run() -> tuple[pipeline.ScoredKeyword, int]:
        async with Fetcher(settings) as fetcher:
            with session() as conn:
                itunes = ITunesClient(fetcher, conn, settings)
                hints = HintsClient(fetcher, conn, settings)
                scored = await pipeline.score_keyword(
                    keyword, country, itunes=itunes, hints=hints, force=force
                )
                # An ad-hoc check must report the same demand number a tracked
                # keyword would. `score_keyword` cannot do this itself — it is
                # deliberately table-free — so the blend happens here, where
                # there is a connection.
                pipeline.blend_outcome(conn, scored.outcome)
            return scored, fetcher.requests_made

    scored, requests_made = asyncio.run(run())

    with session() as conn:
        existing = repository.get_keyword(conn, keyword, country)
        percentile, compared = opportunity_percentile(
            conn, scored.outcome.opportunity_score
        )

    return LookupResult(
        scored=scored,
        requests_made=requests_made,
        tracked=existing is not None,
        percentile=percentile,
        compared_against=compared,
    )
