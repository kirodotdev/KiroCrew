"""Restart recovery accepts root aliases, never redirected run files."""

import json
import os

import pytest

from conftest import make_dir_link
from kiro_crew.workflow_memory import publish_binding
from kiro_crew.workflows.registry import RunHandle, RunRegistry
from kiro_crew.workflows.store import WorkflowRunStore


def _save(base, run_id="wf_000001"):
    store = WorkflowRunStore(base_dir=base)
    registry = RunRegistry(store=store)
    publish_binding(run_id, "", "dashboard:test")
    handle = RunHandle(run_id=run_id, name="restart", source="large source " * 1000)
    handle.execution_binding_version = 1
    registry.register(handle)
    registry.mark_terminal(run_id, "finished", result={"saved": True})
    return store


def test_restart_restores_public_run_through_symlink_ancestor(tmp_path, monkeypatch):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    make_dir_link(alias, actual)
    monkeypatch.setenv("KIROCREW_HOME", str(alias))
    _save(alias / "workflows")
    restored = RunRegistry(store=WorkflowRunStore(base_dir=alias / "workflows"))
    assert restored.load_persisted() == 1
    run = restored.get("wf_000001")
    assert run.result == {"saved": True}
    assert run.source == "large source " * 1000


@pytest.mark.parametrize("kind", ["hardlink", "leaf", "directory"])
def test_restart_refuses_linked_run_files(tmp_path, kind):
    store = _save(tmp_path / "workflows")
    path = store.runs_dir / "wf_000001.json"
    outside = tmp_path / "outside.json"
    path.rename(outside)
    if kind == "hardlink":
        os.link(outside, path)
    elif kind == "directory":
        make_dir_link(path, tmp_path)
    else:
        try:
            path.symlink_to(outside)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                pytest.skip("File symlink privilege unavailable; directory link covered separately")
            raise
    assert RunRegistry(store=store).load_persisted() == 0


def test_restart_refuses_ancestor_swap_outside_resolved_root(tmp_path, monkeypatch):
    from kiro_crew import platform_compat

    store = _save(tmp_path / "workflows")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "wf_000001.json").write_text(
        json.dumps({"run_id": "wf_000001", "status": "finished", "result": "foreign"})
    )
    real_open = platform_compat.open_file_no_reparse

    def swap_then_open(path, **kwargs):
        store.runs_dir.rename(tmp_path / "retired")
        make_dir_link(store.runs_dir, outside)
        return real_open(path, **kwargs)

    monkeypatch.setattr(platform_compat, "open_file_no_reparse", swap_then_open)
    assert RunRegistry(store=store).load_persisted() == 0


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("damage", ["missing", "malformed"])
def test_restart_requires_surviving_protected_binding(tmp_path, private, damage):
    from kiro_crew.workflow_memory import binding_path, private_payload_path

    run_id = "wf_000001"
    if private:
        store = WorkflowRunStore(base_dir=tmp_path / "workflows")
        publish_binding(run_id, "member-alice", "dashboard:test")
        handle = RunHandle(run_id=run_id, name="PRIVATE_RUN_SENTINEL", execution_binding_version=1)
        store.save(run_id, handle.to_store_json())
        path = private_payload_path(run_id)
    else:
        store = _save(tmp_path / "workflows")
        path = store.runs_dir / f"{run_id}.json"
    saved = path.read_bytes()
    if damage == "missing":
        binding_path(run_id).unlink()
    else:
        binding_path(run_id).write_text("PRIVATE_BINDING_SENTINEL", encoding="utf-8")
    with pytest.raises(OSError, match="inventory unavailable") as error:
        RunRegistry(store=store).load_persisted()
    assert path.read_bytes() == saved
    assert "PRIVATE_" not in str(error.value)
    assert str(tmp_path) not in str(error.value)


def test_restart_isolates_unresolvable_discovery_root(tmp_path, monkeypatch):
    from pathlib import Path

    store = _save(tmp_path / "workflows")
    real_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == store.runs_dir:
            raise OSError("discovery root disappeared")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(OSError, match="inventory unavailable"):
        store.load_all()


def test_restart_refuses_partial_inventory_when_private_root_discovery_fails(tmp_path, monkeypatch):
    from kiro_crew.workflows import store as store_module

    store = _save(tmp_path / "workflows")
    original = store_module.private_payload_path

    def private_path(run_id):
        if run_id == "discovery":
            raise OSError("private discovery unavailable")
        return original(run_id)

    path = store.runs_dir / "wf_000001.json"
    saved = path.read_bytes()
    monkeypatch.setattr(store_module, "private_payload_path", private_path)
    with pytest.raises(OSError, match="inventory unavailable") as error:
        RunRegistry(store=store).load_persisted()
    assert str(tmp_path) not in str(error.value)
    assert "private discovery unavailable" not in str(error.value)
    assert path.read_bytes() == saved
    monkeypatch.setattr(store_module, "private_payload_path", original)
    rows = store.load_all()
    assert len(rows) == 1 and rows[0]["run_id"] == "wf_000001"


@pytest.mark.parametrize("failure", ["private-root", "root", "record"])
def test_discovery_diagnostics_never_log_private_text(tmp_path, monkeypatch, caplog, failure):
    import logging
    from pathlib import Path

    from kiro_crew.workflows import store as store_module

    store = _save(tmp_path / "workflows")
    sentinel = "PRIVATE_DISCOVERY_SECRET_SENTINEL"
    if failure == "private-root":

        def fail_private_path(_run_id):
            raise OSError(sentinel)

        monkeypatch.setattr(store_module, "private_payload_path", fail_private_path)
    elif failure == "root":
        real_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            if path == store.runs_dir:
                raise OSError(sentinel)
            return real_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve)
    else:
        path = store.runs_dir / f"{sentinel}.json"
        path.write_text("broken", encoding="utf-8")
        real_open = store_module.platform_compat.open_file_no_reparse

        def open_record(candidate, **kwargs):
            if candidate == path:
                raise OSError(sentinel)
            return real_open(candidate, **kwargs)

        monkeypatch.setattr(store_module.platform_compat, "open_file_no_reparse", open_record)
    with caplog.at_level(logging.DEBUG, logger=store_module.__name__):
        if failure in ("root", "private-root"):
            with pytest.raises(OSError, match="inventory unavailable") as error:
                store.load_all()
            assert sentinel not in str(error.value)
            assert str(tmp_path) not in str(error.value)
        else:
            rows = store.load_all()
            assert len(rows) == 1
    records = [record for record in caplog.records if record.name == store_module.__name__]
    assert records, "Recovery must retain a useful diagnostic"
    assert "OSError" in caplog.text
    assert sentinel not in caplog.text
    assert str(tmp_path) not in caplog.text
    assert all(record.exc_info is None and record.exc_text is None for record in records)


def test_unreadable_record_with_surrogate_name_does_not_abort_recovery(tmp_path, monkeypatch):
    from pathlib import Path

    from kiro_crew.workflows import store as store_module

    store = _save(tmp_path / "workflows")
    bad_path = store.runs_dir / "unreadable-\udcff.json"
    real_glob = Path.glob
    real_open = store_module.platform_compat.open_file_no_reparse

    def discover(path, pattern):
        yield from real_glob(path, pattern)
        if path == store.runs_dir:
            yield bad_path

    def open_record(path, **kwargs):
        if path == bad_path:
            raise OSError("unreadable record")
        return real_open(path, **kwargs)

    monkeypatch.setattr(Path, "glob", discover)
    monkeypatch.setattr(store_module.platform_compat, "open_file_no_reparse", open_record)
    rows = store.load_all()
    assert len(rows) == 1 and rows[0]["run_id"] == "wf_000001"


def test_private_root_redirect_cannot_return_public_only_inventory(tmp_path):
    from kiro_crew.workflow_memory import private_payload_path

    store = _save(tmp_path / "workflows")
    public_file = store.runs_dir / "wf_000001.json"
    saved = public_file.read_bytes()
    root = private_payload_path("discovery").parent
    root.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "redirected-private-inventory"
    outside.mkdir()
    make_dir_link(root, outside)
    with pytest.raises(OSError, match="inventory unavailable") as error:
        store.load_all()
    assert str(root) not in str(error.value)
    assert str(outside) not in str(error.value)
    assert public_file.read_bytes() == saved


@pytest.mark.parametrize("status", ["running", "finished", "cancelled"])
@pytest.mark.parametrize("execution_error", [None, "cancelled by operator"])
def test_checkpoint_code_does_not_relabel_execution_errors(status, execution_error):
    handle = RunHandle(run_id="wf_code", name="code", status=status, error=execution_error)
    assert "error_code" not in handle.snapshot()
    handle._persistence_error = "checkpoint problem"
    snapshot = handle.snapshot()
    assert snapshot["error"].startswith("checkpoint problem")
    if execution_error:
        assert execution_error in snapshot["error"]
        assert "error_code" not in snapshot
    else:
        assert snapshot["error_code"] == "workflow_checkpoint_failed"
    handle._persistence_error = None
    assert "error_code" not in handle.snapshot()


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("windows_missing", [False, True])
def test_missing_inventory_under_file_is_not_first_boot(
    tmp_path, monkeypatch, nested, windows_missing
):
    from pathlib import Path

    blocker = tmp_path / "blocked"
    blocker.write_text("keep this file", encoding="utf-8")
    base = blocker / "missing" / "workflows" if nested else blocker
    store = WorkflowRunStore(base_dir=base)
    original_stat = Path.stat

    def stat_path(path, *args, **kwargs):
        # Windows can report ERROR_PATH_NOT_FOUND for a child of a plain file,
        # where POSIX reports ENOTDIR. Keep the actual ancestor inode intact.
        if windows_missing and path != blocker and path.is_relative_to(blocker):
            raise FileNotFoundError("path not found")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_path)
    with pytest.raises(OSError, match="inventory unavailable"):
        store.load_all()
    assert blocker.read_text(encoding="utf-8") == "keep this file"


def test_genuinely_missing_inventory_accepts_first_boot(tmp_path):
    store = WorkflowRunStore(base_dir=tmp_path / "not-created" / "workflows")
    assert store.load_all() == []
    assert not store.runs_dir.exists()
