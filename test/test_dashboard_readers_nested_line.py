"""A usage, telemetry or transcript line nested past the recursion limit is skipped.

``json.loads`` raises ``RecursionError`` on such a line, and it is not a
``ValueError``. Each dashboard reader below skips an unparseable line and keeps
reading, so it skips this one too instead of raising out of the whole read.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from stray_line_helpers import cyclic_gc_quiesced  # noqa: F401 - fixture
from stray_line_helpers import too_deep_to_decode

import kiro_crew.dashboard.handlers.usage as usage_mod
from kiro_crew.dashboard import slot_buffers
from kiro_crew.dashboard.handlers import telemetry
from kiro_crew.dashboard.handlers.usage_series import _shard_rows

pytestmark = pytest.mark.usefixtures("cyclic_gc_quiesced")

_SLOT = "chat-1-1700000000"


def _no_raise(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """*fn*'s result; a ``RecursionError`` fails the test (not errors it)."""
    try:
        return fn(*args, **kwargs)
    except RecursionError:
        pytest.fail(f"{getattr(fn, '__name__', fn)} raised RecursionError on a nested line")


def _row(now: datetime) -> dict[str, Any]:
    return {
        "_type": "tokens",
        "ts": now.isoformat(),
        "slot": _SLOT,
        "model": "m",
        "credits": 2.5,
        "input_tokens": 10,
        "output_tokens": 5,
    }


@pytest.fixture
def shard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_tz) -> Path:
    """Today's usage shard: a nested line, then one ordinary row.

    The zone is pinned to UTC and the instant is read ONCE, so the shard's day name
    and the row's ``ts`` cannot fall on different days.
    """
    local_tz("UTC")
    now = datetime.now(timezone.utc)
    d = tmp_path / "tokens"
    d.mkdir()
    path = d / f"{now:%Y-%m-%d}.jsonl"
    path.write_text(too_deep_to_decode() + "\n" + json.dumps(_row(now)) + "\n", encoding="utf-8")
    monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", d)
    monkeypatch.setattr(usage_mod, "_shards_in_window", lambda *a, **k: [path])
    for name in ("_CONTEXT_CACHE", "_CONTEXT_CACHE_KEY", "_COST_CACHE", "_COST_CACHE_KEY"):
        monkeypatch.setattr(usage_mod, name, None)
    monkeypatch.setattr(usage_mod, "_TOKEN_CACHE", {})
    monkeypatch.setattr(usage_mod, "_TOKEN_CACHE_KEY", None)
    return path


def test_slot_spend_skips_a_nested_line(shard: Path) -> None:
    spend = _no_raise(usage_mod.slot_spend)
    assert spend[f"dashboard:{_SLOT}"]["credits"] == 2.5


def test_daily_credits_skips_a_nested_line(shard: Path) -> None:
    assert sum(_no_raise(usage_mod.daily_credits).values()) == 2.5


def test_slot_turn_usage_skips_a_nested_line(shard: Path) -> None:
    assert len(_no_raise(usage_mod.slot_turn_usage, _SLOT)) == 1


def test_context_occupancy_skips_a_nested_line(shard: Path) -> None:
    assert isinstance(_no_raise(usage_mod.context_occupancy), dict)


def test_cost_breakdown_skips_a_nested_line(shard: Path) -> None:
    assert isinstance(_no_raise(usage_mod.cost_breakdown), dict)


def test_token_history_skips_a_nested_line(shard: Path) -> None:
    assert isinstance(_no_raise(usage_mod._parse_token_history), dict)


def test_series_shard_rows_skip_a_nested_line(shard: Path) -> None:
    rows, _refused = _no_raise(_shard_rows, shard, 100)
    assert len(rows) == 1


def test_telemetry_export_cycles_skip_a_nested_line(tmp_path: Path) -> None:
    shard = tmp_path / "metrics-2026-11-03-123.jsonl"
    shard.write_text(too_deep_to_decode() + "\n" + json.dumps({"ok": 1}) + "\n", encoding="utf-8")
    cycles = _no_raise(lambda: list(telemetry._iter_export_cycles([shard])))
    assert cycles == [({"ok": 1}, "2026-11-03", "123")]


def test_note_probe_skips_a_nested_line(tmp_path: Path) -> None:
    note_id = "note-42"
    transcript = tmp_path / "k.jsonl"
    nested = too_deep_to_decode()
    # The probe parses only lines that carry the note id, so the nested line does.
    mid = len(nested) // 2
    poisoned = nested[:mid] + f'"{note_id}",' + nested[mid:]
    committed = {"role": "assistant", "content": "x", "meta": {"noteId": note_id}}
    transcript.write_text(poisoned + "\n" + json.dumps(committed) + "\n", encoding="utf-8")

    class _Log:
        def _path(self, key: str) -> str:
            return str(transcript)

    assert _no_raise(slot_buffers._note_row_committed, _Log(), "k", note_id) is True
