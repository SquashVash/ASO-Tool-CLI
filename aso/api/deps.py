"""FastAPI dependencies."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from ..db import session


def get_conn() -> Iterator[sqlite3.Connection]:
    """One connection per request, closed when it ends.

    `check_same_thread=False` is not optional here, and it is not laziness.
    FastAPI resolves a sync (`def`) `yield` dependency in one
    `anyio.to_thread.run_sync` call and then runs the sync path operation in a
    *separate* one. AnyIO guarantees no thread affinity between the two, so
    under concurrency this generator opens the connection on worker thread A
    while the handler — and the `__exit__` that closes it — run on worker
    thread B. With the default `check_same_thread=True`, sqlite3 refuses:
    "SQLite objects created in a thread can only be used in that same thread".
    Ten overlapping `GET /health` requests produced six such 500s.

    Turning the check off is safe *here and only here* because the connection
    is request-scoped: nothing else can reach it, and the thread hop is a
    sequential handoff, never two threads touching it at once. Every other
    caller (CLI, dashboard, job bodies) is single-threaded and keeps the check.

    `db.connect` also sets WAL and a 30s busy timeout, which is what makes a
    read safe alongside a refresh writing snapshots.
    """
    with session(check_same_thread=False) as conn:
        yield conn
