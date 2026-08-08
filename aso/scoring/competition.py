"""Competition score: how hard is it to rank for this keyword?

0-100, higher = harder. Six normalized components, combined as a weighted mean
with weights from `config.COMPETITION_WEIGHTS`.

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

from ..clients.itunes import AppRecord, Serp
from ..config import (
    COMP_BREADTH_LOG_DIVISOR,
    COMP_MAX_STARS,
    COMP_PUBLISHER_MIN_APPEARANCES,
    COMP_RATING_COUNT_LOG_DIVISOR,
    COMP_RECENCY_MAX_DAYS,
    COMPETITION_TOP_N,
    COMPETITION_WEIGHTS,
)

WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class CompetitionComponents:
    """The six normalized 0-100 components. `None` means "not computable"."""

    comp_rating_count: float | None = None
    comp_exact_match: float | None = None
    comp_stars: float | None = None
    comp_recency: float | None = None
    comp_publisher: float | None = None
    comp_breadth: float | None = None

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
    """Review mass of the top 10 — the heaviest single signal (weight 0.35).

    `min(log10(median + 1) / 6, 1) * 100`: log-scaled because the gap between
    100 and 1,000 ratings matters far more than between 100,000 and 101,000.
    Saturates at a median of 1,000,000.

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


def exact_match_component(apps: Sequence[AppRecord], keyword: str) -> float | None:
    """Fraction of the top 10 explicitly targeting the full keyword (weight 0.25).

    Substring match on title or subtitle, case- and whitespace-insensitive.

    In practice the iTunes Search API returns no subtitle, so this is a
    title-only match and therefore *understates* competition for terms
    incumbents target in the subtitle. See `clients/itunes.py`.
    """
    needle = normalize_text(keyword)
    if not needle or not apps:
        return None
    hits = sum(
        1
        for app in apps
        if needle in normalize_text(app.track_name)
        or needle in normalize_text(app.subtitle)
    )
    return clamp(hits / len(apps) * 100)


def stars_component(apps: Sequence[AppRecord]) -> float | None:
    """Quality of the incumbents (weight 0.10).

    Median star rating over apps that actually have one, out of 5. Unrated apps
    are excluded rather than counted as 0 — see the module docstring.
    """
    ratings = [
        float(app.average_user_rating)  # type: ignore[arg-type]
        for app in apps
        if has_real_rating(app)
    ]
    med = _median_or_none(ratings)
    if med is None:
        return None
    return clamp(med / COMP_MAX_STARS * 100)


def recency_component(
    apps: Sequence[AppRecord], now: datetime | None = None
) -> float | None:
    """How actively the incumbents are maintained (weight 0.10).

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
    """Publisher concentration (weight 0.10).

    Fraction of the top 10 whose seller appears at least
    `COMP_PUBLISHER_MIN_APPEARANCES` times across the *full* result set, not
    just the top 10 — one publisher holding many slots means a space that is
    hard to break into.

    Apps with no seller name can't be attributed, so they count toward the
    denominator but never the numerator: unknown attribution is not evidence
    of concentration.
    """
    if not top:
        return None
    appearances: dict[str, int] = {}
    for app in full_results:
        seller = normalize_text(app.seller_name)
        if seller:
            appearances[seller] = appearances.get(seller, 0) + 1
    repeated = {
        seller
        for seller, count in appearances.items()
        if count >= COMP_PUBLISHER_MIN_APPEARANCES
    }
    hits = sum(1 for app in top if normalize_text(app.seller_name) in repeated)
    return clamp(hits / len(top) * 100)


def breadth_component(result_count: int) -> float | None:
    """How crowded the overall result set is (weight 0.10).

    `min(log10(count + 1) / 2.3, 1) * 100`.

    Note the ceiling: the API's `resultCount` counts what was returned, so it
    saturates at the request limit (50 -> ~74). This component separates thin
    niches from crowded ones; it cannot distinguish crowded from enormous.
    """
    if result_count < 0:
        return None
    return clamp(
        min(math.log10(result_count + 1) / COMP_BREADTH_LOG_DIVISOR, 1.0) * 100
    )


# ---------------------------------------------------------------------------
# combination
# ---------------------------------------------------------------------------


def combine(
    components: Mapping[str, float | None],
    weights: Mapping[str, float] | None = None,
) -> float | None:
    """Weighted mean over the components that could be computed.

    Missing components are dropped and the remaining weights renormalized, so
    a partial capture is scored on what it knows instead of being dragged
    toward zero by absent data. Returns `None` if nothing was computable.

    This is the function that makes stored history re-scorable: feed it a
    `snapshots` row and a new weight dict and you get the new score, no
    network access involved.
    """
    active = weights if weights is not None else COMPETITION_WEIGHTS
    total_weight = 0.0
    accumulated = 0.0
    for name, weight in active.items():
        value = components.get(name)
        if value is None or weight <= 0:
            continue
        accumulated += value * weight
        total_weight += weight
    if total_weight == 0:
        return None
    return clamp(accumulated / total_weight)


def score(
    serp: Serp,
    *,
    now: datetime | None = None,
    weights: Mapping[str, float] | None = None,
    top_n: int = COMPETITION_TOP_N,
) -> CompetitionResult:
    """Score a SERP. Fewer than `top_n` results is fine — it scores what's there.

    An empty SERP scores 0: breadth is a genuine 0 (nobody is competing) while
    every other component is unknown, so the renormalized mean is 0. That is
    the right answer — an empty result set is an uncontested term — but treat
    it with suspicion, since it also looks exactly like a keyword Apple has no
    index for at all.
    """
    top = serp.top(top_n)
    components = CompetitionComponents(
        comp_rating_count=rating_count_component(top),
        comp_exact_match=exact_match_component(top, serp.keyword),
        comp_stars=stars_component(top),
        comp_recency=recency_component(top, now),
        comp_publisher=publisher_component(top, serp.apps),
        comp_breadth=breadth_component(serp.result_count),
    )
    return CompetitionResult(
        score=combine(components.as_dict(), weights),
        components=components,
        sample_size=len(top),
    )
