"""Telegram dispatcher: the inbound turn, inline-button callbacks and the /model picker.

Covers ``TelegramDispatcher`` (transport_dispatch.py, dispatch/callbacks.py,
dispatch/pickers.py): the channel-governance gate on messages and callbacks, a full turn
from claim to release, shutdown refusals, error text, /new, /help and /compact, how a
mid-turn message chooses between the running turn and the queue, the order an
inline-button press is judged in, and the button-only /model picker with the exact
outcome of every branch.

The five classes share one module because three of them call another class's helper by
name: ``TestQueueOrSteerDecision`` and ``TestCallbackRouting`` build their button presses
with ``TestDispatcher._option_callback``, and ``TestModelPickerOutcomes`` and
``TestCallbackRouting`` build their dispatcher with ``TestModelPicker()._with_models``.
Importing a test class into another module would collect its tests twice.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from telegram_helpers import _drop_live_config_snapshot  # noqa: F401
from telegram_helpers import (
    FakeProvider,
    FakeSessions,
    _dispatcher,
    _dm,
    _origin,
    _press,
    _prime_live,
    _tg_origin,
)

from kiro_crew.acp.client import AcpError
from kiro_crew.messaging.queue_drain import person_tag
from kiro_crew.messaging.renderer import session_provenance_tag
from kiro_crew.messaging.session_resume import RoutingDecision
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.telegram.renderer import TelegramApprovalDecider
from kiro_crew.telegram.transport import TelegramInboundMessage
from kiro_crew.telegram.transport_dispatch import _MODEL_PICKER_TTL_SECS


def _deny_channel_profile(monkeypatch, tmp_path, allow=("slack",)):
    """Point the ProfileStore at a host profile that allows only ``allow`` — so
    any other channel is denied by the inbound gate. Returns nothing; resets the
    store so the next resolve sees the profile."""
    import json

    from kiro_crew.platform import governance_profiles as gp

    pdir = tmp_path / "profiles"
    pdir.mkdir(exist_ok=True)
    monkeypatch.setattr(gp, "_PROFILES_DIR", pdir)
    gp.reset_store()
    (pdir / "host.json").write_text(
        json.dumps(
            {
                "name": "host",
                "bind": {"type": "surface", "id": "host"},
                "channels": {"members": {"mode": "allow", "allow": list(allow)}},
            }
        )
    )


class TestDispatcher:
    def test_channels_deny_drops_inbound_message(self, tmp_path, monkeypatch) -> None:
        # A channels DENY must stop handle_message from driving a turn. This
        # locks the Telegram inbound chokepoint — removing the gate makes this
        # test fail (a turn would run).
        from kiro_crew.platform import governance_profiles as gp

        _deny_channel_profile(monkeypatch, tmp_path)
        d, cli, sess = _dispatcher({7})
        try:
            asyncio.run(
                d.handle_message(
                    InboundMessage(
                        channel_type="telegram", user_id="7", conversation_id="7", text="hello"
                    )
                )
            )
            assert cli.final_text() in (None, "")
            assert sess.successes == []
        finally:
            gp.reset_store()

    def test_channels_deny_drops_callback_approval(self, tmp_path, monkeypatch) -> None:
        # A callback press must not resolve a pending tool approval on a denied
        # channel. This locks the on_callback gate.
        from kiro_crew.platform import governance_profiles as gp

        _deny_channel_profile(monkeypatch, tmp_path)
        d, cli, _ = _dispatcher({7})

        async def _go() -> bool:
            key = TelegramApprovalDecider.key(d._session_key(("direct", "7")), "rq1")
            fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            TelegramApprovalDecider._REGISTRY[key] = fut
            # The nonce the renderer would have minted for this prompt; the press
            # must carry it or resolution refuses it as stale.
            nonce = "n1"
            TelegramApprovalDecider.arm(key, nonce)
            try:
                cb = SimpleNamespace(
                    callback_query_id="q1",
                    user_id=7,
                    chat_id=7,
                    message_id=100,
                    data=f"a:rq1:{nonce}:1",
                    label="",
                    chat_type="private",
                )
                await d.on_callback(cb)  # type: ignore[arg-type]
                return fut.done()
            finally:
                TelegramApprovalDecider._REGISTRY.pop(key, None)

        try:
            assert asyncio.run(_go()) is False, "denied channel must not resolve the tool approval"
        finally:
            gp.reset_store()

    def test_channels_deny_still_resolves_callback_reject(self, tmp_path, monkeypatch) -> None:
        # A REJECT callback ("a:...:0") on a denied channel must STILL resolve the
        # pending approval as refused (False) — a reject is a denial, and dropping
        # it would strand the pending future until timeout. Only APPROVE is gated
        # out.
        from kiro_crew.platform import governance_profiles as gp

        _deny_channel_profile(monkeypatch, tmp_path)
        d, cli, _ = _dispatcher({7})

        async def _go() -> "tuple[bool, bool]":
            key = TelegramApprovalDecider.key(d._session_key(("direct", "7")), "rq1")
            fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            TelegramApprovalDecider._REGISTRY[key] = fut
            TelegramApprovalDecider.arm(key, "n1")
            try:
                cb = SimpleNamespace(
                    callback_query_id="q1",
                    user_id=7,
                    chat_id=7,
                    message_id=100,
                    data="a:rq1:n1:0",  # reject (flag 0)
                    label="",
                    chat_type="private",
                )
                await d.on_callback(cb)  # type: ignore[arg-type]
                return fut.done(), (fut.result() if fut.done() else True)
            finally:
                TelegramApprovalDecider._REGISTRY.pop(key, None)

        try:
            done, result = asyncio.run(_go())
            assert (
                done and result is False
            ), "a reject on a denied channel must resolve the approval as refused"
        finally:
            gp.reset_store()

    def test_full_turn_records_success_and_releases(self) -> None:
        d, cli, sess = _dispatcher({7})

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="hello world"
                )
            )

        asyncio.run(_go())
        assert cli.final_text() == "Answer: hello world"
        assert sess.successes == ["telegram:kirocrew:direct:7"]
        assert sess.released == ["telegram:kirocrew:direct:7"]
        # Pins that the pre-dispatch closing gate is consulted on the normal
        # path, so it cannot be dropped or renamed into a no-op unnoticed.
        assert sess.begin_turns == 1

    def test_a_shutdown_between_the_claim_and_the_dispatch_never_opens_the_turn(self) -> None:
        """The lease-dispatch race gate.

        ``get_or_create`` guards the CLAIM, but the turn only opens at
        ``driver.run``, and the context build between them is wide enough for a
        gateway restart to land in. Opening a turn then registers it behind the
        drain snapshot ``close_all`` has already taken, so it is killed
        mid-flight holding its native lock and reaches the user as an empty
        response instead of this channel's notice.
        """
        d, cli, sess = _dispatcher({7})
        # get_or_create deliberately ignores `closing`, so the CLAIM still
        # succeeds here. That is the race being pinned: a refused claim was
        # always handled, an accepted claim whose DISPATCH loses was not.
        sess.closing = True

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="hello world"
                )
            )

        asyncio.run(_go())

        assert "Answer: hello world" not in (
            cli.final_text() or ""
        ), "the turn must not open behind close_all's drain snapshot"
        assert sess.begin_turns == 1
        # A restart is neither a success nor a session fault: charging it to the
        # circuit breaker would count toward resetting a session that never
        # misbehaved.
        assert sess.successes == []
        assert sess.failures == []
        # Refused is not leaked -- the session-keyed semaphore still comes back.
        assert sess.released == ["telegram:kirocrew:direct:7"]

    def test_a_shutdown_refusal_is_spooled_for_a_persistent_session(
        self, tmp_path, monkeypatch
    ) -> None:
        """The durable inbound spool receives the refused message."""
        from kiro_crew.messaging import inbound_spool as S

        monkeypatch.setattr(S, "data_home", lambda: tmp_path)
        d, _cli, sess = _dispatcher({7})
        sess.closing = True

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="keep me"
                )
            )

        asyncio.run(_go())

        spool = tmp_path / "inbound-spool" / "refused.jsonl"
        assert spool.exists() and "keep me" in spool.read_text(encoding="utf-8")

    def test_a_shutdown_refusal_is_not_spooled_for_a_restricted_session(
        self, tmp_path, monkeypatch
    ) -> None:
        """``/incognito`` is a promise that nothing persists, and the spool is a file.

        RED-BEFORE: without the restricted-session gate at the refusal point the
        private message is written verbatim to ``refused.jsonl``. The same
        predicate that gates the durable-history write gates this one.
        """
        from kiro_crew.messaging import inbound_spool as S

        monkeypatch.setattr(S, "data_home", lambda: tmp_path)
        d, _cli, sess = _dispatcher({7})
        sess.closing = True
        sess.reserve_inbound_callback = lambda: None

        d._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:restricted")
        )

        async def _restricted(key: str) -> bool:
            return key == "dashboard:restricted"

        monkeypatch.setattr(d, "_session_restricted", _restricted)

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="my secret"
                )
            )

        asyncio.run(_go())

        spool = tmp_path / "inbound-spool" / "refused.jsonl"
        assert not spool.exists(), "an incognito message was persisted to the spool"
        assert sess.released == [], "paused admission must not acquire or release a session"

    def test_agent_resolves_to_kirocrew_when_unset(self) -> None:
        # agent=None + empty default_agent must fall back to "kirocrew" so the
        # session loads kirocrew-core (spawn_run), not kiro-cli's bare default.
        d, cli, sess = _dispatcher({7})

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(channel_type="telegram", user_id="7", conversation_id="7", text="hi")
            )

        asyncio.run(_go())
        assert sess.last_agent == "kirocrew"

    def test_cold_start_failure_finalizes_and_skips_release(self) -> None:
        # If get_or_create raises (cold-start), the turn must still be finalized
        # (block streaming sends an error block, no silent dead turn) and the
        # semaphore must NOT be released (it was never acquired).
        d, cli, sess = _dispatcher({7}, raise_on_get=True)

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(channel_type="telegram", user_id="7", conversation_id="7", text="hi")
            )

        asyncio.run(_go())
        assert cli.sent and "Error" in cli.sent[-1][0]  # finalized by close()
        assert sess.released == []  # never acquired -> never released
        assert sess.failures == []  # not acquired -> not recorded as a failed turn

    def test_permanent_acp_error_reason_reaches_user(self) -> None:
        # A permanent AcpError (model entitlement) must surface its actionable
        # message, not the generic retry advice.
        msg = "Your account does not have access to model 'x'. Available: a, b."

        class _FailingProvider(FakeProvider):
            async def stream(self, message: str) -> Any:
                raise AcpError(msg, transient=False, user_worded=True)
                yield  # pragma: no cover — makes this an async generator

        class _Sessions(FakeSessions):
            async def get_or_create(self, key: str, **kw: Any) -> Any:
                return _FailingProvider(), True, False

        d, cli, sess = _dispatcher({7})
        d.sessions = _Sessions()  # type: ignore[assignment]

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(channel_type="telegram", user_id="7", conversation_id="7", text="hi")
            )

        asyncio.run(_go())
        final = cli.sent[-1][0]
        assert msg in final
        assert "please try again" not in final
        assert "\n" not in final  # single-line, bounded output

    def test_transient_acp_error_keeps_retry_text(self) -> None:
        class _FailingProvider(FakeProvider):
            async def stream(self, message: str) -> Any:
                raise AcpError("backend hit a transient error (HTTP 5xx)", transient=True)
                yield  # pragma: no cover

        class _Sessions(FakeSessions):
            async def get_or_create(self, key: str, **kw: Any) -> Any:
                return _FailingProvider(), True, False

        d, cli, sess = _dispatcher({7})
        d.sessions = _Sessions()  # type: ignore[assignment]

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(channel_type="telegram", user_id="7", conversation_id="7", text="hi")
            )

        asyncio.run(_go())
        assert "please try again" in cli.sent[-1][0]
        assert "5xx" not in cli.sent[-1][0]

    def test_non_acp_error_stays_generic_and_leaks_nothing(self) -> None:
        class _FailingProvider(FakeProvider):
            async def stream(self, message: str) -> Any:
                raise ValueError("secret internal detail /home/alice/.kiro/x")
                yield  # pragma: no cover

        class _Sessions(FakeSessions):
            async def get_or_create(self, key: str, **kw: Any) -> Any:
                return _FailingProvider(), True, False

        d, cli, sess = _dispatcher({7})
        d.sessions = _Sessions()  # type: ignore[assignment]

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(channel_type="telegram", user_id="7", conversation_id="7", text="hi")
            )

        asyncio.run(_go())
        final = cli.sent[-1][0]
        assert "please try again" in final
        assert "secret internal detail" not in final
        assert "/home/alice" not in final

    def test_new_command_bumps_gen_and_replies(self) -> None:
        d, cli, sess = _dispatcher({7})

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="/new"
                )
            )

        asyncio.run(_go())
        assert d._conv.current_gen(("direct", "7")) == 1
        assert "New conversation" in cli.sent[-1][0]
        assert sess.successes == []  # no turn ran

    def test_help_command_replies(self) -> None:
        d, cli, _ = _dispatcher({7})

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="/help"
                )
            )

        asyncio.run(_go())
        assert "Kiro Crew" in cli.sent[-1][0]

    def test_compact_refused_while_turn_running(self) -> None:
        # /compact must NOT drive the same provider while a turn streams. The
        # guard now atomically try_acquire()s the semaphore: if a turn holds it,
        # acquisition fails and we refuse — no acquire, no concurrent stream.
        d, cli, sess = _dispatcher({7})
        sess._busy = True  # simulate an in-flight turn holding the semaphore

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="/compact"
                )
            )

        asyncio.run(_go())
        assert any("try /compact" in s[0] for s in cli.sent)  # refused with notice
        assert not any("Compacting" in s[0] for s in cli.sent)  # never started
        assert sess.acquired == []  # semaphore never taken while busy

    def test_compact_when_idle_holds_and_releases_semaphore(self) -> None:
        # When idle, /compact atomically acquires the per-session semaphore for
        # the whole compaction (serializing against a normal turn), then always
        # releases it — so it can't interleave JSON-RPC on the shared provider.
        d, cli, sess = _dispatcher({7})

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="/compact"
                )
            )

        asyncio.run(_go())
        assert sess.acquired == ["telegram:kirocrew:direct:7"]  # acquired the turn semaphore
        assert sess.released == ["telegram:kirocrew:direct:7"]  # and released it in finally
        assert any("Compact" in s[0] for s in cli.sent) or any("Compact" in e[1] for e in cli.edits)

    def test_compact_declined_on_auto_managed_backend(self) -> None:
        # A backend that cannot serve /compact gets the informational reply and
        # compact() is NEVER dispatched.
        d, cli, sess = _dispatcher({7})
        calls: list[int] = []

        async def _compact(context: str = "") -> None:
            calls.append(1)

        sess._gp.compact = _compact
        sess._gp.manual_compact_unsupported_backend = "kas"

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="/compact"
                )
            )

        asyncio.run(_go())
        visible = " ".join([text for text, _ in cli.sent] + [text for _, text, _ in cli.edits])
        assert "manages compaction automatically" in visible
        assert calls == []
        assert sess.released == ["telegram:kirocrew:direct:7"]  # semaphore still handed back

    def test_compact_none_capability_preserves_dispatch(self) -> None:
        # The ABC's None (supported) default keeps the existing dispatch.
        d, cli, sess = _dispatcher({7})
        sess._gp.manual_compact_unsupported_backend = None

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="/compact"
                )
            )

        asyncio.run(_go())
        visible = " ".join([text for text, _ in cli.sent] + [text for _, text, _ in cli.edits])
        assert "Context compacted" in visible

    def test_compact_summary_body_is_not_sent(self) -> None:
        d, cli, sess = _dispatcher({7})

        async def _completed(timeout: float = 0.0) -> dict:
            return {"type": "completed", "summary": "## OBJECTIVE\ninternal guidance"}

        sess._gp.wait_for_compaction = _completed

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="/compact"
                )
            )

        asyncio.run(_go())
        visible = " ".join([text for text, _ in cli.sent] + [text for _, text, _ in cli.edits])
        assert "Context compacted" in visible
        assert "OBJECTIVE" not in visible and "internal guidance" not in visible

    def test_compact_timeout_reports_gracefully(self) -> None:
        # Regression: nested 120s timeouts made the graceful-timeout branch
        # unreachable and destroyed a healthy session. A compaction that yields
        # no terminal status must report a timeout and KEEP the session.
        d, cli, sess = _dispatcher({7})

        async def _timeout(timeout: float = 0.0) -> dict:
            return {"type": "timeout"}

        sess._gp.wait_for_compaction = _timeout

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="/compact"
                )
            )

        asyncio.run(_go())
        assert any("timed out" in s[0] for s in cli.sent) or any(
            "timed out" in e[1] for e in cli.edits
        )
        assert sess.destroyed == [] and sess.discarded == []  # healthy session preserved

    @staticmethod
    def _option_callback(data: str, label: str = "Say Hi") -> Any:
        return SimpleNamespace(
            callback_query_id="q1",
            user_id=7,
            chat_id=7,
            message_id=99,
            data=data,
            label=label,
            chat_type="private",
        )

    def test_callback_option_echoes_choice_and_redispatches(self) -> None:
        d, cli, sess = _dispatcher({7})
        tag = session_provenance_tag(d._session_key(("direct", "7")))

        async def _go() -> None:
            await d.on_callback(self._option_callback(f"opt:0:{tag}"))  # type: ignore[arg-type]

        asyncio.run(_go())
        # Tapping an option retires the keyboard on the original message WITHOUT
        # overwriting its text, echoes the picked choice as its own block, then
        # re-dispatches the choice so the answer arrives as a NEW message.
        assert cli.markup_edits[-1] == (99, {"inline_keyboard": []})
        assert all(mid != 99 for mid, _, _ in cli.edits)  # original text never clobbered
        assert "Say Hi" in cli.sent[0][0]  # choice echoed as its own block first
        assert cli.final_text() == "Answer: Say Hi"  # answer streamed as a NEW message
        assert sess.successes == ["telegram:kirocrew:direct:7"]

    def test_callback_option_label_is_literal_not_a_command(self) -> None:
        d, cli, sess = _dispatcher({7})
        route = ("direct", "7")
        tag = session_provenance_tag(d._session_key(route))
        before = d._conv.current_gen(route)

        asyncio.run(
            d.on_callback(  # type: ignore[arg-type]
                self._option_callback(f"opt:0:{tag}", label="/new")
            )
        )

        assert d._conv.current_gen(route) == before
        assert sess.successes == ["telegram:kirocrew:direct:7"]
        assert not any("New conversation started" in text for text, _ in cli.sent)
        assert "Answer: /new" in (cli.final_text() or "")

    def test_untagged_option_press_is_refused_fail_closed(self) -> None:
        d, cli, sess = _dispatcher({7})

        asyncio.run(d.on_callback(self._option_callback("opt:0")))  # type: ignore[arg-type]

        assert cli.markup_edits[-1] == (99, {"inline_keyboard": []})
        assert any("predate" in text for text, _ in cli.sent)
        assert not any("Say Hi" in text for text, _ in cli.sent)
        assert sess.successes == [] and sess.queued == [] and sess._gp.steered == []

    def test_pre_new_option_press_is_refused_before_busy_path(self) -> None:
        d, cli, sess = _dispatcher({7})
        route = ("direct", "7")
        old_tag = session_provenance_tag(d._session_key(route))
        asyncio.run(d.handle_message(_dm("/new")))
        cli.sent.clear()
        sess._busy = True

        asyncio.run(
            d.on_callback(  # type: ignore[arg-type]
                self._option_callback(f"opt:0:{old_tag}", label="Choice A")
            )
        )

        assert any("moved away" in text for text, _ in cli.sent)
        assert not any("busy" in text.lower() for text, _ in cli.sent)
        assert sess.successes == [] and sess.queued == [] and sess._gp.steered == []

    def test_option_press_after_agent_switch_is_refused(self) -> None:
        d, cli, sess = _dispatcher({7})
        route = ("direct", "7")
        old_tag = session_provenance_tag(d._session_key(route))
        d._agent_picks.set(route, "research-agent")

        asyncio.run(
            d.on_callback(  # type: ignore[arg-type]
                self._option_callback(f"opt:0:{old_tag}", label="Choice A")
            )
        )

        assert any("moved away" in text for text, _ in cli.sent)
        assert sess.successes == []

    def test_tagged_option_press_is_revalidated_after_idle_rotation(self) -> None:
        d, cli, sess = _dispatcher({7})
        route = ("direct", "7")
        tag = session_provenance_tag(d._session_key(route))

        def _rotate_now(*_args: Any, **_kwargs: Any) -> bool:
            d._conv.bump_gen(route)
            return True

        d._conv.maybe_rotate = _rotate_now  # type: ignore[method-assign]

        asyncio.run(
            d.on_callback(  # type: ignore[arg-type]
                self._option_callback(f"opt:0:{tag}", label="Choice A")
            )
        )

        assert any("moved away" in text for text, _ in cli.sent)
        assert sess.successes == []

    def test_valid_tagged_option_press_while_busy_is_not_queued_or_steered(self) -> None:
        d, cli, sess = _dispatcher({7})
        tag = session_provenance_tag(d._session_key(("direct", "7")))
        sess._busy = True

        asyncio.run(
            d.on_callback(  # type: ignore[arg-type]
                self._option_callback(f"opt:0:{tag}", label="Choice A")
            )
        )

        assert any("busy" in text.lower() and "NOT applied" in text for text, _ in cli.sent)
        assert sess.successes == [] and sess.queued == [] and sess._gp.steered == []

    def test_callback_approval_resolves_decider(self) -> None:
        d, cli, _ = _dispatcher({7})

        async def _go() -> bool:
            key = TelegramApprovalDecider.key(d._session_key(("direct", "7")), "rq9")
            fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            TelegramApprovalDecider._REGISTRY[key] = fut
            TelegramApprovalDecider.arm(key, "n1")
            cb = SimpleNamespace(
                callback_query_id="q2",
                user_id=7,
                chat_id=7,
                message_id=100,
                data="a:rq9:n1:1",
                label="",
                chat_type="private",
            )
            await d.on_callback(cb)  # type: ignore[arg-type]
            return fut.done() and fut.result() is True

        assert asyncio.run(_go()) is True

    def test_callback_approval_expired_shows_expired_not_approved(self) -> None:
        # Post-timeout: no pending future for the key (decider already denied by
        # default and popped it). An "Approve" press must NOT display "Approved".
        d, cli, _ = _dispatcher({7})
        cb = SimpleNamespace(
            callback_query_id="q5",
            user_id=7,
            chat_id=7,
            message_id=101,
            data="a:gone:1",
            label="",
            chat_type="private",
        )

        async def _go() -> None:
            await d.on_callback(cb)  # type: ignore[arg-type]

        asyncio.run(_go())
        assert cli.edits, "expected a verdict edit"
        assert "expired" in cli.edits[-1][1].lower()
        assert "Approved" not in cli.edits[-1][1]

    def test_callback_unauthorized_user_ignored(self) -> None:
        d, cli, _ = _dispatcher({7})
        cb = SimpleNamespace(
            callback_query_id="q3",
            user_id=999,
            chat_id=999,
            message_id=1,
            data="opt:0",
            label="X",
            chat_type="private",
        )

        async def _go() -> None:
            await d.on_callback(cb)  # type: ignore[arg-type]

        asyncio.run(_go())
        # Deny-by-default short-circuits BEFORE the ack: no Bot API round-trip
        # and no edit/redispatch for an unauthorized user.
        assert cli.answered == []
        assert cli.edits == []

    def test_callback_non_private_chat_ignored(self) -> None:
        # Defense-in-depth: even an allow-listed user's press is ignored if the
        # callback isn't from a private chat (mirrors the receive() guard).
        d, cli, _ = _dispatcher({7})
        cb = SimpleNamespace(
            callback_query_id="q4",
            user_id=7,
            chat_id=-100,
            message_id=1,
            data="opt:0",
            label="X",
            chat_type="group",
        )

        async def _go() -> None:
            await d.on_callback(cb)  # type: ignore[arg-type]

        asyncio.run(_go())
        assert cli.answered == []
        assert cli.edits == []


class TestModelPicker:
    """/model is button-only: the user picks from what the backend advertised."""

    _MODELS = [
        {"modelId": "claude-opus-5", "name": "Opus 5"},
        {"modelId": "gpt-5.6-sol", "name": "GPT 5.6 Sol"},
    ]

    def _with_models(self, models: list | None = None) -> Any:
        d, cli, sess = _dispatcher({7})
        sess._gp.advertised = list(self._MODELS if models is None else models)
        return d, cli, sess

    def test_posts_one_button_per_model_plus_auto(self) -> None:
        d, cli, _ = self._with_models()
        asyncio.run(d.handle_message(_dm("/model")))
        text, markup = cli.sent[-1]
        rows = markup["inline_keyboard"]
        assert [r[0]["callback_data"] for r in rows] == ["m:0", "m:1", "m:2"]
        assert "Auto" in rows[0][0]["text"]
        assert "Opus 5" in rows[1][0]["text"]
        assert "Current model:" in text

    def test_callback_data_stays_inside_the_64_byte_cap(self) -> None:
        # Telegram rejects callback_data over 64 bytes and real model ids run
        # long, which is why the button carries an index, never the id.
        long_id = "a" * 300
        d, cli, _ = self._with_models([{"modelId": long_id, "name": long_id}])
        asyncio.run(d.handle_message(_dm("/model")))
        for row in cli.sent[-1][1]["inline_keyboard"]:
            assert len(row[0]["callback_data"].encode()) <= 64

    def test_press_switches_the_live_session_and_stores_the_pick(self) -> None:
        d, cli, sess = self._with_models()
        asyncio.run(d.handle_message(_dm("/model")))
        picker_mid = cli.sent[-1] and 101
        asyncio.run(d.on_callback(_press("m:2", message_id=picker_mid)))
        assert sess._gp.set_models == ["gpt-5.6-sol"]
        assert d._model_pref[("direct", "7")] == "gpt-5.6-sol"
        assert "Now using gpt-5.6-sol" in cli.edits[-1][1]
        assert cli.edits[-1][2] == {"inline_keyboard": []}, "the keyboard must be retired"

    def test_pick_flows_into_the_next_session(self) -> None:
        # The stored preference is only useful if session creation consumes it.
        d, cli, sess = self._with_models()
        asyncio.run(d.handle_message(_dm("/model")))
        asyncio.run(d.on_callback(_press("m:2")))
        asyncio.run(d.handle_message(_dm("hi")))
        assert sess.last_model == "gpt-5.6-sol"

    def test_auto_is_recorded_without_a_wire_call(self) -> None:
        # There is no ACP id meaning "let the backend choose", so Auto can only
        # be recorded — claiming a live switch would be a lie.
        d, cli, sess = self._with_models()
        asyncio.run(d.handle_message(_dm("/model")))
        asyncio.run(d.on_callback(_press("m:0")))
        assert sess._gp.set_models == []
        assert d._model_pref[("direct", "7")] == ""
        # A session is live here, and get_or_create returns a reused session
        # before it reads `model=`, so the pick CANNOT land on the next message —
        # promising that would be the same lie in different words.
        assert "/new" in cli.edits[-1][1]
        assert "next message" not in cli.edits[-1][1]

    def test_auto_with_no_live_session_does_promise_the_next_message(self) -> None:
        # The mirror case: with nothing live, the next message is what creates
        # the session, so it really does consume the preference then.
        d, cli, sess = self._with_models()
        asyncio.run(d.handle_message(_dm("/model")))
        sess._has = False
        asyncio.run(d.on_callback(_press("m:0")))
        assert "next message" in cli.edits[-1][1]

    def test_auto_reaches_session_creation_as_none_not_empty_string(self) -> None:
        # Auto must mean "as if never picked". get_or_create gates its own model
        # resolution on `model is None`, so handing it the Auto row's stored ""
        # would skip that resolution and land on the provider factory's narrower
        # fallback — a different model than an untouched conversation gets.
        d, cli, sess = self._with_models()
        asyncio.run(d.handle_message(_dm("/model")))
        asyncio.run(d.on_callback(_press("m:0")))
        asyncio.run(d.handle_message(_dm("hi")))
        assert sess.last_model is None

    def test_failed_switch_does_not_claim_success(self) -> None:
        d, cli, sess = self._with_models()
        sess._gp.set_model_error = RuntimeError("model not available")
        asyncio.run(d.handle_message(_dm("/model")))
        asyncio.run(d.on_callback(_press("m:2")))
        outcome = cli.edits[-1][1]
        assert "Couldn't switch" in outcome and "Now using" not in outcome

    def test_busy_session_defers_instead_of_racing_the_turn(self) -> None:
        d, cli, sess = self._with_models()
        asyncio.run(d.handle_message(_dm("/model")))
        sess._busy = True  # try_acquire refuses -> no JSON-RPC on a live channel
        asyncio.run(d.on_callback(_press("m:2")))
        assert sess._gp.set_models == []
        assert "still running" in cli.edits[-1][1]
        assert d._model_pref[("direct", "7")] == "gpt-5.6-sol"

    def test_advertised_id_is_sent_verbatim(self) -> None:
        # An advertised id is already what this backend accepts. Routing it
        # through the canonical registry would REWRITE some ids into a different
        # model (model_registry.to_acp_id("opus-4.8-1m") == "claude-opus-4.8"),
        # so the picked id must reach set_model untouched.
        d, cli, sess = self._with_models([{"modelId": "opus-4.8-1m", "name": "Opus 4.8 (1M)"}])
        asyncio.run(d.handle_message(_dm("/model")))
        asyncio.run(d.on_callback(_press("m:1")))
        assert sess._gp.set_models == ["opus-4.8-1m"]

    def test_no_advertised_models_asks_for_a_message_first(self) -> None:
        d, cli, _ = self._with_models([])
        asyncio.run(d.handle_message(_dm("/model")))
        assert "send a message first" in cli.sent[-1][0]
        assert cli.sent[-1][1] is None, "no keyboard when there is nothing to pick"

    def test_expired_picker_refuses_to_act_on_a_stale_list(self) -> None:
        d, cli, sess = self._with_models()
        asyncio.run(d.handle_message(_dm("/model")))
        picker = d._model_pickers["7:101"]
        picker.created_at -= _MODEL_PICKER_TTL_SECS + 1
        asyncio.run(d.on_callback(_press("m:2")))
        assert sess._gp.set_models == []
        assert "no longer active" in cli.edits[-1][1]

    def test_unknown_picker_is_reported_not_ignored(self) -> None:
        d, cli, sess = self._with_models()
        asyncio.run(d.on_callback(_press("m:0", message_id=999)))
        assert sess._gp.set_models == []
        assert "no longer active" in cli.edits[-1][1]

    def test_out_of_range_index_cannot_pick_a_model(self) -> None:
        d, cli, sess = self._with_models()
        asyncio.run(d.handle_message(_dm("/model")))
        asyncio.run(d.on_callback(_press("m:99")))
        assert sess._gp.set_models == []
        assert "no longer active" in cli.edits[-1][1]

    def test_second_press_cannot_apply_twice(self) -> None:
        d, cli, sess = self._with_models()
        asyncio.run(d.handle_message(_dm("/model")))
        asyncio.run(d.on_callback(_press("m:2")))
        asyncio.run(d.on_callback(_press("m:2")))
        assert sess._gp.set_models == ["gpt-5.6-sol"], "the picker must be single-use"


class TestQueueOrSteerDecision:
    """How a mid-turn message chooses between the running turn and the queue."""

    _KEY = "telegram:kirocrew:direct:7"

    def test_the_steer_flag_is_read_per_message(self) -> None:
        d, _cli, sess = _dispatcher({7})
        sess._busy = True
        sess._gp.supports_steer = False  # type: ignore[misc]
        asyncio.run(d.handle_message(_dm("first")))
        assert [t for _ts, t, _kw in sess.queued] == ["first"] and sess._gp.steered == []

        sess._gp.supports_steer = True  # type: ignore[misc]
        asyncio.run(d.handle_message(_dm("second")))
        assert sess._gp.steered == ["second"]
        assert [t for _ts, t, _kw in sess.queued] == ["first"]

    def test_the_provider_is_fetched_per_message(self) -> None:
        d, _cli, sess = _dispatcher({7})
        sess._busy = True
        first = sess._gp
        asyncio.run(d.handle_message(_dm("one")))
        sess._gp = FakeProvider()
        asyncio.run(d.handle_message(_dm("two")))
        assert first.steered == ["one"] and sess._gp.steered == ["two"]

    def test_a_provider_without_a_liveness_probe_is_live(self) -> None:
        d, _cli, sess = _dispatcher({7})
        sess._busy = True
        steer = AsyncMock(return_value=True)
        sess._gp = SimpleNamespace(supports_steer=True, steer=steer)

        asyncio.run(d.handle_message(_dm("x")))

        assert steer.await_args.args == ("x",)
        assert sess.queued == []

    def test_a_provider_without_steer_queues(self) -> None:
        d, _cli, sess = _dispatcher({7})
        sess._busy = True
        sess._gp = SimpleNamespace(supports_steer=True, has_active_turn=lambda: True)

        asyncio.run(d.handle_message(_dm("x")))

        assert [t for _ts, t, _kw in sess.queued] == ["x"]

    def test_a_steer_override_on_a_dead_turn_queues_the_stripped_text(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess._busy = True
        d.cfg.messaging.queue_mode = "queue"
        _prime_live(d.cfg)
        sess._gp.active_turn = False

        asyncio.run(
            d.handle_message(
                TelegramInboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="7",
                    text="/steer now",
                    message_id=3,
                )
            )
        )

        assert sess._gp.steered == [] and cli.reactions == []
        assert [t for _ts, t, _kw in sess.queued] == ["now"]

    def test_an_attachment_caption_directive_is_queued_verbatim(self) -> None:
        d, _cli, sess = _dispatcher({7})
        sess._busy = True
        photos = [{"file_id": "p1", "file_name": "a.jpg", "mime_type": "image/jpeg"}]

        asyncio.run(
            d.handle_message(
                InboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="7",
                    text="/steer look at this",
                    attachments=photos,
                )
            )
        )

        assert sess._gp.steered == []
        assert sess.queued[0][1] == "/steer look at this"
        assert sess.queued[0][2]["attachments"] == photos

    def test_the_busy_path_receives_the_stripped_text_and_its_request(self) -> None:
        from unittest.mock import call

        d, _cli, sess = _dispatcher({7})
        sess._busy = True
        d._handle_busy = AsyncMock()  # type: ignore[method-assign]
        steer = TelegramInboundMessage(
            channel_type="telegram",
            user_id="7",
            conversation_id="7",
            text="/steer stop now",
            message_id=12,
        )
        modified = TelegramInboundMessage(
            channel_type="telegram",
            user_id="7",
            conversation_id="7",
            text="/incognito and also this",
            message_id=13,
        )

        asyncio.run(d.handle_message(steer))
        asyncio.run(d.handle_message(modified))

        assert d._handle_busy.await_args_list == [
            call(
                self._KEY, steer, "stop now", "steer", thread=None, privacy_request="", caller="7"
            ),
            call(
                self._KEY,
                modified,
                "and also this",
                None,
                thread=None,
                privacy_request="incognito",
                caller="7",
            ),
        ]

    def test_an_enqueue_miss_reruns_the_original_message(self) -> None:
        from kiro_crew.messaging import privacy_mode

        privacy_mode.reset()
        try:
            d, cli, sess = _dispatcher({7})
            d.cfg.messaging.queue_mode = "queue"
            _prime_live(d.cfg)
            sess._busy = True

            def _late(key: str, ts: str, text: str, *, force: bool = False, **kw: Any) -> bool:
                sess._busy = False
                return False

            sess.enqueue = _late  # type: ignore[method-assign]
            calls: list[tuple[Any, dict[str, Any]]] = []
            original = d.handle_message

            async def _record(msg: Any, **kw: Any) -> None:
                calls.append((msg, kw))
                await original(msg, **kw)

            d.handle_message = _record  # type: ignore[method-assign]
            msg = _dm("/temporary later")

            asyncio.run(d.handle_message(msg))

            assert len(calls) == 2
            assert calls[1][0] is msg and calls[1][1] == {}
            assert not any("Queued" in t for t, _ in cli.sent)
            assert privacy_mode.is_temporary(self._KEY)
            assert "Answer: later" in (cli.final_text() or "")
            assert sess.successes == [self._KEY]
        finally:
            privacy_mode.reset()

    def test_the_queue_path_enqueues_with_the_messages_own_origin(self) -> None:
        from unittest.mock import call

        d, _cli, sess = _dispatcher({7})
        d.cfg.messaging.queue_mode = "queue"
        _prime_live(d.cfg)
        sess._busy = True
        d._enqueue_with_receipt = AsyncMock(return_value=True)  # type: ignore[method-assign]
        photos = [{"file_id": "p1", "file_name": "a.jpg", "mime_type": "image/jpeg"}]
        with_photo = InboundMessage(
            channel_type="telegram",
            user_id="7",
            conversation_id="7",
            text="see",
            attachments=photos,
        )

        asyncio.run(d.handle_message(_dm("later")))
        asyncio.run(d.handle_message(with_photo))

        first, second = d._enqueue_with_receipt.await_args_list
        assert first == call(
            self._KEY,
            7,
            "later",
            thread=None,
            attachments=None,
            privacy_request="",
            origin=_tg_origin(7, 7),
            # The message's own flag rides to the entry (a test double's default).
            person_origin=False,
        )
        assert second.kwargs["attachments"] == photos
        assert second.kwargs["attachments"] is not with_photo.attachments

    def test_a_queued_entry_carries_its_payload_and_its_receipt(self) -> None:
        d, cli, sess = _dispatcher({7})
        d.cfg.messaging.queue_mode = "queue"
        _prime_live(d.cfg)
        sess._busy = True

        asyncio.run(d.handle_message(_dm("later")))

        assert sess.queued[0][1] == "later"
        assert sess.queued[0][2] == {
            "attachments": [],
            "privacy_request": "",
            **_origin(7, 7),
            **person_tag(False),
        }
        assert [t for t, _ in cli.sent] == ["⏳ Queued (1): “later”"]

    def test_a_busy_option_press_is_refused_byte_exact(self) -> None:
        from kiro_crew.telegram import transport_dispatch as td

        busy = (
            "🔘 That conversation is busy with another turn, so your choice was NOT "
            "applied. Type it as a message once the turn finishes."
        )
        assert td._BUSY_OPTIONS_REFUSAL == busy
        d, cli, sess = _dispatcher({7})
        tag = session_provenance_tag(d._session_key(("direct", "7")))
        sess._busy = True
        d._handle_busy = AsyncMock()  # type: ignore[method-assign]

        asyncio.run(
            d.on_callback(  # type: ignore[arg-type]
                TestDispatcher._option_callback(f"opt:0:{tag}", label="Choice A")
            )
        )

        assert [t for t, _ in cli.sent] == ["<blockquote>Choice A</blockquote>", busy]
        assert cli.markup_edits[-1] == (99, {"inline_keyboard": []})
        d._handle_busy.assert_not_awaited()
        assert sess.queued == [] and sess._gp.steered == []

    def test_a_press_staled_by_rotation_runs_nothing(self, monkeypatch: Any) -> None:
        from kiro_crew.messaging import privacy_mode
        from kiro_crew.telegram import transport_dispatch as td

        stale = (
            "🔘 These buttons belong to a conversation this chat has since moved away "
            "from, so your choice was NOT applied. Type it as a message instead."
        )
        assert td._STALE_OPTIONS_REFUSAL == stale
        d, cli, sess = _dispatcher({7})
        route = ("direct", "7")
        tag = session_provenance_tag(d._session_key(route))

        def _rotate_now(*_args: Any, **_kwargs: Any) -> bool:
            d._conv.bump_gen(route)
            return True

        d._conv.maybe_rotate = _rotate_now  # type: ignore[method-assign]
        hydrated: list[str] = []
        monkeypatch.setattr(privacy_mode, "hydrate", lambda _s, key: hydrated.append(key))
        apply = AsyncMock()
        monkeypatch.setattr(privacy_mode, "apply_mode", apply)

        asyncio.run(
            d.on_callback(  # type: ignore[arg-type]
                TestDispatcher._option_callback(f"opt:0:{tag}", label="Choice A")
            )
        )

        assert [t for t, _ in cli.sent] == ["<blockquote>Choice A</blockquote>", stale]
        assert hydrated == [] and apply.await_count == 0
        assert sess.begin_turns == 0 and sess.requested_models == {}
        assert d._active_renderers == {}
        assert d._conv.current_gen(route) == 1

    def test_an_untagged_press_is_refused_byte_exact(self) -> None:
        from kiro_crew.telegram import transport_dispatch as td

        untagged = (
            "🔘 These buttons predate a session-safety update, so which conversation "
            "they belong to cannot be verified and your choice was NOT applied. "
            "Type it as a message instead."
        )
        assert td._UNTAGGED_OPTIONS_REFUSAL == untagged
        d, cli, _sess = _dispatcher({7})

        asyncio.run(
            d.on_callback(TestDispatcher._option_callback("opt:0"))  # type: ignore[arg-type]
        )

        assert [t for t, _ in cli.sent] == [untagged]
        assert cli.markup_edits == [(99, {"inline_keyboard": []})]


def _facade_audits(monkeypatch: Any) -> list[dict[str, Any]]:
    """Capture every SEL row the dispatcher writes, through its own module binding."""
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "kiro_crew.telegram.transport_dispatch.sel",
        lambda: SimpleNamespace(log_api_access=lambda **kw: rows.append(kw)),
    )
    return rows


class TestModelPickerOutcomes:
    """The exact text, keyboard and side effects of every /model branch."""

    _KEY = "telegram:kirocrew:direct:7"
    _AUTO = "Auto (let the backend choose)"

    def _post(self, models: list | None = None) -> tuple[Any, Any, Any]:
        d, cli, sess = TestModelPicker()._with_models(models)
        asyncio.run(d.handle_message(_dm("/model")))
        return d, cli, sess

    def test_choices_filter_the_advertised_rows(self) -> None:
        d, _cli, sess = _dispatcher({7})
        sess._gp.advertised = [
            {"modelId": "auto", "name": "Auto"},
            "junk",
            None,
            {"modelId": ""},
            {"modelId": None},
            {"modelId": "  model-a  "},
            {"modelId": "model-b", "name": "Bee"},
        ]
        assert d._model_choices(self._KEY) == (
            ("", self._AUTO),
            ("model-a", "model-a"),
            ("model-b", "Bee"),
        )

    def test_the_cut_counts_the_auto_row(self) -> None:
        models = [{"modelId": f"model-{i:02d}", "name": f"M{i:02d}"} for i in range(30)]
        d, cli, _sess = self._post(models)
        rows = cli.sent[-1][1]["inline_keyboard"]
        assert len(rows) == 24
        assert rows[-1][0] == {"text": "M22", "callback_data": "m:23"}
        assert not any("not shown" in row[0]["text"] for row in rows)
        assert len(d._model_pickers["7:101"].choices) == 24

    def test_an_unusable_model_list_offers_nothing(self) -> None:
        for broken in ("not-callable", None):
            d, cli, sess = _dispatcher({7})
            if broken is None:
                sess.get_provider = lambda key: None  # type: ignore[method-assign]
            else:
                sess._gp.available_models = broken  # type: ignore[assignment]
            asyncio.run(d.handle_message(_dm("/model")))
            assert cli.sent[-1] == (
                "No model list available yet — send a message first, then /model.",
                None,
            )
            assert d._model_pickers == {}

    def test_the_header_and_bullet_follow_the_pick(self) -> None:
        d, cli, _sess = self._post()
        text, markup = cli.sent[-1]
        assert text == f"Current model: {self._AUTO}\nPick one:"
        assert markup["inline_keyboard"] == [
            [{"text": f"• {self._AUTO}", "callback_data": "m:0"}],
            [{"text": "Opus 5", "callback_data": "m:1"}],
            [{"text": "GPT 5.6 Sol", "callback_data": "m:2"}],
        ]
        asyncio.run(d.on_callback(_press("m:2")))
        asyncio.run(d.handle_message(_dm("/model")))
        text, markup = cli.sent[-1]
        assert text == "Current model: GPT 5.6 Sol\nPick one:"
        assert markup["inline_keyboard"][2][0]["text"] == "• GPT 5.6 Sol"
        assert markup["inline_keyboard"][0][0]["text"] == self._AUTO

    def test_an_argument_is_refused_but_the_list_still_posts(self) -> None:
        d, cli, _sess = TestModelPicker()._with_models()
        asyncio.run(d.handle_message(_dm("/model foo")))
        assert cli.sent[-1][0] == (
            "/model takes no argument — pick from the list.\n\n"
            f"Current model: {self._AUTO}\nPick one:"
        )

    def test_an_unlisted_preference_shows_its_raw_id(self) -> None:
        d, cli, _sess = TestModelPicker()._with_models()
        d._model_pref[("direct", "7")] = "ghost"
        asyncio.run(d.handle_message(_dm("/model")))
        text, markup = cli.sent[-1]
        assert text == "Current model: ghost\nPick one:"
        assert not any(row[0]["text"].startswith("• ") for row in markup["inline_keyboard"])

    def test_a_native_picker_records_its_route_and_session(self) -> None:
        d, _cli, _sess = self._post()
        picker = d._model_pickers["7:101"]
        assert picker.route == ("direct", "7")
        assert picker.session_key == self._KEY
        assert picker.store_route_preference is True
        assert picker.choices == d._model_choices(self._KEY)

    def test_a_miss_retires_the_keyboard_with_the_pickers_own_wording(self) -> None:
        d, cli, _sess = _dispatcher({7})
        asyncio.run(d.on_callback(_press("m:0", message_id=999)))
        assert cli.edits[-1] == (
            999,
            "⌛ This model list is no longer active — send /model again.",
            {"inline_keyboard": []},
        )
        asyncio.run(d.on_callback(_press("g:0", message_id=998)))
        assert cli.edits[-1] == (
            998,
            "⌛ This agent list is no longer active — send /agent again.",
            {"inline_keyboard": []},
        )

    def test_an_out_of_range_press_destroys_the_live_picker(self) -> None:
        d, cli, sess = self._post()
        asyncio.run(d.on_callback(_press("m:99")))
        assert "7:101" not in d._model_pickers
        asyncio.run(d.on_callback(_press("m:2")))
        assert "no longer active" in cli.edits[-1][1]
        assert sess._gp.set_models == []

    def test_the_picker_is_consumed_before_it_is_applied(self) -> None:
        d, _cli, _sess = self._post()
        seen: list[bool] = []

        async def _probe(*_a: Any, **_k: Any) -> str:
            seen.append("7:101" in d._model_pickers)
            return "ok"

        d._apply_model = _probe  # type: ignore[method-assign]
        asyncio.run(d.on_callback(_press("m:2")))
        assert seen == [False]

    def test_a_picker_is_keyed_by_chat(self) -> None:
        d, cli, sess = TestModelPicker()._with_models()
        d._allowed.add(8)
        asyncio.run(d.handle_message(_dm("/model")))
        asyncio.run(d.on_callback(_press("m:2", uid=8)))
        assert "no longer active" in cli.edits[-1][1]
        assert "7:101" in d._model_pickers
        assert sess._gp.set_models == []

    def test_a_press_after_new_is_denied_and_audited(self, monkeypatch: Any) -> None:
        audits = _facade_audits(monkeypatch)
        d, cli, sess = self._post()
        asyncio.run(d.handle_message(_dm("/new")))

        asyncio.run(d.on_callback(_press("m:2")))

        assert cli.edits[-1] == (
            101,
            "⌛ This model list belongs to a session this chat no longer controls. "
            "Send /model again.",
            {"inline_keyboard": []},
        )
        assert sess._gp.set_models == [] and ("direct", "7") not in d._model_pref
        assert sess.acquired == []
        assert [a for a in audits if a["operation"] == "telegram.set_model"] == [
            {
                "caller": "7",
                "operation": "telegram.set_model",
                "outcome": "denied",
                "source": "telegram",
                "resources": "model=GPT 5.6 Sol",
                "error": "session_binding_changed",
            }
        ]
        asyncio.run(d.on_callback(_press("m:2")))
        assert "no longer active" in cli.edits[-1][1]

    def test_a_press_is_audited_with_the_display_label(self, monkeypatch: Any) -> None:
        audits = _facade_audits(monkeypatch)
        d, _cli, _sess = self._post()
        asyncio.run(d.on_callback(_press("m:2")))
        asyncio.run(d.handle_message(_dm("/model")))
        asyncio.run(d.on_callback(_press("m:0", message_id=102)))
        assert [a for a in audits if a["operation"] == "telegram.set_model"] == [
            {
                "caller": "7",
                "operation": "telegram.set_model",
                "outcome": "allowed",
                "source": "telegram",
                "resources": "model=GPT 5.6 Sol",
            },
            {
                "caller": "7",
                "operation": "telegram.set_model",
                "outcome": "allowed",
                "source": "telegram",
                "resources": f"model={self._AUTO}",
            },
        ]

    def test_apply_then_audit_then_one_edit(self, monkeypatch: Any) -> None:
        order: list[Any] = []
        monkeypatch.setattr(
            "kiro_crew.telegram.transport_dispatch.sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: order.append("audit")),
        )
        d, cli, _sess = self._post()

        async def _apply(*args: Any, **kwargs: Any) -> str:
            order.append(("apply", args, kwargs))
            return "OUT"

        d._apply_model = _apply  # type: ignore[method-assign]
        edits_before = len(cli.edits)

        asyncio.run(d.on_callback(_press("m:2")))

        assert order == [
            (
                "apply",
                (("direct", "7"), "gpt-5.6-sol", self._KEY),
                {"store_route_preference": True},
            ),
            "audit",
        ]
        assert cli.edits[edits_before:] == [(101, "OUT", {"inline_keyboard": []})]

    def test_a_successful_switch_acquires_switches_and_releases(self) -> None:
        d, cli, sess = self._post()
        order: list[str] = []
        real_release = sess.release

        def _release(key: str) -> None:
            order.append("release")
            real_release(key)

        async def _set_model(model_id: str) -> None:
            order.append(f"set_model:{model_id}")

        sess.release = _release  # type: ignore[method-assign]
        sess._gp.client = SimpleNamespace(set_model=_set_model)

        asyncio.run(d.on_callback(_press("m:2")))

        assert order == ["set_model:gpt-5.6-sol", "release"]
        assert sess.acquired == [self._KEY] and sess.released == [self._KEY]
        assert cli.edits[-1][1] == "✅ Now using gpt-5.6-sol."

    def test_each_switch_outcome_is_exact(self) -> None:
        label = "gpt-5.6-sol"
        next_new = (
            f"✅ Model set to {label} — this conversation keeps its current model; "
            "the switch applies to your next one (/new)."
        )
        cases = {
            "not-live": f"✅ Model set to {label} — it applies to your next message.",
            "busy": (
                f"✅ Model set to {label}, but a reply is still running — this "
                "conversation keeps its current model; the switch applies to your "
                "next one (/new)."
            ),
            "no-set-model": next_new,
            "failure": (
                f"⚠️ Couldn't switch this conversation to {label} (RuntimeError) — "
                "it applies to your next conversation (/new)."
            ),
        }
        for case, expected in cases.items():
            d, cli, sess = self._post()
            if case == "not-live":
                sess._has = False
            elif case == "busy":
                sess._busy = True
            elif case == "no-set-model":
                sess._gp.client = SimpleNamespace()
            else:
                sess._gp.set_model_error = RuntimeError("x")
            asyncio.run(d.on_callback(_press("m:2")))
            assert cli.edits[-1][1] == expected, case
            assert d._model_pref[("direct", "7")] == label, case
            assert sess.released == ([] if case in ("not-live", "busy") else [self._KEY]), case

    def test_auto_outcomes_are_exact(self) -> None:
        d, cli, sess = self._post()
        asyncio.run(d.on_callback(_press("m:0")))
        assert cli.edits[-1][1] == (
            "✅ Model set to Auto — this conversation keeps its current model; "
            "the switch applies to your next one (/new)."
        )
        d, cli, sess = self._post()
        sess._has = False
        asyncio.run(d.on_callback(_press("m:0")))
        assert cli.edits[-1][1] == "✅ Model set to Auto — it applies to your next message."

    def test_a_resumed_switch_never_stores_the_route_preference(self) -> None:
        d, _cli, sess = _dispatcher({7})
        route = ("direct", "7")
        assert asyncio.run(
            d._apply_model(route, "", "dashboard:x", store_route_preference=False)
        ) == (
            "⚠️ Auto can only be selected when starting a new Telegram conversation; "
            "the resumed session was not changed."
        )
        assert route not in d._model_pref
        assert asyncio.run(d._apply_model(route, "model-a", "dashboard:x")) == (
            "✅ Now using model-a."
        )
        assert route not in d._model_pref and sess.acquired == ["dashboard:x"]
        sess._gp.set_model_error = RuntimeError("x")
        assert asyncio.run(d._apply_model(route, "model-a", "dashboard:x")) == (
            "⚠️ Couldn't switch this conversation to model-a (RuntimeError) — "
            "the resumed session was not changed."
        )


class TestCallbackRouting:
    """The order an inline-button press is judged in, before any branch acts on it."""

    def test_an_unauthorized_press_is_neither_acked_nor_audited(self, monkeypatch: Any) -> None:
        audits = _facade_audits(monkeypatch)
        d, cli, _sess = _dispatcher({7})
        asyncio.run(
            d.on_callback(
                SimpleNamespace(
                    callback_query_id="q1",
                    user_id=999,
                    chat_id=-1001,
                    message_id=1,
                    data="opt:0:x",
                    label="x",
                    chat_type="supergroup",
                    message_thread_id=5,
                )
            )
        )
        d._allowed.clear()
        asyncio.run(d.on_callback(_press("opt:0:x")))
        d._allowed.add(7)
        asyncio.run(d.on_callback(_press("opt:0:x", uid=0)))
        assert audits == [] and cli.answered == []

    def test_a_forum_refusal_is_audited_without_an_ack(self, monkeypatch: Any) -> None:
        audits = _facade_audits(monkeypatch)
        d, cli, _sess = _dispatcher({7}, allow_forum=True, allowed_forum_chat_ids=[-1009999999999])
        for chat_type, thread, outcome in (
            ("supergroup", 5, "denied_forum_not_allowed"),
            ("supergroup", None, "denied_non_private_chat"),
            ("group", 5, "denied_non_private_chat"),
        ):
            audits.clear()
            asyncio.run(
                d.on_callback(
                    SimpleNamespace(
                        callback_query_id="qf",
                        user_id=7,
                        chat_id=-1001234567890,
                        message_id=1,
                        data="opt:0:x",
                        label="x",
                        chat_type=chat_type,
                        message_thread_id=thread,
                    )
                )
            )
            assert audits == [
                {
                    "caller": "7",
                    "operation": "telegram_transport.on_callback",
                    "outcome": outcome,
                    "source": "telegram",
                }
            ], (chat_type, thread)
        assert cli.answered == []

    def test_the_ack_precedes_the_governance_check(self, monkeypatch: Any) -> None:
        order: list[str] = []

        async def _gov(channel: str) -> bool:
            order.append(f"gov:{channel}")
            return False

        monkeypatch.setattr("kiro_crew.telegram.transport_dispatch.channel_inbound_permitted", _gov)
        d, cli, _sess = _dispatcher({7})

        async def _ack(callback_query_id: str, text: str = "") -> None:
            order.append(f"ack:{callback_query_id}")

        cli.answer_callback = _ack  # type: ignore[method-assign]
        asyncio.run(d.on_callback(_press("a:rq:n1:1")))
        assert order == ["ack:q1", "gov:telegram"]
        assert cli.edits == []

    def test_only_an_approval_reject_skips_governance(self, monkeypatch: Any) -> None:
        calls: list[str] = []

        async def _gov(channel: str) -> bool:
            calls.append(channel)
            return False

        monkeypatch.setattr("kiro_crew.telegram.transport_dispatch.channel_inbound_permitted", _gov)
        d, cli, _sess = _dispatcher({7})
        asyncio.run(d.on_callback(_press("a:rq1:n1:0")))
        assert calls == [] and cli.edits[-1][1] == "⌛ This approval already expired."
        for data in ("a:rq1:n1:1", "m:0", "noop"):
            calls.clear()
            asyncio.run(d.on_callback(_press(data)))
            assert calls == ["telegram"], data

    def test_a_denied_press_leaves_every_branch_untouched(self, monkeypatch: Any) -> None:
        allow = {"value": True}

        async def _gov(channel: str) -> bool:
            return allow["value"]

        monkeypatch.setattr("kiro_crew.telegram.transport_dispatch.channel_inbound_permitted", _gov)
        d, cli, sess = TestModelPicker()._with_models()
        asyncio.run(d.handle_message(_dm("/model")))
        allow["value"] = False
        choose = AsyncMock()
        d._session_resume.choose = choose  # type: ignore[method-assign]
        tag = session_provenance_tag(d._session_key(("direct", "7")))
        edits_before = len(cli.edits)
        sent_before = len(cli.sent)
        for data in ("m:0", f"opt:0:{tag}", "s:0"):
            asyncio.run(d.on_callback(_press(data, label="x")))
        assert len(cli.edits) == edits_before and cli.markup_edits == []
        assert len(cli.sent) == sent_before
        assert "7:101" in d._model_pickers and ("direct", "7") not in d._model_pref
        choose.assert_not_awaited()
        assert sess.successes == []

    def test_a_session_press_chooses_under_the_routing_lock(self) -> None:
        for dm_scope in ("per-channel-peer", "unified"):
            d, cli, _sess = _dispatcher({7}, dm_scope=dm_scope)
            held: list[dict[str, Any]] = []
            choose = AsyncMock(side_effect=lambda *a, **k: held.append(dict(d._routing_locks)))
            d._session_resume.choose = choose  # type: ignore[method-assign]
            cb = SimpleNamespace(
                callback_query_id="q1",
                user_id=7,
                chat_id=7,
                message_id=101,
                data="s:0",
                label="",
                chat_type="private",
                message_thread_id=None,
            )
            asyncio.run(d.on_callback(cb))
            choose.assert_awaited_once_with(
                d.client, cb, native_key=d._session_key(("direct", "7"))
            )
            assert list(held[0]) == [d._session_resume.expectation_id(7, None)]
            assert d._routing_locks == {}

    def test_an_approval_press_edits_the_exact_verdict(self) -> None:
        d, cli, _sess = _dispatcher({7})
        key = TelegramApprovalDecider.key(d._session_key(("direct", "7")), "rq9")
        for flag, verdict in (("1", "✅ Approved"), ("0", "🚫 Denied"), ("x", "🚫 Denied")):

            async def _go(flag: str = flag) -> bool:
                fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
                TelegramApprovalDecider._REGISTRY[key] = fut
                TelegramApprovalDecider.arm(key, "n1")
                await d.on_callback(_press(f"a:rq9:n1:{flag}", message_id=100))
                return fut.result()

            assert asyncio.run(_go()) is (flag == "1")
            assert cli.edits[-1] == (100, verdict, {"inline_keyboard": []}), flag
        asyncio.run(d.on_callback(_press("a:rq9:n1:1", message_id=100)))
        assert cli.edits[-1][1] == "⌛ This approval already expired."

    def test_an_approval_press_is_parsed_from_the_right(self, monkeypatch: Any) -> None:
        resolved: list[tuple[Any, ...]] = []

        def _resolve(key: str, approved: bool, nonce: str = "") -> bool:
            resolved.append((key, approved, nonce))
            return True

        monkeypatch.setattr(TelegramApprovalDecider, "resolve_global", staticmethod(_resolve))
        d, _cli, _sess = _dispatcher({7})
        session_key = d._session_key(("direct", "7"))
        asyncio.run(d.on_callback(_press("a:spawn:abc:n1:1")))
        asyncio.run(d.on_callback(_press("a:r:1")))
        assert resolved == [
            (TelegramApprovalDecider.key(session_key, "spawn:abc"), True, "n1"),
            (TelegramApprovalDecider.key(session_key, ""), True, "r"),
        ]

    def test_a_plain_approval_writes_no_trust_audit(self, monkeypatch: Any) -> None:
        audits = _facade_audits(monkeypatch)
        d, _cli, _sess = _dispatcher({7})
        asyncio.run(d.on_callback(_press("a:rq9:n1:1")))
        assert [a for a in audits if a["operation"] == "telegram.trust_session"] == []

    def test_an_unreadable_option_label_is_refused(self) -> None:
        d, cli, sess = _dispatcher({7})
        tag = session_provenance_tag(d._session_key(("direct", "7")))
        asyncio.run(d.on_callback(TestDispatcher._option_callback(f"opt:0:{tag}", label="")))
        assert cli.markup_edits[-1] == (99, {"inline_keyboard": []})
        assert cli.sent == [("⚠️ Couldn't read that choice — please type it instead.", None)]
        assert sess.successes == []

    def test_an_untagged_press_is_refused_before_its_label_is_read(self) -> None:
        from kiro_crew.telegram import transport_dispatch as td

        for data in ("opt:0", "opt:0:"):
            d, cli, _sess = _dispatcher({7})
            asyncio.run(d.on_callback(TestDispatcher._option_callback(data, label="")))
            assert [t for t, _ in cli.sent] == [td._UNTAGGED_OPTIONS_REFUSAL], data

    def test_an_option_echo_is_escaped_html_without_a_plain_retry(self) -> None:
        d, cli, _sess = _dispatcher({7})
        sends: list[tuple[str, dict[str, Any]]] = []
        real_send = cli.send_message

        async def _record(chat_id: int, text: str, **kw: Any) -> int:
            sends.append((text, kw))
            return await real_send(chat_id, text, **kw)

        cli.send_message = _record  # type: ignore[method-assign]
        tag = session_provenance_tag(d._session_key(("direct", "7")))
        label = '<b>R&D</b> "x"'
        asyncio.run(d.on_callback(TestDispatcher._option_callback(f"opt:0:{tag}", label=label)))
        text, kw = sends[0]
        assert text == "<blockquote>&lt;b&gt;R&amp;D&lt;/b&gt; &quot;x&quot;</blockquote>"
        assert kw == {"message_thread_id": None, "parse_mode": "HTML", "retry_plain": False}

    def test_an_option_echo_falls_back_to_plain_text(self) -> None:
        d, cli, _sess = _dispatcher({7})
        real_send = cli.send_message

        async def _no_html(chat_id: int, text: str, **kw: Any) -> Any:
            if text.startswith("<blockquote>"):
                return None
            return await real_send(chat_id, text, **kw)

        cli.send_message = _no_html  # type: ignore[method-assign]
        tag = session_provenance_tag(d._session_key(("direct", "7")))
        asyncio.run(d.on_callback(TestDispatcher._option_callback(f"opt:0:{tag}")))
        assert cli.sent[0][0] == "» Say Hi"
        assert "Answer: Say Hi" in (cli.final_text() or "")

    def test_an_option_press_replays_a_widget_message(self) -> None:
        d, _cli, _sess = _dispatcher({7})
        seen: list[tuple[Any, dict[str, Any]]] = []

        async def _spy(msg: Any, **kw: Any) -> None:
            seen.append((msg, kw))

        d.handle_message = _spy  # type: ignore[method-assign]
        asyncio.run(d.on_callback(TestDispatcher._option_callback("opt:0:ab:cd")))
        msg, kw = seen[0]
        assert kw == {"interpret_commands": False, "origin_tag": "ab:cd"}
        assert msg == TelegramInboundMessage(
            channel_type="telegram",
            user_id="7",
            conversation_id="7",
            text="Say Hi",
            thread_id=None,
            chat_type="private",
            from_widget=True,
            person_origin=True,
        )

    def test_unknown_callback_data_is_inert(self, monkeypatch: Any) -> None:
        audits = _facade_audits(monkeypatch)
        calls: list[str] = []

        async def _gov(channel: str) -> bool:
            calls.append(channel)
            return True

        monkeypatch.setattr("kiro_crew.telegram.transport_dispatch.channel_inbound_permitted", _gov)
        d, cli, _sess = _dispatcher({7})
        for data in ("noop", "", "zzz", "x:1"):
            asyncio.run(d.on_callback(_press(data)))
        assert cli.answered == ["q1"] * 4 and calls == ["telegram"] * 4
        assert cli.edits == [] and cli.markup_edits == [] and cli.sent == []
        assert audits == []
