"""A polling predicate never reads a conversation transcript on the event loop.

``ConversationLog.read_messages`` retries a failed open three times and pauses
between tries only OFF the loop (``_pause_for_transient_retry``): on the loop a
pause would block it, which is right for product code. A test that polls the
transcript from ``async_wait_until`` while a worker thread writes it therefore
spends all three tries inside the writer's window, and on Windows, where an
open can be denied while another handle is open, the read raises
``PermissionError`` and the test fails with nothing wrong in the code it tests.
``async_wait_until(..., off_loop=True)`` runs the predicate on a thread, where
the pause applies.

The scan reads every ``*.py`` under ``test/`` and ``src/**/tests/`` and finds
each ``async_wait_until`` call whose predicate or ``describe`` reads a
transcript without ``off_loop=True``. A read is a call of a method named in
``READERS``, or of a function that makes one: a plain ``def`` in the same file,
or one the file imports from another scanned module, followed to any depth.

It does not see a read reached through product code (a predicate that calls a
route which reads the transcript), through an attribute it cannot resolve
(``harness.rows()``), or a single read made on the loop outside a poll, which
races a writer only if the test left one running.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_transcript_poll_off_loop_guard")

#: The ``ConversationLog`` readers whose retry pauses only off the loop.
READERS = frozenset({"read_messages", "_read_messages"})
WAIT = "async_wait_until"
#: The arguments of ``async_wait_until`` that it evaluates where the poll runs.
EVALUATED = frozenset({"predicate", "describe"})


def scanned_files(root: Path) -> list[Path]:
    files = set((root / "test").rglob("*.py"))
    files.update(root.glob("src/**/tests/**/*.py"))
    return sorted(files)


def _called(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else None


class Scan:
    """The scanned tree: each module parsed once, and the functions in it that read."""

    def __init__(self, root: Path, files: list[Path] | None = None) -> None:
        self.root = root
        self.files = scanned_files(root) if files is None else files
        by_stem: dict[str, list[Path]] = {}
        for path in self.files:
            by_stem.setdefault(path.stem, []).append(path)
        # A stem two modules share names neither: an import of it is not followed.
        self.modules = {stem: paths[0] for stem, paths in by_stem.items() if len(paths) == 1}
        self._trees: dict[Path, ast.Module | None] = {}
        self._helpers: dict[Path, set[str]] = {}
        self._imported: dict[Path, tuple[dict[str, tuple[Path, str]], dict[str, Path]]] = {}

    def tree(self, path: Path) -> ast.Module | None:
        if path not in self._trees:
            try:
                self._trees[path] = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, SyntaxError, ValueError):
                self._trees[path] = None
        return self._trees[path]

    def _imports(self, path: Path) -> tuple[dict[str, tuple[Path, str]], dict[str, Path]]:
        """``({local name: (module, its name there)}, {local alias: module})`` for scanned modules."""
        if path in self._imported:
            return self._imported[path]
        names: dict[str, tuple[Path, str]] = {}
        aliases: dict[str, Path] = {}
        tree = self.tree(path)
        for node in ast.walk(tree) if tree is not None else ():
            if isinstance(node, ast.ImportFrom) and node.module:
                module = self.modules.get(node.module.rsplit(".", 1)[-1])
                for alias in node.names if module is not None else ():
                    names[alias.asname or alias.name] = (module, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    module = self.modules.get(alias.name.rsplit(".", 1)[-1])
                    if module is not None:
                        aliases[alias.asname or alias.name.rsplit(".", 1)[-1]] = module
        self._imported[path] = (names, aliases)
        return names, aliases

    def helpers(self, path: Path) -> set[str]:
        """The plain functions of ``path`` that read a transcript, directly or through a call."""
        if path in self._helpers:
            return self._helpers[path]
        found = self._helpers[path] = set()  # set before the walk: an import cycle ends here
        tree = self.tree(path)
        functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)] if tree else []
        grew = True
        while grew:
            grew = False
            for function in functions:
                if function.name not in found and self.reads(path, function):
                    found.add(function.name)
                    grew = True
        return found

    def reads(self, path: Path, node: ast.AST) -> bool:
        """Whether ``node``, in ``path``, names or calls something that reads a transcript."""
        names, aliases = self._imports(path)
        local = self._helpers.get(path, set())

        def named(name: str) -> bool:
            if name in local:
                return True
            return name in names and names[name][1] in self.helpers(names[name][0])

        if isinstance(node, ast.Name):
            return named(node.id)
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func, name = inner.func, _called(inner)
            if name in READERS:
                return True
            if isinstance(func, ast.Name) and named(func.id):
                return True
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                owner = func.value.id
                if owner in aliases and func.attr in self.helpers(aliases[owner]):
                    return True
                if owner in ("self", "cls") and func.attr in local:
                    return True
        return False

    def on_loop_polls(self, path: Path) -> list[int]:
        """Lines of ``async_wait_until`` calls in ``path`` that read a transcript on the loop."""
        tree = self.tree(path)
        if tree is None:
            return []
        self.helpers(path)
        lines = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _called(node) != WAIT:
                continue
            off_loop = any(
                keyword.arg == "off_loop"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            )
            evaluated = node.args[:1] + [k.value for k in node.keywords if k.arg in EVALUATED]
            if not off_loop and any(self.reads(path, part) for part in evaluated):
                lines.append(node.lineno)
        return lines

    def offenders(self) -> list[str]:
        found = []
        for path in self.files:
            try:
                if WAIT not in path.read_text(encoding="utf-8", errors="replace"):
                    continue
            except OSError:
                continue
            rel = path.relative_to(self.root).as_posix()
            found.extend(f"{rel}:{line}" for line in self.on_loop_polls(path))
        return found


def test_no_polling_predicate_reads_a_transcript_on_the_event_loop() -> None:
    scan = Scan(ROOT)
    # The scan is alive: it parses this tree and finds the polls that read off the loop.
    assert len(scan.files) > 1000
    reading = [p for p in scan.files if p.name == "test_taskrunner_coverage.py"]
    assert reading and "_transcript" in scan.helpers(reading[0])
    offenders = scan.offenders()
    assert not offenders, (
        "these async_wait_until calls read a conversation transcript on the event loop; "
        "pass off_loop=True so the read's retry can pause (see this module's docstring):\n  "
        + "\n  ".join(offenders)
    )


# `twice` comes first: it reads only through `rows`, which the scan meets after it.
HELPERS = (
    "def twice(log):\n"
    "    return rows(log) + rows(log)\n"
    "def rows(log):\n"
    "    return log.read_messages('k')\n"
    "def unrelated(log):\n"
    "    return log.name\n"
    "async def waits(log):\n"
    "    return log.read_messages('k')\n"
)

# Each case: the lines of a coroutine's body, and whether its poll reads on the loop.
CASES: dict[str, tuple[str, bool]] = {
    "a reader in the predicate": (
        "await async_wait_until(lambda: len(log.read_messages('k')) > 1)",
        True,
    ),
    "the private reader": ("await async_wait_until(lambda: log._read_messages('k'))", True),
    "a reader in describe only": (
        "await async_wait_until(lambda: done, describe=lambda: log.read_messages('k'))",
        True,
    ),
    "the predicate by keyword": (
        "await async_wait_until(predicate=lambda: log.read_messages('k'))",
        True,
    ),
    "a helper of the same file": ("await async_wait_until(lambda: local_rows(log))", True),
    "a helper passed by name": ("await async_wait_until(local_reads)", True),
    "an imported helper": ("await async_wait_until(lambda: rows(log))", True),
    "an imported helper that calls one": ("await async_wait_until(lambda: twice(log))", True),
    "a helper through a module alias": ("await async_wait_until(lambda: shared.rows(log))", True),
    "a qualified wait": ("await wait.async_wait_until(lambda: log.read_messages('k'))", True),
    "off_loop=False": (
        "await async_wait_until(lambda: log.read_messages('k'), off_loop=False)",
        True,
    ),
    "off_loop=True": (
        "await async_wait_until(lambda: log.read_messages('k'), off_loop=True)",
        False,
    ),
    "off_loop=True on a helper": (
        "await async_wait_until(lambda: twice(log), describe=lambda: rows(log), off_loop=True)",
        False,
    ),
    "no reader": ("await async_wait_until(lambda: seen, describe=lambda: seen)", False),
    "an imported function that reads nothing": (
        "await async_wait_until(lambda: unrelated(log))",
        False,
    ),
    "a reader in another argument": (
        "await async_wait_until(lambda: seen, timeout=len(log.read_messages('k')))",
        False,
    ),
    "a read beside the poll": (
        "before = log.read_messages('k')\n    await async_wait_until(lambda: seen)",
        False,
    ),
    "another wait helper": ("await wait_for(lambda: log.read_messages('k'))", False),
}


@pytest.mark.parametrize("name", CASES)
def test_the_scan_finds_a_poll_that_reads_on_the_loop_and_no_other(
    tmp_path: Path, name: str
) -> None:
    body, on_loop = CASES[name]
    tests = tmp_path / "test"
    tests.mkdir()
    (tests / "shared.py").write_text(HELPERS, encoding="utf-8")
    source = (
        "import shared\n"
        "from shared import rows, twice, unrelated\n"
        "from kiro_crew.testing import wait\n"
        "from kiro_crew.testing.wait import async_wait_until\n"
        "def local_rows(log):\n"
        "    return log.read_messages('k')\n"
        "def local_reads():\n"
        "    return local_rows(LOG)\n"
        f"async def test_it(log):\n    {body}\n"
    )
    (tests / "test_case.py").write_text(source, encoding="utf-8")
    scan = Scan(tmp_path)
    assert scan.helpers(tests / "shared.py") == {"rows", "twice"}
    if not on_loop:
        assert scan.offenders() == []
        return
    line = source.count("\n", 0, source.index("async_wait_until(", source.index("async def"))) + 1
    assert scan.offenders() == [f"test/test_case.py:{line}"]


def test_the_scan_follows_imports_to_any_depth_and_ends_on_a_cycle(tmp_path: Path) -> None:
    tests = tmp_path / "src" / "pkg" / "tests"
    tests.mkdir(parents=True)
    (tests / "deep.py").write_text(HELPERS, encoding="utf-8")
    (tests / "middle.py").write_text(
        "from deep import rows\nfrom top import again\ndef through(log):\n    return rows(log)\n",
        encoding="utf-8",
    )
    (tests / "top.py").write_text(
        "from middle import through\ndef again(log):\n    return through(log)\n", encoding="utf-8"
    )
    (tests / "test_deep.py").write_text(
        "from top import again\nasync def test_it(log):\n"
        "    await async_wait_until(lambda: again(log))\n",
        encoding="utf-8",
    )
    # A helper method the poll reaches through `self`.
    (tests / "test_method.py").write_text(
        "class TestBox:\n"
        "    def boxed(self, log):\n        return log.read_messages('k')\n"
        "    def plain(self, log):\n        return log.name\n"
        "    async def test_reads(self, log):\n"
        "        await async_wait_until(lambda: self.boxed(log))\n"
        "    async def test_does_not(self, log):\n"
        "        await async_wait_until(lambda: self.plain(log))\n",
        encoding="utf-8",
    )
    # A file that does not parse, and a stem two modules share, are passed over.
    (tests / "test_broken.py").write_text("async_wait_until(\n", encoding="utf-8")
    other = tmp_path / "test"
    other.mkdir()
    (other / "deep.py").write_text("def rows(log):\n    return []\n", encoding="utf-8")
    scan = Scan(tmp_path)
    assert "deep" not in scan.modules and scan.helpers(tests / "top.py") == set()
    assert scan.offenders() == ["src/pkg/tests/test_method.py:7"]
    (other / "deep.py").unlink()
    assert Scan(tmp_path).offenders() == [
        "src/pkg/tests/test_deep.py:3",
        "src/pkg/tests/test_method.py:7",
    ]
