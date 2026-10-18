"""The app-name contract: what an app may call itself, and its version grammar.

``app_name_error`` is the single rule every door that admits an app applies -- manifest
validation (install, update, discovery), external self-registration and builtin
registration -- so a name refused at one door cannot be admitted at another. The
reserved sets it reads, the kebab-case grammar other manifest identifiers share, and the
semver check ``AppManifest.validate`` applies to ``version`` live beside it.

Served through :mod:`kiro_crew.apps.manifest`, which callers and tests import and patch;
this module never imports it.
"""

from __future__ import annotations

import re

from kiro_crew.constants import WINDOWS_DEVICE_STEMS

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
# Rejected on all platforms, not only Windows. Resource paths (``manifest_containment``) are
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
