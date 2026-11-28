"""ACP v1 transport and request coordinator for the Kiro BYO backend.

KiroCrew owns the host's tasks, session intent and permissions. This module
coordinates the wire protocol and delegates durable-run recovery, live session
retirement and persistence to independently auditable execution seams.
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, TextIO

from .agent_projection import AgentProjection, from_session_params
from .session_restore_projection import resolve_restored_agent_projection
from .acp_session_state import Session, PendingClientRequest
from .acp_content import prompt_blocks_from_user_message, prompt_message, prompt_text
from .mode_router import pack_label, pack_short_label, policy_label, route_model
from .policy_packs import get_policy_pack
from .config import ConfigError, KiroByoConfig, ModelSpec, ProviderSpec, home_dir, load_config
from .errors import ClientRequestError, SessionBusyError
from .mcp_client import DEFERRED_TOOL_ACTIVE_LIMIT, DEFERRED_TOOL_SEARCH_NAME, McpManager
from .metadata_refresh import refresh_stale_model_metadata
from .native_tools import (
    NativeInvocation,
    NativeToolError,
    NativeToolRuntime,
    PermissionDecision,
    ToolValidationError,
)
from .sandbox import SandboxError
from .protocol import (
    AGENT_NAME,
    AGENT_VERSION,
    parse_type_selection,
    type_selection_id,
    EFFORT_CONFIG_ID,
    THINKING_CONFIG_ID,
    LEGACY_EFFORT_CONFIG_IDS,
    MODEL_CONFIG_ID,
    MODEL_ECHO,
    MODEL_UPPER,
    PROTOCOL_VERSION,
    initialize_result,
)
from .telemetry import TurnTelemetry, TurnTelemetryStore, now_timestamp
from .runtime_trace import RuntimeTraceStore
from .reasoning_control import transport_effort_level
from .session_events import (
    append_lifecycle,
    clear_messages,
    derive_work_link,
    bind_work_link,
)
from .turn_runner import TurnRunnerMixin
from .reasoning_options import ReasoningOptionsMixin
from .session_compaction import SessionCompactionMixin
from .session_store import SessionStoreMixin
from .session_lifecycle import SessionLifecycleMixin
from .run_recovery import RunRecoveryMixin
from .acp_tool_bridge import AcpToolBridgeMixin
from .agent_runtime import AgentSpec
from .acp_compat import adapter_for_session_params
from .acp_dispatch import ACP_REQUEST_HANDLERS
from .acp_worker import AcpAgentCatalog
from .workspace_manager import WorkspaceError, WorkspaceManager
from .acp_conformance import conformance_report
from .recovery_policy import RecoveryPolicy
from .host_work_link import work_link_from_params
from .work_link_control import handle_work_bind_request
from .state_io import _atomic_json_write

ECHO_MODELS: tuple[tuple[str, str], ...] = (
    (MODEL_ECHO, "Kiro BYO Echo"),
    (MODEL_UPPER, "Kiro BYO Echo (uppercase)"),
)


class OutboundTransportError(RuntimeError):
    """The ACP stdout transport is no longer able to deliver frames."""


class AcpServer(ReasoningOptionsMixin, SessionCompactionMixin, AcpToolBridgeMixin, SessionLifecycleMixin, RunRecoveryMixin, SessionStoreMixin, TurnRunnerMixin):
    @staticmethod
    def _session_storage_home() -> Path:
        return home_dir()

    @staticmethod
    def _session_storage_write(path: Path, payload: dict[str, Any]) -> None:
        _atomic_json_write(path, payload)

    def __init__(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self._ids = itertools.count(1)
        self._client_request_ids = itertools.count(1)
        self.sessions: dict[str, Session] = {}
        self._sessions_lock = threading.RLock()
        self._send_lock = threading.Lock()
        # A single FIFO writer owns stdout after run() begins. The bounded
        # queue avoids runaway memory usage when stdout stops making progress.
        # Never block senders holding turn_lock/state_lock on queue.put().
        self._outbound: "queue.Queue[str | None]" = queue.Queue(maxsize=2048)
        self._writer_thread = threading.Thread(
            target=self._outbound_writer_loop,
            name="kiro-byo-acp-writer",
            daemon=True,
        )
        self._writer_started = False
        self._outbound_failure: BaseException | None = None
        self._outbound_failed = threading.Event()
        # Only in-process tests / pre-run diagnostics can send before run().
        # The state transition and enqueue share _send_lock: after activation,
        # NO caller can ever use the inline pre-run path.
        self._restoring_sessions: set[str] = set()
        self._retiring_sessions: dict[str, Session] = {}
        self._retirement_workers: set[threading.Thread] = set()
        # Prompt and configuration terminal responses are buffered until their
        # session locks are released. Without this boundary the
        # client can observe a completed JSON-RPC response while ``active_turn``
        # is still true (and the turn lock is still owned), then immediately
        # issue the next prompt into a false-busy race. Thread-local storage keeps
        # concurrent sessions independent without changing ordinary RPC replies.
        self._prompt_response_local = threading.local()
        self._pending_lock = threading.Lock()
        self._pending_client_requests: dict[str, PendingClientRequest] = {}
        self._executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="kiro-byo-acp")
        self.telemetry = TurnTelemetryStore()
        self.runtime_trace = RuntimeTraceStore()
        self.workspace_manager = WorkspaceManager()
        self.acp_agent_catalog = AcpAgentCatalog()

    def send(self, payload: dict[str, Any]) -> None:
        try:
            self.runtime_trace.record(
                "acp.outbound",
                session_id=str((payload.get("params") or {}).get("sessionId") or "") if isinstance(payload.get("params"), dict) else "",
                data={"method": payload.get("method", ""), "has_result": "result" in payload, "has_error": "error" in payload},
                content={"payload": payload},
            )
        except OSError as exc:
            self.trace(f"runtime trace write skipped: {type(exc).__name__}")
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        # _send_lock gives the pre-run -> running transition a single
        # linearization point; no frame can overtake an earlier submitted one.
        with self._send_lock:
            if self._outbound_failed.is_set():
                raise OutboundTransportError("ACP stdout has failed") from self._outbound_failure
            if not self._writer_started:
                # Compatibility with callers that exercise handle() before
                # run(); in production the writer is started before input.
                # This path and writer activity are mutually exclusive.
                self.stdout.write(line)
                self.stdout.flush()
                return
            try:
                self._outbound.put_nowait(line)
            except queue.Full as exc:
                failure = OutboundTransportError("ACP outbound queue exceeded 2048 frames; peer is not draining")
                self._outbound_failure = failure
                self._outbound_failed.set()
                raise failure from exc

    def _start_outbound_writer(self) -> None:
        with self._send_lock:
            if self._writer_started:
                return
            # Mark the transition only after the thread successfully starts.
            self._writer_thread.start()
            self._writer_started = True

    def _outbound_writer_loop(self) -> None:
        """Single stdout owner. A failed stream is terminal, not best-effort."""
        while True:
            line = self._outbound.get()
            if line is None or self._outbound_failed.is_set():
                return
            try:
                # Only the writer ever touches stdout after activation.
                self.stdout.write(line)
                self.stdout.flush()
            except (OSError, ValueError) as exc:
                self._outbound_failure = exc
                self._outbound_failed.set()
                self.trace(f"ACP outbound transport unavailable: {type(exc).__name__}: {exc}")
                return

    @staticmethod
    def trace(message: str) -> None:
        # ACP owns stdout for JSON-RPC. Timing diagnostics must stay on stderr.
        print(f"[kiro-byo timing] {message}", file=sys.stderr, flush=True)


    def _begin_prompt_response_buffer(self, req_id: Any) -> None:
        self._prompt_response_local.context = {"req_id": req_id, "payload": None}

    def _take_prompt_response_buffer(self, req_id: Any) -> dict[str, Any] | None:
        context = getattr(self._prompt_response_local, "context", None)
        if not isinstance(context, dict) or context.get("req_id") != req_id:
            return None
        try:
            payload = context.get("payload")
            return payload if isinstance(payload, dict) else None
        finally:
            try:
                del self._prompt_response_local.context
            except AttributeError:
                pass

    def _send_or_buffer_response(self, req_id: Any, payload: dict[str, Any]) -> None:
        context = getattr(self._prompt_response_local, "context", None)
        if isinstance(context, dict) and context.get("req_id") == req_id:
            # A JSON-RPC request must have one terminal response. Keep the first
            # one so a later accidental response cannot mask the actual outcome.
            if context.get("payload") is None:
                context["payload"] = payload
            return
        self.send(payload)

    def result(self, req_id: Any, value: dict[str, Any]) -> None:
        self._send_or_buffer_response(req_id, {"jsonrpc": "2.0", "id": req_id, "result": value})

    def error(self, req_id: Any, code: int, message: str) -> None:
        self._send_or_buffer_response(
            req_id, {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
        )

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    @staticmethod
    def prompt_text(params: dict[str, Any]) -> str:
        """Compatibility entry point for the shared ACP content codec."""
        return prompt_text(params)

    @staticmethod
    def prompt_message(params: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Compatibility entry point for the shared ACP content codec."""
        return prompt_message(params)

    @staticmethod
    def _config() -> KiroByoConfig:
        # Upgrade 0.2.1-era provider catalogues once, without asking the user to
        # re-enter their key. Failures are best-effort and leave text chat intact.
        refresh_stale_model_metadata()
        return load_config()

    @staticmethod
    def _configured_models(config: KiroByoConfig) -> list[tuple[str, str]]:
        return [(public_id, model.name) for public_id, model in config.public_models]

    @staticmethod
    def _model_description(model: ModelSpec) -> str:
        return ""

    @staticmethod
    def _type_model_entries(config: KiroByoConfig) -> list[tuple[str, str, str]]:
        """Return the active Mode's routing selections for an interactive ACP session.

        BYO Types are not global provider models. They are session-local routing
        selections, analogous to Crew's Jev choice. The shared dashboard/provider
        catalogue therefore stays concrete-only, while an interactive ACP session
        advertises the Type wire ids it can actually accept via ``session/set_model``.
        """
        if not config.models:
            return []
        pack = get_policy_pack(config.mode_pack)
        if pack is None:
            raise ConfigError(f"mode.id {config.mode_pack!r} is not installed")
        if not pack.enabled:
            raise ConfigError(f"mode.id {pack.id!r} is not available yet")
        mode_name = str(pack.short_name or pack.display_name or pack.id).strip() or pack.id
        rows: list[tuple[str, str, str]] = []
        for policy in pack.policies:
            type_name = str(policy.display_name or policy.id).strip() or policy.id
            if type_name.lower().endswith(" type"):
                type_name = type_name[:-5].strip() or policy.id
            rows.append((
                type_selection_id(pack.id, policy.id),
                f"{mode_name} — {type_name}",
                str(policy.short_description or "").strip(),
            ))
        return rows

    @classmethod
    def models_payload(
        cls,
        current: str,
        config: KiroByoConfig,
        *,
        include_types: bool = False,
    ) -> dict[str, Any]:
        """Return ACP model-selection state.

        ``include_types`` is intentionally session-scoped. Generic/shared model
        catalogues use the default ``False`` and remain ``auto + concrete`` only.
        Interactive chat sessions opt in so every Type shown by the Crew picker is
        also a valid id in ``availableModels`` and can pass Crew's ACP validation.
        """
        available: list[dict[str, Any]] = []
        for value, name, model in cls._available_model_entries(config):
            available.append({
                "modelId": value,
                "name": name,
                "description": cls._model_description(model) if model is not None else name,
            })

        type_ids: set[str] = set()
        if include_types:
            type_rows = cls._type_model_entries(config)
            type_ids = {value for value, _name, _description in type_rows}
            # Keep Auto as a compatibility/default entry, but make the real Type
            # selections first-class ACP ids for this interactive session.
            insert_at = 1 if available and available[0].get("modelId") == "auto" else 0
            available[insert_at:insert_at] = [
                {"modelId": value, "name": name, "description": description}
                for value, name, description in type_rows
            ]

        if include_types and current in type_ids:
            current_id = current
        else:
            current_id = "auto" if parse_type_selection(current) is not None else current
        return {"availableModels": available, "currentModelId": current_id}

    @classmethod
    def _available_model_entries(
        cls, config: KiroByoConfig
    ) -> list[tuple[str, str, ModelSpec | None]]:
        if config.models:
            concrete = [
                (public_id, model.name, model) for public_id, model in config.public_models
            ]
            return [("auto", "Auto", None), *concrete]
        return [(value, name, None) for value, name in ECHO_MODELS]

    @classmethod
    def available_models(cls, config: KiroByoConfig) -> list[tuple[str, str]]:
        """Return the shared/global model catalogue (never Type routing ids)."""
        return [(value, name) for value, name, _model in cls._available_model_entries(config)]

    @classmethod
    def default_model(cls, config: KiroByoConfig) -> str:
        # Representative model for control surfaces before a prompt exists. Keep
        # the virtual mode sentinel out of this resolver or it would recursively
        # resolve to itself instead of a real upstream model.
        models = cls._configured_models(config) or list(ECHO_MODELS)
        valid = {key for key, _ in models}
        if config.default_model:
            resolved = config.resolve_model(config.default_model)
            if resolved is not None:
                _provider, model = resolved
                public_id = config.public_model_id(model)
                if public_id in valid:
                    return public_id
        for provider in config.providers:
            if provider.default_model:
                model = provider.model(provider.default_model)
                if model is not None:
                    public_id = config.public_model_id(model)
                    if public_id in valid:
                        return public_id
        return models[0][0]

    @classmethod
    def model_config(
        cls,
        current: str,
        config: KiroByoConfig,
        *,
        include_types: bool = False,
    ) -> dict[str, Any]:
        type_rows = cls._type_model_entries(config) if include_types else []
        type_ids = {value for value, _name, _description in type_rows}
        current_value = current if include_types and current in type_ids else (
            "auto" if parse_type_selection(current) is not None else current
        )
        options = [{"value": value, "name": name} for value, name in cls.available_models(config)]
        if type_rows:
            insert_at = 1 if options and options[0].get("value") == "auto" else 0
            options[insert_at:insert_at] = [
                {"value": value, "name": name}
                for value, name, _description in type_rows
            ]
        return {
            "id": "model",
            "name": "Model",
            "category": "model",
            "type": "select",
            "currentValue": current_value,
            "options": options,
        }

    @classmethod
    def _session_advertises_types(cls, session: Session, config: KiroByoConfig) -> bool:
        """Whether this live ACP session owns the interactive Type surface.

        Crew's shared ``kirocrew-lite`` worker must stay concrete-only. Normal
        chat/member sessions may select BYO Types, and therefore must advertise
        those exact wire ids before Crew will forward a model switch.
        """
        return bool(config.models) and not cls._is_kirocrew_background_session(session)

    @classmethod
    def _session_models_payload(cls, session: Session, config: KiroByoConfig) -> dict[str, Any]:
        return cls.models_payload(
            session.model,
            config,
            include_types=cls._session_advertises_types(session, config),
        )

    @classmethod
    def _config_options(
        cls,
        session: Session,
        config: KiroByoConfig,
    ) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = [
            cls.model_config(
                session.model,
                config,
                include_types=cls._session_advertises_types(session, config),
            )
        ]
        options.append({"id": "host_tool_approval", "name": "Host tool approval",
                        "type": "select",
                        "currentValue": "required" if session.host_tool_approval_required else "unconfigured",
                        "options": [{"value": "unconfigured", "name": "Legacy saved-Agent permissions"},
                                    {"value": "required", "name": "Ask host for native/MCP tools"}]})
        selected_type = cls._type_for_selection(config, session.model)
        resolved = cls._resolved_model(config, session)
        model = resolved[1] if resolved is not None else None
        # A Type owns effort internally. It may route to models with different
        # effort vocabularies (or no effort control at all), so exposing one
        # session-wide slider would promise a capability that does not exist.
        # Concrete-model sessions remain user-adjustable. Deep-thinking remains
        # a separate per-model BYO preference in both cases.
        if selected_type is None:
            effort = cls._effort_config(model, session.effort)
            if effort is not None:
                options.append(effort)
        return options

    @staticmethod
    def _runtime_system_message(
        provider: ProviderSpec,
        model: ModelSpec,
        effort: str = "",
        *,
        thinking_mode: str = "",
        deferred_tool_search: bool = False,
        mcp_server_names: tuple[str, ...] = (),
        native_tool_names: tuple[str, ...] = (),
        mode_context: str = "",
        reasoning_language_context: str = "",
        crew_browser_available: bool = False,
    ) -> dict[str, str]:
        """Return transport/tool metadata without inventing an assistant persona.

        Crew owns the member/agent identity and Jev routing state.  Kiro BYO only
        contributes facts about the concrete upstream model serving this turn and
        the tool bridge it exposes.  This mirrors the native ACP separation: a
        backend reports model/runtime state but does not rename the Crew member.
        """
        effort_line = f"Reasoning effort: {effort}\n" if effort else "Reasoning effort: provider default\n"
        thinking_line = f"Deep thinking: {thinking_mode}\n" if thinking_mode else "Deep thinking: provider/model default\n"
        tool_search_text = ""
        if deferred_tool_search:
            servers = ", ".join(mcp_server_names) if mcp_server_names else "Crew MCP servers"
            tool_search_text = (
                "\nCrew tools are loaded on demand to keep the active tool set small. "
                f"Available server groups: {servers}. "
                f"If {DEFERRED_TOOL_SEARCH_NAME} is present, call it before using a Crew "
                "capability whose exact function is not already supplied. Search with short "
                "English capability keywords even when the user speaks another language "
                "(for example: chat history, memory recall, schedule reminder, desktop screenshot). "
                "The search only loads trusted function definitions and has no external side "
                "effect. After it returns, call one of the newly available functions on the next "
                "step; search again if none fits.\n"
            )
        native_text = ""
        if native_tool_names:
            names = ", ".join(native_tool_names)
            native_text = (
                f"\nNative workspace tools available: {names}. "
                "They are host-approved and constrained by Kiro BYO's workspace policy. "
                "If a filesystem target is outside the current session roots, Kiro BYO can "
                "ask the host/user for a narrow temporary or session-scoped directory grant; "
                "do not treat that as a reason to fall back to shell. For project inspection, "
                "prefer list_directory/glob/grep/read_files/read and batch a small set of "
                "known reads instead of issuing many shell file-inspection commands. For broad "
                "requests such as 'look at this project', start with shallow reconnaissance and "
                "do not run tests, builds, linters, package installs, or exhaustive scans unless "
                "the user explicitly asks or a concrete issue requires verification. A filesystem "
                "scope denial remains a real boundary after the user declines it: never use shell "
                "merely to bypass a refused read/list/search path. Use write/edit for file changes "
                "and shell only for commands "
                "that genuinely require process execution.\n"
            )
        # KiroCrew's core browser MCP chooses embedded panel vs. CLI fallback
        # from *live Crew settings and governance*. Never guess those settings
        # locally, launch a second browser or bypass a denied browser request.
        browser_text = (
            "\nCrew browser is available as a host-projected MCP tool. "
            "For interactive webpage operations, use that supplied browser tool "
            "rather than inventing a separate browser runner. The Crew tool "
            "will honor the current built-in-browser setting, panel availability, "
            "and fallback/permission policy. A denied tool must not be bypassed.\n"
            if crew_browser_available else ""
        )
        mode_text = f"\n{mode_context}\n" if mode_context else ""
        language_text = f"\n{reasoning_language_context}\n" if reasoning_language_context else ""
        return {
            "role": "system",
            "content": (
                "Runtime metadata for this turn (not the Crew member/agent identity):\n"
                f"Served upstream model: {model.name}\n"
                f"Provider: {provider.name}\n"
                f"API model id: {model.model_id}\n"
                f"{thinking_line}"
                f"{effort_line}"
                + mode_text
                + "Real MCP invocations are host-approved before execution.\n"
                + native_text
                + tool_search_text
                + browser_text
                + "Only tools present in the supplied tool list exist; never invent "
                "execute_bash/DSML markup or unsupported host functions."
                + language_text
            ),
        }

    def emit_text(self, session_id: str, text: str, *, is_thinking: bool = False) -> None:
        if not text:
            return
        # Official Kiro distinguishes model prose from pre-tool-call narration:
        # a reasoning/thinking chunk is folded by Crew and NEVER counted as the
        # turn's deliverable (see Crew acp/_dispatch.parse_text_chunk, which flags
        # is_thinking when an agent_message_chunk's inner content.type is
        # "thinking"/"reasoning"). Emitting narration that precedes a tool call as
        # plain "text" is what makes it leak into cron results as a second
        # notification. Mark it as thinking so only the final answer is the
        # deliverable, matching first-party behaviour.
        self.notify(
            "session/update",
            {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {
                        "type": "thinking" if is_thinking else "text",
                        "text": text,
                    },
                },
            },
        )

    def emit_runtime_error(self, session_id: str, message: str) -> None:
        self.emit_text(session_id, f"Kiro BYO error: {message}")

    def emit_usage_update(self, session_id: str, *, used: int, size: int) -> None:
        """Publish real provider context occupancy when both counts are known.

        Crew 0.9 treats ``usage_update`` as authoritative for its context meter and
        composer warning.  Never synthesize a reference window here: an unknown
        provider limit is better represented by no update than by a plausible but
        false number.
        """
        if used <= 0 or size <= 0:
            return
        self.notify(
            "session/update",
            {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "usage_update",
                    "used": int(used),
                    "size": int(size),
                },
            },
        )
        # Kiro CLI V3 moved context telemetry onto standard session/update
        # ``session_info_update`` frames. Crew 0.9 understands both shapes and
        # deliberately treats the exact-token usage_update above as authoritative,
        # so emitting the percentage companion preserves today's Crew meter while
        # keeping the backend wire-compatible with current Kiro ACP clients.
        self.notify(
            "session/update",
            {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "session_info_update",
                    "_meta": {
                        "kiro": {
                            "kind": "context_usage",
                            "usagePercentage": min(100.0, max(0.0, (float(used) / float(size)) * 100.0)),
                        }
                    },
                },
            },
        )

    def _notify_config_update(self, session: Session, config: KiroByoConfig) -> None:
        self.notify(
            "session/update",
            {
                "sessionId": session.session_id,
                "update": {
                    "sessionUpdate": "config_option_update",
                    "models": self._session_models_payload(session, config),
                    "configOptions": self._config_options(session, config),
                },
            },
        )

    @staticmethod
    def _type_for_selection(config: KiroByoConfig, selection: str, *, fallback: bool = True) -> str | None:
        selected = parse_type_selection(selection)
        if selected is None:
            return None
        pack = get_policy_pack(config.mode_pack)
        if pack is None:
            raise ConfigError(f"mode.id {config.mode_pack!r} is not installed")
        if not pack.enabled:
            raise ConfigError(f"mode.id {pack.id!r} is not available yet")
        # Type ids are local to their Mode. A persisted selection from another
        # Mode is stale after a Mode switch and resolves to the new Mode's
        # declared default only on internal restore paths. Explicit user/model
        # selection (fallback=False) must reject it.
        if selected.mode_id != pack.id:
            return pack.default_policy if fallback else None
        if pack.policy(selected.type_id) is not None:
            return selected.type_id
        return pack.default_policy if fallback else None

    @classmethod
    def _selection_kind_for(cls, config: KiroByoConfig, selection: str) -> str:
        return "type" if cls._type_for_selection(config, selection, fallback=False) is not None else "concrete"

    @classmethod
    def _selection_truth_payload(cls, session: Session, config: KiroByoConfig) -> dict[str, Any]:
        actual = cls._selection_kind_for(config, session.model)
        return {
            "id": session.model,
            "kind": actual,
            "source": str(getattr(session, "selection_source", "") or "unknown"),
            "requested": str(getattr(session, "selection_requested", "") or ""),
            "routing_applied": actual == "type",
        }

    def _ensure_selection_truth(self, session: Session, config: KiroByoConfig) -> dict[str, Any]:
        """Keep UI/session selection semantics aligned with the actual wire id."""
        actual = self._selection_kind_for(config, session.model)
        stored = str(getattr(session, "selection_kind", "") or "unknown")
        if stored != actual:
            self._runtime_event(
                "selection.truth.corrected", session,
                data={
                    "selection": session.model,
                    "stored_kind": stored,
                    "actual_kind": actual,
                    "previous_source": str(getattr(session, "selection_source", "") or ""),
                },
            )
            session.selection_kind = actual
            if not str(getattr(session, "selection_source", "") or ""):
                session.selection_source = "runtime_reconciled"
        return self._selection_truth_payload(session, config)

    @classmethod
    def _resolved_model(
        cls,
        config: KiroByoConfig,
        session: Session,
    ) -> tuple[ProviderSpec, ModelSpec] | None:
        # Concrete selections resolve directly. A Type selection routes per-turn
        # in TurnRunnerMixin once the current prompt/context is known. For
        # control surfaces that need a representative model before a prompt
        # exists, run a neutral empty-task route under the selected Type.
        selected_type = cls._type_for_selection(config, session.model)
        if selected_type is None:
            return config.resolve_model(session.model)
        decision = route_model(
            config, pack=config.mode_pack, policy=selected_type, prompt="", history=session.messages,
            has_image=False, tools_required=False, tools_available=False,
            reasoning_effort="",
        )
        return (decision.provider, decision.model) if decision is not None else None

    @staticmethod
    def _is_kirocrew_background_agent(agent: AgentProjection) -> bool:
        """Only Crew's explicit lightweight-agent projection gets this exception.

        Session IDs, background-looking workspace paths and queue names are not
        proof of a role; the host must actually project ``kirocrew-lite``.
        """
        return str(getattr(agent, "agent_id", "") or "").strip() == "kirocrew-lite"

    @classmethod
    def _is_kirocrew_background_session(cls, session: Session) -> bool:
        return cls._is_kirocrew_background_agent(session.agent)

    @classmethod
    def _background_auto_model(cls, config: KiroByoConfig, *, policy: str | None = None) -> str:
        """Resolve background ``auto`` to one concrete model, never a BYO Type.

        Crew background one-liners are short, tool-free utility turns.  Feeding
        their ``auto`` sentinel into the interactive Type surface made the shared
        ``_bg`` session enter composite routing/orchestration that Crew never
        asked for.  A neutral route through the CURRENT pack/type preserves the
        operator's routing policy while materialising exactly one real model.
        """
        decision = route_model(
            config,
            pack=config.mode_pack,
            policy=policy or config.mode_type,
            prompt="",
            history=[],
            has_image=False,
            tools_required=False,
            tools_available=False,
            reasoning_effort="",
        )
        if decision is not None:
            return config.public_model_id(decision.model)
        return config.public_models[0][0] if config.public_models else ""

    def _set_model(
        self, session: Session, config: KiroByoConfig, requested: str, *, source: str = "user_set_config"
    ) -> bool:
        requested = str(requested or "").strip()
        if requested.lower() == "auto" and config.models and self._is_kirocrew_background_session(session):
            concrete = self._background_auto_model(config)
            resolved = config.resolve_model(concrete) if concrete else None
            if resolved is None:
                return False
            _provider, model = resolved
            session.model = config.public_model_id(model)
            session.selection_kind = "concrete"
            session.selection_source = "background_auto"
            session.selection_requested = requested
            append_lifecycle(
                session, "selection/changed",
                {
                    "selection": session.model,
                    "kind": "concrete",
                    "source": session.selection_source,
                    "requested": requested,
                },
            )
            self._runtime_event("selection.changed", session, data=self._selection_truth_payload(session, config))
            self._normalize_session_reasoning(session, model)
            return True
        # Crew background workers have a concrete-only model catalogue. A
        # virtual Type id must not bypass it through ACP model controls.
        if self._is_kirocrew_background_session(session) and parse_type_selection(requested) is not None:
            return False
        requested_type = (
            config.mode_type if requested.lower() == "auto" and config.models
            else self._type_for_selection(config, requested, fallback=False)
        )
        if requested_type is not None:
            if not config.models:
                return False
            session.model = type_selection_id(config.mode_pack, requested_type)
            session.selection_kind = "type"
            session.selection_source = str(source or "user_set_config")
            session.selection_requested = requested
            append_lifecycle(
                session, "selection/changed",
                {"selection": session.model, "kind": "type", "source": session.selection_source, "requested": requested},
            )
            self._runtime_event("selection.changed", session, data=self._selection_truth_payload(session, config))
            # Type owns reasoning effort; a concrete model's manual override
            # must not persist as a hidden constraint after Type selection.
            session.effort = ""
            session.effort_user_set = False
            resolved = self._resolved_model(config, session)
            self._normalize_session_reasoning(session, resolved[1] if resolved is not None else None)
            session.effort = ""
            session.effort_user_set = False
            return True
        resolved = config.resolve_model(requested)
        if resolved is None:
            return False
        _provider, model = resolved
        session.model = config.public_model_id(model)
        session.selection_kind = "concrete"
        session.selection_source = str(source or "user_set_config")
        session.selection_requested = requested
        append_lifecycle(
            session, "selection/changed",
            {"selection": session.model, "kind": "concrete", "source": session.selection_source, "requested": requested},
        )
        self._runtime_event("selection.changed", session, data=self._selection_truth_payload(session, config))
        self._normalize_session_reasoning(session, model)
        return True


    # ---- Full-duplex control plane / durable sessions ----------------------

    def _replay_session_history(self, session: Session) -> None:
        """Replay persisted user/assistant text before ``session/load`` returns.

        ACP v1 requires load to reconstruct transcript state by sending ordinary
        ``session/update`` frames before the response.  KiroCrew 0.9 registers its
        live queue after load completes, so these frames do not duplicate its own
        transcript while standards-compliant clients still receive replay.
        """
        for message in session.messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "")
            if role not in {"user", "assistant"}:
                continue
            content = message.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "".join(
                    str(part.get("text") or "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            else:
                text = ""
            if not text:
                continue
            update = {
                "sessionUpdate": "user_message_chunk" if role == "user" else "agent_message_chunk",
                "content": {"type": "text", "text": text},
                "_meta": {"kiro": {"replay": True}},
            }
            self.notify("session/update", {"sessionId": session.session_id, "update": update})

    def _restore_session(
        self,
        session_id: str,
        params: dict[str, Any],
        *,
        replay: bool,
    ) -> tuple[Session, KiroByoConfig]:
        # Reserve this ID before reading persistence or constructing runtimes.
        # Concurrent session/load, resume, close, and prompt cannot replace a
        # live turn or publish two different objects for the same ID.
        with self._sessions_lock:
            if session_id in self._restoring_sessions or session_id in self._retiring_sessions:
                raise SessionBusyError("session restore/retirement already in progress")
            old = self.sessions.get(session_id)
            if old is not None:
                if old.retired or old.replacing or old.active_turn or old.turn_lock.locked():
                    raise SessionBusyError("session has an active turn; restore refused")
                old.replacing = True
            self._restoring_sessions.add(session_id)
        try:
            return self._restore_session_impl(session_id, params, replay=replay)
        finally:
            with self._sessions_lock:
                self._restoring_sessions.discard(session_id)
                if old is not None:
                    old.replacing = False

    def _restore_session_impl(
        self,
        session_id: str,
        params: dict[str, Any],
        *,
        replay: bool,
    ) -> tuple[Session, KiroByoConfig]:
        record = self._load_persisted_record(session_id)
        if record is None:
            raise KeyError(session_id)
        config = self._config()
        fallback_selection = type_selection_id(config.mode_pack, config.mode_type) if config.models else self.default_model(config)
        model_id = str(record.get("model") or "").strip() or fallback_selection
        selection_source = str(record.get("selection_source") or "restored_legacy")
        selection_requested = str(record.get("selection_requested") or "")
        effort = str(record.get("effort") or "")
        effort_user_set = bool(record.get("effort_user_set", False))
        selected_type = self._type_for_selection(config, model_id) if config.models else None
        if selected_type is not None and config.models:
            # Since 0.1.5 a Type always owns effort. Clear legacy session locks
            # persisted by 0.1.4 so an old concrete-model choice cannot filter
            # the Type's candidate pool after upgrade.
            effort = ""
            effort_user_set = False
            # A persisted Type from a previous Mode may no longer exist. Switching
            # Mode intentionally resets such stale selections to the new Mode's
            # declared default Type rather than treating it as a provider model.
            model_id = type_selection_id(config.mode_pack, selected_type)
            history = record.get("messages") if isinstance(record.get("messages"), list) else []
            decision = route_model(
                config, pack=config.mode_pack, policy=selected_type, prompt="", history=history,
                has_image=False, tools_required=False, tools_available=False, reasoning_effort="",
            )
            resolved = (decision.provider, decision.model) if decision is not None else None
        elif model_id in {MODEL_ECHO, MODEL_UPPER} and not config.models:
            resolved = None
        else:
            resolved = config.resolve_model(model_id)
            if resolved is None:
                model_id = fallback_selection
                selection_source = "restore_fallback"
                if config.models:
                    selected_type = self._type_for_selection(config, model_id)
                    decision = route_model(
                        config, pack=config.mode_pack, policy=config.mode_type, prompt="", history=[],
                        has_image=False, tools_required=False, tools_available=False, reasoning_effort="",
                    )
                    resolved = (decision.provider, decision.model) if decision is not None else None
                else:
                    selected_type = None
                    resolved = None
        thinking_mode = str(record.get("thinking_mode") or "")
        thinking_user_set = bool(record.get("thinking_user_set", False))
        if (
            selected_type is None
            and resolved is not None
            and not effort_user_set
            and effort not in resolved[1].effort_supported_levels
        ):
            effort = self._effort_default(resolved[1])
        persisted_cwd = str(record.get("cwd") or "").strip()
        cwd, additional_directories = self._session_paths_from_params(
            params,
            expected_cwd=persisted_cwd,
        )
        agent = self._custom_agent_from_params(params)
        has_host_link, host_link = work_link_from_params(params)
        mcp = self._mcp_from_params(params)
        native = self._native_from_params(cwd, additional_directories)
        # Explicitly supplied empty/invalid current Crew agent projections are
        # authoritative: do not resurrect a deleted/disabled saved Agent from
        # BYO's old session history. Legacy clients that supply no projection
        # retain the previous read-only fallback (never persisted approvals).
        meta = params.get("_meta")
        kiro_meta = meta.get("kiro") if isinstance(meta, dict) else None
        # Any current Crew _meta.kiro envelope is authoritative about whether
        # it supplies an active Saved Agent. An absent customAgents key in such
        # an envelope does not authorise resurrecting an old Agent from disk.
        # A truly legacy ACP client without the Crew envelope retains the
        # backward-compatible display/prompt projection below (never grants).
        crew_reprojected_agents = isinstance(kiro_meta, dict)
        agent = resolve_restored_agent_projection(
            agent, record, crew_reprojected_agents=crew_reprojected_agents,
        )
        if config.models and selected_type is not None and self._is_kirocrew_background_agent(agent):
            # Legacy records may have accidentally persisted an interactive
            # Tiga Type for Crew's lightweight worker. Normalize on restore,
            # but never overwrite an already persisted concrete host choice.
            concrete = self._background_auto_model(config, policy=selected_type)
            resolved = config.resolve_model(concrete) if concrete else None
            if resolved is None:
                raise ConfigError("Crew background role has no eligible concrete model")
            model_id = config.public_model_id(resolved[1])
            selection_source = "background_auto_restore"
            selection_requested = selection_requested or "auto"
            selected_type = None
            effort = self._effort_default(resolved[1])
            effort_user_set = False
        session = Session(
            session_id=session_id,
            cwd=cwd,
            model=model_id,
            selection_kind="type" if selected_type is not None else "concrete",
            selection_source=selection_source,
            selection_requested=selection_requested,
            additional_directories=additional_directories,
            effort=effort,
            thinking_mode=thinking_mode,
            effort_user_set=effort_user_set,
            thinking_user_set=thinking_user_set,
            agent=agent,
            host_work_link=derive_work_link(record.get("events") or []),
            messages=[m for m in record.get("messages", []) if isinstance(m, dict)],
            events=[e for e in record.get("events", []) if isinstance(e, dict)],
            mcp=mcp,
            native=native,
            created_at=float(record.get("created_at") or time.time()),
            loaded_tool_names=[
                str(name) for name in (record.get("loaded_tool_names") or [])
                if isinstance(name, str) and name
            ][-DEFERRED_TOOL_ACTIVE_LIMIT:],
            route_pins={
                str(key): (str(value[0]), str(value[1]))
                for key, value in (record.get("route_pins") or {}).items()
                if isinstance(key, str)
                and isinstance(value, (list, tuple))
                and len(value) == 2
                and all(isinstance(item, str) and item for item in value)
            } if isinstance(record.get("route_pins"), dict) else {},
            updated_at=float(record.get("updated_at") or time.time()),
            storage_revision=int(record.get("storage_revision", 0)),
            storage_loaded=True,
        )
        self._normalize_session_reasoning(
            session,
            resolved[1] if resolved is not None else None,
        )
        if selected_type is not None:
            # Type-owned effort must stay automatic even when loading a session
            # created by a pre-0.1.5 build.
            session.effort = ""
            session.effort_user_set = False
        host_link_changed = bind_work_link(session, host_link) if has_host_link else False
        append_lifecycle(
            session,
            "session/resume",
            {
                "source": "load" if replay else "resume",
                "model_selection": model_id,
                "selection_kind": session.selection_kind,
                "selection_source": session.selection_source,
                "selection_requested": session.selection_requested,
                "host_adapter": adapter_for_session_params(params).name,
            },
        )
        self._runtime_event(
            "selection.resolved", session, data=self._selection_truth_payload(session, config)
        )
        self._reconcile_restored_durable_runs(session, session_id)
        # A new host binding is durable before we acknowledge it. Ordinary
        # session/load stays read-only (and preserves its historical fast path).
        if host_link_changed and not self._persist_session(session):
            raise SessionBusyError("could not persist Crew work link; reload session")
        try:
            self._put_session(session, restoring=True)
        except Exception:
            # A concurrent prompt may win the race after restore began.
            # Never leave the new candidate's resources running if rejected.
            if session.mcp is not None:
                session.mcp.close()
            if session.native is not None:
                session.native.close()
            raise
        if replay:
            self._replay_session_history(session)
        return session, config

    def _reconcile_restored_durable_runs(self, session: Session, session_id: str) -> None:
        """Classify orphaned runs without replay or erasing side-effect evidence."""
        # A run left in a non-terminal state means the previous harness process
        # disappeared before it could close the Run. Mark that fact explicitly;
        # hotfix6 exposes the recovery boundary but does not auto-replay side
        # effects or pretend child transcripts survived the restart.
        run_store = self._durable_run_store()
        pending_runs = [
            row for row in run_store.pending(session_id=session_id)
            if not run_store.is_lease_active(row)
        ]
        if pending_runs:
            reconciled = []
            for pending in pending_runs:
                run_id = str(pending.get("run_id") or "")
                if run_id:
                    state = run_store.restart_recovery_state(pending)
                    try:
                        run_store.mark_recovery(run_id, state)
                        reconciled.append(pending)
                    except ValueError:
                        # A concurrent healthy owner may have renewed a lease.
                        # Never classify that run as orphaned.
                        continue
            pending_runs = reconciled
            if pending_runs:
                try:
                    stale_agents = self._agent_state_store().mark_session_stale(session_id)
                except OSError as exc:
                    # Advisory projection must not prevent session reconciliation.
                    stale_agents = 0
                    self.trace(f"agent state projection unavailable: {type(exc).__name__}")
                append_lifecycle(
                    session, "run/recovery_pending",
                    {"run_ids": [str(row.get("run_id") or "") for row in pending_runs], "stale_agents": stale_agents},
                )
                self._runtime_event(
                    "run.recovery.pending", session,
                    data={"runs": len(pending_runs), "stale_agents": stale_agents},
                )

    def _delete_persisted_session(self, session_id: str) -> None:
        try:
            self._session_path(session_id).unlink(missing_ok=True)
            self._legacy_session_path(session_id).unlink(missing_ok=True)
        except OSError as exc:
            self.trace(f"session delete failed session={session_id}: {exc}")


    @staticmethod
    def _session_paths_from_params(
        params: dict[str, Any],
        *,
        expected_cwd: str = "",
    ) -> tuple[str, tuple[str, ...]]:
        """Validate ACP v1 ``cwd`` + ``additionalDirectories``.

        ACP owns these roots, not Kiro BYO's persisted session file. On
        load/resume the client must send the complete intended list again;
        omitting it means no extra roots. ``cwd`` is immutable for a persisted
        session, matching the stable v1 session setup contract.
        """
        cwd_raw = str(params.get("cwd") or expected_cwd or "").strip()
        if not cwd_raw:
            raise ValueError("cwd is required")
        cwd_path = Path(cwd_raw).expanduser()
        if not cwd_path.is_absolute():
            raise ValueError("cwd must be an absolute path")
        try:
            cwd_path = cwd_path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"cwd is unavailable: {exc}") from exc
        if not cwd_path.is_dir():
            raise ValueError(f"cwd is not a directory: {cwd_path}")

        if expected_cwd:
            try:
                expected = Path(expected_cwd).expanduser().resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise ValueError(f"persisted cwd is unavailable: {exc}") from exc
            if os.path.normcase(os.path.abspath(str(expected))) != os.path.normcase(os.path.abspath(str(cwd_path))):
                raise ValueError(
                    f"cwd does not match the persisted session root: expected {expected}, got {cwd_path}"
                )

        raw_extra = params.get("additionalDirectories", [])
        if raw_extra is None:
            raw_extra = []
        if not isinstance(raw_extra, list):
            raise ValueError("additionalDirectories must be an array of absolute directories")
        extras: list[str] = []
        seen: set[str] = set()
        cwd_key = os.path.normcase(os.path.abspath(str(cwd_path)))
        for raw in raw_extra:
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError("additionalDirectories entries must be non-empty strings")
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                raise ValueError(f"additionalDirectories entry must be absolute: {raw}")
            try:
                candidate = candidate.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise ValueError(f"additionalDirectories entry is unavailable: {raw}: {exc}") from exc
            if not candidate.is_dir():
                raise ValueError(f"additionalDirectories entry is not a directory: {candidate}")
            key = os.path.normcase(os.path.abspath(str(candidate)))
            if key == cwd_key or key in seen:
                continue
            seen.add(key)
            extras.append(str(candidate))
        return str(cwd_path), tuple(extras)

    @staticmethod
    def _mcp_from_params(params: dict[str, Any]) -> McpManager:
        raw_mcp = params.get("mcpServers") or []
        mcp = McpManager(raw_mcp if isinstance(raw_mcp, list) else [])
        # Match Crew's eager-spawn intent without making ACP session/new wait for
        # three Python broker handshakes.  Tool discovery runs concurrently and
        # the first prompt joins it only if the catalogue is still unknown.
        mcp.start_background()
        return mcp

    def _native_from_params(
        self,
        cwd: str,
        additional_directories: tuple[str, ...] = (),
    ) -> NativeToolRuntime | None:
        if not cwd:
            return None
        try:
            return NativeToolRuntime(cwd, additional_roots=additional_directories)
        except (SandboxError, OSError) as exc:
            # A missing/unusable workspace must fail closed for host tools without
            # taking down text chat or Crew MCP. Never silently widen to process cwd.
            self.trace(f"native runtime disabled cwd={cwd!r}: {exc}")
            return None

    @staticmethod
    def _custom_agent_from_params(params: dict[str, Any]) -> AgentProjection:
        """Consume the saved Crew agent projection for this session.

        Tool visibility and the active agent's ``allowedTools`` are preserved.
        Most authorization remains a Crew permission decision plus the local
        sandbox; an ``allowedTools`` match intentionally skips the prompt, matching
        Kiro's saved-agent contract.
        """
        return from_session_params(params)

    @staticmethod
    def _current_mode_id(session: Session) -> str:
        return session.agent.agent_id or "default"

    def _session_start_payload(self, session: Session, config: KiroByoConfig) -> dict[str, Any]:
        mode_id = self._current_mode_id(session)
        mode_name = session.agent.agent_id or "Default"
        mode_description = (
            "Crew saved agent projection"
            if session.agent.agent_id
            else "Kiro BYO default session mode"
        )
        # ``modes`` is intentionally present on *every* session/new and
        # session/load response. KiroCrew 0.9's shared AcpRuntime uses that
        # response field as positive evidence that a persisted session was really
        # restored. Omitting it on an ordinary Kiro BYO session makes a successful
        # backend load look like a failed resume to the host.
        recoverable: list[dict[str, Any]] = []
        try:
            for row in self._durable_run_store().list(limit=10, session_id=session.session_id):
                resume = RecoveryPolicy.durable_run(row, strategy="resume")
                replan = RecoveryPolicy.durable_run(row, strategy="replan")
                if not resume.safe and not replan.safe:
                    continue
                recoverable.append({
                    "runId": str(row.get("run_id") or ""),
                    "status": str(row.get("status") or ""),
                    "recoveryState": str(row.get("recovery_state") or "clean"),
                    "resume": bool(resume.safe),
                    "replan": bool(replan.safe),
                })
        except OSError:
            recoverable = []
        return {
            "sessionId": session.session_id,
            "models": self._session_models_payload(session, config),
            "configOptions": self._config_options(session, config),
            "modes": {
                "availableModes": [
                    {
                        "id": mode_id,
                        "name": mode_name,
                        "description": mode_description,
                    }
                ],
                "currentModeId": mode_id,
            },
            "_meta": {
                "kiro": {
                    "durableRecovery": {
                        "runs": recoverable,
                        "methods": ["_kiro/run/retry", "_kiro/run/resume", "_kiro/run/replan"],
                    },
                    **({"byoWorkLink": dict(session.host_work_link)} if session.host_work_link else {}),
                }
            },
        }

    def _resolve_client_response(self, msg: dict[str, Any]) -> bool:
        """Route a client response to the worker that issued the server request."""
        req_id = msg.get("id")
        if req_id is None or msg.get("method") is not None:
            return False
        key = str(req_id)
        with self._pending_lock:
            pending = self._pending_client_requests.get(key)
        if pending is None:
            return False
        pending.response = msg
        pending.event.set()
        return True

    def _request_client(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = 300.0,
        cancel_event: threading.Event | None = None,
        should_interrupt: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Send an agent鈫抍lient JSON-RPC request without stealing stdin.

        ``run()`` remains the sole stdin reader.  It resolves responses into this
        registry, while the prompt worker blocks on an Event.  This is the key
        full-duplex primitive needed by ``session/request_permission``.
        """
        req_id = f"kiro-byo-client-{next(self._client_request_ids)}"
        pending = PendingClientRequest()
        with self._pending_lock:
            self._pending_client_requests[req_id] = pending
        deadline = time.monotonic() + timeout
        try:
            # A failed/overloaded outbound transport raises synchronously.
            # Keep send inside try so the pending request is always removed.
            self.send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ClientRequestError(f"client request timed out: {method}")
                if pending.event.wait(timeout=min(0.1, remaining)):
                    break
                if should_interrupt is not None and should_interrupt():
                    raise ClientRequestError(f"client request interrupted: {method}")
                if cancel_event is not None and cancel_event.is_set():
                    raise ClientRequestError(f"client request cancelled: {method}")
            response = pending.response or {}
            error = response.get("error")
            if isinstance(error, dict):
                raise ClientRequestError(str(error.get("message") or error))
            result = response.get("result")
            return result if isinstance(result, dict) else {}
        finally:
            with self._pending_lock:
                self._pending_client_requests.pop(req_id, None)

    @staticmethod
    def _unwrap_steer_message(message: str) -> str:
        text = str(message or "").strip()
        prefix = "<user_message>"
        suffix = "</user_message>"
        if text.startswith(prefix) and text.endswith(suffix):
            text = text[len(prefix) : -len(suffix)].strip()
        return text

    def _emit_steer_info(
        self, session: Session, kind: str, text: str = "", message_id: str = ""
    ) -> None:
        kiro: dict[str, Any] = {"kind": kind}
        if text:
            kiro["content"] = text
        if message_id:
            kiro["messageId"] = message_id
        self.notify(
            "session/update",
            {
                "sessionId": session.session_id,
                "update": {
                    "sessionUpdate": "session_info_update",
                    "_meta": {"kiro": kiro},
                },
            },
        )

    def _queue_steer(
        self, session: Session, message: str, message_id: str
    ) -> tuple[bool, str]:
        """Admit a single Crew-owned steering receipt without duplicate delivery.

        The true/false return remains Crew's queued acknowledgement; status is
        diagnostic only. A completed receipt is retained across turn boundaries
        so a retried messageId cannot be executed a second time.
        """
        text = self._unwrap_steer_message(message)
        if not text:
            return False, "empty"
        with session.state_lock:
            prior = session.steer_receipts.get(message_id)
            if prior is not None:
                previous_text, status = prior
                if previous_text != text:
                    return False, "conflicting_message_id"
                return status in {"queued", "injected"}, f"duplicate_{status}"
            if not session.active_turn or not session.steer_accepting or session.cancel_event.is_set():
                return False, "session_not_accepting"
            # Only a small number of pending corrections should be buffered
            # inside a running turn. The durable, unbounded queue belongs to Crew.
            if len(session.steer_queue) >= 64:
                return False, "steer_queue_full"
            if len(session.steer_receipts) >= 256:
                old_id = next((key for key, value in session.steer_receipts.items()
                               if value[1] != "queued"), "")
                if not old_id:
                    return False, "steer_receipts_full"
                del session.steer_receipts[old_id]
            session.steer_queue.append(text)
            session.steer_pending_ids.append(message_id)
            session.steer_receipts[message_id] = (text, "queued")
            # Order the queued event before a worker can observe the interrupt
            # and report injected; both actions are serialized by state_lock.
            self._emit_steer_info(session, "steering_queued", text, message_id)
            session.steer_event.set()
        return True, "queued"

    def _clear_steers(self, session: Session, *, notify: bool = True) -> bool:
        """Clear only unconsumed steering, recording that it was NOT injected."""
        with session.state_lock:
            pending = tuple(session.steer_pending_ids)
            had_pending = bool(session.steer_queue) or session.steer_event.is_set()
            for message_id in pending:
                prior = session.steer_receipts.get(message_id)
                if prior is not None and prior[1] == "queued":
                    session.steer_receipts[message_id] = (prior[0], "cleared")
            session.steer_pending_ids.clear()
            session.steer_queue.clear()
            session.steer_event.clear()
            session.steer_accepting = False
        if had_pending and notify:
            self._emit_steer_info(session, "steering_cleared")
        return had_pending

    def _emit_steer_consumed(
        self, session: Session, text: str, message_id: str = ""
    ) -> None:
        # The receipt follows insertion into the provider-facing transcript.
        # It is not proof that a model finished executing the correction.
        self._emit_steer_info(session, "steering_injected", text, message_id)

    @staticmethod
    def _interrupt_requested(session: Session) -> bool:
        return session.cancel_event.is_set() or session.steer_event.is_set()


    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        # Notifications must remain cheap and non-blocking. In particular cancel
        # is handled by the sole stdin reader while the prompt runs on a worker.
        if method != "session/cancel":
            return
        session_id = str(params.get("sessionId") or "")
        session = self._get_session(session_id)
        if session is not None:
            now = time.perf_counter()
            with session.state_lock:
                session.cancel_source = "client_session_cancel"
                session.cancel_requested_at = now
                active = bool(session.active_turn)
                elapsed_ms = (
                    max(0.0, (now - session.turn_started_at) * 1000.0)
                    if session.turn_started_at > 0.0 else -1.0
                )
                session.cancel_event.set()
            self._runtime_event(
                "control.cancel.requested",
                session,
                data={
                    "source": "client_session_cancel",
                    "active_turn": active,
                    "elapsed_ms": elapsed_ms,
                },
            )
            self._clear_steers(session)

    def _handle_initialize_request(self, req_id: Any, params: dict[str, Any]) -> None:
        del params
        self.result(req_id, initialize_result())

    def _handle_session_new_request(self, req_id: Any, params: dict[str, Any]) -> None:
        t_session = time.perf_counter()
        try:
            config = self._config()
        except ConfigError as exc:
            self.error(req_id, -32602, f"Kiro BYO configuration error: {exc}")
            return
        t_config = time.perf_counter()
        session_id = f"kiro-byo-{uuid.uuid4()}"
        selected_model = type_selection_id(config.mode_pack, config.mode_type) if config.models else self.default_model(config)
        selection_source = "default_type" if config.models else "default_model"
        selection_requested = ""
        host_adapter = adapter_for_session_params(params)
        host_hints = host_adapter.session_hints(params)
        requested_model = host_hints.requested_model
        # The Crew role is authoritative before resolving the starting model:
        # background ``auto`` must not accidentally become an interactive Type.
        try:
            agent = self._custom_agent_from_params(params)
        except ValueError as exc:
            self.error(req_id, -32602, str(exc))
            return
        if requested_model:
            requested_type = self._type_for_selection(config, requested_model, fallback=False)
            if requested_type is not None:
                selected_model = type_selection_id(config.mode_pack, requested_type)
                selection_source = "host_session_new"
                selection_requested = requested_model
            elif config.resolve_model(requested_model) is not None:
                selected_model = requested_model
                selection_source = "host_session_new"
                selection_requested = requested_model
        selected_type = self._type_for_selection(config, selected_model) if config.models else None
        if config.models and selected_type is not None and self._is_kirocrew_background_agent(agent):
            # Crew's lightweight background role is one concrete model per
            # request. Honor an explicit host concrete model above; materialise
            # only the automatic/Type selection here, before the ACP session is
            # published. Do not turn a background task into composite Tiga work.
            concrete = self._background_auto_model(config, policy=selected_type)
            if not concrete or config.resolve_model(concrete) is None:
                self.error(req_id, -32602, "Crew background role has no eligible concrete model")
                return
            selected_model = concrete
            selection_source = "background_auto"
            selection_requested = requested_model or "auto"
            selected_type = None
        if config.models and selected_type is not None:
            selected_model = type_selection_id(config.mode_pack, selected_type)
            decision = route_model(
                config, pack=config.mode_pack, policy=selected_type, prompt="", history=[],
                has_image=False, tools_required=False, tools_available=False, reasoning_effort="",
            )
            resolved = (decision.provider, decision.model) if decision is not None else None
        else:
            resolved = config.resolve_model(selected_model) if config.models else None
        effective_model = resolved[1] if resolved is not None else None
        # Types own effort internally and therefore start with no session-wide
        # effort state. Concrete-model sessions may still accept an explicit
        # host effort selection when that model actually advertises it.
        initial_effort = "" if selected_type is not None else self._effort_default(effective_model)
        initial_thinking = self._thinking_default(effective_model)
        requested_effort = host_hints.requested_effort
        effort_user_set = False
        if (
            selected_type is None
            and effective_model is not None
            and requested_effort in effective_model.selectable_effort_levels
        ):
            initial_effort = requested_effort
            effort_user_set = True
        requested_thinking = host_hints.requested_thinking
        thinking_user_set = False
        if effective_model is not None and requested_thinking in self._thinking_choices(effective_model):
            initial_thinking = requested_thinking
            thinking_user_set = True
        try:
            cwd, additional_directories = self._session_paths_from_params(params)
        except ValueError as exc:
            self.error(req_id, -32602, str(exc))
            return
        try:
            _, host_work_link = work_link_from_params(params)
        except ValueError as exc:
            self.error(req_id, -32602, str(exc))
            return
        try:
            mcp = self._mcp_from_params(params)
        except Exception as exc:
            self.error(req_id, -32603, f"failed to start session MCP servers: {exc}")
            return
        t_mcp = time.perf_counter()
        native = self._native_from_params(cwd, additional_directories)
        session = Session(
            session_id=session_id,
            cwd=cwd,
            model=selected_model,
            selection_kind="type" if selected_type is not None else "concrete",
            selection_source=selection_source,
            selection_requested=selection_requested,
            effort=initial_effort,
            thinking_mode=initial_thinking,
            effort_user_set=effort_user_set,
            thinking_user_set=thinking_user_set,
            additional_directories=additional_directories,
            agent=agent,
            mcp=mcp,
            native=native,
        )
        if host_work_link:
            bind_work_link(session, host_work_link)
        append_lifecycle(
            session,
            "session/start",
            {
                "source": "new",
                "model_selection": selected_model,
                "selection_kind": session.selection_kind,
                "selection_source": session.selection_source,
                "selection_requested": session.selection_requested,
                "host_adapter": host_hints.host,
            },
        )
        self._runtime_event(
            "selection.resolved", session, data=self._selection_truth_payload(session, config)
        )
        # New and restored sessions share the same fenced orphan-run
        # reconciliation; neither path is allowed to replay side effects.
        self._reconcile_restored_durable_runs(session, session_id)
        self._put_session(session)
        self._persist_session(session)
        self.result(req_id, self._session_start_payload(session, config))
        t_done = time.perf_counter()
        self.trace(
            f"session/new total={(t_done-t_session)*1000:.0f}ms "
            f"config={(t_config-t_session)*1000:.0f}ms "
            f"mcp_schedule={(t_mcp-t_config)*1000:.0f}ms "
            f"servers={len(mcp.servers)} cached_servers={len(mcp.catalog_cache_hits)} "
            f"cached_tools={len(mcp.bindings)} native_tools={len(native.specs()) if native else 0} "
            f"warmup=background"
        )

    def _handle_session_restore_request(
        self, req_id: Any, params: dict[str, Any], *, method: str
    ) -> None:
        t_session = time.perf_counter()
        session_id = str(params.get("sessionId") or "")
        if not session_id:
            self.error(req_id, -32602, "sessionId is required")
            return
        try:
            session, config = self._restore_session(
                session_id,
                params,
                replay=(method == "session/load"),
            )
        except SessionBusyError as exc:
            self.error(req_id, -32002, str(exc))
            return
        except KeyError:
            self.error(req_id, -32004, f"session not found: {session_id}")
            return
        except ConfigError as exc:
            self.error(req_id, -32602, f"Kiro BYO configuration error: {exc}")
            return
        except ValueError as exc:
            self.error(req_id, -32602, str(exc))
            return
        except Exception as exc:
            self.error(req_id, -32603, f"failed to restore session: {exc}")
            return
        self.result(req_id, self._session_start_payload(session, config))
        self.trace(
            f"{method} total={(time.perf_counter()-t_session)*1000:.0f}ms "
            f"session={session_id} messages={len(session.messages)} "
            f"servers={len(session.mcp.servers) if session.mcp else 0} "
            f"cached_tools={len(session.mcp.bindings) if session.mcp else 0} "
            f"native_tools={len(session.native.specs()) if session.native else 0} "
            f"replay={method == 'session/load'}"
        )

    def _handle_session_list_request(self, req_id: Any, params: dict[str, Any]) -> None:
        cwd_filter = str(params.get("cwd") or "").strip()
        cursor_raw = str(params.get("cursor") or "").strip()
        try:
            offset = max(0, int(cursor_raw)) if cursor_raw else 0
        except ValueError:
            self.error(req_id, -32602, "invalid session/list cursor")
            return
        records = sorted(
            self._persisted_session_records(),
            key=lambda item: float(item.get("updated_at") or 0.0),
            reverse=True,
        )
        infos = [info for record in records if (info := self._session_info(record)) is not None]
        if cwd_filter:
            infos = [info for info in infos if info.get("cwd") == cwd_filter]
        page_size = 100
        page = infos[offset : offset + page_size]
        result: dict[str, Any] = {"sessions": page}
        if offset + page_size < len(infos):
            result["nextCursor"] = str(offset + page_size)
        self.result(req_id, result)

    def _handle_session_steer_request(self, req_id: Any, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "").strip()
        if not session_id:
            self.error(req_id, -32602, "sessionId is required")
            return
        session = self._get_session(session_id)
        if session is None:
            self.error(req_id, -32004, f"session not found: {session_id}")
            return
        message = self._unwrap_steer_message(str(params.get("message") or ""))
        if not message:
            self.error(req_id, -32602, "steer message is required")
            return
        message_id = str(params.get("messageId") or "").strip() or f"steer-{uuid.uuid4()}"
        queued, status = self._queue_steer(session, message, message_id)
        if status == "conflicting_message_id":
            self.error(req_id, -32602, "messageId already used for different steering text")
            return
        result: dict[str, Any] = {"queued": queued, "messageId": message_id}
        if status.startswith("duplicate_"):
            result["duplicate"] = status.removeprefix("duplicate_")
        elif not queued:
            result["dropped"] = status
        self.result(req_id, result)

    def _handle_session_compact_request(self, req_id: Any, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "").strip()
        if not session_id:
            self.error(req_id, -32602, "sessionId is required")
            return
        session = self._get_session(session_id)
        if session is None:
            self.error(req_id, -32004, f"session not found: {session_id}")
            return
        preserve_context = str(params.get("context") or "")
        self.result(
            req_id,
            {"success": self._run_manual_compaction(
                session, preserve_context=preserve_context
            )},
        )

    def _handle_session_set_config_option_request(
        self, req_id: Any, params: dict[str, Any]
    ) -> None:
        session_id = str(params.get("sessionId") or "")
        session = self._get_session(session_id)
        if session is None:
            self.error(req_id, -32602, "unknown session")
            return
        config_id = str(params.get("configId") or "")
        value = str(params.get("value") or "")
        try:
            config = self._config()
        except ConfigError as exc:
            self.error(req_id, -32602, f"Kiro BYO configuration error: {exc}")
            return

        # Model/config selection is the next-turn control plane. Never mutate a
        # session while its model/tool loop owns the turn lock; a simultaneous
        # selection would otherwise alter the active turn mid-flight.
        if not session.turn_lock.acquire(blocking=False):
            self.error(req_id, -32002, "session has an active turn; retry configuration after it completes")
            return
        # Even an invalid option must not reply before the lock is released:
        # clients often retry immediately when they receive an ACP error.
        self._begin_prompt_response_buffer(req_id)
        notify_changed = False
        try:
            with session.state_lock:
                if config_id == "host_tool_approval":
                    # Host gate is monotone and never persisted across sessions.
                    if value != "required":
                        self.error(req_id, -32602, "host tool approval cannot be disabled via ACP")
                        return
                    session.host_tool_approval_required = True
                elif config_id == MODEL_CONFIG_ID:
                    if not self._set_model(session, config, value):
                        self.error(req_id, -32602, f"unknown model: {value}")
                        return
                elif config_id == THINKING_CONFIG_ID:
                    resolved = self._resolved_model(config, session)
                    if resolved is None:
                        self.error(req_id, -32602, "current model is no longer configured")
                        return
                    _provider, model = resolved
                    canonical = value.strip().lower()
                    if canonical not in self._thinking_choices(model):
                        self.error(req_id, -32602, f"unsupported deep-thinking mode for {model.name}: {value}")
                        return
                    session.thinking_mode = canonical
                    session.thinking_user_set = True
                elif config_id == EFFORT_CONFIG_ID or config_id in LEGACY_EFFORT_CONFIG_IDS:
                    if self._type_for_selection(config, session.model) is not None:
                        self.error(req_id, -32602, "reasoning effort is managed automatically by the active Type")
                        return
                    resolved = self._resolved_model(config, session)
                    if resolved is None:
                        self.error(req_id, -32602, "current model is no longer configured")
                        return
                    _provider, model = resolved
                    canonical = value.strip().lower()
                    # Crew's "use default reasoning effort" is BYO's AUTO state:
                    # once enabled, the active Type may select among this model's
                    # advertised effort levels for each invocation. Be liberal in
                    # what we accept because Crew versions have used empty/default
                    # spellings for the same control.
                    if canonical in {"", "auto", "default", "provider_default"}:
                        session.effort = self._effort_default(model)
                        session.effort_user_set = False
                    else:
                        if canonical not in model.selectable_effort_levels:
                            self.error(req_id, -32602, f"unsupported configurable effort for {model.name}: {value}")
                            return
                        session.effort = canonical
                        session.effort_user_set = True
                else:
                    self.error(req_id, -32602, f"unknown config option: {config_id}")
                    return
            self._persist_session(session)
            updated_options = self._config_options(session, config)
            self.result(req_id, {"configOptions": updated_options})
            notify_changed = True
        finally:
            # Every terminal response (success *or* failure) is fenced by the
            # released control lock. The queued ACP response is the client's
            # signal that the next model/effort operation is safe to send.
            session.turn_lock.release()
            terminal = self._take_prompt_response_buffer(req_id)
            if terminal is not None:
                self.send(terminal)
        if notify_changed:
            self._notify_config_update(session, config)

    def _handle_session_set_model_request(self, req_id: Any, params: dict[str, Any]) -> None:
        """Legacy ACP model picker: reject invalid or racing requests, never fake success."""
        session_id = str(params.get("sessionId") or "")
        session = self._get_session(session_id)
        if session is None:
            self.error(req_id, -32602, "unknown session")
            return
        requested = params.get("modelId")
        if not isinstance(requested, str) or not requested.strip():
            self.error(req_id, -32602, "modelId must be a non-empty string")
            return
        try:
            config = self._config()
        except ConfigError as exc:
            self.error(req_id, -32602, f"Kiro BYO configuration error: {exc}")
            return
        if not session.turn_lock.acquire(blocking=False):
            self.error(req_id, -32002, "session has an active turn; retry model selection after it completes")
            return
        self._begin_prompt_response_buffer(req_id)
        notify_changed = False
        try:
            with session.state_lock:
                if not self._set_model(session, config, requested):
                    self.error(req_id, -32602, f"unknown model: {requested}")
                    return
            self._persist_session(session)
            self.result(req_id, {})
            notify_changed = True
        finally:
            session.turn_lock.release()
            terminal = self._take_prompt_response_buffer(req_id)
            if terminal is not None:
                self.send(terminal)
        if notify_changed:
            self._notify_config_update(session, config)

    def _handle_session_set_mode_request(self, req_id: Any, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "")
        session = self._get_session(session_id)
        if session is None:
            self.error(req_id, -32602, "unknown session")
            return
        requested = str(params.get("modeId") or "").strip()
        current = self._current_mode_id(session)
        if not requested or requested != current:
            self.error(req_id, -32602, f"unknown mode: {requested or '<empty>'}")
            return
        self.result(req_id, {})

    def _handle_agent_list_request(self, req_id: Any, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "")
        session = self._get_session(session_id)
        if session is None:
            self.error(req_id, -32602, "unknown session")
            return
        plan_id = str(params.get("planId") or "")
        agents = [task.public_dict() for task in session.agent_registry.list(plan_id=plan_id)]
        self.result(req_id, {"agents": agents})

    def _handle_agent_send_request(self, req_id: Any, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "")
        task_id = str(params.get("taskId") or "")
        message = str(params.get("message") or "").strip()
        session = self._get_session(session_id)
        if session is None:
            self.error(req_id, -32602, "unknown session")
            return
        if not task_id or not message:
            self.error(req_id, -32602, "taskId and message are required")
            return
        task = session.agent_registry.send_message(task_id, message)
        if task is None:
            self.error(req_id, -32602, "unknown agent task")
            return
        self.runtime_trace.record(
            "agent.message.queued", session_id=session_id,
            data={"task_id": task_id, "status": task.status, "message_chars": len(message)},
        )
        self._project_agent_state(task, event="message_queued")
        self.result(req_id, {"agent": task.public_dict()})

    def _handle_agent_cancel_request(self, req_id: Any, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "")
        task_id = str(params.get("taskId") or "")
        session = self._get_session(session_id)
        if session is None:
            self.error(req_id, -32602, "unknown session")
            return
        runtime = getattr(session, "_kiro_agent_runtime", None)
        ok = bool(runtime.cancel(task_id)) if runtime is not None else session.agent_registry.cancel(task_id) is not None
        if not ok:
            self.error(req_id, -32602, "unknown agent task")
            return
        task = session.agent_registry.get(task_id)
        self.runtime_trace.record(
            "agent.cancel.requested", session_id=session_id,
            data={"task_id": task_id, "status": task.status if task is not None else "unknown"},
        )
        if task is not None:
            self._project_agent_state(task, event="cancel_requested")
        self.result(req_id, {"agent": task.public_dict() if task is not None else None})

    def _handle_agent_resume_request(self, req_id: Any, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "")
        task_id = str(params.get("taskId") or "")
        message = str(params.get("message") or "").strip()
        session = self._get_session(session_id)
        if session is None:
            self.error(req_id, -32602, "unknown session")
            return
        if session.active_turn:
            self.error(req_id, -32002, "session is busy; queue a message or wait for the parent turn")
            return
        if session.agent_registry.get(task_id) is None:
            self.error(req_id, -32602, "unknown agent task")
            return
        result = self._resume_agent_task(session, task_id, message=message)
        task = session.agent_registry.get(task_id)
        self.result(
            req_id,
            {
                "agent": task.public_dict() if task is not None else None,
                "result": result.public_dict(),
                # A running result is an accepted continuation, NOT completion.
                "inProgress": result.status == "running",
                "messageQueued": result.status == "running" and bool(message),
            },
        )

    def _handle_team_list_request(self, req_id: Any, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "")
        session = self._get_session(session_id)
        if session is None:
            self.error(req_id, -32602, "unknown session")
            return
        teams = [row.public_dict(include_messages=False) for row in session.team_registry.list(session_id=session_id)]
        self.result(req_id, {"teams": teams})

    def _handle_team_get_request(self, req_id: Any, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "")
        team_id = str(params.get("teamId") or "")
        session = self._get_session(session_id)
        if session is None:
            self.error(req_id, -32602, "unknown session")
            return
        team = session.team_registry.get(team_id)
        if team is None or team.session_id != session_id:
            self.error(req_id, -32602, "unknown agent team")
            return
        payload = team.public_dict()
        payload["agents"] = [
            task.public_dict() for task in session.agent_registry.list(plan_id=team.plan_id)
        ]
        self.result(req_id, {"team": payload})

    def _handle_team_send_request(self, req_id: Any, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "")
        team_id = str(params.get("teamId") or "")
        sender = str(params.get("senderTaskId") or "")
        recipient = str(params.get("recipientTaskId") or "")
        message = str(params.get("message") or "").strip()
        session = self._get_session(session_id)
        if session is None:
            self.error(req_id, -32602, "unknown session")
            return
        posted = session.team_registry.post_message(
            team_id, sender_task_id=sender, recipient_task_id=recipient, text=message
        )
        if posted is None:
            self.error(req_id, -32602, "invalid team message or member")
            return
        task = session.agent_registry.send_message(recipient, message)
        if task is None:
            self.error(req_id, -32602, "recipient task no longer exists")
            return
        self.runtime_trace.record(
            "agent.team.message", session_id=session_id,
            data={"team_id": team_id, "sender": sender, "recipient": recipient, "message_chars": len(message)},
        )
        self.result(req_id, {"message": posted.public_dict(), "agent": task.public_dict()})

    def _handle_team_recycle_request(self, req_id: Any, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "")
        team_id = str(params.get("teamId") or "")
        task_id = str(params.get("taskId") or "")
        session = self._get_session(session_id)
        if session is None:
            self.error(req_id, -32602, "unknown session")
            return
        team = session.team_registry.get(team_id)
        task = session.agent_registry.get(task_id)
        if team is None or task is None or task.plan_id != team.plan_id:
            self.error(req_id, -32602, "unknown team member")
            return
        if task.status == "running" or task.task_id == team.leader_task_id:
            self.error(req_id, -32011, "running or leader agents cannot be recycled")
            return
        if session.agent_registry.children(task_id):
            self.error(req_id, -32011, "agent still owns child agents; recycle descendants first")
            return
        workspace_id = task.workspace_id
        if not session.team_registry.remove_member(team_id, task_id):
            self.error(req_id, -32011, "agent cannot be recycled")
            return
        if not session.agent_registry.remove_task(task_id):
            # Preconditions above should make this unreachable, but keep the
            # team/registry boundary fail-closed rather than reporting success.
            session.team_registry.add_member(team_id, task_id)
            self.error(req_id, -32011, "agent registry refused recycle")
            return
        if workspace_id and bool(params.get("removeWorkspace", True)):
            try:
                self.workspace_manager.remove(workspace_id, force=bool(params.get("force", False)))
            except WorkspaceError:
                pass
        self.runtime_trace.record(
            "agent.team.recycled", session_id=session_id, data={"team_id": team_id, "task_id": task_id}
        )
        self.result(req_id, {"recycled": True, "taskId": task_id})

    def _handle_team_board_request(self, req_id: Any, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "")
        team_id = str(params.get("teamId") or "")
        session = self._get_session(session_id)
        if session is None:
            self.error(req_id, -32602, "unknown session")
            return
        team = session.team_registry.get(team_id)
        if team is None:
            self.error(req_id, -32602, "unknown agent team")
            return
        action = str(params.get("action") or "list").strip().lower()
        try:
            if action == "add":
                item = session.team_registry.add_board_item(
                    team_id, title=str(params.get("title") or ""),
                    owner_task_id=str(params.get("ownerTaskId") or ""),
                    depends_on=tuple(str(row) for row in (params.get("dependsOn") or []) if row),
                )
                if item is None:
                    raise ValueError("invalid board item")
            elif action == "update":
                item = session.team_registry.update_board_item(
                    team_id, str(params.get("itemId") or ""),
                    status=str(params.get("status") or ""),
                    owner_task_id=str(params.get("ownerTaskId")) if "ownerTaskId" in params else None,
                    detail=str(params.get("detail")) if "detail" in params else None,
                )
                if item is None:
                    raise ValueError("unknown board item")
            elif action != "list":
                raise ValueError("action must be list, add, or update")
        except ValueError as exc:
            self.error(req_id, -32602, str(exc))
            return
        self.result(req_id, {"board": [row.public_dict() for row in team.board.values()]})

    def _resolve_dynamic_child_model(self, session: Session, objective: str) -> tuple[Any, Any] | None:
        config = self._config()
        selected_type = self._type_for_selection(config, session.model, fallback=False)
        if selected_type is not None:
            decision = route_model(
                config, pack=config.mode_pack, policy=selected_type, prompt=objective,
                history=list(session.messages), has_image=False, tools_required=True,
                tools_available=session.native is not None, reasoning_effort="",
            )
            return (decision.provider, decision.model) if decision is not None else None
        return config.resolve_model(session.model)

    def _handle_team_spawn_request(self, req_id: Any, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "")
        team_id = str(params.get("teamId") or "")
        objective = str(params.get("objective") or "").strip()
        session = self._get_session(session_id)
        if session is None:
            self.error(req_id, -32602, "unknown session")
            return
        if session.active_turn:
            self.error(req_id, -32002, "session is busy; dynamic spawn is only admitted between parent turns")
            return
        team = session.team_registry.get(team_id)
        if team is None or team.session_id != session_id:
            self.error(req_id, -32602, "unknown agent team")
            return
        if not objective:
            self.error(req_id, -32602, "objective is required")
            return
        role = str(params.get("role") or "worker").strip().lower()
        if role not in {"worker", "writer", "specialist"}:
            self.error(req_id, -32602, "role must be worker, writer, or specialist")
            return
        backend = str(params.get("backend") or "model").strip().lower()
        if backend not in {"model", "acp"}:
            self.error(req_id, -32602, "backend must be model or acp")
            return
        if backend == "acp" and role == "writer":
            self.error(req_id, -32602, "external ACP workers are read-only at this trust boundary")
            return
        parent_task_id = str(params.get("parentTaskId") or team.leader_task_id)
        tool_policy = "isolated_write" if role == "writer" else "read_only"
        workspace_policy = "git_worktree" if role == "writer" else "shared_read"
        backend_id = str(params.get("agentId") or "").strip() if backend == "acp" else ""
        if backend == "acp" and self.acp_agent_catalog.get(backend_id) is None:
            self.error(req_id, -32602, "unknown or disabled external ACP agent")
            return
        spec = AgentSpec(
            role=role, name=str(params.get("name") or f"{role}-dynamic"), objective=objective,
            model_policy="external" if backend == "acp" else "dynamic",
            tier_pool=str(params.get("tierPool") or "any"), tool_policy=tool_policy,
            authority="isolated_writer" if role == "writer" else "assist",
            reasoning_intent=str(params.get("reasoningIntent") or "inherit"), max_turns=8,
            sticky_model=True, required=False, backend=backend, backend_id=backend_id,
            workspace_policy=workspace_policy,
        )
        try:
            task = session.agent_registry.spawn_task(
                plan_id=team.plan_id, session_id=session_id, spec=spec,
                parent_task_id=parent_task_id, max_agents=team.max_agents, max_depth=team.max_depth,
            )
        except ValueError as exc:
            self.error(req_id, -32011, str(exc))
            return
        if not session.team_registry.add_member(team_id, task.task_id):
            session.agent_registry.remove_task(task.task_id)
            self.error(req_id, -32011, "agent team has reached max_agents")
            return
        with session.state_lock:
            session.agent_registry.attach_context(task.task_id, list(session.messages))
        item = session.team_registry.add_board_item(team_id, title=objective, owner_task_id=task.task_id)
        if role == "writer":
            try:
                lease = self.workspace_manager.provision(cwd=session.cwd, task_id=task.task_id)
            except WorkspaceError as exc:
                session.agent_registry.transition(task.task_id, "failed", error=str(exc))
                self.error(req_id, -32012, f"isolated writer workspace failed: {exc}")
                return
            session.agent_registry.set_workspace(
                task.task_id, path=lease.path, kind=lease.kind, state="ready", workspace_id=lease.workspace_id
            )
        if backend == "acp":
            session.agent_registry.pin_model(task.task_id, "acp", backend_id)
        else:
            explicit_provider = str(params.get("providerId") or "")
            explicit_model = str(params.get("modelId") or "")
            resolved = None
            if explicit_model:
                config = self._config()
                if explicit_provider:
                    provider = next((row for row in config.providers if row.id == explicit_provider), None)
                    model = next((row for row in (provider.models if provider is not None else ()) if row.model_id == explicit_model), None)
                    resolved = (provider, model) if provider is not None and model is not None else None
                else:
                    resolved = config.resolve_model(explicit_model)
            else:
                try:
                    resolved = self._resolve_dynamic_child_model(session, objective)
                except ConfigError as exc:
                    session.agent_registry.transition(task.task_id, "failed", error=str(exc))
                    if task.workspace_id:
                        try:
                            self.workspace_manager.remove(task.workspace_id, force=True)
                        except WorkspaceError:
                            pass
                    if item is not None:
                        session.team_registry.update_board_item(team_id, item.item_id, status="blocked")
                    self.error(req_id, -32602, f"Kiro BYO configuration error: {exc}")
                    return
            if resolved is None:
                session.agent_registry.transition(task.task_id, "failed", error="no model is available for dynamic child")
                if task.workspace_id:
                    try:
                        self.workspace_manager.remove(task.workspace_id, force=True)
                    except WorkspaceError:
                        pass
                if item is not None:
                    session.team_registry.update_board_item(team_id, item.item_id, status="blocked")
                self.error(req_id, -32602, "no model is available for dynamic child")
                return
            session.agent_registry.pin_model(task.task_id, resolved[0].id, resolved[1].model_id)
        self._project_agent_state(task, event="spawned")
        if item is not None:
            session.team_registry.update_board_item(team_id, item.item_id, status="running")
        result = self._resume_agent_task(session, task.task_id)
        if item is not None:
            terminal = "done" if result.status == "completed" else "cancelled" if result.status == "cancelled" else "blocked"
            session.team_registry.update_board_item(team_id, item.item_id, status=terminal)
        self.runtime_trace.record(
            "agent.team.spawned", session_id=session_id,
            data={"team_id": team_id, "task_id": task.task_id, "role": role, "backend": backend, "status": result.status},
        )
        self.result(req_id, {"agent": task.public_dict(), "result": result.public_dict(), "team": team.public_dict(include_messages=False)})

    def _handle_workspace_list_request(self, req_id: Any, params: dict[str, Any]) -> None:
        del params
        self.result(req_id, {"workspaces": [row.public_dict() for row in self.workspace_manager.list()]})

    def _handle_workspace_get_request(self, req_id: Any, params: dict[str, Any]) -> None:
        lease = self.workspace_manager.get(str(params.get("workspaceId") or ""))
        if lease is None:
            self.error(req_id, -32602, "unknown workspace")
            return
        try:
            status = self.workspace_manager.status(lease.workspace_id)
        except WorkspaceError as exc:
            status = f"workspace error: {exc}"
        self.result(req_id, {"workspace": {**lease.public_dict(), "statusText": status}})

    def _handle_workspace_diff_request(self, req_id: Any, params: dict[str, Any]) -> None:
        workspace_id = str(params.get("workspaceId") or "")
        try:
            diff = self.workspace_manager.diff(workspace_id, binary=bool(params.get("binary", False)))
        except WorkspaceError as exc:
            self.error(req_id, -32012, str(exc))
            return
        self.result(req_id, {"workspaceId": workspace_id, "diff": diff})

    def _handle_workspace_check_request(self, req_id: Any, params: dict[str, Any]) -> None:
        workspace_id = str(params.get("workspaceId") or "")
        argv = params.get("argv")
        if not isinstance(argv, list):
            self.error(req_id, -32602, "argv must be an array")
            return
        try:
            result = self.workspace_manager.run_check(workspace_id, [str(row) for row in argv], timeout=float(params.get("timeout", 300.0) or 300.0))
        except (WorkspaceError, TypeError, ValueError) as exc:
            self.error(req_id, -32012, str(exc))
            return
        self.result(req_id, {"workspaceId": workspace_id, "check": result})

    def _handle_workspace_compare_request(self, req_id: Any, params: dict[str, Any]) -> None:
        raw = params.get("workspaceIds")
        if not isinstance(raw, list) or not raw:
            self.error(req_id, -32602, "workspaceIds must be a non-empty array")
            return
        rows = self.workspace_manager.compare([str(row) for row in raw][:8])
        self.result(req_id, {"candidates": rows})

    def _handle_workspace_apply_request(self, req_id: Any, params: dict[str, Any]) -> None:
        workspace_id = str(params.get("workspaceId") or "")
        if not workspace_id:
            self.error(req_id, -32602, "workspaceId is required")
            return
        try:
            result = self.workspace_manager.apply_candidate(workspace_id)
        except WorkspaceError as exc:
            self.error(req_id, -32012, str(exc))
            return
        self.runtime_trace.record(
            "agent.workspace.applied", session_id=str(params.get("sessionId") or ""),
            data={"workspace_id": workspace_id, "applied": bool(result.get("applied"))},
        )
        self.result(req_id, result)

    def _handle_workspace_remove_request(self, req_id: Any, params: dict[str, Any]) -> None:
        try:
            removed = self.workspace_manager.remove(
                str(params.get("workspaceId") or ""), force=bool(params.get("force", False))
            )
        except WorkspaceError as exc:
            self.error(req_id, -32012, str(exc))
            return
        self.result(req_id, {"removed": removed})

    def _handle_acp_agents_list_request(self, req_id: Any, params: dict[str, Any]) -> None:
        del params
        try:
            rows = [row.public_dict() for row in self.acp_agent_catalog.load()]
        except Exception as exc:
            self.error(req_id, -32602, f"ACP agent catalog error: {exc}")
            return
        self.result(req_id, {"agents": rows})

    def _handle_acp_conformance_request(self, req_id: Any, params: dict[str, Any]) -> None:
        del params
        self.result(req_id, conformance_report())

    @staticmethod
    def _prompt_blocks_from_user_message(message: dict[str, Any]) -> list[dict[str, Any]]:
        """Compatibility entry point for the shared ACP content codec."""
        return prompt_blocks_from_user_message(message)


    _handle_work_bind_request = handle_work_bind_request


    def handle(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if method is None:
            return
        method = str(method)
        req_id = msg.get("id")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

        if req_id is None:
            self._handle_notification(method, params)
            return
        if method == "session/prompt":
            self._handle_prompt_request(req_id, params)
            return
        if method in {"session/load", "session/resume"}:
            self._handle_session_restore_request(req_id, params, method=method)
            return

        handler_name = ACP_REQUEST_HANDLERS.get(method)
        if handler_name is None:
            self.error(req_id, -32601, f"method not found: {method}")
            return
        getattr(self, handler_name)(req_id, params)

    def _safe_handle(self, msg: dict[str, Any]) -> None:
        try:
            self.handle(msg)
        except SessionBusyError as exc:
            req_id = msg.get("id")
            if req_id is not None:
                self.error(req_id, -32002, str(exc))
        except Exception as exc:
            req_id = msg.get("id")
            self.trace(
                f"unhandled ACP error method={msg.get('method')!r} id={req_id!r}: "
                f"{type(exc).__name__}: {exc}"
            )
            if req_id is not None:
                try:
                    self.error(req_id, -32603, f"internal error: {type(exc).__name__}: {exc}")
                except Exception:
                    pass

    def run(self) -> int:
        """Run a full-duplex stdio JSON-RPC loop.

        The stdin reader NEVER executes a long prompt.  It only parses/routs
        frames, so responses to agent鈫抍lient permission requests, cancellation,
        and steering can arrive while a provider HTTP request is in flight.
        """
        # Start the dedicated outbound writer before processing any frame so
        # send() enqueues instead of blocking the caller on the stdout pipe.
        self._start_outbound_writer()
        try:
            for raw in self.stdin:
                if self._outbound_failed.is_set():
                    raise OutboundTransportError("ACP stdout transport failed") from self._outbound_failure
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue

                try:
                    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
                    self.runtime_trace.record(
                        "acp.inbound",
                        session_id=str(params.get("sessionId") or ""),
                        data={"method": msg.get("method", ""), "is_response": "method" not in msg},
                        content={"payload": msg},
                    )
                except OSError as exc:
                    self.trace(f"runtime trace write skipped: {type(exc).__name__}")

                # Responses to requests Kiro BYO issued to the Crew client must be
                # consumed by this one reader and handed to their prompt worker.
                if self._resolve_client_response(msg):
                    continue

                method = msg.get("method")
                # Cancel and steer are control-plane latency sensitive. They touch
                # only thread-safe events/queues, so handle them immediately rather
                # than letting a saturated worker pool delay them.
                if method == "session/cancel" and msg.get("id") is None:
                    self._safe_handle(msg)
                    continue
                if method == "_session/steer":
                    self._safe_handle(msg)
                    continue

                self._executor.submit(self._safe_handle, msg)
        finally:
            with self._sessions_lock:
                sessions = list(self.sessions.values())
            for session in sessions:
                self._signal_cancel(session, "server_shutdown")
            self._executor.shutdown(wait=True, cancel_futures=False)
            # Retiring sessions belong exclusively to their retirement worker.
            # Do not persist/close them again here: doing so can race deletion
            # or resurrect a session after a deferred session/delete.
            with self._sessions_lock:
                retirement_workers = list(self._retirement_workers)
            for worker in retirement_workers:
                worker.join(timeout=10.0)
            with self._sessions_lock:
                sessions = list(self.sessions.values())
                self.sessions.clear()
            for session in sessions:
                self._persist_session(session)
                self._shutdown_agent_runtime(session)
                if session.mcp is not None:
                    session.mcp.close()
                if session.native is not None:
                    session.native.close()
            # All terminal frames (including shutdown persistence results) have
            # now been enqueued. Signal the writer to flush the remaining queue
            # and stop, then wait for it so the process does not exit mid-frame.
            if self._writer_started:
                try:
                    self._outbound.put_nowait(None)
                except queue.Full:
                    # A stalled stdout must not block process shutdown.
                    pass
                self._writer_thread.join(timeout=10.0)
        return 0

def _force_utf8_std_streams() -> None:
    """ACP frames are UTF-8 on the wire. On Windows a child's stdout/stdin
    default to the system locale (e.g. GBK), so any non-ASCII byte breaks the
    parent's UTF-8 frame decode. Pin the real process streams to UTF-8 here, at
    the subprocess entry point, so BYO is reliable on non-UTF-8 hosts. Guarded
    so a stream that cannot be reconfigured (already wrapped, redirected) is left
    untouched rather than crashing startup."""
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


def run_acp() -> int:
    _force_utf8_std_streams()
    return AcpServer().run()
