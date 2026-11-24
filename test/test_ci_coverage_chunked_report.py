"""One analysis gives the same ``coverage.xml`` and the same table, and fails closed.

``scripts/ci/coverage_chunked_report.py`` sits between coverage.py and the
Coverage Gate: the XML it merges is the file the gate's floors are read from,
and the per-file table in the job log is derived from that XML. Three things
are pinned here.

* The merged XML is byte for byte what coverage.py's own XML reporter writes
  in one pass, for every chunk count, apart from the ``timestamp`` attribute.
* The derived table is byte for byte what coverage.py's own text reporter
  prints for the same data: every column, the Missing ranges and branch arcs,
  the widths and the TOTAL line.
* Fail closed. Every way a chunk or the table can be wrong ends the build with
  a named error and leaves no merged XML behind.

The reporters run in this process through coverage's API, so no interpreter is
started. The workflow half runs the Coverage Combine step's own body under bash
with shell functions standing in for ``coverage`` and ``python3``.
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
    "src/pkg/branchy.py": (
        "def grade(value):\n"
        "    # a line that is no statement\n"
        "    if value > 10:\n"
        "        label = 'high'\n"
        "    elif value > 5:\n"
        "        label = 'mid'\n"
        "    else:\n"
        "        label = 'low'\n"
        "\n"
        "    for step in range(value):\n"
        "        if step == 99:\n"
        "            return 'never'\n"
        "    while value:\n"
        "        value -= 1\n"
        "    return label\n"
        "\n"
        "\n"
        "RESULT = grade(7)\n"
        "UNUSED = [\n"
        "    1,\n"
        "    2,\n"
        "]\n"
    ),
    "src/pkg/clamp.py": (
        "def clamp(value):\n    if value < 0:\n        value = 0\n    return value\n\n\nLOW = clamp(-1)\n"
    ),
    "src/pkg/first.py": (
        "def first(items):\n    for item in items:\n        return item\n\n\nHEAD = first([1])\n"
    ),
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
    "src/pkg/branchy.py": [
        (-1, 1),
        (1, 18),
        (18, 19),
        (19, -1),
        (-1, 3),
        (3, 5),
        (5, 6),
        (6, 10),
        (10, 11),
        (11, 10),
        (10, 13),
        (13, 14),
        (14, 13),
        (13, 15),
        (15, -1),
    ],
    "src/pkg/clamp.py": [(-1, 1), (1, 7), (7, -1), (-1, 2), (2, 3), (3, 4), (4, -1)],
    "src/pkg/first.py": [(-1, 1), (1, 6), (6, -1), (-1, 2), (2, 3), (3, -1)],
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


def _build(tree: Path, chunks: int, runner=in_process, **kwargs) -> str:
    return chunked.build(
        _measured(),
        chunks,
        tree / "merged.xml",
        tree / "report.txt",
        tree / "work",
        runner=runner,
        options=chunked.report_options(_coverage().get_option),
        **kwargs,
    )


def _real_table() -> list[str]:
    """What coverage's own text reporter prints for the data in the cwd."""
    buffer = io.StringIO()
    _coverage().report(file=buffer)
    return buffer.getvalue().splitlines()


def _derived_table(tree: Path) -> list[str]:
    """The table in the report file: from its header line to its TOTAL line."""
    lines = (tree / "report.txt").read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("Name "))
    end = next(index for index, line in enumerate(lines) if line.startswith("TOTAL "))
    return lines[start : end + 1]


def _lines_only_data() -> None:
    Path(".coverage").unlink()
    data = coverage.CoverageData(basename=".coverage")
    data.add_lines(
        {
            name: sorted({line for arc in arcs for line in arc if line > 0})
            for name, arcs in ARCS.items()
        }
    )
    data.write()


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
    missing = "\n".join(_real_table())
    assert "2->4" in missing, "a branch never taken to a line that did run"
    assert "2->exit" in missing, "a branch never taken out of the function"
    assert re.search(r"\b\d+-\d+\b", missing), "a run of missed statements"


@pytest.mark.parametrize("chunks", range(1, len(SOURCES) + 1))
def test_the_merged_xml_is_what_one_coverage_xml_writes(tree: Path, chunks: int) -> None:
    _build(tree, chunks)
    assert _masked((tree / "merged.xml").read_text(encoding="utf-8")) == _masked(_single(tree))


@pytest.mark.parametrize("chunks", [1, 3, len(SOURCES)])
def test_the_derived_table_is_what_coverage_report_prints(tree: Path, chunks: int) -> None:
    total = _build(tree, chunks)
    real = _real_table()
    assert _derived_table(tree) == real
    assert total == real[-1]
    assert sorted(line.split()[0] for line in real if line.startswith("src/")) == FILES


@pytest.mark.parametrize("chunks", [1, 2, len(SOURCES)])
def test_data_without_branch_arcs_gives_what_coverage_writes_and_prints(
    tree: Path, chunks: int
) -> None:
    _lines_only_data()
    _build(tree, chunks)
    single = _single(tree)
    assert 'branch="true"' not in single
    assert _masked((tree / "merged.xml").read_text(encoding="utf-8")) == _masked(single)
    real = _real_table()
    assert "Branch" not in real[0]
    assert _derived_table(tree) == real


@pytest.mark.parametrize(
    "setting",
    [
        "show_missing = false\n",
        "show_missing = true\nprecision = 2\n",
        "show_missing = false\nprecision = 1\n",
        "show_missing = true\nsort = Name\n",
    ],
)
def test_the_derived_table_follows_the_report_settings(tree: Path, setting: str) -> None:
    base = CONFIG.replace("show_missing = true\n", "")
    (tree / "setup.cfg").write_text(base + setting, encoding="utf-8")
    _build(tree, 3)
    assert _derived_table(tree) == _real_table()


@pytest.mark.parametrize("unrun", ["src/pkg/branchy.py", "src/pkg/alpha.py", "src/other/delta.py"])
def test_the_derived_table_matches_when_a_whole_file_never_ran(tree: Path, unrun: str) -> None:
    Path(".coverage").unlink()
    data = coverage.CoverageData(basename=".coverage")
    data.add_arcs({name: arcs for name, arcs in ARCS.items() if arcs and name != unrun})
    for name, arcs in ARCS.items():
        if not arcs or name == unrun:
            data.touch_file(name)
    data.write()
    _build(tree, 2)
    assert _derived_table(tree) == _real_table()
    assert _masked((tree / "merged.xml").read_text(encoding="utf-8")) == _masked(_single(tree))


def test_the_report_says_the_table_is_derived_and_times_every_chunk(tree: Path) -> None:
    ticks = iter(range(0, 1000, 5))
    lock = threading.Lock()

    def clock() -> float:
        with lock:
            return float(next(ticks))

    _build(
        tree, 3, clock=clock, cpu_note="CPUs: 8 on the machine, 4 this process may use; 3 chunk(s)."
    )
    lines = (tree / "report.txt").read_text(encoding="utf-8").splitlines()
    assert "derived from the merged coverage.xml" in lines[0]
    assert "CPUs: 8 on the machine, 4 this process may use; 3 chunk(s)." in lines
    timed = [
        line
        for line in lines
        if re.search(r"^chunk \d of 3: .* coverage xml took \d+\.\d s$", line)
    ]
    assert len(timed) == 3
    assert re.search(
        r"^all chunks \d+\.\d s, merge, table and read-back \d+\.\d s, total \d+\.\d s$", lines[-1]
    )


def _entry(*lines: dict[str, str], counts=None) -> chunked.ClassReport:
    rows = tuple(tuple(line.items()) for line in lines)
    counted = counts or chunked._line_counts(rows, "t")
    return chunked.ClassReport("p", (("filename", "a.py"),), rows, counted)


def test_a_table_whose_totals_disagree_with_the_xml_is_refused() -> None:
    entry = _entry({"number": "1", "hits": "0"}, counts=(5, 5, 0, 0))
    with pytest.raises(
        chunked.ChunkError, match="the table's totals .* disagree with the merged XML's"
    ):
        chunked.render_table({"a.py": entry}, True, chunked.ReportOptions())


@pytest.mark.parametrize(
    ("line", "match"),
    [
        (
            {
                "number": "2",
                "hits": "1",
                "branch": "true",
                "condition-coverage": "50% (1/2)",
                "missing-branches": "4,6",
            },
            "misses 1 branch.es. and names 2",
        ),
        (
            {"number": "2", "hits": "1", "missing-branches": "4"},
            "names missing branches and is no branch",
        ),
        (
            {
                "number": "2",
                "hits": "1",
                "branch": "true",
                "condition-coverage": "50% (1/2)",
                "missing-branches": "x",
            },
            "unreadable branch target",
        ),
    ],
)
def test_a_line_whose_branch_attributes_disagree_is_refused(
    line: dict[str, str], match: str
) -> None:
    with pytest.raises(chunked.ChunkError, match=match):
        chunked.render_table({"a.py": _entry(line)}, True, chunked.ReportOptions())


@pytest.mark.parametrize(
    ("setting", "match"),
    [
        ("fail_under = 90\n", "fail_under is set"),
        ("skip_covered = true\n", "skip_covered is set"),
        ("skip_empty = true\n", "skip_empty is set"),
        ("sort = Cover\n", "sort is set"),
        ("sort = -name\n", "sort is set"),
        ("format = markdown\n", "format is set"),
    ],
)
def test_a_report_setting_the_table_cannot_honour_is_refused_before_anything_runs(
    tree: Path, capsys: pytest.CaptureFixture[str], setting: str, match: str
) -> None:
    (tree / "setup.cfg").write_text(CONFIG + setting, encoding="utf-8")
    assert chunked.main(["--xml", "merged.xml", "--report", "report.txt"]) == 1
    assert match in capsys.readouterr().err
    assert not (tree / "merged.xml").exists()
    assert not (tree / "work").exists() and not (tree / "coverage-chunks").exists()


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
    assert "chunk 1 of 3" in report and "FAILED: chunk 2" in report
    assert "Name " not in report, "no table is printed for a build that failed"


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
        second = argv[3] == "xml" and argv[5].endswith("chunk-2.xml")
        return in_process([*argv, FILES[0]] if second else argv, stdout)

    _fails(tree, runner, f"chunk 2 reports {FILES[0]}, which it was not dealt")


def test_a_measured_file_no_chunk_reports_stops_the_build(tree: Path) -> None:
    def runner(argv, stdout):
        if argv[3] == "xml" and FILES[-1] in argv:
            argv = [arg for arg in argv if arg != FILES[-1]]
        return in_process(argv, stdout)

    _fails(tree, runner, "in no chunk's report and coverage does not call them omitted")


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


def test_the_plan_depends_only_on_the_names_and_the_sizes() -> None:
    files = [f"src/f{index:05d}.py" for index in range(60)]
    weights = {name: 1 + (index * 53) % 17 for index, name in enumerate(files)}
    plan = chunked.plan_chunks(files, 4, weights)
    shuffled = files[30:] + files[:30]
    assert chunked.plan_chunks(shuffled, 4, dict(reversed(list(weights.items())))) == plan


def test_the_script_imports_nothing_the_combine_job_does_not_install() -> None:
    import ast
    import sys

    tree = ast.parse((ROOT / "scripts/ci/coverage_chunked_report.py").read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            roots.add(node.module.split(".")[0])
    third_party = roots - set(sys.stdlib_module_names)
    assert "xml" not in roots, "chunk reports are parsed through defusedxml"
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"]
    installs = " ".join(
        str(step.get("run", ""))
        for step in jobs["coverage-combine"]["steps"]
        if "pip install" in str(step.get("run", ""))
    )
    installed = {
        re.split(r"[=<>!~\[]", word.strip("'\""))[0]
        for word in installs.split()
        if not word.startswith("-")
    }
    assert third_party and third_party <= installed, sorted(third_party - installed)


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


def test_the_real_commands_never_ignore_errors(tree: Path) -> None:
    seen: list[list[str]] = []

    def runner(argv, stdout):
        seen.append(list(argv))
        return in_process(argv, stdout)

    _build(tree, 2, runner)
    assert {argv[3] for argv in seen} == {"xml"}, "one analysis: coverage report is not run"
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
