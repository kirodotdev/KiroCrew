"""The real SEL writer redacts a credential-shaped ``operation`` before it is stored.

So the ``_sel`` route and its client need not refuse such an operation: the row is
written, with the credential replaced (``sel.py`` ``_REDACTED_TEXT_FIELDS`` lists
``operation``; the writer applies it where the batch is flushed). Refusing would only
lose an audit row the writer already makes safe.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.sel import SecurityEventLog

_SECRET_LIKE = [
    "AKIAIOSFODNN7EXAMPLE",
    "ASIA" + "IOSFODNN7EXAMPLE",
    "app.xoxb-1234567890-abcdefghij",
    "app.eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop",
]


@pytest.mark.parametrize("operation", _SECRET_LIKE)
def test_the_writer_stores_a_credential_shaped_operation_redacted(
    tmp_path: Path, operation: str
) -> None:
    log = SecurityEventLog(base_dir=tmp_path, sync=True)
    log.log_api_access(
        caller="app:x", operation=operation, outcome="ok", source="app", critical=True
    )
    log.flush()
    stored = "".join(
        f.read_bytes().decode("utf-8", errors="ignore") for f in tmp_path.rglob("*") if f.is_file()
    )
    secret = operation.removeprefix("app.")
    assert secret not in stored
    assert "[REDACTED: credential]" in stored
