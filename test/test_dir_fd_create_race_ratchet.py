"""No new descriptor-relative open may create a file with a nonexclusive ``O_CREAT``.

On Darwin, two callers that both ``os.open(name, flags | O_CREAT, dir_fd=fd)`` an
absent name can get ``ENOENT`` back: a pure-stdlib probe of six concurrent
creators measured about 440 failures in 1800 opens, and an exclusive create
measured none. ``platform_compat.open_create_or_existing`` is the race-safe
spelling. code_review_sage's layout lock hit this and failed a nightly build.

The scan resolves flags one level deep: a flags NAME is looked up in the same
function's assignments. The sites below predate the ratchet and each has one
writer per name, so none of them can meet a sibling creator; a new site goes
through ``open_create_or_existing`` instead of onto this list.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

#: (path under src/kiro_crew, enclosing function) -> why it cannot race.
ALLOWED = {
    ("diag/recorder.py", "_append"): "one recorder per gateway, appends under its own thread lock",
    ("acp/_frame_record.py", "_open_private_append"): (
        "opt-in debug frame recording; a failed open stands recording down and costs no turn"
    ),
    ("apps/builtins/aws_control/crew/packaging/pipeline/destination.py", "_write_bytes_nofollow"): (
        "one build process writes each staged file once"
    ),
}


def _names(node: ast.AST) -> set[str]:
    return {
        n.attr if isinstance(n, ast.Attribute) else n.id
        for n in ast.walk(node)
        if isinstance(n, (ast.Attribute, ast.Name))
    }


def _racy_functions(tree: ast.AST) -> set[str]:
    """Names of functions in *tree* holding a dir_fd open that creates nonexclusively."""
    found: set[str] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        assigned: dict[str, set[str]] = {}
        for st in ast.walk(fn):
            if isinstance(st, ast.Assign):
                for target in st.targets:
                    if isinstance(target, ast.Name):
                        assigned.setdefault(target.id, set()).update(_names(st.value))
        for call in ast.walk(fn):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "open"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "os"
                and len(call.args) >= 2
                and any(k.arg == "dir_fd" for k in call.keywords)
            ):
                continue
            flags = _names(call.args[1])
            for name in list(flags):
                flags |= assigned.get(name, set())
            if "O_CREAT" in flags and "O_EXCL" not in flags:
                found.add(fn.name)
    return found


@functools.lru_cache(maxsize=1)
def _racy_sites() -> frozenset[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if "/tests/" in f"/{rel}" or path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found.update((rel, fn) for fn in _racy_functions(tree))
    return frozenset(found)


def test_no_new_descriptor_relative_nonexclusive_create():
    new = sorted(_racy_sites() - set(ALLOWED))
    assert not new, (
        "os.open(..., O_CREAT, dir_fd=...) without O_EXCL can return ENOENT on Darwin "
        "when two callers create the same name; use "
        f"platform_compat.open_create_or_existing instead: {new}"
    )


def test_every_allowed_site_still_exists():
    """A fixed site leaves the list, so the list never hides a new one by name."""
    gone = sorted(set(ALLOWED) - _racy_sites())
    assert not gone, f"remove these from ALLOWED, they no longer create racily: {gone}"


def test_the_scan_flags_the_layout_lock_shape_and_not_the_fix():
    """Non-vacuity: the old layout-lock open is flagged; the exclusive create is not."""
    racy = ast.parse(
        "import os\n"
        "def old(dir_fd):\n"
        "    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW\n"
        "    return os.open('.layout.lock', flags, 0o600, dir_fd=dir_fd)\n"
        "def by_name(path):\n"
        "    return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "def exclusive(dir_fd):\n"
        "    return os.open('x', os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd)\n"
    )
    assert _racy_functions(racy) == {"old"}
