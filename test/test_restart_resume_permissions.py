"""The error pass and a chat's approval mode: changed in neither direction.

Sibling of ``test_restart_resume.py`` (which holds the shared slot/state helpers,
the held-sleep double and the ``quiet_dispatch`` fixture this module imports).
RFC §4.6 records as an accepted trade-off that a Trusted chat resumed after an
error keeps its Trust; this pins the pass to both halves of that rule -- it
restores no Trust a restart dropped and downgrades none the person holds -- and
to passing no mode to ``continue_slot_turn``. Also the boot pass's side of the
same rule for a crewmate thread: the turn admission seeds the thread's declared
mode before the resumed turn claims a provider, so the pass passes none. The
notice that a chat came back without its mode is ``chat_trust_restart``'s
(``test_chat_trust_restart_notice.py``), not this module's.
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
from kiro_crew.dashboard.chat_utils import TRANSIENT_NOTICE_GIVE_UP, TRANSIENT_NOTICE_META_KEY
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.members import DM_SLOT_MODE


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
        # A restored crewmate thread comes back on Ask every time, like every chat;
        # ``crewmate_approval.seed_for_turn`` gives it its profile's declared mode as
        # the resumed turn is admitted, before it claims a provider, so the resumed
        # turn runs under the mode the profile declares, not under the restored
        # slot's interactive default (Opus review, rev 56). Pinned on the real seed
        # with a restored thread, and on the admission's call to it.
        from kiro_crew.dashboard.crewmate_approval import seed_for_turn
        from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        thread = _cut_off(_ChatSlot("dm", agent="mate", mode=DM_SLOT_MODE))
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

        trust_before_the_turn = thread._trust
        seeded = seed_for_turn(state, thread, cfg, conductor)

        assert trust_before_the_turn is False  # the restore grants nothing
        assert seeded is True and thread._trust is True
        admission = Path(restart_resume.__file__).parent / "chat_turn" / "turn_admission.py"
        assert "seed_for_turn(state, slot, loaded_cfg, execution_context)" in admission.read_text()
