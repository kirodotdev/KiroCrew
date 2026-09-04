"""``GET /api/instances/{id}/capabilities`` — the peer's rosters, re-shaped.

A session bound to a peer for execution must offer the PEER's agents, models,
effort levels and workspaces in its header; the local ones describe a machine
that is not answering the turn, so a wrong pick is accepted by the picker and
refused on send. These tests pin the two halves that makes true: the **shape**
each peer endpoint answers with (they do not agree, and a mismatch degrades to a
silently empty menu rather than an error), and the **clamping** applied to a
peer's reply, which is untrusted input on its way to the browser.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import kiro_crew
from kiro_crew.dashboard import handlers_instances as hi


class _Req:
    """Request stub mirroring aiohttp's mapping surface.

    ``user`` is "local-app" because the handler's owner gate is POSITIVE
    (``is_owner_dashboard_request``): with no configured owner_id only the local
    dashboard subjects pass, so a bare truthy user would fail the second gate.
    """

    def __init__(self, state, instance_id, identity):
        self.app = {"state": state}
        self.match_info = {"id": instance_id}
        self.headers: dict[str, str] = {}
        self.query: dict[str, str] = {}
        self._attrs = identity

    def get(self, key, default=""):
        return self._attrs.get(key, default)

    def __contains__(self, key):
        return key in self._attrs

    def __getitem__(self, key):
        return self._attrs[key]


def _request(state, instance_id="nobita", app="", user="local-app"):
    return _Req(state, instance_id, {"user": user, "app": app})


def _enable_instances(monkeypatch):
    monkeypatch.setattr(
        hi.KiroCrewConfig,
        "load",
        staticmethod(lambda: SimpleNamespace(instances=SimpleNamespace(enabled=True))),
    )


def _state(replies, *, raises: set[str] | None = None):
    """A state whose manager answers ``peer_capability`` from *replies* by path."""
    raises = raises or set()

    async def _peer_capability(_iid, path):
        if path in raises:
            raise ConnectionResetError("tunnel died")
        return replies.get(path, (False, {"code": "capability_error"}))

    return SimpleNamespace(instances_manager=SimpleNamespace(peer_capability=_peer_capability))


def _all_ok(**overrides):
    """Every capability read succeeding, in each endpoint's REAL reply shape."""
    replies = {
        "/api/version": (True, {"version": kiro_crew.__version__}),
        # A dict with the list under a key, next to a sibling default — this is
        # what `GET /api/agents` actually answers with.
        "/api/agents": (True, {"agents": [{"name": "coder"}], "default_agent": "coder"}),
        # A BARE list — `GET /api/models` answers with no envelope at all.
        "/api/models": (True, [{"model_name": "sonnet", "context_window": 200000}]),
        "/api/effort-levels": (True, ["low", "high"]),
        "/api/workspaces": (
            True,
            {"workspaces": [{"name": "main", "path": "/w"}], "default": "main"},
        ),
    }
    replies.update(overrides)
    return replies


async def _body(resp):
    return json.loads(resp.body.decode())


@pytest.mark.asyncio
class TestPayloadShapes:
    """Each peer endpoint answers differently; all four must reach the picker."""

    async def test_a_dict_wrapped_agents_reply_still_populates_the_picker(self, monkeypatch):
        """The regression that matters most: agents arrive WRAPPED.

        ``GET /api/agents`` answers ``{"agents": [...], "default_agent": ...}``
        while ``_cap_rows`` accepts only a list. Handing it the dict returns
        ``[]``, which is indistinguishable in the UI from a crew that genuinely
        has no agents — so the bound session's agent picker would be empty on
        every healthy peer, and nothing would report an error.
        """
        _enable_instances(monkeypatch)
        state = _state(_all_ok())

        resp = await hi.api_instances_capabilities(_request(state))

        assert resp.status == 200
        data = await _body(resp)
        assert [row["name"] for row in data["agents"]] == ["coder"]
        assert data["unavailable"] == {}

    async def test_the_peers_default_agent_is_carried_beside_its_roster(self, monkeypatch):
        """What answers before the user picks anything.

        A bound session records NO agent at create time (this machine's default
        names a crew from this machine's roster), so the header has to render the
        peer's default or it would advertise the wrong crew.
        """
        _enable_instances(monkeypatch)
        state = _state(_all_ok())

        data = await _body(await hi.api_instances_capabilities(_request(state)))

        assert data["default_agent"] == "coder"

    async def test_an_unreadable_roster_reports_no_default_agent(self, monkeypatch):
        """ "" rather than a guess: the caller must not substitute the local default."""
        _enable_instances(monkeypatch)
        state = _state(_all_ok(**{"/api/agents": (False, {"code": "agent_list_failed"})}))

        data = await _body(await hi.api_instances_capabilities(_request(state)))

        assert data["default_agent"] == ""
        assert data["unavailable"]["agents"] == "agent_list_failed"

    async def test_a_bare_models_list_is_accepted_unwrapped(self, monkeypatch):
        _enable_instances(monkeypatch)
        state = _state(_all_ok())

        data = await _body(await hi.api_instances_capabilities(_request(state)))

        assert data["models"] == [
            {
                "model_name": "sonnet",
                "display_name": "",
                "description": "",
                "context_window": 200000,
            }
        ]

    async def test_workspaces_carry_their_rows_and_the_peers_default(self, monkeypatch):
        _enable_instances(monkeypatch)
        state = _state(_all_ok())

        data = await _body(await hi.api_instances_capabilities(_request(state)))

        assert data["workspaces"] == [{"name": "main", "path": "/w"}]
        assert data["default_workspace"] == "main"

    async def test_effort_levels_keep_only_strings(self, monkeypatch):
        _enable_instances(monkeypatch)
        state = _state(_all_ok(**{"/api/effort-levels": (True, ["low", 7, None, "high"])}))

        data = await _body(await hi.api_instances_capabilities(_request(state)))

        assert data["effort_levels"] == ["low", "high"]


@pytest.mark.asyncio
class TestVersionGate:
    async def test_an_equal_version_reports_a_match(self, monkeypatch):
        _enable_instances(monkeypatch)
        state = _state(_all_ok())

        data = await _body(await hi.api_instances_capabilities(_request(state)))

        assert data["version"] == kiro_crew.__version__
        assert data["local_version"] == kiro_crew.__version__
        assert data["version_match"] is True

    async def test_a_skewed_peer_reports_no_match(self, monkeypatch):
        """Surfaced BEFORE the first send, which is the point of shipping it.

        The relay refuses a version-skewed dispatch anyway; reporting it here is
        what lets the UI explain the refusal while the composer is still empty
        rather than after the user has typed a message.
        """
        _enable_instances(monkeypatch)
        state = _state(_all_ok(**{"/api/version": (True, {"version": "0.0.1"})}))

        data = await _body(await hi.api_instances_capabilities(_request(state)))

        assert data["version"] == "0.0.1"
        assert data["version_match"] is False

    async def test_an_unreported_version_is_not_a_match(self, monkeypatch):
        _enable_instances(monkeypatch)
        state = _state(_all_ok(**{"/api/version": (False, {"code": "capability_peer_too_old"})}))

        data = await _body(await hi.api_instances_capabilities(_request(state)))

        assert data["version"] == ""
        assert data["version_match"] is False
        assert data["unavailable"]["version"] == "capability_peer_too_old"


@pytest.mark.asyncio
class TestPartialDegradation:
    async def test_one_failed_read_does_not_fail_the_others(self, monkeypatch):
        """Per-control degradation, so the UI can disable exactly what is missing.

        Failing the whole request would blank a shelf whose other three controls
        are perfectly usable.
        """
        _enable_instances(monkeypatch)
        state = _state(_all_ok(**{"/api/models": (False, {"code": "model_list_timeout"})}))

        data = await _body(await hi.api_instances_capabilities(_request(state)))

        assert data["models"] == []
        assert data["unavailable"] == {"models": "model_list_timeout"}
        assert [row["name"] for row in data["agents"]] == ["coder"]

    async def test_a_raising_read_is_reported_as_unreachable(self, monkeypatch):
        _enable_instances(monkeypatch)
        state = _state(_all_ok(), raises={"/api/agents"})

        data = await _body(await hi.api_instances_capabilities(_request(state)))

        assert data["agents"] == []
        assert data["unavailable"] == {"agents": "capability_unreachable"}

    async def test_a_non_dict_error_payload_still_names_a_code(self, monkeypatch):
        _enable_instances(monkeypatch)
        state = _state(_all_ok(**{"/api/workspaces": (False, "boom")}))

        data = await _body(await hi.api_instances_capabilities(_request(state)))

        assert data["unavailable"] == {"workspaces": "capability_error"}
        assert data["workspaces"] == []
        assert data["default_workspace"] == ""


@pytest.mark.asyncio
class TestPeerReplyClamping:
    """A peer is untrusted input on its way to the browser."""

    async def test_unlisted_fields_are_dropped_not_forwarded(self, monkeypatch):
        _enable_instances(monkeypatch)
        state = _state(
            _all_ok(
                **{
                    "/api/agents": (
                        True,
                        {"agents": [{"name": "coder", "system_prompt": "leak", "tools": ["fs"]}]},
                    )
                }
            )
        )

        data = await _body(await hi.api_instances_capabilities(_request(state)))

        assert set(data["agents"][0]) == {"name", "description", "scope", "model"}

    async def test_an_overlong_string_is_truncated(self, monkeypatch):
        _enable_instances(monkeypatch)
        state = _state(
            _all_ok(
                **{
                    "/api/agents": (
                        True,
                        {"agents": [{"name": "x" * 500, "description": "y" * 900}]},
                    )
                }
            )
        )

        data = await _body(await hi.api_instances_capabilities(_request(state)))

        assert len(data["agents"][0]["name"]) == 128
        assert len(data["agents"][0]["description"]) == hi._CAP_MAX_STR

    async def test_a_bogus_context_window_reads_as_unknown(self, monkeypatch):
        """0 is the frontend's "unknown", which falls back to the reference window.

        ``True`` is the case worth pinning: it is an ``int`` in Python, so a bare
        isinstance check would forward ``1`` as a context window of one token.
        """
        _enable_instances(monkeypatch)
        state = _state(
            _all_ok(
                **{
                    "/api/models": (
                        True,
                        [
                            {"model_name": "a", "context_window": "200000"},
                            {"model_name": "b", "context_window": True},
                        ],
                    )
                }
            )
        )

        data = await _body(await hi.api_instances_capabilities(_request(state)))

        assert [row["context_window"] for row in data["models"]] == [0, 0]

    async def test_a_flood_of_rows_is_capped(self, monkeypatch):
        _enable_instances(monkeypatch)
        rows = [{"model_name": f"m{i}"} for i in range(hi._CAP_MAX_ROWS + 50)]
        state = _state(_all_ok(**{"/api/models": (True, rows)}))

        data = await _body(await hi.api_instances_capabilities(_request(state)))

        assert len(data["models"]) == hi._CAP_MAX_ROWS

    async def test_non_dict_rows_are_skipped(self, monkeypatch):
        _enable_instances(monkeypatch)
        state = _state(_all_ok(**{"/api/models": (True, ["sonnet", None, {"model_name": "opus"}])}))

        data = await _body(await hi.api_instances_capabilities(_request(state)))

        assert [row["model_name"] for row in data["models"]] == ["opus"]


@pytest.mark.asyncio
class TestGates:
    async def test_no_manager_is_a_503_not_an_empty_roster(self, monkeypatch):
        """503 so the client retries; an empty 200 would cache "no models"."""
        _enable_instances(monkeypatch)
        state = SimpleNamespace(instances_manager=None)

        resp = await hi.api_instances_capabilities(_request(state))

        assert resp.status == 503
        assert (await _body(resp))["code"] == "instances_unavailable"

    async def test_the_feature_flag_is_enforced(self, monkeypatch):
        monkeypatch.setattr(
            hi.KiroCrewConfig,
            "load",
            staticmethod(lambda: SimpleNamespace(instances=SimpleNamespace(enabled=False))),
        )

        resp = await hi.api_instances_capabilities(_request(_state(_all_ok())))

        assert resp.status == 403

    async def test_an_unauthenticated_caller_is_refused(self, monkeypatch):
        _enable_instances(monkeypatch)

        resp = await hi.api_instances_capabilities(_request(_state(_all_ok()), user=""))

        assert resp.status == 401

    async def test_a_slack_origin_cannot_read_a_peers_rosters(self, monkeypatch):
        _enable_instances(monkeypatch)
        request = _request(_state(_all_ok()))
        request.headers["X-Session-Key"] = "slack:C123"

        resp = await hi.api_instances_capabilities(request)

        assert resp.status == 403

    async def test_a_non_owner_dashboard_subject_is_refused(self, monkeypatch):
        """The reads spend the OWNER's manager-held peer credential.

        A Slack-invited user holding a `!dashboard` link is an authenticated
        subject with an empty app, so `_guard` alone would let them through.
        """
        _enable_instances(monkeypatch)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda _r: False,
        )

        resp = await hi.api_instances_capabilities(_request(_state(_all_ok())))

        assert resp.status in (401, 403)


class TestUnwrapHelper:
    """``_cap_list`` is what keeps a shape mismatch from becoming an empty menu."""

    def test_a_bare_list_passes_through(self):
        assert hi._cap_list([{"a": 1}], "agents") == [{"a": 1}]

    def test_a_wrapped_list_is_unwrapped_by_key(self):
        assert hi._cap_list({"agents": [{"a": 1}], "default_agent": "x"}, "agents") == [{"a": 1}]

    def test_a_wrapper_without_the_key_yields_nothing_usable(self):
        assert hi._cap_list({"other": [1]}, "agents") is None

    def test_a_scalar_is_left_for_cap_rows_to_reject(self):
        assert hi._cap_rows(hi._cap_list("nonsense", "agents"), {"name": 8}) == []


class TestPeerCapabilityCarrier:
    """The carrier is a closed path set, not a widening of the proxy fence."""

    def test_the_path_set_holds_exactly_the_five_reads(self):
        from kiro_crew.instances.ssh_tunnel_manager import _PEER_CAPABILITY_PATHS

        assert _PEER_CAPABILITY_PATHS == frozenset(
            {
                "/api/version",
                "/api/agents",
                "/api/models",
                "/api/effort-levels",
                "/api/workspaces",
            }
        )

    @pytest.mark.asyncio
    async def test_a_path_outside_the_set_raises_before_the_tunnel(self):
        """The fence is checked BEFORE any target is resolved.

        ``api/agents`` under the proxy's *prefix* fence would also have admitted
        the peer's mutating ``PUT /api/agents/{name}``; this carrier cannot
        express a path it does not list. A raise rather than a ``(False, …)`` is
        deliberate: every caller passes a literal from the set, so reaching here
        is a programming error and must not degrade into a soft "unavailable"
        that reads like an offline peer.
        """
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        mgr = SshTunnelManager.__new__(SshTunnelManager)
        mgr._peer_target = MagicMock()

        with pytest.raises(ValueError):
            await mgr.peer_capability("nobita", "/api/agents/evil")
        mgr._peer_target.assert_not_called()
