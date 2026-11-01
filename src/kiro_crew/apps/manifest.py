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
import os
import posixpath
import re
import sys  # noqa: F401 -- callers patch manifest.sys.platform, which PlatformConfig reads
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.agent_spec_format import NATIVE_SKILL_ALIAS_PREFIX  # noqa: F401
from kiro_crew.apps.manifest_containment import (  # noqa: F401
    _AGENT_TEMPLATE_PLACEHOLDER_RE,
    PATH_SCREEN_REFUSAL,
    PATH_TRAVERSAL_REFUSAL,
    PATH_UNVERIFIED_REFUSAL,
    Containment,
    _app_path_refusal,
    _declared_agent_names,
    _has_dotdot_segment,
    _is_rooted_path,
    app_root_containment,
    contain_command_agents,
    contained_in_screened_root,
    screen_app_path,
)
from kiro_crew.apps.manifest_contributes import (  # noqa: F401
    _COMMAND_AGENT_RE,
    _COMMAND_SLUG_RE,
    _FILE_MENU_KINDS,
    _HOST_RE,
    _MAX_ARGUMENT_HOSTS,
    _MAX_COMMANDS_PER_APP,
    _MAX_FILE_MENU_ITEMS_PER_APP,
    _MAX_KEYWORD,
    _MAX_KEYWORDS,
    _MAX_LINK_PREVIEW_HOSTS,
    _MAX_PANEL_TABS_PER_APP,
    _MAX_PROMPT_TEMPLATE,
    _MAX_TITLE,
    _PANEL_TAB_ID_RE,
    _SESSION_CONTROL_ID_RE,
    _SESSION_CONTROL_STATUS_PATH_RE,
    _TEXT_PATTERN_FLAGS_RE,
    _TEXT_PATTERN_KEYS,
    ARGUMENT_TOKEN,
    FILE_MENU_SURFACES,
    MAX_LINK_PREVIEW_TEXT_PATTERN_LEN,
    MAX_LINK_PREVIEW_TEXT_PATTERNS,
    MAX_LINK_PREVIEWS_PER_APP,
    MAX_SESSION_CONTROLS_PER_APP,
    CommandArgument,
    CommandContribution,
    Contributes,
    FileMenuItemConfig,
    FileMenuWhen,
    LinkPreviewConfig,
    PanelTabConfig,
    SessionControlContribution,
    _mirrored_len,
    _normalize_status_path,
)
from kiro_crew.apps.manifest_names import (  # noqa: F401
    KEBAB_RE,
    RESERVED_APP_NAME_CODE,
    RESERVED_APP_NAMES,
    RESERVED_APP_PATH_SEGMENTS,
    RESERVED_ROUTE_APP_NAMES,
    SEMVER_RE,
    UNPORTABLE_APP_NAMES,
    app_name_error,
    is_reserved_app_name,
)
from kiro_crew.apps.manifest_sections import (  # noqa: F401
    _CHANNEL_PRIORITIES,
    _OVERLAY_SLUG_RE,
    LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT,
    LIFECYCLE_SCRIPT_TIMEOUT_MAX,
    MAX_NOTIFICATION_CHANNELS,
    BackendConfig,
    CapabilityDependencies,
    ClientInstallConfig,
    CronEntry,
    Dependencies,
    HooksConfig,
    NotificationChannel,
    NotificationsConfig,
    Permissions,
    PlatformConfig,
    PublishProviderConfig,
    SetupConfig,
    UIConfig,
    UIOverlay,
    UIPage,
    UISidebar,
    _granted_list,
    clamp_lifecycle_timeout,
    lifecycle_timeout_error,
    lifecycle_timeout_seconds,
    logger,
)
from kiro_crew.atomic_write import read_bytes_with_retry
from kiro_crew.constants import WINDOWS_DEVICE_STEMS  # noqa: F401

# This module is the facade of the app manifest: every name is reached through it as
# ``kiro_crew.apps.manifest.<name>``. It keeps ``AppManifest`` itself, the cron grant rule
# an admitted manifest must meet, the endpoint allowlist every app-declared URL is checked
# against, and the predicates for what the runtime provisions out of process. The rest is
# imported from sibling owners, which never import this module. New work goes to the owner
# of its responsibility:
#
#   ``manifest_names`` -- the app-name contract and the version grammar
#   ``manifest_containment`` -- app-root path containment and contributed-agent containment
#   ``manifest_sections`` -- the typed ``app.json`` blocks other than ``contributes``
#   ``manifest_contributes`` -- the ``contributes`` block and its caps


# Expected JSON type per CronEntry field that from_dict type-gates; turns a
# recorded parse-time violation into a message an app author can act on.
_CRON_FIELD_JSON_TYPES = {
    "every": "a number of seconds",
    "agent_sequence": "an array of agent names",
    "command": "a string command",
    "env": "an object of string keys to string values",
    "script": "a string script path",
    "timezone": "a string IANA zone name",
    "skip_dates": "an array of YYYY-MM-DD strings",
    "timeout": "a whole number of seconds",
    "timeout_secs": "a whole number of seconds",
    "strict_schedule": "a boolean",
}

#: Refusal for a manifest that schedules ``crons`` without the ``permissions.cron``
#: grant. A schedule runs agent prompts or commands unattended, so the
#: ``permissions`` block an operator reads before installing must say so; the
#: message carries the one-line fix because the install error is the channel an
#: app author reads.
CRON_PERMISSION_REQUIRED_ERROR = (
    'crons is declared but permissions.cron is not: add "cron": true under '
    '"permissions" in app.json'
)


def cron_permission_error(manifest: AppManifest) -> str:
    """Return the missing-grant refusal for *manifest*, or ``""`` when it holds.

    The rule applies wherever an app is admitted with a new manifest -- install,
    update and self-registration -- and never at load time, so an app installed
    before the rule keeps loading and firing its crons until its next update.
    """
    if manifest.crons and not manifest.permissions.cron:
        return CRON_PERMISSION_REQUIRED_ERROR
    return ""


#: First path segments under ``/api/apps/<app>/`` that CORE owns rather than the app.
#: Being inside the app's own namespace is therefore NOT sufficient for an app-declared
#: endpoint: core mounts its own handlers in that namespace, so a manifest naming one
#: would pass the prefix test and have the host POST to a core handler with the reader's
#: own session -- on a row the reader clicked, believing it belonged to the app.
#:
#: Enumerated from the core registrations of that namespace, which are the only place
#: this list can be derived from:
#:
#: * ``apps/routes.py`` ``setup_routes`` -- ``manifest``, ``config``, and the lifecycle
#:   verbs ``uninstall`` (whose ``uninstall/preview`` child is covered by the segment),
#:   ``update``, ``enable``, ``disable``, ``open``, ``dev``, ``migrate-cleanup``
#: * ``dashboard/routes/system.py`` -- ``token``
#: * ``apps/job_routes.py`` -- ``_jobs``
#:
#: A NEW core route mounted under ``/api/apps/{name}/`` MUST be added here in the same
#: commit, or an app can claim it; ``test_file_menu_items.py`` scans those sources and
#: fails on a segment this set is missing. Mirrored by ``CORE_APP_ROUTE_SEGMENTS`` in
#: ``website/src/apps/fileMenuContributions.tsx``, the dispatch-time floor for a manifest
#: that reached the dashboard without passing install validation.
CORE_APP_ROUTE_SEGMENTS = frozenset(
    {
        "_jobs",
        "config",
        "dev",
        "disable",
        "enable",
        "frames",
        "manifest",
        "migrate-cleanup",
        "open",
        "token",
        "uninstall",
        "update",
    }
)

#: Splits a path tail at the first segment boundary, query, or fragment. The router
#: matches on the PATH alone, so ``/api/apps/foo/uninstall?x=1`` reaches the core
#: uninstall handler; comparing the raw tail would let a query smuggle a reserved
#: segment past :data:`CORE_APP_ROUTE_SEGMENTS`.
_SEGMENT_BOUNDARY_RE = re.compile(r"[/?#]")


#: Characters an app-declared endpoint may contain, as an ALLOWLIST.
#:
#: A blocklist loses this argument one character at a time. Two separate bypasses of
#: :func:`app_endpoint_allowed` were exactly that shape: the browser's URL parser STRIPS
#: U+0009/000A/000D while ``fetch`` builds the request, so ``/api/apps/foo/uninstall\n``
#: read as the segment ``"uninstall\n"`` here and arrived at core's uninstall handler;
#: and it treats ``\`` as a path separator for http(s), so ``/api/apps/foo/.\uninstall``
#: read as one opaque segment here and normalized to ``/api/apps/foo/uninstall`` there.
#: Both are the same defect -- a character the PARSER reinterprets after this check
#: accepted it -- and there is no reason to believe the set is now enumerated.
#:
#: So the shape is inverted: unreserved characters (:rfc:`3986` §2.3), the delimiters a
#: real declared endpoint needs (``/``, ``?``, ``#``, ``=``, ``&``, ``+``, ``,``, ``:``,
#: ``@``, ``!``, ``$``, ``'``, ``(``, ``)``, ``*``, ``;``), and nothing else. ``\``,
#: every C0 control, DEL, space, and the characters a parser may rewrite or a header may
#: fold on are all outside it without being named.
#: ``\Z``, not ``$``: Python's ``$`` also matches immediately BEFORE a trailing newline,
#: so ``"…/uninstall\n"`` would satisfy a ``$``-anchored pattern and sail through the one
#: bypass this exists to close. The TypeScript mirror needs no equivalent -- JavaScript's
#: ``$`` without the ``m`` flag is a true end anchor -- which is exactly the kind of
#: per-language difference a "mirrored" pair hides, so it is stated on both sides.
_ENDPOINT_ALLOWED_RE = re.compile(r"^[A-Za-z0-9\-._~/?#=&+,:@!$'()*;]+\Z")


def app_endpoint_allowed(
    app_name: str, endpoint: str, *, allow_proxy_namespace: bool = False
) -> bool:
    """Whether an app-declared endpoint routes inside that app's own namespace (§9.3).

    The one implementation of this allowlist. Every place a manifest hands the host a URL
    to call -- ``publishProvider.endpoint``, ``contributes.fileMenuItems[].endpoint`` --
    checks it here, because a second copy of a security control is free to drift from the
    first and the drift is invisible until something is let through.

    ``allow_proxy_namespace`` additionally admits ``/apps/<app>/api/``, the reverse-proxy
    prefix a PROCESS-backed app is served under. It is the caller's decision because each
    caller documents its own contract: a contributed row must reach a process-backed app,
    while the publish-provider registry names ``/api/apps/<app>/`` in its refusal message
    and keeps that narrower shape. Defaulting it off means adding a caller cannot widen an
    existing surface by accident.

    Three properties do the work, and each is easy to lose when rewritten from memory:
    normalization happens BEFORE the prefix test, so ``/api/apps/foo/../../shutdown``
    cannot escape the namespace; the prefix carries a trailing slash, so a sibling app
    (``/api/apps/foobar/x``) cannot pass ``foo``'s allowlist on a bare ``startswith``;
    and the first segment BELOW the prefix is refused when core owns it, because the app's
    own namespace is where core mounts the lifecycle routes
    (:data:`CORE_APP_ROUTE_SEGMENTS`).
    """
    if not app_name or not endpoint:
        return False
    # A RESERVED path segment is never an app's own namespace, whatever an app is called.
    # `/api/apps/registries/refresh` and `/api/apps/registry/install` are shared literal
    # routes registered BEFORE the `/api/apps/{name}` catch-all, and the first segment
    # below the prefix (`refresh`, `install`) is not a per-app lifecycle name, so
    # :data:`CORE_APP_ROUTE_SEGMENTS` does not cover them.
    #
    # Reserving the NAME is explicitly forward-looking (see
    # :data:`RESERVED_APP_PATH_SEGMENTS`): it stops new apps claiming one but leaves an
    # already-published app so named, and the token_auth carve-out that constrains such an
    # app governs an APP's own token, not a row a reader clicks with their own session.
    # This is the check that has to refuse it.
    if app_name in RESERVED_APP_PATH_SEGMENTS:
        return False
    decoded = urllib.parse.unquote(endpoint)
    # Character allowlist FIRST, before anything else reads the path: see
    # :data:`_ENDPOINT_ALLOWED_RE` for why this is an allowlist and not a set of named
    # refusals. Applied after ``unquote`` so a percent-encoded ``%5c`` or ``%0a`` is
    # judged as the character it becomes, not as the escape.
    if not _ENDPOINT_ALLOWED_RE.match(decoded):
        return False
    normalized = posixpath.normpath(decoded)
    if ".." in decoded or normalized != decoded.rstrip("/"):
        return False
    # BOTH documented app namespaces, but only when the CALLER's contract covers the
    # second one: which namespace an app owns is not its choice — one declaring
    # ``backend.entryPoint`` runs its own process and is reverse-proxied at
    # ``/apps/<app>/api/`` (``routes_proxy.py`` ``handle_app_api_proxy``), while one declaring
    # only ``backend.hooks.routes`` is registered in-gateway under ``/api/apps/<app>/``.
    # Admitting just the in-gateway form refused every process-backed app's contribution
    # at install, so those apps could not contribute a row at all.
    #
    # It is a per-caller flag rather than a blanket widening because this is the ONE
    # shared endpoint check (see ``test_app_endpoint_allowed_is_the_one_shared_check``)
    # and its other caller, the publish-provider registry in ``routes_listing.py``, states
    # ``/api/apps/<app>/`` as its contract in its own refusal message. Widening the
    # helper for everyone would have broadened that surface silently.
    #
    # The core-owned reserved segments apply to the IN-GATEWAY prefix only: the proxy
    # namespace forwards wholesale into the app's own process, so nothing core serves
    # lives under it and a reserved name there is the app's own route.
    prefixes: list[tuple[str, frozenset[str]]] = [
        (f"/api/apps/{app_name}/", CORE_APP_ROUTE_SEGMENTS),
    ]
    if allow_proxy_namespace:
        prefixes.append((f"/apps/{app_name}/api/", frozenset()))
    for prefix, reserved in prefixes:
        if not (normalized + "/").startswith(prefix):
            continue
        # The bare namespace root yields "" (removeprefix is a no-op on `/api/apps/foo`,
        # whose split then starts with the leading slash), which no core route claims.
        segment = _SEGMENT_BOUNDARY_RE.split(normalized.removeprefix(prefix), 1)[0]
        return segment not in reserved
    return False


#: Bounds for top-level config ownership declarations. These mirror the generic
#: foreign-manifest container and key bounds in ``apps.plugin_import`` so every
#: manifest-facing parser retains the same maximum amount of untrusted input.
_MAX_CONFIG_KEYS = 200
_MAX_CONFIG_KEY_CHARS = 200

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
        "config_keys",
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

    # --- Config ownership ---
    config_keys: list[str] = field(default_factory=list)
    config_keys_error: str = field(default="", repr=False)

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

    def validate(
        self, app_root: Path | None = None, *, unchecked: list[str] | None = None
    ) -> list[str]:
        """Return list of validation errors (empty list means valid).

        When ``app_root`` is provided, resource paths are checked for canonical
        containment (screened canonicalisation + is_relative_to) against it;
        otherwise a lexical check (reject absolute paths and ``..`` segments) is
        applied.

        When ``unchecked`` is a list, a resource path the shared screen did not
        answer for (:data:`PATH_UNVERIFIED_REFUSAL`) is appended there instead of
        to the returned errors, so a caller can tell "not checked" from "invalid"
        without parsing messages.
        """
        errors: list[str] = []

        screened_root = screen_app_path(app_root) if app_root is not None else None

        def path_refusal(rel_path: str) -> str:
            return _app_path_refusal(rel_path, app_root, screened_root)

        def report_path(refusal: str, message: str) -> None:
            if refusal == PATH_UNVERIFIED_REFUSAL and unchecked is not None:
                unchecked.append(message)
            else:
                errors.append(message)

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

        if self.config_keys_error:
            errors.append(self.config_keys_error)

        # Path containment check on all app-root-relative resource paths.
        # Canonical (screened canonicalisation + is_relative_to) when app_root is known; lexical
        # (reject absolute + '..' segments) otherwise. Applied uniformly to
        # agents/skills/sops, ui.entry, ui.pages[].entryPoint AND
        # backend.entryPoint. (A module-style dotted backend.entryPoint such as
        # 'kiro_crew.apps.builtins.x.server' has no '..' and is not absolute, so
        # the helper never false-positives on it.)
        agent_refusals: dict[str, str] = {}
        for path_list_name in ("agents", "skills", "sops"):
            for p in getattr(self, path_list_name):
                refusal = path_refusal(str(p))
                if path_list_name == "agents":
                    agent_refusals[str(p)] = refusal
                if refusal:
                    report_path(refusal, f"{path_list_name} path {refusal}: {p!r}")

        refusal = path_refusal(self.ui.entry)
        if refusal:
            report_path(refusal, f"ui.entry {refusal}: {self.ui.entry!r}")

        refusal = path_refusal(self.backend.entryPoint)
        if refusal:
            report_path(refusal, f"backend.entryPoint {refusal}: {self.backend.entryPoint!r}")

        # A panel-tab entry is mounted by the same ESM host as ui.pages, so it is
        # subject to the same path-containment check.
        for tab in self.contributes.panelTabs:
            refusal = path_refusal(tab.entry)
            if refusal:
                report_path(refusal, f"contributes.panelTabs entry {refusal}: {tab.entry!r}")
        # A link-preview entry is imported by the same ESM host, so same check.
        for preview in self.contributes.linkPreviews:
            refusal = path_refusal(preview.entry)
            if refusal:
                report_path(refusal, f"contributes.linkPreviews entry {refusal}: {preview.entry!r}")

        # UI page validation
        for page in self.ui.pages:
            if not page.route:
                errors.append("ui page missing required field: route")
            if not page.label:
                errors.append("ui page missing required field: label")
            refusal = path_refusal(page.entryPoint)
            if refusal:
                report_path(refusal, f"ui page entryPoint {refusal}: {page.entryPoint!r}")

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

        # Session control validation.
        #
        # Stricter than page validation on purpose: a control renders inside the
        # composer on the path of every turn, so a malformed one must be refused
        # at install time rather than discovered as a broken chat.
        if len(self.contributes.sessionControls) > MAX_SESSION_CONTROLS_PER_APP:
            errors.append(
                f"contributes.sessionControls: at most {MAX_SESSION_CONTROLS_PER_APP} per app "
                f"(declared {len(self.contributes.sessionControls)}) — use a page for further config"
            )
        seen_control_ids: set[str] = set()
        for ctl in self.contributes.sessionControls:
            if not ctl.id:
                errors.append("session control contribution missing required field: id")
            elif not _SESSION_CONTROL_ID_RE.fullmatch(ctl.id):
                errors.append(f"session control contribution id must be kebab-case: {ctl.id!r}")
            elif ctl.id in seen_control_ids:
                # Duplicate ids would make the rendered controls indistinguishable
                # to React's key and to any per-control persistence.
                errors.append(f"session control contribution id is duplicated: {ctl.id!r}")
            else:
                seen_control_ids.add(ctl.id)
            if not ctl.entryPoint:
                errors.append(
                    f"session control contribution {ctl.id or '<unnamed>'} missing required field: entryPoint"
                )
            elif refusal := path_refusal(ctl.entryPoint):
                report_path(
                    refusal,
                    f"session control contribution entryPoint {refusal}: {ctl.entryPoint!r}",
                )
            if ctl.statusPath and not _SESSION_CONTROL_STATUS_PATH_RE.fullmatch(ctl.statusPath):
                # Refused rather than ignored: a status route the dashboard
                # declines to call would leave the chip permanently stateless
                # with nothing saying why.
                errors.append(
                    f"session control contribution {ctl.id or '<unnamed>'} statusPath must be a relative "
                    f"backend route (lowercase, no scheme, host or query): {ctl.statusPath!r}"
                )

        # Cron validation. The cron modules are imported only for a manifest
        # that declares crons: a manifest is also read by the stdio MCP servers
        # at start-up, for its config keys, and the cron service is heavy.
        if self.crons:
            from kiro_crew.cron import is_valid_skip_date, is_valid_timezone
            from kiro_crew.cron_service.fields import validate_create_timeouts
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
            # Same create-time rule build_job enforces, so a bad pair is
            # reported at install/validate time instead of only as a logged
            # per-job registration failure.
            try:
                validate_create_timeouts(
                    cron.timeout, cron.timeout_secs, command=cron.command, script=cron.script
                )
            except ValueError as exc:
                errors.append(f"cron entry {cron.name!r}: {exc}")
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
        cron_grant_error = cron_permission_error(self)
        if cron_grant_error:
            errors.append(cron_grant_error)

        # Backend hooks validation
        errors.extend(self.backend.hooks.validate())

        # onEnable / onDisable timeouts that are not a number at all
        errors.extend(self.setup.validate())

        # Platform names the install and enable gates compare against
        errors.extend(self.platform.validate())

        # Notification channel validation (RFC Phase 2: 8-channel cap, kebab ids)
        errors.extend(self.notifications.validate())

        # Contributed commands, panel tabs and file-menu rows: ids, caps,
        # prompt/argument agreement, matcher kind, entry paths, and the file-menu
        # surfaces / when-filter grammar.
        errors.extend(self.contributes.validate())

        # A contributed command's `agent` is contained to the agents THIS app ships.
        # The prompt is app-authored, so letting it name any host agent would let an
        # app run its text under an agent carrying broader tool grants and
        # auto-approvals than the default. Refused at install, where the app's own
        # `agents` are known, rather than clamped later where nobody sees it.
        commands_with_agent = [c for c in self.contributes.commands if c.agent]
        if commands_with_agent:
            unchecked_agents: list[str] = []
            own = _declared_agent_names(
                self.agents, app_root, unchecked_agents, known_refusals=agent_refusals
            )
            for cmd in commands_with_agent:
                if cmd.agent in own:
                    continue
                if unchecked_agents:
                    # An agent path the screen could not check may be the one that
                    # declares this name, so the miss is not a verdict yet.
                    report_path(
                        PATH_UNVERIFIED_REFUSAL,
                        f"contributes.commands[{cmd.id or '?'}]: agent {cmd.agent!r} "
                        f"{PATH_UNVERIFIED_REFUSAL}: {', '.join(unchecked_agents)}",
                    )
                    continue
                errors.append(
                    f"contributes.commands[{cmd.id or '?'}]: agent {cmd.agent!r} is "
                    "not one of this app's own agents (declared in `agents`)"
                )

        # A contributed row's endpoint is checked against the app's OWN namespace here,
        # where the name is known -- refusing it at install is what keeps a declaration
        # naming a core route (`/api/shutdown`) from ever reaching the dashboard, which
        # would POST to it with the reader's session on a row the reader clicked.
        for item in self.contributes.fileMenuItems:
            if item.endpoint and not app_endpoint_allowed(
                self.name, item.endpoint, allow_proxy_namespace=True
            ):
                errors.append(
                    f"contributes.fileMenuItems[{item.id or '?'}]: endpoint "
                    f"{item.endpoint!r} must route under /api/apps/{self.name}/ "
                    f"or /apps/{self.name}/api/ "
                    "(no traversal, no other app's namespace, no core route)"
                )

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
        if self.config_keys:
            body["config_keys"] = sorted(self.config_keys)
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
        if (
            self.contributes.commands
            or self.contributes.sessionControls
            or self.contributes.panelTabs
            or self.contributes.fileMenuItems
            # A link-preview `entry` is an ESM module the dashboard imports and runs
            # in its own origin, like a panel tab's: same reason to sign it.
            or self.contributes.linkPreviews
        ):
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
            # existed keep producing the identical payload -- and an app contributing
            # only commands produces the same bytes it did before panel tabs and file
            # menus existed.
            #
            # `panelTabs` is in the guard for a sharper version of the same reason: a
            # tab's `entry` names an ESM module the AppHost imports and RUNS in the
            # dashboard's own origin. Guarding on commands/sessionControls alone left a
            # panelTabs-ONLY manifest out of the payload entirely, so its `entry` was
            # the one part of a signed app an attacker could repoint with the signature
            # still verifying.
            #
            # A contributed file-menu row is covered for the same reason at one remove:
            # its `endpoint` is where the host POSTs the path of a file the reader picked,
            # so rewriting it on a signed app redirects that dispatch while every visible
            # character of the row, and the signature, stay exactly as published.
            body["contributes"] = self.contributes.to_dict()
        setup_d = self.setup.to_dict()
        if setup_d:
            # `onInstall`/`onUpdate`/`onUninstall`/`onEnable`/`onDisable` are shell
            # text `run_lifecycle_script` hands to `/bin/bash -c`. The string IS the
            # program -- no file in the package has to exist for it to run -- so a
            # manifest-only rewrite of a published app buys execution outright. Same
            # reason `crons` is covered one clause up: vetting bounds the SYNTAX of
            # what runs, only the signature authenticates PUBLISHER INTENT.
            # Included only when non-empty so manifests signed before setup was
            # covered keep producing the identical payload.
            body["setup"] = setup_d
        if self.mcpServers:
            # Each entry's `command`/`args`/`env` is written into the agent config
            # kiro-cli reads and SPAWNS (`bridges._register_mcp_servers`). Like
            # `setup`, the executed argv lives in the manifest itself, so this is the
            # one part of a signed app whose program an attacker could swap with the
            # signature still verifying.
            # Included only when non-empty so manifests signed before mcpServers was
            # covered keep producing the identical payload.
            body["mcpServers"] = dict(self.mcpServers)
        backend_d = self.backend.to_dict()
        if backend_d:
            # `hooks.*` names a module:callable `module_loader.load_app_module`
            # imports INTO THE GATEWAY PROCESS, and `entryPoint` + `type` select the
            # file and interpreter `_start_app_backend_body` spawns. These are
            # selectors rather than literal argv, so repointing one at a module that
            # already sits in an otherwise untouched package is enough -- and a
            # manifest-only tamper is precisely what the signature is the sole
            # detector for.
            # The whole canonical dict, not the executed keys alone: cherry-picking
            # leaves `port`/`healthCheck`/`routes` as residue an attacker may still
            # move, and one guard is smaller than four.
            # Included only when non-empty so manifests signed before backend was
            # covered keep producing the identical payload.
            body["backend"] = backend_d
        ui_d = self.ui.to_dict()
        if ui_d:
            # `entry` and every `pages[].entryPoint` name an ESM module the dashboard
            # builds `/apps/<name>/ui/<path>` from and dynamic-`import()`s in its OWN
            # origin (`website/src/components/AppHost.tsx`) -- the same surface
            # `contributes.panelTabs[].entry` is covered for one clause up. Outside the
            # payload, the derivative is signed and the original is not.
            # The whole canonical dict, not the two executed keys alone: a
            # `pages[].route` decides which URL serves that bundle, and a page's
            # `label`/`icon` plus `sidebar` are the whole of what the reader sees
            # before clicking it -- so cherry-picking the entry paths leaves the
            # routing and the presentation of an attacker-chosen bundle as residue.
            # Included only when non-empty so manifests signed before ui was covered
            # keep producing the identical payload.
            body["ui"] = ui_d
        if self.agents:
            # Each path names a JSON agent spec `bridges._register_agents` writes into
            # the user's agents dir, carrying its own `tools`/`allowedTools`/`model`/
            # `prompt`. APPENDING one entry is enough to get an attacker-authored agent
            # materialized, and the same guard covers that: the payload is recomputed
            # from the manifest as received, so a package that GAINS an `agents` key
            # gains it in the signed bytes too and the signature stops matching.
            # Included only when non-empty so manifests signed before agents was
            # covered keep producing the identical payload. List order preserved.
            body["agents"] = list(self.agents)
        if self.skills:
            # `bridges._register_skills` symlinks each directory into the skills root,
            # both namespaced and flat, so a repointed entry puts attacker-authored
            # skill instructions where an agent reads them as its own.
            # Included only when non-empty so manifests signed before skills was
            # covered keep producing the identical payload. List order preserved.
            body["skills"] = list(self.skills)
        if self.sops:
            # Same shape as `skills` one clause up: the file's text becomes procedure an
            # agent follows, and only the signature authenticates whose text it is.
            # Included only when non-empty so manifests signed before sops was covered
            # keep producing the identical payload. List order preserved.
            body["sops"] = list(self.sops)
        dependencies_d = self.dependencies.to_dict()
        if dependencies_d:
            # `capabilities.mcp` is an id `dependencies.resolve_dependencies` hands to
            # `CapabilityManager.install_mcp` AT INSTALL TIME, so a swapped id installs
            # an attacker's MCP server -- whose command an agent then spawns -- under
            # the publisher's signature.
            # The whole canonical dict, not `capabilities` alone: `managedBy` is the
            # switch deciding whether the gateway installs the capability at all, and
            # `commands`/`optionalCommands` name host executables the install chain
            # probes and reports as missing.
            # Included only when non-empty so manifests signed before dependencies was
            # covered keep producing the identical payload.
            body["dependencies"] = dependencies_d
        platform_d = self.platform.to_dict()
        if platform_d:
            # `clientInstall.shell` is a one-liner the App Store hands the reader to
            # PASTE INTO A TERMINAL -- `install_from_registry` returns the whole `clientInstall`
            # dict with `needsClientInstall: True`, under a signature-verified publisher badge.
            # No file in the package has to exist for it to run, which is the property
            # that put `setup` in the payload.
            # The whole canonical dict, not `clientInstall` alone: `installMode:
            # "client"` is what makes that block reach the reader at all, so signing the
            # one-liner without the switch that surfaces it is the cherry-pick this
            # method avoids everywhere else.
            # Included only when non-empty so manifests signed before platform was
            # covered keep producing the identical payload.
            body["platform"] = platform_d
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
        if self.config_keys:
            d["config_keys"] = self.config_keys
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

        config_keys_raw = data.get("config_keys", [])
        if not isinstance(config_keys_raw, list):
            config_keys: list[str] = []
            config_keys_error = "config_keys must be an array"
        elif len(config_keys_raw) > _MAX_CONFIG_KEYS:
            config_keys = []
            config_keys_error = f"config_keys must contain at most {_MAX_CONFIG_KEYS} entries"
        elif not all(isinstance(key, str) and key for key in config_keys_raw):
            config_keys = []
            config_keys_error = "config_keys entries must be non-empty strings"
        elif any(len(key) > _MAX_CONFIG_KEY_CHARS for key in config_keys_raw):
            config_keys = []
            config_keys_error = (
                f"config_keys entries must be at most {_MAX_CONFIG_KEY_CHARS} characters"
            )
        else:
            config_keys = list(config_keys_raw)
            config_keys_error = ""

        # A present non-list value for a list field reads as absent: `get` returns a
        # stored `null` instead of the default, and iterating it would raise (or split
        # a string into characters). The same guard covers every top-level list below.
        crons_raw = data.get("crons", [])
        if not isinstance(crons_raw, list):
            crons_raw = []
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
            agents=[
                str(a)
                for a in (data["agents"] if isinstance(data.get("agents"), list) else [])
                if a
            ],
            skills=[
                str(s.get("path", s.get("name", "")) if isinstance(s, dict) else s)
                for s in (data["skills"] if isinstance(data.get("skills"), list) else [])
                if s
            ],
            sops=[
                str(s) for s in (data["sops"] if isinstance(data.get("sops"), list) else []) if s
            ],
            mcpServers=dict(data.get("mcpServers", {})),  # noqa: N815
            config_keys=config_keys,
            config_keys_error=config_keys_error,
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
            tags=[
                str(t) for t in (data["tags"] if isinstance(data.get("tags"), list) else []) if t
            ],
            jobFamilies=[  # noqa: N815
                str(j)
                for j in (data["jobFamilies"] if isinstance(data.get("jobFamilies"), list) else [])
                if j
            ],
            extra=extra,
        )

    @classmethod
    def from_json_file(cls, path: Path) -> AppManifest:
        """Parse from an ``app.json`` file."""
        data = json.loads(read_bytes_with_retry(path).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"app.json must be a JSON object, got {type(data).__name__}")
        return cls.from_dict(data)


# ---------------------------------------------------------------------------
# What the runtime provisions out of process
# ---------------------------------------------------------------------------
#
# The app runtime installs an app's root ``requirements.txt`` with
# ``pip install --target`` into the app's own deps directory, a tree that
# reaches processes spawned on the app's behalf and never the gateway's own
# import path. Exactly two paths do it, and each decides from the typed
# manifest plus the app root. The predicates below ARE that decision, imported
# by both provisioners (``backend.py`` at the spawn of a backend entry point,
# ``bridges_mcp.py`` at the registration of a stdio ``mcpServers`` entry) and by
# the install-time desktop gate that mirrors them, so the three sites cannot
# drift: ``test_apps_provisioning_predicate.py`` pins each site to these names.

#: A ``backend.entryPoint`` ending in one of these is a script FILE even when
#: it contains dots (``run.server.py``); anything else dotted is a module path.
SCRIPT_ENTRY_POINT_SUFFIXES: tuple[str, ...] = (".py", ".js", ".ts", ".mjs", ".cjs", ".sh")

#: Suffixes the backend spawn launches with ``node`` (``backend.type: "node"``
#: chooses the same branch by declaration).
NODE_ENTRY_POINT_SUFFIXES: tuple[str, ...] = (".js", ".mjs", ".cjs")

#: The suffix the backend spawn launches as a shell script (``backend.type:
#: "exec"`` chooses the same branch by declaration, and so does an extensionless
#: executable whose first line is a shebang naming no ``python``).
SHELL_ENTRY_POINT_SUFFIX = ".sh"


def spawn_launches_entry_point_as_python(manifest: AppManifest, app_root: Path) -> bool:
    """True when the backend spawn would run ``backend.entryPoint`` as a PYTHON child.

    The one file-style shape the provisioned deps tree REACHES. ``backend.py``
    pip-installs the root ``requirements.txt`` at the spawn of any file-style
    entry, but hands the tree to a Python child only -- through the ``deps_boot``
    shim when the child runs the gateway interpreter, through the shim under an
    ABI-matched shebang, or on ``PYTHONPATH`` for an ABI-matched interpreter (the
    transport block after the launch is built) -- and a node or shell child gets
    none of the three: its dependencies land beside it, unreachable. Mirrors the
    spawn's own dispatch (``_start_app_backend_body``, ``_is_shell_entry``): an
    explicit ``backend.type`` of ``node`` or ``exec``, a node suffix, or ``.sh``
    is not Python; an extensionless EXECUTABLE whose first line is a shebang
    naming no ``python`` is a shell launcher; everything else -- ``.py``, a dotted
    file name such as ``server.main``, an extensionless script the spawn feeds
    to the interpreter -- is Python. Asked of the tree the spawn will resolve
    the name under, like :func:`file_entry_point_refusal`; an extensionless
    entry that is not there yet is judged by name alone.
    """
    entry_point = manifest.backend.entryPoint
    if manifest.backend.type in ("node", "exec"):
        return False
    if entry_point.endswith(NODE_ENTRY_POINT_SUFFIXES) or entry_point.endswith(
        SHELL_ENTRY_POINT_SUFFIX
    ):
        return False
    if "." in entry_point.rsplit("/", 1)[-1]:
        return True
    entry = app_root / entry_point
    try:
        if not os.access(entry, os.X_OK):
            return True
        with open(entry, "rb") as fh:
            first_line = fh.readline(256)
    except OSError:
        return True
    return not (first_line.startswith(b"#!") and b"python" not in first_line)


def is_module_style_entry_point(entry_point: str, app_root: Path) -> bool:
    """True when ``backend.entryPoint`` names a dotted Python module, not a file.

    The backend spawn runs such an entry with ``python -m`` from the trusted
    package (a built-in app living inside the Kiro Crew package itself), never
    from the writable app directory, so nothing found in that directory -- a
    ``requirements.txt`` included -- is provisioned for it. Four conditions
    together decide the shape: no path separator, no script suffix, at least
    one dot, and no file of that literal name under *app_root* (a file named
    ``server.main`` is a file). An empty entry point is not module-style.
    """
    return (
        "/" not in entry_point
        and not entry_point.endswith(SCRIPT_ENTRY_POINT_SUFFIXES)
        and "." in entry_point
        and not (app_root / entry_point).exists()
    )


def has_stdio_mcp_server(manifest: AppManifest) -> bool:
    """True when the manifest declares a stdio ``mcpServers`` entry: one without ``url``.

    A ``url`` server is remote and spawns nothing on the app's behalf. A stdio
    server is a process the gateway launches, and its Python imports resolve
    from the app's provisioned deps tree, which is why its presence is what
    makes ``bridges_mcp.py`` provision at registration.
    """
    return any(isinstance(cfg, dict) and not cfg.get("url") for cfg in manifest.mcpServers.values())


def file_entry_point_refusal(entry_point: str, app_root: Path) -> str:
    """Why the backend spawn would refuse *entry_point* as a FILE-style entry,
    or ``""`` when it would spawn it.

    The spawn's own precondition (``backend.py::_start_app_backend_body``),
    spelled once: the entry must be a regular file under *app_root* whose
    resolution stays inside the root. Returns the spawn's reason -- ``"not
    found"``, ``"escapes app root"``, ``"refused by the path screen"``, ``"path
    resolution failed"`` (the screen could not finish checking it) -- so the
    spawn can log it and :func:`runtime_provisions_requirements` can treat any
    reason as "the backend provisioner never runs for this entry".
    """
    entry = app_root / entry_point
    # Containment is asked of the shared screen before anything follows the
    # entry's links, ``is_file`` included.
    contained = app_root_containment(entry, app_root)
    if contained is Containment.UNCHECKED:
        return "path resolution failed"
    if contained is Containment.REFUSED:
        return "refused by the path screen"
    if contained is Containment.OUTSIDE:
        return "escapes app root"
    if not entry.is_file():
        return "not found"
    return ""


def runtime_provisions_requirements(manifest: AppManifest, app_root: Path) -> bool:
    """True when the runtime installs the app's root ``requirements.txt`` out of
    process FOR A PROCESS THAT RECEIVES IT.

    Composed from the same predicates the two provisioners call, plus the one
    fact the provisioners do not decide by -- which child the installed tree
    reaches:

    - ``backend.py`` provisions at the spawn of any FILE-style ``backend.entryPoint``
      -- the spawn runs only for an entry that IS a regular file inside the app
      root (:func:`file_entry_point_refusal`); a declared file the spawn refuses
      is returned before provisioning -- but hands the tree to a PYTHON child
      only (:func:`spawn_launches_entry_point_as_python`): a shell or node entry is
      spawned with the deps installed beside it and no way to import them, so it
      counts here as no consumer. Never for a module-style entry, which executes
      trusted package code;
    - ``bridges_mcp.py`` provisions at the registration of a stdio ``mcpServers``
      entry for an app whose entry point is absent or file-style -- the same
      module-style exclusion, and no existence requirement of its own: the
      stdio server is the consumer it provisions for, and the one that can still
      make this true beside a shell or node entry.

    So a module-style entry point is never provisioned, whatever else the
    manifest declares; a ``.py`` entry point that would spawn always is; a
    non-Python file entry, or a declared file the spawn would refuse, leaves only
    a stdio server to make it true; without an entry point, a stdio server is.
    Says nothing about ``backend.hooks``: a hook is imported into the gateway
    process, which the deps tree never reaches, so a gate that waives an
    install-time refusal on this predicate must exclude hooks itself.
    """
    entry_point = manifest.backend.entryPoint
    if entry_point:
        if is_module_style_entry_point(entry_point, app_root):
            return False
        if spawn_launches_entry_point_as_python(
            manifest, app_root
        ) and not file_entry_point_refusal(entry_point, app_root):
            return True
    return has_stdio_mcp_server(manifest)


#: Read cap for an app's ``requirements.txt``: the provisioner buffers it in the
#: GATEWAY's memory, so an oversized, app-controlled file must exhaust a bounded
#: buffer, not the gateway. 1 MiB is orders of magnitude beyond any real
#: requirements.txt. Owned here so the one acceptance rule below can apply the
#: same cap the provisioner's bounded read enforces.
REQUIREMENTS_TXT_MAX_BYTES = 1024 * 1024


def requirements_in_tree(app_root: Path, req_file: Path) -> tuple[Path, Path] | None:
    """The strictly-resolved ``(app_root, requirements.txt)`` pair when *req_file*
    is one the runtime will read, else ``None``.

    The provisioner's own acceptance rule for the file, spelled once: the entry
    must strictly resolve (a dangling link is ``None``) to a regular file (a
    directory is ``None``) inside the strictly-resolved app root (a link that
    escapes it is ``None``), no larger than :data:`REQUIREMENTS_TXT_MAX_BYTES`
    (an oversized file is ``None``; the provisioner's bounded read refuses it).
    ``requirements.txt -> requirements/prod.txt`` is legitimate layout and
    resolves.

    Three callers, one rule. ``backend_runtime/provisioning.py``'s provisioning read and its
    activation gate use it as the fast refusal before their descriptor-pinned,
    every-component-no-follow open of the returned target -- that open, and the
    bounded read through it, are the security boundary; this is not, and it
    stays where it is. ``registry_pipeline/install_desktop_gate.py``'s install-time desktop
    gate uses it to predict what those two will do, so it never waives a file the
    provisioner would refuse. ``None`` on any resolution error, so no caller catches here.
    """
    try:
        root_resolved = app_root.resolve(strict=True)
        target = req_file.resolve(strict=True)
        if not target.is_file() or not target.is_relative_to(root_resolved):
            return None
        if target.stat().st_size > REQUIREMENTS_TXT_MAX_BYTES:
            return None
    except (OSError, RuntimeError):
        # RuntimeError: a symlink loop, which Path.resolve raises as such before
        # Python 3.13 (ELOOP, an OSError, from then on). Either way "not a file
        # the runtime will read", never an exception for a caller to turn into a
        # 500.
        return None
    return root_resolved, target


#: Cron helpers served as attributes of this module on first access, so
#: ``kiro_crew.apps.manifest.<name>`` is the object its home module holds. The
#: cron service is heavy and the stdio MCP servers read manifests at start-up.
_LAZY_CRON_NAMES: dict[str, str] = {
    "is_valid_skip_date": "kiro_crew.cron",
    "is_valid_timezone": "kiro_crew.cron",
    "validate_create_timeouts": "kiro_crew.cron_service.fields",
}


def __getattr__(name: str) -> Any:
    module = _LAZY_CRON_NAMES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value
