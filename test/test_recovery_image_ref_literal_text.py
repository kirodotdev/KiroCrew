"""The unsupported-image recovery reads the same literal-text view as the builder."""

from __future__ import annotations

import os

import pytest

from kiro_crew.dashboard.chat_turn.recovery import _current_turn_carries_image_ref

# An absolute path on the running OS: ``/tmp/...`` is not one on Windows.
SHOT = os.path.abspath(os.path.join(os.sep, "tmp", "shot.png"))


@pytest.mark.parametrize(
    "message",
    [
        f"look at {SHOT} please",
        f"![x]({SHOT})",
    ],
)
def test_a_bare_image_path_is_proof_the_turn_carries_an_image(message: str) -> None:
    assert _current_turn_carries_image_ref(message) is True


@pytest.mark.parametrize(
    "message",
    [
        f"run `cat {SHOT}` for me",
        f"```\nls {SHOT}\n```",
        f"    {SHOT}",
        f"\\![x]({SHOT})",
    ],
)
def test_a_path_written_as_literal_text_is_not_proof(message: str) -> None:
    # build_prompt_blocks attaches nothing from these, so the recovery must not
    # treat them as the image the user just sent.
    assert _current_turn_carries_image_ref(message) is False


def test_an_empty_message_carries_no_image() -> None:
    assert _current_turn_carries_image_ref("") is False
