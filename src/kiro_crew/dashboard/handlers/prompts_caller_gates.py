"""Who may read or change a prompt or a skill, and how a refusal is audited.

The owner check every mutating skill route runs, the refusal of an app's token
reaching another app's skill slot, the project a requesting chat names for its
local prompts, the prompt-write refusals and the audit records they and refused
reads leave.

Served through :mod:`kiro_crew.dashboard.handlers.prompts`, which callers and tests import and patch; this module
never imports it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.slot_ownership import slot_not_found
from kiro_crew.dashboard.state import DashboardState

from ._shared import active_project_dir, requesting_slot_project

logger = logging.getLogger("kiro_crew.dashboard.handlers.prompts")


_CODE_DASHBOARD_OWNER_REQUIRED = "dashboard_owner_required"


#: The literal the dashboard's browser client sends as ``X-Session-Key`` on every
#: request that has no chat to name (``website/src/api/client.ts``). It marks the
#: SURFACE, so it must never be read as a slot key: the slot-name split turns it
#: into ``ui``, and a chat literally named ``ui`` is a name a user can pick — that
#: chat's project would then drive every settings-page create, update and delete.
#: Several handlers already refuse it by this literal (artifacts, cron,
#: ``_shared``'s restricted-session predicates); it is spelled here rather than
#: imported so this module owns the value it compares.
_DASHBOARD_SURFACE_KEY = "dashboard:ui"


def _deny_non_owner_skill_operation(request: web.Request, operation: str) -> web.Response | None:
    """Restrict owner-only skill state to the configured dashboard owner.

    Covers the project-skill consent endpoints and every mutating skill
    handler: CRUD writes, pending approve/dismiss/dismiss-all, pin,
    inject-on-trigger, and the registry install in ``discover.py``. Skill
    content is injected into agent context, so any skill mutation is an
    instruction-injection surface: only the dashboard owner may perform it.
    ``is_owner_dashboard_request`` already refuses app tokens (any non-empty
    app identity) and non-owner dashboard subjects, and both outcomes are
    SEL-audited here.
    """
    if is_owner_dashboard_request(request):
        try:
            _sel().log_api_access(
                caller=str(request.get("user") or request.get("app") or "unknown"),
                operation=operation,
                outcome="allowed",
                source="dashboard",
            )
        except Exception:  # noqa: BLE001 — preserve authorized access if SEL is unwritable
            logger.debug("Could not audit allowed project-skill trust access", exc_info=True)
        return None
    try:
        _sel().log_api_access(
            caller=str(request.get("user") or request.get("app") or "unknown"),
            operation=operation,
            outcome="denied",
            source="dashboard",
            error="dashboard owner required",
        )
    except Exception:  # noqa: BLE001 — preserve the denial response if SEL is unwritable
        logger.debug("Could not audit denied project-skill trust access", exc_info=True)
    return web.json_response(
        {
            "error": "dashboard owner required",
            "code": _CODE_DASHBOARD_OWNER_REQUIRED,
        },
        status=403,
    )


def _named_slot(state: DashboardState, session_key: str) -> Any | None:
    """The chat slot *session_key* names, or ``None``.

    The slot name is the part after the transport prefix, the same split
    ``requesting_slot_project`` applies — shared so the app-isolation checks that
    read the slot's owning app and that resolver can never disagree about which
    slot a key selected.
    """
    slot_name = session_key.split(":", 1)[-1] if session_key else ""
    if not slot_name:
        return None
    slots = getattr(state, "_slots", {}) or {}
    return slots.get(slot_name)


def _deny_foreign_app_skill_slot(
    request: web.Request,
    state: DashboardState,
    session_key: str,
    operation: str,
) -> web.Response | None:
    """Require an app caller to own a project-bound slot selected by its header.

    Dashboard requests have owner-wide visibility. An app permission only opens
    the endpoint; it does not let the app select a foreign or unscoped slot and
    use another slot's project as a metadata/read oracle. Missing, projectless,
    and foreign slots return 404 so the isolation check does not enumerate slot
    identities or project bindings.
    """
    request_app = request.get("app", "")
    if not request_app:
        return None
    slot_name = session_key.split(":", 1)[-1] if session_key else ""
    slot = _named_slot(state, session_key)
    owner = getattr(slot, "_app", "") if slot is not None else ""
    if owner == request_app and requesting_slot_project(state, session_key) is not None:
        try:
            _sel().log_api_access(
                caller=request_app,
                operation=operation,
                outcome="allowed",
                source="app_isolation",
                resources=f"slot={slot_name}",
            )
        except Exception:  # noqa: BLE001 — preserve authorized access if SEL is unwritable
            logger.debug("Could not audit allowed app skill access", exc_info=True)
        return None
    if slot is None:
        reason = "slot not found"
    elif owner == request_app:
        reason = "owned slot has no project"
    elif owner:
        reason = "app does not own this slot"
    else:
        reason = "app cannot access unscoped slots"
    try:
        _sel().log_api_access(
            caller=request_app,
            operation=operation,
            outcome="denied",
            source="app_isolation",
            resources=f"slot={slot_name}",
            error=reason,
        )
    except Exception:  # noqa: BLE001 — preserve the anti-enumeration response
        logger.debug("Could not audit denied app skill access", exc_info=True)
    return slot_not_found()


def _sel():
    """Late-binding sel() — allows monkeypatching at parent package level."""
    import kiro_crew.dashboard.handlers as _pkg

    return _pkg.sel()


def _prompt_local_project(
    request: web.Request, state: DashboardState, session_key: str
) -> Path | None:
    """The project whose ``.kiro/prompts`` this request's ``local`` scope names.

    Every prompt surface routes "This project" through here, so create, list,
    read, update and delete cannot disagree about where "local" is for one
    request. ``None`` means there is no local directory for this caller, which
    flows into the existing ``no_active_project`` contract on the write paths
    and simply omits ``source: "local"`` entries from a listing.

    Which question to ask depends on whether the request names a real chat, and
    the two cases genuinely differ:

    * A request whose ``X-Session-Key`` names an **existing slot** is speaking for
      one chat, so it gets :func:`requesting_slot_project` — that slot's project
      or nothing. This is the answer ``chat_runner`` reaches by reading
      ``slot.project`` when it expands an ``@mention``, so a prompt the chat
      surface will match is exactly the one this surface offers, and a chat with
      no project of its own is told so rather than shown a neighbour's checkout.
    * A request that names **no slot** is a global surface, and gets
      :func:`active_project_dir`'s fallback: the single project every open slot
      shares. That fallback is load-bearing, not laxity — the only surfaces that
      offer this scope (the overview Prompts tab and the command palette) sit
      outside any chat and have nothing to name, so the strict resolver would
      answer ``None`` for every request they can make and "This project" would be
      permanently dead there. Two chats on different projects still resolve
      ``None``: a settings page has no defensible answer then, and guessing would
      create, overwrite or delete in the wrong checkout.

    An **app** request gets no fallback and no foreign slot. App-token grants
    are path-only, so an app permitted to READ ``/api/prompts`` could otherwise
    name any slot in a forged ``X-Session-Key`` and use another slot's project
    as a prompt-content oracle; the shared-project fallback would hand it one
    without even a header. It resolves strictly per-slot and only for a slot it
    owns, so a mismatch narrows the answer to ``None`` — the app keeps the
    package SOPs and global prompts its grant already covered, and gains no
    local ones. Narrowing rather than refusing is deliberate: the endpoint's
    other content is not slot-scoped, so a 404 would withdraw a capability the
    grant does cover. Both outcomes are SEL-audited under one
    ``operation``/``source`` pair, so an app request leaves exactly one
    attributable line whether the selection was granted or refused — a forged key
    is visible, and so is which project a granted selection actually served.

    The ``dashboard:ui`` placeholder is folded to "no key" before any of that.
    It is what the browser sends when it has no chat to name, and the slot-name
    split would otherwise turn it into ``ui`` — so a user whose own chat is named
    ``ui`` would have that chat's checkout selected by every settings-page
    request, and a create, update or delete aimed at "This project" would land in
    it. Folded, those requests take the slotless path they belong on.
    """
    if session_key == _DASHBOARD_SURFACE_KEY:
        session_key = ""
    request_app = request.get("app", "")
    slot = _named_slot(state, session_key)
    if not request_app:
        if slot is not None:
            return requesting_slot_project(state, session_key)
        # No slot named, so active_project_dir's step 1 cannot fire either; what
        # is wanted from it here is only the shared-project step.
        return active_project_dir(state, session_key)
    slot_name = session_key.split(":", 1)[-1] if session_key else ""
    if slot is not None and getattr(slot, "_app", "") == request_app:
        # Audit the GRANT, not only the refusal. The ownership test is the whole
        # authorization for an app reading another principal's checkout through
        # this endpoint, so a log carrying only refusals cannot answer which
        # project an app was actually served — the reconstruction an operator
        # needs after a compromised app, where every selection succeeded. Same
        # operation/source as the denial below, so one app request leaves exactly
        # one attributable line either way; same shape as
        # ``_deny_foreign_app_skill_slot``'s allowed event on the skills surface.
        try:
            _sel().log_api_access(
                caller=request_app,
                operation="prompt_local_project",
                outcome="allowed",
                source="app_isolation",
                resources=f"slot={slot_name}",
            )
        except Exception:  # noqa: BLE001 — preserve authorized access if SEL is unwritable
            logger.debug("Could not audit allowed app prompt-slot selection", exc_info=True)
        return requesting_slot_project(state, session_key)
    try:
        _sel().log_api_access(
            caller=request_app,
            operation="prompt_local_project",
            outcome="denied",
            source="app_isolation",
            resources=f"slot={slot_name}",
            error="slot not found" if slot is None else "app does not own this slot",
        )
    except Exception:  # noqa: BLE001 — a narrowed answer must survive an unwritable SEL
        logger.debug("Could not audit denied app prompt-slot selection", exc_info=True)
    return None


def _audit_unread(tool_name: str, tool_kind: str, outcome: str, metadata: dict) -> None:
    """SEL-record content a read gate would not hand back.

    A silent fallback is what makes a planted alias invisible: the surface keeps
    answering — an entry with no description, a 404 — so nothing above it can see
    that a read was refused rather than merely empty. SEL is operator-side and
    unreachable through the endpoint, so recording the event here leaves the HTTP
    response no more of an oracle than it already was.

    *outcome* is coarse on purpose, and the coarseness is the gate's rather than
    a choice: ``blocked`` is the one cause that is knowable, because
    ``validate_file_path`` rejected the name outright before any open. Everything
    else collapses into a bare ``None`` from one descriptor — a refused inode
    (hardlinked, non-regular, escaped its parent) and an ordinary read failure
    are indistinguishable there, and re-``stat``ing the path to tell them apart
    would be another by-name look at the input these reads exist to stop
    trusting. So the audit line records THAT the bytes were withheld, never why.

    Best-effort: a listing or a detail view must not fail because an audit write
    did.
    """
    try:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name=tool_name,
            tool_kind=tool_kind,
            outcome=outcome,
            metadata=metadata,
        )
    except Exception:  # noqa: BLE001 — an audit write must not break the response
        logger.debug("Could not audit an unread %s", tool_kind, exc_info=True)


def _prompt_audit(op: str, outcome: str, **meta: Any) -> None:
    """Audit a prompt write — every outcome, rejections included (a refused write
    can be filesystem probing)."""
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name=op,
        tool_kind="prompt",
        outcome=outcome,
        metadata=meta,
    )


def _refuse(op: str, outcome: str, code: str, **meta: Any) -> None:
    """Audit a refused prompt write under the same identifier the response carries.

    The caller answers with a literal status and the same ``code`` (the contract
    the dashboard localizes on; ``error`` prose is advisory, RFC 9457 3.1.3).
    Passing ``code`` as the audited reason is what keeps "what was logged" and
    "what was answered" the same word.
    """
    _prompt_audit(op, outcome, reason=code, **meta)


_CODE_APP_TOKEN_FORBIDDEN = "app_token_forbidden"


def _deny_non_owner_prompt_write(request: web.Request, op: str, **meta: Any) -> web.Response | None:
    """403 unless this is the configured owner's dashboard request, else ``None``.

    Two refusals, distinct codes. An app-token caller — or an absent claim,
    where the middleware did not authenticate this request or deliberately
    withheld the claim, as the internal-secret transport does — is refused
    ``app_token_forbidden``: app-token grants are path-only, so an app whose
    manifest covers ``/api/prompts`` for its read surface would otherwise
    reach these mutations too. A dashboard caller who is not the configured
    owner is refused ``dashboard_owner_required``: prompts are the owner's
    agent instructions — a write is an instruction-injection surface — and
    allowed messaging users other than the owner can hold dashboard sessions,
    so "any dashboard user" is the wrong bar (``is_owner_dashboard_request``
    is reused rather than re-derived, and admits the signed local bootstrap
    subjects when no owner is configured). Both refusals fire before any body
    parsing and are SEL-audited under the code the response carries (``meta``
    carries the target, e.g. ``name``/``scope``, so a name-enumerating caller
    leaves attributable lines).
    """
    request_app = request.get("app")
    if request_app != "":
        try:
            _refuse(
                op,
                "blocked",
                _CODE_APP_TOKEN_FORBIDDEN,
                caller=str(request_app or "absent-claim"),
                **meta,
            )
        except Exception:  # noqa: BLE001 — preserve the denial response if SEL is unwritable
            logger.debug("Could not audit refused app-token prompt write", exc_info=True)
        return web.json_response(
            {"error": "app tokens may not modify prompts", "code": _CODE_APP_TOKEN_FORBIDDEN},
            status=403,
        )
    if not is_owner_dashboard_request(request):
        try:
            _refuse(
                op,
                "blocked",
                _CODE_DASHBOARD_OWNER_REQUIRED,
                caller=str(request.get("user") or "unknown"),
                **meta,
            )
        except Exception:  # noqa: BLE001 — preserve the denial response if SEL is unwritable
            logger.debug("Could not audit refused non-owner prompt write", exc_info=True)
        return web.json_response(
            {
                "error": "dashboard owner required",
                "code": _CODE_DASHBOARD_OWNER_REQUIRED,
            },
            status=403,
        )
    return None
