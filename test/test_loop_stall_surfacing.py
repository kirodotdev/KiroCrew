"""Tests for surfacing a loop-stall hard exit, and for its configurable budget.

Background. The gateway's event loop runs the HTTP server, every agent turn and
all background work on one thread. When it wedges, a faulthandler timer dumps
every thread's stack and calls ``os._exit()`` — skipping every ``except``,
``finally`` and persistence flush. systemd restarts the process seconds later,
but the user was never told a crash happened: a monitoring loop that was
mid-round simply stopped, with fixes written and never committed.

Three contracts are covered here:

* the hard-exit budget is configurable rather than hard-coded;
* managed services use a wider budget than Electron-supervised desktop runs;
  and
* a dump is re-detected on every start for up to 7 days, so notifying about it
  unconditionally would turn one stall into a week of identical alerts.

The same re-detection is why a dump carries a second question beyond its age:
whether a later gateway session has already started after it. One that has is
history — reporting it as a current fault buries the run's real finding under a
week of the same stack.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from kiro_crew.config.loader import (
    LOOP_STALL_EXIT_AFTER_MAX,
    LOOP_STALL_EXIT_AFTER_MIN,
    KiroCrewConfig,
    _clamp_security_bounds,
    consume_managed_service_launch_environment,
    load_loop_stall_exit_after,
    resolve_loop_stall_exit_after,
)
from kiro_crew.dashboard.crash_dump_store import (
    claim_dump_notification,
    current_process_identity,
    dump_superseded,
    dumps_with_stacks,
)
from kiro_crew.dashboard.loop_watchdog import LoopStallWatchdog


def _dump(tmp_path: Path, name: str = "loopstall-20260803T000000Z.txt") -> Path:
    p = tmp_path / name
    p.write_text("# opened 2026-08-03\n# PID: 1\n#\n\nTimeout (0:00:25)!\n", encoding="utf-8")
    return p


class TestClaimDumpNotification:
    def test_first_claim_succeeds(self, tmp_path) -> None:
        assert claim_dump_notification(_dump(tmp_path), tmp_path) is True

    def test_second_claim_for_the_same_dump_is_refused(self, tmp_path) -> None:
        """One stall must not alert on every restart for a week."""
        dump = _dump(tmp_path)
        assert claim_dump_notification(dump, tmp_path) is True
        assert claim_dump_notification(dump, tmp_path) is False
        assert claim_dump_notification(dump, tmp_path) is False

    def test_a_new_dump_claims_again(self, tmp_path) -> None:
        """A second, genuinely different stall must still be surfaced."""
        first = _dump(tmp_path, "loopstall-20260803T000000Z.txt")
        second = _dump(tmp_path, "loopstall-20260804T000000Z.txt")
        assert claim_dump_notification(first, tmp_path) is True
        assert claim_dump_notification(second, tmp_path) is True
        assert claim_dump_notification(second, tmp_path) is False

    def test_marker_is_not_mistaken_for_a_dump(self, tmp_path) -> None:
        """The marker lives in the dumps dir; rotation must ignore it."""
        from kiro_crew.dashboard.crash_dump_store import _list_dumps

        dump = _dump(tmp_path)
        claim_dump_notification(dump, tmp_path)
        assert (tmp_path / ".notified").is_file()
        assert _list_dumps(tmp_path) == [dump]

    def test_unwritable_marker_notifies_rather_than_going_silent(self, tmp_path) -> None:
        """A suppressed crash alert is a worse failure than a duplicate one."""
        dump = _dump(tmp_path)
        missing = tmp_path / "does-not-exist"
        assert claim_dump_notification(dump, missing) is True


class TestDumpSuperseded:
    """A stall a healthy LOCAL session has already replaced is history, not a fault."""

    def _write(
        self,
        tmp_path: Path,
        name: str,
        *,
        stacks: bool,
        mtime: float,
        domain: str | None = None,
        pid_line: bool = True,
    ) -> Path:
        if domain is None:
            domain = current_process_identity()[0]
        header = f"# PID: 1 @ {domain}\n" if pid_line else "#\n"
        body = f"# opened\n{header}#\n\n"
        if stacks:
            body += "Timeout (0:00:25)!\nThread 0x1 (most recent call first):\n"
        p = tmp_path / name
        p.write_text(body, encoding="utf-8")
        # mtime is the ordering key _list_dumps sorts on; two files written in
        # the same test would otherwise be ordered by filesystem granularity.
        os.utime(p, (mtime, mtime))
        return p

    def test_a_lone_stall_dump_is_not_superseded(self, tmp_path) -> None:
        dump = self._write(tmp_path, "loopstall-20260901T000000Z.txt", stacks=True, mtime=1_000)
        assert dump_superseded(dump, tmp_path) is False

    def test_a_later_local_session_supersedes_it(self, tmp_path) -> None:
        """The next gateway's own header-only file is the proof it started."""
        dump = self._write(tmp_path, "loopstall-20260901T000000Z.txt", stacks=True, mtime=1_000)
        self._write(tmp_path, "loopstall-20260902T000000Z.txt", stacks=False, mtime=2_000)
        assert dump_superseded(dump, tmp_path) is True

    def test_an_earlier_session_does_not_supersede_it(self, tmp_path) -> None:
        self._write(tmp_path, "loopstall-20260831T000000Z.txt", stacks=False, mtime=1_000)
        dump = self._write(tmp_path, "loopstall-20260901T000000Z.txt", stacks=True, mtime=2_000)
        assert dump_superseded(dump, tmp_path) is False

    def test_a_later_dump_carrying_stacks_does_not_supersede_it(self, tmp_path) -> None:
        """A newer file with stacks is a SECOND stall, not a session that survived."""
        dump = self._write(tmp_path, "loopstall-20260901T000000Z.txt", stacks=True, mtime=1_000)
        self._write(tmp_path, "loopstall-20260902T000000Z.txt", stacks=True, mtime=2_000)
        assert dump_superseded(dump, tmp_path) is False

    def test_a_foreign_domains_file_does_not_supersede_it(self, tmp_path) -> None:
        """A shared data home collects files whose existence says nothing about a
        LOCAL restart -- the case rotate_dumps and sweep_stale_dumps already model."""
        dump = self._write(tmp_path, "loopstall-20260901T000000Z.txt", stacks=True, mtime=1_000)
        self._write(
            tmp_path,
            "loopstall-20260902T000000Z.txt",
            stacks=False,
            mtime=2_000,
            domain="some-other-host/pid:[4026531836]",
        )
        assert dump_superseded(dump, tmp_path) is False

    def test_an_unattributable_successor_does_not_supersede_it(self, tmp_path) -> None:
        """No parseable owner is not evidence of anything; keep the stall visible."""
        dump = self._write(tmp_path, "loopstall-20260901T000000Z.txt", stacks=True, mtime=1_000)
        self._write(
            tmp_path, "loopstall-20260902T000000Z.txt", stacks=False, mtime=2_000, pid_line=False
        )
        assert dump_superseded(dump, tmp_path) is False

    def test_an_unlistable_directory_keeps_the_stall_visible(self, tmp_path) -> None:
        """Hiding a real stall is the expensive direction; fail toward reporting."""
        dump = self._write(tmp_path, "loopstall-20260901T000000Z.txt", stacks=True, mtime=1_000)
        assert dump_superseded(dump, tmp_path / "does-not-exist") is False


class TestDumpsWithStacks:
    """Two stalls on record is a repeating wedge, which restarts do not make history."""

    def _write(self, tmp_path: Path, name: str, *, stacks: bool) -> Path:
        body = "# opened\n# PID: 1\n#\n\n"
        if stacks:
            body += "Timeout (0:00:25)!\nThread 0x1 (most recent call first):\n"
        p = tmp_path / name
        p.write_text(body, encoding="utf-8")
        return p

    def test_counts_only_dumps_carrying_stacks(self, tmp_path) -> None:
        self._write(tmp_path, "loopstall-20260901T000000Z.txt", stacks=True)
        self._write(tmp_path, "loopstall-20260902T000000Z.txt", stacks=False)
        self._write(tmp_path, "loopstall-20260903T000000Z.txt", stacks=True)
        assert dumps_with_stacks(tmp_path) == 2

    def test_an_empty_directory_counts_none(self, tmp_path) -> None:
        assert dumps_with_stacks(tmp_path) == 0


class TestLoopStallBudgetConfig:
    def test_default_remains_automatic_until_launch(self) -> None:
        assert KiroCrewConfig().dashboard.loop_stall_exit_after_secs is None

    def test_managed_service_uses_the_wider_budget(self) -> None:
        assert resolve_loop_stall_exit_after({}, {"KIROCREW_SERVICE_MANAGED": "1"}) == 90
        assert resolve_loop_stall_exit_after({}, {"INVOCATION_ID": "legacy-unit"}) == 25
        assert resolve_loop_stall_exit_after({}, {"SYSTEMD_EXEC_PID": str(os.getpid())}) == 25
        assert resolve_loop_stall_exit_after({}, {"KIROCREW_SERVICE_MANAGED": "0"}) == 25
        assert resolve_loop_stall_exit_after({}, {"INVOCATION_ID": ""}) == 25
        assert resolve_loop_stall_exit_after({}, {}) == 25

    def test_managed_marker_is_consumed_before_children_are_started(self) -> None:
        environment = {
            "KIROCREW_SERVICE_MANAGED": "1",
            "PATH": "/usr/bin",
        }

        launch_environment = consume_managed_service_launch_environment(environment)

        assert launch_environment == {"KIROCREW_SERVICE_MANAGED": "1"}
        assert environment == {"PATH": "/usr/bin"}
        assert resolve_loop_stall_exit_after({}, launch_environment) == 90
        assert resolve_loop_stall_exit_after({}, environment) == 25

    def test_operator_budget_is_preserved_for_managed_services(self) -> None:
        explicit = {"loop_stall_exit_after_secs": 25}
        assert resolve_loop_stall_exit_after(explicit, {"KIROCREW_SERVICE_MANAGED": "1"}) == 25

    def test_watchdog_arms_with_the_managed_service_budget(self) -> None:
        arms: list[float] = []
        watchdog = LoopStallWatchdog(
            exit_after=resolve_loop_stall_exit_after({}, {"KIROCREW_SERVICE_MANAGED": "1"}),
            arm_later=arms.append,
            cancel_later=lambda: None,
        )
        watchdog.start()
        try:
            assert arms == [90]
        finally:
            watchdog.stop()

    def test_automatic_default_survives_unrelated_full_config_save(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.config.loader import _invalidate_config_cache

        monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
        _invalidate_config_cache()
        try:
            assert load_loop_stall_exit_after({"KIROCREW_SERVICE_MANAGED": "1"}) == 90
            cfg = KiroCrewConfig.load()
            assert cfg.dashboard.loop_stall_exit_after_secs is None
            cfg.save()
            saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
            assert saved["dashboard"]["loop_stall_exit_after_secs"] is None
            assert load_loop_stall_exit_after({"KIROCREW_SERVICE_MANAGED": "1"}) == 90
            assert load_loop_stall_exit_after({}) == 25
        finally:
            _invalidate_config_cache()

    def test_schema_explains_the_automatic_launch_class_defaults(self) -> None:
        from kiro_crew.config.schema import SCHEMA_REGISTRY

        entry = next(
            item for item in SCHEMA_REGISTRY if item.path == "dashboard.loop_stall_exit_after_secs"
        )
        assert entry.nullable is True
        assert entry.default_value is None
        assert "25 seconds" in entry.help
        assert "90 seconds" in entry.help

    def test_legacy_materialized_desktop_default_is_reported_not_rewritten(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.config.loader import _invalidate_config_cache

        (tmp_path / "config.json").write_text(
            json.dumps({"dashboard": {"loop_stall_exit_after_secs": 25}}),
            encoding="utf-8",
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
        _invalidate_config_cache()
        try:
            assert KiroCrewConfig.load().dashboard.loop_stall_exit_after_secs == 25
            assert load_loop_stall_exit_after({"KIROCREW_SERVICE_MANAGED": "1"}) == 25
            saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
            assert saved["dashboard"]["loop_stall_exit_after_secs"] == 25
        finally:
            _invalidate_config_cache()

    def test_explicit_desktop_default_is_preserved_for_managed_service(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.config.loader import _invalidate_config_cache

        (tmp_path / "config.json").write_text(
            json.dumps({"dashboard": {"loop_stall_exit_after_secs": 25}}),
            encoding="utf-8",
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
        _invalidate_config_cache()
        try:
            assert KiroCrewConfig.load().dashboard.loop_stall_exit_after_secs == 25
            assert load_loop_stall_exit_after({"KIROCREW_SERVICE_MANAGED": "1"}) == 25
        finally:
            _invalidate_config_cache()

    def test_local_operator_budget_overrides_base_and_managed_default(
        self, tmp_path, monkeypatch
    ) -> None:
        (tmp_path / "config.json").write_text(
            json.dumps({"dashboard": {"loop_stall_exit_after_secs": 25}}),
            encoding="utf-8",
        )
        (tmp_path / "config.local.json").write_text(
            json.dumps({"dashboard": {"loop_stall_exit_after_secs": 60}}),
            encoding="utf-8",
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)

        assert load_loop_stall_exit_after({"KIROCREW_SERVICE_MANAGED": "1"}) == 60

    def test_configured_value_is_read(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.config.loader import _invalidate_config_cache

        cfg_dir = tmp_path / "cfgdir"
        cfg_dir.mkdir()
        (cfg_dir / "config.json").write_text(
            json.dumps({"dashboard": {"loop_stall_exit_after_secs": 60}}), encoding="utf-8"
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: cfg_dir)
        _invalidate_config_cache()
        try:
            assert KiroCrewConfig.load().dashboard.loop_stall_exit_after_secs == 60
        finally:
            _invalidate_config_cache()

    def test_out_of_range_values_are_clamped(self) -> None:
        """The budget is a recovery mechanism; it cannot be set to never fire."""
        data = {"dashboard": {"loop_stall_exit_after_secs": 99999}}
        _clamp_security_bounds(data)
        assert data["dashboard"]["loop_stall_exit_after_secs"] == LOOP_STALL_EXIT_AFTER_MAX

        data = {"dashboard": {"loop_stall_exit_after_secs": 1}}
        _clamp_security_bounds(data)
        assert data["dashboard"]["loop_stall_exit_after_secs"] == LOOP_STALL_EXIT_AFTER_MIN


class TestChatTurnCeilingConfig:
    def test_default_preserves_existing_behaviour(self, tmp_path, monkeypatch) -> None:
        """A config that omits the key must load the dataclass default.

        The loader carries its own fallback literal for the key; a drift between
        it and ``AgentConfig`` makes an on-disk config with the key omitted
        behave differently from ``KiroCrewConfig()``.
        """
        from kiro_crew.config.loader import AgentConfig, _invalidate_config_cache

        (tmp_path / "config.json").write_text(json.dumps({"agent": {}}), encoding="utf-8")
        monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
        _invalidate_config_cache()
        try:
            loaded = KiroCrewConfig.load().agent.chat_turn_timeout_secs
        finally:
            _invalidate_config_cache()
        assert loaded == AgentConfig().chat_turn_timeout_secs
        assert KiroCrewConfig().agent.chat_turn_timeout_secs == AgentConfig().chat_turn_timeout_secs

    def test_out_of_range_values_are_clamped(self) -> None:
        from kiro_crew.config.loader import CHAT_TURN_TIMEOUT_MAX, CHAT_TURN_TIMEOUT_MIN

        data = {"agent": {"chat_turn_timeout_secs": 999999}}
        _clamp_security_bounds(data)
        assert data["agent"]["chat_turn_timeout_secs"] == CHAT_TURN_TIMEOUT_MAX

        data = {"agent": {"chat_turn_timeout_secs": 5}}
        _clamp_security_bounds(data)
        assert data["agent"]["chat_turn_timeout_secs"] == CHAT_TURN_TIMEOUT_MIN
