"""Ratchet: a whole-string pattern ending in ``$`` checked with ``match`` or ``search``.

In Python ``$`` matches at the end of the string and also just before a final
newline. A pattern written to describe a whole value, ``^[a-z0-9]{1,32}$``,
therefore passes ``value + "\\n"`` when it is checked with ``PATTERN.match(value)``
or ``re.match(PATTERN, value)``. The spellings that check the whole value are
``PATTERN.fullmatch(value)`` and a pattern that ends in ``\\Z``.

What is counted, per file under ``src/kiro_crew``:

* ``NAME.match(...)`` / ``NAME.search(...)`` where ``NAME`` is bound to
  ``re.compile(<literal ending in "$">)`` without ``re.MULTILINE``, in the same
  file or reached through ``from <module> import NAME`` (re-exports followed);
* ``re.match(NAME, ...)`` / ``re.search(NAME, ...)`` on such a name;
* ``re.match(<literal>, ...)`` / ``re.search(<literal>, ...)`` and
  ``re.compile(<literal>).match(...)`` written inline.

A ``search`` counts only when the pattern also starts with ``^`` or ``\\A``: a
suffix test such as ``\\.py$`` is not a whole-value check.

What this scan cannot see:

* a pattern built at run time (a variable, a call, string concatenation the
  parser does not fold), or reached as an attribute (``module.NAME.match``,
  ``self._pattern.match``) or through a function argument;
* a pattern handed to another checker, such as ``FieldSpec(pattern=...)``, which
  trims the value before it matches;
* ``re.MULTILINE`` patterns, where ``$`` is a line end by design;
* anything under ``src/kiro_crew/security/``, which is outside this scan.

Many counted sites read one line of text and are indifferent to the newline;
they are counted all the same, because the scan cannot tell them from a check
on a caller's value. :data:`BASELINE_FILE` holds, per file, the count it had
when this gate landed. A count may go down and never up; a file not listed
starts at zero. A site that reads a line and wants ``$`` says so on its line
with ``# newline-ok: <reason>``.

Lowering the baseline is optional (a lower real count always passes).
``python test/test_anchored_pattern_match_ratchet.py --write-baseline`` lowers
every line to its real count and drops files that are gone; it never raises or
adds.
"""

from __future__ import annotations

import ast
import functools
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "kiro_crew"
PACKAGE = "kiro_crew"
BASELINE_FILE = Path(__file__).resolve().parent / "anchored_pattern_match_baseline.txt"
OUTSIDE_SCAN = ("security",)

_HEADER = """\
# Whole-string patterns ending in "$" that are checked with match/search, per
# file under src/kiro_crew, when the anchored-pattern ratchet landed. Read by
# test/test_anchored_pattern_match_ratchet.py; one "<count> <path under
# src/kiro_crew/>" per line.
# RATCHET: a count may only go down (delete the line at zero); never raise or add one.
# `python test/test_anchored_pattern_match_ratchet.py --write-baseline` lowers every
# line to its real count and drops files that are gone; it never raises or adds.
"""

_OK = re.compile(r"#\s*newline-ok:\s*(?P<reason>\S.*?)\s*\Z")
MIN_REASON_CHARS = 10
_RE_MODULES = frozenset({"re", "_re"})
_PARTIAL = frozenset({"match", "search"})
_MAX_REEXPORT_HOPS = 6

Site = tuple[int, str]


def _literal(node: ast.expr | None) -> str | None:
    """The pattern text of a string literal; an f-string's holes read as ``{}``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = [
            part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else "{}"
            for part in node.values
        ]
        return "".join(parts)
    return None


def _ends_in_dollar(pattern: str) -> bool:
    if not pattern.endswith("$"):
        return False
    body = pattern[:-1]
    return (len(body) - len(body.rstrip("\\"))) % 2 == 0


def _starts_anchored(pattern: str) -> bool:
    return pattern.startswith(("^", "\\A", "(?:^"))


def _is_re_call(node: ast.expr, names: frozenset[str]) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in names
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in _RE_MODULES
    )


def _multiline(call: ast.Call, first_flag: int) -> bool:
    flags = [*call.args[first_flag:], *(kw.value for kw in call.keywords)]
    return any(re.search(r"\b(MULTILINE|M)\b", ast.unparse(flag)) for flag in flags)


def _whole_value_pattern(call: ast.Call, first_flag: int) -> str | None:
    """*call*'s literal pattern when it ends in ``$`` and is not multi-line."""
    pattern = _literal(call.args[0]) if call.args else None
    if pattern is None or not _ends_in_dollar(pattern) or _multiline(call, first_flag):
        return None
    return pattern


class Module:
    """One parsed file: the ``$`` patterns it binds and the names it imports."""

    def __init__(self, source: str, dotted: str, is_package: bool = False) -> None:
        self.lines = source.splitlines()
        self.tree = ast.parse(source)
        self.patterns: dict[str, str] = {}
        self.imports: dict[str, tuple[str, str]] = {}
        self.partial_calls: list[ast.Call] = []
        package = dotted if is_package else dotted.rpartition(".")[0]
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr in _PARTIAL:
                    self.partial_calls.append(node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                self._bind(node)
            elif isinstance(node, ast.ImportFrom):
                base = _absolute(node.module, node.level, package)
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = (base, alias.name)

    def _bind(self, node: ast.Assign | ast.AnnAssign) -> None:
        value = node.value
        if value is None or not _is_re_call(value, frozenset({"compile"})):
            return
        assert isinstance(value, ast.Call)
        pattern = _whole_value_pattern(value, 1)
        if pattern is None:
            return
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                self.patterns[target.id] = pattern


def _absolute(module: str | None, level: int, package: str) -> str:
    if not level:
        return module or ""
    parts = package.split(".")
    base = parts[: len(parts) - (level - 1)]
    return ".".join([*base, *([module] if module else [])])


def _dotted(rel: str) -> tuple[str, bool]:
    parts = [PACKAGE, *rel[: -len(".py")].split("/")]
    is_package = parts[-1] == "__init__"
    return ".".join(parts[:-1] if is_package else parts), is_package


class Tree:
    """The package's files, parsed on demand, for following an import to its pattern."""

    def __init__(self, sources: dict[str, str]) -> None:
        self.sources = sources
        self.paths = {_dotted(rel)[0]: rel for rel in sources}
        self._parsed: dict[str, Module | None] = {}

    def module(self, rel: str) -> Module | None:
        if rel not in self._parsed:
            try:
                self._parsed[rel] = Module(self.sources[rel], *_dotted(rel))
            except SyntaxError:
                self._parsed[rel] = None
        return self._parsed[rel]

    def pattern_of(self, module: Module, name: str) -> str | None:
        for _ in range(_MAX_REEXPORT_HOPS):
            if name in module.patterns:
                return module.patterns[name]
            origin = module.imports.get(name)
            target = (
                self.module(self.paths[origin[0]]) if origin and origin[0] in self.paths else None
            )
            if origin is None or target is None:
                return None
            module, name = target, origin[1]
        return None

    def sites_in(self, module: Module) -> list[Site]:
        found: list[Site] = []
        for node in module.partial_calls:
            assert isinstance(node.func, ast.Attribute)
            how = node.func.attr
            owner = node.func.value
            pattern: str | None = None
            label = ""
            if isinstance(owner, ast.Name) and owner.id in _RE_MODULES:
                first = node.args[0] if node.args else None
                if isinstance(first, ast.Name):
                    pattern = self.pattern_of(module, first.id)
                    label = f"re.{how}({first.id})"
                else:
                    pattern = _whole_value_pattern(node, 2)
                    label = f"re.{how}(literal)"
            elif isinstance(owner, ast.Name):
                pattern = self.pattern_of(module, owner.id)
                label = f"{owner.id}.{how}"
            elif _is_re_call(owner, frozenset({"compile"})):
                assert isinstance(owner, ast.Call)
                pattern = _whole_value_pattern(owner, 1)
                label = f"re.compile(literal).{how}"
            if pattern is None or (how == "search" and not _starts_anchored(pattern)):
                continue
            if _excused(module.lines, node):
                continue
            found.append((node.lineno, label))
        return sorted(found)


def _excused(lines: list[str], node: ast.Call) -> bool:
    last = node.end_lineno or node.lineno
    for number in range(node.lineno, last + 1):
        excuse = _OK.search(lines[number - 1]) if number <= len(lines) else None
        if excuse and len(excuse["reason"]) >= MIN_REASON_CHARS:
            return True
    return False


def scan_sources(sources: dict[str, str]) -> dict[str, list[Site]]:
    """Counted sites by path under the package root, for *sources* (path -> text)."""
    tree = Tree(sources)
    out: dict[str, list[Site]] = {}
    for rel, text in sorted(sources.items()):
        if rel.split("/")[0] in OUTSIDE_SCAN or not any(f"{how}(" in text for how in _PARTIAL):
            continue
        module = tree.module(rel)
        found = tree.sites_in(module) if module else []
        if found:
            out[rel] = found
    return out


def scan(root: Path = SRC_ROOT) -> dict[str, list[Site]]:
    sources: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        try:
            sources[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
    return scan_sources(sources)


@functools.lru_cache(maxsize=1)
def _tree_scan() -> dict[str, list[Site]]:
    return scan()


def read_baseline(path: Path = BASELINE_FILE) -> dict[str, int]:
    out: dict[str, int] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        count, _, rel = line.partition(" ")
        if not count.isdigit() or not rel or rel in out:
            raise ValueError(f"{path.name}:{number}: expected '<count> <path>', got {raw!r}")
        out[rel] = int(count)
    return out


def format_baseline(counts: dict[str, int]) -> str:
    width = max((len(str(n)) for n in counts.values()), default=1)
    body = "".join(f"{n:>{width}} {rel}\n" for rel, n in sorted(counts.items()) if n > 0)
    return _HEADER + body


def risen(
    found: dict[str, list[Site]], baseline: dict[str, int]
) -> dict[str, tuple[int, list[Site]]]:
    return {
        rel: (baseline.get(rel, 0), sites)
        for rel, sites in found.items()
        if len(sites) > baseline.get(rel, 0)
    }


# ── the gate ─────────────────────────────────────────────────────────────


def test_no_file_gains_a_dollar_pattern_checked_with_match() -> None:
    over = risen(_tree_scan(), read_baseline())
    if over:
        detail = "\n".join(
            f"  src/kiro_crew/{rel}: {len(sites)} > baseline {was}: "
            + ", ".join(f"{line}:{label}" for line, label in sites)
            for rel, (was, sites) in sorted(over.items())
        )
        raise AssertionError(
            'A file gained a whole-string pattern ending in "$" that is checked with '
            "match or search, which also passes the value plus a newline:\n"
            f"{detail}\n"
            "Check with PATTERN.fullmatch(value), or end the pattern in \\Z. A site that "
            "reads one line of text and wants $ says so with `# newline-ok: <reason>` "
            "on its line."
        )


def test_the_baseline_names_only_real_files() -> None:
    missing = [rel for rel in read_baseline() if not (SRC_ROOT / rel).is_file()]
    assert missing == [], f"baseline lines for files that are gone: {missing}"


def test_the_baseline_lists_nothing_outside_the_scan() -> None:
    listed = [rel for rel in read_baseline() if rel.split("/")[0] in OUTSIDE_SCAN]
    assert listed == []


# ── the scanner itself ───────────────────────────────────────────────────


def _labels(source: str, **others: str) -> list[str]:
    sources = {"mod.py": source, **{f"{name}.py": text for name, text in others.items()}}
    return [label for _, label in scan_sources(sources).get("mod.py", [])]


def test_each_partial_check_of_a_dollar_pattern_is_counted() -> None:
    bound = 'import re\nID = re.compile(r"^[a-z]{1,8}$")\n'
    assert _labels(bound + "ok = ID.match(value)\n") == ["ID.match"]
    assert _labels(bound + "ok = ID.search(value)\n") == ["ID.search"]
    assert _labels(bound + "ok = re.match(ID, value)\n") == ["re.match(ID)"]
    assert _labels('import re\nok = re.match(r"^[a-z]+$", value)\n') == ["re.match(literal)"]
    assert _labels('import re\nok = re.match(r"[a-z]+$", value)\n') == ["re.match(literal)"]
    assert _labels('import re\nok = re.search(r"^[a-z]+$", value)\n') == ["re.search(literal)"]
    assert _labels('import re\nok = re.compile(r"^[a-z]+$").match(value)\n') == [
        "re.compile(literal).match"
    ]
    assert _labels('import re as _re\nID = _re.compile(f"^{PART}$")\nID.match(v)\n') == ["ID.match"]
    assert _labels('import re\nID: re.Pattern[str] = re.compile(r"^a$")\nID.match(v)\n') == [
        "ID.match"
    ]


def test_a_pattern_is_followed_through_an_import() -> None:
    shapes = 'import re\nID = re.compile(r"^[a-z]{1,8}$")\n'
    facade = "from kiro_crew.shapes import ID\n"
    assert _labels("from kiro_crew.shapes import ID\nID.match(v)\n", shapes=shapes) == ["ID.match"]
    assert _labels("from .shapes import ID as _ID\n_ID.match(v)\n", shapes=shapes) == ["_ID.match"]
    assert _labels(
        "from kiro_crew.facade import ID\nID.match(v)\n", shapes=shapes, facade=facade
    ) == ["ID.match"]
    assert _labels(
        "def f():\n    from kiro_crew.shapes import ID\n    ID.match(v)\n", shapes=shapes
    )


def test_a_same_named_pattern_elsewhere_is_not_confused() -> None:
    other = 'import re\nID = re.compile(r"^[a-z]{1,8}$")\n'
    own = 'import re\nID = re.compile(r"\\A[a-z]{1,8}\\Z")\nID.match(v)\n'
    assert _labels(own, other=other) == []


def test_the_whole_value_spellings_are_not_counted() -> None:
    clean = "\n".join(
        [
            "import re",
            'ID = re.compile(r"^[a-z]{1,8}$")',
            'END = re.compile(r"\\A[a-z]{1,8}\\Z")',
            'LINE = re.compile(r"^key: (.*)$", re.MULTILINE)',
            'SUFFIX = re.compile(r"\\.py$")',
            'PRICE = re.compile(r"^[0-9]+\\$")',
            "ID.fullmatch(value)",
            "END.match(value)",
            "LINE.search(text)",
            "SUFFIX.search(name)",
            "PRICE.match(value)",
            're.fullmatch(r"^[a-z]+$", value)',
            're.match(r"^key: (.*)$", text, re.M)',
            're.search(r"\\.py$", name)',
            "ID.sub('', value)",
            "ID.match(line)  # newline-ok: one line of git output, ending kept",
        ]
    )
    assert _labels(clean + "\n") == []


def test_an_excuse_needs_a_reason() -> None:
    bound = 'import re\nID = re.compile(r"^[a-z]{1,8}$")\n'
    assert _labels(bound + "ID.match(value)  # newline-ok: x\n") == ["ID.match"]


def test_security_is_outside_the_scan() -> None:
    source = 'import re\nID = re.compile(r"^[a-z]{1,8}$")\nID.match(v)\n'
    assert scan_sources({"security/mod.py": source, "other/mod.py": source}) == {
        "other/mod.py": [(3, "ID.match")]
    }


def test_a_new_site_in_a_listed_or_unlisted_file_is_a_rise() -> None:
    found = {"a.py": [(3, "ID.match"), (9, "ID.match")], "b.py": [(1, "ID.match")]}
    assert risen(found, {"a.py": 2, "b.py": 1}) == {}
    assert set(risen(found, {"a.py": 1, "b.py": 1})) == {"a.py"}
    assert set(risen(found, {"a.py": 2})) == {"b.py"}


def _write_baseline() -> int:
    counts = {rel: len(sites) for rel, sites in scan().items()}
    if BASELINE_FILE.exists():
        old = read_baseline()
        lowered = {rel: min(n, counts.get(rel, 0)) for rel, n in old.items()}
    else:
        lowered = dict(counts)
    BASELINE_FILE.write_text(format_baseline(lowered), encoding="utf-8")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--write-baseline"]:
        sys.exit(_write_baseline())
    for rel, sites in sorted(scan().items()):
        print(f"{len(sites):>3} src/kiro_crew/{rel}")
