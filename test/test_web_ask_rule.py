"""An organisation's ``ask`` on web fetch / web search reaches a person (``web_ask_rule``).

In an LTS build KAS asks Crew about a ``web_fetch`` or ``web_search`` call its
organisation's rule says to ask about. No trust tier answers that question: session
trust, trust-reads, a trusted pattern, YOLO, a scoped grant and the ``tool_permission``
grant ladder all leave it to the approval card. Either source says "ask": KAS's own
consent block on the request, or the policy fetch's ``unattended_web_tools`` record.
ALLOW, DENY and no rule keep today's behaviour, and an ``oss`` build is unchanged.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_mode_security_helpers import state_fixture  # noqa: F401 -- requested by name
from chat_runner_coverage_helpers import (
    _complete,
    _permission,
    _runner_state,
    _set_stream,
    _settle,
    _slot,
)

from kiro_crew import web_ask_rule
from kiro_crew.acp._dispatch_parts.permission_requests import kas_managed_ask
from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, AcpEvent
from kiro_crew.dashboard import chat_handlers, chat_runner
from kiro_crew.platform import enterprise_policy_translate as t
from kiro_crew.platform import governance
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

_ORG_ASK = {"matchedRule": {"capability": "web_fetch", "effect": "ask"}, "scope": "organization"}


def _ceiling(*rules: str):
    body = "".join(f"  - {r}\n" for r in rules) if rules else "  []\n"
    return governance.parse_policy(t.translate("rules:\n" + body))


@pytest.fixture
def lts(monkeypatch):
    monkeypatch.setattr("kiro_crew.edition.EDITION", "lts")


@pytest.fixture
def lts_rule(monkeypatch):
    """LTS as the rule reads it, the rest of the turn left an ``oss`` build's: the
    dashboard harness composes no LTS boot."""
    from types import SimpleNamespace

    monkeypatch.setattr(web_ask_rule, "edition", SimpleNamespace(is_lts=lambda: True))


@pytest.fixture
def policy(monkeypatch):
    """Install the translated ceiling for *rules* as what ``policy_permits`` reads."""

    def install(*rules: str) -> None:
        # The translated document is an LTS fetch's; parsed as one in every test.
        with patch("kiro_crew.edition.EDITION", "lts"):
            ceiling = _ceiling(*rules)
        monkeypatch.setattr(
            "kiro_crew.edition_locks.policy_permits",
            lambda scope, item="", **_k: governance.resolve(ceiling, None, scope, item).permitted,
        )

    return install


def _kas_event(tool_id: str = "web_fetch", *, managed: bool = False, kas: bool = True, **kw):
    fields = {
        "kind": EVENT_PERMISSION_REQUEST,
        "title": "Fetching https://example.com",
        "request_id": "req-1",
        "tool_kind": "fetch",
        "tool_name": tool_id,
        "harness_tool_id": tool_id,
        "kas_builtin_ids": kas,
        "kas_managed_ask": managed,
        "raw_params_trusted": True,
        "raw_tool_params": {"url": "https://example.com"},
    }
    fields.update(kw)
    return AcpEvent(**fields)  # type: ignore[arg-type]


# ── The record the policy fetch writes ──────────────────────────────────────


class TestTranslation:
    @pytest.mark.parametrize(
        "rule, asked",
        [
            ("{capability: web_fetch, effect: ask}", ["web_fetch"]),
            ("{capability: web_search, effect: ask}", ["web_search"]),
            ("{capability: web_fetch, effect: ask, match: ['*.example.com']}", ["web_fetch"]),
            ("{capability: builtin, effect: ask}", ["web_fetch", "web_search"]),
            ("{capability: all, effect: ask}", ["web_fetch", "web_search"]),
        ],
    )
    def test_an_ask_rule_denies_its_term_in_the_record(self, lts, rule, asked):
        doc = t.translate(f"rules:\n  - {rule}\n")
        assert doc["unattended_web_tools"] == {"mode": "deny", "deny": asked}
        ceiling = governance.parse_policy(doc)
        for term in web_ask_rule.TERMS:
            permitted = governance.resolve(ceiling, None, web_ask_rule.SCOPE, term).permitted
            assert permitted is (term not in asked)

    @pytest.mark.parametrize(
        "rules",
        [
            "  []\n",
            "  - {capability: web_fetch, effect: allow}\n",
            "  - {capability: web_search, effect: deny}\n",
            "  - {capability: shell, effect: ask}\n",
        ],
    )
    def test_allow_deny_or_no_rule_writes_no_record(self, lts, rules):
        assert "unattended_web_tools" not in t.translate("rules:\n" + rules)

    def test_the_revision_names_the_web_ask_record(self):
        from kiro_crew.platform import enterprise_policy_fetch as epf

        assert epf.TRANSLATION_REVISION == "t7"


# ── KAS's consent block ─────────────────────────────────────────────────────


class TestKasConsent:
    @pytest.mark.parametrize("scope", ["organization", "administration"])
    def test_an_organisation_ask_is_read(self, scope):
        params = {"_meta": {"kiro": {"consent": {**_ORG_ASK, "scope": scope}}}}
        assert kas_managed_ask(params) is True

    @pytest.mark.parametrize(
        "consent",
        [
            {"askType": "implicit"},
            {**_ORG_ASK, "scope": "kiro"},
            {**_ORG_ASK, "scope": "user"},
            {**_ORG_ASK, "scope": ["organization"]},
            {"matchedRule": {"effect": "allow"}, "scope": "organization"},
            {"matchedRule": "ask", "scope": "organization"},
        ],
    )
    def test_any_other_shape_is_not(self, consent):
        assert kas_managed_ask({"_meta": {"kiro": {"consent": consent}}}) is False

    def test_no_meta_is_not(self):
        assert kas_managed_ask({}) is False
        assert kas_managed_ask(None) is False


# ── The predicate ───────────────────────────────────────────────────────────


class TestNeedsPerson:
    def test_a_policy_ask_names_the_term(self, lts, policy):
        policy("{capability: web_fetch, effect: ask}")
        assert web_ask_rule.needs_person(_kas_event("web_fetch")) == "web_fetch"
        assert web_ask_rule.needs_person(_kas_event("remote_web_search")) == ""

    def test_web_search_is_named_by_its_remote_tool_id(self, lts, policy):
        policy("{capability: web_search, effect: ask}")
        assert web_ask_rule.needs_person(_kas_event("remote_web_search")) == "web_search"

    def test_kas_consent_alone_is_enough(self, lts, policy):
        policy()
        assert web_ask_rule.needs_person(_kas_event("web_fetch", managed=True)) == "web_fetch"

    def test_kas_consent_on_another_tool_is_not_this_rule(self, lts, policy):
        policy()
        assert web_ask_rule.needs_person(_kas_event("run_command", managed=True)) == ""

    def test_a_kas_request_with_no_tool_id_fails_closed_under_an_ask(self, lts, policy):
        policy("{capability: web_search, effect: ask}")
        assert web_ask_rule.needs_person(_kas_event("")) == "unidentified"
        policy()
        assert web_ask_rule.needs_person(_kas_event("")) == ""

    @pytest.mark.parametrize(
        "rule",
        [None, "{capability: web_fetch, effect: allow}", "{capability: web_fetch, effect: deny}"],
    )
    def test_allow_deny_and_no_rule_need_nobody(self, lts, policy, rule):
        policy(*([rule] if rule else []))
        assert web_ask_rule.needs_person(_kas_event("web_fetch")) == ""

    def test_another_harness_is_not_judged(self, lts, policy):
        policy("{capability: web_fetch, effect: ask}")
        assert web_ask_rule.needs_person(_kas_event("web_fetch", kas=False, managed=True)) == ""

    def test_an_oss_build_is_unchanged(self, policy):
        policy("{capability: web_fetch, effect: ask}")
        assert web_ask_rule.needs_person(_kas_event("web_fetch", managed=True)) == ""
        assert web_ask_rule.needs_person(_kas_event("")) == ""


# ── The tool_permission grant ladder ────────────────────────────────────────


class _Wire:
    def __init__(self, log: list) -> None:
        self.log = log

    async def allow(self, ask, stopping=None) -> bool:
        self.log.append("allow")
        return True

    async def refuse(self, ask, notice) -> None:
        self.log.append("reject")


class _Audit:
    def refused(self, ask, refusal) -> None: ...

    def approved(self, ask, settled) -> None: ...

    def declined(self, ask, refusal, tier) -> None: ...


class _Gate:
    def judge(self, ask):
        return Hit("hook_auto_approve", Evidence.IDENTITY)


class _Grant:
    async def offer(self, ask):
        return Hit("parent_policy_auto", Evidence.NONE)


class _Person:
    name = "callback"
    attended = True

    def __init__(self, log: list) -> None:
        self.log = log

    async def ask(self, ask) -> Answer:
        self.log.append("person")
        return Answer(approved=False)


async def _settle_with_grants(event) -> list:
    log: list = []
    policy = Policy(
        gate=_Gate(),
        audit=_Audit(),
        otherwise=Refusal.host("headless", "x", "policy"),
        grants=(GATE_GRANT, _Grant()),
        responders=(_Person(log),),
    )
    await settle(Ask(event, _Wire(log), "k"), policy)
    return log


@pytest.mark.asyncio
async def test_settle_asks_the_person_and_no_grant_under_an_ask(lts, policy):
    policy("{capability: web_fetch, effect: ask}")
    assert await _settle_with_grants(_kas_event("web_fetch")) == ["person", "reject"]


@pytest.mark.asyncio
@pytest.mark.parametrize("edition", ["oss", "lts"])
async def test_settle_keeps_its_grants_without_an_ask(monkeypatch, policy, edition):
    monkeypatch.setattr("kiro_crew.edition.EDITION", edition)
    policy("{capability: web_fetch, effect: allow}")
    assert await _settle_with_grants(_kas_event("web_fetch")) == ["allow"]


# ── The dashboard turn ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _vouch_for_the_program():
    with (
        patch.object(chat_runner, "_name_grant_refusal_off_loop", new=AsyncMock(return_value=None)),
        patch.object(chat_runner, "_name_grant_refusal_for", new=AsyncMock(return_value=None)),
    ):
        yield


def _web_permission(**kw):
    base = _permission(
        title="Fetching https://example.com",
        tool_input=json.dumps({"url": "https://example.com"}),
        tool_kind="fetch",
        is_shell=False,
        tool_name="web_fetch",
    )
    fields = {"harness_tool_id": "web_fetch", "kas_builtin_ids": True}
    fields.update(kw)
    return dataclasses.replace(base, **fields)


def _trusting(mode: str, state):
    slot = _slot()
    if mode == "trust":
        slot._trust = True
    elif mode == "yolo":
        state.is_yolo_active = MagicMock(return_value=True)
    elif mode == "scoped":
        slot._trust_scope = "scope-1"
    return slot


async def _turn(tmp_path, mode: str, event) -> tuple:
    state, client = _runner_state(tmp_path)
    slot = _trusting(mode, state)
    _set_stream(client, [event, _complete()])
    with (
        patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0),
        patch.object(chat_runner, "safety_override") as override,
    ):
        override.return_value.is_scope_active.return_value = mode == "scoped"
        override.return_value.is_active.return_value = mode == "yolo"
        slot._empty_response_retries = 2
        await chat_runner._run_chat(state, slot, "hello")
        await _settle(slot)
    return client, slot


_MODES = ["trust", "yolo", "scoped"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _MODES)
async def test_a_policy_ask_reaches_the_card_under_every_trust_tier(
    tmp_path, lts_rule, policy, mode
):
    policy("{capability: web_fetch, effect: ask}")
    client, slot = await _turn(tmp_path, mode, _web_permission())
    client.approve_tool.assert_not_awaited()
    cards = [m for m in slot.messages if m.get("role") == "permission"]
    assert len(cards) == 1
    assert json.loads(cards[0]["cls"]).get(web_ask_rule.CARD_META_KEY) == "1"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _MODES)
async def test_kas_consent_alone_reaches_the_card(tmp_path, lts_rule, policy, mode):
    policy()
    client, _slot_ = await _turn(tmp_path, mode, _web_permission(kas_managed_ask=True))
    client.approve_tool.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("edition", ["oss", "lts"])
@pytest.mark.parametrize(
    "rule",
    [None, "{capability: web_fetch, effect: allow}", "{capability: web_fetch, effect: deny}"],
)
async def test_trust_approves_as_before_without_an_ask(
    tmp_path, monkeypatch, policy, edition, rule, mode
):
    """The control: each tier above does approve this call when no ``ask`` is in force."""
    if edition == "lts":
        monkeypatch.setattr(web_ask_rule, "edition", MagicMock(is_lts=lambda: True))
    policy(*([rule] if rule else []))
    client, _slot_ = await _turn(tmp_path, mode, _web_permission())
    client.approve_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_oss_build_lets_trust_answer_even_an_ask(tmp_path, policy):
    policy("{capability: web_fetch, effect: ask}")
    client, _slot_ = await _turn(tmp_path, "trust", _web_permission(kas_managed_ask=True))
    client.approve_tool.assert_awaited_once()


# ── A trust grant's sweep of pending cards ──────────────────────────────────


def test_a_trust_sweep_leaves_a_web_ask_card_open(monkeypatch):
    loop = asyncio.new_event_loop()
    try:
        slot = _slot()
        asked, plain = loop.create_future(), loop.create_future()
        slot._approval_futures = {"web": asked, "other": plain}
        slot.messages = [
            {"role": "permission", "cls": json.dumps({"request_id": "web", "web_ask_rule": "1"})},
            {"role": "permission", "cls": json.dumps({"request_id": "other"})},
        ]
        state = MagicMock()
        monkeypatch.setattr(chat_handlers, "_mark_permission_resolved", lambda *a: False)
        monkeypatch.setattr(chat_handlers, "native_resolution_target", lambda _m: {})
        monkeypatch.setattr(chat_handlers, "sel", MagicMock())
        chat_handlers._sweep_pending_approvals(state, slot, "trust", lambda c: c)
        assert not asked.done()
        assert plain.result() == "approved"
    finally:
        loop.close()


def test_the_card_names_the_rule():
    assert "asks before web fetch." in web_ask_rule.card_notice("web_fetch")
    assert "asks before web search." in web_ask_rule.card_notice("web_search")
    assert "could not be identified" in web_ask_rule.card_notice("unidentified")


@pytest.mark.asyncio
async def test_an_all_slots_trust_sweep_leaves_web_ask_background_and_channel_approvals(
    state,
):
    """``web_ask_rule``: an unscoped Trust grant sweeps background and channel
    approvals, but not the ones an organisation's web ``ask`` holds for a person."""
    from types import SimpleNamespace

    loop = asyncio.get_running_loop()
    held_bg, plain_bg = loop.create_future(), loop.create_future()
    state._approval_futures.update({"bg-web": held_bg, "bg-plain": plain_bg})
    state._pending_approvals.update(
        {
            "bg-web": {"id": "bg-web", "slot": "", "person_only": True},
            "bg-plain": {"id": "bg-plain", "slot": ""},
        }
    )
    held_ch, plain_ch = loop.create_future(), loop.create_future()
    agents = {
        "a": SimpleNamespace(
            agent_name="a", _approval_future=held_ch, _pending_approval_person_only=True
        ),
        "b": SimpleNamespace(
            agent_name="b", _approval_future=plain_ch, _pending_approval_person_only=False
        ),
    }
    state.channel_manager = MagicMock()
    state.channel_manager._channels = {
        "c1": SimpleNamespace(id="c1", members=agents, trusted=False, _save=lambda: None)
    }
    with patch.object(chat_handlers, "sel", MagicMock()):
        await chat_handlers.apply_approval_mode(
            state,
            mode="trust",
            slot=None,
            slot_key=None,
            request_app="",
            cron_creator="",
            audit_caller=lambda c: c,
        )
    assert not held_bg.done() and not held_ch.done()
    assert plain_bg.done() and plain_ch.result() == "approved"
