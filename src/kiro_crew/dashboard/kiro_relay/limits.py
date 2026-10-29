"""The remote limits: routes a relay-delivered request may never reach.

A relayed request is rewritten so it never reads as local (see
``caller.RELAY_REMOTE``), which keeps every loopback-trusting gate closed to it. This
table is the explicit second layer, with one typed refusal per limit, so the person
learns which limit stopped them and a reviewer can read the whole boundary in one
place:

* local-only routes: the strict internal API paths only this machine's own
  processes call, and any request that presents a local credential header;
* ``/api/instances`` and everything under it, for every remote person;
* Crew's local token and secret routes: the local owner-token bootstrap, the
  sign-in link mints, the refresh rotation, the app secret exchange, external
  app registration, the Dev Fleet pod-token mint and the webhook bearers (each
  hands back a credential that outlives the relay session), the
  local-secret CLI routes and the secrets vault, whose values would otherwise cross
  the relay in plaintext;
* shutdown;
* the app surface for a teammate, until an app opts in to remote teammates: an
  app's UI and backend proxy (``/apps/*``), every route an app or the app
  platform registers in-process (``/api/apps`` and under it), and the two
  app-platform routes outside it;
* a path not in canonical form, which a later hop could read as another path.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from kiro_crew.dashboard.kiro_relay.caller import RelayCaller


@dataclass(frozen=True)
class RemoteLimit:
    """One limit: the machine-readable code and the advisory sentence."""

    code: str
    message: str


LOCAL_ONLY = RemoteLimit(
    "relay_local_only",
    "This route is only for processes on the Crew's own computer.",
)
INSTANCES_OFF = RemoteLimit(
    # The code the instances gate answers when the feature is off, which the
    # dashboard already reads as "no instances here" rather than as a failure it
    # reports: for a remote person that is exactly what this limit means.
    "instances_disabled",
    "Instances can't be managed through the relay.",
)
LOCAL_CREDENTIAL = RemoteLimit(
    "relay_local_credential",
    "Crew's local tokens and secrets aren't available through the relay.",
)
SHUTDOWN = RemoteLimit(
    "relay_shutdown_refused",
    "The Crew can't be shut down through the relay.",
)
APPS_OFF_FOR_TEAMMATES = RemoteLimit(
    "relay_apps_teammate",
    "Apps aren't available to teammates through the relay.",
)
NONCANONICAL_PATH = RemoteLimit(
    "relay_path_noncanonical",
    "This address isn't in a form the relay accepts.",
)

#: Every limit, for the spec table and the test that exercises each one.
ALL_LIMITS = (
    LOCAL_ONLY,
    INSTANCES_OFF,
    LOCAL_CREDENTIAL,
    SHUTDOWN,
    APPS_OFF_FOR_TEAMMATES,
    NONCANONICAL_PATH,
)

#: Headers that authenticate a process on this machine. No relayed client has a
#: reason to send one, and a relayed request presenting one is refused outright
#: rather than letting any branch weigh it.
LOCAL_CREDENTIAL_HEADERS = ("X-Internal-Secret", "X-Local-Secret", "X-App-Secret")

_LOCAL_CREDENTIAL_EXACT = frozenset(
    {
        # Mints an owner token for a process on this machine.
        "/api/token/local",
        # Mints a sign-in link that outlives the relay's 5-minute access token.
        "/api/auth/mobile-link",
        "/api/tailnet/mobile/qr",
        # Rotates a Crew refresh cookie into fresh Crew credentials. A relayed
        # caller is authenticated by the relay and never holds one.
        "/api/auth/refresh",
        # Authenticated by the local secret (X-Local-Secret) for the CLI.
        "/api/logout",
        "/api/update/revalidate",
        # The secrets vault.
        "/api/secrets",
        # External app registration: writes an app secret and returns it.
        "/api/apps/register",
        # Mint and return a Dev Fleet pod's dashboard token, through the
        # gateway's own route and through the app's backend.
        "/api/apps/dev-fleet/pod-token",
        "/apps/dev-fleet/api/pod/token",
        # Mints a webhook bearer and signing secret that invoke an agent on their
        # own, after the relay session ends.
        "/api/webhooks/tokens",
    }
)
#: Under these, every route manages a stored credential: the vault's entries and
#: each webhook bearer.
_LOCAL_CREDENTIAL_PREFIXES = ("/api/secrets/", "/api/webhooks/tokens/")
#: The app secret exchange (``POST /api/apps/<name>/token``), any method.
_APP_TOKEN_EXCHANGE_RE = re.compile(r"\A/api/apps/[^/]+/token/?\Z")

_INSTANCES = "/api/instances"
_SHUTDOWN = "/api/shutdown"
#: An app's UI and the reverse proxy to its backend.
_APPS_PREFIX = "/apps/"
#: Every route an app or the app platform registers in-process: the app
#: management routes and each app's own backend routes under ``/api/apps/<name>/``.
_APPS_API = "/api/apps"
#: App-platform routes outside those namespaces: the publish-provider list and the
#: app store's catalog refresh.
_APPS_PLATFORM_EXACT = frozenset({"/api/publish-providers", "/api/app-store/refresh"})
#: A ``%`` left after decoding, an empty segment, or a ``.`` / ``..`` segment. One
#: trailing slash is canonical.
_NONCANONICAL_RE = re.compile(r"%|//|/\.\.?(?:/|\Z)")


def _at_or_under(path: str, root: str) -> bool:
    """Whether *path* is *root* or a child of it, on a segment boundary."""
    trimmed = path.rstrip("/") or "/"
    return trimmed == root or path.startswith(root + "/")


def remote_limit_for(
    path: str,
    headers: Mapping[str, str],
    caller: RelayCaller,
    *,
    strict_internal_paths: Iterable[str],
) -> RemoteLimit | None:
    """The limit that refuses this relayed request, or ``None`` when none applies.

    *path* is the decoded request path, the same form the router and token auth
    match on. *strict_internal_paths* is the gateway's own strict internal set,
    the one its token auth is built with, matched the same way (exact or child).
    Every limit applies to the owner and teammates alike except the app limit,
    which is a teammate's only.

    A path that is not in canonical form is refused before any route is matched:
    one with a ``%`` left after decoding, an empty segment or a dot segment. A
    downstream hop could read it as another path than this table does (the app
    proxy re-encodes what it captured, so ``%2574`` reaches an app's backend as
    ``t``), and no relayed client has a reason to send one.
    """
    if any(name in headers for name in LOCAL_CREDENTIAL_HEADERS):
        return LOCAL_ONLY
    if _is_noncanonical(path):
        return NONCANONICAL_PATH
    if any(_at_or_under(path, entry) for entry in strict_internal_paths):
        return LOCAL_ONLY
    if _at_or_under(path, _INSTANCES):
        return INSTANCES_OFF
    if (path.rstrip("/") or "/") in _LOCAL_CREDENTIAL_EXACT:
        return LOCAL_CREDENTIAL
    if path.startswith(_LOCAL_CREDENTIAL_PREFIXES) or _APP_TOKEN_EXCHANGE_RE.match(path):
        return LOCAL_CREDENTIAL
    if _at_or_under(path, _SHUTDOWN):
        return SHUTDOWN
    if not caller.is_owner and _is_app_surface(path):
        return APPS_OFF_FOR_TEAMMATES
    return None


def _is_noncanonical(path: str) -> bool:
    """Whether *path* could read as another path at a later hop."""
    return _NONCANONICAL_RE.search(path) is not None


def _is_app_surface(path: str) -> bool:
    """Whether *path* is an app's UI, an app's API, or an app-platform route."""
    return (
        path.startswith(_APPS_PREFIX)
        or _at_or_under(path, _APPS_API)
        or (path.rstrip("/") or "/") in _APPS_PLATFORM_EXACT
    )
