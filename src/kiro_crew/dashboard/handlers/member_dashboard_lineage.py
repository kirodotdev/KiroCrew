"""Edit mode's gateway routes: where one Dashboard value came from, and the person's fix.

Both sit behind the ``dashboard_edit_mode`` feature preview and answer 404
``not_enabled`` while it is off.

``GET /api/members/{slug}/dashboard/lineage?member=&path=`` answers the chain behind one
value on the crewmate's page in force, built only from data the gateway holds: the field's
declared source, the value it reads now, and the crew-log rows behind it. The page names a
PATH and nothing else, so the chain is this module's word, never the page's. A hop the
record cannot supply yet comes back as ``{"kind": ..., "needs": "<what>"}``. Owner-only and
read-only.

``POST /api/members/{slug}/dashboard/correct?member=`` is the person's correction of a value
the Bot writes (``{agentic: true}``): checked against the field's declared type exactly as a
``dashboard_write`` is, then appended as the same ``dashboard/agentic_value`` entry with
``actor: "person"``. The Bot may write over it later; its next turn is told the person
corrected it (``dashboard_agentic.person_corrections_line``). Owner-only and refused to an
agent's internal-secret caller, so a page script or a Bot can never make one.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Final, Mapping

from aiohttp import web

from kiro_crew import dashboard_edit_mode
from kiro_crew.crew_log.entry_types import DASHBOARD_REASON_LIMIT

logger = logging.getLogger(__name__)

__all__ = [
    "api_member_dashboard_correct",
    "api_member_dashboard_lineage",
    "build_lineage",
    "register_member_dashboard_lineage_routes",
]

#: Gaps the record cannot fill yet, by the words the host shows.
NEEDS_FOLD_SEQ: Final[str] = "entry seq on fold rows"
NEEDS_LOOKED_AT: Final[str] = "source + observed_at on dashboard_write"
NEEDS_RUN_ID: Final[str] = "run id and code digest on the scheduler's run record"

#: The custom fold's name, as ``crew_log.entry_types_surfaces.CUSTOM_FOLD_NAME`` spells it.
_CUSTOM_FOLD: Final[str] = "custom"


def _bad(code: str, message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


def _custom_row(custom: Any, path: str) -> tuple[str, Mapping[str, Any] | None]:
    """``(record type, its latest row)`` for a ``custom`` fold path ``latest.<type>...``."""
    parts = path.split(".")
    if len(parts) < 2 or parts[0] != "latest":
        return "", None
    latest = custom.get("latest") if isinstance(custom, Mapping) else None
    row = latest.get(parts[1]) if isinstance(latest, Mapping) else None
    return parts[1], row if isinstance(row, Mapping) else None


def _record_hops(record_type: str, row: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The hops behind a custom record: who wrote it, how long it holds, how its run went."""
    if row is None:
        return [{"kind": "record", "type": record_type, "found": False, "by": "gateway"}]
    hops: list[dict[str, Any]] = [
        {
            "kind": "record",
            "type": record_type,
            "found": True,
            "writer": str(row.get("writer") or ""),
            "at": str(row.get("at") or ""),
            "seq": row.get("seq") if isinstance(row.get("seq"), int) else None,
            "by": "gateway",
        }
    ]
    # In the order a reader asks: is it current, did the job keep running, which
    # script wrote it, and what that script says it read.
    hops.append(
        {
            "kind": "freshness",
            "max_age_s": row.get("max_age_s") if isinstance(row.get("max_age_s"), int) else None,
            "stale_after": str(row.get("stale_after") or ""),
            "by": "script" if row.get("max_age_s") else "gateway",
        }
    )
    if row.get("failed_at"):
        hops.append(
            {
                "kind": "run",
                "failed_at": str(row.get("failed_at") or ""),
                "last_error": str(row.get("last_error") or ""),
                "by": "gateway",
            }
        )
    provenance = row.get("provenance")
    if isinstance(provenance, Mapping):
        script: dict[str, Any] = {
            "kind": "script",
            "job": str(provenance.get("job") or ""),
            "run_at": str(provenance.get("run_at") or ""),
            "script": str(provenance.get("script") or ""),
            # The scheduler keeps no id per run and no digest of the code that ran, so
            # neither is stored; the gap is named.
            "needs": NEEDS_RUN_ID,
            "by": "gateway",
        }
        hops.append(script)
    # The readings ride on the source hop, which a script may send without a descriptor.
    has_source = isinstance(row.get("source"), Mapping)
    if has_source or isinstance(row.get("points"), list):
        source: dict[str, Any] = {
            "kind": "source",
            "source": dict(row["source"]) if has_source else {},
            "by": "script",
        }
        if isinstance(row.get("points"), list):
            source["points"] = row["points"]
        hops.append(source)
    return hops


def _write_hop(row: Mapping[str, Any]) -> dict[str, Any]:
    actor = "person" if row.get("actor") == "person" else "bot"
    hop: dict[str, Any] = {
        "kind": "write",
        "actor": actor,
        "at": str(row.get("at") or ""),
        "instance_version": (
            row.get("instance_version") if isinstance(row.get("instance_version"), int) else None
        ),
        "by": "gateway",
    }
    if isinstance(row.get("seq"), int):
        hop["seq"] = row["seq"]
    if actor == "person" and row.get("reason"):
        hop["reason"] = str(row["reason"])
    return hop


def build_lineage(
    spec: Any,
    steps: list[str | int],
    *,
    path: str,
    field_value: Any,
    field_found: bool,
    agentic_row: Mapping[str, Any] | None = None,
    stale_after: str = "",
    custom: Any = None,
    redact: Callable[[Any], Any] = lambda value: value,
) -> dict[str, Any]:
    """The lineage of one picked *path* on a page whose field is *spec*.

    *spec* is the page manifest's ``FieldSpec`` for the path's root field, *field_value*
    the value the page is sent for it (``field_found`` False when it did not resolve),
    *agentic_row* that field's row in the ``agentic`` fold, *stale_after* when that
    row's write goes out of date (``dashboard_agentic.stale_after_map``'s stamp) and
    *custom* the ``custom`` fold's value. Pure: the route reads, this arranges. Every
    value leaving here goes through *redact*, the page's own pass.
    """
    from kiro_crew import dashboard_agentic

    found, value = dashboard_edit_mode.walk(field_value, steps) if field_found else (False, None)
    sub = path[len(spec.name) :]
    hops: list[dict[str, Any]] = []
    if spec.agentic:
        kind = "agentic"
        hops.append({"kind": "field", "field": spec.name, "type": spec.type, "agentic": True})
        row = agentic_row if isinstance(agentic_row, Mapping) else None
        if row is None:
            hops.append({"kind": "write", "found": False, "by": "gateway"})
        else:
            hops.append(_write_hop(row))
            prior = row.get("prior")
            if isinstance(prior, Mapping):
                earlier = _write_hop(prior)
                earlier["kind"] = "earlier_write"
                hops.append(earlier)
            # The field's own limit (``stale_after_s``, or the default), and the time the
            # write passes it, both as the page's out-of-date mark reads them. A limit of
            # 0 is never stale, so it has neither.
            limit = dashboard_agentic.stale_after_seconds(spec)
            hops.append(
                {
                    "kind": "freshness",
                    "max_age_s": limit if limit > 0 else None,
                    "stale_after": stale_after if limit > 0 else "",
                    "by": "gateway",
                }
            )
        hops.append({"kind": "looked_at", "needs": NEEDS_LOOKED_AT})
        # A whole object or list is not typed by hand: the popover offers Ask instead.
        edit: dict[str, Any] = {
            "kind": "correct",
            "type": spec.type,
            "scalar": spec.type not in ("array", "object"),
        }
    else:
        fold, fold_path = str(spec.fold or ""), str(spec.path or "")
        kind = "custom" if fold == _CUSTOM_FOLD else "fold"
        hops.append(
            {
                "kind": "field",
                "field": spec.name,
                "type": spec.type,
                "fold": fold,
                "path": fold_path,
            }
        )
        hops.append(
            {
                "kind": "fold",
                "fold": fold,
                "path": fold_path + sub,
                "found": found,
                "by": "gateway",
            }
        )
        if kind == "custom":
            # A field bound to ``latest`` names its record only in the picked subpath.
            record_type, row = _custom_row(custom, (fold_path + sub).lstrip("."))
            hops.extend(_record_hops(record_type, row))
            writer = str((row or {}).get("writer") or "")
            job = writer.removeprefix("cron:") if writer.startswith("cron:") else ""
            edit = {"kind": "source", "writer": writer, "job": job}
        else:
            hops.append({"kind": "crew_log", "fold": fold, "needs": NEEDS_FOLD_SEQ})
            edit = {"kind": "fold"}
    return redact(
        {
            "path": path,
            "field": spec.name,
            "kind": kind,
            "type": spec.type,
            "found": found,
            "value": value if found else None,
            "hops": hops,
            "edit": edit,
        }
    )


async def _gate(request: web.Request, operation: str) -> tuple[str, str] | web.Response:
    """``(slug, member)`` once the preview, the member and the owner checks pass."""
    from kiro_crew.dashboard.handlers import member_dashboard as md

    if not await asyncio.to_thread(dashboard_edit_mode.edit_mode_on):
        return _bad("not_enabled", "dashboard edit mode is not turned on", status=404)
    resolved = await md._resolve(request)
    if isinstance(resolved, web.Response):
        return resolved
    denied = await md._owner_only(request, operation)
    if denied is not None:
        return denied
    return resolved


def _manifest_in_force(slug: str) -> tuple[Any, bool]:
    """``(manifest, is_package)`` of the page *slug*'s tab shows; manifest ``None`` when
    there is no page. File IO.

    A package page first, through ``dashboard_package.read_package_model``: the seam
    a ``dashboard_write`` is checked through, which answers the read route's own rule
    (a pinned package, or an older record's bound package with a version a person
    cut). Otherwise the template page in force. *is_package* tells the lineage which
    render's session unit to read, since the two renders pick different units.
    """
    from kiro_crew import dashboard_agentic, dashboard_package

    try:
        package = dashboard_package.read_package_model(slug)
    except dashboard_package.PackageReadError:
        package = None
    if package is not None and package.from_package and package.model is not None:
        return package.model.manifest, True
    page = dashboard_agentic.page_in_force(slug)
    return (page[1] if page is not None else None), False


#: A fold this request may not fold from scratch (``cold=False`` found no warm cell).
_NOT_READY: Final = object()


def _warm_slot_value(slot: str, name: str) -> Any:
    """*slot*'s *name* fold value, read WARM ONLY, or :data:`_NOT_READY`. File IO.

    A lineage is a request, so it never folds a slot from its first record. A cell with
    nothing warm and no savepoint answers "not ready" and is handed to one background
    prime (:mod:`kiro_crew.crew_log.slot_prime`); the next ask reads it warm.
    """
    from kiro_crew.crew_log import projection, slot_prime

    try:
        return projection.read_slot_projection(slot, name, cold=False).value
    except projection.SlotFoldNotReady:
        slot_prime.schedule(slot, name, for_reader=True)
        return _NOT_READY


def _tree_value(slot: str, fold: str, fold_path: str) -> Any:
    """``(found, value)`` of *fold_path* in the tree fold *fold* rooted at *slot*, or
    :data:`_NOT_READY`. File IO.

    The tree ``LivePage._read_trees`` reads for the page, but warm only: the page's own
    load folds the tree, so a lineage asked from that page finds the cell warm, and a
    cell that is not warm yet answers "not ready". A tree that cannot be read, or that
    lacks the path, is not found.
    """
    from kiro_crew.crew_log import projection
    from kiro_crew.dashboard_feed import MISSING, resolve_path

    try:
        units, dropped = projection.tree_fold_units(slot, fold)
        checkpoint, revision = projection.fold_slot_warm_revised(
            fold, list(units), slot=slot, cold=False
        )
        tree = projection.projection_of(checkpoint, revision=revision).value
        if dropped and isinstance(tree, dict):
            # As read_tree_projection: the selection's left-out units count as dropped.
            tree = {**tree, "dropped": int(tree.get("dropped", 0)) + dropped}
    except projection.SlotFoldNotReady:
        return _NOT_READY
    except Exception:
        logger.info("lineage: the %s tree could not be read", fold)
        return False, None
    value = resolve_path(tree, fold_path)
    return (False, None) if value is MISSING else (True, value)


def _read_lineage(slug: str, member: str, path: str, state: Any) -> dict[str, Any] | str:
    """The lineage for *path* on *slug*'s page in force, or a refusal code. File IO."""
    from kiro_crew import dashboard_agentic
    from kiro_crew.crew_log import projection
    from kiro_crew.dashboard.handlers import member_dashboard as md

    parsed = dashboard_edit_mode.parse_field_path(path)
    if parsed is None:
        return "invalid_path"
    root, steps = parsed
    manifest, is_package = _manifest_in_force(slug)
    if manifest is None:
        return "no_page"
    spec = manifest.fields.get(root)
    if spec is None:
        return "field_not_declared"
    slot = md._dashboard_slot(member, slug)
    # The unit the page reads its session folds from, so a traced value is the shown one.
    # A package page's live view reads the live session's unit (`_live_or_newest_unit`);
    # a template page's render reads `read_fields`' own default (`_write_session`), so
    # it passes no unit and lets `read_fields` pick the same one.
    session = md._live_or_newest_unit(state, slot) if is_package else None
    read = md.read_fields(slug, member, manifest, prime_wait=1.0, session=session)
    agentic_row = None
    stale_after = ""
    custom = None
    if spec.agentic:
        fold = _warm_slot_value(slot, "agentic")
        if fold is _NOT_READY:
            return "not_ready"
        rows = fold.get("fields") if isinstance(fold, Mapping) else None
        agentic_row = rows.get(root) if isinstance(rows, Mapping) else None
        if isinstance(agentic_row, Mapping) and isinstance(agentic_row.get("at"), str):
            stale_after = dashboard_agentic.stale_after_map(
                manifest, {root: agentic_row["at"]}
            ).get(root, "")
    elif spec.fold == _CUSTOM_FOLD:
        custom = _warm_slot_value(slot, _CUSTOM_FOLD)
        if custom is _NOT_READY:
            return "not_ready"
    field_value = read.fields.get(root)
    field_found = root in read.fields and root not in read.missing
    # ONE SNAPSHOT: the value shown and the provenance beside it come from the same
    # fold read, so a write landing between two reads cannot pair one write's value
    # with another's writer, time and freshness.
    if isinstance(agentic_row, Mapping) and "value" in agentic_row:
        field_found, field_value = True, agentic_row["value"]
    elif custom is not None:
        from kiro_crew.dashboard_feed import MISSING, resolve_path

        resolved = resolve_path(custom, str(spec.path or ""))
        field_found, field_value = resolved is not MISSING, (
            None if resolved is MISSING else resolved
        )
    if not spec.agentic and str(spec.fold or "") in projection.TREE_PROJECTION_NAMES:
        # A tree fold has no subscription, so the feed reports its fields missing; the
        # package page reads the tree rooted at its own slot, and so does this.
        tree = _tree_value(slot, str(spec.fold), str(spec.path or ""))
        if tree is _NOT_READY:
            return "not_ready"
        field_found, field_value = tree
    if field_found:
        # THE VALUE THE PAGE DREW, WHOLE: the page is sent this field through
        # ``page_safe_for``, which stamps work items with their worker facts and then
        # masks what the page may not see (a worker's session key, a bind event's text).
        # The picked subpath is walked in THAT value, so it can reach neither a masked
        # key nor miss a stamped one; the reply still goes through the same pass.
        field_value = md.page_safe_for(state, slot)(field_value)
    return build_lineage(
        spec,
        steps,
        path=path,
        field_value=field_value,
        field_found=field_found,
        agentic_row=agentic_row,
        stale_after=stale_after,
        custom=custom,
        redact=md.page_safe_for(state, slot),
    )


_REFUSALS: Final[Mapping[str, tuple[str, int]]] = {
    "invalid_path": ("path is not a field path the page could name", 400),
    "no_page": ("this Bot has no Dashboard page in force", 404),
    "field_not_declared": ("the page does not declare that field", 404),
    "not_ready": ("the value's records are still being read; try again shortly", 503),
}


async def api_member_dashboard_lineage(request: web.Request) -> web.Response:
    """GET /api/members/{slug}/dashboard/lineage?member=&path= -- one value's lineage."""
    gated = await _gate(request, "members.dashboard.lineage")
    if isinstance(gated, web.Response):
        return gated
    slug, member = gated
    path = request.query.get("path", "")
    state = request.app.get("state")
    try:
        answer = await asyncio.to_thread(_read_lineage, slug, member, path, state)
    except Exception:
        logger.warning("dashboard lineage for %s could not be read", slug, exc_info=True)
        return _bad("lineage_failed", "the lineage could not be read", status=500)
    if isinstance(answer, str):
        message, status = _REFUSALS[answer]
        return _bad(answer, message, status=status)
    return web.json_response(answer)


def _append_correction(slug: str, member: str, entry: dict[str, Any]) -> bool:
    """Append the person's correction to the Bot's DM log. File IO."""
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.dashboard.handlers import member_dashboard as md
    from kiro_crew.session_ledger import _APPEND_FLUSH_SECONDS

    unit = md._write_session(slug, member)
    if not unit:
        return False
    if not crew_log_emit.dashboard_entry_fits("dashboard/agentic_value", entry):
        return False
    return bool(crew_log_emit.on_dashboard_agentic(unit, entry, timeout=_APPEND_FLUSH_SECONDS))


def _page_in_force(package: Any) -> tuple[str, Any]:
    """``(page, version)`` of the page in force, spelled as the page read names it.

    The page is ``package:<slug>@<apply counter>`` or ``template:<template id>``; the
    version is the number the read reports as ``instance_version``: the PACKAGE version
    on a package page (``_serve_package``), the record's counter on a template one.
    A package page carries the record's apply counter too, which only goes up, because
    a slug deleted and saved again starts its versions at 1 again (the page read sends
    it as ``page_revision``).
    """
    manifest = getattr(package.model, "manifest", None)
    if getattr(package, "from_package", False):
        revision = getattr(package.model, "instance_version", None)
        page = f"package:{getattr(package, 'slug', '')}@{revision}"
        return page, getattr(manifest, "version", None)
    return (
        f"template:{getattr(manifest, 'id', '')}",
        getattr(package.model, "instance_version", None),
    )


async def api_member_dashboard_correct(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/dashboard/correct?member= -- the person corrects a value.

    Body ``{field, value, page, page_version, reason?}``: ``page`` and ``page_version``
    name the page the person typed against. Only a field the Bot writes can be corrected: a fold
    or a script's record is the record's, and is fixed at its source.
    """
    from kiro_crew import agent_panel, dashboard_agentic, dashboard_package
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.dashboard.handlers import member_dashboard as md

    gated = await _gate(request, "members.dashboard.correct")
    if isinstance(gated, web.Response):
        return gated
    slug, member = gated
    refused_agent = md._refuse_agent_caller(
        request,
        "members.dashboard.correct",
        "only the person can correct a Dashboard value; a Bot writes with dashboard_write",
    )
    if refused_agent is not None:
        return refused_agent
    try:
        body = await request.json()
    except Exception:
        return _bad("invalid_json", "invalid JSON body")
    # The page the person typed against, by name AND version: a version alone does not
    # name a page, since a template's counter and a package's version both start at 1.
    seen_page = body.get("page") if isinstance(body, dict) else None
    seen_version = body.get("page_version") if isinstance(body, dict) else None
    if (
        not isinstance(body, dict)
        or not isinstance(body.get("field"), str)
        or "value" not in body
        or not isinstance(seen_page, str)
        or not seen_page
        or not isinstance(seen_version, int)
        or isinstance(seen_version, bool)
    ):
        return _bad("invalid_body", "body must be {field, value, page, page_version, reason?}")
    reason = body.get("reason", "")
    if isinstance(reason, str) and reason.strip():
        from kiro_crew.platform.context import redact_via_context

        # Measured as stored: redaction can lengthen a reason, and the fold keeps at
        # most DASHBOARD_REASON_LIMIT, so a longer one would be cut without a word.
        reason = redact_via_context(reason.strip())
    if (
        not isinstance(reason, str)
        or len(reason) > DASHBOARD_REASON_LIMIT
        or "\n" in reason.strip()
    ):
        return _bad(
            "invalid_reason",
            f"reason must be one line of at most {DASHBOARD_REASON_LIMIT} characters",
        )
    field = body["field"]
    if not crew_log_emit.enabled():
        return _bad("crew_log_off", "this gateway is not recording a crew log", status=503)

    def check_and_append() -> web.Response | None:
        """Read the page in force, check the value against it, and append, as one step.

        Held under the lock a page apply commits under, so no newer page can land
        between the checks and the write. File IO; run off the loop.
        """
        from kiro_crew.dashboard_templates import instance as instance_store

        with instance_store.instance_write_lock(slug):
            try:
                package = dashboard_package.read_package_model(slug)
            except dashboard_package.PackageReadError:
                return _bad("package_unreadable", "the Dashboard page could not be read", 503)
            # Typed against the page the person saw: another page in force, or a newer
            # version of it, may give the field another meaning, so it is refused, not
            # written into. With no page in force there is nothing to compare, and
            # `package_refusal` below says why.
            if package.model is not None and (seen_page, seen_version) != _page_in_force(package):
                return _bad("stale_page", "the Dashboard page changed; check the value again", 409)
            try:
                unusable = dashboard_agentic.package_refusal(
                    package.state, package.state_reason, field
                )
                if unusable is not None:
                    raise unusable
                # THE SAME CHECK a Bot's dashboard_write passes: declared, agentic, the
                # declared type, and small enough to keep. Not recorded in the Bot's
                # mistake book: the book teaches the Bot, and this was not its mistake.
                entry = dashboard_agentic.check_write(package.model, field, body["value"])
            except dashboard_agentic.WriteRefused as refused:
                return _bad(refused.code, dashboard_agentic.refusal_sentence(refused))
            if entry.get("type") in ("array", "object"):
                # A whole list or record is the Bot's to rebuild; the person asks instead.
                return _bad("not_scalar", "a list or object value cannot be corrected by hand")
            entry["crew_key"] = agent_panel.crew_key(member)
            entry["actor"] = "person"
            if reason.strip():
                entry["reason"] = reason
            if not _append_correction(slug, member, entry):
                return _bad("append_failed", "the correction could not be written", 503)
        return None

    refused = await asyncio.to_thread(check_and_append)
    if refused is not None:
        return refused
    state = request.app.get("state")
    if state is not None:
        state.broadcast_ws("dashboard_value_written", {"slug": slug})
    return web.json_response({"ok": True, "field": field})


def register_member_dashboard_lineage_routes(app: web.Application) -> None:
    """The two edit mode routes. ``mcp_routes`` binds the same paths through its deferred
    binder; ``test_dashboard_edit_mode`` pins the two spellings equal."""
    app.router.add_get("/api/members/{slug}/dashboard/lineage", api_member_dashboard_lineage)
    app.router.add_post("/api/members/{slug}/dashboard/correct", api_member_dashboard_correct)
