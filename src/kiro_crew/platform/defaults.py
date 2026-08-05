"""Default adapters — the public open-source behavior for every extension point.

Each ``Default*`` adapter delegates to the existing module-level symbol it
replaces (``agent._MANAGED_MCP_SERVERS``, ``sandbox._STRICT_DIRS``,
``security.redact``, ``sso_status.*``, ``embeddings._MODEL_ID``, …) so the
standalone edition is behaviorally identical to today — the contract adds an
indirection layer, not a behavior change.

The Amazon companion subclasses or replaces these in its composition root.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from kiro_crew.platform.interfaces import McpScope

from kiro_crew import security, sso_status
from kiro_crew.platform.interfaces import CapabilityResult, InterceptDecision

# ``agent``, ``sandbox``, ``embeddings``, ``apps.registry`` and ``slack.enterprise``
# import ``kiro_crew.platform`` at module-load time, so importing them at the top
# of this module (loaded during ``platform`` package init via ``bootstrap``)
# would create a cycle — those stay local to each method and carry a
# ``# circular import`` annotation.  ``security`` and ``sso_status`` are imported at
# top level here because neither reaches ``platform`` at MODULE-LOAD time.
# Exception (deferred-only): ``security.scan_exfiltration_urls`` /
# ``redact_exfiltration_urls`` read ``current_context().credentials
# .exempt_exact_hosts()`` through a FUNCTION-LOCAL import of
# ``kiro_crew.platform.context`` (the ``sel.py`` deferred pattern).  Because that
# reach-back is deferred to call time — never at ``security`` module load — the
# top-level ``security`` import above stays cycle-free.


class DefaultProviderRegistry:
    """Kiro-CLI-ACP only.  Leaves the dormant ACP_BACKEND_CLAUDE seam untouched."""

    def create_factory(self, cfg: Any) -> Callable[..., Any]:
        return cfg.create_provider_factory()

    def register_acp_backends(self) -> None:
        # The public edition registers no extra ACP backends.  The companion
        # re-registers a Claude backend here via the acp/client.py:_is_claude
        # seam.
        return None


class DefaultPublishRegistry:
    """Registers no publish provider — the public edition has no artifact-publish
    destination.  The ``publish_provider`` registry stays empty, so
    ``get_provider`` raises ``PublishUnavailableError`` (→ 503) and
    ``list_providers`` returns ``[]`` (dashboard shows "publishing unavailable")
    with no core branching.  A companion registers its concrete providers here
    via the ``publish_provider.register_provider`` side effect — the structural
    twin of ``DefaultProviderRegistry.register_acp_backends``."""

    def register_publish_providers(self) -> None:
        return None


class DefaultAgentRuntime:
    """Today's managed MCP servers + first-run setup."""

    def managed_mcp_servers(self) -> Dict[str, dict]:
        # RESERVED (see context.RESERVED_METHODS['agent_runtime']): no core call
        # site reads this — the agent config is built from the
        # ``agent._MANAGED_MCP_SERVERS`` global directly.  Kept faithful to that
        # global so the method is correct if it is ever wired; contribute extra
        # servers through the WIRED ``McpToolingProvider.extra_mcp_servers()``.
        from kiro_crew import agent  # circular import: agent imports platform

        return dict(agent._MANAGED_MCP_SERVERS)

    def run_first_run_setup(self) -> None:
        # WIRED: ``slack/gateway.py`` gateway boot calls this through the seam.
        # Delegating to the real ``agent.run_first_run_setup`` makes the routing
        # behavior-preserving for the standalone edition — byte-for-byte the same
        # first-run wiring (PATH shim + admission-policy seed + one-time stale
        # managed-MCP purge) the gateway would otherwise invoke directly.  A companion
        # overrides this to add its own one-time provisioning on top (and should
        # call the same underlying function, or super(), to keep the core steps).
        from kiro_crew import agent  # circular import: agent imports platform

        agent.run_first_run_setup()


class DefaultAgentExecutableResolver:
    """Use the executable selected by the public installation unchanged."""

    def resolve_executable(self, executable: str) -> str:
        return executable


class DefaultSandboxPolicy:
    """Today's open-source sensitive-dir lists from ``sandbox.py``."""

    def strict_dirs(self) -> List[str]:
        from kiro_crew import sandbox  # circular import: sandbox imports platform

        return list(sandbox._STRICT_DIRS)

    def cc_dirs(self) -> List[str]:
        from kiro_crew import sandbox  # circular import: sandbox imports platform

        return list(sandbox._CC_DIRS)


class DefaultCredentialPolicy:
    """Today's AKIA/ASIA + exfil redaction passes from ``security.py``."""

    def redact(self, text: str) -> str:
        return security.redact(text)

    def exempt_exact_hosts(self) -> "frozenset[str]":
        # The public edition exempts no hosts from the exfil heuristics — the
        # base64-blob / query-length checks run for every domain, so redaction
        # is byte-identical to today.  The companion returns its trusted-tenant
        # host set (empty = MORE redaction, the safe direction).
        return frozenset()


class DefaultSlackEnterpriseGate:
    """Default-open gate delegating to ``slack/enterprise.py``."""

    def validate_enterprise(self, bot_token: str, *, extra_ids: "set[str] | None" = None) -> bool:
        # deferred: defaults.py loads at platform-init (bootstrap imports it);
        # importing slack.enterprise eagerly would pull the slack + config stack
        # into every boot. No import cycle here — kept local for lazy loading.
        from kiro_crew.slack import enterprise

        return enterprise.validate_enterprise(bot_token, extra_ids=extra_ids)

    def check_message_origin(self, event_team_id: str) -> bool:
        # deferred: see validate_enterprise above (lazy-load the slack stack;
        # no cycle).
        from kiro_crew.slack import enterprise

        return enterprise.check_message_origin(event_team_id)

    def heartbeat_safe_tools(self) -> "frozenset[str]":
        # The public edition adds no tools to the heartbeat allowlist — the set
        # stays exactly the core HEARTBEAT_SAFE_TOOLS. The companion returns its
        # internal read-only tool names.
        return frozenset()

    def intercept_message(
        self,
        orch: Any,
        *,
        channel: str,
        sender_id: str,
        clean_text: str,
        thread_ts: "str | None",
        msg_ts: str,
    ) -> "InterceptDecision":
        # The public edition processes every allowed message inline — no
        # challenge-and-redirect. Returning PROCESS keeps _route_message
        # byte-identical to the pre-seam OSS behavior.
        return InterceptDecision.PROCESS


class DefaultIdentityProvider:
    """No-SSO local identity — the ``sso_status.py`` no-op stubs."""

    def status(self) -> Dict[str, object]:
        return sso_status.sso_status()

    async def status_line(self, prefix: str = "*SSO:*") -> str:
        return await sso_status.get_sso_status_line(prefix)

    def whoami(self) -> Optional[str]:
        # RESERVED (see context.RESERVED_METHODS['identity']): no core call site.
        # The public edition has no SSO principal beyond what kiro-cli reports.
        return None

    def issuer(self) -> Optional[str]:
        # RESERVED (see context.RESERVED_METHODS['identity']): no core call site.
        return None

    def preflight_checks(self) -> List[Callable[[], None]]:
        # The public edition runs no pre-launch checks — gateway/token startup
        # is unchanged.  The companion returns its SSO-session checks here.
        return []

    def credential_watch_paths(self) -> List[Path]:
        # The public edition watches no credential files — the MCP gateway
        # daemon runs with no rotation watcher. A companion returns its
        # rotated-credential file path(s) here.
        return []


class DefaultEmbeddingSource:
    """Bundled in-process model (vendored llama.cpp), unsigned local inference.

    RESERVED slot (see ``context.RESERVED_SLOTS['embeddings']``): the core has no
    HTTP embed path, so NO method here is consumed.  Kept faithful to today's
    model id so the adapter is correct if the slot is ever wired; a companion
    supplying a different runtime composes an ``EmbeddingBackend`` via
    ``embeddings.register_embedding_backend`` instead.
    """

    def registry_model(self) -> str:
        from kiro_crew import embeddings  # circular import: embeddings imports platform

        return embeddings._MODEL_ID

    def endpoint_url(self) -> Optional[str]:
        # In-process runtime — no remote endpoint.
        return None

    def sign_request(
        self, method: str, url: str, headers: dict, body: "bytes | str"
    ) -> Optional[dict]:
        # Unsigned: in-process inference makes no HTTP requests.
        return None


class DefaultMcpToolingProvider:
    """No extra MCP servers, skills, or provider scopes beyond the managed set."""

    def extra_mcp_servers(self) -> Dict[str, dict]:
        return {}

    def extra_skills(self) -> List[Path]:
        return []

    def extra_mcp_scopes(self) -> List["McpScope"]:
        return []


class DefaultAgentCatalogProvider:
    """No edition agent-catalog rows — discovery is the on-disk scan only."""

    def builtin_agents(self) -> List[Dict[str, Any]]:
        return []


class DefaultPromptSourceProvider:
    """No edition prompt/SOP roots — only user-authored prompts are listed."""

    def prompt_source_roots(self) -> List[Path]:
        return []


class DefaultCapabilityManager:
    """Unavailable capability manager — the public edition ships no external
    package manager, so ``/api/capability/*`` report 503. Every operation is a
    fail-closed no-op that MUST NOT be reached (handlers guard on ``available()``)."""

    def available(self) -> bool:
        return False

    async def list_mcp(self) -> List[Dict[str, Any]]:
        return []

    async def install_mcp(self, server_id: str) -> "CapabilityResult":
        return CapabilityResult(ok=False, message="capability manager not available")

    async def uninstall_mcp(self, server_id: str) -> "CapabilityResult":
        return CapabilityResult(ok=False, message="capability manager not available")

    async def registry(self) -> List[Dict[str, Any]]:
        return []

    async def list_skills(self) -> List[Dict[str, Any]]:
        return []

    async def install_skill(self, package: str) -> "CapabilityResult":
        return CapabilityResult(ok=False, message="capability manager not available")

    async def uninstall_skill(self, package: str) -> "CapabilityResult":
        return CapabilityResult(ok=False, message="capability manager not available")

    async def list_agents(self) -> List[Dict[str, Any]]:
        return []

    async def install_agent(self, package: str) -> "CapabilityResult":
        return CapabilityResult(ok=False, message="capability manager not available")

    async def uninstall_agent(self, package: str) -> "CapabilityResult":
        return CapabilityResult(ok=False, message="capability manager not available")

    async def list_plugins(self) -> List[Dict[str, Any]]:
        return []

    async def plugins_out_of_sync(self) -> List[str]:
        # Empty = "in sync", which is the correct answer for an edition with no
        # plugin concept (not merely a fail-closed stub).
        return []

    async def sync_plugins(self) -> "CapabilityResult":
        return CapabilityResult(ok=False, message="capability manager not available")


class DefaultAppRegistryPolicy:
    """Today's public trusted-host set + clone-sandbox-mode decision.

    Delegates to ``apps/registry.py._PUBLIC_GIT_HOSTS`` — the public-forge set
    (github / gitlab / bitbucket / sr.ht / codeberg), with NO internal host
    trusted.  A clone from any host outside that set runs ``strict`` sandbox
    mode.  The Amazon companion's ``AmazonAppRegistryPolicy`` overrides
    ``public_git_hosts()`` to add the internal git hosts so its registry clones
    are trusted; the public Default never trusts an internal host.
    """

    def public_git_hosts(self) -> "frozenset[str]":
        from kiro_crew.apps import registry  # circular import: apps.registry imports platform

        return registry._PUBLIC_GIT_HOSTS

    def clone_sandbox_mode(self, git_url: str, trusted_hosts: "frozenset[str] | None") -> str:
        from kiro_crew.apps import registry  # circular import: apps.registry imports platform

        return registry._clone_sandbox_mode(git_url, trusted_hosts)


class DefaultAppsLoader:
    """The open-source ``apps/builtins/`` set."""

    def bundled_app_names(self) -> List[str]:
        # auto_research + file_explorer ship in the public core.
        return ["auto_research", "file_explorer"]

    def manifest_sources(self) -> List[Path]:
        return []

    def registry_rows(self) -> List[Dict[str, Any]]:
        # The public edition bundles no extra App-Store rows beyond
        # apps/app-registry.json. A companion returns its internal catalog rows.
        return []


class DefaultPackageManager:
    """Public brew/curl/pip install strategy (delegated to cli_doctor logic).

    RESERVED slot (see ``context.RESERVED_SLOTS['package_manager']``): no core
    call site routes installs through this seam — ``cli_doctor.py`` keeps its
    inline per-tool logic.  Use ``CapabilityManager`` for registry-backed
    installs of MCP servers / skills / agent packages.
    """

    def install_plan(self, tool: str) -> List[str]:
        # The public edition has no managed installer; callers fall back to
        # their existing inline brew/curl/pip logic when the plan is empty.
        return []

    def which(self, tool: str) -> Optional[str]:
        return shutil.which(tool)


class DefaultTunnelProvider:
    """Disabled tunnel — the ``tunnel/manager.py`` stub is a no-op."""

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def public_url(self) -> str:
        return ""

    def enabled(self) -> bool:
        return False

    def register_callbacks(
        self,
        *,
        on_connect: Optional[Callable[[str], Any]] = None,
        on_disconnect: Optional[Callable[[], Any]] = None,
    ) -> None:
        # No managed tunnel → the connect/disconnect reflection callbacks never
        # fire.  The stub TunnelManager keeps them for import compatibility.
        return None

    def status_snapshot(self) -> Optional[Dict[str, Any]]:
        # None → the stub TunnelManager reports its own local status, so the
        # standalone ``/api/tunnel/status`` payload is byte-identical to today.
        return None

    async def ensure_available(self, *, install: bool = True) -> str:
        # Standalone never auto-provisions a tunnel — pure no-op, so a shared
        # dashboard link falls back to the local host:port exactly as today.
        return "disabled"


class DefaultTelemetryProvider:
    """No-op telemetry; RUM stays disabled (frontend shim already no-op)."""

    def record_event(self, event_type: str, data: dict) -> None:
        return None

    def frontend_rum_config(self) -> Optional[dict]:
        return None


class DefaultKnowledgeProvider:
    """No extra connectors — the public edition ships only the built-in set."""

    def extra_connectors(self, cfg: Any) -> Dict[str, Any]:
        return {}


class DefaultDashboardContributor:
    """No-op dashboard contributor — no edition routes, services, or login handler."""

    def contribute_routes(self, app: Any) -> None:
        return None

    async def start_services(self, app: Any) -> None:
        return None

    async def stop_services(self, app: Any) -> None:
        return None

    def sso_login_handler(self) -> Optional[Callable[..., Any]]:
        # None → the dashboard keeps its built-in /api/sso-login stub handler.
        return None

    def on_user_message(self, app: Any, message: str) -> None:
        # The public edition observes no chat messages. A companion uses this to
        # e.g. auto-ingest doc links pasted into chat.
        return None

    def on_token_consumed(
        self,
        user_id: str,
        channel: str,
        session_exp: float,
        thread_ts: "str | None",
    ) -> None:
        # The public edition opens no Slack auth window on token consumption.
        return None

    def decorate_reply(self, text: str, *, channel: str, user_id: str) -> str:
        # The public edition sends replies unchanged (no expiry footer / window
        # refresh — there is no challenge window in OSS).
        return text


class DefaultJailProvider:
    """No jail — the public edition never re-execs into a process isolation jail."""

    def available(self) -> bool:
        return False

    def status_detail(self) -> str:
        return "no jail provider (public edition)"

    def maybe_reexec_into_jail(self, argv: List[str], mode: str) -> Optional[int]:
        # None → no re-exec; the command runs in-process exactly as today.
        return None
