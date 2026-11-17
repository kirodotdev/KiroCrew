#!/usr/bin/env python3
"""check_emoji_icons.py — gate emoji used as icons or status marks in the dashboard.

``website/AUTOSDE.yaml`` rule ``no-emoji-as-icons`` (blocking): the dashboard's
icons are ``lucide-react`` components, because an emoji ignores ``currentColor``
and the theme tokens, renders differently per platform, cannot be sized with
CSS, and is read out by name to a screen reader. This is the deterministic half
of that rule: it fails on an added line under ``website/src`` whose CODE (not a
comment) carries an emoji or a pictograph glyph (``✅ ⚠ ★ ● ▸ ✕ ✓``).

## What is exempt, and why

* Comments — prose about a glyph is not a glyph on screen.
* Test files — a fixture asserting how emoji are handled is data.
* The rule's own exceptions, where the emoji IS the data: the folder icon
  picker (``FolderConfigModal.tsx``), its renderer (``FolderGlyph.tsx``), the
  scene components (``src/pages/scenes/``), ``hooks/useTheme.tsx`` and
  ``themeEditor.tsx`` (theme display names).
* Generated files (``*.gen.ts``) — judged at their source.
* Arrows (``→``), the middle dot, the bullet ``•`` and keyboard glyphs
  (``⌘ ⌥ ⇧``): typography in copy and shortcut hints, not icons. A ``•`` drawn
  as a status mark is therefore caught only by review.
* A line carrying an ``emoji-ok: <reason>`` comment: code that MATCHES text
  someone else wrote — a gateway notice prefix such as ``🔧`` or ``⚠``, a
  terminal prompt — rather than drawing a glyph. Say whose text it is.

## Why diff-scoped and not whole-tree

The tree holds dozens of such data-matching lines from before this gate, and
marking each would be churn with no reader. A glyph only reaches ``main``
through a diff that wrote it, so judging added lines is complete for regression.
The whole-tree count is still printed as a non-failing report.

## Usage

    # enforce on the lines this branch added (exit 1 on any violation)
    EMOJI_ICON_BASE_REF=origin/main python3 scripts/check_emoji_icons.py

    # report the whole-tree hits, enforce nothing (exit 0)
    python3 scripts/check_emoji_icons.py

    # self-test: plant one probe per rule, assert each verdict
    python3 scripts/check_emoji_icons.py --test
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_ROOT = "website/src/"
MARKER = "emoji-ok:"

# Emoji and the pictograph blocks glyph icons come from. Python's stdlib has no
# Extended_Pictographic property, so the blocks are named here; arrows
# (U+2190-21FF) and the keyboard glyphs in U+2300-23FF are left out on purpose.
GLYPH = re.compile(
    "["
    "\U0001f000-\U0001faff"  # emoji, pictographs, symbols and supplements
    "\u2600-\u27bf"  # miscellaneous symbols, dingbats: ⚠ ★ ✅ ❌ ✓ ✕
    "\u25a0-\u25ff"  # geometric shapes: ● ○ ▸ ▾ ▲ ▼ ◆
    "\u2b00-\u2bff"  # ⭐ ⬆ and friends
    "\u231a\u231b\u2328\u23cf\u23e9-\u23f3\u23f8-\u23fa"  # ⌚ ⏳ ⏱ ⏹
    "\u2139\ufe0f"  # ℹ, and the emoji presentation selector
    "]"
)

EXEMPT_PATHS = (
    "website/src/pages/scenes/",
    "website/src/hooks/useTheme.tsx",
)
EXEMPT_NAMES = frozenset({"FolderConfigModal.tsx", "FolderGlyph.tsx", "themeEditor.tsx"})
TEST_PATH = re.compile(r"(?:\.(?:test|spec)\.tsx?$|/__tests__/|^website/src/test/)")

# After one of these a `/` starts a regex literal rather than a division.
_REGEX_PREV = set("(,=:[!&|?{};+-*%>~^") | {""}  # not `<`: `</p>` is a JSX closing tag
_REGEX_KEYWORDS = frozenset(
    {"return", "typeof", "case", "in", "of", "delete", "void", "throw", "new", "else", "do"}
)


def in_scope(path: str) -> bool:
    if not path.startswith(SCAN_ROOT) or not path.endswith((".ts", ".tsx")):
        return False
    if path.endswith(".gen.ts") or TEST_PATH.search(path):
        return False
    if path.startswith(EXEMPT_PATHS):
        return False
    return os.path.basename(path) not in EXEMPT_NAMES


def blank_comments(src: str) -> str:
    """``src`` with comment bodies blanked, offsets and newlines kept.

    Strings, template literals and regex literals are walked so a ``//`` or a
    quote inside one is not read as a comment or a string edge: a regex such as
    ``/^⚠ x```/`` would otherwise open a template literal and blank the rest of
    the file. A quote string ends at a newline, so an apostrophe in JSX text
    (``Don't``) can derail one line at most.
    """
    out = list(src)
    n = len(src)
    i = 0
    prev = ""  # last significant code character
    word = ""  # identifier that ends at ``prev``, for `return /re/`
    templates: list[int] = []  # brace depth inside each open `${`

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    def skip_template(j: int) -> int:
        """Walk template text from ``j``; stop after ``${`` (pushed) or the closing tick."""
        while j < n:
            if src[j] == "\\":
                j += 2
            elif src[j] == "`":
                return j + 1
            elif src.startswith("${", j):
                templates.append(0)
                return j + 2
            else:
                j += 1
        return j

    while i < n:
        c = src[i]
        two = src[i : i + 2]
        if two == "/*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
            continue
        if two == "//":
            j = src.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
            continue
        if c in "\"'":
            j = i + 1
            while j < n and src[j] not in (c, "\n"):
                j += 2 if src[j] == "\\" else 1
            i = j + 1
            prev, word = c, ""
            continue
        if c == "`":
            i = skip_template(i + 1)
            prev, word = "`", ""
            continue
        if c == "/" and (prev in _REGEX_PREV or word in _REGEX_KEYWORDS):
            j, in_class = i + 1, False
            while j < n and src[j] != "\n":
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "[":
                    in_class = True
                elif src[j] == "]":
                    in_class = False
                elif src[j] == "/" and not in_class:
                    break
                j += 1
            i = j + 1
            prev, word = "/", ""
            continue
        if c == "{" and templates:
            templates[-1] += 1
        elif c == "}" and templates:
            if templates[-1] == 0:
                templates.pop()
                i = skip_template(i + 1)
                prev, word = "`", ""
                continue
            templates[-1] -= 1
        if not c.isspace():
            if c.isalnum() or c in "_$":
                joined = i > 0 and (src[i - 1].isalnum() or src[i - 1] in "_$")
                word = word + c if joined else c
            else:
                word = ""
            prev = c
        i += 1
    return "".join(out)


def scan_source(raw: str, lines: set[int] | None = None) -> list[tuple[int, str]]:
    """``(line, glyphs)`` for each code line carrying a glyph, limited to ``lines``."""
    hits = []
    raw_lines = raw.split("\n")
    for no, code in enumerate(blank_comments(raw).split("\n"), 1):
        if lines is not None and no not in lines:
            continue
        found = GLYPH.findall(code)
        if found and MARKER not in raw_lines[no - 1]:
            hits.append((no, "".join(dict.fromkeys(found))))
    return hits


def read_text(path: str) -> str | None:
    try:
        with open(os.path.join(REPO_ROOT, path), encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


_SCOPE_MODULE = None


def _scope():
    """The shared diff plumbing in ``scripts/ratchet_scope.py``, loaded by path."""
    global _SCOPE_MODULE
    if _SCOPE_MODULE is None:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ratchet_scope.py")
        spec = importlib.util.spec_from_file_location("ratchet_scope", script)
        if spec is None or spec.loader is None:
            raise SystemExit(f"cannot load {script}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _SCOPE_MODULE = module
    return _SCOPE_MODULE


def report_tree() -> int:
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", SCAN_ROOT],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
        encoding="utf-8",
    ).stdout.split("\0")
    total = 0
    for path in sorted(p for p in tracked if p and in_scope(p)):
        raw = read_text(path)
        for no, glyphs in scan_source(raw or ""):
            print(f"  {path}:{no}  {glyphs}")
            total += 1
    print(f"::notice::emoji-icon gate: {total} unmarked glyph line(s) in the tree (not enforced)")
    return 0


def enforce_diff(base: str) -> int:
    scope = _scope()
    frm = scope.resolve_base(base)
    try:
        paths = [p for p in scope.changed_paths_at(frm) if in_scope(p)]
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"::error::emoji-icon gate: cannot diff against {frm} — the base commit is "
            f"not present. Fetch it, or unset EMOJI_ICON_BASE_REF to report only.\n{exc.stderr}"
        )
    bad = []
    for path in paths:
        raw = read_text(path)
        if raw is None:
            continue
        for no, glyphs in scan_source(raw, scope.added_lines_at(frm, path)):
            bad.append(f"  {path}:{no}  {glyphs}")
    if not bad:
        print(f"emoji-icon gate: {len(paths)} changed file(s), no emoji icon added")
        return 0
    print(
        "::error::emoji-icon gate: an added line draws an emoji or glyph as an icon or "
        'status mark. Use a lucide-react icon with className="lucide-inline" '
        "(website/AUTOSDE.yaml no-emoji-as-icons). Code that only matches text "
        "someone else wrote takes an `emoji-ok: <whose text>` comment on that line."
    )
    print("\n".join(bad))
    return 1


PROBES: list[tuple[str, str, int]] = [
    # (name, source, expected hit count)
    ("glyph in JSX text", "<span>✅ done</span>", 1),
    ("status dot in a string", "const dot = '●'", 1),
    ("emoji in a template", "const t = `${n} ⚠ left`", 1),
    ("glyph in a line comment", "const a = 1 // ✅ ok", 0),
    ("glyph in a block comment", "/*\n  ★ here\n*/\nconst a = 1", 0),
    ("glyph in a JSX comment", "<div>{/* ⚠ note */}</div>", 0),
    ("marked data match", "x.startsWith('🔧') // emoji-ok: gateway tool prefix", 0),
    ("arrows and keyboard glyphs", "const s = 'Settings → About · ⌘K ⇧'", 0),
    ("regex with a backtick", "const r = /^a```/\n<b>🔧</b>", 1),
    ("division then comment", "const r = a / b // ⚠\nconst q = c / d", 0),
    ("apostrophe in JSX text", "<p>Don't</p>\n<i>★</i>", 1),
    ("closing tag is no regex", "<p>a</p>\n<i>▸</i>", 1),
    ("nested template", "const t = `a ${f(`b`)} c`\nconst s = '✕'", 1),
]


def self_test() -> int:
    failures = []
    for name, src, want in PROBES:
        got = len(scan_source(src))
        if got != want:
            failures.append(f"  {name}: expected {want} hit(s), got {got}")
    for path, want in (
        ("website/src/pages/ChannelPage.tsx", True),
        ("website/src/App.tsx", True),
        ("website/src/pages/scenes/OfficeScene.tsx", False),
        ("website/src/hooks/useTheme.tsx", False),
        ("website/src/components/themeEditor.tsx", False),
        ("website/src/pages/chat-sidebar/FolderGlyph.tsx", False),
        ("website/src/pages/ChannelPage.test.tsx", False),
        ("website/src/test/foo.tsx", False),
        ("website/src/components/commandPalette/settingsRegistry.gen.ts", False),
        ("src/kiro_crew/foo.ts", False),
    ):
        if in_scope(path) != want:
            failures.append(f"  in_scope({path!r}) should be {want}")
    if failures:
        print("emoji-icon gate self-test FAILED:")
        print("\n".join(failures))
        return 1
    print(f"emoji-icon gate self-test: {len(PROBES)} probes agree OK")
    return 0


def main(argv: list[str]) -> int:
    if "--test" in argv:
        return self_test()
    base = os.environ.get("EMOJI_ICON_BASE_REF", "").strip()
    report_tree()
    return enforce_diff(base) if base else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
