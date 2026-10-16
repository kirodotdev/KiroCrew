"""Guest-side MCP tool server for a remote session workspace.

The agent's engine runs on the gateway; this server is the only way it touches
the remote workspace. It speaks MCP over newline-delimited JSON-RPC on stdio
and is started by ``worker.py`` inside the guest, so the gateway reaches it
through the authenticated, integrity-checked ``wire`` stream.

Stdlib-only and Python 3.9 compatible: guest images ship older interpreters.

Every file path is resolved (symlinks included) and must stay inside ``--root``;
a path that escapes is refused, not clamped. Results are capped so one large
file or noisy command cannot flood a slow tunnel. ``run_command`` is an
unrestricted shell in the root, by design: the workspace is disposable and every
call still goes through Kiro Crew's normal approval flow on the gateway.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
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


class Workspace:
    def __init__(self, root: str, *, lock_dir: Optional[str] = None) -> None:
        real = os.path.realpath(root)
        if not os.path.isdir(real):
            raise SystemExit(f"workspace root {root!r} is not a directory")
        self.root = real
        self.lock_dir = lock_dir or os.path.join(real, LOCK_DIR)

    # -- paths -----------------------------------------------------------------
    def resolve(self, path: Any, *, must_exist: bool = True) -> str:
        if not isinstance(path, str) or not path or "\x00" in path:
            raise ToolError("path must be a non-empty string")
        joined = path if os.path.isabs(path) else os.path.join(self.root, path)
        real = os.path.realpath(joined)
        if real != self.root and not real.startswith(self.root + os.sep):
            raise ToolError(f"path {path!r} is outside the workspace {self.root}")
        if must_exist and not os.path.exists(real):
            raise ToolError(f"{path!r} does not exist")
        return real

    def rel(self, real: str) -> str:
        return os.path.relpath(real, self.root)

    # -- tools -----------------------------------------------------------------
    def read_file(self, path: str, offset: int = 0, limit: int = 0) -> str:
        real = self.resolve(path)
        if os.path.isdir(real):
            raise ToolError(f"{path!r} is a directory; use list_directory")
        with open(real, "rb") as handle:
            data = handle.read(MAX_READ_BYTES + 1)
        truncated = len(data) > MAX_READ_BYTES
        text = data[:MAX_READ_BYTES].decode("utf-8", "replace")
        if offset or limit:
            lines = text.splitlines(keepends=True)
            end = offset + limit if limit else len(lines)
            text = "".join(lines[max(offset, 0) : end])
        if truncated:
            text += f"\n[truncated at {MAX_READ_BYTES} bytes; use offset/limit]"
        return text

    @contextlib.contextmanager
    def write_lock(self, path: str):
        """Serialize guest tool and dashboard writers by canonical path.

        The sidecar's inode stays stable across file replacements and process
        exits. Never unlink it: another writer may already be waiting on it.
        Shell commands and programs outside these tools do not take this lock.
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

    def list_directory(self, path: str = ".") -> str:
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

    def glob(self, pattern: str, path: str = ".") -> str:
        base = self.resolve(path)
        found = []
        for full in self._walk(base):
            rel = os.path.relpath(full, base)
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(os.path.basename(full), pattern):
                found.append(self.rel(full))
                if len(found) >= MAX_LIST_ENTRIES:
                    found.append("[more matches not shown]")
                    break
        return "\n".join(found) or "(no matches)"

    def grep(self, pattern: str, path: str = ".", include: str = "") -> str:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
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
        try:
            proc = subprocess.run(
                ["/bin/sh", "-c", command],
                cwd=workdir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                start_new_session=True,
            )
            output, code = proc.stdout, proc.returncode
        except subprocess.TimeoutExpired as exc:
            output, code = exc.stdout or b"", None
        text = output.decode("utf-8", "replace")
        if len(text) > MAX_OUTPUT_BYTES:
            # Keep the tail: errors and summaries come last.
            text = (
                f"[{len(text) - MAX_OUTPUT_BYTES} earlier bytes omitted]\n"
                + text[-MAX_OUTPUT_BYTES:]
            )
        status = f"timed out after {timeout}s" if code is None else f"exit {code}"
        return f"{text}\n[{status}]"


def _schema(properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


_S = {"type": "string"}
_I = {"type": "integer"}


def tool_specs(root: str) -> List[Dict[str, Any]]:
    where = f"in the remote workspace ({root}). Relative paths are relative to it."
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
            "inputSchema": _schema({"path": _S}, []),
        },
        {
            "name": "glob",
            "description": f"Find files by glob pattern {where}",
            "inputSchema": _schema({"pattern": _S, "path": _S}, ["pattern"]),
        },
        {
            "name": "grep",
            "description": f"Search file contents with a regex {where}",
            "inputSchema": _schema({"pattern": _S, "path": _S, "include": _S}, ["pattern"]),
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
    """One READ-ONLY request for the dashboard's file browser.

    Only ``list`` and ``read``: the browser is a view, so this entry point can
    never write, edit or run anything, whatever the request says. Same root
    confinement and caps as the agent's tools.
    """
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
                text = data[:MAX_READ_BYTES].decode("utf-8")
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
    """Read-only ``git status`` of the workspace root: fixed argv, no shell.

    ``-c core.fsmonitor= -c core.pager=cat`` and no hooks keep the repository's
    config from naming a program for this read to run. Not every such program is
    closed off: a ``filter.<name>.clean`` driver the repository's attributes
    select still runs when status reads a modified file, and driver names are
    open-ended, so no fixed argv can blank them all. That runs as the guest user,
    who can already run any command in the guest through ``run_command``.
    """
    argv = [
        "git",
        "-c",
        "core.fsmonitor=",
        "-c",
        "core.pager=cat",
        "-C",
        ws.root,
        "status",
        "--porcelain=v1",
        "--branch",
        "--untracked-files=normal",
    ]
    env = {k: v for k, v in os.environ.items() if k not in WITHHELD_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
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
    """The browser's ONE mutation: save a file's text, under the same root rule.

    A separate entry point from :func:`browse` so the read path can never write,
    and refusing a write whose ``expected`` text differs from the file's, so
    saving a stale buffer cannot silently overwrite the agent's newer edit.
    """
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
    serve(Server(ws))
    return 0


if __name__ == "__main__":
    sys.exit(main())
