"""The typed ``app.json`` blocks other than ``contributes``.

One dataclass per block -- ``crons``, ``ui``, ``backend`` and its ``hooks``,
``permissions``, ``setup``, ``dependencies``, ``platform``, ``publishProvider`` and
``notifications`` -- each with the ``from_dict`` / ``to_dict`` pair ``AppManifest``
composes and, where a block has rules of its own, a ``validate``. Parsing is defensive by
contract: a wrong JSON type degrades to the field's empty value, and a value that would
silently change meaning is recorded so validation can refuse it.

Served through :mod:`kiro_crew.apps.manifest`, which callers and tests import and patch;
this module never imports it.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Any, ClassVar

from kiro_crew.apps.manifest_names import KEBAB_RE


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
    # Subprocess bound for a ``command``/``script`` job, in seconds. 0 = the
    # scheduler default (30s script, 300s command).
    timeout: int = 0
    # Per-wake execution budget, in seconds (1..86400). 0 = the scheduler
    # default. On a command/script job it must cover ``timeout`` plus cleanup;
    # the same create-time check the cron SDK applies runs at validate().
    timeout_secs: int = 0
    # Fire exactly on the schedule instead of after the scheduler's random start
    # delay (up to 59 minutes on a daily job), for a job whose time the user
    # chose on purpose -- a report wanted ready at 06:50, not near 07:40.
    strict_schedule: bool = False
    # Schedule-page folder NAME to file the job in (names, not ids: a manifest
    # is portable across installs, and folder ids are minted per-machine by the
    # dashboard). Resolved against existing folders at registration; a name
    # with no matching folder registers the job ungrouped with a logged
    # warning, and re-files it on the next enable once the folder exists.
    # Empty = ungrouped. Registration re-applies this on every enable, so the
    # assignment survives the disable/enable delete-and-recreate cycle that
    # loses a folder set by hand on an app cron.
    folder: str = ""
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
    # author who wrote ``skip_dates`` as one date string instead of an array
    # asked for a skip, and dropping it without a word lets the job fire on the
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
        if self.folder:
            d["folder"] = self.folder
        # Present only when set, like the calendar fields: signing_payload
        # folds to_dict() into the signed body, so an unset value must not
        # change an already-signed manifest's payload.
        if self.timeout:
            d["timeout"] = self.timeout
        if self.timeout_secs:
            d["timeout_secs"] = self.timeout_secs
        if self.strict_schedule:
            d["strict_schedule"] = True
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

        def _bool_or_false(key: str, v: Any) -> bool:
            # Strict like ``enabled``: the string "false" is truthy, and reading
            # it as True would drop the start delay an author never gave up.
            if isinstance(v, bool):
                return v
            if v is not None:
                invalid.append(key)
            return False

        entry = cls(
            name=_str_or_empty(data.get("name")),
            every=_int_or_zero("every", data.get("every", 0)),
            cron_expr=_str_or_empty(data.get("cron_expr")),
            agent=_str_or_empty(data.get("agent")),
            message=_str_or_empty(data.get("message")),
            command=_str_or_flagged("command", data.get("command")),
            script=_str_or_flagged("script", data.get("script")),
            agent_sequence=[
                str(a) for a in _list_or_empty("agent_sequence", data.get("agent_sequence"))
            ],
            env={str(k): str(v) for k, v in _dict_or_empty("env", data.get("env")).items()},
            timezone=_str_or_flagged("timezone", data.get("timezone")),
            skip_dates=[str(d) for d in _list_or_empty("skip_dates", data.get("skip_dates"))],
            folder=_str_or_empty(data.get("folder")),
            timeout=_int_or_zero("timeout", data.get("timeout", 0)),
            timeout_secs=_int_or_zero("timeout_secs", data.get("timeout_secs", 0)),
            persistent_session=bool(data.get("persistent_session", True)),
            silent=bool(data.get("silent", False)),
            strict_schedule=_bool_or_false("strict_schedule", data.get("strict_schedule")),
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
    # Per-page JS bundle path, resolved against the app's ``ui`` directory like
    # ``UIConfig.entry`` below, because one static route serves both.
    entryPoint: str = ""  # noqa: N815
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

    # ESM bundle path resolved against the app's ``ui`` directory, e.g.
    # "dist/index.mjs" is the file at ``<app root>/ui/dist/index.mjs``. One static
    # route (``/apps/<name>/ui/<entry>``) serves every entry an app declares, so a
    # leading ``ui/`` here would resolve to ``ui/ui/``.
    entry: str = ""
    pages: list[UIPage] = field(default_factory=list)
    overlays: list[UIOverlay] = field(default_factory=list)
    sidebar: UISidebar = field(default_factory=UISidebar)
    # Placeholder shape the dashboard shows while the bundle loads, from
    # ``"loading": {"layout": "<name>"}``. A closed set of names the dashboard
    # draws from its own skeleton pieces (``AppLoadingFallback.tsx``); an app never
    # supplies markup or styles. Empty means undeclared, which the dashboard
    # renders as "page", the skeleton every app had before the field existed.
    loading_layout: str = ""

    # Names `ui.loading.layout` may take. Mirrors ``APP_LOADING_LAYOUTS`` in
    # ``website/src/components/AppLoadingFallback.tsx``.
    LOADING_LAYOUTS: ClassVar[frozenset[str]] = frozenset({"page", "rail", "centered", "split"})

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.entry:
            d["entry"] = self.entry
        if self.loading_layout:
            # Emitted only when declared, so a manifest without it keeps producing
            # the identical dict (and the identical signed payload).
            d["loading"] = {"layout": self.loading_layout}
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
            loading_layout=cls._parse_loading_layout(data.get("loading")),
        )

    @classmethod
    def _parse_loading_layout(cls, raw: Any) -> str:
        """Return the declared loading layout, or ``""`` for anything not exactly valid.

        Total, like the rest of ``from_dict``: a hand-edited manifest can hold
        ``null``, a string, a list, or a layout in the wrong case, and none of those
        may raise out of install validation. Each one is dropped, which the dashboard
        reads as the default "page" skeleton. Matching is exact (no case folding or
        trimming) so the stored value is always one the dashboard recognises verbatim.
        """
        if not isinstance(raw, dict):
            return ""
        layout = raw.get("layout")
        # Exact type, not isinstance: a str subclass can override __eq__/__hash__
        # and pass a set lookup with a value the dashboard would not recognise.
        if type(layout) is str and layout in cls.LOADING_LAYOUTS:
            return layout
        return ""


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
    # Opt in to a request-bound principal claim from the gateway's reverse proxy.
    # Only the literal JSON boolean true enables it: a string or a number keeps
    # the default off, so a malformed manifest never widens what a backend trusts.
    signedPrincipal: bool = False  # noqa: N815
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
        if self.signedPrincipal:
            d["signedPrincipal"] = True
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
            signedPrincipal=data.get("signedPrincipal") is True,  # noqa: N815
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
    #: May send turns, choose response options, approve or deny pending tool
    #: requests, and change approval modes for user-owned sessions.
    sessionApproval: bool = False  # noqa: N815
    #: May spawn a background agent through the host's subagent manager.
    #: Declared rather than implicit so "which apps can start an agent" is
    #: auditable from the manifest instead of from an app's import graph.
    spawn: bool = False
    #: May run durable background jobs through the host's Job SDK, and gains the
    #: shared ``_jobs/*`` HTTP surface under its own namespace. Declared for the
    #: same reason as ``spawn``: "which apps can start work that outlives the
    #: page that started it" must be answerable from the manifest.
    jobs: bool = False
    #: May raise a ``crewmate.import`` owner card through ``ctx.crewmate``. The
    #: card creates nothing until the owner presses it.
    proposeCrewmate: bool = False  # noqa: N815
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
        if self.sessionApproval:
            d["sessionApproval"] = True
        if self.spawn:
            d["spawn"] = True
        if self.jobs:
            d["jobs"] = True
        if self.proposeCrewmate:
            d["proposeCrewmate"] = True
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
            sessionApproval=data.get("sessionApproval") is True,  # noqa: N815
            spawn=data.get("spawn") is True,
            jobs=data.get("jobs") is True,
            proposeCrewmate=data.get("proposeCrewmate") is True,  # noqa: N815
            exposeToApps=_granted_list(data.get("exposeToApps")),  # noqa: N815
        )


@dataclass
class SetupConfig:
    """Installation and setup configuration for an app."""

    # `onInstall` runs during a registry install AND again on every registry update:
    # `handle_update_app` re-enters `install_from_registry`, which runs the script
    # before the installed copy is created. A local-path install does not run it.
    # Keep it idempotent.
    onInstall: str = ""  # shell command, registry install + every registry update  # noqa: N815
    # `onUpdate` is declared and round-trips but NOTHING dispatches it (see the
    # declared-not-wired paragraph in docs/system-specs/modules/app-kit-platform.md
    # and test/test_setup_hooks_contract.py). Put update-time work in `onInstall`.
    onUpdate: str = ""  # declared, not executed: no code path dispatches it  # noqa: N815
    onUninstall: str = ""  # shell command run before removing app files  # noqa: N815
    onEnable: str = ""  # shell command run when app is enabled  # noqa: N815
    onDisable: str = ""  # shell command run when app is disabled  # noqa: N815
    # onEnable / onDisable / onUninstall run through `/bin/bash` and are NOT run
    # on native Windows (no `/bin/bash`): the runner refuses them with an
    # unsupported-platform reason before any spawn. (`onInstall` runs in the
    # separate install transaction and is not covered by that refusal.) See
    # docs/app-kit/manifest-reference.md ("setup — Lifecycle Scripts").
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
    """External dependencies that Kiro Crew should resolve during install.

    ``managedBy`` controls the default installation strategy:
      - ``"gateway"``: Kiro Crew resolves each dependency through the edition's
        ``CapabilityManager`` seam
      - ``"app"``: Kiro Crew only checks existence, does not install

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
            optionalCommands=[str(c) for c in (data.get("optionalCommands") or [])],  # noqa: N815
        )


@dataclass
class ClientInstallConfig:
    """Instructions for installing an app on the user's local machine.

    Used when Kiro Crew runs remotely (e.g. cloud desktop) and the app
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

    - ``"server"`` (default): Kiro Crew clones + installs on the server.
    - ``"client"``: Must be installed on the user's local machine.
      When Kiro Crew is on an incompatible platform, the App Store shows
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
    # ``windows`` is in both directions because Kiro Crew itself supports Windows
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

    # The values ``installMode`` takes. Both install gates test ``== "client"``, so any
    # other spelling is read as ``"server"``.
    _INSTALL_MODES = ("server", "client")

    def supports_platform(self, sys_platform: str) -> bool:
        """Check if this platform config supports the given sys.platform value."""
        return sys_platform in {self._OS_TO_PLATFORM.get(o, o) for o in self.os}

    @staticmethod
    def current_os() -> str:
        """Return the user-friendly OS name for the current platform."""
        return PlatformConfig._PLATFORM_TO_OS.get(sys.platform, sys.platform)

    def validate(self) -> list[str]:
        """Return an error for each ``platform`` value no gate recognises.

        ``supports_platform`` resolves an ``os`` name through ``_OS_TO_PLATFORM`` and passes a
        name without a row through unchanged, so ``"Windows"``, ``"win"`` or ``"windows "``
        equals no ``sys.platform`` and the gate answers no for the platform its author named.
        The platform strings themselves (``"win32"``, ``"darwin"``) match only through that
        pass-through and are refused too: a manifest names an OS by its row.
        ``installMode`` has the same shape: ``"Client"`` is not ``"client"``, so the app
        installs as a server app. The error names the accepted values.
        """
        errors: list[str] = []
        for name in self.os:
            if name not in self._OS_TO_PLATFORM:
                errors.append(
                    f"platform.os: unknown value {name!r} "
                    f"(expected one of {', '.join(self._OS_TO_PLATFORM)})"
                )
        if self.installMode not in self._INSTALL_MODES:
            errors.append(
                f"platform.installMode: unknown value {self.installMode!r} "
                f"(expected one of {', '.join(self._INSTALL_MODES)})"
            )
        return errors

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
