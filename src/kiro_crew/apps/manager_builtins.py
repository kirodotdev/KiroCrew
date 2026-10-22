"""Built-in apps: registering them, promoting default-on ones, and retiring migrated ones.

``register_builtin_apps`` and the definitions it registers from, the one-shot
default-on backfill, the backend URL a builtin's ``mcpServers`` declares
(``resolve_mcp_backend_url``), orphan detection with its cache, and the cleanup of a
builtin whose feature moved into core.

Part of the ``kiro_crew.apps.manager`` family: callers and tests reach every name here
as ``kiro_crew.apps.manager.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from kiro_crew.apps.discovery import discover_builtin_apps
from kiro_crew.apps.manager_activation import _app_activation_denied
from kiro_crew.apps.manager_records import (
    APP_MANIFEST_FILENAME,
    INSTALLED_META_FILENAME,
    AppResult,
    InstalledApp,
    _now_iso,
    _read_installed,
    _write_installed,
    app_dir,
    apps_dir,
    hold_app_record_lock,
    holds_app_record_locks,
    refresh_app_config_keys,
)
from kiro_crew.apps.manager_tree import _check_path_safety, gateway_data_dir_obstruction
from kiro_crew.apps.manifest import app_name_error
from kiro_crew.atomic_write import atomic_write
from kiro_crew.owner_only_files import ensure_directory
from kiro_crew.pinned_fs import supports_pinned_walk
from kiro_crew.platform import current_context, safe_context_call
from kiro_crew.platform_compat import is_link_or_junction

logger = logging.getLogger("kiro_crew.apps.manager")


# ---------------------------------------------------------------------------
# Built-in app registration
# ---------------------------------------------------------------------------

# Built-in apps are features baked into the Kiro Crew dashboard that we
# surface in the App Store as "builtin" entries.  They use the host's
# React tree directly (no ESM bundle) and their page components resolve
# through ``BUILTIN_COMPONENT_REGISTRY`` in the frontend.  The registration
# here is metadata-only so the App Store can display them alongside
# installable apps.
#
# Default-disabled policy: a builtin app ships with ``defaultEnabled: False``
# so a fresh install presents a minimal sidebar (core surfaces only) instead of
# every app at once. Apps are opt-in from the App Store Browse tab. Because
# ``register_builtin_apps()`` applies ``defaultEnabled`` only on first
# registration and preserves user state on restart, existing users keep
# whatever they already enabled — this only changes the out-of-the-box
# experience for new installs.
#
# The exception is _DEFAULT_ON_BUILTINS below.

# Builtins deliberately shipped ENABLED on a fresh install, exempt from the
# opt-in policy above because they are core surfaces rather than optional
# add-ons. Adding a name here is a product decision, not a convenience — keep
# the set small. A default-on builtin still honors the ``apps`` governance
# allowlist at registration (see _app_activation_denied), so a deny-by-default
# host policy is never bypassed.
#
# This is the single source of truth for the exemption: the policy tests over
# both the hardcoded list and the file-based manifests read it from here, so a
# builtin cannot become default-on in one registration path while the other
# path's test still forbids it.
_DEFAULT_ON_BUILTINS: frozenset[str] = frozenset(
    {
        "projects",  # Task Runner
        # Command Bar replaces the quick-search (Cmd+K) surface rather than adding
        # a sidebar entry, so shipping it off leaves the gesture on the legacy
        # palette and the launcher unseen. Disabling the app is what restores the
        # old surface, which is the opt-out this exemption trades for.
        "command-bar",
    }
)


# Promotions still owed to installs that PREDATE them — a different question from
# the set above, and the distinction is load-bearing.
#
# ``_DEFAULT_ON_BUILTINS`` answers "what does a FRESH install enable". This set
# answers "which promotion has not yet reached installs that registered the app
# while it was still default-off". Reading the first set for the second question
# reverses deliberate opt-outs: ``projects`` (Task Runner) has shipped
# ``defaultEnabled: true`` since it was aligned with the other builtins, long
# before this allowlist existed, so it has been enabled and visible in the
# sidebar on every existing install. A record showing ``enabled: false`` for it
# is therefore a user who FOUND it and turned it off — the opposite of the
# population a backfill exists to serve.
#
# So a name belongs here only when both hold: a fresh install enables it (it is
# in the set above), and existing installs were never in a position to choose.
# ``command-bar`` qualifies because it was default-OFF at first registration for
# those installs AND, replacing the quick-search surface rather than adding a
# sidebar entry, it appears on no store or launcher surface they could have found
# it on. An app already default-on when they installed it never qualifies.
#
# Entries are permanent, not cleaned up after a release: the marker is per
# install, so a user restoring an old data home still gets the promotion once.
_DEFAULT_ON_BACKFILL: frozenset[str] = frozenset({"command-bar"})


@holds_app_record_locks
def backfill_default_on_builtins() -> list[str]:
    """Deliver a default-on PROMOTION to installs that predate it. One-shot per app.

    ``register_builtin_apps()`` applies ``defaultEnabled`` only on FIRST
    registration and preserves user state on every later start, so adding a name
    to ``_DEFAULT_ON_BUILTINS`` reaches NEW installs only. An install that
    registered the app while it was still default-off keeps ``enabled: false``
    through every subsequent restart, update and version bump — the record lives
    in the user's data home, which a code update does not touch.

    That is survivable for a builtin that adds a sidebar entry, because the App
    Store can still offer it. It is not survivable for one that replaces a host
    surface: it has no page, so it is absent from the launcher's own app list,
    and it is absent from Discover unless the published catalog carries a row for
    it, which leaves a disabled row in Library as the only trace. Those users
    cannot enable what they have no way to learn exists.

    Reads ``_DEFAULT_ON_BACKFILL``, NOT ``_DEFAULT_ON_BUILTINS`` — see that set's
    comment for why conflating the two silently reverses deliberate opt-outs.

    ONE-SHOT, and the record of that is ``InstalledApp.defaultOnBackfilled``,
    written in the SAME atomic record write that flips ``enabled``. One document
    deliberately: a separate marker file has no correct ordering, because
    whichever of the two writes goes first leaves a window the other one owns.
    Marker-last loses the record of an enable that happened, so every later start
    re-applies the promotion and reverses the user's own disable forever;
    marker-first can outlive a flip that failed, so the app is skipped forever and
    the promotion is never delivered. Both are real; neither is reachable when the
    flag and the state it guards land or fail together.

    Surviving a user's disable is the point: disabling the app is the ONLY thing
    that gives a replaced host surface back. Per app rather than per install, so a
    promotion added in a later release is still delivered.

    Returns the names actually flipped, so the caller can log them.
    """
    flipped: list[str] = []
    for name in sorted(_DEFAULT_ON_BACKFILL):
        hold_app_record_lock(name)
        existing = _read_installed(name)
        if existing is None:
            # Not registered on this install (an older wheel does not ship the
            # app). A record created LATER is born already flagged, because a
            # first registration under the promoted default IS the promotion
            # being received — see register_builtin_apps().
            continue
        if not _builtin_owns_install(existing):
            # A USER installed an app under this name. Same boundary
            # register_builtin_apps() keeps: never touch their entry.
            continue
        if existing.defaultOnBackfilled:
            continue
        turning_on = not existing.enabled
        if turning_on:
            denied = _app_activation_denied(name)
            if denied:
                # Mirror the gate register_builtin_apps() applies to a default-on
                # builtin: a deny-by-default host policy is not bypassed by
                # arriving through the backfill. Deliberately NOT flagged — if the
                # policy later permits the app, the promotion is still owed.
                logger.info("Default-on backfill skipped %s: %s", name, denied)
                continue
            existing.enabled = True
        existing.defaultOnBackfilled = True
        existing.updatedAt = _now_iso()
        # atomic_write, so a failure here persists NEITHER the flag nor the enable
        # and the promotion is simply retried on the next start. The failure
        # propagates out of this function (the caller logs it and continues
        # startup), so no partially-delivered state and no half-truthful return
        # value is observable. `flipped` is appended after the write to keep that
        # reading obvious, not because anything could observe the other order.
        _write_installed(name, existing)
        if turning_on:
            flipped.append(name)
            _audit_default_on_backfill(name)
    return flipped


def _audit_default_on_backfill(name: str) -> None:
    """Record that *name* was activated with no user request behind it.

    The dashboard and CLI enable paths are reachable only by someone asking; this
    one runs at startup, and activation is the chokepoint where an app starts
    contributing agents, skills, crons and routes. An operator reconstructing
    "when did this app become active, and who asked for it" would otherwise find
    nothing at all. Same shape as the trust-grant withdrawal above: emitted AFTER
    the write so it attests something that actually happened, and never allowed to
    fail the operation — losing the audit line is bad, refusing to deliver a
    promotion because the audit sink is unavailable is worse.
    """
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller="gateway",
            operation="app_default_on_backfill",
            outcome="allowed",
            source="startup",
            resources=f"{name}=enabled_by_promotion_backfill",
        )
    except Exception:  # noqa: BLE001 - the activation already happened
        logger.warning("could not audit the default-on backfill for %r", name, exc_info=True)


# EMPTY, and that is a finished migration rather than an oversight. Every builtin now
# ships as a file-based manifest under ``builtins/<dir>/app.json`` and is picked up by
# ``discover_builtin_apps()``. ``agent-worlds`` and ``channels`` were the last two
# hardcoded entries; they moved to ``builtins/agent_worlds/app.json`` and
# ``builtins/channels/app.json`` with every field byte-identical, including the
# ``defaultEnabled: false`` / ``hidden: true`` flags, which survive because
# ``_manifest_to_builtin_dict`` copies ``AppManifest.extra`` verbatim.
#
# One thing JSON cannot carry came with them, so it is recorded here: ``channels`` sets
# ``hidden: true`` to keep itself out of the App Store Browse grid only. Its code and
# routes stay fully intact and it is enabled with ``kirocrew app enable channels``.
# ``hidden`` gates store visibility, nothing else.
#
# Why it had to happen for i18n: the display copy of a builtin is localised by the
# ``APP_MANIFEST_KEY`` table in ``website/src/components/appstore/appManifest.ts``, and
# ``scripts/check-app-manifest-sync.mjs`` proves the English catalog value still equals
# the manifest's own prose. A manifest that lives in a Python literal has no file for
# that check to read, so these two apps would have been the only builtins whose copy
# could drift silently.
#
# It stays a list rather than being deleted because it is still the ADD-only precedence
# seam: ``register_builtin_apps`` and ``detect_orphaned_builtins`` union it with the
# discovered and edition-contributed sets, so an edition (or a test) can inject a
# builtin that outranks a discovered one without reintroducing the hardcoding. Prefer a
# file manifest; reach for this only when there is no directory to put one in.
_BUILTIN_APPS: list[dict[str, Any]] = []


_REQUIRED_BUILTIN_FIELDS = {"name", "version", "displayName", "description", "author"}


def _validate_builtin_app(app_data: dict[str, Any]) -> list[str]:
    """Validate a builtin app definition. Returns list of errors (empty = valid).

    Builtin App Definition Schema:

    Required fields:
      - name (str): Kebab-case app identifier (e.g. "my-feature")
      - version (str): Semver version string (e.g. "1.0.0")
      - displayName (str): Human-readable name shown in App Store
      - description (str): Short description for App Store listing
      - author (str): Author name or team

    Optional fields:
      - tags (list[str]): Categorization tags for discovery
      - defaultEnabled (bool): Initial enabled state on first registration.
          Default: True. Set to False for apps that should be opt-in.
      - permissions (dict): API and event permissions declaration
      - ui (dict): UI configuration with "pages" list for sidebar entries
          Each page: {"route": str, "label": str, "icon": str}
    """
    errors: list[str] = []
    for field in _REQUIRED_BUILTIN_FIELDS:
        if not app_data.get(field):
            errors.append(f"missing required field: {field}")
    if "defaultEnabled" in app_data and not isinstance(app_data["defaultEnabled"], bool):
        errors.append("defaultEnabled must be a boolean")
    name = app_data.get("name", "")
    if name and not _check_path_safety(name):
        errors.append(f"unsafe app name: {name!r}")
    elif name:
        # Builtins are registered from a dict, never through AppManifest, so the
        # shared contract has to be applied here too — otherwise an edition's
        # AppsLoader could contribute a name the manifest path would refuse.
        name_error = app_name_error(name)
        if name_error:
            errors.append(name_error)
    # migratedTo validation is lenient — invalid formats are handled by
    # _effective_migrated_to() which returns "" for bad values.  We log a
    # warning in register_builtin_apps() but do NOT block registration.
    # See design doc: "Log warning, skip the migratedTo field (app still
    # registers normally)".
    return errors


def _effective_migrated_to(app_data: dict[str, Any]) -> str:
    """Return migratedTo value if valid format, else empty string.

    Pure helper — does not mutate app_data.
    """
    migrated_to = app_data.get("migratedTo", "")
    if migrated_to and not re.match(
        r"^(registry|standalone):[a-z][a-z0-9]*(-[a-z0-9]+)*$", migrated_to
    ):
        return ""
    return migrated_to


def _edition_builtin_apps() -> list[dict[str, Any]]:
    """Builtin apps contributed by the active PlatformContext's AppsLoader.

    The Default ``AppsLoader`` returns empty ``manifest_sources`` so the
    standalone discovery set is exactly the package's ``builtins/`` dir — no
    extra apps, byte-for-byte today's behavior.  The internal companion returns a
    directory (inside the companion package) holding the feature-app
    ``app.json`` manifests; each such dir is scanned with the SAME
    ``discover_builtin_apps`` logic (subdir-with-app.json → app dict), so the
    companion's apps are namespaced/validated/registered identically to the
    OSS builtins.  Missing dirs are skipped gracefully by ``discover_builtin_apps``.
    """
    # Fail-closed via safe_context_call: a non-standalone host that cannot compose
    # re-raises PlatformCompositionError (never silently degrades to the OSS builtin
    # set); any other lookup failure falls back to no edition sources.
    _no_sources: list[Path] = []
    sources = safe_context_call(
        lambda: current_context().apps_loader.manifest_sources(),
        fallback=_no_sources,
        log_message="apps_loader.manifest_sources lookup failed; using none",
    )

    apps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in sources:
        # discover_builtin_apps already skips a non-existent dir and validates
        # each manifest, so a bad/missing source can never break registration.
        for app_data in discover_builtin_apps(Path(source)):
            name = app_data.get("name", "")
            if name and name not in seen:
                seen.add(name)
                apps.append(app_data)
    return apps


def _edition_bundled_app_names() -> list[str]:
    """Names the active edition declares it bundles (PlatformContext).

    The Default ``AppsLoader`` returns the OSS builtins (``auto_research`` /
    ``file_explorer``) which are already covered by the package's ``builtins/``
    discovery, so this is a no-op for standalone.  The internal companion declares
    its feature-app names; used by orphan detection so a declared app is never
    mis-orphaned even if its manifest dir is momentarily unavailable.
    """
    # Fail-closed via safe_context_call (see _edition_builtin_apps above).
    _no_names: list[str] = []
    return list(
        safe_context_call(
            lambda: current_context().apps_loader.bundled_app_names(),
            fallback=_no_names,
            log_message="apps_loader.bundled_app_names lookup failed; using none",
        )
    )


def _rmtree_dirfd(fd: int) -> None:
    """Recursively delete the contents of an OPEN directory descriptor using
    only dir_fd-relative operations — immune to rename/symlink swaps because
    no absolute path is ever re-resolved."""
    with os.scandir(fd) as it:
        entries = list(it)
    for entry in entries:
        if entry.is_dir(follow_symlinks=False):
            child = os.open(
                entry.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_DIRECTORY,
                dir_fd=fd,
            )
            try:
                _rmtree_dirfd(child)
            finally:
                os.close(child)
            os.rmdir(entry.name, dir_fd=fd)
        else:
            os.unlink(entry.name, dir_fd=fd)


def _dirfd_ops_supported() -> bool:
    # supports_pinned_walk covers the openat capability itself (O_DIRECTORY,
    # O_NOFOLLOW, os.open in supports_dir_fd); _rmtree_dirfd above also removes
    # files AND directories relative to the pinned descriptor, so those two extra
    # syscalls are probed on top -- the extras name the descriptor-relative calls
    # this surface actually issues, the way prompts.py adds {os.unlink, os.mkdir}
    # for its own.
    return supports_pinned_walk() and {os.unlink, os.rmdir}.issubset(os.supports_dir_fd)


def resolve_mcp_backend_url(mcp_servers: Any) -> str | None:
    """Derive an app backend's base URL from its ``mcpServers`` declaration.

    This is the single definition of that rule.  Self-managed apps -- ones the
    gateway does not spawn, like the Crew Companion desktop app on :7778 --
    declare no ``backend.entryPoint``, so their backend is discovered from the
    MCP URL instead, with the path stripped.

    TWO callers depend on agreeing exactly, which is why this is one function
    and not two copies: ``handle_app_api_proxy`` resolves the URL to forward to,
    and ``register_builtin_apps`` decides whether to write the ``.app_secret``
    the proxy signs with.  If they ever disagree, an app resolves a backend and
    is then refused a secret, and every proxied request fails with 502 "has no
    secret" -- silently, since nothing checks at registration time.

    Returns None when no usable URL is declared.  Refused, matching the proxy's
    own guards: a non-loopback host (SSRF via a manifest-declared URL), a
    non-literal host (parsed with ``ip_address``, so a DNS name never resolves
    here), and the gateway's own port (self-referential, not a real backend).
    """
    if not isinstance(mcp_servers, dict):
        return None
    gateway_port = int(os.environ.get("KIROCREW_PORT", "5476"))
    for server_cfg in mcp_servers.values():
        if not isinstance(server_cfg, dict):
            continue
        url = server_cfg.get("url", "")
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        # ONE guard around the whole parse. urlparse's accessors are lazy and
        # several raise ValueError on malformed input -- `parsed.port` does it for
        # "…:notaport". An escape from here propagates through
        # _app_declares_backend into register_builtin_apps() and the gateway fails
        # to START, so a single bad manifest would take down registration for every
        # builtin. A manifest is user-supplied data; it must only be skippable.
        try:
            parsed = urlparse(url)
            # Normalize localhost -> 127.0.0.1: aiohttp on macOS may fail on ::1.
            host = parsed.hostname or "127.0.0.1"
            if host == "localhost":
                host = "127.0.0.1"
            if not ipaddress.ip_address(host).is_loopback:
                logger.warning("Refusing non-loopback backend URL %s", url)
                continue
            port_num = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            # Non-IP host, unparsable port, or any other malformed component.
            logger.warning("Refusing unusable backend URL %s: %s", url, exc)
            continue
        if port_num == gateway_port:
            logger.warning("Refusing self-referential backend URL %s", url)
            continue
        return f"{parsed.scheme}://{host}:{port_num}"
    return None


def _builtin_owns_install(existing: InstalledApp) -> bool:
    """Whether an existing app entry was written by ``register_builtin_apps()``.

    False means a USER installed an app under this name, and the builtin must not
    touch it. That distinction cannot be recovered once lost: registration would
    overwrite ``origin`` and set ``lifecycle="locked"``, so afterwards nothing on
    disk shows the install was ever user-owned, and the user cannot uninstall it.

    ``source`` is the discriminator: this function is the only writer of
    ``source="builtin"``, while ``install_app()`` records the install path or
    registry ref. ``origin`` is accepted as a secondary signal so entries written
    by older gateway versions are still recognised as ours.
    """
    return existing.source == "builtin" or existing.origin == "builtin"


def builtin_owns_installed(name: str) -> bool:
    """Whether the ACTIVE installed record for ``name`` is builtin-owned.

    ``True`` only when an ``installed.json`` exists for ``name`` AND it was
    written by :func:`register_builtin_apps` (``source``/``origin`` == builtin,
    per :func:`_builtin_owns_install`). A user-installed app that shadows a
    builtin's name — which makes registration *stand down* and leaves the
    user's record in place — or a missing/unreadable record both return
    ``False`` (fail-closed). Callers use this to confirm a shipped-manifest name
    is actually occupied by first-party code before granting it first-party
    trust; it can only REMOVE trust, never manufacture it.
    """
    existing = _read_installed(name)
    return existing is not None and _builtin_owns_install(existing)


def _app_declares_backend(app_data: dict[str, Any]) -> bool:
    """Whether a manifest declares a backend the gateway proxy can reach.

    Either shape counts: a gateway-spawned ``backend.entryPoint``, or a
    resolvable loopback ``mcpServers`` URL.  Both are proxied, and the proxy
    refuses a request outright when the app has no ``.app_secret``, so both must
    earn one.  An app with neither declares no backend and gets no secret.
    """
    if app_data.get("backend", {}).get("entryPoint"):
        return True
    return resolve_mcp_backend_url(app_data.get("mcpServers")) is not None


@holds_app_record_locks
def register_builtin_apps() -> int:
    """Register built-in dashboard features as app entries.

    Called once at Gateway startup.  Idempotent — updates existing entries
    without removing user customizations.  Returns the number of apps
    registered or updated.

    Each app definition is validated before registration.  Invalid definitions
    are skipped with a warning log — they do not affect other apps.

    The ``defaultEnabled`` field (default: True) controls the initial enabled
    state for newly registered apps.  Existing apps preserve their user-set
    enabled state regardless of the definition's ``defaultEnabled`` value.

    Sources (merged, hardcoded list takes precedence on name collision):
    1. ``_BUILTIN_APPS`` hardcoded list — EMPTY since every builtin moved to a file
       manifest; kept as the ADD-only precedence seam for editions and tests
    2. Auto-discovered from ``builtins/`` directory via ``discovery.py``
    3. Edition-contributed builtins from the active PlatformContext's
       ``AppsLoader.manifest_sources()`` (empty in standalone; the internal
       companion contributes its feature apps).  ADD-only: the hardcoded list
       and the package's own builtins still take precedence on name collision.
    """
    # Merge hardcoded list with auto-discovered builtins + edition-contributed
    # builtins (PlatformContext).  Standalone contributes nothing extra
    # (manifest_sources == []), so ``discovered`` is exactly the package's
    # builtins/ dir — unchanged from today.
    discovered = discover_builtin_apps()
    discovered_names = {a["name"] for a in discovered}
    for app_data in _edition_builtin_apps():
        if app_data["name"] not in discovered_names:
            discovered_names.add(app_data["name"])
            discovered.append(app_data)
    hardcoded_names = {a["name"] for a in _BUILTIN_APPS}

    # Clean up apps that have been escalated to built-in surfaces, merged into
    # an existing surface, or removed from the fork — delete stale installed
    # state so they don't linger in the App Store / nav after the change.
    #   - knowledge: promoted from App Store to registerBuiltinSurface()
    #   - orchestrated: Autopilot merged into the unified Chat surface (mode flag)
    #   - board: removed from the fork (mirrors the upstream project, alongside
    #     the Channels hide); drop stale beta-install dirs so the
    #     orphaned entry doesn't resurface in the App Store Browse grid.
    _escalated = ["knowledge", "orchestrated", "board"]
    for esc_name in _escalated:
        esc_dir = app_dir(esc_name)
        # Never follow a symlinked app dir: iterdir()/rmtree would land on the
        # link target and delete data OUTSIDE the apps tree. Also require the
        # resolved path to stay contained under apps_dir().
        if esc_dir.is_symlink():
            logger.warning("Skipping escalation cleanup for %r: app dir is a symlink", esc_name)
            continue
        if not esc_dir.is_dir():
            continue
        try:
            if not esc_dir.resolve().is_relative_to(apps_dir().resolve()):
                logger.warning(
                    "Skipping escalation cleanup for %r: resolves outside apps dir",
                    esc_name,
                )
                continue
        except OSError as exc:
            logger.warning("Skipping escalation cleanup for %r: %s", esc_name, exc)
            continue
        # Only remove a POSITIVELY identified legacy builtin install: an
        # unrelated local/registry/external app that merely shares the name
        # must never be deleted (it may hold user code and secrets).
        #
        # PIN-FIRST: the app directory descriptor is pinned
        # BEFORE any validation, and installed.json / data/ are inspected
        # RELATIVE to that pinned descriptor. A rename swapping the directory
        # between validation and deletion can therefore never redirect the
        # delete: verdict and deletion refer to the same inode by
        # construction.
        if not _dirfd_ops_supported() or not hasattr(os, "O_DIRECTORY"):
            # No POSIX dir_fd primitives (Windows): validation and deletion
            # cannot be pinned to the same inode, so a rename between them
            # could delete an unvalidated replacement directory. Fail
            # closed — leave legacy-builtin cleanup to the operator here.
            logger.info(
                "Skipping escalation cleanup for %r: platform lacks dir_fd "
                "primitives to pin validation to deletion — remove the "
                "directory manually if no longer needed",
                esc_name,
            )
            continue
        parent_fd = -1
        fd = -1
        try:
            # Anchor at the trusted apps root, then open the app dir RELATIVE
            # to that descriptor with O_NOFOLLOW: containment holds by
            # construction and cannot be raced by renames/symlinks.
            parent_fd = os.open(str(apps_dir()), os.O_RDONLY | os.O_DIRECTORY)
            fd = os.open(
                esc_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_DIRECTORY,
                dir_fd=parent_fd,
            )
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode):
                raise OSError("not a directory")

            # installed.json read through the pinned descriptor: O_NOFOLLOW
            # + fstat-regular on the OPENED fd — a symlinked or mid-race
            # swapped meta file is refused by the kernel atomically.
            meta = None
            meta_fd = -1
            try:
                meta_fd = os.open(
                    INSTALLED_META_FILENAME,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=fd,
                )
                mst = os.fstat(meta_fd)
                if not stat.S_ISREG(mst.st_mode):
                    raise OSError("installed.json is not a regular file")
                with os.fdopen(meta_fd, "r", encoding="utf-8") as fh:
                    meta_fd = -1  # ownership transferred to fdopen
                    meta = json.load(fh)
            except (OSError, ValueError):
                meta = None
            finally:
                if meta_fd >= 0:
                    os.close(meta_fd)
            if not isinstance(meta, dict) or meta.get("origin") != "builtin":
                logger.info(
                    "Keeping app dir %r during escalation cleanup: origin=%r "
                    "is not a legacy builtin",
                    esc_name,
                    meta.get("origin") if isinstance(meta, dict) else None,
                )
                continue

            # Preserve user data/ across the escalation — inspected through
            # the same pinned descriptor. A symlinked data/ or any error we
            # cannot classify fails closed (keep).
            try:
                data_fd = os.open(
                    "data",
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_DIRECTORY,
                    dir_fd=fd,
                )
                try:
                    has_data = bool(os.listdir(data_fd))
                finally:
                    os.close(data_fd)
            except (FileNotFoundError, NotADirectoryError):
                has_data = False
            except OSError as exc:
                # Symlinked data/ (ELOOP) or unreadable — fail closed: keep.
                logger.warning(
                    "Skipping escalation cleanup for %r: cannot inspect data/: %s",
                    esc_name,
                    exc,
                )
                continue
            if has_data:
                # No partial deletion: keep everything and leave removal to
                # the operator.
                logger.info(
                    "Keeping escalated builtin %r: data/ is non-empty — remove "
                    "the directory manually if no longer needed",
                    esc_name,
                )
                continue

            _rmtree_dirfd(fd)
            os.close(fd)
            fd = -1
            # Unlink the NAME only if the entry still refers to the pinned
            # inode: a directory swapped in after the pin is left untouched
            # (rmdir would also refuse a non-empty swap, but check anyway).
            try:
                st2 = os.stat(esc_name, dir_fd=parent_fd, follow_symlinks=False)
                if (st2.st_ino, st2.st_dev) == (st.st_ino, st.st_dev):
                    os.rmdir(esc_name, dir_fd=parent_fd)
                    logger.info(
                        "Removed escalated app %r (now a built-in surface)",
                        esc_name,
                    )
                else:
                    logger.warning(
                        "Escalation cleanup for %r: directory entry changed "
                        "after pin — leaving the new entry in place",
                        esc_name,
                    )
            except FileNotFoundError:
                pass
        except OSError as exc:
            logger.warning("Escalation cleanup failed for %r (kept): %s", esc_name, exc)
        finally:
            if fd >= 0:
                os.close(fd)
            if parent_fd >= 0:
                os.close(parent_fd)
    # Discovered apps that aren't already in the hardcoded list
    extra = [a for a in discovered if a["name"] not in hardcoded_names]
    all_builtins = list(_BUILTIN_APPS) + extra

    count = 0
    for app_data in all_builtins:
        # Validate definition — skip invalid entries without affecting others
        errors = _validate_builtin_app(app_data)
        if errors:
            logger.warning(
                "Skipping invalid builtin app definition %r: %s",
                app_data.get("name", "<unnamed>"),
                "; ".join(errors),
            )
            continue

        name = app_data["name"]

        # Lenient migratedTo handling: warn but don't block registration
        migrated_to_raw = app_data.get("migratedTo", "")
        migrated_to_effective = _effective_migrated_to(app_data)
        if migrated_to_raw and not migrated_to_effective:
            logger.warning(
                "Builtin app %r has invalid migratedTo format %r — field ignored",
                name,
                migrated_to_raw,
            )
        elif migrated_to_effective:
            target_name = migrated_to_effective.split(":", 1)[1]
            if target_name != name:
                logger.warning(
                    "Builtin app %r migratedTo target %r differs from app name "
                    "— this may break data directory sharing",
                    name,
                    migrated_to_effective,
                )

        hold_app_record_lock(name)
        existing = _read_installed(name)

        dest = app_dir(name)
        ensure_directory(dest)  # apps/<name>/ is born 0700 in the data home

        # A pre-existing entry this function did not write belongs to the USER:
        # they installed an app that happens to share this builtin's name. Taking
        # it over is unrecoverable -- see _builtin_owns_install() -- so stand down
        # entirely and leave their install exactly as it is.
        if existing and not _builtin_owns_install(existing):
            logger.warning(
                "Not registering builtin %r: a user-installed app already occupies "
                "%s (source=%r, origin=%r). Leaving its manifest and metadata "
                "untouched; the builtin is not registered on this host.",
                name,
                app_dir(name),
                existing.source,
                existing.origin,
            )
            continue

        if existing:
            # Only update version + displayName, preserve user state
            existing.version = app_data["version"]
            existing.displayName = app_data["displayName"]
            existing.updatedAt = _now_iso()
            has_ui_bundle = bool(app_data.get("ui", {}).get("entry"))
            existing.origin = "local" if has_ui_bundle else "builtin"
            existing.resources = "gateway"
            existing.lifecycle = "locked"
            # Sync migratedTo from definition (overwrite stale values)
            existing.migratedTo = _effective_migrated_to(app_data)
            _write_installed(name, existing)
        else:
            # Use defaultEnabled from definition (defaults to True for backward compat)
            default_enabled = app_data.get("defaultEnabled", True)
            # Governance chokepoint. enable_app() normally enforces the ``apps``
            # activation allowlist, but a *default-enabled* builtin is persisted
            # here on first registration and never routes through enable_app() —
            # which would let it bypass a host deny-by-default policy. Re-apply the
            # same gate so a governance-denied app registers DISABLED. This is a
            # no-op for default-disabled builtins (the historical case).
            if default_enabled and _app_activation_denied(name):
                default_enabled = False
            meta = InstalledApp(
                name=name,
                version=app_data["version"],
                displayName=app_data["displayName"],
                enabled=default_enabled,
                installedAt=_now_iso(),
                source="builtin",
                origin="builtin",
                resources="gateway",
                lifecycle="locked",
                migratedTo=_effective_migrated_to(app_data),
                # A first registration under the promoted default IS the promotion
                # being received, so nothing is owed and the backfill must never
                # touch this record. Without this the sequence "install, disable
                # the app in that same session, restart" would re-enable it: the
                # backfill would find a disabled record it had never flagged and
                # read the user's own choice as a promotion still owed.
                #
                # Gated on the POST-governance ``default_enabled``, matching the
                # rule the backfill itself applies: a governance-denied app
                # registers DISABLED, so it did NOT receive the promotion and is
                # still owed one. Flagging it here would strand it -- relaxing the
                # policy later could never deliver the launcher, because the
                # record would claim it already had.
                defaultOnBackfilled=default_enabled and name in _DEFAULT_ON_BACKFILL,
            )
            _write_installed(name, meta)

        # Persist manifest so dashboard can show full info
        atomic_write(
            dest / APP_MANIFEST_FILENAME,
            json.dumps(app_data, indent=2) + "\n",
        )

        # Built-in apps with a backend need an app secret so the gateway
        # proxy can authenticate requests to them.  Generate once; preserve
        # existing secret across restarts to keep live backends valid.  A
        # backend is either a gateway-spawned entryPoint OR a resolvable
        # loopback mcpServers URL (self-managed apps) — both go through the
        # proxy, which 502s without a secret, so both must get one.
        if _app_declares_backend(app_data):
            secret_path = dest / ".app_secret"
            if not secret_path.is_file():
                # circular import: token_auth → app_secret_store → manager
                # token_auth imports app_secret_store, which transitively
                # imports the manager module's app-directory helpers.
                # Importing at module scope here would create a cycle, so
                # we defer to the function body.
                from kiro_crew.dashboard.token_auth import generate_app_secret, write_app_secret

                write_app_secret(name, generate_app_secret())
            # Invalidate the proxy secret cache so the newly-written (or
            # pre-existing) secret is picked up on the next request.
            try:
                # circular import: routes → manager
                # kiro_crew.apps.routes imports from kiro_crew.apps.manager
                # at module load, so we cannot import routes at the top of
                # this file without creating a cycle.
                from kiro_crew.apps.routes import invalidate_app_secret_cache

                invalidate_app_secret_cache(name)
            except Exception:
                pass  # routes module may not be importable during bootstrap

        count += 1

    if count:
        logger.info("Registered %d built-in app(s)", count)

    # Publish declarations and warm the orphan cache after registration.
    refresh_app_config_keys()
    detect_orphaned_builtins(force_refresh=True)

    return count


# ---------------------------------------------------------------------------
# Orphan detection
# ---------------------------------------------------------------------------

_orphaned_builtins_cache: set[str] | None = None


def shipped_builtin_names() -> set[str]:
    """The three sources ``register_builtin_apps`` registers from, screened by the
    same ``_validate_builtin_app`` it skips on. Wider sets belong to their callers.
    """
    candidates = list(_BUILTIN_APPS) + discover_builtin_apps() + _edition_builtin_apps()
    return {app["name"] for app in candidates if not _validate_builtin_app(app)}


def detect_orphaned_builtins(*, force_refresh: bool = False) -> set[str]:
    """Return set of orphaned builtin app names.

    Scans apps_dir for builtin apps not in _BUILTIN_APPS list or
    auto-discovered from the builtins/ directory.
    Result is cached after first call; pass force_refresh=True to re-scan
    (called on mc:apps-changed events).
    """
    global _orphaned_builtins_cache
    if _orphaned_builtins_cache is not None and not force_refresh:
        return _orphaned_builtins_cache

    # Combine hardcoded list + auto-discovered names + edition-contributed
    # builtins (PlatformContext).  Standalone adds nothing (manifest_sources ==
    # [] and bundled_app_names() == OSS builtins already covered); the internal
    # companion's feature apps are recognized as builtins here so they are not
    # mis-flagged as orphans after registration.  ``bundled_app_names()`` is
    # also honored as a declaration so a declared app whose manifest dir is
    # momentarily missing is not mis-orphaned.
    builtin_names = {app["name"] for app in _BUILTIN_APPS}
    builtin_names.update(app["name"] for app in discover_builtin_apps())
    builtin_names.update(app["name"] for app in _edition_builtin_apps())
    builtin_names.update(_edition_bundled_app_names())

    orphaned: set[str] = set()
    root = apps_dir()
    if not root.is_dir():
        _orphaned_builtins_cache = orphaned
        return orphaned
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        meta = _read_installed(entry.name)
        if meta and meta.origin == "builtin" and entry.name not in builtin_names:
            orphaned.add(entry.name)
    _orphaned_builtins_cache = orphaned
    return orphaned


def invalidate_orphan_cache() -> None:
    """Called when apps change (install/uninstall/cleanup)."""
    global _orphaned_builtins_cache
    _orphaned_builtins_cache = None


# ---------------------------------------------------------------------------
# Migration cleanup
# ---------------------------------------------------------------------------


def migrated_builtin_cleanup_applies(name: str) -> bool:
    """Whether a safe builtin-owned record qualifies for migration teardown and removal.

    An app directory that is a symlink or junction is ineligible, and so is anything
    at ``data`` that is not a real directory (:func:`gateway_data_dir_obstruction`):
    the teardown keeps only a directory there, so it would delete anything else.
    """
    from kiro_crew.apps.builtins import _MIGRATED_BUILTINS

    if not _check_path_safety(name):
        return False
    path = app_dir(name)
    if path.is_symlink() or is_link_or_junction(path):
        return False
    if gateway_data_dir_obstruction(path):
        return False
    meta = _read_installed(name)
    return bool(
        meta
        and meta.origin == "builtin"
        and (name in _MIGRATED_BUILTINS or name in detect_orphaned_builtins(force_refresh=True))
    )


def cleanup_migrated_builtin(name: str) -> AppResult:
    """Remove orphaned builtin metadata after its functionality was folded into core.

    Matches by app NAME (not migratedTo metadata) — existing installs from before
    the migration mechanism won't have migratedTo set. The presence of `name` in
    _MIGRATED_BUILTINS is the authoritative signal.

    Preserves data/ directory. Removes installed.json and app.json only.
    Idempotent: returns ok=True if already cleaned up.
    """
    from kiro_crew.apps.builtins import _MIGRATED_BUILTINS

    if name not in _MIGRATED_BUILTINS:
        return AppResult(ok=False, name=name, error="not a migrated builtin")

    if not _check_path_safety(name):
        return AppResult(ok=False, name=name, error=f"unsafe app name: {name!r}")

    meta = _read_installed(name)
    if not meta:
        # Already cleaned up or was never installed — success (idempotent).
        logger.debug("cleanup_migrated_builtin: %s not installed (already clean)", name)
        return AppResult(ok=True, name=name, message="not installed — nothing to clean up")

    # If the install has origin != builtin, a standalone replacement already took
    # over — nothing to clean up.
    if meta.origin != "builtin":
        return AppResult(
            ok=True,
            name=name,
            message="already migrated — standalone version is in place",
        )

    # Perform cleanup — remove metadata files, preserve data/
    dest = app_dir(name)
    installed_path = dest / INSTALLED_META_FILENAME
    manifest_path = dest / APP_MANIFEST_FILENAME

    try:
        if manifest_path.is_file():
            manifest_path.unlink()
        if installed_path.is_file():
            installed_path.unlink()
    except OSError as exc:
        logger.error("cleanup_migrated_builtin: failed to clean up %s: %s", name, exc)
        return AppResult(
            ok=False,
            name=name,
            error=f"failed to clean up app metadata: {exc}",
            error_code="io_error",
        )

    # Refresh declarations and orphan state after removing installed metadata.
    refresh_app_config_keys()
    invalidate_orphan_cache()

    logger.info("Cleaned up migrated builtin %s (data preserved)", name)
    return AppResult(
        ok=True,
        name=name,
        message="cleaned up migrated builtin entry, data preserved",
    )
