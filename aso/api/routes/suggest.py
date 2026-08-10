"""Keyword discovery: what Apple offers to someone typing toward this term.

Reads through the process-wide response cache at the 3-day hints TTL, so a
repeat inside that window costs nothing. Nothing is written and nothing is
scored — see `aso/suggest.py` for why pricing the candidates is a separate,
far more expensive job.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ... import suggest as suggest_module
from ...clients.hints import UnknownStorefront
from ...store import Store
from ..deps import get_store
from ..schemas import SuggestRequest, SuggestResponse

router = APIRouter(tags=["suggest"])


@router.post("/suggest", response_model=SuggestResponse)
async def suggest(
    body: SuggestRequest, request: Request, store: Store = Depends(get_store)
) -> SuggestResponse:
    """Walk every rung of the prefix ladder and return what Apple suggested.

    Synchronous despite costing up to 12 paced requests (~48s for a long
    keyword): `/lookup` already blocks longer building a chart index, and under
    a minute does not warrant the job registry. Set your client timeout
    accordingly.
    """
    state = request.app.state.aso
    try:
        result = await suggest_module.suggest_async(
            body.keyword,
            body.country,
            force=body.force,
            include_tracked=body.include_tracked,
            fetcher=state.fetcher,
            store=store,
        )
    except (ValueError, UnknownStorefront) as exc:
        # Both are caller error and both are raised before any request goes
        # out: a blank keyword, or a country with no Apple storefront id.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SuggestResponse.from_result(result)
