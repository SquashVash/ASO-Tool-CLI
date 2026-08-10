"""Live scoring of any keyword, tracked or not.

Reads through the process-wide response cache (SERP and autocomplete at a
3-day TTL), so a repeat lookup inside that window costs nothing and returns in
milliseconds. Stored scores are never read: the score is always recomputed, so
a weight change in `config.py` shows up on the next call either way.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ... import lookup as lookup_module
from ..schemas import LookupRequest, LookupResponse

router = APIRouter(tags=["lookup"])


@router.post("/lookup", response_model=LookupResponse)
async def lookup(body: LookupRequest, request: Request) -> LookupResponse:
    state = request.app.state.aso
    try:
        charts = await state.chart_index(body.country)
        result = await lookup_module.lookup_async(
            body.keyword,
            body.country,
            force=body.force,
            fetcher=state.fetcher,
            charts=charts,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return LookupResponse.from_result(result)
