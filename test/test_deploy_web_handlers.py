"""Tests for deploy_web handlers — endpoint core, scan-gate, confirm-gate, approval."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.apps.builtins.deploy_web import engine, handlers


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(handlers, "CONFIG_PATH", cfg)
    monkeypatch.setattr(handlers, "DATA_DIR", tmp_path)
    return cfg


def _run(coro):
    return asyncio.run(coro)


def _set_profile(monkeypatch, profile="p", region="us-west-2"):
    handlers._save_config(profile, region)


# --- config ---------------------------------------------------------------

def test_config_roundtrip():
    handlers._save_config("my-sso", "eu-west-1")
    cfg = handlers._load_config()
    assert cfg == {"profile": "my-sso", "region": "eu-west-1"}


class _FakeReq:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


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


def test_deploy_proceeds_with_confirm_and_override(monkeypatch):
    _set_profile(monkeypatch)
    leaky = "<p>AKIAABCDEFGHIJKLMNOP</p>"
    monkeypatch.setattr(handlers, "get_default_store", lambda: _fake_store(content=leaky), raising=False)
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)
    captured = {}

    def fake_deploy(site_id, src_dir, profile, region):
        captured["src_dir"] = src_dir
        captured["index"] = Path(src_dir, "index.html").read_text(encoding="utf-8")
        return {"site_id": site_id, "url": "https://d.cloudfront.net/", "reused": False,
                "bucket": "kirocrew-web-x", "distribution_id": "D1", "status": "InProgress"}

    monkeypatch.setattr(engine, "deploy", fake_deploy)
    status, payload = _run(handlers._do_deploy(
        {"site_id": "s", "artifact_slug": "a", "confirm": True, "override_scan": True}))
    assert status == 200
    assert payload["url"] == "https://d.cloudfront.net/"
    # The rendered standalone doc was written as index.html and handed to the engine.
    assert "<!DOCTYPE html>" in captured["index"]
    # Temp dir cleaned up afterwards.
    assert not Path(captured["src_dir"]).exists()


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
    for p in ("/api/apps/deploy-web/deploy", "/api/apps/deploy-web/recall",
              "/api/apps/deploy-web/destroy", "/api/apps/deploy-web/sites",
              "/api/apps/deploy-web/config"):
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
