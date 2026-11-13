"""Issue Radar: the app-owned human-answer store (``crew-answer-checks.json``).

Split out of ``test_issue_radar_crew_runtime_sweep.py`` (file line cap). These read and
write the store directly; the sweep's own answer tests stay beside the sweep.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from issue_radar_crew_runtime_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    OWNER,
    REPO,
    _app,
    _app_is_on,
    _crew,
    _FakeState,
    _isolated_crew_log,
    _private_sel_root_per_test,
    _unit_for,
)
from off_loop_helpers import off_loop
from test_issue_radar_crew_runtime_sweep import _KEY, _T0_MS, _AnswerClient, _unlabeled

from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime as cr
from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs
from kiro_crew.apps.builtins.issue_radar.backend import provider
from kiro_crew.apps.builtins.issue_radar.backend import store as store_mod


class TestAnswerStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_a_superseding_reason_is_clamped_to_the_fold_bound(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_answer_store as answers
        from kiro_crew.crew_log.projection_radar import RADAR_TEXT_LIMIT

        at = "2026-01-01T00:00:00.000000Z"
        answers.record(OWNER, REPO, self.root, 7, "alice", at, "c")
        answers.supersede(OWNER, REPO, self.root, 7, at, {"reason": "x" * (RADAR_TEXT_LIMIT * 3)})
        held = answers.answers(OWNER, REPO, self.root)["7"]["pass"]["reason"]
        self.assertEqual(len(held), RADAR_TEXT_LIMIT)

    def test_superseding_marks_only_once_and_says_so(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_answer_store as answers

        at = "2026-01-01T00:00:01.000000Z"
        answers.record(OWNER, REPO, self.root, 1, "a", at, "c")
        self.assertTrue(answers.supersede(OWNER, REPO, self.root, 1, at, {"crew_id": "x"}))
        self.assertFalse(answers.supersede(OWNER, REPO, self.root, 1, at, {"crew_id": "y"}))
        self.assertEqual(answers.answers(OWNER, REPO, self.root)["1"]["pass"]["crew_id"], "x")

    def test_an_unreadable_answer_file_is_never_written_over(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_answer_store as answers

        file = answers.path(OWNER, REPO, self.root)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("{not json", encoding="utf-8")
        with self.assertRaises(answers.AnswerFileUnreadable):
            cr.write_answer_checks(OWNER, REPO, {"7": {"checked_at": 1.0}}, self.root)
        self.assertEqual(file.read_text(encoding="utf-8"), "{not json")

    def test_an_evicted_answer_is_counted(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_answer_store as answers

        with mock.patch.object(answers, "ANSWERS_LIMIT", 3):
            for n in range(3):
                at = f"2026-01-01T00:00:0{n}.000000Z"
                self.assertTrue(answers.record(OWNER, REPO, self.root, n, "a", at, "c"))
            newest = "2027-01-01T00:00:00.000000Z"
            self.assertTrue(answers.record(OWNER, REPO, self.root, 99999, "b", newest, "c"))
        held = answers.answers(OWNER, REPO, self.root)
        self.assertEqual(sorted(held), ["1", "2", "99999"])  # the oldest went
        self.assertEqual(answers.evicted(OWNER, REPO, self.root), 1)
        # The sweep's check marks share the file and survive an answer being written.
        cr.write_answer_checks(OWNER, REPO, {"7": {"checked_at": 1.0}}, self.root)
        answers.record(OWNER, REPO, self.root, 5, "c", newest, "c")
        self.assertEqual(cr.read_answer_checks(OWNER, REPO, self.root), {"7": {"checked_at": 1.0}})
        self.assertIn("5", answers.answers(OWNER, REPO, self.root))

    def test_superseded_answers_count_toward_the_bound(self):
        """Every row is bounded, superseded ones too: answered-then-re-skipped issues
        cannot grow the file past ANSWERS_LIMIT, and each eviction is counted."""
        from kiro_crew.apps.builtins.issue_radar.backend import crew_answer_store as answers

        with mock.patch.object(answers, "ANSWERS_LIMIT", 2):
            for n in (1, 2, 3, 4):
                at = f"2026-01-01T00:00:0{n}.000000Z"
                answers.record(OWNER, REPO, self.root, n, "a", at, "c")
                answers.supersede(
                    OWNER, REPO, self.root, n, at, {"crew_id": "b", "scope": "needs-decision"}
                )
        self.assertEqual(sorted(answers.answers(OWNER, REPO, self.root)), ["3", "4"])
        self.assertEqual(answers.evicted(OWNER, REPO, self.root), 2)
        # An evicted row's removal read again stays consumed: it is refused, not re-kept.
        old = "2026-01-01T00:00:01.000000Z"
        self.assertFalse(answers.record(OWNER, REPO, self.root, 1, "a", old, "c"))
        self.assertNotIn("1", answers.answers(OWNER, REPO, self.root))
        # The store has no pruning hook a partial crew-log read could drive.
        self.assertFalse(hasattr(answers, "prune_superseded"))

    def test_a_reskip_to_an_unanswerable_scope_is_pinned_within_the_bound(self):
        """Only this row holds the new scope gate (the fold keeps the original pass),
        so it is never evicted; a new answer past the bound goes instead, counted."""
        from kiro_crew.apps.builtins.issue_radar.backend import crew_answer_store as answers

        with mock.patch.object(answers, "ANSWERS_LIMIT", 2):
            for n, scope in ((1, "architecture"), (2, "needs-decision")):
                at = f"2026-01-01T00:00:0{n}.000000Z"
                answers.record(OWNER, REPO, self.root, n, "a", at, "c")
                answers.supersede(OWNER, REPO, self.root, n, at, {"scope": scope})
            answers.record(OWNER, REPO, self.root, 3, "a", "2026-01-01T00:00:03.000000Z", "c")
            self.assertEqual(sorted(answers.answers(OWNER, REPO, self.root)), ["1", "3"])
            answers.supersede(
                OWNER, REPO, self.root, 3, "2026-01-01T00:00:03.000000Z", {"scope": "duplicate"}
            )
            # Every row is pinned now: the new answer is evicted as it is written.
            self.assertFalse(
                answers.record(OWNER, REPO, self.root, 4, "a", "2026-01-01T00:00:04.000000Z", "c")
                and "4" in answers.answers(OWNER, REPO, self.root)
            )
        self.assertEqual(sorted(answers.answers(OWNER, REPO, self.root)), ["1", "3"])
        self.assertEqual(
            answers.answers(OWNER, REPO, self.root)["1"]["pass"]["scope"], "architecture"
        )
        self.assertEqual(answers.evicted(OWNER, REPO, self.root), 2)


class TestEvictedSupersededAnswer(unittest.IsolatedAsyncioTestCase):
    """An evicted superseded answer must not let its old label removal hand the issue
    back while the crew's newer pass is still in the crew log."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.now = [_T0_MS]
        from kiro_crew.crew_log import store as crew_log_store

        clock = mock.patch.object(crew_log_store, "now_ms", side_effect=lambda: self.now[0])
        clock.start()
        self.addCleanup(clock.stop)
        self.cid = _crew(self.root, unattended=True)["id"]

    def _skip(self, number: int) -> None:
        off_loop(
            cs.commit_work_progress,
            OWNER,
            REPO,
            self.cid,
            number,
            {"phase": "skipped"},
            "skip",
            "asked which fix to take",
            skip_reason="two valid fixes; a maintainer has to pick",
            skip_scope="needs-decision",
            root=self.root,
            session_id=_unit_for(self.cid),
        )

    async def _sweep(self, client):
        client.forge_root = self.root
        with (
            mock.patch.object(provider, "client_for", return_value=client),
            mock.patch.object(cr.provider, "client_for", return_value=client),
            mock.patch.object(cr, "wake_crew", new=mock.AsyncMock(return_value=True)) as wake,
        ):
            woken = await cr.sweep_repo(_app(_FakeState()), _KEY, self.root)
        return woken, wake

    async def test_the_old_removal_hides_nothing_once_its_row_is_evicted(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_answer_store as answers

        self._skip(7)
        store_mod.write_issues_cache(
            OWNER, REPO, [{"number": 7, "state": "open", "labels": []}], root=self.root
        )
        client = _AnswerClient([_unlabeled("alice", _T0_MS + 60_000)])
        self.now[0] = _T0_MS + 120_000
        woken, _ = await self._sweep(client)
        self.assertEqual(woken, {self.cid: [cr.SIG_HUMAN_ANSWERED]})

        self.now[0] = _T0_MS + 180_000
        self._skip(7)  # still undecided: the answer is superseded
        with mock.patch.object(answers, "ANSWERS_LIMIT", 1):
            answers.record(OWNER, REPO, self.root, 99, "bob", "2099-01-01T00:00:00.000000Z", "c")
        self.assertNotIn("7", answers.answers(OWNER, REPO, self.root))

        for _ in range(2):
            woken, wake = await self._sweep(client)
            self.assertEqual(woken, {})
            wake.assert_not_awaited()
        skipped = {
            row["number"] for row in off_loop(cs.read_skips, OWNER, REPO, self.root).values()
        }
        self.assertIn(7, skipped)
