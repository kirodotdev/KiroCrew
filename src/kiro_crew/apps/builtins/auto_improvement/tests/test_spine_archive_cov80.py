"""The run archive's read side — resume, top-K memory, and TSV hygiene.

``results/`` is append-only and the run's state is RECONSTRUCTED from it on restart, so
the reads pinned here are load-bearing: a corrupt JSONL line must be skipped rather
than end a resume, ``cycle_count`` must recover the high-water mark from disk, and
``top_k`` must exclude kept candidates and rows that carry no measurement at all
(the bug track writes ``primary_delta=""``).

Also pinned: the TSV cell scrubber. An agent-authored description can carry a TAB or a
newline, which would shift every later column or split one row into two and corrupt the
archive that positional readers parse.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.apps.builtins.auto_improvement.spine.archive import (
    CONTROL_COLUMNS,
    Archive,
    _jsonable,
    _render_secondary,
)


def _rows(archive: Archive) -> list[dict]:
    return [json.loads(ln) for ln in archive.jsonl.read_text().splitlines() if ln.strip()]


def _tsv_cells(archive: Archive) -> list[list[str]]:
    lines = archive.tsv.read_text().splitlines()
    return [ln.split("\t") for ln in lines]


class TestSecondaryRendering:
    def test_a_dict_renders_as_sorted_key_value_pairs(self) -> None:
        assert _render_secondary({"rss": 2, "cpu": 1}) == "cpu=1;rss=2"

    def test_an_empty_dict_renders_as_an_empty_cell(self) -> None:
        assert _render_secondary({}) == ""

    def test_none_renders_as_an_empty_cell_rather_than_the_word_none(self) -> None:
        assert _render_secondary(None) == ""

    def test_anything_else_is_coerced_so_the_column_is_always_present(self) -> None:
        """The TSV is parsed positionally, so a missing cell would shift the row."""
        assert _render_secondary(12.5) == "12.5"
        assert _render_secondary(["a"]) == "['a']"


# Bound once so the assertion can compare against str(_REPO) instead of the POSIX
# spelling, which is not what Path renders on Windows.
_REPO = Path("/repo")


class TestJsonable:
    def test_a_dataclass_becomes_a_dict(self) -> None:
        @dataclass
        class Row:
            a: int
            p: Path

        # A relative Path with str() on both sides: the property under test is
        # that a Path is serialised via str(), which holds on either separator.
        # An absolute /tmp literal would assert the POSIX spelling and trip the
        # cross-platform gate.
        p = Path("stub") / "x"
        assert _jsonable(Row(a=1, p=p)) == {"a": 1, "p": str(p)}

    def test_a_set_becomes_a_list_so_json_can_encode_it(self) -> None:
        assert _jsonable({"only"}) == ["only"]
        assert sorted(_jsonable(frozenset({"b", "a"}))) == ["a", "b"]

    def test_nested_containers_are_walked(self) -> None:
        # str(), not the POSIX spelling: the coercion is what is under test, and the
        # separator differs on Windows.
        a = Path("/a")
        assert _jsonable({"k": [a, ("b",)]}) == {"k": [str(a), ["b"]]}

    def test_a_scalar_passes_through_untouched(self) -> None:
        assert _jsonable(3) == 3
        assert _jsonable(None) is None


class TestRunMetadata:
    def test_meta_round_trips_through_the_jsonable_coercion(self, tmp_path: Path) -> None:
        archive = Archive(tmp_path / "results")
        clone = Path("/repo")
        archive.write_meta({"clone": clone, "tracks": {"bug"}})
        assert archive.read_meta() == {"clone": str(clone), "tracks": ["bug"]}

    def test_an_absent_meta_file_reads_as_empty(self, tmp_path: Path) -> None:
        assert Archive(tmp_path / "results").read_meta() == {}

    def test_a_corrupt_meta_file_reads_as_empty_rather_than_raising(self, tmp_path: Path) -> None:
        """A half-written meta must not stop a resume."""
        archive = Archive(tmp_path / "results")
        archive.meta_path.write_text("{not json")
        assert archive.read_meta() == {}


class TestArchiveLayout:
    def test_the_tsv_header_is_written_once_and_not_duplicated_on_reopen(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "results"
        first = Archive(root)
        first.append_row({"cycle": 1, "cand_id": "a"})
        reopened = Archive(root)

        cells = _tsv_cells(reopened)
        assert cells[0] == list(CONTROL_COLUMNS)
        assert len(cells) == 2  # header + the one row, no second header

    def test_it_creates_the_candidate_anchor_and_drift_directories(self, tmp_path: Path) -> None:
        archive = Archive(tmp_path / "results")
        assert archive.candidates_dir.is_dir()
        assert archive.anchors_dir.is_dir()
        assert archive.drift_dir.is_dir()


class TestSavedCandidate:
    def test_it_writes_the_diff_and_detail_and_returns_the_diff_ref(self, tmp_path: Path) -> None:
        archive = Archive(tmp_path / "results")
        ref = archive.save_candidate(
            cand_id="c1_wide_x", diff="diff --git a/x b/x\n", detail={"p": _REPO}
        )
        assert ref == "c1_wide_x.diff"
        assert (archive.candidates_dir / "c1_wide_x.diff").read_text() == "diff --git a/x b/x\n"
        assert json.loads((archive.candidates_dir / "c1_wide_x.json").read_text()) == {
            "p": str(_REPO)
        }

    def test_an_empty_diff_still_produces_a_file(self, tmp_path: Path) -> None:
        archive = Archive(tmp_path / "results")
        archive.save_candidate(cand_id="c2", diff="", detail={})
        assert (archive.candidates_dir / "c2.diff").read_text() == ""


class TestAppendedRow:
    def test_tabs_and_newlines_in_a_cell_are_collapsed_to_spaces(self, tmp_path: Path) -> None:
        """An agent-authored description carrying a TAB would shift every later column."""
        archive = Archive(tmp_path / "results")
        archive.append_row(
            {"cycle": 1, "cand_id": "a", "description": "one\ttwo\nthree\r4", "secondary": {"m": 1}}
        )

        cells = _tsv_cells(archive)[1]
        assert len(cells) == len(CONTROL_COLUMNS)
        assert cells[CONTROL_COLUMNS.index("description")] == "one two three 4"
        assert cells[CONTROL_COLUMNS.index("secondary")] == "m=1"

    def test_the_jsonl_row_keeps_the_unaltered_value(self, tmp_path: Path) -> None:
        archive = Archive(tmp_path / "results")
        archive.append_row({"cycle": 1, "description": "one\ttwo"})
        assert _rows(archive)[0]["description"] == "one\ttwo"

    def test_a_missing_control_column_becomes_an_empty_cell(self, tmp_path: Path) -> None:
        archive = Archive(tmp_path / "results")
        archive.append_row({"cycle": 3})
        cells = _tsv_cells(archive)[1]
        assert cells[CONTROL_COLUMNS.index("commit")] == ""
        assert cells[CONTROL_COLUMNS.index("secondary")] == ""


class TestCycleCount:
    def test_an_empty_archive_has_no_cycles(self, tmp_path: Path) -> None:
        assert Archive(tmp_path / "results").cycle_count() == 0

    def test_it_recovers_the_high_water_mark_from_disk(self, tmp_path: Path) -> None:
        """Held on disk, not in memory, so a restart resumes at the right cycle."""
        archive = Archive(tmp_path / "results")
        for cycle in (1, 4, 2):
            archive.append_row({"cycle": cycle, "cand_id": f"c{cycle}"})
        assert Archive(archive.root).cycle_count() == 4

    def test_a_corrupt_line_is_skipped_rather_than_ending_the_resume(self, tmp_path: Path) -> None:
        archive = Archive(tmp_path / "results")
        archive.append_row({"cycle": 2, "cand_id": "a"})
        with archive.jsonl.open("a") as f:
            f.write("{truncated\n")
        assert archive.cycle_count() == 2


class TestTopK:
    def _archive(self, tmp_path: Path, rows: list[dict]) -> Archive:
        archive = Archive(tmp_path / "results")
        for row in rows:
            archive.append_row(row)
        return archive

    def test_most_improving_first_and_kept_candidates_excluded(self, tmp_path: Path) -> None:
        archive = self._archive(
            tmp_path,
            [
                {"cand_id": "small", "status": "discarded_noise", "primary_delta": "-1"},
                {"cand_id": "best", "status": "discarded_noise", "primary_delta": "-9"},
                {"cand_id": "winner", "status": "kept", "primary_delta": "-99"},
            ],
        )
        assert [r["cand_id"] for r in archive.top_k()] == ["best", "small"]

    def test_rows_with_no_measurement_are_not_ranked(self, tmp_path: Path) -> None:
        """The bug track writes ``primary_delta=""``; ranking it as 0.0 would let a
        non-measurement outrank a real regression."""
        archive = self._archive(
            tmp_path,
            [
                {"cand_id": "bug", "status": "filed", "primary_delta": ""},
                {"cand_id": "none", "status": "failed_gate"},
                {"cand_id": "bad", "status": "failed_gate", "primary_delta": "not-a-number"},
                {"cand_id": "real", "status": "failed_gate", "primary_delta": "-2"},
            ],
        )
        assert [r["cand_id"] for r in archive.top_k()] == ["real"]

    def test_k_bounds_the_result(self, tmp_path: Path) -> None:
        archive = self._archive(
            tmp_path,
            [{"cand_id": f"c{i}", "status": "x", "primary_delta": f"-{i}"} for i in range(5)],
        )
        assert len(archive.top_k(k=2)) == 2

    def test_an_absent_jsonl_yields_nothing(self, tmp_path: Path) -> None:
        assert Archive(tmp_path / "results").top_k() == []

    def test_a_corrupt_line_is_skipped(self, tmp_path: Path) -> None:
        archive = self._archive(tmp_path, [{"cand_id": "ok", "status": "x", "primary_delta": "-1"}])
        with archive.jsonl.open("a") as f:
            f.write("]]not json\n")
        assert [r["cand_id"] for r in archive.top_k()] == ["ok"]


class TestDrift:
    def test_a_rebest_record_carries_the_cycle_and_a_timestamp(self, tmp_path: Path) -> None:
        archive = Archive(tmp_path / "results")
        drift_path = Path("/repo/x")
        archive.write_drift(7, {"reason": "anchor moved", "path": drift_path})

        payload = json.loads((archive.drift_dir / "rebest-7.json").read_text())
        assert payload["cycle"] == 7
        assert payload["reason"] == "anchor moved"
        assert payload["path"] == str(drift_path)
        assert isinstance(payload["ts"], float)
