"""Backend-route tests: the follow-up routes.

Opening a follow-up never hands back a session that resumed nothing. The state route says
whether a run is resumable and why, the start route seeds the resume and refuses a deleted,
live or unseedable run, and a returning user sees re-entry once a session exists.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from sage_lib import store

from kiro_crew.apps.builtins.code_review_sage.tests.backend_routes_helpers import (
    _load_routes_module,
)
from kiro_crew.apps.builtins.code_review_sage.tests.fixtures import OwnerRequest


class _FakeSessions:
    """The two SessionManager methods the follow-up route uses."""

    def __init__(self, mapped=None):
        self.map: dict[str, tuple] = dict(mapped or {})
        self.seeds: list[tuple] = []
        # When set, a seeded entry is dropped on read-back — what the real
        # session map does to an entry whose session files are gone.
        self.drop_seeded = False

    def resumable_sid(self, key):
        entry = self.map.get(key)
        return entry[0] if entry else None

    def seed_conversation(self, key, sid, *, provider="", cwd=""):
        self.seeds.append((key, sid, provider, cwd))
        if not self.drop_seeded:
            self.map[key] = (sid, provider, cwd)


class _FakeState:
    def __init__(self, sessions=None, folders=None, slots=None):
        self.sessions = sessions
        self._folders = list(folders or [])
        self._slots = dict(slots or {})

    async def mutate_folders(self, mutate):
        _changed, value = mutate(self._folders)
        return value


class _Req(OwnerRequest):
    def __init__(self, body=None, query=None):
        super().__init__()
        self._body = body or {}
        self.query = query or {}

    async def json(self):
        return self._body


class TestFollowupRoutes(unittest.IsolatedAsyncioTestCase):
    """Opening a follow-up must never hand back a session that resumed nothing.

    The dashboard's fallback for a failed resume is to replay Kiro Crew's own
    conversation log, and a follow-up slot has none — so a slot created without a
    live mapping answers confidently with no idea what was reviewed. Every branch
    here is about refusing that outcome rather than degrading into it.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        self.mod = _load_routes_module()
        self.mod._RUNS = [{"run_id": "run1", "status": "done"}]
        self.mod.is_app_enabled = lambda name: True
        self.sessions_dir = Path(self.tmp) / "kiro-sessions"
        self.sessions_dir.mkdir(parents=True)
        self.mod.followup.kiro_sessions_dir = lambda: self.sessions_dir
        store.ensure_layout()
        store.ensure_run_layout("run1")

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _record(self, sid="sid-1", change="GH-o-r-42"):
        (self.sessions_dir / f"{sid}.json").write_text("{}", encoding="utf-8")
        self.assertTrue(
            self.mod.followup.write_descriptor(
                "run1", change, sid=sid, agent="sage-reviewer", cwd="/work"
            )
        )

    async def test_state_reports_not_resumable_with_a_reason(self):
        resp = await self.mod._handle_chat_get(
            _Req(query={"run_id": "run1", "change_id": "GH-o-r-42"})
        )
        data = json.loads(resp.body)
        self.assertFalse(data["resumable"])
        self.assertEqual(data["reason"], self.mod.followup.ERR_NO_DESCRIPTOR)
        self.assertTrue(data["slot_key"])

    async def test_state_reports_resumable_once_recorded(self):
        self._record()
        resp = await self.mod._handle_chat_get(
            _Req(query={"run_id": "run1", "change_id": "GH-o-r-42"})
        )
        data = json.loads(resp.body)
        self.assertTrue(data["resumable"])
        self.assertEqual(data["reason"], "")

    async def test_state_requires_both_ids(self):
        resp = await self.mod._handle_chat_get(_Req(query={"run_id": "run1"}))
        self.assertEqual(resp.status, 400)

    async def test_start_refuses_a_deleted_run(self):
        self._record()
        self.mod._RUNS = []
        resp = await self.mod._handle_followup_start(
            _Req({"run_id": "run1", "change_id": "GH-o-r-42"})
        )
        self.assertEqual(resp.status, 409)
        self.assertEqual(json.loads(resp.body)["code"], self.mod.followup.ERR_RUN_GONE)

    async def test_start_refuses_when_the_transcript_is_gone(self):
        self._record()
        (self.sessions_dir / "sid-1.json").unlink()
        self.mod._APP_STATE["state"] = _FakeState(_FakeSessions())
        resp = await self.mod._handle_followup_start(
            _Req({"run_id": "run1", "change_id": "GH-o-r-42"})
        )
        self.assertEqual(resp.status, 409)
        self.assertEqual(json.loads(resp.body)["code"], self.mod.followup.ERR_TRANSCRIPT_GONE)

    async def test_start_seeds_the_resume_and_returns_the_slot(self):
        self._record()
        sessions = _FakeSessions()
        self.mod._APP_STATE["state"] = _FakeState(sessions)
        resp = await self.mod._handle_followup_start(
            _Req({"run_id": "run1", "change_id": "GH-o-r-42"})
        )
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body)
        expected_key = self.mod.followup.slot_key("run1", "GH-o-r-42")
        self.assertEqual(data["slot_key"], expected_key)
        self.assertEqual(data["agent"], "sage-reviewer")
        self.assertTrue(data["folder_id"])
        self.assertTrue(data["title"].startswith("followup-pr#42"))
        # Seeded under the session key a dashboard slot resolves its resume
        # from, carrying the provider and cwd the review actually ran with: a
        # provider mismatch makes the dashboard DISCARD the session id, and the
        # cwd is what the reviewer's relative paths were written against.
        self.assertEqual(sessions.seeds, [(f"dashboard:{expected_key}", "sid-1", "acp", "/work")])

    async def test_start_does_not_reseed_an_existing_conversation(self):
        """Re-seeding would point a follow-up conversation back at the review's
        own starting transcript and discard everything discussed since."""
        self._record()
        key = self.mod.followup.slot_key("run1", "GH-o-r-42")
        sessions = _FakeSessions({f"dashboard:{key}": ("later-sid", "acp", "/w")})
        self.mod._APP_STATE["state"] = _FakeState(sessions)
        resp = await self.mod._handle_followup_start(
            _Req({"run_id": "run1", "change_id": "GH-o-r-42"})
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(sessions.seeds, [])

    async def test_start_refuses_when_the_seed_does_not_stick(self):
        """The session map self-prunes an entry whose files are gone, so the
        read-back is the last check that there is anything to resume."""
        self._record()
        sessions = _FakeSessions()
        sessions.drop_seeded = True
        self.mod._APP_STATE["state"] = _FakeState(sessions)
        resp = await self.mod._handle_followup_start(
            _Req({"run_id": "run1", "change_id": "GH-o-r-42"})
        )
        self.assertEqual(resp.status, 409)
        self.assertEqual(json.loads(resp.body)["code"], self.mod.followup.ERR_TRANSCRIPT_GONE)

    async def test_start_reports_unavailable_sessions_rather_than_pretending(self):
        self._record()
        self.mod._APP_STATE["state"] = _FakeState(None)
        resp = await self.mod._handle_followup_start(
            _Req({"run_id": "run1", "change_id": "GH-o-r-42"})
        )
        self.assertEqual(resp.status, 503)

    async def test_the_folder_is_adopted_not_duplicated(self):
        self._record()
        self._record(sid="sid-2", change="GH-o-r-43")
        state = _FakeState(_FakeSessions())
        self.mod._APP_STATE["state"] = state
        first = json.loads(
            (
                await self.mod._handle_followup_start(
                    _Req({"run_id": "run1", "change_id": "GH-o-r-42"})
                )
            ).body
        )
        second = json.loads(
            (
                await self.mod._handle_followup_start(
                    _Req({"run_id": "run1", "change_id": "GH-o-r-43"})
                )
            ).body
        )
        self.assertEqual(first["folder_id"], second["folder_id"])
        self.assertEqual([f["name"] for f in state._folders], [self.mod.followup.FOLDER_NAME])

    async def test_a_disabled_app_answers_nothing(self):
        """Disabling an app withdraws its runtime, not just its UI — a request
        already in flight is the one no teardown hook can reach."""
        self._record()
        self.mod.is_app_enabled = lambda name: False
        self.mod._APP_STATE["state"] = _FakeState(_FakeSessions())
        for resp in (
            await self.mod._handle_chat_get(
                _Req(query={"run_id": "run1", "change_id": "GH-o-r-42"})
            ),
            await self.mod._handle_followup_start(
                _Req({"run_id": "run1", "change_id": "GH-o-r-42"})
            ),
        ):
            self.assertEqual(resp.status, 403)
            self.assertEqual(json.loads(resp.body)["code"], "app_disabled")


class TestFollowupRunLiveAndReentry(unittest.IsolatedAsyncioTestCase):
    """A live run must not be offerable, and a returning user must see re-entry.

    The mid-run window is the reachable one: a first pass writes a descriptor, the
    panel offers it, and a successful second coverage pass then retires that
    descriptor -- leaving an already-open conversation pointing at findings the run
    replaced, with nothing saying so.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        self.mod = _load_routes_module()
        self.mod.is_app_enabled = lambda name: True
        self.sessions_dir = Path(self.tmp) / "kiro-sessions"
        self.sessions_dir.mkdir(parents=True)
        self.mod.followup.kiro_sessions_dir = lambda: self.sessions_dir
        store.ensure_layout()
        store.ensure_run_layout("run1")
        (self.sessions_dir / "sid-1.json").write_text("{}", encoding="utf-8")
        self.mod.followup.write_descriptor(
            "run1", "GH-o-r-42", sid="sid-1", agent="sage-reviewer", cwd="/work"
        )
        self.sessions = _FakeSessions()
        self.state = _FakeState(self.sessions)
        self.mod._APP_STATE["state"] = self.state

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, **over):
        run = {"run_id": "run1", "status": "done"}
        run.update(over)
        self.mod._RUNS = [run]

    async def test_a_running_run_is_not_offerable(self):
        self._run(status="running")
        data = json.loads(
            (
                await self.mod._handle_chat_get(
                    _Req(query={"run_id": "run1", "change_id": "GH-o-r-42"})
                )
            ).body
        )
        self.assertFalse(data["resumable"])
        self.assertEqual(data["reason"], self.mod.followup.ERR_RUN_LIVE)

    async def test_a_posting_run_is_not_offerable_either(self):
        """Posting happens AFTER a terminal status, so the status check alone
        would let this through."""
        self._run(status="done", posting=True)
        data = json.loads(
            (
                await self.mod._handle_chat_get(
                    _Req(query={"run_id": "run1", "change_id": "GH-o-r-42"})
                )
            ).body
        )
        self.assertFalse(data["resumable"])
        self.assertEqual(data["reason"], self.mod.followup.ERR_RUN_LIVE)

    async def test_start_refuses_while_the_run_is_live(self):
        self._run(status="running")
        resp = await self.mod._handle_followup_start(
            _Req({"run_id": "run1", "change_id": "GH-o-r-42"})
        )
        self.assertEqual(resp.status, 409)
        self.assertEqual(json.loads(resp.body)["code"], self.mod.followup.ERR_RUN_LIVE)
        self.assertEqual(self.sessions.seeds, [])

    async def test_a_finished_run_is_offerable(self):
        self._run(status="done")
        data = json.loads(
            (
                await self.mod._handle_chat_get(
                    _Req(query={"run_id": "run1", "change_id": "GH-o-r-42"})
                )
            ).body
        )
        self.assertTrue(data["resumable"])
        self.assertEqual(data["reason"], "")

    async def _open_flag(self):
        data = json.loads(
            (
                await self.mod._handle_chat_get(
                    _Req(query={"run_id": "run1", "change_id": "GH-o-r-42"})
                )
            ).body
        )
        return data["followup_open"]

    async def test_reentry_is_reported_once_a_session_exists(self):
        """Without this the panel invites a returning user to "open" a
        conversation they already had, with no trace of it -- which reads as the
        review having lost it."""
        self._run(status="done")
        key = self.mod.followup.slot_key("run1", "GH-o-r-42")
        self.assertFalse(await self._open_flag())
        self.state._slots[key] = object()
        self.assertTrue(await self._open_flag())

    async def test_a_surviving_mapping_is_not_an_existing_session(self):
        """Closing a tab pops the slot but KEEPS its mapping, so a resume can
        reload it later. Reading existence off the map therefore reported a closed
        session as open, and the recreate that followed omitted the title and
        folder -- leaving the session unfiled under the placeholder title.
        """
        self._run(status="done")
        key = self.mod.followup.slot_key("run1", "GH-o-r-42")
        self.sessions.map[f"dashboard:{key}"] = ("sid-1", "acp", "/work")
        self.assertFalse(await self._open_flag())

    async def test_reentry_is_false_when_state_is_unavailable(self):
        self._run(status="done")
        self.mod._APP_STATE["state"] = _FakeState(None)
        self.assertFalse(await self._open_flag())
        data = json.loads(
            (
                await self.mod._handle_chat_get(
                    _Req(query={"run_id": "run1", "change_id": "GH-o-r-42"})
                )
            ).body
        )
        self.assertTrue(data["resumable"])
