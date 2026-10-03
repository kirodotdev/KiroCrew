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

WINDOW = 1_000_000


def _stamp(dt: datetime) -> str:
    """A kiro-cli ``end_timestamp``: UTC with a ``Z`` suffix."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _local_day(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d")


def _noon(days_ago: int) -> datetime:
    return (datetime.now().astimezone() - timedelta(days=days_ago)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )


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


def _write(d: Path, name: str, doc: object) -> Path:
    path = d / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


@pytest.fixture
def cli_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cli"
    d.mkdir()
    return d


def _estimate(d: Path) -> dict[str, Any]:
    """Every turn counts: a window from the epoch to the far future."""
    return _estimate_cli_tokens(
        sorted(d.iterdir()),
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
            sorted(cli_dir.iterdir()),
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
            sorted(cli_dir.iterdir()),
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
        document = _write(cli_dir, "a.json", _doc([_turn(DAY, 10, requests=1)]))
        real_lstat = Path.lstat

        def _reject_document_lstat(path: Path) -> os.stat_result:
            if path == document:
                raise AssertionError("the refused document was statted")
            return real_lstat(path)

        with (
            patch.object(usage_mod, "validate_file_path", return_value=None),
            patch.object(Path, "lstat", autospec=True, side_effect=_reject_document_lstat),
        ):
            r = _estimate(cli_dir)

        assert r == {"by_day": {}, "unreadable": 1}

    def test_a_document_untouched_since_before_the_window_is_not_read(self, cli_dir: Path) -> None:
        path = _write(cli_dir, "a.json", _doc([_turn(DAY, 10, requests=1)]))
        os.utime(path, (1_000_000, 1_000_000))
        with patch.object(usage_mod, "safe_read_file_bytes_nolink") as reader:
            r = _estimate_cli_tokens(
                [path],
                root=cli_dir,
                since_day="1970-01-01",
                since_epoch=2_000_000.0,
                until_day="9999-12-31",
            )
        assert r == {"by_day": {}, "unreadable": 0}
        reader.assert_not_called()  # the validated mtime check skips the reader

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

    @requires_symlinks
    def test_the_mtime_check_never_follows_a_link_to_its_target(
        self, cli_dir: Path, tmp_path: Path
    ) -> None:
        target = tmp_path / "outside.json"
        target.write_text(json.dumps(_doc([_turn(DAY, 10)])), encoding="utf-8")
        os.utime(target, (1_000_000, 1_000_000))
        link = cli_dir / "link.json"
        link.symlink_to(target)
        real_lstat = os.lstat
        operations: list[tuple[str, str]] = []

        def _validate_to_target(raw: str) -> str:
            operations.append(("validate", raw))
            return str(target)

        def _lstat_validated(path: os.PathLike[str] | str) -> os.stat_result:
            operations.append(("lstat", os.fspath(path)))
            return real_lstat(path)

        with (
            patch.object(usage_mod, "validate_file_path", side_effect=_validate_to_target),
            patch.object(usage_mod.os, "lstat", side_effect=_lstat_validated),
            patch.object(usage_mod, "safe_read_file_bytes_nolink") as reader,
        ):
            r = _estimate_cli_tokens(
                [link],
                root=cli_dir,
                since_day="1970-01-01",
                since_epoch=2_000_000.0,
                until_day="9999-12-31",
            )
        assert operations == [("validate", str(link)), ("lstat", str(target))]
        reader.assert_not_called()
        assert r == {"by_day": {}, "unreadable": 0}

        with (
            patch.object(usage_mod, "validate_file_path", return_value=None) as validator,
            patch.object(usage_mod.os, "lstat") as stat,
            patch.object(usage_mod, "safe_read_file_bytes_nolink") as reader,
        ):
            r = _estimate_cli_tokens(
                [link],
                root=cli_dir,
                since_day="1970-01-01",
                since_epoch=2_000_000.0,
                until_day="9999-12-31",
            )
        validator.assert_called_once_with(str(link))
        stat.assert_not_called()
        reader.assert_not_called()
        assert r == {"by_day": {}, "unreadable": 1}

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
            sorted(cli_dir.iterdir()),
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


def _month_start() -> datetime:
    return datetime.now().astimezone().replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@pytest.fixture
def no_shards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = tmp_path / "tokens"
    d.mkdir()
    monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", d)


@pytest.mark.usefixtures("no_shards")
class TestParseSessionsCarriesTheEstimate:
    def test_this_month_and_last_month_split_at_the_month_start(self, cli_dir: Path) -> None:
        # Today is always in this month; the day before the 1st is always last month.
        this_month = datetime.now().astimezone().replace(second=0, microsecond=0) - timedelta(
            minutes=1
        )
        if this_month < _month_start():
            this_month = _month_start() + timedelta(minutes=1)
        last_month = (_month_start() - timedelta(days=1)).replace(hour=12)
        two_months_ago = (last_month.replace(day=1) - timedelta(days=1)).replace(hour=12)
        _write(cli_dir, "a.json", _doc([_turn(two_months_ago, 10, requests=7)]))
        _write(cli_dir, "b.json", _doc([_turn(last_month, 10, requests=2, reply=40)]))
        _write(cli_dir, "c.json", _doc([_turn(this_month, 20, requests=1, reply=8)]))
        with patch.object(usage_mod, "_SESSIONS_DIR", cli_dir):
            r = _parse_sessions()
        assert r["estimated_tokens"] == {
            "this_month": {"input": 100_000, "output": 2, "requests": 1},
            "last_month": {"input": 100_000, "output": 10, "requests": 2},
            "unreadable_sessions": 0,
        }

    def test_history_rows_carry_the_day_estimate(self, cli_dir: Path) -> None:
        today = _noon(0)
        (cli_dir / "s1.jsonl").write_text(
            json.dumps({"kind": "Prompt", "timestamp": today.isoformat()}) + "\n"
        )
        _write(cli_dir, "s1.json", _doc([_turn(today, 10, requests=2, reply=400)]))
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

    def test_a_day_with_only_estimated_tokens_gets_a_zero_session_row(self, cli_dir: Path) -> None:
        # A session that started yesterday and ran a turn today: its transcript
        # counts on the start day, its token use on the day the turn ended.
        today, yesterday = _noon(0), _noon(1)
        (cli_dir / "s1.jsonl").write_text(
            json.dumps({"kind": "Prompt", "timestamp": yesterday.isoformat()}) + "\n"
        )
        _write(cli_dir, "s1.json", _doc([_turn(today, 10, requests=1)]))
        with patch.object(usage_mod, "_SESSIONS_DIR", cli_dir):
            r = _parse_sessions()
        rows = {h["date"]: (h["sessions"], h["est_tokens"]) for h in r["daily_history"]}
        assert rows == {_local_day(yesterday): (1, 0), _local_day(today): (0, 50_000)}

    def test_an_estimate_older_than_the_history_window_adds_no_row(self, cli_dir: Path) -> None:
        old = _noon(usage_mod._SESSIONS_HISTORY_DAYS + 1)
        _write(cli_dir, "a.json", _doc([_turn(old, 10, requests=1)]))
        with patch.object(usage_mod, "_SESSIONS_DIR", cli_dir):
            r = _parse_sessions()
        assert r["daily_history"] == []

    def test_a_turn_stamped_after_today_is_neither_summed_nor_a_row(self, cli_dir: Path) -> None:
        # Clock skew or an imported session can stamp a turn in the future. It
        # neither inflates this month (a sum with no upper bound) nor adds a
        # Daily History row dated after today.
        today, ahead = _noon(0), _noon(-40)
        _write(
            cli_dir, "a.json", _doc([_turn(today, 10, requests=1), _turn(ahead, 20, requests=1)])
        )
        with patch.object(usage_mod, "_SESSIONS_DIR", cli_dir):
            r = _parse_sessions()
        # Today's turn alone: 1 * (0 + 100k) / 2.
        assert r["estimated_tokens"]["this_month"] == {"input": 50_000, "output": 0, "requests": 1}
        assert [h["date"] for h in r["daily_history"]] == [_local_day(today)]

    def test_history_days_before_last_month_still_get_an_estimate(
        self, cli_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Whenever the history window opens before the 1st of last month (the
        # first days of March with a 30-day window, every day with a 75-day one),
        # a day between the two starts still gets its estimate: the scan starts
        # at the earlier of them, while the month sums keep their own bounds.
        monkeypatch.setattr(usage_mod, "_SESSIONS_HISTORY_DAYS", 75)
        between = _noon(70)  # at most 61 days reach the 1st of last month
        _write(cli_dir, "a.json", _doc([_turn(between, 10, requests=1)]))
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

    def test_unreadable_documents_reach_the_payload(self, cli_dir: Path) -> None:
        (cli_dir / "a.json").write_text("{", encoding="utf-8")
        with patch.object(usage_mod, "_SESSIONS_DIR", cli_dir):
            r = _parse_sessions()
        assert r["estimated_tokens"]["unreadable_sessions"] == 1

    def test_deeply_nested_json_reaches_the_unreadable_count(self, cli_dir: Path) -> None:
        depth = 100_000
        (cli_dir / "deeply-nested.json").write_text("[" * depth + "]" * depth, encoding="utf-8")
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
