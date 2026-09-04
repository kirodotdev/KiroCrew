"""Per-folder icon epoch on ARTIFACT folders (issue #7991).

The chat-folder subsystem closed three stale-write-back races with a per-folder
icon epoch (``_CHAT_FOLDER_ICON_EPOCHS``, PR #7353). Artifact folders guarded
their async icon write-back with a bare ``fstore.exists()`` check, which only
catches deletion — these tests pin the ported guard:

* a **manual icon set** while generation is in flight is not clobbered,
* an **icon clear** mid-generation is not overwritten (the value would be
  absent -> absent, so no value-pin could catch it),
* a **rename** mid-generation drops the icon derived from the old name,
* the epoch entry is popped only after a **confirmed** delete, so a failed
  store write leaves the guard armed instead of resetting it to 0.

Mirrors ``test/test_chat_folder_icons.py``. The store-layer class exercises
:class:`ArtifactFolderStore` directly (``test_artifact_folders.py`` fixture
style); the handler-layer class drives the real HTTP handlers through the
``stores`` + ``_request`` shape of ``test_artifact_folder_handlers.py``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import artifacts as art_mod
from kiro_crew.artifacts import ArtifactFolderStore, ArtifactStore
from kiro_crew.dashboard.handlers import artifacts as art_handlers
from kiro_crew.dashboard.handlers.artifacts import (
    api_artifact_folder_create,
    api_artifact_folder_delete,
    api_artifact_folder_update,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def fstore(tmp_path: Path) -> ArtifactFolderStore:
    return ArtifactFolderStore(path=tmp_path / "artifact_folders.json")


@pytest.fixture
def stores(tmp_path: Path, monkeypatch):
    """Isolated artifact + folder stores wired into the module globals."""
    store = ArtifactStore(root=tmp_path / "artifacts")
    folders = ArtifactFolderStore(path=tmp_path / "artifact_folders.json")
    monkeypatch.setattr(art_mod, "_default_store", store)
    monkeypatch.setattr(art_mod, "_default_folder_store", folders)
    return store, folders


@pytest.fixture
def patch_restricted(monkeypatch):
    def _stub(_state, req) -> bool:
        return req.app.get("_restricted_session", False)

    monkeypatch.setattr(art_handlers, "_is_restricted_session", _stub)


def _request(
    *,
    body: dict | None = None,
    match: dict | None = None,
    query: dict | None = None,
) -> MagicMock:
    req = MagicMock()
    req.headers = {"X-Session-Key": "dashboard:test"}
    req.match_info = match or {}
    req.query = query or {}
    encoded = json.dumps(body).encode() if isinstance(body, dict) else b""
    req.read = AsyncMock(return_value=encoded)
    req.app = {"state": MagicMock(), "_restricted_session": False}
    return req


def _body(resp) -> dict:
    return json.loads(resp.body)


def _epoch(store: ArtifactFolderStore, folder_id: str) -> int:
    """Read a folder's icon epoch for assertions.

    Local to the tests on purpose: the store exposes no public accessor, because
    no production caller may READ the epoch back -- create pins 0 and rename
    returns its own. Shipping a getter for the tests' benefit would be a method
    with zero production callers.
    """
    return store._icon_epochs.get(folder_id, 0)


async def _drain_icon_tasks() -> None:
    """Wait for this test's in-flight icon write-backs before asserting.

    ``_ARTIFACT_FOLDER_ICON_TASKS`` is a module-global set that every test
    touching the create handler populates, so it can hold tasks belonging to
    an EARLIER test's event loop -- awaiting one of those raises
    ``ValueError: The future belongs to a different loop``. Filtering on the
    running loop selects exactly the tasks this test created, because each
    test gets its own loop. ``return_exceptions`` keeps a background task's
    own failure from surfacing as a drain error; the assertions that follow
    are what judge the outcome.
    """
    loop = asyncio.get_running_loop()
    mine = [
        t
        for t in list(art_handlers._ARTIFACT_FOLDER_ICON_TASKS)
        if t.get_loop() is loop and not t.done()
    ]
    if mine:
        await asyncio.gather(*mine, return_exceptions=True)


@asynccontextmanager
async def _held_generation(
    gen: Callable[[asyncio.Event], Any],
) -> AsyncIterator[asyncio.Event]:
    """Patch the emoji generator with a held implementation, guaranteeing release.

    ``gen`` is a factory taking the release Event and returning the coroutine
    function to install. The ``finally`` is the point of this helper: a held
    task parks on ``release.wait()``, and if an assertion aborts the body before
    the release, that task stays parked while still strongly referenced by the
    module-global ``_ARTIFACT_FOLDER_ICON_TASKS``. It then outlives its event
    loop and poisons a LATER test that awaits that set -- the sibling handler
    suite awaits it unfiltered, so the failure surfaces far from its cause.
    Releasing and draining on every exit path keeps a failing assertion's blast
    radius inside its own test.
    """
    release = asyncio.Event()
    with patch.object(art_handlers, "generate_emoji_for_name", AsyncMock(side_effect=gen(release))):
        try:
            yield release
        finally:
            release.set()
            await _drain_icon_tasks()


# ── Store layer ─────────────────────────────────────────────────────────────


class TestStoreIconEpoch:
    def test_a_fresh_folder_starts_at_epoch_zero(self, fstore: ArtifactFolderStore) -> None:
        f = fstore.create("Reports")
        assert _epoch(fstore, f["id"]) == 0

    def test_an_unknown_folder_reads_epoch_zero(self, fstore: ArtifactFolderStore) -> None:
        assert _epoch(fstore, "nope") == 0

    def test_set_icon_bumps_the_epoch(self, fstore: ArtifactFolderStore) -> None:
        f = fstore.create("Reports")
        fstore.set_icon(f["id"], "🧪")
        assert _epoch(fstore, f["id"]) == 1

    def test_an_icon_clear_bumps_the_epoch(self, fstore: ArtifactFolderStore) -> None:
        """A clear is the race a value-pin cannot catch: the icon VALUE goes
        absent -> absent, so only a counter records that the user acted."""
        f = fstore.create("Reports")
        assert _epoch(fstore, f["id"]) == 0
        fstore.set_icon(f["id"], "")
        assert _epoch(fstore, f["id"]) == 1

    def test_rename_bumps_the_epoch(self, fstore: ArtifactFolderStore) -> None:
        f = fstore.create("Reports")
        fstore.rename(f["id"], "Quarterly")
        assert _epoch(fstore, f["id"]) == 1

    def test_set_icon_if_epoch_writes_when_the_epoch_matches(
        self, fstore: ArtifactFolderStore
    ) -> None:
        f = fstore.create("Reports")
        out = fstore.set_icon_if_epoch(f["id"], "🚀", 0)
        assert out is not None and out["icon"] == "🚀"
        assert fstore.get(f["id"])["icon"] == "🚀"

    def test_the_generated_write_back_does_not_bump_the_epoch(
        self, fstore: ArtifactFolderStore
    ) -> None:
        """It is a generated result landing, not a user mutation later
        generations must lose to."""
        f = fstore.create("Reports")
        fstore.set_icon_if_epoch(f["id"], "🚀", 0)
        assert _epoch(fstore, f["id"]) == 0

    def test_set_icon_if_epoch_drops_a_stale_write(self, fstore: ArtifactFolderStore) -> None:
        f = fstore.create("Reports")
        captured = _epoch(fstore, f["id"])
        fstore.set_icon(f["id"], "🧪")  # the user acts while generation runs
        assert fstore.set_icon_if_epoch(f["id"], "🚀", captured) is None
        assert fstore.get(f["id"])["icon"] == "🧪"

    def test_set_icon_if_epoch_drops_a_write_for_a_deleted_folder(
        self, tmp_path: Path, fstore: ArtifactFolderStore
    ) -> None:
        store = ArtifactStore(root=tmp_path / "artifacts")
        f = fstore.create("Reports")
        fstore.delete(f["id"], delete_contents=False, artifact_store=store)
        assert fstore.set_icon_if_epoch(f["id"], "🚀", 0) is None

    def test_a_confirmed_delete_pops_the_epoch_entry(
        self, tmp_path: Path, fstore: ArtifactFolderStore
    ) -> None:
        store = ArtifactStore(root=tmp_path / "artifacts")
        f = fstore.create("Reports")
        fstore.set_icon(f["id"], "🧪")
        assert fstore._icon_epochs.get(f["id"]) == 1
        fstore.delete(f["id"], delete_contents=False, artifact_store=store)
        assert f["id"] not in fstore._icon_epochs

    def test_a_cascade_delete_pops_every_subtree_entry(
        self, tmp_path: Path, fstore: ArtifactFolderStore
    ) -> None:
        store = ArtifactStore(root=tmp_path / "artifacts")
        parent = fstore.create("Reports")
        child = fstore.create("Q3", parent_id=parent["id"])
        fstore.set_icon(parent["id"], "🧪")
        fstore.set_icon(child["id"], "📈")
        fstore.delete(parent["id"], delete_contents=True, artifact_store=store)
        assert parent["id"] not in fstore._icon_epochs
        assert child["id"] not in fstore._icon_epochs

    def test_a_failed_delete_commit_keeps_the_epoch_guard(
        self, tmp_path: Path, fstore: ArtifactFolderStore, monkeypatch
    ) -> None:
        """Popping before the write is confirmed would leave the folder present
        with its epoch reset to 0, letting a stale in-flight generation clobber
        the manual icon the user just chose."""
        store = ArtifactStore(root=tmp_path / "artifacts")
        f = fstore.create("Reports")
        fstore.set_icon(f["id"], "🧪")
        assert fstore._icon_epochs.get(f["id"]) == 1

        def _boom() -> None:
            raise OSError("simulated store write failure")

        monkeypatch.setattr(fstore, "_save", _boom)
        with pytest.raises(OSError):
            fstore.delete(f["id"], delete_contents=False, artifact_store=store)
        # The guard survived the failed commit, so the stale result still loses.
        assert fstore._icon_epochs.get(f["id"]) == 1
        assert fstore.set_icon_if_epoch(f["id"], "🚀", 0) is None

    def test_epochs_are_per_store_instance(self, tmp_path: Path) -> None:
        """Two stores over different files must not alias each other's folder
        ids — a module-level registry keyed only by folder id would."""
        a = ArtifactFolderStore(path=tmp_path / "a.json")
        b = ArtifactFolderStore(path=tmp_path / "b.json")
        fa = a.create("Reports")
        a.set_icon(fa["id"], "🧪")
        assert _epoch(a, fa["id"]) == 1
        assert _epoch(b, fa["id"]) == 0

    def test_rename_returns_its_own_bump(self, fstore: ArtifactFolderStore) -> None:
        f = fstore.create("Reports")
        folder, armed = fstore.rename(f["id"], "Quarterly")
        assert folder["name"] == "Quarterly"
        assert armed == _epoch(fstore, f["id"]) == 1

    def test_the_returned_epoch_is_what_a_read_back_would_have_lost(
        self, fstore: ArtifactFolderStore
    ) -> None:
        """The epoch a rename arms generation with must be the one its OWN bump
        produced. Reading it back afterwards picks up a manual icon set that
        landed in between, and the generated icon then satisfies the check and
        clobbers the manual pick."""
        f = fstore.create("Reports")
        _folder, armed = fstore.rename(f["id"], "Quarterly")
        # A manual pick lands after the rename, before the generated result.
        fstore.set_icon(f["id"], "🧪")
        # Armed with the rename's own epoch, the stale generation loses.
        assert fstore.set_icon_if_epoch(f["id"], "🚀", armed) is None
        assert fstore.get(f["id"])["icon"] == "🧪"
        # A read-back at this point would have returned the manual set's epoch,
        # and the stale write would have satisfied the check and landed.
        assert _epoch(fstore, f["id"]) != armed

    def test_an_unrelated_folders_mutation_does_not_invalidate_a_generation(
        self, fstore: ArtifactFolderStore
    ) -> None:
        """The epoch is per-folder, not a store-wide generation counter."""
        target = fstore.create("Reports")
        other = fstore.create("Scratch")
        captured = _epoch(fstore, target["id"])
        fstore.set_icon(other["id"], "🧪")
        fstore.rename(other["id"], "Scratchpad")
        out = fstore.set_icon_if_epoch(target["id"], "🚀", captured)
        assert out is not None and out["icon"] == "🚀"


# ── Handler layer: the three mid-generation races ────────────────────────────


class TestIconRacesThroughHandlers:
    @pytest.mark.asyncio
    async def test_create_generates_and_writes_the_icon(self, stores, patch_restricted) -> None:
        _store, folders = stores
        with patch.object(
            art_handlers, "generate_emoji_for_name", AsyncMock(return_value="🚀")
        ) as gen:
            resp = await api_artifact_folder_create(_request(body={"name": "Rocketry"}))
            assert resp.status == 201
            fid = _body(resp)["id"]
            await _drain_icon_tasks()
        gen.assert_awaited_once()
        assert folders.get(fid)["icon"] == "🚀"

    @pytest.mark.asyncio
    async def test_a_failed_generation_leaves_the_folder_unchanged(
        self, stores, patch_restricted
    ) -> None:
        _store, folders = stores
        with patch.object(art_handlers, "generate_emoji_for_name", AsyncMock(return_value="")):
            resp = await api_artifact_folder_create(_request(body={"name": "Rocketry"}))
            fid = _body(resp)["id"]
            await _drain_icon_tasks()
        assert not folders.get(fid).get("icon")

    @pytest.mark.asyncio
    async def test_a_folder_deleted_mid_generation_is_not_resurrected(
        self, stores, patch_restricted
    ) -> None:
        store, folders = stores
        created: dict[str, Any] = {}

        async def _gen(_state: Any, _name: str) -> str:
            folders.delete(created["id"], delete_contents=False, artifact_store=store)
            return "🚀"

        with patch.object(art_handlers, "generate_emoji_for_name", AsyncMock(side_effect=_gen)):
            resp = await api_artifact_folder_create(_request(body={"name": "Rocketry"}))
            created["id"] = _body(resp)["id"]
            await _drain_icon_tasks()
        assert folders.get(created["id"]) is None

    @pytest.mark.asyncio
    async def test_a_manual_icon_set_mid_generation_is_not_clobbered(
        self, stores, patch_restricted
    ) -> None:
        _store, folders = stores
        created: dict[str, Any] = {}

        def _gen(release: asyncio.Event) -> Any:
            async def _run(_state: Any, _name: str) -> str:
                await release.wait()  # hold generation until the PATCH lands
                return "🚀"

            return _run

        async with _held_generation(_gen):
            resp = await api_artifact_folder_create(_request(body={"name": "Rocketry"}))
            created["id"] = _body(resp)["id"]
            patched = await api_artifact_folder_update(
                _request(body={"icon": "🧪"}, match={"id": created["id"]})
            )
            assert patched.status == 200
        assert folders.get(created["id"])["icon"] == "🧪"

    @pytest.mark.asyncio
    async def test_an_icon_clear_mid_generation_is_not_overwritten(
        self, stores, patch_restricted
    ) -> None:
        """An explicit clear must win. Under a value-pin the clear left the
        icon equal to its at-schedule value (absent -> absent), so the stale
        emoji landed anyway."""
        _store, folders = stores
        created: dict[str, Any] = {}

        def _gen(release: asyncio.Event) -> Any:
            async def _run(_state: Any, _name: str) -> str:
                await release.wait()
                return "🚀"

            return _run

        async with _held_generation(_gen):
            resp = await api_artifact_folder_create(_request(body={"name": "Rocketry"}))
            created["id"] = _body(resp)["id"]
            patched = await api_artifact_folder_update(
                _request(body={"icon": ""}, match={"id": created["id"]})
            )
            assert patched.status == 200
        assert not folders.get(created["id"]).get("icon")

    @pytest.mark.asyncio
    async def test_a_rename_mid_generation_drops_the_stale_icon(
        self, stores, patch_restricted
    ) -> None:
        """The pending emoji was derived from the OLD name; the rename's own
        regeneration is what supplies the new one."""
        _store, folders = stores
        created: dict[str, Any] = {}
        first = {"hit": False}

        def _gen(release: asyncio.Event) -> Any:
            async def _run(_state: Any, _name: str) -> str:
                if not first["hit"]:
                    first["hit"] = True
                    await release.wait()
                    return "🚀"  # stale: derived from "Rocketry"
                return "🧬"  # the rename's own regeneration

            return _run

        async with _held_generation(_gen):
            resp = await api_artifact_folder_create(_request(body={"name": "Rocketry"}))
            created["id"] = _body(resp)["id"]
            patched = await api_artifact_folder_update(
                _request(body={"name": "Chemistry"}, match={"id": created["id"]})
            )
            assert patched.status == 200
        folder = folders.get(created["id"])
        assert folder["name"] == "Chemistry"
        # The stale "Rocketry" emoji lost; only the post-rename result may land.
        assert folder.get("icon") != "🚀"

    @pytest.mark.asyncio
    async def test_a_rename_then_manual_pick_keeps_the_manual_icon(
        self, stores, patch_restricted
    ) -> None:
        """The case named in the issue: artifact rename REGENERATES, so a
        rename followed by a manual pick must not lose to the regenerated
        result."""
        _store, folders = stores
        created: dict[str, Any] = {}
        calls = {"n": 0}

        def _gen(release: asyncio.Event) -> Any:
            async def _run(_state: Any, _name: str) -> str:
                calls["n"] += 1
                if calls["n"] == 1:
                    return "🚀"  # create-time generation, lands immediately
                await release.wait()  # rename-time regeneration, held
                return "🧬"

            return _run

        async with _held_generation(_gen):
            resp = await api_artifact_folder_create(_request(body={"name": "Rocketry"}))
            created["id"] = _body(resp)["id"]
            await _drain_icon_tasks()
            renamed = await api_artifact_folder_update(
                _request(body={"name": "Chemistry"}, match={"id": created["id"]})
            )
            assert renamed.status == 200
            picked = await api_artifact_folder_update(
                _request(body={"icon": "🧪"}, match={"id": created["id"]})
            )
            assert picked.status == 200
        assert folders.get(created["id"])["icon"] == "🧪"

    @pytest.mark.asyncio
    async def test_an_explicit_icon_in_the_same_patch_wins_over_regeneration(
        self, stores, patch_restricted
    ) -> None:
        """rename + icon in ONE request sets the icon and arms no generation,
        which is the contract the handler comment already asserted."""
        _store, folders = stores
        with patch.object(
            art_handlers, "generate_emoji_for_name", AsyncMock(return_value="🚀")
        ) as gen:
            resp = await api_artifact_folder_create(_request(body={"name": "Rocketry"}))
            fid = _body(resp)["id"]
            await _drain_icon_tasks()
            gen.reset_mock()
            patched = await api_artifact_folder_update(
                _request(body={"name": "Chemistry", "icon": "🧪"}, match={"id": fid})
            )
            assert patched.status == 200
            await _drain_icon_tasks()
        gen.assert_not_awaited()
        assert folders.get(fid)["icon"] == "🧪"

    @pytest.mark.asyncio
    async def test_a_manual_pick_in_the_arming_window_is_not_clobbered(
        self, stores, patch_restricted, monkeypatch
    ) -> None:
        """The window between the rename committing and the handler arming
        generation. Capturing the epoch by re-reading it there picks up the
        competing manual set's epoch, so the generated icon satisfies the check
        and overwrites the user's pick. The rename must arm with the epoch its
        own bump produced instead."""
        _store, folders = stores
        with patch.object(art_handlers, "generate_emoji_for_name", AsyncMock(return_value="🧬")):
            resp = await api_artifact_folder_create(_request(body={"name": "Rocketry"}))
            fid = _body(resp)["id"]
            await _drain_icon_tasks()

            real_off_loop = art_handlers._run_off_loop
            fired = {"done": False}

            async def _hooked(fn):  # type: ignore[no-untyped-def]
                out = await real_off_loop(fn)
                # Land a manual pick the instant _apply_updates has committed,
                # i.e. inside the arming window.
                if not fired["done"]:
                    fired["done"] = True
                    folders.set_icon(fid, "🧪")
                return out

            monkeypatch.setattr(art_handlers, "_run_off_loop", _hooked)
            patched = await api_artifact_folder_update(
                _request(body={"name": "Chemistry"}, match={"id": fid})
            )
            assert patched.status == 200
            assert fired["done"], "the arming-window hook never fired"
            await _drain_icon_tasks()
        assert folders.get(fid)["icon"] == "🧪"

    @pytest.mark.asyncio
    async def test_a_manual_pick_before_a_created_folders_icon_lands_wins(
        self, stores, patch_restricted
    ) -> None:
        """Create arms with epoch 0 by construction. A manual pick landing
        before the generated result raises the epoch, so the generated icon
        must lose rather than overwrite it."""
        _store, folders = stores
        created: dict[str, str] = {}

        def _gen(release: asyncio.Event) -> Any:
            async def _run(_state: Any, _name: str) -> str:
                await release.wait()
                return "🚀"

            return _run

        async with _held_generation(_gen):
            resp = await api_artifact_folder_create(_request(body={"name": "Rocketry"}))
            created["id"] = _body(resp)["id"]
            folders.set_icon(created["id"], "🧪")
        assert folders.get(created["id"])["icon"] == "🧪"

    @pytest.mark.asyncio
    async def test_a_delete_through_the_handler_releases_the_epoch(
        self, stores, patch_restricted
    ) -> None:
        _store, folders = stores
        with patch.object(art_handlers, "generate_emoji_for_name", AsyncMock(return_value="🚀")):
            resp = await api_artifact_folder_create(_request(body={"name": "Rocketry"}))
            fid = _body(resp)["id"]
            await _drain_icon_tasks()
        patched = await api_artifact_folder_update(_request(body={"icon": "🧪"}, match={"id": fid}))
        assert patched.status == 200
        assert folders._icon_epochs.get(fid) == 1
        deleted = await api_artifact_folder_delete(_request(match={"id": fid}))
        assert deleted.status == 200
        assert fid not in folders._icon_epochs
