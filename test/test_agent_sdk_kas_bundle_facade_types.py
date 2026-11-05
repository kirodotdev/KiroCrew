"""The ``agent_sdk.kas_bundle`` facade types every lazy name it re-exports.

The facade serves the installer's names through a :pep:`562` ``__getattr__``.
mypy reads such a name through the hook's return type, and in an import cycle it
can read it before that ``-> Any`` is analysed: a call then fails as
``Any? not callable``, but only on runs that reach the module in that order
(``mypy --no-incremental src/kiro_crew/dashboard/change_cards_settings.py`` did,
at ``backend_install.py``). A ``TYPE_CHECKING`` import of each name gives mypy a
real type and leaves the runtime import lazy. These tests keep that import equal
to ``_OWNED`` and prove it never runs.
"""

from __future__ import annotations

import ast
import inspect

from kiro_crew.agent_sdk import kas_bundle as facade

_OWNER = "kiro_crew.acp.kas_bundle"


def _type_checking_imports(tree: ast.Module) -> set[str]:
    """Names imported from the owner inside a module-level ``if TYPE_CHECKING:``."""
    names: set[str] = set()
    for node in tree.body:
        if not (isinstance(node, ast.If) and isinstance(node.test, ast.Name)):
            continue
        if node.test.id != "TYPE_CHECKING":
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.ImportFrom) and stmt.module == _OWNER:
                names.update(alias.asname or alias.name for alias in stmt.names)
    return names


def test_kas_bundle_facade_types_every_owned_name() -> None:
    tree = ast.parse(inspect.getsource(facade))
    assert _type_checking_imports(tree) == set(facade._OWNED)


def test_the_typed_names_stay_lazy_at_runtime() -> None:
    # Read one first: the hook must still serve it without binding it here, or a
    # purge-and-reimport of the owner would leave this module holding a stale copy.
    assert facade.installed_bundle is not None
    held = set(vars(facade)) & set(facade._OWNED)
    assert held == set()
