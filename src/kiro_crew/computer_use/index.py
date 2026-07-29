"""Per-session, per-app snapshot cache — the element-index lifecycle.

Element indices address a LIVE user interface, so the cache that maps an index
back to an element is a correctness control, not an optimization. Three
mechanisms bound its validity, none of which needs a turn-boundary signal
kiro-cli does not have:

1. **Hard fail, never lazy re-snapshot.** Acting on an app with no cached
   snapshot is refused. A lazy re-walk would silently let the model act on a
   tree it was never shown, which is exactly the failure indices are supposed to
   make impossible.
2. **TTL** (:data:`SNAPSHOT_TTL_SECS`). ``time.monotonic()`` throughout: a wall
   clock adjustment must never make a stale snapshot look fresh.
3. **Fingerprint drift**, checked by the caller against a FRESH walk before
   every mutating action. This module supplies the comparison; the driver
   supplies the fresh tree.

Plus :meth:`SnapshotIndex.end_turn` for an explicit early release.

**Entries are keyed by ``(session_key, window_key)``** — session, then the specific
WINDOW, and both halves came from a review finding.

The WINDOW half: element indices address one window's accessibility tree, so keying
by application alone aliased distinct windows of the same app. Snapshot document A,
focus document B, and the follow-up action — which re-resolves to B — retrieved A's
cached tree. The fingerprint check cannot catch it, because two documents of the same
app routinely have identically-shaped toolbars, so ``role|subrole|title`` at a given
index matches and the action mutates the wrong document. ``AppRef.window_key``
carries the pid as well as the window id, since a window id is only unique within a
session and a relaunched app can reuse one.

The SESSION half (reviewer finding). The gateway runs one
process serving every surface — dashboard tabs, Slack threads, cron jobs — so a
cache keyed by application ALONE is shared mutable state across concurrent
sessions: session A walks Preview and is shown indices, session B then walks
Preview after the UI moved and its snapshot REPLACES A's under the same key, and
A's next action resolves (and fingerprint-verifies) against B's tree. Both
sessions look internally consistent and the wrong control is activated, which is
the one outcome element addressing exists to prevent. The key is therefore
``(session_key, app_key)``, and lifecycle operations are per-session: one
session's ``computer_end_turn`` must not blow away another's live indices.

The cap is per session as well, so a chatty session cannot evict another's
entries — the eviction path would otherwise be a cross-session denial of service
that surfaces as a confusing "call computer_get_state first".

Honest limit, stated here and in the spec: fingerprinting narrows the race, it
does not eliminate it. A tree can still change between the verifying walk and
the action microseconds later. It converts a silent wrong-click into a loud
refusal in the overwhelming majority of cases; it is not a transactional
guarantee. Namespacing removes the CROSS-SESSION race entirely (two sessions can
no longer share an entry); it does not remove the within-session one, which is
inherent to driving a live UI.

Pure: no ctypes, no I/O, no platform calls.
"""

from __future__ import annotations

import threading
import time

from kiro_crew.computer_use.types import (
    ERR_INDEX_DRIFT,
    ERR_NO_STATE,
    ERR_STALE_STATE,
    ERR_UNKNOWN_INDEX,
    MAX_INDEXED_APPS,
    SNAPSHOT_TTL_SECS,
    TOOL_GET_STATE,
    ElementRec,
    Snapshot,
    StaleIndex,
)


class SnapshotIndex:
    """Bounded, TTL'd map of ``(session key, window key)`` -> last :class:`Snapshot`.

    Thread-safe: the MCP stdio loop dispatches tool calls on a worker thread
    while the main thread reads stdin, and the gateway dispatches concurrent
    sessions' calls on a thread pool — so every mutation is under one lock.

    Every accessor takes the ``session_key`` explicitly rather than reading an
    ambient value. The dispatcher already carries that identity (it is the same
    key the authoritative gate is queried with), and a thread-local would be
    wrong here: the gateway runs each dispatch on a POOLED thread, so a
    thread-local would attribute one session's snapshot to whichever session
    reused the thread next.
    """

    def __init__(
        self,
        *,
        ttl_secs: float = SNAPSHOT_TTL_SECS,
        max_apps: int = MAX_INDEXED_APPS,
    ) -> None:
        self._ttl = ttl_secs
        self._max_apps = max_apps
        self._lock = threading.Lock()
        # Insertion-ordered: dicts preserve order, so the oldest inserted key is
        # the first one to evict once the cap is hit. Keyed by the COMPOSITE
        # (session, app) — see the module docstring for why app alone is unsafe.
        self._snapshots: dict[tuple[str, str], Snapshot] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._snapshots)

    @property
    def keys(self) -> tuple[tuple[str, str], ...]:
        """Currently cached ``(session key, app key)`` pairs (diagnostics/tests)."""
        with self._lock:
            return tuple(self._snapshots)

    def app_keys(self, session_key: str) -> tuple[str, ...]:
        """App keys cached for ONE session (diagnostics/tests)."""
        with self._lock:
            return tuple(app for sess, app in self._snapshots if sess == session_key)

    def _count_locked(self, session_key: str) -> int:
        """Entries held by *session_key*. Caller holds the lock."""
        return sum(1 for sess, _app in self._snapshots if sess == session_key)

    def put(self, snap: Snapshot, *, session_key: str) -> None:
        """Cache *snap* for *session_key*, replacing that session's prior entry.

        Evicts the oldest entry OF THIS SESSION when the per-session cap is hit.
        Only a handful of apps are ever in play in one turn, so the cap exists to
        bound RSS (each snapshot holds its encoded JPEG), not to manage
        contention — and it is per-session so a chatty surface cannot evict
        another's live indices.
        """
        composite = (session_key, snap.key)
        with self._lock:
            self._snapshots.pop(composite, None)
            self._snapshots[composite] = snap
            while self._count_locked(session_key) > self._max_apps:
                oldest = next(key for key in self._snapshots if key[0] == session_key)
                self._snapshots.pop(oldest, None)

    def get(self, app_key: str, *, session_key: str, now: float | None = None) -> Snapshot | None:
        """This session's cached snapshot for *app_key*, or ``None`` if absent/expired.

        An expired entry is DROPPED here rather than left to rot, so a later
        call cannot resurrect it and the cap is not consumed by dead entries.
        """
        stamp = time.monotonic() if now is None else now
        composite = (session_key, app_key)
        with self._lock:
            snap = self._snapshots.get(composite)
            if snap is None:
                return None
            if stamp - snap.captured_at > self._ttl:
                self._snapshots.pop(composite, None)
                return None
            return snap

    def age(self, app_key: str, *, session_key: str, now: float | None = None) -> float:
        """Age in seconds of this session's cached snapshot, or ``-1.0`` when absent.

        Read WITHOUT the TTL drop so an expiry message can quote the real age.
        """
        stamp = time.monotonic() if now is None else now
        with self._lock:
            snap = self._snapshots.get((session_key, app_key))
            return -1.0 if snap is None else stamp - snap.captured_at

    def require(
        self,
        app_key: str,
        app_label: str,
        *,
        session_key: str,
        now: float | None = None,
    ) -> Snapshot:
        """Return a live snapshot for *app_key* or raise :class:`StaleIndex`.

        The two refusals are distinct on purpose: "call get_state first" and
        "your state is N seconds old" tell the model different things, and the
        age number is what makes the second one actionable. A snapshot another
        session took is not visible here at all, so it produces the ordinary
        "call get_state first" refusal rather than someone else's tree.
        """
        stamp = time.monotonic() if now is None else now
        age = self.age(app_key, session_key=session_key, now=stamp)
        snap = self.get(app_key, session_key=session_key, now=stamp)
        if snap is None:
            if age < 0:
                raise StaleIndex(ERR_NO_STATE.format(app=app_label, tool=TOOL_GET_STATE))
            raise StaleIndex(
                ERR_STALE_STATE.format(app=app_label, age=int(age), tool=TOOL_GET_STATE)
            )
        return snap

    def resolve(self, snap: Snapshot, element_index: int) -> ElementRec:
        """Return the element at *element_index* within *snap*.

        Looked up by the record's own ``index`` field rather than by list
        position: elided container nodes never consume an index, so position and
        index are NOT interchangeable, and indexing by position would silently
        address a different element than the one the model was shown.
        """
        for rec in snap.elements:
            if rec.index == element_index:
                return rec
        raise StaleIndex(
            ERR_UNKNOWN_INDEX.format(
                index=element_index,
                app=snap.app.bundle_id or snap.app.name,
                count=len(snap.elements),
            )
        )

    def invalidate(self, app_key: str, *, session_key: str) -> None:
        """Drop THIS session's cached snapshot for one app (post-action, or on drift)."""
        with self._lock:
            self._snapshots.pop((session_key, app_key), None)

    def end_turn(self, *, session_key: str) -> None:
        """Drop every snapshot cached for *session_key* — the explicit early release.

        Called by ``computer_end_turn``: once the model is done acting, holding
        stale trees serves no purpose and every later index is a liability.

        Scoped to the calling session. A process-wide clear would let any surface
        invalidate every OTHER surface's live indices, turning another session's
        next action into a spurious "call computer_get_state first".
        """
        with self._lock:
            for key in [k for k in self._snapshots if k[0] == session_key]:
                self._snapshots.pop(key, None)

    def clear(self) -> None:
        """Drop EVERY session's snapshots — lifecycle only (backend reset/teardown).

        Deliberately not reachable from a tool: this is the process-wide reset the
        backend swap needs (indices from one driver's walk are meaningless against
        another's), whereas :meth:`end_turn` is the per-session release a model
        can ask for.
        """
        with self._lock:
            self._snapshots.clear()


def drift_message(
    app_label: str,
    element_index: int,
    before: str,
    after: str,
) -> str:
    """The refusal text for a fingerprint mismatch.

    Names BOTH the old and the new identity: without them the model cannot tell
    whether the UI merely re-laid out or it was about to click something
    genuinely different, and it would just retry blindly.
    """
    return ERR_INDEX_DRIFT.format(
        index=element_index,
        tool=TOOL_GET_STATE,
        before=before,
        after=after,
    )


# ── Process-wide shared index ──
# The sidecar has exactly one snapshot cache: the dispatch chokepoint and the
# lifecycle hooks (``computer_end_turn``, backend reset) must all see the same
# map, and passing it through every call site would be threaded state for no
# benefit in a single-purpose process.

_shared_index: SnapshotIndex | None = None
_shared_index_lock = threading.Lock()


def get_shared_index() -> SnapshotIndex:
    """Process-wide snapshot cache singleton."""
    global _shared_index
    with _shared_index_lock:
        if _shared_index is None:
            _shared_index = SnapshotIndex()
        return _shared_index


def reset_shared_index() -> None:
    """Drop the shared cache (tests, backend swap, KIROCREW_HOME changes).

    Swapping the backend MUST drop the cache: indices from one driver's walk are
    meaningless against another's, and a fake backend inheriting a real
    driver's snapshot (or the reverse) would be a genuine correctness bug.
    """
    global _shared_index
    with _shared_index_lock:
        if _shared_index is not None:
            _shared_index.clear()
        _shared_index = None
