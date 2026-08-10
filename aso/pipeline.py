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

import dataclasses
import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from . import cache
from .clients import apple_popularity, asa, hints
from .clients.charts import ChartIndex, ChartsClient
from .clients.hints import HintsClient, UnknownStorefront
from .clients.itunes import ITunesClient
from .config import (
    APPLE_POPULARITY_RATE_PER_MIN,
    ASA_RATE_LIMIT_PER_MIN,
    COMPETITION_BRIDGE_SOURCE,
    Settings,
)
from .config import settings as default_settings
from .db import transaction, utcnow
from .http import Fetcher, FetchError
from .repository import (
    ASATermWrite,
    DemandWrite,
    SnapshotWrite,
    all_snapshots_for_rescoring,
    apple_demand_map,
    calibration_samples,
    latest_bridge,
    update_snapshot_scores,
    write_asa_terms,
    write_demand_observations,
    write_serp,
    write_snapshot,
)
from .scoring import competition, search
from .scoring.blend import blend
from .scoring.bridge import Bridge
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
    # The unblended autocomplete + SERP score, kept when a measured value
    # replaces it — so an ad-hoc check can show both what Apple says and what
    # the proxy guessed, which is the interesting comparison.
    search_score_proxy: float | None = None
    # Which ruler produced `search_score`. None until `blend_outcome` runs.
    search_source: str | None = None
    # The competition score before the level bridge. Kept beside the final for
    # the same reason `search_score_proxy` is: it is what stops a bridged score
    # being bridged again, and it is the interesting comparison to show.
    competition_score_raw: float | None = None
    prefix_depth: int | None = None
    hint_rank: int | None = None
    hint_extensions: int | None = None
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


@dataclass
class ScoredKeyword:
    """Everything one scoring pass learned, before anything is persisted.

    Split out from `refresh_keyword` so a keyword can be scored *without*
    being tracked — see `score_keyword` and `aso check`. Both paths run the
    identical scoring code; only persistence differs.
    """

    outcome: KeywordOutcome
    comp_result: competition.CompetitionResult | None
    observation: search.LadderObservation | None
    serp: object | None = None

    @property
    def components(self) -> dict[str, float | None]:
        if self.comp_result is None:
            return {name: None for name in competition.COMPETITION_WEIGHTS}
        return self.comp_result.components.as_dict()


async def score_keyword(
    keyword: str,
    country: str,
    *,
    itunes: ITunesClient,
    hints: HintsClient,
    force: bool = False,
    now: datetime | None = None,
    keyword_id: int = 0,
    charts: ChartIndex | None = None,
) -> ScoredKeyword:
    """Fetch and score one keyword. Touches no table, never raises on a fetch failure.

    The HTTP response cache *is* written, because that is a cache rather than a
    record: it makes a later `refresh` of the same keyword free, and it is what
    makes an ad-hoc `aso check` cheap to repeat.

    `charts` is optional and defaults to absent, which makes `comp_app_power`
    `None` and leaves `combine()` to renormalize. That is the honest answer for
    a caller that did not pay for the index, and it keeps this function usable
    without the 48 chart requests a full storefront index costs.
    """
    outcome = KeywordOutcome(keyword_id=keyword_id, keyword=keyword, country=country)
    errors: list[str] = []

    comp_result: competition.CompetitionResult | None = None
    serp = None
    try:
        serp = await itunes.search(keyword, country, force=force)
        comp_result = competition.score(serp, now=now, charts=charts)
        outcome.serp_size = len(serp.apps)
        outcome.competition_score = comp_result.score
    except RECOVERABLE as exc:
        logger.warning("serp failed for %r (%s): %s", keyword, country, exc)
        errors.append(f"serp: {exc}")

    observation: search.LadderObservation | None = None
    try:

        async def probe(prefix: str) -> Sequence[str]:
            return (await hints.suggest(prefix, country, force=force)).terms

        observation = await search.observe(keyword, probe)
        # Rating mass now carries no demand weight (SEARCH_RATING_MASS_WEIGHT
        # is 0.0 — it moved to comp_incumbent), so this argument currently
        # changes nothing. The plumbing stays, exactly as it does for the
        # zero-weighted savings and rank components: the observation is still
        # captured, and reviving it is a config edit rather than a code change.
        outcome.search_score = search.score(
            observation,
            rating_mass=(
                comp_result.components.comp_rating_count
                if comp_result is not None
                else None
            ),
        )
        outcome.prefix_depth = observation.prefix_depth
        outcome.hint_rank = observation.hint_rank
        outcome.hint_extensions = observation.extensions
        outcome.ladder_queries = observation.queries_used
    except RECOVERABLE as exc:
        logger.warning("hints failed for %r (%s): %s", keyword, country, exc)
        errors.append(f"hints: {exc}")

    outcome.opportunity_score = opportunity(
        outcome.search_score, outcome.competition_score
    )
    outcome.failed = bool(errors)
    outcome.error = "; ".join(errors) if errors else None

    return ScoredKeyword(
        outcome=outcome, comp_result=comp_result, observation=observation, serp=serp
    )


def blend_outcome(
    conn: sqlite3.Connection, outcome: KeywordOutcome
) -> KeywordOutcome:
    """Apply a measured demand value to an ad-hoc score, in place.

    `score_keyword` deliberately touches no table, so it can only ever produce
    the proxy. That is correct for what it is and wrong for what a caller
    wants: a keyword we have already measured should not be reported at its
    guessed value just because this code path does not read the database.

    Without this, `aso check "75 hard"` answered 70.7 while every stored
    snapshot of the same keyword read 9.0 — the same keyword, two numbers, and
    no indication which to believe. The blend belongs wherever a demand score
    is reported, not only where one is written.

    Recomputes `opportunity` too, since it multiplies by the score that just
    changed.
    """
    outcome.search_score_proxy = outcome.search_score
    value, censored = apple_demand_map(conn).get(
        (search.normalize_keyword(outcome.keyword), outcome.country), (None, False)
    )
    row = latest_bridge(conn, country=outcome.country)
    fitted = (
        Bridge.from_json(row["knots"], n_overlap=row["n_overlap"], rmse=row["rmse"] or 0.0)
        if row is not None
        else None
    )

    blended = blend(
        outcome.search_score, apple_value=value, censored=censored, bridge=fitted
    )
    if blended is not None:
        outcome.search_score = blended.value
        outcome.search_source = blended.source
    outcome.opportunity_score = opportunity(
        outcome.search_score, outcome.competition_score
    )
    return outcome


def bridge_outcome(
    conn: sqlite3.Connection,
    outcome: KeywordOutcome,
    *,
    bridge: Bridge | None = None,
) -> KeywordOutcome:
    """Apply the competition level bridge to a scored outcome, in place.

    The competition-side twin of `blend_outcome`, and it exists for the same
    reason. `score_keyword` touches no table, so it can only ever produce the
    raw score — correct for what that function is, and wrong for what every
    caller wants. A keyword whose storefront has a fitted scale must not be
    reported on the unfitted one just because this code path does not read the
    database.

    Without this, `aso check "habit tracker"` answered 42.0 while a stored
    snapshot of the same keyword read 80-odd: the same keyword, two numbers,
    and no indication which to believe. Two commands documented as running the
    same scoring code must not disagree.

    `bridge` may be passed to avoid a lookup per keyword in a long run.

    Recomputes `opportunity` too, since it multiplies by the score that just
    changed — linearly, which is why the level matters here at all.
    """
    outcome.competition_score_raw = outcome.competition_score
    fitted = bridge if bridge is not None else _load_competition_bridge(
        conn, outcome.country
    )
    if fitted is not None and outcome.competition_score is not None:
        outcome.competition_score = fitted.apply(outcome.competition_score)
    outcome.opportunity_score = opportunity(
        outcome.search_score, outcome.competition_score
    )
    return outcome


def _load_competition_bridge(
    conn: sqlite3.Connection, country: str
) -> Bridge | None:
    """The stored competition level bridge for one storefront, if any.

    `metric="competition"` is what keeps this from picking up a demand bridge
    fitted for the same storefront — see db.py migration 16.
    """
    row = latest_bridge(
        conn,
        country=country,
        source=COMPETITION_BRIDGE_SOURCE,
        metric="competition",
    )
    if row is None:
        return None
    return Bridge.from_json(
        row["knots"], n_overlap=row["n_overlap"], rmse=row["rmse"] or 0.0
    )


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
    charts: ChartIndex | None = None,
    competition_bridge: Bridge | None = None,
) -> KeywordOutcome:
    """Refresh one keyword and write its snapshot. Never raises for a fetch failure.

    `competition_bridge` is looked up per storefront when not supplied. `refresh`
    passes it so a 500-keyword run does one lookup per country instead of 500.
    """
    config = config or default_settings
    captured_at = utcnow()

    scored = await score_keyword(
        keyword,
        country,
        itunes=itunes,
        hints=hints,
        force=force,
        now=now,
        keyword_id=keyword_id,
        charts=charts,
    )
    outcome = scored.outcome
    serp_captured_at = scored.serp.captured_at if scored.serp is not None else None
    track_ids = (
        [app.track_id for app in scored.serp.apps] if scored.serp is not None else []
    )

    bridge_outcome(conn, outcome, bridge=competition_bridge)

    snapshot = SnapshotWrite(
        keyword_id=keyword_id,
        captured_at=captured_at,
        search_score=outcome.search_score,
        competition_score=outcome.competition_score,
        competition_score_raw=outcome.competition_score_raw,
        opportunity_score=outcome.opportunity_score,
        search_prefix_depth=outcome.prefix_depth,
        search_hint_rank=outcome.hint_rank,
        search_hint_extensions=outcome.hint_extensions,
        fetch_failed=outcome.failed,
        fetch_error=outcome.error,
        **scored.components,
    )

    with transaction(conn):
        write_snapshot(conn, snapshot)
        if serp_captured_at is not None and track_ids:
            write_serp(conn, keyword_id, serp_captured_at, track_ids)

    return outcome


@dataclass
class RescoreReport:
    total: int = 0
    changed: int = 0
    largest_move: float = 0.0
    largest_move_keyword: str | None = None
    backfilled_extensions: int = 0


def backfill_extensions(conn: sqlite3.Connection) -> int:
    """Fill `search_hint_extensions` on snapshots taken before it existed.

    Reads the local HTTP cache, not Apple: every ladder walk's first rung is
    the keyword's own full-length prefix, so the suggestion list this count
    comes from was already fetched and stored. Recovering it needs no request.

    Returns how many rows were filled. Snapshots whose cached response has
    since been purged keep NULL, which the scorer reads as "not measured" and
    renormalizes around — a wrong 0 there would claim Apple offered no
    completions when nobody looked.

    Only ever writes NULL cells. A stored count is a measurement and is never
    overwritten by a re-derived one.
    """
    rows = conn.execute(
        """
        SELECT s.id, k.keyword, k.country
        FROM snapshots s
        JOIN keywords k ON k.id = s.keyword_id
        WHERE s.search_hint_extensions IS NULL
          AND s.fetch_failed = 0
        """
    ).fetchall()
    filled = 0
    for row in rows:
        keyword = search.normalize_keyword(row["keyword"])
        cached = conn.execute(
            "SELECT body FROM http_cache WHERE cache_key = ?",
            (cache.hints_key(keyword, row["country"]),),
        ).fetchone()
        if cached is None:
            continue
        try:
            terms = hints.parse_hints(cached["body"])
        except ValueError:
            logger.warning("cached hints for %r were unparseable", keyword)
            continue
        conn.execute(
            "UPDATE snapshots SET search_hint_extensions = ? WHERE id = ?",
            (search._extensions_of(keyword, terms), row["id"]),
        )
        filled += 1
    return filled


def rescore(conn: sqlite3.Connection, *, config: Settings | None = None) -> RescoreReport:
    """Recompute every stored snapshot's three finals from its own components.

    Makes no network requests. This is the payoff for storing components next
    to finals: change `COMPETITION_WEIGHTS` or the search mapping and the whole
    history moves with it, so trend charts compare like with like instead of
    silently splicing two different scoring schemes together.

    Only the finals are rewritten. Components and the raw ladder observations
    are inputs and stay exactly as captured — with the single exception of
    `backfill_extensions`, which fills a column that did not exist when older
    snapshots were written and derives it from the cached response those very
    snapshots were built from.
    """
    config = config or default_settings
    report = RescoreReport()

    # Apple's readings and the fitted bridges, loaded once. Both are empty on a
    # tool that has never pulled popularity, and every branch below then takes
    # its pre-blend path — which is why enabling none of this changes any score.
    demand = apple_demand_map(conn)
    bridges: dict[str, Bridge | None] = {}

    def bridge_for(country: str) -> Bridge | None:
        if country not in bridges:
            row = latest_bridge(conn, country=country)
            bridges[country] = (
                Bridge.from_json(row["knots"], n_overlap=row["n_overlap"], rmse=row["rmse"] or 0.0)
                if row is not None
                else None
            )
        return bridges[country]

    # The competition-side twin, keyed separately. Same object, different
    # metric: `latest_bridge`'s `metric` argument is what keeps a demand
    # bridge and a competition bridge from being mistaken for each other.
    comp_bridges: dict[str, Bridge | None] = {}

    def comp_bridge_for(country: str) -> Bridge | None:
        if country not in comp_bridges:
            row = latest_bridge(
                conn,
                country=country,
                source=COMPETITION_BRIDGE_SOURCE,
                metric="competition",
            )
            comp_bridges[country] = (
                Bridge.from_json(row["knots"], n_overlap=row["n_overlap"], rmse=row["rmse"] or 0.0)
                if row is not None
                else None
            )
        return comp_bridges[country]

    with transaction(conn):
        report.backfilled_extensions = backfill_extensions(conn)

        for row in all_snapshots_for_rescoring(conn):
            report.total += 1

            # `derive` recomputes comp_incumbent from the two stored
            # measurements rather than reading the stored column, so a change
            # to the formula reaches history here instead of being frozen into
            # whatever each row was captured under.
            comp_components = competition.derive(
                {name: row[name] for name in competition.COMPETITION_WEIGHTS}
            )
            comp_raw = competition.combine(
                comp_components, weights=config.competition_weights
            )
            # The bridge maps our raw score onto the vendor's level. It is
            # applied here rather than folded into `combine` so the pre-bridge
            # number survives in its own column — which is what stops the next
            # rescore bridging an already-bridged score, exactly as
            # `search_score_proxy` does on the demand side.
            comp_bridge = comp_bridge_for(row["keyword_country"])
            comp_score = (
                comp_raw
                if comp_raw is None or comp_bridge is None
                else comp_bridge.apply(comp_raw)
            )

            depth = row["search_prefix_depth"]
            rank = row["search_hint_rank"]
            if depth is None and rank is None and row["search_score"] is None:
                # Hints never succeeded for this snapshot. Nothing to recompute
                # from, and inventing the no-match floor would turn a failed
                # fetch into a measurement.
                proxy_score = None
            else:
                length = len(search.normalize_keyword(row["keyword_text"]))
                proxy_score = search.score_from_observations(
                    depth,
                    rank,
                    length,
                    extensions=row["search_hint_extensions"],
                    # Zero-weighted today; passed so that re-enabling it in
                    # config re-scores history without a schema change.
                    rating_mass=row["comp_rating_count"],
                )

            # Blend Apple's measurement over the proxy where one exists. With
            # no popularity pulled, `measured` is (None, False) for every
            # keyword and `blend` returns the bridged — i.e. unchanged — proxy.
            country = row["keyword_country"]
            apple_value, censored = demand.get(
                (search.normalize_keyword(row["keyword_text"]), country), (None, False)
            )
            blended = blend(
                proxy_score,
                apple_value=apple_value,
                censored=censored,
                bridge=bridge_for(country),
            )
            search_score = blended.value if blended is not None else None
            search_source = blended.source if blended is not None else None

            opp = opportunity(search_score, comp_score)

            before = row["opportunity_score"]
            update_snapshot_scores(
                conn,
                row["id"],
                search_score=search_score,
                competition_score=comp_score,
                opportunity_score=opp,
                search_score_proxy=proxy_score,
                search_source=search_source,
                comp_incumbent=comp_components["comp_incumbent"],
                competition_score_raw=comp_raw,
            )
            if before != opp:
                report.changed += 1
                if before is not None and opp is not None:
                    move = abs(opp - before)
                    if move > report.largest_move:
                        report.largest_move = move
                        report.largest_move_keyword = row["keyword_text"]

    return report


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
        charts_client = ChartsClient(active, conn, config)
        # One index per storefront, built on first use and reused for every
        # keyword in that market. The index is a property of the country and
        # the day, not of the keyword, so building it per keyword would repeat
        # 48 requests for an identical answer. `force` is deliberately not
        # passed through: it means "refetch this keyword", and re-pulling the
        # whole chart index behind it would make a one-keyword refresh cost 49
        # requests. `charts.CHARTS_TTL_DAYS` already expires it daily.
        indexes: dict[str, ChartIndex] = {}
        comp_bridges: dict[str, Bridge | None] = {}

        for row in keywords:
            country = row["country"].lower()
            if country not in indexes:
                indexes[country] = await charts_client.index(country)
            if country not in comp_bridges:
                comp_bridges[country] = _load_competition_bridge(conn, country)
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
                charts=indexes[country],
                competition_bridge=comp_bridges[country],
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


# ---------------------------------------------------------------------------
# Apple Search Ads
# ---------------------------------------------------------------------------


@dataclass
class ASAPullReport:
    campaigns_seen: int = 0
    campaigns_skipped: list[str] = field(default_factory=list)
    terms_written: int = 0
    start: str = ""
    end: str = ""
    requests_made: int = 0


async def pull_asa(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
    config: Settings | None = None,
    fetcher: Fetcher | None = None,
    client: asa.ASAClient | None = None,
) -> ASAPullReport:
    """Pull search-term impressions for every campaign in the configured org.

    Each campaign commits its own transaction, so a credential expiring or a
    report timing out halfway through leaves the campaigns already pulled
    safely stored — same resumability property as `refresh`.

    Campaigns targeting more than one country are pulled but stored with a NULL
    country, which excludes them from calibration. Attributing a search term to
    one of several storefronts would be a guess, and calibration is the one
    place in this tool where a guess defeats the entire purpose.
    """
    config = config or default_settings
    report = ASAPullReport(start=start.isoformat(), end=end.isoformat())

    async def run(active: Fetcher) -> None:
        api = client or asa.ASAClient(active, config)
        org_id = config.asa.org_id or ""

        for campaign in await api.campaigns():
            report.campaigns_seen += 1
            country = campaign.country
            if country is None:
                report.campaigns_skipped.append(
                    f"{campaign.name or campaign.campaign_id} "
                    f"(targets {len(campaign.countries)} countries)"
                )

            rows = await api.search_terms(campaign.campaign_id, start=start, end=end)
            writes = [
                ASATermWrite(
                    org_id=org_id,
                    campaign_id=campaign.campaign_id,
                    search_term=row.search_term,
                    country=country,
                    impressions=row.impressions,
                    taps=row.taps,
                    installs=row.installs,
                    start_date=report.start,
                    end_date=report.end,
                )
                for row in rows
            ]
            # Also project into the flat demand table, which is what
            # calibration reads. Only single-country campaigns qualify: a term
            # from a multi-country campaign cannot be attributed to a
            # storefront, and calibration joins on (keyword, country).
            demand = (
                [
                    DemandWrite(
                        source="asa",
                        scale=search.SCALE_COUNT,
                        keyword=row.search_term,
                        country=country,
                        value=float(row.impressions),
                    )
                    for row in rows
                    if row.impressions > 0
                ]
                if country is not None
                else []
            )

            with transaction(conn):
                report.terms_written += write_asa_terms(conn, writes)
                write_demand_observations(conn, demand)

        report.requests_made = active.requests_made

    if fetcher is not None:
        await run(fetcher)
    else:
        async with Fetcher(dataclasses.replace(
            config, rate_limit_per_min=ASA_RATE_LIMIT_PER_MIN
        )) as active:
            await run(active)

    return report


def calibration_from_db(
    conn: sqlite3.Connection, source: str
) -> list[search.CalibrationSample]:
    """Build calibration samples for one source's measured demand.

    One source at a time, deliberately. An impression count and an ordinal
    popularity score are not comparable quantities, and averaging them would
    produce a number that means nothing.
    """
    samples = []
    for row in calibration_samples(conn, source):
        length = len(search.normalize_keyword(row["keyword"]))
        samples.append(
            search.CalibrationSample(
                prefix_depth=row["prefix_depth"],
                hint_rank=row["hint_rank"],
                keyword_length=length,
                impressions=float(row["value"]),
                scale=row["scale"],
                extensions=row["extensions"] or 0,
                rating_mass=row["rating_mass"] or 0.0,
            )
        )
    return samples


@dataclass
class PopularityPullReport:
    """What one popularity pull actually learned."""

    requested: int = 0
    scored: int = 0
    censored: int = 0
    # Keywords Apple never mentioned in its response. NOT recorded as
    # observations — see clients/apple_popularity on why absence is not
    # censoring.
    # Terms that arrived attached to another keyword's seed and were stored.
    related: int = 0
    from_cache: int = 0


async def pull_apple_popularity(
    conn: sqlite3.Connection,
    keywords: Sequence[str],
    *,
    country: str,
    config: Settings | None = None,
    fetcher: Fetcher | None = None,
    client: "apple_popularity.ApplePopularityClient | None" = None,
    store_related: bool = True,
) -> PopularityPullReport:
    """Fetch Apple's popularity index and store it as demand observations.

    Writes into the same `demand_observations` table AppFigures and ASA use,
    under source 'apple' and scale 'ordinal_100'. Nothing about calibration or
    scoring needs to learn a new kind of thing exists — which is what that
    table's `source` column was for.

    Censored readings are stored with `censored = 1`. They train nothing
    (`calibration_samples` filters `value > 0`) and bound everything
    (`scoring.blend` caps the proxy at Apple's floor for them).

    `store_related` also keeps the terms that arrived attached to somebody
    else's seed. Those are real Apple measurements obtained for no extra
    request, and they are what makes one pull worth far more than the keywords
    it asked about. They are NOT added to `keywords` — measuring a term is not
    a decision to track it.
    """
    config = config or default_settings
    report = PopularityPullReport(requested=len(keywords))
    if not keywords:
        return report

    owns_fetcher = fetcher is None
    fetcher = fetcher or Fetcher(
        rate_per_minute=APPLE_POPULARITY_RATE_PER_MIN, config=config
    )
    transport = None
    try:
        if client is None:
            # The cookie transport needs the fetcher; the browser one ignores
            # it and drives its own profile. `build_transport` decides which,
            # and raises with the specific remedy when neither is available.
            transport = apple_popularity.build_transport(config, fetcher=fetcher)
            client = apple_popularity.ApplePopularityClient(transport, conn, config)
        result = await client.popularity(keywords, country)
    finally:
        if transport is not None:
            await transport.aclose()
        if owns_fetcher:
            await fetcher.aclose()

    report.from_cache = sum(1 for row in result.rows if row.from_cache)
    writes: list[DemandWrite] = []
    for row in result.rows:
        if row.censored:
            report.censored += 1
        else:
            report.scored += 1
        writes.append(
            DemandWrite(
                source=apple_popularity.SOURCE,
                scale=search.SCALE_ORDINAL_100,
                keyword=row.keyword,
                country=row.country,
                value=row.popularity or 0.0,
                censored=row.censored,
            )
        )

    if store_related:
        asked = {apple_popularity.normalize_term(k) for k in keywords}
        for term, value in result.related.items():
            if term in asked:
                continue
            report.related += 1
            writes.append(
                DemandWrite(
                    source=apple_popularity.SOURCE,
                    scale=search.SCALE_ORDINAL_100,
                    keyword=term,
                    country=country.lower(),
                    value=value,
                )
            )

    with transaction(conn):
        write_demand_observations(conn, writes)
    return report
