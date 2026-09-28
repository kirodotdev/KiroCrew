"""Sections that configure the gateway's background services.

Owns the DTOs and defaults for ``taskrunner``, ``orchestrator``, ``messaging``,
``cron_history``, ``monitoring``, ``heartbeat`` and ``watchdog``. The monitoring
runtime bounds come from ``monitoring.limits``, their single owner.
``config.sections`` re-exports every name; this module never imports it, the
loader, schema or validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kiro_crew.config.fields import _meta
from kiro_crew.monitoring.limits import DEFAULT_RUNTIME_CEILING_SECS, MAX_RUNTIME_CEILING_SECS

# Ceiling for a WHOLE orchestrator plan. The per-stage timeout multiplies by
# stage count, so this is the only bound on total unattended runtime.
DEFAULT_MAX_PLAN_DURATION = 7200  # 2 h


DEFAULT_MAX_PARALLEL_STEPS = (
    0  # 0 = auto: derive from agent.subagent_auto_max via compute_max_subagents
)


@dataclass
class TaskRunnerConfig:
    max_parallel_steps: int = field(
        default=DEFAULT_MAX_PARALLEL_STEPS,
        metadata=_meta(
            "Max Parallel Steps",
            "Maximum task steps to run in parallel. 0 = auto (the host-safe cap from agent.subagent_auto_max, clamped to memory/CPU). A positive value only *lowers* concurrency — it is capped at the auto maximum and can never exceed the host-safe limit.",
        ),
    )
    workspace_dir: str = field(
        default="",
        metadata=_meta(
            "Workspace Folder",
            "Absolute path where task runner executions run. When set, "
            "every execution operates in this folder instead of a per-run scratch "
            "directory, so the task runner works on the intended target location. "
            "Empty = use the default per-run workspace directory.",
        ),
    )


@dataclass
class OrchestratorConfig:
    stage_timeout_seconds: int = field(
        default=1800,
        metadata=_meta(
            "Stage Timeout", "Max seconds per stage before auto-run stops. Default 30 min."
        ),
    )
    max_plan_duration_seconds: int = field(
        default=DEFAULT_MAX_PLAN_DURATION,
        metadata=_meta(
            "Max Plan Duration",
            "Ceiling for a WHOLE auto-run plan in seconds, checked at each stage "
            "boundary, with one warning at 75% of the budget. The per-stage "
            "timeout above multiplies by stage count, so without this a long "
            "plan can run unattended for hours. 0 disables. Default 2 h.",
        ),
    )


@dataclass
class MessagingConfig:
    use_transport: bool = field(
        default=True,
        metadata=_meta(
            "Use Transport",
            "Route inbound Slack messages through the SlackTransport → TurnDriver → "
            "SlackRenderer channel-neutral path instead of the native handle_message "
            "monolith. Default ON in Kiro Crew (the transport abstraction is the canonical "
            "path, shared with future channels). Set to false to fall back to the legacy "
            "native handler.",
        ),
    )
    dm_scope: str = field(
        default="per-channel-peer",
        metadata=_meta(
            "DM Session Scope",
            "How direct-message conversations map to sessions. 'per-channel-peer' "
            "(default) keeps one session per (channel, user), so the same person on "
            "Telegram vs WeCom stays isolated. 'unified' collapses all DMs into one "
            "shared session per agent for cross-surface continuity. Takes effect at "
            "the next restart: each conversation's generation counter is seeded from "
            "the namespace in force at boot, so switching namespaces live could "
            "resume a stale session persisted under the other one.",
            restart=True,
        ),
    )
    idle_reset_minutes: int = field(
        default=0,
        metadata=_meta(
            "DM Idle Reset (minutes)",
            "Start a fresh session generation when a DM arrives after this many "
            "minutes of inactivity. 0 (default) disables idle reset.",
        ),
    )
    daily_reset_hour: int = field(
        default=-1,
        metadata=_meta(
            "DM Daily Reset Hour",
            "Local-time hour (0-23) at which the next DM starts a fresh session "
            "generation once per day. -1 (default) disables daily reset.",
        ),
    )
    queue_mode: str = field(
        default="steer",
        metadata=_meta(
            "DM Queue Mode",
            "How a DM that arrives while a turn is running is handled. 'steer' "
            "(default) folds it into the running reply; 'queue' holds it and runs "
            "it after the current turn finishes.",
        ),
    )

    def __post_init__(self) -> None:
        # Fail safe on hand-edited values (mirrors WeComConfig): an unknown scope
        # or mode falls back to the safe default, and the reset windows clamp to
        # valid ranges so a bad config can't wedge dispatch.
        if self.dm_scope not in ("per-channel-peer", "unified"):
            self.dm_scope = "per-channel-peer"
        if self.queue_mode not in ("steer", "queue"):
            self.queue_mode = "steer"
        self.idle_reset_minutes = max(0, self.idle_reset_minutes)
        if not 0 <= self.daily_reset_hour <= 23:
            self.daily_reset_hour = -1


@dataclass
class CronHistoryConfig:
    cron_summary_cap: int = field(
        default=200,
        metadata=_meta("Summary Cap", "Max characters for run summary field."),
    )
    cron_trace_cap_kb: int = field(
        default=50,
        metadata=_meta("Trace Cap KB", "Max kilobytes for run trace field."),
    )
    cron_max_records_per_job: int = field(
        default=100,
        metadata=_meta("Max Records Per Job", "Max history records kept per job file."),
    )
    cron_max_index_records: int = field(
        default=2000,
        metadata=_meta("Max Index Records", "Max records in the global index."),
    )


@dataclass
class MonitoringConfig:
    """Monitor arming preference and finite wall-clock policy.

    The preference changes tool guidance, not eligibility. The runtime ceiling
    is enforced across tools, API mutations and persistence; raising it never
    extends an existing loop's stored budget or creation time.
    """

    max_runtime_secs: int = field(
        default=DEFAULT_RUNTIME_CEILING_SECS,
        metadata=_meta(
            "Maximum monitoring runtime (seconds)",
            "Finite wall-clock ceiling for new and updated monitors. Accepts up to "
            "2592000 seconds (30 days). Raising this limit never extends an existing deadline.",
            min=1,
            max=MAX_RUNTIME_CEILING_SECS,
        ),
    )

    prefer_structured_arming: bool = field(
        default=False,
        metadata=_meta(
            "Prefer the structured monitor when arming",
            "Which side has to justify itself before a supported pull request is "
            "watched. Off, the default and the shipped wording: the structured "
            "monitor monitor_watch is admissible only once the caller has "
            "satisfied itself that the objective is fully determined by typed "
            "provider facts, a judgement that leans to the prompt loop whenever "
            "the caller is unsure. On: a supported pull request is enough, and "
            "the prompt loop monitor_start becomes the exception that needs its "
            "own reason. Both positions send evidence the typed provider cannot "
            "observe -- comments, advisory review findings -- to the prompt loop, "
            "so this moves the burden rather than swapping two defaults. What it "
            "changes is the text those two descriptions give the agent: it does "
            "not refuse either tool and cannot guarantee which one the agent "
            "picks. Neither path is gated by this key -- both are armable with it "
            "off -- so turning it on grants no new unattended capability. The "
            "value is read afresh every time the tool list is built, so no "
            "gateway restart is needed; a session already open keeps the tool "
            "list it was given, so the change reaches the next session. Two "
            "things to know before turning it on. The structured path observes "
            "typed provider facts only -- lifecycle, checks, mergeability, "
            "review decision, review threads -- and not generic comments or "
            "advisory review findings, so an objective that depends on reading "
            "those still needs the prompt loop. And this key is reversible but "
            "an already-armed structured monitor is not: stopping one records a "
            "retained USER_STOP outcome that refuses a re-arm, so moving such a "
            "session to the prompt loop needs its owner to clear that record in "
            "the dashboard's monitor popover first.",
        ),
    )


@dataclass
class HeartbeatConfig:
    """Heartbeat background task queue (~/.kiro/crew/workspace/HEARTBEAT.md)."""

    default_deliver: str = field(
        default="slack",
        metadata=_meta(
            "Default delivery",
            "Where a heartbeat completion with no inline <!-- deliver:... --> tag is "
            "routed: 'slack' (Slack DM + dashboard bell, the default) or 'dashboard' "
            "(dashboard slot + bell only, no Slack). Per-task deliver tags always "
            "override this.",
        ),
    )


@dataclass
class WatchdogConfig:
    """ACP per-session watchdog / liveness-oracle tuning (acp/session_handle.py).

    Wellness (the liveness oracle) is the primary detector; these windows govern
    only the UNKNOWN-verdict backstop class. A WORKING verdict is never acted on
    at any elapsed time, and every watchdog action is non-lethal (auto-recovery,
    never a silent kill).
    """

    check_after_secs: float = field(
        default=60.0,
        metadata=_meta(
            "Check after (s)",
            "Idle seconds on a turn before the liveness oracle is consulted at all. "
            "Below this, the dispatch loop does no watchdog work.",
        ),
    )
    stale_window_secs: float = field(
        default=600.0,
        metadata=_meta(
            "Stale probe window (s)",
            "Idle seconds before an UNKNOWN-verdict model-wait turn is safe-probed "
            "via session/cancel. Probes are non-lethal, but a probe of a LIVE think "
            "cancels and regenerates it, so the window must clear an ordinary "
            "silent think. Default 10 min. A think the oracle can attest (an "
            "established backend socket) gets the longer model-silent window "
            "instead, so this one governs only thinks with no such evidence: a "
            "host without procfs, or a backend connection that is momentarily "
            "down. Its cost is wedge-recovery latency on a runtime that is "
            "already dead, never lost work.",
        ),
    )
    tool_stall_suspect_secs: float = field(
        default=5400.0,
        metadata=_meta(
            "Tool stall suspect (s)",
            "Idle seconds before an UNKNOWN-verdict in-flight tool is cancelled and "
            "the turn routed to tool-stall recovery (continue-nudge, no re-run of "
            "the original message). WORKING tools (a matched live build child, an "
            "MCP subtree with CPU movement) are never cancelled regardless of "
            "duration, so this window governs only what the liveness oracle cannot "
            "attest. Default 90 min: it clears every shipped budget a single tool "
            "call can legitimately spend silent (the task runner's 90-minute test "
            "command, a full test suite with one retry in one shell call) while "
            "still landing inside the turn's own ceiling "
            "(agent.chat_turn_timeout_secs) so recovery is reachable. Enforcement is "
            "at handle construction, not config load: a window past the headroom "
            "fraction of the transport's per-prompt timeout is clamped with a "
            "warning, while one that merely exceeds agent.chat_turn_timeout_secs is "
            "warned about but left as set, because the same handle also serves "
            "callers that pass a larger prompt timeout (review and cron turns).",
        ),
    )
    tool_stall_hard_cap_secs: float = field(
        default=7200.0,
        metadata=_meta(
            "Hard cap (s)",
            "Absolute ceiling for UNKNOWN-verdict forbearance (e.g. the extended "
            "probably-thinking window) and for any per-agent "
            "watchdog_tool_stall_* override. Applies ONLY to UNKNOWN verdicts — "
            "never to a WORKING session, which is deferred before this cap is "
            "consulted and is therefore bounded only by the turn's own ceiling. "
            "Default 2h, clamped against the transport's per-prompt timeout like "
            "the suspect window.",
        ),
    )
    model_silent_probe_secs: float = field(
        default=1800.0,
        metadata=_meta(
            "Silent-think probe window (s)",
            "Extended probe window for a model-wait with an established backend "
            "connection but flat counters (non-streamed server-side reasoning, "
            "e.g. long xhigh thinks). Probing a live think cancels and regenerates "
            "it, so this window is deliberately generous: 30 min clears the long "
            "end of an extended-effort think.",
        ),
    )
    wellness_sample_secs: float = field(
        default=3.0,
        metadata=_meta(
            "Wellness sample interval (s)",
            "Minimum spacing between CPU/IO counter samples used for movement "
            "deltas in the liveness oracle.",
        ),
    )
