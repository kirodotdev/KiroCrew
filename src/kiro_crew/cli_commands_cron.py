"""``kirocrew cron``: list, add, update, remove, pause, resume, trigger, adopt and preview.

``_cron`` runs ``_cron_dispatch`` and turns an unreadable store into one error line.
``_cron_add`` vets a new job with the checks the ``cron_add`` tool runs and records
its denial or success in the security event log; ``_cron_preview`` dry-runs a script
job. ``_format_schedule`` renders a schedule, with the full date of an ``at`` job.

An owner of the mirror facade ``kiro_crew.cli_commands``: callers and tests reach every
name here as ``kiro_crew.cli_commands.<name>``, and a patch through the facade reaches
this module. This module never imports the facade.
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import logging
import os
import re
import sys
import time as _time
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn
from zoneinfo import ZoneInfo

from kiro_crew import model_registry
from kiro_crew.config import config_dir
from kiro_crew.cron import (
    _JOB_TIMEOUT_SECS,
    _SUBPROC_CLEANUP_ALLOWANCE_SECS,
    CronSchedule,
    CronService,
    CronStoreBusy,
    CronStoreUnreadable,
    format_schedule,
    get_local_tz,
    is_valid_timezone,
    lookup_cron_folder_id,
    parse_time_string,
    validate_managed_by,
)
from kiro_crew.cron_script import resolve_script_path
from kiro_crew.cron_trigger import trigger_cron_job
from kiro_crew.history import ConversationLog
from kiro_crew.mcp_cron import _vet_cron_capability_governance, _vet_script_file, _vet_shell_command
from kiro_crew.platform import redact_log_via_context
from kiro_crew.port_resolution import resolve_client_port_ex
from kiro_crew.security import redact
from kiro_crew.sel import sel
from kiro_crew.validation import (
    _AGENT_NAME_RE,
    _MODEL_NAME_RE,
    CHANNEL_ID_RE,
    CHANNEL_MAX_LEN,
    CRON_ADD_SCHEMA,
    MAX_SHORT_STRING,
)


def _format_schedule(schedule: object, *, tz_name: str = "") -> str:
    """Human-readable schedule description (CLI shows full date for 'at' jobs)."""

    if not isinstance(schedule, CronSchedule):
        return str(schedule)
    if schedule.kind == "at" and schedule.at_ts:
        try:
            if tz_name:
                dt = datetime.fromtimestamp(schedule.at_ts, ZoneInfo(tz_name))
                return f"at {dt:%Y-%m-%d %H:%M %Z}"
            _, configured_tz = get_local_tz()
            dt = datetime.fromtimestamp(schedule.at_ts, configured_tz)
            return f"at {dt:%Y-%m-%d %H:%M %Z}"
        except Exception:
            # Same degrade-on-render posture as cron.format_schedule: an
            # extreme stored at_ts (beyond year 9999, epoch milliseconds)
            # must not crash `kirocrew cron list` -- fall through to the
            # shared renderer, whose own fallback string covers it.
            pass
    if tz_name:
        return format_schedule(schedule, tz_name=tz_name)
    return format_schedule(schedule)


def _cron_add_bounds(field_name: str) -> tuple[int, int]:
    """Numeric bounds of a ``cron add`` field, read from ``CRON_ADD_SCHEMA``.

    The MCP ``cron_add`` tool validates its input against that schema; the
    CLI enforces the same range by reading it rather than repeating the
    literals, so the two surfaces cannot drift apart.
    """
    spec = next(s for s in CRON_ADD_SCHEMA.fields if s.name == field_name)
    assert spec.min_val is not None and spec.max_val is not None, field_name
    return int(spec.min_val), int(spec.max_val)


def _cron_add_fail(msg: str, *, audit_kind: str = "") -> NoReturn:
    """Refuse a ``cron add`` on stderr with exit 1.

    Every refusal takes this one path so a headless installer can rely on a
    single contract: nothing is written, the reason is on stderr, and the exit
    status is non-zero. A refusal on stdout with exit 0 is indistinguishable
    from success to a script, which is why none of the checks below prints
    its own message.

    ``audit_kind`` names the job kind (``agent`` / ``script`` / ``command``)
    when the refusal is a SECURITY decision: the ``cron:cli_add`` capability
    gate denying authoring outright (any kind), a vetted command body, or a
    script path outside the crons root. A blocked job never reaches the
    kiro-cli permission/hook flow that would otherwise leave the audit
    trail, so the
    denial is recorded in the SEL here, as ``cron_add`` (``_log_cron_denial``)
    and the apps SDK do for their surfaces. Plain input validation (a bad
    flag combination, an unparseable time) is not a permission decision and
    leaves no event. The audited copy of the message is passed through
    ``redact_log_via_context`` here -- the gate-side log spelling, which
    applies a loaded companion's stricter pass and never raises -- so the
    invariant holds whoever produced the message: the vets already redact,
    but the script-resolution and identifier refusals relay a raw exception
    or the caller's own argument.
    """
    text = msg if msg.startswith("Error: ") else f"Error: {msg}"
    if audit_kind:
        # The refusal contract outranks the audit trail; denial logging is
        # best-effort, matching mcp_cron._log_cron_denial.
        try:
            sel().log_api_access(
                caller="cli",
                operation="cron.add",
                outcome="denied",
                source="cli",
                resources=f"kind={audit_kind}",
                error=redact_log_via_context(text),
            )
        except Exception:
            logging.getLogger("kiro_crew.cli_commands").debug(
                "cron add denial audit emit failed", exc_info=True
            )
    print(text, file=sys.stderr)
    sys.exit(1)


def _cron_add(svc: CronService, args: argparse.Namespace) -> None:
    """``kirocrew cron add``: validate every field, then ONE locked ``add_job``.

    The CLI is a thin front-end over ``CronService.add_job`` -- the same single
    writer the dashboard POST, the MCP ``cron_add`` tool and the apps SDK
    converge on. Every field is resolved here and handed to that one locked
    build+persist, so the job lands on disk fully-formed. The old shape --
    ``add_job`` with a subset, then ``job.agent_id = ...`` / ``job.silent = True``
    and a second, unlocked ``svc._save()`` -- was a data-loss window: the bare
    save could clobber a job another writer had appended between the two
    writes, and a crash between them left a job missing its agent.

    Job kind is exactly one of agent (the default), ``--script`` or
    ``--command``; schedule is exactly one of ``--every``, ``--cron`` or
    ``--at``. Schedule/timeout coupling is split by who can see it: when both
    ``--timeout`` and ``--timeout-secs`` are given, ``_build_job`` at the
    persistence owner validates the pair and the CLI surfaces its
    ``ValueError``; when ``--timeout-secs`` is omitted the CLI passes ``0``,
    which ``_build_job`` reads as "no wake budget given", so the CLI itself
    checks ``--timeout`` against the default wake budget it knows will apply.

    A ``--script`` file must already live under ``<config_dir>/crons/``
    (``resolve_script_path`` enforces containment) -- the CLI registers, it
    does not stage. Script bodies and shell commands go through the same
    storage-time security vet the MCP tool and the apps SDK apply
    (``_vet_script_file`` / ``_vet_shell_command``). The gateway re-vets at
    fire time regardless, so this is fail-fast for the caller, not the only
    guard: a job that would be refused at every wake is refused once, here,
    with a non-zero exit, instead of being stored and silently never running.
    """
    every = getattr(args, "every", None)
    cron_expr = (getattr(args, "cron_expr", None) or "").strip() or None
    at_raw = str(getattr(args, "at", None) or "").strip()
    tz = (getattr(args, "timezone", "") or "").strip()
    channel = (getattr(args, "channel", None) or "").strip() or None
    approval_mode = getattr(args, "approval_mode", "") or ""
    agent = (getattr(args, "agent", "") or "").strip()
    script = (getattr(args, "script", "") or "").strip()
    command = (getattr(args, "shell_command", "") or "").strip()
    model = (getattr(args, "model", "") or "").strip()
    silent = bool(getattr(args, "silent", False))
    hide_in_chat = bool(getattr(args, "hide_in_chat", False))
    timeout = getattr(args, "timeout", None)
    timeout_secs = getattr(args, "timeout_secs", None)
    folder_ref = (getattr(args, "folder", "") or "").strip()
    # None means the flag was not given; an explicit "" is a refusal, never
    # "unmanaged": an installer whose $KEY is unset would otherwise store a
    # keyless job on every run that its `remove --managed-by ""` can't find.
    managed_by = getattr(args, "managed_by", None)
    message = args.message or ""

    # ── Installer ownership key ──
    # Not stripped: the key is compared on bytes with the one the installer
    # recomputes, so a padded key is refused by the shape check below rather
    # than silently becoming a different key than the one it will look up.
    if managed_by is not None:
        try:
            validate_managed_by(managed_by)
        except ValueError as e:
            _cron_add_fail(str(e))

    # ── Job kind: exactly one of agent / script / command ──
    if script and command:
        _cron_add_fail("--script and --command are mutually exclusive")
    if agent and (script or command):
        _cron_add_fail("--agent cannot be combined with --script or --command")
    zero_token = bool(script or command)
    if not message and not zero_token:
        _cron_add_fail("message is required for an agent job (only --script/--command may omit it)")
    if agent and not _AGENT_NAME_RE.match(agent):
        # Read here, not at module level: the facade mirrors every module-level
        # import of its owners, and its import inventory is frozen.
        from kiro_crew.validation import AGENT_NAME_HINT

        _cron_add_fail(f"invalid agent name ({AGENT_NAME_HINT})")
    # ``--agent`` is the CLI's only field for naming either a provider template or
    # a crew member. Resolve the split the same way every create surface does, so
    # a member name is captured as a member selection (``member_id``) instead of a
    # bare template id the cron chat's reply path cannot resolve. ``agent_id`` stays
    # a pure template field, as the record contract requires.
    from kiro_crew.cron_service.identity import split_cron_agent_member

    agent, member_id = split_cron_agent_member(agent, "")
    if timeout is not None:
        if not zero_token:
            _cron_add_fail("--timeout applies only to a --script or --command job")
        lo, hi = _cron_add_bounds("timeout")
        if not lo <= int(timeout) <= hi:
            _cron_add_fail(f"--timeout must be within {lo}..{hi}, got {timeout}")
    if timeout_secs is not None:
        lo, hi = _cron_add_bounds("timeout_secs")
        if not lo <= int(timeout_secs) <= hi:
            _cron_add_fail(f"--timeout-secs must be within {lo}..{hi}, got {timeout_secs}")
    effective_timeout_secs = int(timeout_secs) if timeout_secs is not None else _JOB_TIMEOUT_SECS
    if (
        zero_token
        and timeout is not None
        and timeout_secs is None
        and int(timeout) + _SUBPROC_CLEANUP_ALLOWANCE_SECS > effective_timeout_secs
    ):
        _cron_add_fail(
            f"--timeout {timeout} plus cleanup exceeds the {effective_timeout_secs}s "
            "wake budget; pass a larger --timeout-secs"
        )

    # ── Schedule: exactly one of --every / --cron / --at ──
    given = [
        flag for flag, val in (("--every", every), ("--cron", cron_expr), ("--at", at_raw)) if val
    ]
    if len(given) != 1:
        _cron_add_fail("provide exactly one of --every, --cron or --at")
    # --timezone is the zone the job's wall clocks are read in: a --cron
    # expression's hour/minute fields and a time string given to --at, which
    # parse_time_string resolves in it (the configured timezone when omitted),
    # with its confirmation rendered in that same zone. An interval (--every)
    # has no wall clock to interpret, so that pair is refused rather than
    # persisted as a field the schedule never consults.
    if tz and every:
        _cron_add_fail(
            "--timezone applies to --cron and --at only; an --every interval has no "
            "wall clock to read in it"
        )
    if tz and not is_valid_timezone(tz):
        _cron_add_fail(f"invalid timezone: {tz!r}")
    at_ts: float | None = None
    if at_raw:
        if len(at_raw) > 64:
            _cron_add_fail("--at must be at most 64 characters")
        try:
            try:
                at_ts = float(at_raw)
            except ValueError:
                parsed = parse_time_string(at_raw, tz)
                if isinstance(parsed, str):
                    _cron_add_fail(parsed)
                at_ts = parsed
            if at_ts > 4102444800:
                _cron_add_fail(
                    "--at must resolve to a timestamp at or before 4102444800 (year 2100)"
                )
            # Guard against a past instant from either spelling, as cron_add does:
            # an `at` job in the past would fire immediately and vanish.
            if at_ts < _time.time():
                # Render in the zone parse_time_string resolved the value in,
                # so the refusal echoes a wall clock the operator recognises.
                shown_tz = ZoneInfo(tz) if tz else get_local_tz()[1]
                local = datetime.fromtimestamp(at_ts, shown_tz)
                _cron_add_fail(
                    f"resolved time {local.strftime('%Y-%m-%d %I:%M %p %Z')} is in the past"
                )
        except (OverflowError, OSError, ValueError):
            _cron_add_fail("--at is outside the supported timestamp range")

    # ── Fields validated up front so an invalid value never strands a job ──
    if channel and (len(channel) > CHANNEL_MAX_LEN or not CHANNEL_ID_RE.match(channel)):
        _cron_add_fail(f"invalid channel ID format (expected {CHANNEL_ID_RE.pattern.strip('^$')})")
    if model:
        if len(model) > MAX_SHORT_STRING:
            _cron_add_fail(f"invalid model format (expected at most {MAX_SHORT_STRING} characters)")
        if not _MODEL_NAME_RE.match(model):
            # Read here, not at module level: the facade mirrors every module-level
            # import of its owners, and its import inventory is frozen.
            from kiro_crew.validation import MODEL_NAME_HINT

            _cron_add_fail(f"invalid model format ({MODEL_NAME_HINT})")
        # Same normalisation as the dashboard and cron_add: the "auto" inherit
        # sentinel resolves to no pinned provider id and is stored as unset.
        if model_registry.to_provider_id(model, "claude_code") == "":
            model = ""
    # Resolve BEFORE add_job so a typo'd folder never leaves an orphaned
    # job behind. Existing folders only: cron_folders.json is owned by the
    # dashboard (its state rewrites the file wholesale), so a CLI-side
    # create could be silently clobbered by the next Schedule-page folder
    # operation.
    folder_id = ""
    if folder_ref:
        found = lookup_cron_folder_id(folder_ref)
        if found.error:
            _cron_add_fail(found.error)
        folder_id = found.folder_id

    if script:
        kind = "script"
    elif command:
        kind = "command"
    else:
        kind = "agent"

    # ── Governance: the capabilities.cron on/off gate, at authoring time ──
    # The gateway re-vets every job at fire time, so a job authored under a
    # disabled capability can never run -- but without this fail-fast the CLI
    # would persist it and print "Added job", and the caller would only learn
    # otherwise from the job's error status at its first wake. Same call
    # cron_add makes before it stores anything. The `cron:` key classifies as
    # the cron surface (sel._infer_source), so the profile that governs cron
    # jobs decides authoring too -- a CLI-surface bind does not, by design:
    # what is being gated is the cron capability, not the CLI as a whole.
    cap_err = _vet_cron_capability_governance("cron:cli_add")
    if cap_err:
        _cron_add_fail(cap_err, audit_kind=kind)

    # ── Storage-time security vet for the two zero-token kinds ──
    if command:
        err = _vet_shell_command(command)
        if err:
            _cron_add_fail(err, audit_kind="command")
    if script:
        # Two leading path separators in any mix (`\\`, `//`, `/\`, `\/`) name
        # a UNC/network path: Windows normalises the alternate separator
        # first, so Path.resolve() would do an outbound host lookup before
        # containment is checked. Refuse the shape before it reaches
        # resolve_script_path; CRON_ADD_SCHEMA's script regex in validation.py
        # excludes the same shape (^(?![\\/]{2})).
        if re.match(r"^[\\/]{2}", script):
            _cron_add_fail(
                "--script must be a local path, not a UNC/network path (leading \\\\ or //)",
                audit_kind="script",
            )
        try:
            script_file, func = resolve_script_path(script)
        except (PermissionError, FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            _cron_add_fail(str(exc) or "could not resolve script path", audit_kind="script")
        # Reject malformed entry points before reading the script body.
        if not func.isidentifier():
            _cron_add_fail(
                f"--script function name {func!r} must be a valid Python identifier "
                "(expected FILE.py:FUNC)",
                audit_kind="script",
            )
        err = _vet_script_file(script_file)
        if err:
            _cron_add_fail(err, audit_kind="script")
        # Persist the RESOLVED absolute spec, not the caller's raw one. A
        # relative --script resolves against the CLI's CWD here; the gateway
        # re-vets at fire time against ITS CWD, so a raw relative spec would
        # fail every wake. An already-absolute spec round-trips unchanged.
        script = f"{script_file}:{func}"

    # ── Session shape: the store's defaults, exactly as cron_add and the
    # dashboard POST forward them. One defaulting policy across every create
    # surface, so the same --command job means the same job whichever door it
    # came through; a caller that wants a fresh minimal wake says so with
    # --no-persistent-session --minimal-context (the help text recommends
    # both for a --script/--command job).
    persistent_session = bool(getattr(args, "persistent_session", True))
    minimal_context = bool(getattr(args, "minimal_context", False))

    try:
        fields = dict(
            name=args.name,
            message=message,
            every_secs=every,
            cron_expr=cron_expr,
            at_ts=at_ts,
            # One-shot: derived from the schedule, never caller-supplied, exactly
            # as cron_add and the dashboard derive it -- a job with a single fire
            # time that never leaves the store is one the scheduler cannot re-run.
            delete_after_run=bool(at_ts),
            channel=channel,
            approval_mode=approval_mode,
            agent_id=agent,
            model=model,
            silent=silent,
            timezone=tz,
            hide_in_chat=hide_in_chat,
            folder_id=folder_id,
            command=command,
            script=script,
            persistent_session=persistent_session,
            minimal_context=minimal_context,
            timeout=int(timeout) if timeout else 0,
            timeout_secs=int(timeout_secs) if timeout_secs else 0,
            auto_pause_after_failures=getattr(args, "auto_pause_after_failures", None),
        )
        # ``member_id`` rides in the call ONLY when ``--agent`` resolved to a
        # crew member; a non-member (template or default) agent keeps the exact
        # add_job call shape the create contract pins, so no member kwarg leaks
        # into the common path.
        if member_id:
            fields["member_id"] = member_id
        if managed_by is not None:
            job = svc.add_managed_job(managed_by, **fields)
        else:
            job = svc.add_job(**fields)
    except CronStoreBusy:
        # The store lock stayed contended past its timeout (another writer --
        # the gateway, a dashboard create -- holds it). Nothing was written;
        # the same retryable refusal cron_add returns, not a traceback.
        _cron_add_fail("cron store busy, please retry")
    except ValueError as e:
        _cron_add_fail(str(e))
    except OSError as e:
        # The store write failed (disk full, permissions, a vanished config
        # dir). _save is tmp-then-rename, so the store is untouched and the
        # refusal contract still holds: reason on stderr, exit 1, nothing
        # written -- not a traceback an installer cannot classify.
        _cron_add_fail(f"could not write the cron store: {e}")
    sched_desc = _format_schedule(job.schedule, tz_name=job.timezone or "")
    # Once add_job persists, the caller-facing contract is the printed id and exit 0.
    # An audit failure must not turn a committed job into a spurious non-zero exit
    # that an installer retries into a duplicate.
    try:
        sel().log_api_access(
            caller="cli",
            operation="cron.add",
            outcome="allowed",
            source="cli",
            resources=(
                f"job_id={job.id} kind={kind} approval_mode={approval_mode or 'default'} "
                f"agent={agent or 'default'} silent={silent}"
                + (f" managed_by={managed_by}" if managed_by else "")
            ),
        )
    except Exception:
        logging.getLogger("kiro_crew.cli_commands").debug(
            "cron add success audit emit failed", exc_info=True
        )
    print(f"Added job: {job.id} ({job.name}) [{sched_desc}]")


def _cron(args: argparse.Namespace) -> None:
    """Dispatch cron subcommands, translating a refused write into an error.

    ``CronService._save`` raises ``CronStoreUnreadable`` rather than silently
    skipping the write when the last load failed, so every mutating verb here can
    fail that way. Untranslated it reached the user as a stack trace, which names
    the raise site but not the one action that fixes it. The exception's own
    message carries that remediation, so it is surfaced verbatim.

    One wrapper rather than a handler per verb: `add`, `update`, `remove`,
    `pause`, `resume` and `adopt` all persist through the same ``_save``, so
    catching at the dispatch boundary covers them without eight duplicated
    blocks, and any verb added later is covered by construction.

    A ``PermissionError`` is translated the same way. The OS can refuse this
    process a path the gateway itself can read: macOS data protection blocks a
    terminal from even stat-ing ``.vault``, which ``cron remove`` must read to
    revoke a job's secret grant before deleting it. The verb still refuses (the
    revoke fence stays fail-closed); only the traceback is replaced by one line
    naming the path and the OS refusal.
    """
    try:
        _cron_dispatch(args)
    except CronStoreUnreadable as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except PermissionError as exc:
        if exc.errno is None:
            # A policy refusal (``raise PermissionError("...")``), not the OS:
            # keep it as it was rather than mislabel it an OS refusal.
            raise
        where = exc.filename or "a Kiro Crew data file"
        print(
            f"Error: the OS refused this process access to {where} ({exc.strerror or exc}). "
            "Manage the job from the dashboard Schedule page, "
            "or grant this terminal access to the data home (macOS: Full Disk Access).",
            file=sys.stderr,
        )
        sys.exit(1)


def _cron_dispatch(args: argparse.Namespace) -> None:
    """Dispatch cron subcommands: list, add, remove, pause, resume."""

    svc = CronService(base_dir=config_dir())

    action = getattr(args, "cron_action", None)
    if action == "list":
        jobs = svc.list_jobs(include_disabled=True)
        if not jobs:
            print("No cron jobs.")
            return
        for j in jobs:
            status = "✅" if j.enabled else "⏸️"
            sched = _format_schedule(j.schedule, tz_name=j.timezone or "")
            print(f"  {status} {j.id}  {j.name}  ({sched})  {j.message[:60]}")
            # Ownership is printed because it decides which surfaces can manage
            # the job at all: a job with no owning session is outside every chat
            # session's scope, so `cron_list` from chat does not list it and the
            # mutating tools answer a deliberately vague "job not found". That is
            # the intended boundary, but with the field invisible here the CLI
            # was the only place the state existed and nothing showed it -- which
            # is what made a normal, correct state read as a job that had
            # vanished. `cron adopt` is the way back.
            owner = j.session_key or ""
            provenance = j.created_by or ""
            if owner:
                detail = f"owner: {owner}"
            else:
                detail = "owner: none (manage from CLI or the dashboard Schedule page)"
            if provenance:
                detail += f"  created by: {provenance}"
            if j.managed_by:
                detail += f"  managed by: {j.managed_by}"
            print(f"      {detail}")

    elif action == "adopt":
        job_id = args.job_id
        if getattr(args, "release", False):
            session_key = ""
        else:
            # One flag, two accepted spellings of the same target. A bare slot
            # name gets the `dashboard:` namespace the delivery consumers strip
            # back off (messaging.py / the Slack gateway both
            # removeprefix("dashboard:")), so adding it here is their exact
            # inverse and needs no lookup. An already-namespaced key passes
            # through untouched -- there is no second flag for that case,
            # because a key with no namespace at all could never equal any
            # caller's session key and so could only ever produce a row nobody
            # can own.
            target = (getattr(args, "session_of", None) or "").strip()
            if not target:
                print("Error: --session-of requires a session", file=sys.stderr)
                sys.exit(1)
            session_key = target if ":" in target else f"dashboard:{target}"
        if not svc.adopt_job(job_id, session_key):
            print(f"Error: job not found: {job_id}", file=sys.stderr)
            sys.exit(1)
        sel().log_api_access(
            caller="cli",
            operation="cron.adopt",
            outcome="allowed",
            source="cli",
            resources=f"job_id={job_id} session_key={session_key or '(released)'}",
        )
        if session_key:
            # Ownership and delivery do not have the same reach. `_owned_by`
            # matches any namespace, so a Slack or Telegram session can own and
            # manage a job -- but only a `dashboard:` key resolves to a slot the
            # delivery path can inject into (both consumers reach a slot with
            # removeprefix("dashboard:")). Saying "results are delivered there"
            # for a `slack:` key would be a promise the code does not keep.
            if session_key.startswith("dashboard:"):
                print(
                    f"Job {job_id} now belongs to {session_key}: that session can manage it "
                    f"and its results are delivered there."
                )
                # A typo'd key is accepted by the store but resolves to no slot,
                # so the job's output would go nowhere -- the same
                # invisible-delivery state this command exists to recover from.
                # Warn rather than refuse: the delivery path resolves a live slot
                # first and only falls back to rehydrating from history, so a
                # brand-new tab that has not logged anything yet is a legitimate
                # target and absence of a log does not prove the key is wrong.
                #
                # Resolve the transcript the way DELIVERY resolves it, rather
                # than guessing at the spelling. The original bug was asking for
                # the prefix-stripped slot, which found nothing for EVERY
                # dashboard session and warned on every correct adopt -- which
                # trains the operator to ignore the one message that would also
                # be a real typo's only signal. Passing the raw session key
                # instead fixes the common ``chat-N`` spelling but reintroduces
                # the same false warning for the two spellings where the two
                # differ: a channel-origin slot (``dashboard:slack_<ts>``, whose
                # transcript is ``slack_<ts>.jsonl`` with no ``dashboard_``
                # prefix) and a stacked prefix (``--session-of
                # dashboard_chat-N-...`` becomes ``dashboard:dashboard_chat-N``).
                # ``_normalize_slot_key`` + ``slot_transcript_key`` are the pair
                # the delivery path itself composes, so reusing them keeps the
                # check and the delivery it predicts from drifting apart.
                try:
                    from kiro_crew.dashboard.chat_utils import slot_transcript_key
                    from kiro_crew.dashboard.state import _normalize_slot_key

                    transcript_key = slot_transcript_key(_normalize_slot_key(session_key))
                    known = ConversationLog().has_log(transcript_key)
                except Exception:
                    known = True  # cannot tell -> stay quiet rather than cry wolf
                if not known:
                    print(
                        f"Warning: no recorded session named "
                        f"{session_key.removeprefix('dashboard:')!r}. If that is a "
                        f"typo, the job's results will not reach anyone -- re-run "
                        f"with the right key, or `--release` to undo.",
                        file=sys.stderr,
                    )
            else:
                print(f"Job {job_id} now belongs to {session_key}: that session can manage it.")
                print(
                    f"Note: results are not injected into a chat for a "
                    f"{session_key.split(':', 1)[0]!r} owner -- only a dashboard session is "
                    f"resolved as a delivery target. Ownership transferred; delivery did not.",
                    file=sys.stderr,
                )
        else:
            print(
                f"Job {job_id} released: no owning session, so manage it from the CLI or "
                f"the dashboard Schedule page."
            )

    elif action == "add":
        _cron_add(svc, args)

    elif action == "update":
        kwargs: dict = {}
        for field in (
            "name",
            "message",
            "every_secs",
            "cron_expr",
            "channel",
            "timeout_secs",
            "auto_pause_after_failures",
        ):
            val = getattr(args, field, None)
            if val is not None:
                if field == "channel":

                    val = val.strip() or None
                    if val is None:
                        continue
                    if len(val) > CHANNEL_MAX_LEN or not CHANNEL_ID_RE.match(val):
                        print(
                            f"Error: invalid channel ID format (expected {CHANNEL_ID_RE.pattern.strip('^$')})"
                        )
                        return
                kwargs[field] = val
        if getattr(args, "agent", None) is not None:
            agent_val = args.agent.strip()
            if agent_val and not _AGENT_NAME_RE.match(agent_val):
                # Read here, not at module level: the facade mirrors every
                # module-level import of its owners, and its inventory is frozen.
                from kiro_crew.validation import AGENT_NAME_HINT

                print(f"Error: invalid agent name ({AGENT_NAME_HINT})", file=sys.stderr)
                sys.exit(1)
            kwargs["agent_id"] = agent_val
        if getattr(args, "approval_mode", None) is not None:
            kwargs["approval_mode"] = "" if args.approval_mode == "default" else args.approval_mode
        if not kwargs:
            print("Provide at least one field to update")
            return
        if "every_secs" in kwargs and "cron_expr" in kwargs:
            print("Provide --every or --cron, not both")
            return
        try:
            updated = svc.update_job(args.job_id, **kwargs)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        if updated:

            audit_resources = f"job_id={args.job_id} fields={','.join(sorted(kwargs))}"
            if "agent_id" in kwargs:
                # Same rationale as cron.add: the agent picks the sandboxed
                # subprocess, so the value belongs in the audit trail.
                audit_resources += f" agent={kwargs['agent_id'] or 'default'}"
            sel().log_api_access(
                caller="cli",
                operation="cron.update",
                outcome="allowed",
                source="cli",
                resources=audit_resources,
            )
            print(f"Updated job: {updated.id} ({updated.name})")
        else:

            sel().log_api_access(
                caller="cli",
                operation="cron.update",
                outcome="not_found",
                source="cli",
                resources=f"job_id={args.job_id} reason=not_found",
            )
            print(f"Job not found: {args.job_id}")

    elif action == "remove":
        job_id = getattr(args, "job_id", None)
        managed_by = getattr(args, "managed_by", None)
        if managed_by is not None:
            # An empty or malformed key must never reach the match below: every
            # unmanaged job carries managed_by == "", so `--managed-by ""` (an
            # installer whose $KEY is unset) would otherwise delete all of them.
            try:
                validate_managed_by(managed_by)
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)
            # Read-decide-write: an unreadable store reads as empty, which would
            # report "nothing to remove" over a corrupt file an installer then
            # believes is clean. Probe first so that case refuses instead.
            svc.raise_if_store_unreadable()
            jobs = svc.list_jobs(include_disabled=True)
            owned = [j.id for j in jobs if j.managed_by == managed_by]
            if not owned:
                print(f"No job is managed by: {managed_by}")
                return
            # Removed by the ids just read: a re-install that lands in between
            # mints a fresh id, which this removal cannot touch.
            for jid in owned:
                if svc.remove_job(jid, actor="cli", source="cli"):
                    print(f"Removed job: {jid} (managed by {managed_by})")
            return
        removed = svc.remove_job(job_id, actor="cli", source="cli")
        if removed:
            print(f"Removed job: {job_id}")
        else:
            print(f"Job not found: {job_id}")

    elif action == "pause":
        if svc.enable_job(args.job_id, enabled=False):
            print(f"Paused job: {args.job_id}")
        else:
            print(f"Job not found: {args.job_id}")
            sys.exit(1)

    elif action == "resume":
        if svc.enable_job(args.job_id, enabled=True):
            print(f"Resumed job: {args.job_id}")
        else:
            print(f"Job not found: {args.job_id}")
            sys.exit(1)

    elif action == "trigger":
        # Instance-aware, for the same reason as the MCP trigger: DASHBOARD_PORT reads
        # KIROCREW_PORT only, so on a --port auto gateway it names a sibling, and the
        # paired credential would let that sibling run the job.
        port, _evidence_backed = resolve_client_port_ex(None)
        secret_path = config_dir() / ".local_secret"
        ok, msg = trigger_cron_job(args.job_id, port, secret_path)
        print(msg)
        sel().log_api_access(
            caller="cli",
            operation="cron.trigger",
            outcome="allowed" if ok else "error",
            source="cli",
            resources=f"job_id={args.job_id}",
        )
        if not ok:
            sys.exit(1)

    elif action == "preview":
        _cron_preview(args)

    else:
        print("Usage: kirocrew cron {list|add|update|remove|pause|resume|trigger|preview}")


def _cron_preview(args: argparse.Namespace) -> None:
    """Dry-run a script cron with real MCP tools but suppressed hooks."""
    # Imported here (not at module top) to avoid a cron_script import cycle.
    from kiro_crew.cron_script import Done, KeptMcpServers, Report, Skip, resolve_script_path

    # Resolve and validate script path (same validation as production cron runner:
    # format, existence, sensitive path, containment under ~/.kiro/crew/crons/)
    try:
        script_path, func_name = resolve_script_path(args.script)
    except (ValueError, FileNotFoundError, PermissionError) as e:
        sel().log_api_access(
            caller="cli",
            operation="cron.preview",
            outcome="denied",
            source="cli",
            resources=f"script={args.script} reason={type(e).__name__}",
        )
        print(f"Error: {e}")
        sys.exit(1)

    # Set env vars before loading module so top-level code can see them
    for kv in args.env or []:
        if "=" in kv:
            k, v = kv.split("=", 1)
            os.environ[k] = v

    # Load the script module
    spec = importlib.util.spec_from_file_location("_cron_preview_module", script_path)
    if spec is None or spec.loader is None:
        print(f"Error: cannot load {script_path}")
        sys.exit(1)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec, as the production child does: dataclasses resolves
    # a postponed annotation through sys.modules[cls.__module__].
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    func = getattr(module, func_name, None)
    if func is None:
        print(f"Error: function '{func_name}' not found in {script_path}")
        sys.exit(1)
    if inspect.iscoroutinefunction(func):
        print(
            f"Error: function '{func_name}' is async; cron preview only supports synchronous functions"
        )
        sys.exit(1)

    @dataclass
    class _PreviewJob:
        id: str = "preview-dry-run"

    class _LiveTestCtx:
        """Dry-run ctx: real MCP tools, suppressed hooks.

        Runs in-process (not sandboxed) unlike production's run_script_sandboxed.
        Acceptable because: scripts are constrained to ~/.kiro/crew/crons/ via
        resolve_script_path, and the command is user-initiated from their terminal."""

        def __init__(self, message: str):
            self.message = message
            self.job = _PreviewJob()
            # Bare on purpose: the preview presents no cron identity to its servers.
            self._kept_servers = KeptMcpServers()

        def call_tool(self, server: str, tool: str, tool_args: dict) -> str:
            # Redact credentials/exfiltration URLs (same as production ScriptContext.call_tool)
            args_str = json.dumps(tool_args)
            args_str = redact(args_str)
            safe_args = json.loads(args_str)
            # One server per name kept for the run, stopped by close() once the
            # script returns (same lifecycle as production ScriptContext.call_tool)
            outcome = "ok"
            try:
                result = self._kept_servers.call_tool(server, tool, safe_args)
            except Exception:
                outcome = "error"
                raise
            finally:
                sel().log_tool_invocation(
                    session_key=f"cron:{self.job.id}",
                    tool_name=f"{server}/{tool}",
                    tool_kind="cron_preview_tool",
                    outcome=outcome,
                )
            return result

        def notify(self, text: str, **kwargs: object) -> dict:
            # Signature mirrors production ScriptContext.notify: scripts pass routing
            # kwargs (session="origin" is the documented way for a cron to reach the
            # chat that created it), so a positional-only stub made preview crash on
            # the one branch a monitor cron exists for. Redaction mirrors production
            # too -- this prints to the user's terminal, and kwargs values are
            # script-supplied. Returns an empty dict: nothing was delivered.
            safe_text = redact(text)
            safe_kwargs = json.loads(redact(json.dumps(kwargs))) if kwargs else {}
            routing = f" (kwargs: {safe_kwargs})" if safe_kwargs else ""
            print(f"[notify suppressed]: {safe_text}{routing}")
            return {}

        def close(self):
            self._kept_servers.close()

    ctx = _LiveTestCtx(message=args.message)
    outcome = "ok"
    try:
        func(ctx)
        print("\n✅ Completed (no exception raised)")
    except Skip:
        print("\n⏭️  Skip (nothing to report)")
    except Report as r:
        print(f"\n📢 Report:\n{r}")
    except Done as d:
        print(f"\n🏁 Done:\n{d}")
    except Exception as e:
        outcome = "error"
        print(f"\n❌ Error: {type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        ctx.close()
        sel().log_api_access(
            caller="cli",
            operation="cron.preview",
            outcome=outcome,
            source="cli",
            resources=f"script={script_path}:{func_name}",
        )
    if outcome == "error":
        sys.exit(1)
