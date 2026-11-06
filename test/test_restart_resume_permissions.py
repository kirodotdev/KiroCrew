"""The error pass and a chat's approval mode: changed in neither direction.

Sibling of ``test_restart_resume.py`` (which holds the shared slot/state helpers,
the held-sleep double and the ``quiet_dispatch`` fixture this module imports).
RFC §4.6 records as an accepted trade-off that a Trusted chat resumed after an
error keeps its Trust; this pins the pass to both halves of that rule -- it
restores no Trust a restart dropped and downgrades none the person holds -- and
to passing no mode to ``continue_slot_turn``. Also the boot pass's side of the
same rule for a crewmate thread: the turn admission seeds the thread's declared
mode before the resumed turn claims a provider, so the pass tells it nothing.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from test_restart_resume import (  # noqa: F401 -- the fixture is requested by name
    _RETRY_INVITED_ROW,
    _cfg,
    _cut_off,
    _HeldPause,
    _HeldSleep,
    _mock_state,
    _slot,
    quiet_dispatch_fixture,
)
from test_restart_resume_budget import _FakeLog

from kiro_crew.dashboard import restart_resume
from kiro_crew.dashboard.chat_utils import (
    TRANSIENT_NOTICE_GIVE_UP,
    TRANSIENT_NOTICE_META_KEY,
    effective_session_key,
)
from kiro_crew.dashboard.slot_persistence import metadata_codec as codec
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.dashboard.system_notices import (
    APPROVAL_MODE_DROPPED_KIND,
    AUTO_RESUME_PAUSED_KIND,
)
from kiro_crew.members import DM_SLOT_MODE

_DROPPED = APPROVAL_MODE_DROPPED_KIND  # the row kind both directions of the notice share


class TestErrorPassApprovalMode:
    @pytest.mark.asyncio
    async def test_the_error_pass_neither_drops_nor_grants_trust(self, monkeypatch, quiet_dispatch):
        # Accepted trade-off (RFC §4.6): a Trusted chat resumed after an error keeps
        # its Trust -- the pass changes a chat's approval mode in neither direction
        # and passes none to continue_slot_turn, so the turn runs under exactly what
        # the person last picked. This pins that; a pass that quietly downgraded a
        # held Trust to Ask, or re-applied a dropped one, would fail it.
        monkeypatch.setattr(restart_resume, "_load_settings", lambda: (True, 5, 3600))
        held = _HeldSleep()
        monkeypatch.setattr(restart_resume, "_backoff_sleep", held)
        trusted = _cut_off(
            _slot("trusted"), kind={TRANSIENT_NOTICE_META_KEY: TRANSIENT_NOTICE_GIVE_UP}
        )
        trusted._trust = True
        trusted._trust_reads = False
        asking = _cut_off(
            _slot("asking"), kind={TRANSIENT_NOTICE_META_KEY: TRANSIENT_NOTICE_GIVE_UP}
        )
        asking._trust = False
        asking._trust_reads = False

        for slot in (trusted, asking):
            held = _HeldSleep()
            monkeypatch.setattr(restart_resume, "_backoff_sleep", held)
            assert restart_resume.schedule_after_error(_mock_state(slot), slot) is True
            await asyncio.wait_for(held.entered.wait(), 30)
            held.release.set()
            await asyncio.wait_for(slot._auto_resume_task, 30)

        assert [c["slot"] for c in quiet_dispatch] == ["trusted", "asking"]
        assert (trusted._trust, trusted._trust_reads) == (True, False)
        assert (asking._trust, asking._trust_reads) == (False, False)
        for call in quiet_dispatch:
            assert not {k for k in call if "trust" in k.lower() or "approval" in k.lower()}
            assert call["directive_user_origin"] is False


class TestNoticeReachesAnOpenDashboard:
    def test_the_dropped_mode_notice_is_broadcast_live_like_the_pause_notice(self):
        # A tab reconnects as soon as the gateway listens, before the boot task
        # runs; the notice must reach it on the wire, not only on a transcript
        # reload, or the person sees the resumed turn (and its approval card)
        # without the row that says why. The pause notice already broadcasts;
        # the two rows of the same pass must behave alike.
        delivered: list[tuple[str, str]] = []
        slot = _slot()
        slot._on_message = lambda key, msg: delivered.append((key, msg["meta"]["kind"]))
        slot._approval_mode_recorded = "trust"
        slot._trust = False
        slot._trust_reads = False
        paused = _slot("paused")
        paused._on_message = lambda key, msg: delivered.append((key, msg["meta"]["kind"]))

        assert restart_resume.approval_mode_dropped_notice(slot) is True
        restart_resume._notice_budget_exhausted(
            _mock_state(paused), paused, used=5, limit=5, window_secs=3600
        )

        kinds = [kind for _, kind in delivered]
        assert AUTO_RESUME_PAUSED_KIND in kinds
        assert APPROVAL_MODE_DROPPED_KIND in kinds
        assert slot.messages[-1]["meta"]["kind"] == APPROVAL_MODE_DROPPED_KIND


def _armed(slot: _ChatSlot) -> MagicMock:
    slot._auto_resume_stamps = [time.time()]
    task = MagicMock()
    task.done.return_value = False
    slot._auto_resume_task = task
    return task


class TestWhoCountsAsAPerson:
    @pytest.mark.asyncio
    async def test_a_crons_continue_leaves_the_budget_alone_like_its_send(
        self, tmp_path, monkeypatch
    ):
        """Continue and send agree on who counts as a person.

        The send route resets the budget only with no app AND no cron creator;
        Continue read only the app, so a cron-attested internal caller (no app)
        counted as a person there and could reset the budget that bounds exactly
        the resumes no person asked for. Same rule on both routes now.
        """
        from chat_test_helpers import _make_app

        from kiro_crew.dashboard import chat_handlers

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

        runs: dict[str, dict] = {}
        ran = asyncio.Event()

        async def fake_run_chat(st, sl, msg, **kwargs):
            runs[sl.key] = kwargs
            sl.append("chunk", "ack", "chunk")
            if len(runs) == 2:
                ran.set()

        # Continue dispatches through the queue drain, which reads the runner's
        # binding (the send route dispatches with the handlers' own); stub both.
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", fake_run_chat)

        async def creator_from_header(request):
            if request.get("internal_auth") is not True:
                return ""
            key = request.headers.get("X-Session-Key", "")
            return key if key.startswith("cron:") else ""

        monkeypatch.setattr(chat_handlers, "cron_slot_creator", creator_from_header)

        @web.middleware
        async def _internal(request, handler):
            request["internal_auth"] = True
            request["peer_verified"] = True
            return await handler(request)

        state = _make_state(tmp_path)
        by_cron = _cut_off(state.get_or_create_slot("by-cron"), kind=_RETRY_INVITED_ROW)
        cron_task = _armed(by_cron)
        stamps = list(by_cron._auto_resume_stamps)
        by_person = _cut_off(state.get_or_create_slot("by-person"), kind=_RETRY_INVITED_ROW)
        person_task = _armed(by_person)

        app = _make_app(state)
        app.router.add_post("/api/chat/slots/{slot}/continue", chat_handlers.api_chat_slot_continue)
        app.middlewares.insert(0, _internal)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/by-cron/continue",
                headers={"X-Session-Key": "cron:nightly"},
                timeout=None,
            )
            assert resp.status == 200, await resp.text()
            resp.close()
            resp = await client.post("/api/chat/slots/by-person/continue", timeout=None)
            assert resp.status == 200, await resp.text()
            resp.close()
            # Both presses dispatched a turn; wait until the fake runner saw both.
            await asyncio.wait_for(ran.wait(), 30)

        # The cron's press dispatched the turn but, like its send, is not a
        # person's action: stamps and the armed retry are as it found them.
        assert by_cron._auto_resume_stamps == stamps
        assert by_cron._auto_resume_task is cron_task
        cron_task.cancel.assert_not_called()
        # Only the budget question changed. The turn's provenance is the send
        # route's ``not request_app`` for a cron as for anyone, so directive
        # handling and the drain see exactly what a cron's send gives them.
        assert runs["by-cron"]["_directive_user_origin"] is True
        assert runs["by-person"]["_directive_user_origin"] is True
        # The person's press is: budget over, armed retry gone.
        assert by_person._auto_resume_stamps == []
        assert by_person._auto_resume_task is None
        person_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mark", ["agent", {"bad": 1}])
    async def test_an_agent_origin_send_leaves_the_budget_alone(self, tmp_path, monkeypatch, mark):
        """A hub conductor's send to a worker arrives owner-authenticated with the
        body key ``turn_origin`` (``dashboard/agent_origin.py``): no app token, no
        cron creator, and no person. It must not refill the budget that bounds
        what no person asked for -- a conductor that keeps sending to a worker
        would otherwise lift the per-chat cap with nobody involved. A malformed
        value reads as marked too, so it is pinned alongside the defined one."""
        from chat_test_helpers import _make_app

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

        async def fake_run_chat(st, sl, msg, **kwargs):
            sl.append("chunk", "ack", "chunk")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
        state = _make_state(tmp_path)
        worker = _cut_off(state.get_or_create_slot("worker"), kind=_RETRY_INVITED_ROW)
        task = _armed(worker)
        epoch = worker._auto_resume_epoch
        stamps = list(worker._auto_resume_stamps)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={"message": "hello", "slot": "worker", "turn_origin": mark},
                timeout=None,
            )
            assert resp.status == 200, await resp.text()  # the send itself is accepted
            resp.close()

        assert worker._auto_resume_stamps == stamps
        assert worker._auto_resume_task is task
        assert worker._auto_resume_epoch == epoch
        task.cancel.assert_not_called()


class TestStopDuringTheBootPass:
    """A Stop pressed while the boot pass waits is the person's answer, card or no card."""

    @pytest.mark.asyncio
    async def test_a_stop_on_the_idle_chat_during_the_pause_skips_it(
        self, tmp_path, monkeypatch, quiet_dispatch
    ):
        """The restored chat is idle, so its Stop takes the no-op branch and writes
        no Stop card; what the pass re-reads after its settle is the marker flag,
        which the Stop must clear -- the way a send's ``reset_budget`` does --
        or the pass resumes a chat the person just stopped. The spent budget
        stands: a Stop is not a reset."""
        from chat_test_helpers import _make_app

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.conversation_log = _FakeLog(True)  # the budget write succeeds: nothing else refuses
        slot = _cut_off(state.get_or_create_slot("s1"))
        slot._auto_resume_stamps = [time.time() - 30.0]
        held = _HeldPause()
        monkeypatch.setattr("kiro_crew.dashboard.cautious_boot.pause_before", held)

        task = asyncio.ensure_future(restart_resume.resume_after_restart(state, _cfg()))
        await asyncio.wait_for(held.entered.wait(), 30)  # the pass is inside its pause
        async with TestClient(TestServer(_make_app(state))) as client:
            response = await client.post("/api/chat/slots/s1/stop")
            assert response.status == 200
            assert (await response.json()).get("info") == "not running"  # the no-op branch
        held.release.set()

        assert await asyncio.wait_for(task, 30) == 0
        assert quiet_dispatch == []
        assert slot._restart_cut_turn is False
        assert restart_resume.restart_candidate(slot) is False
        assert not any(r.get("kind") == "stop" for r in slot.messages)  # no card was written
        assert len(slot._auto_resume_stamps) == 1  # a Stop is not a reset: the count stands


class TestBootPassCrewmateThread:
    def test_the_resumed_turns_admission_re_applies_the_declared_mode(self, tmp_path, monkeypatch):
        # A crewmate thread whose recorded mode is the one its profile declares is
        # told nothing, cut turn or not: ``crewmate_approval.seed_for_turn`` gives
        # the restored thread that mode as the resumed turn is admitted, before it
        # claims a provider, so the turn runs under what the person last saw (Opus
        # review, rev 56). Pinned on the real seed with a restored thread, and on
        # the admission's call to it, which is what makes the silence right.
        from kiro_crew.dashboard.crewmate_approval import seed_for_turn
        from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        thread = _cut_off(_ChatSlot("dm", agent="mate", mode=DM_SLOT_MODE))
        thread._approval_mode_recorded = "trust"
        state._slots[thread.key] = thread
        cfg = SimpleNamespace(
            dashboard=_cfg().dashboard, agents={"mate": SimpleNamespace(approval_mode="trust")}
        )
        conductor = SimpleNamespace(
            selection_kind="member",
            template_id=MATE_CONDUCTOR_AGENT,
            member_id=None,
            selection_name="mate",
        )

        told = restart_resume.approval_mode_dropped_notice(thread, cfg=cfg)
        trust_before_the_turn = thread._trust
        seeded = seed_for_turn(state, thread, cfg, conductor)

        assert (told, trust_before_the_turn) == (False, False)  # the notice restores nothing
        assert seeded is True and thread._trust is True
        assert all(
            (m.get("meta") or {}).get("kind") != APPROVAL_MODE_DROPPED_KIND for m in thread.messages
        )
        admission = Path(restart_resume.__file__).parent / "chat_turn" / "turn_admission.py"
        assert "seed_for_turn(state, slot, loaded_cfg, execution_context)" in admission.read_text()

    @staticmethod
    def _worker_execution():
        from kiro_crew.mate_worker import MATE_WORKER_AGENT

        return SimpleNamespace(
            selection_kind="template",
            template_id=MATE_WORKER_AGENT,
            member_id="m1",
            selection_name="",
        )

    @staticmethod
    def _conductor_execution():
        from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT

        return SimpleNamespace(
            selection_kind="member",
            template_id=MATE_CONDUCTOR_AGENT,
            member_id="m1",
            selection_name="mate",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("shape", ["worker", "conductor", "unreadable"])
    async def test_the_pass_decides_the_exemption_from_the_execution_record(
        self, monkeypatch, shape
    ):
        # A crewmate's worker, and a conductor chat outside the mate's own thread,
        # are plain slots: nothing on the slot says whose they are. The admission
        # seed finds the crewmate through the session's execution record
        # (``crewmate_name_for``), so the pass reads the same record to decide
        # the exemption (Opus review, rev 57: keyed on the thread's slot shape, it
        # told a worker "now on Ask every time" while the admission put Trust
        # back). A record that cannot be read admits no turn, so nothing seeds
        # and that chat is told like any other.
        crewmate = _cut_off(_slot("crewmate"))
        crewmate._approval_mode_recorded = "trust"
        plain = _cut_off(_slot("plain"))
        plain._approval_mode_recorded = "trust"
        executions = {"worker": self._worker_execution(), "conductor": self._conductor_execution()}

        def read(key):
            if key != effective_session_key(crewmate):
                return None
            if shape == "unreadable":
                raise RuntimeError("session record is unreadable")
            return executions[shape]

        monkeypatch.setattr("kiro_crew.execution_context.read_session_execution", read)
        loaded: list[str] = []

        async def fake_load(slot):
            loaded.append(slot.key)

        monkeypatch.setattr(restart_resume, "load_window_off_loop", fake_load)
        cfg = SimpleNamespace(
            dashboard=_cfg().dashboard,
            agents={"mate": SimpleNamespace(approval_mode="trust", member_id="m1")},
        )
        state = _mock_state(crewmate, plain)

        told = await restart_resume.notice_dropped_approval_modes(state, cfg)

        crewmate_told = any(
            (m.get("meta") or {}).get("kind") == APPROVAL_MODE_DROPPED_KIND
            for m in crewmate.messages
        )
        if shape == "unreadable":
            assert (told, crewmate_told, sorted(loaded)) == (2, True, ["crewmate", "plain"])
        else:
            assert (told, crewmate_told, loaded) == (1, False, ["plain"])
        assert plain.messages[-1]["meta"]["kind"] == APPROVAL_MODE_DROPPED_KIND

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("recorded", "cut", "shape", "told_raised"),
        [
            # The finding: Ask every time last seen, Trust seeded, the turn cut.
            ("normal", True, "conductor", True),
            ("normal", True, "worker", True),
            # Nothing seeds a plain chat: its resumed turn waits at the card.
            ("normal", True, "plain", False),
            # No automatic resume: the person's own next turn seeds it, under their eyes.
            ("normal", False, "conductor", False),
            # An absent record says nothing about what was seen.
            ("", True, "conductor", False),
            # The declared mode comes back: nothing changed.
            ("trust", True, "conductor", False),
        ],
    )
    async def test_a_mate_chat_the_pass_would_resume_under_a_grant_it_last_showed_as_ask_is_told(
        self, monkeypatch, recorded, cut, shape, told_raised
    ):
        """The inverse direction of the notice (Opus review, rev 70). A person set a
        mate's chat to Ask every time in the composer -- in memory, so the line
        records ``normal`` -- and the restart cut its turn. The admission seed
        puts the profile's grant back as the resumed turn is admitted, so the
        boot pass would run that turn's tools under Trust with nobody there and,
        before this, nobody was told: the notice covered only a dropped grant.
        Now the pass tells that direction too, before it resumes, for exactly
        the chats it would resume. It still seeds and skips nothing (§4.6)."""
        slot = _cut_off(_slot("chat"), kind=_RETRY_INVITED_ROW, marker=cut)
        slot._approval_mode_recorded = recorded
        execution = {
            "conductor": self._conductor_execution(),
            "worker": self._worker_execution(),
            "plain": None,
        }[shape]
        monkeypatch.setattr(
            "kiro_crew.execution_context.read_session_execution", lambda key: execution
        )

        async def fake_load(slot):
            pass

        monkeypatch.setattr(restart_resume, "load_window_off_loop", fake_load)
        audit = MagicMock()
        monkeypatch.setattr(restart_resume, "_audit", audit)
        cfg = SimpleNamespace(
            dashboard=_cfg().dashboard,
            agents={"mate": SimpleNamespace(approval_mode="", member_id="m1")},  # unset: Trust
        )
        state = _mock_state(slot)

        told = await restart_resume.notice_dropped_approval_modes(state, cfg)

        rows = [m for m in slot.messages if (m.get("meta") or {}).get("kind") == _DROPPED]
        assert (told, len(rows)) == ((1, 1) if told_raised else (0, 0))
        assert slot._trust is False and slot._trust_reads is False  # the notice seeds nothing
        assert slot._restart_cut_turn is cut  # and consumes no boot reading
        if told_raised:
            row = rows[0]
            assert row["role"] == "assistant"
            assert {k: v for k, v in row["meta"].items() if k != "mid"} == {
                "kind": APPROVAL_MODE_DROPPED_KIND,
                "from": "normal",
                "to": "trust",
            }
            assert "it was on Ask every time" in row["content"]
            assert "Trust (tools run without asking)" in row["content"]
            assert "the mode its profile declares" in row["content"]
            assert "automatic resume" in row["content"]
            assert "set Ask every time in the profile" in row["content"]
            assert audit.call_args.args[1:] == (
                "restart_resume:approval_mode_raised",
                "normal->trust",
            )
            # Idempotent: a second pass before the person acts adds nothing.
            assert await restart_resume.notice_dropped_approval_modes(state, cfg) == 0
            assert sum((m.get("meta") or {}).get("kind") == _DROPPED for m in slot.messages) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("recorded", "declared", "shape", "cut", "to"),
        [
            # The finding: Reads last seen, the profile declares Trust, the turn cut.
            ("trust_reads", "trust", "worker", True, "trust"),
            ("trust_reads", "trust", "worker", False, "trust"),
            ("trust", "trust_reads", "conductor", True, "trust_reads"),
            # Nothing seeds a plain chat: it came back on Ask every time, as told today.
            ("trust", "", "plain", True, "normal"),
            # The declared mode comes back: nothing changed, nothing told.
            ("trust_reads", "trust_reads", "worker", True, ""),
        ],
    )
    async def test_the_dropped_notice_names_the_grant_the_admission_seeds(
        self, monkeypatch, recorded, declared, shape, cut, to
    ):
        """GPT review, rev 71. The writer read the restored slot's live flags for
        "now on", and a restored slot holds nothing: so a mate's chat the person
        had set to Reads, whose profile declares Trust, was told it is now on Ask
        every time while every turn of it -- the resumed one included -- runs on
        Trust as the admission seeds it. The pass already resolved that seed to
        decide whether to tell; now it hands it to the writer, which names it."""
        slot = _cut_off(_slot("chat"), kind=_RETRY_INVITED_ROW, marker=cut)
        slot._approval_mode_recorded = recorded
        execution = {
            "conductor": self._conductor_execution(),
            "worker": self._worker_execution(),
            "plain": None,
        }[shape]
        monkeypatch.setattr(
            "kiro_crew.execution_context.read_session_execution", lambda key: execution
        )

        async def fake_load(slot):
            pass

        monkeypatch.setattr(restart_resume, "load_window_off_loop", fake_load)
        audit = MagicMock()
        monkeypatch.setattr(restart_resume, "_audit", audit)
        cfg = SimpleNamespace(
            dashboard=_cfg().dashboard,
            agents={"mate": SimpleNamespace(approval_mode=declared, member_id="m1")},
        )
        state = _mock_state(slot)

        told = await restart_resume.notice_dropped_approval_modes(state, cfg)

        rows = [m for m in slot.messages if (m.get("meta") or {}).get("kind") == _DROPPED]
        assert (told, len(rows)) == ((1, 1) if to else (0, 0))
        assert slot._trust is False and slot._trust_reads is False  # the notice seeds nothing
        if not to:
            return
        row = rows[0]
        assert {k: v for k, v in row["meta"].items() if k != "mid"} == {
            "kind": APPROVAL_MODE_DROPPED_KIND,
            "from": recorded,
            "to": to,
        }
        assert audit.call_args.args[1:] == (
            "restart_resume:approval_mode_dropped",
            f"{recorded}->{to}",
        )
        if to == "normal":
            assert "is now on Ask every time" in row["content"]
            assert "choose Trust in Approval mode under the message box" in row["content"]
        else:
            assert "Ask every time" not in row["content"]
            assert (
                f"its turns now run on {restart_resume._APPROVAL_MODE_LABELS[to]}, "
                "the mode its profile declares"
            ) in row["content"]
            assert (
                f"To keep {restart_resume._APPROVAL_MODE_OPTIONS[recorded]}, set it in the profile"
            ) in row["content"]

    def test_the_standalone_writer_reads_a_threads_declared_mode_for_now_on(self):
        # The writer called on its own (no pass, no seed handed over) reads the
        # crewmate thread's declared mode on the loop, the way its predicate does.
        thread = _cut_off(_ChatSlot("dm", agent="mate", mode=DM_SLOT_MODE))
        thread._approval_mode_recorded = "trust_reads"
        cfg = SimpleNamespace(
            dashboard=_cfg().dashboard, agents={"mate": SimpleNamespace(approval_mode="trust")}
        )

        told = restart_resume.approval_mode_dropped_notice(thread, cfg=cfg)

        row = thread.messages[-1]
        assert told is True and row["meta"]["to"] == "trust"
        assert "its turns now run on Trust (tools run without asking)" in row["content"]

    def test_a_full_save_keeps_the_ask_every_time_record_the_raised_notice_needs(
        self, tmp_path, monkeypatch
    ):
        """GPT review, rev 71. The full save omitted the default, so a periodic
        flush during an Ask-every-time period wrote a line with no key, which the
        restore reads as unknown: the raised notice (rev 70) then had nothing to
        say, and the resumed turn ran under the profile's Trust untold. Both save
        forms now write the live mode, ``normal`` included; a line from before the
        key still says nothing."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        worker = state.get_or_create_slot("worker")  # the person picked Ask every time
        folds = codec.SaveFolds(memory_mode=worker.memory_mode)
        line = codec.encode(worker, folds=folds)
        restored = state.get_or_create_slot("restored")
        legacy = state.get_or_create_slot("legacy")

        codec.apply(
            state, restored, {"_type": "metadata", **line}, codec.Restore(name="r", member=None)
        )
        codec.apply(state, legacy, {"_type": "metadata"}, codec.Restore(name="l", member=None))

        assert (
            restored._approval_mode_recorded,
            restart_resume._seeded_grant_not_seen(restored, "trust"),
        ) == ("normal", "trust")
        assert line["approval_mode_seen"] == "normal"
        assert (
            legacy._approval_mode_recorded,
            restart_resume._seeded_grant_not_seen(legacy, "trust"),
        ) == ("", "")
        assert restored._trust is False and restored._trust_reads is False
