"""Settings, loaded from .env with sane defaults.

Everything tunable lives here so scoring logic never has a magic number baked
into it. Import the module-level `settings` singleton; call `reload()` in tests
or a REPL if you change the environment underneath it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


# ---------------------------------------------------------------------------
# Competition weights
# ---------------------------------------------------------------------------
# Six normalized 0-100 components combined as a weighted mean. Tune these
# freely: every component is stored on the snapshot row, so changing a weight
# lets you re-score history without re-fetching anything.
#
# Keys must match the `comp_*` column names on `snapshots`.
COMPETITION_WEIGHTS: dict[str, float] = {
    "comp_rating_count": 0.35,  # how much review mass the top 10 carries
    "comp_exact_match": 0.25,  # how many incumbents target the term explicitly
    "comp_stars": 0.10,  # quality of the incumbents
    "comp_recency": 0.10,  # how actively the incumbents are maintained
    "comp_publisher": 0.10,  # concentration: same seller holding many slots
    "comp_breadth": 0.10,  # how crowded the total result set is
}

# --- component normalization constants -------------------------------------
# comp_rating_count: log10(median + 1) / DIVISOR, clamped to 1.
# 6 means "a median of 1,000,000 ratings in the top 10 saturates the component".
COMP_RATING_COUNT_LOG_DIVISOR = 6.0

# comp_breadth: log10(total_result_count + 1) / DIVISOR, clamped to 1.
# 2.3 means "~200 total results saturates" — the iTunes API caps out around
# there anyway, so this mostly separates thin niches from everything else.
COMP_BREADTH_LOG_DIVISOR = 2.3

# comp_recency: linear map from days-since-update to score.
# 0 days -> 100 (actively maintained, harder), 365+ days -> 0 (stale, easier).
COMP_RECENCY_MAX_DAYS = 365.0

# comp_stars: median average rating out of this maximum.
COMP_MAX_STARS = 5.0

# How many SERP results feed the competition score.
COMPETITION_TOP_N = 10

# A seller must appear at least this many times in the *full* result set before
# its top-10 slots count toward comp_publisher.
COMP_PUBLISHER_MIN_APPEARANCES = 2


# ---------------------------------------------------------------------------
# Search-volume (prefix ladder) constants
# ---------------------------------------------------------------------------
# The search score is a PROXY, not measured volume. It asks: how early in the
# typing of this keyword does the App Store start suggesting it, and how high
# does it sit in that suggestion list? Terms Apple surfaces from two characters
# in are terms lots of people type. See scoring/search.py for the mapping and
# for `calibrate()`, the hook that will later fit these constants against real
# Apple Search Ads impression counts.

# Shortest prefix we're willing to query. 1-char prefixes are noisy and burn
# rate limit; 2 is the practical floor.
SEARCH_MIN_PREFIX_LEN = 2

# Hard cap on autocomplete requests per keyword, to bound a refresh run.
# Longer keywords get their prefix ladder sampled down to this many rungs.
SEARCH_MAX_PREFIX_QUERIES = 12

# How the two observations combine into one 0-100 score.
SEARCH_DEPTH_WEIGHT = 0.70  # how short a prefix still surfaces the keyword
SEARCH_RANK_WEIGHT = 0.30  # where it sits in that suggestion list

# Rank component decays geometrically: rank 1 -> 100, rank 2 -> 82, rank 3 -> 67...
SEARCH_RANK_DECAY = 0.82

# Score for a keyword that never appears in its own autocomplete ladder, even
# at the full string. Not zero — it means "below the measurable floor", and a
# real zero would make opportunity ranking collapse.
SEARCH_NO_MATCH_SCORE = 1.0

# Autocomplete suggestion lists are ~10 long; ranks past this are floor-value.
SEARCH_MAX_HINT_RANK = 10


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
# Only these two public Apple endpoints are ever called. Deliberately no
# lookup/rss/undocumented storefront APIs.
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
HINTS_URL = "https://search.itunes.apple.com/WebObjects/MZSearchHints.woa/wa/hints"
ASA_TOKEN_URL = "https://appleid.apple.com/auth/oauth2/token"
ASA_API_BASE = "https://api.searchads.apple.com/api/v5"

USER_AGENT = "aso-tool/0.1 (+local research tool)"

# How many results to ask the iTunes search endpoint for. 50 is what the
# competition score's breadth component is calibrated against; see
# clients/itunes.py for why the API's resultCount is a floor, not a true total.
ITUNES_SEARCH_LIMIT = 50

# Token-bucket burst size. 1 means strict pacing — one request every
# 60/rate_limit_per_min seconds, no bursting. Raising this lets a run fire N
# requests back to back before throttling, which is exactly how you trip
# Apple's 403 threshold at the start of a long refresh. Raise with care.
RATE_LIMIT_BURST = 1

# Backoff between retries: exponential from INITIAL, capped at MAX, with up to
# JITTER seconds of randomness so parallel workers don't retry in lockstep.
RETRY_INITIAL_WAIT_SECONDS = 2.0
RETRY_MAX_WAIT_SECONDS = 60.0
RETRY_JITTER_SECONDS = 2.0


def _env_str(key: str, default: str) -> str:
    value = os.getenv(key)
    return value if value not in (None, "") else default


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from exc


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be a number, got {raw!r}") from exc


def _env_path(key: str) -> Path | None:
    raw = os.getenv(key)
    return Path(raw).expanduser() if raw else None


@dataclass(frozen=True)
class ASASettings:
    """Apple Search Ads credentials. Phase 2 — see clients/asa.py."""

    client_id: str | None = None
    team_id: str | None = None
    key_id: str | None = None
    org_id: str | None = None
    private_key_path: Path | None = None

    @property
    def configured(self) -> bool:
        return all(
            [
                self.client_id,
                self.team_id,
                self.key_id,
                self.org_id,
                self.private_key_path,
            ]
        )


@dataclass(frozen=True)
class Settings:
    db_path: Path = PROJECT_ROOT / "aso.db"
    default_country: str = "us"

    # Rate limiting, shared across every iTunes + hints call in a process.
    rate_limit_per_min: int = 15
    max_concurrency: int = 3
    http_timeout_seconds: float = 20.0
    retry_attempts: int = 4

    # Cache TTLs in days.
    serp_ttl_days: int = 3
    app_ttl_days: int = 7
    hints_ttl_days: int = 3

    competition_weights: dict[str, float] = field(
        default_factory=lambda: dict(COMPETITION_WEIGHTS)
    )
    asa: ASASettings = field(default_factory=ASASettings)

    @classmethod
    def from_env(cls) -> "Settings":
        db_path = _env_path("ASO_DB_PATH") or (PROJECT_ROOT / "aso.db")
        if not db_path.is_absolute():
            db_path = PROJECT_ROOT / db_path
        return cls(
            db_path=db_path,
            default_country=_env_str("ASO_DEFAULT_COUNTRY", "us").lower(),
            rate_limit_per_min=_env_int("ASO_RATE_LIMIT_PER_MIN", 15),
            max_concurrency=_env_int("ASO_MAX_CONCURRENCY", 3),
            http_timeout_seconds=_env_float("ASO_HTTP_TIMEOUT_SECONDS", 20.0),
            retry_attempts=_env_int("ASO_RETRY_ATTEMPTS", 4),
            serp_ttl_days=_env_int("ASO_SERP_TTL_DAYS", 3),
            app_ttl_days=_env_int("ASO_APP_TTL_DAYS", 7),
            hints_ttl_days=_env_int("ASO_HINTS_TTL_DAYS", 3),
            competition_weights=dict(COMPETITION_WEIGHTS),
            asa=ASASettings(
                client_id=os.getenv("ASO_ASA_CLIENT_ID") or None,
                team_id=os.getenv("ASO_ASA_TEAM_ID") or None,
                key_id=os.getenv("ASO_ASA_KEY_ID") or None,
                org_id=os.getenv("ASO_ASA_ORG_ID") or None,
                private_key_path=_env_path("ASO_ASA_PRIVATE_KEY_PATH"),
            ),
        )


settings = Settings.from_env()


def reload() -> Settings:
    """Re-read .env and the environment into the module-level `settings`."""
    global settings
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    settings = Settings.from_env()
    return settings
