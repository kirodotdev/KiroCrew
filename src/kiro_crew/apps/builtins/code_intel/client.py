"""One running language server: a sandboxed child speaking JSON-RPC over stdio.

Every method here blocks. The manager calls them on the code-intel worker pool,
never on the gateway's event loop.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from kiro_crew.apps.builtins.code_intel.protocol import (
    FramingError,
    encode_message,
    path_to_uri,
    read_message,
)

logger = logging.getLogger(__name__)

# JSON-RPC "method not found", the answer to a server request we do not implement.
_METHOD_NOT_FOUND = -32601
# LSP ContentModified / ServerCancelled: the server dropped the request because
# its state moved (typically while indexing); asking again is the expected reply.
RETRYABLE_RPC_CODES = frozenset({-32801, -32802})
# Seconds to wait for ``shutdown`` before ``exit`` and then a kill.
_SHUTDOWN_GRACE_SECS = 3.0
# Bytes of a server's stderr kept for the "server exited" diagnostic.
_STDERR_TAIL_BYTES = 4096
# How long a failure report waits for the stderr drain to reach EOF.
_STDERR_JOIN_SECS = 1.0
# Problems a server reported (see `LspClient._note`), kept for the LSP tab and
# an empty answer's notice.
# Documents kept open on one server: each holds its full text here, and the
# server holds its own copy; a long session that opens many files closes the
# ones it opened first.
MAX_OPEN_DOCUMENTS = 200
_MAX_NOTICES = 5
_NOTICE_CHARS = 500
# jdtls stamps each log line "Oct 27, 2026, 5:33:05 PM"; the reason follows it.
_LOG_TIMESTAMP = re.compile(r"^[A-Z][a-z]{2} \d{1,2}, \d{4}, \d{1,2}:\d{2}:\d{2}\W*[AP]M\s+")
# LSP MessageType: 1 Error, 2 Warning.
_NOTICE_TYPES = {1: "error", 2: "warning"}

# What the gateway tells a server it can do: only the requests the dashboard
# sends, plus the client features servers commonly probe for.
_CLIENT_CAPABILITIES: dict = {
    "general": {"positionEncodings": ["utf-16"]},
    "workspace": {"configuration": True, "workspaceFolders": True},
    "window": {"workDoneProgress": True},
    "textDocument": {
        "synchronization": {"didSave": False, "dynamicRegistration": False},
        "definition": {"linkSupport": True},
        "references": {},
        "hover": {"contentFormat": ["markdown", "plaintext"]},
        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
    },
}


class LspError(Exception):
    """A request failed: the server answered an error, timed out, or is gone."""

    def __init__(self, message: str, *, code: str = "server_error", rpc_code: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.rpc_code = rpc_code


class LspClient:
    """A language server process bound to one project root."""

    def __init__(
        self,
        server_id: str,
        root: str,
        argv: list[str],
        init_options: dict | None = None,
        folders: list[str] | None = None,
    ) -> None:
        self.server_id = server_id
        self.root = root
        # Every folder the server opens: the root first, then the projects it
        # references, which a jdtls resolves only when they are open too.
        self.folders = list(folders) if folders else [root]
        self.argv = argv
        self.init_options = init_options or {}
        self._notices: deque[dict] = deque(maxlen=_MAX_NOTICES)
        # Set by the pool: why this server may not see the project ('' when none).
        self.setup_hint = ""
        # The last error or warning from the server's own log, for a load failure.
        self._last_log_problem = ""
        self.started_at = 0.0
        # When this client was created: a server still starting reports its age.
        self.spawned_at = time.monotonic()
        self.last_used = time.monotonic()
        self._proc: subprocess.Popen[bytes] | None = None
        self._cleanup_path: str | None = None
        self._write_lock = threading.Lock()
        self._pending: dict[int, Future] = {}
        self._pending_lock = threading.Lock()
        self._next_id = 0
        self._ready = threading.Event()
        self._start_error: str | None = None
        self._stderr_tail = bytearray()
        self._stderr_thread: threading.Thread | None = None
        # Set once this side starts taking the server down, so its exit is not
        # reported as a crash. Guarded by _state_lock together with _proc: a
        # shutdown that arrives while the start thread is still spawning must
        # still reach the child.
        self._closing = False
        self._state_lock = threading.Lock()
        # uri -> (version, text) of every document open on this server, oldest
        # first; past MAX_OPEN_DOCUMENTS the oldest is closed on the server too.
        self._documents: OrderedDict[str, tuple[int, str]] = OrderedDict()
        self._documents_lock = threading.Lock()
        self.capabilities: dict = {}

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self, init_timeout: float) -> None:
        """Spawn the server and run the ``initialize`` handshake.

        Sets the ready flag on success and records the failure otherwise; never
        raises, so a background start cannot lose its error.
        """
        try:
            self._spawn()
            result = self.request(
                "initialize",
                {
                    "processId": os.getpid(),
                    "clientInfo": {"name": "kirocrew-dashboard"},
                    "rootUri": path_to_uri(self.root),
                    "rootPath": self.root,
                    "workspaceFolders": self._workspace_folders(),
                    "capabilities": _CLIENT_CAPABILITIES,
                    **({"initializationOptions": self.init_options} if self.init_options else {}),
                },
                timeout=init_timeout,
                require_ready=False,
            )
            caps = result.get("capabilities") if isinstance(result, dict) else None
            self.capabilities = caps if isinstance(caps, dict) else {}
            self.notify("initialized", {})
            self.started_at = time.monotonic()
            self._ready.set()
        except Exception as exc:  # recorded for every waiter, then the child is reaped
            self._start_error = str(exc) or type(exc).__name__
            self.kill()
            logger.warning(
                "code_intel: %s server for %s failed to start: %s%s",
                self.server_id,
                self.root,
                self._start_error,
                self._stderr_suffix(),
            )
            self._ready.set()

    def document_text(self, path: str) -> str | None:
        """The text this server last received for *path*, or None if never opened."""
        with self._documents_lock:
            known = self._documents.get(path_to_uri(path))
        return known[1] if known else None

    def _workspace_folders(self) -> list[dict]:
        return [{"uri": path_to_uri(f), "name": os.path.basename(f)} for f in self.folders]

    def _spawn(self) -> None:
        from kiro_crew.sandbox import cgroup_scope_argv, popen_limited, wrap_argv
        from kiro_crew.sandbox_rlimit import RLIMIT_PROFILE_BUILD

        # Strict mode: the server reads the project and its toolchain, never the
        # credential homes. BUILD limits: indexers hold many files open at once.
        with self._state_lock:
            if self._closing:
                raise LspError(
                    "language server was stopped before it started", code="server_failed"
                )
        wrapped, cleanup_path = wrap_argv(list(self.argv), mode="strict")
        proc = popen_limited(
            cgroup_scope_argv(wrapped),
            profile=RLIMIT_PROFILE_BUILD,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.root,
        )
        with self._state_lock:
            self._proc, self._cleanup_path = proc, cleanup_path
            closing = self._closing
        if closing:
            # stop()/evict ran between the check above and the spawn: it found no
            # process to stop, so this side takes the child down now.
            self.kill()
            raise LspError("language server was stopped before it started", code="server_failed")
        threading.Thread(target=self._read_loop, name=f"lsp-{self.server_id}", daemon=True).start()
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, name=f"lsp-{self.server_id}-err", daemon=True
        )
        self._stderr_thread.start()

    def wait_ready(self, timeout: float) -> bool:
        """Whether the handshake finished within *timeout*; raises if it failed."""
        if not self._ready.wait(timeout):
            return False
        if self._start_error is not None:
            raise LspError(self._start_error, code="server_failed")
        return True

    @property
    def start_error(self) -> str | None:
        return self._start_error

    @property
    def alive(self) -> bool:
        proc = self._proc
        if self._start_error is not None:
            return False
        return proc is None or proc.poll() is None

    def shutdown(self) -> None:
        """Ask the server to exit, then make sure it has."""
        with self._state_lock:
            self._closing = True
        if self._proc is not None and self._proc.poll() is None and self._ready.is_set():
            try:
                self.request("shutdown", None, timeout=_SHUTDOWN_GRACE_SECS)
                self.notify("exit", None)
                self._proc.wait(timeout=_SHUTDOWN_GRACE_SECS)
            except Exception:
                logger.debug("code_intel: %s did not shut down cleanly", self.server_id)
        self.kill()

    def kill(self) -> None:
        # The server's own process only: `shutdown` + `exit` (see `shutdown`) is
        # how a server is asked to take its workers down with it, and the kill
        # here is the fallback for one that would not answer.
        with self._state_lock:
            self._closing = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=_SHUTDOWN_GRACE_SECS)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=_SHUTDOWN_GRACE_SECS)
                except subprocess.TimeoutExpired:
                    logger.warning("code_intel: %s (pid %s) did not exit", self.server_id, proc.pid)
        if self._cleanup_path:
            try:
                os.unlink(self._cleanup_path)
            except OSError:
                pass
            self._cleanup_path = None
        self._fail_pending("language server exited")

    @property
    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc is not None else None

    def stderr_tail(self) -> str:
        return bytes(self._stderr_tail).decode("utf-8", "replace")

    def _stderr_suffix(self) -> str:
        """The server's last stderr output, for the log line that reports its exit.

        Waits briefly for the drain thread, which reaches EOF once the child has
        exited, so the tail holds the message the server died with.
        """
        thread = self._stderr_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_STDERR_JOIN_SECS)
        from kiro_crew.security.redaction import redact_credentials

        # The tail can quote the server's environment or arguments; the log is
        # the one place it goes raw otherwise.
        tail = redact_credentials(self.stderr_tail().strip())[0]
        return f"\n--- {self.server_id} stderr (last {len(tail)} chars) ---\n{tail}" if tail else ""

    # ── JSON-RPC ───────────────────────────────────────────────────────────

    def request(
        self, method: str, params: Any, *, timeout: float, require_ready: bool = True
    ) -> Any:
        if require_ready and not self._ready.is_set():
            raise LspError("language server is starting", code="server_starting")
        future: Future = Future()
        with self._pending_lock:
            self._next_id += 1
            request_id = self._next_id
            self._pending[request_id] = future
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            return future.result(timeout=timeout)
        except FutureTimeout as exc:
            self._send_quietly(
                {"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": request_id}}
            )
            raise LspError(f"{method} timed out", code="timeout") from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def notify(self, method: str, params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise LspError("language server is not running", code="server_failed")
        data = encode_message(payload)
        with self._write_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise LspError("language server closed its input", code="server_failed") from exc

    def _send_quietly(self, payload: dict) -> None:
        try:
            self._send(payload)
        except LspError:
            pass

    def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                message = read_message(proc.stdout)
                if message is None:
                    break
                self._dispatch(message)
        except FramingError as exc:
            logger.warning("code_intel: %s sent a bad frame: %s", self.server_id, exc)
            self.kill()
        except Exception:
            logger.exception("code_intel: %s reader crashed", self.server_id)
        finally:
            if self._ready.is_set() and not self._closing:
                try:
                    proc.wait(timeout=_STDERR_JOIN_SECS)
                except subprocess.TimeoutExpired:
                    pass
                logger.warning(
                    "code_intel: %s server for %s exited (rc=%s)%s",
                    self.server_id,
                    self.root,
                    proc.returncode,
                    self._stderr_suffix(),
                )
            self._fail_pending("language server exited")

    def _drain_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        try:
            for chunk in iter(lambda: proc.stderr.read(1024), b""):  # type: ignore[union-attr]
                self._stderr_tail.extend(chunk)
                del self._stderr_tail[:-_STDERR_TAIL_BYTES]
        except Exception:
            pass

    def _dispatch(self, message: dict) -> None:
        if "method" in message:
            if "id" in message:
                self._answer_server_request(message)
            else:
                self._note(message)
            return
        request_id = message.get("id")
        with self._pending_lock:
            future = self._pending.get(request_id) if isinstance(request_id, int) else None
        if future is None or future.done():
            return
        if "error" in message:
            error = message.get("error") or {}
            text = error.get("message") if isinstance(error, dict) else None
            rpc_code = error.get("code") if isinstance(error, dict) else None
            future.set_exception(
                LspError(
                    str(text or "request failed"),
                    rpc_code=rpc_code if isinstance(rpc_code, int) else 0,
                )
            )
        else:
            future.set_result(message.get("result"))

    def notices(self) -> list[dict]:
        """The server's recent error and warning messages, oldest first."""
        return list(self._notices)

    def _note(self, message: dict) -> None:
        """Keep what the server reported going wrong; ignore other notifications.

        ``window/showMessage`` errors and warnings are meant for the user and are
        kept as sent. ``window/logMessage`` is the server's own log (jdtls sends
        every build problem there), so it is only remembered, and surfaced when
        jdtls's ``language/status`` says the project did not load
        (``ProjectStatus`` ``ERROR``; ``WARNING`` means it loaded with a
        problem and becomes a warning): the log line before that status
        names the reason, such as a Gradle import that failed.
        """
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, dict):
            return
        text = params.get("message")
        if not isinstance(text, str) or not text.strip():
            return
        first = _LOG_TIMESTAMP.sub("", text.strip().splitlines()[0])[:_NOTICE_CHARS]
        if method == "window/logMessage":
            if params.get("type") in _NOTICE_TYPES:
                self._last_log_problem = first
            return
        if method == "window/showMessage":
            kind = params.get("type")
            level = _NOTICE_TYPES.get(kind) if isinstance(kind, int) else None
            if level:
                self._add_notice(level, first)
        elif method == "language/status":
            kind = params.get("type")
            if kind == "Error":
                self._add_notice("error", first)
            elif kind == "ProjectStatus" and first.upper() == "ERROR":
                reason = self._last_log_problem or first
                self._add_notice("error", f"The project did not load: {reason}")
            elif kind == "ProjectStatus" and first.upper() == "WARNING" and self._last_log_problem:
                # Loaded, but something was off (a missing source folder, say):
                # worth showing when an answer comes back empty, not an error.
                self._add_notice(
                    "warning", f"The project loaded with a warning: {self._last_log_problem}"
                )

    def _add_notice(self, level: str, message: str) -> None:
        if not self._notices or self._notices[-1]["message"] != message:
            self._notices.append({"level": level, "message": message})

    def _answer_server_request(self, message: dict) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        result: Any = None
        if method == "workspace/configuration":
            items = params.get("items") if isinstance(params, dict) else None
            result = [None] * (len(items) if isinstance(items, list) else 0)
        elif method == "workspace/workspaceFolders":
            result = self._workspace_folders()
        elif method == "window/showMessageRequest":
            self._note({"method": "window/showMessage", "params": params})
            result = None
        elif method in (
            "window/workDoneProgress/create",
            "client/registerCapability",
            "client/unregisterCapability",
            "workspace/semanticTokens/refresh",
            "workspace/inlayHint/refresh",
            "workspace/codeLens/refresh",
            "workspace/diagnostic/refresh",
        ):
            result = None
        else:
            self._send_quietly(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": _METHOD_NOT_FOUND, "message": f"unsupported: {method}"},
                }
            )
            return
        self._send_quietly({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def _fail_pending(self, reason: str) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(LspError(reason, code="server_failed"))

    # ── documents ──────────────────────────────────────────────────────────

    def sync_document(self, path: str, language_id: str, text: str) -> str:
        """Make the server's copy of *path* equal *text*; return its URI.

        The first request for a file opens it; later ones send a full-text change
        only when the text moved, so positions always refer to what the viewer
        shows, unsaved edits included.
        """
        uri = path_to_uri(path)
        with self._documents_lock:
            known = self._documents.get(uri)
            if known is None:
                self.notify(
                    "textDocument/didOpen",
                    {
                        "textDocument": {
                            "uri": uri,
                            "languageId": language_id,
                            "version": 1,
                            "text": text,
                        }
                    },
                )
                self._documents[uri] = (1, text)
                while len(self._documents) > MAX_OPEN_DOCUMENTS:
                    oldest, _ = self._documents.popitem(last=False)
                    self._send_quietly(
                        {
                            "jsonrpc": "2.0",
                            "method": "textDocument/didClose",
                            "params": {"textDocument": {"uri": oldest}},
                        }
                    )
            elif known[1] != text:
                self._documents.move_to_end(uri)
                version = known[0] + 1
                self.notify(
                    "textDocument/didChange",
                    {
                        "textDocument": {"uri": uri, "version": version},
                        "contentChanges": [{"text": text}],
                    },
                )
                self._documents[uri] = (version, text)
        return uri
