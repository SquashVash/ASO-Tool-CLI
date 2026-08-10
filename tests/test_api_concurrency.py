"""Overlapping requests.

The rest of the API suite drives the app through `TestClient`, which issues
requests one at a time. That is enough to prove a handler is correct and
nothing at all about what happens when two of them are in flight together —
and the deployment this API was built for is several services on one host
polling it. These tests keep more than one request alive at once.
"""

from __future__ import annotations

import asyncio

import httpx

from aso import db, repository
from aso.api.app import create_app


async def test_ten_overlapping_health_requests_all_succeed():
    """A `def` handler's connection is opened and used on different threads.

    FastAPI resolves a sync `yield` dependency in one `anyio.to_thread.run_sync`
    call and runs the sync path operation in another. AnyIO promises no thread
    affinity between the two, so under concurrency `get_conn` opens the
    connection on worker thread A and the handler — plus the closing
    `__exit__` — touches it on worker thread B. With sqlite3's default
    `check_same_thread=True` that is a `ProgrammingError`, and every `def`
    handler in this API has it: `/health`, `/movers`, `/tags`, `/countries`,
    the six `/keywords*` routes, `/rescore`.
    """
    db.init_db()
    with db.session() as conn:
        repository.add_keyword(conn, "forex", "us")

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            responses = await asyncio.gather(
                *(client.get("/health") for _ in range(10))
            )

    assert [response.status_code for response in responses] == [200] * 10
    assert all(response.json()["status"] == "ok" for response in responses)


async def test_overlapping_keyword_reads_all_succeed():
    """The same hazard on a route that actually queries rows."""
    db.init_db()
    with db.session() as conn:
        repository.add_keyword(conn, "forex", "us")
        repository.add_keyword(conn, "trading", "us")

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            responses = await asyncio.gather(
                *(client.get("/keywords") for _ in range(10))
            )

    assert [response.status_code for response in responses] == [200] * 10
    assert all(len(response.json()) == 2 for response in responses)
