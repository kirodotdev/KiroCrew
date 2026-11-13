"""The kiro-cli tool name <-> KAS tool id rows a tool matcher is compared with.

Split out of :mod:`kiro_crew.acp.kas_permissions`, which re-exports every name here,
so ``kas_permissions`` stays the import path callers use.
"""

from __future__ import annotations

from kiro_crew.platform.tool_names import KIRO_CLI_NAME_BY_KAS_TOOL, KIRO_TOOL_ALIASES

#: kiro-cli tool name -> the KAS tool ids that do the same job.
#:
#: DERIVED, not hand-kept: the kiro-cli <-> KAS relationship has ONE table,
#: :data:`kiro_crew.platform.tool_names.KIRO_CLI_NAME_BY_KAS_TOOL` (the policy
#: direction: each KAS built-in id -> the kiro-cli name a rule about it is
#: written under, pinned to the recorded engine registry). This mapping is its
#: inverse, grouped per kiro-cli name, plus the rows only the hook vocabulary
#: needs: ``fs_write`` is a KAS id as well as the kiro-cli name (a file create
#: states it on a permission request), and ``web_fetch`` reaches a KAS tool no
#: gate policy names. A matcher in an agent
#: spec's ``hooks`` names tools in kiro-cli's vocabulary (``execute_bash``,
#: ``fs_write``). KAS names its own tools differently, and the name it states for
#: a call is the ``toolId`` of the permission request's ``_meta.kiro``
#: (``AcpEvent.harness_tool_id``): a shell command is ``run_command``, a partial
#: edit is ``str_replace``. Every id here is one a live KAS session (kiro-cli
#: 2.24, ``--agent-engine v3``) stated on a permission request for that kind of
#: call, and each row stays inside the KAS capability its kiro-cli name belongs
#: to in KAS's tool table (``policy/capabilities.ts``), so a row never reaches a
#: tool of a different kind.
#:
#: Coverage is per id, not per capability: a row lists the ids seen for that
#: kind of call, and a KAS id outside every row (a process-control tool) is met
#: only by a matcher that names it as written. A kiro-cli tool with no row
#: (``use_aws``) meets no KAS call. Read it through
#: :func:`kas_tool_match_names` and :data:`KAS_TOOL_MATCH_VOCABULARY`; the rows
#: themselves are for a caller that must go from a kiro-cli name to KAS ids.
_HOOK_ONLY_ROWS: dict[str, tuple[str, ...]] = {
    "web_fetch": ("web_fetch",),
}


def _rows_from_policy_table() -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {"fs_write": ["fs_write"]}
    for kas_id, kiro_cli_name in KIRO_CLI_NAME_BY_KAS_TOOL.items():
        grouped.setdefault(kiro_cli_name, []).append(kas_id)
    rows = {name: tuple(ids) for name, ids in grouped.items()}
    rows.update(_HOOK_ONLY_ROWS)
    return rows


KAS_TOOL_IDS_BY_KIRO_TOOL: dict[str, tuple[str, ...]] = _rows_from_policy_table()


#: Every name a tool matcher can mean on KAS: the kiro-cli names and their
#: aliases, and the KAS ids they reach.
KAS_TOOL_MATCH_VOCABULARY: frozenset[str] = (
    frozenset(KAS_TOOL_IDS_BY_KIRO_TOOL)
    | frozenset(KIRO_TOOL_ALIASES)
    | frozenset(tool_id for ids in KAS_TOOL_IDS_BY_KIRO_TOOL.values() for tool_id in ids)
)


def kas_tool_match_names(tool_id: str) -> tuple[str, ...]:
    """The names a tool matcher is compared with for a KAS call to ``tool_id``.

    First the kiro-cli name whose row reaches the id (the id itself when no row
    does), then the KAS id, then the aliases, with no repeats. So a matcher written
    as ``execute_bash``, ``run_command`` or ``shell`` meets a KAS shell call, and the
    first name is the kiro-cli built-in a spec hook's stdin is told the call is
    (:func:`kiro_crew.agent_sdk.spec_hooks.kiro_cli_hook_payload`). Empty
    for an empty id: a call KAS did not name matches no tool matcher.
    """
    if not tool_id:
        return ()
    kiro_names = sorted(name for name, ids in KAS_TOOL_IDS_BY_KIRO_TOOL.items() if tool_id in ids)
    aliases = sorted(alias for alias, name in KIRO_TOOL_ALIASES.items() if name in kiro_names)
    return tuple(dict.fromkeys([*kiro_names, tool_id, *aliases]))
