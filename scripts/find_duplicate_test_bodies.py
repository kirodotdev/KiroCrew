#!/usr/bin/env python3
"""find_duplicate_test_bodies.py -- count test functions whose bodies are identical.

A group is two or more ``test_*`` functions (module-level or methods, sync or
async) whose bodies have the same ``ast.dump``. ``ast.dump`` omits line and
column numbers, so the same statements at different places in different files
compare equal, while any difference in a name, a literal or a docstring keeps two
bodies apart. Decorators and the argument list are not part of the body, so a
copy that only changes which fixture it takes still counts as a copy.

A copied body is a candidate for one parametrized test: the copies differ only
in module-level data, and a param id per copy keeps every one of them checked.

Usage::

    python3 scripts/find_duplicate_test_bodies.py            # counts for test/
    python3 scripts/find_duplicate_test_bodies.py --top 10   # plus the 10 largest groups
    python3 scripts/find_duplicate_test_bodies.py --glob 'test_*facade*.py'
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterator, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
_SKIP_DIRS = frozenset({"node_modules", ".venv", "__pycache__", ".worktrees"})

Site = Tuple[str, int, str]


def _test_functions(tree: ast.AST) -> Iterator[ast.AST]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test"
        ):
            yield node


def _iter_files(root: Path, pattern: str) -> Iterator[Path]:
    for path in sorted(root.rglob(pattern)):
        if not _SKIP_DIRS.intersection(path.relative_to(root).parts):
            yield path


def find_groups(root: Path, pattern: str = "test_*.py") -> List[List[Site]]:
    """Return every group of two or more test sites sharing one body, largest first."""
    by_body: Dict[str, List[Site]] = defaultdict(list)
    for path in _iter_files(root, pattern):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as exc:
            print(f"skip {path}: {exc}", file=sys.stderr)
            continue
        rel = (
            path.relative_to(REPO_ROOT).as_posix() if path.is_relative_to(REPO_ROOT) else str(path)
        )
        for fn in _test_functions(tree):
            key = ast.dump(ast.Module(body=fn.body, type_ignores=[]))
            by_body[key].append((rel, fn.lineno, fn.name))
    groups = [sorted(sites) for sites in by_body.values() if len(sites) > 1]
    groups.sort(key=lambda g: (-len(g), g[0]))
    return groups


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "root",
        nargs="?",
        default=str(REPO_ROOT / "test"),
        help="directory to scan (default: test/)",
    )
    parser.add_argument(
        "--glob", default="test_*.py", help="file pattern, matched recursively (default: test_*.py)"
    )
    parser.add_argument("--top", type=int, default=0, help="also list the N largest groups")
    args = parser.parse_args(argv)

    groups = find_groups(Path(args.root).resolve(), args.glob)
    print(f"duplicate-body groups: {len(groups)}")
    print(f"tests in those groups: {sum(len(g) for g in groups)}")
    for group in groups[: args.top]:
        path, line, name = group[0]
        print(f"  x{len(group)}  {path}:{line} {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
