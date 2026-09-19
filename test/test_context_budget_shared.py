"""The shared budget module (``context_budget``) is the ONE home of the budget figures.

The two budget numbers every context consumer relies on live in
``context_budget`` so the compaction coordinator can import them at module
scope without pulling ``context`` (and its ~200 dependencies) onto every CLI
invocation. ``context`` derives its section caps from the same base and does
NOT carry a second spelling of either function: a duplicate would be a second
number waiting to drift. These tests pin the split: the base the light module
computes is the base ``context`` resolves its caps from, the constants are one
value, ``context`` has no wrapper copies, and the coordinator's import stays
light.
"""

from __future__ import annotations

import pytest

from kiro_crew import context as ctx
from kiro_crew import context_budget as cb

WINDOWS = [None, 0, -1, 200_000, 1_000_000, 2_000_000]


@pytest.mark.parametrize("window", WINDOWS)
def test_the_light_base_is_the_base_context_resolves_its_caps_from(window) -> None:
    # ``context`` resolves the full cap set; the light module derives the same
    # base directly. The base is what a rotation projection charges.
    assert cb.budget_base(window) == ctx._resolve_caps(window).base


@pytest.mark.parametrize("window", WINDOWS)
def test_the_startup_ceiling_is_the_full_ceiling_the_context_builder_enforces(window) -> None:
    # A rotation projection charges what build_session_context enforces with
    # lazy skills on (caps.max_context), not the bare base: the base alone
    # understates the successor by about a sixth of it.
    caps = ctx._resolve_caps(window)
    assert cb.startup_context_chars(window) == caps.max_context
    assert caps.max_context > caps.base


def test_context_derives_its_reference_caps_from_the_one_fraction_table() -> None:
    # One table: a fraction changed in context_budget changes the cap context
    # applies AND the ceiling the projection charges, in the same edit.
    for name, cap in {
        "compressed_history": ctx._COMPRESSED_HISTORY_CAP,
        "prefs": ctx._MEMORY_PREFS_CAP,
        "projects": ctx._MEMORY_PROJECTS_CAP,
        "memory_history": ctx._MEMORY_HISTORY_CAP,
        "semantic": ctx._SEMANTIC_MEMORY_CAP,
        "episodic": ctx._EPISODIC_MEMORY_CAP,
        "lessons": ctx._LESSONS_CAP,
        "skills": ctx._SKILLS_CAP,
        "steering": ctx._STEERING_CAP,
        "preamble_headroom": ctx._PREAMBLE_HEADROOM,
    }.items():
        assert cap == cb.reference_cap(cb.SECTION_FRACTIONS[name]), name
    assert ctx._MAX_CONTEXT_CHARS == cb.startup_context_chars(cb.REFERENCE_WINDOW_TOKENS)


@pytest.mark.parametrize("window", WINDOWS)
def test_replay_walk_options_use_the_shared_replay_budget(window) -> None:
    # The replay walks with the shared figure; there is no ``context``-local one.
    assert ctx.replay_walk_options(window)["budget_chars"] == cb.replay_budget_chars(window)


def test_context_carries_no_duplicate_spelling_of_the_budget_functions() -> None:
    # The deletion is the pin: a wrapper in ``context`` would be a second
    # implementation of the same figure with no base-tree importer to justify it.
    assert not hasattr(ctx, "replay_budget_chars")
    assert not hasattr(ctx, "startup_context_chars")


def test_the_reexported_constants_are_the_same_object_or_value() -> None:
    assert ctx._CONTEXT_BUDGET_BASE == cb.CONTEXT_BUDGET_BASE
    assert ctx._REFERENCE_WINDOW_TOKENS == cb.REFERENCE_WINDOW_TOKENS
    assert ctx._MIN_CONTEXT_BUDGET_BASE == cb.MIN_CONTEXT_BUDGET_BASE
    assert ctx._REPLAY_BUDGET_CHARS == cb.REPLAY_BUDGET_CHARS


def test_effective_window_falls_back_to_the_reference() -> None:
    for unusable in (None, 0, -5):
        assert cb.effective_window(unusable) == cb.REFERENCE_WINDOW_TOKENS
    assert cb.effective_window(200_000) == 200_000


def test_importing_the_coordinator_does_not_pull_context(tmp_path) -> None:
    """The reason the module exists: a CLI import path stays light.

    Run in a fresh interpreter so the assertion is about a cold import, not
    whatever this test session already loaded.
    """
    import subprocess
    import sys

    code = (
        "import sys; import kiro_crew.session_compaction; "
        "print('context' in sys.modules or 'kiro_crew.context' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert out.stdout.strip() == "False", out.stdout
