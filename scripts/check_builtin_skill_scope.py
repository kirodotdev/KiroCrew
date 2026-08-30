#!/usr/bin/env python3
"""check_builtin_skill_scope.py -- keep this repository out of shipped skills.

Every file under ``src/kiro_crew/builtin_skills/`` installs on every machine that
installs Kiro Crew. A skill body that names THIS repository -- its GitHub slug, a
source path only a dev checkout has, a test file, a workflow file -- is giving an
instruction that resolves for exactly one reader: someone working on Kiro Crew
itself. For everyone else it is a dead reference, and for an agent it is worse
than dead: a confident pointer at a path that does not exist, which the agent
will try.

``prepare-pr`` already solved this properly and is the pattern this gate keeps:
repository specifics live in a PROFILE keyed by repository
(``prepare-pr/profiles/``, resolved by ``resolve_profile.py``), so the skill body
says "run the gate floor" and the profile says what that means here. Anything
repository-shaped that a skill needs either resolves from the repository being
acted on, or belongs in a profile, or the skill is not a builtin.

## Nothing is exempted by record

The rule is absolute: a marker outside ``kirocrew-dev/`` fails. There is no
baseline file and no per-file allowance, so the gate has nothing to forgive and
cannot drift into forgiving everything. A repository shape that a skill genuinely
needs is a signal to move the specific into a profile, not to record an exception.

## What counts as a marker

Four classes, all of which are only meaningful while working on this repository:

* ``kirodotdev/KiroCrew`` -- this repository by name, including a github.com URL.
* ``src/kiro_crew/...`` -- a SOURCE-CHECKOUT path. An installed wheel has
  ``kiro_crew/`` with no ``src/`` prefix, so this form resolves only in a clone.
* ``test/test_*.py`` -- this repository's test layout.
* ``.github/workflows/*.yml`` -- this repository's CI.

## What is deliberately NOT a marker

The PRODUCT surface. ``kirocrew`` CLI commands, ``~/.kiro/crew/...`` runtime
paths, config keys, dashboard routes, MCP tool names: those ship WITH the skill
and are correct on every install. Confusing the two would gate the very content
builtin skills exist to carry, so the rule names repository shapes only and this
list is the reason the marker set is narrow rather than "any path".

## What a skill body must be

A regular UTF-8 text file. A symlink is refused before any read -- one pointing at
``/dev/zero`` makes the read stream nulls until CI kills the job -- and that closes
the whole class, because git carries only regular files, symlinks and gitlinks, so
there is no other route to a device file or FIFO in a checkout. A body that cannot
be decoded fails too, rather than being skipped: a gate that silently passes what
it could not read ships the one file nobody scanned.

## Known limit, on purpose

Repository paths OUTSIDE the four classes are not caught -- ``website/src/
index.css`` and ``docs/system-specs/...`` are as checkout-only as
``src/kiro_crew/``, and ``theme-pack-authoring`` legitimately cites the first one
today. Widening the set means fixing those references in the same breath, which is
a content change with its own argument to make; this gate covers the four shapes
that were actually leaking. Read it as "these four shapes are extinct", not as
"no repository path can reach a skill".

## The kirocrew-dev family is exempt, structurally

``builtin_skills/kirocrew-dev/`` is the family FOR developing this repository --
``prepare-pr``, ``babysit``, ``writing-tests``, ``kirocrew-worktree-dev``. A
Kiro Crew path there is the subject matter, not a leak, and 26 of the tree's 28
markers live in it. That exemption is a directory rule rather than 26 recorded
lines on purpose: it is a property of what the family IS, so it stays true as
those skills are edited, where a per-line list would just record today's bytes
and demand churn on every edit.

It is matched as the family directory DIRECTLY beneath ``builtin_skills/``, not as
any path segment: a nested ``widgets/kirocrew-dev/`` would otherwise inherit the
exemption and ship a leak under a name that only looks exempt.

The consequence to be honest about: this gate does NOT judge whether that family
should ship to every install at all. It only stops the leak spreading to skills
that claim to be repository-agnostic.

## Usage

    python3 scripts/check_builtin_skill_scope.py             # gate
    python3 scripts/check_builtin_skill_scope.py --test      # self-test the rule
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOT = Path("src") / "kiro_crew" / "builtin_skills"

# The family whose subject matter IS this repository.
EXEMPT_FAMILY = "kirocrew-dev"

# One pattern per marker class, kept separate so a violation can name WHICH
# class it tripped -- "this is a source-checkout path" is actionable where
# "matched the marker regex" is not.
MARKER_CLASSES: tuple[tuple[str, str, str], ...] = (
    (
        "repo-slug",
        # NOT preceded by "github.com/": a bare slug ("file it on
        # kirodotdev/KiroCrew") is an instruction only a contributor can act on,
        # but an https://github.com/kirodotdev/KiroCrew/... URL RESOLVES on every
        # machine because this repository is public, so it is a citation. Treating
        # both alike forces a downgrade -- it turns a fetchable link to the
        # authoritative theming contract into prose an installed reader cannot
        # follow at all -- on every skill that cites the project's own public docs.
        # The lookbehind is deliberately narrow rather than a whole-line
        # exemption: one line can carry both a citation and a real instruction.
        r"(?<!github\.com/)kirodotdev/KiroCrew",
        "names this repository as a place to act; a reader elsewhere has no such repo",
    ),
    (
        "checkout-path",
        r"\bsrc/kiro_crew/[A-Za-z0-9_./-]+",
        "a source-checkout path; an installed wheel has no src/ prefix",
    ),
    (
        "test-path",
        r"\btest/test_[A-Za-z0-9_]+\.py",
        "this repository's test layout",
    ),
    (
        "workflow-path",
        r"\.github/workflows/[A-Za-z0-9._-]+\.ya?ml",
        "this repository's CI",
    ),
)

REMEDY = (
    "Resolve it from the repository being acted on, move it into a profile the\n"
    "way prepare-pr/profiles does, or move the skill under builtin_skills/\n"
    "kirocrew-dev/ if it is genuinely a skill for developing this repository."
)


def marker_hits(text: str) -> list[tuple[int, str, str]]:
    """(line_no, class_name, matched_text) for every marker in ``text``.

    Line-numbered because a count alone cannot be acted on, and one line can
    carry two classes (a github.com URL that also contains a source path), so
    each class is scanned independently rather than through one alternation.
    """
    hits: list[tuple[int, str, str]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for name, pattern, _why in MARKER_CLASSES:
            for match in re.finditer(pattern, line):
                hits.append((line_no, name, match.group(0)))
    hits.sort()
    return hits


def is_exempt(rel_path: Path) -> bool:
    """Whether ``rel_path`` is inside the family this repository is the subject of.

    The family must sit DIRECTLY beneath ``builtin_skills/``. Matching any path
    segment would exempt a nested ``widgets/kirocrew-dev/SKILL.md``, and matching
    a prefix would exempt a ``kirocrew-devtools/`` sibling -- both would ship a
    leak under a name that merely resembles the exempt one.
    """
    prefix = SKILL_ROOT.parts
    parts = rel_path.parts
    if parts[: len(prefix)] != prefix:
        return False
    return len(parts) > len(prefix) and parts[len(prefix)] == EXEMPT_FAMILY


def scan_tree(root: Path) -> tuple[dict[str, list[tuple[int, str, str]]], list[tuple[str, str]]]:
    """(marker hits per repo-relative path, (path, reason) for what could not be scanned).

    Unscannable files are RETURNED rather than skipped: a gate that silently
    passes what it could not read is a gate that ships the one file nobody
    scanned.

    A symlink is refused BEFORE any read. Following one is a real hang: a
    candidate pointing at ``/dev/zero`` makes ``read_text`` stream nulls until the
    job is killed (measured: the read does not return). Refusing symlinks closes
    that whole class for anything a checkout can produce, because git's object
    model carries only regular files, symlinks and gitlinks -- there is no other
    route to a device file or a FIFO in the tree. A skill body is prose in a
    regular file; the tree has never held a symlinked one.
    """
    found: dict[str, list[tuple[int, str, str]]] = {}
    unscannable: list[tuple[str, str]] = []
    skill_root = root / SKILL_ROOT
    if not skill_root.is_dir():
        return found, unscannable
    for path in sorted(skill_root.rglob("*.md")):
        rel = path.relative_to(root)
        if is_exempt(rel):
            continue
        if path.is_symlink():
            unscannable.append(
                (rel.as_posix(), "is a symlink; a skill body must be a regular file")
            )
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            unscannable.append(
                (rel.as_posix(), "cannot be read as UTF-8 text, so it cannot be scanned")
            )
            continue
        hits = marker_hits(text)
        if hits:
            found[rel.as_posix()] = hits
    return found, unscannable


def _annot_message(value: str) -> str:
    """Escape a workflow-command MESSAGE per GitHub's documented rules.

    The runner parses ``::`` commands on every output line, so an untrusted byte
    reaching an annotation unescaped is a forged-annotation vector rather than a
    cosmetic problem: a filename carrying LF + ``::error::`` would make the runner
    render a second annotation this gate never emitted.
    """
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _annot_property(value: str) -> str:
    """Escape a workflow-command PROPERTY value (``file=``, ``line=``).

    Property values additionally escape ``:`` and ``,``, which are the property
    and command separators -- otherwise a name containing either could split the
    property list and move the annotation to a file of the author's choosing.
    """
    return _annot_message(value).replace(":", "%3A").replace(",", "%2C")


def _report(
    current: dict[str, list[tuple[int, str, str]]], unscannable: list[tuple[str, str]]
) -> int:
    why = {name: reason for name, _pattern, reason in MARKER_CLASSES}
    for rel, hits in sorted(current.items()):
        for line_no, name, text in hits:
            print(
                "::error file={},line={}::{}: {} -- {}".format(
                    _annot_property(rel),
                    line_no,
                    name,
                    _annot_message(repr(text)),
                    why[name],
                )
            )
    for rel, reason in sorted(unscannable):
        print("::error file={}::{}".format(_annot_property(rel), _annot_message(reason)))
    if current or unscannable:
        if current:
            print(f"\n{REMEDY}", file=sys.stderr)
        total = sum(len(hits) for hits in current.values())
        print(
            f"builtin-skill-scope gate FAILED: {total} marker(s) in "
            f"{len(current)} file(s), {len(unscannable)} unscannable file(s).",
            file=sys.stderr,
        )
        return 1
    print(
        "builtin-skill-scope gate passed: no repository markers in a shipped skill "
        f"outside {EXEMPT_FAMILY}/."
    )
    return 0


_PROBES: tuple[tuple[str, str, int], ...] = (
    ("clean prose", "Open the dashboard and pick a theme.\n", 0),
    ("product CLI is not a marker", "Run `kirocrew pod up mypod --provision`.\n", 0),
    ("runtime path is not a marker", "Scripts live under `~/.kiro/crew/crons/`.\n", 0),
    (
        "installed package path is not a marker",
        "See `kiro_crew/dashboard/theme_validate.py`.\n",
        0,
    ),
    ("repo slug", "File it on kirodotdev/KiroCrew please.\n", 1),
    (
        "a resolvable public URL to this repo is NOT a marker",
        "See https://github.com/kirodotdev/KiroCrew/blob/main/README.md\n",
        0,
    ),
    ("checkout path", "Edit `src/kiro_crew/dashboard/state.py`.\n", 1),
    ("test path", "Run `test/test_prepare_pr_status.py`.\n", 1),
    ("workflow path", "Wire it into `.github/workflows/ci.yml`.\n", 1),
    ("workflow path, yaml spelling", "See .github/workflows/ci.yaml\n", 1),
    (
        "two classes on one line are both reported",
        "`src/kiro_crew/x.py` is covered by `test/test_x.py`.\n",
        2,
    ),
    (
        "two markers on separate lines",
        "First `src/kiro_crew/a.py`.\nThen `src/kiro_crew/b.py`.\n",
        2,
    ),
)


def self_test() -> int:
    failures = 0
    for desc, text, expected in _PROBES:
        got = len(marker_hits(text))
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] {desc}: expected {expected}, got {got}")
    # The exemption and the unreadable path are part of the rule, so both are
    # self-tested rather than left to the test suite alone -- CI runs --test
    # first precisely so a weakened rule fails before the gate reports green.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        leak = "Edit `src/kiro_crew/x.py`.\n"
        for rel in (
            f"{EXEMPT_FAMILY}/prepare-pr/SKILL.md",
            f"{EXEMPT_FAMILY}/prepare-pr/references/notes.md",
            "widgets/SKILL.md",
            f"widgets/{EXEMPT_FAMILY}/SKILL.md",
            "kirocrew-devtools/SKILL.md",
        ):
            path = root / SKILL_ROOT / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(leak, encoding="utf-8")
        undecodable = root / SKILL_ROOT / "widgets" / "binary.md"
        undecodable.write_bytes(b"\xff\xfe not utf-8 \x80\x81")
        # A symlink is refused before any read. The target is a dangling path on
        # purpose: refusal must not depend on it existing, and pointing a probe at
        # /dev/zero would make a regression HANG this self-test instead of failing
        # it (that hang is real -- it is why the guard exists).
        #
        # Creating one can be UNAVAILABLE rather than merely awkward: on Windows
        # it needs SeCreateSymbolicLinkPrivilege, which an ordinary account does
        # not hold, and this self-test runs from the prepare-pr gate floor on
        # contributor machines as well as on ubuntu in CI. Letting the OSError
        # escape would abort the gate with a crash instead of a verdict, so the
        # probe is skipped where the privilege is missing and says so -- the same
        # probe-not-guess approach test/conftest.py's requires_symlinks takes.
        linked = root / SKILL_ROOT / "widgets" / "linked.md"
        symlinks_available = True
        try:
            linked.symlink_to(root / "nowhere.md")
        except (OSError, NotImplementedError, AttributeError):
            symlinks_available = False
        found, unscannable = scan_tree(root)
        flagged = set(found)
        reasons = dict(unscannable)
        cases = (
            (
                f"{EXEMPT_FAMILY}/ body is exempt",
                f"{SKILL_ROOT.as_posix()}/{EXEMPT_FAMILY}/prepare-pr/SKILL.md" not in flagged,
            ),
            (
                f"nested {EXEMPT_FAMILY}/ reference file is exempt",
                f"{SKILL_ROOT.as_posix()}/{EXEMPT_FAMILY}/prepare-pr/references/notes.md"
                not in flagged,
            ),
            (
                "a repository-agnostic body is scanned",
                f"{SKILL_ROOT.as_posix()}/widgets/SKILL.md" in flagged,
            ),
            (
                f"a NESTED {EXEMPT_FAMILY}/ does not inherit the exemption",
                f"{SKILL_ROOT.as_posix()}/widgets/{EXEMPT_FAMILY}/SKILL.md" in flagged,
            ),
            (
                "a look-alike sibling does not inherit the exemption",
                f"{SKILL_ROOT.as_posix()}/kirocrew-devtools/SKILL.md" in flagged,
            ),
            (
                "an undecodable body is reported, not skipped",
                "UTF-8" in reasons.get(f"{SKILL_ROOT.as_posix()}/widgets/binary.md", ""),
            ),
        )
        for desc, ok in cases:
            failures += 0 if ok else 1
            print(f"  [{'ok' if ok else 'FAIL'}] {desc}")
        total = len(_PROBES) + len(cases)
        if symlinks_available:
            ok = "symlink" in reasons.get(f"{SKILL_ROOT.as_posix()}/widgets/linked.md", "")
            failures += 0 if ok else 1
            total += 1
            print(f"  [{'ok' if ok else 'FAIL'}] a symlinked body is refused unread")
        else:
            print("  [skip] symlink refusal: no privilege to create one here")
    if failures:
        print(f"self-test: {failures} probe(s) failed", file=sys.stderr)
        return 1
    print(f"self-test: all {total} probes passed")
    return 0


def main(argv: list[str]) -> int:
    if "--test" in argv:
        return self_test()
    current, unreadable = scan_tree(ROOT)
    return _report(current, unreadable)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
