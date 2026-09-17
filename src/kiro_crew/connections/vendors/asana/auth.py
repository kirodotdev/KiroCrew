"""Asana authorization surfaces: MCP vs native REST, modeled as two concepts.

WHAT THIS OWNS
==============
The pure-logic model of Asana's TWO distinct, non-interchangeable authorization
surfaces, and the vendor rules that keep them separate. Network-free: it
validates an authorization intent against the documented rules of whichever
surface it targets and refuses a request that conflates the two. It issues no
token and makes no call.

This is vendor-fact modeling, not the account-link runtime: the shipped OAuth
minting/binding lives in ``kiro_crew.connections`` and the shared adapter seam
is W01's. This module neither imports nor depends on those; it only encodes the
Asana-specific truths a later wiring layer must respect.

THE TWO SURFACES, NEVER MERGED
==============================
:class:`AuthSurface` is a closed two-value enum, and the whole module exists to
keep the two from collapsing into one credential concept:

* ``MCP`` -- the Streamable HTTP MCP server at
  :data:`MCP_SERVER_URL` (``https://mcp.asana.com/v2/mcp``). The retired v1
  ``/sse`` transport is NEVER modeled (see :data:`RETIRED_MCP_SSE_URL`, present
  only to be explicitly refused). An MCP app:

  - has NO OAuth scopes: the only accepted scope value is ``"default"``; any
    other value is an "Invalid scope" error, refused here at
    :func:`normalize_mcp_scopes`.
  - binds to a SINGLE workspace at consent time; cross-workspace work needs a
    separate session.
  - does NOT support dynamic client registration (DCR): the app must be
    pre-registered to obtain a client id/secret. This is a fixed vendor fact.
  - its token does NOT work against the REST API.

* ``NATIVE_REST`` -- a standard Asana API OAuth app using FINE-GRAINED scopes
  (``tasks:read``, ``projects:write``, ...). A separate app from the MCP one;
  its token does NOT work against the MCP server.

THE TOKEN-INTERCHANGE REFUSAL
=============================
:func:`assert_token_usable_on` is the load-bearing guard: a token minted for
one surface used against the other is refused with :class:`AuthSurfaceMismatch`.
The two token kinds are never treated as one, exactly as Asana documents ("MCP
tokens do not work with the Asana REST API ... create a separate app").

WHAT THIS DELIBERATELY DOES NOT OWN
===================================
No token issuance, no refresh, no live call. A misuse (an MCP request carrying
a real scope, a REST request with no scopes, a token used on the wrong surface)
raises :class:`AsanaAuthError` / :class:`AuthSurfaceMismatch`, plain
``ValueError`` subclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Sequence

#: The current, authoritative MCP transport: v2 Streamable HTTP.
MCP_SERVER_URL = "https://mcp.asana.com/v2/mcp"
#: The retired v1 SSE transport. Present ONLY so it can be explicitly refused --
#: it must never be connected to. Its shutdown date is contradictory across
#: Asana's own docs (5 Aug 2026 on one page, 05/11/2026 on others), which is
#: exactly why this connector treats it as retired rather than date-gating it.
RETIRED_MCP_SSE_URL = "https://mcp.asana.com/sse"
#: The one scope value the MCP surface accepts. Anything else is "Invalid scope".
MCP_ONLY_SCOPE = "default"


class AsanaAuthError(ValueError):
    """An authorization intent violated a documented Asana surface rule.

    A shaping fault (an MCP request with a real scope, a REST request lacking
    scopes, a request against the retired SSE endpoint). Distinct from
    :class:`AuthSurfaceMismatch`, which is the specific cross-surface token
    misuse.
    """


class AuthSurfaceMismatch(AsanaAuthError):
    """A token minted for one surface was used against the other.

    MCP tokens do not work against REST and vice versa; this is the typed
    refusal of that misuse -- never a silent acceptance.
    """


class AuthSurface(str, Enum):
    """Asana's two non-interchangeable authorization surfaces.

    Deliberately a closed two-value set. There is no "either" or "any" value:
    every credential and every request belongs to exactly one surface, which is
    the invariant this module protects.
    """

    MCP = "mcp"
    NATIVE_REST = "native_rest"


def normalize_mcp_scopes(scopes: Optional[Sequence[str]]) -> tuple[str, ...]:
    """Validate the scope list for an MCP authorization, returning the canonical form.

    The MCP surface has no scopes: the only accepted value is
    :data:`MCP_ONLY_SCOPE` (``"default"``). ``None`` or an empty list is
    normalized to ``("default",)``. Any OTHER scope value (a fine-grained REST
    scope like ``tasks:read``) is refused with :class:`AsanaAuthError` -- Asana
    returns "Invalid scope" for it, and this connector will not send it.
    """

    if not scopes:
        return (MCP_ONLY_SCOPE,)
    invalid = [s for s in scopes if s != MCP_ONLY_SCOPE]
    if invalid:
        raise AsanaAuthError(
            f"the MCP surface accepts only the scope {MCP_ONLY_SCOPE!r}; refused: {invalid!r} "
            "(passing a real scope to an MCP app is an 'Invalid scope' error)"
        )
    return (MCP_ONLY_SCOPE,)


def normalize_rest_scopes(scopes: Optional[Sequence[str]]) -> tuple[str, ...]:
    """Validate the fine-grained scope list for a native REST authorization.

    The REST surface REQUIRES at least one scope -- a REST authorization with no
    scope grants nothing, so an empty/None scope list is refused, and every
    member must be a non-empty string. The ``"default"`` MCP pseudo-scope is
    refused here too: it is meaningless on the REST surface, and accepting it
    would be one more way the two surfaces bleed together.

    The connector does NOT enforce a scope *grammar* (e.g. a ``resource:action``
    colon shape): Asana's REST surface issues both fine-grained
    ``resource:action`` scopes AND colon-less OpenID Connect scopes
    (``openid``, ``email``, ``profile``), and no cited vendor source makes
    ``resource:action`` the only legal shape. Consistent with this PR's rule
    that an uncited unknown is preserved as an unknown, a validly-shaped-unknown
    scope string is passed through rather than refused as "malformed".
    """

    if not scopes:
        raise AsanaAuthError("the native REST surface requires at least one fine-grained scope")
    # Strip and validate every member FIRST, then test membership against the
    # normalized values. Checking ``MCP_ONLY_SCOPE in scopes`` on the raw list
    # would let a whitespace-padded pseudo-scope like ``" default "`` slip past
    # the refusal and then be normalized back to ``"default"`` on return -- the
    # exact value this rule exists to reject.
    cleaned = tuple(s.strip() for s in scopes if isinstance(s, str) and s.strip())
    if not cleaned or len(cleaned) != len(tuple(scopes)):
        raise AsanaAuthError("REST scopes must be non-empty strings")
    if MCP_ONLY_SCOPE in cleaned:
        raise AsanaAuthError(
            f"{MCP_ONLY_SCOPE!r} is the MCP surface's pseudo-scope and is not a REST scope; "
            "REST uses OAuth scopes minted for the REST app"
        )
    return cleaned


def assert_mcp_server_url(url: str) -> str:
    """Return ``url`` if it is the current v2 MCP server, else refuse.

    The retired v1 ``/sse`` transport is refused outright (see
    :data:`RETIRED_MCP_SSE_URL`): it must never be connected to, regardless of
    the contradictory shutdown dates in Asana's own docs. Any URL other than the
    v2 server is refused too -- the MCP surface has exactly one endpoint.
    """

    if url == RETIRED_MCP_SSE_URL:
        raise AsanaAuthError(
            "the v1 SSE MCP transport is retired and must not be connected to; "
            f"use the v2 Streamable HTTP server {MCP_SERVER_URL}"
        )
    if url != MCP_SERVER_URL:
        raise AsanaAuthError(
            f"unexpected MCP server URL {url!r}; the v2 endpoint is {MCP_SERVER_URL}"
        )
    return url


@dataclass(frozen=True)
class SurfaceToken:
    """A token bound to exactly ONE authorization surface.

    ``surface`` names which surface issued it; ``workspace`` is the single
    workspace an MCP token is bound to at consent time (``None`` for a REST
    token, which is not workspace-pinned). The binding is carried so a caller
    cannot use an MCP token outside its consented workspace, nor treat a REST
    token as workspace-scoped when it is not.

    ``scopes`` is normalized and validated per surface at construction: an MCP
    token's scopes collapse to the ``default`` pseudo-scope (any real scope is
    refused), and a REST token MUST carry at least one non-empty scope (a
    scope-less REST token is refused). No scope *grammar* is enforced -- Asana's
    REST surface issues both ``resource:action`` and colon-less OIDC scopes.
    """

    surface: AuthSurface
    workspace: Optional[str] = None
    scopes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.surface is AuthSurface.MCP:
            if not (self.workspace and self.workspace.strip()):
                raise AsanaAuthError(
                    "an MCP token is bound to a single workspace at consent time; "
                    "workspace is required"
                )
            # The MCP surface has no OAuth scopes: only the "default" pseudo-scope
            # is accepted, and an empty scope set normalizes to it. A real scope
            # here is an "Invalid scope" fault, refused rather than accepted.
            object.__setattr__(self, "scopes", normalize_mcp_scopes(self.scopes))
        elif self.surface is AuthSurface.NATIVE_REST:
            if self.workspace is not None:
                # A REST token is not workspace-pinned; recording a workspace on
                # it would misrepresent the surface's own scoping model.
                raise AsanaAuthError(
                    "a native REST token is not workspace-scoped; leave workspace unset"
                )
            # The native REST surface REQUIRES at least one scope;
            # a scope-less REST token is a shaping fault, not a silent pass.
            object.__setattr__(self, "scopes", normalize_rest_scopes(self.scopes))
        else:
            # Fail closed: never let an unknown surface value fall through to the
            # REST (or any) path. AuthSurface is a closed set; anything outside it
            # is an invalid authorization that must be refused, not defaulted.
            raise AsanaAuthError(
                f"unknown authorization surface {self.surface!r}; "
                "must be one of the AuthSurface members"
            )


def assert_token_usable_on(token: SurfaceToken, target: AuthSurface) -> None:
    """Refuse a token used against the surface it was not minted for.

    MCP tokens do not work against REST and REST tokens do not work against MCP.
    This is the enforcement of "the two tokens are not interchangeable": a
    mismatch raises :class:`AuthSurfaceMismatch`, never a silent pass. It says
    nothing about whether the token is otherwise valid (unexpired, un-revoked)
    -- that is a runtime concern this pure layer does not touch.
    """

    if token.surface is not target:
        raise AuthSurfaceMismatch(
            f"a {token.surface.value} token cannot be used on the {target.value} surface; "
            "Asana issues separate, non-interchangeable tokens per surface"
        )
