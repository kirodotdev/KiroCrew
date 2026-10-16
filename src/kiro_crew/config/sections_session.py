"""The ``session`` section, crew records, workspaces, and a session's resolved bindings.

An owner of the :mod:`kiro_crew.config.sections` facade; it never imports the facade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from kiro_crew.config.fields import _meta
from kiro_crew.config.sections_defaults import (
    DEFAULT_AUTOCOMPACT_PCT,
    DEFAULT_POOL_SIZE,
    DEFAULT_RECONCILE_MAX_KILLS,
    DEFAULT_SESSION_TIMEOUT,
    DEFAULT_WATCHDOG_RSS_MAX_MB,
    SectionReader,
)
from kiro_crew.config.session_environments import (
    ARCHIVED_RETENTION_DAYS_DEFAULT,
    load_archived_retention_days,
    load_default_profile,
    load_session_environments,
    load_stop_on_shutdown,
)


def _archive_retention_days(session_data: dict) -> int:
    """Resolve session.archive_retention_days, normalizing the disable sentinel.

    An explicit ``null`` (``None``) and any negative value both mean "disable
    automatic cleanup"; both normalize to ``-1``.  A non-negative integer is the
    retention window in days.  An omitted key, or a value ``int()`` cannot read,
    takes the field's default (30).
    """
    section = SectionReader(SessionConfig, session_data)
    raw = section.get("archive_retention_days")
    if raw is None:
        return -1
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return section.default("archive_retention_days")
    return val if val >= 0 else -1


@dataclass
class SessionConfig:
    timeout_secs: int = field(
        default=DEFAULT_SESSION_TIMEOUT,
        metadata=_meta("Session Timeout", "Idle session timeout in seconds."),
    )
    idle_exempt_keys: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Idle-Exempt Sessions",
            "Session keys the idle timeout never ends, for a chat that automation wakes "
            "hours apart. A dashboard chat's key is dashboard: followed by the sid in its "
            "URL, e.g. dashboard:chat-12-1790000000, unless the tab is linked to a cron job "
            "or a channel thread, which uses that link's key instead, e.g. cron:<job id>. "
            "Closing a dashboard or cron tab still ends its process; a channel thread's "
            "session belongs to the channel, so closing its viewer does not. The Watchdog "
            "RSS Limit, when set, still applies. A listed session with no tab, such as a "
            "cron job that runs without one, keeps running even after its job is deleted, "
            "so take a deleted job's key off the list. The RSS limit or a gateway restart "
            "still ends it. On the kiro-cli or KAS backend, a Kiro sign-out from the dashboard "
            "ends it at once, whatever account it signed in with. After any other switch or "
            "sign-out of the Kiro account it signed in with, the next dashboard chat turn ends "
            "it, and so does the next agent cron run unless Kiro Crew then cannot tell which "
            "account is signed in, as with a social login. A session that "
            "is mid-turn then ends later instead, when its next turn starts or at a later "
            "dashboard chat turn or agent cron run. At most 10 keys are kept.",
        ),
    )
    empty_response_auto_continue: bool = field(
        default=True,
        metadata=_meta(
            "Auto-Continue on Empty Response",
            "After the model returns an empty response twice in a row, "
            "automatically send a 'continue' nudge on the same session "
            "(transcript-visible, bounded by Max Auto-Continues on Empty "
            "Response).",
        ),
    )
    empty_response_max_continues: int = field(
        default=1,
        metadata=_meta(
            "Max Auto-Continues on Empty Response",
            "How many 'continue' nudges may run back to back before the "
            "runner gives up and asks for a message (1-10). Above 1 the "
            "recovery notice shows progress ('recovery 2 of 3'). Useful "
            "during provider-instability windows where each continuation "
            "makes real progress before failing the same way. Only applies "
            "while Auto-Continue on Empty Response is enabled.",
        ),
    )
    autocompact_pct: float = field(
        default=DEFAULT_AUTOCOMPACT_PCT,
        metadata=_meta(
            "Auto-Compact Threshold",
            "Context usage percentage at which auto-compaction triggers (5-90).",
        ),
    )
    compact_wait_secs: float = field(
        default=0.0,
        metadata=_meta(
            "Compaction Wait Budget",
            "Seconds to wait for a compaction to finish: automatic, the task "
            "runner's context-overflow compaction, and a manual /compact on "
            "any surface. Past it, the session manager's automatic compaction "
            "and the task runner's compaction restart the session, a chat "
            "channel's near-limit compaction gives up and keeps the session, "
            "and a manual /compact reports that it timed out. 0 (the default) "
            "uses the built-in budget. A positive value "
            "below 60 is raised to 60 and a value above 3600 is capped. Raise "
            "it on a host where compaction on a large context window regularly "
            "needs longer than the built-in budget; a stuck compaction also "
            "waits the full budget.",
        ),
    )
    pool_size: int = field(
        default=DEFAULT_POOL_SIZE,
        metadata=_meta(
            "Warm Pool Size",
            "Number of pre-spawned kiro-cli processes kept ready for instant session start. 0 disables.",
        ),
    )
    pool_agent: str = field(
        default="",
        metadata=_meta(
            "Warm Pool Agent",
            "Agent name for warm pool processes. Empty string uses agent.default_agent.",
        ),
    )
    pool_ttl_secs: int = field(
        default=1800,
        metadata=_meta(
            "Warm Pool TTL",
            "Max age in seconds for pooled processes. Stale processes are discarded at claim time. 0 disables.",
        ),
    )
    eager_spawn: bool = field(
        default=True,
        metadata=_meta(
            "Eager Session Spawn",
            "Speculatively create a chat slot's session when the slot is created, "
            "its agent is switched, or its project directory changes, instead of "
            "on first message. Hides the multi-second session handshake behind "
            "user think-time.",
        ),
    )
    archive_retention_days: int = field(
        default=30,
        metadata=_meta(
            "Archive Retention (days)",
            "Days to keep compacted/rotated session archives before auto-cleanup. "
            "-1 disables cleanup (manage deletion manually). The same window collects "
            "a closed session's crew log, and with the crew log on that is where "
            "Issue Radar keeps each repository's shared skip memory and its crews' "
            "open work items.",
            nullable=True,
        ),
    )
    file_changes_keep_turns: int = field(
        default=0,
        metadata=_meta(
            "File-Change Copies Kept (turns)",
            "How many of a dashboard chat's most recent file-changing turns keep "
            "the before and after copies their file-change card diffs. An older "
            "turn keeps its file list, and the rows that held its copies are "
            "written to the session archive first. 0, the default, keeps every "
            "turn's copies.",
        ),
    )
    watchdog_rss_max_mb: int = field(
        default=DEFAULT_WATCHDOG_RSS_MAX_MB,
        metadata=_meta(
            "Watchdog RSS Limit (MiB)",
            "Recycle a session when its process tree resident memory exceeds "
            "this many MiB. 0 disables (the default); the internal background "
            "runtime still recycles at 1536 MiB. Busy sessions (turn in "
            "flight) are never recycled.",
        ),
    )
    reconcile_max_kills: int = field(
        default=DEFAULT_RECONCILE_MAX_KILLS,
        metadata=_meta(
            "Reconciler Kill Budget (per pass)",
            "Unowned root candidates the runtime reconciler may signal the process "
            "tree of in one pass, at most 5 -- one candidate can signal several "
            "processes. Lower it where more than one install shares this data "
            "home, since a runtime owned by another install has no record here and "
            "reads as unowned. 0 makes the arm observe-only: it still publishes the "
            "leak reading and audits each candidate it would have signalled.",
        ),
    )


#: The template kiro-cli runs when ``agent.default_agent`` names none.
DEFAULT_KIRO_TEMPLATE = "kirocrew"


@dataclass
class KiroCrewAgentConfig:
    member_id: str = field(
        default="",
        metadata=_meta("Member ID", "Immutable identity assigned when member memory is created."),
    )
    kiro_agent: str = field(
        default="",
        metadata=_meta("Kiro Agent", "Kiro agent name (modeId for session/set_mode)."),
    )
    workspace: str = field(
        default="default",
        metadata=_meta("Workspace", "Named workspace from the workspaces section."),
    )
    memory_store: str = field(
        default="default",
        metadata=_meta("Memory Store", "Named memory store from the memory_stores section."),
    )
    model: str = field(
        default="",
        metadata=_meta(
            "Model",
            "Default model for sessions on this agent. Empty inherits: the bound "
            "kiro agent's own pinned model first, then the global agent.model "
            "fallback. A per-session pick still overrides this.",
        ),
    )
    reasoning_effort: str = field(
        default="",
        metadata=_meta(
            "Reasoning Effort",
            "Default reasoning effort for sessions on this crew. Empty inherits: "
            "the global agent.reasoning_effort (or, for a background worker crew, "
            "its role effort). A per-session pick still overrides this. Only "
            "reasoning-capable models accept a level; on any other model the pin "
            "is ignored, exactly as the global default is.",
        ),
    )
    worker_model: str = field(
        default="",
        metadata=_meta(
            "Worker Model",
            "Model for the sessions this crewmate dispatches through "
            "session_create when the call names none. Empty copies the "
            "crewmate's model pin when set; with both fields empty, no session "
            "model is pinned and the worker's own agent or the global default "
            "decides. Unlike 'model', 'auto' is a real pin here: workers run "
            "on the backend's own default, which is how a crewmate on a costly "
            "model keeps its workers cheap. An explicit model on the call wins.",
        ),
    )
    worker_reasoning_effort: str = field(
        default="",
        metadata=_meta(
            "Worker Reasoning Effort",
            "Reasoning effort for the sessions this crewmate dispatches through "
            "session_create when the call names none. Empty means: while the "
            "workers run the crewmate's own model (worker_model empty and no model "
            "on the call) they take the crewmate's own effort (the field above); "
            "under any other model no level is pinned and the worker's agent "
            "decides as usual (its own crew pin if it runs as a crewmate, else "
            "agent.reasoning_effort, else the model default). Set, one of the five "
            "standard levels. An explicit effort on the call wins.",
        ),
    )
    approval_mode: str = field(
        default="",
        metadata=_meta(
            "Approval Mode",
            "Tool approval for this crewmate's own Crewmates-page thread: normal, "
            "trust_reads or trust. Empty means the user never chose one, and the "
            "thread opens in trust.",
        ),
    )
    display_name: str = field(
        default="",
        metadata=_meta(
            "Display Name",
            "Optional label the dashboard shows instead of the agent's name. "
            "Purely presentational: the name stays the immutable identity — it "
            "keys this record, addresses /api/agents/{name}, and is what "
            "dispatch, crons and spawn resolve — so renaming the label never "
            "breaks a binding. Empty means the dashboard shows the name itself.",
        ),
    )
    description: str = field(
        default="",
        metadata=_meta("Description", "Human-readable agent description."),
    )
    triggers: str = field(
        default="",
        metadata=_meta(
            "Triggers",
            "Routing intent for orchestrator crew selection: free-text 'when to "
            "use this crew' guidance the main agent reads via select_crew. A crew "
            "with no triggers is not offered for selection.",
        ),
    )
    source: str = field(
        default="kirocrew",
        metadata=_meta("Source", "Agent origin: kirocrew or builtin."),
    )
    starred: bool = field(
        default=False,
        metadata=_meta(
            "Starred",
            "Marks this crew as a favourite on the Crew Members page. Purely a "
            "roster preference: the page's star filter shows only starred "
            "crews, so the dozens of package-installed crews the agent sync "
            "writes here can be collapsed behind the few you actually drive. "
            "Never read by routing, spawning, or the orchestrator.",
        ),
    )
    # Per-agent watchdog window overrides. The global ``watchdog.tool_stall_*``
    # defaults are build-scale forbearance; an agent that never runs a long
    # build (a pure-LLM reviewer, read-only git) can declare much lower windows
    # here. 0 (the default) inherits the global value — mirrors the
    # empty-inherits convention of ``model`` above.
    watchdog_tool_stall_suspect_secs: float = field(
        default=0.0,
        metadata=_meta(
            "Tool stall suspect override (s)",
            "Per-agent override for watchdog.tool_stall_suspect_secs on sessions "
            "running this agent. 0 inherits watchdog.tool_stall_suspect_secs (tuned "
            "for long builds). Set low (e.g. 900) for a pure-LLM agent whose "
            "longest legitimate silent gap is minutes, not hours.",
        ),
    )
    watchdog_tool_stall_hard_cap_secs: float = field(
        default=0.0,
        metadata=_meta(
            "Tool stall hard cap override (s)",
            "Per-agent override for watchdog.tool_stall_hard_cap_secs on sessions "
            "running this agent. 0 inherits watchdog.tool_stall_hard_cap_secs. Applies "
            "to UNKNOWN verdicts and to an opaque MCP tool whose only WORKING "
            "evidence is movement in the runtime's process tree; every other "
            "WORKING session is never acted on.",
        ),
    )
    # Per-agent compaction threshold. Same empty-inherits convention as the
    # watchdog windows above: 0 means "use session.autocompact_pct". A
    # session's own slider override still wins over this.
    autocompact_pct: float = field(
        default=0.0,
        metadata=_meta(
            "Auto-compact threshold override (%)",
            "Default context-usage percentage at which sessions running this "
            "agent compact. 0 inherits session.autocompact_pct, and so does a "
            "negative value. A positive value is held to the same 5-90 range as "
            "the global. Set low (e.g. 45) for "
            "an orchestrator that should stay sharp, high (e.g. 85) for a worker "
            "that should keep its long context. A session's own threshold "
            "override (the context popover slider) still takes precedence.",
        ),
    )
    session_color: str = field(
        default="",
        metadata=_meta(
            "Session Color",
            "Default session color for sessions created by this agent. Accepts "
            "a CSS hex color string (#rrggbb, lowercase). Applied at render time "
            "to any session this agent started that has no color of its own, so "
            "editing it re-tints those sessions live. A color set on the session "
            "itself (a manual pick or the dashboard default-color policy) always "
            "takes precedence. Empty means no agent color.",
        ),
    )
    telegram_account: str = field(
        default="",
        metadata=_meta(
            "Telegram Account",
            "Deprecated and inert: a binding to a named telegram.accounts entry "
            "no longer routes anything, because named accounts no longer start a "
            "bot. Preserved on load and save so an existing config is not "
            "rewritten out from under the operator.",
            deprecated=True,
        ),
    )
    avatar: dict = field(
        default_factory=dict,
        metadata=_meta(
            "Avatar",
            "Per-crew avatar override. Empty means the face is derived from "
            "the crew's name. {'kind': 'ghost', 'traits': {...}} pins explicit "
            "ghost traits chosen in the avatar builder; {'kind': 'image'} "
            "means an uploaded picture served from the per-crew avatar "
            "endpoint.",
        ),
    )


@dataclass
class WorkspaceConfig:
    dir: str = field(
        default="workspace",
        metadata=_meta("Directory", "Workspace directory path."),
    )


def _migrate_workspaces(raw_workspaces: dict) -> dict[str, WorkspaceConfig]:
    """Auto-migrate workspaces from flat or structured format.

    - String values → WorkspaceConfig(dir=value)
    - Dict values → WorkspaceConfig(dir=value["dir"]), the field default without ``dir``
    - Non-string/non-dict values → default WorkspaceConfig()
    - Empty input → {"default": WorkspaceConfig()}
    """
    result: dict[str, WorkspaceConfig] = {}
    for name, value in raw_workspaces.items():
        if isinstance(value, str):
            result[name] = WorkspaceConfig(dir=value)
        elif isinstance(value, dict):
            result[name] = WorkspaceConfig(dir=SectionReader(WorkspaceConfig, value).get("dir"))
        else:
            result[name] = WorkspaceConfig()
    if not result:
        result["default"] = WorkspaceConfig()
    return result


@dataclass
class SessionEnvironmentProfileConfig:
    """One named remote workspace profile the Session Environments UI offers.

    Configuration only: a profile names a template and a working directory, it
    does not provision anything. Validated by
    ``config.session_environments.validate_profile``.
    """

    id: str = field(
        default="",
        metadata=_meta(
            "Profile ID",
            "Stable identifier: an ASCII letter or digit first, then letters, "
            "digits, '_' or '-'; at most 64 characters and unique within the list.",
        ),
    )
    name: str = field(
        default="",
        metadata=_meta("Profile Name", "Display name shown in the picker (1-128 characters)."),
    )
    template: str = field(
        default="",
        metadata=_meta(
            "Template", "Name of the workspace template this profile uses (1-255 characters)."
        ),
    )
    workdir: str = field(
        default="",
        metadata=_meta(
            "Working Directory",
            "Absolute POSIX path sessions start in inside the workspace "
            "(at most 1024 characters).",
        ),
    )
    compute_survives_stop: bool = field(
        default=True,
        metadata=_meta(
            "Files Survive A Stop",
            "Whether the template's workspace storage survives a workspace stop. "
            "Turn it off for a template whose stop destroys the compute and its "
            "files (such as MicroVM): Kiro Crew then never stops that workspace "
            "itself, and its session is not recoverable after a stop.",
        ),
    )


@dataclass
class SessionEnvironmentsConfig:
    """Persisted Session Environments settings (configuration only).

    Saving this section registers no provider, activates no remote runtime and
    contacts nothing. It holds no credential. The UI is gated by the per-device
    preview flag, so there is deliberately no ``enabled`` field here.
    """

    coder_url: str = field(
        default="",
        metadata=_meta(
            "Coder URL",
            "Base URL of the Coder deployment: empty, or an http(s) URL with a "
            "hostname and no username, password, query or fragment.",
        ),
    )
    profiles: list[SessionEnvironmentProfileConfig] = field(
        default_factory=list,
        metadata=_meta(
            "Profiles",
            "Named workspace profiles (at most 20), each {id, name, template, workdir, "
            "compute_survives_stop}.",
        ),
    )

    archived_retention_days: int = field(
        default=ARCHIVED_RETENTION_DAYS_DEFAULT,
        metadata=_meta(
            "Keep Archived Workspaces",
            "Days an archived session's workspace is kept, stopped, before it is "
            "deleted along with its files (0-7, 0 deletes it as soon as the session "
            "is archived). Reopening the session before then keeps the workspace.",
        ),
    )

    default_profile: str = field(
        default="",
        metadata=_meta(
            "Default Environment",
            "Profile id new dashboard chats start on; empty runs them on this machine. "
            "Applied once, when a chat is created, and changeable in its composer until "
            "the first message. Scheduled jobs, channels and subagents are unaffected "
            "(a subagent follows its parent).",
        ),
    )

    stop_on_shutdown: bool = field(
        default=True,
        metadata=_meta(
            "Stop On Shutdown",
            "Stop every remote session environment when the gateway shuts down, except "
            "one whose files do not survive a workspace stop. Off keeps them running "
            "across a restart, which reconnects faster. Coder's own auto-stop still "
            "applies either way.",
        ),
    )

    @classmethod
    def from_raw(cls, section: object) -> "SessionEnvironmentsConfig":
        """Build from the raw section, keeping every valid part (never raises)."""
        coder_url, profiles = load_session_environments(section)
        return cls(
            coder_url=coder_url,
            profiles=[SessionEnvironmentProfileConfig(**p) for p in profiles],
            archived_retention_days=load_archived_retention_days(section),
            default_profile=load_default_profile(section),
            stop_on_shutdown=load_stop_on_shutdown(section),
        )


@dataclass
class ResolvedBindings:
    """Resolved workspace, memory store, and kiro agent for a session."""

    workspace_dir: Path
    memory_store_name: str
    effective_memory_config: dict
    kiro_agent: str
    execution_context: object | None = None
    # The Kiro Crew agent's own default model, "" when it pins none. Ranks below
    # a per-session pick and above the bound kiro agent's pin / the global
    # agent.model fallback. Defaulted so existing keyword constructions and
    # test doubles built before this field stay valid.
    model: str = ""
    # Whether the REQUESTED agent name was actually honored. False means the
    # resolver fell back to the default agent, so dispatching these bindings runs
    # a different agent than the caller asked for. Callers that store the
    # requested name (chat slots) must not advertise it when this is False.
    # Defaults True so constructions predating this field keep their meaning.
    requested_resolved: bool = True
    # The Kiro Crew ALIAS whose bindings these are ("" when no alias applied). A
    # caller replacing an unhonored request must store THIS, not ``kiro_agent``:
    # the stored value is re-resolved later and an alias is matched first, so a
    # physical kiro agent name that also happens to be an alias key would resolve
    # to that alias's target instead — reintroducing the advertised-vs-answering
    # mismatch. An alias key round-trips to itself.
    resolved_alias: str = ""
    # Positive selection provenance. A later discovered member with the same
    # name must not change a conversation that selected the provider template.
    selection_kind: str = ""
    # Revision observed before resolution: "" means no protected record;
    # None means no observation was made. Only automatic publication uses it.
    selection_revision: str | None = None

    def same_dispatch_binding(self, other: "ResolvedBindings") -> bool:
        """Whether two resolutions name the SAME dispatch target.

        Owned here, next to the field set, so a future dispatch-relevant
        binding field forces the identity question at the layer that defines
        it rather than silently widening a permission check that enumerated
        fields by hand (the dashboard's slot agent-conflict guard uses this to
        decide whether two different NAMES may share a slot). Compares every
        field that changes what answers a turn — the kiro agent, workspace,
        memory store, and model — and deliberately not ``resolved_alias``
        (two names resolving to one alias's target ARE the same binding) or
        ``selection_kind`` (the namespace is retained separately for later
        resolution; identical current targets may still share a slot) or
        ``selection_revision`` (a publication guard, not a dispatch target) or
        ``requested_resolved``/``effective_memory_config`` (the former is
        request metadata the caller checks separately; the latter is derived
        from ``memory_store_name`` plus global config shared by both sides).
        ``execution_context`` carries the admitted session's identity and mode;
        session selection checks those separately before comparing these targets.
        """
        return (
            self.kiro_agent == other.kiro_agent
            and self.workspace_dir == other.workspace_dir
            and self.memory_store_name == other.memory_store_name
            and self.model == other.model
        )
