"""The Slack ``status`` keyword replies with the stats summary.

``handle_message`` reads the module-level ``current_context`` in its status
branch. A function-local import of the same name anywhere in that body makes
the name local for the whole function, so the status branch raises
``UnboundLocalError`` before the later import runs.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from test_slack_handler import FakeSessionManager

from conftest import MockSlackClient
from kiro_crew.slack import handler as h
from kiro_crew.stats import Stats


@pytest.mark.asyncio
async def test_status_keyword_posts_stats_summary():
    slack = MockSlackClient()

    await h.handle_message(slack, FakeSessionManager(), "C1", "status", None, "msg1", "U1")

    posts = [a[1]["text"] for a in slack.actions if a[0] == "post"]
    summary = Stats().summary()
    assert any(p.startswith(summary) for p in posts), posts


def test_handler_functions_do_not_import_current_context_locally():
    tree = ast.parse(Path(h.__file__).read_text(encoding="utf-8"))
    offenders = [
        f"{fn.name}:{node.lineno}"
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(fn)
        if isinstance(node, ast.ImportFrom)
        and any(alias.name == "current_context" for alias in node.names)
    ]
    assert offenders == [], offenders
