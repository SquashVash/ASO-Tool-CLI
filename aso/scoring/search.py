"""Demand score: how much traffic a keyword plausibly carries.

0-100, higher = more demand.

THIS IS A PROXY. READ THIS BEFORE TRUSTING A NUMBER.
----------------------------------------------------
Nothing here measures search volume. Apple publishes no volume figure for
organic App Store search. What this module does is combine three independent
observations that each correlate with demand, and the result is ordinal:
useful for ranking keywords against each other, meaningless as an absolute.

THREE INSTRUMENTS, NOT ONE
--------------------------
1. **Prefix depth** — how little you have to type before Apple suggests the
   keyword. A term offered from three characters in is one a lot of people
   type. Measured by walking the prefix ladder (see `observe`).
2. **Extensions** — how many of the suggestions for the keyword's own full
   length prefix extend it. Apple offers completions and never echoes the
   query back, so a keyword that is a *stem* of popular longer terms is
   invisible to the ladder no matter how much demand it has. `insta` measures
   75/100 at AppFigures, appears at no rung of its own ladder, and has ten
   suggestions extending it.
3. **Rating mass** — the review mass of the apps holding the SERP's top 10,
   read off the stored `comp_rating_count`. Apple ranks by relevance against
   popularity, so the incumbents of a query are an equilibrium outcome:
   queries worth winning get won by big apps.

The first two come from autocomplete and fail together. The third comes from
the SERP and is nearly uncorrelated with them (0.17), which is why the
combination beats any of them alone. Measured against 182 keywords with
AppFigures popularity: depth 0.478, extensions 0.582, rating mass 0.660,
combined 0.779 — 0.718 cross-validated.

All three are stored on `snapshots`, so the mapping can be replaced and
history re-scored without re-querying Apple. `score_from_observations` drops
whatever is missing and renormalizes, so one instrument failing degrades the
score rather than voiding it.

WHAT THIS STILL CANNOT TELL YOU
-------------------------------
Autocomplete ranks completions *within a prefix* and says nothing about how
contested that prefix is: "75 hard" wins a rare prefix at rank 1 and reads
identically to instagram. That saturation was once fatal, because depth
carried the whole score. Rating mass fixes it from the other side — the two
keywords have wildly different incumbents — but the SERP instrument has its
own failure, the mirror image: a low-volume query inside a crowded category
inherits the category's giants and scores too high. `finsta` measures 5/100
and scores 92 here.

So read the number as "how much traffic is plausibly behind this query", and
expect it to be wrong in both directions on terms where supply and demand come
apart. Only measured impressions settle it, i.e. `calibrate()` against Apple
Search Ads.

A NOTE ON LADDER DIRECTION
--------------------------
The ladder is walked **longest prefix first, descending**, stopping at the
first prefix that fails to surface the keyword. Matching is monotone in
practice — a longer, more specific prefix is likelier to surface the keyword
than a shorter one — so the first failure means nothing shorter can match
either, and the last success is the shortest matching prefix. Walking upward
instead would have to query the entire ladder to find the same answer. The
first rung is also where `extensions` is read, at no extra cost.
"""

from __future__ import annotations

import dataclasses
import logging
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from ..config import (
    CALIBRATION_DEPTH_DECAY_GRID,
    CALIBRATION_EXTENSIONS_CAP_GRID,
    CALIBRATION_WEIGHT_STEP,
    CALIBRATION_MIN_ORDERABLE_PAIRS,
    CALIBRATION_MIN_DISTINCT_TARGETS,
    CALIBRATION_MIN_SAMPLES,
    CALIBRATION_RANK_DECAY_GRID,
    CALIBRATION_SAVINGS_CAP_GRID,
    SEARCH_DEPTH_DECAY,
    SEARCH_DEPTH_WEIGHT,
    SEARCH_EXTENSIONS_CAP,
    SEARCH_EXTENSIONS_WEIGHT,
    SEARCH_MAX_HINT_RANK,
    SEARCH_RATING_MASS_WEIGHT,
    SEARCH_MAX_PREFIX_QUERIES,
    SEARCH_MIN_PREFIX_LEN,
    SEARCH_NO_MATCH_SCORE,
    SEARCH_RANK_DECAY,
    SEARCH_RANK_WEIGHT,
    SEARCH_SAVINGS_CAP,
    SEARCH_SAVINGS_WEIGHT,
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
    # How many suggestions for the keyword's own full-length prefix extend it.
    # None means the ladder never got that far (an empty keyword); 0 means it
    # looked and nothing extended the term. Free: the full-length prefix is
    # the ladder's first rung, so this reads a list already fetched.
    extensions: int | None = None
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
    extensions: int | None = None,
    rating_mass: float | None = None,
    min_len: int = SEARCH_MIN_PREFIX_LEN,
) -> float:
    """Map the raw observations onto 0-100.

    Five parts, weighted by `SEARCH_DEPTH_WEIGHT` / `SEARCH_EXTENSIONS_WEIGHT` /
    `SEARCH_RATING_MASS_WEIGHT` / `SEARCH_SAVINGS_WEIGHT` /
    `SEARCH_RANK_WEIGHT`:

    * **depth** — geometric decay at `SEARCH_DEPTH_DECAY` per character of the
      shortest matching prefix, in ABSOLUTE characters: depth 1 = 100,
      2 = 85, 3 = 72, 4 = 61. Every extra character the typist has to commit
      before Apple will guess the term costs the same, whatever the term's
      length. Zero when the ladder never surfaced the keyword.
    * **extensions** — how many suggestions for the keyword's own full-length
      prefix extend it, linear to `SEARCH_EXTENSIONS_CAP`. See `observe`.
    * **rating mass** — the SERP's `comp_rating_count` component, i.e. the
      review mass of the top 10. Already 0-100 and already stored, so it costs
      nothing extra. See the note in `config.py` for why supply measures
      demand.
    * **savings** — characters the completion saved (`keyword_length - depth`),
      linear to a cap of `SEARCH_SAVINGS_CAP`. Currently weighted 0.
    * **rank** — geometric decay at `SEARCH_RANK_DECAY` per position.
      Currently weighted 0.

    Missing inputs are dropped and the surviving weights renormalized, exactly
    as `competition.combine` does. That matters because the two instruments
    fail independently: a hints timeout must not drag a keyword's demand to
    the floor when its SERP was captured cleanly, and vice versa. It is also
    what lets snapshots taken before `search_hint_extensions` existed keep
    scoring — they simply score on what they measured.

    Note the distinction the caller must preserve: `extensions=None` means
    "not measured", while `extensions=0` means "measured, nothing extends it".
    The first is dropped from the average; the second is a real zero.

    Why depth is absolute and not length-relative
    ---------------------------------------------
    This term used to be `(keyword_length - depth) / (keyword_length - min_len)`
    — a *ratio*. That made the ceiling trivially reachable for short keywords
    (a 7-character term needed to survive 5 rungs) and near-unreachable for
    long ones (a 21-character phrase needed 19), so keywords were rewarded for
    being short rather than for being in demand. Measured against terms whose
    relative volume is not in dispute, it put "75 hard" at exactly 100.0,
    tied with instagram, youtube, spotify and netflix. The absolute form plus
    the separate, bounded savings term keeps the "long phrase completed early"
    signal without letting brevity buy the ceiling.

    A keyword with nothing measurable at all scores `SEARCH_NO_MATCH_SCORE`
    rather than 0. It means "below the measurable floor", and a true zero would
    collapse the opportunity ranking, which multiplies by this number.

    Pure and dependency-free by design: this is what re-scores stored history
    after `calibrate()` changes the constants. `min_len` is accepted for
    signature stability and no longer affects the result — the mapping is
    absolute, so it does not care where the ladder was allowed to stop.
    """
    parts: list[tuple[float, float]] = []  # (weight, 0-100 component)

    if prefix_depth is not None and hint_rank is not None:
        effective_depth = max(1, min(prefix_depth, keyword_length))
        parts.append(
            (SEARCH_DEPTH_WEIGHT, 100.0 * (SEARCH_DEPTH_DECAY ** (effective_depth - 1)))
        )

        saved = max(0, keyword_length - effective_depth)
        parts.append(
            (SEARCH_SAVINGS_WEIGHT, 100.0 * min(saved, SEARCH_SAVINGS_CAP) / SEARCH_SAVINGS_CAP)
        )

        capped_rank = min(max(hint_rank, 1), SEARCH_MAX_HINT_RANK)
        parts.append(
            (SEARCH_RANK_WEIGHT, 100.0 * (SEARCH_RANK_DECAY ** (capped_rank - 1)))
        )
    else:
        # Measured and absent, not unmeasured: the ladder ran and found
        # nothing. That is a real observation of low demand, so these
        # components score 0 and still carry their weight.
        parts.append((SEARCH_DEPTH_WEIGHT, 0.0))
        parts.append((SEARCH_SAVINGS_WEIGHT, 0.0))
        parts.append((SEARCH_RANK_WEIGHT, 0.0))

    if extensions is not None:
        capped = min(max(extensions, 0), SEARCH_EXTENSIONS_CAP)
        parts.append((SEARCH_EXTENSIONS_WEIGHT, 100.0 * capped / SEARCH_EXTENSIONS_CAP))

    if rating_mass is not None:
        parts.append((SEARCH_RATING_MASS_WEIGHT, max(0.0, min(100.0, rating_mass))))

    total_weight = sum(weight for weight, _ in parts if weight > 0)
    if total_weight == 0:
        return SEARCH_NO_MATCH_SCORE
    score = sum(weight * value for weight, value in parts if weight > 0) / total_weight
    return max(SEARCH_NO_MATCH_SCORE, min(100.0, score))


def score(
    observation: LadderObservation, *, rating_mass: float | None = None
) -> float:
    """Score a ladder walk. `rating_mass` is the SERP's `comp_rating_count`.

    Passing `rating_mass=None` scores the keyword on autocomplete alone, which
    is what happens when the SERP fetch failed. That is a genuine degradation
    — rating mass carries most of the weight — but a partial answer beats
    refusing to score a keyword whose ladder was captured perfectly well.
    """
    return score_from_observations(
        observation.prefix_depth,
        observation.hint_rank,
        observation.keyword_length,
        extensions=observation.extensions,
        rating_mass=rating_mass,
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
    extensions: int | None = None
    queries = 0

    for length in lengths:
        prefix = normalized[:length]
        terms = await probe(prefix)
        queries += 1
        if length == len(normalized):
            # The first rung is the keyword itself, so this list is exactly
            # "what Apple suggests to someone who has typed the whole term".
            # Counting the completions of it costs no request and is the only
            # signal available for keywords no prefix ever surfaces.
            extensions = _extensions_of(normalized, terms)
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
        extensions=extensions,
        queries_used=queries,
        sampled=sampled,
    )


def _rank_of(keyword: str, terms: Sequence[str]) -> int | None:
    for index, term in enumerate(terms, start=1):
        if normalize_keyword(term) == keyword:
            return index
    return None


def _extensions_of(keyword: str, terms: Sequence[str]) -> int:
    """How many suggestions extend `keyword` without equalling it.

    Mirrors `clients.hints.HintList.extensions_of`, kept here so this module
    stays free of any client import — `observe` receives bare term lists.
    """
    if not keyword:
        return 0
    return sum(
        1
        for term in terms
        if (normalized := normalize_keyword(term)) != keyword
        and normalized.startswith(keyword)
    )


@dataclass(frozen=True)
class Calibration:
    """A fitted parameter set, plus enough evidence to judge whether to use it."""

    depth_decay: float
    rank_decay: float
    savings_cap: int
    extensions_cap: int
    depth_weight: float
    savings_weight: float
    rank_weight: float
    extensions_weight: float
    rating_mass_weight: float

    n_samples: int
    rmse: float
    spearman: float
    # Spearman of the *current* uncalibrated constants over the same samples,
    # so "did this actually help?" is answerable rather than assumed.
    baseline_spearman: float
    # Constants that landed on the edge of their search grid. An edge means the
    # best value is probably OUTSIDE the range searched, so the fit is a
    # boundary artefact rather than an optimum — worth saying out loud.
    at_grid_edge: tuple[str, ...] = ()
    # Out-of-sample Spearman, k-fold cross-validated: fit on part of the data,
    # scored on the part it never saw. `spearman` above is in-sample and is
    # chosen as the best of tens of thousands of grid points, so it flatters
    # itself; this is
    # the number to actually believe. None when there were too few samples
    # to fold.
    cv_spearman: float | None = None
    # How many distinct values the target took. Two keywords cannot be ordered
    # by a target that gives them the same number.
    distinct_targets: int = 0

    def as_config(self) -> dict[str, float | int]:
        """The constants to paste into `config.py`, named exactly as they are there."""
        return {
            "SEARCH_DEPTH_DECAY": round(self.depth_decay, 4),
            "SEARCH_RANK_DECAY": round(self.rank_decay, 4),
            "SEARCH_SAVINGS_CAP": self.savings_cap,
            "SEARCH_EXTENSIONS_CAP": self.extensions_cap,
            "SEARCH_DEPTH_WEIGHT": round(self.depth_weight, 4),
            "SEARCH_SAVINGS_WEIGHT": round(self.savings_weight, 4),
            "SEARCH_RANK_WEIGHT": round(self.rank_weight, 4),
            "SEARCH_EXTENSIONS_WEIGHT": round(self.extensions_weight, 4),
            "SEARCH_RATING_MASS_WEIGHT": round(self.rating_mass_weight, 4),
        }

    @property
    def improved(self) -> bool:
        """Whether adopting this fit is actually justified.

        Compared on the **cross-validated** correlation when there is one. The
        in-sample number is the best of every grid point measured on its own
        training data, while `baseline_spearman` involves no fitting at all and
        is therefore already out-of-sample — so comparing the two would be
        rigged in the fit's favour. On the first real run it was: 0.320
        in-sample against a 0.169 baseline looked like a decisive win, and the
        honest out-of-sample figure was 0.185.
        """
        achieved = self.spearman if self.cv_spearman is None else self.cv_spearman
        return achieved > self.baseline_spearman

    @property
    def margin(self) -> float:
        """How much better, on the fairest comparison available."""
        achieved = self.spearman if self.cv_spearman is None else self.cv_spearman
        return achieved - self.baseline_spearman


# How a source's raw value maps onto the 0-100 the score lives on.
SCALE_COUNT = "count"  # unbounded, log-distributed (ASA impressions)
SCALE_ORDINAL_100 = "ordinal_100"  # already 0-100 (AppFigures popularity)
SCALES = frozenset({SCALE_COUNT, SCALE_ORDINAL_100})


@dataclass(frozen=True)
class CalibrationSample:
    # None means "the ladder ran and never surfaced the keyword" — a real
    # observation of low demand, not a missing one. Failed fetches are filtered
    # out upstream by `repository.calibration_samples`, so None here is always
    # measured absence.
    prefix_depth: int | None
    hint_rank: int | None
    keyword_length: int
    # Measured demand. Interpretation depends on `scale`.
    impressions: float
    scale: str = SCALE_COUNT
    # Suggestions extending the keyword, and the SERP's review-mass component.
    # Both default to 0.0 rather than None: a sample is something to fit
    # against, and a missing feature there is a zero row, not a renormalization
    # — otherwise different samples would be fitted on different feature sets
    # and their weights would not be comparable.
    extensions: int = 0
    rating_mass: float = 0.0


class NotEnoughData(ValueError):
    """Too few measured keywords to fit against. Fitting anyway would be fiction."""


class DegenerateSample(NotEnoughData):
    """Enough rows, but the target barely varies, so there is no ordering to fit."""


def _components(
    sample: CalibrationSample,
    depth_decay: float,
    rank_decay: float,
    savings_cap: int,
    extensions_cap: int = SEARCH_EXTENSIONS_CAP,
) -> tuple[float, ...]:
    """The five 0-100 components, in `COMPONENT_NAMES` order.

    Mirrors `score_from_observations`, including its treatment of a keyword the
    ladder never surfaced: those three components are 0, not absent. Keeping
    the two in step matters more than it looks — this one decides the weights
    and that one applies them, so any divergence fits constants for a mapping
    that is never actually used.
    """
    if sample.prefix_depth is None or sample.hint_rank is None:
        depth_part = savings_part = rank_part = 0.0
    else:
        depth = max(1, min(sample.prefix_depth, sample.keyword_length))
        depth_part = 100.0 * (depth_decay ** (depth - 1))
        saved = max(0, sample.keyword_length - depth)
        savings_part = 100.0 * min(saved, savings_cap) / savings_cap
        rank = min(max(sample.hint_rank, 1), SEARCH_MAX_HINT_RANK)
        rank_part = 100.0 * (rank_decay ** (rank - 1))
    capped = min(max(sample.extensions, 0), extensions_cap)
    extensions_part = 100.0 * capped / extensions_cap
    rating_part = max(0.0, min(100.0, sample.rating_mass))
    return depth_part, savings_part, rank_part, extensions_part, rating_part


def _targets(samples: Sequence[CalibrationSample]) -> list[float]:
    """Map measured demand onto the 0-100 the score lives on.

    Two source shapes, handled differently, and getting this wrong silently
    corrupts the fit:

    * **`count`** (ASA impressions) — unbounded and roughly log-distributed,
      so `log10` first. A linear target would let three head terms dictate the
      entire fit.
    * **`ordinal_100`** (AppFigures popularity) — already a bounded 0-100
      ranking. Taking its log would compress the top of the scale and *distort
      the very ordering being fitted*, which is the only thing this target is
      good for.

    Either way, min-max onto 0-100 at the end: the score's absolute scale is
    arbitrary, and claiming a fixed demand-per-point rate would be a precision
    neither source supports.
    """
    scales = {s.scale for s in samples}
    if len(scales) > 1:
        raise ValueError(
            f"Cannot fit a mix of demand scales {sorted(scales)} in one "
            "calibration — the sources are not comparable. Calibrate one at a time."
        )

    if scales == {SCALE_ORDINAL_100}:
        values = [float(s.impressions) for s in samples]
    else:
        values = [math.log10(s.impressions + 1.0) for s in samples]

    low, high = min(values), max(values)
    if high - low < 1e-9:
        return [50.0] * len(values)
    return [100.0 * (value - low) / (high - low) for value in values]


def _solve_weights(
    features: Sequence[tuple[float, ...]], targets: Sequence[float]
) -> tuple[float, ...]:
    """Least-squares weights for the components, forced onto the simplex.

    Solves the normal equations by Gaussian elimination — no numpy, and at
    this size an exact solve is a dozen lines. The unconstrained solution can
    put a component negative, which would mean "appearing at a shorter prefix
    makes a keyword *less* popular"; that is never a conclusion worth drawing
    from noisy data, so negatives are clamped to zero and the rest renormalized
    to sum to 1. Clamp-then-renormalize is not the true constrained optimum,
    but it is close, it always lands in bounds, and it is legible — which the
    active-set alternative is not.
    """
    size = len(features[0]) if features else len(COMPONENT_NAMES)
    ata = [[0.0] * size for _ in range(size)]
    atb = [0.0] * size
    for row, target in zip(features, targets):
        for i in range(size):
            atb[i] += row[i] * target
            for j in range(size):
                ata[i][j] += row[i] * row[j]

    # Ridge term: without it, a sample set where every keyword shares a rank
    # makes the matrix singular.
    for i in range(size):
        ata[i][i] += 1e-6

    matrix = [ata[i][:] + [atb[i]] for i in range(size)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda r: abs(matrix[r][col]))
        if abs(matrix[pivot][col]) < 1e-12:
            # Singular even with the ridge — fall back to the constants in
            # force. Must be the full COMPONENT_NAMES-length tuple: returning a
            # short one here raised IndexError in `calibrate` the moment two
            # components were added, because every caller unpacks by position.
            return CURRENT_WEIGHTS
        matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
        for row in range(size):
            if row == col:
                continue
            factor = matrix[row][col] / matrix[col][col]
            for k in range(col, size + 1):
                matrix[row][k] -= factor * matrix[col][k]

    weights = [max(0.0, matrix[i][size] / matrix[i][i]) for i in range(size)]
    total = sum(weights)
    if total <= 0:
        return CURRENT_WEIGHTS
    return tuple(weight / total for weight in weights)


# The components `_components` returns, in order. Everything downstream —
# weight tuples, the simplex, the fitted `Calibration` — is indexed by this.
COMPONENT_NAMES = ("depth", "savings", "rank", "extensions", "rating_mass")

# The weights currently in force, in `COMPONENT_NAMES` order. Used as the
# calibration baseline and as the fallback when the normal equations are
# singular.
CURRENT_WEIGHTS: tuple[float, ...] = (
    SEARCH_DEPTH_WEIGHT,
    SEARCH_SAVINGS_WEIGHT,
    SEARCH_RANK_WEIGHT,
    SEARCH_EXTENSIONS_WEIGHT,
    SEARCH_RATING_MASS_WEIGHT,
)


def _simplex_grid(step: float, dims: int = len(COMPONENT_NAMES)) -> tuple[tuple[float, ...], ...]:
    """Every weight tuple on the `dims`-simplex, at `step`.

    Five components at step 0.1 is 1001 tuples (compositions of 10 into 5
    parts). That is the reason `CALIBRATION_DEPTH_DECAY_GRID` and friends were
    thinned when rating mass was added: the product of the two is what
    `calibrate` actually walks, and it grew by ~15x from the three-component
    version.
    """
    ticks = int(round(1.0 / step))

    def compositions(remaining: int, parts: int) -> list[tuple[int, ...]]:
        if parts == 1:
            return [(remaining,)]
        return [
            (head,) + tail
            for head in range(remaining + 1)
            for tail in compositions(remaining - head, parts - 1)
        ]

    return tuple(
        tuple(value / ticks for value in combo) for combo in compositions(ticks, dims)
    )


_WEIGHT_CANDIDATES = _simplex_grid(CALIBRATION_WEIGHT_STEP)


def _best_weights(
    features: Sequence[tuple[float, ...]],
    targets: Sequence[float],
    target_ranks: Sequence[float] | None = None,
) -> tuple[tuple[float, ...], float, float]:
    """Weights maximizing rank correlation. Returns (weights, spearman, rmse).

    Why not just least squares
    --------------------------
    `_solve_weights` minimizes squared error against the *values*. But an
    ordinal target — AppFigures popularity, a 5-100 ordinal — has no meaningful
    metric distance between levels, and this fit is graded on Spearman. Those
    are different objectives, and on real data they disagree sharply: least
    squares kept choosing a rank weight near 0.32, while an out-of-fold sweep
    put the optimum at 0.15 and showed rank correlation falling monotonically
    above it. Selecting grid points on Spearman while deriving the weights
    inside each grid point from RMSE optimized the metric only one level deep.

    The least-squares solution is still evaluated as one candidate — it is
    exact, free, and lands off-grid where the simplex cannot reach.
    """
    if target_ranks is None:
        target_ranks = _ranks(targets)
    best_weights = _solve_weights(features, targets)
    best_rho = float("-inf")
    best_rmse = float("inf")

    for weights in (best_weights, *_WEIGHT_CANDIDATES):
        predicted = [
            sum(weight * value for weight, value in zip(weights, f)) for f in features
        ]
        rho = _spearman_against_ranks(predicted, target_ranks)
        rmse = math.sqrt(
            sum((p - t) ** 2 for p, t in zip(predicted, targets)) / len(targets)
        )
        if (rho, -rmse) > (best_rho, -best_rmse):
            best_weights, best_rho, best_rmse = weights, rho, rmse

    return best_weights, best_rho, best_rmse


def spearman(a: Sequence[float], b: Sequence[float]) -> float:
    """Rank correlation, with ties averaged. 0.0 for a degenerate input.

    The right quality metric here: the score claims an *ordering* of keywords,
    not an impressions estimate, so this is the number that says whether the
    claim holds.
    """
    if len(a) < 2:
        return 0.0
    return _spearman_against_ranks(a, _ranks(b))


def _spearman_against_ranks(a: Sequence[float], rank_b: Sequence[float]) -> float:
    """`spearman` with the second vector's ranks precomputed.

    The grid search calls this ~130,000 times against one fixed target vector.
    Ranking that vector every time was half the total sort cost for no reason.
    """
    if len(a) < 2:
        return 0.0
    rank_a = _ranks(a)
    n = len(a)
    mean = (n + 1) / 2
    num = sum((x - mean) * (y - mean) for x, y in zip(rank_a, rank_b))
    den_a = math.sqrt(sum((x - mean) ** 2 for x in rank_a))
    den_b = math.sqrt(sum((y - mean) ** 2 for y in rank_b))
    if den_a < 1e-12 or den_b < 1e-12:
        return 0.0
    return num / (den_a * den_b)


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        stop = index
        while stop + 1 < len(order) and values[order[stop + 1]] == values[order[index]]:
            stop += 1
        average = (index + stop) / 2 + 1
        for position in range(index, stop + 1):
            ranks[order[position]] = average
        index = stop + 1
    return ranks


def calibrate(
    samples: Sequence[CalibrationSample],
    *,
    min_samples: int = CALIBRATION_MIN_SAMPLES,
) -> Calibration:
    """Fit the mapping's constants against measured Apple Search Ads impressions.

    This is the step that turns the autocomplete proxy from "an ordering I find
    plausible" into "an ordering that reproduces demand I actually measured".

    How it fits
    -----------
    The two decay constants and the savings cap enter the score non-linearly,
    so they are searched over a coarse grid (`CALIBRATION_*_GRID` in config).
    For each grid point the three *weights* enter linearly, so they are solved
    exactly by least squares against min-max-normalized log impressions. The
    grid point with the lowest RMSE wins. Coarse on purpose: the data is noisy
    and a finer grid would be false precision.

    What it will not do
    -------------------
    Fit fewer than `min_samples` keywords. Six free parameters against a dozen
    points reproduces the sample and predicts nothing, and a confidently wrong
    scale is worse than an honestly uncalibrated one — the whole reason this
    function existed as a stub rather than a guess.

    It also reports `baseline_spearman`, the current constants' rank
    correlation over the same samples. If the fit does not beat it, the honest
    move is to keep the existing constants and get more data. See
    `Calibration.improved`.

    Applying the result is deliberately manual: print `as_config()`, paste into
    `config.py`, run `aso rescore`. Constants that silently rewrite themselves
    are constants nobody reviews.
    """
    usable = [s for s in samples if s.impressions > 0 and s.keyword_length > 0]
    if len(usable) < min_samples:
        raise NotEnoughData(
            f"Need at least {min_samples} keywords with measured demand to "
            f"calibrate; got {len(usable)}. Import a broader keyword export, "
            "track those keywords, and run `aso refresh`."
        )

    targets = _targets(usable)

    # Rows are not enough; the target has to actually vary. See
    # CALIBRATION_MIN_DISTINCT_TARGETS for how this bit in practice.
    counts: dict[float, int] = {}
    for value in targets:
        counts[round(value, 6)] = counts.get(round(value, 6), 0) + 1
    distinct = len(counts)
    modal_share = max(counts.values()) / len(targets)

    # What actually matters is how many PAIRS carry an ordering, not how many
    # rows share the modal value. Spearman is computed over pairs, and a tied
    # pair contributes nothing to it.
    #
    # The modal share alone was the gate here and it was the wrong statistic.
    # It cannot tell "half the sample sits on the vendor's floor while the
    # other half spans the full range" from "the whole sample is one flat
    # cluster", and those differ enormously in what can be fitted: the first
    # leaves 74% of pairs orderable, the second almost none. It started
    # rejecting good samples the moment keywords autocomplete never surfaces
    # were admitted to calibration, because those cluster on the floor by
    # construction — a guard firing on the fix rather than on the fault.
    #
    # Modal share is still reported: it is the usual *cause* when the pair
    # fraction is bad, and naming it points at the remedy.
    n = len(targets)
    tied_pairs = sum(count * (count - 1) // 2 for count in counts.values())
    total_pairs = n * (n - 1) // 2
    orderable = 1.0 - (tied_pairs / total_pairs if total_pairs else 1.0)

    if distinct < CALIBRATION_MIN_DISTINCT_TARGETS or orderable < CALIBRATION_MIN_ORDERABLE_PAIRS:
        raise DegenerateSample(
            f"The measured demand barely varies: {len(usable)} keywords take only "
            f"{distinct} distinct value(s), {modal_share:.0%} of them share a "
            f"single value, and only {orderable:.0%} of keyword pairs can be "
            "ordered at all. There is almost no ordering here to fit, so any "
            "correlation would be noise.\n"
            "This usually means the export came from one long-tail cluster where "
            "the vendor reports its floor value for everything below its "
            "measurable threshold. Import keywords spanning the whole popularity "
            "range — seed exports from some high-volume terms too, not just "
            "variations on one phrase."
        )

    # The constants currently in force, scored on every component the samples
    # carry. Passing only depth and rank here would have measured a crippled
    # version of the live mapping and flattered any fit against it.
    baseline = spearman(
        [
            score_from_observations(
                s.prefix_depth,
                s.hint_rank,
                s.keyword_length,
                extensions=s.extensions,
                rating_mass=s.rating_mass,
            )
            for s in usable
        ],
        targets,
    )

    found = _search_grid(usable)
    assert found is not None  # `usable` passed the min_samples gate above
    depth_decay, rank_decay, savings_cap, extensions_cap, weights, rho, rmse = found

    best = Calibration(
        depth_decay=depth_decay,
        rank_decay=rank_decay,
        savings_cap=savings_cap,
        extensions_cap=extensions_cap,
        depth_weight=weights[0],
        savings_weight=weights[1],
        rank_weight=weights[2],
        extensions_weight=weights[3],
        rating_mass_weight=weights[4],
        n_samples=len(usable),
        rmse=rmse,
        spearman=rho,
        baseline_spearman=baseline,
        distinct_targets=distinct,
    )

    # A constant pinned to the end of its grid means the optimum is probably
    # outside the range searched, so the "fit" is a boundary artefact.
    #
    # A single-valued grid is skipped. Its one entry is simultaneously the
    # first and last element, so it would warn unconditionally — and the
    # warning would be meaningless, since nothing was searched to be at the
    # edge of. The rank and savings grids were collapsed to one value each
    # when their weights went to zero, and left unguarded they printed a
    # standing boundary warning on every run, which is how real warnings get
    # ignored.
    def at_edge(value: object, grid: Sequence[object]) -> bool:
        return len(grid) > 1 and value in (grid[0], grid[-1])

    edges: list[str] = []
    if at_edge(best.depth_decay, CALIBRATION_DEPTH_DECAY_GRID):
        edges.append("SEARCH_DEPTH_DECAY")
    if at_edge(best.rank_decay, CALIBRATION_RANK_DECAY_GRID):
        edges.append("SEARCH_RANK_DECAY")
    if at_edge(best.savings_cap, CALIBRATION_SAVINGS_CAP_GRID):
        edges.append("SEARCH_SAVINGS_CAP")
    if at_edge(best.extensions_cap, CALIBRATION_EXTENSIONS_CAP_GRID):
        edges.append("SEARCH_EXTENSIONS_CAP")

    return dataclasses.replace(
        best,
        at_grid_edge=tuple(edges),
        cv_spearman=_cross_validated_spearman(usable),
    )


def grid_size() -> int:
    """How many (decay, decay, cap, weights) combinations a fit evaluates.

    Computed rather than written down: the reported count drifted out of date
    the first time the grids were retuned, and a stale "best of ~1800" next to
    a real correlation is the kind of small lie that makes the honest numbers
    beside it harder to trust.
    """
    return (
        len(CALIBRATION_DEPTH_DECAY_GRID)
        * len(CALIBRATION_RANK_DECAY_GRID)
        * len(CALIBRATION_SAVINGS_CAP_GRID)
        * len(CALIBRATION_EXTENSIONS_CAP_GRID)
        * len(_WEIGHT_CANDIDATES)
    )


def _search_grid(
    samples: Sequence[CalibrationSample],
) -> tuple[float, float, int, int, tuple[float, ...], float, float] | None:
    """Best (depth_decay, rank_decay, savings_cap, extensions_cap, weights, rho, rmse).

    Exhaustive over decay grid x weight simplex, on purpose.

    A cheaper two-stage version was tried and removed: rank every decay point
    by its least-squares weights, then re-fit weights only for the top K. It
    was measurably wrong. The winning point on real data (rho 0.514) ranked
    *below* the 400th under least-squares weights, so every K short of the full
    grid returned a worse answer (0.502) while looking successful. That is the
    same class of error as selecting on RMSE and reporting Spearman: a cheap
    proxy standing in for the real objective and quietly changing the result.
    """
    if len(samples) < 4:
        return None
    targets = _targets(samples)
    target_ranks = _ranks(targets)

    best = None
    best_key = (-2.0, float("-inf"))
    for depth_decay in CALIBRATION_DEPTH_DECAY_GRID:
        for rank_decay in CALIBRATION_RANK_DECAY_GRID:
            for savings_cap in CALIBRATION_SAVINGS_CAP_GRID:
                for extensions_cap in CALIBRATION_EXTENSIONS_CAP_GRID:
                    features = [
                        _components(
                            s, depth_decay, rank_decay, savings_cap, extensions_cap
                        )
                        for s in samples
                    ]
                    weights, rho, rmse = _best_weights(features, targets, target_ranks)
                    # Select on Spearman, tie-break on RMSE — at both levels:
                    # which grid point wins, and which weights win inside it.
                    # Selecting on RMSE and reporting Spearman let the first
                    # real run return a fit that scored WORSE than the
                    # constants it was meant to replace.
                    key = (rho, -rmse)
                    if key > best_key:
                        best_key = key
                        best = (
                            depth_decay,
                            rank_decay,
                            savings_cap,
                            extensions_cap,
                            weights,
                            rho,
                            rmse,
                        )
    return best


def _cross_validated_spearman(
    samples: Sequence[CalibrationSample], *, folds: int = 5
) -> float | None:
    """Honest out-of-sample estimate: fit on k-1 folds, score on the held-out one.

    The headline `spearman` is the best of every grid point measured on
    the same data it was chosen with, so it is biased upward — with six free
    parameters and a few dozen samples that bias is not small. This refits from
    scratch per fold and pools the held-out predictions, so nothing scored here
    influenced the constants that scored it.

    Returns None when the sample is too small to fold meaningfully.
    """
    n = len(samples)
    if n < 2 * folds:
        return None

    ordered = sorted(samples, key=lambda s: s.impressions)
    # Stride assignment rather than contiguous blocks: contiguous folds over a
    # value-sorted list would hand each fold a narrow demand band, and rank
    # correlation inside a narrow band is mostly noise.
    buckets: list[list[CalibrationSample]] = [[] for _ in range(folds)]
    for index, sample in enumerate(ordered):
        buckets[index % folds].append(sample)

    pooled_pred: list[float] = []
    pooled_true: list[float] = []
    for fold in range(folds):
        holdout = buckets[fold]
        train = [s for i, b in enumerate(buckets) if i != fold for s in b]
        if len(holdout) < 2 or len(train) < 4:
            continue
        try:
            fitted = _search_grid(train)
            held_targets = _targets(holdout)
        except ValueError:  # pragma: no cover - mixed scales caught earlier
            return None
        if fitted is None:
            continue
        depth_decay, rank_decay, savings_cap, extensions_cap, weights, _, _ = fitted
        for sample, target in zip(holdout, held_targets):
            f = _components(sample, depth_decay, rank_decay, savings_cap, extensions_cap)
            pooled_pred.append(sum(w * v for w, v in zip(weights, f)))
            pooled_true.append(target)

    if len(pooled_pred) < 2:
        return None
    return spearman(pooled_pred, pooled_true)
