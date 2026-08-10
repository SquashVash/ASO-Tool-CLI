"""FastAPI dependencies."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from ..db import session


def get_conn() -> Iterator[sqlite3.Connection]:
    """One connection per request, closed when it ends.

    `sqlite3` connections are not shareable across threads and read handlers
    run in FastAPI's threadpool, so a process-wide connection would be a bug.
    `db.connect` already sets WAL and a 30s busy timeout, which is what makes a
    read safe alongside a refresh writing snapshots.
    """
    with session() as conn:
        yield conn
