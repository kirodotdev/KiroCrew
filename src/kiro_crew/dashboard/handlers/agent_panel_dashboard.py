"""The dynamic dashboard's agent routes, under ``/api/agent-panel/dashboard/``.

Fields, types, write, save, templates, preview, apply and rollback, with what they share:
resolving the caller to the one instance it may reach, reading that instance and its
history, and the refusals a write records in the crewmate's crew log.

:mod:`kiro_crew.dashboard.handlers.agent_panel` is this module's facade: callers and tests
import and patch every name through it, and a write there -- ``monkeypatch.setattr``,
``mock.patch`` -- reaches this module too (:mod:`kiro_crew._facade_mirror`). This module
never imports the facade.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Mapping
from typing import Any, Final

from aiohttp import web

from kiro_crew import agent_panel, dashboard_agentic, dashboard_package, dashboard_types
from kiro_crew.artifact_store import dashboard_package as _dashboard
from kiro_crew.artifact_store.model import ArtifactValidationError
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection
from kiro_crew.dashboard.handlers.agent_panel_publish import _resolve_publishing_crew
from kiro_crew.dashboard.handlers.agent_panel_read import _panel_slot
from kiro_crew.dashboard.handlers.dashboard_page_checks import (
    MANUAL_FIELDS_NOTE,
    page_rendering,
    shape_warnings,
    skipped_note,
    theme_warnings,
)
from kiro_crew.dashboard.handlers.members import _member_names_for_slug
from kiro_crew.dashboard.handlers.session_ledger import _session_unit
from kiro_crew.dashboard.state import DashboardState, _normalize_slot_key
from kiro_crew.session_ledger import _APPEND_FLUSH_SECONDS
from kiro_crew.validation import (
    DASHBOARD_SAVE_SCHEMA,
    DASHBOARD_WRITE_SCHEMA,
    ValidationError,
    validate_tool_args,
)

logger = logging.getLogger("kiro_crew.dashboard.handlers.agent_panel")


# -------------------------------------------------------------------------- #
# the dynamic dashboard's agent surface (contract v3, part 6)
# -------------------------------------------------------------------------- #


def read_instance(slug: str, member: str) -> dashboard_agentic.Instance | None:
    """The crewmate's dashboard instance, or ``None`` when it has none to write to.

    THE SEAM TO THE REGISTRY, and the one place this file depends on a store it does
    not own. ``dashboard_templates.instance.read`` is the reader and
    ``GET /api/members/{slug}/dashboard`` serves the same record to the frame, so
    this surface and the page agree about which manifest is in force without a
    second source of truth.

    Resolved through the MODULE rather than imported at the top, which keeps the
    boot-path rule (this handler is reached deferred) and lets a test replace this
    one function with a stub.

    It narrows the record deliberately. The store's own ``Instance`` carries the
    html and the state; this returns only the parsed manifest and the version,
    because an agent tool that could read its own page's markup is one that could
    be talked into reporting it, and the state is the frame's branch, not a
    writer's.

    ``live`` and ``stale`` resolve to the stored copy and ``empty`` to the DEFAULT, the
    page that crewmate is actually shown; only ``error`` and a missing default -- a
    crewmate with no page named for it, or a registry without one -- resolve to
    ``None``, which the write path reports as ``no_instance``. The test is whether a
    field name can be checked against a manifest a reader can trust. A stale copy HAS
    its own manifest, complete and still renderable, and the write is checked against
    the same manifest the page is drawn from; what a stale copy cannot do is be
    compared against its source, which says nothing about a field the crewmate
    declares. An ``empty`` record has no copy of its own, but the page it renders has a
    manifest all the same -- the default's -- and validating against that is what makes
    the written value appear on the page the reader is looking at. ``error`` is the one
    state where no manifest parses, so there is nothing to validate against and
    refusing is the only honest answer.

    An absent registry is NOT one of those states and is not caught here. The
    registry ships in the same package as this surface, so a build without it is a
    packaging fault, and swallowing the ``ImportError`` would report that fault as
    ``no_instance`` -- sending every crewmate to adopt a template through a registry
    the build does not have.
    """
    from kiro_crew.dashboard_templates import instance as instance_store
    from kiro_crew.dashboard_templates.manifest import ManifestError, parse_manifest

    try:
        record = instance_store.read(slug)
    except Exception:
        logger.warning("the dashboard instance for %s could not be read", slug, exc_info=True)
        return None
    if getattr(record, "package_slug", ""):
        # THE CREWMATE'S OWN PACKAGE IS IN FORCE, so its fields are the ones listed:
        # the same Model the write is checked against (`read_package_model`). Asked
        # only when the record pins a package, so a template page costs no library scan.
        try:
            package = dashboard_package.read_package_model(slug)
        except dashboard_package.PackageReadError:
            logger.warning("the dashboard package for %s is unreadable", slug, exc_info=True)
            return None
        return package.model
    # A STALE copy is writable and a LIVE one is: `_state_of` calls a stale copy
    # complete and still renderable, and the write is validated against the COPY's own
    # manifest, which a stale copy still has. What a stale copy cannot do is be
    # compared against its source, and that says nothing about a field the crewmate
    # declares and writes.
    #
    # EMPTY resolves to the DEFAULT, because the default is the page that crewmate is
    # actually being shown: `api_member_dashboard` falls back to `default_instance` for
    # exactly this state, so refusing here made the two paths disagree about which
    # template is in force. The disagreement is not academic -- the conductor's board
    # is the page named for it, never adopted, and the goal-conductor skill writes its
    # `needs_you` field whenever the user must act, so refusing EMPTY would refuse every
    # such write. A crewmate with no page named for it has no default, so its write is
    # refused here as it is shown no page there.
    #
    # ERROR stays refused on its own terms: it is the state in which the manifest does
    # not parse, so there is nothing to validate a write against.
    live = getattr(instance_store, "STATE_LIVE", "live")
    stale = getattr(instance_store, "STATE_STALE", "stale")
    empty = getattr(instance_store, "STATE_EMPTY", "empty")
    state = getattr(record, "state", "")
    if state == empty:
        try:
            fallback = instance_store.default_instance(slug)
        except Exception:
            # No default to fall back to: the registry is unreadable or ships none. A
            # refusal here is the honest answer, and it is the one case where
            # `no_instance` still describes the world.
            logger.warning("no default dashboard for %s to write against", slug, exc_info=True)
            return None
        if fallback is None:
            # `default_instance` ANSWERS None for a crewmate with no page named for it
            # and when the registry ships no default; it does not raise, so the guard
            # above does not cover this.
            return None
        record = fallback
    elif state not in (live, stale):
        return None
    try:
        manifest = parse_manifest(dict(record.manifest))
    except (ManifestError, TypeError, ValueError):
        # A record the store called live whose manifest will not parse here is a
        # disagreement between two readers, not a crewmate's mistake. Logged for the
        # operator and reported as no instance, so the write is refused rather than
        # checked against a manifest this process could not read.
        logger.warning("the dashboard manifest for %s does not parse", slug, exc_info=True)
        return None
    return dashboard_agentic.Instance(
        manifest=manifest, instance_version=int(record.instance_version)
    )


def _mistake_book(slot: str) -> dict[str, Any]:
    """The crewmate's folded mistake book, or an empty one.

    Read through the ordinary slot-keyed projection -- the same warm kernel the
    panel fold uses -- and NEVER by refolding a log, which the contract forbids
    twice. TOTAL: a damaged or absent fold reads as an empty book, because a
    crewmate with no readable mistakes is in the same position as one that has made
    none, and failing a write because its mistake book would not load would be the
    book costing the feature it exists to improve.
    """
    try:
        value = projection.read_slot_projection(slot, dashboard_agentic.MISTAKES_FOLD).value
    except Exception:
        logger.warning("the mistakes fold is unreadable for slot %s", slot, exc_info=True)
        return {}
    return value if isinstance(value, dict) else {}


async def _resolve_dashboard_caller(
    request: web.Request, operation: str
) -> tuple[tuple[str, str, str], None] | tuple[None, web.Response]:
    """Vet the caller and resolve it to ``(slug, crew_name, slot)``.

    Through the SAME gate the publish route uses, so the dashboard surface inherits
    every refusal that one earns: the internal-secret requirement, the app-caller
    denial, the restricted-session block, the operator switch, and the rule that
    the crew comes from the session's own binding and never from the body.

    The SLOT comes back too, because both handlers need it -- one to read the
    mistake book, the other to append -- and resolving it twice would load the
    config twice and could answer differently if it changed in between.

    AND the slug must be this crew's alone. An instance is one directory per slug,
    and slugification is lossy, so two configured names that differ only in case --
    ``Oncall`` and ``oncall`` -- resolve to one directory: either crew could then
    replace the other's staged or installed page, and a rollback by one would
    restore a version the other wrote. The browser's own resolver refuses that slug
    outright (``member_dashboard._resolve``, ``dashboard_slug_ambiguous``); this
    surface reaches the same files through MCP and therefore owes the same refusal.
    It is asked HERE rather than per route so a route added later inherits it.
    """
    resolved, refusal = await _resolve_publishing_crew(request, operation, root_session=True)
    if refusal is not None:
        return None, refusal
    assert resolved is not None
    slug, crew_name = resolved
    if not crew_name:
        # A root session's own page. Its slot is the caller's, and the store key was
        # derived from it, so no slug can be shared and there is no owner to ask about.
        sk = request.headers.get("X-Session-Key", "")
        return (slug, "", _normalize_slot_key(sk)), None

    def _owners_then_slot() -> tuple[list[str], str]:
        # ONE config load for both answers, and the OWNERS question first: an
        # ambiguous slug is one `_member_thread_slot` refuses outright, so resolving
        # the slot before asking would answer a 409 case with a 500.
        cfg = KiroCrewConfig.load()
        owners = _member_names_for_slug(cfg, slug)
        if owners and owners != [crew_name]:
            return owners, ""
        return owners, _panel_slot(cfg, crew_name, slug)

    owners, slot = await asyncio.to_thread(_owners_then_slot)
    # EMPTY is allowed and is not the ambiguous case. A publishing crew need not be
    # a configured MEMBER -- `_member_names_for_slug` lists addressable members, and
    # `_resolve_publishing_crew` has already vetted a caller that is not one -- so an
    # empty list means no configured member claims this slug, not that somebody else
    # does. What is refused is a slug a configured name OTHER than this crew resolves
    # to, which is both the case-collision pair and the plain wrong-owner case.
    if owners and owners != [crew_name]:
        return None, web.json_response(
            {
                "error": (
                    "more than one crew resolves to this dashboard, so a write here "
                    "would land on a page that is not only yours; ask the human to "
                    "rename one of them"
                ),
                "code": "dashboard_slug_ambiguous",
            },
            status=409,
        )
    return (slug, crew_name, slot), None


async def api_dashboard_fields(request: web.Request) -> web.Response:
    """GET /api/agent-panel/dashboard/fields -- the fields, and the mistake book.

    The read that makes an agent's first write usually correct. It is on the
    strict-internal prefix with the publish routes because it reports a crewmate's
    own refused writes, which is state about that crewmate and nobody else's to
    read.

    The reads after the gate run in ONE worker hop. Each ``asyncio.to_thread`` call
    queues on the gateway's shared default executor, so a busy gateway charges every
    hop its queue wait before the work starts. The reads are sequential anyway --
    each later one needs the instance the first returns -- so one hop pays that
    wait once instead of once per read.
    """
    resolved, refusal = await _resolve_dashboard_caller(request, "dashboard_fields")
    if refusal is not None:
        return refusal
    assert resolved is not None
    slug, crew_name, slot = resolved

    def _read() -> dict[str, Any]:
        instance = read_instance(slug, crew_name)
        mistakes = _mistake_book(slot) if slot else {}
        # The version history rides along on THIS read rather than having a tool of
        # its own. An agent about to answer "go back" already has to make this call to
        # know its fields, the rows are capped at ten, and a second round trip for ten
        # rows is a cycle spent on a payload that fits in this one.
        rows, retained = _instance_history(slug, slot)
        values, written_at = _current_values(slug, crew_name, instance, "" if crew_name else slot)
        # The upkeep rows read the shipped catalog (every built-in directory).
        return dashboard_agentic.fields_for_agent(
            instance,
            mistakes,
            history=rows,
            rollback_versions=retained,
            values=values,
            written_at=written_at,
            slug=slug,
        )

    # How a page draws the types a text cell shows raw, on the read an agent makes
    # before it writes or rebuilds a page.
    return web.json_response({**await asyncio.to_thread(_read), "rendering": page_rendering()})


async def api_dashboard_types(request: web.Request) -> web.Response:
    """GET /api/agent-panel/dashboard/types -- the data-type catalog, READ ONLY.

    What a dashboard block can be bound to: every fold the crew log registers, its
    field shape, the fold the value comes from, and how far that fold has been folded
    for THIS caller. Nothing is written, nothing is staged, and the catalog of shapes
    is the same for every caller -- the per-caller half is only the seq numbers.

    On the strict-internal prefix with its siblings, and through the same
    ``_resolve_dashboard_caller`` gate, for two reasons. The seqs are facts about this
    crewmate's own logs, so they are no more public than its mistake book. And the
    route must resolve a slot at all to read them.

    A fold that could not be read comes back as ``folded_through: null`` rather than
    ``0``: zero is a fold that has consumed nothing, which is a different answer, and a
    composing agent that cannot tell them apart would read an unreadable fold as an
    empty one and bind a block to it.
    """
    resolved, refusal = await _resolve_dashboard_caller(request, "dashboard_types")
    if refusal is not None:
        return refusal
    assert resolved is not None
    _slug, _crew_name, slot = resolved
    state: DashboardState = request.app["state"]
    unit = _session_unit(state, request.headers.get("X-Session-Key", ""))
    reached = await asyncio.to_thread(_folded_through, slot, unit)
    return web.json_response({**dashboard_types.describe(reached), "rendering": page_rendering()})


def _folded_through(slot: str, unit: str) -> dict[str, int | None]:
    """How far each registered fold has been folded, for *slot* and *unit*.

    Two reads, not fourteen: the session-keyed folds come back from ONE
    ``fold_session`` pass over the unit's log, and each slot-keyed fold is a warm memo
    lookup on an eager fold that walks nothing.

    What the session pass costs depends on what is already on disk. It is called with no
    ``since``, so it resumes from the unit's newest savepoint and walks only the entries
    past it -- and for a unit that has none, which is every unit this route reaches
    before its first eager fold lands, it walks the log from the beginning. That is one
    pass for all seven session folds either way, and it is bounded by the log rather than
    by the number of folds.

    Every omission is deliberate and each is ``None`` rather than ``0``:

    * ``radar`` is :data:`~kiro_crew.crew_log.projection.OWNER_SERVED_SLOT_PROJECTION`
      -- its owner orders the slot's units itself and a generic read would serve part
      of the record as the whole, so this route does not make one;
    * a caller with no slot, or whose slot has no live unit, has nothing to read the
      session folds against;
    * a fold whose read RAISES is logged and reported unread, because a catalog of
      shapes is still worth serving when one seq cannot be fetched.
    """
    reached: dict[str, int | None] = {name: None for name in projection.FOLD_NAMES}
    if unit:
        try:
            bundle = projection.fold_session(unit, projection.SESSION_FOLD_NAMES)
            for name, checkpoint in bundle.checkpoints.items():
                # The CHECKPOINT's seq, not ``bundle.projection(name).seq``: both
                # answer the same number, and this one reads it without rendering
                # fourteen values the caller asked nothing about.
                reached[name] = checkpoint.last_seq
        except Exception:
            logger.warning("dashboard types: session folds for %s are unreadable", unit)
    if slot:
        for name in projection.SLOT_PROJECTION_NAMES:
            if name == projection.OWNER_SERVED_SLOT_PROJECTION:
                continue
            try:
                reached[name] = projection.read_slot_projection(slot, name).seq
            except Exception:
                logger.warning("dashboard types: the %s fold for %s is unreadable", name, slot)
    return reached


def _current_values(
    slug: str,
    crew_name: str,
    instance: dashboard_agentic.Instance | None,
    session_slot: str = "",
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """The page's current field values, read the way the page reads them.

    *session_slot* is set for a root session's own page, whose folds are read from that
    slot rather than from a crewmate's DM slot.

    ``(None, {})`` when there is no instance or the read fails, so the listing still
    answers with the fields and their shapes rather than failing the whole read.
    """
    if instance is None:
        return None, {}
    try:
        from kiro_crew.dashboard.handlers.member_dashboard import read_fields

        if session_slot:
            read = read_fields(slug, crew_name, instance.manifest, slot=session_slot)
        else:
            read = read_fields(slug, crew_name, instance.manifest)
    except Exception:
        logger.warning("dashboard values for %s are unreadable", slug, exc_info=True)
        return None, {}
    return dict(read.fields), dict(read.written_at)


async def api_dashboard_write(request: web.Request) -> web.Response:
    """POST /api/agent-panel/dashboard/write -- one type-checked agentic value.

    The order of the steps is the feature:

    1. read the instance, so the check is against the manifest this crewmate is
       actually running rather than one the caller names;
    2. read the mistake book, so a refusal can say "you have done this before" and
       quote what worked;
    3. check the write -- and on a refusal, RECORD it and hand the sentence back;
    4. on success, append the value, then append the correction when this write
       answers a mistake the book was still holding.

    Step 3's recording is what makes the next cycle cheaper, and it happens before
    the response: an agent that is refused and then crashes has still taught its
    successor. Step 4's correction comes after the value lands, never before -- a
    correction for a write that failed to append would tell the crewmate a wrong
    field name was fixed by one that was never stored.
    """
    resolved, refusal = await _resolve_dashboard_caller(request, "dashboard_write")
    if refusal is not None:
        return refusal
    assert resolved is not None
    slug, crew_name, slot = resolved
    state: DashboardState = request.app["state"]
    sk = request.headers.get("X-Session-Key", "")
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_body"}, status=400
        )
    try:
        args = validate_tool_args(body, DASHBOARD_WRITE_SCHEMA)
    except ValidationError as exc:
        return web.json_response({"error": str(exc), "code": "validation_error"}, status=400)

    field = str(args.get("field") or "")
    value = args.get("value")
    # THE MODEL COMES FROM THE PACKAGE, through the one reader that knows where a
    # package lives. The check is against the Model this crewmate is actually running
    # rather than a builtin manifest a caller names -- which is the whole of the v3
    # change on this route, and why nothing else here reaches a template store.
    #
    # NO ARTIFACT SLUG IS PASSED. This route is keyed by the MEMBER, and `slug` here is
    # the member's own slug, not the package artifact's; handing it over as one would
    # name a package that does not exist. The reader resolves the artifact from the
    # member's binding, which is why its first argument is optional.
    try:
        package = await asyncio.to_thread(dashboard_package.read_package_model, slug)
    except dashboard_package.PackageReadError as exc:
        # NOT a refusal. A read that failed says nothing about the write, so there is
        # no lesson to record and no field to blame.
        logger.warning("dashboard package for %s is unreadable", slug, exc_info=True)
        return web.json_response({"error": str(exc), "code": "package_unreadable"}, status=503)
    mistakes = await asyncio.to_thread(_mistake_book, slot) if slot else {}
    owner_key = _writer_key(crew_name, slot)
    try:
        # The package's own state first, because every later question presumes a Model:
        # "is this a declared field" cannot be asked of a package that is absent,
        # broken, or another member's. Raised rather than returned so all four refusals
        # leave by one path and are recorded once, below.
        unusable = dashboard_agentic.package_refusal(package.state, package.state_reason, field)
        if unusable is not None:
            raise unusable
        # A Model that came from the PACKAGE is checked like any other: the page in
        # force draws that same package (`instance.pinned_package`, asked by both the
        # page read and `read_package_model`), so a value accepted here is one the
        # page can draw.
        entry = dashboard_agentic.check_write(package.model, field, value, mistakes)
    except dashboard_agentic.WriteRefused as refused:
        # RECORDED BEFORE THE RESPONSE. The refusal is already decided, so this
        # append costs the caller nothing it was going to get, and it is the only
        # reason the next cycle is cheaper than this one.
        recorded = dashboard_agentic.refusal_entry(refused, owner_key)
        await asyncio.to_thread(_record_refusal, state, sk, recorded)
        # The SAME scrubbed strings the entry carries. Reading them off `refused`
        # again would hand the caller the unscrubbed sentence and field name, so the
        # response and the record would disagree about what the refusal said.
        return web.json_response(
            {
                # The WHOLE scrubbed sentence, where the entry stores a 240-character
                # prefix of it. Same scrub and same wording, so the record and the
                # body cannot disagree about what the refusal said -- but the body is
                # not cut, because the tail is the remedy: the valid field names come
                # first and the "you have done this before; 'x' worked" note last, so
                # a manifest with about five fields pushes exactly the useful half
                # past the cap.
                "error": dashboard_agentic.refusal_sentence(refused),
                "code": recorded["code"],
                # Absent from the entry exactly when the refusal named no field, and
                # then `refused.field` is itself empty, so this carries no agent text.
                "field": recorded.get("field", refused.field),
            },
            status=400,
        )
    entry["crew_key"] = owner_key

    def _append() -> tuple[bool, bool]:
        unit = _session_unit(state, sk)
        if not unit:
            return False, False
        # ASKED BEFORE THE APPEND, like the panel route asks its own: an entry over
        # the log's whole-LINE ceiling can never land, so writing it would report a
        # value as stored that no fold will ever see.
        if not crew_log_emit.dashboard_entry_fits(dashboard_agentic.VALUE_ENTRY_TYPE, entry):
            return False, False
        if not crew_log_emit.on_dashboard_agentic(unit, entry, timeout=_APPEND_FLUSH_SECONDS):
            return False, False
        correction = dashboard_agentic.correction_entry(field, mistakes, owner_key)
        if correction is None:
            return True, False
        return True, crew_log_emit.on_dashboard_refused(
            unit, correction, timeout=_APPEND_FLUSH_SECONDS
        )

    if not crew_log_emit.enabled():
        # THE LOG IS THE DASHBOARD'S ONLY RECORD for an agentic value -- there is no
        # file beside it, because no host Python computes a dashboard value. So this
        # is a REFUSAL rather than the best-effort shrug the panel publish gives:
        # reporting success would promise a cell that will never fill.
        return web.json_response(
            {
                "error": (
                    "this gateway is not recording a crew log, and an agentic dashboard "
                    "value has nowhere else to live -- ask the human to switch the crew "
                    "log on"
                ),
                "code": "crew_log_off",
            },
            status=503,
        )
    stored, corrected = await asyncio.to_thread(_append)
    if not stored:
        return web.json_response(
            {
                "error": (
                    "the value could not be written to this crewmate's crew log; try "
                    "again on your next cycle"
                ),
                "code": "append_failed",
            },
            status=503,
        )
    # Tell an open dashboard a value changed. The fold's own bus event is what
    # drives the frame's refill; this frame is for a client watching the panel
    # surface, and it carries the SLUG only, like the publish broadcast.
    _broadcast_changed(state, "dashboard_value_written", slug, crew_name, slot)
    return web.json_response(
        {"ok": True, "written": {"field": field, "type": entry["type"]}, "corrected": corrected}
    )


def _writer_key(crew_name: str, slot: str) -> str:
    """The ownership digest a dashboard entry carries: the crew's, or the root session's."""
    return agent_panel.crew_key(crew_name if crew_name else f"session:{slot}")


def _broadcast_changed(
    state: DashboardState, msg_type: str, slug: str, crew_name: str, slot: str
) -> None:
    """Tell an open dashboard to re-read, without telling an app which slot changed.

    A crewmate's page is named by its SLUG and broadcast as before. A root session's
    page -- the caller bound to no crew -- is named by its SLOT, which is what the
    side panel holds; its store key is a digest no client can match. A slot key is
    hidden from an app socket unless it holds slot visibility, and these frames are
    scoped as global ``panels`` events, so the slot frame goes to owner sockets only.
    """
    if crew_name:
        state.broadcast_ws(msg_type, {"slug": slug})
    else:
        state.broadcast_ws_owners(msg_type, {"slot": slot})


# --------------------------------------------------------------------------- #
# Saving a composed page
# --------------------------------------------------------------------------- #


def _credential_shaped_names(package: Mapping[str, Any]) -> str:
    """Where this package declares a name a redactor would mask, or ``""``.

    ONE RULE, AND IT LIVES WITH THE READ MODULE. The scan itself is
    ``member_dashboard.package_declared_name_spots``, which this route asks to
    refuse the SAVE. Keeping the predicate in two places would be keeping two
    rules, and the first thing the second one would do is drift.

    It sits in the read's module rather than here because that is where the name
    rule belongs: a saved page is an artifact its owner can also write through the
    artifact API, so the save route is not its only writer and the rule must not be
    one this route privately owns. Imported inside the function, like
    ``read_fields`` above, because that module imports nothing from this one and
    the pair must not become a cycle.

    Why a refusal rather than a mask, what the three name sets are, and why a
    POSITION comes back instead of the name: see that function.
    """
    from kiro_crew.dashboard.handlers.member_dashboard import package_declared_name_spots

    return ", ".join(package_declared_name_spots(package))


def _page_binding(slug: str) -> str:
    """The ``bound_to`` for this caller's page, as the package spells it.

    Derived from the slug ``_resolve_dashboard_caller`` resolved and NEVER from
    the body. That resolution has already refused a caller with no dashboard
    slot, an app identity, a cookie-only request and a slug more than one crew
    answers to, so this string is the authorization decision in serialized form:
    it is what the page hangs off, and it is the key the save looks the existing
    page up by.

    TWO SCOPES, because the grammar has two. ``_BOUND_TO_RE`` accepts
    ``crewmate:<slug>`` or ``session:<slot key>``, and a ROOT SESSION's slug is
    already the second form -- ``instance.session_instance_key`` spells it
    ``session:<digest>``. So a session slug is returned AS IT STANDS and only a
    member slug takes the prefix. Prefixing both built
    ``crewmate:session:<digest>``, which the grammar refuses, and every
    root-session save answered 400 ``invalid_package`` with no way for the agent
    out: the argument schema refuses a caller-sent ``bound_to``, so the one key
    that would have corrected the binding is the one key it may not send.

    Keyed on the SLUG's own shape rather than on the absence of a crew name. The
    two agree -- a session resolves with no member behind it -- but the slug is
    what the grammar judges, and ``is_session_key`` is the function that answers
    for it, so the test here and the test there cannot drift apart.
    """
    from kiro_crew.dashboard_templates import instance as instance_store

    if instance_store.is_session_key(slug):
        return slug
    return f"crewmate:{slug}"


async def _await_check(work: Awaitable[dict[str, Any] | None]) -> dict[str, Any] | None:
    """Wait for the check for at most ``dashboard_verify.REPLY_SECONDS``.

    The one place a save waits on a check, so the order in which the wait and the check end
    can be set by a test instead of by a timer.
    """
    from kiro_crew import dashboard_verify

    return await asyncio.wait_for(work, timeout=dashboard_verify.REPLY_SECONDS)


async def _page_check(
    slug: str, crew_name: str, slot: str, package: dict[str, Any]
) -> dict[str, Any] | None:
    """The check of a saved page, which this save never waits on past its limit.

    The work (the record read, the copy, the browser) runs on a thread and holds the
    gateway's single check slot until it is over. The wait here ends at
    ``dashboard_verify.REPLY_SECONDS`` whatever the thread is doing: the reply says
    ``timed_out`` and the thread winds down on its own, still holding the slot, so no
    second check can start on top of it. A block layout has nothing to run and costs no
    thread.
    """
    from kiro_crew import dashboard_verify
    from kiro_crew.dashboard.handlers import member_dashboard_fields

    if member_dashboard_fields.authored_html(package) is None:
        return None
    deadline = time.monotonic() + dashboard_verify.RUN_TIMEOUT_SECONDS
    try:
        return await _await_check(
            asyncio.to_thread(
                member_dashboard_fields.verify_package_page,
                slug,
                crew_name,
                slot,
                package,
                deadline,
            )
        )
    except TimeoutError:
        return dashboard_verify.timed_out_wire()


#: Why a root session cannot save or apply a composed package: the hosted-page reader
#: resolves a crewmate's binding only, so such a page would be stored and never drawn.
_SESSION_PACKAGE_REFUSAL: Final[str] = (
    "a root session's Dashboard is a template page: pick one with dashboard_templates "
    "and dashboard_apply with its template_id. A composed package is a crewmate's own page"
)


def _dashboard_artifact_store() -> Any:
    """The artifact store the save writes the package into. A seam for tests."""
    from kiro_crew.artifacts import get_default_store

    return get_default_store()


async def api_dashboard_save(request: web.Request) -> web.Response:
    """POST /api/agent-panel/dashboard/save -- store the page the agent composed.

    The crewmate's own ``kind="dashboard"`` artifact is created, or updated when one
    is already bound to it. The page in force does not move: ``dashboard_apply`` puts
    the saved package live as a new instance version, with no preview step between
    (`rfc-crewmate-dynamic-dashboard.md`, "a page goes live on apply").

    The body carries the three declarations -- ``model``, ``view`` and ``theme`` --
    and the gateway supplies the other two keys of the package: ``kind``, which is
    fixed, and ``bound_to``, which is the authorization decision. A caller that sends
    either is refused by the argument schema. Because ``bound_to`` is derived from the
    vetted caller, this route only ever writes the caller's OWN package.

    The package passes the store's own write gate (``canonical_package_content``):
    the closed catalogs, the size cap, the lone surrogate scan and the depth refusal.
    The refusal comes back whole, naming the key path and the accepted values.

    A ROOT SESSION is refused: the hosted-page reader resolves a crewmate's binding
    only, so a package saved for a session would be stored and never drawn.

    A page written as ``view.html`` is also run once over a synthetic copy of the
    crewmate's data and the reply carries a ``verification`` block
    (``dashboard_verify``); the page stays saved, and not live, either way.
    """
    resolved, refusal = await _resolve_dashboard_caller(request, "dashboard_save")
    if refusal is not None:
        return refusal
    assert resolved is not None
    slug, crew_name, slot = resolved
    body = await _instance_body(request)
    if isinstance(body, web.Response):
        return body
    try:
        args = validate_tool_args(body, DASHBOARD_SAVE_SCHEMA)
    except ValidationError as exc:
        # Where a caller-sent ``bound_to`` or ``kind`` lands: a ToolSchema refuses
        # an unknown key by name, so the sentence tells the agent which key it may
        # not set. Reported as the validation failure it is rather than as a
        # package refusal, because the package was never built.
        return web.json_response({"error": str(exc), "code": "validation_error"}, status=400)
    if not crew_name:
        return web.json_response(
            {"error": _SESSION_PACKAGE_REFUSAL, "code": "session_package_unsupported"},
            status=400,
        )

    binding = _page_binding(slug)
    package = {
        "kind": _dashboard.DASHBOARD_KIND,
        "bound_to": binding,
        "model": args.get("model"),
        "view": args.get("view"),
        "theme": args.get("theme"),
    }
    where = _credential_shaped_names(package)
    if where:
        # REFUSED HERE, before anything is written, and by the page's own rule. The
        # read refuses a package whose declared names a redactor would rewrite, and
        # it refuses the WHOLE read, because a field name is a join key. So a save
        # that stored one would succeed and hand the crewmate a page that can never
        # render once a renderer exists -- the refusal keeps an unservable package
        # out of the store rather than letting it be saved and fail to render later.
        return web.json_response(
            {
                "error": (
                    "a dashboard package cannot declare a name a redactor would mask: "
                    f"{where} looks like a credential, and the page refuses a whole "
                    "read rather than rewriting such a name, because every name a "
                    "package declares is a JOIN KEY -- a field name joins the read's "
                    "fields and the page's cells, and a theme "
                    "token is the CSS variable the page's own style references. "
                    "Rename it to something a reader recognises -- a name is shown "
                    "to the person, so 'open_prs' and '--accent', not a token"
                ),
                "code": "unsafe_declared_name",
            },
            status=400,
        )

    def _save() -> tuple[Any, dict[str, int], dict[str, Any]]:
        from kiro_crew.dashboard_templates import instance as instance_store

        content = _dashboard.canonical_package_content(json.dumps(package, ensure_ascii=False))
        store = _dashboard_artifact_store()
        # ONE SAVE AT A TIME per crewmate, across the lookup and the write it decides:
        # two first saves would otherwise both find no package and create two with
        # the same binding, which every later save and apply refuses as ambiguous.
        with instance_store.package_save_lock(slug):
            art = _write_bound_package(store, content)
        parsed = _dashboard.parse_package(content)
        counts = {"fields": len((parsed.get("model") or {}).get("types") or {})}
        # The parsed canonical bytes go back too, for the page check below.
        return art, counts, parsed

    def _write_bound_package(store: Any, content: str) -> Any:
        """Update the package bound to the caller, or create it. Under the save lock."""
        existing = _dashboard.resolve_bound_slug(binding, store=store)
        if existing:
            # The store fences every dashboard write on the snapshot it read, so the
            # update names the read it is based on.
            current = store.get(existing, assign_token=True)
            # THE TOKEN-BEARING READ IS THE ONE AUTHORIZED: the record found by the
            # binding lookup may have been rebound before this read, and the store's
            # fence checks only the token. So this snapshot must itself still carry
            # the caller's binding, and the update is fenced on its token.
            try:
                held = _dashboard.parse_package(current.content or "").get("bound_to")
            except ArtifactValidationError:
                held = None
            if held != binding:
                raise ArtifactValidationError(
                    f"the dashboard package {existing!r} is not bound to you any more; "
                    "nothing was written -- save again"
                )
            return store.update(
                existing,
                content=content,
                actor="agent",
                snapshot=True,
                snapshot_token=current.content_token,
            )
        return store.create(
            name=f"{crew_name} dashboard",
            content=content,
            kind=_dashboard.DASHBOARD_KIND,
            description="This crewmate's own Dashboard page.",
        )

    try:
        art, counts, saved_package = await asyncio.to_thread(_save)
    except ArtifactValidationError as exc:
        # WHOLE, like a refused agentic write: the refusal names the key path and,
        # for a closed catalog, the values that would have been accepted. A binding
        # the library cannot resolve to one package lands here too.
        return web.json_response({"error": str(exc), "code": "invalid_package"}, status=400)
    except Exception as exc:
        logger.warning("a dashboard page for %s could not be saved", slug, exc_info=True)
        return web.json_response(
            {"error": f"the page could not be saved: {exc}", "code": "save_failed"},
            status=503,
        )

    # After saving, so a page that throws is still saved and one more save fixes it.
    verification = await _page_check(slug, crew_name, slot, saved_package)
    if isinstance(verification, dict) and verification.get("status") == "skipped":
        # A skipped run checked nothing: said in so many words, so the crewmate does not
        # read "verification" as a pass on a host with no browser.
        verification["note"] = skipped_note(str(verification.get("reason") or ""))
    sources = field_sources(saved_package)
    manual = [row["field"] for row in sources if row["source"] == "manual"]
    # Off the loop: both checks read the page text the crewmate wrote, up to the view cap.
    shapes, themes = await asyncio.to_thread(
        lambda: (shape_warnings(saved_package), theme_warnings(saved_package))
    )

    # NO BROADCAST: the page in force has not changed. `dashboard_apply` puts it live.
    return web.json_response(
        {
            "ok": True,
            "saved": {"package": art.slug, "package_version": int(art.version), **counts},
            # What the page raised over a synthetic copy of the record; None for a block layout.
            "verification": verification,
            # Every field with its type and where its value comes from, and the
            # hand-written ones named, so the crewmate sees which cells stay current only
            # while it keeps writing them.
            "fields": sources,
            "manual_fields": manual,
            **({"manual_note": MANUAL_FIELDS_NOTE} if manual else {}),
            # Checked here, by the gateway, because the page run above is skipped on a
            # host with no browser: a list bound to a text cell, a theme for one scheme.
            **({"shape_warnings": shapes} if shapes else {}),
            **({"theme_warnings": themes} if themes else {}),
            "next": "dashboard_apply puts this page live on your Dashboard as a new version",
        }
    )


def field_sources(package: Mapping[str, Any]) -> list[dict[str, str]]:
    """Each field the model declares: its type and source (``fold:<fold>.<path>`` or ``manual``).

    ``manual`` is a field the crewmate fills itself (``{"agentic": true}``); everything
    else names the crew-log fold the gateway pushes it from.
    """
    types = ((package.get("model") or {}).get("types")) or {}
    out: list[dict[str, str]] = []
    for name, spec in types.items():
        source = spec.get("source") if isinstance(spec, Mapping) else None
        if isinstance(source, Mapping) and source.get("fold") and not source.get("agentic"):
            path = str(source.get("path") or "")
            where = f"fold:{source['fold']}" + (f".{path}" if path else "")
        else:
            where = "manual"
        kind = spec.get("type") if isinstance(spec, Mapping) else None
        out.append({"field": str(name), "source": where, "type": str(kind or "")})
    return out


def _apply_own_package(slug: str, crew_name: str, slot: str) -> Any:
    """Put the caller's own saved package live: the apply half of ``dashboard_save``.

    The package is the one bound to the caller (``_page_binding``), so this only ever
    applies the caller's own page. Its CURRENT bytes are re-gated by the store's own
    ``canonical_package_content`` and kept as the gateway-owned snapshot the new
    instance version pins (``instance.apply_package``). Blocking file IO.
    """
    from kiro_crew.dashboard_templates import instance as instance_store

    if not crew_name:
        raise instance_store.InstanceRefused(_SESSION_PACKAGE_REFUSAL)
    store = _dashboard_artifact_store()
    # A crewmate's binding; a root session was refused above.
    existing = _dashboard.resolve_bound_slug(f"crewmate:{slug}", store=store)
    if not existing:
        raise instance_store.InstanceRefused(
            "nothing to apply: name a template_id from dashboard_templates, or save the "
            "page you composed with dashboard_save first"
        )
    loaded = store.get(existing)
    content = _dashboard.canonical_package_content(loaded.content or "")
    # The bytes applied are the ones checked: they must still name the caller.
    if _dashboard.parse_package(content).get("bound_to") != f"crewmate:{slug}":
        raise ArtifactValidationError(
            f"the dashboard package {existing!r} is not bound to you any more; nothing was applied"
        )
    return instance_store.apply_package(
        slug,
        package_slug=existing,
        package_version=int(loaded.version),
        content=content,
        session_id=_history_session(slot),
    )


def _record_refusal(state: DashboardState, sk: str, entry: dict[str, Any]) -> bool:
    """Append one refusal to the caller's own log. Best-effort, never raises.

    BEST-EFFORT unlike the value append, and the asymmetry is the point: by the
    time this runs the caller is already being refused, so a log that is off costs
    the mistake book this row and nothing else. Failing the request here would turn
    "we could not remember your mistake" into "your write was not refused", which
    is false.
    """
    if not crew_log_emit.enabled():
        return False
    try:
        unit = _session_unit(state, sk)
        if not unit:
            return False
        return crew_log_emit.on_dashboard_refused(unit, entry, timeout=_APPEND_FLUSH_SECONDS)
    except Exception:
        logger.warning("a refused dashboard write was not recorded", exc_info=True)
        return False


def _instance_history(slug: str, slot: str) -> tuple[tuple[dict[str, Any], ...], tuple[int, ...]]:
    """This instance's history rows and the versions a rollback can still reach.

    TOTAL, like the mistake book beside it: a history that will not load costs the
    rows and never the read. An agent that cannot see its history is in the position of
    one whose dashboard has none, and failing the fields read because of it would make
    the history cost the call it rides on.

    The two halves come from different places on purpose. The ROWS are the fold, capped
    at :data:`instance.MAX_HISTORY_ROWS`; the VERSIONS are the payloads still on disk,
    capped lower at :data:`instance.MAX_RETAINED_VERSIONS`. So the fold can name a
    version a rollback would refuse, and listing only what is retained is what keeps an
    agent from spending a cycle being told the page it asked for is gone.
    """
    from kiro_crew.dashboard_templates import instance as instance_store

    rows: tuple[dict[str, Any], ...] = ()
    retained: tuple[int, ...] = ()
    try:
        rows = instance_store.history(slug, session_id=_history_session(slot))
    except Exception:
        logger.warning("the dashboard history for %s is unreadable", slug, exc_info=True)
    try:
        retained = instance_store.versions(slug)
    except Exception:
        logger.warning("the retained dashboard versions for %s are unreadable", slug)
    return rows, retained


def _history_session(slot: str) -> str:
    """The session a dashboard change's history entry belongs to: the crewmate's DM log.

    The same derivation ``member_dashboard._write_session`` makes, from the slot this
    surface already resolved rather than from the member name -- resolving it twice
    would load the config twice and could answer differently if it changed between.

    "Newest" comes from the DURABLE succession chain and not from a header clock, for
    the reason that handler gives: a unit's ``createdAt`` is stamped once, so a clock
    that steps backward between two units of one slot would attribute every later
    change to a session that is already over.

    Empty when there is none, which is not a failure. The instance record is a file and
    is committed either way, so a crewmate whose DM thread has never run gets a working
    dashboard with no history row rather than a refused change.
    """
    if not slot:
        return ""
    try:
        from kiro_crew.crew_log.projection import units_in_succession

        units = units_in_succession(slot)
        return units[-1] if units else ""
    except Exception:
        logger.warning("could not resolve the history session for slot %s", slot, exc_info=True)
        return ""


def _instance_refusal(exc: Exception) -> web.Response:
    """One refused instance write, as the tool's caller reads it.

    A refusal carries the store's own sentence, which is written for an agent: it says
    what was wrong and what to do instead. An unexpected failure does NOT -- it carries
    a path or an errno -- so it answers 503 with a fixed sentence and the traceback goes
    to the log, where an operator reads it and a crewmate cannot.
    """
    from kiro_crew.dashboard_templates import instance as instance_store

    if isinstance(exc, instance_store.InstanceLocked):
        # Its own code: the agent's next step is to ask the person, not to retry or
        # change its arguments, and a client can only tell that apart by the code.
        return web.json_response({"error": str(exc), "code": "dashboard_locked"}, status=423)
    if isinstance(exc, instance_store.InstanceRefused):
        return web.json_response({"error": str(exc), "code": "refused"}, status=400)
    logger.warning("a dashboard instance call failed", exc_info=True)
    return web.json_response(
        {
            "error": (
                "this crewmate's dashboard store could not be written; try again on "
                "your next cycle"
            ),
            "code": "store_failed",
        },
        status=503,
    )


def _template_matches(listing: Mapping[str, Any], query: str) -> bool:
    """Whether one catalog listing answers *query*.

    The haystack includes the FOLD NAMES AND FIELD PATHS, not only the title and the
    description. A person asking for "the one that shows cost" is naming what they want
    to see, and the template that shows it is the one whose fields READ a usage fold --
    a word its author may never have put in the title. Matching only the prose would
    answer "there is none" for a template that does exactly the thing.

    Substring, case-folded, every whitespace-separated word required. Not a ranking:
    the catalog is a handful of templates, a chooser reads all of them, and a relevance
    score here would be a number nobody can explain to the person who asked.
    """
    haystack = " ".join(
        str(part).casefold()
        for part in (
            listing.get("id", ""),
            listing.get("title", ""),
            listing.get("description", ""),
            " ".join(str(f) for f in listing.get("fields", []) or ()),
            " ".join(str(f) for f in listing.get("folds", []) or ()),
            " ".join(str(p) for p in listing.get("paths", []) or ()),
        )
    )
    return all(word in haystack for word in query.casefold().split())


def _templates_for_agent(slug: str, query: str) -> dict[str, Any]:
    """The catalog as a chooser reads it, with this crewmate's current template marked.

    MARKED rather than filtered out. "Show me another" is answered by a list, and a
    list that silently omits the page already on screen makes the one thing the reader
    can verify -- that their current page is in there -- look like a missing template.

    ``problems`` is carried through from the scan. A directory that will not load is
    not this caller's fault and cannot be fixed by it, but a listing that silently drops
    it is how a template somebody saved stays invisible with nothing anywhere to read.
    """
    from kiro_crew.dashboard_templates import catalog
    from kiro_crew.dashboard_templates import instance as instance_store

    try:
        current = instance_store.read(slug).template_id
    except Exception:
        logger.warning("could not read %s's current template", slug, exc_info=True)
        current = ""
    found = catalog.list_templates()
    rows: list[dict[str, Any]] = []
    for entry in found.entries:
        listing = dict(entry.listing())
        # The paths its fields READ, which `listing()` does not carry: it reports the
        # fold names, and a path is the half that names the quantity ("usage.cost_usd")
        # rather than the fold it sits in. Added here, where the search needs it, rather
        # than widening every reader of a catalog listing.
        listing["paths"] = sorted(spec.path for spec in entry.manifest.fields.values() if spec.path)
        if query and not _template_matches(listing, query):
            continue
        listing["current"] = entry.id == current and bool(current)
        rows.append(listing)
    return {
        "templates": rows,
        "current_template_id": current,
        "query": query,
        "problems": [{"template": name, "problem": why} for name, why in found.problems],
    }


async def api_dashboard_templates(request: web.Request) -> web.Response:
    """GET /api/agent-panel/dashboard/templates?query= -- the catalog, searchable.

    On the strict-internal prefix with the rest of this surface. The listing itself is
    not a crewmate's data, but the body also says which template THIS crewmate is
    running, and that is state about this crewmate like its refused writes are.
    """
    resolved, refusal = await _resolve_dashboard_caller(request, "dashboard_templates")
    if refusal is not None:
        return refusal
    assert resolved is not None
    slug, _crew_name, _slot = resolved
    query = str(request.query.get("query") or "").strip()[:200]
    try:
        body = await asyncio.to_thread(_templates_for_agent, slug, query)
    except Exception as exc:
        return _instance_refusal(exc)
    return web.json_response(body)


async def _instance_body(request: web.Request) -> dict[str, Any] | web.Response:
    """The request's JSON object, or the response that refuses it."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_body"}, status=400
        )
    return body


async def api_dashboard_preview(request: web.Request) -> web.Response:
    """POST /api/agent-panel/dashboard/preview -- stage a template for a look.

    NOTHING IS RECORDED. The staged page is checked and written beside the record, the
    instance version does not move, and the body hands back the link a person opens to
    see it. That split is the feature: an agent can offer a page without having changed
    the one somebody is currently reading, so "show me another" costs nothing if the
    answer is no.

    Only a ``template_id`` is accepted. A body carrying ``html`` or ``manifest`` is
    refused by the store, which owns that refusal so every route into it answers the
    same way; this handler passes them through rather than dropping them, because a
    caller that sent a page it wrote has to be TOLD, not silently given a template it
    did not ask for.
    """
    resolved, refusal = await _resolve_dashboard_caller(request, "dashboard_preview")
    if refusal is not None:
        return refusal
    assert resolved is not None
    slug, crew_name, slot = resolved
    body = await _instance_body(request)
    if isinstance(body, web.Response):
        return body
    template_id = body.get("template_id")
    html = body.get("html")
    manifest = body.get("manifest")
    if template_id is not None and not isinstance(template_id, str):
        return web.json_response(
            {"error": "template_id must be a string", "code": "validation_error"}, status=400
        )

    from kiro_crew.dashboard_templates import instance as instance_store

    def _stage() -> dict[str, Any]:
        preview = instance_store.stage_preview(
            slug, template_id=template_id, html=html, manifest=manifest
        )
        wire = preview.wire(crew_name)
        if not crew_name:
            # A session's page is read by its SLOT, and the store key is a one-way
            # digest of it, so the link is built from the slot this caller resolved to.
            wire["preview_url"] = instance_store.session_preview_url(slot)
        return wire

    try:
        staged = await asyncio.to_thread(_stage)
    except Exception as exc:
        return _instance_refusal(exc)
    return web.json_response({"ok": True, "preview": staged})


def _dashboard_block(slug: str) -> str:
    """The new page's ``[DASHBOARD]`` block, upkeep pointer included."""
    from kiro_crew import dashboard_agentic

    return dashboard_agentic.turn_block(slug)


async def api_dashboard_apply(request: web.Request) -> web.Response:
    """POST /api/agent-panel/dashboard/apply -- put a page live, as a new version.

    ``{"template_id": <id>}`` applies that builtin or operator template. With no
    ``template_id`` the crewmate's own package saved by ``dashboard_save`` is applied.
    There is no preview step: a person cannot judge a page's script by looking at it
    (`rfc-crewmate-dynamic-dashboard.md`). A template apply RELEASES
    any package in force, which is the way from a composed page back to a template
    one; ``dashboard_rollback`` reaches every earlier version in one call.
    """
    resolved, refusal = await _resolve_dashboard_caller(request, "dashboard_apply")
    if refusal is not None:
        return refusal
    assert resolved is not None
    slug, crew_name, slot = resolved
    body: dict[str, Any] = {}
    if request.can_read_body:
        parsed = await _instance_body(request)
        if isinstance(parsed, web.Response):
            return parsed
        body = parsed
    template_id = body.get("template_id")
    if template_id is not None and (not isinstance(template_id, str) or not template_id.strip()):
        return web.json_response(
            {
                "error": "template_id must be a non-empty string",
                "code": "validation_error",
            },
            status=400,
        )

    from kiro_crew.dashboard_templates import instance as instance_store

    def _apply() -> Any:
        session_id = _history_session(slot)
        if template_id is not None:
            return instance_store.adopt(slug, template_id.strip(), session_id=session_id)
        return _apply_own_package(slug, crew_name, slot)

    try:
        record = await asyncio.to_thread(_apply)
    except ArtifactValidationError as exc:
        # The saved package fails the store's own gate, or its binding
        # cannot be resolved to one package. The sentence names what to fix.
        return web.json_response({"error": str(exc), "code": "invalid_package"}, status=400)
    except Exception as exc:
        return _instance_refusal(exc)
    # Tell an open dashboard its page changed, the same one-key frame the agentic
    # write broadcasts. The tab refetches the page; this carries the slug only.
    state: DashboardState = request.app["state"]
    _broadcast_changed(state, "dashboard_instance_changed", slug, crew_name, slot)
    return web.json_response(
        {
            "ok": True,
            "instance_version": record.instance_version,
            "template_id": record.template_id,
            "package": getattr(record, "package_slug", "") or None,
            # The member section is built at session start and after a compaction,
            # not every turn, so the new page's block and upkeep pointer ride this
            # reply until then.
            "dashboard": await asyncio.to_thread(_dashboard_block, slug),
        }
    )


async def api_dashboard_rollback(request: web.Request) -> web.Response:
    """POST /api/agent-panel/dashboard/rollback -- go back to a retained version.

    FORWARD, like the store's own rollback: version 1 restored over version 2 becomes
    version 3. The response says which version is now current rather than echoing the
    one that was asked for, because those are two different numbers and a caller that
    reported the second would tell a person they are on a version nobody is on.
    """
    resolved, refusal = await _resolve_dashboard_caller(request, "dashboard_rollback")
    if refusal is not None:
        return refusal
    assert resolved is not None
    slug, crew_name, slot = resolved
    body = await _instance_body(request)
    if isinstance(body, web.Response):
        return body
    to_version = body.get("to_version")
    if not isinstance(to_version, int) or isinstance(to_version, bool) or to_version < 1:
        return web.json_response(
            {"error": "to_version must be a positive integer", "code": "validation_error"},
            status=400,
        )

    from kiro_crew.dashboard_templates import instance as instance_store

    def _rollback() -> Any:
        return instance_store.rollback(slug, to_version, session_id=_history_session(slot))

    try:
        record = await asyncio.to_thread(_rollback)
    except Exception as exc:
        return _instance_refusal(exc)
    state: DashboardState = request.app["state"]
    _broadcast_changed(state, "dashboard_instance_changed", slug, crew_name, slot)
    return web.json_response(
        {
            "ok": True,
            "instance_version": record.instance_version,
            "restored_from": to_version,
            "template_id": record.template_id,
            "dashboard": await asyncio.to_thread(_dashboard_block, slug),
        }
    )
