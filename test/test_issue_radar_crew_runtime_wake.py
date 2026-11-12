"""Issue Radar crews: the watcher's new-work and mention wakes, and their timers.

Split out of ``test_issue_radar_crew_runtime_sweep.py`` (file line cap). The sweep is
driven the same way: a fake forge client, a fake gateway state, a real crew store.
"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from issue_radar_crew_runtime_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    OWNER,
    REPO,
    _app,
    _crew,
    _FakeClient,
    _FakeLoop,
    _FakeNudge,
    _FakeState,
    _isolated_crew_log,
    _item,
    _private_sel_root_per_test,
    _unit_for,
)
from off_loop_helpers import off_loop

from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime as cr
from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs
from kiro_crew.apps.builtins.issue_radar.backend import provider
from kiro_crew.apps.builtins.issue_radar.backend import watch as watch_mod
from kiro_crew.safety_override import reset_singleton
from kiro_crew.testing.wait import await_until

_KEY = provider.key_from_parts(OWNER, REPO)


@pytest.fixture(autouse=True)
def _gitlab_cutoff_far_ahead(_floor_monkeypatch):
    """These tests stamp forge times in 2099: the GitLab listings' "now" cutoff is
    moved past them, and the cutoff's own test sets it explicitly."""
    from kiro_crew.apps.builtins.issue_radar.backend import crew_mention_scan as scan

    _floor_monkeypatch.setattr(
        scan, "_utc_now_stamp", lambda: "9999-12-31T23:59:59Z", raising=False
    )


# ── new work and mentions ───────────────────────────────────────────────────


_OLD = "2020-01-01T00:00:00Z"
#: Later than any cursor a test can seed from the real clock.
_LATER = "2099-01-01T00:00:00Z"
_LATEST = "2099-01-02T00:00:00Z"


class _WakeClient(_FakeClient):
    """The forge as the new-work and mention detection reads it (GitLab shape)."""

    def __init__(self, login="crew-bot"):
        super().__init__()
        self.login = login
        self.open: list[dict[str, Any]] = []
        self.pulls: list[dict[str, Any]] = []
        self.notes: dict[tuple[int, bool], list[dict[str, Any]]] = {}

    def add_issue(self, number, labels=(), updated_at=_OLD):
        self.open = [i for i in self.open if i["number"] != number]
        self.open.append({"number": number, "labels": list(labels), "updated_at": updated_at})

    def note(self, number, note_id, actor, body, *, pr=False, at=_LATER):
        self.notes.setdefault((number, pr), []).append(
            {"id": str(note_id), "actor": actor, "created_at": at, "body": body}
        )
        rows = self.pulls if pr else self.open
        for row in rows:
            if row["number"] == number:
                row["updated_at"] = at
                return
        rows.append({"number": number, "labels": [], "updated_at": at})

    def get_current_login(self, **kw):
        self.calls.append("login")
        return self.login

    def list_open_issues_first_page(self, owner, repo, **kw):
        self.calls.append("open-issues")
        return [dict(i) for i in self.open]

    def list_open_pulls_first_page(self, owner, repo, **kw):
        self.calls.append("open-pulls")
        return [dict(p) for p in self.pulls]

    def list_threads_updated_since(self, owner, repo, since, *, pr=False, limit=100, page=1, **kw):
        self.calls.append(f"threads:{'!' if pr else '#'}{page}")
        rows = sorted(self.pulls if pr else self.open, key=lambda r: r["updated_at"])
        rows = [dict(r) for r in rows if r["updated_at"][:19] >= since[:19]]
        return rows[(page - 1) * limit : page * limit]

    def list_thread_comments(self, owner, repo, number, *, pr=False, **kw):
        self.calls.append(f"notes:{'!' if pr else '#'}{number}")
        return [dict(n) for n in self.notes.get((number, pr), [])]


class TestNewWorkWakes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.wake_result = True
        # Both wake kinds are opt-in; these tests exercise them, so they opt in.
        cs.write_settings(
            OWNER,
            REPO,
            {"crew_answer_mentions": True, "crew_wake_on_new_work": True},
            self.root,
        )

    async def test_new_work_wakes_are_off_unless_the_repo_opts_in(self):
        cs.write_settings(
            OWNER, REPO, {"crew_answer_mentions": False, "crew_wake_on_new_work": False}, self.root
        )
        _crew(self.root, labels=["bug"])
        _crew(self.root, name="Bode")
        client = _WakeClient()
        client.add_issue(5, ["bug"])
        await self._sweep(client)
        client.add_issue(6, ["bug"])
        client.add_issue(7, [])

        _, wake = await self._sweep(client)

        wake.assert_not_awaited()
        checks = cr.read_wake_checks(OWNER, REPO, self.root)
        self.assertEqual((checks["scope"], checks["new_mark"]), ({}, {}))
        # A truthy non-bool does not turn it on; only a JSON true does.
        cs.write_settings(OWNER, REPO, {"crew_wake_on_new_work": "yes"}, self.root)
        self.assertIs(cs.read_settings(OWNER, REPO, self.root)["crew_wake_on_new_work"], False)

        # Turning it on seeds from that moment: what was labelled meanwhile wakes nobody.
        cs.write_settings(OWNER, REPO, {"crew_wake_on_new_work": True}, self.root)
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()
        client.add_issue(8, ["bug"])
        _, wake = await self._sweep(client)
        self.assertEqual(sorted(self._reasons(wake)), ["#8 new-issue", "#8 scope-label"])

    async def test_mention_wakes_are_off_unless_the_repo_opts_in(self):
        cs.write_settings(OWNER, REPO, {"crew_answer_mentions": False}, self.root)
        _crew(self.root)
        client = _WakeClient()
        client.add_issue(5)
        await self._sweep(client)
        client.note(5, 101, "alice", "@crew-bot can you take this?")

        _, wake = await self._sweep(client)

        wake.assert_not_awaited()
        self.assertFalse([c for c in client.calls if c.startswith("notes:")])
        self.assertEqual(cr.read_wake_checks(OWNER, REPO, self.root)["mentions"], {})
        # A truthy non-bool does not turn it on; only a JSON true does.
        cs.write_settings(OWNER, REPO, {"crew_answer_mentions": "yes"}, self.root)
        self.assertIs(cs.read_settings(OWNER, REPO, self.root)["crew_answer_mentions"], False)

    async def test_turning_mentions_off_mid_sweep_drops_the_scans_asks(self):
        crew = _crew(self.root, labels=["bug"])
        client = _WakeClient()
        client.add_issue(5)
        await self._sweep(client)
        client.note(5, 101, "alice", "@crew-bot can you take this?")
        client.add_issue(6, ["bug"])
        real = cr.detect_new_work

        async def then_opt_out(*args, **kwargs):
            found = await real(*args, **kwargs)
            # The operator saves "off" while the scan's results are on their way.
            off_loop(cs.write_settings, OWNER, REPO, {"crew_answer_mentions": False}, self.root)
            return found

        with mock.patch.object(cr, "detect_new_work", new=then_opt_out):
            woken, wake = await self._sweep(client)

        # The scope-label wake still runs; the ask is gone, on disk too.
        self.assertEqual(self._reasons(wake), ["#6 scope-label"])
        self.assertEqual(cr.read_wake_checks(OWNER, REPO, self.root)["mentions"], {})
        self.assertEqual(woken[crew["id"]], [cr.SIG_SCOPE_LABEL, cr.SIG_ASKED])

    async def test_an_ask_dropped_mid_sweep_is_not_kept_for_a_quick_turn_back_on(self):
        _crew(self.root)
        client = _WakeClient()
        client.add_issue(5)
        await self._sweep(client)
        client.note(5, 101, "alice", "@crew-bot can you take this?")
        real = cr.detect_new_work

        async def then_opt_out(*args, **kwargs):
            found = await real(*args, **kwargs)
            off_loop(cs.write_settings, OWNER, REPO, {"crew_answer_mentions": False}, self.root)
            return found

        with mock.patch.object(cr, "detect_new_work", new=then_opt_out):
            _, wake = await self._sweep(client)
        wake.assert_not_awaited()

        # Back on before any other sweep ran: the dropped ask must not come back.
        cs.write_settings(OWNER, REPO, {"crew_answer_mentions": True}, self.root)
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()

    async def test_a_boundary_second_split_across_pages_is_not_skipped(self):
        crew = _crew(self.root)
        client = _WakeClient()
        client.list_thread_comments = mock.Mock(side_effect=AssertionError("GitLab path"))
        client.list_repo_comments_since = mock.Mock(return_value=[])
        await self._sweep(client)
        older = "2099-01-01T00:00:00Z"
        tied = "2099-01-01T00:00:09Z"

        def row(cid, updated, body="progress"):
            return {
                "id": cid,
                "number": 5,
                "is_pr": False,
                "actor": "alice",
                "created_at": updated,
                "updated_at": updated,
                "body": body,
            }

        everything = [row(f"o{i}", older) for i in range(cr.REPO_COMMENTS_PAGE - 1)]
        everything += [row("t1", tied), row("t2", tied, "@crew-bot over here")]

        def page_of(o, r, since, page=1, **kw):
            # Inclusive ``since`` sorted by update: a cursor at ``tied`` without the
            # overlap would skip nothing here, an exclusive one would skip t2.
            rows = [c for c in everything if c["updated_at"][:19] > since[:19]]
            first = (page - 1) * cr.REPO_COMMENTS_PAGE
            return rows[first : first + cr.REPO_COMMENTS_PAGE]

        client.list_repo_comments_since.side_effect = page_of
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()  # t2 was the 101st row: not read yet

        woken, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#5 asked t2"])
        self.assertIn(crew["id"], woken)

    async def test_a_full_ask_queue_holds_questions_back_instead_of_dropping_them(self):
        crew = _crew(self.root)
        client = _WakeClient()
        client.list_thread_comments = mock.Mock(side_effect=AssertionError("GitLab path"))
        client.list_repo_comments_since = mock.Mock(return_value=[])
        await self._sweep(client)
        asks = [
            {
                "id": f"q{i:02d}",
                "number": 5,
                "is_pr": False,
                "actor": "alice",
                "created_at": f"2099-01-01T00:00:{i:02d}Z",
                "updated_at": f"2099-01-01T00:00:{i:02d}Z",
                "body": "@crew-bot ?",
            }
            for i in range(cr._MAX_PENDING_ASKS + 1)
        ]
        client.list_repo_comments_since.return_value = asks

        self.wake_result = False  # busy: the queue fills and stays full
        _, wake = await self._sweep(client)
        self.assertEqual(len(self._reasons(wake)[0].split("; ")), cr._MAX_PENDING_ASKS)
        self.assertEqual(cr.read_wake_checks(OWNER, REPO, self.root)["mentions"]["held_back"], 1)

        self.wake_result = True  # the crew takes the twenty
        await self._sweep(client)
        woken, wake = await self._sweep(client)  # and the held one follows
        self.assertEqual(self._reasons(wake), [f"#5 asked q{cr._MAX_PENDING_ASKS:02d}"])
        self.assertIn(crew["id"], woken)

    async def test_a_mention_past_the_body_cut_still_wakes(self):
        crew = _crew(self.root)
        client = _WakeClient()
        client.list_thread_comments = mock.Mock(side_effect=AssertionError("GitLab path"))
        client.list_repo_comments_since = mock.Mock(return_value=[])
        await self._sweep(client)
        client.list_repo_comments_since.return_value = [
            {
                "id": "c9",
                "number": 5,
                "is_pr": False,
                "actor": "alice",
                "created_at": _LATER,
                "updated_at": _LATER,
                "body": "log " * 1000,
                "asked": True,
            }
        ]
        woken, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#5 asked c9"])
        self.assertIn(crew["id"], woken)

    async def test_scope_marks_keep_no_labels(self):
        crew = _crew(self.root, labels=["bug"])
        client = _WakeClient()
        client.add_issue(5, ["bug", *[f"extra-{i}" for i in range(50)]])
        await self._sweep(client)
        seen = cr.read_wake_checks(OWNER, REPO, self.root)["scope"][crew["id"]]["seen"]
        self.assertEqual(seen, {"5": 1})

    async def _sweep(self, client, key=_KEY):
        wake = mock.AsyncMock(side_effect=lambda *a, **k: self.wake_result)
        with (
            mock.patch.object(provider, "client_for", return_value=client),
            mock.patch.object(cr.provider, "client_for", return_value=client),
            mock.patch.object(cr, "wake_crew", new=wake),
        ):
            woken = await cr.sweep_repo(_app(_FakeState()), key, self.root)
        return woken, wake

    @staticmethod
    def _reasons(wake) -> list[str]:
        return [call.args[4] for call in wake.await_args_list]

    async def test_a_scope_label_wakes_once_after_a_silent_first_look(self):
        crew = _crew(self.root, labels=["Bug"])
        client = _WakeClient()
        client.add_issue(5, ["bug"])
        woken, wake = await self._sweep(client)
        # First observation: the backlog is recorded, nobody is woken.
        self.assertEqual(woken, {})
        wake.assert_not_awaited()

        client.add_issue(6, ["bug", "ui"])
        client.add_issue(7, ["docs"])
        woken, wake = await self._sweep(client)
        self.assertEqual(woken, {crew["id"]: [cr.SIG_SCOPE_LABEL]})
        self.assertEqual(self._reasons(wake), ["#6 scope-label"])

        woken, wake = await self._sweep(client)
        self.assertEqual(woken, {})
        wake.assert_not_awaited()

    async def test_claimed_needs_human_skipped_and_own_issues_wake_nobody(self):
        crew = _crew(self.root, labels=["bug"])
        client = _WakeClient()
        await self._sweep(client)
        off_loop(
            cs.commit_work_progress,
            OWNER,
            REPO,
            crew["id"],
            8,
            {"phase": "skipped"},
            "skip",
            "duplicate of #2",
            skip_reason="duplicate",
            skip_scope="duplicate",
            root=self.root,
            session_id=_unit_for(crew["id"]),
        )
        _item(self.root, crew["id"], 9, phase="claimed")
        client.add_issue(6, ["bug", cr.CLAIM_LABEL])
        client.add_issue(7, ["bug", "crew: needs human"])
        client.add_issue(8, ["bug"])
        client.add_issue(9, ["bug"])
        client.add_issue(10, ["bug"])

        woken, wake = await self._sweep(client)

        self.assertEqual(self._reasons(wake), ["#10 scope-label"])

    async def test_a_busy_crew_is_woken_again_next_sweep(self):
        crew = _crew(self.root, labels=["bug"])
        client = _WakeClient()
        await self._sweep(client)
        client.add_issue(6, ["bug"])

        self.wake_result = False  # mid-turn: the wake is dropped
        _, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#6 scope-label"])

        self.wake_result = True
        woken, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#6 scope-label"])
        self.assertIn(crew["id"], woken)

        _, wake = await self._sweep(client)
        wake.assert_not_awaited()

    async def test_an_empty_scope_crew_wakes_for_a_new_issue(self):
        crew = _crew(self.root)
        client = _WakeClient()
        client.add_issue(5, ["bug"])
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()

        client.add_issue(6, [])
        client.add_issue(7, [cr.CLAIM_LABEL])
        woken, wake = await self._sweep(client)
        self.assertEqual(woken, {crew["id"]: [cr.SIG_NEW_ISSUE]})
        self.assertEqual(self._reasons(wake), ["#6 new-issue"])
        # An old issue gaining labels is not new to a crew with no scope.
        client.add_issue(5, ["bug", "ui"], updated_at=_LATER)
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()

    async def test_a_mention_wakes_the_crew_and_names_the_comment(self):
        crew = _crew(self.root)
        client = _WakeClient()
        client.add_issue(5)
        _, wake = await self._sweep(client)
        # The cursor is seeded on first sight: no thread is read yet.
        self.assertFalse([c for c in client.calls if c.startswith("notes:")])

        client.note(5, 101, "alice", "@Crew-Bot why was this skipped?")
        client.note(5, 102, "bob", "@crew-botanist unrelated")
        client.note(3, 7, "alice", "cc @crew-bot", pr=True, at=_LATEST)
        key = provider.key_from_parts(OWNER, REPO, "gitlab")
        woken, wake = await self._sweep(client, key)

        self.assertEqual(woken, {crew["id"]: [cr.SIG_ASKED, cr.SIG_ASKED]})
        self.assertEqual(self._reasons(wake), ["#5 asked 101; !3 asked 7"])

        _, wake = await self._sweep(client, key)
        wake.assert_not_awaited()

    async def test_the_crews_own_mention_wakes_nobody(self):
        _crew(self.root)
        client = _WakeClient()
        client.add_issue(5)
        await self._sweep(client)
        client.note(5, 101, "crew-bot", "Answered above, @crew-bot out.")

        _, wake = await self._sweep(client)

        wake.assert_not_awaited()

    async def test_a_mention_on_a_busy_crew_stays_pending(self):
        crew = _crew(self.root)
        client = _WakeClient()
        client.add_issue(5)
        await self._sweep(client)
        client.note(5, 101, "alice", "@crew-bot ?")

        self.wake_result = False
        await self._sweep(client)
        self.wake_result = True
        woken, wake = await self._sweep(client)

        self.assertEqual(self._reasons(wake), ["#5 asked 101"])
        self.assertIn(crew["id"], woken)
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()

    async def test_an_ask_not_in_the_wake_stays_pending(self):
        crew = _crew(self.root, labels=["bug"])
        client = _WakeClient()
        client.add_issue(5)
        await self._sweep(client)
        client.note(5, 101, "alice", "@crew-bot ?")
        self.wake_result = False  # busy: the ask is queued
        await self._sweep(client)
        self.wake_result = True

        # The login read fails this sweep, so the ask is not folded into the wake;
        # an unrelated scope-label wake still starts a turn.
        client.login = ""
        client.add_issue(6, ["bug"])
        _, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#6 scope-label"])

        client.login = "crew-bot"
        woken, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#5 asked 101"])
        self.assertIn(crew["id"], woken)

    async def _carried_ask(self):
        crew = _crew(self.root)
        client = _WakeClient()
        client.add_issue(5)
        await self._sweep(client)
        client.note(5, 101, "alice", "@crew-bot ?")
        _, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#5 asked 101"])  # scheduled, settled
        return crew, client

    async def _end_turn(self, crew, outcome):
        """Run the scheduled turn through the real capped runner. *outcome*: what the
        permit wait does (``timeout``, ``cancel``), or what the admitted chat does:
        ``consume`` (output streamed), ``end_turn`` (a real end of turn, no output),
        ``unconsumed`` (it returns before the model read it, as a cancellation the
        runner swallows does), ``retry`` (it queues a retry carrying the callbacks),
        ``cancel_in_chat``. A queued retry lands on ``self.turn_queue``."""
        from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime_session as crs

        ran = []

        async def run_background_turn(slot, coro):
            if outcome in ("timeout", "cancel"):
                coro.close()
                raise TimeoutError if outcome == "timeout" else asyncio.CancelledError
            return await coro

        state = mock.Mock(run_background_turn=run_background_turn)
        slot = mock.Mock(key=f"crew-{crew['id']}")
        self.turn_queue = slot._queue = []

        async def run_chat(*args, **kwargs):
            ran.append(args[2])
            if outcome in ("retry", "retry_then_cancel"):
                kwargs["_on_consumed"](False)  # an empty reply retracts, then requeues
                keys = ("_on_consumed", "_on_irreversibly_consumed")
                self.turn_queue.append({k: kwargs[k] for k in keys})
                if outcome == "retry_then_cancel":
                    raise asyncio.CancelledError
            elif outcome == "consume":
                await kwargs["_on_irreversibly_consumed"]()
            elif outcome == "end_turn":
                kwargs["_on_consumed"](True)
            elif outcome == "cancel_in_chat":
                raise asyncio.CancelledError

        with mock.patch.object(crs, "_run_chat", new=run_chat):
            try:
                await crs._capped_run_chat(state, slot, "[crew wake: #5 asked 101]\nbrief")
            except asyncio.CancelledError:
                pass
        return ran

    async def test_an_ask_cancelled_in_the_chat_before_the_model_read_it_is_asked_again(self):
        crew, client = await self._carried_ask()
        ran = await self._end_turn(crew, "cancel_in_chat")
        self.assertEqual(len(ran), 1)  # admitted, but the model never consumed it
        _, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#5 asked 101"])

    async def test_a_turn_that_ends_unconsumed_with_no_retry_asks_again_at_once(self):
        crew, client = await self._carried_ask()
        ran = await self._end_turn(crew, "unconsumed")  # e.g. a swallowed cancellation
        self.assertEqual(len(ran), 1)
        _, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#5 asked 101"])

    async def test_a_restart_while_a_retry_is_queued_asks_again(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_wake_inflight as inflight

        crew, client = await self._carried_ask()
        await self._end_turn(crew, "retry")
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()  # still carried: the queued retry may consume it
        with mock.patch.object(inflight, "BOOT", "a-later-process"):
            _, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#5 asked 101"])

    async def test_a_retry_that_ends_its_turn_consumes_the_ask(self):
        await self._assert_retry_end_turn_consumes("retry")

    async def test_a_retry_left_by_a_cancelled_attempt_still_consumes_the_ask(self):
        await self._assert_retry_end_turn_consumes("retry_then_cancel")

    async def _assert_retry_end_turn_consumes(self, outcome):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_wake_inflight as inflight

        crew, client = await self._carried_ask()
        await self._end_turn(crew, outcome)
        (entry,) = self.turn_queue

        async def drained_retry():
            entry["_on_consumed"](True)  # a real end of turn, no streamed output
            await asyncio.sleep(0)

        await asyncio.ensure_future(drained_retry())
        # The watcher of that turn, and the consumed write it starts.
        for _ in range(2):
            others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            await await_until(asyncio.gather(*others), what="the retry's watcher")
        with mock.patch.object(inflight, "BOOT", "a-later-process"):
            _, wake = await self._sweep(client)
        wake.assert_not_awaited()

    async def test_an_ask_survives_a_crash_before_its_requeue_is_saved(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime as runtime
        from kiro_crew.apps.builtins.issue_radar.backend import crew_wake_inflight as inflight

        _, client = await self._carried_ask()
        with mock.patch.object(inflight, "BOOT", "a-later-process"):
            # The restarted process requeues the ask, then dies before the queue is saved.
            with mock.patch.object(runtime, "write_wake_checks", side_effect=OSError("crash")):
                try:
                    await self._sweep(client)
                except OSError:
                    pass
            _, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#5 asked 101"])

    async def test_an_ask_whose_turn_timed_out_before_admission_is_asked_again(self):
        crew, client = await self._carried_ask()
        await self._end_turn(crew, "timeout")
        _, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#5 asked 101"])

    async def test_an_ask_whose_turn_was_cancelled_before_admission_is_asked_again(self):
        crew, client = await self._carried_ask()
        await self._end_turn(crew, "cancel")
        _, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#5 asked 101"])

    async def test_an_ask_carried_by_a_gateway_that_restarted_is_asked_again(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_wake_inflight as inflight

        _, client = await self._carried_ask()
        # The process that carried it is gone, and its queued turn with it.
        with mock.patch.object(inflight, "BOOT", "a-later-process"):
            _, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#5 asked 101"])

    async def _assert_consumed_is_not_asked_again(self, outcome):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_wake_inflight as inflight

        crew, client = await self._carried_ask()
        ran = await self._end_turn(crew, outcome)
        self.assertEqual(len(ran), 1)  # the turn ran
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()
        with mock.patch.object(inflight, "BOOT", "a-later-process"):
            _, wake = await self._sweep(client)
        wake.assert_not_awaited()  # a restart hands back nothing consumed

    async def test_an_ask_the_model_streamed_an_answer_to_is_not_asked_again(self):
        await self._assert_consumed_is_not_asked_again("consume")

    async def test_an_ask_whose_turn_reached_its_end_is_not_asked_again(self):
        await self._assert_consumed_is_not_asked_again("end_turn")

    async def test_a_consumed_ask_left_pending_by_a_failed_save_is_not_asked_again(self):
        crew, client = await self._carried_ask_unsettled()
        await self._end_turn(crew, "consume")
        self.assertIn(crew["id"], self._pending())  # the failed save left it queued
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()
        self.assertNotIn(crew["id"], self._pending())

    async def test_a_carried_ask_left_pending_by_a_failed_save_is_not_carried_twice(self):
        crew, client = await self._carried_ask_unsettled()
        # The first turn still waits for its permit; a busy crew drops the second wake.
        self.wake_result = False
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()
        self.wake_result = True
        await self._end_turn(crew, "cancel")  # the first turn's end still hands it back
        _, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#5 asked 101"])

    async def _carried_ask_unsettled(self):
        """A carried ask whose wake started a turn, but whose pending removal failed."""
        crew = _crew(self.root)
        client = _WakeClient()
        client.add_issue(5)
        await self._sweep(client)
        client.note(5, 101, "alice", "@crew-bot ?")
        with mock.patch.object(cr, "settle_new_work", side_effect=OSError("disk full")):
            _, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#5 asked 101"])
        return crew, client

    def _pending(self):
        return cr.read_wake_checks(OWNER, REPO, self.root)["mentions"].get("pending") or {}

    async def test_an_ask_past_the_in_flight_cap_waits_and_never_evicts_a_carried_one(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_wake_inflight as inflight

        crew, client = await self._carried_ask()  # #5 asked 101, its turn outstanding
        client.note(5, 102, "bob", "@crew-bot and this?", at=_LATEST)
        with mock.patch.object(inflight, "_MAX_PER_CREW", 1):
            _, wake = await self._sweep(client)
            wake.assert_not_awaited()  # no room: 102 waits, 101 is kept
            self.assertEqual(self._pending()[crew["id"]], ["#5 asked 102"])
            await self._end_turn(crew, "cancel")  # 101's turn ends unconsumed
            _, wake = await self._sweep(client)
        # 101 came back intact and fills the one slot; 102 still waits for room.
        self.assertEqual(self._reasons(wake), ["#5 asked 101"])
        self.assertEqual(self._pending()[crew["id"]], ["#5 asked 102"])

    def test_the_cap_makes_room_from_consumed_rows_only(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_wake_inflight as inflight

        args = (OWNER, REPO, self.root, "c1", "crew-c1")
        with mock.patch.object(inflight, "_MAX_PER_CREW", 2):
            self.assertEqual(inflight.carry(*args, ["a", "b"]), ["a", "b"])
            self.assertEqual(inflight.carry(*args, ["c"]), [])  # both still outstanding
            off_loop(inflight._resolve, "crew-c1", ["a"], True)
            self.assertEqual(inflight.carry(*args, ["c", "d"]), ["c"])  # a made room
        back, held = inflight.review(OWNER, REPO, self.root, "c1")
        self.assertEqual((back, held), ([], {"b", "c"}))

    async def test_a_paused_crews_pending_ask_is_answered_once_it_resumes(self):
        crew = _crew(self.root)
        other = _crew(self.root, name="Bode")
        client = _WakeClient()
        client.add_issue(5)
        await self._sweep(client)
        client.note(5, 101, "alice", "@crew-bot ?")  # goes to the first crew by id
        first = min(crew["id"], other["id"])
        self.wake_result = False  # busy: the ask is queued
        await self._sweep(client)
        self.wake_result = True
        off_loop(cs.set_crew_paused, OWNER, REPO, first, True, "holiday", self.root)
        _, wake = await self._sweep(client)  # the other crew's sweep keeps it
        wake.assert_not_awaited()
        self.assertEqual(self._pending()[first], ["#5 asked 101"])

        off_loop(cs.set_crew_paused, OWNER, REPO, first, False, "", self.root)
        woken, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#5 asked 101"])
        self.assertIn(first, woken)

        self.wake_result = False
        client.note(5, 102, "alice", "@crew-bot again?", at=_LATEST)
        await self._sweep(client)
        self.assertEqual(self._pending()[first], ["#5 asked 102"])
        off_loop(cs.retire_crew, OWNER, REPO, first, self.root)
        await self._sweep(client)
        self.assertNotIn(first, self._pending())  # a retired crew keeps nothing

    async def test_a_mention_goes_to_the_crew_whose_scope_matches(self):
        _crew(self.root, name="Andromeda", labels=["ui"])
        backend = _crew(self.root, name="Bode", labels=["api"])
        client = _WakeClient()
        client.add_issue(5, ["api"])
        await self._sweep(client)
        client.note(5, 101, "alice", "@crew-bot which endpoint?")

        _, wake = await self._sweep(client)

        self.assertEqual([c.args[3]["id"] for c in wake.await_args_list], [backend["id"]])

    async def test_github_reads_every_thread_in_one_call(self):
        crew = _crew(self.root)
        client = _WakeClient()
        client.list_thread_comments = mock.Mock(side_effect=AssertionError("GitLab path"))
        client.list_repo_comments_since = mock.Mock(return_value=[])
        await self._sweep(client)
        client.list_repo_comments_since.return_value = [
            {
                "id": "c1",
                "number": 5,
                "is_pr": True,
                "actor": "alice",
                "created_at": _LATER,
                "body": "@crew-bot ping",
            },
        ]

        woken, wake = await self._sweep(client)

        self.assertEqual(self._reasons(wake), ["#5 asked c1"])
        self.assertIn(crew["id"], woken)
        self.assertEqual(client.list_repo_comments_since.call_count, 1)

    async def test_bot_edited_and_repeated_comments_wake_once_at_most(self):
        """Nearby inputs on GitHub: a bot's mention, an old comment edited to add a
        mention (``since`` matches on update time, so it comes back), and the same
        comment id served twice and again on the next read."""
        crew = _crew(self.root)
        client = _WakeClient()
        client.list_thread_comments = mock.Mock(side_effect=AssertionError("GitLab path"))
        client.list_repo_comments_since = mock.Mock(return_value=[])
        await self._sweep(client)

        def comment(cid, actor, at, body="@crew-bot ping"):
            return {
                "id": cid,
                "number": 5,
                "is_pr": True,
                "actor": actor,
                "created_at": at,
                "body": body,
            }

        human = comment("c2", "alice", _LATER)
        client.list_repo_comments_since.return_value = [
            comment("c0", "readiness-bot[bot]", _LATER),
            comment("c1", "alice", "2020-01-01T00:00:00Z", "edited: @crew-bot"),
            human,
            human,
        ]
        woken, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#5 asked c2"])
        self.assertEqual(woken, {crew["id"]: [cr.SIG_ASKED]})

        _, wake = await self._sweep(client)
        wake.assert_not_awaited()

    async def test_a_mention_is_answered_by_one_crew_the_owner_first(self):
        first = _crew(self.root, name="Andromeda")
        owner = _crew(self.root, name="Bode")
        _item(self.root, owner["id"], 9, phase="selected", pr_number=12)
        client = _WakeClient()
        client.add_issue(5)
        await self._sweep(client)  # seeds the cursor and the new-issue marks
        key = provider.key_from_parts(OWNER, REPO, "gitlab")

        client.note(12, 201, "alice", "@crew-bot is CI fixed?", pr=True)
        _, wake = await self._sweep(client, key)
        self.assertEqual(
            [(c.args[3]["id"], c.args[4]) for c in wake.await_args_list],
            [(owner["id"], "!12 asked 201")],
        )

        # Nobody owns #5 and no scope matches: exactly one crew, the first by id.
        client.note(5, 202, "alice", "@crew-bot thoughts?", at=_LATEST)
        _, wake = await self._sweep(client, key)
        self.assertEqual(
            [(c.args[3]["id"], c.args[4]) for c in wake.await_args_list],
            [(min(first["id"], owner["id"]), "#5 asked 202")],
        )

    async def test_a_mention_after_a_handover_goes_to_the_crew_holding_it(self):
        first = _crew(self.root, name="Andromeda")
        second = _crew(self.root, name="Bode")
        gave_up, holder = sorted([first, second], key=lambda c: c["id"])
        _item(self.root, gave_up["id"], 7, phase="preempted")
        _item(self.root, holder["id"], 7, phase="selected")
        client = _WakeClient()
        client.add_issue(7)
        await self._sweep(client)

        client.note(7, 401, "alice", "@crew-bot status?")
        _, wake = await self._sweep(client)

        self.assertEqual([c.args[3]["id"] for c in wake.await_args_list], [holder["id"]])

    async def test_a_page_of_edited_old_comments_cannot_stall_github_mentions(self):
        crew = _crew(self.root)
        client = _WakeClient()
        client.list_thread_comments = mock.Mock(side_effect=AssertionError("GitLab path"))
        client.list_repo_comments_since = mock.Mock(return_value=[])
        await self._sweep(client)

        def row(cid, created, updated, body="progress"):
            return {
                "id": cid,
                "number": 5,
                "is_pr": False,
                "actor": "alice",
                "created_at": created,
                "updated_at": updated,
                "body": body,
            }

        edits = [row(f"e{i}", _OLD, _LATER, "@crew-bot old") for i in range(cr.REPO_COMMENTS_PAGE)]
        asked = row("c1", _LATEST, _LATEST, "@crew-bot new question")
        # Sorted by update: the page of edits, then the question behind it.
        client.list_repo_comments_since.side_effect = lambda o, r, since, page=1, **kw: (
            edits if page == 1 else [asked] if page == 2 else []
        )
        woken, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#5 asked c1"])
        self.assertIn(crew["id"], woken)

    async def test_a_github_tie_wider_than_a_page_is_read_through(self):
        crew = _crew(self.root)
        client = _WakeClient()
        client.list_thread_comments = mock.Mock(side_effect=AssertionError("GitLab path"))
        client.list_repo_comments_since = mock.Mock(return_value=[])
        await self._sweep(client)

        def row(cid, created, body):
            return {
                "id": cid,
                "number": 5,
                "is_pr": False,
                "actor": "alice",
                "created_at": created,
                "updated_at": _LATER,
                "body": body,
            }

        # One bulk edit: a full page and the question behind it, all at ONE time.
        pages = {
            1: [row(f"e{i}", _OLD, "@crew-bot old") for i in range(cr.REPO_COMMENTS_PAGE)],
            2: [row("c1", _LATER, "@crew-bot new question")],
        }
        client.list_repo_comments_since.side_effect = lambda o, r, s, page=1, **kw: pages.get(
            page, []
        )

        woken, wake = await self._sweep(client)

        self.assertEqual(self._reasons(wake), ["#5 asked c1"])
        self.assertIn(crew["id"], woken)

        # A tie past the per-sweep page cap resumes where it stopped next sweep.
        cap = cr.MAX_TIED_COMMENT_PAGES

        # Each page is one update time, but the time changes between pages: the
        # resumed read must keep the window it was numbered in.
        def edits(prefix, updated):
            return [
                {**row(f"{prefix}{i}", _OLD, "@crew-bot old"), "updated_at": updated}
                for i in range(cr.REPO_COMMENTS_PAGE)
            ]

        pages = {1: edits("x1-", _LATER)}
        pages.update({n: edits(f"x{n}-", _LATEST) for n in range(2, cap + 1)})
        pages[cap + 1] = [{**row("c2", _LATER, "@crew-bot behind the cap"), "updated_at": _LATEST}]
        seen_since = []
        client.list_repo_comments_since.side_effect = lambda o, r, s, page=1, **kw: (
            seen_since.append((s, page)) or pages.get(page, [])
        )
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()

        woken, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#5 asked c2"])
        # It resumed on the page after the cap (then re-read the earlier pages once).
        self.assertIn(cap + 1, [page for _since, page in seen_since])
        self.assertEqual(len({since for since, _ in seen_since}), 1)  # one window
        # The window finished: the next read starts a new one, on page 1.
        done = len(seen_since)
        await self._sweep(client)
        since, page = seen_since[done]
        self.assertEqual(page, 1)
        self.assertEqual(since, cr._second_before(_LATEST))  # one second of overlap

    async def test_a_wake_a_turn_beat_to_the_slot_is_dropped_not_queued(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime_supervision as sup

        crew = _crew(self.root, unattended=False)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        real = sup._reconcile_trust
        calls = []

        async def a_turn_starts_meanwhile(*args, **kwargs):
            calls.append(1)
            ok = await real(*args, **kwargs)
            if len(calls) == 2:  # the last await before the dispatch
                slot.running = True
            return ok

        with mock.patch.object(sup, "_reconcile_trust", new=a_turn_starts_meanwhile):
            started = await cr.wake_crew(state, OWNER, REPO, crew, "#5 asked 1", self.root)
        self.assertFalse(started)
        self.assertEqual(slot.prompts, [])  # nothing queued behind the running turn

    async def test_gitlab_threads_past_a_full_updated_page_are_read_next_sweep(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_mention_scan as scan

        crew = _crew(self.root)
        client = _WakeClient()
        for number in (7, 8, 9):
            client.add_issue(number)
        await self._sweep(client)
        key = provider.key_from_parts(OWNER, REPO, "gitlab")
        client.note(7, 300, "alice", "@crew-bot over here", at=_LATER)  # oldest update
        client.note(8, 301, "bob", "no mention", at=_LATEST)
        client.note(9, 302, "bob", "no mention", at=_LATEST)
        with mock.patch.object(scan, "THREAD_PAGE", 2):
            woken, wake = await self._sweep(client, key)
            self.assertEqual(self._reasons(wake), ["#7 asked 300"])
            # Page full: the floor did not move past the threads not listed yet.
            client.note(9, 303, "carol", "@crew-bot and me", at=_LATEST)
            woken, wake = await self._sweep(client, key)
        self.assertEqual(self._reasons(wake), ["#9 asked 303"])
        self.assertIn(crew["id"], woken)

    async def test_a_gitlab_thread_too_busy_to_read_back_does_not_stall_the_scan(self):
        from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client_crew_reads as reads

        _crew(self.root)
        client = _WakeClient()
        for number in (5, 6):
            client.add_issue(number)
        await self._sweep(client)
        key = provider.key_from_parts(OWNER, REPO, "gitlab")
        client.note(5, 101, "alice", "@crew-bot newest on a busy thread", at=_LATER)
        client.note(6, 201, "bob", "@crew-bot on a quiet one", at=_LATEST)
        real = client.list_thread_comments

        def busy(owner, repo, number, **kw):
            rows = real(owner, repo, number, **kw)
            if number == 5:
                raise reads.NotesIncomplete("too many notes", rows)
            return rows

        client.list_thread_comments = busy
        _, wake = await self._sweep(client, key)
        self.assertEqual(self._reasons(wake), ["#5 asked 101; #6 asked 201"])
        state = cr.read_wake_checks(OWNER, REPO, self.root)["mentions"]
        self.assertLess(state["floor"], _LATER)  # the floor waits for a whole read

    async def test_a_github_resume_steps_back_when_an_earlier_comment_moves(self):
        crew = _crew(self.root)
        client = _WakeClient()
        client.list_thread_comments = mock.Mock(side_effect=AssertionError("GitLab path"))
        client.list_repo_comments_since = mock.Mock(return_value=[])
        await self._sweep(client)
        cap, size = cr.MAX_TIED_COMMENT_PAGES, cr.REPO_COMMENTS_PAGE

        def row(cid, created, updated, body):
            return {
                "id": cid,
                "number": 5,
                "is_pr": False,
                "actor": "alice",
                "created_at": created,
                "updated_at": updated,
                "body": body,
            }

        # A bulk edit wider than the per-sweep cap, the question right behind it.
        rows = [row(f"e{i}", _OLD, _LATER, "old") for i in range(cap * size)]
        rows.append(row("q", _LATER, _LATER, "@crew-bot behind the tie"))

        def page_of(o, r, since, page=1, **kw):
            live = sorted(
                (c for c in rows if c["updated_at"][:19] > since[:19]),
                key=lambda c: c["updated_at"],
            )
            return live[(page - 1) * size : page * size]

        client.list_repo_comments_since.side_effect = page_of
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()  # the tie outran the cap; the question is unread
        # An edit moves a comment from the first page to the end: the question
        # moves up onto the last page already read.
        rows[0] = {**rows[0], "updated_at": _LATEST}
        woken, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#5 asked q"])
        self.assertIn(crew["id"], woken)

    async def test_a_gitlab_tie_wider_than_a_thread_page_is_read_through(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_mention_scan as scan

        crew = _crew(self.root)
        client = _WakeClient()
        for number in (7, 8, 9):
            client.add_issue(number)
        await self._sweep(client)
        key = provider.key_from_parts(OWNER, REPO, "gitlab")
        client.note(7, 300, "bob", "no mention", at=_LATER)
        client.note(8, 301, "bob", "no mention", at=_LATER)
        client.note(9, 302, "alice", "@crew-bot behind the tie", at=_LATER)  # same time
        with mock.patch.object(scan, "THREAD_PAGE", 2):
            _, wake = await self._sweep(client, key)
            wake.assert_not_awaited()  # the first page of the tie, read
            woken, wake = await self._sweep(client, key)
        self.assertEqual(self._reasons(wake), ["#9 asked 302"])
        self.assertIn(crew["id"], woken)

    async def test_a_github_resume_finds_a_question_moved_up_many_pages(self):
        crew = _crew(self.root)
        client = _WakeClient()
        client.list_thread_comments = mock.Mock(side_effect=AssertionError("GitLab path"))
        client.list_repo_comments_since = mock.Mock(return_value=[])
        await self._sweep(client)
        cap, size = cr.MAX_TIED_COMMENT_PAGES, cr.REPO_COMMENTS_PAGE

        def row(cid, created, body):
            return {
                "id": cid,
                "number": 5,
                "is_pr": False,
                "actor": "alice",
                "created_at": created,
                "updated_at": _LATER,
                "body": body,
            }

        rows = [row(f"e{i}", _OLD, "old") for i in range(cap * size)]
        rows.append(row("q", _LATER, "@crew-bot behind the tie"))

        def page_of(o, r, since, page=1, **kw):
            live = sorted(
                (c for c in rows if c["updated_at"][:19] > since[:19]),
                key=lambda c: c["updated_at"],
            )
            return live[(page - 1) * size : page * size]

        client.list_repo_comments_since.side_effect = page_of
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()
        # A bulk edit moves all but one page of the tie past it: the question moves
        # from behind page five up to page two.
        for i in range((cap - 1) * size):
            rows[i] = {**rows[i], "updated_at": _LATEST}
        woken, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#5 asked q"])
        self.assertIn(crew["id"], woken)

    async def test_an_ask_handed_back_then_consumed_by_a_retry_is_not_asked_again(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_wake_inflight as inflight

        crew, client = await self._carried_ask()
        await self._end_turn(crew, "unconsumed")  # handed back; a retry is on its way
        self.wake_result = False  # the retry runs, so the crew is busy
        await self._sweep(client)
        self.assertEqual(self._pending()[crew["id"]], ["#5 asked 101"])
        off_loop(inflight._resolve, f"crew-{crew['id']}", ["#5 asked 101"], True)
        self.wake_result = True
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()
        self.assertNotIn(crew["id"], self._pending())

    async def test_an_issue_updated_between_the_two_gitlab_lists_is_not_passed(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_mention_scan as scan

        crew = _crew(self.root)
        client = _WakeClient()
        client.add_issue(7)
        await self._sweep(client)
        key = provider.key_from_parts(OWNER, REPO, "gitlab")
        before = "2099-01-01T00:00:05Z"
        real = client.list_threads_updated_since

        def interleaved(owner, repo, since, *, pr=False, **kw):
            rows = real(owner, repo, since, pr=pr, **kw)
            if not pr and not client.pulls:
                # Right after the issue list is read: a mention lands on an issue, and
                # an MR is updated just after it, both before the MR list is read.
                # Same second as the cutoff, after it: still too late for this sweep.
                client.note(7, 300, "alice", "@crew-bot in between", at="2099-01-01T00:00:05.500Z")
                client.note(3, 400, "bob", "no mention", pr=True, at="2099-01-01T00:00:05.900Z")
            return rows

        client.list_threads_updated_since = interleaved
        with mock.patch.object(scan, "_utc_now_stamp", return_value=before, create=True):
            _, wake = await self._sweep(client, key)
        wake.assert_not_awaited()
        woken, wake = await self._sweep(client, key)
        self.assertEqual(self._reasons(wake), ["#7 asked 300"])
        self.assertIn(crew["id"], woken)

    async def test_a_gitlab_tie_wider_than_the_page_budget_is_worked_through(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_mention_scan as scan

        crew = _crew(self.root)
        client = _WakeClient()
        for number in (7, 8, 9):
            client.add_issue(number)
        await self._sweep(client)
        key = provider.key_from_parts(OWNER, REPO, "gitlab")
        client.note(7, 300, "bob", "no mention", at=_LATER)
        client.note(8, 301, "bob", "no mention", at=_LATER)
        client.note(9, 302, "alice", "@crew-bot behind the tie", at=_LATER)  # same time
        reasons, woken_ids = [], set()
        with mock.patch.multiple(scan, THREAD_PAGE=1, MAX_THREAD_PAGES=1):
            for _ in range(8):  # one page per sweep: the tie resumes where it stopped
                woken, wake = await self._sweep(client, key)
                reasons += self._reasons(wake)
                woken_ids |= set(woken)
        self.assertEqual(reasons, ["#9 asked 302"])
        self.assertIn(crew["id"], woken_ids)

    def test_the_gitlab_mention_listing_reads_closed_and_merged_threads(self):
        from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client as gl

        with mock.patch.object(gl, "_glab_api", return_value=[]) as api:
            gl.list_threads_updated_since("g", "p", "2024-05-01T00:00:00Z")
            gl.list_threads_updated_since("g", "p", "2024-05-01T00:00:00Z", pr=True)
        for call in api.call_args_list:
            self.assertNotIn("state=opened", call.args[0])

    async def test_a_github_edit_while_the_pages_are_read_cannot_pass_a_mention(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_mention_scan as scan

        crew = _crew(self.root)
        client = _WakeClient()
        client.list_thread_comments = mock.Mock(side_effect=AssertionError("GitLab path"))
        client.list_repo_comments_since = mock.Mock(return_value=[])
        await self._sweep(client)
        size, edited_at = cr.REPO_COMMENTS_PAGE, "2099-01-01T00:00:10Z"

        def row(cid, created, body):
            return {
                "id": cid,
                "number": 5,
                "is_pr": False,
                "actor": "alice",
                "created_at": created,
                "updated_at": _LATER,
                "body": body,
            }

        rows = [row(f"e{i}", _OLD, "old") for i in range(size)]
        rows.append(row("q", _LATER, "@crew-bot right behind the page"))

        def page_of(o, r, since, page=1, **kw):
            if page == 2 and rows[0]["updated_at"] == _LATER:
                rows[0] = {**rows[0], "updated_at": edited_at}  # edited between the reads
            live = sorted(
                (c for c in rows if c["updated_at"][:19] > since[:19]),
                key=lambda c: c["updated_at"],
            )
            return live[(page - 1) * size : page * size]

        client.list_repo_comments_since.side_effect = page_of
        with mock.patch.object(scan, "_utc_now_stamp", return_value=edited_at, create=True):
            woken, wake = await self._sweep(client)
        # The question moved up onto the page already read; reading that page again
        # finds it, and the edit at the cutoff keeps the cursor short of the present.
        self.assertEqual(self._reasons(wake), ["#5 asked q"])
        self.assertIn(crew["id"], woken)
        state = cr.read_wake_checks(OWNER, REPO, self.root)["mentions"]
        self.assertLess(state["cursor"], edited_at)

    async def test_a_github_edit_during_a_tie_read_cannot_pass_a_shifted_mention(self):
        crew = _crew(self.root)
        client = _WakeClient()
        client.list_thread_comments = mock.Mock(side_effect=AssertionError("GitLab path"))
        client.list_repo_comments_since = mock.Mock(return_value=[])
        await self._sweep(client)
        size = cr.REPO_COMMENTS_PAGE
        tie, newer, edited = _LATER, "2099-01-01T00:00:05Z", "2099-01-01T00:00:09Z"

        def row(cid, created, updated, body):
            return {
                "id": cid,
                "number": 5,
                "is_pr": False,
                "actor": "alice",
                "created_at": created,
                "updated_at": updated,
                "body": body,
            }

        rows = [row(f"e{i}", _OLD, tie, "old") for i in range(size)]
        rows.append(row("q", tie, tie, "@crew-bot right behind the tie"))
        rows += [row(f"n{i}", newer, newer, "progress") for i in range(size - 1)]

        def page_of(o, r, since, page=1, **kw):
            if page == 2 and rows[0]["updated_at"] == tie:
                rows[0] = {**rows[0], "updated_at": edited}  # edited between the reads
            live = sorted(
                (c for c in rows if c["updated_at"][:19] > since[:19]),
                key=lambda c: c["updated_at"],
            )
            return live[(page - 1) * size : page * size]

        client.list_repo_comments_since.side_effect = page_of
        woken, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#5 asked q"])
        self.assertIn(crew["id"], woken)

    async def test_a_github_resume_rereads_the_step_back_pages_after_a_shift(self):
        crew = _crew(self.root)
        client = _WakeClient()
        client.list_thread_comments = mock.Mock(side_effect=AssertionError("GitLab path"))
        client.list_repo_comments_since = mock.Mock(return_value=[])
        await self._sweep(client)
        cap, size = cr.MAX_TIED_COMMENT_PAGES, cr.REPO_COMMENTS_PAGE

        def row(cid, created, updated, body):
            return {
                "id": cid,
                "number": 5,
                "is_pr": False,
                "actor": "alice",
                "created_at": created,
                "updated_at": updated,
                "body": body,
            }

        rows = [row(f"e{i}", _OLD, _LATER, "old") for i in range(cap * size)]
        rows.append(row("q", _LATER, _LATER, "@crew-bot first behind the cap"))
        rows += [row(f"x{i}", _OLD, _LATER, "old") for i in range(49)]
        rows += [row(f"n{i}", _LATEST, _LATEST, "progress") for i in range(3)]

        def page_of(o, r, since, page=1, **kw):
            if page == cap + 1 and rows[0]["updated_at"] == _LATER:
                # After the resume's step-back pass: one edit moves q up a page.
                rows[0] = {**rows[0], "updated_at": "2099-01-03T00:00:00Z"}
            live = sorted(
                (c for c in rows if c["updated_at"][:19] > since[:19]),
                key=lambda c: c["updated_at"],
            )
            return live[(page - 1) * size : page * size]

        client.list_repo_comments_since.side_effect = page_of
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()  # the tie outran the cap
        woken, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#5 asked q"])
        self.assertIn(crew["id"], woken)

    async def test_a_gitlab_edit_while_threads_are_listed_cannot_pass_a_mention(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_mention_scan as scan

        crew = _crew(self.root)
        client = _WakeClient()
        for number in (7, 8, 9, 10):
            client.add_issue(number)
        await self._sweep(client)
        key = provider.key_from_parts(OWNER, REPO, "gitlab")
        client.note(7, 300, "bob", "no mention", at=_LATER)
        client.note(8, 301, "bob", "no mention", at=_LATER)
        with mock.patch.object(scan, "THREAD_PAGE", 2):
            await self._sweep(client, key)  # 7 and 8 read; the cursor waits at their tie
            client.note(9, 302, "alice", "@crew-bot right behind the tie", at=_LATER)
            client.note(10, 303, "bob", "progress", at=_LATEST)
            real = client.list_threads_updated_since

            def edited_between(owner, repo, since, *, page=1, **kw):
                if page == 2:  # issue 7 is edited after page 1 was listed
                    next(r for r in client.open if r["number"] == 7)[
                        "updated_at"
                    ] = "2099-01-03T00:00:00Z"
                return real(owner, repo, since, page=page, **kw)

            client.list_threads_updated_since = edited_between
            woken, wake = await self._sweep(client, key)
        self.assertIn("#9 asked 302", self._reasons(wake)[0])
        self.assertIn(crew["id"], woken)

    async def test_a_gitlab_thread_past_the_cap_keeps_its_question(self):
        crew = _crew(self.root, labels=["ui"])
        client = _WakeClient()
        await self._sweep(client)
        key = provider.key_from_parts(OWNER, REPO, "gitlab")
        for n in range(1, cr.MAX_MENTION_THREADS_PER_SWEEP + 1):
            client.note(n, 100 + n, "bob", "no mention here", at=_LATEST)
        # Same update time as the tenth thread, and its question is OLDER than it.
        client.note(20, 300, "alice", "@crew-bot over here", at=_LATER)
        next(r for r in client.open if r["number"] == 20)["updated_at"] = _LATEST

        _, wake = await self._sweep(client, key)
        wake.assert_not_awaited()
        self.assertNotIn("notes:#20", client.calls)

        woken, wake = await self._sweep(client, key)
        self.assertEqual(self._reasons(wake), ["#20 asked 300"])
        self.assertIn(crew["id"], woken)

    async def test_eviction_keeps_marks_for_issues_still_on_the_page(self):
        cid = "crew-1"
        seen = {str(n): 1 for n in range(1, cr._MAX_WAKE_SEEN + 1)}
        checks = {"scope": {cid: {"labels": ["bug"], "seen": seen}}, "new_mark": {}}
        checks["mentions"] = {}
        plan = {
            "checks": checks,
            "staged": {cid: {"seen": {"9999": 1}}},
            "visible": ["1", "9999"],
        }
        await cr.settle_new_work(_KEY, plan, {cid: True}, self.root)

        kept = checks["scope"][cid]["seen"]
        self.assertEqual(len(kept), cr._MAX_WAKE_SEEN)
        self.assertIn("1", kept)  # still eligible on the page: evicting it re-wakes
        self.assertIn("9999", kept)
        self.assertNotIn("2", kept)

    def test_the_mention_floor_is_seeded_at_whole_seconds(self):
        # GitHub's created_at has no fraction: a floor at 12:00:00.5 would drop a
        # comment written at 12:00:00.9 (served as 12:00:00Z).
        stamp = cr._now_stamp()
        self.assertTrue(stamp.endswith(".000000Z"), stamp)

    def test_mentions_match_the_whole_username(self):
        self.assertTrue(cr.mentions("hey @crew-bot.", "crew-bot"))
        self.assertTrue(cr.mentions("(@CREW-BOT)", "crew-bot"))
        self.assertFalse(cr.mentions("@crew-bot2 hi", "crew-bot"))
        self.assertFalse(cr.mentions("@crew-bot.alice hi", "crew-bot"))
        self.assertTrue(cr.mentions("thanks @crew-bot... and", "crew-bot"))
        self.assertFalse(cr.mentions("mail me at x@crew-bot", "crew-bot"))
        self.assertFalse(cr.mentions("@crew-bot", ""))


class TestWakeReads(unittest.TestCase):
    """The client reads the new-work sweep adds: the query each builds, and its shape."""

    def test_gitlab_notes_drop_system_notes_and_read_one_page(self):
        from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client as gl

        notes = [
            {
                "id": 2,
                "system": False,
                "author": {"username": "bob"},
                "created_at": "b",
                "body": "@x",
            },
            {
                "id": 1,
                "system": True,
                "author": {"username": "bot"},
                "created_at": "a",
                "body": "added",
            },
            {
                "id": 0,
                "system": False,
                "author": {"username": "amy"},
                "created_at": "a",
                "body": "hi",
            },
        ]
        with mock.patch.object(gl, "_glab_api", return_value=notes) as api:
            rows = gl.list_thread_comments("g", "p", 3, pr=True)
        path = api.call_args.args[0]
        self.assertIn("/merge_requests/3/notes?", path)
        self.assertNotIn("paginate", api.call_args.kwargs)
        self.assertEqual([r["id"] for r in rows], ["0", "2"])
        self.assertEqual(rows[1]["actor"], "bob")

    def test_gitlab_thread_notes_keep_a_bounded_body(self):
        from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client as gl
        from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client_crew_reads as reads

        notes = [
            {"id": 1, "author": {"username": "amy"}, "created_at": "a", "body": "x" * 9000},
            {"id": 2, "system": True, "body": "added label"},
            {"author": {"username": "bob"}, "body": "no id"},
        ]
        with mock.patch.object(gl, "_glab_api", return_value=notes) as api:
            rows = gl.list_thread_comments("g", "p", 3, pr=True)
        self.assertIn("/merge_requests/3/notes?", api.call_args.args[0])
        self.assertEqual([r["id"] for r in rows], ["1"])
        self.assertEqual(len(rows[0]["body"]), reads.NOTE_BODY_MAX)

    def test_gitlab_notes_match_the_whole_body_before_the_cut(self):
        from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client as gl
        from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client_crew_reads as reads

        crowd = " ".join(f"@a{i:03d}" for i in range(80))  # many handles sorting first
        body = "log " * 2000 + crowd + " cc @z-crew"
        notes = [{"id": 1, "author": {"username": "alice"}, "body": body}]
        with mock.patch.object(gl, "_glab_api", return_value=notes):
            rows = gl.list_thread_comments("g", "p", 3, mention="z-crew")
            other = gl.list_thread_comments("g", "p", 3, mention="z-crew2")
        self.assertEqual(len(rows[0]["body"]), reads.NOTE_BODY_MAX)
        self.assertIs(rows[0]["asked"], True)
        self.assertIs(other[0]["asked"], False)

    def test_gitlab_notes_read_back_to_the_floor_past_a_full_page(self):
        from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client as gl

        def note(i, at):
            return {"id": i, "author": {"username": "amy"}, "created_at": at, "body": "x"}

        newer = [note(100 + i, "2024-05-02T00:00:00Z") for i in range(50)]  # a full page
        older = [note(7, "2024-05-01T12:00:00Z"), note(6, "2024-04-30T00:00:00Z")]
        pages = {"1": newer, "2": older}
        with mock.patch.object(gl, "_glab_api", side_effect=lambda p, **k: pages[p[-1]]) as api:
            rows = gl.list_thread_comments("g", "p", 3, since="2024-05-01T00:00:00Z")
        self.assertEqual(api.call_count, 2)
        self.assertIn("7", [r["id"] for r in rows])  # the unread ask under the burst

        # Without a floor (no scan reached the present yet) one page is the read.
        with mock.patch.object(gl, "_glab_api", return_value=newer) as api:
            gl.list_thread_comments("g", "p", 3)
        self.assertEqual(api.call_count, 1)

    def test_gitlab_notes_that_never_reach_the_floor_are_an_incomplete_read(self):
        from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client as gl
        from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client_crew_reads as reads

        newer = [
            {"id": i, "author": {"username": "amy"}, "created_at": "2024-05-02T00:00:00Z"}
            for i in range(50)
        ]
        with mock.patch.object(gl, "_glab_api", return_value=newer) as api:
            with self.assertRaises(reads.NotesIncomplete):
                gl.list_thread_comments("g", "p", 3, since="2024-05-01T00:00:00Z")
        self.assertEqual(api.call_count, reads.MAX_NOTE_PAGES)

    def test_github_comment_read_matches_the_whole_body_in_go_regexp(self):
        import json as _json
        import re as _re

        from kiro_crew.apps.builtins.issue_radar.backend import github_client_repo_reads as gh
        from kiro_crew.apps.builtins.issue_radar.backend import mention_match as mm

        jq = gh._repo_comment_jq("Crew-Bot")
        self.assertIn("asked:", jq)
        self.assertIn("ascii_downcase | test(", jq)
        # gh --jq uses Go's regexp: no lookaround may appear in the pattern.
        pattern = mm.go_pattern("Crew-Bot")
        self.assertFalse(_re.search(r"\(\?[<=!]", pattern))
        self.assertIn(_json.dumps(pattern), jq)
        # Same verdicts as the Python rule, for a lower-cased body.
        for body in ("hi @crew-bot.", "@crew-bot", "x@crew-bot", "@crew-bot.alice", "@crew-bot2"):
            with self.subTest(body=body):
                self.assertEqual(
                    _re.search(pattern, body.lower()) is not None, mm.mentions(body, "crew-bot")
                )
        # Not a plain login: no ``asked`` field, the caller falls back to the body.
        self.assertNotIn("asked:", gh._repo_comment_jq('x") | halt'))


@pytest.mark.asyncio
async def test_failed_consumed_write_is_retried_before_turn_finishes(tmp_path, monkeypatch):
    from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime_session as crs
    from kiro_crew.apps.builtins.issue_radar.backend import crew_wake_inflight as inflight

    cid = "c1"
    slot_key = f"crew-{cid}"
    ask = "#5 asked 101"
    prompt = f"[crew wake: {ask}]\nbrief"
    monkeypatch.setattr(inflight, "BOOT", "test-process")
    monkeypatch.setattr(inflight, "_WHERE", {})
    monkeypatch.setattr(crs, "_ADMISSION_HOOK", inflight._on_admission)
    assert inflight.carry(OWNER, REPO, tmp_path, cid, slot_key, [ask]) == [ask]

    real_atomic_write = inflight.atomic_write
    writes = 0

    def flaky_atomic_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 1:
            raise OSError("disk unavailable")
        return real_atomic_write(*args, **kwargs)

    async def run_chat(*args, **kwargs):
        kwargs["_on_consumed"](True)
        await kwargs["_on_irreversibly_consumed"]()

    async def run_background_turn(_slot, turn):
        return await turn

    monkeypatch.setattr(inflight, "atomic_write", flaky_atomic_write)
    monkeypatch.setattr(crs, "_run_chat", run_chat)
    state = mock.Mock(run_background_turn=run_background_turn)
    slot = mock.Mock(key=slot_key)
    slot._queue = []

    await await_until(
        asyncio.ensure_future(crs._capped_run_chat(state, slot, prompt)),
        what="the capped crew turn",
    )

    assert writes == 2
    stamp = inflight._read(OWNER, REPO, tmp_path)[cid][ask]
    assert stamp.split(" ", 1)[0] == inflight._DONE


class TestWakeTimers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        reset_singleton()
        self.addCleanup(reset_singleton)

    def test_the_watcher_interval_comes_from_the_environment_clamped(self):
        env = watch_mod.POLL_INTERVAL_ENV
        cases = {"": 60, "20": 20, "1": 15, "999999": 300, "soon": 60, " 45 ": 45}
        for raw, expected in cases.items():
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {env: raw}):
                self.assertEqual(watch_mod.poll_interval_secs(), expected)

    def test_the_watcher_ceiling_renews_trust_before_it_lapses(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime_session as crs

        # The watchdog pass in every sweep is what renews an unattended crew's grant.
        self.assertLessEqual(watch_mod.POLL_INTERVAL_RANGE[1] * 3, crs.TRUST_TTL_SECS)


class TestUnreadableWakeState(unittest.TestCase):
    """A state file that exists but cannot be read is never written over: an empty
    fallback saved in its place would erase the pending and carried asks it holds."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _garble(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

    def test_unreadable_wake_checks_raise_instead_of_reading_empty(self):
        path = cr.wake_checks_path(OWNER, REPO, self.root)
        self._garble(path)
        with self.assertRaises(cr.WakeChecksUnreadable):
            cr.read_wake_checks(OWNER, REPO, self.root)
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json")

    def test_an_unreadable_inflight_file_is_never_written_over(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_wake_inflight as inflight

        path = inflight._path(OWNER, REPO, self.root)
        self._garble(path)
        for call in (
            lambda: inflight.carry(OWNER, REPO, self.root, "c", "k", ["#1 asked by @a"]),
            lambda: inflight.uncarry(OWNER, REPO, self.root, "c", ["#1 asked by @a"]),
            lambda: inflight.review(OWNER, REPO, self.root, "c"),
        ):
            with self.assertRaises(inflight.InflightUnreadable):
                call()
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json")


class TestMentionSeenWindow(unittest.IsolatedAsyncioTestCase):
    """An unfinished scan keeps every handled id, so its resume never rewinds onto
    comments it already handled; a finished scan cuts the list back."""

    async def _scan(self, state: dict[str, Any], count: int, complete: bool) -> None:
        floor = "2026-01-01T00:00:00.000000Z"
        comments = [
            {"id": f"c{state['n'] + i}", "created_at": "2026-01-02T00:00:00Z", "body": "hi"}
            for i in range(count)
        ]
        state["n"] += count
        result = (comments, "2026-01-02T00:00:00.000000Z", [], complete)
        checks = {"mentions": state["m"]}
        state["m"].setdefault("cursor", floor)
        state["m"].setdefault("floor", floor)
        with (
            mock.patch.object(cr, "_forge_identity", return_value="crew-bot"),
            mock.patch.object(cr, "_recent_comments", return_value=result),
        ):
            await cr._detect_mentions(_KEY, [], [], checks, [], True, {})

    async def test_an_unfinished_scan_keeps_every_handled_id(self):
        state: dict[str, Any] = {"n": 0, "m": {}}
        for _ in range(3):
            await self._scan(state, 500, complete=False)
        self.assertEqual(len(state["m"]["seen"]), 1500)
        self.assertEqual(state["m"]["seen"][0], "c0")
        await self._scan(state, 10, complete=True)
        self.assertEqual(len(state["m"]["seen"]), cr._MAX_COMMENT_IDS)
