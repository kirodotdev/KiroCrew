"""A crewmate on a template page asks once, on a card, before it replaces that page.

A crewmate whose tab shows nothing of its own writes its page with no card
(``goal_reminder.DASHBOARD_BUILD_LINE``). One on a TEMPLATE page -- one the person
adopted, or the page named for it -- never replaces it unasked: on the first Daily
Goal Reminder, goal-setup or milestones-confirmed wake after it knows what its page
is about (a goal and confirmed milestones), the wake carries :data:`OFFER_LINE`
instead, and the crewmate calls ``offer_dashboard_page`` (``kirocrew-mate``), which
raises one ``crewmate.dashboard_page`` change card and writes nothing. Pressing it runs
``POST /api/members/{slug}/dashboard/page-offer`` (:func:`api_member_page_offer`),
which wakes the crewmate once with :func:`write_prompt`: write the page,
``dashboard_save`` it and ``dashboard_apply`` it. Declining, or leaving the card to
expire, writes nothing.

Asked-once is a record per crewmate in ``crew-panels`` (:func:`record_path`), which
the crewmate's own shell cannot reach: written just before the card is shown, so a yes, a
no and an expiry all count, and no later wake asks again. The offer line and the
card's own preview both read it.

A yes waits in memory for the crewmate's thread to go idle, so a gateway that exits
in that wait would lose it with the record still saying "asked". The yes therefore
marks the record ``pending`` with this process's boot id
(``boot_id.current_boot_id``, :func:`record_yes`), and the wake clears the mark once it starts
(:func:`record_started`). A mark left by an earlier process is a yes whose wake never
started: :func:`was_asked` reads it as not asked, so the next wake offers the page again.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kiro_crew.dashboard_templates.page_look import PAGE_LOOK

logger = logging.getLogger(__name__)

_RECORD_SUFFIX = ".page-offer.json"


def record_path(slug: str) -> Path:
    """Where crewmate *slug*'s asked-once record lives (``crew-panels``, gateway-only)."""
    from kiro_crew.agent_panel import panel_dir
    from kiro_crew.members import validate_slug

    validate_slug(slug)
    return panel_dir() / f"{slug}{_RECORD_SUFFIX}"


def read_record(slug: str) -> dict[str, Any] | None:
    """Crewmate *slug*'s asked-once record, or ``None`` when it was never asked.

    A record that exists but cannot be read counts as asked, so a damaged file never
    turns into a second card.
    """
    try:
        path = record_path(slug)
    except Exception:
        return None
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("page offer record for %s is unreadable; treated as asked", slug)
        return {}
    return data if isinstance(data, dict) else {}


def was_asked(slug: str) -> bool:
    """Whether crewmate *slug* has already been asked (yes, no or expiry all count).

    A yes an earlier gateway process accepted but never started its wake for
    (``pending`` naming another process) does not count, so the page is offered again.
    """
    rec = read_record(slug)
    if rec is None:
        return False
    pending = rec.get("pending")
    if not (isinstance(pending, str) and pending):
        return True
    from kiro_crew.dashboard.boot_id import current_boot_id

    return pending == current_boot_id()


def _write(slug: str, data: dict[str, Any]) -> None:
    from kiro_crew.atomic_write import atomic_write

    path = record_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(data, sort_keys=True), fsync=True)


def record_asked(slug: str, replaces: str, template_id: str = "") -> None:
    """Record that crewmate *slug* is raising its one page-offer card.

    Written before the card is published, so a card is never shown without the record
    that stops a second one. *replaces* is the template the yes wake names, and
    *template_id* the page the person said yes over, checked again when the wake is
    sent (:func:`schedule_write`).
    """
    data = {"replaces": replaces}
    if template_id:
        data["template_id"] = template_id
    _write(slug, data)


def _mark(slug: str, pending: bool) -> None:
    """Set or clear the record's ``pending`` mark. Best effort: a mark that cannot be
    written leaves the record as it was, which still counts as asked."""
    try:
        rec = read_record(slug)
        if not rec:
            return
        data = {k: v for k, v in rec.items() if k != "pending"}
        if pending:
            from kiro_crew.dashboard.boot_id import current_boot_id

            data["pending"] = current_boot_id()
        if data != rec:
            _write(slug, data)
    except Exception:
        logger.warning("page offer record for %s could not be marked", slug, exc_info=True)


def record_yes(slug: str) -> None:
    """Mark *slug*'s record: the person said yes, and the write wake has not started."""
    _mark(slug, True)


def record_started(slug: str) -> None:
    """Clear the yes mark: the write wake started, so the offer is spent."""
    _mark(slug, False)


def forget_asked(slug: str) -> None:
    """Drop *slug*'s record: the card it was written for was never raised."""
    try:
        record_path(slug).unlink(missing_ok=True)
    except OSError:
        logger.warning("page offer record for %s could not be removed", slug, exc_info=True)


def _replaces_phrase(replaces: str) -> str:
    return f", in place of the {replaces} template on your Dashboard tab" if replaces else ""


#: The line a wake carries while the crewmate may offer its page. ``{replaces}`` is
#: :func:`_replaces_phrase`. An offer, never a cost: the card and this line say what
#: the page is for, not what it takes to write.
OFFER_LINE: str = (
    "Kiro Crew notes: you can offer the user a Dashboard page built around your goal"
    "{replaces}. Do not write it now. Call offer_dashboard_page (kirocrew-mate) once in "
    "this turn, and mention the offer in one short sentence; the user answers on the "
    "card, and you are woken to write the page only if they say yes."
)


def offer_line(slug: str, rec: dict[str, Any]) -> str:
    """:data:`OFFER_LINE` for crewmate *slug*, or ``""`` when it should not ask.

    Asks only once *rec* holds a goal and confirmed milestones, only while a template
    page is on the crewmate's tab (:func:`kiro_crew.dashboard_agentic.page_offer_target`),
    and only if it was never asked. TOTAL.
    Blocking IO.
    """
    from kiro_crew.dashboard.goal_reminder import knows_its_page

    if not slug or not knows_its_page(rec):
        return ""
    try:
        if was_asked(slug):
            return ""
        from kiro_crew import dashboard_agentic

        target = dashboard_agentic.page_offer_target(slug)
    except Exception:
        logger.debug("page offer line for %s: lookup failed", slug, exc_info=True)
        return ""
    if target is None:
        return ""
    return OFFER_LINE.format(replaces=_replaces_phrase(target.get("replaces", "")))


#: The hidden turn after the person says yes on the card.
WRITE_PROMPT: str = (
    "[Dashboard page] The user said yes to your offer of a Dashboard page built around "
    "your goal{replaces}. Write it now with the dashboard skill: your own HTML page "
    "(view.html), saved with dashboard_save and then put live with dashboard_apply, with "
    "no second question. Read your goal and milestones first (goal_read). The page shows "
    "your goal, each milestone with its target and latest reading, what you are "
    "working on, and what waits on the user. Read each value from a crew-log fold where "
    "one carries it, so it stays current by itself: goal for the goal and its milestones, "
    "waiting for what waits on the user, work and workstreams for your work items; "
    "dashboard_types lists the rest. A hand-written field is for prose only you know. "
    + PAGE_LOOK
    + " Colour only with the variables the Dashboard tab sets (var(--bg), var(--card), "
    "var(--text), var(--muted), var(--border), var(--accent), var(--ok), var(--warn), "
    "var(--danger) and the rest the skill lists) so light and dark both read, and act on any "
    "shape or theme warning the save reply gives before you apply. If dashboard_apply "
    "refuses because the person locked the layout, say so in one sentence and stop. "
    "Once it is live, post one short message, the only one about it: a Dashboard page "
    "is ready for the user on your Dashboard tab, open it to see it; {restore} No "
    "preview, nothing else."
)


def _restore_phrase(template_id: str) -> str:
    """How the replaced template comes back. A page named for the crewmate that it never
    adopted has no earlier version for ``dashboard_rollback`` to reach, so the template
    is put back by id."""
    if template_id:
        return (
            "if they want the template back, you put it back with "
            f'dashboard_apply(template_id="{template_id}").'
        )
    return "the template comes back with one dashboard_rollback."


def write_prompt(replaces: str, template_id: str = "") -> str:
    """:data:`WRITE_PROMPT`, naming the page it replaces and how that page comes back."""
    return WRITE_PROMPT.format(
        replaces=_replaces_phrase(replaces), restore=_restore_phrase(template_id)
    )


def schedule_write(
    state: Any, slug: str, member: str, *, page: "dict[str, Any] | None" = None
) -> None:
    """Wake *member* once its thread is idle to write, save and apply its page.

    A wake that never starts (the thread stays busy past the wait, or closes) takes the
    asked-once record back, so a later wake offers the page again instead of the yes
    being lost. The page is read again when the wake is built: a template the person
    picked while the thread was busy is a newer choice than the yes, so no wake is sent
    and the record goes back the same way. *page* is the page the card's yes was given
    over (its last previewed snapshot); without it, the page the card was raised over.
    The caller marks the yes first, off the loop (:func:`record_yes`); the wake clears
    the mark when it starts, so a gateway that exits while the wake waits leaves the
    page to be offered again.
    """
    from kiro_crew.dashboard import goal_setup

    record = page if isinstance(page, dict) else (read_record(slug) or {})
    replaces = str(record.get("replaces") or "")
    approved = str(record.get("template_id") or "")

    def build(_rec: dict[str, Any], _tools: bool) -> "str | None":
        from kiro_crew import dashboard_agentic

        target = dashboard_agentic.page_offer_target(slug)
        if target is None or (approved and target.get("template_id") != approved):
            logger.info("page offer for %s: the page changed since the yes; no wake", slug)
            return None
        return write_prompt(replaces, approved)

    goal_setup._schedule(
        state,
        f"{slug}:dashboard-page",
        slug,
        member,
        build,
        "dashboard page",
        on_dropped=lambda _outcome: forget_asked(slug),
        on_started=lambda: record_started(slug),
    )
