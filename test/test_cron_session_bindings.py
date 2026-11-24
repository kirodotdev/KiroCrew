"""Gateway-only cron session bindings follow every CronService ownership writer."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from kiro_crew import sandbox, sandbox_leaves, sandbox_precreate
from kiro_crew.cron import CronService
from kiro_crew.cron_service import session_bindings
from kiro_crew.cron_service.model import CronJob
from kiro_crew.cron_service.service import job_ownership, job_removal, job_writes
from kiro_crew.security import paths_lists


@pytest.fixture(autouse=True)
def binding_home(_floor_monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    _floor_monkeypatch.setattr(session_bindings, "data_home", lambda: tmp_path)
    return tmp_path


def _set_seatbelt_verdict(monkeypatch: pytest.MonkeyPatch, verdict: bool | None) -> None:
    monkeypatch.setattr(sandbox, "_macos_sandbox_state", lambda: verdict)


@pytest.fixture(autouse=True)
def unconfined_process(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the gateway's unconfined view, so a sandboxed CI host cannot relax a check."""
    _set_seatbelt_verdict(_floor_monkeypatch, False)


def test_a_binding_uses_a_hashed_filename_and_validated_payload(binding_home: Path) -> None:
    job_id = "nightly-dispatcher"
    session_key = "dashboard:chat-7"

    session_bindings.record_session_binding(job_id, session_key)

    digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
    path = binding_home / session_bindings.SESSION_BINDINGS_DIR_NAME / f"{digest}.json"
    assert session_bindings.session_binding_path(job_id) == path
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "job_id": job_id,
        "session_key": session_key,
    }
    assert session_bindings.read_session_binding(job_id) == session_key


@pytest.mark.parametrize(
    "body",
    [
        "{not json",
        "[]",
        '{"version":2,"job_id":"job","session_key":"dashboard:x"}',
        '{"version":1,"job_id":"other","session_key":"dashboard:x"}',
        '{"version":1,"job_id":"job","session_key":""}',
        '{"version":1,"job_id":"job","session_key":7}',
    ],
)
def test_a_malformed_binding_refuses(body: str) -> None:
    path = session_bindings.session_binding_path("job")
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")

    assert session_bindings.read_session_binding("job") == ""


def test_an_unreadable_binding_refuses() -> None:
    path = session_bindings.session_binding_path("job")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe")

    assert session_bindings.read_session_binding("job") == ""


def _rewrite_stored_session_key(store_dir: Path, job_id: str, session_key: str) -> None:
    path = store_dir / "crons.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    row = next(job for job in document["jobs"] if job["id"] == job_id)
    row["session_key"] = session_key
    path.write_text(json.dumps(document), encoding="utf-8")


def test_add_and_adopt_record_the_persisted_binding(tmp_path: Path) -> None:
    service = CronService(base_dir=tmp_path / "store")
    job = service.add_job("nightly", "run", every_secs=3600, session_key="dashboard:created-here")
    assert session_bindings.read_session_binding(job.id) == "dashboard:created-here"

    assert service.adopt_job(job.id, "dashboard:adopted-here")
    assert session_bindings.read_session_binding(job.id) == "dashboard:adopted-here"

    assert service.adopt_job(job.id, "")
    assert session_bindings.read_session_binding(job.id) == ""


def test_adopt_release_revokes_a_binding_when_the_store_owner_was_blanked(
    tmp_path: Path,
) -> None:
    store_dir = tmp_path / "store"
    service = CronService(base_dir=store_dir)
    owner = "dashboard:old-owner"
    job = service.add_job("nightly", "run", every_secs=3600, session_key=owner)
    _rewrite_stored_session_key(store_dir, job.id, "")
    service._sync()

    assert service.get_job(job.id).session_key == ""
    assert service.adopt_job(job.id, "")

    assert session_bindings.read_session_binding(job.id) == ""


def test_adopt_rebind_revokes_the_record_when_the_store_already_has_the_new_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_dir = tmp_path / "store"
    service = CronService(base_dir=store_dir)
    old_owner = "dashboard:old-owner"
    new_owner = "dashboard:new-owner"
    job = service.add_job("nightly", "run", every_secs=3600, session_key=old_owner)
    _rewrite_stored_session_key(store_dir, job.id, new_owner)
    service._sync()
    real_record = session_bindings.record_session_binding

    def record_after_revocation(job_id: str, session_key: str) -> None:
        assert session_bindings.read_session_binding(job_id) == ""
        real_record(job_id, session_key)

    monkeypatch.setattr(job_ownership, "record_session_binding", record_after_revocation)

    assert service.adopt_job(job.id, new_owner)

    assert session_bindings.read_session_binding(job.id) == new_owner


def test_session_delete_revokes_a_binding_when_the_store_owner_was_blanked(
    tmp_path: Path,
) -> None:
    store_dir = tmp_path / "store"
    service = CronService(base_dir=store_dir)
    owner = "dashboard:retired-owner"
    job = service.add_job("nightly", "run", every_secs=3600, session_key=owner)
    _rewrite_stored_session_key(store_dir, job.id, "")
    service._sync()

    assert asyncio.run(service.release_jobs_owned_by({owner})) == []

    assert service.get_job(job.id).session_key == ""
    assert session_bindings.read_session_binding(job.id) == ""


def test_retired_stateless_run_keeps_a_live_run_child_binding(tmp_path: Path) -> None:
    service = CronService(base_dir=tmp_path / "store")
    parent = service.add_job("parent", "run", every_secs=3600, persistent_session=False)
    old_owner = f"cron:{parent.id}:run-old"
    live_owner = f"cron:{parent.id}:run-live"
    old_child = service.add_job("old child", "run", every_secs=3600, session_key=old_owner)
    live_child = service.add_job("live child", "run", every_secs=3600, session_key=live_owner)
    service.register_active_session_key(parent.id, live_owner)

    released = asyncio.run(service.release_jobs_owned_by({old_owner}))

    assert released == [old_child.id]
    assert service.get_job(old_child.id).session_key == ""
    assert session_bindings.read_session_binding(old_child.id) == ""
    assert service.get_job(live_child.id).session_key == live_owner
    assert session_bindings.read_session_binding(live_child.id) == live_owner


def test_a_recurring_bound_job_keeps_its_binding_through_a_run_merge(tmp_path: Path) -> None:
    service = CronService(base_dir=tmp_path / "store")
    job = service.add_job("recurring", "run", every_secs=3600, session_key="dashboard:chat-7")
    job.last_status = "ok"
    job.last_run_ts = 100.0

    service._merge_job_result(job)

    assert service.get_job(job.id) is not None
    assert session_bindings.read_session_binding(job.id) == "dashboard:chat-7"


def test_listing_bindings_skips_malformed_files(binding_home: Path) -> None:
    session_bindings.record_session_binding("valid-job", "dashboard:valid-owner")
    directory = binding_home / session_bindings.SESSION_BINDINGS_DIR_NAME
    (directory / "invalid-json.json").write_text("{not json", encoding="utf-8")
    (directory / "wrong-name.json").write_text(
        json.dumps(
            {
                "version": 1,
                "job_id": "forged-job",
                "session_key": "dashboard:forged-owner",
            }
        ),
        encoding="utf-8",
    )

    assert session_bindings.list_session_bindings() == [("valid-job", "dashboard:valid-owner")]


def _deny_binding_directory_enumeration(
    monkeypatch: pytest.MonkeyPatch, binding_home: Path
) -> None:
    directory = binding_home / session_bindings.SESSION_BINDINGS_DIR_NAME
    original_iterdir = Path.iterdir

    def fail_binding_directory(path: Path) -> Iterator[Path]:
        if path == directory:
            raise PermissionError(errno.EACCES, "binding directory denied")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_binding_directory)


def test_listing_bindings_distinguishes_a_missing_directory_from_an_io_failure(
    monkeypatch: pytest.MonkeyPatch, binding_home: Path
) -> None:
    assert session_bindings.list_session_bindings() == []

    (binding_home / session_bindings.SESSION_BINDINGS_DIR_NAME).mkdir()
    _deny_binding_directory_enumeration(monkeypatch, binding_home)

    with pytest.raises(PermissionError, match="binding directory denied"):
        session_bindings.list_session_bindings()


def test_listing_bindings_raises_on_record_io_but_admission_stays_tolerant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = "nightly"
    session_bindings.record_session_binding(job_id, "dashboard:owner")
    record_path = session_bindings.session_binding_path(job_id)
    original_read_text = Path.read_text

    def fail_binding_record(
        path: Path, *, encoding: str | None = None, errors: str | None = None
    ) -> str:
        if path == record_path:
            raise PermissionError("binding record denied")
        return original_read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", fail_binding_record)

    assert session_bindings.read_session_binding(job_id) == ""
    with pytest.raises(PermissionError, match="binding record denied"):
        session_bindings.list_session_bindings()


def _deny_binding_record_unlink(monkeypatch: pytest.MonkeyPatch, binding_home: Path) -> None:
    directory = binding_home / session_bindings.SESSION_BINDINGS_DIR_NAME
    original_unlink = Path.unlink

    def fail_binding_unlink(path: Path, missing_ok: bool = False) -> None:
        if path.parent == directory:
            raise PermissionError("binding unlink denied")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_binding_unlink)


def test_a_seatbelt_confined_shell_reads_a_denied_binding_directory_as_empty(
    monkeypatch: pytest.MonkeyPatch, binding_home: Path
) -> None:
    session_bindings.record_session_binding("nightly", "dashboard:owner")
    _deny_binding_directory_enumeration(monkeypatch, binding_home)
    _set_seatbelt_verdict(monkeypatch, True)

    assert session_bindings.list_session_bindings() == []


@pytest.mark.parametrize("verdict", [False, None], ids=["kernel says no", "unanswerable"])
def test_a_sandbox_marker_without_a_seatbelt_verdict_keeps_the_listing_strict(
    monkeypatch: pytest.MonkeyPatch, binding_home: Path, verdict: bool | None
) -> None:
    (binding_home / session_bindings.SESSION_BINDINGS_DIR_NAME).mkdir()
    _deny_binding_directory_enumeration(monkeypatch, binding_home)
    monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
    _set_seatbelt_verdict(monkeypatch, verdict)

    with pytest.raises(PermissionError, match="binding directory denied"):
        session_bindings.list_session_bindings()


def test_a_confined_shell_skips_a_denied_binding_record(monkeypatch: pytest.MonkeyPatch) -> None:
    session_bindings.record_session_binding("denied-job", "dashboard:denied-owner")
    session_bindings.record_session_binding("readable-job", "dashboard:readable-owner")
    denied_path = session_bindings.session_binding_path("denied-job")
    original_read_text = Path.read_text

    def fail_binding_record(
        path: Path, *, encoding: str | None = None, errors: str | None = None
    ) -> str:
        if path == denied_path:
            raise PermissionError("binding record denied")
        return original_read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", fail_binding_record)
    _set_seatbelt_verdict(monkeypatch, True)

    assert session_bindings.list_session_bindings() == [
        ("readable-job", "dashboard:readable-owner")
    ]


def test_a_denied_revoking_unlink_raises_unless_the_shell_is_confined(
    monkeypatch: pytest.MonkeyPatch, binding_home: Path
) -> None:
    session_bindings.record_session_binding("nightly", "dashboard:owner")
    _deny_binding_record_unlink(monkeypatch, binding_home)

    with pytest.raises(PermissionError, match="binding unlink denied"):
        session_bindings.revoke_session_bindings(("nightly",))

    _set_seatbelt_verdict(monkeypatch, True)
    session_bindings.revoke_session_bindings(("nightly",))

    assert session_bindings.read_session_binding("nightly") == "dashboard:owner"


def test_a_dead_parent_release_clears_a_preexisting_binding(tmp_path: Path) -> None:
    service = CronService(base_dir=tmp_path / "store")
    job = service._build_job("orphan", "run", every_secs=3600, session_key="cron:missing-parent")
    session_bindings.record_session_binding(job.id, job.session_key)

    service._persist_add_locked(job)

    assert job.session_key == ""
    assert session_bindings.read_session_binding(job.id) == ""


def test_batch_release_clears_the_binding(tmp_path: Path) -> None:
    service = CronService(base_dir=tmp_path / "store")
    job = service.add_job("nightly", "run", every_secs=3600, session_key="dashboard:retired-owner")

    released = asyncio.run(service.release_jobs_owned_by({"dashboard:retired-owner"}))

    assert released == [job.id]
    assert service.get_job(job.id).session_key == ""
    assert session_bindings.read_session_binding(job.id) == ""


def test_removal_clears_the_removed_job_and_released_child_bindings(tmp_path: Path) -> None:
    service = CronService(base_dir=tmp_path / "store")
    parent = service.add_job("parent", "run", every_secs=3600, session_key="dashboard:parent-owner")
    child = service.add_job("child", "run", every_secs=3600, session_key=f"cron:{parent.id}")

    assert service.remove_job(parent.id, actor="test", source="test")

    assert session_bindings.read_session_binding(parent.id) == ""
    assert session_bindings.read_session_binding(child.id) == ""
    assert service.get_job(child.id).session_key == ""


def test_binding_write_failure_does_not_fail_the_cron_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _fail(*_args, **_kwargs) -> None:
        raise OSError("read-only binding directory")

    monkeypatch.setattr(session_bindings, "atomic_write", _fail)
    service = CronService(base_dir=tmp_path / "store")

    job = service.add_job("nightly", "run", every_secs=3600, session_key="dashboard:x")

    assert job.session_key == "dashboard:x"
    assert service.get_job(job.id) is job
    assert session_bindings.read_session_binding(job.id) == ""


def test_strict_binding_revocation_raises_on_a_non_record_path(tmp_path: Path) -> None:
    service = CronService(base_dir=tmp_path / "store")
    job = service.add_job("nightly", "run", every_secs=3600, session_key="dashboard:x")
    path = session_bindings.session_binding_path(job.id)
    path.unlink()
    path.mkdir()

    with pytest.raises(OSError):
        session_bindings.revoke_session_bindings((job.id,))

    assert path.is_dir()


def test_the_binding_directory_is_hidden_from_sandboxes_and_file_tools() -> None:
    leaf = session_bindings.SESSION_BINDINGS_DIR_NAME

    assert leaf in sandbox_leaves._CREW_HIDDEN_LEAVES
    assert leaf in sandbox_precreate._CREW_PRECREATE_HIDDEN_DIR_LEAVES
    assert leaf in paths_lists._CREW_SECRET_LEAVES
    assert leaf not in sandbox_leaves._CREW_SANDBOX_VISIBLE_LEAVES


def _raise_save_failure_after_revocation(
    service: CronService,
    monkeypatch: pytest.MonkeyPatch,
    revoked_job_ids: set[str],
) -> None:
    def fail_save() -> None:
        assert all(
            session_bindings.read_session_binding(job_id) == "" for job_id in revoked_job_ids
        )
        raise OSError("store save failed")

    monkeypatch.setattr(service, "_save", fail_save)


def _raise_strict_revocation_failure(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    expected_job_ids: set[str],
) -> None:
    def fail_revocation(job_ids: object) -> None:
        assert set(job_ids) == expected_job_ids  # type: ignore[arg-type]
        raise OSError("binding revocation failed")

    monkeypatch.setattr(module, "revoke_session_bindings", fail_revocation)


def _assert_persisted_owners(
    store_dir: Path,
    parent_id: str,
    parent_owner: str,
    child_id: str | None = None,
    child_owner: str = "",
) -> None:
    reloaded = CronService(base_dir=store_dir)
    parent = reloaded.get_job(parent_id)
    assert parent is not None and parent.session_key == parent_owner
    if child_id is not None:
        child = reloaded.get_job(child_id)
        assert child is not None and child.session_key == child_owner


def test_release_revokes_before_a_failed_store_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_dir = tmp_path / "store"
    service = CronService(base_dir=store_dir)
    owner = "dashboard:old-owner"
    job = service.add_job("nightly", "run", every_secs=3600, session_key=owner)
    store_before = (store_dir / "crons.json").read_bytes()
    _raise_save_failure_after_revocation(service, monkeypatch, {job.id})

    with pytest.raises(OSError, match="store save failed"):
        service.adopt_job(job.id, "")

    assert (store_dir / "crons.json").read_bytes() == store_before
    assert service.get_job(job.id).session_key == owner
    assert session_bindings.read_session_binding(job.id) == ""
    _assert_persisted_owners(store_dir, job.id, owner)


def test_release_aborts_when_strict_revocation_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_dir = tmp_path / "store"
    service = CronService(base_dir=store_dir)
    owner = "dashboard:old-owner"
    job = service.add_job("nightly", "run", every_secs=3600, session_key=owner)
    store_before = (store_dir / "crons.json").read_bytes()
    _raise_strict_revocation_failure(monkeypatch, job_ownership, {job.id})

    with pytest.raises(OSError, match="binding revocation failed"):
        service.adopt_job(job.id, "")

    assert (store_dir / "crons.json").read_bytes() == store_before
    assert service.get_job(job.id).session_key == owner
    assert session_bindings.read_session_binding(job.id) == owner


def test_adopt_to_another_key_revokes_before_save_and_grants_after_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_dir = tmp_path / "store"
    service = CronService(base_dir=store_dir)
    old_owner = "dashboard:old-owner"
    job = service.add_job("nightly", "run", every_secs=3600, session_key=old_owner)
    store_before = (store_dir / "crons.json").read_bytes()
    granted: list[tuple[str, str]] = []
    monkeypatch.setattr(job_ownership, "record_session_binding", lambda *args: granted.append(args))
    _raise_save_failure_after_revocation(service, monkeypatch, {job.id})

    with pytest.raises(OSError, match="store save failed"):
        service.adopt_job(job.id, "dashboard:new-owner")

    assert granted == []
    assert (store_dir / "crons.json").read_bytes() == store_before
    assert service.get_job(job.id).session_key == old_owner
    assert session_bindings.read_session_binding(job.id) == ""


def test_adopt_to_another_key_aborts_when_strict_revocation_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_dir = tmp_path / "store"
    service = CronService(base_dir=store_dir)
    old_owner = "dashboard:old-owner"
    job = service.add_job("nightly", "run", every_secs=3600, session_key=old_owner)
    store_before = (store_dir / "crons.json").read_bytes()
    _raise_strict_revocation_failure(monkeypatch, job_ownership, {job.id})

    with pytest.raises(OSError, match="binding revocation failed"):
        service.adopt_job(job.id, "dashboard:new-owner")

    assert (store_dir / "crons.json").read_bytes() == store_before
    assert service.get_job(job.id).session_key == old_owner
    assert session_bindings.read_session_binding(job.id) == old_owner


def test_batch_release_revokes_before_a_failed_store_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_dir = tmp_path / "store"
    service = CronService(base_dir=store_dir)
    owner = "dashboard:old-owner"
    jobs = [
        service.add_job(name, "run", every_secs=3600, session_key=owner)
        for name in ("first", "second")
    ]
    job_ids = {job.id for job in jobs}
    store_before = (store_dir / "crons.json").read_bytes()
    _raise_save_failure_after_revocation(service, monkeypatch, job_ids)

    with pytest.raises(OSError, match="store save failed"):
        asyncio.run(service.release_jobs_owned_by({owner}))

    assert (store_dir / "crons.json").read_bytes() == store_before
    assert all(job.session_key == owner for job in service._jobs)
    assert all(session_bindings.read_session_binding(job_id) == "" for job_id in job_ids)


def test_batch_release_aborts_when_strict_revocation_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_dir = tmp_path / "store"
    service = CronService(base_dir=store_dir)
    owner = "dashboard:old-owner"
    jobs = [
        service.add_job(name, "run", every_secs=3600, session_key=owner)
        for name in ("first", "second")
    ]
    job_ids = {job.id for job in jobs}
    store_before = (store_dir / "crons.json").read_bytes()
    _raise_strict_revocation_failure(monkeypatch, job_ownership, job_ids)

    with pytest.raises(OSError, match="binding revocation failed"):
        asyncio.run(service.release_jobs_owned_by({owner}))

    assert (store_dir / "crons.json").read_bytes() == store_before
    assert all(job.session_key == owner for job in service._jobs)
    assert all(session_bindings.read_session_binding(job_id) == owner for job_id in job_ids)


def test_add_does_not_grant_when_the_store_save_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = CronService(base_dir=tmp_path / "store")
    granted: list[tuple[str, str]] = []
    monkeypatch.setattr(job_writes, "record_session_binding", lambda *args: granted.append(args))
    monkeypatch.setattr(service, "_save", lambda: (_ for _ in ()).throw(OSError("store failed")))

    with pytest.raises(OSError, match="store failed"):
        service.add_job("nightly", "run", every_secs=3600, session_key="dashboard:new-owner")

    assert granted == []


def test_adopt_does_not_grant_when_the_store_save_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = CronService(base_dir=tmp_path / "store")
    job = service.add_job("nightly", "run", every_secs=3600)
    granted: list[tuple[str, str]] = []
    monkeypatch.setattr(job_ownership, "record_session_binding", lambda *args: granted.append(args))
    monkeypatch.setattr(service, "_save", lambda: (_ for _ in ()).throw(OSError("store failed")))

    with pytest.raises(OSError, match="store failed"):
        service.adopt_job(job.id, "dashboard:new-owner")

    assert granted == []
    assert service.get_job(job.id).session_key == ""
    assert session_bindings.read_session_binding(job.id) == ""


def _removal_case(tmp_path: Path, path: str) -> tuple[CronService, CronJob, CronJob, Path]:
    store_dir = tmp_path / "store"
    service = CronService(base_dir=store_dir)
    schedule = {"at_ts": 1.0} if path == "one-shot consume" else {"every_secs": 3600}
    parent = service.add_job(
        "parent",
        "run",
        delete_after_run=path == "one-shot consume",
        created_by="app:test",
        session_key="dashboard:parent-owner",
        **schedule,
    )
    child = service.add_job("child", "run", every_secs=3600, session_key=f"cron:{parent.id}")
    if path == "deferred one-shot":
        service.defer_removal(parent.id)
    if path == "one-shot consume":
        parent.last_status = "ok"
        parent.last_run_ts = 100.0
        parent.fire_time_denied = False
        parent.run_never_started = False
    return service, parent, child, store_dir


def _invoke_removal_path(service: CronService, parent: CronJob, path: str) -> object:
    if path == "single":
        return service.remove_job(parent.id, actor="test", source="test")
    if path == "conditional":
        return asyncio.run(
            service.remove_job_if_async(
                parent.id,
                expected_revision="current",
                revision_of=lambda _job: "current",
                actor="test",
                source="test",
            )
        )
    if path == "batch":
        return asyncio.run(service.remove_jobs([parent.id], actor="test", source="test"))
    if path == "by owner":
        return service.remove_jobs_by_owner_sync("app:test")
    if path == "deferred one-shot":
        return service._drain_pending_removals_locked()
    if path == "one-shot consume":
        return service._merge_job_result(parent)
    raise AssertionError(f"unknown removal path: {path}")


@pytest.mark.parametrize(
    "path",
    ["single", "batch", "by owner", "deferred one-shot", "one-shot consume"],
)
def test_removal_paths_revoke_a_recorded_child_when_its_store_owner_was_blanked(
    tmp_path: Path, path: str
) -> None:
    service, parent, child, store_dir = _removal_case(tmp_path, path)
    unrelated = service.add_job(
        "unrelated", "run", every_secs=3600, session_key="dashboard:unrelated"
    )
    _rewrite_stored_session_key(store_dir, child.id, "")
    service._sync()

    assert service.get_job(child.id).session_key == ""
    assert session_bindings.read_session_binding(child.id) == f"cron:{parent.id}"

    _invoke_removal_path(service, parent, path)

    assert session_bindings.read_session_binding(child.id) == ""
    assert session_bindings.read_session_binding(unrelated.id) == "dashboard:unrelated"


def test_removal_aborts_when_the_binding_scan_cannot_enumerate(
    monkeypatch: pytest.MonkeyPatch, binding_home: Path
) -> None:
    service, parent, child, store_dir = _removal_case(binding_home, "single")
    _rewrite_stored_session_key(store_dir, child.id, "")
    service._sync()
    store_before = (store_dir / "crons.json").read_bytes()
    child_record_path = session_bindings.session_binding_path(child.id)
    child_record_before = child_record_path.read_bytes()
    _deny_binding_directory_enumeration(monkeypatch, binding_home)

    with pytest.raises(PermissionError, match="binding directory denied"):
        service.remove_job(parent.id, actor="test", source="test")

    assert (store_dir / "crons.json").read_bytes() == store_before
    assert child_record_path.read_bytes() == child_record_before
    assert session_bindings.read_session_binding(child.id) == f"cron:{parent.id}"
    _assert_persisted_owners(
        store_dir,
        parent.id,
        "dashboard:parent-owner",
        child.id,
        "",
    )


def test_a_sandbox_marker_in_an_unconfined_process_still_aborts_a_removal(
    monkeypatch: pytest.MonkeyPatch, binding_home: Path
) -> None:
    service, parent, child, store_dir = _removal_case(binding_home, "single")
    store_before = (store_dir / "crons.json").read_bytes()
    _deny_binding_directory_enumeration(monkeypatch, binding_home)
    monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")

    with pytest.raises(PermissionError, match="binding directory denied"):
        service.remove_job(parent.id, actor="test", source="test")

    assert (store_dir / "crons.json").read_bytes() == store_before
    assert session_bindings.read_session_binding(child.id) == f"cron:{parent.id}"
    _assert_persisted_owners(
        store_dir, parent.id, "dashboard:parent-owner", child.id, child.session_key
    )


def test_a_confined_removal_saves_when_seatbelt_denies_the_binding_directory(
    monkeypatch: pytest.MonkeyPatch, binding_home: Path
) -> None:
    service, parent, child, store_dir = _removal_case(binding_home, "single")
    _deny_binding_directory_enumeration(monkeypatch, binding_home)
    _deny_binding_record_unlink(monkeypatch, binding_home)
    _set_seatbelt_verdict(monkeypatch, True)

    assert service.remove_job(parent.id, actor="test", source="test")

    reloaded = CronService(base_dir=store_dir)
    assert reloaded.get_job(parent.id) is None
    released_child = reloaded.get_job(child.id)
    assert released_child is not None and released_child.session_key == ""


@pytest.mark.parametrize(
    "path",
    ["single", "conditional", "batch", "by owner", "deferred one-shot", "one-shot consume"],
)
def test_removal_paths_revoke_jobs_and_released_children_before_a_failed_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str
) -> None:
    service, parent, child, store_dir = _removal_case(tmp_path, path)
    store_before = (store_dir / "crons.json").read_bytes()
    revoked = {parent.id, child.id}
    _raise_save_failure_after_revocation(service, monkeypatch, revoked)

    with pytest.raises(OSError, match="store save failed"):
        _invoke_removal_path(service, parent, path)

    assert (store_dir / "crons.json").read_bytes() == store_before
    assert all(session_bindings.read_session_binding(job_id) == "" for job_id in revoked)
    _assert_persisted_owners(
        store_dir,
        parent.id,
        "dashboard:parent-owner",
        child.id,
        f"cron:{parent.id}",
    )


@pytest.mark.parametrize(
    "path",
    ["single", "conditional", "batch", "by owner", "deferred one-shot", "one-shot consume"],
)
def test_removal_paths_abort_when_strict_revocation_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str
) -> None:
    service, parent, child, store_dir = _removal_case(tmp_path, path)
    store_before = (store_dir / "crons.json").read_bytes()
    revoked = {parent.id, child.id}
    module = job_removal
    _raise_strict_revocation_failure(monkeypatch, module, revoked)

    with pytest.raises(OSError, match="binding revocation failed"):
        _invoke_removal_path(service, parent, path)

    assert (store_dir / "crons.json").read_bytes() == store_before
    assert session_bindings.read_session_binding(parent.id) == "dashboard:parent-owner"
    assert session_bindings.read_session_binding(child.id) == f"cron:{parent.id}"
    _assert_persisted_owners(
        store_dir,
        parent.id,
        "dashboard:parent-owner",
        child.id,
        f"cron:{parent.id}",
    )


@pytest.mark.parametrize("path", ["add", "add if absent"])
def test_dead_parent_release_revokes_before_a_failed_store_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str
) -> None:
    store_dir = tmp_path / "store"
    service = CronService(base_dir=store_dir)
    kept = service.add_job("kept", "run", every_secs=3600)
    job = service._build_job("orphan", "run", every_secs=3600, session_key="cron:missing-parent")
    session_bindings.record_session_binding(job.id, job.session_key)
    store_before = (store_dir / "crons.json").read_bytes()
    _raise_save_failure_after_revocation(service, monkeypatch, {job.id})

    with pytest.raises(OSError, match="store save failed"):
        if path == "add":
            service._persist_add_locked(job)
        else:
            service._persist_add_if_absent_locked(lambda _job: False, job)

    assert (store_dir / "crons.json").read_bytes() == store_before
    assert session_bindings.read_session_binding(job.id) == ""
    assert CronService(base_dir=store_dir).get_job(job.id) is None
    assert CronService(base_dir=store_dir).get_job(kept.id) is not None


@pytest.mark.parametrize("path", ["add", "add if absent"])
def test_dead_parent_release_aborts_when_strict_revocation_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str
) -> None:
    store_dir = tmp_path / "store"
    service = CronService(base_dir=store_dir)
    kept = service.add_job("kept", "run", every_secs=3600)
    job = service._build_job("orphan", "run", every_secs=3600, session_key="cron:missing-parent")
    session_bindings.record_session_binding(job.id, job.session_key)
    store_before = (store_dir / "crons.json").read_bytes()
    _raise_strict_revocation_failure(monkeypatch, job_writes, {job.id})

    with pytest.raises(OSError, match="binding revocation failed"):
        if path == "add":
            service._persist_add_locked(job)
        else:
            service._persist_add_if_absent_locked(lambda _job: False, job)

    assert (store_dir / "crons.json").read_bytes() == store_before
    assert session_bindings.read_session_binding(job.id) == "cron:missing-parent"
    assert job.session_key == "cron:missing-parent"
    assert CronService(base_dir=store_dir).get_job(job.id) is None
    assert CronService(base_dir=store_dir).get_job(kept.id) is not None
