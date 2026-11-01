"""MCP server exposing cron tools to kiro-cli.

Runs as ``kirocrew mcp-cron`` — kiro-cli spawns it as a child process
and calls tools via JSON-RPC over stdio (MCP protocol).

Tools:
    cron_list       — list all scheduled jobs
    cron_add        — add a cron job (every/cron/at)
    cron_remove     — remove a job by ID
    cron_remove_all — remove all jobs
    cron_pause      — pause a job
    cron_resume     — resume a paused job
    cron_secret_request — request vault secrets for an owned script cron
                          job (records a PENDING grant; operator approves in
                          the dashboard — this tool never grants)
"""

from __future__ import annotations

import importlib as _importlib
import logging
import os
import sys as _sys
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from kiro_crew import _facade_mirror, model_registry
from kiro_crew.config.loader import config_dir
from kiro_crew.cron import (
    _JOB_TIMEOUT_SECS,
    CronJob,
    CronService,
    CronStoreBusy,
    CronStoreUnreadable,
    agent_sequence_dispatches,
    cron_job_id_from_session_key,
    cron_session_key_is_stable,
    format_schedule,
    get_local_tz,
    is_valid_skip_date,
    is_valid_timezone,
    parse_time_string,
)
from kiro_crew.cron_script import (
    compute_secret_env_pin,
    delivery_fingerprint,
    resolve_script_path,
    validate_secret_env_grant,
)
from kiro_crew.cron_trigger import _JOB_ID_RE, trigger_cron_job
from kiro_crew.mcp_caller import current_caller
from kiro_crew.mcp_core import (
    _resolve_session_key,
    require_strict_session_key,
    strict_identity_diagnosis,
)
from kiro_crew.mcp_cron_list_render import (
    _render_cron_list_compact,
    _render_cron_list_full,
    _render_cron_list_json,
)
from kiro_crew.mcp_cron_tool_list import _list_tools
from kiro_crew.mcp_cron_vet import (
    _log_cron_denial,
    _resolve_cron_folder,
    _vet_cron_capability_governance,
    _vet_script_file,
    _vet_shell_command,
)
from kiro_crew.mcp_instructions import CRON as _INSTRUCTIONS
from kiro_crew.mcp_shared import call_tool_with_logging, run_mcp_stdio_loop
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.port_resolution import resolve_serving_port
from kiro_crew.sel import sel
from kiro_crew.validation import (
    MCP_CRON_SCHEMAS,
    ValidationError,
    infer_use_case,
    validate_tool_args,
)

logger = logging.getLogger("kiro_crew.mcp_cron")


def _sub_floor_timeout_note(timeout_secs_val: object) -> str:
    """Return a caller-facing note when ``timeout_secs`` is below the reaper floor.

    The primary ``asyncio.wait_for`` guard in ``_execute_with_timeout`` honors any
    value in ``1..86400``, so a sub-floor budget IS enforced on the normal path.
    The reaper force-kill backstop, however, clamps its deadline to at least
    ``_JOB_TIMEOUT_SECS`` (``max(min(timeout_secs, 86400), _JOB_TIMEOUT_SECS)``),
    so if the event loop stalls or the task ignores cancellation the job is not
    force-killed until that floor. Surfacing the gap at set time is cheaper than
    letting the caller discover it from a job that outran its configured budget.

    Returns an empty string when the value is absent, non-numeric, or already at
    or above the floor, so callers can unconditionally append it to their reply.
    """
    if not isinstance(timeout_secs_val, (int, float, str)):
        return ""
    try:
        secs = int(timeout_secs_val)
    except (ValueError, TypeError):
        return ""
    if 1 <= secs < _JOB_TIMEOUT_SECS:
        return (
            f" Note: timeout_secs={secs}s is below the {_JOB_TIMEOUT_SECS}s reaper "
            "floor -- the primary guard enforces it, but if the event loop stalls "
            f"the force-kill backstop will not trigger until {_JOB_TIMEOUT_SECS}s."
        )
    return ""


def _validate_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate tool arguments against schema. Returns cleaned args."""
    schema = MCP_CRON_SCHEMAS.get(name)
    if schema:
        cleaned = validate_tool_args(args, schema)
    else:
        cleaned = args  # tools without schemas (cron_remove_all) pass through
    # Semantic check: reject past timestamps for one-shot jobs
    at_ts = cleaned.get("at")
    if at_ts is not None and at_ts < time.time():
        raise ValidationError("at", f"timestamp {int(at_ts)} is in the past")
    return cleaned


def _call_tool(name: str, raw_args: dict[str, Any]) -> str:
    """Execute a cron tool and return the result as text."""
    # Second fence behind the unmount: a job runs on the gateway machine, so a
    # session working in a remote workspace may not schedule or drive one.
    from kiro_crew.session_environment import refuse_local_only_tool

    remote_refusal = refuse_local_only_tool(_resolve_session_key(), "kirocrew-cron", name)
    if remote_refusal:
        return f"Error: {remote_refusal}"
    # Managed callers use ordinary authenticated gateway routing, including
    # live restricted sessions whose execution record intentionally is not on disk.
    if current_caller() is not None or _resolve_session_key():
        # The gateway authenticates the request and resolves its captured session
        # execution before opening the cron store.
        from kiro_crew.mcp_core import _post

        session_key, refusal = require_strict_session_key(
            "Cannot identify this cron caller. Reopen the conversation.",
            server="kirocrew-cron",
        )
        if refusal:
            return f"Error: {refusal}"
        args = dict(raw_args)
        if name == "cron_add" and not args.get("channel"):
            # Preserve the direct runtime's delivery default as an ordinary,
            # server-validated argument; it confers no ownership authority.
            channel = _caller_channel_id() or os.environ.get("KIROCREW_CHANNEL_ID")
            if channel:
                args["channel"] = channel
        # Default the reply thread on the runtime side, ONLY when ``thread_ts``
        # is ABSENT (an explicit value -- including ``""``, a top-level opt-out --
        # is the caller's, and wins). ``channel`` is still guarded by
        # ``isinstance`` FIRST: a non-string ``channel`` (e.g. a model emitting a
        # Slack ts/id as a JSON number) must fall through to schema validation to
        # be rejected cleanly, not raise ``AttributeError`` from the
        # pre-validation ``.strip()`` below. A non-string value is left untouched.
        channel_arg = args.get("channel")
        if (
            name == "cron_add"
            and "thread_ts" not in args
            and isinstance(channel_arg, (str, type(None)))
        ):
            # Default the reply thread HERE, on the runtime side, because the
            # verified caller channel (``_caller_channel_id()``) is only
            # reliable before the gateway hop -- the HTTP boundary rebuilds the
            # CallerContext without ``channel_id`` (dashboard/handlers/cron.py),
            # so a comparison made on the gateway side would always see an empty
            # caller channel and wrongly skip (or, worse, mis-apply) the
            # inherit. A thread_ts is scoped to the channel it was created in,
            # so only inherit the caller's thread when the resolved delivery
            # channel IS the caller's own channel: a defaulted channel is the
            # caller's own by construction, and an explicit channel must equal
            # it. Forward the result as an ordinary argument, mirroring the
            # channel default above; explicit thread_ts (handled by the guard
            # above) still wins, and a non-Slack caller gets no thread.
            #
            # The inherit fires ONLY when the ``thread_ts`` key is ABSENT. An
            # explicit ``"thread_ts": ""`` is a caller opting OUT -- asking for
            # top-level delivery to its own channel -- and must NOT be
            # overridden with the inherited thread; it falls through unchanged
            # for the gateway side to normalize ``""`` to no thread. (The
            # ``isinstance`` guard on ``channel`` still matters: a non-string
            # channel -- a model emitting one as a JSON number -- must fall
            # through to schema validation, not raise ``AttributeError`` from a
            # pre-validation ``.strip()`` below.)
            caller_channel = _caller_channel_id() or os.environ.get("KIROCREW_CHANNEL_ID") or None
            resolved_channel = (channel_arg or "").strip() or None
            if caller_channel and resolved_channel == caller_channel:
                thread = _caller_thread_id()
                if thread:
                    args["thread_ts"] = thread
        response = _post(
            "/api/crons/tools", {"name": name, "arguments": args}, session_key=session_key
        )
        if (
            response.get("refused")
            and current_caller() is None
            and infer_use_case(session_key) == "cli"
        ):
            # No gateway is listening (nothing was executed) and the identity is
            # POSITIVELY the attended CLI's own -- ``kirocrew chat`` presents the
            # ``cli_chat`` key everywhere it is identified, and it is the one
            # surface whose cron tools always wrote the host store directly.
            # Keep that. A gateway-minted key (dashboard, channel, cron,
            # subagent) with no injected caller is the non-pooled gateway
            # topology, where a refused dial is an outage of the gateway that
            # validates the call: report it, never write around it.
            return _call_tool_locally(name, raw_args)
        if response.get("error"):
            advice = (
                " Outcome unknown; check cron_list before retrying a mutation."
                if response.get("transport_error")
                else ""
            )
            return f"Error: {response['error']}{advice}"
        result = response.get("result")
        if not isinstance(result, str):
            return (
                "Error: the cron gateway returned an invalid response. "
                "Check cron_list before retrying a mutation."
            )
        return result
    return _call_tool_locally(name, raw_args)


def _call_tool_locally(name: str, raw_args: dict[str, Any]) -> str:
    """Validated host dispatch, also used by the authenticated HTTP boundary."""
    return call_tool_with_logging(
        name,
        raw_args,
        _validate_args,
        _call_tool_inner,
        # Real caller identity when resolvable, mirroring mcp_core: a hardcoded
        # "mcp_cron" made every cron tool's audit record session-blind, so a
        # SUCCESSFUL authorization -- a list where the caller owned everything --
        # had no event naming who was authorized. The lenient resolver is right
        # here: this labels an audit row, it does not decide access (that is
        # _authz_session_key's job, which refuses forgeable sources).
        session_key=_resolve_session_key() or "mcp_cron",
        downstream_service="kirocrew-cron",
    )


def _caller_channel_id() -> str:
    """The channel the calling session lives in, per the injected caller block.

    ``KIROCREW_CHANNEL_ID`` has the same defect the session key had: process
    environment can only ever name ONE session's channel, and gatewayd forwards no
    such variable to a shared backend, so a pooled ``cron_add`` defaulted the
    delivery channel to nothing. The caller block carries ``channelId`` alongside
    the session key -- same envelope, same trust, already stripped-and-reinjected
    by gatewayd -- so it is the source that stays correct when pooled. The env var
    remains the fallback for a non-gateway launch.
    """
    ctx = current_caller()
    return ctx.channel_id if ctx is not None else ""


def _caller_thread_id() -> str:
    """The Slack thread the calling session runs in, or ``""``.

    A cron created from within a Slack thread should reply INTO that thread, but
    ``cron_add`` only set ``thread_ts`` when passed explicitly -- so a cron
    scheduled by an agent running in a Slack thread produced a job with no
    thread, and its results posted as a NEW top-level channel message instead of
    a thread reply.

    The thread is recoverable with no extra gateway state: a Slack session key
    is ``canonical_key(reply_ts)`` == ``"slack:<thread_ts>"`` (see
    slack/transport_dispatch.py), where ``reply_ts`` is the bare thread
    timestamp. So the caller's own strict session key already names its thread.
    Parsing it here keeps the fix entirely inside the MCP tool (no CallerContext
    schema change, no claim/rekey plumbing, no SessionManager import), and is
    correct under pooling because the key comes from the strict resolver, not the
    forgeable environment.

    The parse is delegated to ``messaging.link.legacy_key`` -- the SAME helper
    the Slack transport uses to build the key -- rather than re-spelling the
    ``slack:<ts>`` shape here, so the two cannot drift. It returns the bare
    ``<thread_ts>`` for a legacy ``slack:<ts>`` key and ``None`` for any other
    shape (DM buckets, dashboard, cron, canonical multi-segment keys), which a
    canonical multi-segment ``slack:agent:...`` key is NOT -- so the caller
    falls back to no thread.
    """
    from kiro_crew.messaging.link import legacy_key

    return legacy_key(_authz_session_key()) or ""


def _authz_session_key() -> str:
    """The session key an ownership decision may be made from, or ``""``.

    STRICT resolution, deliberately. The lenient
    :func:`mcp_core._resolve_session_key` ends its fallback chain in a ``/proc``
    ancestor walk over the per-pid session file -- which ``mcp_core`` itself
    documents as "agent-writable and therefore forgeable". Reading identity from
    it is tolerable for labelling an audit row; deciding who may delete whose
    scheduled job is not. This module is deliberately NOT a call site for that
    file: the strict resolver accepts only the gateway-injected caller block,
    ``KIROCREW_SESSION_KEY``, or ``KIROCREW_HOST_PID`` plus its HMAC sidecar --
    three sources the gateway authors and an agent cannot write.

    Empty means "this call did not arrive with an identity the gateway vouches
    for", which is a non-gateway launch (neither the ACP spawn path nor the
    sandbox launcher ran). It is NOT the same as "single user, so anything goes":
    see :func:`_unidentified_caller_refusal`.
    """
    # Resolve-half of the shared strict gate only: the refusal text (and its
    # diagnosis) lives in :func:`_unidentified_caller_refusal`, which each
    # mutating tool composes itself.
    return require_strict_session_key("cron ownership authorization")[0]


def _deny_channel_agent_cron(tool_name: str) -> str | None:
    """Deny ``cron_add`` / ``cron_update`` to a channel agent, else ``None``.

    Channel agents (session keys ``channel:<channel_id>:<agent_id>``) are
    confined to channel-post communication -- ``CHANNEL_AGENT_BLOCKED_TOOLS``
    holds back ``send_*``, every ``session_*`` verb, and every verb that starts
    work outside the caller's own turn, for exactly that reason.
    Scheduling a cron job is the same shape made durable: ``cron_add`` takes an
    ``agent`` and ``approval_mode`` that flow straight to ``add_job``, so a
    channel agent could schedule ``agent="kirocrew", approval_mode="auto"`` and
    have a full-tool agent run on the gateway host on a timer -- an escalation
    past its own confinement that outlives both the turn and the channel.
    ``cron_update`` maps ``agent`` onto ``agent_id``, so it re-targets the agent
    of a job the calling session already owns.

    The interactive guard in ``channel.py`` rejects blocked tools at the
    permission-request event, but an AUTO-APPROVED call fires no such event: an
    ``@kirocrew-cron/cron_add`` entry in a channel agent's ``allowedTools`` is
    translated to a KAS auto-approve permission (``acp/kas_permissions.py``;
    MCP tools are not in ``WITHHELD_FROM_AUTO_APPROVE``), and auto-approval is
    the ABSENCE of a permission request -- so ``_blocked_tool_named`` never runs.
    The containment therefore has to hold HERE, at MCP dispatch, keyed on the
    verified caller identity (the strict resolver refuses forgeable sources).
    Mirrors ``mcp_core._deny_channel_agent_messaging``.

    Only the ``channel:`` orchestrator-agent namespace is confined; a
    ``slack:``/``discord:`` session is an allow-listed HUMAN participant
    scheduling their own recurring work, which is the legitimate flow the issue
    is careful not to break. Best-effort SEL audit mirrors channel.py's
    ``rejected_blocked_tool`` outcome; an audit failure never unblocks the deny.
    """
    caller_session = _authz_session_key()
    if not caller_session.startswith("channel:"):
        return None
    try:
        sel().log_tool_invocation(
            session_key=caller_session,
            source="mcp",
            tool_name=tool_name,
            tool_kind="kirocrew-cron",
            outcome="rejected_blocked_tool",
        )
    except Exception:
        # File-backed SEL write; stdio-silent (no logger -- stderr would corrupt
        # the JSON-RPC stream). The deny below still holds.
        pass
    return (
        f"Error: {tool_name} is not available to channel agents -- a channel "
        "agent is confined to channel posts and may not schedule a job that "
        "runs as another agent."
    )


#: A job with no recorded owner. Written by every creation path that has no
#: session to name: ``kirocrew cron add`` from the CLI (``cli_commands``, which
#: drives ``CronService`` directly and never routes through this server), the
#: onboarding importer.
#:
#: No MCP session may read or write one. "Nobody owns it" must not read as
#: "anybody may have it": a job's ``message`` is arbitrary prompt text and its
#: ``command``/``script`` are arbitrary payloads, and an identified session is not
#: necessarily the operator -- an allowlisted Slack or Telegram participant gets a
#: session of their own. Disclosing the admin surface's rows to one of those is a
#: disclosure to a different principal, so ownerless rows are simply outside every
#: session's scope, in both directions.
#:
#: The consequence is deliberate and worth stating: a cron created with
#: ``kirocrew cron add`` does not appear in ``cron_list`` from chat. The CLI
#: remains its management surface. The alternative -- showing it -- is what the
#: paragraph above rules out.
#:
#: Note the set keeps GROWING for as long as the CLI and the importer create rows
#: without a session, which is why this is a permanent scope rule rather than a
#: drainable exemption for legacy rows.
_UNOWNED = ""


def _unidentified_caller_refusal(tool_name: str) -> str:
    """The single answer every MUTATING cron tool gives an unidentifiable caller.

    Before the caller block was consumed here, a pooled backend resolved no
    session key and each mutating path invented its own reading of that: the
    per-job ownership gate returned "allow", ``cron_list`` skipped its filter,
    ``cron_add`` stored an ownerless row, and only ``cron_remove_all`` refused.
    Two of those are fail-open, which made the ownership gate dead code for every
    caller arriving through the gateway -- the case the gate exists for.

    One rule instead: anything that WRITES refuses, and a read is narrowed to the
    rows that belong to no session (see :func:`_visible_to`) rather than every
    row. Both follow from the same fact: gatewayd forwards ``caller=None`` when a
    stub registers without a session key and peer resolution fails, so an
    unidentifiable caller can be sharing a pooled backend with identified ones.
    It is not necessarily alone, so it gets neither their rows nor authority over
    them. The refusal never strands an operator, and it names the route that does
    work: ``kirocrew cron ...`` drives :class:`CronService` directly and never
    arrives here, so its authority does not depend on anything this server reads.
    No ambient environment value grants scope on this path; identity comes from
    the sources :func:`_authz_session_key` accepts, and nothing else.
    """
    try:
        sel().log_tool_invocation(
            session_key="mcp_cron",
            source="mcp",
            tool_name=tool_name,
            tool_kind="authz",
            outcome="denied",
            error="caller identity unresolved; refusing a write",
        )
    except Exception:
        pass
    return (
        "Error: cannot determine which session is calling, so this write is "
        "refused. Manage jobs from the CLI (`kirocrew cron ...`), which carries "
        "admin authority." + strict_identity_diagnosis("kirocrew-cron")
    )


def _not_found(job_id: str) -> str:
    """The ONE answer the ownership gate gives for any job it will not act on.

    Shared by all three refusal branches -- no such id, a row owned by another
    session, a row owning no session -- so none of them is an existence oracle for
    the others. The ``Error:`` prefix is the tool contract's failure marker.

    The two sentences after the marker are addressed to the MODEL reading this, and
    neither narrows the ambiguity above: they hold verbatim in all three branches,
    so the string stays one string and discloses nothing it did not before.

    They are here because the vague wording has a failure mode of its own. An agent
    told only "job not found" reasonably concludes the job is gone and reports THAT
    to whoever asked -- a fluent, plausible answer that happens to be false, and one
    the caller cannot distinguish from a true one. Saying outright that this answer
    is not evidence of absence is the only place that inference can be intercepted.
    The recovery command is named for the same reason: ``cron adopt`` exists
    precisely to un-strand these rows, and a caller who never sees it named
    has no way to reach it from inside the product. It is phrased as something to
    ASK THE USER for, not to run: ``security/denied_rules.py``'s ``self-protection-cron-adopt``
    rule denies that command to the agent so a session cannot assign itself
    ownership of a scheduled job, so an instruction to run it would dead-end at
    that gate and read as a malfunction.
    """
    return (
        f"Error: job not found: {job_id}. This is the same answer whether no such "
        f"job exists or one exists that this session does not own, so do not report "
        f"it as proof the job is gone. `kirocrew cron list` shows every job with its "
        f"owner; if the job should belong to this session, ask the user to run "
        f"`kirocrew cron adopt {job_id} --session-of <session>`."
    )


def _unowned_row_refusal(job_id: str) -> str:
    """The answer for a row that exists but records no owner.

    Deliberately the SAME vague wording the cross-session branch uses. An earlier
    revision named the row and pointed at the CLI, which was safe only while such
    rows were listed to the caller; now that they are outside every session's
    scope, a distinct message would confirm the existence of a row the caller
    cannot see -- an enumeration oracle over the admin surface's jobs.
    """
    try:
        sel().log_tool_invocation(
            session_key=_authz_session_key() or "mcp_cron",
            source="mcp",
            tool_name=f"cron:{job_id}",
            tool_kind="authz",
            outcome="denied",
            error="job records no owner; out of scope for every MCP session",
        )
    except Exception:
        pass
    return _not_found(job_id)


def _audit_list_scope(
    kept: list[CronJob], all_jobs: list[CronJob], session_key: str
) -> list[CronJob]:
    """Record the ``cron_list`` scoping decision on the SEL trail, and pass *kept* on.

    ``cron_list`` withholding rows is an authorization decision, and every other
    one in this module already lands on the trail: the two refusal helpers do, and
    ``cron_remove_all`` logs its ``scoped`` outcome. This one did not, which is a
    gap that grew when the filter stopped being a no-op -- an unidentifiable caller
    now has EVERYTHING withheld, and that denial was the least visible of the lot.

    Only a decision with an EFFECT is logged here. The authorized case -- a caller
    that owns everything it can see -- is NOT unaudited: ``call_tool_with_logging``
    records every cron tool invocation and, since :func:`_call_tool` stopped passing
    a hardcoded label, records it against the calling session. A second event per
    call would duplicate that, and ``cron_list`` is called often enough for the
    duplicate to bury the trail it is meant to serve.
    """
    withheld = len(all_jobs) - len(kept)
    if not withheld:
        return kept
    try:
        sel().log_tool_invocation(
            session_key=session_key or "mcp_cron",
            source="mcp",
            tool_name="cron_list",
            tool_kind="authz",
            outcome="denied" if not kept else "scoped",
            resources=f"session={session_key} kept={len(kept)} withheld={withheld}",
            error=("caller identity unresolved; every row withheld" if not session_key else ""),
        )
    except Exception:
        pass
    return kept


def _check_cron_job_ownership(svc: "CronService", job_id: str) -> str | None:
    """Return an error string if the caller doesn't own this job, else None."""
    session_key = _authz_session_key()
    if not session_key:
        return _unidentified_caller_refusal(f"cron:{job_id}")
    job = svc.get_job(job_id)
    if not job:
        # Same wording as both refusals below, and that is the point: this gate
        # is anti-enumeration, so answering "Job not found" here and "Error: job
        # not found" for another session's row would be two distinguishable
        # strings, letting a caller tell an id that exists from one that does
        # not. Post-gate messages may name the row freely: by then
        # the caller owns it.
        return _not_found(job_id)
    if job.session_key == _UNOWNED:
        return _unowned_row_refusal(job_id)
    if job.session_key != session_key:
        try:
            sel().log_tool_invocation(
                session_key=session_key,
                tool_name=f"cron:{job_id}",
                outcome="denied",
                error="cross-session ownership check failed",
            )
        except Exception:
            pass
        return _not_found(job_id)
    return None


#: The answer when rows exist but none are in the caller's scope, kept DISTINCT
#: from the store-is-empty ``"No cron jobs."``.
#:
#: One string for both states is actively misleading: a filtered result reading
#: "nothing is scheduled" tells an operator whose 15 jobs are all enabled and
#: running on schedule that this server is pointed at a different store. The
#: scoping decision is otherwise legible only in the SEL
#: row that records it (``kept=0 withheld=N``), and an audit log is the right
#: place to keep that record, not the only place to explain it.
#:
#: ``dashboard/handlers/cron.py`` already documents the invariant this sentence
#: states -- "cron_list only shows a session its own jobs, so a job whose key is
#: empty ... [is] manageable only from this page or the CLI". The knowledge was
#: written down; the message a caller actually sees just did not carry it.
#:
#: Deliberately WITHOUT a count of what was withheld. :data:`_UNOWNED` exists
#: because an identified session is not necessarily the operator, and "N jobs
#: exist that you may not see" discloses the admin surface's volume to exactly
#: that principal. Naming the two surfaces that CAN reach those rows discloses
#: nothing further: both already require the operator's own machine, and
#: ``cron adopt`` is a verb on one of them rather than a third surface.
#:
#: The "do not report that no cron jobs exist" clause is addressed to the MODEL,
#: and it is load-bearing for a caller this sentence otherwise misleads. A
#: sub-agent asked to review the schedule is scoped to its OWN key, so it reaches
#: this string legitimately and can relay "there are no cron jobs" upward without
#: any error having occurred -- a wrong answer that is indistinguishable from a
#: right one, since the parent has no view of what was filtered. Withholding the
#: count is still right; leaving the caller to infer absence from the silence was
#: not.
#:
#: ``cron adopt`` is named as something to ASK THE USER for. The agent cannot run
#: it: ``security/denied_rules.py``'s ``self-protection-cron-adopt`` rule denies the command so
#: a session cannot assign itself ownership of a scheduled job. Coaching the model
#: to run it would send it into that gate; coaching it to relay the command points
#: the text and the deny rule the same way.
_SCOPED_EMPTY = (
    "No cron jobs owned by this session. This is NOT an empty registry, so do not "
    "report that no cron jobs exist: jobs owned by another session, or created "
    "without one (the CLI and the onboarding importer), are outside this session's "
    "scope and are not listed here. `kirocrew cron list` and the dashboard Schedule "
    "page show every job with its owner; to bring one into this session's scope for "
    "good, ask the user to run `kirocrew cron adopt <job_id> --session-of <session>`."
)


def _owner_unusable_caveat(svc: "CronService", session_key: str, job_id: str) -> str:
    """Why the row just written may already be unmanageable, or ``""``.

    ``cron_add`` stamps the CALLER's key as the owner and every mutating tool then
    demands an exact match, so a caller whose own key does not outlive the job has
    just written a row only ``kirocrew cron adopt`` can rescue. Both branches below
    succeed today and report a bare success, which is the defect: the loss is
    silent at the one moment the caller still has the id in front of it and could
    act.

    A WARNING appended to a success, deliberately, not a refusal. A sub-agent
    scheduling a job nobody ever pauses is a working flow -- only management is
    lost, never execution -- so refusing would break callers who are getting
    exactly what they asked for, to protect them from a cost they may not be
    paying. Naming the consequence costs those callers one sentence and tells the
    ones who DID intend to manage the job what to do while it is still cheap.

    Every ``cron adopt`` reference here is addressed to the USER, in the same
    ``Tell the user:`` register the success string already uses. That is not a
    style choice: ``security/denied_rules.py``'s ``self-protection-cron-adopt`` rule denies the
    command to the agent outright, precisely so a session cannot assign itself
    ownership of a scheduled job. An instruction telling the model to RUN it would
    dead-end at that gate and read as a malfunction, so the model is told to relay
    it instead -- the human is the only principal who can execute the remedy, and
    the deny rule and this text now point the same way.

    Neither branch is reachable from the surfaces that already work: a chat tab is
    ``dashboard:<slot>`` and a durable cron is ``cron:<job_id>``, and both outlive
    the jobs they create.
    """
    if session_key.startswith("subagent:"):
        return (
            " Note: the owner recorded is this sub-agent's conversation. The parent "
            "session cannot present that key, so the parent cannot pause, resume, or "
            "remove this job -- not eventually, but starting now -- and the key stops "
            "resolving at all once the conversation is released or reaped. Tell the "
            "user: either have the parent create the job, or run `kirocrew cron adopt "
            f"{job_id} --session-of <parent session>` to transfer it."
        )
    # Asked of the JOB RECORD, never of the key's shape. Two different minting
    # paths produce a three-segment ``cron:`` key -- an ephemeral per-fire run id
    # and a DURABLE per-agent name for an agent_sequence job -- so counting
    # separators warns the multi-agent case wrongly. See
    # cron.cron_session_key_is_stable, which lives next to both mint sites.
    caller_job_id = cron_job_id_from_session_key(session_key)
    if not caller_job_id:
        return ""
    caller_job = svc.get_job(caller_job_id)
    # No record means no evidence, so say nothing: a warning we cannot substantiate
    # is worse than no warning.
    if caller_job is None or cron_session_key_is_stable(caller_job):
        return ""
    return (
        " Note: the cron creating this job runs with persistent_session disabled, "
        "so its session key carries a fresh per-run id and will never match again. "
        "The owner recorded here is already unusable -- this cron will not "
        "recognise the job on its next run. Tell the user to run `kirocrew cron "
        f"adopt {job_id} --session-of <session>` if anything should manage it later."
    )


def _ephemeral_authority_caveat(
    persistent: bool, is_agent_job: bool, agent_sequence: list[str]
) -> str:
    """Warn when the job BEING created or updated is the one that loses authority.

    Distinct from :func:`_owner_unusable_caveat`, which is about the caller. Here
    the caller may be perfectly durable while the job it is writing is not:
    ``persistent_session=False`` gives each run a fresh ``cron:<id>:<run_id>`` key
    (``cron.build_cron_session_context``), so that job can never satisfy an
    ownership check on a later run -- including against jobs it created itself on
    an earlier one.

    Two exemptions, both because the flag cannot produce the bad state:

    * NOT an agent job. A script cron is launched with
      ``KIROCREW_SESSION_KEY=cron:<job_id>`` unconditionally and a command cron
      issues no MCP call at all.
    * a dispatching ``agent_sequence`` (:func:`cron.agent_sequence_dispatches`,
      the one spelling of that gate). That path mints a stable
      ``cron:<job_id>:<agent>`` key and ignores ``persistent_session``, so the
      warning would be false -- the same conflation
      :func:`cron.cron_session_key_is_stable` exists to prevent.

    Applied at ``cron_update`` as well as ``cron_add``: the flag is writable on
    both, so warning only at creation would leave the identical state reachable
    through a bare ``Updated job:``.

    The argument reads as a context-management knob -- whether a run sees the
    previous run's transcript -- and nothing at the call site suggests it also
    revokes the job's authority over the scheduler. That gap is why this is worth
    a sentence rather than a docs line.
    """
    if persistent or not is_agent_job or agent_sequence_dispatches(agent_sequence):
        return ""
    return (
        " Note: persistent_session is false, so every run of this job gets a fresh "
        "session key. It will not be able to pause, resume, or remove any cron job, "
        "including ones it creates itself, because the ownership check needs the "
        "same key to come back on a later run. Leave persistent_session at its "
        "default if this job is meant to manage other jobs."
    )


def _withdraw_pending_request(svc: "CronService", job_id: str) -> str:
    """Drop *job_id*'s pending ``cron_secret_request``; an approved grant is untouched."""
    try:
        svc.update_job(job_id, secret_env_pending={})
    except CronStoreBusy:
        return "Error: cron store busy, please retry"
    return "Withdrew the pending secret request."


def _owned_by(jobs: list[CronJob], session_key: str) -> list[CronJob]:
    """The jobs *session_key* may reach -- for reading and for writing alike.

    ONE scope, deliberately. An earlier revision of this change had a wider read
    scope than write scope so that ownerless rows stayed visible; that is a
    disclosure to a principal who may not be the operator (see :data:`_UNOWNED`),
    and two scopes were two chances to use the wrong one -- ``cron_remove_all``
    had already picked the wider one once.

    An empty *session_key* returns nothing rather than every ownerless row, which
    is what ``j.session_key == session_key`` would otherwise mean: an
    unidentifiable caller can be sharing a pooled backend with identified ones, so
    it gets nothing at all.
    """
    if not session_key:
        return []
    return [j for j in jobs if j.session_key == session_key]


_AUTO_APPROVAL_OWNER_ONLY = (
    "Error: approval_mode 'auto' can only be turned on by the owner. "
    "Create or update the job without it, then ask the user to turn on "
    "auto-approve for this job on the Schedule page."
)


#: ``cron_update`` fields (as ``update_job`` kwargs) that change what a job does or
#: where it reports. Changing one through a tool turns the owner's auto-approve off.
_AUTO_RESETTING_FIELDS = frozenset(
    {"message", "agent_id", "model", "cron_expr", "every_secs", "channel", "thread_ts"}
)

_AUTO_APPROVAL_RESET_NOTE = (
    " Auto-approve was turned off because the job changed; ask the user to turn it "
    "back on for this job on the Schedule page."
)


def _refuse_tool_auto_approval(tool_name: str, args: dict[str, Any]) -> str:
    """Refuse a tool call that would turn on ``approval_mode="auto"``.

    An auto job approves every tool call of every later fire with nobody
    watching, so it is the owner's switch: the Schedule page's owner-gated
    routes set it. A tool caller cannot be told apart from other code running
    in the same session (both reach this handler the same way), so no tool
    call may set it. ``""`` (the default, hook-based approval) is unaffected.
    """
    if args.get("approval_mode") != "auto":
        return ""
    _log_cron_denial(tool_name, _AUTO_APPROVAL_OWNER_ONLY)
    return _AUTO_APPROVAL_OWNER_ONLY


def _call_tool_inner(name: str, args: dict[str, Any]) -> str:
    """Execute a cron tool (post-validation)."""
    svc = CronService(base_dir=config_dir())

    if name == "cron_list":
        verbose = bool(args.get("verbose", False))
        ids_filter = args.get("ids") or None
        jobs = svc.list_jobs(include_disabled=True)
        if not jobs:
            return "No cron jobs."
        # Ownership filter: a caller sees ONLY the rows it owns. Not ownerless
        # rows (they belong to the admin surface, and an identified session is not
        # necessarily the operator -- see _UNOWNED), and an UNIDENTIFIABLE caller
        # sees nothing rather than everything, because gatewayd can forward
        # caller=None on a pooled connection.
        session_key = _authz_session_key()
        jobs = _audit_list_scope(_owned_by(jobs, session_key), jobs, session_key)
        if not jobs:
            # NOT the store-empty string above: rows exist, they are just out
            # of scope. See _SCOPED_EMPTY for why the two must differ.
            return _SCOPED_EMPTY
        # Drill-in: ids filter forces full bodies for matching jobs only.
        if ids_filter:
            id_set = set(ids_filter)
            jobs = [j for j in jobs if j.id in id_set]
            if not jobs:
                missing = ", ".join(sorted(id_set))
                return f"No cron jobs match ids: {missing}"
            verbose = True
        # JSON last, and it wins: it is a different CONSUMER, not a third verbosity.
        # A machine reading this must get a parseable document even when `ids` has
        # already forced verbose on, so the precedence is stated in the description
        # the same way `ids over verbose` already is.
        if bool(args.get("json", False)):
            return _render_cron_list_json(jobs)
        if verbose:
            return _render_cron_list_full(jobs)
        return _render_cron_list_compact(jobs)

    if name == "cron_add":
        # Channel-agent containment FIRST: a channel agent may not schedule a
        # durable job (which can run as another, more privileged agent). Keyed
        # on the verified caller identity so an auto-approved call -- which fires
        # no permission event for channel.py's guard to catch -- is still denied.
        chan_err = _deny_channel_agent_cron("cron_add")
        if chan_err:
            return chan_err
        # Capability gate FIRST: if the calling surface's policy/profile disables
        # the cron capability, no job may be authored at all (command, script, or
        # message). This is the on/off gate, distinct from the per-command body
        # check below (the ``commands`` scope).
        cap_err = _vet_cron_capability_governance()
        if cap_err:
            _log_cron_denial("cron_add", cap_err)
            return cap_err
        auto_err = _refuse_tool_auto_approval("cron_add", args)
        if auto_err:
            return auto_err
        n = args["name"]
        msg = args.get("message", "")
        script = args.get("script", "")
        command = args.get("command", "")
        if command:
            err = _vet_shell_command(command)
            if err:
                _log_cron_denial("cron_add", err)
                return err
        if script:
            try:
                script_path, _ = resolve_script_path(script)
            except (ValueError, FileNotFoundError, PermissionError) as e:
                return f"Error: {e}"
            err = _vet_script_file(script_path)
            if err:
                _log_cron_denial("cron_add", err)
                return err
        every = args.get("every")
        cron_expr = args.get("cron_expr")
        at_ts = args.get("at")
        delay = args.get("delay")
        at_time = args.get("at_time")
        # The job's timezone is validated BEFORE at_time is parsed, because it is
        # the zone a wall-clock at_time ("9am", "tomorrow 5pm") is read in -- the
        # same zone the job's cron_expr and skip_dates use and its confirmation
        # below renders in. A clock read in any other zone stores an instant the
        # confirmation would then render as a time the caller never asked for.
        tz = args.get("timezone", "")
        if tz and not is_valid_timezone(tz):
            safe_tz = redact(tz)
            return f"Error: invalid timezone: {safe_tz!r}"
        if delay is not None and at_ts is None:
            at_ts = time.time() + delay
        if at_time is not None and at_ts is None:
            parsed = parse_time_string(at_time, tz)
            if isinstance(parsed, str):
                return parsed  # error message
            at_ts = parsed
        # The resolved instant carries the schema's ceiling for a raw ``at``
        # (year 2100), whatever produced it -- the same bound and wording the
        # dashboard route and the CLI apply. ``at_time``'s relative form has no
        # bound of its own: "in 999999999 hours" parses to a time ~116,000
        # years out that the Schedule page cannot render.
        if at_ts is not None:
            at_spec = next(s for s in MCP_CRON_SCHEMAS["cron_add"].fields if s.name == "at")
            lo, hi = at_spec.min_val, at_spec.max_val
            if (lo is not None and at_ts < lo) or (hi is not None and at_ts > hi):
                return f"Error: resolved time is outside the supported range ({lo} to {hi})"
        # Guard against past timestamps from any source (at, delay, at_time)
        if at_ts is not None and at_ts < time.time():
            # Rendered in the zone the job keeps its wall clocks in, so a refused
            # at_time echoes the clock the caller typed rather than the process's.
            shown_tz = ZoneInfo(tz) if tz else get_local_tz()[1]
            local = datetime.fromtimestamp(at_ts, shown_tz)
            return f"Error: resolved time {local.strftime('%I:%M %p %Z')} is in the past"
        channel = (args.get("channel") or "").strip() or None
        caller_channel = _caller_channel_id() or None
        if channel is None:
            channel = caller_channel or os.environ.get("KIROCREW_CHANNEL_ID") or None
        if not every and not cron_expr and not at_ts:
            return "Error: provide every, cron_expr, at, delay, or at_time"
        # Validate model BEFORE add_job so an invalid value never leaves an
        # orphaned job behind (a retried cron_add would then duplicate it).
        model_arg = str(args.get("model") or "").strip()
        if model_arg:
            # No membership gate: the model list is sourced from the live
            # kiro-cli `--list-models` (via /api/models), not the claude_code
            # registry family, so any id the CLI advertises is valid. Matches
            # the chat model path (which also skips membership); the runtime is
            # model-agnostic with a gateway fallback. Only normalize the "auto"
            # inherit sentinel.
            resolved_model = model_registry.to_provider_id(model_arg, "claude_code")
            if resolved_model == "":
                # "auto" sentinel (canonical key with no pinned provider id):
                # explicit inherit — same as leaving model unset.
                model_arg = ""
        # Pre-check here only to return a REDACTED, user-facing message (the
        # authoritative calendar-validity enforcement now lives in add_job at
        # the persistence owner, so any create caller is covered and the values
        # land in the job's FIRST _save() -- no orphaned/half-populated job).
        skip_dates = args.get("skip_dates", [])
        if skip_dates:
            for d in skip_dates:
                if not is_valid_skip_date(d):
                    return f"Error: invalid skip_date: {redact(str(d))!r} (expected YYYY-MM-DD)"
        thread_ts_absent = "thread_ts" not in args
        thread_ts = (args.get("thread_ts") or "").strip() or None
        # Auto-inherit the caller's thread when not passed explicitly: a cron
        # scheduled from inside a Slack thread replies INTO that thread rather
        # than posting a new top-level message. A thread_ts is scoped to the
        # channel it was created in, so it may only be inherited when the
        # resolved delivery channel IS the caller's OWN channel.
        #
        # This inherit runs ONLY when the caller's channel is known HERE
        # (``caller_channel`` is set) -- i.e. the direct-local dispatch path,
        # where ``_caller_channel_id()`` is reliable. On the gateway path the
        # HTTP boundary rebuilds the CallerContext WITHOUT ``channel_id``
        # (dashboard/handlers/cron.py), so ``_caller_channel_id()`` is empty
        # server-side; the thread was already resolved on the runtime side in
        # ``_call_tool`` (where the caller channel is reliable) and forwarded as
        # an explicit ``thread_ts`` argument, so re-deriving it here would at
        # best duplicate that and at worst inherit a thread ``_call_tool``
        # deliberately withheld for a cross-channel target. Requiring a known
        # ``caller_channel`` is what keeps the guard effective on the primary
        # routed path. Explicit ``thread_ts`` always wins; a non-Slack caller
        # gets no thread.
        #
        # The gate is the thread_ts key being ABSENT, not merely falsy: an
        # explicit ``"thread_ts": ""`` is a caller opting OUT (top-level delivery
        # to its own channel), which normalizes to ``None`` above but must NOT be
        # overridden with the inherited thread.
        if (
            thread_ts_absent
            and thread_ts is None
            and channel
            and caller_channel is not None
            and channel == caller_channel
        ):
            thread_ts = _caller_thread_id() or None
        # Resolve EVERY first-save field before the single locked add_job() so
        # the job is persisted fully-formed in one transaction -- no
        # create-then-mutate + second unlocked _save() window that a crash or a
        # concurrent reader could capture as a job missing its agent_id/model.
        # Bool fields are enforced by validation.py CRON_ADD_SCHEMA (FieldSpec
        # ... bool), so a non-bool falls back to the field default rather than
        # being coerced from a raw-truthy value.
        agent = args.get("agent", "")
        silent = args.get("silent", False)
        approval_mode = args.get("approval_mode", "")
        session_key = _authz_session_key()
        if not session_key:
            # Refuse rather than mint another ownerless row. A job whose owner is
            # unknown is precisely what made the ownership gate unenforceable, and
            # every such row is then visible-but-not-mutable through MCP (see
            # _UNOWNED). Any session the gateway can name keeps creating jobs; so
            # does `kirocrew cron add`, which reaches the store directly.
            return _unidentified_caller_refusal("cron_add")
        persistent_session = args.get("persistent_session")
        minimal_context = args.get("minimal_context")
        hide_in_chat = args.get("hide_in_chat")
        strict_schedule = args.get("strict_schedule")
        timeout_val = args.get("timeout", 0)
        timeout_secs_val = args.get("timeout_secs", 0)
        auto_pause_val = args.get("auto_pause_after_failures")
        alert_after_val = args.get("alert_after_failures")
        # Resolve the folder BEFORE add_job so an unresolvable reference never
        # leaves an orphaned job behind (same position as the model check
        # above). A folder auto-created here that a subsequent add_job failure
        # strands is benign: an empty folder, removable from the Schedule page.
        folder_id = ""
        if args.get("folder"):
            folder_id, folder_err = _resolve_cron_folder(args["folder"], session_key=session_key)
            if folder_err:
                return f"Error: {folder_err}"
        try:
            # A member-named ``agent`` must record a member selection, not a bare
            # template id the reply path cannot resolve.
            from kiro_crew.cron_service.identity import split_cron_agent_member

            _agent_id, _member_id = split_cron_agent_member(agent or "", args.get("member_id", ""))
            job = svc.add_job(
                name=n,
                message=msg,
                every_secs=every,
                cron_expr=cron_expr,
                at_ts=at_ts,
                channel=channel,
                thread_ts=thread_ts,
                delete_after_run=bool(at_ts),
                timezone=tz,
                skip_dates=skip_dates,
                agent_id=_agent_id,
                member_id=_member_id,
                approval_mode=approval_mode or "",
                model=model_arg,
                silent=bool(silent),
                strict_schedule=strict_schedule if isinstance(strict_schedule, bool) else False,
                hide_in_chat=hide_in_chat if isinstance(hide_in_chat, bool) else False,
                folder_id=folder_id,
                command=command or "",
                script=script or "",
                persistent_session=(
                    persistent_session if isinstance(persistent_session, bool) else True
                ),
                session_key=session_key,
                minimal_context=minimal_context if isinstance(minimal_context, bool) else False,
                timeout=timeout_val or 0,
                timeout_secs=timeout_secs_val or 0,
                auto_pause_after_failures=auto_pause_val,
                alert_after_failures=alert_after_val,
            )
        except CronStoreBusy:
            return "Error: cron store busy, please retry"
        except CronStoreUnreadable as exc:
            return f"Error: {exc}"
        except ValueError as e:
            return f"Error: {e}"
        sched_str = format_schedule(job.schedule, tz_name=job.timezone or "")
        sel().log_api_access(
            caller="mcp",
            operation="cron.create",
            outcome="allowed",
            source="mcp",
            resources=f"job_id={job.id}",
        )
        # Appended to the SUCCESS, after the id exists so both caveats can name it.
        # Not folded into the audit row above: call_tool_with_logging already records
        # this invocation against the calling session, and a caveat is advice to the
        # caller rather than a decision with an effect (cf. _audit_list_scope).
        #
        # Every input is read from ``args``, the way persistent_session and
        # command/script above are, so this site depends on the stored row for
        # nothing but ``job.id`` -- which the success string already used. Reading
        # ``job.agent_sequence`` back instead bought nothing (this tool has no
        # agent_sequence argument, so the field is always empty here) and coupled
        # the caveat to the whole returned object. ``args`` also keeps the answer
        # correct by construction if the argument is ever added to the schema.
        caveats = _owner_unusable_caveat(svc, session_key, job.id) + _ephemeral_authority_caveat(
            persistent_session if isinstance(persistent_session, bool) else True,
            not (command or script),
            list(args.get("agent_sequence") or []),
        )
        return (
            f"Added job: {job.id} ({job.name}) [{sched_str}]. "
            f"Tell the user: scheduled for {sched_str}.{caveats}"
            + _sub_floor_timeout_note(timeout_secs_val)
        )

    if name == "cron_update":
        # Channel-agent containment FIRST (see cron_add): cron_update maps
        # ``agent`` onto ``agent_id`` on an existing job, so it re-targets a
        # job's agent -- the same escalation, reachable without creating a job.
        chan_err = _deny_channel_agent_cron("cron_update")
        if chan_err:
            return chan_err
        jid = args["job_id"]
        # Ownership check
        own_err = _check_cron_job_ownership(svc, jid)
        if own_err:
            return own_err
        auto_err = _refuse_tool_auto_approval("cron_update", args)
        if auto_err:
            return auto_err
        kwargs: dict[str, Any] = {}
        for key in ("name", "message"):
            if key in args and args[key]:
                kwargs[key] = args[key]
        for key in ("agent", "channel", "thread_ts"):
            if key in args:
                val = args[key]
                if key == "thread_ts":
                    val = (val or "").strip() or None
                k = "agent_id" if key == "agent" else key
                kwargs[k] = val
        if "approval_mode" in args:
            kwargs["approval_mode"] = args["approval_mode"]
        if "silent" in args:
            kwargs["silent"] = args["silent"]
        # A boolean spelling of the clear: some clients drop an empty list before it
        # reaches this server, so ``skip_dates: []`` alone cannot be relied on to empty
        # the list. It never merges with a list that names dates.
        if args.get("clear_skip_dates") is True:
            if args.get("skip_dates"):
                return "Error: clear_skip_dates removes every skip date; pass it without skip_dates"
            kwargs["skip_dates"] = []
        elif "skip_dates" in args:
            sd = args["skip_dates"]
            if sd:
                for d in sd:
                    if not is_valid_skip_date(d):
                        return f"Error: invalid skip_date: {redact(str(d))!r} (expected YYYY-MM-DD)"
            kwargs["skip_dates"] = sd
        if "timezone" in args:
            tz_val = args["timezone"]
            if tz_val and not is_valid_timezone(tz_val):
                safe_tz = redact(tz_val)
                return f"Error: invalid timezone: {safe_tz!r}"
            kwargs["timezone"] = tz_val
        if "strict_schedule" in args:
            kwargs["strict_schedule"] = args["strict_schedule"]
        if "folder" in args:
            # "" resolves to "" (ungrouped) with no error, so an explicit empty
            # string moves the job out of its folder.
            fid, folder_err = _resolve_cron_folder(args["folder"], session_key=_authz_session_key())
            if folder_err:
                return f"Error: {folder_err}"
            kwargs["folder_id"] = fid
        if "persistent_session" in args:
            kwargs["persistent_session"] = args["persistent_session"]
        if "minimal_context" in args:
            mc = args["minimal_context"]
            if isinstance(mc, bool):
                kwargs["minimal_context"] = mc
        if "hide_in_chat" in args:
            hic = args["hide_in_chat"]
            if isinstance(hic, bool):
                kwargs["hide_in_chat"] = hic
        if "model" in args:
            m = str(args["model"] or "").strip()
            if m:
                # No membership gate: the model list is sourced from the live
                # kiro-cli `--list-models` (via /api/models), not the
                # claude_code registry family, so any id the CLI advertises is
                # valid. Matches the chat model path (which also skips
                # membership); the runtime is model-agnostic with a gateway
                # fallback. Only normalize the "auto" inherit sentinel.
                resolved_model = model_registry.to_provider_id(m, "claude_code")
                if resolved_model == "":
                    # "auto" sentinel — explicit inherit, same as clearing.
                    m = ""
            kwargs["model"] = m
        if "cron_expr" in args and args["cron_expr"]:
            kwargs["cron_expr"] = args["cron_expr"]
        if "every" in args and args["every"]:
            kwargs["every_secs"] = args["every"]
        if "timeout" in args:
            kwargs["timeout"] = args["timeout"]
        if "timeout_secs" in args:
            kwargs["timeout_secs"] = args["timeout_secs"]
        if "auto_pause_after_failures" in args:
            kwargs["auto_pause_after_failures"] = args["auto_pause_after_failures"]
        if "alert_after_failures" in args:
            kwargs["alert_after_failures"] = args["alert_after_failures"]
        if not kwargs:
            return "Error: no fields to update"
        # The owner's auto-approve covers the job the owner saw. A tool edit that
        # changes what the job does turns it off in the same write, so the owner
        # re-enables it on the Schedule page. Written unconditionally (a no-op on a
        # job that is not auto) so no read-then-write window can keep it on.
        was_auto = False
        if _AUTO_RESETTING_FIELDS.intersection(kwargs):
            prior = svc.get_job(jid)
            was_auto = prior is not None and prior.approval_mode == "auto"
            kwargs["approval_mode"] = ""
        kwargs["audit_actor"] = _authz_session_key()
        try:
            updated = svc.update_job(jid, **kwargs)
        except CronStoreBusy:
            return "Error: cron store busy, please retry"
        except CronStoreUnreadable as exc:
            return f"Error: {exc}"
        except ValueError as e:
            return f"Error: {e}"
        if not updated:
            return f"Error: job not found: {jid}"
        sel().log_api_access(
            caller="mcp",
            operation="cron.update",
            outcome="allowed",
            source="mcp",
            resources=f"job_id={jid}",
        )
        sched_str = format_schedule(updated.schedule, tz_name=updated.timezone or "")
        # Read from the SAVED row, not from ``args``: an update that leaves
        # persistent_session alone must still warn if the job is already in that
        # state and this call turned it into an agent job, and the flag is writable
        # here exactly as it is on cron_add -- warning only at creation would leave
        # the identical no-authority state reachable through a bare "Updated job:".
        caveat = _ephemeral_authority_caveat(
            updated.persistent_session,
            not (updated.command or updated.script),
            updated.agent_sequence,
        )
        note = _sub_floor_timeout_note(args["timeout_secs"]) if "timeout_secs" in args else ""
        if was_auto:
            note += _AUTO_APPROVAL_RESET_NOTE
        return f"Updated job: {updated.id} ({updated.name}) [{sched_str}]{caveat}{note}"

    if name == "cron_remove":
        jid = args["job_id"]
        # Ownership check
        own_err = _check_cron_job_ownership(svc, jid)
        if own_err:
            return own_err
        try:
            removed = svc.remove_job(jid, actor="mcp", source="mcp")
        except CronStoreBusy:
            return "Error: cron store busy, please retry"
        except CronStoreUnreadable as exc:
            return f"Error: {exc}"
        if removed:
            return f"Removed job: {jid}"
        return f"Error: job not found: {jid}"

    if name == "cron_remove_all":
        jobs = svc.list_jobs(include_disabled=True)
        if not jobs:
            return "No cron jobs to remove."
        session_key = _authz_session_key()
        if not session_key:
            return _unidentified_caller_refusal("cron_remove_all")
        jobs = _owned_by(jobs, session_key)
        if not jobs:
            return "No cron jobs owned by this session."
        sel().log_tool_invocation(
            session_key=session_key,
            source="mcp",
            tool_name="cron_remove_all",
            tool_kind="authz",
            outcome="scoped",
            resources=f"session={session_key} count={len(jobs)}",
        )
        try:
            # Distinct from the ``removed: bool`` that ``cron_remove``'s
            # single-job path binds above: this is the batch's removed-id LIST,
            # and reusing the name would rebind one variable to two types.
            removed_ids, _missing = svc.remove_jobs_sync(
                [j.id for j in jobs], actor=session_key, source="mcp"
            )
        except CronStoreBusy:
            return "Error: cron store busy, please retry"
        except CronStoreUnreadable as exc:
            return f"Error: {exc}"
        # main's count, not len(jobs): remove_jobs_sync reports what it actually
        # removed, so a requested id that was already gone is not counted.
        return f"Removed {len(removed_ids)} job(s)."

    if name == "cron_pause":
        jid = args["job_id"]
        # Ownership check
        own_err = _check_cron_job_ownership(svc, jid)
        if own_err:
            return own_err
        try:
            paused = svc.enable_job(jid, enabled=False)
        except CronStoreBusy:
            return "Error: cron store busy, please retry"
        except CronStoreUnreadable as exc:
            return f"Error: {exc}"
        if paused:
            return f"Paused job: {jid}"
        return f"Error: job not found: {jid}"

    if name == "cron_resume":
        jid = args["job_id"]
        # Ownership check
        own_err = _check_cron_job_ownership(svc, jid)
        if own_err:
            return own_err
        try:
            resumed = svc.enable_job(jid, enabled=True)
        except CronStoreBusy:
            return "Error: cron store busy, please retry"
        except CronStoreUnreadable as exc:
            return f"Error: {exc}"
        if resumed:
            return f"Resumed job: {jid}"
        return f"Error: job not found: {jid}"

    if name == "cron_trigger":
        jid = args["job_id"]
        # Shape BEFORE ownership: the id is about to be interpolated into a URL,
        # and a malformed one deserves its own message rather than the ownership
        # gate's deliberately vague "job not found". The check inside
        # ``trigger_cron_job`` is the enforcing one; this only makes its reason
        # reachable, since an unknown id never survives the ownership lookup.
        if not _JOB_ID_RE.fullmatch(jid):
            return f"Error: Invalid job ID format: {jid}"
        # Ownership check
        own_err = _check_cron_job_ownership(svc, jid)
        if own_err:
            return own_err
        # Resolve through the gateway-side serving resolver, not DASHBOARD_PORT and
        # not the client resolver: DASHBOARD_PORT reads KIROCREW_PORT only, and the
        # client resolver reads it FIRST -- both give 5476 on a --port auto gateway,
        # a SIBLING. resolve_serving_port prefers the bound port, so the credential
        # is paired to the instance actually serving and the job runs here.
        port = resolve_serving_port()
        secret_path = config_dir() / ".local_secret"
        ok, msg = trigger_cron_job(jid, port, secret_path)
        sel().log_api_access(
            caller="mcp",
            operation="cron.trigger",
            outcome="allowed" if ok else "error",
            source="mcp",
            resources=f"job_id={jid}",
        )
        if ok:
            return f"{msg} - executing now."
        # The audit row above already calls this an error; say so on the wire.
        return msg if msg.startswith("Error:") else f"Error: {msg}"

    if name == "cron_secret_request":
        jid = args["job_id"]
        # Ownership check — a session may only request secrets for a job it owns.
        own_err = _check_cron_job_ownership(svc, jid)
        if own_err:
            return own_err
        if args.get("withdraw") is True:
            # The spelling of ``secrets: {}`` that survives a client which drops an
            # empty object before it reaches this server; the schema has already
            # refused it beside a non-empty mapping, and the ownership gate above
            # has already found the job.
            return _withdraw_pending_request(svc, jid)
        secrets = args.get("secrets")
        if not isinstance(secrets, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in secrets.items()
        ):
            return "Error: secrets must be an object mapping env-var names to vault secret names"
        sjob = svc.get_job(jid)
        if sjob is None:
            return f"Error: job not found: {jid}"
        if not secrets:
            return _withdraw_pending_request(svc, jid)
        if not sjob.script:
            return (
                "Error: secret grants apply only to SCRIPT jobs. An agent "
                "job's session would expose the plaintext to the model; a "
                "command job's pin can cover only the command text, not the "
                "bytes of any helper file the command invokes."
            )
        try:
            validate_secret_env_grant(secrets)
        except ValueError as exc:
            return f"Error: {redact(str(exc))}"
        # Deliberately NO vault-name existence probe here: distinguishing
        # "stored" from "not stored" to an agent caller would let it enumerate
        # the owner's vault names by guessing. Names are validated on the
        # owner-only approval surfaces, where the operator sees the vault and
        # the request side by side; a request naming a missing secret is
        # simply refused there.
        try:
            # Pin the REQUEST to the job's current code. Approval re-verifies
            # this pin against the code at approval time, so what the operator
            # blesses is exactly what the agent showed them.
            pin = compute_secret_env_pin(
                sjob.script,
                sjob.command,
                sjob.message,
                job_id=sjob.id,
                grant=secrets,
                domain="pending",
                delivery=delivery_fingerprint(
                    sjob.session_key, sjob.silent, sjob.channel or "", sjob.thread_ts or ""
                ),
            )
        except (ValueError, FileNotFoundError, PermissionError, RuntimeError) as exc:
            return f"Error: {redact(str(exc))}"
        try:
            svc.update_job(
                jid,
                secret_env_pending=dict(secrets),
                secret_env_pending_pin=pin,
                secret_env_pending_ts=time.time(),
            )
        except CronStoreBusy:
            return "Error: cron store busy, please retry"
        except CronStoreUnreadable as exc:
            return f"Error: {exc}"
        except ValueError as exc:
            return f"Error: {redact(str(exc))}"
        sel().log_api_access(
            caller="mcp",
            operation="cron.secret_request",
            outcome="allowed",
            source="mcp",
            resources=f"job_id={jid}:{','.join(sorted(secrets))}",
        )
        # No in-chat approval surface, deliberately: an approval record
        # reachable from generic chat resolution paths keeps widening the
        # owner-only boundary, so approval lives EXCLUSIVELY on the
        # owner-gated Schedule page. The durable pending record above is the
        # source of truth.
        return (
            f"Recorded a PENDING secret request for job {jid} "
            f"({', '.join(sorted(secrets))}). Nothing is granted yet: the "
            "operator must approve it in the dashboard under Schedule > this "
            "job > Secrets. Tell the user to review and approve it there."
        )

    return f"Unknown tool: {name}"


#: Whether this server advertises ``kirocrew.caller-identity`` -- i.e. whether it
#: consumes the per-call caller block gatewayd injects instead of reading identity
#: from its own process. True here because it does: every authorization decision
#: goes through :func:`_authz_session_key`, whose first source is that block.
#:
#: Advertising is not cosmetic. ``mcp_gateway/backend_stubs.py`` strips any client-forged
#: caller block from EVERY forwarded request and re-injects its own only when the
#: backend advertised this capability -- so without the advertisement the block
#: never arrives, and this server's resolver reads an empty identity no matter how
#: correctly it is written. Nothing declines to POOL an unadvertised backend
#: (``rewriter.UNPOOLABLE_SERVERS`` lists only credential vendors and documents that the capability is
#: read only to decide injection), so the unadvertised state was not "per-session
#: spawn" -- it was pooled AND identity-blind.
#:
#: A module-level constant rather than a bare argument below so the value is
#: readable without executing :func:`run_mcp_server`, and so
#: ``test/test_mcp_managed_caller_identity.py`` can assert it against the argument
#: actually handed to the shim.
ADVERTISE_CALLER_IDENTITY = True


def run_mcp_server() -> None:
    """Run MCP stdio server — reads JSON-RPC from stdin, writes to stdout."""
    run_mcp_stdio_loop(
        "kirocrew-cron",
        "1.0.0",
        _list_tools,
        _call_tool,
        advertise_caller_identity=ADVERTISE_CALLER_IDENTITY,
        error_prefix_is_error=True,
        instructions=_INSTRUCTIONS,
    )


# This module is the facade of the cron MCP server: every name is reached through it as
# ``kiro_crew.mcp_cron.<name>``. A read is served by the owner that binds the name, and a
# write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every owner holding it
# (:mod:`kiro_crew._facade_mirror`). Owners never import this module. The server itself
# stays here: argument validation, the call path, the caller's identity and ownership
# checks, the tool bodies and the stdio entry point. New work goes to the owner of its
# responsibility:
#
#   ``mcp_cron_shell_scan`` -- the lexical scanners the cron shell gate runs on a command
#   ``mcp_cron_vet`` -- the cron gate: governance, command, script and fire-time vets
#   ``mcp_cron_tool_list`` -- the tool catalogue ``tools/list`` returns
#   ``mcp_cron_list_render`` -- how ``cron_list`` renders jobs (full, compact, JSON)

#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.mcp_cron_shell_scan",
    "kiro_crew.mcp_cron_vet",
    "kiro_crew.mcp_cron_tool_list",
    "kiro_crew.mcp_cron_list_render",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.mcp_cron_list_render import (  # noqa: F401
        _ERR_PREVIEW_LEN,
        _JSON_BYTE_BUDGET,
        _JSON_HISTORY_BUDGET_SECS,
        _JSON_HISTORY_LIMIT,
        _JSON_HISTORY_TIMEOUT_SECS,
        _JSON_MAX_JOBS,
        _JSON_MESSAGE_LEN,
        _MSG_PREVIEW_LEN,
        _NOOP_RE,
        _RESULT_PREVIEW_LEN,
        _fetch_history_stats,
        _fold_runs,
        _format_next_run,
        _job_kind,
        _normalize_summary,
        _sanitize,
    )
    from kiro_crew.mcp_cron_shell_scan import (  # noqa: F401
        _BACKSLASH_ESCAPE_RE,
        _BASH_WORD_BREAKING_WHITESPACE,
        _BRACE_SCAN_STEP_BUDGET,
        _CRON_BARE_HOME_RE,
        _CRON_BRACE_EXPANSION_RE,
        _CRON_CMD_CRED_PATH_RE,
        _CRON_CMD_SUBST_RE,
        _CRON_CRED_PATH_RE,
        _CRON_DOLLAR_EXPANSION_START,
        _CRON_DOLLAR_PLAIN_NEXT,
        _CRON_MAX_ASSIGNMENTS,
        _CRON_MAX_COMMAND_SCAN,
        _CRON_MAX_GLOB_WORD,
        _CRON_NESTED_REPARSER_RE,
        _CRON_POSITIONAL_PARAM_RE,
        _CRON_QUOTE_OR_BACKSLASH_RE,
        _CRON_SECRET_ENV_NAMES,
        _CRON_SECRET_ENV_RE,
        _CRON_SECRET_NAME_RE,
        _CRON_SHELL_KEYWORD_RE,
        _CRON_VAR_REF_ALLOWED,
        _CRON_VAR_REF_RE,
        _EXTGLOB_MAX_EXPANSIONS,
        _EXTGLOB_OP_RE,
        _MAX_SCRIPT_SCAN_BYTES,
        _SCRIPT_READ_PROBE_BYTES,
        _SENSITIVE_HOME_DIRS_BY_LOWER,
        _SHELL_STATE_QUOTE,
        _contains_glob_meta,
        _extglob_expansions,
        _extglob_group_end,
        _extglob_superset,
        _glob_could_reach_credentials,
        _has_bash_brace_expansion,
        _has_unplain_dollar,
        _iter_local_assignments,
        _matched_sensitive_name,
        _protected_path_refusal,
        _quote_states,
        _rebinds_home,
        _scan_one_level,
        _ScanTooComplex,
        _strip_shell_quotes,
    )
    from kiro_crew.mcp_cron_tool_list import (  # noqa: F401
        _ALERT_AFTER_DESCRIPTION,
        _AUTO_PAUSE_AFTER_DESCRIPTION,
    )
    from kiro_crew.mcp_cron_vet import (  # noqa: F401
        _CRON_FOLDER_ID_RE,
        _audit_fire_time_decision,
        _audit_governance_deny,
        _vet_app_owner_enabled,
        _vet_command_governance,
        _vet_script_contents,
        vet_job_at_fire_time,
    )

if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew.config.loader import (  # noqa: F401
        read_local_secret,
    )
    from kiro_crew.cron import (  # noqa: F401
        compute_next_run_ts,
        lookup_cron_folder_id,
    )
    from kiro_crew.cron_service.model import (  # noqa: F401
        _ALERT_AFTER_DEFAULT,
        _AUTO_PAUSE_MAX,
        _AUTO_PAUSE_THRESHOLD,
    )
    from kiro_crew.loopback_http import (  # noqa: F401
        loopback_urlopen,
    )
    from kiro_crew.mcp_tool_titles import (  # noqa: F401
        with_titles,
    )
    from kiro_crew.pinned_fs import (  # noqa: F401
        fd_real_path,
    )
    from kiro_crew.platform import (  # noqa: F401
        current_context,
        redact_log_via_context,
    )
    from kiro_crew.platform_compat import (  # noqa: F401
        strip_extended_length_prefix,
    )
    from kiro_crew.sandbox import (  # noqa: F401
        _AGENT_DENIED_ENV_KEYS,
    )
    from kiro_crew.security import (  # noqa: F401
        _SENSITIVE_HOME_DIRS,
        MAX_SCANNABLE_SOURCE_BODY_CHARS,
        _fold_line_continuations,
        _iter_shell_chars,
        audit_bash_exfiltration,
        enabled_rule_ids,
        is_sensitive_bash_command,
        is_sensitive_path,
        is_unverifiable_path_refusal,
        scan_exfiltration_urls,
        sensitive_path_refusal,
    )
    from kiro_crew.security.readonly_bash import (  # noqa: F401
        _EXTGLOB_RE,
    )
