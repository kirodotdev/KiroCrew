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
* **macOS** -- ``sandbox-exec`` with a ``(deny default)`` profile allowing reads
  of the system runtime, Homebrew and the developer tools, the Python install and
  the tool-server files; reads and writes under the root and the scratch; the
  Mach services in :data:`MACOS_MACH_SERVICES` only; and unix sockets only under
  the root and the scratch (plus the system name resolver).
* **anything else** (Windows included) -- unsupported: there is no plan, and the
  connector refuses to start rather than run unconfined (B06).

There is no fallback. A missing ``bwrap``/``sandbox-exec``, a kernel without
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
  On macOS it is the Mach allow-list (LaunchServices, Apple Events,
  preferences, the keychain and the pasteboard are not on it) and the unix
  socket paths.

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
PLAN_MACOS: Final[str] = "macos-seatbelt"

#: The ``/etc`` entries a confined child may read on Linux (each only if present).
LINUX_ETC_READS: Final[tuple[str, ...]] = (
    "/etc/ssl",
    "/etc/ca-certificates",
    "/etc/pki",
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
#: Paths a confined child may read on macOS.
MACOS_READS: Final[tuple[str, ...]] = (
    "/usr",
    "/bin",
    "/sbin",
    "/System",
    "/Library/Apple",
    "/Library/Developer/CommandLineTools",
    "/Applications/Xcode.app",
    "/opt/homebrew",
    "/usr/local",
    "/private/etc",
    "/private/var/db/timezone",
    "/dev",
)
#: The Mach services a confined child may look up on macOS: user and group
#: lookups, notifications, logging, certificate trust and name resolution.
#: Anything that acts for the user outside the root -- LaunchServices (``open``),
#: Apple Events, preferences, the keychain, the pasteboard, FSEvents -- is absent.
MACOS_MACH_SERVICES: Final[tuple[str, ...]] = (
    "com.apple.system.opendirectoryd.libinfo",
    "com.apple.system.opendirectoryd.membership",
    "com.apple.system.DirectoryService.libinfo_v1",
    "com.apple.system.DirectoryService.membership_v1",
    "com.apple.system.notification_center",
    "com.apple.system.logger",
    "com.apple.logd",
    "com.apple.diagnosticd",
    "com.apple.trustd",
    "com.apple.trustd.agent",
    "com.apple.dnssd.service",
)
#: The unix socket the macOS name resolver listens on.
MACOS_RESOLVER_SOCKET: Final[str] = "/private/var/run/mDNSResponder"
#: An excluded Mach service the macOS self-test must fail to reach. It runs in
#: every session, so the unconfined control run can always reach it.
MACOS_MACH_WITNESS: Final[str] = "com.apple.cfprefsd.daemon"
#: Environment names copied into a confined child, when set locally.
_ENV_KEPT: Final[tuple[str, ...]] = ("LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ")
_DEFAULT_PATH: Final[str] = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
_MACOS_PATH: Final[str] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


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
        for path in [*runtime_reads(), *reads]:
            out += ["--ro-bind-try", path, path]
        out += ["--proc", "/proc", "--dev", "/dev"]
        out += ["--bind", scratch, scratch, "--bind", root, root, "--chdir", root]
        fd = _program_fd(self.machine)
        out += ["--seccomp", str(fd), "--", *argv]
        # The child's whole environment is bwrap's own spawn environment, which
        # bwrap passes through: values never sit in an argv any local user can
        # read in /proc/<pid>/cmdline.
        return Confined(out, child_env(scratch, extra=env), root, pass_fds=(fd,))


def _sbpl_string(path: str) -> str:
    if any(ch in path for ch in ('"', "\\", "\n", "\r", "\x00")):
        raise ConfinementUnavailable("this folder's name cannot be confined on macOS")
    return f'"{path}"'


def macos_profile(*, root: str, scratch: str, reads: Sequence[str]) -> str:
    """The Seatbelt profile for one child: deny by default, allow-list below."""
    read_paths = [*MACOS_READS, *runtime_reads(), *reads]
    ancestors = sorted(
        {str(p) for p in Path(root).parents} | {str(p) for p in Path(scratch).parents}
    )
    mach = " ".join(f'(global-name "{name}")' for name in MACOS_MACH_SERVICES)
    owned = f"(subpath {_sbpl_string(root)}) (subpath {_sbpl_string(scratch)})"
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-fork)",
        "(allow process-exec)",
        "(allow signal (target same-sandbox))",
        "(allow sysctl-read)",
        f"(allow mach-lookup {mach})",
        "(allow ipc-posix-shm)",
        # Network access: IP, as the user's shell has it.
        '(allow network-outbound (remote ip "*:*"))',
        '(allow network-inbound (local ip "*:*"))',
        '(allow network-bind (local ip "*:*"))',
        # Credential protection: unix sockets only in the root and the scratch.
        f"(allow network-outbound (literal {_sbpl_string(MACOS_RESOLVER_SOCKET)}))",
        f"(allow network-outbound network-inbound network-bind {owned})",
        "(allow file-read-metadata "
        + " ".join(f"(literal {_sbpl_string(a)})" for a in ancestors)
        + ")",
        "(allow file-read* " + " ".join(f"(subpath {_sbpl_string(p)})" for p in read_paths) + ")",
        f"(allow file-read* file-write* (subpath {_sbpl_string(root)}))",
        f"(allow file-read* file-write* (subpath {_sbpl_string(scratch)}))",
        '(allow file-write* (literal "/dev/null") (literal "/dev/tty") (literal "/dev/dtracehelper"))',
    ]
    return "\n".join(lines) + "\n"


class MacPlan:
    name = PLAN_MACOS

    def __init__(self, sandbox_exec: str) -> None:
        self.sandbox_exec = sandbox_exec

    def wrap(
        self,
        argv: Sequence[str],
        *,
        root: str,
        scratch: str,
        reads: Sequence[str] = (),
        env: Mapping[str, str] = _NO_ENV,
    ) -> Confined:
        # Seatbelt matches the resolved path: a scratch from mkdtemp sits under
        # /var/folders, which is /private/var/folders, so the profile, HOME and
        # TMPDIR all name the real path or every write there is denied.
        root, scratch = os.path.realpath(root), os.path.realpath(scratch)
        profile = macos_profile(root=root, scratch=scratch, reads=reads)
        return Confined(
            [self.sandbox_exec, "-p", profile, *argv],
            child_env(scratch, path=_MACOS_PATH, extra=env),
            root,
        )


def plan_for_this_machine(
    platform: str = sys.platform, *, machine: Optional[str] = None
) -> "LinuxPlan | MacPlan":
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
    if platform == "darwin":
        sandbox_exec = _bin("sandbox-exec")
        if sandbox_exec is None:
            raise ConfinementUnavailable(
                "this Mac has no sandbox-exec, so commands cannot be confined"
            )
        return MacPlan(sandbox_exec)
    raise ConfinementUnavailable(
        "connected computers is not available on this operating system yet: it cannot "
        "confine commands to the shared folder here"
    )


# -- the self-test ------------------------------------------------------------------

#: The probe, run twice by :func:`self_test`: once unconfined (the control, which
#: must reach every witness) and once confined. Prints one JSON object.
_PROBE = r"""
import json, os, socket, sys
spec = json.loads(sys.argv[1])
def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return "denied"
def write(path):
    try:
        with open(path, "w") as f:
            f.write("x")
    except OSError:
        return "denied"
    try:
        os.unlink(path)
    except OSError:
        pass
    return "allowed"
def listing(path, name):
    try:
        return "allowed" if name in os.listdir(path) else "denied"
    except OSError:
        return "denied"
def connect(family, address):
    try:
        s = socket.socket(family, socket.SOCK_STREAM)
    except OSError:
        return "denied"
    try:
        s.settimeout(5)
        s.connect(address)
        return "allowed"
    except OSError:
        return "denied"
    finally:
        s.close()
def mach(name):
    try:
        import ctypes
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        bp = ctypes.c_uint.in_dll(lib, "bootstrap_port")
        port = ctypes.c_uint(0)
        kr = lib.bootstrap_look_up(bp, name.encode(), ctypes.byref(port))
    except Exception:
        return "error"
    return "allowed" if kr == 0 and port.value else "denied"
def keyring(nr):
    # keyctl(KEYCTL_GET_KEYRING_ID, KEY_SPEC_SESSION_KEYRING, 0): reads only.
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        got = libc.syscall(ctypes.c_long(nr), ctypes.c_long(0), ctypes.c_long(-3), ctypes.c_long(0))
    except Exception:
        return "error"
    return "allowed" if got >= 0 else "denied"
out = {
    "root_read": read(spec["root_file"]),
    "root_write": write(spec["root_new"]),
    "out_read": read(spec["out_file"]),
    "out_write": write(spec["out_new"]),
    "out_list": listing(spec["out_dir"], spec["out_name"]),
    "link_read": read(spec["link"]),
    "net": connect(socket.AF_INET, ("127.0.0.1", spec["tcp_port"])),
    # Where the child's own temp files go: HOME and TMPDIR are the scratch.
    "scratch_write": write(os.path.join(os.environ.get("TMPDIR", ""), spec["scratch_name"])),
}
if spec["ipc_abstract"]:
    out["ipc"] = connect(socket.AF_UNIX, "\0" + spec["ipc_abstract"])
else:
    out["ipc"] = connect(socket.AF_UNIX, spec["ipc_path"])
out["mach"] = mach(spec["mach"]) if spec["mach"] else "skip"
out["keyring"] = keyring(spec["keyctl"]) if spec["keyctl"] else "skip"
print(json.dumps(out))
"""

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
    "mach",
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
    mach: bool,
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
        "link_read": tokens["out"],
        "ipc": "allowed",
        "net": "allowed",
        "scratch_write": "allowed",
        "mach": "allowed" if mach else "skip",
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
    if mach:
        fenced["mach"] = "denied"
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

    def cleanup(self) -> None:
        for sock in self.sockets:
            with contextlib.suppress(OSError):
                sock.close()
        for path in self.root_paths:
            with contextlib.suppress(OSError):
                os.unlink(path)
        shutil.rmtree(self.out_dir, ignore_errors=True)
        shutil.rmtree(self.scratch, ignore_errors=True)


def _prepare(root: str, platform: str) -> _Witnesses:
    """Create the witnesses the probe must reach unconfined and not reach confined."""
    # As the connector's own scratch is made; the plan resolves it (macOS).
    scratch = tempfile.mkdtemp(prefix="kirocrew-cc-check-")
    parents = [p for p in (tempfile.gettempdir(), str(Path.home())) if _outside(p, root)]
    w = _Witnesses(scratch, "", [], [], {}, {})
    try:
        if not parents:
            raise ConfinementUnavailable("the confinement check found no folder outside the root")
        w.out_dir = tempfile.mkdtemp(prefix="kirocrew-cc-witness-", dir=parents[0])
        tag = secrets.token_hex(8)
        w.tokens = {"root": secrets.token_hex(16), "out": secrets.token_hex(16)}
        root_file = os.path.join(root, f".kirocrew-cc-check-{tag}")
        root_new = os.path.join(root, f".kirocrew-cc-write-{tag}")
        link = os.path.join(root, f".kirocrew-cc-link-{tag}")
        out_file = os.path.join(w.out_dir, "witness")
        w.root_paths = [root_file, root_new, link]
        with open(out_file, "w", encoding="ascii") as f:
            f.write(w.tokens["out"])
        with open(root_file, "w", encoding="ascii") as f:
            f.write(w.tokens["root"])
        os.symlink(out_file, link)
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        w.sockets.append(tcp)
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(4)
        unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        w.sockets.append(unix)
        abstract, ipc_path = "", ""
        if platform.startswith("linux"):
            abstract = f"kirocrew-cc-witness-{tag}"
            unix.bind("\0" + abstract)
        else:
            ipc_path = os.path.join(w.out_dir, "s")
            unix.bind(ipc_path)
        unix.listen(4)
        w.spec = {
            "root_file": root_file,
            "root_new": root_new,
            "out_file": out_file,
            "out_new": os.path.join(w.out_dir, "new"),
            "out_dir": w.out_dir,
            "out_name": "witness",
            "link": link,
            "scratch_name": f".kirocrew-cc-scratch-{tag}",
            "tcp_port": tcp.getsockname()[1],
            "ipc_abstract": abstract,
            "ipc_path": ipc_path,
            "mach": MACOS_MACH_WITNESS if platform == "darwin" else "",
            "keyctl": keyctl_number(platform, platform_mod.machine()),
        }
        return w
    except BaseException:
        w.cleanup()
        raise


async def _run_probe(plan: Any, w: _Witnesses, root: str, timeout: float) -> dict[str, Any]:
    argv = [sys.executable, "-I", "-c", _PROBE, json.dumps(w.spec)]
    if plan is None:
        child = Confined(argv, child_env(w.scratch), root)
    else:
        child = await wrap_off_loop(plan, argv, root=root, scratch=w.scratch)
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
    plan: "LinuxPlan | MacPlan", root: str, *, timeout: float = 20.0, platform: str = sys.platform
) -> None:
    """Prove ``plan`` holds on this machine for ``root``, or raise the reason.

    Run once per connector start, before the connector tells the gateway it is
    ready. The probe runs unconfined first, against witnesses created for this
    check (a file, a folder and a unix socket outside the root, a symlink in the
    root pointing out, a loopback TCP listener, on macOS an excluded Mach
    service, on Linux the session keyring), so every "denied" in the confined
    run is the fence's doing.
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
        mach=platform == "darwin",
        keyring=bool(w.spec["keyctl"]),
    )
