"""ACP v1 server for the Kiro BYO backend.

0.1.0 targets KiroCrew 0.9's shared ``AcpRuntime`` contract.  The process is
multiplexed and durable: sessions can be created, evicted, reloaded and steered
without restarting the provider transport.  Native host tools expose the
Kiro-compatible read/write/glob/grep/shell surface (plus ``edit``) and every
executable call still returns through Crew's permission request before the local
sandbox/executor runs it.

Crew-projected agent identity/prompt arrives in ``_meta.kiro.customAgents``.
Kiro BYO consumes that projection and acknowledges the active template in the
ACP ``modes`` response so Crew can verify the session-specific active agent.
This acknowledgement is not a claim that Kiro BYO implements Crew's full
``member_saved_agent`` contract; that capability remains separately governed.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import sys
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

from .agent_projection import AgentProjection, from_session_params
from .mode_router import pack_label, pack_short_label, policy_label, route_model
from .policy_packs import get_policy_pack
from .config import ConfigError, KiroByoConfig, ModelSpec, ProviderSpec, home_dir, load_config
from .errors import ClientRequestError
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
from .reasoning_control import resolve_thinking_mode
from .turn_runner import TurnRunnerMixin
from .providers import ProviderError, ProviderInterrupted, chat_completion_turn

ECHO_MODELS: tuple[tuple[str, str], ...] = (
    (MODEL_ECHO, "Kiro BYO Echo"),
    (MODEL_UPPER, "Kiro BYO Echo (uppercase)"),
)


@dataclass
class Session:
    session_id: str
    cwd: str
    model: str
    effort: str = ""
    # Defaults advertised by a provider are not user intent. These flags let a
    # Type apply its reasoning policy unless the user explicitly changed the
    # corresponding control in this session.
    effort_user_set: bool = False
    thinking_user_set: bool = False
    # Deep-thinking on/off/auto is distinct from reasoning effort intensity.
    # Canonical values are "on", "off", "auto", or "" for provider default.
    thinking_mode: str = ""
    # Client-owned ACP v1 filesystem roots. They are intentionally re-supplied
    # on load/resume rather than restored implicitly from the session record.
    additional_directories: tuple[str, ...] = ()
    agent: AgentProjection = field(default_factory=AgentProjection)
    messages: list[dict[str, Any]] = field(default_factory=list)
    mcp: McpManager | None = None
    native: NativeToolRuntime | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # One upstream prompt per session, while other sessions remain concurrent.
    turn_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    state_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    steer_event: threading.Event = field(default_factory=threading.Event, repr=False)
    steer_queue: deque[str] = field(default_factory=deque, repr=False)
    active_turn: bool = False
    loaded_tool_names: list[str] = field(default_factory=list)
    # In-memory route pins for sticky execution forms (pack/type -> provider,
    # model). Long tasks stay on one model so caches and continuity survive.
    route_pins: dict[str, tuple[str, str]] = field(default_factory=dict, repr=False)


@dataclass
class PendingClientRequest:
    event: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None


class AcpServer(TurnRunnerMixin):
    def __init__(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self._ids = itertools.count(1)
        self._client_request_ids = itertools.count(1)
        self.sessions: dict[str, Session] = {}
        self._sessions_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending_client_requests: dict[str, PendingClientRequest] = {}
        self._executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="kiro-byo-acp")
        self.telemetry = TurnTelemetryStore()
        self.runtime_trace = RuntimeTraceStore()

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
        # Prompt workers and the stdin reader can both emit frames. JSON-RPC over
        # stdio is line framed, so writes must never interleave.
        with self._send_lock:
            self.stdout.write(line)
            self.stdout.flush()

    @staticmethod
    def trace(message: str) -> None:
        # ACP owns stdout for JSON-RPC. Timing diagnostics must stay on stderr.
        print(f"[kiro-byo timing] {message}", file=sys.stderr, flush=True)


    def result(self, req_id: Any, value: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "id": req_id, "result": value})

    def error(self, req_id: Any, code: int, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    @staticmethod
    def prompt_text(params: dict[str, Any]) -> str:
        blocks = params.get("prompt") or []
        out: list[str] = []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                out.append(block["text"])
        return "".join(out)

    @staticmethod
    def prompt_message(params: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Convert ACP v1 ContentBlocks to OpenAI-compatible user content.

        Text and ResourceLink are baseline ACP prompt content and must never be
        silently discarded. Images are translated to data URLs. Embedded Resource
        blocks are accepted best-effort (text and image resources); Kiro BYO does
        *not* advertise ``embeddedContext`` yet because arbitrary binary embedded
        resources cannot be faithfully represented by Chat Completions.
        """
        blocks = params.get("prompt") or []
        parts: list[dict[str, Any]] = []
        has_image = False

        def add_text(text: str) -> None:
            if text:
                parts.append({"type": "text", "text": text})

        def add_image(data: str, mime: str) -> None:
            nonlocal has_image
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{data}"},
            })
            has_image = True

        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                add_text(block["text"])
                continue
            if kind == "image" and isinstance(block.get("data"), str):
                add_image(block["data"], str(block.get("mimeType") or "image/png"))
                continue
            if kind == "resource_link":
                uri = str(block.get("uri") or "").strip()
                name = str(block.get("name") or block.get("title") or "resource").strip()
                description = str(block.get("description") or "").strip()
                if uri:
                    suffix = f" 鈥?{description}" if description else ""
                    add_text(f"[Referenced resource: {name}] {uri}{suffix}")
                continue
            if kind == "resource" and isinstance(block.get("resource"), dict):
                resource = block["resource"]
                uri = str(resource.get("uri") or "").strip()
                mime = str(resource.get("mimeType") or "").strip()
                if isinstance(resource.get("text"), str):
                    heading = f"[Embedded resource: {uri}]\n" if uri else "[Embedded resource]\n"
                    add_text(heading + resource["text"])
                elif isinstance(resource.get("blob"), str) and mime.lower().startswith("image/"):
                    if uri:
                        add_text(f"[Embedded image resource: {uri}]")
                    add_image(resource["blob"], mime or "image/png")
                else:
                    # Be observable rather than silently dropping an unsupported
                    # binary embedded resource. Clients should not send this while
                    # embeddedContext is unadvertised, but tolerant handling makes
                    # relay/version skew diagnosable.
                    label = uri or "unnamed resource"
                    add_text(f"[Unsupported embedded binary resource: {label}{' (' + mime + ')' if mime else ''}]")

        if not has_image:
            text = "".join(str(part.get("text") or "") for part in parts if part.get("type") == "text")
            return {"role": "user", "content": text}, False
        return {"role": "user", "content": parts}, True

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

    @classmethod
    def models_payload(cls, current: str, config: KiroByoConfig) -> dict[str, Any]:
        # KiroCrew's picker is a shared execution selector.  Kiro BYO exposes
        # the active Mode's Types first, followed by real provider models.
        # Types are execution policies, not fake provider models; the wire ids
        # are private sentinels used only to carry the selected Type per session.
        available = []
        for value, name, model in cls._available_model_entries(config):
            selected_type = parse_type_selection(value)
            if selected_type is not None:
                pack = get_policy_pack(selected_type.mode_id)
                policy = pack.policy(selected_type.type_id) if pack is not None else None
                available.append({
                    "modelId": value,
                    "name": name,
                    "description": policy.short_description if policy is not None else policy_label(selected_type.type_id, selected_type.mode_id),
                })
            else:
                available.append({
                    "modelId": value,
                    "name": name,
                    "description": cls._model_description(model) if model is not None else name,
                })
        return {"availableModels": available, "currentModelId": current}

    @classmethod
    def _available_model_entries(
        cls, config: KiroByoConfig
    ) -> list[tuple[str, str, ModelSpec | None]]:
        if config.models:
            pack = get_policy_pack(config.mode_pack)
            if pack is None:
                raise ConfigError(f"mode.id {config.mode_pack!r} is not installed")
            types = [
                (type_selection_id(pack.id, policy.id), policy.display_name, None)
                for policy in pack.policies
            ]
            concrete = [
                (public_id, model.name, model) for public_id, model in config.public_models
            ]
            return [*types, *concrete]
        return [(value, name, None) for value, name in ECHO_MODELS]

    @classmethod
    def available_models(cls, config: KiroByoConfig) -> list[tuple[str, str]]:
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
    def model_config(cls, current: str, config: KiroByoConfig) -> dict[str, Any]:
        return {
            "id": "model",
            "name": "Model",
            "category": "model",
            "type": "select",
            "currentValue": current,
            "options": [
                {"value": value, "name": name}
                for value, name in cls.available_models(config)
            ],
        }

    @staticmethod
    def _thinking_choices(model: ModelSpec | None) -> tuple[str, ...]:
        """Deep-thinking modes that have an exact, safe upstream mapping.

        Thinking mode and thinking *effort* are independent capabilities.  In
        particular, an adaptive-only provider may legitimately expose
        ``off/auto`` without a forced ``on`` mode, while an always-on model may
        expose effort levels but no thinking selector at all.  Do not synthesize
        a universal on/off/auto trio for UI symmetry.
        """
        if model is None or model.reasoning_supported is not True or model.reasoning_can_disable is not True:
            return ()

        choices: list[str] = []
        # ``on`` is only meaningful if the adapter can request it explicitly, an
        # effort level itself enables reasoning, or omission is a verified
        # default-on contract and there is no distinct adaptive/auto wire mode.
        can_on = bool(
            model.reasoning_enable_value
            or model.effort_supported_levels
            or (model.reasoning_default_enabled is True and not model.reasoning_auto_value)
        )
        if can_on:
            choices.append("on")
        if model.reasoning_disable_value:
            choices.append("off")
        if model.reasoning_supports_auto is True and model.reasoning_auto_value:
            choices.append("auto")

        # A one-option selector is not a control. Fixed modes are shown as model
        # capability text rather than a meaningless dropdown.
        return tuple(choices) if len(choices) >= 2 else ()

    @classmethod
    def _thinking_default(cls, model: ModelSpec | None) -> str:
        if model is None or model.reasoning_supported is not True:
            return ""
        if model.reasoning_can_disable is not True:
            return "on"
        choices = cls._thinking_choices(model)
        if not choices:
            return ""
        if model.reasoning_default_enabled is False and "off" in choices:
            return "off"
        if model.reasoning_default_enabled is True:
            if "on" in choices:
                return "on"
            # Some APIs represent the only enabled state as an explicit adaptive
            # mode.  In that case Auto is the faithful default-on control.
            if "auto" in choices:
                return "auto"
        # Unknown remains provider default; never invent a mode.
        return ""

    @classmethod
    def _thinking_config(cls, model: ModelSpec | None, current: str) -> dict[str, Any] | None:
        choices = cls._thinking_choices(model)
        if not choices:
            return None
        names = {"on": "开", "off": "关", "auto": "自动"}
        return {
            "id": THINKING_CONFIG_ID,
            "name": "Deep thinking",
            "category": "thought_mode",
            "type": "select",
            "currentValue": current if current in choices else "",
            "options": [{"value": value, "name": names[value]} for value in choices],
        }

    @classmethod
    def _reasoning_wire_value(cls, model: ModelSpec | None, thinking_mode: str, effort: str) -> str:
        """Map independent thinking + effort state to this adapter's wire value.

        Current OpenAI-compatible transports expose a single reasoning_effort
        field. Models whose on/off switch uses another protocol must not declare
        a switch until that provider adapter knows how to map it.
        """
        if model is None:
            return ""
        # The BYO App owns the user-facing Auto/On/Off control. Wire mapping
        # must use raw model capability metadata, not Crew selector choices:
        # a provider can have a perfectly valid disable value even when Crew
        # intentionally renders no thinking selector.
        if thinking_mode == "off":
            if model.reasoning_can_disable is True and model.reasoning_disable_value:
                return model.reasoning_disable_value
            return ""
        if thinking_mode == "auto":
            # ``auto`` should normally have been resolved before transport. Keep
            # this only for non-Type internal compatibility paths.
            return model.reasoning_auto_value if model.reasoning_auto_value else ""
        if effort and effort in model.effort_supported_levels:
            return effort
        if thinking_mode == "on" and model.reasoning_enable_value:
            return model.reasoning_enable_value
        return cls._effort_default(model)

    @classmethod
    def _normalize_session_reasoning(cls, session: Session, model: ModelSpec | None) -> None:
        if model is None:
            session.effort = ""
            session.thinking_mode = ""
            session.effort_user_set = False
            session.thinking_user_set = False
            return
        # Keep explicit user intent even when a newly routed model cannot express
        # that exact vocabulary; the wire mapper will safely fall back. Provider
        # defaults, on the other hand, may be replaced by the new model default.
        if not session.effort_user_set and session.effort not in model.effort_supported_levels:
            session.effort = cls._effort_default(model)
        choices = cls._thinking_choices(model)
        if not session.thinking_user_set:
            if choices:
                if session.thinking_mode not in choices:
                    session.thinking_mode = cls._thinking_default(model)
            else:
                session.thinking_mode = cls._thinking_default(model)

    @staticmethod
    def _effort_default(model: ModelSpec | None) -> str:
        if model is None:
            return ""
        if model.reasoning_mode == "fixed" and model.reasoning_fixed_level:
            # Fixed reasoning is not user-selectable, but an upstream that
            # explicitly advertised its sole accepted value may still require
            # that transport parameter.
            if model.reasoning_fixed_level in model.effort_supported_levels:
                return model.reasoning_fixed_level
            return ""
        if not model.effort_supported_levels:
            return ""
        if model.effort_default_level in model.effort_supported_levels:
            return model.effort_default_level
        # An empty current value means "let the provider use its own default".
        return ""

    @staticmethod
    def _effort_config(model: ModelSpec | None, current: str) -> dict[str, Any] | None:
        if model is None or not model.selectable_effort_levels:
            return None
        def display_name(level: str) -> str:
            if level.strip().lower() in {"none", "off", "disabled"}:
                return "Off"
            return level

        options = [
            {"value": level, "name": display_name(level)}
            for level in model.selectable_effort_levels
        ]
        current_value = current if current in model.selectable_effort_levels else ""
        # ACP select options do not require a synthetic "default" row. An empty
        # current value means the provider default is in effect; once the user
        # chooses a level Crew persists and writes the advertised vocabulary.
        return {
            "id": EFFORT_CONFIG_ID,
            "name": "Reasoning effort",
            "category": "thought_level",
            "type": "select",
            "currentValue": current_value,
            "options": options,
        }

    @classmethod
    def _config_options(
        cls,
        session: Session,
        config: KiroByoConfig,
    ) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = [cls.model_config(session.model, config)]
        resolved = cls._resolved_model(config, session)
        model = resolved[1] if resolved is not None else None
        # Deep-thinking Auto/On/Off is a persistent per-model setting managed
        # in the BYO App. Crew's right-side control is reserved for reasoning
        # effort/intensity, which is a separate capability.
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
        mode_text = f"\n{mode_context}\n" if mode_context else ""
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
                + "Only tools present in the supplied tool list exist; never invent "
                "execute_bash/DSML markup or unsupported host functions."
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
                    "models": self.models_payload(session.model, config),
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
            has_image=False, tools_expected=session.agent.unrestricted or bool(session.agent.tools),
            reasoning_effort=session.effort if session.effort_user_set else "",
        )
        return (decision.provider, decision.model) if decision is not None else None

    def _set_model(self, session: Session, config: KiroByoConfig, requested: str) -> bool:
        requested = str(requested or "").strip()
        requested_type = self._type_for_selection(config, requested, fallback=False)
        if requested_type is not None:
            if not config.models:
                return False
            session.model = type_selection_id(config.mode_pack, requested_type)
            resolved = self._resolved_model(config, session)
            model = resolved[1] if resolved is not None else None
            self._normalize_session_reasoning(session, model)
            return True
        resolved = config.resolve_model(requested)
        if resolved is None:
            return False
        _provider, model = resolved
        session.model = config.public_model_id(model)
        self._normalize_session_reasoning(session, model)
        return True

    @staticmethod
    def _compact_context_suffix(text: str) -> str:
        stripped = text.strip()
        if not stripped.startswith("/compact"):
            return ""
        return stripped[len("/compact") :].strip()

    def _compact_session(
        self,
        session: Session,
        provider: ProviderSpec,
        model: ModelSpec,
        preserve_context: str,
    ) -> str:
        """Summarize and replace this session's model transcript.

        The operation is provider-native but tool-free.  It observes the same
        cancel event as an ordinary turn so ``session/cancel`` can abort an
        in-flight manual compaction instead of waiting for the upstream HTTP
        stream to finish.  The returned summary is useful only for lifecycle
        diagnostics; the durable source of truth is ``session.messages``.
        """
        if not session.messages:
            return ""

        transcript_parts: list[str] = []
        for message in session.messages:
            role = str(message.get("role") or "unknown")
            raw_content = message.get("content")
            if isinstance(raw_content, list):
                chunks: list[str] = []
                for block in raw_content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        chunks.append(str(block.get("text") or ""))
                    elif isinstance(block, dict) and block.get("type") == "image_url":
                        chunks.append("[image attached]")
                content = " ".join(chunks)
            else:
                content = str(raw_content or "")
            transcript_parts.append(f"[{role}]\n{content}")
        transcript = "\n\n".join(transcript_parts)

        instruction = (
            "Summarize the conversation for future continuation. Preserve concrete facts, "
            "user preferences, decisions, unresolved tasks, important code/file names, and "
            "constraints. Remove repetition and conversational filler. Do not invent facts. "
            "Return only the compacted summary, with enough detail that another instance of "
            "the same model can continue the work."
        )
        if preserve_context:
            keep = preserve_context[:4000]
            instruction += f"\nThe host additionally asked that this context be preserved:\n{keep}"

        compact_messages = [
            {"role": "system", "content": instruction},
            {"role": "user", "content": transcript},
        ]
        compact_thinking = resolve_thinking_mode(model, getattr(model, "thinking_preference", "auto"), "")
        compact_effort = "" if compact_thinking == "off" else session.effort
        turn = chat_completion_turn(
            provider,
            model.model_id,
            compact_messages,
            lambda _chunk: None,
            reasoning_effort=self._reasoning_wire_value(model, compact_thinking, compact_effort) or None,
            thinking_mode=compact_thinking,
            model_spec=model,
            cache_key=session.session_id + ":compact",
            tools=None,
            should_interrupt=session.cancel_event.is_set,
        )
        summary = turn.text.strip()
        if not summary:
            raise ProviderError("compaction returned an empty summary")

        with session.state_lock:
            session.messages = [
                {
                    "role": "system",
                    "content": "Compacted conversation summary:\n" + summary,
                }
            ]
            session.updated_at = time.time()
        return summary

    def _emit_summarization_info(
        self,
        session: Session,
        kind: str,
        *,
        summary: str = "",
        detail: str = "",
    ) -> None:
        """Emit the CLI V3 replacement for legacy compaction-status frames."""
        kiro: dict[str, Any] = {"kind": kind}
        if summary:
            # Keep lifecycle updates bounded. The full durable summary remains in
            # the session store and is replayed through normal session history.
            kiro["summary"] = summary[:4000]
        if detail:
            kiro["detail"] = detail[:2000]
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

    def _run_manual_compaction(
        self,
        session: Session,
        *,
        preserve_context: str = "",
    ) -> bool:
        """Run one manual compaction while owning the session turn lock.

        Current Kiro V3's typed extension reports ``success: false`` when a
        session is already busy/compacting. Unknown sessions are rejected by the
        RPC handler before reaching here.
        """
        if not session.turn_lock.acquire(blocking=False):
            return False
        try:
            session.cancel_event.clear()
            self._emit_summarization_info(session, "summarization_started")
            try:
                config = self._config()
                resolved = self._resolved_model(config, session)
                if resolved is None:
                    raise ProviderError(f"model {session.model!r} is no longer configured")
                provider, model = resolved
                summary = self._compact_session(
                    session, provider, model, preserve_context
                )
                self._persist_session(session)
                self._emit_summarization_info(
                    session, "summarization_completed", summary=summary
                )
                return True
            except ProviderInterrupted:
                self._emit_summarization_info(
                    session, "summarization_failed", detail="cancelled"
                )
                return False
            except (ConfigError, ProviderError) as exc:
                self._emit_summarization_info(
                    session, "summarization_failed", detail=str(exc)
                )
                return False
        finally:
            session.turn_lock.release()

    # 鈹€鈹€ Full-duplex control plane / durable sessions 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    @staticmethod
    def _session_store_dir() -> Path:
        return home_dir() / "data" / "sessions" / "kiro-byo"

    @staticmethod
    def _safe_session_id(session_id: str) -> str:
        value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(session_id or "")).strip("._")
        return value or "invalid"

    @classmethod
    def _session_path(cls, session_id: str) -> Path:
        return cls._session_store_dir() / f"{cls._safe_session_id(session_id)}.json"

    def _persist_session(self, session: Session) -> None:
        """Atomically persist provider-neutral conversation state.

        Kiro BYO owns this record rather than asking Crew to serialize provider
        traffic.  That makes ``session/load`` work after the adapter process has
        been restarted, which is the contract behind resumable shared subagents.
        """
        try:
            store = self._session_store_dir()
            store.mkdir(parents=True, exist_ok=True)
            with session.state_lock:
                session.updated_at = time.time()
                payload = {
                    "schema_version": 1,
                    "session_id": session.session_id,
                    "cwd": session.cwd,
                    # Persisted only for diagnostics/history. ACP v1 requires
                    # the client to re-supply these roots on load/resume and the
                    # restore path never activates this saved value implicitly.
                    "last_additional_directories": list(session.additional_directories),
                    "model": session.model,
                    "effort": session.effort,
                    "thinking_mode": session.thinking_mode,
                    "effort_user_set": bool(session.effort_user_set),
                    "thinking_user_set": bool(session.thinking_user_set),
                    "agent": {
                        "id": session.agent.agent_id,
                        "prompt": session.agent.prompt,
                        "tools": list(session.agent.tools),
                        "tools_declared": session.agent.tools_declared,
                        "excludedTools": list(session.agent.excluded_tools),
                        # Authorization grants are deliberately NOT persisted.
                        # session/load must receive a fresh Crew projection; a
                        # missing projection falls back to prompting, never to a
                        # stale auto-approve grant.
                    },
                    "messages": session.messages,
                    "loaded_tool_names": list(session.loaded_tool_names),
                    "created_at": session.created_at,
                    "updated_at": session.updated_at,
                }
            path = self._session_path(session.session_id)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError as exc:
            # A chat should not be destroyed merely because durable continuation
            # storage is unavailable.  The trace is intentionally loud: a host that
            # wants session sharing can detect this before advertising it broadly.
            self.trace(f"session persistence failed session={session.session_id}: {exc}")

    def _load_persisted_record(self, session_id: str) -> dict[str, Any] | None:
        path = self._session_path(session_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or int(payload.get("schema_version") or 0) != 1:
            return None
        if str(payload.get("session_id") or "") != session_id:
            return None
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return None
        return payload

    @classmethod
    def _persisted_session_records(cls) -> list[dict[str, Any]]:
        store = cls._session_store_dir()
        if not store.is_dir():
            return []
        records: list[dict[str, Any]] = []
        for path in store.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or int(payload.get("schema_version") or 0) != 1:
                continue
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                continue
            records.append(payload)
        return records

    @staticmethod
    def _session_info(record: dict[str, Any]) -> dict[str, Any] | None:
        session_id = str(record.get("session_id") or "").strip()
        cwd = str(record.get("cwd") or "").strip()
        if not session_id or not cwd or not Path(cwd).is_absolute():
            # Pre-0.9.10 records may not persist cwd. Do not fabricate an invalid ACP
            # SessionInfo for them; they remain directly loadable by id.
            return None
        updated = float(record.get("updated_at") or 0.0)
        result: dict[str, Any] = {"sessionId": session_id, "cwd": cwd}
        if updated > 0:
            result["updatedAt"] = datetime.fromtimestamp(updated, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        return result

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
        record = self._load_persisted_record(session_id)
        if record is None:
            raise KeyError(session_id)
        config = self._config()
        fallback_selection = type_selection_id(config.mode_pack, config.mode_type) if config.models else self.default_model(config)
        model_id = str(record.get("model") or "").strip() or fallback_selection
        effort = str(record.get("effort") or "")
        effort_user_set = bool(record.get("effort_user_set", False))
        selected_type = self._type_for_selection(config, model_id) if config.models else None
        if selected_type is not None and config.models:
            # A persisted Type from a previous Mode may no longer exist. Switching
            # Mode intentionally resets such stale selections to the new Mode's
            # declared default Type rather than treating it as a provider model.
            model_id = type_selection_id(config.mode_pack, selected_type)
            history = record.get("messages") if isinstance(record.get("messages"), list) else []
            decision = route_model(
                config, pack=config.mode_pack, policy=selected_type, prompt="", history=history,
                has_image=False, tools_expected=True, reasoning_effort=effort if effort_user_set else "",
            )
            resolved = (decision.provider, decision.model) if decision is not None else None
        elif model_id in {MODEL_ECHO, MODEL_UPPER} and not config.models:
            resolved = None
        else:
            resolved = config.resolve_model(model_id)
            if resolved is None:
                model_id = fallback_selection
                if config.models:
                    decision = route_model(
                        config, pack=config.mode_pack, policy=config.mode_type, prompt="", history=[],
                        has_image=False, tools_expected=True, reasoning_effort=effort if effort_user_set else "",
                    )
                    resolved = (decision.provider, decision.model) if decision is not None else None
                else:
                    resolved = None
        thinking_mode = str(record.get("thinking_mode") or "")
        thinking_user_set = bool(record.get("thinking_user_set", False))
        if resolved is not None and not effort_user_set and effort not in resolved[1].effort_supported_levels:
            effort = self._effort_default(resolved[1])
        persisted_cwd = str(record.get("cwd") or "").strip()
        cwd, additional_directories = self._session_paths_from_params(
            params,
            expected_cwd=persisted_cwd,
        )
        mcp = self._mcp_from_params(params)
        native = self._native_from_params(cwd, additional_directories)
        agent = self._custom_agent_from_params(params)
        if not agent.active:
            saved_agent = record.get("agent")
            if isinstance(saved_agent, dict):
                agent = AgentProjection(
                    agent_id=str(saved_agent.get("id") or "").strip(),
                    prompt=str(saved_agent.get("prompt") or ""),
                    tools=tuple(str(x) for x in (saved_agent.get("tools") or []) if isinstance(x, str)),
                    tools_declared=bool(saved_agent.get("tools_declared")),
                    excluded_tools=tuple(
                        str(x) for x in (saved_agent.get("excludedTools") or []) if isinstance(x, str)
                    ),
                )
            else:
                legacy_id = str(record.get("agent_id") or "").strip()
                if legacy_id:
                    agent = AgentProjection(agent_id=legacy_id, prompt=str(record.get("agent_prompt") or ""))
        session = Session(
            session_id=session_id,
            cwd=cwd,
            model=model_id,
            additional_directories=additional_directories,
            effort=effort,
            thinking_mode=thinking_mode,
            effort_user_set=effort_user_set,
            thinking_user_set=thinking_user_set,
            agent=agent,
            messages=[m for m in record.get("messages", []) if isinstance(m, dict)],
            mcp=mcp,
            native=native,
            created_at=float(record.get("created_at") or time.time()),
            loaded_tool_names=[
                str(name) for name in (record.get("loaded_tool_names") or [])
                if isinstance(name, str) and name
            ][-DEFERRED_TOOL_ACTIVE_LIMIT:],
            updated_at=float(record.get("updated_at") or time.time()),
        )
        self._normalize_session_reasoning(session, resolved[1] if resolved is not None else None)
        self._put_session(session)
        if replay:
            self._replay_session_history(session)
        return session, config

    def _delete_persisted_session(self, session_id: str) -> None:
        try:
            self._session_path(session_id).unlink(missing_ok=True)
        except OSError as exc:
            self.trace(f"session delete failed session={session_id}: {exc}")

    def _get_session(self, session_id: str) -> Session | None:
        with self._sessions_lock:
            return self.sessions.get(session_id)

    def _put_session(self, session: Session) -> None:
        with self._sessions_lock:
            old = self.sessions.get(session.session_id)
            self.sessions[session.session_id] = session
        if old is not None and old is not session:
            if old.mcp is not None:
                old.mcp.close()
            if old.native is not None:
                old.native.close()

    def _pop_session(self, session_id: str) -> Session | None:
        with self._sessions_lock:
            return self.sessions.pop(session_id, None)

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
        return {
            "sessionId": session.session_id,
            "models": self.models_payload(session.model, config),
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
        self.send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        try:
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
    def _tool_identity(session: Session, public_name: str) -> tuple[str, str]:
        if session.mcp is None:
            return "", ""
        binding = session.mcp.bindings.get(public_name)
        if binding is None:
            return "", ""
        return binding.server_name, binding.tool_name

    def _tool_call_payload(
        self,
        session: Session,
        call_id: str,
        public_name: str,
        arguments: dict[str, Any],
        *,
        status: str | None = None,
    ) -> dict[str, Any]:
        server_name, tool_name = self._tool_identity(session, public_name)
        is_native = bool(session.native is not None and session.native.has_tool(public_name))
        payload: dict[str, Any] = {
            "toolCallId": call_id,
            "title": public_name,
            # Native tools use canonical ACP kinds so Crew PreToolUse / governance
            # sees the real operation. MCP tools keep trusted identity metadata.
            "kind": session.native.kind_for(public_name) if is_native and session.native else "other",
            "rawInput": arguments,
        }
        if status:
            payload["status"] = status
        kiro_meta: dict[str, Any] = {}
        if is_native and session.native is not None:
            kiro_meta["toolId"] = session.native.canonical_name(public_name)
        if server_name and tool_name:
            # Keep the legacy flat identity for KiroCrew 0.9.0-insider.1 while
            # also emitting Kiro V3's versioned MCP identity envelope. Hints are
            # display metadata only; neither Kiro BYO nor Crew may authorize from
            # them alone.
            kiro_meta.update(
                {
                    "mcpServerName": server_name,
                    "toolName": tool_name,
                    "isMcpTool": True,
                }
            )
            binding = session.mcp.bindings.get(public_name) if session.mcp is not None else None
            mcp_tool: dict[str, Any] = {
                "version": 1,
                "identity": {"serverName": server_name, "toolName": tool_name},
            }
            if binding is not None and binding.annotations:
                mcp_tool["annotations"] = dict(binding.annotations)
            kiro_meta["mcpTool"] = mcp_tool
        if kiro_meta:
            payload["_meta"] = {"kiro": kiro_meta}
        return payload

    def _emit_tool_call(
        self,
        session: Session,
        call_id: str,
        public_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        tool_call = self._tool_call_payload(
            session, call_id, public_name, arguments, status="pending"
        )
        self.notify(
            "session/update",
            {
                "sessionId": session.session_id,
                "update": {"sessionUpdate": "tool_call", **tool_call},
            },
        )
        return tool_call

    def _emit_tool_status(
        self,
        session: Session,
        call_id: str,
        *,
        status: str,
        text: str = "",
    ) -> None:
        update: dict[str, Any] = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": call_id,
            "status": status,
        }
        if text:
            update["content"] = [{"type": "text", "text": text}]
        self.notify("session/update", {"sessionId": session.session_id, "update": update})

    def _permission_consent_meta(
        self,
        session: Session,
        public_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Build Kiro V3 consent context without making it an auth decision."""
        server_name, tool_name = self._tool_identity(session, public_name)
        if server_name and tool_name:
            capability = "mcp"
            resource = f"{server_name}/{tool_name}"
        elif session.native is not None and session.native.has_tool(public_name):
            capability = session.native.capability_for(public_name)
            canonical = session.native.canonical_name(public_name)
            if canonical == "shell":
                resource = str(arguments.get("command") or canonical)
            elif canonical == "web_fetch":
                resource = str(arguments.get("url") or canonical)
            elif canonical == "web_search":
                resource = str(arguments.get("query") or canonical)
            elif canonical == "read_files":
                resource = ", ".join(str(x) for x in arguments.get("paths", [])[:5]) or canonical
            else:
                resource = str(arguments.get("path") or canonical)
        else:
            capability = "all"
            resource = public_name
        return {
            "capability": capability,
            "resource": resource,
            "triggeringResource": resource,
            "workspaceRoot": session.cwd,
            "persistableConsent": True,
        }

    def _request_tool_permission_choice(
        self,
        session: Session,
        call_id: str,
        public_name: str,
        arguments: dict[str, Any],
    ) -> str:
        tool_call = self._emit_tool_call(session, call_id, public_name, arguments)
        kiro_meta: dict[str, Any] = {
            "consent": self._permission_consent_meta(session, public_name, arguments)
        }
        server_name, tool_name = self._tool_identity(session, public_name)
        if server_name and tool_name:
            binding = session.mcp.bindings.get(public_name) if session.mcp is not None else None
            mcp_tool: dict[str, Any] = {
                "version": 1,
                "identity": {"serverName": server_name, "toolName": tool_name},
            }
            if binding is not None and binding.annotations:
                mcp_tool["annotations"] = dict(binding.annotations)
            # Kiro V3 places MCP identity on the permission request metadata.
            # Keep the copy in toolCall._meta too for Crew 0.9.0-insider.1.
            kiro_meta["mcpTool"] = mcp_tool
        result = self._request_client(
            "session/request_permission",
            {
                "sessionId": session.session_id,
                "toolCall": tool_call,
                "options": [
                    {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "allow_always", "name": "Always allow", "kind": "allow_always"},
                    {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
                    {"optionId": "reject_always", "name": "Always reject", "kind": "reject_always"},
                ],
                "_meta": {"kiro": kiro_meta},
            },
            cancel_event=session.cancel_event,
            should_interrupt=lambda: self._interrupt_requested(session),
        )
        outcome = result.get("outcome")
        if not isinstance(outcome, dict):
            return ""
        if outcome.get("outcome") != "selected":
            return ""
        selected = str(outcome.get("optionId") or "")
        if selected in {"allow_once", "allow_always", "allow"}:
            return selected
        return ""

    def _request_tool_permission(
        self,
        session: Session,
        call_id: str,
        public_name: str,
        arguments: dict[str, Any],
    ) -> bool:
        return bool(
            self._request_tool_permission_choice(
                session, call_id, public_name, arguments
            )
        )

    @staticmethod
    def _unwrap_steer_message(message: str) -> str:
        text = str(message or "").strip()
        prefix = "<user_message>"
        suffix = "</user_message>"
        if text.startswith(prefix) and text.endswith(suffix):
            text = text[len(prefix) : -len(suffix)].strip()
        return text

    def _emit_steer_info(self, session: Session, kind: str, text: str = "") -> None:
        kiro: dict[str, Any] = {"kind": kind}
        if text:
            kiro["content"] = text
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

    def _queue_steer(self, session: Session, message: str) -> bool:
        text = self._unwrap_steer_message(message)
        if not text:
            return False
        with session.state_lock:
            if not session.active_turn:
                return False
            session.steer_queue.append(text)
            session.steer_event.set()
        self._emit_steer_info(session, "steering_queued", text)
        return True

    def _consume_steers(self, session: Session) -> list[str]:
        with session.state_lock:
            items = list(session.steer_queue)
            session.steer_queue.clear()
            session.steer_event.clear()
        return items

    def _clear_steers(self, session: Session, *, notify: bool = True) -> bool:
        """Drop queued steering and publish the V3 lifecycle event.

        Cancellation/termination ends the current steering ledger. Keeping stale
        text in the queue can otherwise inject an old user correction after a
        resumed turn. The notification is intentionally emitted only when there
        was something to clear.
        """
        with session.state_lock:
            had_pending = bool(session.steer_queue) or session.steer_event.is_set()
            session.steer_queue.clear()
            session.steer_event.clear()
        if had_pending and notify:
            self._emit_steer_info(session, "steering_cleared")
        return had_pending

    def _emit_steer_consumed(self, session: Session, text: str) -> None:
        # Current Kiro CLI V3 represents an injected steer as a session-info
        # discriminant. Keep it session-scoped on the shared runtime so another
        # tenant can never settle this session's pending steer ledger.
        self._emit_steer_info(session, "steering_injected", text)

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
            session.cancel_event.set()
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
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        kiro_meta = meta.get("kiro") if isinstance(meta.get("kiro"), dict) else {}
        requested_model = str(kiro_meta.get("modelId") or "").strip()
        if requested_model:
            requested_type = self._type_for_selection(config, requested_model, fallback=False)
            if requested_type is not None:
                selected_model = type_selection_id(config.mode_pack, requested_type)
            elif config.resolve_model(requested_model) is not None:
                selected_model = requested_model
        selected_type = self._type_for_selection(config, selected_model) if config.models else None
        if config.models and selected_type is not None:
            selected_model = type_selection_id(config.mode_pack, selected_type)
            decision = route_model(
                config, pack=config.mode_pack, policy=selected_type, prompt="", history=[],
                has_image=False, tools_expected=True, reasoning_effort="",
            )
            resolved = (decision.provider, decision.model) if decision is not None else None
        else:
            resolved = config.resolve_model(selected_model) if config.models else None
        effective_model = resolved[1] if resolved is not None else None
        initial_effort = self._effort_default(effective_model)
        initial_thinking = self._thinking_default(effective_model)
        requested_effort = str(kiro_meta.get("effortLevel") or "").strip()
        effort_user_set = False
        if effective_model is not None and requested_effort in effective_model.selectable_effort_levels:
            initial_effort = requested_effort
            effort_user_set = True
        requested_thinking = str(kiro_meta.get("thinkingMode") or "").strip().lower()
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
            mcp = self._mcp_from_params(params)
        except Exception as exc:
            self.error(req_id, -32603, f"failed to start session MCP servers: {exc}")
            return
        t_mcp = time.perf_counter()
        native = self._native_from_params(cwd, additional_directories)
        agent = self._custom_agent_from_params(params)
        session = Session(
            session_id=session_id,
            cwd=cwd,
            model=selected_model,
            effort=initial_effort,
            thinking_mode=initial_thinking,
            effort_user_set=effort_user_set,
            thinking_user_set=thinking_user_set,
            additional_directories=additional_directories,
            agent=agent,
            mcp=mcp,
            native=native,
        )
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
        queued = self._queue_steer(session, message)
        result: dict[str, Any] = {"queued": queued, "messageId": message_id}
        if not queued:
            result["dropped"] = "session_not_active"
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

        with session.state_lock:
            if config_id == MODEL_CONFIG_ID:
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
        self._notify_config_update(session, config)

    def _handle_session_set_model_request(self, req_id: Any, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "")
        session = self._get_session(session_id)
        requested = params.get("modelId")
        if session is not None and isinstance(requested, str) and requested:
            try:
                config = self._config()
                with session.state_lock:
                    if not self._set_model(session, config, requested):
                        self.error(req_id, -32602, f"unknown model: {requested}")
                        return
                self._persist_session(session)
                self.result(req_id, {})
                self._notify_config_update(session, config)
                return
            except ConfigError as exc:
                self.error(req_id, -32602, f"Kiro BYO configuration error: {exc}")
                return
        self.result(req_id, {})

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

    def _handle_session_close_request(self, req_id: Any, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "")
        closed = self._pop_session(session_id)
        if closed is not None:
            closed.cancel_event.set()
            self._clear_steers(closed)
            # Persistence is deliberately retained. Continuation creates a new
            # runtime and reaches the thread through session/load.
            self._persist_session(closed)
            if closed.mcp is not None:
                closed.mcp.close()
            if closed.native is not None:
                closed.native.close()
        self.result(req_id, {})

    def _handle_session_delete_request(self, req_id: Any, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "")
        closed = self._pop_session(session_id)
        if closed is not None:
            closed.cancel_event.set()
            self._clear_steers(closed)
            if closed.mcp is not None:
                closed.mcp.close()
            if closed.native is not None:
                closed.native.close()
        self._delete_persisted_session(session_id)
        self.result(req_id, {})

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

        handlers = {
            "initialize": self._handle_initialize_request,
            "session/new": self._handle_session_new_request,
            "session/list": self._handle_session_list_request,
            "_session/steer": self._handle_session_steer_request,
            "_kiro/session/compact": self._handle_session_compact_request,
            "session/set_config_option": self._handle_session_set_config_option_request,
            "session/set_model": self._handle_session_set_model_request,
            "session/set_mode": self._handle_session_set_mode_request,
            "session/close": self._handle_session_close_request,
            "_kiro.dev/session/terminate": self._handle_session_close_request,
            "session/delete": self._handle_session_delete_request,
            "_kiro/session/delete": self._handle_session_delete_request,
        }
        handler = handlers.get(method)
        if handler is None:
            self.error(req_id, -32601, f"method not found: {method}")
            return
        handler(req_id, params)

    def _safe_handle(self, msg: dict[str, Any]) -> None:
        try:
            self.handle(msg)
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
        try:
            for raw in self.stdin:
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
                session.cancel_event.set()
            self._executor.shutdown(wait=True, cancel_futures=False)
            with self._sessions_lock:
                sessions = list(self.sessions.values())
                self.sessions.clear()
            for session in sessions:
                self._persist_session(session)
                if session.mcp is not None:
                    session.mcp.close()
                if session.native is not None:
                    session.native.close()
        return 0

def run_acp() -> int:
    return AcpServer().run()
