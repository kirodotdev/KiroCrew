"""Native Notes vault grants and Owner/App compare-and-swap cooperation."""

from __future__ import annotations

import asyncio
import hashlib
import multiprocessing
import os
from contextlib import contextmanager
from pathlib import Path
from threading import Event

import pytest

from kiro_crew.apps.document_store import DocumentStoreError
from kiro_crew.apps.resource_access import ResourceAccessError
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


@pytest.fixture
def native_vault(tmp_path, monkeypatch):
    from kiro_crew.apps.builtins.md_notebook import server_store

    workspace = tmp_path / "workspace"
    root = workspace / "notes"
    root.mkdir(parents=True)
    vault = {"id": "vault1", "localPath": str(root), "localOnly": True}
    monkeypatch.setattr(server_store, "_read_vaults_sync", lambda: [vault])
    return workspace, root, vault


@pytest.mark.asyncio
async def test_notes_use_native_vault_and_exact_revision(native_vault):
    from kiro_crew.apps.builtins.md_notebook.scoped_documents import read_resource

    workspace, root, _ = native_vault
    created = await read_resource(
        workspace, "vault1", "one.md", "create", {"content": "first", "expectedRevision": None}
    )
    assert created["revision"] == hashlib.sha256(b"first").hexdigest()
    read = await read_resource(workspace, "vault1", "one.md", "read", {})
    assert read == created
    updated = await read_resource(
        workspace,
        "vault1",
        "one.md",
        "update",
        {"content": "second", "expectedRevision": read["revision"]},
    )
    assert (root / "one.md").read_text() == updated["content"] == "second"
    with pytest.raises(DocumentStoreError, match="conflict"):
        await read_resource(
            workspace,
            "vault1",
            "one.md",
            "update",
            {"content": "stale", "expectedRevision": read["revision"]},
        )
    assert (root / "one.md").read_text() == "second"


@pytest.mark.asyncio
async def test_notes_refuse_foreign_vault_nested_document_and_unsupported_platform(
    native_vault, monkeypatch
):
    from kiro_crew.apps import document_store
    from kiro_crew.apps.builtins.md_notebook.scoped_documents import read_resource

    workspace, _, _ = native_vault
    with pytest.raises(ResourceAccessError, match="resource_not_found"):
        await read_resource(workspace, "unknown", "one.md", "read", {})
    with pytest.raises(ResourceAccessError, match="invalid_document"):
        await read_resource(workspace, "vault1", "folder/one.md", "read", {})
    with pytest.raises(ResourceAccessError, match="resource_scope_mismatch"):
        await read_resource(workspace / "other", "vault1", "one.md", "read", {})
    monkeypatch.setattr(document_store.pinned_fs, "supports_pinned_walk", lambda: False)
    with pytest.raises(DocumentStoreError, match="unsupported_platform"):
        await read_resource(
            workspace, "vault1", "one.md", "create", {"content": "x", "expectedRevision": None}
        )


@pytest.mark.asyncio
async def test_owner_stale_hash_cannot_overwrite_app_write(native_vault):
    from kiro_crew.apps.builtins.md_notebook import server
    from kiro_crew.apps.builtins.md_notebook.scoped_documents import read_resource

    workspace, root, vault = native_vault
    created = await read_resource(
        workspace, "vault1", "one.md", "create", {"content": "first", "expectedRevision": None}
    )
    await read_resource(
        workspace,
        "vault1",
        "one.md",
        "update",
        {"content": "app", "expectedRevision": created["revision"]},
    )
    with pytest.raises(server.ApiError) as error:
        await server._save_note_contents(
            vault, root / "one.md", "one.md", "owner", None, created["revision"]
        )
    assert error.value.code == "ESTALE"
    assert (root / "one.md").read_text() == "app"


@pytest.mark.asyncio
async def test_app_stale_hash_cannot_overwrite_owner_write(native_vault):
    from kiro_crew.apps.builtins.md_notebook import server
    from kiro_crew.apps.builtins.md_notebook.scoped_documents import read_resource

    workspace, root, vault = native_vault
    created = await read_resource(
        workspace, "vault1", "one.md", "create", {"content": "first", "expectedRevision": None}
    )
    await server._save_note_contents(
        vault, root / "one.md", "one.md", "owner", None, created["revision"]
    )
    with pytest.raises(DocumentStoreError, match="conflict"):
        await read_resource(
            workspace,
            "vault1",
            "one.md",
            "update",
            {"content": "stale app", "expectedRevision": created["revision"]},
        )
    assert (root / "one.md").read_text() == "owner"


@pytest.mark.asyncio
async def test_hook_registers_without_reading_registry(native_vault, monkeypatch):
    from types import SimpleNamespace

    from aiohttp import web

    from kiro_crew.apps.builtins.md_notebook import scoped_documents, server_store
    from kiro_crew.apps.resource_access import registry_for

    monkeypatch.setattr(
        server_store, "_read_vaults_sync", lambda: pytest.fail("startup registry read")
    )
    app = web.Application()
    assert scoped_documents.register_routes(SimpleNamespace(http_app=app)) == []
    assert "md-notebook" in registry_for(app).providers


@pytest.mark.asyncio
async def test_owner_save_publishes_under_shared_document_and_native_vault_locks(
    native_vault, monkeypatch
):
    from kiro_crew.apps import document_store
    from kiro_crew.apps.builtins.md_notebook import server
    from kiro_crew.apps.builtins.md_notebook.scoped_documents import read_resource

    workspace, root, vault = native_vault
    created = await read_resource(
        workspace, "vault1", "one.md", "create", {"content": "first", "expectedRevision": None}
    )
    entered = []
    real_locked = document_store.DocumentStore.locked_path
    real_write = document_store.LockedDocument.write

    @contextmanager
    def observed_lock(store, relative):
        with real_locked(store, relative) as document:
            entered.append(relative)
            try:
                yield document
            finally:
                entered.pop()

    def observed_write(document, *args, **kwargs):
        assert entered == ["__vault_write__", "one.md"]
        assert server.vault_write_lock(vault["localPath"]).locked()
        return real_write(document, *args, **kwargs)

    monkeypatch.setattr(document_store.DocumentStore, "locked_path", observed_lock)
    monkeypatch.setattr(document_store.LockedDocument, "write", observed_write)
    await server._save_note_contents(
        vault, root / "one.md", "one.md", "owner", None, created["revision"]
    )
    assert (root / "one.md").read_text() == "owner"


@pytest.mark.asyncio
async def test_owner_locator_binds_only_native_markdown_and_windows_keeps_owner_fallback(
    native_vault, monkeypatch
):
    from kiro_crew.apps.builtins.md_notebook import scoped_documents

    workspace, root, vault = native_vault
    found = await scoped_documents.owner_locator(root / "one.md")
    assert found.root == root
    assert found.relative == "one.md"
    assert await scoped_documents.owner_locator(root / ".git" / "config") is None
    assert await scoped_documents.owner_locator(workspace / "foreign.md") is None
    from kiro_crew.apps.builtins.md_notebook import server_vault_lock

    monkeypatch.setattr(server_vault_lock.platform_compat, "IS_WINDOWS", True)
    assert scoped_documents.owner_save(vault, "one.md", "content", None, None) is False
    with pytest.raises(DocumentStoreError, match="unsupported_platform"):
        await scoped_documents.read_resource(
            workspace, "vault1", "one.md", "create", {"content": "x", "expectedRevision": None}
        )
    assert not (root / "one.md").exists()


@pytest.mark.asyncio
async def test_hashless_owner_cannot_overwrite_app_edit_with_unchanged_mtime(native_vault):
    from kiro_crew.apps.builtins.md_notebook import server
    from kiro_crew.apps.builtins.md_notebook.scoped_documents import read_resource

    workspace, root, vault = native_vault
    created = await read_resource(
        workspace, "vault1", "one.md", "create", {"content": "first", "expectedRevision": None}
    )
    before = (root / "one.md").stat()
    await read_resource(
        workspace,
        "vault1",
        "one.md",
        "update",
        {"content": "app", "expectedRevision": created["revision"]},
    )
    os.utime(root / "one.md", ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(server.ApiError) as error:
        await server._save_note_contents(
            vault, root / "one.md", "one.md", "hashless owner", before.st_mtime * 1000, None
        )
    assert error.value.code == "ESTALE"
    assert error.value.extra["contentHash"] == hashlib.sha256(b"app").hexdigest()
    assert (root / "one.md").read_text() == "app"


def _hold_native_vault_lock(local_path, home, connection):
    os.environ["KIROCREW_HOME"] = home
    from kiro_crew.apps.document_store import DocumentStore

    with DocumentStore(Path(local_path)).locked_path("__vault_write__"):
        connection.send("held")
        if not connection.poll(FALLBACK_TIMEOUT_SECS):
            raise RuntimeError("vault lock release signal missing")
        connection.recv()
    connection.close()


@pytest.mark.asyncio
async def test_app_write_waits_for_native_vault_mutex_in_another_process(
    native_vault, monkeypatch, tmp_path
):
    from kiro_crew.apps import document_store
    from kiro_crew.apps.builtins.md_notebook.scoped_documents import read_resource

    workspace, root, _ = native_vault
    home = tmp_path / "crew-home"
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_hold_native_vault_lock, args=(str(root), str(home), child))
    attempting = Event()
    real_locked = document_store.DocumentStore.locked_path

    @contextmanager
    def observed(store, relative):
        if relative == "__vault_write__":
            attempting.set()
        with real_locked(store, relative) as document:
            yield document

    process.start()
    child.close()
    try:
        assert await asyncio.to_thread(parent.poll, FALLBACK_TIMEOUT_SECS)
        assert parent.recv() == "held"
        monkeypatch.setattr(document_store.DocumentStore, "locked_path", observed)
        writing = asyncio.create_task(
            read_resource(
                workspace,
                "vault1",
                "one.md",
                "create",
                {"content": "app", "expectedRevision": None},
            )
        )
        assert await asyncio.to_thread(attempting.wait, FALLBACK_TIMEOUT_SECS)
        assert not writing.done()
        assert not (root / "one.md").exists()
        parent.send("release")
        assert (await asyncio.wait_for(writing, FALLBACK_TIMEOUT_SECS))["content"] == "app"
        await asyncio.to_thread(process.join, FALLBACK_TIMEOUT_SECS)
        assert process.exitcode == 0
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, FALLBACK_TIMEOUT_SECS)


@pytest.mark.asyncio
async def test_cancelled_vault_lock_entry_is_drained_and_released(native_vault, monkeypatch):
    from kiro_crew.apps import document_store
    from kiro_crew.apps.builtins.md_notebook.server_vault_lock import vault_write_lock

    _, root, _ = native_vault
    entered, release, exited = Event(), Event(), Event()

    @contextmanager
    def delayed(store, relative):
        entered.set()
        assert release.wait(FALLBACK_TIMEOUT_SECS)
        try:
            yield None
        finally:
            exited.set()

    monkeypatch.setattr(document_store.DocumentStore, "locked_path", delayed)
    lock = vault_write_lock(str(root))

    async def waiting():
        async with lock:
            pytest.fail("cancelled lock waiter entered its body")

    waiter = asyncio.create_task(waiting())
    assert await asyncio.to_thread(entered.wait, FALLBACK_TIMEOUT_SECS)
    waiter.cancel()
    waiter.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiter, FALLBACK_TIMEOUT_SECS)
    assert exited.is_set()
    assert not lock.locked()
