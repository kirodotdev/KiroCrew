"""The crew-work-migration mutation sweep is kept honest by CI, not by memory.

`.kiro/specs/crew-work-migration/tools/mutation_sweep.py` proves the feature's
tests have teeth: it edits one line of source, runs the single test that should
object, and restores the file. Each mutation finds its line by an exact `old`
string, and that is the fragile part -- a refactor that reformats or rewords the
anchored line leaves the anchor matching nothing. The sweep already reports such
a mutation as `SKIPPED (anchor not found)` and exits non-zero, so it is not
silent to a person who RUNS it; what it lacked was anyone running it. A mutation
whose anchor has rotted is a test that is no longer guarded while the sweep's
last recorded output still says 28/28.

This module is that consumer. It does not mutate anything and runs no nested
test session -- it reads the sweep's table and asserts each entry still points at
something real, which costs milliseconds and rides the ordinary backend suite.
The full sweep stays a manual deep check; this is the tripwire that says when it
needs re-anchoring.

Asserting the anchor appears EXACTLY once, not merely at least once: the sweep
substitutes the first occurrence (`replace(..., 1)`), so a second copy makes the
mutation ambiguous -- it would silently exercise whichever one happens to come
first in the file.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SWEEP = REPO / ".kiro/specs/crew-work-migration/tools/mutation_sweep.py"


def _load_sweep():
    """Import the sweep by path.

    It lives under `.kiro/specs/` rather than in a package, so there is no
    importable name for it. The module must be placed in `sys.modules` BEFORE
    it executes: `@dataclasses.dataclass` resolves its own module out of
    `sys.modules` to decide whether an annotation is `KW_ONLY`, and raises
    `AttributeError` on a module that is not registered there yet.
    """
    spec = importlib.util.spec_from_file_location("crew_work_migration_sweep", SWEEP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mutations() -> list:
    assert SWEEP.exists(), f"the mutation sweep is missing: {SWEEP.relative_to(REPO)}"
    table = _load_sweep().MUTATIONS
    assert table, "the sweep declares no mutations, so it proves nothing"
    return table


def test_every_mutation_anchor_still_resolves_exactly_once(mutations) -> None:
    """A rotted anchor silently disables one mutation. Name every one that rotted."""
    rotted: list[str] = []
    for m in mutations:
        target = REPO / m.path
        if not target.exists():
            rotted.append(f"{m.path}: file is gone ({m.what})")
            continue
        found = target.read_text(encoding="utf-8").count(m.old)
        if found != 1:
            rotted.append(f"{m.path}: anchor found {found}x, want 1 ({m.what})\n    {m.old!r}")
    assert not rotted, "re-anchor these mutations:\n  " + "\n  ".join(rotted)


def test_every_mutation_actually_changes_the_line(mutations) -> None:
    """`new == old` is a no-op that always reports `caught` while testing nothing."""
    noop = [f"{m.path}: {m.what}" for m in mutations if m.new == m.old]
    assert not noop, "these mutations change nothing:\n  " + "\n  ".join(noop)


def test_every_mutation_names_a_test_file_that_exists(mutations) -> None:
    """A mutation pointed at a deleted test is a guard with nothing behind it."""
    missing: list[str] = []
    for m in mutations:
        # pytest targets are `path::test_name`; vitest targets are website-relative.
        rel = m.test.split("::")[0]
        target = REPO / rel if m.runner == "pytest" else REPO / "website" / rel
        if not target.exists():
            missing.append(f"{m.test} ({m.what})")
    assert not missing, "these mutations name a test file that is gone:\n  " + "\n  ".join(missing)


def test_every_mutation_is_judged_by_the_runner_that_can_read_its_test(mutations) -> None:
    """A mismatched runner turns a mutation's verdict into a false `caught`.

    The sweep calls a mutation `caught` whenever its command exits non-zero, so a
    frontend mutation left on the default pytest runner is recorded as caught
    because pytest cannot COLLECT a `.tsx` file -- not because the test objected.
    The verdict then says the test has teeth while the test never ran. Pairing the
    runner with the test's own suffix is what keeps a `caught` meaningful.
    """
    mismatched: list[str] = []
    for m in mutations:
        rel = m.test.split("::")[0]
        is_frontend_test = rel.endswith((".tsx", ".ts"))
        want = "vitest" if is_frontend_test else "pytest"
        if m.runner != want:
            mismatched.append(f"{m.test}: runner={m.runner!r}, want {want!r} ({m.what})")
    assert not mismatched, "these mutations cannot be judged by their runner:\n  " + "\n  ".join(
        mismatched
    )
