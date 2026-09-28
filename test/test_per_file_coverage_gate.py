"""Unit tests for scripts/check_per_file_coverage.py.

Covers the three baseline rules (new offender / regression / graduation), the
warn-only stale-entry path, the Cobertura reader's merge behavior, and the CLI
contract CI depends on (exit codes and the ``--update-baseline`` round trip).
"""

from __future__ import annotations

import builtins
import importlib.util
import os
import subprocess
import sys
import types

import pytest

# Two tests spawn a real child interpreter to pin the CLI contract; pin the
# module to a dedicated xdist worker so concurrent cold-starts under -n auto
# don't starve each other. Requires --dist loadgroup.
pytestmark = pytest.mark.xdist_group(name="subprocess_spawn")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT_PATH = os.path.join(_REPO_ROOT, "scripts", "check_per_file_coverage.py")


def _load_module() -> types.ModuleType:
    """Import the gate by path: scripts/ is not a package on sys.path.

    The module must be registered in ``sys.modules`` BEFORE ``exec_module``:
    ``@dataclass`` resolves its own module through ``sys.modules[cls.__module__]``
    while processing the class body, and an unregistered module makes that
    lookup return ``None``.
    """
    spec = importlib.util.spec_from_file_location("check_per_file_coverage", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_module()


def _cobertura(entries: list[tuple[str, int, int]]) -> str:
    """Render a minimal Cobertura document: (filename, statements, covered)."""
    classes = []
    for name, stmts, covered in entries:
        lines = "".join(
            f'<line number="{i + 1}" hits="{1 if i < covered else 0}"/>' for i in range(stmts)
        )
        classes.append(f'<class name="{name}" filename="{name}"><lines>{lines}</lines></class>')
    return (
        '<?xml version="1.0" ?><coverage line-rate="0.5"><packages><package>'
        f"<classes>{''.join(classes)}</classes></package></packages></coverage>"
    )


def _cobertura_lines(entries: list[tuple[str, list[tuple[int, int]]]]) -> str:
    """Render Cobertura with explicit (line number, hits) pairs per class."""
    classes = []
    for name, lines in entries:
        rendered = "".join(f'<line number="{n}" hits="{h}"/>' for n, h in lines)
        classes.append(f'<class name="{name}" filename="{name}"><lines>{rendered}</lines></class>')
    return (
        '<?xml version="1.0" ?><coverage line-rate="0.5"><packages><package>'
        f"<classes>{''.join(classes)}</classes></package></packages></coverage>"
    )


def _write_report(tmp_path, entries: list[tuple[str, int, int]]) -> str:
    path = os.path.join(str(tmp_path), "coverage.xml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(_cobertura(entries))
    return path


def _write_line_report(tmp_path, entries, name="coverage.xml") -> str:
    path = os.path.join(str(tmp_path), name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(_cobertura_lines(entries))
    return path


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def test_parse_report_reads_rates(tmp_path) -> None:
    report = _write_report(tmp_path, [("a.py", 10, 9), ("b.py", 4, 1)])
    files = {f.path: f for f in gate.parse_report(gate.Path(report))}
    assert files["a.py"].rate == pytest.approx(90.0)
    assert files["b.py"].rate == pytest.approx(25.0)


def test_parse_report_merges_duplicates_by_line_identity(tmp_path) -> None:
    """A line present in both occurrences must count ONCE, covered if either hit it."""
    report = _write_line_report(
        tmp_path,
        [
            ("a.py", [(n, 1) for n in range(1, 9)] + [(n, 0) for n in range(9, 11)]),
            ("a.py", [(n, 0) for n in range(1, 3)] + [(n, 0) for n in range(3, 11)]),
        ],
    )
    files = gate.parse_report(gate.Path(report))
    assert len(files) == 1
    # Ten distinct lines, eight of them hit by the first occurrence. Summing the
    # two occurrences instead would report 20 statements and 8 covered (40%).
    assert (files[0].statements, files[0].covered) == (10, 8)


def test_duplicate_classes_cannot_inflate_a_rate_over_the_floor(tmp_path) -> None:
    """The fail-open case: overlapping duplicates must not push a bare file past the floor.

    Truth is 4 covered of 6 distinct lines (66.7%). Count-summing would report
    8/10 = 80% and pass an unlisted file that belongs in ``new_offenders``.
    """
    report = _write_line_report(
        tmp_path,
        [
            ("new.py", [(1, 1), (2, 1), (3, 1), (4, 1)]),
            ("new.py", [(1, 1), (2, 1), (3, 1), (4, 1), (5, 0), (6, 0)]),
        ],
    )
    files = gate.parse_report(gate.Path(report))
    assert (files[0].statements, files[0].covered) == (6, 4)
    assert files[0].rate == pytest.approx(200 / 3)

    verdicts = gate.evaluate(files, {}, floor=80.0)
    assert [f.path for f in verdicts.new_offenders] == ["new.py"]
    assert verdicts.failed


def test_unnumbered_lines_still_count_as_statements(tmp_path) -> None:
    path = os.path.join(str(tmp_path), "coverage.xml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            '<?xml version="1.0" ?><coverage line-rate="0"><packages><package><classes>'
            '<class name="a.py" filename="a.py"><lines>'
            '<line hits="0"/><line hits="0"/><line number="3" hits="1"/>'
            "</lines></class></classes></package></packages></coverage>"
        )
    files = gate.parse_report(gate.Path(path))
    assert (files[0].statements, files[0].covered) == (3, 1)


def test_statement_free_file_is_vacuously_full() -> None:
    assert gate.FileCoverage("empty.py", 0, 0).rate == 100.0


# ---------------------------------------------------------------------------
# The three baseline rules
# ---------------------------------------------------------------------------


def test_unlisted_file_below_floor_fails() -> None:
    verdicts = gate.evaluate([gate.FileCoverage("new.py", 100, 50)], {}, floor=80.0)
    assert [f.path for f in verdicts.new_offenders] == ["new.py"]
    assert verdicts.failed


def test_baselined_file_below_floor_is_exempt() -> None:
    verdicts = gate.evaluate([gate.FileCoverage("bare.py", 100, 42)], {"bare.py": 42.0}, floor=80.0)
    assert not verdicts.failed
    assert verdicts.exempt == 1


def test_baselined_file_may_drift_within_tolerance() -> None:
    drifted = 42.0 - gate.REGRESSION_TOLERANCE_PP + 0.5
    verdicts = gate.evaluate(
        [gate.FileCoverage("bare.py", 1000, int(drifted * 10))], {"bare.py": 42.0}, floor=80.0
    )
    assert not verdicts.failed


def test_baselined_file_past_tolerance_is_a_regression() -> None:
    dropped = 42.0 - gate.REGRESSION_TOLERANCE_PP - 1.0
    verdicts = gate.evaluate(
        [gate.FileCoverage("bare.py", 1000, int(dropped * 10))], {"bare.py": 42.0}, floor=80.0
    )
    assert [f.path for f, _ in verdicts.regressed] == ["bare.py"]
    assert verdicts.regressed[0][1] == pytest.approx(42.0)
    assert verdicts.failed


def test_graduated_file_must_leave_the_baseline() -> None:
    """The ratchet: clearing the floor by the noise band while still listed is a failure."""
    verdicts = gate.evaluate(
        [gate.FileCoverage("fixed.py", 100, 85)], {"fixed.py": 40.0}, floor=80.0
    )
    assert [f.path for f in verdicts.graduated] == ["fixed.py"]
    assert verdicts.failed


def test_graduation_inside_the_noise_band_does_not_fire() -> None:
    """Without an upward band, floor-adjacent noise oscillates a file between two failures.

    A file recorded just under the floor crosses it on shard noise, graduation
    fails for someone who never touched it, the refresh drops it, and the next
    run puts it back under -- where it fails as a new offender instead.
    """
    edge = 80.0 + gate.REGRESSION_TOLERANCE_PP - 0.5
    verdicts = gate.evaluate(
        [gate.FileCoverage("edge.py", 1000, int(edge * 10))], {"edge.py": 79.0}, floor=80.0
    )
    assert verdicts.graduated == []
    assert not verdicts.failed
    assert verdicts.passing == 1  # over the floor, so it passes rather than being exempt


def test_stale_entry_is_reported_but_does_not_fail() -> None:
    """A baselined file legitimately vanishes when deleted, renamed, or unmeasured."""
    verdicts = gate.evaluate([gate.FileCoverage("a.py", 10, 10)], {"gone.py": 10.0}, floor=80.0)
    assert verdicts.stale == ["gone.py"]
    assert not verdicts.failed


# ---------------------------------------------------------------------------
# Baseline I/O
# ---------------------------------------------------------------------------


def test_seed_baseline_round_trip(tmp_path) -> None:
    path = gate.Path(str(tmp_path)) / "nested" / "baseline.txt"
    files = [gate.FileCoverage("bare.py", 100, 40), gate.FileCoverage("ok.py", 100, 95)]
    assert gate.seed_baseline(path, files, floor=80.0) == 1
    assert gate.read_baseline(path) == {"bare.py": pytest.approx(40.0)}


def test_prune_drops_graduated_and_vanished_entries(tmp_path) -> None:
    path = gate.Path(str(tmp_path)) / "baseline.txt"
    gate.seed_baseline(
        path,
        [
            gate.FileCoverage("bare.py", 100, 40),
            gate.FileCoverage("fixed.py", 100, 40),
            gate.FileCoverage("gone.py", 100, 10),
        ],
        floor=80.0,
    )
    # bare.py stays below, fixed.py graduates clear of the band, gone.py vanishes.
    files = [gate.FileCoverage("bare.py", 100, 41), gate.FileCoverage("fixed.py", 100, 90)]
    kept, refused = gate.prune_baseline(path, files, gate.read_baseline_lines(path), floor=80.0)

    assert kept == 1
    assert refused == []
    assert set(gate.read_baseline(path)) == {"bare.py"}


def test_prune_copies_a_surviving_line_verbatim(tmp_path) -> None:
    """A rewritten survivor would pair its recorded rate with today's fraction."""
    path = gate.Path(str(tmp_path)) / "baseline.txt"
    # Trailing whitespace on purpose: "verbatim" has to survive a hand-edited file,
    # or the refresh shows an untouched entry as rewritten in the baseline diff.
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(gate.BASELINE_HEADER + "bare.py 60.0   # 60/100   \nfixed.py 40.0\n")
    before = [ln for ln in path.read_text(encoding="utf-8").splitlines() if "bare.py" in ln]

    # bare.py improves but stays under the floor; fixed.py graduates and is dropped.
    gate.prune_baseline(
        path,
        [gate.FileCoverage("bare.py", 100, 75), gate.FileCoverage("fixed.py", 100, 95)],
        gate.read_baseline_lines(path),
        floor=80.0,
    )
    after = [ln for ln in path.read_text(encoding="utf-8").splitlines() if "bare.py" in ln]

    assert after == before, "the surviving line must be byte-identical, not re-rendered"
    assert after[0].endswith("   "), "even trailing whitespace is preserved"
    assert "# 60/100" in after[0]  # the recorded snapshot, not today's 75/100
    assert "fixed.py" not in path.read_text(encoding="utf-8")


def test_prune_writes_atomically(tmp_path, monkeypatch) -> None:
    """A truncate-then-write refresh loses high-water marks nothing else stores."""
    path = gate.Path(str(tmp_path)) / "baseline.txt"
    gate.seed_baseline(path, [gate.FileCoverage("bare.py", 100, 40)], floor=80.0)
    original = path.read_text(encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(gate.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        gate.prune_baseline(
            path,
            [gate.FileCoverage("bare.py", 100, 40)],
            gate.read_baseline_lines(path),
            floor=80.0,
        )

    assert path.read_text(encoding="utf-8") == original, "the live baseline survives a failed write"
    leftovers = [p.name for p in path.parent.iterdir() if p.name != path.name]
    assert leftovers == [], f"the temp file must not be left behind: {leftovers}"


def test_parsing_refuses_to_fall_back_to_the_stdlib_xml_parser(tmp_path, monkeypatch) -> None:
    """The stdlib parser resolves external entities; a fork PR controls the report.

    Verified end to end on a bare interpreter too: `--test` still passes without
    defusedxml, and parsing without it exits 1 with a `::error::` annotation.
    """
    report = _write_report(tmp_path, [("a.py", 10, 10)])
    real_import = builtins.__import__

    def no_defusedxml(name, *args, **kwargs):
        if name.startswith("defusedxml"):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_defusedxml)
    with pytest.raises(SystemExit) as excinfo:
        gate.parse_report(gate.Path(report))
    assert excinfo.value.code == 1


def test_prune_preserves_an_operator_comment(tmp_path) -> None:
    """A refresh must not delete a human's note: it is context no tool can rebuild."""
    path = gate.Path(str(tmp_path)) / "baseline.txt"
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            gate.BASELINE_HEADER
            + "# blocked on the upstream fixture rewrite, see #1234\n"
            + "bare.py 40.0   # 40/100\n"
            + "\n"
            + "fixed.py 40.0   # 40/100\n"
        )
    kept, _ = gate.prune_baseline(
        path,
        [gate.FileCoverage("bare.py", 100, 40), gate.FileCoverage("fixed.py", 100, 95)],
        gate.read_baseline_lines(path),
        floor=80.0,
    )
    written = path.read_text(encoding="utf-8")

    assert kept == 1
    assert "# blocked on the upstream fixture rewrite, see #1234" in written
    assert "bare.py" in written
    assert "fixed.py" not in written  # graduated, so the entry line is gone
    assert written.count("Per-file coverage baseline") == 1, "the header is not duplicated"


def test_prune_keeps_the_header_when_every_entry_graduates(tmp_path) -> None:
    """An emptied baseline must stay self-documenting rather than become 0 bytes."""
    path = gate.Path(str(tmp_path)) / "baseline.txt"
    gate.seed_baseline(path, [gate.FileCoverage("fixed.py", 100, 40)], floor=80.0)
    kept, _ = gate.prune_baseline(
        path, [gate.FileCoverage("fixed.py", 100, 95)], gate.read_baseline_lines(path), floor=80.0
    )
    written = path.read_text(encoding="utf-8")
    assert kept == 0
    assert "Per-file coverage baseline" in written
    assert gate.read_baseline(path) == {}


def test_prune_refuses_a_report_from_the_other_lane(tmp_path) -> None:
    """Zero path overlap is a mismatched report, and the baseline must survive it.

    Both lanes' artifacts contain a file named `*.xml`, so pointing the refresh at
    the wrong one is an easy mistake. Every entry would read as vanished, the file
    would be rewritten empty, and only then would the command report a problem --
    destroying high-water marks nothing else stores.
    """
    path = gate.Path(str(tmp_path)) / "baseline.txt"
    gate.seed_baseline(
        path,
        [
            gate.FileCoverage("src/api/pins.ts", 100, 40),
            gate.FileCoverage("src/api/other.ts", 100, 30),
        ],
        floor=80.0,
    )
    before = path.read_text(encoding="utf-8")

    other_lane = [gate.FileCoverage("src/kiro_crew/history.py", 100, 10)]
    with pytest.raises(ValueError, match="other lane's coverage report"):
        gate.prune_baseline(path, other_lane, gate.read_baseline_lines(path), floor=80.0)

    assert path.read_text(encoding="utf-8") == before, "the baseline must be untouched"


def test_cli_reports_a_mismatched_lane_report_as_an_annotation(tmp_path) -> None:
    baseline = os.path.join(str(tmp_path), "baseline.txt")
    seed = _write_report(tmp_path, [("src/api/pins.ts", 100, 40)])
    assert _run(seed, "--floor", "80", "--baseline", baseline, "--seed-baseline").returncode == 0
    with open(baseline, encoding="utf-8") as handle:
        before = handle.read()

    wrong = _write_line_report(
        tmp_path,
        [("src/kiro_crew/history.py", [(n, 0) for n in range(1, 11)])],
        name="wrong.xml",
    )
    result = _run(wrong, "--floor", "80", "--baseline", baseline, "--update-baseline")
    assert result.returncode == 1
    assert "::error::" in result.stdout
    assert "other lane's coverage report" in result.stdout
    assert "nothing was changed" in result.stdout
    with open(baseline, encoding="utf-8") as handle:
        assert handle.read() == before


def test_prune_refuses_to_add_an_unlisted_offender(tmp_path) -> None:
    """Otherwise the documented remediation for a graduation launders a new offender."""
    path = gate.Path(str(tmp_path)) / "baseline.txt"
    gate.seed_baseline(path, [gate.FileCoverage("bare.py", 100, 40)], floor=80.0)

    files = [gate.FileCoverage("bare.py", 100, 40), gate.FileCoverage("sneaky.py", 100, 10)]
    kept, refused = gate.prune_baseline(path, files, gate.read_baseline_lines(path), floor=80.0)

    assert kept == 1
    assert [f.path for f in refused] == ["sneaky.py"]
    assert "sneaky.py" not in gate.read_baseline(path)


def test_prune_never_rewrites_a_recorded_rate(tmp_path) -> None:
    """Re-recording would let a slide be cleared by refreshing instead of by fixing."""
    path = gate.Path(str(tmp_path)) / "baseline.txt"
    gate.seed_baseline(path, [gate.FileCoverage("bare.py", 100, 60)], floor=80.0)
    assert gate.read_baseline(path) == {"bare.py": pytest.approx(60.0)}

    slid = [gate.FileCoverage("bare.py", 100, 30)]
    gate.prune_baseline(path, slid, gate.read_baseline_lines(path), floor=80.0)

    assert gate.read_baseline(path) == {"bare.py": pytest.approx(60.0)}
    # ...so the regression is still visible to the gate afterwards.
    verdicts = gate.evaluate(slid, gate.read_baseline(path), floor=80.0)
    assert [f.path for f, _ in verdicts.regressed] == ["bare.py"]


def test_prune_keeps_a_file_inside_the_graduation_band(tmp_path) -> None:
    """evaluate() and prune() must agree, or the band file oscillates between them."""
    path = gate.Path(str(tmp_path)) / "baseline.txt"
    gate.seed_baseline(path, [gate.FileCoverage("edge.py", 100, 60)], floor=80.0)
    inside = [gate.FileCoverage("edge.py", 1000, int((80.0 + 0.5) * 10))]

    kept, _ = gate.prune_baseline(path, inside, gate.read_baseline_lines(path), floor=80.0)
    assert kept == 1  # over the floor but inside the band: still listed
    assert gate.evaluate(inside, gate.read_baseline(path), floor=80.0).graduated == []


def test_baseline_is_written_with_lf_endings(tmp_path) -> None:
    """Pinned so a Windows checkout cannot turn an unrelated PR into a whole-file diff."""
    path = gate.Path(str(tmp_path)) / "baseline.txt"
    gate.seed_baseline(path, [gate.FileCoverage("bare.py", 100, 40)], floor=80.0)
    with open(path, "rb") as handle:
        assert b"\r\n" not in handle.read()


def test_missing_baseline_means_every_file_must_pass(tmp_path) -> None:
    absent = gate.Path(str(tmp_path)) / "does-not-exist.txt"
    assert gate.read_baseline(absent) == {}


# ---------------------------------------------------------------------------
# Exact contract boundaries
#
# Each of these fails if the comparison at that boundary is flipped, which the
# away-from-boundary cases above do not: `>=` -> `>` at the floor, `>= floor+tol`
# -> `>` for graduation, and `<` -> `<=` for regression all survive a test that
# only probes the middle of a band.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The tolerance scales with file size
# ---------------------------------------------------------------------------


def test_tolerance_never_finer_than_one_statement() -> None:
    assert gate.tolerance_for(1000) == pytest.approx(gate.REGRESSION_TOLERANCE_PP)
    assert gate.tolerance_for(19) == pytest.approx(100.0 / 19)  # 5.26pp
    assert gate.tolerance_for(4) == pytest.approx(25.0)
    assert gate.tolerance_for(0) == pytest.approx(gate.REGRESSION_TOLERANCE_PP)


def test_dilution_by_added_uncovered_lines_is_not_a_regression() -> None:
    """The real case from this gate's first CI run, reproduced exactly.

    src/components/appstore/SourcesPopover.tsx was baselined at 31.6% (6/19). An
    unrelated commit added two uncovered lines: 6/21 = 28.6%. The covered count
    never moved, so nothing was lost — but a flat 2pp band called it lost coverage,
    because a 19-statement file cannot change by less than 5.26pp.
    """
    diluted = [gate.FileCoverage("SourcesPopover.tsx", 21, 6)]
    verdicts = gate.evaluate(diluted, {"SourcesPopover.tsx": 31.6}, floor=80.0)
    assert verdicts.regressed == []
    assert verdicts.exempt == 1
    assert not verdicts.failed


def test_losing_a_covered_line_in_a_small_file_is_still_a_regression() -> None:
    """The scaled band must not swallow a real loss: 6/19 -> 5/19 is -5.3pp."""
    lost = [gate.FileCoverage("SourcesPopover.tsx", 19, 5)]
    verdicts = gate.evaluate(lost, {"SourcesPopover.tsx": 31.6}, floor=80.0)
    assert [f.path for f, _ in verdicts.regressed] == ["SourcesPopover.tsx"]
    assert verdicts.failed


def test_prune_uses_the_same_scaled_band_as_evaluate(tmp_path) -> None:
    """If prune and evaluate disagreed on the band, a file would oscillate between them."""
    path = gate.Path(str(tmp_path)) / "baseline.txt"
    gate.seed_baseline(path, [gate.FileCoverage("small.ts", 19, 6)], floor=80.0)
    # 16/19 = 84.2%: over the floor, but inside a 5.26pp band, so NOT graduated.
    inside = [gate.FileCoverage("small.ts", 19, 16)]
    kept, _ = gate.prune_baseline(path, inside, gate.read_baseline_lines(path), floor=80.0)
    assert kept == 1
    assert gate.evaluate(inside, gate.read_baseline(path), floor=80.0).graduated == []


def test_exactly_the_floor_passes(tmp_path) -> None:
    """80.0% is at the floor, so it passes: the comparison is `>=`, not `>`."""
    verdicts = gate.evaluate([gate.FileCoverage("ok.py", 10, 8)], {}, floor=80.0)
    assert verdicts.new_offenders == []
    assert verdicts.passing == 1
    assert not verdicts.failed


def test_a_hair_under_the_floor_fails() -> None:
    verdicts = gate.evaluate([gate.FileCoverage("bare.py", 1000, 799)], {}, floor=80.0)
    assert [f.path for f in verdicts.new_offenders] == ["bare.py"]


def test_exactly_the_graduation_band_edge_graduates() -> None:
    """floor + tolerance graduates: the comparison is `>=`, not `>`."""
    edge = int((80.0 + gate.REGRESSION_TOLERANCE_PP) * 10)
    verdicts = gate.evaluate(
        [gate.FileCoverage("fixed.py", 1000, edge)], {"fixed.py": 60.0}, floor=80.0
    )
    assert [f.path for f in verdicts.graduated] == ["fixed.py"]


def test_a_hair_under_the_graduation_edge_does_not_graduate() -> None:
    edge = int((80.0 + gate.REGRESSION_TOLERANCE_PP) * 10) - 1
    verdicts = gate.evaluate(
        [gate.FileCoverage("edge.py", 1000, edge)], {"edge.py": 60.0}, floor=80.0
    )
    assert verdicts.graduated == []
    assert not verdicts.failed


def test_a_drop_of_exactly_the_tolerance_is_allowed() -> None:
    """recorded - tolerance is tolerated: the comparison is `<`, not `<=`."""
    dropped = int((42.0 - gate.REGRESSION_TOLERANCE_PP) * 10)
    verdicts = gate.evaluate(
        [gate.FileCoverage("bare.py", 1000, dropped)], {"bare.py": 42.0}, floor=80.0
    )
    assert verdicts.regressed == []
    assert verdicts.exempt == 1


def test_a_drop_one_step_past_the_tolerance_is_a_regression() -> None:
    dropped = int((42.0 - gate.REGRESSION_TOLERANCE_PP) * 10) - 1
    verdicts = gate.evaluate(
        [gate.FileCoverage("bare.py", 1000, dropped)], {"bare.py": 42.0}, floor=80.0
    )
    assert [f.path for f, _ in verdicts.regressed] == ["bare.py"]


# ---------------------------------------------------------------------------
# Malformed recorded rates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "-1", "101", "NaN"])
def test_a_non_percentage_recorded_rate_is_refused(tmp_path, bad: str) -> None:
    """`nan` compares false against everything, so it would exempt a file at 0%."""
    path = gate.Path(str(tmp_path)) / "baseline.txt"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"bare.py {bad}\n")
    with pytest.raises(ValueError):
        gate.read_baseline(path)


def test_a_nan_baseline_cannot_exempt_a_zero_coverage_file(tmp_path) -> None:
    """The end-to-end consequence: refused at parse, so it never reaches evaluate()."""
    report = _write_report(tmp_path, [("bare.py", 100, 0)])
    baseline = os.path.join(str(tmp_path), "baseline.txt")
    with open(baseline, "w", encoding="utf-8") as handle:
        handle.write("bare.py nan\n")
    result = _run(report, "--floor", "80", "--baseline", baseline)
    assert result.returncode == 1
    assert "::error::" in result.stdout
    assert "percentage in [0, 100]" in result.stdout
    assert "Traceback" not in result.stderr


def test_malformed_baseline_line_raises(tmp_path) -> None:
    path = gate.Path(str(tmp_path)) / "baseline.txt"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("only-a-path-no-rate\n")
    with pytest.raises(ValueError, match="malformed baseline line"):
        gate.read_baseline(path)


def test_comments_and_blank_lines_are_ignored(tmp_path) -> None:
    path = gate.Path(str(tmp_path)) / "baseline.txt"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("# header\n\nbare.py 40.0   # 40/100\n")
    assert gate.read_baseline(path) == {"bare.py": pytest.approx(40.0)}


# ---------------------------------------------------------------------------
# CLI contract (what ci.yml actually invokes)
# ---------------------------------------------------------------------------


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, _SCRIPT_PATH, *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_self_test_passes() -> None:
    result = _run("--test")
    assert result.returncode == 0, result.stdout + result.stderr


def test_cli_seed_then_enforce_is_green(tmp_path) -> None:
    report = _write_report(tmp_path, [("bare.py", 100, 40), ("ok.py", 100, 95)])
    baseline = os.path.join(str(tmp_path), "baseline.txt")
    common = [report, "--label", "Test", "--floor", "80", "--baseline", baseline]

    seeded = _run(*common, "--seed-baseline")
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr

    enforced = _run(*common)
    assert enforced.returncode == 0, enforced.stdout + enforced.stderr
    assert "1 baselined" in enforced.stdout


def test_cli_refresh_refuses_to_absorb_a_new_offender(tmp_path) -> None:
    """The documented remediation for a graduation must not launder a new offender."""
    baseline = os.path.join(str(tmp_path), "baseline.txt")
    seed = _write_report(tmp_path, [("bare.py", 100, 40)])
    assert _run(seed, "--floor", "80", "--baseline", baseline, "--seed-baseline").returncode == 0

    both = _write_report(tmp_path, [("bare.py", 100, 40), ("sneaky.py", 100, 10)])
    refreshed = _run(both, "--floor", "80", "--baseline", baseline, "--update-baseline")
    assert refreshed.returncode == 1
    assert "refused to add" in refreshed.stdout
    assert "sneaky.py" in refreshed.stdout
    with open(baseline, encoding="utf-8") as handle:
        assert "sneaky.py" not in handle.read()


def test_cli_seed_refuses_to_overwrite_an_existing_baseline(tmp_path) -> None:
    """Reseeding a live lane would replace its high-water marks and launder regressions."""
    baseline = os.path.join(str(tmp_path), "baseline.txt")
    seed = _write_report(tmp_path, [("bare.py", 100, 60)])
    assert _run(seed, "--floor", "80", "--baseline", baseline, "--seed-baseline").returncode == 0
    with open(baseline, encoding="utf-8") as handle:
        before = handle.read()

    slid = _write_line_report(
        tmp_path,
        [("bare.py", [(n, 1) for n in range(1, 31)] + [(n, 0) for n in range(31, 101)])],
        name="slid.xml",
    )
    result = _run(slid, "--floor", "80", "--baseline", baseline, "--seed-baseline")
    assert result.returncode == 1
    assert "create-only" in result.stdout
    with open(baseline, encoding="utf-8") as handle:
        assert handle.read() == before  # the 60.0 high-water mark survives

    # ...so the slide is still caught on the next enforcement.
    enforced = _run(slid, "--floor", "80", "--baseline", baseline)
    assert enforced.returncode == 1
    assert "lost coverage" in enforced.stdout


def test_graduation_failure_prints_an_executable_refresh_recipe(tmp_path) -> None:
    """A graduation fails for someone who may never have touched the file.

    "Rerun with --update-baseline" is not runnable on its own: the refresh needs
    the CI-combined report, and a locally generated one has different rates. The
    message must name the artifact and warn against a local report.
    """
    baseline = os.path.join(str(tmp_path), "baseline.txt")
    seed = _write_report(tmp_path, [("fixed.py", 100, 40)])
    assert _run(seed, "--floor", "80", "--baseline", baseline, "--seed-baseline").returncode == 0

    lifted = _write_line_report(
        tmp_path,
        [("fixed.py", [(n, 1) for n in range(1, 96)] + [(n, 0) for n in range(96, 101)])],
        name="lifted.xml",
    )
    result = _run(lifted, "--label", "Frontend", "--floor", "80", "--baseline", baseline)
    assert result.returncode == 1
    assert "now meet the floor" in result.stdout
    assert "gh run download" in result.stdout
    assert "coverage-frontend" in result.stdout  # the lane's real artifact name
    assert "not a local coverage run" in result.stdout
    assert "only DELETES entries" in result.stdout


def test_cli_rejects_both_baseline_write_modes(tmp_path) -> None:
    report = _write_report(tmp_path, [("a.py", 10, 10)])
    result = _run(
        report,
        "--baseline",
        os.path.join(str(tmp_path), "baseline.txt"),
        "--update-baseline",
        "--seed-baseline",
    )
    assert result.returncode == 2
    assert "mutually exclusive" in result.stderr


def test_cli_reports_a_malformed_baseline_as_an_annotation(tmp_path) -> None:
    """Every other failure path emits ::error::; a hand-edit typo must too, not a traceback."""
    report = _write_report(tmp_path, [("a.py", 10, 10)])
    baseline = os.path.join(str(tmp_path), "baseline.txt")
    with open(baseline, "w", encoding="utf-8") as handle:
        handle.write("only-a-path-no-rate\n")
    result = _run(report, "--floor", "80", "--baseline", baseline)
    assert result.returncode == 1
    assert "::error::" in result.stdout
    assert "malformed baseline line" in result.stdout
    assert "Traceback" not in result.stderr


def test_cli_fails_on_a_new_offender(tmp_path) -> None:
    report = _write_report(tmp_path, [("new.py", 100, 10)])
    baseline = os.path.join(str(tmp_path), "baseline.txt")
    result = _run(report, "--label", "Test", "--floor", "80", "--baseline", baseline)
    assert result.returncode == 1
    assert "not baselined" in result.stdout
    assert "new.py" in result.stdout


def test_cli_rejects_a_missing_report(tmp_path) -> None:
    result = _run(
        os.path.join(str(tmp_path), "absent.xml"),
        "--baseline",
        os.path.join(str(tmp_path), "baseline.txt"),
    )
    assert result.returncode == 1
    assert "coverage report not found" in result.stdout


def test_cli_requires_a_baseline_unless_self_testing(tmp_path) -> None:
    report = _write_report(tmp_path, [("a.py", 10, 10)])
    result = _run(report)
    assert result.returncode == 2  # argparse usage error
    assert "--baseline" in result.stderr
