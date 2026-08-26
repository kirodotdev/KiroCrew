"""dashboard.gitlab_hosts is READ-ONLY over the dashboard-config API.

The GET exposes it so the client knows which pasted merge-request links may
become source tabs, but authorizing a self-managed GitLab instance is a
config-file decision -- the PUT must never persist it. Both settings surfaces
save with ``mutate({ ...dashCfg, ...patch })``, so every GET field returns in the
PUT body; a read-only field therefore has to be dropped rather than rejected, or
it 400s every unrelated toggle save.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader import KiroCrewConfig


@pytest.fixture()
def cfg_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=p):
        yield p


@pytest.fixture()
def mock_sel():
    try:
        import kiro_crew.dashboard.handlers  # noqa: F401
    except ImportError:
        pytest.skip("dashboard handler deps not available locally")
    m = MagicMock()
    m.log_tool_invocation = MagicMock()
    with patch("kiro_crew.dashboard.handlers.sel", return_value=m):
        yield m


@pytest.fixture()
def handler_app(cfg_file, mock_sel):
    from kiro_crew.dashboard.handlers.files import api_dashboard_config

    app = web.Application()
    app.router.add_put("/api/dashboard/config", api_dashboard_config)
    app.router.add_get("/api/dashboard/config", api_dashboard_config)
    return app


@pytest.mark.asyncio
async def test_get_exposes_configured_gitlab_hosts(handler_app, cfg_file):
    cfg_file.write_text(
        json.dumps({"dashboard": {"gitlab_hosts": ["gitlab.acme.internal"]}}),
        encoding="utf-8",
    )
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.get("/api/dashboard/config")
        assert resp.status == 200
        assert (await resp.json())["gitlab_hosts"] == ["gitlab.acme.internal"]


@pytest.mark.asyncio
async def test_round_tripped_gitlab_hosts_does_not_reject_an_unrelated_save(
    handler_app, cfg_file
):
    """Regression: the settings UI spreads the whole GET response into the PUT
    body, so shipping gitlab_hosts on the GET without dropping it on the PUT made
    every dashboard-settings save fail with 400 "Unknown fields"."""
    cfg_file.write_text(
        json.dumps({"dashboard": {"gitlab_hosts": ["gitlab.acme.internal"]}}),
        encoding="utf-8",
    )
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.get("/api/dashboard/config")
        body = await resp.json()
        body["session_grid"] = True  # the user's actual toggle

        resp = await client.put("/api/dashboard/config", json=body)
        assert resp.status == 200, await resp.text()

    cfg = KiroCrewConfig.load()
    assert cfg.dashboard.session_grid is True
    # Untouched by the write -- still whatever the config file says.
    assert cfg.dashboard.gitlab_hosts == ["gitlab.acme.internal"]


@pytest.mark.asyncio
async def test_put_cannot_add_a_gitlab_host(handler_app, cfg_file):
    """The PUT silently drops the field instead of persisting it: a dashboard
    caller must not be able to authorize a new GitLab instance."""
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put(
            "/api/dashboard/config",
            json={"gitlab_hosts": ["gitlab.evil.test"], "quick_send": True},
        )
        assert resp.status == 200, await resp.text()

    cfg = KiroCrewConfig.load()
    assert cfg.dashboard.quick_send is True
    assert cfg.dashboard.gitlab_hosts == []
    # cfg.save() serializes every dataclass field, so the key is present -- what
    # matters is that the caller-supplied host was never adopted.
    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw["dashboard"]["gitlab_hosts"] == []


@pytest.mark.asyncio
async def test_unknown_fields_are_still_rejected(handler_app, cfg_file):
    """The read-only drop must not become a blanket unknown-key bypass."""
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"totally_unknown": 1})
        assert resp.status == 400
        assert "totally_unknown" in (await resp.json())["error"]


@pytest.mark.parametrize(
    "method,tool_name",
    [("GET", "dashboard_config_read"), ("PUT", "dashboard_config_write")],
)
@pytest.mark.asyncio
async def test_cancellation_during_config_load_is_audited(
    mock_sel, monkeypatch, method, tool_name
):
    """The client now polls this endpoint, so the config load runs off-loop via
    ``asyncio.to_thread``. A cancellation at that await (client disconnect,
    gateway shutdown) must still emit a terminal audit: without the guard the
    handler unwinds before the read-success / write-outcome audit, leaving an
    authorized config access absent from the tamper-evident SEL chain. Driven
    without a TestClient because aiohttp turns a handler ``CancelledError`` into
    a connection abort that would mask the re-raise."""
    from kiro_crew.dashboard.handlers import files as files_mod

    async def cancel_load(_fn):
        raise asyncio.CancelledError()

    # Patch the module's ``asyncio.to_thread`` so the cancellation lands exactly
    # at the handler's await rather than depending on executor/future edge cases.
    monkeypatch.setattr(files_mod.asyncio, "to_thread", cancel_load)

    class _FakeRequest:
        def __init__(self, http_method: str) -> None:
            self.method = http_method

        async def json(self):
            return {}

    with pytest.raises(asyncio.CancelledError):
        await files_mod.api_dashboard_config(_FakeRequest(method))

    # Revert-verified: dropping the try/except lets the CancelledError propagate
    # with no audit recorded at all (the load precedes every audit), so this
    # assertion fails.
    call = mock_sel.log_tool_invocation.call_args
    assert call.kwargs["tool_name"] == tool_name
    assert call.kwargs["outcome"] == "failure"
    assert call.kwargs["error"] == "request_cancelled"
