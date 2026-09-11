"""Tests for `session_key` on `POST /api/crons`.

A cron job delivers its result into the session that owns it; with no owner it
gets its own `cron-<id>` tab. `adopt_job` is the only thing that binds a job to a
chat session, and its only caller is the CLI, so a job created over HTTP cannot
deliver into an existing conversation.

This is an OWNERSHIP surface rather than a parity one — `cron_add` does not take
a session key from its caller either, it derives one — so the tests here pin the
gate as much as the plumbing: who may name a target, which targets are namable,
and that a request naming none is unaffected.

Mirrors test_dashboard_cron_folder_id.py's create-path structure.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from body_stream_helpers import attach_body

import kiro_crew.dashboard.handlers.cron as cron_handlers
from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers import api_crons, api_crons_create
from kiro_crew.validation import MAX_SHORT_STRING

_SLOT = "chat-3-1712793600"


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    yield


@pytest.fixture
def owner_gate(monkeypatch):
    """Patch the owner gate and hand the test its mock.

    Returned so a test can assert the gate was *not* consulted: "an existing
    session-less create is unaffected" is a claim about the gate never running,
    which only the call count can show.
    """
    gate = AsyncMock(return_value=None)
    monkeypatch.setattr(cron_handlers, "require_owner_dashboard_request", gate)
    return gate


def _create_request(body: dict, crons: CronService, slots: tuple[str, ...] = (_SLOT,)):
    state = MagicMock()
    state.crons = crons
    state.has_slot = MagicMock(side_effect=lambda name: name in slots)
    # A plain dashboard slot carries no link (`linked_session_key: str = ""`). Set
    # explicitly because a bare MagicMock attribute is truthy, which would read as
    # "linked to a foreign conversation" on every test.
    state.get_slot = MagicMock(return_value=MagicMock(linked_session_key=""))
    request = MagicMock()
    request.app = {"state": state}
    attach_body(request, body)
    return request


def _base(**extra) -> dict:
    return {"name": "reminder", "message": "ping", "every": 3600, **extra}


def _body(resp) -> dict:
    return json.loads(resp.body)


class TestTargetAccepted:
    """A named dashboard slot becomes the job's owning session."""

    @pytest.mark.asyncio
    async def test_bare_slot_name_is_namespaced(self, owner_gate):
        crons = CronService()
        resp = await api_crons_create(_create_request(_base(session_key=_SLOT), crons))
        assert resp.status == 200
        # Stored namespaced, the inverse of the `removeprefix("dashboard:")` the
        # delivery path applies — a bare name would resolve to no slot there.
        assert crons.list_jobs()[0].session_key == f"dashboard:{_SLOT}"

    @pytest.mark.asyncio
    async def test_already_namespaced_key_passes_through(self, owner_gate):
        crons = CronService()
        resp = await api_crons_create(
            _create_request(_base(session_key=f"dashboard:{_SLOT}"), crons)
        )
        assert resp.status == 200
        assert crons.list_jobs()[0].session_key == f"dashboard:{_SLOT}"

    @pytest.mark.asyncio
    async def test_target_round_trips_through_the_list_payload(self, owner_gate):
        """Create then list: the owner has to be readable, or the Schedule page
        cannot show which conversation a job belongs to."""
        crons = CronService()
        create = _create_request(_base(session_key=_SLOT), crons)
        assert (await api_crons_create(create)).status == 200
        state = MagicMock()
        state.crons = crons
        state.has_slot = MagicMock(return_value=False)
        list_request = MagicMock()
        list_request.app = {"state": state}
        resp = await api_crons(list_request)
        assert resp.status == 200
        assert _body(resp)["jobs"][0]["session_key"] == f"dashboard:{_SLOT}"


class TestSessionlessUnaffected:
    """The default path must not change shape."""

    @pytest.mark.asyncio
    async def test_absent_session_key_leaves_the_job_unowned(self, owner_gate):
        crons = CronService()
        resp = await api_crons_create(_create_request(_base(), crons))
        assert resp.status == 200
        assert crons.list_jobs()[0].session_key == ""

    @pytest.mark.asyncio
    async def test_absent_session_key_does_not_invoke_the_owner_gate(self, owner_gate):
        """The gate is additive: it guards naming a target, not creating a job.

        Asserted on the call count because a gate that ran for every create would
        be a behaviour change for existing callers, invisible in the response of
        a request that happens to be the owner's.
        """
        crons = CronService()
        assert (await api_crons_create(_create_request(_base(), crons))).status == 200
        owner_gate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_session_key_reads_as_absent(self, owner_gate):
        crons = CronService()
        resp = await api_crons_create(_create_request(_base(session_key=""), crons))
        assert resp.status == 200
        assert crons.list_jobs()[0].session_key == ""
        owner_gate.assert_not_awaited()


class TestOwnershipDecisionsAreAudited:
    """Both outcomes of the ownership decision reach the audit log."""

    @pytest.mark.asyncio
    async def test_approved_target_emits_an_allowed_sel_event(self, owner_gate, monkeypatch):
        """The gate records only refusals, so the ALLOW has to be emitted here.

        Without it, the approved case leaves no trace of who was granted a
        delivery target — while this module's session-recognition gate holds that
        every permission decision emits an event, allow included.
        """
        sel = MagicMock()
        monkeypatch.setattr(cron_handlers, "_sel", lambda: sel)
        crons = CronService()
        resp = await api_crons_create(_create_request(_base(session_key=_SLOT), crons))
        assert resp.status == 200
        calls = [
            c.kwargs
            for c in sel.log_api_access.call_args_list
            if c.kwargs.get("operation") == "cron.create_for_session"
        ]
        assert len(calls) == 1
        assert calls[0]["outcome"] == "allowed"
        assert calls[0]["resources"] == _SLOT

    @pytest.mark.asyncio
    async def test_a_refused_target_still_audits_the_gate_decision(self, owner_gate, monkeypatch):
        """The auditable fact is the DECISION, not whether the request succeeded.

        Every check after the owner gate answers 400, so auditing only the fully
        resolved case left an owner who cleared the gate and then named a bad
        target with no audit event at all — a successful authorization decision
        that escaped the log.
        """
        sel = MagicMock()
        monkeypatch.setattr(cron_handlers, "_sel", lambda: sel)
        crons = CronService()
        resp = await api_crons_create(_create_request(_base(session_key="chat-99-0"), crons))
        assert resp.status == 400
        allowed = [
            c.kwargs
            for c in sel.log_api_access.call_args_list
            if c.kwargs.get("operation") == "cron.create_for_session"
            and c.kwargs.get("outcome") == "allowed"
        ]
        assert len(allowed) == 1
        # The REQUESTED target, since that is what the caller was permitted to name.
        assert allowed[0]["resources"] == "chat-99-0"

    @pytest.mark.asyncio
    async def test_a_denied_caller_emits_no_allowed_event(self, monkeypatch):
        """A refusal at the gate must not read as a granted decision."""
        sel = MagicMock()
        monkeypatch.setattr(cron_handlers, "_sel", lambda: sel)
        denial = web.json_response({"error": "owner only", "code": "owner_only"}, status=403)
        monkeypatch.setattr(
            cron_handlers,
            "require_owner_dashboard_request",
            AsyncMock(return_value=denial),
        )
        crons = CronService()
        resp = await api_crons_create(_create_request(_base(session_key=_SLOT), crons))
        assert resp.status == 403
        assert not [
            c
            for c in sel.log_api_access.call_args_list
            if c.kwargs.get("operation") == "cron.create_for_session"
            and c.kwargs.get("outcome") == "allowed"
        ]
        assert crons.list_jobs() == []


class TestTargetRefused:
    """Every refusal carries a machine-readable `code` and stores nothing."""

    @pytest.mark.asyncio
    async def test_unknown_slot_is_refused(self, owner_gate):
        """A key naming no live slot would deliver to nobody.

        The CLI's `adopt` warns an operator about a typo; over HTTP there is no
        one reading a warning, so it is refused rather than stored.
        """
        crons = CronService()
        resp = await api_crons_create(_create_request(_base(session_key="chat-99-0"), crons))
        assert resp.status == 400
        assert _body(resp)["code"] == "unknown_session"
        assert crons.list_jobs() == []

    @pytest.mark.asyncio
    async def test_channel_namespace_is_refused(self, owner_gate):
        """Only a `dashboard:` key resolves to a slot the injection path reaches.

        Storing a channel key would let it own the job while delivering nothing,
        which is a promise the code does not keep.
        """
        crons = CronService()
        resp = await api_crons_create(
            _create_request(_base(session_key="slack:1788203585.923439"), crons)
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "unsupported_session_namespace"
        assert crons.list_jobs() == []

    @pytest.mark.asyncio
    async def test_slot_name_that_overflows_once_namespaced_is_refused(self, owner_gate):
        """The PREFIXED key is what the store caps, so it is what must fit.

        A bare name inside `MAX_SHORT_STRING` can still exceed it once
        `dashboard:` is prepended. The store's own validator raises `ValueError`
        there, from `_build_job` on the event loop — past this handler's
        `except CronStoreBusy/Unreadable` — so an unbudgeted prefix is a 500
        rather than a 400.
        """
        overflowing = "c" * MAX_SHORT_STRING
        crons = CronService()
        resp = await api_crons_create(
            _create_request(_base(session_key=overflowing), crons, slots=(overflowing,))
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_session_key"
        assert crons.list_jobs() == []

    @pytest.mark.asyncio
    async def test_longest_name_that_still_fits_is_accepted(self, owner_gate):
        """The boundary is the store's cap, not an arbitrary margin."""
        longest = "c" * (MAX_SHORT_STRING - len("dashboard:"))
        crons = CronService()
        resp = await api_crons_create(
            _create_request(_base(session_key=longest), crons, slots=(longest,))
        )
        assert resp.status == 200
        stored = crons.list_jobs()[0].session_key
        assert stored == f"dashboard:{longest}"
        assert len(stored) == MAX_SHORT_STRING

    @pytest.mark.asyncio
    async def test_channel_linked_slot_is_refused(self, owner_gate):
        """A tab can display a conversation whose session key is not its own.

        A channel-born chat keeps the channel's key while its dashboard tab is
        open, so `dashboard:<slot>` is not that conversation's key: `_owned_by`
        would never match it and the chat the user is looking at could not list or
        cancel its own job. Refused rather than stored under either key — only a
        `dashboard:` key resolves for injection, and only the linked key owns.
        """
        crons = CronService()
        request = _create_request(_base(session_key=_SLOT), crons)
        request.app["state"].get_slot = MagicMock(
            return_value=MagicMock(linked_session_key="slack:1788203585.923439")
        )
        resp = await api_crons_create(request)
        assert resp.status == 400
        assert _body(resp)["code"] == "linked_session_not_targetable"
        assert crons.list_jobs() == []

    @pytest.mark.asyncio
    async def test_cron_linked_slot_is_refused(self, owner_gate):
        """Same rule for a cron tab, which carries `cron:<job id>`."""
        crons = CronService()
        request = _create_request(_base(session_key=_SLOT), crons)
        request.app["state"].get_slot = MagicMock(
            return_value=MagicMock(linked_session_key="cron:abc123")
        )
        resp = await api_crons_create(request)
        assert resp.status == 400
        assert _body(resp)["code"] == "linked_session_not_targetable"

    @pytest.mark.asyncio
    async def test_self_linked_slot_is_accepted(self, owner_gate):
        """A slot linked to its own dashboard key is not a foreign conversation."""
        crons = CronService()
        request = _create_request(_base(session_key=_SLOT), crons)
        request.app["state"].get_slot = MagicMock(
            return_value=MagicMock(linked_session_key=f"dashboard:{_SLOT}")
        )
        resp = await api_crons_create(request)
        assert resp.status == 200
        assert crons.list_jobs()[0].session_key == f"dashboard:{_SLOT}"

    @pytest.mark.asyncio
    async def test_a_hidden_job_cannot_name_a_target(self, owner_gate):
        """`hide_in_chat` gates every injector call site, so nothing would arrive."""
        crons = CronService()
        body = _base(session_key=_SLOT)
        body["hide_in_chat"] = True
        resp = await api_crons_create(_create_request(body, crons))
        assert resp.status == 400
        assert _body(resp)["code"] == "hidden_job_cannot_target_session"
        assert crons.list_jobs() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [1, 0, "false", "true", "", "no", ["x"], {}])
    async def test_a_non_boolean_hide_in_chat_is_refused(self, owner_gate, bad):
        """Neither coercion nor identity can read a string honestly.

        `bool("false")` is True and `"false" is True` is False, so either spelling
        silently disagrees with somebody — one refuses a caller who asked for a
        visible job, the other admits one this handler then stores hidden. A string
        is not a boolean either way, so the value is refused rather than guessed at.
        """
        crons = CronService()
        body = _base(session_key=_SLOT)
        body["hide_in_chat"] = bad
        resp = await api_crons_create(_create_request(body, crons))
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_hide_in_chat"
        assert crons.list_jobs() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("falsy", [False, None])
    async def test_a_visible_job_still_accepts_a_target(self, owner_gate, falsy):
        """`False` and an absent field both mean visible, and both are accepted."""
        crons = CronService()
        body = _base(session_key=_SLOT)
        body["hide_in_chat"] = falsy
        resp = await api_crons_create(_create_request(body, crons))
        assert resp.status == 200
        assert crons.list_jobs()[0].session_key == f"dashboard:{_SLOT}"

    @pytest.mark.asyncio
    async def test_a_hidden_job_with_no_target_is_unaffected(self, owner_gate):
        """The refusal is scoped to the combination, not to `hide_in_chat`."""
        crons = CronService()
        body = _base()
        body["hide_in_chat"] = True
        resp = await api_crons_create(_create_request(body, crons))
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_non_string_session_key_is_refused(self, owner_gate):
        crons = CronService()
        resp = await api_crons_create(_create_request(_base(session_key=123), crons))
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_session_key"
        assert crons.list_jobs() == []

    @pytest.mark.asyncio
    async def test_non_owner_cannot_name_a_target(self, monkeypatch):
        """The gate's refusal is returned verbatim, and nothing is created."""
        from aiohttp import web

        denial = web.json_response({"error": "owner only", "code": "owner_only"}, status=403)
        monkeypatch.setattr(
            cron_handlers,
            "require_owner_dashboard_request",
            AsyncMock(return_value=denial),
        )
        crons = CronService()
        resp = await api_crons_create(_create_request(_base(session_key=_SLOT), crons))
        assert resp.status == 403
        assert _body(resp)["code"] == "owner_only"
        assert crons.list_jobs() == []
