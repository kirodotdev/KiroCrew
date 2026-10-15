"""AutoNudgeService tests: firing a loop and where it fires.

The feature flag, message and idle updates, delivery failures and their backoff, the stop
sentinel and the rendered message, channel keys and self re-arming, structural terminal
stops, and the config generation.
"""

from __future__ import annotations

import pytest
from autonudge_helpers import _enable  # noqa: F401
from autonudge_helpers import svc_fixture  # noqa: F401

from kiro_crew import autonudge as _an
from kiro_crew import autonudge_authz as _autonudge_mod
from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.dashboard.handlers.autonudge import render_nudge_message

#: Lost-run ceiling for synchronization the test itself must release. It is a
#: hang guard whose expiry fails at the awaited line, never a performance
#: assertion, and is at most half the repository's ``--timeout=120``.
_LOST_RUN_SECS = 30.0

_FROZEN_NOW = 1_000_000.0


def _freeze_clock(monkeypatch) -> None:
    """Pin ``autonudge``'s clock so a deadline-anchored first delay is exact.

    ``add()`` anchors ``next_due_ts = now + idle_secs``, then AWAITS an fsync of
    the state file before ``_arm_from_deadline`` re-reads ``time.time()`` to
    derive ``remaining``. Unfrozen, the first delay comes out short by however
    long that write took (2.4s on a loaded Windows shard), so any tolerance on it
    is really asserting that the runner never stalls between two statements.
    Frozen, ``remaining`` is exactly ``idle_secs``, so every assertion below can
    be exact -- a tolerance there would only hide a regression that reintroduces
    the wall-clock dependency.
    """
    monkeypatch.setattr(_an.time, "time", lambda: _FROZEN_NOW)


@pytest.mark.asyncio
async def test_disabled_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "0")
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()
    # Service is a no-op when flag is off — add/remove still work on the in-memory
    # dict but timers never arm. Verify via the enabled() helper.
    from kiro_crew.autonudge import enabled

    assert not enabled()


@pytest.mark.asyncio
async def test_update_changes_message_and_idle(svc):
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="old", idle_secs=30)
    updated = await svc.update(loop.id, message="new", idle_secs=60)
    assert updated is not None
    assert updated.message == "new"
    assert updated.idle_secs == 60


@pytest.mark.asyncio
async def test_idle_secs_clamped(svc):
    """Verify add() clamps idle_secs to [_MIN_IDLE_SECS, _MAX_IDLE_SECS]."""
    await svc.start()
    # Below min → clamped up to 15.
    loop_low = await svc.add(slot_key="s1", message="m", idle_secs=5)
    assert loop_low.idle_secs == 15
    # Above max → clamped down to 86400.
    loop_high = await svc.add(slot_key="s2", message="m", idle_secs=100_000)
    assert loop_high.idle_secs == 86400


@pytest.mark.asyncio
async def test_skip_when_delivery_returns_false(svc, monkeypatch):
    """A skipped delivery (slot mid-turn) must NOT bump cycle_count, and must
    re-arm the timer with a backoff so the loop self-heals."""
    import asyncio

    import kiro_crew.autonudge as _an

    real_sleep = asyncio.sleep  # capture before patching
    sleep_calls: list[float] = []
    gate = asyncio.Event()  # never set — blocks the re-armed timer's sleep

    async def _sleep(secs):
        sleep_calls.append(secs)
        if len(sleep_calls) >= 2:
            await asyncio.wait_for(
                gate.wait(), timeout=_LOST_RUN_SECS
            )  # halt the re-arm chain so the test is bounded
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _sleep)
    _freeze_clock(monkeypatch)

    fired: list[NudgeLoop] = []

    async def on_fire_skip(loop):
        fired.append(loop)
        return False  # delivery skipped (e.g. slot busy)

    svc._on_fire = on_fire_skip
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=60)
    # add() now yields internally (offloaded persist), so the first timer cycle
    # may complete before add() returns — wait for the fire + self-heal re-arm
    # instead of capturing/awaiting the first timer task.
    for _ in range(500):
        if len(fired) >= 1 and len(sleep_calls) >= 2:
            break
        await real_sleep(0.005)
    # Callback ran, delivery skipped → cycle_count must not bump, loop alive.
    assert len(fired) == 1
    assert svc._loops[loop.id].cycle_count == 0
    assert svc._loops[loop.id].last_fire_ts == 0.0
    assert svc._loops[loop.id].active is True
    # Self-heal: a NEW timer is armed and parked on the gated backoff sleep.
    assert loop.id in svc._timers
    assert not svc._timers[loop.id].done()
    # First sleep used the (deadline-anchored) full idle; the re-arm used the
    # shorter backoff.
    assert sleep_calls[0] == 60
    assert _an._REARM_BACKOFF_SECS in sleep_calls
    svc._cancel_timer(loop.id)  # cleanup


@pytest.mark.asyncio
async def test_fire_callback_exception_does_not_deactivate(svc, monkeypatch):
    """An exception in _on_fire is swallowed (treated as not-delivered):
    cycle_count unchanged, loop stays active, AND the timer self-heals by
    re-arming with a backoff."""
    import asyncio

    import kiro_crew.autonudge as _an

    real_sleep = asyncio.sleep  # capture before patching
    sleep_calls: list[float] = []
    gate = asyncio.Event()  # never set — blocks the re-armed timer's sleep

    async def _sleep(secs):
        sleep_calls.append(secs)
        if len(sleep_calls) >= 2:
            await asyncio.wait_for(gate.wait(), timeout=_LOST_RUN_SECS)
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _sleep)

    async def on_fire_raise(loop):
        raise RuntimeError("kaboom")

    svc._on_fire = on_fire_raise
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=60)
    # First cycle may complete before add() returns (offloaded persist yields);
    # wait for the fire + self-heal re-arm to be observable.
    for _ in range(500):
        if len(sleep_calls) >= 2:
            break
        await real_sleep(0.005)
    refreshed = svc._loops[loop.id]
    assert refreshed.cycle_count == 0  # exception treated as not-delivered
    assert refreshed.active is True  # loop still alive
    # Self-heal: timer re-armed and parked on the gated backoff sleep.
    assert loop.id in svc._timers
    assert not svc._timers[loop.id].done()
    svc._cancel_timer(loop.id)  # cleanup


@pytest.mark.asyncio
async def test_rearm_backoff_escalates_on_consecutive_failures(svc, monkeypatch):
    """Consecutive non-deliveries escalate the re-arm delay (15 → 30 → 60 …),
    so a never-delivering loop backs off instead of hammering."""
    import asyncio

    import kiro_crew.autonudge as _an

    sleep_calls: list[float] = []
    real_sleep = asyncio.sleep

    async def _sleep(secs):
        sleep_calls.append(secs)
        if len(sleep_calls) >= 5:
            raise asyncio.CancelledError  # halt the chain; _timer returns cleanly
        # Yield, because the real asyncio.sleep always does. A double that
        # returns without yielding lets every timer generation run back-to-back
        # inside a single event-loop slice, which is not how the production
        # chain is scheduled; the yield keeps the mock a faithful stand-in.
        await real_sleep(0)

    monkeypatch.setattr(_an.asyncio, "sleep", _sleep)
    _freeze_clock(monkeypatch)

    async def on_fire_skip(loop):
        return False

    svc._on_fire = on_fire_skip
    await svc.start()
    # idle_secs large so neither the 300s ceiling nor idle_secs clamps the ramp.
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=10000)
    task = svc._timers[loop.id]
    for _ in range(12):
        try:
            await task
        except asyncio.CancelledError:
            break
        nxt = svc._timers.get(loop.id)
        if nxt is None or nxt is task:
            break
        task = nxt
    # First sleep = (deadline-anchored) full idle; then exponential backoff per failure.
    assert sleep_calls == [10000, 15, 30, 60, 120]
    assert svc._loops[loop.id].active is True
    assert svc._rearm_fail_count[loop.id] == 4
    svc._cancel_timer(loop.id)


@pytest.mark.asyncio
async def test_failure_log_rate_limited_to_once_per_streak(svc, monkeypatch):
    """A permanently-failing callback logs a full traceback only on the first
    failure of a streak, not every re-arm (log-spam fix)."""
    import asyncio

    import kiro_crew.autonudge as _an

    sleep_calls: list[float] = []

    async def _sleep(secs):
        sleep_calls.append(secs)
        if len(sleep_calls) >= 4:
            raise asyncio.CancelledError
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _sleep)
    exc_calls: list[tuple] = []
    monkeypatch.setattr(_an.logger, "exception", lambda *a, **k: exc_calls.append(a))

    async def on_fire_raise(loop):
        raise RuntimeError("kaboom")

    svc._on_fire = on_fire_raise
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=10000)
    task = svc._timers[loop.id]
    for _ in range(12):
        try:
            await task
        except asyncio.CancelledError:
            break
        nxt = svc._timers.get(loop.id)
        if nxt is None or nxt is task:
            break
        task = nxt
    # 3 fires raised (calls 1-3); only the first emitted a full traceback.
    assert len(exc_calls) == 1
    assert svc._rearm_fail_count[loop.id] == 3
    svc._cancel_timer(loop.id)


@pytest.mark.asyncio
async def test_failure_streak_resets_on_delivery(svc, monkeypatch):
    """A delivered fire clears the failure streak so the next skip starts the
    backoff ramp fresh."""
    import asyncio

    import kiro_crew.autonudge as _an

    sleep_calls: list[float] = []

    async def _sleep(secs):
        sleep_calls.append(secs)
        if len(sleep_calls) >= 5:
            raise asyncio.CancelledError
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _sleep)
    _freeze_clock(monkeypatch)

    results = [False, False, True]  # third fire delivers
    idx = {"i": 0}

    async def on_fire(loop):
        i = idx["i"]
        idx["i"] += 1
        return results[i] if i < len(results) else True

    svc._on_fire = on_fire
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=10000)
    task = svc._timers[loop.id]
    for _ in range(12):
        try:
            await task
        except asyncio.CancelledError:
            break
        nxt = svc._timers.get(loop.id)
        if nxt is None or nxt is task:
            break
        task = nxt
    # 2 skips escalated (15, 30), then delivery bumped cycle_count and the
    # delivered happy-path does not re-arm, so the chain stops at 3 sleeps.
    assert sleep_calls == [10000, 15, 30]
    assert svc._loops[loop.id].cycle_count == 1
    assert loop.id not in svc._rearm_fail_count  # streak cleared on delivery


@pytest.mark.asyncio
async def test_fire_removed_loop_does_not_rearm_orphan(svc, monkeypatch):
    """If _on_fire removes the loop (e.g. slot missing) and returns False, the
    re-arm path must NOT resurrect it with a fresh timer (orphan)."""
    import asyncio as _asyncio

    import kiro_crew.autonudge as _an

    real_sleep = _asyncio.sleep  # capture before patching
    sleep_calls: list[float] = []

    async def _sleep(secs):
        sleep_calls.append(secs)
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _sleep)
    _freeze_clock(monkeypatch)

    removed = _asyncio.Event()

    async def on_fire_self_remove(loop):
        await svc.remove(loop.id)  # slot gone — fire path drops the loop
        removed.set()
        return False

    svc._on_fire = on_fire_self_remove
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=60)
    # First cycle may complete before add() returns (offloaded persist yields);
    # wait for the fire-path removal instead of awaiting the timer task.
    for _ in range(500):
        if removed.is_set() and loop.id not in svc._timers:
            break
        await real_sleep(0.005)
    # Loop was removed by the fire path and must stay gone — no resurrection.
    assert loop.id not in svc._loops
    assert loop.id not in svc._timers
    assert loop.id not in svc._rearm_fail_count
    # Only the initial idle sleep ran; no backoff re-arm fired.
    assert sleep_calls == [60]


@pytest.mark.asyncio
async def test_delivered_bumps_cycle_count(svc, monkeypatch):
    """When _on_fire returns True, cycle_count bumps and 'fired' event emits."""
    import kiro_crew.autonudge as _an

    async def _nosleep(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)

    events: list[tuple[str, str]] = []
    svc.subscribe(lambda ev, lp: events.append((ev, lp.id if lp else "")))

    async def on_fire_ok(loop):
        return True

    svc._on_fire = on_fire_ok
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15)
    await svc._timers[loop.id]
    assert svc._loops[loop.id].cycle_count == 1
    assert svc._loops[loop.id].last_fire_ts > 0.0
    assert ("fired", loop.id) in events


@pytest.mark.asyncio
async def test_resolve_stop_sentinel(tmp_path, monkeypatch):
    """resolve_stop_sentinel computes per-slot path from workspace."""
    monkeypatch.setattr(_autonudge_mod, "workspace_dir_for", lambda ws="default": tmp_path)
    path = _autonudge_mod.resolve_stop_sentinel("chat:1/123", "default")
    assert path == str(tmp_path / ".stop-chat_1_123")


def test_render_nudge_message():
    """render_nudge_message replaces {{STOP_FILE}} with the sentinel path."""
    result = render_nudge_message("halt: create {{STOP_FILE}}", "/tmp/.stop-x")
    assert result == "halt: create /tmp/.stop-x"
    assert "{{STOP_FILE}}" not in result

    # None sentinel produces empty string
    result2 = render_nudge_message("create {{STOP_FILE}}", None)
    assert result2 == "create "


# ── Channel-key (Slack / Discord babysit) loops ──


def test_is_channel_key():
    from kiro_crew.autonudge import is_channel_key

    assert is_channel_key("slack:1700000000.123456")
    assert is_channel_key("discord:kirocrew:direct:42")
    assert is_channel_key("unified:kirocrew")
    # Bare dashboard slot keys are NOT channel keys.
    assert not is_channel_key("chat-1-123")
    # Fully-qualified dashboard keys never appear as binding keys, but must
    # not be misclassified either.
    assert not is_channel_key("dashboard:chat-1-123")


@pytest.mark.parametrize(
    "key",
    [
        "telegram:kirocrew:direct:4242",
        "webex:kirocrew:direct:user@example.com",
        "teams:kirocrew:direct:29:1abcdef",
        "weixin:kirocrew:direct:oUserOpenId",
        "imessage:kirocrew:direct:+15550100",
    ],
)
def test_proactive_channel_namespaces_are_channel_keys(key):
    """Every transport that CAN send unattended classifies as a channel session.

    ``is_channel_key`` selects the re-arm strategy and the expiry-notification
    metadata: a channel loop self-re-arms, while a dashboard loop waits for
    ``notify_turn_complete``, which never fires for a channel key. Leaving
    webex/teams/weixin/imessage out therefore made those sessions structurally
    unrunnable rather than merely unsupported — misread as dashboard slots they
    would stall with no armed timer and carry a ``dashboard:<namespace>:<id>``
    jump link pointing at no slot — even though each of those transports declares
    ``supports_proactive_send=True``.
    """
    from kiro_crew.autonudge import is_channel_key

    assert is_channel_key(key)


def test_wecom_is_classified_because_it_gained_a_proactive_send_path():
    """The classifier and the capability are asserted TOGETHER so they cannot drift.

    WeCom was excluded while its reply was bound to the inbound request's own reply
    token: a nudge cycle there would wake, spend a turn and have nowhere to put the
    answer. The channel has a proactive path over its long connection, which
    flipped the capability, so the exclusion's own stated condition fired and the
    namespace is now classified like every other channel.

    Keeping both halves in one test is the point. Either alone can go stale
    silently: a classifier entry for a channel that cannot send unattended arms
    loops with nowhere to deliver, and a flipped capability with no entry leaves a
    channel that CAN be nudged unreachable.
    """
    from kiro_crew.autonudge import is_channel_key
    from kiro_crew.wecom.transport import WECOM_CAPABILITIES

    assert WECOM_CAPABILITIES.supports_proactive_send is True
    assert is_channel_key("wecom:kirocrew:direct:oUserOpenId")


def test_channel_key_prefixes_mirror_the_shipped_namespaces():
    """The tuple is spelled out in ``autonudge``, so pin it against drift.

    Deriving it would make ``autonudge`` — imported at module scope by
    ``mcp_core`` (every MCP server process) and by the dashboard chat layer —
    name ``kiro_crew.messaging.link``, whose package ``__init__`` pulls the
    driver/renderer/transport layer and with it the ACP client, agent, hooks,
    artifacts, metrics and sqlite: 48 extra ``kiro_crew`` modules to obtain one
    tuple of string literals. This assertion buys the drift protection that
    import would have bought, at no import cost.

    Equality, not containment: a namespace added to ``CHANNEL_SESSION_NAMESPACES``
    must be classified here too, with no escape hatch for "excluded on purpose".
    The exclusion that looks tempting — a channel nothing can currently deliver a
    nudge to — is precisely the misclassification this guards against, because an
    unlisted key reads as a dashboard slot and stops being re-armed silently.
    Undeliverability belongs to the ladder; see ``_CHANNEL_KEY_PREFIXES``.
    """
    from kiro_crew.autonudge import _CHANNEL_KEY_PREFIXES
    from kiro_crew.messaging.link import CHANNEL_SESSION_NAMESPACES

    assert {p.rstrip(":") for p in _CHANNEL_KEY_PREFIXES} == set(CHANNEL_SESSION_NAMESPACES)


@pytest.mark.asyncio
async def test_unrouted_channel_namespace_degrades_instead_of_raising(svc, monkeypatch):
    """A classified namespace with no fire route degrades; it never raises.

    ``whatsapp`` is in the prefix tuple but has no transport package in this fork
    at all, so the gateway's ``_fire`` dispatcher reaches its "unsupported channel
    key" arm: it logs the reason, removes the loop and returns False. The service
    must treat that as a TERMINAL non-delivery — no backoff re-arm, no
    resurrection, no exception escaping the timer — so classifying a namespace can
    never turn an undeliverable session into a loop that hot-polls forever.
    """
    import asyncio as _asyncio

    import kiro_crew.autonudge as _an

    real_sleep = _asyncio.sleep  # capture before patching
    sleep_calls: list[float] = []

    async def _sleep(secs):
        sleep_calls.append(secs)
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _sleep)
    _freeze_clock(monkeypatch)

    removed = _asyncio.Event()

    async def on_fire_unsupported(loop):
        # Mirrors slack/gateway.py::_fire's else-branch for a channel key with
        # no implemented fire route.
        await svc.remove(loop.id)
        removed.set()
        return False

    svc._on_fire = on_fire_unsupported
    await svc.start()
    loop = await svc.add(
        slot_key="whatsapp:kirocrew:direct:15550100", message="watch", idle_secs=60
    )
    for _ in range(500):
        if removed.is_set() and loop.id not in svc._timers:
            break
        await real_sleep(0.005)
    assert loop.id not in svc._loops
    assert loop.id not in svc._timers
    assert loop.id not in svc._rearm_fail_count
    # Only the initial idle sleep ran — no backoff re-arm was scheduled.
    assert sleep_calls == [60]
    svc.stop()


@pytest.mark.asyncio
async def test_cross_surface_ladder_still_refuses_unroutable_channels(tmp_path):
    """The delivery ladder, not the key classifier, is the enforcement point.

    Membership in ``_CHANNEL_KEY_PREFIXES`` asserts "this key names a
    conversation", never "a send will succeed", so the fail-closed ladder
    ``chat_runner._resolve_channel_target`` (defined in
    ``dashboard/chat_turn/recipient.py``: governance → a REGISTERED transport →
    ``supports_proactive_send``) has to keep refusing on its own. Both of its
    transport arms are pinned here: a namespace with no
    registered transport (``whatsapp``) and a registered transport that declares
    no proactive send (a SYNTHETIC capability — every shipped channel now declares
    True, and the arm still has to refuse). Each logs its reason and degrades to a
    no-op rather than raising into the turn.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_runner import _deliver_cross_surface_reply
    from kiro_crew.messaging.link import ChannelLink
    from kiro_crew.messaging.transport import TransportCapabilities

    state = _make_state(tmp_path)

    # (a) Nothing registered for the namespace — the ladder's transport arm.
    state.sessions.get_mirror_link = MagicMock(
        return_value=ChannelLink("whatsapp", channel_id="15550100", thread_id=None)
    )
    assert state.get_channel_transport("whatsapp") is None
    await _deliver_cross_surface_reply(state, "whatsapp:kirocrew:direct:15550100", "cycle 1 output")

    # (b) Registered, but declaring no unattended send — the
    # ``supports_proactive_send`` arm. SYNTHETIC on purpose: every shipped channel
    # declares True now, and this arm must keep refusing regardless, or a future
    # transport that cannot send unattended would be handed a nudge to deliver.
    bound = SimpleNamespace(
        channel_type="telegram",
        capabilities=TransportCapabilities(supports_proactive_send=False),
        send_message=AsyncMock(return_value="mid-1"),
    )
    state.register_channel_transport(bound)
    state.sessions.get_mirror_link = MagicMock(
        return_value=ChannelLink("telegram", channel_id="4242", thread_id=None)
    )
    await _deliver_cross_surface_reply(state, "telegram:kirocrew:direct:4242", "cycle 1 output")
    bound.send_message.assert_not_awaited()


def test_binding_key_for_does_not_widen_with_the_classifier():
    """Classifying a namespace must NOT make it armable ahead of its arm path.

    ``binding_key_for`` is the "may this session be armed?" answer, and honouring
    it additionally needs an ownership check in ``autonudge_authz`` and a fire
    route in the gateway's ``_fire`` dispatcher. Passing weixin/teams/imessage
    through here ahead of those two would arm a loop that the chokepoint denies (or
    that is removed on its first fire), which is strictly worse than the clean "not
    supported from this session type" refusal it replaces.
    """
    from kiro_crew.autonudge import binding_key_for, is_channel_key

    for key in (
        "weixin:kirocrew:direct:oUserOpenId",
        "teams:kirocrew:direct:29:1abcdef",
        "imessage:kirocrew:direct:+15550100",
    ):
        assert is_channel_key(key)
        assert binding_key_for(key) is None
    # The namespaces that DO have both halves pass through. Webex is here rather
    # than in the list above because it ships both: ``_fire_webex_nudge`` in
    # slack/gateway.py and a deny-by-default ownership branch in
    # ``autonudge_authz`` (allow-listed DM sessions only, matched against the key
    # the dispatcher currently derives) — the same pair Discord has.
    assert binding_key_for("slack:1700000000.123456") == "slack:1700000000.123456"
    assert binding_key_for("discord:kirocrew:direct:42") == "discord:kirocrew:direct:42"
    assert (
        binding_key_for("webex:kirocrew:direct:user@example.com")
        == "webex:kirocrew:direct:user@example.com"
    )


@pytest.mark.asyncio
async def test_channel_loop_self_rearms_after_delivered_fire(svc, monkeypatch):
    """Slack/Discord loops run on a fixed interval: the timer re-arms itself
    after a delivered fire (notify_turn_complete never fires for these keys)."""
    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    svc._on_fire = on_fire
    import kiro_crew.autonudge as _an

    _real_sleep = _an.asyncio.sleep

    async def _nosleep(_secs):
        await _real_sleep(0)

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
    await svc.start()
    # max_cycles=1 bounds the loop: the re-armed second timer run hits the
    # cycle cap and deactivates, keeping the test deterministic.
    loop = await svc.add(
        slot_key="slack:1700000000.123456", message="check PR", idle_secs=15, max_cycles=1
    )
    await svc._timers[loop.id]
    assert len(fired) == 1
    # The re-armed second run hits the cycle cap and deactivates the loop —
    # proof the channel loop re-armed itself. A dashboard loop would idle
    # forever here waiting for notify_turn_complete. Poll with real time (not
    # bare yields): the re-arm's deadline bookkeeping awaits a locked persist
    # on an executor thread before the cap check can run.
    for _ in range(200):
        if not svc._loops[loop.id].active:
            break
        await _real_sleep(0.01)
    assert not svc._loops[loop.id].active
    assert len(fired) == 1  # cap check runs before firing — no second delivery
    svc.stop()


@pytest.mark.asyncio
async def test_dashboard_loop_does_not_self_rearm(svc, monkeypatch):
    """Dashboard loops stay idle-driven: after a delivered fire they wait for
    notify_turn_complete instead of self-re-arming."""
    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    svc._on_fire = on_fire
    import kiro_crew.autonudge as _an

    async def _nosleep(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15)
    timer1 = svc._timers[loop.id]
    await timer1
    assert len(fired) == 1
    # No new timer was armed — the finished task is still the registered one.
    assert svc._timers.get(loop.id) is timer1
    svc.stop()


def test_a_webex_session_is_nudge_able():
    """Webex is in both rosters because it now has the two things that matter.

    A fire adapter (``_fire_webex_nudge``) and an authorization branch in
    ``autonudge_authz``. Listing a channel without both arms a loop that is then
    denied or deleted on its first cycle while reporting itself healthy — which is
    exactly why these rosters are narrow.
    """
    from kiro_crew.autonudge import binding_key_for, is_channel_key

    key = "webex:kirocrew:direct:kyle@example.com"
    assert is_channel_key(key)
    assert binding_key_for(key) == key


def test_the_channels_without_a_fire_adapter_stay_excluded():
    """Deliberately narrow, and pinned so it stays a decision.

    wecom / teams / weixin / imessage have no ``_fire_*_nudge`` and no authz
    branch, so a loop bound there could never deliver.

    Asserted on ``binding_key_for`` (may this be ARMED?) and not on
    ``is_channel_key``, which answers the different question of whether the key
    names a conversation rather than a chat slot — every channel namespace is a
    channel key, deliberately, so that a loop whose transport is momentarily
    absent is not misread as a dashboard slot and silently stops re-arming.
    """
    from kiro_crew.autonudge import binding_key_for, is_channel_key

    for channel in ("wecom", "teams", "weixin", "imessage"):
        key = f"{channel}:kirocrew:direct:someone"
        assert is_channel_key(key), channel  # names a conversation...
        assert binding_key_for(key) is None, channel  # ...but is not armable


def test_a_unified_scope_key_is_never_bindable():
    """``unified:`` collapses several users' DMs into one bucket.

    It counts as a channel key (so the fixed-interval timer applies) but has no
    single conversation to deliver to, so a loop must not bind there.
    """
    from kiro_crew.autonudge import binding_key_for, is_channel_key

    assert is_channel_key("unified:kirocrew")
    assert binding_key_for("unified:kirocrew") is None


def test_every_nudge_able_channel_has_a_fire_adapter():
    """The roster and the adapters cannot drift apart.

    This is the invariant the first version of this change got wrong: ``webex:``
    was added to ``binding_key_for`` while ``_fire`` still handled only slack and
    discord, so an armed Webex loop was DELETED on its first cycle.
    """
    from kiro_crew.slack.gateway import GatewayOrchestrator

    for prefix in ("slack:", "discord:", "webex:"):
        channel = prefix.rstrip(":")
        assert hasattr(GatewayOrchestrator, f"_fire_{channel}_nudge"), channel


@pytest.mark.asyncio
async def test_structural_terminal_stop_from_on_fire_does_not_rearm(tmp_path):
    """When a fire cycle's callback STOPS the loop (as the gateway's
    structural-terminal guard does: ``update(active=False, ...)`` then returns
    a refused/undelivered result), ``_run_fire_cycle`` must NOT re-arm it.

    This is the end-to-end contract behind the malformed-storm fix. The gateway
    guard refuses to dispatch a nudge whose prior delivered turn was rejected as
    malformed and stops the loop; the service must honour that stop instead of
    treating an undelivered cycle as a transient failure to retry. Otherwise the
    loop would back off and fire again -- the very storm the guard exists to end.
    """
    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        # Stand in for _fire_dashboard_nudge hitting the structural-terminal
        # guard: it stops the loop and reports the cycle as NOT delivered.
        fired.append(loop)
        await service.update(loop.id, active=False, stopped_reason=_an.STRUCTURAL_TERMINAL_REASON)
        return False

    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="loop-structural",
        slot_key="chat-1-123",
        message="check the PR",
        idle_secs=30,
    )
    service._loops[loop.id] = loop
    try:
        await service._timer(loop, delay=0)
        assert fired == [loop], "the cycle must have fired once"
        # The loop was stopped by the callback, so it must not be re-armed and
        # must not count the undelivered cycle.
        assert loop.active is False
        assert loop.stopped_reason == _an.STRUCTURAL_TERMINAL_REASON
        assert loop.id not in service._timers, "a stopped loop must not re-arm"
        assert loop.cycle_count == 0, "an undelivered cycle must not be counted"
    finally:
        service.stop()


def test_structural_terminal_reason_is_replaceable():
    """A structural-terminal stop is system-imposed, so a later directive re-arm
    may displace it -- the remedy is a NEW conversation, which /clear provides."""
    loop = NudgeLoop(
        id="loop-x",
        slot_key="chat-1-123",
        message="m",
        idle_secs=30,
        active=False,
        stopped_reason=_an.STRUCTURAL_TERMINAL_REASON,
    )
    assert _an._stopped_row_is_replaceable(loop) is True


@pytest.mark.asyncio
async def test_structural_stop_does_not_overwrite_a_manual_pause(tmp_path):
    """A structural stop landing AFTER a manual pause must not clobber it.

    Race: the user pauses a loop (update active=False -> "manual") while a nudge
    cycle is in flight; that cycle then hits the malformed rejection and the fire
    path calls update(active=False, stopped_reason="structural_terminal"). If
    structural_terminal were NOT a terminal-bound reason, that second update
    would OVERWRITE the manual pause with a REPLACEABLE reason, so a later
    directive could revive a loop the user explicitly paused. Membership in
    _TERMINAL_BOUND_REASONS makes update's already-inactive no-op branch preserve
    the manual pause instead. (A manual pause is NOT replaceable, so the loop
    stays paused until the user acts.)
    """
    service = AutoNudgeService(base_dir=tmp_path)
    loop = NudgeLoop(id="loop-race", slot_key="chat-1-123", message="m", idle_secs=30)
    service._loops[loop.id] = loop
    try:
        # 1) user pause lands first
        await service.update(loop.id, active=False, stopped_reason="manual")
        assert loop.active is False
        assert loop.stopped_reason == "manual"
        # 2) the in-flight cycle's structural stop arrives after the pause
        await service.update(loop.id, active=False, stopped_reason=_an.STRUCTURAL_TERMINAL_REASON)
        # The manual pause is preserved (not clobbered), so it stays NOT
        # replaceable and a directive cannot silently revive it.
        assert loop.stopped_reason == "manual"
        assert _an._stopped_row_is_replaceable(loop) is False
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_message_change_advances_config_generation_and_fences_stale_stop(tmp_path):
    """The atomic (id, generation) fence: a structural stop carrying a generation
    captured BEFORE an instruction change must be refused; one carrying the
    current generation must apply. Covers the A->B->A race that value-identity
    on the message alone cannot tell apart.
    """
    service = AutoNudgeService(base_dir=tmp_path)
    loop = NudgeLoop(id="loop-gen", slot_key="chat-1-123", message="A", idle_secs=30)
    service._loops[loop.id] = loop
    try:
        assert loop.config_generation == 0
        fired_gen = loop.config_generation  # captured at fire time (A)

        # A -> B -> A via monitor_update; each real change advances the generation.
        await service.update(loop.id, message="B")
        assert loop.config_generation == 1
        await service.update(loop.id, message="A")
        assert loop.config_generation == 2, "re-committed A is a NEW generation"

        # A stale completion of the ORIGINAL A (fired_gen=0) is refused by the
        # fence, so the loop is NOT stopped even though its message is again "A".
        await service.update(
            loop.id,
            active=False,
            stopped_reason=_an.STRUCTURAL_TERMINAL_REASON,
            expected_generation=fired_gen,
        )
        assert loop.active is True, "a stale-generation structural stop was wrongly applied"

        # A completion carrying the CURRENT generation does apply.
        await service.update(
            loop.id,
            active=False,
            stopped_reason=_an.STRUCTURAL_TERMINAL_REASON,
            expected_generation=loop.config_generation,
        )
        assert loop.active is False
        assert loop.stopped_reason == _an.STRUCTURAL_TERMINAL_REASON

        # A no-op same-message save does NOT advance the generation.
        gen_before = loop.config_generation
        await service.update(loop.id, message="A")
        assert loop.config_generation == gen_before

        # Revival advances the generation (a fresh run, old verdict cannot follow).
        await service.update(loop.id, active=True)
        assert loop.active is True
        assert loop.config_generation == gen_before + 1
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "3", -1, True, 2.5, [1]])
async def test_load_normalizes_a_corrupt_config_generation_to_zero(tmp_path, bad):
    """A persisted config_generation that is null / string / negative / bool /
    non-int must be normalized to 0 at load, so a later `+= 1` or the equality
    fence cannot raise TypeError mid-mutation (partial update + HTTP 500)."""
    import json

    store = {
        "loops": [
            {
                "id": "loop-badgen",
                "slot_key": "chat-1-123",
                "message": "m",
                "idle_secs": 30,
                "active": True,
                "config_generation": bad,
            }
        ]
    }
    (tmp_path / "autonudge.json").write_text(json.dumps(store), encoding="utf-8")
    service = AutoNudgeService(base_dir=tmp_path)
    await service.start()
    try:
        loop = service._loops["loop-badgen"]
        assert loop.config_generation == 0
        assert isinstance(loop.config_generation, int) and not isinstance(
            loop.config_generation, bool
        )
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_load_keeps_a_valid_config_generation(tmp_path):
    """A valid non-negative int config_generation survives load unchanged."""
    import json

    store = {
        "loops": [
            {
                "id": "loop-goodgen",
                "slot_key": "chat-1-123",
                "message": "m",
                "idle_secs": 30,
                "active": True,
                "config_generation": 4,
            }
        ]
    }
    (tmp_path / "autonudge.json").write_text(json.dumps(store), encoding="utf-8")
    service = AutoNudgeService(base_dir=tmp_path)
    await service.start()
    try:
        assert service._loops["loop-goodgen"].config_generation == 4
    finally:
        service.stop()
