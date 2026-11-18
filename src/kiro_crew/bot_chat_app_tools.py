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
import copy
import json
import logging
import threading
from collections.abc import Callable, Iterable
from typing import Any

logger = logging.getLogger(__name__)

#: ``dashboard.feature_previews.<this>``.
PREVIEW = "bot_chat_app_tools"
#: Characters that make a ``disabledTools`` pattern a glob, which Claude deny rules
#: cannot express (``claude_mcp_tool_name`` sanitises them, producing a wrong name).
_GLOB_CHARS: frozenset[str] = frozenset("*?{[")


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


def _rendered_entry(app: str, agent: str, server: str) -> dict[str, Any] | None:
    """The entry a Bot chat mounts for *server*, from the app agent's RENDERED spec.

    ``None`` when the server is switched off whole, in that spec or on the MCP page,
    under its OWN name: the mount renames it ``<app>:<server>``, which no switch-off
    names. Rebuilt from launch keys alone (``template_mcp_additions.ENTRY_KEYS``), so
    no trust key such as ``autoApprove`` reaches the Bot's chat, then given the tools
    switched off for that server, in the spec or on the page, as ``disabledTools``.
    """
    from kiro_crew.agent_sdk.agent_specs import (
        global_mcp_servers,
        switched_off_tools,
        unusable_disabled_tools,
    )
    from kiro_crew.apps.bridges import _kiro_agents_dir, _namespace, _safe_link_name
    from kiro_crew.mate_worker import globally_disabled_refs
    from kiro_crew.mcp_cleanup import mcp_entry_is_muted, mcp_server_muted_in_any_store
    from kiro_crew.mcp_utils import mcp_server_alias
    from kiro_crew.template_mcp_additions import ENTRY_KEYS

    path = _kiro_agents_dir() / f"{_safe_link_name(_namespace(app, agent))}.json"
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    servers = spec.get("mcpServers") if isinstance(spec, dict) else None
    entry = servers.get(server) if isinstance(servers, dict) else None
    if not isinstance(entry, dict) or mcp_server_muted_in_any_store(server, entry):
        return None
    canonical = mcp_server_alias(server)
    if any(
        mcp_server_alias(str(name)) == canonical and mcp_entry_is_muted(row)
        for name, row in global_mcp_servers().items()
    ):
        return None
    if unusable_disabled_tools({"mcpServers": {server: entry}}):
        # A malformed restriction would be dropped by the rebuild below and the
        # server mount whole: refuse it, as KAS refuses such a spec.
        return None
    out = {key: copy.deepcopy(entry[key]) for key in ENTRY_KEYS if key in entry}
    # By the source name, canonically (the page row and the toggle's stub use the
    # slash-free alias): the spec's and Kiro-global switch-offs, then the MCP page's,
    # whichever Crew store it saved them to.
    off = {tool for name, tool in switched_off_tools(spec) if mcp_server_alias(name) == canonical}
    for ref in globally_disabled_refs({server, canonical}):
        name, _, tool = ref[1:].partition("/")
        if tool and mcp_server_alias(name) == canonical:
            off.add(tool)
    if off:
        if any(_GLOB_CHARS & set(tool) for tool in off):
            # A glob-pattern restriction cannot be translated to a Claude deny rule
            # (``claude_mcp_tool_name`` replaces glob chars with ``_``, producing a
            # rule for a nonexistent tool). Withhold the offer instead, as the worker
            # path already refuses ``uncarried_tool_limits_of`` for the same reason.
            return None
        out["disabledTools"] = sorted(off)
    return out


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
#: Using a per-app counter means an unrelated app's change never stales another app's
#: entries, so a Bot chat that starts while a *different* app is ticked/unticked still
#: mounts its own app's servers correctly.
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
#: Lock that serialises the final generation check, pending-delivery registration, and
#: void re-check in :func:`check_and_filter_projection` with the generation bump and
#: void write in :func:`revoke_app_generation`.  This closes the race window between
#: "gate passed" and "pending registered": a revocation that fires in that window
#: would otherwise find neither table populated and miss the in-flight session.
#: Both functions only hold the lock for a few fast dict operations (no I/O, no
#: blocking calls), so contention is negligible.
_DELIVERY_LOCK: threading.Lock = threading.Lock()
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
#: ``set[app_name]`` apps whose revocation is in progress (from the initial
#: :func:`revoke_app_generation` call until :func:`stop_app_bot_chat_servers`
#: returns).  While an app is in this set, the delivery gate rejects ALL entries
#: for it regardless of generation—covering paths that do not go through the
#: generation check (swap_conversation, re-delivery) and ensuring no new session
#: can mount the app between the generation bump and the teardown completing.
_REVOCATION_FENCE: set[str] = set()
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
        # Clear this session from the pending-delivery table now that record_mounted
        # is running.
        _PENDING_DELIVERY.pop(session_key, None)
        voided = _VOIDED_DELIVERY.pop(session_key, None)
        if voided:
            # The delivery array was already handed to the session before this void
            # was detected.  Record the voided aliases as MOUNTED (so any subsequent
            # revoke or stop can find the session) and schedule a reconcile that will
            # immediately trigger a fresh start, tearing the running server down.
            # Do NOT strip from `handed`: that would leave _MOUNTED empty, making
            # the session invisible to stop_app_bot_chat_servers forever.
            # The harness path that calls stop_app_bot_chat_servers (via
            # revoke_app_generation → stop_app_bot_chat_servers) already discards
            # the pending session key; this branch handles the case where
            # record_mounted runs before that stop fires.
            voided_apps = frozenset(voided)
            _schedule_reconcile()
        # Also treat any app currently under a revocation fence as voided: even if
        # the delivery gate was bypassed (e.g. via swap_conversation), the app's
        # server must not remain in _MOUNTED without a pending reconcile.
        fenced_apps = frozenset(
            alias.split(":")[0]
            for alias in {a for a, _ in offered.items() if ":" in a}
            if alias.split(":")[0] in _REVOCATION_FENCE
        )
        if fenced_apps:
            voided_apps = voided_apps | fenced_apps
            _schedule_reconcile()
        # codex keeps only the first; the error is toward a fresh start on untick, never
        # toward a revoked server left running.
        _MOUNTED[slug] = {a: f for a, f in offered.items() if spelled(a) in handed}
        _MOUNTED_SESSION_KEYS[slug] = session_key
        # Aliases dropped at delivery due to a generation mismatch are recorded in
        # _REPROJECT_SLUGS by check_and_filter_projection.  Exclude them from
        # _UNMOUNTED so after_app_change's reconcile does not treat them as
        # "harness-withheld" (which would suppress a reload).  The next reconcile
        # sees them absent from _MOUNTED but absent from _UNMOUNTED too, so now != was
        # and it schedules a reload.
        gen_dropped = _REPROJECT_SLUGS.pop(slug, set())
        _UNMOUNTED[slug] = {
            **shadow,
            **{
                a: f
                for a, f in offered.items()
                if spelled(a) not in handed and a not in gen_dropped
            },
        }
        _PENDING.pop(slug, None)
        # If the sidecar for this session still exists (check_and_filter_projection
        # was not called, e.g. a direct delivery path), check whether any grant/tick/
        # preview change occurred (via _CHANGE_EPOCH) or any per-app revocation
        # advanced (via _APP_GENERATIONS) and trigger a reconcile if so.
        stale_snap = _PROJECTION_GENS.pop(session_key, None)
        stale_epoch = _PROJECTION_EPOCH.pop(session_key, None)
        _PROJECTION_SLUGS.pop(session_key, None)
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
            continue
        if recorded_gen != _APP_GENERATIONS.get(app_name, 0):
            _schedule_reconcile()  # a change raced this projection; re-compare
            # Record the gen-mismatch drop so record_mounted excludes this alias
            # from _UNMOUNTED.  After the reconcile, after_app_change sees the alias
            # absent from _MOUNTED and absent from _UNMOUNTED, detects the gap, and
            # schedules a reload.
            if slug is not None:
                _REPROJECT_SLUGS.setdefault(slug, set()).add(alias)
            continue  # revocation or grant change raced the projection
        # Block delivery for a specific (slug, app) pair whose previous KAS close was
        # not confirmed.  Using the slug key means only THIS Bot's session for this app
        # is blocked; other Bots' sessions for the same app can mount normally.
        if slug is not None and (slug, app_name) in _PENDING_CLOSE_APPS:
            _schedule_reconcile()
            continue
        # Re-check execution admission using this session's own Bot slug.
        if slug is not None and not _app_reaches(app_name, slug):
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
                    if slug is not None:
                        _REPROJECT_SLUGS.setdefault(slug, set()).add(alias)
            # Pop any void written by a revocation that ran after us.
            voided = _VOIDED_DELIVERY.pop(session_key, None)
            if voided:
                for alias in list(out):
                    if ":" in alias and alias.split(":")[0] in voided:
                        out.pop(alias)
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
    for alias, entry in servers.items():
        if alias in shadowed:
            continue
        app_name = alias.split(":")[0] if ":" in alias else None
        if app_name:
            proj_gens[alias] = gen_snapshot_map.get(app_name, 0)
        mounted[alias] = stamp_session_label(entry, label)
        if "*" not in tools and f"@{alias}" not in tools:
            tools.append(f"@{alias}")
    if proj_gens:
        _PROJECTION_GENS[session_key] = proj_gens
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
    from kiro_crew.dashboard.crewmate_worker_reload import GONE, reload_if_idle

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
            out[slug] = await reload_if_idle(state, slot, notice=_RELOAD_NOTICE)
            if out[slug] != GONE:
                _tell_bot(slot, _RELOAD_NOTICE)
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
    for key in list(_PENDING_CLOSE.keys()):
        slug, app_name = key
        handles = _PENDING_CLOSE.get(key)
        if not handles:
            _PENDING_CLOSE.pop(key, None)
            _PENDING_CLOSE_APPS.discard(key)
            _schedule_reconcile()
            continue
        for handle in list(handles):
            pc_runtime, pc_sid = handle
            try:
                confirmed = await pc_runtime.evict_session(pc_sid)
            except Exception:  # noqa: BLE001
                confirmed = False
                logger.warning(
                    "bot chat app tools: drain: retry close for %s/%s session %s failed",
                    slug,
                    app_name,
                    pc_sid,
                    exc_info=True,
                )
            if confirmed:
                # Per-entry remove: handles added during this await are unaffected.
                try:
                    _PENDING_CLOSE[key].remove(handle)
                except (KeyError, ValueError):
                    pass  # already removed by a concurrent call
            else:
                logger.warning(
                    "bot chat app tools: drain: retry close for %s/%s session %s still unconfirmed",
                    slug,
                    app_name,
                    pc_sid,
                )
        # Unblock only when the list is now genuinely empty.
        remaining = _PENDING_CLOSE.get(key)
        if remaining is not None and len(remaining) == 0:
            _PENDING_CLOSE.pop(key, None)
            _PENDING_CLOSE_APPS.discard(key)
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
        withheld = _UNMOUNTED.get(slug, {})
        now = {a: f for a, f in now.items() if withheld.get(a) != f}
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
    for slug, mounted in list(_MOUNTED.items()):
        for alias in mounted:
            if ":" in alias:
                app_name = alias.split(":")[0]
                app_slugs.setdefault(app_name, []).append(slug)
    for _sk, pending_apps in list(_PENDING_DELIVERY.items()):
        for app_name in pending_apps:
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


def _revocation_fence_clear(app_name: str) -> None:
    """Lower the revocation fence for *app_name* when safe to do so.

    Safe means no pending-close handles remain for the app (across all slugs):
    if any ``(slug, app_name)`` key still has unconfirmed KAS close handles, the
    fence must stay up so the delivery gate keeps blocking new mounts until all
    old sessions are confirmed stopped.  Call :func:`_drain_pending_close` first
    to give outstanding handles a chance to be confirmed.
    """
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

    The revocation fence raised by :func:`revoke_app_generation` is lowered before
    this function returns, on **every** exit path (early return, normal return,
    unhandled exception), provided no pending-close handles remain for the app.
    If handles remain (a KAS close is still unconfirmed), the fence stays up and
    failures are reported so callers can retry.

    Returns ``(stopped, failed)``: the Bot slugs whose sessions were discarded, and
    those whose discard call raised. Never raises; failures are returned so callers
    can decide whether to 409.
    """
    prefix = f"{app_name}:"
    # Drain any pending-close handles first, independent of _MOUNTED, so handles
    # from earlier cold starts are confirmed before we scan for new affected slugs.
    await _drain_pending_close()
    pending_session_keys = [sk for sk, apps in list(_PENDING_DELIVERY.items()) if app_name in apps]
    affected = [
        slug
        for slug, mounted in _MOUNTED.items()
        if any(alias.startswith(prefix) for alias in mounted)
    ]
    if not affected and not pending_session_keys:
        # No sessions to stop: lower the fence immediately.
        _revocation_fence_clear(app_name)
        return [], []

    stopped: list[str] = []
    failed: list[str] = []
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
                # If previous stop attempts left unconfirmed KAS close handles for
                # this (slug, app_name) pair, _drain_pending_close (called above)
                # already retried them.  If any remain, report failure.
                if _PENDING_CLOSE.get((slug, app_name)):
                    logger.warning(
                        "bot chat app tools: prior unconfirmed close for %s/%s still pending; "
                        "reporting failure so caller can retry",
                        slug,
                        app_name,
                    )
                    failed.append(slug)
                    continue
                # Snapshot before any await: a cold start between awaits can
                # overwrite _MOUNTED[slug].  We stop exactly the session we
                # identified, and only clear _MOUNTED if we stopped it.
                session_key_snapshot = session_key
                mounted_snapshot = dict(_MOUNTED.get(slug, {}))
                # Void the allocation reservation unconditionally before discarding.
                try:
                    alloc = sessions._allocation_boundary()
                    alloc.begin_ending(session_key)
                    alloc.end_ending(session_key)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "bot chat app tools: could not void allocation reservation for %s (app %s)",
                        slug,
                        app_name,
                        exc_info=True,
                    )
                unconfirmed_out: list = []
                confirmed = await sessions.discard_conversation(
                    session_key,
                    replay=False,
                    confirm_teardown=True,
                    unconfirmed_handle_out=unconfirmed_out,
                )
                if not confirmed:
                    logger.warning(
                        "bot chat app tools: discard_conversation unconfirmed for %s (app %s); "
                        "KAS session/close timed out or was refused",
                        slug,
                        app_name,
                    )
                    if unconfirmed_out:
                        pc_runtime, pc_sid = unconfirmed_out[0]
                        _PENDING_CLOSE.setdefault((slug, app_name), []).append((pc_runtime, pc_sid))
                        _PENDING_CLOSE_APPS.add((slug, app_name))
                    failed.append(slug)
                    continue
                # Only remove from _MOUNTED when confirmed stopped AND snapshot matches.
                if (
                    _MOUNTED_SESSION_KEYS.get(slug) == session_key_snapshot
                    and dict(_MOUNTED.get(slug, {})) == mounted_snapshot
                ):
                    _MOUNTED.pop(slug, None)
                    _MOUNTED_SESSION_KEYS.pop(slug, None)
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
                    try:
                        alloc = sessions._allocation_boundary()
                        alloc.begin_ending(sk)
                        alloc.end_ending(sk)
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "bot chat app tools: could not void allocation reservation for pending %s (app %s)",
                            sk,
                            app_name,
                            exc_info=True,
                        )
                    confirmed = await sessions.discard_conversation(
                        sk, replay=False, confirm_teardown=True
                    )
                    if confirmed:
                        _PENDING_DELIVERY.pop(sk, None)
                    else:
                        logger.warning(
                            "bot chat app tools: discard_conversation unconfirmed for pending %s (app %s); "
                            "KAS session/close timed out or was refused",
                            sk,
                            app_name,
                        )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "bot chat app tools: could not stop pending Bot chat %s (app %s)",
                        sk,
                        app_name,
                        exc_info=True,
                    )
    finally:
        # Lower the revocation fence on every exit path: early return, normal
        # return, and unhandled exception.  _revocation_fence_clear only discards
        # when no pending-close handles remain; if some are still unconfirmed the
        # fence stays up until the next retry confirms them.
        _revocation_fence_clear(app_name)
    return stopped, failed
