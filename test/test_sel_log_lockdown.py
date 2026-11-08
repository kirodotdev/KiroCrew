"""The SEL audit log is owner-only on Windows too.

Each append locks the live log down before writing, under the calling-thread rule
``config.loader_document`` applies to ``config.json``: off the event loop the
Windows DACL applies on any volume; on the loop a non-local volume is skipped with
a warning, because the DACL write would stall the loop on SMB.
"""

from __future__ import annotations

import logging

import pytest
from sel_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _make_event,
    reset_singleton,
)

from kiro_crew import platform_compat
from kiro_crew import sel as sel_mod
from kiro_crew.sel import SecurityEventLog


@pytest.mark.skipif(
    platform_compat.IS_POSIX,
    reason="Windows DACL branch (POSIX carries access in the mode bits)",
)
def test_windows_append_applies_an_owner_only_dacl(tmp_path) -> None:
    from kiro_crew import windows_acl

    log = SecurityEventLog(base_dir=tmp_path, sync=True)
    log.log(_make_event(), critical=True)

    described = windows_acl.describe(tmp_path / "security_events.jsonl")
    expected = {"S-1-3-4", platform_compat.current_user_sid()}
    writers = {w.sid for w in described.writers}
    assert not described.null_dacl
    assert writers <= expected, f"unexpected writers: {sorted(writers - expected)}"


@pytest.mark.skipif(
    platform_compat.IS_POSIX,
    reason="flipping IS_POSIX on POSIX sends the SEL chain lock to msvcrt",
)
@pytest.mark.parametrize("volume", ["remote", "unknown"])
def test_on_loop_append_skips_a_non_local_volume_and_warns(
    monkeypatch, tmp_path, caplog, volume
) -> None:
    def _volume_is_local(_path) -> bool:
        if volume == "unknown":
            raise OSError("descriptor API unavailable")
        return False

    lockdowns: list[object] = []
    log = SecurityEventLog(base_dir=tmp_path, sync=True)
    monkeypatch.setattr(sel_mod, "_on_event_loop", lambda: True)
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(platform_compat.windows_acl, "volume_is_local", _volume_is_local)
    monkeypatch.setattr(platform_compat, "restrict_to_owner", lockdowns.append)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
        log.log(_make_event(), critical=True)

    assert lockdowns == []
    assert "extras-evt-0001" in (tmp_path / "security_events.jsonl").read_text("utf-8")
    skipped = [r for r in caplog.records if "owner-only DACL was skipped" in r.getMessage()]
    assert len(skipped) == 1
