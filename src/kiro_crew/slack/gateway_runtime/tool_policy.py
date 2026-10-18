"""Which tool calls an unattended gateway turn may run.

The deny-by-default classifiers behind the gateway's unattended approvals: the
read-only verb test ``--approval reads`` applies (``hooks.py`` imports it too), the
heartbeat's identity-based allowlist with its server-qualified edition extension,
the heartbeat-scoped hooks that drop the user's ``auto_approve_tools`` and the
gate's read-only tier, the sources that deny fast because no human answers them,
and tool-title normalisation.

The approval callbacks that consult these (``_interactive_approval`` and
``_heartbeat_approval``) live in ``turn_approvals.py``, beside the SEL audit sites
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
        LLMEvent,
        _is_host_read_only_builtin,
        current_context,
        is_read_only_bash,
        name_grant,
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
# a human, so the gateway keeps a strict explicit allowlist of read-only /
# observation tools that auto-approve.  Anything outside it is rejected with a
# SEL audit event so operators can see what got blocked and tune the list.
#
# The allowlist is **identity-based and exact-match only** (no verb/heuristic
# fallback).  Heartbeat polls untrusted external content (CR comments, ticket
# bodies) where prompt-injection could try to coax the agent into write
# actions; a verb-based fallback could be widened by a clever name like
# ``get_all_credentials`` or ``list_env_secrets`` from a malicious MCP server
# or injected payload.  Exact-match enforcement is auditable and cannot be
# widened that way.
#
# The decision keys on the event's TRUSTED identity -- ``mcp_identity_trusted``,
# ``tool_name``, ``mcp_server_name``, stamped by the adapter under ``_meta`` --
# never on ``event.title``, which is model-authored prose (``select_tool_title``
# even prefers the model's own ``description`` for some tools).  A backend that
# publishes no trusted identity gets no heartbeat tool approval at all.
#
# When a legitimate new read tool needs to run in heartbeat, operators
# observe the SEL ``denied`` events for it and explicitly add the name to
# the right set below.  This is deny-by-default per the security-controls guideline.

#: The one server the heartbeat spec always mounts.
_HEARTBEAT_CORE_SERVER = "kirocrew-core"

#: Kiro Crew core reads (no side effects) served by :data:`_HEARTBEAT_CORE_SERVER`.
#: Matched ONLY as the qualified ``@kirocrew-core/<name>`` identity, never bare: any
#: MCP server, an edition's included, could expose a tool with one of these names,
#: and a same-named tool on any other server is denied.
_HEARTBEAT_SAFE_CORE_MCP_TOOLS: frozenset[str] = frozenset(
    {
        "learn_list",
        "spawn_list",
        "spawn_status",
        "artifact_list",
        "artifact_get",
        "artifact_versions",
        "local_knowledge_search",
    }
)

_HEARTBEAT_STATUS_PREFIXES = ("Running: ",)


def _is_heartbeat_safe_tool(event: LLMEvent) -> bool:
    """Return True if *event* is safe to auto-approve in a heartbeat task.

    Decides on the event's trusted identity fields (``mcp_identity_trusted``,
    ``tool_name``, ``mcp_server_name``; populated from ``_meta.kiro``, never from
    agent-authored content) and never on ``event.title``.  No trusted identity, no
    approval: deny-by-default, with no fallback to parsing the title.

    Four branches, each on its own identity shape:

    * a host **shell** command (``is_shell`` with no ``mcp_server_name``): the
      recovered ``shell_command`` must classify read-only (``is_read_only_bash``)
      and must not run ``git`` at all -- git reads ``.git/config`` on invocation,
      and ``core.fsmonitor``/``core.hooksPath`` name a program there that no
      text classifier sees; heartbeat's cwd is exactly where a local writer could
      plant one, and nobody is behind this session to catch it;
    * a host **built-in**: ``hooks._is_host_read_only_builtin`` on the real
      ``_meta.kiro.toolName`` spellings (``fs_read``/``glob``/``grep``/
      ``web_fetch``/``web_search``);
    * a **core MCP read**: a ``_HEARTBEAT_SAFE_CORE_MCP_TOOLS`` name on
      ``kirocrew-core``, so the same name on another server is denied;
    * an **edition addition**: the exact ``@server/tool`` identity a companion pinned
      in ``SlackEnterpriseGate.heartbeat_safe_tools()``.

    A cache hit on two empty strings still counts as ``mcp_identity_trusted``
    (``acp/_dispatch_parts/permission_requests.py`` checks presence, not value), which is what a backend
    with no ``_meta.kiro`` support at all produces; every branch therefore also
    requires a non-empty ``tool_name``.
    """
    if not event.mcp_identity_trusted:
        return False
    tool_name = event.tool_name
    mcp_server_name = event.mcp_server_name
    if not tool_name:
        return False
    if event.is_shell and not mcp_server_name:
        # ``is_shell`` follows the ACP ``kind`` the agent declares, so an MCP tool
        # may declare ``execute`` while carrying a real server: that one is judged
        # on its pinned identity below, never on command text.  Here a genuine
        # host shell is judged on the host-observed command bytes
        # (``AcpEvent.shell_command``), through the same deny-by-default classifier
        # the interactive path trusts.  The program set comes from
        # ``name_grant.program_names``, the same walk ``_heartbeat_approval``'s
        # name-grant check runs: a command whose program set cannot be
        # established is denied, and git is excluded whatever the subcommand,
        # any case, because git reads ``.git/config`` and ``core.fsmonitor`` /
        # ``core.hooksPath`` name a program there.
        shell_command = event.shell_command
        if not shell_command:
            return False
        programs = name_grant.program_names(shell_command)
        if programs is None or any(p.rsplit("/", 1)[-1].lower() == "git" for p in programs):
            return False
        return is_read_only_bash(shell_command)
    if _is_host_read_only_builtin(
        tool_name,
        mcp_server_name,
        mcp_identity_trusted=event.mcp_identity_trusted,
        kas_builtin_ids=event.kas_builtin_ids,
    ):
        return True
    if not mcp_server_name:
        return False
    if mcp_server_name == _HEARTBEAT_CORE_SERVER and tool_name in _HEARTBEAT_SAFE_CORE_MCP_TOOLS:
        return True
    # Edition-contributed additions. Deferred context read via the sel.py pattern
    # so this module never imports the platform package at load time; fails closed
    # to the core set on any error.
    #
    # SECURITY -- match ONLY the trusted ``@server/tool`` identity pair, never a
    # bare tool name: a bare-name allowlist entry would let a DIFFERENT (or
    # compromised) MCP server expose a destructive tool with the same bare name
    # as an allowlisted read-only one, and an injected heartbeat could get it
    # auto-approved. An edition entry that is itself a bare name never matches.
    qualified = f"@{mcp_server_name}/{tool_name}"
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
    must NEVER widen the heartbeat allowlist -- ``llm_helpers._resolve_permission``
    consults ``hooks.on_tool_call()`` BEFORE the ``on_tool_approval`` callback,
    so a user-config auto-approve would bypass ``_heartbeat_approval``
    entirely (per code review).

    The heartbeat-scoped hooks keep:
      - sensitive-path deny (always-on, structural -- not from user config)
      - the user's ``auto_deny_tools`` (denies are safe; users can only
        narrow, not widen, what runs in heartbeat)
      - the user's ``confirm_tools`` and whether that list could be read
        (``confirm_tools_unknown``): a confirm-listed MCP call gets no verdict
        from the read-only tier, so it reaches ``_heartbeat_approval`` too

    They drop:
      - ``auto_approve_tools`` (set to empty so the heartbeat allowlist in
        ``_is_heartbeat_safe_tool`` is the sole approval authority)
      - ``auto_replies`` / ``transforms`` / ``context_rules`` (chat-only)
      - the gate's read-only classifier tier (``defer_reads_to_approver``): it
        auto-approves on the ACP ``kind``, the title and ``is_shell``, all of
        which the agent influences, so an edition MCP tool declaring
        ``kind="read"`` would otherwise run before ``_heartbeat_approval`` and
        its SEL audit ever saw it. Heartbeat judges reads itself, on the
        trusted identity, in ``_is_heartbeat_safe_tool``.

    The result: every tool call in a heartbeat session the deny tiers let
    through takes the ``on_tool_approval`` branch, where ``_heartbeat_approval``
    enforces the strict allowlist + SEL audit.
    """
    user_cfg = user_hooks._config  # noqa: SLF001 -- internal hooks state by design
    scoped = HooksConfig(
        auto_approve_tools=[],
        auto_deny_tools=list(user_cfg.auto_deny_tools),
        # The confirm list only withholds verdicts, so carrying it can narrow
        # what a heartbeat settles without the allowlist, never widen it.
        confirm_tools=list(user_cfg.confirm_tools),
        confirm_tools_unknown=user_cfg.confirm_tools_unknown,
        # Denied-command opt-out state carries over: denies can only narrow what
        # runs in a heartbeat session, never widen it, so the effective built-in
        # ruleset (and user-added denies) must apply here too.
        denied_commands_disabled_ids=list(user_cfg.denied_commands_disabled_ids),
        denied_commands_disable_all=user_cfg.denied_commands_disable_all,
        denied_commands_user_added=list(user_cfg.denied_commands_user_added),
        defer_reads_to_approver=True,
    )
    return HookManager(scoped)


def _bare_tool_name(title: str) -> str:
    """``Running: @server/Tool`` / ``mcp__server__Tool`` / ``Tool`` -> ``Tool``.

    A display helper over the title's wire forms; ``_is_heartbeat_safe_tool``
    never reads the title, it decides on the event's trusted identity.
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
