"""A failed mid-chain full read must fail closed, not serve a wrong cursor.

`chain_mid_rotation` is true exactly when a chain member AFTER the first has
archive segments, so that archive is SANDWICHED inside the corpus rather than
occupying its first ``rotated_count`` rows. A prefix cursor of ``rotated_count``
therefore addresses the wrong span, the page it returns does not advance past the
sandwiched rows, ``has_more`` goes false, and those rows become unreachable with
no error the reader can see or retry.

The sibling non-mid-rotation branch uses the same value legitimately, because
there the rotation is on the first member and ``rotated_count`` IS the boundary.
That is why the guard has to be scoped to this branch rather than to the value.
"""

from __future__ import annotations

from pathlib import Path


def _handler_source() -> str:
    src = (
        Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard" / "chat_handlers.py"
    )
    return src.read_text(encoding="utf-8")


def _mid_rotation_except_block(body: str) -> str:
    """The `except` body belonging to the mid-rotation full read.

    Sliced between the read that can raise and the sibling `elif`, so a guard
    added anywhere else in the handler cannot satisfy this test.
    """
    start = body.index("if rotated_count > 0 and mid_rotation:")
    end = body.index("elif rotated_count > 0:", start)
    return body[start:end]


def test_mid_chain_read_failure_returns_a_retryable_error() -> None:
    block = _mid_rotation_except_block(_handler_source())
    assert "history_corpus_unreadable()" in block, (
        "a failed mid-chain full read must return a retryable error; a silent "
        "fallback strands the sandwiched archived rows"
    )


def test_mid_chain_failure_does_not_serve_the_prefix_cursor() -> None:
    block = _mid_rotation_except_block(_handler_source())
    assert "next_before = rotated_count" not in block, (
        "`rotated_count` is not the boundary of a SANDWICHED archive, so using it "
        "as a prefix cursor here points the reader at the wrong span"
    )


def test_the_first_member_branch_still_uses_the_prefix_cursor() -> None:
    """The fix must not spread to the branch where the cursor is correct."""
    body = _handler_source()
    sibling = body[body.index("elif rotated_count > 0:") :][:400]
    assert "next_before = rotated_count" in sibling
