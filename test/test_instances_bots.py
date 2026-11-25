"""``GET /api/instances/bots``: the bots on every connected crew, for the Bots page.

One ``/api/agents`` read per CONNECTED crew through the closed-path capability
carrier, reshaped to the two fields the page renders, every string redacted and
clamped; a crew whose read fails is listed with the carrier's code instead of
being left out.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from redaction_structured_cases import github_classic

from kiro_crew.dashboard import handlers_instances as hi
from kiro_crew.instances.ssh_tunnel_manager import TunnelState


class _Req:
    """Request stub mirroring aiohttp's mapping surface (see the versions tests)."""

    def __init__(self, state, identity):
        self.app = {"state": state}
        self.match_info: dict[str, str] = {}
        self.headers: dict[str, str] = {}
        self.query: dict[str, str] = {}
        self._attrs = identity

    def get(self, key, default=""):
        return self._attrs.get(key, default)

    def __contains__(self, key):
        return key in self._attrs

    def __getitem__(self, key):
        return self._attrs[key]


def _request(state, app=""):
    return _Req(state, {"user": "local-app", "app": app})


def _state(replies, states):
    """A manager whose crews answer ``/api/agents`` from *replies* (an exception raises)."""
    calls: list[tuple[str, str]] = []

    async def _peer_capability(iid, path):
        calls.append((iid, path))
        outcome = replies[iid]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    mgr = SimpleNamespace(
        peer_capability=_peer_capability,
        status_all=lambda: {iid: SimpleNamespace(state=s) for iid, s in states.items()},
    )
    return SimpleNamespace(instances_manager=mgr), calls


@pytest.fixture(autouse=True)
def _instances_enabled(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        hi.KiroCrewConfig,
        "load",
        staticmethod(lambda: SimpleNamespace(instances=SimpleNamespace(enabled=True))),
    )


@pytest.mark.asyncio
class TestBotsRoute:
    async def test_a_connected_crews_roster_is_reshaped_and_redacted(self):
        token = github_classic(seed=39808)
        roster = {
            "agents": [
                {
                    "name": "scout",
                    "display_name": f"Scout {token}",
                    "description": "Finds things",
                    "model": "auto",
                    "scope": "global",
                },
            ],
            "default_agent": "scout",
        }
        state, calls = _state(
            {"a": (True, roster)},
            {"a": TunnelState.CONNECTED, "b": TunnelState.ERROR},
        )

        resp = await hi.api_instances_bots(_request(state))

        assert resp.status == 200
        body = resp.body.decode()
        assert token not in body
        [crew] = json.loads(body)["crews"]
        assert crew["id"] == "a" and crew["error"] == ""
        assert crew["omitted"] == 0
        [bot] = crew["bots"]
        assert set(bot) == {"name", "display_name"}
        assert bot["name"] == "scout"
        assert bot["display_name"].startswith("Scout ")
        assert calls == [("a", "/api/agents")]

    async def test_a_failed_or_raising_read_is_listed_with_its_code(self):
        state, _ = _state(
            {
                "a": (False, {"code": "capability_peer_too_old"}),
                "b": ConnectionResetError("tunnel died"),
            },
            {"a": TunnelState.CONNECTED, "b": TunnelState.CONNECTED},
        )

        data = json.loads((await hi.api_instances_bots(_request(state))).body.decode())

        assert data["crews"] == [
            {"id": "a", "bots": [], "error": "capability_peer_too_old", "omitted": 0},
            {"id": "b", "bots": [], "error": "capability_unreachable", "omitted": 0},
        ]

    async def test_rows_past_the_cap_are_counted_not_silently_dropped(self):
        roster = {"agents": [{"name": f"bot-{i}"} for i in range(hi._CAP_MAX_ROWS + 1)]}
        state, _ = _state({"a": (True, roster)}, {"a": TunnelState.CONNECTED})

        data = json.loads((await hi.api_instances_bots(_request(state))).body.decode())

        [crew] = data["crews"]
        assert len(crew["bots"]) == hi._CAP_MAX_ROWS
        assert crew["omitted"] == 1
        assert crew["error"] == ""

    async def test_an_app_token_is_refused_before_any_read(self):
        state, calls = _state({"a": (True, {"agents": []})}, {"a": TunnelState.CONNECTED})

        resp = await hi.api_instances_bots(_request(state, app="some-app"))

        assert resp.status == 403
        assert calls == []
