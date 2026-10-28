"""The record door: ``POST /api/agent-panel/dashboard/record``.

A script cron bound to a crewmate hands the gateway one record -- a type name and an
object -- and the gateway appends it to that crewmate's own DM session log as ONE
``dashboard/custom_value`` entry, stamped with who wrote it. The ``custom`` fold keeps
the latest record per type, so a Dashboard page bound to
``custom.latest.<type>.data`` redraws when the record lands, with no model turn.

Modelled on the Issue Radar ledger's path (``crew_store_log.py``): one registered entry
type, appended gateway-side through :mod:`kiro_crew.crew_log.emit`, provenance stamped
by the gateway. What the caller may say is the record's TYPE and DATA; who wrote it,
for whom, and the envelope ``src`` are the gateway's to say and are never read from
the body.

WHO MAY WRITE is decided in one place, :func:`_resolve_record_writer`, and today the
answer is a script cron bound to a member and nothing else. An app-token caller is
turned away by the same ``_deny_app_caller`` every member route uses; an installed
app's own grant is a separate decision and is not made here.

:mod:`kiro_crew.dashboard.handlers.agent_panel` is this module's facade: callers and
tests import and patch every name through it (:mod:`kiro_crew._facade_mirror`). This
module never imports the facade.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Mapping
from typing import Any, Final, NamedTuple

from aiohttp import web

from kiro_crew import agent_panel
from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log.entry_types import (
    CUSTOM_ENTRY_TYPE,
    CUSTOM_RECORD_BYTES,
    CUSTOM_RECORD_DEPTH,
    CUSTOM_TYPE_NAME_LIMIT,
)
from kiro_crew.crew_log.projection_dashboard import (
    custom_record_depth,
    custom_record_fits,
    custom_type_name_ok,
)
from kiro_crew.dashboard.handlers._shared import member_request_scope
from kiro_crew.dashboard.handlers.agent_panel_read import _panel_slot
from kiro_crew.dashboard.handlers.members import _deny_app_caller
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.members import MemberSlugError
from kiro_crew.security import (
    REDACTED_CREDENTIAL_TAG,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel
from kiro_crew.session_ledger import _APPEND_FLUSH_SECONDS

logger = logging.getLogger("kiro_crew.dashboard.handlers.agent_panel")

#: Records one writer may land per minute. A script that polls every few seconds is
#: well inside it; what it bounds is a wedged script writing in a loop, which would
#: otherwise grow the crewmate's log by one line per iteration until retention cut it.
#: Refused with 429, never queued: a record a script is told to resend is one it still
#: holds, and a queue here would be a second buffer in front of the writer's own.
RECORD_QUOTA_PER_MINUTE: Final[int] = 30
_QUOTA_WINDOW_SECONDS: Final[float] = 60.0
#: Distinct writers the quota map holds at once. A BOUND on the one input nobody
#: controls -- how many jobs a gateway runs over its life -- so the map is sized by the
#: active window and not by history: a writer whose window has emptied is dropped on the
#: next call, and a NEW writer arriving while this many are still inside their windows
#: is refused rather than a live writer's count evicted. Far above any real fleet.
RECORD_QUOTA_WRITERS: Final[int] = 256

_OPERATION: Final[str] = "dashboard_record"

#: Per-writer landing times inside the window. Process memory, like the panel's
#: shared-key warning cap: the quota is a rate, and a rate that survived a restart
#: would refuse the first record after one for a minute nobody was writing in.
_recent_records: dict[str, deque[float]] = {}
_recent_records_guard = threading.Lock()


class RecordWriter(NamedTuple):
    """Who a record route call writes AS and FOR, resolved from the attested caller."""

    #: The provenance stamped on the entry: ``cron:<job id>``.
    writer: str
    #: The crewmate the writer is bound to -- its configured name, its slug, and the DM
    #: slot its Dashboard page folds under.
    crew_name: str
    slug: str
    slot: str


def _refuse(request: web.Request, sk: str, code: str, error: str, status: int) -> web.Response:
    """One refusal, audited the way the publish gate audits its own."""
    sel().log_api_access(
        caller=sk,
        operation=_OPERATION,
        outcome="denied",
        source="dashboard",
        resources=request.path,
        error=error,
    )
    return web.json_response({"error": error, "code": code}, status=status)


async def _resolve_record_writer(
    request: web.Request,
) -> tuple[RecordWriter, None] | tuple[None, web.Response]:
    """Vet the caller and resolve it to the writer it is, or the refusal it earns.

    The ONE place the door decides who may write. The identity comes from the
    transport-attested ``cron:<job id>`` key and the scheduler's own record of that job
    (``member_request_scope``), so a caller that merely asserts a header, or names a job
    the scheduler does not hold, resolves to nothing. The member comes from the job's
    captured execution context, never from the body: a script cannot write another
    crewmate's log by naming it.

    Ordered like ``_resolve_publishing_crew``: the internal secret first, then the
    operator's ``agent.crew_panel`` switch (a record is a dashboard value, so the switch
    that withdraws the dashboard surface withdraws this door with it), then the
    app-caller denial BEFORE any identity is read, then the cron's own binding.
    """
    sk = request.headers.get("X-Session-Key", "")
    if request.get("internal_auth") is not True:
        return None, _refuse(request, sk, "internal_secret_required", "forbidden", 403)
    if not await asyncio.to_thread(members_mod.crew_panel_enabled):
        return None, _refuse(
            request, sk, "crew_panel_disabled", "the crew dashboard is switched off", 403
        )
    denied = await _deny_app_caller(request, _OPERATION)
    if denied is not None:
        return None, denied
    scope = await member_request_scope(request)
    session = scope.session or ""
    if not scope.verified or not session.startswith("cron:"):
        return None, _refuse(
            request,
            sk,
            "caller_not_cron",
            "only a script cron bound to a crewmate can append a record; this caller is "
            "not an attested cron job",
            403,
        )
    member_id = getattr(scope.execution, "member_id", None) if scope.execution else None
    if not member_id:
        return None, _refuse(
            request,
            sk,
            "cron_not_member_bound",
            "this cron job is bound to no crewmate, so it has no crew log to append to; "
            "bind the job to a crewmate (its `member`) and run it again",
            403,
        )
    job_id = session.removeprefix("cron:").split(":")[0]

    def _resolve() -> RecordWriter:
        from kiro_crew.execution_context import member_config_for_id

        cfg = KiroCrewConfig.load()
        crew_name, _member = member_config_for_id(cfg, str(member_id))
        slug = members_mod.member_slug(crew_name, cfg)
        members_mod.validate_slug(slug)
        return RecordWriter(f"cron:{job_id}", crew_name, slug, _panel_slot(cfg, crew_name, slug))

    try:
        return await asyncio.to_thread(_resolve), None
    except MemberSlugError:
        return None, _refuse(
            request, sk, "bad_crew_slug", "this crew's name has no addressable slug", 400
        )
    except Exception:
        # ``member_config_for_id`` raises the execution context's own "unavailable"
        # error for a member id the config does not hold, or holds twice. Logged for
        # the operator; the caller is told the identity could not be resolved, which is
        # the answer every member route gives for this state.
        logger.warning("dashboard record: cron %s has an unresolvable member", job_id)
        return None, _refuse(
            request,
            sk,
            "member_identity_unavailable",
            "the crewmate this cron job is bound to could not be resolved",
            409,
        )


def _over_quota(writer: str, now: float | None = None) -> bool:
    """Count one record against *writer*'s window and say whether it was one too many.

    Counted at the door rather than after the append, so a refused record costs the
    writer a slot in the window too: a script retrying a bad payload in a loop is the
    very thing the quota is for.
    """
    moment = time.monotonic() if now is None else now
    with _recent_records_guard:
        # EVERY writer's window is trimmed, not only this one's, and a writer whose
        # window emptied is dropped: the map then holds the writers inside the last
        # minute and nothing a retired job left behind. O(writers) per call, bounded
        # by RECORD_QUOTA_WRITERS.
        for other in list(_recent_records):
            times = _recent_records[other]
            while times and moment - times[0] >= _QUOTA_WINDOW_SECONDS:
                times.popleft()
            if not times and other != writer:
                del _recent_records[other]
        landed = _recent_records.get(writer)
        if landed is None:
            if len(_recent_records) >= RECORD_QUOTA_WRITERS:
                return True
            landed = _recent_records[writer] = deque()
        if len(landed) >= RECORD_QUOTA_PER_MINUTE:
            return True
        landed.append(moment)
        return False


def _scrubbed(text: str) -> str:
    """One string scrubbed the way an agentic value is: exfiltration URLs, then credentials."""
    cleaned, _ = redact_exfiltration_urls(text)
    cleaned, _ = redact_credentials(cleaned)
    return cleaned


def _scrubbed_under(key: str, value: str) -> str:
    """*value* scrubbed as the value UNDER *key*.

    A credential pattern can need its key to match: ``Bearer <token>`` alone passes
    the redactor while ``authorization: Bearer <token>`` is caught, and a script that
    records an upstream response's headers writes exactly that pair. So the value is
    judged twice -- alone, then the cleaned result spelled beside its key as a header
    line spells it -- and a pair the second judgement catches is answered with the
    redactor's own tag in the value's place, so the key stays and the shape holds.
    """
    cleaned = _scrubbed(value)
    # The keyed judgement runs on the CLEANED value, always: a value carrying two tokens
    # -- one the bare scrub catches and one only its key reveals -- must not keep the
    # second because the first was found.
    spelled = f"{key}: {cleaned}"
    if _scrubbed(spelled) != spelled:
        return REDACTED_CREDENTIAL_TAG
    return cleaned


def _redacted_record(value: Any, key: str = "") -> Any:
    """*value* with every string in it scrubbed, shape kept -- the DOOR's redaction.

    Applied at the gateway and not left to the client, because ``ScriptContext`` is
    one caller of this route and not its gate: a script that posts through
    ``ctx._post`` itself sends whatever it holds. The record lands in the crewmate's log
    and renders on a page, so what lands is what is scrubbed, the posture
    ``dashboard_agentic._redacted`` takes for an agentic value, with one addition --
    a string value is judged beside its key (:func:`_scrubbed_under`). Keys are scrubbed
    too, and a key that scrubs onto one already present is suffixed rather than
    dropped. Non-string scalars pass through. The depth check runs first, so this
    recursion is bounded by :data:`CUSTOM_RECORD_DEPTH`.
    """
    if isinstance(value, str):
        return _scrubbed_under(key, value) if key else _scrubbed(value)
    if isinstance(value, list):
        return [_redacted_record(item, key) for item in value]
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        suffixes: dict[Any, int] = {}
        for raw_key, item in value.items():
            safe_key = _scrubbed(raw_key) if isinstance(raw_key, str) else raw_key
            safe_key = _unique_key(out, safe_key, suffixes)
            out[safe_key] = _redacted_record(item, raw_key if isinstance(raw_key, str) else "")
        return out
    return value


def _unique_key(out: Mapping[Any, Any], key: Any, suffixes: dict[Any, int]) -> Any:
    """*key*, or ``<key> (n)`` when *out* already holds it.

    One counter per colliding base in *suffixes*, so N keys that scrub to one name
    cost N steps and not N squared: each collision continues from the last suffix
    given out for that base instead of searching up from 2 again. A key that scrubs
    onto one already present is suffixed rather than dropped, so two distinct members
    do not collapse into one.
    """
    if key not in out:
        return key
    suffix = suffixes.get(key, 1) + 1
    while f"{key} ({suffix})" in out:
        suffix += 1
    suffixes[key] = suffix
    return f"{key} ({suffix})"


def _record_body_refusal(body: Any) -> tuple[str, str] | None:
    """``(code, error)`` for a body the door refuses, or ``None`` for one it takes.

    Only ``type`` and ``data`` are read. Anything else in the body is ignored rather
    than refused, so a client that adds a field later is not broken by an older
    gateway -- but no other key is ever read, which is what keeps ``writer``,
    ``member`` and ``crew_key`` the gateway's alone.
    """
    if not isinstance(body, dict):
        return "invalid_body", "body must be a JSON object"
    name = body.get("type")
    if not isinstance(name, str) or not custom_type_name_ok(name):
        return (
            "bad_record_type",
            "type must be one name of letters, digits and underscores only, at most "
            f"{CUSTOM_TYPE_NAME_LIMIT} characters, so a page can bind custom.latest.<type>",
        )
    if _scrubbed(name) != name:
        # The grammar admits a token-shaped string (a forty-character PAT is letters,
        # digits and underscores). A name the scrubber would change is a credential in
        # the one cell the door cannot scrub -- a type is a KEY, and a redacted key
        # names no record -- so it is refused rather than stored or echoed.
        return (
            "bad_record_type",
            "type looks like a credential; name the record for what it measures",
        )
    data = body.get("data")
    if not isinstance(data, dict):
        return "invalid_record", "data must be a JSON object"
    if custom_record_depth(data) > CUSTOM_RECORD_DEPTH:
        return (
            "record_too_deep",
            f"data nests deeper than {CUSTOM_RECORD_DEPTH} levels; a page binds a dotted "
            "path into it, so flatten it",
        )
    # The RAW record is measured before anything walks its strings: the scrub costs a
    # regex pass per string, so a record is admitted to it only once it is already
    # inside the byte ceiling -- which bounds the key count the scrub can meet. The
    # scrubbed form is measured again after, because a short token scrubs to a longer
    # tag and the fold re-measures what landed.
    return _record_size_refusal(data)


def _record_size_refusal(record: Mapping[str, Any]) -> tuple[str, str] | None:
    """``(code, error)`` for a scrubbed record over the byte ceiling, or ``None``.

    Measured on the record AS IT WILL LAND -- after the door's redaction -- because
    that is what the fold re-measures on read; a size judged on the raw body could
    pass a record the fold then drops.
    """
    if not custom_record_fits(record):
        return (
            "record_too_large",
            f"data must serialize to at most {CUSTOM_RECORD_BYTES} bytes; send fewer or "
            "shorter fields",
        )
    return None


async def api_dashboard_record(request: web.Request) -> web.Response:
    """POST /api/agent-panel/dashboard/record -- one custom record, for the caller's crewmate.

    Body ``{"type": <name>, "data": {...}}``. The order of the steps is the feature:

    1. resolve the WRITER from the attested caller (never from the body);
    2. count the call against the writer's quota, before the body is even read, so a
       refusal costs a slot too;
    3. check the record's shape and depth, scrub every string in it at the door, and
       judge its size on the scrubbed form, so a record that can never land is refused
       before it costs an append and what lands is what was measured;
    4. append it to the crewmate's DM log and wait for the writer's answer, so a record
       reported stored is one the fold will see.

    Answers ``{"ok": true, "recorded": {"type", "writer", "member"}}``. Every refusal
    carries a ``code`` the script can branch on through ``ScriptContext._exchange``.
    """
    resolved, refusal = await _resolve_record_writer(request)
    if refusal is not None:
        return refusal
    assert resolved is not None
    sk = request.headers.get("X-Session-Key", "")
    # COUNTED FIRST, before the body is read: every answer below, the refusals
    # included, costs the writer a slot, so a script retrying a bad payload in a loop
    # is throttled rather than parsed and scrubbed on the loop once per retry.
    if _over_quota(resolved.writer):
        return _refuse(
            request,
            sk,
            "quota_exceeded",
            f"this cron job has sent {RECORD_QUOTA_PER_MINUTE} records in the last "
            "minute; wait before sending more",
            429,
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body", "code": "invalid_json"}, status=400)
    bad = _record_body_refusal(body)
    if bad is not None:
        code, error = bad
        return web.json_response({"error": error, "code": code}, status=400)
    # SCRUBBED AT THE DOOR, after the shape, depth and raw-size checks bound the walk,
    # and OFF the event loop: the scrub is a regex pass per string over a record the
    # caller sized, and the loop serves every other route meanwhile. The scrubbed form
    # is measured again, so what lands and what is measured are the same bytes.
    record = await asyncio.to_thread(_redacted_record, body["data"])
    bad = _record_size_refusal(record)
    if bad is not None:
        code, error = bad
        return web.json_response({"error": error, "code": code}, status=400)
    if not crew_log_emit.enabled():
        # THE LOG IS THE RECORD'S ONLY HOME, as it is for an agentic value: a refusal,
        # not a shrug, because reporting success would promise a cell that never fills.
        return web.json_response(
            {
                "error": (
                    "this gateway is not recording a crew log, and a custom record has "
                    "nowhere else to live -- ask the human to switch the crew log on"
                ),
                "code": "crew_log_off",
            },
            status=503,
        )
    # STAMPED HERE. The body's ``type`` and ``data`` are the script's; everything else
    # on the entry is the gateway's reading of who called, and the body is not consulted
    # for any of it.
    entry: dict[str, Any] = {
        "type": body["type"],
        "data": record,
        "writer": resolved.writer,
        "member": resolved.slug,
        "crew_key": agent_panel.crew_key(resolved.crew_name),
    }

    def _append() -> str:
        # The crewmate's LIVE DM unit, the one its page reads. Imported here, not at
        # module scope, for the reason every other store call in these handlers is: the
        # crew log is an optional subsystem and the boot path must not load it.
        from kiro_crew.dashboard.handlers.member_dashboard import _newest_unit

        unit = _newest_unit(resolved.slot)
        if not unit:
            return "crew_log_unavailable"
        # ASKED BEFORE THE APPEND: an entry over the log's whole-line ceiling can never
        # land, so writing it would report a record as stored that no fold will see.
        if not crew_log_emit.dashboard_entry_fits(CUSTOM_ENTRY_TYPE, entry):
            return "record_too_large"
        if not crew_log_emit.on_dashboard_custom(unit, entry, timeout=_APPEND_FLUSH_SECONDS):
            return "append_failed"
        return ""

    outcome = await asyncio.to_thread(_append)
    if outcome == "crew_log_unavailable":
        return _refuse(
            request,
            sk,
            outcome,
            "this crewmate's thread has no crew log yet; open its chat once so its "
            "session starts, then run the job again",
            409,
        )
    if outcome == "record_too_large":
        return web.json_response(
            {
                "error": "this record does not fit one crew log entry; send fewer or shorter fields",
                "code": outcome,
            },
            status=400,
        )
    if outcome:
        return web.json_response(
            {
                "error": (
                    "the record could not be written to this crewmate's crew log; "
                    "send it again on the next run"
                ),
                "code": outcome,
            },
            status=503,
        )
    sel().log_api_access(
        caller=sk,
        operation=_OPERATION,
        outcome="allowed",
        source="dashboard",
        resources=f"{resolved.slug}:{body['type']}",
    )
    # The fold's own bus event drives an open page's refill; this frame is for a client
    # watching the panel surface, and it carries the SLUG only, like the write route's.
    state: DashboardState = request.app["state"]
    state.broadcast_ws("dashboard_value_written", {"slug": resolved.slug})
    return web.json_response(
        {
            "ok": True,
            "recorded": {"type": body["type"], "writer": resolved.writer, "member": resolved.slug},
        }
    )
