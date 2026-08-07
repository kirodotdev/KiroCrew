"""The reproducing-test path reconciliation (T2's real failure mode).

A bug candidate arrives carrying a test path the spine INVENTED from the target
slug, but the authoring prompt tells the agent to name its own
``test/test_bug_<short_slug>.py``. Those names disagree, and the invented one also
collides: the slug is the target path lowercased and truncated to 40 chars, so two
loci under the same long directory prefix produce the SAME filename. The gate then
ran ``pytest --collect-only`` against a nonexistent path, so T2 rejected every
candidate ("reproducing test does not collect") regardless of fix quality — two
independent verified RED->GREEN fixes were both thrown away this way.

These pin the adoption that fixes it.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew.apps.builtins.auto_improvement.spine.agent_runner import _adopt_authored_test
from kiro_crew.apps.builtins.auto_improvement.spine.contracts import (
    BugReproducingTest,
    Candidate,
)


def _candidate(test_path: str = "test/test_bug_invented.py") -> Candidate:
    return Candidate(
        kind="bug",
        target="src/pkg/mod.py::fn",
        signature="sig",
        hypothesis="hyp",
        reproducing_test=BugReproducingTest(test_id=test_path, test_path=test_path),
    )


def _porcelain(*paths: str) -> str:
    # '??' = untracked (a brand-new repro test), 'A ' = staged add, ' M' = modified.
    return "\n".join(f"?? {p}" for p in paths)


def _rt(c: Candidate) -> BugReproducingTest:
    """The candidate's repro test, asserted present (narrows the Optional for mypy)."""
    rt = c.reproducing_test
    assert rt is not None
    return rt


class TestAdoptAuthoredTest:
    def test_adopts_the_file_the_agent_actually_wrote(self, tmp_path: Path) -> None:
        (tmp_path / "test").mkdir()
        (tmp_path / "test" / "test_bug_archive_nan_sort.py").write_text("def test_x(): pass\n")
        c = _candidate()
        _adopt_authored_test(c, tmp_path, _porcelain("test/test_bug_archive_nan_sort.py"))
        assert _rt(c).test_path == "test/test_bug_archive_nan_sort.py"
        # test_id doubles as the pytest nodeid for the RED/GREEN runs.
        assert _rt(c).test_id == "test/test_bug_archive_nan_sort.py"

    def test_a_modified_source_file_is_not_mistaken_for_the_test(self, tmp_path: Path) -> None:
        (tmp_path / "test").mkdir()
        (tmp_path / "test" / "test_bug_real.py").write_text("def test_x(): pass\n")
        c = _candidate()
        porcelain = " M src/pkg/mod.py\n?? test/test_bug_real.py\n"
        _adopt_authored_test(c, tmp_path, porcelain)
        assert _rt(c).test_path == "test/test_bug_real.py"

    def test_no_authored_test_leaves_the_candidate_untouched(self, tmp_path: Path) -> None:
        """Only a source edit: keep whatever the spine had and let the gate judge it."""
        c = _candidate("test/test_bug_invented.py")
        _adopt_authored_test(c, tmp_path, " M src/pkg/mod.py\n")
        assert _rt(c).test_path == "test/test_bug_invented.py"

    def test_a_named_file_that_is_not_on_disk_is_ignored(self, tmp_path: Path) -> None:
        """Porcelain can name a path that was written then removed; never point the
        gate at a file that does not exist."""
        c = _candidate("test/test_bug_invented.py")
        _adopt_authored_test(c, tmp_path, _porcelain("test/test_bug_ghost.py"))
        assert _rt(c).test_path == "test/test_bug_invented.py"

    def test_multiple_authored_tests_pick_is_deterministic(self, tmp_path: Path) -> None:
        (tmp_path / "test").mkdir()
        for n in ("test_bug_bbb.py", "test_bug_aaa.py", "test_bug_a_longer_name.py"):
            (tmp_path / "test" / n).write_text("def test_x(): pass\n")
        picks = set()
        for _ in range(3):
            c = _candidate()
            _adopt_authored_test(
                c,
                tmp_path,
                _porcelain(
                    "test/test_bug_bbb.py",
                    "test/test_bug_aaa.py",
                    "test/test_bug_a_longer_name.py",
                ),
            )
            picks.add(_rt(c).test_path)
        # Shortest-then-lexicographic → stable across runs, so a re-run gates the
        # same file instead of flapping between candidates.
        assert picks == {"test/test_bug_aaa.py"}

    def test_a_renamed_path_takes_the_destination(self, tmp_path: Path) -> None:
        (tmp_path / "test").mkdir()
        (tmp_path / "test" / "test_bug_final.py").write_text("def test_x(): pass\n")
        c = _candidate()
        _adopt_authored_test(c, tmp_path, "R  test/test_bug_old.py -> test/test_bug_final.py\n")
        assert _rt(c).test_path == "test/test_bug_final.py"

    def test_a_candidate_without_a_repro_test_is_a_noop(self, tmp_path: Path) -> None:
        c = Candidate(kind="perf", target="src/pkg/mod.py::fn", signature="s", hypothesis="h")
        _adopt_authored_test(c, tmp_path, _porcelain("test/test_bug_x.py"))
        assert c.reproducing_test is None


class TestTheInventedSlugCollides:
    """Why adoption is REQUIRED, not a nicety: the spine's invented name is not
    merely different from the agent's — it is ambiguous between distinct targets."""

    @staticmethod
    def _invented(target: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", target.lower()).strip("_")[:40] or "surface"
        return f"test/test_bug_{slug}.py"

    def test_two_distinct_loci_produce_the_same_invented_path(self) -> None:
        a = "src/kiro_crew/apps/builtins/auto_improvement/spine/archive.py::Archive.top_k"
        b = "src/kiro_crew/apps/builtins/auto_improvement/spine/keeper.py::fingerprint"
        assert self._invented(a) == self._invented(b)


class TestArchiveTsvSurvivesAgentText:
    """A candidate's `description` is agent-authored and can contain a literal TAB or
    newline. `append_row` joins the control columns with TAB into `results.tsv`, so an
    unescaped tab shifts every later column and a newline splits one row into several —
    corrupting the append-only archive that results.tsv readers parse positionally. The
    full value still survives untouched in the JSONL row. Raised by the GPT review.
    """

    def test_a_tab_or_newline_in_a_cell_does_not_break_the_row(self, tmp_path: Path) -> None:
        import json as _json

        from kiro_crew.apps.builtins.auto_improvement.spine.archive import (
            CONTROL_COLUMNS,
            Archive,
        )

        arc = Archive(tmp_path / "run")
        arc.append_row(
            {
                "cycle": 1,
                "cand_id": "c1",
                "status": "kept",
                "description": "line one\twith tab\nand a newline\r\nand a CRLF",
                "diff_ref": "c1.diff",
            }
        )
        lines = [ln for ln in (tmp_path / "run" / "results.tsv").read_text().splitlines() if ln]
        # Header + exactly ONE data row — the newlines did not split it into three.
        assert len(lines) == 2, f"the agent text split the TSV into {len(lines)} lines"
        data = lines[1]
        # Exactly the right number of columns — the tab did not shift the layout.
        assert data.count("\t") == len(CONTROL_COLUMNS) - 1
        # The description cell itself carries no tab (they were collapsed to spaces).
        desc_cell = data.split("\t")[CONTROL_COLUMNS.index("description")]
        assert "\t" not in desc_cell and "with tab" in desc_cell
        # The UNALTERED value is still recoverable from the JSONL row beside it.
        jrow = _json.loads((tmp_path / "run" / "candidates.jsonl").read_text().splitlines()[-1])
        assert "\t" in jrow["description"] and "\n" in jrow["description"]
