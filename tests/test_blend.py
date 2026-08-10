from __future__ import annotations

import pytest

from aso.config import APPLE_POPULARITY_FLOOR
from aso.scoring.blend import (
    SOURCE_APPLE,
    SOURCE_PROXY,
    SOURCE_PROXY_CENSORED,
    blend,
)
from aso.scoring.bridge import fit


def doubling_bridge():
    """Proxy 0-50 maps onto Apple 5-100. Deliberately not the identity."""
    return fit([(float(i * 2.5), 5.0 + i * 4.75) for i in range(20)])


def test_apple_value_is_used_verbatim() -> None:
    result = blend(92.0, apple_value=41.0, bridge=doubling_bridge())
    assert result is not None
    assert result.value == pytest.approx(41.0)
    assert result.source == SOURCE_APPLE
    assert result.is_measured


def test_proxy_is_bridged_not_used_raw() -> None:
    bridge = doubling_bridge()
    result = blend(25.0, bridge=bridge)
    assert result is not None
    assert result.source == SOURCE_PROXY
    assert result.value == pytest.approx(bridge.apply(25.0))
    assert result.value != pytest.approx(25.0)


def test_censoring_caps_the_proxy_at_apples_floor() -> None:
    """The `finsta` case: proxy says 92, Apple says 'below anything I report'.

    This is the single most corrective fact available about the keywords the
    proxy is worst at, so it must bind.
    """
    result = blend(92.0, censored=True, bridge=doubling_bridge())
    assert result is not None
    assert result.value <= APPLE_POPULARITY_FLOOR
    assert result.source == SOURCE_PROXY_CENSORED


def test_censoring_still_orders_the_tail() -> None:
    """Capped is not flattened — censored keywords keep their relative order."""
    bridge = doubling_bridge()
    low = blend(2.0, censored=True, bridge=bridge)
    high = blend(90.0, censored=True, bridge=bridge)
    assert low is not None and high is not None
    assert low.value <= high.value


def test_censoring_beats_a_contradictory_apple_value() -> None:
    """Apple either scored the term or it did not; censoring is the specific claim."""
    result = blend(50.0, apple_value=80.0, censored=True, bridge=doubling_bridge())
    assert result is not None
    assert result.source == SOURCE_PROXY_CENSORED
    assert result.value <= APPLE_POPULARITY_FLOOR


def test_nothing_measured_at_all_returns_none() -> None:
    """Distinct from a floor score: the caller must be able to leave NULL."""
    assert blend(None) is None
    assert blend(None, apple_value=None) is None


def test_censored_with_no_proxy_still_scores_the_floor() -> None:
    result = blend(None, censored=True)
    assert result is not None
    assert result.value == pytest.approx(APPLE_POPULARITY_FLOOR)
    assert result.source == SOURCE_PROXY_CENSORED


def test_missing_bridge_passes_the_proxy_through_unchanged() -> None:
    """A missing bridge degrades the blend; it must not block it."""
    result = blend(63.5)
    assert result is not None
    assert result.value == pytest.approx(63.5)
    assert result.source == SOURCE_PROXY


def test_apple_values_are_clamped_into_range() -> None:
    assert blend(10.0, apple_value=140.0).value == pytest.approx(100.0)
    assert blend(10.0, apple_value=1.0).value == pytest.approx(APPLE_POPULARITY_FLOOR)
