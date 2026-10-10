"""The v3 dashboard's push: one open page's fold subscriptions in, block patches out.

The protocol this module implements, in the five sentences the brief fixes:

1. **First load renders the whole page.** The controller's
   ``GET /api/members/{slug}/dashboard`` serves every block's values, and ARMS one
   :class:`LivePage` for the crewmate as it does so. The body carries the push version
   the page starts from, so the first patch can be checked against something.
2. **After that, a fold pushes only the blocks that subscribe to it.** One crew-log bus
   subscription per fold the Model names; a fold that moves is turned into the values of
   the blocks whose fields read THAT fold, and nothing else is sent.
3. **Over the existing WebSocket.** ``state.broadcast_ws_owners`` -- the same hub and
   the same owner-socket set ``slot_projection`` uses. :data:`BLOCK_FRAME` is a new
   message TYPE on that channel, deliberately not a second channel: a socket of its own
   would be a second place to get the owner gate, the backpressure and the reconnect
   right.
4. **A version gap, or a layout change, triggers a full refetch.** Every patch carries
   ``version`` -- a counter that steps by exactly one per frame for this page -- and
   ``layout``, the package artifact's version. The page refetches when ``version`` is
   not the one it holds plus one, which also covers a gateway restart (the counter
   restarts at zero, which is not ``held + 1``). A layout change is detected HERE, on
   the re-read below, and sent as a frame carrying ``refetch`` and no values at all:
   the block set itself may have changed, so values computed under the new layout must
   not be applied to a document rendered under the old one.
5. **The owner check and the redaction stay in the controller.** Neither is implemented
   in this module. ``redact`` and ``reread`` are handed in by
   :func:`~kiro_crew.dashboard.handlers.member_dashboard.arm_block_push`, which is where
   the binding is checked and where ``_page_safe`` lives; this module cannot emit a
   value that did not cross ``redact`` because :meth:`LivePage.blocks` is the only place
   values are produced and it calls it on every path.

WHY THERE ARE TWO FOLD FEEDS, and why this one is not built on the other. The repository
already has :class:`kiro_crew.dashboard_feed.DashboardFeed`
(``src/kiro_crew/dashboard_feed.py:151``), and :class:`LivePage`
(``src/kiro_crew/dashboard/handlers/member_dashboard_push.py:113``) caches the same
bus's folds. They are not interchangeable, because they answer "which folds, and where
in them" from DIFFERENT documents: the feed resolves a field through a
``TemplateManifest`` -- a shipped template's own declaration, which it imports at module
scope -- while this page resolves it through the package ``DashboardModel``, which is the
crewmate's own stored layout. Rebuilding this push on the feed would mean teaching the
feed to read a package, which points the template line's module at package code; and the
template line is being RETIRED, so that edge would have to be unpicked again. The one
thing that genuinely is shared -- the per-fold cache cell and the revision rule ordering
it -- is imported rather than copied (see ``_Cell`` below). ``DashboardFeed`` goes away
with the template line, and these two converge by the feed being deleted, not by this
module depending on it.

Threads. A bus callback runs on the crew log's FOLD WORKER thread, synchronously inside
``bus.publish``, so it does what every other subscriber in this repo does: compare a
revision, store a value, hop to the loop, return. Nothing else -- no IO, no socket, no
lock held across the hop. The re-read that decides a layout change is file IO and runs
on a worker thread through ``asyncio.to_thread``, never on the loop and never on the
folder.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from typing import Any, Callable, Final, Mapping

from kiro_crew.crew_log.projection import DASHBOARD_FOLD_NAME as _DASHBOARD_FOLD_NAME

# ``_Cell`` IS IMPORTED, NOT RE-DECLARED. Both this module and ``dashboard_feed`` cache
# the same bus's folds, so both need "the latest value plus the revision that orders it"
# -- and a second declaration of it is a second place for that ordering rule to drift,
# which is the one rule a cache must not get wrong. One home, and it is the feed's:
# ``dashboard_feed`` must not import package code, so the dependency can only point this
# way. Private by name and shared by one deliberate import rather than public, because
# it is still the fold cache's own type and not an interface anything else may hold.
#
# The two feeds remain separate, and that is not an oversight: see the module docstring.
from kiro_crew.dashboard_feed import MISSING, _Cell, resolve_path, scope_for

logger = logging.getLogger(__name__)

#: The module's surface, and all of it: what the CONTROLLER calls. A read of the
#: registry and a teardown hook live in the test that wants them, because the
#: controller is handed the page it armed and no shutdown path tears these down.
__all__ = [
    "BLOCK_FRAME",
    "LivePage",
    "close_page",
    "open_page",
]

#: The one WS message type this push uses. A TYPE on the existing owner channel, not a
#: channel: see the module docstring.
BLOCK_FRAME: Final[str] = "dashboard_block_patch"

#: The fold a package's agentic fields are read from. The crewmate's own writes land in
#: one slot fold, exactly as ``member_dashboard.read_fields`` reads them for a v2 page.
#:
#: Read from the projection kernel rather than spelled here, because the kernel's own
#: name for it (``entry_types.DASHBOARD_FOLD_NAME``) is ``"agentic"`` -- two spellings of
#: one fold is how a push ends up subscribed to a fold nothing ever advances.
AGENTIC_FOLD: Final[str] = _DASHBOARD_FOLD_NAME

#: How many crewmates may have a live push at once. Each page holds one bus subscription
#: per fold it reads, so an unbounded map would grow for the life of the gateway on a
#: host whose operator opens every member's tab. The least recently armed page is closed
#: when the cap is passed, which costs that tab its pushes until its next read arms it
#: again -- a refetch, never a wrong value.
MAX_LIVE_PAGES: Final[int] = 32

#: Why a frame asks for a full refetch. Sent as ``reason`` beside ``refetch: true``, so
#: a reader looking at one frame can tell a recomposed layout from a rebound page.
REASON_LAYOUT: Final[str] = "layout_changed"
REASON_UNBOUND: Final[str] = "package_unbound"


class LivePage:
    """One crewmate's open v3 dashboard.

    Built by :func:`open_page` and never directly: the registry is what makes a second
    arming of the same crewmate refresh one page rather than leave two sets of
    subscriptions writing to two counters the browser cannot tell apart.
    """

    def __init__(
        self,
        slug: str,
        member: str,
        model: Any,
        *,
        slot: str,
        state: Any,
        loop: asyncio.AbstractEventLoop | None,
        redact: Callable[[Any], Any],
        reread: Callable[[str], Any],
        locale: str = "",
        display_seam: Callable[..., Any] | None = None,
        patch_seam: Callable[..., Any] | None = None,
        package: Mapping[str, Any] | None = None,
    ) -> None:
        if not callable(redact):
            # Refused rather than defaulted. A default would be a second redactor, and
            # the one that matters is the controller's.
            raise TypeError("a live dashboard page needs the controller's redactor")
        if not slot:
            # REFUSED RATHER THAN DERIVED, for a stronger reason than the redactor's.
            # The slug is not the slot: a V2 crewmate's DM log lives on a store-scoped
            # key, so subscribing by slug reads an EMPTY slot and the page would render
            # every field unresolved -- and two display names that slugify together
            # ("Atlas" and "Member Atlas") would read ONE slot, putting one crewmate's
            # values on the other's dashboard. `member_dashboard._dashboard_slot` is the
            # one derivation, it needs config IO, and this object must not do IO; so the
            # caller resolves it and an unresolved one is an error rather than a guess.
            raise ValueError("a live dashboard page needs the crewmate's DM slot key")
        self.slug = slug
        #: The DM slot this page's slot-scoped folds are keyed by -- NOT the slug. See
        #: the refusal above and :meth:`bus_key`.
        self.slot = slot
        self.member = member
        self.model = model
        #: The canonical package the Model was projected from. Kept beside it rather
        #: than re-read, and REPLACED TOGETHER WITH IT on every re-read, because
        #: ``block_patch`` narrows per block off ``package["view"]`` while the Model
        #: answers which fields exist -- two halves of one layout that must not come
        #: from two different versions of it.
        self.package: Mapping[str, Any] = package or {}
        #: The reader's UI language as the first load resolved it. Carried because a
        #: push has no request to ask, and the document picks its own words by it.
        self.locale = locale
        self._state = state
        self._loop = loop
        self._redact = redact
        self._reread = reread
        self._display_seam = display_seam
        self._patch_seam = patch_seam
        self._lock = threading.Lock()
        self._cells: dict[str, _Cell] = {}
        self._disposers: list[Callable[[], None]] = []
        self._version = 0
        self._closed = False
        #: True only while :meth:`subscribe` is running. The bus delivers a baseline
        #: SYNCHRONOUSLY inside ``subscribe``, on this thread, and that value is the one
        #: the first load is about to serve -- so it is cached and NOT pushed. Without
        #: this, arming a page sends the browser a patch carrying what its own response
        #: body already holds, and ``push_version`` in that body depends on whether the
        #: patch was broadcast before or after it was read.
        self._priming = False

    # -- identity ----------------------------------------------------------- #

    @property
    def version(self) -> int:
        """The push version the browser should be holding. Carried by the first load."""
        return self._version

    @property
    def layout(self) -> int:
        """The package artifact's version, which moves only on a layout change."""
        return int(getattr(self.model, "version", 0) or 0)

    @property
    def binding(self) -> str:
        return str(getattr(self.model, "bound_to", "") or "")

    def bind(self, state: Any, loop: asyncio.AbstractEventLoop | None) -> None:
        """Re-point this page at the loop and the hub now serving.

        The same reason ``install_crew_log_publisher`` re-binds its publisher: a gateway
        restarted inside one interpreter must push on the loop that is actually serving,
        through the hub that actually holds the sockets.
        """
        self._state = state
        self._loop = loop

    # -- subscription ------------------------------------------------------- #

    def folds(self) -> frozenset[str]:
        """The folds this page reads: the Model's, plus the agentic one when it writes."""
        folds = set(getattr(self.model, "folds", frozenset()))
        if getattr(self.model, "agentic_fields", None) and self.model.agentic_fields():
            folds.add(AGENTIC_FOLD)
        return frozenset(folds)

    def bus_key(self, scope: str) -> str:
        """The bus key this page subscribes with under *scope*, or ``""`` to skip it.

        THE ONE PLACE A KEY KIND IS DECIDED, and the extension point for a third one.
        ``dashboard_feed.scope_for`` answers which scope a fold is keyed by, off the
        projection kernel's own sets; this answers what the key then IS, which only
        this page knows.

        Today exactly one kind is reachable. A package's binding is ``crewmate:<slug>``
        or ``session:<slot key>``, so a page always has a SLOT key -- and never a session
        UNIT id, which is what ``SCOPE_SESSION`` is keyed by. A session fold is therefore
        not something this page can subscribe to at all, and saying so is better than
        guessing a key and never receiving an event.

        THE SLOT KEY IS :attr:`slot`, NOT THE SLUG, and the difference is the whole
        reason the constructor demands it: a V2 crewmate's DM log lives on a
        store-scoped key, so the bare slug names a different and empty slot. Keyed by
        slug this page subscribes where nothing is ever published -- the baseline reads
        nothing and every field renders unresolved -- and two display names that
        slugify together read the same slot, so one crewmate's conversation values
        appear on the other's dashboard. ``member_dashboard._dashboard_slot`` is the
        same derivation ``api_member_thread`` creates the thread with.

        A THIRD KEY KIND (D4's registered tree fold) lands here: give its scope a row
        and the subscription path above takes it unchanged. Until it has one, a fold
        under it is reported unavailable rather than silently never delivered -- which
        is the difference this method exists to make.
        """
        from kiro_crew.crew_log import bus as crew_log_bus

        if scope == crew_log_bus.SCOPE_SLOT:
            return self.slot
        return ""

    def subscribe(self) -> list[str]:
        """Subscribe to this page's folds; return the ones that could not be.

        ``baseline=True`` on every subscription, for the reason :class:`DashboardFeed`
        gives: without it the page holds a subscription that is correct from now on and
        empty until the crewmate happens to do something, so a dashboard opened on a
        quiet crewmate would push nothing over a log that has the numbers in it. THE
        BASELINE READ IS FILE IO on the calling thread, so a caller on the event loop
        runs this through ``asyncio.to_thread``.
        """
        from kiro_crew.crew_log import bus as crew_log_bus

        self.unsubscribe()
        self._priming = True
        try:
            return self._subscribe_each(crew_log_bus)
        finally:
            self._priming = False

    def _subscribe_each(self, crew_log_bus: Any) -> list[str]:
        """One subscription per reachable fold. Split out so priming is exception-safe."""
        unavailable: list[str] = []
        for fold in sorted(self.folds()):
            scope = scope_for(fold)
            key = self.bus_key(scope)
            if not scope or not key:
                # A fold this page cannot reach. Reported rather than raised -- the
                # fields it feeds read as missing, which is the honest rendering, and
                # one unreachable fold must not deny the page the others.
                unavailable.append(fold)
                continue
            with self._lock:
                self._cells.setdefault(fold, _Cell())
            try:
                self._disposers.append(
                    crew_log_bus.subscribe(
                        crew_log_bus.FOLD_ADVANCED,
                        self._callback(fold),
                        scope=scope,
                        key=key,
                        fold=fold,
                        baseline=True,
                    )
                )
            except Exception:
                logger.warning(
                    "dashboard push: the %s fold could not be subscribed for %r",
                    fold,
                    self.slug,
                    exc_info=True,
                )
                unavailable.append(fold)
        return unavailable

    def ensure_subscribed(self) -> list[str]:
        """Subscribe only if this page is not subscribed already.

        What the READ calls, because a reused page is the ordinary case: a tab that
        polls would otherwise tear down its subscriptions and re-run a baseline file
        read per fold per request, and -- worse -- serve its full load from a cache it
        had just emptied. A page that is already subscribed holds values the bus has
        been keeping current, which is the whole point of a live feed.

        Retried when nothing is subscribed, which includes the case where every fold
        was unavailable last time: the condition may have changed.
        """
        if self._disposers:
            return []
        return self.subscribe()

    def unsubscribe(self) -> None:
        """Drop every subscription and forget every cached value. Idempotent."""
        disposers, self._disposers = self._disposers, []
        for dispose in disposers:
            dispose()
        with self._lock:
            self._cells.clear()

    def close(self) -> None:
        """Stop pushing for good. A closed page never sends another frame."""
        self._closed = True
        self.unsubscribe()

    def _callback(self, fold: str) -> Callable[[Any], None]:
        """The per-fold callback, closed over the fold NAME rather than reading it.

        The bus filters on fold already, so the event's own name adds nothing -- and
        taking it from the closure means a malformed event cannot write into the wrong
        cell, which is the one thing a cache must not let a publisher do.
        """

        def _apply(event: Any) -> None:
            self.on_fold(fold, event)

        return _apply

    # -- fold worker thread -------------------------------------------------- #

    def on_fold(self, fold: str, event: Any) -> None:
        """Store one fold value if it is newer, then hand the push to the loop.

        Runs on the FOLD WORKER thread. The event is read DEFENSIVELY -- a bus carries
        whatever a publisher sends, and a malformed one must cost this push rather than
        the fan-out to the next subscriber.
        """
        if self._closed:
            return
        revision = int(getattr(event, "revision", 0) or 0)
        seq = int(getattr(event, "seq", 0) or 0)
        value = getattr(event, "value", None)
        if revision <= 0 or not isinstance(value, Mapping):
            return
        if fold not in self.folds():
            # A fold this page does not read. The bus filters on fold already and the
            # callback is closed over the name, so this cannot happen through a
            # subscription -- it is the guard for a caller holding an event, and it is
            # the one thing a cache must not let a publisher do: write the wrong cell.
            return
        with self._lock:
            cell = self._cells.get(fold)
            if cell is None:
                # Created on first arrival rather than only by :meth:`subscribe`, so a
                # value cannot be dropped for arriving between the subscribe and the
                # baseline -- the fold set above is what decides, not the cache's shape.
                cell = self._cells[fold] = _Cell()
            if cell.seen and revision <= cell.revision:
                # ORDERED BY REVISION, never by arrival: a baseline and a held event can
                # reach here in either order and seq cannot order them.
                return
            cell.value = value
            cell.revision = revision
            cell.seq = seq
            cell.seen = True
        loop = self._loop
        if loop is None or self._priming:
            # CACHED, NOT PUSHED. See ``_priming``: this is the subscribe-time baseline,
            # which is the value the first load is about to put in its own response.
            return
        try:
            loop.call_soon_threadsafe(self._arm, fold)
        except RuntimeError:
            # The loop is closed, which happens while the gateway shuts down. A push
            # nobody can receive is not worth reporting.
            logger.debug("dashboard push for %r/%s arrived after the loop closed", self.slug, fold)

    # -- event loop ---------------------------------------------------------- #

    def _arm(self, fold: str) -> None:
        """Start the send for *fold*. On the loop."""
        loop = self._loop
        if loop is None or self._closed:
            return
        task = loop.create_task(self.send(fold))
        task.add_done_callback(_report_failure)

    async def send(self, fold: str) -> bool:
        """Send one frame for *fold*. True when a frame went out. On the loop.

        The ORDER of the three decisions is the protocol: is this page still the
        crewmate's at all, has its layout moved, and only then which blocks heard the
        fold. Deciding the blocks first would compute values under a Model the stored
        package has already replaced.
        """
        if self._closed:
            return False
        reread = await asyncio.to_thread(self._reread, self.slug)
        fresh_package, fresh = reread if reread is not None else ({}, None)
        if fresh is None or str(getattr(fresh, "bound_to", "")) != self.binding:
            # The package is gone, or bound somewhere else now. A rebind writes no
            # version (the package line's ruling), so it is invisible to the layout
            # check and has to be caught here. The frame carries NO values: a page that
            # is not this crewmate's must not be told one more number.
            self.close()
            return self._refetch(REASON_UNBOUND)
        if int(getattr(fresh, "version", 0) or 0) != self.layout or str(
            getattr(fresh, "layout_fingerprint", "")
        ) != str(self.model.layout_fingerprint):
            # BOTH, together. The package is what `block_patch` narrows against and the
            # Model is what says which fields exist; replacing one and keeping the other
            # would patch a new layout's blocks with an old layout's field set.
            self.model, self.package = fresh, fresh_package
            # AND THE SUBSCRIPTIONS GO WITH THEM. They belong to the Model just
            # replaced, and the refetch below REUSES this page: `open_page` finds its
            # layout and fingerprint matching the fresh Model, and `ensure_subscribed`
            # returns early while `_disposers` is non-empty. A fold the new layout adds
            # would then never be subscribed and its fields would read missing for as
            # long as the page stayed open. Dropped here so the refetch's own read
            # subscribes again, under the Model it is about to serve.
            self.unsubscribe()
            return self._refetch(REASON_LAYOUT)
        patch = self.patch(fold)
        if patch is None or not patch.get("blocks"):
            # NOTHING SUBSCRIBES, so nothing is sent and the counter does not move. A
            # frame here would spend a version on a page that has nothing to apply.
            return False
        self._version += 1
        self._broadcast(
            {
                "slug": self.slug,
                "dashboard": str(getattr(self.model, "slug", "") or ""),
                "version": self._version,
                "layout": self.layout,
                "fold": fold,
                # WHICH BLOCKS MOVED, and which of their fields. Derived FROM the patch
                # rather than computed beside it, so the frame cannot name one set of
                # blocks while the payload carries another.
                "blocks": {
                    block_id: sorted(entry.get("fields", {}))
                    for block_id, entry in patch["blocks"].items()
                },
                "missing": list(patch.get("missing", ())),
                # WHAT D3 FORWARDS INTO THE IFRAME, VERBATIM. Built by the renderer's
                # own `block_patch`, which is also the half the document has a listener
                # for -- so the narrowing, the formatting and the message type all live
                # in Python and no payload is constructed in TS.
                "patch": patch,
                "refetch": False,
                "reason": "",
            }
        )
        return True

    def patch(self, fold: str) -> dict[str, Any] | None:
        """The renderer's block-patch payload for the blocks *fold* moved, or ``None``.

        ``None`` means no patch builder was injected, so there is nothing to narrow with
        -- the state a page constructed without ``patch_seam`` is in, which is a test's
        page and never the controller's (it passes the renderer's ``block_patch`` on
        every path). Reported rather than
        worked around: the only workaround is to re-send the whole read, and the page's
        own full-paint listener re-initialises every block, which appends a second
        canvas to a 3D block and leaves two scenes animating over each other. That is
        the bug ``block_patch`` and its idempotent painters exist to avoid, and shipping
        a path that reintroduces it would be worse than not pushing at all.

        REDACTED VALUES GO IN, so the formatted strings ``block_patch`` builds with
        ``display_values`` come out of already-masked input. The redaction is still the
        controller's and still one call over the whole mapping.

        The NARROWING is the renderer's too: ``block_patch`` keeps only the fields each
        block actually renders, so a patch cannot put a value on a block the view never
        placed it on. Passing the moved field names and letting it resolve the blocks
        itself keeps that rule in one place rather than re-deriving it here.
        """
        build = self._patch_seam
        if build is None:
            return None
        package = self.package
        if not package:
            return None
        moved, missing, seq, stale = self._moved(fold)
        if not moved:
            return None
        try:
            # BOTH SIDES OF THE BUILDER. The values go in masked, and the payload comes
            # out masked too, because `block_patch` formats each one through
            # `display_values` -- which reads the field's SPEC, agent-authored package
            # content the redactor has never seen. A `unit` or an `enum` choice holding
            # a secret would otherwise ride out in the patch's display string with the
            # raw value beside it already masked. See :meth:`read` for the same rule.
            return self._redact(
                build(
                    package,
                    self._redact(moved),
                    seq=seq,
                    stale=stale,
                    missing=missing,
                )
            )
        except Exception:
            logger.warning("dashboard push: %r's block patch could not be built", self.slug)
            return None

    def _moved(self, fold: str) -> tuple[dict[str, Any], list[str], int, bool]:
        """``(values, missing, seq, stale)`` for the fields *fold* feeds.

        Only this fold's fields, because only they moved. ``missing`` and ``stale``
        describe the WHOLE page rather than this fold, because the page's band is about
        the page: a cell left unresolved by another fold is still older than the record.
        """
        cells = self._snapshot()
        moved: dict[str, Any] = {}
        seq = 0
        for name, spec in self.model.fields.items():
            if self._fold_of(spec) != fold:
                continue
            resolved = self._value_of(name, cells)
            if resolved is MISSING:
                continue
            moved[name] = resolved
            seq = max(seq, self._seq_of(name, cells))
        whole = self.read()
        return moved, list(whole.get("missing", ())), seq, bool(whole.get("stale"))

    def _refetch(self, reason: str) -> bool:
        """Ask the page to read the whole thing again. Carries no values, by design."""
        self._version += 1
        self._broadcast(
            {
                "slug": self.slug,
                "dashboard": str(getattr(self.model, "slug", "") or ""),
                "version": self._version,
                "layout": self.layout,
                "fold": "",
                "blocks": {},
                "missing": [],
                "patch": {},
                "refetch": True,
                "reason": reason,
            }
        )
        return True

    def _broadcast(self, data: dict[str, Any]) -> None:
        """Hand one frame to the hub. Never raises: a push must not cost the loop."""
        broadcast = getattr(self._state, "broadcast_ws_owners", None)
        if broadcast is None:
            return
        try:
            broadcast(BLOCK_FRAME, data)
        except Exception:
            logger.debug("dashboard push for %r failed", self.slug, exc_info=True)

    # -- values -------------------------------------------------------------- #

    def blocks(self) -> tuple[dict[str, list[str]], list[str]]:
        """``(blocks, missing)`` -- the WHOLE page's blocks and which fields each holds.

        THE FIRST LOAD'S SHAPE, and the only one: every block, because the first load
        paints every block. A push narrows instead -- and it narrows in the renderer,
        in ``dashboard_package_render.blocks_reading``, which :meth:`patch` reaches
        through ``block_patch``. There is deliberately no second narrowing here: two
        would be two spellings of which block renders which field, and the renderer's
        is the one the document is built from.

        NAMES, NOT VALUES, and that is the whole point. Every value on this page lives
        once, in :meth:`read`, where it is redacted and formatted by the renderer's own
        ``display_values``. A copy here would be a second home for the same number --
        and an UNFORMATTED one, so whoever read it would show a bare ``1200000000``
        where the first load showed ``1.2 GB``. One home, one spelling of the format
        rules, and that spelling is the renderer's.

        Safe to carry as keys: a block id and a field name are both matched against a
        restricted grammar by the package gate (``[a-z][a-z0-9_-]{0,63}``), so nothing
        here is agent free text and nothing here needs redacting.

        A field whose path does not resolve is NAMED in ``missing``: the page dims that
        cell, and it deliberately gets no value and no display string -- an empty string
        would render as a filled cell holding nothing, which is worse than dimmed.
        """
        model = self.model
        cells = self._snapshot()
        out: dict[str, list[str]] = {}
        missing: list[str] = []
        for block_id in getattr(model, "subscriptions", {}):
            names: list[str] = []
            for name in model.subscriptions[block_id]:
                if self._value_of(name, cells) is MISSING:
                    if name not in missing:
                        missing.append(name)
                    continue
                names.append(name)
            out[block_id] = names
        return out, missing

    def read(self) -> dict[str, Any]:
        """This page's whole read, in the shape the DOCUMENT accepts. Redacted.

        ``dashboard_frame.read_payload`` builds it, which is the point: the v2 page, the
        package renderer's data island and this refill are then one shape, and a reader
        cannot be shown a first paint and a refill that describe the same read
        differently.

        COMPLETE, not restricted to the pushed blocks, and that is forced by the
        document: its message listener does ``read = data.read``, replacing what it
        holds, so a partial read would blank every cell outside the pushed blocks. Which
        blocks actually moved is carried separately, in the frame's ``blocks``. If the
        document's listener is changed to MERGE, this can be narrowed to the pushed
        fields and the saving is real; until then sending everything is the correct
        answer rather than the cheap one.

        ``display`` is the renderer's OWN formatter over these same values, so there is
        exactly one spelling of "a number with a unit and a precision" and it is not in
        page JS. A page constructed with no ``display_seam`` sends no ``display`` and the
        document falls back to a plain ``String(value)`` -- an unformatted number a
        reader can SEE is unformatted, rather than one formatted by a second set of
        rules that drifted.

        ORDER IS LOAD-BEARING: ``display`` is computed from the REDACTED fields, never
        from the raw ones. A string formatted before the redactor ran would carry
        exactly what the redactor exists to mask, past a reader who was looking at the
        masked value right beside it. The redaction still happens in ONE call over the
        whole mapping, so the property that there is a single chokepoint is kept.

        A field in ``missing`` gets neither a value nor a display string, because
        ``display_values`` only formats names present in ``fields``.
        """
        model = self.model
        cells = self._snapshot()
        fields: dict[str, Any] = {}
        missing: list[str] = []
        unresolved: list[str] = []
        written_at: dict[str, str] = {}
        seq = 0
        agentic = set(model.agentic_fields()) if hasattr(model, "agentic_fields") else set()
        for name in model.fields:
            resolved = self._value_of(name, cells)
            if resolved is MISSING:
                missing.append(name)
                # STALE is about a fold-backed cell only. An agentic field the crewmate
                # has never written is ABSENT, and counting it would raise the band
                # forever about a value nothing had yet produced.
                if name not in agentic:
                    unresolved.append(name)
                continue
            fields[name] = resolved
            if name in agentic:
                at = self._written_at(name, cells)
                if at:
                    written_at[name] = at
            else:
                seq = max(seq, self._seq_of(name, cells))
        payload = self._read_payload(
            fields=self._redact(fields),
            agentic=sorted(agentic),
            seq=seq,
            stale=bool(unresolved),
            missing=sorted(missing),
            written_at={name: str(self._redact(at)) for name, at in written_at.items()},
            locale=self.locale,
        )
        display = self._display_values(model, payload.get("fields", {}))
        if display is not None:
            # REDACTED AFTER FORMATTING, not only before it. The fields going in are
            # already masked, but a formatted string is built from the field's SPEC as
            # well as its value, and the spec is agent-authored package content that no
            # redactor has seen: `format_value` appends a number's `unit` and renders an
            # `enum` against its `choices`. So a secret written into a unit or a choice
            # would reach a socket inside the display string with the raw value beside
            # it already masked. The output crosses the redactor too.
            payload["display"] = self._redact(display)
        return payload

    @staticmethod
    def _read_payload(**kwargs: Any) -> dict[str, Any]:
        """``dashboard_frame.read_payload``, imported where it is used.

        Deferred rather than at module scope for the boot-path rule the whole handler
        follows: the frame module pulls the template manifest and the projection in.
        """
        from kiro_crew import dashboard_frame

        return dashboard_frame.read_payload(**kwargs)

    def _display_values(self, model: Any, fields: Mapping[str, Any]) -> dict[str, str] | None:
        """The renderer's own formatter over *fields*, or ``None`` when none was injected.

        Reached through the CONTROLLER's seam rather than imported, for the same reason
        ``redact`` and ``reread`` are handed in: this module must not be the second place
        that decides what a dashboard's values are formatted, masked or read by. The
        controller passes the renderer's ``display_values`` on every path, so ``None``
        here is a page a test built without one.
        """
        getter = self._display_seam
        if getter is None:
            return None
        try:
            return getter({"model": {"types": dict(model.fields)}}, fields)
        except Exception:
            logger.debug("dashboard push: the display formatter failed", exc_info=True)
            return None

    def _snapshot(self) -> dict[str, tuple[Any, bool, int]]:
        with self._lock:
            return {name: (cell.value, cell.seen, cell.seq) for name, cell in self._cells.items()}

    @staticmethod
    def _fold_of(spec: Any) -> str:
        """WHICH FOLD advancing moves this field, or ``""`` when nothing does.

        THE ONE PLACE THAT MAPPING LIVES, because an agentic field does not spell it the
        way a fold-backed one does: its source is exactly ``{"agentic": True}`` and
        carries no ``fold`` key at all (``dashboard_package._validate_source`` allows no
        other shape). The crewmate's own write is what moves it, and that write lands on
        :data:`AGENTIC_FOLD` -- which this page subscribes to and caches. Read the
        ``fold`` key alone and ``send(AGENTIC_FOLD)`` matches no field, so a crewmate's
        own value is cached and never pushed; and ``_seq_of`` looks up the cell under
        ``""``, so such a field's patch carries ``seq=0``.
        """
        source = spec.get("source") if isinstance(spec, Mapping) else None
        if not isinstance(source, Mapping):
            return ""
        if source.get("agentic") is True:
            return AGENTIC_FOLD
        return str(source.get("fold") or "")

    def _seq_of(self, name: str, cells: Mapping[str, tuple[Any, bool, int]]) -> int:
        cached = cells.get(self._fold_of(self.model.fields.get(name)))
        return int(cached[2]) if cached is not None else 0

    def _written_at(self, name: str, cells: Mapping[str, tuple[Any, bool, int]]) -> str:
        """When an agentic cell was written, from the FOLD's own row.

        Taken only where the value was taken, so the two cannot disagree about which
        write they describe -- a stamp inside the value would be the writer's own claim
        about its own freshness.
        """
        cached = cells.get(AGENTIC_FOLD)
        if cached is None or not cached[1]:
            return ""
        row = resolve_path(cached[0], f"fields.{name}")
        at = row.get("at") if isinstance(row, Mapping) else None
        return at if isinstance(at, str) else ""

    def _value_of(self, name: str, cells: Mapping[str, tuple[Any, bool, int]]) -> Any:
        """One field's value from the cached folds, or :data:`MISSING`.

        An agentic field is read from the agentic fold's own row, which is where the
        crewmate's write landed and where its ``at`` stamp lives. A fold-backed field
        walks its declared path into its fold's value. The two cannot be confused: the
        Model's ``source`` says which, and it is required on every field.
        """
        spec = self.model.fields.get(name)
        if not isinstance(spec, Mapping):
            return MISSING
        source = spec.get("source")
        if not isinstance(source, Mapping):
            return MISSING
        if source.get("agentic") is True:
            cached = cells.get(AGENTIC_FOLD)
            if cached is None or not cached[1]:
                return MISSING
            row = resolve_path(cached[0], f"fields.{name}")
            if not isinstance(row, Mapping) or "value" not in row:
                return MISSING
            return row["value"]
        fold = str(source.get("fold") or "")
        cached = cells.get(fold)
        if cached is None or not cached[1]:
            return MISSING
        return resolve_path(cached[0], str(source.get("path") or ""))


def _report_failure(task: "asyncio.Task[Any]") -> None:
    """Log a push task that raised. Without this the exception is only ever a warning
    from the loop's default handler, with no slug in it."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("dashboard push task failed", exc_info=exc)


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #

#: The live pages, oldest first. LRU by arming, bounded at :data:`MAX_LIVE_PAGES`.
_PAGES: "OrderedDict[str, LivePage]" = OrderedDict()
_PAGES_LOCK = threading.Lock()


def open_page(
    slug: str,
    member: str,
    model: Any,
    *,
    slot: str,
    state: Any,
    loop: asyncio.AbstractEventLoop | None,
    redact: Callable[[Any], Any],
    reread: Callable[[str], Any],
    locale: str = "",
    display_seam: Callable[..., Any] | None = None,
    patch_seam: Callable[..., Any] | None = None,
    package: Mapping[str, Any] | None = None,
) -> LivePage:
    """Arm -- or refresh -- the live push for one crewmate's package page.

    IDEMPOTENT for an unchanged page: a second read of the same dashboard re-points the
    existing page at the loop and hub now serving and returns it, rather than building a
    second set of subscriptions. Two sets would write to two counters, and the browser
    cannot tell two senders apart -- it would see the versions interleave and refetch on
    every frame.

    REPLACED when the member, the binding or the layout differ, because then the blocks
    this page would push are not the blocks the document on screen is holding.
    """
    with _PAGES_LOCK:
        existing = _PAGES.get(slug)
        reusable = (
            existing is not None
            and not existing._closed
            and existing.member == member
            # THE SLOT IS PART OF IDENTITY, not a detail: it is what the subscriptions
            # are keyed by. A crewmate moved onto its own memory store keeps its slug,
            # its member, its binding and its layout, and reads a DIFFERENT DM slot --
            # reusing the page there would keep subscriptions pointed at the old one.
            and existing.slot == slot
            and existing.binding == str(getattr(model, "bound_to", "") or "")
            and existing.layout == int(getattr(model, "version", 0) or 0)
            and existing.model.layout_fingerprint == model.layout_fingerprint
        )
        if reusable and existing is not None:
            existing.bind(state, loop)
            # THE LATEST READER'S LANGUAGE, not the first one's. A push has no request
            # to ask, so it serves `self.locale`; left alone, a reader who switches UI
            # language and refetches is answered in the language whoever armed this page
            # was reading in. The reuse branch is where that reader is standing.
            existing.locale = locale
            if package:
                # Same layout by fingerprint, so this is the same package -- but the
                # read that produced it is the newer one, and a page armed before the
                # controller had a package at all would otherwise keep an empty one.
                existing.package = package
            _PAGES.move_to_end(slug)
            return existing
        page = LivePage(
            slug,
            member,
            model,
            slot=slot,
            state=state,
            loop=loop,
            redact=redact,
            reread=reread,
            locale=locale,
            display_seam=display_seam,
            patch_seam=patch_seam,
            package=package,
        )
        if existing is not None:
            existing.close()
        _PAGES[slug] = page
        _PAGES.move_to_end(slug)
        evicted: list[LivePage] = []
        while len(_PAGES) > MAX_LIVE_PAGES:
            _slug, old = _PAGES.popitem(last=False)
            evicted.append(old)
    for old in evicted:
        old.close()
    return page


def close_page(slug: str) -> None:
    """Stop pushing for *slug*. Idempotent."""
    with _PAGES_LOCK:
        page = _PAGES.pop(slug, None)
    if page is not None:
        page.close()
