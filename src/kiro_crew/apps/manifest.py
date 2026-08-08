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
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

# ---------------------------------------------------------------------------
# Nested manifest types
# ---------------------------------------------------------------------------

KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+([+-]|$)")

# App names that would collide with reserved notification namespaces: an app
# named "system" would produce channels "system.<id>", shadowing the bus's
# reserved system channels (e.g. "system.approval"). Rejected at manifest
# validation AND defense-in-depth at the push endpoint.
RESERVED_APP_NAMES = frozenset({"system"})


def _is_rooted_path(rel_path: str) -> bool:
    """Return True if ``rel_path`` is anything other than a purely relative path.

    App-resource paths are joined onto the app root, so any path carrying a drive
    or a root anchor can relocate that join and must be refused. ``is_absolute()``
    is too narrow twice over:

    - It is flavour-bound to the RUNNING host, and ``os.path`` IS ``ntpath`` on
      Windows, so ``os.path.isabs(x) or ntpath.isabs(x)`` collapses to one
      Windows-only test there. Windows' flavour does not consider ``/etc/passwd``
      anchored (no drive), so a POSIX-absolute path passed validation on Windows.
      The Windows flavour is a strict superset — it reads ``/`` and ``\\`` as a
      root — so testing it alone covers both syntaxes on either host.
    - A drive-relative path (``D:evil.py``) has a drive but no root, so
      ``is_absolute()`` is False, yet joining it onto the app root yields
      ``D:evil.py`` and escapes. Hence drive-OR-root, not ``is_absolute()``.
    """
    win = PureWindowsPath(rel_path)
    return bool(win.drive or win.root)


def _has_dotdot_segment(rel_path: str) -> bool:
    """Return True if ``rel_path`` contains a ``..`` segment under EITHER flavour.

    Segmenting both ways is load-bearing: a POSIX host reads ``..\\evil`` as one
    opaque filename, so a POSIX-only split lets a backslash traversal through —
    and the manifest that declares it is portable data, validated on whichever
    host happens to install the app. No legitimate resource path has a bare
    ``..`` segment (``a..b`` and ``notes..md`` are single segments and unaffected).
    """
    return ".." in PurePosixPath(rel_path).parts or ".." in PureWindowsPath(rel_path).parts


def _path_escapes_app_root(rel_path: str, app_root: Path | None) -> bool:
    """Return True if ``rel_path`` is an unsafe app-resource path.

    Unsafe = rooted (drive-qualified, POSIX-absolute, or UNC), or containing a
    ``..`` segment, or — resolved against ``app_root`` — escaping ``app_root``
    (canonical containment, matching the runtime checks in ``module_loader`` /
    ``bridges``).

    The lexical checks run BEFORE the canonical one and regardless of whether
    ``app_root`` is known, so a manifest is judged identically on every host.
    Deferring them to ``resolve()`` would make the verdict host-dependent: on a
    POSIX host ``app_root / "..\\evil.py"`` is a single odd filename that stays
    inside the root and would be accepted, while the same manifest is rejected on
    Windows. Canonical containment then adds what no lexical check can see — a
    symlink or reparse point inside the root whose target leaves it.
    """
    if not rel_path:
        return False
    if _is_rooted_path(rel_path) or _has_dotdot_segment(rel_path):
        return True
    if app_root is not None:
        try:
            resolved = (app_root / rel_path).resolve()
            return not resolved.is_relative_to(app_root.resolve())
        except (OSError, ValueError):
            return True
    return False


@dataclass
class CronEntry:
    """A scheduled agent job declared by an app."""

    name: str = ""
    every: int = 0  # seconds between runs (0 = use cron_expr)
    cron_expr: str = ""  # cron expression (alternative to every)
    agent: str = ""  # agent name to run
    message: str = ""  # prompt message for the agent
    command: str = ""  # shell command for direct execution (bypasses LLM)
    script: str = ""  # Python callable path (file.py:func) for direct execution (bypasses LLM)
    # Extended fields for advanced scheduling
    agent_sequence: list[str] = field(default_factory=list)  # ordered list of agents to run
    env: dict[str, str] = field(default_factory=dict)  # environment variables for the job
    persistent_session: bool = True  # whether to carry context between runs
    silent: bool = False  # suppress dashboard notifications
    # When False the cron is registered in a paused state (visible in the
    # dashboard Schedule view, resumable) instead of firing on install/enable.
    # Apps that need user configuration before their crons are useful ship
    # them disabled and enable/resume once configured.
    enabled: bool = True
    # Parse-time type violation: manifest "enabled" was present but not a JSON
    # boolean (e.g. the string "false", which bool() would coerce truthy —
    # silently re-creating the fires-unconfigured bug). Reported as a
    # validation error; never serialized.
    enabled_type_invalid: bool = False

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
        if self.command:
            d["command"] = self.command
        if self.script:
            d["script"] = self.script
        if self.agent_sequence:
            d["agent_sequence"] = self.agent_sequence
        if self.env:
            d["env"] = self.env
        if not self.persistent_session:
            d["persistent_session"] = False
        if self.silent:
            d["silent"] = True
        if not self.enabled:
            d["enabled"] = False
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CronEntry:
        def _str_or_empty(v: Any) -> str:
            return v if isinstance(v, str) else ""

        return cls(
            name=_str_or_empty(data.get("name")),
            every=int(data.get("every", 0)),
            cron_expr=_str_or_empty(data.get("cron_expr")),
            agent=_str_or_empty(data.get("agent")),
            message=_str_or_empty(data.get("message")),
            command=_str_or_empty(data.get("command")),
            script=_str_or_empty(data.get("script")),
            agent_sequence=[str(a) for a in data.get("agent_sequence", [])],
            env={str(k): str(v) for k, v in data.get("env", {}).items()},
            persistent_session=bool(data.get("persistent_session", True)),
            silent=bool(data.get("silent", False)),
            # STRICT boolean: "enabled" gates whether a cron fires at all, so a
            # type slip (the string "false" is truthy) must not silently
            # re-enable a disabled-by-design cron. Non-boolean values are
            # flagged and rejected by AppManifest.validate(); the value falls
            # back to True only so the flagged manifest still round-trips.
            enabled=(data["enabled"] if isinstance(data.get("enabled"), bool) else True),
            enabled_type_invalid=("enabled" in data and not isinstance(data["enabled"], bool)),
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
    type: str = ""  # "python", "asgi", "node", "exec", or "" (auto-detect)
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


def _granted_list(value: Any) -> list[str]:
    """The entries of a list-valued GRANT, or nothing if it is not a list.

    A JSON scalar must NOT be coerced. `[str(x) for x in value]` over a STRING
    iterates its characters, so `"exposeToApps": "*"` would yield `["*"]` -- the
    wildcard -- and any string containing `*` or `/` produces that token too:
    `"api": "/api/chat"` gives the prefix `"/"`, which `app_token_path_allowed`
    matches against every path. A malformed grant has to deny, the same direction
    the boolean grants below fail in.
    """
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if v]


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
    #: May spawn a background agent through the host's subagent manager.
    #: Declared rather than implicit so "which apps can start an agent" is
    #: auditable from the manifest instead of from an app's import graph.
    spawn: bool = False
    # WS cross-app visibility opt-in: app names (or ["*"]) allowed to use
    # slots:app:<this-app> / subagent:app:<this-app> declarations to observe
    # this app's slots and subagents. Empty list = no cross-app visibility.
    exposeToApps: list[str] = field(default_factory=list)  # noqa: N815

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
        if self.spawn:
            d["spawn"] = True
        if self.exposeToApps:
            d["exposeToApps"] = self.exposeToApps
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Permissions:
        # `is True`, not `bool(...)`, for every CAPABILITY GRANT below. `bool()` on a
        # JSON value that is not a boolean grants on anything truthy — and the
        # string `"false"` is truthy, so a manifest that writes `"spawn": "false"`
        # meaning to DENY would have been handed the capability to launch
        # unattended agents. Requiring the literal `true` means a malformed or
        # unexpected value denies, which is the direction a grant must fail in.
        #
        # Note this is the opposite coercion from a RESTRICTION (see
        # `admission.require_signature`): there, an unexpected value must keep the
        # restriction ON. Same defect class, mirrored fix — the safe default
        # follows what the field grants or withholds, not the field's type.
        return cls(
            api=_granted_list(data.get("api")),
            events=_granted_list(data.get("events")),
            mcpTools=_granted_list(data.get("mcpTools")),  # noqa: N815
            storage=data.get("storage") is True,
            network=data.get("network") is True,
            memory=str(data.get("memory", "")),
            cron=data.get("cron") is True,
            spawn=data.get("spawn") is True,
            exposeToApps=_granted_list(data.get("exposeToApps")),  # noqa: N815
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
class CapabilityDependencies:
    """Capability-manager-provided dependencies (MCP servers, skills, agents).

    Resolved through the ``CapabilityManager`` CPP seam, NOT a named external
    binary — the edition owns which package manager (if any) backs these and its
    invocation grammar.  The public edition ships no capability manager, so these
    entries are reported as unresolved rather than installed.
    """

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
    def from_dict(cls, data: dict[str, Any]) -> CapabilityDependencies:
        return cls(
            mcp=list(data.get("mcp", [])),
            skills=list(data.get("skills", [])),
            agents=list(data.get("agents", [])),
        )


@dataclass
class Dependencies:
    """External dependencies that KiroCrew should resolve during install.

    ``managedBy`` controls the default installation strategy:
      - ``"gateway"``: KiroCrew resolves each dependency through the edition's
        ``CapabilityManager`` seam
      - ``"app"``: KiroCrew only checks existence, does not install

    Individual entries can override via object format:
    ``{"id": "some-mcp", "managedBy": "app"}``

    The wire key is ``capabilities``.  ``aim`` is accepted as a DEPRECATED read
    alias so manifests authored against the pre-rename schema keep loading; it is
    never re-emitted by :meth:`to_dict`, so a round-trip migrates the manifest.

    ``commands`` are REQUIRED host executables — a missing one is reported as a
    missing dependency.  ``optionalCommands`` are probed and reported but never
    block: an app that works without them, or that can provision its own copy,
    declares them here.  Two manifests in-tree already used the key while the
    dataclass silently dropped it (``papyrus`` round-tripped to ``{}``,
    ``issue_radar`` lost ``glab``), so the requirement was invisible to every
    consumer — that is the bug this field closes, not a new feature.
    """

    managedBy: str = "gateway"  # noqa: N815
    capabilities: CapabilityDependencies = field(default_factory=CapabilityDependencies)
    commands: list[str] = field(default_factory=list)
    optionalCommands: list[str] = field(default_factory=list)  # noqa: N815

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.managedBy != "gateway":
            d["managedBy"] = self.managedBy
        cap_d = self.capabilities.to_dict()
        if cap_d:
            d["capabilities"] = cap_d
        if self.commands:
            d["commands"] = self.commands
        if self.optionalCommands:
            d["optionalCommands"] = self.optionalCommands
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Dependencies:
        raw = data.get("capabilities")
        if not isinstance(raw, dict):
            # Deprecated alias — read-only, never written back.
            raw = data.get("aim")
        capabilities = (
            CapabilityDependencies.from_dict(raw)
            if isinstance(raw, dict)
            else CapabilityDependencies()
        )
        return cls(
            managedBy=str(data.get("managedBy", "gateway")),  # noqa: N815
            capabilities=capabilities,
            # `or []`, not just a `[]` default: an explicit `"commands": null` in a
            # manifest satisfies `.get`'s default and then fails to iterate, so the
            # install endpoint answered an unhandled TypeError as a 500 rather than a
            # validation error. A hand-written or generated app.json can carry a JSON
            # null for an absent list, and a manifest parser reads UNTRUSTED input —
            # it must degrade to "empty", never crash. Both lines, since the
            # pre-existing `commands` had the identical shape.
            commands=[str(c) for c in (data.get("commands") or [])],
            optionalCommands=[  # noqa: N815
                str(c) for c in (data.get("optionalCommands") or [])
            ],
        )


@dataclass
class ClientInstallConfig:
    """Instructions for installing an app on the user's local machine.

    Used when KiroCrew runs remotely (e.g. cloud desktop) and the app
    requires a specific local platform (e.g. macOS for Electron apps).
    """

    shell: str = ""  # one-liner for the user to run in their terminal
    postInstall: str = (
        ""  # command to run after install (e.g. "open ~/Applications/Mochi.app")  # noqa: N815
    )

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

    ``requiresDesktopApp`` is a different axis from ``os``: ``os`` constrains
    the machine the GATEWAY runs on, while this constrains the SURFACE the user
    is viewing from. An app sets it when its UI needs capabilities only the
    Electron shell provides (native always-on-top windows, global shortcuts,
    tray, screen capture) and would be broken or pointless in a browser tab.
    The App Store surfaces the requirement and withholds the enable action from
    browser sessions.

    It is a UX gate, not a security boundary: the browser marker
    (``window.kirocrew.isElectron``, set by the shell's preload) is client-side
    and therefore spoofable. Nothing security-relevant may depend on it — an app
    whose BACKEND must not run outside the desktop needs a real server-side
    check, not this flag.
    """

    os: list[str] = field(default_factory=lambda: ["macos", "linux"])
    arch: list[str] = field(default_factory=list)  # empty = any arch
    installMode: str = "server"  # "server" | "client"  # noqa: N815
    clientInstall: ClientInstallConfig = field(default_factory=ClientInstallConfig)  # noqa: N815
    requiresDesktopApp: bool = False  # noqa: N815

    # Map user-friendly OS names to sys.platform values.
    #
    # ``windows`` is in both directions because KiroCrew itself supports Windows
    # natively (see ``docs/guides/windows-install.md``): without the row, ``"windows"``
    # was not even EXPRESSIBLE in a manifest — a declaring app silently never
    # matched — and ``current_os()`` fell through to the raw ``"win32"``, which is
    # not one of the user-friendly names any manifest or UI compares against.
    #
    # The default below stays ``["macos", "linux"]``. Widening it would silently
    # promise Windows on behalf of every existing app, and an app opts in by
    # naming ``windows`` itself.
    _OS_TO_PLATFORM = {"macos": "darwin", "linux": "linux", "windows": "win32"}
    _PLATFORM_TO_OS = {"darwin": "macos", "linux": "linux", "win32": "windows"}

    def supports_platform(self, sys_platform: str) -> bool:
        """Check if this platform config supports the given sys.platform value."""
        return sys_platform in {self._OS_TO_PLATFORM.get(o, o) for o in self.os}

    @staticmethod
    def current_os() -> str:
        """Return the user-friendly OS name for the current platform."""
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
        if self.requiresDesktopApp:
            d["requiresDesktopApp"] = True
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlatformConfig:
        ci_raw = data.get("clientInstall", {})
        ci = (
            ClientInstallConfig.from_dict(ci_raw)
            if isinstance(ci_raw, dict)
            else ClientInstallConfig()
        )
        return cls(
            os=[str(o) for o in data.get("os", ["macos", "linux"])],
            arch=[str(a) for a in data.get("arch", [])],
            installMode=str(data.get("installMode", "server")),  # noqa: N815
            requiresDesktopApp=bool(data.get("requiresDesktopApp", False)),  # noqa: N815
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
    endpoint: str = (
        ""  # app backend route the artifact page posts to (e.g. /api/apps/deploy-web/deploy)
    )
    kinds: list[str] = field(default_factory=list)  # supported artifact kinds (empty = all)
    setupRoute: str = ""  # UI route to the app's setup/console page  # noqa: N815
    configFile: str = "config.json"  # relative to <app_dir>/data/  # noqa: N815
    configuredField: str = (
        ""  # field in configFile that must be non-empty to count as configured  # noqa: N815
    )

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
@dataclass
class NotificationChannel:
    """A producer-declared notification channel (RFC local notification bus, Phase 2)."""

    id: str = ""  # kebab-case, unique within the app
    name: str = ""  # human-readable display name for settings UI
    icon: str = ""  # lucide icon name (optional)
    defaultPriority: str = "default"  # "critical" | "default" | "passive"  # noqa: N815

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "name": self.name}
        if self.icon:
            d["icon"] = self.icon
        if self.defaultPriority != "default":
            d["defaultPriority"] = self.defaultPriority
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NotificationChannel:
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            icon=str(data.get("icon", "")),
            defaultPriority=str(data.get("defaultPriority", "default")),  # noqa: N815
        )


# Maximum declared channels per app (RFC "Channel registry"): keeps the
# per-channel settings surface bounded; raising later is free, lowering after
# apps ship with more is a breaking change.
MAX_NOTIFICATION_CHANNELS = 8

_CHANNEL_PRIORITIES = ("critical", "default", "passive")


@dataclass
class NotificationsConfig:
    """Notification channel declarations for an app."""

    channels: list[NotificationChannel] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        if not self.channels:
            return {}
        return {"channels": [c.to_dict() for c in self.channels]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NotificationsConfig:
        raw = data.get("channels", [])
        channels = [NotificationChannel.from_dict(c) for c in raw if isinstance(c, dict)]
        return cls(channels=channels)

    def validate(self) -> list[str]:
        errors: list[str] = []
        if len(self.channels) > MAX_NOTIFICATION_CHANNELS:
            errors.append(
                f"notifications.channels: at most {MAX_NOTIFICATION_CHANNELS} channels "
                f"per app, got {len(self.channels)}"
            )
        seen: set[str] = set()
        for ch in self.channels:
            if not ch.id:
                errors.append("notifications.channels: channel missing required field: id")
                continue
            if not KEBAB_RE.match(ch.id):
                errors.append(f"notifications.channels: channel id must be kebab-case: {ch.id!r}")
            if ch.id in seen:
                errors.append(f"notifications.channels: duplicate channel id: {ch.id!r}")
            seen.add(ch.id)
            if not ch.name:
                errors.append(f"notifications.channels: channel {ch.id!r} missing name")
            if ch.defaultPriority not in _CHANNEL_PRIORITIES:
                errors.append(
                    f"notifications.channels: channel {ch.id!r} defaultPriority must be "
                    f"one of {_CHANNEL_PRIORITIES}, got {ch.defaultPriority!r}"
                )
        return errors


_KNOWN_FIELDS = frozenset(
    {
        "name",
        "version",
        "displayName",
        "description",
        "author",
        "license",
        "minKiroCrewVersion",
        "signer",
        "signature",
        "agents",
        "skills",
        "sops",
        "mcpServers",
        "crons",
        "ui",
        "backend",
        "permissions",
        "setup",
        "tags",
        "jobFamilies",
        "platform",
        "dependencies",
        "publishProvider",
        "notifications",
    }
)


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
    publishProvider: PublishProviderConfig = field(
        default_factory=PublishProviderConfig
    )  # noqa: N815

    # --- Notifications (RFC local notification bus, Phase 2) ---
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)

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
        elif self.name in RESERVED_APP_NAMES:
            errors.append(
                f"app name {self.name!r} is reserved (would shadow the "
                f"{self.name}.* notification channel namespace)"
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
                    errors.append(f"{path_list_name} path contains path traversal: {p!r}")

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
                errors.append(f"ui page entryPoint contains path traversal: {page.entryPoint!r}")

        # Cron validation
        for cron in self.crons:
            if not cron.name:
                errors.append("cron entry missing required field: name")
            if not cron.every and not cron.cron_expr:
                errors.append(
                    f"cron entry {cron.name!r} must specify either 'every' or 'cron_expr'"
                )
            if cron.command and cron.script:
                errors.append(
                    f"cron entry {cron.name!r}: 'command' and 'script' are mutually exclusive"
                )
            if cron.enabled_type_invalid:
                errors.append(
                    f"cron entry {cron.name!r}: 'enabled' must be a JSON boolean "
                    "(true/false) — got a non-boolean value"
                )
            if not (
                cron.agent or cron.agent_sequence or cron.message or cron.command or cron.script
            ):
                errors.append(
                    f"cron entry {cron.name!r}: must specify at least one of "
                    "'agent', 'agent_sequence', 'message', 'command', or 'script'"
                )

        # Backend hooks validation
        errors.extend(self.backend.hooks.validate())

        # Notification channel validation (RFC Phase 2: 8-channel cap, kebab ids)
        errors.extend(self.notifications.validate())

        return errors

    def signing_payload(self) -> bytes:
        """Canonical bytes an admission signature covers (manifest minus the
        signature). Dict keys are sorted for determinism; note the channels
        list keeps its manifest order, so reordering channel declarations is
        a signature-relevant change (intentional -- the signed bytes track
        the manifest as written)."""
        body: dict[str, Any] = {
            "name": self.name,
            "version": self.version,
            "signer": self.signer,
            "permissions": self.permissions.to_dict(),
        }
        if self.notifications.channels:
            # Channel declarations gate what an app may push and at which
            # default priority -- tampering must invalidate the signature.
            # Included only when non-empty so manifests signed before
            # notifications existed keep producing the identical payload.
            body["notifications"] = self.notifications.to_dict()
        if self.crons:
            # Cron declarations can carry `command`/`script` -- a direct
            # code-execution surface. Tampering with a signed app's cron
            # definitions (e.g. swapping the shell command) must invalidate
            # the signature: vetting bounds the SYNTAX of what runs, but only
            # the signature authenticates PUBLISHER INTENT. Each entry's
            # canonical to_dict() (list order preserved -- reordering is a
            # signature-relevant change) covers name/schedule/agent/message/
            # command/script/env. Included only when non-empty so manifests
            # signed before crons existed keep producing the identical payload.
            body["crons"] = [c.to_dict() for c in self.crons]
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
        notif_d = self.notifications.to_dict()
        if notif_d:
            d["notifications"] = notif_d
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
            Permissions.from_dict(perms_raw) if isinstance(perms_raw, dict) else Permissions()
        )

        setup_raw = data.get("setup", {})
        setup = SetupConfig.from_dict(setup_raw) if isinstance(setup_raw, dict) else SetupConfig()

        deps_raw = data.get("dependencies", {})
        deps = Dependencies.from_dict(deps_raw) if isinstance(deps_raw, dict) else Dependencies()

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

        notif_raw = data.get("notifications", {})
        notifications = (
            NotificationsConfig.from_dict(notif_raw)
            if isinstance(notif_raw, dict)
            else NotificationsConfig()
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
            notifications=notifications,
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
