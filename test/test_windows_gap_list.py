"""Audit ``windows-expected-failures.txt`` itself.

The list is applied as ``pytest.mark.skip`` (rootdir ``conftest.py``), and a skipped
test asserts nothing — so the list cannot report its own decay. Two failure modes
have already happened and neither showed up anywhere:

* **A dead entry.** A renamed or deleted test leaves a line that matches no node id.
  It is silently inert, and the burn-down count it inflates is wrong in the
  direction that makes the backlog look bigger than it is.
* **An orphaned test.** A test that already guards itself to Windows runs on no
  platform once it is also listed here — the POSIX matrix skips it by its own guard
  and Windows skips it by this list. That is how the owner-only DACL applied to every
  secret at rest came to be verified nowhere.

Both audits run on the POSIX matrix, where they always execute:
``test_mcp_gateway_pool_integ`` makes the same argument for guarding coverage from
outside the suite it protects, and does it for one file by hand.

Resolution is by AST, not by collecting the suite: collecting every testpath
takes over a minute. Each file is parsed once, reduced to one small dict, and its
tree dropped before the next. That ordering is load-bearing for memory, not tidiness:
holding all 90-odd trees at once costs ~250 MiB of worker peak, while dropping each
one costs ~34 MiB — the peak of the single largest listed module, which is the floor
for any AST approach. Per-worker memory is the resource this suite is least able to
spare, so keep the parse local if this grows.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LISTFILE = Path(__file__).with_name("windows-expected-failures.txt")

#: Condition fragments that mean "this only runs on Windows". Spelled as source text
#: because that is what both readers (a ``skipif`` condition and an early-return
#: ``pytest.skip`` guard) have in common.
_WINDOWS_ONLY_CONDITIONS = (
    "not IS_WINDOWS",
    "not pc.IS_WINDOWS",
    "not platform_compat.IS_WINDOWS",
    'os.name != "nt"',
    "os.name != 'nt'",
    'sys.platform != "win32"',
    "sys.platform != 'win32'",
    'platform.system() != "Windows"',
    "platform.system() != 'Windows'",
)

_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _entries() -> list[str]:
    text = _LISTFILE.read_text(encoding="utf-8")
    found = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    assert found, (
        f"read no entries from {_LISTFILE.name} — this audit has gone blind and would "
        "pass no matter what the list contained"
    )
    return found


def _windows_only_decorator(nodes: list[ast.AST]) -> bool:
    for holder in nodes:
        for dec in getattr(holder, "decorator_list", []):
            # ``ast.get_source_segment`` splits the complete module source on every
            # call.  Some listed modules contain thousands of tests, making this
            # audit repeatedly rescan those files until pytest's timeout fires.
            # Unparsing walks only the decorator node and preserves everything this
            # source-text check needs.
            text = ast.unparse(dec)
            if "skipif" in text and any(c in text for c in _WINDOWS_ONLY_CONDITIONS):
                return True
    return False


def _windows_only_body(fn: ast.AST) -> bool:
    """True when a statement directly in the body skips the test off Windows.

    Only a guard that calls ``pytest.skip`` counts. A condition that merely brackets
    ONE assertion (``if os.name != "nt": assert mode == 0o600``) does not: the test
    still runs and still asserts everything else, so treating it as Windows-only
    would make the audit reject legitimate entries and train readers to delete it.
    """
    for stmt in getattr(fn, "body", []):
        if not isinstance(stmt, ast.If):
            continue
        cond = ast.unparse(stmt.test)
        if not any(c in cond for c in _WINDOWS_ONLY_CONDITIONS):
            continue
        for inner in ast.walk(ast.Module(body=stmt.body, type_ignores=[])):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "skip"
            ):
                return True
    return False


@functools.lru_cache(maxsize=None)
def _file_index(path: Path) -> dict[str, bool] | None:
    """``{"Class::test" | "test": runs_only_on_windows}`` for one test module.

    ``None`` when the file cannot be read or parsed. The parsed tree stays local so
    it is freed before the next file: only this small dict is cached.
    """
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return None

    index: dict[str, bool] = {}
    classes: dict[str, ast.ClassDef] = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    for node in tree.body:
        if isinstance(node, _DEFS):
            index[node.name] = _windows_only_decorator([node]) or _windows_only_body(node)
    for cls_name, cls in classes.items():
        own = {n.name: n for n in cls.body if isinstance(n, _DEFS)}
        # A test may be inherited from a mixin declared in the same module; without
        # this it would read as deleted and the audit would demand a live line's
        # removal.
        inherited: dict[str, ast.AST] = {}
        for base in cls.bases:
            base_cls = classes.get(base.id) if isinstance(base, ast.Name) else None
            if base_cls is None:
                continue
            inherited.update({n.name: n for n in base_cls.body if isinstance(n, _DEFS)})
        for name, fn in {**inherited, **own}.items():
            index[f"{cls_name}::{name}"] = _windows_only_decorator([fn, cls]) or _windows_only_body(
                fn
            )
    return index


def _lookup(entry: str) -> tuple[str, bool | None]:
    """``(why_dead, windows_only)`` for one list entry; ``why_dead`` empty when live."""
    path_part, sep, rest = entry.partition("::")
    if not sep:
        return "not a node id: no '::'", None
    path = _REPO_ROOT / path_part
    if not path.exists():
        return "file does not exist", None
    if "@" in rest:
        # ``@name`` is xdist's loadgroup scheduling key, not part of a node id, so an
        # entry carrying one can never match. Named separately because the lookup
        # below would otherwise report it as a plain rename.
        return "carries an xdist @group suffix, which node ids never have", None
    index = _file_index(path)
    if index is None:
        return "file could not be parsed", None
    if rest not in index:
        return "no such test in the file — renamed or deleted?", None
    return "", index[rest]


def test_every_entry_names_a_test_that_still_exists() -> None:
    """No inert lines.

    A line that resolves to nothing skips nothing. It also cannot be told apart from
    a line whose gap is still open, which is what makes the backlog unreadable rather
    than merely stale.
    """
    dead = [f"{e}  ({why})" for e in _entries() for why in [_lookup(e)[0]] if why]
    assert not dead, (
        f"{len(dead)} entries in {_LISTFILE.name} name nothing and skip nothing; "
        "delete them:\n  " + "\n  ".join(dead)
    )


def test_no_entry_is_a_test_that_only_runs_on_windows() -> None:
    """No entry may name a test that already guards itself to Windows.

    Listing one is not a tracked gap, it is the deletion of the test: the POSIX matrix
    skips it by its own guard and Windows skips it by this list. Fix the test, or
    replace the assertion — do not add the line.
    """
    orphans = [e for e in _entries() if _lookup(e)[1]]
    assert not orphans, (
        "these tests only run on Windows AND are skipped on Windows by "
        f"{_LISTFILE.name}, so they run on no platform:\n  " + "\n  ".join(orphans)
    )


@pytest.mark.parametrize(
    ("node_id", "windows_only"),
    [
        # The audits above are only as good as this lookup. Pin one Windows-only test
        # and one that runs everywhere, so a lookup that silently starts answering
        # "not found" or "never Windows-only" stops passing vacuously.
        (
            "test/test_platform_compat.py::TestRestrictToOwner"
            "::test_applies_owner_only_dacl_on_windows",
            True,
        ),
        (
            "test/test_platform_compat.py::TestRestrictToOwner"
            "::test_applies_owner_only_mode_on_posix",
            False,
        ),
        # Brackets ONE assertion with ``if os.name != "nt"`` and still runs
        # everywhere; the detector must not mistake that for a Windows-only test.
        ("test/test_token_auth.py::test_signing_secret_persisted_across_loads", False),
    ],
)
def test_lookup_classifies_known_node_ids(node_id: str, windows_only: bool) -> None:
    why_dead, is_windows_only = _lookup(node_id)
    assert not why_dead, why_dead
    assert is_windows_only is windows_only
