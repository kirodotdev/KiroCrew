"""The ``worker_grill`` feature preview: a conductor may offer a fast worker grill.

Behind ``dashboard.feature_previews.worker_grill``, default off. On, a conductor's
dashboard turn carries :data:`GUIDANCE`: when a choice has two plausible paths and a
wrong pick is costly, offer "let workers grill it first" in ``[OPTIONS:]``, answer a
pick with one ``[GRILL: {...}]`` line, and on the owner's start dispatch one
moderator worker that runs the quick-chat protocol in the goal-conductor skill's
``references/worker-grill.md``. That replaces the skill's unasked debate offer. Off,
the turn carries none of it and the conductor behaves as on ``main``: the skill's
own rule for offering a debate (``references/worker-debate.md``) stands.
"""

from __future__ import annotations

from typing import Any, Final

from kiro_crew.agent_files import (
    CONDUCTOR_AGENT_FILENAME,
    LEDGER_CONDUCTOR_AGENT_FILENAME,
    MATE_CONDUCTOR_AGENT_FILENAME,
    TEAM_LEAD_AGENT_FILENAME,
)

#: The preview's name under ``dashboard.feature_previews``.
WORKER_GRILL_PREVIEW: Final[str] = "worker_grill"

#: The agents that read the goal-conductor skill and dispatch workers: the
#: conductor (and its deprecated ledger alias), the team lead Crew Mode runs on, and
#: a Bot's conductor. The guidance names that skill's reference file, so an agent
#: without the skill is never told to read it. Derived from the filenames the
#: runtime installs, so a renamed spec cannot leave a stale literal.
GRILL_AGENTS: Final[frozenset[str]] = frozenset(
    name.removesuffix(".json")
    for name in (
        CONDUCTOR_AGENT_FILENAME,
        LEDGER_CONDUCTOR_AGENT_FILENAME,
        TEAM_LEAD_AGENT_FILENAME,
        MATE_CONDUCTOR_AGENT_FILENAME,
    )
)

#: The paragraph a conductor's dashboard turn carries while the preview is on.
GUIDANCE: Final[str] = (
    "\n\n(Worker grill preview is on: it replaces the goal-conductor skill's unasked "
    "debate offer. When a choice has two or more plausible paths "
    "AND a wrong pick is costly -- it crosses a security, data, approval or trust "
    "boundary, changes a contract others depend on, or follows a failed attempt or a "
    "reviewer's design block -- add \"Let workers grill it first\" to that reply's "
    "[OPTIONS:]. Never for a small clear fix, CI or rebase work, wording, an "
    "emergency, or a choice the owner already made. When the owner picks it, reply "
    "with one line, on its own and before any [OPTIONS:] line: "
    '[GRILL: {"q":"<question>","must":"<must achieve>","skip":"<out of scope>",'
    '"who":[{"name":"A","stance":"<side>"},{"name":"B","stance":"<side>"}],'
    '"minutes":15}] -- q, must, skip at most 200 chars each; who 2-3 entries, name '
    "at most 20 chars, stance at most 80; minutes an integer 1-60; valid JSON. "
    "End that reply with an [OPTIONS:] line of two choices in the owner's language: "
    "start the grill, and skip it, so the owner can answer where no discussion card "
    "is drawn. The card's messages may be in any language. A start is one opening "
    "line plus three labelled lines (question, must, not discussing) carrying the "
    "owner's edits, plus at most one `key: <value>` line; a start choice without the "
    "labelled lines uses the card as written. The other lines follow the owner's "
    "language; the key line is a wire value, never translated, matched only as a "
    "whole line, its value opaque. On a start, dispatch ONE kirocrew-worker as "
    "moderator, following "
    "references/worker-grill.md in the goal-conductor skill; for a keyed start, end "
    "its item title with `grill key:<value>`. A start is ignored, in whatever "
    "language it comes, when any grill item's title, open or closed, ends in exactly "
    "its `grill key:<value>`, "
    "or, with no key line, when its question already has an open grill item: never "
    "a second moderator. A skip choice or the card's decline, in "
    "any language, keyed or not, "
    "drops it. The moderator ends with a brief, never questions: on its done, post "
    "the brief link and its one-line conclusion, then an [OPTIONS:] choice in the "
    "owner's language to run another grill; picking it gives a new card whose q is "
    "the point the owner disliked. A formal debate runs only when the owner names "
    "one.)"
)


def worker_grill_enabled(cfg: Any) -> bool:
    """Whether *cfg* turns the preview on. ``False`` on anything unexpected."""
    from kiro_crew.edition_locks import feature_previews

    try:
        previews = feature_previews(
            getattr(getattr(cfg, "dashboard", None), "feature_previews", None)
        )
    except Exception:  # noqa: BLE001 -- an unreadable switch reads as off
        return False
    return isinstance(previews, dict) and previews.get(WORKER_GRILL_PREVIEW) is True


def guidance_for(agent: str | None, cfg: Any) -> str:
    """:data:`GUIDANCE` for a conductor *agent* while *cfg* has the preview on, else ``""``."""
    if not agent or agent not in GRILL_AGENTS:
        return ""
    return GUIDANCE if worker_grill_enabled(cfg) else ""
