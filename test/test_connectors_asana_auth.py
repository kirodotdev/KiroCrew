"""Tests for Asana's two authorization surfaces, kept separate.

Covers: MCP accepts only the 'default' scope (negative: a real scope);
REST requires at least one scope (negative: none, or the MCP pseudo-scope) but
enforces no scope grammar (colon-less OIDC scopes pass through);
the retired SSE endpoint is refused; MCP tokens are workspace-bound and REST
tokens are not; and a token used on the wrong surface is refused.
"""

from __future__ import annotations

import pytest

from kiro_crew.connections.vendors.asana.auth import (
    MCP_ONLY_SCOPE,
    MCP_SERVER_URL,
    RETIRED_MCP_SSE_URL,
    AsanaAuthError,
    AuthSurface,
    AuthSurfaceMismatch,
    SurfaceToken,
    assert_mcp_server_url,
    assert_token_usable_on,
    normalize_mcp_scopes,
    normalize_rest_scopes,
)

# ── MCP scopes: only 'default' ──────────────────────────────────────────────


def test_mcp_scopes_none_defaults():
    assert normalize_mcp_scopes(None) == (MCP_ONLY_SCOPE,)


def test_mcp_scopes_default_accepted():
    assert normalize_mcp_scopes(["default"]) == ("default",)


def test_mcp_scopes_real_scope_rejected():
    # Passing a fine-grained scope to an MCP app is an "Invalid scope" error.
    with pytest.raises(AsanaAuthError):
        normalize_mcp_scopes(["tasks:read"])


# ── REST scopes: fine-grained, required ─────────────────────────────────────


def test_rest_scopes_fine_grained_accepted():
    assert normalize_rest_scopes(["tasks:read", "projects:write"]) == (
        "tasks:read",
        "projects:write",
    )


def test_rest_scopes_empty_rejected():
    with pytest.raises(AsanaAuthError):
        normalize_rest_scopes([])


def test_rest_scopes_reject_mcp_pseudo_scope():
    # 'default' is the MCP surface's pseudo-scope, meaningless (and conflating)
    # on REST.
    with pytest.raises(AsanaAuthError):
        normalize_rest_scopes(["default"])


def test_rest_scopes_reject_whitespace_padded_pseudo_scope():
    # A whitespace-padded pseudo-scope must NOT slip past the refusal: the
    # membership test runs on the stripped values, so " default ", "\tdefault",
    # "default\n" and mixed cases all normalize to the forbidden "default" and
    # are refused rather than stored on a NATIVE_REST token.
    for padded in [" default ", "\tdefault", "default\n", "  default", "default\t"]:
        with pytest.raises(AsanaAuthError):
            normalize_rest_scopes([padded])
    # Also refused when mixed in with a legitimate scope.
    with pytest.raises(AsanaAuthError):
        normalize_rest_scopes(["tasks:read", " default "])


def test_rest_scopes_colonless_oidc_accepted():
    # Asana's REST/OAuth surface also issues colon-less OpenID Connect scopes
    # (openid/email/profile). No cited source makes 'resource:action' the only
    # legal grammar, so a validly-shaped-unknown scope is passed through, not
    # refused as "malformed" -- consistent with the PR's preserve-the-unknown rule.
    assert normalize_rest_scopes(["openid", "email", "profile"]) == (
        "openid",
        "email",
        "profile",
    )
    assert normalize_rest_scopes(["tasks:read", "openid"]) == ("tasks:read", "openid")


def test_rest_scopes_reject_non_string_member():
    with pytest.raises(AsanaAuthError):
        normalize_rest_scopes(["tasks:read", 123])  # type: ignore[list-item]


# ── MCP endpoint: v2 only, SSE retired ──────────────────────────────────────


def test_mcp_server_url_v2_accepted():
    assert assert_mcp_server_url(MCP_SERVER_URL) == MCP_SERVER_URL


def test_mcp_server_url_sse_retired_refused():
    with pytest.raises(AsanaAuthError):
        assert_mcp_server_url(RETIRED_MCP_SSE_URL)


def test_mcp_server_url_unknown_refused():
    with pytest.raises(AsanaAuthError):
        assert_mcp_server_url("https://mcp.asana.com/v3/mcp")


# ── token workspace binding ─────────────────────────────────────────────────


def test_mcp_token_requires_workspace():
    with pytest.raises(AsanaAuthError):
        SurfaceToken(surface=AuthSurface.MCP, workspace=None)


def test_mcp_token_with_workspace_ok():
    tok = SurfaceToken(surface=AuthSurface.MCP, workspace="900")
    assert tok.workspace == "900"
    # MCP scopes are normalized to the 'default' pseudo-scope.
    assert tok.scopes == (MCP_ONLY_SCOPE,)


def test_mcp_token_real_scope_rejected():
    # A real OAuth scope on an MCP token is an "Invalid scope" fault.
    with pytest.raises(AsanaAuthError):
        SurfaceToken(surface=AuthSurface.MCP, workspace="900", scopes=("tasks:read",))


def test_rest_token_must_not_carry_workspace():
    # A REST token is not workspace-pinned; recording one misrepresents it.
    with pytest.raises(AsanaAuthError):
        SurfaceToken(surface=AuthSurface.NATIVE_REST, workspace="900", scopes=("tasks:read",))


def test_rest_token_with_scopes_ok():
    tok = SurfaceToken(surface=AuthSurface.NATIVE_REST, scopes=("tasks:read", "projects:write"))
    assert tok.workspace is None
    assert tok.scopes == ("tasks:read", "projects:write")


def test_rest_token_without_scopes_rejected():
    # A scope-less REST token is a shaping fault, not a silent pass.
    with pytest.raises(AsanaAuthError):
        SurfaceToken(surface=AuthSurface.NATIVE_REST)


def test_rest_token_with_mcp_pseudo_scope_rejected():
    # The MCP 'default' pseudo-scope is not a valid REST scope.
    with pytest.raises(AsanaAuthError):
        SurfaceToken(surface=AuthSurface.NATIVE_REST, scopes=(MCP_ONLY_SCOPE,))


def test_unknown_surface_is_rejected_fail_closed():
    # F1: an out-of-enum surface value must NOT fall through to the REST path.
    # It is refused (fail-closed), never accepted as a default authorization.
    with pytest.raises(AsanaAuthError):
        SurfaceToken(surface="not-a-surface", scopes=("tasks:read",))  # type: ignore[arg-type]


# ── tokens are not interchangeable across surfaces ──────────────────────────


def test_mcp_token_refused_on_rest():
    tok = SurfaceToken(surface=AuthSurface.MCP, workspace="900")
    with pytest.raises(AuthSurfaceMismatch):
        assert_token_usable_on(tok, AuthSurface.NATIVE_REST)


def test_rest_token_refused_on_mcp():
    tok = SurfaceToken(surface=AuthSurface.NATIVE_REST, scopes=("tasks:read",))
    with pytest.raises(AuthSurfaceMismatch):
        assert_token_usable_on(tok, AuthSurface.MCP)


def test_token_usable_on_own_surface():
    mcp = SurfaceToken(surface=AuthSurface.MCP, workspace="900")
    rest = SurfaceToken(surface=AuthSurface.NATIVE_REST, scopes=("tasks:read",))
    # No exception -> usable.
    assert_token_usable_on(mcp, AuthSurface.MCP)
    assert_token_usable_on(rest, AuthSurface.NATIVE_REST)
