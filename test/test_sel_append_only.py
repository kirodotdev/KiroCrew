"""F3: the SEL and audit logs are made kernel append-only by the gateway.

The agent's own shell shares the sandbox namespace with the in-sandbox SEL
appenders, so a plain read-only mount is not an option (it would break the
legitimate append). Instead the gateway sets the append-only FS attribute
(``FS_APPEND_FL`` / ``SF_APPEND``): ``O_APPEND`` writes keep working while
truncate/overwrite/rename/unlink are refused by the kernel. These tests pin:

* the append path wires :func:`kiro_crew.append_only.set_append_only` in, and
  rotation clears the flag BEFORE the rename and the next append re-applies it;
* the helper degrades gracefully (warn-once, no raise, writer unaffected) where
  the flag cannot be set;
* the shell-audit-log sweep sets the flag and clears it around its own rotation;
* (when this host actually lets us set the flag) an in-sandbox-style truncate/rm
  is refused while the gateway append and ``sel_rotation`` still work.

Shares the ``sel_helpers`` fixtures with ``test_sel.py`` / ``test_sel_rotation.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sel_helpers import (  # noqa: F401 -- fixtures requested by name
    _fill,
    _make_event,
    reset_singleton,
    sel_dir_fixture,
    small_segments_fixture,
)

import kiro_crew.sel as sel_mod
from kiro_crew import append_only, shell_audit_log
from kiro_crew.sel import SecurityEventLog


@pytest.fixture(autouse=True)
def _clear_warn_cache():
    append_only._warned.clear()
    yield
    append_only._warned.clear()


def _can_really_set_append_only(tmp_path: Path) -> bool:
    """True when THIS host/user can actually set the kernel flag (root + fs support)."""
    probe = tmp_path / "_append_only_probe"
    probe.write_text("x", encoding="utf-8")
    try:
        if not append_only.set_append_only(probe):
            return False
        append_only.clear_append_only(probe)
        return True
    finally:
        try:
            append_only.clear_append_only(probe)
            probe.unlink()
        except OSError:
            pass


class TestAppendPathWiring:
    def test_append_sets_the_flag_on_the_live_log(self, sel_dir, monkeypatch):
        calls: list[Path] = []
        real = append_only.set_append_only
        monkeypatch.setattr(
            append_only, "set_append_only", lambda p: calls.append(Path(p)) or real(p)
        )
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        log.log(_make_event(event_id="e1"))
        live = sel_dir / "security_events.jsonl"
        assert live in calls, "append path did not assert the append-only flag on the live log"

    def test_writer_still_appends_when_the_flag_cannot_be_set(self, sel_dir, monkeypatch, caplog):
        def _boom(path, *, enable):
            raise OSError(1, "Operation not permitted")

        monkeypatch.setattr(append_only, "_linux_set_flag", _boom)
        monkeypatch.setattr(append_only, "_macos_set_flag", _boom)
        monkeypatch.setattr(append_only, "IS_LINUX", True)
        with caplog.at_level("WARNING"):
            log = SecurityEventLog(base_dir=sel_dir, sync=True)
            log.log(_make_event(event_id="e1"))
            log.log(_make_event(event_id="e2"))
        live = sel_dir / "security_events.jsonl"
        assert live.exists()
        assert len(live.read_text(encoding="utf-8").strip().splitlines()) == 2
        total, valid = log.verify_integrity()
        assert total == valid == 2
        warnings = [r for r in caplog.records if "append-only" in r.getMessage()]
        assert warnings, "a failed flag set should warn"
        # warn-once per path: two appends, not two warnings.
        assert len(warnings) == 1


class TestRotationToggle:
    def test_rotation_clears_flag_before_rename_then_reapplies(
        self, sel_dir, small_segments, monkeypatch
    ):
        events: list[tuple[str, str]] = []
        real_set = append_only.set_append_only
        real_clear = append_only.clear_append_only

        monkeypatch.setattr(
            append_only, "set_append_only", lambda p: events.append(("set", str(p))) or real_set(p)
        )
        monkeypatch.setattr(
            append_only,
            "clear_append_only",
            lambda p: events.append(("clear", str(p))) or real_clear(p),
        )
        real_replace = os.replace

        def _recording_replace(src, dst, *a, **k):
            events.append(("replace", str(src)))
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(sel_mod._sel_rotation.os, "replace", _recording_replace)

        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)

        assert log._segments_oldest_first(), "no rotation happened; cannot test the toggle"
        live = str(sel_dir / "security_events.jsonl")
        kinds = [k for k, p in events if p == live]
        assert "replace" in kinds, "the live log was never renamed"
        clear_idx = kinds.index("clear") if "clear" in kinds else -1
        replace_idx = kinds.index("replace")
        assert clear_idx != -1 and clear_idx < replace_idx, (
            f"clear must precede the rename; order for live log was {kinds}"
        )
        # The fresh live log is re-asserted append-only by the next append.
        assert kinds[-1] == "set" or "set" in kinds[replace_idx + 1 :], (
            f"flag not re-applied after rotation; order was {kinds}"
        )
        # The appender still produced a verifiable chain across the rotation.
        total, valid = log.verify_integrity()
        assert total == valid and total > 0


class TestShellAuditLog:
    def test_sweep_sets_flag_below_cap_and_does_not_rotate(self, tmp_path, monkeypatch):
        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            append_only, "set_append_only", lambda p, **k: calls.append(("set", str(p))) or True
        )
        monkeypatch.setattr(
            append_only, "clear_append_only", lambda p, **k: calls.append(("clear", str(p))) or True
        )
        path = shell_audit_log.shell_audit_log_path(tmp_path)
        path.write_text("one small line\n", encoding="utf-8")
        rotated = shell_audit_log.rotate_shell_audit_log(tmp_path)
        assert rotated is False
        assert ("set", str(path)) in calls
        assert ("clear", str(path)) not in calls  # no rename below the cap

    def test_sweep_clears_flag_before_rotating_over_cap(self, tmp_path, monkeypatch):
        order: list[str] = []
        monkeypatch.setattr(
            append_only, "set_append_only", lambda p, **k: order.append("set") or True
        )
        monkeypatch.setattr(
            append_only, "clear_append_only", lambda p, **k: order.append("clear") or True
        )
        monkeypatch.setattr(
            shell_audit_log, "rotate_jsonl_at", lambda *a, **k: order.append("rename")
        )
        monkeypatch.setattr(shell_audit_log, "SHELL_AUDIT_LOG_MAX_BYTES", 8)
        path = shell_audit_log.shell_audit_log_path(tmp_path)
        path.write_text("this is definitely over eight bytes\n", encoding="utf-8")
        shell_audit_log.rotate_shell_audit_log(tmp_path)
        assert "clear" in order and "rename" in order
        assert order.index("clear") < order.index("rename"), (
            f"append-only must be cleared before the rename; order was {order}"
        )


class TestRealKernelFlag:
    """End-to-end with the real attribute -- skipped where the host can't set it."""

    def test_flag_refuses_truncate_and_unlink_but_allows_append_and_rotation(
        self, sel_dir, small_segments
    ):
        if not _can_really_set_append_only(sel_dir):
            pytest.skip("host cannot set the append-only flag (needs root + a supporting fs)")
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        live = sel_dir / "security_events.jsonl"
        try:
            log.log(_make_event(event_id="e1"))
            assert live.exists()
            # O_APPEND write still works (the in-sandbox appender's path).
            fd = os.open(live, os.O_WRONLY | os.O_APPEND)
            try:
                os.write(fd, b'{"appended":"ok"}\n')
            finally:
                os.close(fd)
            # Truncate / overwrite / unlink are refused by the kernel.
            with pytest.raises(OSError):
                os.open(live, os.O_WRONLY | os.O_TRUNC)
            with pytest.raises(OSError):
                live.unlink()
            # Rotation (gateway) still works: it clears the flag around the rename.
            _fill(log, 200)
            assert log._segments_oldest_first(), "rotation did not run under the flag"
        finally:
            # Clear the flag on every SEL file so pytest can clean up tmp_path.
            append_only.clear_append_only(live)
            seg_dir = sel_dir / "security_events.d"
            if seg_dir.is_dir():
                for f in seg_dir.iterdir():
                    append_only.clear_append_only(f)
