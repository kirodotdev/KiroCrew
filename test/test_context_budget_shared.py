"""The shared budget module (``context_budget``) is the ONE home of the figures a rotation projects from.

The two budget numbers every context consumer relies on live in
``context_budget`` so the compaction coordinator can import them at module
scope without pulling ``context`` (and its ~200 dependencies) onto every CLI
invocation. ``context`` reads its base, its standing-rule allowance and its
replay scale from the same module and does NOT carry a second spelling of
either function: a duplicate would be a second number waiting to drift. These
tests pin the split: the pool the light module names is the pool ``context``
enforces, the startup figure is what a fresh session is admitted up to, the
constants are one value, ``context`` has no wrapper copies, and the
coordinator's import stays light.
"""

from __future__ import annotations

import pytest

from kiro_crew import context as ctx
from kiro_crew import context_budget as cb

WINDOWS = [None, 0, -1, 200_000, 1_000_000, 2_000_000]


@pytest.mark.parametrize("window", WINDOWS)
def test_the_startup_figure_is_what_the_context_builder_admits_a_fresh_session_up_to(
    window,
) -> None:
    # A rotation projection charges the three allowances build_session_context
    # admits: the shared discretionary pool (caps.max_context, one budget that
    # section limits do not enlarge), the standing-rule tier admitted beside
    # it and the pref.* rows admitted beside it. All are window-independent,
    # so the figure is too.
    caps = ctx._resolve_caps(window)
    assert (
        cb.startup_context_chars(window)
        == caps.max_context + caps.lessons_startup + caps.prefs_startup
    )
    assert caps.max_context == caps.base == cb.CONTEXT_BUDGET_BASE


@pytest.mark.parametrize("window", WINDOWS)
def test_replay_walk_options_use_the_shared_replay_budget(window) -> None:
    # The replay walks with the shared figure; there is no ``context``-local one.
    assert ctx.replay_walk_options(window)["budget_chars"] == cb.replay_budget_chars(window)


def test_the_replay_scale_is_the_window_over_the_reference_clamped() -> None:
    # 20% is the 200K tier, the smallest real window; larger windows cannot
    # enlarge the tail beyond the reference allowance; an unknown window is the
    # reference, never the floor.
    assert cb.replay_scale(200_000) == pytest.approx(0.2)
    assert cb.replay_scale(100_000) == pytest.approx(0.2)
    assert cb.replay_scale(500_000) == pytest.approx(0.5)
    assert cb.replay_scale(1_000_000) == 1.0
    assert cb.replay_scale(2_000_000) == 1.0
    assert cb.replay_scale(None) == 1.0
    assert cb.replay_budget_chars(200_000) == round(cb.REPLAY_BUDGET_CHARS * 0.2)
    assert cb.replay_budget_chars(None) == cb.REPLAY_BUDGET_CHARS


def test_context_carries_no_duplicate_spelling_of_the_budget_functions() -> None:
    # The deletion is the pin: a wrapper in ``context`` would be a second
    # implementation of the same figure with no base-tree importer to justify it.
    assert not hasattr(ctx, "replay_budget_chars")
    assert not hasattr(ctx, "startup_context_chars")
    assert not hasattr(ctx, "replay_scale")


def test_the_reexported_constants_are_the_same_object_or_value() -> None:
    assert ctx._CONTEXT_BUDGET_BASE == cb.CONTEXT_BUDGET_BASE
    assert ctx._LESSONS_STARTUP_CAP == cb.LESSONS_STARTUP_CAP
    assert ctx._PREFS_STARTUP_CAP == cb.PREFS_STARTUP_CAP
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
