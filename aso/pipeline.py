"""Orchestration: refresh a set of keywords and write a snapshot each.

Per keyword, in order:

1. fetch the SERP (cached 3 days) and score competition,
2. walk the autocomplete prefix ladder (cached 3 days) and score search volume,
3. combine into opportunity,
4. write one `snapshots` row plus the ranking into `serps`.

Three properties this module exists to guarantee:

**Failures are recorded, not fatal.** A keyword whose SERP or hints can't be
fetched still gets a snapshot row, with `fetch_failed = 1`, the error text, and
whatever scores did succeed. The run continues. A 500-keyword refresh must not
die on keyword 300 because Apple rate-limited one request.

**Partial results stay partial.** If the SERP succeeds and hints fail, the
competition score and its components are written and the search score stays
NULL — never zero, never a guess. `opportunity` is NULL whenever either input
is, so a half-measured keyword can't outrank a fully measured one.

**Runs are resumable.** Each keyword commits its own transaction, and every
response is cached on disk, so Ctrl-C and restart costs only the keyword that
was in flight.

Keywords are processed sequentially. Concurrency would buy nothing: the shared
token bucket serializes every request anyway, and interleaving transactions on
one SQLite connection would break the per-keyword atomicity above.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .clients.hints import HintsClient, UnknownStorefront
from .clients.itunes import ITunesClient
from .config import Settings
from .config import settings as default_settings
from .db import transaction, utcnow
from .http import Fetcher, FetchError
from .repository import SnapshotWrite, write_serp, write_snapshot
from .scoring import competition, search
from .scoring.opportunity import opportunity

logger = logging.getLogger(__name__)

# Errors that mean "this keyword failed" rather than "the run is broken".
# ValueError covers unparseable responses from either endpoint.
RECOVERABLE = (FetchError, ValueError, UnknownStorefront)


@dataclass
class KeywordOutcome:
    keyword_id: int
    keyword: str
    country: str
    search_score: float | None = None
    competition_score: float | None = None
    opportunity_score: float | None = None
    prefix_depth: int | None = None
    hint_rank: int | None = None
    serp_size: int = 0
    ladder_queries: int = 0
    failed: bool = False
    error: str | None = None

    @property
    def status(self) -> str:
        if not self.failed:
            return "ok"
        return "partial" if self.competition_score is not None else "failed"


@dataclass
class RefreshReport:
    outcomes: list[KeywordOutcome] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    requests_made: int = 0
    retries: int = 0

    @property
    def succeeded(self) -> int:
        return sum(1 for o in self.outcomes if not o.failed)

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if o.failed)

    @property
    def duration_seconds(self) -> float:
        if not self.started_at or not self.finished_at:
            return 0.0
        start = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(self.finished_at.replace("Z", "+00:00"))
        return (end - start).total_seconds()


ProgressCallback = Callable[[KeywordOutcome], None]


async def refresh_keyword(
    keyword_id: int,
    keyword: str,
    country: str,
    *,
    conn: sqlite3.Connection,
    itunes: ITunesClient,
    hints: HintsClient,
    force: bool = False,
    config: Settings | None = None,
    now: datetime | None = None,
) -> KeywordOutcome:
    """Refresh one keyword and write its snapshot. Never raises for a fetch failure."""
    config = config or default_settings
    outcome = KeywordOutcome(keyword_id=keyword_id, keyword=keyword, country=country)
    captured_at = utcnow()
    errors: list[str] = []

    # --- competition -------------------------------------------------------
    comp_result: competition.CompetitionResult | None = None
    serp_captured_at: str | None = None
    track_ids: list[int] = []
    try:
        serp = await itunes.search(keyword, country, force=force)
        comp_result = competition.score(serp, now=now)
        serp_captured_at = serp.captured_at
        track_ids = [app.track_id for app in serp.apps]
        outcome.serp_size = len(serp.apps)
        outcome.competition_score = comp_result.score
    except RECOVERABLE as exc:
        logger.warning("serp failed for %r (%s): %s", keyword, country, exc)
        errors.append(f"serp: {exc}")

    # --- search volume -----------------------------------------------------
    observation: search.LadderObservation | None = None
    try:

        async def probe(prefix: str) -> Sequence[str]:
            return (await hints.suggest(prefix, country, force=force)).terms

        observation = await search.observe(keyword, probe)
        outcome.search_score = search.score(observation)
        outcome.prefix_depth = observation.prefix_depth
        outcome.hint_rank = observation.hint_rank
        outcome.ladder_queries = observation.queries_used
    except RECOVERABLE as exc:
        logger.warning("hints failed for %r (%s): %s", keyword, country, exc)
        errors.append(f"hints: {exc}")

    # --- combine and persist ----------------------------------------------
    outcome.opportunity_score = opportunity(
        outcome.search_score, outcome.competition_score
    )
    outcome.failed = bool(errors)
    outcome.error = "; ".join(errors) if errors else None

    components = (
        comp_result.components.as_dict()
        if comp_result is not None
        else {name: None for name in competition.COMPETITION_WEIGHTS}
    )
    snapshot = SnapshotWrite(
        keyword_id=keyword_id,
        captured_at=captured_at,
        search_score=outcome.search_score,
        competition_score=outcome.competition_score,
        opportunity_score=outcome.opportunity_score,
        search_prefix_depth=outcome.prefix_depth,
        search_hint_rank=outcome.hint_rank,
        fetch_failed=outcome.failed,
        fetch_error=outcome.error,
        **components,
    )

    with transaction(conn):
        write_snapshot(conn, snapshot)
        if serp_captured_at is not None and track_ids:
            write_serp(conn, keyword_id, serp_captured_at, track_ids)

    return outcome


async def refresh(
    conn: sqlite3.Connection,
    keywords: Sequence[sqlite3.Row],
    *,
    force: bool = False,
    config: Settings | None = None,
    on_progress: ProgressCallback | None = None,
    fetcher: Fetcher | None = None,
) -> RefreshReport:
    """Refresh every keyword in `keywords`, writing one snapshot each.

    A single `Fetcher` is shared by both clients so iTunes and autocomplete
    traffic draw on the same per-IP rate limit.
    """
    config = config or default_settings
    report = RefreshReport(started_at=utcnow())

    async def run(active: Fetcher) -> None:
        itunes = ITunesClient(active, conn, config)
        hints = HintsClient(active, conn, config)
        for row in keywords:
            outcome = await refresh_keyword(
                row["id"],
                row["keyword"],
                row["country"],
                conn=conn,
                itunes=itunes,
                hints=hints,
                force=force,
                config=config,
                now=datetime.now(timezone.utc),
            )
            report.outcomes.append(outcome)
            if on_progress is not None:
                on_progress(outcome)

    if fetcher is not None:
        await run(fetcher)
        active_fetcher = fetcher
    else:
        async with Fetcher(config) as owned:
            await run(owned)
            active_fetcher = owned

    report.finished_at = utcnow()
    report.requests_made = active_fetcher.requests_made
    report.retries = active_fetcher.retries
    return report
