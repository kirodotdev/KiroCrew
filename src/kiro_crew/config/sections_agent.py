"""The ``agent`` section DTO, and the readers that resolve against its declared defaults.

An owner of the :mod:`kiro_crew.config.sections` facade; it never imports the facade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from kiro_crew import model_registry
from kiro_crew.acp_backends import DEFAULT_ACP_BACKEND
from kiro_crew.config.fields import _meta, field_default
from kiro_crew.config.sections_agent_env import (
    coerce_agent_env,
    coerce_child_env_defaults,
    coerce_deepseek_env,
)
from kiro_crew.config.sections_agent_fields import (
    _VALID_JAIL_MODES,
    _YOLO_DURATION_SECS,
    DEFAULT_CWD_ALLOWED_ROOTS,
    JAIL_MODE_AUTO,
    KAS_BUNDLE_DEFAULT_URL,
    YOLO_UNTIL_SHUTDOWN,
    coerce_model_context_tokens,
    coerce_refusal_fallback_model,
    coerce_role_efforts,
    coerce_role_models,
    normalize_agent_model,
)
from kiro_crew.config.sections_defaults import DEFAULT_MODEL
from kiro_crew.constants import DEFAULT_SPAWN_MIN_MEMORY_GB as _DEFAULT_SPAWN_MIN_MEMORY_GB
from kiro_crew.constants import DEFAULT_SUBAGENT_COST_GB as _DEFAULT_SUBAGENT_COST_GB
from kiro_crew.constants import DEFAULT_SUBAGENT_MAX_TURNS as _DEFAULT_SUBAGENT_MAX_TURNS
from kiro_crew.constants import (
    DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS as _DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS,
)
from kiro_crew.constants import SUBAGENT_TIMEOUT_MAX as _SUBAGENT_TIMEOUT_MAX
from kiro_crew.constants import SUBAGENT_TIMEOUT_MIN as _SUBAGENT_TIMEOUT_MIN
from kiro_crew.constants import SUBAGENT_TIMEOUT_SECS as _SUBAGENT_TIMEOUT_SECS
from kiro_crew.effort import EFFORT_LEVELS

logger = logging.getLogger("kiro_crew.config.loader")


def coerce_fallback_model(raw: object) -> str:
    """Normalize the throttle-fallback model (agent.fallback_model).

    Single value with three shapes: ``"auto"`` (the default — defer to the
    backend's availability-aware routing when the active model stays
    throttled), ``""`` (feature explicitly disabled: fail loudly, pre-feature
    behavior), or a concrete model id normalized through
    :func:`model_registry.to_provider_id` for the ``acp`` provider (registry
    canonical keys and aliases land as the kiro-cli id the wire needs;
    unregistered ids pass through unchanged — existing registry behavior).
    Absent/junk input (``None``, non-string) collapses to the field's
    ``"auto"`` default. ``"auto"`` is matched case-insensitively; an unregistered
    id that the registry maps to ``""`` also collapses to that default rather than
    silently disabling the feature.
    """
    if raw is None or not isinstance(raw, str):
        return field_default(AgentConfig, "fallback_model")
    s = raw.strip()
    if not s:
        return ""
    if s.lower() == "auto":
        return "auto"
    return model_registry.to_provider_id(s, "acp") or field_default(AgentConfig, "fallback_model")


#: The Crew Mode conductor template's name. Equal to
#: ``crew_mode.DEFAULT_CREW_MODE_AGENT`` (a test pins it); defined here so this owner
#: binds no name the sections facade does not.
_CREW_MODE_AGENT_DEFAULT = "kirocrew-conductor"


@dataclass
class AgentConfig:
    approval_mode: str = field(
        default="auto",
        metadata=_meta(
            "Approval Mode",
            "Tool approval mode. Every channel dispatcher resolves it once at start "
            "(with the CLI --approval override), so a change takes effect at the "
            "next restart.",
            enum=["auto", "interactive"],
            restart=True,
        ),
    )
    streaming: bool = field(
        default=True,
        metadata=_meta("Streaming", "Enable streaming responses."),
    )
    model: str = field(
        default=DEFAULT_MODEL,
        metadata=_meta("Model", "LLM model identifier. 'auto' resolves from agent config."),
    )
    role_models: dict[str, str] = field(
        default_factory=dict,
        metadata=_meta(
            "Per-role models",
            "Optional per-task-class model overrides. Keys: 'background' "
            "(lite / heartbeat background workers) and 'subagent' (spawned "
            "sub-agents). An empty value or 'auto' defers to the chat default "
            "(agent.model) and then to the provider default, so an unpinned "
            "role stays usable on every subscription tier. Pin a cheaper model "
            "here to run background / sub-agent work on it without changing the "
            "interactive chat default. A 'subagent' pin runs each spawned "
            "sub-agent in its own runtime (its own agent process and MCP "
            "servers, about 0.55 GB of memory each) where it could otherwise "
            "share the parent chat's.",
        ),
    )
    role_efforts: dict[str, str] = field(
        default_factory=dict,
        metadata=_meta(
            "Per-role reasoning effort",
            "Optional per-task-class reasoning effort, paired with role_models "
            "(keys: 'background', 'subagent'). Empty for a role inherits the chat "
            "default (agent.reasoning_effort) and then the provider/model default. "
            "Only applies on reasoning-capable models. Like a model pin, a "
            "'subagent' effort runs each spawned sub-agent in its own runtime "
            "(about 0.55 GB of memory each) where it could otherwise share the "
            "parent chat's.",
        ),
    )
    model_context_tokens: dict[str, int] = field(
        default_factory=dict,
        metadata=_meta(
            "Served context windows",
            "Context window, in tokens, that an exact model id is served with, for "
            'example {"server-a/org/model-7b": 32768}. The key is the id exactly as '
            "agent.model carries it, so two servers of the same weights stay "
            "distinct. A declared window budgets the first prompt of a new session, "
            "before the backend has reported one, and outranks the catalog and "
            "registry windows. A value is an integer from 1024 to 10000000, at most "
            "64 entries are read, and an invalid entry is ignored with a warning. "
            "Empty by default: a model with no window of its own keeps the "
            "1,000,000-token reference. Takes effect on the next config reload.",
        ),
    )
    fallback_model: str = field(
        default="auto",
        metadata=_meta(
            "Fallback model",
            "Model tried when the active model's transient-retry budget is "
            "exhausted (throttle/capacity). Default 'auto' defers to the "
            "backend's availability-aware routing; a concrete model id (as "
            "advertised by the provider, e.g. 'claude-opus-4.8') is tried "
            "first with 'auto' as the final fallthrough; empty ('') disables "
            "fallback entirely (fail loudly, pre-feature behavior). A fallback "
            "swap is announced in chat, sticks until the primary recovers, and "
            "the serving model is recorded in every turn's stats — never "
            "silent.",
        ),
    )
    refusal_fallback_model: str = field(
        default="",
        metadata=_meta(
            "Refusal fallback model",
            "Model the current message is retried on ONCE when the active "
            "model's content filter declines it (refusal / CONTENT_FILTERED). "
            "Empty ('', the default) disables the retry: the refusal card "
            "surfaces exactly as before. A concrete model id (as advertised "
            "by the provider, e.g. 'claude-opus-4.8') retries that single "
            "message on it and restores the primary model on the next turn; "
            "'auto' retries on the model the provider's refusal envelope "
            "recommends, when it names one. The retry is announced in chat — "
            "never silent — and a refusal from the fallback too is terminal.",
        ),
    )
    reasoning_effort: str = field(
        default="",
        metadata=_meta(
            "Reasoning Effort",
            "Default reasoning effort for new sessions on models that support it. "
            "Empty defers to the provider/model default. Per-session overrides win.",
            enum=["", *EFFORT_LEVELS],
        ),
    )
    provider: str = field(
        default="acp",
        metadata=_meta("Provider", "LLM provider backend (KiroACP / kiro-cli).", enum=["acp"]),
    )
    mcp_registry_mode: bool = field(
        default=False,
        metadata=_meta(
            "Enterprise MCP Registry Mode",
            "Set true when this Kiro account is governed by an enterprise MCP "
            "registry (Kiro console -> Shared settings -> MCP Registry URL, which "
            "applies to IAM Identity Center and API-key sign-ins). In registry "
            "mode the client connects ONLY to mcpServers entries carrying "
            "'type': \"registry\" that resolve to a catalog entry of the same "
            "name, so Kiro Crew stamps that marker on the servers it manages. "
            "Leave false on a personal account: with no registry configured the "
            "filter inverts and registry-marked entries are the ones dropped. "
            "The administrator must also allow-list, by those exact names, every "
            "managed kirocrew-* server Kiro Crew mounts: kirocrew-core, "
            "kirocrew-cron and kirocrew-computer by default, plus any opt-in "
            "server (e.g. kirocrew-dashboard, kirocrew-work) an agent is granted.",
        ),
    )
    mcp_quarantine_after_failures: int = field(
        default=3,
        metadata=_meta(
            "Failing-Probe Threshold",
            "Consecutive failed probes before an MCP server is reported as "
            "persistently failing on its dashboard row. A probe verdict is "
            "otherwise forgotten between rounds, so a server that failed once on a "
            "cold cache looked identical to one that has failed forty times. "
            "Counts only 'error' and 'timeout': a server asking for OAuth sign-in "
            "is working correctly and is never counted, and one success clears the "
            "count. The server stays mounted, but while its streak is at or over "
            "this threshold MCP discovery stops spawning it for probes; the count "
            "survives a gateway restart and holds until it is cleared (the "
            "dashboard offers a one-click reset). 0 turns it off.",
        ),
    )
    acp_backend: str = field(
        default=DEFAULT_ACP_BACKEND,
        metadata=_meta(
            "ACP Backend",
            "Which ACP agent to drive. Omitted or '' means no choice: KAS is the "
            "default. 'kiro' selects kiro-cli; any other value must be a registered "
            "selectable backend id. The live list is served by GET /api/config/schema.",
            # Deliberately NO ``enum``. A literal here was frozen at import and fed
            # two import-time structures (``JSON_SCHEMA`` and ``SCHEMA_REGISTRY``),
            # both strictly earlier than an edition registering a backend at boot.
            # That made the enum actively harmful rather than merely stale —
            # ``validate_config_data`` DELETES an out-of-enum value before the
            # loader ever sees it, so a registered backend was stripped from
            # config.json on the way in. ``resolve_selected_backend`` is now the
            # single gate (it logs the reason it degrades), and
            # ``GET /api/config/schema`` supplies the live values the dashboard
            # renders. See harness-parity H4.
        ),
    )
    member_acp_backend: str = field(
        default="kas",
        metadata=_meta(
            "Crew member ACP backend",
            "Backend for crew-member DM sessions: 'kas' (default when omitted or '') "
            "or another dispatch-capable backend (claude, codex, opencode, goose). "
            "Crewmates run on KAS as their Kiro backend, so 'kiro' here also "
            "selects KAS. Members dispatch work into worker sessions through "
            "session-control tools mounted per session over the wire.",
            # Same no-enum reasoning as acp_backend above: the live selectable
            # set comes from the registry via resolve_selected_backend, never a
            # frozen literal.
        ),
    )
    kas_bundle: bool = field(
        default=False,
        metadata=_meta(
            "KAS: run Kiro Crew's own engine bundle",
            "When true, the 'kas' backend spawns the @kiro/agent build that "
            "'kirocrew kas install' laid down under the data home (engines/kas/) "
            "with its own Node runtime, instead of reaching the engine through "
            "kiro-cli's ACP relay. kiro-cli is not needed on this route, and the "
            "engine always takes its credential from Kiro Crew's own sign-in "
            "(the dashboard's Sign in), never from kiro-cli's store. A true value "
            "with nothing installed refuses the spawn and names the install command.",
            # The running KAS runtime keeps its process across the switch, so
            # sessions move routes only once that process is replaced.
            restart=True,
        ),
    )
    kas_persist_mcp_sign_in: bool = field(
        default=False,
        metadata=_meta(
            "KAS: keep remote MCP sign-ins across sessions",
            "When true, the 'kas' backend lets Kiro Crew keep a remote MCP server's "
            "OAuth sign-in (client registration and tokens) in a dedicated encrypted "
            "vault under the data home (kas/mcp-grants/), so new sessions, subagents "
            "and crons reuse it until the refresh token expires or is revoked. The "
            "vault is not the Secrets vault: it is not listed there and cannot be "
            "granted to a cron, and the agent cannot read it. When false (the "
            "default), the engine keeps each sign-in in memory and a new session "
            "asks again.",
            # Declared in the engine's initialize, so a running KAS process keeps
            # what it declared until it is replaced.
            restart=True,
        ),
    )
    kas_bundle_url: str = field(
        default=KAS_BUNDLE_DEFAULT_URL,
        metadata=_meta(
            "KAS bundle base URL",
            "https:// base the KAS engine manifest is fetched from "
            "(<base>/kas/<version>/manifest.json). Every artifact is sha256-pinned by "
            "that manifest and the manifest by this release, so the url chooses WHERE "
            "the pinned build is fetched from and never what is trusted. Only an https:// url is "
            "accepted; redirects that leave the host are refused.",
        ),
    )
    native_skill_projection: bool = field(
        default=True,
        metadata=_meta(
            "Native skill views for kiro-cli",
            "When true (the default), the 'kiro' backend launches each agent under a "
            "Kiro Crew managed view (kirocrew-skill-view-*.json in the kiro agents "
            "directory) that serves skills through Kiro Crew's skill search, and "
            "overlays the workspace's .kiro/settings/cli.json while it runs. When "
            "false, agents launch under their authored names, no view is written, "
            "the workspace overlay is rolled back, and kiro-cli loads skill metadata "
            "natively again. Same effect as KIROCREW_NATIVE_SKILL_PROJECTION=0, "
            "which still disables projection whatever this says.",
            # Latched at spawn: a running native process keeps the mode it was
            # launched under until it is replaced.
            restart=True,
        ),
    )
    default_agent: str = field(
        default="",
        metadata=_meta("Default Agent", "Default agent name for new sessions."),
    )
    crew_mode_agent: str = field(
        default=_CREW_MODE_AGENT_DEFAULT,
        metadata=_meta(
            "Crew Mode conductor",
            "The conductor template a chat runs when Crew Mode is turned on. "
            "Empty falls back to the default. Takes effect live for new chats; "
            "no gateway restart.",
        ),
    )
    deepseek_env: dict[str, str] = field(
        default_factory=dict,
        metadata=_meta(
            "DeepSeek Harness provider keys",
            "Provider keys handed to the DeepSeek Harness ('deepseek' backend) as "
            "environment variables at spawn, mapping an environment-variable NAME "
            "to a 'secret://<vault name>' reference. The harness resolves a "
            "provider credential from its inherited environment above its own "
            "credential files, so this is how it reaches a hosted model without "
            "Kiro Crew leaving those files readable inside the sandbox. Store the "
            "key under Settings > Secrets, then map it here, e.g. "
            '{"DEEPSEEK_API_KEY": "secret://my-dsh-key"}. Any provider name the '
            "harness knows works (ANTHROPIC_API_KEY, OPENAI_API_KEY, ...). A "
            "plaintext value is REFUSED at write time -- the config write itself "
            "fails, so no credential is ever stored in config.json -- and so, at "
            "spawn, is a name the harness would forward to its own shell "
            "children, a name Kiro Crew owns, or one Kiro Crew's agent "
            "environment scrub strips; the DeepSeek session is refused before it "
            "starts with a message naming the offending key. Empty means no key: "
            "a model served locally on this machine needs none. Ignored by every "
            "other backend.",
        ),
    )
    sweep_agents_backups: bool = field(
        default=False,
        metadata=_meta(
            "Sweep foreign agent backups",
            "When true, the agents-directory janitor also deletes aged backup "
            "files (*.bak-<digits> / *.json.bak.<digits>, older than 14 days) "
            "from the shared kiro agents directory. OFF by default: Kiro Crew "
            "does not author those backups, so every one it would delete belongs "
            "to another tool whose retention policy is not ours to decide. The "
            "orphaned atomic-write TEMP sweep (24h) always runs and reclaims most "
            "of the growth at near-zero risk; enable this only if you also want "
            "foreign backups in that directory reaped.",
        ),
    )
    sandbox: str = field(
        default="auto",
        metadata=_meta(
            "Sandbox",
            "Sandbox mode for ACP provider. Default 'auto' engages OS-level "
            "isolation (namespace on Linux, sandbox-exec on macOS) at the "
            "standard tier and automatically defers to kiro-cli's internal "
            "sandbox on macOS when it is enabled (kiro-cli >= 2.13; nested "
            "seatbelt causes EPERM). The standard tier deliberately leaves "
            "~/.aws, ~/.ssh and ~/.kube visible to the agent's shell so the aws "
            "CLI, boto3 credential_process, git-over-SSH and kubectl keep "
            "working; the file tools still refuse those paths. Set to 'strict' "
            "to also hide ~/.aws (including ~/.aws/sso/cache, kiro-cli's grant "
            "store for OAuth-connected remote MCP servers), ~/.ssh (except "
            "known_hosts), ~/.kube and ~/.config/gh, plus the credential files "
            "~/.npmrc, ~/.pypirc, ~/.netrc and ~/.git-credentials, from every "
            "agent subprocess -- opt-in, and inside the agent it breaks the aws "
            "CLI, boto3, git-over-SSH, gh, kubectl, npm/pip registry auth, "
            ".netrc HTTPS auth, the git credential store and remote-MCP OAuth "
            "for the same reason. Like every value of this key, a change applies "
            "to sessions started after it; a session already running keeps the "
            "tier it was spawned with until it ends. 'strict' changes "
            "nothing where Kiro Crew applies no sandbox of its own: Windows has "
            "no OS backend, and a macOS spawn delegated to kiro-cli's internal "
            "sandbox is confined by that profile instead. Set to 'off' to skip "
            "Kiro Crew's own OS-level sandbox -- delegation to kiro-cli's "
            "internal sandbox still fires on macOS if it is enabled, and a "
            "SECURITY warning is logged when neither layer is active.",
            enum=["auto", "strict", "off"],
        ),
    )
    sandbox_allow_no_isolation: bool = field(
        default=False,
        metadata=_meta(
            "Allow No-Isolation Fallback",
            "Acknowledge running the agent subprocess WITHOUT OS-level credential "
            "isolation when no sandbox backend is available (e.g. macOS >= 26, or "
            "Linux without user namespaces). When false (default), that fallback is "
            "logged as a loud SECURITY warning. When true, the operator has accepted "
            "the risk and it is logged at info level.",
        ),
    )
    sandbox_allow_unsandboxed_exec: bool = field(
        default=False,
        metadata=_meta(
            "Allow Unsandboxed Execution",
            "When true, allow agent subprocesses to execute without any sandbox "
            "backend (fail-open). When false, wrap_argv raises if no sandbox "
            "backend is available and mode is not 'off', preventing unsandboxed "
            "execution entirely (fail-closed). This is distinct from "
            "sandbox_allow_no_isolation which only controls warning severity — "
            "this field controls whether execution proceeds at all. "
            "A LOADED config carries the effective policy: the loader resolves an "
            "undeclared key through config.loader.unsandboxed_exec_platform_default() "
            "— allow on Windows, which has no backend that any operator action could "
            "install, and fail-closed everywhere else, where a missing backend is "
            "broken or one profile away from working. A declared value always wins in "
            "both directions, and a governance sandbox.min_level floor outranks the "
            "declaration. save() omits the key unless the operator declared it, so "
            "a 'never decided' host stays undeclared on disk. This dataclass "
            "default stays platform-independent so the committed config-schema "
            "snapshot is identical on every platform. "
            "`kirocrew setup` surfaces the decision on a backend-less host, offering "
            "the opt-in where the default is fail-closed and stating the exposure plus "
            "offering the opt-out where it is allow, and writes nothing unless the "
            "operator answers yes.",
        ),
    )
    apps_allow_third_party: bool = field(
        default=False,
        metadata=_meta(
            "Allow Third-Party Apps",
            "Explicitly allow executable code from third-party (non-builtin) apps. "
            "Defaults to false. Only the JSON boolean true admits in-process Python "
            "hooks, backend processes, lifecycle/install scripts, and openCommand. "
            "App code can access the filesystem, network, and in-memory credentials; "
            "enable this only for apps you trust (CSE SEC-012). Prefer "
            "apps_trusted, which grants the same admission to ONE named app.",
        ),
    )
    apps_trusted: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Trusted Apps",
            "Per-app grants for third-party execution — the narrow form of "
            "apps_allow_third_party. An app whose manifest name appears here is "
            "admitted to run Python hooks, its backend, lifecycle scripts, and "
            "openCommand; every other third-party app stays blocked. Only a JSON "
            "array of app-name strings is honoured, and no wildcard entry is "
            "accepted (use apps_allow_third_party to trust all).",
        ),
    )
    apps_trusted_local: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Trusted Local Apps",
            "App names whose per-app execution grant was explicitly reviewed "
            "as local, repository-less code. This internal grant-kind marker "
            "distinguishes current local consent from legacy name-only grants; "
            "it is effective only with the matching apps_trusted entry.",
        ),
    )
    apps_trusted_repositories: dict[str, str] = field(
        default_factory=dict,
        metadata=_meta(
            "Trusted App Repositories",
            "Repository coordinates captured by the per-app trust endpoint. "
            "Each key is an app name from apps_trusted and each value is the "
            "normalized repository shown at consent. Registry installation "
            "refuses if that name later resolves to a different repository. "
            "Legacy repository-backed grants without an entry require one-time "
            "re-consent before code execution.",
        ),
    )
    apps_default_installs_applied: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Applied Default App Installs",
            "App names already installed once because their registry marked "
            "them defaultInstall. A name here is never installed again "
            "automatically, so an app you uninstall or disable stays that way. "
            "Remove a name to let it be installed again on the next start.",
        ),
    )
    apps_ui_stream_timeout_secs: int = field(
        default=30,
        metadata=_meta(
            "App UI Stream Timeout",
            "Total transfer deadline, in seconds, for ONE response body on the "
            "unauthenticated /apps/{name}/ui/ route. Clamped to 5..600; a value "
            "that is not a whole number loads as 30. Read per request, so an "
            "edit applies without a restart. There is no off switch: the route "
            "holds eight descriptor permits, and this deadline is what stops a "
            "client that quits draining its socket from holding one for as long "
            "as it stays connected — eight such clients would otherwise stop "
            "every app UI on the host. Raise it only when a real transfer on "
            "this host needs longer than the default allows.",
        ),
    )
    jail: str = field(
        default=JAIL_MODE_AUTO,
        metadata=_meta(
            "Jail",
            "Process-isolation jail mode for agent-bearing commands. 'auto' uses a "
            "jail when the active edition supplies a working backend (the public "
            "edition has none, so 'auto' and 'on' are no-ops there); 'off' disables "
            "it. Disable per-invocation with --no-jail or KIROCREW_NO_JAIL=1.",
            enum=list(_VALID_JAIL_MODES),
            restart=True,
        ),
    )
    dangerously_skip_permissions: bool = field(
        default=False,
        metadata=_meta(
            "Dangerously Skip Permissions",
            "Skip EVERY tool approval confirmation, permanently. Declaring it here "
            "is a standing instruction: the grant does not expire and is "
            "re-established on every startup. This is the advanced, "
            "config-file-only escape hatch — there is deliberately no dashboard "
            "toggle for it. An enterprise policy can forbid it, which falls back "
            "to the ad-hoc duration below.",
            restart=True,
        ),
    )
    yolo_duration: str = field(
        default="6h",
        metadata=_meta(
            "Ad-hoc Auto-approve Duration",
            "How long auto-approve (YOLO) lasts when it is enabled AD HOC — from "
            "the dashboard picker, Slack, or the API. Every one of those surfaces "
            "uses this same duration. Accepts 30m / 1h / 6h / 12h / 24h, or "
            "until_shutdown to keep it on with no timed expiry until Kiro Crew "
            "restarts. Timed values are capped at 24h. Does NOT apply to a grant "
            "declared via 'dangerously_skip_permissions' above, which persists.",
            enum=["30m", "1h", "6h", "12h", "24h", "until_shutdown"],
        ),
    )
    notify_override_expiry: bool = field(
        default=True,
        metadata=_meta(
            "Notify on Override Expiry",
            "DM the Slack owner when a time-limited safety override (YOLO) expires. "
            "Disable to silence the recurring expiry DM; the dashboard banner still shows.",
        ),
    )
    bot_name: str = field(
        default="",
        metadata=_meta(
            "Bot Name",
            "Custom name the bot identifies as in conversations. Leave empty for default.",
        ),
    )
    tool_search: bool = field(
        default=True,
        metadata=_meta(
            "MCP Tool Search",
            "Load MCP tool specs on demand (search-and-call) instead of sending "
            "every tool definition each turn, keeping the context window clear "
            "when many MCP servers are configured. kiro-cli applies it through the "
            "agent overlay; KAS receives it in the ACP initialize request, enabled "
            "only when the spawn agent's spec grants the tool_search loader. "
            "Deferral only starts once the specs cross tool_search_min_pct or "
            "tool_search_min_tokens; disabling reverts to sending full tool "
            "specs. Crew's servers defer only when the spawn runs the pinned "
            "kiro-cli install or its kiro-cli-chat, and both are >= 2.27.0; any "
            "other executable, an older or unknown version keeps them resident. To override the never-defer list, set "
            "ASBX_KIRO_MANDATORY_MCPS (comma-separated server names) in the "
            "gateway's environment. No effect on any other ACP backend.",
        ),
    )
    tool_search_min_pct: int = field(
        default=5,
        metadata=_meta(
            "Tool Search threshold (% of context)",
            "Start deferring MCP tool specs once they exceed this percentage of "
            "the context window. Paired with tool_search_min_tokens — whichever "
            "is crossed first wins. Below both thresholds every spec is sent "
            "directly, so the agent never pays a tool_search round-trip for a "
            "small tool set. 0 with tool_search_min_tokens 0 defers always. "
            "Clamped to 0-100; matches the kiro-cli default.",
        ),
    )
    tool_search_min_tokens: int = field(
        default=50000,
        metadata=_meta(
            "Tool Search threshold (tokens)",
            "Start deferring MCP tool specs once they exceed this many tokens. "
            "Paired with tool_search_min_pct — whichever is crossed first wins. "
            "0 with tool_search_min_pct 0 defers always. Matches the kiro-cli "
            "default.",
        ),
    )
    session_sharing: bool = field(
        default=True,
        metadata=_meta(
            "Session Sharing",
            "Subagents reuse a shared ACP runtime instead of spawning a fresh "
            "backend process per subagent. Reduces startup from ~3-5s to ~200ms "
            "and memory from ~400MB to near-zero per subagent. Default ON for the "
            "eligible backends (kiro-cli and codex); every other ACP backend "
            "ignores it and runs a dedicated process per subagent. Set false to "
            "opt an eligible backend back onto per-subagent processes.",
        ),
    )
    max_subagents: int = field(
        default=0,
        metadata=_meta(
            "Max SubAgents",
            "Maximum amount of subagents at one time. 0 = auto: the "
            "subagent_auto_max ceiling, with free memory bounding each start "
            "through spawn_min_memory_gb long before that on most hosts (sized "
            "from host memory when spawn_min_memory_gb is 0, 3 when host memory "
            "cannot be read; see dynamic-subagent-sizing docs). "
            "Default; set a fixed cap by pinning an integer >= 3 (values of 1 or "
            "2 are raised to 3). A live edit moves the subagent cap at once; the "
            "MCP gateway's spawn-gate ceiling, raised to this figure, follows "
            "at the next gateway restart.",
        ),
    )
    max_stop_hook_nudges: int = field(
        default=100,
        metadata=_meta(
            "Max Stop-hook nudges",
            "Maximum consecutive Stop-hook block continuations before the run "
            "halts and surfaces a halt card instead of dispatching another turn. "
            "Bounds a buggy always-block hook in an unattended session. 0 = "
            "uncapped (opt-in for genuinely unbounded feedback loops).",
        ),
    )
    spawn_min_memory_gb: float = field(
        default=_DEFAULT_SPAWN_MIN_MEMORY_GB,
        metadata=_meta(
            "Spawn Min Memory GB",
            "Available memory (GB) that must remain after admitting a subagent start. A "
            "dedicated-process start is priced at what such a runtime settles at (about 1 GB "
            "until runs of that agent have been measured, then their learned size capped at "
            "2 GB, never below subagent_cost_gb); one that shares its parent's runtime at "
            "about 0.35 GB "
            "less. A spawn that does not fit waits in the queue and is re-checked when "
            "memory can have moved, at the latest after admit_wait_secs; one in "
            "temporary or incognito memory mode, or with "
            "task_queue_enabled off, is lost if the gateway restarts. On macOS a start also "
            "waits while the kernel reports memory pressure and one of this gateway's "
            "dedicated subagents is running. 0 disables the check, that wait included, "
            "and then sizes the auto subagent cap (max_subagents=0) from available memory "
            "instead.",
        ),
    )
    resource_pressure_gb: float = field(
        default=4.0,
        metadata=_meta(
            "Resource Pressure Threshold (GB)",
            "Available memory (GB) at or below which the agent is told host memory "
            "is 'tight' via a compact [RESOURCES] context line, so it can prefer "
            "the lighter path for heavy work (targeted tests, smaller sub-agent "
            "waves). On macOS the line also fires while the kernel reports memory "
            "pressure. Advisory only — not enforced. 0 disables the context line, "
            "that macOS case included. Lower this on small-memory hosts / "
            "memory-limited containers (e.g. a 2-4 GB pod) so the advisory only fires "
            "under genuine pressure.",
        ),
    )
    resource_critical_gb: float = field(
        default=2.0,
        metadata=_meta(
            "Resource Critical Threshold (GB)",
            "Available memory (GB) at or below which the [RESOURCES] context line "
            "escalates to 'critically low' and advises against starting heavy work "
            "at all. Should be <= resource_pressure_gb. 0 disables the critical tier.",
        ),
    )
    admission_gate: bool = field(
        default=True,
        metadata=_meta(
            "Posture Admission Gate",
            "While available memory is at or below resource_critical_gb, defer "
            "scheduled cron firings to the next tick, and TaskRunner steps and "
            "workflow ctx.agent() calls by admit_wait_secs, until memory frees. "
            "Subagent spawns are not gated on this posture; they wait on "
            "spawn_min_memory_gb instead. Manually triggered cron runs and "
            "direct chat turns are never gated; an unreadable probe admits "
            "(fail-open). Set false to make the critical posture advisory-only.",
        ),
    )
    task_queue_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Durable Task Queue",
            "Persist every accepted subagent spawn to $KIROCREW_HOME/tasks/tasks.db "
            "before its id is returned, so accepted work survives a gateway crash "
            "and memory pressure defers a spawn instead of refusing it. Set false "
            "to fall back to the in-memory spawn queue for one release; tasks.db "
            "is left in place and unread.",
        ),
    )
    task_dispatch_window: int = field(
        default=64,
        metadata=_meta(
            "Task Dispatch Window",
            "Maximum number of queued spawns the gateway keeps in memory at once; "
            "the rest wait as rows in tasks.db and are read in FIFO order as the "
            "window drains. 2000 accepted tasks are 2000 rows and this many "
            "Python objects. Clamped to 1..4096.",
            restart=True,
        ),
    )
    task_store_journal_mode: str = field(
        default="auto",
        metadata=_meta(
            "Task Store Journal Mode",
            "SQLite journal mode for tasks.db: 'auto' picks WAL only on a volume "
            "DETECTED local, and DELETE both on a detected network filesystem and "
            "when detection cannot tell -- WAL needs local shared memory, which "
            "SMB/NFS does not provide, so an undecidable volume takes the slower "
            "correct mode rather than risking the store. 'wal' or 'delete' force "
            "one and skip the detection, so a local disk whose mount table is "
            "unreadable keeps WAL by declaring it. Unknown values read as 'auto'.",
            restart=True,
            # The loader lower-cases, then reads any other value as 'auto'.
            enum=["auto", "wal", "delete"],
        ),
    )
    admit_wait_secs: int = field(
        default=30,
        metadata=_meta(
            "Admit Wait (seconds)",
            "How long an admitted task may wait for its resources before it goes "
            "back to queued, and the longest a spawn deferred by the memory floor "
            "(spawn_min_memory_gb) waits before it is re-checked. Clamped to 1..3600.",
            restart=True,
        ),
    )
    subagent_queue_max_wait_secs: int = field(
        default=_DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS,
        metadata=_meta(
            "Subagent Queue Max Wait (seconds)",
            "How long a subagent waits for free memory before it gives up. Its "
            "parent is then told it never started. Waiting for a free slot does not "
            "count, except on macOS: a start held for memory pressure is timed from "
            "its first hold. 0 waits forever; the most is 86400 (one day).",
        ),
    )
    start_collect_timeout_secs: int = field(
        default=300,
        metadata=_meta(
            "Start Collect Timeout (seconds)",
            "After a session start times out, how long the start collector keeps "
            "the task in 'recovering' to adopt a late session/new response before "
            "the start is retried. Reserved for the session-start gate; clamped "
            "to 10..3600.",
            restart=True,
        ),
    )
    lane_weights: dict[str, int] = field(
        default_factory=dict,
        metadata=_meta(
            "Lane Weights",
            "Per-lane weight overrides for the task dispatcher, keyed by lane "
            "(a root session key, or 'system'). Unlisted lanes weigh 1. Values "
            "are clamped to 1..64. Weights shape the share of picks and, while a lane "
            "waits for memory, each lane's share of running dedicated subagents; never a "
            "hard cap: a lane with nothing pending costs the others nothing.",
        ),
    )
    child_reserve: int = field(
        default=1,
        metadata=_meta(
            "Child Reserve",
            "Execution slots a top-level (depth-0) task may never take while a "
            "nested task is queued or a parent is waiting on its children. Only "
            "children and resuming parents may use them, so a fleet of parents "
            "can never hold every slot with no child able to start; while a "
            "parent waits, an adaptive cap is also lifted to at least "
            "adaptive_floor + child_reserve (never above max_subagents). 0 "
            "disables the reserve. Clamped to 0..8.",
        ),
    )
    recovery_backoff_base_secs: float = field(
        default=2.0,
        metadata=_meta(
            "Recovery Backoff Base (seconds)",
            "First retry delay of the shared recovery ladder (tool call, backend, "
            "ACP runtime) and of a coordinated dependency wait; each further "
            "attempt doubles it with jitter. One schedule for every layer, so "
            "layers never retry in lock-step; snapshotted once at gateway start. "
            "The gateway-daemon supervisor keeps its own pinned floor. "
            "Clamped to 0.1..60.",
            restart=True,
        ),
    )
    recovery_backoff_max_secs: float = field(
        default=120.0,
        metadata=_meta(
            "Recovery Backoff Cap (seconds)",
            "Longest delay between two recovery attempts on the shared ladder or "
            "a dependency wait; a server-stated retry hint is honoured up to this "
            "cap. Snapshotted once at gateway start. Clamped to 1..3600 and never "
            "below the base.",
            restart=True,
        ),
    )
    session_start_concurrency: int | str = field(
        default="auto",
        metadata=_meta(
            "Session Start Concurrency",
            "How many ACP session/new requests may be outstanding at once per "
            "gateway event loop (the SessionStartGate). session/new blocks while "
            "the backend initializes the session's MCP servers, so a burst of "
            "subagent starts on one shared runtime slows every start until the "
            "budget is hit; queued starts a person is waiting on are served first, "
            "except that a waiting background start is let through after a bounded "
            "run of them so it is never starved, and FIFO within each class. Queue "
            "time is not charged to the start budget "
            "or the startup deadline, but a subagent start that stays queued past "
            "the start-queue cap ends as never started, unless its subagent "
            "timeout ends it first. A fixed bound, not adaptive: the adaptive loop is the MCP gateway spawn "
            'gate and the execution-cap controller. "auto" (the default) sizes it '
            "once at gateway start as min(cpus // 4, available GB // 3) clamped to "
            "2..16, with cpus the affinity count capped by a cgroup cpu.max quota; "
            "the gateway logs the value in force at boot, and kirocrew doctor "
            "shows what auto picks for the host now. An integer sets it "
            "explicitly, clamped to 1..64.",
            restart=True,
        ),
    )
    child_env_defaults: dict[str, str] = field(
        default_factory=dict,
        metadata=_meta(
            "kiro-cli Child Env Defaults",
            "Environment variables given to every spawned kiro-cli process (chat "
            "sessions, background sessions and subagents) ONLY when the variable "
            "is not already set in the environment it inherits. kiro-cli sizes its "
            "thread pools by core count, and every thread counts against the agent "
            "slice's task ceiling, so where that ceiling is in force (Linux with "
            "the agents slice) a name left out of this map defaults to "
            "min(cpus, 4) for TOKIO_WORKER_THREADS and RAYON_NUM_THREADS; on other "
            "hosts no default is set. kirocrew "
            "doctor shows the value in force. A value set here wins, e.g. "
            '{"TOKIO_WORKER_THREADS": "8"}. Only '
            "TOKIO_WORKER_THREADS and RAYON_NUM_THREADS are accepted, each a "
            "decimal integer string from 1 to 1024; any other "
            "entry is ignored with a warning. Kiro Crew's own agent environment "
            "scrub still applies. Every process the agent runs (tools, MCP servers, "
            "your own programs) inherits these variables too. Takes effect on the "
            "next spawn.",
        ),
    )
    env: dict[str, str] = field(
        default_factory=dict,
        metadata=_meta(
            "Agent Environment Variables",
            "Environment variables set on every agent process Kiro Crew starts for "
            "a chat session, background session or subagent, on every backend, and "
            "therefore inherited by everything that agent runs: shell tool calls, "
            "the MCP servers the agent itself starts, and their children. Not the "
            "gateway, MCP servers the gateway hosts, or Kiro Crew's own one-shot "
            "helper commands. For non-sensitive configuration such as a proxy, a CA "
            "bundle, an AWS profile or a package registry, e.g. "
            '{"HTTPS_PROXY": "http://proxy.example:3128", "NO_PROXY": '
            '"localhost,127.0.0.1", "AWS_PROFILE": "dev"}. A value set here wins '
            "over the same variable "
            "in the gateway's own environment; variables Kiro Crew sets for the "
            "session itself still win over this map, except defaults it fills in "
            "only when a name is unset (agent.child_env_defaults, the pytest-xdist "
            "worker cap), which this map overrides. A name this map refuses is "
            "rejected when the setting is saved (kirocrew config set or a dashboard "
            "write fails and names the key and the reason), and dropped with a "
            "warning naming the key if a hand edit puts it in config.json. Values "
            "are stored in plaintext, so a name that looks like a credential, with "
            "a name segment that is or ends in KEY, TOKEN, SECRET, PASSWORD, PASSWD "
            "or CREDENTIAL (API_KEY, GITHUB_TOKEN, APIKEY), is refused: keep "
            "credentials under Settings > Secrets. GOPRIVATE and "
            "TOKENIZERS_PARALLELISM are not credential names. Also refused: names "
            "Kiro Crew or the sandbox owns (PATH, HOME, TMPDIR, "
            "DBUS_*_BUS_ADDRESS, KIROCREW_*, KIRO_*, OPENCODE_*, ...); names known "
            "to make a shell, the loader, a runtime or a tool run code the command "
            "did not name (LD_*, DYLD_*, PYTHON*, NODE_OPTIONS, BASH*, SHELLOPTS, "
            "PS4, ZDOTDIR, IFS, SSH_*, GIT_* except GIT_TERMINAL_PROMPT and "
            "GIT_SSL_CAINFO, and any name ending in PATH, LIB, OPT, OPTS, OPTIONS, "
            "FLAGS, COMMAND, SHELL, EXECUTABLE, EDITOR, PAGER, ASKPASS, BROWSER, "
            "...), including the location of a config file a tool may read a "
            "program from (KUBECONFIG, AWS_CONFIG_FILE, any name ending in CONFIG, "
            "CONFIG_DIR, CONFIG_FILE or CONFIG_HOME); the variables that move "
            "a harness's sign-in directory (CLAUDE_CONFIG_DIR, CODEX_HOME, "
            "XDG_CONFIG_HOME, XDG_DATA_HOME, ...); and names the agent environment "
            "scrub removes. Takes effect for agent processes started after the "
            "change: warm pooled processes and the shared background process are "
            "replaced, while a session that is already running, and subagents "
            "sharing its process, keep the environment that process started with.",
        ),
    )
    cold_start_concurrency: int | str = field(
        default="auto",
        metadata=_meta(
            "Session Cold-Start Concurrency",
            "How many sessions one session manager may cold-start at once. Each "
            "start holds its permit from runtime spawn until its MCP servers have "
            "reported, so a burst of starts beyond this bound queues. A person's "
            'start also has one reserved permit on top. "auto" (the default) '
            "follows the session start concurrency: at least 4, at most 16, so 16 "
            "once that concurrency is 16. An integer is clamped to 1..32. Read when the "
            "session manager is built; a later host reading can widen it, never narrow it.",
            restart=True,
        ),
    )
    runtime_spawn_concurrency: int | str = field(
        default="auto",
        metadata=_meta(
            "Runtime Spawn Concurrency",
            "How many ACP runtime processes may be in spawn + initialize at once "
            'per gateway event loop. "auto" (the default) is half the session '
            "start concurrency: at least 2, at most 8, so 8 once that concurrency "
            "is 16. An "
            "integer is clamped to 1..32. Read when the loop's first runtime "
            "spawns; a later host reading can widen it, never narrow it.",
            restart=True,
        ),
    )
    adaptive_concurrency: bool = field(
        default=True,
        metadata=_meta(
            "Adaptive Concurrency",
            "Run the adaptive concurrency controller: a runtime execution cap "
            "beneath max_subagents (the ceiling, never written) that starts AT "
            "that ceiling, halves when two signals that admitted work is failing "
            "agree (attributable start timeouts, slow starts on several MCP "
            "servers, failing backend inits, fd or process counts near their "
            "limit, a low completion rate) and earns +1 back per clean window. "
            "It never reads event-loop lag or free memory: spawn_min_memory_gb "
            "bounds memory per start. The MCP gateway daemon's spawn gate is "
            "shaped too, and it does react to loop lag and low memory. Set false "
            "to run at the user cap only.",
        ),
    )
    adaptive_concurrency_mode: str = field(
        default="aimd",
        metadata=_meta(
            "Adaptive Concurrency Mode",
            "'aimd': multiplicative decrease / additive increase, with "
            "pause-and-probe on the MCP spawn gate. 'fixed': both caps pinned at "
            "their initial values (the execution cap at max_subagents) -- a plain "
            "semaphore -- the one-flip reversal if the controller is seen to "
            "oscillate.",
            enum=["aimd", "fixed"],
        ),
    )
    adaptive_floor: int = field(
        default=1,
        metadata=_meta(
            "Adaptive Floor",
            "Lowest execution cap the controller may shrink to under sustained "
            "pressure. Clamped to 1..64.",
        ),
    )
    adaptive_initial: int = field(
        default=4,
        metadata=_meta(
            "Adaptive Initial Cap",
            "Inert: the execution cap now starts at max_subagents (or "
            "subagent_auto_max when it is 0), because free memory, not a count, "
            "bounds how many subagents start. Preserved on load and save so an "
            "existing config is not rewritten out from under the operator.",
        ),
    )
    adaptive_slow_start: bool = field(
        default=True,
        metadata=_meta(
            "Adaptive Slow Start",
            "Until the gateway first meets corroborated host pressure, let a "
            "cap below its ceiling DOUBLE per clear 5-second window (on one "
            "completion and real demand) instead of climbing +1 per clear "
            "30-second window, bounded by max_subagents (subagent_auto_max when "
            "it is 0). The execution cap "
            "starts at that ceiling, so this is how a cut cap climbs back. The "
            "first pressure ends slow start for the life "
            "of the process. Set false to climb +1 per clear 30-second window "
            "from the start. Slow start earns an increase on one completion plus "
            "real demand; congestion avoidance earns each +1 after one full wave "
            "of the CURRENT cap completes (at most 20 runs). Alternatively, "
            "fresh stream progress with queued work and no provider throttle can "
            "earn one probe slot per clear window without a completion; this "
            "never earns doubling or relaxes the initialization gate.",
        ),
    )
    controller_sample_secs: int = field(
        default=5,
        metadata=_meta(
            "Controller Sample Interval (seconds)",
            "How often the adaptive controller samples the host and the spawn "
            "gate. Clamped to 1..300.",
        ),
    )
    dependency_max_attempts: int = field(
        default=20,
        metadata=_meta(
            "Dependency Max Attempts",
            "Coordinated retries a dependency scope gets before every task "
            "waiting on it is failed with the reason. One probe per attempt for "
            "the whole scope, not one per waiting task. Clamped to 1..1000.",
        ),
    )
    dependency_wait_deadline_secs: int = field(
        default=3600,
        metadata=_meta(
            "Dependency Wait Deadline (seconds)",
            "Wall-clock ceiling a task may wait on one dependency scope before "
            "it is failed with the reason; 0 disables the clock and leaves only "
            "the attempts cap. Clamped to 0..86400.",
        ),
    )
    dependency_wake_per_tick: int = field(
        default=0,
        metadata=_meta(
            "Dependency Wake Per Tick",
            "How many waiting tasks a recovered dependency releases per wake "
            "tick, after the single probe that confirms recovery. 0 = the "
            "current effective admission capacity, so a recovered dependency "
            "never replays every waiter at once. Clamped to 0..4096.",
        ),
    )
    dependency_wake_spacing_secs: float = field(
        default=1.0,
        metadata=_meta(
            "Dependency Wake Spacing (seconds)",
            "Pause between wake ticks while a recovered dependency's waiters are "
            "released in capacity-sized batches. Clamped to 0..60.",
        ),
    )
    interactive_command_policy: str = field(
        default="cancel",
        metadata=_meta(
            "Interactive Command Policy",
            "What the tool-stall watchdog does when a stalled shell command is "
            "classified as waiting for input (a pager, editor, REPL or confirm "
            "prompt). 'cancel' ends that tool call non-lethally and re-drives the "
            "turn with a non-interactive hint; 'wait' announces a waiting_input "
            "status once and keeps the turn open for real input, bounded by the "
            "turn's own ceiling. Neither ever answers the prompt itself. Read "
            "when a session handle is created and on config hot-apply.",
            enum=["cancel", "wait"],
        ),
    )
    workflow_run_timeout_secs: int = field(
        default=3600,
        metadata=_meta(
            "Workflow Run Timeout (secs)",
            "Wall-clock ceiling for one dynamic-workflow run. This is a runaway "
            "backstop, so it is clamped to 60s..21600s (6h) — raise it for long "
            "multi-phase investigations, but it can never be disabled. Reaching "
            "the ceiling is no longer a data-loss event: every agent result "
            "completed before the cutoff is preserved on the run record.",
        ),
    )
    subagent_mem_buffer_pct: int = field(
        default=20,
        metadata=_meta(
            "SubAgent Memory Buffer %",
            "Percent of available memory reserved for the OS and other processes "
            "when sizing the TaskRunner's auto parallel-step cap "
            "(taskrunner.max_parallel_steps=0). The auto subagent cap is sized "
            "from memory only when spawn_min_memory_gb is 0 or less and so "
            "disables the per-start floor; then it uses this same figure. "
            "Otherwise spawn_min_memory_gb bounds each subagent start.",
        ),
    )
    chat_turn_timeout_secs: int = field(
        default=14400,
        metadata=_meta(
            "Chat Turn Timeout (secs)",
            "Wall-clock ceiling for one chat turn. This is a runaway backstop, "
            "so it is clamped to 300s..86400s (24h) and can never be disabled. "
            "The 4h default covers the longest single turn the shipped budgets "
            "produce (a 90-minute test command plus a fix and a re-run, or a "
            "blocking subagent wave at its 2h wait cap); the ACP transport's "
            "prompt wait follows it. Hitting the ceiling is visible: the turn ends "
            "with a card naming the limit. For work spanning many hours or days, "
            "prefer monitor/goal loops — they end the turn between cycles and "
            "survive restarts, which a single marathon turn cannot.",
        ),
    )
    session_start_timeout_secs: int = field(
        default=90,
        metadata=_meta(
            "Session Start Timeout (secs)",
            "Budget for ACP session/new and session/load on the shared "
            "runtime. kiro-cli blocks the response while it initializes the "
            "agent's MCP servers, so session start scales with server count "
            "and per-server cold-start cost (sandboxed launchers, remote "
            "servers, loaded hosts). Raise this when a large agent "
            "legitimately needs longer than the 90s default. The floor is "
            "the default itself: the budget must stay comfortably above the "
            "backend's 30s OAuth authorization wait, so values below 90 are "
            "clamped up.",
        ),
    )
    tool_approval_timeout_secs: int = field(
        default=600,
        metadata=_meta(
            "Tool Approval Timeout (secs)",
            "How long a chat turn waits for a human to answer a tool-approval "
            "prompt before declining it and telling the user to resend. Kept "
            "well below the chat-turn ceiling on purpose: a window at or above "
            "it can never fire, so an unattended turn burns the whole ceiling "
            "and is then misreported as a turn timeout. Clamped to 30s..7200s, "
            "and additionally to 60s below the turn ceiling at load time.",
        ),
    )
    session_control: bool = field(
        default=True,
        metadata=_meta(
            "Session Control",
            "Let one chat session open a new session, and stop, read or send to "
            "another session of yours. Reading returns a transcript tail, stopping "
            "cancels an in-flight turn, a created session starts empty for you to "
            "type into, and a send runs text as the target's next turn. On by "
            "default, because the grant that decides who can do this is the agent "
            "config: the tools come from the kirocrew-dashboard MCP server, so an "
            "agent that does not mount it never has them, exactly like any other "
            "MCP server. Turn this off to withdraw the capability from every agent "
            "at once without editing each spec, except crew member DM sessions and "
            "the Conductor in your own dashboard tab, which keep dispatching "
            "workers unless Member Dispatch is also off. Sessions can only reach "
            "peers in the same workspace; incognito, app-scoped and scheduled "
            "sessions are never addressable, and a crew member, a scheduled run, "
            "or a Conductor admitted with this off reaches only sessions it "
            "created itself.",
        ),
    )
    session_manager_chain: bool = field(
        default=False,
        metadata=_meta(
            "Session Manager Chain",
            "Let a session reach the whole line of sessions under it, not only the "
            "ones it manages directly. With it on, a lead that handed a worker to a "
            "conductor can still send to, read, stop and close that worker, and "
            "closing the conductor hands its workers to the lead. Off by default.",
        ),
    )
    member_dispatch: bool = field(
        default=True,
        metadata=_meta(
            "Member Dispatch",
            "Let a crew member's DM session, and the Conductor in your own "
            "dashboard tab, open and drive worker sessions they create, even when "
            "Session Control is off. On by default, because dispatching work into "
            "workers is the crew-member operating model, not an opt-in: this is "
            "the zero-configuration contract. Turn this off to put member callers "
            "and the Conductor back under the Session Control switch, so an "
            "operator who withdrew session control keeps them chat-only without "
            "disabling them. When on, their reach is still bounded to sessions "
            "they created themselves (creator ownership), the same fence that "
            "binds a member when Session Control is on.",
        ),
    )
    crew_panel: bool = field(
        default=True,
        metadata=_meta(
            "Crew Dashboard",
            "Let a crew member publish its own webview, shown in that member's "
            "drawer on the Crew page. A member sends a JSON object and names a "
            "template that renders it, so a long-running crew can say what it is "
            "holding, which worker is stuck and what needs a decision, to someone "
            "who is not reading its transcript. On by default, because a crew that "
            "cannot be watched is the thing this surface exists to fix. The tools "
            "come from the kirocrew-panel MCP server, so the grant follows the "
            "same rule as every other MCP server: an agent that does not mount it "
            "never has them. Turn this off to withdraw the capability from every "
            "member at once without editing each spec, and the withdrawal is "
            "immediate: the publish route reads this switch on every call, so a "
            "member whose session was already running loses the panel too. A "
            "member writes only its OWN panel: the server resolves the publishing "
            "crew from the calling session and takes no crew or session argument, "
            "and a subagent has no panel to write.",
        ),
    )
    subagent_cost_gb: float = field(
        default=_DEFAULT_SUBAGENT_COST_GB,
        metadata=_meta(
            "SubAgent Memory Cost (GB)",
            "The least a dedicated sub-agent start is priced at when admission "
            "reserves its memory (the measured or learned settled size applies "
            "when higher); also the per-agent fallback the TaskRunner's auto "
            "parallel-step cap is sized with until a learned value accumulates, "
            "and the auto subagent cap too while spawn_min_memory_gb is 0 or "
            "less and so disables the per-start floor.",
        ),
    )
    subagent_cpu_cost_cores: float = field(
        default=1.0,
        metadata=_meta(
            "SubAgent CPU Cost (cores)",
            "Deprecated and inert: CPU never sizes subagent concurrency, because "
            "over-committing memory is an unrecoverable OOM while over-committing "
            "CPU only slows work the adaptive controller already backs off from. "
            "Preserved on load and save so an existing config is not rewritten "
            "out from under the operator.",
            deprecated=True,
        ),
    )
    subagent_auto_max: int = field(
        default=32,
        metadata=_meta(
            "SubAgent Auto-Size Max",
            "How many subagents may run at once when max_subagents=0 (auto). A "
            "high count ceiling, not a memory figure: free memory bounds each "
            "start through spawn_min_memory_gb and is reached long before this on "
            "most hosts. It stands in for what the memory floor does not model, "
            "the LLM provider's concurrency and the host's file-descriptor and "
            "process limits. 3 applies instead when host memory cannot be read. "
            "Also the upper bound of the TaskRunner's auto parallel-step cap. "
            "Ignored when max_subagents is set explicitly. Like max_subagents, "
            "a live edit reaches the MCP spawn-gate ceiling at the next gateway "
            "restart.",
        ),
    )
    subagent_spawn_stagger_secs: float = field(
        default=0.25,
        metadata=_meta(
            "SubAgent Spawn Stagger (seconds)",
            "Delay between successive subagent spawns (initial fill and queued "
            "drain) to bound cold-start CPU/memory spikes. Starts stay "
            "serialized; the interval only decides how fast a wide fan-out "
            "fills. Raise it if this "
            "host or the model provider is the bottleneck -- a spawn still has "
            "to leave spawn_min_memory_gb free after its start and clear the host "
            "budget, and the adaptive "
            "controller cuts the cap when admitted work fails (timeouts, slow "
            "starts, fd or process exhaustion), so this is a smoothing "
            "interval rather than the memory guard.",
        ),
    )
    subagent_max_turns: int = field(
        default=_DEFAULT_SUBAGENT_MAX_TURNS,
        metadata=_meta("SubAgent Max Turns", "Default tool-call budget per subagent."),
    )
    subagent_timeout_secs: int = field(
        default=_SUBAGENT_TIMEOUT_SECS,
        metadata=_meta(
            "SubAgent Timeout (seconds)",
            "Wall-clock timeout per subagent execution. 0 uses the same default. "
            f"Clamped to {_SUBAGENT_TIMEOUT_MIN}s..{_SUBAGENT_TIMEOUT_MAX}s at load "
            "(0 is preserved). Raising it past 2 hours only helps the "
            "fire-and-forget spawn_run path: a BLOCKING spawn_sub_agents call "
            "collects for at most 2 hours whatever this is set to, so use "
            "spawn_run for work longer than that.",
        ),
    )
    subagent_stall_idle_secs: int = field(
        default=120,
        metadata=_meta(
            "SubAgent Stall Idle (seconds)",
            "Seconds with no stream activity before a running subagent is surfaced "
            "as 'stalled' in the running-card. 0 uses hardcoded default (120s).",
        ),
    )
    completion_keep: str = field(
        default="head",
        metadata=_meta(
            "Completion Keep",
            "Which end of the subagent transcript to keep in the completion event "
            "injected into the parent session. Three values: 'head' (first N chars), "
            "'tail' (last N chars), 'both' (head + middle marker + tail). The full "
            "transcript stays in result.txt until cleanup; use spawn_status MCP tool "
            "to read it.",
            enum=["head", "tail", "both"],
        ),
    )
    completion_keep_chars: int = field(
        default=3000,
        metadata=_meta(
            "Completion Keep Chars",
            "Maximum characters retained in the completion event after applying "
            "completion_keep. 0 disables truncation entirely. Default 3000.",
        ),
    )
    subagent_result_ttl_secs: int = field(
        default=3600,
        metadata=_meta(
            "SubAgent Result TTL (seconds)",
            "How long a delivered subagent's result.txt is retained before the "
            "reaper prunes it. The completion event returns a summary plus this "
            "file path; the parent reads the full transcript on demand (read / "
            "grep / spawn_status) within this window instead of re-running the "
            "subagent. 0 prunes on the next reaper sweep. Default 3600 (1h).",
        ),
    )
    subagent_cwd_allowed_roots: list[str] = field(
        default_factory=lambda: list(DEFAULT_CWD_ALLOWED_ROOTS),
        metadata=_meta(
            "SubAgent CWD Allowed Roots",
            "Directory roots under which spawn_run's cwd parameter is permitted. "
            "Values support ~ expansion. Empty list disables cwd overrides.",
        ),
    )
    max_channels: int = field(
        default=1,
        metadata=_meta("Max Channels", "Maximum concurrent agent channels (1-5)."),
    )
    max_channel_agents: int = field(
        default=3,
        metadata=_meta("Max Channel Agents", "Maximum agents per channel (1-10)."),
    )
    log_level: str = field(
        default="WARNING",
        metadata=_meta(
            "Log Level",
            "Persistent log level for the kiro_crew logger. "
            "Applied at startup; overridden by --verbose CLI flag.",
            enum=["DEBUG", "INFO", "WARNING", "ERROR"],
        ),
    )
    soft_stop_budget_secs: float = field(
        default=10.0,
        metadata=_meta(
            "Soft-Stop Budget",
            "Seconds to wait for cooperative cancel before hard-killing the session.",
        ),
    )

    def __post_init__(self) -> None:
        self.max_channels = max(1, min(5, self.max_channels))
        self.max_channel_agents = max(1, min(10, self.max_channel_agents))
        # Clamp to [0.5, 60.0] to match ``KiroCrewConfig.load()`` behavior
        # (dashboard PATCH and YAML loader both clamp rather than raise).
        clamped = max(0.5, min(60.0, float(self.soft_stop_budget_secs)))
        if clamped != self.soft_stop_budget_secs:
            logger.warning(
                "soft_stop_budget_secs=%s out of range [0.5, 60.0]; clamped to %s",
                self.soft_stop_budget_secs,
                clamped,
            )
            self.soft_stop_budget_secs = clamped
        # Keep only known role keys, each normalized ("auto"/non-str -> "").
        # Defensive for directly-constructed instances; the load() path already
        # feeds coerced input.
        self.role_models = coerce_role_models(self.role_models)
        self.role_efforts = coerce_role_efforts(self.role_efforts)
        self.model_context_tokens = coerce_model_context_tokens(self.model_context_tokens)
        # Same defensive TYPE coercion for the DeepSeek provider-key map. What its
        # names and values may BE is refused at spawn, by name -- see
        # coerce_deepseek_env.
        self.deepseek_env = coerce_deepseek_env(self.deepseek_env)
        self.child_env_defaults = coerce_child_env_defaults(self.child_env_defaults)
        self.env = coerce_agent_env(self.env)
        # Same defensive coercion for the throttle-fallback model: normalize to
        # ""/"auto"/acp id, so consumers can trust the stored shape.
        self.fallback_model = coerce_fallback_model(self.fallback_model)
        # And for the content-filter fallback model — same shapes, junk
        # collapses to "" (off) rather than "auto".
        self.refusal_fallback_model = coerce_refusal_fallback_model(self.refusal_fallback_model)

    def resolve_model(self, role: str) -> str:
        """Effective model id for a task ``role`` — INDEPENDENT of the chat model.

        Returns the role's own pin (``role_models[role]``) or :data:`DEFAULT_MODEL`
        (``"auto"``). It deliberately does NOT inherit ``agent.model``: background
        workers (lite / heartbeat) run unattended, so riding the interactive chat
        flagship on every cycle would be a silent cost regression. ``"auto"`` lets
        the provider pick a served model, entitlement-safe on every tier. Callers
        that write a kiro agent spec / cc_model store this verbatim.
        """
        return normalize_agent_model(self.role_models.get(role, "")) or DEFAULT_MODEL

    def resolve_effort(self, role: str) -> str:
        """Effective reasoning effort for a task ``role`` — INDEPENDENT of the chat
        default.

        Returns ``role_efforts[role]`` or ``""`` (the provider/model default). It
        does not inherit ``agent.reasoning_effort``, for the same reason
        :meth:`resolve_model` does not inherit ``agent.model``. Effort only takes
        effect on reasoning-capable models; on others it is ignored downstream.
        """
        return self.role_efforts.get(role, "")


# The field's default label, read once for ``yolo_duration_to_secs``'s unknown label.
_YOLO_DURATION_DEFAULT = field_default(AgentConfig, "yolo_duration")


def _read_skip_permissions(agent_data: dict) -> bool:
    """Read the standing auto-approve declaration, honouring older spellings.

    The key was renamed from ``yolo`` so the config itself warns about what it
    does. Canonical spelling is ``dangerously_skip_permissions`` — snake_case
    like every other key in this file, which is also what ``save()`` writes, so
    a save/load round-trip preserves it.

    Two other spellings are accepted on read, most-specific first:
    ``dangerouslySkipPermissions`` (the camelCase form used by other agent tools,
    so a config copied from one still works) and the legacy ``yolo`` (so no
    existing config silently loses auto-approve on upgrade).

    Requires a REAL ``bool``, not Python truthiness: a stringly-typed value
    from a templated/generated config — ``"false"``, ``"0"``, ``"no"``, or any
    other non-empty string a hand-edit or a config generator might write — is
    truthy in Python, so a bare ``bool(...)`` here would silently turn
    "explicitly disabled" into the standing, unattended tool-auto-approve
    grant this key controls. A non-bool value is never treated as an
    affirmative grant; it falls through to check the next spelling, then to
    ``False``. With no spelling present at all it reads the field's default.
    """
    malformed = False
    for key in ("dangerously_skip_permissions", "dangerouslySkipPermissions", "yolo"):
        if key in agent_data:
            value = agent_data[key]
            if isinstance(value, bool):
                return value
            logger.warning(
                "agent.%s must be a real boolean, got %r — treating as unset",
                key,
                value,
            )
            malformed = True
    if malformed:
        return False
    return field_default(AgentConfig, "dangerously_skip_permissions")


def yolo_duration_to_secs(label: str) -> int:
    """Seconds for a ``yolo_duration`` label; 0 means "no timed expiry"."""
    if label == YOLO_UNTIL_SHUTDOWN:
        return 0
    return _YOLO_DURATION_SECS.get(label, _YOLO_DURATION_SECS[_YOLO_DURATION_DEFAULT])
