"""User-facing messages link docs by full URL, not by a repo-relative path.

An installed package ships no ``docs/`` folder, so a warning ending in
``See docs/architecture/resource-protection.md`` points the reader at a file
they do not have. This scans the string literals of the modules whose
warnings and setup text carried such a pointer; docstrings are exempt because
only contributors read them.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"

#: A ``docs/...md`` path that is not the tail of a URL.
REPO_RELATIVE_DOC = re.compile(r"(?<![/\w])docs/[\w./-]+\.md")

MODULES = (
    "sandbox_cgroup.py",
    "sandbox_slice.py",
    "sandbox_wrap.py",
    "cli_setup.py",
    "dashboard/messaging_api/slack_workspace.py",
)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            found.add(id(node.value))
    return found


@pytest.mark.parametrize("relpath", MODULES)
def test_message_strings_carry_no_repo_relative_doc_path(relpath: str) -> None:
    tree = ast.parse((SRC / relpath).read_text(encoding="utf-8"))
    docstrings = _docstring_nodes(tree)
    offenders = [
        (node.lineno, match)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        for match in REPO_RELATIVE_DOC.findall(node.value)
    ]
    assert offenders == [], f"{relpath}: repo-relative doc path in a message: {offenders}"


def test_pattern_accepts_full_url_and_rejects_bare_path() -> None:
    url = "See https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/docker.md"
    assert REPO_RELATIVE_DOC.search(url) is None
    assert REPO_RELATIVE_DOC.search("See docs/guides/docker.md.") is not None
