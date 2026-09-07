"""Feature videos — deterministic, one-shot feature-intro clips for the dashboard.

Sibling of :mod:`kiro_crew.tips`, and deliberately NOT built like it. A tip is
generated: the model picks a feature from a catalog and writes prose about it, so
the engine is a cadence gate plus a weighted-random selector over a pool that
changes every six hours. A video is a shipped artifact — a recorded clip with a
title and a poster — so nothing about it can be generated at request time, and
"which one do I show" has exactly one right answer for a given install state.

That makes the rule set DETERMINISTIC:

* the catalog is a static tuple in this module (:data:`CATALOG`), not a doc scan
  and not an LLM call;
* selection walks the catalog in order and returns the FIRST entry that is
  eligible, so two requests one second apart cannot disagree;
* "has the user already used this feature?" is answered by named probes
  (:data:`_PROBES` / :data:`_PARAM_PROBES`) that read local state, never by a
  model's guess.

Both statuses a user can record (``seen`` / ``dismissed``) are PERMANENT. There
is no snooze, because a feature intro is not a recurring nudge: once it has been
watched or waved away, showing it again is noise.

Asset paths are same-origin relative paths under ``/app-assets/feature-videos/``
and are validated as such (:func:`validate_asset_path`). Downloading a clip from
a remote origin is a separate, future change — see the note in that function for
the single place the rule would relax.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from aiohttp import web

import kiro_crew
from kiro_crew.apps.version import parse_version
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig, config_local_path, config_path
from kiro_crew.config.paths import config_dir
from kiro_crew.dashboard.handlers._shared import (
    _blocks_reads_session,
    _is_restricted_session,
    read_bounded_json,
)
from kiro_crew.sel import sel
from kiro_crew.tips_allowlist import TIP_DOC_ALLOWLIST

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: Every shipped clip and poster lives under this same-origin prefix, served
#: from ``website/public/app-assets/`` like every other bundled app asset.
ASSET_PREFIX = "/app-assets/feature-videos/"

#: Bound on a CATALOG entry's ``id``. The feedback route does not use it: there,
#: catalog membership is the tighter check and already closes the state file's
#: key set to the shipped slugs.
_VIDEO_ID_MAX_CHARS = 100

#: How far back a ``sel_event_seen`` probe reads. The SEL log is scanned
#: backward from the tail, so this is a real cost ceiling rather than a hint.
#: A tool used so long ago that it has fallen past this window reads as "not
#: used", which shows the video again — the safe direction for a probe
#: (see :func:`probe_fires`).
_SEL_PROBE_LIMIT = 500

#: The two statuses a user can record. Both are permanent.
VALID_STATUSES = ("seen", "dismissed")

#: Serializes the feedback route's read-modify-write of the state file. The
#: record step loads, mutates one key and saves, so two tabs recording different
#: videos at once would otherwise have the second save overwrite the first
#: video's row and offer it again. A ``threading.Lock`` rather than an
#: ``asyncio.Lock`` because the critical section runs in an executor THREAD, and
#: because a module-level asyncio primitive binds to whichever loop first
#: awaited it — the defect ``LoopBoundLock`` exists for. It holds no per-caller
#: data: the state it guards is one instance-wide file.
_state_write_lock = threading.Lock()


# ── Catalog ──


@dataclass(frozen=True)
class VideoEntry:
    """One shipped feature-intro clip.

    Frozen: the catalog is a constant, and a handler that could mutate an entry
    in place would leak one request's edit into every later request in the
    process.
    """

    #: Stable slug. Doubles as the state-file key and the asset basename.
    id: str
    #: The feature this clip introduces, as the docs name it.
    feature: str
    title: str
    description: str
    #: Same-origin relative path to the clip (see :func:`validate_asset_path`).
    src: str
    #: Same-origin relative path to the still frame shown before playback.
    poster: str
    duration_s: float
    #: User-facing doc for the feature. Must be in ``TIP_DOC_ALLOWLIST`` — the
    #: same gate tips use, so a video cannot point at an internal design note.
    doc: str
    #: Deterministic "the user already found this feature" signals. ANY of them
    #: firing withdraws the video: an intro for a feature already in use is the
    #: one thing a feature intro must not do.
    used_when: tuple[str, ...] = ()
    #: Minimum running version, or ``""`` for no floor. A clip recorded against
    #: a feature that does not exist on this build must not be offered.
    min_version: str = ""

    def payload(self) -> dict[str, object]:
        """The client-facing shape: everything except the probe signals.

        ``used_when`` is withheld on purpose. It names local state the frontend
        has no business reading, and shipping it would invite a client to
        re-evaluate eligibility itself and drift from this module's answer.
        """
        return {
            "id": self.id,
            "feature": self.feature,
            "title": self.title,
            "description": self.description,
            "src": self.src,
            "poster": self.poster,
            "duration_s": self.duration_s,
            "doc": self.doc,
            "min_version": self.min_version,
        }


def validate_asset_path(value: object) -> str:
    """Return *value* if it is a safe same-origin asset path, else ``""``.

    A video element's ``src`` is fetched by the browser with the dashboard's own
    credentials, so an attacker-controlled value here is an outbound request the
    user authorized without knowing it. Everything that could redirect the fetch
    off this origin, or walk out of the asset directory, is refused:

    * a scheme (``http:``, ``data:``, ``javascript:``) — any ``:`` at all, which
      also catches a Windows drive letter;
    * a protocol-relative ``//`` prefix, and any ``//`` elsewhere (an empty path
      segment is never meaningful for a bundled asset);
    * ``..`` in any form, plus ``%`` so a percent-encoded ``%2e%2e`` cannot
      reconstitute one after the browser decodes it;
    * a backslash, which some clients normalize to ``/``;
    * anything outside :data:`ASSET_PREFIX`.

    Returns the empty string rather than raising: a bad path in the shipped
    catalog is a bug, but it must degrade to "this entry is not offered" rather
    than 500 every ``/api/feature-videos/*`` request until it is fixed.

    Cloud-hosted clips are a future change. When they land, the relaxation goes
    HERE and nowhere else: accept an ``https://`` URL whose host is on an
    explicit allowlist, keep rejecting every other scheme, and keep the
    traversal rules for the relative case. Do not relax the caller instead —
    every entry reaches the client through this one function.
    """
    if not isinstance(value, str) or not value:
        return ""
    if ":" in value or "//" in value or ".." in value or "%" in value or "\\" in value:
        return ""
    if any(ch.isspace() or ord(ch) < 0x20 for ch in value):
        return ""
    if not value.startswith(ASSET_PREFIX):
        return ""
    # A trailing slash means the prefix itself was passed with no filename.
    if len(value) <= len(ASSET_PREFIX):
        return ""
    return value


def _entry_is_valid(entry: VideoEntry) -> bool:
    """Whether *entry* is safe to offer. Logs the reason when it is not."""
    reason = ""
    if not entry.id or len(entry.id) > _VIDEO_ID_MAX_CHARS:
        reason = "id missing or too long"
    elif not validate_asset_path(entry.src):
        reason = f"unsafe src {entry.src!r}"
    elif not validate_asset_path(entry.poster):
        reason = f"unsafe poster {entry.poster!r}"
    elif entry.doc not in TIP_DOC_ALLOWLIST:
        reason = f"doc {entry.doc!r} is not in the tips doc allowlist"
    elif entry.min_version:
        try:
            parse_version(entry.min_version)
        except ValueError:
            reason = f"unparseable min_version {entry.min_version!r}"
    if reason:
        logger.warning("feature video %r dropped from the catalog: %s", entry.id, reason)
        return False
    return True


#: The shipped catalog, in offer order. Seeded with the two features whose
#: "have you found this yet?" signal is cheapest to answer honestly.
CATALOG: tuple[VideoEntry, ...] = (
    VideoEntry(
        id="feature-tips",
        feature="feature-tips",
        title="Feature tips above the composer",
        description=(
            "A short card appears above the composer while a turn runs, pointing at a "
            "feature you have not used yet. Dismiss one and it stays gone."
        ),
        src=f"{ASSET_PREFIX}feature-tips.mp4",
        poster=f"{ASSET_PREFIX}feature-tips.jpg",
        duration_s=18.0,
        doc="feature-tips.md",
        used_when=("tips_feedback_exists",),
    ),
    VideoEntry(
        id="monitor-loops",
        feature="monitor-loops",
        title="Let one session watch a pull request",
        description=(
            "A monitor loop re-injects your own check instructions into this session on "
            "an interval, so one session can follow a pull request or a CI run to done."
        ),
        src=f"{ASSET_PREFIX}monitor-loops.mp4",
        poster=f"{ASSET_PREFIX}monitor-loops.jpg",
        duration_s=22.0,
        doc="monitor-loops.md",
        used_when=("sel_event_seen:monitor_start",),
    ),
)


def catalog() -> tuple[VideoEntry, ...]:
    """The catalog with unsafe entries filtered out.

    This is STRUCTURAL validity only -- a well-formed entry whose media has not
    shipped yet is still in here. That is deliberate: the feedback route checks
    membership against this set, and a user who has already been shown a clip
    must be able to record a verdict on it even if its asset later goes missing.
    :func:`offerable` is the set that may actually be shown.
    """
    return tuple(e for e in CATALOG if _entry_is_valid(e))


def _asset_root() -> Path:
    """Where ``ASSET_PREFIX`` is served from on disk.

    ``server.py`` mounts ``static/dist/app-assets`` at ``/app-assets``, so a
    catalog ``src`` of ``/app-assets/feature-videos/x.mp4`` is the file
    ``<static>/dist/app-assets/feature-videos/x.mp4``. Resolved through a
    function, not a constant, so a test can point it at a temp directory.
    """
    return Path(__file__).resolve().parent / "static" / "dist" / "app-assets"


def _asset_exists(url_path: str) -> bool:
    """Whether the file behind a validated ``/app-assets/...`` path is on disk."""
    prefix = "/app-assets/"
    if not url_path.startswith(prefix):
        return False
    return (_asset_root() / url_path[len(prefix) :]).is_file()


def offerable() -> tuple[VideoEntry, ...]:
    """The entries that may be SHOWN: valid, and with both media files on disk.

    "Asset shipped" is a precondition of "on offer", enforced here rather than
    trusted to the client. The dialog opens on the JSON answer alone and its
    ``<video>`` is ``preload="none"``, so nothing is fetched -- and no media error
    can fire -- until the user presses play. An entry whose clip is not shipped
    would therefore open a dialog around a blank player, and the natural "Got it"
    writes a PERMANENT verdict, retiring the real intro before anyone saw it.
    Dropping such an entry here keeps it on offer for the launch after its clip
    lands, which is the recoverable outcome.
    """
    kept: list[VideoEntry] = []
    for entry in catalog():
        missing = [p for p in (entry.src, entry.poster) if not _asset_exists(p)]
        if missing:
            logger.info(
                "feature video %r withheld: asset(s) not shipped: %s", entry.id, ", ".join(missing)
            )
            continue
        kept.append(entry)
    return tuple(kept)


# ── "Already used this feature" probes ──


def _probe_tips_feedback_exists() -> bool:
    """True once the user has reacted to a feature tip in any way.

    Reads the tips state file directly rather than importing the tips runtime:
    the two engines share a data home, not a code path, and a probe must not
    drag an LLM-bearing module (and its cache init) into a route that is polled.

    ``opted_out`` counts, and is checked SEPARATELY from the collection keys
    below rather than added to them. Turning tips off in Settings is the
    strongest reaction a user can have to the feature, but it is the one that
    writes no collection and leaves ``last_shown_ts`` at ``0.0`` — so a user who
    opted out before the cadence gate ever opened is exactly the person the
    collection scan reads as never having seen a tip, and the person the intro
    would then be played to.
    """
    path = config_dir() / "tips_state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        # RecursionError for the same reason load_state catches it: a nested
        # tips_state.json must degrade to "no feedback", never to a 500.
        return False
    if not isinstance(data, dict):
        return False
    if data.get("opted_out") is True:
        return True
    for key in ("shown", "dismissed", "dismissed_docs", "snoozed", "snoozed_docs"):
        value = data.get(key)
        if isinstance(value, (dict, list)) and value:
            return True
    last_shown = data.get("last_shown_ts")
    if isinstance(last_shown, (int, float)) and not isinstance(last_shown, bool):
        return bool(last_shown > 0)
    return False


def _probe_artifacts_nonempty() -> bool:
    """True when the artifact library holds at least one artifact.

    Scans directory entries and stops at the first hit instead of going through
    ``ArtifactStore.list()``, which reads every ``meta.json`` — this runs on a
    polled route, and the question is only "any at all?".
    """
    root = config_dir() / "artifacts"
    try:
        with os.scandir(root) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir() and (Path(entry.path) / "meta.json").is_file():
                    return True
    except OSError:
        return False
    return False


def _probe_sel_event_seen(tool_name: str) -> bool:
    """True when the audit log carries a recent row naming *tool_name*.

    The SEL read is bounded on both ends (:data:`_SEL_PROBE_LIMIT`, tail-first),
    so this stays cheap on a large log.
    """
    if not tool_name:
        return False
    for row in sel().recent(limit=_SEL_PROBE_LIMIT):
        if not isinstance(row, dict):
            continue
        if row.get("operation") == tool_name:
            return True
    return False


def _probe_config_key_set(dotted: str) -> bool:
    """True when the user has explicitly set *dotted* in their config on disk.

    Deliberately reads the FILES rather than the loaded config: every key in the
    effective config has a value, so an effective-config read would fire on the
    shipped default and withdraw the video from someone who never touched the
    setting. Presence in ``config.json`` or ``config.local.json`` is the actual
    "the user configured this" signal.

    Presence, not truth — so this signal says "the user has an opinion about
    this key", which is not the same as "the feature is on". Do not reach for it
    to express "the operator disabled X": the same probe fires when they
    explicitly enabled it.
    """
    if not dotted:
        return False
    for path in (config_path(), config_local_path()):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            continue
        cur: object = raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                break
            cur = cur[part]
        else:
            return True
    return False


#: Probes taking no argument, keyed by the exact signal name.
_PROBES: dict[str, Callable[[], bool]] = {
    "tips_feedback_exists": _probe_tips_feedback_exists,
    "artifacts_nonempty": _probe_artifacts_nonempty,
}

#: Probes taking one argument, keyed by the part before the first ``:``.
_PARAM_PROBES: dict[str, Callable[[str], bool]] = {
    "sel_event_seen": _probe_sel_event_seen,
    "config_key_set": _probe_config_key_set,
}


def probe_fires(signal: str) -> bool:
    """Evaluate one ``used_when`` signal. Blocking — call off the event loop.

    An unknown signal and a raising probe both answer False, i.e. "the user has
    NOT used this feature", i.e. show the video. That is the safe direction:
    the failure mode is one clip a user may not need, where the opposite
    default would silently withhold every intro on a host whose audit log or
    artifact directory happens to be unreadable. Both cases are logged, because
    a probe that never fires looks exactly like a feature nobody uses.
    """
    name, separator, arg = signal.partition(":")
    try:
        if not separator:
            fn = _PROBES.get(name)
            if fn is None:
                logger.warning("unknown feature-video used_when signal %r", signal)
                return False
            return bool(fn())
        param_fn = _PARAM_PROBES.get(name)
        if param_fn is None:
            logger.warning("unknown feature-video used_when signal %r", signal)
            return False
        return bool(param_fn(arg))
    except Exception:
        logger.warning("feature-video probe %r failed; treating as unused", signal, exc_info=True)
        return False


# ── State ──


@dataclass
class FeatureVideoState:
    """Persisted per-video display state: ``id -> {"status": ..., "ts": ...}``."""

    videos: dict[str, dict[str, object]] = field(default_factory=dict)

    def status_of(self, video_id: str) -> str:
        row = self.videos.get(video_id)
        if not isinstance(row, dict):
            return ""
        status = row.get("status")
        return status if isinstance(status, str) and status in VALID_STATUSES else ""


def _state_path() -> Path:
    # Beside tips_state.json, through the same path helper: KIROCREW_HOME
    # tilde-expansion and unsafe-system-directory rejection must match the rest
    # of the config stack, which a raw os.environ read would not.
    return config_dir() / "feature_videos_state.json"


def _finite_ts(value: object) -> float:
    """Coerce a persisted timestamp to a finite float, or ``0.0``.

    ``float()`` on a several-hundred-digit JSON integer raises ``OverflowError``,
    which is NOT a subclass of the ``ValueError`` the loader catches — so
    without this a hand-edited state file would take down every
    ``/api/feature-videos/*`` request with a 500 until it was repaired by hand.
    The same guard tips' ``_finite`` carries, for the same reason.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def load_state() -> FeatureVideoState:
    """Read the state file, degrading to empty on anything unexpected.

    Per-entry validation, not just a root type check: a syntactically valid file
    carrying ``{"videos": {"x": 3}}`` would otherwise crash the selector and 500
    every endpoint until someone repaired the file by hand.
    """
    path = _state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        # RecursionError is neither an OSError nor a ValueError: json.loads
        # raises it on JSON nested past the interpreter's recursion limit. Our
        # own writer only ever emits a three-level document, so reaching this
        # needs a hand-edited file -- but the file is on disk and an operator
        # can edit it, and the alternative is a 500 on every feature-video
        # endpoint until someone works out which file to repair. Degrading to
        # "no state recorded" re-offers a clip at worst.
        return FeatureVideoState()
    if not isinstance(data, dict):
        logger.warning("feature_videos_state.json has non-dict root; using defaults")
        return FeatureVideoState()
    raw = data.get("videos")
    if not isinstance(raw, dict):
        return FeatureVideoState()
    videos: dict[str, dict[str, object]] = {}
    for key, row in raw.items():
        if not isinstance(key, str) or not isinstance(row, dict):
            continue
        status = row.get("status")
        if not isinstance(status, str) or status not in VALID_STATUSES:
            continue
        videos[key] = {"status": status, "ts": _finite_ts(row.get("ts"))}
    return FeatureVideoState(videos=videos)


def save_state(st: FeatureVideoState) -> None:
    """Persist *st* owner-only."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        path,
        json.dumps({"videos": st.videos}, indent=2) + "\n",
        # Owner-only, for the same reason tips_state.json is: the file records
        # which features this user has and has not engaged with, which is a
        # behavioural profile and must not be world-readable on a shared box.
        # restrict_to_owner locks the temp file down BEFORE content reaches it
        # (a post-rename lockdown leaves the payload readable under the
        # inherited DACL on Windows), implies 0o600 on POSIX — which also
        # corrects a pre-existing 0644 file on the next write — and applies the
        # owner-only DACL on Windows. Warn-and-continue: a lockdown failure must
        # not break persistence, but it must be visible.
        restrict_to_owner=True,
        restrict_on_error="warn",
    )


def record_status(video_id: str, status: str) -> None:
    """Record one permanent display status. Blocking — call off the loop.

    Load, mutate, save under :data:`_state_write_lock`: the three steps are one
    transaction, and two tabs recording different videos concurrently would
    otherwise both read the same bytes and the later save would drop the earlier
    video's row — offering an already-dismissed clip again.
    """
    with _state_write_lock:
        st = load_state()
        st.videos[video_id] = {"status": status, "ts": time.time()}
        save_state(st)


# ── Selection ──


def _version_ok(min_version: str, running: str) -> bool:
    """Whether *running* satisfies *min_version*.

    An unparseable RUNNING version passes: the floor exists to hide a clip for a
    feature this build does not have, and refusing to decide would hide EVERY
    floored clip on a build whose own version string cannot be read. An
    unparseable FLOOR fails, because a floor nobody can evaluate is not a floor.
    """
    if not min_version:
        return True
    try:
        floor = parse_version(min_version)
    except ValueError:
        return False
    try:
        return parse_version(running) >= floor
    except ValueError:
        return True


def select_next(running_version: str) -> VideoEntry | None:
    """First eligible catalog entry, or None. Blocking — call off the loop.

    Order is the catalog's own order, so the answer is stable across requests.
    Probes are evaluated LAST and lazily: they are the only expensive part, and
    an entry already ruled out by state or version must not pay for them.
    """
    st = load_state()
    for entry in offerable():
        if st.status_of(entry.id):
            continue
        if not _version_ok(entry.min_version, running_version):
            continue
        if any(probe_fires(signal) for signal in entry.used_when):
            continue
        return entry
    return None


# ── HTTP handlers ──


def _enabled() -> bool:
    return bool(KiroCrewConfig.load().dashboard.feature_videos_enabled)


async def api_feature_videos_next(request: web.Request) -> web.Response:
    """GET /api/feature-videos/next — the next intro clip to play, or null.

    Returns ``{"video": <entry-without-used_when> | null, "enabled": <bool>}``.

    ``enabled`` is reported even when it is false, rather than a 204: the
    settings panel and the modal both need to tell "the operator turned this
    off" apart from "nothing left to show", and a bodiless response cannot.
    """
    state: DashboardState = request.app["state"]
    loop = asyncio.get_running_loop()

    enabled = await loop.run_in_executor(None, _enabled)
    if not enabled:
        return web.json_response({"video": None, "enabled": False})

    # A temporary or incognito session shows no intro. The modal records
    # permanent state for the whole instance, and a session the user opened
    # precisely so it would leave no trace must not write that — the same reason
    # tips do not fetch in a temporary session.
    if _is_restricted_session(state, request):
        return web.json_response({"video": None, "enabled": True})

    entry = await loop.run_in_executor(None, select_next, kiro_crew.__version__)
    return web.json_response({"video": entry.payload() if entry else None, "enabled": True})


async def api_feature_videos_status(request: web.Request) -> web.Response:
    """GET /api/feature-videos/status — kill-switch state plus the whole state map.

    Returns ``{"enabled": <bool>, "state": {<id>: {"status", "ts"}}}`` for the
    settings panel, which needs to render (and later reset) what has been seen.

    The state map is a READ of engagement history, so it is gated by
    ``_blocks_reads_session`` rather than ``_is_restricted_session``. That is the
    product's own read/write split, not a looser gate: incognito withholds
    WRITES, and a temporary session withholds reads as well. ``/next`` uses the
    broader predicate because reaching it leads to a permanent write, while this
    route only reads — so an incognito session still renders its own settings
    panel, and only a read-blocking session is served an empty map.

    ``enabled`` is reported truthfully either way: the kill switch is instance
    configuration, not history, and withholding it would make the panel claim
    the feature is off.
    """
    state: DashboardState = request.app["state"]
    loop = asyncio.get_running_loop()
    enabled = await loop.run_in_executor(None, _enabled)
    if _blocks_reads_session(state, request):
        return web.json_response({"enabled": enabled, "state": {}})
    st = await loop.run_in_executor(None, load_state)
    return web.json_response({"enabled": enabled, "state": st.videos})


async def api_feature_videos_feedback(request: web.Request) -> web.Response:
    """POST /api/feature-videos/feedback — record a permanent display status.

    Body: ``{"id": <catalog id>, "status": "seen" | "dismissed"}``.

    Both statuses are terminal: there is no snooze, so a recorded video is never
    offered again. The id must name a CATALOG entry — an unknown id is a client
    bug, and accepting it would let an unbounded set of keys accumulate in the
    state file forever. That membership check is the only id validation, since
    it is strictly tighter than any length or shape bound.
    """
    state: DashboardState = request.app["state"]

    body, err = await read_bounded_json(request)
    if err is not None:
        return err
    if body is None:  # pragma: no cover — read_bounded_json returns one or the other
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)

    video_id = body.get("id", "")
    status = body.get("status", "")
    if not isinstance(video_id, str) or not isinstance(status, str):
        return web.json_response(
            {"error": "id and status must be strings", "code": "invalid_field_type"},
            status=400,
        )
    if status not in VALID_STATUSES:
        return web.json_response({"error": "invalid status", "code": "invalid_status"}, status=400)
    # Catalog membership is the ONLY id check, deliberately: it is strictly
    # tighter than any length bound, so an oversized id is already refused here
    # as "not a shipped slug". A separate length branch would ship a second
    # permanent `code` for a case this one fully covers, and a `code` is API
    # surface that cannot be narrowed later. This check is also what keeps the
    # state file's key set closed to the shipped catalog.
    if video_id not in {e.id for e in catalog()}:
        return web.json_response({"error": "unknown video id", "code": "unknown_video"}, status=400)

    # The write side needs the SAME gate the read side has, not just a symmetric
    # gesture: this is where the permanent, instance-wide row is actually
    # created, so gating only /next would leave the trace a restricted session
    # exists to avoid one POST away. Reported as success because nothing the
    # client did was wrong — the session simply keeps no state.
    if _is_restricted_session(state, request):
        return web.json_response({"ok": True})

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, record_status, video_id, status)
    return web.json_response({"ok": True})
