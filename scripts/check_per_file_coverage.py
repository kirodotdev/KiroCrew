#!/usr/bin/env python3
"""check_per_file_coverage.py — a per-file coverage floor with a shrinking baseline.

## Why per-file at all

The Coverage Gate compares one project-wide average against a floor. An average
lets a well-covered large file pay for a bare small one. Measured on ``main``
(backend 89.60%, frontend 88.53%), the subsidy is not theoretical:

* backend — 320 files sit below 90% owing 8,450 statements, while 555 files
  carry 7,553 statements of *surplus* above it. The project number is 897
  statements away from 90%, so that target is reachable without touching a
  single one of those 320 files.
* frontend — 62 files have never executed a single statement, and the largest
  hole in the lane (``src/pages/ChatPage.tsx``, 628 uncovered) is simultaneously
  one of the biggest *contributors* to passing the 60% project floor.

A per-file floor removes the subsidy: every file answers for itself, so the
average can no longer hide where the risk actually is.

## Why a baseline instead of a bare floor

A bare per-file 80% floor fails 125 backend and 263 frontend files the day it
lands, and lifting them all is roughly 7,700 statements of new tests. Gate that
work behind one PR and the gate never lands. So the floor applies to every file
*except* those recorded in a baseline, and the baseline can only shrink:

* a file **not** in the baseline must meet the floor — no new offenders land;
* a file **in** the baseline must not fall below its recorded rate (beyond
  ``REGRESSION_TOLERANCE_PP``) — a known-bare file cannot get barer;
* a file **in** the baseline that clears the floor **by that same band** must be
  **removed** from it — this is the ratchet, and it is what makes the list shrink
  rather than rot.

The third rule is the one that costs a contributor anything: incidentally
lifting a baselined file clear of the floor turns the gate red until the baseline
is refreshed. That is deliberate. Without it the list only ever grows stale, and
a stale list silently re-creates the averaging problem this gate exists to
remove. The failure prints the exact one-line command to fix it.

## Why refreshing prunes but never adds

``--update-baseline`` deletes graduated and vanished entries and copies every
surviving line through **verbatim**. It refuses to add a path, and because it
never rewrites a line it cannot re-record a rate either. Three properties follow,
each closing a hole that a naive "rewrite the file from the report" refresh opens:

* Adding would make the documented remediation for one verdict launder another.
  A graduation tells the reader to refresh; if that refresh also absorbed
  unlisted files, one command would exempt an under-tested new file forever, and
  "no new offenders land" would hold only while a human read every baseline diff.
* Re-recording would erase the high-water mark the regression rule compares
  against, so a slide could be cleared by refreshing instead of by fixing.
* Rewriting a survivor at all would pair its recorded rate with today's
  covered/statements, leaving a line whose rate and fraction disagree with
  nothing to say which is then and which is now.

Standing up a lane for the first time is the one operation that must add paths,
so it lives behind its own flag, ``--seed-baseline`` — which is **create-only**
for the same reason: reseeding a live lane would replace every recorded rate with
the current one, laundering exactly the regressions the split protects against.

## Why the graduation rule has a noise band too

Both directions are compared against the same band, not just the downward one.
Without a band on the upward side, a file recorded just under the floor crosses it
on ordinary shard noise, graduation fails the build for someone who never touched
that file, the refresh drops it from the baseline, and the next run's noise puts it
back under — where, now unlisted, it fails as a new offender instead. That
oscillation has no green state anyone can reach.

The band is per-file: ``max(REGRESSION_TOLERANCE_PP, one statement's worth)``. A
flat percentage is meaningless below a certain file size — a 19-statement component
has 5.26pp of resolution, so a 2pp band cannot absorb even one statement of change.
That is not hypothetical: this gate's first real CI run flagged a 19-statement file
that had grown by two uncovered lines on an unrelated commit, holding its covered
count at 6 while the rate fell 31.6% → 28.6%. Nothing was lost; the denominator
moved. Scaling the band by the file's own resolution absorbs dilution and still
catches a real regression, since losing one COVERED line moves more than one
statement's worth.

## Why a regression tolerance at all

Per-file rates are not bit-stable across runs: the backend lane deselects tests
that need capabilities GH Actions lacks, and the shard split is balanced by
recorded duration rather than by file. A file whose rate moves by a fraction of
a point between two green runs is noise, not a regression, and a zero-tolerance
comparison would charge that noise to whoever pushed next. Two points is wide
enough to absorb it and narrow enough that deleting a test is still caught.

## Why the floor is 80 and not 90

At 90% the retrofit is ~14,800 statements across both lanes; at 80% it is
~7,700, and 116 of the 388 failing files are near-misses in the 70–80% band
costing 838 statements between them. 80% buys most of the signal for half the
debt. Raise it the way the project floor is raised: only after ``main`` has held
the higher number, and never above the lowest recent measurement.

## Usage

    # enforce a lane (exit 1 on any violation)
    python3 scripts/check_per_file_coverage.py coverage.xml \\
        --label Backend --floor 80 --baseline .github/coverage-baselines/backend.txt

    # refresh a baseline after files graduate, are deleted, or are renamed
    python3 scripts/check_per_file_coverage.py coverage.xml \\
        --label Backend --floor 80 --baseline .github/coverage-baselines/backend.txt \\
        --update-baseline

    # stand up a NEW lane's baseline (create-only; the only mode that may add paths)
    python3 scripts/check_per_file_coverage.py coverage.xml \\
        --label Backend --floor 80 --baseline .github/coverage-baselines/backend.txt \\
        --seed-baseline

    # self-test: plant one probe per verdict, assert each fires
    python3 scripts/check_per_file_coverage.py --test

Both lanes emit Cobertura: the backend from ``coverage xml`` and the frontend
from vitest's cobertura reporter. Paths inside each report are relative to that
lane's root, so the two baselines never collide and are never interchangeable.
"""

from __future__ import annotations

import argparse
import contextlib
import math
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# The FLOOR of the per-file tolerance, in percentage points. See "Why a
# regression tolerance" above — this absorbs shard-split and deselection noise,
# not a deleted test. `tolerance_for()` raises it for small files.
REGRESSION_TOLERANCE_PP = 2.0


def tolerance_for(statements: int) -> float:
    """The effective tolerance for a file of this size, in percentage points.

    A rate cannot move by less than one statement, so a tolerance finer than that
    is not a tolerance — it is a guarantee of false positives. A 19-statement
    component has 5.26pp of resolution: growing by two uncovered lines drops it
    3.0pp with the covered count unchanged, which a flat 2pp band reports as lost
    coverage when nothing was lost. Small files dominate the frontend lane, so the
    flat band fired on ordinary dilution from an unrelated commit.

    Scaling by the file's own resolution keeps a real regression visible: that
    same file losing one COVERED line moves 5.26pp, which is not absorbed.
    """
    if statements <= 0:
        return REGRESSION_TOLERANCE_PP
    return max(REGRESSION_TOLERANCE_PP, 100.0 / statements)


# Rendered into every baseline written by --update-baseline, so the file explains
# itself to the next reader without them having to find this script first.
BASELINE_HEADER = """\
# Per-file coverage baseline — files below the floor when the gate landed.
#
# The gate (scripts/check_per_file_coverage.py) requires every measured file to
# meet the per-file floor EXCEPT the ones listed here. This list may only
# shrink. A refresh DELETES graduated and vanished entries and copies every
# surviving line through verbatim, so a recorded rate is never rewritten and no
# path can be added -- neither a new offender nor a regression can be cleared by
# refreshing instead of by adding tests. Each line therefore stays an accurate
# record of the moment it was taken.
#
# Refresh after a file graduates, is deleted, or is renamed:
#   python3 scripts/check_per_file_coverage.py <report> --label <lane> \\
#       --floor <n> --baseline <this file> --update-baseline
#
# Format: <path> <rate-when-recorded>   # <covered>/<statements>
"""


@dataclass(frozen=True)
class FileCoverage:
    """One measured file's line coverage, as reported by Cobertura."""

    path: str
    statements: int
    covered: int

    @property
    def rate(self) -> float:
        """Line coverage as a percentage.

        A file with no statements is vacuously full. Reachable only through direct
        construction: ``parse_report`` drops a statement-free ``<class>`` rather
        than admitting it, because a class with no lines carries no coverage signal
        and a per-file floor has nothing to say about it. Admitting them instead
        would pad every lane with phantom 100% rows.
        """
        if not self.statements:
            return 100.0
        return 100.0 * self.covered / self.statements


def _parse_xml(path: Path):  # type: ignore[no-untyped-def]
    """Parse a Cobertura report through defusedxml, never the stdlib parser.

    ``xml.etree`` resolves external entities, so a crafted report can mount an
    XXE -- a local-file read or an entity-expansion DoS -- and on a fork PR the
    report is produced by code the contributor controls. ``defusedxml`` is a
    declared dependency of this package for exactly this reason (see setup.cfg),
    and ``doc_parser.py`` states the rule the whole repo follows: never fall back
    to stdlib xml.

    Imported here rather than at module scope so ``--test`` -- which parses no
    XML -- keeps working under an interpreter that has no site-packages.
    """
    try:
        from defusedxml.ElementTree import parse as _xml_parse
    except ModuleNotFoundError:
        print(
            "::error::defusedxml is required to parse a coverage report "
            "(pip install 'defusedxml>=0.7,<1'). Refusing to fall back to the "
            "stdlib xml parser, which resolves external entities."
        )
        raise SystemExit(1) from None
    return _xml_parse(str(path))


def parse_report(path: Path) -> list[FileCoverage]:
    """Read per-file line coverage out of a Cobertura XML report.

    Both producers emit one ``<class>`` per source file, but neither guarantees
    uniqueness — coverage.py can split a file across shards and vitest can emit a
    file twice when it is reached through two entry points.

    Duplicates are therefore merged by LINE IDENTITY, not by count: a line counts
    as covered when any occurrence reports a hit, and the statement total is the
    number of distinct lines. Summing the two counts instead double-counts a line
    that is covered in one occurrence and merely present in both, which inflates
    the computed rate above the true one and can pass a file that is actually
    below the floor — the one failure mode a gate must not have.
    """
    # path -> line number -> covered by any occurrence
    merged: dict[str, dict[int, bool]] = {}
    # Cobertura always numbers its lines. A line without a number still counts as
    # a statement, so give it a key of its own rather than folding them together.
    unnumbered = 0
    for element in _parse_xml(path).getroot().iter("class"):
        name = element.attrib.get("filename") or element.attrib.get("name")
        if not name:
            continue
        lines = list(element.iter("line"))
        if not lines:
            continue
        by_line = merged.setdefault(name, {})
        for line in lines:
            raw = line.attrib.get("number")
            if raw is None:
                unnumbered -= 1
                number = unnumbered
            else:
                number = int(raw)
            hit = int(line.attrib.get("hits", "0")) > 0
            by_line[number] = by_line.get(number, False) or hit
    return [
        FileCoverage(path=name, statements=len(by_line), covered=sum(by_line.values()))
        for name, by_line in sorted(merged.items())
    ]


def _parse_entry(raw: str) -> tuple[str, float] | None:
    """Return ``(path, recorded rate)`` for an entry line, or ``None`` for a comment/blank.

    The rate is range-checked, not merely parsed. ``float()`` accepts ``nan`` and
    ``inf``, and a recorded ``nan`` silently disables the regression rule for that
    entry -- every comparison against it is false, so the file stays exempt at any
    coverage including zero. A negative rate has the same effect. Both are
    unreachable from ``seed_baseline`` and reachable from a hand edit, which is
    exactly the input this parser exists to validate.
    """
    line = raw.split("#", 1)[0].strip()
    if not line:
        return None
    fields = line.split()
    if len(fields) < 2:
        raise ValueError(f"malformed baseline line: {raw.strip()!r}")
    try:
        rate = float(fields[1])
    except ValueError:
        raise ValueError(f"malformed baseline rate: {raw.strip()!r}") from None
    if not math.isfinite(rate) or not 0.0 <= rate <= 100.0:
        raise ValueError(f"baseline rate must be a percentage in [0, 100]: {raw.strip()!r}")
    return fields[0], rate


def read_baseline_lines(path: Path) -> list[str]:
    """Return the baseline's raw lines, validating every entry before any caller sees one.

    Reading once and handing the lines onward is what keeps enforcement and
    refresh looking at the SAME snapshot. Re-reading inside the refresh instead
    opened a window where the file could turn malformed between the two reads --
    raising past the annotation handler -- or disappear, leaving the refresh to
    write an empty baseline over every recorded high-water mark.
    """
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    for raw in lines:
        _parse_entry(raw)
    return lines


def baseline_from_lines(lines: list[str]) -> dict[str, float]:
    """Project validated lines to ``path -> recorded rate``."""
    recorded: dict[str, float] = {}
    for raw in lines:
        parsed = _parse_entry(raw)
        if parsed is not None:
            recorded[parsed[0]] = parsed[1]
    return recorded


def read_baseline(path: Path) -> dict[str, float]:
    """Load ``path -> recorded rate``. A missing file is an empty baseline, not an error.

    A missing baseline means "every file must meet the floor", which is the
    correct reading for a lane that has not been baselined yet.
    """
    return baseline_from_lines(read_baseline_lines(path))


def seed_baseline(path: Path, files: list[FileCoverage], floor: float) -> int:
    """Create a baseline from scratch: every file currently below ``floor``.

    Seeding is how a lane is stood up, and it is the only path that may ADD a
    path to the baseline -- see ``prune_baseline`` for why that separation
    exists. Returns the entry count.
    """
    below = sorted((f for f in files if f.rate < floor), key=lambda f: f.path)
    _write_lines(
        path,
        [f"{f.path} {f.rate:.1f}   # {f.covered}/{f.statements}" for f in below],
        header=True,
    )
    return len(below)


def prune_baseline(
    path: Path,
    files: list[FileCoverage],
    lines: list[str],
    floor: float,
) -> tuple[int, list[FileCoverage]]:
    """Delete graduated and vanished entries. A surviving line is copied VERBATIM.

    ``lines`` is the already-read, already-validated snapshot from
    ``read_baseline_lines``: this function never re-reads the file it is about to
    replace, so enforcement and refresh cannot disagree about what the baseline
    said.

    Pruning only ever removes entry lines. Everything else in the file -- the
    header, an operator's note, a blank separator -- is carried through untouched,
    because a refresh that dropped a human's comment would destroy the context
    explaining why an entry is still listed, and that is the one thing here a tool
    cannot reconstruct.

    Re-rendering a survivor would pair its recorded rate with today's
    covered/statements, so the line would state a rate and a fraction that
    disagree with no marker of which is then and which is now -- in the one file
    whose safeguard is a human reading its diff. Copying the line through, byte
    for byte, keeps each entry an immutable record of the moment it was taken.

    Two refusals follow from the same discipline, and both close a hole that a
    naive "rewrite the file from the report" refresh opens:

    * It never ADDS a path. The graduation failure tells the reader to refresh, so
      a refresh that also absorbed unlisted files would make the documented
      remediation for one verdict launder another -- one command, and an
      under-tested new file is exempt forever. Unlisted offenders are returned to
      the caller to report, never written.
    * It never re-records a rate, because it never rewrites a line at all, so the
      high-water mark the regression rule compares against cannot be erased by
      refreshing instead of by fixing.

    Returns the surviving-entry count and the unlisted offenders it refused to add.
    """
    measured = {f.path: f for f in files}
    kept: list[str] = []
    listed: set[str] = set()
    for raw in lines:
        parsed = _parse_entry(raw)
        if parsed is None:
            # The header, an operator's note, a blank separator. Preserved: a
            # refresh that dropped a human's comment would destroy the context
            # explaining why an entry is still listed, which is the one thing in
            # this file a tool cannot reconstruct.
            kept.append(raw)
            continue
        entry_path, _ = parsed
        listed.add(entry_path)
        current = measured.get(entry_path)
        if current is None:
            continue  # deleted, renamed, or no longer measured
        if current.rate >= floor + tolerance_for(current.statements):
            continue  # graduated clear of the noise band
        # No rstrip: "verbatim" has to mean verbatim, or a survivor carrying
        # trailing whitespace shows up rewritten in the baseline diff.
        kept.append(raw)

    # Validate BEFORE writing. A report from the other lane shares no paths with
    # this baseline, so every entry reads as vanished and the refresh would write
    # an empty file -- destroying every recorded high-water mark -- and only then
    # report the mismatch. Zero overlap is never a legitimate refresh: a file that
    # graduated is still MEASURED, so it appears in the report. The lanes' reports
    # are easy to confuse because both are named `*.xml` inside a download dir.
    if listed and not (listed & set(measured)):
        raise ValueError(
            f"{path} lists {len(listed)} file(s), none of which appear in this report "
            f"({len(measured)} measured) -- this looks like the other lane's coverage "
            f"report. Refusing to rewrite the baseline; nothing was changed."
        )

    _write_lines(path, kept)
    refused = [f for f in files if f.rate < floor and f.path not in listed]
    return len([ln for ln in kept if _parse_entry(ln) is not None]), sorted(
        refused, key=lambda f: f.path
    )


def _write_lines(path: Path, lines: list[str], header: bool = False) -> None:
    """Atomically write the baseline. ``header`` prepends the self-documenting preamble.

    Only ``seed_baseline`` asks for the header, because only it creates a file.
    A refresh passes the lines it kept -- which already include whatever header and
    operator comments the file had -- so re-adding one would duplicate it.

    Written to a sibling temp file and moved into place, because the naive
    ``open(path, "w")`` truncates the live baseline before the replacement is
    durable -- an interrupted or failing refresh would then have destroyed
    recorded high-water marks that nothing else stores. ``os.replace`` is atomic
    on POSIX and Windows alike, so the file a reader sees is always one complete
    version or the other.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (BASELINE_HEADER if header else "") + "".join(f"{line}\n" for line in lines)
    # newline='\n' via open() rather than Path.write_text(newline=...), which only
    # exists on 3.10+: this script is also run by bare system interpreters. Pinning
    # LF keeps a Windows checkout from rewriting the file as CRLF and turning an
    # unrelated PR into a whole-file diff.
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(handle.name)
        raise


@dataclass
class Verdicts:
    """What the gate found. Any non-empty failing list means exit 1."""

    new_offenders: list[FileCoverage]
    regressed: list[tuple[FileCoverage, float]]
    graduated: list[FileCoverage]
    stale: list[str]
    passing: int
    exempt: int

    @property
    def failed(self) -> bool:
        return bool(self.new_offenders or self.regressed or self.graduated)


def evaluate(files: list[FileCoverage], baseline: dict[str, float], floor: float) -> Verdicts:
    """Apply the three baseline rules. Pure, so the self-test drives it directly."""
    new_offenders: list[FileCoverage] = []
    regressed: list[tuple[FileCoverage, float]] = []
    graduated: list[FileCoverage] = []
    passing = 0
    exempt = 0
    seen: set[str] = set()

    for entry in files:
        seen.add(entry.path)
        recorded = baseline.get(entry.path)
        band = tolerance_for(entry.statements)
        if entry.rate >= floor:
            # Graduation needs the SAME noise band the regression path uses, and
            # for a sharper reason than symmetry. Without it a file recorded just
            # under the floor crosses it on ordinary shard noise, graduation goes
            # red for someone who never touched that file, the documented refresh
            # drops it from the baseline, and the next run's noise puts it back
            # under -- now unlisted, so it fails as a new offender instead. That
            # oscillation has no green state for anyone to reach.
            if recorded is not None and entry.rate >= floor + band:
                graduated.append(entry)
            else:
                passing += 1
            continue
        if recorded is None:
            new_offenders.append(entry)
            continue
        if entry.rate < recorded - band:
            regressed.append((entry, recorded))
        else:
            exempt += 1

    # Warn-only: a baselined file legitimately disappears when it is deleted,
    # renamed, or dropped from the coverage config. Failing here would charge a
    # deletion to whoever performed it. --update-baseline prunes these.
    stale = sorted(set(baseline) - seen)
    return Verdicts(new_offenders, regressed, graduated, stale, passing, exempt)


ARTIFACT_BY_LABEL = {"Backend": "coverage-backend", "Frontend": "coverage-frontend"}


def _print_refresh_recipe(baseline_path: Path, label: str) -> None:
    """Print an executable refresh recipe, not just the flag name.

    A graduation fails the build for a contributor who may never have touched the
    file, so the remediation has to be runnable without knowing how this repo's CI
    stores coverage. "Rerun with --update-baseline" is not runnable on its own: the
    refresh needs the CI-COMBINED report, and a locally generated one has different
    rates (CI deselects capability-gated tests and splits shards by recorded
    duration), so refreshing from a local run can prune entries CI would keep.
    """
    artifact = ARTIFACT_BY_LABEL.get(label, f"coverage-{label.lower()}")
    lane = label.lower()
    print(
        "\n    To refresh, use THIS run's artifact -- not a local coverage run, whose\n"
        "    rates differ from CI's and would prune the wrong entries:\n"
        f"      gh run download $GITHUB_RUN_ID --name {artifact} --dir /tmp/cov-{lane}\n"
        f"      python3 scripts/check_per_file_coverage.py \\\n"
        f"        $(find /tmp/cov-{lane} -name '*.xml' | head -1) \\\n"
        f"        --label {label} --floor <floor> --baseline {baseline_path} \\\n"
        "        --update-baseline\n"
        f"    then commit {baseline_path}. Keep the download dir lane-specific as shown:\n"
        "    both lanes' reports are named `*.xml`, and passing the other lane's is\n"
        "    refused (nothing is written) rather than silently emptying the baseline.\n"
        "    The refresh only DELETES entries -- it cannot add a path or rewrite a\n"
        "    recorded rate -- so it is safe to run and review."
    )


def report(label: str, floor: float, verdicts: Verdicts, baseline_path: Path) -> None:
    """Print a reviewer-legible verdict, and for each failure the action that clears it."""
    total = verdicts.passing + verdicts.exempt + len(verdicts.new_offenders)
    total += len(verdicts.regressed) + len(verdicts.graduated)
    print(f"{label} per-file coverage floor: {floor:.0f}%")
    print(
        f"  {total} measured files — {verdicts.passing} at or above the floor, "
        f"{verdicts.exempt} baselined"
    )

    if verdicts.new_offenders:
        print(
            f"\n  ::error::{len(verdicts.new_offenders)} file(s) below {floor:.0f}% "
            f"and not baselined — add tests, do not extend the baseline:"
        )
        for entry in sorted(verdicts.new_offenders, key=lambda f: f.rate):
            print(f"    {entry.rate:5.1f}%  {entry.path}  ({entry.covered}/{entry.statements})")

    if verdicts.regressed:
        print(
            f"\n  ::error::{len(verdicts.regressed)} baselined file(s) lost coverage "
            f"(tolerance is the greater of {REGRESSION_TOLERANCE_PP:.0f}pp and one "
            f"statement):"
        )
        for entry, was in sorted(verdicts.regressed, key=lambda pair: pair[0].rate):
            band = tolerance_for(entry.statements)
            print(
                f"    {entry.rate:5.1f}%  (baselined {was:.1f}%, allowed down to "
                f"{was - band:.1f}%)  {entry.path}  [{entry.covered}/{entry.statements}]"
            )

    if verdicts.graduated:
        print(
            f"\n  ::error::{len(verdicts.graduated)} baselined file(s) now meet the floor. "
            f"Remove them so the baseline keeps shrinking:"
        )
        for entry in sorted(verdicts.graduated, key=lambda f: f.path):
            print(f"    {entry.rate:5.1f}%  {entry.path}")
        _print_refresh_recipe(baseline_path, label)

    if verdicts.stale:
        print(
            f"\n  note: {len(verdicts.stale)} baseline entr(ies) no longer measured "
            f"(deleted or renamed); --update-baseline prunes them:"
        )
        for path in verdicts.stale[:10]:
            print(f"    {path}")
        if len(verdicts.stale) > 10:
            print(f"    ... and {len(verdicts.stale) - 10} more")


def self_test() -> int:
    """Plant one probe per verdict and assert each fires. Mirrors check_brand_name.py --test."""
    files = [
        FileCoverage("ok.py", 100, 95),  # passes outright
        FileCoverage("new_offender.py", 100, 50),  # below floor, unlisted
        FileCoverage("still_bare.py", 100, 41),  # listed, within tolerance
        FileCoverage("regressed.py", 100, 30),  # listed, slid past tolerance
        FileCoverage("graduated.py", 100, 85),  # listed, clear of the noise band
        FileCoverage("at_the_edge.py", 100, 81),  # listed, over the floor but inside the band
        FileCoverage("empty.py", 0, 0),  # no statements: vacuously full
    ]
    baseline = {
        "still_bare.py": 42.0,
        "regressed.py": 45.0,
        "graduated.py": 60.0,
        "at_the_edge.py": 60.0,
        "deleted.py": 10.0,
    }
    verdicts = evaluate(files, baseline, floor=80.0)

    checks: list[tuple[str, bool]] = [
        ("new offender flagged", [f.path for f in verdicts.new_offenders] == ["new_offender.py"]),
        ("regression flagged", [f.path for f, _ in verdicts.regressed] == ["regressed.py"]),
        ("graduation flagged", [f.path for f in verdicts.graduated] == ["graduated.py"]),
        (
            "graduation inside the noise band NOT flagged",
            "at_the_edge.py" not in [f.path for f in verdicts.graduated],
        ),
        ("within-tolerance drift exempt", verdicts.exempt == 1),
        ("stale entry reported", verdicts.stale == ["deleted.py"]),
        ("statement-free file passes", verdicts.passing == 3),
        ("overall verdict is failure", verdicts.failed),
    ]
    failures = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if failures:
        print(f"::error::self-test failed: {', '.join(failures)}")
        return 1
    print(f"self-test: {len(checks)} checks passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument("report", nargs="?", type=Path, help="Cobertura XML report")
    parser.add_argument("--label", default="Lane", help="lane name used in output")
    parser.add_argument("--floor", type=float, default=80.0, help="per-file floor, percent")
    parser.add_argument("--baseline", type=Path, help="path to the lane's baseline file")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="prune graduated and vanished entries; never adds a path or rewrites a rate",
    )
    parser.add_argument(
        "--seed-baseline",
        action="store_true",
        help="create the baseline from scratch (create-only; the only mode that may add paths)",
    )
    parser.add_argument("--test", action="store_true", help="run the self-test and exit")
    args = parser.parse_args(argv)

    if args.test:
        return self_test()
    if args.report is None or args.baseline is None:
        parser.error("report and --baseline are required unless --test is given")
    if args.update_baseline and args.seed_baseline:
        parser.error("--update-baseline and --seed-baseline are mutually exclusive")
    if not args.report.exists():
        print(f"::error::coverage report not found: {args.report}")
        return 1

    files = parse_report(args.report)
    if not files:
        print(f"::error::{args.report} contained no measured files")
        return 1

    if args.seed_baseline:
        # Seeding is create-only. Left able to overwrite, it would undo the very
        # protection the seed/prune split exists for: rewriting an established
        # lane replaces every recorded high-water mark with the current, lower
        # rate, so the next enforcement reads those regressions as baseline state
        # and passes. Deleting the file first makes that an explicit act.
        if args.baseline.exists():
            print(
                f"::error::{args.baseline} already exists. Seeding is create-only, because "
                f"reseeding a live lane would replace its recorded rates with current ones "
                f"and launder any regression. Use --update-baseline to prune it, or delete "
                f"the file first to seed deliberately."
            )
            return 1
        count = seed_baseline(args.baseline, files, args.floor)
        print(f"{args.label}: seeded {count} entr(ies) below {args.floor:.0f}% to {args.baseline}")
        return 0

    # A hand-edited baseline is the one input that is neither the report nor a
    # verdict, so its parse failure gets the same ::error:: treatment every other
    # failure path has -- otherwise the step fails with a bare traceback and the
    # reader cannot tell a typo from a coverage regression without opening the log.
    # Read ONCE here: both consumers below work from this snapshot, so the file
    # cannot change shape between validation and use.
    try:
        lines = read_baseline_lines(args.baseline)
    except ValueError as exc:
        print(f"::error::{exc}")
        return 1
    baseline = baseline_from_lines(lines)

    if args.update_baseline:
        try:
            kept, refused = prune_baseline(args.baseline, files, lines, args.floor)
        except ValueError as exc:
            print(f"::error::{exc}")
            return 1
        print(f"{args.label}: kept {kept} entr(ies) in {args.baseline}")
        if refused:
            print(
                f"\n  ::error::refused to add {len(refused)} unlisted file(s) below "
                f"{args.floor:.0f}%. A refresh prunes; it does not absorb new offenders. "
                f"Add tests, or seed a new lane with --seed-baseline:"
            )
            for entry in refused:
                print(f"    {entry.rate:5.1f}%  {entry.path}")
            return 1
        return 0

    verdicts = evaluate(files, baseline, args.floor)
    report(args.label, args.floor, verdicts, args.baseline)
    return 1 if verdicts.failed else 0


if __name__ == "__main__":
    sys.exit(main())
