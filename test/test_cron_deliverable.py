"""extract_cron_deliverable: the <deliverable> marker contract for cron delivery."""

import pathlib

import pytest

from kiro_crew.cron_deliverable import extract_cron_deliverable


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The opt-in: only the marked block is delivered.
        ("Checking...\n<deliverable>\nDigest\n</deliverable>\nDone.", "Digest"),
        # Several blocks are kept, in order, a blank line apart.
        (
            "<deliverable>A</deliverable> narration <deliverable>B</deliverable>",
            "A\n\nB",
        ),
        # The tag is matched case-insensitively.
        ("x <Deliverable>Digest</DELIVERABLE> y", "Digest"),
        # An [OPTIONS:] line inside the block is kept with it.
        (
            "x <deliverable>Pick\n[OPTIONS: a | b]</deliverable>",
            "Pick\n[OPTIONS: a | b]",
        ),
    ],
)
def test_marked_blocks_are_extracted(text, expected):
    assert extract_cron_deliverable(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "No marker at all.",
        # Fail open: an unclosed or a stray closing tag is not a block.
        "Checking... <deliverable>Digest never closed",
        "Digest</deliverable>",
        # Fail open: an empty block must never turn a real run into an empty one.
        "Narration <deliverable>  \n </deliverable>",
    ],
)
def test_text_without_a_usable_block_is_returned_unchanged(text):
    assert extract_cron_deliverable(text) == text


def test_every_agent_cron_turn_cuts_before_the_placeholder_and_annotations():
    """Both agent-cron turn sites cut the answer before anything is appended to it.

    The framework's own notes (empty-reply placeholder, fallback and partial-block
    annotations) are added after the cut, so a marked answer can never drop them.
    """
    src = pathlib.Path(__file__).resolve().parents[1] / "src/kiro_crew/slack/gateway.py"
    lines = src.read_text(encoding="utf-8").splitlines()
    sites = [
        i for i, line in enumerate(lines) if "await _cron_stream_with_posttoken_resume(" in line
    ]
    assert len(sites) == 2, sites
    for start in sites:
        window = lines[start : start + 60]
        cut = next(
            i for i, line in enumerate(window) if "extract_cron_deliverable(result_text)" in line
        )
        for later in (
            "empty_reply_placeholder()",
            "_annotate_model_fallback(result_text",
        ):
            appended = next(i for i, line in enumerate(window) if later in line)
            assert cut < appended, (start, later)
