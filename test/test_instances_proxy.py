"""``SshTunnelManager.proxy_request``, the remote-crew chat carrier to a peer.

Includes the peer target and cookie derivation it shares with
``send_session_bundle`` and ``search_sessions_remote``.
"""

from __future__ import annotations

import asyncio

import pytest
from instances_helpers import _FakeTunnel, _patch_port_probe


class TestProxyRequest:
    """SshTunnelManager.proxy_request — the remote-crew chat carrier."""

    @pytest.fixture(autouse=True)
    # flake-ok: pre-existing autouse port-probe stub on the shared monkeypatch, moved verbatim; tracked for a de-flake pass
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class-local autouse stub, moved verbatim; de-flake pass tracked
    def _free_ports(self, monkeypatch):
        # connect() re-probes the fixed base port 53500 for real; on a busy macOS
        # runner something else can hold it, connect() returns an error status, and
        # the remint test then answers False.
        _patch_port_probe(monkeypatch)

    def _mgr(self, tmp_path, *, mint=None, factory=_FakeTunnel):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        reg = InstancesRegistry(path=tmp_path / "instances.json")

        async def ok_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            return "SECRET_TOK"

        return reg, SshTunnelManager(
            reg, base_port=53500, mint_token=mint or ok_mint, tunnel_factory=factory
        )

    @staticmethod
    def _fake_session(calls, *, statuses):
        """A ClientSession stand-in recording request kwargs; statuses pop per call."""

        class _Resp:
            def __init__(self, status):
                self.status = status

            def release(self):
                return None

        class _Sess:
            def __init__(self, *a, **k):
                self.closed = False

            async def request(self, method, url, **kwargs):
                calls.append({"method": method, "url": url, **kwargs})
                return _Resp(statuses.pop(0))

            async def close(self):
                self.closed = True

        return _Sess

    @pytest.mark.asyncio
    async def test_not_connected_raises_typed_error(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

        _reg, mgr = self._mgr(tmp_path)
        with pytest.raises(ProxyRequestError) as ei:
            async with mgr.proxy_request("nope", "GET", "api/status"):
                pass
        assert ei.value.code == "proxy_peer_not_connected"
        assert ei.value.http_status == 503

    @pytest.mark.asyncio
    async def test_success_sends_port_scoped_cookie_and_refuses_redirects(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.instances import ssh_tunnel_manager as m

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        st = await mgr.connect("cd-1")
        calls: list = []
        exchanged: list = []

        async def fake_exchange(url, link, cookie_name):
            exchanged.append((url, link, cookie_name))
            return "SESSION_TOK"

        monkeypatch.setattr(mgr, "_exchange_link", fake_exchange)
        monkeypatch.setattr(m.aiohttp, "ClientSession", self._fake_session(calls, statuses=[200]))

        async with mgr.proxy_request(
            "cd-1", "POST", "/api/chat", params={"a": "b"}, data=b"{}"
        ) as resp:
            assert resp.status == 200
        assert len(calls) == 1
        call = calls[0]
        assert call["method"] == "POST"
        assert call["url"] == f"http://127.0.0.1:{st.local_port}/api/chat"
        # The minted link is exchanged once; the SESSION it yields travels as
        # the PORT-SCOPED cookie, never the link and never a bare name.
        assert exchanged == [
            (
                f"http://127.0.0.1:{st.local_port}/api/chat",
                "SECRET_TOK",
                f"mc_token_{st.local_port}",
            )
        ]
        assert call["headers"]["Cookie"] == f"mc_token_{st.local_port}=SESSION_TOK"
        # SSRF guard: a compromised peer answering 30x must not steer the hub.
        assert call["allow_redirects"] is False

    @pytest.mark.asyncio
    async def test_401_gets_exactly_one_remint_retry(self, tmp_path, monkeypatch):
        from kiro_crew.instances import ssh_tunnel_manager as m

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")
        calls: list = []
        monkeypatch.setattr(
            m.aiohttp, "ClientSession", self._fake_session(calls, statuses=[401, 200])
        )
        remints = []

        async def fake_refresh(instance_id):
            remints.append(instance_id)
            mgr._tokens[instance_id] = "FRESH_TOK"
            return "FRESH_TOK"

        monkeypatch.setattr(mgr, "refresh_token", fake_refresh)

        async def fake_exchange(url, link, cookie_name):
            return f"S-{link}"

        monkeypatch.setattr(mgr, "_exchange_link", fake_exchange)

        async with mgr.proxy_request("cd-1", "GET", "api/chat/slots") as resp:
            assert resp.status == 200
        assert remints == ["cd-1"]
        assert len(calls) == 2
        # The re-mint retires the cached session: the retry exchanges the new link.
        assert "S-SECRET_TOK" in calls[0]["headers"]["Cookie"]
        assert "S-FRESH_TOK" in calls[1]["headers"]["Cookie"]

    @pytest.mark.asyncio
    async def test_session_is_exchanged_once_per_minted_link(self, tmp_path, monkeypatch):
        from kiro_crew.instances import ssh_tunnel_manager as m

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")
        calls: list = []
        exchanged: list = []

        async def fake_exchange(url, link, cookie_name):
            exchanged.append(link)
            return "SESSION_TOK"

        monkeypatch.setattr(mgr, "_exchange_link", fake_exchange)
        monkeypatch.setattr(
            m.aiohttp, "ClientSession", self._fake_session(calls, statuses=[200, 200])
        )
        for _ in range(2):
            async with mgr.proxy_request("cd-1", "GET", "api/chat/slots") as resp:
                assert resp.status == 200
        assert exchanged == ["SECRET_TOK"]
        assert all("SESSION_TOK" in c["headers"]["Cookie"] for c in calls)

    @pytest.mark.asyncio
    async def test_failed_exchange_sends_no_link_and_remints(self, tmp_path, monkeypatch):
        """A link whose exchange fails is never sent as a cookie; the
        peer's refusal drives the single re-mint, whose fresh link exchanges."""
        from kiro_crew.instances import ssh_tunnel_manager as m

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")
        calls: list = []

        async def fake_exchange(url, link, cookie_name):
            return "" if link == "SECRET_TOK" else f"S-{link}"

        async def fake_refresh(instance_id):
            mgr._tokens[instance_id] = "FRESH_TOK"
            return "FRESH_TOK"

        monkeypatch.setattr(mgr, "_exchange_link", fake_exchange)
        monkeypatch.setattr(mgr, "refresh_token", fake_refresh)
        monkeypatch.setattr(
            m.aiohttp, "ClientSession", self._fake_session(calls, statuses=[401, 200])
        )
        async with mgr.proxy_request("cd-1", "GET", "api/chat/slots") as resp:
            assert resp.status == 200
        assert "Cookie" not in calls[0]["headers"]
        assert calls[1]["headers"]["Cookie"].endswith("=S-FRESH_TOK")

    @staticmethod
    def _exchange_session(gets, calls, *, exchange):
        """A ClientSession stand-in for the REAL ``_exchange_link``.

        ``get`` (the link exchange) behaves per *exchange*: an exception class
        is raised on entry, an int is the peer's status with no cookie set.
        ``request`` (the carrier) records and answers 403, as a peer does to a
        request that carries no cookie.
        """

        class _Resp:
            def __init__(self, status):
                self.status = status
                self.cookies: dict = {}

            def release(self):
                return None

        class _Get:
            async def __aenter__(self):
                if isinstance(exchange, type):
                    raise exchange()
                return _Resp(exchange)

            async def __aexit__(self, *exc):
                return None

        class _Sess:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            def get(self, url, **kwargs):
                gets.append(kwargs.get("params", {}).get("token"))
                return _Get()

            async def request(self, method, url, **kwargs):
                calls.append(kwargs.get("headers", {}))
                return _Resp(403)

            async def close(self):
                return None

        return _Sess

    @pytest.mark.asyncio
    async def test_unreachable_exchange_is_no_credential_not_a_remint(self, tmp_path, monkeypatch):
        """An exchange the peer never answered is not a refusal: the request is
        not sent credential-less, the single re-mint is not spent on it, and
        the caller learns the exchange failed rather than "peer rejected"."""
        from kiro_crew.instances import ssh_tunnel_manager as m
        from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        gets: list = []
        calls: list = []
        monkeypatch.setattr(
            m.aiohttp,
            "ClientSession",
            self._exchange_session(gets, calls, exchange=asyncio.TimeoutError),
        )
        await mgr.connect("cd-1")
        remints: list = []

        async def fake_refresh(instance_id):
            remints.append(instance_id)
            mgr._tokens[instance_id] = "FRESH_TOK"
            return "FRESH_TOK"

        monkeypatch.setattr(mgr, "refresh_token", fake_refresh)
        with pytest.raises(ProxyRequestError) as ei:
            async with mgr.proxy_request("cd-1", "GET", "api/chat/slots"):
                pass
        assert ei.value.code == "proxy_no_credential"
        assert ei.value.message == "could not exchange the credential with the peer"
        assert ei.value.http_status == 503
        assert remints == []
        assert calls == []  # never sent without a credential
        assert "cd-1" not in mgr._peer_sessions

    @pytest.mark.asyncio
    async def test_peer_error_on_exchange_is_no_credential_not_a_remint(
        self, tmp_path, monkeypatch
    ):
        """A peer that answered 500 to the link did not refuse it: the request
        is not sent credential-less, the single re-mint is not spent on it,
        and the caller learns the exchange failed rather than "peer rejected"."""
        from kiro_crew.instances import ssh_tunnel_manager as m
        from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        gets: list = []
        calls: list = []
        monkeypatch.setattr(
            m.aiohttp, "ClientSession", self._exchange_session(gets, calls, exchange=500)
        )
        await mgr.connect("cd-1")
        remints: list = []

        async def fake_refresh(instance_id):
            remints.append(instance_id)
            mgr._tokens[instance_id] = "FRESH_TOK"
            return "FRESH_TOK"

        monkeypatch.setattr(mgr, "refresh_token", fake_refresh)
        with pytest.raises(ProxyRequestError) as ei:
            async with mgr.proxy_request("cd-1", "GET", "api/chat/slots"):
                pass
        assert ei.value.code == "proxy_no_credential"
        assert ei.value.message == "could not exchange the credential with the peer"
        assert ei.value.http_status == 503
        assert remints == []
        assert calls == []  # never sent without a credential
        assert "cd-1" not in mgr._peer_sessions

    @pytest.mark.asyncio
    @pytest.mark.parametrize("refusal", [401, 403])
    async def test_refused_exchange_still_gets_exactly_one_remint(
        self, tmp_path, monkeypatch, refusal
    ):
        """A peer that answered 401/403 to the link is the case a re-mint fixes:
        the request goes out without a cookie, the 403 drives ONE re-mint,
        and a second refusal is the typed credential failure."""
        from kiro_crew.instances import ssh_tunnel_manager as m
        from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        gets: list = []
        calls: list = []
        monkeypatch.setattr(
            m.aiohttp, "ClientSession", self._exchange_session(gets, calls, exchange=refusal)
        )
        await mgr.connect("cd-1")
        remints: list = []

        async def fake_refresh(instance_id):
            remints.append(instance_id)
            mgr._tokens[instance_id] = "FRESH_TOK"
            return "FRESH_TOK"

        monkeypatch.setattr(mgr, "refresh_token", fake_refresh)
        with pytest.raises(ProxyRequestError) as ei:
            async with mgr.proxy_request("cd-1", "GET", "api/chat/slots"):
                pass
        assert ei.value.code == "proxy_unauthorized"
        assert remints == ["cd-1"]
        # connect primes SECRET_TOK; the request retries it, then the fresh link.
        assert gets == ["SECRET_TOK", "SECRET_TOK", "FRESH_TOK"]
        assert calls == [{}, {}]

    @staticmethod
    def _cookie_gated_session(calls):
        """A ClientSession stand-in: 200 with a Cookie header, 403 without one."""

        class _Resp:
            def __init__(self, status):
                self.status = status

            def release(self):
                return None

        class _Sess:
            def __init__(self, *a, **k):
                pass

            async def request(self, method, url, **kwargs):
                calls.append(kwargs.get("headers", {}))
                return _Resp(200 if "Cookie" in kwargs.get("headers", {}) else 403)

            async def close(self):
                return None

        return _Sess

    def _aging_links(self, monkeypatch, mgr):
        """Patch the exchange so a link trades only until it is marked stale."""
        stale: set = set()
        exchanged: list = []

        async def fake_exchange(url, link, cookie_name):
            exchanged.append(link)
            return "" if link in stale else f"S-{link}"

        monkeypatch.setattr(mgr, "_exchange_link", fake_exchange)
        return stale, exchanged

    @pytest.mark.asyncio
    async def test_link_is_exchanged_at_connect_while_fresh(self, tmp_path, monkeypatch):
        """A request made after the link's click window still carries a
        session, with no re-mint on the request path."""
        from kiro_crew.instances import ssh_tunnel_manager as m

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        stale, exchanged = self._aging_links(monkeypatch, mgr)
        await mgr.connect("cd-1")
        assert exchanged == ["SECRET_TOK"]
        stale.add("SECRET_TOK")
        remints: list = []

        async def no_refresh(instance_id):
            remints.append(instance_id)
            return None

        monkeypatch.setattr(mgr, "refresh_token", no_refresh)
        calls: list = []
        monkeypatch.setattr(m.aiohttp, "ClientSession", self._cookie_gated_session(calls))
        async with mgr.proxy_request("cd-1", "GET", "api/chat/slots") as resp:
            assert resp.status == 200
        assert remints == []
        assert calls[0]["Cookie"].endswith("=S-SECRET_TOK")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blocker", ["reconfiguring", "mint_failure"])
    async def test_session_from_mint_time_survives_a_blocked_remint(
        self, tmp_path, monkeypatch, blocker
    ):
        """Where a re-mint cannot run, the session obtained at mint time
        still authenticates every peer request kind."""
        from kiro_crew.instances import ssh_tunnel_manager as m
        from kiro_crew.instances.token_mint import TokenMintError

        mints = {"fail": False}

        async def mint(host, **_kw):
            if mints["fail"]:
                raise TokenMintError("nope")
            return "SECRET_TOK"

        reg, mgr = self._mgr(tmp_path, mint=mint)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        stale, _exchanged = self._aging_links(monkeypatch, mgr)
        await mgr.connect("cd-1")
        stale.add("SECRET_TOK")
        if blocker == "reconfiguring":
            mgr._reconfiguring.add("cd-1")
        else:
            mints["fail"] = True
        calls: list = []
        monkeypatch.setattr(m.aiohttp, "ClientSession", self._cookie_gated_session(calls))
        async with mgr.proxy_request("cd-1", "GET", "api/chat/slots") as resp:
            assert resp.status == 200
        assert calls and all(c["Cookie"].endswith("=S-SECRET_TOK") for c in calls)

    @pytest.mark.asyncio
    async def test_remint_exchanges_the_new_link_at_once(self, tmp_path, monkeypatch):
        """The proactive re-mint primes the new link's session, retiring the old."""
        seq = iter(["SECRET_TOK", "NEW_TOK"])

        async def mint(host, **_kw):
            return next(seq)

        reg, mgr = self._mgr(tmp_path, mint=mint)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        _stale, exchanged = self._aging_links(monkeypatch, mgr)
        await mgr.connect("cd-1")
        assert await mgr._refresh_token_once("cd-1") is True
        assert exchanged == ["SECRET_TOK", "NEW_TOK"]
        assert mgr._peer_sessions["cd-1"] == ("NEW_TOK", "S-NEW_TOK")
        await mgr.disconnect("cd-1")

    @pytest.mark.asyncio
    async def test_exchange_raced_by_a_remint_does_not_displace_it(self, tmp_path, monkeypatch):
        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")

        async def racing_exchange(url, link, cookie_name):
            mgr._tokens["cd-1"] = "NEWER_TOK"
            return f"S-{link}"

        monkeypatch.setattr(mgr, "_exchange_link", racing_exchange)
        mgr._peer_sessions.pop("cd-1", None)
        url, cookie = mgr._peer_target("cd-1", "api/status")
        headers = await mgr._peer_cookie_header("cd-1", url, cookie)
        assert headers["Cookie"].endswith("=S-SECRET_TOK")
        assert "cd-1" not in mgr._peer_sessions

    @pytest.mark.asyncio
    async def test_exchange_link_keeps_the_session_cookie_the_peer_sets(self):
        from aiohttp import web

        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager, _PeerUnavailable

        seen: list = []

        async def status(request):
            seen.append((request.path, request.query.get("token")))
            if request.query.get("token") == "BAD":
                return web.json_response({"error": "unauthorized"}, status=403)
            if request.query.get("token") == "BROKEN":
                return web.json_response({"error": "boom"}, status=500)
            if request.query.get("token") == "MOVED":
                raise web.HTTPFound("/login")
            resp = web.json_response({"ok": True})
            if request.query.get("token") == "LINK":
                resp.set_cookie("mc_token_1234", "SESSION")
            return resp

        app = web.Application()
        app.router.add_get("/api/status", status)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        try:
            mgr = SshTunnelManager.__new__(SshTunnelManager)
            url = f"http://127.0.0.1:{port}/api/chat/slots"
            assert await mgr._exchange_link(url, "LINK", "mc_token_1234") == "SESSION"
            assert await mgr._exchange_link(url, "BAD", "mc_token_1234") == ""
            for peer_error in ("OTHER", "BROKEN", "MOVED"):
                with pytest.raises(_PeerUnavailable) as ei:
                    await mgr._exchange_link(url, peer_error, "mc_token_1234")
                assert ei.value.kind == "exchange_failed", peer_error
        finally:
            await runner.cleanup()
        assert seen == [
            ("/api/status", "LINK"),
            ("/api/status", "BAD"),
            ("/api/status", "OTHER"),
            ("/api/status", "BROKEN"),
            ("/api/status", "MOVED"),
        ]

    @pytest.mark.asyncio
    async def test_exchange_link_waits_the_proxy_connect_budget_not_the_probe_budget(
        self, monkeypatch
    ):
        """The link exchange is a peer call, so it gets the proxy connect budget.

        ``_TOKEN_PROBE_TIMEOUT`` sizes the deny-by-default liveness probe and is
        deliberately short. A peer whose ``/api/status`` answers slower than that
        is still reachable; timing the exchange out on the probe budget would
        report it as 503 ``proxy_no_credential``. The probe budget is pinned
        below the peer's delay and the proxy connect budget above it, so the
        exchange succeeds only when it waits on the proxy budget.
        """
        from aiohttp import web

        from kiro_crew.instances import ssh_tunnel_manager as m
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        peer_delay = 0.3
        # flake-ok: pre-existing 50 ms probe budget pinned below the stub delay, off the passing path, moved verbatim
        monkeypatch.setattr(m, "_TOKEN_PROBE_TIMEOUT", peer_delay / 6)
        # flake-ok: pre-existing 6 s connect budget, twenty times the stub delay, moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(m, "_PROXY_CONNECT_TIMEOUT", peer_delay * 20)

        async def status(request):
            # flake-ok: pre-existing 0.3 s stub peer delay inside the 6 s connect budget, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(peer_delay)
            resp = web.json_response({"ok": True})
            resp.set_cookie("mc_token_1234", "SESSION")
            return resp

        app = web.Application()
        app.router.add_get("/api/status", status)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        try:
            mgr = SshTunnelManager.__new__(SshTunnelManager)
            url = f"http://127.0.0.1:{port}/api/chat/slots"
            assert await mgr._exchange_link(url, "LINK", "mc_token_1234") == "SESSION"
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_persistent_401_raises_unauthorized_not_a_loop(self, tmp_path, monkeypatch):
        from kiro_crew.instances import ssh_tunnel_manager as m
        from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        async def fake_exchange(url, link, cookie_name):
            return f"S-{link}"

        monkeypatch.setattr(mgr, "_exchange_link", fake_exchange)
        await mgr.connect("cd-1")
        calls: list = []
        monkeypatch.setattr(m.aiohttp, "ClientSession", self._fake_session(calls, statuses=[401]))

        async def no_refresh(instance_id):
            return None

        monkeypatch.setattr(mgr, "refresh_token", no_refresh)
        with pytest.raises(ProxyRequestError) as ei:
            async with mgr.proxy_request("cd-1", "GET", "api/chat/slots"):
                pass
        assert ei.value.code == "proxy_unauthorized"
        assert len(calls) == 1  # no retry storm

    @pytest.mark.asyncio
    async def test_401_after_a_successful_remint_is_still_typed(self, tmp_path, monkeypatch):
        """The re-mint succeeding does not make the SECOND 401 a normal reply.

        A guard of `status in (401, 403) and not reminted` fails here:
        once a fresh credential is minted a second rejection falls
        through to the caller as a bare peer 401 — the UI would read "the chat
        endpoint said no" instead of a credential failure, with no coded error.
        """
        from kiro_crew.instances import ssh_tunnel_manager as m
        from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        async def fake_exchange(url, link, cookie_name):
            return f"S-{link}"

        monkeypatch.setattr(mgr, "_exchange_link", fake_exchange)
        await mgr.connect("cd-1")
        calls: list = []
        monkeypatch.setattr(
            m.aiohttp, "ClientSession", self._fake_session(calls, statuses=[401, 401])
        )

        async def fake_refresh(instance_id):
            return "FRESH"

        monkeypatch.setattr(mgr, "refresh_token", fake_refresh)
        with pytest.raises(ProxyRequestError) as ei:
            async with mgr.proxy_request("cd-1", "GET", "api/chat/slots"):
                pass
        assert ei.value.code == "proxy_unauthorized"
        assert len(calls) == 2  # original + exactly one retry, then stop

    @pytest.mark.asyncio
    async def test_transport_error_maps_to_unreachable(self, tmp_path, monkeypatch):
        from kiro_crew.instances import ssh_tunnel_manager as m
        from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        async def fake_exchange(url, link, cookie_name):
            return f"S-{link}"

        monkeypatch.setattr(mgr, "_exchange_link", fake_exchange)
        await mgr.connect("cd-1")

        class _BoomSess:
            def __init__(self, *a, **k):
                pass

            async def request(self, *a, **k):
                raise asyncio.TimeoutError()

            async def close(self):
                return None

        monkeypatch.setattr(m.aiohttp, "ClientSession", _BoomSess)
        with pytest.raises(ProxyRequestError) as ei:
            async with mgr.proxy_request("cd-1", "GET", "api/status"):
                pass
        assert ei.value.code == "proxy_peer_unreachable"
        assert ei.value.http_status == 502


class TestPeerRequestSharedDance:
    """The derivation shared by the three peer-request methods.

    `proxy_request`, `send_session_bundle` and `search_sessions_remote` each
    make an authenticated call to a CONNECTED peer over the open forward. The
    part that is identical — connected-only, loopback target, port-scoped cookie
    name, credential read per attempt — lives in `_peer_target` /
    `_peer_cookie_header` so it is stated once. What is NOT shared is each
    method's error contract, and that is what these tests pin.
    """

    @pytest.fixture(autouse=True)
    # flake-ok: pre-existing autouse port-probe stub on the shared monkeypatch, moved verbatim; tracked for a de-flake pass
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class-local autouse stub, moved verbatim; de-flake pass tracked
    def _free_ports(self, monkeypatch):
        _patch_port_probe(monkeypatch)

    def _mgr(self, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        reg = InstancesRegistry(path=tmp_path / "instances.json")

        async def ok_mint(host, **kwargs):
            return "SECRET_TOK"

        return reg, SshTunnelManager(
            reg, base_port=53700, mint_token=ok_mint, tunnel_factory=_FakeTunnel
        )

    @pytest.mark.asyncio
    async def test_peer_target_is_the_single_source_of_the_port_scoped_cookie(self, tmp_path):
        """One derivation, not three: loopback URL + `mc_token_{port}`.

        The port scope is load-bearing — the peer keys its cookie on the port the
        CLIENT connected to, so two remotes both serving 7777 through different
        forwards must not collide, and a bare `mc_token` would 403 every call.
        """
        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        st = await mgr.connect("cd-1")

        url, cookie_name = mgr._peer_target("cd-1", "/api/chat/slots")
        assert url == f"http://127.0.0.1:{st.local_port}/api/chat/slots"
        assert cookie_name == f"mc_token_{st.local_port}"
        # Leading slash is optional — callers pass both spellings.
        assert mgr._peer_target("cd-1", "api/chat/slots")[0] == url

    @pytest.mark.asyncio
    async def test_peer_cookie_header_refuses_when_no_credential(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import _PeerUnavailable

        _reg, mgr = self._mgr(tmp_path)
        with pytest.raises(_PeerUnavailable) as ei:
            await mgr._peer_cookie_header("cd-1", "http://127.0.0.1:1/api/status", "mc_token_1")
        assert ei.value.kind == "no_credential"

    @pytest.mark.asyncio
    async def test_each_caller_keeps_its_own_not_connected_code(self, tmp_path):
        """The shared helper must NOT flatten the three error families.

        `proxy_*`, `transfer_*` and `search_*` belong to three separate route
        contracts pinned by test_error_code_contract.py. Deriving them from the
        helper's `kind` would be tidier and wrong — this is the drift guard that
        catches it, and it is the reason the codes are literals at each site.
        """
        from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

        _reg, mgr = self._mgr(tmp_path)

        with pytest.raises(ProxyRequestError) as ei:
            async with mgr.proxy_request("nope", "GET", "api/chat"):
                pass
        assert ei.value.code == "proxy_peer_not_connected"

        ok, payload = await mgr.send_session_bundle(
            "nope", {"bundle_version": 2}, serialise=lambda _b: None
        )
        assert (ok, payload["code"]) == (False, "transfer_peer_not_connected")

        ok, payload = await mgr.search_sessions_remote("nope", "q", 10)
        assert (ok, payload["code"]) == (False, "search_peer_not_connected")

    @pytest.mark.asyncio
    async def test_each_caller_keeps_its_own_no_credential_code(self, tmp_path):
        """Same split for the missing-credential condition, on a LIVE tunnel."""
        from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")
        mgr._tokens.pop("cd-1", None)  # connected, but the credential is gone

        with pytest.raises(ProxyRequestError) as ei:
            async with mgr.proxy_request("cd-1", "GET", "api/chat"):
                pass
        assert ei.value.code == "proxy_no_credential"
        assert ei.value.http_status == 503

        ok, payload = await mgr.send_session_bundle(
            "cd-1", {"bundle_version": 2}, serialise=lambda _b: None
        )
        assert (ok, payload["code"]) == (False, "transfer_no_credential")

        ok, payload = await mgr.search_sessions_remote("cd-1", "q", 10)
        assert (ok, payload["code"]) == (False, "search_no_credential")

    def test_proxy_request_exposes_no_timeout_override(self):
        """The timeout policy is a property of the method, not a per-call choice.

        It is connect+read-idle rather than total because a proxied chat turn
        streams for minutes and a total cap would sever it. Nothing ever passed
        the old override, so the parameter only invited a caller to break that
        for itself; this pins the subtraction.
        """
        import inspect

        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        params = inspect.signature(SshTunnelManager.proxy_request).parameters
        assert "timeout" not in params
        assert set(params) == {
            "self",
            "instance_id",
            "method",
            "path",
            "params",
            "data",
            "content_type",
        }
