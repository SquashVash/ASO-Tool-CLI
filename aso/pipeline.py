"""Orchestration: refresh a set of keywords and record the scores.

Per keyword, in order:

1. fetch the SERP (cached 3 days) and score competition,
2. walk the autocomplete prefix ladder (cached 3 days) and score search volume,
3. combine into opportunity,
4. write the scores onto the keyword's record, replacing what was there.

Three properties this module exists to guarantee:

**Failures are recorded, not fatal.** A keyword whose SERP or hints can't be
fetched still gets its record updated, with `fetch_failed = 1`, the error text,
and whatever scores did succeed. The run continues. A 500-keyword refresh must
not die on keyword 300 because Apple rate-limited one request.

**Partial results stay partial.** If the SERP succeeds and hints fail, the
competition score and its components are written and the search score stays
None — never zero, never a guess. `opportunity` is None whenever either input
is, so a half-measured keyword can't outrank a fully measured one.

**A run is written once.** The keyword list is a single JSON file, so the
scores accumulate in memory and are saved when the run ends, rather than being
rewritten per keyword. A run interrupted partway therefore records nothing —
the trade for no longer holding a database open, and the reason `refresh`
reports what it wrote rather than leaving the caller to assume.

Keywords are processed sequentially. Concurrency would buy nothing: the shared
token bucket serializes every request anyway.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from .calibration import (
    DemandWrite,
    demand_map,
    demand_samples,
    latest_bridge,
    to_bridge,
    write_demand_observations,
)
from .clients import apple_popularity, asa
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
from .files import utcnow
from .http import Fetcher, FetchError
from .store import Store
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


def blend_outcome(outcome: KeywordOutcome) -> KeywordOutcome:
    """Apply a measured demand value to an ad-hoc score, in place.

    `score_keyword` deliberately reads no stored data, so it can only ever
    produce the proxy. That is correct for what it is and wrong for what a
    caller wants: a keyword we have already measured should not be reported at
    its guessed value just because this code path does not read the
    observations.

    Without this, `aso check "75 hard"` answered 70.7 while every stored
    snapshot of the same keyword read 9.0 — the same keyword, two numbers, and
    no indication which to believe. The blend belongs wherever a demand score
    is reported, not only where one is written.

    Recomputes `opportunity` too, since it multiplies by the score that just
    changed.
    """
    outcome.search_score_proxy = outcome.search_score
    value, censored = demand_map().get(
        (search.normalize_keyword(outcome.keyword), outcome.country), (None, False)
    )
    fitted = to_bridge(latest_bridge(country=outcome.country))

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
    fitted bridges.

    Without this, `aso check "habit tracker"` answered 42.0 while a stored
    snapshot of the same keyword read 80-odd: the same keyword, two numbers,
    and no indication which to believe. Two commands documented as running the
    same scoring code must not disagree.

    `bridge` may be passed to avoid a lookup per keyword in a long run.

    Recomputes `opportunity` too, since it multiplies by the score that just
    changed — linearly, which is why the level matters here at all.
    """
    outcome.competition_score_raw = outcome.competition_score
    fitted = bridge if bridge is not None else _load_competition_bridge(outcome.country)
    if fitted is not None and outcome.competition_score is not None:
        outcome.competition_score = fitted.apply(outcome.competition_score)
    outcome.opportunity_score = opportunity(
        outcome.search_score, outcome.competition_score
    )
    return outcome


def _load_competition_bridge(country: str) -> Bridge | None:
    """The stored competition level bridge for one storefront, if any.

    `metric="competition"` is what keeps this from picking up a demand bridge
    fitted for the same storefront.
    """
    return to_bridge(
        latest_bridge(
            country=country,
            source=COMPETITION_BRIDGE_SOURCE,
            metric="competition",
        )
    )


async def refresh_keyword(
    keyword_id: int,
    keyword: str,
    country: str,
    *,
    store: Store,
    itunes: ITunesClient,
    hints: HintsClient,
    force: bool = False,
    config: Settings | None = None,
    now: datetime | None = None,
    charts: ChartIndex | None = None,
    competition_bridge: Bridge | None = None,
) -> KeywordOutcome:
    """Refresh one keyword and record its scores. Never raises for a fetch failure.

    Writes into `store` in memory; the caller saves. `competition_bridge` is
    looked up per storefront when not supplied — `refresh` passes it so a
    500-keyword run does one lookup per country instead of 500.
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

    bridge_outcome(outcome, bridge=competition_bridge)
    blend_outcome(outcome)

    store.write_scores(
        keyword_id,
        captured_at=captured_at,
        search_score=outcome.search_score,
        search_score_proxy=outcome.search_score_proxy,
        search_source=outcome.search_source,
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
    return outcome


@dataclass
class RescoreReport:
    total: int = 0
    changed: int = 0
    largest_move: float = 0.0
    largest_move_keyword: str | None = None


def rescore(store: Store, *, config: Settings | None = None) -> RescoreReport:
    """Recompute every keyword's three finals from its own stored components.

    Makes no network requests. This is the payoff for storing components next
    to finals: change `COMPETITION_WEIGHTS` or the search mapping, run this,
    and every keyword moves onto the new scheme without re-asking Apple.

    Only the finals are rewritten. Components and the raw ladder observations
    are inputs and stay exactly as captured.

    Mutates `store` in memory; the caller saves.
    """
    config = config or default_settings
    report = RescoreReport()

    # Apple's readings and the fitted bridges, loaded once. Both are empty on a
    # tool that has never pulled popularity, and every branch below then takes
    # its pre-blend path — which is why enabling none of this changes any score.
    demand = demand_map()
    bridges: dict[str, Bridge | None] = {}
    comp_bridges: dict[str, Bridge | None] = {}

    def bridge_for(country: str) -> Bridge | None:
        if country not in bridges:
            bridges[country] = to_bridge(latest_bridge(country=country))
        return bridges[country]

    # The competition-side twin, keyed separately. Same object, different
    # metric: `latest_bridge`'s `metric` argument is what keeps a demand bridge
    # and a competition bridge from being mistaken for each other.
    def comp_bridge_for(country: str) -> Bridge | None:
        if country not in comp_bridges:
            comp_bridges[country] = _load_competition_bridge(country)
        return comp_bridges[country]

    for record in store.records:
        if not record.get("captured_at") or record.get("fetch_failed"):
            continue
        report.total += 1
        country = record["country"]

        # `derive` recomputes comp_incumbent from the two stored measurements
        # rather than reading the stored value, so a change to the formula
        # reaches every record here instead of being frozen into whatever each
        # one was captured under.
        comp_components = competition.derive(
            {name: record.get(name) for name in competition.COMPETITION_WEIGHTS}
        )
        comp_raw = competition.combine(
            comp_components, weights=config.competition_weights
        )
        # The bridge maps our raw score onto the vendor's level. It is applied
        # here rather than folded into `combine` so the pre-bridge number
        # survives in its own field — which is what stops the next rescore
        # bridging an already-bridged score, exactly as `search_score_proxy`
        # does on the demand side.
        comp_bridge = comp_bridge_for(country)
        comp_score = (
            comp_raw
            if comp_raw is None or comp_bridge is None
            else comp_bridge.apply(comp_raw)
        )

        depth = record.get("search_prefix_depth")
        rank = record.get("search_hint_rank")
        if depth is None and rank is None and record.get("search_score") is None:
            # Hints never succeeded for this keyword. Nothing to recompute
            # from, and inventing the no-match floor would turn a failed fetch
            # into a measurement.
            proxy_score = None
        else:
            length = len(search.normalize_keyword(record["keyword"]))
            proxy_score = search.score_from_observations(
                depth,
                rank,
                length,
                extensions=record.get("search_hint_extensions"),
                # Zero-weighted today; passed so that re-enabling it in config
                # rescores everything without a change of stored shape.
                rating_mass=record.get("comp_rating_count"),
            )

        # Blend Apple's measurement over the proxy where one exists. With no
        # popularity pulled, `measured` is (None, False) for every keyword and
        # `blend` returns the bridged — i.e. unchanged — proxy.
        apple_value, censored = demand.get(
            (search.normalize_keyword(record["keyword"]), country), (None, False)
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

        before = record.get("opportunity_score")
        store.update_scores(
            int(record["id"]),
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
                    report.largest_move_keyword = record["keyword"]

    return report


async def refresh(
    store: Store,
    keywords: Sequence[dict[str, Any]],
    *,
    force: bool = False,
    config: Settings | None = None,
    on_progress: ProgressCallback | None = None,
    fetcher: Fetcher | None = None,
) -> RefreshReport:
    """Refresh every keyword in `keywords`, recording the scores on each.

    A single `Fetcher` is shared by both clients so iTunes and autocomplete
    traffic draw on the same per-IP rate limit. Mutates `store` in memory; the
    caller saves once the run finishes.
    """
    config = config or default_settings
    report = RefreshReport(started_at=utcnow())

    async def run(active: Fetcher) -> None:
        itunes = ITunesClient(active, config=config)
        hints = HintsClient(active, config=config)
        charts_client = ChartsClient(active, config=config)
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
                comp_bridges[country] = _load_competition_bridge(country)
            outcome = await refresh_keyword(
                row["id"],
                row["keyword"],
                row["country"],
                store=store,
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

    requests_before = fetcher.requests_made if fetcher is not None else 0
    retries_before = fetcher.retries if fetcher is not None else 0

    if fetcher is not None:
        await run(fetcher)
        active_fetcher = fetcher
    else:
        async with Fetcher(config) as owned:
            await run(owned)
            active_fetcher = owned

    report.finished_at = utcnow()
    # A caller-supplied fetcher may be long-lived and shared — the API owns one
    # for the whole process — so its counters accumulate across runs. Report
    # what THIS run cost, not what the process has spent since boot.
    report.requests_made = active_fetcher.requests_made - requests_before
    report.retries = active_fetcher.retries - retries_before
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
    *,
    start: date,
    end: date,
    config: Settings | None = None,
    fetcher: Fetcher | None = None,
    client: asa.ASAClient | None = None,
) -> ASAPullReport:
    """Pull search-term impressions for every campaign in the configured org.

    Campaigns targeting more than one country are pulled but contribute no
    observation. Attributing a search term to one of several storefronts would
    be a guess, and calibration is the one place in this tool where a guess
    defeats the entire purpose. The campaign is named in `campaigns_skipped` so
    the omission is visible rather than silent.

    **Impressions are summed across campaigns, then written.** Two campaigns
    bidding on the same term saw genuinely different impressions and those add
    up; an observation is keyed on (source, keyword, country), so writing each
    campaign's figure as it arrived would leave the last one standing and
    silently discard the rest. The totals are accumulated here and written
    after every campaign, so the running figure on disk is always a correct
    total of the campaigns pulled so far — a credential expiring halfway
    through leaves a smaller number, never a wrong one.

    Re-running the same window is therefore safe: the totals start empty each
    run and replace what was stored rather than adding to it.
    """
    config = config or default_settings
    report = ASAPullReport(start=start.isoformat(), end=end.isoformat())

    async def run(active: Fetcher) -> None:
        api = client or asa.ASAClient(active, config)
        totals: dict[tuple[str, str], float] = {}

        for campaign in await api.campaigns():
            report.campaigns_seen += 1
            country = campaign.country
            if country is None:
                report.campaigns_skipped.append(
                    f"{campaign.name or campaign.campaign_id} "
                    f"(targets {len(campaign.countries)} countries)"
                )

            rows = await api.search_terms(campaign.campaign_id, start=start, end=end)
            if country is None:
                continue
            for row in rows:
                if row.impressions <= 0:
                    continue
                key = (search.normalize_keyword(row.search_term), country.lower())
                totals[key] = totals.get(key, 0.0) + float(row.impressions)

            write_demand_observations(
                [
                    DemandWrite(
                        source="asa",
                        scale=search.SCALE_COUNT,
                        keyword=keyword,
                        country=term_country,
                        value=value,
                    )
                    for (keyword, term_country), value in totals.items()
                ]
            )
            report.terms_written = len(totals)

        report.requests_made = active.requests_made

    if fetcher is not None:
        await run(fetcher)
    else:
        async with Fetcher(dataclasses.replace(
            config, rate_limit_per_min=ASA_RATE_LIMIT_PER_MIN
        )) as active:
            await run(active)

    return report


def calibration_for(source: str, store: Store | None = None) -> list[search.CalibrationSample]:
    """Build calibration samples for one source's measured demand.

    One source at a time, deliberately. An impression count and an ordinal
    popularity score are not comparable quantities, and averaging them would
    produce a number that means nothing.
    """
    samples = []
    for row in demand_samples(source, store):
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
    keywords: Sequence[str],
    *,
    country: str,
    config: Settings | None = None,
    fetcher: Fetcher | None = None,
    client: "apple_popularity.ApplePopularityClient | None" = None,
    store_related: bool = True,
) -> PopularityPullReport:
    """Fetch Apple's popularity index and store it as demand observations.

    Writes into the same demand observations AppFigures and ASA use, under
    source 'apple' and scale 'ordinal_100'. Nothing about calibration or
    scoring needs to learn a new kind of thing exists — which is what the
    `source` field was for.

    Censored readings are stored with `censored = 1`. They train nothing
    (`calibration.demand_samples` filters `value > 0`) and bound everything
    (`scoring.blend` caps the proxy at Apple's floor for them).

    `store_related` also keeps the terms that arrived attached to somebody
    else's seed. Those are real Apple measurements obtained for no extra
    request, and they are what makes one pull worth far more than the keywords
    it asked about. They are NOT added to the keyword list — measuring a term
    is not a decision to track it.
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
            client = apple_popularity.ApplePopularityClient(transport, config=config)
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

    write_demand_observations(writes)
    return report
