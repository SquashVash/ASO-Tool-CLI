"""App factory and lifespan.

The lifespan owns the one `Fetcher` this process gets. Creating it here rather
than per-request is the whole rate-limit design: see the module docstring in
`aso/api/__init__.py`.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ..config import settings
from ..db import init_db
from ..http import Fetcher
from .routes import meta
from .state import AppState

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    async with Fetcher(settings) as fetcher:
        app.state.aso = AppState(fetcher=fetcher)
        logger.info("aso api ready, rate limit %s/min", settings.rate_limit_per_min)
        yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="aso",
        description="Keyword research for the iOS App Store. Loopback only; no auth.",
        lifespan=lifespan,
    )
    app.include_router(meta.router)
    return app
