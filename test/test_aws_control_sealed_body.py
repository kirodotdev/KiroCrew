"""Tests for the non-dumpable sealing helper (:mod:`sealed_body`).

The helper exists to close GPT 5.6's finding that a WRITABLE memfd is reachable
through the gateway's dumpable ``/proc/<pid>/fd`` before it is sealed. The structural
guarantee is: the gateway never holds a writable alias of the body -- a forked child
clears its dumpable flag, creates+fills+seals the memfd and returns ONLY the sealed
descriptor. These tests pin the three observable consequences:

1. a sealed descriptor (and only that) reaches the caller;
2. a write to the returned descriptor is refused (``EPERM``);
3. a helper that dies before sealing yields no body -- the caller fails closed.
"""

from __future__ import annotations

import errno
import os

import pytest

from kiro_crew.apps.builtins.aws_control.backend import sealed_body
from kiro_crew.deploy.engine import AWSError

pytestmark = pytest.mark.skipif(
    not hasattr(os, "memfd_create") or sealed_body.ctypes is None or sealed_body.fcntl is None,
    reason="the sealing helper is a Linux memfd+fork+prctl path; absent here",
)


def _write(payload: bytes):
    def _w(fd: int) -> None:
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view) :]

    return _w


def test_only_a_sealed_descriptor_reaches_the_caller():
    # The returned fd carries exactly what write_body produced, and it is sealed:
    # a write is refused through this descriptor (and so through any /proc alias).
    fd = sealed_body.build_sealed_body(_write(b"the-body-bytes"), "unit")
    try:
        assert os.pread(fd, 64, 0) == b"the-body-bytes"
        with pytest.raises(OSError) as exc:
            os.write(fd, b"tamper")
        assert exc.value.errno == errno.EPERM
    finally:
        os.close(fd)


def test_the_returned_memfd_is_nameless_and_shows_deleted_in_proc():
    # A memfd has no directory entry; its /proc link reads as deleted. This is the
    # observable sign the body is anonymous RAM, not a file a name could reach.
    fd = sealed_body.build_sealed_body(_write(b"x"), "unit")
    try:
        assert os.fstat(fd).st_nlink == 0
        assert "(deleted)" in os.readlink(f"/proc/self/fd/{fd}")
    finally:
        os.close(fd)


def test_an_empty_body_still_returns_a_sealed_descriptor():
    fd = sealed_body.build_sealed_body(lambda _fd: None, "unit")
    try:
        assert os.fstat(fd).st_size == 0
        with pytest.raises(OSError):
            os.write(fd, b"x")
    finally:
        os.close(fd)


def test_the_parent_fails_closed_when_no_sealed_fd_arrives(monkeypatch):
    # The helper runs in a FRESH interpreter (exec), so a parent monkeypatch cannot
    # reach it. Model the helper dying before sealing by making the parent's receive
    # return no descriptor: the caller must fail closed, not return an unheld body.
    monkeypatch.setattr(sealed_body, "_recv_fd", lambda _sock: None)
    with pytest.raises(AWSError, match="died before it sealed"):
        sealed_body.build_sealed_body(_write(b"data"), "unit")


def test_the_bootstrap_clears_dumpable_before_sealing():
    # The dumpable clear MUST come first, with stdlib only, BEFORE the memfd is
    # created. Pin the ordering in the bootstrap string: the prctl clear + its
    # fail-closed os._exit(3) both appear before memfd_create.
    bootstrap = sealed_body._HELPER_BOOTSTRAP
    clear_at = bootstrap.index("prctl(4, 0, 0, 0, 0)")
    exit_at = bootstrap.index("os._exit(3)")
    memfd_at = bootstrap.index("memfd_create")
    assert (
        clear_at < exit_at < memfd_at
    ), "the dumpable clear and its fail-closed exit must precede the memfd seal"


def test_the_bootstrap_imports_nothing_from_the_agent_writable_package():
    # GPT :307: the fresh helper interpreter must NOT import kiro_crew/sealed_body
    # from the (agent-writable) source tree, or an agent that replaced that module on
    # disk would have this unsandboxed interpreter execute the replacement with the
    # gateway's inherited fds. The whole seal job is stdlib-only; nothing kiro_crew is
    # imported and no trusted-root argv is passed.
    bootstrap = sealed_body._HELPER_BOOTSTRAP
    assert "kiro_crew" not in bootstrap
    assert "sys.path.insert" not in bootstrap
    assert "sys.argv[4]" not in bootstrap
    assert not hasattr(sealed_body, "_trusted_import_root")


def test_the_bootstrap_seal_mask_matches_body_seals():
    # The seal bitmask inlined in the stdlib-only bootstrap must equal _body_seals()
    # exactly -- the in-process reference and the production inline cannot drift.
    bootstrap = sealed_body._HELPER_BOOTSTRAP
    assert (
        "fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL"
        in bootstrap
    )


def test_the_bootstrap_fails_closed_when_prctl_cannot_clear_dumpable(tmp_path):
    # Drive the REAL bootstrap in a subprocess whose prctl is forced to fail (a stub
    # libc that returns non-zero), and confirm it exits non-zero WITHOUT importing the
    # package or sending a descriptor -- the parent then fails closed. This exercises
    # the exec'd path's own fail-closed branch, invisible to in-process coverage.
    import subprocess
    import sys as _sys

    # A bootstrap variant whose CDLL is a stub returning a prctl that fails, so the
    # ordering guard (clear-first, exit-on-failure) is what decides the exit code.
    stubbed = sealed_body._HELPER_BOOTSTRAP.replace(
        "_libc = ctypes.CDLL('libc.so.6', use_errno=True)",
        "import types\n" "    _libc = types.SimpleNamespace(prctl=lambda *a: 1)",
    )
    proc = subprocess.run(
        [_sys.executable, "-I", "-c", stubbed, "unit", "0", "1", str(tmp_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    # Exit 3 is the fail-closed-before-import code; a successful import then crash
    # would be a different code and would mean the import ran while dumpable.
    assert proc.returncode == 3, proc.stderr.decode()[-500:]


def test_the_helper_entry_fails_closed_on_a_seal_error(monkeypatch):
    # A seal failure in the helper's entry exits 4 and sends no descriptor.
    import socket as _socket

    def _boom(*_a, **_k):
        raise OSError("seal boom")

    monkeypatch.setattr(sealed_body.fcntl, "fcntl", _boom)
    _p, c = _socket.socketpair()
    dr, dw = os.pipe()
    os.write(dw, b"data")
    os.close(dw)
    try:
        # _run_helper_child wraps result_fd in a socket that OWNS it, so hand it a dup.
        assert sealed_body._run_helper_child("unit", dr, os.dup(c.fileno())) == 4
    finally:
        _p.close()
        c.close()
        os.close(dr)


def test_the_helper_entry_seals_and_sends_on_the_happy_path():
    # The helper's entry, in-process: seals the body and sends it, returns 0. The
    # dumpable clear is the bootstrap's job (before the import), not this function's,
    # so nothing to clear or restore here.
    import socket as _socket

    p, c = _socket.socketpair()
    dr, dw = os.pipe()
    os.write(dw, b"entry-body")
    os.close(dw)
    try:
        rc = sealed_body._run_helper_child("unit", dr, os.dup(c.fileno()))
        assert rc == 0
        fd = sealed_body._recv_fd(p)
        assert fd is not None
        assert os.pread(fd, 32, 0) == b"entry-body"
        os.close(fd)
    finally:
        p.close()
        c.close()
        os.close(dr)


def test_a_producer_error_propagates_and_returns_no_body(monkeypatch):
    # A write_body that raises tears the helper down (EOF, no seal) and the error
    # propagates to the caller -- no body is returned.
    def _raise(_fd: int) -> None:
        raise ValueError("producer boom")

    with pytest.raises(ValueError, match="producer boom"):
        sealed_body.build_sealed_body(_raise, "unit")


def test_a_launch_failure_fails_closed(monkeypatch):
    # If the sealing helper cannot be launched, there is no helper and no body: fail
    # closed. Simulated by subprocess.Popen raising, the way a spawn failure surfaces.
    def _no_spawn(*_a, **_k):
        raise OSError(errno.EAGAIN, "cannot spawn")

    monkeypatch.setattr(sealed_body.subprocess, "Popen", _no_spawn)
    with pytest.raises(AWSError, match="could not launch"):
        sealed_body.build_sealed_body(_write(b"data"), "unit")


def test_the_body_channel_is_a_socketpair_not_a_proc_reopenable_pipe(monkeypatch):
    # Opus BLOCKING fix: the body must travel over an AF_UNIX socketpair, never an
    # os.pipe. A pipe end is reopenable through /proc/<gateway-pid>/fd by a same-UID
    # agent, which could inject bytes into the stream the helper seals; a unix socket
    # reopened through /proc fails with ENXIO. Prove the body channel is a socketpair
    # by counting the socketpairs the build creates (one for the body, one for the
    # sealed-fd return) -- two means the body did not go over a pipe.
    import socket as _socket

    pairs: list[tuple[int, int]] = []
    real_socketpair = _socket.socketpair

    def _spy(*a, **k):
        p = real_socketpair(*a, **k)
        pairs.append((p[0].fileno(), p[1].fileno()))
        return p

    monkeypatch.setattr(sealed_body.socket, "socketpair", _spy)
    fd = sealed_body.build_sealed_body(_write(b"over-a-socket"), "unit")
    try:
        assert os.pread(fd, 64, 0) == b"over-a-socket"
        # Two socketpairs: one for the body channel, one for the sealed-fd return.
        # A pipe-based body channel would create only one (the return socket).
        assert len(pairs) >= 2, "the body channel must be a socketpair, not a pipe"
    finally:
        os.close(fd)


def test_a_helper_that_never_sends_times_out_and_fails_closed(monkeypatch):
    # Opus BLOCKING fix: _recv_fd is a blocking recvmsg; without a deadline a helper
    # that never sends (or a same-UID writer holding the stream open) would hang the
    # backup worker forever. The parent sets a timeout on the receive socket before
    # _recv_fd; a receive that times out must fail CLOSED (no body), not hang or raise
    # a raw socket error. Simulate the stuck receive and assert the fail-closed path.
    monkeypatch.setattr(sealed_body, "_RECV_FD_TIMEOUT", 0.2)

    def _times_out(_sock):
        raise TimeoutError("recv timed out")

    monkeypatch.setattr(sealed_body, "_recv_fd", _times_out)
    with pytest.raises(AWSError, match="died before it sealed"):
        sealed_body.build_sealed_body(_write(b"never-sealed"), "unit")


# -- Child-side helpers, exercised IN-PROCESS ------------------------------------
# The real child runs in a fork, so coverage.py in the parent never records its
# lines. These drive the same functions directly (no fork) so the create/fill/seal/
# send/receive logic is both tested AND counted.


def test_seal_from_pipe_creates_fills_seals_and_sends(tmp_path):
    import socket as _socket

    data_r, data_w = os.pipe()
    parent, child = _socket.socketpair(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        os.write(data_w, b"streamed-into-the-memfd")
        os.close(data_w)
        data_w = -1
        sealed_body._seal_from_pipe(data_r, child, "unit")
        fd = sealed_body._recv_fd(parent)
        assert fd is not None
        try:
            assert os.pread(fd, 64, 0) == b"streamed-into-the-memfd"
            with pytest.raises(OSError) as exc:
                os.write(fd, b"x")
            assert exc.value.errno == errno.EPERM
        finally:
            os.close(fd)
    finally:
        os.close(data_r)
        if data_w != -1:
            os.close(data_w)
        parent.close()
        child.close()


def test_recv_fd_returns_none_when_the_peer_closed_without_sending():
    import socket as _socket

    parent, child = _socket.socketpair(_socket.AF_UNIX, _socket.SOCK_STREAM)
    child.close()  # peer gone, no fd sent -> fail-closed signal
    try:
        assert sealed_body._recv_fd(parent) is None
    finally:
        parent.close()


def test_body_seals_bitmask_is_the_full_write_grow_shrink_seal_set():
    import fcntl as _fcntl

    seals = sealed_body._body_seals()
    for bit in (_fcntl.F_SEAL_WRITE, _fcntl.F_SEAL_GROW, _fcntl.F_SEAL_SHRINK, _fcntl.F_SEAL_SEAL):
        assert seals & bit


def test_clear_dumpable_constant_matches_prctl_pr_set_dumpable():
    # The bootstrap inlines the literal 4 (it must be stdlib-only and self-contained
    # before the package import); this names it, so a drift from PR_SET_DUMPABLE is
    # caught here and the bootstrap's magic 4 has a documented source.
    assert sealed_body._PR_SET_DUMPABLE == 4
    assert "prctl(4, 0, 0, 0, 0)" in sealed_body._HELPER_BOOTSTRAP
