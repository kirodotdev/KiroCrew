"""The status payload: the served bundle id, the lesson count and ``status_snapshot``.

Instance fields of ``DashboardState`` this module reads and writes:

Reads only: ``_build_info``, ``context_builder``, ``lessons``, ``messages_received``,
``no_crons``, ``sessions``, ``slack_client``, ``slack_socket_connected``,
``start_time``, ``subagents``.

A function whose first parameter is ``self`` is a ``DashboardState`` method: the
class binds it under the same name (``state_parts/dashboard_state.py``), so it reads
and writes the state's own fields and calls its siblings through ``self``.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.release_channel import channel as _release_channel_of_build
from kiro_crew.safety_override import cached_disabled_approval_modes

if TYPE_CHECKING:
    from kiro_crew.dashboard.state_parts.dashboard_state import DashboardState


#: Cache for :meth:`DashboardState.served_bundle_id` — the served frontend
#: entry point's ``(mtime_ns, size)`` -> short content hash. One slot: there is
#: exactly one served bundle per process, and the snapshot that reads it sits
#: on the hot status path.
_BUNDLE_ID_CACHE: dict[str, tuple[tuple[int, int], str]] = {}


#: The entry point the gateway serves (``server.py``'s ``_DIST_DIR``).
_SERVED_INDEX = Path(__file__).resolve().parent.parent.parent / "static" / "dist" / "index.html"


def served_bundle_id(index: Path | None = None) -> str:
    """A short content hash of the SERVED frontend bundle's entry point.

    The WS status frame already lets the SPA reload itself across a gateway
    UPGRADE (it compares ``version`` between pushes), but a rebuild of the
    same version — the normal state of a git-checkout dev install, whose
    in-app update pulls, rebuilds, and restarts without the version moving —
    changes neither ``version`` nor ``commit``-visible fields in a way every
    open tab observes. This id is the field that does move: it hashes the
    entry ``index.html`` under ``static/dist`` (whose hashed asset names
    change on every rebuild), so any tab holding a stale bundle can detect
    the swap on its next status frame and reload.

    Cached by ``(mtime_ns, size)``: the snapshot sits on the hot status
    path (every ``/api/status`` poll and WS push), so the file is re-read
    only when a rebuild actually replaced it. Empty string when there is no
    served bundle (source tree without a built frontend, unit tests) — the
    SPA treats empty as UNKNOWN and never reloads over it.

    ``index`` is injectable for tests; the default is the entry point the
    gateway actually serves (``server.py``'s ``_DIST_DIR``).
    """
    if index is None:
        index = _SERVED_INDEX
    try:
        st = index.stat()
    except OSError:
        return ""
    key = (st.st_mtime_ns, st.st_size)
    cached = _BUNDLE_ID_CACHE.get("v")
    if cached and cached[0] == key:
        return cached[1]
    try:
        digest = hashlib.sha256(index.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""
    _BUNDLE_ID_CACHE["v"] = (key, digest)
    return digest


def _count_lessons(self: DashboardState) -> int | None:
    """Count available Global lessons without blocking the recovery dashboard."""
    from kiro_crew.memory_startup import MemoryStartupUnavailable

    try:
        count = len(self.lessons.load_all())
        if self.context_builder:
            vs = self.context_builder.memory.vector_store
            if vs:
                # COUNT(*) keeps status polling from materializing lessons.
                count += vs.count_lessons()
        return count
    except MemoryStartupUnavailable:
        # The selected store's Recovery view supplies the diagnostic. Keep
        # the dashboard shell usable and do not misreport unavailable as 0.
        return None


def status_snapshot(
    self: DashboardState,
    *,
    cron_jobs: int | None = None,
    lessons: int | None = None,
    update_available: bool | None = None,
    update_can_apply: bool = False,
    update_check_status: str = "unchecked",
    update_command: str = "",
    update_latest_version: str = "",
    update_latest_version_display: str = "",
    update_channel: str = "",
    update_channel_move_pending: bool = False,
    update_managed_by: str = "",
    update_commits_ahead: int = 0,
    update_commits_behind: int = 0,
    update_last_checked_at: float | None = None,
    update_check_interval_secs: int = 43200,
    update_required: bool = False,
    update_min_version: str = "",
    update_can_arm: bool = False,
    update_auto_effect: str = "unknown",
    update_bundled_by_app: bool = False,
    version_display: str = "",
    bundle_id: str = "",
) -> dict[str, Any]:
    """Core status fields shared by /api/status, SSE, and WebSocket pushes.

    ``cron_jobs`` and ``lessons`` are supplied by the caller and never
    computed here: the only production caller is
    ``status_counts.cached_status_snapshot``, which loads both counts off
    the event loop through the shared count cache. Computing them inline
    would run ``_count_lessons`` (a JSONL read plus a SQLite ``COUNT(*)``)
    and ``crons.count_enabled_from_disk`` (a ``crons.json`` parse) on the
    loop thread -- the ``no-blocking-call-on-event-loop`` freeze class this
    emitter path exists to avoid. ``None`` means the count is unknown and
    renders as a loading skeleton, never an authoritative 0.
    """
    uptime = int(time.time() - self.start_time)
    branch, commit = self._build_info
    return {
        "uptime": _fmt_duration(uptime),
        # Auto-approve modes forbidden by the ``approval_modes`` policy
        # scope. In the shared snapshot so HTTP/SSE/WS frames all agree —
        # the picker hides these and the value must survive a WS push.
        "disabled_approval_modes": cached_disabled_approval_modes(),
        "start_time": self.start_time,
        "sessions": self.sessions.count,
        "messages": self.messages_received,
        "cron_jobs": cron_jobs,
        "lessons": lessons,
        "subagents": self.subagents.count if self.subagents else 0,
        "update_available": update_available,
        # Can THIS install replace its own code without the user leaving the
        # app? Only a git checkout can (``POST /api/update`` is git fetch +
        # reset). Shipped alongside the availability flag so the dashboard can
        # offer an Update button that will actually work, instead of one that
        # 409s on a wheel install — it must not have to run a fresh check just
        # to learn the layout.
        "update_can_apply": update_can_apply,
        # Where the check itself got to: "unchecked", "checking", "succeeded",
        # "failed" or "deferred". ``update_available`` is only authoritative on
        # "succeeded", and is null otherwise — without this pair the UI cannot
        # tell "checked and current" from "never checked", and painting a green
        # "Up to date" pill next to a red "couldn't check" line is the exact
        # half-truth the update contract exists to prevent.
        "update_check_status": update_check_status,
        # The upgrade command for an install that cannot replace itself, so the
        # 12-hourly BACKGROUND check can light the nav badge and still land the
        # user on something actionable. Deriving it only from a manual check
        # left the badge pointing at an Update button that 409s.
        "update_command": update_command,
        # The candidate release's version string ("" until a check finds a
        # newer build). The proactive update popup keys its per-version
        # snooze/skip on this, so it rides the hot-path subset; the
        # changelog text deliberately does not.
        "update_latest_version": update_latest_version,
        # DISPLAY-ONLY fold of the candidate above (clean base on the
        # stable channel); the popup's snooze/skip records key on the raw
        # value. Empty string on emitters that don't pass it.
        "update_latest_version_display": update_latest_version_display,
        # The release channel this INSTALL follows (the ``channel`` file
        # cli.sh wrote), empty when the layout has no channel at all (a git
        # checkout tracks a remote; a desktop bundle or container is updated
        # by something else). Distinct from ``release_channel`` below, which
        # is derived from the running version string and answers "which lane
        # were these BYTES built on". The two diverge for the whole window
        # between switching channels and the new lane's build landing, so the
        # switcher must key on this one or it would snap back on every poll.
        "update_channel": update_channel,
        # Is the running build ahead of everything ``update_channel``
        # publishes? True means that lane never shipped these bytes, so the
        # install is not on it yet and only re-running the installer moves
        # it. Deliberately NOT derived by comparing ``update_channel``
        # against ``release_channel`` below: promotion re-points the soaked
        # candidate's bytes without re-stamping them, so a promoted stable
        # build reports ``release_channel: insider`` while being a stable
        # install with nothing pending. False on every layout with no feed
        # answer.
        "update_channel_move_pending": update_channel_move_pending,
        # Who manages updates on this host: "" (self-managed), or the
        # mechanism that owns them (e.g. "command" for a policy-pinned
        # provider). The panel keys its update copy on this — a
        # command-managed host must not render self-managed installer
        # instructions its policy exists to bypass.
        "update_managed_by": update_managed_by,
        # Commit distance from a git checkout's upstream, both directions.
        # DIVERGED (both > 0) reports ``update_available: False`` exactly
        # like a current checkout — the destructive apply paths must never
        # be offered local commits — so without the counts the badge cannot
        # tell the two apart. 0/0 on non-git layouts and before any check.
        "update_commits_ahead": update_commits_ahead,
        "update_commits_behind": update_commits_behind,
        "update_can_arm": update_can_arm,
        # What an available update leads to on this install; see
        # ``update_capability.auto_update_effect``.
        "update_auto_effect": update_auto_effect,
        # Whether the desktop app bundles this gateway; see
        # ``update_capability.bundled_by_desktop_app``.
        "update_bundled_by_app": update_bundled_by_app,
        "update_last_checked_at": update_last_checked_at,
        "update_check_interval_secs": update_check_interval_secs,
        # Mandatory-update verdict (enterprise governance pin OR the release
        # feed's breaking-change floor) plus the floor that triggered it.
        # The proactive update modal reads these off the status frame and
        # drops its snooze/skip affordances while required — it must not
        # depend on the user opening Settings to learn the update is
        # mandatory.
        "update_required": update_required,
        "update_min_version": update_min_version,
        # The RUNNING build's version folded for display (clean base on
        # the stable channel; see `_display_version` in
        # handlers/updates_verdict.py). DISPLAY-ONLY sibling of the raw `version`
        # the WS frame appends after this snapshot — that one is what the
        # SPA compares across pushes to force a reload over a gateway
        # upgrade, so it must never be folded. Empty string on emitters
        # that don't pass it (they render the raw version, as before).
        "version_display": version_display,
        "no_crons": self.no_crons,
        "branch": branch,
        "commit": commit,
        # Content hash of the SERVED frontend bundle (see
        # ``served_bundle_id``). The SPA compares it across status pushes
        # and reloads when it moves — the signal ``version`` cannot give
        # for a same-version rebuild (a git checkout's in-app update), and
        # one that reaches every open tab, not just the one that clicked
        # Update. Empty when no built bundle is served (dev source tree),
        # which the SPA treats as unknown, never as a change.
        # Read off the loop by ``cached_status_snapshot``.
        "bundle_id": bundle_id,
        # Which release lane these bytes came from: "nightly", "insider" or
        # "stable". Shipped as a RESOLVED ANSWER rather than leaving the
        # dashboard to parse `version` itself, because the rule is not
        # obvious (the same release is stamped as SemVer for desktop and
        # PEP 440 for wheels, and neither PEP 440 prerelease spelling
        # contains a `-`) and a frontend mirror of it would drift silently.
        # The dashboard uses this to give prerelease users an obvious way to
        # report a bug; see release_channel.py for the full rule.
        "release_channel": _release_channel_of_build(),
        # True only when Socket Mode actually connected this session, not
        # merely that tokens were present at boot. slack_client is set
        # whenever tokens existed, even if connect() then failed
        # (invalid_auth, a network error), so keying the status badge on it
        # alone painted a green "Connected" over a Slack that never came up.
        # Require BOTH a wired client and the real connect outcome the
        # gateway records after _connect_slack. This is the same field
        # /api/slack/config already reports to the settings badge.
        "slack_connected": (self.slack_client is not None and self.slack_socket_connected),
        # Every OTHER channel's live state, from the same flags each channel's
        # settings badge reads. Only `slack_connected` reached this payload
        # before, so System > Services was silent about a Telegram or Discord
        # channel that failed to start — the operator saw a healthy page and a
        # bot that never answered. Derived by roster loop, so the next channel
        # is covered without touching this dict.
        "channels": self.channel_status(),
        # Governance enforcement health: "active" (enforcing),
        # "disabled" (permissive default / not restricting), "degraded" (a
        # fail-closed trip, integrity mismatch, or unverified policy this
        # session), or "unknown" (policy not yet loaded).  Pure in-memory read.
        "governance": _governance_status(),
        # FIX 2: cap / in-flight / queued counts for unattended app-owned
        # turns. Published so a fleet parked behind the cap is visibly
        # throttled rather than looking like a set of hung workers.
        "background_turns": self.background_turn_stats(),
    }


def _fmt_duration(secs: int) -> str:
    """Format seconds as human-readable duration."""
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m" if h > 0 else f"{m}m {s}s"


def _governance_status() -> str:
    """Governance health for the status snapshot (never raises)."""
    try:
        from kiro_crew.platform.governance_health import governance_status

        return governance_status()
    except Exception:
        return "unknown"
