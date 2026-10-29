"""The installed app's record door: ``POST /api/member-records/{slug}``.

An installed app hands the gateway one record -- a type name and an object -- for the
crewmate the path names, and the gateway appends it to that crewmate's own DM session
log as ONE ``dashboard/custom_value`` entry, stamped ``writer: "app:<id>"``. It is the
second caller of the door a script cron appends through
(:mod:`kiro_crew.dashboard.handlers.agent_panel_record`): the same entry type, the same
caps, the same quota, the same ``custom`` fold, so a page bound to
``custom.latest.<app>__<type>.data`` redraws when the record lands, with no model turn.

Three things are this module's and nothing else is:

**The grant is a manifest ``permissions.api`` entry**, ``/api/member-records/*``, and
the token middleware's deny-by-default scope check is what enforces it
(``token_auth_app_scope._enforce_app_scope``): an app that did not declare it, or whose
owner has not yet approved it on the app's detail page, never reaches this handler.
The route is deliberately OUTSIDE the app's own ``/api/apps/<name>`` namespace, which
an app token may call with no grant at all; a door that let an app write a crewmate's
log just by owning its own namespace would be no grant. No ``contributions`` block,
no second approval record, no new consent screen: the owner approves this prefix the
way they approve any other.

**The writer is the token's app claim, never the body's.** ``request["app"]`` is set
by the token middleware from the claim the gateway minted at the app's secret
exchange, so the ``writer`` stamped on the entry is the gateway's reading of who
called. The body is read for ``type`` and ``data`` only, exactly as the cron door reads
it; ``writer``, ``member``, ``src``, ``seq`` and ``time`` cannot be supplied.

**The record type is keyed by the app**, stored as ``<app>__<type>`` where ``<app>`` is
the app id with each ``-`` written ``_`` (:func:`app_record_type`). Three properties,
each closing a specific hole:

- *Bindable.* A page binds a DOTTED path into the fold, and a path segment is
  ``[A-Za-z0-9_]+`` (``dashboard_templates.manifest._PATH``), so neither ``-`` nor ``.``
  may appear in the key. The door's shared type rule (``custom_type_name_ok``) already
  holds every caller's type to that alphabet; an app id is kebab-case
  (``apps.manifest_names.KEBAB_RE``), so its hyphens are rewritten into it, and the
  whole key is one segment.
- *Injective.* A kebab id has no ``_``, no two hyphens in a row and no hyphen at
  either end, so its rewritten form never contains ``__`` and ends in an alphanumeric
  (``apps.manifest_names.app_name_error`` admits every app name); the FIRST ``__`` in a
  key therefore always sits exactly after the app part, and ``github`` + ``pr_status``
  and ``github-pr`` + ``status`` are the two keys ``github__pr_status`` and
  ``github_pr__status``.
- *Reserved.* ``__`` is refused in every caller-supplied type, cron and app alike
  (:func:`agent_panel_record._record_body_refusal`), so no writer can hand the door a
  type that spells another app's key. The key names who wrote it, beside the ``writer``
  the fold row carries, and an app can neither land on another app's key nor on a
  cron's.

The whole key is held to the same 64-character rule as a cron's type
(:func:`custom_type_name_ok`), so a type that fits alone but not behind its app id is
refused naming the key, rather than written under one no page could bind.

An app backend with a command entry point runs as a sandboxed gateway child with the
whole ``crew-log`` root hidden from it, so this route is the only way such an app can
put a value in a crewmate's log; a ``backend.routes`` module running inside the gateway
process could already append anything, and gains nothing here it did not have.

:mod:`kiro_crew.dashboard.handlers.agent_panel` is this module's facade: callers and
tests import and patch every name through it (:mod:`kiro_crew._facade_mirror`). This
module never imports the facade.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

from aiohttp import web

from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.crew_log.entry_types import CUSTOM_TYPE_NAME_LIMIT
from kiro_crew.crew_log.projection_dashboard import custom_type_name_ok
from kiro_crew.dashboard.handlers.agent_panel_read import _panel_slot
from kiro_crew.dashboard.handlers.agent_panel_record import (
    RECORD_QUOTA_PER_MINUTE,
    RecordWriter,
    _over_quota,
    _record_body_refusal,
    _refuse,
    record_through_door,
)
from kiro_crew.dashboard.handlers.members import _member_names_for_slug

logger = logging.getLogger("kiro_crew.dashboard.handlers.agent_panel")

#: The provenance prefix stamped on an app's entry: ``app:<id>``.
APP_WRITER_PREFIX: Final[str] = "app:"

#: Joins the rewritten app id and the app's own type into the stored key. Two
#: underscores, because a rewritten kebab id can never contain two in a row, which is
#: what makes the first occurrence the separator; the door refuses ``__`` in every
#: caller-supplied type for the same reason.
TYPE_SEPARATOR: Final[str] = "__"

_RECORD_OPERATION: Final[str] = "member_record"


def app_record_type(app_name: str, record_type: str) -> str:
    """The fold key an app's *record_type* is stored under: ``<app>__<type>``, the app
    id's hyphens written as underscores so the key is one bindable path segment."""
    return f"{app_name.replace('-', '_')}{TYPE_SEPARATOR}{record_type}"


async def _resolve_app_writer(
    request: web.Request, slug: str
) -> tuple[RecordWriter, None] | tuple[None, web.Response]:
    """Vet the caller and resolve it to the app it is, writing for the crewmate *slug*
    names, or the refusal it earns.

    The ONE place this door decides who may write. The identity is the app claim the
    token middleware put on the request; a caller with no app claim -- the owner's own
    cookie, an internal MCP caller, a cron -- is refused, because each of those already
    has its own way to put a value on the page (``dashboard_write``, the cron door) and
    this one carries an app's provenance. The crewmate comes from the path, resolved
    the way every member route resolves a slug: no crew for it is 404, two crews
    sharing it is 409, because an entry landing in the wrong crewmate's log is the
    outcome the ambiguity check exists to refuse.
    """
    app_name = str(request.get("app") or "")
    if not app_name:
        return None, _refuse(
            request,
            "",
            "app_token_required",
            "only an installed app may append a member record here; use the app's own " "token",
            403,
            operation=_RECORD_OPERATION,
        )
    if not await asyncio.to_thread(members_mod.crew_panel_enabled):
        return None, _refuse(
            request,
            app_name,
            "crew_panel_disabled",
            "the crew dashboard is switched off",
            403,
            operation=_RECORD_OPERATION,
        )

    def _resolve() -> RecordWriter | str:
        cfg = KiroCrewConfig.load()
        try:
            members_mod.validate_slug(slug)
        except members_mod.MemberSlugError:
            return "member_not_found"
        names = _member_names_for_slug(cfg, slug)
        if not names:
            return "member_not_found"
        if len(names) > 1:
            return "ambiguous_slug"
        crew_name = names[0]
        return RecordWriter(
            f"{APP_WRITER_PREFIX}{app_name}",
            crew_name,
            slug,
            _panel_slot(cfg, crew_name, slug),
        )

    resolved = await asyncio.to_thread(_resolve)
    if resolved == "member_not_found":
        return None, _refuse(
            request,
            app_name,
            "member_not_found",
            "no crewmate has this slug",
            404,
            operation=_RECORD_OPERATION,
        )
    if resolved == "ambiguous_slug":
        return None, _refuse(
            request,
            app_name,
            "ambiguous_slug",
            "two crewmates share this slug, so the record has no single log to land in",
            409,
            operation=_RECORD_OPERATION,
        )
    assert isinstance(resolved, RecordWriter)
    return resolved, None


async def api_member_record(request: web.Request) -> web.Response:
    """POST /api/member-records/{slug} -- one custom record from an installed app, for
    the crewmate the path names.

    Body ``{"type": <name>, "data": {...}}``. The steps are the cron door's, with the
    app's own resolver in front:

    1. resolve the WRITER from the token's app claim and the path's slug (never from
       the body);
    2. count the call against the app's quota, before the body is read, so a refusal
       costs a slot too;
    3. check the record's shape and size, then the KEYED type -- ``<app>__<type>`` is
       what the fold keys on, so it is what has to fit the type-name rule;
    4. scrub and append it through the shared door.

    Answers ``{"ok": true, "recorded": {"type": "<app>__<type>", "writer": "app:<id>",
    "member": <slug>}}``; the ``type`` in the answer is the stored key, so an app that
    tells its user which fold path to bind can quote it. Every refusal carries a
    ``code``.
    """
    slug = request.match_info.get("slug", "")
    resolved, refusal = await _resolve_app_writer(request, slug)
    if refusal is not None:
        return refusal
    assert resolved is not None
    app_name = resolved.writer[len(APP_WRITER_PREFIX) :]
    # COUNTED FIRST, before the body is read, as the cron door counts: every answer
    # below costs the app a slot, so an app retrying a bad payload is throttled rather
    # than parsed and scrubbed once per retry.
    if _over_quota(resolved.writer):
        return _refuse(
            request,
            app_name,
            "quota_exceeded",
            f"this app has sent {RECORD_QUOTA_PER_MINUTE} records in the last minute; "
            "wait before sending more",
            429,
            operation=_RECORD_OPERATION,
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body", "code": "invalid_json"}, status=400)
    bad = _record_body_refusal(body)
    if bad is not None:
        code, error = bad
        return web.json_response({"error": error, "code": code}, status=400)
    # The key's one-way split rests on the app id being kebab-case. That is not
    # re-checked here: ``apps.manifest_names.app_name_error`` is the single contract
    # every admission path runs, so no live app token carries a name that fails it.
    stored_type = app_record_type(app_name, body["type"])
    if not custom_type_name_ok(stored_type):
        # The type passed the shared rule on its own; what can fail here is the LENGTH
        # once the app part is in front. The alphabet cannot: ``custom_type_name_ok``
        # admits ``[A-Za-z0-9_]`` only and the rewritten id is in that alphabet.
        return web.json_response(
            {
                "error": (
                    f"the record is stored under {stored_type!r} (the app id with '-' as "
                    "'_', then '__', then the type), which must be one plain name of at "
                    f"most {CUSTOM_TYPE_NAME_LIMIT} characters; shorten the type"
                ),
                "code": "bad_record_type",
            },
            status=400,
        )
    data: dict[str, Any] = body["data"]
    return await record_through_door(
        request, resolved, stored_type, data, caller=app_name, operation=_RECORD_OPERATION
    )
