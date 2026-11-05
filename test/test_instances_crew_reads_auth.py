"""Tests for the crew-scoped session reads: who may call.

``_authorize_crew_read`` is a POSITIVE gate, not a denylist: the internal secret
alone admits nobody; the calling session key must be present and attested; only
a ``dashboard:`` key may continue, and only while it names a live tab that is not
app-scoped, has a turn running, was not opened or steered by a channel participant,
is running a turn the PERSON sent, and was not opened by automation on another
caller's behalf. Every other namespace -- ``subagent``, ``side``, ``cron``, the
channel namespaces, the legacy ``channel:`` shape, the workflow family, anything
minted later -- is refused by name with nothing resolved on its behalf. Each arm is
pinned with its admitted control so the gate cannot be satisfied by refusing
everyone.

Shared doubles come from ``instances_crew_reads_helpers`` (this family) and
``instances_helpers`` (every per-topic instances test).
"""

from __future__ import annotations

import asyncio

import pytest
from instances_crew_reads_helpers import CrewReadsCase
from instances_helpers import _body, _enable, _FakeReq, _State

_ROUTE_QUERY = {"search": {"q": "hello"}, "list": {}, "read": {"key": "k1"}}


class TestCrewReadAuthorization(CrewReadsCase):
    """The owner's-own-tab gate on api_crew_sessions_search|list|read."""

    def test_the_flag_is_off_by_default(self):
        """``instances.crew_reads_enabled`` is a separate opt-in on top of
        ``instances.enabled``: a peer's transcripts enter the agent's context as
        untrusted text, so turning Instances on must not turn this on with it."""
        from kiro_crew.config.sections import InstancesConfig

        assert InstancesConfig().crew_reads_enabled is False
        assert InstancesConfig(enabled=True).crew_reads_enabled is False

    @pytest.mark.parametrize("route", ["search", "list", "read"])
    def test_every_leg_refuses_while_the_flag_is_off(self, tmp_path, monkeypatch, route):
        """The flag is the FIRST arm: with it off, the owner's own tab mid-turn (the
        one caller every other arm admits) is refused with ``crew_reads_disabled``,
        the refusal is audited, nothing is resolved for the caller and no peer is
        reached. Its own code, so the tool can tell "disabled here" from a refused
        identity."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch, crew_reads=False)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        events: list[tuple[str, str]] = []
        monkeypatch.setattr(
            handlers, "_audit", lambda op, outcome, **kw: events.append((outcome, kw.get("error")))
        )
        resolved: list[str] = []
        from kiro_crew.dashboard.handlers import guide

        real_resolve = guide._resolve_agent_caller

        async def _spy(request, operation):
            resolved.append(operation)
            return await real_resolve(request, operation)

        monkeypatch.setattr(guide, "_resolve_agent_caller", _spy)
        mgr = self._three_leg_mgr()
        state = _State(reg, mgr)
        leg = getattr(handlers, f"api_crew_sessions_{route}")
        r = asyncio.run(leg(self._internal_req(state, crew="cd-1", **_ROUTE_QUERY[route])))
        assert r.status == 403
        assert _body(r)["code"] == "crew_reads_disabled"
        assert events == [("denied", "crew_reads_disabled")]
        assert resolved == [], "a disabled install must not resolve the caller"
        assert getattr(mgr, "last_path", None) is None, "a disabled install must not reach a peer"

    def _three_leg_mgr(self):
        return self._mgr_with(
            search=(True, {"sessions": []}),
            list_body=b'{"sessions": []}',
            read_body=b'[{"role": "user", "content": "hi"}]',
        )

    def _read(self, handlers, req):
        return asyncio.run(handlers.api_crew_sessions_read(req))

    # ── the control: the person's own tab, mid-turn ───────────────────────

    @pytest.mark.parametrize("route", ["search", "list", "read"])
    def test_every_leg_admits_the_owners_own_tab_mid_turn(self, tmp_path, monkeypatch, route):
        """The caller the feature exists for: the person's agent, in the person's
        tab, answering a message the person sent. If this 403s, the gate has cost
        the capability it exists to confine -- so it is the control for every
        refusal below, pinned per leg because each discloses a different slice."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = self._owner_state(reg, mgr=self._three_leg_mgr())
        req = self._keyed_req(state, "dashboard:ui", crew="cd-1", **_ROUTE_QUERY[route])
        r = asyncio.run(getattr(handlers, f"api_crew_sessions_{route}")(req))
        assert r.status == 200

    @pytest.mark.parametrize(
        ("session_key", "slot_id"),
        [("dashboard:ui", "ui"), ("dashboard_member-alice", "member-alice")],
    )
    def test_read_admits_both_spellings_of_a_live_owner_tab(
        self, tmp_path, monkeypatch, session_key, slot_id
    ):
        """A live key is ``dashboard:<slot>`` and a persisted stem folds the separator
        to ``dashboard_<slot>``; the attestation speaks for either form, and both
        must resolve to the same live slot rather than one of them reading as a
        slot that does not exist."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = self._owner_state(reg, slot=slot_id)
        r = self._read(handlers, self._keyed_req(state, session_key, crew="cd-1", key="k1"))
        assert r.status == 200

    # ── the door: secret, app claim, attestation ─────────────────────────

    def test_crew_read_rejects_app_token_caller(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        # internal_auth is present, but the caller is an app-token identity
        # (request["app"] set) -- crew reads are owner-only, like the federated route.
        req = _FakeReq(self._owner_state(reg), query={"crew": "cd-1", "key": "k1"})
        req._attrs["internal_auth"] = True
        req._attrs["app"] = "some-app"
        r = self._read(handlers, req)
        assert r.status == 403
        assert _body(r)["code"] == "app_caller"

    @pytest.mark.parametrize("session_key", ["", "   "])
    def test_read_refuses_a_caller_with_no_session_key(self, tmp_path, monkeypatch, session_key):
        """An ABSENT key is a caller that declined to name itself, not the gateway.

        The internal secret proves a request came through the local gateway and
        nothing more: every process the gateway spawns holds it, an agent-authored
        cron script included. The gateway never reads a peer's transcripts for
        itself, so there is no legitimate headless caller on this surface, and a
        guard that admitted absence would be bypassed by omitting one header. A
        whitespace-only key is the same absence after the strip the door applies.
        The shared caller resolver names the case (``missing_session_key``, 400),
        and the namespace arm deliberately leaves an absent key to it.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = self._owner_state(reg)
        req = (
            self._bare_req(state, crew="cd-1", key="k1")
            if not session_key
            else self._keyed_req(state, session_key, crew="cd-1", key="k1")
        )
        r = self._read(handlers, req)
        assert r.status == 400
        assert _body(r)["code"] == "missing_session_key"

    @pytest.mark.parametrize("route", ["search", "list", "read"])
    def test_every_leg_refuses_an_unattested_session_key(self, tmp_path, monkeypatch, route):
        """A PRESENT key with nothing behind it is the caller's own word.

        Any same-machine process holding the internal secret can read the owner's
        ``dashboard:`` key off a transcript and declare it here. The transport has
        two ways to vouch for a declared key -- the Unix-socket peer attestation or
        the launcher's signed per-session token -- and with neither the door
        refuses before the slot is ever looked at, even though the slot it names
        is the owner's own tab mid-turn.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = self._owner_state(reg, mgr=self._three_leg_mgr())
        req = self._keyed_req(
            state, "dashboard:ui", attested=False, crew="cd-1", **_ROUTE_QUERY[route]
        )
        r = asyncio.run(getattr(handlers, f"api_crew_sessions_{route}")(req))
        assert r.status == 403
        assert _body(r)["code"] == "unattested_caller"

    def test_read_admits_a_key_attested_by_the_session_token(self, tmp_path, monkeypatch):
        """The token branch is the MCP launcher's attestation and must admit too.

        ``mcp_core._get`` forwards the per-session token beside the key; a hub whose
        tool servers reach it over loopback rather than the Unix socket has no peer
        attestation, so refusing the token branch would refuse every real tool call
        on that deployment. The verifier is stubbed to the one answer under test.
        """
        from kiro_crew import member_memory_auth
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        # The verifier is read by the shared caller resolver, which imports it from
        # its home module at call time, so it is stubbed there.
        monkeypatch.setattr(
            member_memory_auth,
            "session_key_is_attested",
            lambda request, key: request.headers.get("X-Session-Token") == f"tok-for-{key}",
        )
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = self._owner_state(reg)
        req = self._keyed_req(state, "dashboard:ui", attested=False, crew="cd-1", key="k1")
        req.headers["X-Session-Token"] = "tok-for-dashboard:ui"
        assert self._read(handlers, req).status == 200
        req.headers["X-Session-Token"] = "tok-for-dashboard:someone-else"
        r = self._read(handlers, req)
        assert r.status == 403
        assert _body(r)["code"] == "unattested_caller"

    # ── the private-memory fence, held on the gateway side too ───────────

    @pytest.mark.parametrize("route", ["search", "list", "read"])
    @pytest.mark.parametrize(
        ("binding", "why"),
        [
            (lambda key: "store-writer-v2", "private-memory session"),
            (
                lambda key: (_ for _ in ()).throw(OSError("seatbelt")),
                "private-memory binding unreadable",
            ),
            (
                lambda key: (_ for _ in ()).throw(ValueError("corrupt")),
                "private-memory binding unreadable",
            ),
        ],
    )
    def test_every_leg_refuses_a_session_fenced_to_a_private_store(
        self, tmp_path, monkeypatch, route, binding, why
    ):
        """The fence the three tools apply before any wire call, held here as well.

        The tool process is not the only caller that can present the internal
        secret and an attested key: a fenced member's own shell can read the
        sandbox-visible ``.local_secret`` and call this route over the socket with
        its own key, which the peer check attests because the process IS that
        session, and every other arm then admits it (``dashboard:`` key, live tab,
        owner-sent turn). So the attested key is resolved through
        ``private_memory_store_for_session`` here too: a non-empty store refuses,
        and so does a binding that cannot be read, since "unknown" on a cross-crew
        read fails closed. Audited with the reason, as every arm is.
        """
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.dashboard import handlers_instances_crew_reads as owner

        _enable(tmp_path, monkeypatch)
        monkeypatch.setattr(owner, "private_memory_store_for_session", binding)
        events: list[tuple[str, str]] = []
        monkeypatch.setattr(
            handlers,
            "_audit",
            lambda op, outcome, **kw: events.append((outcome, kw.get("error", ""))),
        )
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = self._owner_state(reg, mgr=self._three_leg_mgr())
        req = self._keyed_req(state, "dashboard:ui", crew="cd-1", **_ROUTE_QUERY[route])
        r = asyncio.run(getattr(handlers, f"api_crew_sessions_{route}")(req))
        assert r.status == 403
        assert _body(r)["code"] == "private_memory"
        assert events == [("denied", why)]
        assert not hasattr(state.instances_manager, "last_path"), "refused before the tunnel"

    def test_an_ordinary_session_resolves_to_no_store_and_is_admitted(self, tmp_path, monkeypatch):
        """The control: ``""`` is an ordinary session and the fence lets it through."""
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.dashboard import handlers_instances_crew_reads as owner

        _enable(tmp_path, monkeypatch)
        seen: list[str] = []

        def _binding(key):
            seen.append(key)
            return ""

        monkeypatch.setattr(owner, "private_memory_store_for_session", _binding)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = self._owner_state(reg)
        assert (
            self._read(
                handlers, self._keyed_req(state, "dashboard:ui", crew="cd-1", key="k1")
            ).status
            == 200
        )
        assert seen == ["dashboard:ui"], "resolved on the attested key, once"

    # ── the namespace: only the owner's dashboard mint may continue ──────
    #
    # A delegated key proves the GATEWAY minted it, never for whom: `spawn_run`
    # mints `subagent:<id>` identically whether the parent is the person's tab or
    # a Slack participant's thread, and every record that could say which is one
    # an agent can write or one the parent's turn end resets. So nothing is
    # resolved: the namespace alone decides, and only `dashboard` passes.

    @pytest.mark.parametrize("route", ["search", "list", "read"])
    def test_every_leg_rejects_a_channel_minted_caller(self, tmp_path, monkeypatch, route):
        """The identity an app-claim-only check could not see: a channel-born slot
        is created without an ``_app``, so ``derive_caller_app`` returns ``""`` and
        the Slack participant arrives shaped exactly like the owner. One per route,
        since a guard added to one leg is not a guard on the others."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = self._owner_state(reg, mgr=self._three_leg_mgr())
        req = self._channel_req(state, crew="cd-1", **_ROUTE_QUERY[route])
        r = asyncio.run(getattr(handlers, f"api_crew_sessions_{route}")(req))
        assert r.status == 403
        assert _body(r)["code"] == "owner_only"

    @pytest.mark.parametrize(
        "session_key",
        [
            # a Slack participant's child: live, well-formed, and its parent is the
            # owner's tab -- the walk that once admitted it is gone, the name refuses
            "subagent:a1",
            "subagent_a1",
            "subagent:",
            # a side chat hanging off the owner's own tab
            "side:ui:3",
            "side:ui",
            # a cron, in every spelling, even when its job row names the owner
            "cron:deadbeef",
            "cron:deadbeef:run-7",
            "cron_deadbeef",
            # provenance the session-address RFC leaves open
            "secretary:notes",
            # the legacy app-platform channel shape the channel roster omits
            "channel:C0123:some-agent",
            # the workflow family and taskrunner: originating key is caller-influenced
            "wf:run1:0",
            "wf-pool:run1:w2",
            "wf-unpooled:run1:3",
            "wf-worker:run1:1",
            "wf-author:run1:a1",
            "taskrunner:run:spec",
            # a transport minted after this gate was written
            "some-future-transport:42",
        ],
    )
    def test_read_refuses_every_namespace_but_the_owners_dashboard(
        self, tmp_path, monkeypatch, session_key
    ):
        """Refused by NAME, with nothing resolved on the caller's behalf.

        The owner's tab is live and mid-turn throughout, and for the subagent and
        side keys it is exactly the parent the caller would have resolved to -- so
        the only thing refusing them is that no resolution is attempted. That is
        the property under test: a delegated key's ancestry is readable only from
        records an agent can write (a run folder, a slot's restored ``created_by``,
        ``crons.json``) or from per-turn flags the parent's turn end resets while a
        background child still runs, and each was found to admit a channel
        participant's or a cron's work. The direction of failure for an unknown
        namespace is the same: closed.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = self._owner_state(reg)
        r = self._read(handlers, self._keyed_req(state, session_key, crew="cd-1", key="k1"))
        assert r.status == 403
        assert _body(r)["code"] == "owner_only"

    # ── the slot: the person, in this tab, right now ──────────────────────

    def test_read_rejects_a_dashboard_key_whose_slot_is_gone(self, tmp_path, monkeypatch):
        """An owner-SHAPED key whose confinement went missing.

        A ``dashboard:`` key names one specific slot. When an APP-owned tab is
        closed while one of its calls is still in flight, the slot is popped without
        draining, so the app claim that would have refused the caller is absent --
        and the namespace is the owner's. Every positive signal then reads as the
        person, which is why the shared resolver's live-slot arm has its own code
        (``no_live_slot``): an audit reader sees a vanished slot rather than a
        rejected identity.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = self._owner_state(reg, slot="still-open")
        req = self._keyed_req(state, "dashboard:popped-tab", crew="cd-1", key="k1")
        r = self._read(handlers, req)
        assert r.status == 409
        assert _body(r)["code"] == "no_live_slot"

    @pytest.mark.parametrize("session_key", ["dashboard:ui", "dashboard_ui"])
    def test_read_refuses_a_dashboard_key_that_names_no_live_slot(
        self, tmp_path, monkeypatch, session_key
    ):
        """A key with no slot behind it can run no turn, so there is no one to
        authorize. ``dashboard:ui`` is exempt from the popped-tab predicate (it
        names no per-tab slot there), but it is not exempt from needing a live tab
        here: with no registry entry it takes the same refusal, whichever spelling
        arrives -- the shared resolver's ``no_live_slot``, which the guide routes
        answer for the same shape."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = _State(reg, self._mgr_with(read_body=b'[{"role": "user", "content": "hi"}]'))
        state._slots = self._slots()  # a registry with nothing live
        r = self._read(handlers, self._keyed_req(state, session_key, crew="cd-1", key="k1"))
        assert r.status == 409
        assert _body(r)["code"] == "no_live_slot"

    def test_read_refuses_an_app_scoped_slot(self, tmp_path, monkeypatch):
        """A slot an app owns is the app's whatever key its turn runs under.

        The door's ``request["app"]`` arm fires only when ``token_auth`` resolved
        the claim; a slot whose registry row carries ``_app`` is refused from the
        slot itself, so the two cannot disagree about whose tab this is.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = self._owner_state(reg, _app="some-app")
        r = self._read(handlers, self._keyed_req(state, "dashboard:ui", crew="cd-1", key="k1"))
        assert r.status == 403
        assert _body(r)["code"] == "app_scoped_caller"

    def test_read_refuses_a_slot_with_no_turn_running(self, tmp_path, monkeypatch):
        """The call must come from a turn running in THIS slot.

        A channel's own session answering a message the dashboard merely mirrors
        runs no turn in the mirrored slot, and a copied or stale key names a slot
        with nothing in flight. Either way nobody the gate can attribute is making
        the call. The same arm the guide routes apply (``no_dashboard_turn``).
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = self._owner_state(reg, turn_running=False)
        r = self._read(handlers, self._keyed_req(state, "dashboard:ui", crew="cd-1", key="k1"))
        assert r.status == 409
        assert _body(r)["code"] == "no_dashboard_turn"

    @pytest.mark.parametrize(
        "flags", [{"_turn_channel_origin": True}, {"_turn_channel_narrowed": True}]
    )
    @pytest.mark.parametrize("route", ["search", "list", "read"])
    def test_every_leg_refuses_a_channel_turn_running_in_a_dashboard_slot(
        self, tmp_path, monkeypatch, route, flags
    ):
        """A Slack or Discord thread bound to a dashboard session runs its turns
        under that tab's own ``dashboard:<slot>`` key, so an allow-listed
        participant who is not the owner arrives shaped exactly like the owner. The
        turn's provenance, as the gateway recorded it at the turn's start (a turn a
        channel message opened) or at a steer's admission (a channel steer into the
        owner's turn), is what refuses -- the same two flags the guide routes read,
        never the agent-writable metadata line."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = self._owner_state(reg, mgr=self._three_leg_mgr(), **flags)
        req = self._keyed_req(state, "dashboard:ui", crew="cd-1", **_ROUTE_QUERY[route])
        r = asyncio.run(getattr(handlers, f"api_crew_sessions_{route}")(req))
        assert r.status == 403
        assert _body(r)["code"] == "channel_caller"

    @pytest.mark.parametrize("route", ["search", "list", "read"])
    def test_every_leg_refuses_a_turn_the_person_did_not_send(self, tmp_path, monkeypatch, route):
        """The positive witness that the person is at this tab NOW.

        ``_turn_user_sent`` is set at the turn's start only for a turn the person
        sent from the dashboard, and cleared by a steer that is not the user's: a
        ``session_send`` into the owner's tab, a cron or app injection, a loop wake
        and a sub-agent completion all run turns in the owner's own slot under the
        owner's own key, and none of them is the person. This is the arm the
        authorization rests on, because it is the one witness the gateway holds
        that neither an agent's file tools nor a restart can produce: a slot's
        creator can be cleared or lost, its turn's opener cannot be invented.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = self._owner_state(reg, mgr=self._three_leg_mgr(), _turn_user_sent=False)
        req = self._keyed_req(state, "dashboard:ui", crew="cd-1", **_ROUTE_QUERY[route])
        r = asyncio.run(getattr(handlers, f"api_crew_sessions_{route}")(req))
        assert r.status == 403
        assert _body(r)["code"] == "not_user_turn"

    @pytest.mark.parametrize("route", ["search", "list", "read"])
    def test_every_leg_refuses_a_turn_a_non_owner_dashboard_user_sent(
        self, tmp_path, monkeypatch, route
    ):
        """A person at a dashboard tab who is not the OWNER.

        ``/kirocrew dashboard`` mints a dashboard token for a channel user the
        install allow-lists (Telegram, Teams, Webex), and their sends reach
        ``api_chat`` as a non-app request: ``_turn_user_sent`` is True for their
        turn exactly as for the owner's, because it records that a person sent the
        turn and not which one. The tool call carries no ``user`` claim, so the
        dashboard-subject arm never sees them either. The owner mark is the
        gateway's own record of ``is_owner_dashboard_request`` for the request
        that dispatched the turn, and a non-owner's turn is refused on it alone,
        with every other field at the owner's-tab value.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = self._owner_state(reg, mgr=self._three_leg_mgr(), _turn_owner_sent=False)
        req = self._keyed_req(state, "dashboard:ui", crew="cd-1", **_ROUTE_QUERY[route])
        r = asyncio.run(getattr(handlers, f"api_crew_sessions_{route}")(req))
        assert r.status == 403
        assert _body(r)["code"] == "not_owner_turn"

    @pytest.mark.parametrize("route", ["search", "list", "read"])
    def test_every_audit_event_names_the_calling_session(self, tmp_path, monkeypatch, route):
        """Spec §18.3: EVERY crew-read audit event carries the caller's session key.

        The refusals are the events an audit reviewer needs most, and they are
        the ones a per-arm ``caller=`` is easiest to forget: a gate refusal, an
        unknown crew and a successful read each record the key the request
        declared in ``X-Session-Key``, so the trail says who was turned away as
        well as who read.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        events: list[tuple[str, str, str]] = []

        def _capture(operation, outcome, *, request_id="", error="", caller=""):
            events.append((outcome, error, caller))

        # Through the facade, as the mirror guard requires of a shared name.
        monkeypatch.setattr(handlers, "_audit", _capture)
        leg = getattr(handlers, f"api_crew_sessions_{route}")
        # A gate refusal: the one arm the lane found silent.
        state = self._owner_state(reg, mgr=self._three_leg_mgr(), _turn_owner_sent=False)
        r = asyncio.run(
            leg(self._keyed_req(state, "dashboard:ui", crew="cd-1", **_ROUTE_QUERY[route]))
        )
        assert r.status == 403
        # A refusal past the gate (unknown crew), then a successful read.
        state = self._owner_state(reg, mgr=self._three_leg_mgr())
        r = asyncio.run(
            leg(self._keyed_req(state, "dashboard:ui", crew="nope", **_ROUTE_QUERY[route]))
        )
        assert r.status == 404
        r = asyncio.run(
            leg(self._keyed_req(state, "dashboard:ui", crew="cd-1", **_ROUTE_QUERY[route]))
        )
        assert r.status == 200
        assert [e[0] for e in events] == ["denied", "denied", "success"]
        assert [e[1] for e in events][:2] == [
            "the running turn was not sent by the owner",
            "crew_unknown",
        ]
        assert all(e[2] == "dashboard:ui" for e in events), events

    @pytest.mark.parametrize(
        "witness",
        [
            {"_created_by": "cron:nightly"},
            {"_created_by": "dashboard:owner-tab"},
            {"_created_by": "slack:1712345.678"},
            {"_lineage_minted": True},
        ],
    )
    def test_read_refuses_a_slot_opened_on_another_callers_behalf(
        self, tmp_path, monkeypatch, witness
    ):
        """A worker ``session_create`` or a firing cron opened is not the person's tab.

        Refused on either spelling of the creator: the in-process mint witness
        (``_lineage_minted``) or the attribution restored after a restart
        (``_created_by``), and whoever the creator was -- a cron, the owner's own
        tab, a channel thread. Even a worker the OWNER's tab opened is refused: the
        person is in the tab that opened it, not in the worker, and a child that
        needs a peer's history asks its parent. The turn here is user-sent on
        purpose, because a cron's ``send_to_session`` into its own worker reaches
        ``chat_send`` as a non-app sender and is stamped user-sent there; the
        creator arm is what refuses the ordinary worker.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = self._owner_state(reg, slot="worker", **witness)
        r = self._read(handlers, self._keyed_req(state, "dashboard:worker", crew="cd-1", key="k1"))
        assert r.status == 403
        assert _body(r)["code"] == "owner_only"

    def test_an_orphaned_worker_is_refused_through_the_turn_it_can_run(self, tmp_path, monkeypatch):
        """The one chain the creator arm cannot see, closed by the user-sent arm.

        A worker's agent can clear ``created_by`` on its own transcript metadata,
        and after a restart the slot then restores with NO creator and reads as an
        unattributed tab. The gate does not try to recover the lost attribution: it
        asks what turn such an orphan can run. Its cron cannot send into it
        (``cron_creator_admission`` fences a cron to slots that still carry its
        ``_created_by``; a blank one "matches no caller"), a ``session_send`` runs a
        turn that is not user-sent, and the only user-sent turn is the person
        opening the tab and typing -- at which point the person IS here. So the
        orphan is refused on ``not_user_turn`` for every turn automation can give
        it, and admitted exactly when the person is in it.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        # Restored after a restart with its attribution cleared: no creator in
        # either spelling. A session_send drives it: the turn is not user-sent.
        state = self._owner_state(reg, slot="worker", _turn_user_sent=False)
        req = self._keyed_req(state, "dashboard:worker", crew="cd-1", key="k1")
        r = self._read(handlers, req)
        assert (r.status, _body(r)["code"]) == (403, "not_user_turn")
        # The person opens the orphaned tab and types: that is the owner, admitted.
        state = self._owner_state(reg, slot="worker")
        req = self._keyed_req(state, "dashboard:worker", crew="cd-1", key="k1")
        assert self._read(handlers, req).status == 200

    def test_slot_refusal_reads_only_gateway_memory_in_a_fixed_order(self):
        """The slot predicate is pure over the two crew-specific gateway-held fields.

        Everything the shared caller resolver already establishes (a live, non-app,
        attended tab running a user-sent, non-channel turn) is NOT re-read here;
        the predicate asks only what a crew read adds. Pinned directly so the order
        holds -- the owner's own turn is asked before the slot's creator -- and so
        the refusing defaults hold: ``None`` is the admitted answer and requires
        both fields at the owner's-tab value.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        assert handlers._slot_refusal(self._tab()) is None
        # The owner mark is a positive witness: a slot double that never had the
        # field (a fake from before it existed) is refused, not admitted.
        tab = self._tab()
        del tab._turn_owner_sent
        assert handlers._slot_refusal(tab)[0] == "not_owner_turn"
        assert (
            handlers._slot_refusal(self._tab(_turn_owner_sent=False, _created_by="cron:x"))[0]
            == "not_owner_turn"
        )
        assert handlers._slot_refusal(self._tab(_created_by="cron:x"))[0] == "owner_only"
        assert handlers._slot_refusal(self._tab(_lineage_minted=True))[0] == "owner_only"
        # The resolver's arms are not this predicate's: a slot it would refuse on
        # them is admitted HERE, which is what keeps the two gates from drifting.
        assert handlers._slot_refusal(self._tab(_app="x", turn_running=False)) is None
        assert (
            handlers._slot_refusal(self._tab(_turn_channel_narrowed=True, _turn_user_sent=False))
            is None
        )
        # A slot double that carries none of the fields is refused, not admitted:
        # the predicate reads each with the refusing default.
        assert handlers._slot_refusal(object()) is not None

    # ── the dashboard subject, when a user claim is present at all ────────

    def test_read_rejects_a_non_owner_dashboard_subject(self, tmp_path, monkeypatch):
        """A `!dashboard`-minted browser subject reaching a reclassified route.

        `local_only=False` reclassifies a strict-internal path as mixed, so a
        cookie-authenticated subject can arrive here. A PRESENT user claim must
        therefore be the configured owner, not merely authenticated.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = self._owner_state(reg)
        state.owner_id = "the-owner"
        req = _FakeReq(
            state,
            query={"crew": "cd-1", "key": "k1"},
            user="slack-guest",
            headers={"X-Session-Key": "dashboard:ui"},
        )
        req._attrs["internal_auth"] = True
        req._attrs["peer_verified"] = True
        req._attrs["app"] = ""  # a dashboard-context claim, but the WRONG subject
        r = self._read(handlers, req)
        assert r.status == 403
        assert _body(r)["code"] != "unattested_caller"

    def test_read_admits_the_owner_dashboard_subject(self, tmp_path, monkeypatch):
        """The owner-subject arm is a check, not a blanket refusal of user claims."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = self._owner_state(reg)
        state.owner_id = "the-owner"
        req = _FakeReq(
            state,
            query={"crew": "cd-1", "key": "k1"},
            user="the-owner",
            headers={"X-Session-Key": "dashboard:ui"},
        )
        req._attrs["internal_auth"] = True
        req._attrs["peer_verified"] = True
        req._attrs["app"] = ""
        assert self._read(handlers, req).status == 200
