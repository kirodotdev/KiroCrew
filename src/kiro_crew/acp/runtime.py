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
import functools
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import weakref
from collections import deque
from pathlib import Path
from typing import Any, Callable, TypeVar

from kiro_crew import agent_scratch, platform_compat
from kiro_crew.acp._dispatch import (
    agent_version_from_init,
    attach_kas_custom_agents,
    build_session_new_params,
    parse_session_modes,
    redact_text,
)
from kiro_crew.acp._dispatch import reject_option_id as _reject_option_id
from kiro_crew.acp._dispatch import (
    set_mode_params,
)
from kiro_crew.acp.client import (
    OversizeLineUnrecoverable,
    _drain_oversize_line,
    _get_start_time,
    _KiroExecutableTrustError,
    _resolve_kiro_bin_for_spawn,
    finish_suspended_spawn,
    is_auth_failure_output,
    kiro_cli_not_found_message,
)
from kiro_crew.acp.kas_agents import (
    KasAgentTranslationError,
    build_kas_custom_agents,
)
from kiro_crew.acp.kas_transport import build_kas_argv
from kiro_crew.acp.session_handle import (
    AcpRequestTimeout,
    AcpRuntimeDead,
    AcpRuntimeError,
    AcpRuntimeProtocol,
    AcpSessionHandle,
    _load_watchdog_settings,
    parse_advertised_models,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_KIRO_IDENTITY_STORE,
    ACP_CLIENT_CAPABILITIES,
    KAS_CLIENT_CAPABILITIES,
    METHOD_KAS_SESSION_DELETE,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_MCP_SERVER_INIT_FAILURE,
    METHOD_MCP_SERVER_INITIALIZED,
    METHOD_REQUEST_PERMISSION,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SESSION_TERMINATE,
    METHOD_SESSION_UPDATE,
    METHOD_SET_MODE,
    METHOD_SUBAGENT_LIST_UPDATE,
    JsonRpcMessage,
    JsonRpcRequest,
)
from kiro_crew.agent import ensure_agent_materialized
from kiro_crew.browser_cli.launch import browser_session_env, browser_socket_env
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.constants import KIROCREW_SPAWNED_ENV, KIROCREW_SPAWNED_VALUE
from kiro_crew.env import augmented_path, resolve_krb5_ccname
from kiro_crew.executors import subprocess_executor
from kiro_crew.mcp_gateway.session_servers import injection_server_names, pooled_session_servers
from kiro_crew.metrics.events import (
    CHILD_PERMISSION_DENIED,
    CHILD_PERMISSION_ROUTED,
    DROPPED_FRAMES,
    emit_counter,
)
from kiro_crew.resource_status import inject_xdist_auto_cap
from kiro_crew.sandbox import (
    RLIMIT_PROFILE_SESSION_HOST,
    BoundWorkspaceMismatch,
    assert_voice_runtime_outside_agent_workspace,
    bind_voice_safe_agent_workspace_async,
    cgroup_scope_argv,
    create_subprocess_limited,
    release_bound_agent_workspace,
    resolve_bound_session_workspace,
    scrub_agent_subprocess_env,
    wrap_argv,
    wrap_argv_async,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
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
    "AcpWorkspaceBindingError",
    "AcpRuntimeDead",
    "AcpRequestTimeout",
    "AcpRuntimeProtocol",
    "AcpSessionHandle",
]


# ── AcpRuntime ──

_T = TypeVar("_T")


class AcpWorkspaceBindingError(AcpRuntimeError):
    """A live descriptor-bound runtime cannot safely serve another cwd."""


_STDOUT_BUFFER_LIMIT = 10 * 1024 * 1024  # 10MB
# How many in-flight request ids to name in the oversize-frame warning. A dropped
# frame can carry a response, and the caller then fails as an opaque
# _send_and_await timeout — naming what was in flight at the drop makes that
# timeout attributable instead of a mystery. Capped so the line stays bounded.
_DROP_IDS_IN_LOG = 8
# JSON-RPC 2.0 "Method not found" — the reader loop answers an ownerless
# server→client request with this itself (see _answer_ownerless_request);
# mirrors the private constant AcpClient keeps for its own dispatch sites.
_JSONRPC_METHOD_NOT_FOUND = -32601
_REQUEST_TIMEOUT = 30.0
# One gateway event loop owns many independent SessionManager and worker-pool
# callers. Keep their expensive subprocess spawn + initialize handshakes behind
# one low process-wide-per-loop bound; worker pools use the same default.
_COLD_START_MAX_CONCURRENT = 2


class _ColdStartAdmission:
    """Loop-affine admission state for runtime spawn + initialize."""

    def __init__(self, limit: int) -> None:
        self.semaphore = asyncio.Semaphore(limit)
        self.active = 0
        self.queued = 0

    async def acquire(self) -> float:
        started = time.monotonic()
        self.queued += 1
        acquired = False
        try:
            await self.semaphore.acquire()
            acquired = True
        finally:
            self.queued -= 1
        if acquired:
            self.active += 1
        return (time.monotonic() - started) * 1000.0

    def release(self) -> None:
        self.active = max(0, self.active - 1)
        self.semaphore.release()


# asyncio synchronization primitives are loop-affine. Gateways normally have one
# loop, while tests and embedded callers can create several; keying by loop keeps
# the production bound gateway-wide without binding a semaphore to the wrong loop.
_cold_start_admissions: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, weakref.ReferenceType[_ColdStartAdmission]
] = weakref.WeakKeyDictionary()
_cold_start_admissions_lock = threading.Lock()


def _cold_start_admission() -> _ColdStartAdmission:
    loop = asyncio.get_running_loop()
    with _cold_start_admissions_lock:
        admission_ref = _cold_start_admissions.get(loop)
        admission = admission_ref() if admission_ref is not None else None
        if admission is None:
            admission = _ColdStartAdmission(_COLD_START_MAX_CONCURRENT)
            _cold_start_admissions[loop] = weakref.ref(admission)
        return admission


def _cold_start_counts() -> tuple[int, int]:
    """Current-loop active and queued starts for bounded diagnostics."""
    admission = _cold_start_admission()
    return admission.active, admission.queued


# Session start (session/new, session/load) gets its own budget because kiro-cli
# blocks the response while it initializes the session's MCP servers, and a
# remote server pending OAuth holds that initialization for its FULL 30s
# authorization wait. _REQUEST_TIMEOUT is also 30s, so sharing it turns session
# start into a race the client usually loses: kiro-cli creates the session, the
# client gives up a beat earlier, and the slot dies. This must stay comfortably
# ABOVE the backend's 30s OAuth wait plus the initialization tail that follows
# it (observed: remaining servers register within ~1s after the wait; a
# 71-server agent with no pending OAuth completes in ~14s) — do NOT "tidy" it
# back down to _REQUEST_TIMEOUT.
# This is the built-in default AND floor; ``agent.session_start_timeout_secs``
# raises it for agents whose MCP fleet legitimately needs longer (see
# _resolve_session_start_timeout below).
_SESSION_NEW_TIMEOUT = 90.0

# Caps for the MCP progress line attached to a session-start timeout: a
# 70-server agent must not turn one error into a multi-kilobyte string, and
# neither a server's own error text nor its NAME is trusted for length. Both are
# config-derived, and an installed app supplies its own server names.
_MCP_PROGRESS_NAME_CAP = 8
_MCP_PROGRESS_ERROR_CAP = 120
_MCP_PROGRESS_NAME_LEN_CAP = 64


def _strip_unprintable(text: str) -> str:
    """Drop the control characters a whitespace collapse cannot reach.

    ``str.split`` removes whitespace controls (newline, tab, CR), but ESC and
    the other non-whitespace controls survive it, and a terminal rendering the
    gateway log interprets them -- an MCP server's failure text could forge or
    recolor terminal output. Spaces are printable, so a collapsed string keeps
    its word separation.
    """
    return "".join(ch for ch in text if ch.isprintable())


def _sanitize_progress_name(name: str) -> str:
    """Make one MCP server name safe to put in a log line and an exception.

    A name is config-derived, so an installed app chooses it. Four hazards, all
    closed here rather than at each use: an embedded newline would forge a line
    in the gateway log, a non-whitespace control (ESC) would inject terminal
    escapes into it, an unbounded name would defeat the count cap that keeps
    one error from becoming a wall of text, and a name carrying
    credential-shaped text would leak it into a sink the error message reaches.
    Whitespace collapse and the control strip run AFTER redaction so a
    redaction marker cannot reintroduce a break.
    """
    scrubbed, _ = redact_exfiltration_urls(name)
    scrubbed, _ = redact_credentials(scrubbed)
    return _strip_unprintable(" ".join(scrubbed.split()))[
        :_MCP_PROGRESS_NAME_LEN_CAP
    ]


def _capped_names(names: list[str]) -> str:
    """Join names for an error line, truncating the tail to a countable summary.

    A pure formatter: names arrive already sanitized from the two points that
    admit them, so a composite like ``name (error)`` keeps its own error cap
    instead of being re-truncated to a name's length.
    """
    head = names[:_MCP_PROGRESS_NAME_CAP]
    rest = len(names) - len(head)
    joined = ", ".join(head)
    return f"{joined} (+{rest} more)" if rest > 0 else joined


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

# Entitlement probe (probe_advertised_models). The probe session carries no MCP
# servers and activates no mode, so it is far cheaper than a real session start;
# the timeout is still generous because the probe runs exactly when something is
# already wrong (a rejection is being revalidated) and a loaded host must not
# turn a recoverable verdict into a spurious probe failure.
_ENTITLEMENT_PROBE_TIMEOUT = 30.0
# A fresh answer is reused for this window so a burst of rejections (several
# chats revalidating at once) costs one round-trip, not one per rejection.
_ENTITLEMENT_PROBE_TTL_SECS = 20.0


KIRO_CLI_BIN = "kiro-cli"
KIRO_CLI_SUBCMD = "acp"
CLIENT_NAME = "kirocrew"
CLIENT_VERSION = "0.1.2"
PROTOCOL_VERSION = "2025-08-22"
# KAS validates this field against a numeric schema and rejects the kiro-cli
# date string with "expected number, received string", so the two backends must
# be sent different types. 1 is what the ACP SDK and KAS's own TUI send.
PROTOCOL_VERSION_KAS = 1


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
    Windows: WorkingSetSize through the ``platform_compat`` shim, since no
    ``ps`` is resolvable there. Returns None on any failure (missing /proc,
    permission error, process gone, ps not found) so callers can treat
    "unknown" the same as "not over threshold" rather than raising.
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
        # Windows ships no `ps` in the fixed system directories the POSIX
        # fallback below resolves through (trusted_system_bin ignores PATH on
        # purpose), so that fallback can only ever answer None here. Read
        # WorkingSetSize via GetProcessMemoryInfo through the shim instead.
        # The watchdog's RSS-recycle ceiling does not depend on this branch —
        # _get_rss_tree_mb serves Windows from proc_rss_tree_mb_for_pid and
        # never calls this function — so this keeps a direct single-pid read
        # honest for a direct caller.
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


#: A whole-machine process table: ``(children_by_ppid, rss_kib_by_pid)``.
_ProcessTable = tuple[dict[int, list[int]], dict[int, int]]

#: How long one ``ps -A`` snapshot may be reused.
#:
#: This exists because the snapshot is WHOLE-MACHINE while its consumer asks
#: per-pid. ``session_memory._blocking_sample`` samples every live runtime pid in
#: one pass, so an uncached snapshot enumerated every process on the host once
#: PER SESSION — 8 sessions on a host with ~150 MCP processes meant 8 full
#: process-table walks every 5s, serialized in one worker. Measured cost on a
#: typical Mac (875 procs): ~33ms per ``ps -Ao``, so 8 walks ≈ 272ms duty cycle
#: per 5s poll — linear amplification that wastes a thread worker and grows with
#: session count (macOS only: the Linux branch above uses ``/proc`` directly and
#: never spawns anything).
#:
#: One second is chosen against the two consumers, not arbitrarily: the Sessions
#: panel polls at 5s and the watchdog's RSS ceiling is a multi-GB threshold
#: checked on a timer, so neither can tell a 1s-old measurement from a fresh
#: one — while a sampling pass over N pids completes well inside the window and
#: therefore pays for exactly one snapshot.
_PS_TABLE_TTL_S = 1.0

_ps_table_lock = threading.Lock()
#: ``(monotonic_taken_at, table)``, or None before the first snapshot. A cached
#: FAILURE is not stored — a transient ``ps`` error must not pin every caller to
#: the single-pid fallback for a whole second.
_ps_table_cache: tuple[float, _ProcessTable] | None = None


def _reset_ps_table_cache() -> None:
    """Drop the memoized process table. Test seam: the cache is keyed on wall
    time only, so a test that fakes ``ps`` output would otherwise inherit the
    previous test's snapshot."""
    global _ps_table_cache
    with _ps_table_lock:
        _ps_table_cache = None


def _ps_process_table() -> _ProcessTable | None:
    """One ``ps -Ao pid=,ppid=,rss=`` snapshot as a parent map + RSS map.

    Memoized for :data:`_PS_TABLE_TTL_S` so a caller that needs the tree for many
    pids pays for ONE process-table walk rather than one per pid. Returns None
    when ``ps`` is unavailable or fails, so callers fall back to a single-pid
    read instead of reporting a phantom-empty tree.

    The snapshot is taken under the lock rather than merely published under it:
    concurrent first-callers would otherwise each spawn ``ps`` before any of them
    stored a result, which is the exact amplification this cache exists to
    remove.
    """
    global _ps_table_cache
    with _ps_table_lock:
        cached = _ps_table_cache
        if cached is not None and (time.monotonic() - cached[0]) < _PS_TABLE_TTL_S:
            return cached[1]
        ps_bin = platform_compat.trusted_system_bin("ps")
        if ps_bin is None:
            return None
        try:
            out = (
                subprocess.check_output([ps_bin, "-Ao", "pid=,ppid=,rss="], timeout=2)
                .decode()
                .strip()
            )
        except (OSError, subprocess.SubprocessError):
            return None
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
        table: _ProcessTable = (children, rss_kib)
        _ps_table_cache = (time.monotonic(), table)
        return table


def _get_rss_tree_mb(pid: int) -> float | None:
    """Sum RSS (MiB) of *pid* and all its descendants, or None if unavailable.

    On Linux the kirocrew-lite background runtime is spawned through the
    namespace sandbox launcher, which ``fork()``s: ``self._pid`` is the
    launcher parent (small, stable, blocked in ``waitpid``) while the real
    kiro-cli that accumulates multi-GB RSS is a child. Measuring only
    ``self._pid`` therefore misses the growth entirely, so we sum the whole
    descendant tree.

    On macOS the tree is walked too, and it is NOT redundant: kiro-cli spawns
    MCP-server / tool children there exactly as it does on Windows (see that
    branch's note), so measuring only ``pid`` under-reports a session's real
    footprint and blinds the watchdog's leak ceiling. An earlier version of this
    docstring claimed the macOS tree "is just the process itself"; it is kept
    corrected here because that claim is what made the per-pid whole-machine
    snapshot look free.
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
        # Windows spawns kiro-cli WITHOUT a launcher fork, but it still spawns
        # MCP-server / tool children that can leak. Sum the tree via
        # proc_rss_tree_mb_for_pid, which enumerates descendants through
        # descendant_termination_handles — the lineage-VALIDATED walk (exact
        # creation/exit-time edge checks across two snapshots). A raw Toolhelp
        # parent-map walk is unsafe here: th32ParentProcessID is never cleared
        # when a parent dies and Windows recycles PIDs, so it would sum unrelated
        # subtrees rooted at a recycled PID into a kill/health decision. The
        # validated walk always counts the root, so an unreadable descendant
        # (another session / higher integrity) narrows the total rather than
        # producing a phantom-low tree attached to a recycled root.
        return platform_compat.proc_rss_tree_mb_for_pid(pid)

    # macOS / other: sum the descendant subtree rooted at pid off a SHARED
    # whole-machine snapshot (ps reports RSS in KiB). The snapshot is memoized in
    # _ps_process_table, so sampling N pids costs one process-table walk, not N.
    table = _ps_process_table()
    if table is None:
        return _get_rss_mb(pid)
    children, rss_kib = table
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


def _resolve_session_start_timeout() -> float:
    """Snapshot ``agent.session_start_timeout_secs`` from config.

    Function-level import (mirrors ``_load_watchdog_settings`` in
    session_handle.py) avoids the config -> dashboard -> acp import cycle;
    any failure falls back to the built-in default rather than breaking a
    runtime. The loader clamps the on-disk value to
    [SESSION_START_TIMEOUT_MIN, SESSION_START_TIMEOUT_MAX]; the ``max`` here
    is belt-and-braces so a degraded load can never shrink the budget below
    the built-in floor — a session-start budget under the backend's 30s OAuth
    wait recreates the race that floor exists to prevent.
    """
    try:
        # circular import: config.loader -> dashboard -> session -> acp
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig.load()
        return max(_SESSION_NEW_TIMEOUT, float(cfg.agent.session_start_timeout_secs))
    except Exception:
        logger.debug("session-start timeout load failed — using default", exc_info=True)
        return _SESSION_NEW_TIMEOUT


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
        mcp_gateway_socket: str | Path | None = None,
        max_age_secs: float = _DEFAULT_MAX_AGE_SECS,
        max_rss_mb: float = _DEFAULT_MAX_RSS_MB,
        model: str | None = None,
        expect_mcp_reports: bool = True,
        acp_backend: str = ACP_BACKEND_KIRO,
        crew_agent: str = "",
    ):
        if work_dir:
            self._work_dir = Path(work_dir)
        else:
            # config.paths is a stdlib-only leaf: importing it here can't
            # re-enter the config.loader -> providers.acp -> acp.client cycle.
            from kiro_crew.config.paths import default_workspace_dir

            self._work_dir = default_workspace_dir()
        self._agent = agent
        # Canonical Kiro Crew agent identity (a cfg.agents key) resolved by the
        # surface that created this runtime — a DIFFERENT namespace from
        # ``agent`` (the kiro template the process spawns with). Default for
        # sessions created on this runtime; a warm-pool rekey overwrites it so
        # later sessions inherit the claiming crew, not the pool's spawn state.
        self._crew_agent = crew_agent
        self._acp_backend = acp_backend
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
        self._mcp_gateway_socket = str(mcp_gateway_socket) if mcp_gateway_socket else None
        # Whether sessions on this runtime should hold drain_init() open for
        # slow MCP servers (the no-report ceiling). A runtime whose agent is
        # KNOWN to have zero MCP servers — the kirocrew-lite background runtime,
        # whose config Kiro Crew itself writes with an empty mcpServers map —
        # opts out so hot one-liner paths (chat titles, suggestions, STT
        # endpointing) don't pay a full ceiling wait that can never be armed.
        self._expect_mcp_reports = expect_mcp_reports
        self._sandbox_cleanup: str | None = None
        self._bound_workspace_fd: int | None = None
        self._spawn_work_dir = str(self._work_dir)

        # Recycling thresholds — see _is_stale(). Long-lived multiplexed
        # runtimes (e.g. the kirocrew-lite background runtime) have no
        # per-turn compaction, so age/RSS are the only signals available to
        # bound unbounded growth.
        self._max_age_secs = max_age_secs
        self._max_rss_mb = max_rss_mb

        # session/new + session/load budget — resolved lazily on first use
        # (never in __init__: KiroCrewConfig.load() is a synchronous disk
        # read + schema validation on a cache miss, and runtimes are
        # constructed on the event loop) and cached for the runtime's
        # lifetime. See _session_start_budget().
        self._session_start_timeout: float | None = None

        # Process state
        self._process: asyncio.subprocess.Process | None = None
        self._pid: int | None = None
        self._start_time: int | None = None
        self._spawn_monotonic: float | None = None
        self._child_pids: dict[int, int | None] = {}
        # Names THIS spawn of the shared child process (fresh per spawn, cleared
        # with the process) — the identity a resource minted by the child is
        # compared against later. See AcpClient.process_instance for why the
        # session id cannot serve: a resume reuses it on a new process.
        self._process_instance: str = ""

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
        # agentInfo.version from the initialize response: the version of the
        # binary THIS process is executing, which can differ from the one on
        # disk after an in-place upgrade. Empty until the handshake completes.
        self._agent_version = ""
        # Entitlement probe state (probe_advertised_models): single-flight lock
        # plus a short-TTL cache of the last non-empty answer.
        self._entitlement_probe_lock = asyncio.Lock()
        self._entitlement_probe_at = 0.0
        self._entitlement_probe_result: list[dict[str, str]] = []
        self._dead = False
        self._last_activity: float = 0.0
        self._stderr_lines: list[str] = []
        # Latched auth-failure observation. ``_stderr_lines`` is a 20-line ring,
        # so on a noisy startup the auth line can be evicted before anything asks
        # about it — and the question is only ever asked LATER, once a request
        # times out or the runtime dies. Re-scanning a buffer that no longer holds
        # the evidence answers "no auth problem", which is indistinguishable from
        # a real negative. Latch on arrival instead: an auth failure observed once
        # stays observed for the life of this runtime, which is correct because
        # nothing about a rejected credential un-rejects itself mid-process.
        self._saw_auth_failure = False
        # Unroutable-frame drop accounting: (sessionId, method) → count since
        # the last flush, plus the monotonic timestamp of that flush (0.0 = no
        # window open yet; the first counted drop opens it). Written ONLY from
        # _reader_loop (the single stdout owner) and its flush helper, so a plain
        # dict needs no lock — asyncio.ensure_future(self._reader_loop()) is
        # called exactly once, in spawn(), and never re-entered.
        self._dropped_frames: dict[tuple[str, str], int] = {}
        # In-flight SEL audit tasks for auto-rejected permission requests;
        # held only to keep them alive (see _answer_unroutable_permission).
        self._audit_tasks: set[asyncio.Task] = set()
        # In-flight ANSWER tasks (the coroutines that write the rejection
        # response), tracked separately from the SEL audit tasks above: the
        # flood cap below must count only tasks that can block on stdin
        # drain() — audit tasks are short-lived thread offloads, and letting
        # them satisfy the cap would let a burst of ordinary audits trip a
        # false mark_dead that kills every multiplexed session.
        self._answer_tasks: set[asyncio.Task] = set()
        # Volume bound for in-flight auto-answer tasks. Each task can block
        # on stdin drain() against a backend that floods permission frames
        # while never reading its stdin — unbounded, that grows the task set
        # until the gateway OOMs. Awaiting an answer inline on the reader
        # would hand the same hostile backend a demux freeze for every
        # session, so the bound treats a capacity timeout as a dead pipe
        # (see _wait_for_answer_capacity).
        self._max_answer_tasks: int = 128
        # Bounded discrimination wait at the cap (see
        # _wait_for_answer_capacity):
        # small enough that a wedged pipe is condemned promptly, large enough
        # that a responsive backend's in-flight answers can complete.
        self._answer_cap_wait_secs: float = 5.0
        # Backend-internal subagent session ids, snapshotted from each
        # `_kiro.dev/subagent/list_update` frame (a FULL list every time, so
        # replacement — not accumulation — keeps it bounded and current).
        # Membership proves an unregistered sessionId is a real backend child.
        # `_subagent_owner` records WHICH registered session was the sole
        # consumer when the announce arrived — routing later requires the sole
        # queue to still be that exact session, so a warm-reused runtime whose
        # session was swapped can never inherit a stale child's approvals.
        # Both are cleared when the owning session unregisters.
        self._subagent_sessions: set[str] = set()
        self._subagent_owner: str | None = None
        # Sessions with an ACTIVELY CONSUMING prompt dispatch loop, marked by
        # AcpSessionHandle.prompt() around its dispatch (all exit paths,
        # including timeout/cancel/synthetic completion, unmark in a finally).
        # _routed_requests is NOT usable for this: it also holds set_mode /
        # steer / config request ids, and a timed-out prompt leaves its entry
        # until the backend response arrives — either would make routing
        # believe a consumer exists and park a child request unread.
        self._turn_active_sessions: set[str] = set()
        self._dropped_frames_flushed_at: float = 0.0

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def process_instance(self) -> str:
        """Identity of the CURRENT child process instance (``""`` when none).

        Fresh per spawn, so equality distinguishes the process that minted a
        resource from any successor — including one that resumed the same ACP
        session id. Liveness is asked separately (:meth:`is_alive`).
        """
        return self._process_instance if self._process is not None else ""

    @property
    def acp_backend(self) -> str:
        """Which ACP backend this runtime's process speaks.

        Public because the backend has to survive being read back off a
        started provider: the runtime is the only object that still knows it
        once ``AcpProvider`` swaps its placeholder client for a session
        provider.
        """
        return self._acp_backend

    @property
    def uses_kiro_identity_store(self) -> bool:
        """True when this runtime's process signs in from kiro-cli's own store.

        Membership in ``ACP_BACKENDS_KIRO_IDENTITY_STORE`` (harness-parity
        H5/H14). ``AcpRuntime`` is not an ``LLMProvider``, but the identity-change
        sweep reaches shared runtimes as well as session providers, so it
        declares the same capability under the same name -- letting that sweep
        ask both families one question instead of probing private attributes.
        """
        return self._acp_backend in ACP_BACKENDS_KIRO_IDENTITY_STORE

    @property
    def supports_image_prompt(self) -> bool:
        """True when the agent advertised ``promptCapabilities.image``.

        Fails closed: an un-handshaked or silent backend reports False, so the
        prompt path sends text only instead of an image block the agent may
        reject.
        """
        return bool(self._prompt_capabilities.get("image", False))

    @property
    def agent_version(self) -> str:
        """``agentInfo.version`` the agent reported at ``initialize`` (``""`` until then).

        This is the version of the binary the process is RUNNING, which is what
        a capability decision about a live session must key on: after an
        in-place kiro-cli upgrade the file on disk is newer than every process
        spawned before it.
        """
        return self._agent_version

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

    def has_active_or_initializing_sessions(self) -> bool:
        """True if any session is registered OR still being created.

        ``has_active_sessions`` sees only REGISTERED queues, and
        ``create_session`` registers outside the runtime lock -- so a co-tenant
        whose ``session/new`` is in flight is momentarily invisible to it. Callers
        that recycle a stale runtime tolerate that window deliberately (their
        ``create_session`` raises ``AcpRuntimeDead`` and a respawn loop backstops
        it, costing one extra respawn).

        A caller with NO such backstop must not: killing the runtime under an
        initializing task session surfaces as ``AcpRuntimeDead`` on work the user
        never connected to whatever prompted the kill. Those callers ask this
        instead, which also counts ``_session_inits_in_flight``.
        """

        return bool(self._session_queues) or self._session_inits_in_flight > 0

    # ── Lifecycle ──

    def _discard_sandbox_cleanup(self) -> None:
        """Unlink and forget the sandbox temp file allocated by ``wrap_argv``.

        Mirrors ``AcpClient._discard_sandbox_cleanup``: once no child will
        exec the launcher/profile file — spawn failed, was cancelled, or the
        runtime is shutting down — it must be removed, or each attempt leaks
        one file into the temp dir for the gateway's lifetime.
        """
        if self._sandbox_cleanup:
            try:
                os.remove(self._sandbox_cleanup)
            except OSError:
                pass
            self._sandbox_cleanup = None

    async def _discard_bound_workspace(self) -> None:
        """Close the parent copy of a macOS workspace identity off-loop."""
        descriptor = getattr(self, "_bound_workspace_fd", None)
        self._bound_workspace_fd = None
        work_dir = getattr(self, "_work_dir", None)
        if work_dir is not None:
            self._spawn_work_dir = str(work_dir)
        if descriptor is not None:
            await release_bound_agent_workspace(descriptor)

    async def _session_work_dir(self, cwd: str | Path | None = None) -> str | Path:
        """Resolve an ACP cwd without re-authorizing a mutable macOS pathname."""
        if self._bound_workspace_fd is None:
            return cwd if cwd else self._work_dir
        requested = cwd if cwd else self._work_dir
        # The shared rule lives in sandbox.resolve_bound_session_workspace; only the
        # error mapping is this front end's. What the peer receives is the BOUND
        # DESCRIPTOR's own name, not the caller's spelling -- that one is what a
        # symlink swap controls, and it can name a descendant this check never
        # covered.
        #
        # What no string here can do is bind the PEER's own resolution.
        # ``session/new`` carries a cwd STRING that a separate process re-resolves
        # after this returns, so a same-UID rename of the canonical directory in that
        # window remains open; that is a property of the protocol boundary, not of the
        # spelling. ``/dev/fd/<n>`` is not the alternative: the binding is darwin-only
        # (see bind_voice_safe_agent_workspace, which returns no descriptor off
        # macOS), and macOS cannot resolve those entries at all -- the very bug this
        # change exists to fix, i.e. that spelling never delivered a working session
        # cwd, let alone a safer one. The agent PROCESS's own cwd is pinned by
        # descriptor at spawn (create_subprocess_limited's chdir_fd), which is the
        # part that does not go through a name.
        try:
            return await resolve_bound_session_workspace(self._bound_workspace_fd, requested)
        except BoundWorkspaceMismatch as exc:
            raise AcpWorkspaceBindingError(
                "A delegated macOS Kiro runtime is bound to one exact workspace; "
                "create a runtime bound to the requested workspace"
            ) from exc
        except OSError as exc:
            raise AcpWorkspaceBindingError(
                "Cannot verify the requested macOS session workspace"
            ) from exc

    async def _to_thread_guarding_sandbox(
        self, fn: Callable[..., _T], /, *args: Any, **kwargs: Any
    ) -> _T:
        """``asyncio.to_thread`` that discards the sandbox file on failure.

        After ``wrap_argv`` has allocated the sandbox temp file, every
        suspension point before the exec is a leak window: a cancellation
        unwinds ``spawn`` without reaching the shutdown cleanup, orphaning the
        file. Route any offload in that window through here so the file is
        removed before re-raising.
        """
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except BaseException:
            self._discard_sandbox_cleanup()
            raise

    @staticmethod
    async def _kiro_cli_missing(environ: dict[str, str], home: Path) -> str:
        """Off-loop wrapper over the shared missing-CLI message.

        Off-loop because building it expands the inherited PATH (filesystem
        work), the same reason AcpClient._spawn defers its own call.
        """
        return await asyncio.to_thread(kiro_cli_not_found_message, environ=environ, home=home)

    async def _resolve_spawn_argv(self) -> list[str]:
        """Pre-sandbox argv for this runtime's backend.

        Explicit per-backend construction: the two agents share no flags, and
        only kiro-cli needs its agent file materialized first.
        """
        # ONE reading of the environment drives both the search and the message
        # that reports it, so a "not found (searched ...)" line here cannot name
        # directories the resolve never walked -- the property AcpClient._spawn
        # already holds, and the reason both go through
        # kiro_cli_not_found_message instead of formatting their own text.
        spawn_environ = dict(os.environ)
        spawn_home = Path.home()

        if self._acp_backend == ACP_BACKEND_KAS:
            # KAS is reached through kiro-cli's own ACP relay, so it resolves the
            # same trusted binary as the kiro backend. No --agent: KAS takes
            # custom agents over the wire in session/new
            # (_meta.kiro.customAgents), not from a CLI flag.
            kas_bin = await _resolve_kiro_bin_for_spawn(environ=spawn_environ, home=spawn_home)
            if not kas_bin:
                raise AcpRuntimeError(await self._kiro_cli_missing(spawn_environ, spawn_home))
            return build_kas_argv(kas_bin)

        kiro_bin = await _resolve_kiro_bin_for_spawn(environ=spawn_environ, home=spawn_home)
        if not kiro_bin:
            raise AcpRuntimeError(await self._kiro_cli_missing(spawn_environ, spawn_home))

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
        return argv

    async def spawn(self) -> None:
        """Start the ACP runtime behind the gateway-wide cold-start admission gate."""
        if self._process is not None:
            raise AcpRuntimeError("Runtime already spawned")

        admission = _cold_start_admission()
        wait_ms = await admission.acquire()
        logger.info(
            "acp_cold_start stage=queue_wait outcome=admitted wait_ms=%.1f "
            "active_starts=%d queued_starts=%d",
            wait_ms,
            admission.active,
            admission.queued,
        )
        started = time.monotonic()
        outcome = "error"
        try:
            await self._spawn_admitted()
            outcome = "ready"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            process = self._process
            if process is None:
                process_state = "absent"
            elif process.returncode is None:
                process_state = "running"
            else:
                process_state = "exited"
            logger.info(
                "acp_cold_start stage=spawn outcome=%s duration_ms=%.1f backend=%s "
                "active_starts=%d queued_starts=%d process_state=%s",
                outcome,
                (time.monotonic() - started) * 1000.0,
                self._acp_backend or "kiro",
                admission.active,
                admission.queued,
                process_state,
            )
            admission.release()

    async def _spawn_admitted(self) -> None:
        """Spawn and initialize after the caller has acquired cold-start admission."""
        if self._process is not None:
            raise AcpRuntimeError("Runtime already spawned")

        # Off-loop: mkdir is a blocking syscall and the parent dirs may live on
        # slow storage; the loop must never wait on the kernel here.
        await asyncio.to_thread(self._work_dir.mkdir, parents=True, exist_ok=True)
        # Delegated Kiro agents on macOS do not inherit Kiro Crew's Seatbelt
        # deny rules. Keep their workspace disjoint from the named voice-decoder
        # runtime so verified executable bytes cannot be replaced before spawn.
        if self._acp_backend in ACP_BACKENDS_INTERNAL_SANDBOX:
            await asyncio.to_thread(assert_voice_runtime_outside_agent_workspace, self._work_dir)

        try:
            argv = await self._resolve_spawn_argv()
        except _KiroExecutableTrustError as exc:
            raise AcpRuntimeError(str(exc)) from exc

        # OSS sandbox.wrap_argv supports (argv, mode, strip_python_env). The
        # MCP-gateway overlay is NOT delivered through the sandbox: its broker
        # stubs are injected at ACP session/new (see new_session), so pooling
        # needs no bind-mount and works with sandbox mode "off". strip_python_env
        # IS applied to keep the host PYTHONPATH/PYTHONHOME out of kiro-cli's
        # foreign MCP subprocesses (which bundle their own interpreter + deps).
        # is_kiro_cli drives the reviewed Kiro internal-sandbox delegation: on
        # macOS wrap_argv skips its seatbelt because the two cannot nest; on
        # Windows the official Kiro backend delegates by default because Crew
        # has no native OS sandbox there. Granted by membership in
        # ACP_BACKENDS_INTERNAL_SANDBOX (harness-parity H7), never as "not KAS":
        # this test fails OPEN, so a harness that inherited a negative test would
        # have Crew's seatbelt skipped in favour of an internal sandbox that never
        # starts. KAS is a Node process with no internal sandbox, so it takes
        # Crew's seatbelt directly, and so does every harness added later.
        argv, self._sandbox_cleanup = await wrap_argv_async(
            argv,
            mode=self._sandbox_mode,
            strip_python_env=True,
            is_kiro_cli=self._acp_backend in ACP_BACKENDS_INTERNAL_SANDBOX,
            _prepare=wrap_argv,
        )
        # cgroup v2 scope (OUTERMOST): bound this agent + all its MCP-server /
        # tool descendants with pids.max (fork bomb) + memory.max (RSS balloon).
        # No-op + loud warning where cgroup delegation is unavailable. --scope
        # execs into the target, so self._pid below is still the real child.
        # Off-loop: first call probes /proc + /sys and the config read touches
        # the config dir (mkdir + file read) — blocking syscalls that must not
        # run on the loop. Guarded: wrap_argv above allocated the sandbox temp
        # file, so a cancellation here must not orphan it.
        argv = await self._to_thread_guarding_sandbox(cgroup_scope_argv, argv)

        env = {**os.environ}
        if self._extra_env:
            env.update(self._extra_env)

        env["PATH"] = augmented_path(env.get("PATH", ""))

        def _resolve_env_off_loop() -> None:
            # KRB5CCNAME resolution lstat/stats /tmp/krb5cc_<uid>, and the
            # CLI's own KIRO_API_KEY is settled here too: re-injected from the
            # data home's .env for the kiro-cli backend (post-scrub Docker),
            # actively stripped for a foreign backend, which must never
            # receive it (see config.loader.inject/strip_kiro_cli_api_key) —
            # a file read either way. Both are blocking syscalls that must not
            # run on the loop, bundled into ONE thread hop. Guarded: the
            # sandbox temp file is live, so a cancellation here must not
            # orphan it.
            resolve_krb5_ccname(env)
            # Deferred import: this module keeps config.loader off its import
            # graph (matches acp.client's in-file convention).
            from kiro_crew.config.loader import (
                inject_kiro_cli_api_key,
                strip_kiro_cli_api_key,
            )

            # KIRO_API_KEY is kiro-cli's own MODEL credential for its v2
            # agent loop, so only the kiro backend is handed it. KAS takes the
            # strip branch even though its process is now a kiro-cli (the ACP
            # relay): the v3 engine authenticates from kiro-cli's OIDC store via
            # --auth-method cli and never reads this variable, so injecting it
            # would widen credential exposure for a consumer that does not
            # exist. Unchanged from when KAS was a bare Node process.
            if self._acp_backend == ACP_BACKEND_KIRO:
                inject_kiro_cli_api_key(env)
            else:
                strip_kiro_cli_api_key(env)

        await self._to_thread_guarding_sandbox(_resolve_env_off_loop)
        # Parent-side equivalent of the launcher scrub. This is required on
        # Windows where the positively classified Kiro backend delegates to the
        # CLI's internal sandbox without a POSIX `env -u` wrapper. Do it after
        # credential-pointer/API-key resolution so no resolver can reintroduce a
        # denied variable; KIRO_API_KEY itself is intentionally not denied.
        env = scrub_agent_subprocess_env(env)
        # Positive-identity marker for the orphan sweep: kiro-cli and every MCP
        # server it spawns inherit this, so escaped launcher trees (``npx
        # @playwright/mcp`` -> node) are identifiable as ours.
        env[KIROCREW_SPAWNED_ENV] = KIROCREW_SPAWNED_VALUE
        # Own browser session per agent process, matching AcpClient._spawn (see
        # browser_session_env). Per PROCESS, not per agent: with session sharing
        # on (the default) an eligible subagent's session is created on the
        # PARENT's runtime, so a parent and its subagents share this process and
        # therefore one browser; a task-runner run is a separate family sharing
        # one run-scoped process. What this buys is isolation BETWEEN families,
        # which is where the reported corruption came from. The docs tell an
        # agent sharing a process with a concurrent browser user to pass -s=.
        browser_env = browser_session_env(env)
        env.update(browser_env)
        if browser_env:
            lifecycle_env = {**os.environ, **browser_env}
            env.update(
                await self._to_thread_guarding_sandbox(
                    browser_socket_env, lifecycle_env
                )
            )
        # Per-process scratch containment: the agent's temp AND its
        # prompt-guided work products land in an owned directory instead of
        # the shared system temp dir. Allocated off-loop (mkdir + config read)
        # through the sandbox guard like the env resolution above, and
        # fail-open -- scratch is hygiene, not a spawn prerequisite. The
        # owner pid is recorded after spawn; reclamation is liveness-keyed
        # (agent_scratch.sweep_dead_scratch), never age-keyed.
        self._scratch_dir = None
        try:
            self._scratch_dir = await self._to_thread_guarding_sandbox(
                agent_scratch.allocate_scratch, "runtime"
            )
            env.update(agent_scratch.scratch_env(self._scratch_dir))
        except OSError:
            logger.warning(
                "agent-scratch: could not allocate; spawning with inherited temp",
                exc_info=True,
            )
        # Memory-aware cap for pytest-xdist's ``-n auto`` (subagent spawn path —
        # mirrors acp/client.py): xdist sizes auto to the CPU count, ignoring
        # memory; PYTEST_XDIST_AUTO_NUM_WORKERS bounds ONLY auto resolution.
        # Respects a pre-set value; see resource_status.inject_xdist_auto_cap.
        # Off-loop: resolving the cap reads the raw config, and that read
        # enters config_dir() (mkdir + file IO + JSON parse) — blocking
        # syscalls that must not run on the loop. Guarded: the sandbox temp
        # file is live, so a cancellation here must not orphan it.
        await self._to_thread_guarding_sandbox(inject_xdist_auto_cap, env)

        await self._discard_bound_workspace()
        if self._acp_backend in ACP_BACKENDS_INTERNAL_SANDBOX:
            self._spawn_work_dir, self._bound_workspace_fd = (
                await bind_voice_safe_agent_workspace_async(self._work_dir)
            )
        try:
            self._process = await create_subprocess_limited(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._spawn_work_dir,
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
                    | platform_compat.CREATE_SUSPENDED
                ),
                # None off macOS, where nothing binds. When set, the child enters
                # the workspace through this verified descriptor instead of
                # resolving ``cwd``'s pathname, which a same-UID symlink retarget
                # could aim elsewhere in between; ``cwd`` stays the same directory
                # by name so the spawn keeps reporting a real path.
                chdir_fd=self._bound_workspace_fd,
                env=env,
                profile=RLIMIT_PROFILE_SESSION_HOST,
            )
        except BaseException:
            await self._discard_bound_workspace()
            self._discard_sandbox_cleanup()
            raise
        self._pid = self._process.pid
        # Minted with the process it names — random, not pid-derived, so it
        # cannot false-match a later spawn that the OS handed a recycled pid.
        self._process_instance = uuid.uuid4().hex[:16]
        # The subprocess is LIVE from here on but nothing has recorded it yet, so
        # this window needs the same guard AcpClient._spawn has. finish_suspended_spawn
        # documents its own resume failure as FATAL, and _get_start_time can raise;
        # all four runtime.spawn() callers (providers/acp.py:726, :825 catch
        # AcpRuntimeError; session.py:1416, :1490 catch AcpRuntimeDead) let anything
        # else through, so a raise here left a live process absent from both PID
        # files -- unreachable by every agent-runtime reaper and leaking until the
        # host reboots. kill() reaps it before we re-raise.
        #
        # BaseException so a cancellation mid-window cleans up too. This is the same
        # guard as the reader/handshake one below; they stay separate blocks because
        # only the later one has reader/stderr tasks to tear down.
        try:
            # Windows resource ceiling, applied while the child is still SUSPENDED,
            # then resumed. No-op on POSIX (CREATE_SUSPENDED is 0 there). This shared
            # runtime multiplexes many session handles, so an unbounded fork/memory
            # blowup here takes down every session on it, not just one. Offloaded for
            # the same reason as in `AcpClient._spawn`: the Windows path reads config
            # and walks the process and thread tables, and this runtime's event loop
            # is serving every other session while it spawns.
            await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(),
                functools.partial(
                    finish_suspended_spawn, self._process, self._pid, label=f"{KIRO_CLI_BIN} acp"
                ),
            )
            self._start_time = _get_start_time(self._pid)
            self._spawn_monotonic = time.monotonic()
            self._last_activity = time.monotonic()
            if self._scratch_dir is not None:
                # Liveness anchor for the scratch sweeps: a dir whose recorded
                # owner is dead is reclaimable. Off-loop (file write), fail-open
                # (an unowned dir falls under the grace-window rule instead).
                await asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(),
                    functools.partial(agent_scratch.record_owner, self._scratch_dir, self._pid),
                )
        except BaseException:
            logger.error(
                "AcpRuntime: spawn failed after the process was live (PID %s); reaping it "
                "so it cannot leak untracked",
                self._pid,
                exc_info=True,
            )
            try:
                await self.kill()
            except Exception:
                logger.warning(
                    "AcpRuntime: cleanup reap after a failed spawn did not complete for PID %s",
                    self._pid,
                    exc_info=True,
                )
            raise
        logger.info(
            "AcpRuntime spawned backend=%s agent=%s (PID %d)",
            self._acp_backend,
            self._agent or "<none>",
            self._pid,
        )

        # Track the PID for orphan cleanup (mirrors AcpClient._spawn). Without
        # this, a kiro-cli process leaked by a gateway crash/restart is never
        # recorded in kiro_session_pids.txt, so startup cleanup can't reap it.
        # A LIVE runtime is already protected during the periodic sweep because
        # AcpSessionProvider._pid feeds _collect_active_pids — this only closes
        # the cross-restart leak.
        # Shield this shared runtime's PID from the periodic orphan sweep.
        # _bg_runtime and companion subagent runtimes are held only in
        # SessionManager instance attributes (not registered sessions /
        # warm-pool providers), so _collect_active_pids would otherwise
        # classify them as orphans and SIGKILL them mid-use.
        #
        # Ordered BEFORE the two file appends, which is the only ordering that
        # is safe: register_protected_pid is an in-memory set insert under a
        # threading lock with no IO, so it cannot fail for the reasons an append
        # can (ENOSPC, a wedged file lock). Behind the appends it was reachable
        # only if they both succeeded, so one failed append escalated into a
        # LIVE runtime losing its shield and being SIGKILLed mid-use by the very
        # sweep this call exists to hide it from.
        register_protected_pid(self._pid)
        try:
            _track_pid(self._pid)
            _track_session_pid(self._pid)
        except Exception:
            # A runtime that is not in the PID files is unreachable by every
            # agent-runtime reaper: cleanup_orphaned_sessions,
            # _periodic_pid_sweep and cleanup_orphaned_session_roots all read
            # those files, and the /proc orphan scan declines managed agent
            # runtimes on purpose (session_pid._MANAGED_AGENT_MARKERS is a
            # negative gate) precisely because this lifecycle is meant to own
            # them. So the process keeps working, holds hundreds of MB, and
            # leaks for the rest of the host's uptime.
            #
            # ERROR, not debug: this log line is the only signal that will ever
            # be emitted for that leak. A failed PID-file REWRITE is loud for
            # the same reason; this is the append half.
            logger.error(
                "AcpRuntime: PID tracking failed for %s — this runtime is now "
                "invisible to every reaper and will leak until the host reboots",
                self._pid,
                exc_info=True,
            )

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
                    # kiro-cli reads the driving client name from `clientInfo.name`
                    # (agent/acp/acp_agent.rs: `if let Some(info) = request.client_info`),
                    # NOT from a flat `clientName` key. Sending it flat left every
                    # AcpRuntime-driven session (the primary kiro-cli path) unnamed in
                    # telemetry — bucketed as "(none)" instead of "kirocrew". Nest it to
                    # match AcpClient and be picked up for acpClientName attribution.
                    "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                    "protocolVersion": (
                        PROTOCOL_VERSION_KAS
                        if self._acp_backend == ACP_BACKEND_KAS
                        else PROTOCOL_VERSION
                    ),
                    "clientCapabilities": (
                        KAS_CLIENT_CAPABILITIES
                        if self._acp_backend == ACP_BACKEND_KAS
                        else ACP_CLIENT_CAPABILITIES
                    ),
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
            self._agent_version = agent_version_from_init(init_resp)
            self._initialized = True
            logger.info("AcpRuntime initialized (PID %d)", self._pid)
        except BaseException:
            try:
                # This death IS abnormal (failed spawn/handshake): kill()'s
                # expected=False default keeps its log at WARNING.
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

    async def kill(self, *, expected: bool = False) -> None:
        """Kill the subprocess and release spawn resources even when cancelled."""
        try:
            await self._kill_inner(expected=expected)
        finally:
            self._discard_sandbox_cleanup()
            await self._discard_bound_workspace()

    async def _kill_inner(self, *, expected: bool = False) -> None:
        """Kill the subprocess and clean up all state.

        ``expected`` changes log severity only: a deliberate teardown of a
        healthy runtime (pool TTL recycle, session shutdown, logout) passes
        ``expected=True`` to log the death at INFO. The default is False —
        matching ``_mark_dead`` — so every cleanup kill on a failure path
        (``initialize()``'s failed-spawn cleanup, a failed session setup) and
        any future call site stays a WARNING without having to opt in.
        ``_mark_dead`` additionally refuses to downgrade when the process
        already exited on its own, so a reap-after-death can never log INFO.
        """
        # Fail pending futures + poison session queues FIRST. _mark_dead sets
        # self._dead internally; doing it up front (before teardown) ensures any
        # waiters learn the runtime died. Calling it after setting _dead=True
        # would hit its early-return guard and skip all cleanup.
        self._mark_dead("killed", expected=expected)

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
            # The id names the process that just ended; the next spawn mints its
            # own, and nothing may answer with this one in between.
            self._process_instance = ""
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

    # ── Reader Task (single owner of stdout) ──

    def _snapshot_subagent_sessions(self, params: dict) -> None:
        """Replace the known backend-subagent session-id set from a list_update.

        The frame carries the backend's FULL current subagent list (kiro-cli
        rebuilds it from `orchestrated_sessions` on every change), so replacing
        the set keeps it bounded and self-cleaning: terminated children vanish
        from the next update. Ids are backend-controlled — length-capped and
        type-checked so a hostile payload cannot grow memory unboundedly.
        """
        raw = params.get("subagents")
        if not isinstance(raw, list):
            return
        ids: set[str] = set()
        for entry in raw[:256]:
            if not isinstance(entry, dict):
                continue
            sid = entry.get("sessionId") or entry.get("session_id")
            if isinstance(sid, str) and sid and len(sid) <= 128:
                ids.add(sid)
        self._subagent_sessions = ids
        # Ownership is provable only when exactly one session is registered:
        # the announce demonstrably belongs to it. Otherwise no owner, and
        # routing stays fail-closed.
        self._subagent_owner = (
            next(iter(self._session_queues)) if len(self._session_queues) == 1 else None
        )

    async def _wait_for_answer_capacity(
        self,
        msg: JsonRpcMessage,
        *,
        request_kind: str,
        session_id: str = "",
        audit_reason: str | None = None,
    ) -> bool:
        """Wait briefly for shared answer capacity or condemn a wedged pipe.

        Server-to-client requests require a response, so overflowing answers
        cannot take the notification counted-drop path. A responsive backend
        may fill the set with already-buffered requests before completed-task
        callbacks run; one completion admits the current request. No
        completion within the bound means writes are wedged, so marking the
        runtime dead resolves every pending wait instead of leaving the remote
        requester unanswered indefinitely.
        """
        def _deny() -> bool:
            """Refuse admission, recording the decision first.

            A refusal that reaches a caller which had already been admitted to
            wait must leave a SEL record, or a permission decision that denied a
            real tool invocation is indistinguishable from one never made.
            """
            if audit_reason is not None:
                self._audit_denied_off_loop(msg, session_id, audit_reason)
            return False

        # Deliberately NOT audited: on an already-dead runtime a flooding
        # backend's frames are gated out here, and auditing each one would grow
        # audit tasks without bound — the very failure the cap prevents.
        if self._dead:
            return False
        if len(self._answer_tasks) < self._max_answer_tasks:
            return True

        done, _pending = await asyncio.wait(
            set(self._answer_tasks),
            timeout=self._answer_cap_wait_secs,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if done:
            # asyncio schedules task callbacks separately from waking waiters.
            # Remove completed entries here so admitting the replacement never
            # transiently exceeds the shared cap; the callbacks remain an
            # idempotent cleanup backstop.
            self._answer_tasks.difference_update(done)
            if self._dead:
                # A concurrent waiter condemned the runtime while this one was
                # parked: capacity freed but admission still fails, so this
                # refusal owes an audit like any other.
                return _deny()
            return True

        logger.error(
            "answer-task cap (%d) reached at %s request id=%s%s and no "
            "in-flight answer completed in %gs — backend is flooding frames "
            "while not reading stdin; marking runtime dead so every pending "
            "wait resolves",
            self._max_answer_tasks,
            request_kind,
            msg.id,
            f" for session {session_id}" if session_id else "",
            self._answer_cap_wait_secs,
        )
        # Audit before condemning the runtime, so the record for this decision
        # cannot race the wait-resolution _mark_dead triggers.
        refusal = _deny()
        self._mark_dead(f"{request_kind}-answer task cap reached (backend not reading)")
        return refusal

    async def _spawn_answer_task(
        self,
        msg: JsonRpcMessage,
        session_id: str,
        *,
        reason: str = "unregistered_session_auto_reject",
    ) -> None:
        """Spawn a bounded off-loop auto-answer for an unroutable permission request.

        Off-loop because the answer's ``send_response`` can block on stdin
        ``drain()`` against a backend that is not reading — awaiting inline
        would freeze the shared reader (every session's demux) on one hostile
        or wedged backend. Bounded because each blocked task is retained in
        ``_answer_tasks``: a backend that floods permission frames while never
        reading stdin would otherwise grow that set until the gateway OOMs.
        At capacity the shared admission wait either observes progress or
        marks the runtime dead so the requester cannot remain unanswered.
        """
        if not await self._wait_for_answer_capacity(
            msg,
            request_kind="permission",
            session_id=session_id,
            audit_reason="answer_task_cap_runtime_dead",
        ):
            return
        _t = asyncio.ensure_future(
            self._answer_unroutable_permission(msg, session_id, reason=reason)
        )
        self._answer_tasks.add(_t)
        _t.add_done_callback(self._answer_tasks.discard)

    async def _answer_unroutable_permission(
        self,
        msg: JsonRpcMessage,
        session_id: str,
        *,
        reason: str = "unregistered_session_auto_reject",
    ) -> None:
        """Answer a permission REQUEST for a session with no registered queue.

        The ACP contract for a server→client request is that the client always
        replies; kiro-cli's own TUI answers even unowned-session permission
        requests (``cancelled``) rather than dropping them. Auto-reject is
        deliberate and conservative: never auto-approve here — no policy engine
        has seen this tool call, and an approve would grant an invisible
        escalation. Per-frame WARNING is safe (unlike the drop counter's flood
        case): each request corresponds to one pending tool approval and the
        backend cannot re-emit it without a new turn.
        """
        params = msg.params if isinstance(msg.params, dict) else {}
        option_id = _reject_option_id(params)
        if option_id is not None:
            result = {"outcome": {"outcome": "selected", "optionId": option_id}}
        else:
            result = {"outcome": {"outcome": "cancelled"}}
        tool_call = params.get("toolCall")
        raw_title = tool_call.get("title") if isinstance(tool_call, dict) else None
        # The title is backend/LLM-authored and may embed a credential-bearing
        # command line — redact BEFORE truncating (truncation first could clip
        # a secret mid-token so the redaction patterns no longer match, leaking
        # a credential prefix into the logs).
        # Bound the redaction input (backend-controlled) BEFORE the regex
        # passes, generously above the display cap so a clipped secret
        # cannot straddle the boundary the display truncation makes.
        title = (
            redact_text(str(raw_title)[:4096])[:120] if raw_title else "<unknown>"
        )
        logger.warning(
            "auto-rejected permission request id=%s for session %s "
            "(tool: %s, reason: %s): no surface on this client can answer it "
            "right now; answering with %s so the backend subagent gets a tool "
            "error instead of hanging",
            msg.id,
            session_id,
            title,
            reason,
            result["outcome"]["outcome"],
        )
        try:
            # Bounded send: an answer that cannot be written within the
            # timeout means the backend is not reading its stdin at all —
            # the pipe is wedged, and every further frame from it would
            # stack another blocked task (the OOM vector). Marking the
            # runtime dead resolves EVERY pending wait by teardown, so no
            # request is left unanswered and nothing accumulates.
            await asyncio.wait_for(self.send_response(msg.id, result), timeout=30.0)
        except asyncio.TimeoutError:
            logger.error(
                "answer for permission request id=%s could not be written in "
                "30s — backend not reading stdin; marking runtime dead",
                msg.id,
            )
            # Audit BEFORE returning: the denial DECISION was made even
            # though delivery failed — mandatory SEL coverage applies to
            # every decision, not just successfully delivered ones.
            self._audit_denied_off_loop(
                msg, session_id, f"{reason}:send_stalled_runtime_dead", title=title
            )
            self._mark_dead("permission-answer write stalled (backend not reading)")
            return
        except AcpRuntimeDead:
            # Runtime died mid-answer; the backend's wait dies with it.
            self._audit_denied_off_loop(
                msg, session_id, f"{reason}:runtime_dead_mid_answer", title=title
            )
            return
        except Exception:
            # This coroutine runs as a RETAINED TASK off the reader loop, so
            # an unexpected send failure would otherwise be swallowed with
            # the task — the child never gets an answer and waits on a
            # stranded oneshot, the exact hang this path exists to prevent.
            # A response write that fails for any reason other than the
            # already-handled dead-runtime case means the pipe cannot be
            # trusted: log and mark the runtime dead so the child's wait
            # dies with the process instead of hanging invisibly.
            logger.exception(
                "failed to answer unroutable permission request id=%s — "
                "marking runtime dead so the requester cannot hang",
                msg.id,
            )
            self._audit_denied_off_loop(
                msg, session_id, f"{reason}:send_failed_runtime_dead", title=title
            )
            self._mark_dead("unroutable-permission answer failed")
            return
        # Every permission decision is SEL-audited (repo convention; see
        # _audit_denied_off_loop for the off-loop/lazy-import rationale).
        self._audit_denied_off_loop(msg, session_id, reason, title=title)

    def _audit_denied_off_loop(
        self,
        msg: JsonRpcMessage,
        session_id: str,
        reason: str,
        *,
        title: str | None = None,
    ) -> None:
        """SEL-audit a denied permission decision without blocking the caller.

        Every permission decision leaves a SEL record (repo convention; the
        dashboard deny path does the same). Off the calling task because
        ``sel()`` may do blocking filesystem work on first use (e.g. Windows
        ACLs). The decision is already made, so an audit failure must not
        undo or delay it; the failure is swallowed after logging. Lazy
        import: a module-level import of ``kiro_crew.sel`` would be circular
        (same pattern as sandbox.py).
        """
        if title is None:
            _params = msg.params if isinstance(msg.params, dict) else {}
            _tc = _params.get("toolCall")
            _raw = _tc.get("title") if isinstance(_tc, dict) else None
            title = redact_text(str(_raw)[:4096])[:120] if _raw else "<unknown>"
        # Hang-resilience series: every runtime-side denial funnels through
        # here, so one emit covers unroutable/between-turns/cap/send-failure
        # denials. ``reason`` is the closed SEL enum (low-cardinality).
        emit_counter(
            CHILD_PERMISSION_DENIED,
            {"surface": "runtime", "reason": reason},
        )
        request_id = msg.id if isinstance(msg.id, (str, int)) else ""
        # SNAPSHOT the attribution key NOW: the audit closure runs later on a
        # worker thread, and `_subagent_owner` is mutable (unregister/session
        # swap). Reading it at execution time would write the wrong owner —
        # or the bare PID — into an immutable SEL row.
        session_key = f"acp:{self._subagent_owner or self._pid}:{session_id}"

        def _audit() -> None:
            try:
                from kiro_crew.sel import sel

                sel().log_tool_invocation(
                    session_key=session_key,
                    agent="kirocrew",
                    source="acp_runtime",
                    tool_name=title,
                    outcome="denied",
                    request_id=request_id,
                    error=reason,
                )
            except Exception:
                logger.exception("SEL audit for auto-rejected permission failed")

        audit_task = asyncio.ensure_future(asyncio.to_thread(_audit))
        # Retain the task so it cannot be garbage-collected mid-flight; the
        # done callback drops the reference and surfaces nothing (audit
        # failures are already logged inside _audit).
        self._audit_tasks.add(audit_task)
        audit_task.add_done_callback(self._audit_tasks.discard)

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
        # Hang-resilience series: classify the drop by method so dashboards
        # can alert on the pre-fix hang signature. ``method_class`` is a
        # closed 3-value enum — the raw method (backend-controlled) never
        # becomes an attribute value.
        _m = method if isinstance(method, str) else ""
        if _m == METHOD_REQUEST_PERMISSION:
            _mclass = "permission"
        elif _m == METHOD_SESSION_UPDATE:
            _mclass = "update"
        else:
            _mclass = "other"
        emit_counter(DROPPED_FRAMES, {"method_class": _mclass})
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
          4. Request (method + id) with no sessionId → answered ONCE at
             connection level (-32601), never broadcast
          5. No sessionId → broadcast to all queues
        """
        assert self._process and self._process.stdout
        stdout = self._process.stdout

        try:
            while True:
                try:
                    line = await stdout.readuntil(b"\n")
                except asyncio.IncompleteReadError as exc:
                    # EOF, possibly holding a trailing unterminated line. Keep
                    # readline()'s old shape: hand the partial to the parser, and
                    # an empty partial falls through to the exit branch below.
                    line = exc.partial
                except asyncio.LimitOverrunError as exc:
                    # ONE oversize frame must not kill the demux — same invariant
                    # as the non-dict and non-numeric-id guards below. Tearing
                    # the runtime down here ends EVERY multiplexed session
                    # mid-turn, which is what users see as "process exited /
                    # chat failure" after a single huge tool result.
                    #
                    # _drain_oversize_line consumes the whole line THROUGH its
                    # terminating newline and discards it, so the stream is back
                    # on a frame boundary and no byte-slice of the oversize line
                    # ever reaches json.loads. Its budget is per call and needs no
                    # cross-iteration state, because every call that returns ends
                    # on a boundary — so a replay of oversize-but-terminated
                    # frames is survivable frame after frame.
                    #
                    # An awaited request whose response was in a dropped frame is
                    # not orphaned: _send_and_await wraps every future in
                    # wait_for(timeout=...), so the caller gets a timeout instead
                    # of hanging. The ids in flight at the drop are logged so that
                    # timeout is attributable.
                    try:
                        dropped = await _drain_oversize_line(stdout, exc)
                    except asyncio.IncompleteReadError:
                        self._mark_dead("stdout closed mid-oversize-line")
                        return
                    except OversizeLineUnrecoverable as fatal:
                        logger.error("stdout unrecoverable: %s", fatal)
                        self._mark_dead(f"stdout overrun: {fatal}")
                        return
                    logger.warning(
                        "dropped an oversize stdout frame (%d bytes); resynced at "
                        "next frame (in-flight awaited=%s routed=%s): %s",
                        dropped,
                        sorted(self._pending_requests)[:_DROP_IDS_IN_LOG],
                        sorted(self._routed_requests)[:_DROP_IDS_IN_LOG],
                        exc,
                    )
                    continue

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

                # Inbound server→client REQUEST (method + id, no result/error).
                # Crew answers no connection-level request of its own: the KAS
                # engine's credential callback (_kiro/auth/getAccessToken) is
                # served by kiro-cli's relay, not by this host (see
                # :mod:`kiro_crew.acp.kas_transport`). A request that still
                # arrives without a sessionId is therefore unroutable and is
                # answered -32601 by _answer_ownerless_request below, rather
                # than being left to hang.

                # Route notifications by sessionId
                session_id = (msg.params or {}).get("sessionId")
                if not session_id and msg.is_method(METHOD_SUBAGENT_LIST_UPDATE):
                    # Snapshot backend-internal subagent session ids before the
                    # broadcast below delivers the frame to the UI consumers.
                    # Each frame carries the FULL current list, so replace.
                    self._snapshot_subagent_sessions(msg.params or {})
                if session_id:
                    # A frame tagged with a sessionId belongs to exactly one
                    # session. Route to it if registered; otherwise DROP it.
                    # Broadcasting a known-but-unregistered session's frame to
                    # every other session would be cross-talk.
                    queue = self._session_queues.get(session_id)
                    if queue is not None:
                        await queue.put(msg)
                    elif (
                        session_id in self._subagent_sessions
                        and self._subagent_owner is not None
                        and list(self._session_queues) == [self._subagent_owner]
                        and (
                            msg.is_method(METHOD_REQUEST_PERMISSION)
                            or msg.is_method(METHOD_SESSION_UPDATE)
                        )
                    ):
                        # A frame for a backend-internal subagent the backend
                        # itself announced via `subagent/list_update`, on a
                        # runtime with an UNAMBIGUOUS consumer (exactly one
                        # registered session — the dashboard-slot shape).
                        #
                        # - session/update: routed so the consumer's
                        #   per-toolCallId caches capture the child's REAL
                        #   command bytes; the handle re-tags them as crew
                        #   activity, never as parent transcript.
                        # - session/request_permission: routed so the child's
                        #   approval flows through the exact policy pipeline a
                        #   main-agent approval takes — with the command bytes
                        #   above, mode behavior (normal/read/trust/yolo) is
                        #   IDENTICAL to the main agent's. Dropping a REQUEST
                        #   is never an option: it strands the backend's
                        #   response oneshot and wedges the child's whole tool
                        #   batch until process teardown, with every approval in
                        #   that batch hanging invisibly for as long as that
                        #   runtime lives.
                        #
                        # With several registered sessions the frame names no
                        # owner; a permission request then falls to the
                        # fail-closed auto-answer below and updates are
                        # counted drops as before.
                        #
                        # A permission REQUEST is routed only while the owner
                        # has an in-flight prompt (an outstanding routed
                        # request = the dispatch loop is consuming the queue).
                        # Between turns nothing reads the queue until the next
                        # prompt's drain, so a background child's request
                        # would sit unanswered — the original hang with extra
                        # steps. Answer it fail-closed NOW instead.
                        _owner_turn_active = (
                            self._subagent_owner in self._turn_active_sessions
                        )
                        if (
                            msg.id is not None
                            and msg.is_method(METHOD_REQUEST_PERMISSION)
                            and not _owner_turn_active
                        ):
                            await self._spawn_answer_task(
                                msg,
                                session_id,
                                # Registered + announced — the owner just
                                # has no in-flight prompt. A distinct SEL
                                # tag keeps normal background-child
                                # behavior distinguishable from a real
                                # misconfiguration in the audit trail.
                                reason="owner_no_active_turn",
                            )
                            # Yield so spawned answer tasks actually RUN
                            # between frames: with 129+ frames already
                            # buffered, readline() returns without
                            # suspending, and the reader would hit the
                            # flood cap before any answer task had a chance
                            # to complete — falsely killing a responsive
                            # runtime. One loop-tick lets quick answers
                            # drain; a genuinely wedged backend still
                            # accumulates blocked tasks and trips the cap.
                            await asyncio.sleep(0)
                        else:
                            # Hang-resilience series: a child permission
                            # request delivered to the mode-parity pipeline.
                            # Counted so routed requests are observable next
                            # to the dropped-frame counter — a permission
                            # request that is neither routed nor answered
                            # leaves its crew waiting on a prompt nobody can
                            # see.
                            if msg.id is not None and msg.is_method(
                                METHOD_REQUEST_PERMISSION
                            ):
                                emit_counter(
                                    CHILD_PERMISSION_ROUTED, {"surface": "runtime"}
                                )
                            await next(iter(self._session_queues.values())).put(msg)
                    elif msg.id is not None and msg.is_method(METHOD_REQUEST_PERMISSION):
                        # Unannounced or ambiguous: nobody on this client can
                        # see or answer the prompt — answer NOW with the
                        # request's own least-destructive reject option so the
                        # backend subagent gets a tool error instead of
                        # hanging.
                        await self._spawn_answer_task(msg, session_id)
                        # Same yield rationale as the routed-owner branch above.
                        await asyncio.sleep(0)
                    elif self._session_inits_in_flight and (
                        msg.is_method(METHOD_MCP_OAUTH_REQUEST)
                        or msg.is_method(METHOD_MCP_SERVER_INITIALIZED)
                        or msg.is_method(METHOD_MCP_SERVER_INIT_FAILURE)
                    ):
                        # session/new can emit OAuth and MCP registration frames
                        # before its response. The response is what gives
                        # create_session the id needed to register this queue,
                        # so retain the frames until then. Registration frames
                        # matter beyond logging: drain_init() arms its idle
                        # shortcut on the first one, so dropping them here would
                        # make every warm session look report-less and pay the
                        # full no-report ceiling.
                        self._pending_init_notifications.append(msg)
                    else:
                        # Counted, not logged per frame: this is the measured
                        # flood (transcript replay during session/load, plus any
                        # backend still streaming after teardown).
                        self._note_dropped_frame(session_id, msg.method)
                    continue

                # No sessionId. An id-carrying frame that still has a method is
                # a server→client REQUEST that names no session — it expects
                # exactly ONE response, so the runtime answers it at connection
                # level (same shape as the KAS auth callback above) instead of
                # broadcasting. Broadcasting would hand it to EVERY registered
                # session's dispatch loop, each of which replies -32601 on the
                # shared stdin: one id, N responses — a JSON-RPC protocol
                # violation that widens with session sharing. Frames with an id
                # but NO method are responses (e.g. a result of null slips past
                # the result/error check above); their handling is unchanged.
                if msg.id is not None and msg.method is not None:
                    # Same volume bound as the permission auto-answers: each
                    # reply can block on stdin drain() against a backend that
                    # floods frames while never reading, so the task must be
                    # retained (a bare ensure_future can be GC'd mid-flight)
                    # and counted. Past the cap the frame takes the counted-
                    # drop path — the flooding backend hangs on its own
                    # unanswered request instead of growing the task set.
                    if len(self._answer_tasks) >= self._max_answer_tasks:
                        self._note_dropped_frame(_DROP_NO_SESSION, msg.method)
                        continue
                    _t = asyncio.ensure_future(
                        self._answer_ownerless_request(msg.id, msg.method)
                    )
                    self._answer_tasks.add(_t)
                    _t.add_done_callback(self._answer_tasks.discard)
                    continue

                # No sessionId → genuinely global notification; broadcast to all.
                if self._session_queues:
                    # Snapshot: `await queue.put` yields, and a concurrent
                    # unregister_session() could pop mid-iteration otherwise.
                    _queues = list(self._session_queues.values())
                    # Fanning one ownerless frame out to SEVERAL sessions means
                    # at most one recipient produced it and nothing says which,
                    # so mark it: a consumer that measures its own activity (the
                    # subagent idle-stall clock) must not count another tenant's
                    # traffic. A lone session IS the sole owner, so it is left
                    # unmarked and keeps reading the frame as its own.
                    if len(_queues) > 1:
                        msg.fanout_no_owner = True
                    for queue in _queues:
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

    async def _answer_ownerless_request(
        self, request_id: int | str, method: str
    ) -> None:
        """Answer a server→client request that names no session with -32601.

        Runs OFF the reader loop (same shape as the KAS auth callback) so a
        stalled stdin drain cannot block stdout demux for every multiplexed
        session. The routed case — an unknown request WITH a sessionId — is
        deliberately not handled here: it is delivered to that session's queue
        and answered once by its dispatch loop (``server_request_unknown``).
        """
        logger.debug(
            "Ownerless server request answered -32601 — method=%s id=%r",
            method,
            request_id,
        )
        try:
            await self.send_error(
                request_id, _JSONRPC_METHOD_NOT_FOUND, "Method not found"
            )
        except AcpRuntimeDead:
            pass

    def saw_not_logged_in(self) -> bool:
        """True if kiro-cli reported an auth failure on stderr.

        Lets callers translate a runtime death into AcpAuthRequired (an
        actionable login prompt) instead of a generic process-death error —
        parity with AcpClient, which inspects stderr the same way.

        That parity is what this now actually delivers. The check used to be a
        single regex for the literal banner ``not logged in``, while AcpClient's
        error-frame path recognised the full auth vocabulary; a real expired
        bearer token writes ``AccessDeniedException: "Invalid token"`` and ``the
        bearer token included in the request is invalid`` and says ``not logged
        in`` nowhere, so this returned False on exactly the state it exists to
        detect, and the operator was shown a ``session/new`` timeout instead.

        Reads the latch, not the ring buffer: see ``_saw_auth_failure``.
        """
        return self._saw_auth_failure

    def _mark_dead(self, reason: str, *, expected: bool = False) -> None:
        """Mark runtime dead, fail all pending requests, poison all session queues.

        ``expected`` selects only the log severity: a deliberate teardown (a
        warm-pool TTL recycle, a session shutdown) logs at INFO, while every
        genuine death path (process exit, reader crash, broken pipe, ...) keeps
        today's WARNING. The default is False so any death path added later is
        a WARNING without having to opt in. Everything else — the ``_dead``
        early return, PID unshielding, failing pending futures, poisoning
        session queues — is identical on both paths.
        """
        if self._dead:
            return
        self._dead = True
        # A process that already exited on its own is a genuine death being
        # reaped, not a teardown this caller initiated — refuse the downgrade
        # regardless of call site. This closes the race where a replacement
        # path observes is_alive() == False (returncode set by the child
        # watcher) and kill()s before the reader loop has marked the death.
        if expected and self._process is not None and self._process.returncode is not None:
            expected = False
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
        log = logger.info if expected else logger.warning
        log(
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
        # The departing session takes its subagent ownership with it: a later
        # session on this warm runtime must never inherit a stale child's
        # approvals (the announce set is re-learned from the next
        # subagent/list_update, which re-establishes ownership explicitly).
        self._turn_active_sessions.discard(session_id)
        if self._subagent_owner == session_id:
            self._subagent_owner = None
            self._subagent_sessions = set()
        logger.debug("Removed session %s", session_id)

    def mark_turn_active(self, session_id: str, active: bool) -> None:
        """Record whether a session's prompt dispatch loop is consuming.

        Called by ``AcpSessionHandle.prompt()`` on entry and (in a finally)
        on every exit path. Child permission requests are routed only while
        the owner is marked active; otherwise they are answered fail-closed
        immediately, because nothing reads the queue until the next turn.
        """
        if active:
            self._turn_active_sessions.add(session_id)
        else:
            self._turn_active_sessions.discard(session_id)

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
                        self._session_teardown_method(),
                        {"sessionId": session_id},
                        timeout=_TERMINATE_TIMEOUT,
                    )
                except Exception:
                    logger.debug(
                        "session teardown failed for %s (runtime dead/slow); "
                        "proceeding with local unregister",
                        session_id,
                        exc_info=True,
                    )
        finally:
            self.unregister_session(session_id)

    def _session_teardown_method(self) -> str:
        """The verb that frees one session on this backend.

        kiro-cli's terminate evicts the session from the process and leaves the
        transcript on disk for the caller to deal with. KAS offers no evict-only
        equivalent, so its delete does both at once.

        That difference is invisible here but matters to the caller: on KAS the
        local ``AcpSessionHandle._cleanup_transcript`` is a NO-OP, because it
        unlinks from kiro-cli's sessions dir and KAS keeps its own store. So the
        ``keep_transcript`` guard does not protect anything on KAS — a KAS
        session's record is gone once this verb returns. The only capability that
        loses is opportunistic ``spawn_continue`` on a shared subagent, which
        degrades to a typed ``conversation_gone`` and a re-spawn; explicitly
        continuable runs are dedicated sessions that never reach this path.
        """
        if self._acp_backend == ACP_BACKEND_KAS:
            return METHOD_KAS_SESSION_DELETE
        return METHOD_SESSION_TERMINATE

    # ── Session Management ──

    def _mcp_init_progress(self, expected: Any) -> str:
        """Describe MCP registration progress for a session start that stalled.

        Reads the frames the reader loop already staged in
        ``_pending_init_notifications`` so a session-start timeout can name the
        servers that never reported, instead of reporting only the elapsed
        budget. Must run BEFORE ``_finish_session_init``, which drops that
        buffer once the last in-flight init closes.

        ``expected`` is the ``mcpServers`` array sent with the request. Its
        entries carry the roster names, and that is what makes the ABSENT
        servers nameable rather than only the present ones.

        Reports are runtime-wide rather than per-session: a request that never
        answered has no session id to match its frames against, so a concurrent
        init is called out in the text instead of being silently folded in.
        Likewise the staging deque is bounded, so on a very large fleet the
        reported count is a floor, not an exact tally.
        """
        roster = [
            _sanitize_progress_name(str(e.get("name") or ""))
            for e in (expected if isinstance(expected, list) else [])
            if isinstance(e, dict) and e.get("name")
        ]
        ready: list[str] = []
        failed: list[str] = []
        failure_text: dict[str, str] = {}
        awaiting_auth: list[str] = []
        for msg in self._pending_init_notifications:
            params = msg.params if isinstance(msg.params, dict) else {}
            name = _sanitize_progress_name(
                str(params.get("serverName") or params.get("name") or "")
            )
            if not name:
                continue
            if msg.is_method(METHOD_MCP_SERVER_INITIALIZED):
                if name not in ready:
                    ready.append(name)
            elif msg.is_method(METHOD_MCP_SERVER_INIT_FAILURE):
                # A failed server's error text can carry connection strings or
                # tokens from its startup, so it takes the same scrub the
                # dashboard banner applies before it lands in an exception.
                err, _ = redact_exfiltration_urls(str(params.get("error") or ""))
                err, _ = redact_credentials(err)
                err = _strip_unprintable(" ".join(err.split()))[
                    :_MCP_PROGRESS_ERROR_CAP
                ]
                if name not in failed:
                    failed.append(name)
                if err:
                    failure_text[name] = err
            elif msg.is_method(METHOD_MCP_OAUTH_REQUEST):
                if name not in awaiting_auth:
                    awaiting_auth.append(name)

        reported = set(ready) | set(failed)
        parts: list[str] = []
        if roster:
            # Count only reports that belong to the roster. kiro-cli initializes
            # the agent spec's own servers as well as the session-injected ones,
            # so the staged frames are a SUPERSET of the roster and a raw
            # len(reported) can exceed the denominator -- "2/1 reported". The
            # out-of-roster servers still appear by name in the failed and
            # awaiting-authorization buckets, where naming them is the point.
            parts.append(
                f"{len(reported & set(roster))}/{len(roster)} MCP server(s) reported"
            )
            silent = [n for n in roster if n not in reported]
            if silent:
                parts.append(f"no report from {_capped_names(silent)}")
        else:
            parts.append(f"{len(reported)} MCP server(s) reported, roster unknown")
        if failed:
            parts.append(
                "failed: "
                + _capped_names(
                    [f"{n} ({failure_text[n]})" if n in failure_text else n for n in failed]
                )
            )
        if awaiting_auth:
            parts.append(f"awaiting authorization: {_capped_names(awaiting_auth)}")
        if self._session_inits_in_flight > 1:
            parts.append(
                f"{self._session_inits_in_flight} session inits in flight, "
                "so these reports are runtime-wide"
            )
        return "; ".join(parts)

    def _session_start_stalled(
        self, exc: AcpRequestTimeout, method: str, expected: Any
    ) -> AcpRequestTimeout:
        """Attach MCP progress to a session-start timeout before it reaches the user.

        Session start is the one request whose cost is dominated by work the
        runtime can observe, so the bare budget is the least useful half of the
        answer. Returns a replacement to raise rather than raising here, so the
        caller keeps the ``from exc`` chain.
        """
        progress = self._mcp_init_progress(expected)
        logger.warning("%s stalled: %s", method, progress or "no MCP reports staged")
        if not progress:
            return exc
        return AcpRequestTimeout(f"{exc} ({progress})")

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

    async def _kas_custom_agents(
        self, agent: str, *, member_dispatch: bool = False
    ) -> list[dict[str, Any]] | None:
        """Agent definitions to carry on ``session/new``, or None for kiro-cli.

        kiro-cli takes its agent from the ``--agent`` spawn flag and reads the
        spec off disk itself; KAS has neither, so the definition has to travel
        over the wire or the session silently stays on KAS's default mode.

        Materializes first for the same reason the kiro spawn path does: the
        managed default spec may not exist yet, and reading it is what the
        projection needs. File I/O is offloaded — this runs on the loop.

        The projection needs to know which servers will ALSO arrive as
        session-level broker stubs, because a session-injected server outranks an
        agent-declared one and declaring both is a double registration. Only this
        layer holds the overlay that answers it, so the set is resolved here and
        passed down. ``injection_server_names`` is one file read with no shaping
        (it exists to be callable on the session path) and returns an empty set
        when nothing is stubbed, which is the default.
        """
        if self._acp_backend == ACP_BACKEND_KAS and agent:

            def _build() -> list[dict[str, Any]]:
                try:
                    ensure_agent_materialized(agent)
                except Exception:
                    logger.warning(
                        "pre-session agent materialization failed for %r", agent, exc_info=True
                    )
                try:
                    stubbed = injection_server_names(self._mcp_gateway_overlay, agent)
                except Exception:
                    # An unreadable overlay must not cost the session its agent.
                    # Empty is the SAFE direction here: it declares a stubbed
                    # server twice (the injection still wins) rather than
                    # withholding one that nothing else will supply.
                    logger.debug(
                        "stubbed-server lookup failed for %r; projecting every "
                        "declared server",
                        agent,
                        exc_info=True,
                    )
                    stubbed = frozenset()
                if member_dispatch:
                    # The member's dashboard server arrives as a session-level
                    # entry, exactly like a broker stub — so it must join the
                    # subtraction set for the same reason: an agent spec that
                    # already declares it (the opt-in assignable set) would
                    # otherwise be projected alongside the injection, and the
                    # identity-less spec declaration could shadow the
                    # member-keyed entry (#927 class).
                    from kiro_crew.members import MEMBER_DISPATCH_SERVER

                    stubbed = frozenset(stubbed) | {MEMBER_DISPATCH_SERVER}
                return build_kas_custom_agents(
                    kiro_agents_dir(),
                    agent,
                    stub_server_names=stubbed,
                    member_dispatch=member_dispatch,
                )

            try:
                return await asyncio.to_thread(_build)
            except KasAgentTranslationError as exc:
                # Fail loud: continuing would create a session on KAS's own default
                # mode, which for a restricted agent means running a BROADER agent
                # than the caller asked for.
                raise AcpRuntimeError(
                    f"cannot project agent {agent!r} onto KAS: {exc}"
                ) from exc
        return None

    async def _session_start_budget(self) -> float:
        """The session/new + session/load budget, resolved lazily off-loop.

        ``_resolve_session_start_timeout`` calls ``KiroCrewConfig.load()``,
        which on a cache miss is a synchronous disk read + schema validation
        — never run it on the event loop. Resolved once per runtime and
        cached: the request paths must not re-read config per call, and a
        changed config value applies to newly spawned runtimes (same
        snapshot semantics as ``watchdog.*`` in session_handle.py).
        """
        if self._session_start_timeout is None:
            self._session_start_timeout = await asyncio.to_thread(
                _resolve_session_start_timeout
            )
        return self._session_start_timeout

    async def create_session(
        self,
        cwd: str | Path | None = None,
        agent: str | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        crew_agent: str | None = None,
        member_session_key: str = "",
    ) -> AcpSessionHandle:
        """Create a new ACP session on this runtime. Returns a session handle.

        ``crew_agent`` is the canonical Kiro Crew identity for THIS session;
        None falls back to the runtime's own (spawn-time or rekeyed) identity.

        ``member_session_key`` marks a crew member's DM session and carries its
        session key: the dashboard session-control server is mounted as a
        session-level entry (identity via ``KIROCREW_SESSION_KEY``), and the
        KAS wire agent's projection widens to grant its tools. Empty — every
        non-member session — leaves both paths byte-identical to before.
        """
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
        if member_session_key:
            # circular import: members' module graph is heavy; resolved at call
            # time like the projection seams below.
            from kiro_crew.members import member_dispatch_session_server

            member_entry = await asyncio.to_thread(
                member_dispatch_session_server, member_session_key
            )
            if member_entry is not None:
                # Session-level entries outrank same-named spec entries, so drop
                # any stub for the same server rather than registering it twice.
                mcp_servers = [
                    e for e in mcp_servers if e.get("name") != member_entry["name"]
                ] + [member_entry]
            else:
                logger.warning(
                    "member session %s: dashboard server unresolved — the DM "
                    "thread runs as plain chat this session",
                    member_session_key,
                )
        # The agent to run: an explicit request, else the runtime default. KAS
        # has no --agent spawn flag, so its default must be BOTH injected (below)
        # and activated (via set_mode after session/new); the kiro default is
        # already active from the --agent spawn.
        active_agent = agent or self._agent
        # Adapter-only seam: _kas_custom_agents returns None on the kiro backend,
        # so the kiro construction path gains no conditional, no new required
        # argument, and no new failure mode (harness-parity H13).
        kas_agents = await self._kas_custom_agents(
            active_agent, member_dispatch=bool(member_session_key)
        )
        session_work_dir = await self._session_work_dir(cwd)
        params = build_session_new_params(
            session_work_dir,
            mcp_servers=mcp_servers,
            kas_custom_agents=kas_agents,
        )

        budget = await self._session_start_budget()
        self._session_inits_in_flight += 1
        session_id = ""
        try:
            resp = await self._send_and_await(
                METHOD_SESSION_NEW, params, timeout=budget
            )
            session_id = str(resp.get("sessionId") or "")
            if not session_id:
                raise AcpRuntimeError(f"session/new did not return sessionId: {resp}")
        except AcpRequestTimeout as exc:
            # Read the staged MCP reports before the finally below clears them.
            raise self._session_start_stalled(exc, METHOD_SESSION_NEW, mcp_servers) from exc
        finally:
            buffered_init = self._finish_session_init(session_id)

        # Register session queue
        queue: asyncio.Queue[JsonRpcMessage | None] = asyncio.Queue()
        self._session_queues[session_id] = queue
        for msg in buffered_init:
            queue.put_nowait(msg)

        # Resolve the watchdog snapshot OFF the loop before constructing the
        # handle: the load is config file reads + jsonschema validation on a
        # config change, and the handle constructor is synchronous. The crew
        # identity is canonical (a cfg.agents key) — the kiro ``agent`` name
        # is a different namespace and is not stored on the handle.
        _crew = crew_agent if crew_agent is not None else self._crew_agent
        _wd = await asyncio.to_thread(_load_watchdog_settings, _crew)
        handle = AcpSessionHandle(
            session_id=session_id,
            queue=queue,
            runtime=self,
            watchdog=_wd,
            crew_agent=_crew,
            bound_cwd=str(session_work_dir),
        )

        # Populate state from session/new response (configOptions, available models)
        handle.store_session_config(resp)
        # The roster this session put on the wire. Set BEFORE drain_init so the
        # report can be read as "of the N we sent, these reported" rather than
        # as a bare list of names.
        handle.mcp_session_report().begin_session(mcp_servers)

        mode_switched = False
        staged_before_switch = 0
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
        # The agent to ACTIVATE. An explicit request always applies. When a KAS
        # custom agent was injected (``kas_agents`` non-empty) the runtime
        # default must be activated too: KAS has no --agent flag, so an injected
        # default that is not set here stays registered-but-inactive and the
        # session silently runs KAS's own default mode. On kiro ``kas_agents`` is
        # None and the --agent spawn already selected the default, so only an
        # explicit override reaches set_mode here.
        mode_agent = agent or (self._agent if kas_agents else None)
        if mode_agent and self._mode_available(mode_agent, resp):
            # Measured BEFORE the request goes out, which is the only moment the
            # answer is unambiguous: everything queued right now initialized
            # under the pre-switch mode. Reading it after set_mode returns would
            # count the switched-to agent's own registrations — which kiro-cli
            # can emit before it answers — as pre-switch, and those frames are
            # then consumed without being recorded, leaving the panel at a false
            # "no report" for the rest of the session.
            staged_before_switch = handle.queued_frame_count()
            try:
                await self._send_and_await(
                    METHOD_SET_MODE,
                    set_mode_params(session_id, mode_agent),
                )
            except Exception:
                await self.terminate_session(session_id)
                raise
            # Whether set_mode actually SWITCHED modes: the servers that
            # initialized during session/new belong to the mode kiro-cli
            # started the session on. If the requested agent differs, those
            # staged registration frames describe the pre-switch roster and
            # must not arm the drain's idle shortcut while the switched-to
            # agent's own servers may still be booting.
            _ids, _current, _adv = parse_session_modes(resp)
            mode_switched = bool(_current) and mode_agent != _current
        elif mode_agent:
            _ids, _current, _adv = parse_session_modes(resp)
            await self.terminate_session(session_id)
            raise AcpRuntimeError(
                f"Agent mode {mode_agent!r} is not available on this session "
                f"(advertised modes: {_ids or 'none'}); its "
                f"~/.kiro/agents/{mode_agent}.json is likely missing. Refusing to run "
                f"the backend default mode {_current or '(unknown)'} in its place. "
                f"Run `kirocrew setup --agent-only` to materialize the agent config."
            )

        # Drain MCP-server-init / oauth / config notifications before the first
        # prompt so they don't race into the first turn (parity with
        # AcpClient._drain_notifications). Best-effort, bounded: exits shortly
        # after the servers report, or at the no-report ceiling if none do.
        # A runtime declared MCP-free skips the ceiling — nothing can arm it.
        # After a real mode SWITCH, reports staged during session/new describe
        # the pre-switch roster, so they must not arm the idle shortcut.
        if self._expect_mcp_reports:
            await handle.drain_init(
                stale_report_frames=staged_before_switch if mode_switched else 0
            )
        else:
            await handle.drain_init(no_report_ceiling=0.0)

        logger.info("Created session %s on runtime PID %d", session_id, self._pid or 0)
        return handle

    async def probe_advertised_models(self) -> list[dict[str, str]]:
        """Fetch a fresh advertised-model (entitlement) snapshot from this backend.

        A session's ``availableModels`` is captured once, from its own
        ``session/new`` response, and the backend resolves that answer from the
        account state it holds at that instant — a lookup racing a token refresh
        or a cold start can answer with the default (free-tier) set. A long-lived
        session holding such an answer refuses models the account actually has,
        and nothing ever corrects it. This re-asks the question on the SAME live
        process with a throwaway minimal session (no MCP servers, no mode
        activation), terminated before returning.

        Single-flight + short TTL: concurrent callers share one probe, and a
        fresh non-empty answer is reused for :data:`_ENTITLEMENT_PROBE_TTL_SECS`
        so a burst of rejections costs one round-trip.

        Returns the normalized advertised list, or ``[]`` when the probe fails
        or advertises nothing. An empty return is NOT evidence about
        entitlement — callers must keep whatever snapshot they already hold.
        """
        async with self._entitlement_probe_lock:
            now = time.monotonic()
            if (
                self._entitlement_probe_result
                and now - self._entitlement_probe_at < _ENTITLEMENT_PROBE_TTL_SECS
            ):
                return list(self._entitlement_probe_result)
            if not self._initialized or self._dead or self._process is None:
                return []
            params = build_session_new_params(
                await self._session_work_dir(), mcp_servers=[]
            )
            session_id = ""
            self._session_inits_in_flight += 1
            try:
                try:
                    resp = await self._send_and_await(
                        METHOD_SESSION_NEW, params, timeout=_ENTITLEMENT_PROBE_TIMEOUT
                    )
                    session_id = str(resp.get("sessionId") or "")
                finally:
                    # Close the init scope even on failure so staged init
                    # notifications from this probe never leak into a later
                    # real session's queue.
                    self._finish_session_init(session_id)
            except Exception:
                logger.debug("entitlement probe session/new failed", exc_info=True)
                return []
            try:
                fresh = parse_advertised_models(resp)
            finally:
                if session_id:
                    # Evict the probe session from the shared process; never
                    # raises (best-effort by contract).
                    await self.terminate_session(session_id)
            if fresh:
                self._entitlement_probe_result = list(fresh)
                self._entitlement_probe_at = time.monotonic()
            return fresh

    async def load_session(
        self,
        session_file: str,
        resume_sid: str,
        cwd: str | Path | None = None,
        agent: str | None = None,
        crew_agent: str | None = None,
        member_session_key: str = "",
    ) -> AcpSessionHandle:
        """Resume a prior session via session/load — mirrors AcpClient.

        Unlike create_session()+handle.load(), this issues session/load
        DIRECTLY (no session/new first), using the ORIGINAL sid as sessionId
        and passing cwd + the pooled broker stubs + the full transcript path,
        exactly as AcpClient._initialize_session does. This avoids the
        double-session footgun (fresh session/new context replayed on top of
        the loaded transcript) that produced stopReason='refusal'. Raises on
        failure so the caller can fall back to create_session().

        ``member_session_key`` mirrors create_session(): session/load
        re-initializes the session's MCP servers and re-registers the wire
        agent, so a member session resumed WITHOUT the same injection loses
        its dispatch tools mid-conversation — the mount must ride every path
        that (re)establishes the session's tool set, not just the first one.
        """
        if not self._initialized:
            raise AcpRuntimeError("Runtime not initialized — call spawn() first")
        if not self._can_load_session:
            raise AcpRuntimeError("Backend does not advertise session/load support")

        # Re-declare the pooled broker stubs so a resumed session keeps talking
        # to the broker — same injection as create_session() and the AcpClient
        # resume path (client.py). session/load re-initializes the session's MCP
        # servers (see the budget note below), so an empty list here is APPLIED,
        # not ignored: the stubs stop shadowing the agent spec's same-named
        # entries and kiro-cli spawns its own copy of every pooled server,
        # silently un-pooling the session for the rest of its life. Resolved off
        # the event loop — the overlay lookup stats and reads files. Empty when
        # the shared gateway is disabled, so non-pooled installs still send [].
        active_agent = agent or self._agent
        mcp_servers = await asyncio.to_thread(
            pooled_session_servers, self._mcp_gateway_overlay, active_agent
        )
        if member_session_key:
            # circular import: members' module graph is heavy; resolved at call
            # time, same as create_session().
            from kiro_crew.members import member_dispatch_session_server

            member_entry = await asyncio.to_thread(
                member_dispatch_session_server, member_session_key
            )
            if member_entry is not None:
                mcp_servers = [
                    e for e in mcp_servers if e.get("name") != member_entry["name"]
                ] + [member_entry]
            else:
                logger.warning(
                    "member session %s: dashboard server unresolved on resume — "
                    "the DM thread runs as plain chat this session",
                    member_session_key,
                )
        load_params: dict[str, Any] = {
            "sessionId": resume_sid,
            "cwd": str(await self._session_work_dir(cwd)),
            "mcpServers": mcp_servers,
        }
        if session_file:
            # Only kiro-cli is handed a transcript path. A backend that locates
            # the session itself from sessionId is called with an empty path, and
            # sending the field anyway would advertise a path that does not exist.
            load_params["_meta"] = {"_kiro.dev/session_file": session_file}
        # Re-inject the agent definition, for the same reason create_session()
        # does: KAS registers client agents per session and has no --agent flag,
        # so a resumed session that is not handed them again advertises only the
        # modes it can find on disk. That set is NOT a superset of what
        # session/new had — KAS skips an agent profile written for kiro-cli — so
        # omitting this made the requested mode genuinely absent on resume, and
        # Guard A below then refused the load rather than run the backend default.
        #
        # Guarded on the backend rather than relying on _kas_custom_agents()
        # answering None, so the kiro resume path reaches a comparison and stops:
        # no awaited step, nothing to unwind, no shared coroutine that could grow
        # a failure mode later. create_session() enters the same seam
        # unconditionally, which is the shape this one deliberately does NOT copy
        # — reading a backend and stopping is the smallest non-zero delta the kiro
        # path can take for KAS behaviour to exist here at all, and it is the
        # positive `== ACP_BACKEND_KAS` dispatch harness-parity H5 asks for (see
        # _deliver_kas_access_token, and six other call sites in this file).
        # H13 governs the REGISTRATION seam — ProviderRegistry and
        # create_provider_factory, per its own row in harness-parity.md — not
        # per-request dispatch inside the runtime; a reading that reached here
        # would forbid those seven shipped call sites too.
        if self._acp_backend == ACP_BACKEND_KAS:
            attach_kas_custom_agents(
                load_params,
                await self._kas_custom_agents(
                    active_agent, member_dispatch=bool(member_session_key)
                ),
            )
        budget = await self._session_start_budget()
        self._session_inits_in_flight += 1
        loaded_session_id = ""
        try:
            # session/load is gated by the SAME MCP (re-)initialization as
            # session/new — kiro-cli re-initializes the session's servers on
            # load, and the runtime stages mcp/oauth_request frames while
            # EITHER request is in flight (the _session_inits_in_flight-keyed
            # staging in _reader_loop, closed by _finish_session_init; see
            # docs/system-specs/modules/acp-client.md "loading a session
            # triggers MCP re-initialization") — so it gets the same budget.
            resp = await self._send_and_await(
                METHOD_SESSION_LOAD, load_params, timeout=budget
            )

            # A genuine resume echoes "modes" in the response (same signal AcpClient
            # keys on). Anything else means load did not actually restore state.
            if "modes" not in resp:
                raise AcpRuntimeError(
                    f"session/load did not resume session {resume_sid}: {resp}"
                )
            loaded_session_id = resume_sid
        except AcpRequestTimeout as exc:
            # Read the staged MCP reports before the finally below clears them.
            raise self._session_start_stalled(exc, METHOD_SESSION_LOAD, mcp_servers) from exc
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

        # Mirrors create_session: a resumed session gets the same
        # canonical-crew watchdog snapshot, resolved off-loop.
        _crew = crew_agent if crew_agent is not None else self._crew_agent
        _wd = await asyncio.to_thread(_load_watchdog_settings, _crew)
        handle = AcpSessionHandle(
            session_id=resume_sid,
            queue=queue,
            runtime=self,
            watchdog=_wd,
            crew_agent=_crew,
            bound_cwd=str(load_params["cwd"]),
        )
        handle.store_session_config(resp)
        # session/load re-initializes this session's servers, so the resumed
        # session gets its own report against the roster load re-declared.
        handle.mcp_session_report().begin_session(mcp_servers)

        mode_switched = False
        staged_before_switch = 0
        # Activate the agent (mirrors AcpClient step 4 — set_mode applies to a
        # resumed session too, not just fresh ones). If set_mode raises, the
        # caller falls back to create_session() (a fresh sid + its own queue),
        # so terminate this resume_sid session first — session/load already
        # succeeded so kiro-cli holds it; a plain local unregister would leak it
        # in the shared process (and leave the reader routing late transcript-
        # replay frames to an abandoned queue). terminate_session unregisters too.
        if agent and self._mode_available(agent, resp):
            # Same reason as create_session: measured before the request goes
            # out, the only moment "queued" and "pre-switch" mean the same thing.
            staged_before_switch = handle.queued_frame_count()
            try:
                await self._send_and_await(
                    METHOD_SET_MODE,
                    set_mode_params(resume_sid, agent),
                )
            except Exception:
                await self.terminate_session(resume_sid)
                raise
            # See create_session: after a real mode switch, registration frames
            # staged during session/load describe the pre-switch roster.
            _ids, _current, _adv = parse_session_modes(resp)
            mode_switched = bool(_current) and agent != _current
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
        # remain to drain here. MCP-free runtimes skip the no-report ceiling.
        # After a real mode SWITCH, staged reports are pre-switch — don't arm.
        if self._expect_mcp_reports:
            await handle.drain_init(
                stale_report_frames=staged_before_switch if mode_switched else 0
            )
        else:
            await handle.drain_init(no_report_ceiling=0.0)

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

        stage = {
            "initialize": "initialize",
            METHOD_SESSION_NEW: "session_new",
            METHOD_SESSION_LOAD: "session_load",
            METHOD_SET_MODE: "set_mode",
        }.get(method)
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_requests.pop(req_id, None)
            active_starts, queued_starts = _cold_start_counts()
            if self._process is None:
                process_state = "absent"
            elif self._process.returncode is None:
                process_state = "running"
            else:
                process_state = "exited"
            logger.warning(
                "acp_startup_stage stage=%s outcome=timeout timeout_method=%s "
                "timeout_budget_s=%g duration_ms=%.1f active_starts=%d "
                "queued_starts=%d process_state=%s stderr_lines=%d",
                stage or "control",
                method,
                timeout,
                (time.monotonic() - started) * 1000.0,
                active_starts,
                queued_starts,
                process_state,
                min(len(self._stderr_lines), 20),
            )
            # Name the budget: a session-start timeout (90s) must be
            # distinguishable from a generic control-plane one (30s).
            raise AcpRequestTimeout(f"Request {method} timed out after {timeout:g}s")
        if stage is not None:
            active_starts, queued_starts = _cold_start_counts()
            logger.info(
                "acp_startup_stage stage=%s outcome=ready timeout_method=%s "
                "timeout_budget_s=%g duration_ms=%.1f active_starts=%d queued_starts=%d",
                stage,
                method,
                timeout,
                (time.monotonic() - started) * 1000.0,
                active_starts,
                queued_starts,
            )
        return result

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
                    # Latch here, at the sink, because this is the only point at
                    # which every line is guaranteed to have been seen. The
                    # trim above is what makes it necessary: nobody asks about
                    # auth until a request has already timed out, by which time a
                    # chatty startup can have pushed the auth line out of the ring.
                    # Deliberately does not log: the line below already emits this
                    # text at debug, so a second record here would add no content
                    # and only raise arbitrary matched stderr to a default-visible
                    # level, against this sink's own convention. The condition is
                    # surfaced where it is actionable instead -- as AcpAuthRequired.
                    if not self._saw_auth_failure and is_auth_failure_output(text):
                        self._saw_auth_failure = True
                    logger.debug("stderr: %s", text[:200])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # An overlong stderr line (ValueError / LimitOverrunError from
            # readline when no newline fits the buffer) or a low-level read
            # error must not kill this task with an unhandled exception. Log and
            # exit the drain cleanly rather than leaving a dead task behind.
            logger.debug("stderr drain task exiting on error: %s", exc, exc_info=True)
