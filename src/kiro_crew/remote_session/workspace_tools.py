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
from typing import Any, Callable, Dict, List, Optional

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
    def __init__(self, root: str, *, lock_dir: Optional[str] = None) -> None:
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
    except ToolError as exc:
        return {"error": str(exc)}
    except OSError as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"error": "op must be list, read or git_status"}


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
    os.makedirs(os.path.expanduser(args.root), exist_ok=True)
    ws = Workspace(
        os.path.expanduser(args.root),
        lock_dir=os.path.join(os.path.expanduser("~"), ".kirocrew-worker", "file-locks"),
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
