"""A channel completion injection that times out is not sent to the parent twice.

The channel route makes up to two attempts and only a timeout starts the second. A
timeout can land after the prompt reached the backend, and the timeout's reset keeps
the parent's conversation, so re-sending the completion would put it in that
conversation twice. The second attempt sends the completion again only when the
first never wrote its prompt; otherwise it asks the parent to continue.

Drives the real ``SessionManager`` lease. The first attempt hangs on an event that is
never set, so the patched injection timeout is what ends it. The route's own pause
between attempts runs as written. The tests whose attempt also reloads a lost session
fire that timeout on a signal (``_AttemptDeadline``), because a reload's worker-thread
hops can outlast any fixed deadline on a loaded runner.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager
from kiro_crew.subagent import SubagentInfo

#: Ceiling for a wait that only fails on a hang, so a regression fails by name.
_HANG_CEILING_S = 30
_ANNOUNCE = "child-1 finished: the answer is 42"
#: The patched ``INJECTION_TIMEOUT`` an ``_AttemptDeadline`` recognises. Long enough
#: never to fire by itself within the hang ceiling.
_SIGNALLED_TIMEOUT_S = 3 * _HANG_CEILING_S


class _AttemptDeadline:
    """The injection attempt's deadline, fired when the test says so.

    Each ``asyncio.wait_for`` given ``timeout=_SIGNALLED_TIMEOUT_S`` runs under an
    ``asyncio.timeout`` that never fires by itself; :meth:`expire` makes the newest
    one fire on the next loop pass. That is a real deadline (the attempt is cancelled
    and the route sees ``TimeoutError``), placed at the moment the test names rather
    than raced against the host's speed. Every other ``wait_for`` runs unchanged.
    """

    def __init__(self) -> None:
        self._real_wait_for = asyncio.wait_for
        self._active: list[asyncio.Timeout] = []
        self.expired = 0

    async def wait_for(self, aw: Any, timeout: float | None = None) -> Any:
        if timeout != _SIGNALLED_TIMEOUT_S:
            return await self._real_wait_for(aw, timeout)
        async with asyncio.timeout(None) as deadline:
            self._active.append(deadline)
            try:
                return await aw
            finally:
                self._active.remove(deadline)

    def expire(self) -> None:
        """Fire the newest attempt's deadline; must run on the loop."""
        assert self._active, "no injection attempt is waiting on its deadline"
        self._active[-1].reschedule(asyncio.get_running_loop().time())
        self.expired += 1


def _provider_factory(session_ids: list[str]) -> Any:
    """Each provider the manager builds takes the next backend session id."""

    def factory(session_key: Any = None, agent: Any = None, channel_id: Any = None, **kwargs: Any):
        m = AsyncMock()
        m.start = AsyncMock()
        m.memory_mode = kwargs.get("memory_mode", "persistent")
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        m.disown_work_dir = MagicMock()
        m.context_usage_pct = lambda: 0.0
        m.context_window_tokens = lambda: 0
        m.has_active_turn = lambda: False
        m.runtime_abort_target = lambda: None
        m.spawn_identity = ""
        m._runtime = None
        m.cancel = AsyncMock(return_value="acked")
        m.prompts_written = 0
        m.session_id = session_ids.pop(0)
        return m

    return factory


def _gateway(sessions: SessionManager) -> Any:
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gateway = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gateway.sessions = sessions
    gateway.ctx_builder = None
    gateway.conv_log = None
    gateway.dashboard_state = None
    gateway.slack = None
    gateway._owner_id = ""
    gateway.subagent_mgr = MagicMock()
    gateway._channel_reply_link = MagicMock(return_value=None)
    gateway._deliver_channel_reply = AsyncMock()
    return gateway


async def _run(
    *,
    first_attempt_writes: bool,
    retry_resumes: bool = True,
    retry_flags: tuple[bool, bool] = (True, True),
) -> tuple[list[str], Any]:
    key = "discord:dm:42"
    # The timeout's reset makes the retry's claim a new provider. A resume loads
    # the same backend session id; a fresh conversation gets another one.
    sessions = SessionManager(
        KiroCrewConfig(),
        provider_factory=_provider_factory(["sess-a", "sess-a" if retry_resumes else "sess-b"]),
    )
    gateway = _gateway(sessions)
    real_get_or_create = sessions.get_or_create
    claims = 0

    async def get_or_create(session_key: str, **kwargs: Any) -> tuple[Any, bool, bool]:
        # The retry's own (is_new, resumed) flags say nothing reliable about the
        # conversation: another claim can have used up its first turn first.
        nonlocal claims
        claims += 1
        client, is_new, resumed = await real_get_or_create(session_key, **kwargs)
        if claims > 1:
            return client, *retry_flags
        return client, is_new, resumed

    sessions.get_or_create = get_or_create  # type: ignore[method-assign]
    sent: list[str] = []
    never = asyncio.Event()

    async def inject(info, client, msg, parent_key, label, **kwargs):
        sent.append(msg)
        if len(sent) == 1:
            if first_attempt_writes:
                client.prompts_written += 1
            await never.wait()
        return "synthesized reply"

    gateway._inject_with_retry = inject
    info = SubagentInfo(id="child-1", task="work", parent_session_key=key)
    info.result = "done"
    info.done = True
    try:
        with (
            patch("kiro_crew.slack.gateway._MAX_INJECT_ATTEMPTS", 2),
            patch("kiro_crew.slack.gateway.INJECTION_TIMEOUT", 0.05),
        ):
            await asyncio.wait_for(
                gateway._inject_subagent_into_channel_parent(
                    info,
                    announce=_ANNOUNCE,
                    body="b",
                    title="t",
                    parent_key=key,
                    carried=(info.id,),
                ),
                _HANG_CEILING_S,
            )
    finally:
        await sessions.close_all()
    return sent, gateway


@pytest.mark.asyncio
async def test_timeout_after_the_prompt_went_out_does_not_resend_the_completion() -> None:
    sent, gateway = await _run(first_attempt_writes=True)

    assert len(sent) == 2
    assert sent[0] == _ANNOUNCE
    assert _ANNOUNCE not in sent[1], "the completion was sent to the parent a second time"
    assert "child-1" in sent[1] and "Continue" in sent[1]
    gateway._deliver_channel_reply.assert_awaited_once()
    gateway.subagent_mgr.notify_injection_failed.assert_not_called()


@pytest.mark.asyncio
async def test_timeout_before_the_prompt_went_out_resends_the_completion() -> None:
    sent, gateway = await _run(first_attempt_writes=False)

    assert sent == [_ANNOUNCE, _ANNOUNCE]
    gateway._deliver_channel_reply.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retry_flags",
    [(True, False), (False, False)],
    ids=["reported_new", "first_turn_used_by_a_queued_message"],
)
async def test_a_retry_on_a_conversation_that_did_not_come_back_resends_the_completion(
    retry_flags: tuple[bool, bool],
) -> None:
    sent, gateway = await _run(
        first_attempt_writes=True, retry_resumes=False, retry_flags=retry_flags
    )

    assert sent == [_ANNOUNCE, _ANNOUNCE], "the new conversation never saw the completion"
    gateway._deliver_channel_reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_reload_inside_the_retry_onto_a_new_session_sends_the_full_completion() -> None:
    """Attempt 2 resumes the session the timed-out prompt went to, so it sends the
    continue note. The backend then answers that it holds no session under that id,
    and the reload inside ``_inject_with_retry`` lands on a different conversation.
    That conversation never received the report, so it gets the full completion."""
    from kiro_crew.acp.client import AcpError

    key = "discord:dm:42"
    sessions = SessionManager(
        KiroCrewConfig(),
        provider_factory=_provider_factory(["sess-a", "sess-a", "sess-b"]),
    )
    gateway = _gateway(sessions)
    sent: list[tuple[str, str]] = []
    never = asyncio.Event()
    deadline = _AttemptDeadline()

    async def stream(client: Any, message: str, **kw: Any) -> str:
        sent.append((client.session_id, message))
        if len(sent) == 1:
            client.prompts_written += 1
            deadline.expire()
            await never.wait()
        if len(sent) == 2:
            raise AcpError("Session not found")
        on_complete = kw.get("on_complete")
        if on_complete is not None:
            on_complete(SimpleNamespace(stop_reason="end_turn"))
        return "synthesized reply"

    info = SubagentInfo(id="child-1", task="work", parent_session_key=key)
    info.result = "done"
    info.done = True
    try:
        with (
            patch("kiro_crew.slack.gateway._MAX_INJECT_ATTEMPTS", 2),
            patch("kiro_crew.slack.gateway.INJECTION_TIMEOUT", _SIGNALLED_TIMEOUT_S),
            patch.object(asyncio, "wait_for", deadline.wait_for),
            patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=stream),
        ):
            await asyncio.wait_for(
                gateway._inject_subagent_into_channel_parent(
                    info,
                    announce=_ANNOUNCE,
                    body="b",
                    title="t",
                    parent_key=key,
                    carried=(info.id,),
                ),
                _HANG_CEILING_S,
            )
    finally:
        await sessions.close_all()

    assert [sid for sid, _ in sent] == ["sess-a", "sess-a", "sess-b"]
    assert sent[0][1] == _ANNOUNCE
    assert _ANNOUNCE not in sent[1][1], "the resumed session should get the continue note"
    assert sent[2][1] == _ANNOUNCE, "the reloaded fresh conversation never got the report"
    gateway._deliver_channel_reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_timeout_after_a_reload_reads_the_session_that_received_the_completion() -> None:
    """Attempt 1's first prompt finds the session lost, and the reload inside
    ``_inject_with_retry`` moves it onto session B. The completion is written to
    B, then the attempt times out. The timeout handler must read B, the provider
    holding the prompt, not the provider the attempt started on. Attempt 2
    resumes B, so it gets the continue note rather than the completion again."""
    from kiro_crew.acp.client import AcpError

    key = "discord:dm:42"
    sessions = SessionManager(
        KiroCrewConfig(),
        provider_factory=_provider_factory(["sess-a", "sess-b", "sess-b"]),
    )
    gateway = _gateway(sessions)
    sent: list[tuple[str, str]] = []
    never = asyncio.Event()
    deadline = _AttemptDeadline()

    async def stream(client: Any, message: str, **kw: Any) -> str:
        sent.append((client.session_id, message))
        if len(sent) == 1:
            raise AcpError("Session not found")
        if len(sent) == 2:
            client.prompts_written += 1
            deadline.expire()
            await never.wait()
        on_complete = kw.get("on_complete")
        if on_complete is not None:
            on_complete(SimpleNamespace(stop_reason="end_turn"))
        return "synthesized reply"

    info = SubagentInfo(id="child-1", task="work", parent_session_key=key)
    info.result = "done"
    info.done = True
    try:
        with (
            patch("kiro_crew.slack.gateway._MAX_INJECT_ATTEMPTS", 2),
            patch("kiro_crew.slack.gateway.INJECTION_TIMEOUT", _SIGNALLED_TIMEOUT_S),
            patch.object(asyncio, "wait_for", deadline.wait_for),
            patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=stream),
        ):
            await asyncio.wait_for(
                gateway._inject_subagent_into_channel_parent(
                    info,
                    announce=_ANNOUNCE,
                    body="b",
                    title="t",
                    parent_key=key,
                    carried=(info.id,),
                ),
                _HANG_CEILING_S,
            )
    finally:
        await sessions.close_all()

    assert [sid for sid, _ in sent] == ["sess-a", "sess-b", "sess-b"]
    assert sent[1][1] == _ANNOUNCE
    assert _ANNOUNCE not in sent[2][1], "session B already holds the completion"
    assert "child-1" in sent[2][1]
    gateway._deliver_channel_reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_deadline_that_lands_inside_the_reload_still_ends_the_attempt() -> None:
    """Attempt 1's prompt finds the session lost, and its deadline fires while the
    reload is ending that session, inside the end's metrics crumb hand-off. The
    session teardown absorbs that cancellation so its own cleanup finishes; the
    attempt must still end on it rather than send the reloaded prompt with no
    deadline left. So attempt 1 times out, and attempt 2 delivers the completion
    on the session the timeout's reset left."""
    from kiro_crew.acp.client import AcpError
    from kiro_crew.metrics import sessions as session_metrics

    key = "discord:dm:42"
    sessions = SessionManager(
        KiroCrewConfig(),
        provider_factory=_provider_factory(["sess-a", "sess-b", "sess-c"]),
    )
    gateway = _gateway(sessions)
    sent: list[tuple[str, str]] = []
    deadline = _AttemptDeadline()
    loop = asyncio.get_running_loop()
    reload_started = threading.Event()
    hop_held = threading.Event()
    released = threading.Event()
    real_unlink_all = session_metrics._unlink_all

    def held_unlink_all(paths: Any) -> None:
        # The reload's first crumb hop stays in flight until the deadline has
        # fired inside it; every other hop runs as written.
        if reload_started.is_set() and not hop_held.is_set():
            hop_held.set()
            loop.call_soon_threadsafe(deadline.expire)
            assert released.wait(_HANG_CEILING_S), "the test never released the crumb hop"
        real_unlink_all(paths)

    async def stream(client: Any, message: str, **kw: Any) -> str:
        sent.append((client.session_id, message))
        if len(sent) == 1:
            reload_started.set()
            raise AcpError("Session not found")
        released.set()
        on_complete = kw.get("on_complete")
        if on_complete is not None:
            on_complete(SimpleNamespace(stop_reason="end_turn"))
        return "synthesized reply"

    info = SubagentInfo(id="child-1", task="work", parent_session_key=key)
    info.result = "done"
    info.done = True
    try:
        with (
            patch("kiro_crew.slack.gateway._MAX_INJECT_ATTEMPTS", 2),
            patch("kiro_crew.slack.gateway.INJECTION_TIMEOUT", _SIGNALLED_TIMEOUT_S),
            patch.object(asyncio, "wait_for", deadline.wait_for),
            patch.object(session_metrics, "_unlink_all", held_unlink_all),
            patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=stream),
        ):
            await asyncio.wait_for(
                gateway._inject_subagent_into_channel_parent(
                    info,
                    announce=_ANNOUNCE,
                    body="b",
                    title="t",
                    parent_key=key,
                    carried=(info.id,),
                ),
                _HANG_CEILING_S,
            )
    finally:
        released.set()
        await sessions.close_all()

    assert hop_held.is_set(), "the reload never reached its crumb hand-off"
    assert deadline.expired == 1
    late = "the reloaded prompt ran in attempt 1 after its deadline had fired"
    assert [sid for sid, _ in sent] == ["sess-a", "sess-c"], late
    assert sent[1][1] == _ANNOUNCE, "the reloaded session never received the prompt"
    gateway._deliver_channel_reply.assert_awaited_once()
    gateway.subagent_mgr.notify_injection_failed.assert_not_called()
