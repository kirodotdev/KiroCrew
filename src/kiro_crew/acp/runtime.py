"""ACP Runtime for multiplexed kiro-cli sessions.

Single-reader demux architecture: one AcpRuntime owns the subprocess and a
reader task that routes frames by sessionId to per-session queues. Each
``AcpSessionHandle`` (in ``session_handle.py``) owns one sessionId + queue and
provides the prompt/cancel/approve/reject API.

The per-session handle, the runtime protocol it depends on, and the runtime
exceptions live in ``session_handle.py`` (the lower layer); they are re-exported
here so ``from kiro_crew.acp.runtime import AcpSessionHandle`` (and the
exceptions) keeps working for existing callers and tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.acp._dispatch import (
    build_session_new_params,
    parse_session_modes,
    set_mode_params,
)
from kiro_crew.acp.client import (
    _NOT_LOGGED_IN_RE,
    _get_start_time,
    _KiroExecutableTrustError,
    _resolve_kiro_bin_for_spawn,
)
from kiro_crew.acp.session_handle import (
    AcpRuntimeDead,
    AcpRuntimeError,
    AcpRuntimeProtocol,
    AcpSessionHandle,
)
from kiro_crew.acp.types import (
    ACP_CLIENT_CAPABILITIES,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SESSION_TERMINATE,
    METHOD_SET_MODE,
    JsonRpcMessage,
    JsonRpcRequest,
)
from kiro_crew.agent import ensure_agent_materialized
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.constants import KIROCREW_SPAWNED_ENV, KIROCREW_SPAWNED_VALUE
from kiro_crew.env import augmented_path, resolve_krb5_ccname
from kiro_crew.executors import subprocess_executor
from kiro_crew.mcp_gateway.session_servers import pooled_session_servers
from kiro_crew.sandbox import (
    RLIMIT_PROFILE_SESSION_HOST,
    cgroup_scope_argv,
    create_subprocess_limited,
    scrub_agent_denied_env,
    wrap_argv,
)
from kiro_crew.session_pid import (
    _track_pid,
    _track_session_pid,
    _untrack_pid,
    _untrack_session_pid,
    register_protected_pid,
    unregister_protected_pid,
)
from kiro_crew.validation import MODEL_ID_RE

logger = logging.getLogger(__name__)

__all__ = [
    "AcpRuntime",
    "AcpRuntimeError",
    "AcpRuntimeDead",
    "AcpRuntimeProtocol",
    "AcpSessionHandle",
]


# ── AcpRuntime ──

_STDOUT_BUFFER_LIMIT = 10 * 1024 * 1024  # 10MB
_INIT_TIMEOUT = 30.0
_REQUEST_TIMEOUT = 30.0
_INIT_NOTIFICATION_BUFFER_LIMIT = 100
# Teardown must be snappy: a session is usually terminated on a hot path
# (background task done, subagent reaped). kiro-cli's terminate handler responds
# as soon as it enqueues the eviction (the actual shutdown runs in its actor
# loop), so a healthy runtime acks well under this bound; a slow/dead one must
# not turn teardown into a multi-second stall.
_TERMINATE_TIMEOUT = 5.0

# Default recycling thresholds for long-lived multiplexed runtimes (see
# _is_stale()). These are conservative defaults chosen to recycle well before
# the unbounded growth observed in production (multi-GB RSS after ~24h of
# uptime with no per-turn compaction) while still amortizing process-spawn
# cost across many background prompts.
_DEFAULT_MAX_AGE_SECS = 6 * 3600  # 6 hours
_DEFAULT_MAX_RSS_MB = 500.0  # 500 MiB

# Below this uptime the RSS staleness probe is skipped entirely (see
# _is_stale()). A freshly-(re)used runtime has not had time to grow, so this
# keeps the hot get_bg_session reuse path — which holds _bg_runtime_lock —
# CPU-only for young runtimes and only pays the offloaded RSS probe once a
# runtime has lived long enough to plausibly have ballooned.
_RSS_PROBE_MIN_AGE_SECS = 300.0  # 5 minutes

# ── Awaited-request error formatting ──
#
# kiro-cli returns this when session/set_mode names an agent it cannot resolve,
# i.e. no ``<name>.json`` in its agents directory. The wire shape is a bare
# -32603 "Internal error", so nothing about the frame itself says "missing file".
# The name charset is bounded to what a real spec filename can hold (see
# validation of agent names elsewhere) rather than a greedy match, so a hostile
# or malformed backend string is not echoed back into a user-facing message.
_MODE_NOT_FOUND_RE = re.compile(r"""Mode ['"](?P<name>[A-Za-z0-9._-]{1,64})['"] not found""")


def _format_runtime_rpc_error(error: object) -> str:
    """Format an awaited-request JSON-RPC error into user-facing text.

    Awaited requests are the handshake ones — ``initialize``, ``session/new``,
    ``session/set_mode`` — so this is NOT the same population as
    ``client._format_acp_error``, which rewrites PROMPT-time provider failures
    (throttling, auth, 5xx) and has no branch that matches a missing agent spec.
    The two are deliberately separate rather than merged: their inputs come from
    different protocol phases and share no shape.

    Exactly one shape is rewritten today: a missing agent spec. Left raw it
    surfaces to the user as ``RPC error: {'code': -32603, 'message': 'Internal
    error', 'data': "Mode 'kirocrew' not found"}`` — which names an internal ACP
    concept, reads as a backend bug, and hides that the cause is a local file and
    the fix is one command. Every other shape falls through to the raw dict, so a
    shape nobody has classified is surfaced rather than swallowed.
    """
    if isinstance(error, dict):
        match = _MODE_NOT_FOUND_RE.search(str(error.get("data", "") or ""))
        if match:
            name = match.group("name")
            return (
                f"Agent spec '{name}' is not installed: kiro-cli found no "
                f"'{name}.json' in {kiro_agents_dir()}. Every turn fails until it "
                f"is restored — repair with `kirocrew setup --agent-only --clean`, "
                f"then restart the gateway."
            )
    return f"RPC error: {error}"


# ── Unroutable-frame drop accounting ──
#
# The reader drops any frame it cannot route (see _reader_loop). That is
# CORRECT behaviour, but logging it per frame is not: kiro-cli is multiplexed,
# so every frame for a torn-down or not-yet-registered sessionId takes the drop
# branch, and a backend that keeps streaming after teardown makes that an
# unbounded STEADY STATE, not a burst. Measured on an operator host: ~60
# lines/second for 6+ hours from one gateway PID, taking 33–59% of every
# gateway.log rotation — which, at RotatingFileHandler(maxBytes=2MB,
# backupCount=3) (see cli.py), rolls the genuine diagnostics needed for an
# incident out of the retained 8MB window before anyone can read them.
#
# So the per-frame line is collapsed into a periodic count keyed by
# (sessionId, method). The key must stay PER SESSION: the incident's decisive
# signal was that two DIFFERENT session UUIDs were flooding at once, which a
# single global tally would hide.
_DROP_SUMMARY_INTERVAL_SECS = 60.0
# Hard cap on distinct (sessionId, method) keys held between flushes. Both
# halves of the key are backend-controlled, so an unbounded map would be a
# memory sink; reaching the cap forces an early flush instead of growing.
_DROP_SUMMARY_MAX_KEYS = 64
# Backend-controlled key text is truncated before it is stored, so a
# pathological sessionId/method (a stdout line may be up to
# _STDOUT_BUFFER_LIMIT) cannot be retained at full length by the map either.
_DROP_SUMMARY_KEY_MAX_CHARS = 80
# Stands in for the sessionId half of the key on the no-sessionId broadcast
# path, which has no session to name.
_DROP_NO_SESSION = "-"
# Stands in for EITHER half of the key when the backend supplied no usable
# string: an absent `method`, or a value of the wrong JSON type (see
# _drop_key_part).
_DROP_KEY_PLACEHOLDER = "?"

KIRO_CLI_BIN = "kiro-cli"
KIRO_CLI_SUBCMD = "acp"
CLIENT_NAME = "kirocrew"
CLIENT_VERSION = "0.1.2"
PROTOCOL_VERSION = "2025-08-22"


def _drop_key_part(value: object) -> str:
    """Bounded, hashable string for one half of a (sessionId, method) drop key.

    BOTH halves arrive verbatim from backend JSON: `JsonRpcMessage.from_dict`
    copies `method` and `params` with no validation, so the `str | None`
    annotation is documentation, not enforcement — `{"method": 123}` yields an
    int, and `params.sessionId` is `Any`. Slicing such a value raises TypeError
    *inside* `_reader_loop`, the single owner of this process's stdout, which
    marks the runtime dead and tears down EVERY multiplexed session on it. One
    malformed frame must not cost every session, so anything that is not a
    `str` (including the legitimate absent-`method` `None`) becomes the
    placeholder before it is sliced or used as a dict key.
    """
    if not isinstance(value, str):
        return _DROP_KEY_PLACEHOLDER
    return value[:_DROP_SUMMARY_KEY_MAX_CHARS]


def _get_rss_mb(pid: int) -> float | None:
    """Get resident set size (RSS) of a process in MiB, or None if unavailable.

    Linux: reads /proc/<pid>/status. macOS (no /proc): shells out to
    ``ps -o rss= -p <pid>`` (ps reports RSS in KiB on both platforms).
    Returns None on any failure (missing /proc, permission error, process
    gone, ps not found) so callers can treat "unknown" the same as "not over
    threshold" rather than raising.
    """
    if sys.platform == "linux":
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        # Format: "VmRSS:\t   123456 kB"
                        parts = line.split()
                        return int(parts[1]) / 1024.0
        except (OSError, IndexError, ValueError):
            return None
        return None

    if platform_compat.IS_WINDOWS:
        # No `ps` on a normal Windows PATH — the POSIX fallback below returned
        # None for every pid, so the watchdog's RSS-recycle ceiling never fired.
        # Read WorkingSetSize via GetProcessMemoryInfo through the shim.
        rss = platform_compat.proc_rss_bytes_for_pid(pid)
        return None if rss is None else rss / (1024.0 * 1024.0)

    # macOS / other: no /proc, fall back to ps (mirrors the sysctl/ps pattern
    # used elsewhere in this codebase for darwin system info).
    ps_bin = platform_compat.trusted_system_bin("ps")
    if ps_bin is None:
        return None
    try:
        out = (
            subprocess.check_output([ps_bin, "-o", "rss=", "-p", str(pid)], timeout=2)
            .decode()
            .strip()
        )
        return int(out) / 1024.0
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _iter_descendant_pids(pid: int) -> list[int]:
    """Return ``[pid, *descendants]`` (Linux only), best-effort.

    Walks ``/proc/<pid>/task/<tid>/children`` breadth-first. Returns ``[pid]``
    when the interface is unavailable. Used so RSS accounting can cover a
    sandbox launcher's exec'd child — see _get_rss_tree_mb().
    """
    order: list[int] = []
    visited: set[int] = set()
    stack = [pid]
    while stack:
        p = stack.pop()
        if p in visited:
            continue
        visited.add(p)
        order.append(p)
        try:
            entries = os.listdir(f"/proc/{p}/task")
        except OSError:
            continue
        for tid in entries:
            try:
                with open(f"/proc/{p}/task/{tid}/children") as f:
                    tokens = f.read().split()
            except OSError:
                continue
            for tok in tokens:
                try:
                    cpid = int(tok)
                except ValueError:
                    continue
                if cpid not in visited:
                    stack.append(cpid)
    return order


def _get_rss_tree_mb(pid: int) -> float | None:
    """Sum RSS (MiB) of *pid* and all its descendants, or None if unavailable.

    On Linux the kirocrew-lite background runtime is spawned through the
    namespace sandbox launcher, which ``fork()``s: ``self._pid`` is the
    launcher parent (small, stable, blocked in ``waitpid``) while the real
    kiro-cli that accumulates multi-GB RSS is a child. Measuring only
    ``self._pid`` therefore misses the growth entirely, so we sum the whole
    descendant tree. On macOS (sandbox-exec / no launcher fork) the tree is
    just the process itself, so the sum equals the single-process RSS.
    """
    if sys.platform == "linux":
        total = 0.0
        found = False
        for p in _iter_descendant_pids(pid):
            r = _get_rss_mb(p)
            if r is not None:
                total += r
                found = True
        return total if found else None

    if platform_compat.IS_WINDOWS:
        # Walk the Toolhelp parent map (the same snapshot descendant tracking
        # uses) to find the subtree rooted at pid, then sum each process's RSS
        # via the shim. Windows spawns kiro-cli without a launcher fork, but the
        # tree still covers any MCP-server / tool children it spawns.
        try:
            parent_map = platform_compat._windows_process_parent_map()
        except Exception:
            return _get_rss_mb(pid)
        win_children: dict[int, list[int]] = {}
        for cpid, ppid in parent_map.items():
            win_children.setdefault(ppid, []).append(cpid)
        total_mb = 0.0
        found = False
        win_visited: set[int] = set()
        stack = [pid]
        while stack:
            p = stack.pop()
            if p in win_visited:
                continue
            win_visited.add(p)
            r = _get_rss_mb(p)
            if r is not None:
                total_mb += r
                found = True
            stack.extend(win_children.get(p, ()))
        return total_mb if found else None

    # macOS / other: build a ppid map from a single ps snapshot, then sum the
    # descendant subtree rooted at pid (ps reports RSS in KiB).
    ps_bin = platform_compat.trusted_system_bin("ps")
    if ps_bin is None:
        return _get_rss_mb(pid)
    try:
        out = (
            subprocess.check_output([ps_bin, "-Ao", "pid=,ppid=,rss="], timeout=2).decode().strip()
        )
    except (OSError, subprocess.SubprocessError):
        return _get_rss_mb(pid)
    children: dict[int, list[int]] = {}
    rss_kib: dict[int, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            cpid, ppid, rss = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(cpid)
        rss_kib[cpid] = rss
    if pid not in rss_kib:
        return None
    total_kib = 0
    visited: set[int] = set()
    stack = [pid]
    while stack:
        p = stack.pop()
        if p in visited:
            continue
        visited.add(p)
        total_kib += rss_kib.get(p, 0)
        stack.extend(children.get(p, []))
    return total_kib / 1024.0


class AcpRuntime:
    """Owns one kiro-cli acp subprocess with single-reader demux.

    The _reader_task is the ONLY coroutine that reads from stdout.
    It routes frames by:
      - 'id' field in _pending_requests → resolve Future (for send_and_await)
      - 'id' field in _routed_requests → put in session queue (for prompt responses)
      - params.sessionId → _session_queues[sessionId].put(msg)
      - no sessionId → broadcast to all session queues
    """

    def __init__(
        self,
        work_dir: str | Path | None = None,
        agent: str = CLIENT_NAME,
        sandbox_mode: str = "auto",
        extra_env: dict[str, str] | None = None,
        mcp_gateway_overlay: str | Path | None = None,
        mcp_gateway_settings_mcp_json: str | Path | None = None,
        mcp_gateway_socket: str | Path | None = None,
        max_age_secs: float = _DEFAULT_MAX_AGE_SECS,
        max_rss_mb: float = _DEFAULT_MAX_RSS_MB,
        model: str | None = None,
    ):
        if work_dir:
            self._work_dir = Path(work_dir)
        else:
            # config.paths is a stdlib-only leaf: importing it here can't
            # re-enter the config.loader -> providers.acp -> acp.client cycle.
            from kiro_crew.config.paths import config_dir

            self._work_dir = config_dir() / "workspace"
        self._agent = agent
        if model is not None:
            if not MODEL_ID_RE.match(model):
                raise ValueError(
                    f"Invalid model identifier: {model!r} — must match "
                    f"^[a-zA-Z0-9][a-zA-Z0-9._-]{{0,127}}$"
                )
        self._model = model
        self._sandbox_mode = sandbox_mode
        self._extra_env = extra_env or {}
        self._mcp_gateway_overlay = str(mcp_gateway_overlay) if mcp_gateway_overlay else None
        self._mcp_gateway_settings_mcp_json = (
            str(mcp_gateway_settings_mcp_json) if mcp_gateway_settings_mcp_json else None
        )
        self._mcp_gateway_socket = str(mcp_gateway_socket) if mcp_gateway_socket else None
        self._sandbox_cleanup: str | None = None

        # Recycling thresholds — see _is_stale(). Long-lived multiplexed
        # runtimes (e.g. the kirocrew-lite background runtime) have no
        # per-turn compaction, so age/RSS are the only signals available to
        # bound unbounded growth.
        self._max_age_secs = max_age_secs
        self._max_rss_mb = max_rss_mb

        # Process state
        self._process: asyncio.subprocess.Process | None = None
        self._pid: int | None = None
        self._start_time: int | None = None
        self._spawn_monotonic: float | None = None
        self._child_pids: dict[int, int | None] = {}

        # Single reader task — the ONLY coroutine that reads stdout
        self._reader_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._stderr_task: asyncio.Task | None = None  # type: ignore[type-arg]

        # Demux routing
        self._pending_requests: dict[int, asyncio.Future[dict[str, Any]]] = {}
        # Maps req_id → sessionId for responses that should be routed to a session queue
        # (e.g. session/prompt response signals turn completion and must reach the session)
        self._routed_requests: dict[int, str] = {}
        self._session_queues: dict[str, asyncio.Queue[JsonRpcMessage | None]] = {}
        # OAuth notifications can precede the session/new or session/load
        # response that reveals which queue to register. Stage only those
        # frames while an init is active, then transfer the matching session's
        # frames into its queue. The bounded buffer is cleared when the last
        # concurrent init finishes so an abandoned URL cannot reach a later
        # session that happens to reuse the same id.
        self._session_inits_in_flight = 0
        self._pending_init_notifications: deque[JsonRpcMessage] = deque(
            maxlen=_INIT_NOTIFICATION_BUFFER_LIMIT
        )
        self._next_id = 1
        self._initialized = False
        # Whether kiro-cli advertised session/load support in its initialize
        # response. Mirrors AcpClient._can_load_session — load_session() guards
        # on it so we never issue session/load against a backend that lacks it.
        self._can_load_session = False
        # promptCapabilities from the initialize response (e.g. {"image": true}).
        # Empty until the handshake completes, so callers fail CLOSED and send
        # text-only rather than guessing a modality the agent never advertised.
        self._prompt_capabilities: dict = {}
        self._dead = False
        self._last_activity: float = 0.0
        self._stderr_lines: list[str] = []
        # Unroutable-frame drop accounting: (sessionId, method) → count since
        # the last flush, plus the monotonic timestamp of that flush (0.0 = no
        # window open yet; the first counted drop opens it). Written ONLY from
        # _reader_loop (the single stdout owner) and its flush helper, so a plain
        # dict needs no lock — asyncio.ensure_future(self._reader_loop()) is
        # called exactly once, in spawn(), and never re-entered.
        self._dropped_frames: dict[tuple[str, str], int] = {}
        self._dropped_frames_flushed_at: float = 0.0

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def supports_image_prompt(self) -> bool:
        """True when the agent advertised ``promptCapabilities.image``.

        Fails closed: an un-handshaked or silent backend reports False, so the
        prompt path sends text only instead of an image block the agent may
        reject.
        """
        return bool(self._prompt_capabilities.get("image", False))

    def is_alive(self) -> bool:
        """True if the underlying process exists and has not exited."""
        return self._process is not None and self._process.returncode is None and not self._dead

    def _stale_by_age(self) -> bool:
        """True if uptime exceeds max_age_secs. Cheap, no I/O — safe to call
        under a lock. Does NOT consider RSS (see _is_stale for that)."""
        if self._pid is None or self._spawn_monotonic is None:
            return False
        return (time.monotonic() - self._spawn_monotonic) > self._max_age_secs

    async def _is_stale(self) -> str | None:
        """Return the recycle reason ('age' or 'rss'), or None if not stale.

        Distinct from is_alive(): a runtime can be perfectly healthy (process
        running, protocol responsive) yet still be "stale" — e.g. the
        kirocrew-lite background runtime observed growing unbounded (multi-GB
        RSS) over ~24h of uptime because the multiplexed design has no per-turn
        compaction or lifetime cap. Callers should check this alongside
        is_alive() and, when active session count is 0, kill() and respawn
        rather than reusing the process indefinitely.

        RSS is measured across the whole descendant tree (_get_rss_tree_mb):
        under the Linux namespace sandbox self._pid is the launcher parent, and
        the real kiro-cli child is what grows. The RSS probe shells out / reads
        /proc, so it is offloaded to subprocess_executor() to keep the event
        loop free.

        The RSS probe is gated behind _RSS_PROBE_MIN_AGE_SECS: a freshly-(re)used
        runtime returns None without any executor round-trip, so the hot reuse
        path in get_bg_session (which holds _bg_runtime_lock) stays CPU-only for
        young runtimes. The lock IS deliberately held across the probe for
        older-and-idle runtimes; the age gate bounds how often that happens.
        """
        if self._pid is None:
            return None

        if self._spawn_monotonic is not None:
            age = time.monotonic() - self._spawn_monotonic
            if age > self._max_age_secs:
                return "age"
            if age < _RSS_PROBE_MIN_AGE_SECS:
                # Too young to have grown — skip the offloaded RSS probe.
                return None

        rss_mb = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), _get_rss_tree_mb, self._pid
        )
        if rss_mb is not None and rss_mb > self._max_rss_mb:
            return "rss"

        return None

    def has_active_sessions(self) -> bool:
        """True if any session is currently registered on this runtime.

        Used by callers deciding whether it's safe to recycle a stale
        runtime: killing it while a co-tenant session is registered would
        drop that session's in-flight prompt/response.
        """
        return bool(self._session_queues)

    # ── Lifecycle ──

    async def spawn(self) -> None:
        """Start the kiro-cli acp subprocess and complete protocol handshake."""
        if self._process is not None:
            raise AcpRuntimeError("Runtime already spawned")

        self._work_dir.mkdir(parents=True, exist_ok=True)

        try:
            kiro_bin = await _resolve_kiro_bin_for_spawn()
        except _KiroExecutableTrustError as exc:
            raise AcpRuntimeError(str(exc)) from exc
        if not kiro_bin:
            raise AcpRuntimeError(f"{KIRO_CLI_BIN} not found in PATH")

        # Self-heal (B): kiro-cli discovers its selectable modes at startup from
        # ~/.kiro/agents/*.json, so the managed default agent file must exist
        # BEFORE this --agent spawn or set_mode later fails with
        # "Mode '<agent>' not found". Regenerate it if missing (best-effort, off
        # the loop). Non-managed agents can't be materialized here — the
        # create_session guard fails those closed instead.
        try:
            await asyncio.to_thread(ensure_agent_materialized, self._agent)
        except Exception:
            logger.warning("pre-spawn agent materialization failed", exc_info=True)

        argv: list[str] = [kiro_bin, KIRO_CLI_SUBCMD, "--agent", self._agent]
        if self._model:
            # Pin the model at process start (mirrors `kiro-cli chat --model X`).
            # This is the ONLY reliable way to run a non-default provider model
            # (e.g. GPT for image generation) — post-session set_model cannot
            # cross provider boundaries, and agent configs may pin a model.
            argv += ["--model", self._model]

        # OSS sandbox.wrap_argv supports (argv, mode, strip_python_env). The
        # MCP-gateway overlay is NOT delivered through the sandbox: its broker
        # stubs are injected at ACP session/new (see new_session), so pooling
        # needs no bind-mount and works with sandbox mode "off". strip_python_env
        # IS applied to keep the host PYTHONPATH/PYTHONHOME out of kiro-cli's
        # foreign MCP subprocesses (which bundle their own interpreter + deps).
        argv, self._sandbox_cleanup = wrap_argv(
            argv,
            mode=self._sandbox_mode,
            strip_python_env=True,
            is_kiro_cli=True,
        )
        # cgroup v2 scope (OUTERMOST): bound this agent + all its MCP-server /
        # tool descendants with pids.max (fork bomb) + memory.max (RSS balloon).
        # No-op + loud warning where cgroup delegation is unavailable. --scope
        # execs into the target, so self._pid below is still the real child.
        argv = cgroup_scope_argv(argv)

        env = {**os.environ}
        if self._extra_env:
            env.update(self._extra_env)

        # Parent-level scrub of gateway-owned channel credentials. The default
        # auto/standard sandbox launcher strips _AGENT_DENIED_ENV_KEYS only for
        # cc/strict, and this path copies a raw os.environ + wrap_argv (not
        # sandboxed_spawn_argv), so without this the Slack/WeCom/Telegram tokens
        # seeded into os.environ by load_credentials() would be inherited by the
        # agent subprocess on the default tier. Leaves the AWS/SSH env the
        # standard sandbox intentionally exposes untouched.
        env = scrub_agent_denied_env(env)

        env["PATH"] = augmented_path(env.get("PATH", ""))
        resolve_krb5_ccname(env)
        # Positive-identity marker for the orphan sweep: kiro-cli and every MCP
        # server it spawns inherit this, so escaped launcher trees (``npx
        # @playwright/mcp`` -> node) are identifiable as ours.
        env[KIROCREW_SPAWNED_ENV] = KIROCREW_SPAWNED_VALUE

        self._process = await create_subprocess_limited(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._work_dir),
            limit=_STDOUT_BUFFER_LIMIT,
            # POSIX: setsid so kill() can killpg the whole tree. Windows:
            # start_new_session is silently ignored; CREATE_NEW_PROCESS_GROUP
            # makes the child tree taskkill /T-reapable (see platform_compat
            # spawn-isolation note). CREATE_NO_WINDOW suppresses the console
            # window Windows would otherwise pop for this console child spawned
            # from the windowless gateway (0 on POSIX, so no effect there).
            start_new_session=platform_compat.IS_POSIX,
            creationflags=(
                platform_compat.CREATE_NEW_PROCESS_GROUP
                | platform_compat._SUBPROCESS_NO_WINDOW
            ),
            env=env,
            profile=RLIMIT_PROFILE_SESSION_HOST,
        )
        self._pid = self._process.pid
        self._start_time = _get_start_time(self._pid)
        self._spawn_monotonic = time.monotonic()
        self._last_activity = time.monotonic()
        logger.info("AcpRuntime spawned kiro-cli acp (PID %d)", self._pid)

        # Track the PID for orphan cleanup (mirrors AcpClient._spawn). Without
        # this, a kiro-cli process leaked by a gateway crash/restart is never
        # recorded in kiro_session_pids.txt, so startup cleanup can't reap it.
        # A LIVE runtime is already protected during the periodic sweep because
        # AcpSessionProvider._pid feeds _collect_active_pids — this only closes
        # the cross-restart leak.
        try:
            _track_pid(self._pid)
            _track_session_pid(self._pid)
            # Shield this shared runtime's PID from the periodic orphan sweep.
            # _bg_runtime and companion subagent runtimes are held only in
            # SessionManager instance attributes (not registered sessions /
            # warm-pool providers), so _collect_active_pids would otherwise
            # classify them as orphans and SIGKILL them mid-use.
            register_protected_pid(self._pid)
        except Exception:
            logger.debug("AcpRuntime: PID tracking failed for %s", self._pid, exc_info=True)

        # Everything after the subprocess exists must be guarded: if reader
        # startup or the initialize handshake fails (kiro-cli hang / auth stall),
        # the process, its reader/stderr tasks, its PID-file entries AND its
        # _PROTECTED_PIDS shield would all leak. kill() reaps them (and
        # unregisters the protected PID via _mark_dead) before we re-raise.
        # BaseException so CancelledError during the 30s handshake also cleans up.
        try:
            # Start stderr drain
            if self._process.stderr:
                self._stderr_task = asyncio.ensure_future(self._drain_stderr())

            # Start the single reader task — owns stdout exclusively
            self._reader_task = asyncio.ensure_future(self._reader_loop())

            # Protocol handshake
            init_resp = await self._send_and_await(
                "initialize",
                {
                    "clientName": CLIENT_NAME,
                    "clientVersion": CLIENT_VERSION,
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientCapabilities": ACP_CLIENT_CAPABILITIES,
                },
            )
            self._can_load_session = bool(
                init_resp.get("agentCapabilities", {}).get("loadSession", False)
            )
            # Retain promptCapabilities so the prompt path can gate non-text
            # blocks -- without them an image block would be sent regardless of
            # whether the agent accepts one, and a refusal would surface as a
            # generic error with no fallback.
            _prompt_caps = init_resp.get("agentCapabilities", {}).get("promptCapabilities", {})
            self._prompt_capabilities = _prompt_caps if isinstance(_prompt_caps, dict) else {}
            self._initialized = True
            logger.info("AcpRuntime initialized (PID %d)", self._pid)
        except BaseException:
            try:
                await self.kill()
            except Exception:
                logger.debug(
                    "AcpRuntime: cleanup kill after failed spawn/handshake failed", exc_info=True
                )
            raise

    # Grace window for SIGTERM before escalating, and the post-SIGKILL reap
    # window. Class attributes so tests can shrink them.
    _KILL_TERM_TIMEOUT = 5.0
    _KILL_REAP_TIMEOUT = 2.0

    async def kill(self) -> None:
        """Kill the subprocess and clean up all state."""
        # Fail pending futures + poison session queues FIRST. _mark_dead sets
        # self._dead internally; doing it up front (before teardown) ensures any
        # waiters learn the runtime died. Calling it after setting _dead=True
        # would hit its early-return guard and skip all cleanup.
        self._mark_dead("killed")

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._process:
            pid = self._process.pid
            # platform_compat.kill_process_tree: killpg on POSIX (the spawn
            # sets start_new_session=IS_POSIX, so the group is the tree);
            # taskkill /T on Windows, where os.getpgid/os.killpg do not exist
            # (a raw call raises AttributeError, which the OSError guard here
            # would NOT catch — the kiro-cli tree then leaks on every session
            # recycle). Offloaded to the subprocess executor: on Windows the
            # shim shells out to taskkill (a blocking subprocess.run), which
            # must not run on the event loop (no blocking call on the event
            # loop).
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(
                    subprocess_executor(),
                    lambda: platform_compat.kill_process_tree(pid, platform_compat.SIGTERM),
                )
            except (OSError, ProcessLookupError):
                pass
            try:
                await asyncio.wait_for(self._process.wait(), timeout=self._KILL_TERM_TIMEOUT)
            except asyncio.TimeoutError:
                try:
                    await loop.run_in_executor(
                        subprocess_executor(),
                        lambda: platform_compat.kill_process_tree(pid, platform_compat.SIGKILL),
                    )
                except (OSError, ProcessLookupError):
                    pass
                # Reap the child so a delivered SIGKILL doesn't leave a zombie
                # that the liveness probe below would misread as a survivor.
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=self._KILL_REAP_TIMEOUT)
                except asyncio.TimeoutError:
                    pass
            self._process = None
            if platform_compat.pid_exists(pid):
                # Both kill_process_tree calls above swallow OSError by design
                # (racing a normal exit), which makes a signal-delivery failure
                # (EPERM through a launcher wrapper, pgid drift) look identical
                # to success. Verify instead of assuming: a survivor must stay
                # PID-tracked so the startup/periodic sweeps keep a handle on
                # it — untracking here would leak the process until reboot.
                logger.warning(
                    "AcpRuntime kill: PID %d survived SIGTERM/SIGKILL escalation; "
                    "leaving PID tracked for sweep",
                    pid,
                )
            else:
                logger.info("AcpRuntime killed (PID %d)", pid)

                # Untrack the PID so the orphan sweep doesn't chase a dead entry
                # (mirrors AcpClient._reset_state). Best-effort — a leftover entry
                # is only pruned lazily otherwise.
                try:
                    _untrack_pid(pid)
                    _untrack_session_pid(pid)
                    unregister_protected_pid(pid)
                except Exception:
                    logger.debug("AcpRuntime: PID untracking failed for %s", pid, exc_info=True)

        if self._sandbox_cleanup:
            try:
                os.remove(self._sandbox_cleanup)
            except OSError:
                pass

    # ── Reader Task (single owner of stdout) ──

    def _note_dropped_frame(self, session_id: object, method: object) -> None:
        """Count one unroutable frame, flushing a summary at most once per interval.

        Replaces a per-frame log line (see the drop-accounting constants above).
        Cheap and synchronous by design: it is called from the hot demux path
        and must not await, so there is no timer task to leak and no blocking
        I/O beyond the throttled ``logger.debug`` the flush itself emits.

        Both arguments are backend-controlled and deliberately typed `object`:
        they are normalized through `_drop_key_part`, which is the only thing
        that keeps a wrong-typed value from raising in the shared reader.
        """
        key = (_drop_key_part(session_id), _drop_key_part(method))
        now = time.monotonic()
        if self._dropped_frames_flushed_at == 0.0:
            # First drop of this runtime's life opens the window. __init__ cannot
            # supply the baseline (a runtime may be constructed long before
            # spawn()), and a stale 0.0 would make every first drop flush
            # immediately instead of aggregating.
            self._dropped_frames_flushed_at = now
        counts = self._dropped_frames
        if key not in counts and len(counts) >= _DROP_SUMMARY_MAX_KEYS:
            # A wide fan-out of distinct keys inside one interval must not grow
            # the map; report what we have and start a fresh window.
            self._flush_dropped_frames(now)
        counts[key] = counts.get(key, 0) + 1
        if now - self._dropped_frames_flushed_at >= _DROP_SUMMARY_INTERVAL_SECS:
            self._flush_dropped_frames(now)

    def _flush_dropped_frames(self, now: float | None = None) -> None:
        """Emit one summary record per (sessionId, method) and reset the window.

        Called on the interval from _note_dropped_frame and unconditionally when
        the reader loop exits, so a low-rate trickle is reported late rather
        than swallowed. A key seen once in an otherwise idle hour is therefore
        reported at the next drop or at loop exit — deliberately traded for
        having no wakeup timer on the event loop.
        """
        self._dropped_frames_flushed_at = time.monotonic() if now is None else now
        counts = self._dropped_frames
        if not counts:
            return
        for (session_id, method), count in counts.items():
            logger.debug(
                "Dropped %d unroutable frame(s) for session %s (method=%s)",
                count,
                session_id,
                method,
            )
        counts.clear()

    async def _reader_loop(self) -> None:
        """Single reader task — owns stdout exclusively. Routes frames by type.

        Routing:
          1. Response with id in _pending_requests → resolve Future
          2. Response with id in _routed_requests → put in session queue
          3. Notification with params.sessionId → session queue
          4. No sessionId → broadcast to all queues
        """
        assert self._process and self._process.stdout
        stdout = self._process.stdout

        try:
            while True:
                try:
                    line = await stdout.readline()
                except (ValueError, asyncio.LimitOverrunError) as exc:
                    logger.error("stdout buffer overrun: %s", exc)
                    self._mark_dead(f"stdout overrun: {exc}")
                    return

                if not line:
                    rc = self._process.returncode if self._process else "?"
                    self._mark_dead(f"process exited (rc={rc})")
                    return

                self._last_activity = time.monotonic()

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("non-JSON stdout line: %s", line[:200])
                    continue

                # Valid JSON is not necessarily a JSON-RPC object: a bare scalar
                # or array (e.g. `123`, `"foo"`, `[1,2]`, `true`, `null`) would
                # make JsonRpcMessage.from_dict -> data.get(...) raise
                # AttributeError, crashing this single-owner reader and tearing
                # down EVERY multiplexed session. Skip anything that isn't an
                # object so one stray line can't kill the demux.
                if not isinstance(data, dict):
                    logger.debug("non-object JSON stdout line: %s", line[:200])
                    continue

                msg = JsonRpcMessage.from_dict(data)

                # Route responses
                if msg.id is not None and (msg.result is not None or msg.error is not None):
                    # JSON-RPC allows string ids, and this runtime only ever
                    # issues int ids — but the id in the response is agent-
                    # controlled. int("req-1") / int([...]) raises ValueError/
                    # TypeError, which the catch-all below turns into
                    # _mark_dead, poisoning EVERY multiplexed session over one
                    # unmatched frame. Same invariant as the non-dict guard
                    # above: skip the frame, don't kill the demux.
                    try:
                        req_id = msg.id if isinstance(msg.id, int) else int(msg.id)
                    except (TypeError, ValueError, OverflowError):
                        # OverflowError: json parses 1e9999 to float("inf"),
                        # which int() rejects differently from a bad string.
                        #
                        # Left per-frame on purpose (same for the unmatched-id
                        # line below), unlike the two session-routing drops:
                        # here the ID is the whole diagnostic value, and it is a
                        # distinct value per frame — aggregating by it would give
                        # the counter an unbounded key space, while aggregating
                        # without it would throw away the only datum that
                        # identifies the correlation bug. Both branches also
                        # require a response-shaped frame, i.e. one per request
                        # THIS runtime issued (bounded by turns), so neither has
                        # the after-teardown steady state that made the
                        # unknown-session line a flood.
                        logger.debug("Response with non-numeric id %r dropped", msg.id)
                        continue

                    # Check awaited requests first (init, session/new, set_mode)
                    future = self._pending_requests.pop(req_id, None)
                    if future and not future.done():
                        if msg.error:
                            future.set_exception(
                                AcpRuntimeError(_format_runtime_rpc_error(msg.error))
                            )
                        else:
                            future.set_result(msg.result or {})
                        continue

                    # Check routed requests (prompt response → session queue)
                    session_id = self._routed_requests.pop(req_id, None)
                    if session_id and session_id in self._session_queues:
                        await self._session_queues[session_id].put(msg)
                        continue

                    logger.debug("Unmatched response id=%d", req_id)
                    continue

                # Route notifications by sessionId
                session_id = (msg.params or {}).get("sessionId")
                if session_id:
                    # A frame tagged with a sessionId belongs to exactly one
                    # session. Route to it if registered; otherwise DROP it.
                    # Broadcasting a known-but-unregistered session's frame to
                    # every other session would be cross-talk.
                    queue = self._session_queues.get(session_id)
                    if queue is not None:
                        await queue.put(msg)
                    elif self._session_inits_in_flight and msg.is_method(
                        METHOD_MCP_OAUTH_REQUEST
                    ):
                        # session/new can emit OAuth before its response. The
                        # response is what gives create_session the id needed to
                        # register this queue, so retain the frame until then.
                        self._pending_init_notifications.append(msg)
                    else:
                        # Counted, not logged per frame: this is the measured
                        # flood (transcript replay during session/load, plus any
                        # backend still streaming after teardown).
                        self._note_dropped_frame(session_id, msg.method)
                    continue

                # No sessionId → genuinely global notification; broadcast to all.
                if self._session_queues:
                    # Snapshot: `await queue.put` yields, and a concurrent
                    # unregister_session() could pop mid-iteration otherwise.
                    for queue in list(self._session_queues.values()):
                        await queue.put(msg)
                else:
                    # Same unbounded shape as the unknown-session branch: with
                    # zero registered sessions EVERY global notification lands
                    # here, so a backend that keeps streaming after the last
                    # teardown floods at frame rate. Counted the same way, with
                    # a sentinel for the session half of the key.
                    self._note_dropped_frame(_DROP_NO_SESSION, msg.method)

        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error("Reader loop crashed: %s", exc, exc_info=True)
            self._mark_dead(f"reader crash: {exc}")
        finally:
            # Report the residual count on EVERY exit (EOF, overrun, cancel,
            # crash) so a trickle that never reached the interval is still
            # accounted for instead of vanishing with the task.
            self._flush_dropped_frames()

    def saw_not_logged_in(self) -> bool:
        """True if kiro-cli's 'not logged in' auth-failure appeared on stderr.

        Lets callers translate a runtime death into AcpAuthRequired (an
        actionable login prompt) instead of a generic process-death error —
        parity with AcpClient, which inspects stderr the same way.
        """
        return any(_NOT_LOGGED_IN_RE.search(line) for line in self._stderr_lines)

    def _mark_dead(self, reason: str) -> None:
        """Mark runtime dead, fail all pending requests, poison all session queues."""
        if self._dead:
            return
        self._dead = True
        # Release the sweep-protection shield on ANY death path (EOF, rc!=0,
        # stdout overrun, reader crash, broken pipe) — not just kill(). Otherwise
        # the dead PID lingers in _PROTECTED_PIDS forever and, after PID reuse,
        # could shield a genuinely-orphaned process from the orphan sweep.
        if self._pid:
            try:
                unregister_protected_pid(self._pid)
            except Exception:
                logger.debug(
                    "AcpRuntime: unregister protected pid failed for %s", self._pid, exc_info=True
                )
        # Diagnostic context: process returncode + tail of captured stderr so
        # operators can tell an OOM/crash from a clean exit without DEBUG logs.
        rc = self._process.returncode if self._process else None
        tail = " | ".join(self._stderr_lines[-5:]) if self._stderr_lines else "<none>"
        logger.warning(
            "AcpRuntime dead (PID %s): %s [returncode=%s] stderr_tail: %s",
            self._pid,
            reason,
            rc,
            tail,
        )

        exc = AcpRuntimeDead(reason)
        for future in self._pending_requests.values():
            if not future.done():
                future.set_exception(exc)
        self._pending_requests.clear()
        self._pending_init_notifications.clear()
        # Also drop routed-request correlations: on death no reader will pop
        # them, and if a session is never destroyed the entry would otherwise
        # linger. unregister_session() also sweeps these per-session; this is
        # belt-and-suspenders for the process-death-before-response case.
        self._routed_requests.clear()

        for queue in list(self._session_queues.values()):
            try:
                queue.put_nowait(None)  # poison sentinel
            except asyncio.QueueFull:
                pass

    # ── Protocol Interface (used by AcpSessionHandle) ──

    async def send_request(self, method: str, params: dict[str, Any]) -> int:
        """Send a JSON-RPC request and return the request id.

        The response will be routed to the session's queue (via _routed_requests)
        so AcpSessionHandle can detect turn completion. For requests that need
        an immediate response (init, session/new), use _send_and_await instead.
        """
        if not self._process or not self._process.stdin:
            raise AcpRuntimeDead("process not running")
        if self._dead:
            raise AcpRuntimeDead("runtime is dead")

        req_id = self._next_id
        self._next_id += 1

        # Register for session routing so the response goes to the right queue
        session_id = params.get("sessionId")
        if session_id and session_id in self._session_queues:
            self._routed_requests[req_id] = session_id

        req = JsonRpcRequest(method=method, params=params, id=req_id)
        data = json.dumps(req.to_dict()) + "\n"

        try:
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._routed_requests.pop(req_id, None)
            self._mark_dead(f"pipe broken: {exc}")
            raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

        self._last_activity = time.monotonic()
        return req_id

    async def send_notification(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no id, no response expected).

        Unlike send_request, this does NOT allocate an id or register routing,
        so it leaves no _routed_requests entry to leak when the server (per the
        ACP spec) sends no response back (e.g. session/cancel).
        """
        if not self._process or not self._process.stdin:
            raise AcpRuntimeDead("process not running")
        if self._dead:
            raise AcpRuntimeDead("runtime is dead")

        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        data = json.dumps(msg) + "\n"

        try:
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._mark_dead(f"pipe broken: {exc}")
            raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

        self._last_activity = time.monotonic()

    async def send_response(self, request_id: str | int, result: dict[str, Any]) -> None:
        """Send a JSON-RPC response (for server→client requests like permission)."""
        if not self._process or not self._process.stdin:
            raise AcpRuntimeDead("process not running")
        if self._dead:
            raise AcpRuntimeDead("runtime is dead")

        msg = {"jsonrpc": "2.0", "id": request_id, "result": result}
        data = json.dumps(msg) + "\n"

        try:
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._mark_dead(f"pipe broken: {exc}")
            raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

    async def send_error(self, request_id: str | int, code: int, message: str) -> None:
        """Send a JSON-RPC error response."""
        if not self._process or not self._process.stdin:
            raise AcpRuntimeDead("process not running")
        if self._dead:
            raise AcpRuntimeDead("runtime is dead")

        msg = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        data = json.dumps(msg) + "\n"

        try:
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._mark_dead(f"pipe broken: {exc}")
            raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

    def unregister_session(self, session_id: str) -> None:
        """Unregister a session queue (called by AcpSessionHandle.destroy)."""
        self._session_queues.pop(session_id, None)
        # Clean up any pending routed requests for this session
        stale = [k for k, v in self._routed_requests.items() if v == session_id]
        for k in stale:
            del self._routed_requests[k]
        logger.debug("Removed session %s", session_id)

    # Alias for backward compat
    remove_session = unregister_session

    async def terminate_session(self, session_id: str) -> None:
        """Evict a session from kiro-cli (freeing its memory), then unregister locally.

        Sends the ``_kiro.dev/session/terminate`` request so the multiplexed
        kiro-cli process drops this session from its in-memory session map and
        shuts the session's agent down (reaping its MCP child processes). WITHOUT
        this, a finished session's transcript + context stay resident in the
        shared process for its entire lifetime — so RSS grows without bound as
        background tasks and subagents accumulate (the multiplexed design has no
        per-turn compaction, so per-session eviction is the only reclaim signal).

        This is co-tenant-safe: it targets exactly one ``sessionId`` and never
        touches the process, unlike ``kill()`` (which would take every sibling
        session down with it).

        Best-effort and bounded: teardown must never hang or raise. If the
        runtime is already dead the session's memory died with the process, so
        the round-trip is skipped. The local ``unregister_session`` ALWAYS runs
        (``finally``) so the reader loop stops routing to an abandoned queue even
        when the terminate request could not be delivered — including when the
        enclosing task is cancelled mid-await (``asyncio.CancelledError`` is a
        ``BaseException``, so it would otherwise slip past the ``except Exception``).
        """
        try:
            if not self._dead and self._process is not None:
                try:
                    await self._send_and_await(
                        METHOD_SESSION_TERMINATE,
                        {"sessionId": session_id},
                        timeout=_TERMINATE_TIMEOUT,
                    )
                except Exception:
                    logger.debug(
                        "session/terminate failed for %s (runtime dead/slow); "
                        "proceeding with local unregister",
                        session_id,
                        exc_info=True,
                    )
        finally:
            self.unregister_session(session_id)

    # ── Session Management ──

    def _finish_session_init(self, session_id: str) -> list[JsonRpcMessage]:
        """Take staged init frames for one session and close its init scope."""
        matched: list[JsonRpcMessage] = []
        retained: deque[JsonRpcMessage] = deque(maxlen=_INIT_NOTIFICATION_BUFFER_LIMIT)
        for msg in self._pending_init_notifications:
            params = msg.params if isinstance(msg.params, dict) else {}
            if session_id and str(params.get("sessionId") or "") == session_id:
                matched.append(msg)
            else:
                retained.append(msg)
        self._pending_init_notifications = retained
        self._session_inits_in_flight -= 1
        if self._session_inits_in_flight == 0:
            # Anything unmatched belongs to a failed/abandoned init. Never let
            # it survive into the next session creation attempt.
            self._pending_init_notifications.clear()
        return matched

    @staticmethod
    def _mode_available(agent: str, resp: dict[str, Any]) -> bool:
        """Whether ``set_mode`` should be attempted for ``agent`` given a
        ``session/new``|``session/load`` response.

        True when the backend advertised no ``modes`` list at all (older kiro-cli
        / offline fake backend — attempt for backward compatibility) OR the agent
        is in the advertised ``availableModes``. False when a modes list WAS
        advertised (even an empty one) and the agent is absent — the case that
        would otherwise fault with ``-32603 "Mode '<agent>' not found"``. An
        explicitly-empty ``availableModes: []`` therefore fails closed, not open.
        """
        ids, _current, advertised = parse_session_modes(resp)
        if not advertised:
            return True
        return agent in ids

    async def create_session(
        self,
        cwd: str | Path | None = None,
        agent: str | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
    ) -> AcpSessionHandle:
        """Create a new ACP session on this runtime. Returns a session handle."""
        if not self._initialized:
            raise AcpRuntimeError("Runtime not initialized — call spawn() first")

        # Inject the shared gateway's broker stubs unless the caller supplied an
        # explicit list. A session-injected server outranks the same-named entry
        # in the agent spec, so this is what actually pools the servers — no file
        # is written anywhere. Empty when the gateway is disabled.
        if mcp_servers is None:
            # Resolve the overlay off the event loop: the lookup stats/reads
            # files, and blocking the loop stalls every other session's I/O.
            mcp_servers = await asyncio.to_thread(
                pooled_session_servers, self._mcp_gateway_overlay, agent or self._agent
            )
        params = build_session_new_params(
            cwd if cwd else self._work_dir,
            mcp_servers=mcp_servers,
        )

        self._session_inits_in_flight += 1
        session_id = ""
        try:
            resp = await self._send_and_await(METHOD_SESSION_NEW, params)
            session_id = str(resp.get("sessionId") or "")
            if not session_id:
                raise AcpRuntimeError(f"session/new did not return sessionId: {resp}")
        finally:
            buffered_init = self._finish_session_init(session_id)

        # Register session queue
        queue: asyncio.Queue[JsonRpcMessage | None] = asyncio.Queue()
        self._session_queues[session_id] = queue
        for msg in buffered_init:
            queue.put_nowait(msg)

        handle = AcpSessionHandle(
            session_id=session_id,
            queue=queue,
            runtime=self,
        )

        # Populate state from session/new response (configOptions, available models)
        handle.store_session_config(resp)

        # Set agent mode if specified. If set_mode raises, no handle is returned
        # to the caller, so terminate the session we just created above —
        # session/new already succeeded so the session exists in kiro-cli; a
        # plain local unregister would leak it in the shared process. terminate_
        # session also unregisters the queue. Mirrors the same cleanup in
        # load_session().
        #
        # Guard (A): only activate the mode when the backend advertised it in the
        # session/new `modes` list, or advertised no modes at all (older kiro-cli
        # / fake backend → attempt, backward-compatible). If modes ARE advertised
        # but the requested agent is absent, its ~/.kiro/agents/<agent>.json never
        # loaded (pre-spawn self-heal covers only the managed default). FAIL CLOSED
        # rather than silently leaving the session on kiro-cli's default mode: for
        # a restricted/app agent that would run a BROADER agent than requested (a
        # privilege escalation), so we terminate and raise an actionable error.
        if agent and self._mode_available(agent, resp):
            try:
                await self._send_and_await(
                    METHOD_SET_MODE,
                    set_mode_params(session_id, agent),
                )
            except Exception:
                await self.terminate_session(session_id)
                raise
        elif agent:
            _ids, _current, _adv = parse_session_modes(resp)
            await self.terminate_session(session_id)
            raise AcpRuntimeError(
                f"Agent mode {agent!r} is not available on this session "
                f"(advertised modes: {_ids or 'none'}); its "
                f"~/.kiro/agents/{agent}.json is likely missing. Refusing to run "
                f"the backend default mode {_current or '(unknown)'} in its place. "
                f"Run `kirocrew setup --agent-only` to materialize the agent config."
            )

        # Drain MCP-server-init / oauth / config notifications before the first
        # prompt so they don't race into the first turn (parity with
        # AcpClient._drain_notifications). Best-effort, bounded (~1s).
        await handle.drain_init()

        logger.info("Created session %s on runtime PID %d", session_id, self._pid or 0)
        return handle

    async def load_session(
        self,
        session_file: str,
        resume_sid: str,
        cwd: str | Path | None = None,
        agent: str | None = None,
    ) -> AcpSessionHandle:
        """Resume a prior session via session/load — mirrors AcpClient.

        Unlike create_session()+handle.load(), this issues session/load
        DIRECTLY (no session/new first), using the ORIGINAL sid as sessionId
        and passing cwd + mcpServers:[] + the full transcript path, exactly as
        AcpClient._initialize_session does. This avoids the double-session
        footgun (fresh session/new context replayed on top of the loaded
        transcript) that produced stopReason='refusal'. Raises on failure so
        the caller can fall back to create_session().
        """
        if not self._initialized:
            raise AcpRuntimeError("Runtime not initialized — call spawn() first")
        if not self._can_load_session:
            raise AcpRuntimeError("Backend does not advertise session/load support")

        load_params = {
            "sessionId": resume_sid,
            "cwd": str(cwd if cwd else self._work_dir),
            "mcpServers": [],  # kiro-cli gets its servers via --agent
            "_meta": {"_kiro.dev/session_file": session_file},
        }
        self._session_inits_in_flight += 1
        loaded_session_id = ""
        try:
            resp = await self._send_and_await(METHOD_SESSION_LOAD, load_params)

            # A genuine resume echoes "modes" in the response (same signal AcpClient
            # keys on). Anything else means load did not actually restore state.
            if "modes" not in resp:
                raise AcpRuntimeError(
                    f"session/load did not resume session {resume_sid}: {resp}"
                )
            loaded_session_id = resume_sid
        finally:
            buffered_init = self._finish_session_init(loaded_session_id)

        # Register the queue AFTER _send_and_await returns. During session/load
        # kiro-cli replays the full prior transcript on stdout; without a
        # registered queue those replay frames hit the "unknown session -> drop"
        # path in the reader loop and are silently discarded. Only frames
        # arriving AFTER this point (from future prompt() calls) reach the queue.
        # The load response itself routes via _pending_requests, not the session
        # queue, so this reorder is safe.
        queue: asyncio.Queue[JsonRpcMessage | None] = asyncio.Queue()
        self._session_queues[resume_sid] = queue
        for msg in buffered_init:
            queue.put_nowait(msg)

        handle = AcpSessionHandle(
            session_id=resume_sid,
            queue=queue,
            runtime=self,
        )
        handle.store_session_config(resp)

        # Activate the agent (mirrors AcpClient step 4 — set_mode applies to a
        # resumed session too, not just fresh ones). If set_mode raises, the
        # caller falls back to create_session() (a fresh sid + its own queue),
        # so terminate this resume_sid session first — session/load already
        # succeeded so kiro-cli holds it; a plain local unregister would leak it
        # in the shared process (and leave the reader routing late transcript-
        # replay frames to an abandoned queue). terminate_session unregisters too.
        if agent and self._mode_available(agent, resp):
            try:
                await self._send_and_await(
                    METHOD_SET_MODE,
                    set_mode_params(resume_sid, agent),
                )
            except Exception:
                await self.terminate_session(resume_sid)
                raise
        elif agent:
            # Guard (A) — see create_session. A resumed session always echoes a
            # `modes` list (checked above), so an absent agent means its config
            # isn't loaded. Fail closed rather than silently resuming on a
            # different (broader) default agent than the one requested.
            _ids, _current, _adv = parse_session_modes(resp)
            await self.terminate_session(resume_sid)
            raise AcpRuntimeError(
                f"Agent mode {agent!r} is not available for resumed session "
                f"{resume_sid} (advertised modes: {_ids or 'none'}); its "
                f"~/.kiro/agents/{agent}.json is likely missing. Refusing to run "
                f"the backend default mode {_current or '(unknown)'} in its place. "
                f"Run `kirocrew setup --agent-only` to materialize the agent config."
            )

        # Drain MCP-init / oauth / config notifications before the first prompt
        # (parity with AcpClient). Transcript-replay frames were already dropped
        # before the queue was registered above, so only genuine init frames
        # remain to drain here.
        await handle.drain_init()

        logger.info("Resumed session %s on runtime PID %d", resume_sid, self._pid or 0)
        return handle

    # ── Internal Helpers ──

    async def _send_and_await(
        self, method: str, params: dict[str, Any], timeout: float = _REQUEST_TIMEOUT
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and await the response via _pending_requests.

        Used for control-plane requests (initialize, session/new, set_mode)
        where we need the response immediately rather than routing it to a
        session queue. ``timeout`` bounds the wait — teardown paths pass a
        tighter value than the default so an unresponsive runtime can't stall
        session eviction.
        """
        if not self._process or not self._process.stdin:
            raise AcpRuntimeDead("process not running")
        if self._dead:
            raise AcpRuntimeDead("runtime is dead")

        req_id = self._next_id
        self._next_id += 1

        req = JsonRpcRequest(method=method, params=params, id=req_id)
        data = json.dumps(req.to_dict()) + "\n"

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_requests[req_id] = future

        try:
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending_requests.pop(req_id, None)
            self._mark_dead(f"pipe broken: {exc}")
            raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

        self._last_activity = time.monotonic()

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_requests.pop(req_id, None)
            raise AcpRuntimeError(f"Request {method} timed out")

    async def _drain_stderr(self) -> None:
        """Drain stderr to prevent subprocess blocking."""
        assert self._process and self._process.stderr
        stderr = self._process.stderr
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    break
                text = line.decode(errors="replace").strip()
                if text:
                    self._stderr_lines.append(text)
                    if len(self._stderr_lines) > 20:
                        self._stderr_lines = self._stderr_lines[-20:]
                    logger.debug("stderr: %s", text[:200])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # An overlong stderr line (ValueError / LimitOverrunError from
            # readline when no newline fits the buffer) or a low-level read
            # error must not kill this task with an unhandled exception. Log and
            # exit the drain cleanly rather than leaving a dead task behind.
            logger.debug("stderr drain task exiting on error: %s", exc, exc_info=True)
