#!/usr/bin/env python3
"""Follow-up sessions — keeping a review RESUMABLE rather than resident.

A review's reasoning is only in its session, so the session's transcript is kept
and a follow-up resumes it as an ordinary chat session. These tests pin the parts
that make that safe rather than merely working:

  * a review is only recorded as resumable when its turn ended healthy, and the
    transcript is marked to survive teardown BEFORE the record names it;
  * nothing is offered when the transcript is gone — a session resumed from
    nothing answers confidently with no idea what was reviewed;
  * a session id and a change id both land in filesystem paths, and the reviewer
    can write into this directory itself, so both ends are hostile input;
  * aging out a transcript cannot delete one a follow-up conversation is still
    appending to.
"""
import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from sage_lib import followup as FU  # noqa: N812
from sage_lib import review_pool as RP  # noqa: N812
from sage_lib import store

from kiro_crew.apps.builtins.code_review_sage.tests.fixtures import SYMLINKS_OK


def _ev(kind, **over):
    base = {"kind": kind, "text": "", "title": "", "stop_reason": "end_turn"}
    base.update(over)
    return SimpleNamespace(**base)


class FakeHandle:
    """Stands in for AcpSessionHandle.

    Carries ``session_id`` and accepts ``keep_transcript`` because those two are
    the whole mechanism: the real class reads the flag in ``destroy()`` to decide
    whether to unlink the transcript this feature depends on.
    """

    def __init__(self, session_id="sid-1", scripts=None):
        self.session_id = session_id
        self.keep_transcript = False
        self.scripts = list(scripts or [])
        self.destroyed = 0

    def prompt(self, message, timeout=0):
        events = self.scripts.pop(0) if self.scripts else [_ev("complete")]

        async def _gen():
            for e in events:
                yield e

        return _gen()

    async def destroy(self):
        self.destroyed += 1


class _SessionsDirCase(unittest.TestCase):
    """Points kiro-cli's sessions dir at a temp dir and lays out one run."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "data"
        self.sessions = Path(self._tmp.name) / "sessions"
        self.sessions.mkdir(parents=True)
        store.ensure_layout(self.root)
        store.ensure_run_layout("run1", self.root)
        self._real_sessions_dir = FU.kiro_sessions_dir
        FU.kiro_sessions_dir = lambda: self.sessions

    def tearDown(self):
        FU.kiro_sessions_dir = self._real_sessions_dir
        self._tmp.cleanup()

    def _transcript(self, sid="sid-1") -> Path:
        path = self.sessions / f"{sid}.json"
        path.write_text('{"conversation": []}', encoding="utf-8")
        (self.sessions / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
        return path


class IdentityTests(unittest.TestCase):
    """The slot key and title are derived, not minted."""

    def test_slot_key_is_stable_and_per_review(self):
        """Derived so a second click reopens the same session instead of building
        a rival one, and per (run, change) because a re-review reasons anew."""
        a = FU.slot_key("run1", "GH-o-r-42")
        self.assertEqual(a, FU.slot_key("run1", "GH-o-r-42"))
        self.assertNotEqual(a, FU.slot_key("run2", "GH-o-r-42"))
        self.assertNotEqual(a, FU.slot_key("run1", "GH-o-r-43"))

    def test_slot_key_is_filename_and_header_safe(self):
        """The key becomes part of a session key and a persisted filename."""
        key = FU.slot_key("run/../1", "GH-o-r-42 ../..")
        self.assertRegex(key, r"\Asage-followup-[0-9a-f]{12}\Z")

    def test_title_names_the_pull_request(self):
        self.assertEqual(
            FU.slot_title("GH-o-r-3910", "feat: logging"),
            "followup-pr#3910-feat: logging")

    def test_title_without_a_number_uses_the_change_id(self):
        """A code review is not a pull request and has no number to name."""
        self.assertEqual(FU.slot_title("CR-abc", ""), "followup-CR-abc")

    def test_title_is_bounded(self):
        long = FU.slot_title("GH-o-r-1", "x" * 500)
        self.assertEqual(len(long), FU.TITLE_MAX)


class SessionFileTests(_SessionsDirCase):
    """A session id reaches this module from a file the reviewer can write."""

    def test_a_traversing_id_is_refused_not_sanitized(self):
        """A "cleaned" id would name a DIFFERENT session's transcript, so the id
        is rejected outright."""
        for bad in ("../../etc/passwd", "..", "/abs/path", "", "a" * 200,
                    "-leading-dash", "with space", "sub/dir"):
            self.assertIsNone(FU.session_file(bad), bad)

    def test_a_plain_id_resolves_inside_the_sessions_dir(self):
        path = FU.session_file("sid-1")
        self.assertIsNotNone(path)
        assert path is not None
        self.assertEqual(path.parent, self.sessions.resolve())
        self.assertEqual(path.name, "sid-1.json")


class DescriptorTests(_SessionsDirCase):
    """What a follow-up needs to resume, and what must never be recorded."""

    def test_roundtrip(self):
        self._transcript()
        self.assertTrue(FU.write_descriptor(
            "run1", "gh:o/r/1", sid="sid-1", agent="reviewer",
            cwd="/work", root=self.root))
        got = FU.read_descriptor("run1", "gh:o/r/1", self.root)
        self.assertEqual(got, {"sid": "sid-1", "agent": "reviewer",
                               "cwd": "/work", "provider": "acp",
                               "created_at": got["created_at"]})
        self.assertGreater(got["created_at"], 0)

    def test_an_unusable_session_id_is_never_recorded(self):
        self.assertFalse(FU.write_descriptor(
            "run1", "c1", sid="../../elsewhere", root=self.root))
        self.assertIsNone(FU.read_descriptor("run1", "c1", self.root))

    def test_recording_will_not_resurrect_a_deleted_run(self):
        """A review whose run was deleted mid-flight must not recreate the
        directory that deletion just removed."""
        self._transcript()
        shutil.rmtree(store.run_dir("run1", self.root))
        self.assertFalse(FU.write_descriptor(
            "run1", "c1", sid="sid-1", root=self.root))
        self.assertFalse(store.run_dir("run1", self.root).exists())

    def test_a_planted_descriptor_is_rejected_not_trusted(self):
        """The reviewer has shell and this path is predictable, so the file may be
        its own writing."""
        path = FU.descriptor_path("run1", "c1", self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        for planted in ("not json", '"a string"', "[]", "{}",
                        '{"sid": 5}', '{"sid": "../../etc/passwd"}'):
            path.write_text(planted, encoding="utf-8")
            self.assertIsNone(
                FU.read_descriptor("run1", "c1", self.root), planted)

    def test_wrongly_typed_fields_are_coerced(self):
        self._transcript()
        path = FU.descriptor_path("run1", "c1", self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"sid": "sid-1", "agent": 7, "cwd": null, "provider": "",'
            ' "created_at": "soon"}', encoding="utf-8")
        got = FU.read_descriptor("run1", "c1", self.root)
        assert got is not None
        self.assertEqual(got["agent"], "")
        self.assertEqual(got["cwd"], "")
        # An empty provider would be read as a switch away from kiro and the
        # session id discarded, so it defaults rather than staying blank.
        self.assertEqual(got["provider"], "acp")
        self.assertEqual(got["created_at"], 0.0)

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_a_planted_symlink_is_not_followed_on_read(self):
        victim = Path(self._tmp.name) / "victim.json"
        victim.write_text('{"sid": "sid-1"}', encoding="utf-8")
        self._transcript()
        path = FU.descriptor_path("run1", "c1", self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(victim)
        self.assertIsNone(FU.read_descriptor("run1", "c1", self.root))

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_a_planted_temp_symlink_is_not_written_through(self):
        """A predictable temp name could be pre-linked at the app's own config;
        the write uses an O_EXCL random name instead."""
        victim = Path(self._tmp.name) / "victim.json"
        victim.write_text("untouched", encoding="utf-8")
        self._transcript()
        path = FU.descriptor_path("run1", "c1", self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.with_suffix(".json.tmp").symlink_to(victim)
        self.assertTrue(FU.write_descriptor(
            "run1", "c1", sid="sid-1", root=self.root))
        self.assertEqual(victim.read_text(encoding="utf-8"), "untouched")


class LinkedDirectoryTests(_SessionsDirCase):
    """Guarding the FILE is not enough — the directories above it are plantable."""

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_a_linked_chat_directory_is_refused(self):
        outside = Path(self._tmp.name) / "elsewhere"
        outside.mkdir()
        (store.run_dir("run1", self.root) / "chat").symlink_to(outside)
        self._transcript()
        self.assertFalse(FU.write_descriptor(
            "run1", "c1", sid="sid-1", root=self.root))
        self.assertEqual(list(outside.iterdir()), [])
        self.assertIsNone(FU.read_descriptor("run1", "c1", self.root))

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_a_linked_run_directory_is_refused(self):
        """Comparing ``chat`` to its own parent is vacuous when the RUN dir is the
        link: both resolve into the attacker's directory and containment passes
        while every write lands outside the data tree."""
        outside = Path(self._tmp.name) / "elsewhere2"
        outside.mkdir()
        (store.runs_root(self.root) / "run2").symlink_to(outside)
        self._transcript()
        self.assertFalse(FU.write_descriptor(
            "run2", "c1", sid="sid-1", root=self.root))
        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_a_linked_runs_ROOT_is_refused(self):
        """Resolving the anchor before checking it would make containment
        attacker-relative: the anchor moves into the planted directory and every
        child then compares as legitimately inside it."""
        outside = Path(self._tmp.name) / "elsewhere3"
        outside.mkdir()
        runs = store.runs_root(self.root)
        for child in list(runs.iterdir()):
            shutil.rmtree(child)
        runs.rmdir()
        runs.symlink_to(outside)
        self._transcript()
        self.assertFalse(FU.write_descriptor(
            "run9", "c1", sid="sid-1", root=self.root))
        self.assertEqual(list(outside.iterdir()), [])


class ResumableTests(_SessionsDirCase):
    """Whether a follow-up would restore the review, decided before it is offered."""

    def test_no_record_is_reported_as_such(self):
        desc, reason = FU.resumable("run1", "c1", self.root)
        self.assertIsNone(desc)
        self.assertEqual(reason, FU.ERR_NO_DESCRIPTOR)

    def test_a_missing_transcript_is_refused_rather_than_attempted(self):
        """A session resumed from nothing still answers, with no idea what was
        reviewed — the one outcome worse than offering nothing."""
        self._transcript()
        FU.write_descriptor("run1", "c1", sid="sid-1", root=self.root)
        (self.sessions / "sid-1.json").unlink()
        desc, reason = FU.resumable("run1", "c1", self.root)
        self.assertIsNone(desc)
        self.assertEqual(reason, FU.ERR_TRANSCRIPT_GONE)

    def test_a_recorded_review_with_its_transcript_is_resumable(self):
        self._transcript()
        FU.write_descriptor("run1", "c1", sid="sid-1", agent="rev",
                            root=self.root)
        desc, reason = FU.resumable("run1", "c1", self.root)
        self.assertEqual(reason, "")
        assert desc is not None
        self.assertEqual(desc["sid"], "sid-1")
        self.assertEqual(desc["agent"], "rev")


class ForgetTests(_SessionsDirCase):
    def test_forget_stops_the_offer_without_deleting_the_session(self):
        """A session id read back from the descriptor proves its FORM, never which
        session Sage recorded -- the reviewer can write that file itself. Deleting
        on that authority would let a planted id name any session on the machine.
        """
        self._transcript()
        FU.write_descriptor("run1", "c1", sid="sid-1", root=self.root)
        FU.forget("run1", "c1", self.root)
        self.assertIsNone(FU.read_descriptor("run1", "c1", self.root))
        self.assertTrue((self.sessions / "sid-1.json").exists())
        self.assertTrue((self.sessions / "sid-1.jsonl").exists())

    def test_a_planted_descriptor_cannot_delete_another_session(self):
        victim = self._transcript("someone-elses-session")
        path = FU.descriptor_path("run1", "c1", self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"sid": "someone-elses-session"}', encoding="utf-8")
        FU.forget("run1", "c1", self.root)
        self.assertTrue(victim.exists())

    def test_forget_is_idempotent(self):
        FU.forget("run1", "never-recorded", self.root)  # must not raise


class PruneTests(_SessionsDirCase):
    """Follow-up offers age out; the transcripts behind them are not ours to delete."""

    def _age(self, sid: str, secs: float) -> None:
        old = os.stat(self.sessions / f"{sid}.json").st_mtime - secs
        for suffix in (".json", ".jsonl"):
            os.utime(self.sessions / f"{sid}{suffix}", (old, old))

    def _record(self, change: str, sid: str, created_at: float) -> None:
        self._transcript(sid)
        FU.write_descriptor("run1", change, sid=sid, root=self.root)
        path = FU.descriptor_path("run1", change, self.root)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["created_at"] = created_at
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_an_untouched_offer_is_retired(self):
        self._record("c1", "sid-old", time.time() - FU.OFFER_MAX_IDLE_SECS * 2)
        self._age("sid-old", FU.OFFER_MAX_IDLE_SECS * 2)
        self.assertEqual(FU.prune(root=self.root), 1)
        self.assertIsNone(FU.read_descriptor("run1", "c1", self.root))

    def test_pruning_never_deletes_the_transcript(self):
        """The sweep can retire an offer; it cannot hollow out a session. An open
        follow-up conversation lives on that file.
        """
        self._record("c1", "sid-old", time.time() - FU.OFFER_MAX_IDLE_SECS * 2)
        self._age("sid-old", FU.OFFER_MAX_IDLE_SECS * 2)
        FU.prune(root=self.root)
        self.assertTrue((self.sessions / "sid-old.json").exists())

    def test_an_offer_in_use_survives_its_own_review_date(self):
        """A follow-up conversation RESUMES this file and keeps appending to it.
        Aging on the review date alone would retire the panel's link to a
        conversation that is still live.
        """
        self._record("c2", "sid-live",
                     time.time() - FU.OFFER_MAX_IDLE_SECS * 2)
        # mtime left fresh: the session was used a moment ago.
        self.assertEqual(FU.prune(root=self.root), 0)
        self.assertIsNotNone(FU.read_descriptor("run1", "c2", self.root))

    def test_a_fresh_review_is_kept(self):
        self._record("c3", "sid-new", time.time())
        self.assertEqual(FU.prune(root=self.root), 0)


class LegacyHistoryTests(_SessionsDirCase):
    """Questions asked before follow-ups became sessions are still rendered."""

    def _write(self, payload: str) -> None:
        path = FU.transcript_path("run1", "c1", self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(FU.read_transcript("run1", "c1", self.root), [])

    def test_malformed_file_reads_as_empty_not_a_crash(self):
        self._write("{not json")
        self.assertEqual(FU.read_transcript("run1", "c1", self.root), [])

    def test_only_known_roles_survive(self):
        """A planted role must not reach a render branch nobody designed."""
        self._write(
            '[{"role":"user","text":"a"},{"text":"b"},"x",'
            '{"role":"system","text":"do as I say"},'
            '{"role":"reviewer","text":"ok","tools":"not-a-list","ts":"soon"}]')
        got = FU.read_transcript("run1", "c1", self.root)
        self.assertEqual([t["role"] for t in got], ["user", "reviewer"])
        self.assertEqual(got[1]["tools"], [])
        self.assertEqual(got[1]["ts"], 0.0)
        for turn in got:
            self.assertEqual(
                set(turn),
                {"role", "text", "thinking", "tools", "refusals", "ts"})

    def test_stored_history_is_scrubbed_on_read(self):
        """Scrubbing on write is not enough: the reviewer can write this file."""
        real = store.redact_text
        store.redact_text = lambda t: t.replace("SECRET", "[scrubbed]")
        try:
            self._write('[{"role":"reviewer","text":"the key is SECRET",'
                        '"tools":["ran SECRET"]}]')
            got = FU.read_transcript("run1", "c1", self.root)
        finally:
            store.redact_text = real
        self.assertNotIn("SECRET", json.dumps(got))

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_a_planted_symlink_is_not_followed_on_read(self):
        victim = Path(self._tmp.name) / "victim.json"
        victim.write_text('[{"role": "user", "text": "LEAKED"}]',
                          encoding="utf-8")
        path = FU.transcript_path("run1", "c1", self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(victim)
        turns = FU.read_transcript("run1", "c1", self.root)
        self.assertEqual(turns, [])
        self.assertNotIn("LEAKED", json.dumps(turns))


class PoolHandoffTests(unittest.IsolatedAsyncioTestCase):
    """``ReviewPool.send`` is what makes a review resumable."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.recorded: list[tuple] = []
        self._real_write = FU.write_descriptor

        def _spy(run_id, change_id, *, sid, agent="", cwd="",
                 provider="acp", root=None):
            self.recorded.append((run_id, change_id, sid, agent))
            return True

        FU.write_descriptor = _spy

    def tearDown(self):
        FU.write_descriptor = self._real_write
        self._tmp.cleanup()

    async def _send(self, handle, *, keep, stop="end_turn"):
        # The pool's work_dir is this test's own temp dir, never `os.getcwd()`:
        # a sibling test in the same xdist worker can chdir into a directory it
        # then deletes, and reading the process cwd here would raise
        # FileNotFoundError depending on how the run happened to shard.
        pool = RP.ReviewPool(max_workers=1, agent="rev", work_dir=self._tmp.name)
        self.pool = pool

        class FakeRuntime:
            async def create_session(self, cwd=None, agent=None):
                return handle

        pool._holder.acquire = lambda: _done(FakeRuntime())  # type: ignore
        handle.scripts = [[_ev("text_chunk", text="report"),
                           _ev("complete", stop_reason=stop)]]
        return await pool.send("task", timeout=5, keep_session_key=keep)

    async def test_a_kept_review_is_marked_and_recorded_then_destroyed(self):
        """The session is torn down like any other — only its transcript stays."""
        h = FakeHandle()
        out = await self._send(h, keep="run1:c1")
        self.assertEqual(out, "report")
        self.assertTrue(h.keep_transcript)
        self.assertEqual(h.destroyed, 1)
        # The agent recorded is the one the review actually RAN as (the pool
        # degrades to the fallback when the reviewer spec is absent), because a
        # follow-up session has to be created with that same agent.
        self.assertEqual(self.recorded,
                         [("run1", "c1", "sid-1", self.pool._agent)])

    async def test_without_a_key_nothing_is_kept(self):
        h = FakeHandle()
        await self._send(h, keep=None)
        self.assertFalse(h.keep_transcript)
        self.assertEqual(h.destroyed, 1)
        self.assertEqual(self.recorded, [])

    async def test_an_abnormal_turn_is_never_kept(self):
        """A session whose turn died has no findings to be asked about, and
        recording it would leave a file nothing will ever load."""
        h = FakeHandle()
        with self.assertRaises(RuntimeError):
            await self._send(h, keep="run1:c1", stop=RP.STOP_REASON_TOOL_STALL)
        self.assertFalse(h.keep_transcript)
        self.assertEqual(h.destroyed, 1)
        self.assertEqual(self.recorded, [])

    async def test_a_malformed_key_is_inert(self):
        h = FakeHandle()
        await self._send(h, keep="no-change-id")
        self.assertEqual(self.recorded, [])

    async def test_a_failed_record_does_not_fail_the_review(self):
        def _boom(*a, **kw):
            raise OSError("disk full")

        FU.write_descriptor = _boom
        h = FakeHandle()
        out = await self._send(h, keep="run1:c1")
        self.assertEqual(out, "report")
        self.assertEqual(h.destroyed, 1)


def _done(value):
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    fut.set_result(value)
    return fut


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
