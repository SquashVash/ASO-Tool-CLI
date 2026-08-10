"""Liveness and provenance: what database is this, and how current is it."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from ... import repository
from ...config import settings
from ...db import applied_versions
from ..deps import get_conn

router = APIRouter(tags=["meta"])


@router.get("/health")
def health(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, object]:
    versions = applied_versions(conn)
    return {
        "status": "ok",
        "db_path": str(settings.db_path),
        "schema_version": max(versions) if versions else 0,
        "keywords": len(repository.list_keywords(conn, active_only=False)),
        "countries": repository.countries(conn),
    }
