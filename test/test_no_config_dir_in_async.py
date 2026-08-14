"""Regression test for #1057: no config_dir() inside async functions.

config_dir() performs start-of-process maintenance (mkdir, breadcrumb refresh,
ungated-archive sweep with shutil.rmtree) on every call. Calling it from an
async function runs that maintenance on the event loop. The fix is to use
data_home() which returns the cached path without maintenance.

This guard enforces that no async function in the listed files calls
config_dir() directly, so the fix cannot silently regress.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

# Files that historically had config_dir() inside async functions (issue #1057).
_ASYNC_CHECKED_FILES = [
    "dashboard/handlers/files.py",
    "dashboard/chat_runner.py",
    "dashboard/handlers/knowledge.py",
    "dashboard/handlers/messaging.py",
    "dashboard/server.py",
    "slack/gateway.py",
    "slack/interactions.py",
    "weixin/gateway.py",
    "cli_chat.py",
]


class TestNoConfigDirInAsync:
    """config_dir() must not be called inside async functions (#1057)."""

    def test_update_layout_channel_helpers_never_maintain(self) -> None:
        """The channel read/write must use ``data_home()``, not ``config_dir()``.

        Neither helper is an ``async def``, so the AST walk above cannot see them
        — but both are reached FROM async handlers: ``release_channel()`` from the
        update check and ``set_release_channel()`` from ``POST
        /api/update/channel``. ``config_dir()`` is resolve-and-maintain (breadcrumb
        refresh + a leftover-archive sweep that can ``shutil.rmtree``), so using it
        there would run a destructive sweep on the event loop — #1057 through an
        indirect call chain, which is exactly how it would come back.
        """
        tree = ast.parse((SRC / "platform" / "update_layout.py").read_text(encoding="utf-8"))
        # AST, not a substring scan: the module's docstrings NAME config_dir to
        # explain why it is the wrong helper here, and a text match would flag
        # exactly the comment that documents the fix.
        called = {
            getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
        }
        imported = {
            alias.name
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom)
            for alias in n.names
        }
        assert "config_dir" not in called and "config_dir" not in imported, (
            "update_layout.py must resolve the data home with data_home(); "
            "config_dir() re-runs start-of-process maintenance on every call and "
            "these helpers are reached from async request handlers (#1057)"
        )
        assert "data_home" in called, "the channel helpers must resolve a path at all"

        """Every async call site must use data_home() instead of config_dir()."""
        offenders: list[str] = []
        for fname in _ASYNC_CHECKED_FILES:
            fp = SRC / fname
            if not fp.exists():
                continue
            tree = ast.parse(fp.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.AsyncFunctionDef):
                    continue
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        name = getattr(sub.func, "id", None) or getattr(
                            sub.func, "attr", None
                        )
                        if name == "config_dir":
                            offenders.append(
                                f"{fname}:{sub.lineno} in async {node.name}()"
                            )
        assert not offenders, (
            "config_dir() called inside async function (issue #1057). "
            "Use data_home() instead:\n  " + "\n  ".join(offenders)
        )
