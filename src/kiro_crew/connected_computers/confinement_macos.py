"""Local machine (macOS): the Seatbelt plan every connector child runs under.

``sandbox-exec`` with a ``(deny default)`` profile built per child:

* reads of the system runtime (:data:`MACOS_READS`: under a package prefix only
  its toolchain, never its ``var`` or ``etc``), the Python install and the
  tool-server files, and reads and writes under the root and the scratch;
* stat-only access to the folders above the root and the scratch, so a path can
  be resolved without any of them becoming listable;
* IP networking as the user's shell has it (network access policy);
* the Mach services in :data:`MACOS_MACH_SERVICES` only, and unix sockets only
  under the root, the scratch and the system name resolver (credential
  protection: LaunchServices, Apple Events, preferences, the keychain and the
  pasteboard are not reachable).

Seatbelt is last-match-wins between an allow and a deny, so the order is the
policy: the read-only runtime and tool paths are denied writes AFTER the root
and the scratch are allowed them, and stay read-only even inside the root.

The child's environment never reaches ``sandbox-exec``: it is written to an
unlinked file in the scratch and read by :mod:`.confinement_exec` inside the
fence, which then becomes the child.
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path
from typing import Final, Mapping, Sequence

from kiro_crew.connected_computers.confinement import (
    _NO_ENV,
    PLAN_MACOS,
    Confined,
    ConfinementUnavailable,
    check_env,
    child_env,
    runtime_reads,
)

#: The package prefixes a confined child's tools come from: Homebrew on Apple
#: silicon, and Homebrew on Intel or a hand-built install under ``/usr/local``.
MACOS_PREFIXES: Final[tuple[str, ...]] = ("/opt/homebrew", "/usr/local")
#: The toolchain subtrees of each prefix a child may read. Never ``var`` (a
#: service's data: PostgreSQL, MySQL, Redis) or ``etc`` (its configs), which
#: belong to the user, not to the tools.
MACOS_PREFIX_TOOLCHAIN: Final[tuple[str, ...]] = (
    "bin",
    "sbin",
    "lib",
    "libexec",
    "include",
    "share",
    "opt",
    "Cellar",
    "Frameworks",
)
#: The user data folders of each prefix, denied again after every read allow,
#: so a Python install at a prefix (``runtime_reads``) cannot reopen them.
MACOS_PREFIX_DATA: Final[tuple[str, ...]] = ("var", "etc")
#: The certificate bundles Homebrew's OpenSSL and curl read from ``etc``.
MACOS_PREFIX_CERTS: Final[tuple[str, ...]] = (
    "etc/ca-certificates/cert.pem",
    "etc/openssl@3/cert.pem",
    "etc/openssl@3/openssl.cnf",
    "etc/openssl@1.1/cert.pem",
    "etc/openssl@1.1/openssl.cnf",
    # Homebrew git's system config: git stops on EPERM reading it, so a deny
    # here would fail every git command and git view on that Mac.
    "etc/gitconfig",
    "etc/gitattributes",
)
#: Paths a confined child may read on macOS: the system, the developer tools,
#: and only the toolchain part of each package prefix. ``/usr`` is listed by
#: its system subtrees, since ``/usr/local`` is a package prefix under it.
MACOS_READS: Final[tuple[str, ...]] = (
    "/usr/bin",
    "/usr/sbin",
    "/usr/lib",
    "/usr/libexec",
    "/usr/share",
    "/usr/standalone",
    "/bin",
    "/sbin",
    "/System",
    "/Library/Apple",
    "/Library/Developer/CommandLineTools",
    "/Applications/Xcode.app",
    *(f"{prefix}/{sub}" for prefix in MACOS_PREFIXES for sub in MACOS_PREFIX_TOOLCHAIN),
    "/private/etc",
    "/private/var/db/timezone",
    # Only the devices a child needs, never all of /dev: the user's other
    # terminals (/dev/ttysNNN) are readable by them and carry what they type.
    "/dev/null",
    "/dev/zero",
    "/dev/random",
    "/dev/urandom",
    "/dev/tty",
    "/dev/dtracehelper",
    "/dev/fd",
)
#: Folders a path through the reads above is resolved in: stat only, not listable.
_READ_PARENTS: Final[tuple[str, ...]] = ("/usr", "/opt", *MACOS_PREFIXES)
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
MACOS_PATH: Final[str] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
#: Device files a child may write: output sinks and the terminal, nothing else.
_DEV_WRITES: Final[tuple[str, ...]] = ("/dev/null", "/dev/tty", "/dev/dtracehelper")
#: The in-fence launcher, resolved once at import so no wrap touches the disk for it.
LAUNCHER_PATH: Final[str] = str(Path(__file__).resolve().with_name("confinement_exec.py"))


def _sbpl(path: str) -> str:
    """``path`` as one SBPL string literal (``sandbox_seatbelt._quoted``'s escaping).

    A line break or NUL is refused rather than escaped: the profile is one
    argv string, and no real folder needs either.
    """
    if any(ch in path for ch in ("\n", "\r", "\x00")):
        raise ConfinementUnavailable("this folder's name cannot be confined on macOS")
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _subpaths(paths: Sequence[str]) -> str:
    return " ".join(f"(subpath {_sbpl(p)})" for p in paths)


def _prefix_paths(names: Sequence[str]) -> list[str]:
    return [f"{prefix}/{name}" for prefix in MACOS_PREFIXES for name in names]


def macos_profile(*, root: str, scratch: str, reads: Sequence[str]) -> str:
    """The Seatbelt profile for one child: deny by default, the allow-list below."""
    read_only = [*runtime_reads(), *reads]
    ancestors = sorted(
        {str(p) for p in Path(root).parents} | {str(p) for p in Path(scratch).parents}
    )
    mach = " ".join(f"(global-name {_sbpl(name)})" for name in MACOS_MACH_SERVICES)
    owned = _subpaths([root, scratch])
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
        f"(allow network-outbound (literal {_sbpl(MACOS_RESOLVER_SOCKET)}))",
        f"(allow network-outbound network-inbound network-bind {owned})",
        "(allow file-read-metadata " + " ".join(f"(literal {_sbpl(a)})" for a in ancestors) + ")",
        f"(allow file-read* {_subpaths([*MACOS_READS, *read_only])})",
        "(allow file-read-metadata "
        + " ".join(f"(literal {_sbpl(d)})" for d in _READ_PARENTS)
        + ")",
        # Last match wins: a prefix's data stays unreadable whatever allowed it
        # above, then only its certificate bundles are readable again.
        f"(deny file-read* {_subpaths(_prefix_paths(MACOS_PREFIX_DATA))})",
        "(allow file-read* "
        + " ".join(f"(literal {_sbpl(f)})" for f in _prefix_paths(MACOS_PREFIX_CERTS))
        + ")",
        f"(allow file-read* file-write* {owned})",
        # After the root: last match wins, so these stay read-only inside it too.
        f"(deny file-write* {_subpaths(read_only)})",
        "(allow file-write* " + " ".join(f"(literal {_sbpl(d)})" for d in _DEV_WRITES) + ")",
    ]
    return "\n".join(lines) + "\n"


def _env_fd(scratch: str, env: Mapping[str, str]) -> int:
    """An unlinked file in the scratch holding ``env`` for :mod:`.confinement_exec`.

    In the scratch, which the profile lets the child read, and created
    exclusive, owner-only and never through a link. A file, not a pipe: the
    settings can outgrow a pipe buffer, which would block before the child reads.
    """
    check_env(env)
    data = b"".join(os.fsencode(n) + b"\0" + os.fsencode(v) + b"\0" for n, v in env.items())
    path = os.path.join(scratch, ".kirocrew-env-" + secrets.token_hex(16))
    # O_NOFOLLOW where it exists: this plan only ever runs on macOS.
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.unlink(path)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.lseek(fd, 0, os.SEEK_SET)
    except BaseException:
        os.close(fd)
        raise
    return fd


class MacPlan:
    name = PLAN_MACOS
    fenced = True

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
        profile = macos_profile(root=root, scratch=scratch, reads=[*reads, LAUNCHER_PATH])
        fd = _env_fd(scratch, child_env(scratch, path=MACOS_PATH, extra=env))
        out = [
            self.sandbox_exec,
            "-p",
            profile,
            sys.executable,
            "-I",
            "-S",
            LAUNCHER_PATH,
            str(fd),
            "--",
            *argv,
        ]
        # sandbox-exec and the launcher start with the bare base environment.
        return Confined(out, child_env(scratch, path=MACOS_PATH), root, pass_fds=(fd,))
