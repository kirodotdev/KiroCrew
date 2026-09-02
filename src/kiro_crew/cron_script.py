"""Code-based cron scripts — deterministic Python as cron callbacks.

Scripts under ``<config_dir>/crons/`` are LLM-writeable by design. The sandbox +
path-restriction prevents filesystem escape, but the LLM can register
self-written scripts. Mitigations: SEL audit trail on every invocation,
is_sensitive_path() blocks credential files, auto-pause after 5 consecutive
failures, concurrent execution guard prevents double-fire.

Usage:
    # <config_dir>/crons/my_monitor.py
    from kiro_crew.cron_script import Skip, Done

    def run(ctx):
        data = ctx.call_tool("kirocrew-core", "local_knowledge_search", {"query": "..."})
        if not ready(data):
            raise Skip()  # silent, retry next tick
        ctx.notify("Done: " + summary)
        raise Done()  # remove cron job
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew import platform_compat
from kiro_crew.agent_discovery import _read_agent_spec
from kiro_crew.config.loader import config_dir, read_local_secret
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.env import sanitize_spec_env
from kiro_crew.github_runner import prevalidated_gh_env
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.port_resolution import resolve_serving_port
from kiro_crew.sandbox import (
    _AGENT_DENIED_ENV_KEYS,
    SandboxUnavailableError,
    cgroup_scope_argv,
    popen_limited,
    run_limited,
    wrap_argv,
)
from kiro_crew.security import is_sensitive_path, redact
from kiro_crew.sel import sel

# Env vars stripped from EVERY cron subprocess (command and script), regardless
# of OS sandbox mode. The OS sandbox can fall back to backend "none" (e.g.
# macOS >= 26, see sandbox._probe_sandbox_exec), so env scrubbing is the only
# guaranteed control on those hosts. _AGENT_DENIED_ENV_KEYS = Slack tokens +
# KIROCREW_OWNER_ID; KIROCREW_INTERNAL_SECRET is handed to scripts via a 0600
# temp file instead of the env (defense-in-depth item 4).
_CRON_ENV_DENY: frozenset[str] = frozenset({"KIROCREW_INTERNAL_SECRET", *_AGENT_DENIED_ENV_KEYS})


def _clean_cron_env() -> dict[str, str]:
    """Return os.environ minus the cron env-deny set (secrets never inherited)."""
    return {k: v for k, v in os.environ.items() if k not in _CRON_ENV_DENY}


# ── Running-subprocess registry (user-initiated cancellation) ──
#
# Script/command crons run as blocking ``subprocess`` calls inside the cron
# thread executor — cancelling the owning asyncio task cannot interrupt them.
# Each sandboxed child is registered here (keyed by job id) so that
# ``CronService.cancel()`` can SIGTERM the whole process group mid-run.
_PROCS_LOCK = threading.Lock()
_RUNNING_PROCS: dict[str, subprocess.Popen] = {}
_CANCELLED_PROC_JOBS: set[str] = set()

_KILL_ESCALATION_GRACE_SECS = 5.0


def _register_proc(job_id: str, proc: subprocess.Popen) -> None:
    with _PROCS_LOCK:
        _RUNNING_PROCS[job_id] = proc


def _unregister_proc(job_id: str, proc: subprocess.Popen) -> bool:
    """Remove the registry entry; return True if this run was cancelled."""
    with _PROCS_LOCK:
        if _RUNNING_PROCS.get(job_id) is proc:
            _RUNNING_PROCS.pop(job_id, None)
        cancelled = job_id in _CANCELLED_PROC_JOBS
        _CANCELLED_PROC_JOBS.discard(job_id)
        return cancelled


def _resolve_safe_pgid(proc: subprocess.Popen) -> int | None:
    """Resolve *proc*'s process group id with broadcast protection.

    Returns None (caller must fall back to the direct Popen handle) unless
    every check passes:

    - ``proc.pid`` must be a real ``int`` > 1. A ``MagicMock`` pid coerces to
      1 via ``__index__``, and ``os.killpg(1, sig)`` is ``kill(-1, sig)`` in
      libc — a signal broadcast to EVERY process this uid can reach, which
      SIGKILLed the whole login session (systemd --user manager included).
    - The resolved pgid must be > 1 (same ``kill(-1)`` footgun) and must not
      be our own process group (suicide / killing the gateway tree).
    """
    if not platform_compat.IS_POSIX:
        # Windows has no process groups (os.getpgid/os.killpg don't exist);
        # callers fall back to platform_compat.kill_process_tree (taskkill /T).
        return None
    pid = getattr(proc, "pid", None)
    if type(pid) is not int or pid <= 1:
        logger.error("kill guard: refusing non-int/reserved pid %r", pid)
        return None
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return None
    if pgid <= 1 or pgid == os.getpgid(0):
        logger.error(
            "kill guard: refusing broadcast/self pgid %d for pid %d", pgid, pid
        )
        return None
    return pgid


def kill_running_process(job_id: str) -> bool:
    """SIGTERM the sandboxed subprocess group for a running script/command cron.

    Escalates to SIGKILL after a grace period from a daemon thread so the
    caller (the async cancel path) never blocks. Returns True when a live
    subprocess was found and signalled.
    """
    with _PROCS_LOCK:
        maybe_proc = _RUNNING_PROCS.get(job_id)
        if maybe_proc is None or maybe_proc.poll() is not None:
            return False
        proc: subprocess.Popen = maybe_proc
        _CANCELLED_PROC_JOBS.add(job_id)
    pgid = _resolve_safe_pgid(proc)
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pgid = None
    if pgid is None:
        # Already gone (or unsignallable) on POSIX; on Windows there are no
        # process groups, so reap the whole tree via taskkill /T
        # (platform_compat) before falling back to a single-process terminate.
        killed_tree = False
        if not platform_compat.IS_POSIX:
            try:
                platform_compat.kill_process_tree(proc.pid, platform_compat.SIGTERM)
                killed_tree = True
            except (OSError, ProcessLookupError):
                killed_tree = False
        if not killed_tree:
            try:
                proc.terminate()
            except Exception:
                # Signal never delivered: clear the cancelled flag so a natural
                # completion is not misreported as a cancellation.
                with _PROCS_LOCK:
                    _CANCELLED_PROC_JOBS.discard(job_id)
                return False

    def _escalate() -> None:
        time.sleep(_KILL_ESCALATION_GRACE_SECS)
        if proc.poll() is None:
            _kill_proc_group(proc)

    threading.Thread(target=_escalate, name=f"cron-cancel-{job_id}", daemon=True).start()
    logger.info("Cancel: sent SIGTERM to subprocess group of cron %s (pid %d)", job_id, proc.pid)
    return True


def _kill_proc_group(proc: subprocess.Popen) -> None:
    """Best-effort SIGKILL of a subprocess and its whole process group."""
    pgid = _resolve_safe_pgid(proc)
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    # Windows (pgid is always None there): reap the tree via taskkill /T before
    # the single-process fallback so children don't orphan.
    if not platform_compat.IS_POSIX:
        try:
            platform_compat.kill_process_tree(proc.pid, platform_compat.SIGKILL)
            return
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.kill()
    except Exception:
        pass


def _drain_after_kill(proc: subprocess.Popen, job_id: str | None) -> None:
    """Reap a SIGKILLed child's pipes without leaking fds or hijacking the result.

    ``communicate(timeout=5)`` can ITSELF raise ``TimeoutExpired``: the child
    outlived the group kill (uninterruptible I/O, or no pgid resolved so only
    ``proc.kill()`` was tried), or another process inherited the write end of
    the pipe and holds it open, so EOF never arrives. Waiting longer cannot help
    once SIGKILL has been sent, and the caller has already decided the outcome —
    so swallow that one exception rather than letting it displace the caller's
    ``raise`` / ``return``. Closing the pipes has to happen either way:
    ``Popen._communicate`` closes them as a side effect of reaching EOF, which
    is exactly the path not taken here, and nothing else ever closes them.
    """
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        logger.warning(
            "Post-kill drain timed out (5s) for cron %s (pid %s); the child "
            "outlived SIGKILL or another process holds the pipe — closing pipes",
            job_id, proc.pid,
        )
    finally:
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass


if TYPE_CHECKING:
    from kiro_crew.cron import CronJob

logger = logging.getLogger(__name__)


class SkipError(Exception):
    """Abort this tick silently. Cron fires again next interval."""


class DoneError(Exception):
    """Complete the cron job. Job is removed from the schedule.

    Use ctx.notify() before raising Done() to deliver a message.
    """

    def __init__(self, message: str = ""):
        self.message = message
        super().__init__(message)


class ReportError(Exception):
    """Deliver a message but keep the job running.

    Use for long-lived monitors that need to report multiple times.
    """

    def __init__(self, message: str = ""):
        self.message = message
        super().__init__(message)


# Backward-compat aliases: Skip/Done/Report are the public API used by
# user-authored cron scripts. Renamed to *Error for flake8 N818; aliases
# preserve the existing import/raise surface with zero behavior change.
Skip = SkipError
Done = DoneError
Report = ReportError


@dataclass
class ScriptContext:
    """Passed to script functions. Provides delivery and tool access."""

    job: CronJob
    _port: int = 5476
    _secret: str = ""

    def __post_init__(self) -> None:
        # The parent injects the port it minted the credential for. Preferring it
        # keeps credential and dial target from one resolution; KIROCREW_PORT is the
        # fallback for a directly-constructed context and is 5476 on a --port auto
        # gateway, which is a SIBLING rather than this instance.
        self._port = int(
            os.environ.pop("_KIROCREW_DIAL_PORT", "")
            or os.environ.get("KIROCREW_PORT", "5476")
        )
        # Secret injected via temp file (not inherited env) to prevent privilege escalation.
        # Pop env var and unlink file immediately so fn(ctx) cannot access the secret directly.
        secret_file = os.environ.pop("_KIROCREW_SECRET_FILE", "")
        if secret_file and Path(secret_file).exists():
            self._secret = Path(secret_file).read_text()
            try:
                Path(secret_file).unlink()
            except OSError:
                pass
        else:
            self._secret = os.environ.pop("KIROCREW_INTERNAL_SECRET", "")

    @property
    def message(self) -> str:
        """The cron job's message field (used to pass args to scripts)."""
        return getattr(self.job, "message", "")

    def notify(self, text: str, **kwargs: Any) -> dict:
        """Send a message via the gateway (same as send_message MCP tool).

        Raises RuntimeError if delivery fails.
        """
        safe_text = redact(text)
        # Redact kwargs values
        kwargs_str = json.dumps(kwargs) if kwargs else "{}"
        kwargs_str = redact(kwargs_str)
        safe_kwargs = json.loads(kwargs_str) if kwargs else {}
        payload: dict[str, Any] = {"text": safe_text, **safe_kwargs}
        # caller_session lets session="origin" resolve to the chat that created this
        # cron; hard-assigned (not setdefault) so a script cannot spoof another session
        payload["caller_session"] = f"cron:{self.job.id}"
        result = self._post("/api/send-message", payload)
        if "error" in result:
            raise RuntimeError(f"notify() failed: {result['error']}")
        return result

    def call_tool(self, server: str, tool: str, args: dict) -> str:
        """Call an MCP tool by spawning the server subprocess directly.

        Args are scanned for credential/URL leakage before passing to the
        sandboxed MCP server subprocess.
        """
        # Scan serialized args for credential patterns
        args_str = json.dumps(args)
        args_str = redact(args_str)
        safe_args = json.loads(args_str)
        client = None
        try:
            client = McpToolClient(server)
            result = client.call_tool(tool, safe_args)
            self._audit_tool_call(server, tool, "ok")
            return result
        except Exception as exc:
            self._audit_tool_call(server, tool, "error", str(exc))
            raise
        finally:
            if client is not None:
                client.close()

    def _audit_tool_call(self, server: str, tool: str, outcome: str, error: str = "") -> None:
        """Log tool invocation for audit trail."""
        logger.info(
            "cron_script tool_call: job=%s server=%s tool=%s outcome=%s%s",
            self.job.id,
            server,
            tool,
            outcome,
            f" error={error}" if error else "",
        )
        try:
            sel().log_tool_invocation(
                session_key=f"cron:{self.job.id}",
                tool_name=f"{server}/{tool}",
                tool_kind="cron_script_tool",
                outcome=outcome,
                error=error,
            )
        except Exception:
            logger.debug("SEL audit logging failed in cron_script tool call", exc_info=True)

    def _post(self, path: str, body: dict) -> dict:
        data = json.dumps(body).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Secret": self._secret,
            "X-Session-Key": f"cron:{self.job.id}",
        }
        req = urllib.request.Request(
            f"http://localhost:{self._port}{path}",
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with loopback_urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            logger.warning("ScriptContext._post(%s) failed: %s", path, exc)
            return {"error": str(exc)}


# ── MCP Tool Bridge ──


class McpToolClient:
    """Minimal MCP JSON-RPC client. Spawns server subprocess, calls tool, closes."""

    def __init__(self, server_name: str):
        self._server_name = server_name
        resolved = _resolve_mcp_server(server_name)
        if not resolved:
            raise RuntimeError(f"MCP server '{server_name}' not found in agent config")
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
        #     `kirocrew-cron` itself, and mcp_cron._caller_is_cli() is just
        #     `os.environ.get("KIROCREW_CLI") == "1"` -- so a `KIROCREW_CLI: "1"`
        #     in that server's `env` block would make the spawned server treat a
        #     SCRIPT CRON as the admin CLI and let it run cron_remove_all across
        #     every session. Sandboxing does not bound that: confinement limits what
        #     the child may touch, not whose jobs Kiro Crew thinks it owns. The deny
        #     lives in the shared sanitizer rather than in _CRON_ENV_DENY so the
        #     discovery probe -- which applies no cron deny-set at all -- is covered
        #     by the same control instead of a second copy of it.
        proc_env = _clean_cron_env()
        proc_env.update(
            sanitize_spec_env((k, v) for k, v in spec_env.items() if k not in _CRON_ENV_DENY)
        )
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
                text=True,
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
        self._req_id = 0
        try:
            self._rpc(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "kirocrew-cron-script", "version": "0.1"},
                },
            )
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def _send(self, msg: dict) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()

    def _recv(self) -> dict | None:
        assert self._proc.stdout is not None
        while True:
            line = self._proc.stdout.readline()
            if not line:  # EOF
                return None
            if line.strip():
                return json.loads(line)

    def _stderr_tail(self, limit: int = 1024) -> str:
        """Return the last `limit` bytes of the subprocess's captured stderr.

        Credentials and exfiltration URLs are redacted before the tail is
        surfaced in an error so a failing spawn (e.g. an auth dump or an
        attacker-controlled MCP server) can't leak secrets or beacon URLs
        into logs, Slack, or the dashboard.
        """
        path = getattr(self, "_stderr_file", None)
        if path is None:
            return ""
        try:
            with open(path.name, errors="replace") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - limit))
                return redact(fh.read().strip())
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
        raise RuntimeError(
            f"MCP server '{name}' did not respond to '{method}' within 1000 messages"
        )

    def call_tool(self, name: str, arguments: dict) -> str:
        r = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if "error" in r:
            raise RuntimeError(f"MCP tool error: {r['error']}")
        result = r.get("result", {})
        if result.get("isError"):
            content = result.get("content", [])
            err_text = content[0].get("text", "unknown error") if content else "unknown error"
            raise RuntimeError(f"MCP tool error: {err_text}")
        content = result.get("content", [])
        return content[0].get("text", "") if content else ""

    def close(self) -> None:
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        except Exception:
            pass
        finally:
            stderr_file = getattr(self, "_stderr_file", None)
            if stderr_file is not None:
                try:
                    stderr_file.close()
                except Exception:
                    pass
                Path(stderr_file.name).unlink(missing_ok=True)
            if self._sandbox_cleanup:
                Path(self._sandbox_cleanup).unlink(missing_ok=True)


@lru_cache(maxsize=16)
def _resolve_mcp_server(name: str) -> tuple[tuple[str, ...], dict[str, str]] | None:
    """Read MCP server command + env from agent config (cached per process).

    Returns ``(argv, env)``. The per-server ``env`` block is required for
    launchers that shell out to a helper binary only reachable via the ``PATH``
    the config supplies: dropping it makes the spawned MCP die at the JSON-RPC
    ``initialize`` handshake. When the config has no ``env`` block -- or declares
    one that is not a JSON object -- the env dict is empty.
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
    # a bare ``read_text`` + ``json.loads``. ``None`` covers every unusable file
    # -- including the malformed-JSON and non-UTF-8 cases the old form let
    # escape as an unhandled JSONDecodeError / UnicodeDecodeError into the cron
    # runner -- and degrades to "no such server", the same answer an absent
    # entry already produced.
    cfg = _read_agent_spec(
        cfg_path,
        operation="cron_resolve_mcp_server",
        source="cron",
    )
    if cfg is None:
        return None
    spec = cfg.get("mcpServers", {}).get(name)
    if not spec:
        return None
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


def _split_script_spec(script_path: str) -> tuple[str, str]:
    """Split a ``"<path>:<func>"`` spec into ``(path, func)``, drive-aware.

    Splits on the LAST colon. A Windows drive letter adds a second colon at
    index 1 (``C:\\...``); taking the rightmost colon keeps the whole drive path
    and the trailing func (``C:\\crons\\job.py:run`` -> ``C:\\crons\\job.py`` +
    ``run``). The only ambiguous input is a bare drive path with no ``:func``
    suffix, which would otherwise split at the drive colon into the nonsense
    ``("C", "\\crons\\job.py")`` — so a colon that IS the drive colon does not
    count as the separator.
    """

    drive_colon = len(script_path) >= 2 and script_path[1] == ":" and script_path[0].isalpha()
    func_colon = script_path.rfind(":")
    if func_colon == -1 or (drive_colon and func_colon == 1):
        raise ValueError(f"Invalid script path '{script_path}': expected 'path.py:func'")
    return script_path[:func_colon], script_path[func_colon + 1:]


def resolve_script_path(script_path: str) -> tuple[str, str]:
    """Validate and resolve a script path. Returns (file_path, func_name).

    Scripts must be files under ``<config_dir>/crons/``.
    Format: "<config_dir>/crons/file.py:function" or "/absolute/path.py:function"
    """
    module_part, func_name = _split_script_spec(script_path)

    file_path = Path(os.path.expanduser(module_part)).resolve()
    if not file_path.exists():
        raise FileNotFoundError(f"Script file not found: {file_path}")
    if is_sensitive_path(str(file_path)):
        raise PermissionError(f"Script path blocked by security policy: {file_path}")
    allowed_dir = (config_dir() / "crons").resolve()
    if not file_path.is_relative_to(allowed_dir):
        raise PermissionError(f"Script must be under {allowed_dir}, got: {file_path}")
    return str(file_path), func_name


def _resolve_internal_secret(port: int) -> str:
    """Internal secret for ScriptContext HTTP calls (e.g. notify -> /api/send-message).

    The gateway generates its secret at startup and publishes it per listener as
    ``run/gateway-<port>.secret`` (with ``config_dir()/.local_secret`` as the
    home-wide fallback); the ``KIROCREW_INTERNAL_SECRET`` env var is normally unset,
    so fall back to the file via the shared ``config.loader.read_local_secret``
    helper (single home for that read). Without this the sandbox sends an empty
    ``X-Internal-Secret`` and every code-cron notify gets HTTP 403.

    Takes the ALREADY-RESOLVED dial port rather than resolving its own. The caller
    resolves the port ONCE and passes the same value here and into
    ``_KIROCREW_DIAL_PORT``. Resolving twice -- once for the credential, once for the
    child -- is a TOCTOU: a ``--port auto`` gateway that binds between the two calls
    would mint the credential for one port and tell the child to dial another, and
    the mismatched credential 403s the callback. One resolution makes that
    unrepresentable, which is what the ``_KIROCREW_DIAL_PORT`` mechanism promised.
    """
    env_secret = os.environ.get("KIROCREW_INTERNAL_SECRET", "")
    if env_secret:
        return env_secret
    return read_local_secret(port)


def _resolve_dial_port() -> int:
    """The ONE port this cron dials, used for both the credential and the child.

    The parent mints the credential and the child sends it, so a second independent
    resolution in the child is exactly how the two diverge: ``ScriptContext`` reads
    ``KIROCREW_PORT``, which is 5476 on a ``--port auto`` gateway, while the parent
    would have minted for the real ephemeral port -- credential for one gateway,
    request to another. One resolution, injected as ``_KIROCREW_DIAL_PORT``, makes
    that mismatch unrepresentable.

    Delegates to :func:`resolve_serving_port`, the shared gateway-side resolver that
    prefers ``KIROCREW_BOUND_PORT`` over an inherited ``KIROCREW_PORT`` -- the cron
    scheduler runs inside the gateway, so the bound port is ground truth and a
    sibling-naming ``KIROCREW_PORT`` must not win.
    """
    return resolve_serving_port()


def run_script_sandboxed(
    script_path: str, job_id: str, job_message: str = "", timeout: int = 30
) -> dict:
    """Run a cron script in a sandboxed subprocess via wrap_argv().

    Returns: {"status": "ok"|"skip"|"done"|"error", "message": "...", "error": "..."}
    """

    file_path_str, func_name = resolve_script_path(script_path)

    launcher = (
        # Import sys first (builtin, unshadowable) and strip the launcher's own
        # /tmp dir from sys.path[0] before importing json/os/types/kiro_crew —
        # otherwise a stray sibling like /tmp/struct.py or /tmp/os.py shadows the
        # stdlib and crashes the cron launcher on startup. The user-script dir is
        # re-added explicitly below, after the strip.
        "import sys\n"
        "sys.path[:] = [p for p in sys.path if p not in ('', sys.path[0])]\n"
        "import json, os, types\n"
        "from kiro_crew.config.loader import KiroCrewConfig\n"
        "from kiro_crew.platform.bootstrap import boot_platform\n"
        "boot_platform(KiroCrewConfig.load())\n"
        "from kiro_crew.cron_script import ScriptContext, Skip, Done, Report\n"
        f"sys.path.insert(0, os.path.dirname({file_path_str!r}))\n"
        f"mod = types.ModuleType('_cron_script')\n"
        f"mod.__file__ = {file_path_str!r}\n"
        f"with open({file_path_str!r}) as f:\n"
        f"    exec(compile(f.read(), {file_path_str!r}, 'exec'), mod.__dict__)\n"
        f"fn = getattr(mod, {func_name!r}, None)\n"
        "if fn is None:\n"
        f"    print(json.dumps({{'status': 'error', 'error': 'Function not found'}}))\n"
        "    sys.exit(0)\n"
        f"job = types.SimpleNamespace(id={job_id!r}, message={job_message!r})\n"
        "ctx = ScriptContext(job=job)\n"
        "try:\n"
        "    fn(ctx)\n"
        "    print(json.dumps({'status': 'ok'}))\n"
        "except Skip:\n"
        "    print(json.dumps({'status': 'skip'}))\n"
        "except Done as d:\n"
        "    print(json.dumps({'status': 'done', 'message': d.message}))\n"
        "except Report as r:\n"
        "    print(json.dumps({'status': 'report', 'message': r.message}))\n"
        "except Exception as e:\n"
        "    print(json.dumps({'status': 'error', 'error': str(e)}))\n"
    )

    fd, launcher_path = tempfile.mkstemp(suffix=".py", prefix="kirocrew_cron_")
    sandbox_cleanup: str | None = None
    # Resolve the dial port ONCE: the credential written below and the
    # _KIROCREW_DIAL_PORT the child dials must come from the same resolution, or a
    # --port auto bind between two resolutions would pair a credential with the
    # wrong port and 403 the callback.
    dial_port = _resolve_dial_port()
    # Write secret to temp file for ScriptContext (scrubbed from env)
    secret_fd, secret_path = tempfile.mkstemp(prefix="kirocrew_secret_")
    try:
        try:
            # Tighten the DACL BEFORE writing the secret bytes so the file is
            # never on disk under the parent-inherited %TEMP% DACL on Windows.
            # On POSIX mkstemp already births the file 0600 so ordering is a
            # no-op; on Windows mkstemp cannot set an owner-only DACL, so the
            # interval between create and lockdown is a real window
            # if we wrote first. Matches the fail-loud convention of the other
            # internal-secret writers (token_secret, refresh_tokens, snapshot,
            # server._write_secret_file, token_auth) — chmod_safe swallows
            # OSError and would hide a lockdown failure. Both calls stay inside
            # the outer try so a lockdown failure still hits the finally that
            # unlinks the secret + launcher (otherwise the fd leaks and temp
            # files persist).
            platform_compat.restrict_to_owner(secret_path)
            os.write(secret_fd, _resolve_internal_secret(dial_port).encode())
        finally:
            os.close(secret_fd)
        try:
            os.write(fd, launcher.encode())
        finally:
            os.close(fd)

        argv = [sys.executable, launcher_path]
        sandboxed_argv, sandbox_cleanup = wrap_argv(argv, mode="standard")

        # Build clean env: secrets (Slack tokens, owner id, internal secret)
        # are never inherited; the internal secret is passed via the 0600 file.
        clean_env = _clean_cron_env()
        clean_env["_KIROCREW_SECRET_FILE"] = secret_path
        # The child must dial the gateway the credential above was minted for:
        # same dial_port, resolved once above, not a second resolution here.
        clean_env["_KIROCREW_DIAL_PORT"] = str(dial_port)
        # Pre-resolve gh OUTSIDE the sandbox and pin its identity for the
        # child: the sandbox's single-uid user namespace maps every root-owned
        # path component to the overflow uid, so the child's own ownership
        # walk refuses ANY gh on the host. Empty when the host has no usable
        # gh -- scripts that never call gh are unaffected either way.
        clean_env.update(prevalidated_gh_env())

        sandboxed_argv = cgroup_scope_argv(sandboxed_argv)  # cgroup DoS ceiling
        proc = popen_limited(
            sandboxed_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=clean_env, start_new_session=True,
        )
        _register_proc(job_id, proc)
        try:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Popen.communicate does not kill the child on timeout
                # (unlike subprocess.run) — clean up before re-raising.
                _kill_proc_group(proc)
                _drain_after_kill(proc, job_id)
                raise
        finally:
            cancelled = _unregister_proc(job_id, proc)
        if cancelled:
            return {"status": "cancelled", "error": "Cancelled by user"}

        if proc.returncode != 0 and not stdout.strip():
            # Report the TERMINAL stderr context, not the head. A process that
            # dies hard leaves its diagnosis LAST -- the traceback is the final
            # thing written -- while whatever a startup path logged first (a
            # data-home migration warning, a deprecation notice, an import
            # banner) sits in front of it. Bounding from the head therefore
            # reports the noise and truncates the cause, and the operator reads
            # a cron failure whose message describes something that did not kill
            # the job.
            #
            # ``rstrip`` first so a trailing newline does not spend part of the
            # budget, and so an all-whitespace stderr still falls through to the
            # exit-code fallback rather than reporting blank text.
            #
            # Redact the WHOLE stream before bounding: slicing first would cut
            # a credential that straddles the 500-char boundary in half, and
            # ``redact`` cannot recognise the surviving fragment, so it would
            # reach logs and the persisted ``last_error`` unmasked.
            tail = redact(stderr.rstrip())
            error_text = tail[-500:] if tail else f"exit {proc.returncode}"
            return {"status": "error", "error": error_text}

        try:
            return json.loads(stdout.strip().split("\n")[-1])
        except (json.JSONDecodeError, IndexError):
            return {
                "status": "error",
                # Redact the complete stdout BEFORE truncating: slicing first
                # could cut a credential at the boundary, leaving its unredacted
                # head in the diagnostic.
                "error": f"Bad output: {redact(stdout)[:200]}",
            }
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": f"Script timed out after {timeout}s"}
    except SandboxUnavailableError as exc:
        # Same reasoning as run_command_sandboxed: a host with no sandbox backend
        # must surface a failed job carrying the remedy, not an escaping
        # exception the scheduler cannot attribute to this job.
        return {"status": "error", "error": f"{_SANDBOX_UNAVAILABLE_PREFIX}{exc}"}
    finally:
        Path(launcher_path).unlink(missing_ok=True)
        Path(secret_path).unlink(missing_ok=True)
        if sandbox_cleanup:
            Path(sandbox_cleanup).unlink(missing_ok=True)


_MAX_COMMAND_OUTPUT = 65536  # 64KB cap

# Leads a cron failure caused by the fail-closed sandbox rather than by the job
# itself. The distinction matters to the reader: the job is fine, the host cannot
# isolate it, and the remedy is a config opt-in — which the wrapped message
# carries verbatim.
_SANDBOX_UNAVAILABLE_PREFIX = "❌ Cron could not run in an OS sandbox: "


def _resolve_command_shell() -> str | None:
    """Return an absolute path to a POSIX shell for ``sh -c`` command crons.

    Command crons are authored as POSIX shell one-liners (and vetted by
    ``mcp_cron._vet_shell_command`` under POSIX quoting), so cmd.exe is NOT a
    substitute — a missing shell must fail loudly rather than silently changing
    the command language. Returns ``None`` when no POSIX-strict shell can be
    found, which makes the caller refuse the job with a legible error.

    Windows always returns ``None``: neither shipped option preserves the
    language the vet gate assumed. Verified on a Git-for-Windows install —
    ``sh.exe -c 'echo test.a{w,w}s'`` prints ``test.aws test.aws``, i.e. it IS
    bash and performs brace expansion, which is exactly how
    ``cat ~/.a{w,w}s/credentials`` hides a credential path from a gate that
    tokenizes the stored string once. Command crons are therefore unavailable on
    Windows by design; ``script`` crons (whose body is scanned in full) are the
    supported path there.
    """

    # On Windows there is no shipped shell whose language matches what
    # mcp_cron._vet_shell_command was written against: cmd.exe is not POSIX at
    # all, and Git-for-Windows's sh.exe IS bash. Refuse rather than route the
    # vetted string through a shell that widens its language.
    if platform_compat.IS_WINDOWS:
        return None
    # POSIX: NEVER consult PATH (shutil.which("sh")). PATH may contain an
    # agent-writable directory that precedes /bin — an agent can plant
    # ``~/.local/bin/sh`` that fingerprints the probe input, passes the strict
    # test, then does something different when called with the real cron. Even
    # inside `cc`-mode isolation the agent-planted binary runs, and `cc` leaves
    # ``.ssh`` reachable, so this is a private-key-exposure vector. Instead,
    # walk a small fixed list of trusted system shell paths (never a bash
    # fallback: bash brace expansion hides `cat ~/.a{w,w}s/credentials` from
    # the tokenizer). Some hosts (macOS /bin/sh is bash-in-POSIX-mode) still
    # perform brace expansion under the `sh` name, so PROBE the candidate:
    # dash / ash / a real POSIX sh preserve the literal; bash-in-any-mode fails.
    for candidate in ("/bin/sh", "/usr/bin/sh"):
        if os.path.isfile(candidate) and _shell_is_posix_strict(candidate):
            return candidate
    return None


# Per-shell-path memoization for the POSIX-strict probe. The probe itself
# spawns a child, so caching it means each candidate is fingerprinted at most
# once per gateway process; a subsequent command cron with the same resolved
# shell does no extra work.
_POSIX_STRICT_CACHE: dict[str, bool] = {}


def _shell_is_posix_strict(shell: str) -> bool:
    """Return True iff *shell* refuses brace expansion (POSIX-sh semantics).

    Runs ``<shell> -c 'echo x.{a,a}'`` in an OS sandbox (strict tier, cron env)
    and requires the OUTPUT to be the literal ``x.{a,a}``. dash / ash / a real
    POSIX sh preserve it; bash (including macOS's ``/bin/sh`` which is
    bash-in-POSIX-mode) expands to ``x.a x.a``. Refusing an expanding shell is
    the only reliable defense: the vet gate (``mcp_cron._vet_shell_command``)
    tokenizes the stored string once, so any downstream re-expansion silently
    widens what a legitimate deny-list can see.

    The probe is SANDBOX-ROUTED as a defense-in-depth belt on the fixed
    trusted-path lookup in ``_resolve_command_shell``. If a future change ever
    widens that resolver to consult PATH again, the sandbox wrap here still
    denies an agent-planted shim the un-isolated execution it would need.
    """

    cached = _POSIX_STRICT_CACHE.get(shell)
    if cached is not None:
        return cached
    sandbox_cleanup: str | None = None
    try:
        argv, sandbox_cleanup = wrap_argv(
            [shell, "-c", "echo x.{a,a}"], mode="strict"
        )
        # Same discipline as every other sandbox-routed spawn in this module
        # (test_every_routed_spawn_applies_resource_limits / _cgroup_scope): the
        # probe is a child process, so it observes the same fork-bomb / RSS
        # ceilings as a real command cron. run_limited applies them after exec,
        # and is a no-op on Windows where there are no POSIX rlimits.
        argv = cgroup_scope_argv(argv)
        proc = run_limited(
            argv,
            capture_output=True,
            text=True,
            timeout=5,
            env=_clean_cron_env(),
        )
        result = proc.returncode == 0 and proc.stdout.strip() == "x.{a,a}"
    except (OSError, subprocess.SubprocessError, SandboxUnavailableError):
        result = False
    finally:
        if sandbox_cleanup:
            try:
                os.unlink(sandbox_cleanup)
            except OSError:
                pass
    _POSIX_STRICT_CACHE[shell] = result
    return result


def run_command_sandboxed(command: str, timeout: int = 300, job_id: str | None = None) -> dict:
    """Run a shell command in a sandboxed subprocess via wrap_argv().

    Returns: {"status": "ok"|"error"|"cancelled", "output": "...", "exit_code": N}
    """
    shell = _resolve_command_shell()
    if shell is None:
        return {
            "status": "error",
            "output": (
                "❌ No POSIX shell available to run this command cron. Command "
                "crons execute with `sh -c` under POSIX-sh semantics (what the "
                "storage-time vet gate assumes); Windows ships no such shell "
                "(Git for Windows's sh.exe is bash and would widen the language "
                "past the vet). Use a script cron or an LLM `message` cron on "
                "this platform, or run the gateway under POSIX."
            ),
            "exit_code": -1,
        }
    argv = [shell, "-c", command]
    # mode="cc" (not "standard"): the command string is fully model-supplied via
    # cron_add and executes outside the kiro-cli ACP permission/hook flow, so this
    # is a low-trust exec path. "cc" hides the credential dirs/files (.aws, .kube,
    # .netrc, .git-credentials, .npmrc, .pypirc, .kirocrew/.env) and scrubs the
    # agent-denied env keys, while deliberately leaving ~/.ssh reachable so a
    # legitimate command cron can still do git/scp/rsync over SSH. "strict" would
    # additionally hide ~/.ssh but break those workflows; the residual .ssh
    # exposure is covered by the storage-time deny-list (mcp_cron._vet_shell_command,
    # which blocks any .ssh reference) — the primary control. This sandbox is
    # defense-in-depth and is bypassed when the OS backend falls back to "none"
    # (e.g. macOS >= 26 — see _clean_cron_env).
    #
    # wrap_argv is INSIDE the try: on a host with no OS sandbox backend (every
    # Windows host) it fail-closes by raising, and outside the try that escaped
    # this function entirely — the scheduler's caller saw a bare exception
    # instead of a job it could mark failed, so the remedy never reached the user.
    sandbox_cleanup: str | None = None
    try:
        sandboxed_argv, sandbox_cleanup = wrap_argv(argv, mode="cc")
        sandboxed_argv = cgroup_scope_argv(sandboxed_argv)  # cgroup DoS ceiling
        clean_env = _clean_cron_env()
        proc = popen_limited(
            sandboxed_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=clean_env, start_new_session=True,
        )
        if job_id:
            _register_proc(job_id, proc)
        cancelled = False
        try:
            try:
                output, stderr_out = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_proc_group(proc)
                _drain_after_kill(proc, job_id)
                return {"status": "error", "output": f"❌ Command timed out after {timeout}s", "exit_code": -1}
        finally:
            if job_id:
                cancelled = _unregister_proc(job_id, proc)
        if cancelled:
            return {"status": "cancelled", "output": "Cancelled by user", "exit_code": proc.returncode}
        if len(output) > _MAX_COMMAND_OUTPUT:
            output = output[:_MAX_COMMAND_OUTPUT] + "\n\n[truncated — output exceeded 64KB]"
        if proc.returncode != 0:
            output = f"⚠️ Exit code {proc.returncode}\n\n{output}"
            if stderr_out:
                # A command that dies hard leaves its diagnosis last: report the
                # stderr tail, not the head, so a chatty startup warning can't
                # displace the terminal error. Redact the complete stderr BEFORE
                # truncating: slicing first could cut off a credential's
                # detectable prefix (e.g. the scheme of a token-bearing URL),
                # letting the raw secret tail through redaction.
                output += f"\n\nstderr:\n{redact(stderr_out.rstrip())[-1000:]}"
        return {
            "status": "ok" if proc.returncode == 0 else "error",
            "output": output,
            "exit_code": proc.returncode,
        }
    except SandboxUnavailableError as exc:
        return {
            "status": "error",
            "output": f"{_SANDBOX_UNAVAILABLE_PREFIX}{exc}",
            "exit_code": -1,
        }
    except Exception as exc:
        return {"status": "error", "output": f"❌ Command failed: {exc}", "exit_code": -1}
    finally:
        if sandbox_cleanup:
            Path(sandbox_cleanup).unlink(missing_ok=True)
