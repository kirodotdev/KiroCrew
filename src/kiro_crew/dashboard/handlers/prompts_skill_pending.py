"""Staged skill candidates: list, read, approve and dismiss them.

The pending-review routes under ``/api/skills/-/pending``; an approval promotes the
candidate under the skill write lock.

Served through :mod:`kiro_crew.dashboard.handlers.prompts`, which callers and tests import and patch; this module
never imports it.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers.prompts_caller_gates import _deny_non_owner_skill_operation, _sel
from kiro_crew.dashboard.handlers.prompts_paths import _plain_stem_ok
from kiro_crew.dashboard.handlers.prompts_skill_edits import _SKILL_WRITE_LOCK
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import discovery_executor
from kiro_crew.skills import PendingApprovalRefused

from ._shared import _get_skills

# Pending-skill approval refusals (api_skill_pending_approve): distinct codes so
# the Skills tab can tell the user WHY the click did nothing instead of
# swallowing one shapeless conflict answer.
_CODE_PENDING_SKILL_NOT_FOUND = "pending_skill_not_found"
# The detail read refused a candidate that is STILL STAGED. A distinct code because
# the list applies none of the read's refusals, so that row remains: answering the
# same `not_found` as a deleted candidate tells the user it was approved or dismissed
# elsewhere while it sits in front of them, and leaves the one action that does apply
# (dismiss it) looking like the wrong one.
_CODE_PENDING_SKILL_UNREADABLE = "pending_skill_unreadable"
_CODE_LIVE_SKILL_EXISTS = "live_skill_exists"
_CODE_SCRIPT_VALIDATION_FAILED = "script_validation_failed"
_CODE_PENDING_APPROVAL_REFUSED = "pending_approval_refused"


# ── Auto-skill pending-approval queue (v2) ──


async def api_skills_pending(request: web.Request) -> web.Response:
    """GET /api/skills/-/pending — list staged auto-skill candidates."""
    state: DashboardState = request.app["state"]
    skills = _get_skills(state)

    def _prune_and_list() -> list:
        # Opportunistic TTL cleanup on read — gives prune_pending a real caller
        # so stale candidates don't accumulate unbounded.
        try:
            ttl = KiroCrewConfig.load().skills.pending_ttl_days
            skills.prune_pending(ttl)
        except Exception:
            pass
        return skills.list_pending_skills()

    try:
        items = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), _prune_and_list
        )
    except Exception:
        items = []
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_skills_pending",
        tool_kind="skill",
        outcome="ok",
        metadata={"count": len(items)},
    )
    return web.json_response({"pending": items})


async def api_skill_pending_detail(request: web.Request) -> web.Response:
    """GET /api/skills/-/pending/{slug} — full candidate incl. body + scripts."""
    state: DashboardState = request.app["state"]
    skills = _get_skills(state)
    slug = request.match_info["slug"]
    if not _plain_stem_ok(slug):
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pending_detail",
            tool_kind="skill",
            outcome="bad_request",
            metadata={"slug": slug},
        )
        return web.json_response({"error": "invalid slug"}, status=400)
    try:
        detail = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), skills.get_pending_skill, slug
        )
    except Exception:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pending_detail",
            tool_kind="skill",
            outcome="error",
            metadata={"slug": slug},
        )
        return web.json_response({"error": "internal error"}, status=500)
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_skill_pending_detail",
        tool_kind="skill",
        outcome="ok" if detail is not None else "not_found",
        metadata={"slug": slug},
    )
    if detail is None:
        # Two situations, two codes. The pinned read refuses a candidate whose tree is
        # not plain files and directories, and `list_pending_skills` applies none of
        # those refusals -- so that candidate's ROW REMAINS while this answers 404. One
        # shared code would have the panel tell the user it was approved or dismissed
        # elsewhere, which is false and points them away from the one action that does
        # apply. The probe is by name and runs only after the read already refused, so
        # losing its race changes the MESSAGE and never grants a read.
        staged = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), skills.pending_candidate_is_staged, slug
        )
        if staged:
            return web.json_response(
                {
                    "error": (
                        "this candidate is still pending, but its files are not a plain "
                        "directory, so it cannot be read safely"
                    ),
                    "code": _CODE_PENDING_SKILL_UNREADABLE,
                },
                status=404,
            )
        return web.json_response(
            {"error": "not found", "code": _CODE_PENDING_SKILL_NOT_FOUND}, status=404
        )
    # Update candidates carry an approval PREVIEW so the UI can show exactly what
    # approving would change: the target's current live body, the proposed
    # post-approval content, and a unified diff between them (computed
    # server-side with difflib so the frontend needs no diff dependency).
    # kind/target may be exposed at the top level or nested under ``meta`` — read
    # defensively. All preview fields are null if the target skill was removed
    # since the candidate was staged.
    _meta = detail.get("meta") if isinstance(detail.get("meta"), dict) else {}
    kind = detail.get("kind") or _meta.get("kind")
    if kind == "update":

        def _preview() -> dict | None:
            try:
                return skills.preview_pending_update(slug)
            except Exception:
                return None

        try:
            pv = await asyncio.get_running_loop().run_in_executor(discovery_executor(), _preview)
        except Exception:
            pv = None
        detail["live_body"] = (pv or {}).get("live_body")
        detail["proposed_body"] = (pv or {}).get("proposed_body")
        detail["diff"] = (pv or {}).get("diff")
        detail["from_version"] = (pv or {}).get("from_version")
        detail["to_version"] = (pv or {}).get("to_version")
        detail["stale_base"] = bool((pv or {}).get("stale_base"))
    return web.json_response(detail)


async def api_skill_pending_approve(request: web.Request) -> web.Response:
    """POST /api/skills/-/pending/{slug}/approve — promote candidate to live."""
    denied = _deny_non_owner_skill_operation(request, "skill_pending_approve")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    skills = _get_skills(state)
    slug = request.match_info["slug"]
    if not _plain_stem_ok(slug):
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pending_approve",
            tool_kind="skill",
            outcome="rejected",
            metadata={"slug": slug, "reason": "invalid_slug"},
        )
        return web.json_response({"error": "invalid slug"}, status=400)

    def _approve_and_bound() -> str | None:
        # Route on candidate kind: an UPDATE candidate rewrites an existing live
        # skill (approve_pending_update_checked); a NEW candidate is promoted fresh
        # (approve_pending_skill_checked). kind is read from the candidate detail
        # (top-level or nested ``meta``), defaulting to the new path.
        kind = None
        try:
            _detail = skills.get_pending_skill(slug)
        except Exception:
            _detail = None
        if isinstance(_detail, dict):
            _meta_raw = _detail.get("meta")
            _meta: dict = _meta_raw if isinstance(_meta_raw, dict) else {}
            kind = _detail.get("kind") or _meta.get("kind")
        # Under the skill write lock: an update promotion replaces a live
        # SKILL.md, so it must not land between a PUT's compare and its write.
        # The lifecycle bound below archives other skills and stays outside it.
        with _SKILL_WRITE_LOCK:
            if kind == "update":
                nm = skills.approve_pending_update_checked(slug)
            else:
                nm = skills.approve_pending_skill_checked(slug)
        if nm:
            # Approving consumes a slot — enforce the bound (archive, never
            # delete). Best-effort; runs in the same off-loop executor job.
            # Exempt the just-approved skill so a full-cap pass can't archive the
            # very skill this request promoted (brand-new + zero-hit, it would
            # otherwise rank lowest in the max-N backstop).
            try:
                cfg = KiroCrewConfig.load().skills
                skills.run_skill_lifecycle(
                    max_auto_skills=cfg.max_auto_skills,
                    stale_after_days=cfg.stale_after_days,
                    archive_after_days=cfg.archive_after_days,
                    exempt={nm},
                )
            except Exception:
                pass
        return nm

    try:
        name = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), _approve_and_bound
        )
    except PendingApprovalRefused as e:
        # The refusal reason reaches the user instead of collapsing into one
        # shapeless conflict answer. SEL keeps the outcome accurate: a
        # missing candidate is ``not_found``; every other refusal is a
        # ``rejected`` with the reason in metadata.
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pending_approve",
            tool_kind="skill",
            outcome="not_found" if e.reason == "not_found" else "rejected",
            metadata={"slug": slug, "reason": e.reason},
        )
        if e.reason == "not_found":
            return web.json_response(
                {"error": "pending skill not found", "code": _CODE_PENDING_SKILL_NOT_FOUND},
                status=404,
            )
        if e.reason == "live_exists":
            return web.json_response(
                {
                    "error": "a live skill with this name already exists",
                    "code": _CODE_LIVE_SKILL_EXISTS,
                },
                status=409,
            )
        if e.reason == "script_validation_failed":
            return web.json_response(
                {
                    "error": "script validation failed",
                    "code": _CODE_SCRIPT_VALIDATION_FAILED,
                    "report": e.report or {},
                },
                status=422,
            )
        return web.json_response(
            {
                "error": f"approval refused: {e.reason}",
                "code": _CODE_PENDING_APPROVAL_REFUSED,
                "reason": e.reason,
            },
            status=409,
        )
    except Exception:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pending_approve",
            tool_kind="skill",
            outcome="error",
            metadata={"slug": slug},
        )
        return web.json_response({"error": "internal error"}, status=500)
    # The checked variant either raised (mapped to a coded response above) or
    # returned the approved name — a falsy name cannot reach here.
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_skill_pending_approve",
        tool_kind="skill",
        outcome="ok",
        metadata={"slug": slug, "name": name},
    )
    return web.json_response({"approved": name})


async def api_skill_pending_dismiss(request: web.Request) -> web.Response:
    """POST /api/skills/-/pending/{slug}/dismiss — delete a candidate."""
    denied = _deny_non_owner_skill_operation(request, "skill_pending_dismiss")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    skills = _get_skills(state)
    slug = request.match_info["slug"]
    if not _plain_stem_ok(slug):
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pending_dismiss",
            tool_kind="skill",
            outcome="rejected",
            metadata={"slug": slug, "reason": "invalid_slug"},
        )
        return web.json_response({"error": "invalid slug"}, status=400)
    try:
        ok = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), skills.dismiss_pending_skill, slug
        )
    except Exception:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skill_pending_dismiss",
            tool_kind="skill",
            outcome="error",
            metadata={"slug": slug},
        )
        return web.json_response({"error": "internal error"}, status=500)
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_skill_pending_dismiss",
        tool_kind="skill",
        outcome="ok" if ok else "not_found",
        metadata={"slug": slug},
    )
    if not ok:
        # Coded like the approve path's not-found: the dashboard keys its
        # recovery (refetch + catalog message) on this code, and an uncoded
        # body would leave that branch reachable only from test mocks.
        return web.json_response(
            {"error": "pending skill not found", "code": "pending_skill_not_found"},
            status=404,
        )
    return web.json_response({"dismissed": slug})


async def api_skills_pending_dismiss_all(request: web.Request) -> web.Response:
    """POST /api/skills/-/pending/-/dismiss-all — dismiss pending candidates.

    Requires a JSON body ``{"slugs": ["slug1", ...]}`` with a non-empty array.
    Only those slugs are dismissed (the client passes the set it displayed to the
    user, so a candidate staged *after* the confirmation dialog is never silently
    deleted). When the body is absent or ``slugs`` is missing or empty, the route
    returns 400 ``slugs_required`` and dismisses nothing.
    """
    denied = _deny_non_owner_skill_operation(request, "skill_pending_dismiss_all")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    skills = _get_skills(state)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_body"}, status=400
        )
    raw_slugs = body.get("slugs")
    if raw_slugs is not None and (
        not isinstance(raw_slugs, list) or not all(isinstance(s, str) for s in raw_slugs)
    ):
        return web.json_response(
            {"error": "slugs must be an array of strings", "code": "invalid_slugs"}, status=400
        )
    slugs: list[str] = raw_slugs if isinstance(raw_slugs, list) else []
    try:
        if slugs:
            count = await asyncio.get_running_loop().run_in_executor(
                discovery_executor(),
                lambda: skills.dismiss_pending_slugs(slugs),
            )
        else:
            return web.json_response(
                {
                    "error": "slugs array is required and must not be empty",
                    "code": "slugs_required",
                },
                status=400,
            )
    except Exception:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skills_pending_dismiss_all",
            tool_kind="skill",
            outcome="error",
            metadata={},
        )
        return web.json_response({"error": "internal error", "code": "internal_error"}, status=500)
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_skills_pending_dismiss_all",
        tool_kind="skill",
        outcome="ok",
        metadata={"count": count},
    )
    return web.json_response({"dismissed_count": count})
