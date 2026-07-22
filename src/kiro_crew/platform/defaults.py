"""Default adapters — the public open-source behavior for every extension point.

Each ``Default*`` adapter delegates to the existing module-level symbol it
replaces (``agent._MANAGED_MCP_SERVERS``, ``sandbox._STRICT_DIRS``,
``security.redact``, ``midway.*``, ``embeddings._MODEL_ID``, …) so the
standalone edition is behaviorally identical to today — the contract adds an
indirection layer, not a behavior change.

The Amazon companion subclasses or replaces these in its composition root.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from kiro_crew import midway, security

# ``agent``, ``sandbox``, ``embeddings``, ``apps.registry`` and ``slack.enterprise``
# import ``kiro_crew.platform`` at module-load time, so importing them at the top
# of this module (loaded during ``platform`` package init via ``bootstrap``)
# would create a cycle — those stay local to each method and carry a
# ``# circular import`` annotation.  ``security`` and ``midway`` are imported at
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
        # re-registers Claude Code here via the acp/client.py:_is_claude seam.
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
        from kiro_crew import agent  # circular import: agent imports platform

        return dict(agent._MANAGED_MCP_SERVERS)

    def run_first_run_setup(self) -> None:
        # Delegate to the real ``agent.run_first_run_setup`` so that IF this
        # NOT-YET-WIRED seam is ever consumed, the standalone Default reproduces
        # today's first-run wiring (PATH shim + one-time stale managed-MCP purge)
        # rather than silently no-op'ing.  Today nothing reads this adapter — the
        # live path calls ``agent.run_first_run_setup()`` directly from the
        # gateway boot — so this is inert; the Amazon companion overrides it to
        # add internal first-run setup on top.
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


class DefaultIdentityProvider:
    """No-SSO local identity — the ``midway.py`` no-op stubs."""

    def status(self) -> Dict[str, object]:
        return midway.midway_status()

    async def status_line(self, prefix: str = "*Midway:*") -> str:
        return await midway.get_midway_status_line(prefix)

    def whoami(self) -> Optional[str]:
        # The public edition has no SSO principal beyond what kiro-cli reports.
        return None

    def issuer(self) -> Optional[str]:
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

    Since the in-process embeddings landed the core no longer routes embed
    requests over HTTP, so ``endpoint_url``/``sign_request`` have no active
    consumption site — the seam stays for contract stability (a companion can
    still supply a remote/signed source and compose a custom
    ``EmbeddingBackend`` via ``embeddings.register_embedding_backend``).
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
    """No extra MCP servers or skills beyond the managed set."""

    def extra_mcp_servers(self) -> Dict[str, dict]:
        return {}

    def extra_skills(self) -> List[Path]:
        return []


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
    """Public brew/curl/pip install strategy (delegated to cli_doctor logic)."""

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

    def mwinit_handler(self) -> Optional[Callable[..., Any]]:
        # None → the dashboard keeps its built-in /api/mwinit stub handler.
        return None

    def on_user_message(self, app: Any, message: str) -> None:
        # The public edition observes no chat messages. A companion uses this to
        # e.g. auto-ingest doc links pasted into chat.
        return None


class DefaultJailProvider:
    """No jail — the public edition never re-execs into a process isolation jail."""

    def available(self) -> bool:
        return False

    def status_detail(self) -> str:
        return "no jail provider (public edition)"

    def maybe_reexec_into_jail(self, argv: List[str], mode: str) -> Optional[int]:
        # None → no re-exec; the command runs in-process exactly as today.
        return None
