"""A cold-start history replay starts after the newest ``/clear`` row.

``/clear`` keeps the transcript but clears what the agent remembers, so
``build_session_replay`` must not hand the turns before it back to the agent.
"""

from __future__ import annotations

from kiro_crew.context import build_session_replay


class _Log:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def read_messages(self, key: str) -> list[dict]:
        return list(self._rows)

    def read_messages_chained(self, key: str) -> list[dict]:
        return list(self._rows)


def _row(role: str, content: str) -> dict:
    return {"role": role, "content": content}


def test_replay_holds_only_the_rows_after_the_newest_clear() -> None:
    log = _Log(
        [
            _row("user", "q0-before"),
            _row("assistant", "a0-before"),
            _row("user", "/clear"),
            _row("assistant", "Conversation cleared."),
            _row("user", "q1-after"),
            _row("assistant", "a1-after"),
        ]
    )
    text = build_session_replay(log, "dashboard:x")  # type: ignore[arg-type]
    assert text is not None
    assert "q1-after" in text and "a1-after" in text
    assert "q0-before" not in text and "a0-before" not in text


def test_replay_without_a_clear_keeps_every_row() -> None:
    log = _Log([_row("user", "q0-kept"), _row("assistant", "a0-kept")])
    text = build_session_replay(log, "dashboard:x")  # type: ignore[arg-type]
    assert text is not None
    assert "q0-kept" in text and "a0-kept" in text
