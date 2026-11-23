#!/usr/bin/env python3
"""check_skill_split.py -- prove a skill split kept every rule line.

A built-in ``SKILL.md`` that grows past one engine read is split: a short core
stays in ``SKILL.md`` and detail moves, verbatim, to files under the skill's
``references/``. A moved line is easy to lose and hard to spot in review, so this
script maps every non-blank line of each ``SKILL.md`` at a base ref to where it
lives now: the core, a reference file of the same skill, or nowhere.

    python3 scripts/check_skill_split.py [--base origin/main] [--all]

By default only skills whose ``SKILL.md`` changed since the base are listed; ``--all``
lists every built-in skill. Lines compare after stripping surrounding whitespace,
so a moved list item keeps its match when its indent changes, and a markdown
link's target is ignored, since moving a line into ``references/`` rebases it. Exit 0 when no
line is missing, 1 when one is (each is printed), 2 when git cannot read the base.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import NamedTuple

SKILLS_ROOT = Path("src/kiro_crew/builtin_skills")


class Coverage(NamedTuple):
    total: int
    in_core: int
    in_refs: int
    missing: list[str]


#: A markdown link's target. A line moved one directory down has its relative
#: targets rebased (``../x`` becomes ``../../x``, ``#a`` becomes ``../SKILL.md#a``),
#: so targets are left out of the comparison; the text around them is not.
_LINK_TARGET = re.compile(r"\]\([^)\s]*\)")


def rule_lines(text: str) -> list[str]:
    """The non-blank lines of *text*, stripped, link targets blanked, in order."""
    return [_LINK_TARGET.sub("]()", line.strip()) for line in text.splitlines() if line.strip()]


def classify(base: str, core: str, refs: dict[str, str]) -> Coverage:
    """Where each rule line of *base* lives now: the *core*, one of *refs*, or nowhere.

    Copies are counted: a line the base holds three times needs three copies
    across the core and *refs*, so dropping one of a repeated rule shows.
    """
    in_core_left = Counter(rule_lines(core))
    in_refs_left = Counter(line for text in refs.values() for line in rule_lines(text))
    in_core = in_refs = 0
    missing: list[str] = []
    for line in rule_lines(base):
        if in_core_left[line]:
            in_core_left[line] -= 1
            in_core += 1
        elif in_refs_left[line]:
            in_refs_left[line] -= 1
            in_refs += 1
        else:
            missing.append(line)
    return Coverage(len(rule_lines(base)), in_core, in_refs, missing)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, encoding="utf-8", check=False
    )


def _base_skill_files(base: str) -> list[str]:
    res = _git("ls-tree", "-r", "--name-only", base, "--", str(SKILLS_ROOT))
    if res.returncode != 0:
        raise SystemExit(f"error: cannot list {SKILLS_ROOT} at {base}: {res.stderr.strip()}")
    return [p for p in res.stdout.splitlines() if p.endswith("/SKILL.md")]


def _skill_refs(skill_dir: Path) -> dict[str, str]:
    """Every markdown file of the skill except its core, keyed by path under the skill."""
    return {
        p.relative_to(skill_dir).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(skill_dir.rglob("*.md"))
        if p.name != "SKILL.md" or p.parent != skill_dir
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--all", action="store_true", help="list unchanged skills too")
    args = parser.parse_args(argv)
    try:
        paths = _base_skill_files(args.base)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2
    print("| skill | rule lines | in core | in references | missing |")
    print("|---|---:|---:|---:|---:|")
    lost: list[tuple[str, str]] = []
    # A skill that did not exist at the base was split out of the one it names on
    # its "Moved verbatim from the `<name>` skill" line, and counts for that one only.
    new_skills: dict[str, dict[str, str]] = {}
    for p in sorted(SKILLS_ROOT.rglob("SKILL.md")):
        if p.as_posix() in set(paths):
            continue
        text = p.read_text(encoding="utf-8")
        source = re.search(r"Moved verbatim from the `([^`]+)` skill", text)
        if source:
            new_skills.setdefault(source.group(1), {})[p.as_posix()] = text
    for path in paths:
        base_text = _git("show", f"{args.base}:{path}").stdout
        core_path = Path(path)
        core = core_path.read_text(encoding="utf-8") if core_path.exists() else ""
        if core == base_text and not args.all:
            continue
        name = core_path.parent.relative_to(SKILLS_ROOT).as_posix()
        cov = classify(
            base_text, core, {**_skill_refs(core_path.parent), **new_skills.get(name, {})}
        )
        print(f"| {name} | {cov.total} | {cov.in_core} | {cov.in_refs} | {len(cov.missing)} |")
        lost.extend((name, line) for line in cov.missing)
    for name, line in lost:
        print(f"MISSING {name}: {line}")
    return 1 if lost else 0


if __name__ == "__main__":
    sys.exit(main())
