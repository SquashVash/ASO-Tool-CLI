"""Streamlit dashboard.

    streamlit run dashboard.py

Five views: ad-hoc keyword lookup, the keyword table, one keyword in detail,
week-over-week movers, and add/remove management.

**No passive side effects.** This used to be strictly read-only. Lookup and
management broke that, so the rule is now narrower and load-bearing rather than
absolute:

- Nothing fetches from Apple or writes to the database except in response to a
  submitted form or a clicked button. Streamlit reruns this entire script on
  *every* widget interaction — changing a filter, sorting a column, resizing —
  so anything performing I/O at module or render scope would fire on all of
  them. That is not hypothetical: at 15 requests/minute and ~13 requests per
  ladder walk, a lookup on every rerun would trip Apple's 403 threshold in
  under a minute and take a running `aso refresh` down with it.
- Every lookup result is cached in `st.session_state`, so reruns re-render from
  memory instead of re-fetching.
- The network call itself lives in `aso.lookup`, not here, which is what makes
  the rule testable — `tests/test_dashboard.py` asserts this module never
  constructs an HTTP client of its own.

Bulk collection still belongs to `aso refresh`. This screen is for answering
"is this one term worth tracking?" without committing to it.

All SQL lives in `aso.repository`, so there is still one place to look when a
query is wrong. This file is presentation only.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pandas as pd
import streamlit as st

from aso import lookup as lookup_module
from aso import repository as repo
from aso.config import COMPETITION_WEIGHTS, settings
from aso.db import migrate, session

st.set_page_config(page_title="aso", page_icon="🔍", layout="wide")

VIEWS = ["Find keywords", "Keywords", "Keyword detail", "Movers", "Manage"]

# Where a lookup result is parked between reruns. Deliberately NOT "lookup":
# `st.form("lookup")` reserves that name in session_state for the widget, and
# assigning to it raises "cannot be modified after the widget ... is
# instantiated" the moment you press the button. Widget keys and your own
# state share one namespace.
LOOKUP_RESULT_KEY = "lookup_result"
COMPONENT_COLUMNS = list(COMPETITION_WEIGHTS)
SCORE_HELP = {
    "opportunity": "search × (100 − competition) ÷ 100. Higher is better.",
    "search": "Autocomplete proxy, NOT measured volume. Ordinal only.",
    "competition": "0–100, higher = harder to rank for.",
}


# --- data ------------------------------------------------------------------
# Cached so a widget interaction doesn't re-query on every rerun. The TTL is
# short because `aso refresh` runs in another process and its results should
# show up without restarting the dashboard.


@st.cache_data(ttl=30)
def load_scores(country: str | None, tag: str | None) -> pd.DataFrame:
    with session() as conn:
        rows = repo.latest_scores(conn, country=country, tag=tag, sort="opportunity")
    return _frame(rows)


@st.cache_data(ttl=30)
def load_filters() -> tuple[list[str], list[str]]:
    with session() as conn:
        return repo.countries(conn), repo.all_tags(conn)


@st.cache_data(ttl=30)
def load_history(keyword_id: int) -> pd.DataFrame:
    with session() as conn:
        return _frame(repo.snapshot_history(conn, keyword_id))


@st.cache_data(ttl=30)
def load_serp(keyword_id: int) -> pd.DataFrame:
    with session() as conn:
        return _frame(repo.latest_serp(conn, keyword_id, limit=10))


@st.cache_data(ttl=30)
def load_movers(days: int, country: str | None, tag: str | None) -> pd.DataFrame:
    with session() as conn:
        return _frame(repo.score_movers(conn, days=days, country=country, tag=tag))


def _frame(rows: list[sqlite3.Row]) -> pd.DataFrame:
    """sqlite3.Row -> DataFrame, keeping columns even when there are no rows.

    An empty frame with no columns breaks every downstream `df[...]`, which is
    exactly the state a fresh database is in.
    """
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(row) for row in rows])


def when(value: str | None) -> str:
    if not value:
        return "never"
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return moment.strftime("%Y-%m-%d %H:%M UTC")


# --- shared chrome ---------------------------------------------------------


def sidebar() -> tuple[str, str | None, str | None]:
    st.sidebar.title("aso")
    st.sidebar.caption(f"`{settings.db_path.name}`")

    view = st.sidebar.radio("View", VIEWS, label_visibility="collapsed")

    countries, tags = load_filters()
    country = st.sidebar.selectbox("Storefront", ["all", *countries])
    tag = st.sidebar.selectbox("Tag", ["all", *tags])

    st.sidebar.divider()
    st.sidebar.caption(
        "Bulk collection is `aso refresh`. Only **Find keywords** queries "
        "Apple, and only when you submit it."
    )
    if st.sidebar.button("Reload data"):
        st.cache_data.clear()
        st.rerun()

    return view, (None if country == "all" else country), (None if tag == "all" else tag)


def caveats() -> None:
    with st.expander("What these numbers do and don't mean", expanded=False):
        st.markdown(
            """
**The iTunes Search API is not the App Store search index.** Result ordering
correlates with App Store ranking but is not the same ranking, is not
personalized, and ignores Search Ads placements. Read the SERP as *"the
competitive field around this term"*, never as *"where apps actually rank"*.

**The search score is a proxy, not measured volume.** It is derived from
autocomplete behaviour: how short a prefix still surfaces the keyword, and how
high it sits in that list. It is **ordinal** — useful for ranking keywords
against each other, meaningless as an absolute. Fitted against AppFigures
popularity on 66 keywords spanning 5–97, it reaches a cross-validated Spearman
of **0.478**. That is a real ordering signal and roughly a third of the
variance, but it is not a demand measurement: keywords a few points apart are
indistinguishable, and only large gaps mean anything.

Almost all of that signal is **prefix depth** — how few characters it takes
before Apple suggests the term. Position within the suggestion list carries
little (rank correlation 0.097) and characters-saved carries none (−0.030),
which is why their weights are 0.10 and 0.00.

**Missing is not zero.** A blank score means "not measured" — a failed fetch,
or a keyword never refreshed. It never means "no demand" or "no competition".
            """
        )


def score_columns(
    opportunity: float | None, search: float | None, competition: float | None
) -> None:
    """The three headline scores, laid out identically everywhere they appear."""
    columns = st.columns(3)
    for column, value, label in zip(
        columns,
        (opportunity, search, competition),
        ("opportunity", "search", "competition"),
    ):
        column.metric(
            label,
            "—" if value is None or pd.isna(value) else f"{value:.1f}",
            help=SCORE_HELP[label],
        )


def components_table(components: dict[str, float | None]) -> None:
    """Competition components beside their weights.

    The weights are shown because a component's value is uninterpretable
    without them: comp_recency at 10 and comp_publisher at 80 do not contribute
    in proportion to those numbers, and the aggregation is a power mean rather
    than a plain one, so a high component pulls harder than its weight alone
    suggests.

    It also explains the rows sitting at weight 0.00, none of which are dead:

    * comp_rating_count and comp_stars are the measured inputs to
      comp_incumbent, stored so the formula stays revisable against history;
    * comp_stars, comp_recency, comp_breadth and comp_incumbent were fitted to
      zero against AppFigures and are kept measured so a broader sample can
      overturn that;
    * comp_app_power is newer than the fit that set these weights, so it has
      never been priced. It is at 0 until the next `aso calibrate-competition`.
    """
    present = [name for name in COMPONENT_COLUMNS if name in components]
    st.dataframe(
        pd.DataFrame(
            {
                "component": present,
                "value": [components[name] for name in present],
                "weight": [COMPETITION_WEIGHTS[name] for name in present],
            }
        ),
        width="stretch",
        hide_index=True,
        column_config={
            "value": st.column_config.ProgressColumn(
                "value", min_value=0, max_value=100, format="%.1f"
            )
        },
    )


def serp_table(rows: list[dict], caption: str) -> None:
    if not rows:
        st.caption("No results returned for this term.")
        return
    st.dataframe(
        pd.DataFrame(rows),
        width="stretch",
        hide_index=True,
        column_config={
            "rank": st.column_config.NumberColumn("#", format="%d"),
            "app": "app",
            "seller": "seller",
            "ratings": st.column_config.NumberColumn("ratings", format="%d"),
            "stars": st.column_config.NumberColumn("stars", format="%.2f"),
            "updated": "updated",
            "price": st.column_config.NumberColumn("price", format="%.2f"),
        },
    )
    st.caption(caption)


# --- views -----------------------------------------------------------------


def find_view(default_country: str | None) -> None:
    """Score a keyword live, without tracking it.

    The one screen here that touches the network. Everything it needs is
    gathered in a form so a single submit triggers exactly one lookup, and the
    result is parked in `st.session_state` so the reruns caused by scrolling,
    sorting or expanding do not re-fetch it.
    """
    st.header("Find keywords")
    st.caption(
        "Score any term against the live App Store without adding it to your "
        "tracked list. Nothing is saved except the HTTP cache, so repeating a "
        "lookup is free."
    )

    countries = load_filters()[0] or [settings.default_country]
    preferred = default_country or settings.default_country
    with st.form("lookup"):
        left, middle, right = st.columns([3, 1, 1])
        keyword = left.text_input("Keyword", placeholder="sleep tracker")
        country = middle.selectbox(
            "Storefront",
            sorted({*countries, settings.default_country}),
            index=sorted({*countries, settings.default_country}).index(preferred)
            if preferred in countries or preferred == settings.default_country
            else 0,
        )
        force = right.checkbox(
            "Bypass cache",
            value=False,
            help="Re-fetch even if this term was looked up recently. Costs "
            "real requests against a 15/min budget.",
        )
        submitted = st.form_submit_button("Look up", type="primary")

    if submitted:
        if not keyword.strip():
            st.error("Enter a keyword.")
        else:
            with st.spinner(f"Scoring {keyword!r} — up to ~13 requests…"):
                try:
                    st.session_state[LOOKUP_RESULT_KEY] = lookup_module.lookup(
                        keyword, country, force=force
                    )
                except Exception as exc:  # noqa: BLE001 - surface anything in the UI
                    st.session_state.pop(LOOKUP_RESULT_KEY, None)
                    st.error(f"Lookup failed: {exc}")

    result = st.session_state.get(LOOKUP_RESULT_KEY)
    if result is None:
        st.info(
            "Enter a keyword above. This runs the same scoring as `aso refresh` "
            "but stores nothing, so there is no trend history for it."
        )
        caveats()
        return

    outcome = result.outcome
    st.subheader(f"{outcome.keyword}  ({outcome.country})")

    if outcome.failed:
        st.error(f"Partial result — {outcome.error}")
        st.caption(
            "Scores below are computed from whatever did arrive. A missing "
            "component is treated as unknown and its weight redistributed, not "
            "as zero."
        )

    score_columns(
        outcome.opportunity_score, outcome.search_score, outcome.competition_score
    )

    if result.percentile is not None:
        st.progress(
            min(1.0, max(0.0, result.percentile / 100.0)),
            text=f"Better opportunity than {result.percentile:.0f}% of your "
            f"{result.compared_against} scored keywords",
        )
        if result.compared_against < 10:
            st.caption(
                f"Only {result.compared_against} keyword(s) to compare against — "
                "treat that percentile as a hint, not a ranking."
            )
    else:
        st.caption(
            "No percentile: nothing scored to compare against yet, or this "
            "lookup produced no opportunity score."
        )

    if result.tracked:
        st.success("Already in your tracked list.")
    else:
        if st.button("Track this keyword", type="primary"):
            with session() as conn:
                _, created = repo.add_keyword(conn, outcome.keyword, outcome.country)
            st.cache_data.clear()
            st.success(
                f"Now tracking {outcome.keyword!r}. "
                "Run `aso refresh` to record its first snapshot — this lookup "
                "was not saved as one."
                if created
                else f"{outcome.keyword!r} was already tracked."
            )

    st.subheader("Search signal")

    if outcome.search_source == "apple":
        st.success(
            f"Demand is Apple's measured popularity: **{outcome.search_score:.0f}**. "
            f"The autocomplete + SERP proxy guessed "
            f"{outcome.search_score_proxy:.1f} — shown below for comparison, "
            "but the measured number is what the score uses."
        )
    elif outcome.search_source == "proxy_censored":
        st.warning(
            f"Apple was asked about this term and declined to score it, which "
            f"means it sits below Apple's reporting threshold. Capped at "
            f"**{outcome.search_score:.1f}** — the proxy guessed "
            f"{outcome.search_score_proxy:.1f}."
        )
    elif outcome.search_score_proxy is not None:
        st.caption(
            "No measured popularity for this keyword — the score below is the "
            "autocomplete + SERP proxy, mapped onto Apple's scale. Run "
            "`aso apple pull` to measure it."
        )

    depth, rank = outcome.prefix_depth, outcome.hint_rank
    if depth is None or rank is None:
        floor = (
            outcome.search_score_proxy
            if outcome.search_score_proxy is not None
            else outcome.search_score
        )
        st.warning(
            "Never surfaced in its own autocomplete, even at the full string. "
            f"The proxy scored it at the floor ({floor:.1f}) — that means "
            "'below the measurable threshold', not 'no demand'."
        )
    else:
        ladder = st.columns(3)
        ladder[0].metric("prefix depth", depth, help="Characters typed before "
                         "Apple suggested this term. Lower means more demand — "
                         "this is the signal that carries nearly all the score.")
        ladder[1].metric("hint rank", rank, help="Position in that suggestion "
                         "list. Weak signal: rank correlation 0.097 against "
                         "measured demand, hence a weight of 0.10.")
        ladder[2].metric("requests used", result.requests_made)
        st.caption(
            f"Apple suggested this term once you had typed {depth} character(s), "
            f"at position {rank}."
        )

    st.subheader("Competition")
    st.caption(
        f"Computed over the top {min(outcome.serp_size or 0, 10)} of "
        f"{outcome.serp_size or 0} results returned."
    )
    components_table(result.scored.components)

    st.subheader("Top 10")
    serp = result.scored.serp
    apps = getattr(serp, "apps", None) or []
    serp_table(
        [
            {
                "rank": position,
                "app": app.track_name or "",
                "seller": app.seller_name or "",
                "ratings": app.user_rating_count,
                "stars": app.average_user_rating,
                "updated": (app.current_version_release_date or "")[:10],
                "price": app.price,
            }
            for position, app in enumerate(apps[:10], start=1)
        ],
        "iTunes Search order — **not App Store rank**.",
    )
    caveats()


def manage_view(country: str | None, tag: str | None) -> None:
    """Add and remove tracked keywords."""
    st.header("Manage keywords")

    st.subheader("Add")
    with st.form("add_keywords"):
        raw = st.text_area(
            "Keywords",
            placeholder="one per line\nsleep tracker\nhabit tracker",
            help="One per line. Re-adding an existing keyword merges tags "
            "rather than duplicating it.",
        )
        left, right = st.columns(2)
        add_country = left.text_input("Storefront", value=country or settings.default_country)
        add_tags = right.text_input("Tags (optional)", placeholder="fitness, v2")
        if st.form_submit_button("Add", type="primary"):
            wanted = [line.strip() for line in raw.splitlines() if line.strip()]
            if not wanted:
                st.error("Nothing to add.")
            else:
                created, merged, failed = 0, 0, []
                with session() as conn:
                    for word in wanted:
                        try:
                            _, was_new = repo.add_keyword(
                                conn, word, add_country, add_tags or None
                            )
                        except ValueError as exc:
                            failed.append(f"{word!r}: {exc}")
                            continue
                        created += was_new
                        merged += not was_new
                st.cache_data.clear()
                if created:
                    st.success(
                        f"Added {created} keyword(s). Run `aso refresh` to score "
                        "them — adding does not fetch anything."
                    )
                if merged:
                    st.info(f"{merged} already tracked; tags merged.")
                for message in failed:
                    st.error(message)

    st.divider()
    st.subheader("Remove")
    with session() as conn:
        rows = repo.list_keywords(conn, country=country, tag=tag, active_only=False)
    if not rows:
        st.info("Nothing tracked in this filter.")
        return

    labels = {f"{row['keyword']}  ({row['country']})": row for row in rows}
    inactive = [label for label, row in labels.items() if not row["active"]]
    if inactive:
        st.caption(f"{len(inactive)} keyword(s) currently paused.")

    chosen = st.multiselect("Keywords", list(labels))
    if not chosen:
        st.caption(
            "**Pause** stops refreshing a keyword and drops it out of "
            "calibration while keeping its history — reversible, and what you "
            "want most of the time. **Delete** destroys the history too."
        )
        return

    selected = [labels[label] for label in chosen]
    with session() as conn:
        footprint = [repo.keyword_footprint(conn, row["id"]) for row in selected]
    snapshots = sum(item["snapshots"] for item in footprint)

    pause, resume, delete = st.columns(3)
    if pause.button(f"Pause {len(selected)}"):
        with session() as conn:
            for row in selected:
                repo.set_active(conn, row["id"], False)
        st.cache_data.clear()
        st.success(f"Paused {len(selected)}. History kept; reversible below.")
        st.rerun()

    if resume.button(f"Resume {len(selected)}"):
        with session() as conn:
            for row in selected:
                repo.set_active(conn, row["id"], True)
        st.cache_data.clear()
        st.success(f"Resumed {len(selected)}.")
        st.rerun()

    with delete.popover(f"Delete {len(selected)}…"):
        st.warning(
            f"Permanently deletes {len(selected)} keyword(s) and "
            f"**{snapshots} snapshot(s)** of history. This cannot be undone, "
            "and the trend data is not recoverable by re-adding the keyword."
        )
        st.caption(
            "Imported demand observations are kept — they are vendor "
            "measurements, still true whether or not you track the term."
        )
        confirmed = st.checkbox("I understand the history is destroyed")
        if st.button("Delete permanently", type="primary", disabled=not confirmed):
            totals = {"keywords": 0, "snapshots": 0, "serps": 0}
            with session() as conn:
                for row in selected:
                    removed = repo.delete_keyword(conn, row["id"])
                    for key in totals:
                        totals[key] += removed[key]
            st.cache_data.clear()
            st.success(
                f"Deleted {totals['keywords']} keyword(s), "
                f"{totals['snapshots']} snapshot(s), {totals['serps']} SERP row(s)."
            )
            st.rerun()




def keywords_view(country: str | None, tag: str | None) -> None:
    st.header("Keywords")
    frame = load_scores(country, tag)
    if frame.empty:
        st.info("No keywords tracked yet. `aso add \"your keyword\"` to start.")
        return

    scored = frame["opportunity_score"].notna().sum()
    left, middle, right = st.columns(3)
    left.metric("Tracked", len(frame))
    middle.metric("Scored", int(scored))
    right.metric("Never refreshed", int(len(frame) - scored))

    only_scored = st.checkbox("Hide keywords that have never been refreshed", value=False)
    view = frame[frame["opportunity_score"].notna()] if only_scored else frame

    columns = [
        c
        for c in (
            "keyword", "country", "tags",
            "opportunity_score", "search_score", "competition_score",
            "captured_at",
        )
        if c in view.columns
    ]
    st.dataframe(
        view[columns],
        width="stretch",
        hide_index=True,
        column_config={
            "opportunity_score": st.column_config.ProgressColumn(
                "opportunity", min_value=0, max_value=100, format="%.1f",
                help=SCORE_HELP["opportunity"],
            ),
            "search_score": st.column_config.NumberColumn(
                "search", format="%.1f", help=SCORE_HELP["search"]
            ),
            "competition_score": st.column_config.NumberColumn(
                "competition", format="%.1f", help=SCORE_HELP["competition"]
            ),
            "captured_at": st.column_config.TextColumn("last refreshed"),
        },
    )
    st.caption(
        "Click a column header to sort. Sorting here is client-side over the "
        "rows already loaded."
    )
    caveats()


def detail_view(country: str | None, tag: str | None) -> None:
    st.header("Keyword detail")
    frame = load_scores(country, tag)
    if frame.empty:
        st.info("Nothing tracked in this filter.")
        return

    labels = {
        f"{row.keyword}  ({row.country})": int(row.keyword_id)
        for row in frame.itertuples()
    }
    chosen = st.selectbox("Keyword", list(labels))
    keyword_id = labels[chosen]
    current = frame[frame["keyword_id"] == keyword_id].iloc[0]

    score_columns(
        current.get("opportunity_score"),
        current.get("search_score"),
        current.get("competition_score"),
    )

    history = load_history(keyword_id)
    if history.empty:
        st.warning("Never refreshed. Run `aso refresh` to collect a snapshot.")
        caveats()
        return

    st.subheader("Trend")
    if len(history) < 2:
        st.caption(
            f"Only one snapshot so far ({when(history['captured_at'].iloc[0])}). "
            "A trend needs at least two."
        )
    else:
        trend = history.set_index("captured_at")[
            ["opportunity_score", "search_score", "competition_score"]
        ]
        st.line_chart(trend)

    st.subheader("Competition components")
    latest = history.iloc[-1]
    present = [c for c in COMPONENT_COLUMNS if c in history.columns]
    components = pd.DataFrame(
        {
            "component": present,
            "value": [latest[c] for c in present],
            "weight": [COMPETITION_WEIGHTS[c] for c in present],
        }
    )
    st.dataframe(
        components, width="stretch", hide_index=True,
        column_config={
            "value": st.column_config.ProgressColumn(
                "value", min_value=0, max_value=100, format="%.1f"
            )
        },
    )
    st.caption(
        "Every score is reproducible from these stored components — that is "
        "what makes `aso rescore` possible without refetching."
    )

    depth = latest.get("search_prefix_depth")
    rank = latest.get("search_hint_rank")
    extensions = latest.get("search_hint_extensions")
    ladder = (
        "never surfaced in its own autocomplete"
        if pd.isna(depth) or pd.isna(rank)
        else f"surfaced from a {int(depth)}-character prefix at rank {int(rank)}"
    )
    # Worth showing next to the ladder result rather than buried: it is the
    # only demand signal a keyword that never surfaced has from autocomplete,
    # and seeing "no match" alone reads as "no demand", which is the mistake
    # this component exists to prevent.
    stem = (
        "extensions not measured"
        if pd.isna(extensions)
        else f"{int(extensions)} suggestion(s) extend it"
    )
    st.caption(f"Autocomplete: {ladder}; {stem}.")

    if latest.get("fetch_failed"):
        st.error(f"Last refresh failed: {latest.get('fetch_error')}")

    st.subheader("Top 10")
    serp = load_serp(keyword_id)
    if serp.empty:
        st.caption("No SERP stored for this keyword.")
    else:
        st.dataframe(
            serp[
                [
                    c
                    for c in ("rank", "track_name", "seller_name",
                              "user_rating_count", "average_user_rating")
                    if c in serp.columns
                ]
            ],
            width="stretch", hide_index=True,
            column_config={
                "track_name": "app",
                "seller_name": "seller",
                "user_rating_count": st.column_config.NumberColumn(
                    "ratings", format="%d"
                ),
                "average_user_rating": st.column_config.NumberColumn(
                    "stars", format="%.2f"
                ),
            },
        )
        st.caption(
            f"iTunes Search order as of {when(serp['captured_at'].iloc[0])} — "
            "**not App Store rank**."
        )
    caveats()


def movers_view(country: str | None, tag: str | None) -> None:
    st.header("Movers")
    days = st.slider("Compare against this many days ago", 1, 60, 7)
    frame = load_movers(days, country, tag)
    if frame.empty:
        st.info("No scored keywords in this filter.")
        return

    moved = frame[frame["opportunity_delta"].notna()]
    unmeasured = len(frame) - len(moved)

    if moved.empty:
        st.info(
            f"No keyword has a snapshot from more than {days} day(s) ago to "
            "compare against. Movers need at least two refreshes spanning that "
            "window."
        )
    else:
        risers = moved[moved["opportunity_delta"] > 0]
        fallers = moved[moved["opportunity_delta"] < 0]
        left, middle, right = st.columns(3)
        left.metric("Comparable", len(moved))
        middle.metric("Up", len(risers))
        right.metric("Down", len(fallers))

        st.dataframe(
            moved[
                [
                    "keyword", "country",
                    "baseline_opportunity", "opportunity_score", "opportunity_delta",
                    "search_delta", "competition_delta",
                ]
            ],
            width="stretch", hide_index=True,
            column_config={
                "baseline_opportunity": st.column_config.NumberColumn(
                    f"{days}d ago", format="%.1f"
                ),
                "opportunity_score": st.column_config.NumberColumn(
                    "now", format="%.1f"
                ),
                "opportunity_delta": st.column_config.NumberColumn(
                    "Δ opportunity", format="%+.1f"
                ),
                "search_delta": st.column_config.NumberColumn(
                    "Δ search", format="%+.1f"
                ),
                "competition_delta": st.column_config.NumberColumn(
                    "Δ competition", format="%+.1f"
                ),
            },
        )

    if unmeasured:
        st.caption(
            f"{unmeasured} keyword(s) have no snapshot older than {days} day(s) "
            "and are omitted rather than shown as zero movement. "
            "*Not measured then* is a different claim from *did not move*."
        )
    caveats()


# --- entry point -----------------------------------------------------------


def main() -> None:
    try:
        with session() as conn:
            migrate(conn)
    except Exception as exc:  # noqa: BLE001 - surface any startup failure in the UI
        st.error(f"Could not open {settings.db_path}: {exc}")
        st.stop()

    view, country, tag = sidebar()
    if view == "Find keywords":
        find_view(country)
    elif view == "Keywords":
        keywords_view(country, tag)
    elif view == "Keyword detail":
        detail_view(country, tag)
    elif view == "Movers":
        movers_view(country, tag)
    else:
        manage_view(country, tag)


# Guarded so the module can be imported by tests. `streamlit run` executes the
# script as __main__, so this still fires in normal use.
if __name__ == "__main__":
    main()
