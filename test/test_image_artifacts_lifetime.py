"""Chat-image copies: the asset owner check, the pinned sidecar read, and their lifetime.

Split from ``test_image_artifacts.py``: these pin what makes a transcript image
survive its scratch file. The asset route serves a copy only to its
owner or a fork of it, the bytes come from the record that was authorized, the widget
sweep never takes a chat image, and a permanent delete reaps only untouched copies.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import types
from pathlib import Path

import pytest
from test_image_artifacts import _png_bytes

from kiro_crew import image_artifacts
from kiro_crew.artifacts import ArtifactError, ArtifactNotFoundError, ArtifactStore


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ArtifactStore:
    """A tmp-rooted store installed as image_artifacts' default."""
    s = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(image_artifacts, "get_default_store", lambda: s)
    return s


class _StatView:
    """A real ``os.stat_result`` with selected fields overridden.

    Lets a test drive the descriptor checks (link count, file type) on every
    host, instead of skipping where the filesystem cannot make a hardlink or a
    FIFO.
    """

    def __init__(self, real: os.stat_result, **overrides: int) -> None:
        self._real = real
        self._overrides = overrides

    def __getattr__(self, name: str) -> object:
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._real, name)


class TestAssetOwnerAndPinnedRead:
    def test_an_over_bound_owner_is_refused_not_truncated(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """One owner-key bound is shared by the store, image registration and
        fork lineage. A key over it is never stored truncated (a truncated
        owner could not match its own `?session=` check or the reap): the store
        refuses it, and registration skips the message with a warning instead of
        trying the store once per image."""
        from kiro_crew import image_artifacts
        from kiro_crew.artifacts import MAX_SESSION_KEY_CHARS, ArtifactValidationError
        from kiro_crew.dashboard import fork_lineage

        assert fork_lineage.MAX_ANCESTOR_KEY_CHARS == MAX_SESSION_KEY_CHARS
        long_owner = "chat-" + "x" * MAX_SESSION_KEY_CHARS
        with pytest.raises(ArtifactValidationError):
            store.create_image(
                name="Pic", image_bytes=_png_bytes(2, 2), mime="image/png", session_key=long_owner
            )
        at_bound = "c" * MAX_SESSION_KEY_CHARS
        art = store.create_image(
            name="Pic", image_bytes=_png_bytes(2, 2), mime="image/png", session_key=at_bound
        )
        assert store.get(art.slug).session_key == at_bound
        monkeypatch.setattr(image_artifacts, "get_default_store", lambda: store)
        shot = tmp_path / "shot.png"
        shot.write_bytes(_png_bytes(2, 2))
        assert image_artifacts.register_images(f"![s]({shot})", "ts-long-owner", long_owner) == []

    def test_asset_refuses_another_sessions_copy_when_the_caller_names_its_session(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Image slugs derive from (message ts, ordinal) only, so two sessions can
        collide. A transcript fallback names its own session; a copy recorded for
        a different session must 404 rather than render the wrong picture."""
        from kiro_crew.dashboard.handlers import artifacts as handlers

        art = store.create_image(
            name="Pic", image_bytes=_png_bytes(2, 2), mime="image/png", session_key="chat-7"
        )
        monkeypatch.setattr(handlers, "get_default_store", lambda: store)

        def _req(session: str) -> types.SimpleNamespace:
            return types.SimpleNamespace(match_info={"slug": art.slug}, query={"session": session})

        audits: list[tuple[str, str, dict]] = []
        monkeypatch.setattr(
            handlers,
            "_audit",
            lambda *, tool, request, outcome, extra=None, error=None: audits.append(
                (tool, outcome, dict(extra or {}))
            ),
        )

        # Owner, in every spelling the dashboard uses.
        for spelling in ("chat-7", "dashboard:chat-7", "dashboard_chat-7"):
            resp = asyncio.run(handlers.api_artifact_asset(_req(spelling)))  # type: ignore[arg-type]
            assert resp.status == 200, spelling
        # Another session.
        resp = asyncio.run(handlers.api_artifact_asset(_req("chat-8")))  # type: ignore[arg-type]
        assert resp.status == 404
        # No session named (artifact library): unchanged behaviour, and no owner
        # decision was made, so nothing is audited for it.
        resp = asyncio.run(handlers.api_artifact_asset(types.SimpleNamespace(match_info={"slug": art.slug})))  # type: ignore[arg-type]
        assert resp.status == 200
        # The owner decision is a permission decision: every one of the four
        # decisions above is audited with its outcome, allowed and denied alike.
        assert [(t, o) for t, o, _ in audits] == [
            ("artifact_asset_owner_check", "success"),
            ("artifact_asset_owner_check", "success"),
            ("artifact_asset_owner_check", "success"),
            ("artifact_asset_owner_check", "denied"),
        ]
        assert audits[-1][2] == {"slug": art.slug, "session": "chat-8", "owner": "chat-7"}

    def test_asset_is_served_to_a_fork_of_the_owning_session(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fork keeps its source's message ts, so it renders the source's copies;
        the owner check must follow `forked_from` (any depth) and still refuse an
        unrelated session or one whose lineage cannot be read."""
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.handlers import artifacts as handlers

        art = store.create_image(
            name="Pic", image_bytes=_png_bytes(2, 2), mime="image/png", session_key="chat-src"
        )
        monkeypatch.setattr(handlers, "get_default_store", lambda: store)
        lineage = {
            "dashboard:chat-fork": "dashboard:chat-src",
            "dashboard:chat-grand": "dashboard:chat-fork",
        }
        log = MagicMock()
        log.get_metadata_status.side_effect = lambda k: (
            ({"forked_from": lineage[k]}, True) if k in lineage else ({}, True)
        )
        state = types.SimpleNamespace(conversation_log=log)

        def _req(session: str) -> types.SimpleNamespace:
            return types.SimpleNamespace(
                match_info={"slug": art.slug}, query={"session": session}, app={"state": state}
            )

        assert asyncio.run(handlers.api_artifact_asset(_req("chat-fork"))).status == 200  # type: ignore[arg-type]
        assert asyncio.run(handlers.api_artifact_asset(_req("chat-grand"))).status == 200  # type: ignore[arg-type]
        assert asyncio.run(handlers.api_artifact_asset(_req("chat-other"))).status == 404  # type: ignore[arg-type]
        # The intermediate fork is deleted: its record is gone, but the grandchild
        # carries the materialized chain and still proves descent from the owner.
        surviving = {
            "dashboard:chat-grand": {
                "forked_from": "dashboard:chat-fork",
                "fork_ancestors": ["dashboard:chat-fork", "dashboard:chat-src"],
            }
        }
        log.get_metadata_status.side_effect = lambda k: (surviving.get(k, {}), True)
        assert asyncio.run(handlers.api_artifact_asset(_req("chat-grand"))).status == 200  # type: ignore[arg-type]
        # Unreadable lineage fails closed.
        log.get_metadata_status.side_effect = lambda k: ({}, False)
        assert asyncio.run(handlers.api_artifact_asset(_req("chat-fork"))).status == 404  # type: ignore[arg-type]

    def test_owner_check_and_bytes_come_from_one_store_snapshot(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The owner the endpoint authorizes must be the owner of the bytes it
        serves. Deleting the slug and recreating it under another session between
        the two would otherwise let the old owner's check pass and the new owner's
        picture be served."""
        from kiro_crew.dashboard.handlers import artifacts as handlers

        art = store.create_image(
            name="Pic",
            image_bytes=_png_bytes(2, 2),
            mime="image/png",
            slug="shared-slug",
            session_key="chat-a",
        )
        monkeypatch.setattr(handlers, "get_default_store", lambda: store)
        # `read_image_bytes_with_owner` reports the owner from the same locked
        # metadata read that resolves the asset path.
        data, mime, owner = store.read_image_bytes_with_owner(art.slug)
        assert (data, mime, owner) == (_png_bytes(2, 2), "image/png", "chat-a")

        # Recreate under another session: the endpoint must now judge by chat-b.
        store.delete(art.slug)
        store.create_image(
            name="Pic2",
            image_bytes=_png_bytes(3, 3),
            mime="image/png",
            slug="shared-slug",
            session_key="chat-b",
        )
        req_a = types.SimpleNamespace(
            match_info={"slug": "shared-slug"}, query={"session": "chat-a"}
        )
        req_b = types.SimpleNamespace(
            match_info={"slug": "shared-slug"}, query={"session": "chat-b"}
        )
        assert asyncio.run(handlers.api_artifact_asset(req_a)).status == 404  # type: ignore[arg-type]
        resp = asyncio.run(handlers.api_artifact_asset(req_b))  # type: ignore[arg-type]
        assert resp.status == 200 and resp.body == _png_bytes(3, 3)

    def test_asset_bytes_are_refused_if_the_sidecar_changed_after_the_owner_was_captured(
        self, store: ArtifactStore
    ) -> None:
        """The owner is captured under the metadata lock; the bytes are read after
        it. The read pins the sidecar's inode from under the lock and refuses any
        other inode, so a slug deleted and recreated in that window can never pair
        the earlier owner with the later record's picture."""
        art = store.create_image(
            name="Pic", image_bytes=_png_bytes(2, 2), mime="image/png", session_key="chat-a"
        )
        asset = next(store._artifact_dir(art.slug).glob("asset.*"))
        pinned = store._sidecar_identity(asset)
        assert pinned is not None
        # Same inode: served.
        assert store._read_image_asset_bytes(asset, identity=pinned) == _png_bytes(2, 2)
        # Sidecar replaced (delete + recreate): even if the filesystem hands the
        # new file the same inode number, its size/mtime differ — refused.
        asset.unlink()
        asset.write_bytes(_png_bytes(3, 3))
        with pytest.raises(ArtifactNotFoundError):
            store._read_image_asset_bytes(asset, identity=pinned)

    def test_pinned_read_refuses_bytes_overwritten_in_place_between_check_and_read(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The store never writes a sidecar in place (atomic tmp + replace), but
        the directory is agent-writable, so an in-place overwrite of the SAME
        inode can land between the descriptor's identity check and its read, and
        with the same length inside one clock tick the inode identity cannot
        tell. The bytes are therefore also bound to the record's SHA-256,
        captured under the metadata lock with the owner: the hook below rewrites
        the file the moment the first ``fstat`` has passed (same inode, same
        length) and the asset read refuses it. A read with no digest to bind to
        (a legacy record) keeps the identity check alone; a digest that matches
        is served; the identity is re-read after the bytes too, so a file grown
        mid-read is refused by size even when its clock is pinned back."""
        art = store.create_image(
            name="Pic", image_bytes=_png_bytes(2, 2), mime="image/png", session_key="chat-a"
        )
        asset = next(store._artifact_dir(art.slug).glob("asset.*"))
        pinned = store._sidecar_identity(asset)
        assert pinned is not None
        original = asset.read_bytes()
        digest = art.image.sha256 if art.image is not None else None
        assert digest == hashlib.sha256(original).hexdigest()
        same_length = bytes(len(original))
        inode = os.stat(asset).st_ino

        real_fstat = os.fstat
        calls: list[int] = []

        def _overwrite_after_first_fstat(fd: int) -> os.stat_result:
            st = real_fstat(fd)
            calls.append(fd)
            if len(calls) == 1:
                with open(asset, "r+b") as handle:  # in place: same inode
                    handle.write(same_length)
                os.utime(asset, ns=(pinned[3], pinned[3]))  # clock pinned back
            return st

        with monkeypatch.context() as mp:
            mp.setattr(os, "fstat", _overwrite_after_first_fstat)
            with pytest.raises(ArtifactNotFoundError, match="does not match its record"):
                store._read_image_asset_bytes(asset, identity=pinned, sha256=digest)
        assert os.stat(asset).st_ino == inode and asset.read_bytes() == same_length
        # The public read path binds to the record the same way.
        with pytest.raises(ArtifactNotFoundError):
            store.read_image_bytes_with_owner(art.slug)

        asset.write_bytes(original)
        os.utime(asset, ns=(pinned[3], pinned[3]))
        assert store._sidecar_identity(asset) == pinned
        assert store._read_image_asset_bytes(asset, identity=pinned, sha256=digest) == original
        assert store.read_image_bytes_with_owner(art.slug)[0] == original

        calls.clear()

        def _grow_after_first_fstat(fd: int) -> os.stat_result:
            st = real_fstat(fd)
            calls.append(fd)
            if len(calls) == 1:
                with open(asset, "ab") as handle:
                    handle.write(b"\0" * 7)
                os.utime(asset, ns=(pinned[3], pinned[3]))
            return st

        monkeypatch.setattr(os, "fstat", _grow_after_first_fstat)
        with pytest.raises(ArtifactNotFoundError, match="not readable"):
            store._read_image_asset_bytes(asset, identity=pinned)

    def test_pinned_read_still_refuses_a_hardlinked_sidecar(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The identity pin is an ADDITIONAL check, not a replacement: the
        regular-file and link-count checks of the unpinned read apply to the
        same descriptor, so an aliased sidecar is refused even when its inode
        is the one captured under the lock. The descriptor's link count is
        faked, so the check runs on every host whether or not its filesystem
        supports hardlinks."""
        art = store.create_image(
            name="Pic", image_bytes=_png_bytes(2, 2), mime="image/png", session_key="chat-a"
        )
        asset = next(store._artifact_dir(art.slug).glob("asset.*"))
        pinned = store._sidecar_identity(asset)
        assert pinned is not None
        assert store._read_image_asset_bytes(asset, identity=pinned)
        real_fstat = os.fstat
        monkeypatch.setattr(os, "fstat", lambda fd: _StatView(real_fstat(fd), st_nlink=2))
        with pytest.raises(ArtifactNotFoundError):
            store._read_image_asset_bytes(asset, identity=pinned)

    def test_pinned_read_refuses_a_fifo_instead_of_blocking_on_it(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A FIFO planted at the sidecar path must be REFUSED, not waited on: a
        blocking ``open`` would run before the regular-file check and pin an
        executor thread per request. The pinned read must open non-blocking, like
        the unpinned one, and refuse a descriptor that is not a regular file even
        when its identity matches the pin. Both halves are driven through faked
        open and ``fstat`` so the check runs on every host, FIFO support or not."""
        import stat as stat_mod

        from kiro_crew import artifacts as mod

        art = store.create_image(
            name="Pic", image_bytes=_png_bytes(2, 2), mime="image/png", session_key="chat-a"
        )
        asset = next(store._artifact_dir(art.slug).glob("asset.*"))
        pinned = store._sidecar_identity(asset)
        assert pinned is not None
        real_open = mod.platform_compat.open_file_no_reparse
        opens: list[bool] = []

        def _recording_open(path, *, nonblocking: bool = False) -> int:
            opens.append(nonblocking)
            return real_open(path, nonblocking=nonblocking)

        real_fstat = os.fstat
        fifo_mode = stat_mod.S_IFIFO | 0o600
        monkeypatch.setattr(mod.platform_compat, "open_file_no_reparse", _recording_open)
        monkeypatch.setattr(os, "fstat", lambda fd: _StatView(real_fstat(fd), st_mode=fifo_mode))
        with pytest.raises(ArtifactNotFoundError):
            store._read_image_asset_bytes(asset, identity=pinned)
        assert opens and all(opens), "the pinned read must open non-blocking"

    def test_delete_waits_for_an_in_flight_registration(self) -> None:
        """A permanent delete right after finalize must not reap before the
        detached registration lands, or the late copy outlives the transcript."""
        from kiro_crew import image_artifacts as ia

        async def _scenario() -> tuple[bool, list[str]]:
            order: list[str] = []
            gate = asyncio.Event()

            async def _register() -> None:
                await gate.wait()
                order.append("registered")

            task = asyncio.create_task(_register())
            ia.track_registration("chat-1", task)
            assert ia.pending_registrations({"chat-1"}) == [task]

            async def _delete() -> bool:
                ok = await ia.drain_registrations({"chat-1"})
                order.append("reaped")
                return ok

            deleter = asyncio.create_task(_delete())
            await asyncio.sleep(0)
            gate.set()
            ok = await deleter
            return ok, order

        ok, order = asyncio.run(_scenario())
        assert ok is True
        assert order == ["registered", "reaped"]

    def test_drain_reports_a_registration_that_outlives_the_timeout(self) -> None:
        from kiro_crew import image_artifacts as ia

        async def _scenario() -> bool:
            hang = asyncio.Event()
            task = asyncio.create_task(hang.wait())
            ia.track_registration("chat-2", task)
            try:
                return await ia.drain_registrations({"chat-2"}, timeout=0.01)
            finally:
                task.cancel()

        assert asyncio.run(_scenario()) is False


class TestChatImageLifetime:
    def test_widget_pruning_never_deletes_unpinned_chat_images(self, store: ArtifactStore) -> None:
        image = store.create_image(
            name="chat screenshot",
            image_bytes=_png_bytes(),
            mime="image/png",
            session_key="chat-1",
            auto_registered=True,
        )
        for i in range(3):
            store.create(
                name=f"widget {i}",
                content=f"<p>{i}</p>",
                kind="widget",
                slug=f"widget-{i}",
                session_key="chat-1",
                auto_registered=True,
            )

        store.prune_auto_widgets(keep=0)

        assert store.get(image.slug).kind == "image"

    def test_session_delete_cleanup_preserves_invested_images(self, store: ArtifactStore) -> None:
        untouched = store.create_image(
            name="temporary screenshot",
            image_bytes=_png_bytes(),
            mime="image/png",
            session_key="chat-1",
            auto_registered=True,
        )
        pinned = store.create_image(
            name="saved screenshot",
            image_bytes=_png_bytes(),
            mime="image/png",
            slug="saved-screenshot",
            session_key="chat-1",
            auto_registered=True,
        )
        store.set_pinned(pinned.slug, True)
        other = store.create_image(
            name="other chat",
            image_bytes=_png_bytes(),
            mime="image/png",
            slug="other-chat",
            session_key="chat-2",
            auto_registered=True,
        )

        assert store.delete_auto_images_for_sessions({"chat-1"}) == 1
        with pytest.raises(ArtifactNotFoundError):
            store.get(untouched.slug)
        assert store.get(pinned.slug).pinned is True
        assert store.get(other.slug).session_key == "chat-2"

    def test_reap_takes_only_the_slugs_snapshotted_before_it_decided(
        self, store: ArtifactStore
    ) -> None:
        """A delete snapshots the store's auto-image slugs, then decides who is
        live and enumerates the store; a session recreated under the deleted key
        in between registers copies whose slugs are not in the snapshot.
        `only_slugs` bounds the reap to the generation it judged: the snapshotted
        copy goes, the later one stays, and no clock is compared."""
        old = store.create_image(
            name="old incarnation",
            image_bytes=_png_bytes(),
            mime="image/png",
            slug="old-copy",
            session_key="chat-1",
            auto_registered=True,
        )
        pinned = store.create_image(
            name="saved",
            image_bytes=_png_bytes(),
            mime="image/png",
            slug="saved-copy",
            session_key="chat-1",
            auto_registered=True,
        )
        store.set_pinned(pinned.slug, True)
        manual = store.create_image(
            name="hand-saved", image_bytes=_png_bytes(), mime="image/png", session_key="chat-1"
        )
        snapshot = store.auto_image_slugs()
        assert snapshot == {old.slug: "chat-1", pinned.slug: "chat-1"}
        assert manual.slug not in snapshot  # not auto-registered: never a reap candidate
        new = store.create_image(
            name="new incarnation",
            image_bytes=_png_bytes(),
            mime="image/png",
            slug="new-copy",
            session_key="chat-1",
            auto_registered=True,
        )
        assert new.slug not in snapshot

        judged = {slug for slug, owner in snapshot.items() if owner in {"chat-1"}}
        assert store.delete_auto_images_for_sessions({"chat-1"}, only_slugs=judged) == 1
        with pytest.raises(ArtifactNotFoundError):
            store.get(old.slug)
        assert store.get(pinned.slug).pinned is True
        assert store.get(new.slug).session_key == "chat-1"
        # Without a snapshot the reap is owner-wide, as before.
        assert store.delete_auto_images_for_sessions({"chat-1"}) == 1
        with pytest.raises(ArtifactNotFoundError):
            store.get(new.slug)

    def test_the_reap_walk_is_bounded_and_aborts_fail_closed_past_the_cap(
        self, tmp_path, monkeypatch
    ):
        """A huge store must not crash the gateway from inside a session delete:
        the walk counts directory entries against MAX_REAP_SCAN_ENTRIES and
        raises ArtifactError past it, before any copy is deleted. Both callers
        treat the raise as an unreadable store and skip the reap."""
        from kiro_crew import artifacts as mod

        store = ArtifactStore(tmp_path / "store")
        for i in range(3):
            store.create_image(
                name=f"copy {i}",
                image_bytes=_png_bytes(),
                mime="image/png",
                slug=f"copy-{i}",
                session_key="chat-1",
                auto_registered=True,
            )
        monkeypatch.setattr(mod, "MAX_REAP_SCAN_ENTRIES", 2)
        with pytest.raises(ArtifactError, match="more than 2 entries"):
            store.auto_image_slugs()
        with pytest.raises(ArtifactError):
            store.delete_auto_images_for_sessions({"chat-1"})
        assert {store.get(f"copy-{i}").slug for i in range(3)} == {"copy-0", "copy-1", "copy-2"}
        # A snapshot-bounded delete loads exactly its slugs and never walks the
        # store, so it still runs under a cap the walk would exceed.
        assert store.delete_auto_images_for_sessions({"chat-1"}, only_slugs={"copy-0"}) == 1
        with pytest.raises(ArtifactNotFoundError):
            store.get("copy-0")
        assert store.get("copy-1").slug == "copy-1"

    def test_the_reap_walk_bounds_the_fields_it_retains_and_aborts_on_an_oversized_record(
        self, tmp_path
    ):
        """The snapshot retains two strings per entry, `slug` and `session_key`,
        decoded from an agent-writable meta.json with no length cap on the read.
        A record carrying either field over its bound aborts the walk with
        ArtifactError (the reap is skipped fail-closed, nothing deleted) instead of
        being retained; a conforming record next to it is unaffected once the
        oversized one is gone."""
        from kiro_crew import artifacts as mod

        store = ArtifactStore(tmp_path / "store")
        store.create_image(
            name="copy",
            image_bytes=_png_bytes(),
            mime="image/png",
            slug="copy-a",
            session_key="chat-1",
            auto_registered=True,
        )
        assert store.auto_image_slugs() == {"copy-a": "chat-1"}
        meta_path = tmp_path / "store" / "copy-a" / "meta.json"
        original = meta_path.read_text()
        meta = json.loads(original)

        meta["session_key"] = "c" * (mod.MAX_SESSION_KEY_CHARS + 1)
        meta_path.write_text(json.dumps(meta))
        with pytest.raises(ArtifactError, match="session image reap aborted"):
            store.auto_image_slugs()
        with pytest.raises(ArtifactError):
            store.delete_auto_images_for_sessions({"chat-1"})
        assert meta_path.exists()

        meta = json.loads(original)
        meta["slug"] = "s" * (mod.MAX_SLUG_CHARS + 1)
        meta_path.write_text(json.dumps(meta))
        with pytest.raises(ArtifactError, match="session image reap aborted"):
            store.auto_image_slugs()

        meta_path.write_text(original)
        assert store.auto_image_slugs() == {"copy-a": "chat-1"}

    def test_a_planted_dot_slug_never_reaches_the_reap_and_the_store_root_survives(self, tmp_path):
        """The reap turns a retained slug into a directory to remove. A planted
        record whose slug is "." (plus a root-level meta.json with the same
        eligible fields) would collapse `_artifact_dir` to the store root. Such a
        record is never retained by the walk, a snapshot naming "." is refused by
        the eligibility check, and every artifact in the store survives."""
        store = ArtifactStore(tmp_path / "store")
        store.create_image(
            name="keep",
            image_bytes=_png_bytes(),
            mime="image/png",
            slug="keep-me",
            session_key="chat-other",
            auto_registered=True,
        )
        planted = json.loads((tmp_path / "store" / "keep-me" / "meta.json").read_text())
        planted["slug"] = "."
        planted["session_key"] = "chat-1"
        (tmp_path / "store" / "x").mkdir()
        (tmp_path / "store" / "x" / "meta.json").write_text(json.dumps(planted))
        (tmp_path / "store" / "meta.json").write_text(json.dumps(planted))
        # A record naming a sibling's directory is not retained either.
        sibling = dict(planted, slug="keep-me")
        (tmp_path / "store" / "y").mkdir()
        (tmp_path / "store" / "y" / "meta.json").write_text(json.dumps(sibling))

        assert store.auto_image_slugs() == {"keep-me": "chat-other"}
        assert store.delete_auto_images_for_sessions({"chat-1"}) == 0
        assert store.delete_auto_images_for_sessions({"chat-1"}, only_slugs={"."}) == 0
        assert (tmp_path / "store").is_dir()
        assert (tmp_path / "store" / "keep-me" / "meta.json").exists()

    def test_the_reap_walk_bounds_the_meta_file_before_decoding_it(self, tmp_path):
        """`meta.json` sits in an agent-writable directory. The reap sizes each
        record before it is read and aborts the walk fail-closed on one past
        MAX_META_BYTES (nothing deleted, the file untouched); the shared meta
        reader is itself a bounded read, so a direct load of the record refuses
        to decode it too and a plain reader never materializes the whole file.
        A record just inside the bound reads as before."""
        from kiro_crew import artifacts as mod

        store = ArtifactStore(tmp_path / "store")
        store.create_image(
            name="copy",
            image_bytes=_png_bytes(),
            mime="image/png",
            slug="copy-a",
            session_key="chat-1",
            auto_registered=True,
        )
        meta_path = tmp_path / "store" / "copy-a" / "meta.json"
        original = meta_path.read_text()
        meta = json.loads(original)

        meta["description"] = "d" * mod.MAX_META_BYTES
        meta_path.write_text(json.dumps(meta))
        assert meta_path.stat().st_size > mod.MAX_META_BYTES
        with pytest.raises(ArtifactError, match="session image reap aborted"):
            store.auto_image_slugs()
        with pytest.raises(ArtifactError):
            store.delete_auto_images_for_sessions({"chat-1"})
        assert meta_path.exists()
        with pytest.raises(ArtifactError, match="exceeds"):
            store.get("copy-a")

        padded = json.loads(original)
        padded["description"] = "d" * (mod.MAX_META_BYTES - len(json.dumps(padded)) - 1)
        meta_path.write_text(json.dumps(padded))
        assert meta_path.stat().st_size <= mod.MAX_META_BYTES
        assert store.auto_image_slugs() == {"copy-a": "chat-1"}

        meta_path.write_text(original)
        assert store.auto_image_slugs() == {"copy-a": "chat-1"}

    def test_the_reap_walk_skips_an_unreadable_meta_and_a_stray_file(self, tmp_path):
        store = ArtifactStore(tmp_path / "store")
        store.create_image(
            name="copy",
            image_bytes=_png_bytes(),
            mime="image/png",
            slug="copy-a",
            session_key="chat-1",
            auto_registered=True,
        )
        (tmp_path / "store" / "stray.txt").write_text("not a directory")
        broken = tmp_path / "store" / "broken"
        broken.mkdir()
        (broken / "meta.json").write_text("{not json")
        assert store.auto_image_slugs() == {"copy-a": "chat-1"}
