"""The decision log: the row's shape, its bounds, and the append's permissions.

Two groups carry the weight. :class:`TestRowShape` pins what the row does NOT
contain — the state, the session key, a provider message — because the row is
written to disk on a path whose whole point is that conversation text does not
land there. :class:`TestAppend`'s permission assertions are the other half: the
row names no credential, but it does reveal when the seam fires and with what
verdicts, so the directory is 0700 and a fresh file 0600 REGARDLESS of umask.
"""

from __future__ import annotations

import json
import os
import stat
import time
from datetime import datetime, timedelta, timezone

import pytest

from kiro_crew import platform_log_append
from kiro_crew.decisions import log as log_mod
from kiro_crew.decisions.log import append, build_row, log_path, session_digest
from kiro_crew.decisions.types import Answer


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Redirect the log directory into *tmp_path* and hand back its path."""
    directory = tmp_path / "decisions"
    monkeypatch.setattr(log_mod, "log_dir", lambda: directory)
    return directory


def _row(**kw):
    base = dict(point="skills.select", session_key="sess-1", latency_ms=212)
    base.update(kw)
    return build_row(**base)


class TestRowShape:
    def test_the_row_is_exactly_these_seven_fields(self):
        """A field added here is a field every later reader must tolerate."""
        assert set(_row()) == {
            "ts",
            "point",
            "session",
            "latency_ms",
            "scrubbed",
            "answers",
            "error",
        }

    def test_a_point_may_add_its_own_fields_flat_beside_the_seven(self):
        """A round number and a turn id read as fields, not as a nested object."""
        row = _row(extra={"turn_id": "abc123", "round": 2, "rounds": 4, "agree": False})
        assert row["turn_id"] == "abc123"
        assert row["round"] == 2 and row["rounds"] == 4
        assert row["agree"] is False
        assert set(row) > {"ts", "point", "session", "latency_ms", "scrubbed", "answers", "error"}

    def test_a_list_field_stays_a_list(self):
        """A reader comparing two arms needs the members, not one rendered string."""
        row = _row(extra={"baseline": ["a", "b"], "jev": []})
        assert row["baseline"] == ["a", "b"]
        assert row["jev"] == []

    def test_no_extra_is_the_seven_fields_exactly(self):
        assert set(_row(extra=None)) == set(_row())
        assert set(_row(extra={})) == set(_row())

    def test_answers_are_flattened_to_value_p_confidence(self):
        row = _row(answers={"verdict": Answer("verdict", "DUP", 0.9, 0.8)})
        assert row["answers"] == {"verdict": {"value": "DUP", "p": 0.9, "confidence": 0.8}}

    def test_no_answers_is_null_not_an_empty_object(self):
        assert _row()["answers"] is None
        assert _row(answers={})["answers"] is None

    def test_the_session_key_is_never_written_verbatim(self):
        row = _row(session_key="chat-with-my-manager")
        assert "chat-with-my-manager" not in json.dumps(row)
        assert row["session"] == session_digest("chat-with-my-manager")

    def test_the_digest_is_stable_and_keyless_calls_share_a_bucket(self):
        assert _row(session_key="k")["session"] == _row(session_key="k")["session"]
        assert _row(session_key=None)["session"] == _row(session_key="")["session"]

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (True, True),
            (False, False),
            (0, False),
            (1, False),
            ("scrubbed:credential", False),
            (["credential"], False),
            (None, False),
        ],
    )
    def test_scrubbed_is_exactly_true_or_false(self, raw, expected):
        """Only ``True`` reads as refused: a truthy stand-in must not.

        A category string or a count in this field would read as "nothing left the
        machine" while carrying something else, and this is the one field a reader
        must be able to trust as a boolean.
        """
        assert _row(scrubbed=raw)["scrubbed"] is expected

    def test_ts_is_utc_and_parseable(self):
        moment = datetime.fromisoformat(_row()["ts"])
        assert moment.tzinfo is not None
        assert moment.utcoffset().total_seconds() == 0


class TestBounds:
    """One malformed provider reply must not write an unbounded line."""

    def test_a_long_value_is_clipped(self):
        row = _row(answers={"v": Answer("v", "x" * 5000, 1.0)})
        assert len(row["answers"]["v"]["value"]) == log_mod._MAX_VALUE_CHARS

    def test_a_number_survives_as_a_number(self):
        """Clipping must not turn a probability into a string a reader has to parse."""
        row = _row(answers={"v": Answer("v", 0.25, 0.25)})
        assert row["answers"]["v"]["value"] == 0.25

    def test_too_many_answers_are_capped(self):
        answers = {f"q{i}": Answer(f"q{i}", "A", 0.5) for i in range(50)}
        assert len(_row(answers=answers)["answers"]) == log_mod._MAX_ANSWERS

    def test_an_unbounded_repr_is_still_bounded(self):
        class _Big:
            def __repr__(self):
                return "y" * 9000

        row = _row(answers={"v": Answer("v", _Big(), 1.0)})
        assert len(row["answers"]["v"]["value"]) == log_mod._MAX_VALUE_CHARS

    def test_an_extra_cannot_rewrite_a_core_field(self):
        """``scrubbed`` and ``session`` must mean what every reader expects."""
        row = _row(
            extra={
                "session": "chat-plaintext",
                "scrubbed": "sort of",
                "ts": "yesterday",
                "point": "somewhere.else",
                "error": "made up",
                "answers": "made up",
                "latency_ms": "ages",
            }
        )
        assert row["session"] == log_mod.session_digest("sess-1")
        assert row["scrubbed"] is False
        assert row["point"] == "skills.select"
        assert row["latency_ms"] == 212
        assert row["error"] is None
        assert row["answers"] is None
        assert row["ts"] != "yesterday"

    def test_too_many_extra_fields_are_capped(self):
        row = _row(extra={f"f{i}": i for i in range(200)})
        added = set(row) - set(_row())
        assert len(added) == log_mod._MAX_EXTRA_KEYS

    def test_a_long_extra_list_and_a_long_extra_value_are_bounded(self):
        row = _row(extra={"keys": ["k"] * 500, "note": "x" * 5000})
        assert len(row["keys"]) == log_mod._MAX_EXTRA_ITEMS
        assert len(row["note"]) == log_mod._MAX_VALUE_CHARS

    def test_an_extra_that_is_not_a_mapping_is_ignored(self):
        assert set(_row(extra=["round", 1])) == set(_row())

    def test_a_null_extra_value_stays_null(self):
        """``p`` is a probability OR nothing; a reader must not parse "None"."""
        assert _row(extra={"p": None})["p"] is None


class TestAppend:
    def test_a_row_lands_as_one_json_line(self, home):
        append(_row())
        text = log_path().read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert len(text.strip().splitlines()) == 1
        assert json.loads(text)["point"] == "skills.select"

    def test_rows_accumulate_rather_than_replace(self, home):
        for i in range(5):
            append(_row(latency_ms=i))
        lines = log_path().read_text(encoding="utf-8").strip().splitlines()
        assert [json.loads(ln)["latency_ms"] for ln in lines] == [0, 1, 2, 3, 4]

    @pytest.mark.skipif(
        os.name != "posix",
        reason="0700 is a POSIX mode; Windows reports 0o777 whatever the open requested",
    )
    def test_the_directory_is_owner_only(self, home):
        append(_row())
        mode = stat.S_IMODE(os.stat(home).st_mode)
        assert mode == 0o700, oct(mode)

    @pytest.mark.skipif(
        os.name != "posix",
        reason="0600 and umask are POSIX; Windows reports 0o666 and has no umask",
    )
    def test_a_fresh_file_is_owner_only_despite_a_permissive_umask(self, home):
        """0600 must come from the open mode, not from inherited umask luck."""
        previous = os.umask(0o000)
        try:
            append(_row())
        finally:
            os.umask(previous)
        mode = stat.S_IMODE(os.stat(log_path()).st_mode)
        assert mode == 0o600, oct(mode)

    def test_the_filename_carries_the_utc_day(self, home):
        append(_row())
        today = datetime.now(timezone.utc).date()
        assert log_path().name == f"decisions-{today:%Y%m%d}.jsonl"

    @pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform has no O_NOFOLLOW")
    def test_append_refuses_a_symlinked_day_log(self, home, tmp_path):
        home.mkdir(parents=True)
        target = tmp_path / "protected.txt"
        target.write_text("unchanged", encoding="utf-8")
        log_path().symlink_to(target)

        append(_row())

        assert target.read_text(encoding="utf-8") == "unchanged"
        assert log_path().is_symlink()

    def test_an_unwritable_home_is_survived_not_raised(self, tmp_path, monkeypatch):
        """Best-effort by contract: an observation must not fail a turn."""
        blocked = tmp_path / "blocked"
        blocked.write_text("i am a file, not a directory", encoding="utf-8")
        monkeypatch.setattr(log_mod, "log_dir", lambda: blocked / "decisions")
        append(_row())  # must not raise

    def test_an_unserialisable_value_is_survived(self, home):
        """``default=str`` keeps an odd value from losing the whole row."""

        class _Odd:
            def __str__(self):
                return "odd-value"

            def __repr__(self):
                return "odd-value"

        append(_row(answers={"v": Answer("v", _Odd(), 1.0)}))
        row = json.loads(log_path().read_text(encoding="utf-8"))
        assert row["answers"]["v"]["value"] == "odd-value"

    def test_a_slow_open_leaves_no_empty_day_file(self, home, monkeypatch):
        """A swallowed failure must not leave a day file that exists and holds nothing.

        :func:`append` never raises, so a timeout it swallows is invisible at the
        call site. The one trace it can leave is the leaf the open already created,
        empty -- and reading that back raises ``JSONDecodeError`` instead of
        returning the row, which is how a reader here goes red on a loaded worker.
        """
        original = platform_log_append._pin_and_open_leaf

        def slow(leaf, anchor, stack):
            fd = original(leaf, anchor, stack)
            time.sleep(platform_log_append._APPEND_TIMEOUT_SECONDS + 0.1)
            return fd

        monkeypatch.setattr(platform_log_append, "_pin_and_open_leaf", slow)
        append(_row())
        assert json.loads(log_path().read_text(encoding="utf-8"))["point"] == "skills.select"


class TestRetention:
    """Day-files are bounded: the append after a day boundary sweeps the old ones."""

    @pytest.fixture(autouse=True)
    def _fresh_sweep(self, monkeypatch):
        monkeypatch.setattr(log_mod, "_swept_on", None)

    def _day(self, home, offset_days, *, today):
        from datetime import timedelta

        day = today - timedelta(days=offset_days)
        path = log_path(day)
        home.mkdir(parents=True, exist_ok=True)
        path.write_text('{"old":true}\n')
        return path

    def test_files_older_than_the_retention_window_are_removed(self, home):
        from datetime import date

        today = date(2026, 9, 18)
        keep_edge = self._day(home, log_mod.RETENTION_DAYS, today=today)
        keep_recent = self._day(home, 1, today=today)
        gone = self._day(home, log_mod.RETENTION_DAYS + 1, today=today)
        stranger = home / "notes.jsonl"
        stranger.write_text("mine\n")

        assert log_mod.sweep_expired(today) == 1
        assert not gone.exists()
        assert keep_edge.exists() and keep_recent.exists()
        assert stranger.exists(), "only names this module wrote are candidates"

    def test_a_symlink_wearing_a_day_file_name_is_not_followed(self, home, tmp_path):
        from datetime import date, timedelta

        today = date(2026, 9, 18)
        target = tmp_path / "precious.jsonl"
        target.write_text("keep\n")
        home.mkdir(parents=True, exist_ok=True)
        link = log_path(today - timedelta(days=log_mod.RETENTION_DAYS + 5))
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable")

        assert log_mod.sweep_expired(today) == 0
        assert link.is_symlink() and target.read_text() == "keep\n"

    def test_the_sweep_runs_once_per_day_per_process(self, home):
        from datetime import date

        today = date(2026, 9, 18)
        self._day(home, log_mod.RETENTION_DAYS + 1, today=today)
        assert log_mod.sweep_expired(today) == 1
        self._day(home, log_mod.RETENTION_DAYS + 2, today=today)
        assert log_mod.sweep_expired(today) == 0, "same day: no second scan"
        from datetime import timedelta

        assert log_mod.sweep_expired(today + timedelta(days=1)) == 1

    @pytest.mark.skipif(os.name != "posix", reason="directory links via the POSIX pinned walk")
    def test_a_swapped_directory_link_does_not_redirect_the_sweep(self, home, tmp_path):
        """The listing and the unlink both go through the pinned no-follow descriptor,
        so a link installed under the log directory's name reaches nothing."""
        from datetime import date

        today = date(2026, 9, 18)
        victim_dir = tmp_path / "elsewhere"
        victim_dir.mkdir()
        victim = victim_dir / log_path(today - timedelta(days=log_mod.RETENTION_DAYS + 3)).name
        victim.write_text('{"precious":true}\n')
        home.parent.mkdir(parents=True, exist_ok=True)
        home.symlink_to(victim_dir, target_is_directory=True)

        assert log_mod.sweep_expired(today) == 0
        assert victim.exists() and victim.read_text() == '{"precious":true}\n'

    def test_an_append_triggers_the_sweep(self, home, monkeypatch):
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).date()
        gone = self._day(home, log_mod.RETENTION_DAYS + 1, today=today)
        home.parent.mkdir(parents=True, exist_ok=True)
        append(_row())
        assert not gone.exists()
        assert log_path().exists()


class TestFileCeiling:
    """A day-file is bounded by size as well as by age, and says so once."""

    @pytest.fixture(autouse=True)
    def _small_ceiling(self, monkeypatch):
        monkeypatch.setattr(log_mod, "MAX_FILE_BYTES", 400)
        monkeypatch.setattr(log_mod, "_full_reported", set())
        monkeypatch.setattr(log_mod, "_swept_on", None)

    def test_rows_past_the_ceiling_are_dropped_and_reported_once(self, home, caplog):
        import logging

        home.parent.mkdir(parents=True, exist_ok=True)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.decisions.log"):
            for _ in range(10):
                append(_row())
        size = log_path().stat().st_size
        assert 0 < size <= 400
        rows = log_path().read_text().splitlines()
        assert 0 < len(rows) < 10
        full = [r for r in caplog.records if "reached 400 bytes" in r.getMessage()]
        assert len(full) == 1, "one warning per file, not one per dropped row"
        # Every kept row is whole: the refusal happened before any byte was written.
        import json

        for line in rows:
            json.loads(line)

    def test_a_new_day_file_starts_with_a_clean_slate(self, home):
        from datetime import date

        home.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(10):
            append(_row())
        # A different day is a different file, so it is not full.
        other = log_path(date(2030, 1, 1))
        log_mod.append_line(other, b'{"fresh":1}\n', max_bytes=log_mod.MAX_FILE_BYTES)
        assert other.read_text() == '{"fresh":1}\n'
