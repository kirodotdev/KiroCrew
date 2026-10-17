"""The controller for a page its owner wrote: subscribed fields in, the ones that moved out.

A ``kind="dashboard"`` package says WHAT its page may be sent -- ``model.types``, each
field with a fold and a path or marked as the owner's own -- and the page says how to
draw it. This module is the gate between the two, and it is the only thing that decides
a value reaches a browser.

Three rules, and each is a line of code rather than a convention:

1. **Only the model's fields.** :class:`LivePage` resolves every value through the
   manifest it builds from ``model.types``, so a field the model does not declare has no
   path to the page at all -- it is not in a frame, and the write gate already refuses a
   binding that names it.
2. **Only what moved.** The last value SENT for each field is kept here, and a frame
   carries the fields whose value differs from it. A fold advancing for a reason this
   page does not read costs one comparison and sends nothing.
3. **A gap means a full read.** Every patch carries a version that steps by exactly one.
   The page checks it (:mod:`kiro_crew.dashboard_frame`) and asks for a whole read when
   it cannot place a patch in order; :meth:`LivePage.refetch` is that answer. A gateway
   restart is covered by the same rule, because the counter restarts at zero.

WHY THIS IS BUILT ON :class:`~kiro_crew.dashboard_feed.DashboardFeed` rather than beside
it. The feed already solves the hard half: one keyed bus subscription per fold with a
baseline that closes the join race, values ordered by REVISION (a slot fold's seq cannot
order two values of one key), a cached value per fold so a read is O(fields), and
staleness as a fact about a path that stopped resolving rather than a timer. All of that
is the same for a page out of a package as for a page out of a template -- what differs
is only WHICH document declares the fields, and that difference is one function here.

THE OWNER CHECK AND THE REDACTION ARE NOT HERE. ``redact`` is handed in by the route
that arms the page, which is where the binding is checked and where the masking pass
lives; this module cannot emit a value that did not cross it, because
:meth:`LivePage._values` is the only place values are produced and it calls it on every
path.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
import uuid
from typing import Any, Callable, Final, Mapping

from kiro_crew.dashboard_templates.manifest import FieldSpec, Shape, TemplateManifest

logger = logging.getLogger(__name__)

__all__ = [
    "AGENTIC_FOLD",
    "FIELD_FRAME",
    "LivePage",
    "package_manifest",
]

#: The WS message type a field frame is sent under.
#:
#: A new TYPE on the owner channel, deliberately not a second channel: a socket of its
#: own would be a second place to get the owner gate, the backpressure and the reconnect
#: right, and those are the three this surface cannot afford to get wrong twice.
FIELD_FRAME: Final[str] = "dashboard_fields"

#: The slot fold a page's own written values live in, keyed by field name.
#: The fold a page's own written values live in. Spelled here as well as in
#: :mod:`kiro_crew.dashboard_feed`, which subscribes it: this module may not import that
#: one at module scope -- the gateway imports this file before the socket binds -- and
#: ``test_the_two_spellings_of_the_agentic_fold_agree`` fails if the two ever drift.
AGENTIC_FOLD: Final[str] = "agentic"

#: A package data type, as the manifest's own JSON-shaped vocabulary spells it.
#:
#: TWO vocabularies, because they answer different questions and both are load-bearing:
#: the package's type says what a value MEANS (a timestamp, one of a fixed set of
#: labels), and the manifest's says what shape it has on the wire. Mapping rather than
#: unifying them keeps the package able to say "timestamp" -- which a page formats
#: differently from a plain string -- while the feed keeps one notion of shape.
_TYPE_MAP: Final[Mapping[str, str]] = {
    "number": "number",
    "text": "string",
    "timestamp": "string",
    "enum": "string",
    "bool": "boolean",
    "array": "array",
    "object": "object",
}


def _tree_fold_names() -> frozenset[str]:
    """The folds keyed by a TREE rather than by a slot or a session.

    Read off the projection kernel's own set rather than listed here, so a tree fold
    added there is handled without an edit. Imported lazily: this module is reachable
    from the dashboard read path and the kernel is not cheap.
    """
    from kiro_crew.crew_log.projection import TREE_PROJECTION_NAMES

    return frozenset(TREE_PROJECTION_NAMES)


def package_manifest(
    package: Mapping[str, Any], *, slug: str = "", version: int = 0
) -> TemplateManifest:
    """The package's ``model.types`` as the manifest the feed subscribes by.

    A TRANSLATION, not a second declaration: every field here comes from the stored
    package and nothing is added, so the folds this subscribes to are exactly the ones
    the owner's model names.

    Built directly rather than through ``parse_manifest``, which validates a template
    DIRECTORY's manifest.json -- an id matching a directory name, a positive version, a
    source in the registry's own set. A package has none of those and should not be made
    to invent them: the package's own validator has already checked every field, so a
    second parse would refuse valid packages for failing a template's metadata rules.

    *package* must be CANONICAL (the output of ``validate_package`` / ``parse_package``).
    """
    types = (package.get("model") or {}).get("types")
    fields: dict[str, FieldSpec] = {}
    if isinstance(types, Mapping):
        for name in sorted(types):
            spec = types[name]
            if not isinstance(spec, Mapping):
                continue
            source = spec.get("source")
            if not isinstance(source, Mapping):
                continue
            kind = _TYPE_MAP.get(str(spec.get("type") or ""), "string")
            if source.get("agentic") is True:
                # An `enum` field keeps its declared choices as the shape's `enum`, so
                # the write's own shape check refuses a value outside the catalog.
                choices = spec.get("choices") if spec.get("type") == "enum" else None
                shape = (
                    Shape(kind, enum=tuple(choices))
                    if isinstance(choices, list) and choices
                    else None
                )
                fields[name] = FieldSpec(name, kind, None, None, True, shape)
                continue
            fold = str(source.get("fold") or "")
            path = str(source.get("path") or "")
            if fold and path:
                fields[name] = FieldSpec(name, kind, fold, path, False, None)
    return TemplateManifest(
        id="dashboard-package",
        version=int(version),
        title=slug or "dashboard",
        description="a dashboard package's own model",
        # NOT a registry source. This manifest never reaches the catalog, and
        # `_trusted_page` gates the TEMPLATE render path on this field -- so naming a
        # renderable source here would be this translation vouching for a page the
        # repository did not ship. A package's page is trusted for a different reason:
        # its owner wrote it and a person approved it.
        source="package",
        fields=fields,
    )


class LivePage:
    """One open page out of one package: subscriptions in, changed fields out.

    Thread-safe by one lock around the sent-value table and the counter, which is the
    whole of the shared state. The fold callback runs on the crew log's FOLD WORKER
    thread, so it does no I/O: the values it needs are already cached in the feed, and
    *send* is expected to hand the frame to the loop that owns the sockets.
    """

    def __init__(
        self,
        slug: str,
        slot: str,
        package: Mapping[str, Any],
        send: Callable[[dict[str, Any]], None],
        *,
        session_unit: str = "",
        layout_version: int = 0,
        redact: Callable[[Any], Any] | None = None,
        locale: object = "",
    ) -> None:
        from kiro_crew.dashboard_feed import DashboardFeed

        self.slug = slug
        #: The artifact version of the layout these frames were computed under. A page
        #: rendered from an older one must not have values applied to it: the field set
        #: itself may have changed, so the frame says which layout it belongs to.
        self.layout_version = int(layout_version)
        self._package = package
        self._manifest = package_manifest(package, slug=slug, version=layout_version)
        self._agentic = frozenset(
            name for name, spec in self._manifest.fields.items() if spec.agentic
        )
        self._send = send
        self._redact = redact or (lambda value: value)
        #: The reader's UI language, carried on EVERY frame. A patch that omitted it
        #: would describe the same read in a different language from the first paint,
        #: and `dashboard_frame.page_locale` would answer English for all of them.
        self._locale = locale
        # REENTRANT, because the read below runs INSIDE this lock and `_resolve_trees`
        # takes it again for the tree cache. A plain lock would deadlock there, and
        # reading outside the lock is what let a stale snapshot win -- see `_full_read`.
        self._lock = threading.RLock()
        self._sent: dict[str, Any] = {}
        #: What the page's stale band was last told, so a change in it is a
        #: reason to send even when every value compares equal.
        self._sent_band: tuple[bool, tuple[str, ...]] = (False, ())
        self._version = 0
        #: This page's own counter, named so a reader can tell one counter's numbers
        #: from another's. Minted per instance because the version restarts with the
        #: instance: a route serving the same crewmate again replaces this object, and
        #: its first read carries version 1 while the tab may hold 7.
        self._generation = uuid.uuid4().hex[:12]
        self._armed = False
        self._trees: dict[str, Any] = {}
        #: The tree a tree-keyed fold is rooted at: this page's own slot. A tree fold
        #: walks what the record SAYS -- the root's units, the workers they bound, the
        #: boards those workers conduct, to closure -- so the root is the only key it
        #: takes and the page's own slot is it.
        self._root = slot
        self._tree_folds = frozenset(
            fold for fold in self._manifest.folds if fold in _tree_fold_names()
        )
        self._feed = DashboardFeed(slot, session_unit, on_change=self._on_fold)

    # -- arming ------------------------------------------------------------- #

    def arm(self) -> dict[str, Any]:
        """Subscribe, and return the FULL read the first paint is composed from.

        File I/O on this thread (the feed's baseline read), which is why a route calls
        it through ``asyncio.to_thread``.

        The ``agentic`` fold is subscribed whenever the model declares a field the owner
        writes, even though no field reads a path into it: its value IS the table of
        written values, and holding it in the feed is what lets a custom value change
        reach the page without a disk read on the fold worker.
        """
        unavailable = [
            fold
            for fold in self._feed.subscribe(self._manifest, bool(self._agentic))
            # A TREE fold reports unavailable BY DESIGN -- it has no bus scope -- so it
            # is not an omission worth logging beside a genuine one. Its value is read
            # here instead.
            if fold not in self._tree_folds
        ]
        self._read_trees()
        if unavailable:
            # REPORTED, never raised: one unsubscribable fold must not deny the page the
            # others, and the fields it feeds read as missing -- which the frame draws as
            # its stale band rather than as an error.
            logger.info(
                "dashboard %s: no subscription for fold(s) %s; their fields read as missing",
                self.slug,
                sorted(unavailable),
            )
        return self._full_read()

    def disarm(self) -> None:
        """Drop every subscription and forget what was sent. Idempotent."""
        self._feed.unsubscribe()
        with self._lock:
            self._sent.clear()
            self._armed = False

    def folds(self) -> frozenset[str]:
        """The folds this page is subscribed to."""
        return self._feed.folds()

    # -- reading ------------------------------------------------------------ #

    def _read_trees(self) -> None:
        """Re-read every TREE-keyed fold the model names. FILE I/O -- never on the folder.

        A tree fold has no bus scope and cannot have one: it joins the logs of many
        slots, so its value is stale when any member's log grows and there is no key an
        event could carry to say which. It is therefore COLD by design, read on a page
        load and on a refetch -- which the gap rule already covers, since a refetch is a
        full read anyway.

        A fold that cannot be read leaves its entry absent, so its fields read as
        missing and the page shows the band rather than a tree drawn from nothing.
        """
        from kiro_crew.crew_log import projection

        values: dict[str, Any] = {}
        for fold in self._tree_folds:
            try:
                values[fold] = projection.read_tree_projection(self._root, fold).value
            except Exception:
                logger.info(
                    "dashboard %s: the %s tree could not be read; its fields read as missing",
                    self.slug,
                    fold,
                )
        with self._lock:
            self._trees = values

    def _values(self) -> tuple[dict[str, Any], Any]:
        """Every subscribed field's current value, redacted, and the read beside it.

        The ONE place a value is produced, which is what makes the redaction
        unskippable: every frame and the first paint both come through here.
        """
        read = self._feed.read(self._manifest, self._feed.fold_value(AGENTIC_FOLD))
        if self._tree_folds:
            self._resolve_trees(read)
        values = {name: self._redact(value) for name, value in read.fields.items()}
        return values, read

    def _resolve_trees(self, read: Any) -> None:
        """Fill the tree-bound fields from the cached read, in place.

        The feed reported them missing, because it has no subscription for a fold with
        no scope. Resolved here from the cache :meth:`_read_trees` filled, and taken OUT
        of ``missing`` when they resolve -- a tree-bound page would otherwise fly the
        stale band permanently, which teaches a reader to ignore it.
        """
        from kiro_crew.dashboard_feed import MISSING, resolve_path

        with self._lock:
            trees = dict(self._trees)
        for name, spec in self._manifest.fields.items():
            fold = spec.fold or ""
            if fold not in self._tree_folds or fold not in trees:
                continue
            resolved = resolve_path(trees[fold], spec.path or "")
            if resolved is MISSING:
                continue
            read.fields[name] = resolved
            if name in read.missing:
                read.missing.remove(name)
        read.stale = bool(read.missing) and any(
            not self._manifest.fields[n].agentic for n in read.missing if n in self._manifest.fields
        )

    def _payload(self, fields: Mapping[str, Any], read: Any, version: int) -> dict[str, Any]:
        """One frame's ``read``, in the shape the frame's own builder produces."""
        from kiro_crew import dashboard_frame

        payload = dashboard_frame.read_payload(
            fields,
            agentic=sorted(self._agentic),
            seq=getattr(read, "seq", 0),
            stale=bool(getattr(read, "stale", False)),
            missing=sorted(getattr(read, "missing", ()) or ()),
            written_at={
                name: str(self._redact(at))
                for name, at in (getattr(read, "written_at", {}) or {}).items()
            },
            locale=self._locale,
        )
        payload["version"] = version
        # WHOSE COUNTER THAT NUMBER IS. The version steps per frame within one armed
        # page and starts again in the next one, so a page comparing two reads needs to
        # know they came from the same counter before it can call one of them older.
        payload["generation"] = self._generation
        return payload

    def _full_read(self) -> dict[str, Any]:
        """Every field, as a read that REPLACES whatever the page holds.

        No ``patch`` marker, so the page replaces rather than merges -- which is the
        point of a full read: it is the answer to "I cannot place your patches", so
        keeping a field it does not carry would leave the page drawing a value that the
        model does not declare.
        """
        with self._lock:
            # THE READ HAPPENS INSIDE THE LOCK, with the version minted from the same
            # critical section. Capturing values first and locking afterwards is a lost
            # update: a fold advancing in between makes `_on_fold` send the NEWER
            # fields, and this older snapshot then takes the next version and is
            # accepted as the latest -- so the page silently goes backwards with no gap
            # for it to detect. Holding the lock across the read is what makes "the
            # values and the version describe one instant" true rather than likely.
            values, read = self._values()
            # A PATCH NUMBER IS SPENT ON A PATCH, and a full read is not one.
            #
            # A patch reaches every owner socket; a refetch answer reaches only the tab
            # that asked. Stepping the counter here spent a number the other tabs never
            # saw, so each of them read the next push as a gap and refetched -- and each
            # refetch put the others behind again, re-reading the tree folds under the
            # page lock every time. Two tabs on one package, or one tab plus the 60s
            # fallback read that re-arms the page, is enough to start it.
            #
            # So a full read is stamped with the CURRENT number: it replaces whatever
            # the page holds rather than following it, so it needs no number of its own,
            # and the next patch is still the successor of what every tab last saw. The
            # first arm is the exception below -- there is no current number yet.
            if not self._armed:
                self._version += 1
            self._sent = dict(values)
            # ARMED HERE, in the same lock as the table it compares against. The feed's
            # baseline is delivered synchronously inside ``subscribe``, so a fold
            # callback runs before this read finishes -- and a patch sent then would be
            # addressed to a page that does not exist yet, while stepping the version
            # the page is about to be handed. Nothing is lost by dropping it: the
            # baseline value is in the feed's cache and this read takes it.
            self._armed = True
            version = self._version
        return self._payload(values, read, version)

    def refetch(self) -> dict[str, Any]:
        """The full read a page asks for when it cannot place a patch in order.

        The counter is NOT reset. It keeps stepping, so the patch after this one is
        checked against the version this read carries -- a reset would make the next
        patch look like a gap and the page would ask again, forever.

        The tree folds are re-read here, which is the OTHER half of why a refetch
        exists: nothing can push a tree, so a refetch is the only moment its value
        moves. File I/O, so a route calls this through ``asyncio.to_thread``.

        THE READ, THE CACHE AND THE VERSION ARE ONE CRITICAL SECTION. Two refetches can
        be in flight at once -- a page asks on every gap it sees -- and with the tree
        read outside the lock the older of the two can finish last, replace ``_trees``
        with what it saw, and then be stamped with the higher version. The page accepts
        that as the latest and draws a tree from before the newer one, with
        ``stale: false`` beside it. Holding the lock across both is what makes the
        values and the version describe one instant, which is the same reason
        ``_full_read`` holds it across its own read.

        The lock is a reentrant one and ``_full_read`` takes it again, which is the
        other reason it has to be: this is the second nesting, after ``_resolve_trees``.
        """
        with self._lock:
            self._read_trees()
            return self._full_read()

    def first_frame(self) -> dict[str, Any]:
        """The frame a freshly armed page is painted from. Alias of a full read."""
        return self._full_read()

    # -- the push ----------------------------------------------------------- #

    def _on_fold(self, fold: str) -> None:
        """A subscribed fold moved. Runs on the crew log's FOLD WORKER thread.

        Does the comparison and hands the frame to *send*. No I/O: the feed already
        holds the folded value, and a read is O(fields).

        A fold that moved for a reason this page does not read sends NOTHING, which is
        the ordinary case: a work ledger advances on every worker report and a page
        binding one field of it redraws only when that field's value differs.
        """
        with self._lock:
            if not self._armed:
                return
            try:
                values, read = self._values()
            except Exception:  # pragma: no cover - a failed read must not cost the folder
                logger.debug("dashboard %s: could not read after %s moved", self.slug, fold)
                return
            moved = {
                name: value
                for name, value in values.items()
                if name not in self._sent or self._sent[name] != value
            }
            # A FIELD THAT STOPS RESOLVING IS NEWS TOO, and it moves no value.
            #
            # `DashboardFeed.read` DROPS a field whose fold path does not resolve, so
            # its last value stays in `self._sent` and nothing compares unequal. A
            # comparison of values alone therefore sends nothing, and the page keeps
            # drawing that value with its stale band hidden -- reporting a number as
            # current at the moment the product cannot read it. So the band's own
            # inputs are part of what "moved".
            band = (
                bool(getattr(read, "stale", False)),
                tuple(sorted(getattr(read, "missing", ()) or ())),
            )
            if not moved and band == self._sent_band:
                return
            self._sent_band = band
            self._sent.update(moved)
            self._version += 1
            version = self._version
        frame = self._payload(moved, read, version)
        # A PATCH: it carries what moved and says nothing about what did not, so the
        # page merges it onto what it holds and fires only the subscribers for these
        # fields.
        frame["patch"] = True
        try:
            self._send(frame)
        except Exception:  # pragma: no cover - a failed send must not cost the folder
            logger.debug("dashboard %s: the frame after %s could not be sent", self.slug, fold)


def render_package_page(
    package: Mapping[str, Any], read: Mapping[str, Any], *, scripts: bool = True
) -> str:
    """The package's own page, framed by the host's contract. The body of one dashboard.

    :func:`kiro_crew.dashboard_frame.compose_body` and nothing else, because the frame
    contract is what makes an agent-written page safe to run: the bindings are filled as
    TEXT so a value can never become markup, ``connect-src 'none'`` closes egress, and
    the host owns the document around the page. Composing this surface any other way
    would be a second frame to get those three right.

    *read* is a payload from :meth:`LivePage.first_frame` or
    :func:`~kiro_crew.dashboard_frame.read_payload`, so the first paint and every refill
    describe one read in one shape.

    Raises :class:`~kiro_crew.dashboard_frame.PageError` for a page the frame refuses --
    which a stored package cannot normally be, because the write gate asked the same
    question while its author was still present. Asked again here for the one case that
    gate cannot cover: content written under an older validator.
    """
    from kiro_crew import dashboard_frame

    html = ((package.get("view") or {}).get("html")) or ""
    dashboard_frame.validate_page(html)
    # WITH NO SCRIPT, NOTHING ELSE FILLS THE CELLS. The bootstrap is what sets a
    # binding's text, so a page mounted without it has every bound cell EMPTY while
    # its stale band stays hidden -- an empty page that reads as a current one.
    # Filled here instead, ESCAPED, so this path keeps the same promise the
    # assignment makes: a value can never become markup.
    #
    # AFTER `validate_page`, so what is validated is the author's own markup and not
    # markup this fill produced -- a value is text either way, but the order means the
    # validator's answer is about the page that was written.
    if not scripts:
        html = dashboard_frame.fill_bindings_as_text(html, read)
    return dashboard_frame.compose_body(html, read)


#: What the script-grant audit calls a page that is staged and has no artifact of its own.
#: Not a slug the store can produce, so a package of any name keeps its own audit entry.
_STAGED: Final[str] = "(staged)"


def authored_html(package: Mapping[str, Any]) -> str | None:
    """The page an agent wrote as ``view.html``, or ``None`` for a block layout.

    A block layout has no script of its own, so there is nothing to run.
    """
    view = package.get("view")
    html = view.get("html") if isinstance(view, Mapping) else None
    return html if isinstance(html, str) and html else None


def verify_package_page(
    slug: str,
    crew: str,
    slot: str,
    package: Mapping[str, Any],
    deadline: float | None = None,
) -> dict[str, Any] | None:
    """Run an authored page once over a SYNTHETIC copy of *crew*'s read; the result in wire form.

    ``None`` when the package draws from blocks. Otherwise the wire form of a
    :class:`~kiro_crew.dashboard_verify.Verification`, which says ``passed``, ``failed``
    with each error and where it was thrown, ``timed_out`` or ``skipped`` with the reason.

    The page is staged and no person has approved it, so it is never given the record: the
    read is taken here exactly as the tab is served it (:meth:`LivePage.arm` over the same
    manifest, redaction and folds, then disarmed), and ``dashboard_verify`` copies it into
    a record of the same shape with invented values and runs the page over that. *package*
    must be CANONICAL, like :func:`package_manifest`'s.

    A page the tab would mount WITHOUT script is not run: an owner whose egress is
    restricted has asked that the page's script not run at all (the tab's own decision,
    ``member_dashboard._egress_decision``), so there is nothing to check. The decision is
    audited in both directions, like the tab's. A session that is not a crewmate (*crew*
    is empty) has no tab and no owner to ask, so its page is not run.

    The page is checked as the tab is served it: its text goes through the route's own
    secret filter first (``member_dashboard._template_text_safe``, and the model's strings
    through ``_manifest_text_safe``), and when that changed the page the result says how many
    places and where the first is, without saying what was replaced.

    What it returns has crossed ``member_dashboard._page_safe``, the pass the tab's values
    cross, because a page's errors are text the page chose.

    The cheap tests come first (:func:`~kiro_crew.dashboard_verify.preflight`: not Linux,
    a check already running, a machine short of memory), so a save that will be skipped
    reads nothing. *deadline* is the ``time.monotonic()`` instant the check must be over by.

    Blocking (fold reads, a browser start), so a route calls it through
    ``asyncio.to_thread``. Never raises: a save must not fail because a check could not.
    """
    html = authored_html(package)
    if html is None:
        return None
    # Deferred: this module is imported before the socket binds and must not pull in
    # the browser runner, and `member_dashboard` imports this module (a cycle).
    from kiro_crew import dashboard_verify
    from kiro_crew.dashboard.handlers import member_dashboard

    def skipped(reason: str) -> dict[str, Any]:
        return dashboard_verify.Verification(dashboard_verify.STATUS_SKIPPED, reason=reason).wire()

    if not crew:
        return skipped("only a crewmate's own page is run, and this session is not a crewmate")
    reason = dashboard_verify.preflight()
    if reason:
        return skipped(reason)
    try:
        key = member_dashboard.governance_session_key(slot)
        decision = member_dashboard._egress_decision(crew, key)
        member_dashboard._audit_page_script_grant(
            member=crew, package_slug=_STAGED, version=0, session_key=key, decision=decision
        )
        if not decision.granted:
            return skipped(
                "the page is shown without script under this crewmate's network policy, so "
                "its script never runs and there is nothing to check"
            )

        # THE TEXT THE TAB IS SERVED. The route runs the page's markup and script through
        # the secret filter before it composes the document, and a script that holds a long
        # run of letters and digits can come out of it changed and break on the gateway
        # alone. These are the route's own two functions, so the check cannot drift from
        # it; `rewrite_note` tells the agent that the page it wrote is not the page it got.
        shown = member_dashboard._template_text_safe(html)
        note = dashboard_verify.rewrite_note(html, shown)

        def read_once() -> Mapping[str, Any]:
            page = LivePage(
                slug,
                slot,
                package,
                lambda _frame: None,
                session_unit=member_dashboard._newest_unit(slot),
                redact=member_dashboard._page_safe,
            )
            try:
                return page.arm()
            finally:
                page.disarm()

        verdict = dashboard_verify.verify_page(
            shown,
            read_once,
            model=member_dashboard._manifest_text_safe(package.get("model")),
            deadline=deadline,
        )
        if note:
            verdict = dataclasses.replace(verdict, notes=(note, *verdict.notes))
        # REDACTED LIKE EVERY VALUE THE TAB IS SERVED. The page's own script chose the text of
        # its errors, and it can build a string that no scan of its source saw (two literals
        # joined at run time), so what goes back to the agent crosses the same recursive pass.
        return member_dashboard._page_safe(verdict.wire())
    except Exception:
        logger.warning("dashboard: the page for %r could not be checked", slug, exc_info=True)
        return skipped("the check could not run on this machine")


def writing_caller(request: Any) -> tuple[str, str]:
    """``(crew, slot)`` this request writes as, each ``""`` when it cannot be shown.

    Resolved from the SESSION's own agent binding, never from the body: a
    body-supplied name would let one crew write a page that presents as another's,
    and whose page a reader is looking at is the one thing this surface guarantees.

    The crew is taken from the allocation's selection rather than from ``slot.agent``,
    because those answer different questions -- a session that selected the provider
    TEMPLATE of a member's name carries the identical string, so reading the name as a
    crew binding would let such a session write the page of the crew it happens to
    share a name with. Only a selection whose namespace is ``member`` counts.

    ``("", "")`` for anything unresolvable, which :func:`binding_refusal` answers as a
    refusal. That direction is deliberate: a gate whose default is "no identity, no
    objection" is not a gate.
    """
    try:
        from kiro_crew.dashboard.handlers.agent_panel import (
            _live_session_key,
            _normalize_slot_key,
        )

        state = request.app["state"]
        sk = request.headers.get("X-Session-Key", "")
        if not sk or sk == "dashboard:ui":
            return "", ""
        slot = _normalize_slot_key(sk)
        crew = ""
        session_key = _live_session_key(state, sk)
        if session_key:
            try:
                namespace, selected = state.sessions.get_agent_selection(session_key)
            except (ValueError, AttributeError):
                namespace, selected = "", ""
            if namespace == "member":
                crew = str(selected or "")
        return crew, slot
    except Exception:
        logger.debug("dashboard: the writing caller could not be identified", exc_info=True)
        return "", ""


def dashboard_package_of(text: Any) -> Mapping[str, Any] | None:
    """*text* as a dashboard package, or ``None`` when it is not one.

    CLASSIFIED BY DECODING, never by looking for a word in the submitted bytes. A
    substring test on the raw text is bypassable with JSON's own escapes --
    ``{"kind":"dash\\u0062oard"}`` carries no literal ``dashboard`` and decodes to one --
    so a gate keyed on the bytes is skipped while the store happily commits the package.
    The decoded ``kind`` is the only spelling there is.

    The one cheap pre-filter is NOT escapable: a JSON document begins with ``{``, and no
    escape can hide that, so prose and markdown are turned away without a parse while
    every possible package still reaches one.

    One classifier for two questions: the owner gate below asks it whose page this is,
    and the artifact write path asks it whether the write needs the stale-write check
    that keeps a rebind from being overwritten. Two spellings of "is this a package"
    could disagree, and the one that said no would be the hole.
    """
    from kiro_crew.artifact_store.dashboard_package import (
        DASHBOARD_KIND,
        ArtifactValidationError,
        parse_package,
    )

    if not isinstance(text, str):
        return None
    if not text.lstrip().startswith("{"):
        return None
    try:
        package = parse_package(text)
    except (ArtifactValidationError, ValueError):
        # Not a package, or not a valid one. The store's own gate answers an invalid
        # package with its own reason, and reporting an ownership refusal for a parse
        # failure would tell the author to go away rather than to fix the JSON.
        return None
    return package if package.get("kind") == DASHBOARD_KIND else None


#: A stored record that names the dashboard kind and whose binding cannot be read.
#: Distinct from "not a package", which has no owner to protect, because these two need
#: opposite answers: one is a write the gate has nothing to say about, the other is a
#: write it cannot authorize.
BINDING_UNPROVABLE: Final[str] = "\x00unprovable"


def stored_package_binding(text: Any) -> str | None:
    """The ``bound_to`` a STORED record carries, read off its envelope.

    ``None`` means the slug holds no dashboard record, so there is no stored owner for
    the write gate to protect. A string is the binding to enforce.
    :data:`BINDING_UNPROVABLE` means the record IS one and its binding could not be
    read, which the gate refuses rather than skips.

    OWNERSHIP IS NOT A PROPERTY OF THE LAYOUT. Reading the stored binding by PARSING
    the package ties it to whether the layout still validates, and those are different
    facts: a package stored before a validator grew stricter still belongs to whoever it
    names. Classifying such a record as "not a package" drops the stored half of the
    gate, and the submitted half is a claim the caller wrote -- so a worker could
    overwrite and rebind a crewmate's page by sending one bound to itself.

    This is :func:`~kiro_crew.artifact_store.dashboard_package.resolve_bound_slug`'s
    rule, for the same reason it gives: the binding is matched off the stored ENVELOPE,
    so a layout the current validator rejects is still FOUND and still has an owner.
    """
    if not isinstance(text, str) or not text.lstrip().startswith("{"):
        return None
    try:
        raw = json.loads(text)
    except (ValueError, TypeError):
        return None
    except RecursionError:
        # A quarter-megabyte of ``[`` is JSON-shaped and deeper than the decoder's
        # stack. ``RecursionError`` is a ``RuntimeError``, so the arm above does not
        # see it, and letting it escape turns every later PATCH of that slug into a
        # 500 -- including the one replacing the content with plain text, so the
        # owner cannot undo it.
        #
        # ``None``, not :data:`BINDING_UNPROVABLE`. Unprovable is for a record that IS
        # a dashboard whose binding cannot be read, and this text cannot be one: the
        # write path decodes a package through
        # ``dashboard_package._decode_package``, which refuses exactly this depth, so
        # no stored dashboard record is this deep. Answering "unprovable" would refuse
        # every write to an ordinary markdown document that happens to start with
        # ``{``, which is this feature charging another kind for its rule.
        return None
    from kiro_crew.artifact_store.dashboard_package import DASHBOARD_KIND

    if not isinstance(raw, Mapping) or raw.get("kind") != DASHBOARD_KIND:
        return None
    bound_to = raw.get("bound_to")
    # A STRING IS REQUIRED, not merely a truthy value: `binding_refusal` compares it,
    # and a number or a list would compare unequal to every crew and read as "bound to
    # nobody" -- which is the one answer a record naming this kind may not get.
    if not isinstance(bound_to, str) or not bound_to.strip():
        return BINDING_UNPROVABLE
    return bound_to


def package_write_refusal(request: Any, content: Any, stored_content: Any = None) -> str:
    """``""`` when this request may write *content* as a dashboard package; else why not.

    THE OWNER GATE'S CALLER, shared by every write path so the rule is asked once:
    the artifact routes (``artifact_update`` / ``artifacts_create``, which a plain
    ``artifact_update`` and the dashboard tool path both reach through the store) and
    any later save / apply route.

    A BROWSER CALL IS NOT GATED HERE and that is not a hole: it carries no internal
    secret, the artifact routes already refuse a non-owner caller, and the person at
    the dashboard IS the owner of every page on their own machine. The caller this
    exists for is an agent -- a worker that could write the package could write the
    page, and the page runs script inside the frame holding the owner's fold values.

    Content that is not a dashboard package answers ``""`` -- this is asked on every
    artifact write, and refusing a prose document for having no binding would break
    every other kind -- UNLESS *stored_content* is one, which is the overwrite case: a
    slug holding somebody's dashboard is theirs whatever the caller submits for it.

    *stored_content* is what the slug holds now. A caller that passes none is
    authorizing the submitted binding alone, which is a claim the caller wrote.
    """
    from kiro_crew.artifact_store.dashboard_package import binding_refusal

    incoming = dashboard_package_of(content)
    # THE STORED SIDE IS CLASSIFIED FIRST, and off the envelope, because whether this
    # gate has anything to say depends on it: a stored package the current validator
    # rejects still has an owner, and treating it as "no package" would leave the
    # submitted binding -- a claim the caller wrote -- as the only thing checked.
    stored = stored_package_binding(stored_content)
    if incoming is None and stored is None:
        return ""
    # AN APP IS REFUSED, and only now that the content is known to be a package: this
    # gate is asked on EVERY artifact write, and an app saving a note is its own
    # business. An app token carries no internal secret, so the person's exemption
    # below would otherwise hand it the same pass -- and the artifact routes admit an
    # app on its own claim (``_non_owner_refusal`` returns early for ``request["app"]``),
    # so nothing else between the token and the store asks whose page this is. Shipped
    # apps already hold ``/api/artifacts`` grants and write through that route.
    #
    # Refused rather than binding-checked: an app has no crewmate identity to compare
    # against a binding, so there is no binding under which its write is authorized.
    #
    # AN APP IS ITS ID, a non-empty STRING the token middleware stamps. Any truthy
    # value read as one: a request object carrying something else in that slot was
    # refused as an app, which is the broad answer standing in for an identification
    # this gate never actually made.
    app_id = request.get("app")
    if isinstance(app_id, str) and app_id:
        return (
            "an app may not write a dashboard package: a page runs script beside the "
            "crewmate's own fold values, so only the crewmate or session its "
            "'bound_to' names may write one"
        )
    # THE PERSON at the dashboard, who owns every page on their own machine and whom
    # the artifact routes have already owner-gated. Reached only after the app check
    # above, which is what makes this a positive identification of the person rather
    # than "whoever sent no secret".
    if request.headers.get("X-Internal-Secret") is None:
        return ""
    crew, slot = writing_caller(request)
    # BOTH BINDINGS ARE AUTHORIZED, and the STORED one first. Checking only the
    # submitted binding authorizes the caller against a claim the caller wrote: a
    # worker sends a package bound to itself, passes, and overwrites the crewmate whose
    # slug it addressed -- rebinding that crewmate's page to the worker in one write.
    # The stored binding is who the page belongs to NOW, so it is the one that decides
    # whether this slug may be written at all.
    # Classified above, before the app and person branches, because it decides whether
    # this gate applies at all. Only "no dashboard record here" may be skipped; a
    # record whose owner cannot be read is refused rather than passed over.
    if stored == BINDING_UNPROVABLE:
        return (
            "that slug holds a dashboard package whose owner could not be read, so "
            "this write cannot be authorized against it; nothing was written -- repair "
            "the package's 'bound_to' first"
        )
    if stored is not None:
        refused = binding_refusal(stored, crew=crew, slot=slot)
        if refused:
            return refused
    if incoming is not None:
        refused = binding_refusal(incoming.get("bound_to"), crew=crew, slot=slot)
        if refused:
            return refused
    return ""
