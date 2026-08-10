"""Process-wide state: the shared fetcher, chart indexes, and the job registry.

Held on `app.state.aso` and reachable from any handler via `request.app.state`.

This is not the only chart index in the process: `pipeline.refresh` keeps its
own for the duration of a run. That costs no extra Apple requests, because
`ChartsClient.index` reads through the SQLite charts cache — the second builder
of the day gets the same feeds off disk. The two exist because their lifetimes
differ: one spans a request-serving day, the other one refresh.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..clients.charts import ChartIndex, ChartsClient
from ..config import settings
from ..http import Fetcher
from .jobs import JobRegistry

logger = logging.getLogger(__name__)


@dataclass
class AppState:
    fetcher: Fetcher
    # Keyed by (country, UTC date). The index is a property of the storefront
    # and the day — `charts.CHARTS_TTL_DAYS` expires the SQLite cache daily, so
    # keying on country alone would let a long-running process serve a
    # week-old index from memory and never notice.
    # Entries for past dates are dropped on the next insert: each `ranks` can
    # hold ~4,800 entries (~0.5MB) and the systemd unit is `Restart=always`
    # with no periodic restart, so keeping them would leak ~180MB a year per
    # storefront on a 4GB box.
    chart_indexes: dict[tuple[str, str], ChartIndex] = field(default_factory=dict)
    chart_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    jobs: JobRegistry = field(default_factory=JobRegistry)

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
            if not index:
                # Every feed failed. `ChartsClient.index` never raises, so the
                # caller still gets an answer — with `comp_app_power` absent,
                # which is honest. Caching it would not be: 0.625 of the fitted
                # competition weight would go missing from every lookup until
                # UTC midnight because of one bad minute. Retry on the next
                # request instead.
                logger.warning("chart index for %s loaded no feeds; not cached", country)
                return index
            # Still inside the lock: yesterday's indexes can never be hit
            # again (the key carries the date) and are pure garbage.
            today = key[1]
            for stale in [k for k in self.chart_indexes if k[1] != today]:
                del self.chart_indexes[stale]
            self.chart_indexes[key] = index
            return index
