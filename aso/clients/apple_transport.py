"""How the popularity GraphQL call actually gets made.

Two ways to hold an Apple Ads session, behind one interface, because the choice
is a deployment question and not a scoring one — `ApplePopularityClient` should
not know or care which is in use.

    CookieTransport   a `Cookie` header pasted into .env, sent with httpx.
    BrowserTransport  a real browser profile on disk, driven by Playwright.

WHY THERE ARE TWO
-----------------
The endpoint takes a dashboard session cookie and nothing else. No bearer
token, no API key. That leaves an unattractive choice:

* A pasted cookie needs no dependencies and works in five minutes, but it is a
  credential in a dotfile with an expiry nobody controls. Fine for a one-off
  pull, wrong for "get keyword data any time".
* A browser profile is a heavyweight dependency and a one-time interactive
  login, but the session then renews itself the way it does for a human who
  keeps the tab open. This is what "any time" actually requires.

So: cookie for the quick path, browser for the durable one, and the client
above is written against whichever it is handed.

WHAT NEITHER CAN DO
-------------------
Neither survives Apple invalidating the session — a password change, a
revoked device, a long enough absence. `BrowserTransport` raises
`ApplePopularitySessionExpired` and `aso apple login` fixes it in one
interactive step. There is no arrangement here that never needs a human again,
and the code does not pretend otherwise.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Protocol

from ..config import (
    APPLE_POPULARITY_OPERATION,
    APPLE_POPULARITY_QUERY,
    APPLE_POPULARITY_URL,
)
from ..http import FetchError, Fetcher

logger = logging.getLogger(__name__)

# Where the dashboard lives. `BrowserTransport` must be ON this origin before it
# can call the endpoint — the request is same-origin from the page's point of
# view, and issuing it from anywhere else trips the site's CSP.
DASHBOARD_URL = "https://app-ads.apple.com"

# How we know a login finished: the dashboard redirects to a /cm/ path once the
# session is real. Checking the URL rather than scraping the page keeps this
# working when Apple restyles the UI, which they do.
SIGNED_IN_MARKER = "/cm/"


class ApplePopularityError(Exception):
    """The popularity endpoint failed or answered in a shape we do not know."""


class ApplePopularitySessionExpired(ApplePopularityError):
    """The session is no longer signed in.

    Its own class because it is the failure that WILL happen, on a schedule
    nobody controls. "Your session expired, run `aso apple login`" is a
    different instruction from "the endpoint moved", and conflating them sends
    people to debug the wrong thing.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(
            (detail + "\n" if detail else "")
            + "The Apple Ads session is expired or invalid.\n"
            "  browser transport:  run `aso apple login` and sign in once.\n"
            "  cookie transport:   copy a fresh `Cookie` header from a "
            "signed-in session at https://app-ads.apple.com into "
            "ASO_APPLE_COOKIE.\n"
            "Until then, demand scores fall back to the autocomplete and SERP "
            "proxy."
        )


class PopularityTransport(Protocol):
    """Ask Apple about one seed keyword, return the parsed JSON body."""

    async def query(self, text: str, country: str) -> object: ...

    async def aclose(self) -> None: ...


def _request_body(text: str, country: str, adam_id: str) -> dict:
    return {
        "operationName": APPLE_POPULARITY_OPERATION,
        "variables": {
            "adamId": adam_id,
            "text": text,
            "storefronts": [country.upper()],
        },
        "query": APPLE_POPULARITY_QUERY,
    }


class CookieTransport:
    """Send the request with a pasted `Cookie` header. No browser required."""

    def __init__(self, fetcher: Fetcher, *, cookie: str, adam_id: str) -> None:
        self.fetcher = fetcher
        self.cookie = cookie
        self.adam_id = adam_id

    async def query(self, text: str, country: str) -> object:
        headers = {
            "Content-Type": "application/json",
            "Cookie": self.cookie,
            "Origin": DASHBOARD_URL,
            "Referer": f"{DASHBOARD_URL}/cm/app",
        }
        try:
            raw = await self.fetcher.post_text(
                APPLE_POPULARITY_URL,
                json=_request_body(text, country, self.adam_id),
                headers=headers,
            )
        except FetchError as exc:
            if exc.status in (301, 302, 401, 403):
                raise ApplePopularitySessionExpired(
                    f"HTTP {exc.status} from the popularity endpoint."
                ) from exc
            raise ApplePopularityError(
                f"popularity request failed with HTTP {exc.status}"
            ) from exc

        try:
            return json.loads(raw)
        except ValueError as exc:
            # An HTML body here is the sign-in page, not a broken endpoint.
            if raw.lstrip().startswith("<"):
                raise ApplePopularitySessionExpired(
                    "The endpoint returned an HTML page, which is the sign-in "
                    "screen."
                ) from exc
            raise ApplePopularityError(
                f"popularity response was not JSON: {exc}"
            ) from exc

    async def aclose(self) -> None:
        return None


class BrowserTransport:
    """Drive a persistent browser profile, and run the query inside the page.

    The request is issued by `page.evaluate` rather than by the driver, which
    matters for two reasons: it is same-origin so the site's CSP permits it,
    and the browser attaches and renews session cookies itself. That renewal is
    the entire point — it is what makes a session last beyond one paste.

    The profile directory is a credential. It is created under the project by
    default and belongs in .gitignore, next to the ASA private key.
    """

    def __init__(self, profile_dir: Path, *, adam_id: str, headless: bool = True) -> None:
        self.profile_dir = Path(profile_dir)
        self.adam_id = adam_id
        self.headless = headless
        self._playwright = None
        self._context = None
        self._page = None

    @staticmethod
    def _import_playwright():
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - dependency-gated
            raise ApplePopularityError(
                "The browser transport needs Playwright, which is an optional "
                "extra:\n"
                "    uv sync --extra browser\n"
                "    uv run playwright install chromium\n"
                "Or set ASO_APPLE_TRANSPORT=cookie to use a pasted cookie "
                "instead."
            ) from exc
        return async_playwright

    async def _ensure_page(self):
        if self._page is not None:
            return self._page

        async_playwright = self._import_playwright()
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            str(self.profile_dir), headless=self.headless
        )
        self._page = (
            self._context.pages[0]
            if self._context.pages
            else await self._context.new_page()
        )

        # Must be ON the dashboard origin before the query will be same-origin.
        await self._page.goto(f"{DASHBOARD_URL}/cm/app", wait_until="domcontentloaded")
        if "idmsa.apple.com" in self._page.url or SIGNED_IN_MARKER not in self._page.url:
            raise ApplePopularitySessionExpired(
                f"The saved profile lands on {self._page.url.split('?')[0]} "
                "rather than the dashboard."
            )
        return self._page

    async def query(self, text: str, country: str) -> object:
        page = await self._ensure_page()
        result = await page.evaluate(
            """async ({url, body}) => {
                const r = await fetch(url, {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body)
                });
                return {status: r.status, text: await r.text()};
            }""",
            {"url": APPLE_POPULARITY_URL, "body": _request_body(text, country, self.adam_id)},
        )

        if result["status"] in (301, 302, 401, 403):
            raise ApplePopularitySessionExpired(
                f"HTTP {result['status']} from the popularity endpoint."
            )
        try:
            return json.loads(result["text"])
        except ValueError as exc:
            if result["text"].lstrip().startswith("<"):
                raise ApplePopularitySessionExpired(
                    "The endpoint returned an HTML page, which is the sign-in "
                    "screen."
                ) from exc
            raise ApplePopularityError(
                f"popularity response was not JSON: {exc}"
            ) from exc

    async def aclose(self) -> None:
        if self._context is not None:
            await self._context.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._page = self._context = self._playwright = None


async def interactive_login(profile_dir: Path, *, timeout_seconds: int = 300) -> str:
    """Open a real window and wait for the human to sign in.

    Deliberately headed and deliberately hands-off: Apple sign-in involves a
    password and usually a second factor, and neither this tool nor anything
    driving it should be typing either. The browser is opened, the person signs
    in, and the resulting session is saved to `profile_dir` for later headless
    runs.

    Returns the URL landed on, so the caller can say where it ended up.
    """
    async_playwright = BrowserTransport._import_playwright()
    profile_dir.mkdir(parents=True, exist_ok=True)

    playwright = await async_playwright().start()
    try:
        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir), headless=False
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(f"{DASHBOARD_URL}/cm/app", wait_until="domcontentloaded")

        try:
            await page.wait_for_url(
                lambda url: SIGNED_IN_MARKER in url and "idmsa.apple.com" not in url,
                timeout=timeout_seconds * 1000,
            )
        except Exception as exc:
            raise ApplePopularityError(
                f"Timed out after {timeout_seconds}s waiting for sign-in. "
                "Nothing was saved. Re-run `aso apple login` and complete the "
                "sign-in, including any two-factor prompt."
            ) from exc

        landed = page.url
        # Let Apple finish writing its session cookies before the profile is
        # closed; a context torn down mid-handshake saves a half-session that
        # fails confusingly on the next headless run.
        await page.wait_for_timeout(2000)
        await context.close()
        return landed
    finally:
        await playwright.stop()
