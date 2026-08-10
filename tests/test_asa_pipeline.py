from __future__ import annotations

import logging
from datetime import date

import httpx
import pytest
import respx

from aso import calibration, pipeline, store as store_module
from aso.clients import asa
from aso.config import ASA_TOKEN_URL

from .test_asa import (  # noqa: F401 - fixtures are used by name
    CAMPAIGNS_URL,
    configured,
    creds,
    mock_token,
    private_key,
    report_payload,
    report_url,
    row,
)
from .test_http import fast_fetcher

WINDOW = {"start": date(2026, 1, 1), "end": date(2026, 3, 31)}


def campaigns_response(*entries: dict) -> httpx.Response:
    return httpx.Response(200, json={"data": list(entries)})


def us_campaign(campaign_id: int = 1, name: str = "US search") -> dict:
    return {"id": campaign_id, "name": name, "countriesOrRegions": ["US"]}


# --- pulling ---------------------------------------------------------------


@respx.mock
async def test_pull_stores_measured_impressions(store: store_module.Store) -> None:
    mock_token()
    respx.get(CAMPAIGNS_URL).mock(return_value=campaigns_response(us_campaign()))
    respx.post(report_url(1)).mock(
        return_value=httpx.Response(
            200, json=report_payload([row("day trading", 5200), row("forex", 130)])
        )
    )

    async with fast_fetcher() as fetcher:
        report = await pipeline.pull_asa(config=configured_settings(), fetcher=fetcher, **WINDOW
        )

    assert report.campaigns_seen == 1
    assert report.terms_written == 2
    totals = {
        r["keyword"]: r["value"]
        for r in calibration.demand_observations()
        if r["source"] == "asa"
    }
    assert totals == {"day trading": 5200.0, "forex": 130.0}


@respx.mock
async def test_pull_normalizes_terms_to_match_tracked_keywords(
    store: store_module.Store,
) -> None:
    """ASA returns whatever the user typed; keywords are stored lowercased."""
    mock_token()
    respx.get(CAMPAIGNS_URL).mock(return_value=campaigns_response(us_campaign()))
    respx.post(report_url(1)).mock(
        return_value=httpx.Response(200, json=report_payload([row("Day  Trading", 42)]))
    )

    async with fast_fetcher() as fetcher:
        await pipeline.pull_asa(config=configured_settings(), fetcher=fetcher, **WINDOW)

    assert [r["keyword"] for r in calibration.demand_observations()] == ["day trading"]


@respx.mock
async def test_repulling_the_same_window_updates_rather_than_doubles(
    store: store_module.Store,
) -> None:
    """`pull` must be safe to re-run after a partial failure."""
    mock_token()
    respx.get(CAMPAIGNS_URL).mock(return_value=campaigns_response(us_campaign()))
    respx.post(report_url(1)).mock(
        return_value=httpx.Response(200, json=report_payload([row("forex", 100)]))
    )

    async with fast_fetcher() as fetcher:
        await pipeline.pull_asa(config=configured_settings(), fetcher=fetcher, **WINDOW)
        await pipeline.pull_asa(config=configured_settings(), fetcher=fetcher, **WINDOW)

    totals = calibration.demand_observations()
    assert len(totals) == 1
    assert totals[0]["value"] == 100.0, "summing a repeat would invent demand"


@respx.mock
async def test_two_campaigns_bidding_on_one_term_sum(store: store_module.Store) -> None:
    """Different campaigns saw genuinely different impressions. Those add."""
    mock_token()
    respx.get(CAMPAIGNS_URL).mock(
        return_value=campaigns_response(us_campaign(1), us_campaign(2, "US brand"))
    )
    respx.post(report_url(1)).mock(
        return_value=httpx.Response(200, json=report_payload([row("forex", 100)]))
    )
    respx.post(report_url(2)).mock(
        return_value=httpx.Response(200, json=report_payload([row("forex", 25)]))
    )

    async with fast_fetcher() as fetcher:
        await pipeline.pull_asa(config=configured_settings(), fetcher=fetcher, **WINDOW)

    assert calibration.demand_observations()[0]["value"] == 125.0


@respx.mock
async def test_multi_country_campaigns_are_excluded_from_calibration(
    store: store_module.Store,
) -> None:
    """A term can't be attributed to a storefront, so it must not train the fit."""
    mock_token()
    respx.get(CAMPAIGNS_URL).mock(
        return_value=campaigns_response(
            {"id": 9, "name": "EU", "countriesOrRegions": ["DE", "FR"]}
        )
    )
    respx.post(report_url(9)).mock(
        return_value=httpx.Response(200, json=report_payload([row("forex", 900)]))
    )

    async with fast_fetcher() as fetcher:
        report = await pipeline.pull_asa(config=configured_settings(), fetcher=fetcher, **WINDOW
        )

    assert report.terms_written == 0, "nothing attributable was learned"
    assert calibration.demand_observations() == [], "so nothing trains the fit"
    assert report.campaigns_skipped, "and the user is told why"


@respx.mock
async def test_credentials_never_reach_the_logs(
    store: store_module.Store, caplog: pytest.LogCaptureFixture
) -> None:
    """`--verbose` must not print a usable client assertion into a terminal or log file."""
    respx.post(ASA_TOKEN_URL).mock(return_value=httpx.Response(500, text="boom"))

    with caplog.at_level(logging.DEBUG):
        async with fast_fetcher(retry_attempts=1) as fetcher:
            with pytest.raises(Exception):
                await asa.ASAClient(fetcher, configured_settings()).access_token()

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "client_assertion" not in logged or "<redacted>" in logged
    assert "eyJ" not in logged, "a JWT leaked into the logs"


# --- calibration end to end ------------------------------------------------


@respx.mock
async def test_calibration_samples_join_observations_to_impressions(
    store: store_module.Store,
) -> None:
    store.add_keyword("day trading", "us")
    keyword = store.require_keyword("day trading", "us")
    store.write_scores(
        keyword["id"],
        captured_at="2026-08-01T00:00:00Z",
        search_prefix_depth=3,
        search_hint_rank=2,
    )
    calibration.write_demand_observations([
            calibration.DemandWrite(
                source="asa", scale="count", keyword="day trading",
                country="us", value=5200.0,
            )
        ],
    )

    samples = pipeline.calibration_for("asa", store)
    assert len(samples) == 1
    assert samples[0].prefix_depth == 3
    assert samples[0].hint_rank == 2
    assert samples[0].keyword_length == len("day trading")
    assert samples[0].impressions == 5200.0


def test_a_failed_hints_fetch_never_becomes_a_training_point(
    store: store_module.Store,
) -> None:
    """The single most important guard: a measurement failure is not an observation."""
    store.add_keyword("forex", "us")
    keyword = store.require_keyword("forex", "us")
    store.write_scores(
        keyword["id"],
        captured_at="2026-08-01T00:00:00Z",
        search_prefix_depth=None,
        search_hint_rank=None,
        fetch_failed=True,
        fetch_error="hints: HTTP 403",
    )
    calibration.write_demand_observations([
            calibration.DemandWrite(
                source="asa", scale="count", keyword="forex",
                country="us", value=900.0,
            )
        ],
    )

    assert pipeline.calibration_for("asa", store) == []


def test_calibration_only_joins_within_a_storefront(store: store_module.Store) -> None:
    """A German impression count must not calibrate a US keyword."""
    store.add_keyword("forex", "us")
    keyword = store.require_keyword("forex", "us")
    store.write_scores(
        keyword["id"], captured_at="2026-08-01T00:00:00Z",
        search_prefix_depth=2, search_hint_rank=1,
    )
    calibration.write_demand_observations([
            calibration.DemandWrite(
                source="asa", scale="count", keyword="forex",
                country="de", value=900.0,
            )
        ],
    )

    assert pipeline.calibration_for("asa", store) == []


def test_calibration_uses_only_the_most_recent_observation(
    store: store_module.Store,
) -> None:
    store.add_keyword("forex", "us")
    keyword = store.require_keyword("forex", "us")
    for captured_at, depth in (("2026-06-01T00:00:00Z", 6), ("2026-08-01T00:00:00Z", 2)):
        store.write_scores(
            keyword["id"], captured_at=captured_at,
            search_prefix_depth=depth, search_hint_rank=1,
        )
    calibration.write_demand_observations([
            calibration.DemandWrite(
                source="asa", scale="count", keyword="forex",
                country="us", value=900.0,
            )
        ],
    )

    samples = pipeline.calibration_for("asa", store)
    assert len(samples) == 1
    assert samples[0].prefix_depth == 2


def configured_settings():
    """Settings with ASA credentials, built from the shared fixtures."""
    import dataclasses
    from pathlib import Path

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from aso.config import ASASettings, settings as live

    global _KEY_PATH
    try:
        path = _KEY_PATH
    except NameError:
        import tempfile

        key = ec.generate_private_key(ec.SECP256R1())
        path = Path(tempfile.mkdtemp()) / "key.pem"
        path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        _KEY_PATH = path

    return dataclasses.replace(
        live,
        asa=ASASettings(
            client_id="SEARCHADS.client",
            team_id="SEARCHADS.team",
            key_id="key-123",
            org_id="4242",
            private_key_path=path,
        ),
    )


@respx.mock
async def test_pull_projects_into_the_demand_table_calibration_reads(
    store: store_module.Store,
) -> None:
    """ASA and imported sources must land in one place, so calibrate has one path."""
    mock_token()
    respx.get(CAMPAIGNS_URL).mock(return_value=campaigns_response(us_campaign()))
    respx.post(report_url(1)).mock(
        return_value=httpx.Response(200, json=report_payload([row("day trading", 5200)]))
    )

    async with fast_fetcher() as fetcher:
        await pipeline.pull_asa(config=configured_settings(), fetcher=fetcher, **WINDOW)

    sources = calibration.demand_sources()
    assert [(r["source"], r["scale"]) for r in sources] == [("asa", "count")]


@respx.mock
async def test_multi_country_pull_writes_no_demand_observation(
    store: store_module.Store,
) -> None:
    """Unattributable to a storefront, so it must not reach the fit at all."""
    mock_token()
    respx.get(CAMPAIGNS_URL).mock(
        return_value=campaigns_response(
            {"id": 9, "name": "EU", "countriesOrRegions": ["DE", "FR"]}
        )
    )
    respx.post(report_url(9)).mock(
        return_value=httpx.Response(200, json=report_payload([row("forex", 900)]))
    )

    async with fast_fetcher() as fetcher:
        await pipeline.pull_asa(config=configured_settings(), fetcher=fetcher, **WINDOW)

    assert calibration.demand_sources() == []


def test_sources_never_mix_in_one_fit(store: store_module.Store) -> None:
    """An impression count and a popularity rank are not the same quantity."""
    store.add_keyword("forex", "us")
    keyword = store.require_keyword("forex", "us")
    store.write_scores(
        keyword["id"], captured_at="2026-08-01T00:00:00Z",
        search_prefix_depth=2, search_hint_rank=1,
    )
    calibration.write_demand_observations([
            calibration.DemandWrite(source="asa", scale="count", keyword="forex",
                             country="us", value=5000.0),
            calibration.DemandWrite(source="appfigures", scale="ordinal_100", keyword="forex",
                             country="us", value=42.0),
        ],
    )

    asa_samples = pipeline.calibration_for("asa", store)
    af_samples = pipeline.calibration_for("appfigures", store)
    assert [s.impressions for s in asa_samples] == [5000.0]
    assert [s.impressions for s in af_samples] == [42.0]
    assert af_samples[0].scale == "ordinal_100"


def test_calibration_skips_deactivated_keywords(store: store_module.Store) -> None:
    """`active = 0` must mean out of the working set, calibration included.

    Otherwise deactivating an over-represented block of keywords appears to do
    nothing, because their old observations keep training the fit.
    """
    for keyword, active in (("kept", True), ("dropped", False)):
        store.add_keyword(keyword, "us")
        row = store.require_keyword(keyword, "us")
        store.write_scores(
            row["id"], captured_at="2026-08-01T00:00:00Z",
            search_prefix_depth=3, search_hint_rank=1,
        )
        calibration.write_demand_observations([
                calibration.DemandWrite(
                    source="appfigures", scale="ordinal_100",
                    keyword=keyword, country="us", value=50.0,
                )
            ],
        )
        if not active:
            store.set_active(row["id"], False)

    samples = calibration.demand_samples("appfigures", store)
    assert [r["keyword"] for r in samples] == ["kept"]
