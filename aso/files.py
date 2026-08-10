"""JSON files on disk, and the timestamp format they store.

Everything this tool keeps is now a list of flat records in a JSON file: the
keyword list, the fitted bridges, the measured observations. This module is the
one place that reads and writes them, so there is one place to look when a file
is malformed and one place to change when the layout moves.

WHY ONE ROW PER LINE
--------------------
`json.dumps(rows, indent=2)` turns 3,120 observations into 26,000 lines, and
every edit rewrites a block of them, so `git diff` shows churn instead of the
change. These files are checked in and reviewed, so they are written with one
compact record per line: the diff then names the row that moved. The result is
still ordinary JSON and `json.load` reads it without knowing any of this.

WHY WRITES ARE ATOMIC
---------------------
A refresh interrupted halfway through writing `keywords.json` must not leave a
truncated file where the keyword list used to be. Writes go to a temporary file
in the same directory and are renamed over the target, which is atomic on both
POSIX and Windows for same-volume renames.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utcnow() -> str:
    """Current time as an ISO-8601 UTC string, second precision.

    Timestamps sort lexicographically in this format, which is what every
    "most recent" comparison in the codebase relies on.
    """
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_ts(value: str) -> datetime:
    """Inverse of `utcnow()`; also accepts plain `+00:00` offsets."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class MalformedData(ValueError):
    """A data file exists but does not hold what it should.

    Raised rather than swallowed. A missing file is a first run and reads as an
    empty list; a file whose contents are wrong is a broken install, and
    silently treating it as empty would discard measurements or a keyword list
    and then cheerfully write the emptiness back.
    """


def read_rows(path: Path, key: str) -> list[dict[str, Any]]:
    """Rows under `key`, or `[]` if the file does not exist yet."""
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as exc:
        raise MalformedData(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or key not in payload:
        raise MalformedData(f"{path} has no {key!r} key")
    rows = payload[key]
    if not isinstance(rows, list):
        raise MalformedData(f"{path}: {key!r} is {type(rows).__name__}, not a list")
    return rows


def write_rows(path: Path, key: str, rows: list[dict[str, Any]], **header: Any) -> None:
    """Replace `path` with `rows`, one record per line, atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = json.dumps({"generated_at": utcnow(), **header})[1:-1]
    body = ",\n".join("    " + json.dumps(row) for row in rows)
    text = "{" + meta + f',\n  "{key}": [\n' + (body + "\n" if rows else "") + "  ]\n}\n"

    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle as tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
