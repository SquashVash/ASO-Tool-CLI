from __future__ import annotations

import dataclasses
import json
from datetime import date
from pathlib import Path

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from aso.clients import asa
from aso.config import ASA_API_BASE, ASA_TOKEN_URL, ASASettings
from aso.config import settings as live_settings

from .test_http import fast_fetcher

CAMPAIGNS_URL = f"{ASA_API_BASE}/campaigns"
ACLS_URL = f"{ASA_API_BASE}/acls"


def report_url(campaign_id: int) -> str:
    return f"{ASA_API_BASE}/reports/campaigns/{campaign_id}/searchterms"


@pytest.fixture
def private_key(tmp_path: Path) -> Path:
    """A real P-256 key, so signing is genuinely exercised rather than mocked."""
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "asa-private-key.pem"
    path.write_bytes(pem)
    return path


@pytest.fixture
def creds(private_key: Path) -> ASASettings:
    return ASASettings(
        client_id="SEARCHADS.client",
        team_id="SEARCHADS.team",
        key_id="key-123",
        org_id="4242",
        private_key_path=private_key,
    )


@pytest.fixture
def configured(creds: ASASettings):
    return dataclasses.replace(live_settings, asa=creds)


def mock_token(expires_in: int = 3600) -> None:
    respx.post(ASA_TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "tok-abc", "expires_in": expires_in}
        )
    )


def report_payload(rows: list[dict], total: int | None = None) -> dict:
    payload = {"data": {"reportingDataResponse": {"row": rows}}}
    if total is not None:
        payload["pagination"] = {"totalResults": total}
    return payload


def row(term: str, impressions: int, **extra) -> dict:
    return {
        "metadata": {"searchTermText": term, **extra},
        "total": {"impressions": impressions, "taps": 3, "installs": 1},
    }


# --- credentials -----------------------------------------------------------


def test_missing_credentials_are_named_individually() -> None:
    creds = ASASettings(client_id="x")
    assert not creds.configured
    assert "ASO_ASA_TEAM_ID" in creds.missing
    assert "ASO_ASA_CLIENT_ID" not in creds.missing


def test_fully_populated_credentials_are_configured(creds: ASASettings) -> None:
    assert creds.configured
    assert creds.missing == []


def test_a_missing_key_file_says_so(tmp_path: Path) -> None:
    with pytest.raises(asa.ASANotConfigured) as excinfo:
        asa.load_private_key(tmp_path / "nope.pem")
    assert "Cannot read" in str(excinfo.value)


def test_pointing_at_the_public_key_by_mistake_is_caught(tmp_path: Path) -> None:
    """An easy and very confusing mistake to make."""
    path = tmp_path / "public.pem"
    path.write_text("-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----\n")
    with pytest.raises(asa.ASANotConfigured) as excinfo:
        asa.load_private_key(path)
    assert "public half" in str(excinfo.value)


# --- the client assertion --------------------------------------------------


def test_assertion_carries_the_claims_apple_requires(
    creds: ASASettings, private_key: Path
) -> None:
    token = asa.build_client_assertion(
        creds, private_key.read_text(encoding="utf-8"), issued_at=1_700_000_000
    )
    claims = jwt.decode(
        token, options={"verify_signature": False}, audience="https://appleid.apple.com"
    )
    assert claims["sub"] == "SEARCHADS.client"
    assert claims["iss"] == "SEARCHADS.team"
    assert claims["aud"] == "https://appleid.apple.com"
    assert claims["iat"] == 1_700_000_000
    assert claims["exp"] > claims["iat"]


def test_assertion_is_signed_es256_with_the_key_id_in_the_header(
    creds: ASASettings, private_key: Path
) -> None:
    token = asa.build_client_assertion(creds, private_key.read_text(encoding="utf-8"))
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256"
    assert header["kid"] == "key-123"


def test_assertion_verifies_against_the_matching_public_key(
    creds: ASASettings, private_key: Path
) -> None:
    """The signature has to be real — a malformed one fails opaquely at Apple."""
    key = serialization.load_pem_private_key(private_key.read_bytes(), password=None)
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    token = asa.build_client_assertion(creds, private_key.read_text(encoding="utf-8"))
    claims = jwt.decode(
        token, public_pem, algorithms=["ES256"], audience="https://appleid.apple.com"
    )
    assert claims["sub"] == "SEARCHADS.client"


def test_an_rsa_key_is_rejected_with_a_useful_message(creds: ASASettings) -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )
    with pytest.raises(asa.ASANotConfigured) as excinfo:
        asa.build_client_assertion(creds, rsa_pem)
    assert "P-256" in str(excinfo.value)


# --- token exchange --------------------------------------------------------


def form_of(request: httpx.Request) -> dict[str, str]:
    from urllib.parse import parse_qsl

    return dict(parse_qsl(request.content.decode()))


@respx.mock
async def test_token_exchange_sends_the_documented_oauth_params(configured) -> None:
    mock_token()
    async with fast_fetcher() as fetcher:
        await asa.ASAClient(fetcher, configured).access_token()

    form = form_of(respx.calls.last.request)
    assert form["grant_type"] == "client_credentials"
    assert form["scope"] == "searchadsorg"
    assert form["client_assertion_type"].endswith("jwt-bearer")
    assert jwt.get_unverified_header(form["client_assertion"])["alg"] == "ES256"


@respx.mock
async def test_the_assertion_is_never_put_in_the_url(configured) -> None:
    """A URL is logged by httpx, by proxies, and by Apple. The body is not."""
    mock_token()
    async with fast_fetcher() as fetcher:
        await asa.ASAClient(fetcher, configured).access_token()

    url = str(respx.calls.last.request.url)
    assert "client_assertion" not in url
    assert "eyJ" not in url


@respx.mock
async def test_a_400_falls_back_to_apples_documented_query_string_form(
    configured,
) -> None:
    """Apple's docs show query params. If the body is refused, try their way."""
    respx.post(ASA_TOKEN_URL).mock(
        side_effect=[
            httpx.Response(400, text="missing grant_type"),
            httpx.Response(200, json={"access_token": "tok-abc", "expires_in": 3600}),
        ]
    )
    async with fast_fetcher(retry_attempts=1) as fetcher:
        token = await asa.ASAClient(fetcher, configured).access_token()

    assert token == "tok-abc"
    assert respx.calls.call_count == 2
    assert "client_assertion" in str(respx.calls.last.request.url)


@respx.mock
async def test_a_400_on_both_forms_reports_bad_credentials(configured) -> None:
    respx.post(ASA_TOKEN_URL).mock(return_value=httpx.Response(400, text="invalid_client"))
    async with fast_fetcher(retry_attempts=1) as fetcher:
        with pytest.raises(asa.ASAAuthError) as excinfo:
            await asa.ASAClient(fetcher, configured).access_token()
    assert "ASO_ASA_KEY_ID" in str(excinfo.value)


@respx.mock
async def test_token_is_reused_rather_than_re_minted(configured) -> None:
    mock_token()
    async with fast_fetcher() as fetcher:
        client = asa.ASAClient(fetcher, configured)
        first = await client.access_token()
        second = await client.access_token()
    assert first == second
    assert respx.calls.call_count == 1, "the cached token should be reused"


@respx.mock
async def test_a_nearly_expired_token_is_replaced(configured) -> None:
    """Refreshing early stops a long report 401ing at the boundary."""
    mock_token(expires_in=10)  # inside ASA_TOKEN_REFRESH_MARGIN_SECONDS
    async with fast_fetcher() as fetcher:
        client = asa.ASAClient(fetcher, configured)
        await client.access_token()
        await client.access_token()
    assert respx.calls.call_count == 2


@respx.mock
async def test_rejected_credentials_give_advice_not_a_traceback(configured) -> None:
    respx.post(ASA_TOKEN_URL).mock(return_value=httpx.Response(401, text="invalid_client"))
    async with fast_fetcher() as fetcher:
        with pytest.raises(asa.ASAAuthError) as excinfo:
            await asa.ASAClient(fetcher, configured).access_token()
    message = str(excinfo.value)
    assert "ASO_ASA_KEY_ID" in message
    assert "invalid_client" in message


@respx.mock
async def test_bad_credentials_are_not_retried(configured) -> None:
    """403 here means a wrong key, not a rate limit. Retrying hides the typo."""
    respx.post(ASA_TOKEN_URL).mock(return_value=httpx.Response(403, text="forbidden"))
    async with fast_fetcher(retry_attempts=4) as fetcher:
        with pytest.raises(asa.ASAAuthError):
            await asa.ASAClient(fetcher, configured).access_token()
    assert respx.calls.call_count == 1


@respx.mock
async def test_a_token_response_without_a_token_is_an_error(configured) -> None:
    respx.post(ASA_TOKEN_URL).mock(return_value=httpx.Response(200, json={"nope": 1}))
    async with fast_fetcher() as fetcher:
        with pytest.raises(asa.ASAAuthError):
            await asa.ASAClient(fetcher, configured).access_token()


async def test_unconfigured_credentials_fail_before_any_request() -> None:
    unconfigured = dataclasses.replace(live_settings, asa=ASASettings())
    async with fast_fetcher() as fetcher:
        with pytest.raises(asa.ASANotConfigured) as excinfo:
            await asa.ASAClient(fetcher, unconfigured).access_token()
    assert "ASO_ASA_CLIENT_ID" in str(excinfo.value)


# --- orgs and campaigns ----------------------------------------------------


@respx.mock
async def test_orgs_are_parsed(configured) -> None:
    mock_token()
    respx.get(ACLS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "orgId": 4242,
                        "orgName": "Indie Apps",
                        "currency": "USD",
                        "roleNames": ["API Account Read Only"],
                    }
                ]
            },
        )
    )
    async with fast_fetcher() as fetcher:
        orgs = await asa.ASAClient(fetcher, configured).orgs()

    assert len(orgs) == 1
    assert orgs[0].org_id == "4242"
    assert orgs[0].name == "Indie Apps"
    assert orgs[0].roles == ("API Account Read Only",)


@respx.mock
async def test_api_calls_carry_the_bearer_token_and_org_context(configured) -> None:
    mock_token()
    respx.get(CAMPAIGNS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    async with fast_fetcher() as fetcher:
        await asa.ASAClient(fetcher, configured).campaigns()

    headers = respx.calls.last.request.headers
    assert headers["Authorization"] == "Bearer tok-abc"
    assert headers["X-AP-Context"] == "orgId=4242"


@respx.mock
async def test_single_country_campaigns_are_calibratable(configured) -> None:
    mock_token()
    respx.get(CAMPAIGNS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": 1, "name": "US", "status": "ENABLED", "countriesOrRegions": ["US"]},
                    {"id": 2, "name": "EU", "countriesOrRegions": ["DE", "FR"]},
                ]
            },
        )
    )
    async with fast_fetcher() as fetcher:
        campaigns = await asa.ASAClient(fetcher, configured).campaigns()

    assert campaigns[0].country == "us", "lowercased to match the keywords table"
    assert campaigns[1].country is None, "multi-country cannot attribute a storefront"


@respx.mock
async def test_campaigns_are_paged_to_exhaustion(configured) -> None:
    mock_token()
    pages = [
        httpx.Response(
            200,
            json={
                "data": [{"id": i, "name": f"c{i}", "countriesOrRegions": ["US"]}],
                "pagination": {"totalResults": 3},
            },
        )
        for i in range(1, 4)
    ]
    respx.get(CAMPAIGNS_URL).mock(side_effect=pages)
    async with fast_fetcher() as fetcher:
        campaigns = await asa.ASAClient(fetcher, configured).campaigns()
    assert [c.campaign_id for c in campaigns] == [1, 2, 3]


# --- search-term reports ---------------------------------------------------


@respx.mock
async def test_search_terms_are_parsed_from_the_nested_envelope(configured) -> None:
    mock_token()
    respx.post(report_url(7)).mock(
        return_value=httpx.Response(
            200, json=report_payload([row("day trading", 5200), row("forex", 130)])
        )
    )
    async with fast_fetcher() as fetcher:
        rows = await asa.ASAClient(fetcher, configured).search_terms(
            7, start=date(2026, 1, 1), end=date(2026, 3, 31)
        )

    assert [r.search_term for r in rows] == ["day trading", "forex"]
    assert rows[0].impressions == 5200
    assert rows[0].taps == 3


@respx.mock
async def test_report_request_asks_for_window_totals(configured) -> None:
    mock_token()
    respx.post(report_url(7)).mock(
        return_value=httpx.Response(200, json=report_payload([]))
    )
    async with fast_fetcher() as fetcher:
        await asa.ASAClient(fetcher, configured).search_terms(
            7, start=date(2026, 1, 1), end=date(2026, 3, 31)
        )

    body = json.loads(respx.calls.last.request.content)
    assert body["startTime"] == "2026-01-01"
    assert body["endTime"] == "2026-03-31"
    assert body["returnRowTotals"] is True
    assert "granularity" not in body, "a daily breakdown would multiply rows for nothing"


@respx.mock
async def test_an_inverted_date_window_is_rejected_before_the_request(configured) -> None:
    mock_token()
    async with fast_fetcher() as fetcher:
        with pytest.raises(ValueError):
            await asa.ASAClient(fetcher, configured).search_terms(
                7, start=date(2026, 3, 31), end=date(2026, 1, 1)
            )


def test_rows_without_a_search_term_are_skipped_not_fatal() -> None:
    """Losing one malformed row beats losing the whole report."""
    rows = asa.parse_search_term_rows(
        report_payload(
            [
                row("good", 10),
                {"metadata": {}, "total": {"impressions": 5}},
                {"metadata": {"searchTermText": "no metrics"}, "total": {}},
            ]
        )
    )
    assert [r.search_term for r in rows] == ["good"]


def test_an_unrecognized_envelope_yields_no_rows_rather_than_crashing() -> None:
    assert asa.parse_search_term_rows({}) == []
    assert asa.parse_search_term_rows({"data": "unexpected"}) == []
    assert asa.parse_search_term_rows({"data": {"reportingDataResponse": {}}}) == []


def test_metric_values_survive_apples_type_wobble() -> None:
    rows = asa.parse_search_term_rows(
        report_payload([{"metadata": {"searchTermText": "x"}, "total": {"impressions": "1234.0"}}])
    )
    assert rows[0].impressions == 1234


@respx.mock
async def test_reports_page_until_exhausted(configured) -> None:
    mock_token()
    respx.post(report_url(7)).mock(
        side_effect=[
            httpx.Response(200, json=report_payload([row("a", 3)], total=2)),
            httpx.Response(200, json=report_payload([row("b", 2)], total=2)),
        ]
    )
    async with fast_fetcher() as fetcher:
        rows = await asa.ASAClient(fetcher, configured).search_terms(
            7, start=date(2026, 1, 1), end=date(2026, 1, 31)
        )
    assert [r.search_term for r in rows] == ["a", "b"]


@respx.mock
async def test_paging_stops_at_the_row_cap(configured) -> None:
    """A runaway report must not page forever."""
    mock_token()
    respx.post(report_url(7)).mock(
        return_value=httpx.Response(
            200, json=report_payload([row("a", 3)], total=1_000_000)
        )
    )
    async with fast_fetcher() as fetcher:
        rows = await asa.ASAClient(fetcher, configured).search_terms(
            7, start=date(2026, 1, 1), end=date(2026, 1, 31), max_rows=3
        )
    assert len(rows) == 3


# --- the report window -----------------------------------------------------


def test_default_window_ends_yesterday() -> None:
    """Today is still accumulating; including it would understate every term."""
    start, end = asa.default_window(lookback_days=7, today=date(2026, 8, 8))
    assert end == date(2026, 8, 7)
    assert start == date(2026, 8, 1)
    assert (end - start).days == 6
