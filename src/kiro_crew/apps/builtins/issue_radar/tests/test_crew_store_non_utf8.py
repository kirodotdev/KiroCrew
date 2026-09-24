"""crew_store's changed readers must degrade to their default on a non-UTF-8 file.

Every read site in this file decodes with an explicit ``path.read_text(encoding=
"utf-8")`` rather than the platform-default codec, so a Windows host does not
silently mis-decode a crew record written with the platform's own default
codec. That explicit codec, without more, makes ``UnicodeDecodeError`` a way to
fail on a file that was written by any process using a different encoding -- a
hand-edited file, or one restored from a backup written by another version,
both explicitly called out in this module's own docstrings as things these
readers must survive.

Every changed reader already treats ``(OSError, json.JSONDecodeError)`` as "this
file is not usable, fall back to the default" rather than a crash; a real
``UnicodeDecodeError`` (a ``ValueError`` subclass, so NOT already covered by
``OSError``) must degrade the same way, not escape as an unhandled exception.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from kiro_crew.apps.builtins.issue_radar.backend import crew_store
from kiro_crew.crew_log import projection as crew_log_projection

OWNER = "o"
REPO = "r"
CREW_ID = "c_deadbeef"

# Latin-1 bytes that are not valid UTF-8 on their own (0x80 is a continuation byte
# with no leading byte before it), so ``.decode("utf-8")`` raises deterministically.
_NON_UTF8_BYTES = b'{"claim_ttl_hours": 5, "note": "caf\x80"}'


class TestCrewStoreReadersSurviveNonUtf8Files(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_read_settings_degrades_to_default_on_non_utf8(self):
        path = crew_store.settings_path(OWNER, REPO, self.tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_NON_UTF8_BYTES)

        out = crew_store.read_settings(OWNER, REPO, self.tmp)

        self.assertEqual(out, dict(crew_store.DEFAULT_SETTINGS))

    def test_read_crew_returns_none_on_non_utf8(self):
        path = crew_store.crew_path(OWNER, REPO, CREW_ID, self.tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_NON_UTF8_BYTES)

        self.assertIsNone(crew_store.read_crew(OWNER, REPO, CREW_ID, self.tmp))

    def test_list_crews_skips_a_non_utf8_record_rather_than_raising(self):
        d = crew_store.crews_dir(OWNER, REPO, self.tmp)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{CREW_ID}.json").write_bytes(_NON_UTF8_BYTES)

        self.assertEqual(crew_store.list_crews(OWNER, REPO, self.tmp), [])


class TestTheCarrySurvivesNonUtf8PreProjectionFiles(unittest.TestCase):
    """The ledger's own files -- work items and the skip index -- are read only once
    more, by the carry into the crew log on a crew's first write; a non-UTF-8 one is
    never silently dropped there. The write that would have carried it is refused,
    retryably, with the file named: dropping it would discard that row's stored state
    for good once the carry was marked finished, and folding without it could let a
    second item into an editing phase the omitted row still holds. Nothing crashes
    and nothing is lost -- the files stay where they are until they can be read."""

    def setUp(self):
        import os
        from unittest import mock

        from kiro_crew.crew_log import emit as crew_log_emit

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"KIROCREW_HOME": home, crew_log_emit.CREW_LOG_ENV: "1"})
        env.start()
        self.addCleanup(env.stop)
        crew_log_emit.reset_caches()
        crew_log_projection.forget_slot_folds()
        self.addCleanup(crew_log_projection.forget_slot_folds)
        self.addCleanup(crew_log_emit.reset_caches)
        self.addCleanup(crew_log_emit.drain_for_shutdown, 2.0)

    def _crew_with_a_log(self) -> tuple[str, str]:
        from kiro_crew.crew_log.schema import KIND_SESSION
        from kiro_crew.crew_log.store import CrewLog

        crew = crew_store.create_crew(OWNER, REPO, {"name": "Andromeda"}, self.tmp)
        sid = "acp-1"
        CrewLog.create(
            KIND_SESSION,
            sid,
            owner="owner",
            agent="kirocrew",
            slot=crew_store.slot_key_for(crew["id"]),
        )
        return crew["id"], sid

    def test_a_non_utf8_work_item_file_refuses_the_write(self):
        cid, sid = self._crew_with_a_log()
        path = crew_store.work_item_path(OWNER, REPO, cid, 1, self.tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_NON_UTF8_BYTES)

        # A file the reader cannot decode is a row that could not be carried, so the
        # write is refused rather than folding a record that is missing it.
        with self.assertRaises(crew_store.CrewLedgerNotRecorded) as caught:
            crew_store.commit_work_progress(
                OWNER,
                REPO,
                cid,
                7,
                {"phase": "claimed"},
                "claim",
                "took #7",
                root=self.tmp,
                session_id=sid,
            )
        self.assertIn("1.json", str(caught.exception))
        self.assertIsNone(crew_store.read_work_item(OWNER, REPO, cid, 1, self.tmp))
        self.assertEqual(crew_store.list_work_items(OWNER, REPO, cid, self.tmp), [])

        # Moved aside, the same update goes through.
        path.unlink()
        out = crew_store.commit_work_progress(
            OWNER,
            REPO,
            cid,
            7,
            {"phase": "claimed"},
            "claim",
            "took #7",
            root=self.tmp,
            session_id=sid,
        )
        self.assertEqual(out["item"]["phase"], "claimed")
        self.assertEqual(
            [i["number"] for i in crew_store.list_work_items(OWNER, REPO, cid, self.tmp)], [7]
        )

    def test_a_non_utf8_skip_index_refuses_the_write(self):
        cid, sid = self._crew_with_a_log()
        path = crew_store.skips_path(OWNER, REPO, self.tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_NON_UTF8_BYTES)

        with self.assertRaises(crew_store.CrewLedgerNotRecorded):
            crew_store.commit_work_progress(
                OWNER,
                REPO,
                cid,
                7,
                {"phase": "claimed"},
                "claim",
                "took #7",
                root=self.tmp,
                session_id=sid,
            )

        self.assertEqual(crew_store.read_skips(OWNER, REPO, self.tmp), {})
