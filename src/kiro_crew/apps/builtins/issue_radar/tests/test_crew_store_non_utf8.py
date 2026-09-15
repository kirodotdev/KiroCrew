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

    def test_read_skips_degrades_to_empty_on_non_utf8(self):
        path = crew_store.skips_path(OWNER, REPO, self.tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_NON_UTF8_BYTES)

        self.assertEqual(crew_store.read_skips(OWNER, REPO, self.tmp), {})

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

    def test_read_work_item_returns_none_on_non_utf8(self):
        path = crew_store.work_item_path(OWNER, REPO, CREW_ID, 1, self.tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_NON_UTF8_BYTES)

        self.assertIsNone(crew_store.read_work_item(OWNER, REPO, CREW_ID, 1, self.tmp))

    def test_list_work_items_skips_a_non_utf8_record_rather_than_raising(self):
        d = crew_store.crews_dir(OWNER, REPO, self.tmp) / CREW_ID
        d.mkdir(parents=True, exist_ok=True)
        (d / "1.json").write_bytes(_NON_UTF8_BYTES)

        self.assertEqual(crew_store.list_work_items(OWNER, REPO, CREW_ID, self.tmp), [])
