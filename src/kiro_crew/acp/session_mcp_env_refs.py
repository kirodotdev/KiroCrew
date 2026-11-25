"""``${VAR}`` / ``${env:VAR}`` in a session-array server's ``env``.

kiro-cli expands both forms in an MCP server's ``env`` when it reads the agent spec,
and the agent materializer leaves every reference but the home/login names for that
reader (``agent_materialization.mcp_sources._expand_env_values``). The third-party
harnesses that take Crew's ``session/new`` array start the server with what the
element carries, so a reference reached the server as literal text and a server that
reads a token or URL from it listed no tools. Measured against the real binaries:
codex, opencode, goose and DeepSeek pass both forms through verbatim, and Claude Code
expands ``${VAR}`` but not ``${env:VAR}``.

:func:`expand_entry_env` resolves the references in Crew before the element is built,
against the same filtered view, with the same miss rule, as the broker's sidecar
(``mcp_gateway.rewriter_env._expand_env_map``): a secret- or credential-named variable
is never dereferenced, and a miss stays literal. The KAS launcher uses the same view
and rule but leaves out a key whose value is exactly a reference to that key and
misses (``acp.kas_env_launch._resolved_env``), because the engine there hands the
server its own environment. The value then rides the in-process ACP pipe to a harness
that does not write the array to disk.

goose does write it: each server's ``envs`` go into its own ``sessions.db``. So its
mirror rebuilds those elements from the spec with :func:`keep_env_references`, and a
resolved value never reaches that file. Until goose gets a launch path that keeps the
value off its disk, its references stay unresolved there, as before.

No value is logged here; the expander logs a variable's NAME on a miss.

The array's env also carries what the gateway adds for its own code:
:func:`with_app_policy_markers` puts the cache-only policy markers on a builtin app
server that runs Kiro Crew's own code, as the managed servers already get them.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

#: A reference in either spelling, anywhere in a value (the expander's own pattern).
_REFERENCE = re.compile(r"\$\{(?:env:)?[^}]+\}")


def carries_env_reference(entry: Any) -> bool:
    """Whether a spec entry's ``env`` holds a ``${...}`` reference in any value."""
    env = entry.get("env") if isinstance(entry, dict) else None
    return isinstance(env, dict) and any(
        isinstance(v, str) and _REFERENCE.search(v) for v in env.values()
    )


def expand_entry_env(name: str, entry: Any) -> Any:
    """*entry* with the references in its ``env`` resolved, or *entry* unchanged.

    A copy: the spec dict belongs to the caller's parse.
    """
    if not carries_env_reference(entry):
        return entry
    from kiro_crew.mcp_gateway.rewriter_env import _expand_env_map

    return {**entry, "env": _expand_env_map(dict(entry["env"]), server=name)}


def keep_env_references(
    elements: list[dict[str, Any]], spec: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """*elements* with each server whose SPEC env holds a reference rebuilt from the
    spec, so its env carries the reference and not the resolved value.

    For a harness that writes the session's array to disk. Read from the spec, not
    the element: the element was built with the references already resolved.

    Only an element that IS that spec entry's translation is rebuilt. An element
    the projection put in place of the spec's entry (Crew's own control plane,
    re-derived from the managed source) is left as it is, so a spec declaring the
    same name cannot bring back its own command on an element that later carries
    the session's identity.

    A builtin app element that carries the gateway's policy markers
    (:func:`with_app_policy_markers`) is matched with the marker keys left out on
    both sides and rebuilt with the element's OWN marker pairs, never a second
    gateway read, so a ceiling or bound that changed since the array was built
    cannot cost the reference.
    """
    from kiro_crew.acp.session_mcp import acp_server_element

    raw = spec.get("mcpServers") if isinstance(spec, Mapping) else None
    declared = raw if isinstance(raw, Mapping) else {}
    out: list[dict[str, Any]] = []
    for element in elements:
        name = element.get("name")
        entry = declared.get(name) if isinstance(name, str) else None
        rebuilt = None
        if isinstance(name, str) and carries_env_reference(entry):
            if acp_server_element(name, expand_entry_env(name, entry)) == element:
                rebuilt = acp_server_element(name, entry)
            else:
                rebuilt = _rebuilt_marked_app_element(name, entry, element)
        out.append(rebuilt or element)
    return out


def _marker_pairs(element: Mapping[str, Any]) -> tuple[list[Any], list[Any]]:
    """``(other, markers)``: *element*'s env pairs split on the policy marker keys."""
    from kiro_crew.agent import POLICY_MARKER_KEYS

    env = element.get("env")
    pairs = env if isinstance(env, list) else []
    markers = [p for p in pairs if isinstance(p, dict) and p.get("name") in POLICY_MARKER_KEYS]
    return [p for p in pairs if p not in markers], markers


def _rebuilt_marked_app_element(
    name: str, entry: Any, element: dict[str, Any]
) -> dict[str, Any] | None:
    """*entry* translated with its reference kept and *element*'s own marker pairs.

    ``None`` unless *element* carries marker pairs, *name* is not one of Crew's
    managed servers (whose element is re-derived from the managed source, never
    the spec), and *element* equals *entry*'s expanded translation once the marker
    keys are left out of both. No gateway, manifest or installed-app state is read,
    so nothing that changed since the array was built can make this miss.
    """
    from kiro_crew.acp.session_mcp import acp_server_element
    from kiro_crew.agent import drop_policy_markers
    from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS

    others, markers = _marker_pairs(element)
    if not markers or not isinstance(entry, dict) or name in KIROCREW_BIN_MCP_SERVERS:
        return None
    bare = dict(entry)
    drop_policy_markers(bare)
    expected = acp_server_element(name, expand_entry_env(name, bare))
    if expected is None or {**element, "env": others} != expected:
        return None
    rebuilt = acp_server_element(name, bare)
    if rebuilt is None:
        return None
    rebuilt["env"] = [*rebuilt["env"], *(dict(p) for p in markers)]
    return rebuilt


def with_app_policy_markers(servers: dict[str, Any]) -> dict[str, Any]:
    """*servers* with the gateway's cache-only policy markers on Crew's own app servers.

    A builtin app server whose entry runs the shipped host-CLI launch
    (``session_mcp.builtin_app_identity_launches``) is Kiro Crew's own code and
    boots its own platform context, so on a centrally governed host it needs the
    markers the managed servers carry. The harness may not pass them on: under a
    ``cc`` or ``strict`` agent tier the sandbox launcher removes them from its
    environment. They go on this session's element only, never on the registered
    entry, and the entry's own copies are dropped first so the values are always
    the gateway's. Every other entry, and every entry on a host with no central
    ceiling, is returned as is. A touched entry is a copy: the dicts belong to the
    caller's parse.
    """
    from kiro_crew import agent as agent_facade

    if not agent_facade.policy_cache_only_env():
        return servers
    # circular import: session_mcp imports this module at load.
    from kiro_crew.acp.session_mcp import _declared_launch, builtin_app_identity_launches

    out = dict(servers)
    for name, launch in builtin_app_identity_launches().items():
        entry = out.get(name)
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("env", {}), dict)
            and _declared_launch(entry) == _declared_launch(launch)
        ):
            entry = dict(entry)
            agent_facade.drop_policy_markers(entry)
            agent_facade.add_policy_markers(entry)
            out[name] = entry
    return out
