"""What a restart does with the agent folders a previous process left behind.

``_reconcile_orphans`` tombstones each folder no live run tracks as a
``gateway_restart``: a complete result is ``result_available``, streamed bytes with
no complete event a ``partial_result``, and no result ``notification_pending``. A
live PID is killed only while it still belongs to the run, which
``pid_recorded_at`` decides, and a folder already tombstoned or still tracked is
skipped. An orphan's notice is redacted, tried by injection first and otherwise
handed back for the caller's digest; it never promises a result a fragment cannot
give, says how to resume a run whose conversation survived and follows the run's
own record, and a failing notice stops no other reconciliation.
``prune_stale_tombstones`` removes a tombstoned folder past the 7-day age and keeps
a recent one, and ``spawn_status`` answers from the folder for a run no live
manager holds, or 404 when there is no folder. The shared ``agent_root`` fixture
lives in ``subagent_persistence_helpers.py``.
"""

from __future__ import annotations

import json
import time

import pytest
from subagent_persistence_helpers import (  # noqa: F401 -- the fixture is requested by name
    agent_root_fixture,
)

from kiro_crew.subagent_persistence import create_agent_folder

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


# ── Slice 7: Orphan reconciliation on startup ────────────────────────


class TestOrphanReconciliation:
    """Verify _reconcile_orphans handles all three branches."""

    @pytest.mark.asyncio
    async def test_dead_pid_with_complete_result_tombstoned_as_delivered(self, agent_root):
        from unittest.mock import MagicMock, patch

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder, write_result_chunk

        sessions = MagicMock()
        manager = SubagentManager(sessions=sessions, ctx_builder=MagicMock())

        # Simulate orphan from prior run: dead PID, has a result its run
        # finished writing (result_complete recorded at the complete event).
        create_agent_folder("orphan1", task="old task", parent_session="dashboard:default")
        write_result_chunk("orphan1", "some result")
        from kiro_crew.subagent_persistence import update_state

        update_state("orphan1", pid=99999, result_complete=True)  # dead PID

        with patch.object(manager, "_is_pid_alive", return_value=False):
            await manager._reconcile_orphans()

        ts = json.loads((agent_root / "orphan1" / "tombstone.json").read_text(encoding="utf-8"))
        assert ts["cause"] == "gateway_restart"
        assert ts["recovery_action"] == "result_available"

    @pytest.mark.asyncio
    async def test_dead_pid_with_partial_result_is_not_offered_as_a_result(self, agent_root):
        """Streamed bytes without a complete event are a fragment, not an answer.

        ``write_result_chunk`` appends per streamed chunk, so result.txt is
        non-empty from the agent's first token. A restart landing mid-turn
        therefore leaves a file that looks exactly like a finished result to
        anyone measuring its size — which is what the parent is told to go read.
        """
        from unittest.mock import MagicMock, patch

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder, write_result_chunk

        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

        create_agent_folder("orphan1p", task="old task", parent_session="dashboard:default")
        # An opening sentence, nothing more — no complete event ever arrived.
        write_result_chunk("orphan1p", "I'll start by opening a scratch worktree")
        from kiro_crew.subagent_persistence import update_state

        update_state("orphan1p", pid=99999)

        with patch.object(manager, "_is_pid_alive", return_value=False):
            await manager._reconcile_orphans()

        ts = json.loads((agent_root / "orphan1p" / "tombstone.json").read_text(encoding="utf-8"))
        assert ts["cause"] == "gateway_restart"
        assert ts["recovery_action"] == "partial_result"

    @pytest.mark.asyncio
    async def test_partial_orphan_notice_does_not_promise_a_result(self, agent_root):
        """The notice is the only thing standing between a fragment and a parent."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder, write_result_chunk

        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
        create_agent_folder("orphan1n", task="old task", parent_session="dashboard:default")
        write_result_chunk("orphan1n", "I'll start by opening a scratch worktree")
        state = {"id": "orphan1n", "task": "old task", "parent_session": ""}

        with patch.object(
            manager, "_try_inject_orphan_notification", AsyncMock(return_value=False)
        ):
            partial = await manager._notify_orphan("orphan1n", state, True)
            whole = await manager._notify_orphan(
                "orphan1n", {**state, "result_complete": True}, True
            )

        assert partial is not None and whole is not None
        assert "Partial output saved at" in partial
        assert "unfinished fragment" in partial
        # The complete-result wording must not leak onto the partial notice.
        assert "Use the read tool to retrieve it." not in partial
        assert "Use the read tool to retrieve it." in whole

    @pytest.mark.asyncio
    async def test_a_lost_orphan_with_a_surviving_conversation_says_how_to_resume(
        self, agent_root, tmp_path, monkeypatch
    ):
        """ "No result was captured" is not the whole story.

        A run the restart caught before its first token has no ``result.txt``,
        but the run's own conversation -- every turn and tool call kiro-cli
        persisted -- is still on disk, and ``spawn_continue`` resumes it from the
        run's ``state.json`` after a restart (that is retain-by-default's whole
        point). A notice that says only that nothing was captured sends the parent
        re-spawning from scratch and paying for the work twice. The notice carries
        the run's progress and the resume handle, by the same bar ``SessionMap.get``
        applies: the ``.json`` present AND a ``.jsonl`` holding a turn.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.subagent import SubagentManager

        sessions_dir = tmp_path / "kiro-sessions"
        sessions_dir.mkdir()
        monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", sessions_dir)
        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
        create_agent_folder("orphan1r", task="old task", parent_session="dashboard:default")
        state = {
            "id": "orphan1r",
            "task": "old task",
            "parent_session": "",
            "session_id": "sid-orphan1r",
            "turns": 6,
            "last_tool": "shell",
        }

        with patch.object(
            manager, "_try_inject_orphan_notification", AsyncMock(return_value=False)
        ):
            gone = await manager._notify_orphan("orphan1r", state, False)
            (sessions_dir / "sid-orphan1r.json").write_text("{}", encoding="utf-8")
            # The .json alone is what SessionMap.get PRUNES: an empty transcript.
            # Nine bytes: one under the bar SessionMap.get prunes at.
            (sessions_dir / "sid-orphan1r.jsonl").write_text("123456789", encoding="utf-8")
            empty = await manager._notify_orphan("orphan1r", state, False)
            (sessions_dir / "sid-orphan1r.jsonl").write_text(
                '{"turn": 1, "text": "hello"}\n', encoding="utf-8"
            )
            resumable = await manager._notify_orphan("orphan1r", state, False)

        assert gone is not None and empty is not None and resumable is not None
        assert "No result was captured before the restart." in gone
        assert "spawn_continue" not in gone, "a pruned conversation must not be offered"
        assert "spawn_continue" not in empty, "a transcript under the bar is not resumable"
        assert "6 turn" in resumable and "shell" in resumable
        assert "spawn_continue" in resumable and "orphan1r" in resumable
        assert "No result was captured before the restart." in resumable

        # A non-kiro backend keeps its own storage: SessionMap.get hands the sid
        # out without a file check and the resume decides, so the handle is offered.
        (sessions_dir / "sid-orphan1r.json").unlink()
        (sessions_dir / "sid-orphan1r.jsonl").unlink()
        with patch.object(
            manager, "_try_inject_orphan_notification", AsyncMock(return_value=False)
        ):
            other = await manager._notify_orphan(
                "orphan1r",
                {**state, "provider": "other-backend"},
                False,
            )
        assert other is not None and "spawn_continue" in other

        # A run minted by spawn_continue shares its conversation with the run it
        # continued: the handle must name that owner, not this run's own id.
        with patch.object(
            manager, "_try_inject_orphan_notification", AsyncMock(return_value=False)
        ):
            continued = await manager._notify_orphan(
                "orphan1r",
                {**state, "provider": "other-backend", "conversation_key": "subagent:orig0001"},
                False,
            )
        assert continued is not None
        assert 'spawn_continue(conversation="orig0001"' in continued
        assert 'conversation="orphan1r"' not in continued

        # ``last_tool`` is the raw command for a shell tool: agent-authored,
        # multi-line and unbounded. The notice flattens and caps it, so a heredoc
        # cannot put a blank line into the notice (which would split the completion
        # card's head/body inside the command) or ship the whole script.
        heredoc = "cat <<'EOF' > /tmp/x\n\nline one\n" + ("y" * 500) + "\nEOF"
        with patch.object(
            manager, "_try_inject_orphan_notification", AsyncMock(return_value=False)
        ):
            bounded = await manager._notify_orphan(
                "orphan1r",
                {**state, "provider": "other-backend", "last_tool": heredoc},
                False,
            )
        assert bounded is not None
        hint = bounded.split("Its conversation survived")[0]
        assert "\n\n" not in hint
        assert "cat <<'EOF' > /tmp/x line one" in hint
        assert "y" * 80 not in hint and "EOF`" not in hint

        # Redaction runs over the WHOLE command before the cap: a credential that
        # straddles the 80-char boundary would otherwise be cut into a fragment too
        # short for the PAT rule, which the later whole-message redaction cannot
        # match, and its prefix would ship in the notice and the digest DM.
        token = "ghp_" + "a" * 40
        with patch.object(
            manager, "_try_inject_orphan_notification", AsyncMock(return_value=False)
        ):
            secret = await manager._notify_orphan(
                "orphan1r",
                {
                    **state,
                    "provider": "other-backend",
                    "last_tool": "curl " + "x" * 55 + " " + token,
                },
                False,
            )
        assert secret is not None
        assert "ghp_" not in secret, "a PAT cut by the cap must not ship as an unredacted prefix"

    @pytest.mark.asyncio
    async def test_a_cut_off_orphan_with_a_surviving_conversation_says_how_to_resume(
        self, agent_root, tmp_path, monkeypatch
    ):
        """A run cut off mid-turn is resumable exactly like one that streamed nothing.

        ``result.txt`` is appended per streamed chunk, so a long run the restart
        catches has usually written an opening fragment and gets the ``⚠️ cut off
        mid-turn`` notice, not the ``❌ lost`` one. Its conversation survives the
        restart all the same, so the notice must carry the same resume handle;
        without it the parent re-spawns the longest runs -- the ones with the most
        work to lose -- from scratch. A run that recorded a whole
        answer is finished, so it is never offered a resume.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.subagent import SubagentManager

        sessions_dir = tmp_path / "kiro-sessions"
        sessions_dir.mkdir()
        monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", sessions_dir)
        (sessions_dir / "sid-orphan1c.json").write_text("{}", encoding="utf-8")
        (sessions_dir / "sid-orphan1c.jsonl").write_text(
            '{"turn": 1, "text": "hello"}\n', encoding="utf-8"
        )
        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
        create_agent_folder("orphan1c", task="long task", parent_session="dashboard:default")
        state = {
            "id": "orphan1c",
            "task": "long task",
            "parent_session": "",
            "session_id": "sid-orphan1c",
            "turns": 9,
            "last_tool": "shell",
        }

        with patch.object(
            manager, "_try_inject_orphan_notification", AsyncMock(return_value=False)
        ):
            cut_off = await manager._notify_orphan("orphan1c", state, True)
            whole = await manager._notify_orphan(
                "orphan1c", {**state, "result_complete": True}, True
            )

        assert cut_off is not None and whole is not None
        assert "cut off mid-turn by gateway restart" in cut_off
        assert "unfinished fragment" in cut_off
        assert "9 turn" in cut_off
        assert 'spawn_continue(conversation="orphan1c"' in cut_off
        assert "finished before gateway restart" in whole
        assert "spawn_continue" not in whole, "a finished run must not be offered a resume"

    @pytest.mark.asyncio
    async def test_dead_pid_no_result_tombstoned_as_notified(self, agent_root):
        from unittest.mock import MagicMock, patch

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder, update_state

        sessions = MagicMock()
        manager = SubagentManager(sessions=sessions, ctx_builder=MagicMock())

        create_agent_folder("orphan2", task="old task")
        update_state("orphan2", pid=99999)

        with patch.object(manager, "_is_pid_alive", return_value=False):
            await manager._reconcile_orphans()

        ts = json.loads((agent_root / "orphan2" / "tombstone.json").read_text(encoding="utf-8"))
        assert ts["cause"] == "gateway_restart"
        assert ts["recovery_action"] == "notification_pending"

    @pytest.mark.asyncio
    async def test_alive_pid_killed_and_tombstoned(self, agent_root):
        from unittest.mock import MagicMock, patch

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder, update_state

        sessions = MagicMock()
        manager = SubagentManager(sessions=sessions, ctx_builder=MagicMock())

        create_agent_folder("orphan3", task="stuck task")
        update_state("orphan3", pid=99999)

        with (
            patch.object(manager, "_is_pid_alive", return_value=True),
            patch.object(manager, "_is_orphan_process", return_value=True),
            patch.object(manager, "_kill_orphan_pid") as mock_kill,
        ):
            await manager._reconcile_orphans()

        mock_kill.assert_called_once_with(99999)
        ts = json.loads((agent_root / "orphan3" / "tombstone.json").read_text(encoding="utf-8"))
        assert ts["cause"] == "gateway_restart"
        assert ts["recovery_action"] == "notification_pending"

    @pytest.mark.asyncio
    async def test_recycled_pid_not_killed(self, agent_root):
        """A live PID that doesn't belong to the original agent must not be killed."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder, update_state

        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

        create_agent_folder("recycled1", task="old task")
        update_state("recycled1", pid=99999)

        with (
            patch.object(manager, "_is_pid_alive", return_value=True),
            patch.object(manager, "_is_orphan_process", return_value=False),
            patch.object(manager, "_kill_orphan_pid") as mock_kill,
        ):
            await manager._reconcile_orphans()

        mock_kill.assert_not_called()
        ts = json.loads((agent_root / "recycled1" / "tombstone.json").read_text(encoding="utf-8"))
        assert ts["cause"] == "gateway_restart"

    @pytest.mark.asyncio
    async def test_reconcile_uses_pid_recorded_at(self, agent_root):
        """pid_recorded_at (not started) is passed to _is_orphan_process."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder, update_state

        sessions = MagicMock()
        manager = SubagentManager(sessions=sessions, ctx_builder=MagicMock())

        create_agent_folder("orphan_ts", task="ts task")
        update_state("orphan_ts", pid=88888, pid_recorded_at=1234567890.5)

        with (
            patch.object(manager, "_is_pid_alive", return_value=True),
            patch.object(manager, "_is_orphan_process", return_value=True) as mock_check,
            patch.object(manager, "_kill_orphan_pid", return_value=None),
        ):
            await manager._reconcile_orphans()

        mock_check.assert_called_once_with(88888, 1234567890.5)
        ts = json.loads((agent_root / "orphan_ts" / "tombstone.json").read_text(encoding="utf-8"))
        assert (ts["cause"], ts["recovery_action"]) == ("gateway_restart", "notification_pending")

    @pytest.mark.asyncio
    async def test_already_tombstoned_skipped(self, agent_root):
        from unittest.mock import MagicMock

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder, write_tombstone

        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

        create_agent_folder("already_dead", task="t")
        write_tombstone("already_dead", cause="timeout", recovery_action="delivered")

        await manager._reconcile_orphans()
        ts = json.loads(
            (agent_root / "already_dead" / "tombstone.json").read_text(encoding="utf-8")
        )
        assert ts["cause"] == "timeout"  # unchanged

    @pytest.mark.asyncio
    async def test_tracked_agents_skipped(self, agent_root):
        from unittest.mock import MagicMock

        from kiro_crew.subagent import SubagentInfo, SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder

        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

        create_agent_folder("tracked1", task="t")
        # Simulate this agent being tracked in current run
        manager._agents["tracked1"] = SubagentInfo(id="tracked1", task="t")

        await manager._reconcile_orphans()
        assert not (agent_root / "tracked1" / "tombstone.json").exists()


# ── Slice 8: Notification — injection + Slack DM fallback ────────────


class TestOrphanNotification:
    """Verify orphan notification with injection attempt and Slack DM fallback."""

    @pytest.mark.asyncio
    async def test_notification_called_for_orphan_with_result(self, agent_root):
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import (
            create_agent_folder,
            update_state,
            write_result_chunk,
        )

        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

        create_agent_folder("notif1", task="important task", parent_session="dashboard:default")
        write_result_chunk("notif1", "the answer is 42")
        # The run finished writing before the restart, so it recorded completion.
        update_state("notif1", pid=99999, result_complete=True)

        with (
            patch.object(manager, "_is_pid_alive", return_value=False),
            patch.object(manager, "_notify_orphan", new_callable=AsyncMock) as mock_notify,
        ):
            await manager._reconcile_orphans()

        mock_notify.assert_awaited_once()
        call_args = mock_notify.call_args
        assert call_args[0][0] == "notif1"  # agent_id
        assert call_args[0][1].get("result_complete") is True  # state: whole
        assert call_args[0][2] is True  # has_result

    @pytest.mark.asyncio
    async def test_notification_called_for_orphan_without_result(self, agent_root):
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder, update_state

        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

        create_agent_folder("notif2", task="lost task")
        update_state("notif2", pid=99999)

        with (
            patch.object(manager, "_is_pid_alive", return_value=False),
            patch.object(manager, "_notify_orphan", new_callable=AsyncMock) as mock_notify,
        ):
            await manager._reconcile_orphans()

        mock_notify.assert_awaited_once()
        call_args = mock_notify.call_args
        assert not call_args[0][1].get("result_complete")  # state: not whole
        assert call_args[0][2] is False  # has_result

    @pytest.mark.asyncio
    async def test_slack_dm_fallback_called(self, agent_root):
        """When injection returns False, the message is returned for the caller's digest.

        _notify_orphan does not DM per orphan — undelivered messages are
        handed back so _reconcile_orphans can batch them into ONE digest DM
        (a restart with N in-flight agents must never produce N pings).
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder, write_result_chunk

        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

        create_agent_folder("notif3", task="fallback task", parent_session="dashboard:default")
        write_result_chunk("notif3", "result data")

        state = {
            "id": "notif3",
            "task": "fallback task",
            "parent_session": "dashboard:default",
            "result_complete": True,
        }

        with (
            patch.object(
                manager,
                "_try_inject_orphan_notification",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(manager, "_send_orphan_slack_dm", new_callable=AsyncMock) as mock_dm,
        ):
            msg = await manager._notify_orphan("notif3", state, True)

        mock_dm.assert_not_awaited()  # DM happens once, at digest time
        assert msg is not None
        assert "notif3" in msg
        assert "finished before gateway restart" in msg

    @pytest.mark.asyncio
    async def test_msg_redacted_before_injection_path(self, agent_root):
        """msg must be redacted before _try_inject_orphan_notification (not just Slack DM)."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder, write_result_chunk

        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
        create_agent_folder("notif_redact", task="secret task")
        write_result_chunk("notif_redact", "result")

        state = {
            "id": "notif_redact",
            "task": "secret task",
            "parent_session": "dashboard:default",
            # The run recorded its whole answer: what decides the ✅ notice.
            "result_complete": True,
        }

        injected_msg = None
        injected_meta = None

        async def _capture_inject(_session, msg, meta=None, delivery=None):
            nonlocal injected_msg, injected_meta
            injected_msg = msg
            injected_meta = meta
            return True

        with (
            patch.object(manager, "_try_inject_orphan_notification", side_effect=_capture_inject),
            patch(
                "kiro_crew.subagent._redact", side_effect=lambda m: f"[REDACTED]{m}"
            ) as mock_redact,
        ):
            await manager._notify_orphan("notif_redact", state, True)

        mock_redact.assert_called()
        assert injected_msg is not None
        assert injected_msg.startswith("[REDACTED]")
        # A whole result the restart caught before delivery → ok (completed, as
        # its tombstone and task row record it), with the header's only
        # explanation as the structured note. The card reads this, not the prose.
        assert injected_meta is not None
        assert injected_meta["kind"] == "single"
        assert injected_meta["outcome"] == "ok"
        assert injected_meta["note"] == "finished before gateway restart"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "whole, has_result, header, outcome, tail",
        [
            (True, True, "✅ finished before gateway restart", "ok", "Use the read tool"),
            (True, False, "✅ finished before gateway restart", "ok", "without writing any text"),
            (False, True, "⚠️ cut off mid-turn by gateway restart", "interrupted", "fragment"),
            (False, False, "❌ lost to gateway restart", "failed", "No result was captured"),
        ],
    )
    async def test_the_notice_follows_the_run_record(
        self, agent_root, whole, has_result, header, outcome, tail
    ):
        """Which notice a restart orphan gets is decided by the run's own record
        (``result_is_whole``) and whether ``result.txt`` holds text, so a run
        that never recorded a whole answer is never announced as completed, and
        the card's structured outcome and note match the prose."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.subagent import SubagentManager

        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
        create_agent_folder("notif_rec", task="recorded task")
        state = {
            "id": "notif_rec",
            "task": "recorded task",
            "parent_session": "dashboard:default",
            **({"result_complete": True} if whole else {}),
        }
        injected: list[tuple[str, dict]] = []

        async def _capture_inject(_session, msg, meta=None, delivery=None):
            injected.append((msg, meta))
            return True

        with patch.object(manager, "_try_inject_orphan_notification", side_effect=_capture_inject):
            await manager._notify_orphan("notif_rec", state, has_result)

        [(msg, meta)] = injected
        assert f"Agent `notif_rec` {header}\n" in msg and tail in msg
        assert meta["outcome"] == outcome and header.endswith(meta["note"])

    @pytest.mark.asyncio
    async def test_notification_failure_doesnt_crash(self, agent_root):
        """Notification failure should not prevent reconciliation of other orphans."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder

        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

        create_agent_folder("notif4", task="t1")
        create_agent_folder("notif5", task="t2")

        call_count = 0

        async def _failing_notify(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("notification failed")

        with (
            patch.object(manager, "_is_pid_alive", return_value=False),
            patch.object(manager, "_notify_orphan", side_effect=_failing_notify),
        ):
            await manager._reconcile_orphans()

        # Both orphans should be tombstoned despite notification failure
        assert (agent_root / "notif4" / "tombstone.json").exists()
        assert (agent_root / "notif5" / "tombstone.json").exists()


# ── Slice 9: Reaper prunes tombstoned folders > 7 days ───────────────


class TestReaperPrunesTombstones:
    """Verify the reaper loop calls prune_stale_tombstones."""

    @pytest.mark.asyncio
    async def test_reaper_prunes_old_tombstones(self, agent_root):
        """Old tombstoned folders are pruned during reaper sweep."""
        from kiro_crew.subagent_persistence import (
            create_agent_folder,
            prune_stale_tombstones,
            write_tombstone,
        )

        # Eight days old: past the 7-day prune cutoff.
        create_agent_folder("old_tomb", task="t")
        write_tombstone("old_tomb", cause="timeout", recovery_action="delivered")
        ts_path = agent_root / "old_tomb" / "tombstone.json"
        ts = json.loads(ts_path.read_text(encoding="utf-8"))
        # flake-ok: an 8-day-old stamp against the 7-day cutoff leaves a day of margin; never a wait or a deadline; moved verbatim
        ts["died"] = time.time() - (8 * 86400)
        ts_path.write_text(json.dumps(ts))

        # Fresh tombstone: inside the cutoff, so it is kept.
        create_agent_folder("new_tomb", task="t")
        write_tombstone("new_tomb", cause="timeout", recovery_action="delivered")

        pruned = prune_stale_tombstones(max_age_days=7)
        assert pruned == 1
        assert not (agent_root / "old_tomb").exists()
        assert (agent_root / "new_tomb").exists()


# ── Slice 10: spawn_status reads from agent folder ───────────────────


class TestSpawnStatusReadsFromAgentFolder:
    """Verify spawn_status falls back to persistence layer for orphaned agents."""

    def test_result_path_points_to_agent_folder(self, agent_root):
        """After spawn+run, info.result_path should point to agent folder."""
        from kiro_crew.subagent_persistence import (
            _agent_dir,
            create_agent_folder,
            write_result_chunk,
        )

        create_agent_folder("status1", task="t")
        write_result_chunk("status1", "full result text")

        expected = str(_agent_dir("status1") / "result.txt")
        actual = (agent_root / "status1" / "result.txt").read_text(encoding="utf-8")
        assert actual == "full result text"
        assert str(agent_root / "status1" / "result.txt") == expected

    def test_read_state_for_orphaned_agent(self, agent_root):
        """read_state returns data for orphaned agents (not in memory)."""
        from kiro_crew.subagent_persistence import create_agent_folder, read_state, write_tombstone

        create_agent_folder(
            "orphan_status", task="orphaned task", parent_session="dashboard:default"
        )
        write_tombstone("orphan_status", cause="gateway_restart", recovery_action="delivered")

        state = read_state("orphan_status")
        assert state is not None
        assert state["task"] == "orphaned task"

    @pytest.mark.asyncio
    async def test_api_spawn_status_fallback_to_disk(self, agent_root):
        """api_spawn_status returns disk data when agent not in memory."""
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.handlers.messaging import api_spawn_status
        from kiro_crew.subagent_persistence import (
            create_agent_folder,
            write_result_chunk,
            write_tombstone,
        )

        create_agent_folder("disk_agent", task="disk task")
        write_result_chunk("disk_agent", "disk result")
        write_tombstone(
            "disk_agent",
            cause="gateway_restart",
            recovery_action="delivered",
            elapsed=12.5,
            credits=1.25,
        )

        # subagents must be truthy (not None/empty) but missing the agent_id
        subagents = MagicMock()
        subagents.get = MagicMock(return_value=None)
        request = MagicMock()
        request.match_info = {"agent_id": "disk_agent"}
        request.query = {}  # real mapping so _apply_result_view reads no paging params
        request.app = {"state": MagicMock(subagents=subagents)}

        resp = await api_spawn_status(request)
        body = json.loads(resp.body)

        assert resp.status == 200
        assert body["id"] == "disk_agent"
        assert body["done"] is True
        assert "disk result" in body["result"]
        assert "gateway_restart" in body["error"]
        assert "started" in body
        assert body["elapsed"] == 12.5
        assert body["credits"] == 1.25

    @pytest.mark.asyncio
    async def test_api_spawn_status_legacy_disk_record_omits_usage(self, agent_root):
        """Pre-feature state files stay honest instead of inventing zero usage."""
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.handlers.messaging import api_spawn_status
        from kiro_crew.subagent_persistence import create_agent_folder, write_result_chunk

        create_agent_folder("legacy_agent", task="legacy task")
        write_result_chunk("legacy_agent", "legacy result")
        subagents = MagicMock()
        subagents.get = MagicMock(return_value=None)
        request = MagicMock()
        request.match_info = {"agent_id": "legacy_agent"}
        request.query = {}
        request.app = {"state": MagicMock(subagents=subagents)}

        resp = await api_spawn_status(request)
        body = json.loads(resp.body)

        assert resp.status == 200
        assert "elapsed" not in body
        assert "credits" not in body

    @pytest.mark.asyncio
    async def test_api_spawn_status_404_when_not_on_disk(self, agent_root):
        """api_spawn_status returns 404 when agent not in memory or on disk."""
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.handlers.messaging import api_spawn_status

        subagents = MagicMock()
        subagents.get = MagicMock(return_value=None)
        request = MagicMock()
        request.match_info = {"agent_id": "nonexistent"}
        request.app = {"state": MagicMock(subagents=subagents)}

        resp = await api_spawn_status(request)
        assert resp.status == 404
