"""What a tool call IS: the non-model-authored identity tests, the first-party app
and builtin-agent registries' readers and writers, the canonical MCP reference,
the title normalization and the pattern matchers.

Composed onto ``kiro_crew.hooks``; see :mod:`kiro_crew.hook_runtime`.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.hooks import (
        _BUILTIN_APP_AGENTS,
        _BUILTIN_APP_MCP_SERVERS,
        _BUILTIN_APP_NAMES,
        _GLOBAL_INLINE_FLAGS_RE,
        _HOST_READ_ONLY_BUILTIN_TOOLS,
        _TITLE_ONLY_GRANT_NOTED,
        _TITLE_ONLY_GRANT_NOTED_CAP,
        _TOOL_TITLE_PREFIXES,
        CORE_MCP_SERVER,
        TOOL_AUTO_APPROVE,
        ToolHookResult,
        _bounded_pattern_search,
        logger,
    )


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


def hook_gate_kwargs(event: object, **overrides: Any) -> dict[str, Any]:
    """The event-derived keyword arguments for ``HookManager.on_tool_call``.

    One extraction, used by every permission-path dispatcher
    (``...hooks.on_tool_call(event.title, session_key=..., **hook_gate_kwargs(event))``)
    so an enforcement-relevant event field is threaded ONCE. A dispatcher that
    hand-copies the fields it happens to know about drops the ones it does not
    — the edit gate's ``diff_path``, or the trusted MCP identity a per-tool
    deny / governance ``@server/tool`` rule keys on — and the drop is SILENT:
    the gate never sees that signal on that surface. That is why the threading
    lives here and not at the sites. ``test_hooks.py`` pins the helper's output
    against the gate's own keyword signature (a new gate parameter must be
    extracted here) and scans the package so no site hand-copies a field.

    Reads the event duck-typed (``getattr`` with the gate's own defaults),
    exactly as the channel dispatchers already did: an ``AcpEvent`` yields its
    fields verbatim, a provider event or test double missing a field yields the
    gate default for it, and a ``None`` in a string/bool slot is normalised to
    that default. ``command`` comes from ``AcpEvent.shell_command`` (None for a
    non-shell tool or an unrecoverable command, which the gate then denies by
    default when ``is_shell`` is set); ``mcp_tool_name`` is the event's
    ``tool_name`` (the ``_meta.kiro`` identity, not the model-authored
    ``title``).

    ``overrides`` let a surface with a genuinely different event shape replace
    an extracted value (the auto-improvement runner recovers the command
    provider-agnostically and falls back from ``tool_kind`` to ``tool_purpose``).
    An override key the helper does not emit is refused: a misspelt override
    would otherwise add a stray kwarg the gate rejects — or worse, one a future
    gate accepts with a meaning the site never intended — so the failure is
    loud and at the site. The structural test pins which sites override which
    keys, so a new override is a reviewed change, never drift.
    """
    kwargs: dict[str, Any] = {
        "tool_kind": getattr(event, "tool_kind", "") or "",
        "raw_params": getattr(event, "raw_tool_params", None),
        "diff_path": getattr(event, "diff_path", "") or "",
        "command": getattr(event, "shell_command", None),
        "is_shell": bool(getattr(event, "is_shell", False)),
        "mcp_server_name": getattr(event, "mcp_server_name", "") or "",
        "mcp_tool_name": getattr(event, "tool_name", "") or "",
        "mcp_identity_trusted": bool(getattr(event, "mcp_identity_trusted", False)),
        "spawn_target": getattr(event, "spawn_target", "") or "",
    }
    unknown = set(overrides) - set(kwargs)
    if unknown:
        raise TypeError(
            "hook_gate_kwargs: override of a key it does not extract: " + ", ".join(sorted(unknown))
        )
    kwargs.update(overrides)
    return kwargs


def _is_host_read_only_builtin(
    mcp_tool_name: str, mcp_server_name: str, *, mcp_identity_trusted: bool
) -> bool:
    """True when the host-trusted identity names a known read-only BUILT-IN.

    Three facts must hold, and the first is a POSITIVE provenance signal rather
    than an absence:

    * ``mcp_identity_trusted`` — the identity pair was populated from a
      provenance-verified source (``AcpEvent.mcp_identity_trusted``: the
      ``_meta.kiro`` parse this client made of the tool_call frame, carried to
      the permission event through the origin-scoped caches). A pair that
      arrived any other way — an inline payload, an event a caller built by
      hand, a cache miss — says nothing about WHO named the tool, so a
      ``fs_read`` there is prose, not identity. This is the same flag
      ``event_is_spawn_run`` demands before it trusts a tool identity.
    * ``mcp_tool_name`` non-empty — ``_meta.kiro.toolName``, which kiro-cli
      sets for built-ins too. Empty (a backend that omits ``_meta.kiro``, an
      uncached permission event) identifies nothing and matches nothing.
    * ``mcp_server_name`` empty — kiro-cli stamps ``mcpServerName`` on every
      MCP-served call, so an MCP server exposing a tool that happens to be
      called ``fs_read`` carries a server name and fails closed here. Only a
      built-in has no server behind it.

    What this cannot see: a backend that stamps ``toolName`` but never
    ``mcpServerName`` for MCP-served calls. The provenance flag proves the pair
    came from the frame this client parsed, not that the backend honoured the
    stamping contract; that contract belongs to kiro-cli
    (``kiro_tool_identity_meta`` in the engine) and is the one every
    ``mcp_server_name`` consumer in ``kiro_crew.hooks`` already rests on.
    """
    if not mcp_identity_trusted:
        return False
    if mcp_server_name or not mcp_tool_name:
        return False
    return mcp_tool_name in _HOST_READ_ONLY_BUILTIN_TOOLS


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
    in ``HookManager.on_tool_call`` (``_app_owns_mcp_server`` returns False for a
    blank app).

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


def _note_title_only_grant_pattern(pattern: str, identity_ref: str) -> None:
    """Log once that an ``auto_approve_tools`` pattern matches only the title.

    For an MCP call with a verified identity the grant is keyed on
    ``@server/tool``, so a pattern written against the agent-authored title
    (a ``description``, or a title that does not spell the identity) stops
    granting. The visible symptom is an approval card, which on an unattended
    surface nobody answers; this line is the breadcrumb that connects the card
    to the pattern and names the rewrite.
    """
    key = (pattern, identity_ref)
    if key in _TITLE_ONLY_GRANT_NOTED:
        return
    if len(_TITLE_ONLY_GRANT_NOTED) >= _TITLE_ONLY_GRANT_NOTED_CAP:
        _TITLE_ONLY_GRANT_NOTED.clear()
    _TITLE_ONLY_GRANT_NOTED.add(key)
    logger.warning(
        "auto_approve_tools pattern %r matches this call's title but not its verified MCP "
        "identity %s; for an MCP call the grant is keyed on the identity, so the call falls "
        "to interactive approval. Rewrite the pattern as %r (or 'Running: %s').",
        pattern,
        identity_ref,
        identity_ref,
        identity_ref,
    )


def identity_grant_covers_child(result: ToolHookResult, event: object) -> bool:
    """True when a hook auto-approve may stand for a LOW-FIDELITY child request.

    A backend-subagent permission event whose arguments are unverified is
    normally downgraded past every hook auto-approve, because those grants read
    the agent-authored title. The one exception is a grant the hook decided by
    the call's VERIFIED MCP identity (``ToolHookResult.identity_grant``) for an
    event whose own identity verified (``AcpEvent.child_mcp_identity_trusted``):
    both sides of that match are the same ``_meta.kiro`` server/tool pair the
    client cached from the tool_call frame, so nothing the agent authors
    reaches the decision. It is also the user's NARROW grant — they allowed
    this tool — where session trust-all or YOLO would allow every tool the
    child calls. The dashboard runner and the subagent manager both consult
    this so the two consumers cannot drift on the rule.
    """
    return bool(
        result.action == TOOL_AUTO_APPROVE
        and result.identity_grant
        and getattr(event, "child_mcp_identity_trusted", False)
    )


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


def _has_global_inline_flags(pattern: str) -> bool:
    """True when *pattern* starts with a global Python flag directive."""
    return _GLOBAL_INLINE_FLAGS_RE.match(pattern) is not None
