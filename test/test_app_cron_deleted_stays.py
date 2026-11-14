"""A manifest app cron job the owner deleted stays deleted across registrations.

Registration (every gateway start, app update and enable) creates each manifest job
missing from the store. A delete the owner asked for (Schedule page, CLI, chat
command) is recorded in ``member-records/app-crons-deleted/<app>.json``, and
registration skips the recorded names. The owner's disable of the app and an
uninstall drop the record, so the next enable registers every job again.

Everything runs on a real ``CronService`` on an isolated home.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
from app_manager_helpers import app_home_fixture  # noqa: F401 - fixture
from app_manager_helpers import _make_app_source

import kiro_crew.apps.execution as execution_mod
from kiro_crew import member_records
from kiro_crew.apps import protected_app_state
from kiro_crew.apps.bridges import _register_crons, register_app_crons_with_service
from kiro_crew.apps.cron_sdk import CronSDK
from kiro_crew.apps.manager import disable_app, enable_app, install_app, uninstall_app
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.cron import CronService

APP = "deleted-app"
KEEP, DROP = "keep", "drop"


@pytest.fixture(autouse=True)
def _outside_a_sandbox(_floor_monkeypatch):
    # The gateway runs unsandboxed; a test host may itself be a sandbox.
    _floor_monkeypatch.delenv(member_records._IN_SANDBOX_MARKER, raising=False)


def _manifest() -> AppManifest:
    return AppManifest.from_dict(
        {
            "name": APP,
            "version": "1.0.0",
            "displayName": "Deleted App",
            "description": "x",
            "author": "t",
            "permissions": {"cron": True},
            "crons": [
                {"name": KEEP, "every": 600, "message": "keep me"},
                {"name": DROP, "every": 600, "message": "drop me"},
            ],
        }
    )


@pytest.fixture()
def home(tmp_path, monkeypatch):
    root = tmp_path / "kirocrew-home"
    (root / "apps" / APP).mkdir(parents=True)
    (root / "crons").mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(root))
    monkeypatch.setattr("kiro_crew.apps.bridges._registration_denied", lambda *a, **k: False)
    monkeypatch.setattr(execution_mod, "third_party_execution_allowed", lambda: True)
    _register_crons(APP, _manifest())
    return root


def _run(home, scenario):
    async def _go():
        svc = CronService(base_dir=home / "crons")
        await svc.start()
        try:
            return await scenario(svc)
        finally:
            await svc.stop()

    return asyncio.run(_go())


def _names(svc: CronService) -> set[str]:
    return {j.name for j in svc.list_jobs(include_disabled=True)}


def _id_of(svc: CronService, short: str) -> str:
    return next(j.id for j in svc.list_jobs(include_disabled=True) if j.name == f"{APP}/{short}")


def _delete_through(path: str):
    """Each service method a person's delete reaches."""

    async def delete(svc: CronService, job_id: str) -> None:
        if path == "single_async":
            assert await svc.remove_job_async(job_id, actor="dashboard", source="api_cron_delete")
        elif path == "single_sync":
            assert await asyncio.to_thread(svc.remove_job, job_id, actor="cli", source="cli")
        elif path == "batch_async":
            removed, _ = await svc.remove_jobs([job_id], actor="dashboard", source="batch")
            assert removed == [job_id]
        else:
            removed, _ = await asyncio.to_thread(
                svc.remove_jobs_sync, [job_id], actor="cli", source="cli"
            )
            assert removed == [job_id]

    return delete


@pytest.mark.parametrize("path", ["single_async", "single_sync", "batch_async", "batch_sync"])
def test_a_deleted_manifest_job_is_not_registered_again(home, path):
    async def scenario(svc):
        await register_app_crons_with_service(APP, svc)
        await _delete_through(path)(svc, _id_of(svc, DROP))
        created = await register_app_crons_with_service(APP, svc)
        return created, _names(svc)

    created, names = _run(home, scenario)

    assert created == []
    assert names == {f"{APP}/{KEEP}"}
    assert protected_app_state.deleted_app_crons(APP) == {f"{APP}/{DROP}"}


def test_the_other_jobs_still_come_back_on_a_fresh_store(home):
    async def scenario(svc):
        await register_app_crons_with_service(APP, svc)
        await svc.remove_job_async(_id_of(svc, DROP), actor="dashboard", source="api")
        await svc.remove_jobs_by_owner(f"app:{APP}")  # the store lost every app job
        created = await register_app_crons_with_service(APP, svc)
        return created, _names(svc)

    created, names = _run(home, scenario)

    assert created == [f"{APP}/{KEEP}"]
    assert names == {f"{APP}/{KEEP}"}


def test_a_removal_the_app_asked_for_is_not_recorded(home):
    async def scenario(svc):
        await register_app_crons_with_service(APP, svc)
        assert await CronSDK(APP, svc).remove_job_async(_id_of(svc, DROP))
        created = await register_app_crons_with_service(APP, svc)
        return created

    assert _run(home, scenario) == [f"{APP}/{DROP}"]
    assert protected_app_state.deleted_app_crons(APP) == frozenset()


def test_a_cleared_record_registers_the_deleted_job_again(home):
    async def scenario(svc):
        await register_app_crons_with_service(APP, svc)
        await svc.remove_job_async(_id_of(svc, DROP), actor="dashboard", source="api")
        protected_app_state.forget_deleted_app_crons(APP)
        return await register_app_crons_with_service(APP, svc)

    assert _run(home, scenario) == [f"{APP}/{DROP}"]


@pytest.mark.parametrize("path", ["single_async", "single_sync", "batch_async", "batch_sync"])
def test_a_record_that_cannot_be_written_still_deletes_the_job(home, monkeypatch, path):
    # The record is best effort: a sandboxed CLI cannot write member-records/, and
    # a delete must not fail there. The fault direction is the job coming back on
    # the next start, never a job the person still wanted being kept from them.
    def refuse(app, name):
        raise OSError("read-only record store")

    async def scenario(svc):
        await register_app_crons_with_service(APP, svc)
        monkeypatch.setattr(protected_app_state, "remember_deleted_app_cron", refuse)
        await _delete_through(path)(svc, _id_of(svc, DROP))
        return _names(svc)

    assert _run(home, scenario) == {f"{APP}/{KEEP}"}


def test_an_app_token_delete_reaches_the_service_as_the_app():
    from kiro_crew.dashboard.handlers.cron_jobs import _removal_actor

    assert _removal_actor({"app": APP}) == f"app:{APP}"
    assert _removal_actor({}) == "dashboard"


def test_an_unreadable_record_reads_as_nothing_deleted(home):
    async def scenario(svc):
        await register_app_crons_with_service(APP, svc)
        await svc.remove_job_async(_id_of(svc, DROP), actor="dashboard", source="api")
        path = member_records.records_dir("app-crons-deleted") / f"{APP}.json"
        path.write_text(json.dumps({"deleted": "not a list"}), encoding="utf-8")
        return await register_app_crons_with_service(APP, svc)

    assert _run(home, scenario) == [f"{APP}/{DROP}"]


@pytest.mark.parametrize("clear", ["disable", "uninstall"])
def test_disable_and_uninstall_drop_the_record(tmp_path, app_home, clear):
    assert install_app(_make_app_source(tmp_path)).ok
    assert enable_app("test-app").ok
    protected_app_state.remember_deleted_app_cron("test-app", "test-app/nightly")

    assert (disable_app if clear == "disable" else uninstall_app)("test-app").ok

    assert protected_app_state.deleted_app_crons("test-app") == frozenset()


def _delete_from_a_stale_cache(path: str):
    """Each person's delete, sent by a service that never read the job."""

    def delete(svc: CronService, job_id: str) -> None:
        if path == "single_sync":
            assert svc.remove_job(job_id, actor="cli", source="cli")
        elif path == "single_async":
            assert asyncio.run(svc.remove_job_async(job_id, actor="dashboard", source="api"))
        elif path == "if_async":
            outcome = asyncio.run(
                svc.remove_job_if_async(
                    job_id,
                    expected_revision="r",
                    revision_of=lambda _job: "r",
                    actor="dashboard",
                    source="api",
                )
            )
            assert outcome == "removed"
        elif path == "batch_async":
            removed, _ = asyncio.run(svc.remove_jobs([job_id], actor="dashboard", source="api"))
            assert removed == [job_id]
        else:
            removed, _ = svc.remove_jobs_sync([job_id], actor="cli", source="cli")
            assert removed == [job_id]

    return delete


@pytest.mark.parametrize(
    "path", ["single_sync", "single_async", "if_async", "batch_async", "batch_sync"]
)
def test_a_job_another_process_registered_is_recorded_when_deleted(home, path):
    # The deleting service read the store before another process (a CLI app
    # enable while the gateway runs) registered the job, so its cache lacks it.
    # The removal reloads the store under its lock; the record must use that row.
    deleter = CronService(base_dir=home / "crons")
    asyncio.run(register_app_crons_with_service(APP, CronService(base_dir=home / "crons")))
    job_id = next(
        j.id
        for j in CronService(base_dir=home / "crons").list_jobs(include_disabled=True)
        if j.name == f"{APP}/{DROP}"
    )
    assert deleter.get_job(job_id) is None

    _delete_from_a_stale_cache(path)(deleter, job_id)

    assert protected_app_state.deleted_app_crons(APP) == {f"{APP}/{DROP}"}


def test_dropping_the_record_holds_the_lock_a_record_write_holds(home, monkeypatch):
    # A record write reads the names and writes them back under the per-app lock.
    # The drop must take the same lock, or a write that read before the drop
    # puts the dropped names back after it.
    protected_app_state.remember_deleted_app_cron(APP, f"{APP}/{DROP}")
    record = member_records.records_dir("app-crons-deleted") / f"{APP}.json"
    real_lock = protected_app_state._exclusive_lock
    seen: list[tuple[str, bool, bool]] = []

    @contextlib.contextmanager
    def recording_lock(path):
        with real_lock(path):
            before = record.exists()
            yield
            seen.append((path.name, before, record.exists()))

    monkeypatch.setattr(protected_app_state, "_exclusive_lock", recording_lock)
    protected_app_state.forget_deleted_app_crons(APP)

    assert not record.exists()
    # The record existed when the lock was taken and was gone before it was let go.
    assert seen == [(f"{APP}.lock", True, False)]
