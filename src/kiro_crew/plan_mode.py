"""Plan mode — a session-scoped read-only gate.

While plan mode is on for a session, the agent may research (read files, search,
fetch pages, spawn read-only helpers) but every mutating tool call is denied
before any approval rung runs, so the user gets a plan instead of edits. The
user turns plan mode off to execute.

Two halves, both here so they cannot drift:

* the **registry** — which session keys are currently planning, including
  inheritance to subagents spawned by a planning session;
* the **predicate** — whether one tool call is read-only, with the denial text
  the model sees.

Enforced from :func:`kiro_crew.hooks.HookManager.on_tool_call` (the pre-rung
every surface consults before its own trust/auto-approve ladder), which is the
single live chokepoint. Deny-by-default: a tool is blocked unless positively
recognized.

Design constraints worth keeping:

* **Not sourced from config.** Config is agent-writable, so a config-backed
  allowlist would let the model widen its own gate. Same reasoning as
  ``platform/interfaces.py::heartbeat_safe_tools``.
* **MCP tools match on the server-qualified name only.** A bare-name match
  would let any MCP server ship a tool called ``artifact_list`` and inherit the
  allowance.
* **Leaf module.** Standard library plus two KiroCrew leaves, so a security gate
  can import it without pulling in the dashboard or messaging graphs.
"""

from __future__ import annotations

import threading

from kiro_crew.bash_readonly import is_read_only_bash, unsafe_bash_reason
from kiro_crew.tool_identity import normalize_tool_identity

# ── Read-only tool allowlist ──

#: Built-in (non-MCP) tools that only read. Matched on the bare display name
#: after status-prefix stripping. Names cover both the kiro-cli spellings
#: ("Read", "Grep") and the tool ids agents declare ("fs_read", "grep").
PLAN_MODE_SAFE_TOOLS = frozenset(
    {
        # File and workspace reads
        "Read",
        "read",
        "fs_read",
        "Grep",
        "grep",
        "Glob",
        "glob",
        "WorkspaceSearch",
        # Network reads
        "web_fetch",
        "web_search",
        # Self-description / help
        "introspect",
        # Agent-local bookkeeping: a task list is scratch state, not a change
        # to the user's system, and planning is exactly when it gets written.
        "todo",
        "todo_list",
        # Deferred-tool discovery loads a tool spec; it never invokes one.
        "tool_search",
    }
)

#: MCP tools allowed while planning, as ``@server/tool``. Qualified on purpose:
#: an entry must not be inheritable by a same-named tool on another server.
PLAN_MODE_SAFE_MCP = frozenset(
    {
        # Reads over KiroCrew's own state
        "@kirocrew-core/learn_list",
        "@kirocrew-core/artifact_list",
        "@kirocrew-core/artifact_get",
        "@kirocrew-core/artifact_versions",
        "@kirocrew-core/artifact_get_comments",
        "@kirocrew-core/local_knowledge_search",
        "@kirocrew-core/list_sessions",
        "@kirocrew-core/search_chat_history",
        "@kirocrew-core/get_chat_session",
        "@kirocrew-core/skill_search",
        "@kirocrew-core/spawn_list",
        "@kirocrew-core/spawn_status",
        "@kirocrew-cron/cron_list",
        # Parallel investigation. Safe only because a subagent spawned by a
        # planning session inherits plan mode (see inherit()); the child hits
        # this same gate on its own session key.
        "@kirocrew-core/spawn_run",
        "@kirocrew-core/spawn_sub_agents",
        # Asking the user a question is how planning converges.
        "@kirocrew-core/ask_question",
    }
)

#: ``code`` is one tool with both read and write operations, so it is gated on
#: its ``operation`` argument rather than its name. Anything not listed here
#: (pattern_rewrite, rename_symbol, format, apply_code_action) mutates.
CODE_READ_OPERATIONS = frozenset(
    {
        "search_symbols",
        "lookup_symbols",
        "find_references",
        "goto_definition",
        "get_document_symbols",
        "get_diagnostics",
        "get_hover",
        "get_completions",
        "get_code_actions",
        "pattern_search",
        "generate_codebase_overview",
        "search_codebase_map",
        "initialize_workspace",
    }
)

#: Tool names whose identity is the ``code`` multiplexer.
_CODE_TOOL_NAMES = frozenset({"code", "Code"})

#: Prepended to every denial so the model can recognize the class of refusal
#: and stop retrying. The dashboard threads hook denial reasons into
#: ``build_refusal_recovery_prompt``, so this text reaches the model verbatim.
DENY_PREFIX = "Plan mode is on"

_DENY_TAIL = (
    "Do not retry and do not look for another way to make the change. "
    "Reading, searching, and web lookups still work — finish investigating, "
    "then write the plan and stop. The user turns plan mode off to execute it."
)


def deny_reason(
    tool_name: str,
    *,
    command: str | None = None,
    is_shell: bool = False,
    raw_params: dict | None = None,
    trusted_tool_name: str = "",
    trusted_server_name: str = "",
) -> str:
    """Return why this tool call is blocked while planning, or "" if allowed.

    ``trusted_tool_name`` / ``trusted_server_name`` are the canonical identity
    from ``_meta.kiro`` (``AcpEvent.tool_name`` / ``.mcp_server_name``) and are
    the ONLY thing an allow decision may key on. ``tool_name`` is the display
    title, which is LLM-authored prose — ``select_tool_title`` prefers the
    model's own ``description`` — so a write tool described as "Read" would
    otherwise match the read allowlist and be waived through to whatever
    auto-approval the slot already has. The title is still used to NAME the tool
    in the refusal text, which is harmless.

    ``raw_params`` supplies the ``code`` tool's ``operation``; that is the real
    input the call executes with, so it is trusted for the same reason
    ``command`` is. Deny-by-default: an unidentifiable call is blocked.
    """
    if is_shell or command:
        # Shell is decided on the real command, never the title. A shell tool
        # whose command could not be recovered is already denied upstream by
        # on_tool_call's deny-by-default guard.
        cmd = command or ""
        # strict_help: `foo --help` RUNS foo, so plan mode honours the probe
        # only for commands the classifier already trusts. trust-reads keeps
        # its looser default.
        if is_read_only_bash(cmd, strict_help=True):
            return ""
        why = unsafe_bash_reason(cmd, strict_help=True) or "the command is not read-only"
        return f"{DENY_PREFIX}, so this shell command is blocked ({why}). {_DENY_TAIL}"

    # Identity for the ALLOW decision comes from _meta.kiro only. When the
    # backend does not emit it the value is "", which matches nothing and so
    # denies — the safe direction, and loud rather than silent.
    bare, qualified = normalize_tool_identity(trusted_tool_name)
    if trusted_server_name and not qualified:
        qualified = f"@{trusted_server_name.lstrip('@')}/{bare}"
    # Display label may fall back to the title; it never gates anything.
    display = bare or normalize_tool_identity(tool_name)[0] or "this tool"

    if bare in _CODE_TOOL_NAMES:
        operation = ""
        if isinstance(raw_params, dict):
            operation = str(raw_params.get("operation") or "")
        if operation in CODE_READ_OPERATIONS:
            return ""
        detail = f"'{operation}' modifies code" if operation else "the operation is unknown"
        return f"{DENY_PREFIX}, so this code operation is blocked ({detail}). {_DENY_TAIL}"

    if qualified:
        if qualified in PLAN_MODE_SAFE_MCP:
            return ""
        return f"{DENY_PREFIX}, so {qualified} is blocked. {_DENY_TAIL}"

    if bare and bare in PLAN_MODE_SAFE_TOOLS:
        return ""

    if not bare:
        return (
            f"{DENY_PREFIX}, and this call carries no verifiable tool identity, "
            f"so it is blocked. {_DENY_TAIL}"
        )
    return f"{DENY_PREFIX}, so {display} is blocked. {_DENY_TAIL}"


# ── Session registry ──

_LOCK = threading.Lock()
_ACTIVE: set[str] = set()


def session_key_for_slot(slot: object) -> str:
    """The session key plan mode is keyed on for a dashboard *slot*.

    Must match what ``chat_runner._run_chat`` syncs, which prefers a linked
    session key (a cron- or workflow-driven slot runs under ``cron:<job>`` /
    the workflow's key) over the slot's own history key. Keying on
    ``dashboard:<key>`` instead would arm a string the gate never consults.

    Duck-typed on the two attributes rather than importing ``_ChatSlot``: this
    module is a leaf that the hook gate and the MCP wrapper both import, and
    pulling in the dashboard slot type would drag that whole graph with it.
    """
    linked = getattr(slot, "linked_session_key", "") or ""
    return linked or f"dashboard:{getattr(slot, 'key', '')}"


def activate(session_key: str) -> None:
    """Turn plan mode on for *session_key*. No-op for an empty key."""
    if not session_key:
        return
    with _LOCK:
        _ACTIVE.add(session_key)


def deactivate(session_key: str) -> None:
    """Turn plan mode off for *session_key*."""
    if not session_key:
        return
    with _LOCK:
        _ACTIVE.discard(session_key)


def set_active(session_key: str, on: bool) -> None:
    """Set plan mode for *session_key* to *on*."""
    if on:
        activate(session_key)
    else:
        deactivate(session_key)


def is_active(session_key: str) -> bool:
    """True when *session_key* is planning.

    An empty key is never active: a caller that cannot identify its session
    must not silently inherit another session's gate, in either direction.
    """
    if not session_key:
        return False
    with _LOCK:
        return session_key in _ACTIVE


def inherit(parent_session_key: str, child_session_key: str) -> bool:
    """Propagate plan mode from a parent session to a spawned child.

    Returns True when the child was marked. Called at subagent spawn: without
    it a planning session could spawn a helper that writes freely, since the
    gate is keyed on the child's own session key. ``spawn_run`` is on the
    plan-mode allowlist only because this holds.
    """
    if not child_session_key or not is_active(parent_session_key):
        return False
    activate(child_session_key)
    return True


def active_sessions() -> frozenset[str]:
    """Snapshot of planning session keys. For diagnostics and tests."""
    with _LOCK:
        return frozenset(_ACTIVE)


def reset() -> None:
    """Clear the registry. Tests only."""
    with _LOCK:
        _ACTIVE.clear()
