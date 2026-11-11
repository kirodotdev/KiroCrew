"""The scheduled job: what one cron job is, as the store holds it.

:class:`CronJob` is the record every other cron owner reads and writes -- the
serializer in :mod:`kiro_crew.cron_service.store` maps it to and from the
``crons.json`` entry, the schedule evaluator asks it when it fires, and the
service's run lifecycle stamps a run's outcome on it. The execution-owned pause
bookkeeping (:meth:`CronJob.record_failure` / :meth:`CronJob.record_success`)
lives with the record because both halves of the pause -- the durable reason and
the in-memory ``enabled`` it is folded into -- are fields of it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.cron_service.schedule import _job_tz

# The cron subsystem logs on one channel whichever owner emits the record, so an
# operator's logger filter and the tests' caplog targets keep matching.
logger = logging.getLogger("kiro_crew.cron")

_JOB_TIMEOUT_SECS = 1800  # 30 min per job
_AUTO_PAUSE_THRESHOLD = 5  # default consecutive failures before a cron auto-pauses
# Upper bound on a per-job ``auto_pause_after_failures``. 0 (never pause) is the
# way to ask for "unlimited"; the cap only keeps a typo from reading as intent.
_AUTO_PAUSE_MAX = 10000
# A job that has not paused by the default limit, because its limit is 0 or
# above the default, is not left firing at full rate while it keeps failing.
# From the failure that would have paused it at the default limit, each further
# run waits at least this long after the previous run, doubling per failure up
# to the cap, so such a job runs at most about once an hour instead of on its
# own schedule. A success resets the counter and with it the backoff.
_NEVER_PAUSE_BACKOFF_BASE_SECS = 300
_NEVER_PAUSE_BACKOFF_MAX_SECS = 3600
# Default consecutive failures before a failure alert leaves the dashboard bell
# for the job's channel or the owner's DM: 1 alerts on the first failure.
_ALERT_AFTER_DEFAULT = 1


def parked_by_its_run(
    kind: object, user_paused: object, user_pause_requested: object, last_run_ts: object
) -> bool:
    """Whether a paused job is a one-shot its own run parked, not a pause the user made.

    A fired ``at`` job that keeps its row is stored as ``enabled`` False and
    ``user_paused`` True, the same flags a user pause writes
    (:func:`~kiro_crew.cron_service.execution.run_parks_one_shot`). A user pause
    also sets ``user_pause_requested``; a park does not. A paused one-shot without
    it that has run (``last_run_ts``) was parked by that run, so health readers
    count it and a failed one-shot is not hidden as a user pause.

    shortcut: a pause saved before ``user_pause_requested`` existed carries no
    marker, so an ``at`` job paused that way after a run reads as parked until the
    user pauses or resumes it again. Drop this note once such stores are gone.
    """
    if kind != "at" or user_paused is not True or user_pause_requested is True:
        return False
    return not isinstance(last_run_ts, bool) and isinstance(last_run_ts, (int, float))


def _lifts_default_pause(limit: object) -> bool:
    """Whether *limit* lets a job keep failing past the default limit (0 or above it)."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        return False
    return limit == 0 or limit > _AUTO_PAUSE_THRESHOLD


def validate_auto_pause_after_failures(value: Any) -> int:
    """Return *value* as a per-job auto-pause limit, or raise ``ValueError``.

    ``0`` means the job never auto-pauses; ``1.._AUTO_PAUSE_MAX`` pauses it after
    that many consecutive failed runs. ``bool`` is refused even though it is an
    ``int`` subclass, so ``true`` cannot silently mean "pause after one failure".
    """
    if isinstance(value, bool):
        raise ValueError(f"auto_pause_after_failures must be an integer, got {value!r}")
    try:
        limit = int(value)
    except (TypeError, ValueError, OverflowError) as e:
        raise ValueError(f"auto_pause_after_failures must be an integer, got {value!r}") from e
    if isinstance(value, float) and value != limit:
        raise ValueError(f"auto_pause_after_failures must be an integer, got {value!r}")
    if not 0 <= limit <= _AUTO_PAUSE_MAX:
        raise ValueError(
            f"auto_pause_after_failures must be within 0..{_AUTO_PAUSE_MAX} "
            f"(0 = never auto-pause), got {limit}"
        )
    return limit


def validate_alert_after_failures(value: Any) -> int:
    """Return *value* as a per-job alert threshold, or raise ``ValueError``.

    ``1.._AUTO_PAUSE_MAX`` sends the failure message only once that many runs
    have failed in a row. ``0`` is refused: "never alert" is what ``silent``
    means. ``bool`` is refused like :func:`validate_auto_pause_after_failures`.
    """
    if isinstance(value, bool):
        raise ValueError(f"alert_after_failures must be an integer, got {value!r}")
    try:
        limit = int(value)
    except (TypeError, ValueError, OverflowError) as e:
        raise ValueError(f"alert_after_failures must be an integer, got {value!r}") from e
    if isinstance(value, float) and value != limit:
        raise ValueError(f"alert_after_failures must be an integer, got {value!r}")
    if not 1 <= limit <= _AUTO_PAUSE_MAX:
        raise ValueError(
            f"alert_after_failures must be within 1..{_AUTO_PAUSE_MAX} "
            f"(1 = alert on the first failure), got {limit}"
        )
    return limit


@dataclass
class CronSchedule:
    """Schedule definition — ``every``, ``at``, or ``cron``."""

    kind: str  # "every" | "at" | "cron"
    every_secs: int | None = None
    at_ts: float | None = None
    cron_expr: str | None = None  # "min hour dom month dow"


@dataclass
class CronJob:
    """A scheduled job."""

    id: str
    name: str
    message: str
    schedule: CronSchedule = field(default_factory=lambda: CronSchedule(kind="every"))
    channel: str | None = None
    thread_ts: str | None = None
    enabled: bool = True
    user_paused: bool = False  # True when explicitly paused by user; never mutated by execution
    # Monotonic per-job order for explicit pause/resume requests, including a
    # repeated request whose booleans already have that value. A pending run
    # record uses it to keep a newer user choice while merging runtime fields.
    user_pause_generation: int = 0
    # True while ``user_paused`` is the person's own pause request (a pause, a job
    # created paused, an import held for review); a resume clears it. A run's park
    # of a fired one-shot writes ``user_paused`` but never this, which is how the
    # health readers tell the two apart (:func:`parked_by_its_run`).
    user_pause_requested: bool = False
    auto_paused: bool = (
        False  # True when paused by execution after repeated failures; cleared on re-enable/success
    )
    last_run_ts: float | None = None
    last_status: str | None = None  # "ok" | "error"
    last_error: str | None = None
    created_ts: float = 0.0
    delete_after_run: bool = False
    # Runtime-only (never serialized): set by the gateway when THIS run was
    # refused by the fire-time governance gate. A denied run is a policy
    # state, not a completed run: a one-shot delete_after_run job is RETAINED
    # instead of deleted, and a denied "at" job is parked DISABLED (a past-due
    # at-job left enabled would be due again on every timer tick — a
    # zero-delay refire loop) so an operator can re-enable it after a policy
    # loosening. Recurring jobs need neither: they wait for their next slot.
    # Reset at the start of every run.
    fire_time_denied: bool = False
    # Runtime-only (never serialized): set by the gateway when THIS run never
    # started because every pool worker was busy for the whole queue budget.
    # Deliberately NOT fire_time_denied, even though both must retain a one-shot:
    # that flag ALSO forces an "at" job disabled and is documented as a *policy*
    # refusal, so reusing it would park a starved job needing an operator to
    # re-enable it and would mislabel pool saturation as a governance denial in
    # history. Starvation clears on its own, so this field is retention-only --
    # read solely where a one-shot would otherwise be consumed by a run it never
    # had. Reset at the start of every run.
    run_never_started: bool = False
    # Runtime-only (never serialized): set by the gateway, with
    # ``run_never_started``, when THIS run was refused for a reason in the job's
    # own configuration that a retry cannot clear (an empty ``agent_sequence``
    # step). A plain one-shot refused this way is parked as a one-shot that ran
    # is, instead of retrying on every floor for as long as the gateway runs; a
    # ``delete_after_run`` one-shot is still retained by ``run_never_started``.
    # Reset at the start of every run.
    refusal_cannot_clear: bool = False
    # Runtime-only (never serialized): set by the gateway when THIS run did
    # nothing a person would look for in the run history (a gateway job's quiet
    # tick, which woke no one), so the run writes no history row. Reset at the
    # start of every run.
    quiet_run: bool = False
    # Runtime-only (never serialized): set by the gateway when THIS run was a
    # script's Skip(), so its history row is marked idle and the Logs tab can
    # hide it. Reset at the start of every run.
    idle_run: bool = False
    last_result: str | None = None
    # Epoch at which ``last_result`` was produced, written by
    # :meth:`set_run_result` and PERSISTED. Carries the run's identity for
    # history attribution.
    last_result_ts: float = 0.0
    # The run stamp as ALREADY RENDERED text, written once by
    # :meth:`set_run_result` and persisted. The header is the row's dedup key
    # (``ConversationLog.append_if_absent`` matches ``(role, content)``), and
    # rendering reads the editable ``timezone``, so re-rendering later could
    # mint a second copy of a row already on disk. Rendering once keeps every
    # injection byte-identical. Display only: row identity is
    # ``cron_inject.run_marker``. ``""`` (legacy) renders the pre-stamp header.
    last_result_stamp: str = ""
    # Runtime-only (never serialized): True once THIS run produced a result
    # via set_run_result(). For AGENT jobs ``last_result`` carries over across
    # result-less runs (prompt dedup), so the history recorder needs this
    # marker, not the value, to attribute it; comparing strings cannot tell a
    # repeated result from none. Command and script jobs clear ``last_result``
    # on every result-less exit. Reset at each run start by _run_job_isolated.
    result_produced: bool = False
    # Runtime-only, reset by _execute_with_timeout at the start of every run:
    # True once THIS run's failure has been counted via record_failure(). The
    # timeout handler consults it so a run that already recorded its failure
    # (e.g. a delivery-path exception) and then overran its deadline during
    # cleanup is counted once, not twice.
    failure_recorded: bool = False
    # Runtime-only: True when THIS run's counted failure was an auth expiry
    # (``record_failure(auth_expiry=True)``), so the store-side pause decision
    # (``execution.pause_at_current_limit``) skips the pause as well.
    failure_auth_expiry: bool = False
    # Runtime-only: ``auto_paused`` as it stood when THIS run started, stamped by
    # _run_job_isolated and carried on the run's record. A store whose
    # ``auto_paused`` differs from it was changed by someone else while the
    # run was in flight (a ``cron resume``), so the run's merge and the reaper
    # leave the store's pause state and failure count as they find them. None
    # for a record no run stamped: the run's pause state is applied as is.
    auto_paused_at_start: bool | None = None
    # Runtime-only, set only on a scheduled cancel or reap's frozen settlement
    # record (``cron_service/settlement.py``). ``settlement_failure_counted`` is
    # True when the record counted this run's failure, so the job it lands on
    # decides the auto-pause with its own stored limit.
    # ``settlement_pause_audited`` is True once a save landed that pause and its
    # ``cron_auto_pause`` row was written, so a later save writes no second row.
    settlement_failure_counted: bool = False
    settlement_pause_audited: bool = False
    context_enabled: bool = False
    agent_id: str = ""
    # Member identity is distinct from the provider template in agent_id.
    # These fields survive reload; omitted legacy records remain on V1.
    member_id: str = ""
    memory_store: str = ""
    execution_context: dict[str, Any] | None = None
    approval_mode: str = ""  # "" (default/hook-based) | "auto" (auto-approve all tools)
    acked_items: list[str] = field(default_factory=list)
    # One id per ``acked_items`` entry, aligned by position ("" for an entry
    # added without one), so an unack can take back exactly the entry its ack
    # added even when an equal summary was acknowledged again later.
    acked_item_ids: list[str] = field(default_factory=list)
    # Digest of the ``acked_items`` the ids above were written against. A
    # writer that changed the items without the ids (an older build) leaves a
    # digest that does not match them, so the ids then name no entry.
    acked_item_ids_digest: str = ""
    created_by: str = ""  # Slack user ID of the creator (for DM fallback)
    # Provenance of a job seeded from a Schedule-page template, written once
    # by the dashboard create path only (other surfaces leave both ""):
    #
    #   source_preset          -- the template preset id (e.g. "error-digest").
    #   source_template_prompt -- the template's prompt text at save time.
    #
    # The snapshot lets the Schedule page tell a TEMPLATE that moved
    # (snapshot != live preset prompt, shows a "template updated" hint) from a
    # USER who edited their copy (message != snapshot, shows nothing).
    # "" for both means unknown; such a job never shows the hint.
    source_preset: str = ""
    source_template_prompt: str = ""
    # The key an external installer owns this job under
    # (``kirocrew cron add --managed-by <key>``), so it can find the job without
    # its random ``id``. Unique: :meth:`CronService.add_managed_job`, the only
    # writer, refuses a duplicate. Create-only, so a Schedule-page edit never
    # detaches a job from its installer. ``""`` = not installer-managed.
    managed_by: str = ""
    # The digest of the ``app.json`` cron fields the app bridge last applied to
    # this job (``kiro_crew.apps.bridges.app_cron_manifest_digest``). Stamped
    # when the bridge creates an app's job and rewritten each time it re-applies
    # a changed manifest entry; ``""`` is "no manifest applied yet" -- a person's
    # job, or an app job from before the field existed -- which the bridge
    # treats as changed once. Only the bridge writes it: no dashboard, MCP or
    # CLI update forwards the key, so a hand edit cannot forge "already applied"
    # and make the next manifest change skip the job.
    app_manifest_digest: str = ""
    silent: bool = False  # suppress auto-delivery; agent sends via send_message
    session_key: str = ""  # session that created this job (for scoped removal)
    person_owner_key: str = ""  # session_key MCP cron_add saw as a live person's tab
    last_posted_hash: str = ""  # hash of last result posted to Slack (dedup)
    consecutive_dupes: int = 0  # count of suppressed duplicate results
    last_posted_at: float = 0.0  # epoch when last Slack post was delivered (dedup reminder)
    last_failure_hash: str = ""  # hash of last failure notification (dedup crashes)
    last_failure_at: float = 0.0  # epoch of last failure Slack alert (dedup reminder)
    consecutive_failures: int = 0  # consecutive failed runs (any error); drives auto-pause
    # How many consecutive failed runs auto-pause this job. 0 = never auto-pause
    # (the job keeps running however often it fails, on the failure backoff of
    # :meth:`failure_backoff_until` from the default threshold on). Read it through
    # :meth:`auto_pause_limit`, never directly, so every threshold comparison
    # shares one meaning of 0.
    auto_pause_after_failures: int = _AUTO_PAUSE_THRESHOLD
    # Consecutive failed runs before the failure message leaves the dashboard
    # bell for the channel or DM. Read it through :meth:`alert_after_limit`.
    alert_after_failures: int = _ALERT_AFTER_DEFAULT
    skip_dates: list[str] = field(default_factory=list)  # ISO dates to skip ["YYYY-MM-DD"]
    timezone: str = ""  # IANA timezone for skip evaluation
    persistent_session: bool = True  # False → fresh ephemeral session per run
    minimal_context: bool = False  # True → skip memory/lessons/skills/history
    hide_in_chat: bool = (
        False  # True → don't create a dashboard chat slot; result still goes to history + Slack/bell
    )
    # True → a STATELESS agent job's run opens its own tab, ``cron-<id>-<run>``,
    # bound to that run's ``cron:<id>:<run>`` session, so the reader replies to
    # one run alone. The store refuses it with ``persistent_session=True``, whose
    # one tab already holds every run. ``hide_in_chat`` still wins.
    new_chat_per_run: bool = False
    # Cron folder grouping; "" = unfiled. CONTRACT for all consumers
    # (Schedule UI, calendar, CLI, MCP): an id that does not match a folder
    # in cron_folders.json MUST be treated as ungrouped — folder deletion
    # clears assignments only best-effort, so dangling ids are expected and
    # benign (they self-heal on the job's next folder move).
    folder_id: str = ""
    model: str = ""  # per-job model override (canonical key or provider id); "" = inherit
    # The CHAT sidebar folder (``folders.json``) the job's ``cron-{id}`` tab is
    # filed into; "" = not filed. Independent of ``folder_id``, which groups the
    # job's ROW on the Schedule page. PERSISTENT jobs only: the store refuses it
    # with ``persistent_session=False`` (no job-wide tab). Filing adds to
    # delivery, never replaces it. CONTRACT: an id with no matching folder MUST
    # be treated as "not filed"; the run still delivers and the skip is recorded
    # once (``cron_inject.chat_folder_for_minted_tab``).
    chat_folder_id: str = ""
    # Optional project directory an agent job's session runs in.
    # ``""`` (and a record written before the field existed) keeps
    # today's cwd: the bound agent's workspace, else the per-session scratch
    # directory. When set, it is the session's cwd for every fire, so the
    # repo's own ``.kiro/steering/`` and ``AGENTS.md`` load as they would for
    # an interactive session scoped there. Stored resolved (``realpath``);
    # validated on write and again at every fire by
    # ``fields.resolve_project_path``. Agent (message) jobs only.
    project_path: str = ""
    # Transient-retry telemetry for the LAST completed run. Both fields are
    # written in ONE place, `CronService._execute`, right after it stamps
    # `last_run_ts`: it reads the in-flight `_transient_attempts` counter the
    # gateway callback leaves on the live job object (a runtime attribute that
    # never reaches disk) and clears it. 0 means the last run needed no retry,
    # or this is a legacy record with neither key.
    last_retry_count: int = 0
    #: The ``last_run_ts`` the count above describes -- the same `time.time()`
    #: value, assigned in the same place. A cancelled run advances
    #: ``last_run_ts`` on its own path (the ``every`` scheduler needs it to, or
    #: the schedule drifts) and never reaches the stamp, so the two disagree and
    #: the Schedule page shows no count for it rather than the previous run's.
    last_retry_run_ts: float = 0.0
    #: Per-job run counter from ``RunClaims.next_generation``, written only by
    #: the terminal merges (``_merge_job_result``, ``_merge_terminal_state_locked``).
    #: Those merge after the claim is released, so a replacement run can merge
    #: first; the older record is then discarded instead of reverting its state.
    #: An integer, not a timestamp: Windows' ``time.time()`` ticks every ~15.6 ms,
    #: so two claims can tie. 0 = a job that never ran.
    run_generation: int = 0

    # A sequence of MORE THAN ONE agent takes precedence over agent_id: the
    # gateway runs those agents in order, each on its own session key. A
    # one-element sequence does NOT, and falls through to agent_id.
    agent_sequence: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)  # per-job environment variables
    timeout_secs: int = _JOB_TIMEOUT_SECS
    strict_schedule: bool = False  # when True, skip jitter and fire exactly on schedule
    script: str = ""  # Script file "<config_dir>/crons/x.py:func"; bypasses LLM dispatch
    command: str = ""  # Shell command for direct execution; bypasses LLM dispatch
    timeout: int = (
        0  # script/command timeout in seconds (0 = use default: 30s script, 300s command)
    )
    # Operator-approved vault secrets for SCRIPT jobs: env-var name -> vault
    # secret NAME (kiro_crew.secrets.SecretVault; no plaintext here). Set ONLY
    # when the owner approves a request on the Schedule page, so an agent cannot
    # grant itself vault access. secret_env_pin (cron_script.compute_secret_env_pin,
    # a keyed HMAC over script spec + message + body) binds the grant to the
    # approved code: crons/ scripts stay agent-writeable, so a body rewritten
    # after approval fails closed at fire time.
    secret_env: dict[str, str] = field(default_factory=dict)
    secret_env_pin: str = ""
    # Agent-REQUESTED grant awaiting operator approval. The MCP
    # ``cron_secret_request`` tool may write ONLY these fields — never the
    # active pair above — so the agent-first flow is "agent proposes, human
    # disposes": the dashboard approve endpoint re-verifies the pending pin
    # against the job's CURRENT code before promoting pending -> active, so an
    # approval never blesses code that changed after the request.
    secret_env_pending: dict[str, str] = field(default_factory=dict)
    secret_env_pending_pin: str = ""
    secret_env_pending_ts: float = 0.0
    # The pending pin of the last re-approval request the SCHEDULER filed for
    # a stale active grant (see cron_service.grant_resurface). It lets the
    # scheduler raise that request once per code revision: an operator who
    # denies it is not asked again on every refused run, while a further code
    # change mints a different pin and asks again. Agent-writable like every
    # stored field, which costs nothing: it only ever suppresses or permits a
    # REQUEST, and nothing is granted without the operator's approval.
    secret_env_resurfaced_pin: str = ""

    def set_run_result(self, value: str) -> None:
        """Record a result produced by the CURRENT run.

        Sole write path for executor callbacks: pairs the ``last_result``
        assignment with the runtime-only ``result_produced`` marker so the
        history recorder can attribute the value to this run. Direct
        ``last_result`` assignment stays reserved for store merge and
        deserialization paths, which restore prior state rather than
        produce a new result.
        """
        self.last_result = value
        self.result_produced = True
        # Stamped and RENDERED here rather than at injection time so all of a
        # run's injection sites emit one identical header -- see
        # ``last_result_stamp`` for why re-rendering duplicates rows.
        self.last_result_ts = time.time()
        self.last_result_stamp = self._render_run_stamp(self.last_result_ts)

    def _render_run_stamp(self, when_ts: float) -> str:
        """Render *when_ts* as the header suffix, in the job's own timezone.

        Display-only: a bad timezone or a bad epoch must never fail the run
        that produced the result, so any error degrades to the UNSTAMPED header
        -- the same spelling a legacy row carries, which keeps the dedup
        coherent -- rather than to a half-rendered third variant.
        """
        from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

        if not when_ts:
            return ""
        try:
            when = seams.datetime.fromtimestamp(when_ts, tz=_job_tz(self))
            return f" | {when:%Y-%m-%d %H:%M:%S %Z}"
        except Exception:
            logger.debug("Cron run stamp render failed for job %s", self.id, exc_info=True)
            return ""

    def clear_carried_result(self) -> None:
        """Drop a PREVIOUS run's result when this run produced none.

        Result-less command/script exits must not display the last run's
        output beside this run's status. Guarded on ``result_produced`` so a
        run that produced and delivered a result and then failed during
        cleanup keeps it. Assigns directly rather than via set_run_result()
        so a cleared field is never marked as produced by this run.
        """
        if not self.result_produced:
            self.last_result = ""

    def _audit_pause_change(self, outcome: str) -> None:
        """Emit a SEL audit event for an auto-pause permission transition.

        Auto-pausing revokes a job's ability to execute (and clearing it restores
        that ability), so the transition is a permission decision that must be
        auditable per the security-controls guideline. Best-effort — an audit
        write failure must never mask the failure/success bookkeeping that drives
        the pause itself; the tool-invocation error paths already log the run
        outcome separately."""
        from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

        try:
            seams.sel.sel().log_tool_invocation(
                session_key=f"cron:{self.id}",
                tool_name=self.script or self.command or "cron_job",
                tool_kind="cron_auto_pause",
                outcome=outcome,
                metadata={"job_id": self.id, "consecutive_failures": self.consecutive_failures},
            )
        except Exception:
            logger.debug("SEL logging failed in cron auto-pause transition", exc_info=True)

    def auto_pause_limit(self) -> int | None:
        """Consecutive failures that auto-pause this job, or ``None`` for never.

        A value no writer can produce (out of range, non-integer) falls back to
        the default rather than disabling the safety net.
        """
        limit = self.auto_pause_after_failures
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 0 <= limit <= _AUTO_PAUSE_MAX
        ):
            return _AUTO_PAUSE_THRESHOLD
        return limit or None

    def alert_after_limit(self) -> int:
        """Consecutive failures before a failure alert leaves the dashboard bell.

        A value no writer can produce falls back to the default, so a damaged
        record alerts on the first failure rather than never.
        """
        limit = self.alert_after_failures
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _AUTO_PAUSE_MAX
        ):
            return _ALERT_AFTER_DEFAULT
        return limit

    def failure_backoff_until(self) -> float | None:
        """Epoch before which a failing job must not fire, or ``None``.

        Every job still enabled once its streak reaches the default limit backs
        off, whatever limit it carries: 0 and any limit above the default alike,
        so raising the limit buys the same bounded retry rate as turning
        auto-pause off. A job at or below the default pauses before it gets
        here, unless the failures were auth expiries (:meth:`record_failure`),
        which never pause it: it is still enabled, so it backs off the same way.
        Below the default streak a job fires on schedule.

        The delay counts from ``last_run_ts``. For an ``every`` job that is the
        start of the last run, because ``close_run`` restores the start time;
        for a cron-expression job it is the last run's completion.
        """
        limit = self.auto_pause_limit()
        if limit is not None and limit <= _AUTO_PAUSE_THRESHOLD and not self.enabled:
            return None
        failures = self.consecutive_failures
        if isinstance(failures, bool) or not isinstance(failures, int):
            return None
        excess = failures - _AUTO_PAUSE_THRESHOLD
        last = self.last_run_ts
        if excess < 0 or not isinstance(last, (int, float)) or isinstance(last, bool) or not last:
            return None
        # min() on the exponent first, so a huge streak cannot build a huge int.
        delay = min(
            _NEVER_PAUSE_BACKOFF_BASE_SECS * 2 ** min(excess, 16),
            _NEVER_PAUSE_BACKOFF_MAX_SECS,
        )
        return float(last) + delay

    def audit_auto_pause_limit_change(self, previous: int | None, actor: str = "") -> None:
        """Emit a SEL audit event when a write moves the limit past the default.

        ``previous`` is the limit before the write, or ``None`` for a new job.
        ``actor`` is the session key of the caller that made the write: the
        creating session for an add, the ``cron_update`` caller for an update.
        It is the row's ``session_key`` so the SEL view filtered by session shows
        which session turned a pause off; with no known caller (the CLI, an
        internal write) the row falls back to ``cron:<id>``.
        A limit of 0 or above the default lets the job keep failing past the
        point where it would otherwise pause, so setting one, changing one, or
        returning to the default must be auditable. Best-effort, like
        :meth:`_audit_pause_change`: an audit failure never undoes the write it
        describes.
        """
        new = self.auto_pause_after_failures
        if new == previous or not (_lifts_default_pause(new) or _lifts_default_pause(previous)):
            return
        if new == 0:
            outcome = "never_pause_set"
        elif previous == 0:
            outcome = "never_pause_cleared"
        else:
            outcome = "limit_changed"
        from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

        try:
            seams.sel.sel().log_tool_invocation(
                session_key=actor or f"cron:{self.id}",
                tool_name=self.script or self.command or "cron_job",
                tool_kind="cron_auto_pause_limit",
                outcome=outcome,
                metadata={
                    "job_id": self.id,
                    "created_by": self.created_by,
                    "actor_session_key": actor,
                    "previous_limit": previous,
                    "auto_pause_after_failures": new,
                },
            )
        except Exception:
            logger.debug("SEL logging failed for a cron auto-pause limit change", exc_info=True)

    def record_failure(self, *, auth_expiry: bool = False) -> None:
        """Count one consecutive failure and auto-pause once the threshold is hit.

        Sets both `enabled` (the scheduler stops at once) and `auto_paused` (the
        durable reason, distinct from a user pause), in one place so the many
        failure branches record a pause the same way.

        A run that started auto-paused and finds the pause lifted under it (a
        resume in this process) counts nothing: the resume gave the job fresh
        attempts. ``failure_recorded`` is still set, so the timeout arm and the
        reaper treat the failure as handled.

        A failure the gateway classified as an auth expiry (``auth_expiry``: the
        harness needs a new sign-in, see ``_cron_auth_expiry``) counts like any
        other but never auto-pauses the job: it is environmental and clears once
        the user signs in again. Its streak still drives the alert and, past the
        default threshold, the backoff in :meth:`failure_backoff_until`, so a job
        that stays signed out keeps a bounded retry rate. A later failure of
        another kind pauses the job as usual once the streak is at its limit.
        """
        counted = self.count_failure()
        self.failure_auth_expiry = auth_expiry
        if counted and not auth_expiry and self.failure_limit_reached():
            self.enabled = False
            self.auto_paused = True
            self._audit_pause_change("auto_paused")

    def count_failure(self) -> bool:
        """The counting half of :meth:`record_failure`: True when the failure counted.

        A scheduled cancel or reap counts on its frozen record and leaves the pause
        to the job the store hands back, whose limit may have been saved during the
        run (``cron_service/settlement.py``).
        """
        self.failure_recorded = True
        self.failure_auth_expiry = False
        if self.auto_paused_at_start and not self.auto_paused:
            return False
        self.consecutive_failures += 1
        return True

    def failure_limit_reached(self) -> bool:
        """Whether a job not yet auto-paused has reached its consecutive-failure limit."""
        limit = self.auto_pause_limit()
        return limit is not None and self.consecutive_failures >= limit and not self.auto_paused

    def record_success(self) -> None:
        """Reset the failure counter and lift any execution auto-pause.

        Clearing an auto-pause also re-enables the job: :func:`_job_enabled`
        rebuilds ``enabled`` on load as ``not user_paused and not auto_paused``,
        so leaving it False would pause the job in memory only, until a restart
        silently resumed it. A user pause stays: ``user_paused`` is never changed
        by execution; lifting it is ``enable_job``'s job.

        This is the ONE place that resets the failure-alert dedup fields: every
        success path routes here, so a relapse after recovery alerts fresh instead
        of being suppressed for up to ``_FAILURE_REMINDER_SECS``.

        Drops ``auto_paused_at_start``: a pause this run lifts itself is not an
        outside resume, so a later failure of the same run still counts.
        """
        self.auto_paused_at_start = None
        self.consecutive_failures = 0
        self.last_failure_hash = ""
        self.last_failure_at = 0.0
        if self.auto_paused:
            self.auto_paused = False
            if not self.user_paused:
                self.enabled = True
            self._audit_pause_change("auto_pause_cleared")
