"""Unified LLM worker pool for Knowledge Library.

Provider-agnostic bounded pool of long-lived workers (CC or ACP).
Knowledge extraction and URL fetch use separate instances of this pool so their
workload policies and session state remain isolated.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from abc import ABC, abstractmethod
from typing import Optional

from kiro_crew import platform_compat
from kiro_crew.agent_sdk.provider_identity import is_claude_code
from kiro_crew.config.paths import config_dir
from kiro_crew.effort import (
    is_valid_effort,
    model_supports_effort,
    select_effort_level,
)
from kiro_crew.sandbox import (
    cgroup_scope_argv,
    create_subprocess_limited,
    wrap_argv,
    wrap_argv_async,
)

try:
    from kiro_crew.acp.client import AcpClient
except ImportError:
    AcpClient = None  # type: ignore[assignment,misc]

# Sweep-protection shield for AcpClient-backed workers. These are direct,
# long-lived AcpClient sessions (not SessionMap sessions / warm-pool providers),
# so the gateway's periodic orphan sweep can't see them via _collect_active_pids
# and would SIGKILL a *busy* worker mid-task as a false orphan ("ACP process
# exited (code=1)"). Registering the live PID here prevents that. Applied inline
# because this pool does NOT ride the shared kiro_crew.acp.worker_pool engine
# (which shields its workers by construction). Imported defensively so llm_pool
# stays importable standalone / in hermetic tests.
try:
    from kiro_crew.session_pid import register_protected_pid, unregister_protected_pid
except Exception:  # pragma: no cover - standalone / test fallback

    def register_protected_pid(pid: int) -> None:  # type: ignore[misc]
        return None

    def unregister_protected_pid(pid: int) -> None:  # type: ignore[misc]
        return None


logger = logging.getLogger(__name__)

DEFAULT_POOL_SIZE = 3
DEFAULT_TIMEOUT = 60.0
FETCH_TIMEOUT = 120.0
AGENT_NAME = "kirocrew-knowledge"
# Knowledge extraction is deliberately high-effort as a LAST RESORT: it is the
# final step of the resolution chain in ``_get_workload_effort`` and only
# applies when neither the config key nor ``agent.role_efforts.background``
# says otherwise. URL fetching passes no explicit effort, so it retains
# provider default.
DEFAULT_EXTRACTION_EFFORT = "high"

# Seconds the pool may sit FULLY idle (no worker checked out) before it is
# scaled to zero — all workers shut down, freeing ~1GB of held process trees.
# Workers respawn lazily on the next acquire(). 0 keeps them warm indefinitely
# (the always-on behaviour).
DEFAULT_IDLE_TTL_SECS = 300.0

# Conversation-recycle thresholds for a checked-out worker.
#
# Pool workers are long-lived sessions, but every prompt they serve is
# self-contained: ``extractor.EXTRACTION_PROMPT`` inlines the whole chunk between
# per-call nonce markers, and ``agent_fetch`` inlines the whole URL, so no call
# depends on anything an earlier call said. The accumulating transcript is
# therefore pure cost — and once it reaches the backend's own ceiling the backend
# auto-compacts, which is a BILLED summarization turn over the entire transcript,
# repeated every few minutes for the rest of a large ingestion.
#
# Recycling the conversation first makes that never happen. The percentage is the
# real signal; the call count is the fallback for backends that report no context
# telemetry at all (a 0% reading is indistinguishable from an empty transcript).
WORKER_RECYCLE_PCT = 50.0
WORKER_RECYCLE_CALLS = 20


# Sandbox modes accepted by ``kiro_crew.sandbox.wrap_argv`` (see its docstring).
# An out-of-set value from config falls back to the safe default rather than
# reaching the subprocess wrapper as an unrecognised string.
_VALID_SANDBOX_MODES = frozenset({"auto", "standard", "strict", "cc", "off"})


def _read_config() -> dict:
    """Read the data home's ``config.json`` once; ``{}`` on any error.

    ``config_dir()`` resolves to ``~/.kiro/crew`` (or ``$KIROCREW_HOME``), so
    this reads ``<data home>/config.json``.

    Synchronous disk read — call from a thread (e.g. ``asyncio.to_thread``) when
    on the event loop, then thread the returned dict into the pure parsers below
    so worker construction never blocks the loop.
    """
    try:
        config_path = config_dir() / "config.json"
        if config_path.exists():
            data = json.loads(config_path.read_text())
            if isinstance(data, dict):
                # Coerce nested sections to dicts so the pure parsers' chained
                # ``.get(...).get(...)`` never hits a hand-edited leaf value
                # (e.g. ``{"agent": "acp"}``) and raises AttributeError.
                for key in ("agent", "knowledge"):
                    if not isinstance(data.get(key, {}), dict):
                        data[key] = {}
                return data
    except Exception:
        pass
    return {}


def _section(data: dict, key: str) -> dict:
    """Return ``data[key]`` if it is a dict, else ``{}``.

    Parsers may be handed a raw dict (bypassing ``_read_config``'s coercion), so
    they guard each nested section independently to stay AttributeError-safe on
    hand-edited config like ``{"agent": "acp"}`` or ``{"knowledge": null}``.
    """
    section = data.get(key, {})
    return section if isinstance(section, dict) else {}


def _get_provider_type(config: Optional[dict] = None) -> str:
    """Configured knowledge provider; defaults to ``"acp"``."""
    data = _read_config() if config is None else config
    provider = _section(data, "agent").get("provider", "acp")
    return provider if isinstance(provider, str) and provider else "acp"


def _get_sandbox_mode(config: Optional[dict] = None) -> str:
    """OS-level sandbox mode for knowledge-worker subprocesses.

    Knowledge workers are wrapped by the same ``wrap_argv`` OS-level sandbox the
    chat/Slack providers use, honouring the operator's ``agent.sandbox`` setting.

    Fallbacks distinguish two cases so a config ERROR can never silently disable
    sandboxing (fail-secure security control):
    - ``sandbox`` **absent/unset** -> ``"auto"``: the intended default, engages
      OS-level isolation and automatically defers to kiro-cli's own internal
      sandbox on macOS when it is enabled (kiro-cli >= 2.13).
    - ``sandbox`` **present but malformed/unrecognised** (typo, wrong type) ->
      ``"auto"``: fail SECURE. A garbage value is a misconfiguration, not an
      intent to run unsandboxed, so we re-enable KiroCrew's OS-level confinement
      rather than degrade to no isolation.
    """
    data = _read_config() if config is None else config
    mode = _section(data, "agent").get("sandbox")
    if mode is None:
        return "auto"  # unset -> intended default (OS-level isolation engaged)
    if isinstance(mode, str) and mode in _VALID_SANDBOX_MODES:
        return mode
    return "auto"  # present but malformed -> fail secure, never silently unsandboxed


def _get_idle_ttl(config: Optional[dict] = None) -> float:
    """Seconds the pool may sit fully idle before scaling to zero.

    Reads ``knowledge.pool_idle_ttl_secs`` (default 300). ``0`` disables the
    reaper (workers stay warm indefinitely). A bool, negative, or typed-wrong
    value falls back to the default rather than silently disabling the reaper.
    """
    data = _read_config() if config is None else config
    value = _section(data, "knowledge").get("pool_idle_ttl_secs", DEFAULT_IDLE_TTL_SECS)
    if isinstance(value, bool):
        return DEFAULT_IDLE_TTL_SECS
    if isinstance(value, (int, float)) and value >= 0:
        return float(value)
    return DEFAULT_IDLE_TTL_SECS


def _get_pool_size(config: Optional[dict] = None) -> int:
    """Configured extraction pool size; defaults to ``DEFAULT_POOL_SIZE``.

    Reads ``knowledge.extraction_pool_size`` (default 3, clamped 1–10).
    """
    data = _read_config() if config is None else config
    value = _section(data, "knowledge").get("extraction_pool_size", DEFAULT_POOL_SIZE)
    if isinstance(value, bool):
        return DEFAULT_POOL_SIZE
    if isinstance(value, int) and 1 <= value <= 10:
        return value
    return DEFAULT_POOL_SIZE


def _normalize_effort(value: object) -> Optional[str]:
    """Return a valid explicit effort level, or ``None`` for provider default."""
    if value is None or value == "":
        return None
    if isinstance(value, str) and is_valid_effort(value):
        return value
    logger.warning("Ignoring invalid Knowledge worker effort: %r", value)
    return None


def _get_workload_effort(
    config: dict,
    key: str,
    fallback: str,
) -> Optional[str]:
    """Resolve a pool's effort: explicit key → role policy → fallback.

    Resolution chain per workload pool (``key`` is ``"extraction_effort"`` or
    ``"fetch_effort"``):

    1. the explicit ``knowledge.<key>`` pin — an operator's deliberate choice;
    2. ``agent.role_efforts.background`` — the existing background-role policy
       these pools used to silently override;
    3. *fallback* — ``DEFAULT_EXTRACTION_EFFORT`` for extraction (the
       pre-config hard default, kept as last resort), ``""`` for fetch
       (provider/model default).

    A key present but invalid or typed-wrong falls through to the role chain,
    NOT to the hard default — a typo must not silently raise extraction cost.
    Pure: never raises, works on a raw (possibly hand-edited) config dict.
    """
    section = _section(config, "knowledge")
    raw = section.get(key)
    if isinstance(raw, str) and is_valid_effort(raw):
        return raw
    if raw not in (None, ""):
        logger.warning(
            "Ignoring invalid knowledge.%s: %r; falling through to role effort",
            key,
            raw,
        )
    role_efforts = _section(config, "agent").get("role_efforts")
    role = role_efforts.get("background") if isinstance(role_efforts, dict) else None
    if isinstance(role, str) and is_valid_effort(role):
        return role
    return fallback if (isinstance(fallback, str) and is_valid_effort(fallback)) else None


class Worker(ABC):
    """Abstract base for a long-lived LLM worker."""

    # Prompts served since this worker's conversation was last reset. The pool
    # reads it to decide when the transcript has grown far enough to be worth
    # dropping (see WORKER_RECYCLE_CALLS). Declared at class level, not set in an
    # ``__init__``, so a subclass that does not chain up still has the counter
    # (``+=`` rebinds it per instance).
    calls_since_reset: int = 0

    @abstractmethod
    async def start(self) -> None:
        """Initialize/spawn the worker process."""

    @abstractmethod
    async def send_message(self, prompt: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        """Send a prompt and return the text response."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Gracefully destroy this worker."""

    @abstractmethod
    def is_alive(self) -> bool:
        """True if this worker can still process messages."""

    def context_pct(self) -> float:
        """Backend-reported context usage for this worker's conversation.

        ``0.0`` when the backend reports nothing (the pool then falls back to the
        call count), so a subclass with no telemetry needs no override.
        """
        return 0.0

    @abstractmethod
    async def reset_conversation(self) -> None:
        """Drop the accumulated transcript, leaving the worker ready to serve.

        Must leave ``is_alive()`` true on success so the pool's dead-worker
        replacement path stays reserved for genuine deaths.
        """


class AcpWorker(Worker):
    """Long-lived AcpClient session with kirocrew-knowledge agent."""

    def __init__(
        self,
        *,
        sandbox_mode: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> None:
        self._client: Optional[AcpClient] = None
        # Pre-resolved by the caller (off the event loop). ``None`` -> resolve
        # lazily in ``start`` (direct construction outside the pool / tests).
        self._sandbox_mode = sandbox_mode
        self._effort = _normalize_effort(effort)
        self._effective_effort: Optional[str] = None
        # PID currently shielded from the gateway orphan sweep (see module note).
        self._protected_pid: Optional[int] = None

    async def start(self) -> None:
        if AcpClient is None:
            raise RuntimeError("AcpClient not available (kiro_crew.acp.client not installed)")
        # Drop any prior client before respawning. send_message re-runs start()
        # when the client is not ready (e.g. a stalled handshake left _session_id
        # None); without this the previous subprocess would be orphaned.
        if self._client is not None:
            # Release the old PID's shield before the process goes away, so a
            # respawn never leaves a dead PID registered.
            if self._protected_pid is not None:
                try:
                    unregister_protected_pid(self._protected_pid)
                except Exception:
                    logger.debug("AcpWorker: unregister protected pid failed", exc_info=True)
                self._protected_pid = None
            try:
                await self._client.shutdown()
            except Exception:
                logger.debug("AcpWorker: stale client shutdown failed", exc_info=True)
            self._client = None
        sandbox_mode = (
            self._sandbox_mode
            if self._sandbox_mode is not None
            else await asyncio.to_thread(_get_sandbox_mode)
        )
        logger.info("AcpWorker: starting with agent=%s", AGENT_NAME)
        self._client = AcpClient(
            agent=AGENT_NAME, sandbox_mode=sandbox_mode, audit_source="subagent"
        )
        self._effective_effort = None
        await self._client.ensure_ready()
        await self._apply_effort()
        # Shield the live worker PID from the periodic orphan sweep for as long
        # as it runs. Paired with unregister in shutdown() and on respawn above.
        pid = getattr(self._client, "_pid", None)
        if isinstance(pid, int) and pid > 0:
            self._protected_pid = pid
            register_protected_pid(pid)
        else:
            self._protected_pid = None
        logger.info(
            "AcpWorker: ready (agent=%s, pid=%s)",
            AGENT_NAME,
            getattr(self._client, "_pid", "unknown"),
        )

    async def _apply_effort(self) -> None:
        """Apply the requested effort without breaking provider-default fallback."""
        client = self._client
        requested = self._effort
        if client is None or requested is None:
            return
        try:
            is_claude = bool(getattr(client, "_is_claude", False))
            if is_claude and not client.supports_config_option("effort"):
                logger.warning(
                    "AcpWorker: effort=%s unsupported; using provider default",
                    requested,
                )
                return
            if not is_claude:
                # The kiro backend rejects /effort on models without effort
                # support ("not available on auto"), and the default model IS
                # "auto" — gate on the model before pushing so a stock install
                # neither warns per spawn nor sends a doomed command. The claude
                # path keeps its own supports_config_option gate above.
                model = getattr(client, "_model", None)
                if not model_supports_effort(model if isinstance(model, str) else None):
                    logger.info(
                        "AcpWorker: model %s does not support effort; " "using provider default",
                        model,
                    )
                    return
            supported = client.get_valid_effort_levels()
            if not isinstance(supported, list):
                supported = []
            effective = select_effort_level(requested, supported)
            if effective is None:
                logger.warning(
                    "AcpWorker: no supported effort at or below %s; " "using provider default",
                    requested,
                )
                return
            if is_claude:
                await client.set_config_option("effort", effective)
            else:
                await client.send_command("/effort", args={"level": effective})
        except Exception:
            logger.warning(
                "AcpWorker: could not apply effort=%s; using provider default",
                requested,
                exc_info=True,
            )
            return
        self._effective_effort = effective
        if effective != requested:
            logger.warning(
                "AcpWorker: effort=%s downgraded to supported effort=%s",
                requested,
                effective,
            )

    async def send_message(self, prompt: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        if self._client is None or not self._client.is_ready:
            await self.start()
        assert self._client is not None
        return await self._client.send_message(prompt, timeout=timeout)

    async def shutdown(self) -> None:
        if self._protected_pid is not None:
            try:
                unregister_protected_pid(self._protected_pid)
            except Exception:
                logger.debug("AcpWorker: unregister protected pid failed", exc_info=True)
            self._protected_pid = None
        if self._client is not None:
            try:
                await self._client.shutdown()
            except Exception:
                logger.debug("AcpWorker shutdown error", exc_info=True)
            self._client = None

    def is_alive(self) -> bool:
        return self._client is not None and self._client.is_process_alive()

    def context_pct(self) -> float:
        client = self._client
        if client is None:
            return 0.0
        try:
            return float(client.last_prompt_stats.context_pct)
        except (AttributeError, TypeError, ValueError):
            return 0.0

    async def reset_conversation(self) -> None:
        """Spawn a fresh ACP session, discarding the accumulated transcript.

        ``start()`` already tears the previous client down (and moves the sweep
        shield to the new PID), so re-running it is the whole reset. It costs one
        cold start per WORKER_RECYCLE_CALLS prompts, far less than the billed
        auto-compaction the growing transcript would otherwise trigger.
        """
        await self.start()
        self.calls_since_reset = 0


class CCWorker(Worker):
    """Long-lived external agent CLI subprocess using stream-json I/O."""

    def __init__(self) -> None:
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
        self._event_queue: asyncio.Queue[Optional[dict]] = asyncio.Queue()
        self._claude_bin: Optional[str] = None

    async def start(self) -> None:
        self._claude_bin = shutil.which("claude")
        if not self._claude_bin:
            raise RuntimeError("claude CLI not found in PATH")
        await self._spawn()

    async def _spawn(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        assert self._claude_bin is not None
        cmd = [
            self._claude_bin,
            "-p",
            "--verbose",
            "--model",
            "haiku",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--permission-mode",
            "bypassPermissions",
        ]
        # Optional URL-fetch tool. Empty by default (no built-in remote fetch on a
        # vanilla machine). Users can opt in by setting KIROCREW_KNOWLEDGE_FETCH_TOOLS
        # to a comma-separated list of MCP tool names their backend exposes.
        fetch_tools = os.environ.get("KIROCREW_KNOWLEDGE_FETCH_TOOLS", "").strip()
        if fetch_tools:
            cmd += ["--allowedTools", fetch_tools]
        wrapped = cgroup_scope_argv(
            (await wrap_argv_async(cmd, _prepare=wrap_argv))[0]
        )  # cgroup DoS ceiling
        self._proc = await create_subprocess_limited(
            *wrapped,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own process group, so the worker's descendants are reachable as a tree.
            # `claude -p` forks helpers (see spine/agent_runner.py's _terminate_group),
            # and without this the child lands in the gateway's OWN group -- which is
            # the one case platform_compat.kill_and_reap deliberately skips its group
            # kill for, so the reap below would degrade to a pid-scoped kill and
            # re-parent those helpers to init.
            #
            # BOTH args, spelled out explicitly, per the recipe in platform_compat:
            # on POSIX start_new_session calls setsid (so killpg reaps the group) and
            # creationflags=0 is a no-op; on Windows start_new_session is silently
            # ignored and CREATE_NEW_PROCESS_GROUP is what makes the child tree
            # `taskkill /T`-reapable. Not **unpacked from a dict -- that defeats
            # mypy's Popen overload resolution on the build fleet.
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
        )
        self._event_queue = asyncio.Queue()
        self._reader_task = asyncio.create_task(self._stdout_reader())

    async def _stdout_reader(self) -> None:
        """Background task: read NDJSON lines from stdout into event queue."""
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                text = line.decode().strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                    await self._event_queue.put(event)
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass
        finally:
            await self._event_queue.put(None)

    async def send_message(self, prompt: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        if self._proc is None or self._proc.returncode is not None:
            await self._spawn()
        assert self._proc is not None and self._proc.stdin is not None

        msg = json.dumps({"type": "user", "message": {"role": "user", "content": prompt}})
        self._proc.stdin.write((msg + "\n").encode())
        await self._proc.stdin.drain()

        try:
            return await asyncio.wait_for(self._collect_response(), timeout=timeout)
        except asyncio.TimeoutError:
            await self.shutdown()
            raise

    async def _collect_response(self) -> str:
        """Collect text from events until a result event arrives."""
        text_parts: list[str] = []
        while True:
            event = await self._event_queue.get()
            if event is None:
                raise RuntimeError("CCWorker process died during message")

            event_type = event.get("type", "")

            if event_type == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))

            elif event_type == "result":
                for block in event.get("result", {}).get("content", []):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                break

        return "".join(text_parts)

    async def shutdown(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        if self._proc is not None:
            try:
                await platform_compat.kill_and_reap(self._proc)
            except Exception:
                logger.debug("CCWorker shutdown error", exc_info=True)
            self._proc = None

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def reset_conversation(self) -> None:
        """Respawn the CLI subprocess, discarding the accumulated transcript.

        The transcript lives only in the process's own stream-json conversation,
        so replacing the process is the reset. ``_spawn()`` cancels the old
        reader task and installs a fresh event queue, leaving the worker ready.
        """
        old = self._proc
        await self._spawn()
        if old is not None and old is not self._proc:
            try:
                await platform_compat.kill_and_reap(old)
            except Exception:
                logger.debug("CCWorker: stale process reap failed", exc_info=True)
        self.calls_since_reset = 0


class LLMPool:
    """Bounded pool of long-lived LLM workers.

    Callers may bind a pool to one workload's effort profile. Extraction and URL
    fetch use separate instances so an effort setting cannot leak between them.
    If all workers are busy, callers wait on the semaphore.
    Dead workers are replaced transparently on acquire.
    """

    def __init__(
        self,
        pool_size: int = DEFAULT_POOL_SIZE,
        *,
        effort: Optional[str] = None,
        effort_key: Optional[str] = None,
        fallback_effort: str = "",
        config_pool_size_key: Optional[str] = None,
    ):
        self._pool_size = pool_size
        self._effort = _normalize_effort(effort)
        # Workload-bound effort resolution: when ``effort_key`` is set, the
        # effort is re-resolved from the config read in ``start()`` (explicit
        # key → role policy → ``fallback_effort``), so the pool follows the
        # operator's config; a direct ``effort=`` arg is the pre-resolved
        # override for tests / pure construction.
        self._effort_key = effort_key
        self._fallback_effort = fallback_effort
        # Config keys override ``pool_size`` ONLY for the workload that owns
        # them: extraction binds ``config_pool_size_key="extraction_pool_size"``
        # while the fetch pool passes nothing, so knowledge.extraction_pool_size
        # can no longer silently resize it (or the auto_research pool).
        self._config_pool_size_key = config_pool_size_key
        self._semaphore = asyncio.Semaphore(pool_size)
        self._workers: list[Worker] = []
        self._available: asyncio.Queue[int] = asyncio.Queue()
        self._started = False
        self._provider_type: str = ""
        self._sandbox_mode: str = "auto"
        self._config: dict = {}
        self._start_lock = asyncio.Lock()
        # Idle-TTL scale-to-zero (see DEFAULT_IDLE_TTL_SECS). Set from config in
        # start(); the reaper shuts all workers down once the pool has been fully
        # idle for longer than the TTL, and the next acquire() respawns lazily.
        self._idle_ttl: float = 0.0
        self._in_use: int = 0
        self._idle_since: Optional[float] = None
        self._reaper_task: Optional["asyncio.Task[None]"] = None
        # Workers a reaper is mid-way through shutting down (outside the lock).
        # Kept visible so a concurrent shutdown() that cancels the reaper can
        # still drain any workers it abandoned.
        self._reaping_workers: Optional[list[Worker]] = None

    @property
    def provider_type(self) -> str:
        return self._provider_type

    async def start(self) -> None:
        """Spawn all workers based on configured provider."""
        async with self._start_lock:
            if self._started:
                return
            # Read config once, off the event loop, and reuse for every worker.
            config = await asyncio.to_thread(_read_config)
            self._provider_type = _get_provider_type(config)
            self._sandbox_mode = _get_sandbox_mode(config)
            # A workload-specific config key (knowledge.extraction_pool_size)
            # overrides the constructor size ONLY when the pool declares that
            # key and the operator has explicitly set it in config.
            if self._config_pool_size_key is not None:
                configured_size = _get_pool_size(config)
                explicit = self._config_pool_size_key in _section(config, "knowledge")
                if explicit and configured_size != self._pool_size:
                    self._pool_size = configured_size
                    self._semaphore = asyncio.Semaphore(configured_size)
            # Workload-bound effort: resolve from the config already in hand.
            if self._effort_key is not None:
                self._effort = _normalize_effort(
                    _get_workload_effort(config, self._effort_key, self._fallback_effort)
                )
            self._idle_ttl = _get_idle_ttl(config)
            self._config = config
            try:
                for i in range(self._pool_size):
                    worker = await self._create_worker()
                    self._workers.append(worker)
                    await self._available.put(i)
            except Exception:
                for w in self._workers:
                    try:
                        await w.shutdown()
                    except Exception:
                        pass
                self._workers.clear()
                self._available = asyncio.Queue()
                raise
            self._in_use = 0
            self._idle_since = time.monotonic()
            self._started = True
            if self._idle_ttl > 0 and self._reaper_task is None:
                self._reaper_task = asyncio.create_task(self._idle_reaper())
            logger.info(
                "LLMPool started: %d workers, provider=%s",
                self._pool_size,
                self._provider_type,
            )

    async def _create_worker(self) -> Worker:
        """Create and start a new worker based on provider type."""
        if is_claude_code(self._provider_type):
            worker: Worker = CCWorker()
        else:
            worker = AcpWorker(
                sandbox_mode=self._sandbox_mode,
                effort=self._effort,
            )
        await worker.start()
        return worker

    async def acquire(self) -> tuple[int, Worker]:
        """Acquire a worker from the pool. Blocks if all busy.

        Returns (worker_index, worker). Caller must release after use.
        Transparently restarts the pool if the idle reaper scaled it to zero.
        """
        while True:
            if not self._started:
                await self.start()
            await self._semaphore.acquire()
            async with self._start_lock:
                if not self._started:
                    # The idle reaper scaled the pool to zero while we were
                    # blocked on the (now-discarded) semaphore. Do NOT release
                    # the fresh semaphore that replaced it — just restart and
                    # retry. The stale permit dies with the old semaphore.
                    continue
                # A held permit guarantees a matching index is already queued
                # (release() enqueues before releasing the permit), so this
                # never blocks.
                idx = self._available.get_nowait()
                self._in_use += 1
                self._idle_since = None
                worker = self._workers[idx]

            if not worker.is_alive():
                logger.warning("LLMPool: worker %d dead, replacing", idx)
                try:
                    await worker.shutdown()
                except Exception:
                    pass
                try:
                    new_worker = await self._create_worker()
                except Exception:
                    self.release(idx)
                    raise
                async with self._start_lock:
                    if self._started and idx < len(self._workers):
                        self._workers[idx] = new_worker
                        worker = new_worker
                        replaced = True
                    else:
                        replaced = False
                if not replaced:
                    # Pool was shut down / scaled to zero while we respawned.
                    # Discard the fresh worker and restart; the stale permit
                    # dies with the old semaphore (as in the reaped path above).
                    try:
                        await new_worker.shutdown()
                    except Exception:
                        pass
                    continue

            return idx, worker

    def release(self, idx: int) -> None:
        """Return a worker to the pool and track the idle transition.

        Enqueues the index BEFORE releasing the semaphore so a held permit
        always implies an available index (acquire relies on this invariant).
        """
        self._available.put_nowait(idx)
        if self._in_use > 0:
            self._in_use -= 1
        if self._in_use == 0:
            self._idle_since = time.monotonic()
        self._semaphore.release()

    async def _idle_reaper(self) -> None:
        """Scale the pool to zero after it sits fully idle past the TTL.

        Runs while the pool is started; exits once it scales the pool to zero
        (a fresh reaper is created by the next start()) or the pool is shut
        down. Cancelled by shutdown().
        """
        ttl = self._idle_ttl
        interval = min(ttl, 30.0) if ttl > 0 else 30.0
        while True:
            await asyncio.sleep(interval)
            if await self._maybe_scale_to_zero():
                return

    async def _maybe_scale_to_zero(self) -> bool:
        """Shut down all workers iff the pool is fully idle past the TTL.

        Returns True if the pool was reaped (or is already gone) — the caller
        (the reaper) should then exit. Returns False to keep polling. The
        worker teardown happens after the pool state is swapped out under the
        lock, so a concurrent acquire() observes ``_started is False`` and
        cleanly restarts rather than racing a half-torn-down pool.
        """
        async with self._start_lock:
            if not self._started:
                return True
            if self._idle_ttl <= 0:
                return False
            if self._in_use != 0 or self._idle_since is None:
                return False
            if (time.monotonic() - self._idle_since) < self._idle_ttl:
                return False
            workers = self._workers
            self._workers = []
            self._available = asyncio.Queue()
            self._semaphore = asyncio.Semaphore(self._pool_size)
            self._started = False
            self._in_use = 0
            self._idle_since = None
            self._reaper_task = None
            # Keep the workers visible to shutdown() so a concurrent shutdown()
            # that cancels us mid-teardown can still drain them.
            self._reaping_workers = workers

        try:
            for worker in workers:
                try:
                    await worker.shutdown()
                except Exception:
                    logger.debug("Worker shutdown error", exc_info=True)
        except asyncio.CancelledError:
            # shutdown() cancelled us mid-teardown; leave _reaping_workers set
            # so shutdown() drains the survivors, then finish cancelling.
            raise
        else:
            self._reaping_workers = None
        logger.info(
            "LLMPool: scaled to zero after %.0fs idle (%d workers freed)",
            self._idle_ttl,
            len(workers),
        )
        return True

    async def send(
        self,
        prompt: str,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> str:
        """Convenience: acquire a worker, send prompt, release, return response."""
        idx, worker = await self.acquire()
        try:
            return await worker.send_message(prompt, timeout=timeout)
        finally:
            try:
                await self._maybe_recycle(idx, worker)
            finally:
                self.release(idx)

    async def _maybe_recycle(self, idx: int, worker: Worker) -> None:
        """Reset *worker*'s conversation once its transcript has grown enough.

        Runs while the worker is still checked out, so no other caller can send
        into a half-reset session. Every knowledge prompt is self-contained, so a
        reset loses nothing — and it must land BEFORE the backend's own
        auto-compaction, which bills a summarization turn over the whole
        transcript every time it fires.

        A failed reset is not fatal: the worker is left reporting dead and
        ``acquire()`` replaces it on the next checkout.
        """
        worker.calls_since_reset += 1
        try:
            pct = worker.context_pct()
        except Exception:
            pct = 0.0
        by_pct = pct >= WORKER_RECYCLE_PCT
        if not by_pct and worker.calls_since_reset < WORKER_RECYCLE_CALLS:
            return
        reason = f"context at {pct:.0f}%" if by_pct else f"{worker.calls_since_reset} calls"
        logger.info("LLMPool: recycling worker %d conversation — %s", idx, reason)
        try:
            await worker.reset_conversation()
        except Exception:
            logger.warning(
                "LLMPool: worker %d conversation reset failed; will be replaced on " "next acquire",
                idx,
                exc_info=True,
            )

    async def send_batch(self, prompts: list[str], timeout: float = DEFAULT_TIMEOUT) -> list[str]:
        """Send multiple prompts concurrently, bounded by pool size.

        Returns responses in same order as prompts.
        """
        if not prompts:
            return []

        results: list[str] = [""] * len(prompts)

        async def _do_one(idx: int, prompt: str) -> None:
            try:
                results[idx] = await self.send(prompt, timeout=timeout)
            except Exception as e:
                logger.warning("LLMPool: batch item %d failed: %s", idx, e)
                results[idx] = ""

        await asyncio.gather(*[_do_one(i, p) for i, p in enumerate(prompts)])
        return results

    async def shutdown(self) -> None:
        """Destroy all workers and stop the idle reaper."""
        task = self._reaper_task
        self._reaper_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 - reaper is being torn down
                pass
        # Swap state out under the lock (mirrors _maybe_scale_to_zero) so a
        # concurrent acquire() observes _started is False rather than racing a
        # half-cleared pool. Also drain any workers a just-cancelled reaper
        # abandoned mid-teardown (stashed on _reaping_workers).
        async with self._start_lock:
            workers = list(self._workers)
            if self._reaping_workers:
                workers.extend(self._reaping_workers)
            self._workers.clear()
            self._reaping_workers = None
            self._started = False
            self._in_use = 0
            self._idle_since = None
        for worker in workers:
            try:
                await worker.shutdown()
            except Exception:
                logger.debug("Worker shutdown error", exc_info=True)
        logger.info("LLMPool: all workers shut down")

    async def __aenter__(self) -> "LLMPool":
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.shutdown()
