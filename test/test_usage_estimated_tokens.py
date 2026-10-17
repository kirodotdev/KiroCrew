"""Estimated Kiro CLI token use on the Usage tab.

kiro-cli writes zero into every token field of its per-turn metadata, so the
Usage tab estimates token use from what each turn does record: its model request
count, its context-window usage as a percentage, and its reply length. These
tests pin that arithmetic in ``_estimate_cli_tokens``, the guards on every value
it reads from a session document, and how ``_parse_sessions`` carries the result
onto the payload (this month, last month, and one figure per Daily History row).
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

import kiro_crew.dashboard.handlers.usage as usage_mod
import kiro_crew.hooks as hooks_mod
from conftest import requires_symlinks
from kiro_crew.dashboard.handlers.usage import (
    _empty_session_summary,
    _estimate_cli_tokens,
    _parse_sessions,
    _sum_estimate,
)
from kiro_crew.testing.clock import ManualClock

WINDOW = 1_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _stamp(dt: datetime) -> str:
    """A kiro-cli ``end_timestamp``: UTC with a ``Z`` suffix."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _local_day(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d")


def _noon(now: datetime, days_ago: int) -> datetime:
    """Noon, local time, ``days_ago`` days before the frozen instant ``now``'s day.

    The arithmetic runs on the local wall time and converts at the end, so the
    result carries that day's own UTC offset across a DST change.
    """
    wall = now.replace(tzinfo=None) - timedelta(days=days_ago)
    return wall.replace(hour=12, minute=0, second=0, microsecond=0).astimezone()


def _touch(path: Path, at: datetime) -> Path:
    """Set ``path``'s mtime to the instant ``at``, built from the frozen clock (D3)."""
    ns = ((at - _EPOCH) // timedelta(microseconds=1)) * 1000
    os.utime(path, ns=(ns, ns))
    return path


def _turn(
    end: datetime, pct: Any = None, *, requests: Any = 1, reply: Any = 0, model: Any = "m-1"
) -> dict[str, Any]:
    return {
        "model": model,
        "end_timestamp": _stamp(end),
        "total_request_count": requests,
        "assistant_response_length": reply,
        "final_context_usage_percentage": pct,
        "context_usage_percentage": pct,
        # kiro-cli's own token fields: always zero on disk, never read.
        "input_token_count": 0,
        "output_token_count": 0,
    }


def _doc(turns: list[Any], *, model_id: Any = "m-1", window: Any = WINDOW) -> dict[str, Any]:
    return {
        "session_id": "s",
        "session_state": {
            "rts_model_state": {
                "model_info": {"model_id": model_id, "context_window_tokens": window}
            },
            "conversation_metadata": {"user_turn_metadatas": turns},
        },
    }


def _write(d: Path, name: str, doc: object, *, mtime: datetime | None = None) -> Path:
    """Write ``doc`` as JSON; ``mtime`` stamps the file from the frozen clock."""
    path = d / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path if mtime is None else _touch(path, mtime)


@pytest.fixture
def cli_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cli"
    d.mkdir()
    return d


def _listing(d: Path) -> list[os.DirEntry[str]]:
    """The directory listing ``_parse_sessions`` hands the estimator."""
    with os.scandir(d) as scan:
        return sorted(scan, key=lambda e: e.name)


class _Listed:
    """A listing entry whose metadata was fixed when the directory was listed."""

    def __init__(self, path: Path, *, mtime: float, is_symlink: bool = False) -> None:
        self.path = str(path)
        self.name = path.name
        self.mtime = mtime
        self.link = is_symlink
        self.stat_calls = 0

    def stat(self, *, follow_symlinks: bool = True) -> SimpleNamespace:
        assert follow_symlinks is False, "the estimator must not follow a link"
        self.stat_calls += 1
        return SimpleNamespace(st_mtime=self.mtime)

    def is_symlink(self) -> bool:
        return self.link


class _OsWithoutStat:
    """``os`` for the module under test, with the stat-by-name calls removed."""

    def __getattr__(self, name: str) -> Any:
        if name in {"stat", "lstat"}:
            raise AssertionError(f"the estimator called os.{name} on a path")
        return getattr(os, name)


def _estimate(d: Path) -> dict[str, Any]:
    """Every turn counts: a window from the epoch to the far future."""
    return _estimate_cli_tokens(
        _listing(d),
        root=d,
        since_day="1970-01-01",
        since_epoch=0.0,
        until_day="9999-12-31",
    )


DAY = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


class TestEstimateCliTokens:
    def test_input_is_requests_times_the_mean_of_start_and_end_context(self, cli_dir: Path) -> None:
        # Turn 1 grows the context 0 -> 100k over 2 requests: 2 * (0 + 100k) / 2.
        # Turn 2 grows it 100k -> 200k over 4 requests: 4 * (100k + 200k) / 2.
        _write(
            cli_dir,
            "a.json",
            _doc([_turn(DAY, 10, requests=2, reply=400), _turn(DAY, 20, requests=4, reply=800)]),
        )
        r = _estimate(cli_dir)
        assert r == {
            "by_day": {_local_day(DAY): {"input": 700_000, "output": 300, "requests": 6}},
            "unreadable": 0,
        }

    def test_days_follow_each_turns_local_end_time(self, cli_dir: Path) -> None:
        later = DAY + timedelta(days=1)
        _write(cli_dir, "a.json", _doc([_turn(DAY, 10, requests=1), _turn(later, 10, requests=1)]))
        by_day = _estimate(cli_dir)["by_day"]
        assert by_day == {
            _local_day(DAY): {"input": 50_000, "output": 0, "requests": 1},
            # The second turn starts where the first ended: 1 * (100k + 100k) / 2.
            _local_day(later): {"input": 100_000, "output": 0, "requests": 1},
        }

    def test_sessions_do_not_share_context(self, cli_dir: Path) -> None:
        _write(cli_dir, "a.json", _doc([_turn(DAY, 50, requests=1)]))
        _write(cli_dir, "b.json", _doc([_turn(DAY, 10, requests=1)]))
        # 1 * (0 + 500k) / 2 + 1 * (0 + 100k) / 2: each session starts from zero.
        assert _estimate(cli_dir)["by_day"][_local_day(DAY)]["input"] == 300_000

    def test_a_turn_without_a_context_reading_keeps_the_context_it_started_with(
        self, cli_dir: Path
    ) -> None:
        turns = [
            _turn(DAY, 10, requests=1),
            _turn(DAY, None, requests=3),
            _turn(DAY, 30, requests=2),
        ]
        _write(cli_dir, "a.json", _doc(turns))
        # 1 * (0 + 100k) / 2  +  3 * (100k + 100k) / 2  +  2 * (100k + 300k) / 2.
        assert _estimate(cli_dir)["by_day"][_local_day(DAY)]["input"] == 50_000 + 300_000 + 400_000

    def test_the_start_of_turn_percentage_stands_in_for_a_missing_final_one(
        self, cli_dir: Path
    ) -> None:
        turn = _turn(DAY, None, requests=2)
        turn["context_usage_percentage"] = 25
        _write(cli_dir, "a.json", _doc([turn]))
        assert _estimate(cli_dir)["by_day"][_local_day(DAY)]["input"] == 250_000

    def test_a_turn_on_another_model_uses_that_models_window(
        self, cli_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            usage_mod.model_registry, "model_window", lambda m: 200_000 if m == "m-2" else None
        )
        _write(cli_dir, "a.json", _doc([_turn(DAY, 50, requests=1, model="m-2")]))
        # 50% of the m-2 window, not of the session model's 1M window.
        assert _estimate(cli_dir)["by_day"][_local_day(DAY)]["input"] == 50_000

    def test_an_oversized_registry_window_keeps_the_turns_start_context(
        self, cli_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            usage_mod.model_registry, "model_window", lambda m: 10**400 if m == "m-2" else None
        )
        turns = [
            _turn(DAY, 10, requests=1),
            _turn(DAY, 50, requests=3, model="m-2"),
        ]
        _write(cli_dir, "a.json", _doc(turns))
        # Turn 2 has no usable window, so it starts and ends at turn 1's 100k context.
        assert _estimate(cli_dir)["by_day"][_local_day(DAY)]["input"] == 50_000 + 300_000

    @pytest.mark.parametrize(
        "model", ["auto", "", None, 7], ids=["auto", "empty", "absent", "not-a-string"]
    )
    def test_auto_or_unnamed_turns_use_the_session_window(
        self, cli_dir: Path, monkeypatch: pytest.MonkeyPatch, model: object
    ) -> None:
        monkeypatch.setattr(usage_mod.model_registry, "model_window", lambda m: 1)
        _write(cli_dir, "a.json", _doc([_turn(DAY, 50, requests=1, model=model)], window=400_000))
        assert _estimate(cli_dir)["by_day"][_local_day(DAY)]["input"] == 100_000

    def test_without_a_session_window_the_registry_answers_for_the_session_model(
        self, cli_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            usage_mod.model_registry, "model_window", lambda m: 300_000 if m == "m-1" else None
        )
        _write(cli_dir, "a.json", _doc([_turn(DAY, 50, requests=1)], window=None))
        assert _estimate(cli_dir)["by_day"][_local_day(DAY)]["input"] == 75_000

    def test_an_unknown_window_counts_the_turn_with_its_start_context(
        self, cli_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(usage_mod.model_registry, "model_window", lambda m: None)
        _write(cli_dir, "a.json", _doc([_turn(DAY, 50, requests=3, reply=40)], window=None))
        # No window, so no context reading: the first turn starts and stays at zero.
        assert _estimate(cli_dir)["by_day"][_local_day(DAY)] == {
            "input": 0,
            "output": 10,
            "requests": 3,
        }

    @pytest.mark.parametrize(
        "value",
        [True, -1, 1.5, "3", None, 10**13, [2], {"n": 2}],
        ids=["bool", "negative", "float", "string", "absent", "huge", "list", "dict"],
    )
    def test_an_unusable_count_reads_as_zero(self, cli_dir: Path, value: object) -> None:
        _write(cli_dir, "a.json", _doc([_turn(DAY, 10, requests=value, reply=value)]))
        assert _estimate(cli_dir)["by_day"][_local_day(DAY)] == {
            "input": 0,
            "output": 0,
            "requests": 0,
        }

    @pytest.mark.parametrize(
        "pct",
        [101, -1, 100.5, math.nan, math.inf, True, "50", 10**400],
        ids=["over-100", "negative", "over-100-float", "nan", "inf", "bool", "string", "huge-int"],
    )
    def test_an_unusable_percentage_is_no_reading(self, cli_dir: Path, pct: object) -> None:
        path = cli_dir / "a.json"
        doc = _doc([_turn(DAY, 10, requests=1), _turn(DAY, "PCT", requests=1)])
        # json.dumps cannot write NaN/inf as JSON tokens; Python's json writes and
        # reads them as the NaN/Infinity extension, and a huge int as digits.
        path.write_text(json.dumps(doc).replace('"PCT"', json.dumps(pct)), encoding="utf-8")
        # Turn 2 keeps turn 1's 100k end: 1 * (0 + 100k) / 2 + 1 * (100k + 100k) / 2.
        assert _estimate(cli_dir)["by_day"][_local_day(DAY)]["input"] == 150_000

    @pytest.mark.parametrize(
        "window",
        [0, -5, True, "1000000", 10**13],
        ids=["zero", "negative", "bool", "string", "huge"],
    )
    def test_an_unusable_session_window_falls_back_to_the_registry(
        self, cli_dir: Path, monkeypatch: pytest.MonkeyPatch, window: object
    ) -> None:
        monkeypatch.setattr(usage_mod.model_registry, "model_window", lambda m: 100_000)
        _write(cli_dir, "a.json", _doc([_turn(DAY, 50, requests=1)], window=window))
        assert _estimate(cli_dir)["by_day"][_local_day(DAY)]["input"] == 25_000

    def test_turns_before_the_window_seed_the_context_but_are_not_counted(
        self, cli_dir: Path
    ) -> None:
        before = DAY - timedelta(days=3)
        _write(cli_dir, "a.json", _doc([_turn(before, 10, requests=5), _turn(DAY, 20, requests=1)]))
        r = _estimate_cli_tokens(
            _listing(cli_dir),
            root=cli_dir,
            since_day=_local_day(DAY),
            since_epoch=0.0,
            until_day="9999-12-31",
        )
        # Only turn 2, starting from turn 1's 100k end: 1 * (100k + 200k) / 2.
        assert r["by_day"] == {_local_day(DAY): {"input": 150_000, "output": 0, "requests": 1}}

    def test_turns_after_the_window_seed_the_context_but_are_not_counted(
        self, cli_dir: Path
    ) -> None:
        # A clock that ran ahead for one turn and was then corrected.
        after = DAY + timedelta(days=3)
        _write(cli_dir, "a.json", _doc([_turn(after, 10, requests=5), _turn(DAY, 20, requests=1)]))
        r = _estimate_cli_tokens(
            _listing(cli_dir),
            root=cli_dir,
            since_day="1970-01-01",
            since_epoch=0.0,
            until_day=_local_day(DAY),
        )
        # Only turn 2, starting from turn 1's 100k end: 1 * (100k + 200k) / 2.
        assert r["by_day"] == {_local_day(DAY): {"input": 150_000, "output": 0, "requests": 1}}

    def test_a_turn_with_no_readable_end_time_is_not_placed_on_a_day(self, cli_dir: Path) -> None:
        turn = _turn(DAY, 10, requests=1)
        turn["end_timestamp"] = "not a time"
        _write(cli_dir, "a.json", _doc([turn]))
        assert _estimate(cli_dir)["by_day"] == {}

    def test_a_document_the_validator_refuses_is_never_statted_and_counts_unreadable(
        self, cli_dir: Path
    ) -> None:
        listed = _Listed(_write(cli_dir, "a.json", _doc([_turn(DAY, 10, requests=1)])), mtime=0.0)
        with (
            patch.object(usage_mod, "validate_file_path", return_value=None) as validator,
            patch.object(usage_mod, "os", _OsWithoutStat()),
            patch.object(usage_mod, "safe_read_file_bytes_nolink") as reader,
        ):
            r = _estimate_cli_tokens(
                [listed],
                root=cli_dir,
                since_day="1970-01-01",
                since_epoch=0.0,
                until_day="9999-12-31",
            )
        validator.assert_called_once_with(listed.path)
        assert listed.stat_calls == 0
        reader.assert_not_called()
        assert r == {"by_day": {}, "unreadable": 1}

    def test_a_document_untouched_since_before_the_window_is_not_read(self, cli_dir: Path) -> None:
        path = _write(cli_dir, "a.json", _doc([_turn(DAY, 10, requests=1)]))
        os.utime(path, (1_000_000, 1_000_000))
        with patch.object(usage_mod, "safe_read_file_bytes_nolink") as reader:
            r = _estimate_cli_tokens(
                _listing(cli_dir),
                root=cli_dir,
                since_day="1970-01-01",
                since_epoch=2_000_000.0,
                until_day="9999-12-31",
            )
        assert r == {"by_day": {}, "unreadable": 0}
        reader.assert_not_called()  # the listed mtime skips the reader

    def test_unreadable_documents_are_counted(
        self, cli_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (cli_dir / "bad-json.json").write_text("{not json", encoding="utf-8")
        (cli_dir / "bad-utf8.json").write_bytes(b'{"session_state": "\xff\xfe"}')
        _write(cli_dir, "big.json", _doc([_turn(DAY, 10)] * 50))
        refused = _write(cli_dir, "refused.json", _doc([_turn(DAY, 10)]))
        _write(cli_dir, "good.json", _doc([_turn(DAY, 10, requests=2)]))
        monkeypatch.setattr(usage_mod, "_CLI_SESSION_JSON_MAX_BYTES", 2_000)
        real_reader = hooks_mod.safe_read_file_bytes_nolink

        def _read(path: str, root: str, *, max_bytes: int) -> bytes | None:
            if path == str(refused):
                return None
            return real_reader(path, root, max_bytes=max_bytes)

        with patch.object(usage_mod, "safe_read_file_bytes_nolink", side_effect=_read):
            r = _estimate(cli_dir)
        assert r["unreadable"] == 4
        assert r["by_day"] == {_local_day(DAY): {"input": 100_000, "output": 0, "requests": 2}}

    @requires_symlinks
    def test_a_dangling_link_is_unreadable(self, cli_dir: Path) -> None:
        (cli_dir / "gone.json").symlink_to(cli_dir / "missing-target.json")
        assert _estimate(cli_dir) == {"by_day": {}, "unreadable": 1}

    @requires_symlinks
    def test_a_session_document_link_outside_root_is_unreadable(
        self, cli_dir: Path, tmp_path: Path
    ) -> None:
        outside = _write(tmp_path, "outside.json", _doc([_turn(DAY, 10, requests=1)]))
        (cli_dir / "link.json").symlink_to(outside)
        assert _estimate(cli_dir) == {"by_day": {}, "unreadable": 1}

    @requires_symlinks
    def test_a_link_to_a_document_in_the_same_directory_counts_its_target_once(
        self, cli_dir: Path
    ) -> None:
        # The validator resolves the link, so the reader would open the target
        # and count its turns a second time. The link counts as unreadable.
        target = _write(cli_dir, "b.json", _doc([_turn(DAY, 10, requests=1)]))
        (cli_dir / "a.json").symlink_to(target)
        assert _estimate(cli_dir) == {
            "by_day": {_local_day(DAY): {"input": 50_000, "output": 0, "requests": 1}},
            "unreadable": 1,
        }

    def test_a_link_in_the_window_is_judged_from_the_listing_before_the_reader_runs(
        self, cli_dir: Path
    ) -> None:
        # The reader opens a link's target, so for a link inside the sessions
        # directory it returns the target's bytes. The listing entry already
        # says the name is a link, so the document is refused before the read,
        # and no path is statted or opened by name to find that out.
        document = _write(cli_dir, "a.json", _doc([_turn(DAY, 10, requests=1)]))
        listed = _Listed(document, mtime=3_000_000.0, is_symlink=True)
        with (
            patch.object(usage_mod, "os", _OsWithoutStat()),
            patch.object(
                usage_mod, "safe_read_file_bytes_nolink", return_value=document.read_bytes()
            ) as reader,
        ):
            r = _estimate_cli_tokens(
                [listed],
                root=cli_dir,
                since_day="1970-01-01",
                since_epoch=2_000_000.0,
                until_day="9999-12-31",
            )
        assert listed.stat_calls == 1
        reader.assert_not_called()
        assert r == {"by_day": {}, "unreadable": 1}

    def test_a_link_untouched_since_before_the_window_is_skipped_like_any_old_document(
        self, cli_dir: Path
    ) -> None:
        listed = _Listed(
            _write(cli_dir, "a.json", _doc([_turn(DAY, 10, requests=1)])),
            mtime=1_000_000.0,
            is_symlink=True,
        )
        with (
            patch.object(usage_mod, "os", _OsWithoutStat()),
            patch.object(usage_mod, "safe_read_file_bytes_nolink") as reader,
        ):
            r = _estimate_cli_tokens(
                [listed],
                root=cli_dir,
                since_day="1970-01-01",
                since_epoch=2_000_000.0,
                until_day="9999-12-31",
            )
        reader.assert_not_called()
        assert r == {"by_day": {}, "unreadable": 0}

    @requires_symlinks
    def test_a_document_swapped_for_a_link_after_validation_is_unreadable(
        self, cli_dir: Path, tmp_path: Path
    ) -> None:
        document = _write(cli_dir, "session.json", _doc([_turn(DAY, 10, requests=1)]))
        outside = _write(tmp_path, "outside.json", _doc([_turn(DAY, 20, requests=2)]))
        real_validate = hooks_mod.validate_file_path
        swapped = False

        def _validate_then_swap(raw: str) -> str | None:
            nonlocal swapped
            validated = real_validate(raw)
            if raw == str(document) and validated is not None and not swapped:
                document.unlink()
                document.symlink_to(outside)
                swapped = True
            return validated

        with (
            patch.object(hooks_mod, "validate_file_path", side_effect=_validate_then_swap),
            patch.object(usage_mod, "validate_file_path", side_effect=_validate_then_swap),
        ):
            r = _estimate(cli_dir)
        assert r == {"by_day": {}, "unreadable": 1}

    def test_the_mtime_check_reads_the_listing_and_stats_no_path(self, cli_dir: Path) -> None:
        # The document on disk is new, but its listing entry says it is old.
        # The estimator trusts the entry and stats nothing by name, so on
        # Windows the check opens no path that a junction swapped into an
        # ancestor of the sessions directory could redirect.
        listed = _Listed(
            _write(cli_dir, "a.json", _doc([_turn(DAY, 10, requests=1)])), mtime=1_000_000.0
        )
        with (
            patch.object(usage_mod, "os", _OsWithoutStat()),
            patch.object(usage_mod, "safe_read_file_bytes_nolink") as reader,
        ):
            r = _estimate_cli_tokens(
                [listed],
                root=cli_dir,
                since_day="1970-01-01",
                since_epoch=2_000_000.0,
                until_day="9999-12-31",
            )
        assert listed.stat_calls == 1
        reader.assert_not_called()
        assert r == {"by_day": {}, "unreadable": 0}

    @pytest.mark.parametrize(
        "doc",
        [
            {"session_id": "s", "sessionState": _doc([_turn(DAY, 10)])["session_state"]},
            {"other": 1},
            {"session_state": "x"},
            [],
            "text",
            {"session_state": {"rts_model_state": {"model_info": {"model_id": "m-1"}}}},
            {"session_state": {"conversation_metadata": {"userTurnMetadatas": [_turn(DAY, 10)]}}},
            {"session_state": {"conversation_metadata": {"user_turn_metadatas": "x"}}},
        ],
        ids=[
            "renamed-session-state",
            "no-session-state",
            "session-state-not-an-object",
            "array",
            "string",
            "no-conversation-metadata",
            "renamed-turn-list",
            "turns-not-a-list",
        ],
    )
    def test_a_document_not_shaped_like_a_session_is_unreadable(
        self, cli_dir: Path, doc: object
    ) -> None:
        # kiro-cli writes only session documents here, so a renamed key in its
        # format raises the unreadable-sessions warning, not a silent zero.
        _write(cli_dir, "a.json", doc)
        assert _estimate(cli_dir) == {"by_day": {}, "unreadable": 1}

    def test_a_turn_list_with_no_readable_end_time_is_unreadable(self, cli_dir: Path) -> None:
        # A renamed stamp key leaves every turn off the calendar: the document
        # feeds the warning rather than reading as a session with nothing to count.
        turns = [_turn(DAY, 10, requests=1), _turn(DAY, 20, requests=1, reply=400)]
        for turn in turns:
            turn["ended_at"] = turn.pop("end_timestamp")
        _write(cli_dir, "a.json", _doc(turns))
        assert _estimate(cli_dir) == {"by_day": {}, "unreadable": 1}

    def test_a_session_with_no_turns_yet_is_not_unreadable(self, cli_dir: Path) -> None:
        # A session opened and never prompted holds an empty turn list: valid,
        # nothing to count, and common enough to raise the warning everywhere.
        _write(cli_dir, "a.json", _doc([]))
        assert _estimate(cli_dir) == {"by_day": {}, "unreadable": 0}

    def test_one_readable_end_time_keeps_a_document_readable(self, cli_dir: Path) -> None:
        # One dated turn, even outside the window, is enough: the undated
        # sibling is skipped, not a warning.
        undated = _turn(DAY, 10, requests=1)
        del undated["end_timestamp"]
        before = DAY - timedelta(days=3)
        _write(cli_dir, "a.json", _doc([undated, _turn(before, 20, requests=1)]))
        r = _estimate_cli_tokens(
            _listing(cli_dir),
            root=cli_dir,
            since_day=_local_day(DAY),
            since_epoch=0.0,
            until_day="9999-12-31",
        )
        assert r == {"by_day": {}, "unreadable": 0}

    def test_only_json_documents_are_read(self, cli_dir: Path) -> None:
        _write(cli_dir, "a.jsonl", _doc([_turn(DAY, 10)]))
        _write(cli_dir, "a.lock", _doc([_turn(DAY, 10)]))
        assert _estimate(cli_dir) == {"by_day": {}, "unreadable": 0}

    def test_non_dict_turns_are_skipped(self, cli_dir: Path) -> None:
        _write(cli_dir, "a.json", _doc(["x", 3, None, _turn(DAY, 10, requests=1)]))
        assert _estimate(cli_dir)["by_day"][_local_day(DAY)]["requests"] == 1


class TestSumEstimate:
    def test_half_open_range(self) -> None:
        by_day = {
            "2026-08-31": {"input": 1, "output": 10, "requests": 100},
            "2026-09-01": {"input": 2, "output": 20, "requests": 200},
            "2026-09-30": {"input": 3, "output": 30, "requests": 300},
            "2026-10-01": {"input": 4, "output": 40, "requests": 400},
        }
        assert _sum_estimate(by_day, "2026-09-01", "2026-10-01") == {
            "input": 5,
            "output": 50,
            "requests": 500,
        }
        assert _sum_estimate(by_day, "2026-10-01", None) == {
            "input": 4,
            "output": 40,
            "requests": 400,
        }
        assert _sum_estimate({}, "2026-10-01", None) == {"input": 0, "output": 0, "requests": 0}


#: The local wall times ``_parse_sessions`` is frozen at: the last half-minute of a
#: month, the first half-minute of the next, a year's first, and a mid-month noon.
#: The clock reads of the product and the test both come from one of these, so no
#: run crosses a midnight or a month end between the two sides.
_MID_MONTH = datetime(2026, 9, 15, 12, 0, 0)
_STRADDLE_WALLS = {
    "month-end-235930": datetime(2026, 9, 30, 23, 59, 30),
    "month-start-000030": datetime(2026, 10, 1, 0, 0, 30),
    "year-start-000030": datetime(2027, 1, 1, 0, 0, 30),
    "mid-month-noon": _MID_MONTH,
}
#: The zones a local-time test runs under (D5). ``host`` keeps the process zone
#: and needs no ``tzset``, so Windows, where ``local_tz`` skips, still runs it.
_ZONES = {
    "host": None,
    "utc": "UTC",
    "plus-14": "Pacific/Kiritimati",
    "minus-0330": "America/St_Johns",
}
_STRADDLE = [
    pytest.param((wall, zone), id=f"{when}-{where}")
    for when, wall in _STRADDLE_WALLS.items()
    for where, zone in _ZONES.items()
]


@pytest.fixture
def frozen(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Freeze ``usage_mod``'s clock at one local instant and return it (D2, D5).

    ``_parse_sessions`` reads the clock through the module's own bindings only:
    ``datetime.now()`` and ``time.time()`` for today and the history cutoff,
    ``datetime.fromtimestamp(cutoff)`` for the history start, and
    ``daily_credits``'s ``datetime.now().astimezone()``. A ``ManualClock``
    installed on those bindings is the one clock both sides read; the stdlib
    clocks stay real. The instant comes back aware, in the process zone, and a
    test builds every turn time and every file mtime from it.

    Parametrize indirectly with ``(wall, zone)``: ``wall`` is the naive local time
    read as now, ``zone`` an IANA name pinned through ``local_tz`` (POSIX-only, so
    that variant skips on Windows) or ``None`` for the host's zone. Without a
    parameter the clock stops at a mid-month noon in the host's zone.
    """
    wall, zone = getattr(request, "param", (_MID_MONTH, None))
    if zone is not None:
        request.getfixturevalue("local_tz")(zone)
    now = wall.astimezone()
    ManualClock(now.timestamp()).install(monkeypatch, usage_mod, datetime=True)
    return now


@pytest.fixture
def no_shards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = tmp_path / "tokens"
    d.mkdir()
    monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", d)


@pytest.mark.usefixtures("no_shards", "frozen")
class TestParseSessionsCarriesTheEstimate:
    @pytest.mark.parametrize("frozen", _STRADDLE, indirect=True)
    def test_this_month_and_last_month_split_at_the_month_start(
        self, cli_dir: Path, frozen: datetime
    ) -> None:
        # A turn ending now is always this month; noon ``now.day`` days earlier is
        # always the last day of last month, and the next step back the month before.
        now = frozen
        last_month = _noon(now, now.day)
        two_months_ago = _noon(now, now.day + last_month.day)
        _write(cli_dir, "a.json", _doc([_turn(two_months_ago, 10, requests=7)]), mtime=now)
        _write(cli_dir, "b.json", _doc([_turn(last_month, 10, requests=2, reply=40)]), mtime=now)
        _write(cli_dir, "c.json", _doc([_turn(now, 20, requests=1, reply=8)]), mtime=now)
        with patch.object(usage_mod, "_SESSIONS_DIR", cli_dir):
            r = _parse_sessions()
        assert r["estimated_tokens"] == {
            "this_month": {"input": 100_000, "output": 2, "requests": 1},
            "last_month": {"input": 100_000, "output": 10, "requests": 2},
            "unreadable_sessions": 0,
        }

    def test_history_rows_carry_the_day_estimate(self, cli_dir: Path, frozen: datetime) -> None:
        today = _noon(frozen, 0)
        transcript = cli_dir / "s1.jsonl"
        transcript.write_text(json.dumps({"kind": "Prompt", "timestamp": today.isoformat()}) + "\n")
        _touch(transcript, frozen)
        _write(cli_dir, "s1.json", _doc([_turn(today, 10, requests=2, reply=400)]), mtime=frozen)
        with patch.object(usage_mod, "_SESSIONS_DIR", cli_dir):
            r = _parse_sessions()
        assert r["daily_history"] == [
            {
                "date": _local_day(today),
                "sessions": 1,
                "messages": 1,
                "tool_calls": 0,
                "credits": 0.0,
                "est_tokens": 100_000 + 100,
            }
        ]

    def test_a_day_with_only_estimated_tokens_gets_a_zero_session_row(
        self, cli_dir: Path, frozen: datetime
    ) -> None:
        # A session that started yesterday and ran a turn today: its transcript
        # counts on the start day, its token use on the day the turn ended.
        today, yesterday = _noon(frozen, 0), _noon(frozen, 1)
        transcript = cli_dir / "s1.jsonl"
        transcript.write_text(
            json.dumps({"kind": "Prompt", "timestamp": yesterday.isoformat()}) + "\n"
        )
        _touch(transcript, frozen)
        _write(cli_dir, "s1.json", _doc([_turn(today, 10, requests=1)]), mtime=frozen)
        with patch.object(usage_mod, "_SESSIONS_DIR", cli_dir):
            r = _parse_sessions()
        rows = {h["date"]: (h["sessions"], h["est_tokens"]) for h in r["daily_history"]}
        assert rows == {_local_day(yesterday): (1, 0), _local_day(today): (0, 50_000)}

    def test_an_estimate_older_than_the_history_window_adds_no_row(
        self, cli_dir: Path, frozen: datetime
    ) -> None:
        old = _noon(frozen, usage_mod._SESSIONS_HISTORY_DAYS + 1)
        _write(cli_dir, "a.json", _doc([_turn(old, 10, requests=1)]), mtime=frozen)
        with patch.object(usage_mod, "_SESSIONS_DIR", cli_dir):
            r = _parse_sessions()
        assert r["daily_history"] == []

    @pytest.mark.parametrize("frozen", _STRADDLE, indirect=True)
    def test_a_turn_stamped_after_today_is_neither_summed_nor_a_row(
        self, cli_dir: Path, frozen: datetime
    ) -> None:
        # Clock skew or an imported session can stamp a turn in the future. It
        # neither inflates this month (a sum with no upper bound) nor adds a
        # Daily History row dated after today.
        today, ahead = frozen, _noon(frozen, -40)
        _write(
            cli_dir,
            "a.json",
            _doc([_turn(today, 10, requests=1), _turn(ahead, 20, requests=1)]),
            mtime=frozen,
        )
        with patch.object(usage_mod, "_SESSIONS_DIR", cli_dir):
            r = _parse_sessions()
        # Today's turn alone: 1 * (0 + 100k) / 2.
        assert r["estimated_tokens"]["this_month"] == {"input": 50_000, "output": 0, "requests": 1}
        assert [h["date"] for h in r["daily_history"]] == [_local_day(today)]

    def test_history_days_before_last_month_still_get_an_estimate(
        self, cli_dir: Path, monkeypatch: pytest.MonkeyPatch, frozen: datetime
    ) -> None:
        # Whenever the history window opens before the 1st of last month (the
        # first days of March with a 30-day window, every day with a 75-day one),
        # a day between the two starts still gets its estimate: the scan starts
        # at the earlier of them, while the month sums keep their own bounds.
        monkeypatch.setattr(usage_mod, "_SESSIONS_HISTORY_DAYS", 75)
        between = _noon(frozen, 70)  # at most 61 days reach the 1st of last month
        _write(cli_dir, "a.json", _doc([_turn(between, 10, requests=1)]), mtime=frozen)
        with patch.object(usage_mod, "_SESSIONS_DIR", cli_dir):
            r = _parse_sessions()
        assert r["daily_history"] == [
            {
                "date": _local_day(between),
                "sessions": 0,
                "messages": 0,
                "tool_calls": 0,
                "credits": 0.0,
                "est_tokens": 50_000,
            }
        ]
        zero = {"input": 0, "output": 0, "requests": 0}
        assert r["estimated_tokens"]["this_month"] == zero
        assert r["estimated_tokens"]["last_month"] == zero

    def test_unreadable_documents_reach_the_payload(self, cli_dir: Path, frozen: datetime) -> None:
        (cli_dir / "a.json").write_text("{", encoding="utf-8")
        _touch(cli_dir / "a.json", frozen)
        with patch.object(usage_mod, "_SESSIONS_DIR", cli_dir):
            r = _parse_sessions()
        assert r["estimated_tokens"]["unreadable_sessions"] == 1

    def test_deeply_nested_json_reaches_the_unreadable_count(
        self, cli_dir: Path, frozen: datetime
    ) -> None:
        depth = 100_000
        (cli_dir / "deeply-nested.json").write_text("[" * depth + "]" * depth, encoding="utf-8")
        _touch(cli_dir / "deeply-nested.json", frozen)
        with patch.object(usage_mod, "_SESSIONS_DIR", cli_dir):
            r = _parse_sessions()
        assert r["estimated_tokens"]["unreadable_sessions"] == 1

    def test_a_missing_sessions_dir_has_a_zero_estimate(self, tmp_path: Path) -> None:
        with patch.object(usage_mod, "_SESSIONS_DIR", tmp_path / "absent"):
            r = _parse_sessions()
        assert r["estimated_tokens"] == _empty_session_summary()["estimated_tokens"]

    def test_the_cold_refresh_shape_carries_a_zero_estimate(self) -> None:
        zero = {"input": 0, "output": 0, "requests": 0}
        assert _empty_session_summary()["estimated_tokens"] == {
            "this_month": zero,
            "last_month": zero,
            "unreadable_sessions": 0,
        }
