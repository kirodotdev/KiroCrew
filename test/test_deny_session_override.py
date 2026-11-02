"""In-session human override of a command deny rule.

Design: ``docs/request-for-change/rfc-deny-session-override.md``. Three layers are
pinned here:

* ``security.is_denied(..., waived=...)``: a waived key steps aside only its own
  return site, so a second rule the command breaks still refuses, and a waiver
  whose ``deny_exception`` audit cannot be written keeps the refusal.
* The gate (``HookManager``): a key a governance ceiling pins is dropped from the
  waivers before ``is_denied`` sees it.
* The dashboard turn: a deny-rules refusal in a session a person drives gets the
  override card, in trust and YOLO too; an app-owned, channel-bound or backend
  subagent request gets today's refusal; and a session override lives on its own
  slot only.

Every case is in-process: the gate is a scripted ``on_tool_call`` (or the real
``HookManager`` on the built-in rules), SEL writes are patched out, and no file
outside ``tmp_path`` is touched.
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from dashboard_approval_helpers import (
    _answer_approval,
    _complete_event,
    _drain,
    _make_slot,
    _make_state,
    _patch_stats,
    _set_stream,
)

from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.dashboard.deny_override import (
    DENY_OVERRIDE_META_KEY,
    override_decision,
    slot_deny_waivers,
)
from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig, ToolHookResult
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.security import is_denied
from kiro_crew.security.denied_rules import DENY_REASON_PREFIX
from kiro_crew.security.deny_waiver import deny_reason_key
from kiro_crew.testing.wait import await_until, finished

_RM_ROOT = "rm -rf /.*"
_CHMOD_777 = "chmod 777.*"
_TWO_RULES = "chmod 777 ./build && rm -rf /"


@pytest.fixture
def audit_ok(monkeypatch):
    """Record every ``deny_exception`` the waiver writes, and report each written."""
    written: list[tuple[str, str]] = []

    def _emit(_tool, key, mechanism="_DENY_EXCEPTIONS"):
        written.append((key, mechanism))
        return True

    monkeypatch.setattr("kiro_crew.security.deny_gate._emit_deny_exception_event", _emit)
    return written


@pytest.fixture
def restore_context():
    from kiro_crew.platform import reset_context

    yield
    reset_context()


def _pin_ceiling(patterns):
    """Install a governance ceiling whose commands-scope deny pins *patterns*."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import governance as gov
    from kiro_crew.platform import set_context
    from kiro_crew.platform.bootstrap import build_default_context
    from kiro_crew.platform.profile import PROFILE_STANDALONE

    ceiling = gov.parse_policy(
        {"version": 1, "boot": {}, "commands": {"mode": "deny", "deny": list(patterns)}}
    )
    ctx = build_default_context(KiroCrewConfig(), profile=PROFILE_STANDALONE)
    set_context(dataclasses.replace(ctx, governance=ceiling))


# ── (a) and (b): the deny gate ──


class TestIsDeniedWaiver:
    def test_waived_key_skips_only_its_rule(self, audit_ok):
        first = deny_reason_key(is_denied(_TWO_RULES) or "")
        assert first == _RM_ROOT
        second = is_denied(_TWO_RULES, waived=frozenset({_RM_ROOT}))
        # The other rule the command breaks still refuses, under its own key.
        assert deny_reason_key(second or "") == _CHMOD_777
        assert audit_ok == [(_RM_ROOT, "session_override")]

    def test_every_key_waived_allows_and_audits_each(self, audit_ok):
        assert is_denied(_TWO_RULES, waived=frozenset({_RM_ROOT, _CHMOD_777})) is None
        assert sorted(key for key, _ in audit_ok) == sorted([_RM_ROOT, _CHMOD_777])

    def test_unrelated_waiver_changes_nothing(self, audit_ok):
        assert deny_reason_key(is_denied("rm -rf /", waived=frozenset({_CHMOD_777})) or "") == (
            _RM_ROOT
        )
        assert audit_ok == []

    def test_floor_key_is_waivable(self, audit_ok):
        push = "git " + "push origin main"
        key = deny_reason_key(is_denied(push) or "")
        assert key == "git-publish-push-protected-branch-name"
        assert is_denied(push, waived=frozenset({key})) is None

    def test_unverifiable_target_key_is_waivable(self, audit_ok):
        # The ungated branch (target fused by an expansion) reports its own key; once
        # waived, the call is judged by every resolvable rule as without it.
        unverifiable = "git " + "push origin $(echo feature-x)"
        key = deny_reason_key(is_denied(unverifiable) or "")
        assert key is not None
        assert is_denied(unverifiable, waived=frozenset({key})) is None
        assert [k for k, _ in audit_ok] == [key]

    def test_unverifiable_waiver_does_not_cover_a_protected_push(self, audit_ok):
        both = "git " + "push origin $(echo x) && git " + "push origin main"
        unverifiable_key = deny_reason_key(is_denied("git " + "push origin $(echo x)") or "")
        reason = is_denied(both, waived=frozenset({unverifiable_key}))
        assert deny_reason_key(reason or "") == "git-publish-push-protected-branch-name"

    def test_failed_audit_keeps_the_refusal(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.security.deny_gate._emit_deny_exception_event",
            lambda *_a, **_k: False,
        )
        reason = is_denied("rm -rf /", waived=frozenset({_RM_ROOT}))
        assert deny_reason_key(reason or "") == _RM_ROOT


# ── (c): governance pins ──


class TestGovernancePinnedNeverWaived:
    def test_gate_drops_pinned_key(self, restore_context, audit_ok):
        _pin_ceiling([_RM_ROOT])
        mgr = HookManager(HooksConfig())
        result = mgr.on_tool_call(
            "rm -rf /", command="rm -rf /", is_shell=True, deny_waivers=frozenset({_RM_ROOT})
        )
        assert result.action == TOOL_DENY
        assert result.tier == "deny-rules"
        assert audit_ok == []

    def test_a_git_publish_pin_pins_the_unverifiable_target_key(self, restore_context, audit_ok):
        # A hidden push destination may be the branch the pinned rule protects, so
        # pinning any git-publish rule must also keep the unverifiable key hard.
        from kiro_crew.security.denied_rules import _GIT_PUBLISH_FLOOR_BY_ID
        from kiro_crew.security.deny_waiver import deny_key_is_pinned

        protected = _GIT_PUBLISH_FLOOR_BY_ID["git-publish-push-protected-branch-name"]
        unverifiable = "git " + "push origin $(echo main)"
        key = deny_reason_key(is_denied(unverifiable) or "")
        assert deny_key_is_pinned(key, {"git-publish-push-protected-branch-name"})
        assert not deny_key_is_pinned(key, {"local-destructive-rm-rf-root"})

        _pin_ceiling([protected])
        result = HookManager(HooksConfig()).on_tool_call(
            unverifiable, command=unverifiable, is_shell=True, deny_waivers=frozenset({key})
        )
        assert result.action == TOOL_DENY
        assert audit_ok == []

    def test_gate_drops_a_profile_pinned_key(self, monkeypatch, audit_ok):
        # Pins from any loaded profile count, not only the active ceiling's.
        monkeypatch.setattr(
            "kiro_crew.security.pinned_builtin_command_ids_for_snapshot",
            lambda: {"local-destructive-rm-rf-root"},
        )
        result = HookManager(HooksConfig()).on_tool_call(
            "rm -rf /", command="rm -rf /", is_shell=True, deny_waivers=frozenset({_RM_ROOT})
        )
        assert result.action == TOOL_DENY
        assert audit_ok == []

    def test_gate_applies_unpinned_key(self, audit_ok):
        mgr = HookManager(HooksConfig())
        denied = mgr.on_tool_call("rm -rf /", command="rm -rf /", is_shell=True)
        assert denied.action == TOOL_DENY
        waived = mgr.on_tool_call(
            "rm -rf /", command="rm -rf /", is_shell=True, deny_waivers=frozenset({_RM_ROOT})
        )
        assert waived.action != TOOL_DENY


# ── The dashboard turn ──


def _shell_event(**overrides) -> LLMEvent:
    fields = dict(
        kind=EVENT_PERMISSION_REQUEST,
        title="Running: rm -rf /",
        tool_kind="execute",
        request_id="req-1",
        tool_input=json.dumps({"command": "rm -rf /"}),
        is_shell=True,
    )
    fields.update(overrides)
    return LLMEvent(**fields)


def _stream(event: LLMEvent | None = None) -> list[LLMEvent]:
    """One permission request, then a reply, so the turn ends without a retry turn."""
    return [
        event or _shell_event(),
        LLMEvent(kind=EVENT_TEXT_CHUNK, text="Done."),
        _complete_event(),
    ]


def _scripted_gate(keys: list[str]) -> MagicMock:
    """A context builder whose gate refuses on each of *keys* unless it is waived."""

    def _judge(_title, **kw):
        waived = kw.get("deny_waivers", frozenset())
        for key in keys:
            if key not in waived:
                verdict = ToolHookResult.deny(f"{DENY_REASON_PREFIX}{key}")
                verdict.tier = "deny-rules"
                return verdict
        return ToolHookResult.allow()

    cb = MagicMock()
    cb.hooks.on_tool_call.side_effect = _judge
    cb.build_message.return_value = ("hello", None)
    return cb


def _no_pins(monkeypatch, pinned=frozenset()):
    monkeypatch.setattr(
        "kiro_crew.security.pinned_builtin_command_ids_for_snapshot", lambda: set(pinned)
    )


def _permission_rows(slot):
    return [m for m in slot.messages if m.get("role") == "permission"]


async def _run_answered(state, slot, answer: str):
    task = None
    import asyncio

    task = asyncio.create_task(_answer_approval(slot, "req-1", answer))
    try:
        with _patch_stats():
            await finished(_run_chat(state, slot, "hello"), "the chat turn completing")
        await await_until(task, what="the override card being answered")
    finally:
        await _drain(task)


class TestDashboardOverrideCard:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["normal", "trust", "yolo"])
    async def test_card_shown_in_every_mode(self, tmp_path, monkeypatch, mode):
        _no_pins(monkeypatch)
        state, client = _make_state(tmp_path, context_builder=_scripted_gate([_RM_ROOT]))
        slot = _make_slot(trust=(mode == "trust"))
        if mode == "yolo":
            state.enable_yolo()
        _set_stream(client, _stream())

        await _run_answered(state, slot, "deny_override_once")

        rows = _permission_rows(slot)
        assert rows, f"{mode}: no override card was shown"
        meta = json.loads(rows[0]["cls"])
        assert meta[DENY_OVERRIDE_META_KEY] == _RM_ROOT
        assert "trust_grantable" not in meta
        client.approve_tool.assert_called_once_with("req-1", deny_waivers=frozenset({_RM_ROOT}))
        client.reject_tool.assert_not_called()
        assert slot._deny_waivers == set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("answer", ["approved", "approved_trust_reads", "rejected"])
    async def test_any_other_answer_refuses(self, tmp_path, monkeypatch, answer):
        # A trust or YOLO sweep resolves pending cards with "approved": never a grant here.
        _no_pins(monkeypatch)
        state, client = _make_state(tmp_path, context_builder=_scripted_gate([_RM_ROOT]))
        slot = _make_slot()
        _set_stream(client, _stream())

        await _run_answered(state, slot, answer)

        client.approve_tool.assert_not_called()
        client.reject_tool.assert_called_once()
        assert any(_RM_ROOT in (m.get("content") or "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_second_rule_still_refuses(self, tmp_path, monkeypatch):
        _no_pins(monkeypatch)
        cb = _scripted_gate([_RM_ROOT, _CHMOD_777])
        state, client = _make_state(tmp_path, context_builder=cb)
        slot = _make_slot()
        _set_stream(client, _stream())

        await _run_answered(state, slot, "deny_override_once")

        client.approve_tool.assert_not_called()
        client.reject_tool.assert_called_once()
        assert any(_CHMOD_777 in (m.get("content") or "") for m in slot.messages)


class TestIneligibleKeepsTodaysRefusal:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "case",
        ["app", "linked", "slack", "channel", "cron_origin", "subagent", "redacted", "pinned"],
    )
    async def test_no_card(self, tmp_path, monkeypatch, case):
        _no_pins(monkeypatch, {"local-destructive-rm-rf-root"} if case == "pinned" else ())
        state, client = _make_state(tmp_path, context_builder=_scripted_gate([_RM_ROOT]))
        slot = _make_slot()
        event = _shell_event()
        if case == "app":
            slot._app = "some-app"
        elif case == "linked":
            slot.linked_session_key = "cron:job-1"
        elif case == "slack":
            slot._slack_linked = True
        elif case == "channel":
            slot.channel_origin = True
        elif case == "cron_origin":
            slot._origin = "cron"
        elif case == "subagent":
            event = _shell_event(sub_session_id="child-1")
        elif case == "redacted":
            event = _shell_event(tool_input_redacted=True)
        _set_stream(client, _stream(event))

        with _patch_stats():
            await finished(_run_chat(state, slot, "hello"), "the chat turn completing")

        assert _permission_rows(slot) == []
        client.reject_tool.assert_called_once()
        client.approve_tool.assert_not_called()
        assert any(
            m.get("role") == "tool" and _RM_ROOT in (m.get("content") or "") for m in slot.messages
        )


# ── (f): a session override is its slot's alone ──


class TestSessionScope:
    @pytest.mark.asyncio
    async def test_session_override_stays_on_its_slot(self, tmp_path, monkeypatch):
        _no_pins(monkeypatch)
        cb = _scripted_gate([_RM_ROOT])
        state, client = _make_state(tmp_path, context_builder=cb)
        slot_a = _make_slot("chat-1-a")
        _set_stream(client, _stream())

        await _run_answered(state, slot_a, "deny_override_session")

        assert slot_a._deny_waivers == {_RM_ROOT}
        client.approve_tool.assert_called_once_with("req-1", deny_waivers=frozenset({_RM_ROOT}))

        # A second call in the same slot passes the gate with the session's key,
        # and the floor gets it too: no second card.
        client.approve_tool.reset_mock()
        state.enable_yolo()
        _set_stream(client, _stream())
        with _patch_stats():
            await finished(_run_chat(state, slot_a, "again"), "the second turn completing")
        client.approve_tool.assert_called_once_with("req-1", deny_waivers=frozenset({_RM_ROOT}))
        # The one card is the first turn's: the second call asked nobody.
        assert len(_permission_rows(slot_a)) == 1
        assert cb.hooks.on_tool_call.call_args.kwargs["deny_waivers"] == frozenset({_RM_ROOT})

        # Another slot of the same gateway holds nothing and is judged without it.
        slot_b = _make_slot("chat-1-b")
        assert slot_b._deny_waivers == set()
        assert slot_deny_waivers(slot_b, _shell_event()) == frozenset()
        cb.hooks.on_tool_call.reset_mock()
        _set_stream(client, _stream())
        await _run_answered(state, slot_b, "rejected")
        assert "deny_waivers" not in cb.hooks.on_tool_call.call_args.kwargs

    def test_session_override_never_reaches_a_subagent(self):
        slot = _make_slot()
        slot._deny_waivers.add(_RM_ROOT)
        assert slot_deny_waivers(slot, _shell_event()) == frozenset({_RM_ROOT})
        assert slot_deny_waivers(slot, _shell_event(sub_session_id="child")) == frozenset()


# ── The approve route ──


class TestAnswerRoute:
    def test_override_decisions(self):
        assert override_decision("deny_override_once") == ("approved", "once")
        assert override_decision("deny_override_session") == ("approved", "session")
        assert override_decision("approved") == ("rejected", "")
        assert override_decision("rejected_once") == ("rejected_once", "")

    @staticmethod
    def _owner(override: bool):
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            fut = loop.create_future()
        finally:
            loop.close()
        meta = {"request_id": "req-1"}
        if override:
            meta[DENY_OVERRIDE_META_KEY] = _RM_ROOT
        owner = SimpleNamespace(
            key="chat-1",
            messages=[{"role": "permission", "content": "x", "cls": json.dumps(meta)}],
        )
        return owner, fut

    @staticmethod
    def _request(user: str):
        class _Req(dict):
            app = {"state": SimpleNamespace(owner_id="")}

        req = _Req(app="", user=user)
        return req

    @pytest.mark.parametrize(
        "override, action, user, from_app, native, code",
        [
            (False, "deny_override_once", "local-app", False, True, "deny_override_not_offered"),
            (True, "approved", "local-app", False, True, "deny_override_card_answer"),
            (True, "trust", "local-app", False, True, "deny_override_card_answer"),
            (True, "deny_override_session", "invited@x", False, True, "deny_override_owner_only"),
            (True, "deny_override_once", "local-app", True, True, "deny_override_owner_only"),
            (
                True,
                "deny_override_once",
                "local-app",
                False,
                False,
                "deny_override_instance_required",
            ),
            (True, "deny_override_once", "local-app", False, True, None),
            (True, "rejected", "local-app", False, False, None),
        ],
    )
    def test_refusals(self, monkeypatch, override, action, user, from_app, native, code):
        from kiro_crew.dashboard import chat_handlers

        # The owner's functions run on the facade's globals, so its ``sel`` is patched.
        monkeypatch.setattr(chat_handlers, "sel", lambda: MagicMock())
        owner, fut = self._owner(override)
        resp = chat_handlers._deny_override_answer_refusal(
            self._request(user),
            owner,
            "req-1",
            fut,
            action,
            from_app=from_app,
            delegated=False,
            strict_native=native,
        )
        if code is None:
            assert resp is None
        else:
            assert resp is not None and json.loads(resp.body)["code"] == code

    @pytest.mark.parametrize("override, applied", [(True, False), (False, True)])
    def test_no_other_route_approves_an_override_card(self, tmp_path, override, applied):
        # resolve_on_slot backs the by-id route, an app's approval and the bulk
        # resolver: a plain approve there settles no override card.
        import asyncio

        from kiro_crew.dashboard.interaction_coordinator import ApprovalCoordinator

        async def _run():
            slot = _make_slot()
            fut = asyncio.get_running_loop().create_future()
            meta = {"request_id": "req-1"}
            if override:
                meta[DENY_OVERRIDE_META_KEY] = _RM_ROOT
            slot.messages.append({"role": "permission", "content": "x", "cls": json.dumps(meta)})
            slot._approval_futures["req-1"] = fut
            state = MagicMock()
            done = ApprovalCoordinator.resolve_on_slot(
                state,
                slot,
                "req-1",
                True,
                rejected_once=False,
                permission_marker=lambda *_a: False,
            )
            return done, fut.done()

        assert asyncio.run(_run()) == (applied, applied)


# ── The approve route, end to end ──


@pytest.fixture
def route_state(tmp_path, monkeypatch, _floor_monkeypatch):
    from chat_test_helpers import _make_state as _make_chat_state

    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_chat_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    return st


def _route_app(state):
    from aiohttp import web

    from kiro_crew.dashboard.chat_handlers import api_chat_slot_approve

    @web.middleware
    async def _dashboard_user(request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_dashboard_user])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    return app


def _override_card(state, request_id):
    import asyncio

    from kiro_crew.dashboard.state import SlotOrigin

    slot = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    fut = asyncio.get_running_loop().create_future()
    meta = {"request_id": request_id, DENY_OVERRIDE_META_KEY: _RM_ROOT}
    row = slot.append("permission", "Tool", json.dumps(meta))
    slot.register_approval(request_id, fut, row)
    return slot, fut


class TestApproveRouteEndToEnd:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("decision", ["deny_override_once", "deny_override_session"])
    async def test_an_instance_bound_override_settles_its_card(self, route_state, decision):
        from aiohttp.test_utils import TestClient, TestServer

        slot, fut = _override_card(route_state, "rid-1")
        body = {
            "action": decision,
            "request_id": "rid-1",
            "origin": "native",
            "request_mid": slot.approval_instance("rid-1"),
        }
        async with TestClient(TestServer(_route_app(route_state))) as client:
            resp = await client.post("/api/chat/slots/selected/approve", json=body)
        assert resp.status == 200
        assert fut.done() and fut.result() == decision

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "extra, code",
        [
            ({}, "deny_override_instance_required"),
            ({"origin": "native", "request_mid": "stale-mid"}, "approval_not_pending"),
        ],
    )
    async def test_an_override_without_its_live_instance_is_refused(self, route_state, extra, code):
        from aiohttp.test_utils import TestClient, TestServer

        _slot, fut = _override_card(route_state, "rid-2")
        body = {"action": "deny_override_once", "request_id": "rid-2", **extra}
        async with TestClient(TestServer(_route_app(route_state))) as client:
            resp = await client.post("/api/chat/slots/selected/approve", json=body)
            payload = await resp.json()
        assert resp.status in (400, 404)
        assert payload["code"] == code
        assert not fut.done()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("native", [True, False])
    async def test_a_non_string_action_is_refused_not_crashed(self, route_state, native):
        from aiohttp.test_utils import TestClient, TestServer

        slot, fut = _override_card(route_state, "rid-3")
        body = {"action": ["deny_override_once"], "request_id": "rid-3"}
        if native:
            body.update(origin="native", request_mid=slot.approval_instance("rid-3"))
        async with TestClient(TestServer(_route_app(route_state))) as client:
            resp = await client.post("/api/chat/slots/selected/approve", json=body)
        assert resp.status < 500
        assert not fut.done() or fut.result() not in ("deny_override_once", "approved")
