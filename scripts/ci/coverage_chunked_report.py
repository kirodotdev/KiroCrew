"""Build ``coverage.xml`` and the text report from combined data, in parallel chunks.

``coverage xml`` and ``coverage report`` each analyse every measured source file
in one process. This script deals the measured files into contiguous ranges of
their list, sorted the way coverage sorts report rows, runs both commands on
each range side by side, and merges the per-range XML reports into the one file
the Coverage Gate reads.

The merged report is the gate's input, so nothing here may let a run pass on
less than a single ``coverage xml`` would measure. Every check below ends the
run non-zero with a named reason:

* a chunk command that exits non-zero, or writes no report;
* a chunk report that does not parse, has an unexpected shape, or whose own
  totals disagree with its ``<line>`` elements;
* a file reported by a chunk it was not dealt to, or reported twice;
* a measured file that no chunk reported, unless ``coverage report`` itself
  says there is no data to report for it (it is omitted by configuration);
* a text table whose row count differs from its chunk's XML;
* a merged report that does not read back with the totals it was written with.

``fail_under`` is refused: it would be applied to each chunk and not to the
whole, and the floors are enforced by the Coverage Gate.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_CHUNKS = 8
ROOT_COUNTS = ("lines-valid", "lines-covered", "branches-valid", "branches-covered")
NO_DATA = "No data to report."
_CONDITION = re.compile(r"^\d+% \((\d+)/(\d+)\)$")
_DIGITS = re.compile(r"(\d+)")
_COMMENT = re.compile(r"<!--(.*?)-->", re.DOTALL)

# (argv, file that receives stdout or None) -> (exit status, captured stderr and stdout)
Runner = Callable[[Sequence[str], "Path | None"], "tuple[int, str]"]


class ChunkError(Exception):
    """A chunk or the merge cannot be trusted; the message says which and why."""


@dataclass(frozen=True)
class ClassReport:
    """One ``<class>`` element: a measured file and its lines."""

    package: str
    attrs: tuple[tuple[str, str], ...]
    lines: tuple[tuple[tuple[str, str], ...], ...]
    counts: tuple[int, int, int, int]

    @property
    def filename(self) -> str:
        return dict(self.attrs)["filename"]


@dataclass
class ChunkReport:
    """What one chunk's XML report holds."""

    version: str
    comments: tuple[str, ...]
    sources: tuple[str, ...]
    arcs: bool = True
    classes: dict[str, ClassReport] = field(default_factory=dict)


def chunk_count(cpus: int | None, files: int) -> int:
    """One chunk per CPU, at most ``MAX_CHUNKS`` and never more than there are files."""
    return max(1, min(cpus or 1, MAX_CHUNKS, files))


def available_cpus() -> int | None:
    affinity = getattr(os, "sched_getaffinity", None)
    return len(affinity(0)) if affinity else os.cpu_count()


def _human_key(text: str) -> tuple[list[str | int], str]:
    """coverage.py's report order: digit runs compare as numbers."""
    return ([int(part) if part.isdigit() else part for part in _DIGITS.split(text)], text)


def plan_chunks(
    files: Sequence[str], chunks: int, weights: dict[str, int] | None = None
) -> list[list[str]]:
    """Deal the files, in report order, into ``chunks`` contiguous ranges of about equal weight.

    A file's weight is its source size, which is what its analysis time follows;
    with no weights every file counts the same and the range sizes differ by at
    most one. A file joins a range when its midpoint falls inside that range's
    share of the total, and every range holds at least one file.
    """
    ordered = sorted(files, key=_human_key)
    if len(set(ordered)) != len(ordered):
        raise ChunkError("the measured file list names a file twice")
    if not 1 <= chunks <= max(1, len(ordered)):
        raise ChunkError(f"cannot deal {len(ordered)} file(s) into {chunks} chunk(s)")
    costs = [max(1, (weights or {}).get(name, 1)) for name in ordered]
    total = sum(costs)
    plan: list[list[str]] = []
    start = spent = 0
    for index in range(chunks):
        last = len(ordered) - (chunks - index - 1)
        end = min(start + 1, last)
        spent += sum(costs[start:end])
        while end < last and (spent + costs[end] / 2) * chunks <= total * (index + 1):
            spent += costs[end]
            end += 1
        if index == chunks - 1:
            end = len(ordered)
        plan.append(ordered[start:end])
        start = end
    return plan


def _rate(hit: int, total: int) -> str:
    """coverage.py's rate format."""
    return "1" if total == 0 else f"{hit / total:.4g}"


def _line_counts(
    lines: Sequence[Sequence[tuple[str, str]]], where: str
) -> tuple[int, int, int, int]:
    valid = covered = branches = branches_covered = 0
    for attrs in lines:
        line = dict(attrs)
        if "number" not in line or line.get("hits") not in ("0", "1"):
            raise ChunkError(f"{where}: a <line> has no number or a hits value other than 0 or 1")
        valid += 1
        covered += line["hits"] == "1"
        if "condition-coverage" in line:
            match = _CONDITION.match(line["condition-coverage"])
            if not match:
                raise ChunkError(
                    f"{where}: unreadable condition-coverage {line['condition-coverage']!r}"
                )
            taken, total = int(match.group(1)), int(match.group(2))
            if taken > total:
                raise ChunkError(
                    f"{where}: more branches taken than exist on line {line['number']}"
                )
            branches += total
            branches_covered += taken
    return valid, covered, branches, branches_covered


def _expect(element: Any, tag: str, where: str) -> Any:
    if element.tag != tag or (element.text or "").strip():
        raise ChunkError(f"{where}: expected <{tag}> with no text, found <{element.tag}>")
    return element


def read_chunk(path: Path) -> ChunkReport:
    """Parse one chunk's XML report and check it against its own totals.

    Parsed with defusedxml, as every Cobertura report in this repository is,
    and with a document type declaration refused: without one a document can
    define no entity. The parser drops comments, so the two that coverage
    writes under the root are read from the head of the file as text.
    """
    from defusedxml import DefusedXmlException
    from defusedxml.ElementTree import ParseError, parse

    where = path.name
    try:
        root = parse(path, forbid_dtd=True).getroot()
        with open(path, encoding="utf-8") as handle:
            head = handle.read(4096).split("<sources", 1)[0]
    except (ParseError, DefusedXmlException, OSError, UnicodeDecodeError) as exc:
        raise ChunkError(f"{where}: not a readable XML report ({exc})") from None
    if root.tag != "coverage":
        raise ChunkError(f"{where}: root element is <{root.tag}>, not <coverage>")
    comments = tuple(_COMMENT.findall(head))
    parts = list(root)
    if [part.tag for part in parts] != ["sources", "packages"]:
        raise ChunkError(f"{where}: <coverage> does not hold exactly <sources> and <packages>")
    sources = tuple(_expect(source, "source", where).text or "" for source in list(parts[0]))
    # Data measured without branch arcs is written with these two attributes the other way round.
    keys = [key for key in root.attrib if key in ("branches-valid", "branches-covered")]
    arcs = keys != ["branches-covered", "branches-valid"]
    report = ChunkReport(root.attrib.get("version", ""), comments, sources, arcs)
    totals = [0, 0, 0, 0]
    for package in list(parts[1]):
        _expect(package, "package", where)
        name = package.attrib.get("name")
        holders = list(package)
        if name is None or [holder.tag for holder in holders] != ["classes"]:
            raise ChunkError(f"{where}: a <package> has no name or does not hold exactly <classes>")
        for element in list(holders[0]):
            _expect(element, "class", where)
            filename = element.attrib.get("filename")
            inner = list(element)
            if (
                not filename
                or [part.tag for part in inner] != ["methods", "lines"]
                or len(inner[0])
            ):
                raise ChunkError(f"{where}: a <class> has no filename or an unexpected shape")
            lines = []
            for line in list(inner[1]):
                if len(_expect(line, "line", where)):
                    raise ChunkError(f"{where}: a <line> of {filename} has children")
                lines.append(tuple(line.attrib.items()))
            counts = _line_counts(lines, f"{where}: {filename}")
            if filename in report.classes:
                raise ChunkError(f"{where}: {filename} is reported twice")
            report.classes[filename] = ClassReport(
                name, tuple(element.attrib.items()), tuple(lines), counts
            )
            totals = [total + count for total, count in zip(totals, counts)]
    stated = []
    for key in ROOT_COUNTS:
        value = root.attrib.get(key, "")
        if not value.isdigit():
            raise ChunkError(f"{where}: <coverage> has no numeric {key}")
        stated.append(int(value))
    if stated != totals:
        raise ChunkError(f"{where}: totals {stated} disagree with its <line> elements {totals}")
    if not arcs and totals[2]:
        raise ChunkError(f"{where}: written as data without branches, yet its lines carry branches")
    return report


def merge_chunks(
    plan: Sequence[Sequence[str]], reports: Sequence[ChunkReport]
) -> tuple[dict[str, ClassReport], list[str]]:
    """Join the chunk reports; return every class by filename and the planned files none reported."""
    if len(plan) != len(reports) or not reports:
        raise ChunkError(f"{len(plan)} chunk(s) were planned and {len(reports)} reported")
    first = reports[0]
    merged: dict[str, ClassReport] = {}
    unreported: list[str] = []
    for index, (planned, report) in enumerate(zip(plan, reports), start=1):
        if (report.version, report.comments, report.sources, report.arcs) != (
            first.version,
            first.comments,
            first.sources,
            first.arcs,
        ):
            raise ChunkError(
                f"chunk {index} was written by a different coverage version or source root than chunk 1"
            )
        dealt = {path.replace("\\", "/") for path in planned}
        for filename, entry in report.classes.items():
            if filename not in dealt:
                raise ChunkError(f"chunk {index} reports {filename}, which it was not dealt")
            if filename in merged:
                raise ChunkError(f"{filename} is reported by two chunks")
            merged[filename] = entry
        unreported.extend(sorted(dealt - set(report.classes)))
    return merged, unreported


def totals_of(classes: dict[str, ClassReport]) -> tuple[int, int, int, int]:
    sums = [0, 0, 0, 0]
    for entry in classes.values():
        sums = [total + count for total, count in zip(sums, entry.counts)]
    return sums[0], sums[1], sums[2], sums[3]


def _attr(value: str) -> str:
    """Escape an attribute value the way xml.dom.minidom writes it."""
    value = (
        value.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;").replace(">", "&gt;")
    )
    return value.replace("\r", "&#13;").replace("\n", "&#10;").replace("\t", "&#9;")


def _tag(name: str, attrs: Sequence[tuple[str, str]], close: str = ">") -> str:
    return "<" + name + "".join(f' {key}="{_attr(value)}"' for key, value in attrs) + close


def render(first: ChunkReport, classes: dict[str, ClassReport], timestamp: int) -> str:
    """Write the merged report in the layout ``coverage xml`` uses."""
    valid, covered, branches, branches_covered = totals_of(classes)
    root = [
        ("version", first.version),
        ("timestamp", str(timestamp)),
        ("lines-valid", str(valid)),
        ("lines-covered", str(covered)),
        ("line-rate", _rate(covered, valid)),
    ]
    if first.arcs:
        root += [
            ("branches-valid", str(branches)),
            ("branches-covered", str(branches_covered)),
            ("branch-rate", _rate(branches_covered, branches)),
        ]
    else:
        root += [("branches-covered", "0"), ("branches-valid", "0"), ("branch-rate", "0")]
    root.append(("complexity", "0"))
    out = ['<?xml version="1.0" ?>', _tag("coverage", root)]
    out += [f"\t<!--{comment}-->" for comment in first.comments]
    out.append("\t<sources>")
    for source in first.sources:
        text = source.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        out.append(f"\t\t<source>{text}</source>")
    out += ["\t</sources>", "\t<packages>"]
    packages: dict[str, list[ClassReport]] = {}
    for entry in classes.values():
        packages.setdefault(entry.package, []).append(entry)
    for name in sorted(packages, key=_human_key):
        members = sorted(packages[name], key=lambda entry: _human_key(entry.filename))
        sums = [sum(entry.counts[i] for entry in members) for i in range(4)]
        rates = [
            ("name", name),
            ("line-rate", _rate(sums[1], sums[0])),
            ("branch-rate", _rate(sums[3], sums[2]) if first.arcs else "0"),
            ("complexity", "0"),
        ]
        out += ["\t\t" + _tag("package", rates), "\t\t\t<classes>"]
        for entry in members:
            out += ["\t\t\t\t" + _tag("class", entry.attrs), "\t\t\t\t\t<methods/>"]
            if entry.lines:
                out.append("\t\t\t\t\t<lines>")
                out += ["\t\t\t\t\t\t" + _tag("line", line, "/>") for line in entry.lines]
                out.append("\t\t\t\t\t</lines>")
            else:
                out.append("\t\t\t\t\t<lines/>")
            out.append("\t\t\t\t</class>")
        out += ["\t\t\t</classes>", "\t\t</package>"]
    out += ["\t</packages>", "</coverage>", ""]
    return "\n".join(out)


def table_rows(text: str) -> int:
    """Count the file rows of one ``coverage report`` table: the lines between its two rules."""
    lines = text.splitlines()
    rules = [index for index, line in enumerate(lines) if line and set(line) == {"-"}]
    if len(rules) != 2:
        raise ChunkError("a text report does not have the two rules of a coverage table")
    return rules[1] - rules[0] - 1


def overall_total(classes: dict[str, ClassReport]) -> str:
    valid, covered, branches, branches_covered = totals_of(classes)
    denominator = valid + branches
    percent = 100.0 * (covered + branches_covered) / denominator if denominator else 100.0
    return (
        f"TOTAL of all chunks, from the merged coverage.xml: {len(classes)} files, "
        f"{valid} statements, {valid - covered} missed, {branches} branches, "
        f"{branches - branches_covered} branches missed, {percent:.2f}% covered"
    )


def run_command(argv: Sequence[str], stdout: Path | None) -> tuple[int, str]:
    """Run one coverage command; its stdout goes to ``stdout`` when given."""
    if stdout is None:
        done = subprocess.run(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
        )
        return done.returncode, done.stdout
    with open(stdout, "w", encoding="utf-8") as handle:
        done = subprocess.run(
            list(argv), stdout=handle, stderr=subprocess.PIPE, encoding="utf-8", errors="replace"
        )
    return done.returncode, done.stderr


def _run_chunk(
    index: int, files: Sequence[str], work: Path, python: str, runner: Runner
) -> tuple[Path, Path]:
    xml, text = work / f"chunk-{index}.xml", work / f"chunk-{index}.txt"
    status, output = runner([python, "-m", "coverage", "xml", "-o", str(xml), *files], None)
    if status != 0:
        raise ChunkError(f"chunk {index}: coverage xml exited {status}: {output.strip()[-2000:]}")
    if not xml.is_file():
        raise ChunkError(f"chunk {index}: coverage xml exited 0 and wrote no report")
    status, output = runner([python, "-m", "coverage", "report", *files], text)
    if status != 0:
        raise ChunkError(
            f"chunk {index}: coverage report exited {status}: {output.strip()[-2000:]}"
        )
    return xml, text


def build(
    files: Sequence[str],
    chunks: int,
    xml_out: Path,
    report_out: Path,
    work: Path,
    *,
    runner: Runner = run_command,
    python: str = sys.executable,
    check_rows: bool = True,
    timestamp: int | None = None,
    weights: dict[str, int] | None = None,
) -> str:
    """Run every chunk, merge, and write both outputs; return the overall total line."""
    plan = plan_chunks(files, chunks, weights)
    work.mkdir(parents=True, exist_ok=True)
    xml_out.unlink(missing_ok=True)
    header = (
        f"{len(files)} measured files, in coverage report's row order, dealt into {len(plan)} contiguous "
        "range(s), one per CPU: a file joins a range while its midpoint, counted in source bytes from the "
        "start of the list, lies within that range's equal share of the total. Each range has its own "
        "table below, so the tables read on from one another."
    )
    sections = [header]
    failure: ChunkError | None = None
    reports: list[ChunkReport] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(plan)) as pool:
        jobs = [
            pool.submit(_run_chunk, i, part, work, python, runner)
            for i, part in enumerate(plan, start=1)
        ]
        for index, (part, job) in enumerate(zip(plan, jobs), start=1):
            title = (
                f"== chunk {index} of {len(plan)}: {len(part)} files, {part[0]} .. {part[-1]} =="
            )
            try:
                xml, text = job.result()
                table = text.read_text(encoding="utf-8")
                sections += [title, table.rstrip("\n")]
                report = read_chunk(xml)
                if check_rows and table_rows(table) != len(report.classes):
                    raise ChunkError(
                        f"chunk {index}: the text table has {table_rows(table)} rows "
                        f"and the XML report {len(report.classes)} files"
                    )
                reports.append(report)
            except ChunkError as exc:
                failure = failure or exc
                sections += [title, f"FAILED: {exc}"]
    try:
        if failure:
            raise failure
        classes, unreported = merge_chunks(plan, reports)
        if unreported:
            status, output = runner([python, "-m", "coverage", "report", *unreported], None)
            if status == 0 or NO_DATA not in output:
                raise ChunkError(
                    f"{len(unreported)} measured file(s) are in no chunk's report and coverage does not "
                    f"call them omitted (first: {unreported[0]})"
                )
            sections.append(
                f"{len(unreported)} measured file(s) omitted by configuration: "
                + ", ".join(unreported)
            )
        if len(classes) + len(unreported) != len(files):
            raise ChunkError(
                f"{len(files)} files were measured and {len(classes) + len(unreported)} accounted for"
            )
        stamp = int(time.time() * 1000) if timestamp is None else timestamp
        xml_out.write_text(render(reports[0], classes, stamp), encoding="utf-8", newline="\n")
        written = read_chunk(xml_out)
        if written.classes != classes:
            xml_out.unlink()
            raise ChunkError(f"{xml_out.name} did not read back as the classes it was written from")
        total = overall_total(classes)
        sections.append(total)
        return total
    finally:
        report_out.write_text("\n".join(sections) + "\n", encoding="utf-8", newline="\n")


def main(argv: Sequence[str] | None = None) -> int:
    import coverage

    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--xml", type=Path, required=True, help="merged XML report to write")
    parser.add_argument("--report", type=Path, required=True, help="text report to write")
    parser.add_argument("--work-dir", type=Path, default=Path("coverage-chunks"))
    parser.add_argument(
        "--chunks", type=int, default=None, help="default: one per CPU, at most %d" % MAX_CHUNKS
    )
    args = parser.parse_args(argv)
    try:
        cov = coverage.Coverage()
        if float(cov.get_option("report:fail_under") or 0) > 0:
            raise ChunkError(
                "fail_under is set; it would apply to each chunk, not to the whole report"
            )
        cov.load()
        files = sorted(cov.get_data().measured_files())
        if not files:
            raise ChunkError("the combined coverage data measures no file")
        per_file_rows = not (
            cov.get_option("report:skip_covered") or cov.get_option("report:skip_empty")
        )
        chunks = (
            args.chunks if args.chunks is not None else chunk_count(available_cpus(), len(files))
        )
        sizes = {name: os.path.getsize(name) for name in files if os.path.isfile(name)}
        total = build(
            files,
            chunks,
            args.xml,
            args.report,
            args.work_dir,
            check_rows=per_file_rows,
            weights=sizes,
        )
    except ChunkError as exc:
        print(f"::error::building the coverage reports failed: {exc}", file=sys.stderr)
        return 1
    print(total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
