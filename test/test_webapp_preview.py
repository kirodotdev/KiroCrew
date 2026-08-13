"""Tests for the webapp-artifact local preview channel (PR #61).

Mirrors ``test_artifact_folder_handlers.py``: MagicMock requests + a real
:class:`ArtifactStore` rooted at a tmp dir, with the allow-listed local roots
monkeypatched to a tmp workspace.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew import artifacts as art_mod
from kiro_crew.artifacts import ArtifactStore
from kiro_crew.dashboard.handlers import webapp_preview as wp
from kiro_crew.deploy.webapp_types import webapp_metadata_from_dict


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """Isolated store + an allow-listed workspace containing one app tree."""
    store = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(art_mod, "_default_store", store)
    ws = tmp_path / "workspace"
    app_dir = ws / "terrace"
    (app_dir / "public" / "css").mkdir(parents=True)
    (app_dir / "public" / "index.html").write_text("<html>terrace</html>")
    (app_dir / "public" / "css" / "app.css").write_text("body{}")
    (app_dir / "public" / ".kirocrew-deploy.json").write_text("{}")
    (app_dir / "api" / "secret").mkdir(parents=True)
    (app_dir / "api" / "secret" / "keys.py").write_text("SECRET = 1")
    monkeypatch.setattr(wp, "_allowed_local_roots", lambda: [ws.resolve()])
    return store, app_dir


def _mk_webapp(store: ArtifactStore, app_dir: Path, slug: str = "terrace-app"):
    meta = webapp_metadata_from_dict({
        "slug": "terrace",
        "app_dir": str(app_dir),
        "deploy_target": {"public_url": "https://d1.cloudfront.net/terrace/"},
        "lifecycle": {"status": "live"},
    })
    return store.create(slug=slug, name="Terrace", kind="webapp",
                        content="app", webapp_metadata=meta)


def _req(match: dict, remote: str = "127.0.0.1") -> MagicMock:
    req = MagicMock()
    req.match_info = match
    req.headers = {}
    req.remote = remote
    req.app = {"state": MagicMock()}
    return req


def _body(resp) -> dict:
    return json.loads(resp.body)


class TestPreviewMint:
    @pytest.mark.asyncio
    async def test_mints_base_for_valid_webapp(self, env) -> None:
        store, app_dir = env
        _mk_webapp(store, app_dir)
        resp = await wp.api_artifact_app_preview(_req({"slug": "terrace-app"}))
        body = _body(resp)
        assert body["available"] is True
        assert body["base"].startswith("/artifact-app/terrace-app/")
        assert body["base"].endswith("/")

    @pytest.mark.asyncio
    async def test_invalid_slug_400(self, env) -> None:
        resp = await wp.api_artifact_app_preview(_req({"slug": "../etc"}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_missing_artifact_unavailable(self, env) -> None:
        resp = await wp.api_artifact_app_preview(_req({"slug": "nope"}))
        assert _body(resp)["available"] is False

    @pytest.mark.asyncio
    async def test_non_webapp_kind_unavailable(self, env) -> None:
        store, _ = env
        store.create(slug="doc", name="Doc", kind="markdown", content="# hi")
        resp = await wp.api_artifact_app_preview(_req({"slug": "doc"}))
        assert _body(resp)["available"] is False

    @pytest.mark.asyncio
    async def test_app_dir_outside_allowed_roots_unavailable(self, env, tmp_path) -> None:
        store, _ = env
        outside = tmp_path / "outside"
        (outside / "public").mkdir(parents=True)
        (outside / "public" / "index.html").write_text("x")
        _mk_webapp(store, outside, slug="evil-app")
        resp = await wp.api_artifact_app_preview(_req({"slug": "evil-app"}))
        assert _body(resp)["available"] is False

    @pytest.mark.asyncio
    async def test_empty_app_dir_unavailable(self, env) -> None:
        store, _ = env
        meta = webapp_metadata_from_dict({"slug": "x", "lifecycle": {"status": "live"}})
        store.create(slug="noapp", name="X", kind="webapp", content="a",
                     webapp_metadata=meta)
        resp = await wp.api_artifact_app_preview(_req({"slug": "noapp"}))
        assert _body(resp)["available"] is False

    @pytest.mark.asyncio
    async def test_proxied_request_denied_mint(self, env) -> None:
        store, app_dir = env
        _mk_webapp(store, app_dir)
        req = _req({"slug": "terrace-app"})
        req.headers = {"X-Forwarded-For": "203.0.113.7"}
        resp = await wp.api_artifact_app_preview(req)
        assert _body(resp)["available"] is False


def _mint(store, app_dir, slug="terrace-app", client="127.0.0.1") -> str:
    """Create the artifact and return a valid token for its web root."""
    _mk_webapp(store, app_dir, slug=slug)
    webroot = wp._resolve_webroot(slug)
    assert webroot is not None
    return wp._make_token(slug, str(webroot), client=client)


class TestPreviewServe:
    @pytest.mark.asyncio
    async def test_serves_index_and_subresource(self, env) -> None:
        store, app_dir = env
        token = _mint(store, app_dir)
        resp = await wp.serve_artifact_app_file(
            _req({"slug": "terrace-app", "token": token, "path": ""}))
        assert resp.status == 200
        assert b"terrace" in resp.body
        # Opaque-origin enforcement travels with every response.
        assert "sandbox allow-scripts" in resp.headers["Content-Security-Policy"]
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        css = await wp.serve_artifact_app_file(
            _req({"slug": "terrace-app", "token": token, "path": "css/app.css"}))
        assert css.status == 200 and b"body{}" in css.body

    @pytest.mark.asyncio
    async def test_traversal_and_backend_source_blocked(self, env) -> None:
        store, app_dir = env
        token = _mint(store, app_dir)
        for path in ("../api/secret/keys.py", "..%2fapi/keys.py", "css/../../api/secret/keys.py"):
            with pytest.raises(web.HTTPNotFound):
                await wp.serve_artifact_app_file(
                    _req({"slug": "terrace-app", "token": token, "path": path}))

    @pytest.mark.asyncio
    async def test_dotfiles_never_served(self, env) -> None:
        store, app_dir = env
        token = _mint(store, app_dir)
        with pytest.raises(web.HTTPNotFound):
            await wp.serve_artifact_app_file(
                _req({"slug": "terrace-app", "token": token,
                      "path": ".kirocrew-deploy.json"}))

    @pytest.mark.asyncio
    async def test_symlink_escape_blocked(self, env, tmp_path) -> None:
        store, app_dir = env
        secret = tmp_path / "loot.txt"
        secret.write_text("loot")
        (app_dir / "public" / "link.txt").symlink_to(secret)
        token = _mint(store, app_dir)
        with pytest.raises(web.HTTPNotFound):
            await wp.serve_artifact_app_file(
                _req({"slug": "terrace-app", "token": token, "path": "link.txt"}))

    @pytest.mark.asyncio
    async def test_bad_and_expired_tokens_404(self, env, monkeypatch) -> None:
        store, app_dir = env
        token = _mint(store, app_dir)
        for bad in ("", "garbage", token + "x", "123.deadbeef"):
            with pytest.raises(web.HTTPNotFound):
                await wp.serve_artifact_app_file(
                    _req({"slug": "terrace-app", "token": bad, "path": ""}))
        # Expired: mint with a past exp using the module's own MAC.
        exp = int(time.time()) - 10
        webroot = wp._resolve_webroot("terrace-app")
        stale = f"{exp}.{wp._mac('terrace-app', str(webroot), exp, '127.0.0.1')}"
        with pytest.raises(web.HTTPNotFound):
            await wp.serve_artifact_app_file(
                _req({"slug": "terrace-app", "token": stale, "path": ""}))

    @pytest.mark.asyncio
    async def test_token_bound_to_slug(self, env, tmp_path) -> None:
        """A token minted for one artifact cannot serve another."""
        store, app_dir = env
        token = _mint(store, app_dir)
        other_dir = tmp_path / "workspace" / "other"
        (other_dir / "public").mkdir(parents=True)
        (other_dir / "public" / "index.html").write_text("other")
        _mk_webapp(store, other_dir, slug="other-app")
        with pytest.raises(web.HTTPNotFound):
            await wp.serve_artifact_app_file(
                _req({"slug": "other-app", "token": token, "path": ""}))


class TestRound1Fixes:
    @pytest.mark.asyncio
    async def test_public_symlink_escape_rejected(self, env, tmp_path) -> None:
        """Round-1 F1: app_dir whose public/ is a symlink outside app_dir."""
        store, _ = env
        evil_dir = tmp_path / "workspace" / "evil"
        evil_dir.mkdir(parents=True)
        loot = tmp_path / "loot"
        loot.mkdir()
        (loot / "index.html").write_text("stolen")
        (evil_dir / "public").symlink_to(loot)
        _mk_webapp(store, evil_dir, slug="evil-sym")
        resp = await wp.api_artifact_app_preview(_req({"slug": "evil-sym"}))
        assert _body(resp)["available"] is False

    @pytest.mark.asyncio
    async def test_sensitive_path_inside_webroot_rejected(self, env, monkeypatch) -> None:
        """Round-1 F1: is_sensitive_path veto applies even inside the root."""
        store, app_dir = env
        token = _mint(store, app_dir, slug="sens-app")
        monkeypatch.setattr(wp, "is_sensitive_path", lambda _p: True)
        with pytest.raises(web.HTTPNotFound):
            await wp.serve_artifact_app_file(
                _req({"slug": "sens-app", "token": token, "path": ""}))

    def test_serve_pipeline_is_threaded(self) -> None:
        """Round-1 F2: the handler body must contain no direct fs reads —
        everything goes through the blocking helper via asyncio.to_thread."""
        import inspect
        src = inspect.getsource(wp.serve_artifact_app_file)
        assert "asyncio.to_thread" in src
        assert "read_bytes" not in src
        mint_src = inspect.getsource(wp.api_artifact_app_preview)
        assert "asyncio.to_thread" in mint_src


class TestRound2Fixes:
    @pytest.mark.asyncio
    async def test_app_dir_without_public_unavailable(self, env, tmp_path) -> None:
        """Round-2 F1: no public/ → no local preview (never serve app_dir)."""
        store, _ = env
        bare = tmp_path / "workspace" / "bare"
        bare.mkdir(parents=True)
        (bare / "index.html").write_text("<html>bare</html>")
        (bare / "config.json").write_text('{"secretish": true}')
        _mk_webapp(store, bare, slug="bare-app")
        resp = await wp.api_artifact_app_preview(_req({"slug": "bare-app"}))
        assert _body(resp)["available"] is False

    @pytest.mark.asyncio
    async def test_egress_pinned_csp_and_cors(self, env) -> None:
        """Round-2 F1/F2: default-src 'self' pins egress; ACAO lets the
        opaque-origin document load its own module scripts."""
        store, app_dir = env
        token = _mint(store, app_dir, slug="hdr-app")
        resp = await wp.serve_artifact_app_file(
            _req({"slug": "hdr-app", "token": token, "path": ""}))
        csp = resp.headers["Content-Security-Policy"]
        assert "sandbox allow-scripts" in csp
        assert "default-src 'self'" in csp
        assert "https:" not in csp  # no wildcard egress
        # Round-4 F1: ACAO is scoped by content type — the HTML document
        # itself must NOT be cross-origin readable.
        assert "Access-Control-Allow-Origin" not in resp.headers

    @pytest.mark.asyncio
    async def test_unsupported_platform_reports_unavailable(self, env, monkeypatch) -> None:
        """Round-2 F3: on platforms without fd-realpath support the mint
        endpoint must say unavailable (so the FE uses the remote fallback)
        and the serve route must 404."""
        store, app_dir = env
        token = _mint(store, app_dir, slug="plat-app")
        monkeypatch.setattr(wp, "_PLATFORM_SUPPORTED", False)
        resp = await wp.api_artifact_app_preview(_req({"slug": "plat-app"}))
        assert _body(resp)["available"] is False
        with pytest.raises(web.HTTPNotFound):
            await wp.serve_artifact_app_file(
                _req({"slug": "plat-app", "token": token, "path": ""}))


class TestRound3Fixes:
    @pytest.mark.asyncio
    async def test_nul_app_dir_fails_closed_not_500(self, env) -> None:
        """Round-3 F1: legacy metadata with an embedded NUL must 404/unavailable."""
        store, _ = env
        meta = webapp_metadata_from_dict({
            "slug": "x", "app_dir": "/tmp/\x00evil",
            "lifecycle": {"status": "live"},
        })
        store.create(slug="nul-app", name="X", kind="webapp", content="a",
                     webapp_metadata=meta)
        resp = await wp.api_artifact_app_preview(_req({"slug": "nul-app"}))
        assert _body(resp)["available"] is False

    @pytest.mark.asyncio
    async def test_huge_token_expiry_404_not_500(self, env) -> None:
        """Round-3 F2: a 5000-digit expiry passes isdigit but must never 500."""
        store, app_dir = env
        _mk_webapp(store, app_dir, slug="tok-app")
        huge = ("9" * 5000) + ".deadbeef"
        with pytest.raises(web.HTTPNotFound):
            await wp.serve_artifact_app_file(
                _req({"slug": "tok-app", "token": huge, "path": ""}))

    def test_write_time_app_dir_validation(self) -> None:
        """Round-3 F1: validation.py rejects control chars and relative paths."""
        from kiro_crew.validation import ValidationError, _validate_webapp_metadata_shape
        _validate_webapp_metadata_shape({"app_dir": "/abs/path"})
        _validate_webapp_metadata_shape({"app_dir": "~/workspace/app"})
        _validate_webapp_metadata_shape({"app_dir": ""})
        for bad in ("/tmp/\x00evil", "relative/path", "a" * 5000, "/tmp/x\n"):
            with pytest.raises(ValidationError):
                _validate_webapp_metadata_shape({"app_dir": bad})


class TestRound4Fixes:
    @pytest.mark.asyncio
    async def test_cors_scoped_to_executable_types(self, env) -> None:
        """Round-4 F1: module scripts need CORS from the opaque origin, but
        data-bearing files (json/html) must stay opaque to cross-origin
        readers — otherwise a malicious app script can fetch() sibling files
        and exfiltrate their contents via self-navigation."""
        store, app_dir = env
        pub = app_dir / "public"
        (pub / "bundle.js").write_text("export const x = 1")
        (pub / "config.json").write_text('{"secretish": true}')
        token = _mint(store, app_dir, slug="cors-app")
        js = await wp.serve_artifact_app_file(
            _req({"slug": "cors-app", "token": token, "path": "bundle.js"}))
        assert js.headers.get("Access-Control-Allow-Origin") == "*"
        for path in ("config.json", "", "index.html"):
            resp = await wp.serve_artifact_app_file(
                _req({"slug": "cors-app", "token": token, "path": path}))
            assert "Access-Control-Allow-Origin" not in resp.headers, path


class TestRound5Fixes:
    @pytest.mark.asyncio
    async def test_token_bound_to_client(self, env) -> None:
        """Round-5 F1: a token exfiltrated by previewed script must be
        useless from any other client address."""
        store, app_dir = env
        token = _mint(store, app_dir, slug="bind-app", client="127.0.0.1")
        ok = await wp.serve_artifact_app_file(
            _req({"slug": "bind-app", "token": token, "path": ""}, remote="127.0.0.1"))
        assert ok.status == 200
        with pytest.raises(web.HTTPNotFound):
            await wp.serve_artifact_app_file(
                _req({"slug": "bind-app", "token": token, "path": ""},
                     remote="203.0.113.7"))

    @pytest.mark.asyncio
    async def test_credential_bearing_file_rejected(self, env) -> None:
        """Round-5 F2: agent-influenced files that trip the mandatory
        redaction scan are rejected, never served (and never mangled)."""
        store, app_dir = env
        pub = app_dir / "public"
        (pub / "leak.js").write_text(
            'const k = "AKIAIOSFODNN7EXAMPLE";\n'
            'const s = "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY";')
        token = _mint(store, app_dir, slug="redact-app")
        with pytest.raises(web.HTTPNotFound):
            await wp.serve_artifact_app_file(
                _req({"slug": "redact-app", "token": token, "path": "leak.js"}))
        clean = await wp.serve_artifact_app_file(
            _req({"slug": "redact-app", "token": token, "path": ""}))
        assert clean.status == 200

    @pytest.mark.asyncio
    async def test_serve_decisions_are_sel_audited(self, env, monkeypatch) -> None:
        """Round-5 F3: allow and deny outcomes both emit sanitized SEL
        events; the token never appears in the record."""
        store, app_dir = env
        events: list[dict] = []
        monkeypatch.setattr(
            wp, "_audit",
            lambda op, outcome, res: events.append(
                {"op": op, "outcome": outcome, "res": res}))
        token = _mint(store, app_dir, slug="audit-app")
        await wp.serve_artifact_app_file(
            _req({"slug": "audit-app", "token": token, "path": ""}))
        with pytest.raises(web.HTTPNotFound):
            await wp.serve_artifact_app_file(
                _req({"slug": "audit-app", "token": "0.bogus", "path": ""}))
        outcomes = {(e["op"], e["outcome"]) for e in events}
        assert ("webapp_preview.serve", "allowed") in outcomes
        assert ("webapp_preview.serve", "denied") in outcomes
        assert all(token not in e["res"] for e in events)


class TestRound6Fixes:
    @pytest.mark.asyncio
    async def test_extensionless_credential_file_rejected(self, env) -> None:
        """Round-6 F1: the redaction scan must not be gated on the GUESSED
        content type — credential text in an extensionless / .bin file
        (octet-stream) is scanned and rejected all the same."""
        store, app_dir = env
        pub = app_dir / "public"
        (pub / "config").write_text('AccessKeyId=AKIAIOSFODNN7EXAMPLE')
        (pub / "blob.bin").write_bytes(b'prefix AKIAIOSFODNN7EXAMPLE suffix')
        token = _mint(store, app_dir, slug="octet-app")
        for path in ("config", "blob.bin"):
            with pytest.raises(web.HTTPNotFound):
                await wp.serve_artifact_app_file(
                    _req({"slug": "octet-app", "token": token, "path": path}))

    @pytest.mark.asyncio
    async def test_clean_binary_still_serves(self, env) -> None:
        """Round-6 F1 counterpart: scanning every body must not break
        ordinary binary assets."""
        store, app_dir = env
        pub = app_dir / "public"
        (pub / "img.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8)
        token = _mint(store, app_dir, slug="bin-app")
        resp = await wp.serve_artifact_app_file(
            _req({"slug": "bin-app", "token": token, "path": "img.png"}))
        assert resp.status == 200

    def test_windows_absolute_app_dir_accepted(self) -> None:
        """Round-6 F2: native Windows absolute paths pass write-time
        validation (preview later degrades via the platform gate);
        relative paths still fail."""
        from kiro_crew.validation import (
            ValidationError,
            _validate_webapp_metadata_shape,
        )
        _validate_webapp_metadata_shape({"app_dir": "C:\\work\\app"})
        with pytest.raises(ValidationError):
            _validate_webapp_metadata_shape({"app_dir": "work/app"})


class TestRound7Fixes:
    @pytest.mark.asyncio
    async def test_uppercase_slug_404_not_500(self, env) -> None:
        """Round-7 F1: a slug the store grammar rejects (uppercase) must be
        stopped by the route regex — never reach store.get() and 500."""
        store, app_dir = env
        token = _mint(store, app_dir, slug="case-app")
        with pytest.raises(web.HTTPNotFound):
            await wp.serve_artifact_app_file(
                _req({"slug": "CASE-app", "token": token, "path": ""}))
        resp = await wp.api_artifact_app_preview(_req({"slug": "UPPER"}))
        assert resp.status == 400

    def test_resolve_webroot_swallows_store_validation_reject(self) -> None:
        """Round-7 F1 defense-in-depth: even if the gates drift again, a
        store-side ArtifactValidationError fails closed (None), never
        propagates."""
        assert wp._resolve_webroot("UPPER_not_valid") is None

    def test_probe_url_allowlist(self) -> None:
        """Round-7 F2 SSRF gate: only https on exact *.cloudfront.net is
        ever probed."""
        ok = "https://d2nzmpzyp0popu.cloudfront.net/site/"
        assert wp._probe_url_allowed(ok)
        for bad in (
            "http://d2nzmpzyp0popu.cloudfront.net/site/",
            "https://evil.cloudfront.net.attacker.example/",
            "https://user:pw@d2nzmpzyp0popu.cloudfront.net/",
            "https://169.254.169.254/latest/meta-data/",
            "",
        ):
            assert not wp._probe_url_allowed(bad), bad

    @pytest.mark.asyncio
    async def test_remote_framable_header_logic(self, monkeypatch) -> None:
        """Round-7 F2: XFO present -> not framable; clean headers ->
        framable; probe failure -> not framable (hero, never blank)."""
        wp._FRAMABLE_CACHE.clear()

        class _Resp:
            def __init__(self, headers):
                self.headers = headers

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _Session:
            def __init__(self, headers=None, raise_exc=False):
                self._h = headers
                self._raise = raise_exc

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def head(self, url, allow_redirects=False):
                if self._raise:
                    raise OSError("boom")
                return _Resp(self._h)

        url = "https://dtest0000000.cloudfront.net/x/"
        monkeypatch.setattr(
            wp.aiohttp, "ClientSession",
            lambda **kw: _Session({"X-Frame-Options": "SAMEORIGIN"}))
        assert await wp._remote_framable(url) is False
        wp._FRAMABLE_CACHE.clear()
        monkeypatch.setattr(
            wp.aiohttp, "ClientSession",
            lambda **kw: _Session({
                "Content-Security-Policy":
                    "default-src 'self'; frame-ancestors 'self' http://localhost:*"}))
        assert await wp._remote_framable(url) is True
        wp._FRAMABLE_CACHE.clear()
        monkeypatch.setattr(
            wp.aiohttp, "ClientSession", lambda **kw: _Session(raise_exc=True))
        assert await wp._remote_framable(url) is False
        wp._FRAMABLE_CACHE.clear()


class TestAppDirPlumbing:
    def test_from_dict_parses_and_caps_app_dir(self) -> None:
        meta = webapp_metadata_from_dict({"app_dir": "/tmp/x"})
        assert meta is not None and meta.app_dir == "/tmp/x"
        meta2 = webapp_metadata_from_dict({"app_dir": "a" * 9000})
        assert meta2 is not None and len(meta2.app_dir) == 4096
        meta3 = webapp_metadata_from_dict({})
        assert meta3 is not None and meta3.app_dir == ""

    def test_app_dir_survives_store_roundtrip(self, env) -> None:
        store, app_dir = env
        _mk_webapp(store, app_dir)
        art = store.get("terrace-app")
        assert art.webapp_metadata is not None
        assert art.webapp_metadata.app_dir == str(app_dir)
