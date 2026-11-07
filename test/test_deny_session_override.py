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
)
from dashboard_approval_helpers import _make_slot as _make_helper_slot
from dashboard_approval_helpers import (
    _make_state,
    _patch_stats,
    _set_stream,
)

from kiro_crew.dashboard import session_control, session_deliver
from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.dashboard.deny_override import (
    DENY_OVERRIDE_META_KEY,
    deny_key_family,
    override_decision,
    override_offer_key,
    slot_deny_waivers,
    slot_may_override,
)
from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig, ToolHookResult
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.security import is_denied
from kiro_crew.security.denied_rules import DENY_REASON_PREFIX
from kiro_crew.security.deny_waiver import deny_key_is_self_guard, deny_reason_key
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

    @pytest.mark.parametrize(
        "push",
        [
            "git " + "push origin $(echo feature-x)",
            # The ungated branch reads no target, so a waiver here would also have
            # let these protected-branch pushes through.
            "git " + "push --force origin main $(true)",
            "git " + 'push origin main "${X:-}"',
        ],
    )
    def test_unverifiable_target_key_is_never_waived(self, audit_ok, push):
        key = deny_reason_key(is_denied(push) or "")
        assert key is not None and deny_key_is_self_guard(key)
        assert deny_reason_key(is_denied(push, waived=frozenset({key})) or "") == key
        assert audit_ok == []

    def test_waived_publish_refusal_records_no_feature_branch_push(self, audit_ok, monkeypatch):
        push = "git " + "push origin main"
        # A push a person waived is not a feature-branch push: its only audit is the
        # override's own deny_exception, never a BRANCH_GATE push_allowed.
        pushes: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.security.deny_gate._schedule_push_allow_audit", pushes.append
        )
        key = deny_reason_key(is_denied(push) or "")
        assert is_denied(push, waived=frozenset({key})) is None
        assert pushes == []
        assert audit_ok == [(key, "session_override")]

    def test_feature_branch_push_still_records_push_allowed(self, audit_ok, monkeypatch):
        pushes: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.security.deny_gate._schedule_push_allow_audit", pushes.append
        )
        feature = "git " + "push origin feature-x"
        assert is_denied(feature) is None
        assert pushes == [feature]
        assert audit_ok == []

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

    def test_gate_drops_a_pinned_git_publish_tag(self, restore_context, audit_ok):
        from kiro_crew.security.denied_rules import _GIT_PUBLISH_FLOOR_BY_ID

        key = "git-publish-push-protected-branch-name"
        _pin_ceiling([_GIT_PUBLISH_FLOOR_BY_ID[key]])
        push = "git " + "push origin main"
        result = HookManager(HooksConfig()).on_tool_call(
            push, command=push, is_shell=True, deny_waivers=frozenset({key})
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


# ── (d): rules that guard the agent's own controls are never overridden ──

#: Commands each refused by a self-guard rule in the deny-rules tier: the
#: credential-mint floor (with and without a lifetime), its interpreter-argv twin,
#: a self-protection catalog rule, a credential-exfil catalog rule whose card family
#: is file upload (held by its category), and the ungated subcommand floors.
_SELF_GUARD_COMMANDS = [
    "kirocrew " + "token",
    "kirocrew " + "token --ttl 30d",
    "python3 -c \"import subprocess; subprocess.run(['kirocrew', 'tok' + 'en'])\"",
    "pkill -f kirocrew",
    "aws s3 cp ./build.zip s3://bucket/",
    "kirocrew restart",
    "kirocrew update",
    "kirocrew gateway restart",
    "kirocrew cloud destroy",
    "kirocrew skills authority-retire",
]


class TestSelfGuardNeverOverridden:
    @pytest.mark.parametrize("command", _SELF_GUARD_COMMANDS)
    def test_is_denied_ignores_the_waiver(self, audit_ok, command):
        key = deny_reason_key(is_denied(command) or "")
        assert key is not None and deny_key_is_self_guard(key)
        assert deny_reason_key(is_denied(command, waived=frozenset({key})) or "") == key
        assert audit_ok == []

    @pytest.mark.parametrize("command", _SELF_GUARD_COMMANDS)
    def test_the_gate_ignores_the_waiver(self, audit_ok, command):
        key = deny_reason_key(is_denied(command) or "")
        result = HookManager(HooksConfig()).on_tool_call(
            command, command=command, is_shell=True, deny_waivers=frozenset({key})
        )
        assert (result.action, result.tier) == (TOOL_DENY, "deny-rules")
        assert deny_reason_key(result.reason or "") == key
        assert audit_ok == []

    def test_every_ungated_floor_is_a_self_guard(self):
        from kiro_crew.security.denied_rules_tables import (
            _SELF_PROTECTION_FLOOR_BY_ID,
            _SELF_PROTECTION_UNGATED_FLOOR_IDS,
        )

        keys = [*_SELF_PROTECTION_UNGATED_FLOOR_IDS, *_SELF_PROTECTION_FLOOR_BY_ID.values()]
        assert [k for k in keys if not deny_key_is_self_guard(k)] == []

    def test_every_self_protection_and_credential_rule_is_a_self_guard(self):
        from kiro_crew.security import denied_rules

        rules = [
            r
            for r in denied_rules.BUILTIN_DENIED_RULES
            if r.category in ("self-protection", "credential-exfil")
        ]
        assert rules
        assert [r.id for r in rules if not deny_key_is_self_guard(r.pattern)] == []
        assert [r.id for r in rules if not deny_key_is_self_guard(r.id)] == []

    @pytest.mark.parametrize(
        "key", [_RM_ROOT, _CHMOD_777, "git-publish-push-protected-branch-name"]
    )
    def test_an_ordinary_rule_is_not_a_self_guard(self, key):
        assert not deny_key_is_self_guard(key)

    @pytest.mark.parametrize(
        ("command", "offered"),
        [*[(c, False) for c in _SELF_GUARD_COMMANDS], ("rm -rf /", True)],
    )
    def test_the_card_is_offered_only_for_an_ordinary_rule(self, monkeypatch, command, offered):
        _no_pins(monkeypatch)
        monkeypatch.setattr("kiro_crew.dashboard.deny_override._overlay_patterns", lambda: set())
        verdict = ToolHookResult.deny(is_denied(command) or "")
        verdict.tier = "deny-rules"
        event = _shell_event(tool_input=json.dumps({"command": command}))
        key = override_offer_key(verdict, _make_slot(), event)
        assert (key is not None) is offered
        if offered:
            assert key == deny_reason_key(verdict.reason)


# ── The dashboard turn ──


def _make_slot(*args, **kwargs):
    """A dashboard slot whose current turn is one the owner sent.

    A turn run through ``_run_chat`` sets ``_turn_user_sent`` and
    ``_turn_owner_sent`` from its own directive origins at the start; this only
    covers the tests that read the eligibility predicates directly, with no turn
    running.
    """
    slot = _make_helper_slot(*args, **kwargs)
    slot._turn_user_sent = True
    slot._turn_owner_sent = True
    return slot


def _shell_event(**overrides) -> LLMEvent:
    fields = dict(
        kind=EVENT_PERMISSION_REQUEST,
        title="Running: rm -rf /",
        tool_kind="execute",
        request_id="req-1",
        tool_input=json.dumps({"command": "rm -rf /"}),
        is_shell=True,
        # A backend whose transport marks sub-agent calls (kiro-cli, KAS, Claude).
        child_calls_marked=True,
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
            await finished(
                _run_chat(
                    state,
                    slot,
                    "hello",
                    _directive_user_origin=True,
                    _directive_owner_origin=True,
                ),
                "the chat turn completing",
            )
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
        # The card shows the refused command itself, so the person reads what runs.
        assert meta["deny_override_command"] == "rm -rf /"
        # The card names what the rule protects by family, a key the client translates.
        assert meta["deny_override_family"] == "wipe"
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
    async def test_pin_lookup_runs_off_the_event_loop(self, tmp_path, monkeypatch):
        # The pin lookup reads profile storage; it must never run on the loop thread.
        import threading

        seen: list[int] = []

        def _pins():
            seen.append(threading.get_ident())
            return set()

        monkeypatch.setattr("kiro_crew.security.pinned_builtin_command_ids_for_snapshot", _pins)
        state, client = _make_state(tmp_path, context_builder=_scripted_gate([_RM_ROOT]))
        slot = _make_slot()
        _set_stream(client, _stream())

        await _run_answered(state, slot, "deny_override_once")

        assert seen, "the offer never read the governance pins"
        assert threading.get_ident() not in seen

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


class TestDenyKeyFamily:
    """The family slug picks the card's plain-language line; every built-in rule has one."""

    def test_every_builtin_rule_has_a_family(self):
        from kiro_crew.security import denied_rules

        # These take the generic line on purpose: no family line fits them.
        generic = {
            "aws-destructive-ssm-get-command-invocation",
            "aws-destructive-ssm-list-command-invocations",
            "aws-destructive-ssm-send-command",
            "aws-destructive-ssm-start-session",
            "credential-exfil-export-aws-access",
            "credential-exfil-export-aws-secret",
        }
        missing = [
            r.id
            for r in denied_rules.BUILTIN_DENIED_RULES
            if r.id not in generic and (not deny_key_family(r.id) or not deny_key_family(r.pattern))
        ]
        assert missing == []
        assert all(deny_key_family(g) == "" for g in generic)
        floors = [k for k in denied_rules._SELF_PROTECTION_FLOOR_NOTES if not deny_key_family(k)]
        assert floors == []

    @pytest.mark.parametrize(
        ("key", "family"),
        [
            ("git-publish-push-protected-branch-name", "git_publish"),
            ("self-protection-restart", "self_protection"),
            ("credential-exfil-kirocrew-token", "credentials"),
            ("local-destructive-chmod-777", "permissions"),
            (_RM_ROOT, "wipe"),
            (_CHMOD_777, "permissions"),
            ("credential-exfil-s3-cp", "file_upload"),
            ("aws-destructive-s3api-put-object", "file_upload"),
            ("credential-exfil-curl-aws-secret", "credentials"),
        ],
    )
    def test_known_keys(self, key, family):
        assert deny_key_family(key) == family

    @pytest.mark.parametrize("key", ["", "my-team-rule", r"\bterraform apply\b"])
    def test_operator_or_unknown_key_has_none(self, key):
        # The client shows its generic line for an empty family.
        assert deny_key_family(key) == ""


class TestIneligibleKeepsTodaysRefusal:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "case",
        [
            "app",
            "linked",
            "slack",
            "channel",
            "cron_origin",
            "subagent",
            "native_child",
            "redacted",
            "pinned",
            "unmarked_backend",
        ],
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
        elif case == "native_child":
            event = _shell_event(native_child_call=True)
        elif case == "redacted":
            event = _shell_event(tool_input_redacted=True)
        elif case == "unmarked_backend":
            # codex, or any backend whose transport cannot tell a child's call apart.
            event = _shell_event(child_calls_marked=False)
        _set_stream(client, _stream(event))

        with _patch_stats():
            await finished(
                _run_chat(
                    state,
                    slot,
                    "hello",
                    _directive_user_origin=True,
                    _directive_owner_origin=True,
                ),
                "the chat turn completing",
            )

        assert _permission_rows(slot) == []
        client.reject_tool.assert_called_once()
        client.approve_tool.assert_not_called()
        assert any(
            m.get("role") == "tool" and _RM_ROOT in (m.get("content") or "") for m in slot.messages
        )


class TestNativeKasChild:
    """A native KAS sub-agent shares the parent's session id, so the transport marks it."""

    @staticmethod
    def _handle():
        import asyncio

        from kiro_crew.acp.session_handle import AcpSessionHandle
        from kiro_crew.acp_backends import ACP_BACKEND_KAS

        rt = MagicMock()
        rt.pid = None
        rt.acp_backend = ACP_BACKEND_KAS
        rt._remote_workspace = None
        return AcpSessionHandle("own-sid", asyncio.Queue(), rt)

    @staticmethod
    def _permission():
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(
            id=7,
            method="session/request_permission",
            params={
                "sessionId": "own-sid",
                "toolCall": {"toolCallId": "tc-1", "status": "pending", "title": "Run Command"},
                "options": [{"optionId": "accept", "name": "Allow", "kind": "allow_once"}],
                "_meta": {"kiro": {"toolId": "run_command"}},
            },
        )

    def test_the_handle_marks_a_native_child_call(self):
        from kiro_crew.acp._dispatch import scoped_tool_cache_key

        handle = self._handle()
        own = handle._build_permission_event(self._permission())
        assert own is not None and not own.native_child_call
        handle._native_child_tool_call_ids.add(scoped_tool_cache_key("own-sid", "tc-1"))
        child = handle._build_permission_event(self._permission())
        assert child is not None and child.native_child_call

    def test_the_provider_forwards_the_mark(self):
        from kiro_crew.providers.acp_turns import _to_llm_event

        assert _to_llm_event(_shell_event(native_child_call=True)).native_child_call

    @pytest.mark.parametrize(
        "params, child",
        [
            ({"sessionId": "own-sid", "toolCall": {"toolCallId": "tc-1"}}, False),
            ({"sessionId": "child-sid", "toolCall": {"toolCallId": "tc-1"}}, True),
            (
                {
                    "sessionId": "own-sid",
                    "toolCall": {
                        "toolCallId": "tc-1",
                        "_meta": {"claudeCode": {"parentToolUseId": "toolu_1"}},
                    },
                },
                True,
            ),
        ],
    )
    def test_the_direct_client_marks_a_child_frame(self, params, child):
        from kiro_crew.acp.client_permissions import _is_child_permission_frame
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(id=1, method="session/request_permission", params=params)
        assert _is_child_permission_frame(msg, "own-sid") is child

    @pytest.mark.asyncio
    async def test_a_call_a_subagent_activity_named_gets_no_override(self, tmp_path, monkeypatch):
        # Any transport: the dashboard's own record of a child's tool call keeps
        # both the parent's session waiver and the card away from it.
        from kiro_crew.acp.types import EVENT_SUBAGENT_ACTIVITY

        _no_pins(monkeypatch)
        state, client = _make_state(tmp_path, context_builder=_scripted_gate([_RM_ROOT]))
        slot = _make_slot()
        slot._deny_waivers = {_RM_ROOT}
        activity = LLMEvent(
            kind=EVENT_SUBAGENT_ACTIVITY, sub_session_id="toolu_1", tool_call_id="tc-child"
        )
        _set_stream(client, [activity, *_stream(_shell_event(tool_call_id="tc-child"))])

        with _patch_stats():
            await finished(
                _run_chat(
                    state,
                    slot,
                    "hello",
                    _directive_user_origin=True,
                    _directive_owner_origin=True,
                ),
                "the chat turn completing",
            )

        assert _permission_rows(slot) == []
        client.approve_tool.assert_not_called()
        client.reject_tool.assert_called_once()

    def test_a_native_child_gets_no_session_waiver(self):
        slot = _make_slot()
        slot._deny_waivers = {_RM_ROOT}
        assert slot_deny_waivers(slot, _shell_event()) == frozenset({_RM_ROOT})
        assert slot_deny_waivers(slot, _shell_event(native_child_call=True)) == frozenset()


# ── Backends: only those whose sub-agent calls are told apart ──


class TestBackendAllowlist:
    def test_lists_name_only_proven_backends(self):
        from kiro_crew.agent_sdk.backends import (
            ACP_BACKEND_CLAUDE,
            ACP_BACKEND_CODEX,
            ACP_BACKEND_KAS,
            ACP_BACKEND_KIRO,
            ACP_BACKENDS_DIRECT_CLIENT_CHILD_CALLS_MARKED,
            ACP_BACKENDS_SHARED_RUNTIME_CHILD_CALLS_MARKED,
        )
        from kiro_crew.security.deny_waiver import child_calls_marked

        direct = ACP_BACKENDS_DIRECT_CLIENT_CHILD_CALLS_MARKED
        shared = ACP_BACKENDS_SHARED_RUNTIME_CHILD_CALLS_MARKED
        assert direct == {ACP_BACKEND_KIRO, ACP_BACKEND_CLAUDE}
        assert shared == {ACP_BACKEND_KIRO, ACP_BACKEND_KAS}
        for verified in (direct, shared):
            assert not child_calls_marked(ACP_BACKEND_CODEX, verified)
            # Unknown and unreadable backends fail closed.
            assert not child_calls_marked("some-new-harness", verified)
            assert not child_calls_marked(None, verified)
            assert not child_calls_marked(MagicMock(), verified)

    @pytest.mark.parametrize(
        "backend, marked", [("", True), ("kas", True), ("codex", False), ("claude", False)]
    )
    def test_the_shared_runtime_stamps_the_mark(self, backend, marked):
        handle = TestNativeKasChild._handle()
        handle._runtime.acp_backend = backend
        event = handle._build_permission_event(TestNativeKasChild._permission())
        assert event is not None and event.child_calls_marked is marked

    def test_an_unset_runtime_backend_is_not_marked(self):
        handle = TestNativeKasChild._handle()
        handle._runtime.acp_backend = MagicMock()
        event = handle._build_permission_event(TestNativeKasChild._permission())
        assert event is not None and event.child_calls_marked is False

    @pytest.mark.parametrize(
        "backend, marked", [("", True), ("claude", True), ("codex", False), ("kas", False)]
    )
    def test_the_direct_client_stamps_the_mark(self, tmp_path, backend, marked):
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path, acp_backend=backend)
        msg = JsonRpcMessage(
            id="req-1",
            method="session/request_permission",
            params={
                "toolCall": {"title": "rm -rf /", "toolCallId": "tc-1", "kind": "execute"},
                "options": [{"optionId": "allow_once", "kind": "allow_once", "name": "Allow"}],
            },
        )
        event = client._build_permission_event(msg)
        assert event is not None and event.child_calls_marked is marked

    def test_the_provider_forwards_the_mark(self):
        from kiro_crew.providers.acp_turns import _to_llm_event

        assert _to_llm_event(_shell_event()).child_calls_marked is True
        assert _to_llm_event(_shell_event(child_calls_marked=False)).child_calls_marked is False

    def test_an_unmarked_backend_gets_no_session_waiver(self):
        slot = _make_slot()
        slot._deny_waivers = {_RM_ROOT}
        assert slot_deny_waivers(slot, _shell_event(child_calls_marked=False)) == frozenset()


# ── Companion SecurityOverlay patterns are never overridable ──

_OVERLAY_GLOB = "*forbidden-share*"
_OVERLAY_COMMAND = "cat /mnt/forbidden-share/x"


class _FakeOverlay:
    def extra_deny_patterns(self):
        return (_OVERLAY_GLOB,)


@pytest.fixture
def overlay_context(restore_context):
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import set_context
    from kiro_crew.platform.bootstrap import build_default_context
    from kiro_crew.platform.profile import PROFILE_STANDALONE
    from kiro_crew.platform.security_authority import PolicyAuthority

    ctx = build_default_context(KiroCrewConfig(), profile=PROFILE_STANDALONE)
    set_context(dataclasses.replace(ctx, security=PolicyAuthority(_FakeOverlay())))


class TestOverlayNeverWaived:
    def test_the_authority_drops_an_overlay_waiver(self, audit_ok):
        from kiro_crew.platform.security_authority import PolicyAuthority

        authority = PolicyAuthority(_FakeOverlay())
        reason = authority.is_denied(_OVERLAY_COMMAND, waived=frozenset({_OVERLAY_GLOB}))
        assert deny_reason_key(reason or "") == _OVERLAY_GLOB
        assert audit_ok == []

    def test_the_gate_drops_an_overlay_waiver(self, overlay_context, audit_ok):
        mgr = HookManager(HooksConfig())
        denied = mgr.on_tool_call(_OVERLAY_COMMAND, command=_OVERLAY_COMMAND, is_shell=True)
        assert denied.action == TOOL_DENY
        assert deny_reason_key(denied.reason or "") == _OVERLAY_GLOB
        still = mgr.on_tool_call(
            _OVERLAY_COMMAND,
            command=_OVERLAY_COMMAND,
            is_shell=True,
            deny_waivers=frozenset({_OVERLAY_GLOB}),
        )
        assert still.action == TOOL_DENY
        assert audit_ok == []

    def test_no_card_is_offered_for_an_overlay_rule(self, overlay_context, monkeypatch):
        from kiro_crew.dashboard.deny_override import override_offer_key

        _no_pins(monkeypatch)
        verdict = ToolHookResult.deny(f"{DENY_REASON_PREFIX}{_OVERLAY_GLOB}")
        verdict.tier = "deny-rules"
        assert override_offer_key(verdict, _make_slot(), _shell_event()) is None
        # The same refusal on a rule the overlay does not add is offered.
        other = ToolHookResult.deny(f"{DENY_REASON_PREFIX}{_RM_ROOT}")
        other.tier = "deny-rules"
        assert override_offer_key(other, _make_slot(), _shell_event()) == _RM_ROOT

    def test_an_unreadable_overlay_offers_nothing(self, monkeypatch):
        from kiro_crew.dashboard.deny_override import override_offer_key

        _no_pins(monkeypatch)

        def _boom():
            raise RuntimeError("no context")

        monkeypatch.setattr("kiro_crew.platform.context.current_context", _boom)
        verdict = ToolHookResult.deny(f"{DENY_REASON_PREFIX}{_RM_ROOT}")
        verdict.tier = "deny-rules"
        assert override_offer_key(verdict, _make_slot(), _shell_event()) is None


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
            await finished(
                _run_chat(
                    state,
                    slot_a,
                    "again",
                    _directive_user_origin=True,
                    _directive_owner_origin=True,
                ),
                "the second turn completing",
            )
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

    @pytest.mark.asyncio
    async def test_session_override_is_kept_only_when_the_rejudge_allows(
        self, tmp_path, monkeypatch
    ):
        # Another rule still refuses the call: the refusal stands and no waiver
        # is left on the slot for a later call to use.
        _no_pins(monkeypatch)
        state, client = _make_state(
            tmp_path, context_builder=_scripted_gate([_RM_ROOT, _CHMOD_777])
        )
        slot = _make_slot()
        _set_stream(client, _stream())

        await _run_answered(state, slot, "deny_override_session")

        assert slot._deny_waivers == set()
        client.approve_tool.assert_not_called()
        client.reject_tool.assert_called_once()
        assert any(_CHMOD_777 in (m.get("content") or "") for m in slot.messages)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("answer", ["deny_override_once", "deny_override_session"])
    async def test_a_command_breaking_two_overridable_rules_cannot_be_waived(
        self, tmp_path, monkeypatch, answer
    ):
        # In this version an override waives one rule for one card, and nothing is
        # kept when another rule still refuses. So the retry stops at the first
        # rule again: every card names that rule, never the second one, and the
        # command never runs.
        _no_pins(monkeypatch)
        state, client = _make_state(
            tmp_path, context_builder=_scripted_gate([_RM_ROOT, _CHMOD_777])
        )
        slot = _make_slot()
        for _attempt in range(2):
            _set_stream(client, _stream())
            await _run_answered(state, slot, answer)

        cards = [json.loads(row["cls"]) for row in _permission_rows(slot)]
        assert [card[DENY_OVERRIDE_META_KEY] for card in cards] == [_RM_ROOT, _RM_ROOT]
        assert slot._deny_waivers == set()
        client.approve_tool.assert_not_called()
        assert client.reject_tool.call_count == 2

    def test_session_override_never_reaches_a_subagent(self):
        slot = _make_slot()
        slot._deny_waivers.add(_RM_ROOT)
        assert slot_deny_waivers(slot, _shell_event()) == frozenset({_RM_ROOT})
        assert slot_deny_waivers(slot, _shell_event(sub_session_id="child")) == frozenset()

    def test_a_channel_steer_into_the_turn_gets_no_card_and_no_waiver(self):
        # A messaging-channel steer admitted into a dashboard turn narrows that
        # turn to channel authority (``_turn_channel_narrowed``), the same as a
        # turn that arrived from a channel: no card, and the session's key is
        # not handed to the gate or the floor for the rest of that turn.
        slot = _make_slot()
        slot._deny_waivers.add(_RM_ROOT)
        assert slot_may_override(slot, _shell_event()) is True
        slot._turn_channel_narrowed = True
        assert slot_may_override(slot, _shell_event()) is False
        assert slot_deny_waivers(slot, _shell_event()) == frozenset()

    def test_a_turn_the_person_did_not_send_gets_no_card_and_no_waiver(self):
        slot = _make_slot()
        slot._deny_waivers.add(_RM_ROOT)
        slot._turn_user_sent = False
        slot._turn_owner_sent = False
        assert slot_may_override(slot, _shell_event()) is False
        assert slot_deny_waivers(slot, _shell_event()) == frozenset()

    def test_a_turn_another_dashboard_user_sent_gets_no_card_and_no_waiver(self):
        # A collaborator or a channel user holding a dashboard link sends a turn a
        # person sent (``_turn_user_sent``) that the owner did not: only the owner
        # can grant an override, so only the owner's turn may use one.
        slot = _make_slot()
        slot._deny_waivers.add(_RM_ROOT)
        slot._turn_user_sent = True
        slot._turn_owner_sent = False
        assert slot_may_override(slot, _shell_event()) is False
        assert slot_deny_waivers(slot, _shell_event()) == frozenset()

    @pytest.mark.asyncio
    async def test_a_non_owner_turn_does_not_reuse_the_owners_session_waiver(
        self, tmp_path, monkeypatch
    ):
        _no_pins(monkeypatch)
        cb = _scripted_gate([_RM_ROOT])
        state, client = _make_state(tmp_path, context_builder=cb)
        state.enable_yolo()
        slot = _make_slot()
        slot._deny_waivers.add(_RM_ROOT)
        _set_stream(client, _stream())

        with _patch_stats():
            await finished(
                _run_chat(state, slot, "run the cleanup", _directive_user_origin=True),
                "the non-owner's turn completing",
            )

        # The turn's flags are cleared when it ends; the gate call records what
        # the turn ran with.
        client.approve_tool.assert_not_called()
        client.reject_tool.assert_called_once()
        assert _permission_rows(slot) == []
        assert "deny_waivers" not in cb.hooks.on_tool_call.call_args.kwargs

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "origin",
        [session_deliver.cron("nightly report"), session_deliver.app("Some App")],
        ids=["cron_delivery", "app_injection"],
    )
    async def test_an_injected_turn_does_not_reuse_the_session_waiver(
        self, tmp_path, monkeypatch, origin
    ):
        # The person allowed the rule for this session earlier; a cron delivery or
        # an app message then starts a turn in the same slot whose agent asks for
        # a command the same rule blocks. That turn was not sent by the person, so
        # the gate and the floor get no waiver and no card is offered.
        _no_pins(monkeypatch)
        cb = _scripted_gate([_RM_ROOT])
        state, client = _make_state(tmp_path, context_builder=cb)
        slot = state.get_or_create_slot("chat-1-injected")
        slot._deny_waivers.add(_RM_ROOT)
        _set_stream(client, _stream())

        with _patch_stats():
            # As the real producers deliver: an app message carries the containment
            # snapshot its handler took (``handlers/mcp_apps.py``), a cron one none.
            admission = (
                session_control.containment_meta(state, slot)
                if origin.kind == session_deliver.ORIGIN_APP
                else None
            )
            outcome = await session_deliver.deliver(
                state, slot, "run the cleanup", origin, admission=admission
            )
            assert outcome == session_deliver.DELIVERED_TURN
            await await_until(slot.task, what="the injected turn completing")

        assert slot._turn_user_sent is False
        client.approve_tool.assert_not_called()
        assert _permission_rows(slot) == []
        assert "deny_waivers" not in cb.hooks.on_tool_call.call_args.kwargs


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

    def test_a_trust_sweep_leaves_an_override_card_open(self, monkeypatch):
        # Pinned on its own: ``override_decision`` would also read the sweep's
        # "approved" as a refusal, which would close the card as a rejection.
        import asyncio

        from kiro_crew.dashboard import chat_handlers

        # The owner's functions run on the facade's globals, so its ``sel`` is patched.
        monkeypatch.setattr(chat_handlers, "sel", lambda: MagicMock())

        async def _run():
            slot = _make_slot()
            loop = asyncio.get_running_loop()
            futures = {}
            for rid, meta in (
                ("req-override", {DENY_OVERRIDE_META_KEY: _RM_ROOT}),
                ("req-plain", {}),
            ):
                meta["request_id"] = rid
                slot.messages.append(
                    {"role": "permission", "content": "x", "cls": json.dumps(meta)}
                )
                futures[rid] = slot._approval_futures[rid] = loop.create_future()
            chat_handlers._sweep_pending_approvals(MagicMock(), slot, "trust", lambda c: c)
            return {rid: fut.done() for rid, fut in futures.items()}

        assert asyncio.run(_run()) == {"req-override": False, "req-plain": True}


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
        # Request ids recur, so the frame names the card row it settled and
        # every window stamps that row, not the oldest one with the id.
        frames = [
            c.args[1]
            for c in route_state.broadcast_ws.call_args_list
            if c.args[0] == "approval_resolved"
        ]
        assert frames == [
            {
                "id": "rid-1",
                "approved": True,
                "slot": slot.key,
                "mid": body["request_mid"],
                "origin": "native",
            }
        ]

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


def _title_gate(key: str, needle: str) -> MagicMock:
    """A context builder whose gate refuses on *key* only a call whose title holds *needle*."""

    def _judge(title, **kw):
        if needle in title and key not in kw.get("deny_waivers", frozenset()):
            verdict = ToolHookResult.deny(f"{DENY_REASON_PREFIX}{key}")
            verdict.tier = "deny-rules"
            return verdict
        return ToolHookResult.allow()

    cb = MagicMock()
    cb.hooks.on_tool_call.side_effect = _judge
    cb.build_message.return_value = ("hello", None)
    return cb


class TestBatchCascadeKeepsTheRuleRefusal:
    """A deny-rules refusal cut short by an earlier rejection in its batch."""

    @pytest.mark.asyncio
    async def test_the_cascaded_call_reads_the_rule_not_user_denied(self, tmp_path, monkeypatch):
        import asyncio

        _no_pins(monkeypatch)
        state, client = _make_state(tmp_path, context_builder=_title_gate(_RM_ROOT, "rm -rf"))
        slot = _make_slot()
        first = _shell_event(
            title="Running: ls",
            request_id="req-0",
            tool_input=json.dumps({"command": "ls"}),
        )
        _set_stream(
            client,
            [
                first,
                _shell_event(request_id="req-1"),
                LLMEvent(kind=EVENT_TEXT_CHUNK, text="Done."),
                _complete_event(),
            ],
        )
        task = asyncio.create_task(_answer_approval(slot, "req-0", "rejected"))
        try:
            with _patch_stats():
                await finished(
                    _run_chat(
                        state,
                        slot,
                        "hello",
                        _directive_user_origin=True,
                        _directive_owner_origin=True,
                    ),
                    "the chat turn completing",
                )
            await await_until(task, what="the first card being answered")
        finally:
            await _drain(task)

        client.approve_tool.assert_not_called()
        rejected = [c.args[0] for c in client.reject_tool.call_args_list]
        assert rejected == ["req-0", "req-1"]
        rows = [m.get("content") or "" for m in slot.messages if m.get("role") == "tool"]
        cascaded = [r for r in rows if "rm -rf" in r]
        assert cascaded, rows
        assert all(_RM_ROOT in r and "(rejected)" not in r for r in cascaded), cascaded
        # No override card is shown for the cascaded call.
        assert not [
            m
            for m in _permission_rows(slot)
            if json.loads(m["cls"]).get("request_id") == "req-1"
            and DENY_OVERRIDE_META_KEY in json.loads(m["cls"])
        ]
