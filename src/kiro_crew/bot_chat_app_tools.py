"""App tools in a Bot's own chat: the servers an app offers, on the Bots its owner ticked.

A Bot's chat runs on the shared ``kirocrew-mate-conductor``, which mounts only the
platform servers (``mate_worker.conductor_mcp_servers``). This module is the one
exception, and a superseding entry in ``docs/decisions`` must cover it (it narrows
the conductor rules there). It adds nothing unless ALL of these hold:

* the feature preview ``bot_chat_app_tools`` is on (always off on LTS);
* the app is installed and enabled;
* the app OFFERS the server in ``contributes.botChatServers`` (each ``{agent,
  server}`` names a server one of the app's own agents declares);
* the owner approved the app's ``tools`` Bot scope (``permissions.bots``, a staged
  grant: ``apps.bot_grants.app_bot_scopes``);
* the owner ticked THIS Bot for the app (``apps.bot_grants.app_granted_bots``).

The offered server joins this Bot's chat spec per session
(:func:`conductor_session_spec`), the way a Bot's worker gets its own spec per
session (``mate_worker.worker_session_spec``); the shared conductor file is never
written. Its entry carries ``KIROCREW_SESSION_LABEL = dashboard:<the chat's slot>``,
so the app's server knows which Bot is calling. Approvals stay the Bot's own:
nothing here grants a tool call.

A chat mounts its servers when its agent process starts, and that start is recorded,
so a change reaches a live chat through :func:`after_app_change`, which compares the
chat with its record: a Bot that loses a server starts a fresh conversation, so the
old tool names leave its context too; one whose servers were added or changed reloads
(same conversation).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import threading
from collections.abc import AsyncIterator, Callable, Iterable
from typing import Any

from kiro_crew.bot_chat_app_entries import _rendered_entry
from kiro_crew.bot_chat_ending_fence import start_in_flight

logger = logging.getLogger(__name__)

#: ``dashboard.feature_previews.<this>``.
PREVIEW = "bot_chat_app_tools"


def preview_on(cfg: Any = None) -> bool:
    """Whether the preview is on. Off on any read failure, and always off on LTS."""
    from kiro_crew.edition_locks import feature_previews

    try:
        if cfg is None:
            from kiro_crew.config.loader import KiroCrewConfig

            cfg = KiroCrewConfig.load()
        previews = getattr(getattr(cfg, "dashboard", None), "feature_previews", None)
    except Exception:  # noqa: BLE001 - an unread config is the preview off
        return False
    previews = feature_previews(previews)
    return isinstance(previews, dict) and previews.get(PREVIEW) is True


def _agent_stem(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".json")


def _offers(app: str) -> list[tuple[str, str]]:
    """``[(agent, server)]`` *app* offers to Bot chats, its own agents only. Blocking.

    *agent* is the name registration files the agent under: the ``name`` its spec
    declares, its file stem when it declares none (``bridges_agents._register_agents``).
    An offer names the agent by its file stem.
    """
    from kiro_crew.apps.bridges import _registration_source
    from kiro_crew.apps.manager import _owner_json

    manifest, root = _registration_source(app)
    if manifest is None:
        return []
    names: dict[str, str] = {}
    for rel in map(str, manifest.agents):
        stem = _agent_stem(rel)
        data = _owner_json(root / rel)
        name = data.get("name", stem) if data else stem
        names[stem] = name if isinstance(name, str) and name else stem
    return [
        (names[item["agent"]], item["server"])
        for item in manifest.contributes.botChatServers
        if item["agent"] in names
    ]


def _app_reaches(app: str, slug: str) -> bool:
    """Whether *app* holds the approved ``tools`` scope AND the owner ticked Bot *slug*,
    AND execution trust for *app* is not denied (re-checked at offer time so a trust
    withdrawal that stops short of deregistration cannot re-mount servers on cold start).
    """
    from kiro_crew.apps.bot_grants import BOT_SCOPE_TOOLS, app_bot_scopes, app_granted_bots
    from kiro_crew.apps.execution import app_execution_denied

    # ``app_bot_scopes`` is empty for an app that is not installed and enabled.
    if not (BOT_SCOPE_TOOLS in app_bot_scopes(app) and slug in app_granted_bots(app)):
        return False
    # Execution-admission gate: covers the trust-withdrawn-but-still-enabled case.
    return app_execution_denied(app, action="bot_chat_server_offer") is None


def _app_names() -> list[str]:
    from kiro_crew.apps.manager import list_apps

    return [str(a.get("name") or "") for a in list_apps() if isinstance(a, dict)]


def bot_servers(slug: str, cfg: Any = None) -> dict[str, dict[str, Any]]:
    """``{alias: entry}`` Bot *slug*'s chat mounts now. Empty with the preview off. Blocking."""
    from kiro_crew.mcp_utils import mcp_server_alias

    if not slug or not preview_on(cfg):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for app in _app_names():
        try:
            if not app or not _app_reaches(app, slug):
                continue
            for agent, server in _offers(app):
                entry = _rendered_entry(app, agent, server)
                if entry is not None:
                    # The server part is made slash-free first: aliasing the whole
                    # key would drop the ``<app>:`` namespace for ``vendor/inbox``.
                    out[f"{app}:{mcp_server_alias(server)}"] = entry
        except Exception:  # noqa: BLE001 - one bad app must not hide the rest
            logger.warning("bot chat tools: app %s unreadable", app, exc_info=True)
    return out


#: ``{Bot slug: {alias: entry fingerprint}}``: what each Bot chat's agent process was
#: last started with, written only by :func:`record_mounted` from the list the
#: harness was really handed. A change is judged against this record,
#: never against a reading taken under the current preview flag or grants, so a
#: revocation after the preview went off still lands. In memory: a gateway restart
#: starts every chat on a freshly built spec.
_MOUNTED: dict[str, dict[str, str]] = {}
#: ``{Bot slug: session_key}`` the session key recorded at mount time — written by
#: :func:`record_mounted` alongside :data:`_MOUNTED`.  Used by
#: :func:`stop_app_bot_chat_servers` so teardown does not rely on a live DM-binding
#: read (which may be unreadable during revocation).
_MOUNTED_SESSION_KEYS: dict[str, str] = {}
#: ``{Bot slug: fingerprints}`` a reconcile has queued (a reload or a fresh start)
#: but no agent process has started with yet. Kept apart from :data:`_MOUNTED` so an
#: addition undone before it applied is never judged a loss of what the chat runs.
#: Cleared when the chat's next process gets its tools (:func:`record_mounted`).
#: shortcut: a removal undone before its fresh start applied keeps the queued
#: discard; cancelling it would need the slot's reset hook, upgrade if it bites.
_PENDING: dict[str, dict[str, str]] = {}
#: ``{Bot slug: {alias: fingerprint}}`` offered to the chat's last process but not
#: reaching it: a platform server of that name won, or the harness withheld it (a
#: codex per-tool limit, a claude array held back whole). A reconcile never counts
#: one as lost, and reloads when its entry changes, since the change may make it
#: mountable.
_UNMOUNTED: dict[str, dict[str, str]] = {}
#: ``{Bot slug: (offers, shadowed offers)}``, each ``{alias: fingerprint}``: every
#: offer the builder last made, split by whether a platform server of that name won;
#: :func:`record_mounted` splits the first by what the harness was handed.
_OFFERED: dict[str, tuple[dict[str, str], dict[str, str]]] = {}
#: ``{app_name: int}`` revocation generation per app — bumped by
#: :func:`revoke_app_generation` (security/trust withdrawals and disable/uninstall).
#: A :func:`conductor_session_spec` projection snapshots the generation of each alias's
#: app BEFORE reading grants; :func:`check_and_filter_projection` at the three delivery
#: call-sites compares the snapshot against the current value and drops stale entries.
#: Per app, so another app's change never stales this app's entries.
_APP_GENERATIONS: dict[str, int] = {}
#: ``{slug: {alias}}`` aliases dropped at delivery by :func:`check_and_filter_projection`
#: due to a generation mismatch.  :func:`record_mounted` excludes these from
#: :data:`_UNMOUNTED`, so :func:`after_app_change`'s reconcile does not treat them as
#: "harness-withheld" and skip them; it sees them absent from :data:`_MOUNTED` and
#: schedules a reload.  Cleared by :func:`record_mounted`.
_REPROJECT_SLUGS: dict[str, set[str]] = {}
#: ``{session_key: frozenset[app_name]}`` apps whose entries passed the delivery gate
#: for *session_key* but whose :func:`record_mounted` call has not yet run.  Used by
#: :func:`revoke_app_generation` and :func:`stop_app_bot_chat_servers` to close the
#: window between "gate passed" and "mounted record written" so a revocation that
#: races delivery cannot leave the server running in an unrecorded session.
_PENDING_DELIVERY: dict[str, set[str]] = {}
#: ``{session_key: set[app_name]}`` apps in :data:`_PENDING_DELIVERY` that were voided
#: by a concurrent revocation.  :func:`record_mounted` strips their aliases before
#: writing :data:`_MOUNTED`, so the server never starts despite the gate having passed.
_VOIDED_DELIVERY: dict[str, set[str]] = {}
#: Serialises :func:`check_and_filter_projection`'s final generation check, pending
#: registration and void re-check with :func:`revoke_app_generation`'s bump and void
#: write, so a revocation between "gate passed" and "pending registered" cannot find
#: neither table populated. Held only for a few dict operations.
_DELIVERY_LOCK: threading.Lock = threading.Lock()
#: ``{session_key: (slug, frozenset[app_name])}`` temporary table written by
#: :func:`record_mounted` before the native KAS session id is known.
#: :func:`bind_snapshot_to_native_sid` migrates each entry into
#: :data:`_SESSION_APP_SNAPSHOT` once the session id is available, then removes it
#: here.  Keyed by conversation (Kiro session) key, NOT native session id, so a
#: swap_conversation that reuses the same conversation key but gets a different
#: native id correctly migrates to the new native id rather than overwriting the old
#: session's snapshot.
_SESSION_APP_SNAPSHOT_PENDING: dict[str, tuple[str, frozenset]] = {}
#: ``{native_sid: (slug, frozenset[app_name])}`` the Bot slug and app names whose
#: servers were actually mounted for *native_sid*, populated by
#: :func:`bind_snapshot_to_native_sid` once :func:`record_mounted`'s
#: ``session/new`` completes and the native KAS session id is known.  Keyed by
#: **native** session id so a swap_conversation that creates a new native session
#: (even with the same conversation key) cannot overwrite the old session's
#: membership: the two sessions have distinct native ids and each holds its own
#: snapshot entry.  Read and popped by :func:`note_unconfirmed_close` after the
#: app names are transferred to :data:`_PENDING_CLOSE`.
_SESSION_APP_SNAPSHOT: dict[str, tuple[str, frozenset]] = {}
#: ``{native_sid: runtime}`` the runtime each bound snapshot's session runs on, so a
#: session left behind by a stop can be handed to the close retry.
_SESSION_RUNTIMES: dict[str, Any] = {}
#: ``{(slug, app_name): [(runtime, session_id), ...]}`` sessions whose shared-KAS
#: close was not confirmed, keyed by (Bot slug, app name) so the gate blocks only
#: the specific (slug, app) pair whose old session is unconfirmed—other Bots for
#: the same app are unaffected.  Handles accumulate across cold starts so a new
#: session mounting the same slug does not evict an earlier unconfirmed handle.
#: :func:`_drain_pending_close` retries every handle autonomously on each
#: :func:`stop_app_bot_chat_servers` call and each :func:`after_app_change`,
#: without depending on :data:`_MOUNTED`.
_PENDING_CLOSE: dict[tuple[str, str], list[tuple[Any, str]]] = {}
#: ``set[(slug, app_name)]`` fast lookup: (slug, app) pairs with at least one
#: unconfirmed-close handle.  Used by the delivery gate (fast path, no iteration).
_PENDING_CLOSE_APPS: set[tuple[str, str]] = set()
#: ``{session_id: int}`` retry attempt count per unconfirmed KAS session handle.
#: When a handle reaches :data:`_PENDING_CLOSE_MAX_RETRIES`, the counter resets so
#: the next scheduled drain attempt is not immediately skipped, and a warning is
#: logged.  The handle stays in :data:`_PENDING_CLOSE`; it is never force-confirmed.
_PENDING_CLOSE_RETRIES: dict[str, int] = {}
#: Maximum drain attempts per session handle before resetting the counter and
#: logging a warning.  The handle remains; retries continue on the next timer tick.
_PENDING_CLOSE_MAX_RETRIES: int = 5
#: Back-off delays in seconds for the per-(slug, app) retry timer.  After each
#: failed drain the next delay is taken from this list in order; the last value
#: repeats.  Timer fires call :func:`_drain_pending_close` then
#: :func:`_schedule_reconcile` on confirmation.
_PENDING_CLOSE_BACKOFFS: tuple[int, ...] = (30, 60, 120, 240, 480)
#: ``{(slug, app_name): int}`` index into :data:`_PENDING_CLOSE_BACKOFFS` for
#: the next timer this (slug, app) pair schedules.
_PENDING_CLOSE_BACKOFF_IDX: dict[tuple[str, str], int] = {}
#: ``{(slug, app_name): asyncio.TimerHandle}`` active back-off timers, one per
#: (slug, app) pair at most.  Cancelled on gateway shutdown and when a drain
#: confirms the handle.
_PENDING_CLOSE_TIMERS: dict[tuple[str, str], "asyncio.TimerHandle"] = {}
#: ``{slug: {alias: fingerprint}}`` aliases blocked at the delivery gate ONLY
#: because a pending-close for that (slug, app) has not been confirmed yet.  Unlike
#: revocation-fenced drops, these aliases must NOT trigger a reconcile or enter
#: :data:`_REPROJECT_SLUGS`: the app is still enabled and granted; it should remount
#: the moment :func:`_drain_pending_close` confirms the old handle.
#: :func:`record_mounted` excludes these aliases from :data:`_UNMOUNTED` (which is
#: permanent and never cleared per-alias).  :func:`after_app_change` merges
#: :data:`_UNMOUNTED` and :data:`_PENDING_CLOSE_WITHHELD` into a combined ``withheld``
#: dict, so the alias is still treated as "harness-withheld unchanged" until drain
#: confirms.  Once :func:`_drain_pending_close` removes the confirmed entry from this
#: dict, neither :data:`_UNMOUNTED` nor :data:`_PENDING_CLOSE_WITHHELD` holds it, so
#: the next reconcile sees it as a new addition and schedules a reload.
_PENDING_CLOSE_WITHHELD: dict[str, dict[str, str]] = {}
#: ``set[app_name]`` apps whose revocation is in progress (from the initial
#: :func:`revoke_app_generation` call until :func:`stop_app_bot_chat_servers`
#: returns).  While an app is in this set, :func:`record_mounted` refuses to write
#: a new harness record for it, ensuring no new session can mount the app between
#: the generation bump and the teardown completing.  :func:`check_and_filter_projection`
#: does not read this set; the generation fence in that path uses :data:`_APP_GENERATIONS`.
#: can mount the app between the generation bump and the teardown completing.
_REVOCATION_FENCE: set[str] = set()
#: ``{app_name: int}`` count of :func:`app_stop_fence` spans open for the app.  :func:`_revocation_fence_clear` only discards the
#: app from :data:`_REVOCATION_FENCE` when this count reaches zero, so a handler
#: that owns the fence across a multi-step operation (stop -> disable -> trust-write)
#: is not evicted by an internal drain or early-return path inside the same span.
_FENCE_HOLD_COUNTS: dict[str, int] = {}
#: Monotonically increasing epoch bumped by every :func:`after_app_change`.  Unlike
#: :data:`_APP_GENERATIONS` (which fences in-flight projections for a *specific* app),
#: this single epoch lets :func:`record_mounted` detect that *any* grant/tick/preview
#: change occurred while a cold-starting chat held a snapshot — without staling every
#: app's projection entries, which would trigger the reconcile feedback loop Opus
#: flagged.  Only :func:`record_mounted`'s stale-snap check reads it; the delivery
#: gate uses :data:`_APP_GENERATIONS` exclusively.
_CHANGE_EPOCH: int = 0
#: ``{session_key: epoch}`` the :data:`_CHANGE_EPOCH` value captured by
#: :func:`conductor_session_spec` before reading grants; consumed by
#: :func:`record_mounted` to detect a concurrent change.
_PROJECTION_EPOCH: dict[str, int] = {}
#: ``(state, loop)`` of the last :func:`after_app_change`, used by
#: :func:`_schedule_reconcile` to trigger a fresh reconcile when stale entries
#: are dropped at delivery.
_RECONCILER: tuple[Any, asyncio.AbstractEventLoop] | None = None


def record_mounted(
    session_key: str, names: Iterable[str], spelled: Callable[[str], str] = str
) -> frozenset:
    """Settle *session_key*'s Bot chat record from the server *names* its harness was
    really handed, once the harness's projection is final. Blocking. Never raises.

    Every harness calls it at the same point, after its own withholding: the KAS
    projection, the array a mirror built (claude, codex), the mirrorless member path.
    Nothing else writes the record: a projection that only reads (a settings
    re-seed, a deny-rule pass) leaves the running chat's record as it is.

    *names* are as the harness registers them, and *spelled* is how that harness
    spells a name it is handed, so each offer is matched under it: the codex callers
    pass ``codex_name`` (codex folds whitespace); KAS and the claude array keep the
    alias as is.

    Returns a ``frozenset`` of app names (the ``<app>`` prefix of ``<app>:<server>``
    aliases) that were **voided** by a concurrent revocation.  The caller must strip
    all entries for those apps from the ``servers`` array before passing it to
    ``session/new``, so revoked code cannot start even when the array was already built.
    An empty frozenset means no aliases were voided.
    """
    voided_apps: frozenset = frozenset()
    try:
        # Use the slug recorded by conductor_session_spec at projection time — it
        # read the identity already.  Only fall back to a live member_identity read
        # when no projection sidecar exists (e.g. a re-delivery after sidecar
        # consumption on a previous call).  If even the fallback fails, nothing is
        # mounted, so do not silently leave _OFFERED stale: skip the record.
        slug = _PROJECTION_SLUGS.get(session_key)
        if slug is None:
            from kiro_crew.members import member_identity_for_session

            ident = member_identity_for_session(session_key)
            if not ident:
                return frozenset()
            slug = ident["slug"]
        record = _OFFERED.get(slug)
        if record is None:
            return frozenset()
        # A shadowed offer shares its name with the platform server that won, so the
        # handed list cannot tell them apart: it stays unmounted whatever is handed.
        offered, shadow = record
        handed = {str(n) for n in names}
        with _DELIVERY_LOCK:
            # Clear this session from the pending-delivery table now that
            # record_mounted is running.  The lock closes the window between this
            # pop and the _MOUNTED write below: a concurrent stop that reads
            # _PENDING_DELIVERY and _MOUNTED under the same lock will not see the
            # session in either table during this transition.
            _PENDING_DELIVERY.pop(session_key, None)
            voided = _VOIDED_DELIVERY.pop(session_key, None)
            if voided:
                # The delivery array was already handed to the session before this void
                # was detected.  Record the voided aliases as MOUNTED (so any subsequent
                # revoke or stop can find the session) and schedule a reconcile that will
                # immediately trigger a fresh start, tearing the running server down.
                # Do NOT strip from `handed`: that would leave _MOUNTED empty, making
                # the session invisible to stop_app_bot_chat_servers forever.  The
                # revoke path (revoke_app_generation → stop_app_bot_chat_servers)
                # already discards the pending session key; this branch handles
                # record_mounted running before that stop fires.
                voided_apps = frozenset(voided)
                _schedule_reconcile()
            # Also treat any app currently under a revocation fence as voided: even if
            # the delivery gate was bypassed (e.g. via swap_conversation), the app's
            # server must not remain in _MOUNTED without a pending reconcile.
            # Fence scope: a fence raised for a confirmed revoke/disable/uninstall
            # blocks ALL Bots (no (*, app) pair in _PENDING_CLOSE_APPS).  A fence
            # kept only by a pending-close for another Bot's (slug, app) pair does
            # NOT block this Bot's cold-start; only (slug, app) pairs for the
            # current slug are blocked.  This prevents Bot B from losing app X's
            # tools because Bot A's KAS close for X was not confirmed.
            # Exception: when _VOIDED_DELIVERY already handled the app (the revoke
            # raced the gate, session is already running), do NOT add to fenced_apps
            # — the alias must stay in _MOUNTED so stop_app_bot_chat_servers can
            # find the running session for teardown.
            fenced_apps = frozenset(
                alias.split(":")[0]
                for alias in {a for a, _ in offered.items() if ":" in a}
                if alias.split(":")[0] in _REVOCATION_FENCE
                and alias.split(":")[0] not in voided_apps  # _VOIDED_DELIVERY already handled
                and (
                    # An active handler holds the fence (_FENCE_HOLD_COUNTS > 0):
                    # block ALL Bots, not only the one with a pending close.
                    # The pending-close exemption applies only when _PENDING_CLOSE alone
                    # keeps the fence up (after a handler has finished), so Bot A's
                    # unconfirmed KAS close does not unblock Bot B during an active
                    # revoke/disable/uninstall operation.
                    _FENCE_HOLD_COUNTS.get(alias.split(":")[0], 0) > 0
                    or not any(a == alias.split(":")[0] for _, a in _PENDING_CLOSE_APPS)
                    or (slug, alias.split(":")[0]) in _PENDING_CLOSE_APPS
                )
            )
            if fenced_apps:
                voided_apps = voided_apps | fenced_apps
                _schedule_reconcile()
            # Exclude fenced-only apps from _MOUNTED so after_app_change sees
            # now != was once the fence drops and reloads the Bot.  Aliases voided
            # via _VOIDED_DELIVERY (the app ran, was revoked mid-flight) MUST still
            # land in _MOUNTED so stop_app_bot_chat_servers can find the running
            # session for teardown; only fence-blocked cold-start aliases are excluded.
            _MOUNTED[slug] = {
                a: f
                for a, f in offered.items()
                if spelled(a) in handed and a.split(":")[0] not in fenced_apps
            }
            _MOUNTED_SESSION_KEYS[slug] = session_key
            # A pending snapshot keyed by conversation key, migrated to
            # _SESSION_APP_SNAPSHOT[native_sid] by bind_snapshot_to_native_sid once
            # session/new returns: a swap reusing the key gives the new session its
            # own native id and entry, so the old session's migrated snapshot survives.
            _SESSION_APP_SNAPSHOT_PENDING[session_key] = (
                slug,
                frozenset(a.split(":")[0] for a in _MOUNTED[slug] if ":" in a),
            )
            # Final-barrier fence check: revoke_app_generation holds _DELIVERY_LOCK
            # too, so a revocation that ran between check_and_filter_projection
            # (which popped _PENDING_DELIVERY) and this lock acquisition was
            # serialised OUTSIDE this critical section.  Anything it added to
            # _REVOCATION_FENCE is visible here while holding this lock.
            # app_stop_fence also adds to _FENCE_HOLD_COUNTS under _DELIVERY_LOCK,
            # so reading it here is safe: a span opened before record_mounted
            # acquired the lock shows _FENCE_HOLD_COUNTS[app] > 0 here.
            # Strip any late-revoked app from _MOUNTED immediately when a handler
            # is actively holding the fence: a pending-close-only fence is already
            # handled by the slug-scoped fenced_apps check above.
            late_revoked_hold = {
                a.split(":")[0]
                for a in list(_MOUNTED.get(slug, {}))
                if ":" in a
                and a.split(":")[0] in _REVOCATION_FENCE
                and _FENCE_HOLD_COUNTS.get(a.split(":")[0], 0) > 0
                and a.split(":")[0] not in voided_apps
            }
            if late_revoked_hold:
                _MOUNTED[slug] = {
                    a: f
                    for a, f in _MOUNTED[slug].items()
                    if ":" not in a or a.split(":")[0] not in late_revoked_hold
                }
                voided_apps = voided_apps | late_revoked_hold
                _schedule_reconcile()
        # Aliases dropped at delivery due to a generation mismatch are recorded in
        # _REPROJECT_SLUGS by check_and_filter_projection.  Exclude them from
        # _UNMOUNTED so after_app_change's reconcile does not treat them as
        # "harness-withheld" (which would suppress a reload).  The next reconcile
        # sees them absent from _MOUNTED but absent from _UNMOUNTED too, so now != was
        # and it schedules a reload.
        # gen_dropped contains the DELIVERED names (possibly whitespace-folded by
        # name_transform, e.g. codex_name) that were dropped due to a generation
        # mismatch.  Compare using spelled(a) so that an original alias whose
        # delivered form is folded is also excluded; without this, an offered alias
        # like "my server" (original) is never equal to "my_server" (delivered /
        # gen_dropped entry) and would land permanently in _UNMOUNTED.
        gen_dropped_delivered = _REPROJECT_SLUGS.pop(slug, set())
        # pending_withheld contains the DELIVERED aliases blocked only because a
        # previous KAS close for this (slug, app) is unconfirmed.  Exclude them
        # from _UNMOUNTED: _UNMOUNTED is permanent (never cleared per-alias), so
        # writing a pending-close alias here would block it forever even after
        # _drain_pending_close confirms the old close.  after_app_change merges
        # _UNMOUNTED and _PENDING_CLOSE_WITHHELD into `withheld` already, so the
        # alias is still treated as "unchanged" until drain clears it; once drain
        # removes it from _PENDING_CLOSE_WITHHELD, _UNMOUNTED has no entry for it
        # either, so the next reconcile sees it as a new addition and schedules
        # a reload.  Compare using spelled(a) for the same folding reason.
        pending_withheld = _PENDING_CLOSE_WITHHELD.get(slug, {}) if slug is not None else {}
        _UNMOUNTED[slug] = {
            **shadow,
            **{
                a: f
                for a, f in offered.items()
                if spelled(a) not in handed
                and spelled(a) not in gen_dropped_delivered
                and spelled(a) not in pending_withheld
            },
        }
        # Pending-close aliases are suppressed only until their drain confirms.
        # They are tracked in _PENDING_CLOSE_WITHHELD and must NOT be written
        # into _UNMOUNTED: _UNMOUNTED is for harness-permanent withholding and
        # is never cleared per-alias, so an alias written here would remain
        # blocked even after the drain confirms, preventing the Bot from ever
        # receiving its tools again.  after_app_change reads _PENDING_CLOSE_WITHHELD
        # directly alongside _UNMOUNTED and applies the same "skip-if-unchanged"
        # filter; once _drain_pending_close clears the entry, the next reconcile
        # sees the alias as a new addition and schedules a reload.
        _PENDING.pop(slug, None)
        # If the sidecar for this session still exists (check_and_filter_projection
        # was not called, e.g. a direct delivery path), check whether any grant/tick/
        # preview change occurred (via _CHANGE_EPOCH) or any per-app revocation
        # advanced (via _APP_GENERATIONS) and trigger a reconcile if so.
        stale_snap = _PROJECTION_GENS.pop(session_key, None)
        stale_epoch = _PROJECTION_EPOCH.pop(session_key, None)
        _PROJECTION_SLUGS.pop(session_key, None)
        _PROJECTION_OFFER_FP.pop(session_key, None)
        epoch_stale = stale_epoch is not None and stale_epoch != _CHANGE_EPOCH
        gen_stale = stale_snap and any(
            snap_gen != _APP_GENERATIONS.get(alias.split(":")[0], 0)
            for alias, snap_gen in stale_snap.items()
        )
        if epoch_stale or gen_stale:
            _schedule_reconcile()
        return voided_apps
    except Exception:  # noqa: BLE001 - the provisional record stands
        logger.warning("bot chat tools: mounted record for %s failed", session_key, exc_info=True)
        return frozenset()


def _note_transient_drop(slug: str | None, alias: str) -> None:
    """Record *alias* (as delivered) as dropped by a gate, not withheld by the harness.

    :func:`record_mounted` keeps every alias recorded here out of the permanent
    :data:`_UNMOUNTED`, so when the gate opens again (re-tick, re-grant, generation
    settled, preview back on) :func:`after_app_change` sees an addition and reloads.
    Only a server the harness cannot carry, or one a platform server shadows, is
    permanent.
    """
    if slug is not None:
        _REPROJECT_SLUGS.setdefault(slug, set()).add(alias)


def note_transient_drops(session_key: str, aliases: Iterable[str]) -> None:
    """:func:`_note_transient_drop` for a delivery site that strips entries itself
    (the mirror's preview-off branch), before it calls :func:`record_mounted`."""
    slug = _PROJECTION_SLUGS.get(session_key)
    for alias in aliases:
        _note_transient_drop(slug, alias)


def mark_seed_lag_reproject(session_key: str) -> None:
    """Mark every app-server alias in this session's sidecar for re-projection.

    Called by any delivery path that withholds the whole array due to a seed
    lag (``_worker_settings_lag`` returned True).  The lag is transient: the
    next start reads a refreshed seed.  Putting the aliases into
    :data:`_REPROJECT_SLUGS` before ``record_mounted`` is called ensures they
    are excluded from :data:`_UNMOUNTED`, so :func:`after_app_change`'s
    reconcile sees the gap and schedules a reload rather than treating the
    missing servers as "harness-withheld" and skipping them.

    ``_schedule_reconcile`` is also called so the reconcile runs soon rather
    than waiting for the next tick.
    """
    slug = _PROJECTION_SLUGS.get(session_key)
    if slug is None:
        # No projection sidecar recorded (non-Bot session or already consumed).
        return
    snap = _PROJECTION_GENS.get(session_key) or {}
    aliases = {alias for alias in snap if ":" in alias}
    if aliases:
        _REPROJECT_SLUGS.setdefault(slug, set()).update(aliases)
    _schedule_reconcile()


def _schedule_reconcile() -> None:
    """Re-run :func:`after_app_change` on the reconciler's loop, from any thread.

    Called by :func:`check_and_filter_projection` when it drops stale entries so
    the next projection reads fresh grants.
    """
    if _RECONCILER is None:
        return
    state, loop = _RECONCILER

    def _start() -> None:
        task = loop.create_task(after_app_change(state))
        tasks = getattr(state, "_crewmate_reload_tasks", None)
        if not isinstance(tasks, set):
            tasks = set()
            try:
                state._crewmate_reload_tasks = tasks
            except AttributeError:
                pass
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    try:
        loop.call_soon_threadsafe(_start)
    except RuntimeError:
        pass


def mounted_names(servers: Any) -> list[str]:
    """The ``name`` of each element of an ACP ``mcpServers`` array, or the keys of an
    ``mcpServers`` map: the shape :func:`record_mounted` takes from any harness."""
    if isinstance(servers, dict):
        return [str(k) for k in servers]
    if isinstance(servers, (list, tuple)):
        return [str(e.get("name")) for e in servers if isinstance(e, dict) and e.get("name")]
    return []


def _fingerprints(servers: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {alias: json.dumps(e, sort_keys=True, default=str) for alias, e in servers.items()}


#: ``{session_key: {alias: gen}}`` the per-app revocation generation captured
#: by :func:`conductor_session_spec` BEFORE reading the grant table.  A revocation
#: that races the grant read cannot advance past the recorded generation.  Entries are
#: removed by :func:`check_and_filter_projection` after the delivery gate runs.
#: Both the original alias and its codex-folded form are stored so the KAS path
#: (which reports the folded name) can find the sidecar entry.
_PROJECTION_GENS: dict[str, dict[str, int]] = {}
#: ``{session_key: slug}`` the Bot slug for each in-flight projection, so
#: :func:`check_and_filter_projection` can call :func:`_app_reaches` without
#: scanning :data:`_OFFERED` (which may hold entries for other Bots).
_PROJECTION_SLUGS: dict[str, str] = {}
#: ``{session_key: {alias: fp}}`` unstamped offer fingerprints from the original
#: :func:`conductor_session_spec` projection.  Stored before ``stamp_session_label``
#: runs, so the fp matches the ``bot_servers()`` output that :func:`after_app_change`
#: compares against.  Used by :func:`check_and_filter_projection` to seed
#: :data:`_PENDING_CLOSE_WITHHELD` with the correct fingerprint.
_PROJECTION_OFFER_FP: dict[str, dict[str, str]] = {}
#: Session keys that have had a sidecar written at some point.  When the sidecar
#: has been consumed (re-delivery), pass through rather than fail-closed.
_HAD_SIDECAR: set[str] = set()


def revoke_app_generation(app_name: str) -> None:
    """Signal that *app_name* trust/grant has been withdrawn.

    Bumps the per-app generation in :data:`_APP_GENERATIONS` for *app_name* only,
    so any in-flight :func:`conductor_session_spec` projection that read grants
    before this call will have its stale entries for *app_name* dropped by
    :func:`check_and_filter_projection` at the three delivery call-sites.
    Other apps' entries are unaffected: a Bot chat starting while a different
    app is being revoked still mounts its own app's servers correctly.

    Also voids any pending deliveries for *app_name* (sessions whose gate passed
    but whose :func:`record_mounted` has not yet run), closing the window between
    the gate and the mounted record.

    Call this BEFORE :func:`stop_app_bot_chat_servers` on every security path.
    """
    with _DELIVERY_LOCK:
        # Raise the revocation fence for this app before bumping the generation.
        # The fence blocks the delivery gate from admitting any entry for the app
        # from this point until stop_app_bot_chat_servers completes.  This covers
        # paths (swap_conversation, re-delivery) that do not go through the
        # generation check alone.
        _REVOCATION_FENCE.add(app_name)
        # Bump under the lock so any check_and_filter_projection that is concurrently
        # holding the same lock and about to register a pending delivery will see the
        # advanced generation on its final re-check inside the lock, and refuse the
        # delivery.  Any check_and_filter_projection that has already registered and
        # released the lock will find its session_key in _PENDING_DELIVERY below and
        # have its delivery voided.
        _APP_GENERATIONS[app_name] = _APP_GENERATIONS.get(app_name, 0) + 1
        # Void all pending deliveries for this app so check_and_filter_projection strips
        # their entries before record_mounted can commit them to _MOUNTED.
        for session_key, apps in list(_PENDING_DELIVERY.items()):
            if app_name in apps:
                _VOIDED_DELIVERY.setdefault(session_key, set()).add(app_name)


def check_and_filter_projection(
    session_key: str,
    servers: dict[str, dict[str, Any]],
    *,
    name_transform: "Callable[[str], str] | None" = None,
) -> dict[str, dict[str, Any]]:
    """Remove app-server entries whose revocation generation has advanced or whose
    admission is invalid, and clear the sidecar-table entries for *session_key*.

    Called at the three delivery points (AcpRuntime mirror, AcpClient array, KAS
    projection) before the final array is handed to the session.  Blocking: calls
    :func:`_app_reaches` and :func:`member_identity_for_session` which read files.
    Wrap in ``asyncio.to_thread`` on async callsites.

    For each ``<app>:<server>`` alias:
    * If the sidecar has no entry for this alias, it was not part of this session's
      projection; it is stripped (fail-closed: an unrecorded app server cannot mount).
    * If the generation in the sidecar differs from :data:`_APP_REVOKE_GEN`, the entry
      is stripped (revocation raced the projection).
    * If :func:`_app_reaches` says the app lacks execution admission, the entry is
      stripped.

    Entries without a ``:`` separator (platform servers) are left as-is.
    *name_transform*, when given, maps a delivered alias to its original form
    (e.g. ``codex_name`` folds whitespace; callers that use it pass it here).
    """
    from kiro_crew.members import member_identity_for_session

    # Only consume the sidecar when this delivery contains <app>: entries.  An empty
    # or platform-server-only array (the held-back case) leaves the sidecar in place
    # for the subsequent real delivery.
    has_app_entry = any(":" in alias for alias in servers)
    if has_app_entry:
        snapshot = _PROJECTION_GENS.pop(session_key, None)
        slug = _PROJECTION_SLUGS.get(
            session_key
        )  # read but do not pop: record_mounted owns cleanup
    else:
        snapshot = _PROJECTION_GENS.get(session_key)
        slug = _PROJECTION_SLUGS.get(session_key)
    if snapshot is None:
        # No sidecar: either no Bot-app servers were offered to this session,
        # or this is a pure re-delivery after the sidecar was already consumed.
        if session_key in _HAD_SIDECAR:
            # Re-delivery: sidecar consumed on a previous call; pass through.
            return servers
        # First delivery but no sidecar was ever written: fail-closed for <app>: entries.
        # A gate drop, not a harness one: it stays out of the permanent _UNMOUNTED.
        for alias in servers:
            if ":" in alias:
                _note_transient_drop(slug, alias)
        return {alias: entry for alias, entry in servers.items() if ":" not in alias}
    # Fall back to live identity when the slug was not stored.
    if slug is None:
        try:
            ident = member_identity_for_session(session_key)
            slug = ident["slug"] if ident else None
        except Exception:  # noqa: BLE001
            slug = None

    out: dict[str, dict[str, Any]] = {}
    for alias, entry in servers.items():
        if ":" not in alias:
            # Platform server (no app namespace) -- always pass through.
            out[alias] = entry
            continue
        app_name = alias.split(":")[0]
        # Look up the alias in the sidecar. If a name_transform was given (e.g.
        # codex_name passed by the KAS/mirror callsite), use it to find original aliases
        # that fold to this delivered name.
        recorded_gen = snapshot.get(alias)
        if recorded_gen is None and name_transform is not None:
            # Try each sidecar key: if name_transform(key) == delivered alias, it's a match.
            for orig_alias, gen in snapshot.items():
                if name_transform(orig_alias) == alias:
                    recorded_gen = gen
                    break
        # Fail-closed: an <app>: entry with no sidecar record did not originate from
        # this projection and must not mount.
        if recorded_gen is None:
            _note_transient_drop(slug, alias)
            continue
        if recorded_gen != _APP_GENERATIONS.get(app_name, 0):
            _schedule_reconcile()  # a change raced this projection; re-compare
            # A gen-mismatch drop is transient: after the reconcile,
            # after_app_change sees the alias absent from _MOUNTED and from
            # _UNMOUNTED, detects the gap, and schedules a reload.
            _note_transient_drop(slug, alias)
            continue  # revocation or grant change raced the projection
        # Block delivery for a specific (slug, app) pair whose previous KAS close was
        # not confirmed.  Using the slug key means only THIS Bot's session for this app
        # is blocked; other Bots' sessions for the same app can mount normally.
        if slug is not None and (slug, app_name) in _PENDING_CLOSE_APPS:
            # Blocked only by a pending close—NOT a revocation or generation mismatch.
            # Do NOT call _schedule_reconcile() and do NOT add to _REPROJECT_SLUGS:
            # the app is still enabled and granted; a reconcile here would trigger a
            # reload loop (the new session also gets blocked → another reconcile → …).
            # Instead, stash the alias+fingerprint so record_mounted can merge it into
            # _UNMOUNTED.  after_app_change then sees withheld.get(alias)==fingerprint,
            # treats now==was, and skips the reload.  When _drain_pending_close confirms
            # the (slug, app) handle, it removes the alias from _PENDING_CLOSE_WITHHELD
            # and schedules a single reconcile so the alias can mount on the next cycle.
            # Ensure a back-off timer is running so this cold start triggers a drain
            # even when no app-change event fires later.
            _schedule_pending_close_drain(slug, app_name)
            # Store the fp that matches after_app_change's bot_servers() output.
            # bot_servers() returns the unstamped entry from the app's installed record,
            # so the fp must come from before stamp_session_label ran.
            # conductor_session_spec saves the pre-stamp fingerprint in
            # _PROJECTION_OFFER_FP[session_key][alias]; use it when available.
            # For non-conductor harnesses (no PROJECTION_OFFER_FP entry), fall back
            # to stripping x-kirocrew-session-label — that removes only the marker key
            # but not env changes stamp_session_label makes, so a mismatch may still
            # occur for those paths; they can be fixed independently if needed.
            # shortcut: fallback strip-only path may still mismatch for stamped env;
            # upgrade by storing pre-stamp fp in client_mcp_array and kas paths too.
            offer_fps = _PROJECTION_OFFER_FP.get(session_key, {})
            if alias in offer_fps:
                fp_json = offer_fps[alias]
            else:
                fp_entry = {k: v for k, v in entry.items() if k != "x-kirocrew-session-label"}
                fp_json = json.dumps(fp_entry, sort_keys=True, default=str)
            _PENDING_CLOSE_WITHHELD.setdefault(slug, {})[alias] = fp_json
            continue
        # Re-check execution admission using this session's own Bot slug.
        if slug is not None and not _app_reaches(app_name, slug):
            # Admission denied (unticked, scope or grant withdrawn): a transient
            # gate drop, so a later re-grant is seen as an addition and reloads.
            _note_transient_drop(slug, alias)
            continue  # execution admission denied
        out[alias] = entry
    # Under _DELIVERY_LOCK, register the apps in this delivery as pending so a
    # concurrent revocation cannot slip between "generation check passed" and
    # "pending registered".  revoke_app_generation bumps the generation and voids
    # pending entries while holding the same lock, so either:
    #   (a) revocation ran first: generation is already advanced; the final re-check
    #       below strips the entry before we register anything, or
    #   (b) we register first: revocation will find our entry in _PENDING_DELIVERY and
    #       void it before record_mounted commits.
    # Both cases are covered; the race window is closed.
    pending_apps = {a.split(":")[0] for a in out if ":" in a}
    if pending_apps:
        with _DELIVERY_LOCK:
            # Record which generation we saw for each app so we can re-verify here.
            # app_seen_gens[app] = the generation recorded_gen we passed the check with.
            app_seen_gens: dict[str, int] = {}
            for alias in list(out):
                if ":" not in alias:
                    continue
                app_name = alias.split(":")[0]
                if app_name not in app_seen_gens:
                    # Reconstruct from snapshot: the generation we passed at.
                    recorded_gen_final = snapshot.get(alias)
                    if recorded_gen_final is None and name_transform is not None:
                        for orig_alias, gen in snapshot.items():
                            if name_transform(orig_alias) == alias:
                                recorded_gen_final = gen
                                break
                    app_seen_gens[app_name] = recorded_gen_final or 0
            # Register under the lock.
            _PENDING_DELIVERY[session_key] = pending_apps
            # Final re-check: strip any app whose generation advanced since the loop ran.
            for alias in list(out):
                if ":" not in alias:
                    continue
                app_name = alias.split(":")[0]
                current_gen = _APP_GENERATIONS.get(app_name, 0)
                if current_gen != app_seen_gens.get(app_name, 0):
                    out.pop(alias)
                    _schedule_reconcile()
                    _note_transient_drop(slug, alias)
            # Pop any void written by a revocation that ran after us.
            voided = _VOIDED_DELIVERY.pop(session_key, None)
            if voided:
                for alias in list(out):
                    if ":" in alias and alias.split(":")[0] in voided:
                        out.pop(alias)
                        _note_transient_drop(slug, alias)
                        _schedule_reconcile()
            # Update pending_apps after stripping.
            live_apps = {a.split(":")[0] for a in out if ":" in a}
            if live_apps:
                _PENDING_DELIVERY[session_key] = live_apps
            else:
                _PENDING_DELIVERY.pop(session_key, None)
    return out


def _slot_of(session_key: str) -> str:
    for prefix in ("dashboard_", "dashboard:"):
        if session_key.startswith(prefix):
            return session_key[len(prefix) :]
    return session_key


def conductor_session_spec(spec: dict[str, Any], session_key: str) -> dict[str, Any]:
    """*spec* (the shared conductor's) plus the app servers this Bot's chat mounts.

    Never raises and never narrows: anything unreadable answers *spec* unchanged. A
    platform server of the same name wins. The session-label marker is replaced by
    the label itself, so every harness hands the server the same caller. Blocking.
    """
    from kiro_crew.agent_sdk.agent_specs import stamp_session_label
    from kiro_crew.members import member_identity_for_session

    try:
        # Snapshot each app's generation AND the change epoch BEFORE reading grants,
        # so a revocation or grant change that races the grant-read cannot advance
        # past this snapshot.
        # check_and_filter_projection uses _APP_GENERATIONS at the three delivery call-sites.
        # record_mounted uses _CHANGE_EPOCH to detect any concurrent grant/tick/preview change.
        # Per-app snapshots mean an unrelated app's change does not stale this
        # projection's entries for a different app.
        import copy as _copy

        gen_snapshot_map = _copy.copy(_APP_GENERATIONS)
        epoch_snapshot = _CHANGE_EPOCH
        ident = member_identity_for_session(session_key)
        servers = bot_servers(ident["slug"]) if ident else {}
    except Exception:  # noqa: BLE001 - see the docstring
        logger.warning("bot chat tools for %s unreadable", session_key, exc_info=True)
        return spec
    out = dict(spec)
    mounted = dict(out.get("mcpServers") or {})
    shadowed = {alias for alias in servers if alias in mounted}
    if ident:
        # Staged only. The record of what the chat runs is written by record_mounted,
        # once a harness hands the session its final tool surface; a projection that
        # only reads (a settings re-seed, a deny-rule pass) must not change it.
        offered = _fingerprints(servers)
        shadow = {a: f for a, f in offered.items() if a in shadowed}
        _OFFERED[ident["slug"]] = ({a: f for a, f in offered.items() if a not in shadowed}, shadow)
    if not servers:
        # Store the epoch snapshot even when there are no app servers: a later tick
        # or preview-on will bump _CHANGE_EPOCH, and record_mounted's stale check
        # (epoch_stale) needs the snapshot to detect it.  Without this, a Bot that
        # starts with no app servers and then gets one ticked is never reconciled,
        # because record_mounted finds no sidecar and schedules no reconcile.
        if ident:
            _PROJECTION_EPOCH[session_key] = epoch_snapshot
            _PROJECTION_SLUGS[session_key] = ident["slug"]
        return spec
    tools = list(out.get("tools") or [])
    label = f"dashboard:{_slot_of(session_key)}"
    # Store the sidecar: {alias: gen} for every app-server alias offered to this
    # session.  The sidecar stores the original alias only; callers that use a
    # name transform (e.g. the KAS path that folds whitespace via codex_name) pass
    # it as name_transform to check_and_filter_projection so it can look up either form.
    proj_gens: dict[str, int] = {}
    proj_offer_fps: dict[str, str] = {}  # alias -> unstamped offer fingerprint
    for alias, entry in servers.items():
        if alias in shadowed:
            continue
        app_name = alias.split(":")[0] if ":" in alias else None
        if app_name:
            proj_gens[alias] = gen_snapshot_map.get(app_name, 0)
            # Store the unstamped fingerprint before stamp_session_label mutates env.
            # check_and_filter_projection uses this when writing _PENDING_CLOSE_WITHHELD
            # so the stored fp matches bot_servers() output (which is also unstamped).
            proj_offer_fps[alias] = json.dumps(entry, sort_keys=True, default=str)
        mounted[alias] = stamp_session_label(entry, label)
        if "*" not in tools and f"@{alias}" not in tools:
            tools.append(f"@{alias}")
    if proj_gens:
        _PROJECTION_GENS[session_key] = proj_gens
        _PROJECTION_OFFER_FP[session_key] = proj_offer_fps
        _PROJECTION_EPOCH[session_key] = epoch_snapshot
        _HAD_SIDECAR.add(session_key)
        if ident:
            _PROJECTION_SLUGS[session_key] = ident["slug"]
    out["mcpServers"] = mounted
    out["tools"] = tools
    return out


def _live_slot(state: Any, slug: str) -> Any:
    from kiro_crew.members import read_dm_binding

    binding = read_dm_binding(slug)
    return state._slots.get(binding["slot_key"]) if binding else None


#: :func:`reload_bot_chats` outcome for a chat whose queued fresh start covers it.
PENDING_FRESH = "pending_fresh"

_RELOAD_NOTICE = (
    "Session reloaded so this bot's chat picks up the app tools its owner turned on. "
    "The conversation is kept."
)

_FRESH_NOTICE = (
    "App tools were taken off this bot's chat, so its next turn starts a fresh "
    "conversation without them. The messages above stay here."
)


def _tell_bot(slot: Any, notice: str) -> None:
    """Queue *notice* for the Bot's next turn via AUTOMATION_NOTICE_KIND.

    A Bot's chat draws no runtime notice row (the recorded decision "the crewmate
    chat draws messages only"): the row appended beside this shows on the Sessions
    page.  Using the AUTOMATION_NOTICE_KIND queue channel (the same one
    ``_deliver_refusal_to_crewmate`` uses) lets it drain as an inject row on the
    Bot's next turn, so the Bot tells its owner.  Best-effort: a slot without
    ``queue_append`` silently skips the notice, leaving only the visible row.
    """
    from kiro_crew.dashboard.chat_utils import AUTOMATION_NOTICE_KIND

    append = getattr(slot, "queue_append", None)
    if callable(append):
        append(f"[Automation notice] {notice}", kind=AUTOMATION_NOTICE_KIND)


async def reload_bot_chats(state: Any, slugs: Iterable[str]) -> dict[str, str]:
    """Reload each Bot's live chat onto its spec now (idle) or at its turn end (busy).

    So a chat picks up an app server on its next turn. Never raises.
    """
    from kiro_crew.dashboard.crewmate_worker_reload import reload_if_idle

    out: dict[str, str] = {}
    for slug in slugs:
        try:
            slot = await asyncio.to_thread(_live_slot, state, slug)
            if slot is None:
                # DM-binding read failed (e.g. a Windows sharing violation): clear the
                # pending entry so the next reconciliation retries instead of skipping.
                _PENDING.pop(slug, None)
                continue
            if getattr(slot, "_pending_discard_conversation_key", None) is not None:
                # A fresh start is already queued (an app disabled then enabled): its
                # spawn reads the new spec, and a reload beside it would start a second
                # agent process on the same chat at once.
                out[slug] = PENDING_FRESH
                continue
            # Told once its process runs on the new spec: now, or at the turn end
            # that runs the reload a busy chat was left owing.
            tell = functools.partial(_tell_bot, slot, _RELOAD_NOTICE)
            out[slug] = await reload_if_idle(state, slot, notice=_RELOAD_NOTICE, on_reloaded=tell)
        except Exception:  # noqa: BLE001 - a missed reload leaves the next spawn to read it
            _PENDING.pop(slug, None)  # allow the next reconciliation to retry
            logger.warning("bot chat reload for %s failed", slug, exc_info=True)
    return out


async def fresh_bot_chats(state: Any, slugs: Iterable[str]) -> list[str]:
    """Queue a fresh conversation on each Bot's live chat; returns the Bots queued.

    For a server taken AWAY. A reload resumes the same conversation (``session/load``),
    and that history still names the removed tools, so the model keeps offering them.
    This queues the discard the ``reset_conversation`` directive queues
    (``slot._pending_discard_conversation_key``): it lands at the next idle turn
    boundary with ``replay=False``, and the next turn spawns on the narrowed spec with
    no memory of the old tools. The transcript stays in the tab. Never raises.
    """
    from kiro_crew.dashboard.chat_runner import _arm_pending_reset_retry
    from kiro_crew.dashboard.chat_utils import effective_session_key

    queued: list[str] = []
    for slug in slugs:
        try:
            slot = await asyncio.to_thread(_live_slot, state, slug)
            # shortcut: a chat with no live slot keeps its resume pointer; every opened
            # Bot chat has a slot while the gateway runs, so only a restart lands here.
            if slot is None:
                # DM-binding read failed: clear the pending entry so the next
                # reconciliation retries the revocation instead of skipping it.
                _PENDING.pop(slug, None)
                continue
            slot._pending_discard_conversation_key = effective_session_key(slot)
            slot._pending_discard_from_wake = False
            slot.append("system", _FRESH_NOTICE, "msg msg-info")
            _tell_bot(slot, _FRESH_NOTICE)
            _arm_pending_reset_retry(state, slot)
            queued.append(slug)
        except Exception:  # noqa: BLE001 - a missed discard leaves the tools uncallable
            _PENDING.pop(slug, None)  # allow the next reconciliation to retry
            logger.warning("bot chat fresh start for %s failed", slug, exc_info=True)
    return queued


_HARNESS_NOTICE = (
    "This bot's engine can't use app tools, so they're off in this chat. Switch the "
    "bot to Kiro Agent, Claude Code or Codex to use them."
)
#: Bot slugs told :data:`_HARNESS_NOTICE` in this process, so each cold start does
#: not repeat it; forgotten once the Bot starts on a harness that carries them.
_HARNESS_NOTED: set[str] = set()
#: Bot slugs whose chat just started without its app tools and is owed the notice.
_HARNESS_OWED: set[str] = set()


async def note_harness_without_app_tools(session_key: str, carried: bool) -> None:
    """At a Bot chat's cold start: on a harness that cannot carry its per-session spec,
    mark the chat owed a notice when the Bot has app tools turned on. Never raises.

    *carried* is the provider's ``mate_worker_kit_supported``: the same opt-in set
    (``ACP_BACKENDS_MATE_WORKER_KIT``) a crewmate's worker is refused on. A Bot chat on
    such a harness (Kiro CLI, which reads ``--agent`` off disk) never calls
    :func:`conductor_session_spec`, so nothing mounts. The allocation door has no
    slot in hand, so the dashboard turn that claimed the session posts it
    (:func:`deliver_harness_notice`).
    """
    from kiro_crew.members import member_identity_for_session

    try:
        ident = await asyncio.to_thread(member_identity_for_session, session_key)
        if not ident:
            return
        slug = ident["slug"]
        if carried:
            _HARNESS_NOTED.discard(slug)
            _HARNESS_OWED.discard(slug)
            return
        if slug in _HARNESS_NOTED or not await asyncio.to_thread(bot_servers, slug):
            return
        _HARNESS_OWED.add(slug)
    except Exception:  # noqa: BLE001 - the chat still starts, only the notice is lost
        logger.warning("bot chat harness notice for %s failed", session_key, exc_info=True)


async def deliver_harness_notice(slot: Any, session_key: str) -> None:
    """Post the notice :func:`note_harness_without_app_tools` left owed on this Bot chat's
    *slot*, once per process. Never raises."""
    if not _HARNESS_OWED:
        return
    from kiro_crew.members import member_identity_for_session

    try:
        ident = await asyncio.to_thread(member_identity_for_session, session_key)
        slug = ident["slug"] if ident else ""
        if slug not in _HARNESS_OWED:
            return
        slot.append("system", _HARNESS_NOTICE, "msg msg-info")
        _tell_bot(slot, _HARNESS_NOTICE)
        _HARNESS_OWED.discard(slug)
        _HARNESS_NOTED.add(slug)
    except Exception:  # noqa: BLE001 - only the notice is lost
        logger.warning("bot chat harness notice for %s failed", session_key, exc_info=True)


def session_has_app_mount(session_key: str) -> bool:
    """Whether *session_key* is a Bot chat that has app servers recorded as mounted.

    Read by the allocator before it ends a start it refused, so the end goes
    through the provider's own shutdown and its verdict is kept.
    """
    snap = _SESSION_APP_SNAPSHOT_PENDING.get(session_key)
    if snap is not None and snap[1]:
        return True
    return any(
        sk == session_key and any(":" in a for a in _MOUNTED.get(slug, {}))
        for slug, sk in _MOUNTED_SESSION_KEYS.items()
    )


def app_has_active_bot_sessions(app_name: str) -> bool:
    """Return True when *app_name* has mounted, pending-delivery or pending-close
    Bot chat sessions.

    Callers should use this instead of reading ``_MOUNTED`` or ``_PENDING_CLOSE_APPS``
    directly, so route handlers stay off private state.
    """
    if any(app_name in apps for apps in _live_apps().values()):
        return True
    if any(app_name in apps for apps in _PENDING_DELIVERY.values()) or _starting_slugs(app_name):
        return True
    return any(a == app_name for _, a in _PENDING_CLOSE_APPS)


def _starting_slugs(app_name: str) -> list[str]:
    """Bots offered *app_name* whose chat has a native session being created or loaded
    now (``bot_chat_ending_fence.recorded_session_start``): its record may already
    be rewritten without the app while the session it replaces still runs it."""
    prefix = f"{app_name}:"
    return [
        slug
        for slug, key in list(_MOUNTED_SESSION_KEYS.items())
        if start_in_flight(key) and any(a.startswith(prefix) for a in _OFFERED.get(slug, ({},))[0])
    ]


def mounted_app_names() -> frozenset[str]:
    """Return the set of app names with at least one mounted alias in an active Bot chat."""
    return frozenset(app for apps in _live_apps().values() for app in apps)


def pending_close_app_names() -> frozenset[str]:
    """Return the set of app names with pending-close handles (unconfirmed KAS close)."""
    return frozenset(a for _, a in _PENDING_CLOSE_APPS)


def in_flight_projection_app_names() -> frozenset[str]:
    """Return app names currently in an in-flight projection (_PROJECTION_GENS)."""
    return frozenset(
        alias.split(":")[0] for gens in _PROJECTION_GENS.values() for alias in gens if ":" in alias
    )


def bind_snapshot_to_native_sid(session_key: str, native_sid: str, runtime: Any = None) -> None:
    """Migrate a pending snapshot from conversation key to native KAS session id.

    Called by :func:`~kiro_crew.acp.harness.kas.KasHarness.record_session_projection`
    after ``session/new`` completes and the native session id is available.
    Moves the ``(slug, apps)`` tuple from :data:`_SESSION_APP_SNAPSHOT_PENDING`
    (keyed by conversation key *session_key*) into :data:`_SESSION_APP_SNAPSHOT`
    (keyed by native *native_sid*).

    If no pending entry exists the call is a no-op: the session may not be a Bot
    chat with app tools, or record_mounted has not run yet. An empty *native_sid*
    is a no-op too, so the pending entry stays where note_unconfirmed_close finds it.
    """
    if not native_sid:
        return
    pending = _SESSION_APP_SNAPSHOT_PENDING.pop(session_key, None)
    if pending is not None:
        _SESSION_APP_SNAPSHOT[native_sid] = pending
        if runtime is not None:
            _SESSION_RUNTIMES[native_sid] = runtime


def _live_apps() -> dict[str, frozenset[str]]:
    """``{Bot slug: app names}`` that some session of the Bot not confirmed closed may
    run: the current record and every native session's own snapshot, bound or still
    pending. A projection writes only its own session's entry, so a replacement that
    mounts nothing leaves the session it replaces visible here until that one's close
    is confirmed (:func:`forget_closed_session`)."""
    out: dict[str, set[str]] = {}
    for slug, mounted in list(_MOUNTED.items()):
        out.setdefault(slug, set()).update(a.split(":")[0] for a in mounted if ":" in a)
    for slug, apps in [*_SESSION_APP_SNAPSHOT.values(), *_SESSION_APP_SNAPSHOT_PENDING.values()]:
        out.setdefault(slug, set()).update(apps)
    return {slug: frozenset(apps) for slug, apps in out.items() if apps}


def forget_closed_session(native_sid: str) -> None:
    """Drop the record of a native session whose close is confirmed
    (``session_provider_shared.teardown_confirmed``): the one way it leaves."""
    _SESSION_APP_SNAPSHOT.pop(native_sid, None)
    _SESSION_RUNTIMES.pop(native_sid, None)


def _keep_left_sessions(slug: str, session_key: str) -> bool:
    """After a confirmed stop of *slug*'s chat: hand every other session of the Bot
    still on record to the close retry. True when any was left."""
    _SESSION_APP_SNAPSHOT_PENDING.pop(session_key, None)  # the chat just stopped
    left = [sid for sid, (s, _apps) in list(_SESSION_APP_SNAPSHOT.items()) if s == slug]
    for sid in left:
        from kiro_crew.bot_chat_ending_fence import _UnobservedKill

        note_unconfirmed_close(
            session_key, _SESSION_RUNTIMES.pop(sid, None) or _UnobservedKill(), sid
        )
    return bool(left)


def note_unconfirmed_close(session_key: str, rt: Any, sid: str | None) -> None:
    """Record an unconfirmed KAS session/close handle for *session_key*.

    Called from teardown when ``_kas_shutdown_unconfirmed`` is True, regardless
    of which code path requested the discard—reset, rewind, regenerate, context
    cap, slot repair, or any other caller.  Looks up the slug and app names from
    :data:`_SESSION_APP_SNAPSHOT` (keyed by the **native** KAS session id *sid*,
    migrated there by :func:`bind_snapshot_to_native_sid`), falling back to
    :data:`_SESSION_APP_SNAPSHOT_PENDING` (keyed by conversation key, written by
    :func:`record_mounted` before the native id was available) and then to
    :data:`_MOUNTED_SESSION_KEYS` and :data:`_MOUNTED` for sessions torn down
    before their first spec delivery.

    Keying by native session id rather than conversation key is the fix for the
    swap_conversation overwrite race: even when a new native session is created for
    the same conversation, it gets a different native id and therefore a different
    snapshot entry.  The old session's snapshot at ``_SESSION_APP_SNAPSHOT[old_sid]``
    survives, so its apps are correctly registered for retry.

    Not async: called from inside a ``finally``-adjacent block in teardown. A
    handle with no session id is kept under the same token
    ``session_provider_shared.note_unconfirmed_teardown`` uses, so the retry still
    has something to settle.
    """
    sid = sid or f"unconfirmed-teardown:{id(rt)}"
    # Primary path: look up the per-session snapshot migrated to native sid.
    # Survives a _MOUNTED[slug] overwrite and a _MOUNTED_SESSION_KEYS rebind.
    snap = _SESSION_APP_SNAPSHOT.pop(sid, None)
    _SESSION_RUNTIMES.pop(sid, None)
    if snap is None:
        # Fallback 1: bind_snapshot_to_native_sid has not been called yet (the
        # session was torn down in the window between record_mounted and
        # bind_snapshot_to_native_sid).  Check the pending table, keyed by
        # conversation key.
        snap = _SESSION_APP_SNAPSHOT_PENDING.pop(session_key, None)
    if snap is not None:
        slug, apps = snap
        if not apps:
            return  # snapshot exists but session carried no app servers
    else:
        # Fallback 2: session was torn down before record_mounted ran (e.g.
        # torn down during the delivery window, or is not a Bot chat with app
        # tools).  Fall back to _MOUNTED_SESSION_KEYS / _MOUNTED; accept the
        # race risk for this narrow path only.
        slug_or_none: str | None = None
        for s, sk in _MOUNTED_SESSION_KEYS.items():
            if sk == session_key:
                slug_or_none = s
                break
        if slug_or_none is None:
            return  # not a Bot chat with a mounted app server
        slug = slug_or_none
        apps = frozenset(alias.split(":")[0] for alias in _MOUNTED.get(slug, {}) if ":" in alias)
        if not apps:
            return  # no app servers mounted; nothing to retain

    for app_name in apps:
        _PENDING_CLOSE.setdefault((slug, app_name), []).append((rt, sid))
        _PENDING_CLOSE_APPS.add((slug, app_name))
        _schedule_pending_close_drain(slug, app_name)
    logger.warning(
        "bot chat app tools: discard for %s (slug=%s apps=%s) "
        "was not confirmed by KAS; handle registered for retry",
        session_key,
        slug,
        sorted(apps),
    )


def _schedule_pending_close_drain(slug: str, app_name: str) -> None:
    """Schedule one back-off timer for ``(slug, app_name)`` if none is active.

    Uses ``loop.call_later`` (the same pattern as :func:`_schedule_reconcile`) so
    tests can patch it without triggering real async sleeps.  Idempotent: if a
    timer is already armed for this pair, this call does nothing.  Called from
    :func:`note_unconfirmed_close` and from :func:`_drain_pending_close` after a
    failed drain when handles remain.

    Not async; safe to call from any thread while the event loop runs.
    """
    if _RECONCILER is None:
        # _RECONCILER is set by after_app_change when the gateway's first app/config
        # change fires.  If a close times out before that (e.g. the gateway just
        # restarted and no app change has occurred yet), try to get the running event
        # loop directly so the retry timer can still be armed.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "bot chat app tools: no event loop available to schedule drain for "
                "(%s, %s); retry will fire when after_app_change next runs",
                slug,
                app_name,
            )
            return
        key = (slug, app_name)
        if key in _PENDING_CLOSE_TIMERS:
            return  # already armed
        idx = _PENDING_CLOSE_BACKOFF_IDX.get(key, 0)
        delay = _PENDING_CLOSE_BACKOFFS[min(idx, len(_PENDING_CLOSE_BACKOFFS) - 1)]

        def _fire_no_reconciler() -> None:
            _PENDING_CLOSE_TIMERS.pop(key, None)
            _PENDING_CLOSE_BACKOFF_IDX[key] = idx + 1
            loop.create_task(_timed_drain_and_reschedule(slug, app_name))

        try:
            handle = loop.call_later(delay, _fire_no_reconciler)
            _PENDING_CLOSE_TIMERS[key] = handle
        except RuntimeError:
            pass
        return
    key = (slug, app_name)
    if key in _PENDING_CLOSE_TIMERS:
        return  # already armed
    idx = _PENDING_CLOSE_BACKOFF_IDX.get(key, 0)
    delay = _PENDING_CLOSE_BACKOFFS[min(idx, len(_PENDING_CLOSE_BACKOFFS) - 1)]
    _state, loop = _RECONCILER

    def _fire() -> None:
        _PENDING_CLOSE_TIMERS.pop(key, None)
        _PENDING_CLOSE_BACKOFF_IDX[key] = idx + 1
        loop.create_task(_timed_drain_and_reschedule(slug, app_name))

    try:
        handle = loop.call_later(delay, _fire)
        _PENDING_CLOSE_TIMERS[key] = handle
    except RuntimeError:
        pass


async def _timed_drain_and_reschedule(slug: str, app_name: str) -> None:
    """Run :func:`_drain_pending_close` for ``(slug, app_name)`` and reschedule if
    handles remain.  Called only from the timer set by
    :func:`_schedule_pending_close_drain`.
    """
    await _drain_pending_close()
    # If the handle was confirmed, _drain_pending_close removed the key already.
    # If it still exists, schedule the next back-off retry.
    if (slug, app_name) in _PENDING_CLOSE_APPS:
        _schedule_pending_close_drain(slug, app_name)


def cancel_pending_close_timers() -> None:
    """Cancel all active back-off timers.  Called on gateway shutdown so no drain
    fires after the event loop is closed.
    """
    for handle in list(_PENDING_CLOSE_TIMERS.values()):
        handle.cancel()
    _PENDING_CLOSE_TIMERS.clear()
    _PENDING_CLOSE_BACKOFF_IDX.clear()


def confirm_last_lease_killed(sid: str) -> None:
    """Confirm all pending-close handles matching *sid* as stopped, without retrying.

    Called by :func:`~kiro_crew.acp.session_provider_shared.AcpSessionProvider.shutdown`
    after the shared KAS runtime's process tree is successfully killed (the last-lease
    path in ``shutdown_shared``).  At that point the KAS process is dead, so every
    session it held—including the one whose close was originally unconfirmed—is
    certainly stopped.  Treat the handle as confirmed and perform the same cleanup
    that :func:`_drain_pending_close` does on successful eviction: remove it from
    :data:`_PENDING_CLOSE`, discard from :data:`_PENDING_CLOSE_APPS`, clear the
    revocation fence, and schedule a reconcile so the Bot can remount.

    Synchronous: safe to call from non-async context (the ``shutdown`` finally block).
    Does NOT cancel retry timers for the affected keys—the timers will fire and
    find the key already gone.
    """
    if not _PENDING_CLOSE:
        return
    confirmed_keys: list[tuple[str, str]] = []
    for key, handles in list(_PENDING_CLOSE.items()):
        new_handles = [(rt, s) for rt, s in handles if s != sid]
        if len(new_handles) < len(handles):
            # At least one handle for this sid was present.
            if new_handles:
                _PENDING_CLOSE[key] = new_handles
            else:
                confirmed_keys.append(key)
    for key in confirmed_keys:
        slug, app_name = key
        _PENDING_CLOSE.pop(key, None)
        _PENDING_CLOSE_APPS.discard(key)
        _PENDING_CLOSE_RETRIES.pop(sid, None)
        if (timer := _PENDING_CLOSE_TIMERS.pop(key, None)) is not None:
            timer.cancel()
        _PENDING_CLOSE_BACKOFF_IDX.pop(key, None)
        _revocation_fence_clear(app_name)
        withheld_for_slug = _PENDING_CLOSE_WITHHELD.get(slug, {})
        for a in [a for a in withheld_for_slug if a.split(":")[0] == app_name]:
            withheld_for_slug.pop(a, None)
        if not withheld_for_slug:
            _PENDING_CLOSE_WITHHELD.pop(slug, None)
        _schedule_reconcile()
    if confirmed_keys:
        logger.info(
            "bot chat app tools: last-lease kill confirmed %d (slug, app) pairs for sid %s",
            len(confirmed_keys),
            sid,
        )


async def _drain_pending_close() -> None:
    """Retry every unconfirmed KAS session/close handle, independent of _MOUNTED.

    Called at the start of :func:`stop_app_bot_chat_servers` and
    :func:`after_app_change` so handles are retried even when the Bot cold-started
    into a new session (meaning its slug is gone from :data:`_MOUNTED` or points to
    a different session key).  A host answer of "unknown / already closed" (evict
    returns ``True``) confirms the close—no extra check needed.  Confirmed handles are
    removed; still-unconfirmed ones stay.  When a key is fully confirmed its pair is
    removed from :data:`_PENDING_CLOSE_APPS`, unblocking the delivery gate for that
    (slug, app) combination so the reconcile that fires next can re-mount the servers.
    """
    if not _PENDING_CLOSE:
        return
    # First, collect unique session handles across all keys to avoid calling
    # evict_session multiple times for the same KAS session_id.  The same handle
    # (runtime, session_id) may appear under several (slug, app) keys when a session
    # mounted multiple apps.
    unique_handles: dict[str, tuple[Any, str]] = {}  # {session_id: (runtime, session_id)}
    for handles in _PENDING_CLOSE.values():
        for pc_runtime, pc_sid in list(handles):
            if pc_sid not in unique_handles:
                unique_handles[pc_sid] = (pc_runtime, pc_sid)

    confirmed_sids: set[str] = set()

    for pc_sid, (pc_runtime, _) in list(unique_handles.items()):
        retries = _PENDING_CLOSE_RETRIES.get(pc_sid, 0)
        if retries >= _PENDING_CLOSE_MAX_RETRIES:
            # Max retries reached.  We do NOT force-confirm: without evidence the
            # session is closed, treating it as stopped risks leaving app code
            # running.  Log a warning so the operator can see repeated 409s and
            # investigate; retrying will keep trying.
            logger.warning(
                "bot chat app tools: drain: session %s has reached max retries (%d) "
                "without confirmation; keeping handle — user will see continued 409",
                pc_sid,
                _PENDING_CLOSE_MAX_RETRIES,
            )
            # Reset counter so the next drain attempt is not immediately blocked.
            _PENDING_CLOSE_RETRIES.pop(pc_sid, None)
            continue
        try:
            confirmed = await pc_runtime.evict_session(pc_sid)
        except Exception:  # noqa: BLE001
            # evict_session does not normally raise: it returns True when the host
            # rejects the id or the runtime is dead, and False on timeout.  An
            # unexpected exception here is treated as unconfirmed.
            confirmed = False
            logger.warning(
                "bot chat app tools: drain: unexpected error closing session %s",
                pc_sid,
                exc_info=True,
            )
        if confirmed:
            logger.info("bot chat app tools: drain: session %s confirmed gone", pc_sid)
        else:
            _PENDING_CLOSE_RETRIES[pc_sid] = retries + 1
            logger.warning(
                "bot chat app tools: drain: retry close for session %s still "
                "unconfirmed (attempt %d/%d)",
                pc_sid,
                retries + 1,
                _PENDING_CLOSE_MAX_RETRIES,
            )
        if confirmed:
            confirmed_sids.add(pc_sid)
            _PENDING_CLOSE_RETRIES.pop(pc_sid, None)

    # Remove confirmed handles from ALL keys that hold them, then unblock
    # each key whose handle list is now empty.
    for key in list(_PENDING_CLOSE.keys()):
        slug, app_name = key
        key_handles: list[tuple[Any, str]] = _PENDING_CLOSE.get(key) or []
        if not key_handles:
            _PENDING_CLOSE.pop(key, None)
            _PENDING_CLOSE_APPS.discard(key)
            # Cancel any pending retry timer for this (slug, app) pair.
            if (timer := _PENDING_CLOSE_TIMERS.pop(key, None)) is not None:
                timer.cancel()
            _PENDING_CLOSE_BACKOFF_IDX.pop(key, None)
            _revocation_fence_clear(app_name)
            # Remove any withheld aliases for this (slug, app) so the next
            # after_app_change sees them as a new addition and mounts them.
            withheld_for_slug = _PENDING_CLOSE_WITHHELD.get(slug, {})
            for a in [a for a in withheld_for_slug if a.split(":")[0] == app_name]:
                withheld_for_slug.pop(a, None)
            if not withheld_for_slug:
                _PENDING_CLOSE_WITHHELD.pop(slug, None)
            _schedule_reconcile()
            continue
        for handle in list(key_handles):
            _, pc_sid = handle
            if pc_sid in confirmed_sids:
                try:
                    _PENDING_CLOSE[key].remove(handle)
                except (KeyError, ValueError):
                    pass  # already removed by a concurrent call
        # Unblock only when the list is now genuinely empty.
        remaining = _PENDING_CLOSE.get(key)
        if remaining is not None and len(remaining) == 0:
            _PENDING_CLOSE.pop(key, None)
            _PENDING_CLOSE_APPS.discard(key)
            # Cancel any pending retry timer for this (slug, app) pair.
            if (timer := _PENDING_CLOSE_TIMERS.pop(key, None)) is not None:
                timer.cancel()
            _PENDING_CLOSE_BACKOFF_IDX.pop(key, None)
            # Last handle for this (slug, app) confirmed: lower the revocation
            # fence if no other slugs still have handles for this app.
            _revocation_fence_clear(app_name)
            # Remove all withheld aliases for this (slug, app) so the next
            # after_app_change sees them as a new addition and re-mounts them.
            withheld_for_slug = _PENDING_CLOSE_WITHHELD.get(slug, {})
            for a in [a for a in withheld_for_slug if a.split(":")[0] == app_name]:
                withheld_for_slug.pop(a, None)
            if not withheld_for_slug:
                _PENDING_CLOSE_WITHHELD.pop(slug, None)
            _schedule_reconcile()


async def after_app_change(state: Any) -> None:
    """After a change that can move a Bot chat's app servers, bring each live chat in line.

    Each Bot chat is compared with what its agent process was started with
    (``_MOUNTED``): one that LOST a server starts a fresh conversation, one whose
    servers were added or whose entries changed (an app update) reloads. Called after
    an app is enabled, disabled, updated or uninstalled, a Bot is ticked or unticked,
    and the preview is switched. Never raises.

    Does NOT bump :data:`_APP_GENERATIONS`: generation is bumped only by
    :func:`revoke_app_generation` on security/trust paths.  Bumping it here would
    stale every in-flight projection on every reconcile cycle, creating a feedback
    loop where _schedule_reconcile triggers another after_app_change which bumps the
    counter again.
    """
    global _RECONCILER, _CHANGE_EPOCH
    if state is None:
        return
    # Drain any unconfirmed KAS close handles before reconciling, so a session
    # that cold-started into a new slug gets its pending-close handles confirmed
    # here rather than waiting for another stop call.  Confirmed handles remove
    # themselves from _PENDING_CLOSE_APPS, unblocking the delivery gate.
    await _drain_pending_close()
    _CHANGE_EPOCH += 1
    _RECONCILER = (state, asyncio.get_running_loop())
    lost: list[str] = []
    changed: list[str] = []
    for slug, was in list(_MOUNTED.items()):
        try:
            now = _fingerprints(await asyncio.to_thread(bot_servers, slug))
        except Exception:  # noqa: BLE001 - the change has committed
            logger.warning("bot chat tools for %s unreadable", slug, exc_info=True)
            continue
        # An offer the last process did not get (withheld or shadowed) counts only
        # once its entry changes, since that change may make it mountable now.
        # Aliases suppressed only by a pending close (not a harness permanent block)
        # are tracked in _PENDING_CLOSE_WITHHELD; they must also be excluded from
        # the "changed" check until drain confirms them, so the reconcile does not
        # trigger a spurious reload.  Once the drain clears the entry the alias
        # becomes visible in the next reconcile and a reload is scheduled normally.
        # For _PENDING_CLOSE_WITHHELD entries where the running session already holds
        # the server (alias in was): preserve was[a] so now==was holds -- the running
        # session is unaffected while only the NEXT cold start is blocked.
        # For _PENDING_CLOSE_WITHHELD entries where the alias was never mounted
        # (alias not in was): exclude from now (same as _UNMOUNTED) so a new delivery
        # being blocked does not look like a new addition.
        pending_close_withheld = _PENDING_CLOSE_WITHHELD.get(slug, {})
        unmounted = _UNMOUNTED.get(slug, {})
        filtered_now: dict[str, str] = {}
        for a, f in now.items():
            pcw_fp = pending_close_withheld.get(a)
            if unmounted.get(a) == f:
                continue  # harness-withheld; unchanged offer
            if pcw_fp is not None and pcw_fp == f:
                # Pending-close is blocking only THIS alias's fp (unchanged offer).
                if a in was:
                    # Running session already holds this server: keep was[a] so
                    # now==was holds and no reload fires.
                    filtered_now[a] = was[a]
                # else: not yet mounted; exclude entirely (no reload, no fresh).
            else:
                filtered_now[a] = f
        now = filtered_now
        if now == was:
            # Back to what the chat runs (an addition undone before it applied): no
            # fresh start, and an owed reload only respawns the same servers.
            _PENDING.pop(slug, None)
            continue
        if _PENDING.get(slug) == now:
            continue  # already queued for exactly this
        _PENDING[slug] = now
        (lost if was.keys() - now.keys() else changed).append(slug)
    if lost:
        await fresh_bot_chats(state, lost)
    if changed:
        schedule_reload(state, changed)


async def on_preview_change(state: Any, change: Any) -> None:
    """Config applier: a flip of this preview reconciles every Bot chat."""
    if change.touched(f"dashboard.feature_previews.{PREVIEW}"):
        await after_app_change(state)


async def on_execution_trust_change(state: Any) -> None:
    """Stop Bot chats whose mounted app servers are denied execution admission.

    Called when the execution-trust configuration changes (``agent.apps_allow_third_party``,
    ``agent.apps_trusted``, or their repository/local variants).  For each distinct app
    name that any Bot chat currently runs, re-checks ``app_execution_denied``.  Any app
    whose admission is now denied is immediately revoked: its generation is bumped and its
    Bot sessions are stopped, the same as the individual trust-revocation path.

    Never raises.
    """
    from kiro_crew.apps.execution import app_execution_denied

    if state is None:
        return
    # Collect the distinct app names across all mounted Bot chats and all sessions
    # still in the pending-delivery window (gate passed, record_mounted not yet run).
    # Both sets must be checked: a session in _PENDING_DELIVERY has already been
    # handed the app's server array and must be stopped before it registers.
    app_slugs: dict[str, list[str]] = {}  # {app_name: [slug, ...]}
    for slug, live in _live_apps().items():
        for app_name in live:
            app_slugs.setdefault(app_name, []).append(slug)
    for _sk, pending_apps in list(_PENDING_DELIVERY.items()):
        for app_name in pending_apps:
            app_slugs.setdefault(app_name, [])
    # Also include apps with unconfirmed KAS close handles: those sessions may
    # still be resident and running app code.
    for _slug, app_name in _PENDING_CLOSE_APPS:
        app_slugs.setdefault(app_name, [])
    if not app_slugs:
        return
    denied_apps: list[str] = []
    for app_name in app_slugs:
        try:
            reason = await asyncio.to_thread(
                app_execution_denied, app_name, action="bot_chat_serve"
            )
            if reason is not None:
                denied_apps.append(app_name)
        except Exception:  # noqa: BLE001
            logger.warning(
                "bot chat app tools: execution-trust check for %r failed", app_name, exc_info=True
            )
    for app_name in denied_apps:
        try:
            revoke_app_generation(app_name)
            _stopped, _failed = await stop_app_bot_chat_servers(state, app_name)
            if _failed:
                logger.warning(
                    "bot chat app tools: execution-trust change for %r: "
                    "could not stop Bot chats for slugs %s",
                    app_name,
                    _failed,
                )
        except Exception:  # noqa: BLE001
            logger.warning(
                "bot chat app tools: execution-trust revocation for %r failed",
                app_name,
                exc_info=True,
            )


def revocation_fence_clear(app_name: str) -> None:
    """Public alias for :func:`_revocation_fence_clear` (no hold release).

    Use this from route handlers so they do not read ``bot_chat_app_tools._*``.
    """
    _revocation_fence_clear(app_name)


@contextlib.asynccontextmanager
async def app_stop_fence(app_name: str) -> AsyncIterator[None]:
    """Hold the revocation fence for *app_name* for the span of the ``with`` body.

    Every lifecycle operation that stops an app's code (disable, uninstall,
    trust revoke, the blanket-trust sweep, session-approval re-consent) enters this
    BEFORE its teardown starts and leaves it AFTER the disabled / uninstalled /
    trust-revoked state is on disk. While it is held, :func:`record_mounted`
    voids the app for every Bot, so a Bot chat that cold-starts during any await
    of the teardown is handed an array without the app's servers. Spans nest: the
    fence drops when the last one exits and no pending-close handle remains. The
    release runs on every exit, an exception or a cancellation included.
    """
    with _DELIVERY_LOCK:
        _FENCE_HOLD_COUNTS[app_name] = _FENCE_HOLD_COUNTS.get(app_name, 0) + 1
        _REVOCATION_FENCE.add(app_name)
    try:
        yield
    finally:
        _revocation_fence_clear(app_name, release_hold=True)


def _revocation_fence_clear(app_name: str, *, release_hold: bool = False) -> None:
    """Lower the revocation fence for *app_name* when safe to do so.

    Safe means no pending-close handles remain for the app (across all slugs):
    if any ``(slug, app_name)`` key still has unconfirmed KAS close handles, the
    fence must stay up so the delivery gate keeps blocking new mounts until all
    old sessions are confirmed stopped.  Call :func:`_drain_pending_close` first
    to give outstanding handles a chance to be confirmed.

    Also safe means no :func:`app_stop_fence` span is open for the app.
    :data:`_FENCE_HOLD_COUNTS` tracks those spans; the fence is only discarded once
    the count reaches zero.

    *release_hold=True* is :func:`app_stop_fence`'s own exit and nothing else's: it
    decrements the count and, at zero, lowers the fence (pending-close
    permitting). Internal paths (drain, the no-mounted-Bot early return) never
    pass it: they hold no span and defer to the open ones.
    """
    if release_hold:
        count = _FENCE_HOLD_COUNTS.get(app_name, 0)
        if count > 0:
            new_count = count - 1
            if new_count == 0:
                del _FENCE_HOLD_COUNTS[app_name]
            else:
                _FENCE_HOLD_COUNTS[app_name] = new_count
                return  # still held by another owner
        # count was 0 or is now 0 — fall through to the pending-close check
    elif _FENCE_HOLD_COUNTS.get(app_name, 0) > 0:
        # An app_stop_fence span is open; defer to it.
        return
    has_pending = any(a == app_name for _, a in _PENDING_CLOSE_APPS)
    if not has_pending:
        _REVOCATION_FENCE.discard(app_name)


def schedule_reload(state: Any, slugs: list[str]) -> "asyncio.Task | None":
    """:func:`reload_bot_chats` as a task, kept alive on *state*."""
    if state is None or not slugs:
        return None
    try:
        task = asyncio.get_running_loop().create_task(reload_bot_chats(state, slugs))
    except RuntimeError:
        return None
    tasks = getattr(state, "_crewmate_reload_tasks", None)
    if not isinstance(tasks, set):
        tasks = set()
        state._crewmate_reload_tasks = tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task


async def stop_app_bot_chat_servers(state: Any, app_name: str) -> tuple[list[str], list[str]]:
    """Immediately stop every Bot chat that mounts *app_name*'s servers.

    For security-critical paths (trust withdrawal, disable, uninstall) that must
    not leave app code running in Bot chats after the grant is gone.

    Unlike :func:`after_app_change`, which queues a deferred fresh-start that only
    takes effect at the next idle boundary, this function calls
    ``discard_conversation`` directly on each affected Bot chat without
    ``skip_if_busy``, so the session's provider (KAS or ACP) is told to shut down
    regardless of whether a turn is in progress.

    Session identity is resolved from :data:`_MOUNTED_SESSION_KEYS`, which is written
    at mount time, so teardown does not rely on a live DM-binding read.  If no
    session key was recorded the slug is treated as a failure (the process may still
    be running and cannot be positively confirmed stopped), unless the slot is also
    absent from the session manager (confirmed down).

    Callers run this inside :func:`app_stop_fence`, which keeps the fence up until
    their state write lands; the fence clear on this function's own exit then
    defers to that span.

    Returns ``(stopped, failed)``: the Bot slugs whose sessions were discarded, and
    those whose discard call raised. Never raises; failures are returned so callers
    can decide whether to 409.
    """
    from kiro_crew.bot_chat_ending_fence import discard_with_ending_fence

    # Ensure the reconciler is registered even when no app/config change has fired
    # since the last gateway restart.  After a restart without any app mutation,
    # _RECONCILER is None, so _schedule_reconcile called by _drain_pending_close
    # on a successful retry would be a no-op and the app's servers would never be
    # remounted.  Registering here — before any drain or stop — means the first
    # security-triggered stop call seeds the reconciler so close-recovery can
    # schedule after_app_change independently.
    global _RECONCILER
    if _RECONCILER is None and state is not None:
        try:
            loop = asyncio.get_running_loop()
            _RECONCILER = (state, loop)
        except RuntimeError:
            pass
    # Drain any pending-close handles first, independent of _MOUNTED, so handles
    # from earlier cold starts are confirmed before we scan for new affected slugs.
    await _drain_pending_close()
    # Collect any (slug, app_name) pairs that STILL have unconfirmed handles after
    # the drain — these are failures regardless of whether _MOUNTED has an entry.
    # This scan runs before the early-return check so a "nothing mounted" state
    # still returns failures for unconfirmed handles (e.g. after a cold start).
    failed: list[str] = []
    for (pc_slug, pc_app), handles in _PENDING_CLOSE.items():
        if pc_app == app_name and handles and pc_slug not in failed:
            logger.warning(
                "bot chat app tools: unconfirmed handle for %s/%s after drain; "
                "reporting failure for retry",
                pc_slug,
                pc_app,
            )
            failed.append(pc_slug)
    with _DELIVERY_LOCK:
        pending_session_keys = [
            sk for sk, apps in list(_PENDING_DELIVERY.items()) if app_name in apps
        ]
        affected = [slug for slug, live in _live_apps().items() if app_name in live]
        affected += [s for s in _starting_slugs(app_name) if s not in affected]
    if not affected and not pending_session_keys:
        # No mounted or pending-delivery sessions to stop.  The clear defers to
        # an open app_stop_fence span and to unconfirmed handles.
        _revocation_fence_clear(app_name)
        return [], failed  # may carry pre-drain failures

    stopped: list[str] = []
    sessions = getattr(state, "sessions", None)
    try:
        for slug in affected:
            try:
                session_key = _MOUNTED_SESSION_KEYS.get(slug)
                if not session_key:
                    # No session key recorded at mount time: fail closed.
                    failed.append(slug)
                    continue
                if sessions is None:
                    failed.append(slug)
                    continue
                # Unconfirmed handles for (slug, app_name) were retried and reported
                # above; still stop the current session, which a pending handle of a
                # predecessor session does not cover.
                if _PENDING_CLOSE.get((slug, app_name)):
                    logger.debug(
                        "bot chat app tools: prior unconfirmed close for %s/%s still pending; "
                        "proceeding to stop the current session anyway",
                        slug,
                        app_name,
                    )
                # Snapshot before any await: a cold start between awaits can
                # overwrite _MOUNTED[slug].  We stop exactly the session we
                # identified, and only clear _MOUNTED if we stopped it.
                session_key_snapshot = session_key
                mounted_snapshot = dict(_MOUNTED.get(slug, {}))
                unconfirmed_out: list = []
                confirmed, in_flight = await discard_with_ending_fence(
                    sessions, session_key, unconfirmed_out
                )
                if in_flight:
                    # A start under this key is inside provider.start(), or was
                    # refused while the fence was up and hard-killed with an outcome
                    # nothing reads back: "no registered session" is not a
                    # confirmation. The mounted record stays and the stop reports
                    # incomplete; the refused start ends through its own shutdown,
                    # which registers an unconfirmed close if it cannot confirm.
                    logger.warning(
                        "bot chat app tools: %s (app %s) has a start in flight (%s); "
                        "stop not confirmed",
                        slug,
                        app_name,
                        in_flight,
                    )
                    failed.append(slug)
                    continue
                if not confirmed:
                    logger.warning(
                        "bot chat app tools: discard_conversation unconfirmed for %s (app %s); "
                        "KAS session/close timed out or was refused",
                        slug,
                        app_name,
                    )
                    if unconfirmed_out:
                        pc_runtime, pc_sid = unconfirmed_out[0]
                        # Register the unconfirmed handle under EVERY app that this
                        # session was running (not just the current app_name).  A session
                        # that mounts both app A and app B shares one KAS session_id;
                        # confirming it confirms the close for both apps at once.
                        # Only registering under the current app leaves the other apps
                        # invisible to the retry path: when app B is stopped next, it
                        # finds no pending handle, discard returns True, and B is
                        # incorrectly treated as stopped even though the KAS session
                        # (which carries B's server too) is still resident.
                        apps_in_session = {
                            alias.split(":")[0] for alias in mounted_snapshot if ":" in alias
                        } or {app_name}
                        for _shared_app in apps_in_session:
                            _PENDING_CLOSE.setdefault((slug, _shared_app), []).append(
                                (pc_runtime, pc_sid)
                            )
                            _PENDING_CLOSE_APPS.add((slug, _shared_app))
                    failed.append(slug)
                    continue
                # Only remove from _MOUNTED when confirmed stopped AND snapshot matches.
                if (
                    _MOUNTED_SESSION_KEYS.get(slug) == session_key_snapshot
                    and dict(_MOUNTED.get(slug, {})) == mounted_snapshot
                ):
                    _MOUNTED.pop(slug, None)
                    _MOUNTED_SESSION_KEYS.pop(slug, None)
                    if _keep_left_sessions(slug, session_key):
                        failed.append(slug)  # a session the chat replaced is not closed
                        continue
                else:
                    logger.warning(
                        "bot chat app tools: %s (app %s) cold-started while stop was in flight; "
                        "old session stopped, new session may still be running",
                        slug,
                        app_name,
                    )
                    failed.append(slug)
                    continue
                stopped.append(slug)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "bot chat app tools: could not stop Bot chat for %s (app %s)",
                    slug,
                    app_name,
                    exc_info=True,
                )
                failed.append(slug)
        # Discard sessions still in the pending-delivery window.
        if sessions is not None:
            for sk in pending_session_keys:
                try:
                    confirmed, in_flight = await discard_with_ending_fence(sessions, sk, None)
                    if confirmed and not in_flight:
                        _PENDING_DELIVERY.pop(sk, None)
                    else:
                        logger.warning(
                            "bot chat app tools: pending %s (app %s) not confirmed stopped "
                            "(start in flight: %s)",
                            sk,
                            app_name,
                            in_flight,
                        )
                        # Pending session could not be confirmed stopped.  Slug is not
                        # known yet (record_mounted has not run), so use the session key
                        # as a failure token so callers receive a non-empty failed list.
                        if sk not in failed:
                            failed.append(sk)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "bot chat app tools: could not stop pending Bot chat %s (app %s)",
                        sk,
                        app_name,
                        exc_info=True,
                    )
                    if sk not in failed:
                        failed.append(sk)
        # Any (slug, app_name) pair that still has unconfirmed handles in
        # _PENDING_CLOSE after the drain above has NOT been confirmed stopped.
        # Include those slugs in failed so callers (e.g. trust revoke) know to
        # treat the stop as incomplete and retry or surface the failure upstream.
    finally:
        # Lower the revocation fence on every exit path.  The clear defers to an
        # open app_stop_fence span and to unconfirmed pending-close handles.
        _revocation_fence_clear(app_name)
    return stopped, failed
