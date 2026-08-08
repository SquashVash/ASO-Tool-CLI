"""Opportunity score — the default sort everywhere.

    opportunity = search_score * (100 - competition_score) / 100

Read it as "expected volume you could actually win": demand, discounted by how
much of the field is already taken. High volume behind a wall of incumbents
scores below moderate volume nobody is contesting.

Both inputs are 0-100, so the output is too. It inherits every caveat of its
inputs — most importantly that `search_score` is an uncalibrated proxy, which
makes opportunity ordinal as well. Rank keywords by it; don't read the number
as a quantity.
"""

from __future__ import annotations


def opportunity(
    search_score: float | None, competition_score: float | None
) -> float | None:
    """Combine the two scores. `None` if either is unknown.

    Unknown is not zero: guessing a missing competition score as 0 would
    promote unmeasured keywords straight to the top of the default sort, which
    is exactly the ranking error this tool exists to avoid.
    """
    if search_score is None or competition_score is None:
        return None
    contested = max(0.0, min(100.0, competition_score))
    volume = max(0.0, min(100.0, search_score))
    return volume * (100.0 - contested) / 100.0
