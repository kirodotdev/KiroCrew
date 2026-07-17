"""App manifest — static metadata for KiroCrew apps.

An app manifest (``app.json``) declares an app's identity, resources, and
requirements without executing any app code.  KiroCrew reads it during
install to register agents, skills, crons, UI pages, and backend config.

Design follows the same pattern as :class:`kiro_crew.plugins.manifest.PluginManifest`
(dataclass + ``from_dict`` / ``to_dict`` / ``validate`` / round-trip) but with
app-specific fields.
"""
from __future__ import annotations

import json
import ntpath
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Nested manifest types
# ---------------------------------------------------------------------------

KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+([+-]|$)")


def _path_escapes_app_root(rel_path: str, app_root: Path | None) -> bool:
    """Return True if ``rel_path`` is an unsafe app-resource path.

    Unsafe = absolute (POSIX/Windows/UNC), or — resolved against ``app_root`` —
    escapes ``app_root`` (canonical containment, matching the runtime checks in
    ``module_loader`` / ``bridges``). When ``app_root`` is None (pure-format
    validation / round-trip tests) falls back to a lexical check that rejects
    absolute paths and any ``..`` path segment.
    """
    if not rel_path:
        return False
    if os.path.isabs(rel_path) or ntpath.isabs(rel_path):
        return True
    if app_root is not None:
        try:
            resolved = (app_root / rel_path).resolve()
            return not resolved.is_relative_to(app_root.resolve())
        except (OSError, ValueError):
            return True
    return ".." in Path(rel_path).parts


@dataclass
class CronEntry:
    """A scheduled agent job declared by an app."""

    name: str = ""
    every: int = 0  # seconds between runs (0 = use cron_expr)
    cron_expr: str = ""  # cron expression (alternative to every)
    agent: str = ""  # agent name to run
    message: str = ""  # prompt message for the agent
    # Extended fields for advanced scheduling
    agent_sequence: list[str] = field(default_factory=list)  # ordered list of agents to run
    env: dict[str, str] = field(default_factory=dict)  # environment variables for the job
    persistent_session: bool = True  # whether to carry context between runs
    silent: bool = False  # suppress dashboard notifications

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name}
        if self.every:
            d["every"] = self.every
        if self.cron_expr:
            d["cron_expr"] = self.cron_expr
        if self.agent:
            d["agent"] = self.agent
        if self.message:
            d["message"] = self.message
        if self.agent_sequence:
            d["agent_sequence"] = self.agent_sequence
        if self.env:
            d["env"] = self.env
        if not self.persistent_session:
            d["persistent_session"] = False
        if self.silent:
            d["silent"] = True
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CronEntry:
        return cls(
            name=str(data.get("name", "")),
            every=int(data.get("every", 0)),
            cron_expr=str(data.get("cron_expr", "")),
            agent=str(data.get("agent", "")),
            message=str(data.get("message", "")),
            agent_sequence=[str(a) for a in data.get("agent_sequence", [])],
            env={str(k): str(v) for k, v in data.get("env", {}).items()},
            persistent_session=bool(data.get("persistent_session", True)),
            silent=bool(data.get("silent", False)),
        )


@dataclass
class UIPage:
    """A frontend page contributed by an app."""

    route: str = ""  # URL path, e.g. /apps/oncall-watchtower
    label: str = ""  # sidebar display text
    icon: str = ""  # lucide icon name or emoji
    iconUrl: str = ""  # custom icon image path relative to ui/ dir  # noqa: N815
    # Optional INACTIVE-state variant of iconUrl (a muted/dark rendering shown when
    # the nav row is not the active route — matches how lucide nav icons gray out).
    iconInactiveUrl: str = ""  # noqa: N815
    entryPoint: str = ""  # path to JS bundle relative to app root  # noqa: N815
    mountFunction: str = "mount"  # exported function name  # noqa: N815

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "route": self.route,
            "label": self.label,
        }
        if self.icon:
            d["icon"] = self.icon
        if self.iconUrl:
            d["iconUrl"] = self.iconUrl
        if self.iconInactiveUrl:
            d["iconInactiveUrl"] = self.iconInactiveUrl
        if self.entryPoint:
            d["entryPoint"] = self.entryPoint
        if self.mountFunction != "mount":
            d["mountFunction"] = self.mountFunction
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UIPage:
        return cls(
            route=str(data.get("route", "")),
            label=str(data.get("label", "")),
            icon=str(data.get("icon", "")),
            iconUrl=str(data.get("iconUrl", "")),  # noqa: N815
            iconInactiveUrl=str(data.get("iconInactiveUrl", "")),  # noqa: N815
            entryPoint=str(data.get("entryPoint", "")),  # noqa: N815
            mountFunction=str(data.get("mountFunction", "mount")),  # noqa: N815
        )


@dataclass
class UISidebar:
    """Sidebar placement config for app pages."""

    section: str = "Apps"
    order: int = 10

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.section != "Apps":
            d["section"] = self.section
        if self.order != 10:
            d["order"] = self.order
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UISidebar:
        return cls(
            section=str(data.get("section", "Apps")),
            order=int(data.get("order", 10)),
        )


@dataclass
class UIConfig:
    """Frontend configuration for an app."""

    entry: str = ""  # ESM bundle path relative to app root, e.g. "dist/index.mjs"
    pages: list[UIPage] = field(default_factory=list)
    sidebar: UISidebar = field(default_factory=UISidebar)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.entry:
            d["entry"] = self.entry
        if self.pages:
            d["pages"] = [p.to_dict() for p in self.pages]
        sidebar_d = self.sidebar.to_dict()
        if sidebar_d:
            d["sidebar"] = sidebar_d
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UIConfig:
        pages = [UIPage.from_dict(p) for p in data.get("pages", []) if isinstance(p, dict)]
        sidebar_raw = data.get("sidebar", {})
        sidebar = UISidebar.from_dict(sidebar_raw) if isinstance(sidebar_raw, dict) else UISidebar()
        return cls(entry=str(data.get("entry", "")), pages=pages, sidebar=sidebar)


@dataclass
class HooksConfig:
    """Python entry points for gateway lifecycle integration.

    Each field is a dotted module path in the format ``module.path:callable_name``,
    resolved relative to the app's directory via the module_loader.
    """

    routes: str = ""  # e.g. "backend.routes:register_routes"
    on_startup: str = ""  # e.g. "backend.hooks:on_startup"
    on_shutdown: str = ""  # e.g. "backend.hooks:on_shutdown"

    # Validation pattern: dotted identifiers separated by colon
    _HOOK_PATH_RE = re.compile(
        r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*:[a-zA-Z_][a-zA-Z0-9_]*$"
    )

    def validate(self) -> list[str]:
        """Validate hook path formats. Returns list of errors."""
        errors: list[str] = []
        for field_name in ("routes", "on_startup", "on_shutdown"):
            value = getattr(self, field_name)
            if value and not self._HOOK_PATH_RE.match(value):
                errors.append(
                    f"backend.hooks.{field_name} must be in format "
                    f"'module.path:callable_name', got: {value!r}"
                )
        return errors

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.routes:
            d["routes"] = self.routes
        if self.on_startup:
            d["on_startup"] = self.on_startup
        if self.on_shutdown:
            d["on_shutdown"] = self.on_shutdown
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HooksConfig:
        return cls(
            routes=str(data.get("routes", "")),
            on_startup=str(data.get("on_startup", "")),
            on_shutdown=str(data.get("on_shutdown", "")),
        )


@dataclass
class BackendConfig:
    """Backend process configuration for an app."""

    entryPoint: str = ""  # e.g. backend/app.py or dist/main.js  # noqa: N815
    port: str = "auto"  # "auto" or a specific port number
    healthCheck: str = "/health"  # health check endpoint path  # noqa: N815
    routes: str = ""  # base route path, e.g. /api/apps/oncall-watchtower
    type: str = ""  # "python", "asgi", "node", or "" (auto-detect)
    hooks: HooksConfig = field(default_factory=HooksConfig)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.entryPoint:
            d["entryPoint"] = self.entryPoint
        if self.port != "auto":
            d["port"] = self.port
        if self.healthCheck != "/health":
            d["healthCheck"] = self.healthCheck
        if self.routes:
            d["routes"] = self.routes
        if self.type:
            d["type"] = self.type
        hooks_d = self.hooks.to_dict()
        if hooks_d:
            d["hooks"] = hooks_d
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BackendConfig:
        hooks_raw = data.get("hooks", {})
        hooks = HooksConfig.from_dict(hooks_raw) if isinstance(hooks_raw, dict) else HooksConfig()
        return cls(
            entryPoint=str(data.get("entryPoint", "")),  # noqa: N815
            port=str(data.get("port", "auto")),
            healthCheck=str(data.get("healthCheck", "/health")),  # noqa: N815
            routes=str(data.get("routes", "")),
            type=str(data.get("type", "")),
            hooks=hooks,
        )


@dataclass
class Permissions:
    """Declared permissions for an app."""

    api: list[str] = field(default_factory=list)  # allowed API path prefixes
    events: list[str] = field(default_factory=list)  # allowed WebSocket event types
    mcpTools: list[str] = field(default_factory=list)  # noqa: N815
    storage: bool = False
    network: bool = False
    memory: str = ""  # "", "app-scoped", or "shared"
    cron: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.api:
            d["api"] = self.api
        if self.events:
            d["events"] = self.events
        if self.mcpTools:
            d["mcpTools"] = self.mcpTools
        if self.storage:
            d["storage"] = True
        if self.network:
            d["network"] = True
        if self.memory:
            d["memory"] = self.memory
        if self.cron:
            d["cron"] = True
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Permissions:
        return cls(
            api=[str(p) for p in data.get("api", []) if p],
            events=[str(e) for e in data.get("events", []) if e],
            mcpTools=[str(t) for t in data.get("mcpTools", []) if t],  # noqa: N815
            storage=bool(data.get("storage", False)),
            network=bool(data.get("network", False)),
            memory=str(data.get("memory", "")),
            cron=bool(data.get("cron", False)),
        )


@dataclass
class SetupConfig:
    """Installation and setup configuration for an app."""

    onInstall: str = ""  # shell command run after first install  # noqa: N815
    onUpdate: str = ""  # shell command run after update (new code in place)  # noqa: N815
    onUninstall: str = ""  # shell command run before removing app files  # noqa: N815
    onEnable: str = ""  # shell command run when app is enabled  # noqa: N815
    onDisable: str = ""  # shell command run when app is disabled  # noqa: N815
    onEnableTimeout: int = 30  # seconds; configurable per-app  # noqa: N815
    onDisableTimeout: int = 30  # seconds; configurable per-app  # noqa: N815
    configSchema: dict[str, Any] = field(default_factory=dict)  # noqa: N815

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.onInstall:
            d["onInstall"] = self.onInstall
        if self.onUpdate:
            d["onUpdate"] = self.onUpdate
        if self.onUninstall:
            d["onUninstall"] = self.onUninstall
        if self.onEnable:
            d["onEnable"] = self.onEnable
        if self.onDisable:
            d["onDisable"] = self.onDisable
        if self.onEnableTimeout != 30:
            d["onEnableTimeout"] = self.onEnableTimeout
        if self.onDisableTimeout != 30:
            d["onDisableTimeout"] = self.onDisableTimeout
        if self.configSchema:
            d["configSchema"] = self.configSchema
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SetupConfig:
        return cls(
            onInstall=str(data.get("onInstall", "")),  # noqa: N815
            onUpdate=str(data.get("onUpdate", "")),  # noqa: N815
            onUninstall=str(data.get("onUninstall", "")),  # noqa: N815
            onEnable=str(data.get("onEnable", "")),  # noqa: N815
            onDisable=str(data.get("onDisable", "")),  # noqa: N815
            onEnableTimeout=int(data.get("onEnableTimeout", 30)),  # noqa: N815
            onDisableTimeout=int(data.get("onDisableTimeout", 30)),  # noqa: N815
            configSchema=dict(data.get("configSchema", {})),  # noqa: N815
        )


@dataclass
class AimDependencies:
    """AIM CLI-managed dependencies (MCP servers, skills, agents)."""

    mcp: list[Any] = field(default_factory=list)  # str or {"id": str, "managedBy": str}
    skills: list[Any] = field(default_factory=list)
    agents: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.mcp:
            d["mcp"] = self.mcp
        if self.skills:
            d["skills"] = self.skills
        if self.agents:
            d["agents"] = self.agents
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AimDependencies:
        return cls(
            mcp=list(data.get("mcp", [])),
            skills=list(data.get("skills", [])),
            agents=list(data.get("agents", [])),
        )


@dataclass
class Dependencies:
    """External dependencies that KiroCrew should resolve during install.

    ``managedBy`` controls the default installation strategy:
      - ``"gateway"``: KiroCrew runs ``aim install`` for each dependency
      - ``"app"``: KiroCrew only checks existence, does not install

    Individual entries can override via object format:
    ``{"id": "some-mcp", "managedBy": "app"}``
    """

    managedBy: str = "gateway"  # noqa: N815
    aim: AimDependencies = field(default_factory=AimDependencies)
    commands: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.managedBy != "gateway":
            d["managedBy"] = self.managedBy
        aim_d = self.aim.to_dict()
        if aim_d:
            d["aim"] = aim_d
        if self.commands:
            d["commands"] = self.commands
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Dependencies:
        aim_raw = data.get("aim", {})
        aim = AimDependencies.from_dict(aim_raw) if isinstance(aim_raw, dict) else AimDependencies()
        return cls(
            managedBy=str(data.get("managedBy", "gateway")),  # noqa: N815
            aim=aim,
            commands=[str(c) for c in data.get("commands", [])],
        )


@dataclass
class ClientInstallConfig:
    """Instructions for installing an app on the user's local machine.

    Used when KiroCrew runs remotely (e.g. cloud desktop) and the app
    requires a specific local platform (e.g. macOS for Electron apps).
    """

    shell: str = ""  # one-liner for the user to run in their terminal
    postInstall: str = ""  # command to run after install (e.g. "open ~/Applications/Mochi.app")  # noqa: N815

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.shell:
            d["shell"] = self.shell
        if self.postInstall:
            d["postInstall"] = self.postInstall
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClientInstallConfig:
        return cls(
            shell=str(data.get("shell", "")),
            postInstall=str(data.get("postInstall", "")),  # noqa: N815
        )


@dataclass
class PlatformConfig:
    """Platform requirements and install mode for an app.

    ``os`` declares which platforms the app can run on.
    ``installMode`` controls how the App Store handles installation:

    - ``"server"`` (default): KiroCrew clones + installs on the server.
    - ``"client"``: Must be installed on the user's local machine.
      When KiroCrew is on an incompatible platform, the App Store shows
      copy-paste terminal instructions instead of running the install.
    """

    os: list[str] = field(default_factory=lambda: ["macos", "linux"])
    arch: list[str] = field(default_factory=list)  # empty = any arch
    installMode: str = "server"  # "server" | "client"  # noqa: N815
    clientInstall: ClientInstallConfig = field(default_factory=ClientInstallConfig)  # noqa: N815

    # Map user-friendly OS names to sys.platform values
    _OS_TO_PLATFORM = {"macos": "darwin", "linux": "linux"}
    _PLATFORM_TO_OS = {"darwin": "macos", "linux": "linux"}

    def supports_platform(self, sys_platform: str) -> bool:
        """Check if this platform config supports the given sys.platform value."""
        return sys_platform in {self._OS_TO_PLATFORM.get(o, o) for o in self.os}

    @staticmethod
    def current_os() -> str:
        """Return the user-friendly OS name for the current platform."""
        import sys
        return PlatformConfig._PLATFORM_TO_OS.get(sys.platform, sys.platform)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.os != ["macos", "linux"]:
            d["os"] = self.os
        if self.arch:
            d["arch"] = self.arch
        if self.installMode != "server":
            d["installMode"] = self.installMode
        ci = self.clientInstall.to_dict()
        if ci:
            d["clientInstall"] = ci
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlatformConfig:
        ci_raw = data.get("clientInstall", {})
        ci = ClientInstallConfig.from_dict(ci_raw) if isinstance(ci_raw, dict) else ClientInstallConfig()
        return cls(
            os=[str(o) for o in data.get("os", ["macos", "linux"])],
            arch=[str(a) for a in data.get("arch", [])],
            installMode=str(data.get("installMode", "server")),  # noqa: N815
            clientInstall=ci,  # noqa: N815
        )


@dataclass
class PublishProviderConfig:
    """Declares an external publish destination this app contributes to the core
    artifact-page publish registry (design §1.3, Route B).

    Core aggregates the **enabled + configured** providers via
    ``GET /api/publish-providers`` and renders a publish action per provider on the
    artifact page. Core never imports app code — it only reads this declaration and
    calls ``endpoint``. ``configFile`` / ``configuredField`` let core resolve the
    "configured" state by reading the app's own persisted config (under the app's
    ``data/`` dir) without invoking the app.
    """

    id: str = ""  # stable provider id, e.g. "deploy-web-aws"
    label: str = ""  # action label, e.g. "Publish to public web (your AWS)"
    icon: str = ""  # lucide icon name
    endpoint: str = ""  # app backend route the artifact page posts to (e.g. /api/apps/deploy-web/deploy)
    kinds: list[str] = field(default_factory=list)  # supported artifact kinds (empty = all)
    setupRoute: str = ""  # UI route to the app's setup/console page  # noqa: N815
    configFile: str = "config.json"  # relative to <app_dir>/data/  # noqa: N815
    configuredField: str = ""  # field in configFile that must be non-empty to count as configured  # noqa: N815

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.id:
            d["id"] = self.id
        if self.label:
            d["label"] = self.label
        if self.icon:
            d["icon"] = self.icon
        if self.endpoint:
            d["endpoint"] = self.endpoint
        if self.kinds:
            d["kinds"] = self.kinds
        if self.setupRoute:
            d["setupRoute"] = self.setupRoute
        if self.configFile != "config.json":
            d["configFile"] = self.configFile
        if self.configuredField:
            d["configuredField"] = self.configuredField
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PublishProviderConfig:
        return cls(
            id=str(data.get("id", "")),
            label=str(data.get("label", "")),
            icon=str(data.get("icon", "")),
            endpoint=str(data.get("endpoint", "")),
            kinds=[str(k) for k in data.get("kinds", []) if k],
            setupRoute=str(data.get("setupRoute", "")),  # noqa: N815
            configFile=str(data.get("configFile", "config.json")),  # noqa: N815
            configuredField=str(data.get("configuredField", "")),  # noqa: N815
        )


# ---------------------------------------------------------------------------
# Main AppManifest
# ---------------------------------------------------------------------------

# Fields that are parsed into typed dataclass attributes
_KNOWN_FIELDS = frozenset({
    "name", "version", "displayName", "description", "author", "license",
    "minKiroCrewVersion", "signer", "signature", "agents", "skills", "sops",
    "mcpServers", "crons", "ui", "backend", "permissions", "setup", "tags",
    "jobFamilies", "platform", "dependencies", "publishProvider",
})


@dataclass
class AppManifest:
    """Static metadata for a KiroCrew app — readable without executing app code.

    Parsed from ``app.json`` at the root of an app package.  Follows the same
    pattern as :class:`~kiro_crew.plugins.manifest.PluginManifest`: dataclass
    with ``validate`` / ``to_dict`` / ``from_dict`` / round-trip support.
    """

    # --- Required ---
    name: str = ""  # unique identifier, kebab-case
    version: str = ""  # semver string
    displayName: str = ""  # human-readable name  # noqa: N815
    description: str = ""  # short summary

    # --- Recommended ---
    author: str = ""
    license: str = ""
    minKiroCrewVersion: str = ""  # noqa: N815
    signer: str = ""  # publisher/signer id, keyed into the fleet admission trust_keys
    signature: str = ""  # detached signature over signing_payload() (verified by admission)

    # --- Agent resources ---
    agents: list[str] = field(default_factory=list)  # paths to agent JSON files
    skills: list[str] = field(default_factory=list)  # paths to skill directories
    sops: list[str] = field(default_factory=list)  # paths to SOP files
    mcpServers: dict[str, Any] = field(default_factory=dict)  # MCP server configs  # noqa: N815

    # --- Scheduling ---
    crons: list[CronEntry] = field(default_factory=list)

    # --- Frontend ---
    ui: UIConfig = field(default_factory=UIConfig)

    # --- Backend ---
    backend: BackendConfig = field(default_factory=BackendConfig)

    # --- Permissions ---
    permissions: Permissions = field(default_factory=Permissions)

    # --- Setup ---
    setup: SetupConfig = field(default_factory=SetupConfig)

    # --- Dependencies ---
    dependencies: Dependencies = field(default_factory=Dependencies)

    # --- Platform ---
    platform: PlatformConfig = field(default_factory=PlatformConfig)

    # --- Publish registry (Route B, §1.3) ---
    publishProvider: PublishProviderConfig = field(default_factory=PublishProviderConfig)  # noqa: N815

    # --- Discovery ---
    tags: list[str] = field(default_factory=list)
    jobFamilies: list[str] = field(default_factory=list)  # noqa: N815

    # --- Forward compatibility ---
    extra: dict[str, Any] = field(default_factory=dict)

    # -----------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------

    def validate(self, app_root: Path | None = None) -> list[str]:
        """Return list of validation errors (empty list means valid).

        When ``app_root`` is provided, resource paths are checked for canonical
        containment (resolve + is_relative_to) against it; otherwise a lexical
        check (reject absolute paths and ``..`` segments) is applied.
        """
        errors: list[str] = []

        # Required fields
        if not self.name:
            errors.append("missing required field: name")
        elif not KEBAB_RE.match(self.name):
            errors.append(
                f"name must be kebab-case (lowercase alphanumeric + hyphens), got: {self.name!r}"
            )

        if not self.version:
            errors.append("missing required field: version")
        elif not SEMVER_RE.match(self.version):
            errors.append(f"version must be semver (e.g. 1.0.0), got: {self.version!r}")

        if not self.displayName:
            errors.append("missing required field: displayName")

        if not self.description:
            errors.append("missing required field: description")

        # Path containment check on all app-root-relative resource paths.
        # Canonical (resolve + is_relative_to) when app_root is known; lexical
        # (reject absolute + '..' segments) otherwise. Applied uniformly to
        # agents/skills/sops, ui.entry, ui.pages[].entryPoint AND
        # backend.entryPoint. (A module-style dotted backend.entryPoint such as
        # 'kiro_crew.apps.builtins.x.server' has no '..' and is not absolute, so
        # the helper never false-positives on it.)
        for path_list_name in ("agents", "skills", "sops"):
            for p in getattr(self, path_list_name):
                if _path_escapes_app_root(str(p), app_root):
                    errors.append(
                        f"{path_list_name} path contains path traversal: {p!r}"
                    )

        if self.ui.entry and _path_escapes_app_root(self.ui.entry, app_root):
            errors.append(f"ui.entry contains path traversal: {self.ui.entry!r}")

        if self.backend.entryPoint and _path_escapes_app_root(self.backend.entryPoint, app_root):
            errors.append(
                f"backend.entryPoint contains path traversal: {self.backend.entryPoint!r}"
            )

        # UI page validation
        for page in self.ui.pages:
            if not page.route:
                errors.append("ui page missing required field: route")
            if not page.label:
                errors.append("ui page missing required field: label")
            if page.entryPoint and _path_escapes_app_root(page.entryPoint, app_root):
                errors.append(
                    f"ui page entryPoint contains path traversal: {page.entryPoint!r}"
                )

        # Cron validation
        for cron in self.crons:
            if not cron.name:
                errors.append("cron entry missing required field: name")
            if not cron.every and not cron.cron_expr:
                errors.append(
                    f"cron entry {cron.name!r} must specify either 'every' or 'cron_expr'"
                )

        # Backend hooks validation
        errors.extend(self.backend.hooks.validate())

        return errors

    def signing_payload(self) -> bytes:
        """Canonical bytes an admission signature covers (manifest minus the
        signature). Deterministic across field ordering so a future admission
        verify has a stable payload over name/version/signer/permissions."""
        body = {
            "name": self.name,
            "version": self.version,
            "signer": self.signer,
            "permissions": self.permissions.to_dict(),
        }
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")

    # -----------------------------------------------------------------
    # Serialization
    # -----------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict, including extra fields."""
        d: dict[str, Any] = {
            "name": self.name,
            "version": self.version,
            "displayName": self.displayName,
            "description": self.description,
        }
        if self.author:
            d["author"] = self.author
        if self.license:
            d["license"] = self.license
        if self.minKiroCrewVersion:
            d["minKiroCrewVersion"] = self.minKiroCrewVersion
        if self.signer:
            d["signer"] = self.signer
        if self.signature:
            d["signature"] = self.signature
        if self.agents:
            d["agents"] = self.agents
        if self.skills:
            d["skills"] = self.skills
        if self.sops:
            d["sops"] = self.sops
        if self.mcpServers:
            d["mcpServers"] = self.mcpServers
        if self.crons:
            d["crons"] = [c.to_dict() for c in self.crons]
        ui_d = self.ui.to_dict()
        if ui_d:
            d["ui"] = ui_d
        backend_d = self.backend.to_dict()
        if backend_d:
            d["backend"] = backend_d
        perms_d = self.permissions.to_dict()
        if perms_d:
            d["permissions"] = perms_d
        setup_d = self.setup.to_dict()
        if setup_d:
            d["setup"] = setup_d
        deps_d = self.dependencies.to_dict()
        if deps_d:
            d["dependencies"] = deps_d
        platform_d = self.platform.to_dict()
        if platform_d:
            d["platform"] = platform_d
        pp_d = self.publishProvider.to_dict()
        if pp_d:
            d["publishProvider"] = pp_d
        if self.tags:
            d["tags"] = self.tags
        if self.jobFamilies:
            d["jobFamilies"] = self.jobFamilies
        # Preserve unknown fields for forward compatibility
        d.update(self.extra)
        return d

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), indent=2)

    # -----------------------------------------------------------------
    # Parsing
    # -----------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppManifest:
        """Parse from dict, preserving unknown fields in ``extra``."""
        extra = {k: v for k, v in data.items() if k not in _KNOWN_FIELDS}

        crons_raw = data.get("crons", [])
        crons = [CronEntry.from_dict(c) for c in crons_raw if isinstance(c, dict)]

        ui_raw = data.get("ui", {})
        ui = UIConfig.from_dict(ui_raw) if isinstance(ui_raw, dict) else UIConfig()

        backend_raw = data.get("backend", {})
        backend = (
            BackendConfig.from_dict(backend_raw)
            if isinstance(backend_raw, dict)
            else BackendConfig()
        )

        perms_raw = data.get("permissions", {})
        permissions = (
            Permissions.from_dict(perms_raw)
            if isinstance(perms_raw, dict)
            else Permissions()
        )

        setup_raw = data.get("setup", {})
        setup = (
            SetupConfig.from_dict(setup_raw)
            if isinstance(setup_raw, dict)
            else SetupConfig()
        )

        deps_raw = data.get("dependencies", {})
        deps = (
            Dependencies.from_dict(deps_raw)
            if isinstance(deps_raw, dict)
            else Dependencies()
        )

        platform_raw = data.get("platform", {})
        platform_cfg = (
            PlatformConfig.from_dict(platform_raw)
            if isinstance(platform_raw, dict)
            else PlatformConfig()
        )

        pp_raw = data.get("publishProvider", {})
        publish_provider = (
            PublishProviderConfig.from_dict(pp_raw)
            if isinstance(pp_raw, dict)
            else PublishProviderConfig()
        )

        return cls(
            name=str(data.get("name", "")),
            version=str(data.get("version", "")),
            displayName=str(data.get("displayName", "")),  # noqa: N815
            description=str(data.get("description", "")),
            author=str(data.get("author", "")),
            license=str(data.get("license", "")),
            minKiroCrewVersion=str(data.get("minKiroCrewVersion", "")),  # noqa: N815
            signer=str(data.get("signer", "")),
            signature=str(data.get("signature", "")),
            agents=[str(a) for a in data.get("agents", []) if a],
            skills=[
                str(s.get("path", s.get("name", "")) if isinstance(s, dict) else s)
                for s in data.get("skills", [])
                if s
            ],
            sops=[str(s) for s in data.get("sops", []) if s],
            mcpServers=dict(data.get("mcpServers", {})),  # noqa: N815
            crons=crons,
            ui=ui,
            backend=backend,
            permissions=permissions,
            setup=setup,
            dependencies=deps,
            platform=platform_cfg,
            publishProvider=publish_provider,
            tags=[str(t) for t in data.get("tags", []) if t],
            jobFamilies=[str(j) for j in data.get("jobFamilies", []) if j],  # noqa: N815
            extra=extra,
        )

    @classmethod
    def from_json_file(cls, path: Path) -> AppManifest:
        """Parse from an ``app.json`` file."""
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"app.json must be a JSON object, got {type(data).__name__}")
        return cls.from_dict(data)
