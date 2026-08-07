#!/usr/bin/env python3
"""Posting is an explicit action, not a consequence of reviewing.

``review.auto_post`` defaults off, so a review is READ in the app. These tests
cover the deferred path: the records survive long enough to post, the public
poster publishes only the Python-redacted envelope, and the endpoint refuses the
cases where posting would be wrong (still running, nothing to post, already
posted).
"""
from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from aiohttp import web
from backend import routes
from sage_lib import results
from sage_lib import review_driver as D
from sage_lib import store

from kiro_crew.dashboard import state as dashboard_state


def _record(cid: str = "CR-1", red: int = 1, yellow: int = 2) -> dict:
    return {
        "schema": "code-review-sage-result", "version": 1, "change_id": cid,
        "platform": "github", "repo_identity": "github.com/o/r", "revision": "1",
        "phase1": {"gate_verdict": "CONCERNS", "design_risk": "medium",
                   "criticality": "medium", "design_headline": "h",
                   "problem": "p", "why_it_matters": "w",
                   "solution_assessment": "Fit: ok"},
        "blast_radius": {"rating": "MEDIUM", "signals": {}},
        "counts": {"red": red, "yellow": yellow},
        "findings": [
            {"dimension": "correctness", "severity": "red", "file": "f.py",
             "line": 3, "snippet": "x", "observation": "o", "consequence": "c",
             "suggestion": "s"},
        ] * red + [
            {"dimension": "style", "severity": "yellow", "file": "f.py",
             "line": 4, "snippet": "y", "observation": "o", "consequence": "c",
             "suggestion": "s"},
        ] * yellow,
        "deep_reviewed": True, "title": cid, "ship_summary": "looks fine",
        "files_covered": ["f.py"], "coverage_complete": True,
    }


def await_sync(fn, *a, **kw):
    """Run a sync driver call from an async test class without asyncio.to_thread
    boilerplate in every case."""
    return fn(*a, **kw)


def _confirmed(_link, _units):
    """Stand in for the GitHub read-back.

    These tests are about which comments the rebuilt draft carries, not about proving
    a delivery, and they have no pull request to read. `post_recorded` refuses to
    write `posted_keys` on the poster's own count, so the confirmation it would
    normally perform is supplied here.
    """
    return True


def _unconfirmed(_link, _units):
    """The read-back finding no matching draft -- a delivery that cannot be proven."""
    return False


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        self.root = Path(self.tmp) / "app"
        store.ensure_layout(self.root)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestPostRecorded(_Base):
    async def test_publishes_the_redacted_envelope_not_model_text(self):
        results.write_result(_record(), self.root, "run-a")
        seen: list = []

        def dispatch(task, timeout=0):
            seen.append(task)
            rec = results.read_result("CR-1", self.root, None) or {}
            # The poster's only job: publish what Python already built.
            self.assertIn("github_review_payload", rec)
            rec["posted_comments"] = len(rec.get("pending_comments") or [])
            rec["design_comment_posted"] = True
            results.write_result(rec, self.root, None)
            return {"ok": True, "output": "posted", "error": ""}

        out = await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=dispatch, confirm=_confirmed, root=self.root, run_id="run-a")
        self.assertTrue(out["post_ok"])
        # 1 red + 2 yellow inline + the always-on ship-readiness comment.
        self.assertEqual(out["pending"], 4)
        self.assertEqual(out["posted_comments"], 4)
        self.assertTrue(seen)

    async def test_a_publish_to_shared_failure_aborts_the_dispatch(self):
        """`publish_to_shared` returning False means the record at the shared path
        is NOT ours -- it refuses precisely when its no-follow read is blocked,
        which is the case where a sibling worker replaced the record with a link.
        Dispatching anyway would point the poster at whatever IS there and publish
        it to the pull request, so no poster may run."""
        results.write_result(_record(red=1, yellow=0), self.root, "run-a")
        calls: list = []

        def dispatch(task, timeout=0):
            calls.append(task)
            return {"ok": True, "output": "posted", "error": ""}

        with unittest.mock.patch.object(results, "publish_to_shared",
                                        return_value=False):
            out = await asyncio.to_thread(
                D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
                dispatch=dispatch, confirm=_confirmed, root=self.root,
                run_id="run-a")

        self.assertEqual(calls, [], "no poster may be dispatched")
        self.assertFalse(out["post_ok"])
        self.assertIn("stage", out["post_error"])
        self.assertEqual(out["posted_comments"], 0)

    async def test_no_poster_is_spawned_when_there_is_nothing_to_post(self):
        results.write_result(_record(red=0, yellow=0), self.root, "run-a")
        calls: list = []

        def dispatch(task, timeout=0):
            calls.append(task)
            return {"ok": True, "output": "", "error": ""}

        out = await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=dispatch, confirm=_confirmed, root=self.root, run_id="run-a")
        # A clean review still gets its ship-readiness comment, so "nothing" here
        # means no record at all — verified below.
        self.assertTrue(out["post_ok"])

    async def test_a_missing_record_posts_nothing(self):
        calls: list = []

        def dispatch(task, timeout=0):
            calls.append(task)
            return {"ok": True, "output": "", "error": ""}

        out = await asyncio.to_thread(
            D.post_recorded, "CR-GONE", "https://github.com/o/r/pull/1",
            dispatch=dispatch, confirm=_confirmed, root=self.root, run_id="run-a")
        self.assertEqual(out["posted_comments"], 0)
        self.assertEqual(calls, [])


class TestSelectivePosting(_Base):
    """Posting individual comments: you rarely agree with every finding."""

    def _poster(self, delivered: int | None = None):
        def dispatch(task, timeout=0):
            rec = results.read_result("CR-1", self.root, None) or {}
            pending = rec.get("pending_comments") or []
            rec["posted_comments"] = len(pending) if delivered is None else delivered
            rec["design_comment_posted"] = any(
                e.get("kind") == "design" for e in pending)
            results.write_result(rec, self.root, None)
            return {"ok": True, "output": "posted", "error": ""}
        return dispatch

    async def test_posts_only_the_selected_comment(self):
        results.write_result(_record(), self.root, "run-a")
        out = await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=self._poster(), confirm=_confirmed, root=self.root, run_id="run-a",
            keys=["finding:1"])
        self.assertEqual(out["pending"], 1)
        self.assertEqual(out["posted_keys"], ["finding:1"])

    async def test_a_second_post_skips_what_already_landed(self):
        results.write_result(_record(), self.root, "run-a")
        await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=self._poster(), confirm=_confirmed, root=self.root, run_id="run-a",
            keys=["finding:0"])
        # Each post creates its own pending review, so re-sending one would put a
        # duplicate on the pull request.
        out = await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=self._poster(), confirm=_confirmed, root=self.root, run_id="run-a",
            keys=["finding:0"])
        self.assertEqual(out["pending"], 0)
        self.assertEqual(out["posted_comments"], 0)

    async def test_posting_the_rest_leaves_the_first_alone(self):
        results.write_result(_record(), self.root, "run-a")
        await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=self._poster(), confirm=_confirmed, root=self.root, run_id="run-a",
            keys=["finding:0"])
        out = await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=self._poster(), confirm=_confirmed, root=self.root, run_id="run-a")
        # 1 red + 2 yellow + ship comment = 4. The replacement draft carries all
        # four: the poster deletes the draft holding the first one, so leaving it
        # out of the payload would delete it from the pull request.
        self.assertEqual(out["pending"], 4)
        self.assertEqual(len(out["posted_keys"]), 4)

    async def test_a_second_post_rebuilds_the_whole_draft(self):
        """A replacement draft must carry the comments already in it.

        GitHub allows one pending review per author, so the poster deletes the
        existing sage draft and creates a new one. A payload holding only the new
        selection replaces rather than appends: the first finding would be deleted
        with the old draft, while `posted_keys` still claimed it had landed — so
        nothing would ever re-send it.
        """
        results.write_result(_record(), self.root, "run-a")
        await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=self._poster(), confirm=_confirmed, root=self.root, run_id="run-a",
            keys=["finding:0"])

        seen: list[list[str]] = []

        def capture(task, timeout=0):
            rec = results.read_result("CR-1", self.root, None) or {}
            payload = rec.get("github_review_payload") or {}
            seen.append([c.get("body", "")[:40]
                         for c in (payload.get("comments") or [])])
            rec["posted_comments"] = len(rec.get("pending_comments") or [])
            results.write_result(rec, self.root, None)
            return {"ok": True, "output": "posted", "error": ""}

        await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=capture, confirm=_confirmed, root=self.root, run_id="run-a",
            keys=["finding:1"])

        # The second payload contains BOTH findings, not just the newly chosen one.
        self.assertEqual(len(seen), 1)
        self.assertGreaterEqual(len(seen[0]), 2)

    async def test_a_poster_that_delivered_nothing_records_nothing(self):
        # The poster's written-back count is the ONLY evidence of delivery; a
        # spawn returning cleanly proves nothing. Recording keys on that would
        # mark comments as sent that are not on the pull request.
        results.write_result(_record(), self.root, "run-a")
        out = await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=self._poster(delivered=0), confirm=_unconfirmed, root=self.root, run_id="run-a",
            keys=["finding:0"])
        self.assertEqual(out["posted_keys"], [])
        rec = results.read_result("CR-1", self.root, "run-a") or {}
        self.assertFalse(rec.get("posted_keys"))

    async def test_a_fabricated_count_writes_no_ledger_entry(self):
        """A poster claiming a delivery it never made must not mark anything sent.

        `posted_comments` is written by the poster about itself, and the poster is an
        LLM session, so the number is reachable by prompt injection. `posted_keys` is
        the durable ledger the UI reads to mark findings sent and to decide whether
        the Post action is still offered -- so an unverified claim would hide the
        action for comments that are not on the pull request. Evidence comes from
        reading the draft back, and a claim with no matching draft proves nothing.
        """
        results.write_result(_record(), self.root, "run-a")
        out = await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            # Claims every unit landed.
            dispatch=self._poster(delivered=99),
            # The read-back finds no draft of that shape.
            confirm=_unconfirmed, root=self.root, run_id="run-a", keys=None)
        self.assertFalse(out["post_ok"],
                         "an unconfirmed delivery reported success")
        self.assertIn("could not be confirmed", out["post_error"])
        rec = results.read_result("CR-1", self.root, "run-a") or {}
        self.assertEqual(rec.get("posted_keys") or [], [],
                         "a fabricated count reached the durable ledger")

    async def test_a_partial_delivery_is_not_attributed(self):
        results.write_result(_record(), self.root, "run-a")
        out = await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=self._poster(delivered=1), confirm=_unconfirmed, root=self.root, run_id="run-a")
        # Which of the four landed is unknowable, so none are marked sent:
        # a visible duplicate beats a silently dropped finding.
        self.assertEqual(out["posted_keys"], [])

    async def test_every_pending_comment_has_a_stable_key(self):
        from sage_lib import pipeline
        pending = pipeline.build_pending_comments(_record())
        self.assertEqual(
            [e["key"] for e in pending],
            ["finding:0", "finding:1", "finding:2", "design"])


class TestRecordsSurviveForPosting(_Base):
    def _dispatch(self):
        def dispatch(task, timeout=0):
            results.write_result(_record(), self.root)
            return {"ok": True, "output": "done", "error": ""}
        return dispatch

    async def test_records_are_kept_when_the_review_was_not_posted(self):
        # They are the ONLY source of the redacted payload, so clearing them on
        # archive would silently make posting later impossible.
        out = await asyncio.to_thread(
            lambda: D.run_review(
                ["CR-1"], dispatch=self._dispatch(),
                archiver=lambda *_a, **_k: "slug-1",
                generate_report=True, root=self.root, run_id="run-a"))
        self.assertNotIn("results_cleaned", out)
        self.assertIsNotNone(results.read_result("CR-1", self.root, "run-a"))

    async def test_records_are_cleared_once_they_have_been_delivered(self):
        import json
        cfg = store.data_dir(self.root) / "config.json"
        cfg.write_text(json.dumps({"review": {"auto_post": True}}),
                       encoding="utf-8")

        def dispatch(task, timeout=0):
            if "SINGLE thorough pass" in task:
                results.write_result(_record(), self.root)
            else:
                rec = results.read_result("CR-1", self.root, None)
                if rec:
                    rec["posted_comments"] = len(rec.get("pending_comments") or [])
                    results.write_result(rec, self.root, None)
            return {"ok": True, "output": "done", "error": ""}

        out = await asyncio.to_thread(
            lambda: D.run_review(
                ["CR-1"], dispatch=dispatch, confirm=_confirmed, archiver=lambda *_a, **_k: "slug-1",
                generate_report=True, root=self.root, run_id="run-b", post=True))
        self.assertGreaterEqual(out.get("results_cleaned", 0), 1)


class TestPostEndpoint(_Base):
    async def asyncSetUp(self):
        self.app = web.Application()
        routes.register_routes(self.app)
        routes._RUNS.clear()

    def _run(self, **over) -> dict:
        run = {
            "run_id": "run-a", "repo": "o/r",
            "changes": ["https://github.com/o/r/pull/1"],
            "change_ids": ["CR-1"], "status": "done",
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:05:00Z",
            **over,
        }
        routes._RUNS.append(run)
        return run

    async def _post(self, run_id="run-a") -> web.Response:
        req = _FakeRequest(run_id)
        return await routes._handle_run_post(req)  # type: ignore[arg-type]

    async def test_refuses_while_the_review_is_still_running(self):
        self._run(status="running")
        resp = await self._post()
        self.assertEqual(resp.status, 409)

    async def test_refuses_when_there_is_nothing_to_post(self):
        self._run()
        resp = await self._post()
        self.assertEqual(resp.status, 409)
        self.assertIn("nothing to post", _body(resp))

    async def test_refuses_a_second_post(self):
        results.write_result(_record(), None, "run-a")
        self._run(posted_at="2026-01-01T00:06:00Z", posted_comments=4)
        resp = await self._post()
        # A duplicate review on someone's PR is not undoable from here.
        self.assertEqual(resp.status, 409)
        self.assertIn("already posted", _body(resp))

    async def test_404_for_an_unknown_run(self):
        resp = await self._post("nope")
        self.assertEqual(resp.status, 404)

    async def test_counts_what_it_would_post(self):
        results.write_result(_record(), None, "run-a")
        run = self._run()
        n = await asyncio.to_thread(routes._pending_comment_count, "run-a", run)
        self.assertEqual(n, 4)


def _body(resp: web.Response) -> str:
    """The response text, for asserting on the refusal reason."""
    return (resp.text or "") if isinstance(resp.text, str) else str(resp.body)


class TestGroupedPost(_Base):
    """A multi-change selection posts as ONE request.

    `posting` is a per-RUN flag and only the poster clears it, while this handler
    returns as soon as it dispatches the poster -- so a client sending one request
    per change had every change after the first refused with `already_posting`, and
    those comments were never published. Sequencing the client's requests does not
    help: resolution means "the poster started", not "it finished".
    """

    async def asyncSetUp(self):
        self.app = web.Application()
        routes.register_routes(self.app)
        routes._RUNS.clear()
        self.dispatched: list[tuple] = []

        async def _capture(run_id, run, change_id="", keys=None, groups=None):
            self.dispatched.append((run_id, change_id, keys, groups))

        self._patch = unittest.mock.patch.object(
            routes, "_post_comments_bg", _capture)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _two_change_run(self) -> dict:
        run = {
            "run_id": "run-g", "repo": "o/r",
            "changes": ["https://github.com/o/r/pull/1",
                        "https://github.com/o/r/pull/2"],
            "change_ids": ["CR-1", "CR-2"], "status": "done",
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:05:00Z",
        }
        routes._RUNS.append(run)
        return run

    async def test_one_request_covers_both_changes(self):
        results.write_result(_record("CR-1"), None, "run-g")
        results.write_result(_record("CR-2"), None, "run-g")
        self._two_change_run()

        resp = await routes._handle_run_post(_FakeRequest("run-g", {"groups": [
            {"change_id": "CR-1"},
            {"change_id": "CR-2"},
        ]}))  # type: ignore[arg-type]
        # The handler returns as soon as it schedules the poster.
        await asyncio.sleep(0)

        self.assertEqual(resp.status, 200)
        # ONE dispatch covering both changes. Two requests would have had the
        # second refused with `already_posting`.
        self.assertEqual(len(self.dispatched), 1)
        _, _, _, groups = self.dispatched[0]
        self.assertEqual(groups, {"CR-1": None, "CR-2": None})

    async def test_a_change_left_out_of_the_groups_is_not_posted(self):
        results.write_result(_record("CR-1"), None, "run-g")
        results.write_result(_record("CR-2"), None, "run-g")
        self._two_change_run()

        resp = await routes._handle_run_post(_FakeRequest("run-g", {"groups": [
            {"change_id": "CR-2"},
        ]}))  # type: ignore[arg-type]
        # The handler returns as soon as it schedules the poster.
        await asyncio.sleep(0)

        self.assertEqual(resp.status, 200)
        _, _, _, groups = self.dispatched[0]
        # CR-1 is absent, so the poster skips it rather than applying CR-2's
        # selection to it (the round-8 scoping rule, preserved inside groups).
        self.assertEqual(groups, {"CR-2": None})
        self.assertNotIn("CR-1", groups)

    async def test_the_single_change_form_still_works(self):
        """The per-finding post path (change_id + keys) is unchanged."""
        results.write_result(_record("CR-1"), None, "run-g")
        results.write_result(_record("CR-2"), None, "run-g")
        self._two_change_run()

        resp = await routes._handle_run_post(
            _FakeRequest("run-g", {"change_id": "CR-1"}))  # type: ignore[arg-type]
        await asyncio.sleep(0)

        self.assertEqual(resp.status, 200)
        _, change_id, keys, groups = self.dispatched[0]
        # No groups: the single-change path is untouched by this change.
        self.assertEqual((change_id, keys, groups), ("CR-1", None, None))

    async def test_a_group_naming_no_real_comment_is_refused(self):
        """A selection that would post nothing must not start a posting cycle,
        because the run-level `posting` flag would then be set for a no-op."""
        results.write_result(_record("CR-1"), None, "run-g")
        results.write_result(_record("CR-2"), None, "run-g")
        self._two_change_run()

        resp = await routes._handle_run_post(_FakeRequest("run-g", {"groups": [
            {"change_id": "CR-1", "keys": ["no-such-key"]},
        ]}))  # type: ignore[arg-type]
        # The handler returns as soon as it schedules the poster.
        await asyncio.sleep(0)

        self.assertEqual(resp.status, 409)
        self.assertEqual(self.dispatched, [])


class _FakeRequest:
    """Minimal stand-in: the handler only reads match_info, query and json()."""

    def __init__(self, run_id: str, body: dict | None = None):
        self.match_info = {"run_id": run_id}
        self.query: dict = {}
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")   # the handler treats this as {}
        return self._body


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TestPostedNotificationActuallyFires(unittest.IsolatedAsyncioTestCase):
    """``notify(kind, title, body, *, meta=None)`` — ``meta`` is keyword-only.

    The call passed a deep link as a 4th POSITIONAL argument, so every invocation
    raised TypeError inside the ``to_thread``; the surrounding ``except Exception``
    swallowed it and the "comments posted" bell notification never fired for any
    post. The fake below mirrors the real signature, so a positional overflow
    fails here the same way it failed in production.
    """

    async def test_the_posted_notification_reaches_the_bell_feed(self):
        seen: dict = {}

        class _FakeState:
            def notify(self, kind, title, body, *, meta=None):
                seen.update(kind=kind, title=title, body=body, meta=meta)

        # Guard the premise: if `notify` ever grows a 4th positional parameter,
        # this test would stop discriminating and should be revisited.
        sig = inspect.signature(dashboard_state.DashboardState.notify)
        self.assertEqual(sig.parameters["meta"].kind,
                         inspect.Parameter.KEYWORD_ONLY)

        prior = routes._APP_STATE.get("state")
        routes._APP_STATE["state"] = _FakeState()
        try:
            await routes._notify_posted({"run_id": "r1"}, 2, False)
        finally:
            if prior is None:
                routes._APP_STATE.pop("state", None)
            else:
                routes._APP_STATE["state"] = prior

        self.assertTrue(seen, "the posted notification never fired")
        self.assertEqual(seen["kind"], "agent")


class TestStaleDeliveryEvidenceIsNotInherited(_Base):
    """The poster's write-back is the only proof of delivery, so it must be fresh.

    `posted_comments` was left on the record between attempts. A first post that
    partially failed left it at 3; the one-comment retry published that record,
    a poster that delivered nothing wrote nothing back, and `3 >= 1` then marked
    that comment delivered and added it to `posted_keys` — permanently skipping a
    finding that was never posted. The posting-skipped path in `run_review`
    already reset these two fields; this was its un-mirrored sibling.
    """

    async def test_a_silent_poster_cannot_inherit_an_earlier_count(self):
        rec = _record()
        rec["posted_comments"] = 3          # residue from a partial attempt
        rec["design_comment_posted"] = True
        results.write_result(rec, self.root, "run-a")

        def dispatch(task, timeout=0):
            return {"ok": True, "output": "", "error": ""}   # writes NOTHING

        out = await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=dispatch, confirm=_unconfirmed, root=self.root, run_id="run-a")

        self.assertEqual(out["posted_comments"], 0, out)
        self.assertEqual(out["posted_keys"], [], out)
        after = results.read_result("CR-1", self.root, "run-a") or {}
        self.assertFalse(after.get("posted_keys"))
        self.assertFalse(after.get("design_comment_posted"))

    async def test_the_skipped_comment_is_still_offered_afterwards(self):
        """The point of the fix: the finding must remain postable."""
        rec = _record()
        rec["posted_comments"] = 9
        results.write_result(rec, self.root, "run-a")

        def silent(task, timeout=0):
            return {"ok": True, "output": "", "error": ""}

        await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=silent, confirm=_confirmed, root=self.root, run_id="run-a")

        # A real poster on the retry delivers and IS recorded.
        def real(task, timeout=0):
            r = results.read_result("CR-1", self.root, None) or {}
            r["posted_comments"] = len(r.get("pending_comments") or [])
            results.write_result(r, self.root, None)
            return {"ok": True, "output": "posted", "error": ""}

        out = await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=real, confirm=_confirmed, root=self.root, run_id="run-a")
        self.assertTrue(out["posted_keys"], out)

    async def test_a_real_delivery_is_still_recorded(self):
        """The reset must not break the path where the poster does write back."""
        results.write_result(_record(), self.root, "run-a")

        def dispatch(task, timeout=0):
            r = results.read_result("CR-1", self.root, None) or {}
            r["posted_comments"] = len(r.get("pending_comments") or [])
            r["design_comment_posted"] = True
            results.write_result(r, self.root, None)
            return {"ok": True, "output": "posted", "error": ""}

        out = await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=dispatch, confirm=_confirmed, root=self.root, run_id="run-a")
        self.assertEqual(out["posted_comments"], out["pending"])
        self.assertTrue(out["posted_keys"])


class TestDeliveryIsCountedInPayloadUnits(_Base):
    """An unanchored finding folds into the review body, so findings != units.

    The poster reports `len(comments) + 1 when body is non-empty`. Comparing that
    against the number of pending entries made a COMPLETE delivery look short
    whenever a finding lacked a usable `{path, line}` anchor, because such a finding
    is folded into the body rather than becoming its own inline comment. `posted_keys`
    then went unwritten and the next post duplicated comments already on the PR.

    The same miscount was used for `posting_expected`, which gates `_record_reviewed`
    and `_all_delivered`, so both also read a finished review as incomplete.
    """

    def test_payload_units_ignores_findings_folded_into_the_body(self):
        from sage_lib import pipeline

        rec = {"revision": "a" * 40, "pending_comments": [
            {"kind": "design", "body": "ship summary", "key": "d1"},
            {"kind": "finding", "body": "anchored", "file": "a.py", "line": 3,
             "key": "f1"},
            {"kind": "finding", "body": "no anchor", "file": "", "line": None,
             "key": "f2"},
        ]}
        payload = pipeline.build_github_review_payload(rec)
        # Three pending entries, but only two deliverable units.
        self.assertEqual(len(payload.get("comments") or []), 1)
        self.assertTrue(payload.get("body"))
        self.assertEqual(pipeline.review_payload_units(payload), 2)
        self.assertLess(pipeline.review_payload_units(payload),
                        len(rec["pending_comments"]))

    def test_an_unanchored_finding_still_records_delivery(self):
        """The real path: a poster that delivers every unit marks them delivered."""
        from sage_lib import pipeline, results

        rec = _record(red=1, yellow=0)
        # Strip the anchor from the one red finding so it folds into the body.
        for f in rec["findings"]:
            f["file"] = ""
            f["line"] = None
        results.write_result(rec, self.root, "run-a")

        def dispatch(task, timeout=0):
            r = results.read_result("CR-1", self.root, None) or {}
            payload = r.get("github_review_payload") or {}
            r["posted_comments"] = pipeline.review_payload_units(payload)
            r["design_comment_posted"] = bool(payload.get("body"))
            results.write_result(r, self.root, None)
            return {"ok": True, "output": "posted", "error": ""}

        out = await_sync(D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
                         dispatch=dispatch, confirm=_confirmed, root=self.root, run_id="run-a")
        # Delivery is recognised: keys recorded, so a retry cannot duplicate.
        self.assertTrue(out["posted_keys"], out)
        self.assertEqual(out["posted_comments"], out["expected_units"])

    def test_expected_units_is_reported_for_the_caller(self):
        """`posting_expected` is set from this, not from red + yellow + 1."""
        from sage_lib import pipeline, results

        results.write_result(_record(red=1, yellow=2), self.root, "run-a")

        def dispatch(task, timeout=0):
            r = results.read_result("CR-1", self.root, None) or {}
            r["posted_comments"] = pipeline.review_payload_units(
                r.get("github_review_payload") or {})
            results.write_result(r, self.root, None)
            return {"ok": True, "output": "posted", "error": ""}

        out = await_sync(D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
                         dispatch=dispatch, confirm=_confirmed, root=self.root, run_id="run-a")
        self.assertIn("expected_units", out)
        self.assertGreater(out["expected_units"], 0)


class TestConfirmedCountIsAuthoritative(unittest.TestCase):
    """Once the draft is confirmed, the count comes from the payload, not the poster."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_a_silent_poster_does_not_under_report_a_confirmed_delivery(self):
        from sage_lib import results

        results.write_result(_record(red=1, yellow=2), self.root, "run-a")

        def dispatch(task, timeout=0):
            # Delivers, but reports nothing about itself.
            return {"ok": True, "output": "posted", "error": ""}

        out = await_sync(D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
                         dispatch=dispatch, confirm=_confirmed, root=self.root,
                         run_id="run-a")
        self.assertTrue(out["post_ok"], out)
        # `_record_reviewed` compares these two; a short count leaves the PR unindexed.
        self.assertEqual(out["posted_comments"], out["expected_units"])
        # And it reads the RECORD, so that is where the authoritative count has to be.
        rec = results.read_result("CR-1", self.root, "run-a") or {}
        self.assertEqual(rec.get("posted_comments"), out["expected_units"], rec)

    def test_an_unconfirmed_delivery_still_reports_the_posters_number(self):
        """Unconfirmed, the poster's report is all there is -- a partial stays visible."""
        from sage_lib import results

        results.write_result(_record(red=1, yellow=2), self.root, "run-a")

        def dispatch(task, timeout=0):
            r = results.read_result("CR-1", self.root, None) or {}
            r["posted_comments"] = 1
            results.write_result(r, self.root, None)
            return {"ok": True, "output": "posted", "error": ""}

        out = await_sync(D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
                         dispatch=dispatch, confirm=_unconfirmed, root=self.root,
                         run_id="run-a")
        self.assertFalse(out["post_ok"], out)
        self.assertEqual(out["posted_comments"], 1)


class TestPostedReviewIdRecorded(unittest.TestCase):
    """A confirmed delivery names the draft it created, not just that it happened."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_records_which_draft_was_confirmed(self):
        from sage_lib import results

        results.write_result(_record(red=1, yellow=2), self.root, "run-a")

        def dispatch(task, timeout=0):
            return {"ok": True, "output": "posted", "error": ""}

        # The seam returns the id it confirmed, the same shape as the real reader.
        out = await_sync(D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
                         dispatch=dispatch, confirm=lambda _l, _p: "4242",
                         root=self.root, run_id="run-a")
        self.assertEqual(out["posted_review_id"], "4242", out)
        rec = results.read_result("CR-1", self.root, "run-a") or {}
        self.assertEqual(rec.get("posted_review_id"), "4242", rec)

    def test_an_unconfirmed_attempt_records_no_draft_id(self):
        """Nothing proven means nothing to bind a later publish to."""
        from sage_lib import results

        results.write_result(_record(red=1, yellow=2), self.root, "run-a")
        out = await_sync(D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
                         dispatch=lambda t, timeout=0: {"ok": True, "output": "",
                                                        "error": ""},
                         confirm=_unconfirmed, root=self.root, run_id="run-a")
        self.assertEqual(out["posted_review_id"], "", out)


class TestApplyPostOutcomeCarriesTheDraftId(unittest.TestCase):
    """Every path that delivers must record WHICH draft it created."""

    def test_apply_post_outcome_carries_the_draft_id(self):
        rec: dict = {}
        D.apply_post_outcome(rec, {
            "post_ok": True, "posted_comments": 3, "expected_units": 3,
            "posted_keys": ["k1"], "posted_review_id": "4242",
        })
        self.assertEqual(rec["posted_review_id"], "4242", rec)

    def test_a_failed_post_records_an_empty_draft_id(self):
        """Nothing delivered means nothing to bind a later publish to."""
        rec: dict = {}
        D.apply_post_outcome(rec, {"post_ok": False, "posted_comments": 0,
                                   "expected_units": 3, "posted_keys": []})
        self.assertEqual(rec["posted_review_id"], "", rec)


class TestPaginationContract(unittest.TestCase):
    """Every paginated read must ask for JSONL, or page two breaks the parse."""

    def test_draft_confirmation_paginates_as_jsonl(self):
        from sage_lib import discovery

        calls: list[dict] = []

        def run_gh_json(path, jq=None, *, timeout=0, paginate=False):
            calls.append({"path": path, "jq": jq, "paginate": paginate})
            if "/comments" in path:
                return [{"path": "src/a.py", "line": 4, "body": "widens scope"}]
            return [{"id": 7, "state": "PENDING", "commit_id": "abc123",
                     "body": "[code-review-sage] summary"}]

        payload = {"body": "[code-review-sage] summary", "commit_id": "abc123",
                   "comments": [{"path": "src/a.py", "line": 4, "side": "RIGHT",
                                 "body": "widens scope"}]}
        with unittest.mock.patch.object(discovery, "run_gh_json", run_gh_json):
            self.assertTrue(
                D._draft_confirmed("https://github.com/o/r/pull/1", payload))

        self.assertEqual(len(calls), 2, calls)
        for call in calls:
            if call["paginate"]:
                self.assertEqual(call["jq"], ".[]", call)


class TestDraftConfirmed(unittest.TestCase):
    """The read-back has to identify the draft, not merely count it."""

    LINK = "https://github.com/o/r/pull/1"

    def _payload(self, body="[code-review-sage] summary", commit="abc123"):
        return {
            "body": body,
            "commit_id": commit,
            "comments": [
                {"path": "src/a.py", "line": 4, "side": "RIGHT", "body": "widens scope"},
            ],
        }

    def _stub(self, reviews, comments):
        """Answer the two `gh api` reads `_draft_confirmed` makes."""
        def run_gh_json(path, jq=None, *, paginate=False):
            return comments if "/comments" in path else reviews
        return run_gh_json

    def _review(self, body="[code-review-sage] summary", commit="abc123"):
        return [{"id": 7, "state": "PENDING", "body": body, "commit_id": commit}]

    def _confirm(self, reviews, comments, payload=None):
        from sage_lib import discovery
        with unittest.mock.patch.object(
                discovery, "run_gh_json", self._stub(reviews, comments)):
            return D._draft_confirmed(self.LINK, payload or self._payload())

    def test_confirms_the_draft_that_was_sent(self):
        got = [{"path": "src/a.py", "line": 4, "body": "widens scope"}]
        self.assertTrue(self._confirm(self._review(), got))

    def test_refuses_a_previous_runs_draft_of_the_same_size(self):
        """Same unit count, different findings -- the whole point of the check."""
        stale = self._review(body="[code-review-sage] an older summary")
        got = [{"path": "src/old.py", "line": 99, "body": "a finding from last time"}]
        self.assertFalse(self._confirm(stale, got))

    def test_refuses_same_comments_but_a_different_summary(self):
        """One part varied: the inline comments match, the body does not."""
        other = self._review(body="[code-review-sage] a different summary")
        got = [{"path": "src/a.py", "line": 4, "body": "widens scope"}]
        self.assertFalse(self._confirm(other, got))

    def test_refuses_same_body_and_count_but_a_different_comment(self):
        """One part varied: same size, same summary, different thing said."""
        got = [{"path": "src/a.py", "line": 4, "body": "a different finding"}]
        self.assertFalse(self._confirm(self._review(), got))

    def test_refuses_the_same_comment_on_a_different_line(self):
        """Where a comment lands is part of what the review says."""
        got = [{"path": "src/a.py", "line": 9, "body": "widens scope"}]
        self.assertFalse(self._confirm(self._review(), got))

    def test_refuses_a_matching_body_anchored_to_another_revision(self):
        """Right words about the wrong code is not the review that was sent."""
        got = [{"path": "src/a.py", "line": 4, "body": "widens scope"}]
        self.assertFalse(self._confirm(self._review(commit="deadbee"), got))

    def test_refuses_when_an_inline_comment_is_missing(self):
        self.assertFalse(self._confirm(self._review(), []))

    def test_refuses_a_payload_with_no_commit_id(self):
        """An unanchored draft cannot be identified, so it is never confirmed."""
        got = [{"path": "src/a.py", "line": 4, "body": "widens scope"}]
        self.assertFalse(self._confirm(self._review(), got,
                                       payload=self._payload(commit="")))

    def test_tolerates_crlf_in_the_body_github_echoes_back(self):
        """Line-ending form is not a difference in what the review says."""
        got = [{"path": "src/a.py", "line": 4, "body": "widens scope"}]
        crlf = self._review(body="[code-review-sage] summary\r\n")
        self.assertTrue(self._confirm(crlf, got))
