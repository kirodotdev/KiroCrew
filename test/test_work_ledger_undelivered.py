"""A dispatched worker whose work-tool call the harness never delivered is not a silent stall.

KAS answers a work-tool name it cannot resolve with ``Tool "work_report" is not
available.`` and never calls the server, so nothing lands on the item. The gateway
sees that failed call in the worker's own turn; these pin that it records it beside
the item from the real frames KAS sends, on the dashboard turn and on a channel
turn, that the patrol probe and the conductor hear of it, that a call which then
gets through clears it, and that a model cannot raise it with any other tool.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import undelivered_call_watch as ucw
from kiro_crew import work_ledger
from kiro_crew.acp._dispatch_parts.session_update_parser import parse_session_update
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO

CONDUCTOR = "chat-conductor-kas"
WORKER = "chat-worker-kas"
UNAVAILABLE = 'Tool "work_report" is not available.'

#: Captured live off KAS (kiro-cli 2.21.1, ``acp --agent-engine v3``): the
#: ``session/update`` pair for a model that called the bare name ``work_report``.
#: The call frame names the tool only in its title, with kind ``other`` and no
#: ``_meta.kiro`` identity; the result is ``failed`` with KAS's own answer.
KAS_UNRESOLVED_CALL = {
    "sessionUpdate": "tool_call",
    "toolCallId": "toolu_bdrk_01XqkspFhE4wFXPryaQ7CPDB",
    "title": "work_report",
    "kind": "other",
    "status": "in_progress",
    "_meta": {"kiro": {}},
}
KAS_UNRESOLVED_RESULT = {
    "sessionUpdate": "tool_call_update",
    "toolCallId": "toolu_bdrk_01XqkspFhE4wFXPryaQ7CPDB",
    "status": "failed",
    "rawOutput": UNAVAILABLE,
    "content": [{"type": "content", "content": {"type": "text", "text": UNAVAILABLE}}],
    "_meta": {"kiro": {}},
}


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def _fresh_order():
    """No test inherits a note another test left in flight."""
    from kiro_crew import work_ledger_undelivered as wlu

    wlu._pending.clear()
    wlu._cleared_at.clear()
    yield
    wlu._pending.clear()
    wlu._cleared_at.clear()


class _ManualClock:
    """Every stamp advances one microsecond, so the display stamp never rides the
    host's tick. Ordering reads no clock at all."""

    def __init__(self) -> None:
        self.ns = 1_790_000_000_000_000_000

    def stamp(self) -> str:
        from datetime import datetime, timezone

        self.ns += 1_000
        moment = datetime.fromtimestamp(self.ns // 1_000 / 1_000_000, tz=timezone.utc)
        return moment.isoformat(timespec="microseconds")


@pytest.fixture
def clock(monkeypatch) -> _ManualClock:
    """The store's display stamp, pinned through the facade."""
    manual = _ManualClock()
    monkeypatch.setattr(work_ledger, "_undelivered_stamp", manual.stamp)
    return manual


@pytest.fixture
def frozen_clock(monkeypatch) -> None:
    """The one clock the record reads, its display stamp, returns the same value
    every time, as on a coarse host clock. Ordering must not need it."""
    monkeypatch.setattr(work_ledger, "_undelivered_stamp", lambda: "2026-10-24T00:00:00+00:00")


def _parsed(*updates: dict) -> list:
    caches: dict = {
        name: {}
        for name in (
            "tool_input_cache",
            "shell_cache",
            "raw_params_cache",
            "mcp_server_name_cache",
            "tool_name_cache",
            "harness_tool_name_cache",
            "tool_kind_cache",
        )
    }
    return [event for update in updates for event in parse_session_update(update, **caches)]


def _bound_item(worker: str = WORKER) -> str:
    work_ledger.ensure_conductor(CONDUCTOR, goal="fix the adapters")
    item_id = str(
        work_ledger.apply_conductor_action(
            CONDUCTOR, "create", title="gitlab half", acceptance={"kind": "human_approval"}
        )["item"].item_id
    )
    work_ledger.apply_conductor_action(
        CONDUCTOR, "bind", item_id=item_id, worker_session_key=worker
    )
    return item_id


def _item(item_id: str):
    return work_ledger.read_work_item(CONDUCTOR, item_id)


def _kas_watch(monkeypatch, key: str = WORKER) -> ucw.UndeliveredCallWatch:
    monkeypatch.setattr(ucw, "_serves_kas", lambda _provider: True)
    return ucw.UndeliveredCallWatch(key, object())


class TestWhichFailuresCount:
    @pytest.mark.parametrize(
        ("called", "expected"),
        [
            ("work_report", "work_report"),
            ("work_brief", "work_brief"),
            ("@kirocrew-work/work_report", "work_report"),
            ("mcp__kirocrew-work__work_brief", "work_brief"),
        ],
    )
    def test_an_unresolved_worker_tool(self, called, expected):
        output = f'Tool "{called}" is not available.'
        assert (
            ucw.undelivered_work_tool(
                wire_title=called, kind="other", status="failed", output=output
            )
            == expected
        )

    def test_a_json_quoted_raw_output_reads_the_same(self):
        assert (
            ucw.undelivered_work_tool(
                wire_title="work_report",
                kind="other",
                status="failed",
                output=json.dumps(UNAVAILABLE),
            )
            == "work_report"
        )

    @pytest.mark.parametrize(
        ("wire_title", "kind", "status", "output"),
        [
            ("work_report", "other", "completed", UNAVAILABLE),
            ("work_report", "other", "failed", '{"error": "done_without_evidence"}'),
            ("work_report", "other", "failed", f"log line\n{UNAVAILABLE}\nmore"),
            ("work_report", "execute", "failed", UNAVAILABLE),
            ("work_brief", "other", "failed", UNAVAILABLE),
            ("", "other", "failed", UNAVAILABLE),
            (
                "work_ledger_record",
                "other",
                "failed",
                'Tool "work_ledger_record" is not available.',
            ),
            ("other_work_report", "other", "failed", 'Tool "other_work_report" is not available.'),
        ],
    )
    def test_everything_else(self, wire_title, kind, status, output):
        """A call the server answered (even with a refusal), the sentence inside other
        output, a shell command printing it, a frame naming a different tool, a
        conductor tool or another server's tool: none is an undelivered report."""
        assert (
            ucw.undelivered_work_tool(
                wire_title=wire_title, kind=kind, status=status, output=output
            )
            is None
        )


class TestTheRecord:
    def test_a_bound_worker_gets_the_flag(self):
        item_id = _bound_item()
        noted = work_ledger.note_undelivered(WORKER, "work_report")
        assert noted is not None and noted["item_id"] == item_id
        assert work_ledger.read_undelivered(CONDUCTOR, _item(item_id))["tool"] == "work_report"

    def test_the_dashboard_spelling_of_the_worker_key_reaches_the_same_record(self):
        item_id = _bound_item()
        assert work_ledger.note_undelivered(f"dashboard_{WORKER}", "work_brief") is not None
        assert work_ledger.read_undelivered(CONDUCTOR, _item(item_id))["tool"] == "work_brief"

    def test_an_unbound_session_records_nothing(self):
        _bound_item()
        assert work_ledger.note_undelivered("chat-not-a-worker", "work_report") is None

    def test_a_call_that_gets_through_clears_it_and_a_later_failure_shows_again(self, clock):
        """Ordered by when each ran, never by comparing stamps."""
        item_id = _bound_item()
        work_ledger.note_undelivered(WORKER, "work_brief")
        work_ledger.clear_undelivered(WORKER)
        assert work_ledger.read_undelivered(CONDUCTOR, _item(item_id)) is None
        work_ledger.apply_worker_report(CONDUCTOR, item_id, status="progress", summary="through")
        work_ledger.note_undelivered(WORKER, "work_report")
        assert work_ledger.read_undelivered(CONDUCTOR, _item(item_id))["tool"] == "work_report"

    def test_a_slow_note_of_a_failure_seen_before_a_clear_is_dropped(self, frozen_clock):
        """The failure was seen, the worker retried and got through (clear), and only
        then did the note run: it must not bring the recovered failure back. Every
        clock read is equal, so only the operation order can tell."""
        item_id = _bound_item()
        seen = work_ledger.undelivered_seen(WORKER)
        work_ledger.clear_undelivered(WORKER)
        assert work_ledger.note_undelivered(WORKER, "work_report", seen=seen) is None
        assert work_ledger.read_undelivered(CONDUCTOR, _item(item_id)) is None

    def test_a_failure_seen_after_a_clear_is_recorded(self, frozen_clock):
        """The other race order on the same equal clock: the clear ran first, so the
        failure seen after it shows."""
        item_id = _bound_item()
        work_ledger.note_undelivered(WORKER, "work_brief")
        work_ledger.clear_undelivered(WORKER)
        seen = work_ledger.undelivered_seen(WORKER)
        assert work_ledger.note_undelivered(WORKER, "work_report", seen=seen) is not None
        assert work_ledger.read_undelivered(CONDUCTOR, _item(item_id))["tool"] == "work_report"

    def test_a_clear_fences_only_the_notes_seen_before_it(self, frozen_clock):
        """Two failures in flight around one clear: the earlier is dropped, the
        later is kept, whichever note finishes first."""
        item_id = _bound_item()
        before = work_ledger.undelivered_seen(WORKER)
        work_ledger.clear_undelivered(WORKER)
        after = work_ledger.undelivered_seen(WORKER)
        assert work_ledger.note_undelivered(WORKER, "work_report", seen=after) is not None
        assert work_ledger.note_undelivered(WORKER, "work_brief", seen=before) is None
        assert work_ledger.read_undelivered(CONDUCTOR, _item(item_id))["tool"] == "work_report"

    def test_the_order_holds_only_notes_in_flight(self):
        """Once every seen failure is settled, nothing about the worker stays in memory."""
        from kiro_crew import work_ledger_undelivered as wlu

        _bound_item()
        work_ledger.note_undelivered(WORKER, "work_report")
        work_ledger.clear_undelivered(WORKER)
        assert WORKER not in wlu._cleared_at, "a clear with nothing in flight keeps nothing"
        seen = work_ledger.undelivered_seen(WORKER)
        work_ledger.clear_undelivered(WORKER)
        assert WORKER in wlu._pending and WORKER in wlu._cleared_at
        work_ledger.note_undelivered(WORKER, "work_report", seen=seen)
        work_ledger.note_undelivered(WORKER, "work_report")
        work_ledger.note_undelivered(
            WORKER, "not_a_worker_tool", seen=work_ledger.undelivered_seen(WORKER)
        )
        assert WORKER not in wlu._pending and WORKER not in wlu._cleared_at

    def test_a_delivered_call_with_nothing_recorded_writes_no_file(self):
        """Every session that never failed, which is every harness but KAS."""
        _bound_item()
        work_ledger.clear_undelivered(WORKER)
        assert not work_ledger.undelivered_path(WORKER).exists()
        assert not work_ledger.undelivered_dir().exists() or not any(
            work_ledger.undelivered_dir().iterdir()
        )

    def test_a_delivered_call_deletes_the_record(self):
        _bound_item()
        work_ledger.note_undelivered(WORKER, "work_report")
        assert work_ledger.undelivered_path(WORKER).exists()
        work_ledger.clear_undelivered(WORKER)
        assert not work_ledger.undelivered_path(WORKER).exists()

    def test_forgetting_the_closed_item_deletes_its_record(self):
        item_id = _bound_item()
        work_ledger.note_undelivered(WORKER, "work_report")
        work_ledger.forget_undelivered_for_item(WORKER, CONDUCTOR, item_id)
        assert not work_ledger.undelivered_path(WORKER).exists()

    def test_forgetting_another_item_keeps_this_ones_record(self):
        item_id = _bound_item()
        work_ledger.note_undelivered(WORKER, "work_report")
        work_ledger.forget_undelivered_for_item(WORKER, CONDUCTOR, "it_00000000")
        work_ledger.forget_undelivered_for_item(WORKER, "chat-other-conductor", item_id)
        assert work_ledger.read_undelivered(CONDUCTOR, _item(item_id))["tool"] == "work_report"

    def test_a_malformed_binding_key_never_raises(self, monkeypatch):
        _bound_item()
        monkeypatch.setattr(
            work_ledger, "read_binding", lambda _key: ("chat-\udcff", "it_abcd1234")
        )
        assert work_ledger.note_undelivered(WORKER, "work_report") is None

    def test_two_failures_in_one_second_are_two_stamps(self, clock):
        _bound_item()
        first = work_ledger.note_undelivered(WORKER, "work_report")["at"]
        second = work_ledger.note_undelivered(WORKER, "work_report")["at"]
        assert first != second

    def test_a_record_for_an_earlier_item_is_not_this_ones(self):
        first = _bound_item()
        work_ledger.note_undelivered(WORKER, "work_report")
        work_ledger.apply_conductor_action(CONDUCTOR, "close", item_id=first, state="abandoned")
        second = str(
            work_ledger.apply_conductor_action(
                CONDUCTOR, "create", title="next", acceptance={"kind": "human_approval"}
            )["item"].item_id
        )
        work_ledger.apply_conductor_action(
            CONDUCTOR, "bind", item_id=second, worker_session_key=WORKER
        )
        assert work_ledger.read_undelivered(CONDUCTOR, _item(second)) is None

    @pytest.mark.parametrize("tool", [[], {}, 7, None, "work_ledger_record"])
    def test_a_malformed_record_reads_as_absent(self, tool):
        item_id = _bound_item()
        work_ledger.note_undelivered(WORKER, "work_report")
        path = work_ledger.undelivered_path(WORKER)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["tool"] = tool
        path.write_text(json.dumps(raw), encoding="utf-8")
        assert work_ledger.read_undelivered(CONDUCTOR, _item(item_id)) is None

    def test_the_record_carries_no_worker_prose(self):
        _bound_item()
        work_ledger.note_undelivered(WORKER, "work_report")
        raw = json.loads(work_ledger.undelivered_path(WORKER).read_text(encoding="utf-8"))
        assert set(raw) == {"schema", "conductor_slot_key", "item_id", "tool", "at"}

    def test_it_lives_outside_every_conductor_directory_and_the_sweep_skips_it(self):
        """A rebuild prunes item files the crew log does not know, and the sweep
        reports any root directory it cannot name as a damaged store."""
        from datetime import datetime, timezone

        from kiro_crew import ledger_sweep

        _bound_item()
        work_ledger.note_undelivered(WORKER, "work_report")
        conductor = work_ledger.conductor_dir(CONDUCTOR).resolve()
        assert conductor not in work_ledger.undelivered_path(WORKER).resolve().parents
        candidates, _kept = ledger_sweep._scan_work_ledgers(
            older_than_days=0, now=datetime.now(timezone.utc)
        )
        assert all(Path(c.path).name != "undelivered" for c in candidates)


class TestTheWatch:
    def test_the_watch_judges_the_same_worker_tools_the_store_records(self):
        assert ucw._WORKER_TOOLS == work_ledger.WORKER_TOOLS

    def test_the_watch_keeps_only_bounded_worker_tool_frames(self, monkeypatch):
        """The per-turn map holds a bounded id and a worker-tool constant, never
        the backend's own strings: an unrelated or oversized frame is not kept."""
        watch = _kas_watch(monkeypatch)
        (call,) = _parsed(KAS_UNRESOLVED_CALL)
        watch.on_call(call)
        assert list(watch._frames.values()) == ["work_report"]

        for frame in (
            dict(KAS_UNRESOLVED_CALL, toolCallId="other-title", title="x" * 100_000),
            dict(KAS_UNRESOLVED_CALL, toolCallId="y" * (ucw._MAX_CALL_ID_LEN + 1)),
            dict(KAS_UNRESOLVED_CALL, toolCallId="not-a-worker-tool", title="read_file"),
            dict(KAS_UNRESOLVED_CALL, toolCallId="a-shell-call", kind="execute"),
        ):
            (event,) = _parsed(frame)
            watch.on_call(event)
        assert set(watch._frames) == {call.tool_call_id}

    def test_the_watch_limits_hold_at_their_boundaries(self, monkeypatch):
        watch = _kas_watch(monkeypatch)
        at_limit = "i" * ucw._MAX_CALL_ID_LEN
        (event,) = _parsed(dict(KAS_UNRESOLVED_CALL, toolCallId=at_limit))
        watch.on_call(event)
        assert at_limit in watch._frames

        full = _kas_watch(monkeypatch)
        for n in range(ucw._MAX_WATCHED_CALLS + 1):
            (event,) = _parsed(dict(KAS_UNRESOLVED_CALL, toolCallId=f"call-{n}"))
            full.on_call(event)
        assert len(full._frames) == ucw._MAX_WATCHED_CALLS

    def test_the_live_kas_frames_are_recorded(self, monkeypatch):
        item_id = _bound_item()
        watch = _kas_watch(monkeypatch)
        call, result = _parsed(KAS_UNRESOLVED_CALL, KAS_UNRESOLVED_RESULT)
        watch.on_call(call)
        noted = asyncio.run(watch.on_result(result))
        assert noted is not None and noted["item_id"] == item_id

    def test_a_shell_call_printing_the_sentence_is_not(self, monkeypatch):
        _bound_item()
        watch = _kas_watch(monkeypatch)
        call = dict(KAS_UNRESOLVED_CALL, kind="execute", rawInput={"command": "work_report"})
        events = _parsed(call, KAS_UNRESOLVED_RESULT)
        watch.on_call(events[0])
        assert asyncio.run(watch.on_result(events[1])) is None

    def test_a_bookkeeping_failure_never_reaches_the_turn(self, monkeypatch):
        _bound_item()
        watch = _kas_watch(monkeypatch)

        async def _boom(*_a, **_kw):
            raise UnicodeEncodeError("utf-8", "x", 0, 1, "surrogate")

        monkeypatch.setattr(ucw, "record_undelivered_call", _boom)
        call, result = _parsed(KAS_UNRESOLVED_CALL, KAS_UNRESOLVED_RESULT)
        watch.on_call(call)
        assert asyncio.run(watch.on_result(result)) is None

    def test_off_kas_nothing_is_watched(self):
        _bound_item()
        watch = ucw.UndeliveredCallWatch(WORKER, object())
        call, result = _parsed(KAS_UNRESOLVED_CALL, KAS_UNRESOLVED_RESULT)
        watch.on_call(call)
        assert asyncio.run(watch.on_result(result)) is None

    @pytest.mark.parametrize(
        ("backend", "expected"), [(ACP_BACKEND_KAS, True), (ACP_BACKEND_KIRO, False)]
    )
    def test_kas_is_read_off_the_provider_label(self, backend, expected):
        from kiro_crew.providers.acp import AcpProvider

        class _Provider(AcpProvider):
            def __init__(self) -> None:
                self._client = SimpleNamespace(backend=backend)

        assert ucw._serves_kas(_Provider()) is expected
        assert ucw._serves_kas(MagicMock()) is False


class TestTheConductorHearsOfIt:
    def test_the_patrol_probe_wakes_once_with_a_structural_brief(self):
        from kiro_crew.probes.work_ledger import WorkLedgerProbe

        item_id = _bound_item()
        work_ledger.note_undelivered(WORKER, "work_report")

        class _Ctx:
            message = json.dumps({"conductor": CONDUCTOR})

            class job:  # noqa: N801 - the attribute the kernel reads
                id = "loop-1"

        probe = WorkLedgerProbe(worker_running=lambda _k: True)
        probe.identity(_Ctx())
        briefs = [obs.brief for obs in probe.observe(_Ctx()).observations]
        assert (
            f"[work-ledger wake] item={item_id} reason=report_undelivered tool=work_report"
            in briefs
        )
        again = probe.observe(_Ctx())
        assert not any("report_undelivered" in obs.brief for obs in again.observations)

    def test_a_note_stopped_before_its_thread_starts_is_settled(self, monkeypatch):
        """A turn cancelled while the note waits for a thread leaves nothing pending."""
        from kiro_crew import work_ledger_undelivered as wlu

        _bound_item()

        async def _cancelled(*_a, **_kw):
            raise asyncio.CancelledError

        monkeypatch.setattr(asyncio, "to_thread", _cancelled)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(ucw.record_undelivered_call(WORKER, "work_report"))
        assert WORKER not in wlu._pending

    def test_a_note_already_running_when_the_turn_is_cancelled_is_still_fenced(self, monkeypatch):
        """Cancelling the turn does not stop a note whose thread has started, so the
        note must stay in flight: a clear that lands meanwhile still fences it."""
        import threading

        from kiro_crew import work_ledger_undelivered as wlu

        item_id = _bound_item()
        real = wlu.note_undelivered
        go = threading.Event()

        async def _go():
            loop = asyncio.get_running_loop()
            started, finished = loop.create_future(), loop.create_future()

            def _slow(*args, **kwargs):
                loop.call_soon_threadsafe(started.set_result, None)
                go.wait(10)
                noted = real(*args, **kwargs)
                loop.call_soon_threadsafe(finished.set_result, noted)
                return noted

            monkeypatch.setattr(work_ledger, "note_undelivered", _slow)
            task = asyncio.create_task(ucw.record_undelivered_call(WORKER, "work_report"))
            await started
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            work_ledger.clear_undelivered(WORKER)
            go.set()
            return await finished

        assert asyncio.run(_go()) is None
        assert work_ledger.read_undelivered(CONDUCTOR, _item(item_id)) is None
        assert WORKER not in wlu._pending

    def test_recording_pulls_the_conductor_forward(self, monkeypatch):
        from kiro_crew import conductor_wake

        _bound_item()
        pushed: list[tuple[str, str]] = []
        monkeypatch.setattr(
            conductor_wake,
            "wake_for_undelivered_report",
            lambda conductor, item: pushed.append((conductor, item)),
        )
        noted = asyncio.run(ucw.record_undelivered_call(WORKER, "work_report"))
        assert pushed == [(CONDUCTOR, noted["item_id"])]

    def test_the_pull_forward_schedules_the_conductors_fire(self, monkeypatch):
        from kiro_crew import autonudge, conductor_wake

        fired: list[tuple[str, str]] = []

        async def _fire(_svc, conductor, item_id=""):
            fired.append((conductor, item_id))
            return "loop-1"

        async def _go():
            loop = asyncio.get_running_loop()
            monkeypatch.setattr(autonudge, "get_instance", lambda: object())
            monkeypatch.setattr(conductor_wake, "_service_loop", lambda _svc: loop)
            monkeypatch.setattr(conductor_wake, "_fire", _fire)
            future = conductor_wake.wake_for_undelivered_report(CONDUCTOR, "it_abcd1234")
            assert future is not None
            await asyncio.wrap_future(future)

        asyncio.run(_go())
        assert fired == [(CONDUCTOR, "it_abcd1234")]


def test_a_channel_turn_records_it_too(monkeypatch):
    """A dispatched worker resumed on a channel runs through the messaging
    ``TurnDriver``, not the dashboard turn; its undelivered report is heard there."""
    from kiro_crew.messaging import TransportCapabilities, TurnDriver
    from kiro_crew.messaging.renderer import Renderer

    item_id = _bound_item()
    monkeypatch.setattr(ucw, "_serves_kas", lambda _provider: True)

    class _Provider:
        async def stream(self, _message):
            for event in _parsed(KAS_UNRESOLVED_CALL, KAS_UNRESOLVED_RESULT):
                yield event

    class _Quiet(Renderer):
        async def on_text_chunk(self, text): ...

        async def on_thinking(self, text): ...

        async def on_tool_call(self, tool_call_id, title, tool_kind="", tool_purpose=""): ...

        async def on_prompt_choice(
            self, options, request_id, tool_title="", tool_purpose="", tool_input=""
        ): ...

        async def on_compaction(self, pct): ...

        async def on_done(self, stop_reason=""): ...

    driver = TurnDriver(_Provider(), _Quiet(TransportCapabilities()), undelivered_watch_key=WORKER)
    asyncio.run(driver.run("report"))
    assert work_ledger.read_undelivered(CONDUCTOR, _item(item_id))["tool"] == "work_report"


def test_an_unattended_turn_records_it_too(monkeypatch):
    """Unattended turns (a Slack nudge, a cron) stream through ``stream_and_collect``."""
    from kiro_crew.llm_helpers_parts.turn_stream import stream_and_collect

    item_id = _bound_item()
    monkeypatch.setattr(ucw, "_serves_kas", lambda _provider: True)

    class _Provider:
        async def stream(self, _message, **_kw):
            for event in _parsed(KAS_UNRESOLVED_CALL, KAS_UNRESOLVED_RESULT):
                yield event

    asyncio.run(
        stream_and_collect(_Provider(), "go", retry_transient=False, undelivered_watch_key=WORKER)
    )
    assert work_ledger.read_undelivered(CONDUCTOR, _item(item_id))["tool"] == "work_report"


@pytest.mark.asyncio
async def test_the_dashboard_turn_records_it_from_the_live_frames(tmp_path: Path, monkeypatch):
    """End to end through the dashboard turn, fed the events Crew's own parser makes
    of KAS's frames for a bare ``work_report``."""
    from chat_test_helpers import _make_state

    from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent
    from kiro_crew.dashboard.chat_runner import _run_chat

    item_id = _bound_item()
    # The mocked client is no provider class, so the KAS label is read through
    # the seam ``test_kas_is_read_off_the_provider_label`` pins.
    monkeypatch.setattr(ucw, "_serves_kas", lambda _provider: True)
    events = [
        *_parsed(KAS_UNRESOLVED_CALL, KAS_UNRESOLVED_RESULT),
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="Opened the PR."),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    state = _make_state(tmp_path)
    client = MagicMock()
    client.session_id = "acp-kas-worker"
    client.shutdown = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=10.0)

    async def _stream(*_a, **_kw):
        for event in events:
            yield event

    client.stream = _stream
    client.stream_command = _stream
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.compaction_due_after_turn = MagicMock(return_value=False)
    state.sessions.effective_autocompact_pct = MagicMock(return_value=80.0)
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.sessions.record_failure = AsyncMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    slot = state.get_or_create_slot(WORKER)
    slot.append("user", "report when done", "msg msg-u")

    await _run_chat(state, slot, "report when done")

    flag = work_ledger.read_undelivered(CONDUCTOR, _item(item_id))
    assert flag is not None and flag["tool"] == "work_report"
