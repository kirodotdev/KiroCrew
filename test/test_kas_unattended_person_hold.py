"""Unattended permission ladders hold a KAS plan handoff and a KAS question.

Two shared ladders answer permission requests on surfaces with no chat slot:

- ``llm_helpers._resolve_permission`` (cron, autonudge, heartbeat, Meetings and
  any ``AUTO_APPROVE`` caller), whose hook auto-approve rung and caller-less
  "default: auto-approve" would otherwise answer a KAS question by picking its
  first answer;
- ``tool_permission.settle`` (subagents and the task runner), whose grants --
  the gate's own auto-approve included -- would do the same.

Each test pairs the KAS request with an ordinary one the same rung approves, so
it fails as soon as the hold is removed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import kiro_crew.slack.gateway  # noqa: F401  (binds ``sel`` before any patch; see deny_notice)
from kiro_crew import name_grant
from kiro_crew.constants import DENY_CAUSE_SURFACE_POLICY
from kiro_crew.hooks import ToolHookResult
from kiro_crew.llm_helpers import ToolApprovalPolicy, _resolve_permission
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent
from kiro_crew.tool_permission import (
    GATE_GRANT,
    Answer,
    Ask,
    Evidence,
    Hit,
    Policy,
    Refusal,
    settle,
)

_ANSWERS = [
    {"optionId": "a1", "kind": "allow_once", "name": "Use Postgres"},
    {"optionId": "a2", "kind": "allow_once", "name": "Use SQLite"},
]


def _question() -> LLMEvent:
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="Which database should the service use?",
        request_id="r1",
        tool_kind="read",
        options=list(_ANSWERS),
        kas_builtin_ids=True,
        answers_only=True,
    )


def _plan() -> LLMEvent:
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="Approve the workflow plan",
        request_id="r1",
        tool_kind="read",
        options=[{"optionId": "ok", "kind": "allow_once", "name": "Approve"}],
        harness_tool_id="update_workflow",
        kas_builtin_ids=True,
    )


def _ordinary() -> LLMEvent:
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST, title="Read README.md", request_id="r1", tool_kind="read"
    )


_KAS = {"question": _question, "plan": _plan}


class _Provider:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.supports_steer = True
        self.supports_refusal_steer = True

    async def steer(self, message: str) -> bool:
        self.calls.append("steer")
        return True

    async def approve_tool(self, request_id) -> None:
        self.calls.append("approve")

    async def reject_tool(self, request_id) -> None:
        self.calls.append("reject")


def _hooks(result: ToolHookResult) -> MagicMock:
    hooks = MagicMock()
    hooks.effective_denied_regexes = MagicMock(return_value=[])
    hooks.on_tool_call = MagicMock(return_value=result)
    return hooks


@pytest.fixture
def no_name_refusal(monkeypatch):
    async def _none(event):
        return None

    monkeypatch.setattr(name_grant, "refusal_for_event", _none)


# ── llm_helpers._resolve_permission ─────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(_KAS))
async def test_the_caller_less_default_does_not_answer_a_kas_request(kind, monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.permission_floor.refusal_for", lambda *a, **k: None, raising=False
    )
    ordinary = _Provider()
    assert await _resolve_permission(ordinary, _ordinary(), ToolApprovalPolicy.AUTO_APPROVE, None)
    assert ordinary.calls == ["approve"]

    provider = _Provider()
    approved = await _resolve_permission(
        provider, _KAS[kind](), ToolApprovalPolicy.AUTO_APPROVE, None
    )
    assert approved is False
    assert provider.calls == ["steer", "reject"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(_KAS))
async def test_a_hook_auto_approve_does_not_answer_a_kas_request(kind, no_name_refusal):
    hooks = _hooks(ToolHookResult.auto_approve())
    ordinary = _Provider()
    assert await _resolve_permission(ordinary, _ordinary(), ToolApprovalPolicy.HOOK_BASED, hooks)
    assert ordinary.calls == ["approve"]

    provider = _Provider()
    assert (
        await _resolve_permission(provider, _KAS[kind](), ToolApprovalPolicy.HOOK_BASED, hooks)
        is False
    )
    assert provider.calls == ["steer", "reject"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(_KAS))
async def test_a_kas_request_still_reaches_the_person(kind, no_name_refusal):
    hooks = _hooks(ToolHookResult.auto_approve())
    asked: list[str] = []

    async def _person(event) -> bool:
        asked.append(event.title)
        return True

    provider = _Provider()
    event = _KAS[kind]()
    assert await _resolve_permission(
        provider, event, ToolApprovalPolicy.HOOK_BASED, hooks, on_tool_approval=_person
    )
    assert asked == [event.title]
    assert provider.calls == ["approve"]


# ── tool_permission.settle ──────────────────────────────────────────────────


class _Wire:
    def __init__(self) -> None:
        self.answers: list[str] = []

    async def allow(self, ask, stopping=None) -> bool:
        self.answers.append("allow")
        return True

    async def refuse(self, ask, notice) -> None:
        self.answers.append("reject")


class _Gate:
    def judge(self, ask):
        return Hit("hook_auto_approve", Evidence.NAME, vouch=True)


class _Audit:
    def refused(self, ask, refusal) -> None:
        pass

    def approved(self, ask, settled) -> None:
        pass

    def declined(self, ask, refusal, tier) -> None:
        pass


class _Person:
    name = "callback"
    attended = True

    def __init__(self) -> None:
        self.asked = 0

    async def ask(self, ask) -> Answer:
        self.asked += 1
        return Answer(approved=True)


def _policy(**kw) -> Policy:
    kw.setdefault("gate", _Gate())
    kw.setdefault("audit", _Audit())
    kw.setdefault(
        "otherwise", Refusal.host("headless", "nothing here can answer", DENY_CAUSE_SURFACE_POLICY)
    )
    kw.setdefault("grants", (GATE_GRANT,))
    return Policy(**kw)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(_KAS))
async def test_settle_grants_do_not_answer_a_kas_request(kind, no_name_refusal):
    ordinary = _Wire()
    await settle(Ask(_ordinary(), ordinary, "k"), _policy())
    assert ordinary.answers == ["allow"]

    wire = _Wire()
    await settle(Ask(_KAS[kind](), wire, "k"), _policy())
    assert wire.answers == ["reject"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(_KAS))
async def test_settle_hands_a_kas_request_to_the_attended_person(kind, no_name_refusal):
    person = _Person()
    wire = _Wire()
    await settle(Ask(_KAS[kind](), wire, "k"), _policy(responders=(person,)))
    assert person.asked == 1
    assert wire.answers == ["allow"]
