"""Tests for _apply_security_headers on dashboard responses.

Guards the Permissions-Policy header that unblocks
``navigator.clipboard.writeText`` on Chrome 143+ (crbug.com/414348233),
the Cache-Control triplet, and the CSP header (including the
instances-mode frame-src extension).
"""

from __future__ import annotations

from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.server import _apply_security_headers


def _make_response() -> web.Response:
    return web.Response(text="ok")


def _make_app(with_instances: bool = False) -> web.Application:
    app = web.Application()
    instances_manager = object() if with_instances else None
    app["state"] = SimpleNamespace(instances_manager=instances_manager)
    return app


class TestApplySecurityHeaders:
    def test_permissions_policy_allows_clipboard_write(self) -> None:
        resp = _make_response()
        _apply_security_headers(resp, _make_app())
        # The specific value matters: Chrome 143+ requires the exact
        # allowlist form; a bare "clipboard-write" without the (self)
        # source expression does not grant permission.
        assert "clipboard-write=(self)" in resp.headers["Permissions-Policy"]
        assert "clipboard-read=(self)" in resp.headers["Permissions-Policy"]

    def test_cache_headers_prevent_stale_asset_caching(self) -> None:
        resp = _make_response()
        _apply_security_headers(resp, _make_app())
        assert "no-store" in resp.headers["Cache-Control"]
        assert resp.headers["Pragma"] == "no-cache"
        assert resp.headers["Expires"] == "0"

    def test_hashed_assets_are_immutable_cached(self) -> None:
        """Vite content-hashed bundles under /assets/ must be cacheable:
        the URL is the version, so no-store would force a full multi-MB
        re-download on every page load (and make post-restart reloads bet
        on a 6MB transfer during gateway cold-start)."""
        resp = _make_response()
        _apply_security_headers(resp, _make_app(), path="/assets/index-D9K94z8J.js")
        cc = resp.headers["Cache-Control"]
        assert "immutable" in cc
        assert "max-age=31536000" in cc
        assert "no-store" not in cc
        # The no-cache companion headers must not undermine the cache
        assert "Pragma" not in resp.headers
        assert "Expires" not in resp.headers
        # Security headers still applied on the immutable path
        assert "Content-Security-Policy" in resp.headers
        assert "Permissions-Policy" in resp.headers

    def test_non_200_under_assets_stays_no_store(self) -> None:
        """During cold-start, /assets/* may return 404 or 503. Caching that
        with immutable would be a permanent black screen. Only success
        statuses get the immutable treatment."""
        for status in (404, 503):
            resp = web.Response(text="error", status=status)
            _apply_security_headers(resp, _make_app(), path="/assets/index-D9K94z8J.js")
            assert "no-store" in resp.headers["Cache-Control"], f"status={status}"
            assert "immutable" not in resp.headers["Cache-Control"], f"status={status}"

    def test_conditional_and_range_under_assets_stay_immutable(self) -> None:
        """aiohttp's static handler answers 304 (conditional) and 206 (range)
        for hashed assets. A 304's headers merge into the browser's stored
        cache entry — answering it with no-store would degrade the cached
        immutable bundle back to uncacheable."""
        for status in (206, 304):
            resp = web.Response(status=status)
            _apply_security_headers(resp, _make_app(), path="/assets/index-D9K94z8J.js")
            assert "immutable" in resp.headers["Cache-Control"], f"status={status}"
            assert "no-store" not in resp.headers["Cache-Control"], f"status={status}"

    def test_shell_and_api_paths_stay_no_store(self) -> None:
        for path in ("/", "/index.html", "/api/health", "/apps/dev-fleet"):
            resp = _make_response()
            _apply_security_headers(resp, _make_app(), path=path)
            assert "no-store" in resp.headers["Cache-Control"], path

    def test_unhashed_static_prefixes_stay_no_store(self) -> None:
        """/vendor, /fonts and /sprites use stable filenames — immutable
        caching would pin stale content across upgrades."""
        for path in (
            "/vendor/react.js",
            "/fonts/diatype.woff2",
            "/sprites/icons.svg",
        ):
            resp = _make_response()
            _apply_security_headers(resp, _make_app(), path=path)
            assert "no-store" in resp.headers["Cache-Control"], path

    def test_csp_default_no_instances(self) -> None:
        resp = _make_response()
        _apply_security_headers(resp, _make_app(with_instances=False))
        csp = resp.headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp
        assert "object-src 'none'" in csp
        # Loopback frame-src is ALWAYS admitted (not instances-gated) so the
        # Web Preview panel (WebPreviewPanel) can frame a local dev/static
        # server in the packaged dashboard. The panel isolates the preview host
        # from the dashboard host so no host-scoped cookie is sent to the frame.
        assert "http://127.0.0.1:*" in csp
        assert "http://localhost:*" in csp
        # …and http+https across the IPv4 loopback hosts normalizeUrl accepts, so
        # a preview never renders blank due to a CSP-blocked frame. IPv6 loopback
        # ([::1]:*) is intentionally absent — see test_csp_no_ipv6_wildcard_source.
        assert "http://[::1]:*" not in csp
        assert "https://localhost:*" in csp
        assert "https://127.0.0.1:*" in csp
        # The *.localhost tunnel wildcard, however, stays instances-only.
        assert "http://*.localhost:*" not in csp
        assert "frame-ancestors 'self'" in csp

    def test_csp_script_src_admits_wasm_but_not_eval(self) -> None:
        """The Pierre highlight workers tokenize with the shiki-wasm engine
        (website/src/pierre/config.ts, PIERRE_REGEX_ENGINE), and a same-origin
        worker takes its CSP from its own script response — this header.
        WebAssembly.instantiate needs 'wasm-unsafe-eval' in script-src or the
        tokenizer worker dies on first highlight and every diff surface
        unmounts. The expression admits ONLY WASM compilation: JS eval stays
        banned, which the second assertion pins so a future edit cannot
        silently widen this into 'unsafe-eval'."""
        resp = _make_response()
        _apply_security_headers(resp, _make_app(with_instances=False))
        csp = resp.headers["Content-Security-Policy"]
        script_src = next(d for d in csp.split(";") if d.strip().startswith("script-src"))
        assert "'wasm-unsafe-eval'" in script_src
        assert "'unsafe-eval'" not in script_src.replace("'wasm-unsafe-eval'", "")

    def test_csp_connect_src_allows_loopback_liveness_probe(self) -> None:
        """WebPreviewPanel polls the framed dev server with a no-cors ``fetch``
        because a cross-origin iframe cannot report that its server died. When
        connect-src admitted only ``'self'`` + ws:// loopback, that probe threw
        on every tick and two strikes declared a healthy preview
        "stopped responding", unmounting the iframe. connect-src must therefore
        admit the SAME loopback origins frame-src does."""
        for with_instances in (False, True):
            resp = _make_response()
            _apply_security_headers(resp, _make_app(with_instances=with_instances))
            csp = resp.headers["Content-Security-Policy"]
            connect_src = next(d for d in csp.split(";") if d.strip().startswith("connect-src"))
            frame_src = next(d for d in csp.split(";") if d.strip().startswith("frame-src"))
            # Every loopback origin the panel can frame, it can also probe.
            for origin in (
                "http://127.0.0.1:*",
                "http://localhost:*",
                "http://0.0.0.0:*",
                "https://127.0.0.1:*",
                "https://localhost:*",
            ):
                assert origin in frame_src, (origin, frame_src)
                assert origin in connect_src, (origin, connect_src)
            # IPv6 loopback wildcard is NOT admitted in either directive — the
            # bracketed-literal-plus-wildcard-port form is invalid CSP grammar
            # (see test_csp_no_ipv6_wildcard_source).
            assert "http://[::1]:*" not in frame_src
            assert "http://[::1]:*" not in connect_src
            # Pre-existing WebSocket loopback grants stay.
            assert "ws://localhost:*" in connect_src
            assert "ws://127.0.0.1:*" in connect_src
            # The probe is loopback-only: no bare wildcard, no public egress,
            # and the *.localhost tunnel wildcard stays frame-src/instances-only.
            assert "http://*.localhost:*" not in connect_src
            assert "https://*" + " " not in connect_src
            assert "*.cloudfront.net" not in connect_src

    def test_csp_no_ipv6_wildcard_source(self) -> None:
        """Regression: Chromium rejects a CSP host-source that pairs a bracketed
        IPv6 literal with a wildcard port (``http://[::1]:*``) — it is invalid
        grammar, so the browser drops the WHOLE source and logs
        "contains an invalid source". The pet page surfaced exactly that on
        connect-src/frame-src. No directive in either dashboard mode may contain
        a bracketed IPv6 host immediately followed by ``:*``."""
        import re

        # A bracketed IPv6 host (any hex/colon run) directly followed by ``:*``.
        ipv6_wildcard = re.compile(r"\[[0-9A-Fa-f:]+\]:\*")
        for with_instances in (False, True):
            resp = _make_response()
            _apply_security_headers(resp, _make_app(with_instances=with_instances))
            csp = resp.headers["Content-Security-Policy"]
            hit = ipv6_wildcard.search(csp)
            assert hit is None, (hit.group(0) if hit else None, csp)

    def test_csp_frame_src_allows_cloudfront_previews(self) -> None:
        """Webapp artifact live previews iframe the deployed CloudFront site
        (WebAppArtifactCard / WebAppThumb): https-only wildcard, present in
        BOTH modes, and never a bare scheme wildcard."""
        for with_instances in (False, True):
            resp = _make_response()
            _apply_security_headers(resp, _make_app(with_instances=with_instances))
            csp = resp.headers["Content-Security-Policy"]
            frame_src = next(d for d in csp.split(";") if d.strip().startswith("frame-src"))
            assert "https://*.cloudfront.net" in frame_src
            assert "http://*.cloudfront.net" not in frame_src
            assert "https://*" + " " not in frame_src  # no bare https wildcard

    def test_csp_admits_the_webfonts_index_html_actually_loads(self) -> None:
        """The dashboard's two brand faces come from Google Fonts, so the
        stylesheet origin must be in style-src and the font origin in font-src.

        Without them the stylesheet is refused and BOTH families fall through
        the stack. macOS lands on ``-apple-system`` and still looks deliberate;
        Windows has no such entry and drops to the generic sans-serif, so the
        whole UI renders in a face the design never targeted."""
        for with_instances in (False, True):
            resp = _make_response()
            _apply_security_headers(resp, _make_app(with_instances=with_instances))
            csp = resp.headers["Content-Security-Policy"]
            style_sources = set(
                next(d for d in csp.split(";") if d.strip().startswith("style-src")).split()
            )
            font_sources = set(
                next(d for d in csp.split(";") if d.strip().startswith("font-src")).split()
            )
            # Subset comparison over the directive's token SET (not `<url> in
            # <string>`) — exact, and with no substring `in` on a URL literal it
            # cannot trip CodeQL's incomplete-url-substring-sanitization
            # heuristic (see test_dashboard_origin.py's same workaround).
            assert {"https://fonts.googleapis.com"} <= style_sources, style_sources
            assert {"https://fonts.gstatic.com"} <= font_sources, font_sources

    def test_csp_covers_every_remote_font_origin_in_index_html(self) -> None:
        """Parity gate: any remote origin ``website/index.html`` fetches a font
        or font stylesheet from must be admitted, so adding a face to the HTML
        without widening the CSP cannot silently ship a blocked font."""
        import re
        from pathlib import Path

        index_html = Path(__file__).resolve().parents[1] / "website" / "index.html"
        if not index_html.is_file():
            # python-only checkout (sdist/wheel test run): nothing to compare.
            return
        html = index_html.read_text(encoding="utf-8")
        origins = {
            f"{m.group(1)}//{m.group(2)}"
            for m in re.finditer(r"(https:)//([A-Za-z0-9.-]*fonts[A-Za-z0-9.-]*)", html)
        }
        assert origins, "expected index.html to reference at least one font origin"
        resp = _make_response()
        _apply_security_headers(resp, _make_app(with_instances=False))
        csp = resp.headers["Content-Security-Policy"]
        # Subset comparison over whole CSP source tokens (not `origin in csp`) —
        # every font origin index.html references must be its own admitted
        # source. Set arithmetic keeps URL literals off the left of `in`, which
        # CodeQL's incomplete-url-substring-sanitization heuristic would flag.
        csp_tokens = set(csp.replace(";", " ").split())
        missing = origins - csp_tokens
        assert not missing, (missing, csp)

    def test_defense_in_depth_headers_present(self) -> None:
        """(CWE-1021/693/200/319): by default the pipeline sets
        clickjacking / MIME-sniffing / referrer / HSTS headers + CSP
        frame-ancestors 'self'. With no embed parent token, the posture is
        unchanged: bare 'self' + X-Frame-Options."""
        resp = _make_response()
        _apply_security_headers(resp, _make_app(with_instances=False))
        assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert "max-age=31536000" in resp.headers["Strict-Transport-Security"]
        assert "frame-ancestors 'self'" in resp.headers["Content-Security-Policy"]

    def test_frame_ancestors_trusts_token_embed_parent(self, monkeypatch) -> None:
        """When the request's signed token carries an embed_parent_port claim (the
        parent desktop app's port, minted at connect), frame-ancestors lists that
        EXACT loopback origin (all loopback hosts at that port) so the cross-port
        multi-instance embed renders. Never a wildcard, never a hardcoded port;
        X-Frame-Options is omitted so SAMEORIGIN can't refuse the cross-port
        embed."""
        # Focused header-logic test: stub the (separately unit-tested) signed
        # claim reader so we don't re-mint a real token here.
        monkeypatch.setattr(
            "kiro_crew.dashboard.server.token_embed_parent_port",
            lambda token: 5476 if token else None,
        )
        request = make_mocked_request("GET", "/?token=deadbeef")
        resp = _make_response()
        _apply_security_headers(resp, _make_app(with_instances=True), request=request)
        csp = resp.headers["Content-Security-Policy"]
        frame_anc = next(d for d in csp.split(";") if d.strip().startswith("frame-ancestors"))
        assert "'self'" in frame_anc
        assert "http://localhost:5476" in frame_anc
        assert "http://127.0.0.1:5476" in frame_anc
        assert "*" not in frame_anc  # exact origins only, no wildcard
        # X-Frame-Options omitted so it cannot contradict the cross-port allowlist
        assert "X-Frame-Options" not in resp.headers

    def test_frame_ancestors_reads_embed_parent_from_session_cookie(self, monkeypatch) -> None:
        """PR #118 follow-up: the framed document authenticates via the
        ``mc_token_<port>`` session cookie, NOT a ``?token=`` query param
        (token_auth_middleware exchanges the connect link token for that cookie).
        The reader MUST consult the cookie — otherwise every steady-state framed
        load falls back to bare frame-ancestors 'self' and the embedded pane
        never renders (the exact blank-pane bug). Reproduced live: a cookie
        carrying the claim previously yielded 'self'."""
        from kiro_crew.dashboard.state import _DEFAULT_PORT

        # Stub the (separately unit-tested) signed-claim reader; the point of
        # THIS test is that the cookie value reaches it (no query token present).
        seen: dict[str, str] = {}

        def _fake_reader(token: str) -> int | None:
            seen["token"] = token
            return 5476 if token else None

        monkeypatch.setattr("kiro_crew.dashboard.server.token_embed_parent_port", _fake_reader)
        request = make_mocked_request(
            "GET",
            "/",
            headers={"Cookie": f"mc_token_{_DEFAULT_PORT}=sessioncookietoken"},
        )
        resp = _make_response()
        _apply_security_headers(resp, _make_app(with_instances=True), request=request)
        # The cookie value (not an empty query token) reached the claim reader.
        assert seen["token"] == "sessioncookietoken"
        csp = resp.headers["Content-Security-Policy"]
        frame_anc = next(d for d in csp.split(";") if d.strip().startswith("frame-ancestors"))
        assert "http://localhost:5476" in frame_anc
        assert "http://127.0.0.1:5476" in frame_anc
        assert "X-Frame-Options" not in resp.headers

    def test_frame_ancestors_prefers_request_stashed_claim(self, monkeypatch) -> None:
        """PR #129 follow-up: on the FIRST ``?token=`` framed document the
        link→session exchange revokes the link nonce, so re-validating the query
        token here returns None and the header would fall back to bare 'self'
        (the browser enforces THIS response's frame-ancestors → blank pane).
        token_auth_middleware stashes the validated parent port on the request
        BEFORE revoking; this reader must prefer it. Simulate: request carries the
        stashed claim but NO usable query token/cookie (reader returns None)."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.server.token_embed_parent_port", lambda token: None
        )
        request = make_mocked_request("GET", "/?token=revoked-link-token")
        request["embed_parent_port"] = "5476"  # what the middleware stashed
        resp = _make_response()
        _apply_security_headers(resp, _make_app(with_instances=True), request=request)
        csp = resp.headers["Content-Security-Policy"]
        frame_anc = next(d for d in csp.split(";") if d.strip().startswith("frame-ancestors"))
        assert "http://localhost:5476" in frame_anc
        assert "http://127.0.0.1:5476" in frame_anc
        assert "X-Frame-Options" not in resp.headers

    def test_frame_ancestors_default_without_embed_token(self, monkeypatch) -> None:
        """A request with no valid embed-parent token (or none at all) keeps the
        default posture: frame-ancestors 'self' + X-Frame-Options: SAMEORIGIN.
        A random local page has no signed token, so it can never inject an
        ancestor (CSE SEC-016 clickjacking)."""
        # Real reader returns None for a request with no token → default posture.
        request = make_mocked_request("GET", "/")
        resp = _make_response()
        _apply_security_headers(resp, _make_app(with_instances=True), request=request)
        csp = resp.headers["Content-Security-Policy"]
        assert "frame-ancestors 'self'" in csp
        assert "localhost:3000" not in csp
        assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"

    def test_shell_csp_ancestors_go_through_the_same_validator(self, monkeypatch) -> None:
        """The dashboard shell's ``frame-ancestors`` is built by
        ``origin.frame_ancestors_value``, the same joiner the sandboxed-document
        responses use — not a local ``" ".join``.

        This is the third ancestor-emitting site. While it hand-joined, it was the
        one source nothing validated, so the helper's guarantee that no future
        ancestor can reintroduce an inexpressible entry did not actually hold for
        the shell. That class of bug is not hypothetical: a bracketed IPv6 literal
        WAS being emitted, and engines refuse the whole source expression when they
        see one, dropping every ancestor with it.

        Asserted by behaviour rather than by reading the source: an inexpressible
        ancestor must not reach the header even when ``_extra_frame_ancestors``
        offers one.
        """
        monkeypatch.setattr(
            "kiro_crew.dashboard.server._extra_frame_ancestors",
            lambda request, app: ["http://[::1]:5476", "http://localhost:5476"],
        )
        request = make_mocked_request("GET", "/")
        resp = _make_response()
        _apply_security_headers(resp, _make_app(with_instances=True), request=request)
        csp = resp.headers["Content-Security-Policy"]
        frame_anc = next(d for d in csp.split(";") if d.strip().startswith("frame-ancestors"))
        assert "[::1]" not in frame_anc
        # The expressible sibling still gets through, so this is a filter and not
        # a blanket refusal.
        assert "http://localhost:5476" in frame_anc
        assert "'self'" in frame_anc

    def test_frame_ancestors_ignores_forged_unsigned_cookie(self) -> None:
        """A forged/unsigned ``mc_token_<port>`` cookie must NOT get its parent
        origin into frame-ancestors. Unlike the positive tests above, this test
        does NOT monkeypatch the signed-claim reader: the real verifier
        (``token_embed_parent_port``) rejects the unsigned JWT and returns None,
        so ``_extra_frame_ancestors`` adds nothing and the posture stays the
        default bare frame-ancestors 'self' + X-Frame-Options: SAMEORIGIN. A
        local page that plants an attacker cookie can therefore never inject an
        ancestor origin (clickjacking, CSE SEC-016 / CWE-778)."""
        from kiro_crew.dashboard.state import _DEFAULT_PORT

        # Real reader (no monkeypatch): the cookie value reaches it and is
        # rejected because it carries no valid signature.
        request = make_mocked_request(
            "GET",
            "/",
            headers={"Cookie": f"mc_token_{_DEFAULT_PORT}=forged.unsigned.jwt"},
        )
        resp = _make_response()
        _apply_security_headers(resp, _make_app(with_instances=True), request=request)
        csp = resp.headers["Content-Security-Policy"]
        frame_anc = next(d for d in csp.split(";") if d.strip().startswith("frame-ancestors"))
        assert "frame-ancestors 'self'" in csp
        # The forged cookie's parent origin was never appended.
        assert "http://localhost:" not in frame_anc
        assert "http://127.0.0.1:" not in frame_anc
        # The legacy clickjacking backstop stays in place in the default posture.
        assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"

    def test_csp_extends_frame_src_when_instances_enabled(self) -> None:
        resp = _make_response()
        _apply_security_headers(resp, _make_app(with_instances=True))
        csp = resp.headers["Content-Security-Policy"]
        # Loopback wildcards enable dynamically-connected tunnel port
        # iframes for the instances feature.
        assert "http://127.0.0.1:*" in csp
        assert "http://localhost:*" in csp
        assert "http://*.localhost:*" in csp

    def test_setdefault_semantics_do_not_override_handler_headers(self) -> None:
        resp = _make_response()
        # Simulate a handler that already set a header
        resp.headers["Cache-Control"] = "public, max-age=3600"
        _apply_security_headers(resp, _make_app())
        # Handler value preserved
        assert resp.headers["Cache-Control"] == "public, max-age=3600"
        # Other headers still applied
        assert "Permissions-Policy" in resp.headers

    def test_app_without_state_still_gets_headers(self) -> None:
        """Auth failure paths return responses on apps without state; the
        middleware must not raise on them."""
        resp = _make_response()
        app = web.Application()  # no state key
        _apply_security_headers(resp, app)
        assert "Permissions-Policy" in resp.headers
        assert "Content-Security-Policy" in resp.headers
