"""Start an ``AcpProvider`` session on a shared ``AcpRuntime``.

The cold start of a runtime-backed session: spawn, resume past a stale native session
lock or fall back to a fresh session with history replay, the configured model pin, the
startup telemetry, the session identity the runtime is handed, and the start-time check
for an MCP server that gave the session no tools.

Each function here whose first parameter is ``self`` is an ``AcpProvider`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_client``, ``_defer_replay_sid_promotion``,
``_history_replay_needed``, ``_shared_scratch``.

Instance fields read: ``_accepts_elicitation``, ``_child_fidelity_aware``, ``_crew_agent``,
``_on_gate_acquired``, ``_on_gate_queued``, ``_tool_search``, ``member_context``,
``memory_mode``, ``start_priority``.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import model_scope
from kiro_crew.acp.client import (
    DEFAULT_MODEL,
    AcpAuthRequired,
    advertised_model_ids,
    model_is_unusable,
    resolve_pin_spelling_on,
    sandbox_init_failure_for_runtime,
)
from kiro_crew.acp.mcp_session_report import (
    McpSessionReport,
    sanitize_sink_text,
    servers_exposing_no_tools,
)
from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_mcp import agent_spec_snapshot
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.transport_errors import AcpDelegateLaunchFailed
from kiro_crew.acp.types import ACP_BACKENDS_HARNESS_OWNED_SESSIONS
from kiro_crew.agent_sdk import host_auth
from kiro_crew.agent_sdk.backends import model_registry_namespace
from kiro_crew.agent_sdk.tool_gate import (
    ADAPTER_EXPOSED_CREDENTIAL_LEAVES,
    adapter_expose_files,
    label_for,
)
from kiro_crew.agent_sdk.tool_search import resume_takes_tool_search_replay
from kiro_crew.config.paths import kiro_sessions_dir
from kiro_crew.messaging.link import telemetry_channel_of
from kiro_crew.platform_compat import pid_exists
from kiro_crew.providers.cleanup import _is_safe_path

if TYPE_CHECKING:
    from kiro_crew.providers.acp import AcpProvider

#: The provider's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.providers.acp")


# ── F2 load-recovery: session/load retry past a stale native session lock ──
# A kiro-cli holder killed uncleanly (SIGKILL, crash, OOM, or a gateway-restart
# drain timeout) can leave its per-session lock held briefly. Re-issuing
# session/load a few times with exponential backoff lets the stale lock release
# so the new gateway resumes LOSSLESSLY. If the lock never clears we fall back
# to a fresh session + Kiro Crew history replay (see _start_kiro_runtime_impl).
_RESUME_MAX_ATTEMPTS = 4  # total session/load attempts before fresh fallback
# Bound on the setup failure's own text in the failed-setup warning: enough for
# an RPC error's message, short enough that one line stays one line.
_SETUP_FAILURE_LOG_CAP = 300
# Whole budget for the start-time /mcp + /tools read (see
# AcpProvider._note_zero_tool_servers). Both answer in well under a second; the
# bound only stops a silent backend from holding the start.
_ZERO_TOOL_CHECK_TIMEOUT_SECS = 5.0
_RESUME_BACKOFF_BASE_S = 1.0  # backoff = base * 2**attempt → 1s, 2s, 4s between attempts
# Substrings (matched case-insensitively) of a session/load error that name a
# TRANSIENT native-lock condition — one that clears once the previous holder
# finishes dying — as opposed to a genuine load failure. Two shapes are known:
#
# * "active in another process": the dead holder's lock is still held.
# * "re-read lock file": kiro-cli creates its per-session lock, then re-reads
#   it to confirm it won; when the previous holder's exit handler unlinks the
#   SAME path in that window the load fails with "failed to re-read lock file
#   ...: No such file or directory". Observed on the dashboard's hard-stop
#   path, whose eager respawn issues session/load while the killed holder is
#   still tearing down. Left un-retried this demoted a lossless resume to the
#   lossy conversation-log replay for the rest of the tab's life.
#
# Deliberately the re-read phrase and not a bare "lock file": a permanent lock
# failure ("failed to open lock file: Permission denied") must still fail fast
# to the fresh-session fallback rather than spend the backoff budget first.
_RESUME_TRANSIENT_LOCK_MARKERS: tuple[str, ...] = (
    "active in another process",
    "re-read lock file",
)


def _unlink_dead_session_lock(session_id: str) -> bool:
    """Remove ``<sid>.lock`` when the PID it names is dead; True when removed.

    kiro-cli writes ``{"pid": N, "started_at": ...}`` and deletes it only on a
    clean exit, so a SIGKILLed holder leaves a lock that refuses every later
    session/load. A live holder (including one we may not signal) or a lock we
    cannot read or parse is left alone.
    """
    sessions_dir = kiro_sessions_dir()
    lock = sessions_dir / f"{session_id}.lock"
    if not session_id or not _is_safe_path(lock, sessions_dir):
        return False
    try:
        pid = json.loads(lock.read_text(encoding="utf-8"))["pid"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if type(pid) is not int or pid <= 0 or pid_exists(pid):
        return False
    try:
        lock.unlink()
    except OSError:
        return False
    logger.warning("Removed stale kiro session lock %s (holder PID %d is dead)", lock, pid)
    return True


def _is_transient_resume_lock_error(exc: BaseException) -> bool:
    """True when *exc* from ``session/load`` names a lock race worth retrying."""
    text = str(exc).lower()
    return any(marker in text for marker in _RESUME_TRANSIENT_LOCK_MARKERS)


# What codex writes on stderr when the AWS SDK finds no credential it can read. The
# ACP answer itself is the SDK's generic -32000 ``Authentication required``, which a
# codex signed out of a non-AWS provider gives too, so it alone names no AWS cause.
_MASKED_CREDENTIAL_AUTH_RE = re.compile(r"failed to load aws credentials", re.IGNORECASE)

#: Names the mask in a startup auth failure, so the deliberate layout is not taken
#: for a broken one. Fixed text: it never echoes a path's contents or a secret.
CREDENTIAL_MASK_AUTH_NOTE = (
    "Kiro Crew hides ~/.aws/credentials and ~/.aws/sso/cache from {label} on "
    "purpose; only ~/.aws/config is readable, so Bedrock via {label} needs a "
    "self-contained credential_process in that file, not SSO or static keys. Keep "
    "static keys in ~/.aws/credentials, not ~/.aws/config, and do not make "
    "~/.aws/config a symlink."
)


def _with_credential_mask_note(
    runtime: object, message: str, cause: BaseException, *, read_stderr: bool = True
) -> str:
    """*message*, plus :data:`CREDENTIAL_MASK_AUTH_NOTE` when the mask explains *cause*.

    Added only when all three hold: this spawn ran under a credential mask, that
    mask re-exposes ``~/.aws/config`` (so ``~/.aws`` itself is hidden), and the
    failure or the child's stderr says the AWS SDK could not load a credential.
    Never added twice. ``read_stderr=False`` matches *cause* alone: the stderr
    tail lives as long as the runtime, so a turn must not inherit an old line.
    """
    backend = getattr(runtime, "acp_backend", None)
    hidden = getattr(runtime, "sandbox_hidden_dirs", None)
    if not isinstance(backend, str) or not isinstance(hidden, tuple) or not hidden:
        return message
    config = os.path.join(os.path.expanduser("~"), ".aws", "config")
    if config not in adapter_expose_files(backend, hidden):
        return message
    tail_reader = getattr(runtime, "redacted_stderr_tail", None) if read_stderr else None
    tail = tail_reader() if callable(tail_reader) else ""
    haystack = f"{cause}\n{tail if isinstance(tail, str) else ''}"
    if not _MASKED_CREDENTIAL_AUTH_RE.search(haystack):
        return message
    note = CREDENTIAL_MASK_AUTH_NOTE.format(label=label_for(backend))
    return message if note in message else f"{message} {note}"


def _name_credential_mask(runtime: object, exc: BaseException, *, read_stderr: bool = True) -> None:
    """Append the mask note to *exc*'s own text in place, keeping its type.

    *read_stderr* is passed through to :func:`_with_credential_mask_note`.
    """
    text = str(exc)
    named = _with_credential_mask_note(runtime, text, exc, read_stderr=read_stderr)
    if named != text:
        exc.args = (named,)


def _launch_agent(provider: AcpProvider) -> str:
    """The launch copy kiro-cli is asked for in place of *provider*'s agent, or ``""``.

    Only kiro-cli takes an agent name on its command line and in ``set_mode``;
    every other runtime host keeps the agent's own name.
    """
    launch = getattr(provider, "app_launch_agent", "")
    return launch if isinstance(launch, str) and launch and provider.is_kiro_backend else ""


def _choices_file_may_exist(session_key: str | None = None) -> bool:
    """Whether session choices may be on disk, or ``session_key`` keeps the marker of
    a binding whose file is gone; a check that fails counts as yes. Never while the
    developer flag is off: then every session runs here."""
    from kiro_crew.coder.session_launch import SessionEnvironmentChoices
    from kiro_crew.session_environment import choices_path, session_environments_enabled

    if not session_environments_enabled():
        return False
    try:
        choices_path().stat()
    except (FileNotFoundError, NotADirectoryError):
        if not session_key:
            return False
        return SessionEnvironmentChoices(choices_path()).may_be_bound(session_key)
    except OSError:
        return True
    return True


async def _start_kiro_runtime(self: AcpProvider) -> None:
    """Spawn an AcpRuntime + session; time the kiro cold-start split.

    Thin telemetry wrapper over ``_start_kiro_runtime_impl`` so the DEFAULT
    (kiro) backend's cold start is finally measured. ``AcpClient.ensure_ready``
    already emits ``kirocrew.session.startup.duration`` for the *claude* path
    only; the kiro path went through here with no duration metric. We reuse the
    same histogram, tagged ``backend=<host id>`` + ``phase=<...>`` so the phase split
    (spawn+initialize vs session/new-the-MCP-toolset-load vs set_model) is
    visible. Best-effort — a telemetry failure never affects startup.
    """
    t0 = time.monotonic()
    phases: dict[str, float] = {}
    # Capture the session key BEFORE the impl runs: on the success path it
    # replaces self._client with an AcpSessionProvider whose _session_key
    # starts empty, so reading it at emit time would file every successful
    # cold start under channel=unknown — exactly the case being measured.
    # Same reason for the backend id: this path serves every runtime host, and
    # the metric is filed under the one that actually started.
    meta: dict[str, object] = {
        "session_key": getattr(self._client, "_session_key", None),
        "backend": getattr(self._client, "backend", None),
    }
    outcome = "error"
    try:
        await self._start_kiro_runtime_impl(phases, meta)
        outcome = "ready"
    except AcpAuthRequired:
        outcome = "auth_required"
        raise
    finally:
        self._emit_kiro_startup_metric(t0, phases, outcome, meta)


def _emit_kiro_startup_metric(
    self: AcpProvider,
    t0: float,
    phases: dict[str, float],
    outcome: str,
    meta: dict[str, object] | None = None,
) -> None:
    """Emit the kiro cold-start histogram (total + per-phase). Best-effort.

    Every point from this path is a cold start: ``_start_kiro_runtime_impl``
    always constructs and spawns a fresh ``AcpRuntime``, and the warm
    fast-path returns before reaching here. The Telemetry aggregator splits
    cold from warm on ``spawned``, so it is emitted unconditionally true.

    ``channel`` records WHICH conversation source paid the cost and
    ``resumed`` which construction path ran (``session/load`` vs
    ``session/new``) — without them a duration cannot be attributed to a
    surface or a path, which is the whole diagnostic question for TTFT.
    """
    try:
        # circular import: importing get_recorder at module top would form a
        # config.loader -> ... -> metrics.provider -> config.loader cycle
        # (same reason as the lazy import in acp/client_start.py's ensure_ready).
        # Keep it lazy so provider is never loaded during config.loader's
        # import chain.
        from kiro_crew.metrics.provider import get_recorder

        # The key comes from meta, captured before startup replaced
        # self._client (see _start_kiro_runtime); the live client is only a
        # fallback for callers that emit without seeding meta.
        _meta = meta or {}
        session_key = _meta.get("session_key") or getattr(
            getattr(self, "_client", None), "_session_key", None
        )
        channel = telemetry_channel_of(session_key if isinstance(session_key, str) else None)
        backend = _meta.get("backend")
        base_attrs: dict[str, str | int | bool | float] = {
            # The default host's id is the empty string, so it files as "kiro".
            "backend": backend if isinstance(backend, str) and backend else "kiro",
            "outcome": outcome,
            "spawned": True,
            "channel": channel,
            "resumed": bool(_meta.get("resumed", False)),
        }
        rec = get_recorder()
        total_ms = (time.monotonic() - t0) * 1000.0
        rec.histogram(
            "kirocrew.session.startup.duration",
            total_ms,
            unit="ms",
            attrs={**base_attrs, "phase": "total"},
        )
        for phase, ms in phases.items():
            rec.histogram(
                "kirocrew.session.startup.duration",
                ms,
                unit="ms",
                attrs={**base_attrs, "phase": phase},
            )
        resume_outcome = _meta.get("resume_outcome")
        if resume_outcome:
            rec.counter(
                "kirocrew.session.resume.outcome",
                1,
                attrs={
                    "outcome": str(resume_outcome),
                    "channel": channel,
                },
            )
    except Exception:  # never let telemetry break session startup
        logger.debug("kiro startup metric emit failed", exc_info=True)


def _owning_session_key(self: AcpProvider) -> str:
    """The Kiro Crew session key this provider serves, or ``""``.

    Handed to the runtime's session-creation paths so the session's broker
    stubs carry a token bound to THIS session (see
    ``AcpRuntime._own_stub_session``). Read off the placeholder client the
    same way :meth:`_member_session_key` does; empty for a pooled worker
    spawned before any session claimed it, whose ``rekey()`` names it later.
    """
    skey = getattr(self._client, "_session_key", None)
    return skey if isinstance(skey, str) else ""


def _owning_channel_id(self: AcpProvider) -> str | None:
    """The channel this provider's session belongs to, or ``None``.

    Two readers. The claim frame carries it beside the session key so a
    channel-driven session's forwarded calls keep naming their channel; the
    runtime's session-start paths carry it onto a MIRRORED host's ``mcpServers``
    elements, which is the only channel a codex stdio server has for it --
    codex-rs launches one with ``env_clear()`` plus an allowlist.

    Both readers want ``""`` rather than ``None`` from the runtime's side, so the
    call sites there spell the fallback; this stays ``None`` for the claim frame,
    whose field is omitted rather than sent empty.
    """
    channel = getattr(self._client, "_channel_id", None)
    return channel if isinstance(channel, str) and channel else None


def _delegate_launch_failure(runtime: AcpRuntime) -> AcpDelegateLaunchFailed | None:
    """Translate a startup death into an actionable delegate-launch error, or None.

    Checked on every startup-death path alongside
    :func:`sandbox_init_failure_for_runtime` and :meth:`saw_not_logged_in`: the
    ``kiro-cli-chat`` delegate failing to launch is deterministic (so the pool
    replacing the worker only reproduces it) and is fixed by repairing the Kiro
    CLI install, not by a retry or a model switch. Raised as
    :class:`AcpDelegateLaunchFailed` -- a non-retryable ``AcpError``, NOT an
    ``AcpProcessDied`` -- so the chat surfaces' process-death respawn arms never
    see it and their terminal error arm shows the install-repair message.
    """
    if not runtime.saw_delegate_launch_failure():
        return None
    return AcpDelegateLaunchFailed()


def _member_session_key(self: AcpProvider) -> str:
    """This session's key when it is a member DM on a dispatch-capable backend.

    Empty for every other session. One resolution rule for BOTH session
    establishment paths (session/new and session/load) — the mount must
    ride whichever one runs, or a gateway restart silently strips a member
    thread of its dispatch tools mid-conversation.
    """
    # circular import: members sits above the provider layer.
    from kiro_crew.acp_backends import ACP_BACKENDS_MEMBER_DISPATCH
    from kiro_crew.members import is_member_session_key

    skey = getattr(self._client, "_session_key", None)
    if (
        isinstance(skey, str)
        and self._client.backend in ACP_BACKENDS_MEMBER_DISPATCH
        and is_member_session_key(skey)
    ):
        return skey
    return ""


async def _load_session_with_retry(
    self: AcpProvider,
    runtime: AcpRuntime,
    session_file: str,
    resume_sid: str,
    work_dir: str | Path | None,
    agent: str | None,
    member_session_key: str = "",
    session_key: str = "",
    channel_id: str = "",
) -> AcpSessionHandle | None:
    """Resume via session/load, retrying past a stale native session lock.

    F2 load-recovery (Phase 1). A kiro-cli holder killed uncleanly can leave
    its per-session lock held for a short window; re-issuing session/load a
    few times with exponential backoff lets the stale lock release so we
    resume LOSSLESSLY (full native history). Outcomes:

    * load succeeds            → return the handle (fast path: no sleep).
    * transient lock error     → retry up to ``_RESUME_MAX_ATTEMPTS`` with
      (see _RESUME_TRANSIENT_     backoff; return the handle if it clears.
      LOCK_MARKERS)
    * lock never clears        → return ``None`` (caller falls back to a
                                   fresh session + history replay — Phase 2).
    * any OTHER load error     → return ``None`` immediately (retrying a
                                   genuine failure only wastes time).
    * runtime dies mid-retry   → return ``None`` (caller's respawn handles it).
    """

    async def load() -> AcpSessionHandle:
        return await runtime.load_session(
            session_file,
            resume_sid,
            cwd=work_dir,
            agent=agent or None,
            member_session_key=member_session_key,
            session_key=session_key,
            channel_id=channel_id,
        )

    swept = False
    for attempt in range(_RESUME_MAX_ATTEMPTS):
        try:
            try:
                handle = await load()
            except Exception as exc:
                # A holder that died uncleanly keeps its lock forever, so
                # waiting cannot clear it: remove it and retry once, now.
                if swept or "active in another process" not in str(exc).lower():
                    raise
                swept = True
                if not _unlink_dead_session_lock(resume_sid):
                    raise
                handle = await load()
            if attempt:
                logger.info(
                    "Resume of kiro session %s recovered on attempt %d/%d " "(stale lock released)",
                    resume_sid,
                    attempt + 1,
                    _RESUME_MAX_ATTEMPTS,
                )
            return handle
        except Exception as exc:
            if not _is_transient_resume_lock_error(exc):
                # Genuine load failure — will not clear with time.
                logger.warning(
                    "Failed to resume session %s, starting fresh",
                    resume_sid,
                    exc_info=True,
                )
                return None
            if not runtime.is_alive():
                logger.warning(
                    "Runtime died while retrying resume of session %s; starting fresh",
                    resume_sid,
                )
                return None
            if attempt + 1 < _RESUME_MAX_ATTEMPTS:
                backoff = _RESUME_BACKOFF_BASE_S * (2**attempt)
                logger.info(
                    "Resume of kiro session %s refused (%s); retry %d/%d in %.1fs",
                    resume_sid,
                    str(exc).strip() or type(exc).__name__,
                    attempt + 1,
                    _RESUME_MAX_ATTEMPTS,
                    backoff,
                )
                await asyncio.sleep(backoff)
    # Exhausted every attempt on a persistent lock. Loud, grep-able marker;
    # the caller migrates to a fresh session with Kiro Crew history replay.
    logger.warning(
        "Resume of kiro session %s failed after %d attempts (native lock "
        "still unavailable — a stale lock from an uncleanly-killed holder, a "
        "lock-file race with a holder still tearing down, or a rare "
        "same-gateway session-key alias miss); migrating to a fresh session "
        "with Kiro Crew history replay",
        resume_sid,
        _RESUME_MAX_ATTEMPTS,
    )
    return None


async def _start_kiro_runtime_impl(
    self: AcpProvider, phases: dict[str, float], meta: dict[str, object]
) -> None:
    """Spawn an AcpRuntime and replace self._client with AcpSessionProvider.

    After this, self._client is an AcpSessionProvider that implements the
    same interface as AcpClient, so all downstream method calls work unchanged.

    ``phases`` is populated in-place with per-step wall-clock (ms) — spawn_init,
    session_new, set_model — for the startup histogram in the wrapper.
    """
    # Extract params from the AcpClient that was created in __init__
    # (it was never spawned — just used for config storage)
    work_dir = self._client._work_dir
    agent = getattr(self._client, "_agent", None) or ""
    sandbox_mode = getattr(self._client, "_sandbox_mode", "auto")
    extra_env = getattr(self._client, "_extra_env", None) or {}
    mcp_gateway_overlay = getattr(self._client, "_mcp_gateway_overlay", None)
    mcp_gateway_socket = getattr(self._client, "_mcp_gateway_socket", None)

    # Check for session resume. A direct dashboard turn (dashboard session
    # key with no channel identity) can restore the transcript without
    # restoring Tool Search's activated schemas: the loader still runs, but
    # a tool it reports as loaded remains absent on the next inference. A
    # fresh native session rebuilds that registry, while
    # ``_history_replay_needed`` preserves the Kiro Crew conversation. Linked
    # Slack and other channel dispatchers keep native resume until they own
    # the same replay-lease contract end to end. The decision itself is
    # ``agent_sdk.tool_search.resume_takes_tool_search_replay``, shared with
    # the resume prefetch (the dashboard may not import this layer).
    resume_sid = (
        getattr(self._client, "_resume_session_id", "") if self.memory_mode == "persistent" else ""
    )
    session_key = getattr(self._client, "_session_key", None)
    channel_id = getattr(self._client, "_channel_id", None)
    if resume_sid and resume_takes_tool_search_replay(
        tool_search=self._tool_search,
        backend=self._client.backend,
        channel_id=channel_id,
        session_key=session_key,
    ):
        logger.info(
            "Tool Search is enabled; replacing native session/load for %s "
            "with a fresh session and conversation replay",
            resume_sid,
        )
        self._history_replay_needed = True
        self._defer_replay_sid_promotion = True
        meta["resume_outcome"] = "tool_search_replay"
        resume_sid = ""

    # Preserve the configured model so we can re-apply it once the session
    # is live. AcpClient sends session/set_model in its handshake; the runtime
    # path must do the same or a slot configured with a non-default kiro model
    # would silently run on the agent's default.
    configured_model = getattr(self._client, "_model", "") or ""

    # A restart replaces the process serving this session: the tree that
    # process exposed (read off it, the way parent_work_scratch_dir reads
    # it) is what the replacement joins, so the session's staged work and
    # its children's windows survive the restart. A root's first start has
    # nothing to read and starts its own tree.
    if self._shared_scratch is None:
        previous_tree = getattr(self._client, "work_scratch_dir", None)
        if isinstance(previous_tree, Path):
            self._shared_scratch = previous_tree

    runtime = AcpRuntime(
        work_dir=work_dir,
        agent=agent or "kirocrew",
        sandbox_mode=sandbox_mode,
        extra_env=extra_env,
        mcp_gateway_overlay=mcp_gateway_overlay,
        mcp_gateway_socket=mcp_gateway_socket,
        acp_backend=self._client.backend,
        crew_agent=self._crew_agent,
        tool_search=self._tool_search_settings(),
        member_context=self.member_context,
        memory_mode=self.memory_mode,
        # A dedicated subagent's inherited work directory (agent_scratch):
        # this runtime, not the placeholder client, is the process the
        # subagent runs in, so the second window has to be mounted HERE.
        shared_scratch=self._shared_scratch,
        launch_agent=_launch_agent(self),
    )
    # One priority for both start queues this path enters, set by the session
    # layer for this start (rule: ``kiro_crew.start_priority``).
    start_priority = self.start_priority
    # Persisted choices, including inherited subagent choices, outlive configuration.
    from kiro_crew.coder.session_launch import prepare_for_session
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.session_environment import session_environments_enabled

    config = live.snapshot()
    if config is None:
        config = await asyncio.to_thread(KiroCrewConfig.load)
    section = config.session_environments
    remote = (
        await prepare_for_session(
            session_key, before_compute=runtime.check_remote_workspace_supported
        )
        if session_environments_enabled()
        and (
            bool(section.coder_url or section.profiles)
            or await asyncio.to_thread(_choices_file_may_exist, session_key)
        )
        else None
    )

    async def attach_remote_workspace(target: AcpRuntime) -> None:
        # A fresh guest worker per runtime: the bridge spends its one-use secret.
        assert remote is not None
        target.check_remote_workspace_supported()
        server = await remote.launch.workspace_server(remote.workdir)
        relay = remote.launch.relay
        target.set_remote_workspace(
            {
                "server": server,
                "workdir": remote.workdir,
                "write_token": relay.write_token,
                # Awaited by the runtime's kill, so a reset returns only once the
                # guest workers (and any command they ran) have stopped; the
                # end-watcher below still covers a runtime that dies on its own.
                "close": functools.partial(remote.launch.close_relay, relay),
            }
        )

    from kiro_crew.session_environment import (
        forget_remote_workspace,
        remember_remote_workspace,
    )

    if remote is not None:
        await attach_remote_workspace(runtime)
        # Per-message context names the guest directory, not the gateway project.
        remember_remote_workspace(session_key, remote.profile_name, remote.workdir, owner=runtime)
        if remote.created and getattr(self._client, "_resume_session_id", None):
            # A conversation that already ran got a new, empty workspace.
            from kiro_crew.session_environment import mark_workspace_recreated

            mark_workspace_recreated(session_key)
    else:
        forget_remote_workspace(session_key)
    session_cwd: str | Path = work_dir
    _t_spawn = time.monotonic()
    try:
        await runtime.spawn(
            start_priority=start_priority,
            on_gate_queued=self._on_gate_queued,
            on_gate_acquired=self._on_gate_acquired,
        )
    except AcpRuntimeError as exc:
        # An OS sandbox that refused to build this child is checked FIRST and
        # on all three of this module's startup paths: it is the narrower
        # fact, it is deterministic (so the pool replacing the worker only
        # reproduces it), and a child the sandbox would not start never got
        # far enough to report a credential problem. The verdict comes from
        # the shared translation so these paths cannot disagree with the
        # per-turn one in session_provider.
        sandbox_failure = await sandbox_init_failure_for_runtime(runtime)
        if sandbox_failure is not None:
            raise sandbox_failure from exc
        # A ``kiro-cli-chat`` delegate that would not launch is checked next, for
        # the same reason and on all three paths: deterministic, fixed by repairing
        # the install rather than a retry, so it must not fall through to the
        # generic process-death card the ladder reproduces identically.
        delegate_failure = _delegate_launch_failure(runtime)
        if delegate_failure is not None:
            raise delegate_failure from exc
        # kiro-cli can exit during initialize when not authenticated —
        # surface an actionable login prompt (parity with AcpClient) rather
        # than a generic runtime-death error.
        if runtime.saw_not_logged_in():
            # ``self._client`` is still the placeholder AcpClient at this
            # point, and it carries the backend this runtime was spawned for
            # (it is the value passed as ``acp_backend`` above) — so the
            # sign-in advice names the harness that actually failed to
            # authenticate rather than assuming kiro-cli.
            signed_out = host_auth.signed_out_message(self._client.backend)
            # One synchronous table read, so the kiro path compares and stops (H13).
            if self._client.backend in ADAPTER_EXPOSED_CREDENTIAL_LEAVES:
                signed_out = _with_credential_mask_note(runtime, signed_out, exc)
            raise AcpAuthRequired(signed_out, backend=self._client.backend) from exc
        if self._client.backend in ADAPTER_EXPOSED_CREDENTIAL_LEAVES:
            _name_credential_mask(runtime, exc)
        raise
    finally:
        # subprocess launch + ACP `initialize` handshake
        phases["spawn_init"] = (time.monotonic() - _t_spawn) * 1000.0
        if remote is not None:
            # Closed once this runtime ends (at once if the spawn failed); its
            # placement entry goes with it, so the bounded table holds live ones.
            remote.launch.close_relay_when_ended(
                runtime.is_alive,
                functools.partial(forget_remote_workspace, session_key, owner=runtime),
            )

    # Everything AFTER a successful spawn() must be guarded: until the
    # AcpSessionProvider is constructed and assigned to self._client, NOTHING
    # owns the spawned kiro-cli process, so any failure here would orphan it
    # until the gateway restarts. Kill the runtime on any failure path before
    # re-raising. BaseException (not Exception) so CancelledError/KeyboardInterrupt
    # also clean up. Once self._client = provider runs, the session lifecycle
    # owns the runtime and this guard has already returned.
    try:
        # Resume via session/load when a prior transcript exists, otherwise a
        # fresh session/new. Resume issues session/load DIRECTLY (no
        # session/new first) so it fully mirrors AcpClient and avoids the
        # double-context 'refusal' failure mode.
        # session/new is where kiro loads the full MCP toolset + system prompt
        # (the dominant cold-start cost) — time it as its own phase. The
        # resume attempt is timed separately as ``session_load``: the two are
        # different protocol calls with different costs, and folding them into
        # one phase hid the resume cost that dominates a rebuilt session.
        handle = None
        resumed = False
        if resume_sid:
            if self.is_kas_backend or self._client.backend in (ACP_BACKENDS_HARNESS_OWNED_SESSIONS):
                # Nothing local to stat. KAS locates the transcript itself
                # from sessionId, and in remote-session mode there are no
                # local files at all; a member of
                # ``ACP_BACKENDS_HARNESS_OWNED_SESSIONS`` keeps its own
                # session records and resolves a resume from the id alone.
                # Attempt the load and let failure fall through to a fresh
                # session/new with history replay.
                #
                # Pre-checking the kiro transcript for those hosts makes
                # ``should_load`` permanently False — the file is never
                # written for them — so every reopen would silently start a
                # fresh session and drop the conversation. ``AcpClient``
                # reads the same set for the same reason (client_start.py's
                # ``file_ok`` branch). KAS is named separately because it is
                # NOT a member: adding it would change that client path too.
                session_file = None
                should_load = True
            else:
                session_file = kiro_sessions_dir() / f"{resume_sid}.json"
                should_load = session_file.exists()
            if should_load:
                # F2 load-recovery: retry past a stale "active in another
                # process" lock (Phase 1); on persistent failure fall through
                # to a fresh session/new below WITH Kiro Crew history replay
                # (Phase 2, self._history_replay_needed). This self-heals
                # regardless of WHY the resume failed (SIGKILL'd holder,
                # crash, OOM, drain timeout) — it never depends on the dead
                # holder cooperating.
                _t_load = time.monotonic()
                try:
                    handle = await self._load_session_with_retry(
                        runtime,
                        str(session_file) if session_file else "",
                        resume_sid,
                        session_cwd,
                        agent,
                        member_session_key=self._member_session_key(),
                        session_key=self._owning_session_key(),
                        channel_id=self._owning_channel_id() or "",
                    )
                finally:
                    phases["session_load"] = (time.monotonic() - _t_load) * 1000.0
                resumed = handle is not None
                meta["resume_outcome"] = "loaded" if resumed else "fallback_replay"
                if handle is None:
                    self._history_replay_needed = True
            else:
                logger.info("Session file missing for %s, skipping load", resume_sid)
                meta["resume_outcome"] = "no_session_file"
        meta["resumed"] = resumed

        _t_sess = time.monotonic()

        if handle is None:
            # ── Fix B: respawn if runtime died during resume attempt ──
            # The orphan sweep may have killed the runtime's PID during the
            # session/load window (race condition). Calling create_session on
            # a dead runtime raises AcpRuntimeDead which bubbles to the user.
            # Instead, detect the dead runtime and transparently respawn.
            if not runtime.is_alive():
                logger.warning(
                    "runtime died during resume; respawning for fresh start " "(PID was %s)",
                    runtime.pid,
                )
                try:
                    # Reap of an already-dead runtime: _mark_dead refuses
                    # the expected-downgrade when the child exited on its
                    # own, so this only labels the genuinely-deliberate case.
                    await runtime.kill(expected=True, reason="reap before resume respawn")
                except Exception:
                    pass
                runtime = AcpRuntime(
                    work_dir=work_dir,
                    agent=agent or "kirocrew",
                    sandbox_mode=sandbox_mode,
                    extra_env=extra_env,
                    mcp_gateway_overlay=mcp_gateway_overlay,
                    mcp_gateway_socket=mcp_gateway_socket,
                    acp_backend=self._client.backend,
                    crew_agent=self._crew_agent,
                    # Same wire settings as the first spawn: on a host that
                    # takes Tool Search at initialize, a respawn without them
                    # would run the replayed session with it silently off.
                    tool_search=self._tool_search_settings(),
                    member_context=self.member_context,
                    memory_mode=self.memory_mode,
                    # The dead runtime's tree, for the same reason a restart
                    # joins it (above): its sessions' work is there.
                    shared_scratch=self._shared_scratch or runtime.work_scratch_dir,
                    launch_agent=_launch_agent(self),
                )
                if remote is not None:
                    await attach_remote_workspace(runtime)
                    remember_remote_workspace(
                        session_key, remote.profile_name, remote.workdir, owner=runtime
                    )
                try:
                    await runtime.spawn(
                        start_priority=start_priority,
                        on_gate_queued=self._on_gate_queued,
                        on_gate_acquired=self._on_gate_acquired,
                    )
                except AcpRuntimeError as exc:
                    sandbox_failure = await sandbox_init_failure_for_runtime(runtime)
                    if sandbox_failure is not None:
                        raise sandbox_failure from exc
                    delegate_failure = _delegate_launch_failure(runtime)
                    if delegate_failure is not None:
                        raise delegate_failure from exc
                    if runtime.saw_not_logged_in():
                        signed_out = host_auth.signed_out_message(self._client.backend)
                        if self._client.backend in ADAPTER_EXPOSED_CREDENTIAL_LEAVES:
                            signed_out = _with_credential_mask_note(runtime, signed_out, exc)
                        raise AcpAuthRequired(signed_out, backend=self._client.backend) from exc
                    if self._client.backend in ADAPTER_EXPOSED_CREDENTIAL_LEAVES:
                        _name_credential_mask(runtime, exc)
                    raise
                finally:
                    if remote is not None:
                        remote.launch.close_relay_when_ended(
                            runtime.is_alive,
                            functools.partial(forget_remote_workspace, session_key, owner=runtime),
                        )
            try:
                handle = await runtime.create_session(
                    cwd=session_cwd,
                    agent=agent or None,
                    member_session_key=self._member_session_key(),
                    memory_mode=self.memory_mode,
                    session_key=self._owning_session_key(),
                    channel_id=self._owning_channel_id() or "",
                    on_gate_acquired=self._on_gate_acquired,
                    on_gate_queued=self._on_gate_queued,
                    start_priority=start_priority,
                )
            except AcpRuntimeError as exc:
                sandbox_failure = await sandbox_init_failure_for_runtime(runtime)
                if sandbox_failure is not None:
                    raise sandbox_failure from exc
                delegate_failure = _delegate_launch_failure(runtime)
                if delegate_failure is not None:
                    raise delegate_failure from exc
                if runtime.saw_not_logged_in():
                    signed_out = host_auth.signed_out_message(self._client.backend)
                    if self._client.backend in ADAPTER_EXPOSED_CREDENTIAL_LEAVES:
                        signed_out = _with_credential_mask_note(runtime, signed_out, exc)
                    raise AcpAuthRequired(signed_out, backend=self._client.backend) from exc
                if self._client.backend in ADAPTER_EXPOSED_CREDENTIAL_LEAVES:
                    _name_credential_mask(runtime, exc)
                raise
            finally:
                # In a finally, mirroring session_load: a start that BLEW its
                # budget is the one whose duration matters most, and recording
                # it only after the try left startup telemetry with no row for
                # any failed start. Guarded by the enclosing ``handle is None``,
                # so a successful resume never reports a near-zero session_new
                # that would understate the phase's real distribution.
                phases["session_new"] = (time.monotonic() - _t_sess) * 1000.0

        # Apply the configured model override (mirrors AcpClient handshake).
        # DEFAULT_MODEL ("auto") means "let kiro-cli pick per agent config".
        if configured_model and configured_model != DEFAULT_MODEL:
            # Withhold a model this account cannot run rather than sending
            # it. session/new has already reported what is on offer, and
            # this model was NOT picked for this turn — it comes from the
            # agent spec, the config default, or a slot value persisted
            # before entitlements were known. Sending it anyway is what put
            # a raw "-32603 ... model is not available" in the transcript on
            # every turn: kiro-cli ACCEPTS the id here (so the except below
            # never fires) and only the service rejects it, mid-prompt.
            # Leaving it unset keeps the session on the backend's own
            # default, so the turn succeeds.
            _advertised = advertised_model_ids(handle.available_models)
            _send_model = configured_model
            _namespace = model_registry_namespace(self._client.backend)
            _foreign_scope = not model_scope.pin_applies(
                configured_model,
                _namespace,
                advertised=_advertised,
            )
            if _foreign_scope:
                # Harness ownership and account entitlement are separate
                # decisions. A foreign pin inherits this harness's default.
                _send_model = ""
                logger.info(
                    "Configured model %s belongs to another harness, not %s; "
                    "leaving the session on the backend default",
                    configured_model,
                    _namespace,
                )
            elif model_is_unusable(configured_model, _advertised):
                # A literal miss can be a stale `<namespace>::` qualifier on a
                # model the backend fully serves: resolve to the advertised
                # spelling and send THAT — same fold the display verdict uses,
                # so chip and wire agree. It can also be a BARE pin on a harness
                # that advertises only ``<model>[<effort>]`` rows while its
                # ``model`` option takes the bare id, and the backend-aware
                # resolver answers that with the model the operator pinned,
                # leaving the adapter to own the effort. Try the fold FIRST,
                # against the snapshot we already have: a qualifier-only miss
                # resolves here with no wire traffic and must not pay a
                # throwaway session/new on every cold start.
                _send_model = resolve_pin_spelling_on(
                    configured_model, _advertised, backend=self._client.backend
                )
                if not _send_model:
                    # The fold found nothing, so this looks like a genuine
                    # miss — but the snapshot was captured seconds ago at
                    # session/new, always inside the startup race window where
                    # an entitlement lookup racing a token refresh answers the
                    # free-tier default. Withholding an entitled pin on that
                    # unconfirmed answer drops the user's model for the whole
                    # session with no retry, so revalidate ONCE against the
                    # live backend and re-run both checks. A failed probe
                    # leaves _advertised as it was (fail open: no evidence
                    # never widens or narrows entitlement).
                    try:
                        # force=True: this is a one-shot per cold start
                        # deciding whether to DROP a configured pin, not a
                        # burst, so it must earn a fresh probe rather than
                        # honour a recent no-evidence failure replay.
                        _advertised = (
                            advertised_model_ids(await handle.refresh_available_models(force=True))
                            or _advertised
                        )
                    except Exception:
                        pass
                    if model_is_unusable(configured_model, _advertised):
                        _send_model = resolve_pin_spelling_on(
                            configured_model, _advertised, backend=self._client.backend
                        )
                    else:
                        _send_model = configured_model
            if not _send_model and not _foreign_scope:
                logger.warning(
                    "Configured model %s is not available to this account; "
                    "leaving the session on the backend default (advertised: %s)",
                    configured_model,
                    ", ".join(_advertised),
                )
                # The pin never runs: record it where a caller billing or
                # labelling the turn by the pin reads (model_pin_refused).
                handle.model_pin_refused = configured_model
            elif _send_model:
                _t_model = time.monotonic()
                try:
                    await handle.set_model(_send_model)
                    logger.info("Kiro runtime model set: %s", _send_model)
                except Exception:
                    logger.warning(
                        "Failed to set model %s on kiro runtime session",
                        _send_model,
                        exc_info=True,
                    )
                finally:
                    phases["set_model"] = (time.monotonic() - _t_model) * 1000.0

        # Replace the placeholder AcpClient with the real AcpSessionProvider
        provider = AcpSessionProvider(
            handle,
            runtime,
            owns_runtime=True,
            # This path is the COLD start, which never rekeys — so the
            # correlation keys have to arrive here or the per-turn re-claim
            # pushes a keyless claim gatewayd throws away.
            session_key=self._owning_session_key(),
            channel_id=self._owning_channel_id(),
        )
        provider.memory_mode = self.memory_mode
        if resumed:
            provider.resumed = True
        # Re-apply the consumer's fidelity opt-in: it was set on THIS
        # provider (possibly landing on the placeholder client, now
        # discarded) before startup — without this, the dashboard's
        # opt-in never reaches the handle and its child permission
        # requests are fail-closed instead of shown as a card.
        if self._child_fidelity_aware:
            provider.child_fidelity_aware = True
        # The elicitation opt-in travels the same way, for the same reason.
        if self._accepts_elicitation:
            provider.accepts_elicitation = True
        self._client = provider  # type: ignore[assignment]
        # The tree this session ACTUALLY has, read off the live process:
        # the runtime drops an inherited window that was swept and falls
        # back to its own directory, so the value handed in at
        # construction can name a directory that is gone. A restart that
        # re-sent it would drop it again and start a third tree, losing
        # what this runtime staged; recording the live answer makes the
        # next spawn join THIS directory instead.
        live_tree = runtime.work_scratch_dir
        if isinstance(live_tree, Path):
            self._shared_scratch = live_tree
    except BaseException as setup_exc:
        # No provider owns the runtime yet — kill it so a failed session
        # setup doesn't leak an orphaned kiro-cli process. Best-effort:
        # the cleanup kill must not mask the original exception.
        if isinstance(setup_exc, Exception):
            # Named here, before the cleanup, because the kill's own lines
            # (its attribution, and on Windows a tree still draining) are
            # otherwise the only trace a repeating setup failure leaves at
            # the gateway's WARNING level. A cancellation is not a failure.
            # The text can be backend-authored (an RPC error frame), so it is
            # redacted and folded to one printable, bounded line: a newline or
            # an escape sequence in it must not forge or recolor a log line.
            logger.warning(
                "Kiro session setup failed on runtime PID %s (%s: %s); killing the runtime",
                runtime.pid,
                type(setup_exc).__name__,
                sanitize_sink_text(str(setup_exc), _SETUP_FAILURE_LOG_CAP),
            )
        try:
            await runtime.kill(expected=True, reason="failed session setup cleanup")
        except Exception:
            logger.debug(
                "Cleanup kill of runtime after failed session setup failed",
                exc_info=True,
            )
        raise
    logger.info(
        "Kiro provider started via AcpRuntime (PID %s, session %s, resumed=%s)",
        runtime.pid,
        handle.session_id,
        resumed,
    )


async def _note_zero_tool_servers(self: AcpProvider) -> None:
    """Warn when a started MCP server gave this session no tools.

    On kiro-cli, two servers that publish the same tool name collide: one
    server's tools reach the model and the other's whole set is dropped,
    with no frame saying so. Both servers still report ``running``, so the
    only trace is a server with no tool in the session's ``/tools`` list.
    This reads ``/mcp`` and ``/tools`` once, records any such server on the
    session's MCP report (the dashboard MCP panel and run summaries read
    it), and logs a warning. Naming the clash is all it does: which server
    wins is the backend's tool table, not Crew's.

    MCP Tool Search deferral, on by default, also leaves a running server
    absent from ``/tools``. :func:`servers_exposing_no_tools` tells the two
    apart from the ``/tools`` result itself -- the loader tool is present
    exactly when the session defers -- so a deferred server is not mistaken
    for a clash victim; without that this would warn for nearly every server
    on a default install.

    ``/tools`` is only read when at least two servers are running, since a
    clash needs two. Bounded, and never fails the start: on any error the
    session simply has no warning.
    """
    client = self._client
    if not hasattr(client, "command_result"):
        return
    try:

        async def _read() -> tuple[str, ...]:
            mcp = await client.command_result("/mcp")
            data = mcp.get("data") if isinstance(mcp, dict) else None
            servers = data.get("servers") if isinstance(data, dict) else None
            running = [
                row
                for row in (servers if isinstance(servers, list) else [])
                if isinstance(row, dict) and row.get("status") == "running"
            ]
            if len(running) < 2:
                return ()
            tools = await client.command_result("/tools")
            spec = await asyncio.to_thread(
                agent_spec_snapshot, client._agent, work_dir=self.cwd or None
            )
            return servers_exposing_no_tools(mcp, tools, spec)

        names = await asyncio.wait_for(_read(), timeout=_ZERO_TOOL_CHECK_TIMEOUT_SECS)
    except Exception:
        logger.debug("MCP zero-tool check skipped", exc_info=True)
        return
    report = self.mcp_session_report()
    if not isinstance(report, McpSessionReport):
        return
    report.record_no_tools(names)
    if report.no_tools:
        logger.warning(
            "ACP: MCP server(s) started but gave this session no tools; a tool "
            "name likely clashes with another server's and the backend kept only "
            "one: %s%s",
            ", ".join(report.no_tools),
            f" (+{report.no_tools_omitted} not listed)" if report.no_tools_omitted else "",
        )
