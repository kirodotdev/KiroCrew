"""Scoped writes use the existing registered Spec, never a new directory."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_registered_spec_create_update_and_readback(tmp_path, monkeypatch):
    from kiro_crew.apps.builtins.spec_builder.backend import scoped_documents as docs

    target = tmp_path / ".kiro" / "specs" / "existing"
    target.mkdir(parents=True)
    (target / "requirements.md").write_text("old", encoding="utf-8")
    monkeypatch.setattr(
        docs, "_aload_index", AsyncMock(return_value={"existing": {"spec_dir": str(target)}})
    )
    original = await docs.read_resource(tmp_path, "existing", "requirements.md", "read", {})
    saved = await docs.read_resource(
        tmp_path,
        "existing",
        "requirements.md",
        "update",
        {"content": "saved", "expectedRevision": original["revision"]},
    )
    assert saved["content"] == "saved"
    assert (await docs.read_resource(tmp_path, "existing", "requirements.md", "read", {})) == saved
    with pytest.raises(Exception, match="conflict"):
        await docs.read_resource(
            tmp_path,
            "existing",
            "requirements.md",
            "update",
            {"content": "lost", "expectedRevision": original["revision"]},
        )
    design = await docs.read_resource(
        tmp_path, "existing", "design.md", "create", {"content": "design", "expectedRevision": None}
    )
    assert design["content"] == "design"
    assert not (target / "tasks.md").exists()


@pytest.mark.asyncio
async def test_unregistered_spec_and_nonformal_document_do_not_write(tmp_path, monkeypatch):
    from kiro_crew.apps.builtins.spec_builder.backend import scoped_documents as docs

    monkeypatch.setattr(docs, "_aload_index", AsyncMock(return_value={}))
    with pytest.raises(ValueError, match="resource_not_found"):
        await docs.read_resource(
            tmp_path, "missing", "design.md", "create", {"content": "new", "expectedRevision": None}
        )
    with pytest.raises(ValueError, match="invalid_document"):
        await docs.read_resource(
            tmp_path, "missing", "code.py", "create", {"content": "new", "expectedRevision": None}
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_index_cannot_redirect_granted_spec_to_other_resource(tmp_path, monkeypatch):
    from kiro_crew.apps.builtins.spec_builder.backend import scoped_documents as docs

    other = tmp_path / ".kiro" / "specs" / "other"
    other.mkdir(parents=True)
    monkeypatch.setattr(
        docs, "_aload_index", AsyncMock(return_value={"existing": {"spec_dir": str(other)}})
    )
    with pytest.raises(ValueError, match="resource_scope_mismatch"):
        await docs.read_resource(
            tmp_path,
            "existing",
            "design.md",
            "create",
            {"content": "bad", "expectedRevision": None},
        )
    assert list(other.iterdir()) == []


@pytest.mark.asyncio
async def test_mutation_without_native_occupancy_state_is_not_guessed_safe(tmp_path):
    from kiro_crew.apps.builtins.spec_builder.backend import scoped_documents as docs
    from kiro_crew.apps.resource_access import ResourceAccessError

    with pytest.raises(ResourceAccessError, match="resource_unavailable"):
        await docs.guarded_reader({})(
            tmp_path, "fixture", "design.md", "create", {"content": "new", "expectedRevision": None}
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True])
async def test_mutation_serializes_with_native_turn_and_refuses_active_editor(
    tmp_path, monkeypatch, running
):
    from kiro_crew.apps.builtins.spec_builder.backend import scoped_documents as docs
    from kiro_crew.apps.builtins.spec_builder.backend.orchestration import turn_guard
    from kiro_crew.apps.resource_access import ResourceAccessError

    target = tmp_path / ".kiro" / "specs" / "fixture"
    target.mkdir(parents=True)
    monkeypatch.setattr(
        docs, "_aload_index", AsyncMock(return_value={"fixture": {"spec_dir": str(target)}})
    )

    async def aliases(directory, **kwargs):
        assert turn_guard._turn_lock(directory).locked()
        return {"slot": "fixture"}

    monkeypatch.setattr(turn_guard, "_alias_slots", aliases)
    monkeypatch.setattr(turn_guard, "_exec_loop_active_for_slot", lambda key: False)
    monkeypatch.setattr(turn_guard, "_busy_observed_directory_slot", lambda *args: "")
    state = SimpleNamespace(get_slot=lambda key: SimpleNamespace(running=running))
    access = docs.guarded_reader({"state": state})
    args = (
        tmp_path,
        "fixture",
        "design.md",
        "create",
        {"content": "saved", "expectedRevision": None},
    )
    if running:
        with pytest.raises(ResourceAccessError, match="resource_busy"):
            await access(*args)
        assert not (target / "design.md").exists()
    else:
        assert (await access(*args))["content"] == "saved"
    assert not turn_guard._turn_lock(str(target)).locked()


@pytest.mark.asyncio
async def test_cancelled_request_drains_document_publication(tmp_path, monkeypatch):
    from kiro_crew.apps.builtins.spec_builder.backend import scoped_documents as docs
    from kiro_crew.apps.document_store import DocumentStore

    target = tmp_path / ".kiro" / "specs" / "fixture"
    target.mkdir(parents=True)
    monkeypatch.setattr(
        docs, "_aload_index", AsyncMock(return_value={"fixture": {"spec_dir": str(target)}})
    )
    loop = asyncio.get_running_loop()
    started = loop.create_future()
    release = threading.Event()
    original = DocumentStore.write

    def delayed_write(self, *args, **kwargs):
        loop.call_soon_threadsafe(started.set_result, None)
        assert release.wait(5), "test publication barrier was not released"
        return original(self, *args, **kwargs)

    monkeypatch.setattr(DocumentStore, "write", delayed_write)
    task = asyncio.create_task(
        docs.read_resource(
            tmp_path,
            "fixture",
            "design.md",
            "create",
            {"content": "saved", "expectedRevision": None},
        )
    )
    try:
        await asyncio.wait_for(started, 5)
        task.cancel()
        cancellation_turn = loop.create_future()
        loop.call_soon(cancellation_turn.set_result, None)
        await cancellation_turn
        assert not task.done(), "the request must retain its native lock until publication ends"
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    assert (target / "design.md").read_text() == "saved"
