"""Hand a path to the editor attached to this host.

A sibling in intent to :func:`platform_compat.reveal_in_file_manager`: both take
a path the gateway holds and give it to a program on the gateway host. The
difference is which program, and that difference is the whole reason this module
exists. A file manager runs on the machine the gateway runs on, so for a user
reaching the dashboard over an SSH tunnel it opens a window on a machine they are
not sitting at. An editor attached over Remote-SSH is the opposite: its window is
on the user's own desk and its file handles reach back here, so a path opened
through it lands where the user is looking.

Kiro's Remote-SSH server unpacks a VS Code-style remote CLI under
``~/.kiro-server/bin/<commit>/bin/remote-cli/kiro``. That CLI talks to the desktop
IDE over a unix socket the extension host creates in the temp directory, named
``vscode-ipc-<uuid>.sock``, and reads the socket path from
``VSCODE_IPC_HOOK_CLI``.

Two facts shape the resolution below:

* There is one socket per IDE window, and the file outlives the window that
  created it, so a host with weeks of uptime carries dozens that answer nothing.
  ``connect()`` is the liveness test, and it has to be: the CLI's own ``--status``
  spends several seconds on SSH round trips, which makes it useless as a probe.
* A socket exists only while a window is connected to THIS host. With no window
  attached there is nowhere to open into, which is a state a UI renders rather
  than an error worth retrying — hence :func:`editor_status`, which answers
  whether an open would work and says why when it would not.

Standard library only, so the gateway interpreter imports it with no extra
dependency.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import socket
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

#: Where the remote server unpacks one tree per IDE build it has seen.
_SERVER_BIN_REL = (".kiro-server", "bin")

#: Glob, relative to a server-bin directory, matching one build's remote CLI.
_REMOTE_CLI_GLOB = "*/bin/remote-cli/kiro"

#: Liveness probe budget. A live socket accepts immediately and a dead one
#: refuses immediately, so this only covers a pathological half-open case.
_CONNECT_TIMEOUT = 1.0

#: The CLI returns as soon as the IDE acknowledges the open, so this bound is
#: generous — it is here to stop a wedged socket hanging a request forever.
_OPEN_TIMEOUT = 20.0

#: The launcher is a short shell stub that hardcodes ``VERSION`` and ``COMMIT``,
#: so the version is a file read rather than a multi-second ``--status`` call.
_VERSION_RE = re.compile(r'^VERSION="([^"]+)"', re.MULTILINE)

#: How much of the launcher stub is read to find that line. The stub is a dozen
#: lines; a cap keeps a path that is unexpectedly large from being slurped whole.
_LAUNCHER_READ_BYTES = 8192

#: Machine-readable reasons :func:`editor_status` reports. A UI renders the
#: matching sentence; nothing here is prose, so a caller that gates an
#: affordance never parses a message to decide.
REASON_OK = "ok"
REASON_NO_SERVER = "no_server"
REASON_NO_WINDOW = "no_window"


class EditorUnavailable(RuntimeError):
    """No editor is reachable from this host right now."""


class EditorOpenFailed(RuntimeError):
    """An editor was reached but refused or failed the open."""


def _server_bin() -> Path:
    """The remote server's build directory, resolved against the live home.

    Resolved per call rather than captured at import so a test that repoints
    ``Path.home`` is observed, matching how the data-home readers in this tree
    resolve their own roots.
    """
    return Path.home().joinpath(*_SERVER_BIN_REL)


def _socket_dirs() -> list[str]:
    """Directories that may hold IPC sockets, most likely first.

    The IDE server uses the platform temp directory, which is ``/tmp`` in
    practice. The gateway's own process may run with ``TMPDIR`` pointed at a
    private scratch directory, so ``/tmp`` is checked explicitly rather than
    inferred from this process's environment.
    """
    seen: list[str] = []
    for candidate in ("/tmp", tempfile.gettempdir()):
        if candidate and candidate not in seen and os.path.isdir(candidate):
            seen.append(candidate)
    return seen


def _mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def _candidate_sockets() -> list[str]:
    """Every IPC socket file on disk, newest first.

    Newest first because the most recently created window is the one the user is
    working in. Ordering is all this does — whether any of them answers is
    :func:`_accepts`'s question.
    """
    found: list[str] = []
    for directory in _socket_dirs():
        found.extend(glob.glob(os.path.join(directory, "vscode-ipc-*.sock")))
    found.sort(key=_mtime, reverse=True)
    return found


def _accepts(path: str) -> bool:
    """Whether something is listening on the unix socket at *path*.

    A ``connect()`` and an immediate close: the only cheap test that tells a live
    endpoint from an abandoned socket file. ``AF_UNIX`` is absent on a platform
    without unix sockets, so its absence answers False rather than raising.
    """
    family = getattr(socket, "AF_UNIX", None)
    if family is None:
        return False
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(_CONNECT_TIMEOUT)
    try:
        sock.connect(path)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def live_socket() -> str | None:
    """The newest socket a window is still listening on, or ``None``."""
    for path in _candidate_sockets():
        if _accepts(path):
            return path
    return None


def remote_cli() -> Path | None:
    """The newest unpacked remote CLI, or ``None`` when the IDE never connected.

    Newest by mtime rather than by the commit hash in the directory name: the
    name carries no ordering, and a host that has seen several IDE builds keeps
    every tree it unpacked.
    """
    try:
        builds = sorted(
            _server_bin().glob(_REMOTE_CLI_GLOB),
            key=lambda candidate: candidate.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    return builds[0] if builds else None


def _cli_version(cli: Path) -> str | None:
    """The IDE version, read out of the launcher stub. ``None`` when unreadable."""
    try:
        with open(cli, "r", encoding="utf-8", errors="replace") as stub:
            head = stub.read(_LAUNCHER_READ_BYTES)
    except OSError:
        return None
    found = _VERSION_RE.search(head)
    return found.group(1) if found else None


def editor_status() -> dict[str, object]:
    """Whether an open would work right now, and why not when it would not.

    Returns ``available`` (the gate a UI reads), ``reason`` (one of the
    ``REASON_*`` constants, for a UI to turn into its own localized sentence),
    ``version`` and ``connections``.
    """
    cli = remote_cli()
    if cli is None:
        return {
            "available": False,
            "reason": REASON_NO_SERVER,
            "version": None,
            "connections": 0,
        }

    sockets = _candidate_sockets()
    if live_socket() is None:
        return {
            "available": False,
            "reason": REASON_NO_WINDOW,
            "version": _cli_version(cli),
            # Deliberately the LIVE count, which is zero here: a caller reading
            # this field wants what it can open into, and the sockets on disk are
            # abandoned files.
            "connections": 0,
        }

    return {
        "available": True,
        "reason": REASON_OK,
        "version": _cli_version(cli),
        # Deliberately "connections", not "windows": one window contributes more
        # than one live endpoint (the extension host and each of its terminals
        # get a socket), so this count runs ahead of the window count the IDE's
        # own --status reports.
        "connections": sum(1 for candidate in sockets if _accepts(candidate)),
    }


def project_dir() -> str | None:
    """The project root this gateway was launched for, or ``None``.

    ``KIROCREW_PROJECT_DIR`` names it, and a value that is not a directory is
    treated as absent: a stale path resolves a relative target somewhere that no
    longer exists, and reporting that as unresolved is more useful than handing
    the editor a path under it.
    """
    val = os.environ.get("KIROCREW_PROJECT_DIR", "").strip()
    if val and os.path.isdir(val):
        return val
    return None


def resolve_target(path: str, *, base: str | None = None) -> str | None:
    """Absolutize a target path as written, or ``None`` when it is ambiguous.

    A transcript renders a path however the text spelled it, so what arrives here
    is `~/notes.md` or `website/src/main.tsx` as often as an absolute path, and
    the editor CLI resolves a relative argument against THIS process's working
    directory -- a directory the writer of the text never named. Anchoring it
    explicitly is what makes the opened file the named one.

    Three shapes, three answers: an absolute path is returned normalized, a ``~``
    prefix expands against the running user's home, and a relative path joins
    *base* (defaulting to :func:`project_dir`). With no base to join, the answer
    is ``None`` rather than a guess, because every candidate base opens a
    different file and a silently wrong file is worse than a refusal.

    Symlinks are deliberately NOT resolved: normalization here is lexical, so the
    string a caller screens for sensitive paths is the string handed to the
    editor, with no second filesystem read between the two for a swap to land in.
    """
    raw = path.strip()
    if not raw:
        return None
    if raw.startswith("~"):
        expanded = os.path.expanduser(raw)
        # expanduser returns the input unchanged for an unknown ~user, which
        # would otherwise be joined to the base as if it were relative.
        return os.path.normpath(expanded) if os.path.isabs(expanded) else None
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    anchor = base if base is not None else project_dir()
    if not anchor:
        return None
    return os.path.normpath(os.path.join(anchor, raw))


def open_path(path: str, *, reuse: bool = False) -> str:
    """Open *path* in the attached editor. Returns the socket that served it.

    *reuse* replaces the folder in the most recent window instead of opening a
    new one.

    Blocking: it spawns a process and waits for the IDE to acknowledge. Callers
    on the event loop hand it to a worker thread.
    """
    cli = remote_cli()
    if cli is None:
        raise EditorUnavailable(
            "No editor remote CLI is installed on this host. Connect the editor "
            "to it once over Remote-SSH and it installs itself."
        )
    sock = live_socket()
    if sock is None:
        raise EditorUnavailable(
            "No editor window is connected to this host, so there is nowhere to " "open the file."
        )
    env = dict(os.environ)
    env["VSCODE_IPC_HOOK_CLI"] = sock
    _run([str(cli), "-r" if reuse else "-n", path], env=env)
    return sock


def _run(argv: list[str], *, env: dict[str, str] | None) -> None:
    """Run *argv* to completion, translating a failure into a typed error.

    ``shell=False`` with a list argv, so the path never reaches a shell.
    """
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_OPEN_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise EditorOpenFailed(
            f"The editor did not acknowledge the open within {_OPEN_TIMEOUT:.0f}s."
        ) from exc
    except OSError as exc:
        # A missing or non-executable program. The message names no path: the
        # caller is a dashboard response, and the launcher location is a fact
        # about this host rather than something the clicking user can act on.
        logger.warning("editor open did not start: %s", argv[0], exc_info=True)
        raise EditorOpenFailed("The editor command could not be started.") from exc
    if done.returncode != 0:
        message = (done.stderr or done.stdout or "").strip()
        raise EditorOpenFailed(message or f"The editor command exited {done.returncode}.")
