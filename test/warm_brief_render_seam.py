"""Test seam for the warm welcome: count and pace each welcome where it is written.

The welcome makes no model call; the tests that once counted model calls as "a
brief ran" now count :func:`warm_task_brief._render` calls. *run_model* is the
test's old stub: it runs (and may block on an event) just before the gateway
renders, which is where a change could still land.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.dashboard import warm_task_brief


def route_render(monkeypatch: Any, run_model: Any) -> None:
    real = warm_task_brief._render

    async def render(state, slot, settled, live, language, checked_epoch):
        await run_model(None, "")
        return await real(state, slot, settled, live, language, checked_epoch)

    monkeypatch.setattr(warm_task_brief, "_render", render)
