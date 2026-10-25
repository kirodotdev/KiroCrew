"""Creating, updating, enabling and deleting cron jobs.

The job routes (``POST /api/crons``, ``PATCH`` and ``DELETE /api/crons/{id}``, the
batch delete and the enable toggle) with their body cap, the one-shot ``at`` time
they resolve, the chat folder a job's tab is filed into, and ``POST /api/crons/tools``,
which runs a member session's cron tool call.

Served through :mod:`kiro_crew.dashboard.handlers.cron`, which callers and tests import and patch; this module
never imports it.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from aiohttp import web

from kiro_crew import model_registry
from kiro_crew.cron import CronStoreBusy, CronStoreUnreadable, is_valid_timezone, parse_time_string
from kiro_crew.cron_service.model import (
    validate_alert_after_failures,
    validate_auto_pause_after_failures,
)
from kiro_crew.dashboard.cron_inject import (
    chat_folder_exists,
    chat_folder_move_refuses_store,
    chat_folder_refuses_store,
    move_cron_job_tab,
)
from kiro_crew.dashboard.cron_timeout_field import timeout_secs_from_body
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.cron_shared import (
    _CRON_BUSY_BODY,
    _CRON_BUSY_STATUS,
    _app_caller,
    _cron_unreadable_response,
    _invalid_path_id_response,
    _refuse_foreign_app_job,
    _sel,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.validation import (
    _MODEL_NAME_RE,
    CHANNEL_ID_RE,
    CHANNEL_MAX_LEN,
    CRON_ADD_SCHEMA,
    MAX_CRON_MESSAGE,
    MAX_SHORT_STRING,
    FieldSpec,
    ValidationError,
    validate_string_field,
)

from ._shared import (
    _redact_memory_field,
    guard_owner_surface_routes,
    read_bounded_json,
    resolve_lesson_memory_store,
)

logger = logging.getLogger("kiro_crew.dashboard.handlers.cron")


_T = TypeVar("_T")


# Byte ceiling for a cron create/update body. The message field is bounded at
# MAX_CRON_MESSAGE characters, and one character costs at most 12 bytes on the
# wire: a client may send it JSON-escaped, and an astral character escapes to a
# \uXXXX\uXXXX surrogate pair (12 bytes), wider than raw UTF-8's 4-byte max. So
# this bounds the largest message the field validator will accept; the 64 KB of
# headroom covers the remaining short fields. Explicit rather than the shared
# default because a maximal multibyte message legitimately exceeds 64 KB.
_MAX_CRON_BODY_BYTES = 12 * MAX_CRON_MESSAGE + 64 * 1024


# ── Cron jobs ──


def _schema_field(field_name: str) -> FieldSpec | None:
    """Return *field_name*'s :class:`FieldSpec` from ``CRON_ADD_SCHEMA``, or ``None``.

    Every limit this route applies to a one-shot field is read through here
    rather than restated, because the route's contract is that it validates the
    same bodies the ``cron_add`` tool does: a copied limit is a limit that drifts
    the moment the schema's is retuned, and the drift reappears as the divergence
    this route exists to close. That covers the numeric bounds AND ``at_time``'s
    length — a longer string than the tool accepts is how an oversized duration
    reaches the parser in the first place.
    """
    for spec in CRON_ADD_SCHEMA.fields:
        if spec.name == field_name:
            return spec
    return None


def _resolve_one_shot_at(
    body: dict[str, Any], tz_name: str = ""
) -> tuple[float | None, web.Response | None]:
    """Resolve a one-shot fire time from ``at`` / ``delay`` / ``at_time``.

    Returns ``(at_ts, None)`` on success — with ``at_ts`` ``None`` when the body
    names no one-shot at all, which is the recurring case and not an error — or
    ``(None, response)`` carrying a 400 the caller returns verbatim.

    Mirrors ``cron_add``'s **parser and precedence**: ``at`` (absolute epoch
    seconds) wins, then ``delay`` (seconds from now), then ``at_time`` (human
    string, parsed by the shared :func:`parse_time_string` in *tz_name* -- the
    body's own, already-validated ``timezone`` -- or the CONFIGURED timezone
    when the body names none), so a one-shot body means the same instant
    whichever door received it. The acceptance sets are NOT identical: the
    resolved-instant ceiling below is stricter than the tool, which bounds only
    its raw fields.

    Four things this refuses that the declared type alone would let through:

    * a bool for ``at``/``delay`` — ``isinstance(True, int)`` is true in Python,
      so a bare ``isinstance`` check would silently read ``True`` as ``1``;
    * a value ``float()`` cannot even represent. JSON integers are unbounded, and
      ``float(10**400)`` raises ``OverflowError`` — uncaught, that is a bare 500
      on a request that never reaches the store;
    * a non-finite float — ``json.loads`` accepts ``NaN`` and ``Infinity`` by
      default, and ``NaN`` defeats every comparison below (each is false), which
      would persist a job whose next run can never arrive;
    * a value outside the schema's own bounds. An unbounded far-future ``at`` is
      not merely a silly job: ``format_schedule`` renders it through
      ``datetime.fromtimestamp``, which raises above year 9999, and it does so
      inside the comprehension that serializes EVERY job — so one poisoned
      record turns the whole cron listing into a 500 until it is deleted by id.
      ``{"at": 1.75e12}`` (epoch MILLIseconds — the ordinary ``Date.now()``
      mistake) is exactly that shape, which is why the bound is enforced here
      rather than left to the store;
    * a time already gone, which would otherwise fire the moment the scheduler
      next ticks rather than when the caller asked.
    """

    def _number(field: str) -> tuple[float | None, web.Response | None]:
        spec = _schema_field(field)

        def refuse(why: str) -> tuple[None, web.Response]:
            return None, web.json_response(
                {"error": f"'{field}' {why}", "code": f"invalid_{field}"}, status=400
            )

        raw = body.get(field)
        if raw is None:
            return None, None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return refuse("must be a number")
        try:
            val = float(raw)
        except OverflowError:
            # A JSON integer has no width limit, so this arrives from the wire.
            # Refused as out-of-range rather than propagating: it is by definition
            # past any ceiling the schema declares.
            return refuse("is too large")
        if not math.isfinite(val):
            return refuse("must be a finite number")
        lo = spec.min_val if spec else None
        hi = spec.max_val if spec else None
        if (lo is not None and val < lo) or (hi is not None and val > hi):
            return refuse(f"must be between {lo} and {hi}")
        return val, None

    at_ts, err = _number("at")
    if err is not None:
        return None, err
    if at_ts is None:
        delay, err = _number("delay")
        if err is not None:
            return None, err
        if delay is not None:
            at_ts = time.time() + delay
    if at_ts is None:
        # Length from the schema, not MAX_SHORT_STRING: a string longer than the
        # tool accepts is how an oversized relative duration ("in <hundreds of
        # digits> hours") reaches the parser, where the arithmetic overflows.
        at_time_spec = _schema_field("at_time")
        at_time_max = at_time_spec.max_len if at_time_spec and at_time_spec.max_len else 100
        try:
            at_time = validate_string_field(body, "at_time", max_len=at_time_max)
        except ValidationError as exc:
            return None, web.json_response(
                {"error": str(exc), "code": "invalid_at_time"}, status=400
            )
        if at_time:
            parsed = parse_time_string(at_time, tz_name)
            if isinstance(parsed, str):
                # parse_time_string reports failure as an already-prefixed
                # "Error: ..." string; strip the prefix so the JSON body is not
                # doubly labelled once the client reads `error`.
                return None, web.json_response(
                    {
                        "error": parsed.removeprefix("Error: "),
                        "code": "invalid_at_time",
                    },
                    status=400,
                )
            at_ts = parsed
    # The resolved instant carries the same ceiling as a raw ``at``, whatever
    # produced it. ``delay`` cannot escape its own bound, but ``at_time``'s
    # relative form has none — ``"in 999999999 hours"`` parses to a timestamp
    # ``datetime.fromtimestamp`` cannot render, which is the poisoned record that
    # 500s the listing. Bounding the raw fields alone would leave that door open.
    if at_ts is not None:
        at_spec = _schema_field("at")
        lo = at_spec.min_val if at_spec else None
        hi = at_spec.max_val if at_spec else None
        if (lo is not None and at_ts < lo) or (hi is not None and at_ts > hi):
            return None, web.json_response(
                {
                    "error": f"resolved time is outside the supported range ({lo} to {hi})",
                    "code": "at_out_of_range",
                },
                status=400,
            )
    if at_ts is not None and at_ts < time.time():
        return None, web.json_response(
            {"error": "requested time is in the past", "code": "at_in_past"},
            status=400,
        )
    return at_ts, None


async def api_cron_tools(request: web.Request) -> web.Response:
    """Run cron tools with ordinary authenticated session routing."""
    if request.get("internal_auth") is not True:
        return web.json_response(
            {
                "error": "An authenticated internal connection is required.",
                "code": "internal_auth_required",
            },
            status=403,
        )
    from kiro_crew.member_memory_auth import memory_request_identity

    actual, verified = await asyncio.to_thread(memory_request_identity, request)
    if not verified or not actual or actual != request.headers.get("X-Session-Key", ""):
        return web.json_response(
            {
                "error": "This caller's session could not be determined. "
                "Reopen the conversation and retry.",
                "code": "member_session_unverified",
            },
            status=403,
        )
    state = request.app["state"]
    # Capture canonical routing and mode once before tool dispatch.
    store, refusal = await resolve_lesson_memory_store(request, state, "cron.tools")
    if refusal is not None:
        return refusal
    body, error = await read_bounded_json(request, max_bytes=_MAX_CRON_BODY_BYTES)
    if error is not None:
        return error
    assert body is not None
    from kiro_crew import mcp_cron
    from kiro_crew.mcp_caller import CallerContext, current_caller, set_current_caller

    name, arguments = body.get("name"), body.get("arguments")
    if (
        set(body) != {"name", "arguments"}
        or not isinstance(name, str)
        or name not in {tool["name"] for tool in mcp_cron._list_tools()}
        or not isinstance(arguments, dict)
    ):
        return web.json_response(
            {
                "error": "Provide a known cron tool and an arguments object.",
                "code": "invalid_cron_tool",
            },
            status=400,
        )

    def dispatch() -> str:
        # ContextVars are isolated by to_thread. Restore the prior value even
        # on failure; no caller identity survives into a later request.
        previous = current_caller()
        set_current_caller(
            CallerContext(
                session_key=actual, session_type=actual.partition(":")[0], from_gateway=True
            )
        )
        try:
            return mcp_cron._call_tool_locally(name, arguments)
        finally:
            set_current_caller(previous)

    try:
        result = await asyncio.to_thread(dispatch)
    except Exception:
        logger.exception("Cron tool dispatch failed")
        return web.json_response(
            {
                "error": "The cron tool did not finish. Check cron_list before retrying a mutation.",
                "code": "cron_tool_failed",
            },
            status=503,
        )
    state.push_refresh("crons")
    return web.json_response({"result": result})


def _resolve_chat_folder_id(
    state: DashboardState, value: object
) -> tuple[str, web.Response | None]:
    """Validate a submitted ``chat_folder_id``: ``(id, None)`` or ``("", 400)``.

    ONE validator for create and update, because the two halves of the check
    answer different questions and only one of them is a type check:

    * shape -- ``None`` means "not filed" (so a client can clear the field by
      sending null), anything non-string or over-cap is a 400, matching how
      ``folder_id`` is handled two fields over;
    * EXISTENCE -- an id naming no folder in the sidebar's tree is refused here,
      at save time. The runtime treats a dangling id as "not filed" by contract
      (a folder can be deleted after the job is saved, and a run must not fail
      over that), but a save is the one moment a person is present to be told.
      Accepting an unknown id would persist a setting whose only observable
      behaviour is a log line nobody reads.
    """
    if value is None:
        return "", None
    if not isinstance(value, str) or len(value) > MAX_SHORT_STRING:
        return "", web.json_response(
            {"error": "invalid chat_folder_id format", "code": "invalid_chat_folder_id"},
            status=400,
        )
    folder_id = value.strip()
    if not folder_id:
        return "", None
    if not chat_folder_exists(state, folder_id):
        return "", _unknown_chat_folder_response()
    return folder_id, None


def _unknown_chat_folder_response() -> web.Response:
    # Shown verbatim under the Schedule form's Save button, so it names the
    # next step rather than only the fact: the reader picked a folder that
    # has since been deleted, and the list they picked from is stale.
    return web.json_response(
        {
            "error": (
                "That chat folder does not exist. Retry the folder list and pick "
                "another, or choose not to file runs."
            ),
            "code": "unknown_chat_folder",
        },
        status=400,
    )


def _refuse_mate_home_chat_folder(
    request: web.Request,
    state: DashboardState,
    chat_folder_id: str,
    memory_store: str,
    operation: str,
    current_chat_folder_id: str = "",
) -> web.Response | None:
    """A 403 ``mate_home_locked`` when the job's tab could never be filed in *chat_folder_id*.

    A mate's home and the folders inside it hold only that mate's sessions, and a
    job's tab runs on the job's memory store: a job on any other store would be
    saved with a folder its runs are never placed in. *current_chat_folder_id*
    is the folder an existing job is filed in: moving its tab out of a mate's
    home its store may not write is refused the same way. Refused here, while a
    person is present to be told, and recorded like the routes' other denials.
    """
    if not chat_folder_move_refuses_store(
        state, current_chat_folder_id, chat_folder_id, memory_store
    ):
        return None
    moving_out = not chat_folder_refuses_store(state, chat_folder_id, memory_store)
    app = _app_caller(request)
    resources = f"chat_folder_id={chat_folder_id}"
    if moving_out:
        resources += f",current_chat_folder_id={current_chat_folder_id}"
    try:
        _sel().log_api_access(
            caller=f"app:{app}" if app else "dashboard",
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources=resources,
            error="mate_home_locked",
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for %s failed", operation, exc_info=True)
    error = (
        "This schedule's runs are filed in a mate's folder, and only that mate can "
        "move them out."
        if moving_out
        else "That chat folder belongs to a mate, so this schedule's runs cannot be "
        "filed there. Pick another folder, or choose not to file runs."
    )
    return web.json_response({"error": error, "code": "mate_home_locked"}, status=403)


def _member_store(member_id: str) -> str:
    """The memory store a schedule bound to *member_id* runs on, or ``""``. Blocking.

    ``""`` (the person's) also for a member that does not resolve: the create
    refuses that member anyway, and ``""`` is kept out of every mate's home.
    """
    if not member_id:
        return ""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.execution_context import resolve_member_execution

    try:
        execution = resolve_member_execution(KiroCrewConfig.load(), member_id)
    except Exception:
        return ""
    return execution.store.legacy_name


class _ChatFolderGone(Exception):
    """The job's chat folder was deleted before the save could commit."""


# Named by the path its callers reach it through, as before the split.
_ChatFolderGone.__module__ = "kiro_crew.dashboard.handlers.cron"


async def _persist_holding_folder(
    state: DashboardState, folder_id: str, persist: Callable[[], Awaitable[_T]]
) -> _T:
    """Run *persist* (a job write) while the folder-store lock is held.

    A job that names a chat folder is checked and written inside one hold of
    that lock, the lock the folder cleanup also takes to read saved jobs and
    delete. So either this save commits first and the cleanup sees the job, or
    the cleanup deletes first and the re-check here refuses the save. Without a
    folder there is nothing to exclude and the write runs as before.
    """
    if not folder_id:
        return await persist()

    async def _section(folders: list[dict[str, Any]]) -> _T:
        if not any(str(f.get("id")) == folder_id for f in folders):
            raise _ChatFolderGone()
        return await persist()

    return await state.hold_folders(_section)


async def api_crons_create(request: web.Request) -> web.Response:
    """POST /api/crons — create a cron job."""
    # Owner identity is a property of a dashboard-user request: ``app == ""`` is
    # the class ``is_owner_dashboard_request`` can rule on at all. An app token
    # carries a non-empty name and stays confined to its manifest's declared
    # paths by ``_enforce_app_scope`` -- ``POST /api/crons`` is one of those
    # declarable paths (see ``docs/app-kit/api-reference.md``). No
    # ``internal_auth`` clause: this route has no ``X-Internal-Secret`` caller,
    # and a clause naming one would exempt a future caller nobody reviewed.
    # The test is ``not app`` rather than ``app == ""``: an internal-secret
    # request carries no ``app`` key at all, and must meet the owner gate too
    # (same shape as the file and workflow routes) -- this and the sibling
    # delete/batch-delete/update/enable gates below.
    if not request.get("app"):
        # Body-scope import, like the sibling gates in this package
        # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
        # reaches back into sibling handler modules, so importing the helper at
        # module scope from here would close a cycle.
        from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

        owner_denied = await require_owner_dashboard_request(request, "crons.create")
        if owner_denied is not None:
            return owner_denied
    state: DashboardState = request.app["state"]
    # Per-route cap: the body carries the job's full agent message/prompt text,
    # whose field bound (MAX_CRON_MESSAGE chars) can exceed the shared 64 KB
    # default in multibyte UTF-8 -- _MAX_CRON_BODY_BYTES sizes the ceiling to it.
    body, body_err = await read_bounded_json(request, max_bytes=_MAX_CRON_BODY_BYTES)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    # Type-validate every string field BEFORE calling string methods on it.
    # A JSON array/dict/int in these fields would otherwise raise AttributeError
    # (.strip() on a non-str) -> HTTP 500. validate_string_field enforces
    # isinstance(str), sanitizes, and bounds length, mirroring the MCP
    # CRON_ADD_SCHEMA so the REST + tool paths validate identically.
    try:
        name = validate_string_field(body, "name", required=True, max_len=MAX_SHORT_STRING)
        message = validate_string_field(body, "message", max_len=MAX_CRON_MESSAGE)
        schedule = validate_string_field(body, "schedule", max_len=100)
        cron_expr = validate_string_field(body, "cron", max_len=100) or None
        channel = validate_string_field(body, "channel", max_len=CHANNEL_MAX_LEN) or None
        approval_mode = validate_string_field(body, "approval_mode", max_len=10)
        timezone_val = validate_string_field(body, "timezone", max_len=50)
        agent_id = validate_string_field(body, "agent", max_len=MAX_SHORT_STRING)
        source_preset = validate_string_field(body, "source_preset", max_len=MAX_SHORT_STRING)
        source_template_prompt = validate_string_field(
            body, "source_template_prompt", max_len=MAX_CRON_MESSAGE
        )
        member_id = validate_string_field(body, "member_id", max_len=MAX_SHORT_STRING)
    except ValidationError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    if not name or not message:
        return web.json_response({"error": "name and message required"}, status=400)
    every = body.get("every")
    if not every and not cron_expr and schedule:
        # Treat schedule string as cron expr if 5-field, else as interval
        cron_expr = schedule if len(schedule.split()) == 5 else None
    # One-shot scheduling, mirroring cron_add's `at` / `delay` / `at_time`.
    # Precedence matches the tool exactly (`at` wins, then `delay`, then
    # `at_time`) so the same request body cannot mean two different instants
    # depending on which entry point received it. The timezone is checked first
    # because it is the zone an `at_time` clock time is read in.
    if timezone_val and not is_valid_timezone(timezone_val):
        safe_tz, _ = redact_credentials(redact_exfiltration_urls(timezone_val)[0])
        return web.json_response({"error": f"invalid timezone: {safe_tz!r}"}, status=400)
    at_ts, at_err = _resolve_one_shot_at(body, timezone_val)
    if at_err is not None:
        return at_err
    if channel and not CHANNEL_ID_RE.match(channel):
        return web.json_response({"error": "invalid channel ID format"}, status=400)
    if approval_mode and approval_mode not in {"", "auto"}:
        return web.json_response({"error": "invalid approval_mode"}, status=400)
    silent = body.get("silent", False)
    strict_schedule = body.get("strict_schedule", False)
    # ``False`` creates the job already paused in the one locked write, so a
    # caller that must never leave it running (an imported crewmate's cron)
    # has no enabled-then-paused window. Only a real bool is honoured.
    enabled = body.get("enabled", True)
    if not isinstance(enabled, bool):
        return web.json_response(
            {"error": "enabled must be a boolean", "code": "invalid_enabled"}, status=400
        )
    hide_in_chat = body.get("hide_in_chat", False)
    # A job created on a full context pays for memory, lessons, steering, skills
    # and prior history on every wake, whether or not the wake had anything to
    # do. The store has carried this flag since the tool path gained it; only
    # this handler dropped it, so a job created from the dashboard could not opt
    # out of that cost without a later edit from chat or the CLI.
    minimal_context = body.get("minimal_context", False)
    # Whether every run resumes one long-lived ``cron:<job_id>`` session (the
    # store's and the tool path's default) or gets a fresh one. Defaults to True
    # here too, so a client that never sends the field keeps creating the same
    # persistent jobs it always did; only an explicit False opts a job out.
    persistent_session = body.get("persistent_session", True)
    timeout_secs, timeout_err = timeout_secs_from_body(body)
    if timeout_err is not None:
        return timeout_err
    # Same folder_id contract as PATCH /api/crons/{id}: string or null → "",
    # anything else is a 400 so the two entry points cannot diverge.
    folder_id = body.get("folder_id", "")
    if folder_id is None:
        folder_id = ""
    elif not isinstance(folder_id, str) or len(folder_id) > MAX_SHORT_STRING:
        return web.json_response(
            {"error": "invalid folder_id format", "code": "invalid_folder_id"},
            status=400,
        )
    # The CHAT folder the job's tab is filed into -- a different tree from
    # folder_id's, which groups the job's row on the Schedule page.
    chat_folder_id, chat_folder_err = _resolve_chat_folder_id(state, body.get("chat_folder_id"))
    if chat_folder_err is not None:
        return chat_folder_err
    # Validate model BEFORE add_job so an invalid value never leaves an
    # orphaned job behind (a retried create would then duplicate it).
    model_raw = body.get("model")
    if model_raw is not None and not isinstance(model_raw, str):
        # A numeric/bool JSON `model` would raise AttributeError on .strip()
        # (HTTP 500); reject it as a clean 400 instead.
        return web.json_response({"error": "invalid model format"}, status=400)
    model_val = (model_raw or "").strip()
    if model_val:
        if len(model_val) > MAX_SHORT_STRING or not _MODEL_NAME_RE.match(model_val):
            return web.json_response({"error": "invalid model format"}, status=400)
        # No membership gate: the model dropdown is sourced from the live
        # kiro-cli `--list-models` (via /api/models), not the claude_code
        # registry family, so any well-formed id the CLI advertises is valid.
        # Matches the chat model path (which also skips membership); the
        # runtime is model-agnostic with a gateway fallback. Only normalize
        # the "auto" inherit sentinel below.
        resolved_model = model_registry.to_provider_id(model_val, "claude_code")
        if resolved_model == "":
            # "auto" sentinel (canonical key with no pinned provider id):
            # explicit inherit — same as leaving model unset.
            model_val = ""
    # Build the job FULLY-FORMED in a single locked add_job_async transaction.
    # Passing every optional field into the locked build+persist (rather than
    # mutating the returned job and calling a bare, unlocked `_save()`) closes
    # the data-loss race: two concurrent creates could interleave at the
    # `await`, and the unlocked save could overwrite the other request's job.
    # The UI resolves a member pick to ``member_id`` client-side, but a scripted
    # POST can still send a member NAME in ``agent``; resolve the split here so
    # that path records a member selection too, not a template id the cron chat's
    # reply path cannot bind. An explicit ``member_id`` is left as the caller set it.
    # The split reads the member roster from the configuration on disk, so it runs
    # on a worker thread and never holds the event loop.
    from kiro_crew.cron_service.identity import split_cron_agent_member

    agent_id, member_id = await asyncio.to_thread(
        split_cron_agent_member, agent_id or "", member_id or ""
    )
    # Kept out of a mate's home even as the person; the member is resolved only
    # when the folder is in one.
    if chat_folder_refuses_store(state, chat_folder_id, ""):
        refused = _refuse_mate_home_chat_folder(
            request,
            state,
            chat_folder_id,
            await asyncio.to_thread(_member_store, member_id or ""),
            "crons.create",
        )
        if refused is not None:
            return refused
    add_kwargs: dict[str, Any] = {
        "channel": channel,
        "agent_id": (agent_id or ""),
        "member_id": member_id or "",
        "model": model_val,
        "silent": bool(silent),
        "timezone": (timezone_val or ""),
        "strict_schedule": bool(strict_schedule),
        "hide_in_chat": bool(hide_in_chat),
        "minimal_context": bool(minimal_context),
        "persistent_session": bool(persistent_session),
        "folder_id": folder_id,
        "chat_folder_id": chat_folder_id,
        # Dashboard-only template provenance (see CronJob.source_preset). The
        # prompt SNAPSHOT is what makes the Schedule-page "template updated"
        # hint attributable: comparing it against the template's current prompt
        # detects a template that moved, distinct from a user who edited their
        # own copy. Both "" for a blank create. Never gate execution.
        "source_preset": (source_preset or ""),
        "source_template_prompt": (source_template_prompt or ""),
    }
    if approval_mode:
        add_kwargs["approval_mode"] = approval_mode
    if not enabled:
        add_kwargs["enabled"] = False
    if timeout_secs is not None:
        add_kwargs["timeout_secs"] = timeout_secs
    # An app token's job is stamped as that app's, exactly as ``CronSDK`` stamps
    # it: without the stamp every later reader (the app's own PATCH/DELETE, the
    # cron session's scope, the disabled-app fire gate, uninstall cleanup) takes
    # the job for the person's. Host-written from the verified claim, never the
    # body, so an app cannot claim another's jobs.
    app_claim = request.get("app")
    if isinstance(app_claim, str) and app_claim:
        from kiro_crew.apps.cron_sdk import owner_tag

        add_kwargs["created_by"] = owner_tag(app_claim)
    # Which schedule this job carries. Resolved to kwargs FIRST, then handed to a
    # single add_job_async call: one call site means the store-failure handling
    # below is written once and cannot drift between the three schedule shapes.
    schedule_kwargs: dict[str, Any]
    if every:
        try:
            every = int(every)
        except (ValueError, TypeError):
            return web.json_response(
                {"error": "'every' must be an integer", "code": "invalid_every"}, status=400
            )
        schedule_kwargs = {"every_secs": every}
    elif cron_expr:
        schedule_kwargs = {"cron_expr": cron_expr}
    elif at_ts is not None:
        # One-shot: `delete_after_run` is derived here rather than accepted from
        # the body, exactly as cron_add derives it (`delete_after_run=bool(at_ts)`).
        # A caller-supplied flag would allow a job with a single fire time that
        # never leaves the store, which the scheduler has no way to run again.
        schedule_kwargs = {"at_ts": at_ts, "delete_after_run": True}
    else:
        return web.json_response(
            {
                "error": "schedule, every, cron, at, delay, or at_time required",
                "code": "missing_schedule",
            },
            status=400,
        )
    try:
        job = await _persist_holding_folder(
            state,
            chat_folder_id,
            lambda: state.crons.add_job_async(name, message, **schedule_kwargs, **add_kwargs),
        )
    except _ChatFolderGone:
        return _unknown_chat_folder_response()
    except CronStoreBusy:
        return web.json_response(_CRON_BUSY_BODY, status=_CRON_BUSY_STATUS)
    except CronStoreUnreadable as exc:
        return _cron_unreadable_response(exc)
    except ValueError as exc:
        # Member binding is validated inside the locked transaction, before
        # append/save. Surface that refusal for every schedule form without
        # publishing a success refresh or retrying against Global memory.
        return web.json_response(
            {"error": _redact_memory_field(str(exc)), "code": "invalid_cron"}, status=400
        )
    state.push_refresh("crons")
    from kiro_crew.dashboard.change_cards import cron_job_revision

    # The fingerprint of the job as this write made it: a card step records it,
    # so its Undo's baseline is what the card made, not a later edit.
    return web.json_response({"ok": True, "id": job.id, "revision": cron_job_revision(job)})


async def api_cron_delete(request: web.Request) -> web.Response:
    """DELETE /api/crons/{id} — remove a cron job."""
    if not request.get("app"):
        owner_denied = await require_owner_dashboard_request(request, "crons.delete")
        if owner_denied is not None:
            return owner_denied
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    if (_e := _invalid_path_id_response(job_id, "job_id")) is not None:
        return _e
    app_denied = await _refuse_foreign_app_job(request, state, [job_id], "crons.delete")
    if app_denied is not None:
        return app_denied
    from kiro_crew.dashboard.handlers.change_cards import REQ_CARD_UNDO_CRON_EXPECT

    expected_revision = request.get(REQ_CARD_UNDO_CRON_EXPECT)
    try:
        if isinstance(expected_revision, str):
            # Card Undo of a schedule it created: delete only while the job
            # still fingerprints to what the card left, compared under the
            # cron store lock so an edit another tab landed after the Undo's
            # snapshot check cannot be clobbered.
            from kiro_crew.dashboard.change_cards import cron_job_revision

            outcome = await state.crons.remove_job_if_async(
                job_id,
                expected_revision=expected_revision,
                revision_of=cron_job_revision,
                actor="dashboard",
                source="api_cron_delete",
            )
            if outcome == "changed":
                return web.json_response(
                    {
                        "error": "this changed after the card was applied; undo refused",
                        "code": "changed_since_apply",
                    },
                    status=409,
                )
            ok = outcome == "removed"
        else:
            ok = await state.crons.remove_job_async(
                job_id, actor="dashboard", source="api_cron_delete"
            )
    except CronStoreBusy:
        return web.json_response(_CRON_BUSY_BODY, status=_CRON_BUSY_STATUS)
    except CronStoreUnreadable as exc:
        return _cron_unreadable_response(exc)
    if ok:
        state.push_refresh("crons")
    return web.json_response({"ok": ok})


# Guardrail: cap batch size so a runaway/hostile payload can't pin the event
# loop deleting thousands of jobs (each remove_job is a sync save + async
# history delete). 500 comfortably exceeds any realistic schedule list.
_MAX_BATCH_DELETE = 500


async def api_cron_batch_delete(request: web.Request) -> web.Response:
    """DELETE /api/crons — remove multiple cron jobs in one call.

    Body: ``{"ids": ["<job_id>", ...]}``. Each id is removed independently so a
    missing/already-deleted id (e.g. a stale UI selection, or a concurrent
    delete) lands in ``failed`` rather than aborting the whole batch. History is
    purged per successfully-removed job, mirroring the single-delete path, and a
    single ``crons`` refresh is pushed after the batch instead of one per id.
    """
    if not request.get("app"):
        owner_denied = await require_owner_dashboard_request(request, "crons.batch_delete")
        if owner_denied is not None:
            return owner_denied
    state: DashboardState = request.app["state"]
    # Default cap: the body is a bounded list of short job ids.
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        return web.json_response({"error": "ids must be a non-empty array"}, status=400)
    if not all(isinstance(i, str) for i in ids):
        return web.json_response({"error": "ids must be an array of strings"}, status=400)
    # De-duplicate while preserving order (a select-all + click race can send dupes).
    unique_ids = list(dict.fromkeys(ids))
    if len(unique_ids) > _MAX_BATCH_DELETE:
        return web.json_response({"error": f"too many ids (max {_MAX_BATCH_DELETE})"}, status=400)
    app_denied = await _refuse_foreign_app_job(request, state, unique_ids, "crons.batch_delete")
    if app_denied is not None:
        return app_denied
    deleted: list[str] = []
    failed: list[str] = []
    try:
        # remove_jobs runs the WHOLE batch under one file lock with one
        # reload/serialize/save — and offloads that disk work to a worker
        # thread (no-blocking-call-on-event-loop; slow/network storage would
        # otherwise stall chat + heartbeat). Only its _arm_timer() step runs
        # back on the loop (asyncio.create_task needs it): moving it off-loop
        # would raise AFTER the on-disk delete and leave the scheduler timer
        # cancelled.
        deleted, failed = await state.crons.remove_jobs(
            unique_ids, actor="dashboard", source="api_cron_batch_delete"
        )
    except Exception:
        # The batch itself raised (unexpected) — report everything as failed.
        logger.warning("Batch delete failed", exc_info=True)
        failed = unique_ids
        deleted = []
    if deleted:
        state.push_refresh("crons")
    # ok reflects whether anything was actually deleted — consistent with the
    # single-delete endpoint (ok:false when the job didn't exist) and the audit
    # line above, so callers can detect a fully-failed batch without inspecting
    # the arrays.
    return web.json_response({"ok": len(deleted) > 0, "deleted": deleted, "failed": failed})


async def api_cron_update(request: web.Request) -> web.Response:
    """PATCH /api/crons/{id} — update a cron job (partial)."""
    if not request.get("app"):
        owner_denied = await require_owner_dashboard_request(request, "crons.update")
        if owner_denied is not None:
            return owner_denied
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    if (_e := _invalid_path_id_response(job_id, "job_id")) is not None:
        return _e
    app_denied = await _refuse_foreign_app_job(request, state, [job_id], "crons.update")
    if app_denied is not None:
        return app_denied
    # Per-route cap: a partial update can carry the job's full agent
    # message/prompt text, whose field bound (MAX_CRON_MESSAGE chars) can
    # exceed the shared 64 KB default in multibyte UTF-8. The helper also owns
    # the non-object 400: a syntactically valid scalar or array parses fine
    # and then has no .get, so the field reads below would raise
    # AttributeError and surface as a 500.
    body, body_err = await read_bounded_json(request, max_bytes=_MAX_CRON_BODY_BYTES)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    kwargs: dict[str, Any] = {}
    for key in (
        "name",
        "message",
        "channel",
        "approval_mode",
        "silent",
        "strict_schedule",
        "hide_in_chat",
        "minimal_context",
        "persistent_session",
        "folder_id",
        "chat_folder_id",
    ):
        if key in body:
            kwargs[key] = body[key]
    timeout_secs, timeout_err = timeout_secs_from_body(body)
    if timeout_err is not None:
        return timeout_err
    if timeout_secs is not None:
        kwargs["timeout_secs"] = timeout_secs
    # name routes through the same validator as POST (type check +
    # sanitize_string + length cap) so the two REST surfaces cannot diverge:
    # without it PATCH would pass the value through unvalidated, letting a
    # non-string or oversize name persist verbatim into crons.json.
    if "name" in kwargs:
        try:
            kwargs["name"] = validate_string_field(body, "name", max_len=MAX_SHORT_STRING)
        except ValidationError as exc:
            return web.json_response({"error": str(exc), "code": "invalid_name"}, status=400)
    # message routes through the same validator as POST (type check +
    # sanitize_string + length cap) so the two REST surfaces cannot diverge:
    # without it PATCH would pass the value through unvalidated. Sanitizing here
    # also keeps length measured post-normalization, matching create.
    if "message" in kwargs:
        try:
            kwargs["message"] = validate_string_field(body, "message", max_len=MAX_CRON_MESSAGE)
        except ValidationError as exc:
            return web.json_response({"error": str(exc), "code": "invalid_message"}, status=400)
    # folder_id must be a string (or null → ""): a non-string JSON value
    # would be persisted verbatim into the schema and corrupt reads.
    if "folder_id" in kwargs:
        fid = kwargs["folder_id"]
        if fid is None:
            kwargs["folder_id"] = ""
        elif not isinstance(fid, str) or len(fid) > MAX_SHORT_STRING:
            return web.json_response(
                {"error": "invalid folder_id format", "code": "invalid_folder_id"},
                status=400,
            )
    # Filled by the store, under the same lock as the write, with the folder
    # `chat_folder_id` held before this request replaced it -- and owned by THIS
    # request, so a concurrent update cannot clobber the answer. The job's chat tab
    # follows the change below on the strength of it.
    chat_folder_transition: dict[str, str] = {}
    if "chat_folder_id" in kwargs:
        resolved, chat_folder_err = _resolve_chat_folder_id(state, kwargs["chat_folder_id"])
        if chat_folder_err is not None:
            return chat_folder_err
        kwargs["chat_folder_id"] = resolved
    # The folder the job's tab is moved to: an explicit one, or none when
    # persistence is turned off (the store then clears a filed job's folder).
    next_chat_folder: str | None = kwargs.get("chat_folder_id")
    if next_chat_folder is None and "persistent_session" in kwargs:
        next_chat_folder = "" if not kwargs["persistent_session"] else None
    # Function-local: ``mate_folders`` reaches this module back through the folder records.
    from kiro_crew.dashboard.mate_folders import is_mate_home

    if next_chat_folder is not None and any(is_mate_home(folder) for folder in state._folders):
        # The move may go into or out of a mate's home. A schedule's member, and so its store, is
        # fixed once created, so the store the job already runs on decides.
        existing = await state.crons.get_job_async(job_id)
        # An unreadable store loads as no job, which would skip the refusal.
        try:
            state.crons.raise_if_store_unreadable()
        except CronStoreUnreadable as exc:
            return _cron_unreadable_response(exc)
        if existing is not None:
            refused = _refuse_mate_home_chat_folder(
                request,
                state,
                next_chat_folder,
                str(getattr(existing, "memory_store", "") or ""),
                "crons.update",
                current_chat_folder_id=str(getattr(existing, "chat_folder_id", "") or ""),
            )
            if refused is not None:
                return refused
    if "chat_folder_id" in kwargs or "persistent_session" in kwargs:
        # Turning persistence off clears a filed job's folder in the store, so
        # that edit moves the tab through the same path an explicit clear does.
        kwargs["chat_folder_transition_out"] = chat_folder_transition
    # UI sends "agent"; internal kwarg is "agent_id". Accept "agent_id" for scripted callers.
    if "member_id" in body:
        try:
            kwargs["member_id"] = validate_string_field(body, "member_id", max_len=MAX_SHORT_STRING)
        except ValidationError as exc:
            return web.json_response({"error": str(exc), "code": "invalid_member_id"}, status=400)
    # Normalize whitespace and coerce null so update and create persist the same value.
    if "agent" in body:
        kwargs["agent_id"] = (body["agent"] or "").strip()
    elif "agent_id" in body:
        kwargs["agent_id"] = (body["agent_id"] or "").strip()
    if "model" in body:
        model_raw = body["model"]
        if model_raw is not None and not isinstance(model_raw, str):
            # Non-string JSON `model` would raise on .strip() (HTTP 500) — 400.
            return web.json_response({"error": "invalid model format"}, status=400)
        m = (model_raw or "").strip()
        if m:
            if len(m) > MAX_SHORT_STRING or not _MODEL_NAME_RE.match(m):
                return web.json_response({"error": "invalid model format"}, status=400)
            # No membership gate: the model dropdown is sourced from the live
            # kiro-cli `--list-models` (via /api/models), not the claude_code
            # registry family, so any well-formed id the CLI advertises is
            # valid. Matches the chat model path (which also skips membership);
            # the runtime is model-agnostic with a gateway fallback. Only
            # normalize the "auto" inherit sentinel below.
            resolved_model = model_registry.to_provider_id(m, "claude_code")
            if resolved_model == "":
                m = ""
        kwargs["model"] = m
    # Validate channel if being updated
    if "channel" in kwargs:
        ch = (kwargs["channel"] or "").strip() or None
        kwargs["channel"] = ch
        if ch and (len(ch) > CHANNEL_MAX_LEN or not CHANNEL_ID_RE.match(ch)):
            return web.json_response({"error": "invalid channel ID format"}, status=400)
    # Schedule: accept cron_expr or every (seconds)
    if "cron" in body:
        kwargs["cron_expr"] = body["cron"]
    if "every" in body:
        kwargs["every_secs"] = body["every"]
    if "timezone" in body:
        tz_val = (body["timezone"] or "").strip()
        if tz_val and not is_valid_timezone(tz_val):
            safe_tz, _ = redact_credentials(redact_exfiltration_urls(tz_val)[0])
            return web.json_response({"error": f"invalid timezone: {safe_tz!r}"}, status=400)
        kwargs["timezone"] = tz_val
    # Checked here, before the store is touched, so a bad value is a 400 the
    # form can name rather than a ValueError from the write. ``null`` is refused
    # too: the store reads ``None`` as "leave unchanged", which would turn a
    # malformed request into a silent no-op.
    if "auto_pause_after_failures" in body:
        try:
            kwargs["auto_pause_after_failures"] = validate_auto_pause_after_failures(
                body["auto_pause_after_failures"]
            )
        except ValueError as exc:
            return web.json_response(
                {"error": str(exc), "code": "invalid_auto_pause_after_failures"}, status=400
            )
        # The store files the limit-change audit row under this name, so it
        # names who made the write: an app token's write is filed under
        # ``app:<name>``, the name ``_refuse_foreign_app_job`` records for the
        # same request, and an owner write keeps the forwarded marker
        # ``audit_actor`` adds. A fixed "dashboard" would file an app turning
        # off its own job's breaker as the person's action.
        # Function-local, as for ``_apply_security_headers`` in portability.py:
        # ``audit_actor``'s module reads its helpers from ``dashboard.server``,
        # which imports the handlers.
        from kiro_crew.dashboard.server import audit_actor

        app = _app_caller(request)
        kwargs["audit_actor"] = f"app:{app}" if app else audit_actor(request, "dashboard")
    if "alert_after_failures" in body:
        try:
            kwargs["alert_after_failures"] = validate_alert_after_failures(
                body["alert_after_failures"]
            )
        except ValueError as exc:
            return web.json_response(
                {"error": str(exc), "code": "invalid_alert_after_failures"}, status=400
            )
    if not kwargs:
        return web.json_response({"error": "no fields to update"}, status=400)
    try:
        job = await _persist_holding_folder(
            state,
            str(kwargs.get("chat_folder_id") or ""),
            lambda: state.crons.update_job_async(job_id, **kwargs),
        )
    except _ChatFolderGone:
        return _unknown_chat_folder_response()
    except CronStoreBusy:
        return web.json_response(_CRON_BUSY_BODY, status=_CRON_BUSY_STATUS)
    except CronStoreUnreadable as exc:
        return _cron_unreadable_response(exc)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    if not job:
        return web.json_response({"error": "job not found"}, status=404)
    # The job's chat tab follows a folder change, AFTER the store commits -- so a
    # refused or busy save never moves a tab for a change that did not land. The
    # sink is filled only when this update actually changed the field, so an
    # unrelated edit -- which the form still submits the field on -- moves nothing.
    # Presence is the signal, not truthiness: the prior folder is "" when an
    # unfiled job is being filed, and that transition moves the tab too.
    if "chat_folder_was" in chat_folder_transition:
        await move_cron_job_tab(
            state,
            job,
            chat_folder_transition["chat_folder_was"],
            getattr(job, "chat_folder_id", ""),
        )
    state.push_refresh("crons")
    return web.json_response({"ok": True, "id": job.id})


async def api_cron_enable(request: web.Request) -> web.Response:
    """POST /api/crons/{id}/enable — toggle enable/disable."""
    if not request.get("app"):
        owner_denied = await require_owner_dashboard_request(request, "crons.enable")
        if owner_denied is not None:
            return owner_denied
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    if (_e := _invalid_path_id_response(job_id, "job_id")) is not None:
        return _e
    app_denied = await _refuse_foreign_app_job(request, state, [job_id], "crons.enable")
    if app_denied is not None:
        return app_denied
    # Default cap: the body is a single flag. allow_absent keeps the
    # missing-body-means-defaults contract; a body that is PRESENT but
    # malformed is a 400; only an absent body defaults.
    body, body_err = await read_bounded_json(request, allow_absent=True)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    enabled = body.get("enabled", True)
    try:
        ok = await state.crons.enable_job_async(job_id, enabled=enabled)
    except CronStoreBusy:
        return web.json_response(_CRON_BUSY_BODY, status=_CRON_BUSY_STATUS)
    except CronStoreUnreadable as exc:
        return _cron_unreadable_response(exc)
    if ok:
        state.push_refresh("crons")
    return web.json_response({"ok": ok})


# Every other api_* handler in this module is an owner surface, so a private
# member's internal call is refused before it runs. These verify and scope
# their own caller instead.
guard_owner_surface_routes(globals(), member_scoped=frozenset({"api_cron_tools"}))
