"""Gateway startup survives an unparseable ``crons.json``.

A store that fails to load refuses every write. Gateway startup writes to it --
the dashboard's app startup hook disarms the crons of an app the gateway will
not run -- so one corrupt file made every launch exit with code 1 and nothing
served the dashboard port. Startup now renames the file to a timestamped
``crons.json.corrupt-<utc>`` copy, runs on an empty store, and says so on the
dashboard and in ``kirocrew doctor``.

Every test drives a REAL ``CronService`` over a real malformed file. The bytes
are asserted unchanged in the copy, because "never rewrite the original" is the
property a user relies on to restore their jobs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web

import kiro_crew.apps.hooks_integration as hooks_mod
from kiro_crew.apps.bridges import disarm_app_crons_for_execution
from kiro_crew.apps.route_registry import RouteRegistry
from kiro_crew.cron import CronService, CronStoreUnreadable
from kiro_crew.cron_service.store import quarantine_copies, quarantine_unreadable_store

# Invalid JSON part-way through a real-looking store, like a truncated or
# hand-edited file: the job above the break is what a user wants back.
_MALFORMED = (
    b'{\n  "version": 2,\n  "jobs": [\n    {"id": "j-keep", "name": "keep"}\n    ,,\n  ]\n}\n'
)


def _write_malformed(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    store = base / "crons.json"
    store.write_bytes(_MALFORMED)
    return store


# ── the store helper ─────────────────────────────────────────────────────────


def test_a_malformed_store_is_renamed_with_its_bytes_intact(tmp_path: Path) -> None:
    store = _write_malformed(tmp_path)

    moved = quarantine_unreadable_store(store)

    assert moved is not None
    assert not store.exists(), "the live store must be gone so the next load reads empty"
    assert moved.parent == tmp_path / "cron-history" / "quarantine"
    assert moved.name.startswith("crons.json.corrupt-")
    assert moved.read_bytes() == _MALFORMED
    assert quarantine_copies(store) == [moved]


def test_the_copy_is_behind_the_same_fence_as_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy holds session keys and commands, so the agent's file tools must refuse it."""
    from kiro_crew.security import is_sensitive_path

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    store = _write_malformed(tmp_path)
    assert is_sensitive_path(str(store))

    moved = quarantine_unreadable_store(store)

    assert moved is not None
    assert is_sensitive_path(str(moved))


@pytest.mark.parametrize(
    "raw",
    [b'{"version": 2, "jobs": []}', b'{"version": 2, "jobs": [{"id": "x"}]}'],
    ids=["empty", "one-bad-record"],
)
def test_a_parseable_store_is_left_in_place(tmp_path: Path, raw: bytes) -> None:
    """A bad RECORD is salvage territory for the loader, not a reason to move the file."""
    store = tmp_path / "crons.json"
    store.write_bytes(raw)

    assert quarantine_unreadable_store(store) is None
    assert store.read_bytes() == raw
    assert quarantine_copies(store) == []


def test_a_missing_store_moves_nothing(tmp_path: Path) -> None:
    assert quarantine_unreadable_store(tmp_path / "crons.json") is None


def test_a_read_error_leaves_the_file_where_it_is(tmp_path: Path) -> None:
    """An OSError says nothing about the bytes, so the store is not moved."""
    store = tmp_path / "crons.json"
    store.mkdir()

    assert quarantine_unreadable_store(store) is None
    assert store.is_dir()


def test_a_second_quarantine_in_the_same_second_does_not_overwrite_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.cron_service.store.time.strftime", lambda *_a: "STAMP")
    store = _write_malformed(tmp_path)
    first = quarantine_unreadable_store(store)
    store.write_bytes(b"{second")
    second = quarantine_unreadable_store(store)

    assert first is not None and second is not None and first != second
    assert first.read_bytes() == _MALFORMED
    assert second.read_bytes() == b"{second"


# ── the gateway factory ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_quarantines_and_leaves_a_writable_empty_store(tmp_path: Path) -> None:
    store = _write_malformed(tmp_path)

    svc = await CronService.create(base_dir=tmp_path, quarantine_unreadable=True)

    moved = svc.quarantined_store
    assert moved is not None and moved.read_bytes() == _MALFORMED
    assert svc.list_jobs(include_disabled=True) == []
    svc.raise_if_store_unreadable()  # the latch is clear: writes are accepted
    assert not store.exists()


@pytest.mark.asyncio
async def test_create_without_the_flag_keeps_refusing(tmp_path: Path) -> None:
    """Only gateway startup moves the file; every other opener keeps today's refusal."""
    store = _write_malformed(tmp_path)

    svc = await CronService.create(base_dir=tmp_path)

    assert svc.quarantined_store is None
    with pytest.raises(CronStoreUnreadable):
        svc.raise_if_store_unreadable()
    assert store.read_bytes() == _MALFORMED


@pytest.mark.asyncio
async def test_a_lock_that_cannot_be_opened_does_not_abort_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unwritable lock file keeps the old refusal; it never raises out of startup."""
    store = _write_malformed(tmp_path)

    def _denied(*_a: object, **_k: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("kiro_crew.cron_service.store.platform_compat.open_lock_file", _denied)

    svc = await CronService.create(base_dir=tmp_path, quarantine_unreadable=True)

    assert svc.quarantined_store is None
    with pytest.raises(CronStoreUnreadable):
        svc.raise_if_store_unreadable()
    assert store.read_bytes() == _MALFORMED


@pytest.mark.asyncio
async def test_the_boot_disarm_that_crashed_startup_now_succeeds(tmp_path: Path) -> None:
    """The exact write that raised out of the dashboard's startup hook."""
    _write_malformed(tmp_path)
    refusing = await CronService.create(base_dir=tmp_path)
    with pytest.raises(CronStoreUnreadable):
        await disarm_app_crons_for_execution("probe-app", refusing)

    svc = await CronService.create(base_dir=tmp_path, quarantine_unreadable=True)

    assert await disarm_app_crons_for_execution("probe-app", svc) == 0


@pytest.mark.asyncio
async def test_the_gateway_init_records_the_copy_and_the_dashboard_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.slack.gateway import GatewayOrchestrator

    _write_malformed(tmp_path)
    monkeypatch.setattr("kiro_crew.slack.gateway.data_home", lambda: tmp_path)
    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.dashboard_state = None
    gw._no_crons = True

    await gw._init_cron(arm=False)

    copies = quarantine_copies(tmp_path / "crons.json")
    assert gw._cron_quarantine == copies[0]
    assert copies[0].read_bytes() == _MALFORMED

    gw.dashboard_state = MagicMock()
    gw._announce_cron_quarantine()
    kind, title, body = gw.dashboard_state.notify.call_args.args
    assert kind == "cron"
    assert str(copies[0]) in body
    assert "crons.json" in body and "kirocrew doctor" in body


def test_no_notice_when_nothing_was_moved() -> None:
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.dashboard_state = MagicMock()
    gw._cron_quarantine = None

    gw._announce_cron_quarantine()

    gw.dashboard_state.notify.assert_not_called()


# ── the dashboard startup hook ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_denied_app_over_an_unreadable_store_does_not_abort_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store the quarantine could not move (a read error) must not crash the hook."""
    _write_malformed(tmp_path)
    svc = CronService(base_dir=tmp_path)
    info = {"name": "probe-app", "enabled": True, "manifest": {"name": "probe-app"}}
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(hooks_mod, "_lifecycle_dispatcher", SimpleNamespace())
    monkeypatch.setattr(hooks_mod, "_route_registry", RouteRegistry(web.Application()))
    monkeypatch.setattr(hooks_mod, "list_apps", lambda: [info])
    monkeypatch.setattr(hooks_mod, "_app_hook_root", lambda _name: tmp_path / "apps" / "probe-app")
    monkeypatch.setattr(hooks_mod, "app_execution_denied", lambda *a, **kw: "untrusted")

    await hooks_mod.on_gateway_startup(cron_service=svc)

    assert (tmp_path / "crons.json").read_bytes() == _MALFORMED


@pytest.mark.asyncio
async def test_an_unopenable_store_lock_does_not_abort_the_startup_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disarm takes the store lock first; a lock it cannot open must not crash boot."""
    _write_malformed(tmp_path)
    svc = CronService(base_dir=tmp_path)
    info = {"name": "probe-app", "enabled": True, "manifest": {"name": "probe-app"}}

    def _denied(*_a: object, **_k: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(hooks_mod, "_lifecycle_dispatcher", SimpleNamespace())
    monkeypatch.setattr(hooks_mod, "_route_registry", RouteRegistry(web.Application()))
    monkeypatch.setattr(hooks_mod, "list_apps", lambda: [info])
    monkeypatch.setattr(hooks_mod, "_app_hook_root", lambda _name: tmp_path / "apps" / "probe-app")
    monkeypatch.setattr(hooks_mod, "app_execution_denied", lambda *a, **kw: "untrusted")
    monkeypatch.setattr("kiro_crew.cron_service.store.platform_compat.open_lock_file", _denied)

    await hooks_mod.on_gateway_startup(cron_service=svc)

    assert (tmp_path / "crons.json").read_bytes() == _MALFORMED


# ── kirocrew doctor ──────────────────────────────────────────────────────────


def test_doctor_names_the_copy_and_how_to_restore_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from kiro_crew.doctor_checks import workload

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    moved = quarantine_unreadable_store(_write_malformed(tmp_path))
    assert moved is not None
    issues: list[str] = []

    workload._doctor_cron_health(issues)

    out = capsys.readouterr().out
    assert out.count("Cron Jobs") == 1
    assert repr(str(moved)) in out  # printed inert, so a Windows path is escaped
    assert "kirocrew stop" in out
    assert str(tmp_path / "crons.json") in out
    assert any("set aside" in issue for issue in issues)


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows file names cannot hold control characters"
)
def test_doctor_prints_a_planted_control_sequence_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file name carrying an OSC 52 clipboard write must not reach the terminal raw."""
    from kiro_crew.doctor_checks import workload

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    folder = tmp_path / "cron-history" / "quarantine"
    folder.mkdir(parents=True)
    (folder / "crons.json.corrupt-\x1b]52;c;cmVwbGFjZWQ=\x07").write_bytes(b"{")

    workload._doctor_cron_health([])

    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out
    assert "\\x1b]52" in out


def test_doctor_is_silent_without_a_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from kiro_crew.doctor_checks import workload

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    issues: list[str] = []

    workload._doctor_cron_health(issues)

    assert capsys.readouterr().out == ""
    assert issues == []


def test_the_quarantine_never_deletes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rename is the only file operation: no unlink, no rewrite of the original."""
    calls: list[str] = []
    monkeypatch.setattr(os, "unlink", lambda *a, **k: calls.append("unlink"))
    monkeypatch.setattr(os, "remove", lambda *a, **k: calls.append("remove"))
    store = _write_malformed(tmp_path)

    moved = quarantine_unreadable_store(store)

    assert calls == []
    assert moved is not None and moved.read_bytes() == _MALFORMED
