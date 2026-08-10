"""Process-wide state: the shared fetcher, chart indexes, and the job registry.

Held on `app.state.aso` and reachable from any handler via `request.app.state`.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field

from ..clients.charts import ChartIndex
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
