"""A person's claim and a BACKGROUND resume prefetch of the same saved session.

The prefetch a reload or a focus arms is a speculative ``get_or_create`` with
``speculative_resume=True`` at BACKGROUND; a typed message is a FOREGROUND claim of the
same key. Two agent processes cannot both load one saved session: the second is refused
by the first one's per-session lock and falls back to a fresh session plus history
replay. So the claim joins a prefetch that is past its spawn door, and starts the session
itself, superseding the prefetch, while the prefetch is still short of that door
(``TestRealClaimJoinsAnInFlightEagerStart`` in test_session_get_or_create.py covers the
other eager starts).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from session_helpers import (  # noqa: F401 -- the fixture is requested by name
    _mock_provider_factory,
    cfg_fixture,
)

from kiro_crew.session import SessionManager, SpeculativeResumeRefused
from kiro_crew.session_capabilities import CapabilityPreparation
from kiro_crew.start_priority import StartPriority
from kiro_crew.testing.wait import async_wait_until, await_until


def _saved_session_factory(tmp_path, monkeypatch, sid: str, gate, providers: list, entered: list):
    """A provider factory for a key whose saved session *sid* is on disk.

    Each provider is added to *providers* when it is built and to *entered* when its
    ``start`` begins, the moment its agent process would spawn; ``start`` then waits on
    *gate*. A provider the allocation asks to resume loads the saved session
    (``client.resumed``); any other starts fresh.
    """
    sessions_dir = tmp_path / "kiro-sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr("kiro_crew.session_map._kiro_sessions_dir", lambda: sessions_dir)
    (sessions_dir / f"{sid}.json").write_text("{}")
    (sessions_dir / f"{sid}.jsonl").write_text("x" * 32)
    # Every provider reads as an ACP one, so the allocation reads the load's
    # outcome off ``client.resumed``.
    monkeypatch.setattr("kiro_crew.providers.acp.AcpProvider", object)
    base = _mock_provider_factory()

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = base(session_key, agent, channel_id, **kwargs)
        m.cwd = "/tmp"
        m.client = MagicMock()
        m.client.resumed = False
        m.client._session_id = sid

        def _set_resume(_resume_sid):
            m.client.resumed = True  # the load restores the transcript

        m.client.set_resume_session_id = _set_resume

        async def _start():
            entered.append(m)
            await gate.wait()

        m.start = _start
        providers.append(m)
        return m

    return factory


def _parked_on_the_prefetch(mgr: SessionManager, key: str) -> bool:
    """Whether a claim is waiting on the key's eager-start announcement."""
    entry = mgr._allocation_state.speculative_starts.get(key)
    return bool(entry is not None and getattr(entry[0], "_waiters", None))


@pytest.mark.asyncio
async def test_a_foreground_send_joins_a_background_resume_prefetch(cfg, tmp_path, monkeypatch):
    """A BACKGROUND resume prefetch past its spawn door is joined whatever the
    priorities: a second process for the saved session could only be refused its
    load by the prefetch's lock and fall back to a fresh session plus history
    replay, after a second full start."""
    sid = "prior-sid-join-resume"
    gate = asyncio.Event()
    providers: list = []
    entered: list = []
    factory = _saved_session_factory(tmp_path, monkeypatch, sid, gate, providers, entered)
    mgr = SessionManager(cfg, provider_factory=factory)
    key = "dashboard:join-resume-prefetch"
    mgr._session_map.set(key, sid)

    prefetch = asyncio.create_task(
        mgr.get_or_create(
            key,
            speculative=True,
            speculative_resume=True,
            start_priority=StartPriority.BACKGROUND,
        )
    )
    # The prefetch is spawning its agent process: past its spawn door.
    await async_wait_until(lambda: len(entered) == 1, describe=lambda: len(entered))
    send = asyncio.create_task(mgr.get_or_create(key, start_priority=StartPriority.FOREGROUND))
    # Parked on the prefetch, or (the defect) starting a second process.
    await async_wait_until(
        lambda: len(providers) >= 2 or _parked_on_the_prefetch(mgr, key),
        describe=lambda: len(providers),
    )

    gate.set()
    prefetched, prefetch_new, prefetch_resumed = await await_until(prefetch, what="the prefetch")
    mgr.release(key)
    provider, is_new, resumed = await await_until(send, what="the send")

    assert len(providers) == 1, "the send started a second agent process on the saved session"
    assert entered == [prefetched]
    assert (prefetch_new, prefetch_resumed) == (True, True)
    assert provider is prefetched
    # The prefetch armed the resumed observation for the real turn.
    assert (is_new, resumed) == (True, True)
    assert mgr.count == 1
    assert mgr._allocation_state.speculative_starts == {}
    mgr.release(key)
    await mgr.close_all()


async def _supersede_a_queued_prefetch(
    mgr: SessionManager, key: str, entered: list, **claim
) -> tuple:
    """Supersede a queued BACKGROUND resume prefetch of *key* with a FOREGROUND send.

    Background starts hold every background permit, so the prefetch queues for its
    cold-start permit with no process and no lock yet. The send (both calls take
    *claim*) must not wait in that queue: it starts the session itself on the person
    reserve, and the prefetch, once its permit comes, stands down at its spawn door
    instead of starting a second process for the saved session. Returns the send's
    ``get_or_create`` result after the stand-down.
    """
    sem = mgr._start_sem
    held = 0
    # A fan-out of background starts holds every background permit.
    while not sem.locked(StartPriority.BACKGROUND):
        await sem.acquire(StartPriority.BACKGROUND)
        held += 1

    # The stood-down prefetch's unstarted provider is handed to the hard kill.
    with patch.object(SessionManager, "_dispatch_hard_kill"):
        prefetch = asyncio.create_task(
            mgr.get_or_create(
                key,
                speculative=True,
                speculative_resume=True,
                start_priority=StartPriority.BACKGROUND,
                **claim,
            )
        )
        try:
            await async_wait_until(
                lambda: sem.queued_by_priority() == (0, 1), describe=sem.describe
            )
            assert entered == [], "the prefetch is queued, short of its spawn door"
            send = asyncio.create_task(
                mgr.get_or_create(key, start_priority=StartPriority.FOREGROUND, **claim)
            )
            # Done, or (the defect) parked on the queued prefetch.
            await async_wait_until(
                lambda: send.done() or _parked_on_the_prefetch(mgr, key),
                describe=sem.describe,
            )
            assert send.done(), (
                "the person's send waits behind background starts for a prefetch "
                "that has not spawned"
            )
            result = send.result()
            mgr.release(key)
            assert entered == [result[0]]
        finally:
            for _ in range(held):
                sem.release(StartPriority.BACKGROUND)
        with pytest.raises(SpeculativeResumeRefused):
            await await_until(prefetch, what="the superseded prefetch")

    assert entered == [result[0]], "the prefetch started a second agent process"
    return result


@pytest.mark.asyncio
async def test_a_foreground_send_does_not_wait_on_a_queued_background_resume_prefetch(
    cfg, tmp_path, monkeypatch
):
    """A BACKGROUND resume prefetch still queued for its cold-start permit, behind
    background starts that hold every background permit, has no process and no lock
    yet. A person's FOREGROUND send must not wait in that queue: it starts the
    session itself on the person reserve and loads it, and the prefetch stands down
    at its spawn door once its permit comes, instead of starting a second process
    for the saved session."""
    sid = "prior-sid-queued-prefetch"
    gate = asyncio.Event()
    gate.set()
    providers: list = []
    entered: list = []
    factory = _saved_session_factory(tmp_path, monkeypatch, sid, gate, providers, entered)
    mgr = SessionManager(cfg, provider_factory=factory)
    key = "dashboard:queued-resume-prefetch"
    mgr._session_map.set(key, sid)

    _provider, is_new, resumed = await _supersede_a_queued_prefetch(mgr, key, entered)

    # The send loaded the saved session itself: nobody held its lock.
    assert (is_new, resumed) == (True, True)
    assert mgr.count == 1
    assert mgr._allocation_state.speculative_starts == {}
    assert mgr._allocation_state.superseded_speculative_starts == set()
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_superseded_crew_member_prefetch_records_no_capability_failure(
    cfg, tmp_path, monkeypatch
):
    """A start of a crew member's session with saved capabilities that fails is
    recorded as a failed capability start, and the member's Capabilities status reads
    ``failed`` from that record once no session of the key is live. A resume prefetch
    that stands down at its spawn door for the person's send did not fail: it leaves
    no record, so the status does not read ``failed`` after the send's session ends."""
    prepared = CapabilityPreparation("mate", "mate-template", "rev-1")
    monkeypatch.setattr(
        "kiro_crew.session_capabilities.prepare_runtime", lambda *_a, **_k: prepared
    )
    monkeypatch.setattr("kiro_crew.session_capabilities.verify_saved", lambda *_a, **_k: None)
    monkeypatch.setattr("kiro_crew.session_capabilities.loaded_stamp", lambda *_a, **_k: "s")
    sid = "prior-sid-member-prefetch"
    gate = asyncio.Event()
    gate.set()
    providers: list = []
    entered: list = []
    saved = _saved_session_factory(tmp_path, monkeypatch, sid, gate, providers, entered)

    def factory(*args, **kwargs):
        m = saved(*args, **kwargs)
        # A harness that carries the member's saved spec, and a fresh process.
        m.member_capabilities_supported = True
        m.member_tool_limits_supported = True
        m.process_instance = ""
        return m

    mgr = SessionManager(cfg, provider_factory=factory)
    key = "dashboard:member-resume-prefetch"
    mgr._session_map.set(key, sid)

    await _supersede_a_queued_prefetch(mgr, key, entered, crew_agent="mate")

    assert (
        mgr._allocation_state.capability_failures == {}
    ), "the stood-down prefetch was recorded as a failed capability start"
    await mgr.close_all()
    assert mgr.capability_runtime_view("mate", "rev-1")["status"] == "pending"


@pytest.mark.asyncio
async def test_a_resume_prefetch_with_nothing_to_load_spends_no_start(cfg, tmp_path, monkeypatch):
    """The resumable hint is an in-memory membership read, so a prefetch can be armed
    for an entry whose transcript is gone; the allocation's lookup prunes it and leaves
    nothing to load. Such a prefetch could only start a fresh session and be refused
    once that start ended, and a send joined to it would wait that start out before
    starting its own. It is refused before any process is built or claimed, and the
    send then starts the one fresh session."""
    gate = asyncio.Event()
    gate.set()
    providers: list = []
    entered: list = []
    factory = _saved_session_factory(tmp_path, monkeypatch, "on-disk-sid", gate, providers, entered)
    mgr = SessionManager(cfg, provider_factory=factory)
    key = "dashboard:stale-resume-prefetch"
    mgr._session_map.set(key, "sid-with-no-transcript")
    assert mgr.resumable_hint(key)

    prefetch = asyncio.create_task(
        mgr.get_or_create(
            key,
            speculative=True,
            speculative_resume=True,
            start_priority=StartPriority.BACKGROUND,
        )
    )
    with pytest.raises(SpeculativeResumeRefused):
        await await_until(prefetch, what="the prefetch with nothing to load")
    assert providers == [], "a prefetch with nothing to load spent an agent start"
    assert mgr._allocation_state.speculative_starts == {}

    provider, is_new, resumed = await await_until(
        asyncio.create_task(mgr.get_or_create(key, start_priority=StartPriority.FOREGROUND)),
        what="the send",
    )
    assert providers == [provider] and entered == [provider]
    assert (is_new, resumed) == (True, False)
    mgr.release(key)
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_resume_prefetch_a_provider_switch_left_nothing_to_load_stands_down_at_its_door(
    cfg, tmp_path, monkeypatch
):
    """A provider switch clears the saved session after the provider is built, so the
    prefetch has nothing to restore. It stands down at its spawn door instead of
    spawning a fresh agent process that would be refused once its start ended."""
    sid = "prior-sid-provider-switch"
    gate = asyncio.Event()
    gate.set()
    providers: list = []
    entered: list = []
    factory = _saved_session_factory(tmp_path, monkeypatch, sid, gate, providers, entered)
    monkeypatch.setattr("kiro_crew.session.detect_provider_switch", lambda *a: True)
    mgr = SessionManager(cfg, provider_factory=factory)
    key = "dashboard:switched-resume-prefetch"
    mgr._session_map.set(key, sid)

    # The stood-down prefetch's unstarted provider is handed to the hard kill.
    with patch.object(SessionManager, "_dispatch_hard_kill") as dispatch:
        with pytest.raises(SpeculativeResumeRefused):
            await await_until(
                asyncio.create_task(
                    mgr.get_or_create(
                        key,
                        speculative=True,
                        speculative_resume=True,
                        start_priority=StartPriority.BACKGROUND,
                    )
                ),
                what="the prefetch a provider switch emptied",
            )
    assert len(providers) == 1
    assert entered == [], "the prefetch spawned an agent process with nothing to restore"
    dispatch.assert_called_once_with(providers[0])
    assert key not in mgr._sessions
    assert mgr._allocation_state.speculative_starts == {}
    await mgr.close_all()
