from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from aso.clients.apple_popularity import (
    ApplePopularityClient,
    ApplePopularityNotConfigured,
    build_transport,
)
from aso.clients.apple_transport import (
    ApplePopularityError,
    ApplePopularitySessionExpired,
    BrowserTransport,
    CookieTransport,
)
from aso.config import settings as base_settings


def make_settings(tmp_path: Path, **overrides):
    defaults = dict(
        apple_popularity_enabled=True,
        apple_adam_id="123456",
        apple_cookie=None,
        apple_transport="auto",
        apple_profile_dir=tmp_path / "profile",
    )
    defaults.update(overrides)
    return dataclasses.replace(base_settings, **defaults)


class FakeTransport:
    """A transport that returns canned payloads and records what it was asked."""

    def __init__(self, payloads):
        self.payloads = payloads
        self.asked: list[str] = []
        self.closed = False

    async def query(self, text, country):
        self.asked.append(text)
        return self.payloads[text]

    async def aclose(self):
        self.closed = True


def envelope(*pairs):
    return {
        "data": {
            "recommendationV2": {
                "getRecommendedKeywords": [
                    {"name": n, "popularity": p} for n, p in pairs
                ]
            }
        }
    }


# --- transport selection ---------------------------------------------------


def test_missing_adam_id_is_refused_before_any_session_work(tmp_path) -> None:
    with pytest.raises(ApplePopularityNotConfigured, match="ADAM_ID"):
        build_transport(make_settings(tmp_path, apple_adam_id=None))


def test_auto_falls_back_to_cookie_when_no_profile_exists(tmp_path) -> None:
    settings = make_settings(tmp_path, apple_cookie="session=abc")
    transport = build_transport(settings, fetcher=object())
    assert isinstance(transport, CookieTransport)


def test_auto_prefers_a_saved_profile_over_a_cookie(tmp_path) -> None:
    """A profile that exists is one somebody deliberately created."""
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "Default").touch()
    settings = make_settings(tmp_path, apple_cookie="session=abc")

    assert isinstance(build_transport(settings, fetcher=object()), BrowserTransport)


def test_explicit_browser_without_a_profile_says_to_log_in(tmp_path) -> None:
    settings = make_settings(tmp_path, apple_transport="browser", apple_cookie="x=1")
    with pytest.raises(ApplePopularityNotConfigured, match="aso apple login"):
        build_transport(settings, fetcher=object())


def test_explicit_cookie_ignores_an_existing_profile(tmp_path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "Default").touch()
    settings = make_settings(tmp_path, apple_transport="cookie", apple_cookie="x=1")

    assert isinstance(build_transport(settings, fetcher=object()), CookieTransport)


def test_no_session_at_all_names_both_remedies(tmp_path) -> None:
    with pytest.raises(ApplePopularityNotConfigured) as exc:
        build_transport(make_settings(tmp_path), fetcher=object())
    assert "aso apple login" in str(exc.value)
    assert "ASO_APPLE_COOKIE" in str(exc.value)


# --- client behaviour over a transport -------------------------------------


async def test_seed_found_in_its_own_results_is_a_measurement(conn, tmp_path) -> None:
    transport = FakeTransport(
        {"habit tracker": envelope(("habit tracker", 52), ("streaks", 25))}
    )
    client = ApplePopularityClient(transport, conn, make_settings(tmp_path))
    result = await client.popularity(["habit tracker"], "us")

    assert result.rows[0].popularity == pytest.approx(52.0)
    assert result.rows[0].measured
    assert result.related == {"streaks": 25.0}


async def test_seed_absent_from_its_own_results_is_censored(conn, tmp_path) -> None:
    """The `finsta` case, which is the whole reason censoring exists."""
    transport = FakeTransport({"finsta": envelope(("sendit", 34), ("treads app", 46))})
    client = ApplePopularityClient(transport, conn, make_settings(tmp_path))
    result = await client.popularity(["finsta"], "us")

    assert result.rows[0].censored
    assert result.rows[0].popularity is None
    # The bycatch is still kept.
    assert result.related["sendit"] == pytest.approx(34.0)


async def test_disabled_flag_refuses_before_touching_the_transport(conn, tmp_path) -> None:
    transport = FakeTransport({})
    client = ApplePopularityClient(
        transport, conn, make_settings(tmp_path, apple_popularity_enabled=False)
    )
    with pytest.raises(ApplePopularityError, match="disabled"):
        await client.popularity(["anything"], "us")
    assert transport.asked == []


async def test_a_second_call_is_served_from_cache(conn, tmp_path) -> None:
    transport = FakeTransport({"insta": envelope(("insta", 73))})
    settings = make_settings(tmp_path)
    client = ApplePopularityClient(transport, conn, settings)

    await client.popularity(["insta"], "us")
    again = await client.popularity(["insta"], "us")

    assert transport.asked == ["insta"]
    assert again.rows[0].from_cache
    assert again.rows[0].popularity == pytest.approx(73.0)


async def test_composed_and_decomposed_forms_match_their_own_row(conn, tmp_path) -> None:
    """Without NFC folding this would record a false censoring."""
    keyword = "i̇nstagram"
    transport = FakeTransport({keyword: envelope((keyword, 61))})
    client = ApplePopularityClient(transport, conn, make_settings(tmp_path))
    result = await client.popularity([keyword], "us")

    assert result.rows[0].measured
    assert result.rows[0].popularity == pytest.approx(61.0)
