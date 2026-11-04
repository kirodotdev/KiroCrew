"""The governed skill view kiro-cli runs for a crew-bound shared template.

kiro-cli honours a spec file's ``allowedTools`` and each server's ``autoApprove``
before Kiro Crew's PreToolUse gate, and Kiro Crew never writes a spec it does not
own (the owner decision in docs/decisions). On kiro-cli, though, a
session does not run the authored file: the native skill projection
(``acp.skill_projection``) writes a Crew-owned copy of every agent, its skill view,
and hands kiro-cli that copy's name on ``--agent`` and ``session/set_mode``. The
view is rebuilt from the authored file at every spawn and every ``set_mode``
re-preparation, under a name derived from its content.

So the ceiling is applied to the copy. For every name a crew in ``config.json`` is
bound to (either spelling, see :func:`governed_view_names`), and for an agent a
crewmate ran on before the startup migration moved it, the view loses every grant
the two governance passes remove: the same passes the start check asks about
(``fork_refresh._apply_governance_passes``), over the same predicate the KAS
projection filters with (``platform.governance.may_skip_gate_now``). Only removal
is possible; the authored file is never written. The start check then judges the
view it is handed (``fork_refresh.require_shared_template_within_ceiling`` with
``launch_name``), and still refuses a start that runs the authored file, which is
what happens whenever no view could be prepared.
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

from kiro_crew import agent as agent_mod

#: The SEL operation a withheld grant is recorded under, shared with every other
#: writer that withholds an auto-approval.
WITHHELD_OP = "mcp_auto_approve_withheld"

#: Removals already reported in this process, so a view rebuilt at every spawn is
#: reported once per distinct removal and not once per spawn. Entries are fixed-size
#: digests. Past the bound the memo is cleared and a removal may be reported again.
_REPORTED: set[str] = set()
_REPORTED_MAX = 256
_REPORTED_LOCK = threading.Lock()


def governed_view_names() -> frozenset[str] | None:
    """The agent names whose skill view the ceiling is applied to, or ``None`` for all.

    Every ``kiro_agent`` a crew binding records, both as written and as the name
    kiro-cli is dispatched under (``dispatch_kiro_agent``), and every agent a crewmate
    ran on before the startup migration moved it: the names the kiro-cli start check
    governs. ``None`` when the set cannot be known whole (a config file the load
    could not read, an unreadable migration ledger, any other failure): every view is
    then governed, the fail-closed answer, since only removal is possible.
    """
    try:
        from kiro_crew.agent_materialization import fork_refresh
        from kiro_crew.config.loader import KiroCrewConfig, dispatch_kiro_agent
        from kiro_crew.mate_config_migration import LedgerUnreadable, moved_from_agents

        cfg = KiroCrewConfig.load()
        if fork_refresh._bindings_may_be_incomplete(cfg):
            return None
        agents = cfg.agents if isinstance(cfg.agents, dict) else {}
        stored = {
            name
            for name in (getattr(binding, "kiro_agent", None) for binding in agents.values())
            if isinstance(name, str) and name
        }
        try:
            stored.update(m for m in moved_from_agents() if isinstance(m, str) and m)
        except LedgerUnreadable:
            return None
        names: set[str] = set()
        for name in stored:
            names.add(name)
            names.add(dispatch_kiro_agent(name))
        return frozenset(names)
    except Exception:  # noqa: BLE001 -- an unknown set governs every view
        agent_mod.logger.warning(
            "shared template views: crew bindings could not be read; the governance "
            "ceiling is applied to every skill view",
            exc_info=True,
        )
        return None


def govern_shared_template_injected_servers(
    agent: str | None, servers: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Remove denied auto-approvals from injected servers for a governed *agent*.

    Session-level entries outrank the same-named entries in the governed skill
    view, so they receive the same auto-approval pass before reaching kiro-cli.
    Unbound agents keep their authored entries unchanged. An unknown binding set
    governs every named agent, matching :func:`governed_view_names`.
    """
    if not agent:
        return servers
    governed = governed_view_names()
    if governed is not None and agent not in governed:
        return servers

    from kiro_crew.agent_materialization import auto_approve, fork_refresh

    by_name = {
        str(entry["name"]): entry
        for entry in servers
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }
    before = fork_refresh._spec_grants({"mcpServers": by_name})
    # _report deduplicates the audit; keep the strip quiet to avoid a second record.
    filtered = auto_approve._strip_ungoverned_auto_approve(by_name, audit=False)
    removed = frozenset(before - fork_refresh._spec_grants({"mcpServers": filtered}))
    if removed:
        _report(agent, removed)
    return [
        filtered.get(str(entry.get("name")), entry) if isinstance(entry, dict) else entry
        for entry in servers
    ]


def govern_shared_template_view(agent: str, view: dict[str, Any]) -> frozenset[tuple[str, str]]:
    """Remove from *view*, in place, every grant the governance ceiling withholds.

    *view* is a Crew-owned skill view of *agent*'s spec, never the authored file.
    Returns the grants removed (``(list, entry)`` pairs, see
    ``fork_refresh._spec_grants``), and reports a removal once per process
    (:func:`_report`).
    """
    from kiro_crew.agent_materialization import fork_refresh

    before = fork_refresh._spec_grants(view)
    # Audited by _report below, once per distinct removal, not by the passes on
    # every spawn that rebuilds the same view.
    fork_refresh._apply_governance_passes(view, source=f"shared-template-view:{agent}", audit=False)
    removed = frozenset(before - fork_refresh._spec_grants(view))
    if removed:
        _report(agent, removed)
    return removed


def _report(agent: str, removed: frozenset[tuple[str, str]]) -> None:
    """Say which grants *agent*'s kiro-cli view does not carry, and why. Best-effort."""
    canonical = json.dumps(
        [agent, sorted(removed)], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    key = hashlib.sha256(canonical).hexdigest()
    with _REPORTED_LOCK:
        if key in _REPORTED:
            return
        if len(_REPORTED) >= _REPORTED_MAX:
            _REPORTED.clear()
        _REPORTED.add(key)
    entries = ", ".join(
        entry if where == "allowedTools" else f"{where} autoApprove {entry}"
        for where, entry in sorted(removed)
    )
    message = (
        f"agent {agent!r}: kiro-cli runs a Kiro Crew copy of its spec without these "
        f"auto-approvals, which the governance ceiling denies: {entries}. The spec file "
        "itself is unchanged; the tools stay mounted and their calls ask for approval."
    )
    agent_mod.logger.warning("%s", message)
    try:
        agent_mod.sel().log_api_access(
            caller="system",
            operation=WITHHELD_OP,
            outcome="ok",
            source=f"shared-template-view:{agent}",
            resources=message,
        )
    except Exception:  # noqa: BLE001 -- the audit must not break the projection
        agent_mod.logger.debug("SEL audit unavailable for a shared template view", exc_info=True)
