"""STAGE fetch and the tree digest (matrix rows 6, 17, 22, 23). Offline: local git + in-memory archive."""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import tarfile
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

from kiro_crew import platform_compat
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
