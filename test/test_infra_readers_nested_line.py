"""A diag, stub-fallback or inbound-spool line nested past the recursion limit is skipped.

``json.loads`` raises ``RecursionError`` on such a line, and it is not a
``ValueError``. Each reader below skips an unparseable line and keeps reading
(the stub-fallback counter promises it never raises), so it skips this one too.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from stray_line_helpers import cyclic_gc_quiesced  # noqa: F401 - fixture
from stray_line_helpers import too_deep_to_decode

from kiro_crew.diag import recorder as recorder_mod
from kiro_crew.mcp_gateway import stub_fallback
from kiro_crew.messaging import inbound_spool

pytestmark = pytest.mark.usefixtures("cyclic_gc_quiesced")


def _no_raise(fn: Callable[..., Any], *args: Any) -> Any:
    """*fn*'s result; a ``RecursionError`` fails the test (not errors it)."""
    try:
        return fn(*args)
    except RecursionError:
        pytest.fail(f"{getattr(fn, '__name__', fn)} raised RecursionError on a nested line")


def test_the_diag_query_skips_a_nested_line(tmp_path: Path) -> None:
    now = time.time()
    rec = recorder_mod.Recorder(config_dir=tmp_path, env={}, clock=lambda: now)
    day_file = (
        rec._dir() / f"{recorder_mod.FILE_PREFIX}{rec._day_key(now)}{recorder_mod.FILE_SUFFIX}"
    )
    day_file.parent.mkdir(parents=True)
    day_file.write_text(
        too_deep_to_decode() + "\n" + json.dumps({"ts": now, "x": 1}) + "\n", encoding="utf-8"
    )

    rows = _no_raise(lambda: list(rec._iter_rows(now - 60, now + 60)))

    assert rows == [{"ts": now, "x": 1}]


def test_the_stub_fallback_counter_skips_a_nested_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "logs" / "stub_fallback.jsonl"
    log.parent.mkdir()
    good = json.dumps({"ts": time.time(), "server": "s1", "reason": "r"})
    log.write_text(too_deep_to_decode() + "\n" + good + "\n", encoding="utf-8")
    monkeypatch.setattr(stub_fallback, "_fallback_log_path", lambda: log)

    counts = _no_raise(stub_fallback.fallback_counts)

    assert counts["total"] == 1
    assert counts["by_server"] == {"s1": 1}


def test_the_inbound_spool_skips_a_nested_line(tmp_path: Path) -> None:
    spool = tmp_path / "spool.jsonl"
    entry = {"channel_type": "telegram", "conversation_id": "42", "text": "hello"}
    spool.write_text(too_deep_to_decode() + "\n" + json.dumps(entry) + "\n", encoding="utf-8")

    entries = _no_raise(inbound_spool._read_entries, spool)

    assert [(e.channel_type, e.conversation_id, e.text) for e in entries] == [
        ("telegram", "42", "hello")
    ]
