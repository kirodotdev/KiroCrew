"""``agent.session_control`` binds a script cron on the two chat routes it writes to.

A script cron opens a dashboard session with ``POST /api/chat/slots`` and seeds
it with ``POST /api/chat``, presenting its ``cron:<job id>`` key behind the
internal secret. While the switch is off, ``private_chat_route_refusal`` refuses
that caller on those two routes with the same body the session-control routes
send. Owner and member callers keep their own gates, and every other chat route
is left alone.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.handlers import _shared

_CRON_KEY = "cron:nightly-dispatcher"
_DISABLED_BODY = {
    "error": "session control is disabled in config (agent.session_control)",
    "code": "session_control_disabled",
}


def _internal_request(method: str, path: str, session_key: str = _CRON_KEY):
    app = web.Application()
    app["state"] = SimpleNamespace(_slots={})
    req = make_mocked_request(method, path, app=app, headers={"X-Session-Key": session_key})
    req["internal_auth"] = True
    req["peer_verified"] = True
    return req


@pytest.fixture
def unscoped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rest of the gate admits the caller, so only the switch can refuse it."""

    async def _scope(_request, _operation, **_kwargs):
        return None, None

    monkeypatch.setattr(_shared, "internal_memory_scope", _scope)


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Record SEL denials instead of writing them."""
    events: list[dict] = []
    recorder = SimpleNamespace(log_api_access=lambda **kw: events.append(kw))
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: recorder)
    return events


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch):
    """Pin ``session_control_enabled`` and count how often the gate reads it."""
    reads: list[bool] = []

    def _set(enabled: bool) -> list[bool]:
        def _read() -> bool:
            reads.append(enabled)
            return enabled

        monkeypatch.setattr(sc, "session_control_enabled", _read)
        return reads

    return _set


def _body(resp: web.Response) -> dict:
    return json.loads(resp.body)


class TestTheSwitchOff:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/api/chat/slots", "/api/chat/slots/"])
    async def test_a_cron_key_is_refused_on_opening_a_session(self, unscoped, audit, switch, path):
        switch(False)
        resp = await _shared.private_chat_route_refusal(_internal_request("POST", path))

        assert resp is not None
        assert resp.status == 403
        assert _body(resp) == _DISABLED_BODY
        (event,) = audit
        assert event["operation"] == "chat.control"
        assert event["outcome"] == "denied"
        assert event["error"] == "session_control_disabled"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/api/chat", "/api/chat?ws=1"])
    async def test_a_cron_key_is_refused_on_seeding_a_session(self, unscoped, audit, switch, path):
        switch(False)
        resp = await _shared.private_chat_route_refusal(_internal_request("POST", path))

        assert resp is not None
        assert resp.status == 403
        assert _body(resp) == _DISABLED_BODY

    @pytest.mark.asyncio
    async def test_a_cron_key_reading_the_session_list_is_not_refused_by_the_switch(
        self, unscoped, audit, switch
    ):
        reads = switch(False)

        assert (
            await _shared.private_chat_route_refusal(_internal_request("GET", "/api/chat/slots"))
            is None
        )
        assert reads == []
        assert audit == []

    @pytest.mark.asyncio
    async def test_a_dashboard_key_is_not_refused_by_the_switch(self, unscoped, audit, switch):
        reads = switch(False)
        req = _internal_request("POST", "/api/chat/slots", session_key="dashboard:chat-1")

        assert await _shared.private_chat_route_refusal(req) is None
        assert reads == []
        assert audit == []


class TestTheSwitchOn:
    @pytest.mark.asyncio
    async def test_a_cron_key_opening_a_session_is_not_refused_by_the_switch(
        self, unscoped, audit, switch
    ):
        reads = switch(True)

        assert (
            await _shared.private_chat_route_refusal(_internal_request("POST", "/api/chat/slots"))
            is None
        )
        assert reads == [True]
        assert audit == []


class TestCronSlotCreator:
    """Only a ``cron:`` key the transport attests names the creator of a slot."""

    @staticmethod
    def _scope_is(monkeypatch: pytest.MonkeyPatch, scope: _shared.MemberScope) -> None:
        async def _resolved(_request):
            return scope

        monkeypatch.setattr(_shared, "member_request_scope", _resolved)

    @pytest.mark.asyncio
    async def test_an_attested_cron_key_is_the_creator(self, monkeypatch):
        self._scope_is(monkeypatch, _shared.MemberScope(_CRON_KEY, True, None))
        req = _internal_request("POST", "/api/chat/slots")

        assert await _shared.cron_slot_creator(req) == _CRON_KEY

    @pytest.mark.asyncio
    async def test_an_unverified_cron_key_is_no_creator(self, monkeypatch):
        self._scope_is(monkeypatch, _shared.MemberScope(_CRON_KEY, False, None))
        req = _internal_request("POST", "/api/chat/slots")

        assert await _shared.cron_slot_creator(req) == ""

    @pytest.mark.asyncio
    async def test_a_dashboard_key_is_no_creator(self, monkeypatch):
        self._scope_is(monkeypatch, _shared.MemberScope("dashboard:chat-1", True, None))
        req = _internal_request("POST", "/api/chat/slots", session_key="dashboard:chat-1")

        assert await _shared.cron_slot_creator(req) == ""

    @pytest.mark.asyncio
    async def test_a_caller_without_the_internal_secret_is_never_resolved(self, monkeypatch):
        async def _must_not_resolve(_request):
            raise AssertionError("member_request_scope read for a non-internal caller")

        monkeypatch.setattr(_shared, "member_request_scope", _must_not_resolve)
        req = _internal_request("POST", "/api/chat/slots")
        del req["internal_auth"]

        assert await _shared.cron_slot_creator(req) == ""


class TestRequestSlotOriginForACron:
    """The cron attribution decides the origin tag; ``_app`` still carries the owner."""

    def test_a_cron_creator_makes_a_dashboard_request_cron(self):
        from kiro_crew.dashboard.state import SlotOrigin, request_slot_origin

        assert request_slot_origin("", cron_creator=_CRON_KEY) == SlotOrigin.CRON

    def test_a_cron_creator_wins_over_the_app_token(self):
        from kiro_crew.dashboard.state import SlotOrigin, request_slot_origin

        assert request_slot_origin("some-app", cron_creator=_CRON_KEY) == SlotOrigin.CRON


class _FakeConversationLog:
    """The two ``ConversationLog`` reads the creator fence makes, recorded."""

    def __init__(self, metas: dict[str, dict] | None = None, *, unreadable: bool = False) -> None:
        self.metas = metas or {}
        self.unreadable = unreadable
        self.has_log_calls: list[str] = []
        self.metadata_calls: list[str] = []

    def has_log(self, key: str) -> bool:
        self.has_log_calls.append(key)
        return key in self.metas

    def get_metadata(self, key: str) -> dict:
        self.metadata_calls.append(key)
        if self.unreadable:
            raise OSError("metadata line unreadable")
        return self.metas[key]


class _UntouchableState:
    """A state any read of fails, so a test can prove the fence never looked."""

    def __getattr__(self, name: str):
        raise AssertionError(f"state.{name} read")


class TestCronCreatorRefusal:
    """A ``cron:`` caller reaches only the slots it created, live or persisted."""

    _SLOT = "nightly-triage"

    @staticmethod
    def _transcript_key(slot_key: str) -> str:
        from kiro_crew.dashboard.chat_utils import slot_transcript_key

        return slot_transcript_key(slot_key)

    @staticmethod
    def _live(created_by: str) -> SimpleNamespace:
        return SimpleNamespace(_created_by=created_by)

    @staticmethod
    def _state(slots: dict | None = None, log: object = None) -> SimpleNamespace:
        return SimpleNamespace(_slots=dict(slots or {}), conversation_log=log)

    @staticmethod
    async def _refusal(state, slot_name, cron_creator=_CRON_KEY):
        req = _internal_request("POST", "/api/chat")
        return await _shared.cron_creator_refusal(req, state, slot_name, cron_creator)

    @staticmethod
    def _assert_not_creator(resp: web.Response | None) -> None:
        # Session control names a cron caller by its ``cron-<job id>`` slot key.
        cron_caller = sc.CRON_SLOT_PREFIX + _CRON_KEY.removeprefix("cron:")
        assert resp is not None
        assert resp.status == 403
        assert _body(resp) == {
            "error": sc._not_creator_reason(None, cron_caller, None, None),
            "code": "not_creator",
        }

    @pytest.mark.asyncio
    async def test_a_live_slot_the_cron_created_is_admitted(self, audit):
        state = self._state({self._SLOT: self._live(_CRON_KEY)})

        assert await self._refusal(state, self._SLOT) is None
        assert audit == []

    @pytest.mark.asyncio
    async def test_a_live_owner_slot_is_refused_and_audited(self, audit):
        state = self._state({self._SLOT: self._live("")})

        resp = await self._refusal(state, self._SLOT)

        self._assert_not_creator(resp)
        (event,) = audit
        assert event["operation"] == "chat.control"
        assert event["outcome"] == "denied"
        assert event["source"] == "session_control"
        assert event["error"] == "not_creator"
        assert event["resources"] == f"/api/chat slot={self._SLOT}"

    @pytest.mark.asyncio
    async def test_a_live_slot_another_cron_created_is_refused(self, audit):
        state = self._state({self._SLOT: self._live("cron:other-job")})

        self._assert_not_creator(await self._refusal(state, self._SLOT))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("slot_name", "cron_creator"),
        [(None, _CRON_KEY), ("", _CRON_KEY), ("nightly-triage", "")],
    )
    async def test_no_slot_name_or_no_cron_creator_reads_nothing(
        self, audit, slot_name, cron_creator
    ):
        assert await self._refusal(_UntouchableState(), slot_name, cron_creator) is None
        assert audit == []

    @pytest.mark.asyncio
    async def test_a_key_with_no_live_slot_and_no_transcript_is_admitted(self, audit):
        log = _FakeConversationLog()

        assert await self._refusal(self._state(log=log), self._SLOT) is None
        assert log.has_log_calls == [self._transcript_key(self._SLOT)]
        assert log.metadata_calls == []
        assert audit == []

    @pytest.mark.asyncio
    async def test_a_persisted_slot_the_cron_created_is_admitted(self, audit):
        log = _FakeConversationLog({self._transcript_key(self._SLOT): {"created_by": _CRON_KEY}})

        assert await self._refusal(self._state(log=log), self._SLOT) is None
        assert audit == []

    @pytest.mark.asyncio
    async def test_a_persisted_slot_with_no_creator_is_refused(self, audit):
        log = _FakeConversationLog({self._transcript_key(self._SLOT): {"title": "Owner tab"}})

        self._assert_not_creator(await self._refusal(self._state(log=log), self._SLOT))
        (event,) = audit
        assert event["error"] == "not_creator"

    @pytest.mark.asyncio
    async def test_an_unreadable_persisted_slot_is_refused(self, audit):
        log = _FakeConversationLog({self._transcript_key(self._SLOT): {}}, unreadable=True)

        self._assert_not_creator(await self._refusal(self._state(log=log), self._SLOT))

    @pytest.mark.asyncio
    async def test_an_owner_slot_opened_during_the_transcript_read_is_refused(self, audit):
        state = self._state()

        class _OwnerOpensMeanwhile(_FakeConversationLog):
            def has_log(self, key: str) -> bool:
                state._slots[TestCronCreatorRefusal._SLOT] = SimpleNamespace(_created_by="")
                return super().has_log(key)

        state.conversation_log = _OwnerOpensMeanwhile()

        self._assert_not_creator(await self._refusal(state, self._SLOT))
