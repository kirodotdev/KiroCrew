"""Which tool calls an unattended gateway turn may run.

The deny-by-default classifiers behind the gateway's unattended approvals: the
read-only verb test ``--approval reads`` applies (``hooks.py`` imports it too), the
heartbeat's exact-name allowlist with its server-qualified edition extension, the
heartbeat-scoped hooks that drop the user's ``auto_approve_tools``, the sources that
deny fast because no human answers them, and tool-title normalisation.

The approval callbacks that consult these (``_interactive_approval`` and
``_heartbeat_approval``) stay in the facade, beside the SEL audit sites
``test_security_posture`` counts there.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import (
        HookManager,
        HooksConfig,
        current_context,
        re,
        safe_context_call,
    )


# Approval sources that run UNATTENDED (no human responder). These deny-fast on a
# short window instead of burning the full 2h human-approval window. Subagent
# approvals are NOT background: they route to the dashboard where the spawning
# human is present (via the parent slot), so they keep the long interactive window.
_BACKGROUND_APPROVAL_SOURCES = frozenset({"cron", "heartbeat", "taskrunner", "autonudge", ""})


# Tool-name prefixes treated as read-only by the --approval reads flag.
# Matched against the leading verb token of an event.title (e.g. "Read foo.txt"
# -> "read"). Conservative list — anything not on it falls through to the
# standard approval flow.
_READ_ONLY_TOOL_PREFIXES = (
    "read",
    "list",
    "get",
    "search",
    "find",
    "describe",
    "show",
    "view",
    "fetch",
    "query",
    "grep",
    "ls",
    "cat",
    "head",
    "tail",
)


# Tokens that disqualify a tool from auto-approval even if its leading
# verb is in _READ_ONLY_TOOL_PREFIXES. After splitting the title on
# whitespace/punctuation/underscore/dash, any resulting token that exactly
# matches one of these entries causes rejection. Catches compound names
# a third-party MCP author might pick (e.g. read_or_write, find_and_replace,
# get_or_create) where the read prefix masks a write capability. Fail
# closed on ambiguity.
_WRITE_INDICATORS = (
    "write",
    "delete",
    "create",
    "destroy",
    "remove",
    "update",
    "modify",
    "replace",
    "set",
    "put",
    "post",
    "exec",
    "execute",
    "run",
    "rm",
    "rmdir",
    "drop",
    "patch",
    "send",
    "publish",
    "save",
    "edit",
    "kill",
    "terminate",
)


def _is_read_only_tool(event_title: str) -> bool:
    """Return True if event_title looks like a read-only tool invocation.

    Used by --approval reads to auto-approve a conservative set of read
    verbs while still gating writes. Two-stage check:

    1. Leading token (before any whitespace/punctuation) must be in
       _READ_ONLY_TOOL_PREFIXES.
    2. After splitting the title on whitespace/punctuation/underscore/dash,
       no resulting token may exactly match one in _WRITE_INDICATORS — catches
       compound names like read_or_write, find_and_replace, get_or_create.
       Exact token equality, not substring containment: ``setter`` does not
       match ``set``.

    Fails closed on ambiguity.
    """
    if not event_title:
        return False
    lowered = event_title.strip().lower()
    if not lowered:
        return False
    # Tokenize on whitespace, underscores, dashes, and common punctuation
    # so compound names like read_or_write break into ["read", "or", "write"].
    tokens = [t for t in re.split(r"[\s_\-:()/.,]+", lowered) if t]
    if not tokens:
        return False
    leading = tokens[0]
    if leading not in _READ_ONLY_TOOL_PREFIXES:
        return False
    # Reject if any token (other than the leading verb itself) is a known
    # write indicator. Catches read_or_write, find_and_replace, etc.
    if any(token in _WRITE_INDICATORS for token in tokens):
        return False
    return True


# ── Heartbeat tool allowlist ──
#
# Heartbeat sessions run unattended on a timer.  Tool approval cannot prompt
# a human, so we maintain a strict explicit allowlist of read-only /
# observation tools that auto-approve.  Anything outside the list is rejected
# with a SEL audit event so operators can see what got blocked and tune the
# list.
#
# The allowlist is **name-based and exact-match only** (no verb/heuristic
# fallback).  Heartbeat polls untrusted external content (CR comments, ticket
# bodies) where prompt-injection could try to coax the agent into write
# actions; a verb-based fallback could be widened by a clever name like
# ``get_all_credentials`` or ``list_env_secrets`` from a malicious MCP server
# or injected payload.  Exact-match enforcement is auditable and cannot be
# widened that way.
#
# When a legitimate new read tool needs to run in heartbeat, operators
# observe the SEL ``denied`` events for it and explicitly add the name to
# this set.  This is deny-by-default per the security-controls guideline.
HEARTBEAT_SAFE_TOOLS = frozenset(
    {
        # Local / built-in read tools
        "Read",
        "Grep",
        "Glob",
        # Workspace exploration
        "WorkspaceSearch",
        # Kiro Crew core reads (no side effects)
        "learn_list",
        "cron_list",
        "spawn_list",
        "spawn_status",
        "artifact_list",
        "artifact_get",
        "artifact_versions",
        "local_knowledge_search",
    }
)


_HEARTBEAT_STATUS_PREFIXES = ("Running: ",)


def _is_heartbeat_safe_tool(event_title: str) -> bool:
    """Return True if *event_title* is safe to auto-approve in a heartbeat task.

    Strict exact-name match against ``HEARTBEAT_SAFE_TOOLS``.  No verb-based
    fallback — heartbeat polls untrusted external content (CR comments,
    ticket bodies) where prompt-injection could try to widen approval via a
    clever read-shaped tool name (``get_all_credentials``,
    ``list_env_secrets``, etc.).  Per security-controls deny-by-default:
    reject unless positively confirmed.

    Title normalization (applied before the set lookup):

    1. Strip leading status prefix (e.g. ``Running: ``).
    2. Strip ACP ``mcp__<server>__<Tool>`` prefix.
    3. Strip runtime ``@<server>/<Tool>`` prefix — kiro-cli titles arrive as
       ``Running: @example-mcp/SomeTool`` at the gateway.

    Only the **bare tool name** is tested against the frozenset.

    Returns False on empty / whitespace-only / unrecognised names.
    """
    if not event_title:
        return False
    name = event_title.strip()
    if not name:
        return False
    # Strip leading status prefix: "Running: @example-mcp/Tool" → "@example-mcp/Tool"
    for prefix in _HEARTBEAT_STATUS_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    # Preserve the server-QUALIFIED form (before the prefix is stripped) so the
    # edition allowlist can match on the full identity and avoid bare-name
    # collisions — normalized to the "@server/Tool" spelling regardless of which
    # wire form arrived ("mcp__server__Tool" or "@server/Tool").
    qualified = ""
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            qualified = f"@{parts[1]}/{parts[2]}"
    elif name.startswith("@") and "/" in name:
        qualified = name
    # Strip MCP server prefix: "mcp__example-mcp__ToolName" → "ToolName"
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            name = parts[2]
    # Strip @server/Tool prefix: "@example-mcp/SomeTool" → "SomeTool"
    if name.startswith("@") and "/" in name:
        name = name.rsplit("/", 1)[-1]
    if name in HEARTBEAT_SAFE_TOOLS:
        return True
    # Edition-contributed additions. Deferred context read via the sel.py pattern
    # so this module never imports the platform package at load time; fails closed
    # to the core set on any error.
    #
    # SECURITY — match ONLY the server-qualified "@server/Tool" identity, never a
    # bare tool name: a bare-name allowlist entry would let a DIFFERENT (or
    # compromised) MCP server expose a destructive tool with the same bare name
    # as an allowlisted read-only one, and an injected heartbeat could get it
    # auto-approved. So a title with no resolvable server (``qualified == ""``)
    # can never match an edition entry, and an edition entry that is itself a
    # bare name simply never matches any qualified title. This keeps the
    # deny-by-default boundary intact; the companion MUST pin "@server/Tool".
    if not qualified:
        return False
    empty: frozenset[str] = frozenset()
    extra: frozenset[str] = safe_context_call(
        lambda: current_context().slack_gate.heartbeat_safe_tools(),
        fallback=empty,
        log_message="heartbeat_safe_tools lookup failed; using core set only",
    )
    return qualified in extra


def _build_heartbeat_hooks(user_hooks: HookManager) -> HookManager:
    """Return a HookManager scoped for heartbeat use.

    The interactive user's ``auto_approve_tools`` (e.g. ``*``, ``Write*``)
    must NEVER widen the heartbeat allowlist — ``llm_helpers._resolve_permission``
    consults ``hooks.on_tool_call()`` BEFORE the ``on_tool_approval`` callback,
    so a user-config auto-approve would bypass ``_heartbeat_approval``
    entirely (per code review).

    The heartbeat-scoped hooks keep:
      - sensitive-path deny (always-on, structural — not from user config)
      - the user's ``auto_deny_tools`` (denies are safe; users can only
        narrow, not widen, what runs in heartbeat)

    They drop:
      - ``auto_approve_tools`` (set to empty so ``HEARTBEAT_SAFE_TOOLS`` is
        the sole approval authority)
      - ``auto_replies`` / ``transforms`` / ``context_rules`` (chat-only)

    The result: every tool call in a heartbeat session takes the
    ``on_tool_approval`` branch, where ``_heartbeat_approval`` enforces
    strict allowlist + SEL audit.
    """
    user_cfg = user_hooks._config  # noqa: SLF001 — internal hooks state by design
    scoped = HooksConfig(
        auto_approve_tools=[],
        auto_deny_tools=list(user_cfg.auto_deny_tools),
        # Denied-command opt-out state carries over: denies can only narrow what
        # runs in a heartbeat session, never widen it, so the effective built-in
        # ruleset (and user-added denies) must apply here too.
        denied_commands_disabled_ids=list(user_cfg.denied_commands_disabled_ids),
        denied_commands_disable_all=user_cfg.denied_commands_disable_all,
        denied_commands_user_added=list(user_cfg.denied_commands_user_added),
    )
    return HookManager(scoped)


def _bare_tool_name(title: str) -> str:
    """``Running: @server/Tool`` / ``mcp__server__Tool`` / ``Tool`` -> ``Tool``.

    Same wire forms ``_is_heartbeat_safe_tool`` unwraps; kept separate
    because that helper answers an allowlist question and this one only
    needs the name.
    """
    name = (title or "").strip()
    for prefix in _HEARTBEAT_STATUS_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            name = parts[2]
    if name.startswith("@") and "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name.strip()
