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
against the same filtered view, with the same miss rule, as the broker's sidecar and
the KAS launcher (``mcp_gateway.rewriter_env._expand_env_map``): a secret- or
credential-named variable is never dereferenced, and a miss stays literal. The value
then rides the in-process ACP pipe to a harness that does not write the array to disk.

goose does write it: each server's ``envs`` go into its own ``sessions.db``. So its
mirror rebuilds those elements from the spec with :func:`keep_env_references`, and a
resolved value never reaches that file. Until goose gets a launch path that keeps the
value off its disk, its references stay unresolved there, as before.

No value is logged here; the expander logs a variable's NAME on a miss.
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
        out.append(rebuilt or element)
    return out
