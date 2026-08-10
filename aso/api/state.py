"""Process-wide state: the shared fetcher, chart indexes, and the job registry.

Held on `app.state.aso` and reachable from any handler via `request.app.state`.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..clients.charts import ChartIndex, ChartsClient
from ..config import settings
from ..http import Fetcher


@dataclass
class AppState:
    fetcher: Fetcher
    # Keyed by (country, UTC date). The index is a property of the storefront
    # and the day — `charts.CHARTS_TTL_DAYS` expires the SQLite cache daily, so
    # keying on country alone would let a long-running process serve a
    # week-old index from memory and never notice.
    chart_indexes: dict[tuple[str, str], ChartIndex] = field(default_factory=dict)
    chart_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def chart_index(self, conn: sqlite3.Connection, country: str) -> ChartIndex:
        """The storefront's chart index, built at most once per country per day.

        Costs ~48 requests (about 3.5 minutes at the paced rate) the first time
        a storefront is asked for on a given day, and nothing afterwards. The
        lock matters: two lookups arriving together would otherwise each pay
        that price for the same answer.
        """
        key = (country.lower(), datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        async with self.chart_lock:
            cached = self.chart_indexes.get(key)
            if cached is not None:
                return cached
            client = ChartsClient(self.fetcher, conn, settings)
            index = await client.index(country)
            self.chart_indexes[key] = index
            return index
