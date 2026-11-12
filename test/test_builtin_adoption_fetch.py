"""STAGE fetch and the tree digest (matrix rows 6, 17, 22, 23). Offline: local git + in-memory archive."""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import tarfile
import threading
import urllib.error

import pytest
from builtin_adoption_helpers import (
    NAME,
    REPO,
    build_world,
    make_archive,
    requires_git,
    write_tree,
)

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.apps import builtin_adoption_fetch as fetch_mod
from kiro_crew.apps.builtin_adoption_fetch import (
    AdoptionRefused,
    StagedArtifact,
    adoption_tree_digest,
    materialize_verified_tree,
    stage_pin,
)
from kiro_crew.apps.builtin_adoption_pins import adoption_pin

pytestmark = requires_git

#: The real fetch, bound before ``build_world`` replaces it with the local-repository one.
_ORIGINAL_GIT_FETCH = fetch_mod._git_fetch


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=60))


# --------------------------------------------------------------------------- #
# Row 22: the tree digest
# --------------------------------------------------------------------------- #


def test_the_git_and_archive_paths_yield_the_same_digest(tmp_path, monkeypatch):
    world = build_world(tmp_path, monkeypatch)
    pin = adoption_pin(NAME)
    assert pin is not None
    checkout = tmp_path / "checkout"
    _run(world.git_fetch(pin, checkout))
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(world.archive)
    from_git, from_archive = tmp_path / "from-git", tmp_path / "from-archive"
    materialize_verified_tree(pin, StagedArtifact("git", checkout), from_git)
    materialize_verified_tree(pin, StagedArtifact("archive", archive), from_archive)
    assert adoption_tree_digest(from_git) == adoption_tree_digest(from_archive) == pin.tree_sha256
    assert not (from_git / ".git").exists()


def test_the_digest_is_path_independent_and_content_sensitive(tmp_path):
    a = write_tree(tmp_path / "a", {"name": "x"})
    b = write_tree(tmp_path / "elsewhere" / "b", {"name": "x"})
    assert adoption_tree_digest(a) == adoption_tree_digest(b)
    (b / "backend" / "routes.py").write_text("ROUTES = [1]\n", encoding="utf-8")
    assert adoption_tree_digest(a) != adoption_tree_digest(b)


def test_only_the_top_level_git_directory_is_skipped(tmp_path):
    a = write_tree(tmp_path / "a", {"name": "x"})
    before = adoption_tree_digest(a)
    (a / ".git").mkdir()
    (a / ".git" / "HEAD").write_text("ref: x\n", encoding="utf-8")
    assert adoption_tree_digest(a) == before
    (a / "backend" / ".git").mkdir()
    (a / "backend" / ".git" / "x").write_text("x", encoding="utf-8")
    assert adoption_tree_digest(a) != before


@pytest.mark.skipif(
    not platform_compat.IS_POSIX, reason="symlink creation needs privilege on Windows"
)
def test_a_symlink_refuses_the_tree(tmp_path):
    a = write_tree(tmp_path / "a", {"name": "x"})
    os.symlink(a / "app.json", a / "link.json")
    with pytest.raises(AdoptionRefused) as refused:
        adoption_tree_digest(a)
    assert refused.value.code == "tree_has_link"


@pytest.mark.skipif(platform_compat.IS_POSIX, reason="junctions exist only on Windows")
def test_a_junction_refuses_the_tree(tmp_path):
    from kiro_crew.testing.links import make_dir_link

    a = write_tree(tmp_path / "a", {"name": "x"})
    target = tmp_path / "target"
    target.mkdir()
    make_dir_link(a / "junction", target)
    with pytest.raises(AdoptionRefused) as refused:
        adoption_tree_digest(a)
    assert refused.value.code == "tree_has_link"


@pytest.mark.parametrize("bad", ["con.txt", "nul", "trailing.", "aux.json"])
def test_a_reserved_or_unportable_name_refuses_the_tree(tmp_path, bad):
    if platform_compat.IS_WINDOWS and bad in ("con.txt", "nul", "aux.json", "trailing."):
        pytest.skip("Windows cannot create this name to test it")
    a = write_tree(tmp_path / "a", {"name": "x"})
    (a / bad).write_text("x", encoding="utf-8")
    with pytest.raises(AdoptionRefused) as refused:
        adoption_tree_digest(a)
    assert refused.value.code == "tree_unportable_name"


def test_a_case_collision_refuses_the_tree(tmp_path):
    a = write_tree(tmp_path / "a", {"name": "x"})
    (a / "Readme.md").write_text("1", encoding="utf-8")
    if (a / "README.md").exists():
        pytest.skip("case-insensitive filesystem cannot hold the collision")
    (a / "README.md").write_text("2", encoding="utf-8")
    with pytest.raises(AdoptionRefused) as refused:
        adoption_tree_digest(a)
    assert refused.value.code == "tree_unportable_name"


def test_an_over_cap_tree_is_refused(tmp_path, monkeypatch):
    a = write_tree(tmp_path / "a", {"name": "x"})
    monkeypatch.setattr(fetch_mod, "MAX_TREE_FILES", 2)
    with pytest.raises(AdoptionRefused) as refused:
        adoption_tree_digest(a)
    assert refused.value.code == "tree_too_large"
    monkeypatch.setattr(fetch_mod, "MAX_TREE_FILES", 5000)
    monkeypatch.setattr(fetch_mod, "MAX_TREE_BYTES", 10)
    with pytest.raises(AdoptionRefused) as refused:
        adoption_tree_digest(a)
    assert refused.value.code == "tree_too_large"


# --------------------------------------------------------------------------- #
# Rows 17, 23: materializing an archive, and a tampered staged tree
# --------------------------------------------------------------------------- #


def _tar(members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for member, data in members:
            tar.addfile(member, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


def _member(name, kind=tarfile.REGTYPE, size=0, linkname=""):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.size = size
    info.linkname = linkname
    return info


@pytest.mark.parametrize(
    "members",
    [
        [(_member("ROOT/evil", tarfile.SYMTYPE, linkname="/etc/passwd"), None)],
        [(_member("ROOT/hard", tarfile.LNKTYPE, linkname="ROOT/app.json"), None)],
        [(_member("ROOT/../escape", size=1), b"x")],
        [(_member("/abs", size=1), b"x")],
        [(_member("other-root/app.json", size=1), b"x")],
        [(_member("ROOT/dev", tarfile.CHRTYPE), None)],
    ],
)
def test_archive_extraction_refuses_a_hostile_member(tmp_path, monkeypatch, members):
    world = build_world(tmp_path, monkeypatch)
    pin = adoption_pin(NAME)
    assert pin is not None
    root = f"{REPO}-{world.commit}"
    fixed = []
    for member, data in members:
        member.name = member.name.replace("ROOT", root)
        member.linkname = member.linkname.replace("ROOT", root)
        fixed.append((member, data))
    archive = tmp_path / "hostile.tar.gz"
    archive.write_bytes(_tar(fixed))
    with pytest.raises(AdoptionRefused) as refused:
        fetch_mod._extract_archive(archive, tmp_path / "out", root=root)
    assert refused.value.code in ("tree_has_link", "tree_unportable_name")
    assert not (tmp_path / "escape").exists()


def test_a_staged_tree_changed_after_staging_fails_on_the_copy(tmp_path, monkeypatch):
    world = build_world(tmp_path, monkeypatch)
    pin = adoption_pin(NAME)
    assert pin is not None
    checkout = tmp_path / "checkout"
    _run(world.git_fetch(pin, checkout))
    (checkout / "backend" / "routes.py").write_text("import os; os.system('x')\n", encoding="utf-8")
    with pytest.raises(AdoptionRefused) as refused:
        materialize_verified_tree(pin, StagedArtifact("git", checkout), tmp_path / "copy")
    assert refused.value.code == "tree_digest_mismatch"


def test_a_staged_archive_changed_after_staging_fails_its_digest(tmp_path, monkeypatch):
    world = build_world(tmp_path, monkeypatch)
    pin = adoption_pin(NAME)
    assert pin is not None
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(make_archive(world.tree, world.commit) + b"\0")
    with pytest.raises(AdoptionRefused) as refused:
        materialize_verified_tree(pin, StagedArtifact("archive", archive), tmp_path / "copy")
    assert refused.value.code == "archive_digest_mismatch"


# --------------------------------------------------------------------------- #
# Row 6: no git on the desktop, and the archive fallback
# --------------------------------------------------------------------------- #


def test_without_git_the_archive_path_stages_and_verifies(tmp_path, monkeypatch):
    world = build_world(tmp_path, monkeypatch)
    pin = adoption_pin(NAME)
    assert pin is not None
    result = _run(stage_pin(pin, git_available=False))
    assert result.ok and result.staged is not None and result.staged.kind == "archive"
    assert world.git_calls == []
    assert world.archive_calls == [pin.archive_url]
    assert result.staged.path.read_bytes() == world.archive


def test_an_archive_digest_mismatch_blocks(tmp_path, monkeypatch):
    world = build_world(tmp_path, monkeypatch)
    world.archive_body = world.archive + b"tampered"
    pin = adoption_pin(NAME)
    assert pin is not None
    result = _run(stage_pin(pin, git_available=False))
    assert not result.ok and result.code == "archive_digest_mismatch"
    assert not (fetch_mod.adoption_dir() / f"{NAME}-{world.commit}.tar.gz").exists()


@pytest.fixture
def harness_pin(tmp_path, monkeypatch):
    """Build a pin read from a harness table whose ``harnessArchive`` holds a body."""
    from builtin_adoption_helpers import pin_row

    from kiro_crew.apps import builtin_adoption_pins as pins_mod

    def make(world, body):
        archive = tmp_path / "harness" / "fixture.tar.gz"
        archive.parent.mkdir()
        archive.write_bytes(body)
        row = pin_row(world.commit, adoption_tree_digest(world.tree), world.archive_sha)
        row["harnessArchive"] = str(archive)
        table = tmp_path / "harness-pins.json"
        table.write_text(json.dumps({"schemaVersion": 1, "pins": [row]}), encoding="utf-8")
        monkeypatch.setenv(pins_mod.HARNESS_PINS_ENV, str(table))
        pins_mod.load_adoption_pins.cache_clear()
        pin = adoption_pin(NAME)
        assert pin is not None and pin.harness_archive == str(archive)
        return pin

    yield make
    pins_mod.load_adoption_pins.cache_clear()


def test_a_harness_archive_pin_stages_from_its_file_and_never_fetches(
    tmp_path, monkeypatch, harness_pin
):
    world = build_world(tmp_path, monkeypatch)
    pin = harness_pin(world, world.archive)
    result = _run(stage_pin(pin, git_available=True))
    assert result.ok and result.staged is not None and result.staged.kind == "archive"
    assert world.git_calls == [] and world.archive_calls == []
    assert result.staged.path.read_bytes() == world.archive


def test_a_harness_archive_is_held_to_the_pin_digest(tmp_path, monkeypatch, harness_pin):
    world = build_world(tmp_path, monkeypatch)
    pin = harness_pin(world, world.archive + b"tampered")
    result = _run(stage_pin(pin, git_available=True))
    assert not result.ok and result.code == "archive_digest_mismatch"
    assert world.git_calls == [] and world.archive_calls == []


def test_a_git_network_failure_falls_back_to_the_archive_in_the_same_attempt(tmp_path, monkeypatch):
    world = build_world(tmp_path, monkeypatch)
    world.git_outcome = "network"
    pin = adoption_pin(NAME)
    assert pin is not None
    result = _run(stage_pin(pin, git_available=True))
    assert result.ok and result.staged is not None and result.staged.kind == "archive"
    assert world.git_calls == [pin.commit] and len(world.archive_calls) == 1


def test_a_git_missing_ref_is_commit_not_found(tmp_path, monkeypatch):
    world = build_world(tmp_path, monkeypatch)
    world.git_outcome = "commit_not_found"
    pin = adoption_pin(NAME)
    assert pin is not None
    result = _run(stage_pin(pin, git_available=True))
    assert result.code == "commit_not_found" and world.archive_calls == []


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (urllib.error.HTTPError("u", 404, "nf", {}, None), "commit_not_found"),
        (urllib.error.HTTPError("u", 410, "gone", {}, None), "commit_not_found"),
        (urllib.error.HTTPError("u", 429, "slow", {}, None), "http_status"),
        (urllib.error.HTTPError("u", 503, "down", {}, None), "http_status"),
        (urllib.error.HTTPError("u", 302, "redirect", {}, None), "http_status"),
        (urllib.error.URLError(OSError("unreachable")), "network_unreachable"),
        (urllib.error.URLError(TimeoutError()), "network_timeout"),
    ],
)
def test_archive_failures_map_to_their_reason_codes(tmp_path, monkeypatch, error, code):
    world = build_world(tmp_path, monkeypatch)
    world.archive_error = error
    pin = adoption_pin(NAME)
    assert pin is not None
    assert _run(stage_pin(pin, git_available=False)).code == code


def test_the_archive_read_is_capped(tmp_path, monkeypatch):
    world = build_world(tmp_path, monkeypatch)
    monkeypatch.setattr(fetch_mod, "MAX_ARCHIVE_BYTES", 16)
    pin = adoption_pin(NAME)
    assert pin is not None
    assert _run(stage_pin(pin, git_available=False)).code == "tree_too_large"
    assert world.archive_calls


def test_redirects_are_refused_by_the_real_opener():
    handler = fetch_mod._NoRedirects()

    class _Req:
        full_url = "https://codeload.github.com/x/y/tar.gz/z"

    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(_Req(), None, 302, "Found", {}, "http://127.0.0.1/")


def test_the_staged_verify_copy_is_removed(tmp_path, monkeypatch):
    build_world(tmp_path, monkeypatch)
    pin = adoption_pin(NAME)
    assert pin is not None
    seen = []

    def verify(tree):
        seen.append(tree)
        return None

    assert _run(stage_pin(pin, git_available=True, verify=verify)).ok
    assert seen and not seen[0].exists()
    leftovers = [p for p in fetch_mod.adoption_dir().iterdir() if p.name.startswith(".verify")]
    assert leftovers == []
    shutil.rmtree(fetch_mod.adoption_dir(), ignore_errors=True)


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="a plain symlink needs no privilege")
def test_a_file_swapped_for_a_link_after_the_walk_refuses_at_its_open(tmp_path, monkeypatch):
    source = write_tree(tmp_path / "source", {"name": "x"})
    secret = tmp_path / "secret"
    secret.write_text("token", encoding="utf-8")
    walked = fetch_mod._walk_tree(source)
    (source / "app.json").unlink()
    os.symlink(secret, source / "app.json")
    monkeypatch.setattr(fetch_mod, "_walk_tree", lambda _root: walked)
    # The by-name copy, the one a platform without a descriptor walk takes.
    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(AdoptionRefused) as refused:
        fetch_mod._copy_checkout(source, dest)
    assert refused.value.code == "tree_has_link"
    assert not (dest / "app.json").exists()


@pytest.mark.skipif(not pinned_fs.supports_pinned_tree_walk(), reason="no fd walk")
def test_a_directory_swapped_for_a_link_mid_copy_is_never_followed(tmp_path, monkeypatch):
    source = write_tree(tmp_path / "source", {"name": "x"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "routes.py").write_text("token", encoding="utf-8")
    genuine = (source / "backend" / "routes.py").read_text(encoding="utf-8")
    real_check = fetch_mod._check_portable

    def swap_after_listing(rel, seen):
        if rel == "backend/routes.py" and not (tmp_path / "moved").exists():
            (source / "backend").rename(tmp_path / "moved")
            os.symlink(outside, source / "backend", target_is_directory=True)
        real_check(rel, seen)

    monkeypatch.setattr(fetch_mod, "_check_portable", swap_after_listing)
    dest = tmp_path / "dest"
    dest.mkdir()
    fetch_mod._copy_checkout(source, dest)
    assert (dest / "backend" / "routes.py").read_text(encoding="utf-8") == genuine


@pytest.mark.skipif(not pinned_fs.supports_pinned_tree_walk(), reason="no fd walk")
def test_a_link_on_the_chain_below_the_home_refuses_the_copy(tmp_path):
    real = write_tree(tmp_path / "real" / "src", {"name": "x"}).parent
    linked = fetch_mod.adoption_dir().parent / "linked-sources"
    os.symlink(real, linked, target_is_directory=True)
    dest = tmp_path / "dest"
    dest.mkdir()
    try:
        with pytest.raises(AdoptionRefused) as refused:
            fetch_mod._copy_checkout(linked / "src", dest)
    finally:
        linked.unlink()
    assert refused.value.code == "tree_has_link"
    assert list(dest.iterdir()) == []


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="a plain symlink needs no privilege")
def test_a_root_that_is_a_link_refuses_the_tree(tmp_path):
    tree = write_tree(tmp_path / "tree", {"name": "x"})
    os.symlink(tree, tmp_path / "link", target_is_directory=True)
    with pytest.raises(AdoptionRefused) as refused:
        adoption_tree_digest(tmp_path / "link")
    assert refused.value.code == "tree_has_link"


def test_a_full_disk_while_copying_the_checkout_is_disk_full(tmp_path, monkeypatch):
    import errno

    source = write_tree(tmp_path / "source", {"name": "x"})

    def full(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(fetch_mod, "_copy_checkout_pinned", full)
    monkeypatch.setattr(fetch_mod, "_copy_checkout_by_name", full)
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(AdoptionRefused) as refused:
        fetch_mod._copy_checkout(source, dest)
    assert refused.value.code == "disk_full"


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="a plain symlink needs no privilege")
def test_the_archive_download_never_writes_through_a_planted_link(tmp_path, monkeypatch):
    world = build_world(tmp_path, monkeypatch)
    pin = adoption_pin(NAME)
    assert pin is not None
    dest = fetch_mod.staged_location(pin, "archive")
    dest.parent.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "victim"
    victim.write_text("untouched", encoding="utf-8")
    os.symlink(victim, dest.with_name(f"{dest.name}.part-{os.getpid()}"))
    result = _run(stage_pin(pin, git_available=False))
    assert result.ok and result.staged is not None
    assert result.staged.path.read_bytes() == world.archive
    assert victim.read_text(encoding="utf-8") == "untouched"


@pytest.mark.parametrize("raises", [False, True])
def test_moved_aside_checkouts_are_removed_off_the_event_loop(tmp_path, monkeypatch, raises):
    from kiro_crew.apps.registry_pipeline import checkout as checkout_mod

    build_world(tmp_path, monkeypatch)
    pin = adoption_pin(NAME)
    assert pin is not None
    aside = tmp_path / "aside"
    (aside / ".git").mkdir(parents=True)
    removed_on = []
    real = checkout_mod._rmtree_force_settled

    async def recording(path):
        removed_on.append(path)
        await real(path)

    async def fake_clone(*_args, pending_cleanup, **_kwargs):
        pending_cleanup.append(aside)
        if raises:
            raise OSError("clone failed")
        return {"error": "unreachable"}

    real_rmtree = shutil.rmtree

    def no_rmtree_on_the_loop(*args, **kwargs):
        if threading.current_thread() is threading.main_thread():
            raise AssertionError("shutil.rmtree ran on the event loop")
        return real_rmtree(*args, **kwargs)

    monkeypatch.setattr(checkout_mod, "_git_clone_or_pull", fake_clone)
    monkeypatch.setattr(checkout_mod, "_rmtree_force_settled", recording)
    monkeypatch.setattr(shutil, "rmtree", no_rmtree_on_the_loop)
    assert _run(_ORIGINAL_GIT_FETCH(pin, tmp_path / "dest")) == "network"
    assert removed_on == [aside]
    assert not aside.exists()
