"""Regression tests for the descriptor-pinned dashboard write migration.

Covers the steering create/update/delete blocking transactions and the file-write
blocking transaction after their move onto ``pinned_fs``:

* create/delete address the leaf relative to a pinned parent descriptor and keep
  their error tokens (``exists``/``writefailed``/``notfound``/``deletefailed``);
* update and file-write route their ``atomic_write`` through the pinned-parent
  mode while preserving the ACL carry, byte-exactness (``newline=""``) and mode;
* the by-name floor still produces the same results when the capability probe is
  forced False (the Windows path).

NOT EXECUTED IN THE INTEGRATIONS_ONLY SANDBOX. Importing the dashboard handler
modules pulls ``aiohttp`` (uninstallable offline, pip 403) and the ``croniter`` /
``snowballstemmer`` chain, so these run in CI only.

CI invocation:

    python -m pytest test/test_dashboard_pinned_write_migration.py
"""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import kiro_crew.dashboard.handlers.files as files_mod
import kiro_crew.dashboard.handlers.prompts as prompts_mod
import kiro_crew.dashboard.handlers.steering as steering_mod
from kiro_crew.dashboard.handlers.files import _file_write_blocking
from kiro_crew.dashboard.handlers.steering import (
    _create_file_blocking,
    _delete_file_blocking,
    _update_file_blocking,
)


def _needs_openat() -> None:
    if not steering_mod._DIR_FD_SUPPORTED:  # pragma: no cover - CI runs POSIX
        pytest.skip("platform without openat")


# ── steering create ──


def test_steering_create_writes_byte_exact(tmp_path):
    _needs_openat()
    target = tmp_path / "steering" / "rules.md"
    body = "line one\nline two\n"
    err, _display = _create_file_blocking(target, body)
    assert err is None
    assert target.read_bytes() == body.encode("utf-8")
    # 0o600 mode preserved on the pinned path.
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_steering_create_refuses_existing_with_exists_token(tmp_path):
    _needs_openat()
    target = tmp_path / "steering" / "rules.md"
    assert _create_file_blocking(target, "first")[0] is None
    err, _ = _create_file_blocking(target, "second")
    assert err == "exists"
    assert target.read_text(encoding="utf-8") == "first"


def test_steering_create_by_name_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(steering_mod, "_DIR_FD_SUPPORTED", False)
    target = tmp_path / "steering" / "rules.md"
    err, _ = _create_file_blocking(target, "body")
    assert err is None
    assert target.read_text(encoding="utf-8") == "body"


def test_a_transient_identity_probe_failure_still_leaves_no_name_behind(tmp_path, monkeypatch):
    """A one-off ``fstat`` failure is re-asked through the descriptor, so the name goes.

    The rollback verifies identity, so it needs one — and capturing it is a syscall
    that can fail (EIO, ESTALE on a network filesystem). The descriptor is still open
    at that point, so the identity is asked for once more through it; an EIO on a
    network filesystem is usually transient, and the second answer puts the rollback
    back on its verified arm instead of stranding the ``O_EXCL`` name for every retry
    to trip over. The re-probe addresses a DESCRIPTOR, so it can never answer with a
    different object.
    """
    _needs_openat()
    target = tmp_path / "steering" / "rules.md"
    real_fstat = os.fstat
    failed_once: list[int] = []

    def fail_the_leaf_probe_once(fd):
        info = real_fstat(fd)
        if stat.S_ISREG(info.st_mode) and not failed_once:
            failed_once.append(fd)
            raise OSError(errno.EIO, "identity probe failed")
        return info

    monkeypatch.setattr(os, "fstat", fail_the_leaf_probe_once)
    err, _display = _create_file_blocking(target, "BODY")
    monkeypatch.undo()

    assert failed_once, "the identity probe was never exercised"
    assert err == "writefailed"
    assert not target.exists(), "the empty O_EXCL name survived a recovered identity probe"
    # And the retry is a create again rather than a permanent "exists".
    assert _create_file_blocking(target, "second try")[0] is None
    assert target.read_text(encoding="utf-8") == "second try"


def test_no_identity_at_all_leaves_an_empty_document_and_never_unlinks(tmp_path, monkeypatch):
    """With BOTH probes failing there is no identity, so nothing is unlinked by name.

    This is the one arm that cannot reach a verified unlink, and the two demands on it
    are opposed: removing the name spares the caller a permanent ``exists``, and not
    removing it spares a rival's file. Only one of them is a data loss, so the name
    stays — and what it costs is bounded to an EMPTY document, because the identity
    probe precedes the first ``os.write``. No partial or truncated body can survive
    here, which is the property the other rollback arms buy with their unlink.
    """
    _needs_openat()
    target = tmp_path / "steering" / "rules.md"
    real_fstat = os.fstat
    unlinked: list[str] = []
    real_unlink = os.unlink

    def always_fail_the_leaf_probe(fd):
        info = real_fstat(fd)
        if stat.S_ISREG(info.st_mode):
            raise OSError(errno.EIO, "identity probe failed")
        return info

    def record_unlink(path, *args, **kwargs):
        unlinked.append(str(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "fstat", always_fail_the_leaf_probe)
    monkeypatch.setattr(os, "unlink", record_unlink)
    err, _display = _create_file_blocking(target, "BODY")
    monkeypatch.setattr(os, "fstat", real_fstat)
    monkeypatch.setattr(os, "unlink", real_unlink)

    assert err == "writefailed"
    assert unlinked == [], f"the rollback unlinked a name with no identity to verify: {unlinked}"
    # The name is left, and what is at it is empty rather than a partial body.
    assert target.exists()
    assert target.read_bytes() == b""
    # Recovery is a save through the update path, not a shell: the empty document is a
    # regular file the atomic replace overwrites in place.
    assert _update_file_blocking(target, "recovered") is None
    assert target.read_text(encoding="utf-8") == "recovered"


def test_no_identity_at_all_spares_a_rivals_replacement(tmp_path, monkeypatch):
    """The no-identity arm cannot reach a rival's file, because it unlinks nothing.

    A descriptor pins the DIRECTORY, not the entry inside it. With no captured inode
    there is nothing to verify a by-name unlink against, so a rival that took the name
    inside the failure window would lose its file to a best-effort ``os.unlink`` — a
    data loss in the arm that exists to prevent one.
    """
    _needs_openat()
    target = tmp_path / "steering" / "rules.md"
    real_fstat = os.fstat

    def swap_the_leaf_then_fail_the_probe(fd):
        info = real_fstat(fd)
        if stat.S_ISREG(info.st_mode):
            # The rival replaces the name in the same instant the probe fails, so no
            # timing is involved.
            target.unlink()
            target.write_text("RIVAL", encoding="utf-8")
            raise OSError(errno.EIO, "identity probe failed")
        return info

    monkeypatch.setattr(os, "fstat", swap_the_leaf_then_fail_the_probe)
    err, _display = _create_file_blocking(target, "BODY")
    monkeypatch.setattr(os, "fstat", real_fstat)

    assert err == "writefailed"
    assert target.read_text(encoding="utf-8") == "RIVAL", "the rollback ate the rival's file"


def test_the_create_rollback_does_not_delete_a_rivals_replacement(tmp_path, monkeypatch):
    """The rollback removes the inode it created, not whatever answers to the name.

    The cleanup addresses ``target.name`` under a descriptor, and a descriptor pins
    the DIRECTORY, not the name inside it. A rival that unlinks our ``O_EXCL`` leaf
    and creates its own inside the failure window would lose its file to a bare
    ``os.unlink`` — a silent data loss in the arm that exists to prevent one.
    Verifying ``(st_dev, st_ino)`` makes the cleanup remove this object or nothing.

    The rival is injected at the moment of failure: the ``os.write`` that fails is
    what swaps the leaf, so no timing is involved.
    """
    _needs_openat()
    target = tmp_path / "steering" / "rules.md"
    real_write = os.write

    def swap_the_leaf_then_fail(fd, data):
        if b"MINE" in bytes(data):
            target.unlink()
            target.write_text("RIVAL", encoding="utf-8")
            raise OSError(errno.ENOSPC, "no space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", swap_the_leaf_then_fail)
    err, _display = _create_file_blocking(target, "MINE")
    monkeypatch.undo()

    assert err == "writefailed"
    assert target.read_text(encoding="utf-8") == "RIVAL", "the rollback ate the rival's file"


def test_a_create_whose_close_fails_leaves_no_partial_file(tmp_path, monkeypatch):
    """A close() error runs the cleanup arm, so a retry is a create and not "exists".

    ``close`` is where a deferred write error surfaces — ENOSPC once the last block
    is flushed, EIO on NFS — so it is not covered by guarding the write loop alone.
    With the close in a ``finally`` it raises AFTER the cleanup arm is skipped, and
    the partial body stays under the ``O_EXCL`` name: every retry then answers
    ``exists`` forever over a truncated document, with no way out through the API.

    The failure is injected only for the leaf's own descriptor, so the pinned parent
    fd and every unrelated close still work.
    """
    _needs_openat()
    target = tmp_path / "steering" / "rules.md"
    real_close = os.close
    leaf_fds: list[int] = []

    real_open = os.open

    def remember(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        if args and args[0] == target.name:
            leaf_fds.append(fd)
        return fd

    def fail_leaf_close(fd):
        if fd in leaf_fds:
            real_close(fd)
            raise OSError(errno.ENOSPC, "no space left on device")
        return real_close(fd)

    monkeypatch.setattr(os, "open", remember)
    monkeypatch.setattr(os, "close", fail_leaf_close)
    err, _display = _create_file_blocking(target, "BODY")
    monkeypatch.undo()

    assert err == "writefailed"
    assert not target.exists(), "the partial body survived a failed close"
    # And the retry is a create again rather than a permanent "exists".
    assert _create_file_blocking(target, "second try")[0] is None
    assert target.read_text(encoding="utf-8") == "second try"


# ── steering delete ──


def test_steering_delete_removes_file(tmp_path):
    _needs_openat()
    target = tmp_path / "steering" / "rules.md"
    _create_file_blocking(target, "x")
    assert _delete_file_blocking(target) is None
    assert not target.exists()


def test_steering_delete_missing_returns_notfound(tmp_path):
    _needs_openat()
    target = tmp_path / "steering" / "missing.md"
    (tmp_path / "steering").mkdir(parents=True)
    assert _delete_file_blocking(target) == "notfound"


# ── steering update ──


def test_steering_update_replaces_and_preserves_mode(tmp_path):
    _needs_openat()
    target = tmp_path / "steering" / "rules.md"
    _create_file_blocking(target, "before")
    os.chmod(target, 0o640)
    assert _update_file_blocking(target, "after") is None
    assert target.read_text(encoding="utf-8") == "after"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_steering_update_missing_returns_notfound(tmp_path):
    target = tmp_path / "steering" / "gone.md"
    (tmp_path / "steering").mkdir(parents=True)
    assert _update_file_blocking(target, "x") == "notfound"


def test_steering_update_carries_acl(tmp_path, monkeypatch):
    _needs_openat()
    if not all(hasattr(os, a) for a in ("listxattr", "getxattr", "setxattr")):
        pytest.skip("platform without xattr syscalls")
    target = tmp_path / "steering" / "rules.md"
    _create_file_blocking(target, "before")

    monkeypatch.setattr(os, "listxattr", lambda *a, **k: ["system.posix_acl_access"], raising=False)
    monkeypatch.setattr(os, "getxattr", lambda *a, **k: b"acl", raising=False)
    recorded: list[tuple[str, bytes]] = []
    monkeypatch.setattr(
        os,
        "setxattr",
        lambda fd, attr, value, *a, **k: recorded.append((attr, value)),
        raising=False,
    )
    assert _update_file_blocking(target, "after") is None
    monkeypatch.undo()
    assert ("system.posix_acl_access", b"acl") in recorded


def test_steering_update_byte_exact_no_crlf(tmp_path):
    _needs_openat()
    target = tmp_path / "steering" / "rules.md"
    _create_file_blocking(target, "seed")
    body = "a\nb\n"
    assert _update_file_blocking(target, body) is None
    assert target.read_bytes() == body.encode("utf-8")


# ── file-write ──


def test_file_write_replaces_and_preserves_mode(tmp_path):
    _needs_openat()
    target = tmp_path / "doc.md"
    target.write_text("before", encoding="utf-8")
    os.chmod(target, 0o644)
    assert _file_write_blocking(str(target), "after") is None
    assert target.read_text(encoding="utf-8") == "after"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_file_write_carries_acl(tmp_path, monkeypatch):
    _needs_openat()
    if not all(hasattr(os, a) for a in ("listxattr", "getxattr", "setxattr")):
        pytest.skip("platform without xattr syscalls")
    target = tmp_path / "doc.md"
    target.write_text("before", encoding="utf-8")

    monkeypatch.setattr(os, "listxattr", lambda *a, **k: ["system.posix_acl_access"], raising=False)
    monkeypatch.setattr(os, "getxattr", lambda *a, **k: b"acl", raising=False)
    recorded: list[tuple[str, bytes]] = []
    monkeypatch.setattr(
        os,
        "setxattr",
        lambda fd, attr, value, *a, **k: recorded.append((attr, value)),
        raising=False,
    )
    assert _file_write_blocking(str(target), "after") is None
    monkeypatch.undo()
    assert ("system.posix_acl_access", b"acl") in recorded


def test_file_write_by_name_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod.pinned_fs, "supports_pinned_walk", lambda: False)
    target = tmp_path / "doc.md"
    target.write_text("before", encoding="utf-8")
    assert _file_write_blocking(str(target), "after") is None
    assert target.read_text(encoding="utf-8") == "after"


# ── the pin is walked from the CANONICAL chain, not re-resolved at pin time ──


@pytest.mark.parametrize(
    ("surface", "expected"),
    [
        ("file-write", "notfound"),
        ("steering-update", "writefailed"),
        ("steering-delete", "notfound"),
    ],
)
def test_a_grandparent_swapped_after_canonicalization_is_refused(tmp_path, surface, expected):
    """Every surface handed a canonical path REFUSES a since-linked ancestor.

    These three receive a path their validation already ran through ``realpath`` /
    ``resolve(strict=True)``, so ``pinned_fs.pin_parent`` can walk that recorded
    chain with ``O_NOFOLLOW`` per component and refuse a component that has become
    a link. ``open_dir_pinned`` would call ``realpath`` AGAIN at pin time and follow
    the link instead -- a second resolution cannot be more faithful than the first,
    only less -- and the write, or the unlink, would land in the attacker's tree
    while every caller comment claimed the parent was pinned.

    The swap is at the GRANDparent on purpose. The leaf's own parent is the final
    component of either walk and carries ``O_NOFOLLOW`` either way, so it is the
    component ABOVE it where a fresh resolution and the recorded chain disagree.
    Reverting any of the three call sites to ``open_dir_pinned`` reds this.
    """
    _needs_openat()
    if not hasattr(os, "O_NOFOLLOW"):  # pragma: no cover - POSIX-only assertion
        pytest.skip("O_NOFOLLOW is required to refuse a link")

    # The tree the caller names and validates.
    named = tmp_path / "named"
    (named / "mid" / "leaf").mkdir(parents=True)
    doc = named / "mid" / "leaf" / "doc.md"
    doc.write_text("mine", encoding="utf-8")
    canonical = Path(os.path.realpath(doc))

    # The tree an attacker wants the write redirected into, holding a file that
    # must survive with its own bytes.
    victim = tmp_path / "victim"
    (victim / "leaf").mkdir(parents=True)
    (victim / "leaf" / "doc.md").write_text("PROTECTED", encoding="utf-8")

    shutil.rmtree(named / "mid")
    (named / "mid").symlink_to(victim, target_is_directory=True)

    if surface == "file-write":
        assert _file_write_blocking(str(canonical), "attacker body") == expected
    elif surface == "steering-update":
        assert _update_file_blocking(canonical, "attacker body") == expected
    else:
        assert _delete_file_blocking(canonical) == expected

    survivor = victim / "leaf" / "doc.md"
    assert survivor.exists(), "the redirected target was deleted through a swapped ancestor"
    assert survivor.read_text(encoding="utf-8") == "PROTECTED"


@pytest.mark.parametrize(
    "surface", ["file-write", "steering-update"], ids=["file-write", "steering-update"]
)
def test_the_mode_and_acl_source_is_opened_through_the_pinned_directory(
    tmp_path, monkeypatch, surface
):
    """A directory replaced at the pinned name cannot supply the replacement's mode.

    Pinning the parent buys nothing if the leaf is then addressed BY NAME again,
    and the metadata read is a leaf reference like any other. With the source
    opened by name, a directory swapped in at the parent's name between the pin and
    that open supplies the mode and the ACL, while ``atomic_write`` still publishes
    through the pinned descriptor into the ORIGINAL directory -- so the real
    document is handed back carrying permissions chosen by whoever did the swap.

    The swap is injected exactly in that window by wrapping ``pin_parent``: it
    returns the genuine descriptor and only then renames the directories, so the
    production code is untouched and the race is deterministic rather than timed.
    """
    _needs_openat()
    named = tmp_path / "named"
    (named / "leaf").mkdir(parents=True)
    doc = named / "leaf" / "doc.md"
    doc.write_text("mine", encoding="utf-8")
    os.chmod(doc, 0o600)
    canonical = Path(os.path.realpath(doc))

    # The directory an attacker parks at the pinned name, holding a wider mode.
    # The value only has to DIFFER from the original's 0o600 for the assertion
    # below to tell which inode the mode came from, so it is the ordinary 0o644
    # rather than something world-writable that a SAST rule would rightly flag.
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "doc.md").write_text("decoy", encoding="utf-8")
    os.chmod(decoy / "doc.md", 0o644)

    handler = files_mod if surface == "file-write" else steering_mod
    real_pin = handler.pinned_fs.pin_parent

    def pin_then_swap(resolved_parent, **kwargs):
        fd = real_pin(resolved_parent, **kwargs)
        (named / "leaf").rename(named / "moved")
        decoy.rename(named / "leaf")
        return fd

    monkeypatch.setattr(handler.pinned_fs, "pin_parent", pin_then_swap)

    if surface == "file-write":
        assert _file_write_blocking(str(canonical), "after") is None
    else:
        assert _update_file_blocking(canonical, "after") is None

    # The write landed in the pinned directory, which is now reachable at its new
    # name -- and it kept ITS OWN mode, not the decoy's.
    published = named / "moved" / "doc.md"
    assert published.read_text(encoding="utf-8") == "after"
    assert stat.S_IMODE(published.stat().st_mode) == 0o600
    # The decoy is untouched: it was never the write's destination.
    assert (named / "leaf" / "doc.md").read_text(encoding="utf-8") == "decoy"


@pytest.mark.parametrize(
    "surface", ["file-write", "steering-update"], ids=["file-write", "steering-update"]
)
def test_a_publish_that_cannot_be_pinned_degrades_to_the_by_name_floor(
    tmp_path, monkeypatch, surface
):
    """Without the descriptor-relative rename the caller passes None, not a descriptor.

    ``atomic_write`` REFUSES a ``parent_dir_fd`` it cannot publish through, so a
    caller that gated only on the pinned WALK would raise on every save on a
    platform where the two capabilities disagree. Both are asked, and the answer is
    the unchanged by-name write.

    The disagreement is emulated by removing ``os.rename`` from
    ``os.supports_dir_fd`` -- the platform FACT both probes read -- rather than by
    patching either module's binding of the probe. A binding patch would only
    convince one of the two callers of it and leave the other computing the host's
    real answer, which is how a test like this passes while the branch it names is
    never taken. ``os.open`` stays a member, so the pinned WALK is still supported
    and the two probes genuinely disagree, which is the case under test.
    """
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd - {os.rename})
    target = tmp_path / "doc.md"
    target.write_text("before", encoding="utf-8")
    if surface == "file-write":
        assert _file_write_blocking(str(target), "after") is None
    else:
        assert _update_file_blocking(target, "after") is None
    assert target.read_text(encoding="utf-8") == "after"


# ── the skill CRUD handlers keep their filesystem work off the event loop ──


class _FakeRequest:
    """The slice of ``web.Request`` the two skill CRUD handlers actually read."""

    def __init__(self, method: str, *, name: str = "", body: dict | None = None) -> None:
        self.method = method
        self.match_info = {"name": name}
        self.app = {"state": SimpleNamespace(context_builder=None)}
        self._body = body or {}

    async def json(self) -> dict:
        return self._body


class _ThreadRecordingSkills:
    """Records which thread each CRUD call ran on."""

    def __init__(self) -> None:
        self.idents: list[int] = []

    def _record(self) -> bool:
        self.idents.append(threading.get_ident())
        return True

    def create_skill(self, name: str, content: str) -> bool:
        return self._record()

    def update_skill(self, name: str, content: str) -> bool:
        return self._record()

    def delete_skill(self, name: str) -> bool:
        return self._record()


@pytest.mark.asyncio
async def test_skill_crud_handlers_do_not_run_on_the_event_loop(monkeypatch):
    """create/update/delete are offloaded, so a slow filesystem cannot stall the loop.

    Each of the three walks a pinned parent chain, and update additionally stages a
    temp file, carries the ACL and renames it into place. On network-backed storage
    a single call can outlast ``dashboard.loop_stall_exit_after_secs``, at which
    point the watchdog kills the gateway for every session -- so the thread the
    work runs on is the property worth pinning, not the syscall count.
    """
    recorder = _ThreadRecordingSkills()
    monkeypatch.setattr(prompts_mod, "_get_skills", lambda _state: recorder)
    loop_ident = threading.get_ident()

    assert (await prompts_mod.api_skill_detail(_FakeRequest("DELETE", name="gone"))).status == 200
    put = _FakeRequest("PUT", name="edit", body={"content": "after"})
    assert (await prompts_mod.api_skill_detail(put)).status == 200
    post = _FakeRequest("POST", body={"name": "fresh", "content": "body"})
    assert (await prompts_mod.api_skills_create(post)).status == 200

    assert len(recorder.idents) == 3
    assert loop_ident not in recorder.idents
    # And the loop thread really is the one the assertion above named.
    assert loop_ident == threading.get_ident()
    assert asyncio.get_running_loop() is not None
