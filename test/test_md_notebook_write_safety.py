"""Notes (md-notebook) backend: concurrency and crash safety of note writes.

Covers the vault lock that writes take against a running sync, atomic staged saves,
contended-rename retries and their stale-write guard, tokenless saves serialized under
the note lock, and which staged temps ``status()`` may hide. Shared fixtures live in
``md_notebook_helpers.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import pytest
from md_notebook_helpers import _seed_template  # noqa: F401 - fixture
from md_notebook_helpers import fixtures_fixture  # noqa: F401 - fixture
from md_notebook_helpers import _BARRIER_LOST_RUN_SEC, _clone, _git, _wait_for, signed_client

from kiro_crew.apps.builtins.md_notebook import git_ops
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.testing.wait import await_until, until_parked


def _parking_vault_lock(server_mod, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Route every ``vault_write_lock`` taker through the real lock, recording who waits.

    The returned list holds one entry per caller suspended in ``acquire()``. Nothing
    awaits between the append and the acquire, so once a writer shows up here it is
    parked on the lock the sync holds; a "did not wait" assertion made after that is a
    consequence, never a guess about how far the request got in a fixed sleep.
    """
    real = server_mod.vault_write_lock
    parked: list[str] = []

    @asynccontextmanager
    async def observed(local_path: str) -> AsyncIterator[None]:
        lock = real(local_path)
        parked.append(local_path)
        try:
            await lock.acquire()
        finally:
            parked.remove(local_path)
        try:
            yield
        finally:
            lock.release()

    monkeypatch.setattr(server_mod, "vault_write_lock", observed)
    return parked


@pytest.mark.asyncio
async def test_creating_writes_wait_for_a_running_sync(fixtures, monkeypatch) -> None:
    """The symlink walk and the write it guards are only meaningful if no
    `git merge` can rewrite the tree between them. So every sync holds the
    vault's write lock for its whole run, and every creating write takes the
    same lock around its check-and-write (new, duplicate, move, save, and the
    move into `.trash`): a note created while a sync is in flight is created
    AFTER the merge, against the tree the merge produced."""
    _mod, remote, _seed = fixtures
    from kiro_crew.apps.builtins.md_notebook import syncer as syncer_mod

    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        lock = _mod.vault_write_lock(vault["localPath"])
        entered = asyncio.Event()
        release = asyncio.Event()
        real_sync = _mod.git_ops.sync

        async def slow_sync(*args, **kwargs):  # type: ignore[no-untyped-def]
            # Observed from inside the merge: the lock is held by the sync.
            assert lock.locked(), "sync ran without the vault write lock"
            entered.set()
            # flake-ok: pre-existing unbounded Event wait inside the fake; the test sets the event itself, moved verbatim; tracked for a de-flake pass
            await release.wait()
            return await real_sync(*args, **kwargs)

        monkeypatch.setattr(_mod.git_ops, "sync", slow_sync)
        monkeypatch.setattr(syncer_mod.git_ops, "sync", slow_sync)
        parked = _parking_vault_lock(_mod, monkeypatch)

        # The manual sync, then the background one: both hold the lock.
        for start in (
            lambda: client.post("/api/sync"),
            lambda: syncer_mod._sync_vault(vault),
        ):
            entered.clear()
            release.clear()
            syncing = asyncio.ensure_future(start())
            await await_until(entered)
            assert parked == []
            for path, payload in (
                ("/api/note/new", {"folder": "Projects"}),
                ("/api/note/duplicate", {"path": "One.md"}),
                ("/api/note/move", {"from": "One.md", "to": "Moved/One.md"}),
                ("DELETE /api/note?path=One.md", None),
            ):
                if path.startswith("DELETE "):
                    writing = asyncio.ensure_future(client.delete(path[len("DELETE ") :]))
                else:
                    writing = asyncio.ensure_future(client.post(path, payload))
                await _wait_for(
                    lambda: parked or writing.done(),
                    f"{path} never reached the vault lock",
                    _BARRIER_LOST_RUN_SEC,
                )
                assert not writing.done(), f"{path} did not wait for the sync"
                writing.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    # flake-ok: pre-existing unbounded await of the task this test just cancelled, moved verbatim; tracked for a de-flake pass
                    await writing
                # The server cancels the dropped request's handler; wait until it has
                # left the queue, so the next writer's barrier sees only that writer.
                await _wait_for(
                    lambda: not parked,
                    f"{path}'s cancelled handler stayed queued",
                    _BARRIER_LOST_RUN_SEC,
                )
            saving = asyncio.ensure_future(
                client.put("/api/note", {"path": "Saved/New.md", "content": "x"})
            )
            await _wait_for(
                lambda: parked or saving.done(),
                "the save never reached the vault lock",
                _BARRIER_LOST_RUN_SEC,
            )
            assert not saving.done(), "save did not wait for the sync"
            release.set()
            # flake-ok: pre-existing unbounded await of the sync task the test released above, moved verbatim; tracked for a de-flake pass
            await syncing
            # flake-ok: pre-existing unbounded await of the save request the test released above, moved verbatim; tracked for a de-flake pass
            status, body = await saving
            assert status == 200, body
        assert (Path(vault["localPath"]) / "Saved" / "New.md").exists()


@pytest.mark.asyncio
async def test_a_symlink_landing_while_a_writer_waits_on_the_lock_is_refused(
    fixtures, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fence closed: resolve the destination INSIDE the lock, after the walk.

    The destination folder is a REAL directory when the request arrives, so a
    pre-lock resolution would capture a benign path. Then a sync's merge lands
    `Projects -> .git/refs/heads` in its place while the create/save parks on the
    vault write lock. Because the link walk AND the directory resolution both run
    inside the lock — in the same worker thread as the write, on the tree the
    merge produced — the write is refused (`folder_is_symlink`) and `.git` is
    never touched. A resolution captured before the lock would instead have aimed
    the write at the stale target and passed the in-lock lexical walk.
    """
    _mod, remote, _seed = fixtures
    from kiro_crew.apps.builtins.md_notebook import syncer as syncer_mod

    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        heads = root / ".git" / "refs" / "heads"
        assert heads.is_dir()
        # `Projects` starts as a REAL directory, so any resolution done before
        # the lock resolves to a benign in-vault path.
        (root / "Projects").mkdir()
        try:
            (root / ".git-swap-probe").symlink_to(".git", target_is_directory=True)
            (root / ".git-swap-probe").unlink()
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform/filesystem")

        lock = _mod.vault_write_lock(vault["localPath"])
        entered = asyncio.Event()
        release = asyncio.Event()
        real_sync = _mod.git_ops.sync

        async def swapping_sync(*args, **kwargs):  # type: ignore[no-untyped-def]
            # Stand in for a merge: while holding the lock, replace the real
            # `Projects` directory with a link into `.git`, exactly what a pulled
            # commit could do to the tree.
            assert lock.locked(), "sync ran without the vault write lock"
            (root / "Projects").rmdir()
            (root / "Projects").symlink_to(
                Path(".git") / "refs" / "heads", target_is_directory=True
            )
            entered.set()
            # flake-ok: pre-existing unbounded Event wait inside the fake; the test sets the event itself, moved verbatim; tracked for a de-flake pass
            await release.wait()
            return await real_sync(*args, **kwargs)

        monkeypatch.setattr(_mod.git_ops, "sync", swapping_sync)
        monkeypatch.setattr(syncer_mod.git_ops, "sync", swapping_sync)
        parked = _parking_vault_lock(_mod, monkeypatch)

        before = sorted(p.name for p in heads.iterdir())
        syncing = asyncio.ensure_future(client.post("/api/sync"))
        await await_until(entered)
        # The link is now in place; these requests park on the lock the sync holds.
        creating = asyncio.ensure_future(client.post("/api/note/new", {"folder": "Projects"}))
        saving = asyncio.ensure_future(
            client.put("/api/note", {"path": "Projects/Plan.md", "content": "x"})
        )
        await _wait_for(
            lambda: len(parked) == 2 or creating.done() or saving.done(),
            "the create and the save never both queued on the vault lock",
            _BARRIER_LOST_RUN_SEC,
        )
        assert not creating.done(), "the create did not wait for the sync"
        assert not saving.done(), "the save did not wait for the sync"
        release.set()
        # flake-ok: pre-existing unbounded await of the sync task the test released above, moved verbatim; tracked for a de-flake pass
        await syncing
        # flake-ok: pre-existing unbounded await of the create request the test released above, moved verbatim; tracked for a de-flake pass
        create_status, create_body = await creating
        # flake-ok: pre-existing unbounded await of the save request the test released above, moved verbatim; tracked for a de-flake pass
        save_status, save_body = await saving
        assert create_status == 400, create_body
        assert create_body["code"] == "folder_is_symlink"
        assert save_status == 400, save_body
        assert save_body["code"] == "folder_is_symlink"
        assert sorted(p.name for p in heads.iterdir()) == before, ".git must be untouched"
        assert not (heads / "Untitled.md").exists()
        assert not (heads / "Plan.md").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("target_folder", [".trash", ".git"])
@pytest.mark.parametrize("remove_link", [False, True], ids=["link-remains", "link-removed"])
async def test_duplicate_source_link_swap_under_the_vault_lock(
    fixtures, monkeypatch: pytest.MonkeyPatch, target_folder: str, remove_link: bool
) -> None:
    """A duplicate must neither read a linked folder nor retain its content.

    A merge holds the lock while it installs a link and optionally replaces it
    with a real folder. Lock-entry notification orders the swap without sleeps.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        target = root / target_folder
        target.mkdir(exist_ok=True)
        hidden = target / "Private.md"
        hidden.write_text("local-only content", encoding="utf-8")
        projects = root / "Projects"
        projects.mkdir()
        try:
            probe = root / "LinkProbe"
            probe.symlink_to(target_folder, target_is_directory=True)
            probe.unlink()
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform/filesystem")

        lock = _mod.vault_write_lock(vault["localPath"])
        waiting = asyncio.Event()

        @asynccontextmanager
        async def observed_lock(_path):
            waiting.set()
            async with lock:
                yield

        hidden_reads = []
        real_read = _mod.hooks.safe_read_file_bytes

        def observing_read(path, *args, **kwargs):
            if Path(path).resolve() == hidden.resolve():
                hidden_reads.append(path)
            return real_read(path, *args, **kwargs)

        monkeypatch.setattr(_mod, "vault_write_lock", observed_lock)
        monkeypatch.setattr(_mod.hooks, "safe_read_file_bytes", observing_read)
        writing = None
        try:
            async with lock:
                projects.rmdir()
                projects.symlink_to(target_folder, target_is_directory=True)
                writing = asyncio.create_task(
                    client.post("/api/note/duplicate", {"path": "Projects/Private.md"})
                )
                await await_until(waiting)
                assert not writing.done()
                if remove_link:
                    projects.unlink()
                    projects.mkdir()
                    (projects / "Private.md").write_text("current note", encoding="utf-8")
            status, body = await await_until(writing)
            if remove_link:
                assert status == 200, body
                assert (projects / "Private copy.md").read_text(encoding="utf-8") == "current note"
            else:
                assert status == 400, body
                assert body["code"] == "folder_is_symlink"
            assert hidden_reads == [], "the duplicate read local-only link-target content"
            assert hidden.read_text(encoding="utf-8") == "local-only content"
            assert not (target / "Private copy.md").exists()
        finally:
            if writing is not None and not writing.done():
                writing.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    # flake-ok: pre-existing unbounded await of the task this test just cancelled, moved verbatim; tracked for a de-flake pass
                    await writing


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["delete", "move"])
@pytest.mark.parametrize("alias", [False, True], ids=["note", "alias"])
async def test_scoped_mutation_rederives_paths_after_a_scope_link_swap(
    fixtures, monkeypatch: pytest.MonkeyPatch, operation: str, alias: bool
) -> None:
    """A scope link replaced during lock contention cannot retain a stale target.

    Both trees contain the requested entry. Only the real scope may be mutated,
    and an alias must move as a link rather than displacing its target.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote, subfolder="Scope")
        root = Path(vault["localPath"])
        public = root / "public"
        public.mkdir()
        (public / "Kept.md").write_text("tracked target", encoding="utf-8")
        scope = root / "Scope"
        try:
            scope.symlink_to("public", target_is_directory=True)
            if alias:
                (public / "Alias.md").symlink_to("Kept.md")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform/filesystem")
        name = "Alias.md" if alias else "Kept.md"
        lock = _mod.vault_write_lock(vault["localPath"])
        waiting = asyncio.Event()

        @asynccontextmanager
        async def observed_lock(_path):
            waiting.set()
            async with lock:
                yield

        monkeypatch.setattr(_mod, "vault_write_lock", observed_lock)
        writing = None
        try:
            async with lock:
                writing = asyncio.create_task(
                    client.delete(f"/api/note?path={name}")
                    if operation == "delete"
                    else client.post("/api/note/move", {"from": name, "to": "Moved.md"})
                )
                await await_until(waiting)
                assert not writing.done()
                scope.unlink()
                scope.mkdir()
                (scope / "Kept.md").write_text("scope note", encoding="utf-8")
                if alias:
                    (scope / "Alias.md").symlink_to("Kept.md")
            status, body = await await_until(writing)
            assert status == 200, body
            assert (public / "Kept.md").read_text(encoding="utf-8") == "tracked target"
            assert (public / name).exists(), "the stale target entry was mutated"
            assert not (public / "Moved.md").exists()
            assert not (public / ".trash").exists()
            assert not (scope / name).exists() and not (scope / name).is_symlink()
            destination = scope / (body["trashed"] if operation == "delete" else "Moved.md")
            if alias:
                assert destination.is_symlink()
                assert destination.readlink() == Path("Kept.md")
                assert (public / "Alias.md").is_symlink()
                assert (scope / "Kept.md").read_text(encoding="utf-8") == "scope note"
            else:
                assert destination.read_text(encoding="utf-8") == "scope note"
        finally:
            if writing is not None and not writing.done():
                writing.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    # flake-ok: pre-existing unbounded await of the task this test just cancelled, moved verbatim; tracked for a de-flake pass
                    await writing


@pytest.mark.asyncio
async def test_every_save_publishes_under_the_vault_lock(fixtures) -> None:
    """A sync's merge landing between a save's last check and its publish would
    be replaced by the publish with a 200: the tokenless path checks nothing on
    its first attempt, and the guarded path's freshness check has already
    passed. So both publish under the vault write lock -- the tokenless
    transaction holds it from staging through publication, the guarded one
    re-takes it around each freshness-check-and-publish."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        lock = _mod.vault_write_lock(vault["localPath"])
        held_at_publish: list[bool] = []
        real_publish = _mod._publish_staged_sync

        def observing_publish(tmp, path):  # type: ignore[no-untyped-def]
            held_at_publish.append(lock.locked())
            real_publish(tmp, path)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_mod, "_publish_staged_sync", observing_publish)
            status, body = await client.put("/api/note", {"path": "One.md", "content": "tokenless"})
            assert status == 200, body
            _, read = await client.get("/api/note?path=One.md")
            status, body = await client.put(
                "/api/note",
                {"path": "One.md", "content": "guarded", "baseMtime": read["mtime"]},
            )
            assert status == 200, body
        # First publish: tokenless. Second: guarded. Both under the lock.
        assert held_at_publish == [True, True], held_at_publish


@pytest.mark.asyncio
async def test_a_failed_save_leaves_the_previous_note_intact(fixtures) -> None:
    """A write that dies partway must not destroy what was already there.

    `write_text` truncates before writing, so a full disk would leave the note
    empty or half-written. The temp-then-rename path keeps the old bytes until
    the new ones are completely on disk.
    """
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        note = root / "keep.md"
        note.write_text("# original content\n", "utf-8")

        boom = OSError("No space left on device")
        original = note.read_text("utf-8")

        # A write that dies after opening the temp file: the original must be
        # untouched and no temp left behind to be mistaken for a note.
        with pytest.MonkeyPatch.context() as mp:
            real_open = open

            def exploding_open(*args, **kwargs):  # type: ignore[no-untyped-def]
                fh = real_open(*args, **kwargs)
                fh.write("partial")
                raise boom

            mp.setattr("builtins.open", exploding_open)
            with pytest.raises(OSError):
                server_mod._atomic_write_text_sync(note, "replacement")

        assert note.read_text("utf-8") == original
        assert not (root / "keep.md.tmp").exists()


@pytest.mark.asyncio
async def test_a_contended_rename_does_not_lose_the_save(fixtures) -> None:
    """A vault is a directory other processes hold open; a save must survive it.

    On Windows ``os.replace`` raises ``PermissionError`` while ANY other handle is
    open on either path. For a note that is expected rather than exotic: these are
    Obsidian vaults, so the user's editor is often holding the file, and Windows
    Search and AV both touch a freshly written one. A bare rename turns that
    transient into a failed save -- the user loses what they just typed.

    The other half of the contract is pinned by the test below: when the
    contention coincides with a real external edit, the retry must yield to it
    instead of publishing. Here nothing else changed the file, so the save is
    simply completed.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        _, read = await client.get("/api/note?path=One.md")
        target = Path(vault["localPath"]) / "One.md"

        attempts: list[int] = []
        real_replace = os.replace

        def contended_once(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
            attempts.append(1)
            if len(attempts) == 1:
                # WinError 32, the sharing violation this retry exists for --
                # and no external edit, so the file is unchanged between attempts.
                raise PermissionError(32, "The process cannot access the file")
            return real_replace(src, dst, *args, **kwargs)

        with pytest.MonkeyPatch.context() as mp:
            # The retry is Windows-only and backs off; select that branch on every
            # platform and zero the wait so it costs no wall clock.
            mp.setattr(_mod.platform_compat, "IS_WINDOWS", True)
            mp.setattr(_mod, "_SAVE_BACKOFF_SECONDS", 0.0)
            mp.setattr(_mod.os, "replace", contended_once)
            status, body = await client.put(
                "/api/note",
                {"path": "One.md", "content": "mine", "baseMtime": read["mtime"]},
            )

        assert status == 200, f"a transient sharing violation lost the save: {body}"
        assert target.read_text(encoding="utf-8") == "mine"
        assert len(attempts) == 2, f"expected one retry, saw {len(attempts)} attempt(s)"
        assert not list(target.parent.glob("One.md.*.tmp")), "a temp file was left behind"


@pytest.mark.asyncio
async def test_a_contended_save_leaves_no_temp_behind(fixtures) -> None:
    """A successful save must not leave a generated temp in the vault.

    The cleanup after a refused rename is best-effort: if the same handle that
    blocked the rename also blocks the unlink, the temp survives. Staging a FRESH
    temp per attempt then meant a contended save could answer 200 with an orphan
    ``One.md.<uuid>.tmp`` still sitting in the vault -- and nothing would tell the
    user to look, because the save reported success.

    That orphan is not inert. A user-initiated Sync stages every change
    ``status()`` reports; only the unattended autosave is narrowed to ``.md``. So
    the generated temp reaches a commit, and a push puts it on the remote.

    One temp per save transaction removes the failure mode rather than cleaning
    up after it: the same staged file is republished by each attempt, so success
    consumes it.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        target = root / "One.md"
        _, read = await client.get("/api/note?path=One.md")

        renames: list[str] = []
        real_replace = os.replace
        real_unlink = os.unlink

        def contended_once(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
            renames.append(str(src))
            if len(renames) == 1:
                raise PermissionError(32, "The process cannot access the file")
            return real_replace(src, dst, *args, **kwargs)

        def unlink_refused(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            # The handle that blocked the rename blocks the cleanup too, which is
            # the whole reason an orphan can survive.
            if str(path).endswith(".tmp"):
                raise PermissionError(32, "The process cannot access the file")
            return real_unlink(path, *args, **kwargs)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_mod.platform_compat, "IS_WINDOWS", True)
            mp.setattr(_mod, "_SAVE_BACKOFF_SECONDS", 0.0)
            mp.setattr(_mod.os, "replace", contended_once)
            mp.setattr(_mod.os, "unlink", unlink_refused)
            status, body = await client.put(
                "/api/note",
                {"path": "One.md", "content": "mine", "baseMtime": read["mtime"]},
            )

        assert status == 200, f"the contended save did not succeed: {body}"
        assert target.read_text(encoding="utf-8") == "mine"

        leftovers = sorted(p.name for p in root.glob("One.md.*.tmp"))
        assert not leftovers, (
            "a save answered 200 while leaving generated temp(s) in the vault, "
            f"which a user Sync would commit: {leftovers}"
        )
        # The retry republished the SAME staged file rather than staging another.
        assert len(set(renames)) == 1, f"each attempt staged its own temp: {sorted(set(renames))}"


@pytest.mark.asyncio
async def test_a_sync_during_the_retry_window_never_commits_the_staged_temp(
    fixtures,
) -> None:
    """A Sync running while a save is between attempts must not commit its temp.

    The save stages content into a sibling temp and renames it over the note. The
    temp is a real untracked file in the user's vault for the whole of that gap,
    and a contended rename stretches the gap across the entire retry budget. A
    user-initiated Sync stages every change ``status()`` reports -- only the
    unattended autosave is narrowed to ``.md`` -- so in that window a second tab
    hitting Sync commits a half-published implementation detail and the push puts
    it on the remote.

    Shortening the window cannot fix this: it is never zero, and the cleanup
    unlink is best-effort precisely because the handle that refused the rename
    commonly refuses it too. ``git_ops.status`` therefore recognises the shape
    ``staged_temp_name`` produces and refuses to report it as an addition.

    Deterministic, never raced: the Sync runs INSIDE the parked backoff, so it is
    guaranteed to land between attempt 1 and attempt 2, and the vault carries a
    genuine non-note change so a Sync that silently did nothing cannot pass.
    """
    _mod, remote, _seed = fixtures
    real_asyncio = asyncio

    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        target = root / "One.md"
        before = target.read_text(encoding="utf-8")
        _, read = await client.get("/api/note?path=One.md")

        # A real user change the whole-scope Sync MUST commit. Without it a Sync
        # that no-opped would satisfy every assertion below.
        (root / "attachment.png").write_bytes(bytes([0x89]) + b"PNG")

        parked = asyncio.Event()
        resume = asyncio.Event()
        parkings: list[int] = []
        attempts: list[str] = []
        real_replace = os.replace

        def contended_once(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
            # Scoped to THIS note's publish. `os` is a shared module object, so
            # the patch is global: the Sync driven from inside the retry gap runs
            # git and rebuilds the cache, and those renames must neither absorb
            # the injected failure nor be counted as attempts.
            if str(dst).endswith("One.md"):
                attempts.append(str(src))
                if len(attempts) == 1:
                    raise PermissionError(32, "The process cannot access the file")
            return real_replace(src, dst, *args, **kwargs)

        class _GatedAsyncio:
            """The real asyncio with the FIRST backoff parked on an event.

            Only the first: the Sync driven from inside that gap runs through the
            same module reference, and parking its waits too would deadlock the
            test rather than test anything.
            """

            async def sleep(self, delay, *args, **kwargs):  # type: ignore[no-untyped-def]
                parkings.append(1)
                if len(parkings) == 1:
                    parked.set()
                    # flake-ok: pre-existing unbounded Event wait inside the fake; the test sets the event itself, moved verbatim; tracked for a de-flake pass
                    await resume.wait()
                    return
                await real_asyncio.sleep(delay, *args, **kwargs)

            def __getattr__(self, name):  # type: ignore[no-untyped-def]
                return getattr(real_asyncio, name)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_mod.platform_compat, "IS_WINDOWS", True)
            mp.setattr(_mod.os, "replace", contended_once)
            mp.setattr(_mod, "asyncio", _GatedAsyncio())

            save = real_asyncio.create_task(
                client.put(
                    "/api/note",
                    {"path": "One.md", "content": "mine", "baseMtime": read["mtime"]},
                )
            )
            await real_asyncio.wait_for(parked.wait(), timeout=5)

            # The exposure is real and present RIGHT NOW: the temp is on disk and
            # git reports it as untracked. Asserting it keeps the rest of this
            # test from passing because nothing was ever staged.
            staged = [q.name for q in root.glob("One.md.*.tmp")]
            assert len(staged) == 1, f"expected one staged temp, saw {staged}"
            porcelain = _git("status", "--porcelain", "--untracked-files=all", cwd=root)
            assert (
                staged[0] in porcelain
            ), f"git cannot see the temp, so this proves nothing: {porcelain!r}"

            # A second tab hits Sync while the first save backs off.
            sync_status, sync_body = await client.post("/api/sync")
            assert sync_status == 200, sync_body
            committed = [c["path"] for c in sync_body["result"]["committed"]]

            resume.set()
            save_status, save_body = await real_asyncio.wait_for(save, timeout=5)

    assert committed == [
        "attachment.png"
    ], f"a Sync inside the retry backoff staged a generated temp: {committed}"
    assert staged[0] not in _git("ls-files", cwd=root), "the temp entered local history"
    assert staged[0] not in _git(
        "ls-tree", "-r", "--name-only", "HEAD", cwd=Path(remote)
    ), "the temp was pushed to the remote"

    # The Sync committed against the note as it stood BEFORE the retry finished,
    # and the save still lands afterwards: the filter costs the save nothing.
    assert _git("show", "HEAD:One.md", cwd=root) == before.rstrip()
    assert save_status == 200, f"the contended save was lost: {save_body}"
    assert target.read_text(encoding="utf-8") == "mine"
    assert len(attempts) == 2, f"expected one retry, saw {len(attempts)} attempt(s)"
    # Both attempts republished the one staged temp the Sync just declined to see.
    assert set(attempts) == {str(root / staged[0])}, f"the temp changed: {attempts}"
    assert not list(root.glob("One.md.*.tmp")), "a temp survived a successful save"


async def _parked_behind(lock: LoopBoundLock, contender: asyncio.Task, what: str) -> None:
    """Return once ``contender`` is parked on ``lock``, the barrier a "still waiting" check needs.

    Without it a contender that has not yet REACHED the lock satisfies the negative as
    well as one the lock is holding back, so the check passes with the lock removed. A
    contender that finishes instead of parking fails here by name; one that never
    parks fails at ``until_parked``'s deadline, quoting how many waiters it saw.
    """
    parked = asyncio.ensure_future(until_parked(lock, describe=lambda: what))
    try:
        # flake-ok: pre-existing asyncio.wait with no timeout; until_parked's own deadline bounds it, moved verbatim; tracked for a de-flake pass
        await asyncio.wait({parked, contender}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        if not parked.done():
            parked.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                # flake-ok: pre-existing unbounded await of the probe this helper just cancelled, moved verbatim; tracked for a de-flake pass
                await parked
    assert not contender.done(), f"{what} finished without ever waiting on the lock"
    parked.result()


@pytest.mark.asyncio
async def test_a_sync_while_the_temp_is_still_staging_never_commits_it(
    fixtures,
) -> None:
    """A Sync started while a note is still staging waits for the staging.

    The retry-window test above begins only after staging returns. A large note
    exposes an earlier window: ``open`` creates the untracked temp, then the
    worker can spend arbitrarily long writing and fsyncing it before returning
    to the coroutine that registers it. The staging call holds the vault write
    lock (`vault_write_lock`) and every Sync takes the same lock for its whole
    run, so a Sync arriving in that interval does not run beside the staging
    worker: it parks until the staging call returns, and the tree it then
    commits and merges is one the save has already moved on from. The
    registration (`inflight_temp`) stays the guard for the retry window, where
    the lock is released between attempts.

    Deterministic: the staging worker writes the real temp and parks BEFORE it
    returns. The Sync is requested while the file is definitely present and the
    save caller is definitely still awaiting staging — and is shown NOT to
    complete until the worker is released.
    """
    _mod, remote, _seed = fixtures
    real_asyncio = asyncio

    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        target = root / "One.md"
        _, read = await client.get("/api/note?path=One.md")

        # Prove this is a real whole-scope Sync, not a no-op that happens to
        # report no generated temp.
        (root / "attachment.png").write_bytes(bytes([0x89]) + b"PNG")

        temp_written = threading.Event()
        release_stage = threading.Event()
        staged: list[Path] = []
        real_stage = _mod._stage_note_text_sync

        def parked_stage(tmp, content):  # type: ignore[no-untyped-def]
            real_stage(tmp, content)
            staged_path = Path(tmp)
            # Sync records lastSync through the same generic atomic writer.
            # Park only this note; delaying settings.json would deadlock the
            # request that is meant to inspect the note temp.
            if staged_path.parent == root and staged_path.name.startswith("One.md."):
                staged.append(staged_path)
                temp_written.set()
                # Not a synchronisation point -- `release_stage.set()` always
                # runs in the `finally` below regardless of how long the Sync
                # or the save's own retries take. This wait is a fail-safe
                # against a genuine deadlock, not a race the test should ever
                # actually run out on, so its budget is generous rather than
                # tight: a loaded CI host's shared `to_thread` executor can
                # make the Sync's own worker-thread git calls queue behind
                # this parked slot, stretching how long `finally` takes to
                # reach `release_stage.set()` well past a tight budget (flake
                # class 2, see testing-conventions.md).
                if not release_stage.wait(timeout=120):
                    raise AssertionError("test never released the staging worker")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_mod, "_stage_note_text_sync", parked_stage)
            save = real_asyncio.create_task(
                client.put(
                    "/api/note",
                    {"path": "One.md", "content": "mine", "baseMtime": read["mtime"]},
                )
            )

            save_status = 0
            save_body: dict[str, Any] = {}
            sync_status = 0
            sync_body: dict[str, Any] = {}
            sync: Optional[asyncio.Task[tuple[int, Any]]] = None
            try:
                # flake-ok: pre-existing 10 s thread-pool wait for the staging write the test gates, failing by name; moved verbatim
                assert await real_asyncio.to_thread(
                    temp_written.wait, 10
                ), "the save never created its staged temp"
                assert len(staged) == 1 and staged[0].is_file(), staged
                porcelain = _git("status", "--porcelain", "--untracked-files=all", cwd=root)
                assert staged[0].name in porcelain, (
                    "git cannot see the staged temp, so this proves nothing: " f"{porcelain!r}"
                )

                # Requested while staging is parked: the Sync must wait on the
                # vault write lock the staging call holds, not run beside it. It is
                # seen parked there first: a Sync that has not reached the lock yet
                # would pass this check as well, lock or no lock.
                sync = real_asyncio.create_task(client.post("/api/sync"))
                await _parked_behind(_mod.vault_write_lock(vault["localPath"]), sync, "the Sync")
                assert not sync.done(), "the Sync ran while a note was still staging"
            finally:
                release_stage.set()
                # After staging releases the vault lock, the sync that was
                # parked behind it acquires the lock first and runs all git
                # operations; save cannot re-acquire the lock to publish
                # until sync releases it.  Drain sync before awaiting save
                # so the save-await is not racing git work on a slow runner.
                if sync is not None:
                    sync_status, sync_body = await real_asyncio.wait_for(sync, timeout=30)
                save_status, save_body = await real_asyncio.wait_for(save, timeout=10)
            assert sync is not None
            assert sync_status == 200, sync_body
            committed = [c["path"] for c in sync_body["result"]["committed"]]

    # The Sync ran AFTER the save published, so it commits exactly the
    # attachment on disk and never the temp, which the publish consumed.
    # committed is sorted, so the exact list is stable and bounds the committed
    # breadth (a stray extra path is a staging regression the ratchet must catch).
    assert committed == ["attachment.png"], committed
    assert (
        staged[0].name not in committed
    ), f"a Sync while staging committed a generated temp: {committed}"
    assert staged[0].name not in _git(
        "ls-files", cwd=root
    ), "the staging temp entered local history"
    assert staged[0].name not in _git(
        "ls-tree", "-r", "--name-only", "HEAD", cwd=Path(remote)
    ), "the staging temp was pushed to the remote"
    assert save_status == 200, f"the parked save failed: {save_body}"
    assert target.read_text(encoding="utf-8") == "mine"
    assert not staged[0].exists(), "the temp survived its successful publish"


@pytest.mark.asyncio
async def test_an_external_edit_during_the_retry_window_still_wins(fixtures) -> None:
    """Retrying a contended save must not defeat the stale-write guard.

    The save is an optimistic check-and-write: the client sends the mtime it
    read, the server re-stats under the lock, and an external edit since that read is a
    409 ESTALE rather than a silent overwrite.

    Retrying only the RENAME would stretch the gap between that check and the
    publish across the whole backoff, so an external edit completing inside the
    gap would be overwritten by the next attempt — the guard defeated by the very
    mechanism meant to make saves reliable. The retry therefore re-runs the
    freshness check before each attempt.

    Ordering is forced, not raced: the external write happens INSIDE the injected
    rename failure, so it is guaranteed to land after attempt 1 and before
    attempt 2's re-check.
    """
    _mod, remote, _seed = fixtures
    external = "# One\n\nchanged by another program\n"

    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        _, read = await client.get("/api/note?path=One.md")
        target = Path(vault["localPath"]) / "One.md"

        attempts: list[int] = []
        real_replace = os.replace

        def contended_then_external(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
            attempts.append(1)
            if len(attempts) == 1:
                # Another program finishes its write while we are between
                # attempts. Its mtime is pinned forward rather than slept for:
                # two writes can share one filesystem tick, which would make the
                # guard's comparison a coin flip instead of the thing under test.
                target.write_text(external, encoding="utf-8")
                future = target.stat().st_mtime + 5
                os.utime(target, (future, future))
                raise PermissionError(32, "The process cannot access the file")
            return real_replace(src, dst, *args, **kwargs)

        from kiro_crew import atomic_write as aw

        with pytest.MonkeyPatch.context() as mp:
            # Both backoff knobs are zeroed, both with raising=False, so this test
            # asserts the CONTRACT rather than where the retry happens to live.
            # That is what makes it a valid negative control: against a build that
            # retries inside the rename it still runs, and fails because the
            # external edit was published over -- not because a constant was
            # missing from the module.
            mp.setattr(_mod.platform_compat, "IS_WINDOWS", True)
            mp.setattr(aw.platform_compat, "IS_WINDOWS", True)
            mp.setattr(_mod, "_SAVE_BACKOFF_SECONDS", 0.0, raising=False)
            mp.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0.0, raising=False)
            mp.setattr(_mod.os, "replace", contended_then_external)
            status, body = await client.put(
                "/api/note",
                {"path": "One.md", "content": "mine", "baseMtime": read["mtime"]},
            )

        assert status == 409, f"the external edit was not detected: {status} {body}"
        assert body["code"] == "ESTALE"
        assert (
            target.read_text(encoding="utf-8") == external
        ), "the retry published over an edit made during its own backoff"
        assert "mine" not in target.read_text(encoding="utf-8")
        # The second attempt must never have REACHED the rename: a re-check that
        # runs first turns it into the conflict above. A build that retries the
        # rename alone shows 2 here and has already overwritten the file.
        assert (
            len(attempts) == 1
        ), f"a second publish attempt ran without revalidating: {len(attempts)} attempts"


@pytest.mark.asyncio
async def test_a_retrying_save_without_basemtime_cannot_overwrite_a_later_one(
    fixtures,
) -> None:
    """A contended save must not resurrect itself over a save that already won.

    ``baseMtime`` is optional (``saveNote(..., baseMtime?: number)``) and
    ``_assert_note_is_fresh`` is a no-op without it, so for an unguarded save a
    writer arriving during the retry backoff is invisible to every later attempt.
    Releasing the per-note lock across that backoff therefore let an earlier
    contended save republish over a later one that had already been acknowledged
    with 200 -- rename order ``['A', 'B', 'A']``, final content ``A``.

    Without a token there is nothing to detect the interleave with, so ordering
    is preserved instead: the lock is held across the backoff, which is exactly
    the serialization the un-retried code had.

    Ordering is forced with events and a structural assertion on the lock, never
    slept for.
    """
    _mod, remote, _seed = fixtures
    real_asyncio = asyncio

    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        target = Path(vault["localPath"]) / "One.md"

        a_parked = asyncio.Event()
        release_a = asyncio.Event()
        renames: list[str] = []
        real_replace = os.replace

        def one_contention(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
            body = ""
            try:
                body = Path(src).read_text(encoding="utf-8")
            except OSError:
                pass
            renames.append(body)
            if len(renames) == 1:
                raise PermissionError(32, "The process cannot access the file")
            return real_replace(src, dst, *args, **kwargs)

        class _GatedAsyncio:
            """The real asyncio with the retry backoff parked on an event.

            Rebinding the module reference the server holds keeps the
            substitution to this one call site; every other attribute
            (``to_thread``, ``Lock``) proxies through untouched.
            """

            async def sleep(self, _delay, *args, **kwargs):  # type: ignore[no-untyped-def]
                # Park only the FIRST backoff -- A's -- which is what pins the
                # ordering. Later backoffs delegate to a real short wait: after A
                # returns, api_note_save re-reads every note in the vault to
                # recompute backlinks, OUTSIDE the per-note lock, so B's rename
                # can hit a genuine Windows sharing violation there. Production
                # absorbs that with the backoff; collapsing it to zero would burn
                # B's whole budget inside A's read window and make this test fail
                # on an artifact of its own instrumentation.
                if not release_a.is_set():
                    a_parked.set()
                    # flake-ok: pre-existing unbounded Event wait inside the fake; the test sets the event itself, moved verbatim; tracked for a de-flake pass
                    await release_a.wait()
                    return
                # flake-ok: pre-existing 10 ms delegated real backoff inside the gated asyncio fake, moved verbatim; tracked for a de-flake pass
                await real_asyncio.sleep(0.01)

            def __getattr__(self, name):  # type: ignore[no-untyped-def]
                return getattr(real_asyncio, name)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_mod.platform_compat, "IS_WINDOWS", True)
            mp.setattr(_mod.os, "replace", one_contention)
            mp.setattr(_mod, "asyncio", _GatedAsyncio())

            task_a = real_asyncio.create_task(
                client.put("/api/note", {"path": "One.md", "content": "A"})
            )
            await real_asyncio.wait_for(a_parked.wait(), timeout=5)

            # A is mid-retry with no freshness token. The note's lock must still
            # be held, so a second save cannot slip in behind it.
            assert _mod._save_lock(
                str(target)
            ).locked(), "the per-note lock was released during an unguarded retry"

            try:
                task_b = real_asyncio.create_task(
                    client.put("/api/note", {"path": "One.md", "content": "B"})
                )
                # B is seen parked on the note's lock before the check: a B whose
                # request has not reached the lock yet would pass it as well, lock
                # or no lock.
                await _parked_behind(_mod._save_lock(str(target)), task_b, "the later save")
                assert not task_b.done(), "a later save interleaved into the retry gap"
            finally:
                release_a.set()
            status_a, body_a = await real_asyncio.wait_for(task_a, timeout=5)
            status_b, body_b = await real_asyncio.wait_for(task_b, timeout=5)

        assert status_a == 200, f"the earlier save failed: {body_a}"
        assert status_b == 200, f"the later save failed: {body_b} renames={renames}"
        # The invariant is ordering, not a retry count: every A attempt precedes
        # every B attempt, so the earlier save can never republish after the later
        # one. An exact count would encode how many times B happened to retry,
        # which depends on a real collision with A post-save vault read.
        assert renames == sorted(
            renames, key=lambda body: body != "A"
        ), f"an A attempt ran after a B attempt: {renames}"
        assert "A" in renames and "B" in renames, f"both saves must publish: {renames}"
        assert (
            target.read_text(encoding="utf-8") == "B"
        ), "an earlier contended save resurrected itself over a later one"


@pytest.mark.asyncio
async def test_a_tokenless_save_stages_inside_the_note_lock(fixtures) -> None:
    """Staging is the slow half of the transaction, so it must be serialized too.

    Without a ``baseMtime`` nothing can DETECT an interleaved writer, so order
    is the only guarantee left -- and order has to cover stage-and-publish, not
    just publish. Staging writes the entire note, so it is exactly where a large
    save is slow: with staging outside the lock, a later save could take the
    lock, publish and answer 200 while the earlier one was still writing its
    temp, and the earlier one then published over it. Both answered 200 and the
    later edit was gone.

    No contention is injected here: this is not about a refused rename. The
    parking point is staging itself, and the assertion is structural -- the
    note's lock is held while A stages, so B cannot start.
    """
    _mod, remote, _seed = fixtures
    real_asyncio = asyncio

    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        target = Path(vault["localPath"]) / "One.md"

        a_staging = threading.Event()
        release_a = threading.Event()
        staged: list[str] = []
        real_stage = _mod._stage_note_text_sync

        def parked_stage(tmp, content):  # type: ignore[no-untyped-def]
            # Runs on a worker thread (`asyncio.to_thread`), so the gate is a
            # threading.Event, not an asyncio one.
            staged.append(content)
            if len(staged) == 1:
                a_staging.set()
                release_a.wait(timeout=10)
            return real_stage(tmp, content)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_mod, "_stage_note_text_sync", parked_stage)

            task_a = real_asyncio.create_task(
                client.put("/api/note", {"path": "One.md", "content": "A"})
            )
            # flake-ok: pre-existing 10 s thread-pool wait for the staging write the test gates, failing by name; moved verbatim
            assert await real_asyncio.to_thread(
                a_staging.wait, 10
            ), "the first save never reached staging"

            # The structural claim: A is only STAGING, has published nothing, and
            # already holds the note's lock.
            assert _mod._save_lock(
                str(target)
            ).locked(), "a tokenless save staged its temp outside the per-note lock"

            try:
                task_b = real_asyncio.create_task(
                    client.put("/api/note", {"path": "One.md", "content": "B"})
                )
                # B is seen parked on the note's lock before the checks: a B whose
                # request has not reached the lock yet would pass them as well, lock
                # or no lock.
                await _parked_behind(_mod._save_lock(str(target)), task_b, "the later save")
                assert staged == [
                    "A"
                ], f"a later save staged while the earlier one held the lock: {staged}"
                assert not task_b.done(), "a later save completed while the earlier one was staging"
            finally:
                release_a.set()
            status_a, body_a = await real_asyncio.wait_for(task_a, timeout=10)
            status_b, body_b = await real_asyncio.wait_for(task_b, timeout=10)

    assert status_a == 200, f"the earlier save failed: {body_a}"
    assert status_b == 200, f"the later save failed: {body_b}"
    assert staged == ["A", "B"], f"staging order was not preserved: {staged}"
    assert (
        target.read_text(encoding="utf-8") == "B"
    ), "the earlier save published over the later one"


@pytest.mark.asyncio
async def test_the_vault_registry_write_also_retries(fixtures) -> None:
    """The registry is re-read by every later request, so it has the same window."""
    from kiro_crew import atomic_write as aw

    server_mod, _remote, _seed = fixtures

    attempts: list[int] = []
    real_replace = os.replace

    def contended_once(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
        attempts.append(1)
        if len(attempts) == 1:
            raise PermissionError(32, "The process cannot access the file")
        return real_replace(src, dst, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(aw.platform_compat, "IS_WINDOWS", True)
        mp.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0.0)
        mp.setattr(aw.os, "replace", contended_once)
        await asyncio.to_thread(server_mod._write_vaults_sync, [{"id": "v1"}])

    assert json.loads(server_mod._vaults_json().read_text("utf-8")) == [{"id": "v1"}]
    assert len(attempts) == 2, f"expected one retry, saw {len(attempts)} attempt(s)"


# -- status() hides a temp only on proven ownership -------------------------
#
# `status()` may hide a path on ONE ground: this process currently knows it owns
# the temp. The name shape is not ownership evidence -- `<name>.<32 hex>.tmp` is
# what `staged_temp_name` produces, not a shape only it can produce -- so the
# cases below pin both directions.

NL = chr(10)


HEX32 = "0123456789abcdef0123456789abcdef"


def _seed_vault(tmp_path: Path, *files: str) -> Path:
    repo = tmp_path / "vault"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    for name in files:
        (repo / name).write_text("seeded " + name + NL, encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "seed")
    return repo


@pytest.mark.asyncio
async def test_a_genuine_user_file_in_the_temp_shape_stays_visible(
    tmp_path: Path,
) -> None:
    """A user file NAMED like a staged temp is still the user's file.

    `<name>.<32 hex>.tmp` is the shape this module produces; nothing stops a
    user or another tool from producing it too. Hiding it on the shape alone
    withheld real content from the user's own repository -- absent from the
    pending badge, from the commit, and from the remote, with nothing to say it
    had been dropped. Withholding unknown filesystem content is a worse failure
    than committing a stray temp, so shape alone must not hide anything.
    """
    repo = _seed_vault(tmp_path, "Report.md")
    genuine = "Report.md." + HEX32 + ".tmp"
    (repo / genuine).write_text("a real file the user made" + NL, encoding="utf-8")

    # The origin is untouched: nothing here looks like a rename.
    assert (repo / "Report.md").exists()

    kinds = {c.path: c.kind for c in await git_ops.status(str(repo))}
    assert kinds.get(genuine) == "added", (
        "status() hid a genuine user file because its NAME matched the staged-temp "
        "shape, so it would never be committed or pushed: %r" % (kinds,)
    )


@pytest.mark.asyncio
async def test_a_registered_in_flight_temp_is_hidden(tmp_path: Path) -> None:
    """The one ground for hiding a path: this process owns it right now."""
    repo = _seed_vault(tmp_path, "Note.md")
    tmp_name = git_ops.staged_temp_name("Note.md")
    (repo / tmp_name).write_text("in flight" + NL, encoding="utf-8")

    with git_ops.inflight_temp(repo / tmp_name):
        paths = {c.path for c in await git_ops.status(str(repo))}
        assert tmp_name not in paths, "a temp this process is publishing reached status(): %r" % (
            paths,
        )

    # Ownership ended, so the file is ordinary content again rather than being
    # withheld forever. An orphan becoming visible is the safe failure direction.
    paths = {c.path for c in await git_ops.status(str(repo))}
    assert tmp_name in paths, (
        "a temp stayed hidden after its transaction ended, so an orphan could "
        "never be seen or cleaned by the user: %r" % (paths,)
    )


@pytest.mark.asyncio
async def test_a_rename_into_the_temp_shape_commits_both_halves(
    tmp_path: Path,
) -> None:
    """A tracked file renamed into the temp shape must not lose either half.

    The rename does not arrive as git's `R` record: the renamed file is
    untracked, so `diff HEAD` reports only the old path as deleted and the new
    path turns up in `ls-files --others`. Dropping the addition on shape left
    the deletion standing alone, and the autosave committed the move as a
    REMOVAL -- the file disappears from the remote while still sitting in the
    vault under its new name.
    """
    repo = _seed_vault(tmp_path, "Note.md")
    renamed_name = git_ops.staged_temp_name("Note.md")
    (repo / "Note.md").rename(repo / renamed_name)

    kinds = {c.path: c.kind for c in await git_ops.status(str(repo))}
    assert (
        kinds.get("Note.md") == "deleted"
    ), "the rename's deleted half vanished from status(): %r" % (kinds,)
    assert kinds.get(renamed_name) == "added", (
        "status() dropped the rename's ADDED half while keeping its deletion, so "
        "a Sync would commit this move as a removal: %r" % (kinds,)
    )


@pytest.mark.asyncio
async def test_a_tokenless_retry_refuses_to_overwrite_an_external_edit(
    fixtures, tmp_path: Path
) -> None:
    """A save with no ``baseMtime`` must not republish over an edit made in its gap.

    ``_assert_note_is_fresh`` is a no-op without a client token, and the note
    lock only serializes API writers -- an editor writing the file directly
    (Obsidian, ``git pull``, a script) never takes it. So a contended rename that
    backs off and retries could rename over whatever landed in the meantime and
    answer 200, destroying a newer edit with no conflict reported.

    The server therefore samples its OWN baseline before the first attempt and
    revalidates against it before every retry. Deterministic: the external write
    is performed from inside the injected backoff, so it is guaranteed to land
    between attempt 1 and attempt 2.
    """
    _mod, _remote, _seed = fixtures

    root = tmp_path / "tokenless-vault"
    root.mkdir()
    target = root / "One.md"
    target.write_text("original" + NL, encoding="utf-8")

    real_replace = os.replace
    real_sleep = asyncio.sleep
    attempts: list[str] = []

    def contended_once(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(dst).endswith("One.md"):
            attempts.append(str(src))
            if len(attempts) == 1:
                raise PermissionError(32, "The process cannot access the file")
        return real_replace(src, dst, *args, **kwargs)

    async def external_write_during_backoff(delay):  # type: ignore[no-untyped-def]
        # THE external editor: lands in the retry gap, holds no lock, and leaves
        # a strictly newer mtime.
        if attempts:
            target.write_text("edited in obsidian" + NL, encoding="utf-8")
            future = time.time() + 5
            os.utime(target, (future, future))
        await real_sleep(0)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "replace", contended_once)
        mp.setattr(_mod.platform_compat, "IS_WINDOWS", True)
        # flake-ok: pre-existing patch of the stdlib asyncio.sleep the server reads, undone when this MonkeyPatch.context exits; moved verbatim; tracked for a de-flake pass
        mp.setattr(_mod.asyncio, "sleep", external_write_during_backoff)

        with pytest.raises(_mod.ApiError) as excinfo:
            await _mod._save_note_contents(
                {"localPath": str(root)}, target, "One.md", "my save" + NL, None
            )

    assert excinfo.value.status == 409, "a tokenless retry answered %r instead of a conflict" % (
        excinfo.value.status,
    )
    assert target.read_text(encoding="utf-8") == "edited in obsidian" + NL, (
        "the tokenless retry republished over an external edit made during its "
        "backoff -- the edit is gone and the client was never told"
    )
    leftovers = sorted(p.name for p in root.glob("One.md.*.tmp"))
    assert not leftovers, "the refused save left its temp behind: %r" % (leftovers,)
