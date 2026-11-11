"""Hosted Jev follows the ceiling's ``endpoints`` list, in the picker payload and the PUT.

Pinned here:

* ``GET /api/decisions/provider`` reports ``hosted_permitted`` as the hosted row AND the
  ceiling's ``endpoints`` list for the hosted default's host;
* ``PUT /api/decisions/provider`` refuses a switch to a hosted address the list refuses with
  ``403 decisions_capability_denied`` and never asks the list about a local preset;
* a pinned hosted row denial withdraws hosted Jev without asking the list.
"""

from __future__ import annotations

import json
from fnmatch import fnmatchcase
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config.sections import DECISION_PROVIDER_ENDPOINT_DEFAULT as DEFAULT_ENDPOINT


def _request(*, app: str = "", user: str = "owner-1", owner: str = "owner-1", body=None):
    """A request shaped like a real DASHBOARD OWNER call (see test_decisions_consent.py)."""
    req = MagicMock()
    req.path = "/api/decisions/provider"
    store = {"app": app, "user": user}
    req.get = lambda key, default=None: store.get(key, default)
    req.__contains__ = lambda _self, key: key in store
    req.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = owner
    req.app = {"state": state}
    req.json = AsyncMock(return_value=body if body is not None else {})
    return req


@pytest.fixture
def audit(monkeypatch):
    import kiro_crew.dashboard.handlers as handlers_pkg

    rows: list[dict] = []
    fake = MagicMock()
    fake.log_api_access = lambda **kw: rows.append(kw)
    monkeypatch.setattr(handlers_pkg, "sel", lambda: fake)
    return rows


@pytest.fixture
def keystone(tmp_path, monkeypatch):
    path = tmp_path / "decisions_consent.json"
    monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: path)
    return path


@pytest.fixture
def provider_config():
    """A reader for the ``decisions.provider`` the pinned data home's config.json holds."""
    from kiro_crew.config.loader import config_path

    path = config_path()

    def _provider() -> dict:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return data.get("decisions", {}).get("provider", {})

    return _provider


@pytest.fixture(autouse=True)
def runtime(_floor_monkeypatch):
    """No local model server may start; the route's runtime calls are recorded."""
    from kiro_crew.decisions import local_runtime

    calls: list = []
    fake = MagicMock()
    fake.status = lambda: {
        "preset": "",
        "state": "idle",
        "port": 0,
        "bytes_done": 0,
        "bytes_total": 0,
        "error": "",
    }
    fake.installed_ids = lambda: []
    fake.activate = lambda m, port: calls.append(("activate", m.id))
    fake.deactivate = lambda **_: calls.append(("deactivate",))
    _floor_monkeypatch.setattr(local_runtime, "get_runtime", lambda: fake)
    return calls




def _ceiling(monkeypatch, *allowed_hosts):
    """A ceiling whose ``endpoints`` list names *allowed_hosts* (globs); returns what was asked."""
    from kiro_crew.decisions import capability
    from kiro_crew.platform.governance_parts.matching import url_policy_hosts

    asked: list[str] = []

    def targets(*_a, point=None, model=None, endpoint=None):
        asked.append(endpoint)
        hosts = url_policy_hosts(endpoint)
        return not all(any(fnmatchcase(h, g) for g in allowed_hosts) for h in hosts)

    monkeypatch.setattr(capability, "targets_denied", targets)
    return asked


async def _get() -> dict:
    from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_get

    return json.loads((await api_decisions_provider_get(_request())).text)


async def _put(body, **kw):
    from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

    resp = await api_decisions_provider_put(_request(body=body, **kw))
    return resp.status, json.loads(resp.text)


LOOPBACK_ONLY = ("127.0.0.1",)


class TestHostedJevFollowsTheEndpointsList:
    @pytest.mark.asyncio
    async def test_no_ceiling_offers_hosted(self, audit):
        assert (await _get())["hosted_permitted"] is True

    @pytest.mark.asyncio
    async def test_a_list_that_refuses_the_default_host_hides_hosted_and_keeps_local(
        self, audit, monkeypatch
    ):
        asked = _ceiling(monkeypatch, *LOOPBACK_ONLY)
        payload = await _get()
        assert payload["hosted_permitted"] is False
        assert payload["local_permitted"] is True
        assert asked == [DEFAULT_ENDPOINT]

    @pytest.mark.asyncio
    async def test_a_list_that_permits_the_default_host_offers_hosted(self, audit, monkeypatch):
        _ceiling(monkeypatch, "*")
        assert (await _get())["hosted_permitted"] is True

    @pytest.mark.asyncio
    async def test_a_hosted_row_deny_withdraws_hosted_without_asking_the_list(
        self, audit, monkeypatch
    ):
        from kiro_crew.decisions import capability

        asked = _ceiling(monkeypatch, "*")
        monkeypatch.setattr(capability, "denied_sides", lambda *a, **k: (True, False))
        assert (await _get())["hosted_permitted"] is False
        assert asked == []

    @pytest.mark.asyncio
    async def test_put_refuses_hosted_jev_where_the_list_refuses_its_host(
        self, audit, provider_config, monkeypatch
    ):
        from kiro_crew.decisions import capability

        monkeypatch.setattr(capability, "is_decisions_denied", lambda *a, **k: False)
        _ceiling(monkeypatch, *LOOPBACK_ONLY)
        status, body = await _put({"preset": "jev"})
        assert status == 403 and body["code"] == "decisions_capability_denied"
        assert provider_config() == {}
        status, _ = await _put({"preset": "none"})
        assert status == 200

    @pytest.mark.asyncio
    async def test_a_local_preset_is_never_asked_about_the_endpoints_list(
        self, audit, provider_config, monkeypatch
    ):
        from kiro_crew.decisions import capability

        monkeypatch.setattr(capability, "is_decisions_denied", lambda *a, **k: False)
        asked = _ceiling(monkeypatch)
        status, _ = await _put({"preset": "laya"})
        assert status == 200
        assert not any("127.0.0.1" in url for url in asked)
