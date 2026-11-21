"""The chunked coverage report equals one ``coverage xml`` and fails closed.

``scripts/ci/coverage_chunked_report.py`` sits between coverage.py and the
Coverage Gate: the XML it merges is the file the gate's floors are read from.
Two things are pinned here.

* Equality. For a real (small) measured tree, the merged XML is byte for byte
  what coverage.py's own XML reporter writes in one pass, for every chunk
  count, apart from the ``timestamp`` attribute. The reporter runs in this
  process through coverage's API, so no interpreter is started.
* Fail closed. Every way a chunk can be wrong ends the build with a named
  error and leaves no merged XML behind: a command that exits non-zero or
  writes nothing, a report that is malformed or disagrees with its own lines,
  a file reported by the wrong chunk, twice, or by none.

The workflow half runs the Coverage Combine step's own body under bash with
shell functions standing in for ``coverage`` and ``python3``.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import coverage
import pytest
import yaml

from conftest import _find_posix_test_shell
from scripts.ci import coverage_chunked_report as chunked

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci" / "coverage_chunked_report.py"
SOURCES = {
    "src/pkg/__init__.py": "",
    "src/pkg/alpha.py": "def pick(flag):\n    if flag:\n        return 1\n    return 2\n\n\nVALUE = pick(True)\n",
    "src/pkg/beta.py": "import os\n\nNAME = os.sep\nfor item in (1, 2):\n    if item > 5:\n        NAME = str(item)\n",
    "src/pkg/sub/__init__.py": "",
    "src/pkg/sub/part9.py": "def unused():\n    return 9\n",
    "src/pkg/sub/part10.py": "TOTAL = 0\nwhile TOTAL < 3:\n    TOTAL += 1\n",
    "src/pkg/sub/deep/gamma.py": "try:\n    X = 1\nexcept ValueError:\n    X = 2\n",
    "src/other/delta.py": "Y = [n for n in range(3) if n]\n",
}
ARCS = {
    "src/pkg/__init__.py": [],
    "src/pkg/alpha.py": [(-1, 1), (1, 7), (7, -1), (-1, 2), (2, 3), (3, -1)],
    "src/pkg/beta.py": [(-1, 1), (1, 3), (3, 4), (4, 5), (5, 4), (4, -1)],
    "src/pkg/sub/__init__.py": [],
    "src/pkg/sub/part9.py": [(-1, 1), (1, -1)],
    "src/pkg/sub/part10.py": [(-1, 1), (1, 2), (2, 3), (3, 2), (2, -1)],
    "src/pkg/sub/deep/gamma.py": [(-1, 1), (1, 2), (2, -1)],
    "src/other/delta.py": [(-1, 1), (1, -1)],
}
CONFIG = "[coverage:run]\nbranch = true\nrelative_files = true\n\n[coverage:report]\nshow_missing = true\n"
FILES = sorted(SOURCES)
_REPORTER_LOCK = threading.Lock()


def _masked(text: str) -> str:
    return re.sub(r'timestamp="\d+"', 'timestamp="T"', text)


def _coverage() -> coverage.Coverage:
    cov = coverage.Coverage(data_file=".coverage", config_file="setup.cfg")
    cov.load()
    return cov


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A measured source tree with combined branch data, and the cwd set to it."""
    for name, body in SOURCES.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    (tmp_path / "setup.cfg").write_text(CONFIG, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    data = coverage.CoverageData(basename=".coverage")
    data.add_arcs({name: arcs for name, arcs in ARCS.items() if arcs})
    for name, arcs in ARCS.items():
        if not arcs:
            data.touch_file(name)
    data.write()
    return tmp_path


def in_process(argv, stdout):
    """Stand in for ``python -m coverage xml|report``: the real reporters, in this process."""
    command, rest = argv[3], list(argv[4:])
    with _REPORTER_LOCK:
        try:
            if command == "xml":
                _coverage().xml_report(morfs=rest[2:], outfile=rest[1])
                return 0, ""
            buffer = io.StringIO()
            _coverage().report(morfs=rest, file=buffer)
        except coverage.exceptions.CoverageException as exc:
            return 1, str(exc)
    if stdout is None:
        return 0, buffer.getvalue()
    Path(stdout).write_text(buffer.getvalue(), encoding="utf-8")
    return 0, ""


def _measured() -> list[str]:
    return sorted(_coverage().get_data().measured_files())


def _build(tree: Path, chunks: int, runner=in_process, files=None) -> str:
    return chunked.build(
        files or _measured(),
        chunks,
        tree / "merged.xml",
        tree / "report.txt",
        tree / "work",
        runner=runner,
    )


def _single(tree: Path) -> str:
    _coverage().xml_report(outfile=str(tree / "single.xml"))
    return (tree / "single.xml").read_text(encoding="utf-8")


def _fails(tree: Path, runner, match: str, chunks: int = 3) -> str:
    with pytest.raises(chunked.ChunkError, match=match):
        _build(tree, chunks, runner)
    assert not (tree / "merged.xml").exists()
    return (tree / "report.txt").read_text(encoding="utf-8")


def _edit_xml(index: int, edit):
    """A runner that rewrites chunk ``index``'s XML after the real reporter wrote it."""

    def runner(argv, stdout):
        result = in_process(argv, stdout)
        if argv[3] == "xml" and Path(argv[5]).name == f"chunk-{index}.xml":
            path = Path(argv[5])
            edited = edit(path.read_text(encoding="utf-8"))
            if edited is None:
                path.unlink()
            else:
                path.write_text(edited, encoding="utf-8")
        return result

    return runner


def test_the_fixture_measures_every_source_file_with_branches(tree: Path) -> None:
    assert _measured() == FILES
    single = _single(tree)
    assert 'branch="true"' in single and 'hits="0"' in single and "missing-branches" in single


@pytest.mark.parametrize("chunks", range(1, len(SOURCES) + 1))
def test_the_merged_xml_is_what_one_coverage_xml_writes(tree: Path, chunks: int) -> None:
    _build(tree, chunks)
    assert _masked((tree / "merged.xml").read_text(encoding="utf-8")) == _masked(_single(tree))


@pytest.mark.parametrize("chunks", [1, 3, len(SOURCES)])
def test_every_measured_file_has_one_row_in_one_table(tree: Path, chunks: int) -> None:
    total = _build(tree, chunks)
    report = (tree / "report.txt").read_text(encoding="utf-8")
    rows = [line.split() for line in report.splitlines() if line.startswith("src/")]
    buffer = io.StringIO()
    _coverage().report(file=buffer)
    assert rows == [
        line.split() for line in buffer.getvalue().splitlines() if line.startswith("src/")
    ]
    assert sorted(row[0] for row in rows) == FILES
    assert report.count("== chunk ") == chunks
    assert report.rstrip("\n").endswith(total)


@pytest.mark.parametrize("chunks", [1, 2, len(SOURCES)])
def test_data_without_branch_arcs_merges_to_what_coverage_writes(tree: Path, chunks: int) -> None:
    (tree / ".coverage").unlink()
    data = coverage.CoverageData(basename=".coverage")
    data.add_lines(
        {
            name: sorted({line for arc in arcs for line in arc if line > 0})
            for name, arcs in ARCS.items()
        }
    )
    data.write()
    _build(tree, chunks)
    single = _single(tree)
    assert 'branch="true"' not in single
    assert _masked((tree / "merged.xml").read_text(encoding="utf-8")) == _masked(single)


def test_the_overall_total_is_the_merged_xmls(tree: Path) -> None:
    total = _build(tree, 3)
    percent = _coverage().xml_report(outfile=str(tree / "single.xml"))
    assert f"{len(FILES)} files" in total
    assert f"{percent:.2f}% covered" in total


def test_a_file_omitted_by_configuration_is_left_out_as_coverage_leaves_it_out(tree: Path) -> None:
    (tree / "setup.cfg").write_text(CONFIG + "omit =\n    */other/*\n", encoding="utf-8")
    _build(tree, 3)
    merged = (tree / "merged.xml").read_text(encoding="utf-8")
    assert _masked(merged) == _masked(_single(tree))
    assert "src/other/delta.py" not in merged
    assert "omitted by configuration: src/other/delta.py" in (tree / "report.txt").read_text(
        encoding="utf-8"
    )


def test_a_missing_source_file_fails_as_it_fails_coverage_xml(tree: Path) -> None:
    (tree / "src/pkg/beta.py").unlink()
    report = _fails(tree, in_process, "coverage xml exited 1: No source for code")
    assert "FAILED" in report


def test_a_chunk_whose_xml_command_fails_stops_the_build(tree: Path) -> None:
    def runner(argv, stdout):
        if argv[3] == "xml" and argv[5].endswith("chunk-2.xml"):
            return 7, "boom"
        return in_process(argv, stdout)

    report = _fails(tree, runner, "chunk 2: coverage xml exited 7: boom")
    assert "== chunk 1 of 3" in report and "FAILED: chunk 2" in report


def test_a_chunk_whose_report_command_fails_stops_the_build(tree: Path) -> None:
    def runner(argv, stdout):
        if argv[3] == "report" and stdout and stdout.name == "chunk-3.txt":
            return 2, "under"
        return in_process(argv, stdout)

    _fails(tree, runner, "chunk 3: coverage report exited 2: under")


def test_a_chunk_that_writes_no_xml_stops_the_build(tree: Path) -> None:
    _fails(
        tree, _edit_xml(1, lambda text: None), "chunk 1: coverage xml exited 0 and wrote no report"
    )


@pytest.mark.parametrize(
    ("edit", "match"),
    [
        (lambda text: text[: len(text) // 2], "not a readable XML report"),
        (lambda text: "", "not a readable XML report"),
        (
            lambda text: text.replace("<coverage ", "<report ").replace("</coverage>", "</report>"),
            "root element",
        ),
        (
            lambda text: re.sub(r'lines-valid="\d+"', 'lines-valid="1"', text),
            "disagree with its <line> elements",
        ),
        (lambda text: re.sub(r' branches-covered="\d+"', "", text), "no numeric branches-covered"),
        (lambda text: text.replace('hits="1"', 'hits="2"', 1), "hits value other than 0 or 1"),
        (
            lambda text: text.replace("<methods/>", "<methods><method/></methods>", 1),
            "unexpected shape",
        ),
        (
            lambda text: text.replace("\t<packages>", "\t<extra/>\n\t<packages>"),
            "exactly <sources> and <packages>",
        ),
        (
            lambda text: re.sub(
                r'condition-coverage="[^"]*"', 'condition-coverage="half"', text, count=1
            ),
            "unreadable",
        ),
        (
            lambda text: re.sub(r'<coverage version="[\d.]+"', '<coverage version="0.0"', text),
            "different coverage version",
        ),
        (lambda text: '<!DOCTYPE c [<!ENTITY e "x">]>\n' + text, "not a readable XML report"),
    ],
)
def test_a_chunk_report_that_cannot_be_trusted_stops_the_build(
    tree: Path, edit, match: str
) -> None:
    _fails(tree, _edit_xml(2, edit), match)


def test_a_file_reported_by_a_chunk_it_was_not_dealt_to_stops_the_build(tree: Path) -> None:
    def runner(argv, stdout):
        if argv[3] == "xml":
            second = argv[5].endswith("chunk-2.xml")
        else:
            second = bool(stdout and stdout.name == "chunk-2.txt")
        return in_process([*argv, FILES[0]] if second else argv, stdout)

    _fails(tree, runner, f"chunk 2 reports {FILES[0]}, which it was not dealt")


def test_a_measured_file_no_chunk_reports_stops_the_build(tree: Path) -> None:
    def runner(argv, stdout):
        if argv[3] in ("xml", "report") and FILES[-1] in argv and len(argv) > 5:
            argv = [arg for arg in argv if arg != FILES[-1]]
        return in_process(argv, stdout)

    _fails(tree, runner, "in no chunk's report and coverage does not call them omitted")


def test_a_table_with_a_row_missing_stops_the_build(tree: Path) -> None:
    def runner(argv, stdout):
        result = in_process(argv, stdout)
        if argv[3] == "report" and stdout and stdout.name == "chunk-1.txt":
            lines = stdout.read_text(encoding="utf-8").splitlines()
            stdout.write_text("\n".join(lines[:2] + lines[3:]) + "\n", encoding="utf-8")
        return result

    _fails(tree, runner, "chunk 1: the text table has")


def test_the_same_file_from_two_chunks_is_refused() -> None:
    entry = chunked.ClassReport("p", (("filename", "a.py"),), (), (0, 0, 0, 0))
    one = chunked.ChunkReport("1", (), ("",), classes={"a.py": entry})
    two = chunked.ChunkReport("1", (), ("",), classes={"a.py": entry})
    with pytest.raises(chunked.ChunkError, match="reported by two chunks"):
        chunked.merge_chunks([["a.py"], ["a.py"]], [one, two])
    with pytest.raises(chunked.ChunkError, match="1 chunk.s. were planned and 2 reported"):
        chunked.merge_chunks([["a.py"]], [one, two])


@pytest.mark.parametrize("count", [0, 1, 2, 7, 8, 9, 100, 4808])
@pytest.mark.parametrize("chunks", [1, 2, 3, 4, 8])
def test_the_plan_is_contiguous_and_holds_every_file_once(count: int, chunks: int) -> None:
    files = [f"src/f{index:05d}.py" for index in range(count)]
    if chunks > max(1, count):
        with pytest.raises(chunked.ChunkError, match="cannot deal"):
            chunked.plan_chunks(files, chunks)
        return
    plan = chunked.plan_chunks(list(reversed(files)), chunks)
    assert [name for part in plan for name in part] == files
    assert chunked.plan_chunks(["a10.py", "a9.py", "a1.py"], 1) == [["a1.py", "a9.py", "a10.py"]]
    sizes = [len(part) for part in plan]
    assert len(plan) == chunks and max(sizes) - min(sizes) <= 1


@pytest.mark.parametrize("chunks", [1, 2, 3, 4, 8])
@pytest.mark.parametrize("heavy", [0, 1, 17, 39])
def test_a_weighted_plan_is_contiguous_complete_and_never_leaves_a_range_empty(
    chunks: int, heavy: int
) -> None:
    files = [f"src/f{index:05d}.py" for index in range(40)]
    weights = {name: 1 + (index * 37) % 11 for index, name in enumerate(files)}
    weights[files[heavy]] = 10_000
    plan = chunked.plan_chunks(files, chunks, weights)
    assert [name for part in plan for name in part] == files
    assert len(plan) == chunks and all(plan)


def test_a_weighted_plan_evens_out_the_ranges() -> None:
    files = [f"src/f{index:05d}.py" for index in range(400)]
    weights = {name: 100 if index < 40 else 1 for index, name in enumerate(files)}
    by_count = [sum(weights[name] for name in part) for part in chunked.plan_chunks(files, 4)]
    by_weight = [
        sum(weights[name] for name in part) for part in chunked.plan_chunks(files, 4, weights)
    ]
    assert max(by_weight) < max(by_count) / 2
    assert max(by_weight) <= sum(weights.values()) / 4 + 100


def test_the_plan_refuses_a_file_named_twice() -> None:
    with pytest.raises(chunked.ChunkError, match="names a file twice"):
        chunked.plan_chunks(["a.py", "a.py"], 1)


@pytest.mark.parametrize(
    ("cpus", "files", "expected"),
    [
        (4, 4808, 4),
        (2, 4808, 2),
        (64, 4808, chunked.MAX_CHUNKS),
        (None, 4808, 1),
        (0, 4808, 1),
        (4, 3, 3),
        (4, 0, 1),
    ],
)
def test_the_chunk_count_follows_the_cpus_up_to_the_cap(cpus, files: int, expected: int) -> None:
    assert chunked.chunk_count(cpus, files) == expected


def test_fail_under_is_refused_before_anything_runs(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tree / "setup.cfg").write_text(CONFIG + "fail_under = 90\n", encoding="utf-8")
    assert chunked.main(["--xml", "merged.xml", "--report", "report.txt"]) == 1
    assert "fail_under is set" in capsys.readouterr().err
    assert not (tree / "merged.xml").exists()


def test_the_real_commands_never_ignore_errors(tree: Path) -> None:
    seen: list[list[str]] = []

    def runner(argv, stdout):
        seen.append(list(argv))
        return in_process(argv, stdout)

    _build(tree, 2, runner)
    assert {argv[3] for argv in seen} == {"xml", "report"}
    for argv in seen:
        assert argv[1:3] == ["-m", "coverage"]
        assert not {"-i", "--ignore-errors", "--fail-under"} & set(argv)
        assert not [
            arg for arg in argv if arg.startswith(("--omit", "--include", "--skip", "--fail-under"))
        ]


# ── The workflow step ──────────────────────────────────────────────────────


def _combine_step() -> str:
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"]
    steps = jobs["coverage-combine"]["steps"]
    (step,) = [s for s in steps if "coverage combine" in str(s.get("run", ""))]
    return str(step["run"])


STAND_INS = """
coverage() { echo "coverage $*" >> calls.log; return "${COMBINE_RC:-0}"; }
python3() {
  echo "python3 $*" >> calls.log
  [ -z "${REPORT_TEXT:-}" ] || echo "$REPORT_TEXT" > coverage-report.txt
  [ -z "${XML_TEXT:-}" ] || echo "$XML_TEXT" > coverage.xml
  return "${SCRIPT_RC:-0}"
}
"""


def _run_step(
    tmp_path: Path,
    present=("setup.cfg", ".coverage.ipv6", ".coverage.kernel-lock-owner"),
    **env: str,
):
    for name in present:
        (tmp_path / name).write_text("x", encoding="utf-8")
    shell = _find_posix_test_shell() if os.name == "nt" else shutil.which("bash")
    assert shell, "this test needs bash (Git Bash on Windows)"
    done = subprocess.run(
        [shell, "-eo", "pipefail", "-c", STAND_INS + _combine_step()],
        cwd=tmp_path,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,  # flake-ok: upper bound on a bash child that runs shell functions only
    )
    log = tmp_path / "calls.log"
    return done, log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_the_step_combines_then_builds_both_reports_and_prints_the_table(tmp_path: Path) -> None:
    done, calls = _run_step(tmp_path, REPORT_TEXT="the per-file table", XML_TEXT="<coverage/>")
    assert done.returncode == 0, done.stderr
    assert "the per-file table" in done.stdout
    assert calls[0] == "coverage combine"
    (script_call,) = [call for call in calls if call.startswith("python3 ")]
    argv = script_call.split()
    assert (ROOT / argv[1]).is_file() and (ROOT / argv[1]).samefile(SCRIPT)
    assert argv[argv.index("--xml") + 1] == "coverage.xml"
    assert calls.index(script_call) > 0


def test_the_step_fails_with_the_scripts_status_and_still_prints_the_table(tmp_path: Path) -> None:
    done, _ = _run_step(
        tmp_path, REPORT_TEXT="FAILED: chunk 2", XML_TEXT="<coverage/>", SCRIPT_RC="3"
    )
    assert done.returncode == 3
    assert "FAILED: chunk 2" in done.stdout
    assert "::error::" in done.stdout


def test_the_step_fails_when_the_script_wrote_no_xml(tmp_path: Path) -> None:
    done, _ = _run_step(tmp_path, REPORT_TEXT="table")
    assert done.returncode != 0
    assert "::error::" in done.stdout


def test_the_step_fails_when_combine_fails_and_builds_no_report(tmp_path: Path) -> None:
    done, calls = _run_step(tmp_path, COMBINE_RC="5", XML_TEXT="<coverage/>")
    assert done.returncode != 0
    assert calls == ["coverage combine"]


@pytest.mark.parametrize("missing", ["setup.cfg", ".coverage.ipv6", ".coverage.kernel-lock-owner"])
def test_the_step_refuses_to_start_without_its_inputs(tmp_path: Path, missing: str) -> None:
    present = [
        n for n in ("setup.cfg", ".coverage.ipv6", ".coverage.kernel-lock-owner") if n != missing
    ]
    done, calls = _run_step(tmp_path, present=present, XML_TEXT="<coverage/>")
    assert done.returncode != 0
    assert calls == []
    assert "::error::" in done.stdout
