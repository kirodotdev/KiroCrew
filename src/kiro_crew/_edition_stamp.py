"""Write the build's edition into a BUILT copy of ``kiro_crew/edition.py``.

Build-time only: ``setup.py``'s ``build_py`` loads this file by path (so it must
import nothing from ``kiro_crew`` and nothing outside the standard library) and
stamps the copy under ``build_lib``. Nothing at run time imports it.
"""

from __future__ import annotations

import ast
import os
import re
from typing import Mapping

ENV = "KIROCREW_BUILD_EDITION"
DEFAULT = "oss"
_EDITION_LINE = re.compile(r'^EDITION = "[a-z]+"$', re.MULTILINE)


def requested_edition(environ: Mapping[str, str]) -> str:
    """The edition this build asks for; unset or blank means ``oss``."""
    return environ.get(ENV, "").strip() or DEFAULT


def declared_editions(source: str) -> frozenset[str]:
    """Read ``EDITIONS = frozenset({...})`` from edition.py source without importing it."""
    for node in ast.parse(source).body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "EDITIONS" for t in node.targets)
            and isinstance(node.value, ast.Call)
            and node.value.args
        ):
            return frozenset(ast.literal_eval(node.value.args[0]))
    raise ValueError("edition.py declares no EDITIONS set")


def _checked_source(path: str, edition: str) -> str:
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    allowed = declared_editions(source)
    if edition not in allowed:
        raise ValueError(f"unknown {ENV} '{edition}' (want one of: {', '.join(sorted(allowed))})")
    return source


def check_edition(path: str, edition: str) -> None:
    """Raise ValueError unless ``edition`` is one the edition.py at ``path`` declares."""
    _checked_source(path, edition)


def stamp_file(path: str, edition: str) -> None:
    """Rewrite the ``EDITION = ...`` line of the edition.py at ``path``.

    Raises ValueError for an edition outside the file's own ``EDITIONS``. The file
    is replaced, not written in place, so a built copy hard-linked to the source
    can never carry the stamp back into the checkout.
    """
    source = _checked_source(path, edition)
    stamped, count = _EDITION_LINE.subn(f'EDITION = "{edition}"', source)
    if count != 1:
        raise ValueError(f"expected one EDITION line in {path}, found {count}")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(stamped)
    os.replace(tmp, path)
