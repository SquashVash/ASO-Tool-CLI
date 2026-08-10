"""The competition bridge: the part of the model that fixes *level*.

`competition.calibrate` scores candidates by rank correlation, which is
invariant under any monotone transform of the output — so it is structurally
incapable of noticing that the whole column sits low. These tests pin the
separation of concerns that follows from that fact.
"""

from __future__ import annotations

import random

import pytest

from aso import config
from aso.scoring import competition as comp
from aso.scoring.bridge import NotEnoughOverlap


def samples(
    n: int = 60,
    *,
    seed: int = 7,
    scale: float = 0.45,
    noise: float = 3.0,
) -> list[dict[str, float | None]]:
    """Rows whose raw competition score tracks the target but sits low.

    `scale` below 1 reproduces the reported symptom exactly: the ordering is
    good, the level is not.
    """
    rng = random.Random(seed)
    rows: list[dict[str, float | None]] = []
    for _ in range(n):
        target = rng.uniform(5.0, 95.0)

        def component() -> float:
            return max(0.0, min(100.0, target * scale + rng.gauss(0, noise)))

        rows.append(
            {
                "comp_publisher": component(),
                "comp_rating_count": component(),
                "comp_exact_match": component(),
                "comp_stars": 60.0,
                "value": target,
            }
        )
    return rows


def raw_scores(rows: list[dict[str, float | None]]) -> list[float]:
    return [
        comp.combine_with(
            comp.derive(row), comp.COMPETITION_WEIGHTS, comp.COMPETITION_AGGREGATION_POWER
        )
        for row in rows
    ]


# --- the point of the thing -------------------------------------------------


def test_the_bridge_lifts_a_score_that_sits_low() -> None:
    rows = samples()
    bridge = comp.fit_bridge(rows)
    raw = raw_scores(rows)

    raw_mean = sum(raw) / len(raw)
    bridged_mean = sum(bridge.apply(v) for v in raw) / len(raw)
    target_mean = sum(float(r["value"]) for r in rows) / len(rows)

    assert raw_mean < target_mean - 10, "the fixture must actually be biased low"
    assert abs(bridged_mean - target_mean) < abs(raw_mean - target_mean)


def test_the_bridge_cannot_reorder_anything() -> None:
    """A monotone map leaves every rank exactly where it found it.

    This is why fitting the ordering and fitting the level are separate steps:
    the bridge is guaranteed not to undo `calibrate`'s work.
    """
    rows = samples()
    bridge = comp.fit_bridge(rows)
    raw = sorted(raw_scores(rows))
    bridged = [bridge.apply(v) for v in raw]
    assert all(a <= b + 1e-9 for a, b in zip(bridged, bridged[1:]))


def test_the_bridge_never_inverts_a_pair() -> None:
    """The precise version of "it cannot reorder anything".

    Note what this does *not* say. PAVA's output is monotone **non-decreasing**,
    not strictly increasing: pooled blocks map a range of distinct raw scores
    onto one fitted value. So the bridge can *tie* two keywords that the raw
    score separated, and a tie is a rank change.

    It can never swap them, which is the property that matters — the ordering
    `calibrate` fitted survives intact. But it does mean a before/after
    Spearman is not quite the identity it is often described as; ties shift it
    slightly, in either direction, for reasons that have nothing to do with the
    bridge being any good. RMSE remains the measure to report.
    """
    rows = samples()
    bridge = comp.fit_bridge(rows)
    raw = raw_scores(rows)
    bridged = [bridge.apply(v) for v in raw]

    for i in range(len(raw)):
        for j in range(len(raw)):
            if raw[i] < raw[j]:
                assert bridged[i] <= bridged[j] + 1e-9, "a pair was inverted"


def test_a_bridge_spearman_moves_only_by_ties_and_is_not_evidence() -> None:
    """Pinned so nobody reads a bridge's rank correlation as an improvement."""
    rows = samples()
    bridge = comp.fit_bridge(rows)
    raw = raw_scores(rows)
    targets = [float(r["value"]) for r in rows]
    before = comp._spearman(raw, targets)
    after = comp._spearman([bridge.apply(v) for v in raw], targets)
    # Close, but not identical — and the gap is pooling, not signal.
    assert abs(after - before) < 0.02


def test_rmse_is_the_number_that_moves() -> None:
    rows = samples()
    bridge = comp.fit_bridge(rows)
    raw = raw_scores(rows)
    targets = [float(r["value"]) for r in rows]

    def rmse(values: list[float]) -> float:
        return (sum((v - t) ** 2 for v, t in zip(values, targets)) / len(targets)) ** 0.5

    assert rmse([bridge.apply(v) for v in raw]) < rmse(raw)
    assert bridge.rmse == pytest.approx(rmse([bridge.apply(v) for v in raw]), rel=1e-6)


# --- scale ------------------------------------------------------------------


def test_competition_is_clamped_to_zero_not_to_apples_popularity_floor() -> None:
    """A keyword nobody contests is a 0. Apple's 5 floor is a demand fact.

    Clamping difficulty up to 5 would invent competition that was never there.
    """
    assert config.COMPETITION_BRIDGE_FLOOR == 0.0
    rows = samples()
    for row in rows:
        row["value"] = 0.0 if float(row["value"]) < 50 else float(row["value"])
    bridge = comp.fit_bridge(rows)
    assert min(y for _, y in bridge.knots) >= 0.0
    assert min(y for _, y in bridge.knots) < config.APPLE_POPULARITY_FLOOR


def test_the_demand_bridge_keeps_its_own_floor() -> None:
    """Parameterizing the range must not have leaked one scale into the other."""
    from aso.scoring import bridge as demand_bridge

    fitted = demand_bridge.fit([(float(i), 0.0) for i in range(20)])
    assert min(y for _, y in fitted.knots) == pytest.approx(
        config.APPLE_POPULARITY_FLOOR
    )


# --- guards -----------------------------------------------------------------


def test_too_little_overlap_is_refused() -> None:
    with pytest.raises(NotEnoughOverlap):
        comp.fit_bridge(samples(n=10))


def test_the_competition_bridge_demands_more_overlap_than_the_demand_one() -> None:
    """The target is a vendor's derived index, so it carries its own error."""
    assert config.COMPETITION_BRIDGE_MIN_OVERLAP > config.BRIDGE_MIN_OVERLAP


def test_rows_without_a_vendor_rating_are_skipped() -> None:
    rows = samples()
    rows.extend({**r, "value": None} for r in samples(n=20, seed=99))
    assert comp.fit_bridge(rows).n_overlap == 60


def test_rows_that_cannot_be_scored_are_skipped() -> None:
    """An unscoreable row is not a data point about level."""
    rows = samples()
    blank: dict[str, float | None] = {name: None for name in comp.COMPETITION_WEIGHTS}
    blank["value"] = 50.0
    rows.append(blank)
    assert comp.fit_bridge(rows).n_overlap == 60


def test_the_fit_follows_the_weights_it_is_given() -> None:
    """A bridge belongs to a weight vector; fitting under the wrong one lies."""
    rows = samples()
    only_publisher = {"comp_publisher": 1.0}
    a = comp.fit_bridge(rows)
    b = comp.fit_bridge(rows, weights=only_publisher, power=1.0)
    assert a.knots != b.knots
