"""Skill learning's dashboard wiring.

The fallback history consolidator a dashboard-only launch builds, and the bell-feed
notification raised and retired for a staged skill candidate.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        _SKILL_APPROVAL_SETTING_URL,
        ContextBuilder,
        ConversationLog,
        DashboardState,
        HistoryConsolidator,
        KiroCrewConfig,
        LessonStore,
        SessionManager,
        SkillsLoader,
        logger,
        set_pending_consumed_hook,
        set_pending_staged_hook,
    )


def _pending_skill_notification(info: dict) -> tuple[str, str, str, list[dict[str, str]]]:
    """Build the bell-feed payload for a staged skill candidate.

    Returns ``(title, body, review_url, actions)``. Module-level (rather than
    inline in the staged hook) so the notification CONTENT is unit-testable
    without booting the dashboard app.
    """
    name = str(info.get("name") or info.get("slug") or "skill")
    slug = str(info.get("slug") or "")
    is_update = info.get("kind") == "update"
    target = str(info.get("target") or "")
    description = str(info.get("description") or "").strip()
    triggers = str(info.get("triggers") or "").strip()
    subject = target or name if is_update else name
    title = "Skill update awaiting review" if is_update else "New skill awaiting review"
    # The body LEADS with name + description because the feed row
    # renders only its first ~80 characters, stripped to one line.
    # The title already says a skill is awaiting review, so opening
    # with "was generated from a session and needs your approval"
    # spends exactly the characters that decide whether the reader
    # opens the queue on words they have already read. Identity plus
    # purpose first; the approval sentence still follows for the
    # detail panel, which renders the whole body as markdown.
    head = f"**{subject}**"
    if description:
        head += f" — {description}"
    lines = [head]
    lines.append(
        "\nGenerated from a session. Needs your approval before "
        + ("it takes effect." if is_update else "it can be used.")
    )
    if triggers:
        lines.append(f"\n**Triggers:** {triggers}")
    if info.get("has_scripts"):
        lines.append("\n_Bundles executable scripts — review them before approving._")
    body = "\n".join(lines)
    # Deep-link straight at the candidate, not just the tab: the
    # queue can hold several rows, and "go find it" is the failure
    # mode this notification exists to prevent. quote() keeps a slug
    # from opening a second query parameter -- slugs are validated
    # against a restrictive pattern upstream, but the URL is built
    # here and must not depend on that invariant holding.
    review_url = "/capabilities?tab=skills"
    if slug:
        review_url += f"&review={quote(slug, safe='')}"
    actions = [
        {
            "id": "review-skill",
            "label": "Review update" if is_update else "Review skill",
            "url": review_url,
        },
        # The opt-out shortcut: lands on the approval_required toggle in
        # Settings. Offered on every staged candidate — including
        # script-bearing ones, where it still governs FUTURE prose-only
        # skills (scripts always stage; the setting's own description
        # explains that boundary). The label shares the destination
        # toggle's polarity ("Require approval …" is ON; this stops it),
        # and the trailing ellipsis signals that the button NAVIGATES to a
        # settings page rather than flipping the setting itself —
        # notification actions are navigation-only.
        {
            "id": "auto-approve-skills",
            "label": "Stop requiring skill approval…",
            "url": _SKILL_APPROVAL_SETTING_URL,
        },
    ]
    return title, body, review_url, actions


def _auto_create_consolidator(
    sessions: SessionManager,
    lessons: LessonStore,
    context_builder: ContextBuilder | None,
    conversation_log: ConversationLog,
) -> HistoryConsolidator | None:
    """The consolidator a dashboard-only launch builds for itself, or ``None``.

    ``start_dashboard`` asks for one when it is handed a conversation log but no
    consolidator; a failure to build it is logged and the dashboard starts without.
    """
    consolidator = None
    try:
        from kiro_crew import history as _hist_mod
        from kiro_crew.memory import MemoryStore

        memory = context_builder.memory if context_builder else MemoryStore()
        if not context_builder:
            memory.init()
        # Wire the skills loader + config so a dashboard-only launch honors
        # the same auto-skill defaults as the CLI/gateway entry points —
        # otherwise this fallback silently ran with auto-generation disabled,
        # contradicting the on-by-default config.
        if context_builder is not None:
            _skills = context_builder.skills
        else:
            _skills = SkillsLoader(install_builtins=False)
        _scfg = KiroCrewConfig.load().skills
        consolidator = _hist_mod.HistoryConsolidator(
            log=conversation_log,
            memory=memory,
            sessions=sessions,
            lesson_store=lessons,
            skills_loader=_skills,
            auto_skills_enabled=_scfg.auto_create_from_sessions,
            auto_refine_enabled=_scfg.auto_refine_on_deviation,
            auto_min_tool_calls=_scfg.auto_min_tool_calls,
            auto_similarity_threshold=_scfg.auto_similarity_threshold,
            approval_required=_scfg.approval_required,
            max_auto_skills=_scfg.max_auto_skills,
            stale_after_days=_scfg.stale_after_days,
            archive_after_days=_scfg.archive_after_days,
            generate_scripts=_scfg.generate_scripts,
            judge_model=_scfg.judge_model,
        )
        logger.info("Auto-created HistoryConsolidator for dashboard (skills wired)")
    except Exception:
        logger.debug("Could not create consolidator", exc_info=True)
    return consolidator


def _register_pending_skill_hooks(state: DashboardState) -> None:
    """Raise and retire the bell-feed notification for a staged skill candidate.

    A staged candidate (new OR update) stays invisible until a human approves
    it, so raise a bell-feed notification with a deep link to the review queue
    and broadcast ``skills.pending_changed`` so an open Skills tab refreshes
    live. The hook is registered at MODULE level in ``skills`` because
    candidates are staged by whichever loader instance the producer holds
    (consolidation uses the ContextBuilder's; dashboard requests build their
    own), so a per-instance callback would miss the consolidation path.
    """
    try:
        # Capture the gateway loop: the hook fires from whatever thread staged
        # the candidate, and consolidation stages from a worker thread
        # (``asyncio.to_thread``). Both notify() and broadcast_ws() ultimately
        # call ``asyncio.ensure_future``, which RAISES off-loop — and
        # ``_send_ws_all`` treats that raise as a dead socket and EVICTS every
        # connected client. Marshal the emit back onto the loop instead.
        def _on_pending_skill_staged(info: dict) -> None:
            try:
                slug = str(info.get("slug") or "")
                is_update = info.get("kind") == "update"
                target = str(info.get("target") or "")
                title, body, review_url, actions = _pending_skill_notification(info)
                payload = {
                    "slug": slug,
                    "candidate_kind": "update" if is_update else "new",
                    "target": target,
                }

                def _emit() -> None:
                    try:
                        state.notify(
                            "skills",
                            title,
                            body,
                            meta=payload,
                            url=review_url,
                            actions=actions,
                        )
                        state.broadcast_ws("skills.pending_changed", payload)
                    except Exception:
                        logger.debug("pending-skill notification failed", exc_info=True)

                loop = state.serving_loop
                if loop is not None and not loop.is_closed():
                    # Safe from the loop thread too — call_soon_threadsafe just
                    # schedules. RuntimeError means the loop is shutting down.
                    try:
                        loop.call_soon_threadsafe(_emit)
                    except RuntimeError:  # pragma: no cover - loop closing
                        pass
                else:
                    _emit()
            except Exception:
                logger.debug("pending-skill notification failed", exc_info=True)

        set_pending_staged_hook(_on_pending_skill_staged)

        def _on_pending_skill_consumed(info: dict) -> None:
            # Counterpart of the staged hook above: when a candidate leaves the
            # queue (approved, dismissed, or TTL-pruned — by ANY loader
            # instance), retire its bell notification instead of leaving an
            # unread row whose deep link now lands on the banner saying the
            # candidate left the review queue. Same thread contract as staging: the hook fires
            # from whatever thread consumed the candidate (dashboard handlers
            # run it on an executor), so marshal onto the gateway loop before
            # touching the notification log or the WS fanout.
            try:
                slug = str(info.get("slug") or "")
                consumed_at = str(info.get("consumed_at") or "")
                if not slug or not consumed_at:
                    return

                def _resolve() -> None:
                    try:
                        task = asyncio.ensure_future(
                            state.resolve_skill_review_notifications(slug, consumed_at)
                        )
                        state._background_tasks.add(task)
                        task.add_done_callback(state._background_tasks.discard)
                    except Exception:
                        logger.debug("pending-skill notification resolve failed", exc_info=True)

                loop = state.serving_loop
                if loop is not None and not loop.is_closed():
                    try:
                        loop.call_soon_threadsafe(_resolve)
                    except RuntimeError:  # pragma: no cover - loop closing
                        pass
                # Without a loop there is no serving dashboard (sync/embedded
                # launch): no SSE/WS clients to update and no executor to
                # persist through, so the row is left as-is.
            except Exception:
                logger.debug("pending-skill notification resolve failed", exc_info=True)

        set_pending_consumed_hook(_on_pending_skill_consumed)
    except Exception:
        logger.debug("Could not register pending-skill staged hook", exc_info=True)
