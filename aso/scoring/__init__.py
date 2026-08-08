"""Scoring: competition, search volume, and the combined opportunity score.

Every function here is pure — it takes parsed data and constants from
`aso.config` and returns numbers. Nothing in this package does I/O, which is
what makes historical re-scoring from stored components possible.
"""
