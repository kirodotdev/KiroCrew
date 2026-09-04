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

from kiro_crew.constants import WINDOWS_DEVICE_STEMS
from kiro_crew.cron import is_valid_skip_date, is_valid_timezone

# ---------------------------------------------------------------------------
# Nested manifest types
# ---------------------------------------------------------------------------

# `$` matches at the true end of the string AND just before a trailing newline,
# so this pattern only carries its intended grammar under ``fullmatch``:
# ``KEBAB_RE.match("demo\n")`` succeeds. Any caller gating an identity on it must
# use ``fullmatch`` — see ``app_name_error``.
KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+([+-]|$)")

# App names that would collide with reserved notification namespaces: an app
# named "system" would produce channels "system.<id>", shadowing the bus's
# reserved system channels (e.g. "system.approval"). Rejected at manifest
# validation AND defense-in-depth at the push endpoint.
RESERVED_APP_NAMES = frozenset({"system"})

# Dashboard route segments under ``/apps/`` that resolve to a STATIC page
# instead of the ``/apps/:name`` installed-app catch-all. An app carrying one
# of these names would be unreachable: its exact URL (``/apps/library``) is
# claimed by the page, which is registered before the catch-all. ``detail`` and
# ``migrate`` need no entry here — their routes carry a mandatory second
# segment (``/apps/detail/:name``), so a bare ``/apps/detail`` still resolves
# to an app named "detail" via the catch-all.
RESERVED_ROUTE_APP_NAMES = frozenset({"library"})

# Literal first path segments registered under ``/api/apps/`` that resolve to a
# SHARED route instead of the ``/api/apps/{name}`` installed-app catch-all
# (registry listing/install, blob proxy, install, self-registration, registries
# refresh). Source of truth is the route table in
# ``kiro_crew.apps.routes.setup_routes``; this set is mirrored by
# ``kiro_crew.dashboard.token_auth.RESERVED_APP_PATH_SEGMENTS`` — keep both in
# sync with that table (they are duplicated rather than shared to avoid a
# manifest <-> token_auth import cycle).
#
# The token_auth carve-out is the PRIMARY security boundary: it refuses to treat
# these segments as an app's own ``/api/apps/<name>`` namespace, so even an app
# already published under one of these names cannot implicitly own the shared
# route. Reserving the names here is a forward-looking DEFENSE-IN-DEPTH backstop
# that keeps NEW apps from claiming them at all. As with the other reservations
# in this module, tightening a name is a one-way door — it invalidates an app
# already published under that name — so this affects only names not yet
# admitted; the carve-out is what constrains an already-published app so named.
RESERVED_APP_PATH_SEGMENTS = frozenset({"registry", "registries", "blob", "install", "register"})

#: Wire code for a reserved-name refusal. Callers that turn ``app_name_error``
#: into a JSON error response set this as ``AppResult.error_code`` (serialized
#: as ``code``) so the frontend can switch on the failure instead of parsing
#: English prose (see ``test_error_code_contract.py``).
RESERVED_APP_NAME_CODE = "reserved_app_name"

# App names that are not safe portable filesystem identities. An app name becomes
# a directory (``apps/<name>/``, plus ``apps/<name>/data`` at first startup), and
# Windows reserves these stems as device names by naming contract.
#
# Only ``nul`` has a measured failure here — ``mkdir`` raises WinError 3 on
# Windows 11 26200 via CPython — while the other stems created usable directories
# on that same path. They are refused anyway, as policy rather than reproduction:
# the reservation is a documented Windows naming rule whose observable behaviour
# differs across Windows APIs and builds, so one host's success does not make the
# name portable. An app name is a PERSISTENT published identity, which makes the
# directions asymmetric: admitting a stem is the one-way door, since tightening
# later invalidates an app already published and installed, while relaxing an
# over-strict rule costs nothing.
#
# Rejected on all platforms, not only Windows. Resource paths in this file are
# already validated under both path flavours for the same reason (see
# ``_has_dotdot_segment``), and ``is_valid_followup_branch`` applies this same
# vocabulary to git branch names on every platform so that grammar does not
# depend on where the gateway runs.
#
# The set is lowercase and ``KEBAB_RE`` already forces lowercase, so membership
# is tested directly with no case folding.
UNPORTABLE_APP_NAMES = WINDOWS_DEVICE_STEMS


def app_name_error(name: str) -> str | None:
    """Return why *name* is inadmissible as an app identifier, else ``None``.

    The single app-name contract. Every path that admits a new app funnels
    through here — manifest validation (install, update, discovery), external
    self-registration, and builtin registration — so that a name refused at one
    door cannot be admitted at another. The name is an identity AND a directory
    component, so the same string has to satisfy both roles.
    """
    if not name:
        return "app name must not be empty"
    # fullmatch, not match: the pattern's `$` also matches before a trailing
    # newline, so `match` admits "demo\n" — and the reserved-name comparisons
    # below test the exact string, so "nul\n" and "system\n" would evade those
    # too and reach a backend that stores and joins the RAW name.
    if not KEBAB_RE.fullmatch(name):
        return f"app name must be kebab-case (lowercase alphanumeric + hyphens): {name!r}"
    if name in RESERVED_APP_NAMES:
        return (
            f"app name {name!r} is reserved (would shadow the "
            f"{name}.* notification channel namespace)"
        )
    if name in RESERVED_ROUTE_APP_NAMES:
        return (
            f"app name {name!r} is reserved (the dashboard /apps/{name} route is a "
            f"static page, so the app's own page would be unreachable)"
        )
    if name in RESERVED_APP_PATH_SEGMENTS:
        return (
            f"app name {name!r} is reserved (the /api/apps/{name} path is a shared "
            f"literal route registered before the /api/apps/{{name}} catch-all, so the "
            f"name would collide with that route)"
        )
    if name in UNPORTABLE_APP_NAMES:
        return (
            f"app name {name!r} is not portable: Windows reserves it as a device name, "
            f"so the app directory is not safe to create there"
        )
    return None


def is_reserved_app_name(name: str) -> bool:
    """Return True if *name* is refused solely because it is reserved.

    Lets callers that translate ``app_name_error`` prose into a JSON error
    attach the machine-readable ``RESERVED_APP_NAME_CODE`` for exactly the
    reserved-name refusals, without re-deriving the reservation sets.
    """
    return (
        name in RESERVED_APP_NAMES
        or name in RESERVED_ROUTE_APP_NAMES
        or name in RESERVED_APP_PATH_SEGMENTS
    )


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


# Expected JSON type per CronEntry field that from_dict type-gates, used to turn
# a recorded parse-time violation into a message an app author can act on.
_CRON_FIELD_JSON_TYPES = {
    "every": "a number of seconds",
    "agent_sequence": "an array of agent names",
    "env": "an object of string keys to string values",
    "timezone": "a string IANA zone name",
    "skip_dates": "an array of YYYY-MM-DD strings",
}


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
    # IANA zone the schedule and skip_dates are evaluated in (e.g.
    # "America/New_York"). Empty falls back to the gateway config's timezone and
    # then to UTC, so a job whose hour is only meaningful in one zone -- market
    # hours, a regional business-day digest -- must name it here. A per-USER zone
    # is not manifest data: an app that schedules against its user's local time
    # passes ``timezone`` to ``ctx.cron.add_job`` instead.
    timezone: str = ""
    # Calendar dates (YYYY-MM-DD, evaluated in ``timezone``) the job must not
    # fire on -- e.g. a publisher's own holiday list.
    skip_dates: list[str] = field(default_factory=list)
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
    # Names of container/numeric fields whose manifest value was PRESENT and
    # non-null but of the wrong JSON type. Parsing degrades them to the field's
    # empty value so /api/apps/register cannot 500, and validate() reports each
    # one -- erasing a wrong-typed value SILENTLY would be its own bug: an
    # author who wrote "skip_dates": "2026-12-25" (a string, not an array) asked
    # for a skip, and dropping it without a word lets the job fire on the
    # excluded date. Same shape as enabled_type_invalid; never serialized.
    type_invalid_fields: list[str] = field(default_factory=list)

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
        if self.timezone:
            d["timezone"] = self.timezone
        if self.skip_dates:
            d["skip_dates"] = self.skip_dates
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

        # app.json is third-party, hand-editable input reached from
        # /api/apps/register, so a wrong JSON TYPE must not raise:
        # `data.get(key, [])` defends only the ABSENT key, and an explicit
        # `"skip_dates": null` (or a scalar, or an object) returns that value,
        # after which the comprehension raises TypeError / AttributeError out of
        # from_dict and surfaces as an HTTP 500 rather than a validation error.
        # Two distinct cases, deliberately treated differently:
        #   * null == "not set" -> the empty value, no error (mirrors
        #     _str_or_empty, and JSON null is how generators spell "absent").
        #   * present, non-null, WRONG type -> the empty value AND a recorded
        #     violation, because silently erasing it would drop a skip date the
        #     author asked for and let the job fire on that date.
        invalid: list[str] = []

        def _list_or_empty(key: str, v: Any) -> list[Any]:
            if isinstance(v, list):
                return v
            if v is not None:
                invalid.append(key)
            return []

        def _dict_or_empty(key: str, v: Any) -> dict[Any, Any]:
            if isinstance(v, dict):
                return v
            if v is not None:
                invalid.append(key)
            return {}

        def _int_or_zero(key: str, v: Any) -> int:
            # bool is an int subclass, so `"every": true` would pass isinstance
            # and coerce to a 1-second interval; it is a type slip, not a
            # schedule. 0 is already the "not set, use cron_expr" value.
            if isinstance(v, bool):
                invalid.append(key)
                return 0
            try:
                return int(v)
            except (TypeError, ValueError, OverflowError):
                # OverflowError is the infinity case: json.loads accepts
                # `"every": 1e1000000` and yields float('inf'), which int()
                # refuses. NaN lands in ValueError. A merely ENORMOUS finite
                # value is not caught here and does not need to be -- it is a
                # valid int, and compute_next_run_ts already absorbs it into
                # "next run unknown" rather than raising.
                if v is not None:
                    invalid.append(key)
                return 0

        def _str_or_flagged(key: str, v: Any) -> str:
            # `timezone` gets the recording treatment that plain _str_or_empty
            # does not, because discarding it silently reproduces the very bug
            # this field exists to fix: validation would pass, the job would
            # persist timezone="" and fire in the fallback zone (UTC on a fresh
            # install), and the author would see a schedule running on the wrong
            # calendar day with nothing anywhere saying why. A discarded `agent`
            # or `message` degrades visibly; a discarded zone does not.
            if isinstance(v, str):
                return v
            if v is not None:
                invalid.append(key)
            return ""

        entry = cls(
            name=_str_or_empty(data.get("name")),
            every=_int_or_zero("every", data.get("every", 0)),
            cron_expr=_str_or_empty(data.get("cron_expr")),
            agent=_str_or_empty(data.get("agent")),
            message=_str_or_empty(data.get("message")),
            command=_str_or_empty(data.get("command")),
            script=_str_or_empty(data.get("script")),
            agent_sequence=[
                str(a) for a in _list_or_empty("agent_sequence", data.get("agent_sequence"))
            ],
            env={
                str(k): str(v) for k, v in _dict_or_empty("env", data.get("env")).items()
            },
            timezone=_str_or_flagged("timezone", data.get("timezone")),
            skip_dates=[str(d) for d in _list_or_empty("skip_dates", data.get("skip_dates"))],
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
        entry.type_invalid_fields = invalid
        return entry


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


# Overlay ids and the host slots they replace are kebab-case slugs: they key the
# frontend overlay registry, so the grammar is deliberately narrower than a page
# route (no dots, no path separators, no leading dash).
_OVERLAY_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass
class UIOverlay:
    """A global overlay surface contributed by an app.

    An overlay is not routed: it floats above whatever the user is looking at
    and is opened by a gesture the host owns, so it carries no sidebar
    placement and no URL. ``id`` keys the frontend overlay registry the same way
    :class:`UIPage`'s ``route`` keys the builtin component registry.

    ``replaces`` names the host overlay slot this app takes over while it is
    enabled (e.g. ``"quick-search"``), and is required: the host opens an overlay
    only through a slot it owns, so a declaration without one can never be shown.
    Whether the named slot exists is decided by the frontend registry, which
    reports an unknown slot rather than vanishing silently -- the same posture as
    an unroutable page route.
    """

    id: str = ""
    replaces: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "replaces": self.replaces}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UIOverlay:
        return cls(
            id=str(data.get("id", "")),
            replaces=str(data.get("replaces", "")),
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
    overlays: list[UIOverlay] = field(default_factory=list)
    sidebar: UISidebar = field(default_factory=UISidebar)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.entry:
            d["entry"] = self.entry
        if self.pages:
            d["pages"] = [p.to_dict() for p in self.pages]
        if self.overlays:
            d["overlays"] = [o.to_dict() for o in self.overlays]
        sidebar_d = self.sidebar.to_dict()
        if sidebar_d:
            d["sidebar"] = sidebar_d
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UIConfig:
        pages = [UIPage.from_dict(p) for p in data.get("pages", []) if isinstance(p, dict)]
        # A hand-edited manifest can carry `"overlays": null` (or a string, or a
        # number): the key is present, so `get` returns that value rather than the
        # default and iterating it would raise out of install validation.
        raw_overlays = data.get("overlays", [])
        overlays = (
            [UIOverlay.from_dict(o) for o in raw_overlays if isinstance(o, dict)]
            if isinstance(raw_overlays, list)
            else []
        )
        sidebar_raw = data.get("sidebar", {})
        sidebar = UISidebar.from_dict(sidebar_raw) if isinstance(sidebar_raw, dict) else UISidebar()
        return cls(
            entry=str(data.get("entry", "")),
            pages=pages,
            overlays=overlays,
            sidebar=sidebar,
        )


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
    #: May run durable background jobs through the host's Job SDK, and gains the
    #: shared ``_jobs/*`` HTTP surface under its own namespace. Declared for the
    #: same reason as ``spawn``: "which apps can start work that outlives the
    #: page that started it" must be answerable from the manifest.
    jobs: bool = False
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
        if self.jobs:
            d["jobs"] = True
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
            jobs=data.get("jobs") is True,
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
            # fullmatch, not match: KEBAB_RE ends in `$`, which also matches
            # just before a trailing newline, so `match` accepts an id with one
            # trailing. The id is joined into a channel name ("<app>.<id>") and
            # used as a subscription key, so the newline survives into both.
            if not KEBAB_RE.fullmatch(ch.id):
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


# A contributed command's id keys the Command Bar row and its usage record, so the
# grammar is the same narrow kebab slug the overlay registry uses -- no dots, no
# path separators, no leading dash.
# Both patterns below are applied with `.fullmatch()`, never `.match()`. Python's `$`
# also matches immediately BEFORE a trailing newline, so `.match()` accepts
# `"approve-all\n"` and `"github.com\n"` -- while JavaScript's `$` without the `m` flag
# does not, so `contributedCommands.ts` rejects exactly those. That asymmetry is the
# drift shape this contract has already been bitten by three times: the manifest
# installs clean and the launcher then shows nothing, with no error the app author sees.
_COMMAND_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: Longest accepted argument host allowlist. The list is scanned per keystroke, and
#: one longer than this is a manifest bug rather than a real allowlist.
_MAX_ARGUMENT_HOSTS = 20

#: A literal hostname, optionally with a leading dot meaning "this domain or any
#: subdomain of it". Fixed and host-owned: it is never built from manifest input.
_HOST_RE = re.compile(r"^\.?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")

#: Most commands one app may contribute, mirroring `MAX_COMMANDS_PER_APP` in
#: `contributedCommands.ts`. Not a layout limit -- the group is display-capped anyway
#: -- but a bound on how much work one app can add to ranking on every keystroke.
_MAX_COMMANDS_PER_APP = 20

#: Bounds on hidden match aliases, mirroring `MAX_KEYWORDS` / `MAX_KEYWORD` in
#: `contributedCommands.ts`. The launcher's ranking walks every keyword on every
#: keystroke, so an unbounded list is paid per character typed, not once per render.
_MAX_KEYWORDS = 30
_MAX_KEYWORD = 60
#: Longest accepted row label, mirroring `MAX_TITLE` in `contributedCommands.ts`.
_MAX_TITLE = 120

#: Longest accepted prompt template. The prompt is sent to an agent as if the user
#: typed it; a template past this length is a document, not a command.
_MAX_PROMPT_TEMPLATE = 4000

#: The placeholder a prompt template uses to interpolate the collected argument.
ARGUMENT_TOKEN = "{argument}"  # noqa: S105 - a template placeholder, not a secret


def _mirrored_len(text: str) -> int:
    """Length in UTF-16 code units -- what JavaScript's ``.length`` counts.

    Every cap above is mirrored by a constant in ``contributedCommands.ts`` compared
    against ``.length``, and the two languages do not agree on what a character is:
    Python counts CODE POINTS, JavaScript counts UTF-16 units, so anything outside the
    BMP counts once here and twice there. A 100-emoji title is 100 to ``len()`` and 200
    to the launcher, which passed the manifest and was then dropped by the renderer --
    the app installed clean and the row never appeared. This file already says of the
    title cap that "a cap that only one side enforces is not a cap"; that holds for the
    UNIT as well as for the number, so the caps are measured the host's way.
    UNPAIRED surrogates are why this passes ``surrogatepass``: JSON can carry a lone
    ``\\ud800`` escape, ``json.loads`` accepts it, and a plain ``utf-16-le`` encode then
    raises ``UnicodeEncodeError`` -- so a manifest would CRASH validation instead of
    being told what is wrong with it. With the flag the count still matches the host
    exactly: a lone surrogate is one unit in both languages, an astral character two.
    """
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


@dataclass
class CommandArgument:
    """The ONE value a contributed command collects before it can act.

    Deliberately singular. Raycast-style multi-argument tokens are a real feature,
    but every argument is another thing the reader must get right before a command
    that writes somewhere fires, and one value covers the cases this contribution
    point exists for (a link, a query, an identifier). A second argument is an
    additive change to this class, not a rewrite of it.

    The argument is checkable BEFORE the command runs -- the collected text is
    spliced into an instruction handed to an agent with tools, so "anything the
    reader pasted" is not an acceptable domain.

    ``kind`` names one of a FIXED set of matchers the host implements. It is
    deliberately not a regex, and that is the whole design of this field. An earlier
    revision let the manifest ship its own ``pattern``; a regex is a small program,
    and running a third party's program against the field on every keystroke, on the
    thread that draws the launcher, is a hang the reader cannot escape -- ``^(a+)+$``
    and ``^(a|aa)+$`` are both under ten characters and both exponential, and neither
    Python nor JavaScript can interrupt a synchronous match. Fencing that off with
    syntactic checks was attempted and abandoned: the checks can only ever recognize
    shapes, so each one invites the next hostile pattern that it does not cover.

    So the manifest DESCRIBES what it wants and the host decides how to check it.
    ``url`` parses with the runtime's own URL parser (linear, no backtracking) and
    then applies ``hosts``; ``text`` accepts any non-empty value. Both run in time
    proportional to the input no matter what the manifest says. Adding a kind is a
    change to this file -- which is exactly the point: the vocabulary is ours.

    The cost is precision, and it is a real cost. A pattern could demand
    ``/pull/<n>`` specifically; ``kind="url"`` with ``hosts=["github.com"]`` accepts
    any URL on that host and leaves what the link DENOTES to the agent reading it.
    That is the right split -- the host is the wrong place to encode another
    product's URL taxonomy, and it cannot do so safely.
    """

    #: Matchers the host implements. Extending this is a deliberate host change.
    KINDS = ("url", "text")

    placeholder: str = ""
    hint: str = ""
    kind: str = "text"
    hosts: list[str] = field(default_factory=list)
    patternError: str = ""
    #: Whether the source manifest carried the retired ``pattern`` key. Kept so
    #: ``validate`` can refuse a stale-contract app loudly instead of silently
    #: dropping an unknown key and running on the loosest matcher. Not serialized --
    #: it describes the INPUT, not the contract.
    saw_pattern: bool = False
    #: Whether the source manifest carried a ``hosts`` that was not a list. Kept for the
    #: same reason as ``saw_pattern``: the coerced value is indistinguishable from a
    #: deliberate empty list, and empty means ANY host. Not serialized.
    bad_hosts: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.placeholder:
            d["placeholder"] = self.placeholder
        if self.hint:
            d["hint"] = self.hint
        if self.kind:
            d["kind"] = self.kind
        if self.hosts:
            d["hosts"] = list(self.hosts)
        if self.patternError:
            d["patternError"] = self.patternError
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CommandArgument:
        hosts_raw = data.get("hosts", [])
        return cls(
            placeholder=str(data.get("placeholder", "")),
            hint=str(data.get("hint", "")),
            kind=str(data.get("kind", "text")),
            hosts=[str(h).strip().lower() for h in hosts_raw if str(h).strip()]
            if isinstance(hosts_raw, list)
            else [],
            patternError=str(data.get("patternError", "")),
            saw_pattern="pattern" in data,
            # A `hosts` that is present but not a list would otherwise coerce to the
            # empty list -- which does not mean "no opinion", it means "any host". The
            # author wrote a restriction and would get none, silently, with autoSend
            # still on. Recorded here and refused in validate() rather than dropped.
            bad_hosts="hosts" in data and not isinstance(hosts_raw, list),
        )


@dataclass
class CommandContribution:
    """One command an app contributes to the host's Command Bar.

    This is the seam that lets a command row live OUTSIDE this repository: the app
    declares what the row says and what it does, and the host renders and runs it.
    It is deliberately DECLARATIVE -- a title, an optional argument, and a prompt
    template -- and carries no code. An app that could ship a function into the
    launcher would be running third-party JavaScript inside the host's own
    surface, on every keystroke, with the reader's session; declaring data the
    host interprets is the same trade the overlay registry already makes by
    resolving ``id`` against components compiled into the bundle rather than
    loading one from the app.

    ``prompt`` is the command's action: activating it opens a NEW session seeded
    with this text. ``autoSend`` asks the host to send it immediately rather than
    leaving it in the composer -- see the module spec for what the host shows the
    reader before it does.

    ``icon`` names a glyph from the host's own set. An arbitrary URL or inline SVG
    is refused: the launcher is not a place to load remote images from, and a glyph
    that must be fetched cannot render in a surface that promises to issue no
    request.
    """

    id: str = ""
    title: str = ""
    subtitle: str = ""
    icon: str = ""
    keywords: list[str] = field(default_factory=list)
    prompt: str = ""
    autoSend: bool = False
    argument: CommandArgument | None = None
    #: Whether the manifest's ``argument`` was present but not an object. Same shape as
    #: ``Contributes.bad_commands``, and the one place where erasing it also DIVERGES
    #: from the host: the frontend distinguishes "no argument" from "argument declared
    #: but broken" and drops the whole row for the latter, so coercing to ``None`` here
    #: installs a manifest whose command the launcher then refuses to render. Not
    #: serialized -- it describes the INPUT.
    bad_argument: bool = False

    def erases_a_restriction(self) -> bool:
        """Whether serializing this would emit something LOOSER than the input.

        The refusal flags deliberately describe the input rather than the contract, so
        they are not serialized -- but ``list_apps()`` reads a manifest off disk and
        re-serializes it with no ``validate()`` in between. For a manifest edited after
        install that is the whole gap: ``pattern`` and a scalar ``hosts`` both vanish,
        and what reaches the dashboard is a well-formed argument on the DEFAULT matcher
        -- ``text``, any host -- so the frontend's mirror of these refusals has nothing
        left to fire on and the value goes to the agent unchecked.

        A malformed ``kind`` or a non-hostname ``hosts`` entry is NOT listed here: those
        survive serialization verbatim, so the frontend still sees and refuses them.
        """
        if self.bad_argument:
            return True
        arg = self.argument
        return arg is not None and (arg.saw_pattern or arg.bad_hosts)

    def to_dict(self) -> dict[str, Any]:
        if self.erases_a_restriction():
            # Emitting nothing costs this app its row on a surface whose install path
            # already refused it loudly. Emitting a rejection MARKER instead would put
            # the safe outcome behind the reader's dashboard understanding a new field,
            # and a dashboard that did not would read the permissive matcher -- the
            # failure this exists to prevent. So it fails closed here.
            return {}
        d: dict[str, Any] = {"id": self.id, "title": self.title, "prompt": self.prompt}
        if self.subtitle:
            d["subtitle"] = self.subtitle
        if self.icon:
            d["icon"] = self.icon
        if self.keywords:
            d["keywords"] = list(self.keywords)
        if self.autoSend:
            d["autoSend"] = True
        if self.argument is not None:
            arg_d = self.argument.to_dict()
            if arg_d:
                d["argument"] = arg_d
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CommandContribution:
        arg_raw = data.get("argument")
        keywords_raw = data.get("keywords", [])
        return cls(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            subtitle=str(data.get("subtitle", "")),
            icon=str(data.get("icon", "")),
            keywords=[str(k) for k in keywords_raw] if isinstance(keywords_raw, list) else [],
            prompt=str(data.get("prompt", "")),
            # Identity against the JSON boolean, NOT ``bool(...)``: every non-empty
            # string is truthy, so ``"autoSend": "false"`` would coerce to True and
            # then serialize back as ``true`` -- a manifest that reads as disabled
            # silently enabling the one capability that sends text on the reader's
            # behalf. Only the literal ``true`` turns it on.
            autoSend=data.get("autoSend") is True,
            argument=CommandArgument.from_dict(arg_raw) if isinstance(arg_raw, dict) else None,
            # A present-but-not-an-object ``argument`` would otherwise coerce to "no
            # argument declared", which is a DIFFERENT command rather than an invalid
            # one. An explicit ``null`` is treated as absent, matching the host.
            bad_argument=(
                "argument" in data
                and arg_raw is not None
                and not isinstance(arg_raw, dict)
            ),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        where = f"contributes.commands[{self.id or '?'}]"
        if not self.id:
            errors.append("contributes.commands: entry missing id")
        elif not _COMMAND_SLUG_RE.fullmatch(self.id):
            errors.append(
                f"{where}: id must be lowercase alphanumeric with dashes, got {self.id!r}"
            )
        if len(self.keywords) > _MAX_KEYWORDS:
            # The launcher's ranking walks every keyword of every row on every keystroke,
            # so this is the one declared field whose cost is paid per character typed.
            # Mirrors `MAX_KEYWORDS` in `contributedCommands.ts`, which drops the overflow
            # -- refused here so the author is told rather than silently trimmed.
            errors.append(
                f"{where}: {len(self.keywords)} keywords exceeds the limit of "
                f"{_MAX_KEYWORDS}"
            )
        for kw in self.keywords:
            if _mirrored_len(kw) > _MAX_KEYWORD:
                errors.append(
                    f"{where}: keyword exceeds {_MAX_KEYWORD} characters "
                    f"({_mirrored_len(kw)})"
                )
                break
        if not self.title:
            errors.append(f"{where}: missing title")
        elif _mirrored_len(self.title) > _MAX_TITLE:
            # Mirrors `MAX_TITLE` in `contributedCommands.ts`. Missing here originally,
            # which meant an over-long title passed the manifest and was then dropped by
            # the frontend -- the command vanished from the launcher with the app author
            # having seen no error on install, the worst of both validators.
            errors.append(
                f"{where}: title exceeds {_MAX_TITLE} characters "
                f"({_mirrored_len(self.title)})"
            )
        if self.subtitle and _mirrored_len(self.subtitle) > _MAX_TITLE:
            # Mirrors the frontend's cap. The subtitle is SEARCHED -- `rankRootRows` runs
            # `fuzzyMatch` over it on every keystroke -- so it belongs with the title and
            # keyword bounds rather than with the untouched display fields.
            errors.append(
                f"{where}: subtitle exceeds {_MAX_TITLE} characters "
                f"({_mirrored_len(self.subtitle)})"
            )
        if not self.prompt:
            # A command with no prompt has no action. There is no other verb yet, so
            # this is a broken row rather than a differently-shaped one.
            errors.append(f"{where}: missing prompt")
        elif _mirrored_len(self.prompt) > _MAX_PROMPT_TEMPLATE:
            errors.append(
                f"{where}: prompt exceeds {_MAX_PROMPT_TEMPLATE} characters "
                f"({_mirrored_len(self.prompt)})"
            )
        interpolates = ARGUMENT_TOKEN in self.prompt
        if self.bad_argument:
            # Reported INSTEAD of the two "declares no argument" errors below, which are
            # both true of the parsed value and both misleading about the manifest: they
            # tell an author who visibly wrote an ``argument`` that they wrote none.
            errors.append(
                f"{where}: argument must be an object -- a non-object value reads as a "
                "command with no argument, which the host treats as a different command "
                "rather than a broken one and refuses to render at all"
            )
        elif self.argument is None:
            if interpolates:
                errors.append(
                    f"{where}: prompt interpolates {ARGUMENT_TOKEN} but the command "
                    "declares no argument"
                )
            if self.autoSend:
                # The host's consent mechanism for autoSend is the resolved-prompt
                # preview, and that preview lives in the ARGUMENT state. A command
                # with no argument never enters it, so autoSend there would send
                # app-authored text to a tool-enabled agent with nothing shown to the
                # reader at all. Refused rather than silently downgraded, so the app
                # author learns the rule instead of wondering why it did not fire.
                errors.append(
                    f"{where}: autoSend requires an argument -- the host shows the "
                    "resolved prompt in the argument field before sending, and a "
                    "command with no argument never reaches that step"
                )
        else:
            if not interpolates:
                # The reader is asked for a value the command then ignores -- always a
                # mistake, and a confusing one, because the command still runs.
                errors.append(
                    f"{where}: declares an argument but the prompt never uses "
                    f"{ARGUMENT_TOKEN}"
                )
            errors.extend(self._validate_matcher(where))
        return errors

    def _validate_matcher(self, where: str) -> list[str]:
        errors: list[str] = []
        arg = self.argument
        if arg is None:
            return errors
        if arg.saw_pattern:
            # An app written against the revision of this contract that accepted its
            # own regex. Refused rather than migrated: `pattern` is an unknown key now,
            # so ignoring it would leave the argument on the default `text` matcher --
            # accepting ANY non-empty string -- while the app still declares autoSend
            # and still believes its pattern is guarding the value. Failing loudly is
            # the only outcome that does not quietly widen what reaches the agent.
            errors.append(
                f"{where}: argument.pattern is no longer accepted -- declare "
                f"argument.kind ({', '.join(CommandArgument.KINDS)}) instead, because "
                "the host implements the matcher and a manifest cannot supply one"
            )
        if arg.bad_hosts:
            errors.append(
                f"{where}: argument.hosts must be an array of hostnames -- a non-array "
                "value would erase the restriction rather than apply it, and an empty "
                "allowlist means ANY host"
            )
        if arg.kind not in CommandArgument.KINDS:
            errors.append(
                f"{where}: argument.kind must be one of "
                f"{', '.join(CommandArgument.KINDS)} (got {arg.kind!r})"
            )
        if arg.hosts and arg.kind != "url":
            errors.append(f"{where}: argument.hosts applies only to kind 'url'")
        if len(arg.hosts) > _MAX_ARGUMENT_HOSTS:
            errors.append(
                f"{where}: argument.hosts exceeds {_MAX_ARGUMENT_HOSTS} entries "
                f"({len(arg.hosts)})"
            )
        for name in ("placeholder", "hint", "patternError"):
            # The last app-supplied strings with no bound. Not searched, so they cost
            # LAYOUT rather than per-keystroke work: an outsized hint or patternError
            # pushes the resolved-prompt preview and the footer around, and that preview
            # is the consent surface for autoSend. Mirrors the frontend's cap, which
            # refuses the command outright.
            value = getattr(arg, name, "")
            if _mirrored_len(value) > _MAX_TITLE:
                errors.append(
                    f"{where}: argument.{name} exceeds {_MAX_TITLE} characters "
                    f"({_mirrored_len(value)})"
                )
        for host in arg.hosts:
            # A host is compared literally against the parsed URL's hostname, so
            # anything that is not a hostname cannot match and is a manifest bug worth
            # naming rather than silently never matching. A leading dot is allowed and
            # means "this domain or any subdomain".
            if not _HOST_RE.fullmatch(host):
                errors.append(f"{where}: argument.hosts entry is not a hostname ({host!r})")
        return errors


@dataclass
class Contributes:
    """What an app adds to host surfaces it does not own.

    Separate from ``ui`` on purpose: ``ui`` is where an app declares surfaces of
    its OWN (a page, an overlay it supplies a component for), while a contribution
    is a row inside a surface the host renders and controls. Keeping them apart is
    what lets an app with no page, no bundle and no backend -- a manifest and a
    skill -- still reach the launcher.
    """

    commands: list[CommandContribution] = field(default_factory=list)
    #: Whether the source manifest's ``commands`` was present but not a list. Same reason
    #: as ``CommandArgument.bad_hosts``: coercing to ``[]`` is indistinguishable from a
    #: deliberate empty list, so the declaration would pass validation and then vanish
    #: from ``to_dict`` -- the author sees no error and no rows. Not serialized.
    bad_commands: bool = False
    #: Whether the manifest's ``contributes`` itself was present but not an object.
    #: Outermost case of the same shape. Not serialized.
    bad_block: bool = False
    #: How many ENTRIES of a well-formed ``commands`` array were not objects. The array
    #: being a list is not enough: a single bad element used to be filtered out here, so
    #: an app declaring five commands with one typo installed with four and no warning.
    #: Counted rather than flagged so the error can say how many vanished. Not
    #: serialized.
    dropped_commands: int = 0

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        # A command that would serialize looser than it was declared emits ``{}`` and is
        # dropped here, so the read path cannot hand the dashboard a restriction-free
        # copy of an argument the manifest refused.
        commands = [c.to_dict() for c in self.commands]
        kept = [c for c in commands if c]
        if kept:
            d["commands"] = kept
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Contributes:
        raw = data.get("commands", [])
        entries = raw if isinstance(raw, list) else []
        return cls(
            commands=[
                CommandContribution.from_dict(c) for c in entries if isinstance(c, dict)
            ],
            bad_commands="commands" in data and not isinstance(raw, list),
            dropped_commands=sum(1 for c in entries if not isinstance(c, dict)),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.bad_block:
            errors.append(
                "contributes must be an object -- a non-object value validates as "
                "contributing nothing and then disappears from the serialized "
                "manifest, so the app author sees neither an error nor any rows"
            )
        if self.bad_commands:
            errors.append(
                "contributes.commands must be an array -- a non-array value passes as "
                "empty and then disappears from the serialized manifest, so the app "
                "author sees neither an error nor any rows"
            )
        if self.dropped_commands:
            errors.append(
                f"contributes.commands: {self.dropped_commands} entr"
                f"{'y' if self.dropped_commands == 1 else 'ies'} "
                "must be an object -- a non-object entry is filtered out before "
                "validation, so the app installs with the remaining rows and its "
                "author is never told one was dropped"
            )
        if len(self.commands) > _MAX_COMMANDS_PER_APP:
            # Mirrors the frontend's slice. The fourth of four bounds to be mirrored
            # and the last one missed, which is the same failure the title cap had:
            # the manifest installed clean and the launcher then dropped the overflow,
            # so a thirty-command app lost ten rows with no error its author could see.
            # A cap that only one side enforces is not a cap; it is a silent truncation.
            errors.append(
                f"contributes.commands: {len(self.commands)} commands exceeds the "
                f"limit of {_MAX_COMMANDS_PER_APP}"
            )
        seen: set[str] = set()
        for cmd in self.commands:
            errors.extend(cmd.validate())
            if cmd.id:
                if cmd.id in seen:
                    # Two rows with one id: the second silently wins the frecency
                    # record and one of them becomes unreachable by usage.
                    errors.append(f"contributes.commands: duplicate id {cmd.id!r}")
                seen.add(cmd.id)
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
        "contributes",
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

    # --- Contributions to host-owned surfaces ---
    #
    # Typed rather than left to ``extra``, even though an unknown top-level key
    # already round-trips to the dashboard through ``extra``: a contribution that
    # ends up as the text of an instruction sent to an agent has to be CHECKED, and
    # ``extra`` is by definition the un-checked bucket. Being a known field is what
    # makes ``validate()`` see it on every parse.
    contributes: Contributes = field(default_factory=Contributes)

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
        else:
            name_error = app_name_error(self.name)
            if name_error:
                errors.append(name_error)

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

        # UI overlay validation
        seen_overlay_ids: set[str] = set()
        for overlay in self.ui.overlays:
            if not overlay.id:
                errors.append("ui overlay missing required field: id")
                continue
            if not _OVERLAY_SLUG_RE.match(overlay.id):
                errors.append(f"ui overlay id must be kebab-case: {overlay.id!r}")
            if overlay.id in seen_overlay_ids:
                errors.append(f"ui overlay duplicate id: {overlay.id!r}")
            seen_overlay_ids.add(overlay.id)
            if not overlay.replaces:
                errors.append(f"ui overlay {overlay.id!r} missing required field: replaces")
            elif not _OVERLAY_SLUG_RE.match(overlay.replaces):
                errors.append(
                    f"ui overlay {overlay.id!r}: replaces must be kebab-case: "
                    f"{overlay.replaces!r}"
                )

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
            # Calendar fields are validated HERE as well as at the persistence
            # owner. register_app_crons_with_service catches a per-job
            # ValueError, logs it and moves on, so a bad zone shipped in a
            # manifest would otherwise register nothing and say so only in the
            # gateway log -- the app author sees a cron that silently does not
            # exist. Surfacing it as a manifest validation error reports it at
            # install/validate time instead.
            for _bad in cron.type_invalid_fields:
                errors.append(
                    f"cron entry {cron.name!r}: {_bad!r} has the wrong JSON type "
                    f"(expected {_CRON_FIELD_JSON_TYPES.get(_bad, 'a different type')}); "
                    f"the value was ignored"
                )
            if cron.timezone and not is_valid_timezone(cron.timezone):
                errors.append(
                    f"cron entry {cron.name!r}: unknown timezone: {cron.timezone!r} "
                    f"(expected an IANA zone name such as 'America/New_York')"
                )
            for _skip in cron.skip_dates:
                if not is_valid_skip_date(_skip):
                    errors.append(
                        f"cron entry {cron.name!r}: invalid skip_date: {_skip!r} "
                        f"(expected zero-padded YYYY-MM-DD)"
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

        # Contributed commands: ids, caps, prompt/argument agreement, matcher kind.
        errors.extend(self.contributes.validate())

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
        if self.contributes.commands:
            # A contributed command's `prompt` is sent to an agent with tools as if
            # the reader typed it, and `autoSend` fires it without a further
            # keystroke -- the same class of surface as a cron's `command`/`script`
            # one clause up, and for the same reason: vetting bounds the SHAPE of a
            # contribution, but only the signature authenticates PUBLISHER INTENT.
            # Left out, a signed app's rows would be the one part of it an attacker
            # could rewrite with the signature still verifying, and the reader's
            # trust in the signature is precisely what would carry the tampered
            # prompt into a session.
            #
            # `argument` rides along inside each entry's canonical to_dict(), which
            # matters as much as the prompt: widening a matcher (`kind: url` with a
            # host allowlist -> `text`) does not change a single visible character of
            # the row, and it is what decides whether the value spliced into that
            # prompt was checked at all.
            #
            # List order preserved, so reordering is a signature-relevant change.
            # Included only when non-empty, so manifests signed before contributions
            # existed keep producing the identical payload.
            body["contributes"] = self.contributes.to_dict()
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
        contrib_d = self.contributes.to_dict()
        if contrib_d:
            d["contributes"] = contrib_d
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

        contrib_raw = data.get("contributes", {})
        contributes = (
            Contributes.from_dict(contrib_raw)
            if isinstance(contrib_raw, dict)
            # Not silently erased: a non-object `contributes` is the outermost case of the
            # fail-open shape already closed for `commands` and `hosts` -- it validates as
            # "contributes nothing" and vanishes from `to_dict`, so the author sees no
            # error and no rows. `bad_block` carries it to `validate`.
            else Contributes(bad_block=True)
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
            contributes=contributes,
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
