"""Apple Search Ads API v5 — the only source of real impression counts.

Everything else in this tool infers demand. This module measures it, for the
narrow slice of the App Store your own campaigns touch. That slice is what
`scoring/search.py::calibrate()` fits the autocomplete proxy against.

Authentication, in three steps
------------------------------
1. You generate a P-256 (prime256v1) private key locally and paste its public
   half into Search Ads → Account Settings → API. Apple returns a `clientId`,
   `teamId` and `keyId`. Apple never holds the private key.
2. Per token request, we mint a short-lived **client assertion**: an ES256 JWT
   with `kid = keyId`, `iss = teamId`, `sub = clientId`, `aud = appleid`.
3. That assertion is exchanged at `appleid.apple.com/auth/oauth2/token` for a
   bearer access token (Apple returns ~1 hour). Every API call then carries
   `Authorization: Bearer …` plus `X-AP-Context: orgId=…`.

The token is cached in memory for the life of the client and refreshed
`ASA_TOKEN_REFRESH_MARGIN_SECONDS` before expiry. It is deliberately **not**
written to the SQLite cache: the response caching in `cache.py` exists to make
long refreshes resumable, and a bearer token on disk is a credential at rest
for no benefit — minting a new one costs a single request.

What this module will not do
----------------------------
Read-only. It calls `/acls`, `/campaigns` and the search-terms report, and
nothing that creates, modifies or spends. An API user provisioned with the
**API Account Read Only** role is sufficient, and is what you should use: this
is a research tool and it has no business being able to move your budget.

A caveat on the data
--------------------
A search-terms report tells you what people typed *that reached your ads*. It
is filtered by your targeting, your budget, and Apple's auction. A term with
zero impressions in your account is not a term nobody searches — it is a term
you did not bid on, or did not win. Calibration must therefore be read as
"fitted to the corner of the App Store my campaigns can see", which is why
`calibrate()` reports its sample size and refuses to run on too few points.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import jwt

from ..config import (
    ASA_API_BASE,
    ASA_ASSERTION_TYPE,
    ASA_DEFAULT_LOOKBACK_DAYS,
    ASA_JWT_AUDIENCE,
    ASA_JWT_LIFETIME_SECONDS,
    ASA_REPORT_MAX_ROWS,
    ASA_REPORT_PAGE_SIZE,
    ASA_SCOPE,
    ASA_TOKEN_REFRESH_MARGIN_SECONDS,
    ASA_TOKEN_URL,
    ASASettings,
    Settings,
)
from ..config import settings as default_settings
from ..http import AUTHENTICATED_TRANSIENT_STATUSES, Fetcher, FetchError

logger = logging.getLogger(__name__)


class ASAError(Exception):
    """Any Apple Search Ads failure that isn't a plain transport error."""


class ASANotConfigured(ASAError):
    """Credentials are absent or incomplete."""


class ASAAuthError(ASAError):
    """Apple rejected the credentials. Retrying will not help."""


@dataclass(frozen=True)
class Org:
    org_id: str
    name: str
    currency: str | None = None
    time_zone: str | None = None
    roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class Campaign:
    campaign_id: int
    name: str
    status: str | None = None
    countries: tuple[str, ...] = ()

    @property
    def country(self) -> str | None:
        """The campaign's country, when it targets exactly one.

        Multi-country campaigns cannot attribute a search term to a storefront,
        and this tool's keywords are per-country. Rather than guess, such
        campaigns report `None` and their rows are stored without a country,
        which excludes them from calibration.
        """
        return self.countries[0].lower() if len(self.countries) == 1 else None


@dataclass(frozen=True)
class SearchTermRow:
    """One search term's totals over the report window."""

    search_term: str
    impressions: int
    taps: int | None = None
    installs: int | None = None
    keyword: str | None = None
    match_type: str | None = None


def load_private_key(path: Path) -> str:
    """Read the PEM private key, with errors that say what to actually do."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ASANotConfigured(
            f"Cannot read the Apple Search Ads private key at {path}: {exc}"
        ) from exc
    if "PRIVATE KEY" not in text:
        raise ASANotConfigured(
            f"{path} does not look like a PEM private key. It should start with "
            "'-----BEGIN EC PRIVATE KEY-----' or '-----BEGIN PRIVATE KEY-----'. "
            "Note this is the key you generated, not the public half you pasted "
            "into the Search Ads UI."
        )
    return text


def build_client_assertion(
    creds: ASASettings,
    private_key: str,
    *,
    issued_at: int | None = None,
    lifetime: int = ASA_JWT_LIFETIME_SECONDS,
) -> str:
    """Mint the ES256 client assertion Apple exchanges for a bearer token.

    Pure and separately testable on purpose — this is the part most likely to
    be subtly wrong, and the failure mode from Apple is an opaque 400.
    """
    now = int(time.time()) if issued_at is None else issued_at
    try:
        return jwt.encode(
            {
                "sub": creds.client_id,
                "aud": ASA_JWT_AUDIENCE,
                "iss": creds.team_id,
                "iat": now,
                "exp": now + lifetime,
            },
            private_key,
            algorithm="ES256",
            headers={"alg": "ES256", "kid": creds.key_id},
        )
    except Exception as exc:  # pyjwt raises a variety of crypto errors here
        raise ASANotConfigured(
            f"Could not sign the Apple Search Ads assertion ({type(exc).__name__}: "
            f"{exc}). The key must be a P-256 (prime256v1) EC private key."
        ) from exc


def _require_configured(creds: ASASettings) -> None:
    if not creds.configured:
        raise ASANotConfigured(
            "Apple Search Ads is not configured. Missing: "
            + ", ".join(creds.missing)
            + ". See the README section 'Apple Search Ads setup'."
        )


def _auth_error(exc: FetchError) -> ASAAuthError:
    return ASAAuthError(
        f"Apple rejected the Search Ads credentials (HTTP {exc.status}). Check "
        "ASO_ASA_CLIENT_ID, ASO_ASA_TEAM_ID and ASO_ASA_KEY_ID against Account "
        "Settings → API, and confirm ASO_ASA_PRIVATE_KEY_PATH points at the "
        "private key whose public half is registered there. "
        f"Apple said: {exc.detail}"
    )


def _parse_json(body: str, *, what: str) -> dict:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ASAError(f"Apple returned a non-JSON {what} response: {exc}") from exc
    if not isinstance(payload, dict):
        raise ASAError(f"Expected a JSON object for {what}, got {type(payload).__name__}")
    return payload


def _as_int(value: object) -> int | None:
    """Apple sends metric values as ints, floats or numeric strings."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def parse_search_term_rows(payload: dict) -> list[SearchTermRow]:
    """Pull the rows out of a search-terms report response.

    The envelope is deep — `data.reportingDataResponse.row[]` — and Apple has
    changed the surrounding shape before, so every level is probed defensively.
    A row missing `searchTermText` is skipped rather than fataling the pull:
    losing one row is better than losing the report.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    reporting = data.get("reportingDataResponse")
    if not isinstance(reporting, dict):
        return []
    raw_rows = reporting.get("row")
    if not isinstance(raw_rows, list):
        return []

    rows: list[SearchTermRow] = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        metadata = raw.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        term = metadata.get("searchTermText")
        if not isinstance(term, str) or not term.strip():
            continue

        totals = raw.get("total")
        totals = totals if isinstance(totals, dict) else {}
        impressions = _as_int(totals.get("impressions"))
        if impressions is None:
            continue

        rows.append(
            SearchTermRow(
                search_term=term.strip(),
                impressions=impressions,
                taps=_as_int(totals.get("taps")),
                installs=_as_int(totals.get("installs")),
                keyword=metadata.get("keyword") if isinstance(
                    metadata.get("keyword"), str
                ) else None,
                match_type=metadata.get("matchType") if isinstance(
                    metadata.get("matchType"), str
                ) else None,
            )
        )
    return rows


@dataclass
class _Token:
    value: str
    expires_at: float

    def valid(self, *, margin: float = ASA_TOKEN_REFRESH_MARGIN_SECONDS) -> bool:
        return time.monotonic() < self.expires_at - margin


class ASAClient:
    """Read-only Apple Search Ads v5 client.

    Give it its own `Fetcher` — see `ASA_RATE_LIMIT_PER_MIN` for why it must not
    share the iTunes one.
    """

    def __init__(
        self,
        fetcher: Fetcher,
        config: Settings | None = None,
        *,
        private_key: str | None = None,
    ) -> None:
        self.fetcher = fetcher
        self.settings = config or default_settings
        self.creds = self.settings.asa
        self._private_key = private_key
        self._token: _Token | None = None

    # --- auth --------------------------------------------------------------

    @property
    def private_key(self) -> str:
        if self._private_key is None:
            _require_configured(self.creds)
            assert self.creds.private_key_path is not None
            self._private_key = load_private_key(self.creds.private_key_path)
        return self._private_key

    async def access_token(self) -> str:
        """A valid bearer token, minting one only when the cached one is stale."""
        if self._token is not None and self._token.valid():
            return self._token.value
        _require_configured(self.creds)

        assertion = build_client_assertion(self.creds, self.private_key)
        form = {
            "grant_type": "client_credentials",
            "client_id": self.creds.client_id or "",
            "client_assertion_type": ASA_ASSERTION_TYPE,
            "client_assertion": assertion,
            "scope": ASA_SCOPE,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        # Sent in the request BODY, not the query string. Apple's own curl
        # example puts these in the URL, which contradicts the
        # `x-www-form-urlencoded` content type it also sets — and a signed
        # assertion in a URL is logged by httpx at INFO, by any proxy in the
        # path, and by Apple's access logs. The body is both RFC 6749 §4.4.2
        # and the only version that doesn't leak a live credential three ways.
        try:
            body = await self.fetcher.post_text(ASA_TOKEN_URL, data=form, headers=headers)
        except FetchError as exc:
            if exc.status == 400:
                # ...but if Apple really does require the query string, a 400
                # is exactly what we'd get. Try it their documented way once
                # before concluding the credentials are wrong.
                logger.warning(
                    "token exchange rejected a form body; retrying with query "
                    "parameters as Apple's documentation shows"
                )
                try:
                    body = await self.fetcher.post_text(
                        ASA_TOKEN_URL, params=form, headers=headers
                    )
                except FetchError as retry_exc:
                    raise _auth_error(retry_exc) from retry_exc
            elif exc.status in (401, 403):
                raise _auth_error(exc) from exc
            else:
                raise

        payload = _parse_json(body, what="token")
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise ASAAuthError(f"Token response had no access_token: {payload}")
        expires_in = _as_int(payload.get("expires_in")) or 3600

        self._token = _Token(token, time.monotonic() + expires_in)
        logger.debug("obtained ASA access token, valid %ss", expires_in)
        return token

    async def auth_headers(self, *, org_id: str | None = None) -> dict[str, str]:
        """Public alias for `_headers`.

        Exists so `clients.apple_popularity` can borrow this client's auth
        without reaching into a private method. Same host, same token, same org
        context — reimplementing the JWT exchange there would be two copies of
        the one piece of security-relevant code in the tool.
        """
        return await self._headers(org_id=org_id)

    async def _headers(self, *, org_id: str | None = None) -> dict[str, str]:
        token = await self.access_token()
        org = org_id or self.creds.org_id or ""
        return {
            "Authorization": f"Bearer {token}",
            "X-AP-Context": f"orgId={org}",
            "Content-Type": "application/json",
        }

    # --- endpoints ---------------------------------------------------------

    async def orgs(self) -> list[Org]:
        """Every org these credentials can reach. The cheapest credential check.

        Note this one endpoint does not need `X-AP-Context` — it is how you
        discover which org IDs are valid in the first place.
        """
        token = await self.access_token()
        body = await self.fetcher.request_text(
            "GET",
            f"{ASA_API_BASE}/acls",
            headers={"Authorization": f"Bearer {token}"},
            transient_statuses=AUTHENTICATED_TRANSIENT_STATUSES,
        )
        payload = _parse_json(body, what="acls")
        entries = payload.get("data")
        if not isinstance(entries, list):
            return []

        orgs: list[Org] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            org_id = entry.get("orgId")
            if org_id is None:
                continue
            roles = entry.get("roleNames")
            orgs.append(
                Org(
                    org_id=str(org_id),
                    name=str(entry.get("orgName") or ""),
                    currency=entry.get("currency"),
                    time_zone=entry.get("timeZone"),
                    roles=tuple(str(r) for r in roles) if isinstance(roles, list) else (),
                )
            )
        return orgs

    async def campaigns(self) -> list[Campaign]:
        """Every campaign in the configured org, paged to exhaustion."""
        _require_configured(self.creds)
        headers = await self._headers()
        found: list[Campaign] = []
        offset = 0

        while True:
            body = await self.fetcher.request_text(
                "GET",
                f"{ASA_API_BASE}/campaigns",
                params={"limit": str(ASA_REPORT_PAGE_SIZE), "offset": str(offset)},
                headers=headers,
                transient_statuses=AUTHENTICATED_TRANSIENT_STATUSES,
            )
            payload = _parse_json(body, what="campaigns")
            entries = payload.get("data")
            if not isinstance(entries, list) or not entries:
                break

            for entry in entries:
                if not isinstance(entry, dict) or entry.get("id") is None:
                    continue
                countries = entry.get("countriesOrRegions")
                found.append(
                    Campaign(
                        campaign_id=int(entry["id"]),
                        name=str(entry.get("name") or ""),
                        status=entry.get("status"),
                        countries=tuple(str(c) for c in countries)
                        if isinstance(countries, list)
                        else (),
                    )
                )

            offset += len(entries)
            if not _has_more(payload, offset):
                break

        return found

    async def search_terms(
        self,
        campaign_id: int,
        *,
        start: date,
        end: date,
        max_rows: int = ASA_REPORT_MAX_ROWS,
    ) -> list[SearchTermRow]:
        """Search-term totals for one campaign over `[start, end]`.

        Totals over the whole window, not a daily breakdown: calibration wants
        one impression count per term, and requesting `granularity` would
        multiply the row count for no gain.
        """
        _require_configured(self.creds)
        if end < start:
            raise ValueError(f"end ({end}) is before start ({start})")

        headers = await self._headers()
        url = f"{ASA_API_BASE}/reports/campaigns/{campaign_id}/searchterms"
        rows: list[SearchTermRow] = []
        offset = 0

        while len(rows) < max_rows:
            request = {
                "startTime": start.isoformat(),
                "endTime": end.isoformat(),
                "returnRowTotals": True,
                "returnRecordsWithNoMetrics": False,
                "selector": {
                    "orderBy": [{"field": "impressions", "sortOrder": "DESCENDING"}],
                    "pagination": {
                        "offset": offset,
                        "limit": min(ASA_REPORT_PAGE_SIZE, max_rows - len(rows)),
                    },
                },
            }
            body = await self.fetcher.post_text(
                url,
                json=request,
                headers=headers,
                transient_statuses=AUTHENTICATED_TRANSIENT_STATUSES,
            )
            payload = _parse_json(body, what="search-terms report")
            page = parse_search_term_rows(payload)
            rows.extend(page)
            if not page:
                break

            offset += len(page)
            if not _has_more(payload, offset):
                break

        if len(rows) >= max_rows:
            logger.warning(
                "campaign %s hit the %s-row cap; some low-volume terms were skipped",
                campaign_id,
                max_rows,
            )
        return rows


def _has_more(payload: dict, seen: int) -> bool:
    """Whether another page exists, per Apple's pagination envelope."""
    pagination = payload.get("pagination")
    if not isinstance(pagination, dict):
        return False
    total = _as_int(pagination.get("totalResults"))
    return total is not None and seen < total


def default_window(
    *, lookback_days: int = ASA_DEFAULT_LOOKBACK_DAYS, today: date | None = None
) -> tuple[date, date]:
    """The report window `aso asa pull` uses by default.

    Ends yesterday, not today: the current day is still accumulating and a
    partial count would understate a term for no reason.
    """
    end = (today or date.today()) - timedelta(days=1)
    return end - timedelta(days=lookback_days - 1), end
