"""Load a checked-in skill script as a module without leaving bytecode behind.

The prepare-pr scripts live inside the checked-out source tree, so importing one
the ordinary way drops a ``__pycache__`` entry beside it that outlives the run --
a persistent mutation of the working copy, which the no-test-side-effects rule
forbids.

Five test modules each grew their own loader and none of them carried the guard.
Measured one file at a time from a clean tree, they left this in
``src/kiro_crew/builtin_skills/kirocrew-dev/prepare-pr/scripts/__pycache__/``:

===================================  ==========================================
test module                          residue
===================================  ==========================================
``test_push_guard``                  ``push_guard.pyc``
``test_prepare_pr_status``           ``pr_status.pyc``
``test_prepare_pr_profiles``         ``pr_status.pyc``, ``resolve_profile.pyc``
``test_prepare_pr_local_review``     ``local_review.pyc``, ``resolve_profile.pyc``
``test_prepare_pr_findings``         ``pr_findings.pyc``, ``pr_status.pyc``
===================================  ==========================================

One helper rather than six copies of a three-line guard, because a guard that has
to be remembered at each call site is one that will be missing at the seventh.
``test_prepare_pr_prove`` had it and the others did not, which is exactly how
that shape fails.
"""

from __future__ import annotations

import contextlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Iterator


@contextlib.contextmanager
def no_bytecode() -> Iterator[None]:
    """Disable bytecode writing for imports performed inside the block.

    For call sites that import by NAME off ``sys.path`` (or reload), where there
    is no spec to hand to :func:`load_skill_script`. Restores the previous value
    rather than clearing it, so nesting and a caller that deliberately enables
    writing both survive.
    """
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = previous


def load_skill_script(module_name: str, path: Path | str) -> ModuleType:
    """Import the script at ``path`` under ``module_name``, writing no bytecode.

    Not registered in ``sys.modules``: these scripts are loaded for unit-level
    assertions on their helpers, and several test modules load the SAME script
    under different names, so registering would let one test's copy answer
    another's import.
    """
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path} as {module_name}")
    module = importlib.util.module_from_spec(spec)
    with no_bytecode():
        spec.loader.exec_module(module)
    return module
