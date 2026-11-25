"""YOLO and chat trust are kept across every gateway exit the user did not choose.

A boot after a restart, a crash, a kill or a host reboot finds no user-stop mark and
restores the signed records; a boot after ``kirocrew stop``, the desktop Quit or
Ctrl+C finds the mark and restores nothing. These tests pin the shared store
(``kept_grants``), the YOLO half (``kept_yolo``) and every writer of the mark. The
chat half is pinned by ``test_chat_trust_restart_notice.py``.

A "new process" here is the module state a fresh image starts with: the boot verdict
and the recorders reset, and a new ``SafetyOverride`` singleton.
"""

from __future__ import annotations

import json
import signal
import stat
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from conftest import requires_symlinks
from kiro_crew import atomic_write, kept_grants, kept_yolo
from kiro_crew import safety_override as so
from kiro_crew import safety_override_grant as so_grant
from kiro_crew.dashboard import token_secret
from kiro_crew.testing.wait import async_wait_until


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data home of its own, its own signing key, and grant publishes run inline."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(token_secret, "_SECRET", None)
    monkeypatch.setattr(token_secret, "_EPHEMERAL_SECRET", None)
    monkeypatch.setattr(so, "_breadcrumb_path", lambda: tmp_path / "last_grant.json")
    monkeypatch.setattr(so, "_enqueue_job", lambda job: job())
    monkeypatch.setattr(so, "sel", MagicMock())
    _new_process()
    yield tmp_path
    _new_process()


def _new_process() -> None:
    kept_grants._reset_for_tests()
    kept_yolo._reset_for_tests()
    so.reset_singleton()


def _record_path(home: Path, name: str) -> Path:
    return home / kept_grants.KEPT_GRANTS_LEAF / name


def _boot_and_arm() -> kept_yolo.KeptYolo | None:
    """One boot's YOLO step, after which this process records its own grant."""
    return kept_yolo.restore_at_boot()


def _audits(mock: MagicMock) -> list[tuple[str, str, str]]:
    return [
        (c.kwargs.get("operation", ""), c.kwargs.get("outcome", ""), c.kwargs.get("resources", ""))
        for c in mock.return_value.log_api_access.call_args_list
    ]


class TestTheStore:
    def test_a_record_reads_back_only_as_written(self, home: Path) -> None:
        assert kept_grants.write_record("x.json", "chats", {"a": 1})
        assert kept_grants.read_record("x.json", "chats") == {"a": 1}
        # Another kind cannot be replayed as this one.
        assert kept_grants.read_record("x.json", "yolo") is None
        assert not _record_path(home, "x.json").exists()

    @pytest.mark.parametrize(
        "tamper",
        [
            lambda e: e["body"].update(a=2),
            lambda e: e.update(mac="0" * 64),
            lambda e: e.pop("mac"),
            lambda e: e.update(version=2),
        ],
        ids=["changed_body", "wrong_mac", "unsigned", "wrong_version"],
    )
    def test_a_defective_record_grants_nothing_and_is_removed(self, home: Path, tamper) -> None:
        assert kept_grants.write_record("x.json", "chats", {"a": 1})
        path = _record_path(home, "x.json")
        envelope = json.loads(path.read_text(encoding="utf-8"))
        tamper(envelope)
        path.write_text(json.dumps(envelope), encoding="utf-8")

        audit = MagicMock()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("kiro_crew.sel.sel", audit)
            assert kept_grants.read_record("x.json", "chats") is None
        assert not path.exists()
        [(operation, outcome, _resources)] = _audits(audit)
        assert (operation, outcome) == ("kept_grants:record_rejected", "denied")

    def test_garbage_and_oversized_files_grant_nothing(self, home: Path) -> None:
        kept_grants.write_record("x.json", "chats", {})
        path = _record_path(home, "x.json")
        path.write_text("not json", encoding="utf-8")
        assert kept_grants.read_record("x.json", "chats") is None
        path.write_text("x" * (kept_grants._MAX_RECORD_BYTES + 1), encoding="utf-8")
        assert kept_grants.read_record("x.json", "chats") is None
        assert not path.exists()

    def test_nothing_is_written_without_a_persisted_signing_key(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert kept_grants.write_record("x.json", "chats", {"a": 1})

        def _ephemeral() -> bytes:
            raise RuntimeError("only a temporary key")

        monkeypatch.setattr(token_secret, "get_persisted_secret", _ephemeral)
        assert not kept_grants.write_record("x.json", "chats", {"a": 2})
        # The record it cannot sign is gone, not left for a later boot.
        assert not _record_path(home, "x.json").exists()

    @requires_symlinks
    def test_a_link_at_the_directory_name_is_refused(self, home: Path, tmp_path_factory) -> None:
        elsewhere = tmp_path_factory.mktemp("elsewhere")
        (home / kept_grants.KEPT_GRANTS_LEAF).symlink_to(elsewhere, target_is_directory=True)
        assert not kept_grants.write_record("x.json", "chats", {"a": 1})
        assert not kept_grants.mark_user_stop("cli")
        assert list(elsewhere.iterdir()) == []

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
    def test_a_loose_existing_directory_is_made_owner_only(self, home: Path) -> None:
        directory = home / kept_grants.KEPT_GRANTS_LEAF
        directory.mkdir()
        directory.chmod(0o755)
        assert kept_grants.write_record("x.json", "chats", {"a": 1})
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    def test_a_missing_data_home_is_not_recreated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gone = tmp_path / "gone"
        assert not kept_grants.write_record("x.json", "chats", {}, home=gone)
        assert not gone.exists()


class TestTheUserStopMark:
    def test_no_mark_keeps(self, home: Path) -> None:
        kept_grants.write_record(kept_grants.CHATS_RECORD, "chats", {"chats": []})
        assert kept_grants.boot_verdict() == kept_grants.VERDICT_KEPT
        assert _record_path(home, kept_grants.CHATS_RECORD).exists()

    def test_a_mark_ends_every_record_once(self, home: Path) -> None:
        kept_grants.write_record(kept_grants.CHATS_RECORD, "chats", {"chats": [1]})
        kept_grants.write_record(kept_grants.YOLO_RECORD, "yolo", {"source": "dashboard"})
        assert kept_grants.mark_user_stop("cli")

        assert kept_grants.boot_verdict() == kept_grants.VERDICT_USER_STOP
        for name in (kept_grants.CHATS_RECORD, kept_grants.YOLO_RECORD, kept_grants.USER_STOP_FILE):
            assert not _record_path(home, name).exists()
        # Readable for the owners' audits, at this boot only.
        assert kept_grants.cleared_record(kept_grants.YOLO_RECORD) == {"source": "dashboard"}
        # Decided once per process image: the same answer, nothing read again.
        assert kept_grants.boot_verdict() == kept_grants.VERDICT_USER_STOP
        _new_process()
        assert kept_grants.boot_verdict() == kept_grants.VERDICT_KEPT

    def test_a_guarded_write_lands_only_while_still_current(self, home: Path) -> None:
        # A write sampled before a revocation must not be put in place after it.
        import threading

        lock = threading.Lock()
        assert not kept_grants.write_record(
            "x.json", "chats", {"a": 1}, commit=(lock, lambda: False)
        )
        directory = home / kept_grants.KEPT_GRANTS_LEAF
        assert sorted(p.name for p in directory.iterdir()) == []
        assert kept_grants.write_record("x.json", "chats", {"a": 2}, commit=(lock, lambda: True))
        assert kept_grants.read_record("x.json", "chats") == {"a": 2}
        assert sorted(p.name for p in directory.iterdir()) == ["x.json"]

    def test_a_repeat_stop_that_stops_nothing_keeps_the_first_stops_mark(self, home: Path) -> None:
        kept_grants.run_user_stop(lambda: None, via="cli")

        def _no_gateway() -> None:
            raise SystemExit(1)

        with pytest.raises(SystemExit):
            kept_grants.run_user_stop(_no_gateway, via="cli")
        assert _record_path(home, kept_grants.USER_STOP_FILE).exists()

    def test_a_gateway_without_a_dashboard_keeps_and_records_yolo_too(self) -> None:
        # The headless server boot runs the same restore, which arms the recorder.
        import inspect

        from kiro_crew.dashboard.server_runtime import api_entrypoint

        source = inspect.getsource(api_entrypoint.start_api_server)
        # Scheduled, never awaited on the boot path.
        assert "create_task(\n        asyncio.to_thread(kept_yolo.restore_at_boot)" in source
        assert "await asyncio.to_thread(kept_yolo.restore_at_boot)" not in source

    def test_a_stop_that_stopped_nothing_takes_its_mark_back(self, home: Path) -> None:
        def _no_gateway() -> None:
            raise SystemExit(1)

        with pytest.raises(SystemExit):
            kept_grants.run_user_stop(_no_gateway, via="cli")
        assert not _record_path(home, kept_grants.USER_STOP_FILE).exists()

        kept_grants.run_user_stop(lambda: None, via="cli")
        assert _record_path(home, kept_grants.USER_STOP_FILE).exists()


@pytest.fixture
def syncs(home: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Every sync of the record directory: its thread, and the record and lock then."""
    seen: list[dict] = []
    real = atomic_write.fsync_dir

    def _spy(path, **kwargs) -> None:
        seen.append(
            {
                "dir": Path(path),
                "thread": threading.get_ident(),
                "yolo_record": _record_path(home, kept_grants.YOLO_RECORD).exists(),
                "io_lock_held": kept_yolo._io_lock.locked(),
            }
        )
        real(path, **kwargs)

    monkeypatch.setattr(kept_grants, "fsync_dir", _spy, raising=False)
    return seen


class TestAnEndedGrantSurvivesAPowerLoss:
    """A delete that ends a grant, a guarded write's rename and the user-stop mark are
    synced to disk before they are acknowledged, never on the event loop's thread and
    never under a lock the loop takes."""

    def test_a_guarded_write_syncs_its_rename_after_the_lock(
        self, home: Path, syncs: list[dict]
    ) -> None:
        _boot_and_arm()
        _grant(3600)
        assert syncs
        assert all(s["yolo_record"] and not s["io_lock_held"] for s in syncs)

    @pytest.mark.asyncio
    async def test_settle_syncs_a_yolo_off_on_a_worker_thread_after_the_off(
        self, home: Path, syncs: list[dict]
    ) -> None:
        _boot_and_arm()
        _grant(3600)
        syncs.clear()
        # Slack turns YOLO off on the loop, then awaits ``settle`` before it answers.
        so.safety_override().deactivate("slack")
        assert syncs == [], "the off itself must not wait on the disk"
        assert await kept_grants.settle() is True
        [synced] = syncs
        assert synced["dir"] == home / kept_grants.KEPT_GRANTS_LEAF
        assert synced["thread"] != threading.get_ident()
        assert synced["yolo_record"] is False and synced["io_lock_held"] is False

    def test_the_user_stop_mark_is_synced_file_and_directory(
        self, home: Path, syncs: list[dict], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fsync_flags: list[object] = []
        real_write = kept_grants.atomic_write

        def _write(path, text, **kwargs) -> None:
            fsync_flags.append(kwargs.get("fsync"))
            real_write(path, text, **kwargs)

        monkeypatch.setattr(kept_grants, "atomic_write", _write)
        assert kept_grants.mark_user_stop("cli")
        assert fsync_flags == [True]
        assert [s["dir"] for s in syncs] == [home / kept_grants.KEPT_GRANTS_LEAF]


def _grant(ttl: int | None, source: str = "dashboard") -> None:
    result = so.safety_override().activate(source, ttl=ttl)
    assert result.active


def _one_hour_cap() -> int:
    return 3600


def _yolo_body(home: Path) -> dict:
    body = kept_grants.read_record(kept_grants.YOLO_RECORD, "yolo")
    assert body is not None
    return body


def test_the_per_test_reset_drops_the_transition_listener(home: Path) -> None:
    from kiro_crew import safety_override_breadcrumb

    _boot_and_arm()
    assert kept_yolo.request_sync in safety_override_breadcrumb._transition_listeners
    kept_yolo._reset_for_tests()
    assert kept_yolo.request_sync not in safety_override_breadcrumb._transition_listeners
    assert kept_yolo._listening is False
    # The next boot registers it again, once.
    _boot_and_arm()
    assert safety_override_breadcrumb._transition_listeners.count(kept_yolo.request_sync) == 1


class TestYoloIsKept:
    def test_a_restart_keeps_a_timed_grant_with_its_original_expiry(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _boot_and_arm()
        _grant(3600)
        deadline = _yolo_body(home)["expires_at"]

        _new_process()
        # Both clocks pinned for the new process, so the check reads no live time.
        wall, mono = deadline - 1234.0, 5000.0
        clock = SimpleNamespace(time=lambda: wall, monotonic=lambda: mono)
        monkeypatch.setattr(kept_yolo, "time", clock)
        monkeypatch.setattr(so_grant, "time", clock)
        kept = _boot_and_arm()
        assert kept is not None and kept.source == "dashboard"
        override = so.safety_override()
        assert override.is_active()
        # The same wall-clock deadline: never a fresh TTL.
        assert kept.remaining_secs == 1234
        assert override.remaining_secs() == 1234

    def test_a_crash_keeps_it_too(self, home: Path) -> None:
        # No shutdown step runs on a crash: the record each transition wrote is all
        # the next boot has, and it is enough.
        _boot_and_arm()
        _grant(600)
        _new_process()
        assert _boot_and_arm() is not None
        assert so.safety_override().is_active()

    def test_until_shutdown_lasts_until_the_user_stops(self, home: Path) -> None:
        _boot_and_arm()
        override = so.safety_override()
        override.adhoc_until_shutdown = True
        _grant(None)
        assert _yolo_body(home)["permanent"] is True

        _new_process()
        so.safety_override().adhoc_until_shutdown = True
        kept = _boot_and_arm()
        assert kept is not None and kept.remaining_secs == -1
        assert so.safety_override().is_permanent

    def test_an_expired_grant_does_not_come_back(self, home: Path) -> None:
        _boot_and_arm()
        _grant(600)
        body = _yolo_body(home)
        body["expires_at"] = time.time() - 1
        kept_grants.write_record(kept_grants.YOLO_RECORD, "yolo", body)

        _new_process()
        assert _boot_and_arm() is None
        assert not so.safety_override().is_active()
        # And this process records that nothing is live.
        assert not _record_path(home, kept_grants.YOLO_RECORD).exists()

    def test_a_policy_that_now_denies_yolo_restores_nothing(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _boot_and_arm()
        _grant(600)
        _new_process()
        monkeypatch.setattr(so, "yolo_policy_permits", lambda: False)
        assert _boot_and_arm() is None
        assert not so.safety_override().has_grant()

    def test_the_cap_counts_from_the_original_activation(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _boot_and_arm()
        _grant(3600)
        body = _yolo_body(home)
        body["activated_at"] = time.time() - 50 * 60
        kept_grants.write_record(kept_grants.YOLO_RECORD, "yolo", body)

        _new_process()
        monkeypatch.setattr(so, "yolo_ttl_cap", _one_hour_cap)
        kept = _boot_and_arm()
        assert kept is not None
        assert kept.remaining_secs <= 10 * 60
        # A later clamp still measures from the original activation.
        assert so.safety_override().clamp_to_cap(3000) is True

    def test_a_cap_installed_during_the_restore_counts_from_the_original_activation(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The restore read no cap; a 1h cap lands before the commit. The grant is
        # already 5000 s old, so it must not come back for its 2000 s left.
        _boot_and_arm()
        _grant(6 * 3600)
        body = _yolo_body(home)
        body["activated_at"] = time.time() - 5000
        body["expires_at"] = time.time() + 2000
        kept_grants.write_record(kept_grants.YOLO_RECORD, "yolo", body)

        _new_process()
        monkeypatch.setattr(so, "yolo_ttl_cap", lambda: 0)
        monkeypatch.setattr(type(so.safety_override()), "_bounded_cap", lambda self: 3600)
        assert _boot_and_arm() is None
        assert not so.safety_override().is_active()

    def test_a_grant_the_owner_turns_on_during_the_restore_is_not_replaced(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _boot_and_arm()
        _grant(600)
        _new_process()
        turned_on: list = []
        permits = so.yolo_policy_permits

        def _policy_read_while_owner_turns_on() -> bool:
            # Past the restore's own "a grant is live" check, before its commit.
            if not turned_on:
                turned_on.append(None)
                turned_on[0] = so.safety_override().activate("slack", 7200)
            return permits()

        monkeypatch.setattr(so, "yolo_policy_permits", _policy_read_while_owner_turns_on)
        _boot_and_arm()
        assert turned_on and turned_on[0].active
        override = so.safety_override()
        assert override.is_active() and override._source == "slack"
        # The owner's 2h grant stands, not the restored 10 min one.
        assert override.remaining_secs() > 3600

    def test_a_clock_that_moved_back_restores_nothing(self, home: Path) -> None:
        _boot_and_arm()
        _grant(600)
        body = _yolo_body(home)
        body["activated_at"] = time.time() + 3600
        kept_grants.write_record(kept_grants.YOLO_RECORD, "yolo", body)
        _new_process()
        assert _boot_and_arm() is None
        assert not so.safety_override().is_active()

    def test_an_off_switch_before_the_boot_restore_ran_wins(self, home: Path) -> None:
        # The owner picks Normal once the server accepts requests, before the
        # deferred restore runs: nothing is live yet, and nothing may come back.
        _boot_and_arm()
        _grant(600)
        _new_process()
        so.safety_override().deactivate("dashboard")
        assert _boot_and_arm() is None
        assert not so.safety_override().is_active()

    def test_an_off_switch_before_the_restore_survives_a_crash(self, home: Path) -> None:
        # Normal picked before the boot restored anything, then the process dies:
        # the next boot must not restore the grant the owner turned off.
        _boot_and_arm()
        _grant(600)
        _new_process()
        so.safety_override().deactivate("dashboard")
        _new_process()
        assert _boot_and_arm() is None
        assert not so.safety_override().is_active()

    def test_an_off_switch_during_the_boot_restore_wins(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The owner picks Normal while the restore is still reading the record: no
        # grant exists yet, and the restore must not turn one on after that pick.
        _boot_and_arm()
        _grant(600)
        _new_process()
        real_read = kept_grants.read_record

        def _read_then_owner_picks_normal(name: str, kind: str):
            body = real_read(name, kind)
            so.safety_override().deactivate("dashboard")
            return body

        monkeypatch.setattr(kept_grants, "read_record", _read_then_owner_picks_normal)
        assert _boot_and_arm() is None
        assert not so.safety_override().is_active()

    def test_turning_it_off_removes_the_record_before_any_queued_write_runs(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A crash right after the user turned YOLO off: the queued jobs never run,
        # so the record must already be gone when the change returns.
        _boot_and_arm()
        _grant(600)
        assert _record_path(home, kept_grants.YOLO_RECORD).exists()
        queued: list = []
        monkeypatch.setattr(so, "_enqueue_job", queued.append)
        so.safety_override().deactivate("dashboard")
        assert queued, "the ordered sync is still queued"
        assert not _record_path(home, kept_grants.YOLO_RECORD).exists()

        _new_process()
        assert _boot_and_arm() is None
        assert not so.safety_override().has_grant()

    def test_a_user_stop_ends_it(self, home: Path) -> None:
        _boot_and_arm()
        _grant(600)
        kept_grants.mark_user_stop("cli")

        _new_process()
        assert _boot_and_arm() is None
        assert not so.safety_override().has_grant()
        assert not _record_path(home, kept_grants.YOLO_RECORD).exists()

    def test_a_tampered_record_restores_nothing(self, home: Path) -> None:
        _boot_and_arm()
        _grant(60)
        path = _record_path(home, kept_grants.YOLO_RECORD)
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["body"]["expires_at"] += 86400
        path.write_text(json.dumps(envelope), encoding="utf-8")

        _new_process()
        assert _boot_and_arm() is None
        assert not so.safety_override().has_grant()

    @pytest.mark.parametrize("source", ["config", "cli"])
    def test_a_declared_grant_is_never_recorded(self, home: Path, source: str) -> None:
        _boot_and_arm()
        so.safety_override().activate_declared(source)
        assert not _record_path(home, kept_grants.YOLO_RECORD).exists()

    def test_turning_it_off_removes_the_record(self, home: Path) -> None:
        _boot_and_arm()
        _grant(600)
        so.safety_override().deactivate("dashboard")
        assert not _record_path(home, kept_grants.YOLO_RECORD).exists()

    def test_nothing_is_recorded_before_the_boot_read_the_previous_record(self, home: Path) -> None:
        _grant(600)
        assert not _record_path(home, kept_grants.YOLO_RECORD).exists()


class TestEveryUserStopMarks:
    def test_kirocrew_stop_marks_and_the_pid_pinned_stop_does_not(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import cli, cli_server

        monkeypatch.setattr(cli_server, "_stop", lambda *a, **k: None)
        monkeypatch.setattr("sys.argv", ["kirocrew", "stop", "--port", "1", "--expect-pid", "2"])
        cli.main()
        assert not _record_path(home, kept_grants.USER_STOP_FILE).exists()

        monkeypatch.setattr("sys.argv", ["kirocrew", "stop"])
        cli.main()
        assert _record_path(home, kept_grants.USER_STOP_FILE).exists()

    def test_the_desktop_stop_and_restart_keeps_the_grants(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # "Stop and restart" of an older gateway is a restart, not a user stop.
        from kiro_crew import cli, cli_server, port_resolution

        stopped: list = []
        monkeypatch.setattr(cli_server, "_stop", lambda *a, **k: stopped.append(a))
        monkeypatch.setattr(port_resolution, "_gateway_owns_port", lambda port: True)
        monkeypatch.setattr("sys.argv", ["kirocrew", "stop", "--port", "5476", "--keeps-grants"])
        cli.main()
        assert stopped
        assert not _record_path(home, kept_grants.USER_STOP_FILE).exists()

    def test_a_port_stop_it_cannot_prove_is_this_homes_still_marks_and_says_so(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Unproven ownership (Windows, a stale run/ file, another home) fails toward
        # ending this home's grants, never toward keeping a stopped gateway's.
        from kiro_crew import cli, cli_server, port_resolution

        mark = _record_path(home, kept_grants.USER_STOP_FILE)
        stopped: list = []
        monkeypatch.setattr(cli_server, "_stop", lambda *a, **k: stopped.append(a))
        monkeypatch.setattr(port_resolution, "_gateway_owns_port", lambda port: False)
        monkeypatch.setattr("sys.argv", ["kirocrew", "stop", "--port", "18977"])
        cli.main()
        assert stopped and mark.exists()
        assert "could not confirm the gateway on port 18977" in capsys.readouterr().out

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body, marks", [({"user_stop": True}, True), ({}, False), (None, False)]
    )
    async def test_the_shutdown_route_marks_only_a_user_stop(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, body, marks: bool
    ) -> None:
        import asyncio

        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.handlers import core

        # The handler imports the event at call time; a private one is never the
        # process's own, so the stop it schedules ends nothing.
        monkeypatch.setattr("kiro_crew.shutdown_event", asyncio.Event())
        monkeypatch.setattr(core, "_sel", MagicMock())
        app = web.Application()
        app["local_secret"] = "s3cret"
        app.router.add_post("/api/shutdown", core.api_shutdown)
        async with TestClient(TestServer(app)) as client:
            kwargs = {"json": body} if body is not None else {}
            resp = await client.post(
                "/api/shutdown", headers={"X-Local-Secret": "s3cret"}, **kwargs
            )
            assert resp.status == 200
        assert _record_path(home, kept_grants.USER_STOP_FILE).exists() is marks

    def test_ctrl_c_marks_and_sigterm_does_not(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SIGTERM is what a service manager sends at a host reboot, so it keeps."""
        import asyncio
        import functools

        from kiro_crew.slack import gateway as gw

        monkeypatch.setattr(gw, "platform_compat", MagicMock(name="platform_compat"))
        monkeypatch.setattr(gw, "shutdown_event", MagicMock(name="shutdown_event"))

        def _install() -> dict[object, object]:
            handlers: dict[object, object] = {}
            loop = asyncio.get_running_loop()
            monkeypatch.setattr(
                loop,
                "add_signal_handler",
                lambda sig, cb, *args: handlers.__setitem__(sig, functools.partial(cb, *args)),
            )
            gw.GatewayOrchestrator.__new__(
                gw.GatewayOrchestrator
            )._install_shutdown_signal_handlers()
            return handlers

        async def _drive() -> None:
            _install()[signal.SIGTERM]()
            assert not _record_path(home, kept_grants.USER_STOP_FILE).exists()
            gw.shutdown_event.set.reset_mock()
            _install()[signal.SIGINT]()
            # The shutdown starts at once; the mark is written on its own thread.
            assert gw.shutdown_event.set.called
            await async_wait_until(
                lambda: _record_path(home, kept_grants.USER_STOP_FILE).exists(), off_loop=True
            )
            assert _record_path(home, kept_grants.USER_STOP_FILE).exists()

        asyncio.run(_drive())


class TestTheAgentCannotReachIt:
    def test_the_directory_is_masked_precreated_and_unaliasable(self) -> None:
        from kiro_crew import sandbox

        leaf = kept_grants.KEPT_GRANTS_LEAF
        assert leaf in sandbox._CREW_HIDDEN_LEAVES
        assert leaf in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
        assert leaf in sandbox._CREW_NO_ALIAS_LEAVES
        assert leaf not in sandbox._CREW_READONLY_LEAVES
        assert leaf not in sandbox._CREW_SANDBOX_VISIBLE_LEAVES

    def test_the_file_tools_refuse_it(self, home: Path) -> None:
        from kiro_crew import security

        crew = Path.home() / ".kiro" / "crew" / kept_grants.KEPT_GRANTS_LEAF
        assert security.is_sensitive_path(str(crew / kept_grants.YOLO_RECORD))
        assert security.is_sensitive_path(str(crew / kept_grants.USER_STOP_FILE))
