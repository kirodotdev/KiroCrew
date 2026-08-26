"""Tests for federated session search across connected instances.

Covers the two halves added for cross-instance search:

* ``SshTunnelManager.search_sessions_remote`` — the tunnel-side GET that keeps
  the minted token inside the manager (cookie-scoped, one re-mint retry).
* ``GET /api/instances/search-sessions`` — the aggregating endpoint that fans
  out to every CONNECTED peer, merges with the local search by rank
  interleaving, re-shapes untrusted peer rows, and degrades per-peer instead of
  failing the request.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew.dashboard import handlers_instances as hi
from kiro_crew.instances.ssh_tunnel_manager import TunnelState


def _async_value(value):
    async def _inner(*_a, **_k):
        return value

    return _inner


def _enable_instances(monkeypatch):
    monkeypatch.setattr(
        hi.KiroCrewConfig,
        "load",
        staticmethod(lambda: SimpleNamespace(instances=SimpleNamespace(enabled=True))),
    )


class _Req:
    """Request stub mirroring aiohttp's mapping surface: require_auth sets
    request["user"] and request["app"] ("" = the owner's own dashboard
    context; a slug = app-token auth). _guard's owner check is POSITIVE
    (is_owner_dashboard_request), so the default subject is "local-app" —
    with no configured owner_id only the local dashboard subjects pass."""

    def __init__(self, state, query, identity):
        self.app = {"state": state}
        self.match_info = {}
        self.headers = {}
        self.query = query
        self._attrs = identity

    def get(self, key, default=""):
        return self._attrs.get(key, default)

    def __contains__(self, key):
        return key in self._attrs

    def __getitem__(self, key):
        return self._attrs[key]


def _request(state, q="apollo", limit="", app="", user="local-app"):
    query = {"q": q}
    if limit:
        query["limit"] = limit
    return _Req(state, query, {"user": user, "app": app})


class _Log:
    """ConversationLog stub: returns a fixed local ranking."""

    def __init__(self, rows):
        self._rows = rows

    def search_sessions(self, _q, _limit):
        return [dict(r) for r in self._rows]


def _connected_status():
    return SimpleNamespace(state=TunnelState.CONNECTED, local_port=17777)


class _Mgr:
    """Instances manager stub with scriptable per-peer search replies."""

    def __init__(self, replies):
        # replies: {instance_id: (ok, payload)}
        self._replies = replies
        self.calls: list[tuple[str, str, int]] = []

    def status_all(self):
        return {iid: _connected_status() for iid in self._replies}

    async def search_sessions_remote(self, iid, q, limit):
        self.calls.append((iid, q, limit))
        return self._replies[iid]


def _state(local_rows, mgr):
    # The aggregator snapshots names via reg.list() off the loop; expose the
    # ids the stub manager reports, each named crew-<id>.
    ids = list(getattr(mgr, "_replies", {})) if mgr is not None else []
    return SimpleNamespace(
        conversation_log=_Log(local_rows),
        instances_manager=mgr,
        instances_registry=SimpleNamespace(
            get=lambda iid: SimpleNamespace(id=iid, name=f"crew-{iid}"),
            list=lambda: [SimpleNamespace(id=iid, name=f"crew-{iid}") for iid in ids],
        ),
    )


async def _json_of(resp):
    import json

    return json.loads(resp.body.decode())


class TestSearchAllEndpoint:
    @pytest.mark.asyncio
    async def test_rank_interleaves_local_first_and_tags_remote_rows(self, monkeypatch):
        """Position k cycles source k-th hits, local source first; remote rows
        carry instance_id + instance_name so the UI can badge and route them."""
        _enable_instances(monkeypatch)
        mgr = _Mgr({"a": (True, {"sessions": [{"key": "ra1"}, {"key": "ra2"}]})})
        state = _state([{"key": "l1"}, {"key": "l2"}], mgr)

        resp = await hi.api_instances_search_sessions(_request(state))

        assert resp.status == 200
        data = await _json_of(resp)
        keys = [(r["key"], r.get("instance_id")) for r in data["sessions"]]
        assert keys == [("l1", None), ("ra1", "a"), ("l2", None), ("ra2", "a")]
        assert data["sessions"][1]["instance_name"] == "crew-a"
        assert data["unreachable"] == []
        assert mgr.calls and mgr.calls[0][0] == "a"

    @pytest.mark.asyncio
    async def test_unreachable_peer_degrades_without_failing_the_request(self, monkeypatch):
        """A dead peer becomes an ``unreachable`` entry; local results still land."""
        _enable_instances(monkeypatch)
        mgr = _Mgr(
            {
                "up": (True, {"sessions": [{"key": "r1"}]}),
                "down": (False, {"error": "boom", "code": "search_unreachable"}),
            }
        )
        state = _state([{"key": "l1"}], mgr)

        resp = await hi.api_instances_search_sessions(_request(state))

        data = await _json_of(resp)
        assert [r["key"] for r in data["sessions"]] == ["l1", "r1"]
        assert data["unreachable"] == [
            {"id": "down", "name": "crew-down", "code": "search_unreachable"}
        ]

    @pytest.mark.asyncio
    async def test_peer_rows_are_reshaped_and_unknown_fields_dropped(self, monkeypatch):
        """Peer replies are untrusted: non-dict rows and rows without a key are
        dropped, unknown fields never reach the browser, and numeric/string
        fields are type-checked."""
        _enable_instances(monkeypatch)
        mgr = _Mgr(
            {
                "a": (
                    True,
                    {
                        "sessions": [
                            "not-a-dict",
                            {"title": "no key"},
                            {
                                "key": "ok",
                                "title": "fine",
                                "modified": 123.0,
                                "evil_extra": {"nested": "payload"},
                                "messages": "not-a-number",
                            },
                            {
                                # Numeric-field garbage: bool is an int subclass,
                                # a non-finite float would make json.dumps emit
                                # bare `Infinity` (JSON.parse rejects it), and an
                                # int too large for float makes math.isfinite
                                # raise OverflowError — each must drop the FIELD,
                                # never the row or the request.
                                "key": "ok2",
                                "modified": float("inf"),
                                "messages": True,
                            },
                            {
                                "key": "ok3",
                                "modified": 10**400,
                            },
                        ]
                    },
                )
            }
        )
        state = _state([], mgr)

        resp = await hi.api_instances_search_sessions(_request(state))

        data = await _json_of(resp)
        assert len(data["sessions"]) == 3
        row = data["sessions"][0]
        assert row["key"] == "ok" and row["modified"] == 123.0
        assert "evil_extra" not in row and "messages" not in row
        # The rows survive but their garbage numerics are dropped: bool,
        # non-finite, and float-overflowing ints never reach json.dumps.
        row2 = data["sessions"][1]
        assert row2["key"] == "ok2"
        assert "modified" not in row2 and "messages" not in row2
        row3 = data["sessions"][2]
        assert row3["key"] == "ok3" and "modified" not in row3

    @pytest.mark.asyncio
    async def test_peer_string_fields_are_length_clamped(self, monkeypatch):
        """A hostile/broken peer cannot ship unbounded strings to the browser:
        every string field is clamped to _PEER_FIELD_MAX_CHARS before redaction,
        and a row whose KEY exceeds the ceiling is dropped outright (a clamped
        key would collide with a different session's identity)."""
        _enable_instances(monkeypatch)
        huge = "x" * (hi._PEER_FIELD_MAX_CHARS * 4)
        mgr = _Mgr(
            {
                "a": (
                    True,
                    {
                        "sessions": [
                            {"key": "ok", "title": huge, "snippet": huge},
                            {"key": huge, "title": "dropped whole"},
                        ]
                    },
                )
            }
        )
        state = _state([], mgr)

        resp = await hi.api_instances_search_sessions(_request(state))

        data = await _json_of(resp)
        assert len(data["sessions"]) == 1
        row = data["sessions"][0]
        assert len(row["title"]) <= hi._PEER_FIELD_MAX_CHARS
        assert len(row["snippet"]) <= hi._PEER_FIELD_MAX_CHARS

    @pytest.mark.asyncio
    async def test_short_query_returns_empty_without_fanout(self, monkeypatch):
        _enable_instances(monkeypatch)
        mgr = _Mgr({"a": (True, {"sessions": [{"key": "r"}]})})
        state = _state([{"key": "l"}], mgr)

        resp = await hi.api_instances_search_sessions(_request(state, q="x"))

        data = await _json_of(resp)
        assert data == {"sessions": [], "unreachable": []}
        assert mgr.calls == []

    @pytest.mark.asyncio
    async def test_no_manager_serves_local_only(self, monkeypatch):
        """A hub whose instances manager is not running is just a local search."""
        _enable_instances(monkeypatch)
        state = SimpleNamespace(
            conversation_log=_Log([{"key": "l1"}]),
            instances_manager=None,
            instances_registry=SimpleNamespace(get=lambda _i: None),
        )

        resp = await hi.api_instances_search_sessions(_request(state))

        data = await _json_of(resp)
        assert [r["key"] for r in data["sessions"]] == ["l1"]
        assert data["unreachable"] == []

    @pytest.mark.asyncio
    async def test_feature_disabled_is_denied(self, monkeypatch):
        """Same gate as every instances route: disabled feature -> 403, so the
        frontend's fallback-to-local branch is what serves a peerless install."""
        monkeypatch.setattr(
            hi.KiroCrewConfig,
            "load",
            staticmethod(lambda: SimpleNamespace(instances=SimpleNamespace(enabled=False))),
        )
        state = _state([], _Mgr({}))

        resp = await hi.api_instances_search_sessions(_request(state))

        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_app_token_is_denied(self, monkeypatch):
        """The instances control plane is OWNER-only: an app-scoped token sets
        request["user"] too (token_auth exposes identity for app tokens), so
        _guard must positively reject a non-empty request["app"] — otherwise an
        app declaring /api/instances in its scope could read every local and
        remote session's titles/snippets across the app isolation boundary."""
        _enable_instances(monkeypatch)
        state = _state([{"key": "l"}], _Mgr({"a": (True, {"sessions": [{"key": "r"}]})}))

        resp = await hi.api_instances_search_sessions(_request(state, app="some-app"))

        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_non_owner_dashboard_token_is_denied(self, monkeypatch):
        """Owner-only is a POSITIVE check: a Slack user allowed to mint a
        dashboard token via `!dashboard` authenticates with app == "" but a
        non-owner subject, and must not reach the federated search (it would
        disclose every local and remote session's titles/snippets)."""
        _enable_instances(monkeypatch)
        state = _state([{"key": "l"}], _Mgr({"a": (True, {"sessions": [{"key": "r"}]})}))

        resp = await hi.api_instances_search_sessions(
            _request(state, user="slack-minted-user")
        )

        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_redacts_credentials_in_peer_titles_and_snippets(self, monkeypatch):
        """Defense in depth: a peer's rows are re-redacted locally even though a
        well-behaved peer already redacted them."""
        _enable_instances(monkeypatch)
        secret = "AKIAIOSFODNN7EXAMPLE"
        mgr = _Mgr({"a": (True, {"sessions": [{"key": "r", "title": f"creds {secret}"}]})})
        state = _state([], mgr)

        resp = await hi.api_instances_search_sessions(_request(state))

        data = await _json_of(resp)
        assert secret not in data["sessions"][0]["title"]


class TestSearchSessionsRemote:
    """Tunnel-side GET: credential handling mirrors send_session_bundle."""

    def _mgr(self):
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        mgr = SshTunnelManager.__new__(SshTunnelManager)
        mgr._tokens = {"peer": "tok-1"}
        mgr._tunnels = {}
        return mgr

    @pytest.mark.asyncio
    async def test_not_connected_short_circuits(self):
        mgr = self._mgr()
        with patch.object(type(mgr), "status", lambda _s, _i: None):
            ok, payload = await mgr.search_sessions_remote("peer", "q", 10)
        assert not ok and payload["code"] == "search_peer_not_connected"

    @pytest.mark.asyncio
    async def test_missing_token_short_circuits_before_any_request(self):
        mgr = self._mgr()
        mgr._tokens = {}
        with patch.object(type(mgr), "status", lambda _s, _i: _connected_status()):
            ok, payload = await mgr.search_sessions_remote("peer", "q", 10)
        assert not ok and payload["code"] == "search_no_credential"

    @pytest.mark.asyncio
    async def test_redirects_disabled_and_oversized_reply_refused(self):
        """Pins the two hostile-peer guards on the HTTP path: (1) the GET must
        pass allow_redirects=False — a compromised peer answering 30x must not
        make the HUB fetch an attacker-chosen URL (SSRF into its loopback
        control planes); (2) a reply larger than SEARCH_REPLY_MAX_BYTES is
        refused BEFORE JSON decoding, so an unbounded peer stream cannot
        exhaust hub memory ahead of the per-field clamps."""
        import kiro_crew.instances.ssh_tunnel_manager as mod

        seen_kwargs: dict = {}
        big = b'{"pad": "' + b"x" * (mod._SEARCH_REPLY_MAX_BYTES + 64) + b'"}'

        class _Content:
            def __init__(self, body):
                self._body = body

            async def iter_chunked(self, size):
                for i in range(0, len(self._body), size):
                    yield self._body[i : i + size]

        class _Resp:
            status = 200

            def __init__(self, body):
                self.content = _Content(body)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _Session:
            def __init__(self, body):
                self._body = body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def get(self, _url, **kwargs):
                seen_kwargs.update(kwargs)
                return _Resp(self._body)

        mgr = self._mgr()
        original = mod.aiohttp.ClientSession
        mod.aiohttp.ClientSession = lambda *a, **k: _Session(big)  # type: ignore[assignment]
        try:
            with patch.object(type(mgr), "status", lambda _s, _i: _connected_status()):
                ok, payload = await mgr.search_sessions_remote("peer", "q", 10)
        finally:
            mod.aiohttp.ClientSession = original  # type: ignore[assignment]

        assert seen_kwargs.get("allow_redirects") is False
        assert not ok and payload["code"] == "search_malformed_reply"

        # An honest reply arriving in MULTIPLE chunks reassembles to EOF:
        # StreamReader.read(n) would have returned only the first buffered
        # prefix, so this pins the accumulate-to-EOF contract.
        honest = b'{"sessions": [{"key": "' + b"k" * 200_000 + b'"}]}'
        mod.aiohttp.ClientSession = lambda *a, **k: _Session(honest)  # type: ignore[assignment]
        try:
            with patch.object(type(mgr), "status", lambda _s, _i: _connected_status()):
                ok, payload = await mgr.search_sessions_remote("peer", "q", 10)
        finally:
            mod.aiohttp.ClientSession = original  # type: ignore[assignment]
        assert ok is True and payload["sessions"][0]["key"] == "k" * 200_000
