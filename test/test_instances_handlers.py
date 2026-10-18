"""The owner-only instances API handlers: gating, CRUD, connect, token safety."""

from __future__ import annotations

import asyncio

import pytest
from instances_helpers import _body, _ConnectedMgr, _enable, _fake_reconfigure, _FakeReq, _State


class TestHandlers:
    def _reg(self, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry

        return InstancesRegistry(path=tmp_path / "instances.json")

    def test_disabled_returns_403(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch, enabled=False)
        r = asyncio.run(handlers.api_instances_list(_FakeReq(_State(self._reg(tmp_path)))))
        assert r.status == 403 and "disabled" in _body(r)["error"]

    def test_disabled_denial_carries_its_discriminator_code(self, tmp_path, monkeypatch):
        # The dashboard opts THIS denial out of its error journal, and keys that on
        # the code rather than on the 403 -- the owner-only and Slack-origin denials
        # below share the status and must still be reported.  Dropping or renaming
        # the code silently re-breaks the spurious-error-report defect, so it is
        # asserted here rather than left to the SPA's fixture to assume.
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch, enabled=False)
        r = asyncio.run(handlers.api_instances_list(_FakeReq(_State(self._reg(tmp_path)))))
        assert _body(r)["code"] == "instances_disabled"

    def test_slack_origin_denial_carries_no_benign_code(self, tmp_path, monkeypatch):
        # The converse guard: a denial the dashboard must REPORT may not wear the
        # benign code, or it would be swallowed with the routine one.
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        req = _FakeReq(_State(self._reg(tmp_path)), headers={"X-Session-Key": "slack:T:C"})
        r = asyncio.run(handlers.api_instances_list(req))
        assert r.status == 403
        assert _body(r).get("code") != "instances_disabled"

    def test_slack_origin_rejected(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        req = _FakeReq(_State(self._reg(tmp_path)), headers={"X-Session-Key": "slack:T:C"})
        r = asyncio.run(handlers.api_instances_list(req))
        assert r.status == 403 and "owner-only" in _body(r)["error"]

    def test_unauthenticated_rejected(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        # No authenticated user (require_auth would have set request["user"]).
        req = _FakeReq(_State(self._reg(tmp_path)), user=None)
        r = asyncio.run(handlers.api_instances_list(req))
        assert r.status == 401 and "authentication required" in _body(r)["error"]

    def test_add_list_includes_cap(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        state = _State(reg)
        r = asyncio.run(
            handlers.api_instances_add(
                _FakeReq(state, body={"name": "CD", "ssh_host": "cd-1-alias"})
            )
        )
        assert r.status == 201
        r = asyncio.run(handlers.api_instances_list(_FakeReq(state)))
        b = _body(r)
        # One crew registered => automatic cap 1, and adding it is enough: the
        # cap does not wait for the tunnel to come up.
        assert b["warm_set_cap"] == 1 and len(b["instances"]) == 1
        # no manager on this state => enabled-in-config but not active (needs restart)
        assert b["active"] is False

    def test_automatic_cap_covers_every_registered_crew_not_just_connected_ones(
        self, tmp_path, monkeypatch
    ):
        """The served cap covers every REGISTERED crew, connected or not.

        This is the regression that produced "one random crew is broken". The cap
        must not be the live connected count: a crew whose tunnel came up just
        after this request was not counted, the cap arrived one short, and the
        viewport evicted a pane to honour it. Eviction unmounts the pane and
        cold-boots the remote SPA on the next click, which reads as a disconnect —
        and which crew lost depended on connection order, so the victim moved on
        every restart. Here 3 are registered and only 2 are connected; the cap
        must still be 3.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        for name in ("a", "b", "c"):
            reg.add(name=name, ssh_host=f"{name}-alias")
        state = _State(reg, _ConnectedMgr(["a", "c"]))
        b = _body(asyncio.run(handlers.api_instances_list(_FakeReq(state))))
        assert len(b["instances"]) == 3
        assert b["warm_set_cap"] == 3

    def test_automatic_cap_serves_ten_and_then_stops_growing(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        for index in range(11):
            reg.add(name=f"crew-{index}", ssh_host=f"crew-{index}")
        state = _State(reg, _ConnectedMgr([]))

        body = _body(asyncio.run(handlers.api_instances_list(_FakeReq(state))))

        assert len(body["instances"]) == 11
        assert body["warm_set_cap"] == 10

    def test_adding_a_crew_widens_the_served_cap(self, tmp_path, monkeypatch):
        """Otherwise every new crew has to be paired with a config edit.

        Forgetting that edit reintroduces the eviction above, so the cap has to
        rise on its own when the fleet grows.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="a", ssh_host="a-alias")
        state = _State(reg, _ConnectedMgr([]))
        before = _body(asyncio.run(handlers.api_instances_list(_FakeReq(state))))["warm_set_cap"]
        reg.add(name="b", ssh_host="b-alias")
        after = _body(asyncio.run(handlers.api_instances_list(_FakeReq(state))))["warm_set_cap"]
        assert (before, after) == (1, 2)

    def test_explicit_cap_is_served_even_below_the_registered_count(self, tmp_path, monkeypatch):
        """An operator's own number is the budget and is not widened for them."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch, warm_set_cap=1)
        reg = self._reg(tmp_path)
        for name in ("a", "b"):
            reg.add(name=name, ssh_host=f"{name}-alias")
        state = _State(reg, _ConnectedMgr(["a", "b"]))
        assert _body(asyncio.run(handlers.api_instances_list(_FakeReq(state))))["warm_set_cap"] == 1

    def test_list_active_reflects_manager_running(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        # manager present => active True; absent => active False
        r = asyncio.run(handlers.api_instances_list(_FakeReq(_State(reg, object()))))
        assert _body(r)["active"] is True
        r = asyncio.run(handlers.api_instances_list(_FakeReq(_State(reg))))
        assert _body(r)["active"] is False

    def test_add_invalid_ssh_host_400(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        r = asyncio.run(
            handlers.api_instances_add(
                _FakeReq(_State(self._reg(tmp_path)), body={"name": "x", "ssh_host": "bad host;rm"})
            )
        )
        assert r.status == 400

    def test_connect_returns_token_but_list_does_not_leak(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        class FakeMgr:
            def __init__(self):
                self._tok = {}

            async def connect(self, iid):
                self._tok[iid] = "SECRET_TOK"
                reg.update(iid, was_connected=True, local_port=7778)
                return TunnelStatus(iid, TunnelState.CONNECTED, local_port=7778, remote_port=7777)

            async def disconnect(self, iid):
                self._tok.pop(iid, None)
                return True

            def status(self, iid):
                return (
                    TunnelStatus(iid, TunnelState.CONNECTED, local_port=7778, remote_port=7777)
                    if iid in self._tok
                    else None
                )

            def get_token(self, iid):
                return self._tok.get(iid, "")

            async def token_validates(self, local_port, token):
                return True  # stored token still good — no re-mint

            async def refresh_token(self, iid):
                self._tok[iid] = "FRESH_TOK"
                return "FRESH_TOK"

            def token_ttl_remaining(self, iid):
                return 72000 if iid in self._tok else None

            def token_ttl_total(self, iid):
                return None

        state = _State(reg, FakeMgr())
        r = asyncio.run(handlers.api_instances_connect(_FakeReq(state, match={"id": "cd-1"})))
        assert r.status == 200 and _body(r)["token"] == "SECRET_TOK"
        # list must NOT leak the token
        r = asyncio.run(handlers.api_instances_list(_FakeReq(state)))
        assert "SECRET_TOK" not in r.body.decode()

    def test_a_forward_that_moves_during_the_token_probe_is_refused(self, tmp_path, monkeypatch):
        """The token and the port are one answer. `body` freezes the port before the
        probe and the re-mint, both of which await for seconds, while the status
        object stays live -- a teardown in that window pops the tunnel without
        zeroing the port on it, and the allocator hands a just-freed port to the
        next connect first. So the frozen port can name a forward that now belongs
        to another crew, and the pane would load that one holding this crew's token.
        """
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        class FakeMgr:
            def __init__(self):
                self.live = None

            async def connect(self, iid, *, rebuild=False, only_if_connected=False):
                reg.update(iid, was_connected=True, local_port=7778)
                self.live = TunnelStatus(
                    iid, TunnelState.CONNECTED, local_port=7778, remote_port=7777
                )
                return self.live

            def get_token(self, iid):
                return "SECRET_TOK"

            async def token_validates(self, local_port, token):
                # The crew is torn down and another takes the freed port while the
                # probe is in flight. The status object survives with the old port.
                self.live.local_port = 7779
                return True

            async def refresh_token(self, iid):
                return "FRESH_TOK"

        state = _State(reg, FakeMgr())
        r = asyncio.run(handlers.api_instances_connect(_FakeReq(state, match={"id": "cd-1"})))

        assert r.status == 502, "answered with a token paired to a port it no longer owns"
        body = _body(r)
        assert "token" not in body, "handed the pane a credential for a moved forward"
        assert body["code"] == "instance_token_unconfirmed"
        assert "SECRET_TOK" not in r.body.decode()

    def test_a_forward_that_drops_during_the_token_probe_is_refused(self, tmp_path, monkeypatch):
        """The same reading, on the half a teardown does not reach: a probe marks the
        live status ERROR with the port unchanged, so comparing ports alone still
        agrees while there is no forward left to load.
        """
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        class FakeMgr:
            def __init__(self):
                self.live = None

            async def connect(self, iid, *, rebuild=False, only_if_connected=False):
                reg.update(iid, was_connected=True, local_port=7778)
                self.live = TunnelStatus(
                    iid, TunnelState.CONNECTED, local_port=7778, remote_port=7777
                )
                return self.live

            def get_token(self, iid):
                return "SECRET_TOK"

            async def token_validates(self, local_port, token):
                self.live.state = TunnelState.ERROR
                return True

        state = _State(reg, FakeMgr())
        r = asyncio.run(handlers.api_instances_connect(_FakeReq(state, match={"id": "cd-1"})))

        assert r.status == 502, "answered for a forward that had dropped"
        assert "token" not in _body(r)
        assert _body(r)["local_port"] == 7778, "the port never moved, so only the state can refuse"

    def test_connect_rebuild_query_reaches_the_manager(self, tmp_path, monkeypatch):
        """``?rebuild=1`` is the pane's Retry after a watchdog verdict; it must be
        forwarded as ``rebuild=True`` and nothing else about the response changes.
        Without the flag the manager is called with its plain positional
        contract, so every existing caller (and fake) keeps working."""
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        calls = []

        class FakeMgr:
            async def connect(self, iid, *, rebuild=False, only_if_connected=False):
                calls.append((rebuild, only_if_connected))
                if only_if_connected and not reg.get(iid).was_connected:
                    return TunnelStatus(
                        iid, TunnelState.DISCONNECTED, local_port=0, remote_port=7777
                    )
                reg.update(iid, was_connected=True, local_port=7778)
                return TunnelStatus(iid, TunnelState.CONNECTED, local_port=7778, remote_port=7777)

            def get_token(self, iid):
                return "SECRET_TOK"

            async def token_validates(self, local_port, token):
                return True

        state = _State(reg, FakeMgr())
        r = asyncio.run(handlers.api_instances_connect(_FakeReq(state, match={"id": "cd-1"})))
        assert r.status == 200
        r = asyncio.run(
            handlers.api_instances_connect(
                _FakeReq(state, match={"id": "cd-1"}, query={"rebuild": "1"})
            )
        )
        assert r.status == 200 and _body(r)["token"] == "SECRET_TOK"
        r = asyncio.run(
            handlers.api_instances_connect(
                _FakeReq(state, match={"id": "cd-1"}, query={"rebuild": "0"})
            )
        )
        assert r.status == 200
        assert calls == [(False, False), (True, False), (False, False)]

    def test_connect_only_if_connected_query_declines_as_200_not_failure(
        self, tmp_path, monkeypatch
    ):
        """``?only_if_connected=1`` (auto-warm) reaches the manager as the keyword;
        a declined (not-up) answer is a 200 with a non-connected state and
        ``code=instance_not_connected`` — the shape the shared connect step reads
        as `warm-declined` — never the 502 a real connect failure gets. A live
        tunnel is answered exactly like a plain connect. Combining it with
        ``rebuild`` is a 400."""
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        calls = []

        class FakeMgr:
            async def connect(self, iid, *, rebuild=False, only_if_connected=False):
                calls.append((rebuild, only_if_connected))
                if only_if_connected and not reg.get(iid).was_connected:
                    return TunnelStatus(
                        iid, TunnelState.DISCONNECTED, local_port=0, remote_port=7777
                    )
                reg.update(iid, was_connected=True, local_port=7778)
                return TunnelStatus(iid, TunnelState.CONNECTED, local_port=7778, remote_port=7777)

            def get_token(self, iid):
                return "SECRET_TOK"

            async def token_validates(self, local_port, token):
                return True

        state = _State(reg, FakeMgr())
        q = {"only_if_connected": "1"}
        r = asyncio.run(
            handlers.api_instances_connect(_FakeReq(state, match={"id": "cd-1"}, query=q))
        )
        assert r.status == 200
        body = _body(r)
        assert body["state"] == "disconnected" and body["code"] == "instance_not_connected"
        assert "token" not in body
        assert reg.get("cd-1").was_connected is False

        # Bring it up the normal way, then the connected-only call is a plain answer.
        r = asyncio.run(handlers.api_instances_connect(_FakeReq(state, match={"id": "cd-1"})))
        assert r.status == 200
        r = asyncio.run(
            handlers.api_instances_connect(_FakeReq(state, match={"id": "cd-1"}, query=q))
        )
        assert r.status == 200 and _body(r)["token"] == "SECRET_TOK"
        assert calls == [(False, True), (False, False), (False, True)]

        r = asyncio.run(
            handlers.api_instances_connect(
                _FakeReq(
                    state, match={"id": "cd-1"}, query={"rebuild": "1", "only_if_connected": "1"}
                )
            )
        )
        assert r.status == 400

    def test_connect_exclusive_flags_rejection_is_audited(self, tmp_path, monkeypatch):
        """The ``rebuild`` + ``only_if_connected`` 400 is a control-plane refusal
        like every other early exit in connect (manager unavailable, not found),
        so it must leave the same ``instances_connect`` / ``denied`` SEL line —
        an owner hand-crafting the pair must not be the one connect outcome the
        audit trail cannot see."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        events = []

        class FakeSel:
            def log_tool_invocation(self, **kw):
                events.append(kw)

        monkeypatch.setattr(handlers, "sel", lambda: FakeSel())

        class FakeMgr:
            async def connect(self, iid, *, rebuild=False, only_if_connected=False):
                raise AssertionError("manager must not be reached on a 400")

        r = asyncio.run(
            handlers.api_instances_connect(
                _FakeReq(
                    _State(reg, FakeMgr()),
                    match={"id": "cd-1"},
                    query={"rebuild": "1", "only_if_connected": "1"},
                )
            )
        )
        assert r.status == 400
        assert [(e["tool_name"], e["outcome"], e["request_id"]) for e in events] == [
            ("instances_connect", "denied", "cd-1")
        ]
        assert "mutually exclusive" in events[0]["error"]

    def test_connect_remints_when_stored_token_stale(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        class FakeMgr:
            # connect() returns a CONNECTED tunnel whose stored token is stale
            # (e.g. failed self-heal re-mint / remote restart). The gate must
            # probe, find it rejected, re-mint once, and serve the fresh token.
            def __init__(self):
                self.refreshed = []
                self._tok = {"cd-1": "STALE_TOK"}

            async def connect(self, iid):
                return TunnelStatus(iid, TunnelState.CONNECTED, local_port=7778, remote_port=7777)

            def get_token(self, iid):
                return self._tok.get(iid, "")

            async def token_validates(self, local_port, token):
                return False  # remote rejects the stored token

            async def refresh_token(self, iid):
                self._tok[iid] = "FRESH_TOK"
                self.refreshed.append(iid)
                return "FRESH_TOK"

        mgr = FakeMgr()
        r = asyncio.run(
            handlers.api_instances_connect(_FakeReq(_State(reg, mgr), match={"id": "cd-1"}))
        )
        assert r.status == 200
        assert _body(r)["token"] == "FRESH_TOK"  # served the fresh mint, not the stale token
        assert mgr.refreshed == ["cd-1"]

    def test_connect_502_when_stale_and_remint_fails(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        class FakeMgr:
            # Probe confirms the stored token is no good, and the re-mint also
            # fails (link genuinely down). The handler must NOT serve the
            # unconfirmed token (which would reproduce the stuck 403) — it
            # returns 502 with no token in the body.
            def __init__(self):
                self._tok = {"cd-1": "STALE_TOK"}

            async def connect(self, iid):
                return TunnelStatus(iid, TunnelState.CONNECTED, local_port=7778, remote_port=7777)

            def get_token(self, iid):
                return self._tok.get(iid, "")

            async def token_validates(self, local_port, token):
                return False

            async def refresh_token(self, iid):
                return None  # re-mint failed (SSH/link unreachable)

        r = asyncio.run(
            handlers.api_instances_connect(_FakeReq(_State(reg, FakeMgr()), match={"id": "cd-1"}))
        )
        assert r.status == 502
        assert "token" not in _body(r)  # never serve a token we couldn't confirm
        assert "STALE_TOK" not in r.body.decode()

    def test_connect_failure_promotes_the_diagnosis_verdict_to_a_top_level_code(
        self, tmp_path, monkeypatch
    ):
        """A failed connect names WHICH link broke where a client can read it.

        The ladder's verdict already travels in ``diagnosis.code``, but the
        dashboard's error journal reads a top-level ``code`` — so without the
        promotion the one field that distinguishes "cannot SSH at all" from "SSH
        works, the remote gateway is down" never reaches the surface that offers to
        act on it. Only a NEGATIVE verdict is promoted: the stored diagnosis is the
        last ladder run, so a stale ``ok`` must not be published as this call's
        reason.
        """
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        def connect_returning(status):
            class FakeMgr:
                async def connect(self, iid):
                    return status

            return asyncio.run(
                handlers.api_instances_connect(
                    _FakeReq(_State(reg, FakeMgr()), match={"id": "cd-1"})
                )
            )

        diagnosed = connect_returning(
            TunnelStatus(
                "cd-1",
                TunnelState.ERROR,
                error="tunnel failed",
                diagnosis={
                    "code": "ssh_unreachable",
                    "ok": False,
                    "reason": "Can't SSH to the host",
                    "probes": [{"name": "ssh", "ok": False}],
                },
            )
        )
        assert diagnosed.status == 502 and _body(diagnosed)["code"] == "ssh_unreachable"

        # No diagnosis on record — the response still names the stage that failed
        # rather than leaving the client to parse prose.
        undiagnosed = connect_returning(
            TunnelStatus("cd-1", TunnelState.ERROR, error="tunnel failed")
        )
        assert undiagnosed.status == 502
        assert _body(undiagnosed)["code"] == "instance_connect_failed"

        # A stale healthy verdict is not this failure's reason.
        stale_ok = connect_returning(
            TunnelStatus(
                "cd-1",
                TunnelState.ERROR,
                error="tunnel failed",
                diagnosis={"code": "ok", "ok": True, "reason": "all good", "probes": []},
            )
        )
        assert _body(stale_ok)["code"] == "instance_connect_failed"

    def test_connect_missing_manager_and_unknown_id_carry_codes(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        no_mgr = asyncio.run(
            handlers.api_instances_connect(_FakeReq(_State(reg), match={"id": "cd-1"}))
        )
        assert no_mgr.status == 503
        assert _body(no_mgr)["code"] == "instances_manager_unavailable"

        class MissingMgr:
            async def connect(self, iid):
                raise KeyError(iid)

        ghost = asyncio.run(
            handlers.api_instances_connect(
                _FakeReq(_State(reg, MissingMgr()), match={"id": "ghost"})
            )
        )
        assert ghost.status == 404 and _body(ghost)["code"] == "instance_not_found"

    def test_status_404(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        r = asyncio.run(
            handlers.api_instances_status(
                _FakeReq(_State(self._reg(tmp_path)), match={"id": "ghost"})
            )
        )
        assert r.status == 404

    def test_status_diagnose_runs_ladder(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        calls = []

        class FakeMgr:
            async def diagnose(self, iid):
                calls.append(iid)
                # No live tunnel (status() is None below), so diagnose returns
                # the result instead of storing it on a tunnel status.
                return {"code": "remote_down", "ok": False, "reason": "remote dashboard down"}

            def status(self, iid):
                return None  # never connected — no live tunnel

            def token_ttl_remaining(self, iid):
                return None

            def token_ttl_total(self, iid):
                return None

            def last_error(self, iid):
                return None  # no retained connect failure for this instance

        r = asyncio.run(
            handlers.api_instances_status(
                _FakeReq(_State(reg, FakeMgr()), match={"id": "cd-1"}, query={"diagnose": "1"})
            )
        )
        assert r.status == 200 and calls == ["cd-1"]
        # Regression: the diagnosis must be surfaced even with no live tunnel,
        # otherwise Diagnose on a disconnected instance shows nothing.
        body = _body(r)
        assert body["diagnosis"]["code"] == "remote_down"
        assert body["diagnosis"]["reason"] == "remote dashboard down"

    def test_add_defaults_remote_port_to_the_stock_gateway_port(self, tmp_path, monkeypatch):
        """The ADD endpoint must not carry its own stale port default.

        The registry default and the handler default were separate literals, so
        correcting the registry left the HTTP path (which is what the Add form
        actually calls) still handing out an earlier default dashboard port. Every
        unit test built records via ``reg.add`` directly and so could not see it;
        an isolated-pod run against the real endpoint did.
        """
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.registry import DEFAULT_REMOTE_PORT

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        state = _State(reg)
        body = {"name": "Defaulted", "ssh_host": "cd-1-alias", "id": "cd-1"}
        assert asyncio.run(handlers.api_instances_add(_FakeReq(state, body=body))).status == 201
        assert reg.get("cd-1").remote_port == DEFAULT_REMOTE_PORT == 5476

    def test_add_duplicate_and_bad_body(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        state = _State(self._reg(tmp_path))
        body = {"name": "CD", "ssh_host": "cd-1-alias", "id": "cd-1"}
        assert asyncio.run(handlers.api_instances_add(_FakeReq(state, body=body))).status == 201
        # duplicate id -> 400
        assert asyncio.run(handlers.api_instances_add(_FakeReq(state, body=body))).status == 400
        # missing/invalid JSON body -> 400
        assert asyncio.run(handlers.api_instances_add(_FakeReq(state))).status == 400
        # body not an object -> 400
        assert asyncio.run(handlers.api_instances_add(_FakeReq(state, body=["x"]))).status == 400

    def test_add_error_bodies_carry_a_machine_readable_code(self, tmp_path, monkeypatch):
        """Every add rejection names its cause in ``code``, not only in prose.

        The dashboard reads this field (``utils/errorReport``'s ``parseErrorCode``)
        to attach the failure's cause to an agent hand-off, and a first-time setup
        rejection is exactly the case where the user cannot diagnose it alone. A
        duplicate is kept distinct from an invalid field because the two are
        different user actions — rename versus correct — and a client that cannot
        tell them apart has to match on prose.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        state = _State(self._reg(tmp_path))
        body = {"name": "CD", "ssh_host": "cd-1-alias", "id": "cd-1"}
        assert asyncio.run(handlers.api_instances_add(_FakeReq(state, body=body))).status == 201

        dup = asyncio.run(handlers.api_instances_add(_FakeReq(state, body=body)))
        assert dup.status == 400 and _body(dup)["code"] == "instance_duplicate"

        no_json = asyncio.run(handlers.api_instances_add(_FakeReq(state)))
        assert no_json.status == 400 and _body(no_json)["code"] == "invalid_json"

        not_object = asyncio.run(handlers.api_instances_add(_FakeReq(state, body=["x"])))
        assert not_object.status == 400 and _body(not_object)["code"] == "invalid_body"

        bad_field = asyncio.run(
            handlers.api_instances_add(
                _FakeReq(state, body={"name": "Bad", "ssh_host": "h", "remote_port": "not-a-port"})
            )
        )
        assert bad_field.status == 400 and _body(bad_field)["code"] == "invalid_field"

        rejected = asyncio.run(
            handlers.api_instances_add(_FakeReq(state, body={"name": "", "ssh_host": ""}))
        )
        assert rejected.status == 400 and _body(rejected)["code"] == "instance_invalid"

    def test_update_paths(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = _State(reg)
        # success (only allowed fields applied)
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cd-1"}, body={"name": "New"})
            )
        )
        assert r.status == 200 and _body(r)["name"] == "New"
        # unknown id -> 404
        assert (
            asyncio.run(
                handlers.api_instances_update(
                    _FakeReq(state, match={"id": "ghost"}, body={"name": "x"})
                )
            ).status
            == 404
        )
        # invalid value -> 400
        assert (
            asyncio.run(
                handlers.api_instances_update(
                    _FakeReq(state, match={"id": "cd-1"}, body={"ssh_host": "bad host;rm"})
                )
            ).status
            == 400
        )
        # bad JSON / non-object body -> 400
        assert (
            asyncio.run(handlers.api_instances_update(_FakeReq(state, match={"id": "cd-1"}))).status
            == 400
        )
        assert (
            asyncio.run(
                handlers.api_instances_update(_FakeReq(state, match={"id": "cd-1"}, body=42))
            ).status
            == 400
        )

    def test_a_wrong_typed_patch_field_is_refused_not_reinterpreted(self, tmp_path, monkeypatch):
        """PATCH validated values but never their TYPE, so the decoded JSON went
        straight into the record: a non-string name reached `name.strip()` and
        answered 500, and `remote_port: true` validated as port 1 because bool is
        an int. Every case must be a 400 that leaves the record untouched -- and a
        400 specifically, because coercing the value would store a port or a name
        the caller never asked for.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1", remote_port=7777)
        state = _State(reg)
        for body in (
            {"name": 123},
            {"name": None},
            {"name": {"first": "CD"}},
            {"ssh_host": ["cd-1-alias"]},
            {"ttl": 20},
            {"remote_port": True},
            {"remote_port": "7778"},
            {"remote_port": 7778.5},
            {"connection_method": 1},
        ):
            r = asyncio.run(
                handlers.api_instances_update(_FakeReq(state, match={"id": "cd-1"}, body=body))
            )
            assert r.status == 400, f"{body!r} answered {r.status}"
            assert _body(r)["code"] == "instance_invalid"
        after = reg.get("cd-1")
        assert after.name == "CD" and after.ssh_host == "cd-1-alias"
        assert after.remote_port == 7777 and after.ttl == "20h"

    def test_every_editable_field_has_a_declared_type(self):
        """The allowed-field set is DERIVED from the type map, so a field cannot be
        made editable without a type to check it against. Pinned as a ratchet: the
        previous shape listed the fields twice, which is how a value reached a
        validator with no type check in front of it.
        """
        import inspect

        from kiro_crew.dashboard import handlers_instances as handlers

        src = inspect.getsource(handlers.api_instances_update)
        assert "allowed = set(_PATCH_FIELD_TYPES)" in src
        assert set(handlers._PATCH_FIELD_TYPES) >= {"name", "ssh_host", "remote_port", "ttl"}

    def test_update_tears_down_a_tunnel_its_own_edit_invalidated(self, tmp_path, monkeypatch):
        """A tunnel is built from ssh_host/remote_port/connection_method, so editing
        one of those leaves a live tunnel forwarding the OLD port to the OLD host
        under the new label. The edit must tear it down, and must keep
        ``was_connected`` — that flag records an explicit user disconnect, which an
        edit is not, and clearing it would drop the crew out of the switcher."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        reg.update("cd-1", was_connected=True)

        class _Manager:
            def __init__(self):
                self.disconnected = []

            async def disconnect(self, instance_id, *, keep_intent=False):
                self.disconnected.append((instance_id, keep_intent))
                # Mirror the real manager: the port hint always clears, and the
                # connect intent clears ONLY for an explicit user disconnect.
                hints = {"local_port": 0}
                if not keep_intent:
                    hints["was_connected"] = False
                reg.update(instance_id, **hints)
                return True

            def status(self, instance_id):
                return None

            def last_error(self, instance_id):
                return None

        mgr = _Manager()
        mgr.reconfigure = _fake_reconfigure(mgr)  # type: ignore[method-assign]
        state = _State(reg, manager=mgr)
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cd-1"}, body={"remote_port": 7999})
            )
        )
        assert r.status == 200 and _body(r)["remote_port"] == 7999
        # One teardown before the write. The post-write sweep is dated — it fires
        # only for a tunnel that connected before the record changed — and this
        # manager reports none live afterwards, so nothing more to tear down. The
        # teardown is a reconfiguration, so it must not claim to be a user
        # disconnect (that flag is what keeps the crew in the switcher).
        assert mgr.disconnected == [("cd-1", True)], (
            "the stale tunnel was left running, or the teardown claimed to be a " "user disconnect"
        )
        inst = reg.get("cd-1")
        assert inst is not None and inst.was_connected is True

    def test_ttl_beyond_the_minters_bound_is_refused(self, tmp_path, monkeypatch):
        """The token minters accept at most four digits. A ttl this layer lets
        through is persisted happily and then fails at the next connect, blaming
        the tunnel for a value the edit should have refused — so the registry
        enforces the same bound the minters do."""
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.registry import InvalidInstanceError

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        # Direct registry write: the API is not the only caller.
        with pytest.raises(InvalidInstanceError):
            reg.update("cd-1", ttl="99999h")
        # Accepted forms still are.
        assert reg.update("cd-1", ttl="9999h").ttl == "9999h"
        assert reg.update("cd-1", ttl="30m").ttl == "30m"

        state = _State(reg)
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cd-1"}, body={"ttl": "99999h"})
            )
        )
        assert r.status == 400 and _body(r)["code"] == "instance_invalid"

    def test_update_refuses_an_invalid_edit_without_touching_the_tunnel(
        self, tmp_path, monkeypatch
    ):
        """A rejected save must not cost the user their connection: the proposed
        record is validated before the teardown, so a typo answers 400 with the
        crew still connected."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        reg.update("cd-1", was_connected=True)

        class _Manager:
            def __init__(self):
                self.disconnected = []

            async def disconnect(self, instance_id, *, keep_intent=False):
                self.disconnected.append(instance_id)
                return True

            def status(self, instance_id):
                return None

            def last_error(self, instance_id):
                return None

        mgr = _Manager()
        mgr.reconfigure = _fake_reconfigure(mgr)  # type: ignore[method-assign]
        state = _State(reg, manager=mgr)
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cd-1"}, body={"ssh_host": "bad host;rm"})
            )
        )
        assert r.status == 400 and _body(r)["code"] == "instance_invalid"
        assert mgr.disconnected == [], "a rejected edit tore down the tunnel anyway"
        inst = reg.get("cd-1")
        assert inst is not None
        assert inst.ssh_host == "cd-1-alias" and inst.was_connected is True

    def test_update_restores_intent_even_when_the_sweep_found_no_tunnel(
        self, tmp_path, monkeypatch
    ):
        """``disconnect()`` clears the persisted intent whether or not it tracked
        a live tunnel, and reports False in that case. Restoring only on a True
        return would drop the crew out of the switcher."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        reg.update("cd-1", was_connected=True)

        class _NoTunnelManager:
            async def disconnect(self, instance_id, *, keep_intent=False):
                # Mirrors the real manager: the registry cleanup runs even with
                # no live tunnel tracked, and the return value is False.
                hints = {"local_port": 0}
                if not keep_intent:
                    hints["was_connected"] = False
                reg.update(instance_id, **hints)
                return False

            def status(self, instance_id):
                return None

            def last_error(self, instance_id):
                return None

        no_tunnel = _NoTunnelManager()
        no_tunnel.reconfigure = _fake_reconfigure(no_tunnel)  # type: ignore[method-assign]
        state = _State(reg, manager=no_tunnel)
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cd-1"}, body={"remote_port": 7999})
            )
        )
        assert r.status == 200
        inst = reg.get("cd-1")
        assert inst is not None and inst.remote_port == 7999
        assert inst.was_connected is True, "the crew lost its switcher entry"

    def test_update_tears_the_tunnel_down_exactly_once(self, tmp_path, monkeypatch):
        """One teardown, inside the reconfiguration. An extra one after the write
        would hit whatever connected next — i.e. a Connect the user just made on
        the new coordinates."""
        import time as _time

        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        class _FreshTunnelManager:
            def __init__(self):
                self.disconnect_calls = 0

            async def disconnect(self, instance_id, *, keep_intent=False):
                self.disconnect_calls += 1
                return True

            def status(self, instance_id):
                # Connected just now — i.e. after the write this handler is about
                # to make, which is the case the dated sweep must spare.
                # flake-ok: pre-existing live-clock stamp set 60 s ahead so it reads newer than the edit's write, moved verbatim
                return TunnelStatus(
                    instance_id=instance_id,
                    state=TunnelState.CONNECTED,
                    connected_at=_time.time() + 60,
                )

            def last_error(self, instance_id):
                return None

            def token_ttl_remaining(self, instance_id):
                return None

            def token_ttl_total(self, instance_id):
                return None

        mgr = _FreshTunnelManager()
        mgr.reconfigure = _fake_reconfigure(mgr)  # type: ignore[method-assign]
        state = _State(reg, manager=mgr)
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cd-1"}, body={"remote_port": 7999})
            )
        )
        assert r.status == 200 and _body(r)["remote_port"] == 7999
        # Only the pre-edit teardown ran; the sweep spared the newer tunnel.
        assert mgr.disconnect_calls == 1

    def test_update_does_not_revive_a_crew_disconnected_mid_edit(self, tmp_path, monkeypatch):
        """An explicit Disconnect landing while a transport edit is in flight must
        win. The edit's teardown preserves intent rather than restoring a snapshot
        of it, so the user's disconnect is not overwritten."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        reg.update("cd-1", was_connected=True)

        class _DisconnectMidEdit:
            def __init__(self):
                self.calls = 0

            async def disconnect(self, instance_id, *, keep_intent=False):
                self.calls += 1
                hints = {"local_port": 0}
                if not keep_intent:
                    hints["was_connected"] = False
                reg.update(instance_id, **hints)
                if self.calls == 1:
                    # The user presses Disconnect while the save is in flight.
                    reg.update(instance_id, was_connected=False)
                return True

            def status(self, instance_id):
                return None

            def last_error(self, instance_id):
                return None

        mid_edit = _DisconnectMidEdit()
        mid_edit.reconfigure = _fake_reconfigure(mid_edit)  # type: ignore[method-assign]
        state = _State(reg, manager=mid_edit)
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cd-1"}, body={"remote_port": 7999})
            )
        )
        assert r.status == 200 and _body(r)["remote_port"] == 7999
        inst = reg.get("cd-1")
        assert inst is not None
        assert inst.was_connected is False, "the edit revived a crew the user disconnected"
        # The response must report the same thing, so the dashboard does not
        # reconnect off a stale view.
        assert _body(r)["was_connected"] is False

    def test_update_rewrites_the_coordinates_inside_the_teardown_critical_section(
        self, tmp_path, monkeypatch
    ):
        """The teardown and the coordinate rewrite must reach the manager as ONE
        operation. Done as two, a connect can read the OLD record in between, and
        whether its tunnel is CONNECTED or still CONNECTING when the write lands
        decides whether anything notices — so the handler must not persist on its
        own when a manager is present."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        reg.update("cd-1", was_connected=True)

        events: list[str] = []

        class _OrderingManager:
            async def disconnect(self, instance_id, *, keep_intent=False):
                events.append(f"teardown(keep_intent={keep_intent})")
                return True

            async def reconfigure(self, instance_id, apply):
                events.append("enter-critical-section")
                await self.disconnect(instance_id, keep_intent=True)
                out = apply()
                events.append("leave-critical-section")
                return out

            def status(self, instance_id):
                return None

            def last_error(self, instance_id):
                return None

            def token_ttl_remaining(self, instance_id):
                return None

            def token_ttl_total(self, instance_id):
                return None

        state = _State(reg, manager=_OrderingManager())
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cd-1"}, body={"remote_port": 7999})
            )
        )
        assert r.status == 200 and _body(r)["remote_port"] == 7999
        assert events == [
            "enter-critical-section",
            "teardown(keep_intent=True)",
            "leave-critical-section",
        ], events
        inst = reg.get("cd-1")
        assert inst is not None and inst.was_connected is True

    def test_update_refuses_to_save_when_the_tunnel_cannot_be_torn_down(
        self, tmp_path, monkeypatch
    ):
        """A stop that failed leaves the OLD forward live.

        Persisting the new coordinates then leaves the record describing one
        machine while the still-open tunnel serves another — and that tunnel is
        the one the user reaches. So the edit aborts, nothing is written, and the
        caller is told to disconnect and retry.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        reg.update("cd-1", was_connected=True)

        class _WedgedManager:
            async def reconfigure(self, instance_id, apply):
                raise OSError("ssh process will not die")

            async def disconnect(self, instance_id, *, keep_intent=False):
                raise OSError("ssh process will not die")

            def status(self, instance_id):
                return None

            def last_error(self, instance_id):
                return None

            def token_ttl_remaining(self, instance_id):
                return None

            def token_ttl_total(self, instance_id):
                return None

        state = _State(reg, manager=_WedgedManager())
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cd-1"}, body={"ssh_host": "cd-2-alias"})
            )
        )
        assert r.status == 503 and _body(r)["code"] == "tunnel_teardown_failed"
        inst = reg.get("cd-1")
        assert inst is not None
        assert inst.ssh_host == "cd-1-alias", "coordinates advanced over a live tunnel"
        assert inst.was_connected is True

    def test_update_leaves_a_healthy_tunnel_alone_when_only_the_label_changes(
        self, tmp_path, monkeypatch
    ):
        """A rename does not change how the tunnel is opened, so dropping the
        connection for it would be a self-inflicted outage."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        class _Manager:
            def __init__(self):
                self.disconnected = []

            async def disconnect(self, instance_id, *, keep_intent=False):
                self.disconnected.append(instance_id)
                return True

            def status(self, instance_id):
                return None

            def last_error(self, instance_id):
                return None

        mgr = _Manager()
        mgr.reconfigure = _fake_reconfigure(mgr)  # type: ignore[method-assign]
        state = _State(reg, manager=mgr)
        # Re-sending the SAME host alongside a new name is still only a rename.
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(
                    state,
                    match={"id": "cd-1"},
                    body={"name": "Renamed", "ssh_host": "cd-1-alias"},
                )
            )
        )
        assert r.status == 200 and _body(r)["name"] == "Renamed"
        assert mgr.disconnected == []

    def test_rename_persists_in_instances_json_and_survives_reload(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.registry import InstancesRegistry

        _enable(tmp_path, monkeypatch)
        path = tmp_path / "instances.json"
        reg = InstancesRegistry(path=path)
        reg.add(name="Old name", ssh_host="crew-host", instance_id="crew-1")
        state = _State(reg)

        response = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "crew-1"}, body={"name": "New name"})
            )
        )

        assert response.status == 200
        assert _body(response)["name"] == "New name"
        assert InstancesRegistry(path=path).get("crew-1").name == "New name"

    def test_blank_rename_is_rejected_without_changing_the_stored_name(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.registry import InstancesRegistry

        _enable(tmp_path, monkeypatch)
        path = tmp_path / "instances.json"
        reg = InstancesRegistry(path=path)
        reg.add(name="Keep me", ssh_host="crew-host", instance_id="crew-1")

        response = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(_State(reg), match={"id": "crew-1"}, body={"name": "   "})
            )
        )

        assert response.status == 400
        assert _body(response)["code"] == "instance_invalid"
        assert InstancesRegistry(path=path).get("crew-1").name == "Keep me"

    def test_remove_success_and_404(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = _State(reg)
        r = asyncio.run(handlers.api_instances_remove(_FakeReq(state, match={"id": "cd-1"})))
        assert r.status == 200 and _body(r)["removed"] == "cd-1"
        assert (
            asyncio.run(
                handlers.api_instances_remove(_FakeReq(state, match={"id": "ghost"}))
            ).status
            == 404
        )

    def test_remove_sweeps_a_reconnect_that_raced_the_removal(self, tmp_path, monkeypatch):
        """The offloaded reg.remove yields the loop between the pre-removal
        disconnect and the deletion, so a tab reconnect can re-establish a
        tunnel for the record mid-delete. A successful removal must disconnect
        AGAIN afterwards: the record is gone by then, so connect refuses new
        attempts and the sweep tears down whatever slipped in."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        class _RacingManager:
            def __init__(self):
                self.disconnect_calls = 0
                self.live = False

            async def disconnect(self, instance_id, *, keep_intent=False):
                self.disconnect_calls += 1
                if self.disconnect_calls == 1:
                    # A reconnect slips in right after the pre-removal teardown.
                    self.live = True
                else:
                    self.live = False
                return True

        mgr = _RacingManager()
        mgr.reconfigure = _fake_reconfigure(mgr)  # type: ignore[method-assign]
        state = _State(reg, manager=mgr)
        r = asyncio.run(handlers.api_instances_remove(_FakeReq(state, match={"id": "cd-1"})))
        assert r.status == 200
        assert mgr.disconnect_calls == 2, "no post-removal teardown sweep ran"
        assert mgr.live is False, "the racing reconnect's tunnel survived the removal"

    def test_connect_503_404_and_502(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        # manager unavailable -> 503
        assert (
            asyncio.run(
                handlers.api_instances_connect(_FakeReq(_State(reg, None), match={"id": "cd-1"}))
            ).status
            == 503
        )

        class FakeMgr:
            async def connect(self, iid):
                if iid == "ghost":
                    raise KeyError(iid)
                return TunnelStatus(iid, TunnelState.ERROR, error="boom")

            def get_token(self, iid):
                return ""

        state = _State(reg, FakeMgr())
        # KeyError -> 404
        assert (
            asyncio.run(
                handlers.api_instances_connect(_FakeReq(state, match={"id": "ghost"}))
            ).status
            == 404
        )
        # non-connected result -> 502 with the error surfaced
        r = asyncio.run(handlers.api_instances_connect(_FakeReq(state, match={"id": "cd-1"})))
        assert r.status == 502 and _body(r)["error"] == "boom"

    def test_disconnect_reports_was_connected(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)

        class FakeMgr:
            async def disconnect(self, iid):
                return True

        r = asyncio.run(
            handlers.api_instances_disconnect(
                _FakeReq(_State(self._reg(tmp_path), FakeMgr()), match={"id": "cd-1"})
            )
        )
        assert r.status == 200 and _body(r)["was_connected"] is True

    def test_restart_paths(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        # known id but no manager -> 503
        assert (
            asyncio.run(
                handlers.api_instances_restart(_FakeReq(_State(reg, None), match={"id": "cd-1"}))
            ).status
            == 503
        )
        # unknown id -> 404 (checked before the manager)
        assert (
            asyncio.run(
                handlers.api_instances_restart(
                    _FakeReq(_State(reg, object()), match={"id": "ghost"})
                )
            ).status
            == 404
        )

        class FakeMgr:
            def __init__(self, ok):
                self._ok = ok

            async def restart_remote(self, iid):
                return {"ok": self._ok, "message": "" if self._ok else "fail"}

        # success -> 200
        r = asyncio.run(
            handlers.api_instances_restart(
                _FakeReq(_State(reg, FakeMgr(True)), match={"id": "cd-1"})
            )
        )
        assert r.status == 200 and _body(r)["ok"] is True
        # failure -> 502
        r = asyncio.run(
            handlers.api_instances_restart(
                _FakeReq(_State(reg, FakeMgr(False)), match={"id": "cd-1"})
            )
        )
        assert r.status == 502

    def test_audit_failure_never_breaks_request(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)

        def boom():
            raise RuntimeError("sel unavailable")

        # _audit swallows SEL failures so the control plane stays available.
        monkeypatch.setattr(handlers, "sel", boom)
        r = asyncio.run(handlers.api_instances_list(_FakeReq(_State(self._reg(tmp_path)))))
        assert r.status == 200

    def test_update_locks_addressing_fields_for_correlated_cloud_instance(
        self, tmp_path, monkeypatch
    ):
        # PATCH must not let a non-dashboard caller
        # (CLI, script, agent) rewrite the fields Stop/Start/Delete use to
        # resolve an EC2 stack launched by Kiro Crew — doing so strands a running,
        # billing instance with no dashboard path to reach it.
        from kiro_crew.cloud import launch_job as lj
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        launched = reg.add(
            name="Cloud",
            connection_method="ssm",
            ssm_target="i-0123abcd",
            aws_profile="prod",
            aws_region="us-east-1",
            instance_id="cloud-launched",
        )
        # A job Kiro Crew provisioned whose EC2 instance id matches the
        # instance's ssm_target — this is what makes it "correlated".
        store = lj.LaunchJobStore()  # honours KIROCREW_HOME, same as production
        job = store.create(profile="prod", region="us-east-1", size_key="light")
        job.instance_id = "i-0123abcd"
        store.save(job)
        state = _State(reg)

        # Editing an addressing field is rejected...
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cloud-launched"}, body={"aws_region": "us-west-2"})
            )
        )
        assert r.status == 400
        body = _body(r)
        assert body["code"] == "cloud_instance_addressing_locked"
        # ...and the record is untouched.
        assert reg.get("cloud-launched").aws_region == "us-east-1"

        # A non-addressing field on the same correlated instance is still editable.
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cloud-launched"}, body={"name": "Renamed"})
            )
        )
        assert r.status == 200 and _body(r)["name"] == "Renamed"

        # A hand-added SSM instance whose ssm_target matches no launch job is
        # NOT correlated — its addressing fields stay editable.
        reg.add(
            name="Hand-added",
            connection_method="ssm",
            ssm_target="i-89ab1234",
            instance_id="cloud-manual",
        )
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cloud-manual"}, body={"aws_region": "eu-west-1"})
            )
        )
        assert r.status == 200 and _body(r)["aws_region"] == "eu-west-1"

        assert launched.ssm_target == "i-0123abcd"  # confidence check: fixture unchanged

    def test_update_fails_closed_when_correlation_check_errors(self, tmp_path, monkeypatch):
        # If the launch job store can't be read, the correlation check must
        # NOT fall back to "not correlated" — that would let this addressing
        # edit through and strand a launched, billing instance. The PATCH
        # refuses the edit instead of persisting one it
        # could not verify was safe.
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(
            name="Cloud",
            connection_method="ssm",
            ssm_target="i-0123abcd",
            aws_profile="prod",
            aws_region="us-east-1",
            instance_id="cloud-launched",
        )
        state = _State(reg)

        def boom(ssm_target):
            raise OSError("disk unavailable")

        monkeypatch.setattr(handlers, "_is_correlated_cloud_instance", boom)

        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cloud-launched"}, body={"aws_region": "us-west-2"})
            )
        )
        assert r.status == 503
        assert _body(r)["code"] == "cloud_instance_correlation_check_failed"
        # ...and the record is untouched.
        assert reg.get("cloud-launched").aws_region == "us-east-1"

        # A non-addressing field never invokes the (broken) correlation check.
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cloud-launched"}, body={"name": "Renamed"})
            )
        )
        assert r.status == 200 and _body(r)["name"] == "Renamed"
