"""What an unauthenticated browser request receives: the deny page or the SPA shell.

These tests pin the JSON 403 for ``/api/*`` and the HTML sign-in page that keeps the
destination path, the public shell served to GET and HEAD navigations without a valid
token and never to data paths, unsafe methods, an unconfigured handler or an IP
mismatch, a valid token on a shell path still minting its cookie and an invalid one
logging its own audit outcome, a shell body that is request-independent and
secret-free, the guidance page and the mtime cache in ``index()``, and the drift guards
that keep every server route, app route and app window entry out of the shell
fallback, each app window at its own route. A topic of the ``test_token_auth`` family:
``test_token_auth.py`` keeps how ``/apps/`` paths are classified, and the other
siblings are ``test_token_auth_signing_secret.py``,
``test_token_auth_session_lifecycle.py``, ``test_token_auth_credential_scope.py`` and
``test_token_auth_tailnet_pin.py``.
"""

from __future__ import annotations

import os

import pytest
from aiohttp import web
from token_auth_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _make_request,
    _ok_handler,
    clear_nonces,
)

from kiro_crew.dashboard.token_auth import (
    bind_token_ip,
    generate_token,
    mark_consumed,
    token_auth_middleware,
    validate_token,
)

# -- Property 12: /api/* paths get JSON 403, non-API non-GET paths get HTML 403 --


@pytest.mark.asyncio
async def test_api_path_gets_json_403() -> None:
    mw = token_auth_middleware()
    req = _make_request(path="/api/status", remote="10.0.0.1")  # No token
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert resp.content_type == "application/json"


@pytest.mark.asyncio
async def test_non_api_path_gets_html_403() -> None:
    # Non-API GET navigations now serve the SPA shell (see
    # test_spa_shell_served_without_token). A non-API request with a
    # state-changing method still hits the deny path and gets HTML 403.
    mw = token_auth_middleware()
    req = _make_request(path="/dashboard", method="POST", remote="10.0.0.1")  # No token
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert resp.content_type == "text/html"
    assert b"Settings \xe2\x86\x92 Security \xe2\x86\x92 Sign in on mobile" in resp.body
    assert b"kirocrew token" in resp.body


@pytest.mark.asyncio
async def test_signin_recovery_preserves_destination_path() -> None:
    # The recovery script runs in the browser on the SAME URL the stale button
    # was tapped (a session deep link, /chat?sid=...&token=<stale>, served 403
    # inline). It must round-trip that destination -- keep the path + query and
    # swap ONLY the pasted token -- instead of redirecting to the dashboard root
    # and discarding /chat?sid=..., which is the "tap and get nowhere" dead end
    # this PR exists to remove (UX stale-tap finding).
    mw = token_auth_middleware()
    req = _make_request(path="/dashboard", method="POST", remote="10.0.0.1")  # No token
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    body = resp.body.decode()
    # Rebuilds from the current URL and swaps only the token param.
    assert "new URL(window.location.href)" in body
    assert "searchParams.set('token'" in body
    # The old path-discarding redirect (protocol//host?token=) is gone.
    assert "window.location.host+'?token='" not in body


# -- Property 12b: SPA shell is public so the app can cold-start refresh --


async def _shell_handler(request: web.Request) -> web.Response:
    """Stand-in for handlers.index — returns a distinguishable body so tests
    can prove the SHELL was served (default-deny) rather than the matched
    route handler (_ok_handler)."""
    return web.Response(text="SHELL", status=200)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/", "/index.html", "/dashboard", "/chat/abc123", "/some/deep/route"]
)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_spa_shell_served_without_token(path: str, method: str) -> None:
    """GET/HEAD to a non-API SPA route with no token serves the shell (200),
    not a 403 — so the React app boots and self-recovers via the refresh
    cookie. The shell handler serves DIRECTLY (default-deny: the matched route
    handler `_ok_handler` is never invoked for an unauthenticated request)."""
    mw = token_auth_middleware(spa_shell_handler=_shell_handler)
    req = _make_request(path=path, method=method, remote="10.0.0.1")  # No token
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert resp.text == "SHELL"  # shell served, NOT the route handler


@pytest.mark.asyncio
async def test_spa_shell_served_with_expired_cookie() -> None:
    """Cold-start variant: an expired/invalid access cookie is still present
    (browser hasn't dropped it yet). The shell still serves so the SPA can
    boot and refresh."""
    mw = token_auth_middleware(spa_shell_handler=_shell_handler)
    req = _make_request(
        path="/", cookies={"mc_token_5476": "garbage.invalid.token"}, remote="10.0.0.1"
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert resp.text == "SHELL"


@pytest.mark.asyncio
async def test_shell_not_served_when_handler_unconfigured() -> None:
    """Default-deny: with no spa_shell_handler wired, a shell GET with no token
    is DENIED (403), not served. The bypass never fails open."""
    mw = token_auth_middleware()  # no shell handler
    req = _make_request(path="/", remote="10.0.0.1")  # No token
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/status", "/api/agents", "/apps/some-app/api/things"])
async def test_data_paths_still_gated_when_shell_public(path: str) -> None:
    """The shell bypass MUST NOT leak to data paths: /api/* and the
    /apps/{name}/api/* reverse proxy still require a valid token even with the
    shell handler wired."""
    mw = token_auth_middleware(spa_shell_handler=_shell_handler)
    req = _make_request(path=path, remote="10.0.0.1")  # No token
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
async def test_shell_bypass_restricted_to_safe_methods(method: str) -> None:
    """Only GET/HEAD serve the shell. A state-changing method to a shell path
    still requires auth — no write ever bypasses."""
    mw = token_auth_middleware(spa_shell_handler=_shell_handler)
    req = _make_request(path="/", method=method, remote="10.0.0.1")  # No token
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_valid_token_on_shell_still_mints_cookie() -> None:
    """A valid ?token= on the shell path is NOT short-circuited by the shell
    bypass — it flows through the normal exchange and mints the access cookie
    (URL-mint flow preserved)."""
    mw = token_auth_middleware(spa_shell_handler=_shell_handler)
    token = generate_token("shell_user", ttl_seconds=300)
    req = _make_request(query={"token": token}, remote="10.0.0.1")  # path="/" default
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert resp.text == "ok"  # routed to the real handler, not the shell
    cookie_header = resp.cookies.get("mc_token_5476")
    assert cookie_header is not None
    # CWE-613: the URL/link token is NEVER reused verbatim as the session cookie;
    # a SEPARATE session token is minted (fresh nonce, same identity). The cookie
    # must therefore differ from the link token but still validate.
    assert cookie_header.value != token
    ok, uid, _ = validate_token(cookie_header.value, use_session_exp=True)
    assert ok is True and uid == "shell_user"


def test_no_get_route_outside_shell_exclusions() -> None:
    """Robust drift guard: every statically-registered GET route in server.py
    must be shell-safe — the index/shell route, a known public PWA/static file,
    under a SPA_FALLBACK_EXCLUDED_PREFIXES data/static prefix, or under the
    /apps/ sub-namespace handled by _APPS_SPA_EXCLUDED_RE. A new GET added
    under a data namespace NOT covered by either guard (e.g. a future
    `GET /v1/models`) fails this test, forcing the exclusion set to be updated
    so the shell bypass can never serve that route unauthenticated.

    Note: /apps/ routes are validated against _APPS_SPA_EXCLUDED_RE (which
    requires a sub-path after {name}/), NOT against SPA_FALLBACK_EXCLUDED_PREFIXES
    (which does not contain "/apps/"; that entry is dead code given
    _is_spa_shell_request gained its own /apps/ early-return branch).
    (Reads source rather than importing server.py to avoid heavy import side effects.)
    """
    import glob
    import os
    import re as _re

    import kiro_crew.dashboard.token_auth as ta

    # server.py and the server_runtime owners it composes, which hold the MCP and
    # build route tables.
    dashboard_dir = os.path.dirname(ta.__file__)
    server_paths = [
        os.path.join(dashboard_dir, "server.py"),
        *sorted(glob.glob(os.path.join(dashboard_dir, "server_runtime", "[!_]*.py"))),
    ]
    assert len(server_paths) > 1, "expected the server_runtime owners beside server.py"
    source = "".join(open(path, encoding="utf-8").read() for path in server_paths)
    get_paths = _re.findall(r'add_get\(\s*["\']([^"\']+)["\']', source)
    assert get_paths, "expected add_get route literals in server.py"

    # The one sanctioned non-literal registration: app window entries, where
    # add_get(route_path, ...) is fed by startup filesystem discovery. Those
    # routes are excluded from the shell fallback BY CONSTRUCTION — the same
    # loop that registers each route appends it to register_app_window_paths()
    # (see test_app_window_entries_register_route_and_exclusion). Assert the
    # coupling is still in place, then assert discovery is the ONLY dynamic
    # add_get so a future one cannot ride in under this exemption.
    dynamic_gets = _re.findall(r"add_get\((?!\s*[\"'])\s*(\w+)", source)
    assert dynamic_gets == ["route_path"], (
        f"non-literal add_get registrations in server.py: {dynamic_gets}. Only "
        f"the app-window-entry discovery loop may register dynamic GET routes, "
        f"and it must pair every route with register_app_window_paths()."
    )
    assert "register_app_window_paths(window_paths)" in source

    nonshell = tuple(ta.SPA_FALLBACK_EXCLUDED_PREFIXES)
    bypass_exact = ta._BYPASS_EXACT

    def shell_safe(p: str) -> bool:
        return (
            p == "/"  # the SPA shell itself
            or p in bypass_exact  # explicit public files (logo, etc.)
            or p.startswith("/{")  # pattern route (PWA: manifest/sw/icon/worklet)
            or p.startswith(nonshell)  # data/static namespaces (/api/, /v1/, etc.)
            # /apps/ routes: safe when they have a sub-path after {name}/ (real
            # handler) OR when they are aiohttp pattern routes (contain "{" in
            # the path, e.g. /apps/{name}/ui/{path:.*}). _APPS_SPA_EXCLUDED_RE
            # matches concrete /apps/{lower-name}/<sub-path> from the live router;
            # pattern-literal routes (with { placeholders) are always real handlers.
            or (p.startswith("/apps/") and (ta._APPS_SPA_EXCLUDED_RE.match(p) or "{" in p))
        )

    offenders = [p for p in get_paths if not shell_safe(p)]
    assert not offenders, (
        f"GET route(s) outside the shell-exclusion set would be served the SPA "
        f"shell unauthenticated: {offenders}. Add their namespace to "
        f"SPA_FALLBACK_EXCLUDED_PREFIXES (or _APPS_SPA_EXCLUDED_RE for /apps/ "
        f"sub-paths) in token_auth_paths.py."
    )


def test_apps_routes_get_paths_are_matched_by_the_apps_spa_regex() -> None:
    """Every ``/apps/`` GET registered in ``apps/routes.py`` must be matched by
    ``_APPS_SPA_EXCLUDED_RE`` once its placeholders are filled in.

    The sibling guard above only scans ``server.py``, and its ``"{" in p``
    escape hatch treats any pattern route as a real handler without ever asking
    the regex. So nothing coupled the regex to the app routes it exists to
    describe: adding ``/apps/{name}/art/{path:.*}`` to ``apps/routes.py`` while
    leaving the regex at ``(?:api|ui)`` left the route registered and
    unreachable — ``_is_spa_shell_request`` classified it as a React Router
    navigation and the middleware answered the SPA shell, so an ``<img>``
    pointed at it received HTML with a 200 and rendered nothing. Silent by
    construction: the route works under a token, the handler is never the thing
    that fails, and no existing test looks at this pair.

    Instantiating the pattern is the whole point — the regex requires a
    lowercase app name AND a following slash, so only a concrete path can
    exercise it.
    """
    import re as _re

    import kiro_crew.apps.routes as app_routes
    import kiro_crew.dashboard.token_auth as ta

    source = open(app_routes.__file__, encoding="utf-8").read()
    get_paths = [p for p in _re.findall(r'add_get\(\s*["\']([^"\']+)["\']', source)]
    apps_paths = [p for p in get_paths if p.startswith("/apps/")]
    assert apps_paths, "expected /apps/ add_get route literals in apps/routes.py"

    def concrete(pattern: str) -> str:
        """Fill each ``{…}`` placeholder with a value the regex would accept."""
        out = _re.sub(r"\{name[^}]*\}", "demo-app", pattern)
        return _re.sub(r"\{[^}]+\}", "assets/icon.webp", out)

    unmatched = [p for p in apps_paths if not ta._APPS_SPA_EXCLUDED_RE.match(concrete(p))]
    assert not unmatched, (
        f"/apps/ GET route(s) in apps/routes.py that _APPS_SPA_EXCLUDED_RE does not "
        f"match: {unmatched}. The middleware will answer these with the SPA shell "
        f"instead of the handler — add the sub-namespace verb to the regex in "
        f"token_auth_paths.py."
    )


@pytest.mark.asyncio
async def test_shell_bypass_does_not_preempt_ip_mismatch() -> None:
    """A VALID token from the wrong IP on a shell path is still a hard 403 —
    the shell bypass lives only in the no-token / invalid-token branches, so a
    valid token flows through to the IP-binding check. Pins the 'IP mismatch
    stays 403' invariant: this test fails if the bypass is ever moved above
    validation/IP-binding."""
    mw = token_auth_middleware(spa_shell_handler=_shell_handler)
    token = generate_token("ipuser", ttl_seconds=300)
    bind_token_ip(token, "10.0.0.1")
    mark_consumed(token)
    req = _make_request(
        path="/", cookies={"mc_token_5476": token}, remote="10.0.0.99"
    )  # valid token, different IP, shell path
    resp = await mw(req, _ok_handler)
    assert resp.status == 403  # IP mismatch wins; shell bypass does NOT fire


@pytest.mark.asyncio
async def test_invalid_token_shell_serve_emits_distinct_sel_outcome(monkeypatch) -> None:
    """The invalid/forged-token shell serve MUST log a distinct, non-"ok" SEL
    outcome so anomaly detection still flags credential-forgery probing on GET
    navigations. Pins the observability signal: fails if the outcome reverts
    to "ok"."""
    import kiro_crew.dashboard.token_auth as ta

    calls: list[dict] = []

    class _FakeSel:
        def log_api_access(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(ta, "_sel_fn", lambda: _FakeSel())
    mw = ta.token_auth_middleware(spa_shell_handler=_shell_handler)
    req = _make_request(
        path="/", cookies={"mc_token_5476": "garbage.invalid.token"}, remote="10.0.0.1"
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    outcomes = [c.get("outcome") for c in calls]
    assert "shell_unauth_invalid_token" in outcomes  # forgery signal preserved
    assert "ok" not in outcomes  # NOT logged as an authenticated success


@pytest.mark.asyncio
async def test_served_shell_is_auth_independent() -> None:
    """Load-bearing invariant for the cold-start bypass: the SPA shell that
    the middleware serves UNAUTHENTICATED must be byte-identical to the shell
    an authenticated client gets, and must carry no injected server/user state.

    The bypass is only safe while index.html is a static, secret-free bundle
    (see the SECURITY CONTRACT on handlers.core.index). If a future change
    inlines bootstrap state (a username, token, feature flags, session blob)
    into the shell, an unauthenticated GET / would leak it across the auth
    boundary. This test fails if the served body becomes request-dependent or
    starts carrying credential/state markers.
    """
    import kiro_crew.dashboard.handlers.core as core

    # Unauthenticated cold-start request vs an "authenticated-looking" one
    # (cookies + remote set). index() must ignore request state entirely.
    anon = _make_request(path="/", remote="10.0.0.1")
    authed = _make_request(
        path="/",
        cookies={"mc_token_5476": "someminted.token.value"},
        remote="10.0.0.1",
    )
    resp_anon = await core.index(anon)
    resp_authed = await core.index(authed)

    # 1. Request-independent: the unauth shell == the authed shell.
    assert resp_anon.text == resp_authed.text

    # 2. No credential/server-state markers leaked into the served shell.
    body = resp_anon.text
    for marker in (
        "mc_token",
        "mc_refresh",
        "refresh_chains",
        "security_events",
        "BEGIN PRIVATE",
        "someminted.token.value",
    ):
        assert marker not in body, f"shell leaked state marker: {marker!r}"


@pytest.mark.asyncio
async def test_index_serves_guidance_when_bundle_missing(tmp_path, monkeypatch) -> None:
    """Fallback branch: when the React build cannot be read, index() serves the
    static guidance page (recognizable heading + cause + restart hint) instead
    of a bare error. It must remain request-independent and secret-free -- the
    same cold-start security contract as the normal shell (this handler is
    served unauthenticated). The legacy dashboard.html fallback was removed
    (security-review); dist/index.html is the sole shell source.
    """
    import kiro_crew.dashboard.handlers.core as core

    # Point the React build index at a non-existent location so index() falls
    # into the FileNotFoundError -> guidance-page branch.
    monkeypatch.setattr(core, "_DIST_INDEX", tmp_path / "no-dist" / "index.html")

    anon = _make_request(path="/", remote="10.0.0.1")
    authed = _make_request(
        path="/",
        cookies={"mc_token_7777": "someminted.token.value"},
        remote="10.0.0.1",
    )
    resp_anon = await core.index(anon)
    resp_authed = await core.index(authed)

    assert resp_anon.status == 200
    assert resp_anon.content_type == "text/html"
    body = resp_anon.text
    # Recognizable heading preserved + actionable guidance added.
    assert "<h1>Dashboard HTML not found</h1>" in body
    assert "restarting Kiro Crew" in body
    assert "kirocrew restart" in body
    # Request-independent and secret-free (same contract as the served shell).
    assert resp_anon.text == resp_authed.text
    assert "someminted.token.value" not in body


# -- index() SPA shell caching -----------------------------------


@pytest.mark.asyncio
async def test_index_caches_html_and_rerereads_only_on_rebuild(tmp_path, monkeypatch) -> None:
    """The shell is cached keyed by index.html's mtime: repeated requests with
    an UNCHANGED file read disk once; a rebuild (mtime change) is picked up on
    the next request WITHOUT a restart.

    Two things are pinned: no unnecessary per-request read of a static bundle,
    and no process-lifetime cache that would pin a
    pre-rebuild shell after a Vite rebuild rewrote the hashed asset refs.
    """
    import kiro_crew.dashboard.handlers.core as core

    fake_index = tmp_path / "index.html"
    fake_index.write_text("<html>spa-shell-v1</html>", encoding="utf-8")

    monkeypatch.setattr(core, "_INDEX_HTML_CACHE", None)
    monkeypatch.setattr(core, "_DIST_INDEX", fake_index)

    call_count = 0
    original_read_text = fake_index.__class__.read_text

    def counting_read_text(self, *args, **kwargs):
        nonlocal call_count
        if self == fake_index:
            call_count += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(fake_index.__class__, "read_text", counting_read_text)

    # Two requests with the file unchanged → one disk read, cache serves the 2nd.
    resp1 = await core.index(_make_request(path="/", remote="127.0.0.1"))
    resp2 = await core.index(_make_request(path="/", remote="127.0.0.1"))
    assert resp1.text == "<html>spa-shell-v1</html>"
    assert resp2.text == resp1.text
    assert call_count == 1, (
        f"_DIST_INDEX.read_text was called {call_count} times across two "
        "unchanged requests; expected 1 (second must hit the mtime cache)"
    )

    # Simulate a rebuild: new content AND a strictly newer mtime.
    stale = fake_index.stat().st_mtime_ns
    fake_index.write_text("<html>spa-shell-v2</html>", encoding="utf-8")
    os.utime(fake_index, ns=(stale + 1_000_000_000, stale + 1_000_000_000))

    resp3 = await core.index(_make_request(path="/", remote="127.0.0.1"))
    assert (
        resp3.text == "<html>spa-shell-v2</html>"
    ), "a rebuilt bundle (changed mtime) must be re-read, not served stale"
    assert (
        call_count == 2
    ), f"expected a re-read after the mtime changed; read_text calls={call_count}"


@pytest.mark.asyncio
async def test_index_missing_bundle_not_cached_recovers_when_dist_appears(
    tmp_path, monkeypatch
) -> None:
    """A FileNotFoundError must NOT be cached: once the bundle appears on disk
    the next request should serve it (self-healing after a dev build).
    """
    import kiro_crew.dashboard.handlers.core as core

    fake_index = tmp_path / "index.html"
    # Do NOT create the file yet — first request should get the fallback.

    monkeypatch.setattr(core, "_INDEX_HTML_CACHE", None)
    monkeypatch.setattr(core, "_DIST_INDEX", fake_index)

    req = _make_request(path="/", remote="127.0.0.1")

    # First request: bundle absent → fallback, cache stays None.
    resp_missing = await core.index(req)
    assert core.DASHBOARD_HTML_NOT_FOUND_MARKER in resp_missing.text
    assert core._INDEX_HTML_CACHE is None, "FileNotFoundError path must NOT populate the cache"

    # Bundle appears (e.g. dev build completed).
    fake_index.write_text("<html>now-built</html>", encoding="utf-8")

    # Second request: bundle present → real shell served and cached.
    resp_recovered = await core.index(req)
    assert resp_recovered.text == "<html>now-built</html>"
    assert core._INDEX_HTML_CACHE is not None
    assert core._INDEX_HTML_CACHE[1] == "<html>now-built</html>"

    # Third request: still the cached value, no further disk read.
    resp_cached = await core.index(req)
    assert resp_cached.text == "<html>now-built</html>"


def test_ambiguous_app_and_window_names_cannot_collide(tmp_path) -> None:
    """A name pair that could collide now yields two distinct routes.

    The old scheme served these flat at ``/<app>-<window>.html``, which is
    ambiguous the moment either name contains a hyphen: app ``foo`` + window
    ``bar-baz`` and app ``foo-bar`` + window ``baz`` both spell
    ``/foo-bar-baz.html``, so one of them had to be refused and the other's
    window was simply unavailable. Keeping the boundary the filesystem already
    has removes the class rather than handling it — this pins that, using the
    exact pair that was the counter-example.
    """
    from kiro_crew.dashboard.server import discover_app_window_entries

    root = tmp_path / "src" / "apps"
    (root / "foo").mkdir(parents=True)
    (root / "foo-bar").mkdir(parents=True)
    (root / "foo" / "bar-baz.html").write_text("<html>first</html>")
    (root / "foo-bar" / "baz.html").write_text("<html>second</html>")
    (root / "foo" / "solo.html").write_text("<html>solo</html>")

    entries = discover_app_window_entries(root)
    routes = dict(entries)

    # Both survive, each addressable, neither shadowing the other.
    assert len(routes) == 3, f"every window must register: {sorted(routes)}"
    assert routes["/app-windows/foo/bar-baz.html"] == root / "foo" / "bar-baz.html"
    assert routes["/app-windows/foo-bar/baz.html"] == root / "foo-bar" / "baz.html"
    assert "/app-windows/foo/solo.html" in routes
    # And the URL a caller builds is derivable from the path, not guessed from it.
    for route, path in entries:
        assert route == f"/app-windows/{path.parent.name}/{path.name}"


def test_app_window_entries_register_route_and_exclusion(tmp_path) -> None:
    """App window entries: discovery must couple route and shell exclusion.

    server.py enumerates dist/src/apps/<app>/<name>.html at startup and, in one
    loop, registers GET /<app>-<name>.html AND excludes that exact path from
    the unauthenticated SPA-shell fallback. This test drives the real
    registration function against a fixture dist tree and asserts both halves,
    plus path-shape safety (the route comes from the enumerated file, never
    from the request).
    """
    from aiohttp import web

    import kiro_crew.dashboard.token_auth as ta

    # Fixture dist: one app with two windows, one unrelated non-html file.
    win = tmp_path / "src" / "apps" / "someapp"
    win.mkdir(parents=True)
    (win / "pet.html").write_text("<html></html>")
    (win / "panel.html").write_text("<html></html>")
    (win / "notes.txt").write_text("not a window")

    # Drive the REAL discovery helper rather than restating the route
    # construction: the previous form duplicated it, and duplicated it in the
    # OLD flat shape, so it kept passing after the scheme changed and asserted
    # nothing about what the gateway actually registers.
    from kiro_crew.dashboard.server import (
        _window_entry_handler,
        discover_app_window_entries,
    )

    app = web.Application()
    window_paths: list[str] = []
    for route_path, entry in discover_app_window_entries(tmp_path / "src" / "apps"):
        # Same handler factory the gateway uses — see server._window_entry_handler
        # for why the path is a closure cell and not a handler parameter.
        app.router.add_get(
            route_path, _window_entry_handler(tmp_path, f"{entry.parent.name}/{entry.name}")
        )
        window_paths.append(route_path)

    prior = ta._APP_WINDOW_EXCLUDED_PATHS
    try:
        ta.register_app_window_paths(window_paths)

        registered = {r.resource.canonical for r in app.router.routes() if r.method == "GET"}
        assert {
            "/app-windows/someapp/panel.html",
            "/app-windows/someapp/pet.html",
        } <= registered
        assert "/app-windows/someapp/notes.html" not in registered  # only .html

        # Both routes are excluded from the shell fallback; a sibling path that
        # was NOT discovered is not (exact-path matching, no prefix bleed).
        assert "/app-windows/someapp/pet.html" in ta._APP_WINDOW_EXCLUDED_PATHS
        assert "/app-windows/someapp/panel.html" in ta._APP_WINDOW_EXCLUDED_PATHS
        assert "/app-windows/someapp/other.html" not in ta._APP_WINDOW_EXCLUDED_PATHS
    finally:
        ta._APP_WINDOW_EXCLUDED_PATHS = prior
