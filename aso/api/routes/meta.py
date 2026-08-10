"""Liveness and provenance: what database is this, and how current is it."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query

from ... import repository
from ...config import settings
from ...db import applied_versions
from ..deps import get_conn
from ..schemas import MoverRow

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


@router.get("/movers", response_model=list[MoverRow])
def movers(
    days: int = Query(7, ge=1),
    country: str | None = None,
    tag: str | None = None,
    include_inactive: bool = False,
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[MoverRow]:
    rows = repository.score_movers(
        conn, days=days, country=country, tag=tag, active_only=not include_inactive
    )
    return [MoverRow.from_row(row) for row in rows]


@router.get("/tags", response_model=list[str])
def tags(conn: sqlite3.Connection = Depends(get_conn)) -> list[str]:
    return repository.all_tags(conn)


@router.get("/countries", response_model=list[str])
def countries(conn: sqlite3.Connection = Depends(get_conn)) -> list[str]:
    return repository.countries(conn)
