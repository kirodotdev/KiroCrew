"""Private owner composed by :mod:`kiro_crew.dashboard.handlers.agents`.

See ``kiro_crew.dashboard.agent_admin`` for the composition contract: this
module's one function runs on the handlers module's globals, so it reads
``asyncio``, ``logger``, ``_sel`` and ``_resolve_roster_project_path`` by name
rather than importing them, and a patch of ``agents._sel`` or
``agents._resolve_roster_project_path`` reaches it the same way it reaches
``api_kirocrew_agents`` itself. The caller passes its own audited-operation
name literal rather than this module holding one, so that construct's one
appearance stays in the facade file for the guard that reads it there
(``test_a_construct_a_guard_reads_in_the_facade_stays_there``).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.agents import _resolve_roster_project_path, _sel, logger


async def _resolve_audited_roster_project_dir(
    request: "web.Request", raw_project_path: str, owner_supplied_path: bool, operation: str
) -> str:
    """``?project_path=`` resolution for ``api_kirocrew_agents``, SEL-audited.

    Returns the directory to scan for project agents (``""`` when none applies
    for this request's ``?project_path=`` branch — the caller falls back to the
    active chat slot's project only when ``raw_project_path`` was empty).

    ``owner_supplied_path`` is true only for an owner caller carrying a
    non-empty ``raw_project_path``: that is the one case honored, resolved and
    audited on its TRUE outcome below. A non-owner (``redact``) carrying the
    param is refused — the param is ignored and audited as a refusal — never
    honored, so the ``elif raw_project_path:`` branch covers that caller.
    ``operation`` names the audited operation; the caller passes its own
    ``"api_kirocrew_agents"`` literal so that construct's one appearance stays
    in the facade file, where a repository guard reads it.
    """
    project_dir = ""
    if owner_supplied_path:
        resolved_path, denied = await asyncio.to_thread(
            _resolve_roster_project_path, raw_project_path
        )
        # An owner directing the scan at an arbitrary directory is a
        # security-relevant action, audited on its TRUE outcome:
        #   - denied  -> a sensitive path was refused;
        #   - allowed -> a real directory was resolved and will be scanned.
        # A non-existent / non-directory path resolves to ("", False): nothing
        # is scanned, so it is neither an allowed scan nor a refusal and emits no
        # event — logging "allowed" there would record a scan that never ran.
        outcome = "denied" if denied else ("allowed" if resolved_path else None)
        if outcome is not None:
            try:
                # A synchronous critical write does filesystem I/O AND `_sel()`
                # itself may initialise SEL (also filesystem I/O) on first use, so
                # BOTH the lookup and the write run OFF the event loop inside one
                # to_thread lambda. to_thread re-raises in the awaiting task, so
                # the fail-closed branch still fires. `critical=True` makes the
                # write synchronous/re-raising (the queued form swallows failures,
                # leaving fail-closed unreachable); `resources` records WHICH dir
                # was scanned/refused. A denied event stays best-effort.
                _outcome = outcome
                # The tree actually enumerated, not the request spelling: for a
                # path like `~/link-into-elsewhere` the two differ, and an
                # `allowed` row naming the spelling would record a scan of a
                # directory that was never walked. `resolved_path` is non-empty
                # on every `allowed` outcome by the test above, so the fallback
                # only serves `denied`, where nothing resolved and the spelling
                # is the only thing there is to name.
                _res = resolved_path or raw_project_path
                _caller = request.get("user", "dashboard")
                await asyncio.to_thread(
                    lambda: _sel().log_api_access(
                        caller=_caller,
                        operation=operation,
                        outcome=_outcome,
                        source="dashboard",
                        resources=_res,
                        critical=(_outcome == "allowed"),
                    )
                )
            except Exception:
                logger.warning("SEL logging failed for owner project_path scan", exc_info=True)
                if outcome == "allowed":
                    # FAIL CLOSED: an "allowed" event means the scan is about to
                    # READ a caller-supplied directory — a security-relevant act
                    # whose whole point is that it leaves an audit trail. The
                    # critical write above re-raises on failure, so we land here
                    # and the scan does not happen.
                    #
                    # Answered as an ERROR, not as a 200 carrying global rows
                    # only. That shape is indistinguishable from the bug this
                    # endpoint exists to fix -- the caller cannot tell "this
                    # directory declares no agents" from "the scan was
                    # refused", so a wedged audit sink silently empties the
                    # picker, and can mark a perfectly valid project agent as
                    # absent from its own directory and block Save, with
                    # nothing but a server-side log to explain it. 503 lets the
                    # client render the refusal it already has a state for
                    # (ErrorNotice + Retry) and makes the retry meaningful,
                    # since the condition is transient by nature.
                    raise web.HTTPServiceUnavailable(
                        reason="project roster scan refused: audit write failed"
                    )
        project_dir = resolved_path
    elif raw_project_path:
        # A NON-OWNER (redact) carrying ?project_path= is refused: the param is
        # ignored and the roster answers with GLOBAL agents only -- no project
        # scope is substituted, since falling back to the slot's project would
        # still disclose project agent names this caller is not entitled to.
        # That refusal is itself a security-relevant event — a non-owner
        # attempting to point the roster scan at an arbitrary directory — so it
        # is audited too. Without this, the audit trail records owner scans
        # (allowed/denied) but stays silent on every non-owner attempt, which is
        # the exact gap the anchor backend-security-controls protects.
        try:
            _caller_no = request.get("user", "dashboard")
            _res_no = raw_project_path
            await asyncio.to_thread(
                lambda: _sel().log_api_access(
                    caller=_caller_no,
                    operation=operation,
                    outcome="denied",
                    source="dashboard",
                    resources=_res_no,
                )
            )
        except Exception:
            logger.warning(
                "SEL logging failed for non-owner project_path attempt",
                exc_info=True,
            )
    return project_dir
