"""Competition score: how hard is it to rank for this keyword?

0-100, higher = harder. Normalized components, combined by `combine()` as a
weighted *power* mean — weights from `config.COMPETITION_WEIGHTS`, exponent
from `config.COMPETITION_AGGREGATION_POWER` — because a keyword is as hard as
its hardest barrier, not as hard as its average one.

MEASUREMENTS VS SCORED COMPONENTS
---------------------------------
Every name in `CompetitionComponents` is a column on `snapshots`, but only some
of them are terms in the mean. `config.COMPETITION_WEIGHTS` is the authority on
which — do not trust a weight quoted in a docstring here, including this one.
Components carry weight 0 for three different reasons, and the distinction
matters when reading the code:

* **Inputs to another component.** `comp_rating_count` and `comp_stars` are the
  raw measurements feeding `comp_incumbent`, the geometric mean of the two.
  Storing all three — the inputs and the function of them — is deliberate.
  `derive()` recomputes the function from the inputs on every rescore, so the
  formula stays revisable against history instead of being frozen into whatever
  each row was captured under.
* **Fitted to zero.** Calibration against a vendor index found `comp_stars`,
  `comp_recency`, `comp_breadth` and `comp_incumbent` added nothing once
  publisher concentration and review mass were in. They stay measured and
  stored so a broader sample can overturn that; see `config.py` for what each
  one scored.
* **Not yet priced.** `comp_app_power` is newer than the fit, so the grid has
  never seen it. Weight 0 here means "unevaluated", not "evaluated and
  rejected", and the two must not be confused.

Missing data is treated as *unknown*, never as zero. That distinction runs
through the whole module and is the main thing to preserve when editing it:

* A component that can't be computed at all (no app in the top 10 reports a
  rating, say) is `None`, and `combine()` renormalizes the remaining weights
  rather than scoring it as 0. A keyword we know nothing about must not look
  easy.
* Within a component, apps missing that field are dropped from the median
  rather than counted as zero.
* `averageUserRating` gets a further step: Apple reports **0.0** for apps with
  no ratings. That is an absence of data wearing the costume of a measurement,
  so apps with no ratings are excluded from the stars median entirely. Counting
  them would say "this incumbent is a 0-star app", which is false, and would
  make crowded-but-unrated niches look uncontested.

`score()` computes components from a live SERP; `combine()` turns components
into a final score. They are separate so that re-weighting stored history
re-scores it without re-fetching anything — every component is a column on
`snapshots`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from statistics import median

from ..clients.charts import ChartIndex
from ..clients.itunes import AppRecord, Serp
from ..config import (
    COMP_APP_POWER_CHARTED_FLOOR,
    COMP_APP_POWER_RANK_DECAY,
    COMPETITION_BRIDGE_CEILING,
    COMPETITION_BRIDGE_FLOOR,
    COMPETITION_BRIDGE_MIN_OVERLAP,
    COMP_BREADTH_LOG_DIVISOR,
    COMP_EXACT_MATCH_PARTIAL_CEILING,
    COMP_MAX_STARS,
    COMP_MIN_MEANINGFUL_STARS,
    COMP_PUBLISHER_RANK_DECAY,
    COMP_PUBLISHER_SATURATION_SLOTS,
    COMP_RATING_COUNT_LOG_DIVISOR,
    COMP_RECENCY_MAX_DAYS,
    COMPETITION_AGGREGATION_POWER,
    COMPETITION_TOP_N,
    COMPETITION_WEIGHTS,
)
from .bridge import Bridge, NotEnoughOverlap
from .bridge import fit as bridge_fit

WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class CompetitionComponents:
    """The normalized 0-100 components. `None` means "not computable".

    `comp_rating_count` and `comp_stars` are also the **inputs** to
    `comp_incumbent`, independently of whatever weight each carries in its own
    right. They are measured and stored either way, because they are the
    measurements — `comp_incumbent` is a function of them, and storing only the
    function would make the formula unrevisable after the fact.

    See the module docstring for what a weight of 0 does and does not mean.
    """

    comp_rating_count: float | None = None
    comp_exact_match: float | None = None
    comp_stars: float | None = None
    comp_recency: float | None = None
    comp_publisher: float | None = None
    comp_breadth: float | None = None
    comp_incumbent: float | None = None
    comp_app_power: float | None = None

    def as_dict(self) -> dict[str, float | None]:
        return asdict(self)


@dataclass(frozen=True)
class CompetitionResult:
    score: float | None
    components: CompetitionComponents
    # How many of the top-N apps the components were computed over. A score
    # built on 3 results deserves less trust than one built on 10.
    sample_size: int


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def normalize_text(value: str | None) -> str:
    """Casefold and collapse whitespace, for keyword/title matching."""
    if not value:
        return ""
    return WHITESPACE.sub(" ", value).strip().casefold()


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _median_or_none(values: Iterable[float]) -> float | None:
    collected = list(values)
    return median(collected) if collected else None


def parse_release_date(value: str | None) -> datetime | None:
    """Parse `currentVersionReleaseDate`, tolerating a missing timezone."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def has_real_rating(app: AppRecord) -> bool:
    """Whether this app's star rating is a measurement rather than a placeholder.

    Apple returns `averageUserRating: 0.0` for apps nobody has rated. Only apps
    with at least one rating have a star value worth reading.
    """
    return (
        app.average_user_rating is not None
        and app.user_rating_count is not None
        and app.user_rating_count > 0
    )


# ---------------------------------------------------------------------------
# components
# ---------------------------------------------------------------------------


def rating_count_component(apps: Sequence[AppRecord]) -> float | None:
    """Review mass of the top 10 — also an input to `comp_incumbent`.

    `min(log10(median + 1) / COMP_RATING_COUNT_LOG_DIVISOR, 1) * 100`:
    log-scaled because the gap between 100 and 1,000 ratings matters far more
    than between 100,000 and 101,000. Saturates at a median of 100,000, which
    is about as entrenched as a top 10 gets — see the constant for the
    percentiles that put it there.

    A reported count of 0 is a real observation (an app nobody has rated) and
    is included; a *missing* count is not.
    """
    counts = [
        float(app.user_rating_count)
        for app in apps
        if app.user_rating_count is not None and app.user_rating_count >= 0
    ]
    med = _median_or_none(counts)
    if med is None:
        return None
    return clamp(min(math.log10(med + 1) / COMP_RATING_COUNT_LOG_DIVISOR, 1.0) * 100)


def _targeting_strength(haystack: str, phrase: str, tokens: Sequence[str]) -> float:
    """How squarely one app's text targets a keyword, 0.0-1.0.

    Three tiers, because targeting is not binary:

    * the contiguous phrase is present -> 1.0
    * some tokens are present -> that fraction, scaled by
      `COMP_EXACT_MATCH_PARTIAL_CEILING`, so matching every token in scattered
      order lands just below a true phrase match
    * nothing matches -> 0.0

    Tokens match as substrings, so "journal" counts against "Journaling". That
    is deliberately generous: Apple's own matching is stem-aware, and the
    alternative — exact token equality — would score a competitor named
    "Journaling Daily" as irrelevant to "journal".
    """
    if phrase and phrase in haystack:
        return 1.0
    if not tokens:
        return 0.0
    hits = sum(1 for token in tokens if token in haystack)
    return COMP_EXACT_MATCH_PARTIAL_CEILING * hits / len(tokens)


def exact_match_component(apps: Sequence[AppRecord], keyword: str) -> float | None:
    """How squarely the top 10 targets the keyword.

    Mean `_targeting_strength` across the top 10, on title plus subtitle,
    case- and whitespace-insensitive.

    WHY THIS IS GRADED RATHER THAN A PHRASE COUNT
    ---------------------------------------------
    It used to ask a yes/no question — does the title contain the whole keyword
    phrase? — and answered "no" for every app in 194 of 397 stored snapshots,
    while carrying a quarter of the model's weight. Those SERPs were not
    uncontested. Ten apps ranked in each of them; they simply did not spell the
    query out contiguously, because Apple does not require them to. A component
    that reports 0 for half of all real inputs is not measuring competition, it
    is subtracting a constant from it.

    For a single-word keyword the phrase and the only token coincide, so this
    still returns exactly the old 0-or-1 answer per app. The change bites on
    multi-word keywords, which is where the old version failed.

    WHY A TOTAL BLANK IS UNKNOWN RATHER THAN ZERO
    ---------------------------------------------
    The Search API returns no subtitle and never the keyword field, so this
    reads titles alone. When not one of the ten apps shows any trace of the
    keyword, there are two explanations: nobody targets the term, or everybody
    targets it somewhere this API cannot see. The second is far likelier —
    Apple ranked all ten of them for the query, so it matched them on
    *something*. Returning 0 asserts "no targeting", which was never observed.
    Returning `None` says "not visible from here" and lets `combine()`
    renormalize, per the rule in the module docstring.

    A partial score carries the same blindness, and is left alone: some visible
    targeting is direct evidence, and there is no principled way to guess how
    much more hides in the fields Apple withholds. This corrects only the case
    where the component would otherwise state a fact it cannot support.
    """
    phrase = normalize_text(keyword)
    if not phrase or not apps:
        return None
    tokens = phrase.split()
    strengths = [
        _targeting_strength(
            f"{normalize_text(app.track_name)} {normalize_text(app.subtitle)}",
            phrase,
            tokens,
        )
        for app in apps
    ]
    total = sum(strengths)
    if total == 0.0:
        return None
    return clamp(total / len(strengths) * 100)


def stars_component(apps: Sequence[AppRecord]) -> float | None:
    """Quality of the incumbents — an input to `comp_incumbent`, fitted to weight 0.

    Median star rating over apps that actually have one, mapped from
    `[COMP_MIN_MEANINGFUL_STARS, COMP_MAX_STARS]` onto 0-100 and clamped below.
    Unrated apps are excluded rather than counted as 0 — see the module
    docstring.

    WHY THE FLOOR IS NOT ZERO
    -------------------------
    Dividing by 5 put every real SERP between 91 and 97, which made this a
    constant rather than a measurement, and a constant multiplicand turns
    `comp_incumbent`'s geometric mean back into a plain rescale of review mass.
    Ratings below 4.0 essentially do not occur in a top 10, so the bottom four
    fifths of the old ruler described nothing. Anchoring at 4.0 spends the
    scale on the range that exists, and a genuinely 4.2-star field now reads as
    meaningfully softer than a 4.8-star one instead of 84 versus 96.

    Stored values from before this change are on the old ruler; migration 11
    rescales them arithmetically so history and fresh captures agree.
    """
    ratings = [
        float(app.average_user_rating)  # type: ignore[arg-type]
        for app in apps
        if has_real_rating(app)
    ]
    med = _median_or_none(ratings)
    if med is None:
        return None
    span = COMP_MAX_STARS - COMP_MIN_MEANINGFUL_STARS
    return clamp((med - COMP_MIN_MEANINGFUL_STARS) / span * 100)


def recency_component(
    apps: Sequence[AppRecord], now: datetime | None = None
) -> float | None:
    """How actively the incumbents are maintained. Fitted to weight 0.

    Median days since last update, mapped linearly: updated today -> 100
    (hard, they're paying attention), 365+ days stale -> 0 (soft).

    A future date (clock skew, a scheduled release) clamps to 0 days.
    """
    reference = now or datetime.now(timezone.utc)
    ages = [
        max(0.0, (reference - released).total_seconds() / 86400.0)
        for released in (parse_release_date(a.current_version_release_date) for a in apps)
        if released is not None
    ]
    med = _median_or_none(ages)
    if med is None:
        return None
    return clamp((1.0 - min(med, COMP_RECENCY_MAX_DAYS) / COMP_RECENCY_MAX_DAYS) * 100)


def publisher_component(
    top: Sequence[AppRecord], full_results: Sequence[AppRecord]
) -> float | None:
    """Publisher concentration — the heaviest fitted component.

    For each of the top 10 slots, how much of the *full* result set that slot's
    publisher holds, log-graded and saturating at
    `COMP_PUBLISHER_SATURATION_SLOTS`; averaged with rank weights that decay by
    `COMP_PUBLISHER_RANK_DECAY`. One publisher holding many slots means a space
    that is hard to break into, and holding them near the top means more.

    Apps with no seller name can't be attributed, so they count toward the
    denominator but never the numerator: unknown attribution is not evidence
    of concentration.

    WHY GRADED RATHER THAN A THRESHOLD COUNT
    ----------------------------------------
    It used to ask a yes/no question per slot — does this seller appear at
    least twice anywhere in the results? — which scored a publisher with two
    apps exactly like one with twenty. Measured over 24 live SERPs the two
    versions tie on level and spread but disagree on ordering in precisely the
    cases the distinction exists for: "chess" and "weather" both scored 50
    under the threshold rule, while the graded version separates them (53.7
    against 45.7) because chess's incumbents hold deeper catalogues.

    WHY RANK-WEIGHTED
    -----------------
    The component asks how hard the space is to break into. A publisher owning
    ranks 1-3 is a higher wall than one owning ranks 8-10, and a flat mean
    calls them identical. This is the only place rank enters the model; every
    other component still takes a flat median over the top 10.

    WHY A DIVERSE FIELD IS 0 AND NOT `None`
    ---------------------------------------
    The rest of this module returns `None` for what it could not observe, and
    this component deliberately does not. Ten distinct publishers with no other
    apps in a 200-result field is a *measurement* — we looked at the whole
    field and found no concentration — not an absence of one. Contrast
    `exact_match_component`, which returns `None` for a total blank precisely
    because Apple hides the fields it would need to see targeting.

    That 0 is also no longer the common case. It was, at
    `ITUNES_SEARCH_LIMIT = 50`: reading concentration off the first quarter of
    the field missed most repeats. Over 24 live 200-result SERPs the component
    is 0 for 5 of them; over the same terms at limit 50 it was 0 for most.
    Widening the request fixed the frequency of the zero without having to
    reinterpret its meaning.
    """
    if not top:
        return None
    appearances: dict[str, int] = {}
    for app in full_results:
        seller = normalize_text(app.seller_name)
        if seller:
            appearances[seller] = appearances.get(seller, 0) + 1

    saturation = math.log(COMP_PUBLISHER_SATURATION_SLOTS)
    weighted = 0.0
    total_weight = 0.0
    for position, app in enumerate(top):
        weight = 1.0 / (1.0 + COMP_PUBLISHER_RANK_DECAY * position)
        seller = normalize_text(app.seller_name)
        slots = appearances.get(seller, 0) if seller else 0
        # log(1) is 0, so a publisher holding a single slot contributes
        # nothing — the same answer the threshold rule gave, reached by a
        # formula that keeps grading above it.
        strength = min(math.log(slots) / saturation, 1.0) if slots >= 1 else 0.0
        weighted += weight * max(0.0, strength) * 100
        total_weight += weight
    return clamp(weighted / total_weight)


def incumbent_component(
    rating_mass: float | None, stars: float | None
) -> float | None:
    """Strength of the apps already holding the top 10. Fitted to weight 0.

    `sqrt(rating_mass * stars)` — the geometric mean of review mass and star
    rating, both already normalized to 0-100.

    Why a product and not a sum
    ---------------------------
    Outranking an incumbent is hard when it is *both* well reviewed and heavily
    reviewed, and those two facts are not independently additive. A weighted
    sum gives a five-star app with twelve ratings most of the credit that a
    five-star app with two million ratings gets, which is exactly backwards:
    the first is a niche app nobody has heard of and the second is the reason
    the keyword is unwinnable. The product refuses to score high unless both
    hold.

    Geometric rather than plain product because the plain product
    (`a * b / 100`) compresses everything downward — a genuinely strong 4.5-star
    5,000-review incumbent lands near 56 — which wastes the top of the scale.
    The geometric mean keeps the component on the same 0-100 ruler as its
    inputs while preserving the same ordering.

    Returns `None` if *either* input is missing. This component asserts an
    interaction between two measurements, and one measurement is not a weaker
    version of that claim — it is a different claim. `combine()` renormalizes
    around it, so a SERP where no app carries a rating is scored on the other
    four components rather than on a guess. Note the contrast with a measured
    zero, which is real and scores 0.
    """
    if rating_mass is None or stars is None:
        return None
    return clamp(math.sqrt(clamp(rating_mass) * clamp(stars)))


def app_power_component(
    apps: Sequence[AppRecord], charts: ChartIndex | None
) -> float | None:
    """How big the incumbents are, from Apple's published top charts.

    Per top-10 slot: `0` if the app charts nowhere, else
    `FLOOR + (1 - FLOOR) * (1 - log(rank) / log(depth + 1))` where `rank` is
    its best position across every chart pulled. Averaged with the same rank
    decay `publisher_component` uses.

    WHY THE MODEL NEEDED THIS
    -------------------------
    Every other component reads the *text and reputation* around a keyword —
    who targets it, how many ratings they carry, how fresh they are. None of
    them can tell a category-leading app from a well-reviewed small one, and
    that distinction is most of what makes a keyword unwinnable. Appfigures
    names downloads and ranks first among its Competitiveness inputs; AppTweak
    reduces the entire question to the top 10's App Power. This is the closest
    free equivalent, and it is the component that fills in the top of the
    scale: over 49 live SERPs it ranges 0 to 88.9 while sitting at a median of
    9.4, so it lifts genuinely hard keywords without inflating ordinary ones.

    WHY A CHART MISS IS 0 BUT A MISSING INDEX IS `None`
    ---------------------------------------------------
    The two look alike and mean opposite things, which is exactly the
    distinction this module exists to keep. `charts` is built by pulling the
    complete top 100 of every app genre, so an app that is not in it is one we
    searched for and did not find: a measured "not a top-100 app in its
    category", scored 0. An index that is *empty because every feed failed* is
    falsey and yields `None`, and `combine()` renormalizes around it — a
    storefront Apple stopped serving charts for must not read as a storefront
    where nothing is popular.

    Coverage is partial by nature: ~40% of top-10 apps chart somewhere. The
    other 60% are genuinely not top-100 apps, which is the measurement, not a
    gap in it.
    """
    if charts is None or not charts:
        return None
    if not apps:
        return None

    ceiling = math.log(charts.depth + 1)
    weighted = 0.0
    total_weight = 0.0
    for position, app in enumerate(apps):
        weight = 1.0 / (1.0 + COMP_APP_POWER_RANK_DECAY * position)
        rank = charts.rank_of(app.track_id)
        if rank is None or rank < 1:
            strength = 0.0
        else:
            depth = 1.0 - math.log(rank) / ceiling
            strength = COMP_APP_POWER_CHARTED_FLOOR + (
                1.0 - COMP_APP_POWER_CHARTED_FLOOR
            ) * max(0.0, depth)
        weighted += weight * strength * 100
        total_weight += weight
    return clamp(weighted / total_weight)


def breadth_component(result_count: int) -> float | None:
    """How crowded the overall result set is. Fitted to weight 0.

    `min(log10(count + 1) / 2.3, 1) * 100`.

    Note the ceiling: the API's `resultCount` counts what was returned, so it
    saturates at the request limit. That limit is now 200 — the API's maximum —
    which is exactly where the 2.3 divisor was always designed to saturate.
    While it was 50 the component could not exceed ~74 and had two points of
    spread across the whole stored corpus, which is why the weight fitted
    against it should not be read as a verdict on breadth. This component
    separates thin niches from crowded ones; it still cannot distinguish
    crowded from enormous.
    """
    if result_count < 0:
        return None
    return clamp(
        min(math.log10(result_count + 1) / COMP_BREADTH_LOG_DIVISOR, 1.0) * 100
    )


# ---------------------------------------------------------------------------
# combination
# ---------------------------------------------------------------------------


def derive(components: Mapping[str, float | None]) -> dict[str, float | None]:
    """Add the components that are functions of other components.

    Today that is only `comp_incumbent`, from `comp_rating_count` and
    `comp_stars`. Call this on a stored `snapshots` row before `combine()`.

    It **recomputes** rather than trusting whatever `comp_incumbent` the row
    already carries. That is the point: the derived column is a cache of a
    formula, so a change to `incumbent_component` has to reach old rows on the
    next `aso rescore`. Trusting the stored value would freeze every historical
    snapshot at the formula in force the day it was captured, and silently — the
    numbers would still look fine.

    Returns a new dict; the input is left alone.
    """
    derived = dict(components)
    derived["comp_incumbent"] = incumbent_component(
        components.get("comp_rating_count"), components.get("comp_stars")
    )
    return derived


def combine(
    components: Mapping[str, float | None],
    weights: Mapping[str, float] | None = None,
) -> float | None:
    """Weighted power mean over the computable components, then uplifted.

    `(sum(w * v**P) / sum(w)) ** (1/P)` with `P =
    COMPETITION_AGGREGATION_POWER`. At P = 1 this is the plain weighted mean it
    used to be; above 1 the strong components pull harder than
    the weak ones, because clearing a keyword means clearing its hardest
    barrier rather than its average one. See the constant for the reasoning and
    for the warning that it is the one number here fitted to nothing.

    Missing components are dropped and the remaining weights renormalized, so
    a partial capture is scored on what it knows instead of being dragged
    toward zero by absent data. Returns `None` if nothing was computable.

    This is the function that makes stored history re-scorable: feed it a
    `snapshots` row and a new weight dict and you get the new score, no
    network access involved.
    """
    active = weights if weights is not None else COMPETITION_WEIGHTS
    return combine_with(components, active, COMPETITION_AGGREGATION_POWER)


# ---------------------------------------------------------------------------
# calibration
# ---------------------------------------------------------------------------
#
# The competition weights were hand-chosen for the whole life of this project,
# because nothing measured difficulty and there was nothing to fit against.
# AppFigures' exports carry a Competitiveness column beside the Popularity one
# the demand side already uses, and `importers.read_competition_csv` now reads
# it. This section is the competition-side twin of `search.calibrate`.
#
# Read the result the same way: it fits our components to reproduce a vendor's
# derived index, which is a real improvement over fitting to nothing and is not
# the same thing as being right.

# Components the fit is allowed to give weight to. `comp_stars` is absent on
# purpose — it is an input to `comp_incumbent`, and letting the grid score it
# alone as well would double-count the same measurement.
CALIBRATION_COMPONENTS: tuple[str, ...] = (
    "comp_rating_count",
    "comp_exact_match",
    "comp_recency",
    "comp_publisher",
    "comp_breadth",
    "comp_incumbent",
    "comp_app_power",
)

# Weights are searched on a simplex of this many steps, so each weight is a
# multiple of 1/steps. Coarse on purpose, exactly as on the demand side: the
# target is a vendor's ordinal index over a few hundred keywords, and a finer
# grid would fit its noise and call the result precision.
CALIBRATION_WEIGHT_STEPS = 8
CALIBRATION_POWERS: tuple[float, ...] = (1.0, 1.5, 2.0, 3.0)
CALIBRATION_MIN_SAMPLES = 40
CALIBRATION_FOLDS = 5

# A component must be present on at least this fraction of the samples before
# the grid may give it weight.
#
# Without the floor the fit silently lies. `combine_with` skips a `None`
# component and renormalizes, so a weight vector putting half its mass on a
# column that is null everywhere scores *identically* to one that ignores it —
# and being an equal best, it can win and be reported. The first real run hit
# exactly that: two columns had just been cleared by a migration, and the fit
# handed one of them 0.125 while actually fitting on four components.
#
# Worse than cosmetic: paste those weights into config.py, run `aso refresh`
# so the column fills in again, and the model starts weighting a component the
# fit never evaluated.
CALIBRATION_MIN_COVERAGE = 0.5


class NotEnoughData(ValueError):
    """Too few keywords carry both stored components and a vendor rating."""


@dataclass(frozen=True)
class CompetitionCalibration:
    """A fitted weight set, plus enough evidence to judge whether to use it."""

    weights: dict[str, float]
    power: float

    n_samples: int
    # In-sample rank correlation of the winning grid point. Flatters itself: it
    # is the best of thousands of candidates measured on the data that chose it.
    spearman: float
    # Nested k-fold: weights re-fitted on each training split and scored on the
    # split they never saw. This is the number to believe.
    cv_spearman: float | None
    # The *current* weights over the same samples. No fitting is involved, so it
    # is already out-of-sample and answers "did this help?" honestly.
    baseline_spearman: float
    # How many distinct values the vendor's index took. Two keywords cannot be
    # ordered by a target that gives them the same number.
    distinct_targets: int
    # Components the grid was not allowed to weight, because too few samples
    # carried them. Surfaced rather than swallowed: "the fit gave stars zero"
    # and "the fit never saw stars" are different findings, and only the first
    # is evidence about stars.
    excluded_components: tuple[str, ...] = ()

    def as_config(self) -> dict[str, float]:
        """The constants to paste into `config.py`, named as they are there."""
        return {
            "COMPETITION_WEIGHTS": {
                name: round(self.weights.get(name, 0.0), 4)
                for name in COMPETITION_WEIGHTS
            },
            "COMPETITION_AGGREGATION_POWER": self.power,
        }

    @property
    def improved(self) -> bool:
        """Whether adopting this fit is justified.

        Compared on the cross-validated number when there is one, for the
        reason spelled out on `search.Calibration.improved`: the in-sample
        figure is the best of every grid point measured on its own training
        data, while the baseline involved no fitting at all.
        """
        return self.margin > 0

    @property
    def margin(self) -> float:
        """How far the fit beats the current weights, on the honest number."""
        fitted = self.cv_spearman if self.cv_spearman is not None else self.spearman
        return fitted - self.baseline_spearman


def _rank(values: Sequence[float]) -> list[float]:
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


def _spearman(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) < 2:
        return 0.0
    rank_a, rank_b = _rank(a), _rank(b)
    n = len(a)
    mean = (n + 1) / 2
    num = sum((x - mean) * (y - mean) for x, y in zip(rank_a, rank_b))
    den_a = math.sqrt(sum((x - mean) ** 2 for x in rank_a))
    den_b = math.sqrt(sum((y - mean) ** 2 for y in rank_b))
    if den_a < 1e-12 or den_b < 1e-12:
        return 0.0
    return num / (den_a * den_b)


def _simplex(steps: int, slots: int) -> Iterable[tuple[int, ...]]:
    """Every way to split `steps` units across `slots` weights."""
    if slots == 1:
        yield (steps,)
        return
    for taken in range(steps + 1):
        for rest in _simplex(steps - taken, slots - 1):
            yield (taken,) + rest


def _score_samples(
    samples: Sequence[Mapping[str, float | None]],
    weights: Mapping[str, float],
    power: float,
) -> tuple[list[float], list[float]]:
    """(predicted, target) over the samples the weights can actually score."""
    predicted: list[float] = []
    target: list[float] = []
    for sample in samples:
        value = combine_with(sample, weights, power)
        if value is None:
            continue
        predicted.append(value)
        target.append(float(sample["value"]))  # type: ignore[arg-type]
    return predicted, target


def combine_with(
    components: Mapping[str, float | None],
    weights: Mapping[str, float],
    power: float,
) -> float | None:
    """`combine()` with the weights and exponent supplied explicitly.

    Exists so the grid search can evaluate a candidate without mutating module
    state. `combine()` is the same computation reading the configured values.
    """
    total_weight = 0.0
    accumulated = 0.0
    for name, weight in weights.items():
        value = components.get(name)
        if value is None or weight <= 0:
            continue
        # Components are already clamped to 0-100, so the power is always
        # taken of a non-negative number and never leaves the reals.
        accumulated += (clamp(float(value)) ** power) * weight
        total_weight += weight
    if total_weight == 0:
        return None
    return clamp((accumulated / total_weight) ** (1.0 / power))


def covered_components(
    samples: Sequence[Mapping[str, float | None]],
    *,
    min_coverage: float = CALIBRATION_MIN_COVERAGE,
) -> tuple[str, ...]:
    """The components present often enough to be worth fitting.

    See `CALIBRATION_MIN_COVERAGE` for why a sparse component must be kept out
    of the grid rather than merely doing nothing inside it.
    """
    if not samples:
        return ()
    return tuple(
        name
        for name in CALIBRATION_COMPONENTS
        if sum(1 for s in samples if s.get(name) is not None) / len(samples)
        >= min_coverage
    )


def _best_on(
    samples: Sequence[Mapping[str, float | None]],
    components: Sequence[str],
) -> tuple[dict[str, float], float, float]:
    """Grid-search weights and exponent. Returns (weights, power, spearman).

    Ties are broken toward the *simplest* candidate — fewest weighted
    components, then the lower exponent. Rank correlation plateaus readily on a
    few hundred samples, and reporting the first of forty equally good vectors
    would attribute the fit to whichever the loop happened to reach first.
    """
    if not components:
        return {}, 1.0, -2.0
    best_rho = -2.0
    best_weights: dict[str, float] = {}
    best_power = 1.0
    best_terms = len(components) + 1
    steps = CALIBRATION_WEIGHT_STEPS
    for combo in _simplex(steps, len(components)):
        if sum(combo) == 0:
            continue
        weights = {
            name: count / steps for name, count in zip(components, combo) if count
        }
        terms = len(weights)
        for power in CALIBRATION_POWERS:
            predicted, target = _score_samples(samples, weights, power)
            if len(predicted) < 8:
                continue
            rho = _spearman(predicted, target)
            better = rho > best_rho + 1e-9
            tied_simpler = abs(rho - best_rho) <= 1e-9 and terms < best_terms
            if better or tied_simpler:
                best_rho, best_weights, best_power, best_terms = (
                    rho,
                    weights,
                    power,
                    terms,
                )
    return best_weights, best_power, best_rho


def calibrate(
    samples: Sequence[Mapping[str, float | None]],
    *,
    min_samples: int = CALIBRATION_MIN_SAMPLES,
    folds: int = CALIBRATION_FOLDS,
) -> CompetitionCalibration:
    """Fit the competition weights against a vendor's difficulty index.

    `samples` are rows from `repository.competition_calibration_samples`: the
    stored `comp_*` columns for a keyword plus the vendor's `value`.

    How it fits
    -----------
    Every weight enters the power mean linearly *inside* the exponent, so no
    closed form solves them; the simplex of weight vectors is searched directly
    against the exponent grid, scoring each candidate by rank correlation with
    the vendor's index. Rank correlation rather than RMSE because the target is
    an ordinal 0-100 index whose absolute level means nothing shared with ours
    — matching the level is a separate job, and `scoring/bridge.py` is what
    does that kind of job.

    What it will not do
    -------------------
    Fit fewer than `min_samples` keywords, or a sample whose target is nearly
    all ties. Six weights against thirty points reproduces the sample and
    predicts nothing.

    The reported `cv_spearman` is **nested**: the grid is re-searched inside
    each training split, so the number measures the fitting procedure rather
    than the winning grid point. It is normally well below `spearman`, and the
    gap is the size of the overfit you would otherwise have believed.

    Applying the result is deliberately manual — print `as_config()`, paste
    into `config.py`, run `aso rescore`. Constants that silently rewrite
    themselves are constants nobody reviews.
    """
    usable = [s for s in samples if s.get("value") is not None]
    if len(usable) < min_samples:
        raise NotEnoughData(
            f"Need at least {min_samples} keywords with both stored components "
            f"and a vendor difficulty rating; got {len(usable)}. Import a "
            "broader export, track those keywords, and run `aso refresh`."
        )
    distinct = len({float(s["value"]) for s in usable})  # type: ignore[arg-type]
    if distinct < 5:
        raise NotEnoughData(
            f"The difficulty column takes only {distinct} distinct values over "
            f"{len(usable)} keywords, which cannot order them. Import an export "
            "covering a wider range of keywords."
        )

    components = covered_components(usable)
    if not components:
        raise NotEnoughData(
            "No competition component is present on at least "
            f"{CALIBRATION_MIN_COVERAGE:.0%} of the {len(usable)} samples, so "
            "there is nothing to fit. Run `aso refresh` to fill the component "
            "columns, then calibrate."
        )
    excluded = tuple(n for n in CALIBRATION_COMPONENTS if n not in components)

    weights, power, in_sample = _best_on(usable, components)

    baseline_pred, baseline_target = _score_samples(
        usable, COMPETITION_WEIGHTS, COMPETITION_AGGREGATION_POWER
    )
    baseline = _spearman(baseline_pred, baseline_target) if baseline_pred else 0.0

    cv: float | None = None
    if len(usable) >= folds * 8:
        # Deterministic interleave rather than a shuffle: the samples arrive
        # sorted by target value, so taking every k-th row gives each fold the
        # full difficulty range. A contiguous split would train on hard
        # keywords and test on easy ones.
        scores: list[float] = []
        for fold in range(folds):
            test = usable[fold::folds]
            train = [s for i, s in enumerate(usable) if i % folds != fold]
            fold_weights, fold_power, _ = _best_on(train, components)
            predicted, target = _score_samples(test, fold_weights, fold_power)
            if len(predicted) >= 8:
                scores.append(_spearman(predicted, target))
        if scores:
            cv = sum(scores) / len(scores)

    return CompetitionCalibration(
        weights=weights,
        power=power,
        n_samples=len(usable),
        spearman=in_sample,
        cv_spearman=cv,
        baseline_spearman=baseline,
        distinct_targets=distinct,
        excluded_components=excluded,
    )


# ---------------------------------------------------------------------------
# level calibration
# ---------------------------------------------------------------------------


def fit_bridge(
    samples: Sequence[Mapping[str, float | None]],
    *,
    weights: Mapping[str, float] | None = None,
    power: float | None = None,
    min_overlap: int = COMPETITION_BRIDGE_MIN_OVERLAP,
) -> Bridge:
    """Fit the monotone map from our competition score onto a vendor's scale.

    WHY THIS IS A SEPARATE STEP FROM `calibrate`
    --------------------------------------------
    `calibrate` scores every grid point by rank correlation, and rank
    correlation is invariant under any monotone transform of the output. That
    is not a shortcoming of the implementation — it is the definition of the
    statistic. It means the fitting procedure **cannot see level at all**: a
    weight vector that reproduces the vendor's ordering perfectly while sitting
    twenty points low scores exactly the same as one that also matches the
    level. Re-running `calibrate` with more data will never move the level,
    however much data it gets.

    So the two jobs are split, exactly as they are on the demand side.
    `calibrate` fixes the ordering and reports Spearman; this fixes the scale
    and reports RMSE. Both numbers are needed and neither substitutes for the
    other.

    ORDER MATTERS
    -------------
    Fit this **after** the components and weights are settled, never before. A
    bridge is a monotone map from whatever the raw score currently is; fitting
    one over a distorted score bakes the distortion into the knots and then
    hides it behind a plausible-looking number.

    WHAT IT DOES NOT DO
    -------------------
    It cannot improve rank correlation, and reporting a before/after Spearman
    for it would be reporting a tautology — see `scoring/bridge.py`, which
    makes the same point at length about the demand bridge.

    Nor does it make us right. It makes us reproduce the vendor's level as well
    as the vendor's ordering, which is a real improvement over reproducing
    neither and is not the same thing.
    """
    active_weights = weights if weights is not None else COMPETITION_WEIGHTS
    active_power = power if power is not None else COMPETITION_AGGREGATION_POWER

    pairs: list[tuple[float, float]] = []
    for sample in samples:
        target = sample.get("value")
        if target is None:
            continue
        raw = combine_with(derive(sample), active_weights, active_power)
        if raw is None:
            continue
        pairs.append((raw, float(target)))

    if len(pairs) < min_overlap:
        raise NotEnoughOverlap(
            f"Need at least {min_overlap} keywords with both a competition "
            f"score and a vendor difficulty rating to fit a bridge; got "
            f"{len(pairs)}. Import a broader export, track those keywords, and "
            "run `aso refresh`."
        )

    return bridge_fit(
        pairs,
        min_overlap=min_overlap,
        floor=COMPETITION_BRIDGE_FLOOR,
        ceiling=COMPETITION_BRIDGE_CEILING,
    )


def score(
    serp: Serp,
    *,
    now: datetime | None = None,
    weights: Mapping[str, float] | None = None,
    top_n: int = COMPETITION_TOP_N,
    charts: ChartIndex | None = None,
) -> CompetitionResult:
    """Score a SERP. Fewer than `top_n` results is fine — it scores what's there.

    An empty SERP scores 0: breadth is a genuine 0 (nobody is competing) while
    every other component is unknown, so the renormalized mean is 0. That is
    the right answer — an empty result set is an uncontested term — but treat
    it with suspicion, since it also looks exactly like a keyword Apple has no
    index for at all.
    """
    top = serp.top(top_n)
    rating_mass = rating_count_component(top)
    stars = stars_component(top)
    components = CompetitionComponents(
        comp_rating_count=rating_mass,
        comp_exact_match=exact_match_component(top, serp.keyword),
        comp_stars=stars,
        comp_recency=recency_component(top, now),
        comp_publisher=publisher_component(top, serp.apps),
        comp_breadth=breadth_component(serp.result_count),
        comp_incumbent=incumbent_component(rating_mass, stars),
        comp_app_power=app_power_component(top, charts),
    )
    return CompetitionResult(
        score=combine(components.as_dict(), weights),
        components=components,
        sample_size=len(top),
    )
