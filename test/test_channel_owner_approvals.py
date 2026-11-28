"""Only a channel's owner changes YOLO or approves a tool, on every channel that offers them.

The owner is the identity the channel's owner setting names (``telegram.owner_user_id``,
``teams.owner_id``, ``webex.owner_email``), else the one allow-listed identity, else
nobody. Each case drives a real dispatcher: ``/yolo on`` from the owner arms the shared
grant and from anyone else is refused with the grant left off, and an approval answer
from anyone else is read as a deny.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from telegram_helpers import _dispatcher as _tg_dispatcher
from telegram_helpers import _drop_live_config_snapshot  # noqa: F401
from telegram_helpers import _dm, _prime_live
from test_teams_commands_dispatch import _SVC, _Client, _Sessions
from webex_dispatch_helpers import FakeClient, FakeCtx, FakeProvider, FakeSessions
from webex_dispatch_helpers import _cfg as _webex_cfg
from webex_dispatch_helpers import _dispatcher as _webex_dispatcher
from webex_dispatch_helpers import _inbound as _webex_inbound
from webex_dispatch_helpers import _spin

from kiro_crew.messaging import commands as shared
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE
from kiro_crew.safety_override import safety_override
from kiro_crew.teams.approvals import TeamsApprovalDecider
from kiro_crew.teams.cards import DECISION_TRUST, KIND_APPROVAL
from kiro_crew.teams.client import TeamsInbound
from kiro_crew.teams.transport_dispatch import TeamsDispatcher
from kiro_crew.telegram.renderer import TelegramApprovalDecider
from kiro_crew.testing.wait import await_until
from kiro_crew.webex import transport_dispatch as webex_dispatch

_OWNER, _OTHER = 7, 8


@pytest.fixture(autouse=True)
def _grant_off() -> Any:
    safety_override().deactivate("test-setup")
    yield
    safety_override().deactivate("test-teardown")


class TestIsChannelOwner:
    def test_the_named_owner_is_the_owner(self) -> None:
        assert shared.is_channel_owner(["A@x.com"], owner="a@x.com", allowed=["a@x.com", "b@x.com"])
        assert not shared.is_channel_owner(
            ["b@x.com"], owner="a@x.com", allowed=["a@x.com", "b@x.com"]
        )

    def test_with_none_named_the_only_listed_identity_is_the_owner(self) -> None:
        assert shared.is_channel_owner([7], owner=0, allowed=[7])
        assert not shared.is_channel_owner([7], owner=0, allowed=[7, 8])
        assert not shared.is_channel_owner([7], owner=0, allowed=[])


# ── Telegram ────────────────────────────────────────────────────────────────


def _tg(allowed: set[int], owner: int = 0) -> Any:
    d, cli, sess = _tg_dispatcher(allowed)
    d.cfg.telegram.owner_user_id = owner
    _prime_live(d.cfg)
    return d, cli, sess


class TestTelegramYolo:
    def test_a_listed_user_who_is_not_the_owner_is_refused(self) -> None:
        d, cli, _ = _tg({_OWNER, _OTHER}, owner=_OWNER)
        asyncio.run(d.handle_message(_dm("/yolo on", uid=str(_OTHER))))
        assert safety_override().is_active() is False
        assert cli.sent[-1][0] == shared.YOLO_OWNER_ONLY

    def test_the_named_owner_arms_it(self) -> None:
        d, cli, _ = _tg({_OWNER, _OTHER}, owner=_OWNER)
        asyncio.run(d.handle_message(_dm("/yolo on", uid=str(_OWNER))))
        assert safety_override().is_active() is True

    def test_a_single_user_with_no_owner_set_still_arms_it(self) -> None:
        d, _cli, _ = _tg({_OWNER})
        asyncio.run(d.handle_message(_dm("/yolo on", uid=str(_OWNER))))
        assert safety_override().is_active() is True

    def test_several_users_and_no_owner_set_means_nobody_changes_it(self) -> None:
        d, cli, _ = _tg({_OWNER, _OTHER})
        safety_override().activate("test")
        asyncio.run(d.handle_message(_dm("/yolo off", uid=str(_OWNER))))
        assert safety_override().is_active() is True
        assert cli.sent[-1][0] == shared.YOLO_OWNER_ONLY

    def test_anyone_listed_may_still_read_the_status(self) -> None:
        d, cli, _ = _tg({_OWNER, _OTHER}, owner=_OWNER)
        asyncio.run(d.handle_message(_dm("/yolo", uid=str(_OTHER))))
        assert cli.sent[-1][0].startswith("YOLO is OFF")


def _decider_for(d: Any, uid: int) -> Any:
    captured: list[dict] = []

    class _Recorder:
        def __init__(self, *a: Any, **kw: Any) -> None:
            captured.append(kw)

        async def run(self, message: str) -> str:
            return "ok"

    with patch("kiro_crew.telegram.transport_dispatch.TurnDriver", _Recorder):
        asyncio.run(d.handle_message(_dm("list the files", uid=str(uid))))
    assert len(captured) == 1
    return captured[0]["decider"]


class TestTelegramApprovals:
    def test_only_the_owner_is_sent_an_approval_prompt(self) -> None:
        d, _cli, _ = _tg({_OWNER, _OTHER}, owner=_OWNER)
        d.approval_mode = APPROVAL_INTERACTIVE
        assert _decider_for(d, _OTHER) is None, "they would be approving their own tool"
        assert isinstance(_decider_for(d, _OWNER), TelegramApprovalDecider)

    @pytest.mark.parametrize(
        ("owner", "flag", "result"),
        [(_OWNER, "1", False), (_OWNER, "t", False), (_OTHER, "1", True)],
    )
    def test_an_approve_from_anyone_but_the_owner_is_a_deny(
        self, owner: int, flag: str, result: bool
    ) -> None:
        from kiro_crew.messaging import session_trust as trust

        trust.clear_trusted_sessions()

        async def _go() -> tuple[asyncio.Future, Any, Any]:
            d, cli, _ = _tg({_OWNER, _OTHER}, owner=owner)
            session_key = d._session_key(("direct", str(_OTHER)))
            key = TelegramApprovalDecider.key(session_key, "r1")
            fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            TelegramApprovalDecider._REGISTRY[key] = fut
            TelegramApprovalDecider.arm(key, "n1")
            press = SimpleNamespace(
                callback_query_id="q",
                user_id=_OTHER,
                chat_id=_OTHER,
                chat_type="private",
                message_id=1,
                data=f"a:r1:n1:{flag}",
                label="",
                message_thread_id=None,
            )
            try:
                await d.on_callback(press)
            finally:
                TelegramApprovalDecider._REGISTRY.pop(key, None)
                TelegramApprovalDecider._NONCES.pop(key, None)
            return fut, session_key, cli

        fut, session_key, _cli = asyncio.run(_go())
        assert fut.done() and fut.result() is result
        assert trust.is_session_trusted(session_key) is (result and flag == "t")


# ── Teams ───────────────────────────────────────────────────────────────────

_ME, _PEER = "me@example.com", "peer@example.com"


def _teams(allowed: set[str], owner: str = "") -> tuple[TeamsDispatcher, _Client]:
    client = _Client()
    d = TeamsDispatcher(
        sessions=_Sessions(),
        ctx_builder=SimpleNamespace(hooks=SimpleNamespace(auto_approve_subagent_spawn=False)),
        cfg=SimpleNamespace(
            messaging=SimpleNamespace(
                queue_mode="steer", dm_scope="per_user", idle_reset_minutes=0, daily_reset_hour=-1
            ),
            agent=SimpleNamespace(default_agent="kirocrew", approval_mode="interactive"),
            teams=SimpleNamespace(soft_threshold_pct=80, hard_threshold_pct=95, owner_id=owner),
            dashboard=SimpleNamespace(url=""),
        ),
        allowed_emails=allowed,
    )
    d.client = client
    # The owner is read from the live config, the way production reads it.
    import dataclasses

    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig

    base = KiroCrewConfig()
    live.reset_for_tests()
    live.watch().prime(
        dataclasses.replace(base, teams=dataclasses.replace(base.teams, owner_id=owner))
    )
    return d, client


def _teams_inbound(text: str, email: str, card_value: dict | None = None) -> TeamsInbound:
    return TeamsInbound(
        conversation_id="CONV",
        conversation_type="personal",
        service_url=_SVC,
        text=text,
        user_email=email,
        activity_id="act-1",
        card_value=card_value,
    )


class TestTeams:
    @pytest.mark.asyncio
    async def test_yolo_on_from_the_owner_and_from_someone_else(self) -> None:
        d, client = _teams({_ME, _PEER}, owner=_ME)
        await d.handle_message(_teams_inbound("/yolo on", _PEER))
        assert safety_override().is_active() is False
        assert client.sent[-1] == shared.YOLO_OWNER_ONLY
        await d.handle_message(_teams_inbound("/yolo on", _ME))
        assert safety_override().is_active() is True

    @pytest.mark.asyncio
    async def test_a_single_identity_with_no_owner_set_still_arms_it(self) -> None:
        d, _client = _teams({_ME})
        await d.handle_message(_teams_inbound("/yolo on", _ME))
        assert safety_override().is_active() is True

    @pytest.mark.asyncio
    async def test_a_trust_click_from_someone_else_denies_and_arms_nothing(self) -> None:
        TeamsApprovalDecider.reset_for_tests()
        try:
            d, _client = _teams({_ME, _PEER}, owner=_ME)
            decider = TeamsApprovalDecider(session_key=d._session_key(_PEER))
            decider.arm("7", "n1")
            task = asyncio.ensure_future(decider(SimpleNamespace(request_id="7")))
            await asyncio.sleep(0)
            click = {"kc": KIND_APPROVAL, "rid": "7", "nonce": "n1", "decision": DECISION_TRUST}
            await d._handle_card_action(_teams_inbound("", _PEER, card_value=click))
            assert await await_until(task, what="the Teams approval") is False
            assert safety_override().is_active() is False
        finally:
            TeamsApprovalDecider.reset_for_tests()


# ── Webex ───────────────────────────────────────────────────────────────────

_A, _B = "a@example.com", "b@example.com"


def _webex(owner: str = "", allowed: tuple[str, ...] = (_A, _B)) -> Any:
    cfg = _webex_cfg()
    cfg.webex.allowed_emails = list(allowed)
    cfg.webex.owner_email = owner
    return _webex_dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient(), cfg=cfg)


class TestWebex:
    @pytest.mark.asyncio
    async def test_yolo_on_from_the_owner_and_from_someone_else(self) -> None:
        d = _webex(owner=_A)
        await d.handle_message(_webex_inbound("/yolo on", email=_B))
        assert safety_override().is_active() is False
        assert d.client.sent[-1][1] == shared.YOLO_OWNER_ONLY
        await d.handle_message(_webex_inbound("/yolo on", email=_A))
        assert safety_override().is_active() is True

    @pytest.mark.asyncio
    async def test_a_single_email_with_no_owner_set_still_arms_it(self) -> None:
        d = _webex(allowed=(_A,))
        await d.handle_message(_webex_inbound("/yolo on", email=_A))
        assert safety_override().is_active() is True

    @pytest.mark.asyncio
    async def test_a_typed_approve_from_someone_else_is_a_deny(self) -> None:
        cfg = _webex_cfg()
        cfg.webex.allowed_emails = [_A, _B]
        cfg.webex.owner_email = _A
        sessions = FakeSessions(FakeProvider([]))
        sessions._busy = True
        d = _webex_dispatcher(sessions, FakeCtx(), FakeClient(), cfg=cfg)
        key = d._approval_key(_B)
        webex_dispatch._APPROVALS.reserve(key, 1)
        task = asyncio.create_task(
            webex_dispatch._APPROVALS.decide_with_cause(key, SimpleNamespace(request_id=1))
        )
        await _spin(lambda: webex_dispatch._APPROVALS.has_pending(key))
        await d.handle_message(_webex_inbound("1", email=_B))
        assert (await await_until(task, what="the Webex approval"))[0] is False
