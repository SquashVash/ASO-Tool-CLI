from __future__ import annotations

import pytest

from aso.config import APPLE_POPULARITY_CEILING, APPLE_POPULARITY_FLOOR
from aso.scoring import search
from aso.scoring.bridge import Bridge, NotEnoughOverlap, fit


def linear_pairs(n: int = 20) -> list[tuple[float, float]]:
    """Proxy and Apple already agreeing, up to scale. The easy case."""
    return [(float(i * 5), 5.0 + i * 4.5) for i in range(n)]


def test_fit_refuses_a_handful_of_points() -> None:
    with pytest.raises(NotEnoughOverlap, match="at least 12"):
        fit([(10.0, 20.0), (20.0, 40.0)])


def test_fit_is_monotone_even_when_the_data_is_not() -> None:
    """The whole point: the map may flatten, never invert.

    The input here inverts hard in the middle — proxy rises while Apple falls,
    which is exactly the `finsta` failure. PAVA must pool that stretch flat
    rather than reproduce the inversion.
    """
    pairs = [(float(i), 50.0) for i in range(20)]
    pairs[8] = (8.0, 90.0)
    pairs[9] = (9.0, 10.0)

    bridge = fit(pairs)
    ys = [y for _, y in bridge.knots]
    assert ys == sorted(ys)


def test_fit_reproduces_a_clean_linear_relationship() -> None:
    bridge = fit(linear_pairs())
    for proxy, apple in linear_pairs():
        assert bridge.apply(proxy) == pytest.approx(apple, abs=1e-6)
    assert bridge.rmse == pytest.approx(0.0, abs=1e-9)


def test_apply_clamps_rather_than_extrapolating() -> None:
    """Beyond the overlap there is no evidence, so the map goes flat.

    Extrapolating the final segment's slope would let a proxy score above
    anything observed sail past 100.
    """
    bridge = fit(linear_pairs())
    lowest = bridge.knots[0][1]
    highest = bridge.knots[-1][1]

    assert bridge.apply(-500.0) == pytest.approx(lowest)
    assert bridge.apply(10_000.0) == pytest.approx(highest)
    assert highest <= APPLE_POPULARITY_CEILING


def test_fit_never_maps_below_apples_floor() -> None:
    """Apple does not report below 5, so the bridge must not claim to."""
    pairs = [(float(i), 0.0) for i in range(20)]
    bridge = fit(pairs)
    assert all(y >= APPLE_POPULARITY_FLOOR for _, y in bridge.knots)
    assert bridge.apply(0.0) == pytest.approx(APPLE_POPULARITY_FLOOR)


def test_ties_on_the_proxy_score_are_averaged_not_duplicated() -> None:
    """A step function cannot separate keywords that scored identically."""
    pairs = [(50.0, 20.0), (50.0, 40.0)] + [(float(i), 5.0 + i) for i in range(20)]
    bridge = fit(pairs)
    # 20 and 40 collapse to one observation at 30, pooled with its neighbours.
    assert bridge.n_overlap == len(pairs)
    xs = [x for x, _ in bridge.knots]
    assert len(xs) == len(set(xs))


def test_bridge_cannot_change_rank_correlation() -> None:
    """The property that makes a before/after Spearman here meaningless.

    A monotone map leaves every rank where it found it. This is asserted rather
    than assumed because it is the reason `score_bridges` stores RMSE.
    """
    proxy = [3.0, 41.0, 12.0, 88.0, 55.0, 7.0, 61.0, 22.0]
    truth = [5.0, 40.0, 20.0, 95.0, 50.0, 10.0, 70.0, 25.0]
    bridge = fit(linear_pairs())

    before = search.spearman(proxy, truth)
    after = search.spearman([bridge.apply(p) for p in proxy], truth)
    assert before == pytest.approx(after)


def test_json_round_trip_preserves_the_map() -> None:
    bridge = fit(linear_pairs())
    restored = Bridge.from_json(bridge.to_json())
    for proxy, _ in linear_pairs():
        assert restored.apply(proxy) == pytest.approx(bridge.apply(proxy), abs=1e-5)


def test_flat_runs_are_collapsed_to_their_endpoints() -> None:
    """A flat run of forty knots is forty ways of writing one segment."""
    pairs = [(float(i), 50.0) for i in range(30)]
    bridge = fit(pairs)
    assert len(bridge.knots) == 2
    assert bridge.apply(15.0) == pytest.approx(50.0)
