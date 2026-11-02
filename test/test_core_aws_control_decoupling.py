"""GATE: Kiro Crew core does not depend on the aws-control app's code.

The aws-control app, with its crew runtime, bundle builder and image build
scripts, is leaving this repository whole. That is only possible while nothing
outside ``src/kiro_crew/apps/builtins/`` imports it. A core feature that needs
something the app ships asks a seam in :mod:`kiro_crew.apps.seams`, and the app
fills it from its own ``seams.py``.

Two properties are pinned:

1. **No core module imports the app.** Every module under ``src/kiro_crew``
   outside ``apps/builtins/``, and every script under ``scripts/``, is parsed.
   ``import``, ``from ... import`` (relative ones resolved), ``from
   kiro_crew.apps.builtins import aws_control``, ``importlib.import_module`` and
   ``__import__`` with a literal name are all in scope, and the probes below fail
   if any of those forms stops being caught.
2. **The strings that still name the package are a pinned, shrinking list.** A
   path built from ``"aws_control"`` reaches the app as surely as an import does.
   The rows left are security tables keyed by the app's file paths or audit id.
   They are listed in ``KNOWN_NAME_REFERENCES`` with the reason each stays, and
   each is deleted in the PR that removes the app. A new one fails here, and so
   does a listed one that is gone.

If this goes red because you imported the app from core, invert the dependency
through a seam instead. Do not add an exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_core_aws_control_decoupling")

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "kiro_crew"
APP_PACKAGE = "kiro_crew.apps.builtins.aws_control"
APP_NAME = "aws_control"

#: Trees under ``src/kiro_crew`` this gate does not read: the builtin apps
#: themselves, and generated or vendored code.
_SKIPPED = ("apps/builtins/", "static/", "_vendor/")

#: (path relative to ``src/kiro_crew``, the literal): the string constants outside
#: the app that still name its package, each with why it is not a seam. These are
#: the sensitive-store read allowlist (an app may not add itself to it) and the
#: security-posture tables that exempt or classify named app files (core's audit
#: of the app's code). Each row is deleted together with the file it names.
KNOWN_NAME_REFERENCES: frozenset[tuple[str, str]] = frozenset(
    {
        ("hooks.py", "aws_control.conversation_export"),
        (
            "security_posture.py",
            "apps/builtins/aws_control/backend/backup_parts/conversations.py",
        ),
        ("security_posture.py", "apps/builtins/aws_control/backend/backup_parts/egress_text.py"),
        ("security_posture.py", "apps/builtins/aws_control/backend/backup_parts/nightly.py"),
        ("security_posture.py", "apps/builtins/aws_control/backend/backup_parts/retention.py"),
        ("security_posture.py", "apps/builtins/aws_control/backend/routes_parts/common.py"),
        ("security_posture.py", "apps/builtins/aws_control/backend/routes_parts/drive.py"),
        ("security_posture.py", "apps/builtins/aws_control/backend/library.py"),
        ("security_posture.py", "apps/builtins/aws_control/backend/storage.py"),
        ("security_posture.py", "apps/builtins/aws_control/backend/accounts.py"),
        ("security_posture_sinks_delivery.py", "apps/builtins/aws_control/backend/routes.py"),
        ("security_posture_sinks_gateway.py", "apps/builtins/aws_control/backend/backup.py"),
    }
)


def _module_name(path: Path) -> str:
    """The dotted module name of *path* under ``src/``, for resolving relative imports."""
    rel = path.relative_to(SRC.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_app(name: str) -> bool:
    return name == APP_PACKAGE or name.startswith(APP_PACKAGE + ".")


def _resolve_relative(module: str | None, level: int, importer: str, is_package: bool) -> str:
    package = importer.split(".") if is_package else importer.split(".")[:-1]
    base = package[: len(package) - (level - 1)] if level > 1 else package
    return ".".join([*base, module] if module else base)


def _app_imports(tree: ast.AST, importer: str, is_package: bool) -> list[tuple[int, str]]:
    """``(line, imported name)`` for every way *tree* reaches the app's package."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits.extend((node.lineno, a.name) for a in node.names if _is_app(a.name))
        elif isinstance(node, ast.ImportFrom):
            base = (
                _resolve_relative(node.module, node.level, importer, is_package)
                if node.level
                else node.module or ""
            )
            if _is_app(base):
                hits.append((node.lineno, base))
            else:
                hits.extend(
                    (node.lineno, f"{base}.{a.name}")
                    for a in node.names
                    if _is_app(f"{base}.{a.name}")
                )
        elif isinstance(node, ast.Call):
            func = node.func
            called = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else ""
            )
            if called in ("import_module", "__import__") and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if _is_app(arg.value):
                        hits.append((node.lineno, arg.value))
    return hits


def _docstring_nodes(tree: ast.AST) -> set[int]:
    nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                nodes.add(id(body[0].value))
    return nodes


def _name_strings(tree: ast.AST) -> list[tuple[int, str]]:
    """``(line, literal)`` for every non-docstring string constant naming the package."""
    docs = _docstring_nodes(tree)
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and APP_NAME in node.value
        and id(node) not in docs
    ]


def _core_files() -> list[Path]:
    files = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if not rel.startswith(_SKIPPED):
            files.append(path)
    return files


@pytest.fixture(scope="module")
def core_trees() -> dict[Path, ast.AST]:
    """The parsed files that mention the package name at all.

    Every form both scans catch spells ``aws_control`` in the source text, so a file
    without it cannot hold a hit and is not parsed.
    """
    trees = {}
    for path in [*_core_files(), *sorted((ROOT / "scripts").rglob("*.py"))]:
        text = path.read_text(encoding="utf-8")
        if APP_NAME in text:
            trees[path] = ast.parse(text, filename=str(path))
    return trees


def test_no_core_module_or_script_imports_the_app(core_trees):
    offenders = []
    for path, tree in core_trees.items():
        if path.is_relative_to(SRC):
            importer, is_package = _module_name(path), path.name == "__init__.py"
        else:
            importer, is_package = path.stem, False
        offenders.extend(
            f"{path.relative_to(ROOT)}:{line}: {name}"
            for line, name in _app_imports(tree, importer, is_package)
        )
    assert not offenders, (
        "core imports the aws-control app; ask a seam in kiro_crew.apps.seams instead:\n"
        + "\n".join(offenders)
    )


def test_the_strings_naming_the_package_are_the_pinned_list(core_trees):
    found = set()
    unexpected = []
    for path, tree in core_trees.items():
        rel = path.relative_to(SRC).as_posix() if path.is_relative_to(SRC) else str(path)
        for line, literal in _name_strings(tree):
            if (rel, literal) in KNOWN_NAME_REFERENCES:
                found.add((rel, literal))
            else:
                unexpected.append(f"{rel}:{line}: {literal!r}")
    assert (
        not unexpected
    ), "a core string names the aws-control package; reach it through a seam:\n" + "\n".join(
        unexpected
    )
    assert found == KNOWN_NAME_REFERENCES, (
        "a pinned reference is gone; delete its row from KNOWN_NAME_REFERENCES: "
        f"{sorted(KNOWN_NAME_REFERENCES - found)}"
    )


@pytest.mark.parametrize(
    ("source", "importer", "is_package"),
    [
        ("import kiro_crew.apps.builtins.aws_control.backend.accounts", "kiro_crew.x", False),
        ("from kiro_crew.apps.builtins.aws_control import seams", "kiro_crew.x", False),
        ("from kiro_crew.apps.builtins import aws_control", "kiro_crew.x", False),
        ("from ..apps.builtins.aws_control import seams", "kiro_crew.cloud.y", False),
        ("from .builtins.aws_control import seams", "kiro_crew.apps.y", False),
        ("from .builtins import aws_control", "kiro_crew.apps", True),
        (
            "import importlib\nimportlib.import_module('kiro_crew.apps.builtins.aws_control.x')",
            "kiro_crew.x",
            False,
        ),
        ("__import__('kiro_crew.apps.builtins.aws_control')", "kiro_crew.x", False),
        (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from kiro_crew.apps.builtins.aws_control.backend import storage",
            "kiro_crew.x",
            False,
        ),
    ],
)
def test_the_scan_catches_every_import_form(source, importer, is_package):
    assert _app_imports(ast.parse(source), importer, is_package)


@pytest.mark.parametrize(
    "source",
    [
        "from kiro_crew.apps import seams",
        "from kiro_crew.apps.builtins import BUILTIN_NAMES",
        "import kiro_crew.apps.builtins.aws_control_other",
    ],
)
def test_the_scan_leaves_other_imports_alone(source):
    assert not _app_imports(ast.parse(source), "kiro_crew.x", False)


def test_the_string_scan_skips_docstrings_but_not_paths():
    tree = ast.parse(
        '"""Mentions aws_control in prose."""\n'
        'RUNTIME = ("apps", "builtins", "aws_control", "crew")\n'
    )
    assert [literal for _line, literal in _name_strings(tree)] == ["aws_control"]
