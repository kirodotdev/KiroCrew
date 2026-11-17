"""Which KAS capabilities a PreToolUse hook covers, and the asks that keep them on Crew's path.

A PreToolUse hook runs on Crew's permission path. A call KAS decides without a
permission request (an allow rule, a saved session answer, a KAS default) never
takes that path, so the projection gives every capability a hook covers an
explicit ``ask``, which outranks all of them. Split out of ``kas_permissions`` by
size; that module re-exports every public name here and owns the tables read.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from kiro_crew.acp.kas_hook_mcp import (
    McpToolNames,
    _bare_mcp_resources,
    _hook_mcp_resources,
    _names_mcp_tools,
    auto_approved_mcp_servers,
)
from kiro_crew.acp.kas_permissions import (
    _BUILTIN_GATEABLE_CAPABILITIES,
    _GLOB_METACHARACTERS,
    _HOOK_CAPABILITIES_BY_TOOL,
    _MCP_CAPABILITY,
    _MCP_PREFIX,
    CAPABILITY_BY_TOOL,
    CREW_ANSWERED_HOOK_HELD_KEY,
    HELD_MCP_PREFIX,
    HOOK_BUILTIN_MATCHER,
    HOOK_GATEABLE_CAPABILITIES,
    HOOK_GATED_MCP_PREFIX,
    HOOK_MCP_TAG,
    KAS_TOOL_IDS_BY_KIRO_TOOL,
    KAS_TOOL_MATCH_VOCABULARY,
    KIRO_TOOL_ALIASES,
    SPEC_HOOK_TOOL_TAGS,
    UNPARSEABLE_SHELL_GUARD,
    WITHHELD_FROM_AUTO_APPROVE,
    _AuditDecision,
    _chunked_rules,
)

# The projection's permission trail is read under this name.
logger = logging.getLogger("kiro_crew.acp.kas_permissions")


def hook_gated_capabilities(
    matchers: Iterable[str],
    *,
    mcp_servers: Iterable[str] | None = None,
    mcp_tool_names: McpToolNames | None = None,
) -> set[str]:
    """The capabilities a PreToolUse hook matcher could cover, each needing an ``ask``.

    Read with the hook store's own tool matcher, so a matcher covers here exactly
    what it covers at the gate. The reading errs toward covering, because the cost
    of a wrong answer is lopsided: covering too much turns an auto-approval into a
    permission request, covering too little lets a call past a hook the author
    wrote to gate it. The shell and filesystem families count
    (:data:`HOOK_GATEABLE_CAPABILITIES`): no allow rule reaches them from here, but
    a saved session answer can still decide their calls without a request.

    * An empty matcher, or ``*``, covers every tool.
    * ``@builtin`` covers every built-in capability and no MCP; ``@builtin/<glob>``
      covers the built-ins its glob names, read as a plain glob is below but with
      no MCP. ``@mcp`` covers all of MCP.
    * A literal tool name covers its own capability. The name is read in every
      spelling :data:`KAS_TOOL_IDS_BY_KIRO_TOOL`, :data:`KIRO_TOOL_ALIASES` and
      :data:`SPEC_HOOK_TOOL_TAGS` give it, so ``remote_web_search`` covers what
      ``web_search`` covers, ``run_command`` what ``execute_bash`` covers and
      ``web`` both web capabilities.
    * A matcher in an MCP form (``@server``, ``@server/tool``, ``@server/get_*``,
      ``mcp__server__tool``) is read against *mcp_servers*: unless it names exactly
      one of them (:func:`_hook_mcp_server`), it covers all of MCP. One with no
      wildcard that meets exactly one of that server's real tools
      (:func:`_hook_mcp_resources`) covers that tool alone, as a
      :data:`HOOK_GATED_MCP_PREFIX` entry: a hook on one MCP tool must not take
      auto-approval from every other one. Any other one (a bare ``@server``, a
      wildcard, which can meet a tool the cache does not list yet, a tool that is
      not cached) covers that server's tools, ``<server>/*``.
    * Any other literal covers everything: it names no tool this table knows, so
      it may be a harness's own name for a built-in one. Unless it is a bare name
      a cached tool of an auto-approved server answers to
      (:func:`_bare_mcp_resources`): then it names that MCP tool, and only the
      resources that function lists are covered.
    * A glob covers each built-in capability it matches by tool or capability
      name, and MCP, whose tool names at the gate this table does not spell.
      A glob that matches no name this module spells (``Fetch*``) covers
      everything, as an unknown literal does: it may match a harness's own
      name for a built-in tool.
    """
    covered: set[str] = set()
    for raw in matchers:
        matcher = raw.strip() if isinstance(raw, str) else ""
        if not matcher or matcher == "*":
            return set(HOOK_GATEABLE_CAPABILITIES)
        lowered = matcher.lower()
        if lowered == HOOK_BUILTIN_MATCHER:
            covered.update(_BUILTIN_GATEABLE_CAPABILITIES)
            continue
        if lowered.startswith(f"{HOOK_BUILTIN_MATCHER}/"):
            builtins = _builtins_matched(matcher[len(HOOK_BUILTIN_MATCHER) + 1 :])
            covered.update(builtins if builtins is not None else _BUILTIN_GATEABLE_CAPABILITIES)
            continue
        if lowered == HOOK_MCP_TAG:
            covered.add(_MCP_CAPABILITY)
            continue
        if _names_mcp_tools(matcher):
            covered.update(_hook_mcp_coverage(matcher, mcp_servers, mcp_tool_names))
            continue
        builtins = _builtins_matched(matcher)
        if builtins is None:
            bare = None
            if not _GLOB_METACHARACTERS.intersection(matcher):
                bare = _bare_mcp_resources(matcher, mcp_servers, mcp_tool_names)
            if bare is None:
                return set(HOOK_GATEABLE_CAPABILITIES)
            covered.update(HOOK_GATED_MCP_PREFIX + r for r in bare)
            continue
        covered.update(builtins)
        if _GLOB_METACHARACTERS.intersection(matcher):
            covered.add(_MCP_CAPABILITY)
    if _MCP_CAPABILITY in covered:
        # Every MCP tool is covered already; the per-tool entries add nothing.
        covered = {c for c in covered if not c.startswith(HOOK_GATED_MCP_PREFIX)}
    return covered


def hook_gated_auto_approved(covered: Iterable[str], approved: Iterable[str]) -> set[str]:
    """The entries of *covered* (:func:`hook_gated_capabilities`) that *approved*
    (:func:`auto_approved_capabilities`) still auto-approves.

    A capability is still approved when *approved* carries it. A
    :data:`HOOK_GATED_MCP_PREFIX` entry is when *approved* carries ``mcp`` and no
    :data:`HELD_MCP_PREFIX` entry for the same pattern.
    """
    granted = set(approved)
    out: set[str] = set()
    for entry in covered:
        if entry.startswith(HOOK_GATED_MCP_PREFIX):
            pattern = entry[len(HOOK_GATED_MCP_PREFIX) :]
            # A server-wide hold covers each of that server's tools too: a batch
            # projected before the tool list was cached holds ``srv/*``.
            server_wide = HELD_MCP_PREFIX + pattern.split("/", 1)[0] + "/*"
            if (
                _MCP_CAPABILITY in granted
                and HELD_MCP_PREFIX + pattern not in granted
                and server_wide not in granted
            ):
                out.add(entry)
        elif entry in granted:
            out.add(entry)
    return out


#: Every tool, tag or capability name this module spells: a matcher naming one of
#: them names a tool Crew knows, whether or not it is ever auto-approved.
_SPELLED_TOOL_NAMES: frozenset[str] = (
    frozenset(CAPABILITY_BY_TOOL)
    | frozenset(HOOK_GATEABLE_CAPABILITIES)
    | KAS_TOOL_MATCH_VOCABULARY
    | WITHHELD_FROM_AUTO_APPROVE
    | frozenset(SPEC_HOOK_TOOL_TAGS)
)


def _capabilities_named(name: str) -> set[str]:
    """The gateable capabilities *name* stands for, in any spelling.

    *name* as a capability, a Crew tool or a :data:`SPEC_HOOK_TOOL_TAGS` tag, and
    every kiro-cli tool it spells through :data:`KIRO_TOOL_ALIASES` or a
    :data:`KAS_TOOL_IDS_BY_KIRO_TOOL` row.
    """
    tools = {name, KIRO_TOOL_ALIASES.get(name, name), *SPEC_HOOK_TOOL_TAGS.get(name, ())}
    tools.update(kiro for kiro, ids in KAS_TOOL_IDS_BY_KIRO_TOOL.items() if name in ids)
    capabilities = {CAPABILITY_BY_TOOL[tool] for tool in tools if tool in CAPABILITY_BY_TOOL}
    for tool in tools:
        capabilities.update(_HOOK_CAPABILITIES_BY_TOOL.get(tool, ()))
    if name in HOOK_GATEABLE_CAPABILITIES:
        capabilities.add(name)
    return capabilities


def _builtins_matched(pattern: str) -> set[str] | None:
    """The built-in capabilities a tool matcher *pattern* names, or ``None`` when it
    names no tool this module spells (it may be a harness's own name for one).

    A literal is read through :func:`_capabilities_named`; a glob through each
    spelled name it matches, with the hook store's own matcher.
    """
    # Deferred: this module imports nothing of Crew's, and the hook store's is heavy.
    from kiro_crew.hooks import _tool_matches

    if not pattern:
        return None
    if not _GLOB_METACHARACTERS.intersection(pattern):
        name = pattern.lower()
        capabilities = _capabilities_named(name)
        if capabilities or name in _SPELLED_TOOL_NAMES:
            return capabilities
        return None
    matched = [name for name in _SPELLED_TOOL_NAMES if _tool_matches(pattern, name)]
    if not matched:
        return None
    return {capability for name in matched for capability in _capabilities_named(name)}


def _hook_mcp_coverage(
    matcher: str,
    servers: Iterable[str] | None,
    tool_names: McpToolNames | None,
) -> set[str]:
    """What an MCP-form *matcher* covers: one exact tool, one server, or all of MCP.

    See :func:`hook_gated_capabilities`. The server-wide answer is for the ``@``
    forms only, whose ``/`` ends the server name. A ``mcp__`` title can be split at
    more than one ``__`` (``mcp__a__b__c`` is a tool of ``a`` and of ``a__b``), and
    a server the policy does not list can still be reached, so a title matcher
    keeps all of MCP unless it resolves to one exact tool.
    """
    if not _GLOB_METACHARACTERS.intersection(matcher):
        resources = _hook_mcp_resources(matcher, servers, tool_names)
        if resources is not None and len(resources) == 1:
            return {HOOK_GATED_MCP_PREFIX + r for r in resources}
    server = _hook_mcp_server(matcher, servers)
    if server is None:
        return {_MCP_CAPABILITY}
    return {f"{HOOK_GATED_MCP_PREFIX}{server}/*"}


def withhold_hook_gated_auto_approval(
    policy: dict[str, Any] | None,
    pre_tool_matchers: Iterable[str],
    *,
    audit_decision: _AuditDecision = lambda refs, outcome, reason: None,
    agent_id: str = "",
    mcp_tool_names: McpToolNames | None = None,
) -> dict[str, Any] | None:
    """*policy* with an ``ask`` for every capability a PreToolUse hook covers.

    A PreToolUse hook runs on Crew's permission path, and an auto-approved call
    never takes that path, so the hook would not see it. KAS resolves the most
    restrictive matching rule (``deny`` over ``ask`` over ``allow``, not first
    match), so one unscoped ``ask`` outranks every ``allow`` on that capability:
    the derived ones and any the author's own block relayed. The call then comes
    back as a permission request, where the hook runs and Crew's own approval
    decides as it does for any other request. A ``deny`` still outranks it.

    A hook written in an MCP form, with no wildcard, that resolves to exactly one real
    ``<server>/<tool>`` resource, among the tools *mcp_tool_names* lists for the
    servers *policy* auto-approves (:func:`_hook_mcp_resources`), gets an ``ask``
    on that exact resource. The same precedence holds there, so that call still
    comes back as a permission request, while every other MCP tool keeps the
    auto-approval its ``allow`` gave it. An ``@server`` hook, or one with a
    wildcard, on one of those servers gets the ``ask`` on ``<server>/*``. Any other
    MCP-form hook gets the ``ask`` on all of MCP. *mcp_tool_names* defaults to the
    MCP probe cache (:func:`kiro_crew.acp.kas_permissions.cached_mcp_tool_names`, looked up
    at call time).

    A ``None`` or empty *policy* gets the asks too. No rule is not the same as
    "every call asks": a session answer Crew saved (``allow_always``), a KAS
    default and a user-scope rule can each decide a call without a request, and an
    explicit ``ask`` outranks all three (KAS ``policy-engine.ts``,
    ``session-authority.ts``). For the same reason the shell and filesystem
    families get an ``ask`` when a hook covers them, and a shell ``ask`` comes with
    the :data:`UNPARSEABLE_SHELL_GUARD` ``deny``, so a command KAS cannot parse
    stays denied instead of reaching Crew as a request no gate can read.

    Each withheld capability or MCP resource is reported through
    ``audit_decision``, the same permission trail the ceiling's withholds use.
    """
    covered = hook_gated_capabilities(
        pre_tool_matchers,
        mcp_servers=auto_approved_mcp_servers(policy),
        mcp_tool_names=mcp_tool_names if mcp_tool_names is not None else _cached_tool_names,
    )
    if not covered:
        return policy
    capabilities = sorted(c for c in covered if not c.startswith(HOOK_GATED_MCP_PREFIX))
    mcp_resources = sorted(
        c[len(HOOK_GATED_MCP_PREFIX) :] for c in covered if c.startswith(HOOK_GATED_MCP_PREFIX)
    )
    refs = [*capabilities, *(f"{_MCP_PREFIX}{r}" for r in mcp_resources)]
    logger.info(
        "agent %r: not auto-approving %s on this backend -- a PreToolUse hook covers "
        "it, and an auto-approved call raises no permission request for it to run on",
        agent_id,
        ", ".join(refs),
    )
    for ref in refs:
        audit_decision(ref, "withheld", "a PreToolUse hook covers it")
    asks: list[dict[str, Any]] = [
        {"capability": capability, "effect": "ask"} for capability in capabilities
    ]
    asks.extend(_chunked_rules(_MCP_CAPABILITY, "ask", mcp_resources))
    if "shell" in capabilities:
        asks.append({"capability": "shell", "match": [UNPARSEABLE_SHELL_GUARD], "effect": "deny"})
    base = policy.get("rules") if isinstance(policy, dict) else None
    return {"rules": [*(base if isinstance(base, list) else []), *asks]}


def hook_held_grants(
    policy: dict[str, Any] | None,
    held: dict[str, Any] | None,
    *,
    mcp_tool_names: McpToolNames | None = None,
) -> list[str]:
    """The ``@server/tool`` refs *policy* auto-approves and *held*, its
    :func:`withhold_hook_gated_auto_approval` result, does not.

    Those are the MCP grants a hook's ``ask`` took from KAS. Each such call comes
    back as a permission request, and on a surface that fires the PreToolUse hooks
    on it Crew's gate answers it on the spec's behalf after its deny rows and
    governance (``gate_tiers._tier_batch_guard_grant``), so the granted tool still
    runs with no prompt and the hook still runs, as on kiro-cli. A surface that
    does not tell the gate it fires them (a channel bot's turn) keeps asking. Both policies are read as KAS
    resolves them (``batch_guard._auto_approved``), so an author's own ``ask`` or
    ``deny`` keeps a tool out, and it prompts as written.

    The candidates are the exact ``<server>/<tool>`` entries of the policy's ``mcp``
    ``allow`` rules and, for a grant written as a glob, each cached tool
    (*mcp_tool_names*, the MCP probe cache by default) of every server the policy
    auto-approves. A glob grant on a server with no cached tool list adds none, so
    its calls still prompt.
    """
    # Deferred: the batch guard reads ``kas_permissions`` at call time too, so
    # neither module imports the other at load.
    from kiro_crew.acp.batch_guard import _auto_approved

    before = policy.get("rules") if isinstance(policy, dict) else None
    after = held.get("rules") if isinstance(held, dict) else None
    if not isinstance(before, list) or not isinstance(after, list):
        return []
    candidates: set[str] = set()
    for rule in before:
        if not isinstance(rule, dict) or rule.get("effect") != "allow":
            continue
        match = rule.get("match") if rule.get("capability") == _MCP_CAPABILITY else None
        candidates.update(
            p
            for p in (match if isinstance(match, list) else ())
            if isinstance(p, str) and "/" in p and not _GLOB_METACHARACTERS.intersection(p)
        )
    names = mcp_tool_names if mcp_tool_names is not None else _cached_tool_names
    for server in auto_approved_mcp_servers(policy) or ():
        candidates.update(f"{server}/{tool}" for tool in names(server) or ())
    return sorted(
        f"{_MCP_PREFIX}{resource}"
        for resource in candidates
        if _auto_approved(before, resource) and not _auto_approved(after, resource)
    )


def _entry_hook_held(entry: Any) -> frozenset[str]:
    """The :data:`~kiro_crew.acp.kas_permissions.CREW_ANSWERED_HOOK_HELD_KEY` refs of
    one projected entry. Anything that is not a list of strings answers empty."""
    raw = entry.get(CREW_ANSWERED_HOOK_HELD_KEY) if isinstance(entry, dict) else None
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(n for n in raw if isinstance(n, str) and n)


def projected_hook_held(custom_agents: Any, agent_id: str) -> frozenset[str]:
    """The :func:`hook_held_grants` refs of *agent_id*'s registered entry, matched by
    id exactly like ``kas_agents.projected_crew_answered``. Kept apart from that set:
    the gate answers them only on a surface that fires the PreToolUse hooks."""
    from kiro_crew.acp.kas_agents import _registered_entry

    return _entry_hook_held(_registered_entry(custom_agents, agent_id))


def switched_hook_held(custom_agents: Any, mode_id: str) -> frozenset[str]:
    """Those refs once a KAS mode switch moves the session to *mode_id* (a wire id)."""
    from kiro_crew.acp.kas_agents import _switched_entry

    return _entry_hook_held(_switched_entry(custom_agents, mode_id))


def _cached_tool_names(server: str) -> list[str] | None:
    """:func:`kiro_crew.acp.kas_permissions.cached_mcp_tool_names`, read at call time,
    so the projection and the staleness check read one seam."""
    from kiro_crew.acp import kas_permissions

    return kas_permissions.cached_mcp_tool_names(server)


def _hook_mcp_server(matcher: str, servers: Iterable[str] | None) -> str | None:
    """The one server of *servers* an ``@server`` / ``@server/...`` *matcher* names.

    Its server part (up to the first ``/``) is compared with each server in any
    case, as the hook store compares, and the server's own spelling is returned.
    ``None`` for any other form, a server part with glob syntax, when *servers*
    is ``None``, and unless exactly one server matches: the caller then covers all
    of MCP.
    """
    if servers is None or not matcher.startswith(_MCP_PREFIX):
        return None
    part = matcher[len(_MCP_PREFIX) :].split("/", 1)[0]
    if not part or _GLOB_METACHARACTERS.intersection(part):
        return None
    matches = [server for server in servers if server.lower() == part.lower()]
    return matches[0] if len(matches) == 1 else None
