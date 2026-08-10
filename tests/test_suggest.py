"""Keyword discovery from Apple's own completions.

The promise being tested: every rung of the prefix ladder is probed, not just
the ones the scorer bothers with. See the spec — `search.observe` stops at the
first miss, which is right for scoring and backwards for discovery.
"""

from __future__ import annotations

import pytest

from aso import store as store_module
from aso import suggest as suggest_module


class FakeHints:
    """A probe backed by a fixed prefix -> suggestions mapping.

    Records what it was asked, so a test can assert on the *walk* and not only
    on its result — which is the whole difference between reusing the scorer's
    early-stopping ladder and not.
    """

    def __init__(self, responses: dict[str, list[str]]) -> None:
        self.responses = responses
        self.asked: list[str] = []

    async def __call__(self, prefix: str) -> list[str]:
        self.asked.append(prefix)
        return list(self.responses.get(prefix, []))


async def collect(responses, keyword="habit", country="us", **kwargs):
    probe = FakeHints(responses)
    result = await suggest_module.collect(keyword, country, probe=probe, **kwargs)
    return result, probe


# --- the walk --------------------------------------------------------------


async def test_every_rung_is_probed_even_after_a_miss() -> None:
    """The `insta` case, and the regression test for reusing `observe`.

    Nothing ever surfaces "habit" itself, so the scorer's ladder stops after
    one probe. Discovery must keep going — those later rungs are where the
    candidates are.
    """
    result, probe = await collect({"hab": ["habit tracker", "habitica"]})

    assert probe.asked == ["habit", "habi", "hab", "ha", "h"]
    assert [c.term for c in result.candidates] == ["habit tracker", "habitica"]


async def test_a_keyword_shorter_than_the_floor_still_gets_probed() -> None:
    result, probe = await collect({"ab": ["abc"]}, keyword="ab")
    assert probe.asked == ["ab", "a"]


# --- dedup and provenance --------------------------------------------------


async def test_a_term_seen_at_several_rungs_keeps_the_deepest_prefix() -> None:
    """Deepest prefix is the strongest RELEVANCE claim, so it is the one kept.

    Shortest prefix would be the stronger *demand* claim, and for discovery
    that is the wrong question: a term that only survives at "h" is a popular
    app, not a term about your seed.
    """
    result, _ = await collect(
        {
            "habit": ["habit tracker"],
            "habi": ["habit tracker"],
            "hab": ["habit tracker"],
        }
    )

    assert len(result.candidates) == 1
    assert result.candidates[0].prefix == "habit"


async def test_surfaced_by_counts_every_rung_that_mentioned_it() -> None:
    """Dedup must not silently discard how widely a term was offered."""
    result, _ = await collect(
        {"habit": ["habit tracker"], "hab": ["habit tracker"], "h": ["habit tracker"]}
    )
    assert result.candidates[0].surfaced_by == 3


async def test_rank_is_the_position_in_the_deepest_prefixs_list() -> None:
    result, _ = await collect(
        {
            "habit": ["other", "habit tracker"],
            "hab": ["habit tracker"],
        }
    )
    found = next(c for c in result.candidates if c.term == "habit tracker")
    assert (found.prefix, found.rank) == ("habit", 2)


# --- ordering --------------------------------------------------------------


async def test_a_deeper_prefix_outranks_a_better_rank_at_a_shallower_one() -> None:
    """Relevance leads, rank breaks ties.

    A term Apple still offers once you have typed five characters is about
    your seed. One that only appeared after one character is a popular app
    that happens to share a letter.
    """
    result, _ = await collect(
        {
            "habit": ["deep term"],
            "ha": ["other", "shallow term"],
        }
    )
    assert [c.term for c in result.candidates] == [
        "deep term",
        "other",
        "shallow term",
    ]


async def test_rank_breaks_ties_within_one_prefix() -> None:
    result, _ = await collect({"hab": ["first", "second", "third"]})
    assert [c.term for c in result.candidates] == ["first", "second", "third"]


# --- what is excluded ------------------------------------------------------


async def test_the_keyword_itself_is_never_a_candidate() -> None:
    """You asked about it; offering it back is not a discovery."""
    result, _ = await collect({"hab": ["habit", "habit tracker"]})
    assert [c.term for c in result.candidates] == ["habit tracker"]


async def test_tracked_terms_are_excluded_by_default(store: store_module.Store) -> None:
    store.add_keyword("habit tracker", "us")
    store.save()

    result, _ = await collect({"hab": ["habit tracker", "habitica"]})

    assert [c.term for c in result.candidates] == ["habitica"]


async def test_include_tracked_returns_them_flagged(store: store_module.Store) -> None:
    store.add_keyword("habit tracker", "us")
    store.save()

    result, _ = await collect(
        {"hab": ["habit tracker", "habitica"]}, include_tracked=True
    )

    assert [(c.term, c.tracked) for c in result.candidates] == [
        ("habit tracker", True),
        ("habitica", False),
    ]


async def test_tracking_is_matched_on_the_normalized_form(
    store: store_module.Store,
) -> None:
    """Case and spacing must not make a tracked keyword look new."""
    store.add_keyword("habit tracker", "us")
    store.save()

    result, _ = await collect({"hab": ["  Habit   Tracker "]})

    assert result.candidates == []


async def test_a_tracked_term_in_another_storefront_is_still_new(
    store: store_module.Store,
) -> None:
    store.add_keyword("habit tracker", "de")
    store.save()

    result, _ = await collect({"hab": ["habit tracker"]})

    assert [c.term for c in result.candidates] == ["habit tracker"]


# --- failure ---------------------------------------------------------------


async def test_a_failure_partway_keeps_what_was_collected() -> None:
    """Partial results stay partial — eight rungs beat an exception."""

    class Failing(FakeHints):
        async def __call__(self, prefix: str) -> list[str]:
            if prefix == "hab":
                raise ValueError("hints: HTTP 403")
            return await super().__call__(prefix)

    probe = Failing({"habit": ["habit tracker"]})
    result = await suggest_module.collect("habit", "us", probe=probe)

    assert result.failed is True
    assert "403" in result.error
    assert [c.term for c in result.candidates] == ["habit tracker"]


async def test_a_clean_walk_reports_no_failure() -> None:
    result, _ = await collect({"hab": ["habitica"]})
    assert result.failed is False
    assert result.error is None


async def test_prefixes_probed_is_reported() -> None:
    result, _ = await collect({"hab": ["habitica"]})
    assert result.prefixes_probed == ["habit", "habi", "hab", "ha", "h"]


# --- input validation, before any request ----------------------------------


@pytest.mark.parametrize("keyword", ["", "   ", "\n"])
async def test_blank_keywords_are_rejected_without_probing(keyword: str) -> None:
    probe = FakeHints({})
    with pytest.raises(ValueError, match="blank"):
        await suggest_module.collect(keyword, "us", probe=probe)
    assert probe.asked == []


async def test_blank_country_is_rejected_without_probing() -> None:
    probe = FakeHints({})
    with pytest.raises(ValueError, match="blank"):
        await suggest_module.collect("habit", "  ", probe=probe)
    assert probe.asked == []
