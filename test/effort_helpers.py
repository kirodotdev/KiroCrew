"""Helpers shared by the ``test_effort*.py`` modules.

The unparseable-overlay parametrization, the cli.json effort reader that other
test modules import from ``test_effort`` as well, and an observer for a Default
start waiting on its work directory's overlay fence.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

_UNPARSEABLE_OVERLAY_CONTENTS = pytest.mark.parametrize(
    "contents",
    [
        b'{"chat.modelDefaults": {"m": {"output_config": {"effort": "max"}}}, "n": '
        + b"1" * 5000
        + b"}",
        b"[" * 200_000 + b"]" * 200_000,
    ],
    ids=["oversized-int-literal", "deeply-nested"],
)


def _read_cli_overlay(work_dir: Path) -> dict[str, str]:
    """Return effort entries from a workspace cli.json for test assertions."""
    cli_json = work_dir / ".kiro" / "settings" / "cli.json"
    if not cli_json.exists():
        return {}
    try:
        data = json.loads(cli_json.read_text(encoding="utf-8"))
    except OSError:
        return {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    model_defaults = data.get("chat.modelDefaults")
    if not isinstance(model_defaults, dict):
        return {}
    result: dict[str, str] = {}
    for model, config in model_defaults.items():
        if not isinstance(config, dict):
            continue
        for key in ("output_config", "reasoning"):
            effort_config = config.get(key)
            if not isinstance(effort_config, dict):
                continue
            effort = effort_config.get("effort")
            if isinstance(effort, str) and effort:
                result[model] = effort
                break
    return result


def _observe_directory_wait(monkeypatch) -> asyncio.Event:
    """Set an event when a Default start begins waiting for its directory's writes.

    Install it after any start that must not trip it has passed that wait: every
    Default start calls it, including one that finds the directory free.
    """
    from kiro_crew.providers import acp as acp_provider

    waiting = asyncio.Event()
    original = acp_provider._WorkspaceEffortOverlayFence.wait_until_writes_clear

    async def observed(fence, timeout):
        waiting.set()
        return await original(fence, timeout)

    monkeypatch.setattr(
        acp_provider._WorkspaceEffortOverlayFence, "wait_until_writes_clear", observed
    )
    return waiting
