"""Channel-side delivery of the spawn-approval prompt.

The host spawn gate's own surfaces are a Slack owner DM and the dashboard, so a
spawn parented on a Telegram conversation reaches none of them and is refused
fast (``no_approval_surface``). This suite covers the channel-neutral delivery
seam the host gate consults FIRST, and Telegram's implementation of it over the
same Approve/Deny/Trust keyboard the main-agent tool ladder already uses.

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
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Reuse the channel fixtures the existing Telegram suite uses (fake client,
# fake sessions, dispatcher factory) so this suite exercises the SAME doubles.
from test_telegram import _cfg, _dispatcher, _prime_live  # noqa: E402

from kiro_crew.messaging import spawn_approval_delivery as seam
from kiro_crew.messaging.display_safety import canonicalize_display
from kiro_crew.messaging.link import CHAT_TYPE_FORUM, parse_session_key
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
        # No channel registered at all -> the unreachable gate answers unchanged.
        cb = _spawn_callback()
        with pytest.raises(SpawnApprovalUnreachable):
            await cb("spawn:a", "spawn_run(x)", "telegram:k:direct:7")

    def test_source_still_consults_the_seam_before_the_gate(self) -> None:
        """Source ratchet: the seam consult must precede the Slack/dashboard gate.

        The behavioural tests above run over a stand-in gate; this pins that the
        REAL ``_spawn_approve`` keeps the seam-first shape and that the exact
        substring the unreachable-gate ratchet also asserts is still present.
        """
        import kiro_crew.slack.gateway as gateway_mod

        src = Path(gateway_mod.__file__).read_text(encoding="utf-8")
        consult = src.index("deliver_spawn_approval(")
        fallback = src.index("return await _approve_spawn_gate(event, parent_session_key)")
        assert consult < fallback, "the channel seam must be consulted before the fallback gate"
        assert "return await _approve_spawn_gate(event, parent_session_key)" in src


# ── (c) the Telegram delivery hook ──────────────────────────────────────────


@pytest.fixture
def _press_lands_inside_the_window(
    _short_prompt_wait: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Give an arm that EXPECTS a press a lost-run window, not the refusal bound.

    The class-level ``_short_prompt_wait`` (0.2s) bounds the arms that never await a press. An arm whose press
    resolves the wait would otherwise also be asserting that ``on_callback``
    finishes inside 0.2s of wall clock, and that path includes the governance
    ``run_in_executor`` hop in ``channel_inbound_permitted``, which a loaded
    runner can stall past it: the wait expires, the future is popped, and the
    press resolves nothing. A press that lands returns at once, so this costs no
    time; one that never resolves still fails by name well inside ``--timeout``.
    """
    import kiro_crew.telegram.renderer as renderer_mod

    monkeypatch.setattr(renderer_mod, "_APPROVAL_TIMEOUT_S", 30.0)


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

    def test_a_credential_split_by_invisible_characters_is_not_displayed(self) -> None:
        """The preview is cleared in the form Telegram renders, as Discord's twin is.

        A zero-width character between two halves of a key breaks every literal
        scan while rendering as nothing, so the upstream pass leaves it intact and
        ``html.escape`` keeps it, and the reader sees a whole secret. The assertion
        is made on the canonicalized text for the same reason: the literal message
        never contains the secret as one string, so checking the literal would
        pass with no redaction at all.
        """
        d, cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))
        secret = "AKIAIOSFODNN7EXAMPLE"
        split = secret[:6] + "​" + secret[6:]

        async def _go() -> str:
            task = asyncio.ensure_future(
                d.deliver_spawn_approval(
                    "spawn:abc", f"spawn_run(deploy using {split})", session_key
                )
            )
            for _ in range(50):
                if cli.sent:
                    break
                await asyncio.sleep(0.01)
            text, _markup = cli.sent[0]
            await _press(d, session_key, "spawn:abc", "0")
            await task
            return text

        text = asyncio.run(_go())
        assert secret not in canonicalize_display(text), "the rendered preview carries the secret"
        assert "REDACTED" in text, text

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

    def test_a_forum_parent_threads_the_prompt_into_the_originating_topic(self) -> None:
        # A supergroup forum Topic parent: the only branch of _spawn_chat_target
        # that derives a thread id. The prompt must be posted into that Topic
        # (message_thread_id carried through) and the press through the same
        # forum-shaped callback resolves it.
        chat_id, thread = -1001234567890, 42
        d, cli, _sess = _dispatcher({7}, allow_forum=True, allowed_forum_chat_ids=[chat_id])
        route = (CHAT_TYPE_FORUM, f"{chat_id}:{thread}")
        session_key = d._session_key(route)
        # The parent key is a genuine forum shape: telegram:{agent}:forum:{chat}:{thread}.
        parsed = parse_session_key(session_key)
        assert parsed is not None and parsed.chat_type == CHAT_TYPE_FORUM
        assert parsed.scope == (str(chat_id), str(thread))

        async def _go() -> bool:
            task = asyncio.ensure_future(
                d.deliver_spawn_approval("spawn:abc", "spawn_run(build)", session_key)
            )
            for _ in range(50):
                if cli.sent:
                    break
                await asyncio.sleep(0.01)
            # The prompt was threaded into the originating Topic, not the group root.
            assert cli.send_threads == [thread]
            key = TelegramApprovalDecider.key(session_key, "spawn:abc")
            for _ in range(50):
                if key in TelegramApprovalDecider._REGISTRY:
                    break
                await asyncio.sleep(0.01)
            nonce = TelegramApprovalDecider._NONCES[key]
            cb = SimpleNamespace(
                callback_query_id="q1",
                user_id=7,
                chat_id=chat_id,
                message_id=100,
                data=f"a:spawn:abc:{nonce}:1",
                label="",
                chat_type="supergroup",
                message_thread_id=thread,
            )
            await d.on_callback(cb)
            return await task

        assert asyncio.run(_go()) is True

    def test_a_generation_rotation_between_spawn_and_press_expires_the_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The prompt is armed under parent_session_key VERBATIM, but a press
        # recomputes the key from the LIVE conversation. A /new (or idle/daily)
        # rotation between spawn and press bumps the generation, so the recomputed
        # key does not match the armed one: the press resolves nothing and the
        # prompt deny-by-defaults at the timeout. Pin that coupling here.
        import kiro_crew.telegram.renderer as renderer_mod

        monkeypatch.setattr(renderer_mod, "_APPROVAL_TIMEOUT_S", 0.2)
        d, cli, _sess = _dispatcher({7})
        route = ("direct", "7")
        session_key = d._session_key(route)  # gen0

        async def _go() -> bool:
            task = asyncio.ensure_future(
                d.deliver_spawn_approval("spawn:abc", "spawn_run(build)", session_key)
            )
            key = TelegramApprovalDecider.key(session_key, "spawn:abc")
            for _ in range(50):
                if key in TelegramApprovalDecider._REGISTRY:
                    break
                await asyncio.sleep(0.01)
            nonce = TelegramApprovalDecider._NONCES[key]
            # Rotate the parent conversation's generation AFTER arming.
            d._conv.bump_gen(route)
            # The press recomputes the (now rotated) key, which diverges from the
            # armed one, so the approve press cannot resolve the gen0 prompt.
            assert d._session_key(route) != session_key
            cb = SimpleNamespace(
                callback_query_id="q1",
                user_id=7,
                chat_id=7,
                message_id=100,
                data=f"a:spawn:abc:{nonce}:1",
                label="",
                chat_type="private",
            )
            await d.on_callback(cb)
            # The armed prompt is still pending — the press landed on a different
            # key — so it deny-by-defaults when the (shortened) timeout elapses.
            return await task

        assert asyncio.run(_go()) is False

    def test_a_cancelled_awaiting_decider_leaks_no_registry_or_nonce_entry(self) -> None:
        # The prompt posts and the hook awaits the press, but the awaiting task is
        # cancelled mid-prompt (e.g. gateway teardown) before a press arrives. The
        # decider's finally must still pop BOTH the registry future and the armed
        # nonce so a later request-id reuse cannot match a stale entry.
        d, cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))
        key = TelegramApprovalDecider.key(session_key, "spawn:abc")

        async def _go() -> None:
            task = asyncio.ensure_future(
                d.deliver_spawn_approval("spawn:abc", "spawn_run(build)", session_key)
            )
            for _ in range(50):
                if key in TelegramApprovalDecider._REGISTRY:
                    break
                await asyncio.sleep(0.01)
            # Prompt is live and awaiting: both entries armed.
            assert key in TelegramApprovalDecider._REGISTRY
            assert key in TelegramApprovalDecider._NONCES
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(_go())
        # No leak: the finally ran on cancellation and cleared both maps.
        assert key not in TelegramApprovalDecider._REGISTRY
        assert key not in TelegramApprovalDecider._NONCES

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


class TestASilentSendFailureFallsThroughAtOnce:
    """A post that fails by RETURNING no message id, rather than by raising.

    ``TelegramClient.send_message`` is typed ``int | None`` and answers ``None``
    once its own attempts are spent -- a revoked token, a deleted forum Topic, a
    chat the bot cannot write to, a 5xx. Nothing is on screen, so the gate reaches
    the same conclusion it reaches for a raise: retire the armed nonce and fall
    through. Reading only the exception leaves it awaiting a press nobody can
    make, which spends the whole approval window and then hands that silence to
    the host gate as a deny, postponing the Slack/dashboard surface that could
    have answered at once.

    What the assertions read is the RESULT (``None``, never ``False``) and the
    absence of a wait, not a duration; the fixture only keeps a regression fast.
    """

    @pytest.fixture(autouse=True)
    def _short_prompt_wait(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Shrink the deny-by-default wait so a REGRESSION fails fast.

        A gate that reads the absent id returns before the prompt is ever
        awaited, so this does not touch the path under test. It bounds the OTHER
        outcome: a gate that awaits anyway would park every assertion below on
        ``_APPROVAL_TIMEOUT_S`` (minutes) and surface as a suite-wide timeout
        instead of a failed assertion.
        """
        import kiro_crew.telegram.renderer as renderer_mod

        monkeypatch.setattr(renderer_mod, "_APPROVAL_TIMEOUT_S", 0.2)

    @staticmethod
    def _silent(cli: Any) -> list[int]:
        """Make the fake client accept the call and report no message id."""
        calls: list[int] = []

        async def _no_id(*_a: object, **_k: object) -> None:
            await asyncio.sleep(0)  # yield like a real network await
            calls.append(1)
            return None

        cli.send_message = _no_id
        return calls

    def test_an_absent_message_id_falls_through_instead_of_waiting(self) -> None:
        d, cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))
        self._silent(cli)

        result = asyncio.run(d.deliver_spawn_approval("spawn:abc", "spawn_run(build)", session_key))

        # ``None`` is "not surfaced here, fall through". ``False`` would be a
        # refusal the operator never made, and the host gate acts on it.
        assert result is None

    def test_it_never_enters_the_decision_wait_so_the_window_is_never_spent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d, cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))
        key = TelegramApprovalDecider.key(session_key, "spawn:abc")
        self._silent(cli)
        entered: list[str] = []

        async def _never(self: TelegramApprovalDecider, *_a: object, **_k: object) -> bool:
            entered.append("wait")
            return False

        # The future is armed BEFORE the post on purpose, so a press that lands
        # while the send is in flight has somewhere to go. What must not happen
        # is the decision wait itself: that is where the window is spent.
        monkeypatch.setattr(TelegramApprovalDecider, "__call__", _never)

        assert (
            asyncio.run(d.deliver_spawn_approval("spawn:abc", "spawn_run(build)", session_key))
            is None
        )
        assert entered == []
        # And nothing is left armed for a prompt that is not on screen.
        assert key not in TelegramApprovalDecider._REGISTRY

    def test_the_nonce_is_retired_so_a_later_prompt_cannot_be_answered_by_it(self) -> None:
        # Request ids are REUSABLE (an ACP sequence restarts per provider
        # process), so a nonce left armed by a prompt that never went out would
        # still authorize a press against the next prompt filed at the same key.
        d, cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))
        key = TelegramApprovalDecider.key(session_key, "spawn:abc")
        self._silent(cli)

        result = asyncio.run(d.deliver_spawn_approval("spawn:abc", "spawn_run(build)", session_key))

        assert result is None
        assert key not in TelegramApprovalDecider._NONCES
        assert key not in TelegramApprovalDecider._REGISTRY
        assert not TelegramApprovalDecider.nonce_matches(key, "any")

    def test_the_gate_does_not_repost_after_an_absent_id(self) -> None:
        # The client owns retrying: ``retry_plain`` re-sends without a parse_mode
        # inside one call, so an absent id is the verdict after those attempts.
        # The gate adds no send of its own on top of it.
        d, cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))
        calls = self._silent(cli)

        result = asyncio.run(d.deliver_spawn_approval("spawn:abc", "spawn_run(build)", session_key))

        assert result is None
        assert len(calls) == 1

    @pytest.mark.usefixtures("_press_lands_inside_the_window")
    def test_a_real_message_id_still_arms_the_wait_and_honors_the_press(self) -> None:
        # The other side of the same branch: a truthy id is a delivered prompt, so
        # the gate still awaits it and returns the press verbatim.
        d, cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))

        async def _go() -> bool | None:
            task = asyncio.ensure_future(
                d.deliver_spawn_approval("spawn:abc", "spawn_run(build)", session_key)
            )
            for _ in range(50):
                if cli.sent:
                    break
                await asyncio.sleep(0.01)
            await _press(d, session_key, "spawn:abc", "1")
            return await task

        assert asyncio.run(_go()) is True


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
        try:
            info = mgr.spawn("do a thing", parent_session_key="telegram:k:direct:7")
            assert info is not None
            for _ in range(50):
                await asyncio.sleep(0)
            # The parent_trusted rung admitted the spawn — the approval callback (and
            # thus the channel prompt behind it) was never consulted.
            approval.assert_not_awaited()
        finally:
            for t in list(mgr._tasks.values()):
                t.cancel()
            await asyncio.sleep(0)
            # Construction opened the durable task queue (``tasks.db`` + ``-wal`` +
            # ``-shm``); cancelling the run tasks does not release it.
            mgr.close()


# ── (e) the destination is re-authorized at the instant of delivery ─────────


class TestTheDestinationIsReauthorizedBeforeTheSend:
    """A withdrawn destination gets no prompt, and the spawn still falls through.

    The gate holds a spawn for as long as its approval takes, so the authorization
    that admitted the originating turn says nothing about the moment the prompt is
    posted: an operator can drop the peer from the allow-list, or the Topic from the
    forum allow-list, inside that window. The prompt carries a task preview, so the
    send is re-decided here against the LIVE gates rather than the ones the turn
    started under. Every arm below asserts the same three things: no send, the armed
    nonce retired, and ``None`` (fall through) rather than ``False`` — nothing was
    surfaced, so there is no decision to report.
    """

    @pytest.fixture(autouse=True)
    def _short_prompt_wait(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Shrink the deny-by-default wait so a REGRESSION fails fast.

        A refusal here returns before the prompt is ever awaited, so this fixture
        does not touch the path under test. It bounds the OTHER outcome: code that
        posts the prompt anyway would park every assertion below on
        ``_APPROVAL_TIMEOUT_S`` (minutes) and surface as a suite-wide timeout
        instead of a failed assertion.
        """
        import kiro_crew.telegram.renderer as renderer_mod

        monkeypatch.setattr(renderer_mod, "_APPROVAL_TIMEOUT_S", 0.2)

    def test_a_peer_dropped_from_the_roster_gets_no_prompt(self) -> None:
        d, cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))
        # Revocation lands after the turn that asked for the spawn: the applier
        # pushes a reloaded allow-list at the live dispatcher.
        d._allowed.clear()

        result = asyncio.run(d.deliver_spawn_approval("spawn:abc", "spawn_run(build)", session_key))

        assert result is None
        assert cli.sent == []
        key = TelegramApprovalDecider.key(session_key, "spawn:abc")
        assert key not in TelegramApprovalDecider._NONCES
        assert key not in TelegramApprovalDecider._REGISTRY

    def test_a_topic_dropped_from_the_forum_allow_list_gets_no_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chat_id, thread = -1001234567890, 42
        d, cli, _sess = _dispatcher({7}, allow_forum=True, allowed_forum_chat_ids=[chat_id])
        session_key = d._session_key((CHAT_TYPE_FORUM, f"{chat_id}:{thread}"))
        # The Topic's supergroup leaves the forum allow-list mid-window. The gate
        # reads the config at point of use, which is what makes that observable.
        revoked = _cfg(allow_forum=True, allowed_forum_chat_ids=[])
        _prime_live(revoked)
        monkeypatch.setattr(d, "_live_cfg", lambda: revoked)

        result = asyncio.run(d.deliver_spawn_approval("spawn:abc", "spawn_run(build)", session_key))

        assert result is None
        assert cli.sent == []
        key = TelegramApprovalDecider.key(session_key, "spawn:abc")
        assert key not in TelegramApprovalDecider._NONCES

    def test_a_transport_egress_denial_gets_no_prompt(self) -> None:
        # The roster still holds the peer, so ONLY the transport's own
        # revocation-at-egress decision can refuse here.
        d, cli, _sess = _dispatcher({7})
        d.transport = SimpleNamespace(may_send_to=lambda _c, _t=None, **_k: False)  # type: ignore[assignment]
        session_key = d._session_key(("direct", "7"))

        result = asyncio.run(d.deliver_spawn_approval("spawn:abc", "spawn_run(build)", session_key))

        assert result is None
        assert cli.sent == []

    def test_a_raising_egress_gate_is_read_as_revoked(self) -> None:
        d, cli, _sess = _dispatcher({7})

        def _boom(_conversation, _thread=None, **_k):  # type: ignore[no-untyped-def]
            raise RuntimeError("roster store unreadable")

        d.transport = SimpleNamespace(may_send_to=_boom)  # type: ignore[assignment]
        session_key = d._session_key(("direct", "7"))

        # Fail closed: an unanswerable gate at an egress boundary is a denial.
        result = asyncio.run(d.deliver_spawn_approval("spawn:abc", "spawn_run(build)", session_key))

        assert result is None
        assert cli.sent == []

    @pytest.mark.usefixtures("_press_lands_inside_the_window")
    def test_an_authorized_destination_still_posts_through_the_transport_gate(self) -> None:
        # The gate is consulted with the destination it will actually send to, and a
        # grant is not turned into a refusal.
        d, cli, _sess = _dispatcher({7})
        seen: list[tuple[str, str | None]] = []

        def _gate(conversation, thread=None, **_k):  # type: ignore[no-untyped-def]
            seen.append((conversation, thread))
            return True

        d.transport = SimpleNamespace(may_send_to=_gate)  # type: ignore[assignment]
        session_key = d._session_key(("direct", "7"))

        async def _go() -> bool | None:
            task = asyncio.ensure_future(
                d.deliver_spawn_approval("spawn:abc", "spawn_run(build)", session_key)
            )
            await asyncio.sleep(0)
            await _press(d, session_key, "spawn:abc", "1")
            return await task

        assert asyncio.run(_go()) is True
        assert len(cli.sent) == 1
        assert seen == [("7", None)]

    @pytest.mark.usefixtures("_press_lands_inside_the_window")
    def test_a_press_slower_than_the_refusal_bound_still_resolves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Price the press path's governance hop past the class's 0.2s refusal
        # bound, as a loaded runner's executor does. The arm that expects a press
        # must still read it, rather than time out and report a denial.
        import time

        import kiro_crew.messaging.identity as identity_mod

        real = identity_mod._channel_inbound_permitted_sync

        def _slow(channel_type: str) -> bool:
            time.sleep(0.3)
            return bool(real(channel_type))

        monkeypatch.setattr(identity_mod, "_channel_inbound_permitted_sync", _slow)
        d, cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))

        async def _go() -> bool | None:
            task = asyncio.ensure_future(
                d.deliver_spawn_approval("spawn:abc", "spawn_run(build)", session_key)
            )
            await _press(d, session_key, "spawn:abc", "1")
            return await asyncio.wait_for(task, 10.0)

        assert asyncio.run(_go()) is True
        assert len(cli.sent) == 1


class TestAnOverlappingRestartKeepsItsReplacementHook:
    """A stale close must not unregister the hook that replaced it."""

    def test_a_stale_close_leaves_the_replacement_registered(self) -> None:
        async def _old(_rid: str, _desc: str, _parent: str) -> bool:
            return True

        async def _new(_rid: str, _desc: str, _parent: str) -> bool:
            return False

        seam.register_channel_delivery("telegram", _old)
        # A restart registers the replacement before the OLD client's close runs.
        seam.register_channel_delivery("telegram", _new)
        seam.unregister_channel_delivery("telegram", _old)

        assert seam.resolve_channel_delivery("telegram:k:direct:7") is _new

    def test_the_owning_close_still_drops_its_own_hook(self) -> None:
        async def _hook(_rid: str, _desc: str, _parent: str) -> bool:
            return True

        seam.register_channel_delivery("telegram", _hook)
        seam.unregister_channel_delivery("telegram", _hook)

        assert seam.resolve_channel_delivery("telegram:k:direct:7") is None

    def test_a_bound_method_registration_is_matched_by_equality(self) -> None:
        # The real caller registers ``dispatcher.deliver_spawn_approval``, and each
        # attribute access mints a NEW bound-method object — so the comparison has to
        # be equality, not identity, or a channel could never drop its own hook.
        d, _cli, _sess = _dispatcher({7})
        seam.register_channel_delivery("telegram", d.deliver_spawn_approval)
        seam.unregister_channel_delivery("telegram", d.deliver_spawn_approval)

        assert seam.resolve_channel_delivery("telegram:k:direct:7") is None

    def test_a_close_from_a_different_dispatcher_is_a_no_op(self) -> None:
        live, _cli, _sess = _dispatcher({7})
        gone, _cli2, _sess2 = _dispatcher({7})
        seam.register_channel_delivery("telegram", live.deliver_spawn_approval)

        # The OTHER dispatcher's close: same method, different instance.
        seam.unregister_channel_delivery("telegram", gone.deliver_spawn_approval)

        assert seam.resolve_channel_delivery("telegram:k:direct:7") == live.deliver_spawn_approval


class TestTheOriginatingTurnsSweepSparesAnInFlightSpawnPrompt:
    """Admission runs this gate in a task of its own, so the turn that asked for the
    spawn returns as soon as the spawn is admitted -- and its end-of-turn sweep can
    land while the gate is still inside the post. The decision window is armed
    before that post, and the gate's own wait has not started yet, so nothing in the
    sweep's view distinguishes this window from a tool prompt whose turn died. The
    gate declares the wait detached when it arms, which is what keeps the sweep off
    a prompt the operator is looking at.
    """

    def test_a_sweep_during_the_post_leaves_the_prompt_answerable(self) -> None:
        d, _cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))
        assert d.client is not None
        real_send = d.client.send_message

        async def _send_while_the_turn_ends(*a, **k):  # type: ignore[no-untyped-def]
            TelegramApprovalDecider.discard_session(session_key)
            return await real_send(*a, **k)

        d.client.send_message = _send_while_the_turn_ends  # type: ignore[method-assign]

        async def _go() -> bool:
            task = asyncio.ensure_future(
                d.deliver_spawn_approval("spawn:abc", "spawn_run(build)", session_key)
            )
            await asyncio.sleep(0)
            key = TelegramApprovalDecider.key(session_key, "spawn:abc")
            # The sweep has already run, inside the send above.
            assert key in TelegramApprovalDecider._NONCES, "the sweep retired the nonce"
            await _press(d, session_key, "spawn:abc", "1")
            return bool(await task)

        assert asyncio.run(_go()) is True


# ── Characterization: the delivery hook's exact shape and every fall-through ──


def _clear_windows() -> None:
    TelegramApprovalDecider._REGISTRY.clear()
    TelegramApprovalDecider._NONCES.clear()
    TelegramApprovalDecider._AWAITED.clear()


def _no_window_left(key: str) -> bool:
    return (
        key not in TelegramApprovalDecider._REGISTRY
        and key not in TelegramApprovalDecider._NONCES
        and key not in TelegramApprovalDecider._AWAITED
    )


class TestTheDeliveryHookCharacterization:
    """What the Telegram hook posts, when it arms, and every path that falls through."""

    @pytest.fixture(autouse=True)
    def _short_prompt_wait(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        import kiro_crew.telegram.renderer as renderer_mod

        monkeypatch.setattr(renderer_mod, "_APPROVAL_TIMEOUT_S", 0.2)
        _clear_windows()
        yield
        _clear_windows()

    def test_a_missing_client_arms_nothing(self) -> None:
        d, _cli, _sess = _dispatcher({7})
        d.client = None
        key = d._session_key(("direct", "7"))
        assert asyncio.run(d.deliver_spawn_approval("spawn:abc", "spawn_run(x)", key)) is None
        assert TelegramApprovalDecider._NONCES == {}
        assert TelegramApprovalDecider._REGISTRY == {} and TelegramApprovalDecider._AWAITED == set()

    @pytest.mark.parametrize(
        "parent",
        [
            "",
            "telegram:kirocrew",
            "telegram:kirocrew:direct:abc",
            "telegram:kirocrew:forum:-1001234567890",
            "telegram:kirocrew:group:7",
            "discord:kirocrew:direct:7",
            "dashboard:chat-1",
            "unified:kirocrew",
        ],
    )
    def test_an_unaddressable_parent_arms_nothing(self, parent: str) -> None:
        d, cli, _sess = _dispatcher({7})
        assert asyncio.run(d.deliver_spawn_approval("spawn:abc", "spawn_run(x)", parent)) is None
        assert cli.sent == [] and TelegramApprovalDecider._NONCES == {}

    @pytest.mark.parametrize(
        ("parent", "expected"),
        [
            ("telegram:kirocrew:direct:7", (7, None)),
            ("telegram:kirocrew:direct:7:gen3", (7, None)),
            ("telegram:kirocrew:forum:-1001234567890:42", (-1001234567890, 42)),
            ("telegram:kirocrew:forum:-1001234567890:42:gen1", (-1001234567890, 42)),
            ("telegram:kirocrew:forum:-1001234567890:42:9", (-1001234567890, 42)),
            ("telegram:kirocrew:forum:-1001234567890", None),
            ("telegram:kirocrew:direct:7:8", None),
            ("telegram:kirocrew:direct:abc", None),
            ("telegram:kirocrew:forum:x:42", None),
            ("telegram:kirocrew:group:7", None),
            ("telegram:kirocrew:direct:", None),
            ("unified:kirocrew", None),
            ("discord:kirocrew:direct:7", None),
            ("", None),
        ],
    )
    def test_the_chat_target_table(self, parent: str, expected: Any) -> None:
        d, _cli, _sess = _dispatcher({7})
        target = d._spawn_chat_target(parent)
        assert target == (None if expected is None else (*expected, parent))

    def test_the_prompt_body_keyboard_and_kwargs_are_exact(self) -> None:
        d, cli, _sess = _dispatcher({7})
        assert d.client is not None
        calls: list[tuple[Any, ...]] = []
        real_send = d.client.send_message

        async def _record(chat_id: int, text: str, **kw: Any) -> Any:
            key = TelegramApprovalDecider.key("telegram:kirocrew:direct:7", "spawn:abc")
            calls.append((chat_id, text, dict(kw), TelegramApprovalDecider._NONCES.get(key)))
            return await real_send(chat_id, text, **kw)

        d.client.send_message = _record  # type: ignore[method-assign]
        asyncio.run(
            d.deliver_spawn_approval(
                "spawn:abc", "spawn_run(<b>\"x\"</b>\n\t& 'y')", "telegram:kirocrew:direct:7"
            )
        )
        chat_id, text, kw, nonce = calls[0]
        assert chat_id == 7
        assert text == (
            "🔐 Approve sub-agent spawn?\n"
            "<pre>spawn_run(&lt;b&gt;&quot;x&quot;&lt;/b&gt; &amp; &#x27;y&#x27;)</pre>"
        )
        assert kw == {
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {"text": "✅ Approve", "callback_data": f"a:spawn:abc:{nonce}:1"},
                        {"text": "🚫 Deny", "callback_data": f"a:spawn:abc:{nonce}:0"},
                    ],
                    [
                        {
                            "text": "🤝 Trust this conversation",
                            "callback_data": f"a:spawn:abc:{nonce}:t",
                        }
                    ],
                ]
            },
            "message_thread_id": None,
        }

    @pytest.mark.parametrize(
        ("description", "detail"),
        [
            ("", "spawn_run"),
            (None, "spawn_run"),
            ("  \n ", ""),
            ("spawn_run(t=[x])", "spawn_run(t=[x])"),
        ],
    )
    def test_the_detail_defaults_and_collapses(self, description: Any, detail: str) -> None:
        d, cli, _sess = _dispatcher({7})
        asyncio.run(
            d.deliver_spawn_approval("spawn:abc", description, "telegram:kirocrew:direct:7")
        )
        assert cli.sent[0][0] == f"🔐 Approve sub-agent spawn?\n<pre>{detail}</pre>"

    def test_the_window_is_armed_detached_before_the_send(self) -> None:
        d, _cli, _sess = _dispatcher({7})
        assert d.client is not None
        key = TelegramApprovalDecider.key("telegram:kirocrew:direct:7", "spawn:abc")
        seen: list[tuple[bool, bool, bool]] = []
        real_send = d.client.send_message

        async def _record(chat_id: int, text: str, **kw: Any) -> Any:
            seen.append(
                (
                    key in TelegramApprovalDecider._NONCES,
                    key in TelegramApprovalDecider._REGISTRY,
                    key in TelegramApprovalDecider._AWAITED,
                )
            )
            return await real_send(chat_id, text, **kw)

        d.client.send_message = _record  # type: ignore[method-assign]
        asyncio.run(d.deliver_spawn_approval("spawn:abc", "d", "telegram:kirocrew:direct:7"))
        assert seen == [(True, True, True)]
        assert _no_window_left(key)

    def test_the_destination_is_checked_after_the_arm(self) -> None:
        d, cli, _sess = _dispatcher({7})
        key = TelegramApprovalDecider.key("telegram:kirocrew:direct:7", "spawn:abc")
        seen: list[tuple[Any, ...]] = []

        def _check(chat_id: int, thread_id: Any) -> bool:
            seen.append((chat_id, thread_id, key in TelegramApprovalDecider._NONCES))
            return False

        d._spawn_prompt_destination_permitted = _check  # type: ignore[method-assign]
        result = asyncio.run(
            d.deliver_spawn_approval("spawn:abc", "d", "telegram:kirocrew:direct:7")
        )
        assert result is None and seen == [(7, None, True)] and cli.sent == []
        assert _no_window_left(key)

    @pytest.mark.parametrize("outcome", ["raises", "none", "zero"])
    def test_a_failed_send_retires_the_window(self, outcome: str) -> None:
        d, _cli, _sess = _dispatcher({7})
        assert d.client is not None
        key = TelegramApprovalDecider.key("telegram:kirocrew:direct:7", "spawn:abc")

        async def _send(chat_id: int, text: str, **kw: Any) -> Any:
            if outcome == "raises":
                raise OSError("bot api down")
            return None if outcome == "none" else 0

        d.client.send_message = _send  # type: ignore[method-assign]
        result = asyncio.run(
            d.deliver_spawn_approval("spawn:abc", "d", "telegram:kirocrew:direct:7")
        )
        assert result is None
        assert _no_window_left(key)

    def test_a_cancel_during_the_send_retires_and_reraises(self) -> None:
        d, cli, _sess = _dispatcher({7})
        assert d.client is not None
        key = TelegramApprovalDecider.key("telegram:kirocrew:direct:7", "spawn:abc")
        entered = asyncio.Event()

        async def _hang(chat_id: int, text: str, **kw: Any) -> Any:
            entered.set()
            await asyncio.Event().wait()

        d.client.send_message = _hang  # type: ignore[method-assign]

        async def _go() -> None:
            task = asyncio.ensure_future(
                d.deliver_spawn_approval("spawn:abc", "d", "telegram:kirocrew:direct:7")
            )
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(_go())
        assert _no_window_left(key) and cli.sent == []

    def test_the_decider_waits_on_the_parent_key_verbatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[tuple[Any, ...]] = []

        async def _decide(self: Any, event: Any) -> Any:
            captured.append((self._session_key, getattr(event, "request_id", None)))
            return 1

        monkeypatch.setattr(TelegramApprovalDecider, "__call__", _decide)
        d, _cli, _sess = _dispatcher({7})
        parent = "telegram:kirocrew:direct:7:gen2"
        assert asyncio.run(d.deliver_spawn_approval("spawn:abc", "d", parent)) is True
        assert captured == [(parent, "spawn:abc")]

    def test_an_elapsed_wait_returns_the_seams_answer_verbatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[tuple[str, str]] = []

        async def _answer(channel: str, rid: str) -> bool:
            asked.append((channel, rid))
            return True

        monkeypatch.setattr("kiro_crew.telegram.transport_dispatch.unpressed_wait_answer", _answer)
        d, _cli, _sess = _dispatcher({7})
        reads: list[tuple[Any, Any]] = []
        real_check = d._spawn_prompt_destination_permitted

        def _count(chat_id: int, thread_id: Any) -> bool:
            reads.append((chat_id, thread_id))
            return real_check(chat_id, thread_id)

        d._spawn_prompt_destination_permitted = _count  # type: ignore[method-assign]
        result = asyncio.run(
            d.deliver_spawn_approval("spawn:abc", "d", "telegram:kirocrew:direct:7")
        )
        assert result is True and asked == [("telegram", "spawn:abc")]
        assert reads == [(7, None), (7, None)]

    def test_egress_revoked_during_the_wait_falls_through(self) -> None:
        d, cli, _sess = _dispatcher({7})
        calls = {"n": 0}

        def _gate(chat: str, thread: Any) -> bool:
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("egress state unreadable")
            return True

        d.transport = SimpleNamespace(may_send_to=_gate)  # type: ignore[assignment]
        result = asyncio.run(
            d.deliver_spawn_approval("spawn:abc", "d", "telegram:kirocrew:direct:7")
        )
        assert result is None and len(cli.sent) == 1

    def test_the_destination_table(self) -> None:
        d, _cli, _sess = _dispatcher({7})
        assert d._spawn_prompt_destination_permitted(7, None) is True
        assert d._spawn_prompt_destination_permitted(8, None) is False
        assert d._spawn_prompt_destination_permitted(0, None) is False
        d._allowed.clear()
        assert d._spawn_prompt_destination_permitted(7, None) is False

        d, _cli, _sess = _dispatcher({7}, allow_forum=True, allowed_forum_chat_ids=[-1001234567890])
        assert d._spawn_prompt_destination_permitted(-1001234567890, 42) is True
        assert d._spawn_prompt_destination_permitted(-1009999999999, 42) is False
        d._allowed.clear()
        assert d._spawn_prompt_destination_permitted(-1001234567890, 42) is False

    def test_the_egress_gate_table(self) -> None:
        d, _cli, _sess = _dispatcher({7}, allow_forum=True, allowed_forum_chat_ids=[-1001234567890])
        seen: list[tuple[Any, Any]] = []

        def _answer(value: Any):  # type: ignore[no-untyped-def]
            def _gate(chat: str, thread: Any) -> Any:
                seen.append((chat, thread))
                return value

            return _gate

        d.transport = SimpleNamespace()  # type: ignore[assignment]
        assert d._spawn_prompt_destination_permitted(7, None) is True
        d.transport = SimpleNamespace(may_send_to=_answer(0))  # type: ignore[assignment]
        assert d._spawn_prompt_destination_permitted(7, None) is False
        d.transport = SimpleNamespace(may_send_to=_answer("yes"))  # type: ignore[assignment]
        assert d._spawn_prompt_destination_permitted(-1001234567890, 42) is True
        assert seen == [("7", None), ("-1001234567890", "42")]
        seen.clear()
        assert d._spawn_prompt_destination_permitted(8, None) is False
        assert seen == [], "a roster denial never asks the transport"
