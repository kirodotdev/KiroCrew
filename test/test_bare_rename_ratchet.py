"""Ratchet: a change may not add a bare by-name rename to a product file under ``src/``.

On Windows a rename onto a file fails with ``PermissionError`` (WinError 5 / 32) while
any other handle is open on either path: an AV scanner, the search indexer, a reader
in another thread. A bare ``os.replace`` then loses the write on the first fault, so
product writers rename through ``atomic_write.replace_with_retry`` (or
``rename_with_retry`` for a move that must never overwrite).

This module counts, per product file, the calls that rename by name and do not
retry: ``os.replace(a, b)``, ``os.rename(a, b)``, and the one-argument
``path.replace(b)`` / ``path.rename(b)``. A rename through ``src_dir_fd`` /
``dst_dir_fd`` is POSIX-only and not counted. The tree already holds many; each file a
change touches is held to its count at the merge-base.

* A file's count may not rise; a new file starts at 0, a deleted file passes.
* A rename moved with its function from another file in the same change is not new.
* A rename that is safe bare (a directory staged under a fresh name, a path no other
  process can know yet) carries ``# rename-ok: <reason>`` on its line.

``test_windows_replace_retry_sweep.py`` checks the writers already converted; this one
stops a new bare rename landing beside them. Test files are not counted: the
flake-pattern ratchet owns them (K23 covers the reader side of the same race).
"""

from __future__ import annotations

import ast
import os
import re
import warnings
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

import pytest
from source_corpus import repo_root
from test_flake_pattern_ratchet import (
    _drop_ambient_git,
    _is_python_test_file,
    _moved_into,
    _run_git,
    ratchet_scope,
)
from test_ratchet_scope import assert_fixture_ignores_ambient_git

#: Env var that forces an explicit base, as ``FLAKE_RATCHET_BASE`` does for its ratchet.
BASE_ENV = "RENAME_RATCHET_BASE"
#: A ``rename-ok`` reason shorter than this is not a reason.
MIN_REASON_CHARS = 10
_RENAME_OK = re.compile(r"#\s*rename-ok:\s*(?P<reason>\S.*?)\s*$")
_RENAMES = frozenset({"replace", "rename"})
#: ``(class, enclosing function text)``: the shape the flake-pattern ratchet's move rule reads.
_Site = tuple[str, str]


def _counted(rel: str) -> bool:
    return rel.startswith("src/") and rel.endswith(".py") and not _is_python_test_file(rel)


def _rename_ok(line: str) -> bool:
    found = _RENAME_OK.search(line)
    return bool(found and len(found["reason"]) >= MIN_REASON_CHARS)


def is_bare_rename(node: ast.Call) -> bool:
    """``os.replace(a, b)`` / ``os.rename(a, b)``, or ``p.replace(b)`` / ``p.rename(b)``."""
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in _RENAMES:
        return False
    if any(kw.arg in {"src_dir_fd", "dst_dir_fd"} for kw in node.keywords):
        return False
    owner = func.value
    if (isinstance(owner, ast.Name) and owner.id == "os") or (
        isinstance(owner, ast.Attribute) and owner.attr == "os"
    ):
        return len(node.args) == 2
    # One positional and no keyword: a Path method, not ``str.replace(old, new)``
    # or ``datetime.replace(tzinfo=...)``.
    return len(node.args) == 1 and not node.keywords


def _findings(source: str) -> Iterable[tuple[_Site, bool]]:
    """``(site identity, excused by rename-ok)`` per bare rename: the identity is the
    enclosing function's unparsed text, so a site that moves with its function keeps it."""
    if "replace" not in source and "rename" not in source:
        return
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return
    lines = source.splitlines()
    stack: list[tuple[ast.AST, ast.AST | None]] = [(tree, None)]
    while stack:
        node, function = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node
        if isinstance(node, ast.Call) and is_bare_rename(node):
            span = range(node.lineno, (node.end_lineno or node.lineno) + 1)
            excused = any(_rename_ok(lines[n - 1]) for n in span if 0 < n <= len(lines))
            yield ("bare-rename", ast.unparse(function or node)), excused
        stack.extend((child, function) for child in ast.iter_child_nodes(node))


def sites(source: str) -> tuple[Counter[_Site], Counter[_Site]]:
    """``(counted, present)``: identity -> count, without and with the excused lines."""
    counted: Counter[_Site] = Counter()
    present: Counter[_Site] = Counter()
    for site, ok in _findings(source):
        present[site] += 1
        if not ok:
            counted[site] += 1
    return counted, present


def count(source: str) -> int:
    return sum(sites(source)[0].values())


def _sites_at(
    root: Path, base: str | None, rel: str | None
) -> tuple[Counter[_Site], Counter[_Site]]:
    if rel is None or not _counted(rel):
        return Counter(), Counter()
    if base is not None:
        text = ratchet_scope.show_at(base, rel, cwd=root)
    else:
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = None
    return sites(text) if text is not None else (Counter(), Counter())


def change_risen(root: Path, base: str) -> dict[str, tuple[int, int]]:
    """``path -> (count now, count allowed)`` for every file this change raises."""
    paths: list[str | None] = []
    befores: list[Counter[_Site]] = []
    nows: list[Counter[_Site]] = []
    gones: list[Counter[_Site]] = []
    pairs = ratchet_scope.change_files(base, cwd=root)
    for old, new in sorted(pairs, key=lambda pair: str(pair[1] or pair[0])):
        before, _ = _sites_at(root, base, old)
        now, now_present = _sites_at(root, None, new)
        paths.append(new)
        befores.append(before)
        nows.append(now)
        gones.append(before - now_present)
    was = [sum(before.values()) for before in befores]
    total = [sum(now.values()) for now in nows]
    unforgiven = _moved_into(
        [max(0, w - c) for w, c in zip(was, total)],
        gones,
        [now - before for before, now in zip(befores, nows)],
        [max(0, c - w) if path is not None else 0 for path, w, c in zip(paths, was, total)],
    )
    return {
        path: (c, c - excess)
        for path, c, excess in zip(paths, total, unforgiven)
        if path is not None and excess > 0
    }


def test_no_changed_product_file_gains_a_bare_rename() -> None:
    root = repo_root()
    base, how = ratchet_scope.change_merge_base(BASE_ENV, cwd=root)
    if base is None:
        warnings.warn(f"bare-rename ratchet compared nothing: {how}", UserWarning, stacklevel=1)
        return
    risen = change_risen(root, base)
    detail = "\n".join(
        f"  {path}: {now} > {allowed}" for path, (now, allowed) in sorted(risen.items())
    )
    assert not risen, (
        "this change adds a bare by-name rename to a product file; on Windows it loses the "
        "write to any handle held on either path (WinError 5 / 32). Rename through "
        "kiro_crew.atomic_write.replace_with_retry (or rename_with_retry), or carry "
        f"`# rename-ok: <reason>` on its line. Counts against {base[:12]} ({how}):\n{detail}"
    )


# -- the detector counts what it says, and only that --------------------------------

_POSITIVES = (
    "import os\ndef f(a, b):\n    os.replace(a, b)\n",
    "import os\ndef f(a, b):\n    os.rename(a, b)\n",
    "def f(tmp, path):\n    tmp.replace(path)\n",
    "def f(tmp, path):\n    tmp.rename(path)\n",
    "def f(a, b):\n    self.os.replace(a, b)\n",
)
_NEAR_MISSES = (
    "def f(s):\n    return s.replace('a', 'b')\n",
    "def f(d):\n    return d.replace(tzinfo=None)\n",
    "import os\ndef f(a, b, fd):\n    os.replace(a, b, src_dir_fd=fd, dst_dir_fd=fd)\n",
    "from kiro_crew.atomic_write import replace_with_retry\ndef f(a, b):\n"
    "    replace_with_retry(a, b)\n",
    "import os\ndef f(a, b):\n    os.replace(a, b)  # rename-ok: staged under a fresh uuid name\n",
)


@pytest.mark.parametrize("source", _POSITIVES)
def test_a_bare_rename_is_counted(source: str) -> None:
    assert count(source) == 1


@pytest.mark.parametrize("source", _NEAR_MISSES)
def test_a_retried_or_excused_or_non_path_call_is_not_counted(source: str) -> None:
    assert count(source) == 0


def test_a_rename_ok_needs_a_reason() -> None:
    assert count("import os\ndef f(a, b):\n    os.replace(a, b)  # rename-ok: tmp\n") == 1


def test_only_product_files_under_src_are_counted() -> None:
    assert _counted("src/kiro_crew/eval/tasks.py")
    assert not _counted("src/kiro_crew/apps/builtins/x/tests/test_a.py")
    assert not _counted("test/test_a.py")
    assert not _counted("scripts/a.py")


# -- the change in front of the ratchet ----------------------------------------------

_WRITER = "import os\ndef write_{name}(tmp, path):\n    os.replace(tmp, path)\n"
_SAFE = (
    "from kiro_crew.atomic_write import replace_with_retry\n"
    "def write(tmp, path):\n    replace_with_retry(tmp, path)\n"
)


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """A repo whose ``main`` holds a product file with one bare rename, on a branch."""
    _drop_ambient_git(monkeypatch)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GITHUB_BASE_REF", "GITHUB_EVENT_NAME", BASE_ENV):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "repo"
    (root / "src" / "kiro_crew").mkdir(parents=True)
    (root / "src" / "kiro_crew" / "a.py").write_text(_WRITER.format(name="a"), encoding="utf-8")
    (root / "src" / "kiro_crew" / "b.py").write_text(_SAFE, encoding="utf-8")
    _run_git(root, "init", "-q")
    _run_git(root, "checkout", "-q", "-b", "main")
    _run_git(root, "add", "-A")
    _run_git(root, "commit", "-q", "-m", "base")
    _run_git(root, "checkout", "-q", "-b", "change")
    return root


def _base(root: Path) -> str:
    base, how = ratchet_scope.change_merge_base(BASE_ENV, cwd=root, env={})
    assert base is not None, how
    return base


def test_the_git_fixture_ignores_ambient_git_state(request, monkeypatch, tmp_path) -> None:
    assert_fixture_ignores_ambient_git(request, monkeypatch, tmp_path, "git_repo")


def test_a_new_bare_rename_in_a_changed_file_is_named(git_repo) -> None:
    (git_repo / "src" / "kiro_crew" / "b.py").write_text(_WRITER.format(name="b"), encoding="utf-8")
    assert change_risen(git_repo, _base(git_repo)) == {"src/kiro_crew/b.py": (1, 0)}


def test_a_legacy_site_and_a_converted_one_keep_the_change_green(git_repo) -> None:
    (git_repo / "src" / "kiro_crew" / "b.py").write_text(_SAFE + "\n# edited\n", encoding="utf-8")
    assert change_risen(git_repo, _base(git_repo)) == {}
    (git_repo / "src" / "kiro_crew" / "a.py").write_text(_SAFE, encoding="utf-8")
    assert change_risen(git_repo, _base(git_repo)) == {}


def test_a_site_moved_with_its_function_is_not_new(git_repo) -> None:
    (git_repo / "src" / "kiro_crew" / "a.py").write_text("X = 1\n", encoding="utf-8")
    (git_repo / "src" / "kiro_crew" / "c.py").write_text(_WRITER.format(name="a"), encoding="utf-8")
    assert change_risen(git_repo, _base(git_repo)) == {}
    (git_repo / "src" / "kiro_crew" / "c.py").write_text(
        _WRITER.format(name="a") + _WRITER.format(name="c").replace("import os\n", ""),
        encoding="utf-8",
    )
    assert change_risen(git_repo, _base(git_repo)) == {"src/kiro_crew/c.py": (2, 1)}
