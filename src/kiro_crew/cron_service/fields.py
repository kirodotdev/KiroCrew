"""What a valid cron job is, and what a valid change to one is.

The persistence chokepoint for every create surface (dashboard, MCP, CLI, apps
SDK, onboarding import): :func:`build_job` validates the fields and constructs
the job BEFORE any store I/O, and :func:`apply_job_update` validates a partial
update against the freshly reloaded job and only then assigns, so a refused
change never leaves a half-applied job for a later save to persist. Both run
their string fields through one table (:data:`_CRON_STRING_FIELD_CAPS`), whose
caps match the REST/MCP boundary schemas.

The store transaction around them -- lock, reload, save -- is the service's.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

from kiro_crew import cron_script
from kiro_crew.cron_service.execution import _SUBPROC_CLEANUP_ALLOWANCE_SECS
from kiro_crew.cron_service.model import CronJob, CronSchedule
from kiro_crew.cron_service.schedule import (
    is_valid_skip_date,
    is_valid_timezone,
    validate_cron_expr,
)
from kiro_crew.cron_service.store import _is_representable_number
from kiro_crew.validation import CHANNEL_MAX_LEN, MAX_CRON_MESSAGE, MAX_SHORT_STRING

_MIN_INTERVAL_SECS = 60

# Shown verbatim under the Schedule form's Save button, so it names the rule
# and the next step rather than only the refusal.
_CHAT_FOLDER_NEEDS_PERSISTENT = (
    "Filing runs into a chat folder needs a persistent session: a stateless job "
    "has no job-wide tab to file. Clear the chat folder, or keep the session."
)

# Table-driven string-field caps for the persistence chokepoint. Every
# caller-supplied string field persisted by _build_job/_update_job_locked
# is listed here with its cap matching the REST/MCP boundary schemas
# (CRON_ADD_SCHEMA / cron_update ToolSchema in validation.py). A helper
# iterates this table so adding a field requires ONE edit, not two.
_CRON_STRING_FIELD_CAPS: tuple[tuple[str, int], ...] = (
    ("name", MAX_SHORT_STRING),
    ("message", MAX_CRON_MESSAGE),
    ("channel", CHANNEL_MAX_LEN),
    ("thread_ts", 30),
    ("agent_id", MAX_SHORT_STRING),
    ("member_id", MAX_SHORT_STRING),
    ("created_by", MAX_SHORT_STRING),
    ("source_preset", MAX_SHORT_STRING),
    ("source_template_prompt", MAX_CRON_MESSAGE),
    ("folder_id", MAX_SHORT_STRING),
    ("chat_folder_id", MAX_SHORT_STRING),
    ("session_key", MAX_SHORT_STRING),
    ("model", MAX_SHORT_STRING),
    ("command", 5000),
    ("script", 200),
    ("timezone", 50),
    # Secret-grant fields have no boundary FieldSpec: the pins are sha256 hex
    # digests computed server-side by the grant endpoint / cron_secret_request
    # tool (grant validity is enforced by pin equality at fire time, not by
    # this length gate). Per the no-schema convention they use the general ID cap.
    ("secret_env_pin", MAX_SHORT_STRING),
    ("secret_env_pending_pin", MAX_SHORT_STRING),
)


# The shape of an installer's provenance key (CronJob.managed_by). Deliberately
# not in _CRON_STRING_FIELD_CAPS: that table also gates apply_job_update, and
# the key is create-only. ASCII, starting with an alphanumeric, so it is never
# confusable with a flag, never carries whitespace or a control character, and
# always compares on bytes the way the installer that recomputes it expects.
MANAGED_BY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}")


def validate_managed_by(key: str) -> str:
    """Return *key* when it is a usable installer key, else raise ``ValueError``."""
    if not isinstance(key, str) or not MANAGED_BY_RE.fullmatch(key):
        raise ValueError(
            "managed-by key must be 1-200 ASCII characters from A-Z a-z 0-9 . _ : / @ + - "
            "and start with a letter or digit"
        )
    return key


def _validate_cron_string_fields(
    values: dict[str, object],
    *,
    required: frozenset[str] = frozenset(),
) -> None:
    """Type+length gate for every caller-supplied string field.

    Iterates _CRON_STRING_FIELD_CAPS. For each field:
    - If in *required*: always validates (rejects non-str or over-cap).
    - Otherwise: ``None`` and ``""`` mean "not set" and are skipped; any
      other value — including falsy non-strings like ``[]`` or ``0``, which
      a bare truthiness test would silently admit — must be a string within
      the cap.
    """
    for field_name, cap in _CRON_STRING_FIELD_CAPS:
        val = values.get(field_name)
        if field_name in required:
            if not isinstance(val, str):
                raise ValueError(f"{field_name} must be a string")
            if len(val) > cap:
                raise ValueError(f"{field_name} exceeds max length {cap}")
        else:
            if val is None or val == "":
                continue
            if not isinstance(val, str):
                raise ValueError(f"{field_name} must be a string")
            if len(val) > cap:
                raise ValueError(f"{field_name} exceeds max length {cap}")


def build_job(
    name: str,
    message: str,
    every_secs: int | None = None,
    at_ts: float | None = None,
    cron_expr: str | None = None,
    channel: str | None = None,
    thread_ts: str | None = None,
    delete_after_run: bool = False,
    created_by: str = "",
    approval_mode: str = "",
    enabled: bool = True,
    agent_id: str = "",
    member_id: str = "",
    model: str = "",
    silent: bool = False,
    timezone: str = "",
    skip_dates: list[str] | None = None,
    strict_schedule: bool = False,
    hide_in_chat: bool = False,
    folder_id: str = "",
    chat_folder_id: str = "",
    command: str = "",
    script: str = "",
    agent_sequence: list[str] | None = None,
    env: dict[str, str] | None = None,
    persistent_session: bool = True,
    session_key: str = "",
    minimal_context: bool = False,
    timeout: int = 0,
    timeout_secs: int = 0,
) -> CronJob:
    """Validate inputs and construct the :class:`CronJob` (no I/O, no lock).

    Shared by :meth:`add_job` and :meth:`add_job_async` so both perform
    identical validation on the event loop before any disk work. Raises
    ``ValueError`` on an invalid schedule or approval mode.

    ``timeout_secs`` is the per-wake execution budget (the
    ``asyncio.wait_for`` deadline in ``_execute_with_timeout``); ``0`` means
    the ``_JOB_TIMEOUT_SECS`` default. Distinct from ``timeout``, which
    bounds only script/command subprocesses.

    The optional presentation/routing fields (``agent_id``, ``model``,
    ``silent``, ``timezone``, ``strict_schedule``, ``hide_in_chat``) are set
    here so the job is persisted **fully-formed** in the single locked
    transaction. This closes a create-then-mutate-then-unlocked-``_save``
    window (two concurrent creates could otherwise interleave at the
    ``await`` and the unlocked save could clobber the other request's job).
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    valid_approval_modes = ("", "auto")
    if approval_mode not in valid_approval_modes:
        raise ValueError(f"Invalid approval_mode: {approval_mode!r}")
    # Writer mirror of _job_from_record's representable-number type-shape
    # check: the reader may skip a record with a non-representable
    # schedule numeric (NaN/Infinity/bignum) precisely BECAUSE no write
    # path can persist one -- this chokepoint covers every create surface
    # (CLI, MCP cron_add, dashboard POST, apps SDK, and both
    # onboarding-import branches, which route here via add_job /
    # add_job_if_absent), so reader and writer stay exactly aligned and
    # the reader-side skip provably drops no writer-produced record.
    for _numeric_label, _numeric_arg in (("every_secs", every_secs), ("at_ts", at_ts)):
        if _numeric_arg is not None and not _is_representable_number(_numeric_arg):
            raise ValueError(f"{_numeric_label} must be a representable finite number")
    # Table-driven type+length gate for every persisted string field.
    # Runs at the persistence owner so EVERY create path (MCP, apps SDK,
    # dashboard, CLI) shares one check. name and message are required
    # (validated even when empty); all other fields use the falsy-skip
    # pattern (None/"" = "not set").
    _validate_cron_string_fields(
        {
            "name": name,
            "message": message,
            "channel": channel,
            "thread_ts": thread_ts,
            "agent_id": agent_id,
            "member_id": member_id,
            "created_by": created_by,
            "folder_id": folder_id,
            "chat_folder_id": chat_folder_id,
            "session_key": session_key,
            "model": model,
            "command": command,
            "script": script,
            "timezone": timezone,
        },
        required=frozenset({"name", "message"}),
    )
    if chat_folder_id and not persistent_session:
        raise ValueError(_CHAT_FOLDER_NEEDS_PERSISTENT)
    if timeout_secs and not 1 <= int(timeout_secs) <= 86400:
        raise ValueError(f"timeout_secs must be within 1..86400, got {timeout_secs}")
    if timeout_secs and (command or script):
        if timeout:
            _eff_sub = int(timeout)
        elif script:
            _eff_sub = 30
        else:
            _eff_sub = 300
        if int(timeout_secs) < _eff_sub + _SUBPROC_CLEANUP_ALLOWANCE_SECS:
            raise ValueError(
                "timeout_secs (wake budget) must cover the command/script "
                f"subprocess timeout plus cleanup: need >= "
                f"{_eff_sub + _SUBPROC_CLEANUP_ALLOWANCE_SECS}, got {timeout_secs}. "
                "A shorter wake budget cancels only the executor future — "
                "the subprocess keeps running while the next wake launches "
                "a duplicate."
            )
    if timezone and not is_valid_timezone(timezone):
        raise ValueError(f"Invalid timezone: {timezone!r}")
    skip_dates = skip_dates or []
    for _d in skip_dates:
        if not is_valid_skip_date(_d):
            raise ValueError(f"Invalid skip_date: {_d!r} (expected YYYY-MM-DD)")
    if cron_expr:
        if not validate_cron_expr(cron_expr):
            raise ValueError(f"Invalid cron expression: {cron_expr}")
        schedule = CronSchedule(kind="cron", cron_expr=cron_expr)
    elif every_secs:
        schedule = CronSchedule(kind="every", every_secs=max(every_secs, _MIN_INTERVAL_SECS))
    elif at_ts:
        schedule = CronSchedule(kind="at", at_ts=at_ts)
    else:
        raise ValueError("Must provide every_secs, at_ts, or cron_expr")

    return CronJob(
        id=uuid.uuid4().hex[:8],
        name=name,
        message=message,
        schedule=schedule,
        channel=channel,
        thread_ts=thread_ts,
        enabled=enabled,
        user_paused=not enabled,
        created_ts=time.time(),
        delete_after_run=delete_after_run,
        created_by=created_by,
        approval_mode=approval_mode,
        agent_id=agent_id,
        member_id=member_id,
        model=str(model or "").strip(),
        silent=silent,
        timezone=timezone,
        skip_dates=skip_dates,
        strict_schedule=strict_schedule,
        hide_in_chat=hide_in_chat,
        folder_id=folder_id,
        chat_folder_id=chat_folder_id,
        command=command,
        script=script,
        agent_sequence=list(agent_sequence) if agent_sequence else [],
        env=dict(env) if env else {},
        persistent_session=persistent_session,
        session_key=session_key,
        minimal_context=minimal_context,
        timeout=timeout,
        timeout_secs=int(timeout_secs) if timeout_secs else seams._JOB_TIMEOUT_SECS,
    )


def apply_job_update(
    job: CronJob, kwargs: dict[str, Any], chat_folder_out: dict[str, str] | None
) -> None:
    """Validate the update ``kwargs`` against ``job``, then apply it.

    Every check runs before the first assignment, so a rejected update raises
    ``ValueError`` with ``job`` untouched. ``chat_folder_out`` is the caller's
    transition sink (see :meth:`CronService._update_job_locked`): filled with the
    prior ``chat_folder_id`` only when this update actually changes it.
    """
    # Validate approval_mode if provided
    if "approval_mode" in kwargs:
        valid_approval_modes = ("", "auto")
        if kwargs["approval_mode"] not in valid_approval_modes:
            raise ValueError(f"Invalid approval_mode: {kwargs['approval_mode']!r}")
    # Validate before any mutations
    # Table-driven type+length gate for every updatable string
    # field. Falsy values are intentional no-ops (the assignment
    # section below skips them too).
    _validate_cron_string_fields(
        {f: kwargs[f] for f, _ in _CRON_STRING_FIELD_CAPS if f in kwargs},
    )
    # Cross-field: only a persistent job has a job-wide tab to file,
    # so a request that itself names a folder for a job this update
    # leaves stateless is refused. Turning persistence off on a filed
    # job is NOT refused: MCP/CLI ``cron_update`` exposes
    # ``persistent_session`` without ``chat_folder_id``, so a refusal
    # there would be a dead end; the assignment below clears the
    # folder instead and reports it through the transition sink.
    # Read the flag through the same coercion the assignment below
    # applies, so the guard judges the value that gets stored.
    if (kwargs.get("chat_folder_id") or "") and not bool(
        kwargs.get("persistent_session", job.persistent_session)
    ):
        raise ValueError(_CHAT_FOLDER_NEEDS_PERSISTENT)
    if (
        "cron_expr" in kwargs
        and kwargs["cron_expr"]
        and "every_secs" in kwargs
        and kwargs["every_secs"]
    ):
        raise ValueError("Cannot specify both cron_expr and every_secs")
    if "cron_expr" in kwargs and kwargs["cron_expr"]:
        if not validate_cron_expr(kwargs["cron_expr"]):
            raise ValueError(f"Invalid cron expression: {kwargs['cron_expr']}")
    if "every_secs" in kwargs and kwargs["every_secs"]:
        try:
            # OverflowError: int(float("inf")) -- not a ValueError,
            # so it must be caught here or it escapes the update.
            val = int(kwargs["every_secs"])
        except (ValueError, TypeError, OverflowError) as e:
            raise ValueError(f"Invalid interval: {kwargs['every_secs']}") from e
        if not _is_representable_number(val):
            # int() accepts a bignum that no consumer can hold
            # (int-float arithmetic raises OverflowError on the
            # timer-arming path) -- same writer mirror as
            # _build_job, one predicate.
            raise ValueError(f"Invalid interval: {kwargs['every_secs']}")
        if val < _MIN_INTERVAL_SECS:
            raise ValueError(f"Interval must be >= {_MIN_INTERVAL_SECS}s, got {val}")
    # Calendar-validity of timezone / skip_dates, validated at the
    # persistence owner so EVERY caller (MCP cron_add/cron_update,
    # dashboard, CLI) is covered by one check rather than each
    # write path re-implementing it. The schema regex only checks
    # the YYYY-MM-DD shape, not that the date exists -- so
    # a February 30 skip date would otherwise persist silently
    # and the skip would never match at fire time.
    if "timezone" in kwargs and kwargs["timezone"]:
        if not is_valid_timezone(kwargs["timezone"]):
            raise ValueError(f"Invalid timezone: {kwargs['timezone']!r}")
    if "skip_dates" in kwargs and kwargs["skip_dates"]:
        for _d in kwargs["skip_dates"]:
            if not is_valid_skip_date(_d):
                raise ValueError(f"Invalid skip_date: {_d!r} (expected YYYY-MM-DD)")
    # Per-wake budget and subprocess timeout: validated HERE, in
    # the pre-mutation section with every other check, so a
    # rejected update cannot leave earlier field mutations (name,
    # message, ...) stranded on the in-memory job for a later
    # save to persist. Assignments happen below with the rest.
    _tsecs: int | None = None
    if "timeout_secs" in kwargs and kwargs["timeout_secs"] is not None:
        try:
            _tsecs = int(kwargs["timeout_secs"])
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid timeout_secs: {kwargs['timeout_secs']!r}") from e
        if not 1 <= _tsecs <= 86400:
            raise ValueError(f"timeout_secs must be within 1..86400, got {_tsecs}")
    # Script/command subprocess timeout. MCP cron_update passes this
    # field, so a branch has to consume it here — otherwise the
    # update is accepted and silently dropped.
    _tsub: int | None = None
    if "timeout" in kwargs and kwargs["timeout"] is not None:
        try:
            _tsub = int(kwargs["timeout"])
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid timeout: {kwargs['timeout']!r}") from e
        if not 0 <= _tsub <= 86400:
            raise ValueError(f"timeout must be within 0..86400, got {_tsub}")
    # Vault secret grant: validated with the other pre-mutation
    # checks so a rejected grant cannot strand earlier field
    # mutations. An empty dict revokes (clears the pin too); a
    # non-empty grant requires a script job and the code
    # pin computed by the grant endpoint. This kwarg is reachable
    # only from operator surfaces — mcp_cron never passes it.
    if "secret_env" in kwargs and kwargs["secret_env"] is not None:
        _se = kwargs["secret_env"]
        if not isinstance(_se, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in _se.items()
        ):
            raise ValueError("secret_env must be a str->str mapping")
        if _se:
            cron_script.validate_secret_env_grant(_se)
            if not job.script:
                raise ValueError(
                    "secret_env grants apply only to SCRIPT jobs. "
                    "An agent job's session would expose the plaintext "
                    "to the model; a command job's pin can cover only "
                    "the command TEXT — a command invoking an "
                    "agent-writable helper file would run changed "
                    "bytes under a still-valid pin."
                )
            if not kwargs.get("secret_env_pin"):
                raise ValueError("a non-empty secret_env requires secret_env_pin")
    # Pending grant REQUEST (agent-reachable via the MCP
    # cron_secret_request tool). Same validation as the active
    # grant — a request the operator could never approve is
    # refused at write time, not at approval time. Writing this
    # field never touches the active pair.
    if "secret_env_pending" in kwargs and kwargs["secret_env_pending"] is not None:
        _sp = kwargs["secret_env_pending"]
        if not isinstance(_sp, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in _sp.items()
        ):
            raise ValueError("secret_env_pending must be a str->str mapping")
        if _sp:
            cron_script.validate_secret_env_grant(_sp)
            if not job.script:
                raise ValueError("secret grants apply only to script jobs")
            if not kwargs.get("secret_env_pending_pin"):
                raise ValueError(
                    "a non-empty secret_env_pending requires " "secret_env_pending_pin"
                )
    # Cross-field: the wake budget must cover the subprocess bound
    # plus cleanup, evaluated on the POST-update effective values —
    # the wake deadline cancels only the executor future, so a
    # shorter budget leaves the subprocess running while later
    # wakes launch duplicates.
    if job.command or job.script:
        _eff_secs = _tsecs if _tsecs is not None else job.timeout_secs
        _eff_sub_new = _tsub if _tsub is not None else job.timeout
        if _eff_sub_new:
            _eff_sub = int(_eff_sub_new)
        elif job.script:
            _eff_sub = 30
        else:
            _eff_sub = 300
        if (_tsecs is not None or _tsub is not None) and _eff_secs < (
            _eff_sub + _SUBPROC_CLEANUP_ALLOWANCE_SECS
        ):
            raise ValueError(
                "timeout_secs (wake budget) must cover the "
                "command/script subprocess timeout plus cleanup: "
                f"need >= {_eff_sub + _SUBPROC_CLEANUP_ALLOWANCE_SECS}, "
                f"got {_eff_secs}"
            )
    # Resolved here, before the first assignment, because a member schedule
    # re-resolves its member's template and that can refuse: the job must be
    # left untouched when it does.
    recaptured = None
    if "agent_id" in kwargs:
        from kiro_crew.cron_service.identity import recapture_cron_template

        recaptured = recapture_cron_template(job, kwargs["agent_id"] or "")
    if "name" in kwargs and kwargs["name"]:
        job.name = kwargs["name"]
    if "message" in kwargs and kwargs["message"]:
        job.message = kwargs["message"]
    if "agent_id" in kwargs:
        job.agent_id = kwargs["agent_id"] or ""
        if recaptured is not None:
            job.execution_context = recaptured
    if "channel" in kwargs:
        job.channel = kwargs["channel"] or None
    if "thread_ts" in kwargs:
        # Paired with ``channel``: together they decide WHERE a run's
        # output lands, and ``add_job`` has always accepted both. With
        # no branch here the field was validated (see the caps table)
        # and then dropped, so the caller was told "Updated" while the
        # cron kept replying in the old thread. Falsy clears, mirroring
        # ``channel`` and how mcp_cron normalizes blank to None.
        #
        # A granted script job re-threaded this way fails its NEXT run
        # closed: thread_ts is bound into the grant's delivery
        # fingerprint (cron_script.delivery_fingerprint), so the pin
        # stops verifying until the operator re-approves. That is the
        # intended fail-closed path, not a regression.
        job.thread_ts = kwargs["thread_ts"] or None
    if "approval_mode" in kwargs:
        job.approval_mode = kwargs["approval_mode"] or ""
    if "silent" in kwargs:
        job.silent = bool(kwargs["silent"])
    if "skip_dates" in kwargs:
        job.skip_dates = kwargs["skip_dates"] or []
    if "timezone" in kwargs:
        job.timezone = kwargs["timezone"] or ""
    if "strict_schedule" in kwargs:
        job.strict_schedule = bool(kwargs["strict_schedule"])
    if "persistent_session" in kwargs:
        job.persistent_session = bool(kwargs["persistent_session"])
    if "minimal_context" in kwargs:
        job.minimal_context = bool(kwargs["minimal_context"])
    if "hide_in_chat" in kwargs:
        job.hide_in_chat = bool(kwargs["hide_in_chat"])
    if "folder_id" in kwargs:
        job.folder_id = kwargs["folder_id"] or ""
    if "chat_folder_id" in kwargs:
        # Reported only on a REAL change: the caller moves a chat tab on
        # the strength of this, and the dashboard form submits the field
        # on every save, so "unchanged" must not read as "cleared".
        #
        # An empty prior value IS reported: filing a job that was unfiled
        # is the transition that puts an existing tab into its folder,
        # and the save is the one moment where that placement is asked
        # for explicitly (the mint files only a tab that did not exist).
        _chat_folder_next = kwargs["chat_folder_id"] or ""
        if chat_folder_out is not None and job.chat_folder_id != _chat_folder_next:
            chat_folder_out["chat_folder_was"] = job.chat_folder_id
        job.chat_folder_id = _chat_folder_next
    elif "persistent_session" in kwargs and not job.persistent_session and job.chat_folder_id:
        # Un-persisting takes the job-wide tab away, so the folder goes
        # with it: cleared here and reported exactly as an explicit
        # clear is, so the caller moves the tab the same way.
        if chat_folder_out is not None:
            chat_folder_out["chat_folder_was"] = job.chat_folder_id
        job.chat_folder_id = ""
    if "model" in kwargs:
        job.model = str(kwargs["model"] or "").strip()
    if "secret_env" in kwargs and kwargs["secret_env"] is not None:
        job.secret_env = dict(kwargs["secret_env"])
        # Pin travels with the grant; a revoke (empty map) clears it.
        job.secret_env_pin = str(kwargs.get("secret_env_pin") or "") if job.secret_env else ""
    if "secret_env_pending" in kwargs and kwargs["secret_env_pending"] is not None:
        job.secret_env_pending = dict(kwargs["secret_env_pending"])
        if job.secret_env_pending:
            job.secret_env_pending_pin = str(kwargs.get("secret_env_pending_pin") or "")
            job.secret_env_pending_ts = float(kwargs.get("secret_env_pending_ts") or 0.0)
        else:
            # Withdraw/deny clears the whole request record.
            job.secret_env_pending_pin = ""
            job.secret_env_pending_ts = 0.0
    # Per-wake budget (the asyncio.wait_for deadline in
    # _execute_with_timeout). Distinct from ``timeout``, which
    # bounds only script/command subprocesses. This is the only
    # writer that changes the field after creation: with no branch
    # here an existing job is stuck on its creation-time value, and
    # raising its budget means editing the store under _file_lock
    # by hand.
    if _tsecs is not None:
        job.timeout_secs = _tsecs
    if _tsub is not None:
        job.timeout = _tsub

    # Schedule changes (already validated above)
    if "cron_expr" in kwargs and kwargs["cron_expr"]:
        job.schedule = CronSchedule(kind="cron", cron_expr=kwargs["cron_expr"])
    elif "every_secs" in kwargs and kwargs["every_secs"]:
        job.schedule = CronSchedule(kind="every", every_secs=int(kwargs["every_secs"]))
