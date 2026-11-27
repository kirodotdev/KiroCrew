"""The failing tests of a red job, read from its check-run annotations.

``main-red-watch.yml`` and ``main-red-pr-notice.yml`` take the failing pytest node
ids of a job from the annotations of its check run, which the rootdir conftest
writes one per failing test, and fall back to the job log only when those cannot
be read. The rows under ``test/fixtures/main_red/`` are what the API returned for
red jobs of push runs of ``CI`` on main, unedited apart from the choice of jobs:

* a Linux shard with three parametrised failures and the runner's own
  ``Process completed with exit code 1`` row;
* the Windows shard of the same run, red on the same three tests, whose
  ``path`` has a backslash while its title does not;
* a shard whose node ids carry the ``@group`` suffix of ``--dist loadgroup``;
* a shard with 21 failures, annotated as the first ten and one notice;
* a Coverage Gate job, which names no test.

``job_log_linux_shard_tail.txt`` is the end of the log of the first job. It pins
that the log parser reads a real summary, colour codes inside the node id
included, so a watcher that files nothing has a read that failed, not a parse.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest
from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "test" / "fixtures" / "main_red"
SCRIPT = ROOT / ".github" / "scripts" / "main_red_nodeids.py"

mod = load_skill_script("main_red_nodeids_annotations_under_test", SCRIPT)

MEMORY = (
    "test/test_kas_orchestrator_memory.py::TestTheRealSessionNewRequest::"
    "test_an_unconfirmed_resume_is_evicted_without_deleting_its_record"
)
DID_NOT_RAISE = "Failed: DID NOT RAISE <class 'kiro_crew.acp.session_handle.AcpRuntimeError'>"


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _rows(name: str) -> list[dict[str, Any]]:
    return json.loads(_text(name))


def _row(title: Any, message: Any = "x", level: Any = "failure") -> dict[str, Any]:
    return {"annotation_level": level, "title": title, "message": message}


def test_a_linux_shard_gives_each_failing_test_once_with_its_reason() -> None:
    found = mod.parse_annotations(_rows("annotations_linux_shard.json"))
    assert found == {
        f"{MEMORY}[response{n}]": DID_NOT_RAISE[: mod.MESSAGE_CHARS] for n in (0, 2, 1)
    }


def test_the_runners_own_failure_row_is_not_a_test() -> None:
    rows = _rows("annotations_linux_shard.json")
    assert any(row["message"] == "Process completed with exit code 1." for row in rows)
    assert len(mod.parse_annotations(rows)) == len(rows) - 1


def test_a_windows_shard_gives_the_node_id_form_a_linux_shard_gives() -> None:
    rows = _rows("annotations_windows_shard.json")
    assert any("\\" in row["path"] for row in rows)
    linux = mod.parse_annotations(_rows("annotations_linux_shard.json"))
    assert set(mod.parse_annotations(rows)) == set(linux)


def test_a_test_red_on_both_legs_is_repeated_and_leads_the_merged_set() -> None:
    group = mod.parse_annotations(_rows("annotations_xdist_group.json"))
    linux = mod.parse_annotations(_rows("annotations_linux_shard.json"))
    windows = mod.parse_annotations(_rows("annotations_windows_shard.json"))
    assert mod.repeated([group, linux]) == []
    assert mod.repeated([group, linux, windows]) == list(linux)
    merged = mod.merge_failures([group, linux, windows])
    assert list(merged) == list(linux) + list(group)


def test_a_group_suffix_stays_part_of_the_node_id() -> None:
    found = mod.parse_annotations(_rows("annotations_xdist_group.json"))
    assert list(found) == [
        "test/test_wait_discipline_ratchet.py::test_the_scan_matches_known_sites"
        "@tree_scan_wait_discipline_ratchet",
        "test/test_wait_discipline_ratchet.py::test_no_file_gains_a_short_fixed_wait"
        "@tree_scan_wait_discipline_ratchet",
    ]
    assert all(message.startswith("AssertionError: ") for message in found.values())
    assert all(len(message) == mod.MESSAGE_CHARS for message in found.values())


def test_a_shard_with_more_failures_than_are_annotated_gives_the_annotated_ten() -> None:
    rows = _rows("annotations_more_than_ten.json")
    (notice,) = [row for row in rows if row["annotation_level"] == "notice"]
    assert notice["message"].startswith("21 tests failed or errored on this job")
    assert len(mod.parse_annotations(rows)) == 10


def test_a_job_that_ran_no_test_gives_nothing() -> None:
    assert mod.parse_annotations(_rows("annotations_coverage_gate.json")) == {}


@pytest.mark.parametrize(
    "rows",
    [
        None,
        {"message": "Resource not accessible by integration"},
        [None, 3, "x", []],
        [_row("test/test_a.py::test_x", level="warning")],
        [_row("test/test_a.py::test_x", level="notice")],
        [_row("test/test_collect.py", "ImportError: no module named x")],
        [_row("", "Process completed with exit code 1.")],
        [_row("not a path::test_x")],
        [_row("test/test_a.py::")],
        [_row(None)],
        [_row("test/test_a.py::test_x", None)],
    ],
    ids=[
        "null",
        "error-body",
        "odd-rows",
        "warning",
        "notice",
        "collection-error",
        "untitled",
        "no-path",
        "no-name",
        "title-not-text",
        "message-not-text",
    ],
)
def test_a_row_that_is_not_a_failing_test_is_ignored(rows: Any) -> None:
    assert mod.parse_annotations(rows) == {}


def test_the_reason_is_the_message_without_the_node_id_it_repeats() -> None:
    row = _row("test/test_a.py::test_x[a - b]", "test/test_a.py::test_x[a - b] - assert 2 == 1")
    assert mod.parse_annotations([row]) == {"test/test_a.py::test_x[a - b]": "assert 2 == 1"}
    bare = _row("test/test_a.py::test_x", "something else")
    assert mod.parse_annotations([bare]) == {"test/test_a.py::test_x": "something else"}


def test_hostile_annotation_text_is_cut_to_printable_ascii_without_backticks() -> None:
    nodeid = "test/test_x.py::test_a"
    row = _row(nodeid, f"{nodeid} - boom `touch /tmp/pwn` @octocat --> <script> $(id) \x07é")
    ((found, message),) = mod.parse_annotations([row]).items()
    assert found == nodeid
    assert "`" not in message and message.isascii() and message.isprintable()
    assert len(message) <= mod.MESSAGE_CHARS
    long_title = _row("test/test_x.py::" + "t" * 5000, "m")
    ((found, _),) = mod.parse_annotations([long_title]).items()
    assert len(found) == 200


def test_the_real_log_of_the_same_job_parses_to_the_same_node_ids() -> None:
    log = _text("job_log_linux_shard_tail.txt")
    assert "\x1b[31mFAILED\x1b[0m test/test_kas_orchestrator_memory.py::\x1b[1m" in log
    from_log = mod.parse_failures(log)
    from_annotations = mod.parse_annotations(_rows("annotations_linux_shard.json"))
    assert set(from_log) == set(from_annotations)


def test_a_job_with_annotations_is_read_from_them_and_one_without_from_its_log() -> None:
    notes = _text("annotations_linux_shard.json")
    log = "2026-10-26T07:20:01.0000001Z FAILED test/test_only_in_log.py::test_x - boom\n"
    assert list(mod.job_failures(notes, log)) == [f"{MEMORY}[response{n}]" for n in (0, 2, 1)]
    assert mod.job_failures(None, log) == {"test/test_only_in_log.py::test_x": "boom"}
    assert mod.job_failures("not json", log) == {"test/test_only_in_log.py::test_x": "boom"}
    # Annotations that were read and name no test leave the log to speak.
    gate = _text("annotations_coverage_gate.json")
    assert mod.job_failures(gate, log) == {"test/test_only_in_log.py::test_x": "boom"}
    assert mod.job_failures(gate, None) == {}
    assert mod.job_failures(None, None) == {}


def test_extract_reads_each_job_once_from_a_folder_of_annotations_and_logs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "11.json").write_text(_text("annotations_linux_shard.json"), encoding="utf-8")
    (tmp_path / "11.log").write_text("FAILED test/test_ignored.py::test_x - boom\n", "utf-8")
    (tmp_path / "9.log").write_text(_text("job_log_linux_shard_tail.txt"), encoding="utf-8")
    (tmp_path / "120.json").write_text(_text("annotations_xdist_group.json"), encoding="utf-8")
    (tmp_path / "read-error.txt").write_text("FAILED test/test_stray.py::test_x\n", "utf-8")
    assert mod.main(["extract", str(tmp_path)]) == 0
    found = json.loads(capsys.readouterr().out)
    assert len(found) == 5
    assert not any("test_ignored" in nodeid or "test_stray" in nodeid for nodeid in found)
    assert list(found)[-1].startswith("test/test_wait_discipline_ratchet.py::")
    assert mod.main(["confirmed", str(tmp_path)]) == 0
    (tmp_path / "9.log").unlink()
    assert mod.main(["confirmed", str(tmp_path)]) == 1
    assert capsys.readouterr().out.startswith("5 test(s) red in one job only (test/")
    assert mod.main(["names-test", str(tmp_path), "11"]) == 0
    assert mod.main(["names-test", str(tmp_path), "12"]) == 1
    assert mod.main(["names-test", str(tmp_path), "../11"]) == 1


# ── A cleaned value is the same after another cleaning ───────────────────────

ODD_TEXTS = [
    "a" * 79 + " tail that is cut after a space",
    "a" * 78 + "  \t tail",
    "test/test_a.py::test_x[" + "p " * 100 + "]",
    "trailing spaces   ",
    "   leading and    inner   runs",
    "\ttabs\tand\r\nline ends\n",
    "unicode \u00e9\u4e2d\u6587 \U0001f600 and control \x07\x1b[31m",
    "markdown `code` **bold** [link](http://x) <!-- c --> <b> @octocat #12",
    "",
    " ",
    "x" * 500,
]


def _generated_texts() -> list[str]:
    rng = random.Random(39701)
    alphabet = "ab \t\n`<>-@#\u00e9\x07*_[]" + "c" * 6
    return ["".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 400))) for _ in range(300)]


@pytest.mark.parametrize("limit", [mod.MESSAGE_CHARS, 150, 200, 300, 1, 0])
def test_cleaning_a_cleaned_text_changes_nothing_at_any_cut(limit: int) -> None:
    for text in ODD_TEXTS + _generated_texts():
        once = mod.clean(text, limit)
        assert mod.clean(once, limit) == once, repr(text)
        assert len(once) <= limit
        assert once == once.strip() and "  " not in once
        assert once.isascii() and "`" not in once
        assert all(" " <= ch <= "~" for ch in once)


def _annotation(nodeid: str, message: str) -> dict[str, Any]:
    return {"annotation_level": "failure", "title": nodeid, "message": f"{nodeid} - {message}"}


def test_what_the_record_gives_back_equals_what_a_run_gives_fresh() -> None:
    """The issue body is written from a run and read by the next; the two must agree."""
    url = "https://github.com/example/repo/actions/runs/900"
    for index, text in enumerate(ODD_TEXTS + _generated_texts()):
        nodeid = f"test/test_a.py::test_{index}"
        fresh = mod.parse_annotations([_annotation(nodeid, text)])
        assert list(fresh) == [nodeid]
        state = mod.next_state(None, fresh, "a" * 40, 900, url)
        assert mod.parse_state(mod.render_body(state, [], 5))["failures"] == fresh, repr(text)
        log = mod.parse_failures(
            f"FAILED {nodeid} - {text.splitlines()[0] if text.strip() else ''}"
        )
        state = mod.next_state(None, log, "a" * 40, 900, url)
        assert mod.parse_state(mod.render_body(state, [], 5))["failures"] == log, repr(text)
