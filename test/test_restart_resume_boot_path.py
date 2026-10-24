"""The dispatcher stays off the gateway boot path.

Split from ``test_restart_resume.py`` (the 2,000-line cap); the boot-path tests
are self-contained and read the dispatcher only through its public module.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import restart_resume


class TestTheDeferredReading:
    """The restart marker is read twice: once from the metadata line as the slot
    is built, and again (``AppliedMeta.settle``) once the window is loaded and
    the marker reconciled. The window load awaits a worker thread, so a person's
    Stop can land between the two readings; ``cancel_armed_retry`` clears the
    flag and the second reading must not put it back (GPT review, rev 48)."""

    def test_settle_keeps_a_stop_that_landed_during_the_window_load(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.slot_persistence import metadata_codec as codec

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("cut")
        meta = {"_type": "metadata", "turn_in_flight_generation": 3}
        applied = codec.apply(state, slot, meta, codec.Restore(name="cut", member=None))
        assert slot._restart_cut_turn is True  # the first reading
        slot.append("user", "do the thing", "msg msg-u")
        slot._disk_window_len = len(slot.messages)

        restart_resume.cancel_armed_retry(slot)  # a Stop while the window loads
        had_marker = applied.settle(list(slot.messages))

        assert had_marker is True  # the marker was there; the person still won
        assert slot._restart_cut_turn is False

    def test_settle_writes_the_marker_when_nothing_cleared_it(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.slot_persistence import metadata_codec as codec

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("cut")
        applied = codec.apply(
            state,
            slot,
            {"_type": "metadata", "turn_in_flight_generation": 3},
            codec.Restore(name="cut", member=None),
        )
        slot.append("user", "do the thing", "msg msg-u")
        slot._disk_window_len = len(slot.messages)

        assert applied.settle(list(slot.messages)) is True
        assert slot._restart_cut_turn is True


class TestBootPath:
    """The dispatcher is optional and off by default, so the gateway boot path must
    not load it (AUTOSDE ``no-new-work-on-gateway-boot-path``: gate the import, not
    just the handler). Everything that runs before the socket binds -- the server
    and chat facades' imports, the restore's per-slot metadata read -- stays clear
    of it; the module is imported on a person's own action, or by the restore
    only when the switch is on."""

    def test_the_facades_and_the_restore_read_do_not_import_the_dispatcher(self, tmp_path):
        import os
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(root / "src"), str(root / "test")])
        env["KIROCREW_HOME"] = str(tmp_path / "crew")
        # Mirrors a default-off launch: the two facades import, then the codec reads
        # a metadata line carrying a budget list -- a tampered one, so the
        # fail-closed validation still runs in full -- for a restored slot.
        script = """
import importlib, sys
from pathlib import Path
importlib.import_module("kiro_crew.dashboard.server")
importlib.import_module("kiro_crew.dashboard.chat_handlers")
assert "kiro_crew.dashboard.restart_resume" not in sys.modules, "a facade imports the dispatcher"
import kiro_crew.dashboard.state as st
st.config_dir = lambda: Path(sys.argv[1])
from chat_test_helpers import _make_state
from kiro_crew.dashboard.slot_persistence import metadata_codec as codec
state = _make_state(Path(sys.argv[1]))
slot = state.get_or_create_slot("restored")
codec.apply(
    state, slot, {"_type": "metadata", "auto_resume_stamps": "garbage"},
    codec.Restore(name="restored", member=None),
)
assert len(slot._auto_resume_stamps) == 64, slot._auto_resume_stamps
assert "kiro_crew.dashboard.restart_resume" not in sys.modules, "the restore read imports the dispatcher"
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, str(tmp_path)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )

        assert result.returncode == 0, result.stderr[-2000:]

    @pytest.mark.asyncio
    async def test_a_default_off_restore_does_not_reach_the_dispatcher(self, tmp_path, monkeypatch):
        state, cfg, calls = await self._restore(tmp_path, monkeypatch, enabled=False)

        assert calls == []

    @pytest.mark.asyncio
    async def test_a_restore_with_the_switch_on_schedules_the_boot_pass(
        self, tmp_path, monkeypatch
    ):
        state, cfg, calls = await self._restore(tmp_path, monkeypatch, enabled=True)

        assert calls == [("armed", state), (state, cfg)]

    @pytest.mark.asyncio
    async def test_the_restore_arms_the_cut_chats_before_its_next_await(
        self, tmp_path, monkeypatch
    ):
        """A channel Stop can land while the restore still awaits (the recent-session
        restore follows the open-tab one); a cut chat's key is armed the moment the
        open tabs exist, so that Stop is recorded (GPT review, rev 50)."""
        order: list[str] = []

        await self._restore(tmp_path, monkeypatch, enabled=True, order=order)

        assert order == ["open", "armed", "recent", "boot"]

    @staticmethod
    async def _restore(tmp_path, monkeypatch, *, enabled: bool, order: list[str] | None = None):
        # The restore body with its own collaborators stubbed: no sessions to
        # rebuild, no cautious-boot pause, no sweeps. What is left is the gate.
        from kiro_crew.dashboard import server
        from kiro_crew.dashboard.server_runtime import session_restore

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        cfg = SimpleNamespace(
            dashboard=SimpleNamespace(
                resume_interrupted_turns=enabled,
                restore_sessions=False,
                restore_window_minutes=0,
                idle_slot_sweep_days=0,
                surface_channel_sessions=False,
            )
        )
        seen = order if order is not None else []
        monkeypatch.setattr(server.cautious_boot, "pause_before", AsyncMock())

        async def open_slots(st):
            seen.append("open")

        async def recent(st, *a, **k):
            seen.append("recent")
            return 0

        monkeypatch.setattr(server.chat, "restore_open_slots_async", open_slots)
        monkeypatch.setattr(server.chat, "restore_recent_sessions_async", recent)
        calls: list[tuple] = []

        def arm(st):
            seen.append("armed")
            calls.append(("armed", st))
            return 0

        def boot_pass(st, c):
            seen.append("boot")
            calls.append((st, c))
            return MagicMock()

        monkeypatch.setattr(restart_resume, "arm_cut_chats", arm, raising=False)
        monkeypatch.setattr(restart_resume, "start_boot_pass", boot_pass)

        await session_restore._restore_dashboard_sessions(state, cfg)
        return state, cfg, calls
