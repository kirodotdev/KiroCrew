"""Ratchet: a test file that can damage the macOS libc handle cache must reset it.

``platform_compat`` builds its ``libc`` and ``libproc`` handles once per process
and keeps the first result, ``None`` included. A test that replaces
``ctypes.CDLL`` or ``ctypes.util.find_library`` (or writes one of the cached
handle names directly) and lets a first build happen inside that window leaves
the fake, or ``None``, cached for every later test in the worker. The
``fresh_darwin_libc_handles`` fixture in ``conftest.py`` puts the cache back at
each test's teardown, so every such file requests it from its module
``pytestmark``. This test fails a file that patches without the request.

A patch is found through ``ast``, not text, so a call the formatter wrapped
across lines and a module alias (``program.ctypes``, ``_ct``) are both seen.
Three spellings count: ``setattr(obj, "<name>", ...)`` and
``patch.object(obj, "<name>", ...)``, the dotted-string form
``setattr("ctypes.CDLL", ...)`` / ``patch("ctypes.CDLL")``, and a direct
assignment ``obj.<name> = ...``. A scanner that silently stops matching reports
zero offenders, which reads like success, so the scan is also run against
planted files and the files known to patch must still be found.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

import pytest

_TEST_ROOT = Path(__file__).resolve().parent
_FIXTURE = "fresh_darwin_libc_handles"

#: The handle cache ``fresh_darwin_libc_handles`` resets. Pinned against the
#: fixture's own tuple below, so a name added there is policed here too.
_CACHE_NAMES = frozenset(
    {
        "_darwin_libc_sysctl",
        "_darwin_libc_sysctl_loaded",
        "_darwin_libproc",
        "_darwin_libproc_loaded",
        "_darwin_libproc_tree_bound",
        "_darwin_libproc_fd_bound",
    }
)
#: The loaders a first build reads.
_LOADER_NAMES = frozenset({"CDLL", "find_library"})
_DAMAGING = _CACHE_NAMES | _LOADER_NAMES

#: Every damaging name contains one of these, so a file whose text holds none of
#: them cannot match and is not parsed.
_TEXT_GATE = ("CDLL", "find_library", "_darwin_lib")


def _callee(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _str_arg(call: ast.Call, index: int) -> str | None:
    if len(call.args) > index:
        arg = call.args[index]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return None


def damaging_lines(tree: ast.AST) -> list[int]:
    """Lines that replace a loader or a cached handle name."""
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            callee = _callee(node.func)
            name = _str_arg(node, 1)
            if callee in {"setattr", "object"} and name in _DAMAGING:
                lines.append(node.lineno)
                continue
            target = _str_arg(node, 0)
            if (
                callee in {"setattr", "patch"}
                and target is not None
                and "." in target
                and target.rsplit(".", 1)[1] in _DAMAGING
            ):
                lines.append(node.lineno)
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Attribute) and t.attr in _DAMAGING for t in targets):
                lines.append(node.lineno)
    return sorted(lines)


def requests_fixture(tree: ast.Module) -> bool:
    """True when a module-level ``pytestmark`` carries ``usefixtures(<fixture>)``."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets):
            continue
        for sub in ast.walk(node.value) if node.value is not None else ():
            if (
                isinstance(sub, ast.Call)
                and _callee(sub.func) == "usefixtures"
                and any(isinstance(a, ast.Constant) and a.value == _FIXTURE for a in sub.args)
            ):
                return True
    return False


def scan(root: Path) -> tuple[dict[str, list[int]], set[str]]:
    """Return ``(offenders, damaging)`` for the ``.py`` files under ``root``.

    ``damaging`` names every file that patches, ``offenders`` maps the ones
    without the module request to the lines that patch.
    """
    offenders: dict[str, list[int]] = {}
    damaging: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not any(token in text for token in _TEXT_GATE):
            continue
        tree = ast.parse(text, filename=str(path))
        lines = damaging_lines(tree)
        if not lines:
            continue
        rel = path.relative_to(root).as_posix()
        damaging.add(rel)
        if not requests_fixture(tree):
            offenders[rel] = lines
    return offenders, damaging


@functools.cache
def _tree_scan() -> tuple[dict[str, list[int]], frozenset[str]]:
    """The test tree is read once per worker for both tree-wide tests."""
    offenders, damaging = scan(_TEST_ROOT)
    return offenders, frozenset(damaging)


def test_every_file_that_patches_the_libc_loaders_requests_the_reset() -> None:
    offenders, _ = _tree_scan()
    assert not offenders, (
        "these test files replace ctypes.CDLL, find_library or a cached macOS libc "
        "handle without resetting the shared cache, so a handle built from the fake "
        "can outlive the test. Add\n"
        f'    pytestmark = pytest.mark.usefixtures("{_FIXTURE}")\n'
        "(or append it to the existing module pytestmark list):\n"
        + "\n".join(f"  {rel}: lines {lines}" for rel, lines in offenders.items())
    )


def test_the_scan_still_finds_the_files_known_to_patch() -> None:
    _, damaging = _tree_scan()
    for rel in (
        "test_host_available_memory.py",
        "test_platform_compat_coverage.py",
        "test_macos_x86_64_cpu_guard.py",
        "test_computer_use_ffi.py",
        "test_sandbox_argv.py",
        "test_session_rss_darwin.py",
    ):
        assert rel in damaging, f"the scan no longer sees the patches in {rel}"


def test_the_cache_names_match_what_the_fixture_resets() -> None:
    tree = ast.parse((_TEST_ROOT / "conftest.py").read_text(encoding="utf-8"))
    fixture = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == _FIXTURE
    )
    reset = {
        elt.elts[0].value
        for node in ast.walk(fixture)
        if isinstance(node, ast.Tuple)
        for elt in node.elts
        if isinstance(elt, ast.Tuple)
        and elt.elts
        and isinstance(elt.elts[0], ast.Constant)
        and isinstance(elt.elts[0].value, str)
    }
    assert reset == _CACHE_NAMES


_PATCHES = {
    "monkeypatch": 'monkeypatch.setattr(ctypes, "CDLL", lambda *a: None)',
    "wrapped": 'monkeypatch.setattr(\n        program.ctypes,\n        "find_library",\n        f,\n    )',
    "dotted": 'monkeypatch.setattr("ctypes.util.find_library", f)',
    "patch_object": 'mock.patch.object(ctypes, "CDLL", f)',
    "patch_string": 'mock.patch("ctypes.CDLL", f)',
    "cache": 'monkeypatch.setattr(pc, "_darwin_libproc_loaded", True)',
    "assign": "pc._darwin_libc_sysctl = None",
}


def _plant(root: Path, name: str, body: str, header: str = "") -> None:
    (root / name).write_text(
        f"import pytest\n{header}\n\ndef test_it(monkeypatch):\n    {body}\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("spelling", sorted(_PATCHES))
def test_a_planted_patch_without_the_request_is_an_offender(tmp_path: Path, spelling: str) -> None:
    _plant(tmp_path, "test_planted.py", _PATCHES[spelling])
    offenders, damaging = scan(tmp_path)
    assert "test_planted.py" in damaging
    assert offenders["test_planted.py"]


@pytest.mark.parametrize(
    "header",
    [
        f'pytestmark = pytest.mark.usefixtures("{_FIXTURE}")',
        f'pytestmark = [pytest.mark.timeout(5), pytest.mark.usefixtures("{_FIXTURE}")]',
    ],
)
def test_the_module_request_clears_a_planted_patch(tmp_path: Path, header: str) -> None:
    _plant(tmp_path, "test_planted.py", _PATCHES["monkeypatch"], header)
    offenders, damaging = scan(tmp_path)
    assert "test_planted.py" in damaging
    assert offenders == {}


@pytest.mark.parametrize(
    "header",
    [
        # A per-test request is not enough: the file's other tests still patch.
        "",
        'pytestmark = pytest.mark.usefixtures("some_other_fixture")',
    ],
)
def test_a_request_that_is_not_module_wide_does_not_count(tmp_path: Path, header: str) -> None:
    (tmp_path / "test_planted.py").write_text(
        f"import pytest\n{header}\n\n"
        f'@pytest.mark.usefixtures("{_FIXTURE}")\n'
        "def test_it(monkeypatch):\n"
        f"    {_PATCHES['monkeypatch']}\n",
        encoding="utf-8",
    )
    offenders, _ = scan(tmp_path)
    assert "test_planted.py" in offenders


def test_a_file_that_only_names_the_loaders_is_not_damaging(tmp_path: Path) -> None:
    (tmp_path / "test_planted.py").write_text(
        'LOADERS = {"CDLL", "find_library"}\n'
        "def test_it(monkeypatch):\n"
        '    monkeypatch.setattr(pc, "_darwin_libproc_handle", lambda: None)\n',
        encoding="utf-8",
    )
    assert scan(tmp_path) == ({}, set())
