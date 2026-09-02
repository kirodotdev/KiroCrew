"""The channel notice for a removed inbound session-resume binding.

``SessionMap`` audits every removal itself but cannot reach the conversation —
that needs a transport, which the gateway owns. These pin the other half: the
listener ``DashboardState`` registers, which reason it stays quiet for, and the
delivery it performs through the governed cross-surface ladder.

All against fakes — no real transport, session manager, or network.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.messaging.link import ChannelLink

LINK = ChannelLink(channel_type="discord", channel_id="chan-1")
KEY = "discord:kirocrew:direct:42"


def _has_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class _Transport:
    def __init__(self, fail: bool = False, proactive: bool = True) -> None:
        self.sent: list[tuple[str, str, str | None]] = []
        self.fail = fail
        self.capabilities = SimpleNamespace(
            supports_proactive_send=proactive, max_message_chars=2000
        )

    def may_send_to(
        self, conversation_id: str, thread_id: str | None = None, *, principal: str = ""
    ) -> bool:
        """Part of the contract the send ladder consults before a proactive send.

        Permissive so these tests keep exercising the notice itself;
        ``test_channel_transport_outbound_authz`` owns the refusal path.
        """
        return True

    async def send_message(
        self, conversation_id: str, content: str, thread_id: str | None = None
    ) -> str:
        if self.fail:
            raise RuntimeError("discord down")
        self.sent.append((conversation_id, content, thread_id))
        return "mid-1"


@pytest.fixture()
def state(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


@contextmanager
def _permit_governance():
    """Permit the ladder's governance gate, which it imports at call time."""
    with patch(
        "kiro_crew.platform.governance_profiles.vet_and_audit",
        return_value=SimpleNamespace(permitted=True),
    ):
        yield


def _listener(state: DashboardState):
    """Install the listener and return the closure handed to the session map."""
    state.wire_session_unbind_listener()
    state.sessions.set_unbind_listener.assert_called_once()
    return state.sessions.set_unbind_listener.call_args[0][0]


async def _sent(
    state: DashboardState,
    *,
    title: str | None = None,
    reason: str = "dashboard_unlink",
) -> str:
    """Deliver one notice through a fake transport and return the text it got."""
    transport = _Transport()
    state.channel_transports["discord"] = transport
    title_patch = (
        patch.object(state, "_unbind_notice_title", return_value=title)
        if title is not None
        else nullcontext()
    )
    with title_patch, _permit_governance():
        await state._notify_inbound_unbind(KEY, LINK, reason)
    assert len(transport.sent) == 1
    assert transport.sent[0][0] == "chan-1"
    return transport.sent[0][1]


class TestWiring:
    @pytest.mark.parametrize(
        "reason, loop_closed, scheduled",
        [
            ("dashboard_unlink", False, True),
            # The in-channel command already replied there, so a notice would echo.
            ("user_unlink", False, False),
            # A shutdown race must not raise out of a synchronous map callback.
            ("entry_deleted", True, False),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_removal_schedules_the_notice_unless_suppressed(
        self, state: DashboardState, reason: str, loop_closed: bool, scheduled: bool
    ) -> None:
        listener = _listener(state)
        closed_patch = (
            patch.object(asyncio.get_running_loop(), "is_closed", return_value=True)
            if loop_closed
            else nullcontext()
        )
        with patch.object(state, "_notify_inbound_unbind", AsyncMock()) as notify:
            with closed_patch:
                listener(KEY, LINK, reason)
            # Two hops: call_soon_threadsafe lands the callback, which creates
            # the task, which then runs.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        if scheduled:
            notify.assert_awaited_once_with(KEY, LINK, reason)
        else:
            notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_notice_task_is_tracked_until_it_finishes(
        self, state: DashboardState
    ) -> None:
        """An untracked task can be collected mid-send, silently losing the notice."""
        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow(key: str, link: ChannelLink, reason: str) -> None:
            started.set()
            await release.wait()

        with patch.object(state, "_notify_inbound_unbind", _slow):
            _listener(state)(KEY, LINK, "dashboard_unlink")
            await asyncio.wait_for(started.wait(), timeout=5)
            # A strong reference is held for the task's whole lifetime.
            assert len(state._background_tasks) == 1
            task = next(iter(state._background_tasks))
            release.set()
            await asyncio.wait_for(task, timeout=5)

        # Released on completion, so the set does not grow without bound.
        assert state._background_tasks == set()

    @pytest.mark.asyncio
    async def test_a_failing_notice_task_is_reaped_and_its_error_consumed(
        self, state: DashboardState
    ) -> None:
        async def _boom(key: str, link: ChannelLink, reason: str) -> None:
            raise RuntimeError("notice exploded")

        with patch.object(state, "_notify_inbound_unbind", _boom):
            _listener(state)(KEY, LINK, "entry_deleted")
            for _ in range(20):
                await asyncio.sleep(0)
                if not state._background_tasks:
                    break

        assert state._background_tasks == set()


class TestWorkerThreadDelivery:
    """A clear on a worker thread must still reach the channel.

    ``SessionMap`` is synchronous and reachable from ``asyncio.to_thread``, so a
    listener that looked up the running loop at call time found none and dropped
    the notice. The loop captured at wire time plus ``call_soon_threadsafe`` closes
    that gap — and since the map holds its lock across the callback, the callback
    must also return without waiting on the delivery.
    """

    @pytest.mark.asyncio
    async def test_a_clear_from_a_worker_thread_is_delivered_without_blocking_it(
        self, state: DashboardState
    ) -> None:
        transport = _Transport()
        state.channel_transports["discord"] = transport
        listener = _listener(state)
        returned = threading.Event()

        def _worker() -> None:
            # No running loop on this thread — the exact condition that used to
            # drop the notice.
            assert not _has_running_loop()
            listener(KEY, LINK, "dashboard_unlink")
            # Set only after the callback returned, so waiting on it proves the
            # callback did not block on the delivery it queued.
            returned.set()

        with _permit_governance():
            thread = threading.Thread(target=_worker)
            thread.start()
            assert await asyncio.to_thread(returned.wait, 5) is True
            thread.join(5)
            # Drain the scheduled callback and the task it creates.
            for _ in range(200):
                if transport.sent:
                    break
                await asyncio.sleep(0.01)

        assert len(transport.sent) == 1
        assert "detached" in transport.sent[0][1]


class TestDelivery:
    @pytest.mark.asyncio
    async def test_notice_names_the_session_by_key_without_a_tab(
        self, state: DashboardState
    ) -> None:
        """No slot displays the session, so the key is what identifies it."""
        assert KEY in await _sent(state, reason="session_destroyed")

    @pytest.mark.parametrize(
        "transport, permit",
        [
            (None, True),  # nothing registered for this channel
            (_Transport(proactive=False), True),  # channel cannot be pushed to
            (_Transport(fail=True), True),  # send raised
            (_Transport(), False),  # governance denied
        ],
    )
    @pytest.mark.asyncio
    async def test_an_undeliverable_notice_is_a_logged_noop(
        self, state: DashboardState, transport, permit: bool, caplog
    ) -> None:
        """The binding is already gone and audited; a failed notice adds nothing —
        but the drop itself must be visible to the operator. The user was NOT told
        their conversation lost its way back, and the SEL event records the
        removal, not the delivery failure, so a fully silent return here is a
        traceless gap (live incident 2026-08-30: a destroyed binding produced no
        channel notice and no log line naming why)."""
        state.channel_transports["discord"] = transport or _Transport(proactive=False)

        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            return_value=SimpleNamespace(permitted=permit),
        ):
            with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.state"):
                await state._notify_inbound_unbind(KEY, LINK, "entry_deleted")

        assert state.channel_transports["discord"].sent == []
        assert any(
            "undeliverable" in record.message or "Failed to deliver" in record.message
            for record in caplog.records
        )


class TestNoticeIsSafeAndHuman:
    """A user-controlled title must not carry secrets, pings or audit tokens out.

    The title comes from a rename or an LLM, and the reason is an internal audit
    dimension. Both reach a channel through this one notice, so both are sanitized
    at the same boundary: the shared display sink, then a human phrase per reason
    with a generic fallback.
    """

    # Split literals so this file carries no scannable secret in one token; both
    # fixtures are ones the redactors are already pinned against in
    # test_security.py. A well-formed S3 presigned URL is deliberately exempt
    # (``_is_safe_presigned``), so the URL below carries a fixed credential in its
    # query instead — the shape ``_exfil_url_warning`` classifies unconditionally.
    AWS_KEY_ID = "AKIA" "IOSFODNN7EXAMPLE"
    EXFIL_URL = "https://evil.example.com/collect?t=" + "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef12"
    ZWSP = "\u200b"

    @pytest.mark.parametrize(
        "title, banned, expected",
        [
            # Credentials: plain, split by markup the channel renders away (the
            # sink scans the CANONICAL display form), and adjacent to an @ so the
            # defang's ZWSP cannot break the scan.
            (f"deploy {AWS_KEY_ID} run", AWS_KEY_ID, None),
            (f"**{AWS_KEY_ID[:4]}**{AWS_KEY_ID[4:]}", AWS_KEY_ID, None),
            (f"@ops {AWS_KEY_ID}", AWS_KEY_ID, None),
            (f"upload to {EXFIL_URL}", "evil.example.com/collect", None),
            # Mentions, in both channel grammars, defanged but still readable.
            ("ping <@1234567890>", "<@1234567890>", "@\u200b1234567890"),
            ("@everyone look", "@everyone", "@\u200beveryone"),
            ("see <!channel> now", "<!channel>", "<\u200b!channel>"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_dangerous_title_never_reaches_the_transport(
        self, state: DashboardState, title: str, banned: str, expected: str | None
    ) -> None:
        text = await _sent(state, title=title)

        assert banned not in text
        if expected is not None:
            assert expected in text
        # The notice still goes out and still explains itself.
        assert "detached" in text

    @pytest.mark.asyncio
    async def test_an_ordinary_title_passes_through_untouched(
        self, state: DashboardState
    ) -> None:
        """Sanitization is not allowed to mangle the common case."""
        text = await _sent(state, title="Refactor the billing importer")

        assert "Refactor the billing importer" in text
        assert "someone unlinked it from the dashboard" in text
        assert "!sessions" in text
        assert self.ZWSP not in text

    @pytest.mark.parametrize(
        "reason, banned",
        [
            ("dashboard_unlink", "dashboard_unlink"),
            ("origin_rebind", "origin_rebind"),
            ("session_destroyed", "session_destroyed"),
            ("entry_deleted", "entry_deleted"),
            ("unspecified", "unspecified"),
            # An unmapped reason must not surface as itself either.
            ("some_future_reason", "some_future_reason"),
            # The reason slot is scanned too, not just the title.
            (f"unlink {AWS_KEY_ID}", AWS_KEY_ID),
        ],
    )
    @pytest.mark.asyncio
    async def test_no_audit_token_reaches_the_channel(
        self, state: DashboardState, reason: str, banned: str
    ) -> None:
        text = await _sent(state, reason=reason)

        assert banned not in text
        assert "detached" in text

    @pytest.mark.asyncio
    async def test_an_unmapped_reason_falls_back_to_generic_copy(
        self, state: DashboardState
    ) -> None:
        assert "the link was cleared" in await _sent(state, reason="some_future_reason")

    def test_every_audited_reason_has_a_phrase(self) -> None:
        """A new reason constant without copy would fall back silently."""
        from kiro_crew.dashboard.state import _INBOUND_UNBIND_WHY
        from kiro_crew.messaging.link import UNBIND_REASON_USER_UNLINK, UNBIND_REASONS

        # user_unlink is never announced (the in-channel command already replied),
        # so it is the one reason that needs no phrase.
        assert UNBIND_REASONS - {UNBIND_REASON_USER_UNLINK} <= set(_INBOUND_UNBIND_WHY)

    @pytest.mark.asyncio
    async def test_a_delivery_failure_logs_at_warning(
        self, state: DashboardState, caplog
    ) -> None:
        """A production gateway logs WARNING and above, so DEBUG is invisible."""
        state.channel_transports["discord"] = _Transport(fail=True)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.state"):
            with _permit_governance():
                await state._notify_inbound_unbind(KEY, LINK, "dashboard_unlink")

        assert any("inbound-unbind notice" in r.message for r in caplog.records)
