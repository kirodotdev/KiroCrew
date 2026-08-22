from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.dashboard.handlers._shared import _session_key_is_restricted, read_bounded_json
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import INCOGNITO_MEMORY_MODES
from kiro_crew.sel import sel
from kiro_crew.session_summary import count_user_turns, extract_turns, render_input
from kiro_crew.subagent import _validate_agent

logger = logging.getLogger(__name__)

_MAX_PURPOSE_CHARS = 500
_ASSISTANT_EXCERPT_CHARS = 600
_MAX_TRANSCRIPT_CHARS = 60_000
_TRUNCATION_MARKER = "\n\n[... earlier transcript omitted to fit the authoring budget ...]\n\n"

_SKILL_AUTHOR_INSTRUCTIONS = """\
Create a skill from this session.

You are a background subagent whose only job is to author one reusable Kiro Crew
skill from the chat transcript provided at the end of this message, then hand off.
Follow the `crystallize` skill in candidate mode:

- Stage the skill as a pending candidate under your Kiro Crew skills directory at
  `auto/.pending/<slug>/`, honoring $KIROCREW_HOME. Write both `SKILL.md` and the
  sibling `.meta.json`. This is a MANUAL, user-requested capture, so mark it as such:
  in `.meta.json` set `"namespace": "manual"`, `"name": "manual/<slug>"`, and
  `"source": "make-it-skill"`, and make the `SKILL.md` frontmatter `name:` read
  `manual/<slug>`. On approval it is promoted to a live skill under `manual/<slug>/`,
  keeping user-captured skills separate from auto-generated ones. Never write a live
  or active skill yourself, never write into the packaged `builtin_skills/` directory,
  and never write into a repository checkout.
- Reconstruct the reusable procedure from the whole transcript, folding in the
  working path from any `[Subagent completion event]` rows. Check the existing
  `auto/` and `manual/` skills first and freshen a near-duplicate rather than staging
  a second copy.
- Prefer prose steps. Add a Python helper script only when determinism earns it, keep
  it under 4 KB, and never let it read credentials, delete files, or call unknown hosts.
- Never include credentials, tokens, absolute paths, or personal data in the skill
  body, the metadata, or any script.

You do not have live access to the parent conversation; the transcript below is the
complete source. The candidate you stage appears in the user's Skills -> Pending review
for approval, so nothing loads until the user approves it.
"""

_PURPOSE_PREFIX = "\n\nThe user described the skill they want in one line:\n"
_TRANSCRIPT_HEADER = "\nTRANSCRIPT:\n\n"


def _audit_denied(request: web.Request, reason: str) -> None:
    """Emit a SEL audit event for a denied request on this owner-only endpoint.

    ``backend-security-controls`` requires every permission decision -- including a
    denial -- to leave an audit trail; the success path audits after the spawn.
    """
    try:
        sel().log_tool_invocation(
            session_key=str(request.get("user") or "dashboard"),
            source="dashboard",
            tool_name="create_skill_from_session",
            outcome="denied",
            error=reason,
        )
    except Exception:
        logger.warning("SEL audit failed for create_skill_from_session denial", exc_info=True)


async def api_create_skill_from_session(request: web.Request) -> web.Response:
    if request.get("app"):
        _audit_denied(request, "app_forbidden")
        return web.json_response(
            {"error": "app token not permitted", "code": "app_forbidden"}, status=403
        )
    if request.get("internal_auth"):
        _audit_denied(request, "human_only")
        return web.json_response(
            {"error": "this endpoint is owner-only", "code": "human_only"}, status=403
        )
    if not is_owner_dashboard_request(request):
        _audit_denied(request, "forbidden")
        return web.json_response({"error": "forbidden", "code": "forbidden"}, status=403)

    state: DashboardState = request.app["state"]
    if not state.subagents:
        return web.json_response(
            {"error": "subagents not available", "code": "subagents_unavailable"}, status=503
        )
    log = state.conversation_log
    if log is None:
        return web.json_response(
            {"error": "conversation history unavailable", "code": "history_unavailable"},
            status=503,
        )

    body, err = await read_bounded_json(request)
    if err is not None or body is None:
        return err or web.json_response(
            {"error": "invalid JSON body", "code": "invalid_json"}, status=400
        )

    session_key = body.get("session_key")
    if not isinstance(session_key, str) or not session_key.strip():
        return web.json_response(
            {"error": "session_key is required", "code": "session_key_required"}, status=400
        )
    session_key = session_key.strip()

    purpose = body.get("purpose", "")
    if not isinstance(purpose, str):
        return web.json_response(
            {"error": "purpose must be a string", "code": "invalid_purpose"}, status=400
        )
    purpose = purpose.strip()
    if not purpose:
        return web.json_response(
            {"error": "purpose is required", "code": "purpose_required"}, status=400
        )
    if len(purpose) > _MAX_PURPOSE_CHARS:
        return web.json_response(
            {
                "error": f"purpose exceeds {_MAX_PURPOSE_CHARS} characters",
                "code": "purpose_too_long",
            },
            status=400,
        )

    slot_name = session_key.split(":", 1)[-1] if ":" in session_key else session_key
    slot = state._slots.get(slot_name)
    if slot is None:
        return web.json_response(
            {"error": f"unknown session {session_key!r}", "code": "unknown_session"}, status=404
        )

    # Refuse if the SESSION whose transcript we would persist is private: the slot's
    # own incognito/temporary mode OR the canonical restriction of the session it runs
    # on. A channel-born slot (e.g. a Slack thread set to !incognito) keeps its
    # restriction on the linked session, not on the slot's memory_mode, so the slot
    # check alone would leak a private thread into a skill.
    if getattr(slot, "memory_mode", "") in INCOGNITO_MEMORY_MODES or _session_key_is_restricted(
        state, effective_session_key(slot)
    ):
        _audit_denied(request, "incognito_session")
        return web.json_response(
            {"error": "cannot author a skill from a private session", "code": "incognito_session"},
            status=400,
        )

    history_key = slot_history_key(slot)
    records = await asyncio.to_thread(log.read_messages_chained, history_key)
    turns = extract_turns(list(records), assistant_excerpt_chars=_ASSISTANT_EXCERPT_CHARS)
    if count_user_turns(turns) < 1:
        return web.json_response(
            {"error": "session has nothing to author from", "code": "empty_session"}, status=400
        )

    shaped = render_input(turns)
    if len(shaped) > _MAX_TRANSCRIPT_CHARS:
        shaped = _TRUNCATION_MARKER + shaped[-_MAX_TRANSCRIPT_CHARS:]

    purpose_section = _PURPOSE_PREFIX + purpose + "\n"
    # nosemgrep: python.django.security.injection.raw-html-format.raw-html-format -- `task` is a prompt string for the authoring subagent, never rendered as HTML (no HTTP HTML body, no mark_safe); the raw-html-format heuristic false-positives on the string concatenation.
    task = _SKILL_AUTHOR_INSTRUCTIONS + purpose_section + _TRANSCRIPT_HEADER + shaped

    parent_key = effective_session_key(slot)

    # Run the authoring subagent under the TARGET session's governance identities, not the
    # default agent: an app- or named-agent session's scoped profile must still bound the
    # subagent. Validate the agent OFF the event loop (list_agents() walks the filesystem),
    # then let spawn() skip its own on-loop scan via _agent_prevalidated; an unresolvable
    # agent is refused rather than silently downgraded to the default.
    target_agent = getattr(slot, "agent", "") or ""
    target_app = getattr(slot, "_app", "") or ""
    agent_prevalidated = False
    if target_agent:
        _resolved, agent_err = await asyncio.to_thread(_validate_agent, target_agent)
        if agent_err:
            return web.json_response({"error": agent_err, "code": "agent_unavailable"}, status=400)
        agent_prevalidated = True

    # TOCTOU: both the transcript read and the off-loop agent validation above are awaited, so a
    # linked participant could flip the session to !incognito in that window. Re-check the
    # canonical restriction as the LAST step before the synchronous spawn -- with no await
    # between the check and the spawn -- and refuse if it changed, so a now-private
    # transcript is never handed to the authoring subagent.
    if getattr(slot, "memory_mode", "") in INCOGNITO_MEMORY_MODES or _session_key_is_restricted(
        state, parent_key
    ):
        _audit_denied(request, "incognito_session")
        return web.json_response(
            {"error": "cannot author a skill from a private session", "code": "incognito_session"},
            status=400,
        )

    # approval_mode="spawn": the owner explicitly triggered this, so the spawn is
    # pre-authorized (no spawn-approval prompt), but the authoring subagent runs on
    # an UNTRUSTED transcript -- it must NOT auto-approve its own tool calls and does
    # NOT inherit a trusted parent's auto policy; its tools stay approval-gated.
    info = state.subagents.spawn(
        task,
        parent_session_key=parent_key,
        agent=target_agent,
        app=target_app,
        _agent_prevalidated=agent_prevalidated,
        approval_mode="spawn",
        silent=True,
        model=None,
        include_memory=True,
        include_lessons=True,
        include_project=True,
    )
    if not info:
        return web.json_response(
            {
                "error": f"capacity reached ({state.subagents.max_concurrent})",
                "code": "at_capacity",
            },
            status=429,
        )
    if info.done and info.error:
        return web.json_response({"error": info.error, "code": "spawn_rejected"}, status=400)

    try:
        sel().log_tool_invocation(
            session_key=parent_key,
            source="dashboard",
            tool_name="create_skill_from_session",
            outcome="invoked",
            request_id=info.id,
        )
    except Exception:
        logger.warning("SEL audit failed for create_skill_from_session", exc_info=True)

    logger.info("create_skill_from_session spawned subagent %s for %s", info.id, history_key)
    return web.json_response({"id": info.id, "status": "spawned"}, status=202)
