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
        return ChartIndex(country=country, ranks={1: 1})


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
