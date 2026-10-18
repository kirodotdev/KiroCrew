"""The MCP rewriter's facade and the ``rewriter_*`` owner modules it serves.

``kiro_crew.mcp_gateway.rewriter`` keeps the rewrite pass (``rewrite_agents``) and the
fingerprint that decides whether a pass may be skipped, and serves every other name from
the owner that defines it (``kiro_crew._facade_mirror``). This file pins:

* the owners log under the facade's logger, the name operators and tests filter on;
* ``_rewrite_inputs_fingerprint`` stays on the facade: a patch of the facade's
  ``__version__``, a dunder the mirror never forwards, reaches no owner;
* every name an owner imports from the rest of ``kiro_crew`` and the facade serves is
  named in the facade's ``TYPE_CHECKING`` block, where type checkers read it.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

from kiro_crew import _facade_mirror
from kiro_crew.mcp_gateway import rewriter

_OWNERS = tuple(importlib.import_module(name) for name in rewriter._REWRITER_OWNERS)


def test_the_owners_log_under_the_facade_logger() -> None:
    assert rewriter.logger.name == "kiro_crew.mcp_gateway.rewriter"
    logging_owners = [owner for owner in _OWNERS if "logger" in vars(owner)]
    assert logging_owners, "no owner logs: this check would pass vacuously"
    assert all(owner.logger is rewriter.logger for owner in logging_owners)


def test_the_fingerprint_reads_the_facade_version() -> None:
    """``__version__`` is a dunder, so a write through the facade stays on the facade."""
    assert rewriter._rewrite_inputs_fingerprint.__module__ == rewriter.__name__
    assert "__version__" in rewriter._rewrite_inputs_fingerprint.__code__.co_names


def _home_imports(tree: ast.Module, family: set[str]) -> dict[str, str]:
    """Name -> module, for each name *tree* imports at its top from kiro_crew outside *family*."""
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            if module.split(".")[0] == "kiro_crew" and module not in family:
                found.update({alias.asname or alias.name: module for alias in node.names})
    return found


def _type_checking_imports(tree: ast.Module) -> dict[str, str]:
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for sub in node.body:
                if isinstance(sub, ast.ImportFrom):
                    found.update(
                        {alias.asname or alias.name: sub.module or "" for alias in sub.names}
                    )
    return found


def test_every_project_name_the_facade_serves_is_in_its_type_checking_block() -> None:
    """A name served at run time is invisible to mypy unless the block names it."""
    family = {rewriter.__name__, *rewriter._REWRITER_OWNERS}
    imported: dict[str, str] = {}
    for owner in _OWNERS:
        source = Path(owner.__file__ or "").read_text(encoding="utf-8")
        imported.update(_home_imports(ast.parse(source), family))
    served = {name: imported[name] for name in rewriter._EXPORTS if name in imported}
    block = _type_checking_imports(
        ast.parse(Path(rewriter.__file__ or "").read_text(encoding="utf-8"))
    )
    missing = {name: home for name, home in served.items() if block.get(name) != home}
    assert served, "the facade serves no project name: this check would pass vacuously"
    assert missing == {}


def test_the_mirror_holds_the_rewriter_family() -> None:
    index = vars(rewriter)[_facade_mirror.INDEX_ATTRIBUTE]
    assert index.owners == rewriter._REWRITER_OWNERS
    assert index.excluded == ()
