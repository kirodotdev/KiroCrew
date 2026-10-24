"""ACP provider — wraps existing AcpClient behind LLMProvider interface."""

from __future__ import annotations

import asyncio  # noqa: F401
import functools  # noqa: F401
import importlib as _importlib
import json  # noqa: F401
import logging
import sys as _sys
import time  # noqa: F401
from collections.abc import AsyncIterator, Callable  # noqa: F401
from contextlib import aclosing  # noqa: F401
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncContextManager

from kiro_crew import _facade_mirror, model_scope  # noqa: F401
from kiro_crew.acp.client import (  # noqa: F401
    DEFAULT_MODEL,
    AcpAuthRequired,
    AcpClient,
    AcpError,
    _is_config_value_rejection,
    advertised_model_ids,
    catalog_row_would_drop,
    model_is_unusable,
    resolve_pin_spelling_on,
    resolve_prompt_timeout_for_deadline,
    sandbox_init_failure_for_runtime,
)
from kiro_crew.acp.mcp_session_report import (  # noqa: F401
    McpSessionReport,
    sanitize_sink_text,
    servers_exposing_no_tools,
)
from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError  # noqa: F401
from kiro_crew.acp.session_handle import (  # noqa: F401
    _READ_PATH_PROBE_DEADLINE_SECS,
    _READ_PATH_REPROBE_MIN_INTERVAL_SECS,
    AcpSessionHandle,
    EntitlementRevalidating,
)
from kiro_crew.acp.session_mcp import agent_spec_snapshot  # noqa: F401
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import (  # noqa: F401
    ACP_BACKEND_CODEX,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKENDS_ACP_RUNTIME,
    ACP_BACKENDS_COMPACT,
    ACP_BACKENDS_CONTEXT_RECYCLE,
    ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION,
    ACP_BACKENDS_EFFORT_OVERLAY,
)
from kiro_crew.acp.types import (
    ACP_BACKENDS_HARNESS_MANAGED_COMPACTION as ACP_BACKENDS_HARNESS_MANAGED,
)
from kiro_crew.acp.types import (  # noqa: F401
    ACP_BACKENDS_HARNESS_OWNED_SESSIONS,
    ACP_BACKENDS_KIRO_FAMILY,
    ACP_BACKENDS_KIRO_SLASH_COMMANDS,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_MATE_WORKER_KIT,
    ACP_BACKENDS_MEMBER_CAPABILITIES,
    ACP_BACKENDS_MEMBER_TOOL_LIMITS,
    ACP_BACKENDS_NATIVE_SPEC_PROMPT_ACROSS_COMPACTION,
    ACP_BACKENDS_SESSION_SHARING,
    ACP_BACKENDS_TOOL_SEARCH_OVERLAY,
    EVENT_COMPACTION_STATUS,
    PROVIDER_LABEL_BY_BACKEND,
    PROVIDER_LABEL_CLAUDE,
    PROVIDER_LABEL_DEFAULT,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
    effort_config_option_id,
    effort_config_option_value,
    spec_prompt_retention_verified,
)
from kiro_crew.acp_backends import POLICY_ID_BY_BACKEND  # noqa: F401
from kiro_crew.agent_sdk import host_auth
from kiro_crew.agent_sdk.backend_identity import is_claude_backend_name
from kiro_crew.agent_sdk.backends import model_registry_namespace  # noqa: F401
from kiro_crew.agent_sdk.capabilities import SessionCapabilities, capabilities_for
from kiro_crew.agent_sdk.tool_search import (  # noqa: F401
    TOOL_SEARCH_DEFAULT_MIN_PCT,
    TOOL_SEARCH_DEFAULT_MIN_TOKENS,
    ToolSearchSettings,
    clamp_min_pct,
    clamp_min_tokens,
    resume_takes_tool_search_replay,
)
from kiro_crew.atomic_write import atomic_write  # noqa: F401
from kiro_crew.config.paths import kiro_sessions_dir  # noqa: F401
from kiro_crew.constants import COMPACT_WAIT_TIMEOUT_SECS  # noqa: F401
from kiro_crew.effort import (  # noqa: F401
    EFFORT_LEVELS,
    effort_settings_key,
    model_supports_effort,
    resolve_effort_for_model,
)
from kiro_crew.mcp_hot_reload import mcp_hot_reload_supported, parse_kiro_cli_version
from kiro_crew.messaging.link import telemetry_channel_of  # noqa: F401
from kiro_crew.platform_compat import pid_exists  # noqa: F401
from kiro_crew.providers import acp_cli_overlay as _acp_cli_overlay
from kiro_crew.providers import acp_lifecycle as _acp_lifecycle
from kiro_crew.providers import acp_model_effort as _acp_model_effort
from kiro_crew.providers import acp_runtime_start as _acp_runtime_start
from kiro_crew.providers import acp_turns as _acp_turns
from kiro_crew.providers.acp_cli_overlay import _read_cli_overlay
from kiro_crew.providers.base import (  # noqa: F401
    CancelOutcome,
    LLMEvent,
    LLMProvider,
    SessionMcpReport,
    resolve_billing_stats,
)
from kiro_crew.providers.cleanup import _is_safe_path  # noqa: F401
from kiro_crew.recovery.ladder import InfraError
from kiro_crew.session_work_dir import mark_run_dir, reclaim_session_work_dir  # noqa: F401
from kiro_crew.trust_patterns import GrantDeadline
from kiro_crew.workspace_cli_settings import (  # noqa: F401
    CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS,
    CLI_SETTINGS_LOCK_TIMEOUT_SECS,
    workspace_cli_settings_lock,
)

logger = logging.getLogger(__name__)


class AcpProvider(LLMProvider):
    """LLMProvider backed by ACP JSON-RPC over stdio (kiro-cli or claude-agent-acp)."""

    # Fields an owner member first sets, declared so the class names them.
    _picker_probe_at: float

    reports_effort_support = True
    approval_withdrawable = True

    def __init__(
        self,
        work_dir: str | Path | None = None,
        model: str | None = None,
        agent: str | None = None,
        sandbox_mode: str = "auto",
        session_key: str | None = None,
        channel_id: str | None = None,
        extra_env: dict[str, str] | None = None,
        acp_backend: str = "",
        effort_per_model: dict[str, str] | None = None,
        unbound_effort: str = "",
        effort_defaults: object = None,
        tool_search: bool | None = None,
        tool_search_min_pct: object = None,
        tool_search_min_tokens: object = None,
        mcp_gateway_overlay: str | Path | None = None,
        mcp_gateway_socket: str | Path | None = None,
        permission_mode: str | None = None,
        crew_agent: str | None = None,
        member_context: bool = False,
        memory_mode: str = "persistent",
        shared_scratch: Path | None = None,
        on_gate_acquired: Callable[..., None] | None = None,
        on_gate_queued: Callable[..., None] | None = None,
        disposable_work_dir: bool = False,
        side_read_only: bool = False,
    ) -> None:
        # An unrecognized backend would pass every ``_is_<backend>`` check and
        # spawn kiro-cli, so a typo'd config would drive the wrong agent with no
        # error. Fail at construction instead.
        if acp_backend not in ACP_BACKENDS_KNOWN:
            raise ValueError(
                f"Unknown acp_backend {acp_backend!r}; "
                f"expected one of {sorted(ACP_BACKENDS_KNOWN)}"
            )
        kwargs: dict[str, Any] = {
            "work_dir": work_dir,
            "model": model,
            "sandbox_mode": sandbox_mode,
            "session_key": session_key,
            "channel_id": channel_id,
            "extra_env": extra_env,
            "acp_backend": acp_backend,
            "mcp_gateway_overlay": mcp_gateway_overlay,
            "mcp_gateway_socket": mcp_gateway_socket,
            # Claude permission mode (Auto-mode/permission-UI parity). None on the
            # kiro-cli path — fully inert; a companion-registered backend threads
            # it.
            "permission_mode": permission_mode,
            # The parent session tree's work directory for a dedicated subagent
            # process; None for a session that starts its own tree.
            "shared_scratch": shared_scratch,
            # A Side Chat session that may run read-only tools; the claude client
            # starts it confined (AcpClient._claude_session_meta). Inert on kiro-cli.
            "side_read_only": side_read_only,
        }
        if agent:
            kwargs["agent"] = agent
        self.member_context = member_context
        self.memory_mode = memory_mode
        # The name kiro-cli is asked for in place of ``agent`` (an app's launch
        # copy, ``apps.agent_launch``), set by session allocation; ``agent``
        # stays the session's identity. Read by the Kiro runtime start only.
        self.app_launch_agent = ""
        # Kept on the provider, not only on the placeholder client: on the kiro
        # path ``_start_kiro_runtime_impl`` replaces that client with an
        # ``AcpRuntime`` it constructs itself, and a dedicated subagent's
        # inherited work directory has to reach THAT process.
        self._shared_scratch: Path | None = shared_scratch
        # Start-queue exit/entry callbacks, forwarded to BOTH ``runtime.spawn`` calls
        # (the first spawn and the resume-died respawn: the cold-start admission)
        # and to ``runtime.create_session`` on the fresh-session path
        # (``session/load`` takes no gate permit). A dedicated subagent pauses its
        # start clock while queued at each, as a session-shared one does at the
        # gate in ``_create_shared_session``: a wait for a permit is admission's
        # cost, not this start's. None -- every non-subagent session -- is inert.
        self._on_gate_acquired: Callable[..., None] | None = on_gate_acquired
        self._on_gate_queued: Callable[..., None] | None = on_gate_queued
        # Whether ``work_dir`` was DERIVED for a one-run session (a subagent, a
        # stateless cron run) and is this provider's to reclaim at shutdown. An
        # explicit caller cwd is never marked, whatever key the session has; and
        # marking only authorizes removing Crew's own residue, never a file the
        # run wrote (session_work_dir.reclaim_session_work_dir).
        self._disposable_work_dir: bool = bool(disposable_work_dir)
        # Installed by the session registry when this provider is registered.
        # The context manager retains the registry lock from the final ownership
        # decision through the filesystem reclaim, closing the successor race.
        self._work_dir_claim_probe: Callable[[], AsyncContextManager[bool]] | None = None
        self._client = AcpClient(**kwargs)
        # Consumer opt-in for the low-fidelity child permission downgrade
        # (see child_fidelity_aware property). Set by fidelity-aware
        # consumers (dashboard chat) BEFORE startup; re-applied when
        # _start_kiro_runtime_impl swaps in the real AcpSessionProvider.
        self._child_fidelity_aware: bool = False
        # Canonical Kiro Crew identity (a cfg.agents key), resolved by the
        # factory at provider-creation time — ``agent`` above is the bound kiro
        # template, a different namespace. Threaded into the kiro-shared
        # runtime so per-agent watchdog windows key off the crew, never off a
        # cross-namespace name match.
        self._crew_agent: str = crew_agent or ""
        # F2 load-recovery: set True by _start_kiro_runtime_impl when a resume
        # falls back to a FRESH native session (the prior session's lock never
        # cleared). Signals SessionManager.get_or_create to replay KiroCrew's
        # conversation_log into the fresh session on the first prompt so the slot
        # is not context-free.
        self._history_replay_needed: bool = False
        # Only the direct-dashboard Tool Search compatibility path owns the
        # dashboard runner's durable replay settlement contract. Generic
        # session/load recovery (including channel dispatchers) still requests
        # history replay but must publish its fresh SID immediately.
        self._defer_replay_sid_promotion: bool = False
        # Terminal compaction status captured by compact() while draining its
        # prompt turn; consumed by wait_for_compaction() (see compact()).
        self._compact_result: dict | None = None
        # Per-model reasoning-effort. Resolution priority (see effort.py):
        # slot override > workspace defaults > None. The kiro backend applies
        # this via a workspace cli.json overlay read at spawn; the claude
        # backend applies it live via session/set_config_option (no overlay).
        self._effort_per_model: dict[str, str] = dict(effort_per_model or {})
        # A level requested while no model is pinned: there is no id to key it
        # under until the backend names the model it chose, so it is held here
        # and bound to that model on every (re)start (``_bind_unbound_effort``).
        self._unbound_effort: str = unbound_effort or ""
        self._unbound_effort_drop_logged: bool = False
        self._effort_defaults = effort_defaults
        # The spelling the factory keyed ``effort_per_model`` under.
        self._configured_model: str = model or ""
        # MCP Tool Search toggle (kiro-cli backend only). None = caller did not
        # specify (e.g. claude_code factory), so leave the overlay untouched.
        # True/False = write the kiro settings overlay deterministically so the
        # KiroCrew toggle is authoritative over any global kiro setting.
        self._tool_search = tool_search
        # None = caller expressed no preference; fall back to kiro-cli's own
        # activation thresholds rather than inventing a product-specific one.
        self._tool_search_min_pct = (
            TOOL_SEARCH_DEFAULT_MIN_PCT if tool_search_min_pct is None else tool_search_min_pct
        )
        self._tool_search_min_tokens = (
            TOOL_SEARCH_DEFAULT_MIN_TOKENS
            if tool_search_min_tokens is None
            else tool_search_min_tokens
        )
        if self._client.backend in ACP_BACKENDS_EFFORT_OVERLAY:
            # Read back the levels the overlay already holds, so after a gateway
            # restart the map names what kiro-cli will run. Caller-provided
            # overrides win — only fill gaps.
            #
            # The overlay's own readers only. The file is keyed by work dir, so
            # every session in one project shares it; read back by a harness that
            # never opens it (KAS), it would start a new chat at whatever level
            # another chat in the same work dir picked last. Such a harness takes
            # its level from the factory alone.
            try:
                for m, lvl in _read_cli_overlay(self._client._work_dir).items():
                    self._effort_per_model.setdefault(m, lvl)
            except Exception:
                logger.debug("ACP effort overlay recovery failed", exc_info=True)
        if self._client.backend in ACP_BACKENDS_KIRO_FAMILY:
            # Write the overlay BEFORE the first spawn so kiro-cli reads it on
            # session/new. Each writer is scoped to the harnesses that read its keys.
            self._apply_effort_overlay()
            self._apply_tool_search_overlay()

    @property
    def client(self) -> AcpClient:
        """Expose underlying client for backward compat (e.g. is_ready check)."""
        return self._client

    @property
    def work_scratch_dir(self) -> Path | None:
        """The session tree's ``$KIROCREW_SCRATCH`` directory (H14 capability).

        Read off whichever process serves this session -- the placeholder
        ``AcpClient`` before start, the ``AcpSessionProvider`` the kiro path
        swaps in after -- so the answer is the directory the live process
        actually exposes, never the one it was asked for.
        """
        return self._client.work_scratch_dir

    @property
    def child_fidelity_aware(self) -> bool:
        """See AcpSessionHandle.child_fidelity_aware.

        Stored on the provider AND forwarded to the live inner client:
        for the kiro backend ``self._client`` starts as a placeholder
        AcpClient and is REPLACED with an AcpSessionProvider at runtime
        startup (_start_kiro_runtime_impl), so a flag set only on the
        inner client before startup would be lost with the placeholder.
        The stored value is re-applied at replacement time.
        """
        return self._child_fidelity_aware

    @child_fidelity_aware.setter
    def child_fidelity_aware(self, value: bool) -> None:
        self._child_fidelity_aware = bool(value)
        inner = getattr(self, "_client", None)
        if inner is not None and hasattr(inner, "child_fidelity_aware"):
            try:
                inner.child_fidelity_aware = bool(value)
            except Exception:  # pragma: no cover - read-only shapes
                pass

    @property
    def model_pin_refused(self) -> str:
        """The pinned model the adapter refused at startup, or ``""``.

        Both client shapes carry the field: a raw ``AcpClient`` sets it in its
        startup model push, and an ``AcpSessionProvider`` delegates to its
        handle's ``set_model``.
        """
        value = self._client.model_pin_refused
        return value if isinstance(value, str) else ""

    @property
    def model_pin_partial(self) -> str:
        """The bare model a ``<model>[<effort>]`` pin landed as, or ``""``.

        Set when the base model applied but its effort half did not.
        """
        value = self._client.model_pin_partial
        return value if isinstance(value, str) else ""

    def settings_withheld_cause(self) -> str:
        """Forward the client's live answer (see ``AcpClient.settings_withheld_cause``).

        Only the claude shape (a raw ``AcpClient``) seeds the settings file; any
        other client shape, or a non-string answer, reads as ``""``.
        """
        read = getattr(self._client, "settings_withheld_cause", None)
        if not callable(read):
            return ""
        value = read()
        return value if isinstance(value, str) else ""

    @property
    def served_model(self) -> str:
        """Model id the live session resolved (public — see LLMProvider).

        Only BACKEND-RESOLVED ids count — the canary in chat_runner must
        never probe a model the backend didn't actually serve. The two client
        shapes store that differently, so resolve per shape:

        - ``AcpSessionProvider`` (kiro path after startup): its public
          ``served_model`` delegates to the handle, which prefers the
          explicit ``set_model`` assignment and falls back to the
          ``session/new|load`` response's ``currentModelId`` — so a session
          on the backend-selected DEFAULT model is still readable.
        - Raw ``AcpClient`` (claude backend / pre-startup): ``_model`` is the
          REQUESTED id (defaults to the ``"auto"`` sentinel) and is
          deliberately NOT consulted; only ``_resolved_model_id``, which the
          backend reported, counts.

        The ``"auto"`` sentinel is filtered to ``""`` (= unknown/inconclusive)
        on both paths.
        """
        client = self._client
        if isinstance(client, AcpSessionProvider):
            model = str(client.served_model or "").strip()
        else:
            model = str(getattr(client, "_resolved_model_id", "") or "").strip()
        return "" if model == DEFAULT_MODEL else model

    @property
    def _effort_model(self) -> str:
        """The model id every effort read and write of this session keys on.

        The pinned model when one is pinned. With nothing pinned the client
        records ``""`` or the ``auto`` sentinel, which no effort level can be
        keyed under, so the id the backend reported serving stands in once it
        is known; until then the client's own value is returned unchanged.
        """
        model = self._client._model
        if model and model != DEFAULT_MODEL:
            return model
        return self.served_model or model

    @property
    def agent_version(self) -> str:
        """``agentInfo.version`` the live process reported at its handshake.

        ``""`` before startup and for a client shape that never handshaked.
        Both client shapes (``AcpSessionProvider`` after startup, raw
        ``AcpClient`` before it / on the claude seam) expose the same
        attribute, so one read covers them.
        """
        return str(getattr(self._client, "agent_version", "") or "")

    @property
    def steer_upgrade_notice(self) -> str:
        """The upgrade notice when this session's adapter is too old to steer, else ``""``.

        Read off :attr:`agent_version`, so ``""`` before the handshake and for an
        adapter that reported no readable version.
        """
        from kiro_crew.acp.client_turn_control import steer_version_issue

        return steer_version_issue(
            str(getattr(self._client, "backend", "") or ""), self.agent_version
        )

    @property
    def cwd(self) -> str:
        """Working directory this provider operates in.

        Overrides the ``LLMProvider`` default ("") so session_map can persist
        the real workspace path for both ACP backends. The wrapper is what every
        caller holds -- ``SessionMap.set(..., cwd=provider.cwd)``, reuse
        validation, the transport dispatchers -- so it must forward the SAME
        session-bound directory the inner provider now reports, not the shared
        runtime's own. After startup ``self._client`` is an
        ``AcpSessionProvider`` whose ``cwd`` reads the handle's bound dir (a
        shared runtime carries sessions opened against different projects);
        answering with ``self._client._work_dir`` here would report the
        runtime's workspace and evict a live session on reuse validation. Before
        startup / on the claude seam ``self._client`` is a raw ``AcpClient``
        with no ``cwd``, so the fallback reads its ``_work_dir``.
        """
        return str(getattr(self._client, "cwd", "") or self._client._work_dir)

    @property
    def is_claude_backend(self) -> bool:
        """True when this ACP provider talks to claude-agent-acp (vs kiro-cli).

        The comparison lives in ``agent_sdk.backend_identity`` so this property,
        ``session._is_claude_backend`` and ``provider_label`` cannot drift about
        what "the claude backend" means. Reading ``self._client.backend`` stays
        here because only this class knows where its own backend string lives.
        """
        return is_claude_backend_name(self._client.backend)

    @property
    def capabilities(self) -> SessionCapabilities:
        """What the backend serving this session can DO -- ask this, not who it is.

        The one attribute application code reads to branch on backend behaviour.
        Every ``is_*_backend`` property beside it names an IDENTITY, and an
        identity branch hands each new harness whichever arm the old comparison
        happened to leave behind (harness-parity H6). Those properties stay for
        the call sites inside this package and for the migration still in front of
        this one; a consumer outside the boundary reads this instead, and
        ``test_agent_sdk_capabilities`` pins that the six that already moved do
        not go back.

        Rebuilt per read rather than cached in ``__init__``: each field is a set
        membership or a dict lookup over four ids, and an edition can register a
        backend after this provider was constructed.
        """
        return capabilities_for(self._client.backend)

    @property
    def kas_auto_approved_capabilities(self) -> frozenset[str] | None:
        """What this session's registered agent batch auto-approves, or ``None``.

        Only a wire-registered host (KAS) has a batch; see
        ``AcpSessionHandle.kas_auto_approved``.
        """
        value = getattr(self._client, "kas_auto_approved_capabilities", None)
        return value if isinstance(value, frozenset) else None

    @property
    def kas_projected_agent(self) -> str:
        """The agent this session's registered batch was built for, or ``""``."""
        value = getattr(self._client, "kas_projected_agent", "")
        return value if isinstance(value, str) else ""

    @property
    def is_codex_backend(self) -> bool:
        """True when this ACP provider talks to codex-acp (vs kiro-cli)."""
        return self._client.backend == ACP_BACKEND_CODEX

    @property
    def is_opencode_backend(self) -> bool:
        """True when this ACP provider talks to OpenCode (vs kiro-cli)."""
        return self._client.backend == ACP_BACKEND_OPENCODE

    @property
    def is_pi_backend(self) -> bool:
        """True when this ACP provider talks to pi-acp (vs kiro-cli)."""
        return self._client.backend == ACP_BACKEND_PI

    @property
    def is_goose_backend(self) -> bool:
        """True when this ACP provider talks to goose (vs kiro-cli)."""
        return self._client.backend == ACP_BACKEND_GOOSE

    @property
    def is_deepseek_backend(self) -> bool:
        """True when this ACP provider talks to DeepSeek Harness (vs kiro-cli)."""
        return self._client.backend == ACP_BACKEND_DEEPSEEK

    @property
    def is_kas_backend(self) -> bool:
        """True when this ACP provider talks to KAS (kiro-agent)."""
        return self._client.backend == ACP_BACKEND_KAS

    @property
    def defer_replay_sid_promotion(self) -> bool:
        """Whether this fresh session waits for replay settlement before SID publish."""
        return self._defer_replay_sid_promotion

    @property
    def is_kiro_backend(self) -> bool:
        """True when this ACP provider talks to kiro-cli.

        Stated positively so call sites that mean "kiro" say so, rather than
        inferring it from ``not is_claude_backend`` — an inference that silently
        captures every future backend.
        """
        return self._client.backend == ACP_BACKEND_KIRO

    @property
    def is_acp_runtime_backend(self) -> bool:
        """True when this provider is served by AcpRuntime rather than AcpClient.

        Membership in ``ACP_BACKENDS_ACP_RUNTIME`` (harness-parity H5), read
        straight off the frozenset. Names the set positively so the start path
        stops being spelled ``not is_claude_backend`` — which would hand the
        shared-runtime path to every harness added later. The claude AcpClient is
        deliberately not a member: it runs one process per session and shares no
        runtime.

        This answers ONE question, the transport, and it is worth saying what it
        does not answer. Membership does not imply the kiro-family spawn
        conventions: the cli.json overlay is gated on its readers' own sets
        (``ACP_BACKENDS_EFFORT_OVERLAY``, ``ACP_BACKENDS_TOOL_SEARCH_OVERLAY``) at
        every site that reads, writes or clears it, because codex and KAS run here
        and read no such file. Nor does it
        imply that a session can be disposed — that is
        ``ACP_BACKENDS_SESSION_EVICTION``, a separate claim earned by a measured
        teardown verb, and the reason the background path asks its own question
        rather than reading this one.
        """
        return self._client.backend in ACP_BACKENDS_ACP_RUNTIME

    @property
    def is_session_sharing_eligible(self) -> bool:
        """True when this provider can host multiplexed subagent sessions.

        Session sharing requires a backend whose single process serves N
        concurrent sessions via AcpRuntime demux, so it is granted by membership
        in ``ACP_BACKENDS_SESSION_SHARING`` (harness-parity H6) rather than by
        ``not is_claude_backend``. The Claude Code backend uses AcpClient (one
        process per session) and is not a member, so subagents fall back to the
        legacy per-process path — and neither does any harness added later,
        until someone adds it deliberately.
        """
        return self._client.backend in ACP_BACKENDS_SESSION_SHARING

    @property
    def manual_compact_unsupported_backend(self) -> str | None:
        """Backend id when a manual ``/compact`` cannot be served, else ``None``.

        Answered from ``ACP_BACKENDS_COMPACT`` membership (harness-parity H6):
        kiro-cli answers the ``/compact`` prompt with
        ``_kiro.dev/compaction/status``, claude-agent-acp compacts natively
        in-prompt, and on KAS the session handle sends the engine's
        ``_kiro/session/compact`` verb instead of the prompt. A non-member
        dispatched anyway strands ``wait_for_compaction()`` for the full
        ``COMPACT_WAIT_TIMEOUT_SECS``.
        Read off the backend STRING, not the ``is_*_backend``
        properties, matching ``provider_label``'s MagicMock caution; a
        non-``str`` value answers ``None`` so a spec'd double never reads as a
        refusal. The empty string is ``ACP_BACKEND_KIRO`` (a member), so a
        non-``None`` answer is always a non-empty backend id.
        """
        backend = getattr(self._client, "backend", ACP_BACKEND_KIRO)
        if not isinstance(backend, str) or backend in ACP_BACKENDS_COMPACT:
            return None
        return backend

    @property
    def compaction_self_managed(self) -> bool:
        """Whether this harness bounds its own context (harness-parity H6).

        Membership in ``ACP_BACKENDS_HARNESS_MANAGED_COMPACTION``, OR membership
        in ``ACP_BACKENDS_COMPACT`` — a backend Crew can compact is bounded
        whoever does the bounding, so the question only discriminates among the
        backends Crew cannot compact. A non-``str`` backend answers ``True``, the
        ABC default, so a spec'd double never reads as the unclassified case.
        """
        backend = getattr(self._client, "backend", ACP_BACKEND_KIRO)
        if not isinstance(backend, str):
            return True
        return backend in ACP_BACKENDS_COMPACT or backend in ACP_BACKENDS_HARNESS_MANAGED

    @property
    def harness_managed_compaction_backend(self) -> str | None:
        """Backend id when the harness compacts on its own, else ``None``.

        Membership in ``ACP_BACKENDS_HARNESS_MANAGED_COMPACTION``, with the same
        ``str`` caution as :attr:`manual_compact_unsupported_backend`.
        """
        backend = getattr(self._client, "backend", ACP_BACKEND_KIRO)
        if not isinstance(backend, str) or backend not in ACP_BACKENDS_HARNESS_MANAGED:
            return None
        return backend

    @property
    def compaction_unmanaged_backend(self) -> str | None:
        """Backend id when neither Crew nor the harness compacts, else ``None``.

        Membership in ``ACP_BACKENDS_CONTEXT_RECYCLE`` (harness-parity H6), read
        POSITIVELY. Answering it by exclusion from the other two compaction sets
        would hand the one session-destroying arm to every harness added later
        without anyone deciding it. The same ``str`` caution
        :attr:`manual_compact_unsupported_backend` takes applies here for a
        sharper version of the same reason — a spec'd double must not read as a
        positive claim, and here the claim ends a conversation.
        """
        backend = getattr(self._client, "backend", ACP_BACKEND_KIRO)
        if not isinstance(backend, str) or backend not in ACP_BACKENDS_CONTEXT_RECYCLE:
            return None
        return backend

    @property
    def uses_kiro_identity_store(self) -> bool:
        """True when this provider's child signs in from kiro-cli's own store.

        Membership in ``backends_retired_by_host_logout()`` (harness-parity
        H5/H14). Declaring it here is what lets the session layer ask the
        question through the ABC instead of probing private attributes, so an
        adapted provider is classified by its own declaration rather than by
        whichever internal shape it happens to expose.
        """
        return self._client.backend in host_auth.backends_retired_by_host_logout()

    @property
    def member_capabilities_supported(self) -> bool:
        """Saved member-spec loading or projection is opt-in (harness-parity H6)."""
        return self._client.backend in ACP_BACKENDS_MEMBER_CAPABILITIES

    @property
    def member_tool_limits_supported(self) -> bool:
        """Per-tool ``disabledTools`` in a saved member spec is opt-in (H6)."""
        return self._client.backend in ACP_BACKENDS_MEMBER_TOOL_LIMITS

    @property
    def mate_worker_kit_supported(self) -> bool:
        """A crewmate worker's per-session kit is carried by opt-in (H6)."""
        return self._client.backend in ACP_BACKENDS_MATE_WORKER_KIT

    @property
    def loaded_capability_template(self) -> str:
        return self._client.loaded_capability_template

    @property
    def capability_projection_gaps(self) -> tuple[str, ...]:
        return self._client.capability_projection_gaps

    @property
    def mcp_config_hot_reload(self) -> bool:
        """True when this provider's process reconciles MCP config edits itself.

        Membership in ``ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD`` AND a handshake
        version at or above the floor (harness-parity H6/H14). Answered from
        what THIS process reported at ``initialize``, never from the binary on
        disk: after an in-place kiro-cli upgrade the file is newer than every
        process spawned before it. Before the handshake (placeholder client,
        empty version) this is False, so a session still starting is reset like
        one that cannot reconcile.
        """
        return mcp_hot_reload_supported(
            self._client.backend, parse_kiro_cli_version(self.agent_version)
        )

    def available_models(self) -> list[dict[str, str]]:
        """Models the backend advertised at session init (may be empty).

        Surfaced to the dashboard so the model dropdown reflects what the
        active backend actually offers (e.g. claude-agent-acp's versioned
        Claude list) rather than a hardcoded set.
        """
        return self._client.available_models()

    def mcp_session_report(self) -> SessionMcpReport:
        """This session's MCP registration report, kept on the inner client.

        The dedicated transport owns one client per session, so its report IS the
        session's. The shared runtime answers the same call from its handle.
        """
        return self._client.mcp_session_report()

    async def wait_for_declared_mcp(self, timeout: float) -> tuple[str, ...]:
        """Hold the first prompt until the spec's servers report (see the client)."""
        return await self._client.wait_for_declared_mcp(timeout)

    def get_valid_effort_levels(self) -> list[str]:
        """Effort levels the backend reported for the CURRENT model, in ACP order.

        Read live from the session's ``configOptions`` so the dashboard dropdown
        reflects exactly what this slot's active model supports (and updates on a
        model switch). Empty when the backend reported no effort selector.
        """
        return self._client.get_valid_effort_levels()

    @property
    def tool_search_settings(self) -> ToolSearchSettings | None:
        """The resolved Tool Search choice this provider spawns runtimes with (H14 capability)."""
        return self._tool_search_settings()

    def _tool_search_settings(self) -> ToolSearchSettings | None:
        """The operator's Tool Search choice, for the runtime's wire channel.

        ``None`` when no toggle value was threaded in, which leaves the wire
        silent exactly as the overlay writer is a no-op. The runtime decides per
        host whether its handshake carries the setting and gates it on the agent
        spec's loader grant; this method only resolves the operator's side.
        """
        if self._tool_search is None:
            return None
        return ToolSearchSettings.from_config(
            self._tool_search, self._tool_search_min_pct, self._tool_search_min_tokens
        )

    @property
    def context_incarnation(self) -> object:
        return (id(self._client), self.session_id, self.process_instance)

    @property
    def context_provider_type(self) -> str:
        return provider_label(self)

    @property
    def native_context_documents(self) -> dict[str, str]:
        if isinstance(self._client, AcpSessionProvider):
            return self._client.native_context_documents
        return {}

    @property
    def member_dispatch_mounted(self) -> bool | None:
        # Both transports answer from the composition that built THIS session's
        # array: the shared runtime records it on the handle, the direct client
        # on itself.
        if isinstance(self._client, (AcpSessionProvider, AcpClient)):
            return self._client.member_dispatch_mounted
        return None

    @property
    def native_steering(self) -> bool:
        # Kiro ACP manual/fileMatch support depends on version and engine;
        # the fallback keeps those guides reachable without a false capability.
        return self._client.backend == ACP_BACKEND_KAS

    @property
    def native_spec_prompt_across_compaction(self) -> bool:
        # Retention through the harness's own compaction is a separate claim,
        # admitted per backend only on a live transcript (the set's comment
        # names it), and only from the release that transcript was taken on:
        # below the floor, and before the handshake reports a version, the
        # block is sent, a duplicate at worst, where a wrong True drops the
        # persona for the rest of the session.
        # Whether the harness loaded the spec's prompt at all is not a property:
        # the runtime records the loaded copy in native_context_documents only
        # on a backend in ACP_BACKENDS_NATIVE_SPEC_PROMPT.
        return self._client.backend in ACP_BACKENDS_NATIVE_SPEC_PROMPT_ACROSS_COMPACTION and (
            spec_prompt_retention_verified(parse_kiro_cli_version(self.agent_version))
        )

    def prompt_timeout_for_deadline(self, deadline: float) -> float:
        """Resolve the immutable transport budget this ACP session supports."""
        return resolve_prompt_timeout_for_deadline(deadline)

    async def approve_tool(
        self,
        request_id: str | int,
        *,
        always: bool = False,
        deadline: GrantDeadline | None = None,
        withdraw_if: Callable[[], bool] | None = None,
    ) -> bool:
        # Passed on only when given, so a call without it reaches the client
        # exactly as before.
        withdrawal: dict[str, Any] = {}
        if withdraw_if is not None:
            withdrawal["withdraw_if"] = withdraw_if
        if deadline is None:
            return await self._client.approve_tool(request_id, always=always, **withdrawal)
        return await self._client.approve_tool(
            request_id, always=always, deadline=deadline, **withdrawal
        )

    async def reject_tool(self, request_id: str | int) -> None:
        await self._client.reject_tool(request_id)

    def context_usage_pct(self) -> float:
        return self._client.last_prompt_stats.context_pct

    def context_usage_unknown(self) -> bool:
        return self._client.last_prompt_stats.context_pct_unknown

    def context_window_tokens(self) -> int:
        return self._client.last_prompt_stats.context_window_tokens

    def context_used_tokens(self) -> int:
        return self._client.last_prompt_stats.context_used_tokens

    def billing_stats(self) -> object | None:
        """Live per-turn billing stats (public — see LLMProvider).

        Forwards the inner client's own DECLARATION rather than reading an
        attribute off it: this provider wraps whichever client the backend
        installs (a raw ``AcpClient`` for claude / pre-startup, an
        ``AcpSessionProvider`` after kiro startup replaces the placeholder), and
        forwarding the capability is what keeps the wrapper from re-introducing
        the attribute-name dependency the accounting path just shed. Resolved
        through the shared helper so this forward cannot diverge from the reader
        it feeds, and per call because the placeholder client is replaced at
        runtime startup.
        """
        return resolve_billing_stats(getattr(self, "_client", None))

    @property
    def steer_needs_loss_recovery(self) -> bool:
        """True when the inner session can lose a steer it reported delivered."""
        return self._client.steer_needs_loss_recovery is True

    @property
    def last_steer_monotonic(self) -> float:
        """Monotonic time of the inner client's last steer (0.0 if never)."""
        return float(getattr(self._client, "last_steer_monotonic", 0.0) or 0.0)

    @property
    def supports_steer(self) -> bool:
        """True when the inner client supports mid-turn steer through this wrapper.

        False for a session that can lose a delivered steer (codex): only the
        dashboard composer, which steers the inner client directly, steers it."""
        return bool(getattr(self._client, "supports_steer", False)) and (
            not self.steer_needs_loss_recovery
        )

    @property
    def supports_refusal_steer(self) -> bool:
        """True when the inner client can steer a deny notice into a refused turn."""
        return bool(getattr(self._client, "supports_refusal_steer", False))

    async def discard_unanswered_prompt(self) -> bool:
        """The inner client's rewind of an unanswered prompt; False when it has none.

        Only the session provider (KAS through the shared runtime) carries one: a
        dedicated ``AcpClient`` keeps the failed copy.
        """
        discard = getattr(self._client, "discard_unanswered_prompt", None)
        if discard is None:
            return False
        return await discard() is True

    async def new_conversation(self) -> None:
        """Clean-slate reset on the SAME warm process (no cold start).

        Delegates to the backing client — ``AcpSessionProvider.new_conversation``
        on the kiro path (``self._client`` is swapped to the session provider by
        ``start()``), which issues a fresh ``session/new`` on the already-running
        process, skipping subprocess spawn + the ``initialize`` handshake. This is
        the primitive a warm session pool uses to reuse one worker across
        independent tasks without leaking context between them. The claude backend
        would delegate to its own client-side reset the same way (its ``AcpClient``
        does not ship one — hence the type ignore).
        """
        await self._client.new_conversation()  # type: ignore[attr-defined]

    def is_alive(self) -> bool:
        """Whether the shared client is responsive.

        PROCESS-level, like every liveness answer on this provider: the client is
        one kiro-cli process that may host several ACP sessions, so co-tenants
        cannot be told apart here. Read it as "the process serving me is up", not
        as "my session is usable".
        """
        return self._client.is_responsive()

    def is_process_alive(self) -> bool:
        """True if the runtime process has not exited (ignores I/O staleness).

        Process-level by name as well as by behaviour, and shared by every session
        the client hosts. Do not read it as "my session is usable" -- see
        :meth:`is_alive`.
        """
        return self._client.is_process_alive()

    @property
    def process_instance(self) -> str:
        """Per-spawn identity of the client's current runtime process (see base).

        A direct read on purpose: a `getattr` hedge would convert a future
        wiring break into "no banner is ever live", indistinguishable from
        correct expiry.
        """
        return self._client.process_instance

    def has_active_turn(self) -> bool:
        """True if a prompt is in flight (and not yet cancelled) on the client."""
        return bool(self._client) and self._client.has_active_turn()

    def background_launch(self) -> tuple[float, str] | None:
        """The client's newest background launch, or ``None`` (see base)."""
        if not self._client:
            return None
        return self._client.background_launch()

    def has_unfinished_turn(self) -> bool:
        """True if the client reports a native turn that has NOT reached its
        done boundary — INDEPENDENT of cancel state (unlike
        :meth:`has_active_turn`).

        Used by the shutdown drain so an already-cancelled turn whose native ack
        has not yet arrived is still drained (waited on) before the process is
        killed — otherwise kiro-cli is killed with the native turn open and its
        session lock held (the empty-response-after-restart bug, #200).
        """
        return bool(self._client) and self._client.has_unfinished_turn()

    async def wait_turn_done(self, timeout: float) -> str:
        """Wait for the current native turn to reach its done boundary; returns
        the stop reason, or raises ``asyncio.TimeoutError``.

        Public wrapper over the inner client's ``wait_turn_done`` (which
        :meth:`cancel` already uses). The shutdown drain calls this on a turn
        that was already cancelled (so ``cancel`` returns ``"no_turn"``) but is
        still unfinished, to wait for the pending ack before teardown.
        """
        return await self._client.wait_turn_done(timeout=timeout)

    @property
    def exit_code(self) -> int | None:
        """The runtime process's exit code, or None if still running.

        One code per process, so every session the client hosted reads the same
        one: it explains a runtime's death, never one session's stop.
        """
        return self._client.exit_code

    @property
    def last_compaction_transient(self) -> bool:
        """Whether that failure is worth retrying (from the inner client).

        Coerced to a real ``bool`` because the consumer compares against
        ``True`` — a truthy stand-in must not read as a verdict.
        """
        return getattr(self._client, "last_compaction_transient", False) is True

    @property
    def last_infra_error(self) -> InfraError | None:
        """The inner client's L1 verdict on the last tool result, or None.

        ``getattr`` with a None default, like ``last_compaction_transient``: on
        the kiro path ``self._client`` is the placeholder AcpClient until
        ``_start_kiro_runtime_impl`` swaps in the AcpSessionProvider that reads the
        session handle, and an AcpClient never classifies tool results, so the
        seams that do not participate answer "no verdict" rather than raising.
        """
        err = getattr(self._client, "last_infra_error", None)
        return err if isinstance(err, InfraError) else None

    def touch_activity(self) -> None:
        """Refresh the CLIENT's activity clock.

        PROCESS-level: the stamp lives on the shared client, so this marks every
        session on that process active, not just this one.
        """
        self._client.touch_activity()

    @property
    def session_id(self) -> str:
        """Return the kiro-cli session UUID."""
        return self._client._session_id if self._client and self._client._session_id else ""

    # The workspace ``cli.json`` overlay a spawn reads: ``kiro_crew.providers.acp_cli_overlay``.
    _apply_effort_overlay = _acp_cli_overlay._apply_effort_overlay
    _apply_tool_search_overlay = _acp_cli_overlay._apply_tool_search_overlay
    # The model and reasoning effort the session runs at: ``kiro_crew.providers.acp_model_effort``.
    maybe_refresh_available_models = _acp_model_effort.maybe_refresh_available_models
    _bind_unbound_effort = _acp_model_effort._bind_unbound_effort
    _advertised_effort_levels = _acp_model_effort._advertised_effort_levels
    accepts_effort_level = _acp_model_effort.accepts_effort_level
    supports_effort = _acp_model_effort.supports_effort
    _resolve_effort = _acp_model_effort._resolve_effort
    _set_effort_config_option = _acp_model_effort._set_effort_config_option
    change_effort = _acp_model_effort.change_effort
    reapply_live_effort = _acp_model_effort.reapply_live_effort
    set_model = _acp_model_effort.set_model
    clear_effort = _acp_model_effort.clear_effort
    _apply_initial_effort = _acp_model_effort._apply_initial_effort
    # A cold start on a shared ``AcpRuntime``: ``kiro_crew.providers.acp_runtime_start``.
    _start_kiro_runtime = _acp_runtime_start._start_kiro_runtime
    _emit_kiro_startup_metric = _acp_runtime_start._emit_kiro_startup_metric
    _owning_session_key = _acp_runtime_start._owning_session_key
    _owning_channel_id = _acp_runtime_start._owning_channel_id
    _member_session_key = _acp_runtime_start._member_session_key
    _load_session_with_retry = _acp_runtime_start._load_session_with_retry
    _start_kiro_runtime_impl = _acp_runtime_start._start_kiro_runtime_impl
    _note_zero_tool_servers = _acp_runtime_start._note_zero_tool_servers
    # The start and shutdown sequence: ``kiro_crew.providers.acp_lifecycle``.
    start = _acp_lifecycle.start
    shutdown = _acp_lifecycle.shutdown
    set_work_dir_claim_probe = _acp_lifecycle.set_work_dir_claim_probe
    disown_work_dir = _acp_lifecycle.disown_work_dir
    _reclaim_work_dir = _acp_lifecycle._reclaim_work_dir
    cleanup_session = _acp_lifecycle.cleanup_session
    # The session's turns on its inner client: ``kiro_crew.providers.acp_turns``.
    _to_llm_event = staticmethod(_acp_turns._to_llm_event)
    _ready_confirmed_session = _acp_turns._ready_confirmed_session
    stream = _acp_turns.stream
    stream_command = _acp_turns.stream_command
    compact = _acp_turns.compact
    cancel = _acp_turns.cancel
    steer = _acp_turns.steer
    _inline_turn_finished_cleanly = _acp_turns._inline_turn_finished_cleanly
    wait_for_compaction = _acp_turns.wait_for_compaction


def is_claude_backend(provider: Any) -> bool:
    """Check if a provider is a Claude backend via the ACP adapter.

    Free function for use from modules that hold the provider as the
    ``LLMProvider`` ABC and can't reach the ``AcpProvider.is_claude_backend``
    property without an isinstance gate.
    """
    return isinstance(provider, AcpProvider) and provider.is_claude_backend


def provider_label(provider: Any) -> str:
    """Backend identity key for *provider*.

    This key indexes three things, so all producers must agree on it:
    resume compatibility (``detect_provider_switch``), the value persisted in
    the session map, and session-file cleanup routing.

    Resolved from the client's backend STRING rather than the ``is_*_backend``
    properties, matching ``session._is_claude_backend``. A ``MagicMock(spec=...)``
    constrains attribute names but not their values, so reading a property would
    make every spec'd provider in the test suite look like every backend at once.

    Both shapes a runtime-backed session can arrive in are accepted: an
    ``AcpProvider`` whose ``client`` was swapped for an ``AcpSessionProvider``
    once startup completed, and a bare ``AcpSessionProvider`` handed out for a
    shared subagent session. Missing either one persists a KAS session under
    the kiro label, and the map then prunes its id for want of a kiro
    transcript.
    """
    if isinstance(provider, AcpSessionProvider):
        backend = provider.backend
    elif isinstance(provider, AcpProvider):
        backend = getattr(getattr(provider, "client", None), "backend", "")
    else:
        return PROVIDER_LABEL_DEFAULT
    # The claude test stays a NAME check ahead of the mapping rather than folding
    # into it: ``is_claude_backend_name`` accepts every spelling this build treats
    # as that harness, which is more than one mapping key can hold.
    if is_claude_backend_name(backend):
        return PROVIDER_LABEL_CLAUDE
    return PROVIDER_LABEL_BY_BACKEND.get(backend, PROVIDER_LABEL_DEFAULT)


# --------------------------------------------------------------------------- #
# Compatibility facade. ``AcpProvider``'s members live in the owner modules below,
# bound by name in the class body, and the module-level helpers of the workspace
# cli.json overlay and of the resume retry live with the code that reads them. Every
# name this module bound before the owners existed stays readable as
# ``kiro_crew.providers.acp.<name>``:
#
# * Every import of this module stays bound here, so a patch of
#   ``kiro_crew.providers.acp.<name>`` reaches every owner that binds the same object
#   (``_facade_mirror``), as it reached this module's own code.
# * A moved helper this module's own code reads is an ordinary import above.
# * Every other moved helper is SERVED: absent from this namespace on purpose, read
#   from its owner through the mirror, and a write or delete through this module
#   lands on every owner binding it.
#
# ``test/test_acp_bindings.py`` pins the member inventory, the binding and the reach.
# --------------------------------------------------------------------------- #
#: Owner modules, lowest layer first: an owner imports only owners listed before it.
_ACP_PROVIDER_OWNERS: tuple[str, ...] = (
    "kiro_crew.providers.acp_cli_overlay",
    "kiro_crew.providers.acp_model_effort",
    "kiro_crew.providers.acp_runtime_start",
    "kiro_crew.providers.acp_lifecycle",
    "kiro_crew.providers.acp_turns",
)
for _owner in _ACP_PROVIDER_OWNERS:
    _importlib.import_module(_owner)
#: Module-level names this module bound before the split that only owner code reads.
_SERVED: frozenset[str] = frozenset(
    {
        "_CLI_JSON_ENCODING",
        "_RESUME_BACKOFF_BASE_S",
        "_RESUME_MAX_ATTEMPTS",
        "_RESUME_TRANSIENT_LOCK_MARKERS",
        "_SETUP_FAILURE_LOG_CAP",
        "_ZERO_TOOL_CHECK_TIMEOUT_SECS",
        "_clamp_min_pct",
        "_clamp_min_tokens",
        "_clear_cli_overlay_effort",
        "_is_transient_resume_lock_error",
        "_unlink_dead_session_lock",
        "_write_cli_overlay",
        "_write_tool_search_overlay",
    }
)
#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_MIRROR = _facade_mirror.install(
    _sys.modules[__name__],
    _ACP_PROVIDER_OWNERS,
    excluded=(),
    surface=frozenset(globals()) | _SERVED,
)
_EXPORTS.update(_MIRROR.served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.providers.acp_cli_overlay import (  # noqa: F401
        _CLI_JSON_ENCODING,
        _clamp_min_pct,
        _clamp_min_tokens,
        _clear_cli_overlay_effort,
        _write_cli_overlay,
        _write_tool_search_overlay,
    )
    from kiro_crew.providers.acp_runtime_start import (  # noqa: F401
        _RESUME_BACKOFF_BASE_S,
        _RESUME_MAX_ATTEMPTS,
        _RESUME_TRANSIENT_LOCK_MARKERS,
        _SETUP_FAILURE_LOG_CAP,
        _ZERO_TOOL_CHECK_TIMEOUT_SECS,
        _is_transient_resume_lock_error,
        _unlink_dead_session_lock,
    )
