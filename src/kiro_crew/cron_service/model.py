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
_AUTO_PAUSE_THRESHOLD = 5  # consecutive failures before a script/command cron auto-pauses


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
    last_result: str | None = None
    # Epoch at which ``last_result`` was produced, written by
    # :meth:`set_run_result` and PERSISTED. Carries the run's identity for
    # history attribution.
    last_result_ts: float = 0.0
    # The run stamp as ALREADY RENDERED text, written once by
    # :meth:`set_run_result` and PERSISTED. This is what the dashboard header
    # displays, and it is a snapshot on purpose.
    #
    # The header is also the row's dedup key: ``ConversationLog.append_if_absent``
    # judges "already persisted" by ``(role, content)``, so any injection site
    # that RE-RENDERED the stamp would have to reproduce it byte for byte
    # forever. Rendering reads the job's ``timezone``, which a user can edit
    # after the run, so a re-render would silently mint a second, differently
    # spelled copy of a row already on disk -- a duplicated run in the tab and
    # in the replay a follow-up turn reads. Rendering once, here, is what makes
    # every later injection (the three executor delivery paths and a later
    # ``/to-chat`` re-surfacing) byte-identical no matter what the job's
    # configuration has become since.
    #
    # DISPLAY ONLY. Row identity is ``cron_inject.run_marker``, which carries
    # ``last_result_ts`` at full precision, so this stamp's resolution does not
    # decide whether two runs collapse -- it is rendered for a person to read.
    #
    # ``""`` (a legacy job, or a store written by an older build) means
    # "unknown" and renders the pre-stamp header unchanged, so rows already on
    # disk keep deduping against their historical spelling.
    last_result_stamp: str = ""
    # Runtime-only (never serialized): True once THIS run produced a result
    # via set_run_result(). For AGENT jobs ``last_result`` is a cross-run
    # context-carry field that result-less runs deliberately leave in place
    # for the next run's prompt dedup, so the history recorder needs this
    # marker — not the value — to decide attribution. Command and script
    # jobs instead clear it on every result-less exit: the prompt built for
    # them is discarded (the command branch never reads it, the script
    # branch reassigns the variable), so a carried-over value could only
    # ever misreport a finished run's result. Identity/equality checks on
    # the string cannot do that job: CPython interns equal literals and caches
    # single-character latin-1 strings, so a run re-producing the same text
    # is indistinguishable from a run that produced nothing. Reset at the
    # start of every run by _run_job_isolated.
    result_produced: bool = False
    # Runtime-only, reset by _execute_with_timeout at the start of every run:
    # True once THIS run's failure has been counted via record_failure(). The
    # timeout handler consults it so a run that already recorded its failure
    # (e.g. a delivery-path exception) and then overran its deadline during
    # cleanup is counted once, not twice.
    failure_recorded: bool = False
    context_enabled: bool = False
    agent_id: str = ""
    # Member identity is distinct from the provider template in agent_id.
    # These fields survive reload; omitted legacy records remain on V1.
    member_id: str = ""
    memory_store: str = ""
    execution_context: dict[str, Any] | None = None
    approval_mode: str = ""  # "" (default/hook-based) | "auto" (auto-approve all tools)
    acked_items: list[str] = field(default_factory=list)
    created_by: str = ""  # Slack user ID of the creator (for DM fallback)
    # Provenance of a job seeded from a Schedule-page template. Curated
    # template prompts are COPIED into the job at save time (the user owns and
    # edits their prompt), so a later fix to a template is unreachable for jobs
    # already saved. Two create-only fields, written together by the dashboard
    # create path ONLY (MCP / CLI / apps SDK / onboarding import never involve a
    # template and leave both ""):
    #
    #   source_preset          -- the template preset id (e.g. "error-digest").
    #   source_template_prompt -- the template's prompt text AS IT WAS at save
    #                             time (a snapshot, written once, never updated).
    #
    # The snapshot is what makes "the template changed" an ATTRIBUTABLE claim.
    # Comparing the job's live message against the template's CURRENT prompt is
    # symmetric: it cannot tell a template that moved from a user who edited
    # their own copy. The snapshot fixes one operand at save time, so the
    # Schedule page can ask the two questions separately -- did the TEMPLATE
    # move (snapshot != live preset prompt), which is the only thing that shows
    # a "template updated" hint, versus did the USER edit their copy
    # (message != snapshot), which shows nothing. It is a text snapshot, not a
    # maintained revision integer, so it cannot drift out of date.
    #
    # "" for both means "unknown" -- a blank/non-dashboard create, or a job
    # saved before these fields existed (_job_from_record defaults both to "").
    # Such a job simply never shows the hint.
    source_preset: str = ""
    source_template_prompt: str = ""
    silent: bool = False  # suppress auto-delivery; agent sends via send_message
    session_key: str = ""  # session that created this job (for scoped removal)
    last_posted_hash: str = ""  # hash of last result posted to Slack (dedup)
    consecutive_dupes: int = 0  # count of suppressed duplicate results
    last_posted_at: float = 0.0  # epoch when last Slack post was delivered (dedup reminder)
    last_failure_hash: str = ""  # hash of last failure notification (dedup crashes)
    last_failure_at: float = 0.0  # epoch of last failure Slack alert (dedup reminder)
    consecutive_failures: int = 0  # consecutive failed runs (any error); drives auto-pause
    skip_dates: list[str] = field(default_factory=list)  # ISO dates to skip ["YYYY-MM-DD"]
    timezone: str = ""  # IANA timezone for skip evaluation
    persistent_session: bool = True  # False → fresh ephemeral session per run
    minimal_context: bool = False  # True → skip memory/lessons/skills/history
    hide_in_chat: bool = (
        False  # True → don't create a dashboard chat slot; result still goes to history + Slack/bell
    )
    # Cron folder grouping; "" = unfiled. CONTRACT for all consumers
    # (Schedule UI, calendar, CLI, MCP): an id that does not match a folder
    # in cron_folders.json MUST be treated as ungrouped — folder deletion
    # clears assignments only best-effort, so dangling ids are expected and
    # benign (they self-heal on the job's next folder move).
    folder_id: str = ""
    model: str = ""  # per-job model override (canonical key or provider id); "" = inherit
    # Per-job project directory for an AGENT job: the working directory its
    # session is rooted at, exactly as a dashboard chat scoped to a project
    # (``slot.project``) is. That cwd is what makes the repo's own
    # ``.kiro/steering/**/*.md`` load (kiro-cli resolves the agent's steering
    # resources relative to its cwd) and what the ``[PROJECT]`` context line
    # names. ``""`` (every job predating the field) keeps the gateway default
    # -- the workspace directory -- which carries no repo steering at all.
    #
    # Stored as a validated REALPATH (see validate_cron_project_dir); the
    # gateway re-checks it at fire time and FAILS the run when the directory
    # is gone or has become sensitive, rather than silently running the job
    # unscoped -- a repo-scoped job running without its repo policy is the
    # exact drift this field exists to prevent. Refused for script/command
    # jobs, which launch no session.
    project_dir: str = ""
    # The project_dir this job's conversation BEGAN under when ``project_dir``
    # was last changed or cleared, held until the gateway's next wake has
    # re-rooted that conversation. Written by the store on the rescope (never
    # by a caller), read and cleared by the gateway. It is what tells "this
    # job's project was cleared, so its conversation still rooted at the old
    # repository must end" apart from "this job never had a project", which
    # the ``""`` in ``project_dir`` alone cannot: a job that never opted in is
    # never policed, whatever directory its tab's own project control moved
    # its session to.
    project_dir_was: str = ""
    # The CHAT (sidebar) folder the job's ``cron-{id}`` tab is filed into; "" =
    # not filed, which is what every job predating the field keeps doing. Its
    # run stamps and markers already make that one tab the job's timeline.
    #
    # Distinct from ``folder_id`` above, and the two are never interchangeable:
    # ``folder_id`` groups the job's ROW on the Schedule page
    # (``cron_folders.json``), while this names a folder in the chat sidebar's
    # own tree (``folders.json``) and decides where the job's TAB lands. A job
    # may carry either, both or neither, and one is never derived from the other.
    #
    # PERSISTENT jobs only: a stateless job (``persistent_session=False``) has
    # no job-wide tab to file, so the store refuses the pair at save time rather
    # than accepting a setting with no observable effect.
    #
    # Filing SUPPLEMENTS delivery, never replaces it: a run with a chat folder
    # still reaches its Slack DM, its dashboard notification and its origin
    # session exactly as it did before.
    #
    # CONTRACT for consumers, matching ``folder_id``'s: an id that does not
    # match a folder in ``folders.json`` MUST be treated as "not filed". A chat
    # folder can be deleted while a job still names it, and a dangling id must
    # cost the tab its place in the sidebar and nothing else -- the run still
    # delivers, and the skip is recorded once (see
    # ``cron_inject.chat_folder_for_minted_tab``). A RENAME is a no-op: ids
    # are stable, so the job follows the folder under its new name.
    chat_folder_id: str = ""
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
    #: The run GENERATION the store holds: a per-job counter, one above the
    #: previous run's, allocated to a run at its first step -- or to the
    #: ``cancel()`` / reap that takes its claim before that -- by
    #: ``RunClaims.next_generation`` and written to the record by the
    #: two terminal merges only (``_merge_job_result`` for a completed run,
    #: ``_merge_terminal_state_locked`` for a cancelled or reaped one). Both
    #: merges run in a worker thread behind the release of the run's claim,
    #: under a store lock other writers contend for, so a replacement run
    #: accepted in that window can complete and merge FIRST; the older run's
    #: record is then discarded rather than applied over it (status, error,
    #: result and the failure counter would all revert until some later run
    #: merged again). An integer, not a timestamp: Windows' ``time.time()``
    #: ticks every ~15.6 ms, so two runs of one job can claim with EQUAL
    #: timestamps, which no "newer wins" rule can order. 0 is a legacy record
    #: or a job that never ran.
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
    # Operator-approved vault secrets for SCRIPT jobs: env-var name ->
    # vault secret NAME (kiro_crew.secrets.SecretVault; plaintext never touches
    # this store). Minted ONLY by the owner approving an agent request on the
    # Schedule page — no surface writes an active grant directly, so an agent
    # cannot grant itself vault access. secret_env_pin (keyed, epoch-bound
    # HMAC over the script spec + message + body bytes, see
    # cron_script.compute_secret_env_pin) binds the grant to the code the
    # operator approved: the crons/ scripts stay agent-writeable by design, so
    # a body rewritten after approval fails closed at fire time instead of
    # running with the secrets.
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

    def record_failure(self) -> None:
        """Count one consecutive failure and auto-pause once the threshold is hit.

        Auto-pause is execution-owned: it sets both `enabled` (so the in-memory
        scheduler stops firing immediately) and `auto_paused` (the durable reason,
        distinct from a user pause), so the pause survives a reload. Single-sourced
        here so the many script/command failure branches can't drift on how a pause
        is recorded — mirroring how the effective-enabled derivation reads it back.
        """
        self.consecutive_failures += 1
        self.failure_recorded = True
        if self.consecutive_failures >= _AUTO_PAUSE_THRESHOLD and not self.auto_paused:
            self.enabled = False
            self.auto_paused = True
            self._audit_pause_change("auto_paused")

    def record_success(self) -> None:
        """Reset the failure counter and lift any execution auto-pause.

        Clearing an auto-pause also re-enables the job, because ``enabled`` is
        not independent state: :func:`_job_enabled` reconstructs it on load as
        ``not user_paused and not auto_paused``. Leaving ``enabled`` False after
        clearing ``auto_paused`` therefore produces a job that is paused in
        memory and enabled on disk — it stays stopped until the next restart
        silently resumes it, which is the surprise a manual "Run Now" on an
        auto-paused job would otherwise spring.

        A job the user paused stays paused: ``user_paused`` is never mutated by
        execution, so it is the discriminator here, and re-enabling THAT is the
        user's action (``enable_job``).

        Also clears the failure-alert dedup fields, and is the ONE owner of that
        reset. A success means the job recovered, so the next failure must alert
        fresh rather than be suppressed as a duplicate of the pre-recovery one --
        and every success path (gate verdict, script, command, the scheduler
        backstop) routes through here, so putting the reset at any single call
        site would silence a relapse on the others for up to
        ``_FAILURE_REMINDER_SECS``.
        """
        self.consecutive_failures = 0
        self.last_failure_hash = ""
        self.last_failure_at = 0.0
        if self.auto_paused:
            self.auto_paused = False
            if not self.user_paused:
                self.enabled = True
            self._audit_pause_change("auto_pause_cleared")
