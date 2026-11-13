"""Revision transactions operate only on temporary, pinned resources."""

from __future__ import annotations

import hashlib
import importlib.util
import multiprocessing
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from threading import Barrier

import pytest

from kiro_crew import pinned_fs
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


def test_document_store_surface_exists():
    assert importlib.util.find_spec("kiro_crew.apps.document_store") is not None


@pytest.fixture
def store_module():
    from kiro_crew.apps import document_store

    return document_store


@pytest.fixture
def store(tmp_path, store_module):
    if not pinned_fs.supports_pinned_walk():
        pytest.skip("transactional publication requires descriptor-relative filesystem APIs")
    return store_module.DocumentStore(tmp_path)


def test_create_read_update_return_exact_immutable_snapshot(store, tmp_path):
    body = "日本語\r\nline\n"
    created = store.write("notes.md", body, operation="create")
    assert created.content == body
    assert created.revision == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert (tmp_path / "notes.md").read_bytes() == body.encode("utf-8")
    assert store.read("notes.md") == created
    with pytest.raises(FrozenInstanceError):
        created.content = "changed"
    updated = store.write(
        "notes.md", "changed", operation="update", expected_revision=created.revision
    )
    assert updated == store.read("notes.md")
    assert updated.revision != created.revision


def test_missing_collision_and_stale_revision_preserve_content(store, store_module):
    with pytest.raises(store_module.DocumentStoreError, match="not_found"):
        store.read("notes.md")
    created = store.write("notes.md", "first", operation="create")
    for operation, revision in (("create", None), ("update", "0" * 64)):
        with pytest.raises(store_module.DocumentStoreError) as caught:
            store.write("notes.md", "lost", operation=operation, expected_revision=revision)
        assert caught.value.code == "conflict"
    assert store.read("notes.md") == created
    with pytest.raises(store_module.DocumentStoreError, match="not_found"):
        store.write("absent.md", "lost", operation="update", expected_revision=created.revision)


@pytest.mark.parametrize("revision", [None, "", "bad", "g" * 64, "0" * 63, 1])
def test_update_requires_sha256_revision(store, store_module, revision):
    with pytest.raises(store_module.DocumentStoreError, match="invalid_revision"):
        store.write("notes.md", "x", operation="update", expected_revision=revision)


def test_create_refuses_revision_and_invalid_operation_or_content(store, store_module, tmp_path):
    for kwargs, content, code in (
        ({"operation": "create", "expected_revision": "0" * 64}, "x", "invalid_revision"),
        ({"operation": "delete"}, "x", "invalid_operation"),
        ({"operation": "create"}, b"x", "invalid_content"),
        ({"operation": "create"}, "\ud800", "invalid_content"),
    ):
        with pytest.raises(store_module.DocumentStoreError) as caught:
            store.write("notes.md", content, **kwargs)
        assert caught.value.code == code
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "relative",
    [
        "",
        ".",
        "..",
        "../escape",
        "/absolute",
        "C:\\escape",
        "a\\b",
        "a/../b",
        "a//b",
        "a/./b",
        "nul\x00.md",
        ".kiro-document-test.lock",
        None,
        1,
        b"notes.md",
    ],
)
def test_lexical_path_refusals(store, store_module, relative):
    with pytest.raises(store_module.DocumentStoreError, match="invalid_path"):
        store.read(relative)


def test_nested_leaf_allowed_but_no_implicit_directory_creation(store, tmp_path, store_module):
    (tmp_path / "nested").mkdir()
    assert store.write("nested/notes.md", "ok", operation="create").content == "ok"
    with pytest.raises(store_module.DocumentStoreError):
        store.write("missing/notes.md", "lost", operation="create")
    assert not (tmp_path / "missing").exists()


def test_symlink_root_ancestor_and_leaf_are_refused(store, tmp_path, store_module):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.md").write_text("private", encoding="utf-8")
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    cases = [
        (store_module.DocumentStore(linked), "notes.md"),
        (store_module.DocumentStore(linked / "child"), "notes.md"),
        (store, "linked/notes.md"),
    ]
    (tmp_path / "leaf.md").symlink_to(outside / "notes.md")
    cases.append((store, "leaf.md"))
    for candidate, relative in cases:
        for action in (
            lambda: candidate.read(relative),
            lambda: candidate.write(relative, "lost", operation="create"),
        ):
            with pytest.raises(store_module.DocumentStoreError, match="unsafe_path"):
                action()
    assert (outside / "notes.md").read_text(encoding="utf-8") == "private"


def test_hardlinks_and_nonregular_leaf_refused(store, tmp_path, store_module):
    (tmp_path / "original").write_text("private", encoding="utf-8")
    os.link(tmp_path / "original", tmp_path / "alias.md")
    (tmp_path / "directory.md").mkdir()
    os.mkfifo(tmp_path / "pipe.md")
    for relative in ("alias.md", "directory.md", "pipe.md"):
        with pytest.raises(store_module.DocumentStoreError, match="unsafe_path"):
            store.read(relative)


def test_non_utf8_content_refused(store, tmp_path, store_module):
    (tmp_path / "notes.md").write_bytes(b"\xff")
    with pytest.raises(store_module.DocumentStoreError, match="invalid_encoding"):
        store.read("notes.md")


def test_update_preserves_mode(store, tmp_path):
    created = store.write("notes.md", "first", operation="create")
    (tmp_path / "notes.md").chmod(0o640)
    store.write("notes.md", "second", operation="update", expected_revision=created.revision)
    assert stat.S_IMODE((tmp_path / "notes.md").stat().st_mode) == 0o640


def test_owner_transaction_uses_same_kernel_and_guard_expires(store, store_module):
    with store.locked_path("notes.md") as document:
        created = document.write("first", operation="create")
        assert document.read() == created
        updated = document.write("owner", operation="update", expected_revision=created.revision)
    with pytest.raises(store_module.DocumentStoreError, match="transaction_closed"):
        document.read()
    with pytest.raises(store_module.DocumentStoreError, match="conflict"):
        store.write("notes.md", "app", operation="update", expected_revision=created.revision)
    assert store.read("notes.md") == updated


def _thread_write(store, barrier, content, revision):
    from kiro_crew.apps.document_store import DocumentStoreError

    barrier.wait(timeout=FALLBACK_TIMEOUT_SECS)
    try:
        return store.write(
            "notes.md", content, operation="update", expected_revision=revision
        ).content
    except DocumentStoreError as exc:
        return exc.code


def test_two_threads_one_revision_have_one_winner(store):
    created = store.write("notes.md", "initial", operation="create")
    barrier = Barrier(3)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_thread_write, store, barrier, body, created.revision)
            for body in ("one", "two")
        ]
        barrier.wait(timeout=FALLBACK_TIMEOUT_SECS)
        results = [future.result(timeout=FALLBACK_TIMEOUT_SECS) for future in futures]
    assert results.count("conflict") == 1
    assert store.read("notes.md").content in ("one", "two")


def _process_write(root, barrier, results, content):
    from kiro_crew.apps.document_store import DocumentStore

    store = DocumentStore(Path(root))
    revision = store.read("notes.md").revision
    results.put(_thread_write(store, barrier, content, revision))


def test_two_processes_one_revision_have_one_winner(store, tmp_path):
    store.write("notes.md", "initial", operation="create")
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    results = context.Queue()
    processes = [
        context.Process(target=_process_write, args=(str(tmp_path), barrier, results, body))
        for body in ("one", "two")
    ]
    try:
        for process in processes:
            process.start()
        barrier.wait(timeout=FALLBACK_TIMEOUT_SECS)
        received = [results.get(timeout=FALLBACK_TIMEOUT_SECS) for _ in processes]
        for process in processes:
            process.join(timeout=FALLBACK_TIMEOUT_SECS)
            assert not process.is_alive(), "document writer did not finish"
            assert process.exitcode == 0
        assert received.count("conflict") == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=FALLBACK_TIMEOUT_SECS)
        results.close()
        results.join_thread()


def test_ancestor_swap_cannot_redirect_atomic_write(store, tmp_path, store_module, monkeypatch):
    current = tmp_path / "folder"
    current.mkdir()
    moved = tmp_path / "moved"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_write = store_module.atomic_write

    def swap_then_write(*args, **kwargs):
        current.rename(moved)
        current.symlink_to(outside, target_is_directory=True)
        return original_write(*args, **kwargs)

    monkeypatch.setattr(store_module, "atomic_write", swap_then_write)
    created = store.write("folder/notes.md", "safe", operation="create")
    assert created.content == "safe"
    assert (moved / "notes.md").read_text(encoding="utf-8") == "safe"
    assert list(outside.iterdir()) == []


def test_create_collision_during_staging_never_replaces_other_content(
    store, tmp_path, store_module, monkeypatch
):
    original_write = store_module.atomic_write

    def occupy_then_stage(*args, **kwargs):
        (tmp_path / "notes.md").write_text("other writer", encoding="utf-8")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(store_module, "atomic_write", occupy_then_stage)
    with pytest.raises(store_module.DocumentStoreError, match="conflict"):
        store.write("notes.md", "lost", operation="create")
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "other writer"
    assert not list(tmp_path.glob("*.tmp"))


def test_lock_sidecar_cannot_be_a_link_alias(store, tmp_path, store_module):
    (tmp_path / "private").write_text("private", encoding="utf-8")
    lock_root = tmp_path / "locks"
    lock_root.mkdir()
    parent = tmp_path.stat()
    key = f"{parent.st_dev}:{parent.st_ino}:notes.md".encode()
    sidecar = lock_root / (hashlib.sha256(key).hexdigest() + ".lock")
    store = store_module.DocumentStore(tmp_path, lock_root=lock_root)
    for install in (
        lambda: sidecar.symlink_to(tmp_path / "private"),
        lambda: os.link(tmp_path / "private", sidecar),
    ):
        install()
        with pytest.raises(store_module.DocumentStoreError, match="unsafe_path"):
            store.write("notes.md", "lost", operation="create")
        sidecar.unlink()
    assert (tmp_path / "private").read_text(encoding="utf-8") == "private"


def test_lock_custody_stays_outside_the_resource_directory(tmp_path, store_module):
    if not pinned_fs.supports_pinned_walk():
        pytest.skip("requires pinned filesystem APIs")
    resource = tmp_path / "docs"
    resource.mkdir()
    lock_root = tmp_path / "gateway" / "cache" / "document-locks"
    store = store_module.DocumentStore(resource, lock_root=lock_root)
    store.write("notes.md", "first", operation="create")
    assert [p.name for p in resource.iterdir()] == ["notes.md"]
    locks = list(lock_root.iterdir())
    assert len(locks) == 1
    assert len(locks[0].stem) == 64
    assert stat.S_IMODE(locks[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(lock_root.stat().st_mode) == 0o700


def test_linked_lock_custody_is_refused_before_document_mutation(tmp_path, store_module):
    if not pinned_fs.supports_pinned_walk():
        pytest.skip("requires pinned filesystem APIs")
    resource = tmp_path / "docs"
    resource.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    store = store_module.DocumentStore(resource, lock_root=linked / "locks")
    with pytest.raises(store_module.DocumentStoreError, match="unsafe_path"):
        store.write("notes.md", "lost", operation="create")
    assert list(outside.iterdir()) == []
    assert list(resource.iterdir()) == []


def test_platform_without_pinning_fails_closed_before_io(tmp_path, store_module, monkeypatch):
    monkeypatch.setattr(store_module.pinned_fs, "supports_pinned_walk", lambda: False)
    store = store_module.DocumentStore(tmp_path)
    with pytest.raises(store_module.DocumentStoreError, match="unsupported_platform"):
        store.write("notes.md", "lost", operation="create")
    assert list(tmp_path.iterdir()) == []


def test_document_limit_counts_utf8_bytes_before_creating_a_file(store, tmp_path, store_module):
    bounded = store_module.DocumentStore(tmp_path, max_bytes=4)
    for content in ("abcde", "日本"):
        with pytest.raises(store_module.DocumentStoreError, match="document_too_large"):
            bounded.write("notes.md", content, operation="create")
    assert list(tmp_path.iterdir()) == []
    assert bounded.write("notes.md", "éé", operation="create").content == "éé"


def test_document_read_and_update_refuse_an_oversized_existing_file(store, tmp_path, store_module):
    bounded = store_module.DocumentStore(tmp_path, max_bytes=4)
    created = bounded.write("notes.md", "four", operation="create")
    (tmp_path / "notes.md").write_bytes(b"12345")
    with pytest.raises(store_module.DocumentStoreError, match="document_too_large"):
        bounded.read("notes.md")
    with pytest.raises(store_module.DocumentStoreError, match="document_too_large"):
        bounded.write("notes.md", "new", operation="update", expected_revision=created.revision)
    assert (tmp_path / "notes.md").read_bytes() == b"12345"
