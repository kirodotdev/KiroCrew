"""Reconcile-prompt storage: round-trip, reset, is_default, and seed.

The store writes into ``app_data_dir("chat-status-tags")``, which is rooted at
``KIROCREW_HOME``. Each test repoints ``KIROCREW_HOME`` at a temp dir so the real
data home is never touched and the file starts absent.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from kiro_crew.apps.builtins.chat_status_tags import settings
from kiro_crew.apps.builtins.chat_status_tags.prompts import DEFAULT_RECONCILE_PROMPT


class TestPromptStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._prev = os.environ.get("KIROCREW_HOME")
        # config_dir() memoizes on the RAW KIROCREW_HOME value, so repointing the
        # env var per test is honoured without a module reload (same isolation the
        # ops-mission-control route tests use).
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_when_absent(self) -> None:
        self.assertFalse(settings.prompt_path().exists())
        self.assertEqual(settings.get_prompt(), DEFAULT_RECONCILE_PROMPT)
        self.assertTrue(settings.is_default())

    def test_round_trip_custom_prompt(self) -> None:
        custom = "Only promote to done when every owned PR is merged AND CI is green."
        settings.set_prompt(custom)
        self.assertTrue(settings.prompt_path().is_file())
        self.assertEqual(settings.get_prompt(), custom)
        self.assertFalse(settings.is_default())

    def test_custom_written_verbatim(self) -> None:
        # Newlines and trailing whitespace-bearing lines must survive byte-for-byte.
        custom = "line one\nline two\n\tindented\n"
        settings.set_prompt(custom)
        self.assertEqual(settings.prompt_path().read_text(encoding="utf-8"), custom)

    def test_reset_via_empty_string(self) -> None:
        settings.set_prompt("something custom")
        self.assertTrue(settings.prompt_path().is_file())
        settings.set_prompt("")  # reset
        self.assertFalse(settings.prompt_path().exists())
        self.assertEqual(settings.get_prompt(), DEFAULT_RECONCILE_PROMPT)
        self.assertTrue(settings.is_default())

    def test_reset_via_whitespace(self) -> None:
        settings.set_prompt("custom")
        settings.set_prompt("   \n\t ")  # whitespace-only counts as reset
        self.assertFalse(settings.prompt_path().exists())
        self.assertTrue(settings.is_default())

    def test_blank_file_reads_as_default(self) -> None:
        # A file somehow left empty must not hand the cron an empty instruction.
        settings.prompt_path().write_text("   \n", encoding="utf-8")
        self.assertEqual(settings.get_prompt(), DEFAULT_RECONCILE_PROMPT)
        self.assertTrue(settings.is_default())

    def test_length_is_capped_on_write(self) -> None:
        settings.set_prompt("x" * (settings.MAX_PROMPT_LEN + 500))
        self.assertEqual(len(settings.get_prompt()), settings.MAX_PROMPT_LEN)

    def test_seed_default_writes_when_absent(self) -> None:
        self.assertFalse(settings.prompt_path().exists())
        settings.seed_default()
        self.assertTrue(settings.prompt_path().is_file())
        self.assertEqual(
            settings.prompt_path().read_text(encoding="utf-8"), DEFAULT_RECONCILE_PROMPT
        )
        self.assertTrue(settings.is_default())

    def test_seed_default_does_not_clobber_custom(self) -> None:
        custom = "operator's own instructions"
        settings.set_prompt(custom)
        settings.seed_default()  # must be a no-op — file already exists
        self.assertEqual(settings.get_prompt(), custom)
        self.assertFalse(settings.is_default())

    def test_seed_default_is_idempotent(self) -> None:
        settings.seed_default()
        first = settings.prompt_path().read_text(encoding="utf-8")
        settings.seed_default()
        self.assertEqual(settings.prompt_path().read_text(encoding="utf-8"), first)


class TestFlagStore(unittest.TestCase):
    """The behaviour-toggles JSON store: defaults, round-trip, corrupt, validation."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_defaults_when_absent(self) -> None:
        self.assertFalse(settings.flags_path().exists())
        self.assertEqual(
            settings.get_flags(),
            {"reconciler_enabled": True, "auto_resume_enabled": True},
        )

    def test_both_default_enabled(self) -> None:
        # Both paid behaviours ship ON.
        flags = settings.get_flags()
        self.assertTrue(flags["reconciler_enabled"])
        self.assertTrue(flags["auto_resume_enabled"])

    def test_set_one_flag_round_trip(self) -> None:
        result = settings.set_flags(auto_resume_enabled=False)
        self.assertTrue(settings.flags_path().is_file())
        self.assertEqual(result, {"reconciler_enabled": True, "auto_resume_enabled": False})
        # Re-read from disk agrees.
        self.assertEqual(
            settings.get_flags(),
            {"reconciler_enabled": True, "auto_resume_enabled": False},
        )

    def test_partial_update_preserves_other_flag(self) -> None:
        settings.set_flags(reconciler_enabled=False)
        settings.set_flags(auto_resume_enabled=False)
        self.assertEqual(
            settings.get_flags(),
            {"reconciler_enabled": False, "auto_resume_enabled": False},
        )

    def test_set_both_flags_at_once(self) -> None:
        result = settings.set_flags(reconciler_enabled=False, auto_resume_enabled=False)
        self.assertEqual(result, {"reconciler_enabled": False, "auto_resume_enabled": False})

    def test_empty_set_is_noop(self) -> None:
        self.assertEqual(
            settings.set_flags(),
            {"reconciler_enabled": True, "auto_resume_enabled": True},
        )

    def test_reject_non_bool(self) -> None:
        bad_values: list[object] = ["true", 1, 0, None, [], {}]
        for bad in bad_values:
            with self.assertRaises(ValueError):
                settings.set_flags(reconciler_enabled=bad)  # type: ignore[arg-type]
        # A rejected write leaves the store untouched (still default/absent).
        self.assertTrue(settings.get_flags()["reconciler_enabled"])

    def test_reject_unknown_key(self) -> None:
        with self.assertRaises(ValueError):
            settings.set_flags(bogus_flag=True)  # type: ignore[call-arg]

    def test_missing_file_reads_defaults(self) -> None:
        self.assertFalse(settings.flags_path().exists())
        self.assertEqual(settings.get_flags()["auto_resume_enabled"], True)

    def test_corrupt_file_reads_defaults(self) -> None:
        settings.flags_path().parent.mkdir(parents=True, exist_ok=True)
        settings.flags_path().write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(
            settings.get_flags(),
            {"reconciler_enabled": True, "auto_resume_enabled": True},
        )

    def test_non_object_json_reads_defaults(self) -> None:
        settings.flags_path().parent.mkdir(parents=True, exist_ok=True)
        settings.flags_path().write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(
            settings.get_flags(),
            {"reconciler_enabled": True, "auto_resume_enabled": True},
        )

    def test_partial_or_foreign_values_fall_back_per_key(self) -> None:
        # One valid bool, one bad value, one unknown key — the good key is
        # adopted, the bad one falls to its default, the unknown is ignored.
        import json

        settings.flags_path().parent.mkdir(parents=True, exist_ok=True)
        settings.flags_path().write_text(
            json.dumps({"auto_resume_enabled": False, "reconciler_enabled": "nope", "junk": 1}),
            encoding="utf-8",
        )
        self.assertEqual(
            settings.get_flags(),
            {"reconciler_enabled": True, "auto_resume_enabled": False},
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
