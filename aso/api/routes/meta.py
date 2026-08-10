"""Liveness and provenance: what data is loaded, and how much of it."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ... import calibration
from ...config import settings
from ...store import Store
from ..deps import get_store

router = APIRouter(tags=["meta"])


@router.get("/health")
def health(store: Store = Depends(get_store)) -> dict[str, object]:
    """Enough to tell a live server from a live server pointed at nothing.

    The counts are the useful part. A server reading an empty `data/` still
    answers 200 on every endpoint — it just scores everything off the proxy —
    so "ok" alone would not distinguish a healthy install from one whose
    calibration files never got deployed.
    """
    return {
        "status": "ok",
        "data_dir": str(settings.data_dir),
        "keywords": len(store.records),
        "countries": store.countries(),
        "demand_observations": len(calibration.demand_observations()),
        "bridges": len(calibration.bridges()),
    }


@router.get("/tags", response_model=list[str])
def tags(store: Store = Depends(get_store)) -> list[str]:
    return store.all_tags()


@router.get("/countries", response_model=list[str])
def countries(store: Store = Depends(get_store)) -> list[str]:
    return store.countries()
