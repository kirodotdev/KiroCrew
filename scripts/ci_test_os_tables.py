"""OS-sensitivity tables for the test map: which files Windows can run differently.

SHADOW ONLY: `scripts/ci_test_map.py bundle` adds these tables to the selection
file, and nothing in CI reads them yet. They are computed from the tree at every
build, because a hand-kept list goes stale and cannot be small.

    "os_sensitive": {src file: "W1" | "W2"}   (every other file left out)
    "windows_targeting": [test file, ...]

* W1 -- branches on the OS, or calls a Windows API;
* W2 -- calls a POSIX-only API with no branch;
* W3 -- calls an API whose behaviour varies by OS (processes, signals, file
  replace and modes, symlinks, separators, encodings, sockets, temp and home).

A file takes its strongest tier. Only code is matched: comments and docstrings
are blanked first. The table holds the W1 and W2 files, the ones the Windows
test set is chosen from; W3 is matched only on the lines a diff adds, so it is
a pattern here (`tier_of`) and not a row. The patterns live here alone so the
scorer applies the same ones to those lines.

`bundle` leaves out a table that cannot be built or that comes out empty (its
tree is missing, or the scan found nothing): a dead scan and a quiet tree would
otherwise look the same.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

SRC_DIR = "src/kiro_crew"
TEST_DIRS = ("test", "src/kiro_crew/apps/builtins")
STORED_TIERS = ("W1", "W2")
COLLECT_IGNORE = "test/windows-collect-ignore.txt"


class Rule(NamedTuple):
    """A regex and the literal it starts with.

    The scan runs the regex only from where `needle` first occurs: one big
    alternation led by `\\b` makes `re` try every branch at every offset, which
    takes minutes over the tree; a substring test takes seconds.
    """

    needle: str
    regex: re.Pattern[str]

    def search(self, text: str) -> bool:
        at = text.find(self.needle)
        return at >= 0 and self.regex.search(text, max(at - 1, 0)) is not None


def _words(*words: str) -> tuple[Rule, ...]:
    return tuple(Rule(w, re.compile(rf"\b{re.escape(w)}\b")) for w in words)


def _rule(needle: str, pattern: str) -> tuple[Rule, ...]:
    return (Rule(needle, re.compile(pattern, re.DOTALL)),)


TIERS: tuple[tuple[str, tuple[Rule, ...]], ...] = (
    (
        "W1",
        _words(
            "sys.platform",
            "os.name",
            "IS_WINDOWS",
            "IS_POSIX",
            "msvcrt",
            "winreg",
            "_winapi",
            "windll",
            "creationflags",
            "CREATE_NEW_PROCESS_GROUP",
            "ntpath",
            "PureWindowsPath",
            "APPDATA",
            "LOCALAPPDATA",
            "USERPROFILE",
        )
        + _rule("platform.system(", r"\bplatform\.system\(")
        + _rule("win32", r"win32")
        + _rule(".exe", r"\.exe[\"']")
        + _rule(".cmd", r"\.cmd[\"']")
        + _rule(".bat", r"\.bat[\"']"),
    ),
    (
        "W2",
        _words(
            "fcntl",
            "flock",
            "os.fork",
            "setsid",
            "getpgid",
            "killpg",
            "start_new_session",
            "preexec_fn",
            "pty",
            "termios",
            "getuid",
            "geteuid",
            "chown",
            "lchown",
            "AF_UNIX",
            "SIGKILL",
            "SIGHUP",
            "O_NOFOLLOW",
            "posixpath",
        ),
    ),
    (
        "W3",
        _words(
            "subprocess",
            "signal",
            "rmtree",
            "chmod",
            "os.sep",
            "socket",
            "tempfile",
            "mkdtemp",
            "gettempdir",
            "expanduser",
        )
        + _rule(".rename(", r"\.rename\(")
        # `Path.replace(target)` takes one positional argument; `str.replace` takes two.
        + _rule(".replace(", r"\.replace\([^,()=]*\)")
        + _rule("os.replace(", r"\bos\.replace\(")
        + _rule(".unlink(", r"\.unlink\(")
        + _rule("S_I", r"\bS_I[RWX]")
        + _rule("0o", r"\b0o[0-7]{3,4}\b")
        + _rule("symlink", r"symlink")
        + _rule(".read_text(", r"\.read_text\((?![^)]*\bencoding\b)")
        + _rule("newline=", r"\bnewline=")
        + _rule(".bind(", r"\.bind\(")
        + _rule("Path.home(", r"\bPath\.home\(")
        + _rule("HOME", r"[\"']HOME[\"']"),
    ),
)

_TARGETING_MARKERS: tuple[Rule, ...] = _words("posix_only", "requires_posix", "windows_only")
# The first argument(s) of a `setattr` / `patch` call that fake the platform.
_FAKED_TARGET = re.compile(
    r"\A\s*(?:[\"'](?:[\w.]*\.)?(?:sys\.platform|os\.name|IS_WINDOWS)[\"']"
    r"|(?:[\w.]*\.)?(?:sys\s*,\s*[\"']platform|os\s*,\s*[\"']name)[\"']"
    r"|[\w.]+\s*,\s*[\"']IS_WINDOWS[\"'])"
)
# A call, and what its own arguments must name for the test file to target Windows.
_TARGETING_CALLS: tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...] = (
    (
        "skipif(",
        re.compile(r"\bskipif\("),
        re.compile(
            r"win32|\bsys\.platform\b|\bIS_WINDOWS\b|\bIS_POSIX\b|\bos\.name\b|\bplatform\.system\b"
        ),
    ),
    ("setattr(", re.compile(r"\bsetattr\("), _FAKED_TARGET),
    ("patch", re.compile(r"\bpatch(?:\.object)?\("), _FAKED_TARGET),
)
_TARGETING_PATH = re.compile(r"windows|win32", re.IGNORECASE)


def _call_args(text: str, start: int) -> str:
    """The argument text of the call whose `(` is at *start*, up to its matching `)`.

    Brackets inside strings and comments do not count.
    """
    depth = 0
    for match in _BRACKETS.finditer(text, start):
        bracket = match.group("bracket")
        if bracket is None:
            continue
        depth += 1 if bracket in "([{" else -1
        if depth == 0:
            return text[start + 1 : match.start()]
    return text[start + 1 :]


def _targeting_hit(code: str) -> bool:
    if _hits(_TARGETING_MARKERS, code):
        return True
    for needle, call, wanted in _TARGETING_CALLS:
        if needle not in code:
            continue
        for match in call.finditer(code):
            if wanted.search(_call_args(code, match.end() - 1)):
                return True
    return False


_NOT_A_BREAK = re.compile(r"[^\r\n]")
# One string literal or one comment. A regex pass, not `tokenize`: tokenizing
# the whole tree takes minutes, this takes seconds.
_LEXEME = re.compile(
    r"(?P<string>[rRbBuUfF]{0,2}(?:\"\"\"(?:\\.|[^\\])*?\"\"\"|'''(?:\\.|[^\\])*?'''"
    r"|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'))|(?P<comment>#[^\n]*)",
    re.DOTALL,
)
_BRACKETS = re.compile(_LEXEME.pattern + r"|(?P<bracket>[()\[\]{}])", re.DOTALL)
# A `def` or `class` header whose body starts on the same line, after its colon.
_INLINE_BODY = re.compile(r"[ \t]*(?:async[ \t]+)?(?:def|class)\b[^#\n]*:[ \t]*\Z")
_REST_OF_LINE = re.compile(r"[ \t]*(?:#[^\n]*)?(?:\r?\n|$)")


def _is_docstring(source: str, match: re.Match[str]) -> bool:
    """Is the string *match*, which sits inside no bracket, a statement of its own?

    Outside brackets a string alone on its lines is an operand only after a
    backslash continuation. An f-string runs code, so it is never a docstring.
    """
    literal = match.group()
    if "f" in literal[: len(literal) - len(literal.lstrip("rRbBuUfF"))].lower():
        return False
    start = match.start()
    line_start = source.rfind("\n", 0, start) + 1
    if not _REST_OF_LINE.match(source, match.end()):
        return False
    head = source[line_start:start]
    if head.strip(" \t"):
        return bool(_INLINE_BODY.match(head))
    return not source[:line_start].rstrip("\r\n").endswith("\\")


def code_only(source: str) -> str:
    """*source* with comments and docstrings blanked; offsets and line breaks kept.

    A docstring here is a string that stands alone as a statement. A string
    inside brackets is an operand whatever line it is on, and so is one the pass
    cannot place (a string it cannot close, brackets that do not balance), so a
    broken file over-matches rather than hides a signal.
    """
    out: list[str] = []
    done = depth = 0
    for match in _BRACKETS.finditer(source):
        bracket = match.group("bracket")
        if bracket is not None:
            depth = max(depth + (1 if bracket in "([{" else -1), 0)
        elif match.group("comment") is not None or (depth == 0 and _is_docstring(source, match)):
            out += [source[done : match.start()], _NOT_A_BREAK.sub(" ", match.group())]
            done = match.end()
    out.append(source[done:])
    return "".join(out)


def _hits(rules: Iterable[Rule], text: str) -> bool:
    return any(rule.search(text) for rule in rules)


def tier_of(code: str) -> str | None:
    """The strongest tier any pattern in *code* hits, or None."""
    for name, rules in TIERS:
        if _hits(rules, code):
            return name
    return None


def file_tier(source: str) -> str | None:
    """`tier_of` the code of *source*; a file no pattern hits at all is never blanked."""
    return tier_of(code_only(source)) if tier_of(source) else None


def targets_windows(path: str, source: str) -> bool:
    """Does the test file at *path*, holding *source*, aim at Windows on purpose?"""
    if _TARGETING_PATH.search(path):
        return True
    return _targeting_hit(source) and _targeting_hit(code_only(source))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _py_files(root: Path, directory: str) -> Iterable[Path]:
    base = root / directory
    return sorted(base.rglob("*.py")) if base.is_dir() else ()


def _is_test_name(name: str) -> bool:
    return name.startswith("test_") and name.endswith(".py")


def os_sensitive(root: Path) -> dict[str, str]:
    """`{src file: W1 or W2}` over `src/kiro_crew`, tests and `_vendor` left out."""
    table: dict[str, str] = {}
    for path in _py_files(root, SRC_DIR):
        rel = path.relative_to(root).as_posix()
        parts = rel.split("/")
        if "_vendor" in parts or "tests" in parts or _is_test_name(parts[-1]):
            continue
        tier = file_tier(_read(path))
        if tier in STORED_TIERS:
            table[rel] = tier
    return table


def collect_ignored(root: Path) -> set[str]:
    """The `test/<name>` paths Windows does not collect."""
    text = (root / COLLECT_IGNORE).read_text(encoding="utf-8")
    names = (line.split("#", 1)[0].strip() for line in text.splitlines())
    return {f"test/{name}" for name in names if name}


def windows_targeting(root: Path) -> list[str]:
    """Test files that aim at Windows on purpose, minus the ones Windows never collects."""
    ignored = collect_ignored(root) if (root / COLLECT_IGNORE).is_file() else set()
    found: set[str] = set()
    for directory in TEST_DIRS:
        for path in _py_files(root, directory):
            rel = path.relative_to(root).as_posix()
            if (
                _is_test_name(path.name)
                and rel not in ignored
                and targets_windows(rel, _read(path))
            ):
                found.add(rel)
    return sorted(found)
