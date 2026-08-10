"""FastAPI dependencies."""

from __future__ import annotations

from collections.abc import Iterator

from ..store import Store, session


def get_store() -> Iterator[Store]:
    """The keyword list, loaded per request and written back if it changed.

    Per-request rather than process-wide on purpose. The file is small enough
    that reloading it costs nothing measurable, and a long-lived in-memory copy
    would drift from the file the moment a CLI command in another process
    wrote to it — which is exactly what `aso refresh` in a terminal alongside a
    running server does.

    `session` writes only when a handler mutated the store, so the read paths
    never touch the file's mtime.
    """
    with session() as store:
        yield store
