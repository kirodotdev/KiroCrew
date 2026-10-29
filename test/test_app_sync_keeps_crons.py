"""A code reload of an enabled app keeps the cron jobs its manifest declares.

Sync (and an out-of-process reinstall) changes the app's code, so the hook
reconciler tears the loaded hooks down and enables the app again. A teardown
that removes every job the app owns hands the enable that follows nothing to
update, so it creates fresh jobs: a fresh job has a new id, so its run history
is gone, and it starts in the manifest's ``enabled`` state, so a job the person
switched on (or off) flips back.

``on_app_disable(keep_manifest_crons=True)`` is the reload's teardown: a job the
current manifest still declares keeps its id and enabled state, and the
registration pass after it updates the job in place. A declared job whose
command, script, env or agent sequence changed is recreated, since the scheduler
cannot change those in place. A job absent from the
manifest, and a job the app created at runtime, are removed the way a disable
removes them. A plain disable removes every job the app owns. Every scenario runs
against a real ``CronService`` on an isolated home.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import kiro_crew.apps.execution as execution_mod
from kiro_crew.apps import hooks_integration as hi
from kiro_crew.apps.bridges import _register_crons, register_app_crons_with_service
from kiro_crew.apps.cron_sdk import CronSDK
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.cron import CronService

APP = "sync-app"
KEPT = f"{APP}/digest"
DROPPED = f"{APP}/weekly"
RUNTIME = f"{APP}/made-at-runtime"


def _manifest(*crons: dict) -> AppManifest:
    return AppManifest.from_dict(
        {
            "name": APP,
            "version": "1.0.0",
            "displayName": "Sync App",
            "description": "x",
            "author": "t",
            "permissions": {"cron": True},
            "crons": list(crons),
        }
    )


def _digest(**extra) -> dict:
    return {"name": "digest", "every": 600, "message": "digest the inbox", **extra}


def _weekly() -> dict:
    return {"name": "weekly", "every": 604800, "message": "weekly report"}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """An isolated crew home with one installed app bundle."""
    root = tmp_path / "kirocrew-home"
    bundle = root / "apps" / APP
    bundle.mkdir(parents=True)
    (root / "crons").mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(root))
    (bundle / "app.json").write_text(
        json.dumps(
            {
                "name": APP,
                "version": "1.0.0",
                "displayName": "Sync App",
                "description": "x",
                "author": "t",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.apps.bridges._registration_denied", lambda *a, **k: False)
    monkeypatch.setattr(execution_mod, "third_party_execution_allowed", lambda: True)
    monkeypatch.setattr(hi, "sel", lambda: MagicMock())
    return root


def _dispatcher_stub(svc: CronService, monkeypatch) -> None:
    async def _stop(app_name: str, *, bounded: bool = False) -> bool:
        return True

    monkeypatch.setattr(
        hi,
        "_lifecycle_dispatcher",
        SimpleNamespace(_cron_service=svc, stop_detached_startup_hooks=_stop),
    )


def _jobs(svc: CronService) -> dict[str, tuple[str, bool]]:
    """name -> (id, enabled) for every job the app owns."""
    return {
        j.name: (j.id, j.enabled)
        for j in svc.list_jobs(include_disabled=True)
        if j.created_by == f"app:{APP}"
    }


def _run(home: Path, scenario):
    async def _go():
        svc = CronService(base_dir=home / "crons")
        await svc.start()
        try:
            return await scenario(svc)
        finally:
            await svc.stop()

    return asyncio.run(_go())


async def _install_v1(svc: CronService) -> dict[str, tuple[str, bool]]:
    """v1 declares two crons; the app also creates one at runtime; the person pauses one."""
    _register_crons(APP, _manifest(_digest(), _weekly()))
    await register_app_crons_with_service(APP, svc)
    await CronSDK(APP, svc).add_job_async(RUNTIME, "made by on_startup", every_secs=3600)
    assert await svc.enable_job_async(_jobs(svc)[KEPT][0], False)
    return _jobs(svc)


def test_a_reload_keeps_the_declared_job_with_its_id_and_pause(home, monkeypatch):
    async def scenario(svc):
        _dispatcher_stub(svc, monkeypatch)
        before = await _install_v1(svc)

        # v2 changes the kept cron's message and drops the weekly one.
        _register_crons(APP, _manifest(_digest(message="new words")))
        result = await hi.on_app_disable(
            APP, {"manifest": {}}, run_app_hooks=False, keep_manifest_crons=True
        )
        created = await register_app_crons_with_service(APP, svc)
        live = {j.name: j for j in svc.list_jobs(include_disabled=True)}
        return before, _jobs(svc), result, created, live[KEPT].message

    before, after, result, created, message = _run(home, scenario)

    assert set(before) == {KEPT, DROPPED, RUNTIME}
    assert set(after) == {KEPT}, "the dropped and the runtime job are removed"
    assert after[KEPT][0] == before[KEPT][0], "same id, so the run history stays with it"
    assert after[KEPT][1] is False, "the person's pause survives the reload"
    assert message == "new words", "the manifest change is applied in place"
    assert created == [], "nothing is recreated"
    assert result.get("cron_cleanup") == "removed 2 job(s)", result


def test_a_reload_recreates_a_job_whose_command_changed(home, monkeypatch):
    """The scheduler cannot change a command in place, so the reload recreates
    that one job from the manifest instead of leaving the old command running."""

    def _cmd(command: str) -> dict:
        return {"name": "digest", "every": 600, "command": command}

    async def scenario(svc):
        _dispatcher_stub(svc, monkeypatch)
        _register_crons(APP, _manifest(_cmd("echo one"), _weekly()))
        await register_app_crons_with_service(APP, svc)
        before = _jobs(svc)

        _register_crons(APP, _manifest(_cmd("echo two"), _weekly()))
        await hi.on_app_disable(
            APP, {"manifest": {}}, run_app_hooks=False, keep_manifest_crons=True
        )
        created = await register_app_crons_with_service(APP, svc)
        live = {j.name: j for j in svc.list_jobs(include_disabled=True)}
        return before, _jobs(svc), created, live[KEPT].command

    before, after, created, command = _run(home, scenario)

    assert command == "echo two"
    assert created == [KEPT]
    assert after[KEPT][0] != before[KEPT][0]
    assert after[DROPPED][0] == before[DROPPED][0], "the unchanged job is still kept"


def test_a_reload_recreates_a_job_whose_message_was_cleared(home, monkeypatch):
    """An agent cron may carry no message; the update gate cannot clear one, so
    the reload recreates the job rather than keep running the old prompt."""

    async def scenario(svc):
        _dispatcher_stub(svc, monkeypatch)
        _register_crons(APP, _manifest(_digest(agent="digest-agent")))
        await register_app_crons_with_service(APP, svc)
        before = _jobs(svc)
        _register_crons(APP, _manifest(_digest(agent="digest-agent", message="")))
        await hi.on_app_disable(
            APP, {"manifest": {}}, run_app_hooks=False, keep_manifest_crons=True
        )
        await register_app_crons_with_service(APP, svc)
        live = {j.name: j for j in svc.list_jobs(include_disabled=True)}
        return before, _jobs(svc), live[KEPT].message

    before, after, message = _run(home, scenario)

    assert message == ""
    assert after[KEPT][0] != before[KEPT][0]


def test_a_reload_keeps_a_users_prompt_on_an_always_empty_message(home, monkeypatch):
    """The manifest never declared a message; the person wrote one. Nothing the
    author changed is pending, so the reload keeps the job and the prompt."""

    async def scenario(svc):
        _dispatcher_stub(svc, monkeypatch)
        _register_crons(APP, _manifest(_digest(agent="digest-agent", message="")))
        await register_app_crons_with_service(APP, svc)
        before = _jobs(svc)
        assert await svc.update_job_async(before[KEPT][0], message="my own prompt")
        _register_crons(APP, _manifest(_digest(agent="digest-agent", message="", timeout_secs=900)))
        await hi.on_app_disable(
            APP, {"manifest": {}}, run_app_hooks=False, keep_manifest_crons=True
        )
        await register_app_crons_with_service(APP, svc)
        live = {j.name: j for j in svc.list_jobs(include_disabled=True)}
        return before, _jobs(svc), live[KEPT].message

    before, after, message = _run(home, scenario)

    assert after[KEPT][0] == before[KEPT][0]
    assert message == "my own prompt"


def test_a_reload_judges_jobs_written_by_another_process(home, monkeypatch):
    """The keep decision runs on the store as it is on disk inside the removal
    lock, so a declared job another process registered since this service last
    read the store is kept, and a runtime job sharing its name is removed."""

    async def scenario(svc):
        _dispatcher_stub(svc, monkeypatch)
        svc.list_jobs(include_disabled=True)  # warm this service's cache first
        other = CronService(base_dir=home / "crons")
        _register_crons(APP, _manifest(_digest()))
        await register_app_crons_with_service(APP, other)
        await CronSDK(APP, other).add_job_async(KEPT, "runtime twin", every_secs=3600)
        written = {
            j.message: j.id
            for j in other.list_jobs(include_disabled=True)
            if j.created_by == f"app:{APP}"
        }
        await hi.on_app_disable(
            APP, {"manifest": {}}, run_app_hooks=False, keep_manifest_crons=True
        )
        fresh = CronService(base_dir=home / "crons")
        return written, [
            (j.message, j.id)
            for j in fresh.list_jobs(include_disabled=True)
            if j.created_by == f"app:{APP}"
        ]

    written, left = _run(home, scenario)

    assert left == [("digest the inbox", written["digest the inbox"])]


def test_a_busy_store_during_a_reload_is_logged_and_raised(home, monkeypatch):
    """The reload cleanup leaves the same trace a disable's cleanup does."""
    from kiro_crew.apps import bridges
    from kiro_crew.cron import CronStoreBusy

    audit = MagicMock()
    monkeypatch.setattr(bridges, "sel", lambda: audit)

    async def busy(*args, **kwargs):
        raise CronStoreBusy("contended")

    async def scenario(svc):
        _register_crons(APP, _manifest(_digest()))
        monkeypatch.setattr(svc, "remove_jobs_by_owner", busy)
        with pytest.raises(CronStoreBusy):
            await bridges.retire_app_crons_for_reload(APP, svc)

    _run(home, scenario)

    outcomes = [c.kwargs.get("outcome") for c in audit.log_api_access.call_args_list]
    assert outcomes == ["failed"]


def test_a_plain_disable_still_removes_every_app_job(home, monkeypatch):
    async def scenario(svc):
        _dispatcher_stub(svc, monkeypatch)
        await _install_v1(svc)
        result = await hi.on_app_disable(APP, {"manifest": {}}, run_app_hooks=False)
        return _jobs(svc), result

    after, result = _run(home, scenario)

    assert after == {}
    assert result.get("cron_cleanup") == "removed 3 job(s)", result


def test_a_reload_leaves_other_owners_jobs_alone(home, monkeypatch):
    async def scenario(svc):
        _dispatcher_stub(svc, monkeypatch)
        await _install_v1(svc)
        # Same name as the kept job, other owner: the keep set is per owner.
        await svc.add_job_async(KEPT, "someone else's", every_secs=3600, created_by="app:other")
        await svc.add_job_async("mine", "tick", every_secs=3600, created_by="U123456")
        await hi.on_app_disable(
            APP, {"manifest": {}}, run_app_hooks=False, keep_manifest_crons=True
        )
        return sorted(
            (j.created_by, j.name)
            for j in svc.list_jobs(include_disabled=True)
            if j.created_by != f"app:{APP}"
        )

    others = _run(home, scenario)

    assert others == [("U123456", "mine"), ("app:other", KEPT)]


# --- the hook reconciler drives the reload: enable failure after retention ---


def _app_info(version: str, *, enabled: bool = True) -> dict:
    return {
        "name": APP,
        "enabled": enabled,
        "version": version,
        "manifest": {"backend": {"hooks": {"on_startup": "backend.hooks:on_startup"}}},
    }


def _reconciler_stub(svc: CronService, monkeypatch, enable_outcomes: list[str]) -> dict:
    """Run the real reconciler against *svc*, with the app's on-disk state staged.

    Each ``on_app_enable`` call registers the manifest crons for real (the real
    enable does that before it wires the hooks) and then takes the next outcome
    from *enable_outcomes*: ``"raise"`` fails the reimport, ``"ok"`` records the
    hooks as loaded.
    """
    from kiro_crew.apps import hook_reconcile as hr

    state: dict = {"current": None}
    monkeypatch.setattr(hi, "hook_signature", lambda info: ("sig", info.get("version")))
    monkeypatch.setattr(hr, "get_app", lambda name: state["current"])
    monkeypatch.setattr(hr, "hook_enable_denied", lambda name: "")
    monkeypatch.setattr(hr, "_cron_service", svc)

    async def disable(name, app_info, **kwargs):
        # The app's own shutdown hook is out of scope here; the cron teardown is real.
        kwargs["run_app_hooks"] = False
        return await hi.on_app_disable(name, app_info, **kwargs)

    async def enable(name, app_info, **kwargs):
        await register_app_crons_with_service(name, svc)
        if enable_outcomes.pop(0) == "raise":
            raise RuntimeError("route import failed")
        await hi.record_loaded_hook_signature(name, app_info)

    monkeypatch.setattr(hr, "on_app_disable", disable)
    monkeypatch.setattr(hr, "on_app_enable", enable)
    return state


def test_a_reconciler_reload_that_succeeds_keeps_the_job_and_its_pause(home, monkeypatch):
    from kiro_crew.apps import hook_reconcile as hr

    async def scenario(svc):
        _dispatcher_stub(svc, monkeypatch)
        state = _reconciler_stub(svc, monkeypatch, ["ok"])
        before = await _install_v1(svc)
        await hi.record_loaded_hook_signature(APP, _app_info("1.0.0"))
        try:
            _register_crons(APP, _manifest(_digest()))
            state["current"] = _app_info("2.0.0")
            await hr._reconcile_app(APP, state["current"])
            return before, _jobs(svc)
        finally:
            hi.clear_loaded_hook_signature(APP)

    before, after = _run(home, scenario)

    assert after == {KEPT: before[KEPT]}, "same id and the person's pause"


def test_a_reconciler_reload_whose_enable_fails_rolls_back_retries_and_tears_down(
    home, monkeypatch
):
    """The reload keeps the manifest crons, then the enable raises. The kept job
    must not keep running for an app with no healthy hook wiring, so the
    reconciler removes it. The next tick retries the load and registers the
    manifest crons again; a later disable removes them like any other."""
    from kiro_crew.apps import hook_reconcile as hr

    async def scenario(svc):
        _dispatcher_stub(svc, monkeypatch)
        state = _reconciler_stub(svc, monkeypatch, ["raise", "ok"])
        before = await _install_v1(svc)
        await hi.record_loaded_hook_signature(APP, _app_info("1.0.0"))
        try:
            _register_crons(APP, _manifest(_digest()))
            state["current"] = _app_info("2.0.0")

            await hr._reconcile_app(APP, state["current"])
            after_failure = _jobs(svc)
            armed = hi.loaded_hook_signature(APP) is None

            await hr._reconcile_app(APP, state["current"])
            after_retry = _jobs(svc)

            state["current"] = _app_info("2.0.0", enabled=False)
            await hr._reconcile_app(APP, state["current"])
            return before, after_failure, armed, after_retry, _jobs(svc)
        finally:
            hi.clear_loaded_hook_signature(APP)

    before, after_failure, armed, after_retry, after_disable = _run(home, scenario)

    assert after_failure == {}, "no app job runs while its hooks are down"
    assert armed, "the load is retried next tick"
    assert set(after_retry) == {KEPT}, "the retry registers the manifest crons again"
    assert after_retry[KEPT][0] != before[KEPT][0], "recreated, not the kept job"
    assert after_disable == {}, "a disable after the retry removes them"


def test_a_disable_that_removes_the_jobs_settles_an_owed_rollback(home, monkeypatch):
    """A reload failed and its rollback hit a busy store, so the reconciler still
    owes a removal. Before its next tick the person disables the app (which
    removes every job) and re-enables it (which registers fresh ones and records
    the hooks as loaded). The owed removal was settled by the disable, so the
    reconciler must not delete the jobs the successful re-enable just made."""
    from kiro_crew.apps import hook_reconcile as hr

    async def scenario(svc):
        _dispatcher_stub(svc, monkeypatch)
        _reconciler_stub(svc, monkeypatch, [])
        await _install_v1(svc)
        hr.pending_reload_cron_rollback.add(APP)
        try:
            # Dashboard disable: the ordinary teardown removes every app job.
            await hi.on_app_disable(APP, {"manifest": {}}, run_app_hooks=False)
            settled = APP not in hr.pending_reload_cron_rollback

            # Dashboard re-enable: fresh jobs, hooks recorded as loaded.
            _register_crons(APP, _manifest(_digest()))
            await register_app_crons_with_service(APP, svc)
            current = _app_info("2.0.0")
            await hi.record_loaded_hook_signature(APP, current)
            fresh = _jobs(svc)

            monkeypatch.setattr(hr, "get_app", lambda name: current)
            await hr._reconcile_app(APP, current)
            return settled, fresh, _jobs(svc)
        finally:
            hr.pending_reload_cron_rollback.discard(APP)
            hi.clear_loaded_hook_signature(APP)

    settled, fresh, after_tick = _run(home, scenario)

    assert settled, "the disable's removal settles the owed rollback"
    assert set(fresh) == {KEPT}
    assert after_tick == fresh, "the reconciler leaves the re-enabled app's jobs alone"


def test_a_busy_disable_leaves_the_rollback_owed(home, monkeypatch):
    """Only a removal that completes settles it: a disable whose cron cleanup
    hits a busy store leaves the jobs on disk, so the reconciler still owes it."""
    from kiro_crew.apps import hook_reconcile as hr
    from kiro_crew.apps.cron_sdk import CronSDK as _SDK
    from kiro_crew.cron import CronStoreBusy

    async def scenario(svc):
        _dispatcher_stub(svc, monkeypatch)
        await _install_v1(svc)
        hr.pending_reload_cron_rollback.add(APP)

        async def busy(self):
            raise CronStoreBusy("held")

        monkeypatch.setattr(_SDK, "remove_all_async", busy)
        try:
            result = await hi.on_app_disable(APP, {"manifest": {}}, run_app_hooks=False)
            return APP in hr.pending_reload_cron_rollback, result
        finally:
            hr.pending_reload_cron_rollback.discard(APP)

    owed, result = _run(home, scenario)

    assert owed
    assert str(result.get("cron_cleanup", "")).startswith("failed: cron store busy"), result


def test_an_enable_after_a_busy_disable_settles_the_owed_rollback(home, monkeypatch):
    """The rollback is owed and the disable's cleanup hit a busy store, so the
    mark survives the disable. The store frees up and the person enables the app
    before the next tick. The enable must settle the owed removal itself, or the
    tick deletes the jobs this enable kept and, seeing the hooks loaded, never
    registers them again."""
    from kiro_crew.apps import hook_reconcile as hr
    from kiro_crew.apps.cron_sdk import CronSDK as _SDK
    from kiro_crew.cron import CronStoreBusy

    async def scenario(svc):
        _dispatcher_stub(svc, monkeypatch)
        _reconciler_stub(svc, monkeypatch, [])
        await _install_v1(svc)
        hr.pending_reload_cron_rollback.add(APP)
        real_remove = _SDK.remove_all_async

        async def busy(self):
            raise CronStoreBusy("held")

        try:
            monkeypatch.setattr(_SDK, "remove_all_async", busy)
            await hi.on_app_disable(APP, {"manifest": {}}, run_app_hooks=False)
            monkeypatch.setattr(_SDK, "remove_all_async", real_remove)

            # Dashboard enable through the real shared enable path; the hooks
            # then come up and are recorded as loaded.
            _register_crons(APP, _manifest(_digest()))
            await hi.on_app_enable(APP, {"name": APP, "manifest": {}}, cron_service=svc)
            current = _app_info("2.0.0")
            await hi.record_loaded_hook_signature(APP, current)
            settled = APP not in hr.pending_reload_cron_rollback
            enabled = _jobs(svc)

            monkeypatch.setattr(hr, "get_app", lambda name: current)
            await hr._reconcile_app(APP, current)
            return settled, enabled, _jobs(svc)
        finally:
            hr.pending_reload_cron_rollback.discard(APP)
            hi.clear_loaded_hook_signature(APP)

    settled, enabled, after_tick = _run(home, scenario)

    assert settled, "the enable settles the owed removal"
    assert set(enabled) == {KEPT}, "registered fresh from the manifest"
    assert after_tick == enabled, "the next tick leaves the enabled app's jobs alone"


def test_an_enable_whose_owed_rollback_is_still_busy_registers_nothing(home, monkeypatch):
    """The owed removal still cannot complete at enable time, so the enable stops
    before registering on top of the kept jobs and leaves the mark for the
    reconciler, which retries the whole enable."""
    from kiro_crew.apps import hook_reconcile as hr
    from kiro_crew.apps.cron_sdk import CronSDK as _SDK
    from kiro_crew.cron import CronStoreBusy

    async def scenario(svc):
        _dispatcher_stub(svc, monkeypatch)
        before = await _install_v1(svc)
        hr.pending_reload_cron_rollback.add(APP)

        async def busy(self):
            raise CronStoreBusy("held")

        monkeypatch.setattr(_SDK, "remove_all_async", busy)
        calls: list[str] = []

        async def register(name, service):
            calls.append(name)
            return []

        monkeypatch.setattr(hi, "register_app_crons_with_service", register)
        try:
            result = await hi.on_app_enable(APP, {"name": APP, "manifest": {}}, cron_service=svc)
            return before, result, calls, APP in hr.pending_reload_cron_rollback, _jobs(svc)
        finally:
            hr.pending_reload_cron_rollback.discard(APP)

    before, result, calls, owed, after = _run(home, scenario)

    assert result.get("cron_registration", "").startswith("deferred"), result
    assert calls == [], "nothing registers on top of the jobs still owed for removal"
    assert owed, "the reconciler still owes the removal"
    assert after == before
