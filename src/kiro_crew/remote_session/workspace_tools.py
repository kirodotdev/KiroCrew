"""Guest-side MCP tool server (JSON-RPC lines on stdio) for a remote workspace,
started by ``worker.py``. Stdlib-only and Python 3.9 compatible.

Paths resolve through symlinks and are refused outside ``--root``; results are
capped. ``run_command`` is an unrestricted shell: the gateway approves each call.
"""

from __future__ import annotations

import argparse
import codecs
import contextlib
import errno
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

SERVER_NAME = "kirocrew-workspace"
SERVER_VERSION = "1"
DEFAULT_PROTOCOL = "2024-11-05"
MAX_READ_BYTES = 256 * 1024
MAX_WRITE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_LIST_ENTRIES = 1000
MAX_MATCHES = 200
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 1800
LOCK_DIR = ".kirocrew-file-locks"
# Matches platform_lock_compat; this guest file ships without the gateway package.
_LOCK_POLL_SECS = 0.01
#: This script's own directory: browsing runs it, so no workspace write may replace it.
_SELF_DIR = os.path.dirname(os.path.realpath(__file__))


def _is_within(path: str, root: str) -> bool:
    """Return True if ``path`` is inside (or equal to) ``root``.

    Drive-safe: on Windows, paths on different drives cannot share a parent,
    so ``os.path.commonpath`` raises ``ValueError`` instead of returning a
    common prefix.  Two paths on different drives are never in a parent/child
    relation, so the function returns ``False`` rather than propagating the
    error.
    """
    if os.path.splitdrive(path)[0].upper() != os.path.splitdrive(root)[0].upper():
        return False
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


#: Blanks a filter driver for one read: no command runs, and none is required.
_FILTER_OFF = (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false"))
_LOCK_TIMEOUT_SECS = 300.0
_BUSY_ERRNOS = (errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK))
SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__", ".mypy_cache", LOCK_DIR})
#: Never handed to a command the agent runs.
WITHHELD_ENV = frozenset(
    {
        "KIRO_API_KEY",
        "KIRO_IDENTITY",
        "CODER_AGENT_TOKEN",
        "CODER_AGENT_TOKEN_FILE",
        "CODER_SESSION_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
)


#: Per proxied host, the remote spellings git rewrites to the proxy (each a
#: prefix git matches). Its hosts mirror ``git_gateway.HOSTS``, which this
#: guest-side file cannot import; ``test_remote_git_proxy`` pins the two equal.
#: Azure's SSH form (v3/ORG/PROJECT/REPO) has a different path shape, so only
#: its HTTPS spelling is rewritten.
GIT_PROXY_REWRITES: Dict[str, tuple] = {
    "github.com": ("https://github.com/", "git@github.com:", "ssh://git@github.com/"),
    "gitlab.com": ("https://gitlab.com/", "git@gitlab.com:", "ssh://git@gitlab.com/"),
    "bitbucket.org": ("https://bitbucket.org/", "git@bitbucket.org:", "ssh://git@bitbucket.org/"),
    "dev.azure.com": ("https://dev.azure.com/",),
}


#: Beside the port file, the gateway writes the number of a proxy start that
#: failed. Mirrors ``remote_worker.GIT_PROXY_FAILED_SUFFIX``.
GIT_PROXY_FAILED_SUFFIX = ".failed"


def _start_failed(port_file: str, starting: str) -> bool:
    """Whether the gateway marked proxy start ``starting`` as failed."""
    try:
        with open(port_file + GIT_PROXY_FAILED_SUFFIX, encoding="ascii") as handle:
            return handle.read(32).strip() == starting
    except (OSError, ValueError):
        return False


def _proxy_port(port_file: str) -> Optional[int]:
    """The port the proxy named in ``port_file``, when something listens there."""
    try:
        with open(port_file, encoding="ascii") as handle:
            port = int(handle.read().strip())
    except (OSError, ValueError):
        return None
    return port if 0 < port < 65536 and _listening(port) else None


def _listening(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def git_proxy_env(
    port_file: str,
    secret: str,
    base: Optional[Dict[str, str]] = None,
    *,
    startup_since: Optional[float] = None,
    starting: str = "",
) -> Dict[str, str]:
    """Environment that routes this command's git traffic for lendable hosts via the gateway.

    Only the hosts the gateway can lend a login for right now are routed: the
    proxy keeps their names, beside its port file, as the gateway last
    published them, and this re-reads that list for every command. Every other
    host keeps the workspace's own git login (its SSH key, its askpass helper),
    exactly as without Kiro Crew, and under ``strict`` the list is empty, so
    nothing is routed. A list that cannot be read, or that names another
    proxy's port, routes nothing: the fallback is the workspace's own login,
    never the gateway's.

    Git LFS also reads these aliases; the gateway returns its batch and lock
    verification requests to their direct HTTPS endpoints without credentials.
    Public LFS remains anonymous; private LFS needs workspace-side authentication.

    Uses git's environment config (``GIT_CONFIG_COUNT``, git 2.31+), so nothing
    is written to a git config file and a command run outside this tool is not
    affected. ``GIT_ASKPASS`` and the terminal prompt are left as the workspace
    set them; a credential helper scoped to the proxy's own URL answers
    ``quit``, so git asks no one for a password for a routed request, even when
    an anonymous fetch returns 401.
    """
    if not secret or not port_file:
        return {}
    deadline = _monotonic() + GIT_PROXY_FIRST_LEND_WAIT_SECS
    port = _proxy_port(port_file)
    if port is None and startup_since is not None and starting:
        # The gateway starts this session's proxy in the background, and the
        # proxy writes its port file only once it listens. When the gateway told
        # this tool server a start is in flight (``starting``, its number), a
        # command waits for that proxy, inside the same bound, so an early clone
        # still gets the login. The wait ends as soon as the port answers or the
        # gateway marks that start failed; with no start in flight, or past this
        # tool server's first moments, a missing proxy costs no wait at all.
        startup_deadline = min(deadline, startup_since + GIT_PROXY_FIRST_LEND_WAIT_SECS)
        while (
            port is None
            and _monotonic() < startup_deadline
            and not _start_failed(port_file, starting)
        ):
            _sleep(0.1)
            port = _proxy_port(port_file)
    if port is None:
        # No proxy, or a file left by one that died: rewriting to a dead port
        # would break even public clones, so the command runs as if no proxy existed.
        return {}
    lendable, complete = _lendable_hosts(port_file, port)
    # The gateway answers the proxy's first ``lend`` request once its credential
    # lookups finish, which can take a few seconds after the session starts. A
    # command run before then would reach a host the gateway can lend for with
    # no login, so it waits, bounded, for that answer. Until the lookups finish
    # the gateway publishes each login it finds, marked partial; the command
    # keeps waiting through those, since a host still being looked up would
    # otherwise be left out of this command's rewrite. A complete list, even an
    # empty one (``strict``, no login anywhere), is used at once; at the
    # deadline the command takes what has been found; a proxy that stops ends
    # the wait.
    while not complete and _monotonic() < deadline:
        _sleep(0.1)
        if not _listening(port):
            return {}
        lendable, complete = _lendable_hosts(port_file, port)
    if not lendable:
        return {}
    proxy = f"http://127.0.0.1:{port}/"
    pairs = [
        (f"url.{proxy}{host}/.insteadOf", prefix)
        for host, prefixes in GIT_PROXY_REWRITES.items()
        if host in lendable
        for prefix in prefixes
    ]
    # URL-scoped; Authorization is also stripped on cross-host LFS redirects.
    pairs.append((f"http.{proxy}.extraHeader", f"Authorization: Bearer {secret}"))
    # No prompt for the proxy's own URL only; other hosts keep the workspace's.
    pairs.append((f"credential.{proxy}.helper", "!f() { echo quit=1; }; f"))
    # Entries the command's environment (``base``) already sets come first and
    # are kept: these are appended after them, not written over them.
    try:
        start = max(0, int((base or {}).get("GIT_CONFIG_COUNT", "0")))
    except ValueError:
        start = 0
    env = {"GIT_CONFIG_COUNT": str(start + len(pairs))}
    for index, (key, value) in enumerate(pairs, start):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


#: Beside the port file, ``git_proxy.py`` keeps ``<port> <host>...``: the hosts
#: the gateway last said it can lend for. Mirrors ``git_proxy.lend_file``.
GIT_PROXY_LEND_SUFFIX = ".lend"

#: How long a command waits for the gateway's first list of lendable hosts.
#: Above one credential CLI's own time limit on the gateway (15 s), so a slow
#: ``az`` lookup still lands before the command; past it the command runs with
#: the workspace's own login, as it would without Kiro Crew.
GIT_PROXY_FIRST_LEND_WAIT_SECS = 20.0
#: The wait's clock, module-local so a test can drive it without real time.
_monotonic = time.monotonic
_sleep = time.sleep


def _lendable_hosts(port_file: str, port: int) -> "tuple[Optional[frozenset[str]], bool]":
    """The hosts to route, and whether the gateway has finished looking them up.

    The hosts are None while this proxy has no answer from the gateway yet:
    ``serve`` removes the file before it writes its port and asks, so a missing
    file, or one naming another proxy's port, means "not answered yet". A ``*``
    after the port marks a partial list, published while other hosts' lookups
    are still running. An unreadable file routes nothing and is final.
    """
    try:
        with open(port_file + GIT_PROXY_LEND_SUFFIX, encoding="ascii") as handle:
            fields = handle.read(4096).split()
    except FileNotFoundError:
        return None, False
    except (OSError, ValueError):
        return frozenset(), True
    if fields[:1] != [str(port)]:
        return None, False  # another proxy's list, or none yet
    complete = fields[1:2] != ["*"]
    return frozenset(fields[1:]) & frozenset(GIT_PROXY_REWRITES), complete


class ToolError(Exception):
    """A refusal or failure reported to the agent as a tool error, not a crash."""


def _glob_regex(pattern: str) -> "re.Pattern[str]":
    """Path-segment glob: ``*`` and ``?`` stay within one segment, ``**/`` spans zero or
    more segments, a trailing ``**`` anything; ``[...]`` is a character class."""
    out, i, n = [], 0, len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            out.append("(?:[^/]*/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[" and "]" in pattern[i + 2 :]:
            end = pattern.index("]", i + 2)
            body = pattern[i + 1 : end].replace("\\", "\\\\")
            out.append("[^" + body[1:] + "]" if body.startswith("!") else "[" + body + "]")
            i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def glob_matches(rel: str, pattern: str) -> bool:
    """Whether a ``/``-separated relative path matches; a pattern with no ``/`` matches
    the file name at any depth."""
    if "/" not in pattern:
        return fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], pattern)
    try:
        return _glob_regex(pattern).match(rel) is not None
    except re.error:
        return False


class Workspace:
    def __init__(
        self,
        root: str,
        git_proxy_port_file: str = "",
        *,
        lock_dir: Optional[str] = None,
        git_proxy_secret: str = "",
        git_proxy_starting: str = "",
    ) -> None:
        #: The port file of THIS session's git proxy (``git_proxy.py --port-file``);
        #: empty when this process runs no git (the browse and write modes).
        self.git_proxy_port_file = git_proxy_port_file
        self.git_proxy_secret = git_proxy_secret
        #: The number of the git proxy start the gateway had in flight when this
        #: tool server started, or "" when none was: only then does an early git
        #: command wait for that proxy (git_proxy_env).
        self.git_proxy_starting = git_proxy_starting
        #: When this tool server started; the gateway starts the git proxy beside
        #: it, so a command this early waits for the proxy too (git_proxy_env).
        self.started_at = _monotonic()
        real = os.path.realpath(root)
        if not os.path.isdir(real):
            raise SystemExit(f"workspace root {root!r} is not a directory")
        self.root = real
        self.lock_dir = lock_dir or os.path.join(real, LOCK_DIR)

    def resolve(self, path: Any, *, must_exist: bool = True) -> str:
        if not isinstance(path, str) or not path or "\x00" in path:
            raise ToolError("path must be a non-empty string")
        joined = path if os.path.isabs(path) else os.path.join(self.root, path)
        real = os.path.realpath(joined)
        prefix = self.root if self.root.endswith(os.sep) else self.root + os.sep
        if real != self.root and not real.startswith(prefix):
            raise ToolError(f"path {path!r} is outside the workspace {self.root}")
        if must_exist and not os.path.exists(real):
            raise ToolError(f"{path!r} does not exist")
        return real

    def rel(self, real: str) -> str:
        return os.path.relpath(real, self.root)

    def read_file(self, path: str, offset: int = 0, limit: int = 0) -> str:
        real = self.resolve(path)
        if os.path.isdir(real):
            raise ToolError(f"{path!r} is a directory; use list_directory")
        with open(real, "rb") as handle:
            if not (offset or limit):
                data = handle.read(MAX_READ_BYTES + 1)
                truncated = len(data) > MAX_READ_BYTES
                text = data[:MAX_READ_BYTES].decode("utf-8", "replace")
            else:
                text, truncated = self._read_lines(handle, max(offset, 0), limit)
        if truncated:
            text += f"\n[truncated at {MAX_READ_BYTES} bytes; use offset/limit]"
        return text

    @staticmethod
    def _read_lines(handle: Any, offset: int, limit: int) -> "tuple[str, bool]":
        """Lines ``offset`` to ``offset + limit`` of the whole file; the slice is capped."""
        out: List[bytes] = []
        size = index = 0
        while not limit or index < offset + limit:
            raw = handle.readline(MAX_READ_BYTES + 1)
            if not raw:
                return b"".join(out).decode("utf-8", "replace"), False
            if index >= offset:
                if size + len(raw) > MAX_READ_BYTES:
                    out.append(raw[: MAX_READ_BYTES - size])
                    return b"".join(out).decode("utf-8", "replace"), True
                out.append(raw)
                size += len(raw)
            # A line longer than one bounded read arrives in pieces; only its end
            # advances the line count.
            if raw.endswith(b"\n"):
                index += 1
        return b"".join(out).decode("utf-8", "replace"), False

    @contextlib.contextmanager
    def write_lock(self, path: str):
        """Serialize guest tool and dashboard writers by canonical path; never unlink
        the sidecar (a writer may wait on it). Shell commands do not take this lock.
        """
        real = self.resolve(path, must_exist=False)
        directory = self.lock_dir
        os.makedirs(directory, exist_ok=True)
        key = hashlib.sha256(os.fsencode(os.path.normcase(real))).hexdigest()
        with open(os.path.join(directory, key), "a+b") as handle:
            if sys.platform == "win32":
                import msvcrt

                # Byte-range locks cover empty files; writing a sentinel races
                # another holder's mandatory lock on the same byte.
                handle.seek(0)
                deadline = time.monotonic() + _LOCK_TIMEOUT_SECS
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if exc.errno not in _BUSY_ERRNOS:
                            raise
                    if time.monotonic() >= deadline:
                        raise ToolError("workspace file lock still held; refusing to write")
                    time.sleep(_LOCK_POLL_SECS)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield real
            finally:
                if sys.platform == "win32":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def write_file(self, path: str, content: str) -> str:
        with self.write_lock(path) as real:
            return self._write_file_locked(real, content)

    def _write_file_locked(self, path: str, content: str) -> str:
        import stat
        import tempfile

        if not isinstance(content, str):
            raise ToolError("content must be a string")
        data = content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise ToolError(f"content exceeds {MAX_WRITE_BYTES} bytes")
        real = self.resolve(path, must_exist=False)
        if _is_within(real, _SELF_DIR):
            raise ToolError(f"{path!r} is in the Kiro Crew worker directory")
        parent = os.path.dirname(real)
        # Re-check after creating parents: a symlinked parent could point out.
        os.makedirs(parent, exist_ok=True)
        self.resolve(parent)
        fd, tmp = tempfile.mkstemp(prefix=".kirocrew-", suffix=".tmp", dir=parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            try:
                mode = stat.S_IMODE(os.stat(real).st_mode)
            except FileNotFoundError:
                pass  # New files keep the staging file's owner-only permissions.
            else:
                os.chmod(tmp, mode)
            os.replace(tmp, real)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return f"wrote {len(data)} bytes to {self.rel(real)}"

    def edit_file(self, path: str, old_str: str, new_str: str, replace_all: bool = False) -> str:
        if not isinstance(replace_all, bool):
            raise ToolError("replace_all must be a boolean")
        with self.write_lock(path) as real:
            return self._edit_file_locked(real, old_str, new_str, replace_all)

    def _edit_file_locked(self, path: str, old_str: str, new_str: str, replace_all: bool) -> str:
        real = self.resolve(path)
        with open(real, "r", encoding="utf-8", newline="") as handle:
            text = handle.read()
        if not old_str:
            raise ToolError("old_str must not be empty")
        count = text.count(old_str)
        if count == 0:
            raise ToolError("old_str was not found")
        if count > 1 and not replace_all:
            raise ToolError(f"old_str occurs {count} times; pass replace_all or add context")
        updated = (
            text.replace(old_str, new_str) if replace_all else text.replace(old_str, new_str, 1)
        )
        self._write_file_locked(path, updated)
        return f"replaced {count if replace_all else 1} occurrence(s) in {self.rel(real)}"

    def list_directory(self, path: str) -> str:
        real = self.resolve(path)
        if not os.path.isdir(real):
            raise ToolError(f"{path!r} is not a directory")
        entries = sorted(name for name in os.listdir(real) if name != LOCK_DIR)
        lines = []
        for name in entries[:MAX_LIST_ENTRIES]:
            full = os.path.join(real, name)
            lines.append(name + ("/" if os.path.isdir(full) else ""))
        if len(entries) > MAX_LIST_ENTRIES:
            lines.append(f"[{len(entries) - MAX_LIST_ENTRIES} more entries not shown]")
        return "\n".join(lines) or "(empty)"

    def _walk(self, base: str):
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                yield os.path.join(dirpath, name)

    def glob(self, pattern: str, path: str) -> str:
        base = self.resolve(path)
        found = []
        for full in self._walk(base):
            rel = os.path.relpath(full, base).replace(os.sep, "/")
            if glob_matches(rel, pattern):
                found.append(self.rel(full))
                if len(found) >= MAX_LIST_ENTRIES:
                    found.append("[more matches not shown]")
                    break
        return "\n".join(found) or "(no matches)"

    def grep(self, pattern: str, path: str, include: str = "") -> str:
        try:
            regex = re.compile(pattern)
        except (re.error, OverflowError, RecursionError) as exc:
            raise ToolError(f"invalid regex: {exc}") from exc
        base = self.resolve(path)
        files = [base] if os.path.isfile(base) else self._walk(base)
        hits: List[str] = []
        for full in files:
            if include and not fnmatch.fnmatch(os.path.basename(full), include):
                continue
            try:
                real = self.resolve(full)
                with open(real, "r", encoding="utf-8", errors="strict") as handle:
                    for number, line in enumerate(handle, 1):
                        if regex.search(line):
                            hits.append(f"{self.rel(full)}:{number}: {line.rstrip()[:300]}")
                            if len(hits) >= MAX_MATCHES:
                                return "\n".join(hits) + "\n[match limit reached]"
            except (ToolError, UnicodeDecodeError, OSError):
                continue  # outside the root, binary or unreadable
        return "\n".join(hits) or "(no matches)"

    def run_command(self, command: str, timeout: int = DEFAULT_TIMEOUT, cwd: str = ".") -> str:
        if not isinstance(command, str) or not command.strip():
            raise ToolError("command must be a non-empty string")
        timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
        workdir = self.resolve(cwd)
        env = {k: v for k, v in os.environ.items() if k not in WITHHELD_ENV}
        # GitHub git traffic goes through the gateway, which holds the credential.
        env.update(
            git_proxy_env(
                self.git_proxy_port_file,
                self.git_proxy_secret,
                env,
                startup_since=self.started_at,
                starting=self.git_proxy_starting,
            )
        )
        argv = ["/bin/sh", "-c", command]
        timer = shutil.which("timeout")
        if timer:
            # coreutils timeout signals the command's whole process group itself,
            # on expiry and when it is terminated, then KILLs after the grace.
            argv = [timer, "-k", str(_KILL_GRACE_SECS), str(timeout), *argv]
        started = time.monotonic()
        proc = subprocess.Popen(
            argv,
            cwd=workdir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        # Drained into a bounded tail, so endless output costs at most MAX_OUTPUT_BYTES.
        tail = _OutputTail(MAX_OUTPUT_BYTES)
        reader = threading.Thread(target=tail.drain, args=(proc.stdout,), daemon=True)
        reader.start()
        code: Optional[int] = None
        try:
            code = proc.wait(timeout=timeout + (2 * _KILL_GRACE_SECS if timer else 0))
        except subprocess.TimeoutExpired:
            code = None
        finally:
            if proc.returncode is None:
                # Under timeout this reaches the whole group; without it, the shell only.
                proc.terminate()
                try:
                    proc.wait(timeout=2 * _KILL_GRACE_SECS)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
            # A detached job holding the pipe keeps only the daemon reader waiting.
            reader.join(_READER_GRACE_SECS)
        output, omitted = tail.snapshot()
        text = output.decode("utf-8", "replace")
        if omitted:
            # Keep the tail: errors and summaries come last.
            text = f"[{omitted} earlier bytes omitted]\n" + text
        expired = time.monotonic() - started >= timeout and code in (124, 137)
        status = f"timed out after {timeout}s" if code is None or expired else f"exit {code}"
        return f"{text}\n[{status}]"


#: Seconds a timed-out command gets to exit on SIGTERM before SIGKILL.
_KILL_GRACE_SECS = 5
#: How long a finished command's remaining output may take to arrive.
_READER_GRACE_SECS = 2.0


class _OutputTail:
    """The last ``limit`` bytes a stream produced, and how many came before them."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._buf = bytearray()
        self._omitted = 0
        self._lock = threading.Lock()

    def drain(self, stream: Any) -> None:
        read = getattr(stream, "read1", None) or stream.read
        try:
            while True:
                chunk = read(65536)
                if not chunk:
                    return
                with self._lock:
                    self._buf += chunk
                    excess = len(self._buf) - self._limit
                    if excess > 0:
                        del self._buf[:excess]
                        self._omitted += excess
        except (OSError, ValueError):
            return
        finally:
            with contextlib.suppress(OSError, ValueError):
                stream.close()

    def snapshot(self) -> "tuple[bytes, int]":
        with self._lock:
            return bytes(self._buf), self._omitted


def _schema(properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


_S = {"type": "string"}
_I = {"type": "integer"}


def tool_specs(root: str) -> List[Dict[str, Any]]:
    # Every file tool takes a path, so governance always has a target to judge.
    where = f"in the remote workspace ({root}). Relative paths are relative to it; use . for it."
    return [
        {
            "name": "read_file",
            "description": f"Read a text file {where}",
            "inputSchema": _schema({"path": _S, "offset": _I, "limit": _I}, ["path"]),
        },
        {
            "name": "write_file",
            "description": f"Create or overwrite a file {where}",
            "inputSchema": _schema({"path": _S, "content": _S}, ["path", "content"]),
        },
        {
            "name": "edit_file",
            "description": f"Replace an exact string in a file {where} old_str must be unique unless replace_all.",
            "inputSchema": _schema(
                {"path": _S, "old_str": _S, "new_str": _S, "replace_all": {"type": "boolean"}},
                ["path", "old_str", "new_str"],
            ),
        },
        {
            "name": "list_directory",
            "description": f"List a directory {where}",
            "inputSchema": _schema({"path": _S}, ["path"]),
        },
        {
            "name": "glob",
            "description": f"Find files by glob pattern {where}",
            "inputSchema": _schema({"pattern": _S, "path": _S}, ["pattern", "path"]),
        },
        {
            "name": "grep",
            "description": f"Search file contents with a regex {where}",
            "inputSchema": _schema({"pattern": _S, "path": _S, "include": _S}, ["pattern", "path"]),
        },
        {
            "name": "run_command",
            "description": f"Run a shell command {where} Returns combined output and exit status.",
            "inputSchema": _schema({"command": _S, "timeout": _I, "cwd": _S}, ["command"]),
        },
    ]


class Server:
    def __init__(self, workspace: Workspace) -> None:
        self.ws = workspace
        self.tools: Dict[str, Callable[..., str]] = {
            spec["name"]: getattr(workspace, spec["name"]) for spec in tool_specs(workspace.root)
        }

    def handle(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method, ident = msg.get("method"), msg.get("id")
        if ident is None:
            return None  # notification
        try:
            result = self._dispatch(method, msg.get("params") or {})
            return {"jsonrpc": "2.0", "id": ident, "result": result}
        except _RpcError as exc:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": exc.code, "message": str(exc)}}

    def _dispatch(self, method: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        if method == "initialize":
            return {
                "protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": tool_specs(self.ws.root)}
        if method == "tools/call":
            name = params.get("name")
            fn = self.tools.get(name) if isinstance(name, str) else None
            if fn is None:
                raise _RpcError(-32602, f"unknown tool {name!r}")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise _RpcError(-32602, "arguments must be an object")
            try:
                text, is_error = fn(**arguments), False
            except ToolError as exc:
                text, is_error = str(exc), True
            except (TypeError, ValueError) as exc:
                text, is_error = f"invalid arguments: {exc}", True
            except OSError as exc:
                text, is_error = f"{type(exc).__name__}: {exc}", True
            except Exception as exc:  # noqa: BLE001 -- one bad call must not end the server
                text, is_error = f"{name} failed: {type(exc).__name__}", True
            return {"content": [{"type": "text", "text": text}], "isError": is_error}
        raise _RpcError(-32601, f"method {method!r} not found")


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def serve(server: Server, inp=None, out=None) -> None:
    inp = inp or sys.stdin.buffer
    out = out or sys.stdout.buffer
    for line in inp:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        reply = server.handle(msg)
        if reply is not None:
            out.write(json.dumps(reply).encode("utf-8") + b"\n")
            out.flush()


def browse(ws: Workspace, request: Any) -> Dict[str, Any]:
    """One READ-ONLY dashboard request: ``list`` or ``read``, nothing else."""
    if not isinstance(request, dict):
        return {"error": "request must be an object"}
    op, path = request.get("op"), request.get("path") or "."
    try:
        if op == "list":
            real = ws.resolve(path)
            if not os.path.isdir(real):
                raise ToolError(f"{path!r} is not a directory")
            names = sorted(name for name in os.listdir(real) if name != LOCK_DIR)
            entries = [
                {"name": n, "dir": os.path.isdir(os.path.join(real, n))}
                for n in names[:MAX_LIST_ENTRIES]
            ]
            return {
                "root": ws.root,
                "path": ws.rel(real),
                "entries": entries,
                "truncated": len(names) > MAX_LIST_ENTRIES,
            }
        if op == "read":
            real = ws.resolve(path)
            if os.path.isdir(real):
                raise ToolError(f"{path!r} is a directory")
            with open(real, "rb") as handle:
                data = handle.read(MAX_READ_BYTES + 1)
            try:
                # At the cap, a character it splits is dropped rather than an error.
                decoder = codecs.getincrementaldecoder("utf-8")()
                text = decoder.decode(data[:MAX_READ_BYTES], final=len(data) <= MAX_READ_BYTES)
                undecodable = False
            except UnicodeDecodeError:
                text, undecodable = "", True
            return {
                "root": ws.root,
                "path": ws.rel(real),
                "content": text,
                "binary": undecodable or b"\x00" in data[:8192],
                "truncated": len(data) > MAX_READ_BYTES,
            }
        if op == "git_status":
            return _git_status(ws)
        if op == "stats":
            return _stats(ws)
    except ToolError as exc:
        return {"error": str(exc)}
    except OSError as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"error": "op must be list, read, git_status or stats"}


def _meminfo() -> Dict[str, int]:
    out: Dict[str, int] = {}
    try:
        with open("/proc/meminfo", "rb") as handle:
            sample = handle.read(MAX_READ_BYTES + 1)
        # Omit an oversized sample rather than report a partially read metric.
        if len(sample) > MAX_READ_BYTES:
            return out
        for line in sample.decode("ascii").splitlines():
            key, _, rest = line.partition(":")
            fields = rest.split()
            if key in ("MemTotal", "MemAvailable") and fields and fields[0].isdigit():
                out[key] = int(fields[0]) * 1024
    except (OSError, UnicodeDecodeError):
        pass
    return out


def _cgroup_number(path: str) -> Optional[int]:
    """A cgroup file's single integer, or None (absent, ``max`` or unreadable)."""
    try:
        with open(path, "rb") as handle:
            text = handle.read(64).decode("ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return int(text) if text.isdigit() else None


def _cgroup_inactive_file(stat_path: str, key: str) -> int:
    """Reclaimable file cache from a cgroup ``memory.stat``, or 0 when unreadable."""
    try:
        with open(stat_path, "rb") as handle:
            sample = handle.read(MAX_READ_BYTES + 1)
        if len(sample) > MAX_READ_BYTES:
            return 0
        for line in sample.decode("ascii").splitlines():
            name, _, value = line.partition(" ")
            if name == key and value.strip().isdigit():
                return int(value.strip())
    except (OSError, UnicodeDecodeError):
        pass
    return 0


def _cgroup_memory() -> Optional[Tuple[int, int]]:
    """``(limit, working set)`` in bytes when the guest runs under a cgroup memory limit.

    A container workspace (Coder Docker or Kubernetes templates) sees the host
    node in ``/proc/meminfo``; its real ceiling is the cgroup's. Usage counts
    page cache, so reclaimable ``inactive_file`` is subtracted, the working set
    docker and kubectl report. v2 first, then v1.
    """
    for limit_path, usage_path, stat_path, key in (
        (
            "/sys/fs/cgroup/memory.max",
            "/sys/fs/cgroup/memory.current",
            "/sys/fs/cgroup/memory.stat",
            "inactive_file",
        ),
        (
            "/sys/fs/cgroup/memory/memory.limit_in_bytes",
            "/sys/fs/cgroup/memory/memory.usage_in_bytes",
            "/sys/fs/cgroup/memory/memory.stat",
            "total_inactive_file",
        ),
    ):
        limit, usage = _cgroup_number(limit_path), _cgroup_number(usage_path)
        if limit is not None and usage is not None:
            return limit, max(0, usage - _cgroup_inactive_file(stat_path, key))
    return None


def _cgroup_cpus() -> Optional[int]:
    """CPUs the cgroup's v2 ``cpu.max`` quota allows, or None without a quota."""
    try:
        with open("/sys/fs/cgroup/cpu.max", "rb") as handle:
            fields = handle.read(64).decode("ascii").split()
    except (OSError, UnicodeDecodeError):
        return None
    if len(fields) != 2 or not fields[0].isdigit() or not fields[1].isdigit():
        return None
    quota, period = int(fields[0]), int(fields[1])
    return max(1, -(-quota // period)) if period else None


def _stats(ws: Workspace) -> Dict[str, Any]:
    """Read-only machine metrics for the dashboard's environment card.

    Only numbers and the architecture: no environment, no process list, no
    paths, and only what the card shows.
    """
    import platform
    import shutil

    cpus = os.cpu_count() or 0
    quota_cpus = _cgroup_cpus()
    # Inside a container, load average and uptime are the host node's, not the
    # workspace's: shown beside the container's own core count they mislead, so
    # a limited container reports neither.
    contained = quota_cpus is not None and (cpus == 0 or quota_cpus < cpus)
    if quota_cpus is not None and contained:
        cpus = quota_cpus
    result: Dict[str, Any] = {"arch": platform.machine(), "cpus": cpus}
    mem = _meminfo()
    if "MemTotal" in mem and "MemAvailable" in mem:
        total, available = mem["MemTotal"], mem["MemAvailable"]
        cgroup = _cgroup_memory()
        if cgroup is not None and cgroup[0] < total:
            # The container's ceiling, not the host node's.
            contained = True
            total = cgroup[0]
            available = max(0, min(available, cgroup[0] - cgroup[1]))
        result["memory"] = {"total_bytes": total, "available_bytes": available}
    if not contained:
        try:
            result["load"] = [round(v, 2) for v in os.getloadavg()]
        except (OSError, AttributeError):
            pass
    try:
        disk = shutil.disk_usage(ws.root)
        result["disk"] = {"total_bytes": disk.total, "free_bytes": disk.free}
    except OSError:
        pass
    if not contained:
        try:
            with open("/proc/uptime", "r") as handle:
                result["uptime_s"] = int(float(handle.read().split()[0]))
        except (OSError, ValueError, IndexError):
            pass
    return result


def _git_status(ws: Workspace) -> Dict[str, Any]:
    """Read-only ``git status``: fixed argv, no shell, no hooks, and every configured
    filter driver blanked (status would run one without command authorization).
    """
    # No hooks, and no index refresh (which would run post-index-change).
    base = [
        "git",
        "-c",
        "core.fsmonitor=",
        "-c",
        "core.pager=cat",
        "-c",
        "core.hooksPath=/dev/null",
    ]
    base += ["-C", ws.root]
    env = {k: v for k, v in os.environ.items() if k not in WITHHELD_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    pipe, quiet = subprocess.PIPE, subprocess.DEVNULL
    try:
        # Reading config runs nothing; exit 1 just means no filter is configured.
        listed = subprocess.run(
            base + ["config", "--null", "--name-only", "--get-regexp", r"^filter\."],
            env=env,
            stdin=quiet,
            stdout=pipe,
            stderr=pipe,
            timeout=30,
        )
    except FileNotFoundError:
        return {"root": ws.root, "git": False, "error_text": "git is not installed"}
    except subprocess.TimeoutExpired:
        raise ToolError("git did not answer in 30 seconds")
    drivers = {
        name.decode("utf-8", "replace")[len("filter.") :].rpartition(".")[0]
        for name in listed.stdout.split(b"\0")
        if name.startswith(b"filter.")
    }
    if listed.returncode not in (0, 1) or any("=" in d or not d for d in drivers):
        # A driver this cannot name in ``-c`` cannot be switched off: run nothing.
        return {"root": ws.root, "git": False, "error_text": "git filter config not checked"}
    off = [f"filter.{d}.{key}={value}" for d in sorted(drivers) for key, value in _FILTER_OFF]
    argv = base + [arg for item in off for arg in ("-c", item)]
    # A submodule's own config could name filters too: never look inside one.
    argv += ["status", "--porcelain=v1", "--branch", "--untracked-files=normal"]
    argv.append("--ignore-submodules=dirty")
    try:
        proc = subprocess.run(argv, env=env, stdin=quiet, stdout=pipe, stderr=pipe, timeout=30)
    except FileNotFoundError:
        return {"root": ws.root, "git": False, "error_text": "git is not installed"}
    except subprocess.TimeoutExpired:
        raise ToolError("git did not answer in 30 seconds")
    out = proc.stdout[:MAX_READ_BYTES].decode("utf-8", "replace")
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        # Not a repository (or no commits yet) is a state, not a failure.
        return {"root": ws.root, "git": False, "error_text": err[-300:]}
    return {
        "root": ws.root,
        "git": True,
        "output": out,
        "truncated": len(proc.stdout) > MAX_READ_BYTES,
    }


def write_request(ws: Workspace, request: Any) -> Dict[str, Any]:
    """The browser's one mutation: save text, refused when ``expected`` differs."""
    if not isinstance(request, dict):
        return {"error": "request must be an object"}
    path, content = request.get("path"), request.get("content")
    if not isinstance(content, str):
        return {"error": "content must be a string"}
    try:
        expected = request.get("expected")
        if not isinstance(path, str):
            raise ToolError("path must be a non-empty string")
        with ws.write_lock(path) as real:
            if os.path.exists(real):
                with open(real, "rb") as handle:
                    current = handle.read(MAX_WRITE_BYTES + 1).decode("utf-8")
                if isinstance(expected, str) and current != expected:
                    return {"error": "the file changed since it was opened", "conflict": True}
            elif isinstance(expected, str):
                return {"error": "the file changed since it was opened", "conflict": True}
            message = ws._write_file_locked(real, content)
        return {"root": ws.root, "path": ws.rel(ws.resolve(path)), "ok": True, "message": message}
    except ToolError as exc:
        return {"error": str(exc)}
    except (OSError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="workspace_tools.py")
    p.add_argument("--root", required=True)
    p.add_argument("--git-proxy-port-file", default="")
    p.add_argument("--git-proxy-secret-stdin", action="store_true")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--browse",
        action="store_true",
        help="answer one read-only JSON request from stdin and exit",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="save one file from a JSON request on stdin and exit",
    )
    args = p.parse_args(argv)
    secret = starting = ""
    if args.git_proxy_secret_stdin:
        # "<capability> <state>": the state says whether the gateway has a proxy
        # start in flight ("starting:<n>") or none ("none").
        line = sys.stdin.buffer.readline(128).decode("ascii").split()
        secret = line[0] if line else ""
        if re.fullmatch(r"[0-9a-f]{64}", secret) is None:
            raise SystemExit("invalid git proxy capability")
        state = line[1] if len(line) > 1 else ""
        if re.fullmatch(r"starting:[0-9]{1,9}", state):
            starting = state.split(":", 1)[1]
    os.makedirs(os.path.expanduser(args.root), exist_ok=True)
    # The port file is resolved here: a relative path names the worker directory
    # this starts in.
    ws = Workspace(
        os.path.expanduser(args.root),
        os.path.abspath(args.git_proxy_port_file) if args.git_proxy_port_file else "",
        lock_dir=os.path.join(os.path.expanduser("~"), ".kirocrew-worker", "file-locks"),
        git_proxy_secret=secret,
        git_proxy_starting=starting,
    )
    if args.browse or args.write:
        limit = (2 * MAX_WRITE_BYTES) if args.write else 65536
        try:
            request = json.loads(sys.stdin.buffer.read(limit) or b"null")
        except ValueError:
            request = None
        result = write_request(ws, request) if args.write else browse(ws, request)
        sys.stdout.write(json.dumps(result))
        sys.stdout.flush()
        return 0
    # Worker retirement must unwind an active command's process-group cleanup, and a
    # second SIGTERM (the worker sends one on stop and again on exit) must not
    # interrupt that cleanup: the first one starts retirement, later ones are ignored.

    def retire(_signum, _frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise SystemExit(0)

    previous = signal.signal(signal.SIGTERM, retire)
    try:
        serve(Server(ws))
    finally:
        signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == "__main__":
    sys.exit(main())
