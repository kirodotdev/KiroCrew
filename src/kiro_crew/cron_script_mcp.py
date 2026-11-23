"""The MCP client a cron script calls tools through.

``McpToolClient`` spawns one local MCP server as a subprocess and speaks JSON-RPC to
it over stdio, or, for a remote (``url``) entry, speaks the same JSON-RPC to it over
streamable HTTP through :class:`kiro_crew.kas_http_bridge.Bridge` from this process;
``KeptMcpServers`` keeps one client per server for the length of a script run.
``_resolve_mcp_server`` turns a server name into the argv and environment to spawn,
or the url and headers to call, from Crew's own agent spec (``kirocrew.json``),
under the launch rules the session and gateway paths apply: a muted or registry
entry is refused, so is a remote entry the bridge cannot call as configured
(:func:`_refuse_uncallable_remote`), and ``${VAR}`` in a local server's ``env`` and
a remote server's ``headers`` is resolved as kiro-cli resolves it.
``KeptMcpServers.call_tool`` refuses a tool the user switched off.

Part of :mod:`kiro_crew.cron_script`, the import path callers, tests and docs use: it
reaches every name here, and a patch through it reaches this module too
(:mod:`kiro_crew._facade_mirror`). Logs under the ``kiro_crew.cron_script`` logger.
"""

from __future__ import annotations

import io
import json
import logging
import os
import queue
import subprocess
import tempfile
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

from kiro_crew import mcp_structured_text as _structured_text
from kiro_crew import platform_compat
from kiro_crew.agent_discovery import _read_agent_spec
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.cron_script_child_env import _CRON_ENV_DENY, _GRANTED_ENV_KEYS, _clean_cron_env
from kiro_crew.cron_script_redaction import (
    _REDACT_STRADDLE_MARGIN,
    _pem_open_label,
    _pem_state_before,
    _safe_tail_redaction_window,
)
from kiro_crew.env import sanitize_spec_env
from kiro_crew.json_line import parse_json_object_line
from kiro_crew.sandbox import cgroup_scope_argv, popen_limited, wrap_argv
from kiro_crew.security import _REDACTED_CREDENTIAL_TAG, redact
from kiro_crew.subprocess_utf8 import UTF8_TEXT

logger = logging.getLogger("kiro_crew.cron_script")


# How many bytes of an MCP server's captured stderr ``_stderr_tail`` reads,
# taken from the END. The capture grows for the whole session, so reading it
# whole would make error reporting cost O(session output). The read only has
# to reach far enough back for ``_safe_tail_redaction_window`` to see its
# window plus the context before it (PEM markers, the severed line).
_STDERR_TAIL_READ_SPAN = 64 * 1024


# ── MCP Tool Bridge ──


class McpToolError(RuntimeError):
    """A tool call refused in a way that leaves the server usable.

    Either the MCP server answered the call with an error, or the tool is one the
    user switched off, which is refused before any server is started.
    """


def _joined_text(content: object) -> str:
    """Every text block of an MCP tool result, in order, joined by newlines.

    A tool may answer with more than one block, for example a leading
    untrusted-content notice followed by the JSON payload. Returning only the
    first block handed the script the notice and dropped the payload, so all
    text blocks are kept, the notice included. Non-text blocks carry no text
    and are skipped.
    """
    if not isinstance(content, list):
        return ""
    texts = [
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    return "\n".join(texts)


class _RemoteMcpServer(NamedTuple):
    """A remote entry as :func:`_resolve_mcp_server` resolves it: what the bridge dials."""

    url: str
    headers: dict[str, str]


class _AnswerLines(io.BytesIO):
    """The ``out`` stream a :class:`~kiro_crew.kas_http_bridge.Bridge` writes answers to.

    Each newline-terminated line the bridge writes is put on :attr:`lines` as it
    arrives, so a call can take its own answer while the request that carried it is
    still reading an event stream the server keeps open.
    """

    def __init__(self) -> None:
        super().__init__()
        self.lines: queue.Queue[bytes] = queue.Queue()
        self._partial = b""

    def write(self, data: Any, /) -> int:
        chunk = self._partial + bytes(data)
        *complete, self._partial = chunk.split(b"\n")
        for line in complete:
            self.lines.put(line)
        return len(data)


#: How long a remote call waits for its own answer. The bridge answers a request
#: itself (code -32000) once a read from the server has blocked for its own
#: ``_READ_TIMEOUT_SECS``, so this bound only names a call that nothing answers.
_REMOTE_ANSWER_SECS = 630.0


class McpToolClient:
    """Minimal MCP JSON-RPC client: one server per client, a tool call at a time.

    A local entry is spawned as a subprocess and spoken to over stdio. A remote
    (``url``) entry is spoken to over streamable HTTP by a
    :class:`kiro_crew.kas_http_bridge.Bridge` held in this process, with in-memory
    streams in place of the process's pipes: each request is sent on a daemon
    thread of its own, as ``Bridge.serve`` sends it, so a call returns once its own
    id is answered even when the server keeps the event stream open. A failure the
    bridge reports as its own (it writes the reason to its ``err`` stream and
    answers the id with code -32000) raises ``RuntimeError``, so
    :class:`KeptMcpServers` stops the client; a JSON-RPC error the server sends
    stays :class:`McpToolError`, as for a local server.
    """

    _bridge: Any = None

    def __init__(self, server_name: str, session_key: str = ""):
        self._server_name = server_name
        self._session_key = session_key
        resolved = _resolve_mcp_server(server_name)
        if not resolved:
            raise RuntimeError(f"MCP server '{server_name}' not found in agent config")
        if isinstance(resolved, _RemoteMcpServer):
            self._open_remote(resolved)
            return
        argv, spec_env = resolved
        sandboxed_argv, self._sandbox_cleanup = wrap_argv(list(argv), mode="standard")
        sandboxed_argv = cgroup_scope_argv(sandboxed_argv)  # cgroup DoS ceiling
        # SECURITY: the confinement wrappers prepended above (`systemd-run` on
        # Linux, `env` -> `sandbox-exec` on macOS) are absolute paths, pinned by
        # the functions that prepend them. That matters here because Popen is
        # handed an env whose PATH is overlaid from the per-server agent config
        # below, and CPython resolves a slash-less argv[0] through THAT env's PATH
        # (os.get_exec_path) -- a bare-name wrapper would be redirectable to an
        # attacker binary running BEFORE confinement exists. Pinning belongs in
        # those producers, not here: re-pinning at the spawn site would also
        # rewrite argv[0] on the fail-open no-sandbox path, where argv[0] is the
        # OPERATOR-declared command and silently resolving it against the
        # gateway's own directories would override the very PATH selection the
        # spec `env` block exists to provide.
        #
        # Build the subprocess env: start from the secret-scrubbed cron env, then
        # overlay the per-server `env` block from the agent config (e.g. the PATH
        # that lets a launcher resolve a helper binary it shells out to). A
        # launcher that execs a binary reachable only via that env dies before the
        # initialize handshake if the env is dropped. Three filters apply, and all
        # three are load-bearing:
        #   * _clean_cron_env() strips secrets from the INHERITED env;
        #   * _CRON_ENV_DENY is re-applied to the spec overlay so a denied key
        #     cannot be reintroduced through the config;
        #   * sanitize_spec_env() drops loader/interpreter-injection keys
        #     (LD_*, DYLD_*, and the specific PYTHONPATH/HOME/STARTUP/USERBASE
        #     startup channels -- see env._SPEC_ENV_DENIED_PREFIXES; it is a prefix
        #     set, NOT all of PYTHON*). Those are NOT in _CRON_ENV_DENY, and the spec
        #     env is externally authorable, so without this an `LD_PRELOAD` in a
        #     server's `env` block would be honoured by the dynamic loader inside
        #     the confinement wrapper process -- executing attacker code before
        #     the wrapper establishes containment. Pinning argv[0] does not help:
        #     the loader acts on the pinned binary. This is the same reason
        #     mcp_discovery routes its probe's spec env through the sanitizer;
        #     PATH is deliberately NOT denied, since forwarding it is the point.
        #   * sanitize_spec_env() ALSO drops the reserved KIROCREW_ namespace
        #     (env._SPEC_ENV_RESERVED_PREFIXES), which is an authorization control
        #     rather than a containment one and is the reason the two other filters
        #     are not sufficient here. The server this bridge most often spawns is
        #     `kirocrew-cron` itself, whose ownership checks resolve the calling
        #     session from KIROCREW_SESSION_KEY / KIROCREW_HOST_PID when no
        #     gateway caller block is present -- so one of those in that server's
        #     `env` block would let a SCRIPT CRON name itself another session and
        #     reach that session's jobs. Sandboxing does not bound that:
        #     confinement limits what the child may touch, not whose jobs Kiro
        #     Crew thinks it owns. The deny lives in the shared sanitizer rather
        #     than in _CRON_ENV_DENY so the discovery probe -- which applies no
        #     cron deny-set at all -- is covered by the same control instead of a
        #     second copy of it.
        proc_env = _clean_cron_env()
        proc_env.update(
            sanitize_spec_env((k, v) for k, v in spec_env.items() if k not in _CRON_ENV_DENY)
        )
        if self._session_key:
            # Hard-assigned (not setdefault) AFTER both overlays, for the same
            # reason ScriptContext.notify() hard-assigns ``caller_session``: the
            # inherited env is the script child's own ``os.environ``, which user
            # code can rewrite before calling ``ctx.call_tool``. The identity the
            # spawned server sees must be the one the launcher gave THIS job, not
            # whatever the script put there. Best-effort friction against
            # in-process forgery, on the same footing as the rest of the env-
            # based identity contract (see session_pid_sig's threat model).
            proc_env["KIROCREW_SESSION_KEY"] = self._session_key
        # Capture stderr to a tempfile instead of DEVNULL so spawn/handshake
        # failures are legible. DEVNULL hid the real cause -- wrong
        # Node version, expired auth cookies, OOM kill, sandbox failure -- behind
        # a generic "disconnected during 'initialize'" RuntimeError.
        self._stderr_file = tempfile.NamedTemporaryFile(
            mode="w+", prefix="mcp-stderr-", suffix=".log", delete=False
        )
        try:
            self._proc = popen_limited(
                sandboxed_argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr_file,
                # errors="replace": a byte that is not UTF-8 costs its line
                # (it does not parse) rather than raising out of readline.
                **UTF8_TEXT,
                env=proc_env,
            )
        except Exception:
            self._stderr_file.close()
            Path(self._stderr_file.name).unlink(missing_ok=True)
            if self._sandbox_cleanup:
                Path(self._sandbox_cleanup).unlink(missing_ok=True)
            raise
        assert self._proc.stdin is not None
        assert self._proc.stdout is not None
        self._initialize()

    def _initialize(self) -> None:
        """The MCP handshake; a failure closes the client and raises."""
        self._req_id = 0
        try:
            answer = self._rpc(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "kirocrew-cron-script", "version": "0.1"},
                },
            )
            if self._bridge is not None and "error" in answer:
                error = answer["error"]
                message = error.get("message") if isinstance(error, dict) else error
                raise RuntimeError(
                    f"MCP server '{self._server_name}' refused 'initialize': {str(message)[:300]}"
                )
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def _open_remote(self, remote: _RemoteMcpServer) -> None:
        """Hold a bridge to *remote* in place of a process, then run the handshake.

        The request leaves from this process: there is no local server to confine,
        so no subprocess or sandbox wrapper is involved.
        """
        # Deferred for the reason given in _switched_off_tools.
        from kiro_crew.kas_http_bridge import Bridge

        self._answers = _AnswerLines()
        self._bridge_err = io.StringIO()
        self._closed = False
        self._bridge = Bridge(
            remote.url,
            remote.headers,
            server=self._server_name,
            out=self._answers,
            err=self._bridge_err,
        )
        self._initialize()

    def _send(self, msg: dict) -> None:
        if self._bridge is not None:
            line = json.dumps(msg).encode("utf-8")
            if "id" not in msg:
                # A notification is sent before the next message, as Bridge.serve
                # sends it, so the server sees ``initialized`` before the call.
                self._bridge.send(line)
                return
            self._request = threading.Thread(
                target=self._bridge.send,
                args=(line,),
                name=f"mcp-remote-{self._server_name}",
                daemon=True,
            )
            self._request.start()
            return
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()

    def _recv(self) -> dict | None:
        """The next line the server wrote as a JSON object, or ``None`` at EOF.

        A line that is not one (a blank, banner or log line on stdout, a
        scalar, a value nested past the decoder's ceiling) comes back as an
        empty object, so it costs that line rather than the call, and every
        line read counts toward ``_rpc``'s cap: a server that writes only
        noise fails the call instead of holding it.
        """
        assert self._proc.stdout is not None
        line = self._proc.stdout.readline()
        if not line:  # EOF
            return None
        msg = parse_json_object_line(line)
        return {} if msg is None else msg

    def _stderr_tail(self, limit: int = 1024) -> str:
        """Return the last `limit` characters of the subprocess's captured stderr.

        Credentials and exfiltration URLs are redacted BEFORE the tail is cut,
        so a secret straddling the cut cannot survive as an unredacted
        fragment. This uses the same rule as the script-stderr tail:
        ``redact`` runs over ``_safe_tail_redaction_window`` (the kept slice
        plus ``_REDACT_STRADDLE_MARGIN``, with severed lines and open
        private-key blocks masked), and only then is the tail sliced.

        Only the last ``_STDERR_TAIL_READ_SPAN`` bytes are read. When that
        read starts mid-file, its first line is dropped (it was cut, so a
        credential on it may have lost its anchor), and a private-key block
        that opened before the read is treated as still open.
        """
        path = getattr(self, "_stderr_file", None)
        if path is None:
            return ""
        try:
            span = max(_STDERR_TAIL_READ_SPAN, 4 * (limit + _REDACT_STRADDLE_MARGIN))
            with open(path.name, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                start = max(0, fh.tell() - span)
                open_label, carry = _pem_state_before(fh, start) if start > 0 else (None, "")
                fh.seek(start)
                text = fh.read(span).decode("utf-8", errors="replace")
            if start > 0:
                newline = text.find("\n")
                if newline == -1:
                    return _REDACTED_CREDENTIAL_TAG
                severed = carry + text[: newline + 1]
                open_label = _pem_open_label(severed, 0, len(severed), open_label)
                text = text[newline + 1 :]
                if open_label is not None:
                    # The block opened before the read. Restore its BEGIN so
                    # the window helper sees it open and masks its body.
                    text = f"-----BEGIN {open_label}-----\n" + text
            text = text.strip()
            return redact(_safe_tail_redaction_window(text, limit))[-limit:]
        except Exception as exc:
            # Defensive — _stderr_tail runs inside error reporting itself, so we
            # never raise here. We DO log the exception type at debug so that a
            # silently broken tail (disk/encoding error, missing tempfile) is
            # diagnosable when investigating MCP spawn failures.
            logger.debug("_stderr_tail failed: %s", type(exc).__name__)
            return ""

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._req_id += 1
        req_id = self._req_id
        name = getattr(self, "_server_name", "?")
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        if self._bridge is not None:
            return self._remote_answer(method, req_id)
        for _ in range(1000):
            msg = self._recv()
            if msg is None:
                rc = self._proc.poll()
                tail = self._stderr_tail()
                raise RuntimeError(
                    f"MCP server '{name}' disconnected during '{method}' "
                    f"(rc={rc}); stderr tail: {tail or '(empty)'}"
                )
            if msg.get("id") == req_id:
                return msg
        raise RuntimeError(f"MCP server '{name}' did not respond to '{method}' within 1000 lines")

    def _remote_answer(self, method: str, req_id: int) -> dict:
        """The bridge's answer to *req_id*, taken as soon as it is written.

        Raises ``RuntimeError`` naming the server and the bridge's reason (which
        never carries a header value) when the answer is the bridge's own failure,
        when the request ended without answering *req_id* (a 202 to a request, say),
        or when nothing answers within ``_REMOTE_ANSWER_SECS``.
        """
        name = self._server_name
        deadline = time.monotonic() + _REMOTE_ANSWER_SECS
        while True:
            remaining = deadline - time.monotonic()
            try:
                line = self._answers.lines.get(timeout=max(0.0, min(remaining, 1.0)))
            except queue.Empty:
                if remaining <= 0:
                    raise RuntimeError(
                        f"MCP server '{name}' did not answer '{method}' within "
                        f"{_REMOTE_ANSWER_SECS:g}s"
                    ) from None
                # The request writes its answer before it ends, so an ended request
                # with nothing queued will never answer.
                if not self._request.is_alive() and self._answers.lines.empty():
                    raise RuntimeError(
                        f"MCP server '{name}' ended its answer to '{method}' without a response"
                    ) from None
                continue
            msg = parse_json_object_line(line.decode("utf-8", errors="replace"))
            if msg is None or msg.get("id") != req_id or "method" in msg:
                continue
            reason = self._bridge_failure(msg.get("error"))
            if reason is not None:
                raise RuntimeError(f"MCP server '{name}' {reason} (during '{method}')")
            return msg

    def _bridge_failure(self, error: object) -> str | None:
        """The bridge's own reason when *error* is its failure, not the server's error.

        The bridge prints each failure to its ``err`` stream before it answers the
        ids with it, so an error the server sent itself, -32000 included, is told
        apart by that line being absent.
        """
        from kiro_crew.kas_http_bridge import _BRIDGE_ERROR

        if not isinstance(error, dict) or error.get("code") != _BRIDGE_ERROR:
            return None
        message = error.get("message")
        if not isinstance(message, str) or message not in self._bridge_err.getvalue().splitlines():
            return None
        prefix = f"kirocrew: MCP server {self._server_name!r} "
        return message[len(prefix) :] if message.startswith(prefix) else message

    def call_tool(self, name: str, arguments: dict) -> str:
        r = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if "error" in r:
            raise McpToolError(f"MCP tool error: {r['error']}")
        result = r.get("result", {})
        # A payload only in ``structuredContent`` reaches the script as its JSON.
        result = _structured_text.with_structured_text(result) or result
        content = result.get("content", [])
        if result.get("isError"):
            raise McpToolError(f"MCP tool error: {_joined_text(content) or 'unknown error'}")
        return _joined_text(content)

    def is_running(self) -> bool:
        """Whether the server process is still up (a remote client: not yet closed),
        and so can take another call."""
        if self._bridge is not None:
            return not self._closed
        return self._proc.poll() is None

    def close(self) -> None:
        if self._bridge is not None:
            self._closed = True
            try:
                self._bridge.close()  # best-effort session DELETE
            except Exception:
                logger.debug("ending a remote MCP session failed", exc_info=True)
            return
        try:
            if platform_compat.IS_WINDOWS:
                # A server behind a ``.cmd`` wrapper is a grandchild terminate() misses.
                platform_compat.kill_popen_tree(self._proc)
            else:
                self._proc.terminate()
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        except Exception:
            # Whatever stopped the shutdown, the cleanup in ``finally`` still runs.
            pass
        finally:
            stderr_file = getattr(self, "_stderr_file", None)
            if stderr_file is not None:
                try:
                    stderr_file.close()
                except Exception:
                    # A failed close must not stop the unlink below.
                    pass
                try:
                    Path(stderr_file.name).unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning(
                        "could not delete MCP stderr capture %s: %s", stderr_file.name, exc
                    )
            if self._sandbox_cleanup:
                Path(self._sandbox_cleanup).unlink(missing_ok=True)


class KeptMcpServers:
    """One MCP server per name, kept across the calls of a run.

    A server is kept after a call it answered, with a result or with a tool
    error, for the run's next call to the same server name. So a server that
    signs in to a service when it starts signs in once per run rather than once
    per call. A call that fails any other way (the server exited, stopped
    answering, or wrote no answer) stops its server, and the next call starts a
    fresh one. A kept server whose process has exited is replaced. A call made
    while another call to the same server still holds the kept server starts a
    server of its own, and once both finish only one is kept. :meth:`close`
    stops every kept server and keeps none afterwards.

    A call to a tool the user switched off (:func:`_switched_off_tools`) raises
    :class:`McpToolError` before any server is taken or started. Which tools are
    off is read once per server name for the run.
    """

    def __init__(self, session_key: str = ""):
        self._session_key = session_key
        self._kept_clients: dict[str, McpToolClient] = {}
        self._kept_clients_lock = threading.Lock()
        self._closed = False
        self._switched_off: dict[str, frozenset[str]] = {}

    def call_tool(self, server: str, tool: str, args: dict) -> str:
        if _tool_is_switched_off(tool, self._tools_switched_off(server)):
            raise McpToolError(f"tool '{tool}' on MCP server '{server}' is switched off")
        client = self._take_kept_client(server)
        kept = False
        try:
            if client is None:
                client = McpToolClient(server, session_key=self._session_key)
            try:
                result = client.call_tool(tool, args)
            except McpToolError:
                kept = self._keep_client(server, client)
                raise
            kept = self._keep_client(server, client)
            return result
        finally:
            if client is not None and not kept:
                try:
                    client.close()
                except Exception:
                    # The call's own error, or its result, outranks a failed stop.
                    logger.debug("stopping an MCP server after its call failed", exc_info=True)

    def close(self) -> None:
        """Stop every kept server; never raises, and later calls keep none."""
        with self._kept_clients_lock:
            self._closed = True
            clients = list(self._kept_clients.values())
            self._kept_clients.clear()
        for client in clients:
            try:
                client.close()
            except Exception:
                logger.debug("stopping a kept MCP server failed", exc_info=True)

    def _take_kept_client(self, server: str) -> McpToolClient | None:
        """The server kept from an earlier call, if its process is still running."""
        with self._kept_clients_lock:
            client = self._kept_clients.pop(server, None)
        if client is not None and not client.is_running():
            client.close()
            return None
        return client

    def _keep_client(self, server: str, client: McpToolClient) -> bool:
        """Keep ``client`` for the next call to ``server``; False when one is already kept."""
        with self._kept_clients_lock:
            if self._closed or server in self._kept_clients:
                return False
            self._kept_clients[server] = client
            return True

    def _tools_switched_off(self, server: str) -> frozenset[str]:
        """The tools of ``server`` the user switched off, read on the run's first call."""
        with self._kept_clients_lock:
            known = self._switched_off.get(server)
        if known is None:
            read = _switched_off_tools(server)
            with self._kept_clients_lock:
                known = self._switched_off.setdefault(server, read)
        return known


def _crew_agent_spec() -> dict[str, Any] | None:
    """Crew's own agent spec, parsed: ``kirocrew.json``, else the first ``*kirocrew*.json``.

    ``None`` when there is none or it cannot be used.
    """
    cfg_path = kiro_agents_dir() / "kirocrew.json"
    if not cfg_path.exists():
        # Fall back to any kirocrew-named agent spec in the same agents dir.
        for p in kiro_agents_dir().glob("*kirocrew*.json"):
            cfg_path = p
            break
    if not cfg_path.exists():
        return None
    # The agents dir is user-writable and shared with other tools, so this goes
    # through the hardened agent-spec reader (size cap, sensitive-symlink
    # screen, explicit UTF-8, non-object rejection, SEL denial event) instead of
    # a bare ``read_text`` + ``json.loads``. ``None`` covers every unusable file,
    # malformed JSON and non-UTF-8 bytes included, and degrades to "no such
    # server", the same answer an absent entry gives.
    return _read_agent_spec(
        cfg_path,
        operation="cron_resolve_mcp_server",
        source="cron",
    )


def _switched_off_tools(server: str) -> frozenset[str]:
    """The tools of ``server`` the user switched off, as a chat session reads them.

    :func:`kiro_crew.agent_sdk.agent_specs.switched_off_tools` (the chat session's
    ``acp.session_mcp.session_mcp_disabled_tools``) on Crew's agent spec:
    ``disabledTools`` on the server's entry there, unioned with the one in the Kiro
    global ``~/.kiro/settings/mcp.json``, where the dashboard's tool-off action
    writes. A source that cannot be read switches nothing off. The keys are
    compared through :func:`kiro_crew.mcp_utils.mcp_server_alias`, as the KAS path
    compares them, because the tool-off action writes the key under the alias
    discovery shows (``playwright-mcp`` for ``npm:@playwright/mcp``). The entries
    are returned as written; :func:`_tool_is_switched_off` matches them.
    """
    # Deferred: an import at module scope here is a name the cron_script facade
    # serves, and these are only needed on a call.
    from kiro_crew.agent_sdk.agent_specs import switched_off_tools
    from kiro_crew.mcp_utils import mcp_server_alias

    alias = mcp_server_alias(server)
    pairs = switched_off_tools(_crew_agent_spec())
    return frozenset(tool for name, tool in pairs if mcp_server_alias(name) == alias)


def _tool_is_switched_off(tool: str, entries: frozenset[str]) -> bool:
    """Whether a ``disabledTools`` entry in *entries* covers *tool*, as kiro-cli reads it.

    kiro-cli compares an entry exactly, and an entry with ``*`` or ``?`` also as a
    glob (``globset``). Such a glob is matched with :func:`fnmatch.fnmatchcase`;
    one holding ``{``, ``}`` or ``\\``, whose meaning ``fnmatch`` does not share,
    covers every tool, so the call is refused rather than let through.
    """
    import fnmatch  # deferred for the reason given in _switched_off_tools

    for entry in entries:
        if entry == tool:
            return True
        if "*" in entry or "?" in entry:
            if any(c in entry for c in "{}\\") or fnmatch.fnmatchcase(tool, entry):
                return True
    return False


def _refuse_unlaunchable(name: str, spec: dict[str, Any]) -> None:
    """Raise ``RuntimeError`` naming why ``ctx.call_tool`` may not start ``spec``.

    The same answers the session and gateway paths give. A server muted in its entry
    or in any MCP config scope is not launched
    (:func:`kiro_crew.mcp_cleanup.mcp_server_muted_in_any_store`, fail-closed on a
    ``disabled`` that is not a boolean). A ``"type": "registry"`` entry names an
    admin catalog record, not a local process. A ``url`` entry is a remote server,
    called rather than started: :func:`_refuse_uncallable_remote` holds what stays
    refused for it, and ``command`` is never read, so a placeholder command beside
    the url is never run. An entry with no command has nothing to start.
    """
    # Deferred for the reason given in _switched_off_tools.
    from kiro_crew.mcp_cleanup import mcp_entry_is_registry_governed, mcp_server_muted_in_any_store

    if mcp_server_muted_in_any_store(name, spec):
        raise RuntimeError(
            f"MCP server '{name}' is disabled; turn it on under MCP Servers "
            "to call it from a script cron"
        )
    if mcp_entry_is_registry_governed(spec):
        raise RuntimeError(
            f"MCP server '{name}' is governed by the admin registry; ctx.call_tool cannot "
            "start it"
        )
    url = spec.get("url")
    if isinstance(url, str) and url:
        _refuse_uncallable_remote(name, spec, url)
        return
    command = spec.get("command")
    if not isinstance(command, str) or not command:
        raise RuntimeError(f"MCP server '{name}' declares no command")


def _refuse_uncallable_remote(name: str, spec: dict[str, Any], url: str) -> None:
    """Raise ``RuntimeError`` naming why ``ctx.call_tool`` may not call remote ``spec``.

    Checked before any request. A url that is not ``http`` or ``https`` is not one
    the bridge can dial (KAS's launcher refuses it too). An ``sse`` entry speaks the
    older two-endpoint transport the bridge does not. A header holding a ``secret://`` reference is not
    resolved for a remote server, so, as the dashboard probe does, the server is not
    contacted with the reference as its credential; only the header's name is given.
    A plain ``http`` url to a host that is not loopback is refused when it carries
    headers only while the operator's ``agent.kas_refuse_plain_http_mcp_headers``
    opt-in is on (:func:`kiro_crew.agent_sdk.agent_specs.plain_http_headers_refused`),
    as KAS's bridge refuses it; otherwise it is called, as that bridge calls it.
    """
    # Deferred for the reason given in _switched_off_tools.
    from kiro_crew.agent_sdk.agent_specs import plain_http_headers_refused
    from kiro_crew.mcp_gateway.secret_uri import display_header_names, header_secret_refs

    if not url.strip().lower().startswith(("https://", "http://")):
        raise RuntimeError(
            f"MCP server '{name}' has a url that is not http or https; ctx.call_tool calls "
            "only an http(s) server"
        )
    if spec.get("type") == "sse":
        raise RuntimeError(
            f"MCP server '{name}' is an 'sse' server; ctx.call_tool calls only a remote "
            "server that speaks streamable HTTP"
        )
    headers = spec.get("headers")
    vaulted = header_secret_refs(headers)
    if vaulted:
        raise RuntimeError(
            f"MCP server '{name}' has a secret:// reference in header "
            f"{display_header_names(vaulted)}, which is not resolved for a remote server, "
            "so ctx.call_tool does not contact it"
        )
    if isinstance(headers, dict) and headers and plain_http_headers_refused(url):
        raise RuntimeError(
            f"MCP server '{name}' would send its headers unencrypted to a plain http url on "
            "another host, which agent.kas_refuse_plain_http_mcp_headers refuses"
        )


def _placeholder_source() -> dict[str, str]:
    """The view ``${VAR}`` in a server's ``env`` or ``headers`` is filled in from.

    The shared view drops only a narrow prefix list of credential names. A granted
    run's os.environ also holds the job's vault values, which _clean_cron_env keeps
    from a server because the grant covers the approved script body and not the
    servers it calls; a reference to one, or to a _CRON_ENV_DENY name, is therefore
    a miss and stays literal, so the value cannot reach the server under a name the
    entry chooses.
    """
    # Deferred for the reason given in _switched_off_tools.
    from kiro_crew.mcp_gateway.rewriter_env import _placeholder_source_env

    return {
        k: v
        for k, v in _placeholder_source_env().items()
        if k not in _CRON_ENV_DENY and k not in _GRANTED_ENV_KEYS
    }


def _expanded(name: str, raw: dict[Any, Any]) -> dict[Any, Any]:
    """*raw* with ``${VAR}`` / ``${env:VAR}`` in each string value filled in, as the
    broker and KAS paths fill them in (``_expand_env_placeholders``), from
    :func:`_placeholder_source`; a miss stays literal, as in kiro-cli."""
    from kiro_crew.mcp_gateway.rewriter_env import _expand_env_placeholders

    source = _placeholder_source()
    return {
        k: (_expand_env_placeholders(v, source=source, server=name) if isinstance(v, str) else v)
        for k, v in raw.items()
    }


#: A header name urllib sends as written: an RFC 9110 token.
_HEADER_NAME_CHARS = frozenset(
    "!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
)


def _remote_headers(name: str, raw: object) -> dict[str, str]:
    """The entry's ``headers`` as the bridge sends them, ``${VAR}`` filled in.

    A non-object block is ignored, and a header that cannot be sent as written (a
    value that is not a string, a name that is not a token, a line break or NUL in
    the value) is dropped, each with a log line naming the header and never its
    value, which can hold a credential.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning(
            "MCP server %r declares a non-object 'headers' (%s); ignoring it",
            name,
            type(raw).__name__,
        )
        return {}
    headers: dict[str, str] = {}
    for key, value in _expanded(name, raw).items():
        if not isinstance(value, str):
            reason = "value is not a string"
        elif not isinstance(key, str) or not key or not set(key) <= _HEADER_NAME_CHARS:
            reason = "not a header name"
        elif any(c in value for c in "\r\n\0"):
            reason = "line break or NUL byte in value"
        else:
            headers[key] = value
            continue
        logger.warning(
            "MCP server %r declares an invalid header %r (%s); ignoring it", name, key, reason
        )
    return headers


@lru_cache(maxsize=16)
def _resolve_mcp_server(
    name: str,
) -> tuple[tuple[str, ...], dict[str, str]] | _RemoteMcpServer | None:
    """Read MCP server command + env from Crew's agent spec (cached per process).

    Returns ``(argv, env)``, or for a ``url`` entry a :class:`_RemoteMcpServer`
    holding :func:`kiro_crew.agent_sdk.agent_specs.remote_bridge_url` of it (the spelling KAS's
    bridge dials) and its headers (:func:`_remote_headers`), or ``None`` when the
    spec declares no such server (an entry that is not a non-empty object counts as
    none). Raises ``RuntimeError`` naming the reason when the entry is one
    ``ctx.call_tool`` may not start or call (:func:`_refuse_unlaunchable`). The
    per-server ``env`` block is
    required for launchers that shell out to a helper binary only reachable via
    the ``PATH`` the config supplies: dropping it makes the spawned MCP die at the
    JSON-RPC ``initialize`` handshake. When the config has no ``env`` block -- or
    declares one that is not a JSON object -- the env dict is empty. ``${VAR}``
    and ``${env:VAR}`` in its values are resolved by the expander the broker and
    KAS paths use (``mcp_gateway.rewriter_env._expand_env_placeholders``), from its
    filtered view less ``_CRON_ENV_DENY`` and ``_GRANTED_ENV_KEYS``. That view drops
    only a narrow prefix list (``AWS_SECRET``, ``AWS_SESSION``, ``AWS_ACCESS``,
    ``OAUTH``, ``SSH_AUTH_SOCK``, ``GNUPGHOME``, ``GIT_ASKPASS``) and the agent-denied
    names, so a gateway variable outside it, ``GITHUB_TOKEN`` say, is dereferenced,
    while a cron-denied or job-granted one never is. A miss stays literal, as in
    kiro-cli. ``args`` are not expanded, as kiro-cli does not expand them.
    """
    cfg = _crew_agent_spec()
    if cfg is None:
        return None
    servers = cfg.get("mcpServers")
    spec = servers.get(name) if isinstance(servers, dict) else None
    if not isinstance(spec, dict) or not spec:
        return None
    _refuse_unlaunchable(name, spec)
    url = spec.get("url")
    if isinstance(url, str) and url:
        from kiro_crew.agent_sdk.agent_specs import remote_bridge_url

        return _RemoteMcpServer(remote_bridge_url(url), _remote_headers(name, spec.get("headers")))
    argv = tuple([spec["command"]] + spec.get("args", []))
    # A hand-edited config can spell `env` as anything JSON allows. Only a
    # mapping has .items(), so a string/list/number would raise AttributeError
    # here and fail EVERY ctx.call_tool for that server with a traceback that
    # names this function rather than the malformed config. Treat a non-mapping
    # as absent and say so once: the declaration cannot be honoured either way,
    # and a silent drop reads as the env-drop bug this function exists to fix.
    raw_env = spec.get("env")
    if raw_env is not None and not isinstance(raw_env, dict):
        logger.warning(
            "MCP server %r declares a non-object 'env' (%s); ignoring it",
            name,
            type(raw_env).__name__,
        )
        raw_env = None
    if raw_env:
        raw_env = _expanded(name, raw_env)
    env: dict[str, str] = {}
    for raw_key, raw_value in (raw_env or {}).items():
        key, value = str(raw_key), str(raw_value)
        # Stringifying is not enough: `Popen` validates the env at the execve
        # boundary and rejects the WHOLE spawn over one bad pair -- ValueError
        # "illegal environment variable name" for `=` in a name, "embedded null
        # byte" for a NUL in either half. Since that fires before the fork, a
        # single malformed entry in this block aborts EVERY ctx.call_tool for the
        # server, and the traceback names Popen rather than the config that is
        # actually wrong -- the same failure shape, for the same reason, as the
        # non-object `env` handled above. An empty name is not a crash but is
        # unreachable (`getenv("")` is always NULL), so it is dropped on the same
        # "cannot be honoured either way" ground.
        #
        # Dropped per ENTRY rather than rejected per SERVER: the sibling entries
        # are still honourable, and the whole point of this function is that a
        # server whose launcher needs a config-supplied PATH must keep working.
        # Logged for the reason the non-object branch logs -- a silent drop reads
        # as the env-drop bug this function exists to fix. The name is named so
        # the operator knows which entry to edit; the value never is, since it
        # can hold a credential.
        if not key:
            reason = "empty name"
        elif "=" in key:
            reason = "'=' in name"
        elif "\0" in key:
            reason = "NUL byte in name"
        elif "\0" in value:
            reason = "NUL byte in value"
        else:
            env[key] = value
            continue
        logger.warning(
            "MCP server %r declares an invalid 'env' entry %r (%s); ignoring it",
            name,
            key,
            reason,
        )
    return argv, env


# A traceback names these by the path callers import them from.
McpToolError.__module__ = "kiro_crew.cron_script"
