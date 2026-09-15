"""``MAIN_REPO`` reaches git and the filesystem only through ``_repo()``.

Dev Fleet represents "no main checkout found" as an empty string in
``MAIN_REPO``. That sentinel is fail-open at any call site that consumes the
global directly: ``git -C ""`` does not fail — it silently runs against the
backend process's working directory — and ``Path("")`` is ``Path(".")``, so an
unguarded consumer operates on an arbitrary directory and returns plausible
results. The ``_repo()`` accessor centralizes the guard: it returns the path or
raises ``RepoNotConfigured``, which the HMAC middleware converts to the 409
``repo_not_configured`` boundary.

Two enforcement tiers (same pattern as ``test_apps_instances_loop_offload.py``):

- Behavior tests: ``_repo()`` raises on the empty sentinel and returns the
  path otherwise, preserving the exception type the middleware boundary maps.
- AST ratchet: outside the accessor itself, a ``MAIN_REPO`` load may appear
  ONLY as a bare truthiness guard (``if MAIN_REPO:`` / ``not MAIN_REPO`` / a
  ``BoolOp`` operand). Any other load — a git argv element, a subprocess
  ``cwd=``, a ``Path(...)`` build, an f-string interpolation, a payload
  field — fails this test, so a future call site cannot silently reintroduce
  the fail-open shape.
"""

from __future__ import annotations

import ast
import inspect
import locale
from types import SimpleNamespace

import pytest

from kiro_crew.apps.builtins.dev_fleet import (
    fleet_state,
    http_api,
    live,
    repository,
    runtime,
    server,
    worktree_ops,
)

# The accessor is the ONLY function whose body may read the bare global: it IS
# the guard. The startup hook's discovery/re-resolve runs on a local and writes
# the global exactly once (a Store, which this ratchet ignores), so even the
# assignment site needs no exemption — and a git call added to startup, where
# MAIN_REPO is most often still unresolved, is caught like anywhere else.
_DEV_FLEET_MODULES = (
    runtime,
    repository,
    live,
    fleet_state,
    worktree_ops,
    http_api,
    server,
)
_ALLOWED_LOADS = {(repository.__name__, "_repo")}


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    cur: ast.AST | None = node
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
        cur = parents.get(cur)
    return None


def _is_bare_truthiness(node: ast.expr, parents: dict[ast.AST, ast.AST]) -> bool:
    """True when the load feeds a truthiness test and nothing else.

    Walking up from the Name, only ``BoolOp`` and ``not`` may intervene before
    the expression lands as the ``test`` of an ``if``/``while`` or a ternary.
    Any other intervening node (a call argument, a container literal, an
    f-string, an assignment value) means the VALUE escapes, which is exactly
    the shape the accessor exists to prevent.
    """
    child: ast.AST = node
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.BoolOp, ast.UnaryOp)):
            if isinstance(cur, ast.UnaryOp) and not isinstance(cur.op, ast.Not):
                return False
            child = cur
            cur = parents.get(cur)
            continue
        if isinstance(cur, (ast.If, ast.While)):
            return cur.test is child
        if isinstance(cur, ast.IfExp):
            return cur.test is child
        return False
    return False


def test_main_repo_loads_only_via_accessor_or_truthiness() -> None:
    violations: list[str] = []
    for module in _DEV_FLEET_MODULES:
        tree = ast.parse(inspect.getsource(module))
        parents = _parent_map(tree)
        for node in ast.walk(tree):
            is_main_repo = (isinstance(node, ast.Name) and node.id == "MAIN_REPO") or (
                isinstance(node, ast.Attribute) and node.attr == "MAIN_REPO"
            )
            if not is_main_repo or not isinstance(node.ctx, ast.Load):
                continue  # assignments (Store) stay on the global by design
            func = _enclosing_function(node, parents)
            if (module.__name__, func) in _ALLOWED_LOADS:
                continue
            if _is_bare_truthiness(node, parents):
                continue
            violations.append(
                f"{module.__name__}:{node.lineno}: MAIN_REPO load in "
                f"{func or '<module>'} — route it through repository._repo()"
            )
    assert not violations, (
        "MAIN_REPO's empty-string sentinel is fail-open when consumed "
        "directly (git -C '' runs against the process CWD). Use _repo():\n"
        + "\n".join(violations)
    )


def test_repo_accessor_raises_on_unresolved_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "MAIN_REPO", "")
    with pytest.raises(repository.RepoNotConfigured):
        repository._repo()


def test_repo_accessor_returns_resolved_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/kirocrew")
    assert repository._repo() == "/somewhere/kirocrew"


def test_primary_checkout_resolution_preserves_the_host_text_decoder(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Moving the startup probe must not reinterpret non-ASCII checkout paths."""
    primary = tmp_path / "primary"
    seen: dict[str, object] = {}

    def _run(_argv, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=str(primary / ".git"))

    monkeypatch.setattr(runtime, "_trusted_bin", lambda _name: "git")
    monkeypatch.setattr(repository.subprocess, "run", _run)

    assert repository._resolve_primary_checkout(str(tmp_path / "linked")) == str(primary)
    assert seen["text"] is True
    assert seen["encoding"] == locale.getpreferredencoding(False)
