"""Persisted auto_research campaign state beyond core CRUD: updating a missing campaign, the
auto_approve flag, clarification questions and the unattended rule that discards them,
and SQLite connection isolation. ``test_auto_research.py`` heads the suite and lists its parts.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from auto_research_helpers import isolate_fixture  # noqa: F401 - fixture

from kiro_crew.apps.builtins.auto_research.handlers import (
    CampaignStatus,
    create_campaign,
    get_campaign,
    update_campaign_status,
)


class TestUpdateNonexistent:
    def test_update_missing_campaign_returns_error(self):
        assert update_campaign_status("deadbeef", CampaignStatus.RUNNING) == {
            "error": "campaign not found"
        }


# --- auto_approve (unattended) persistence ---


class TestAutoApprovePersist:
    def test_auto_approve_persists(self):
        c = create_campaign(
            {
                "question": "Research question about something here",
                "sources": ["web"],
                "auto_approve": True,
            }
        )
        assert get_campaign(c["id"])["auto_approve"] == 1

    def test_auto_approve_defaults_off(self):
        c = create_campaign(
            {"question": "Research question about something here", "sources": ["web"]}
        )
        assert get_campaign(c["id"])["auto_approve"] == 0


# --- D11 clarification questions + question-mode brief ---


class TestClarificationQuestions:
    def test_pending_question_surfaced(self):
        from kiro_crew.apps.builtins.auto_research.handlers import _campaign_dir

        c = create_campaign(
            {"question": "Research question about something here", "sources": ["web"]}
        )
        (_campaign_dir(c["id"]) / "questions.json").write_text(
            json.dumps({"question": "Which framework should I assume?"})
        )
        assert get_campaign(c["id"])["pending_question"] == "Which framework should I assume?"

    def test_brief_question_mode(self):
        from kiro_crew.apps.builtins.auto_research.handlers import (
            _campaign_dir,
            _get_db,
            _write_brief,
        )

        for auto in (True, False):
            c = create_campaign(
                {
                    "question": "Research question about something here",
                    "sources": ["web"],
                    "auto_approve": auto,
                }
            )
            db = _get_db()
            row = db.execute(
                "SELECT question, sub_questions, sources, max_cycles, idle_secs, "
                "success_criteria, auto_approve FROM campaigns WHERE id = ?",
                (c["id"],),
            ).fetchone()
            db.close()
            _write_brief(c["id"], row)
            brief = (_campaign_dir(c["id"]) / "brief.md").read_text(encoding="utf-8")
            # Attended exposes the questions directive; unattended omits it entirely
            # (no LLM-facing "you may ask" — no-pause is code-enforced instead).
            assert ("Questions allowed" in brief) is (not auto)


class TestUnattendedQuestionEnforcement:
    """Code-enforced guarantee: unattended campaigns never pause for input."""

    def _seed(self, tmp_path: Path):
        from kiro_crew.apps.builtins.auto_research import handlers as h

        d = tmp_path / "a1b2c3d4"
        d.mkdir()
        (d / "questions.json").write_text('{"question": "?"}')
        return h, d

    def test_unattended_discards_question_and_does_not_pause(self, tmp_path: Path):
        h, d = self._seed(tmp_path)
        with patch.object(h, "RESEARCH_DIR", tmp_path):
            assert h._should_pause_for_question("a1b2c3d4", True) is False
            assert not (d / "questions.json").exists()  # discarded

    def test_attended_keeps_question_and_pauses(self, tmp_path: Path):
        h, d = self._seed(tmp_path)
        with patch.object(h, "RESEARCH_DIR", tmp_path):
            assert h._should_pause_for_question("a1b2c3d4", False) is True
            assert (d / "questions.json").exists()  # preserved for the user

    def test_no_question_no_pause(self, tmp_path: Path):
        from kiro_crew.apps.builtins.auto_research import handlers as h

        (tmp_path / "a1b2c3d4").mkdir()
        with patch.object(h, "RESEARCH_DIR", tmp_path):
            assert h._should_pause_for_question("a1b2c3d4", False) is False


class TestSqliteIsolation:
    def test_concurrent_reader_writer_no_deadlock(self, tmp_path: Path):
        """Two connections open *simultaneously*: with isolation_level=None +
        explicit BEGIN/COMMIT each write commits and releases its lock, so a
        second still-open connection can read AND write without hitting a
        leaked write lock ("database is locked"). Under the old default
        isolation the first connection's implicit transaction would leak the
        lock and the second connection's write would block/raise.
        """
        from kiro_crew.apps.builtins.auto_research import handlers as h

        camp = h.create_campaign(
            {
                "question": "Concurrent test question padded enough",
                "sub_questions": [],
                "sources": ["web"],
                "max_cycles": 5,
            }
        )
        cid = camp["id"]

        # Open TWO connections at once and keep BOTH alive for the whole test.
        writer = h._get_db()
        reader = h._get_db()
        # Fail fast instead of waiting out the default 5s busy timeout if a
        # lock leaks, so a regression surfaces as a quick error not a hang.
        writer.execute("PRAGMA busy_timeout = 500")
        reader.execute("PRAGMA busy_timeout = 500")
        try:
            # Writer commits a change; its write lock must be released after.
            writer.execute("BEGIN")
            writer.execute("UPDATE campaigns SET status='running' WHERE id=?", (cid,))
            writer.execute("COMMIT")

            # The still-open reader sees the committed write...
            row = reader.execute("SELECT status FROM campaigns WHERE id=?", (cid,)).fetchone()
            assert row["status"] == "running"

            # ...and can itself write while the writer connection is STILL open.
            # This is the real concurrency assertion: a leaked write lock from
            # the writer would make this raise sqlite3.OperationalError
            # ("database is locked").
            reader.execute("BEGIN")
            reader.execute("UPDATE campaigns SET status='paused' WHERE id=?", (cid,))
            reader.execute("COMMIT")

            row2 = writer.execute("SELECT status FROM campaigns WHERE id=?", (cid,)).fetchone()
            assert row2["status"] == "paused"
        finally:
            reader.close()
            writer.close()

        # Quick check: the handler API path (BEGIN + UPDATE + COMMIT) still works.
        h.update_campaign_status(cid, "running")
        db = h._get_db()
        try:
            row3 = db.execute("SELECT status FROM campaigns WHERE id=?", (cid,)).fetchone()
            assert row3["status"] == "running"
        finally:
            db.close()

    def test_isolation_level_is_none(self, tmp_path: Path):
        """Verify _get_db returns a connection with isolation_level=None."""
        from kiro_crew.apps.builtins.auto_research import handlers as h

        db = h._get_db()
        assert db.isolation_level is None
        db.close()
