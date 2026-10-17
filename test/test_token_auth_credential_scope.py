"""The routes each dashboard credential may reach, and malformed ones refused as wrong.

These tests pin the app-token least-privilege scope (the path-pattern and namespace
boundaries, the reserved ``/api/apps/`` segments, denial on unscoped and internal
paths, the implicit ``/api/ws`` grant) with a user token left outside it, the internal
secret reaching every ``/api/deploy`` route, and a non-ASCII signature, token, internal
secret or app secret refused like a wrong one instead of raising. A topic of the
``test_token_auth`` family: ``test_token_auth.py`` keeps the internal and mixed
internal path gate, and the other siblings are ``test_token_auth_signing_secret.py``,
``test_token_auth_session_lifecycle.py``, ``test_token_auth_spa_shell.py`` and
``test_token_auth_tailnet_pin.py``.
"""

from __future__ import annotations

import pytest
from token_auth_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _make_request,
    _ok_handler,
    clear_nonces,
)

from kiro_crew.dashboard.token_auth import (
    _api_pattern_matches,
    _app_owns_path,
    app_token_path_allowed,
    bind_token_ip,
    generate_token,
    mark_consumed,
    token_auth_middleware,
    validate_token,
)

# -- App-token least-privilege scope (CWE-269) -------------------------------


def test_api_pattern_matches_boundaries() -> None:
    # Bare prefix: exact + child under a path boundary, but not a longer name.
    assert _api_pattern_matches("/api/chat", "/api/chat")
    assert _api_pattern_matches("/api/chat", "/api/chat/slots")
    assert not _api_pattern_matches("/api/chat", "/api/chatx")
    # Trailing /* — inclusive of the base and its children.
    assert _api_pattern_matches("/api/chat/*", "/api/chat")
    assert _api_pattern_matches("/api/chat/*", "/api/chat/slots/1")
    # Bare * — raw prefix.
    assert _api_pattern_matches("/api/status*", "/api/status-page")
    assert not _api_pattern_matches("", "/anything")


def test_app_owns_path_boundaries() -> None:
    assert _app_owns_path("foo", "/apps/foo/api/x")
    assert _app_owns_path("foo", "/apps/foo/ui/index.js")
    assert _app_owns_path("foo", "/api/apps/foo/config")
    # Path boundary: foo must not match foo-bar.
    assert not _app_owns_path("foo", "/apps/foo-bar/api/x")
    assert not _app_owns_path("foo", "/api/apps/foo-bar/config")
    # Unrelated dashboard endpoints are never owned.
    assert not _app_owns_path("foo", "/api/sessions")
    assert not _app_owns_path("foo", "/api/config/kirocrew")
    # An app named `registry` cannot claim the store's manual refresh, and TWO
    # independent controls now say so. Route placement: the endpoint lives at
    # /api/app-store/refresh, a namespace no app name can collide with. The
    # reserved-segment carve-out below: even under the /api/apps/registry/refresh
    # spelling (never a registered route — this pins the boundary, not a live
    # endpoint) the name does not own the path, so relocating a shared route
    # under /api/apps/ cannot silently hand it to a same-named app.
    assert not _app_owns_path("registry", "/api/app-store/refresh")
    assert not _app_owns_path("registry", "/api/apps/registry/refresh")

    # Reserved-segment carve-out: the literal
    # first-segment routes under /api/apps/ (registry, registries, blob, install,
    # register) are SHARED routes registered before the /api/apps/{name}
    # catch-all. An app that names itself after one of them must NOT implicitly
    # own that route via _app_owns_path — that was the CWE-269 authorization
    # bypass (POST /api/apps/registries/refresh triggers outbound git fetches of
    # every configured registry with no permissions.api grant).
    assert not _app_owns_path("registries", "/api/apps/registries/refresh")
    assert not _app_owns_path("registries", "/api/apps/registries")
    assert not _app_owns_path("registry", "/api/apps/registry/install")
    assert not _app_owns_path("registry", "/api/apps/registry/install-stream")
    assert not _app_owns_path("install", "/api/apps/install")
    assert not _app_owns_path("register", "/api/apps/register")
    assert not _app_owns_path("blob", "/api/apps/blob")

    # A normal app name is UNAFFECTED: only the reserved literal segments are
    # carved out, and path-boundary protection is unchanged.
    assert _app_owns_path("foo", "/api/apps/foo/config")

    # The /apps/ reverse-proxy/UI branch is a SEPARATE namespace and is
    # intentionally left unchanged by the carve-out: its literal-page
    # reservations live in apps.manifest.RESERVED_ROUTE_APP_NAMES, not here. An
    # app can never actually be named one of these segments (manifest validation
    # now rejects them), but were the /apps/ proxy reached it would still resolve
    # by name — the carve-out deliberately touches only /api/apps/.
    assert _app_owns_path("registries", "/apps/registries/api/x")
    assert _app_owns_path("registry", "/apps/registry/ui/index.js")


def test_reserved_app_path_segments_stay_in_sync() -> None:
    """Drift guard: RESERVED_APP_PATH_SEGMENTS is defined
    INDEPENDENTLY in dashboard.token_auth and apps.manifest (duplicated rather
    than shared to avoid a manifest <-> token_auth import cycle). Both sets, plus
    the literal /api/apps/<segment> route table in apps.routes.setup_routes, are
    hand-maintained sources of truth that must agree; if they silently drift the
    _app_owns_path carve-out reopens for the un-mirrored segment (the CWE-269
    authorization bypass this fix closes). This asserts the two module sets are
    identical so a future edit to one without the other fails loudly here.
    """
    from kiro_crew.apps.manifest import RESERVED_APP_PATH_SEGMENTS as manifest_segments
    from kiro_crew.dashboard.token_auth import RESERVED_APP_PATH_SEGMENTS as token_auth_segments

    assert token_auth_segments == manifest_segments


def test_app_token_path_allowed_empty_name_denies() -> None:
    # Defensive: an empty app_name must never be treated as "allow all".
    assert app_token_path_allowed("", "/api/sessions") is False


@pytest.mark.asyncio
async def test_app_token_denied_on_unscoped_endpoint(monkeypatch) -> None:
    """The pentest scenario: an app token hitting /api/sessions must be 403."""
    import kiro_crew.dashboard.token_auth as _ta

    monkeypatch.setattr(_ta, "_app_api_allowlist", lambda name: ())
    mw = token_auth_middleware()
    token = generate_token("file-explorer", ttl_seconds=300, app="file-explorer")
    req = _make_request(path="/api/sessions", query={"token": token})
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_app_token_named_registries_denied_on_refresh(monkeypatch) -> None:
    """An app token named 'registries' must NOT reach the shared
    POST /api/apps/registries/refresh (which triggers outbound git fetches of
    every configured registry). With _app_api_allowlist forced empty, the only
    way a grant could happen is the _app_owns_path carve-out failing — so a 403
    here proves the deny is attributable to the carve-out, not a missing perm.
    """
    import kiro_crew.dashboard.token_auth as _ta

    monkeypatch.setattr(_ta, "_app_api_allowlist", lambda name: ())
    mw = token_auth_middleware()
    token = generate_token("registries", ttl_seconds=300, app="registries")
    req = _make_request(path="/api/apps/registries/refresh", query={"token": token})
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_app_token_allowed_on_own_namespace(monkeypatch) -> None:
    import kiro_crew.dashboard.token_auth as _ta

    monkeypatch.setattr(_ta, "_app_api_allowlist", lambda name: ())
    mw = token_auth_middleware()
    token = generate_token("file-explorer", ttl_seconds=300, app="file-explorer")
    req = _make_request(path="/apps/file-explorer/api/list", query={"token": token})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_app_token_allowed_on_declared_api(monkeypatch) -> None:
    import kiro_crew.dashboard.token_auth as _ta

    monkeypatch.setattr(_ta, "_app_api_allowlist", lambda name: ("/api/widgets/*",))
    mw = token_auth_middleware()
    token = generate_token("file-explorer", ttl_seconds=300, app="file-explorer")
    req = _make_request(path="/api/widgets/abc", query={"token": token})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_user_token_unaffected_by_app_scope() -> None:
    """A dashboard-user token (no app claim) still reaches /api/sessions."""
    mw = token_auth_middleware()
    token = generate_token("alice", ttl_seconds=300)
    req = _make_request(path="/api/sessions", query={"token": token})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_app_token_denied_on_mixed_internal_path(monkeypatch) -> None:
    """Escalation guard: an app token on a mixed_internal path (e.g. /api/chat)
    over loopback must NOT be silently treated as the dashboard user. Before the
    fix this branch never set request['app'] and granted unconditionally."""
    import kiro_crew.dashboard.token_auth as _ta

    monkeypatch.setattr(_ta, "_app_api_allowlist", lambda name: ())
    mw = token_auth_middleware(mixed_internal_paths=frozenset({"/api/chat"}))
    token = generate_token("file-explorer", ttl_seconds=300, app="file-explorer")
    req = _make_request(
        path="/api/chat",
        cookies={"mc_token_5476": token},
        remote="127.0.0.1",
        method="POST",
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_app_token_denied_on_strict_internal_path(monkeypatch) -> None:
    """Impersonation guard: an app token hitting a strict-internal path such as
    /api/send-message over loopback (no secret header) must be scope-denied —
    otherwise a compromised app could send notifications impersonating the
    system (app-sandbox-roadmap threat)."""
    import kiro_crew.dashboard.token_auth as _ta

    monkeypatch.setattr(_ta, "_app_api_allowlist", lambda name: ())
    mw = token_auth_middleware(internal_paths=frozenset({"/api/send-message"}))
    token = generate_token("file-explorer", ttl_seconds=300, app="file-explorer")
    req = _make_request(
        path="/api/send-message",
        cookies={"mc_token_5476": token},
        remote="127.0.0.1",
        method="POST",
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


# --- item #2: /api/deploy reachable via X-Internal-Secret (MCP tool path) ---


@pytest.mark.asyncio
async def test_deploy_path_accessible_via_internal_secret() -> None:
    """/api/deploy/deploy must be reachable with X-Internal-Secret (mixed_internal_paths).

    Regression test: the deploy_artifact MCP tool posts to /api/deploy/deploy with
    X-Internal-Secret. Without /api/deploy in mixed_internal_paths, this falls through
    to cookie auth and the tool NEVER works.
    """
    secret = "deploy-secret-xyz"
    mw = token_auth_middleware(
        mixed_internal_paths=frozenset({"/api/deploy"}), internal_secret=secret
    )
    req = _make_request(path="/api/deploy/deploy", headers={"X-Internal-Secret": secret})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_deploy_path_prefix_matching() -> None:
    """All /api/deploy/* sub-routes are covered by the prefix entry."""
    secret = "deploy-secret-xyz"
    mw = token_auth_middleware(
        mixed_internal_paths=frozenset({"/api/deploy"}), internal_secret=secret
    )
    for path in ("/api/deploy/deploy", "/api/deploy/list", "/api/deploy/profiles"):
        req = _make_request(path=path, headers={"X-Internal-Secret": secret})
        resp = await mw(req, _ok_handler)
        assert resp.status == 200, f"expected 200 for {path}, got {resp.status}"


def test_app_token_path_allowed_implicit_ws():
    """``/api/ws`` is the only implicitly allowed path.

    It is connection infrastructure rather than a capability, and the implicit
    grant is sound only because the WS layer filters events per app
    (``ws_event_scope.py``). ``/api/status`` has NO such response-level filter
    and discloses ``owner_id_hash``, host specs, cron/usage stats and the live
    safety-override state, so it must be declared like any other capability.
    Functional paths must still be declared, so the negative case is asserted
    alongside.
    """
    from kiro_crew.dashboard.token_auth import app_token_path_allowed

    assert app_token_path_allowed("some-app", "/api/ws") is True
    assert app_token_path_allowed("some-app", "/api/status") is False
    # Undeclared functional paths stay denied.
    assert app_token_path_allowed("some-app", "/api/chat") is False
    assert app_token_path_allowed("some-app", "/api/spawn") is False
    # An empty app name must never be granted, even for implicit paths.
    assert app_token_path_allowed("", "/api/ws") is False


# -- A non-ASCII credential is a wrong credential, never a crash --
#
# hmac.compare_digest raises TypeError on a str holding a non-ASCII character.
# "\udcff" is what a raw non-UTF-8 header byte decodes to; "\ud800" is any lone surrogate.

_NON_ASCII = ["é", "\udcff", "\ud800"]


@pytest.mark.parametrize("bad", _NON_ASCII)
def test_non_ascii_signature_is_invalid_not_a_crash(bad: str) -> None:
    payload = generate_token("user-na").split(".", 1)[0]
    assert validate_token(f"{payload}.{bad}") == (False, "", "invalid signature")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", _NON_ASCII)
async def test_non_ascii_query_token_denied_like_a_forged_one(bad: str) -> None:
    payload = generate_token("user-na").split(".", 1)[0]
    mw = token_auth_middleware()
    forged = await mw(_make_request(path="/", query={"token": f"{payload}.AAAA"}), _ok_handler)
    resp = await mw(_make_request(path="/", query={"token": f"{payload}.{bad}"}), _ok_handler)
    assert forged.status in (401, 403)
    assert resp.status == forged.status


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", _NON_ASCII)
async def test_non_ascii_internal_secret_denied_loopback(bad: str) -> None:
    mw = token_auth_middleware(internal_paths=frozenset({"/api/spawn"}), internal_secret="real")
    req = _make_request(path="/api/spawn", headers={"X-Internal-Secret": bad})
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", _NON_ASCII)
async def test_non_ascii_internal_secret_denied_non_loopback_mixed(bad: str) -> None:
    token = generate_token("testuser", ttl_seconds=300)
    bind_token_ip(token, "10.0.0.1")
    mark_consumed(token)
    mw = token_auth_middleware(
        mixed_internal_paths=frozenset({"/api/spawn"}), internal_secret="real"
    )
    req = _make_request(
        path="/api/spawn",
        remote="10.0.0.1",
        headers={"X-Internal-Secret": bad},
        cookies={"mc_token_5476": token},
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.parametrize("bad", _NON_ASCII)
def test_non_ascii_app_secret_is_rejected(bad: str, tmp_path) -> None:
    from kiro_crew.dashboard.token_auth import validate_app_secret

    app_dir = tmp_path / "apps" / "demo"
    app_dir.mkdir(parents=True)
    (app_dir / ".app_secret").write_text("real", encoding="utf-8")
    assert validate_app_secret("demo", "real") is True
    assert validate_app_secret("demo", bad) is False
