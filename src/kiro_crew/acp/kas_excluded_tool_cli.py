"""An agent file that excludes the sub-agent tool, read as kiro-cli reads it.

Kiro Crew gives an entry whose ``excludedTools`` withhold the sub-agent tool the
rule that takes every engine sub-agent away
(``kas_subagent_targets.deny_excluded_subagent_tool``). kiro-cli adds no such
rule: its engine takes away the tools an ``excludedTools`` entry matches by id or
by tag, and no other. With the ``kas_subagents`` preview on, a person's own agent
whose file is one of the two shapes run on kiro-cli 3.5.0 (engine 0.66.119) gets
what kiro-cli gave it:

- ``tools: ["*"]`` excluding ``use_subagent`` and ``invoke_sub_agent``:
  ``invoke_sub_agent`` leaves the agent and its per-agent ``subagent_<id>`` tools
  stay, so it starts a child through one of them;
- ``tools: ["*"]`` excluding ``subagent``: the engine reads that as the tag its
  per-agent tools carry, so those leave and ``invoke_sub_agent`` stays. The
  projection files ``subagent`` under kiro-cli's older ``use_subagent`` spelling
  (``tool_names.KIRO_CLI_TOOL_SPELLINGS``), so the entry's exclusion is put back
  to the tag here.

Every other file keeps the rule (:func:`cli_run_shape` lists what the two shapes
allow beside those names), and so does every agent Kiro Crew writes, every
registered sub-agent target, a remote workspace, and every session the preview
does not reach. What kiro-cli itself showed to remove delegation still removes
it: ``subagent_*`` beside ``invoke_sub_agent`` keeps the rule, and a
``permissions`` deny on ``subagent`` stays the file's own.

shortcut: the ``subagent`` shape is put back after the target projection ran, so
the session-start line naming the agents the engine's roster skips is not added
for it. Move the step before ``project_subagent_targets`` if that line is wanted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kiro_crew.acp import kas_subagent_targets
from kiro_crew.acp.kas_subagent_targets import KAS_SUBAGENT_TOOL, audit_subagent_policy

#: The ``excludedTools`` entries about a sub-agent tool in each of the two files:
#: both spellings of the tool's id, or the engine's tag for its per-agent tools.
_BY_ID = frozenset({"use_subagent", KAS_SUBAGENT_TOOL})
PER_AGENT_TOOLS_TAG = "subagent"
_BY_TAG = frozenset({PER_AGENT_TOOLS_TAG})

#: The tags a server-like ``@name`` entry would select a sub-agent tool by.
_SUBAGENT_TAG_NAMES = frozenset({"builtin", "subagent", "subagent-explicit"})

_KEPT_BY_ID = (
    "ℹ Agent {agent} excludes use_subagent and invoke_sub_agent. As on kiro-cli, "
    "that removes invoke_sub_agent only: the agent can still start a sub-agent "
    "through a subagent_<name> tool. To remove those too, add subagent_* to "
    "excludedTools."
)
_KEPT_BY_TAG = (
    "ℹ Agent {agent} excludes subagent. As on kiro-cli, that removes the "
    "subagent_<name> tools only: the agent can still start a sub-agent through "
    "invoke_sub_agent. To remove that too, add invoke_sub_agent and subagent_* to "
    "excludedTools."
)


def _about_subagents(entry: Any) -> bool | None:
    """Whether one ``excludedTools`` *entry* is one of the names the two files hold.

    ``False`` for an entry that can match no sub-agent tool: an MCP server or one
    of its tools, or any other tool named outright. ``None`` for everything else
    (a ``*`` or ``?`` pattern, a tag, another spelling of the tool, a per-agent
    tool, a value that is not a string): no such file was run on kiro-cli.
    """
    from kiro_crew.platform.tool_names import canonical_kiro_cli_tool_name

    if not isinstance(entry, str) or not entry:
        return None
    if entry in _BY_ID or entry in _BY_TAG:
        return True
    if entry.startswith("@"):
        server = entry[1:].split("/", 1)[0]
        named = server and not set(server) & set("*?") and server not in _SUBAGENT_TAG_NAMES
        return False if named else None
    if set(entry) & set("*?") or entry.startswith("subagent"):
        return None
    return None if canonical_kiro_cli_tool_name(entry) in _BY_ID else False


def cli_run_shape(spec: Any) -> frozenset[str] | None:
    """Which of the two shapes run on kiro-cli the agent file *spec* is, else ``None``.

    :data:`_BY_ID` or :data:`_BY_TAG`: ``tools`` is ``["*"]``, the ``excludedTools``
    entries about a sub-agent tool are exactly that set, every other entry names an
    MCP server, one of its tools or another tool outright, and nothing else in the
    file speaks about sub-agents (no ``toolsSettings`` for the sub-agent tool, no
    ``permissions`` rule on a capability that covers ``subagent``, no
    ``permissions`` shape other than a ``rules`` list).
    """
    from kiro_crew.acp.kas_permissions import META_CAPABILITY_EXPANSION
    from kiro_crew.platform.tools_settings_aliases import canonical_tools_settings

    if not isinstance(spec, dict) or spec.get("tools") != ["*"]:
        return None
    excluded = spec.get("excludedTools")
    if not isinstance(excluded, list):
        return None
    read = [_about_subagents(entry) for entry in excluded]
    if None in read:
        return None
    about = frozenset(entry for entry, is_about in zip(excluded, read) if is_about)
    if about not in (_BY_ID, _BY_TAG):
        return None
    settings, _dropped = canonical_tools_settings(spec.get("toolsSettings"))
    if isinstance(settings, dict) and settings.get("subagent"):
        return None
    permissions = spec.get("permissions")
    if permissions is None:
        return about
    rules = permissions.get("rules") if isinstance(permissions, dict) else None
    if not isinstance(rules, list) or set(permissions) != {"rules"}:
        return None
    for rule in rules:
        capability = rule.get("capability") if isinstance(rule, dict) else None
        if not isinstance(capability, str) or "subagent" in META_CAPABILITY_EXPANSION.get(
            capability, (capability,)
        ):
            return None
    return about


def _persons_own(
    agent: str,
    spec: dict[str, Any],
    spec_dir: Path | None,
    agents_dir: Path,
    work_dir: str | Path | None,
) -> bool:
    """Whether *agent* is a file a person wrote, in their own or the project's agents folder.

    Never one Kiro Crew writes (``default_crew_servers.is_custom_agent``) and never
    a file in the crewmate folder: *spec_dir* is the folder the session's spec was
    read from. Blocking file I/O, as :func:`is_custom_agent`.
    """
    from kiro_crew.acp.default_crew_servers import is_custom_agent
    from kiro_crew.config import paths as paths_mod

    if spec_dir is None or not is_custom_agent(agent, spec):
        return False
    project_dir = paths_mod.project_agents_dir(work_dir) if work_dir else None
    return spec_dir in (agents_dir, project_dir)


def hold_excluded_as_cli(
    batch: list[dict[str, Any]],
    agent: str,
    spec: Any,
    *,
    spec_dir: Path | None,
    agents_dir: Path,
    work_dir: str | Path | None,
    notices: list[str] | None,
) -> list[dict[str, Any]]:
    """*batch* with the sub-agent hold on every entry but a session agent kiro-cli would spare.

    The session's own agent *agent* is spared when it is a person's own file
    (:func:`_persons_own`) in one of the two shapes (:func:`cli_run_shape`); its
    entry is then left as projected, or, for the ``subagent`` shape, excludes the
    tag in place of the tool's id. The change is recorded in the security event log
    and named in *notices*. Every other entry, and every batch with no such agent,
    goes through ``kas_subagent_targets.deny_excluded_subagent_tool`` unchanged.
    """
    hold = kas_subagent_targets.deny_excluded_subagent_tool
    shape = cli_run_shape(spec)
    if shape is None or not _persons_own(agent, spec, spec_dir, agents_dir, work_dir):
        return hold(batch)
    out: list[dict[str, Any]] = []
    for entry in batch:
        if not (isinstance(entry, dict) and entry.get("id") == agent):
            out.extend(hold([entry]))
            continue
        if shape == _BY_TAG:
            kept = [t for t in entry.get("excludedTools") or [] if t not in _BY_ID]
            entry = {**entry, "excludedTools": [*kept, PER_AGENT_TOOLS_TAG]}
        audit_subagent_policy(
            "kas_subagent_exclusion_read_as_cli",
            agent,
            "excludedTools read as kiro-cli reads them, so "
            + ("invoke_sub_agent stays" if shape == _BY_TAG else "the per-agent tools stay"),
        )
        if notices is not None:
            line = _KEPT_BY_TAG if shape == _BY_TAG else _KEPT_BY_ID
            notices.append(line.format(agent=agent))
        out.append(entry)
    return out


__all__ = ["PER_AGENT_TOOLS_TAG", "cli_run_shape", "hold_excluded_as_cli"]
