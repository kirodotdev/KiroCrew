"""Feature-use counters: did a person's or an agent's use of a feature happen.

=============================================  ====================================
counter                                        one increment per
=============================================  ====================================
``kirocrew.memory.recalled``                   explicit recall, or admitted turn
                                               whose prompt composition placed
                                               memory (``path``: tool / context)
``kirocrew.lesson.injected``                   admitted turn composed with a lesson
                                               tier (``hit``: a lesson went in)
``kirocrew.app.installed``                     first install of an app from a registry
                                               (``via``: store / default)
``kirocrew.app.updated``                       registry install over an installed app
``kirocrew.app.uninstalled``                   uninstall the person asked for
                                               (``keep_data``)
``kirocrew.app.session_started``               session start owned by an app
                                               (``kind``: its ``session_kind``)


``kirocrew.app.run_time``                      finished turn of an app's session,
                                               adds its seconds (``duration``)
``kirocrew.app.present``                       installed app, once per UTC day
                                               (``status``, ``backend``)
``kirocrew.app.opened``                        app page mounted or ``openCommand``
                                               launched, owner views only
                                               (``surface``)
``kirocrew.app.foreground_time``               seconds an app page was visible,
                                               owner views only (``surface``,
                                               ``duration``)
``kirocrew.channel.message.received``          inbound channel message, once
                                               admission has decided
``kirocrew.ui.control.clicked``                click on an agent-rendered control
``kirocrew.tips.feedback``                     saved reaction to a feature tip
                                               (``action``, ``source``, ``feature``)
``kirocrew.feature_video.status``              saved feature-video status
                                               (``status``, ``feature``)
=============================================  ====================================

**Every attribute value is closed.** Each list is a tuple below, and each
``record_*`` function maps anything outside it to ``other`` (or the bucket the
list names for it). ``test/metrics/test_feature_usage.py`` and
``test/test_recommendation_metrics.py`` pin the lists the ``record_*`` functions read. No
query, result, lesson key, skill name the operator chose, app name a third party
chose, channel or sender id, or control text is ever an attribute.

**Skill and app ids are exported only for what Kiro Crew ships or a store
published.** An operator's own skill can be named after what they work on, so a
name outside :data:`SHIPPED_SKILL_IDS` is one bucket, ``custom``. That list is in
memory at import, so neither importing this module nor labelling a skill reads the
filesystem. :func:`app_public_id`, the label of ``kirocrew.app.installed``, names
the shipped built-ins and the apps installed from the store, and any other app is
``private``; it and :func:`app_public_version`, their version, read a map the
gateway fills off the event loop, so labelling with them reads no file either.

**Recording never changes the result of what it measures.** Every function here
swallows its own failures after a debug log, and each call site records only
after the action it measures has committed. Recording is an in-memory add: no
disk write, no network call, no log line.
"""

from __future__ import annotations

import functools
import logging
import math
import re

logger = logging.getLogger(__name__)

MEMORY_RECALLED = "kirocrew.memory.recalled"
LESSON_INJECTED = "kirocrew.lesson.injected"
#: The on-demand skill body load counter, emitted by ``skills_body_reads``. It
#: carries the closed ``skill`` label from :func:`skill_label`.
SKILL_LAZY_LOAD_COUNT = "kirocrew.skill.lazy_load.count"
APP_INSTALLED = "kirocrew.app.installed"
APP_UPDATED = "kirocrew.app.updated"
APP_UNINSTALLED = "kirocrew.app.uninstalled"
APP_SESSION_STARTED = "kirocrew.app.session_started"
APP_RUN_TIME = "kirocrew.app.run_time"
APP_PRESENT = "kirocrew.app.present"
APP_OPENED = "kirocrew.app.opened"
APP_FOREGROUND_TIME = "kirocrew.app.foreground_time"
CHANNEL_MESSAGE_RECEIVED = "kirocrew.channel.message.received"
UI_CONTROL_CLICKED = "kirocrew.ui.control.clicked"
TIPS_FEEDBACK = "kirocrew.tips.feedback"
FEATURE_VIDEO_STATUS = "kirocrew.feature_video.status"

OTHER = "other"
BOOL_VALUES: tuple[str, ...] = ("true", "false")


def _bool(value: object) -> str:
    return "true" if value else "false"


def _closed(value: object, values: tuple[str, ...]) -> str:
    """*value* when it is one of *values*, else ``other``."""
    return value if isinstance(value, str) and value in values else OTHER


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


# -- app.installed ------------------------------------------------------------


APP_PRIVATE = "private"


#: Every app with a public id (:mod:`kiro_crew.apps.store_origin`), mapped to its
#: version: the shipped built-ins by their ``app.json`` name, and the apps installed
#: from the store. Filled off the event loop by :func:`refresh_public_app_ids`, one
#: assignment per fill; empty until then, so every app is ``private`` before the
#: first fill.
_public_app_ids: dict[str, str] = {}


def refresh_public_app_ids() -> dict[str, str] | None:
    """Re-read which apps have a public id. Reads the install records: call off the loop.

    The gateway runs it at boot and after every app lifecycle change
    (``slots_push.refresh_app_state``), and a registry install runs it before it
    counts the install. Returns the map this call read, so a caller can label from
    its own scan even when a concurrent refresh swaps in an older one. Never raises;
    a failed read keeps the last set and returns ``None``.

    shortcut: only a gateway process fills the set, so a CLI process labels every
    app ``private``; fill it there too once a CLI path records an app metric.
    """
    global _public_app_ids
    try:
        from kiro_crew.apps.store_origin import public_app_ids

        ids = public_app_ids()
    except Exception:
        logger.debug("public app ids refresh failed", exc_info=True)
        return None
    _public_app_ids = ids
    return ids


def app_public_id(app: object, ids: dict[str, str] | None = None) -> str:
    """*app*'s public id: a shipped built-in's or a store install's name, else ``private``.

    Not a string is ``other``. Whether an install came from the store is read from
    its install record, so an app the store later delists keeps its id. Reads only
    memory. *ids* is a map :func:`refresh_public_app_ids` returned; default the last
    one filled.
    """
    if not isinstance(app, str):
        return OTHER
    return app if app in (_public_app_ids if ids is None else ids) else APP_PRIVATE


#: A version an app with a public id sends as itself: three dot-separated numbers.
#: ``SEMVER_RE`` (``apps/manifest_names.py``) lets any text follow ``-`` or ``+``,
#: so a pre-release or build suffix is ``other``.
_PUBLIC_VERSION = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")


def app_public_version(app: object, ids: dict[str, str] | None = None) -> str:
    """*app*'s installed version, the ``entity_version`` label beside :func:`app_public_id`.

    ``private`` whenever :func:`app_public_id` is ``private``, so a private app's
    version never leaves the machine. For an app with a public id, its version when
    it is ``N.N.N`` (at most four digits each, ``1.2.3``), else ``other``
    (``1.2.3-beta``, ``1.2.3+build``, empty). Not a string is ``other``. Reads only
    memory: *ids*, a map :func:`refresh_public_app_ids` returned, else the one it
    last filled.
    """
    known = _public_app_ids if ids is None else ids
    if not isinstance(app, str):
        return OTHER
    if app not in known:
        return APP_PRIVATE
    version = known.get(app)
    return version if isinstance(version, str) and _PUBLIC_VERSION.match(version) else OTHER


#: How an install was asked for: ``store`` from the store or a registry the owner
#: added, ``default`` by the default-apps pass.
APP_INSTALL_VIA_VALUES: tuple[str, ...] = ("store", "default", OTHER)


def record_app_installed(app: object, via: object, ids: dict[str, str] | None = None) -> None:
    """*app* was installed for the first time, asked for *via* (``store`` / ``default``).

    *ids* as in :func:`app_public_id`.
    """
    _emit(
        APP_INSTALLED,
        {
            "app": app_public_id(app, ids),
            "via": _closed(via, APP_INSTALL_VIA_VALUES),
            "entity_version": app_public_version(app, ids),
        },
    )


def record_app_updated(app: object, via: object, ids: dict[str, str] | None = None) -> None:
    """A registry install replaced an installed *app*; labelled with the new version.

    *ids* as in :func:`app_public_id`.
    """
    _emit(
        APP_UPDATED,
        {
            "app": app_public_id(app, ids),
            "via": _closed(via, APP_INSTALL_VIA_VALUES),
            "entity_version": app_public_version(app, ids),
        },
    )


def record_app_uninstalled(public_id: object, public_version: object, *, keep_data: bool) -> None:
    """An uninstall the person asked for committed.

    *public_id* and *public_version* are :func:`app_public_id` and
    :func:`app_public_version` of the app, read BEFORE its install record was
    removed, so they name the version that went. Anything that is not a string
    is ``other``.
    """
    _emit(
        APP_UNINSTALLED,
        {
            "app": public_id if isinstance(public_id, str) else OTHER,
            "entity_version": public_version if isinstance(public_version, str) else OTHER,
            "keep_data": _bool(keep_data),
        },
    )


def record_app_session_started(app: object, kind: object) -> None:
    """A session owned by *app* started; *kind* is its ``session_kind``.

    Called by ``record_session_started`` right after it counts
    ``kirocrew.session.started``, only when the provider's ``owning_app`` (set by
    session allocation from the caller's slot app, never from transcript
    metadata) is not empty, so this never counts more than ``session.started``.
    """
    from kiro_crew.metrics.usage_attrs import SESSION_KIND_VALUES

    _emit(
        APP_SESSION_STARTED,
        {
            "app": app_public_id(app),
            "entity_version": app_public_version(app),
            "kind": _closed(kind, SESSION_KIND_VALUES),
        },
    )


#: The ``duration`` label of an app's timed rows: the upper edge of the bucket the
#: seconds fall in, ``gt_4h`` past four hours, ``other`` when there is no number.
APP_DURATION_VALUES: tuple[str, ...] = (
    "le_10s",
    "le_1m",
    "le_5m",
    "le_15m",
    "le_1h",
    "le_4h",
    "gt_4h",
    OTHER,
)


def app_duration_bucket(seconds: object) -> str:
    """The :data:`APP_DURATION_VALUES` bucket of *seconds*; each edge is in the lower bucket."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return OTHER
    if not 0 < seconds < math.inf:
        return OTHER
    if seconds <= 10:
        return "le_10s"
    if seconds <= 60:
        return "le_1m"
    if seconds <= 300:
        return "le_5m"
    if seconds <= 900:
        return "le_15m"
    if seconds <= 3600:
        return "le_1h"
    if seconds <= 14400:
        return "le_4h"
    return "gt_4h"


def record_app_run_time(owning_app: object, duration_ms: object) -> None:
    """Add one finished turn's wall seconds, rounded up, to its app's run time.

    *owning_app* is the trusted app of the session that ran the turn (the live
    provider's ``owning_app``); empty or not a string records nothing, so a turn
    outside an app adds nothing. A turn with no positive duration adds nothing
    either: a 0 would read as a measured instant turn. The bucket is taken from the
    rounded seconds, so the value and its ``duration`` agree. Never raises.
    """
    if not isinstance(owning_app, str) or not owning_app:
        return
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float)):
        return
    if not 0 < duration_ms < math.inf:
        return
    try:
        from kiro_crew.metrics.provider import get_recorder

        seconds = math.ceil(duration_ms / 1000)
        get_recorder().counter(
            APP_RUN_TIME,
            seconds,
            unit="s",
            attrs={
                "app": app_public_id(owning_app),
                "entity_version": app_public_version(owning_app),
                "duration": app_duration_bucket(seconds),
            },
        )
    except Exception:  # telemetry must never break the measured path
        logger.debug("app run time emit failed", exc_info=True)


#: Whether an installed app is enabled, on ``kirocrew.app.present``.
APP_PRESENT_STATUS_VALUES: tuple[str, ...] = ("enabled", "disabled", OTHER)
#: An installed app's backend process: ``none`` when its manifest runs none
#: (no ``backend.entryPoint``), else ``running`` or ``stopped`` in this gateway.
APP_PRESENT_BACKEND_VALUES: tuple[str, ...] = ("none", "running", "stopped", OTHER)


def record_app_present(
    app: object, status: object, backend: object, ids: dict[str, str] | None = None
) -> None:
    """*app* is installed today; the daily pass in :mod:`kiro_crew.metrics.app_present`.

    *ids* as in :func:`app_public_id`.
    """
    _emit(
        APP_PRESENT,
        {
            "app": app_public_id(app, ids),
            "entity_version": app_public_version(app, ids),
            "status": _closed(status, APP_PRESENT_STATUS_VALUES),
            "backend": _closed(backend, APP_PRESENT_BACKEND_VALUES),
        },
    )


# -- app.opened / app.foreground_time ------------------------------------------
#
# Both come from the owner's own dashboard tabs only, through the owner-gated
# ``POST /api/telemetry/app-view`` (``dashboard/handlers/telemetry_app_view.py``).
# They count owner views, unlike ``session.started`` and ``turn.completed``,
# which count every session.

#: Where an app was opened: its routed page, its pop-out window, or its
#: ``openCommand`` launched from the Library (``desktop``).
APP_OPENED_SURFACE_VALUES: tuple[str, ...] = ("desktop", "page", "popout", OTHER)

#: Where a foreground span was measured. An ``openCommand`` app has no page here.
APP_FOREGROUND_SURFACE_VALUES: tuple[str, ...] = ("page", "popout", OTHER)

#: Longest foreground span counted, in seconds: a longer one is cut to this, so
#: a tab left open across a weekend adds a day, not three.
APP_FOREGROUND_MAX_SECONDS = 24 * 3600


def record_app_opened(app: object, surface: object) -> None:
    """The owner opened *app*'s page (``page`` / ``popout``) or launched it (``desktop``)."""
    _emit(
        APP_OPENED,
        {
            "app": app_public_id(app),
            "entity_version": app_public_version(app),
            "surface": _closed(surface, APP_OPENED_SURFACE_VALUES),
        },
    )


def record_app_foreground(app: object, surface: object, seconds: int) -> None:
    """*app*'s page was visible to the owner for *seconds* whole seconds.

    Adds the seconds, cut to :data:`APP_FOREGROUND_MAX_SECONDS`, in the
    :func:`app_duration_bucket` of what was added. A span under one second adds
    nothing: it is a flicker, not a view. Never raises.
    """
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 1:
        return
    seconds = min(seconds, APP_FOREGROUND_MAX_SECONDS)
    try:
        from kiro_crew.metrics.provider import get_recorder

        get_recorder().counter(
            APP_FOREGROUND_TIME,
            seconds,
            unit="s",
            attrs={
                "app": app_public_id(app),
                "entity_version": app_public_version(app),
                "surface": _closed(surface, APP_FOREGROUND_SURFACE_VALUES),
                "duration": app_duration_bucket(seconds),
            },
        )
    except Exception:  # telemetry must never break the measured path
        logger.debug("feature counter emit failed for %s", APP_FOREGROUND_TIME, exc_info=True)


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


# -- tips.feedback ------------------------------------------------------------

#: The ``POST /api/tips/feedback`` actions, the same list the route accepts.
TIP_ACTION_VALUES: tuple[str, ...] = (
    "shown",
    "ack",
    "dismiss",
    "snooze",
    "helpful",
    "optout",
    "optin",
    OTHER,
)

#: Where the tip came from: a hand-authored tip shipped in ``tips_curated.json``,
#: a catalog tip built from a shipped doc, a tip the model wrote, a one-line tip
#: shipped in ``tips_exploratory.json``, or ``other`` when the id matches none of
#: them (an opt-out carries no tip id at all).
TIP_SOURCE_VALUES: tuple[str, ...] = ("curated", "catalog", "generated", "exploratory", OTHER)


@functools.lru_cache(maxsize=1)
def shipped_curated_tip_ids() -> frozenset[str]:
    """The ids of the tips shipped in the package's ``data/tips_curated.json``.

    Read through the tips module's own loader of the packaged file, never from
    the live tips cache: an edition can replace the curated pool, and its ids
    are not a closed list. Empty when the file cannot be read.
    """
    from kiro_crew.tips import _load_curated_tips

    return frozenset(t["id"] for t in _load_curated_tips())


def tip_feature_label(feature: object, edition_tip_ids: frozenset[str] = frozenset()) -> str:
    """*feature* when it names a shipped tip or doc, else ``other``.

    Kept when it is a doc in ``TIP_DOC_ALLOWLIST`` (the shipped, user-facing
    feature docs), the id of a tip shipped in ``tips_curated.json`` or
    ``tips_exploratory.json``, or one of
    *edition_tip_ids*, the fixed ids an edition pool lists for its own curated
    tips (``TipsPool.metric_tip_ids``). All are lists a build ships, so a kept
    value names nothing the user wrote.
    """
    from kiro_crew.tips_allowlist import TIP_DOC_ALLOWLIST
    from kiro_crew.tips_exploratory import exploratory_tip_ids

    if not isinstance(feature, str) or not feature:
        return OTHER
    if (
        feature in TIP_DOC_ALLOWLIST
        or feature in shipped_curated_tip_ids()
        or feature in exploratory_tip_ids()
        or feature in edition_tip_ids
    ):
        return feature
    return OTHER


def record_tip_feedback(
    action: object,
    source: object,
    feature: object,
    edition_tip_ids: frozenset[str] = frozenset(),
) -> None:
    """One saved tip reaction. Never raises."""
    try:
        _emit(
            TIPS_FEEDBACK,
            {
                "action": _closed(action, TIP_ACTION_VALUES),
                "source": _closed(source, TIP_SOURCE_VALUES),
                "feature": tip_feature_label(feature, edition_tip_ids),
            },
        )
    except Exception:
        logger.debug("tips feedback counter failed", exc_info=True)


# -- feature_video.status -----------------------------------------------------

#: ``feature_videos.VALID_STATUSES`` plus ``other``.
VIDEO_STATUS_VALUES: tuple[str, ...] = ("seen", "dismissed", OTHER)

#: The Explore panel's feature rows (``milestones.feature_rows()``). Spelled out
#: so labelling reads no catalog; a test holds it equal to the live rows.
EXPLORE_FEATURE_IDS: tuple[str, ...] = (
    "widgets",
    "scheduled-task",
    "mate",
    "parallel-chats",
    "slack",
    "pr-watch",
    "crew-mode",
)


def video_feature_label(feature: object) -> str:
    """A feature video's ``feature`` key when it names a shipped feature, else ``other``.

    A key is kept when it is an Explore feature id, a doc in ``TIP_DOC_ALLOWLIST``,
    or the stem of one (``monitor-loops`` for ``monitor-loops.md``, which is how
    the video catalog names its features). A hosted manifest is publisher data,
    so any other key is one bucket.
    """
    from kiro_crew.tips_allowlist import TIP_DOC_ALLOWLIST

    if not isinstance(feature, str) or not feature:
        return OTHER
    if feature in EXPLORE_FEATURE_IDS or feature in TIP_DOC_ALLOWLIST:
        return feature
    if f"{feature}.md" in TIP_DOC_ALLOWLIST:
        return feature
    return OTHER


def record_feature_video_status(status: object, feature: object) -> None:
    """One saved feature-video status. Never raises."""
    try:
        _emit(
            FEATURE_VIDEO_STATUS,
            {
                "status": _closed(status, VIDEO_STATUS_VALUES),
                "feature": video_feature_label(feature),
            },
        )
    except Exception:
        logger.debug("feature video counter failed", exc_info=True)
