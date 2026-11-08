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

logger = logging.getLogger(__name__)

__all__ = [
    "api_member_dashboard_correct",
    "api_member_dashboard_lineage",
    "build_lineage",
    "register_member_dashboard_lineage_routes",
]

#: Characters the person's reason for a correction keeps.
REASON_LIMIT: Final[int] = 240

#: Gaps the record cannot fill yet, by the words the host shows.
NEEDS_FOLD_SEQ: Final[str] = "entry seq on fold rows"
NEEDS_LOOKED_AT: Final[str] = "source + observed_at on dashboard_write"

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
    provenance = row.get("provenance")
    if isinstance(provenance, Mapping):
        hops.append(
            {
                "kind": "script",
                "job": str(provenance.get("job") or ""),
                "run_at": str(provenance.get("run_at") or ""),
                "script": str(provenance.get("script") or ""),
                "sha": str(provenance.get("sha") or ""),
                "by": "gateway",
            }
        )
    if isinstance(row.get("source"), str) and row.get("source"):
        source: dict[str, Any] = {"kind": "source", "source": row["source"], "by": "script"}
        if isinstance(row.get("points"), list):
            source["points"] = row["points"]
        hops.append(source)
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
        edit: dict[str, Any] = {"kind": "correct", "type": spec.type}
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
            record_type, row = _custom_row(custom, fold_path)
            hops.extend(_record_hops(record_type, row))
            writer = str((row or {}).get("writer") or "")
            edit = {"kind": "none", "reason": "cron", "writer": writer}
        else:
            hops.append({"kind": "crew_log", "fold": fold, "needs": NEEDS_FOLD_SEQ})
            edit = {"kind": "none", "reason": "fold"}
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


def _read_lineage(slug: str, member: str, path: str, state: Any) -> dict[str, Any] | str:
    """The lineage for *path* on *slug*'s page in force, or a refusal code. File IO."""
    from kiro_crew import dashboard_agentic
    from kiro_crew.crew_log import projection
    from kiro_crew.dashboard.handlers import member_dashboard as md

    parsed = dashboard_edit_mode.parse_field_path(path)
    if parsed is None:
        return "invalid_path"
    root, steps = parsed
    page = dashboard_agentic.page_in_force(slug)
    if page is None:
        # shortcut: an older record that finds its package by binding alone is not
        # traced; upgrade by sharing member_dashboard._serve's selection if one needs it.
        return "no_page"
    _record, manifest = page
    spec = manifest.fields.get(root)
    if spec is None:
        return "field_not_declared"
    slot = md._dashboard_slot(member, slug)
    read = md.read_fields(slug, member, manifest, prime_wait=1.0)
    agentic_row = None
    stale_after = ""
    custom = None
    if spec.agentic:
        fold = projection.read_slot_projection(slot, "agentic").value
        rows = fold.get("fields") if isinstance(fold, Mapping) else None
        agentic_row = rows.get(root) if isinstance(rows, Mapping) else None
        if isinstance(agentic_row, Mapping) and isinstance(agentic_row.get("at"), str):
            stale_after = dashboard_agentic.stale_after_map(
                manifest, {root: agentic_row["at"]}
            ).get(root, "")
    elif spec.fold == _CUSTOM_FOLD:
        custom = projection.read_slot_projection(slot, _CUSTOM_FOLD).value
    return build_lineage(
        spec,
        steps,
        path=path,
        field_value=read.fields.get(root),
        field_found=root in read.fields and root not in read.missing,
        agentic_row=agentic_row,
        stale_after=stale_after,
        custom=custom,
        redact=md.page_safe_for(state, slot),
    )


_REFUSALS: Final[Mapping[str, tuple[str, int]]] = {
    "invalid_path": ("path is not a field path the page could name", 400),
    "no_page": ("this Bot has no Dashboard page in force", 404),
    "field_not_declared": ("the page does not declare that field", 404),
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


async def api_member_dashboard_correct(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/dashboard/correct?member= -- the person corrects a value.

    Body ``{field, value, reason?}``. Only a field the Bot writes can be corrected: a fold
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
    if not isinstance(body, dict) or not isinstance(body.get("field"), str) or "value" not in body:
        return _bad("invalid_body", "body must be {field, value, reason?}")
    reason = body.get("reason", "")
    if not isinstance(reason, str) or len(reason) > REASON_LIMIT or "\n" in reason.strip():
        return _bad(
            "invalid_reason", f"reason must be one line of at most {REASON_LIMIT} characters"
        )
    field = body["field"]
    try:
        package = await asyncio.to_thread(dashboard_package.read_package_model, slug)
    except dashboard_package.PackageReadError:
        return _bad("package_unreadable", "the Dashboard page could not be read", status=503)
    try:
        unusable = dashboard_agentic.package_refusal(package.state, package.state_reason, field)
        if unusable is not None:
            raise unusable
        # THE SAME CHECK a Bot's dashboard_write passes: declared, agentic, the declared
        # type, and small enough to keep. Not recorded in the Bot's mistake book: the
        # book teaches the Bot, and this was not its mistake.
        entry = dashboard_agentic.check_write(package.model, field, body["value"])
    except dashboard_agentic.WriteRefused as refused:
        return _bad(refused.code, dashboard_agentic.refusal_sentence(refused))
    entry["crew_key"] = agent_panel.crew_key(member)
    entry["actor"] = "person"
    if reason.strip():
        from kiro_crew.platform.context import redact_via_context

        entry["reason"] = redact_via_context(reason.strip())
    if not crew_log_emit.enabled():
        return _bad("crew_log_off", "this gateway is not recording a crew log", status=503)
    if not await asyncio.to_thread(_append_correction, slug, member, entry):
        return _bad("append_failed", "the correction could not be written", status=503)
    state = request.app.get("state")
    if state is not None:
        state.broadcast_ws("dashboard_value_written", {"slug": slug})
    return web.json_response({"ok": True, "field": field})


def register_member_dashboard_lineage_routes(app: web.Application) -> None:
    """The two edit mode routes. ``mcp_routes`` binds the same paths through its deferred
    binder; ``test_dashboard_edit_mode`` pins the two spellings equal."""
    app.router.add_get("/api/members/{slug}/dashboard/lineage", api_member_dashboard_lineage)
    app.router.add_post("/api/members/{slug}/dashboard/correct", api_member_dashboard_correct)
