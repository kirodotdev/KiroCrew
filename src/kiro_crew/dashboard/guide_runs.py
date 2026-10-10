"""Gateway-owned guide runs: the state machine behind the ``kirocrew-guide`` tools.

A guide is an offer, made by an agent, to walk the human through an ordered list
of registered actions (:mod:`kiro_crew.guide_catalog`). The gateway owns every
piece of state; the MCP shim holds none, and a browser reload reads it back from
``GET /api/guide/pending``.

Lifecycle::

    offered --claim--> active --progress/commit--> ... --> completed
       ^                 |  \\--target missing--> target_missing --target found--> active
       |                 |
       +--lease lapses---+        any non-terminal --cancel--> cancelled
                                  any non-terminal --TTL-----> expired

* ``completed``, ``cancelled`` and ``expired`` are terminal and immutable.
* One live guide per conversation: a new offer from the agent cancels the
  unfinished one (reason ``superseded``) instead of being refused, so the agent
  never has to check or cancel before offering; only a guide whose save is in
  flight refuses the new offer (``guide_saving``).
* ``target_missing`` is recoverable: once the owning tab sees the SAME step's
  target again (the human came back to its page), it reports ``target_found``
  and the guide returns to ``active`` on that step, without advancing. When the
  page came back at an EARLIER step of the same action instead (a form that
  remounted starts over), the report names that ``resume_step_index`` and the
  guide moves back to it; never forward, and never while a save is pending. A
  ``target_found`` on an already active guide changes nothing; on a terminal
  one it is refused like any other write.
* Every mutation bumps ``revision``; a browser update names the revision it read,
  so a stale or foreign tab's write is refused rather than applied.
* A ``ui.show`` action (plan version 2) has one step list per placement. The
  claim names the placement the tab walks (its viewport's), and the gateway
  records that placement's step ids from the record's own plan, under the
  claim's revision bump; a progress report must name the current step's
  recorded id (and a resume, the earlier step's), so no tab can step through
  another placement's list. A takeover may re-pick only an action the guide
  has not started. Mid-guide, the owning tab may re-plan the current action to
  its new viewport's placement (:meth:`GuideStore.replan`), revision-checked
  and only at a step boundary both placements share.
* A ``ui.find`` action's reports may carry ``find``, what the tab's search
  found (:func:`guide_catalog.clean_find_report`: a result, a count, a role,
  a registered id; never page text), kept on the action record as ``find``.
  A search that found nothing is ``target_missing`` with reason ``not_found``;
  several controls with that name, ``ambiguous_target``.
* A ``ui.show`` ``gate`` / ``select`` step that cannot go on is
  ``target_missing`` with reason ``gate_off`` / ``needs_selection`` and a
  ``blocker`` naming only plan ids (the gate and its setting id, or the
  selection scope), never which entity anyone picked.
* One tab owns an active guide, on a lease renewed by heartbeat. A lapsed lease
  returns the guide to ``offered``; another tab takes it over only by an explicit
  claim (``take_over`` is an explicit human action in the UI).
* A ``ui`` step advances on the owning tab's report. A ``commit`` step advances
  ONLY through :meth:`GuideStore.begin_commit` / :meth:`GuideStore.finish_commit`,
  called by the real owner-only mutation route after IT succeeded, with the
  identity that route returned. A cancel or expiry between the two retires the
  association, so a save finishing late cannot revive a guide.

All methods are synchronous and called on the event loop, so no mutation can
interleave with another. Nothing here is persisted: a gateway restart drops every
guide, which is the conservative failure for a UI hint.

A guide that ended is kept for a while: the
newest one per slot is served by ``pending`` for ``TERMINAL_SHOWN_SECONDS`` (so a
page reload still shows its result line in that chat) until the owner dismisses
it, and it is pruned after ``TERMINAL_RETAIN_SECONDS``. ``MAX_STORED_GUIDES``
bounds the whole store either way.
"""

from __future__ import annotations

import copy
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

from kiro_crew import guide_catalog as catalog

GUIDE_TTL_SECONDS = 30 * 60
TAB_LEASE_SECONDS = 45
#: How long a terminal guide is kept at all (``guide_status`` reads it).
TERMINAL_RETAIN_SECONDS = 7 * 24 * 60 * 60
#: How long ``pending`` still serves a slot's newest ended guide, for its chat's
#: result line.
TERMINAL_SHOWN_SECONDS = 24 * 60 * 60
#: Ceiling on live (non-terminal) guides across every caller.
MAX_LIVE_GUIDES = 64
#: Ceiling on stored guides of any status.
MAX_STORED_GUIDES = 256

STATUS_OFFERED = "offered"
STATUS_ACTIVE = "active"
STATUS_TARGET_MISSING = "target_missing"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"

TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_CANCELLED, STATUS_EXPIRED})
LIVE_STATUSES = frozenset({STATUS_OFFERED, STATUS_ACTIVE, STATUS_TARGET_MISSING})

OUTCOME_OBSERVED = "observed"
OUTCOME_TARGET_MISSING = "target_missing"
OUTCOME_TARGET_FOUND = "target_found"
_OUTCOMES = (OUTCOME_OBSERVED, OUTCOME_TARGET_MISSING, OUTCOME_TARGET_FOUND)

#: ``target_missing`` because several copies of the target were visible at
#: once, so the guide could point at none of them.
REASON_AMBIGUOUS_TARGET = "ambiguous_target"
#: The tab that owns the guide did not answer a live observation in time.
REASON_STALE_TAB = "stale_tab"
#: A tab refused the guide: its bundle was built from another index than the
#: one the gateway accepted the guide against.
REASON_BUILD_MISMATCH = "build_mismatch"
#: The reasons a browser may give when it refuses (not ends) a guide.
TAB_REFUSE_REASONS = frozenset({REASON_BUILD_MISMATCH})
#: ``target_missing`` because a runtime predicate the step's control needs
#: (``UI_RUNTIME_PREDICATES``) is unmet, so the control cannot be drawn.
REASON_PREDICATE_UNMET = "predicate_unmet"
#: ``target_missing`` on a ``gate`` step: the gate (developer mode, a preview
#: flag) is off. The guide carries ``blocker: {kind, gate, setting_id}`` so the
#: agent can tell the user how to turn it on; the guide resumes once it is on.
REASON_GATE_OFF = "gate_off"
#: ``target_missing`` on a ``select`` step whose picker has nothing to choose.
#: The guide carries ``blocker: {kind, selection}``; never which entity.
REASON_NEEDS_SELECTION = "needs_selection"
#: ``target_missing`` on a ``ui.find`` action: no visible control carries the
#: name, on the page or inside any container the tab could open.
REASON_NOT_FOUND = "not_found"
#: The details a ``target_missing`` report may carry.
_MISSING_DETAILS = {
    "ambiguous": REASON_AMBIGUOUS_TARGET,
    "predicate_unmet": REASON_PREDICATE_UNMET,
    "gate_off": REASON_GATE_OFF,
    "selection_empty": REASON_NEEDS_SELECTION,
    "not_found": REASON_NOT_FOUND,
}
#: Which step kind each blocker reason may be reported on.
_DETAIL_STEP_KIND = {
    "gate_off": catalog.STEP_KIND_GATE,
    "selection_empty": catalog.STEP_KIND_SELECT,
}

REASON_CANCELLED_BY_USER = "cancelled_by_user"
REASON_SAVED_WITHOUT_GUIDE = "saved_without_guide"
#: The reasons a browser may give when it ends a guide.
TAB_CANCEL_REASONS = frozenset({REASON_CANCELLED_BY_USER, REASON_SAVED_WITHOUT_GUIDE})
#: A newer offer in the same conversation replaced this unfinished guide.
REASON_SUPERSEDED = "superseded"

_TAB_ID_MAX = 128
_GUIDE_ID_MAX = 64


class GuideError(Exception):
    """A refused guide operation, carrying its HTTP status and stable code."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class _Guide:
    guide_id: str
    slot_key: str
    session_key: str
    actions: list[dict[str, Any]]
    created_at: float
    expires_at: float
    status: str = STATUS_OFFERED
    revision: int = 1
    owner_tab: str | None = None
    lease_expires_at: float | None = None
    action_index: int = 0
    step_index: int = 0
    reason: str = ""
    finished_at: float | None = None
    pending_commit: str | None = None
    pending_kind: str = ""
    dismissed: bool = False
    intro: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def current_step_meta(self) -> dict[str, Any] | None:
        """The current ``ui.show`` step's ``select`` / ``gate`` entry, if it has one."""
        if not 0 <= self.action_index < len(self.actions):
            return None
        record = self.actions[self.action_index]
        ids, meta = record.get("step_ids"), record.get("step_meta")
        if not isinstance(ids, list) or not isinstance(meta, dict):
            return None
        if not 0 <= self.step_index < len(ids):
            return None
        entry = meta.get(ids[self.step_index])
        return entry if isinstance(entry, dict) else None

    def blocker(self) -> dict[str, Any] | None:
        """What a ``gate_off`` / ``needs_selection`` guide waits on: plan ids only."""
        if self.status != STATUS_TARGET_MISSING or self.reason not in (
            REASON_GATE_OFF,
            REASON_NEEDS_SELECTION,
        ):
            return None
        meta = self.current_step_meta()
        if meta is None:
            return None
        if self.reason == REASON_GATE_OFF:
            return {
                "kind": REASON_GATE_OFF,
                "gate": meta.get("gate"),
                "setting_id": meta.get("setting_id"),
            }
        return {"kind": REASON_NEEDS_SELECTION, "selection": meta.get("selection")}

    def to_public(self) -> dict[str, Any]:
        return {
            "guide_id": self.guide_id,
            "slot_key": self.slot_key,
            "status": self.status,
            "revision": self.revision,
            "owner_tab": self.owner_tab,
            "action_index": self.action_index,
            "step_index": self.step_index,
            "actions": copy.deepcopy(self.actions),
            "reason": self.reason,
            "blocker": self.blocker(),
            "expires_at": self.expires_at,
            "lease_expires_at": self.lease_expires_at,
            "finished_at": self.finished_at,
            "dismissed": self.dismissed,
            "intro": self.intro,
        }


def _clean_tab(tab_id: object) -> str:
    if not isinstance(tab_id, str) or not tab_id or len(tab_id) > _TAB_ID_MAX:
        raise GuideError(400, "invalid_tab_id", "tab_id is required")
    if any(ord(ch) < 0x21 or ch == "\x7f" for ch in tab_id):
        raise GuideError(400, "invalid_tab_id", "tab_id has invalid characters")
    return tab_id


def _clean_revision(revision: object) -> int:
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise GuideError(400, "invalid_revision", "revision must be a positive integer")
    return revision


def _clean_index(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GuideError(400, f"invalid_{name}", f"{name} must be a non-negative integer")
    return value


class GuideStore:
    """Every guide this gateway holds, bounded and in memory."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._guides: OrderedDict[str, _Guide] = OrderedDict()
        # Commit token -> guide id, for the in-flight mutation association.
        self._commits: dict[str, str] = {}

    # ── housekeeping ──

    def _touch(self, g: _Guide, *, reason: str | None = None) -> None:
        g.revision += 1
        if reason is not None:
            g.reason = reason

    def _finish(self, g: _Guide, status: str, reason: str) -> None:
        g.status = status
        g.reason = reason
        g.finished_at = self._clock()
        g.owner_tab = None
        g.lease_expires_at = None
        self._drop_commit(g)
        self._touch(g)

    def _drop_commit(self, g: _Guide) -> None:
        if g.pending_commit is not None:
            self._commits.pop(g.pending_commit, None)
        g.pending_commit = None
        g.pending_kind = ""

    def _refresh(self, g: _Guide) -> bool:
        """Apply TTL and lease lapse to *g*. Returns True when it changed."""
        if g.status in TERMINAL_STATUSES:
            return False
        now = self._clock()
        if now >= g.expires_at:
            self._finish(g, STATUS_EXPIRED, "expired")
            return True
        if g.owner_tab is not None and g.lease_expires_at is not None and now >= g.lease_expires_at:
            # The owning tab went quiet. Release it so another tab (or the same one
            # after a reload) can claim; the revision bump makes the lapsed tab's
            # next write stale. An in-flight commit stays associated: the save it
            # belongs to was submitted under a valid lease.
            g.owner_tab = None
            g.lease_expires_at = None
            if g.status == STATUS_ACTIVE:
                g.status = STATUS_OFFERED
            self._touch(g, reason="lease_lapsed")
            return True
        return False

    def sweep(self) -> list[dict[str, Any]]:
        """Refresh every guide and prune old terminal ones. Returns changed guides."""
        changed = []
        now = self._clock()
        for g in list(self._guides.values()):
            if self._refresh(g):
                changed.append(g.to_public())
        for gid, g in list(self._guides.items()):
            if (
                g.status in TERMINAL_STATUSES
                and g.finished_at is not None
                and now - g.finished_at >= TERMINAL_RETAIN_SECONDS
            ):
                del self._guides[gid]
        while len(self._guides) > MAX_STORED_GUIDES:
            victim = next(
                (gid for gid, g in self._guides.items() if g.status in TERMINAL_STATUSES), None
            )
            if victim is None:
                break
            del self._guides[victim]
        return changed

    def _get(self, guide_id: object) -> _Guide:
        if not isinstance(guide_id, str) or not guide_id or len(guide_id) > _GUIDE_ID_MAX:
            raise GuideError(400, "invalid_guide_id", "guide_id is required")
        g = self._guides.get(guide_id)
        if g is None:
            raise GuideError(404, "guide_not_found", "no such guide")
        self._refresh(g)
        return g

    # ── agent side (caller already verified; slot derived from its session) ──

    def start_superseding(
        self, *, slot_key: str, session_key: str, actions: object, intro: object = None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Offer a new guide in *slot_key*, replacing its unfinished one.

        Returns ``(new guide, superseded guides)``, both public. The
        conversation's live guide, if any, is cancelled with reason
        ``superseded`` once the new offer is valid (an invalid offer leaves it
        alone), so the agent never checks or cancels first; the caller
        broadcasts the superseded ones. A guide whose save is in flight is the
        one exception: the human is mid save, and that is refused as
        ``guide_saving``.
        """
        self.sweep()
        live_here = [
            g for g in self._guides.values() if g.slot_key == slot_key and g.status in LIVE_STATUSES
        ]
        if any(g.pending_commit is not None for g in live_here):
            raise GuideError(
                409,
                "guide_saving",
                "this session's guide is saving a change; try again once it finishes",
            )
        live_elsewhere = sum(
            1 for g in self._guides.values() if g.status in LIVE_STATUSES and g.slot_key != slot_key
        )
        if live_elsewhere >= MAX_LIVE_GUIDES:
            raise GuideError(429, "too_many_guides", "too many guides are in progress")
        try:
            records = catalog.validate_actions(actions)
            intro_text = catalog.validate_intro(intro)
        except catalog.GuideCatalogError as exc:
            raise GuideError(400, exc.code, exc.message) from None
        superseded = []
        for old in live_here:
            self._finish(old, STATUS_CANCELLED, REASON_SUPERSEDED)
            superseded.append(old.to_public())
        now = self._clock()
        g = _Guide(
            guide_id=f"g_{secrets.token_urlsafe(12)}",
            slot_key=slot_key,
            session_key=session_key,
            actions=records,
            created_at=now,
            expires_at=now + GUIDE_TTL_SECONDS,
            intro=intro_text,
        )
        self._guides[g.guide_id] = g
        return g.to_public(), superseded

    def start(
        self, *, slot_key: str, session_key: str, actions: object, intro: object = None
    ) -> dict[str, Any]:
        """:meth:`start_superseding`, answering only the new guide."""
        guide, _superseded = self.start_superseding(
            slot_key=slot_key, session_key=session_key, actions=actions, intro=intro
        )
        return guide

    def _owned_by_caller(self, guide_id: object, slot_key: str) -> _Guide:
        g = self._get(guide_id)
        if g.slot_key != slot_key:
            # Indistinguishable from absence: a caller learns nothing about
            # another session's guides.
            raise GuideError(404, "guide_not_found", "no such guide")
        return g

    def status_for_caller(self, *, slot_key: str, guide_id: object = None) -> dict[str, Any]:
        self.sweep()
        if guide_id not in (None, ""):
            return self._owned_by_caller(guide_id, slot_key).to_public()
        mine = [g for g in self._guides.values() if g.slot_key == slot_key]
        if not mine:
            raise GuideError(404, "guide_not_found", "this session has no guide")
        live = [g for g in mine if g.status in LIVE_STATUSES]
        return (live or mine)[-1].to_public()

    def owner_tab_for_slot(self, slot_key: str) -> tuple[str, str] | None:
        """``(guide_id, owner_tab)`` of *slot_key*'s running guide, or ``None``.

        A guide that was started is observed in the tab that owns it, never in
        whichever tab last sent a message.
        """
        self.sweep()
        for g in reversed(self._guides.values()):
            if (
                g.slot_key == slot_key
                and g.status in (STATUS_ACTIVE, STATUS_TARGET_MISSING)
                and g.owner_tab is not None
            ):
                return g.guide_id, g.owner_tab
        return None

    def cancel_by_caller(self, *, slot_key: str, guide_id: object) -> dict[str, Any]:
        g = self._owned_by_caller(guide_id, slot_key)
        if g.status in TERMINAL_STATUSES:
            raise GuideError(409, "guide_finished", f"guide is already {g.status}")
        self._finish(g, STATUS_CANCELLED, "cancelled_by_agent")
        return g.to_public()

    # ── browser side (owner cookie already verified) ──

    def retire_closed_slots(self, has_slot: Callable[[str], bool]) -> list[dict[str, Any]]:
        """Retire hints whose originating conversation was closed."""
        retired = []
        for guide in self._guides.values():
            if guide.status in LIVE_STATUSES and not has_slot(guide.slot_key):
                self._finish(guide, STATUS_CANCELLED, "slot_closed")
                retired.append(guide.to_public())
        return retired

    def pending(self, slot_key: str | None = None) -> list[dict[str, Any]]:
        """Live guides, plus each slot's newest recently ended, undismissed one.

        The ended one is what that slot's chat shows as a result line, so a page
        reload keeps it; one per slot keeps the answer bounded by the slot count.
        """
        self.sweep()
        now = self._clock()
        newest_ended: dict[str, _Guide] = {}
        for g in self._guides.values():
            if g.status in TERMINAL_STATUSES and g.finished_at is not None:
                held = newest_ended.get(g.slot_key)
                if held is None or (held.finished_at or 0.0) <= g.finished_at:
                    newest_ended[g.slot_key] = g
        live_slots = {g.slot_key for g in self._guides.values() if g.status in LIVE_STATUSES}
        shown = {
            g.guide_id
            for g in newest_ended.values()
            # A newer guide in progress supersedes the line, and a closed
            # conversation has no chat left to show one in.
            if g.slot_key not in live_slots
            and not g.dismissed
            and g.reason != "slot_closed"
            and now - (g.finished_at or 0.0) < TERMINAL_SHOWN_SECONDS
        }
        return [
            g.to_public()
            for g in self._guides.values()
            if (g.status in LIVE_STATUSES or g.guide_id in shown)
            and (not slot_key or g.slot_key == slot_key)
        ]

    def dismiss(self, *, guide_id: object) -> dict[str, Any]:
        """Hide an ended guide's result line. A live guide is cancelled, not dismissed."""
        g = self._get(guide_id)
        if g.status not in TERMINAL_STATUSES:
            raise GuideError(409, "guide_live", "a guide in progress is cancelled, not dismissed")
        if not g.dismissed:
            g.dismissed = True
            self._touch(g)
        return g.to_public()

    def replay(self, *, guide_id: object, revision: object) -> dict[str, Any]:
        """Offer a completed show-me guide again, from its first step.

        Only a guide whose every step is a UI step: replaying one that saved a
        change would walk the user into making it twice.
        """
        self.sweep()
        g = self._get(guide_id)
        self._check_revision(g, revision)
        if g.status != STATUS_COMPLETED:
            raise GuideError(409, "guide_not_completed", "only a finished guide can be shown again")
        if any(catalog.commit_step_index(a["id"]) is not None for a in g.actions):
            raise GuideError(409, "guide_not_replayable", "this guide made a change; ask again")
        if any(
            o.slot_key == g.slot_key and o.status in LIVE_STATUSES for o in self._guides.values()
        ):
            raise GuideError(
                409, "guide_active", "this session already has a guide in progress; cancel it first"
            )
        now = self._clock()
        g.status = STATUS_OFFERED
        g.action_index = 0
        g.step_index = 0
        # A replay is claimed afresh, by whichever tab (viewport) starts it.
        for record in g.actions:
            # A ui.find search is run again, so its last answer is not kept.
            record.pop("find", None)
            if catalog.record_needs_placement(record):
                for key in ("placement", "step_ids", "step_kinds"):
                    record.pop(key, None)
                record["step_count"] = None
        g.finished_at = None
        g.dismissed = False
        g.expires_at = now + GUIDE_TTL_SECONDS
        self._touch(g, reason="")
        return g.to_public()

    def _check_revision(self, g: _Guide, revision: object) -> None:
        rev = _clean_revision(revision)
        if rev != g.revision:
            raise GuideError(409, "stale_revision", "the guide changed; reload it")

    def _require_live(self, g: _Guide) -> None:
        if g.status in TERMINAL_STATUSES:
            raise GuideError(409, "guide_finished", f"guide is already {g.status}")

    def _require_owner_tab(self, g: _Guide, tab: str) -> None:
        if g.owner_tab != tab:
            raise GuideError(409, "not_owner_tab", "another tab owns this guide")

    def _placement_updates(self, g: _Guide, placements: object) -> dict[int, dict[str, Any]]:
        """Validate a claim's ``placements`` into per-action record updates.

        One entry per action: the ``ui.show`` placement the claiming tab walks
        (its viewport's), ``None`` for any other action. A version-2
        ``ui.show`` action not yet claimed must name one; one already claimed
        keeps its placement unless the claim names another for an action the
        guide has not started (a later action, or the current one at its
        first step), so a takeover from another viewport never walks a step
        list the guide is part-way through. Nothing is changed here.
        """
        n = len(g.actions)
        if placements is None:
            wanted: list[object] = [None] * n
        elif isinstance(placements, list) and len(placements) == n:
            wanted = list(placements)
        else:
            raise GuideError(400, "invalid_placements", "placements must list one entry per action")
        out: dict[int, dict[str, Any]] = {}
        for i, (record, pid) in enumerate(zip(g.actions, wanted)):
            if not catalog.record_needs_placement(record):
                if pid is not None:
                    raise GuideError(400, "invalid_placements", f"action {i} takes no placement")
                continue
            held = record.get("placement")
            if pid is None:
                if held is None:
                    raise GuideError(409, "placement_required", f"action {i} needs a placement")
                continue
            if pid == held:
                continue
            started = i < g.action_index or (i == g.action_index and g.step_index > 0)
            if held is not None and started:
                raise GuideError(
                    409, "placement_locked", f"action {i} is already being walked another way"
                )
            try:
                out[i] = catalog.claim_placement(record, pid)
            except catalog.GuideCatalogError as exc:
                raise GuideError(400, exc.code, exc.message) from None
        return out

    def claim(
        self,
        *,
        guide_id: object,
        tab_id: object,
        revision: object,
        take_over: object = False,
        placements: object = None,
    ) -> dict[str, Any]:
        g = self._get(guide_id)
        tab = _clean_tab(tab_id)
        self._require_live(g)
        self._check_revision(g, revision)
        if take_over is not True and take_over is not False:
            raise GuideError(400, "invalid_take_over", "take_over must be a boolean")
        if g.owner_tab is not None and g.owner_tab != tab and not take_over:
            raise GuideError(409, "owned_elsewhere", "another tab is showing this guide")
        # Validated in full before anything moves; recorded under the same
        # revision bump as the claim itself.
        for i, update in self._placement_updates(g, placements).items():
            g.actions[i].update(update)
        g.owner_tab = tab
        g.lease_expires_at = self._clock() + TAB_LEASE_SECONDS
        if g.status == STATUS_OFFERED:
            g.status = STATUS_ACTIVE
        self._touch(g, reason="")
        return g.to_public()

    def heartbeat(self, *, guide_id: object, tab_id: object, revision: object) -> dict[str, Any]:
        g = self._get(guide_id)
        tab = _clean_tab(tab_id)
        self._require_live(g)
        self._check_revision(g, revision)
        self._require_owner_tab(g, tab)
        # A lease renewal only; no revision bump, so it never makes the owner's own
        # in-flight progress report stale. The tab answering again also clears
        # an earlier missed observation.
        g.lease_expires_at = self._clock() + TAB_LEASE_SECONDS
        if g.reason == REASON_STALE_TAB:
            g.reason = ""
        return g.to_public()

    def note_stale_tab(self, guide_id: str) -> dict[str, Any] | None:
        """The owner tab missed a live observation: say so on the guide.

        Only the ``reason`` moves, with no revision bump, so the owner's own
        in-flight writes stay valid; its next heartbeat clears it. ``None`` when
        the guide is not a live one with an owner.
        """
        g = self._guides.get(guide_id)
        if g is None:
            return None
        self._refresh(g)
        if g.status not in (STATUS_ACTIVE, STATUS_TARGET_MISSING) or g.owner_tab is None:
            return None
        if g.reason in ("", "target_found", "target_missing", "replanned"):
            g.reason = REASON_STALE_TAB
        return g.to_public()

    def refuse(
        self, *, guide_id: object, tab_id: object, revision: object, reason: object
    ) -> dict[str, Any]:
        """A tab could not show the guide at all (``build_mismatch``).

        Nothing moves but the reason: the guide stays where it is, so a tab of
        the matching build (a reload) can still take it. A guide another tab
        owns is that tab's to report on.
        """
        g = self._get(guide_id)
        tab = _clean_tab(tab_id)
        self._require_live(g)
        self._check_revision(g, revision)
        if g.owner_tab is not None:
            self._require_owner_tab(g, tab)
        if not isinstance(reason, str) or reason not in TAB_REFUSE_REASONS:
            raise GuideError(400, "invalid_reason", "unknown refusal reason")
        if g.reason != reason:
            self._touch(g, reason=reason)
        return g.to_public()

    def replan(
        self,
        *,
        guide_id: object,
        tab_id: object,
        revision: object,
        action_index: object,
        placement: object,
    ) -> dict[str, Any]:
        """The owning tab's viewport changed: walk the current ``ui.show``
        action by *placement* from the current step on.

        Revision-checked and owner-only like every write. Allowed only at a
        step boundary of the current action that both placements share (the
        steps walked so far are the same steps; see
        :func:`guide_catalog.replan_placement`); the step index stays, and a
        guide whose target went missing because of the change is active again.
        Anything else is refused and nothing moves, so the tab shows the
        missing target as it would without a re-plan.
        """
        g = self._get(guide_id)
        tab = _clean_tab(tab_id)
        self._require_live(g)
        self._check_revision(g, revision)
        self._require_owner_tab(g, tab)
        ai = _clean_index(action_index, "action_index")
        if ai != g.action_index:
            raise GuideError(409, "wrong_step", "that is not the guide's current action")
        record = g.actions[ai]
        if not catalog.record_needs_placement(record) or record.get("placement") is None:
            raise GuideError(409, "replan_not_allowed", "this action has no placement to change")
        if g.pending_commit is not None:
            raise GuideError(409, "replan_not_allowed", "a save is in flight")
        if placement == record.get("placement"):
            return g.to_public()
        try:
            update = catalog.replan_placement(record, placement, g.step_index)
        except catalog.GuideCatalogError as exc:
            raise GuideError(
                409 if exc.code == "replan_not_at_boundary" else 400, exc.code, exc.message
            ) from None
        record.update(update)
        if g.status == STATUS_TARGET_MISSING:
            g.status = STATUS_ACTIVE
        self._touch(g, reason="replanned")
        return g.to_public()

    def progress(
        self,
        *,
        guide_id: object,
        tab_id: object,
        revision: object,
        action_index: object,
        step_index: object,
        outcome: object,
        resume_step_index: object = None,
        detail: object = None,
        step_id: object = None,
        resume_step_id: object = None,
        find: object = None,
    ) -> dict[str, Any]:
        g = self._get(guide_id)
        tab = _clean_tab(tab_id)
        self._require_live(g)
        self._check_revision(g, revision)
        self._require_owner_tab(g, tab)
        ai = _clean_index(action_index, "action_index")
        si = _clean_index(step_index, "step_index")
        if (ai, si) != (g.action_index, g.step_index):
            raise GuideError(409, "wrong_step", "that is not the guide's current step")
        # A version-2 ui.show step is named by the id the claim recorded, and
        # only by it; any other action's report carries none.
        ids = g.actions[ai].get("step_ids")
        if isinstance(ids, list):
            if step_id != ids[si]:
                raise GuideError(409, "wrong_step", "that is not the guide's current step id")
            if resume_step_index is not None:
                rsi = _clean_index(resume_step_index, "resume_step_index")
                if rsi >= len(ids) or resume_step_id != ids[rsi]:
                    raise GuideError(
                        409, "invalid_resume_step", "the guide cannot resume at that step"
                    )
        elif step_id is not None or resume_step_id is not None:
            raise GuideError(400, "invalid_step_id", "this action's steps carry no ids")
        if outcome not in _OUTCOMES:
            raise GuideError(
                400, "invalid_outcome", "outcome must be observed, target_missing or target_found"
            )
        is_find = g.actions[ai].get("id") == catalog.ACTION_UI_FIND
        report: dict[str, Any] | None = None
        if find is not None:
            if not is_find:
                raise GuideError(400, "invalid_find", "only a ui.find step reports a search")
            try:
                report = catalog.clean_find_report(find)
            except catalog.GuideCatalogError as exc:
                raise GuideError(400, exc.code, exc.message) from None
        if detail is not None and not isinstance(detail, str):
            # Browser-sent: a list or object would fail the lookups below with a
            # TypeError, which is a 500 rather than a refusal.
            raise GuideError(400, "invalid_detail", "detail must be a string")
        if detail == "not_found" and not is_find:
            raise GuideError(400, "invalid_detail", "only a ui.find step can be not found")
        if outcome == OUTCOME_TARGET_FOUND:
            # Recovery, not progress: the same step is shown again, so nothing is
            # completed and any step kind may recover. Only a missing guide moves;
            # an active one is answered as it is, with no revision bump, so a
            # repeated report never makes the owner's next write stale.
            if g.status == STATUS_TARGET_MISSING:
                if resume_step_index is not None:
                    # The page came back at an EARLIER step of this action (a
                    # remounted form starts over): the guide follows the page
                    # back, never forward and never across a pending save.
                    rs = _clean_index(resume_step_index, "resume_step_index")
                    if rs > si or g.pending_commit is not None:
                        raise GuideError(
                            409, "invalid_resume_step", "the guide cannot resume at that step"
                        )
                    g.step_index = rs
                g.status = STATUS_ACTIVE
                self._keep_find(g, ai, report)
                self._touch(g, reason="target_found")
            return g.to_public()
        if detail is not None and (
            outcome != OUTCOME_TARGET_MISSING or detail not in _MISSING_DETAILS
        ):
            raise GuideError(400, "invalid_detail", "detail names why a target is missing")
        if detail in _DETAIL_STEP_KIND:
            # A gate or selection blocker is reported only by the step that
            # waits on one, as the record's own plan says.
            meta = g.current_step_meta()
            if meta is None or meta.get("kind") != _DETAIL_STEP_KIND[str(detail)]:
                raise GuideError(400, "invalid_detail", "this step waits on no such thing")
        if outcome == OUTCOME_TARGET_MISSING:
            g.status = STATUS_TARGET_MISSING
            reason = "target_missing" if detail is None else _MISSING_DETAILS[str(detail)]
            self._keep_find(g, ai, report)
            self._touch(g, reason=reason)
            return g.to_public()
        if catalog.record_step_kind(g.actions[ai], si) != catalog.STEP_UI:
            # The mutation step is proven by the gateway's own route, never by the
            # browser saying so. Kinds are read from the stored record: a
            # ``ui.show`` record carries its own steps, all of them UI steps.
            raise GuideError(
                409,
                "commit_step_requires_server_evidence",
                "this step completes only when the change is actually saved",
            )
        g.status = STATUS_ACTIVE
        self._keep_find(g, ai, report)
        self._advance(g)
        return g.to_public()

    @staticmethod
    def _keep_find(g: _Guide, action_index: int, report: dict[str, Any] | None) -> None:
        """Record a validated ``ui.find`` search result, in the move it came with."""
        if report is not None:
            g.actions[action_index]["find"] = report

    def cancel_by_tab(
        self, *, guide_id: object, tab_id: object, revision: object, reason: object = None
    ) -> dict[str, Any]:
        """End the guide. ``reason`` is one of :data:`TAB_CANCEL_REASONS`.

        ``saved_without_guide``: the action's own save went through without the
        guide's association (the tab had not caught up), so the guide has no
        evidence to complete on and ends saying the change was made outside it.
        """
        g = self._get(guide_id)
        tab = _clean_tab(tab_id)
        self._require_live(g)
        self._check_revision(g, revision)
        if g.owner_tab is not None:
            self._require_owner_tab(g, tab)
        if reason is None:
            reason = REASON_CANCELLED_BY_USER
        if not isinstance(reason, str) or reason not in TAB_CANCEL_REASONS:
            raise GuideError(400, "invalid_reason", "unknown cancel reason")
        self._finish(g, STATUS_CANCELLED, str(reason))
        return g.to_public()

    def _advance(self, g: _Guide) -> None:
        count = int(g.actions[g.action_index]["step_count"])
        if g.step_index + 1 < count:
            g.step_index += 1
            self._touch(g, reason="")
            return
        if g.action_index + 1 < len(g.actions):
            g.action_index += 1
            g.step_index = 0
            self._touch(g, reason="")
            return
        self._finish(g, STATUS_COMPLETED, "completed")

    # ── mutation association (called only by the owner-only mutation routes) ──

    def begin_commit(
        self, *, guide_id: object, tab_id: object, revision: object, kind: str
    ) -> str | None:
        """Associate one in-flight owner request with the guide's commit step.

        Returns an opaque token, or ``None`` when the headers do not name a guide
        currently waiting on exactly this kind of commit from exactly this tab at
        exactly this revision. ``None`` never blocks the mutation itself: the
        human's save always proceeds; it just does not count for the guide.
        """
        try:
            g = self._get(guide_id)
            tab = _clean_tab(tab_id)
            self._check_revision(g, _coerce_header_int(revision))
        except GuideError:
            return None
        if g.status not in (STATUS_ACTIVE, STATUS_TARGET_MISSING) or g.owner_tab != tab:
            return None
        if g.pending_commit is not None:
            return None
        action_id = g.actions[g.action_index]["id"]
        if action_id != kind or catalog.commit_step_index(action_id) != g.step_index:
            return None
        token = secrets.token_urlsafe(16)
        g.pending_commit = token
        g.pending_kind = kind
        self._commits[token] = g.guide_id
        return token

    def abort_commit(self, token: str | None) -> None:
        if not token:
            return
        gid = self._commits.pop(token, None)
        g = self._guides.get(gid) if gid else None
        if g is not None and g.pending_commit == token:
            g.pending_commit = None
            g.pending_kind = ""

    def finish_commit(self, token: str | None, evidence: dict[str, Any]) -> dict[str, Any] | None:
        """Record the mutation route's own result and advance. ``None`` if retired.

        Revalidates everything: the guide still exists, is not terminal (a cancel
        or expiry in the meantime wins), still holds THIS token, and is still on
        the commit step of the action the token was issued for.
        """
        if not token:
            return None
        gid = self._commits.pop(token, None)
        g = self._guides.get(gid) if gid else None
        if g is None:
            return None
        self._refresh(g)
        if g.pending_commit != token or g.status in TERMINAL_STATUSES:
            return None
        kind = g.pending_kind
        g.pending_commit = None
        g.pending_kind = ""
        action = g.actions[g.action_index]
        if action["id"] != kind or catalog.commit_step_index(kind) != g.step_index:
            return None
        action["result"] = dict(evidence)
        g.status = STATUS_ACTIVE
        self._advance(g)
        return g.to_public()


def _coerce_header_int(value: object) -> object:
    """A header revision arrives as text; anything but plain digits stays invalid."""
    if isinstance(value, str) and value.isdigit() and len(value) <= 12:
        return int(value)
    return value


def guide_store_for(state: Any) -> GuideStore:
    """The one store attached to this gateway's dashboard state."""
    store = getattr(state, "_guide_store", None)
    if not isinstance(store, GuideStore):
        store = GuideStore()
        state._guide_store = store
    return store
