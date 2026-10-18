#!/usr/bin/env python3
"""copy_living_ui_blocks.py -- copy the Living UI toolkit's helpers into its skill.

    python3 scripts/copy_living_ui_blocks.py           # rewrite the blocks in SKILL.md
    python3 scripts/copy_living_ui_blocks.py --check   # exit 1 when a block differs

``toolkit/living-ui.js`` in the ``living-ui`` built-in skill is the one place the
toolkit's code is edited. Each helper in it sits between ``// living-ui:begin
<name>`` and ``// living-ui:end <name>``. A crewmate with no file tools cannot open
that file, so the skill's ``SKILL.md`` carries every helper as its own fenced
block, between a ``<!-- living-ui:blocks begin`` line and a ``<!-- living-ui:blocks
end -->`` line, and an author copies the blocks a figure needs from there.

This script writes that region from the file, each helper byte for byte, under a
heading naming it. ``--check`` names every helper whose block is missing, extra or
different, and ``test/test_living_ui_toolkit.py`` runs the same check, so an edit to
the file that skips this script fails CI.

Outside the marked blocks the file may hold only ``//`` comments and blank lines.
A line of code there would reach nobody who copies from SKILL.md, so it is refused
rather than skipped.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = ROOT / "src" / "kiro_crew" / "builtin_skills" / "living-ui"
TOOLKIT = SKILL_DIR / "toolkit" / "living-ui.js"
SKILL_MD = SKILL_DIR / "SKILL.md"

_BEGIN = re.compile(r"^// living-ui:begin ([A-Za-z][A-Za-z0-9]*)$")
_END = re.compile(r"^// living-ui:end ([A-Za-z][A-Za-z0-9]*)$")
REGION_BEGIN = "<!-- living-ui:blocks begin"
REGION_END = "<!-- living-ui:blocks end -->"
# One copied block as render() writes it: its heading, then a fence of three or
# more backticks with the js tag, the code, and the same fence again.
_COPIED = re.compile(r"^#### `([A-Za-z][A-Za-z0-9]*)`\n\n(`{3,})js\n(.*?)^\2$", re.M | re.S)


class BlockError(ValueError):
    """The toolkit file or SKILL.md is not in the shape this script reads."""


def read_blocks(source: str) -> list[tuple[str, str]]:
    """Every marked helper in *source*, as ``(name, code)`` in file order.

    *code* is every line between the two marker lines, each ending in a newline.
    Lines are split on ``\\n`` only: ``str.splitlines`` also breaks at characters
    such as U+2028, which may sit inside a JavaScript string, so the copy would
    no longer equal the file.
    """
    blocks: list[tuple[str, str]] = []
    name: str | None = None
    opened = 0
    body: list[str] = []
    for number, line in enumerate(source.split("\n"), start=1):
        begin, end = _BEGIN.match(line), _END.match(line)
        if name is None:
            if begin:
                name, opened, body = begin.group(1), number, []
            elif end:
                raise BlockError(f"line {number}: 'end {end.group(1)}' closes no block")
            elif line.strip() and not line.lstrip().startswith("//"):
                raise BlockError(f"line {number}: code outside a marked block: {line.strip()!r}")
            continue
        if begin:
            raise BlockError(f"line {number}: 'begin {begin.group(1)}' inside block {name!r}")
        if end:
            if end.group(1) != name:
                raise BlockError(f"line {number}: 'end {end.group(1)}' closes block {name!r}")
            if not "".join(body).strip():
                raise BlockError(f"line {opened}: block {name!r} is empty")
            blocks.append((name, "".join(f"{text}\n" for text in body)))
            name = None
            continue
        body.append(line)
    if name is not None:
        raise BlockError(f"line {opened}: block {name!r} is never closed")
    if not blocks:
        raise BlockError("no marked helper found")
    names = [block_name for block_name, _ in blocks]
    twice = sorted({block_name for block_name in names if names.count(block_name) > 1})
    if twice:
        raise BlockError(f"helper marked more than once: {', '.join(twice)}")
    return blocks


def _fence(code: str) -> str:
    """A backtick fence longer than any run of backticks inside *code*."""
    longest = max((len(run) for run in re.findall(r"`+", code)), default=0)
    return "`" * max(3, longest + 1)


def render(blocks: list[tuple[str, str]]) -> str:
    """The text between the region's marker lines: a heading and a fenced block each."""
    copies = [f"#### `{name}`\n\n{_fence(code)}js\n{code}{_fence(code)}\n" for name, code in blocks]
    return "\n" + "\n".join(copies) + "\n"


def split_region(skill: str) -> tuple[str, str, str]:
    """``(text through the begin line, the region, text from the end line)``."""
    if skill.count(REGION_BEGIN) != 1 or skill.count(REGION_END) != 1:
        raise BlockError(f"SKILL.md needs exactly one {REGION_BEGIN!r} and one {REGION_END!r}")
    begin = skill.index(REGION_BEGIN)
    end = skill.index(REGION_END)
    head_end = skill.find("\n", begin) + 1
    if head_end == 0 or head_end > end:
        raise BlockError(f"SKILL.md: {REGION_END!r} must start a line after {REGION_BEGIN!r}")
    return skill[:head_end], skill[head_end:end], skill[end:]


def copied_blocks(region: str) -> list[tuple[str, str]]:
    """The ``(name, code)`` blocks a SKILL.md region carries, in order."""
    return [(match.group(1), match.group(3)) for match in _COPIED.finditer(region)]


def drift(blocks: list[tuple[str, str]], region: str) -> list[str]:
    """What keeps *region* from being the copy of *blocks*; empty when it is."""
    copied = copied_blocks(region)
    have = dict(copied)
    found: list[str] = []
    for name, code in blocks:
        if name not in have:
            found.append(f"{name}: missing from SKILL.md")
        elif have[name] != code:
            found.append(f"{name}: SKILL.md block differs from toolkit/living-ui.js")
    marked = {name for name, _ in blocks}
    found.extend(
        f"{name}: in SKILL.md but not marked in the file"
        for name, _ in copied
        if name not in marked
    )
    if not found and region != render(blocks):
        found.append("the region's order or layout differs from what this script writes")
    return found


def check() -> list[str]:
    """Drift between the committed SKILL.md and the toolkit file; empty when current."""
    blocks = read_blocks(TOOLKIT.read_text(encoding="utf-8"))
    _, region, _ = split_region(SKILL_MD.read_text(encoding="utf-8"))
    return drift(blocks, region)


def write() -> bool:
    """Rewrite SKILL.md's region from the toolkit file; True when its bytes changed."""
    blocks = read_blocks(TOOLKIT.read_text(encoding="utf-8"))
    skill = SKILL_MD.read_text(encoding="utf-8")
    head, region, tail = split_region(skill)
    fresh = render(blocks)
    if region == fresh:
        return False
    with SKILL_MD.open("w", encoding="utf-8", newline="\n") as out:
        out.write(head + fresh + tail)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="change nothing; exit 1 when a SKILL.md block differs from the file",
    )
    args = parser.parse_args(argv)
    try:
        if args.check:
            found = check()
            for line in found:
                print(f"drift: {line}", file=sys.stderr)
            if found:
                print("Run: python3 scripts/copy_living_ui_blocks.py", file=sys.stderr)
                return 1
            print("SKILL.md blocks match toolkit/living-ui.js")
            return 0
        changed = write()
    except (BlockError, OSError) as exc:
        print(f"copy_living_ui_blocks: {exc}", file=sys.stderr)
        return 2
    print("rewrote the SKILL.md blocks" if changed else "SKILL.md blocks already match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
