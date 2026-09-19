"""Best-effort hook helpers for the per-member append-only event log.

Every function here is additive and swallows its own failures: a logging fault
must never break the path it is hooked into. The event-log service itself
(``kiro_crew.eventlog.service``) is filled in concurrently and its bodies may
still raise ``NotImplementedError`` while callers run, which is precisely why
:func:`emit` wraps ensure+append in a blanket ``try/except``.

The service contract this codes against is synchronous:

    svc = get_service()
    svc.ensure(slug, name)
    svc.append(slug, type, data)
    svc.attach_broadcast(fn)

Imports of the service and of the members module are done lazily inside the
functions to avoid import cycles with ``dashboard.state`` and
``slack.gateway``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The config-derived fields the roster view carries and a member/config event
#: snapshots. Kept in lockstep with ``members_projections._CONFIG_FIELDS`` and
#: with the snapshot the config-save hook writes in ``handlers/agents.py`` — the
#: reconcile below compares exactly these against ``cfg.agents[name]`` so a
#: hand-edited config still lands a correcting member/config event.
_CONFIG_FIELDS = (
    "kiro_agent",
    "workspace",
    "memory_store",
    "model",
    "source",
    "starred",
    "avatar",
)


def _config_snapshot_for_agent(agent_cfg) -> dict:
    """The 7 config-derived fields as a member/config would carry them.

    ``starred`` is coerced to ``bool`` (it is a load-time-coerced flag), matching
    the snapshot ``handlers/agents.py`` writes and the ``bool(agent_cfg.starred)``
    the roster endpoint sends. ``source`` is bounded to the roster vocabulary via
    the same ``normalize_member_source`` the HTTP row uses, so a credential- or
    URL-shaped value planted in the agent-writable ``source`` cannot reach the
    browser through the durable projection either (the roster row already
    collapses it; without this the projected snapshot would ship it raw). Every
    other field is passed through as-is.
    """
    # Lazy import mirrors the other handlers.members lookups in this module and
    # keeps the config->projection path free of an import cycle.
    from kiro_crew.dashboard.handlers.members import normalize_member_source

    out: dict = {}
    for field in _CONFIG_FIELDS:
        value = getattr(agent_cfg, field, None)
        if field == "starred":
            out[field] = bool(value)
        elif field == "source":
            out[field] = normalize_member_source(value)
        else:
            out[field] = value
    return out


def reconcile_member_config(slug, name, agent_cfg, roster_view) -> "list[str] | None":
    """Append a correcting member/config when the log's roster drifts from config.

    Compares the log-derived *roster_view*'s 7 config fields against the live
    ``agent_cfg`` snapshot. When any differ — or the roster view has NO config
    field at all (no member/config has ever been appended) — appends one
    MEMBER_CONFIG carrying the full config snapshot plus a ``changed`` list, so
    the log becomes correct even when the config was edited by hand rather than
    through the dashboard (which emits its own member/config on save).

    Returns the ``changed`` field list when an event was appended, ``None`` when
    the roster already matched (no write). Best-effort: any failure is swallowed
    and reported as ``None``.
    """
    if not slug:
        return None
    try:
        snapshot = _config_snapshot_for_agent(agent_cfg)
        view = roster_view if isinstance(roster_view, dict) else {}
        # No config field present at all -> the log has never seen a
        # member/config for this member; treat every field as changed so the
        # first snapshot lands.
        never_configured = not any(f in view for f in _CONFIG_FIELDS)
        if never_configured:
            changed = list(_CONFIG_FIELDS)
        else:
            changed = [f for f in _CONFIG_FIELDS if view.get(f) != snapshot[f]]
        if not changed:
            return None
        from kiro_crew.eventlog.service import get_service
        from kiro_crew.eventlog.types import MEMBER_CONFIG

        svc = get_service()
        svc.ensure(slug, name or slug)
        svc.append(slug, MEMBER_CONFIG, {**snapshot, "changed": changed})
        return changed
    except Exception:
        logger.debug("reconcile_member_config failed for slug=%r", slug, exc_info=True)
        return None


def reconcile_members_at_startup(cfg, state, autonudge_svc) -> int:
    """Reconcile every crew member's log against live state at gateway boot.

    For each global crew member:

    * ``ensure`` its log exists;
    * config-reconcile it (see :func:`reconcile_member_config`), so a config
      edited while the gateway was down lands a correcting member/config;
    * write CLOSERS for durable facts the log still believes are open but the
      live process does not back:
        - ``wake.patrol == 'armed'`` with NO live auto-nudge loop for
          ``wake.slot_key`` -> PATROL_STOPPED {slot_key, reason: 'interrupted'};
        - each ``driving.open`` slot_key absent from ``state._slots`` ->
          SLOT_CLOSED {slot_key, reason: 'interrupted'}.

    Best-effort by contract: a failure on one member never aborts the sweep or
    boot. Returns the number of CLOSER events written (config events excluded),
    logged at info.
    """
    closers = 0
    try:
        from kiro_crew.eventlog import types
        from kiro_crew.eventlog.service import get_service
        from kiro_crew.validation import _AGENT_NAME_RE

        svc = get_service()
        agents = getattr(cfg, "agents", {}) or {}
        live_slots = getattr(state, "_slots", {}) if state is not None else {}
        for name, agent_cfg in agents.items():
            if not _AGENT_NAME_RE.match(name):
                continue
            # The PERSISTED identity, so this sweep reconciles the same log the
            # roster read and the history route address. `cfg` is already loaded
            # here, so no config read is added per member.
            slug = ledger_slug(name, cfg)
            if slug is None:
                continue
            try:
                svc.ensure(slug, name)
                snap = svc.snapshot(slug)
                values = snap.get("values", {}) if isinstance(snap, dict) else {}
                reconcile_member_config(slug, name, agent_cfg, values.get(types.PROJ_ROSTER, {}))
                # Patrol closer.
                wake = values.get(types.PROJ_WAKE, {}) or {}
                if wake.get("patrol") == "armed":
                    wake_slot = wake.get("slot_key")
                    has_loop = False
                    if autonudge_svc is not None and wake_slot:
                        try:
                            get_by_slot = getattr(autonudge_svc, "get_by_slot", None)
                            has_loop = (
                                bool(get_by_slot(wake_slot)) if callable(get_by_slot) else False
                            )
                        except Exception:
                            has_loop = False
                    if not has_loop:
                        svc.append(
                            slug,
                            types.PATROL_STOPPED,
                            {"slot_key": wake_slot, "reason": "interrupted"},
                        )
                        closers += 1
                # Slot closers.
                driving = values.get(types.PROJ_DRIVING, {}) or {}
                for slot_key in driving.get("open", []) or []:
                    if slot_key not in live_slots:
                        svc.append(
                            slug,
                            types.SLOT_CLOSED,
                            {"slot_key": slot_key, "reason": "interrupted"},
                        )
                        closers += 1
            except Exception:
                logger.debug("startup reconcile failed for slug=%r", slug, exc_info=True)
    except Exception:
        logger.debug("reconcile_members_at_startup failed", exc_info=True)
    logger.info("member event-log startup reconcile wrote %d closer event(s)", closers)
    return closers


def member_slug_for_slot(slot_key) -> "str | None":
    """Return the member slug a DM slot is keyed to, or ``None``.

    Member DM slots are keyed ``member-<slug>`` (possibly under a
    ``dashboard_`` / ``dashboard:`` prefix). Uses the members module's own
    predicate and derivation so this stays in lockstep with the slot layer.
    """
    if not isinstance(slot_key, str) or not slot_key:
        return None
    try:
        from kiro_crew import members as members_mod

        if not members_mod.is_member_session_key(slot_key):
            return None
        key = slot_key
        for prefix in ("dashboard_", "dashboard:"):
            if key.startswith(prefix):
                key = key[len(prefix) :]
                break
        prefix = members_mod.DM_SLOT_KEY_PREFIX
        if not key.startswith(prefix):
            return None
        slug = key[len(prefix) :]
        # Round-trip through validate_slug so a malformed tail reads as "not a
        # member slot" rather than an unusable slug.
        return members_mod.validate_slug(slug)
    except Exception:
        logger.debug("member_slug_for_slot failed for %r", slot_key, exc_info=True)
        return None


def member_name_for_slug(cfg, slug) -> "str | None":
    """Return the exact member NAME for *slug* under *cfg*, or ``None``.

    Reuses the members handler's name resolver (config-order, first match wins
    for a colliding slug); falls back to a direct scan of ``cfg.agents`` through
    :func:`ledger_slug` if that import is unavailable, so both paths answer for
    the same key the writers use.
    """
    if not slug:
        return None
    try:
        from kiro_crew.dashboard.handlers.members import _member_names_for_slug

        names = _member_names_for_slug(cfg, slug)
        return names[0] if names else None
    except Exception:
        logger.debug("member_name_for_slug resolver failed for %r", slug, exc_info=True)
    try:
        for name in getattr(cfg, "agents", {}) or {}:
            if ledger_slug(name, cfg) == slug:
                return name
    except Exception:
        logger.debug("member_name_for_slug fallback failed for %r", slug, exc_info=True)
    return None


def ledger_slug(name, config=None) -> "str | None":
    """The key a member's ledger and projections are addressed by, or ``None``.

    ``members.member_slug``, never ``members.slug_for_name``: slugification is
    LOSSY, so two crew names can fold to one slug, and a lossy key means one log
    and one projection block hold two members -- the roster then reconciles each
    member's config in turn, every read appends a correcting event, and both
    cards show the other's state. ``member_slug`` prefers the member's PERSISTED
    ``member_id``, which is what the roster read and the history route already
    key on, so a writer using the lossy form would also be writing where those
    readers never look.

    Loads the config when none is passed, so call it OFF the event loop (or hand
    it a config the caller already holds). ``None`` on any failure, which every
    caller treats as "do not write".
    """
    if not name:
        return None
    try:
        from kiro_crew import members as members_mod

        return members_mod.member_slug(name, config)
    except Exception:
        logger.debug("ledger_slug failed for %r", name, exc_info=True)
        return None


def emit_for_member(name, type, data, config=None) -> None:
    """:func:`emit` keyed by the member's persisted identity rather than a slug.

    The write-side counterpart of the roster read: a caller that has a NAME must
    not derive the key itself, or the two halves drift the way they did when the
    writers used the lossy slug and the readers used ``member_id``.
    """
    slug = ledger_slug(name, config)
    if slug is None:
        return
    emit(slug, name, type, data)


def emit(slug, name, type, data) -> None:
    """Ensure a member's log exists and append one event, swallowing errors.

    Best-effort by contract: any failure (including a service body that still
    raises ``NotImplementedError``) is logged at debug and never propagates.
    """
    if not slug:
        return
    try:
        from kiro_crew.eventlog.service import get_service

        svc = get_service()
        svc.ensure(slug, name or slug)
        svc.append(slug, type, data)
    except Exception:
        logger.debug("eventlog emit failed for slug=%r type=%r", slug, type, exc_info=True)
