"""The ``dashboard`` section DTO, its Jira and link-rule records, and its bounds.

An owner of the :mod:`kiro_crew.config.sections` facade; it never imports the facade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import overload

from kiro_crew.config.fields import _meta
from kiro_crew.config.sections_tailnet import TailscaleConfig

# The terminal apps the desktop shell can open in a project directory
# (``dashboard.terminal.external_app``), by id. Must equal the ids of
# ``TERMINALS`` in ``website/electron/external-terminal.js``, which maps each id
# to its fixed install paths and pinned argv; test_external_terminal_gate.py
# pins the two lists equal.
EXTERNAL_TERMINAL_APPS: tuple[str, ...] = (
    "alacritty",
    "ghostty",
    "gnome-terminal",
    "kitty",
    "konsole",
    "wezterm",
    "xfce4-terminal",
)


@dataclass
class JiraAuthEntry:
    """Connection metadata for one Jira instance (Cloud or Server/DC).

    The API token is NOT stored here — it lives in the protected .env file
    as JIRA_API_TOKEN (same isolation pattern as Slack/Discord/Telegram tokens).
    This dataclass holds only non-sensitive connection metadata.
    """

    host: str = field(
        default="",
        metadata=_meta(
            "Host",
            "Jira instance hostname (e.g. 'myorg.atlassian.net' or "
            "'jira.internal.corp:8443'). Must match the host in the issue URL.",
        ),
    )
    email: str = field(
        default="",
        metadata=_meta(
            "Email",
            "Atlassian account email for Cloud instances (used in Basic auth "
            "header). Leave empty for Server/DC instances that use a PAT.",
        ),
    )


@dataclass
class LinkPatternRule:
    """One text-to-link rewrite rule for chat transcripts.

    Rendering-only: the dashboard rewrites matching plain text into links at
    display time; stored messages are never modified. The pattern is compiled
    by the BROWSER (JavaScript regex dialect), so the backend validates only
    shape and size, never regex semantics.
    """

    pattern: str = field(
        default="",
        metadata=_meta(
            "Pattern",
            "JavaScript regular expression matched against transcript text "
            "(e.g. '\\\\bPROJ-\\\\d+\\\\b'). A rule that matches the empty string is "
            "ignored.",
        ),
    )
    url: str = field(
        default="",
        metadata=_meta(
            "URL Template",
            "Link target for each match: an absolute http(s) URL in which "
            "'{match}' inserts the matched text, percent-encoded (e.g. "
            "'https://tracker.example.com/browse/{match}'). The renderer "
            "additionally requires the placeholder outside the host and "
            "refuses userinfo.",
        ),
    )


# dashboard.link_patterns -- bounds on operator-supplied transcript link rules.
# The count cap bounds per-message scan work (each rule is one regex pass over
# every rendered markdown block); the length caps bound pathological patterns
# and keep the config API payload small.
LINK_PATTERNS_MAX = 50
LINK_PATTERN_PATTERN_MAX_LEN = 300
LINK_PATTERN_URL_MAX_LEN = 2000


# dashboard.loop_stall_exit_after_secs -- event-loop silence tolerated before
# the gateway dumps all thread stacks and hard-exits. ``None`` is the
# serializable "automatic" sentinel: launch class selects the desktop or
# managed-service default without an unrelated config save pinning either one.
LOOP_STALL_EXIT_AFTER_MIN = 10
LOOP_STALL_EXIT_AFTER_MAX = 300
LOOP_STALL_EXIT_AFTER_DEFAULT = 25
LOOP_STALL_EXIT_AFTER_MANAGED_DEFAULT = 90
_MANAGED_SERVICE_ENV = "KIROCREW_SERVICE_MANAGED"

# dashboard.chat_entry_cache_max_entries / chat_entry_cache_max_bytes -- bounds
# on the persisted-message entry memo in ``dashboard/chat_persistence.py``. The
# right entry count is host-dependent: the cache's working set is roughly
# ``active_slots x window_size``, so a gateway with many concurrent chat slots
# overflows the entry bound while the byte bound still has headroom, and the LRU
# then evicts each slot's window just before its next save (a zero-hit cliff,
# every save re-paying redaction plus key derivation). The defaults match the
# previous hardcoded values; raising the entry bound on a many-slot host is the
# operator's call, with the byte ceiling still bounding memory.
CHAT_ENTRY_CACHE_ENTRIES_MIN = 256
CHAT_ENTRY_CACHE_ENTRIES_MAX = 262144
CHAT_ENTRY_CACHE_ENTRIES_DEFAULT = 4096
CHAT_ENTRY_CACHE_BYTES_MIN = 4 * 1024 * 1024
CHAT_ENTRY_CACHE_BYTES_MAX = 512 * 1024 * 1024
CHAT_ENTRY_CACHE_BYTES_DEFAULT = 32 * 1024 * 1024

#: The ``dashboard.verbosity`` levels a writer may store.
VERBOSITY_LEVELS: tuple[str, ...] = ("default", "concise", "answer_only")
#: Retired levels and the level each one now means. ``ultra`` sat between
#: ``concise`` and ``answer_only`` and was folded into the terser neighbour, so a
#: config or an older client that still says ``ultra`` keeps a terse reply style.
LEGACY_VERBOSITY_ALIASES: dict[str, str] = {"ultra": "answer_only"}


@overload
def normalize_verbosity(raw: str) -> str: ...


@overload
def normalize_verbosity(raw: object) -> object: ...


def normalize_verbosity(raw: object) -> object:
    """Map a retired ``dashboard.verbosity`` level onto its replacement.

    Anything that is not a known alias is returned unchanged, so the existing
    validation (enum check, the UI's own narrowing) still decides what an
    unknown value becomes.
    """
    if isinstance(raw, str):
        return LEGACY_VERBOSITY_ALIASES.get(raw, raw)
    return raw


@dataclass
class DashboardConfig:
    url: str = field(
        default="",
        metadata=_meta(
            "Dashboard URL",
            "Public URL for the dashboard (used in Slack links).",
            restart=True,
        ),
    )
    tailscale: TailscaleConfig = field(
        default_factory=TailscaleConfig,
        metadata=_meta(
            "Tailscale",
            "Reach the dashboard over your tailnet via `tailscale serve`.",
        ),
    )
    restore_sessions: bool = field(
        default=False,
        metadata=_meta(
            "Restore Sessions",
            "Re-open recently active sessions on startup.",
            restart=True,
        ),
    )
    dynamic_dashboard_cards: bool = field(
        default=False,
        metadata=_meta(
            "Automatic session cards",
            "Use the background model to update session-owned HTML cards after activity. "
            "Off by default. One call at a time, at most one per session every two minutes "
            "and 60 per gateway hour. Live status and native decisions work without it.",
        ),
    )
    crewmate_threads: bool = field(
        default=False,
        metadata=_meta(
            "Reply threads on mate chat messages",
            "Let any message in a mate's chat carry its own reply thread, "
            "opened in the side panel while the main chat stays visible. Off by "
            "default: the thread routes answer not-found, no thread frame is sent, "
            "and the dashboard draws no Reply in thread control. Takes effect on "
            "the next request; no restart.",
        ),
    )
    crewmates_in_agent_picker: bool = field(
        default=False,
        metadata=_meta(
            "Mates in the chat agent picker",
            "List mates in the chat composer's agent picker, beside the agent "
            "templates, so a chat can be switched onto a mate (and its own "
            "workspace and memory) without opening it from the Crew page. Off by "
            "default: the picker lists templates plus any mate no listed "
            "template already covers; the default agent stays even when covered, "
            "unless a template shares its name. Takes effect when the dashboard is "
            "reloaded; no gateway restart.",
        ),
    )
    qr_session_until_restart: bool = field(
        default=True,
        metadata=_meta(
            "Phone Sign-In Lasts Until Restart",
            "Keep a phone signed in for as long as this gateway process runs. "
            "The QR code still has to be scanned within its short window; after "
            "that the phone is not signed out for being idle in ordinary use, "
            "and a gateway restart signs it out. The one remaining idle limit is "
            "the refresh credential's own 30-day lifetime, which each visit "
            "renews, so a phone that goes untouched for 30 days re-scans. Turn "
            "this OFF to go back to a timed session that expires on a clock "
            "whether or not the gateway is still running. Either way `kirocrew "
            "logout` ends the session immediately, and the session stays pinned "
            "to the peer it was established from.",
        ),
    )
    qr_session_persist_across_restart: bool = field(
        default=False,
        metadata=_meta(
            "Phone Sign-In Survives A Gateway Restart",
            'REQUIRES BOTH: "Phone Sign-In Lasts Until Restart" must also be ON, '
            "and tailnet identity trust must be configured "
            "(`dashboard.tailscale.trust_identity` with a non-empty "
            "`allowed_logins`). Without either one this setting is ignored and a "
            "warning naming the missing prerequisite is logged. Note the first "
            'requirement is NOT a contradiction: "Lasts Until Restart" is what '
            "issues the renewable credential, and this setting then removes the "
            "restart bound from it -- turning that one OFF instead leaves a "
            "session that expires on a fixed clock, with nothing to renew. "
            "What it does: let a scanned phone stay signed in across gateway "
            "restarts, so one scan lasts until the refresh credential's own "
            "30-day lifetime lapses. OFF by default because a restart is "
            "otherwise a hard sign-out that needs no recorded state. The "
            "identity requirement is not optional bookkeeping: behind "
            "`tailscale serve` every request reaches the gateway from 127.0.0.1, "
            "so without a daemon-verified peer identity the session is a bearer "
            "credential any tailnet peer could replay, and outliving the process "
            "is exactly what makes that matter.",
        ),
    )
    restore_window_minutes: int = field(
        default=30,
        metadata=_meta(
            "Restore Window Minutes",
            "Time window (minutes) for session restoration, and for surfacing "
            "channel conversations in the chat list (0-1440). 0 = no limit.",
            restart=True,
        ),
    )
    idle_slot_sweep_days: int = field(
        default=7,
        metadata=_meta(
            "Archive Idle Sessions After Days",
            "When 450 or more sessions are open, archive sessions idle for this many "
            "days to history, hourly, and post a notification. Pinned sessions, app "
            "sessions, the open session, sessions with a running turn, a pending "
            "approval or sub-agents, crew worker sessions and sessions an auto-nudge "
            "loop drives are kept. 0 = never.",
            restart=True,
        ),
    )
    surface_channel_sessions: bool = field(
        default=True,
        metadata=_meta(
            "Show Channel Conversations In Chat List",
            "Show recently active Slack/Discord/Teams (etc.) conversations in the "
            "dashboard's chat list instead of only under History. Uses the same "
            "recency window as session restoration.",
            restart=True,
        ),
    )
    bot_name: str = field(
        default="",
        metadata=_meta(
            "Bot Name",
            "Custom bot display name for the dashboard UI.",
        ),
    )
    avatar: str = field(
        default="",
        metadata=_meta(
            "Avatar",
            "Path to custom avatar image for the dashboard UI.",
        ),
    )
    merge_queued_messages: bool = field(
        default=False,
        metadata=_meta(
            "Merge Queued Messages",
            "Concatenate follow-up messages while the agent is busy instead of queueing them separately.",
        ),
    )
    title_refresh_every_turns: int = field(
        default=0,
        metadata=_meta(
            "Refresh Auto Title Every N Turns",
            "Re-examine a session's auto-generated title every N user turns "
            "(at turns N, 2N, 3N, ...) from its last ten messages, and rename the "
            "session when the topic has moved. 0 keeps the built-in schedule: "
            "turns 8 and 24 only. Either way, a title that began as a bare link "
            "or ticket key also gets one refresh after the first turn. A value "
            "from 1 to 3 is raised to 4, and the ceiling is 1000. Each refresh is "
            "one background LLM call. Turns are counted over the messages held "
            "for the session. A session reloaded by a gateway restart or "
            "reopened from History holds its latest 500 plus the new ones, and "
            "the cadence continues from the restored count; if the reload keeps "
            "fewer user turns than the latest built-in turn the session had "
            "already reached (the first turn, 8 or 24), the cadence resumes "
            "after that turn instead. Once 10,000 are held, the oldest drop off "
            "as new ones arrive, so refreshes slow down or stop until the next "
            "reload. A title you renamed by hand is never refreshed. Takes "
            "effect on the next turn; no restart.",
        ),
    )
    title_ticket_prefix: bool = field(
        default=False,
        metadata=_meta(
            "Lead Auto Titles With The Ticket Key",
            "When a session's opening message names a ticket, such as PROJ-1234 "
            "or #123, its auto-generated title starts with that key: "
            '"PROJ-1234: Increase regression shards". The first key in the '
            "opening message is used, and the title is unchanged when there is "
            "none. Names like SHA-256 or ISO-8601 have the same shape and are read "
            "as keys too, which is why this is off by default. A title you renamed "
            "by hand is never changed. Takes effect on the next title; no restart.",
        ),
    )
    upload_max_mb: int = field(
        default=100,
        metadata=_meta(
            "Upload Size Limit (MB)",
            "Largest file, in megabytes, the dashboard accepts from the chat "
            "composer and the Knowledge upload (1-512). The Knowledge upload "
            "is also bounded by knowledge.max_ingest_file_mb. Video keeps its own "
            "512 MB ceiling. A chat upload is held in memory while its type is "
            "checked, so a large value costs gateway memory per upload. Takes "
            "effect on the next upload; no restart.",
        ),
    )
    mcp_probe_timeout_secs: int = field(
        default=15,
        metadata=_meta(
            "MCP Probe Timeout",
            "Seconds to wait for MCP server handshake during probe (5-120).",
        ),
    )
    loop_stall_exit_after_secs: int | None = field(
        default=None,
        metadata=_meta(
            "Loop-stall Hard-exit Budget (secs)",
            "Seconds the gateway's event loop may go silent before it dumps all "
            "thread stacks and exits. Leave unset for the automatic default: "
            "25 seconds for desktop/foreground launches and 90 seconds for a "
            "managed systemd/launchd service. An explicit value overrides both. "
            "Raise it on a host that does heavy subprocess work (long builds, "
            "test suites, many child reaps), which can wedge the loop briefly "
            "without being genuinely dead. Clamped to 10s..300s. The desktop app's "
            "liveness probe kills at roughly 20s independently, so a value "
            "above that only takes effect for a headless gateway — the desktop "
            "probe wins first and the stack dump is lost.",
            restart=True,
        ),
    )
    chat_entry_cache_max_entries: int = field(
        default=CHAT_ENTRY_CACHE_ENTRIES_DEFAULT,
        metadata=_meta(
            "Chat Entry Cache Max Entries",
            "Maximum number of persisted-message entries the chat save path "
            "memoises. The cache's working set is roughly the number of active "
            "chat slots times their window size, so the right bound is "
            "host-dependent: a gateway with many concurrent slots overflows "
            "this bound while the byte ceiling still has headroom, and the "
            "cache hit rate collapses to zero (every save re-pays redaction). "
            "Raise it on a many-slot host. Clamped to 256..262144. Applies to "
            "the next chat save; no restart needed.",
        ),
    )
    chat_entry_cache_max_bytes: int = field(
        default=CHAT_ENTRY_CACHE_BYTES_DEFAULT,
        metadata=_meta(
            "Chat Entry Cache Max Bytes",
            "Memory ceiling in bytes for the chat save path's persisted-message "
            "entry memo. Evicted alongside the entry-count bound; raise it "
            "together with the entry bound when a many-slot host needs a "
            "larger cache. Clamped to 4 MiB..512 MiB. Applies to the next chat "
            "save; no restart needed.",
        ),
    )
    cautious_boot: bool = field(
        default=True,
        metadata=_meta(
            "Cautious Boot After Crash",
            "When the gateway starts and finds a recent loop-stall crash dump "
            "(under 30 minutes old) from the previous instance, stagger the "
            "startup burst — MCP servers, cron scheduler, app backends, "
            "session restores — with short pauses instead of launching "
            "everything at once, so a host still under memory pressure is "
            "not pushed straight back into the same collapse.",
            restart=True,
        ),
    )
    default_memory_mode: str = field(
        default="persistent",
        metadata=_meta(
            "Default Memory Mode",
            "Memory mode for new dashboard chat sessions. 'persistent' reads "
            "and writes memory; 'incognito' reads but does not write; "
            "'temporary' neither reads nor writes. Explicit per-session choices "
            "still win.",
            enum=["persistent", "incognito", "temporary"],
        ),
    )
    widget_density: str = field(
        default="more",
        metadata=_meta(
            "Widget Density",
            "How aggressively the agent uses inline widgets. "
            "'more' encourages widgets for any visual content; "
            "'less' limits to only when markdown is clearly insufficient.",
            enum=["more", "less"],
        ),
    )
    use_builtin_browser: bool = field(
        default=True,
        metadata=_meta(
            "Use Built-in Browser",
            "When on, the browser tool opens pages in Kiro Crew's built-in panel "
            "(desktop app only). When off, the agent browses via playwright-cli.",
        ),
    )
    browser_local_origins: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Built-in Browser Local Origins",
            "Loopback origins the browser tool may open in the built-in panel, "
            "each written host:port with host localhost, 127.0.0.1 or [::1] "
            "(e.g. localhost:5173 for a Vite dev server). Empty (the default) "
            "keeps every loopback, private and link-local address refused. The "
            "port is required and matched exactly, so one entry opens one dev "
            "server and never the rest of the machine; the gateway's own port is "
            "refused even when listed. Entries in any other shape are dropped. "
            "The agent cannot set this while its sandbox is on: config.json is "
            "read-only inside the sandbox.",
        ),
    )
    browser_view_port: int = field(
        default=0,
        metadata=_meta(
            "Browser Live-View Port",
            "Pin the browser live-view server (playwright-cli show) to this "
            "loopback port. 0 (the default) picks a fresh OS-assigned ephemeral "
            "port on every start. Set a fixed port when the dashboard is viewed "
            "remotely through an SSH tunnel that forwards a fixed set of ports, "
            "so the Browser panel can always reach the view. The server binds "
            "loopback-only either way. A value outside 1-65535 is treated as "
            "unset. A changed pin applies the next time the view server "
            "(re)starts; an already-running server keeps its current port.",
        ),
    )
    verbosity: str = field(
        # A fresh install starts terse. An existing config already carries the
        # key (every full save writes the whole section), so it keeps its level.
        default="answer_only",
        metadata=_meta(
            "Response Verbosity",
            "Controls how terse the agent's prose is. New installs start at "
            "'answer_only'. 'default' is normal length; "
            "'concise' injects brevity guidelines (lead with the answer, cut "
            "filler, keep code/errors verbatim); "
            "'answer_only' drops explanation altogether — the answer alone, drawn "
            "as a picture when it has a shape, in sentences of at most twelve "
            "plain words; detail only when the user asks for it, plus one undo "
            "line for a destructive command and one risk line for anything "
            "touching security, data or spend. At every level security warnings and "
            "irreversible-action confirmations always appear but stay brief, "
            "and ordered multi-step instructions stay complete.",
            enum=list(VERBOSITY_LEVELS),
        ),
    )
    link_previews: bool = field(
        default=False,
        metadata=_meta(
            "Link Previews",
            "Render http(s) links in assistant messages as favicon + page title "
            "instead of a raw URL. Off by default because it is a network "
            "decision, not a display one: this machine fetches every link the "
            "model outputs, so each linked site sees a request from your IP "
            "address. When false the /api/link-meta endpoint fetches nothing and "
            "returns 403.",
        ),
    )
    tail_fork_enabled: bool = field(
        default=False,
        metadata=_meta(
            "Tail-only Fork",
            "When forking, keep only the messages after the chosen point. The "
            "earlier messages are dropped.",
        ),
    )
    auto_open_browser: bool = field(
        default=True,
        metadata=_meta(
            "Auto Open Browser",
            "Open the dashboard URL in the default browser on gateway startup.",
            restart=True,
        ),
    )
    prevent_sleep: bool = field(
        default=False,
        metadata=_meta(
            "Prevent Sleep While Running",
            "Keep this computer awake while the agent is running a task, so a long "
            "task is not interrupted by the machine going to sleep. Off by default. "
            "Uses caffeinate on macOS, systemd-inhibit on Linux, and "
            "SetThreadExecutionState on Windows; on a host with no keep-awake "
            "backend it is a no-op.",
        ),
    )
    quick_send: bool = field(
        default=False,
        metadata=_meta(
            "Quick Send",
            "Click a suggested reply to send it instantly. Shift+Click to select multiple.",
        ),
    )
    model_picker_configured: bool = field(
        default=False,
        metadata=_meta(
            "Model Picker Visibility Saved",
            "Internal marker set after the user saves the interactive model "
            "picker visibility list. It lets the dashboard distinguish a "
            "never-configured picker from one intentionally saved with no "
            "hidden models.",
        ),
    )
    model_picker_hidden_models: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Selectable Models",
            "Model IDs hidden from the interactive chat model picker. Empty shows "
            "every advertised model; 'auto' is always shown. This preference does "
            "not change entitlement, provider model discovery, defaults, role "
            "models, fallback models, bulk switching, or app-specific model lists.",
        ),
    )
    session_grid: bool = field(
        default=False,
        metadata=_meta(
            "Session Grid (Split View)",
            "Opt-in: enable terminal-style split view to run multiple chat sessions side by side.",
        ),
    )
    mcp_app_panel: bool = field(
        default=False,
        metadata=_meta(
            "Open MCP Apps in the side panel",
            "Render interactive MCP Apps (such as Excalidraw diagrams) in the right "
            "side panel instead of inline in the chat bubble. The panel opens "
            "automatically and can be expanded; the chat keeps a compact "
            "placeholder linking to it.",
        ),
    )
    # Off by default because the panel's dismissal marker is keyed by slot and a
    # new session inherits `dashboard.default_project`: with this on, every new
    # chat in a git project opens the panel, which is not the once-per-project
    # nudge the behaviour looks like. That reasoning is the flag's rationale, not
    # something a user reading the setting needs, so it stays out of `help`.
    auto_open_git_panel: bool = field(
        default=False,
        metadata=_meta(
            "Auto-open Git in the side panel",
            "Expand the chat's right side panel to its Git tab each time a session "
            "starts in a project directory that is a git repository. The Git tab "
            "itself is always created either way, so it is one click away.",
        ),
    )
    side_panel_always_open: bool = field(
        default=False,
        metadata=_meta(
            "Keep the side panel open",
            "Open the chat's right side panel in every chat you open, new or "
            "existing, and after a reload. Closing it hides it until you switch "
            "chats. Each chat still remembers its own open or closed panel, and "
            "turning this off brings that back. This setting does not apply on a "
            "phone.",
        ),
    )
    side_panel_pin_summary: bool = field(
        default=False,
        metadata=_meta(
            "Pin Summary in the side panel",
            "Show the session Summary as the first side panel tab, which cannot be "
            "closed, and show it each time the panel opens. A panel opened for "
            "something specific, such as a file, still shows that. Works only "
            "while session summaries are on.",
        ),
    )
    # Default TRUE: the chip strip shipped unconditionally before this switch
    # existed, so a config that never mentions the key must render exactly what
    # it rendered before.
    session_card_source_links: bool = field(
        default=True,
        metadata=_meta(
            "PR and issue chips on session cards",
            "Show a chip on a session's sidebar card for each pull request, merge "
            "request and issue mentioned anywhere in that session's transcript. "
            "Turning this off reclaims a row per card on the densest surface in "
            "the app, keeps numbers from unrelated work off screen while sharing "
            "it, and stops the periodic credentialed provider calls that keep "
            "those chips' CI and merge status fresh. The in-session Resources and "
            "Changes panels are unaffected.",
        ),
    )
    terminal: dict = field(
        default_factory=lambda: {"enabled": True},
        metadata=_meta(
            "Terminal",
            "Terminal panel configuration. Set enabled=false to hide the CLI panel in the dashboard.",
            # Declared sub-keys become first-class schema entries
            # (dashboard.terminal.<key>) so Settings controls can reference
            # them by configKey and `kirocrew config set` accepts them on a
            # config that has never written one (the CLI's key check consults
            # SCHEMA_REGISTRY - see cli_config._declared_entry). `enabled` needs
            # no declaration for that: it rides on the default_factory below, so
            # it is always in the document that check walks. The field stays a
            # plain dict, so a key added by a future release still round-trips
            # untouched via additionalProperties.
            properties={
                "shell": {
                    "type": "string",
                    "default": "",
                    "x-meta": {
                        "label": "Default shell",
                        "help": (
                            "Shell the built-in terminal launches — an absolute path or a "
                            "command on PATH. Empty = the system default ($SHELL)."
                        ),
                    },
                },
                "max_sessions": {
                    "type": "integer",
                    "default": 12,
                    "x-meta": {
                        "label": "Max terminal sessions",
                        "help": (
                            "Ceiling on concurrent terminal sessions across every chat, "
                            "server-wide. Each chat's activity bar caps its own terminals "
                            "below this; a session beyond the ceiling is refused."
                        ),
                    },
                },
                "cwd": {
                    "type": "string",
                    "default": "",
                    "x-meta": {
                        "label": "Default working directory",
                        "help": (
                            "Directory a terminal opens in when the chat passes no project "
                            "directory of its own. Empty = $HOME."
                        ),
                    },
                },
                "reuse_current": {
                    "type": "boolean",
                    "default": False,
                    "x-meta": {
                        "label": "Reuse the current terminal",
                        "help": (
                            "Run-in-terminal focuses the terminal tab you have "
                            "selected and copies the command, so you can paste it into "
                            "that shell and keep its state (working directory, "
                            "environment, an active login session). With no terminal "
                            "open, it still copies the command for you to paste — it is "
                            "never run for you. Off = a fresh terminal each time."
                        ),
                    },
                },
                # The choices are EXTERNAL_TERMINAL_APPS above, the desktop
                # shell's launch allowlist. Unset by default, and any other
                # value reads as unset where it is used.
                "external_app": {
                    "type": "string",
                    "default": "",
                    "enum": ["", *EXTERNAL_TERMINAL_APPS],
                    "x-meta": {
                        "label": "External terminal app",
                        "help": (
                            "Desktop app only, on the machine running the gateway: adds "
                            '"Open in <your terminal app>" to the Files tab\'s Project '
                            "actions menu, which opens this app in the project directory. "
                            "Empty = no external terminal; the built-in terminal is "
                            "unaffected either way."
                        ),
                    },
                },
                "completion": {
                    "type": "object",
                    "additionalProperties": True,
                    "x-meta": {
                        "label": "Command completion",
                        "help": "The Terminal tab's inline completion popup.",
                    },
                    "properties": {
                        "enabled": {
                            "type": "boolean",
                            "default": True,
                            "x-meta": {
                                "label": "Command completion",
                                "help": (
                                    "Show the completion popup while typing in the "
                                    "Terminal tab. Off = no popup; the shell's own Tab "
                                    "completion still works."
                                ),
                            },
                        },
                        # Left open like `completion` itself: a typed
                        # `additionalProperties` would flatten into a
                        # `commands.*` registry entry, which is not reachable
                        # from the dataclass hierarchy the schema mirrors. The
                        # values are protocol names and a value that is not one
                        # is ignored by `terminal_commands.protocol_for`.
                        "commands": {
                            "type": "object",
                            "additionalProperties": True,
                            "default": {},
                            "x-meta": {
                                "label": "Completion protocol overrides",
                                "help": (
                                    "Re-point an already-allowlisted command at a different "
                                    'completion protocol, e.g. {"docker": "cobra"}. It can '
                                    "only change the protocol of a command the release "
                                    "already knows - it cannot add one, because the "
                                    "allowlist is the set of tools whose probe argv is "
                                    "known to be inert."
                                ),
                            },
                        },
                    },
                },
            },
        ),
    )
    default_project: str = field(
        default="",
        metadata=_meta(
            "Default Project",
            "Directory path used as the project for new chat tabs. Empty = workspace dir.",
        ),
    )
    # Off while empty. The root is re-validated each time a folder is made under
    # it, so a hand-edited value that names a sensitive or missing directory
    # falls back to the default project instead of being trusted.
    session_folder_root: str = field(
        default="",
        metadata=_meta(
            "New Session Folders",
            "Directory under which each new dashboard chat gets its own freshly "
            "created folder, used as that chat's project directory. The folder is "
            "named by date with a short random suffix and only you can read it. "
            "A chat filed into a folder that has its own project keeps that "
            "project. Empty = off; new chats use the default project.",
        ),
    )
    # A folder ID, not a name or path, so renaming or moving the folder keeps it.
    # Read at each create and ignored when the folder is gone, so a deleted or
    # hand-edited value leaves new chats unfiled instead of refusing them.
    default_session_folder: str = field(
        default="",
        metadata=_meta(
            "Default Sidebar Folder for New Chats",
            "ID of the sidebar folder that new dashboard chats are filed into when "
            "they are started from New without choosing a folder. A chat started in "
            "a specific folder still goes there, and every other create path, such "
            "as apps, crons, channels and agents, keeps its own placement. Empty = "
            "new chats are unfiled.",
        ),
    )
    theme_mode: str = field(
        default="",
        metadata=_meta(
            "Theme Mode",
            "Dashboard color mode preference: 'dark', 'light', or 'system'. "
            "Empty = unset (frontend falls back to localStorage or 'system').",
            enum=["", "dark", "light", "system"],
        ),
    )
    sso_login_flags: str = field(
        default="",
        metadata=_meta(
            "SSO Login Flags",
            "Flags passed to the SSO login command by an edition that supplies a "
            "real login handler (DashboardContributor.sso_login_handler). Empty = "
            "the edition default. Inert in the public build (the core /api/sso-login "
            "is a no-op stub); the companion validates the token allowlist when it "
            "uses them.",
        ),
    )
    theme_color: str = field(
        default="",
        metadata=_meta(
            "Theme Color",
            "Dashboard color theme slug (e.g. 'kiro', 'emerald', 'monokai'). "
            "Empty = unset (frontend falls back to localStorage or 'kiro').",
        ),
    )
    language: str = field(
        default="",
        metadata=_meta(
            "Language",
            "Dashboard UI language as a BCP-47 tag (e.g. 'en', 'zh-CN'). "
            "Empty = auto-detect from the browser's preferred languages, "
            "falling back to English. Persisted here (not only in the browser) "
            "so the choice follows the user across browsers and the desktop app.",
        ),
    )
    recent_tint_count: int = field(
        default=0,
        metadata=_meta(
            "Recent Session Tint Count",
            "Number of most-recently-active sessions to highlight in the sidebar with a "
            "graded accent stripe (0-10; 0 = off).",
        ),
    )
    # Literal enum rather than FOLDER_SORT_MODES: that constant lives in
    # sections_limits with the other write/load bounds, and this field spells the
    # same list. test_config_patch.py::TestFolderSortRoundTrip::
    # test_the_allowlist_enum_is_the_loader_list_spelled_once pins the two
    # spellings equal.
    folder_sort: str = field(
        default="custom",
        metadata=_meta(
            "Sidebar Folder Order",
            "How the chat sidebar orders session folders: 'custom' keeps the stored "
            "positions (set by dragging a folder or by chat_folder_move), 'name' is an "
            "ASCII-case-insensitive natural order (01. < 02. < 10.; A-Z fold to a-z, "
            "other letters compare as written), 'created' is newest "
            "first. A view preference only -- choosing a mode never rewrites the "
            "stored positions, so switching back to 'custom' restores them exactly. "
            "Read by the sidebar and by chat_folder_tree, which lists folders in the "
            "order the sidebar draws them.",
            enum=["custom", "name", "created"],
        ),
    )
    # Literal enum for the same reason as folder_sort; pinned equal to
    # SESSION_SORT_MODES by test_config_patch.py::TestSessionSortRoundTrip.
    session_sort: str = field(
        default="date-desc",
        metadata=_meta(
            "Sidebar Session Order",
            "How the chat sidebar orders sessions: 'date-desc' / 'date-asc' by last "
            "activity, 'created-desc' / 'created-asc' by creation time, 'name-asc' / "
            "'name-desc' by title, 'manual' in the order the user drags them into. A "
            "view preference only; pinned sessions keep their own order. Stored here "
            "(not only in the browser) so the choice follows the user across browsers "
            "and the desktop app.",
            enum=[
                "date-desc",
                "date-asc",
                "created-desc",
                "created-asc",
                "name-asc",
                "name-desc",
                "manual",
            ],
        ),
    )
    update_nudge: dict = field(
        default_factory=dict,
        metadata=_meta(
            "Update Nudge",
            "Per-version state for the proactive update popup. Written by the "
            "dashboard when the user snoozes or skips a release; a record only "
            "suppresses the popup for the version it names. Validated as one "
            "atomic record by the PATCH allowlist (dashboard.update_nudge); "
            "no Settings control reads it, so it carries no schema properties.",
        ),
    )
    onboarded: bool = field(
        default=False,
        metadata=_meta(
            "Onboarded",
            "Whether the user has completed the dashboard onboarding flow. "
            "When true, the 'Choose your look' modal is skipped on first load.",
        ),
    )
    import_onboarded: bool = field(
        default=False,
        metadata=_meta(
            "Import Onboarded",
            "Whether the user has completed or skipped foreign-agent import onboarding.",
        ),
    )
    privacy_acked: bool = field(
        default=False,
        metadata=_meta(
            "Privacy Acknowledged",
            "Whether the user has seen the mandatory first-run Privacy chapter, which "
            "discloses the anonymous heartbeat and offers the opt-out. Server-backed "
            "rather than browser-local because the gateway gates the very FIRST "
            "heartbeat on it: until this is true the user has not yet been shown the "
            "opt-out, and a ping sent before the offer makes the offer meaningless.",
        ),
    )
    crewmates_onboarded: bool = field(
        default=False,
        metadata=_meta(
            "Mates Onboarded",
            "Whether the user has finished or dismissed the first-run Meet Mates "
            "flow (the four-step introduction that creates the first mate). "
            "Server-backed like the other first-run flags so a second machine does "
            "not replay it. Also set when the flow is re-run from the Mates page.",
        ),
    )
    user_role: str = field(
        default="",
        metadata=_meta(
            "User Role",
            "The user's professional background, collected during onboarding "
            "(developer, designer, product-manager, data-ml, it-ops, other). "
            "Injected into the agent prompt so responses match the user's "
            "domain vocabulary. Empty = unspecified.",
        ),
    )
    user_role_other: str = field(
        default="",
        metadata=_meta(
            "User Role (Custom)",
            "Free-text role the user typed when they picked 'other' during "
            "onboarding (e.g. 'solutions architect'). Consulted ONLY while "
            "user_role == 'other'; quoted verbatim into the agent prompt. "
            "Retained (not cleared) when another role is picked, so it is "
            "inert rather than contradictory and survives switching back. "
            "Empty = 'other' contributes nothing.",
        ),
    )
    user_technical_level: str = field(
        default="",
        metadata=_meta(
            "User Technical Level",
            "How technical the user is (codes, somewhat-technical, non-technical), "
            "collected during onboarding. Injected into the agent prompt to "
            "calibrate explanation depth. Empty = unspecified.",
        ),
    )
    tips_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Tips Enabled",
            "Show feature tip cards while the agent is thinking.",
        ),
    )
    feature_videos_enabled: bool = field(
        default=False,
        metadata=_meta(
            "Feature Videos Enabled",
            "Show short feature-intro clips for features this install has not used "
            "yet. Instance-wide kill switch.",
        ),
    )
    feature_videos_cache_max_mb: float = field(
        default=500.0,
        metadata=_meta(
            "Feature Videos Cache Size (MB)",
            "Disk budget for downloaded clips. Whole release folders are evicted "
            "oldest-first to fit; the running release is never evicted. 0 = no cap.",
        ),
    )
    folder_suggestions_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Folder Suggestions Enabled",
            "Offer to file a newly-titled, unfiled chat session into a matching folder.",
        ),
    )
    topic_tags_enabled: bool = field(
        default=False,
        metadata=_meta(
            "Topic Tags Enabled",
            "Ask the background model for up to two topic tags from a new chat "
            "session's first message. A session never gets more than three tags "
            "this way. Off by default because each new session costs one model call.",
        ),
    )
    tips_cadence_hours: float = field(
        default=6.0,
        metadata=_meta(
            "Tips Cadence Hours",
            "Minimum hours between showing a new tip.",
        ),
    )
    tips_snooze_hours: float = field(
        default=48.0,
        metadata=_meta(
            "Tips Snooze Hours",
            "Hours before a snoozed tip becomes eligible again.",
        ),
    )
    tips_recency_decay: float = field(
        default=0.6,
        metadata=_meta(
            "Tips Recency Decay",
            "Decay factor for weighted-random selection (0-1). Lower = stronger bias to newer tips.",
        ),
    )
    tips_model: str = field(
        default="auto",
        metadata=_meta(
            "Tips Model",
            'Model ID for tips generation. Defaults to "auto" so it inherits the '
            "account's governed model; a hardcoded id can be rejected on accounts "
            "or partitions that do not serve it.",
        ),
    )
    tips_explore_ratio: float = field(
        default=0.2,
        metadata=_meta(
            "Tips Explore Ratio",
            "Probability of picking a random catalog tip instead of personalized (0-1). Higher = more general discovery.",
        ),
    )
    github_hosts: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "GitHub Enterprise Hosts",
            "Exact hostnames of GitHub Enterprise Server instances that gh-backed "
            "features may reach: the Changes and Issues panels and session source "
            "links. "
            "Empty = github.com only (deny-by-default): a pull-request or issue "
            "URL on any other host is only sent to the gh CLI if its host is an "
            "exact member of this list. Suffixes, wildcards and ports are not "
            "accepted (gh cannot address a host on a non-default port). "
            "Each listed host authenticates only from its own `gh auth login "
            "--hostname <host>` entry; the github.com token is never sent to it. "
            "A host that is also listed in gitlab_hosts or jira_hosts is refused. "
            "The Code Review Sage app reads its own github_hosts list from its "
            "app config, not this one; list a host in both to use it in both.",
        ),
    )
    gitlab_hosts: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Self-Hosted GitLab Hosts",
            "Exact hostnames (optionally host:port) of self-managed GitLab "
            "instances that glab-backed features may reach: the Changes panel, "
            "session source links, the monitor's merge-request path and Issue "
            "Radar. "
            "Empty = gitlab.com only (deny-by-default): a merge-request URL is "
            "only sent to the glab CLI if its host is an exact member of this "
            "list, so a pasted link cannot aim the credential-bearing CLI at an "
            "arbitrary or internal host. Suffixes and wildcards are not matched. "
            "Adding an entry authorizes the local glab CLI, with its token, to "
            "reach that host, including hosts only resolvable on your network.",
        ),
    )
    jira_hosts: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Self-Hosted Jira Hosts",
            "Exact hostnames (optionally host:port) of self-managed Jira or "
            "Jira Data Center instances whose issue URLs the Issues panel may "
            "recognize. Atlassian Cloud instances (*.atlassian.net) are always "
            "accepted without listing. Empty = Cloud-only (deny-by-default): a "
            "Jira issue URL is only recognized if its host matches an entry "
            "here. Suffixes and wildcards are not matched.",
        ),
    )
    bitbucket_server_hosts: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Bitbucket Server Hosts",
            "Exact hostnames (optionally host:port) of Bitbucket Server or "
            "Bitbucket Data Center instances whose pull-request URLs the "
            "Changes panel and session source links may read, read-only, over "
            "the instance's REST API. Empty = none (deny-by-default): a "
            "/projects/KEY/repos/SLUG/pull-requests/N URL is only fetched when "
            "its host is an exact member of this list. Suffixes and wildcards "
            "are not matched, and bitbucket.org is never a Bitbucket Server "
            "host. A host that is also listed in gitlab_hosts, jira_hosts or "
            "github_hosts is refused. The gateway sends an HTTP access token "
            "only to the listed host it belongs to: the vault secret "
            "BITBUCKET_SERVER_TOKEN serves a single listed host, and "
            "BITBUCKET_SERVER_TOKEN_<HEX> (the host's hex encoding, as the "
            "panel's setup message names it) serves one host of several. "
            "Without a token the read is anonymous.",
        ),
    )
    gerrit_hosts: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Gerrit Hosts",
            "Exact hostnames (optionally host:port) of Gerrit Code Review "
            "instances whose change URLs the Changes panel and session source "
            "links may read, read-only, over the instance's REST API. Empty = "
            "none (deny-by-default): a /c/<project>/+/<number> URL is only "
            "fetched when its host is an exact member of this list. Suffixes "
            "and wildcards are not matched. A host that is also listed in "
            "gitlab_hosts, jira_hosts, github_hosts or bitbucket_server_hosts "
            "is refused. Gerrit refuses anonymous REST reads on most instances, "
            "so the gateway authenticates with the HTTP credential of the "
            "listed host only: the vault secret GERRIT_HTTP_CREDENTIAL, holding "
            "<username>:<HTTP password> as generated under the Gerrit account's "
            "HTTP Credentials settings, serves a single listed host, and "
            "GERRIT_HTTP_CREDENTIAL_<HEX> (the host's hex encoding) serves one "
            "host of several. Settings > Secrets offers one labelled row per "
            "listed host, so the name is never typed by hand.",
        ),
    )
    jira_auth: list[JiraAuthEntry] = field(
        default_factory=list,
        metadata=_meta(
            "Jira Authentication",
            "Per-host credentials for the Jira REST API so the Issues panel "
            "can fetch issue details inline. Each entry pairs a host with an "
            "API token. Atlassian Cloud (*.atlassian.net) uses email + API "
            "token (Basic auth); Jira Server/Data Center uses a Personal "
            "Access Token (Bearer). When no entry matches the issue host, the "
            "panel falls back to the link-out 'Open in Jira' behavior.",
        ),
    )
    link_patterns: list[LinkPatternRule] = field(
        default_factory=list,
        metadata=_meta(
            "Transcript Link Patterns",
            "Rewrite matching plain text in chat transcripts into clickable "
            "links at display time (e.g. ticket ids like PROJ-123 to your "
            "tracker). Each rule pairs a JavaScript regex with an absolute "
            "http(s) URL template in which '{match}' inserts the matched "
            "text, percent-encoded. Rendering-only: stored messages never change. Text "
            "inside code blocks, existing links, and raw HTML is not rewritten; "
            "an inline code span whose whole text matches becomes a link chip.",
        ),
    )
