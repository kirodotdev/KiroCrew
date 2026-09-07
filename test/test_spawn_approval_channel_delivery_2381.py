"""Channel-side delivery of the spawn-approval prompt — issue #2381 item 1.

Post-#8914 a spawn parented on a Telegram conversation reached no surface and was
refused fast (``no_approval_surface``): the host spawn gate raced only a Slack
owner DM and the dashboard. This suite covers the fix — a channel-neutral
delivery seam the host gate consults FIRST, and Telegram's implementation of it
over the same Approve/Deny/Trust keyboard the main-agent tool ladder already
uses.

Four things are pinned:

* the seam registry itself — register/lookup by session, per-channel isolation,
  and the ``None`` fall-through contract;
* the wrapped ``on_spawn_approval`` shape — the channel decision is returned when
  a hook answers ``True``/``False``, and the pre-existing Slack-DM/dashboard gate
  (still raising ``SpawnApprovalUnreachable`` with no surface attached) runs when
  the hook answers ``None`` or none is registered;
* the Telegram delivery hook — an Approve/Deny/Trust keyboard for a ``spawn:<id>``
  request, resolving ``True`` on Approve, ``False`` on Deny, and granting session
  trust on Trust, all through the existing ``on_callback`` ``a:`` path;
* precedence — a trusted/auto parent never reaches the channel prompt, because
  the host auto-approve rungs run before ``on_spawn_approval`` is ever invoked.

All Telegram client I/O is faked; nothing touches the network.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# Reuse the channel fixtures the existing Telegram suite uses (fake client,
# fake sessions, dispatcher factory) so this suite exercises the SAME doubles.
from test_telegram import _dispatcher  # noqa: E402

from kiro_crew.messaging import spawn_approval_delivery as seam
from kiro_crew.messaging.session_trust import (
    _trusted_sessions,
    clear_trusted_sessions,
    is_session_trusted,
)
from kiro_crew.subagent import SpawnApprovalUnreachable
from kiro_crew.telegram.renderer import TelegramApprovalDecider

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@pytest.fixture(autouse=True)
def _clean_seam():
    """Every test starts and ends with an empty registry and no lingering trust."""
    seam.clear_channel_delivery_hooks()
    TelegramApprovalDecider._REGISTRY.clear()
    TelegramApprovalDecider._NONCES.clear()
    _trusted_sessions.clear()
    yield
    seam.clear_channel_delivery_hooks()
    TelegramApprovalDecider._REGISTRY.clear()
    TelegramApprovalDecider._NONCES.clear()
    clear_trusted_sessions()


# ── (a) the delivery seam registry ──────────────────────────────────────────


class TestDeliverySeamRegistry:
    """A process-global map from a channel to its live delivery hook."""

    @pytest.mark.asyncio
    async def test_registered_hook_is_resolved_for_its_channel_session(self) -> None:
        async def _hook(_rid: str, _desc: str, _parent: str) -> bool:
            return True

        seam.register_channel_delivery("telegram", _hook)
        resolved = seam.resolve_channel_delivery("telegram:kirocrew:direct:7")
        assert resolved is _hook

    @pytest.mark.asyncio
    async def test_resolve_returns_the_hooks_decision(self) -> None:
        async def _yes(_rid: str, _desc: str, _parent: str) -> bool:
            return True

        seam.register_channel_delivery("telegram", _yes)
        assert await seam.deliver_spawn_approval("spawn:a", "spawn_run(x)", "telegram:k:direct:7")

    @pytest.mark.asyncio
    async def test_an_unregistered_channel_falls_through_with_none(self) -> None:
        # Nothing registered for slack -> None (the gate falls through).
        assert await seam.deliver_spawn_approval("spawn:a", "d", "slack:1700000000.0001") is None

    @pytest.mark.asyncio
    async def test_an_unowned_or_non_channel_key_falls_through_with_none(self) -> None:
        async def _hook(_rid: str, _desc: str, _parent: str) -> bool:
            return True

        seam.register_channel_delivery("telegram", _hook)
        # A dashboard/cron/subagent key is not a channel key -> no hook resolves.
        assert seam.resolve_channel_delivery("dashboard:chat-1-2") is None
        assert seam.resolve_channel_delivery("") is None
        assert await seam.deliver_spawn_approval("spawn:a", "d", "cron:job7") is None

    @pytest.mark.asyncio
    async def test_hooks_are_per_channel_isolated(self) -> None:
        async def _tg(_rid: str, _desc: str, _parent: str) -> bool:
            return True

        async def _slack(_rid: str, _desc: str, _parent: str) -> bool:
            return False

        seam.register_channel_delivery("telegram", _tg)
        seam.register_channel_delivery("slack", _slack)
        assert seam.resolve_channel_delivery("telegram:k:direct:7") is _tg
        assert seam.resolve_channel_delivery("slack:1700000000.0001") is _slack

    @pytest.mark.asyncio
    async def test_unregister_stops_resolution(self) -> None:
        async def _hook(_rid: str, _desc: str, _parent: str) -> bool:
            return True

        seam.register_channel_delivery("telegram", _hook)
        seam.unregister_channel_delivery("telegram")
        assert seam.resolve_channel_delivery("telegram:k:direct:7") is None
        # Idempotent: a second unregister (or one that never registered) is safe.
        seam.unregister_channel_delivery("telegram")
        seam.unregister_channel_delivery("discord")

    @pytest.mark.asyncio
    async def test_a_raising_hook_is_contained_as_a_fall_through(self) -> None:
        async def _boom(_rid: str, _desc: str, _parent: str) -> bool:
            raise RuntimeError("delivery bug")

        seam.register_channel_delivery("telegram", _boom)
        # A channel-delivery bug degrades to the fallback (None), never a hard fail.
        assert await seam.deliver_spawn_approval("spawn:a", "d", "telegram:k:direct:7") is None

    @pytest.mark.asyncio
    async def test_registering_replaces_the_channels_prior_hook(self) -> None:
        async def _old(_rid: str, _desc: str, _parent: str) -> bool:
            return True

        async def _new(_rid: str, _desc: str, _parent: str) -> bool:
            return False

        seam.register_channel_delivery("telegram", _old)
        seam.register_channel_delivery("telegram", _new)
        assert seam.resolve_channel_delivery("telegram:k:direct:7") is _new


# ── (b) the wrapped on_spawn_approval callback ──────────────────────────────


def _spawn_callback():  # type: ignore[no-untyped-def]
    """Reproduce the gateway's ``_spawn_approve`` wiring over the real seam.

    Seam FIRST; on ``None`` fall through to a stand-in for the pre-existing
    ``_approve_spawn_gate`` that raises ``SpawnApprovalUnreachable`` exactly as
    the Slack-DM/dashboard gate does when no surface is attached. The source
    ratchet below pins that the production callback keeps this shape.
    """
    from kiro_crew.messaging.spawn_approval_delivery import deliver_spawn_approval
    from kiro_crew.providers.base import LLMEvent

    async def _approve_spawn_gate(_event: LLMEvent, _parent: str) -> bool:
        raise SpawnApprovalUnreachable("no interactive surface is attached")

    async def _spawn_approve(
        request_id: str, description: str, parent_session_key: str = ""
    ) -> bool:
        channel_decision = await deliver_spawn_approval(request_id, description, parent_session_key)
        if channel_decision is not None:
            return channel_decision
        event = LLMEvent(kind="permission_request", request_id=request_id, title=description)
        return await _approve_spawn_gate(event, parent_session_key)

    return _spawn_approve


class TestWrappedSpawnApproval:
    """The host callback consults the channel FIRST, then the Slack/dashboard gate."""

    @pytest.mark.asyncio
    async def test_channel_true_is_the_decision(self) -> None:
        async def _yes(_rid: str, _desc: str, _parent: str) -> bool:
            return True

        seam.register_channel_delivery("telegram", _yes)
        cb = _spawn_callback()
        assert await cb("spawn:a", "spawn_run(x)", "telegram:k:direct:7") is True

    @pytest.mark.asyncio
    async def test_channel_false_is_the_decision(self) -> None:
        async def _no(_rid: str, _desc: str, _parent: str) -> bool:
            return False

        seam.register_channel_delivery("telegram", _no)
        cb = _spawn_callback()
        assert await cb("spawn:a", "spawn_run(x)", "telegram:k:direct:7") is False

    @pytest.mark.asyncio
    async def test_channel_none_falls_through_to_the_unreachable_gate(self) -> None:
        async def _none(_rid: str, _desc: str, _parent: str) -> "bool | None":
            return None

        seam.register_channel_delivery("telegram", _none)
        cb = _spawn_callback()
        with pytest.raises(SpawnApprovalUnreachable):
            await cb("spawn:a", "spawn_run(x)", "telegram:k:direct:7")

    @pytest.mark.asyncio
    async def test_no_hook_falls_through_to_the_unreachable_gate(self) -> None:
        # No channel registered at all -> the pre-#8914 behaviour is byte-for-byte.
        cb = _spawn_callback()
        with pytest.raises(SpawnApprovalUnreachable):
            await cb("spawn:a", "spawn_run(x)", "telegram:k:direct:7")

    def test_source_still_consults_the_seam_before_the_gate(self) -> None:
        """Source ratchet: the seam consult must precede the Slack/dashboard gate.

        The behavioural tests above run over a stand-in gate; this pins that the
        REAL ``_spawn_approve`` keeps the seam-first shape and that the exact
        substring the #8914 ratchet also asserts is still present.
        """
        import kiro_crew.slack.gateway as gateway_mod

        src = Path(gateway_mod.__file__).read_text(encoding="utf-8")
        consult = src.index("deliver_spawn_approval(")
        fallback = src.index("return await _approve_spawn_gate(event, parent_session_key)")
        assert consult < fallback, "the channel seam must be consulted before the fallback gate"
        assert "return await _approve_spawn_gate(event, parent_session_key)" in src


# ── (c) the Telegram delivery hook ──────────────────────────────────────────


async def _press(dispatcher, session_key: str, request_id: str, flag: str) -> None:  # type: ignore[no-untyped-def]
    """Simulate the user pressing an Approve/Deny/Trust button in the DM.

    Waits for the hook to post its prompt and register its awaiting future, then
    recovers the nonce it armed and drives ``on_callback`` exactly as a real
    inline-button press does — so the spawn prompt resolves through the SAME
    ``a:`` path a tool approval uses.
    """
    key = TelegramApprovalDecider.key(session_key, request_id)
    for _ in range(50):
        if key in TelegramApprovalDecider._REGISTRY:
            break
        await asyncio.sleep(0.01)
    nonce = TelegramApprovalDecider._NONCES[key]
    cb = SimpleNamespace(
        callback_query_id="q1",
        user_id=7,
        chat_id=7,
        message_id=100,
        data=f"a:{request_id}:{nonce}:{flag}",
        label="",
        chat_type="private",
    )
    await dispatcher.on_callback(cb)


class TestTelegramDeliveryHook:
    """The prompt lands in the originating conversation and the press resolves it."""

    def test_approve_resolves_true(self) -> None:
        d, cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))

        async def _go() -> bool:
            task = asyncio.ensure_future(
                d.deliver_spawn_approval("spawn:abc", "spawn_run(build)", session_key)
            )
            await asyncio.sleep(0)  # let the prompt post and the future register
            await _press(d, session_key, "spawn:abc", "1")
            return await task

        assert asyncio.run(_go()) is True

    def test_deny_resolves_false(self) -> None:
        d, cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))

        async def _go() -> bool:
            task = asyncio.ensure_future(
                d.deliver_spawn_approval("spawn:abc", "spawn_run(build)", session_key)
            )
            await asyncio.sleep(0)
            await _press(d, session_key, "spawn:abc", "0")
            return await task

        assert asyncio.run(_go()) is False

    def test_trust_grants_session_trust_and_resolves_true(self) -> None:
        d, cli, sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))

        async def _go() -> bool:
            task = asyncio.ensure_future(
                d.deliver_spawn_approval("spawn:abc", "spawn_run(build)", session_key)
            )
            await asyncio.sleep(0)
            await _press(d, session_key, "spawn:abc", "t")
            return await task

        assert asyncio.run(_go()) is True
        # Trust granted for the parent session so subsequent spawns auto-approve.
        assert is_session_trusted(session_key)

    def test_the_prompt_is_an_approve_deny_trust_keyboard(self) -> None:
        d, cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))

        async def _go() -> None:
            task = asyncio.ensure_future(
                d.deliver_spawn_approval("spawn:abc", "spawn_run(build)", session_key)
            )
            for _ in range(50):
                if cli.sent:
                    break
                await asyncio.sleep(0.01)
            # One prompt posted, carrying the three-button keyboard for spawn:abc.
            assert len(cli.sent) == 1
            _text, markup = cli.sent[0]
            rows = markup["inline_keyboard"]
            datas = [btn["callback_data"] for row in rows for btn in row]
            assert any(cd.startswith("a:spawn:abc:") and cd.endswith(":1") for cd in datas)
            assert any(cd.startswith("a:spawn:abc:") and cd.endswith(":0") for cd in datas)
            assert any(cd.startswith("a:spawn:abc:") and cd.endswith(":t") for cd in datas)
            # Resolve so the awaiting task does not leak.
            await _press(d, session_key, "spawn:abc", "0")
            await task

        asyncio.run(_go())

    def test_a_non_telegram_parent_key_falls_through(self) -> None:
        d, cli, _sess = _dispatcher({7})
        # A Slack key handed to the Telegram hook cannot be turned into a chat.
        result = asyncio.run(d.deliver_spawn_approval("spawn:abc", "d", "slack:1700000000.0001"))
        assert result is None
        assert cli.sent == []

    def test_a_failed_post_falls_through_and_retires_the_nonce(self) -> None:
        d, cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))

        async def _boom(*_a, **_k):  # type: ignore[no-untyped-def]
            raise RuntimeError("telegram is down")

        d.client.send_message = _boom  # type: ignore[assignment]

        result = asyncio.run(d.deliver_spawn_approval("spawn:abc", "spawn_run(build)", session_key))
        assert result is None
        # The armed nonce was retired so no stale prompt lingers.
        key = TelegramApprovalDecider.key(session_key, "spawn:abc")
        assert key not in TelegramApprovalDecider._NONCES

    def test_a_missing_client_falls_through(self) -> None:
        d, _cli, _sess = _dispatcher({7})
        d.client = None  # type: ignore[assignment]
        result = asyncio.run(
            d.deliver_spawn_approval("spawn:abc", "d", d._session_key(("direct", "7")))
        )
        assert result is None


# ── (d) precedence: a trusted/auto parent never reaches the channel prompt ──


class TestAutoApprovedNeverReachesTheChannel:
    """The host auto-approve rungs run BEFORE ``on_spawn_approval`` is invoked."""

    @staticmethod
    def _mock_sessions(*, policy: str) -> MagicMock:
        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.context_usage_pct = lambda: 0.0

        async def _empty_stream(*_a: object, **_k: object):  # type: ignore[no-untyped-def]
            return
            yield

        provider.stream = MagicMock(side_effect=lambda *a, **kw: _empty_stream())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        sessions.record_success = MagicMock()
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_approval_policy = MagicMock(return_value=policy)
        return sessions

    @pytest.mark.asyncio
    async def test_a_trusted_parent_never_calls_the_spawn_approval(self) -> None:
        from kiro_crew.subagent import SubagentManager

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("built", None))
        ctx.hooks.on_tool_call = MagicMock()
        ctx.hooks.auto_approve_subagent_spawn = False

        approval = AsyncMock(return_value=True)
        mgr = SubagentManager(
            sessions=self._mock_sessions(policy="auto"),  # parent trusted
            ctx_builder=ctx,
            on_spawn_approval=approval,
            is_yolo=lambda: False,
        )
        info = mgr.spawn("do a thing", parent_session_key="telegram:k:direct:7")
        assert info is not None
        for _ in range(50):
            await asyncio.sleep(0)
        # The parent_trusted rung admitted the spawn — the approval callback (and
        # thus the channel prompt behind it) was never consulted.
        approval.assert_not_awaited()
        for t in list(mgr._tasks.values()):
            t.cancel()
        await asyncio.sleep(0)
