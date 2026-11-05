"""Regression tests for the descriptor-pinned dashboard write migration.

Steering create/update/delete and file-write go through ``pinned_fs``, keep their error
tokens, ACL carry, ``newline=""`` and mode, and give the same results on the by-name
floor (the Windows path). They need ``aiohttp``, so they run in CI only.
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

    EIO on a network filesystem is usually transient, and a second probe through the
    still-open descriptor cannot answer for a different object.
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

    An unverified unlink could delete a rival's file, so the name stays. The identity
    probe precedes the first ``os.write``, so what stays is an empty document.
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

    A descriptor pins the directory, not the name, so cleanup checks ``(st_dev, st_ino)``.
    The failing ``os.write`` itself swaps the leaf, so no timing is involved.
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

    Deferred write errors (ENOSPC, EIO on NFS) surface at ``close``. The failure is
    injected only for the leaf's own descriptor.
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

    ``pinned_fs.pin_parent`` walks the recorded chain with ``O_NOFOLLOW``, while
    ``open_dir_pinned`` would resolve again and follow the link. The swap is at the
    grandparent because the leaf's parent carries ``O_NOFOLLOW`` in either walk.
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

    Read by name, the swapped-in directory would supply the mode and ACL for the real
    document. Wrapping ``pin_parent`` renames the directories right after the pin, so the
    race is deterministic.
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

    ``atomic_write`` refuses a ``parent_dir_fd`` it cannot publish through. Removing
    ``os.rename`` from ``os.supports_dir_fd`` changes the fact both probes read; patching
    one module's probe would leave the other on the host's real answer.
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

    On network storage one call can outlast ``dashboard.loop_stall_exit_after_secs``, and
    the watchdog then kills the gateway.
    """
    recorder = _ThreadRecordingSkills()
    monkeypatch.setattr(prompts_mod, "_get_skills", lambda _state: recorder)
    # The skill CRUD writes are owner-only; the gate itself is covered in
    # test_skill_write_guard.py, and here it would only stand between the test
    # and the thread the work runs on.
    monkeypatch.setattr(
        prompts_mod, "is_owner_dashboard_request", lambda _request: True, raising=False
    )
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
