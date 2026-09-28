"""Produce an upload body into a sealed memfd without ever exposing a WRITABLE one.

A sealed ``memfd`` holds its bytes immutable for the whole transfer -- through the
owning descriptor and through every ``/proc/<pid>/fd`` alias -- but only AFTER the
seal. ``memfd_create`` returns a WRITABLE descriptor, and the seal is applied only
once the body is fully written, so between those two points the backing memory is
rewritable through the creating process's ``/proc/<pid>/fd``. The gateway shares its
pid namespace and ``/proc`` with the agents it launches and stays dumpable, so a
same-UID agent -- including a CONFINED one, which is the gate's ordinary steady
state -- can enumerate ``/proc/<gateway-pid>/fd`` and rewrite that memfd before the
seal. The substituted bytes are then fingerprinted and uploaded under the operator's
key, with no later check able to notice.

The structural close: the writable memfd never exists in the gateway. A SEPARATE
helper process -- a FRESH python interpreter started with ``exec`` in ISOLATED mode
(``python -I``) -- clears its own dumpable flag FIRST (so the kernel reparents its
``/proc`` entries to root and a same-UID reader gets ``EACCES``), THEN creates the
memfd, writes the body into it, seals it, and hands back ONLY the sealed descriptor
over a unix socket with ``SCM_RIGHTS``. Isolated mode drops the working directory,
``PYTHONPATH`` and the user site-dir from the fresh interpreter's ``sys.path``, so no
``sitecustomize``/startup code planted in a writable CWD can run before the dumpable
flag is cleared; the one trusted directory holding ``kiro_crew`` is passed in and
prepended by the bootstrap. The gateway streams the body into the helper over an
AF_UNIX socketpair (a socket end reopened through ``/proc/<pid>/fd`` fails with
``ENXIO``, so a same-UID agent cannot inject into the stream the way it could into a
pipe) and receives the sealed fd over a second socket; it never holds a descriptor to
the unsealed memory. The helper dies as soon as it has sent the sealed fd. If it dies
BEFORE sealing and sending, the parent gets no descriptor and fails closed rather than
uploading an unheld body.

The helper is launched with ``exec`` (``subprocess``'s C-level ``fork``+``exec``),
NOT a bare ``os.fork``: a bare fork of this multi-threaded gateway could inherit a
glibc loader or allocator lock a sibling thread held at the fork instant and then
DEADLOCK the moment the child touched ``ctypes``/allocation, with no deadline on any
wait. A fresh interpreter starts with no inherited locks, so that whole class of
fork-safety hang is gone. The exec path runs no user Python between fork and exec.

This is Linux-only (``memfd_create`` + file seals + ``prctl``); every other platform
fails closed upstream before reaching here.
"""

from __future__ import annotations

import array
import os
import socket
import subprocess
import sys
from collections.abc import Callable

import kiro_crew
from kiro_crew.deploy.engine import AWSError


def _trusted_import_root() -> str:
    """The one directory to put on the helper's ``sys.path`` so it can import us.

    The helper runs in ISOLATED mode (``python -I``), which drops the current working
    directory and ``PYTHONPATH`` from ``sys.path`` -- the whole point, so a
    ``sitecustomize.py`` planted in a writable CWD cannot run at interpreter startup
    before the helper clears its dumpable flag. Isolated mode keeps the stdlib and
    site-packages, so an INSTALLED gateway resolves ``kiro_crew`` with no help; a
    source-tree gateway does not, so we pass the single directory that contains the
    ``kiro_crew`` package -- resolved here from the already-imported package, never
    from the environment or CWD -- for the bootstrap to prepend. One explicitly
    trusted entry, not the parent's whole ``sys.path``.
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(kiro_crew.__file__)))


try:  # Linux-only; the module must still import on Windows (where it is never called).
    import ctypes
    import fcntl
except ModuleNotFoundError:  # pragma: no cover - exercised on Windows only
    ctypes = None  # type: ignore[assignment]
    fcntl = None  # type: ignore[assignment]

#: ``prctl`` option number for the dumpable flag (``linux/prctl.h``); value 0 clears it.
_PR_SET_DUMPABLE = 4

#: Chunk size the parent streams and the helper drains.
_CHUNK = 1 << 20

#: Deadline (seconds) for receiving the sealed descriptor from the helper. The helper
#: is bounded (the parent kills it after 30s), so a body that has not arrived well
#: past that is a stuck/held stream, not slow progress: time out and fail closed
#: rather than let a same-UID writer holding the stream open hang the worker forever.
_RECV_FD_TIMEOUT = 60.0


def _body_seals() -> int:
    """``F_SEAL_WRITE | F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL``.

    ``WRITE`` makes every write fail, through the owning descriptor and every
    ``/proc`` alias; ``GROW``/``SHRINK`` pin the length so the fingerprinted size is
    the uploaded size; ``SEAL`` stops any later process removing the other three.
    """
    return fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL


def _send_fd(sock: socket.socket, fd: int) -> None:
    """Send one descriptor over *sock* with ``SCM_RIGHTS`` (plus a one-byte payload)."""
    sock.sendmsg([b"\x01"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))])


def _recv_fd(sock: socket.socket) -> int | None:
    """Receive one ``SCM_RIGHTS`` descriptor from *sock*; ``None`` if none arrived.

    ``None`` is the fail-closed signal: the helper died (its socket end closed)
    before it sealed and sent, so there is no body to trust.
    """
    fds = array.array("i")
    msg, ancdata, _flags, _addr = sock.recvmsg(1, socket.CMSG_LEN(fds.itemsize))
    if not msg:
        return None
    for level, typ, data in ancdata:
        if level == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS:
            fds.frombytes(data[: len(data) - (len(data) % fds.itemsize)])
    if not fds:
        return None
    received = list(fds)
    # Defensive: at most one fd is ever sent; close any extras rather than leak them.
    for extra in received[1:]:
        os.close(extra)
    return received[0]


def _seal_from_pipe(data_r: int, result_sock: socket.socket, label: str) -> None:
    """Create a memfd, drain *data_r* into it, seal it, and send it over *result_sock*.

    The helper's whole job, factored out so a test can drive it IN-PROCESS (the
    helper subprocess's own execution is invisible to the parent's coverage). Raises
    on any failure; the caller decides how to fail closed (the ``__main__`` entry
    exits non-zero, so the parent receives no descriptor).
    """
    mem_fd = os.memfd_create(f"kc-upload-{label}", os.MFD_ALLOW_SEALING | os.MFD_CLOEXEC)
    try:
        while True:
            chunk = os.read(data_r, _CHUNK)
            if not chunk:
                break
            written = 0
            while written < len(chunk):
                written += os.write(mem_fd, chunk[written:])
        fcntl.fcntl(mem_fd, fcntl.F_ADD_SEALS, _body_seals())
        _send_fd(result_sock, mem_fd)
    finally:
        os.close(mem_fd)


def build_sealed_body(write_body: Callable[[int], None], label: str) -> int:
    """Return a SEALED memfd carrying what *write_body* writes, built off-process.

    *write_body* is called with a single write descriptor and must stream the whole
    body into it (and return when done); the parent closes that descriptor to mark
    EOF. The bytes are received, written into a memfd, and sealed inside a SEPARATE
    helper process (a fresh ``exec``'d interpreter, so no inherited lock can deadlock
    it), which is NON-DUMPABLE before it creates the memfd -- so the gateway never
    holds a writable alias of the body and no same-UID ``/proc`` reader can reach it
    before the seal.

    Fails closed (`AWSError`) if the helper cannot be launched, dies before sealing,
    or exits non-zero -- in every case no body is returned rather than an unheld one.
    The caller owns closing the returned descriptor.
    """
    if ctypes is None or fcntl is None or not hasattr(os, "memfd_create"):
        raise AWSError(
            "refusing to stage an upload body that cannot be held unrewritable: this "
            "platform has no sealable memfd, so there is no way to hold the bytes "
            "immutable for the whole transfer. Failing closed."
        )

    # The body travels over an AF_UNIX socketpair, NOT an os.pipe: a pipe end is
    # reopenable through /proc/<gateway-pid>/fd by a same-UID agent sharing the
    # gateway's pid namespace, so it could inject bytes into the stream the helper
    # drains and seals -- the exact substitution this module exists to close. A unix
    # socket reopened through /proc fails with ENXIO, so the agent cannot write into
    # it; only this process's retained end can. The gateway writes the body into its
    # end (data_w) and the helper drains the other (data_r); closing data_w signals
    # EOF to the helper just as a pipe close would.
    data_parent, data_child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    data_w = data_parent.fileno()
    data_r = data_child.fileno()
    parent_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    proc: subprocess.Popen | None = None
    sealed_fd: int | None = None
    try:
        # Launch via exec (subprocess does fork+exec in C with no user Python
        # between), NOT a bare os.fork: a fresh interpreter inherits no glibc loader
        # or allocator lock a sibling gateway thread might hold at the fork instant,
        # so the fork-safety deadlock class is gone. The data-read end and the
        # result-socket end are the only inherited fds (pass_fds clears O_CLOEXEC on
        # exactly these); the helper reads them by the numbers passed in argv.
        #
        # ISOLATED mode (``-I``) is the point of the exec hardening here: it drops the
        # current working directory, ``PYTHONPATH`` and the user site-dir from the
        # fresh interpreter's ``sys.path``, so a ``sitecustomize.py`` (or any importable
        # name) planted in a writable gateway CWD cannot execute at interpreter startup
        # -- which, under a plain ``-c``, runs via ``site`` BEFORE the helper clears its
        # dumpable flag, i.e. unsandboxed code with the gateway's inherited access. The
        # cost is that ``-I`` also ignores ``PYTHONPATH``, so a source-tree gateway
        # cannot resolve ``kiro_crew`` on its own; we hand the bootstrap the ONE trusted
        # directory that holds the package (resolved from the already-imported module,
        # not from the environment) for it to prepend -- an installed gateway finds the
        # package on site-packages and does not even need it.
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    _HELPER_BOOTSTRAP,
                    label,
                    str(data_r),
                    str(child_sock.fileno()),
                    _trusted_import_root(),
                ],
                pass_fds=(data_r, child_sock.fileno()),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as exc:
            raise AWSError(
                "refusing to stage an upload body: could not launch the sealing " f"helper ({exc})."
            ) from exc

        # The helper owns the read end and the child socket now; the parent must
        # drop them so its own ``recvmsg`` sees EOF when the helper exits.
        data_child.close()
        data_r = -1
        child_sock.close()

        try:
            write_body(data_w)
        finally:
            # EOF to the helper regardless of how write_body ended, so a producer
            # error tears the helper down (no seal, no fd) rather than hanging it.
            data_parent.close()
            data_w = -1
        # Bound the wait for the sealed descriptor: _recv_fd is a blocking recvmsg,
        # and without a deadline a helper that never sends (or an injected writer
        # that holds the stream open) would hang the backup worker forever. On
        # timeout, recvmsg raises and the finally tears the helper down -> no fd ->
        # fail closed, exactly as a helper death does.
        parent_sock.settimeout(_RECV_FD_TIMEOUT)
        try:
            sealed_fd = _recv_fd(parent_sock)
        except (TimeoutError, OSError):
            # Timed out (or the socket errored) waiting for the sealed fd: treat it
            # as the helper failing to deliver -> sealed_fd stays None -> fail closed
            # below, rather than letting a raw error escape past the fail-closed path.
            sealed_fd = None
    finally:
        parent_sock.close()
        if data_r != -1:
            data_child.close()
        if data_w != -1:
            data_parent.close()
        if proc is not None:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - helper is bounded
                proc.kill()
                proc.wait()

    if sealed_fd is None:
        raise AWSError(
            "refusing to stage an upload body: the sealing helper died before it "
            "sealed and returned the body, so the bytes cannot be held unrewritable "
            "for the transfer. Failing closed."
        )
    return sealed_fd


#: The helper program, run by a FRESH interpreter via ``python -I -c``. ISOLATED
#: mode drops CWD/PYTHONPATH/user-site from ``sys.path`` so no planted startup code
#: (``sitecustomize``) runs at interpreter startup.
#:
#: CRITICAL ORDERING: the dumpable flag is cleared FIRST, with STDLIB ONLY (``ctypes``
#: + ``prctl``), BEFORE ``sys.path`` is touched or ``kiro_crew`` is imported. The one
#: trusted directory holding the package (``argv[4]``) is resolved on a source/editable
#: checkout from a tree this codebase treats as agent-writable, so importing from it
#: while still dumpable would run attacker-substituted module code at import time with
#: the gateway's inherited fds -- defeating the seal this helper exists to guarantee.
#: Clearing dumpable first reparents this process's ``/proc`` entries to root, so that
#: window is closed before any package code can load. If the clear cannot be applied
#: the bootstrap exits non-zero WITHOUT importing anything or sending a descriptor, so
#: the parent's ``_recv_fd`` returns ``None`` and the caller fails closed. Only after
#: the clear does it prepend the trusted root, import this module, and delegate the
#: seal+send to the SAME ``_run_helper_child`` the in-process coverage test drives.
#: No user Python ran in this process before exec, so no inherited lock can deadlock it.
_HELPER_BOOTSTRAP = (
    "import ctypes, sys\n"
    # Clear dumpable with stdlib only, before any sys.path change or package import.
    "try:\n"
    "    _libc = ctypes.CDLL('libc.so.6', use_errno=True)\n"
    "    _ok = hasattr(_libc, 'prctl') and _libc.prctl(4, 0, 0, 0, 0) == 0\n"
    "except OSError:\n"
    "    _ok = False\n"
    "import os\n"
    "if not _ok:\n"
    "    os._exit(3)\n"
    # Non-dumpable now: safe to add the trusted root and import the package.
    "sys.path.insert(0, sys.argv[4])\n"
    "from kiro_crew.apps.builtins.aws_control.backend import sealed_body as _sb\n"
    "os._exit(_sb._run_helper_child(sys.argv[1], int(sys.argv[2]), int(sys.argv[3])))\n"
)


def _run_helper_child(label: str, data_fd: int, result_fd: int) -> int:
    """Seal the body from the inherited fds and send it; the helper's post-clear job.

    Driveable IN-PROCESS and run verbatim by the exec'd helper via
    :data:`_HELPER_BOOTSTRAP` AFTER it has cleared the dumpable flag (the clear MUST
    precede importing this module from the agent-writable source tree, so it lives in
    the stdlib-only bootstrap, not here). Returns the exit code (0 clean, non-zero
    fail-closed). The in-process coverage test clears dumpable on its own throwaway
    process before calling this, mirroring the production ordering.
    """
    try:
        result = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, fileno=result_fd)
        _seal_from_pipe(data_fd, result, label)
    except BaseException:  # noqa: BLE001 - any failure must fail closed, not send a body
        return 4
    return 0
