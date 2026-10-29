"""``spawn_sub_agents`` re-sends its mark-collected POST when the outcome is unknown.

A missed mark leaves each inline-returned child's completion event enabled, so the
parent receives the same result twice. The mark is idempotent on the gateway, so a
transport failure (timeout, reset) is retried; a definite refusal is not.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kiro_crew import mcp_core
from kiro_crew.mcp_tools.spawn import _SubAgentsStep

_PATH = "/api/spawn/mark-collected"


def _settle(monkeypatch: pytest.MonkeyPatch, answers: list[dict]) -> list[tuple[dict, float]]:
    """Collect one finished child and commit; return each mark-collected POST."""
    calls: list[tuple[dict, float]] = []
    replies = iter(answers)

    def post(path: str, body: dict, *, timeout: float = 30, **_: object) -> dict:
        assert path == _PATH
        calls.append((body, timeout))
        return next(replies)

    monkeypatch.setattr(mcp_core, "_get", lambda path: {"done": True, "result": "ok"})
    monkeypatch.setattr(mcp_core, "_post", post)
    monkeypatch.setattr(mcp_core, "sel", MagicMock())
    step = _SubAgentsStep(
        sa_ids=["a1"],
        sa_deferred=set(),
        sa_errors=[],
        parent_session="dashboard:owner",
        max_wait=60,
        inline_result=lambda aid, text: text,
    )
    step.phase = "collect"
    text = step.step()
    assert text is not None and '"completed"' in text
    step.on_settled(text)
    return calls


def test_transport_failure_is_sent_again(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _settle(
        monkeypatch,
        [{"error": "timed out", "transport_error": True}, {"status": "ok", "marked": 1}],
    )
    body = {"ids": ["a1"], "parent_session": "dashboard:owner"}
    assert calls == [(body, 5), (body, 10)]


def test_success_is_sent_once(monkeypatch: pytest.MonkeyPatch) -> None:
    assert len(_settle(monkeypatch, [{"status": "ok", "marked": 1}])) == 1


def test_definite_refusal_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    assert len(_settle(monkeypatch, [{"error": "forbidden"}])) == 1


def test_retries_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    lost = {"error": "timed out", "transport_error": True}
    assert len(_settle(monkeypatch, [lost, lost, lost])) == 2
