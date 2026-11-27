"""The gate's ``spec-parent-limits`` row: a sub-agent run is refused what its parents are.

``ToolCall.spec_parent_limits`` holds one
:class:`~kiro_crew.platform.spec_denied_grant.SpecParentLimits` per agent above a
KAS sub-agent run. :func:`refusal` judges the call under each with the gate's own
limit rows (``spec-shell-allowlist``, ``spec-write-allowlist``, ``spec-denies``,
``spec-unjudged-denies``), so a parent's limit reads exactly as the same limit
reads on the parent's own call. It has no matcher of its own and never grants.

Where a parent's denies cannot decide the call, that parent's own call asks a
person: its spec's grants wait on a deny check that answers a clear no
(``spec_denied_gate.denied_grant_applies``). :func:`withhold_undecided` holds the
run to the same answer by taking the run's own spec grants off the call the later
rows judge, so no grant row answers it and it asks too.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

from kiro_crew.platform import allowed_commands
from kiro_crew.platform.spec_denied_gate import (
    _SHELL_NAMES,
    _WEB_FETCH,
    _is_write,
    _write_denied,
    web_fetch_blocked_match,
)
from kiro_crew.platform.spec_denied_grant import denies_only

#: What the limit rows call the spec they refuse by.
_OWN = "this agent's spec"


def refusal(facts: Any, tier: Any, rows: Sequence[Callable[[Any, Any], Any]]) -> Any:
    """The first refusal any parent record gives *facts*' call, or ``None``.

    Each record is judged on a copy of the call that carries the record's limits, in
    place of the run's own, in the fields *rows* read. The refusal names the agent
    whose spec refused.
    """
    call = facts.call
    for limits in call.spec_parent_limits:
        # A shallow copy: what the facts memoize is derived from the call's title,
        # command and arguments, none of which the copy below changes.
        held = copy.copy(facts)
        held.call = replace(
            call,
            spec_shell_allowlist=limits.shell_allowlist,
            spec_shell_readonly=limits.shell_readonly,
            spec_write_allowlist=limits.write_allowlist,
            spec_denied_grant=limits.denies,
            spec_unjudged_denies=limits.unjudged_denies,
            spec_parent_limits=(),
        )
        for row in rows:
            verdict = row(held, tier)
            if verdict is not None:
                whose = f"the spec of agent {limits.agent!r}, under which this sub-agent runs"
                return replace(verdict, reason=verdict.reason.replace(_OWN, whose, 1))
    return None


def _undecided(call: Any, names: tuple[str, ...], limits: Any) -> bool:
    """Whether the agent of *limits* would be asked about its own call of this shape.

    The checks its own grant waits on (``spec_denied_gate.denied_grant_applies``):
    a shell command its ``deniedCommands`` do not clear, a write its ``deniedPaths``
    do not clear, a fetch its ``web_fetch.blocked`` entries do not clear, and any
    call of a tool whose deny list Kiro Crew cannot read. A call no deny of that
    agent reaches is decided.
    """
    if call.mcp_server:
        return False
    record = limits.denies
    if _SHELL_NAMES.intersection(names):
        if "execute_bash" in limits.unread_denies:
            return True
        if record is not None and record.denied_commands:
            clear = allowed_commands.denied_match(record.denied_commands, call.command) is False
            if not (call.is_shell and clear):
                return True
    if _is_write(call, names):
        if "fs_write" in limits.unread_denies:
            return True
        if record is not None and record.denied_write_paths:
            if _write_denied(call, record) is not False:
                return True
    if _WEB_FETCH in names:
        if _WEB_FETCH in limits.unread_denies:
            return True
        if record is not None and record.web_fetch_blocked:
            if not record.web_fetch_blocked_complete:
                return True
            return web_fetch_blocked_match(call, record) is not False
    return False


def withhold_undecided(facts: Any, names: tuple[str, ...]) -> None:
    """Take the run's own spec grants off *facts*' call when a parent cannot decide it.

    Called after :func:`refusal` found no refusal. The rows that follow then see a
    call with no ``spec_granted_*`` field and a deny record with no grant, so the
    call asks unless a row that does not read the run's spec answers it (an
    operator grant, the read-only classifier), as it would for the parent.
    """
    call = facts.call
    for limits in call.spec_parent_limits:
        if _undecided(call, names, limits):
            facts.call = replace(
                call,
                spec_granted_builtins=frozenset(),
                spec_granted_commands=(),
                spec_granted_paths=(),
                spec_granted_read_paths=(),
                spec_denied_grant=denies_only(call.spec_denied_grant),
            )
            facts.ask_note = (
                f"Kiro Crew cannot check this call against the denies of agent "
                f"{limits.agent!r}, under which this sub-agent runs, so it asks."
            )
            return
