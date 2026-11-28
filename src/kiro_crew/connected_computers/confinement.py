"""Local machine: the OS confinement every connector child runs under.

An ALLOW-list, unlike the gateway's own agent sandbox (which masks known secret
paths): a child may write only the granted root and a private scratch, and may
read only the root, the scratch and the runtime it needs. That holds for every
process the connector starts -- the workspace tool server, each command it runs
and their children, and each exposed local MCP server -- because the confinement
wraps the process the connector spawns, and children inherit it.

Plans:

* **Linux** -- ``bwrap`` (bubblewrap) with new user, PID, IPC and UTS
  namespaces: read-only ``/usr`` (and the ``/bin``, ``/lib*``, ``/sbin`` links),
  a few named ``/etc`` files (certificates, name resolution, users, time zone,
  the loader cache), the Python install that runs the tool server, the copied
  tool-server files; read-write root and scratch (which is also ``HOME``);
  ``/proc`` and ``/dev`` of the sandbox; and the :mod:`.seccomp` filter, which
  refuses AF_UNIX sockets, io_uring and the kernel keyring calls. Needs unprivileged user namespaces and a
  processor :mod:`.seccomp` has a table for.
* **anything else** (macOS and Windows included) -- unsupported for now: there
  is no plan, and the connector refuses to start rather than run unconfined
  (B06).

There is no fallback. A missing ``bwrap``, a kernel without
user namespaces, or a failed :func:`self_test` makes the computer unavailable
with that reason; the operator's "run unsandboxed" consent for the agent
sandbox (``sandbox_unavailable``) is deliberately not consulted here.

Two policies, kept apart:

* **Network access** -- IP networking is not confined: commands may reach the
  network (and listen on it) as the user's own shell would (git, package
  managers, a dev server). :func:`self_test` checks this still holds.
* **Credential protection** -- nothing that carries the user's authority past
  the root is reachable: no file outside the allow-list, no environment variable
  of the local gateway (:func:`child_env` builds the environment from nothing),
  no local IPC endpoint of the user's session. On Linux that last part is the
  seccomp filter: without a network namespace the child shares the user's
  ABSTRACT unix sockets (X11, a session bus), which no mount hides, so creating
  any AF_UNIX socket is refused -- a unix socket inside the root is refused too.

Toolchains installed under the home folder (``~/.nvm``, ``~/.cargo``, ...) are
outside the allow-list and so not visible; the child's ``HOME`` is its scratch.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform as platform_mod
import secrets
import shutil
import socket
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Final, Mapping, Optional, Sequence, TypeVar

from kiro_crew.connected_computers import seccomp

_NO_ENV: Final[Mapping[str, str]] = MappingProxyType({})

PLAN_LINUX: Final[str] = "linux-bwrap"
#: Why a computer that is not Linux cannot share: there is no confinement plan.
UNSUPPORTED_OS_REASON: Final[str] = (
    "connected computers is available only on Linux for now: this computer cannot "
    "confine commands to the shared folder"
)

#: The ``/etc`` entries a confined child may read on Linux (each only if present).
LINUX_ETC_READS: Final[tuple[str, ...]] = (
    "/etc/ssl/certs",
    "/etc/ssl/cert.pem",
    "/etc/ca-certificates",
    "/etc/pki/tls/certs",
    "/etc/pki/ca-trust",
    "/etc/resolv.conf",
    "/etc/hosts",
    "/etc/nsswitch.conf",
    "/etc/passwd",
    "/etc/group",
    "/etc/localtime",
    "/etc/alternatives",
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
    "/etc/ld.so.conf.d",
    "/etc/gitconfig",
)
#: System directories a confined child may read on Linux (each only if present).
LINUX_SYSTEM_READS: Final[tuple[str, ...]] = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64")
#: Environment names copied into a confined child, when set locally.
_ENV_KEPT: Final[tuple[str, ...]] = ("LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ")
_DEFAULT_PATH: Final[str] = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


class ConfinementUnavailable(RuntimeError):
    """No OS confinement can be applied here; the message is the user-facing reason."""


@dataclass(frozen=True)
class Confined:
    """A child ready to spawn: its argv, its whole environment, and fds it inherits.

    Spawn it with ``pass_fds=confined.pass_fds``, then :meth:`close` (spawned or
    not): the fds are the parent's copies of what the plan hands its wrapper.
    """

    argv: list[str]
    env: dict[str, str]
    cwd: str
    pass_fds: tuple[int, ...] = field(default=())

    def close(self) -> None:
        for fd in self.pass_fds:
            with contextlib.suppress(OSError):
                os.close(fd)


_R = TypeVar("_R")


async def run_owned(
    release: Callable[[_R], None], fn: Callable[..., _R], *args: Any, **kwargs: Any
) -> _R:
    """``fn(*args, **kwargs)`` in a worker thread; a result nobody takes is released.

    Cancelling a thread's await does not stop the thread. When the caller is
    cancelled first, the result (open fds, a temp folder) is handed to
    ``release`` on the same worker pool once it exists, so nothing leaks.
    """
    work = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        loop = asyncio.get_running_loop()

        def abandoned(done: "asyncio.Future[_R]") -> None:
            if not done.cancelled() and done.exception() is None:
                loop.run_in_executor(None, release, done.result())

        work.add_done_callback(abandoned)
        raise


async def wrap_off_loop(plan: Any, argv: list[str], **kwargs: Any) -> Confined:
    """``plan.wrap`` off the event loop: its probes, realpath walks and seccomp pipe.

    The caller owns the returned :class:`Confined` and must :meth:`~Confined.close`
    it; a wrap that finishes after the caller was cancelled is closed here.
    """
    return await run_owned(Confined.close, plan.wrap, argv, **kwargs)


def runtime_reads() -> list[str]:
    """The Python install that runs the tool server, as real paths."""
    out: list[str] = []
    for raw in {sys.base_prefix, sys.prefix, sys.exec_prefix}:
        real = os.path.realpath(raw)
        if real not in out:
            out.append(real)
    return out


def child_env(
    scratch: str, *, path: str = _DEFAULT_PATH, extra: Mapping[str, str] = _NO_ENV
) -> dict:
    """A child environment built from nothing but locale names and ``extra``."""
    env = {"PATH": path, "HOME": scratch, "TMPDIR": scratch, "USER": _user()}
    for name in _ENV_KEPT:
        value = os.environ.get(name)
        if value:
            env[name] = value
    for name, value in dict(extra).items():
        env[str(name)] = str(value)
    # Never a credential of the local gateway, whatever ``extra`` held.
    from kiro_crew.remote_session.worker import WITHHELD_ENV

    for name in WITHHELD_ENV:
        env.pop(name, None)
    return env


def _user() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # noqa: BLE001 -- the name is cosmetic for the child
        return "user"


def _bin(name: str) -> Optional[str]:
    from kiro_crew.platform_compat import trusted_system_bin_quiet

    return trusted_system_bin_quiet(name)


def _program_fd(machine: str) -> int:
    """A pipe's read end holding the seccomp program (it fits the pipe buffer)."""
    rfd, wfd = os.pipe()
    try:
        os.write(wfd, seccomp.program(machine))
    except BaseException:
        os.close(rfd)
        raise
    finally:
        os.close(wfd)
    return rfd


def _setenv_args_fd(env: Mapping[str, str]) -> int:
    """An unlinked file holding ``--setenv NAME VALUE`` for bubblewrap's ``--args FD``.

    Bubblewrap applies these inside the sandbox, just before it runs the child,
    so a value never reaches bubblewrap's own environment (where a loader
    variable such as ``LD_PRELOAD`` pointing into the shared root would run
    before any fence exists) nor an argv any local user can read in
    ``/proc/<pid>/cmdline``. A file, not a pipe: the arguments can outgrow a
    pipe buffer, which would block the write before bubblewrap reads.
    """
    for name, value in env.items():
        # The file is NUL-separated: an embedded NUL would add bubblewrap
        # options of its own (``--bind / /``), and ``=`` cannot be in a name.
        if not name or "\0" in name or "=" in name or "\0" in value:
            raise ConfinementUnavailable(
                "a setting for this tool holds a character it cannot be started with"
            )
    data = b"".join(
        b"--setenv\0" + name.encode("utf-8") + b"\0" + value.encode("utf-8") + b"\0"
        for name, value in env.items()
    )
    memfd = getattr(os, "memfd_create", None)
    if memfd is not None:
        fd = memfd("kirocrew-bwrap-env", getattr(os, "MFD_CLOEXEC", 0))
    else:
        with tempfile.TemporaryFile() as handle:
            fd = os.dup(handle.fileno())
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.lseek(fd, 0, os.SEEK_SET)
    except BaseException:
        os.close(fd)
        raise
    return fd


class LinuxPlan:
    name = PLAN_LINUX

    def __init__(self, bwrap: str, *, machine: Optional[str] = None) -> None:
        self.bwrap = bwrap
        self.machine = machine or platform_mod.machine()

    def wrap(
        self,
        argv: Sequence[str],
        *,
        root: str,
        scratch: str,
        reads: Sequence[str] = (),
        env: Mapping[str, str] = _NO_ENV,
    ) -> Confined:
        out = [
            self.bwrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup-try",
        ]
        for path in LINUX_SYSTEM_READS:
            if os.path.islink(path):
                out += ["--symlink", os.readlink(path), path]
            elif os.path.isdir(path):
                out += ["--ro-bind", path, path]
        for path in LINUX_ETC_READS:
            out += ["--ro-bind-try", path, path]
        out += ["--proc", "/proc", "--dev", "/dev"]
        out += ["--bind", scratch, scratch, "--bind", root, root]
        # After the root: a later mount wins, so these stay read-only even if
        # one of them lies inside the root.
        for path in [*runtime_reads(), *reads]:
            out += ["--ro-bind-try", path, path]
        out += ["--chdir", root]
        fd = _program_fd(self.machine)
        try:
            env_fd = _setenv_args_fd(child_env(scratch, extra=env))
        except BaseException:
            os.close(fd)
            raise
        out += ["--seccomp", str(fd), "--args", str(env_fd), "--", *argv]
        # Bubblewrap itself starts with the bare base environment; the child's
        # own settings arrive through ``--args`` and apply inside the sandbox.
        return Confined(out, child_env(scratch), root, pass_fds=(fd, env_fd))


def plan_for_this_machine(
    platform: str = sys.platform, *, machine: Optional[str] = None
) -> LinuxPlan:
    """This machine's plan, or raise :class:`ConfinementUnavailable` with the reason."""
    if platform.startswith("linux"):
        bwrap = _bin("bwrap")
        if bwrap is None:
            raise ConfinementUnavailable(
                "this computer cannot confine commands to the shared folder: install "
                "bubblewrap (bwrap) and try again"
            )
        arch = machine or platform_mod.machine()
        if arch not in seccomp.TABLES:
            raise ConfinementUnavailable(
                f"connected computers cannot confine commands on this processor ({arch}) yet"
            )
        return LinuxPlan(bwrap, machine=arch)
    raise ConfinementUnavailable(UNSUPPORTED_OS_REASON)


# -- the self-test ------------------------------------------------------------------

_PROBES: Final[tuple[str, ...]] = (
    "root_read",
    "root_write",
    "out_read",
    "out_write",
    "out_list",
    "link_read",
    "ipc",
    "net",
    "scratch_write",
    "keyring",
)


def keyctl_number(platform: str, machine: str) -> int:
    """The ``keyctl`` syscall number the Linux probe calls; 0 elsewhere (skipped)."""
    from kiro_crew.connected_computers import seccomp

    if not platform.startswith("linux"):
        return 0
    table = seccomp.TABLES.get(machine)
    return table.numbers["keyctl"] if table is not None else 0


def judge_self_test(
    control: Mapping[str, Any],
    confined: Mapping[str, Any],
    *,
    tokens: Mapping[str, str],
    keyring: bool = False,
) -> None:
    """Raise :class:`ConfinementUnavailable` unless both runs answered as a fence does.

    The control must reach every witness, or a "denied" from the confined run
    proves nothing. The confined run must read and write the root, write its
    own scratch through ``TMPDIR``, reach the network, and reach no witness
    outside the root.

    The kernel keyring (Linux) is the one witness the control may fail to
    reach: a host whose own seccomp refuses ``keyctl`` (a container's, often)
    has no keyring a child could use. The confined run must be refused either way.
    """
    reach = {
        "root_read": tokens["root"],
        "root_write": "allowed",
        "out_read": tokens["out"],
        "out_write": "allowed",
        "out_list": "allowed",
        "link_read": "denied",
        "ipc": "allowed",
        "net": "allowed",
        "scratch_write": "allowed",
        "keyring": "skip",
    }
    if keyring and control.get("keyring") in ("allowed", "denied"):
        # Either answer is a real one; see the docstring.
        reach["keyring"] = control["keyring"]
    elif keyring:
        reach["keyring"] = "allowed"
    fenced = dict(reach)
    for name in ("out_read", "out_write", "out_list", "link_read", "ipc"):
        fenced[name] = "denied"
    if keyring:
        fenced["keyring"] = "denied"
    for answers, want, message in (
        (control, reach, "the confinement check could not prepare its test ({})"),
        (
            confined,
            fenced,
            "this computer's sandbox did not confine a test command to the shared "
            "folder ({}), so the folder is not shared",
        ),
    ):
        if set(answers) != set(_PROBES):
            missing = sorted(set(_PROBES) ^ set(answers))
            raise ConfinementUnavailable(message.format(", ".join(missing) or "no answer"))
        for name in _PROBES:
            if answers[name] != want[name]:
                raise ConfinementUnavailable(message.format(name))


def _outside(path: str, root: str) -> bool:
    real, base = Path(os.path.realpath(path)), Path(os.path.realpath(root))
    return real != base and base not in real.parents


@dataclass
class _Witnesses:
    scratch: str
    out_dir: str
    root_paths: list[str]
    sockets: list[socket.socket]
    spec: dict[str, Any]
    tokens: dict[str, str]
    root_dir: str = ""

    def cleanup(self) -> None:
        for sock in self.sockets:
            with contextlib.suppress(OSError):
                sock.close()
        if self.root_dir:
            shutil.rmtree(self.root_dir, ignore_errors=True)
        shutil.rmtree(self.out_dir, ignore_errors=True)
        shutil.rmtree(self.scratch, ignore_errors=True)


def _prepare(root: str, platform: str) -> _Witnesses:
    """Create the witnesses the probe must reach unconfined and not reach confined."""
    # As the connector's own scratch is made. Real paths: the probe opens every
    # component without following links, so a symlinked /tmp or TMPDIR would
    # read as "denied" even unconfined.
    scratch = os.path.realpath(tempfile.mkdtemp(prefix="kirocrew-cc-check-"))
    parents = [
        p
        for p in (os.path.realpath(d) for d in (tempfile.gettempdir(), str(Path.home())))
        if _outside(p, root)
    ]
    w = _Witnesses(scratch, "", [], [], {}, {})
    try:
        if not parents:
            raise ConfinementUnavailable("the confinement check found no folder outside the root")
        w.out_dir = os.path.realpath(
            tempfile.mkdtemp(prefix="kirocrew-cc-witness-", dir=parents[0])
        )
        tag = secrets.token_hex(8)
        w.tokens = {"root": secrets.token_hex(16), "out": secrets.token_hex(16)}
        # Created under the root's real path and never resolved again: a
        # command already in the root could swap the new folder for a link
        # before a realpath, and the cleanup would then delete its target.
        w.root_dir = tempfile.mkdtemp(prefix=".kirocrew-cc-probe-", dir=os.path.realpath(root))
        root_file = os.path.join(w.root_dir, secrets.token_hex(16))
        root_new = os.path.join(w.root_dir, secrets.token_hex(16))
        link = os.path.join(w.root_dir, secrets.token_hex(16))
        out_file = os.path.join(w.out_dir, secrets.token_hex(16))
        w.root_paths = [root_file, root_new, link]
        from kiro_crew.platform_compat import pin_directory

        for parent, target, value in (
            (w.out_dir, out_file, w.tokens["out"]),
            (w.root_dir, root_file, w.tokens["root"]),
        ):
            fd = pin_directory(parent)
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                with os.fdopen(
                    os.open(os.path.basename(target), flags, 0o600, dir_fd=fd), "w"
                ) as f:
                    f.write(value)
            finally:
                os.close(fd)
        fd = pin_directory(w.root_dir)
        try:
            os.symlink(out_file, os.path.basename(link), dir_fd=fd)
        finally:
            os.close(fd)
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        w.sockets.append(tcp)
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(4)
        unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        w.sockets.append(unix)
        # An abstract socket: the session endpoints (X11, a bus) no mount hides.
        abstract = f"kirocrew-cc-witness-{tag}"
        unix.bind("\0" + abstract)
        unix.listen(4)
        w.spec = {
            "root_file": root_file,
            "root_new": root_new,
            "out_file": out_file,
            "out_new": os.path.join(w.out_dir, secrets.token_hex(16)),
            "out_dir": w.out_dir,
            "out_name": os.path.basename(out_file),
            "link": link,
            "scratch_name": f".kirocrew-cc-scratch-{tag}",
            "tcp_port": tcp.getsockname()[1],
            "ipc_abstract": abstract,
            "keyctl": keyctl_number(platform, platform_mod.machine()),
        }
        return w
    except BaseException:
        w.cleanup()
        raise


#: The self-test's probe script, resolved once at import so no probe touches the disk
#: on the event loop.
_PROBE_PATH: Final[str] = str(Path(__file__).resolve().with_name("confinement_probe.py"))


async def _run_probe(plan: Any, w: _Witnesses, root: str, timeout: float) -> dict[str, Any]:
    probe_path = _PROBE_PATH
    argv = [sys.executable, "-I", probe_path, json.dumps(w.spec)]
    if plan is None:
        child = Confined(argv, child_env(w.scratch), root)
    else:
        child = await wrap_off_loop(plan, argv, root=root, scratch=w.scratch, reads=[probe_path])
    try:
        proc = await asyncio.create_subprocess_exec(
            *child.argv,
            cwd=child.cwd,
            env=child.env,
            pass_fds=child.pass_fds,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    finally:
        child.close()
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        from kiro_crew.platform_compat import kill_and_reap

        # Cancelled (a revoke, a shutdown) or too slow: the probe never outlives
        # this check, and the caller releases the witnesses only after this.
        await asyncio.shield(kill_and_reap(proc))
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise ConfinementUnavailable("the confinement check did not finish") from exc
    lines = out.decode("ascii", "replace").strip().splitlines()
    if proc.returncode != 0 or not lines:
        return {}
    try:
        answers = json.loads(lines[-1])
    except ValueError:
        return {}
    return answers if isinstance(answers, dict) else {}


async def self_test(
    plan: LinuxPlan, root: str, *, timeout: float = 20.0, platform: str = sys.platform
) -> None:
    """Prove ``plan`` holds on this machine for ``root``, or raise the reason.

    Run once per connector start, before the connector tells the gateway it is
    ready. The probe runs unconfined first, against witnesses created for this
    check (a file, a folder and an abstract unix socket outside the root, a
    symlink in the root pointing out, a loopback TCP listener and the session
    keyring), so every "denied" in the confined run is the fence's doing.
    """
    # Owned: a cancellation while the worker still prepares releases them too.
    w = await run_owned(_Witnesses.cleanup, _prepare, root, platform)
    try:
        control = await _run_probe(None, w, root, timeout)
        confined = await _run_probe(plan, w, root, timeout)
    finally:
        await asyncio.shield(asyncio.to_thread(w.cleanup))
    judge_self_test(
        control,
        confined,
        tokens=w.tokens,
        keyring=bool(w.spec["keyctl"]),
    )
