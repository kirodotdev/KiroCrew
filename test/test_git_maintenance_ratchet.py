"""A test file that builds its own git env keeps git's auto maintenance off.

The repo-root ``conftest`` turns auto maintenance off through ``GIT_CONFIG_GLOBAL``
and ``GIT_CONFIG_COUNT``. A helper that strips ``GIT_*`` keys from an env, or
assigns ``GIT_CONFIG_GLOBAL`` or ``GIT_CONFIG_COUNT`` in an env dict of its own,
drops both, and every commit under it can leave a detached
``git maintenance run --auto`` behind. Such a file must also call
``git_maintenance_off_entries`` or ``git_global_config_text`` from
``git_maintenance_helpers``, or read ``GIT_MAINTENANCE_OFF_CONFIG``, which a
file writes into a hermetic remote's own config (a local push starts the
remote's ``receive-pack`` with neither the global file nor ``GIT_CONFIG_*``).

The scan reads every ``*.py`` under ``test/`` and ``src/**/tests/``, helper
modules and conftests included, because env builders live in helper modules
too (``ai_review_workflows_helpers``). It sees these shapes:

* a ``.startswith("GIT_")`` call (the usual strip comprehension);
* ``"GIT_CONFIG_GLOBAL"`` or ``"GIT_CONFIG_COUNT"`` as a dict-literal key, a
  keyword argument (``env.update(GIT_CONFIG_GLOBAL=...)``, ``dict(...)``) or a
  subscript assignment target, except on ``os.environ`` itself, which the
  function-scoped floor re-covers.

Code inside an ``assert`` is skipped (a test reading the env production code
built). ``_NOT_A_GIT_ENV`` lists the files whose matches build no env git runs
under, each with its reason; an entry that stops matching fails.

It does not see an env built from nothing (``env={"PATH": ...}``), a strip
written another way (a slice compare, a regex, a helper outside both trees), or
a name assembled at run time. It checks the file, not the helper: a file that
calls the helpers once passes even if a second helper forgets them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_git_maintenance_ratchet")

_CONFIG_NAMES = frozenset({"GIT_CONFIG_GLOBAL", "GIT_CONFIG_COUNT"})
_REMEDIES = frozenset({"git_maintenance_off_entries", "git_global_config_text"})
_REMEDY_CONSTANT = "GIT_MAINTENANCE_OFF_CONFIG"

#: Files whose matches build no env any git runs under.
_NOT_A_GIT_ENV = {
    "test/test_apps_registry_coverage_characterization.py": (
        "builds the input mapping for registry._git_transport_env and runs no git"
    ),
    "test/test_dev_fleet_app_trusted_helpers.py": (
        "builds the trusted-helper mapping _build_env layers in and runs no git"
    ),
}


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _is_config_name(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value in _CONFIG_NAMES


def env_rebuild_sites(source: str) -> list[int]:
    """Line numbers where *source* strips ``GIT_*`` or assigns the config names."""
    tree = ast.parse(source)
    asserted = {
        id(inner)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assert)
        for inner in ast.walk(node)
    }
    sites: list[int] = []
    for node in ast.walk(tree):
        if id(node) in asserted:
            continue
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "startswith"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "GIT_"
            ):
                sites.append(node.lineno)
            if any(kw.arg in _CONFIG_NAMES for kw in node.keywords):
                sites.append(node.lineno)
        elif isinstance(node, ast.Dict):
            if any(_is_config_name(key) for key in node.keys):
                sites.append(node.lineno)
        elif isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Subscript)
                    and _is_config_name(target.slice)
                    and not _is_os_environ(target.value)
                ):
                    sites.append(node.lineno)
    return sorted(set(sites))


def calls_a_remedy(source: str) -> bool:
    """Whether *source* calls either helper or reads ``GIT_MAINTENANCE_OFF_CONFIG``."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id == _REMEDY_CONSTANT:
            return True
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in _REMEDIES:
                return True
    return False


def _scanned_files(root: Path) -> list[Path]:
    files = set((root / "test").rglob("*.py"))
    files.update(root.glob("src/**/tests/**/*.py"))
    return sorted(files)


def _offenders(root: Path) -> list[str]:
    offenders = []
    for path in _scanned_files(root):
        rel = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8")
        if rel in _NOT_A_GIT_ENV or "GIT_" not in source:
            continue
        sites = env_rebuild_sites(source)
        if sites and not calls_a_remedy(source):
            offenders.append(f"{rel}:{sites[0]}")
    return offenders


def test_every_file_that_rebuilds_a_git_env_keeps_maintenance_off() -> None:
    offenders = _offenders(ROOT)
    assert offenders == [], (
        "these files build a git env that drops the suite's maintenance floor; apply "
        "env.update(git_maintenance_off_entries(env)) to the finished env, or write "
        "git_global_config_text() into the global file it points at: " + ", ".join(offenders)
    )


@pytest.mark.parametrize("rel", sorted(_NOT_A_GIT_ENV))
def test_every_exempt_file_still_matches(rel: str) -> None:
    source = (ROOT / rel).read_text(encoding="utf-8")
    assert env_rebuild_sites(source), f"{rel} no longer matches; drop its exemption"
    assert not calls_a_remedy(source), f"{rel} calls a helper; drop its exemption"


def test_the_scan_reads_both_testpaths() -> None:
    files = _scanned_files(ROOT)
    assert ROOT / "test" / "test_ai_review_shallow_fetch.py" in files
    assert ROOT / "test" / "ai_review_workflows_helpers.py" in files
    assert ROOT / "test" / "conftest.py" in files
    assert any("src" in path.relative_to(ROOT).parts[:1] for path in files)


@pytest.mark.parametrize(
    "rel", ["test/fixture_env_helpers.py", "src/pkg/tests/env_helpers.py", "test/sub/conftest.py"]
)
def test_a_planted_helper_module_without_the_helpers_is_caught(tmp_path: Path, rel: str) -> None:
    planted = tmp_path / rel
    planted.parent.mkdir(parents=True)
    planted.write_text(_PLANTED["strip_comprehension"], encoding="utf-8")
    assert _offenders(tmp_path) == [f"{rel}:1"]
    planted.write_text(
        _PLANTED["strip_comprehension"] + "env.update(git_maintenance_off_entries(env))\n",
        encoding="utf-8",
    )
    assert _offenders(tmp_path) == []


_PLANTED = {
    "strip_comprehension": "env = {k: v for k, v in environ.items() if not k.startswith('GIT_')}\n",
    "dict_literal_global": "env = {'GIT_CONFIG_GLOBAL': '/x', 'PATH': '/bin'}\n",
    "update_keyword_global": "env = {}\nenv.update(GIT_CONFIG_GLOBAL='/x')\n",
    "dict_call_count": "env = dict(GIT_CONFIG_COUNT='1')\n",
    "subscript_count": "env = {}\nenv['GIT_CONFIG_COUNT'] = '1'\n",
}


@pytest.mark.parametrize("shape", sorted(_PLANTED))
def test_a_planted_shape_without_the_helpers_is_caught(shape: str) -> None:
    source = _PLANTED[shape]
    assert env_rebuild_sites(source), shape
    assert not calls_a_remedy(source)


@pytest.mark.parametrize(
    "remedy",
    [*(f"{name}(env)" for name in sorted(_REMEDIES)), f"for k, v in {_REMEDY_CONSTANT}: pass"],
)
@pytest.mark.parametrize("shape", sorted(_PLANTED))
def test_a_planted_shape_with_a_remedy_passes(shape: str, remedy: str) -> None:
    source = _PLANTED[shape] + remedy + "\n"
    assert env_rebuild_sites(source)
    assert calls_a_remedy(source)


def test_an_import_alone_is_not_a_call() -> None:
    source = _PLANTED["strip_comprehension"] + (
        "from git_maintenance_helpers import git_maintenance_off_entries\n"
    )
    assert not calls_a_remedy(source)


@pytest.mark.parametrize(
    "source",
    [
        "import os\nos.environ['GIT_CONFIG_GLOBAL'] = '/x'\n",
        "def t(monkeypatch):\n    monkeypatch.setenv('GIT_CONFIG_GLOBAL', '/x')\n",
        "env = {}\nenv['GIT_AUTHOR_NAME'] = 't'\n",
        "name.startswith('GIT_AUTHOR')\n",
        "assert {k for k in env if k.startswith('GIT_')} == {'GIT_CONFIG_GLOBAL'}\n",
    ],
    ids=["os-environ", "monkeypatch-setenv", "other-git-key", "other-prefix", "in-assert"],
)
def test_a_shape_the_floor_already_covers_is_not_flagged(source: str) -> None:
    assert env_rebuild_sites(source) == []
