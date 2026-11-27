"""A pooled backend's memory is measured where there is no ``/proc``.

On macOS the ``/proc`` subtree walk answers -1, and the metrics page showed
``rss_kb: -1`` for every backend.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.mcp_gateway import pool as pool_mod
from kiro_crew.platform_compat_metrics import SubtreeSample


def _no_proc(is_windows: bool):
    return (
        patch.object(platform_compat, "IS_LINUX", False),
        patch.object(platform_compat, "IS_WINDOWS", is_windows),
        patch.object(
            platform_compat, "proc_subtree_sample", return_value=SubtreeSample(-1, 0, None, None)
        ),
    )


@pytest.mark.parametrize(
    "is_windows, probe",
    [(True, "proc_rss_tree_mb_for_pid"), (False, "darwin_footprint_tree_mb")],
)
def test_without_proc_the_tree_footprint_is_reported_in_kib(is_windows: bool, probe: str) -> None:
    linux, windows, sample = _no_proc(is_windows)
    with linux, windows, sample, patch.object(platform_compat, probe, return_value=12.5) as tree:
        assert pool_mod.backend_rss_kb(4242) == 12800
    tree.assert_called_once_with(4242)


@pytest.mark.parametrize(
    "is_windows, probe",
    [(True, "proc_rss_tree_mb_for_pid"), (False, "darwin_footprint_tree_mb")],
)
def test_an_unreadable_tree_stays_unknown(is_windows: bool, probe: str) -> None:
    linux, windows, sample = _no_proc(is_windows)
    with linux, windows, sample, patch.object(platform_compat, probe, return_value=None):
        assert pool_mod.backend_rss_kb(4242) == -1
        assert pool_mod.backend_rss_kb(None) == -1
