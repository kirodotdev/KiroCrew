"""Feature-use counters: did a person's or an agent's use of a feature happen.

=============================================  ====================================
counter                                        one increment per
=============================================  ====================================
``kirocrew.memory.recalled``                   explicit recall, or admitted turn
                                               whose prompt composition placed
                                               memory (``path``: tool / context)
``kirocrew.lesson.injected``                   admitted turn composed with a lesson
                                               tier (``hit``: a lesson went in)
``kirocrew.app.task.completed``                unit of work an app reports finished
``kirocrew.channel.message.received``          inbound channel message, once
                                               admission has decided
``kirocrew.ui.control.clicked``                click on an agent-rendered control
=============================================  ====================================

**Every attribute value is closed.** Each list is a tuple below, and each
``record_*`` function maps anything outside it to ``other`` (or the bucket the
list names for it). ``test/metrics/test_feature_usage.py`` pins every list. No
query, result, lesson key, skill name the operator chose, app name a third party
chose, channel or sender id, or control text is ever an attribute.

**Skill and app ids are exported only for what Kiro Crew ships.** An operator's
own skill can be named after what they work on, so a name outside
:data:`SHIPPED_SKILL_IDS` is one bucket, ``custom``. The same holds for apps: an
app that is not one of :data:`OFFICIAL_APP_IDS` is ``private``. Both lists are
spelled out in this module, so neither importing it nor labelling a value reads
the filesystem.

**Recording never changes the result of what it measures.** Every function here
swallows its own failures after a debug log, and each call site records only
after the action it measures has committed. Recording is an in-memory add: no
disk write, no network call, no log line.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

MEMORY_RECALLED = "kirocrew.memory.recalled"
LESSON_INJECTED = "kirocrew.lesson.injected"
#: The on-demand skill body load counter, emitted by ``skills_body_reads``. It
#: carries the closed ``skill`` label from :func:`skill_label`.
SKILL_LAZY_LOAD_COUNT = "kirocrew.skill.lazy_load.count"
APP_TASK_COMPLETED = "kirocrew.app.task.completed"
CHANNEL_MESSAGE_RECEIVED = "kirocrew.channel.message.received"
UI_CONTROL_CLICKED = "kirocrew.ui.control.clicked"

OTHER = "other"
BOOL_VALUES: tuple[str, ...] = ("true", "false")


def _bool(value: object) -> str:
    return "true" if value else "false"


def _emit(name: str, attrs: dict[str, str | int | bool | float]) -> None:
    """Add 1 to *name*; never raises."""
    try:
        from kiro_crew.metrics.events import emit_counter

        emit_counter(name, attrs)
    except Exception:  # telemetry must never break the measured path
        logger.debug("feature counter emit failed for %s", name, exc_info=True)


# -- memory.recalled ----------------------------------------------------------

#: ``tool``: an explicit ``memory_recall`` call. ``context``: memory placed into
#: an admitted turn's prompt by context composition.
MEMORY_PATH_VALUES: tuple[str, ...] = ("tool", "context")

#: The context blocks (``context_blocks._MARKERS`` labels) that carry memory.
MEMORY_BLOCKS: tuple[str, ...] = ("memory", "semantic_memory", "episodic_memory")


def record_memory_recalled(path: str, *, hit: bool) -> None:
    """One recall on *path*; *hit* is whether anything came back."""
    _emit(
        MEMORY_RECALLED,
        {"path": path if path in MEMORY_PATH_VALUES else OTHER, "hit": _bool(hit)},
    )


def recall_payload_hit(payload: object) -> bool:
    """Whether a ``/api/memory/recall`` response carried any memory; never raises."""
    try:
        if not isinstance(payload, dict):
            return False
        retrieval = payload.get("retrieval")
        if isinstance(retrieval, dict):
            for key in ("facts", "episodes"):
                if retrieval.get(key):
                    return True
        for key in ("context", "facts", "episodes", "results"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return True
            if isinstance(value, (list, dict)) and value:
                return True
        return False
    except Exception:
        return False


# -- lesson.injected ----------------------------------------------------------

#: The receipt kinds (``injection_receipts.KINDS``) that are lesson tiers.
#: ``findings`` is a separate tier and is not a lesson.
LESSON_RECEIPT_KINDS: tuple[str, ...] = ("lessons", "turn_lessons")


def record_context_composition(blocks: object, receipts: object) -> None:
    """Record the memory and lesson readings of one ADMITTED turn's composition.

    *blocks* is the turn's ``split_blocks`` label -> size mapping and *receipts*
    the lesson receipts its composition collected. Called once per admitted turn,
    beside ``on_context_composed``; never raises.

    ``memory.recalled{path=context}`` is recorded for every admitted turn, with
    ``hit`` true when a memory block is non-empty. ``lesson.injected`` is recorded
    only when the composition rendered a lesson tier, with ``hit`` true when at
    least one lesson went in -- by key, or as unnamed rows from a store that keeps
    no key (then the lesson block itself is non-empty).
    """
    try:
        sizes = blocks if isinstance(blocks, dict) else {}
        memory_hit = any(_positive(sizes.get(label)) for label in MEMORY_BLOCKS)
        record_memory_recalled("context", hit=memory_hit)
        rec = receipts if isinstance(receipts, dict) else {}
        tiers = [rec[kind] for kind in LESSON_RECEIPT_KINDS if kind in rec]
        if tiers:
            any_id = any(isinstance(t, dict) and t.get("ids") for t in tiers)
            _emit(LESSON_INJECTED, {"hit": _bool(any_id or _positive(sizes.get("lessons")))})
    except Exception:
        logger.debug("context composition counters failed", exc_info=True)


def _positive(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


# -- skill.loaded -------------------------------------------------------------

#: The skills synced into an install from the repository's ``skills/`` directory.
#: Spelled out because that directory is not part of the installed package; a
#: test compares it with the directory in a source checkout.
REPO_SKILL_IDS: tuple[str, ...] = (
    "gateway-restart",
    "goal-loop",
    "grill",
    "kirocrew-app-dev",
    "rubber-duck",
    "self-nudge-loop",
    "self-rca",
    "self-update",
)


#: The skills the package ships under ``builtin_skills/``, nested ones by their
#: slash-separated path (``kirocrew-dev/writing-tests``) -- the name ``load_skill``
#: is called with. Spelled out so labelling a load reads no directory; a test
#: compares it with the package tree.
BUILTIN_SKILL_IDS: tuple[str, ...] = (
    "artifacts",
    "blocked-by-policy",
    "browser-auth",
    "browser-recording",
    "computer-use",
    "cron-cost-optimize",
    "crystallize",
    "dashboard",
    "debug-gateway",
    "explain-for",
    "feature-request",
    "five-whys",
    "frontend-design-workflow",
    "goal-conductor",
    "goal-ledger-conductor",
    "image-authoring",
    "ios-simulator-preview",
    "kirocrew-commands",
    "kirocrew-dev/babysit",
    "kirocrew-dev/dashboard-template",
    "kirocrew-dev/kirocrew-prepare-pr",
    "kirocrew-dev/kirocrew-worktree-dev",
    "kirocrew-dev/writing-tests",
    "living-ui",
    "llm-council",
    "meetings",
    "ops-mission-control",
    "papyrus-diagnose-compilation",
    "papyrus-latex-comments",
    "papyrus-latex-suggestions",
    "papyrus-make-fluent",
    "papyrus-writing",
    "pipeline-conductor",
    "pptx-maker",
    "remote-crew-microvm",
    "security-conductor",
    "session-summaries",
    "team-lead",
    "theme-pack-authoring",
    "web-browse",
    "web-preview",
    "web-verify",
    "widgets",
)


#: Every skill id Kiro Crew ships. Only these are sent as themselves.
SHIPPED_SKILL_IDS: frozenset[str] = frozenset(BUILTIN_SKILL_IDS) | frozenset(REPO_SKILL_IDS)

SKILL_CUSTOM = "custom"

_SAFE_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/]{0,127}$")


def skill_label(name: object) -> str:
    """A shipped skill id, ``custom`` for any other safe name, else ``other``."""
    if isinstance(name, str):
        if name in SHIPPED_SKILL_IDS:
            return name
        if _SAFE_SKILL_NAME.match(name) and ".." not in name:
            return SKILL_CUSTOM
    return OTHER


# -- app.task.completed -------------------------------------------------------


#: The official apps that report a unit of work, by their ``apps/builtins/``
#: directory name. An app joins this list in the change that makes it report, so
#: the list never names an app that sends nothing; a test holds every
#: ``record_app_task`` call in the package to an id listed here.
_OFFICIAL_APP_ID_LIST: tuple[str, ...] = ("papyrus",)

#: Every official app id. Any other app reports :data:`APP_PRIVATE`.
OFFICIAL_APP_IDS: frozenset[str] = frozenset(_OFFICIAL_APP_ID_LIST)

APP_PRIVATE = "private"

APP_TASK_OUTCOME_VALUES: tuple[str, ...] = ("ok", "error", OTHER)


def app_label(app: object) -> str:
    """An official app id, else ``private``."""
    if isinstance(app, str) and app in OFFICIAL_APP_IDS:
        return app
    return APP_PRIVATE


def record_app_task(app: object, outcome: object) -> None:
    """One unit of work *app* finished with *outcome* (``ok`` / ``error``)."""
    value = outcome if isinstance(outcome, str) and outcome in APP_TASK_OUTCOME_VALUES else OTHER
    _emit(APP_TASK_COMPLETED, {"app": app_label(app), "outcome": value})


# -- channel.message.received -------------------------------------------------


def _channel_values() -> tuple[str, ...]:
    from kiro_crew.constants import CHANNEL_SESSION_NAMESPACES

    return tuple(CHANNEL_SESSION_NAMESPACES) + (OTHER,)


#: The channel namespaces plus ``other``.
CHANNEL_VALUES: tuple[str, ...] = _channel_values()


def channel_label(session_key: object) -> str:
    """The transport namespace of *session_key*, else ``other``; never raises."""
    try:
        from kiro_crew.messaging.link import channel_namespace_of

        ns = channel_namespace_of(session_key) if isinstance(session_key, str) else None
        return ns if ns in CHANNEL_VALUES else OTHER
    except Exception:
        return OTHER


def record_channel_message(session_key: object, *, admitted: bool, automation: bool) -> None:
    """One inbound channel message after admission decided."""
    _emit(
        CHANNEL_MESSAGE_RECEIVED,
        {
            "channel": channel_label(session_key),
            "admitted": _bool(admitted),
            "automation": _bool(automation),
        },
    )


# -- ui.control.clicked -------------------------------------------------------

#: The agent-rendered controls a click can be reported for.
UI_CONTROL_VALUES: tuple[str, ...] = (
    "option_chip",
    "question_card_answer",
    "question_card_freetext",
    "followup_start_worktree",
    "followup_start_chat",
    "followup_add_session",
    "followup_skip",
    OTHER,
)


def ui_control_label(control: object) -> str:
    """*control* when it is in :data:`UI_CONTROL_VALUES`, else ``other``."""
    return control if isinstance(control, str) and control in UI_CONTROL_VALUES else OTHER


def record_ui_control_clicked(control: object) -> None:
    """One click on an agent-rendered control."""
    _emit(UI_CONTROL_CLICKED, {"control": ui_control_label(control)})
