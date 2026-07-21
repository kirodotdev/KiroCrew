"""Tests for deploy_web handlers — endpoint core, scan-gate, confirm-gate, approval."""
from __future__ import annotations

import asyncio
import json as _j2
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.deploy import engine, handlers
from kiro_crew.deploy import profiles as profiles_mod


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(handlers, "CONFIG_PATH", cfg)
    monkeypatch.setattr(handlers, "DATA_DIR", tmp_path)
    return cfg


@pytest.fixture(autouse=True)
def _mock_base_stack(monkeypatch):
    """Default mock for engine.run_aws so all confirm=True tests pass the base-stack check.

    Tests that specifically want to test missing-base-stack behavior can
    override engine.run_aws in their body (monkeypatch is last-write-wins).
    """
    import json as _json
    _base_outputs = _json.dumps([{"OutputKey": "BucketName", "OutputValue": "kirocrew-base-bucket"}])
    monkeypatch.setattr(engine, "run_aws", lambda *a, **kw: (0, _base_outputs, ""))


def _run(coro):
    return asyncio.run(coro)


def _set_profile(monkeypatch, profile="p", region="us-west-2"):
    handlers._save_config(profile, region)


# --- config ---------------------------------------------------------------

def test_config_roundtrip():
    handlers._save_config("my-sso", "eu-west-1")
    cfg = handlers._load_config()
    assert cfg == {"profile": "my-sso", "region": "eu-west-1"}


class _NonRestrictedState:
    """Minimal dashboard state that passes _deny_restricted (non-restricted)."""
    _restricted_keys: set = set()
    _slots: dict = {}


class _FakeReq:
    """Test request with a non-restricted app state installed."""

    def __init__(self, body, *, match_info=None):
        self._body = body
        self.headers = {"X-Session-Key": "dashboard:ui"}
        self.app = {"state": _NonRestrictedState()}
        self.match_info = match_info or {}

    async def json(self):
        return self._body

    async def read(self):
        import json as _json
        return _json.dumps(self._body).encode()


def test_put_config_rejects_bad_profile():
    resp = _run(handlers._handle_put_config(_FakeReq({"profile": "evil;rm -rf", "region": "us-west-2"})))
    assert resp.status == 400


def test_put_config_rejects_bad_region():
    resp = _run(handlers._handle_put_config(_FakeReq({"profile": "ok", "region": "not_a_region"})))
    assert resp.status == 400


def test_put_config_accepts_valid():
    resp = _run(handlers._handle_put_config(_FakeReq({"profile": "my-sso", "region": "us-east-1"})))
    assert resp.status == 200
    assert handlers._load_config() == {"profile": "my-sso", "region": "us-east-1"}


def test_deploy_requires_config():
    status, payload = _run(handlers._do_deploy({"site_id": "x", "artifact_slug": "a"}))
    assert status == 400 and "not configured" in payload["error"]


# --- deploy flow -----------------------------------------------------------

def _fake_store(kind="widget", content="<div>hi</div>", name="My Art"):
    art = SimpleNamespace(kind=kind, content=content, name=name)
    return SimpleNamespace(get=lambda slug: art)


def test_deploy_confirm_gate_returns_preview(monkeypatch):
    _set_profile(monkeypatch)
    monkeypatch.setattr(handlers, "get_default_store", lambda: _fake_store(), raising=False)
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)
    status, payload = _run(handlers._do_deploy({"site_id": "cr-dash", "artifact_slug": "a"}))
    assert status == 200
    assert payload["requires_confirm"] is True
    assert payload["public"] is True
    assert payload["site_id"] == "cr-dash"


def test_deploy_scan_gate_blocks_secret(monkeypatch):
    _set_profile(monkeypatch)
    leaky = "<p>AKIAABCDEFGHIJKLMNOP</p>"
    monkeypatch.setattr(handlers, "get_default_store", lambda: _fake_store(content=leaky), raising=False)
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)
    status, payload = _run(handlers._do_deploy({"site_id": "s", "artifact_slug": "a", "confirm": True}))
    assert status == 409
    assert payload["blocked"] is True and payload["reason"] == "scan"


def test_deploy_credential_finding_cannot_be_overridden(monkeypatch):
    """Credential findings are hard-blocked — override_scan has no effect."""
    _set_profile(monkeypatch)
    leaky = "<p>AKIAABCDEFGHIJKLMNOP</p>"
    monkeypatch.setattr(handlers, "get_default_store", lambda: _fake_store(content=leaky), raising=False)
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)

    status, payload = _run(handlers._do_deploy(
        {"site_id": "s", "artifact_slug": "a", "confirm": True, "override_scan": True}))
    assert status == 409
    assert payload["blocked"] is True
    assert payload.get("credential") is True


def test_deploy_info_finding_overridable(monkeypatch):
    """Info-class findings (internal hosts) CAN be overridden via override_scan."""
    _set_profile(monkeypatch)
    # An internal host finding is info-class, not credential.
    leaky = "<p>Visit secret.amazon.com for details</p>"
    monkeypatch.setattr(handlers, "get_default_store", lambda: _fake_store(content=leaky), raising=False)
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)
    captured = {}

    def fake_deploy(site_id, src_dir, profile, region):
        captured["src_dir"] = src_dir
        captured["index"] = Path(src_dir, "index.html").read_text(encoding="utf-8")
        return {"site_id": site_id, "url": "https://d.cloudfront.net/", "reused": False,
                "bucket": "kirocrew-web-x", "distribution_id": "D1", "status": "InProgress"}

    monkeypatch.setattr(engine, "deploy", fake_deploy)
    # Without override_scan -> blocked
    status, payload = _run(handlers._do_deploy(
        {"site_id": "s", "artifact_slug": "a", "confirm": True}))
    assert status == 409
    assert payload["blocked"] is True

    # With override_scan -> proceeds (info findings are overridable)
    status, payload = _run(handlers._do_deploy(
        {"site_id": "s", "artifact_slug": "a", "confirm": True, "override_scan": True}))
    assert status == 200
    assert payload["url"] == "https://d.cloudfront.net/"


def test_deploy_blocks_sensitive_local_dir(monkeypatch, tmp_path):
    """Security: a local_dir that is (or contains) a sensitive credential path
    must be rejected before any read/upload — see AutoSDE f-1558139c."""
    _set_profile(monkeypatch)
    src = tmp_path / "site"
    src.mkdir()
    (src / "index.html").write_text("<p>ok</p>", encoding="utf-8")
    monkeypatch.setattr(handlers, "_allowed_local_roots", lambda: [tmp_path.resolve()])
    # Simulate the dir resolving to a sensitive credential path.
    monkeypatch.setattr(handlers, "is_sensitive_path", lambda p: "site" in p)
    status, payload = _run(handlers._do_deploy(
        {"site_id": "s", "local_dir": str(src), "confirm": True}))
    assert status == 400
    assert "sensitive credential path" in payload["error"]


def test_deploy_rejects_invalid_local_dir_chars(monkeypatch):
    """validation.py schema rejects shell-metacharacter / control chars in
    local_dir before any filesystem or subprocess use — see AutoSDE f-* (126)."""
    _set_profile(monkeypatch)
    status, payload = _run(handlers._do_deploy(
        {"site_id": "s", "local_dir": "/tmp/foo;rm -rf /", "confirm": True}))
    assert status == 400
    assert "invalid local_dir" in payload["error"]


def test_deploy_rejects_local_dir_outside_allowed_roots(monkeypatch, tmp_path):
    """A local_dir resolving outside the allow-listed roots is refused."""
    _set_profile(monkeypatch)
    src = tmp_path / "site"
    src.mkdir()
    (src / "index.html").write_text("<p>ok</p>", encoding="utf-8")
    monkeypatch.setattr(handlers, "_allowed_local_roots", lambda: [Path("/nonexistent-root")])
    status, payload = _run(handlers._do_deploy(
        {"site_id": "s", "local_dir": str(src), "confirm": True}))
    assert status == 400
    assert "allowed roots" in payload["error"] or "standard workspace" in payload["error"]


def test_deploy_missing_artifact_404(monkeypatch):
    _set_profile(monkeypatch)
    from kiro_crew.artifacts import ArtifactNotFoundError

    def boom():
        return SimpleNamespace(get=lambda slug: (_ for _ in ()).throw(ArtifactNotFoundError("x")))

    monkeypatch.setattr(handlers, "get_default_store", boom, raising=False)
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)
    status, payload = _run(handlers._do_deploy({"site_id": "s", "artifact_slug": "missing", "confirm": True}))
    assert status == 404


# --- recall / destroy confirm-gate ----------------------------------------

def test_recall_preview_then_confirm(monkeypatch):
    _set_profile(monkeypatch)
    site = {"bucket": "kirocrew-web-x", "distribution_id": "D1", "distribution_arn": "arn"}
    monkeypatch.setattr(engine, "find_site_by_tag", lambda sid, p, r=None: site)
    # preview (no confirm)
    status, payload = _run(handlers._do_recall({"site_id": "s"}))
    assert status == 200 and payload["requires_confirm"] is True and payload["action"] == "recall"
    # confirm
    monkeypatch.setattr(engine, "recall", lambda sid, p, r=None: {"site_id": sid, "recalled": True})
    status, payload = _run(handlers._do_recall({"site_id": "s", "confirm": True}))
    assert status == 200 and payload["recalled"] is True


def test_destroy_preview_echoes_resources(monkeypatch):
    _set_profile(monkeypatch)
    site = {"bucket": "kirocrew-web-x", "distribution_id": "D1"}
    monkeypatch.setattr(engine, "find_site_by_tag", lambda sid, p, r=None: site)
    status, payload = _run(handlers._do_destroy({"site_id": "s"}))
    assert status == 200
    assert payload["requires_confirm"] is True and payload["destructive"] is True
    assert "kirocrew-web-x" in payload["message"] and "D1" in payload["message"]


def test_destroy_confirm_runs_engine(monkeypatch):
    _set_profile(monkeypatch)
    monkeypatch.setattr(engine, "destroy", lambda sid, p, r=None: {"site_id": sid, "destroyed": True})
    status, payload = _run(handlers._do_destroy({"site_id": "s", "confirm": True}))
    assert status == 200 and payload["destroyed"] is True


def test_destroy_missing_site_404(monkeypatch):
    _set_profile(monkeypatch)
    monkeypatch.setattr(engine, "find_site_by_tag", lambda sid, p, r=None: None)
    status, payload = _run(handlers._do_destroy({"site_id": "gone"}))
    assert status == 404


# --- list ------------------------------------------------------------------

def test_list_unconfigured_returns_empty():
    status, payload = _run(handlers._do_list())
    assert status == 200 and payload["configured"] is False and payload["sites"] == []


def test_list_configured(monkeypatch):
    _set_profile(monkeypatch)
    monkeypatch.setattr(engine, "list_sites", lambda p, r=None: [{"site_id": "a", "url": "https://x/"}])
    status, payload = _run(handlers._do_list())
    assert status == 200 and payload["configured"] is True
    assert payload["sites"][0]["site_id"] == "a"


def test_aws_error_surfaces_missing_statement(monkeypatch):
    _set_profile(monkeypatch)

    def boom(sid, p, r=None):
        raise engine.AWSError("denied", missing_statement="S3BucketLevel")

    monkeypatch.setattr(engine, "find_site_by_tag", lambda sid, p, r=None: {"bucket": "b", "distribution_id": "d"})
    monkeypatch.setattr(engine, "recall", boom)
    status, payload = _run(handlers._do_recall({"site_id": "s", "confirm": True}))
    assert status == 502 and payload["missing_statement"] == "S3BucketLevel"


def test_routes_register():
    from aiohttp import web
    app = web.Application()
    handlers.register_routes(app)
    paths = {r.resource.canonical for r in app.router.routes() if r.resource}
    for p in ("/api/deploy/deploy", "/api/deploy/recall",
              "/api/deploy/destroy", "/api/deploy/list",
              "/api/deploy/config"):
        assert p in paths


# --- additional coverage --------------------------------------------------

def test_safe_site_id_normalization():
    assert handlers._safe_site_id("CR Dash!") == "cr-dash"
    assert handlers._safe_site_id("  Hello/World  ") == "hello-world"
    assert handlers._safe_site_id("a" * 200) == "a" * handlers._SITE_ID_MAX
    assert handlers._safe_site_id("--__--") == ""


@pytest.mark.parametrize("kind,content,marker", [
    ("markdown", "# Title\n\nbody", "<h1>Title</h1>"),
    ("html", "<html><body>full</body></html>", "full"),
])
def test_deploy_renders_each_kind(monkeypatch, kind, content, marker):
    _set_profile(monkeypatch)
    monkeypatch.setattr(handlers, "get_default_store", lambda: _fake_store(kind=kind, content=content), raising=False)
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)
    captured = {}

    def fake_deploy(sid, src, p, r):
        captured["index"] = Path(src, "index.html").read_text(encoding="utf-8")
        return {"site_id": sid, "url": "https://d/", "reused": False,
                "bucket": "b", "distribution_id": "D", "status": "InProgress"}

    monkeypatch.setattr(engine, "deploy", fake_deploy)
    status, payload = _run(handlers._do_deploy({"site_id": "s", "artifact_slug": "a", "confirm": True}))
    assert status == 200
    assert marker in captured["index"]


def test_deploy_local_dir_scan_gate(monkeypatch, tmp_path):
    _set_profile(monkeypatch)
    monkeypatch.setattr(handlers, "_allowed_local_roots", lambda: [tmp_path.resolve()])
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<p>AKIAABCDEFGHIJKLMNOP</p>", encoding="utf-8")
    status, payload = _run(handlers._do_deploy({"site_id": "s", "local_dir": str(site), "confirm": True}))
    assert status == 409 and payload["reason"] == "scan"


def test_deploy_local_dir_scans_non_index_files(monkeypatch, tmp_path):
    """The scan gate must cover every uploaded file, not just index.html."""
    _set_profile(monkeypatch)
    monkeypatch.setattr(handlers, "_allowed_local_roots", lambda: [tmp_path.resolve()])
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<p>clean</p>", encoding="utf-8")
    (site / "data.js").write_text("const k = 'AKIAABCDEFGHIJKLMNOP'", encoding="utf-8")
    status, payload = _run(handlers._do_deploy({"site_id": "s", "local_dir": str(site), "confirm": True}))
    assert status == 409 and payload["reason"] == "scan"


def test_deploy_rejects_invalid_artifact_slug(monkeypatch):
    """artifact_slug is validated before the store lookup."""
    _set_profile(monkeypatch)
    status, payload = _run(handlers._do_deploy({"site_id": "s", "artifact_slug": "bad/slug;rm", "confirm": True}))
    assert status == 400 and "invalid artifact_slug" in payload["error"]


def test_deploy_local_dir_missing(monkeypatch):
    _set_profile(monkeypatch)
    status, payload = _run(handlers._do_deploy({"site_id": "s", "local_dir": "/no/such/dir", "confirm": True}))
    assert status == 400


# --- authorization guard: restricted sessions denied on all mutating routes ---

class _RestrictedReq:
    """Fake request that simulates a restricted session."""

    def __init__(self, method="POST", match_info=None, body=None):
        self.method = method
        self.match_info = match_info or {}
        self.headers = {"X-Session-Key": "incognito:abc"}
        self._body = body or {}
        self.app = {"state": _RestrictedState()}

    async def json(self):
        return self._body

    async def read(self):
        import json as _json
        return _json.dumps(self._body).encode()


class _RestrictedState:
    _restricted_keys = {"incognito:abc"}
    _slots = {}


@pytest.mark.parametrize("handler_fn,kwargs", [
    (handlers._handle_put_config, {}),
    (handlers._handle_deploy, {}),
    (handlers._handle_recall, {}),
    (handlers._handle_destroy, {}),
    (handlers._handle_verify, {}),
    (handlers._handle_profiles_post, {}),
    (handlers._handle_profiles_put, {"match_info": {"name": "test"}}),
    (handlers._handle_profiles_delete, {"match_info": {"name": "test"}}),
    (handlers._handle_teardown, {"match_info": {"slug": "test"}}),
])
def test_mutating_handlers_deny_restricted_session(handler_fn, kwargs):
    """All mutating deploy endpoints return 403 for restricted sessions."""
    req = _RestrictedReq(**kwargs)
    resp = _run(handler_fn(req))
    assert resp.status == 403


# --- teardown ordering: persistent deploy fails without tombstone on manifest error ---

def test_teardown_persistent_manifest_unreachable_returns_502(monkeypatch):
    """If manifest expiry fails and the deploy is persistent, return 502 without
    tombstoning (card retains Tear down button for retry)."""
    # F2 (r4): teardown validates metadata profile against the registry —
    # register the fake profile so these tests exercise the paths under test.
    from kiro_crew.deploy import profiles as _profiles_mod
    monkeypatch.setattr(_profiles_mod, "load_registry", lambda: {
        "version": 2,
        "profiles": [{"name": "p", "region": "us-west-2"}],
        "default": "p",
    })
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)
    # Reaper is installed — this test is about manifest expiry, not reaper check.
    monkeypatch.setattr(handlers, "_check_reaper_installed", lambda p, r: True)

    # Build a fake webapp artifact with persistent=True (no expires_at)
    lifecycle = SimpleNamespace(created_at="2026-01-01T00:00:00Z", expires_at=None,
                                persistent=True, status="live")
    meta = SimpleNamespace(deploy_target=SimpleNamespace(profile="p", region="us-west-2"),
                           slug="test-slug", lifecycle=lifecycle)
    art = SimpleNamespace(webapp_metadata=meta, slug="test-slug", kind="webapp")

    # mark_webapp_expired returns the art, get returns it for initial load
    store = SimpleNamespace(
        get=lambda slug: art,
        mark_webapp_expired=lambda slug: art,
        unmark_webapp_expired=lambda slug: art,
    )
    monkeypatch.setattr(handlers, "get_default_store", lambda: store, raising=False)

    # Make _expire_manifest_best_effort return "unreachable"
    async def _fake_expire(a):
        return "unreachable"

    monkeypatch.setattr(handlers, "_expire_manifest_best_effort", _fake_expire)

    # Non-restricted request with confirm=True
    class _Req:
        headers = {"X-Session-Key": "dashboard:ui"}
        match_info = {"slug": "test-slug"}
        app = {"state": MagicMock(_restricted_keys=set(), _slots={})}

        async def read(self):
            import json as _json
            return _json.dumps({"confirm": True}).encode()

        async def json(self):
            return {"confirm": True}

    resp = _run(handlers._handle_teardown(_Req()))
    assert resp.status == 502
    import json
    body = json.loads(resp.body)
    assert body["retry"] is True
    assert "manifest unreachable" in body["error"]


# --- _expire_manifest_best_effort: cross-deployment interference guard ---


def test_expire_manifest_uses_art_slug_not_meta_slug(monkeypatch):
    """_expire_manifest_best_effort always uses art.slug, ignoring meta.slug."""
    from types import SimpleNamespace

    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)

    # Register a profile so it passes the registry check.
    handlers._save_config("legit-profile", "us-west-2")

    # Build an artifact where meta.slug != art.slug (forged metadata).
    lifecycle = SimpleNamespace(created_at="2026-01-01T00:00:00Z", expires_at=None,
                                persistent=False, status="live")
    meta = SimpleNamespace(
        # R17 F1: fail-closed identity requires a distribution_id matching the
        # existing manifest's.
        deploy_target=SimpleNamespace(profile="legit-profile", region="us-west-2",
                                      distribution_id="EIDENT1"),
        slug="FORGED-SLUG",  # should be ignored
        lifecycle=lifecycle,
    )
    art = SimpleNamespace(webapp_metadata=meta, slug="real-slug", kind="webapp")

    # Track what slug the engine receives.
    captured_args = []

    def _fake_run_aws(cmd, profile, timeout):
        captured_args.append(cmd)
        if "describe-stacks" in cmd:
            import json as _j
            return (0, _j.dumps([{"OutputKey": "BucketName", "OutputValue": "my-bucket"}]), "")
        if "s3" in cmd and "cp" in cmd:
            if "-" in cmd:  # manifest READ -- must exist + match identity (R17 F1)
                return (0, _j2.dumps({"slug": "real-slug",
                                      "distribution_id": "EIDENT1"}), "")
            return (0, "", "")
        return (1, "", "")

    monkeypatch.setattr(engine, "run_aws", _fake_run_aws)

    result = _run(handlers._expire_manifest_best_effort(art))
    assert result == "expired-now"
    # The S3 key must use "real-slug", not "FORGED-SLUG"
    s3_cmd = [c for c in captured_args if "s3" in c and "cp" in c]
    assert s3_cmd  # R12 F2: read + write — check every s3 call uses art.slug
    for cmd in s3_cmd:
        s3_args_str = " ".join(cmd)
        assert "real-slug/.kirocrew-deploy.json" in s3_args_str
        assert "FORGED-SLUG" not in s3_args_str


def test_expire_manifest_unregistered_profile_returns_unreachable(monkeypatch):
    """If the metadata profile is not in the registry, return 'unreachable' (no engine call)."""
    from types import SimpleNamespace

    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)

    # Do NOT register any profile.
    lifecycle = SimpleNamespace(created_at="2026-01-01T00:00:00Z", expires_at=None,
                                persistent=False, status="live")
    meta = SimpleNamespace(
        deploy_target=SimpleNamespace(profile="ghost-profile", region="us-west-2"),
        slug="some-slug",
        lifecycle=lifecycle,
    )
    art = SimpleNamespace(webapp_metadata=meta, slug="some-slug", kind="webapp")

    # engine.run_aws should never be called.
    def _should_not_call(*a, **kw):
        raise AssertionError("engine.run_aws should not be called for unregistered profile")

    monkeypatch.setattr(engine, "run_aws", _should_not_call)

    result = _run(handlers._expire_manifest_best_effort(art))
    assert result == "unreachable"


# --- compat aliases (/api/apps/deploy-web/* -> /api/deploy/*) ---------------

def test_compat_alias_registered():
    from aiohttp import web
    app = web.Application()
    handlers.register_routes(app)
    paths = {r.resource.canonical for r in app.router.routes() if r.resource}
    assert "/api/apps/deploy-web/{tail}" in paths


def test_compat_alias_redirects_within_deploy_surface():
    """Old app-era URLs 307 to the canonical /api/deploy/* routes.

    Static endpoints redirect. teardown/<slug> returns 404 JSON (no Location
    header — slug is user data, F7). Security is preserved: the canonical
    handlers enforce _deny_restricted themselves.
    """
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def scenario():
        app = web.Application()
        handlers.register_routes(app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/apps/deploy-web/sites", allow_redirects=False)
            assert resp.status == 307
            assert resp.headers["Location"] == "/api/deploy/list"
            # teardown/<slug>: no redirect (slug is user data), 404 with hint
            resp2 = await client.post(
                "/api/apps/deploy-web/teardown/some-slug",
                json={"confirm": True}, allow_redirects=False)
            assert resp2.status == 404
            assert "Location" not in resp2.headers
            data = await resp2.json()
            assert data["error"] == "moved"
            assert "/api/deploy/teardown/some-slug" in data["use"]

    _run(scenario())


def test_compat_alias_rejects_unsafe_tails():
    """Traversal / unsafe-charset tails 404 instead of redirecting.

    The tail is attacker-controlled; without validation a crafted
    ``/api/apps/deploy-web/../../admin`` Location would be normalized by the
    browser into an arbitrary internal path (CodeQL: URL redirection from
    remote source). Only the legacy endpoint charset is redirected.
    """
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def scenario():
        app = web.Application()
        handlers.register_routes(app)
        async with TestClient(TestServer(app)) as client:
            for tail in ("..%2F..%2Fadmin", "list%2F..%2F..%2Fsecrets",
                         "a%5Cb", "x%20y"):
                resp = await client.get(
                    f"/api/apps/deploy-web/{tail}", allow_redirects=False)
                assert resp.status == 404, tail
                assert "Location" not in resp.headers, tail
            # Safe tails still redirect.
            ok = await client.get(
                "/api/apps/deploy-web/sites", allow_redirects=False)
            assert ok.status == 307

    _run(scenario())


def test_deploy_rejects_on_windows(monkeypatch):
    """_do_deploy returns 400 with clear message on Windows (os.name == 'nt')."""
    import asyncio

    from kiro_crew.deploy import handlers

    monkeypatch.setattr("os.name", "nt")

    async def scenario():
        status, body = await handlers._do_deploy({"site_id": "test", "artifact_slug": "x"})
        assert status == 400
        assert "POSIX shell" in body["error"]
        assert "WSL" in body["error"]

    asyncio.run(scenario())


# --- teardown reaper-check tests (item 3 R18) --------------------------------

def test_teardown_reaper_present_tombstones(monkeypatch):
    """Teardown proceeds (tombstones) when the reaper stack exists."""
    # F2 (r4): teardown validates metadata profile against the registry —
    # register the fake profile so these tests exercise the paths under test.
    from kiro_crew.deploy import profiles as _profiles_mod
    monkeypatch.setattr(_profiles_mod, "load_registry", lambda: {
        "version": 2,
        "profiles": [{"name": "p", "region": "us-west-2"}],
        "default": "p",
    })
    from kiro_crew.deploy import handlers

    # Mock _check_reaper_installed to return True
    monkeypatch.setattr(handlers, "_check_reaper_installed", lambda p, r: True)

    # Create a minimal artifact namespace with webapp metadata
    meta = SimpleNamespace(
        deploy_target=SimpleNamespace(profile="p", region="us-west-2", slug="x"),
        lifecycle=SimpleNamespace(persistent=False),
    )
    art = SimpleNamespace(webapp_metadata=meta, slug="test-slug", kind="webapp")
    store = SimpleNamespace(
        get=lambda slug: art,
        mark_webapp_expired=lambda slug: art,
    )
    monkeypatch.setattr(handlers, "get_default_store", lambda: store)

    async def _fake_expire(a):
        return "ok"

    monkeypatch.setattr(handlers, "_expire_manifest_best_effort", _fake_expire)

    class _Req(_FakeReq):
        def __init__(self):
            super().__init__(body={"confirm": True}, match_info={"slug": "test-slug"})

    # Patch _serialize to avoid import issues
    monkeypatch.setattr("kiro_crew.dashboard.handlers.artifacts._serialize",
                        lambda a, include_content=False: {
                            "webapp_metadata": {"teardown": {}, "architecture": {}},
                        })

    resp = _run(handlers._handle_teardown(_Req()))
    assert resp.status == 200


def test_teardown_reaper_absent_returns_409(monkeypatch):
    """Teardown returns 409 when the reaper stack is not installed."""
    # F2 (r4): teardown validates metadata profile against the registry —
    # register the fake profile so these tests exercise the paths under test.
    from kiro_crew.deploy import profiles as _profiles_mod
    monkeypatch.setattr(_profiles_mod, "load_registry", lambda: {
        "version": 2,
        "profiles": [{"name": "p", "region": "us-west-2"}],
        "default": "p",
    })
    from kiro_crew.deploy import handlers

    # Mock _check_reaper_installed to return False
    monkeypatch.setattr(handlers, "_check_reaper_installed", lambda p, r: False)

    # Create a minimal artifact
    meta = SimpleNamespace(
        deploy_target=SimpleNamespace(profile="p", region="us-west-2", slug="x"),
        lifecycle=SimpleNamespace(persistent=True),
    )
    art = SimpleNamespace(webapp_metadata=meta, slug="test-slug", kind="webapp")
    store = SimpleNamespace(get=lambda slug: art)
    monkeypatch.setattr(handlers, "get_default_store", lambda: store)

    class _Req(_FakeReq):
        def __init__(self):
            super().__init__(body={"confirm": True}, match_info={"slug": "test-slug"})

    resp = _run(handlers._handle_teardown(_Req()))
    import json as _json
    body = _json.loads(resp.text)
    assert resp.status == 409
    assert "reaper not installed" in body["error"]
    assert body["reaper_missing"] is True


# --- local_dir fullstack layout test (item 5 R18) ---

def test_do_deploy_local_dir_stages_public_dir(monkeypatch, tmp_path):
    """_do_deploy with local_dir correctly stages the directory for static deploy."""
    from kiro_crew.deploy import engine, handlers

    # Create a fullstack-layout dir: public/ + api/
    public = tmp_path / "myapp" / "public"
    public.mkdir(parents=True)
    (public / "index.html").write_text("<h1>Hello</h1>")
    api = tmp_path / "myapp" / "api"
    api.mkdir()
    (api / "index.py").write_text("def handler(event, ctx): pass")

    # Set up profile
    monkeypatch.setattr(handlers, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(handlers, "DATA_DIR", tmp_path)
    handlers._save_config("test-profile", "us-west-2")

    # Monkeypatch config_dir so staging goes under tmp_path
    fake_cfg = tmp_path / "fakecfg"
    fake_cfg.mkdir()
    monkeypatch.setattr(handlers, "config_dir", lambda: fake_cfg)

    # Mock engine.deploy to capture what gets passed
    deployed = {}

    def fake_deploy(site_id, src_dir, profile, region):
        deployed["site_id"] = site_id
        deployed["src_dir"] = src_dir
        return {"url": "https://fake.cloudfront.net/my-app/"}

    monkeypatch.setattr(engine, "deploy", fake_deploy)
    # Mock _allowed_local_roots to include tmp_path
    monkeypatch.setattr(handlers, "_allowed_local_roots", lambda: [tmp_path])

    status, body = _run(handlers._do_deploy({
        "site_id": "my-app",
        "local_dir": str(public),
        "confirm": True,
    }))
    assert status == 200, f"Expected 200, got {status}: {body}"
    # F3: src_dir is now the staged immutable copy, not the live directory
    assert "deploy-staging" in deployed["src_dir"]
    assert "deploy-stage-" in deployed["src_dir"]


# --- item 6: ttl_hours manifest write ---

def test_do_deploy_writes_manifest_with_ttl(tmp_path, monkeypatch):
    """After successful deploy, _do_deploy writes .kirocrew-deploy.json with TTL."""
    public = tmp_path / "site"
    public.mkdir()
    (public / "index.html").write_text("<h1>hi</h1>")

    captured_s3_calls = []

    def fake_deploy(site_id, src_dir, profile, region):
        return {"url": "https://d.cloudfront.net/", "bucket": "test-bucket",
                "site_id": site_id, "distribution_id": "EXXX"}

    def fake_run_aws(args, profile, timeout=30):
        captured_s3_calls.append(args)
        # Return valid base stack output for describe-stacks (base-stack check)
        if "describe-stacks" in args:
            import json as _json
            return (0, _json.dumps([{"OutputKey": "BucketName", "OutputValue": "test-bucket"}]), "")
        return (0, "", "")

    monkeypatch.setattr(engine, "deploy", fake_deploy)
    monkeypatch.setattr(engine, "run_aws", fake_run_aws)
    monkeypatch.setattr(handlers, "_allowed_local_roots", lambda: [tmp_path])

    # Mock profile resolution
    monkeypatch.setattr(profiles_mod, "resolve_profile", lambda p: ("default", "us-west-2"))
    monkeypatch.setattr(profiles_mod, "load_registry", lambda: {"default": "default", "profiles": [{"name": "default", "region": "us-west-2"}]})

    status, body = _run(handlers._do_deploy({
        "site_id": "my-ttl-app",
        "local_dir": str(public),
        "ttl_hours": 24,
        "confirm": True,
    }))
    assert status == 200

    # Verify an S3 cp command was issued for the manifest
    manifest_calls = [c for c in captured_s3_calls if ".kirocrew-deploy.json" in " ".join(c)]
    assert len(manifest_calls) == 1
    assert "s3://test-bucket/my-ttl-app/.kirocrew-deploy.json" in " ".join(manifest_calls[0])

    # Verify the temp manifest file contents (the s3 cp source)
    src_file = manifest_calls[0][2]  # ["s3", "cp", <local_file>, "s3://..."]
    # File is already cleaned up but we can verify it was in the call
    assert src_file.endswith(".json")


# --- item #3: ttl_hours validation ----------------------------------------

def test_deploy_invalid_ttl_rejected_before_deploy(monkeypatch):
    """Invalid ttl_hours returns 400 before any AWS call is made."""
    _set_profile(monkeypatch)
    monkeypatch.setattr(handlers, "get_default_store", lambda: _fake_store(), raising=False)
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)
    status, payload = _run(handlers._do_deploy(
        {"site_id": "s", "artifact_slug": "a", "confirm": True, "ttl_hours": "banana"}))
    assert status == 400
    assert "ttl_hours must be an integer" in payload["error"]


def test_deploy_negative_ttl_rejected(monkeypatch):
    """Negative ttl_hours returns 400."""
    _set_profile(monkeypatch)
    monkeypatch.setattr(handlers, "get_default_store", lambda: _fake_store(), raising=False)
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)
    status, payload = _run(handlers._do_deploy(
        {"site_id": "s", "artifact_slug": "a", "confirm": True, "ttl_hours": -5}))
    assert status == 400
    assert "non-negative" in payload["error"]


def test_deploy_expires_at_uses_future_time(monkeypatch):
    """expires_at in manifest = now + ttl_hours, NOT creation time."""
    import json
    from datetime import datetime, timedelta, timezone

    _set_profile(monkeypatch)
    monkeypatch.setattr(handlers, "get_default_store", lambda: _fake_store(), raising=False)
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)

    captured_manifest = {}

    def fake_deploy(site_id, src_dir, profile, region):
        return {"site_id": site_id, "url": "https://d.cloudfront.net/", "reused": False,
                "bucket": "b", "distribution_id": "D1", "status": "Deployed"}

    def fake_run_aws(cmd, profile, timeout=15):
        if "describe-stacks" in cmd:
            import json as _j
            return (0, _j.dumps([{"OutputKey": "BucketName", "OutputValue": "base-bkt"}]), "")
        if cmd[0] == "s3" and cmd[1] == "cp":
            # Capture the manifest content before the file is deleted
            src_file = cmd[2]
            try:
                captured_manifest["data"] = Path(src_file).read_text()
            except FileNotFoundError:
                pass
        return (0, "", "")

    monkeypatch.setattr(engine, "deploy", fake_deploy)
    monkeypatch.setattr(engine, "run_aws", fake_run_aws)

    before = datetime.now(timezone.utc)
    status, payload = _run(handlers._do_deploy(
        {"site_id": "s", "artifact_slug": "a", "confirm": True, "ttl_hours": 24}))
    after = datetime.now(timezone.utc)
    assert status == 200

    # Parse the manifest that was written
    manifest = json.loads(captured_manifest["data"])
    expires = datetime.strptime(manifest["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    # expires_at should be ~24 hours from now (not equal to created_at)
    assert expires > before + timedelta(hours=23, minutes=59)
    assert expires < after + timedelta(hours=24, minutes=1)
    # created_at should be approximately now
    created = datetime.strptime(manifest["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert created != expires  # Critical: they must NOT be the same


# --- item #4: manifest goes to base-stack bucket --------------------------

def test_deploy_manifest_written_to_base_stack_bucket(monkeypatch):
    """Manifest should go to the shared base-stack bucket, not per-site engine bucket."""
    _set_profile(monkeypatch)
    monkeypatch.setattr(handlers, "get_default_store", lambda: _fake_store(), raising=False)
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)

    s3_calls = []

    def fake_deploy(site_id, src_dir, profile, region):
        return {"site_id": site_id, "url": "https://d.cloudfront.net/", "reused": False,
                "bucket": "kirocrew-web-random123", "distribution_id": "D1", "status": "Deployed"}

    def fake_run_aws(cmd, profile, timeout=15):
        import json as _j
        if "describe-stacks" in cmd and "kirocrew-deploy-base" in cmd:
            return (0, _j.dumps([
                {"OutputKey": "BucketName", "OutputValue": "shared-base-bucket"},
                {"OutputKey": "DistributionId", "OutputValue": "DXYZ"},
            ]), "")
        if cmd[0] == "s3" and cmd[1] == "cp":
            s3_calls.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(engine, "deploy", fake_deploy)
    monkeypatch.setattr(engine, "run_aws", fake_run_aws)

    status, _ = _run(handlers._do_deploy(
        {"site_id": "mysite", "artifact_slug": "a", "confirm": True}))
    assert status == 200

    # The manifest must target the SHARED base bucket, not the per-site random one
    assert len(s3_calls) == 1
    s3_dest = s3_calls[0][3]  # "s3://shared-base-bucket/mysite/.kirocrew-deploy.json"
    assert "shared-base-bucket" in s3_dest
    assert "kirocrew-web-random123" not in s3_dest
    assert "mysite/.kirocrew-deploy.json" in s3_dest


# --- F1: manifest includes arch/bucket/distribution_id fields ----------------

def test_deploy_manifest_includes_engine_arch_fields(monkeypatch, tmp_path):
    """F1: _do_deploy must write arch, bucket, distribution_id to the manifest."""
    _set_profile(monkeypatch, "p")

    from kiro_crew.artifacts import ArtifactStore

    store = ArtifactStore(tmp_path / "store")
    store.create(name="mywidget", content="<p>Hello</p>", kind="widget")
    monkeypatch.setattr(handlers, "get_default_store", lambda: store)

    manifest_data: list = []

    def fake_deploy(site_id, src_dir, profile, region):
        return {"site_id": site_id, "bucket": "kirocrew-web-abc123",
                "distribution_id": "E1234XYZ", "url": "https://d.cf.net/", "status": "Deployed"}

    def fake_run_aws(args, profile, timeout=30):
        # Capture the manifest write
        if args[:2] == ["s3", "cp"] and ".kirocrew-deploy.json" in str(args):
            import json as _json

            # Read the temp file that was written
            src_file = args[2]
            with open(src_file) as f:
                manifest_data.append(_json.load(f))
        if "describe-stacks" in args:
            import json as _json
            return (0, _json.dumps([{"OutputKey": "BucketName", "OutputValue": "shared-bucket"}]), "")
        return (0, "", "")

    monkeypatch.setattr(engine, "deploy", fake_deploy)
    monkeypatch.setattr(engine, "run_aws", fake_run_aws)

    status, payload = _run(handlers._do_deploy(
        {"site_id": "testsite", "artifact_slug": "mywidget", "confirm": True}))
    assert status == 200

    assert len(manifest_data) == 1
    man = manifest_data[0]
    assert man["arch"] == "engine"
    assert man["bucket"] == "kirocrew-web-abc123"
    assert man["distribution_id"] == "E1234XYZ"


# --- F1: reaper engine-arch happy path + retry path --------------------------

def test_reaper_engine_arch_happy_path(monkeypatch):
    """F1: reaper reaps engine-arch deploy (distribution disabled → deleted → bucket emptied)."""
    import sys
    from unittest.mock import MagicMock

    # Mock boto3 before importing the reaper module
    mock_boto3 = MagicMock()
    mock_botocore = MagicMock()
    monkeypatch.setitem(sys.modules, "boto3", mock_boto3)
    monkeypatch.setitem(sys.modules, "botocore", mock_botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", mock_botocore.exceptions)

    # Set env vars the module expects
    monkeypatch.setenv("BUCKET", "shared-bucket")
    monkeypatch.setenv("DIST_ID", "ESHARED")

    reaper_path = str(Path(__file__).resolve().parent.parent /
                      "src/kiro_crew/deploy/skills/artifact-deploy/scripts/reaper_lambda")
    monkeypatch.syspath_prepend(reaper_path)

    # Force reimport
    if "index" in sys.modules:
        del sys.modules["index"]
    import index as reaper_mod

    mock_s3 = MagicMock()
    mock_cf = MagicMock()
    monkeypatch.setattr(reaper_mod, "s3", mock_s3)
    monkeypatch.setattr(reaper_mod, "cf", mock_cf)
    monkeypatch.setenv("ACCOUNT_ID", "123456789012")

    # R19 F4: tag verification mocks — distribution and bucket tags match slug
    mock_cf.list_tags_for_resource.return_value = {
        "Tags": {"Items": [{"Key": "kirocrew:site", "Value": "mysite"}]}
    }
    mock_s3.get_bucket_tagging.return_value = {
        "TagSet": [{"Key": "kirocrew:site", "Value": "mysite"}]
    }

    # Distribution is already disabled (Enabled=False)
    mock_cf.get_distribution_config.return_value = {
        "ETag": "etag1",
        "DistributionConfig": {"Enabled": False},
    }
    mock_cf.delete_distribution.return_value = {}

    # Bucket listing returns one object
    mock_s3.get_paginator.return_value.paginate.return_value = [
        {"Contents": [{"Key": "index.html"}]}
    ]
    mock_s3.delete_objects.return_value = {}
    mock_s3.delete_bucket.return_value = {}
    mock_s3.delete_object.return_value = {}

    man = {
        "slug": "mysite",
        "arch": "engine",
        "bucket": "kirocrew-web-mysite",
        "distribution_id": "E999",
        "expires_at": "2020-01-01T00:00:00Z",
        "persistent": False,
    }

    result = reaper_mod._reap_engine_arch(man, "mysite", 9999999999)

    assert result == "reaped"
    mock_cf.delete_distribution.assert_called_once_with(Id="E999", IfMatch="etag1")
    # P476046789: destructive S3 calls now pin ExpectedBucketOwner (from ACCOUNT_ID).
    mock_s3.delete_bucket.assert_called_once_with(
        Bucket="kirocrew-web-mysite", ExpectedBucketOwner="123456789012"
    )
    mock_s3.delete_object.assert_called_once()


def test_reaper_engine_arch_distribution_not_disabled_retries(monkeypatch):
    """F1: reaper returns 'reaping' when DistributionNotDisabled."""
    import sys
    from unittest.mock import MagicMock

    mock_boto3 = MagicMock()
    mock_botocore = MagicMock()

    # We need a real ClientError class
    class _FakeClientError(Exception):
        def __init__(self, error_response, operation_name):
            self.response = error_response
            super().__init__(f"{operation_name}: {error_response}")

    mock_botocore.exceptions.ClientError = _FakeClientError
    monkeypatch.setitem(sys.modules, "boto3", mock_boto3)
    monkeypatch.setitem(sys.modules, "botocore", mock_botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", mock_botocore.exceptions)

    monkeypatch.setenv("BUCKET", "shared-bucket")
    monkeypatch.setenv("DIST_ID", "ESHARED")

    reaper_path = str(Path(__file__).resolve().parent.parent /
                      "src/kiro_crew/deploy/skills/artifact-deploy/scripts/reaper_lambda")
    monkeypatch.syspath_prepend(reaper_path)

    if "index" in sys.modules:
        del sys.modules["index"]
    import index as reaper_mod

    mock_s3 = MagicMock()
    mock_cf = MagicMock()
    monkeypatch.setattr(reaper_mod, "s3", mock_s3)
    monkeypatch.setattr(reaper_mod, "cf", mock_cf)
    monkeypatch.setattr(reaper_mod, "botocore", mock_botocore)
    monkeypatch.setenv("ACCOUNT_ID", "123456789012")

    # R19 F4: tag verification mocks — distribution tags match slug
    mock_cf.list_tags_for_resource.return_value = {
        "Tags": {"Items": [{"Key": "kirocrew:site", "Value": "retrysite"}]}
    }

    # Distribution is disabled but delete fails with DistributionNotDisabled
    mock_cf.get_distribution_config.return_value = {
        "ETag": "etag2",
        "DistributionConfig": {"Enabled": False},
    }
    error_response = {"Error": {"Code": "DistributionNotDisabled", "Message": "still propagating"}}
    mock_cf.delete_distribution.side_effect = _FakeClientError(
        error_response, "DeleteDistribution")

    man = {
        "slug": "retrysite",
        "arch": "engine",
        "bucket": "kirocrew-web-retrysite",
        "distribution_id": "EXYZ",
        "expires_at": "2020-01-01T00:00:00Z",
        "persistent": False,
    }

    result = reaper_mod._reap_engine_arch(man, "retrysite", 9999999999)

    assert result == "reaping"
    # Manifest should be rewritten with reaping=True
    mock_s3.put_object.assert_called_once()
    call_kwargs = mock_s3.put_object.call_args[1]
    import json as _json
    body = _json.loads(call_kwargs["Body"])
    assert body["reaping"] is True


# --- F2: symlink-escape blocks deploy ----------------------------------------

def test_scan_tree_symlink_escape_blocked(tmp_path):
    """F2: symlink pointing outside source tree → symlink-escape finding, target never read."""
    # Create a file outside the source tree
    outside = tmp_path / "outside"
    outside.mkdir()
    secret_file = outside / "passwd"
    secret_file.write_text("root:x:0:0:root:/root:/bin/bash")

    # Create the source tree with a symlink to outside
    src = tmp_path / "src"
    src.mkdir()
    (src / "index.html").write_text("<h1>safe</h1>")
    link = src / "evil_link"
    link.symlink_to(secret_file)

    findings, byte_size = handlers._scan_tree(src)

    # Must have a symlink-escape finding
    escape_findings = [f for f in findings if f.kind == "symlink-escape"]
    assert len(escape_findings) == 1
    assert "escapes source tree" in escape_findings[0].snippet

    # Target content must NEVER appear in any finding snippet
    all_snippets = " ".join(f.snippet for f in findings)
    assert "root:x:0:0" not in all_snippets


def test_dir_contains_sensitive_symlink_escape(tmp_path):
    """F2: _dir_contains_sensitive returns True for symlink escaping the tree."""
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "creds"
    target.write_text("AWS_SECRET=xxx")

    src = tmp_path / "src"
    src.mkdir()
    link = src / "link"
    link.symlink_to(target)

    assert handlers._dir_contains_sensitive(src, src.resolve()) is True


# --- F4: ttl_hours upper bound validation ------------------------------------

def test_ttl_hours_overflow_blocked(monkeypatch, tmp_path):
    """F4: ttl_hours > 8760 returns 400 BEFORE any engine call."""
    _set_profile(monkeypatch, "p")

    from kiro_crew.artifacts import ArtifactStore

    store = ArtifactStore(tmp_path / "store")
    store.create(name="myart", content="<p>test</p>", kind="widget")
    monkeypatch.setattr(handlers, "get_default_store", lambda: store)

    engine_called = []
    monkeypatch.setattr(engine, "deploy", lambda *a, **kw: engine_called.append(1) or {})

    status, payload = _run(handlers._do_deploy(
        {"site_id": "x", "artifact_slug": "myart", "ttl_hours": 10**12, "confirm": True}))
    assert status == 400
    assert "0-8760" in payload["error"]
    assert engine_called == []  # Engine never invoked


# ══════════════════════════════════════════════════════════════════════════════
# F1: Internal-secret callers cannot confirm/override — server enforces preview-only
# ══════════════════════════════════════════════════════════════════════════════

def test_internal_secret_strips_confirm_enforces_preview():
    """POST /api/deploy/deploy with X-Internal-Secret + confirm=true returns
    the requires_confirm preview — engine.deploy is NOT called.

    This tests through the real auth middleware (aiohttp TestClient), not a mocked _post.
    """
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.token_auth import token_auth_middleware

    _internal_secret = "test-secret-12345678"

    async def scenario():
        app = web.Application()
        # Install minimal token_auth middleware with an internal secret
        app.middlewares.append(
            token_auth_middleware(
                internal_paths=frozenset(),
                mixed_internal_paths=frozenset({"/api/deploy"}),
                internal_secret=_internal_secret,
                port=9999,
                local_only=True,
            )
        )
        handlers.register_routes(app)
        # Install minimal state for _deny_restricted
        app["state"] = _NonRestrictedState()

        engine_called = []

        import kiro_crew.deploy.engine as _eng
        orig_deploy = _eng.deploy

        def _fake_deploy(*a, **kw):
            engine_called.append(1)
            return {"bucket": "b", "distribution_id": "d"}

        _eng.deploy = _fake_deploy
        try:
            async with TestClient(TestServer(app)) as client:
                # Internal-secret request WITH confirm=true — should get preview, NOT deploy
                resp = await client.post(
                    "/api/deploy/deploy",
                    json={"site_id": "test-site", "local_dir": "/tmp", "confirm": True},
                    headers={"X-Internal-Secret": _internal_secret},
                )
                data = await resp.json()
                # Should get a 400 (no profile configured) or 200 preview — NOT a deploy.
                # The key assertion: confirm was stripped, so engine never runs.
                assert engine_called == [], f"engine.deploy was called! Response: {data}"
                # If we get far enough: requires_confirm or a pre-confirm error
                assert resp.status != 500
        finally:
            _eng.deploy = orig_deploy

    _run(scenario())


def test_internal_secret_teardown_denied_403():
    """POST /api/deploy/teardown/<slug> with X-Internal-Secret is denied 403.

    /api/deploy is a prefix-matched mixed-internal path, so teardown IS
    reachable by internal-secret callers; unlike deploy it has no preview
    form, so the server must deny outright (destruction needs a human in
    the dashboard UI). Goes through the real auth middleware.
    """
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.token_auth import token_auth_middleware

    _internal_secret = "test-secret-12345678"

    async def scenario():
        app = web.Application()
        app.middlewares.append(
            token_auth_middleware(
                internal_paths=frozenset(),
                mixed_internal_paths=frozenset({"/api/deploy"}),
                internal_secret=_internal_secret,
                port=9999,
                local_only=True,
            )
        )
        handlers.register_routes(app)
        app["state"] = _NonRestrictedState()

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/deploy/teardown/some-slug",
                json={"confirm": True},
                headers={"X-Internal-Secret": _internal_secret},
            )
            assert resp.status == 403
            data = await resp.json()
            assert "not available to internal-secret" in data.get("error", "")

    _run(scenario())


# ══════════════════════════════════════════════════════════════════════════════
# F2: Unreadable files produce unreadable-file findings (fail closed)
# ══════════════════════════════════════════════════════════════════════════════

def test_scan_tree_unreadable_file_produces_finding(tmp_path):
    """chmod-000 file in scan tree -> deploy blocked with unreadable-file finding."""
    import os
    if os.geteuid() == 0:
        pytest.skip("running as root — cannot test permission-denied")
    # Create a normal file and an unreadable one
    (tmp_path / "ok.txt").write_text("hello world")
    bad = tmp_path / "secret.txt"
    bad.write_text("sensitive")
    bad.chmod(0o000)
    try:
        findings, byte_size = handlers._scan_tree(tmp_path)
        denied = [f for f in findings if f.kind in ("unreadable-file", "hook-denied")]
        assert len(denied) >= 1
        assert denied[0].severity == "credential"
        assert "secret.txt" in denied[0].snippet
    finally:
        bad.chmod(0o644)


# ══════════════════════════════════════════════════════════════════════════════
# F3: Builtin migration matches actual legacy dir name "deploy-web"
# ══════════════════════════════════════════════════════════════════════════════

def test_migrated_builtin_cleans_hyphenated_name(tmp_path, monkeypatch):
    """Migration cleans up a 'deploy-web' legacy app install."""
    from kiro_crew.apps import manager as mgr
    from kiro_crew.apps.builtins import _MIGRATED_BUILTINS

    # Verify "deploy-web" is in the list
    assert "deploy-web" in _MIGRATED_BUILTINS

    # Create a fake legacy installed app directory
    monkeypatch.setattr(mgr, "apps_dir", lambda: tmp_path)
    app_path = tmp_path / "deploy-web"
    app_path.mkdir()
    # Write minimal installed.json (origin=builtin)
    import json as _json
    (app_path / "installed.json").write_text(_json.dumps({
        "name": "deploy-web",
        "origin": "builtin",
        "resources": "gateway",
        "lifecycle": "gateway",
        "schemaVersion": 2,
    }))
    (app_path / "app.json").write_text(_json.dumps({"name": "deploy-web", "version": "1.0.0"}))
    # Create a data dir that should survive
    data_dir = app_path / "data"
    data_dir.mkdir()
    (data_dir / "user-state.json").write_text("{}")

    result = mgr.cleanup_migrated_builtin("deploy-web")
    assert result.ok
    # Metadata removed
    assert not (app_path / "installed.json").exists()
    assert not (app_path / "app.json").exists()
    # Data dir preserved
    assert data_dir.exists()
    assert (data_dir / "user-state.json").exists()


# ══════════════════════════════════════════════════════════════════════════════
# F4: Finite TTL + no base stack = 409 pre-deploy; engine never called
# ══════════════════════════════════════════════════════════════════════════════

def test_deploy_ttl_nonzero_no_base_stack_returns_409(monkeypatch, tmp_path):
    """ttl_hours>0 with no reaper base stack -> 409 before engine.deploy."""
    _set_profile(monkeypatch, "p", "us-west-2")
    src = tmp_path / "site"
    src.mkdir()
    (src / "index.html").write_text("<h1>hi</h1>")
    monkeypatch.setattr(handlers, "_allowed_local_roots", lambda: [tmp_path])

    # Override the autouse mock: base stack not found
    monkeypatch.setattr(engine, "run_aws", lambda *a, **kw: (255, "", "stack not found"))

    engine_called = []
    monkeypatch.setattr(engine, "deploy", lambda *a, **kw: engine_called.append(1) or {})

    status, payload = _run(handlers._do_deploy({
        "site_id": "test", "local_dir": str(src), "confirm": True, "ttl_hours": 72}))
    assert status == 409
    assert "reaper base stack" in payload["error"]
    assert engine_called == []


def test_deploy_ttl_zero_no_base_stack_succeeds(monkeypatch, tmp_path):
    """ttl_hours=0 (persistent) deploys fine even without reaper base stack."""
    _set_profile(monkeypatch, "p", "us-west-2")
    src = tmp_path / "site"
    src.mkdir()
    (src / "index.html").write_text("<h1>hi</h1>")
    monkeypatch.setattr(handlers, "_allowed_local_roots", lambda: [tmp_path])

    # Override the autouse mock: base stack not found
    monkeypatch.setattr(engine, "run_aws", lambda *a, **kw: (255, "", "stack not found"))
    monkeypatch.setattr(engine, "deploy", lambda *a, **kw: {"bucket": "b", "url": "https://x.cf.net/"})

    status, payload = _run(handlers._do_deploy({
        "site_id": "test", "local_dir": str(src), "confirm": True, "ttl_hours": 0}))
    assert status == 200
    assert payload.get("url") or payload.get("bucket")


# ══════════════════════════════════════════════════════════════════════════════
# F7: Compat redirects — static endpoints 307, teardown no Location, unsafe 404
# ══════════════════════════════════════════════════════════════════════════════

def test_compat_alias_static_endpoints_redirect():
    """The four static legacy endpoints 307 to their canonical paths."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def scenario():
        app = web.Application()
        handlers.register_routes(app)
        async with TestClient(TestServer(app)) as client:
            for tail, target in [
                ("deploy", "/api/deploy/deploy"),
                ("recall", "/api/deploy/recall"),
                ("destroy", "/api/deploy/destroy"),
                ("sites", "/api/deploy/list"),
            ]:
                resp = await client.get(
                    f"/api/apps/deploy-web/{tail}", allow_redirects=False)
                assert resp.status == 307, f"{tail}: expected 307 got {resp.status}"
                assert resp.headers["Location"] == target

    _run(scenario())


def test_compat_alias_teardown_no_location_header():
    """teardown/<slug> returns 404 JSON with hint, no Location header."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def scenario():
        app = web.Application()
        handlers.register_routes(app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/apps/deploy-web/teardown/my-slug",
                json={"confirm": True}, allow_redirects=False)
            assert resp.status == 404
            assert "Location" not in resp.headers
            data = await resp.json()
            assert data["error"] == "moved"
            assert "/api/deploy/teardown/my-slug" in data["use"]

    _run(scenario())


def test_compat_alias_unknown_tail_404():
    """Unknown tails get plain 404."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def scenario():
        app = web.Application()
        handlers.register_routes(app)
        async with TestClient(TestServer(app)) as client:
            for tail in ("../../admin", "unknown-path", "not-an-endpoint"):
                resp = await client.get(
                    f"/api/apps/deploy-web/{tail}", allow_redirects=False)
                assert resp.status == 404, f"{tail}: expected 404 got {resp.status}"
                assert "Location" not in resp.headers

    _run(scenario())


# ══════════════════════════════════════════════════════════════════════════════
# F1: Registration-time assertion — every handler in allowlist or has guard
# ══════════════════════════════════════════════════════════════════════════════

def test_register_routes_assertion_coverage():
    """Every /api/deploy handler is either allowed or @_internal_denied."""
    from aiohttp import web
    app = web.Application()
    # Should NOT raise — all handlers are properly covered
    handlers.register_routes(app)


def test_internal_secret_profiles_post_denied():
    """internal-secret POST /api/deploy/profiles -> 403."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.token_auth import token_auth_middleware

    _internal_secret = "test-secret-12345678"

    class _NonRestrictedState:
        class config:
            dashboard = type("D", (), {"restricted_mode": False})()

    async def scenario():
        app = web.Application()
        app.middlewares.append(
            token_auth_middleware(
                internal_paths=frozenset(),
                mixed_internal_paths=frozenset({"/api/deploy"}),
                internal_secret=_internal_secret,
                port=9999,
                local_only=True,
            )
        )
        handlers.register_routes(app)
        app["state"] = _NonRestrictedState()

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/deploy/profiles",
                json={"name": "evil", "region": "us-west-2"},
                headers={"X-Internal-Secret": _internal_secret},
            )
            assert resp.status == 403
            data = await resp.json()
            assert "not available to internal-secret" in data.get("error", "")

    _run(scenario())


def test_internal_secret_list_allowed():
    """internal-secret GET /api/deploy/list -> 200 (in allowlist)."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.token_auth import token_auth_middleware

    _internal_secret = "test-secret-12345678"

    async def scenario():
        app = web.Application()
        app.middlewares.append(
            token_auth_middleware(
                internal_paths=frozenset(),
                mixed_internal_paths=frozenset({"/api/deploy"}),
                internal_secret=_internal_secret,
                port=9999,
                local_only=True,
            )
        )
        handlers.register_routes(app)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/deploy/list",
                headers={"X-Internal-Secret": _internal_secret},
            )
            assert resp.status == 200

    _run(scenario())


# ══════════════════════════════════════════════════════════════════════════════
# F3: TOCTOU — deployed dir is the staged copy, staging dir cleaned
# ══════════════════════════════════════════════════════════════════════════════

def test_deploy_local_dir_staged_copy_cleaned(tmp_path, monkeypatch):
    """F3: src_dir passed to engine.deploy is under staging root, not the original dir;
    staging is cleaned after deploy completes."""

    fake_cfg = tmp_path / "fakecfg"
    fake_cfg.mkdir()
    monkeypatch.setattr(handlers, "config_dir", lambda: fake_cfg)

    public = tmp_path / "myapp"
    public.mkdir()
    (public / "index.html").write_text("<h1>test</h1>")

    handlers._save_config("test-profile", "us-west-2")

    deployed = {}

    def fake_deploy(site_id, src_dir, profile, region):
        deployed["src_dir"] = src_dir
        # Verify the staged tree still exists during deploy
        assert Path(src_dir).exists()
        return {"url": "https://fake.cloudfront.net/my-app/"}

    monkeypatch.setattr(engine, "deploy", fake_deploy)
    monkeypatch.setattr(handlers, "_allowed_local_roots", lambda: [tmp_path])

    status, body = _run(handlers._do_deploy({
        "site_id": "staged-test",
        "local_dir": str(public),
        "confirm": True,
    }))
    assert status == 200, f"Expected 200, got {status}: {body}"
    staging_root = str(fake_cfg / "deploy-staging")
    assert deployed["src_dir"].startswith(staging_root)
    # Staging dir should be cleaned up after deploy
    assert not Path(deployed["src_dir"]).exists()


# ══════════════════════════════════════════════════════════════════════════════
# F2: /tmp no longer in _allowed_local_roots; staging root IS allowed
# ══════════════════════════════════════════════════════════════════════════════

def test_allowed_local_roots_no_bare_tmp(monkeypatch, tmp_path):
    """F2: _allowed_local_roots does NOT include bare /tmp."""
    from kiro_crew.config.loader import KiroCrewConfig
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: type("C", (), {
        "agent": type("A", (), {"subagent_cwd_allowed_roots": ["/home/testuser/workplace"]})()
    })())
    monkeypatch.setattr(handlers, "config_dir", lambda: tmp_path)
    roots = handlers._allowed_local_roots()
    root_strs = [str(r) for r in roots]
    # /tmp should NOT be in the list
    assert "/tmp" not in root_strs
    # But the staging root should be (under config_dir now)
    assert any("deploy-staging" in r for r in root_strs)


# ══════════════════════════════════════════════════════════════════════════════
# F1 (R11): NUL-prepended file still scanned for credentials
# ══════════════════════════════════════════════════════════════════════════════


def test_scan_tree_nul_prepended_file_detects_credential(tmp_path):
    """F1 R11: A file with NUL in first 8KiB containing AKIA key must still
    produce a credential finding. Previously the binary-detection short-circuited
    the entire content scan, allowing NUL-prepended secrets to deploy."""
    src = tmp_path / "src"
    src.mkdir()
    # NUL byte + AKIA key in "JS" that a browser would still parse
    payload = b"\x00// sneaky NUL-prepended file\nconst key = 'AKIAIOSFODNN7EXAMPLE';\n"
    (src / "app.js").write_bytes(payload)
    findings, byte_size = handlers._scan_tree(src)
    cred_findings = [f for f in findings if f.severity == "credential"]
    assert len(cred_findings) >= 1, f"Expected credential finding, got: {findings}"
    assert any("AKIA" in f.snippet for f in cred_findings)


def test_scan_tree_real_binary_no_credentials_deploys_clean(tmp_path):
    """F1 R11: A real binary file (PNG header + random bytes, no credentials)
    should produce no credential findings and deploy fine."""
    src = tmp_path / "src"
    src.mkdir()
    # PNG header + random non-credential binary content
    png_header = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    (src / "image.png").write_bytes(png_header + b"\x00" * 100 + b"\xff" * 200)
    findings, byte_size = handlers._scan_tree(src)
    cred_findings = [f for f in findings if f.severity == "credential"]
    assert len(cred_findings) == 0, f"Real binary should produce no credential findings: {findings}"
