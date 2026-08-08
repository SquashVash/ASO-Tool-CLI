"""Search-volume score via the prefix-ladder method.

0-100, higher = more volume.

THIS IS A PROXY. READ THIS BEFORE TRUSTING A NUMBER.
----------------------------------------------------
Nothing here measures search volume. Apple publishes no volume figure for
organic App Store search. What it does publish, implicitly, is an ordering:
autocomplete surfaces high-demand completions first and surfaces them early.

So the method asks two questions:

1. **How little do you have to type before Apple suggests this keyword?**
   A term suggested from two characters in is one a great many people type.
   A term that only appears once you have typed the whole thing is one almost
   nobody searches for.
2. **How high in that suggestion list does it sit?**

`prefix_depth` and `hint_rank` answer those, and `score_from_observations()`
maps the pair onto 0-100. Both raw observations are stored on `snapshots`, so
the mapping can be replaced and history re-scored without re-querying Apple.

The mapping's shape is defensible but its constants are guesses. It is ordinal:
useful for ranking keywords against each other, meaningless as an absolute.
`calibrate()` is where that gets fixed once Apple Search Ads impression data is
available. Until then, do not read "72" as anything but "higher than 60".

A NOTE ON LADDER DIRECTION
--------------------------
The ladder is walked **longest prefix first, descending**, stopping at the
first prefix that fails to surface the keyword. Matching is monotone in
practice — a longer, more specific prefix is likelier to surface the keyword
than a shorter one — so the first failure means nothing shorter can match
either, and the last success is the shortest matching prefix. Walking upward
instead would have to query the entire ladder to find the same answer.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from ..config import (
    SEARCH_DEPTH_WEIGHT,
    SEARCH_MAX_HINT_RANK,
    SEARCH_MAX_PREFIX_QUERIES,
    SEARCH_MIN_PREFIX_LEN,
    SEARCH_NO_MATCH_SCORE,
    SEARCH_RANK_DECAY,
    SEARCH_RANK_WEIGHT,
)

logger = logging.getLogger(__name__)

# An async callable taking a prefix and returning that prefix's suggestions in
# Apple's order. Injected rather than imported so this module stays free of any
# knowledge of HTTP, caching or clients — and so tests need no network.
Probe = Callable[[str], Awaitable[Sequence[str]]]


@dataclass(frozen=True)
class LadderObservation:
    """What the ladder walk actually saw. Both fields are stored on `snapshots`."""

    # Length of the shortest prefix that still surfaced the keyword.
    # None means the keyword never appeared, not even at its full length.
    prefix_depth: int | None
    # 1-based position in that prefix's suggestion list. None iff no match.
    hint_rank: int | None
    # Length of the keyword the ladder was built from. Not stored — it is
    # recoverable from `keywords.keyword` — but needed to score.
    keyword_length: int
    # How many prefixes were actually queried, for run accounting.
    queries_used: int = 0
    # True when the ladder was sampled rather than walked exhaustively.
    sampled: bool = False


def normalize_keyword(keyword: str) -> str:
    return " ".join(keyword.split()).casefold()


def prefix_lengths(
    keyword: str,
    *,
    min_len: int = SEARCH_MIN_PREFIX_LEN,
    max_queries: int = SEARCH_MAX_PREFIX_QUERIES,
) -> list[int]:
    """Prefix lengths to try, longest first.

    Full length down to `min_len`. When that exceeds `max_queries`, the ladder
    is sampled evenly instead — always keeping both ends, since the full length
    is the match of last resort and the shortest rung is what a top score
    depends on. Sampling coarsens `prefix_depth`: the reported depth is the
    shortest *sampled* prefix that matched, which may overstate the true one.

    A keyword shorter than `min_len` still gets queried at its own length.
    """
    length = len(keyword)
    if length == 0:
        return []
    floor = min(min_len, length)
    descending = list(range(length, floor - 1, -1))
    if len(descending) <= max_queries:
        return descending
    if max_queries <= 1:
        return [length]

    # Even sample across the range, endpoints included.
    step = (len(descending) - 1) / (max_queries - 1)
    picked = sorted({descending[round(i * step)] for i in range(max_queries)}, reverse=True)
    return picked


def score_from_observations(
    prefix_depth: int | None,
    hint_rank: int | None,
    keyword_length: int,
    *,
    min_len: int = SEARCH_MIN_PREFIX_LEN,
) -> float:
    """Map the two raw observations onto 0-100.

    Two parts, weighted by `SEARCH_DEPTH_WEIGHT` / `SEARCH_RANK_WEIGHT`:

    * **depth** — linear in how far below the full keyword the shortest
      matching prefix sat. Matching only at the full string scores 0; matching
      at the shortest rung on the ladder scores 100.
    * **rank** — geometric decay at `SEARCH_RANK_DECAY` per position, so
      rank 1 = 100, rank 2 = 82, rank 3 = 67, and so on.

    A keyword that never appeared scores `SEARCH_NO_MATCH_SCORE` rather than 0.
    It means "below the measurable floor", and a true zero would collapse the
    opportunity ranking, which multiplies by this number.

    Pure and dependency-free by design: this is what re-scores stored history
    after `calibrate()` changes the constants.
    """
    if prefix_depth is None or hint_rank is None:
        return SEARCH_NO_MATCH_SCORE

    floor = min(min_len, keyword_length)
    span = keyword_length - floor
    if span <= 0:
        # The keyword is at or below the shortest rung, so its only prefix is
        # itself. Surfacing at all is the strongest signal available.
        depth_part = 100.0
    else:
        depth_part = 100.0 * (keyword_length - prefix_depth) / span

    capped_rank = min(max(hint_rank, 1), SEARCH_MAX_HINT_RANK)
    rank_part = 100.0 * (SEARCH_RANK_DECAY ** (capped_rank - 1))

    score = SEARCH_DEPTH_WEIGHT * depth_part + SEARCH_RANK_WEIGHT * rank_part
    return max(SEARCH_NO_MATCH_SCORE, min(100.0, score))


def score(observation: LadderObservation) -> float:
    return score_from_observations(
        observation.prefix_depth, observation.hint_rank, observation.keyword_length
    )


async def observe(
    keyword: str,
    probe: Probe,
    *,
    min_len: int = SEARCH_MIN_PREFIX_LEN,
    max_queries: int = SEARCH_MAX_PREFIX_QUERIES,
) -> LadderObservation:
    """Walk the prefix ladder, returning the raw observations.

    `probe(prefix)` supplies the suggestion list; everything about how it is
    fetched, cached or rate-limited belongs to the caller. Exceptions from
    `probe` propagate untouched — a failed fetch must not be recorded as "this
    keyword has no suggestions".

    Walks longest prefix first and stops at the first miss (see module
    docstring). The last hit is the shortest matching prefix.
    """
    normalized = normalize_keyword(keyword)
    lengths = prefix_lengths(normalized, min_len=min_len, max_queries=max_queries)
    if not lengths:
        return LadderObservation(None, None, keyword_length=0)

    sampled = lengths != list(range(len(normalized), min(min_len, len(normalized)) - 1, -1))
    best_depth: int | None = None
    best_rank: int | None = None
    queries = 0

    for length in lengths:
        prefix = normalized[:length]
        terms = await probe(prefix)
        queries += 1
        rank = _rank_of(normalized, terms)
        if rank is None:
            # Monotonicity: nothing shorter can surface it either.
            logger.debug("ladder for %r stopped at prefix %r", normalized, prefix)
            break
        best_depth, best_rank = length, rank

    return LadderObservation(
        prefix_depth=best_depth,
        hint_rank=best_rank,
        keyword_length=len(normalized),
        queries_used=queries,
        sampled=sampled,
    )


def _rank_of(keyword: str, terms: Sequence[str]) -> int | None:
    for index, term in enumerate(terms, start=1):
        if normalize_keyword(term) == keyword:
            return index
    return None


def calibrate(samples: Sequence[tuple[int | None, int | None, int, float]]) -> dict:
    """Fit the depth/rank mapping against real Apple Search Ads impressions.

    Not implemented — it needs data this tool cannot get yet. Phase 2, once
    `clients/asa.py` can pull `POST /v5/reports/campaigns/{id}/searchterms`.

    `samples` is `(prefix_depth, hint_rank, keyword_length, impressions)` per
    keyword, joining stored `snapshots` observations to measured ASA volume.
    The intended shape of the fit:

    1. Regress log(impressions) on the depth ratio and the rank position.
    2. Solve for `SEARCH_DEPTH_WEIGHT`, `SEARCH_RANK_WEIGHT` and
       `SEARCH_RANK_DECAY` that best reproduce the observed ordering.
    3. Return them as a dict for `config.py`, then re-score every stored
       snapshot through `score_from_observations()` — no refetching, because
       `search_prefix_depth` and `search_hint_rank` are already on every row.

    Until this runs, every search score in the database is an unvalidated
    proxy and should be read as ordinal only.
    """
    raise NotImplementedError(
        "Search-score calibration needs Apple Search Ads impression data. "
        "Implement clients/asa.py first, then fit against stored "
        "search_prefix_depth / search_hint_rank observations."
    )
