"""The hook pipeline's value types: the message-hook result and the tool gate's verdict
(``ToolHookResult``, whose constructors count every decision), the rule dataclasses,
the operator's denied-command pattern, and ``HooksConfig`` with its defensive parse of
the ``hooks`` section.

Composed onto ``kiro_crew.hooks``; see :mod:`kiro_crew.hook_runtime`.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.hooks import (
        _BUNDLED_AUTO_APPROVE_TOOLS,
        _GATE_UNCOUNTED,
        HOOK_INJECT_CONTEXT,
        HOOK_MODIFY,
        HOOK_PASSTHROUGH,
        HOOK_REPLY,
        TOOL_ALLOW,
        TOOL_AUTO_APPROVE,
        TOOL_DENY,
        _coerce_bool,
        logger,
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
    #: True when a TOOL_AUTO_APPROVE was decided by the call's VERIFIED MCP
    #: identity (``_meta.kiro`` server + tool, read from the client's own
    #: tool_call cache) and by nothing the agent authors -- the app-own-server
    #: grant, or an ``auto_approve_tools`` pattern matched against that
    #: identity. False for every grant that read the title, the payload's
    #: ``kind``, or a command. A consumer holding a backend-subagent request
    #: whose ARGUMENTS are unverified but whose identity is
    #: (``AcpEvent.child_mcp_identity_trusted``) may honor exactly these grants:
    #: their matched input is the same trusted identity, so a forged title
    #: cannot reach them. Any other auto-approve stays downgraded for that
    #: request, as before.
    identity_grant: bool = False
    #: True when a TOOL_AUTO_APPROVE is the read-only CLASSIFIER's verdict —
    #: i.e. a statement about what the call can DO: the deny-by-default bash
    #: classifier, and for a non-shell call either the interactive path's
    #: ACP-kind allow-list / read-only title fallback or, under
    #: ``classifier_only``, the host-known read-only built-in identity alone
    #: (``_HOST_READ_ONLY_BUILTIN_TOOLS``).
    #: False when it is a GRANT: an ``auto_approve_tools`` glob (title- or
    #: identity-keyed) or the app-own-server rule vouches for who is calling and
    #: says nothing about the call's effect. ``ToolApprovalPolicy.READ_ONLY``
    #: honours only the former; a surface with an interactive approver treats
    #: both alike. The action alone cannot say which branch produced it, and a
    #: result built outside the factory stays unproven (False) — fail-closed.
    read_only: bool = False
    #: The ``GATE_TIERS`` row that decided, stamped by ``HookManager.judge`` -- the
    #: per-target rule's name for a per-target tier (``"sensitive-bash"``, the same
    #: name the denial differential reports). ``""`` when no tier decided (the
    #: fall-through ``allow``), when the gate itself crashed, and for a result built
    #: anywhere else. Not part of equality: it says WHERE a verdict came from, not
    #: what it is.
    tier: str = field(default="", compare=False)

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

        The two rejected alternatives were instrumenting every exit of the gate's
        tiers (one call site per exit, on a security path) and
        counting in ``__post_init__`` behind a ``from_gate`` flag -- the flag had
        no reader other than the counter itself, so it was state carried purely to
        signal, where calling this from the four factories says the same thing
        with no field at all.

        ``action`` is one of three module constants and ``security_deny`` a bool,
        so the series is bounded by construction -- no reason string, tool name or
        command reaches the recorder.

        A consultation made inside :func:`uncounted_gate` is not counted: the
        transport floor re-asks the gate for a request its consumer already
        counted, and counting both would report one request as two decisions.
        """
        if _GATE_UNCOUNTED.get():
            return
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
    def auto_approve(*, identity_grant: bool = False, read_only: bool = False) -> ToolHookResult:
        """Auto-approve. ``identity_grant=True`` marks a grant decided by the
        verified MCP identity alone; ``read_only=True`` marks the read-only
        classifier's verdict, never a grant."""
        ToolHookResult._count(TOOL_AUTO_APPROVE, False)
        return ToolHookResult(
            action=TOOL_AUTO_APPROVE, identity_grant=identity_grant, read_only=read_only
        )

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
    #: Tool patterns that always ask before running, in the ``auto_approve_tools``
    #: spelling (``@server``, ``@server/tool*``, ``*``). A matching user-added MCP
    #: server stays mounted but is kept off the agent's blanket auto-approve list,
    #: so its calls reach the approval gate, and a match here outranks an
    #: ``auto_approve_tools`` grant. Empty by default: nothing changes unless the
    #: operator lists something.
    confirm_tools: list[str] = field(default_factory=list)
    #: Set when the load could not read a config file that names
    #: ``confirm_tools`` (``governance.confirm_tools_unknown``), whatever list it
    #: kept. The gate then withholds every ``auto_approve_tools`` grant, since the
    #: patterns it would have checked are not known. Derived state, never serialized.
    confirm_tools_unknown: bool = field(default=False, compare=False)
    auto_replies: list[AutoReplyHook] = field(default_factory=list)
    transforms: list[TransformHook] = field(default_factory=list)
    context_rules: list[ContextRule] = field(default_factory=list)
    # User-configurable denied-command opt-out state (Settings > Security),
    # persisted nested under the ``hooks.denied_commands`` sub-object.
    denied_commands_disabled_ids: list[str] = field(default_factory=list)
    denied_commands_disable_all: bool = False
    denied_commands_user_added: list[UserDeniedPattern] = field(default_factory=list)
    #: Structural, never read from config (``from_dict`` does not set it): a scoped
    #: HookManager builder (``slack.gateway._build_heartbeat_hooks``) sets it for a
    #: session whose own approval callback must see every call. When True the gate's
    #: read-only classifier tier (``_tier_read_only``) gives no verdict, so a call it
    #: would have auto-approved on the ACP ``kind``, the title or a read-only shell
    #: command falls through to that callback instead. The deny tiers above it are
    #: untouched, so this can only narrow what runs unattended, never widen it.
    defer_reads_to_approver: bool = False

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
            confirm_tools=_str_list(data.get("confirm_tools", [])),
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
            "confirm_tools": list(self.confirm_tools),
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
