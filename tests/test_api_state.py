"""The process-wide chart index cache."""

from __future__ import annotations

from aso import db
from aso.api.state import AppState
from aso.clients.charts import ChartIndex


class CountingCharts:
    def __init__(self) -> None:
        self.calls = 0

    async def index(self, country: str, *, force: bool = False) -> ChartIndex:
        self.calls += 1
        return ChartIndex(country=country, ranks={1: 1}, charts_loaded=48)


async def test_chart_index_is_built_once_per_country_per_day(monkeypatch):
    from aso.api import state as state_module

    charts = CountingCharts()
    monkeypatch.setattr(state_module, "ChartsClient", lambda *a, **kw: charts)

    app_state = AppState(fetcher=object())
    db.init_db()
    with db.session() as conn:
        first = await app_state.chart_index(conn, "us")
        second = await app_state.chart_index(conn, "us")

    assert first is second
    assert charts.calls == 1, "48 chart requests must not be repeated per lookup"


class FailingThenWorkingCharts:
    """First build loads no feeds, the second loads one.

    `ChartIndex.__bool__` is `charts_loaded > 0`, so the first index is falsey:
    every feed failed, and it proves nothing about what charts.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def index(self, country: str, *, force: bool = False) -> ChartIndex:
        self.calls += 1
        if self.calls == 1:
            return ChartIndex(country=country, ranks={}, charts_loaded=0)
        return ChartIndex(country=country, ranks={1: 1}, charts_loaded=48)


async def test_an_index_that_loaded_nothing_is_returned_but_not_cached(monkeypatch):
    """A minute of failed feeds must not cost the storefront its whole day.

    `comp_app_power` carries 0.625 of the fitted competition weight; caching an
    empty index would drop it from every lookup until UTC midnight.
    """
    from aso.api import state as state_module

    charts = FailingThenWorkingCharts()
    monkeypatch.setattr(state_module, "ChartsClient", lambda *a, **kw: charts)

    app_state = AppState(fetcher=object())
    db.init_db()
    with db.session() as conn:
        empty = await app_state.chart_index(conn, "us")
        rebuilt = await app_state.chart_index(conn, "us")
        cached = await app_state.chart_index(conn, "us")

    assert not empty, "the caller still gets an answer, just an empty one"
    assert charts.calls == 2, "the empty index must not have been cached"
    assert rebuilt is cached, "the index that did load is cached as usual"


async def test_a_stale_dated_index_is_evicted_when_a_new_day_is_built(monkeypatch):
    """Yesterday's index is unreachable — the key carries the date — and each
    one holds ~4,800 ranks. The API process is `Restart=always` with no
    periodic restart, so nothing else would ever free them."""
    from aso.api import state as state_module

    charts = CountingCharts()
    monkeypatch.setattr(state_module, "ChartsClient", lambda *a, **kw: charts)

    app_state = AppState(fetcher=object())
    stale = ("us", "2020-01-01")
    app_state.chart_indexes[stale] = ChartIndex(
        country="us", ranks={1: 1}, charts_loaded=48
    )
    db.init_db()
    with db.session() as conn:
        await app_state.chart_index(conn, "gb")

    assert stale not in app_state.chart_indexes
    assert len(app_state.chart_indexes) == 1


async def test_chart_index_is_keyed_by_country(monkeypatch):
    from aso.api import state as state_module

    charts = CountingCharts()
    monkeypatch.setattr(state_module, "ChartsClient", lambda *a, **kw: charts)

    app_state = AppState(fetcher=object())
    db.init_db()
    with db.session() as conn:
        await app_state.chart_index(conn, "us")
        await app_state.chart_index(conn, "gb")

    assert charts.calls == 2
