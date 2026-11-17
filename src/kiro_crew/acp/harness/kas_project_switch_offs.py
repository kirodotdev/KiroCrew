"""The checkout spec's switch-off keys layered on a KAS session's own servers.

Split out of :mod:`kiro_crew.acp.harness.kas`, which imports
:func:`_with_project_switch_offs` back under its old name.
"""

from __future__ import annotations

from typing import Any


def _keep_malformed_tool_list(own: dict[str, Any], keys: dict[str, Any]) -> dict[str, Any]:
    """*keys* without its ``disabledTools`` when *own* carries one that is not a list.

    KAS refuses a session whose spec holds a malformed ``disabledTools``. Joining a
    checkout's list onto it would replace it with a valid one and start the server
    with no exclusions, so the malformed value is left for the translator to refuse.
    """
    prior = own.get("disabledTools")
    if prior is None or isinstance(prior, list):
        return keys
    return {key: value for key, value in keys.items() if key != "disabledTools"}


def _with_project_switch_offs(spec: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    """*spec* with the checkout spec's switch-off keys layered on its own servers.

    Only narrows: a mute or switched-off tools from *project* reach a server *spec*
    already declares, and nothing else of the checkout does. A name *spec* does not
    declare is skipped, so the checkout can add no entry. The same keys the
    mirrored hosts layer on (``session_mcp._with_restrictions``).
    """
    from kiro_crew.acp.session_mcp import _project_restrictions, _with_restrictions

    declared = spec.get("mcpServers")
    if not isinstance(declared, dict):
        return spec
    kept = {
        name: _keep_malformed_tool_list(declared[name], keys)
        for name, keys in _project_restrictions(project).items()
        if isinstance(declared.get(name), dict)
    }
    return _with_restrictions(spec, kept)
