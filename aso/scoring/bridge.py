"""Map the proxy score onto Apple's popularity scale.

WHY THIS EXISTS
---------------
Once Apple's own 5-100 popularity index is available for some keywords, the
demand column stops being one measurement. Head terms carry Apple's number;
everything below Apple's reporting threshold carries the autocomplete + SERP
proxy. Both are "0-100", and that is a coincidence of formatting, not a shared
meaning — the proxy's 60 was fitted to reproduce an *ordering*, and its
absolute level is arbitrary (see `scoring/search.py`, which says so at length).

Sorting a column that mixes the two compares two different rulers. Worse,
`opportunity = search x (100 - competition)` multiplies by that column, so the
distortion does not stay in the demand ranking — it propagates.

This module fits the monotone map that makes the two commensurable, using the
keywords where BOTH are known.

WHY ISOTONIC AND NOT A LINE
---------------------------
A linear rescale assumes the proxy is right up to a shift and a stretch. It is
not: `search.py` documents two failure modes that bend the relationship in
opposite directions at opposite ends of the range (autocomplete saturates at
the top, the SERP inherits its category at the bottom). A monotone step
function absorbs exactly that kind of bend and assumes nothing about its shape.

It also cannot reorder anything. That is the point: the proxy's ordering is
the part we have evidence for — 0.718 cross-validated — and the part we are
fixing is the scale. A fit that could reorder would be quietly discarding the
one thing that was measured.

WHAT THIS DOES NOT DO
---------------------
It does not improve rank correlation, and anyone reporting a before/after
Spearman for a bridge is reporting an artefact.

Be precise about why, because the usual one-line version of this claim is
slightly wrong. A *strictly* increasing map would leave every rank exactly
where it found it and the two Spearmans would agree to the last bit. PAVA's
output is monotone **non-decreasing**: pooled blocks collapse a range of
distinct proxy scores onto a single fitted value. The map therefore cannot
swap any pair — the ordering is genuinely preserved — but it can *tie* two
keywords the proxy had separated, and ties move a rank correlation. Measured
on a 60-point synthetic fit, that pooling shifted Spearman by about +0.006.

The shift is arithmetic about ties, in either direction, and carries no
information about whether the bridge helped. The measure that means something
here is RMSE against the target values on the overlap. The question "did
blending help?" is about the blended column and belongs to `aso eval`, not
here.

The measure that means something here is RMSE against Apple's values on the
overlap. The question "did blending help?" is about the blended column and
belongs to `aso eval`, not here.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass

from ..config import APPLE_POPULARITY_CEILING, APPLE_POPULARITY_FLOOR


class NotEnoughOverlap(ValueError):
    """Too few keywords carry both a proxy score and an Apple value.

    Fitting a step function through a handful of points reproduces those points
    and says nothing about any other keyword, which is the failure mode
    `calibrate()` guards against too.
    """


@dataclass(frozen=True)
class Bridge:
    """A fitted monotone map from proxy score onto Apple's scale.

    `knots` is ascending in x and non-decreasing in y by construction. Between
    knots the map is linear; outside them it is flat.
    """

    knots: tuple[tuple[float, float], ...]
    n_overlap: int
    rmse: float

    def apply(self, proxy_score: float) -> float:
        """Map one proxy score onto Apple's scale.

        Clamped, never extrapolated. Beyond the range the overlap actually
        covered there is no evidence about the relationship, and continuing the
        last segment's slope would manufacture some — a proxy score above
        anything in the overlap would sail past 100 on a steep final segment.
        Flat is the honest continuation.
        """
        if not self.knots:
            return proxy_score

        if proxy_score <= self.knots[0][0]:
            return self.knots[0][1]
        if proxy_score >= self.knots[-1][0]:
            return self.knots[-1][1]

        for (x0, y0), (x1, y1) in zip(self.knots, self.knots[1:]):
            if x0 <= proxy_score <= x1:
                if x1 - x0 < 1e-12:
                    return y1
                fraction = (proxy_score - x0) / (x1 - x0)
                return y0 + fraction * (y1 - y0)

        return self.knots[-1][1]  # pragma: no cover - covered by the clamps

    def to_json(self) -> str:
        return json.dumps([[round(x, 6), round(y, 6)] for x, y in self.knots])

    @classmethod
    def from_json(cls, raw: str, *, n_overlap: int = 0, rmse: float = 0.0) -> Bridge:
        parsed = json.loads(raw)
        knots = tuple((float(x), float(y)) for x, y in parsed)
        return cls(knots=knots, n_overlap=n_overlap, rmse=rmse)


def _pava(values: Sequence[float], weights: Sequence[float]) -> list[float]:
    """Pool Adjacent Violators: the nearest non-decreasing sequence to `values`.

    Walks left to right maintaining a stack of blocks, each a (weighted mean,
    total weight) pair. Whenever a new block is lower than the one before it,
    the two are merged and their mean recomputed — which can violate the block
    before *that*, so the merge repeats until the stack is ordered again.

    Weighted because the input is grouped by distinct proxy score: two keywords
    sharing a proxy score are one block carrying twice the weight, and letting
    them count once each would let ties quietly outvote isolated points.
    """
    means: list[float] = []
    block_weights: list[float] = []

    for value, weight in zip(values, weights):
        means.append(value)
        block_weights.append(weight)
        while len(means) > 1 and means[-2] > means[-1]:
            total = block_weights[-2] + block_weights[-1]
            pooled = (means[-2] * block_weights[-2] + means[-1] * block_weights[-1]) / total
            means[-2:] = [pooled]
            block_weights[-2:] = [total]

    fitted: list[float] = []
    for mean, weight in zip(means, block_weights):
        fitted.extend([mean] * int(round(weight)))
    return fitted


def fit(
    pairs: Sequence[tuple[float, float]],
    *,
    min_overlap: int = 12,
    floor: float = APPLE_POPULARITY_FLOOR,
    ceiling: float = APPLE_POPULARITY_CEILING,
) -> Bridge:
    """Fit the bridge from (proxy_score, target_value) pairs.

    Ties on the proxy score are averaged into a single point before the fit
    rather than being handed to PAVA as separate observations. A step function
    cannot separate two keywords that scored identically on the proxy, so
    pretending it might would only add noise to the block it lands in.

    `floor` and `ceiling` clamp the fitted output to the range the target scale
    actually occupies. They default to Apple's popularity range because that
    was this module's first and original caller, but nothing else here is
    demand-specific: the competition side fits the same monotone map from its
    own raw score onto a vendor difficulty index, over 0-100. Passing the range
    explicitly is what keeps one scale's floor from leaking into the other's —
    clamping a difficulty bridge up to Apple's popularity floor of 5 would
    invent competition for keywords that have none.
    """
    usable = [
        (float(proxy), float(apple))
        for proxy, apple in pairs
        if proxy is not None and apple is not None
    ]
    if len(usable) < min_overlap:
        raise NotEnoughOverlap(
            f"Need at least {min_overlap} keywords with both a proxy score and "
            f"an Apple popularity value to fit a bridge; got {len(usable)}. "
            "Pull popularity for more of your tracked keywords, or lower "
            "BRIDGE_MIN_OVERLAP if you accept a coarser map."
        )

    grouped: dict[float, list[float]] = {}
    for proxy, apple in usable:
        grouped.setdefault(proxy, []).append(apple)

    xs = sorted(grouped)
    ys = [sum(grouped[x]) / len(grouped[x]) for x in xs]
    weights = [float(len(grouped[x])) for x in xs]

    fitted_flat = _pava(ys, weights)
    # `_pava` returns one value per original observation; collapse back to one
    # per distinct x, which is what the knots are indexed by.
    fitted: list[float] = []
    cursor = 0
    for weight in weights:
        fitted.append(fitted_flat[cursor])
        cursor += int(round(weight))

    clamped = [max(floor, min(ceiling, value)) for value in fitted]

    # Collapse runs of equal y into their endpoints. A flat run of forty knots
    # is forty ways of writing the same segment, and the stored JSON is meant
    # to be readable by whoever audits the fit.
    knots: list[tuple[float, float]] = []
    for index, (x, y) in enumerate(zip(xs, clamped)):
        first = index == 0
        last = index == len(xs) - 1
        if first or last or clamped[index - 1] != y or clamped[index + 1] != y:
            knots.append((x, y))

    bridge = Bridge(knots=tuple(knots), n_overlap=len(usable), rmse=0.0)
    residuals = [bridge.apply(proxy) - apple for proxy, apple in usable]
    rmse = math.sqrt(sum(r * r for r in residuals) / len(residuals))
    return Bridge(knots=tuple(knots), n_overlap=len(usable), rmse=rmse)
