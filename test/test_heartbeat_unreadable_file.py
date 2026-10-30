"""A HEARTBEAT.md the tick cannot read must not stop the rest of the tick.

The file step runs first in ``_beat``. Before this fix both reads of the file
caught only ``FileNotFoundError``, so a task list saved in a legacy code page
(``UnicodeDecodeError``) raised out of the step on every tick and skipped the
FTS rebuild, retention pruning and idle-session consolidation that follow it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import kiro_crew.heartbeat as hb_mod
from kiro_crew.heartbeat import _HEADER, HeartbeatService

# "café" in latin-1: byte 0xe9 is not valid UTF-8.
_LATIN1_TASKS = b"# Heartbeat\n- [ ] caf\xe9 check\n"


async def _tick(svc: HeartbeatService) -> None:
    """Run one tick; a raise is the bug, since ``_loop`` would drop the rest of it."""
    try:
        await svc._beat()
    except Exception as exc:
        pytest.fail(f"the tick raised {exc!r} instead of skipping the file")


@pytest.mark.asyncio
async def test_undecodable_file_does_not_skip_the_rest_of_the_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "HEARTBEAT.md"
    target.write_bytes(_LATIN1_TASKS)
    monkeypatch.setattr(hb_mod, "heartbeat_path", lambda: target)
    on_task = MagicMock()
    consolidator = MagicMock()
    svc = HeartbeatService(memory=MagicMock(), on_task=on_task, consolidator=consolidator)
    svc._tick = 1  # neither the FTS rebuild nor the prune tick
    svc._memory_backup_started = True

    await _tick(svc)

    on_task.assert_not_called()
    consolidator.check_idle_sessions.assert_called_once_with()
    assert target.read_bytes() == _LATIN1_TASKS, "an unreadable file must be left as it is"


@pytest.mark.asyncio
async def test_file_turning_undecodable_mid_cycle_is_left_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rewrite re-reads the file to merge mid-cycle appends. If that
    re-read cannot decode it, overwriting would destroy content nobody could
    read, so the rewrite is skipped and the tick carries on."""
    target = tmp_path / "HEARTBEAT.md"
    target.write_text(f"{_HEADER}- do a thing\n", encoding="utf-8")
    monkeypatch.setattr(hb_mod, "heartbeat_path", lambda: target)

    async def _on_task(_text: str, _deliver: str) -> str:
        target.write_bytes(_LATIN1_TASKS)
        return "done"

    consolidator = MagicMock()
    svc = HeartbeatService(memory=MagicMock(), on_task=_on_task, consolidator=consolidator)
    svc._tick = 1
    svc._memory_backup_started = True

    await _tick(svc)

    consolidator.check_idle_sessions.assert_called_once_with()
    assert target.read_bytes() == _LATIN1_TASKS
