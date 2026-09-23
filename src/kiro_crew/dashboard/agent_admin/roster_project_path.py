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
from typing import TYPE_CHECKING, NoReturn

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.agents import (
        DashboardState,
        Path,
        _read_session_key,
        _resolve_roster_project_path,
        _sel,
        active_project_dir,
        json,
        logger,
        supports_pinned_walk,
    )


async def _roster_project_dir_for_request(
    request: "web.Request", state: "DashboardState | None", redact: bool, operation: str
) -> "tuple[str | Path | None, bool]":
    """Decide which directory ``api_kirocrew_agents`` scans for project rows.

    Returns ``(project_dir, owner_supplied_path)``.

    An explicit ``?project_path=`` overrides the slot-derived project. It is the
    ONLY way a surface with no chat slot yet -- the folder create/settings
    modal, whose project directory is a draft the user is still typing -- can
    scope the roster to that directory. Without it that modal falls back to the
    cross-slot ``active_project_dir``, which is never the folder's own
    directory, so its "default agent" picker can never list project agents.

    The param lets the caller point the scan at an ARBITRARY directory, so it is
    owner-only. ``redact`` is already True for any non-owner caller (app token,
    or an allow-listed messaging user holding a dashboard token); honoring the
    param for them would let a non-owner enumerate agent names under any
    readable directory. Resolution and SEL audit of either branch
    (owner-honored or non-owner-refused) are
    :func:`_resolve_audited_roster_project_dir` above.

    The active chat slot's project is used ONLY when NO ``?project_path=`` was
    supplied at all. When one WAS supplied -- whether honored (owner) or refused
    (non-owner, audited as "denied") -- that request asked for a specific
    directory's roster, so an unresolved or refused path ships GLOBAL rows only
    rather than silently substituting the active slot's project agents. A
    non-owner's ``?project_path=B`` request therefore takes the same GLOBAL-only
    path, so chat slot A's project roster is never leaked to a caller who asked
    about an unrelated project B.

    The slot-derived path is not caller-supplied, so it never goes through the
    resolver; ``project_agent_names`` fences it exactly as before. Runs on the
    handlers module's globals per this module's composition contract, so
    ``active_project_dir`` and ``_read_session_key`` are read by name.
    """
    raw_project_path = request.query.get("project_path", "").strip()
    owner_supplied_path = bool(raw_project_path) and not redact
    project_dir: "str | Path | None" = await _resolve_audited_roster_project_dir(
        request, raw_project_path, owner_supplied_path, operation=operation
    )
    if not project_dir and not raw_project_path:
        project_dir = active_project_dir(state, _read_session_key(request)) if state else ""
    return project_dir, owner_supplied_path


def _refuse_unverifiable_roster_scan() -> NoReturn:
    """Answer an owner-supplied project scan that could not be run or trusted.

    Never read a refusal as "no agents in this project". Same 503 shape as the
    SEL audit-write failure in the caller, and for the identical reason: the
    picker's ErrorNotice + Retry state is what a caller-visible "could not
    verify" belongs in, not a 200 with an empty roster indistinguishable from a
    project that genuinely declares none.

    Two distinct shapes ride the same 503. A platform that can never pin a
    directory descriptor (``supports_pinned_walk()`` is False) is a PERMANENT
    limitation -- no retry of this same request can ever succeed, so the
    response names it ``code: "scan_unsupported_platform"`` and the modal must
    not offer a Retry for it. Every other cause (a genuine pin failure, an
    audit-write failure, an entry-count overflow) is a transient refusal that a
    retry can clear, so it keeps the pre-existing unlabeled shape the modal's
    ordinary Retry state already handles.

    Runs on the handlers module's globals per this module's composition
    contract, so ``json`` and ``supports_pinned_walk`` are read by name.
    """
    if not supports_pinned_walk():
        raise web.HTTPServiceUnavailable(
            text=json.dumps(
                {
                    "error": "folder-level project-agent selection is not "
                    "supported on this platform",
                    "code": "scan_unsupported_platform",
                }
            ),
            content_type="application/json",
        )
    raise web.HTTPServiceUnavailable(reason="project roster scan could not be verified")


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
