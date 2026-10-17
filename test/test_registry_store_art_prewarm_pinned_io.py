"""The owner-tier store-art prewarm's reads from a clone and writes to the cache.

Each asset, the manifest and the manifest-cache sidecars are read through a pinned
no-follow descriptor walk, and the copy publishes through a pinned destination parent.
Also pinned: the manifest size cap, the hooks neutralizer every checkout spawn carries,
and hostile index coordinates that degrade to safe cache-key defaults instead of
crashing the derivation.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from registry_store_art_prewarm_helpers import (
    ART,
    MANIFEST,
    SIBLING,
    _entry,
    _fake_fetch,
    _pin_registry,
)

from conftest import requires_o_nofollow, requires_symlinks
from kiro_crew.apps import registry as reg_mod
from kiro_crew.apps.registry import (
    _TRUST_OWNER,
    _blob_cache_dir,
    _copy_declared_art,
    _manifest_cache_path,
    _manifest_source_coordinates,
    _open_pinned_asset,
    _prewarm_owner_tier_store_assets,
    _publish_pinned_asset,
    _read_manifest_cache,
    _store_art_cache_path,
)

# ---------------------------------------------------------------------------
# The manifest size cap
# ---------------------------------------------------------------------------


class TestManifestSizeCap:
    @pytest.mark.asyncio
    async def test_an_oversize_manifest_is_refused_before_reading(self, monkeypatch):
        """An app.json over the cap makes the row return False and caches nothing.

        The ceiling is enforced by the pinned read's descriptor-size validation
        (``_open_pinned_asset`` fstat-gates the opened fd against
        ``max_bytes=_MANIFEST_MAX_BYTES``), so an
        oversize manifest is a source refusal exactly like a symlinked or absent one --
        no separate by-path ``os.stat`` pre-check, no distinct warning."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_MANIFEST_MAX_BYTES", 8)
        big_manifest = dict(MANIFEST, description="x" * 64)
        _fake_fetch(monkeypatch, ART, big_manifest)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0

        assert _read_manifest_cache(entry) is None
        assert not any(p.is_file() for p in _blob_cache_dir().rglob("*"))

    @pytest.mark.asyncio
    async def test_a_manifest_under_the_cap_is_read(self, monkeypatch):
        """Negative control: with the cap raised the same manifest IS read and cached,
        so the refusal above observes the cap and not an accident of the fixture."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_MANIFEST_MAX_BYTES", 1024 * 1024)
        big_manifest = dict(MANIFEST, description="x" * 64)
        _fake_fetch(monkeypatch, ART, big_manifest)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert _read_manifest_cache(entry) == big_manifest


# ---------------------------------------------------------------------------
# The checkout hooks-path neutralizer
# ---------------------------------------------------------------------------


class TestFetchRefHooksNeutralizer:
    """Every git invocation ``_git_fetch_ref`` spawns -- init, remote add, fetch,
    checkout, branch, network or not -- must carry the hooks/fsmonitor neutralizer,
    so a repo shipping ``.githooks/post-checkout`` plus a global ``core.hooksPath``
    cannot run code during the local checkout step (which runs with the plain
    ``clone_env``, not the network transport env)."""

    @staticmethod
    async def _run_fetch(monkeypatch, tmp_path, neutralize=True):
        from kiro_crew.apps.registry_pipeline import checkout as co

        spawned: list[list[str]] = []

        class _Proc:
            returncode = 0

            async def communicate(self):
                return (b"", b"")

        async def _spawn(*args, **kwargs):
            spawned.append(list(args))
            return _Proc()

        async def _wrap(argv, *, mode, _prepare, **_kwargs):
            return list(argv), None

        monkeypatch.setattr(co, "wrap_argv_async", _wrap)
        monkeypatch.setattr(co, "cgroup_scope_argv", lambda a: a)
        monkeypatch.setattr(co, "create_subprocess_limited", _spawn)
        if not neutralize:
            monkeypatch.setattr(co, "_HOOKS_NEUTRALIZER_ARGV", ())

        dest = tmp_path / "clone"
        err = await co._git_fetch_branch(
            "https://forge.example.com/org/app.git",
            "main",
            dest,
            [],
            clone_env={"BASE": "1"},
            sandbox_mode="strict",
        )
        return err, spawned

    @pytest.mark.asyncio
    async def test_every_spawn_carries_the_neutralizer(self, monkeypatch, tmp_path):
        import os

        err, spawned = await self._run_fetch(monkeypatch, tmp_path)
        assert err is None
        assert spawned, "no git process was spawned"
        for argv in spawned:
            assert argv[0] == "git"
            assert f"core.hooksPath={os.devnull}" in argv, argv
            assert "core.fsmonitor=false" in argv, argv
            # The neutralizer precedes the git subcommand.
            first_c = argv.index("-c")
            subcommand = next(tok for tok in argv[1:] if not tok.startswith("-") and "=" not in tok)
            assert first_c < argv.index(subcommand), argv

    @pytest.mark.asyncio
    async def test_the_neutralizer_can_be_absent(self, monkeypatch, tmp_path):
        """Negative control: with the neutralizer emptied no spawn carries it, proving
        the assertion above observes the injection and not an unconditional truth."""
        import os

        err, spawned = await self._run_fetch(monkeypatch, tmp_path, neutralize=False)
        assert err is None
        assert spawned
        assert all(f"core.hooksPath={os.devnull}" not in argv for argv in spawned)


# ---------------------------------------------------------------------------
# The asset read is a pinned, no-follow descriptor walk
# ---------------------------------------------------------------------------


def _clone_with(tmp_path: Path, tree: dict[str, bytes]) -> Path:
    """Lay *tree* into a fresh clone directory under *tmp_path* and return it."""
    clone = tmp_path / "clone"
    for rel, data in tree.items():
        path = clone / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    clone.mkdir(parents=True, exist_ok=True)
    return clone


class TestPinnedNoFollowAssetRead:
    """``_copy_declared_art`` reads each declared asset through
    ``_open_pinned_asset``: a no-follow descriptor walk from the clone directory that
    refuses a link at ANY path component and validates the same opened inode it reads
    from. This closes the TOCTOU in the old by-path read (``resolve()`` + ``stat`` +
    ``is_file`` + ``read_bytes`` by path), where a same-uid process could swap a
    component in the agent-writable clone tempdir for a symlink to a secret between
    the checks and the read. A link present at read time -- the observable a swap
    would create -- is refused regardless of timing, so the whole class of race is
    closed. Every refusal records the asset unobtainable exactly as an absent one."""

    @requires_symlinks
    def test_a_declared_asset_that_is_a_symlink_out_of_the_clone_is_refused(
        self, tmp_path, monkeypatch
    ):
        """Test 1: the asset is a symlink to a secret OUTSIDE the clone. Its bytes
        never reach the blob cache and the path is recorded unobtainable."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)  # scopes the blob cache dir
        assert reg is not None
        secret = tmp_path / "secret.png"
        secret.write_bytes(b"\x89PNG SECRET-SENTINEL")
        clone = _clone_with(tmp_path, {"ui/icon.svg": ART["ui/icon.svg"]})
        (clone / "ui" / "hero.png").symlink_to(secret)  # declared as heroImage
        entry = _entry()

        written, unobtainable = _copy_declared_art(entry, clone, MANIFEST)

        assert "ui/hero.png" in unobtainable
        # The good regular file still landed; the link did not.
        assert _store_art_cache_path(entry, "ui/icon.svg").is_file()
        assert not _store_art_cache_path(entry, "ui/hero.png").exists()
        # The sentinel is nowhere in the blob cache.
        assert b"SECRET-SENTINEL" not in b"".join(
            p.read_bytes() for p in _blob_cache_dir().rglob("*") if p.is_file()
        )

    @requires_symlinks
    def test_the_by_path_read_would_follow_it_negative_control(self, tmp_path, monkeypatch):
        """Negative control for test 1: reverting to the pre-fix by-path read makes
        ``_open_pinned_asset`` follow the same link and the sentinel DOES land -- so
        the refusal above observes the no-follow walk, not an accident of the fixture."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        secret = tmp_path / "secret.png"
        secret.write_bytes(b"\x89PNG SECRET-SENTINEL")
        clone = _clone_with(tmp_path, {"ui/icon.svg": ART["ui/icon.svg"]})
        (clone / "ui" / "hero.png").symlink_to(secret)
        entry = _entry()

        # Pre-fix behaviour: resolve the path and read the bytes, following links.
        def _by_path(root, rel_parts):
            src = (root / Path(*rel_parts)).resolve()
            return src.read_bytes() if src.is_file() else None

        monkeypatch.setattr(reg_mod, "_open_pinned_asset", _by_path)
        _copy_declared_art(entry, clone, MANIFEST)

        cached = _store_art_cache_path(entry, "ui/hero.png")
        assert cached.is_file()
        assert cached.read_bytes() == b"\x89PNG SECRET-SENTINEL"

    @requires_symlinks
    def test_an_intermediate_symlinked_directory_is_refused(self, tmp_path):
        """Test 2: an intermediate directory component is a symlink to a directory
        outside the clone. The no-follow walk refuses it before reaching the file."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "icon.svg").write_bytes(b"\x89PNG OUTSIDE-DIR")
        clone = _clone_with(tmp_path, {"keep.txt": b"x"})
        (clone / "ui").symlink_to(outside, target_is_directory=True)

        # ui/icon.svg walks through the symlinked ``ui`` directory.
        assert _open_pinned_asset(clone, ("ui", "icon.svg")) is None

    def test_a_hard_linked_asset_is_refused(self, tmp_path):
        """Test 3: a declared asset with st_nlink != 1 (hard-linked to a file
        elsewhere on the host) is refused; a single-link copy of the same bytes is
        accepted, proving the refusal observes the link count and not the content."""
        clone = _clone_with(tmp_path, {})
        (clone / "ui").mkdir(parents=True, exist_ok=True)
        target = clone / "ui" / "icon.svg"
        target.write_bytes(b"<svg/>")
        elsewhere = tmp_path / "elsewhere.svg"
        try:
            os.link(target, elsewhere)  # now st_nlink == 2 on target
        except OSError:
            pytest.skip("hard links not supported on this filesystem")

        assert _open_pinned_asset(clone, ("ui", "icon.svg")) is None

        # A single-link file of the same bytes IS accepted.
        elsewhere.unlink()
        assert _open_pinned_asset(clone, ("ui", "icon.svg")) == b"<svg/>"

    def test_a_regular_file_is_read_byte_identical(self, tmp_path):
        """Test 4: a plain regular file is read from the pinned fd byte-for-byte."""
        payload = b"\x89PNG" + bytes(range(256)) * 4
        clone = _clone_with(tmp_path, {"ui/hero.png": payload})
        assert _open_pinned_asset(clone, ("ui", "hero.png")) == payload

    def test_the_size_ceiling_is_inclusive(self, tmp_path, monkeypatch):
        """Test 5: exactly _ART_MAX_BYTES is accepted; one byte over is refused."""
        monkeypatch.setattr(reg_mod, "_ART_MAX_BYTES", 16)
        clone = _clone_with(tmp_path, {"ok.png": b"x" * 16, "big.png": b"x" * 17})
        assert _open_pinned_asset(clone, ("ok.png",)) == b"x" * 16
        assert _open_pinned_asset(clone, ("big.png",)) is None

    @requires_o_nofollow
    def test_the_final_open_carries_o_nofollow(self, tmp_path, monkeypatch):
        """Test 6: on a platform with O_NOFOLLOW, every ``os.open`` the walk issues
        carries the flag, so no component can be a followed link."""
        if os.open not in os.supports_dir_fd:
            pytest.skip("this platform has no dir_fd support; the no-follow walk uses lstat")
        clone = _clone_with(tmp_path, {"ui/icon.svg": b"<svg/>"})
        seen_flags: list[int] = []
        real_open = os.open

        def _spy_open(path, flags, *args, **kwargs):
            seen_flags.append(flags)
            return real_open(path, flags, *args, **kwargs)

        # Keep the spy in ``supports_dir_fd`` so ``_open_pinned_asset`` still selects
        # the dir_fd walk (its guard tests ``os.open in os.supports_dir_fd``).
        monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {_spy_open})
        monkeypatch.setattr(os, "open", _spy_open)
        assert _open_pinned_asset(clone, ("ui", "icon.svg")) == b"<svg/>"

        assert seen_flags, "no os.open was issued"
        assert all(flags & os.O_NOFOLLOW for flags in seen_flags), seen_flags

    def test_an_absent_asset_is_refused(self, tmp_path):
        """A declared path with no file at all is a None (source refusal), as before."""
        clone = _clone_with(tmp_path, {"ui/icon.svg": b"<svg/>"})
        assert _open_pinned_asset(clone, ("ui", "missing.png")) is None

    def test_empty_rel_parts_is_refused(self, tmp_path):
        clone = _clone_with(tmp_path, {"ui/icon.svg": b"<svg/>"})
        assert _open_pinned_asset(clone, ()) is None


# ---------------------------------------------------------------------------
# The DESTINATION publish is pinned too
# ---------------------------------------------------------------------------


class TestPinnedDestinationPublish:
    """The copy PUBLISHES through a pinned destination-parent descriptor
    (``_publish_pinned_asset``). ``_store_art_cache_path`` resolve-and-contains the
    destination, but the by-name ``mkdir`` + ``atomic_write`` that followed re-walked
    that parent by name: a same-uid process swapping an intermediate parent for a
    symlink to an outside directory between the check and the write would land the
    copy OUTSIDE the blob cache. The pinned publish refuses a link at any parent
    component; such a refusal is a destination-write failure, so the asset stays cold
    and is NOT recorded unobtainable."""

    @requires_symlinks
    @pytest.mark.asyncio
    async def test_a_symlinked_destination_parent_writes_nothing_outside(
        self, monkeypatch, tmp_path
    ):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        _fake_fetch(monkeypatch, ART)
        entry = _entry()

        # Pre-plant the row's blob-cache branch directory (the parent of the leaf
        # files) as a symlink to an outside directory. The pinned publish opens the
        # blob root and walks down with O_NOFOLLOW, so this link is refused.
        outside = tmp_path / "outside"
        outside.mkdir()
        icon_leaf = _store_art_cache_path(entry, "ui/icon.svg")
        branch_dir = icon_leaf.parent.parent  # <key>/<branch> -> parent is <key>
        branch_dir.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, branch_dir, target_is_directory=True)

        await _prewarm_owner_tier_store_assets(reg, [entry])

        # Nothing landed inside the outside directory.
        assert not any(p.is_file() for p in outside.rglob("*"))
        # The assets are recorded as destination-write failures: cold, NOT unobtainable.
        record = reg_mod._unobtainable_art_path(entry)
        recorded = json.loads(record.read_text(encoding="utf-8")) if record.exists() else []
        assert recorded == []

    def test_publish_refuses_a_linked_parent_directly(self, monkeypatch, tmp_path):
        """Direct ``_publish_pinned_asset``: an intermediate parent that is a symlink
        to an outside dir is refused (OSError) and the outside dir stays empty."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        entry = _entry()
        cache_path = _store_art_cache_path(entry, "ui/icon.svg")
        outside = tmp_path / "outside"
        outside.mkdir()
        key_dir = cache_path.parent.parent.parent  # blob_root/<key>
        key_dir.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, key_dir, target_is_directory=True)

        with pytest.raises(OSError):
            _publish_pinned_asset(cache_path, b"<svg/>")
        assert not any(p.is_file() for p in outside.rglob("*"))

    @requires_symlinks
    def test_publish_refuses_a_linked_cache_ancestor(self, monkeypatch, tmp_path):
        """The blob root is ``config_dir()/cache/blobs``. A by-name
        ``blob_root.mkdir(parents=True)`` before the pinned walk would create/follow the
        ``cache`` ANCESTOR by name first, so a linked ``cache`` (pointing outside the
        data home) would be followed before any O_NOFOLLOW walk ran. The publish now
        anchors the pinned walk at ``config_dir()`` and opens ``("cache", "blobs", ...)``
        through it, so a linked ``cache`` ancestor is refused and nothing lands in its
        target."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        # Point the data home and blob root at a private tree under tmp_path so the
        # planted ``cache`` symlink never touches the shared data home this class uses.
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(reg_mod, "config_dir", lambda: home)
        monkeypatch.setattr(reg_mod, "_blob_cache_dir", lambda: home / "cache" / "blobs")
        entry = _entry()
        cache_path = _store_art_cache_path(entry, "ui/icon.svg")
        outside = tmp_path / "outside"
        outside.mkdir()
        # Plant ``config_dir()/cache`` (the ancestor of the blob root) as a symlink to
        # the outside directory, before any part of the blob tree exists.
        cache_ancestor = home / "cache"
        assert not cache_ancestor.exists()
        os.symlink(outside, cache_ancestor, target_is_directory=True)

        with pytest.raises(OSError):
            _publish_pinned_asset(cache_path, b"<svg/>")
        assert not any(
            p.is_file() for p in outside.rglob("*")
        ), "a linked cache ancestor must not let the write land outside the data home"

    def test_publish_writes_a_regular_file_through_a_clean_chain(self, monkeypatch, tmp_path):
        """Negative-control shape: with no planted link the same publish lands the
        bytes at the computed cache path byte-for-byte."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        entry = _entry()
        cache_path = _store_art_cache_path(entry, "ui/icon.svg")
        _publish_pinned_asset(cache_path, b"<svg/>")
        assert cache_path.is_file()
        assert cache_path.read_bytes() == b"<svg/>"

    @requires_symlinks
    def test_the_by_name_arm_refuses_a_linked_blob_root(self, monkeypatch, tmp_path):
        """The by-name (no-``dir_fd``) publish arm — ``_publish_pinned_asset`` when
        ``pinned_fs.supports_pinned_walk()`` is False — lstat-refuses a LINKED blob
        root through ``open_pinned_descendant_dir`` (created with ``mkdir(exist_ok=True)``,
        which silently succeeds on a junction/symlink root) and writes nothing into the
        link target. Forced on POSIX by monkeypatching the pinned-walk probe, so the
        Windows arm runs here too."""
        import kiro_crew.pinned_fs as pinned_fs

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        outside = tmp_path / "outside"
        outside.mkdir()
        blob_root = _blob_cache_dir()
        blob_root.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, blob_root, target_is_directory=True)
        entry = _entry()
        cache_path = _store_art_cache_path(entry, "ui/icon.svg")

        with pytest.raises(OSError):
            reg_mod._publish_pinned_asset(cache_path, b"<svg/>")
        assert not any(p.is_file() for p in outside.rglob("*")), "nothing written into the target"

    def test_the_by_name_arm_writes_through_a_real_root_negative_control(
        self, monkeypatch, tmp_path
    ):
        """Negative control: the SAME by-name arm, given a REAL (unlinked) blob root,
        writes the bytes — so the refusal above is the linked root, not the arm being
        inert."""
        import kiro_crew.pinned_fs as pinned_fs

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        entry = _entry()
        cache_path = _store_art_cache_path(entry, "ui/icon.svg")

        reg_mod._publish_pinned_asset(cache_path, b"<svg/>")
        assert cache_path.is_file() and cache_path.read_bytes() == b"<svg/>"


# ---------------------------------------------------------------------------
# app.json is read through the pinned no-follow walk, not by path
# ---------------------------------------------------------------------------


class TestPinnedManifestRead:
    """``_fetch_owner_tier_store_assets`` reads ``app.json`` through the SAME pinned
    no-follow descriptor walk the art files use (``_open_pinned_asset`` with
    ``max_bytes=_MANIFEST_MAX_BYTES``), not by path. The by-path
    ``resolve``/``stat``/``read_text`` it replaced was a TOCTOU: a same-uid process
    could swap ``app.json`` for a symlink to a secret between the containment check and
    the read, landing that secret in the agent-readable manifest cache. A link at the
    ``app.json`` component, a hard-linked manifest, or one over the ceiling is now a
    source refusal; a plain regular manifest reads unchanged."""

    @requires_symlinks
    @pytest.mark.asyncio
    async def test_a_symlinked_app_json_never_lands_its_target_in_the_manifest_cache(
        self, monkeypatch, tmp_path
    ):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        secret = tmp_path / "credentials.json"
        secret.write_text(json.dumps({"token": "MANIFEST-SENTINEL", "iconPath": "ui/icon.svg"}))
        _fake_fetch(monkeypatch, ART, manifest=None)
        real_fetch = reg_mod._git_fetch_branch

        async def _fetch_with_link(git_url, branch, dest, log_lines, **kw):
            result = await real_fetch(git_url, branch, dest, log_lines, **kw)
            (Path(dest) / "app.json").symlink_to(secret)
            return result

        monkeypatch.setattr(reg_mod, "_git_fetch_branch", _fetch_with_link)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert _read_manifest_cache(entry) is None
        assert not _manifest_cache_path(entry).exists()
        # The sentinel appears nowhere the prewarm wrote.
        for path in list(_manifest_cache_path(entry).parent.rglob("*")) + list(
            _blob_cache_dir().rglob("*")
        ):
            if path.is_file():
                assert b"MANIFEST-SENTINEL" not in path.read_bytes()

    @pytest.mark.asyncio
    async def test_a_hard_linked_app_json_is_refused(self, monkeypatch, tmp_path):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        elsewhere = tmp_path / "elsewhere.json"
        elsewhere.write_text(json.dumps(dict(MANIFEST)))
        _fake_fetch(monkeypatch, ART, manifest=None)
        real_fetch = reg_mod._git_fetch_branch

        async def _fetch_with_hardlink(git_url, branch, dest, log_lines, **kw):
            result = await real_fetch(git_url, branch, dest, log_lines, **kw)
            try:
                os.link(elsewhere, Path(dest) / "app.json")  # st_nlink == 2
            except OSError:
                pytest.skip("hard links not supported on this filesystem")
            return result

        monkeypatch.setattr(reg_mod, "_git_fetch_branch", _fetch_with_hardlink)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert _read_manifest_cache(entry) is None

    @pytest.mark.asyncio
    async def test_a_regular_app_json_reads_unchanged(self, monkeypatch):
        """A plain regular ``app.json`` is read byte-identically through the pinned
        walk and its every declared art file lands -- the read path change is
        transparent to the normal case."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _fake_fetch(monkeypatch, ART)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert len(calls) == 1
        assert _read_manifest_cache(entry) == MANIFEST
        for rel in ART:
            assert _store_art_cache_path(entry, rel).is_file()

    @requires_symlinks
    @pytest.mark.asyncio
    async def test_the_pinned_manifest_read_can_fail_negative_control(self, monkeypatch, tmp_path):
        """Negative control: reverting to a by-path follow read makes the SAME
        symlinked ``app.json`` be followed and its target land in the manifest cache --
        so the refusal above observes the pinned read, not an accident of the fixture."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        secret = tmp_path / "credentials.json"
        secret.write_text(json.dumps({"token": "MANIFEST-SENTINEL"}))
        _fake_fetch(monkeypatch, ART, manifest=None)
        real_fetch = reg_mod._git_fetch_branch

        async def _fetch_with_link(git_url, branch, dest, log_lines, **kw):
            result = await real_fetch(git_url, branch, dest, log_lines, **kw)
            (Path(dest) / "app.json").symlink_to(secret)
            return result

        monkeypatch.setattr(reg_mod, "_git_fetch_branch", _fetch_with_link)

        def _by_path(root, rel_parts, *, max_bytes=None):
            src = (root / Path(*rel_parts)).resolve()
            return src.read_bytes() if src.is_file() else None

        monkeypatch.setattr(reg_mod, "_open_pinned_asset", _by_path)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert _read_manifest_cache(entry) == {"token": "MANIFEST-SENTINEL"}

    def test_the_max_bytes_ceiling_is_inclusive_for_the_manifest(self, tmp_path, monkeypatch):
        """The pinned read validates the opened descriptor against the caller's
        ``max_bytes``: exactly the ceiling is accepted, one byte over refused."""
        monkeypatch.setattr(reg_mod, "_MANIFEST_MAX_BYTES", 16)
        clone = tmp_path / "clone"
        clone.mkdir(parents=True, exist_ok=True)
        (clone / "app.json").write_bytes(b"x" * 16)
        assert reg_mod._open_pinned_asset(clone, ("app.json",), max_bytes=16) == b"x" * 16
        (clone / "app.json").write_bytes(b"x" * 17)
        assert reg_mod._open_pinned_asset(clone, ("app.json",), max_bytes=16) is None


# ---------------------------------------------------------------------------
# The manifest-cache sidecars are read once through a pinned no-follow descriptor
# ---------------------------------------------------------------------------


class TestSidecarPinnedRead:
    """The ``.unobtainable``/``.clone-failed`` records live beside the manifest cache
    in the agent-writable ``by-source/`` directory. ``_read_pinned_sidecar`` opens each
    ONCE ``O_NOFOLLOW`` under its pinned parent, validates the opened descriptor
    (regular, single link, no larger than the ceiling) and reads at most the ceiling
    plus one byte -- closing the size-check-then-read race a by-name
    ``stat``/``read_text`` had, and refusing a symlinked sidecar rather than following
    it. Every refusal is a ``None`` return, which the record readers treat as absent."""

    def test_a_small_regular_sidecar_reads_with_its_mtime(self, tmp_path):
        path = tmp_path / "rec.json"
        path.write_text('["a", "b"]', encoding="utf-8")
        result = reg_mod._read_pinned_sidecar(path, max_bytes=1024)
        assert result is not None
        data, mtime = result
        assert data == b'["a", "b"]'
        assert mtime == pytest.approx(path.stat().st_mtime)

    def test_an_oversize_sidecar_is_refused(self, tmp_path):
        path = tmp_path / "rec.json"
        path.write_bytes(b"x" * 17)
        assert reg_mod._read_pinned_sidecar(path, max_bytes=16) is None
        # Exactly the ceiling is accepted (inclusive).
        path.write_bytes(b"x" * 16)
        result = reg_mod._read_pinned_sidecar(path, max_bytes=16)
        assert result is not None and result[0] == b"x" * 16

    @requires_symlinks
    def test_a_symlinked_sidecar_is_refused(self, tmp_path):
        secret = tmp_path / "secret.json"
        secret.write_text('["SENTINEL"]', encoding="utf-8")
        link = tmp_path / "rec.json"
        link.symlink_to(secret)
        assert reg_mod._read_pinned_sidecar(link, max_bytes=1024) is None

    def test_a_hard_linked_sidecar_is_refused(self, tmp_path):
        real = tmp_path / "rec.json"
        real.write_text('["a"]', encoding="utf-8")
        alias = tmp_path / "alias.json"
        try:
            os.link(real, alias)  # st_nlink == 2
        except OSError:
            pytest.skip("hard links not supported on this filesystem")
        assert reg_mod._read_pinned_sidecar(real, max_bytes=1024) is None

    def test_a_missing_sidecar_is_none(self, tmp_path):
        assert reg_mod._read_pinned_sidecar(tmp_path / "absent.json", max_bytes=1024) is None

    @requires_symlinks
    @pytest.mark.asyncio
    async def test_an_oversize_unobtainable_record_keeps_the_row_cold(self, monkeypatch):
        """Integration: an oversize ``.unobtainable`` record swapped in reads as the
        empty set, so the declared-missing path is NOT counted satisfied and the row
        stays cold (re-cloned) rather than the reader allocating the oversize file."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        manifest = dict(MANIFEST, screenshots=["ui/missing.png"])
        calls = _fake_fetch(monkeypatch, ART, manifest)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert len(calls) == 1
        record = reg_mod._unobtainable_art_path(entry)
        assert json.loads(record.read_text(encoding="utf-8")) == ["ui/missing.png"]

        # Swap the record for one past the ceiling: the read refuses it (empty set), so
        # ui/missing.png is not satisfied and the row is cold again.
        monkeypatch.setattr(reg_mod, "_MANIFEST_MAX_BYTES", 8)
        record.write_text(json.dumps(["ui/missing.png"]) + " " * 64, encoding="utf-8")
        manifest_mtime = _manifest_cache_path(entry).stat().st_mtime
        assert reg_mod._read_unobtainable_art(entry, manifest_mtime) == frozenset()

    @requires_symlinks
    def test_a_symlinked_clone_failure_record_reads_as_absent(self, monkeypatch, tmp_path):
        """Integration: a symlinked ``.clone-failed`` record is refused, so the backoff
        reads as absent (retry) rather than following the link."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        entry = _entry()
        record = reg_mod._clone_failure_path(entry)
        record.parent.mkdir(parents=True, exist_ok=True)
        secret = tmp_path / "secret.json"
        secret.write_text(json.dumps({"at": time.time(), "reason": "x"}), encoding="utf-8")
        record.symlink_to(secret)
        assert reg_mod._read_clone_failure(entry) is None


# ---------------------------------------------------------------------------
# The manifest-cache source coordinates degrade a hostile row to safe defaults
# ---------------------------------------------------------------------------


class TestManifestSourceCoordinatesDegradeHostileValues:
    """``_manifest_source_coordinates`` feeds a cache KEY, so every value an
    external index controls must degrade to a safe, distinct-but-harmless default
    when it is not the expected type -- never crash the derivation. A row whose
    ``name``, ``branch`` and ``subdirectory`` are all non-strings (and an empty
    branch) exercises each defensive branch at once."""

    def test_non_string_name_branch_and_subdirectory_fall_back(self):
        coords = _manifest_source_coordinates(
            {
                "name": 123,
                "gitUrl": SIBLING,
                "repo": SIBLING,
                "branch": 456,
                "subdirectory": 789,
            }
        )
        origin, ref, subdirectory, name = coords
        # The origin is still the normalized credential-free clone URL.
        assert origin == SIBLING.removesuffix(".git")
        # Each non-string coordinate collapsed to its safe default.
        assert name == ""
        assert ref == "branch:main"  # a non-string branch defaults to main
        assert subdirectory == ""

    def test_an_empty_branch_also_defaults_to_main(self):
        """An empty-string branch is as unusable as a non-string one and takes the
        same ``main`` default, so the two share one identity component."""
        _origin, ref, _subdir, name = _manifest_source_coordinates(
            {"name": "app", "gitUrl": SIBLING, "repo": SIBLING, "branch": "", "subdirectory": ""}
        )
        assert ref == "branch:main"
        assert name == "app"

    def test_a_well_formed_row_is_unchanged_negative_control(self):
        """Negative control: a row with ordinary string coordinates keeps them, so
        the fallbacks above observe the bad types and not an unconditional rewrite."""
        _origin, ref, subdirectory, name = _manifest_source_coordinates(
            {
                "name": "app",
                "gitUrl": SIBLING,
                "repo": SIBLING,
                "branch": "dev",
                "subdirectory": "apps/app",
            }
        )
        assert name == "app"
        assert ref == "branch:dev"
        assert subdirectory == "apps/app"
