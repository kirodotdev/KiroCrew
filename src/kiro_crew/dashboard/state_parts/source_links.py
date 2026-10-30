"""Source-link chips: derivation, budgets, dismissal and the public-repo status gate.

The module half is the budgets, the public-status grant/deny audit and the
projection a chip is served through; the slot half derives a slot's links from
its transcript, filters its dismissed identities and caches the result.

Instance fields of ``_ChatSlot`` this module reads and writes:

Writes: ``_dismissed_hydrated``, ``_dismissed_source_links``, ``_dismissed_txn_depth``,
``_dismissed_txn_pending``, ``_projection``, ``_source_links_cache``,
``_source_links_revision``.

Instance fields of ``DashboardState`` this module reads and writes:

Writes: ``_source_active_at``.
Reads only: ``_owner_ws_clients``, ``_slots``.

A function whose first parameter is ``self`` is a ``_ChatSlot`` method: the
class binds it under the same name (``state_parts/chat_slot.py``), so it reads
and writes the slot's own fields and calls its siblings through ``self``.

A function whose first parameter is ``self`` is a ``DashboardState`` method: the
class binds it under the same name (``state_parts/dashboard_state.py``), so it reads
and writes the state's own fields and calls its siblings through ``self``.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from kiro_crew.dashboard.slot_projection import SlotProjection
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot
    from kiro_crew.dashboard.state_parts.dashboard_state import DashboardState

logger = logging.getLogger("kiro_crew.dashboard.state")


_MAX_SOURCE_LINKS_PER_SLOT = 64


# How many source links each slot payload actually serializes, ACROSS kinds:
# the sidebar renders at most this many chips per session card, pull requests
# and issues together. Shared with the periodic check-status refresh so the
# driver and the serializer cannot drift.
_SERIALIZED_SOURCE_LINKS_PER_SLOT = 3


# How long a slot keeps its PR/MR chips on the fast refresh tier after a turn
# ended on it or the owner opened it (``DashboardState.note_source_activity``).
# Past it, and for every tab restored at boot until one of those happens, the
# periodic driver hands the slot's chips to the slower idle tier
# (``chip_refresh.schedule_idle_check_refresh``).
_SOURCE_ACTIVE_WINDOW_SECS = 30 * 60


# Hard ceiling on a slot's persisted dismissed-source-link set. Additions are
# gated on an identity being one of the transcript's DISTINCT derived links, so
# the set is bounded by real transcript content -- but a very long transcript
# mentioning many distinct links keeps that implicit bound loose. This names an
# explicit cap enforced at the add site (``dismiss_source_link``), the union
# write in the unlink handler, and the restore site
# (``_restore_dismissed_source_links``), so the retained set and its serialized
# metadata cannot grow past a stated bound. 512 sits well above the 64 links a
# slot ever RENDERS, so an ordinary session never approaches it.
_MAX_DISMISSED_SOURCE_LINKS = 512


def _budgeted_source_links(links: list[dict]) -> list[dict]:
    """Apply the sidebar chip budget: ``_SERIALIZED_SOURCE_LINKS_PER_SLOT`` chips
    in total, pull requests first.

    One shared allowance caps what a session card shows at three chips,
    however many links the session mentions; the rest sit behind the "+N"
    overflow chip. The slice is taken AFTER grouping changes
    ahead of issues, so an issue can only ever occupy a slot no pull request
    wanted: a session with three pull requests and a dozen issues shows three
    PR chips and folds every issue into "+N". That ordering is what keeps the
    check-status refresh honest too -- it reads this same slice, so every PR
    chip the card renders is also the one whose CI status gets scheduled, and a
    mentioned issue can never push a PR out of either.
    """
    changes, issues = _source_links_by_kind(links)
    return (changes + issues)[:_SERIALIZED_SOURCE_LINKS_PER_SLOT]


def _source_links_by_kind(links: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split links into (changes, issues), preserving discovery order in each.

    ``kind`` is absent on older payloads and means ``"change"`` there, so the
    default keeps a pre-``kind`` link rendering as the pull request it always
    was.
    """
    changes = [link for link in links if link.get("kind", "change") == "change"]
    issues = [link for link in links if link.get("kind", "change") == "issue"]
    return changes, issues


# Deduplicated SEL audit of the non-owner public-repo status grant. This runs on
# the per-link serialization path (every push broadcast), so an un-deduplicated
# write would be unbounded — collapse to one event per URL per window, mirroring
# ws_event_scope._audit_decision. AUTOSDE (backend-security-controls) requires a
# SEL event for every permission decision, grants included.
_PUBLIC_STATUS_GRANT_AUDIT: dict[str, float] = {}


_PUBLIC_STATUS_DENY_AUDIT: dict[str, float] = {}


_PUBLIC_STATUS_GRANT_WINDOW_SECS = 300.0


def _drop_expired_public_status_audits(audit: dict[str, float], now: float) -> None:
    # An entry past its window is never read again, so each insert drops those;
    # the map then holds only the links audited inside the current window.
    cutoff = now - _PUBLIC_STATUS_GRANT_WINDOW_SECS
    for stale in [url for url, at in list(audit.items()) if at <= cutoff]:
        audit.pop(stale, None)


def _audit_public_status_grant(url: str) -> None:
    now = time.monotonic()
    last = _PUBLIC_STATUS_GRANT_AUDIT.get(url)
    if last is not None and (now - last) < _PUBLIC_STATUS_GRANT_WINDOW_SECS:
        return
    _drop_expired_public_status_audits(_PUBLIC_STATUS_GRANT_AUDIT, now)
    _PUBLIC_STATUS_GRANT_AUDIT[url] = now
    try:
        sel().log_api_access(
            caller="dashboard-user",
            operation="source_link_public_status",
            outcome="allowed",
            source="source_links",
            resources=url,
        )
    except Exception:
        logger.debug("SEL audit for public-status grant failed", exc_info=True)


def _audit_public_status_denied(url: str) -> None:
    # Symmetric to the grant audit: AUTOSDE backend-security-controls requires a
    # SEL event for every permission DECISION, denials included. A non-owner
    # dashboard user requested status on a repo that is NOT confirmed public
    # (private / unknown / stale) and was denied — record it, deduplicated per
    # URL per window (same hot per-link broadcast path as the grant).
    now = time.monotonic()
    last = _PUBLIC_STATUS_DENY_AUDIT.get(url)
    if last is not None and (now - last) < _PUBLIC_STATUS_GRANT_WINDOW_SECS:
        return
    _drop_expired_public_status_audits(_PUBLIC_STATUS_DENY_AUDIT, now)
    _PUBLIC_STATUS_DENY_AUDIT[url] = now
    try:
        sel().log_api_access(
            caller="dashboard-user",
            operation="source_link_public_status",
            outcome="denied",
            source="source_links",
            resources=url,
        )
    except Exception:
        logger.debug("SEL audit for public-status denial failed", exc_info=True)


def _project_source_links(
    links: list[dict],
    include_check_status: bool,
    *,
    dashboard_user: bool = False,
) -> list[dict]:
    """Attach cached chip status to each link, gated on kind and on the caller.

    The chip-status cache is pull-request-only: it holds a {ci, state}
    projection of a PR/MR lifecycle. Consulting it for an issue would key on a
    URL it never stores -- and if a PR and an issue ever normalized to the same
    key, the issue chip would inherit the PR's CI glyph. Gate on kind.

    ``include_check_status`` is the OWNER gate: the owner sees status for every
    link, public or private. ``dashboard_user`` is the weaker, PUBLIC-ONLY gate:
    a non-owner but authenticated dashboard user sees status only for a link
    whose repository is KNOWN public, because that lifecycle state is already
    world-visible on the provider's website. Private and not-yet-known repos
    fall through to owner-only (fail closed). App tokens pass neither flag and
    keep bare chips.

    Shared by the budgeted slots payload and the unbudgeted overflow-expand
    read so the two cannot decorate the same link differently.
    """

    def _attach(link: dict) -> bool:
        if link.get("kind", "change") != "change":
            return False
        if include_check_status:
            return True
        granted = dashboard_user and _repo_is_public(link["url"]) is True
        if granted:
            # SEL-audit the AUTHORIZATION DECISION (AUTOSDE
            # backend-security-controls: every permission decision, grants
            # included). This grants a non-owner status on a repo whose public
            # visibility we positively confirmed — a real access-control
            # decision, so it must leave an allow event. Deduplicated per URL per
            # window because this runs per-link on every push broadcast; an
            # un-deduplicated write here would be unbounded on the hot path.
            _audit_public_status_grant(link["url"])
        elif dashboard_user:
            # DENY decision: a non-owner dashboard user requested status on a
            # change link but the repo is not confirmed public (private /
            # unknown / stale), so status is withheld. AUTOSDE requires the
            # denial to be audited too — deduplicated the same way.
            # include_check_status (owner) paths never reach here, and
            # app tokens set neither flag so they are not "denied dashboard-user"
            # decisions.
            _audit_public_status_denied(link["url"])
        return granted

    def _status(url: str) -> dict:
        # Project ONLY the known chip-status keys, never the raw cache dict.
        # Splatting ``**cache`` was fail-open: the cache payload already outgrew
        # its docstring once (mergeStateStatus), so the next field would ride
        # silently into every frame — including, via the general broadcast, an
        # app-token frame (ws_event_scope strips the same names on the far end,
        # but the two lists must not be the sole guarantee). Enumerating here
        # fails closed at the source: an unlisted field is simply not emitted.
        cached = _cached_check_status(url) or {}
        return {k: cached[k] for k in _CHIP_STATUS_KEYS if k in cached}

    return [{**link, **(_status(link["url"]) if _attach(link) else {})} for link in links]


# The only fields the chip-status cache is allowed to project onto a source
# link. Kept in step with ws_event_scope._SOURCE_LINK_STATUS_KEYS (the app-token
# strip) and the frontend SidebarSourceLink type; a field absent here is never
# emitted, so widening the cache cannot leak a new field into any frame.
_CHIP_STATUS_KEYS = ("ci", "ciPending", "state", "mergeable", "mergeStateStatus")


_NON_DURABLE_SOURCE_LINK_ROLES = frozenset(
    {"chunk", "done", "streaming", "queued", "permission", "elicitation"}
)


def init_source_links_fields(self: _ChatSlot) -> None:
    """Set this module's slot fields to their defaults (``_ChatSlot.__init__``)."""
    self._projection = SlotProjection()
    # (content revision, links) cache for the sidebar PR chips scan.
    self._source_links_revision = 0
    self._source_links_cache = None
    # Serialized ``SourceRef.identity`` keys the user has explicitly unlinked
    # from this session. The derivation in ``SlotProjection.source_links``
    # filters against this, so a dismissed change stays gone across the
    # transcript re-scan that every revision bump triggers. Persisted in the
    # slot's durable metadata (``dismissed_source_links``) so a gateway
    # restart does not resurrect a chip the user removed.
    self._dismissed_source_links = set()
    # False marks a slot bound to a transcript whose dismissed set could NOT
    # be read (a transient metadata-read failure at bind). The binding is
    # kept for routing/continuity, but the slot's full save must then CARRY
    # FORWARD the on-disk dismissed line rather than serialize its (empty)
    # in-memory set, or it would erase the transcript's real tombstones. A
    # readable restore (``_restore_dismissed_source_links``) sets it True.
    self._dismissed_hydrated = True
    # >0 while one OR MORE unlink transactions hold an uncommitted, tentative
    # dismissal in ``_dismissed_source_links`` (between the in-memory mutate
    # and the guarded persist/rollback). It is a DEPTH COUNTER, not a bool,
    # because concurrent unlink requests can touch the SAME slot object under
    # DIFFERENT ``_source_link_txn_lock`` keys (a dirty slot rebound onto
    # another transcript mid-flight): each transaction increments on entry and
    # decrements on its own exit, so the slot stays in-flight while ANY
    # transaction still holds it and one request's rollback can never clear
    # another's guard. A periodic full-save flush that fires while this is >0
    # carries the on-disk dismissed line forward instead of serializing the
    # tentative set — the guarded write may still fail and roll it back.
    self._dismissed_txn_depth = 0
    # The subset of ``_dismissed_source_links`` this slot added under an
    # in-flight (not-yet-committed) unlink transaction. The source-link
    # projection subtracts these so a CONCURRENT ``push_slots_update`` fired
    # during the guarded metadata write does not publish a tentative
    # dismissal (persist-before-publish): a chip stays visible to clients
    # until the write that removes it has durably committed, and a failed
    # write that rolls the tentative dismissal back never leaves a client
    # showing a chip disk still records. Cleared for the key when the
    # transaction commits (durable) or rolls back (removed from the set).
    self._dismissed_txn_pending = set()


def invalidate_source_links(self: _ChatSlot) -> None:
    """Mark cached sidebar PR/MR/issue links stale after message-content mutation."""
    self._source_links_revision += 1


def dismiss_source_link(self: _ChatSlot, identity_key: str) -> bool:
    """Suppress one source-link identity from this session's derived chips.

    Records the serialized identity into the per-slot dismissed set and
    invalidates the cache so the next derivation re-scans without it. The
    transcript is never touched -- the link is DERIVED, so removing it would
    be undone by the next re-scan; suppression is the only stable removal.
    No remote provider mutation happens: this hides a chip, it does not close
    a pull request. Returns ``True`` when the key was newly added, ``False``
    when it was already dismissed (an idempotent repeat).
    """
    if identity_key in self._dismissed_source_links:
        return False
    if len(self._dismissed_source_links) >= _MAX_DISMISSED_SOURCE_LINKS:
        # At the named ceiling: refuse to grow the retained set (and the
        # metadata it serializes) further. Additions are already gated on
        # real transcript links, so reaching this bound is pathological; drop
        # the add rather than let the set grow unbounded.
        return False
    self._dismissed_source_links.add(identity_key)
    self.invalidate_source_links()
    return True


def mentions_source_identity(self: _ChatSlot, identity_key: str) -> bool:
    """Does this transcript RAW-mention the given source-link identity?

    Unlike ``_pr_source_links`` (which filters the dismissed set out), this
    asks only whether the transcript's own rows actually reference the
    identity — so it is independent of the per-slot dismissed set, which a
    concurrent unlink on a since-rebound slot can populate TENTATIVELY with a
    foreign key. The unlink authorization uses it to admit a non-derived
    identity only when the PINNED transcript genuinely carries it, closing
    the path where a tentative foreign key would authorize a durable tombstone
    on a transcript that never mentioned the link. Reuses the same parser and
    identity keying as the derivation so the two cannot drift.
    """
    from kiro_crew.dashboard.handlers.source_providers import (
        parse_source_url,
        source_link_path_markers,
    )
    from kiro_crew.dashboard.source_providers.contract import source_ref_identity_key
    from kiro_crew.dashboard.source_providers.links import iter_source_url_candidates

    path_markers = source_link_path_markers()
    # No parse budget and no early stop here (unlike the bounded derivation):
    # this predicate authorizes a durable tombstone, so it must scan the WHOLE
    # transcript -- a budget that gave up early could report a genuinely
    # mentioned identity as absent and misauthorize the write.
    for msg in self.messages:
        if not isinstance(msg, dict) or msg.get("role") in _NON_DURABLE_SOURCE_LINK_ROLES:
            continue
        content = msg.get("content")
        if not isinstance(content, str) or "https://" not in content:
            continue
        for candidate in iter_source_url_candidates(content, path_markers):
            try:
                ref = parse_source_url(candidate)
            except ValueError:
                continue
            if source_ref_identity_key(ref.identity) == identity_key:
                return True
    return False


def _pr_source_links(self: _ChatSlot) -> list[dict]:
    """Return cached source links ordered by their most recent mention."""
    return self._projection.source_links(
        self,
        max_links=_MAX_SOURCE_LINKS_PER_SLOT,
        non_durable_roles=_NON_DURABLE_SOURCE_LINK_ROLES,
    )


def source_links_payload(
    self: _ChatSlot, *, include_check_status: bool = False, dashboard_user: bool = False
) -> dict:
    """Every source link this slot carries — the unbudgeted read.

    ``to_dict`` serializes at most ``_SERIALIZED_SOURCE_LINKS_PER_SLOT``
    links in total, so the sidebar's "+N" overflow chip has nothing on the
    client to expand into. This is what that expand fetches.

    Ordering repeats the budgeted slice's grouping (changes, then issues) so
    the chips already on screen keep their positions and the revealed ones
    append inside their own group instead of shuffling the row.
    """
    links = self._pr_source_links()
    changes, issues = _source_links_by_kind(links)
    return {
        "links": _project_source_links(
            changes + issues, include_check_status, dashboard_user=dashboard_user
        ),
        "total": len(links),
    }


def _summary_source_links(self: _ChatSlot) -> list[dict]:
    # Skip extraction itself when the chips are off, not just the two fields
    # the projection derives from it: `_pr_source_links()` scans the transcript
    # under a per-call parse budget, and paying for a payload nothing renders
    # is the cost this switch exists to remove. The getter is a cache-only
    # snapshot lookup -- it never touches config, which is what makes it safe
    # to call from this synchronous per-slot path on the event loop.
    from kiro_crew.dashboard.handlers.source_providers import (
        session_card_source_links_enabled,
    )

    return self._pr_source_links() if session_card_source_links_enabled() else []


def source_links_view(
    self: _ChatSlot, *, include_check_status: bool = False, dashboard_user: bool = False
) -> list[dict]:
    """The ``source_links`` field ``to_dict`` emits for one audience.

    ``include_check_status`` and ``dashboard_user`` change NOTHING in the
    slot summary except this field (``slot_projection.SlotProjection.to_dict``
    reads them only through ``project_source_links``). The broadcast uses
    that: it serializes each slot once and swaps this field per audience
    instead of re-running the projection body (last-message markdown strip,
    credential redaction, options parse) three times per slot on the event
    loop. The source-link transcript scan itself is memoized per slot
    revision, so it was never the repeated cost.
    """
    return _project_source_links(
        _budgeted_source_links(self._summary_source_links()),
        include_check_status,
        dashboard_user=dashboard_user,
    )


def source_link_urls(self: DashboardState) -> list[str]:
    """URLs of the sidebar-visible PR/MR chips across all slots.

    Only the links each slot actually serializes (the first
    ``_SERIALIZED_SOURCE_LINKS_PER_SLOT``) are returned — these are the
    chips whose check status the periodic owner-WS refresh keeps fresh.
    Reads the per-slot revision cache, so this is cheap to call on a timer.

    Issue links are excluded: the check-status path reaches ``gh pr view``
    and has no meaning for an issue.

    Returns nothing while the chips are switched off. This is the point of
    gating here rather than only in the payload: the periodic refresh this
    feeds spawns a credentialed provider subprocess per round, and a user who
    turned the chips off should stop paying for status nobody renders.
    """
    from kiro_crew.dashboard.handlers.source_providers import (
        session_card_source_links_enabled,
    )

    if not session_card_source_links_enabled():
        return []
    urls: list[str] = []
    for s in self._slots.values():
        urls.extend(self._slot_chip_urls(s))
    return urls


def _slot_chip_urls(slot: _ChatSlot) -> list[str]:
    """The PR/MR chip URLs one slot serializes (sidebar budget, no issues)."""
    return [
        link["url"]
        for link in _budgeted_source_links(slot._pr_source_links())
        if link.get("kind", "change") == "change"
    ]


def note_source_activity(self: DashboardState, key: str) -> None:
    """Keep this slot's chips on the fast refresh tier for a while.

    Called when a turn ends on the slot and when the owner opens it. Spends
    no credentials itself: it only decides which tier the owner's own
    periodic driver puts the slot's chips on.
    """
    now = time.monotonic()
    stamps = self._prune_source_activity(now)
    if key in self._slots:
        stamps[key] = now


def _prune_source_activity(self: DashboardState, now: float) -> dict[str, float]:
    """The activity stamps, minus closed slots and lapsed windows.

    Run on every write as well as every read, so the map never holds more
    than one entry per live slot however its readers are scheduled.
    """
    stamps = getattr(self, "_source_active_at", None)
    if stamps is None:
        stamps = self._source_active_at = {}
    for key in [
        key
        for key, at in stamps.items()
        if key not in self._slots or now - at >= _SOURCE_ACTIVE_WINDOW_SECS
    ]:
        del stamps[key]
    return stamps


def idle_source_link_urls(self: DashboardState) -> set[str]:
    """Chip URLs that only slots nobody is using link to.

    A slot is in use while a turn runs on it, and for
    ``_SOURCE_ACTIVE_WINDOW_SECS`` after :meth:`note_source_activity`. Every
    other slot is idle -- in particular every tab restored from
    ``open_slots.json`` at boot, until it is opened or runs a turn. The
    periodic driver re-reads these on the slower idle tier, so a long
    sidebar history stops costing a provider burst every chip TTL. A URL
    that any in-use slot also links stays on the fast tier.
    """
    stamps = self._prune_source_activity(time.monotonic())
    active: set[str] = set()
    idle: set[str] = set()
    for key, slot in self._slots.items():
        in_use = key in stamps or bool(getattr(slot, "turn_running", False))
        (active if in_use else idle).update(self._slot_chip_urls(slot))
    return idle - active


def source_link_urls_for_slot(self: DashboardState, key: str) -> list[str]:
    """Sidebar-visible PR/MR chip URLs for one slot (same cap and kind filter).

    Empty while the chips are switched off, for the same reason
    :meth:`source_link_urls` is: this feeds the turn-boundary status refresh.
    """
    from kiro_crew.dashboard.handlers.source_providers import (
        session_card_source_links_enabled,
    )

    if not session_card_source_links_enabled():
        return []
    slot = self._slots.get(key)
    if slot is None:
        return []
    return self._slot_chip_urls(slot)


def push_source_status(self: DashboardState, delta: dict) -> None:
    """Push a single PR/MR status delta to owner websockets only.

    Chip status is credential-backed provider data, so this never reaches
    non-owner or app-token clients. Fire-and-forget: the panel's own poll
    remains the safety net if a client misses the event.
    """
    if not self._owner_ws_clients:
        return
    self._send_ws_owners(json.dumps({"type": "source_status", "data": delta}))


def refresh_slot_source_status(self: DashboardState, key: str) -> None:
    """Re-read this slot's PR/MR status now — called at agent turn boundaries.

    A turn that just ran ``gh pr create``, pushed a revision, or drove a PR
    review is exactly when a PR's lifecycle moved, and nothing else in
    the system invalidates the status caches on that event: the chips would
    wait out the periodic rotation and the detail panel would not refetch at
    all. Fires ONLY when an OWNER window is open: the read runs the
    operator's credentials, so a non-owner window must not drive it (a
    non-owner renders the owner-populated caches read-only). With no owner
    window open there is nobody entitled to a credentialed read, so no
    provider subprocess is spawned. Rate-floored inside
    ``request_check_refresh_now``.
    """
    # Both the status read and the visibility revalidation below run the
    # operator's `gh`/`glab` credentials, so this turn-boundary refresh runs
    # ONLY when an OWNER window is open. A non-owner dashboard window never
    # drives it: the owner's own connection keeps the caches warm
    # and non-owner windows render the result read-only via the fail-closed
    # is_repo_public gate. With no owner window open there is nobody entitled
    # to drive a credentialed read, so this is a no-op.
    #
    # The slot just ran a turn, so it is in use whoever is watching: its
    # chips stay on the fast tier of the periodic driver for a while.
    self.note_source_activity(key)
    if not self._owner_ws_clients:
        return
    try:
        urls = self.source_link_urls_for_slot(key)
        if not urls:
            return
        from kiro_crew.dashboard.handlers.source_providers import (
            request_check_refresh_now,
            schedule_visibility_refresh,
        )

        request_check_refresh_now(urls, self.push_slots_update)
        # force=True: the status read above bypasses the TTL, so visibility
        # MUST be revalidated in lockstep — otherwise fresh (now-private)
        # status could be projected against a still-cached-public visibility
        # entry, leaking private status to a non-owner. Runs
        # AFTER the status schedule: both are fire-and-forget schedulers, and
        # ordering the status call first preserves the turn-boundary contract
        # while the downstream render gate (`_project_source_links` ->
        # is_repo_public) is what actually withholds status until visibility
        # reconfirms.
        schedule_visibility_refresh(urls, self.push_slots_update, force=True)
    except Exception:
        logger.debug("turn-boundary source status refresh failed", exc_info=True)


def _cached_check_status(url: str) -> dict | None:
    """Lazy wrapper so state.py has no import-time dep on the handler module."""
    from kiro_crew.dashboard.handlers.source_providers import get_cached_check_status

    return get_cached_check_status(url)


def _repo_is_public(url: str) -> bool | None:
    """Lazy wrapper for the repo-visibility reader (public/private/unknown).

    Function-local import (top-level-imports exception): ``source_providers``
    imports chat-state helpers from this module, so a module-scope import here
    would create a bootstrap import cycle. Same rationale as
    ``_cached_check_status`` directly above.
    """
    from kiro_crew.dashboard.handlers.source_providers import is_repo_public

    return is_repo_public(url)
