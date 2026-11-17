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
  app registration, a built-in app's own credential mints (its host grants'
  ``relay_credential_paths``) and the webhook bearers (each
  hands back a credential that outlives the relay session), the
  local-secret CLI routes and the secrets vault, whose values would otherwise cross
  the relay in plaintext, and the relay viewer's routes under ``/api/relay-viewer``;
* shutdown;
* a path not in canonical form, which a later hop could read as another path.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from kiro_crew.apps import builtin_host_grants
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
        # Mints a webhook bearer and signing secret that invoke an agent on their
        # own, after the relay session ends.
        "/api/webhooks/tokens",
    }
    # A shipped built-in app's own credential mints, declared in its host grants
    # (``relay_credential_paths``): exact paths under that app's own namespace.
    | builtin_host_grants.relay_credential_paths()
)
#: Under these, every route manages a stored credential: the vault's entries and
#: each webhook bearer.
_LOCAL_CREDENTIAL_PREFIXES = ("/api/secrets/", "/api/webhooks/tokens/")
#: The relay viewer: it calls the control plane with this machine's Kiro sign-in
#: and mints relay access tokens, so a relayed caller reaches none of it.
_RELAY_VIEWER = "/api/relay-viewer"
#: The app secret exchange (``POST /api/apps/<name>/token``), any method.
_APP_TOKEN_EXCHANGE_RE = re.compile(r"\A/api/apps/[^/]+/token/?\Z")

_INSTANCES = "/api/instances"
_SHUTDOWN = "/api/shutdown"
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
    Every limit applies to the owner and teammates alike: what a teammate may do
    inside the Crew is the owner gates' question (``shared_crew.teammate_access``),
    not the transport's, so *caller* decides no limit here.

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
    return remote_route_limit(path)


def remote_route_limit(path: str) -> RemoteLimit | None:
    """The limit a canonical *path* alone decides: instances, local credentials, shutdown.

    The part of :func:`remote_limit_for` that needs no header and no strict-path set,
    so ``shared_crew.teammate_access`` holds every Shared Crew teammate, on any path,
    to the same route boundary the relay holds every remote caller to.
    """
    if _is_noncanonical(path):
        return NONCANONICAL_PATH
    if _at_or_under(path, _INSTANCES):
        return INSTANCES_OFF
    if is_local_credential_path(path):
        return LOCAL_CREDENTIAL
    if _at_or_under(path, _SHUTDOWN):
        return SHUTDOWN
    return None


def is_local_credential_path(path: str) -> bool:
    """Whether *path* mints, rotates, discloses or stores a credential that outlives
    the session asking: Crew's local tokens and secrets, the vault, an app secret,
    a webhook bearer, a built-in app's own credential mints and the relay viewer.

    The relay refuses these to every relayed caller; ``shared_crew.teammate_access``
    keeps them the machine owner's on every path, since a credential a teammate took
    away would survive their access being revoked.
    """
    if (path.rstrip("/") or "/") in _LOCAL_CREDENTIAL_EXACT:
        return True
    if path.startswith(_LOCAL_CREDENTIAL_PREFIXES) or _APP_TOKEN_EXCHANGE_RE.match(path):
        return True
    return _at_or_under(path, _RELAY_VIEWER)


def _is_noncanonical(path: str) -> bool:
    """Whether *path* could read as another path at a later hop."""
    return _NONCANONICAL_RE.search(path) is not None
