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

    def test_no_config_dir_in_async_functions(self) -> None:
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
