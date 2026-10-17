"""Crew Mode's routing tools: ``route_crew`` ranks the configured crews for a task
and ``select_crew`` returns the roster or binds one crew to the calling session.

:mod:`kiro_crew.mcp_core` stays the import path and the patch surface; see
:mod:`kiro_crew.mcp_core_parts`.
"""

from __future__ import annotations

import json
from typing import Any

from kiro_crew.config.loader import KiroCrewConfig, resolve_agent_bindings
from kiro_crew.history import ConversationLog
from kiro_crew.mcp_core_parts.identity import _resolve_session_key
from kiro_crew.members import record_activity
from kiro_crew.memory_stores import UnknownMemoryStore
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.security import redact_local_paths
from kiro_crew.trigger_match import rank_triggered


def _crew_memory_unavailable_reason(error: UnknownMemoryStore) -> str:
    return redact_local_paths(redact(str(error)))[0][:1000]


def _with_display_name(entry: dict[str, Any], key: str, agent: Any) -> dict[str, Any]:
    """Add the crew's dashboard label to a routing entry when it has one.

    The user names a crew by the label the dashboard shows; the model must
    dispatch by the key. Without the label beside the key, a crew whose label
    differs from its id cannot be matched to the user's words.
    """
    label = (getattr(agent, "display_name", "") or "").strip()
    if label and label != key:
        entry["display_name"] = label
    return entry


def _key_with_label(key: str, label: str | None) -> str:
    """Render one ``available`` item: the key, then its label when it has one."""
    return f"{key} ({label})" if label else key


def _do_route_crew(task: str) -> str:
    """Rank the crews whose triggers match *task* (the route_crew tool body).

    The SCORED half of routing, next to ``select_crew``'s roster. Both exist
    because they answer different questions: the roster asks the model to judge,
    which is right when the task is prose and the crews are described in prose;
    this ranks the same triggers mechanically, which is right when a caller wants
    the same task to reach the same crew every time.

    Shares ``trigger_match`` with the skills loader rather than scoring its own
    way, so "this phrasing matches" cannot mean two things in one product. A
    crew with no triggers is not a candidate — that is the operator's opt-out,
    and it is the same rule the roster applies. The roster's default crewmate
    (``config.default_agent``) is left out by RFC rule 5
    (``docs/request-for-change/rfc-session-template-vs-crewmate.md``): it is the
    one ``select_crew`` keeps for itself, the standing fallback the guidance
    names, not a hand-off target -- a routing rule, since the calling session is
    a template session rather than that crewmate.

    Reports rather than binds. Binding happens where a run is created
    (``spawn_run(crew=...)``), because that is the only place the decision can be
    honoured on both halves the caller cares about, memory and template.
    """
    if not task or not task.strip():
        return json.dumps({"error": "task must be a non-empty string"}, ensure_ascii=False)
    cfg = KiroCrewConfig.load()
    default = cfg.default_agent
    candidates = [(n, c.triggers) for n, c in cfg.agents.items() if n != default]
    ranked = rank_triggered(task, candidates)
    matches = []
    unavailable = []
    for name, score in ranked:
        try:
            binding = resolve_agent_bindings(cfg, name, validate_memory_files=False)
        except UnknownMemoryStore as exc:
            unavailable.append(
                _with_display_name(
                    {"crew": name, "reason": _crew_memory_unavailable_reason(exc)},
                    name,
                    cfg.agents[name],
                )
            )
            continue
        matches.append(
            _with_display_name(
                {
                    "crew": name,
                    "score": round(score, 3),
                    "description": (cfg.agents[name].description or "").strip(),
                    "memory_store": binding.memory_store_name,
                },
                name,
                cfg.agents[name],
            )
        )
    return json.dumps(
        {
            "task": task[:200],
            "default_agent": default,
            "matches": matches,
            "unavailable": unavailable,
            "guidance": (
                "Ranked by trigger overlap, best first. If both matches and "
                "unavailable are empty, no crew claims this task -- handle it "
                "on the default crew rather than picking the least-bad match. "
                "Unavailable crews matched but their memory binding was refused: "
                "report that refusal; do not substitute Global memory for them. "
                "To act on a healthy match, spawn with "
                "crew=<name>: that is what gives the run that crew's memory and "
                "template, and what keeps another crew's memory out of it."
            ),
        },
        ensure_ascii=False,
    )


def _do_select_crew(crew: str) -> str:
    """Orchestrator crew routing (the select_crew tool body).

    Empty ``crew`` → JSON roster of *selectable* crews: those with a non-empty
    ``triggers`` (a crew with no triggers is not a routing candidate at all),
    excluding the roster's default crewmate: RFC rule 5 keeps it as the one
    ``select_crew`` holds for itself, the standing fallback the guidance names.
    That is a routing rule, not an identity -- the calling session is a template
    session (the default custom agent), not that crewmate. The response also
    carries ``default_agent`` and explicit guidance so the model selects only on
    a high-confidence match and otherwise falls back to the default crew. A named
    crew → validate it exists, resolve its bindings, and return the bound
    {workspace, memory_store, kiro_agent, model}. An unknown name returns a JSON
    ``error`` with the available names.
    """
    cfg = KiroCrewConfig.load()
    default = cfg.default_agent
    if not crew:
        roster = [
            _with_display_name({"name": n, "triggers": c.triggers}, n, c)
            for n, c in cfg.agents.items()
            if n != default and c.triggers.strip()
        ]
        return json.dumps(
            {
                "default_agent": default,
                "crews": roster,
                "guidance": (
                    "Select a crew ONLY when its triggers clearly and specifically "
                    "match the task with high confidence. If no crew is a strong "
                    "match (or the list is empty), do NOT route — use the default "
                    "crew. Crews without triggers are intentionally omitted. "
                    "The user may refer to a crew by its display_name; always "
                    "pass its name to spawn_run(crew=...)."
                ),
            },
            ensure_ascii=False,
        )
    if crew not in cfg.agents:
        available = (
            ", ".join(
                _key_with_label(k, _with_display_name({}, k, cfg.agents[k]).get("display_name"))
                for k in sorted(cfg.agents)
            )
            or "(none)"
        )
        return json.dumps(
            {"error": f"unknown crew '{crew}'", "available": available},
            ensure_ascii=False,
        )
    try:
        b = resolve_agent_bindings(cfg, crew, validate_memory_files=False)
    except UnknownMemoryStore as exc:
        return json.dumps(
            {"crew": crew, "error": _crew_memory_unavailable_reason(exc)},
            ensure_ascii=False,
        )
    # Routing-decision pointer. This is the one place a member's identity is
    # unambiguous on the delegation path: `spawn_run`'s `agent` is validated
    # against the installed TEMPLATES, so by the time a sub-agent starts the
    # member it came from cannot be recovered (two members may share one
    # template). Recording at bind time sidesteps that.
    #
    # It records the DECISION, not an execution — binding a crew does not oblige
    # the model to delegate to it. That is the signal trigger generation wants
    # (what the router believes belongs to whom), but it means these entries are
    # intent. A `via="spawn"` execution entry is deliberately left for when the
    # spawn path carries member identity.
    #
    # The caller's memory mode is resolved HERE rather than defaulted: this log
    # outlives session pruning, so recording a no-trace session's key would
    # durably defeat the mode. An unreadable session degrades to the private
    # spelling so the failure mode is "skip the entry", never "log a private
    # session". No try/except around the write itself: record_activity is total
    # and reports failure by returning False, which matters because this module
    # keeps no logger.
    _sk = _resolve_session_key()
    try:
        _mode = str(ConversationLog().get_metadata(_sk).get("memory_mode", "") or "")
    except Exception:
        _mode = "incognito"
    record_activity(crew, _sk, _mode, via="select_crew")
    return json.dumps(
        {
            "crew": crew,
            "bound": {
                "kiro_agent": b.kiro_agent,
                "workspace": str(b.workspace_dir),
                "memory_store": b.memory_store_name,
                "model": cfg.agents[crew].model,
            },
        },
        ensure_ascii=False,
    )
