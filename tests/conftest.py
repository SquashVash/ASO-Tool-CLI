from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from aso import db

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A migrated, throwaway database. Never touches the real aso.db."""
    connection = db.connect(tmp_path / "test.db")
    db.migrate(connection)
    try:
        yield connection
    finally:
        connection.close()
