"""A pooled backend's memory is measured where there is no ``/proc``.

On macOS the ``/proc`` subtree walk answers -1, and the metrics page showed
``rss_kb: -1`` for every backend.
"""

from __future__ import annotations

from unittest.mock import patch

from kiro_crew import platform_compat
from kiro_crew.acp import runtime_process_tree
from kiro_crew.mcp_gateway import pool as pool_mod
from kiro_crew.platform_compat_metrics import SubtreeSample


def test_without_proc_the_tree_footprint_is_reported_in_kib() -> None:
    with (
        patch.object(platform_compat, "IS_LINUX", False),
        patch.object(
            platform_compat, "proc_subtree_sample", return_value=SubtreeSample(-1, 0, None, None)
        ),
        patch.object(runtime_process_tree, "_get_rss_tree_mb", return_value=12.5) as tree,
    ):
        assert pool_mod.backend_rss_kb(4242) == 12800
    tree.assert_called_once_with(4242)


def test_an_unreadable_tree_stays_unknown() -> None:
    with (
        patch.object(platform_compat, "IS_LINUX", False),
        patch.object(
            platform_compat, "proc_subtree_sample", return_value=SubtreeSample(-1, 0, None, None)
        ),
        patch.object(runtime_process_tree, "_get_rss_tree_mb", return_value=None),
    ):
        assert pool_mod.backend_rss_kb(4242) == -1
        assert pool_mod.backend_rss_kb(None) == -1
