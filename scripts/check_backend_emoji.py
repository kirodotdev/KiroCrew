#!/usr/bin/env python3
"""check_backend_emoji.py -- no emoji in the text the Python backend sends.

## The rule this enforces

``docs/system-specs/common/code-style.md`` ("Backend messages use plain words"):
a message the backend writes -- CLI and doctor output, a chat-app reply, a
dashboard notice, a tool reply, a prompt the model reads -- says ``Error:``,
``Warning:`` or ``Done`` in words, not ``❌ ⚠ ✅``. An emoji renders differently
per terminal and font, breaks column alignment, is read out by name to a screen
reader, and in a prompt or tool reply is text the model reads and imitates.

## What counts as a violation

A Python STRING token (docstrings included: a tool docstring is the description
the model reads) on an added line under ``src/kiro_crew/`` that carries either

* an emoji or pictograph glyph from ``GLYPH``, read from the dashboard's
  ``scripts/check_emoji_icons.py`` gate, so both surfaces agree on
  what an emoji is (arrows, the middle dot, the bullet and box drawing are
  typography and pass), or
* a Slack ``:shortcode:`` from ``SHORTCODE_NAMES`` (``:white_check_mark:``,
  ``:no_entry:``), which Slack draws as the same emoji. The name must stand
  alone and be on that list, so ``:func:`` roles, ``::error::`` and ARNs pass.

Strings are found with ``tokenize``, never a line regex, so a comment that names
a glyph is never judged. f-string text is judged on every Python version: one
``STRING`` token before 3.12, ``FSTRING_MIDDLE`` pieces after. An escape that
spells a glyph (``"\\u2705"``, ``"\\U0001f680"``, ``"\\N{WHITE HEAVY CHECK
MARK}"``) counts too, except in raw and bytes literals, where it stays text.

## What is exempt, and why

* Comments -- prose about a glyph is not a glyph anyone is sent.
* ``src/kiro_crew/apps/builtins/`` -- those apps are moving out of core.
* ``src/kiro_crew/_vendor/`` -- vendored third-party code is not ours to rewrite.
* Tests under ``src/`` -- a fixture asserting how emoji are handled is data.
* A line with a trailing ``# emoji-ok: <reason>`` comment: the glyph is DATA,
  not a status mark -- a Slack reaction name, a user-picked icon, code that
  parses a glyph out of text someone else wrote, or a reader that must still
  accept the glyph saved history carries. Say whose text it is.

## Why diff-scoped and not whole-tree

The tree still carries the pre-rule glyphs while the cleanup PRs land, so a
whole-tree gate would charge that backlog to whoever pushed next. A glyph only
reaches ``main`` through a diff that wrote it, so judging added lines is
complete for regression. The whole-tree count is still printed as a report.

## Usage

    # enforce on the lines this branch added (exit 1 on any violation)
    BACKEND_EMOJI_BASE_REF=origin/main python3 scripts/check_backend_emoji.py

    # report the whole-tree hits, enforce nothing (exit 0)
    python3 scripts/check_backend_emoji.py

    # self-test: plant one probe per rule, assert each verdict
    python3 scripts/check_backend_emoji.py --test
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import os
import re
import subprocess
import tokenize
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_ENV = "BACKEND_EMOJI_BASE_REF"
SCAN_ROOT = "src/kiro_crew/"
EXCLUDED_ROOTS = ("src/kiro_crew/apps/builtins/", "src/kiro_crew/_vendor/")


def _load_script(name: str):
    """A sibling gate in ``scripts/``, loaded by path (``scripts/`` is not a package)."""
    script = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The dashboard gate owns the glyph set, so both surfaces agree on what an
# emoji is and a change to one cannot leave the other behind.
GLYPH: re.Pattern[str] = _load_script("check_emoji_icons").GLYPH

# Slack emoji names a status or notice line reaches for. A bare ``:name:``
# pattern also matches docstring roles (``:func:``), ARNs and ids by the
# thousand, so the name must be one Slack draws as an emoji.
# shortcut: a curated list, not Slack's full alias table; add a name here when
# a review finds one this misses.
SHORTCODE_NAMES = (
    "white_check_mark heavy_check_mark ballot_box_with_check x negative_squared_cross_mark "
    "no_entry no_entry_sign warning rotating_light octagonal_sign stop_sign sos "
    "hourglass hourglass_flowing_sand stopwatch alarm_clock timer_clock "
    "lock unlock closed_lock_with_key key "
    "information_source question grey_question exclamation heavy_exclamation_mark bangbang "
    "red_circle large_green_circle green_circle yellow_circle large_blue_circle "
    "large_orange_circle white_circle black_circle "
    "rocket tada sparkles fire zap boom star star2 bulb bell "
    "memo pencil pencil2 clipboard pushpin round_pushpin link paperclip package "
    "inbox_tray outbox_tray page_facing_up file_folder open_file_folder calendar "
    "wrench hammer hammer_and_wrench gear mag mag_right eyes robot_face bug construction "
    "arrows_counterclockwise repeat recycle speech_balloon thought_balloon "
    "chart_with_upwards_trend chart_with_downwards_trend bar_chart "
    "thumbsup thumbsdown +1 -1 ok_hand wave pray raised_hands clap muscle "
    "heavy_plus_sign heavy_minus_sign new ok skull broken_heart heart"
).split()
SHORTCODE = re.compile(
    r"(?<![\w:]):(?:" + "|".join(re.escape(n) for n in SHORTCODE_NAMES) + r"):(?![\w:])"
)

MARKER = re.compile(r"#\s*emoji-ok:\s*\S")

_STRING_TYPES = {tokenize.STRING} | {
    getattr(tokenize, name)
    for name in ("FSTRING_MIDDLE", "TSTRING_MIDDLE")
    if hasattr(tokenize, name)
}
_TEST_NAME = re.compile(r"(?:^test_.*|.*_test|^conftest)\.py$")


def in_scope(path: str) -> bool:
    if not path.startswith(SCAN_ROOT) or not path.endswith(".py"):
        return False
    if path.startswith(EXCLUDED_ROOTS):
        return False
    parts = path.split("/")
    if any(part in ("test", "tests") for part in parts[:-1]):
        return False
    return not _TEST_NAME.match(parts[-1])


# A text escape that can spell an emoji. A doubled backslash is matched first
# so ``"\\u2705"`` (a literal backslash, then ``u2705``) is not read as one.
_ESCAPE = re.compile(r"\\\\|\\u([0-9a-fA-F]{4})|\\U([0-9a-fA-F]{8})|\\N\{([^}]+)\}")
_FSTRING_START = getattr(tokenize, "FSTRING_START", None)
_TSTRING_START = getattr(tokenize, "TSTRING_START", None)


def _decodes_escapes(prefix: str) -> bool:
    """True when a literal with this prefix turns ``\\u`` escapes into characters."""
    lowered = prefix.lower()
    return "r" not in lowered and "b" not in lowered


def _escaped_glyphs(text: str) -> list[tuple[int, str]]:
    """``(offset, escape)`` for every ``\\u`` / ``\\U`` / ``\\N{}`` escape naming a glyph."""
    hits = []
    for m in _ESCAPE.finditer(text):
        four, eight, name = m.groups()
        if four or eight:
            code = int(four or eight, 16)
            char = chr(code) if code <= 0x10FFFF else ""
        elif name:
            try:
                char = unicodedata.lookup(name)
            except KeyError:
                char = ""
        else:
            continue
        if char and GLYPH.search(char):
            hits.append((m.start(), m.group()))
    return hits


def _found_in(text: str, decodes: bool = False) -> list[tuple[int, str]]:
    """``(offset, match)`` for every glyph, shortcode and glyph escape in ``text``."""
    hits = [(m.start(), m.group()) for m in GLYPH.finditer(text)]
    hits += [(m.start(), m.group()) for m in SHORTCODE.finditer(text)]
    if decodes:
        hits += _escaped_glyphs(text)
    return sorted(hits)


def _string_prefix(token_text: str) -> str:
    """The letters before the opening quote of a STRING or f-string start token."""
    return token_text[: len(token_text) - len(token_text.lstrip("rRbBuUfFtT"))]


def scan_source(source: str, lines: set[int] | None = None) -> list[tuple[int, str]]:
    """``(line, found)`` for each string line carrying an emoji, limited to ``lines``.

    Raises ``tokenize.TokenError`` (or ``SyntaxError``) on source that does not
    tokenize: reading a broken file as clean would let it pass the gate.
    """
    if not GLYPH.search(source) and not SHORTCODE.search(source) and "\\" not in source:
        return []
    per_line: dict[int, list[str]] = {}
    marked: set[int] = set()
    fstring_prefixes: list[str] = []  # one per open f-string, innermost last
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            if MARKER.search(token.string):
                marked.add(token.start[0])
            continue
        if token.type in (_FSTRING_START, _TSTRING_START) and token.type is not None:
            fstring_prefixes.append(_string_prefix(token.string))
            continue
        if (
            token.type
            in (
                getattr(tokenize, "FSTRING_END", None),
                getattr(tokenize, "TSTRING_END", None),
            )
            and token.type is not None
        ):
            if fstring_prefixes:
                fstring_prefixes.pop()
            continue
        if token.type not in _STRING_TYPES:
            continue
        if token.type == tokenize.STRING:
            decodes = _decodes_escapes(_string_prefix(token.string))
        else:
            decodes = bool(fstring_prefixes) and _decodes_escapes(fstring_prefixes[-1])
        for offset, match in _found_in(token.string, decodes):
            line = token.start[0] + token.string.count("\n", 0, offset)
            per_line.setdefault(line, []).append(match)
    return [
        (line, " ".join(dict.fromkeys(found)))
        for line, found in sorted(per_line.items())
        if line not in marked and (lines is None or line in lines)
    ]


def _load_scope():
    """The shared diff-scope helpers in ``scripts/ratchet_scope.py``, loaded by path."""
    return _load_script("ratchet_scope")


def _read(rel: str) -> str | None:
    try:
        return (ROOT / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def report_tree() -> int:
    """Whole-tree counts, never failing: the backlog to watch, not a gate."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", SCAN_ROOT],
        cwd=ROOT,
        capture_output=True,
        check=True,
        encoding="utf-8",
    ).stdout.split("\0")
    total = files = 0
    for rel in sorted(p for p in tracked if p and in_scope(p)):
        try:
            found = scan_source(_read(rel) or "")
        except (SyntaxError, tokenize.TokenError):
            continue
        if found:
            files += 1
            total += len(found)
    print(
        f"::notice::backend-emoji gate: {total} unmarked emoji string line(s) in "
        f"{files} file(s) under {SCAN_ROOT} (not enforced; set {BASE_ENV} to gate a change)"
    )
    return 0


def enforce_diff(base: str) -> int:
    """Enforce on the lines this change adds. Fails closed when the base is unreadable."""
    scope = _load_scope()
    frm = scope.resolve_base(base)
    try:
        paths = [p for p in scope.changed_paths_at(frm) if in_scope(p)]
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"::error::backend-emoji gate: cannot diff against {frm} -- the base commit "
            f"is not present. Fetch it, or unset {BASE_ENV} to report only.\n{exc.stderr}"
        )
    bad: list[str] = []
    for rel in paths:
        source = _read(rel)
        if source is None:
            continue  # a deletion adds no lines
        lines = scope.added_lines_at(frm, rel)
        if not lines:
            continue
        try:
            found = scan_source(source, lines)
        except (SyntaxError, tokenize.TokenError) as exc:
            bad.append(f"  {rel}: does not tokenize ({exc})")
            continue
        bad += [f"  {rel}:{line}  {text}" for line, text in found]
    if not bad:
        print(f"backend-emoji gate: {len(paths)} changed file(s), no emoji added to a string")
        return 0
    print(
        "::error::backend-emoji gate: an added string under src/kiro_crew carries an "
        "emoji or a Slack :shortcode:. Backend messages use plain words (Error:, "
        "Warning:, Done); see docs/system-specs/common/code-style.md. A glyph that is "
        "data (a reaction name, a user-picked icon, text parsed from elsewhere) takes "
        "a trailing `# emoji-ok: <reason>` comment on that line."
    )
    print("\n".join(bad))
    return 1


PROBES: list[tuple[str, str, int]] = [
    # (name, source, expected hit count)
    ("glyph in a string", 'print("✅ done")\n', 1),
    ("glyph in an f-string", 'msg = f"{n} ⚠ left"\n', 1),
    ("glyph in a docstring", 'def f():\n    """Return ❌ on failure."""\n', 1),
    ("glyph on a later line of a triple string", 'X = """head\n🔧 tool\n"""\n', 1),
    ("Slack shortcode", 'text = ":white_check_mark: merged"\n', 1),
    ("Slack +1 shortcode", 'text = "thanks :+1:"\n', 1),
    ("glyph in a comment", "x = 1  # ✅ ok\n", 0),
    ("shortcode in a comment", "x = 1  # :no_entry: here\n", 0),
    ("marked data", 'if s.startswith("🔧"):  # emoji-ok: tool rows saved history\n    pass\n', 0),
    ("marker with no reason", 'x = "✅"  # emoji-ok:\n', 1),
    ("typography passes", 'x = "Settings → About · • ─"\n', 0),
    ("CI annotation is no shortcode", 'print("::error::bad")\n', 0),
    ("namespaced id is no shortcode", 'k = "crew:mate:id"\n', 0),
    ("docstring role is no shortcode", '"""See :func:`run` and :lock: nothing."""\n', 1),
    ("unlisted name is no shortcode", 'k = f"{item}:stop:{code}"\n', 0),
    ("time format is no shortcode", 'f = "%H:%M:%S"\n', 0),
    ("reaction name without colons", 'add_reaction("white_check_mark")\n', 0),
    ("\\u escape", 'print("\\u2705 done")\n', 1),
    ("\\U escape", 'print("\\U0001f680 shipped")\n', 1),
    ("\\N escape", 'print("\\N{WHITE HEAVY CHECK MARK} done")\n', 1),
    ("escape in an f-string", 'print(f"{n} \\u26a0 left")\n', 1),
    ("escape on a later line of a triple string", 'X = """head\n\\u274c tool\n"""\n', 1),
    ("raw string keeps the escape as text", 'R = r"\\u2705"\n', 0),
    ("raw f-string keeps the escape as text", 'R = rf"{n}\\u2705"\n', 0),
    ("bytes keep the escape as text", 'B = b"\\u2705"\n', 0),
    ("escaped backslash is no escape", 'S = "\\\\u2705"\n', 0),
    ("non-glyph escape passes", 'S = "\\u00e9t\\u00e9 \\u2192"\n', 0),
]


def _self_test() -> int:
    """Plant one probe per rule; a broken rule fails here, not in CI."""
    failures = []
    for name, src, want in PROBES:
        got = len(scan_source(src))
        if got != want:
            failures.append(f"  {name}: expected {want} hit(s), got {got}")
    for path, want in (
        ("src/kiro_crew/cli.py", True),
        ("src/kiro_crew/slack/code_channels/handoff.py", True),
        ("src/kiro_crew/apps/builtins/foo/app.py", False),
        ("src/kiro_crew/_vendor/lib/x.py", False),
        ("src/kiro_crew/foo/tests/test_x.py", False),
        ("src/kiro_crew/foo/test_x.py", False),
        ("src/kiro_crew/foo/x_test.py", False),
        ("src/kiro_crew/static/dist/app.js", False),
        ("scripts/check_backend_emoji.py", False),
        ("test/test_cli.py", False),
    ):
        if in_scope(path) != want:
            failures.append(f"  in_scope({path!r}) should be {want}")
    try:
        scan_source('x = ("✅"\n')
        failures.append("  unparseable source read as clean")
    except (SyntaxError, tokenize.TokenError):
        pass
    if failures:
        print("backend-emoji gate self-test FAILED:")
        print("\n".join(failures))
        return 1
    print(f"backend-emoji gate self-test: {len(PROBES)} probes agree OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true", help="run the self-test")
    args = parser.parse_args(argv)
    if args.test:
        return _self_test()
    base = os.environ.get(BASE_ENV, "").strip()
    report_tree()
    return enforce_diff(base) if base else 0


if __name__ == "__main__":
    raise SystemExit(main())
