#!/usr/bin/env python3
"""check_black_formatting.py -- black as a real gate, behind a shrinking baseline.

## The problem this replaces

``AGENTS.md`` lists ``black src/kiro_crew test`` as a gate to run before every
commit, and CI does not check it. That combination is worse than having no gate,
because the instruction is not merely unenforced, it is actively harmful to
follow: 1,420 files under ``src/`` and ``test/`` are not black-clean, so running
the documented command reformats about 95,800 lines that have nothing to do with
the change being made. A contributor who obeys the documentation buries their own
diff; one who notices has to knowingly skip a documented gate and hope no
reviewer objects. Both outcomes were observed on real PRs.

The bulk format pass that would make the command safe has been "pending" long
enough for the note in ``ci.yml`` to go stale by a factor of three (it estimates
~406 files). Blocking enforcement on that pass landing is what let this rot, so
this gate does not wait for it.

## The ratchet

Every file is required to be black-clean *except* those recorded in the baseline:

* a file **not** in the baseline must be clean -- no new unformatted code lands,
  which is the property the missing gate was supposed to provide;
* a file **in** the baseline that has *become* clean must be **removed** from it,
  so the list shrinks instead of rotting.

Nothing demands that a baselined file be reformatted, so no PR is forced to carry
unrelated churn. Touching one is still free: it stays listed until someone
formats it deliberately.

This deliberately mirrors ``check_per_file_coverage.py`` -- same offender/graduate
vocabulary, same prune-only refresh -- because the repository already solved
"large pre-existing violation set, must only shrink" once, and a second shape for
the same problem is a second thing to learn.

## Scope

The "new offender" verdict covers only the files THIS change touches. Judging the
whole tree sounds stricter but is not: the base branch keeps merging files that
are not black-clean until this gate is live, and CI evaluates a PR's merge ref, so
an unscoped gate reddens a PR for someone else's file with no rebase involved. That
happened three times while landing this, which is how the scoping earned its place.

When no base ref is resolvable -- a bare push, a detached checkout -- the scope
falls back to the whole tree rather than to nothing: a scoping mechanism that fails
open would disable the gate exactly when its inputs are unusual.

## Refreshing

``--update-baseline`` only ever DELETES lines: entries that are now clean, and
entries whose file is gone. It never adds a path, so a new offender can never be
silenced by running the refresh -- that is the one rule which keeps the gate from
being a formality. There is deliberately no operation that adds one.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / ".github" / "black-baseline.txt"
DEFAULT_TARGETS = ("src", "test")
# Pinned so the gate cannot start disagreeing with the documented command when a
# contributor's black defaults differ. Matches ci.yml and AGENTS.md.
TARGET_VERSION = "py310"
WOULD_REFORMAT = re.compile(r"^would reformat (.+)$")
HEADER = """\
# Files that are not black-clean yet. The gate requires every OTHER file to be
# clean, so this list can only shrink.
#
# Do NOT add a path here to make a red gate green: a new offender means the file
# needs `black --target-version py310 <path>`, not an exemption. The refresh
# command below only deletes lines, so it cannot add one for you.
#
# Refresh (after formatting something listed here):
#   python3 scripts/check_black_formatting.py --update-baseline
"""


def _unformatted(targets: tuple[str, ...]) -> set[str]:
    """Return repo-relative paths black would reformat, via one black run."""
    existing = [name for name in targets if (ROOT / name).exists()]
    if not existing:
        raise SystemExit(f"none of the targets {targets} exist under {ROOT}")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "black",
            "--check",
            "--target-version",
            TARGET_VERSION,
            *existing,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    # black exits 1 for "would reformat", 123 for an internal error. Only the
    # former is a verdict; the latter must not read as "everything is clean".
    if proc.returncode not in (0, 1):
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"black failed with exit code {proc.returncode}")
    found: set[str] = set()
    for line in proc.stderr.splitlines():
        match = WOULD_REFORMAT.match(line.strip())
        if not match:
            continue
        # black reports ABSOLUTE paths even for relative arguments, and an
        # absolute path in a committed baseline matches nothing on any other
        # checkout -- CI included -- so the gate would silently flag all 1,420
        # files as new offenders. Store repo-relative paths only.
        raw = Path(match.group(1))
        try:
            relative = raw.resolve().relative_to(ROOT)
        except ValueError:
            raise SystemExit(f"black reported a path outside the repository: {raw}")
        found.add(relative.as_posix())
    # Exit 1 with nothing parsed is NOT "zero offenders". `python -m black` also
    # exits 1 when the module is absent, so an environment without black would
    # otherwise look like a fully formatted tree -- and `--update-baseline` would
    # then write an EMPTY baseline over 1,420 recorded paths, destroying the
    # ratchet in a way no gate run afterwards could detect. Exit 0 with nothing
    # parsed is the real "all clean" answer and stays allowed.
    if proc.returncode == 1 and not found:
        sys.stderr.write(proc.stderr)
        raise SystemExit(
            "black exited 1 but reported no 'would reformat' file. Refusing to "
            "treat that as a clean tree: it is what a missing or broken black "
            "looks like, and acting on it would erase the baseline."
        )
    return found


def _git(*args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return proc.returncode, proc.stdout


def _changed_paths() -> tuple[set[str] | None, str]:
    """This change's paths plus how they were determined, for the log.

    Several checkout shapes have to work and they fail in ways that look alike, so
    each attempt is named and the winner is printed. Guessing silently is what let
    an earlier version of this gate fall back to whole-tree scope on CI without
    saying so, and then report a file the base branch had merged.

    * A ``pull_request`` checkout leaves HEAD as the MERGE commit, whose tree is
      the base tree plus this change. So ``diff HEAD^1 HEAD`` is exactly this
      change and needs no merge base -- only HEAD and its first parent.
    * ``diff HEAD^1 HEAD^2`` is equivalent but needs BOTH parents' trees, which a
      shallow clone may not have.
    * Locally HEAD is the branch tip, so the three-dot diff against the base
      branch is the right question.

    None means undeterminable, and the caller must then judge the whole tree
    rather than nothing: a scope that fails open disables the gate exactly when
    its inputs are unusual.
    """
    code, out = _git("rev-list", "--parents", "-n", "1", "HEAD")
    is_merge = code == 0 and len(out.split()) >= 3
    attempts: list[tuple[str, list[str]]] = []
    if is_merge:
        attempts.append(("merge HEAD^1..HEAD", ["diff", "--name-only", "HEAD^1", "HEAD"]))
        attempts.append(("merge parents", ["diff", "--name-only", "HEAD^1", "HEAD^2"]))
    for base in ("origin/main", "main"):
        attempts.append((f"{base}...HEAD", ["diff", "--name-only", f"{base}...HEAD"]))
    for label, args in attempts:
        code, out = _git(*args)
        if code == 0:
            return {line.strip() for line in out.splitlines() if line.strip()}, label
    return None, "undeterminable (judging the whole tree)"


def _read_baseline(path: Path) -> list[str]:
    if not path.is_file():
        raise SystemExit(
            f"baseline {path} is missing; restore it from git rather than regenerating "
            "it, since a regenerated baseline would silently absorb every offender "
            "added since it was recorded"
        )
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _write_baseline(path: Path, paths: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{entry}\n" for entry in sorted(paths))
    path.write_text(HEADER + body, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="prune entries that are now clean or gone; never adds a path",
    )
    args = parser.parse_args(argv)

    unformatted = _unformatted(DEFAULT_TARGETS)

    baseline = set(_read_baseline(args.baseline))

    if args.update_baseline:
        survivors = baseline & unformatted
        pruned = len(baseline) - len(survivors)
        _write_baseline(args.baseline, survivors)
        print(f"pruned {pruned} entr(y/ies); {len(survivors)} remain")
        return 0

    changed, scope_label = _changed_paths()
    unlisted = unformatted - baseline
    print(f"black gate scope: {scope_label}", end="")
    print("" if changed is None else f" ({len(changed)} changed file(s))")
    if changed is None:
        new_offenders = sorted(unlisted)
    else:
        # Only THIS change's files can be its offenders. Without this the gate
        # reports files the base branch merged after the baseline was recorded,
        # so a PR's colour would depend on other people's formatting hygiene --
        # observed three times while landing this gate, once per rebase.
        new_offenders = sorted(unlisted & changed)
    graduated = sorted(baseline - unformatted)

    for path in new_offenders:
        print(
            f"::error file={path}::not black-formatted. Run: black --target-version {TARGET_VERSION} {path}"
        )
    if graduated:
        print(
            f"::error::{len(graduated)} baselined file(s) are now black-clean. "
            "Remove them so the baseline keeps shrinking: "
            "python3 scripts/check_black_formatting.py --update-baseline"
        )
        for path in graduated:
            print(f"  {path}")

    if new_offenders or graduated:
        print(
            f"\nblack gate FAILED: {len(new_offenders)} new offender(s), "
            f"{len(graduated)} graduated entr(y/ies) to prune."
        )
        return 1

    print(
        "black gate passed: nothing in scope is unformatted outside the baseline "
        f"({len(unformatted)} known-unformatted file(s) still listed)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
