#!/usr/bin/env python3
"""check_file_line_cap.py -- no hand-written code file over 2,000 lines.

## The rule

A tracked source or test file holds at most ``CAP`` (2,000) lines. Code means a
path whose extension is in ``CODE_EXTENSIONS``: Python, TypeScript and
JavaScript, CSS, shell, PowerShell and Swift, backend and frontend alike. New
code goes in a new module. When a file nears the cap, move whole definitions into
a sibling module and keep the original path as the import path callers use.

A file past the cap is the one every change contends for: merge conflicts and
review time concentrate there, and splitting it later costs far more than not
growing it now.

## What is checked

Every regular file git tracks (``git ls-files``) whose extension is a code
extension, except:

* the exemptions in ``EXEMPTIONS``, each with the reason it is out of scope.
  Generated output is regenerated, never hand-split; vendored code tracks its
  upstream byte for byte; data, prose and CI YAML are not code;
* binary files, meaning a NUL byte in the first 8 KiB;
* symlinks and submodule entries, which have no content of their own at their
  path.

Only tracked files count, never untracked ones, so the verdict is the one CI
reaches on a clean checkout, and a scratch file under a ``TMPDIR`` inside the
checkout cannot change it.

A line is counted the way ``wc -l`` counts it (newline bytes), plus one when the
file does not end in a newline, so an unterminated last line counts too. CRLF
endings count the same as LF.

## The baseline

``.github/file-line-cap-baseline.txt`` lists the files that were already over
the cap when the rule arrived, as ``<lines> <path>`` rows sorted by path, after a
``#`` header. It only ever shrinks. The check FAILS on:

1. a checked file over the cap that the baseline does not list. Split it; never
   add a row;
2. a stale row: its file does not exist, is not checked (exempt, binary, not
   code), or has ``CAP`` lines or fewer. The message names the exact row to
   delete. From then on the file is capped like any other;
3. a malformed baseline: a row that is not ``<lines> <path>``, a count that is
   not over the cap, a comment after the first row, or rows out of order or
   repeated.

## The verdict is scoped to the change

The tree is scanned whole, but rules 1 and 2 fail only for a file the change
under test touches (``scripts/ratchet_scope.py``, the scope the other merge-ref
gates share, read again with ``--no-renames`` so a renamed file's old path
counts). A violation in an untouched file is MAIN DRIFT: two changes that were
each green can together push a file over the cap, and failing every open change
for it would turn main red for everyone. Main drift is a warning naming the file
and the remedy. Rule 3 always fails. When the scope cannot be determined, or
``RATCHET_SCOPE_WHOLE_TREE`` or ``--whole-tree`` is set, the whole tree is
judged.

A change that edits the baseline is judged against the baseline at its base: an
edit that adds a path or raises a count fails, because the baseline only
shrinks, and so does one that lowers a row below its file's size; a
deletion-only or lowering edit is judged by the rows it changed, so someone
else's stale row beside it stays main drift.

A baselined file that grew past its row is REPORTED, not failed. It must not
grow, and it should be split, but most changes that touch these files are not
about their size, and they land every day.

``--update-baseline`` deletes stale rows and lowers counts to the current size,
for the files the change touches only, so it never edits someone else's row.
When it cannot tell which files those are, it refuses (exit 2) unless
``--whole-tree`` asks it to rewrite every row. It never adds a path and never
raises a count, so it cannot absorb a new oversized file or growth.

Exit codes: 0 clean (warnings and reports may still be printed), 1 a failure
above, 2 a usage or environment error (git could not list the checkout, the
baseline cannot be read, or ``--update-baseline`` met a malformed baseline).

Usage::

    python3 scripts/check_file_line_cap.py                    # check the checkout
    python3 scripts/check_file_line_cap.py --update-baseline  # prune after a split
    python3 scripts/check_file_line_cap.py --test             # self-test

CI enforces the scoped verdict in Backend Lint and Frontend Lint, which check out
the full history, and ``main-ratchet-audit.yml`` judges main's whole tree.
``test/test_file_line_cap.py`` also runs the check in the backend test suite
(Linux and Windows) on every change; its shallow checkout cannot resolve the
change's scope, so there it reports violations without failing. Only tracked
files count, so ``git add`` a new file before checking it locally.
"""

from __future__ import annotations

import argparse
import fnmatch
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINE_RELPATH = ".github/file-line-cap-baseline.txt"

CAP = 2000

CODE_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyi",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".css",
        ".scss",
        ".sh",
        ".bash",
        ".ps1",
        ".swift",
    }
)

# A NUL byte this early means the file is not text, whatever its extension.
BINARY_PROBE_BYTES = 8192

# git index modes of a regular file. 120000 is a symlink and 160000 a submodule
# gitlink; neither has hand-written content at its own path.
_REGULAR_FILE_MODES = frozenset({"100644", "100755"})


@dataclass(frozen=True)
class Exemption:
    """Paths the cap does not apply to, and why.

    A pattern with no ``/`` matches the file's basename. A pattern with a ``/``
    matches the whole repo-relative path, and its ``*`` also crosses
    directories. Matching is case-sensitive on every platform.
    """

    patterns: tuple[str, ...]
    reason: str

    def matches(self, path: str) -> bool:
        name = path.rsplit("/", 1)[-1]
        return any(
            fnmatch.fnmatchcase(path if "/" in pattern else name, pattern)
            for pattern in self.patterns
        )


# Only an entry that matches a CODE_EXTENSIONS path changes a verdict; the others
# record why that kind of file is out of scope.
EXEMPTIONS: tuple[Exemption, ...] = (
    Exemption(
        ("*.gen.ts", "*.generated.*", "config-baseline.json", "test/link_screen_sites.py"),
        "generated output: regenerate it with its generator, never hand-split it",
    ),
    Exemption(
        ("package-lock.json", "*.lock", "website/src/i18n/locales/*.json"),
        "data and catalogues (lockfiles, i18n locale catalogues), not code",
    ),
    Exemption(
        ("src/kiro_crew/_vendor/*", "*.min.js"),
        "vendored third-party code, kept byte-for-byte equal to its upstream",
    ),
    Exemption(
        ("*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.ico", "*.icns", "*.pdf")
        + ("*.mp3", "*.mp4", "*.mov", "*.webm", "*.wav")
        + ("*.woff", "*.woff2", "*.ttf", "*.otf"),
        "media and fonts, not code",
    ),
    Exemption(
        ("THIRD-PARTY-NOTICES*", "CHANGELOG.md", "docs/*.md"),
        "prose, not code; capping the docs tree is a separate phase",
    ),
    Exemption(
        (".github/workflows/*.yml", ".github/workflows/*.yaml"),
        "CI workflow logic; capping it is a separate, maintainer-approved phase",
    ),
)

HEADER = f"""\
# Code files already over the {CAP:,}-line cap, as `<lines> <path>`, sorted by
# path. scripts/check_file_line_cap.py fails on any OTHER file over the cap, so
# this list can only shrink: a file that drops to {CAP:,} lines or fewer must
# leave it, and from then on it is capped like any other file.
#
# Do NOT add a line or raise a count to make a red check green: split the file
# instead, moving whole definitions into a new sibling module. A listed file
# must not grow; the check reports one that did.
#
# Delete only your own rows, never someone else's. The refresh below edits only
# the rows of files your change touches, and only deletes lines and lowers counts;
# where it cannot tell which files those are, it refuses unless --whole-tree asks
# it to rewrite every row:
#   python3 scripts/check_file_line_cap.py --update-baseline
"""

_ROW = re.compile(r"(?P<count>[1-9][0-9]*) (?P<path>\S(?:.*\S)?)\Z")

_UPDATE_HINT = "python3 scripts/check_file_line_cap.py --update-baseline"


class CheckError(Exception):
    """The check cannot run. Exit 2, never a verdict about the tree."""


def count_lines(data: bytes) -> int:
    """Lines the way ``wc -l`` counts them, plus one for an unterminated last line."""
    lines = data.count(b"\n")
    if data and not data.endswith(b"\n"):
        lines += 1
    return lines


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:BINARY_PROBE_BYTES]


def exemption_for(path: str) -> Exemption | None:
    for exemption in EXEMPTIONS:
        if exemption.matches(path):
            return exemption
    return None


def is_code_path(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in CODE_EXTENSIONS


def parse_ls_files_stage(output: bytes) -> list[str]:
    """Regular-file paths from ``git ls-files --stage -z`` output, in git's order.

    Each record is ``<mode> <object> <stage>\\t<path>``. A path in a merge
    conflict appears once per stage and is returned once.
    """
    paths: list[str] = []
    seen: set[str] = set()
    for record in output.split(b"\0"):
        if not record:
            continue
        meta, sep, raw_path = record.partition(b"\t")
        if not sep:
            raise CheckError(f"unexpected git ls-files record: {record[:80]!r}")
        mode = meta.split(b" ", 1)[0].decode("ascii", "replace")
        # surrogateescape: a path that is not valid UTF-8 still round-trips to
        # the filesystem instead of taking the whole check down.
        path = raw_path.decode("utf-8", "surrogateescape")
        if mode in _REGULAR_FILE_MODES and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def tracked_files(root: Path) -> list[str]:
    """Every regular file git tracks under *root*, as repo-relative POSIX paths."""
    argv = ["git", "ls-files", "--stage", "-z"]
    try:
        proc = subprocess.run(argv, capture_output=True, check=False, timeout=120, cwd=str(root))
    except (OSError, subprocess.SubprocessError) as exc:
        raise CheckError(f"cannot run git ls-files in {root}: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise CheckError(f"git ls-files failed in {root}: {detail}")
    return parse_ls_files_stage(proc.stdout)


@dataclass
class Scan:
    """Line counts of the checked files, and why each other path was skipped."""

    lines: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)


def scan(root: Path, paths: list[str]) -> Scan:
    """Measure every checked file among *paths* (repo-relative, POSIX)."""
    result = Scan()
    for path in paths:
        exemption = exemption_for(path)
        if exemption is not None:
            result.skipped[path] = f"exempt ({exemption.reason})"
            continue
        if not is_code_path(path):
            result.skipped[path] = "not a code file"
            continue
        try:
            data = (root / path).read_bytes()
        except FileNotFoundError:
            # Tracked but deleted in the working tree: there is nothing to measure.
            continue
        except OSError as exc:
            raise CheckError(f"cannot read {path}: {exc}") from exc
        if is_binary(data):
            result.skipped[path] = "binary"
            continue
        result.lines[path] = count_lines(data)
    return result


@dataclass
class Baseline:
    rows: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def parse_baseline(text: str) -> Baseline:
    """Parse the baseline. A malformed row lands in ``problems``; nothing raises."""
    baseline = Baseline()
    previous: str | None = None
    in_header = True
    for number, line in enumerate(text.splitlines(), start=1):
        where = f"{BASELINE_RELPATH}:{number}"
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if not in_header:
                baseline.problems.append(
                    f"{where}: a comment or blank line after the first row; the "
                    "header goes at the top and the rows follow it"
                )
            continue
        in_header = False
        match = _ROW.match(line)
        if match is None:
            baseline.problems.append(f"{where}: {line!r} is not a `<lines> <path>` row")
            continue
        count, path = int(match["count"]), match["path"]
        if "\\" in path or path.startswith(("/", "./")) or "/../" in f"/{path}":
            baseline.problems.append(
                f"{where}: {path!r} is not a plain repo-relative path with forward slashes"
            )
        elif count <= CAP:
            baseline.problems.append(
                f"{where}: {path} records {count} lines; only files over the "
                f"{CAP:,}-line cap belong in the baseline"
            )
        if path in baseline.rows:
            baseline.problems.append(f"{where}: {path} is listed twice")
            continue
        if previous is not None and path < previous:
            baseline.problems.append(
                f"{where}: {path} is out of order; rows are sorted by path, so it "
                f"belongs before {previous}"
            )
        baseline.rows[path] = count
        previous = path
    return baseline


def render_baseline(rows: dict[str, int]) -> str:
    """The canonical baseline text: ``HEADER``, then the rows sorted by path."""
    return HEADER + "".join(f"{count} {path}\n" for path, count in sorted(rows.items()))


def read_baseline(root: Path) -> Baseline:
    """The committed baseline. A missing file is an empty baseline, the strictest one."""
    try:
        text = (root / BASELINE_RELPATH).read_text(encoding="utf-8")
    except FileNotFoundError:
        return Baseline()
    except (OSError, UnicodeDecodeError) as exc:
        raise CheckError(f"cannot read {BASELINE_RELPATH}: {exc}") from exc
    return parse_baseline(text)


@dataclass
class Verdict:
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reports: list[str] = field(default_factory=list)
    stale: set[str] = field(default_factory=set)
    lowerable: dict[str, int] = field(default_factory=dict)
    checked: int = 0
    scope_note: str = ""


def _in_scope(path: str, changed: set[str] | None) -> bool:
    return changed is None or path in changed


def _growth_report(path: str, recorded: int, lines: int) -> str:
    return (
        f"{path} grew past its baseline row ({recorded:,} -> {lines:,} lines, cap {CAP:,}). "
        "A baselined file must not grow: split it instead of adding to it; see AGENTS.md. "
        "(Reported only; this does not fail the check.)"
    )


def evaluate(measured: Scan, baseline: Baseline, changed: set[str] | None = None) -> Verdict:
    """Judge a scan against the baseline. Pure: no filesystem, no git.

    *changed* is the set of paths to judge, or None for the whole tree. A
    violation in a file outside it is main drift: a warning, because failing
    every open change for a file one other change pushed over the cap turns main
    red for everyone. A malformed baseline always fails.
    """
    verdict = Verdict(checked=len(measured.lines))
    verdict.failures.extend(f"malformed baseline: {problem}" for problem in baseline.problems)

    for path, lines in sorted(measured.lines.items()):
        if lines <= CAP or path in baseline.rows:
            continue
        if _in_scope(path, changed):
            verdict.failures.append(
                f"{path} has {lines:,} lines, over the {CAP:,}-line cap. Split it; see "
                "AGENTS.md: move whole definitions into a new sibling module and keep "
                "this path as the import path callers use. Never add a row to "
                f"{BASELINE_RELPATH}."
            )
        else:
            verdict.warnings.append(
                f"main drift: {path} {lines:,} lines, over the {CAP:,}-line cap, in a "
                "file this change does not touch, so it does not fail this change. "
                "Remedy: split it in its own change; see AGENTS.md. Never add a row to "
                f"{BASELINE_RELPATH}."
            )

    for path, recorded in sorted(baseline.rows.items()):
        row = f"`{recorded} {path}`"
        if path in measured.lines and measured.lines[path] > CAP:
            lines = measured.lines[path]
            if lines > recorded:
                verdict.reports.append(_growth_report(path, recorded, lines))
            elif lines < recorded:
                verdict.lowerable[path] = lines
            continue
        verdict.stale.add(path)
        if path in measured.lines:
            state = f"has {measured.lines[path]:,} lines, within the {CAP:,}-line cap"
        elif path in measured.skipped:
            state = f"is not checked: {measured.skipped[path]}"
        else:
            state = "does not exist"
        if _in_scope(path, changed):
            verdict.failures.append(
                f"stale baseline row {row}: {path} {state}. Delete exactly that row from "
                f"{BASELINE_RELPATH} (or run `{_UPDATE_HINT}`, which edits only the rows "
                "of files this change touches). Delete only your own rows: never edit "
                "someone else's row, by hand or with --update-baseline."
            )
        else:
            verdict.warnings.append(
                f"main drift: stale baseline row {row}: {path} {state}, in a file this "
                "change does not touch, so it does not fail this change. Remedy: delete "
                f"exactly that row from {BASELINE_RELPATH} in its own change."
            )
    return verdict


@dataclass(frozen=True)
class Scope:
    """The files the change under test touches; ``changed`` None means the whole tree.

    ``base_rows`` is the baseline at the change's base, read only when the
    change edits the baseline (None when it cannot be read there).
    """

    changed: set[str] | None
    label: str
    base_rows: dict[str, int] | None = None
    whole_tree_requested: bool = False


WHOLE_TREE = Scope(None, "whole tree")


def rescope_baseline_edit(
    scope: Scope, rows: dict[str, int]
) -> tuple[set[str] | None, list[str], str]:
    """(paths to judge, failures, note) once the change's own baseline edit counts.

    A baseline edit is judged by the rows it changed, on either side: they join
    the judged paths, so a deleted row whose file is still over the cap fails as
    unlisted and a lowered row of a file now within the cap fails as stale, while
    every other stale row stays main drift. An edit that adds a path or raises a
    count fails, because the baseline only shrinks. When the baseline at the base
    cannot be read (the change creates it, or git cannot show it), the whole tree
    is judged.
    """
    changed = scope.changed
    if changed is None or BASELINE_RELPATH not in changed:
        return changed, [], ""
    base = scope.base_rows
    if base is None:
        return (
            None,
            [],
            (
                "the baseline at the base cannot be read (this change creates it, or git "
                "cannot show it there), so the whole tree is judged"
            ),
        )
    failures: list[str] = []
    for path, count in sorted(rows.items()):
        if path not in base:
            failures.append(
                f"this change adds the baseline row `{count} {path}`: the baseline only "
                f"shrinks. Split {path} instead; see AGENTS.md."
            )
        elif count > base[path]:
            failures.append(
                f"this change raises the baseline row for {path} from {base[path]:,} to "
                f"{count:,} lines: the baseline only shrinks. Split the file instead; see "
                "AGENTS.md."
            )
    edited = {path for path in base.keys() | rows.keys() if rows.get(path) != base.get(path)}
    return (changed - {BASELINE_RELPATH}) | edited, failures, ""


def judge(measured: Scan, baseline: Baseline, scope: Scope) -> Verdict:
    """The verdict for *scope*, its baseline edit included. Pure: no filesystem, no git.

    A row this change lowered below its file's measured size is a failure, not a
    growth report: a row the change writes may never be below the file's size,
    nor above the base row. The remedy names the count that satisfies both.
    """
    judged, edit_failures, note = rescope_baseline_edit(scope, baseline.rows)
    verdict = evaluate(measured, baseline, judged)
    base = scope.base_rows if judged is not None else None
    for path, count in sorted(baseline.rows.items()):
        lines = measured.lines.get(path, 0)
        if base and path in base and count < base[path] and lines > max(count, CAP):
            if lines > base[path]:
                remedy = (
                    f"restore the base row {base[path]:,} (the file grew past it, which is "
                    "reported, not failed)"
                )
            else:
                remedy = f"lower it to {lines:,}"
            edit_failures.append(
                f"this change lowers the baseline row for {path} to {count:,}, below its "
                f"{lines:,} lines: {remedy}."
            )
            verdict.reports = [r for r in verdict.reports if not r.startswith(f"{path} grew ")]
            if lines > base[path]:
                verdict.reports.append(_growth_report(path, base[path], lines))
    verdict.failures[len(baseline.problems) : len(baseline.problems)] = edit_failures
    verdict.scope_note = note
    return verdict


def _load_ratchet_scope(root: Path) -> Any:
    """``scripts/ratchet_scope.py``, loaded by path and pointed at *root*.

    A fresh module per call, so pointing it at another checkout reaches no other
    caller. ``scripts/`` is not a package, so it is loaded the way the other
    merge-ref gates load it.
    """
    script = ROOT / "scripts" / "ratchet_scope.py"
    spec = importlib.util.spec_from_file_location("ratchet_scope", script)
    if spec is None or spec.loader is None:
        raise CheckError(f"cannot load {script}")
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = root
    return module


def _diff_names(root: Path, *revisions: str) -> set[str]:
    """Paths ``git diff --no-renames`` names, so a rename keeps its old path too."""
    proc = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", *revisions],
        capture_output=True,
        check=False,
        timeout=120,
        cwd=str(root),
    )
    if proc.returncode != 0:
        return set()
    return {p for p in proc.stdout.decode("utf-8", "surrogateescape").split("\0") if p}


def change_scope(root: Path, *, whole_tree: bool = False) -> Scope:
    """Which files the change under test touches, and how that was decided.

    ``scripts/ratchet_scope.py`` gives the answer the other merge-ref gates
    share, and the diff from its base (``base_ref``) is read again with
    ``--no-renames``, so a renamed file's old path is in the change as well as
    its new one. None means the whole tree: *whole_tree* is set,
    ``RATCHET_SCOPE_WHOLE_TREE`` is set, or the scope is undeterminable. The
    uncommitted edits to tracked files count too (a clean CI checkout has none),
    so a file that is staged but not yet committed is judged as part of the
    change rather than as main drift.
    """
    if whole_tree:
        return Scope(None, "whole tree (--whole-tree)", whole_tree_requested=True)
    root = root.resolve()
    module = _load_ratchet_scope(root)
    changed, label = module.changed_paths()
    if changed is None:
        return Scope(None, label)
    base = module.base_ref(label, cwd=root)
    if base:
        changed |= _diff_names(root, base, "HEAD")
    uncommitted = _diff_names(root, "HEAD")
    if uncommitted:
        changed |= uncommitted
        label += " plus uncommitted edits"
    base_rows = None
    if BASELINE_RELPATH in changed:
        shown = module.show_at(base, BASELINE_RELPATH, cwd=root) if base else None
        base_rows = parse_baseline(shown).rows if shown is not None else None
    return Scope(changed, label, base_rows)


def check(root: Path, scope: Scope = WHOLE_TREE) -> Verdict:
    """The verdict for the checkout at *root*, judged for *scope*."""
    return judge(scan(root, tracked_files(root)), read_baseline(root), scope)


def pruned_rows(
    baseline: Baseline, verdict: Verdict, changed: set[str] | None = None
) -> dict[str, int]:
    """The baseline minus stale rows, with counts lowered to the current size.

    Only rows of files in *changed* are edited (every row when it is None), so a
    refresh never touches someone else's row. Built only from rows already
    present, so it can never add a path, and a count only ever moves down.
    """
    if baseline.problems:
        raise CheckError(
            "the baseline is malformed; fix it by hand before --update-baseline:\n  "
            + "\n  ".join(baseline.problems)
        )
    rows: dict[str, int] = {}
    for path, recorded in baseline.rows.items():
        if not _in_scope(path, changed):
            rows[path] = recorded
        elif path not in verdict.stale:
            rows[path] = min(recorded, verdict.lowerable.get(path, recorded))
    return rows


def update_baseline(root: Path, scope: Scope) -> tuple[int, int, str]:
    """Rewrite the baseline in canonical form; return (rows deleted, counts lowered, note).

    Refuses when the rows to edit would be every row by accident (no scope, or a
    baseline edit whose base copy cannot be read): rewriting every row is what
    ``--whole-tree`` asks for, and nothing else may do it.
    """
    target = root / BASELINE_RELPATH
    baseline = read_baseline(root)
    judged, _, note = rescope_baseline_edit(scope, baseline.rows)
    if judged is None and not scope.whole_tree_requested:
        why = note or scope.label
        raise CheckError(
            f"--update-baseline cannot tell which files this change touches ({why}), so it "
            "would rewrite every row, including other changes' rows. Delete your own rows "
            "by hand, or pass --whole-tree to rewrite the whole baseline on purpose."
        )
    verdict = evaluate(scan(root, tracked_files(root)), baseline, judged)
    rows = pruned_rows(baseline, verdict, judged)
    if target.exists():
        staging = target.with_name(f"{target.name}.update")
        with open(staging, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(render_baseline(rows))
        os.replace(staging, target)
    deleted = len(baseline.rows) - len(rows)
    lowered = sum(1 for path, count in rows.items() if count < baseline.rows[path])
    return deleted, lowered, note


def _print_verdict(verdict: Verdict) -> None:
    if verdict.scope_note:
        print(f"note: {verdict.scope_note}")
    for report in verdict.reports:
        print(f"REPORT {report}")
    for warning in verdict.warnings:
        print(f"WARN {warning}")
    for failure in verdict.failures:
        print(f"FAIL {failure}")
    if verdict.lowerable:
        print(
            f"note: {len(verdict.lowerable)} baselined file(s) shrank but are still over "
            f"the cap; `{_UPDATE_HINT}` lowers their rows."
        )
    print(
        f"check_file_line_cap: {verdict.checked} code files checked, "
        f"{len(verdict.failures)} failure(s), {len(verdict.warnings)} main-drift "
        f"warning(s), {len(verdict.reports)} report(s)."
    )


def _self_test(workdir: Path) -> list[str]:
    """Exercise every verdict kind on a synthetic tree; return what failed."""
    failures: list[str] = []

    def expect(name: str, cond: bool) -> None:
        if not cond:
            failures.append(name)

    def write(rel: str, lines: int, *, terminated: bool = True) -> None:
        target = workdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        body = b"x\n" * lines
        target.write_bytes(body if terminated else body[:-1])

    expect("an empty file has no lines", count_lines(b"") == 0)
    expect("an unterminated line counts", count_lines(b"a") == 1)
    expect("a terminated line counts once", count_lines(b"a\n") == 1)
    expect("CRLF counts like LF", count_lines(b"a\r\nb") == 2)
    expect("a NUL in the probe window is binary", is_binary(b"x\0y"))
    expect(
        "a NUL past the probe window is not binary",
        not is_binary(b"x" * BINARY_PROBE_BYTES + b"\0"),
    )

    write("at_cap.py", CAP)
    write("over.py", CAP + 1)
    write("unterminated.ts", CAP, terminated=False)
    write("grew.py", CAP + 50)
    write("kept.py", CAP + 40)
    write("shrank.py", CAP + 10)
    write("now_small.py", 10)
    write("gen/registry.gen.ts", CAP * 2)
    write("notes.txt", CAP * 2)
    (workdir / "blob.js").write_bytes(b"\0" + b"x\n" * (CAP * 2))
    paths = [
        "at_cap.py",
        "over.py",
        "unterminated.ts",
        "grew.py",
        "kept.py",
        "shrank.py",
        "now_small.py",
        "gen/registry.gen.ts",
        "notes.txt",
        "blob.js",
    ]
    baseline = parse_baseline(
        HEADER + f"{CAP + 1} blob.js\n"
        f"{CAP + 1} gen/registry.gen.ts\n"
        f"{CAP + 10} gone.py\n"
        f"{CAP + 1} grew.py\n"
        f"{CAP + 40} kept.py\n"
        f"{CAP + 5} now_small.py\n"
        f"{CAP + 20} shrank.py\n"
    )
    expect("the canonical header parses clean", not baseline.problems)
    verdict = evaluate(scan(workdir, paths), baseline)
    expect("exempt, binary and non-code files are not checked", verdict.checked == 7)
    expect(
        "an unlisted file over the cap fails, one at the cap does not",
        sum(f.startswith("over.py ") for f in verdict.failures) == 1
        and not any(f.startswith(("at_cap.py", "unterminated.ts")) for f in verdict.failures),
    )
    expect(
        "every stale row fails (gone, under the cap, exempt, binary)",
        verdict.stale == {"blob.js", "gen/registry.gen.ts", "gone.py", "now_small.py"}
        and len(verdict.failures) == 5,
    )
    expect(
        "growth is reported, not failed, and a file at its row is not reported",
        [r.split()[0] for r in verdict.reports] == ["grew.py"],
    )
    expect("a shrunken row can be lowered", verdict.lowerable == {"shrank.py": CAP + 10})
    expect(
        "--update-baseline deletes and lowers, never adds or raises",
        pruned_rows(baseline, verdict)
        == {"grew.py": CAP + 1, "kept.py": CAP + 40, "shrank.py": CAP + 10},
    )

    # Scoped to a change that touches neither the files nor the baseline, every
    # violation is main drift: a warning, never a failure.
    measured = scan(workdir, paths)
    untouched = evaluate(measured, baseline, {"unrelated.py"})
    expect(
        "untouched violations are main-drift warnings, not failures",
        not untouched.failures
        and len(untouched.warnings) == 5
        and all(w.startswith("main drift: ") for w in untouched.warnings),
    )
    touched = evaluate(measured, baseline, {"over.py", "gone.py"})
    expect(
        "a violation in a touched file fails",
        sorted(f.split()[0] for f in touched.failures) == ["over.py", "stale"]
        and len(touched.warnings) == 3,
    )
    # A change that edits the baseline is judged by the rows it changed. Here it
    # deletes now_small.py's row; the other stale rows stay main drift.
    shrunk = {p: c for p, c in baseline.rows.items() if p != "now_small.py"}
    base_scope = Scope({BASELINE_RELPATH, "now_small.py"}, "test", dict(baseline.rows))
    judged, edit_failures, _ = rescope_baseline_edit(base_scope, shrunk)
    expect(
        "a deletion-only baseline edit is judged by the rows it changed",
        judged == {"now_small.py"} and not edit_failures,
    )
    others_stale = evaluate(measured, Baseline(rows=shrunk), judged)
    expect(
        "someone else's stale row beside a deletion-only edit is main drift",
        not others_stale.failures
        and sum("stale baseline row" in w for w in others_stale.warnings) == 3,
    )
    _, added, _ = rescope_baseline_edit(base_scope, {**shrunk, "new.py": CAP + 5})
    _, raised, _ = rescope_baseline_edit(base_scope, {**shrunk, "grew.py": CAP + 99})
    expect(
        "a baseline edit that adds a path or raises a count fails",
        len(added) == 1
        and "adds the baseline row" in added[0]
        and len(raised) == 1
        and "raises the baseline row" in raised[0],
    )
    lowered = parse_baseline(render_baseline({**shrunk, "kept.py": CAP + 20}))
    too_low = judge(measured, lowered, Scope({BASELINE_RELPATH}, "test", dict(baseline.rows)))
    expect(
        "a row lowered below its file's size fails instead of reporting growth",
        any("lower it to" in f for f in too_low.failures)
        and not any(r.startswith("kept.py ") for r in too_low.reports),
    )
    expect(
        "an unreadable base baseline judges the whole tree",
        rescope_baseline_edit(Scope({BASELINE_RELPATH}, "test"), shrunk)[0] is None,
    )
    expect(
        "a scoped --update-baseline edits only the touched files' rows",
        pruned_rows(baseline, untouched, {"shrank.py", "now_small.py"})
        == {
            "blob.js": CAP + 1,
            "gen/registry.gen.ts": CAP + 1,
            "gone.py": CAP + 10,
            "grew.py": CAP + 1,
            "kept.py": CAP + 40,
            "shrank.py": CAP + 10,
        },
    )
    malformed = parse_baseline(f"{CAP + 1} b.py\n{CAP + 1} a.py\n")
    expect(
        "a malformed baseline fails whatever the scope",
        bool(evaluate(Scan(), malformed, {"unrelated.py"}).failures),
    )

    for name, bad in (
        ("unsorted rows", f"{CAP + 1} b.py\n{CAP + 1} a.py\n"),
        ("a repeated row", f"{CAP + 1} a.py\n{CAP + 1} a.py\n"),
        ("a count within the cap", f"{CAP} a.py\n"),
        ("a count with a leading zero", f"0{CAP + 1} a.py\n"),
        ("a row without a count", "a.py\n"),
        ("a comment after the rows", f"{CAP + 1} a.py\n# late comment\n"),
        ("a backslash path", f"{CAP + 1} src\\a.py\n"),
    ):
        expect(f"{name} is malformed", bool(parse_baseline(bad).problems))
    try:
        pruned_rows(parse_baseline("a.py\n"), Verdict())
    except CheckError:
        pass
    else:
        failures.append("--update-baseline refuses a malformed baseline")
    expect("every exemption carries a reason", all(e.reason and e.patterns for e in EXEMPTIONS))
    if (ROOT / ".git").exists():
        # An enumeration that silently came back empty would pass every check.
        expect(
            "git enumeration of this checkout reaches its scripts",
            any(p.startswith("scripts/") and is_code_path(p) for p in tracked_files(ROOT)),
        )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--update-baseline",
        action="store_true",
        help="delete stale rows and lower counts of files this change touches; never adds",
    )
    mode.add_argument("--test", action="store_true", help="run the self-test")
    parser.add_argument("--root", type=Path, default=ROOT, help="the checkout to check")
    parser.add_argument(
        "--whole-tree",
        action="store_true",
        help="judge every file, and let --update-baseline rewrite every row, not only "
        "the files this change touches",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()

    try:
        if args.test:
            with tempfile.TemporaryDirectory(prefix="line-cap-self-test-") as workdir:
                failed = _self_test(Path(workdir))
            for name in failed:
                print(f"check_file_line_cap self-test FAILED: {name}", file=sys.stderr)
            if failed:
                return 1
            print("check_file_line_cap self-test: all checks passed")
            return 0
        scope = change_scope(root, whole_tree=args.whole_tree)
        print(
            f"check_file_line_cap scope: {scope.label}"
            + ("" if scope.changed is None else f" ({len(scope.changed)} changed file(s))")
        )
        if args.update_baseline:
            deleted, lowered, note = update_baseline(root, scope)
            if note:
                print(f"note: {note}")
            print(
                f"check_file_line_cap: {deleted} row(s) deleted, {lowered} count(s) "
                f"lowered in {BASELINE_RELPATH}."
            )
            return 0
        verdict = check(root, scope)
    except CheckError as exc:
        print(f"check_file_line_cap: {exc}", file=sys.stderr)
        return 2
    _print_verdict(verdict)
    return 1 if verdict.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
