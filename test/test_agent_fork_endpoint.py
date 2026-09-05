"""Tests for api_agent_fork — POST /api/agents/detail/{name}/fork.

Blueprint semantics: a crew's first definition edit forks a private copy of the
shared template (named after the crew), records lineage in the agent_state
sidecar, copies model tracking, and rebinds the crew — all under the config
lock. It is idempotent (a second fork of a copy already private to the crew is a
no-op) and validates its inputs (400 on bad body / missing crew, 404 on unknown
template or unknown crew).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew import agent_state
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard.handlers.agents import api_agent_fork


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch):
    """Run past the owner boundary; owner-auth has its own coverage elsewhere."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _fork_request(name: str, body, *, bad_json: bool = False):
    request = MagicMock(spec=web.Request)
    request.method = "POST"
    request.match_info = {"name": name}
    request.app = {"state": MagicMock()}

    async def _json():
        if bad_json:
            raise ValueError("not json")
        return body

    request.json = _json
    return request


def _write_template(agents_dir, stem: str, **extra) -> None:
    spec = {"name": stem, "model": "claude-x", "tools": ["ReadFile"]}
    spec.update(extra)
    (agents_dir / f"{stem}.json").write_text(json.dumps(spec), encoding="utf-8")


def _seed_config(crew: str, kiro_agent: str) -> None:
    """Persist a config.json (in the isolated home) with one crew bound to a template."""
    cfg = KiroCrewConfig()
    cfg.agents = {crew: KiroCrewAgentConfig(kiro_agent=kiro_agent)}
    cfg.default_agent = crew
    cfg.save()


@pytest.mark.asyncio
async def test_fork_happy_path(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_template(agents_dir, "mytemplate")
    _seed_config("design-crew", "mytemplate")

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_fork(_fork_request("mytemplate", {"crew": "design-crew"}))

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body == {
        "ok": True,
        "template": "design-crew",
        "filename": "design-crew.json",
        "forked_from": "mytemplate",
    }

    # File created, its declared name equals the file stem.
    copy = json.loads((agents_dir / "design-crew.json").read_text(encoding="utf-8"))
    assert copy["name"] == "design-crew"
    assert copy["model"] == "claude-x"
    # Sidecar lineage recorded.
    assert agent_state.get_fork_info("design-crew") == {
        "forked_from": "mytemplate",
        "private_to": "design-crew",
    }
    # Rebind persisted to config.json.
    reloaded = KiroCrewConfig.load()
    assert reloaded.agents["design-crew"].kiro_agent == "design-crew"


@pytest.mark.asyncio
async def test_fork_copies_model_managed(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_template(agents_dir, "mytemplate")
    _seed_config("crew-a", "mytemplate")
    agent_state.set_model_managed("mytemplate", True)

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_fork(_fork_request("mytemplate", {"crew": "crew-a"}))

    assert resp.status == 200
    assert agent_state.get_model_managed("crew-a") is True


@pytest.mark.asyncio
async def test_fork_collision_gets_numeric_suffix(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_template(agents_dir, "mytemplate")
    # An unrelated file already owns the sanitized crew name.
    _write_template(agents_dir, "design-crew")
    # Crew name "design crew" sanitizes to "design-crew", which is taken.
    _seed_config("design crew", "mytemplate")

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_fork(_fork_request("mytemplate", {"crew": "design crew"}))

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["template"] == "design-crew-2"
    assert (agents_dir / "design-crew-2.json").exists()
    # The pre-existing unrelated file is untouched.
    assert json.loads((agents_dir / "design-crew.json").read_text())["name"] == "design-crew"


@pytest.mark.asyncio
async def test_fork_idempotent_second_call(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_template(agents_dir, "mytemplate")
    _seed_config("c", "mytemplate")

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        first = await api_agent_fork(_fork_request("mytemplate", {"crew": "c"}))
        assert first.status == 200
        # The crew now points at its copy "c"; a repeat fork-before-edit call
        # names that copy and must be a no-op.
        second = await api_agent_fork(_fork_request("c", {"crew": "c"}))

    assert second.status == 200
    body = json.loads(second.text)
    assert body == {"ok": True, "template": "c", "already_private": True}
    # No -2 copy was created.
    assert not (agents_dir / "c-2.json").exists()


@pytest.mark.asyncio
async def test_fork_unknown_template_404(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_config("crew-a", "whatever")

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_fork(_fork_request("nonexistent", {"crew": "crew-a"}))

    assert resp.status == 404
    assert "not found" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_fork_unknown_crew_404(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_template(agents_dir, "mytemplate")
    _seed_config("crew-a", "mytemplate")

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_fork(_fork_request("mytemplate", {"crew": "ghost-crew"}))

    assert resp.status == 404
    # No copy is written when the crew is unknown.
    assert list(agents_dir.glob("ghost*")) == []


@pytest.mark.asyncio
async def test_fork_invalid_json_400(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_fork(_fork_request("mytemplate", None, bad_json=True))

    assert resp.status == 400


@pytest.mark.asyncio
async def test_fork_non_object_body_400(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_fork(_fork_request("mytemplate", ["not", "an", "object"]))

    assert resp.status == 400


@pytest.mark.asyncio
async def test_fork_missing_crew_400(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_fork(_fork_request("mytemplate", {"crew": "   "}))

    assert resp.status == 400
    assert "crew is required" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_fork_stale_binding_409(tmp_path):
    """A fork naming a template the crew is no longer bound to is refused,
    so a stale or racing request cannot clobber the newer binding."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_template(agents_dir, "oldtemplate")
    _write_template(agents_dir, "newtemplate")
    _seed_config("design-crew", "newtemplate")

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_fork(_fork_request("oldtemplate", {"crew": "design-crew"}))

    assert resp.status == 409
    assert json.loads(resp.text)["code"] == "stale_binding"
    # Nothing forked, nothing rebound.
    assert not (agents_dir / "design-crew.json").exists()
    assert KiroCrewConfig.load().agents["design-crew"].kiro_agent == "newtemplate"


@pytest.mark.asyncio
async def test_fork_bounds_overlong_crew_name(tmp_path):
    """A 200-char crew name must yield a bounded copy filename, not OSError."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    crew = "c" * 200
    _write_template(agents_dir, "mytemplate")
    _seed_config(crew, "mytemplate")

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_fork(_fork_request("mytemplate", {"crew": crew}))

    assert resp.status == 200
    filename = json.loads(resp.text)["filename"]
    # 48-char base cap leaves room for a collision suffix under the 63-char rule.
    assert len(filename) <= len("48chars") + 60 and (agents_dir / filename).exists()
    assert len(Path(filename).stem) <= 52


@pytest.mark.asyncio
async def test_fork_bookkeeping_failure_compensates(tmp_path):
    """A sidecar failure AFTER the copy is created must undo the file too —
    an unbound copy would surface as a shared template (GPT round-8)."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_template(agents_dir, "mytemplate")
    _seed_config("design-crew", "mytemplate")

    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir),
        patch(
            "kiro_crew.dashboard.handlers.agents.agent_state.set_fork_info",
            side_effect=RuntimeError("sidecar unavailable"),
        ),
    ):
        resp = await api_agent_fork(_fork_request("mytemplate", {"crew": "design-crew"}))

    assert resp.status == 500
    assert json.loads(resp.text)["code"] == "bookkeeping_failed"
    assert not (agents_dir / "design-crew.json").exists()
    assert agent_state.get_fork_info("design-crew") is None


@pytest.mark.asyncio
async def test_fork_suffixes_past_reserved_windows_basename(tmp_path):
    """A crew named like a Windows device file (con) must not yield con.json —
    generated names suffix past reserved basenames (GPT round-10)."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_template(agents_dir, "mytemplate")
    _seed_config("con", "mytemplate")

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_fork(_fork_request("mytemplate", {"crew": "con"}))

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["template"] == "con-2"
    assert not (agents_dir / "con.json").exists()
    assert (agents_dir / "con-2.json").exists()


def test_write_spec_file_unlinks_failed_create(tmp_path):
    """A partial write (ENOSPC) must not leave a truncated spec occupying the
    name — the failed exclusive create is unlinked (GPT round-10)."""
    from unittest.mock import ANY  # noqa: F401 — parity with module style

    from kiro_crew.dashboard.handlers.agents import _write_spec_file

    dest = tmp_path / "newspec.json"
    with patch(
        "kiro_crew.platform.governance.sanitize_agent_config_governance",
        side_effect=RuntimeError("disk full mid-serialize"),
    ):
        with pytest.raises(RuntimeError):
            _write_spec_file(dest, {"name": "newspec"})
    # NOTE: sanitize runs before open('x'); simulate the WRITE failing instead.
    real_open = open
    calls = {"n": 0}

    class _FailingFile:
        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            self._fh.__enter__()
            return self

        def __exit__(self, *a):
            return self._fh.__exit__(*a)

        def write(self, _data):
            raise OSError(28, "No space left on device")

    def failing_open(path, mode="r", **kwargs):
        fh = real_open(path, mode, **kwargs)
        if "x" in mode:
            calls["n"] += 1
            return _FailingFile(fh)
        return fh

    with patch("builtins.open", side_effect=failing_open):
        with pytest.raises(OSError):
            _write_spec_file(dest, {"name": "newspec"})
    assert calls["n"] == 1
    assert not dest.exists()
