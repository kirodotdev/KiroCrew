"""Idle restored tabs do not cost a provider burst every chip TTL.

The owner dashboard re-reads the PR/MR status behind every sidebar chip. Before
the idle tier it treated every slot alike, so a workspace with dozens of tabs
restored from ``open_slots.json`` kept cycling ``gh pr view`` across its whole
history -- four at a time, sixteen admitted per minute -- with no agent work
running at all. These tests pin the tiers that replaced that:

* a slot is in use while a turn runs on it, and for a while after a turn ended on
  it or the owner opened it; every restored tab starts idle;
* chips only idle slots link are re-read on a slower clock, at most a couple per
  chip TTL across every owner socket, longest-unread first;
* a chip never read since boot still fills in at the normal pace;
* a turn boundary and an owner open still refresh at once.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import chat_persistence as cp
from kiro_crew.dashboard import state as dashboard_state
from kiro_crew.dashboard import ws as dashboard_ws
from kiro_crew.dashboard.chat_utils import _history_key_for
from kiro_crew.dashboard.handlers import source_providers
from kiro_crew.dashboard.source_providers import cache, chip_refresh, chip_status
from kiro_crew.testing.wait import async_wait_until, await_until

#: The idle tier's clock and per-TTL budget, as this file pins them.
_IDLE_TTL = 15 * 60
_IDLE_BUDGET = 2


def _pr(number: int) -> str:
    return f"https://github.com/acme/repo/pull/{number}"


@pytest.fixture(autouse=True)
def _clean_chip_state(_floor_monkeypatch):
    # The floor's own patcher, not the test's: a test that undoes its own
    # ``monkeypatch`` must not also lift this reset (D11).
    stores = (
        chip_status._check_cache,
        chip_status._check_inflight,
        chip_status._check_forced_at,
        chip_status._check_force_pending,
    )
    for store in stores:
        store.clear()
    _floor_monkeypatch.setattr(
        chip_refresh, "_idle_check_window", [float("-inf"), 0.0], raising=False
    )
    yield
    for store in stores:
        store.clear()


def _fake_reads(monkeypatch, now) -> list[str]:
    """Replace the provider read: record each URL read and store a fresh status."""
    started: list[str] = []

    async def _read(url: str, on_update=None) -> None:
        started.append(url)
        chip_status._check_inflight.discard(url)
        chip_status._check_cache[url] = (now(), {"state": "open"})

    monkeypatch.setattr(chip_refresh, "_refresh_check_status", _read)
    return started


async def _drain() -> None:
    loop = asyncio.get_running_loop()
    tasks = [task for task in chip_refresh._CHECK_TASKS if task.get_loop() is loop]
    if tasks:
        await await_until(asyncio.gather(*tasks), what="the chip reads")


# ── The idle scheduler ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_idle_rereads_spend_a_small_shared_budget_longest_unread_first(monkeypatch):
    clock = {"now": 100_000.0}
    monkeypatch.setattr(chip_refresh, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    started = _fake_reads(monkeypatch, lambda: clock["now"])
    # More idle chips than the pending cap, all read longer ago than the idle
    # clock; pull/1 the longest ago.
    urls = [_pr(n) for n in range(1, 41)]
    assert len(urls) > chip_refresh._CHECK_PENDING_MAX
    assert (chip_refresh._IDLE_CHECK_TTL_SECS, chip_refresh._IDLE_CHECK_PER_ROUND) == (
        _IDLE_TTL,
        _IDLE_BUDGET,
    )
    for n, url in enumerate(urls, start=1):
        chip_status._check_cache[url] = (clock["now"] - _IDLE_TTL - 100 + n, {"state": "open"})

    budget = _IDLE_BUDGET
    assert chip_refresh.schedule_idle_check_refresh(urls) == urls[:budget]
    await _drain()
    # A second owner socket's driver, inside the same chip TTL, gets nothing:
    # one budget for the gateway, not one per dashboard window.
    clock["now"] += chip_status._CHECK_TTL_SECS / 2
    assert chip_refresh.schedule_idle_check_refresh(urls) == []
    # The next TTL reads the next longest-unread, not the ones just read.
    clock["now"] += chip_status._CHECK_TTL_SECS
    assert chip_refresh.schedule_idle_check_refresh(urls) == urls[budget : 2 * budget]
    await _drain()
    assert started == urls[: 2 * budget]


@pytest.mark.asyncio
async def test_never_read_idle_chips_fill_in_up_to_the_free_pending_capacity(monkeypatch):
    """A restart leaves every chip unread. Those are read at the normal pace, not
    on the idle budget -- but never past the pending cap, whose overflow stamp
    would make an unread chip look read and park it on the slow clock blank."""
    monkeypatch.setattr(chip_refresh, "_refresh_check_status", AsyncMock(return_value=None))
    busy = {_pr(900 + n) for n in range(10)}
    chip_status._check_inflight.update(busy)
    urls = [_pr(n) for n in range(1, 41)]

    free = chip_refresh._CHECK_PENDING_MAX - len(busy)
    assert chip_refresh.schedule_idle_check_refresh(urls) == urls[:free]
    assert not any(url in chip_status._check_cache for url in urls[free:])
    await _drain()


@pytest.mark.asyncio
async def test_a_chip_whose_first_read_failed_is_retried_on_the_chip_ttl(monkeypatch):
    """A boot hiccup (``gh`` or the network not ready) leaves ``(stamp, None)``.
    That chip has never shown a status, so it is retried at the normal pace and
    outside the idle budget, not left blank for the slow clock."""
    clock = {"now": 200_000.0}
    monkeypatch.setattr(chip_refresh, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    monkeypatch.setattr(chip_refresh, "_refresh_check_status", AsyncMock(return_value=None))
    failed = [_pr(n) for n in range(1, 6)]
    just_failed = _pr(77)
    for url in failed:
        chip_status._check_cache[url] = (clock["now"] - chip_status._CHECK_TTL_SECS - 1, None)
    chip_status._check_cache[just_failed] = (clock["now"] - 1, None)
    assert len(failed) > _IDLE_BUDGET

    assert chip_refresh.schedule_idle_check_refresh([*failed, just_failed]) == failed
    await _drain()


@pytest.mark.parametrize(
    ("status", "age", "due"),
    [
        # Past the chip TTL but inside the idle clock: an idle chip waits.
        ({"state": "open"}, 5 * 60, False),
        ({"state": "open"}, _IDLE_TTL + 1, True),
        # Finished pull requests keep their own, longer clocks.
        ({"state": "merged"}, _IDLE_TTL + 1, False),
        ({"state": "closed"}, _IDLE_TTL + 1, False),
        ({"state": "closed"}, cache._CLOSED_TTL_SECS + 1, True),
    ],
)
def test_idle_clock_replaces_only_the_open_pull_request_ttl(status, age, due) -> None:
    now = 1_000_000.0
    entry = (now - age, status)
    assert chip_refresh._chip_refresh_due(entry, now, force=False, open_ttl=_IDLE_TTL) is due


# ── Which slots are idle ────────────────────────────────────────────────────

_KEYS = ["chat-1-alpha", "chat-2-beta", "chat-3-gamma"]


@pytest.fixture
def restored(tmp_path, monkeypatch):
    """Three tabs restored from ``open_slots.json``, each linking its own PR and
    the first two also sharing one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    seed = _make_state(tmp_path / "sessions")
    for n, key in enumerate(_KEYS, start=1):
        history_key = _history_key_for(key)
        seed.conversation_log.append(history_key, "user", f"{key} question")
        shared = f" and {_pr(99)}" if n < 3 else ""
        seed.conversation_log.append(history_key, "assistant", f"opened {_pr(n)}{shared}")
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": _KEYS, "ts": 0.0}))
    state = _make_state(tmp_path / "sessions")
    assert cp.restore_open_slots(state) == len(_KEYS)
    return state


def test_restored_tabs_start_idle_and_a_turn_boundary_moves_one_to_the_fast_tier(
    restored, monkeypatch
) -> None:
    state = restored
    every_chip = {_pr(1), _pr(2), _pr(3), _pr(99)}
    assert set(state.source_link_urls()) == every_chip
    assert state.idle_source_link_urls() == every_chip

    # The turn boundary still forces its read at once (an owner window is open)...
    state._owner_ws_clients.add(MagicMock(closed=False))
    forced: list[list[str]] = []
    monkeypatch.setattr(
        source_providers,
        "request_check_refresh_now",
        lambda urls, on_update=None: forced.append(urls),
    )
    monkeypatch.setattr(source_providers, "schedule_visibility_refresh", MagicMock())
    state.refresh_slot_source_status(_KEYS[0])
    assert forced and set(forced[0]) == {_pr(1), _pr(99)}

    # ...and keeps that slot on the fast tier. pull/99 is fast too: a chip any
    # slot in use links is not idle, whoever else links it.
    assert state.idle_source_link_urls() == {_pr(2), _pr(3)}


def test_the_fast_tier_lapses_after_the_activity_window(restored) -> None:
    state = restored
    state.note_source_activity(_KEYS[2])
    assert _pr(3) not in state.idle_source_link_urls()

    state._source_active_at[_KEYS[2]] -= dashboard_state._SOURCE_ACTIVE_WINDOW_SECS
    assert _pr(3) in state.idle_source_link_urls()
    assert _KEYS[2] not in state._source_active_at


def test_activity_stamps_never_outgrow_the_live_slots(restored) -> None:
    """Every write prunes too, so stamps for closed sessions cannot pile up when
    nothing reads the tiers (no owner socket, no slots list)."""
    state = restored
    state.note_source_activity(_KEYS[0])
    state.note_source_activity("chat-never-open")
    assert set(state._source_active_at) == {_KEYS[0]}

    state._slots.pop(_KEYS[0])  # the session closes
    state.note_source_activity(_KEYS[1])
    assert set(state._source_active_at) == {_KEYS[1]}
    assert len(state._source_active_at) <= len(state._slots)


@pytest.mark.asyncio
async def test_a_running_turn_keeps_its_slot_on_the_fast_tier(restored) -> None:
    state = restored
    running = asyncio.get_running_loop().create_future()
    state._slots[_KEYS[2]].task = running  # type: ignore[assignment]
    try:
        assert _pr(3) not in state.idle_source_link_urls()
    finally:
        running.cancel()
        state._slots[_KEYS[2]].task = None


# ── The HTTP and WebSocket callers ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_opening_a_session_refreshes_its_chips_and_keeps_them_fast(
    restored, monkeypatch
) -> None:
    state = restored
    scheduled: list[list[str]] = []
    monkeypatch.setattr(
        source_providers,
        "schedule_check_refresh",
        lambda urls, on_update=None: scheduled.append(urls),
    )
    monkeypatch.setattr(source_providers, "schedule_visibility_refresh", MagicMock())
    async with TestClient(TestServer(_make_app(state))) as client:
        response = await client.get(f"/api/chat/slots/{_KEYS[2]}")
        assert response.status == 200

    assert scheduled == [[_pr(3)]]
    assert _pr(3) not in state.idle_source_link_urls()


@pytest.mark.asyncio
async def test_a_non_owner_open_drives_no_read_and_changes_no_tier(restored, monkeypatch) -> None:
    state = restored
    # With an owner configured, the test client's local subject is not the owner.
    state.owner_id = "U_OWNER"
    refresh = MagicMock()
    monkeypatch.setattr(source_providers, "schedule_check_refresh", refresh)
    monkeypatch.setattr(source_providers, "schedule_visibility_refresh", MagicMock())
    async with TestClient(TestServer(_make_app(state))) as client:
        response = await client.get(f"/api/chat/slots/{_KEYS[2]}")
        assert response.status == 200

    refresh.assert_not_called()
    assert _pr(3) in state.idle_source_link_urls()


@pytest.mark.asyncio
async def test_slots_get_sends_idle_chips_to_the_idle_tier(restored, monkeypatch) -> None:
    state = restored
    state.note_source_activity(_KEYS[0])
    fast: list[list[str]] = []
    idle: list[list[str]] = []
    monkeypatch.setattr(
        source_providers, "schedule_check_refresh", lambda urls, on_update=None: fast.append(urls)
    )
    monkeypatch.setattr(source_providers, "schedule_visibility_refresh", MagicMock())
    monkeypatch.setattr(
        chip_refresh, "schedule_idle_check_refresh", lambda urls, on_update=None: idle.append(urls)
    )
    async with TestClient(TestServer(_make_app(state))) as client:
        response = await client.get("/api/chat/slots")
        assert response.status == 200

    assert [set(urls) for urls in fast] == [{_pr(1), _pr(99)}]
    assert [set(urls) for urls in idle] == [{_pr(2), _pr(3)}]


class _Request(dict):
    def __init__(self, state) -> None:
        super().__init__({"user": "U_OWNER", "app": "", "is_dashboard_user": True})
        self.app = {"state": state}


class _OwnerSocket:
    """Stays open until ``release`` is set, then ends the handler."""

    def __init__(self, release: asyncio.Event) -> None:
        self.closed = False
        self._release = release
        self._flags: dict = {"_is_dashboard_user": True}

    def __setitem__(self, key: str, value) -> None:
        self._flags[key] = value

    def __getitem__(self, key: str):
        return self._flags[key]

    def get(self, key: str, default=None):
        return self._flags.get(key, default)

    async def prepare(self, request) -> None:
        return None

    async def send_json(self, payload: dict) -> None:
        return None

    async def send_str(self, payload: str) -> None:
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self._release.wait()
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_two_owner_sockets_over_many_restored_tabs_stay_on_one_small_budget(
    monkeypatch,
) -> None:
    """The reported shape: far more PR-linked restored tabs than the pending cap,
    and more than one owner window open. Several refresh rounds later the idle
    chips have cost at most one round's idle budget, while the chip on the slot
    in use is still re-read on the chip TTL."""
    # The scheduler's own clock is frozen, so no runner pause can open a second
    # budget window between rounds; the cache stamps and the fake reads use it.
    frozen = 500_000.0
    monkeypatch.setattr(chip_refresh, "time", SimpleNamespace(monotonic=lambda: frozen))
    started = _fake_reads(monkeypatch, lambda: frozen)
    idle_urls = [_pr(n) for n in range(1, 41)]
    active_url = _pr(500)
    for url in idle_urls:
        chip_status._check_cache[url] = (frozen - _IDLE_TTL - 1, {"state": "open"})
    chip_status._check_cache[active_url] = (
        frozen - chip_status._CHECK_TTL_SECS - 1,
        {"state": "open"},
    )

    state = MagicMock()
    state.owner_id = "U_OWNER"
    state.serialize_slots.return_value = []
    state._yolo = False
    state.source_link_urls.return_value = [*idle_urls, active_url]
    state.idle_source_link_urls.return_value = set(idle_urls)

    release = asyncio.Event()
    sockets = iter([_OwnerSocket(release), _OwnerSocket(release)])
    monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
    monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: next(sockets))
    monkeypatch.setattr(source_providers, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source_providers, "schedule_visibility_refresh", MagicMock())
    monkeypatch.setattr(source_providers, "CHECK_STATUS_TTL_SECS", 0.01)

    handlers = asyncio.gather(
        dashboard_ws.api_ws(_Request(state)),  # type: ignore[arg-type]
        dashboard_ws.api_ws(_Request(state)),  # type: ignore[arg-type]
    )
    try:
        # Three rounds per socket, all inside one real chip TTL.
        await async_wait_until(
            lambda: state.source_link_urls.call_count >= 6,
            describe=lambda: f"rounds={state.source_link_urls.call_count}",
        )
    finally:
        release.set()
        await await_until(handlers, what="both owner sockets to close")
    await _drain()

    idle_reads = [url for url in started if url in set(idle_urls)]
    assert len(idle_reads) <= _IDLE_BUDGET, idle_reads
    assert active_url in started
