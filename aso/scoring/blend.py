"""Combine Apple's popularity index with the proxy into one demand column.

THE THREE CASES
---------------
1. **Apple scored the keyword.** Use its number verbatim. There is nothing to
   improve on: this is the quantity the proxy was built to estimate.
2. **Apple was asked and declined to score it** (`censored`). Apple reports
   nothing below its threshold, which is not silence — it is the statement
   "less popular than the least popular thing I will name". So the proxy still
   orders these keywords against each other, but is capped at
   `APPLE_POPULARITY_FLOOR`. The bound is used as a bound.
3. **Apple was never asked**, or the endpoint was off or failed. Bridged proxy,
   uncapped. This is every keyword when `apple_popularity_enabled` is false,
   which is why that case has to stay exactly as good as it was before any of
   this existed.

WHY CASE 2 IS NOT JUST CASE 3
-----------------------------
It would be easier to treat "no value" as "no information" and let the proxy
speak freely. But the proxy's documented failure is scoring low-demand terms
far too high — `finsta` measures 5 and the proxy says 92, because the SERP
inherits Instagram's incumbents. Case 2 is precisely the population where that
failure lives, and Apple has just told us the true value is at the floor. Not
applying the cap would discard the single most corrective fact available about
the keywords the proxy is worst at.

WHY THE PROXY IS BRIDGED AND NOT USED RAW
-----------------------------------------
See `scoring/bridge.py`. The proxy's ordering is evidence; its absolute level
is arbitrary. Mixing a raw proxy score with an Apple value in one sorted column
compares two rulers, and `opportunity` multiplies by that column.

Without a fitted bridge the proxy passes through unchanged and the column is
labelled honestly as such — a missing bridge degrades the blend, it does not
block it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import APPLE_POPULARITY_CEILING, APPLE_POPULARITY_FLOOR
from .bridge import Bridge

# Values for `snapshots.search_source`. NULL there means a row written before
# the blend existed, carrying a raw proxy score on the old scale.
SOURCE_APPLE = "apple"
SOURCE_PROXY = "proxy"
SOURCE_PROXY_CENSORED = "proxy_censored"


@dataclass(frozen=True)
class BlendedScore:
    """A demand score and the ruler that produced it."""

    value: float
    source: str

    @property
    def is_measured(self) -> bool:
        """True when Apple supplied the number rather than the proxy."""
        return self.source == SOURCE_APPLE


def blend(
    proxy_score: float | None,
    *,
    apple_value: float | None = None,
    censored: bool = False,
    bridge: Bridge | None = None,
) -> BlendedScore | None:
    """Resolve one keyword's demand score.

    Returns None when there is nothing to score at all — no proxy and no Apple
    reading. That is distinct from a floor score: the caller must be able to
    leave `search_score` NULL for a snapshot whose fetch failed, exactly as
    `rescore` already does today.

    `censored=True` with `apple_value` set is contradictory — Apple either
    scored the term or it did not. The value is ignored and the censoring
    believed, because the censoring is the more specific claim.
    """
    if apple_value is not None and not censored:
        clamped = max(
            APPLE_POPULARITY_FLOOR, min(APPLE_POPULARITY_CEILING, float(apple_value))
        )
        return BlendedScore(value=clamped, source=SOURCE_APPLE)

    if proxy_score is None:
        # Apple declined to score it and we have no proxy either. The censoring
        # is still a real observation, and the floor is what it says.
        if censored:
            return BlendedScore(value=APPLE_POPULARITY_FLOOR, source=SOURCE_PROXY_CENSORED)
        return None

    mapped = bridge.apply(float(proxy_score)) if bridge is not None else float(proxy_score)

    if censored:
        return BlendedScore(
            value=min(mapped, APPLE_POPULARITY_FLOOR), source=SOURCE_PROXY_CENSORED
        )
    return BlendedScore(value=mapped, source=SOURCE_PROXY)
