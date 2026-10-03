"""A consolidation prompt quotes history, so it must not re-inline the pictures.

A consolidation prompt is built from transcript rows, and a row that pasted a
screenshot names that file by absolute path. A builder that turned every
readable image path in a prompt into a real image block shipped one full-size
attachment per screenshot the session ever took (83 of them, 67 MB, on one
measured span). ``build_prompt_blocks`` emits image blocks only from the
channel's structured attachment list, which no consolidation prompt has; the
strip at :func:`kiro_crew.history_consolidation._fmt_message` keeps a dead path
out of the quoted prose, and these tests pin both.
"""

from __future__ import annotations

import base64

import pytest

from kiro_crew.acp.prompt_blocks import build_prompt_blocks
from kiro_crew.history_consolidation import _fmt_message
from kiro_crew.image_refs import STRIPPED_IMAGE_MARKER
from kiro_crew.prompt_attachments import image_attachments

# A 1x1 PNG: enough bytes to pass the raster sniff so the builder would inline it.
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


@pytest.fixture
def screenshot(tmp_path):
    path = tmp_path / "shot.png"
    path.write_bytes(_PNG_1X1)
    return path


def test_fmt_message_replaces_bare_image_path_with_marker(screenshot):
    row = {
        "ts": "2026-09-29T05:12:00",
        "role": "tool",
        "content": f"screenshot saved to {screenshot}",
    }
    rendered = _fmt_message(row)
    assert str(screenshot) not in rendered
    assert STRIPPED_IMAGE_MARKER in rendered
    assert rendered.startswith("[2026-09-29T05:12] TOOL: screenshot saved to ")


def test_fmt_message_replaces_markdown_image_with_marker(screenshot):
    row = {
        "ts": "2026-09-29T05:12:00",
        "role": "assistant",
        "content": f"here it is ![the login error]({screenshot}) as requested",
    }
    rendered = _fmt_message(row)
    assert str(screenshot) not in rendered
    assert "the login error" not in rendered
    assert STRIPPED_IMAGE_MARKER in rendered


def test_fmt_message_keeps_tools_suffix_and_plain_text():
    row = {
        "ts": "2026-09-29T05:12:00",
        "role": "user",
        "tools": ["execute_bash"],
        "content": "no pictures here, just words",
    }
    assert _fmt_message(row) == (
        "[2026-09-29T05:12] USER [tools: execute_bash]: no pictures here, just words"
    )


def test_rendered_row_yields_no_image_block_even_when_file_is_readable(screenshot):
    """The end-to-end property: the prompt builder finds nothing to inline."""
    row = {"ts": "2026-09-29T05:12:00", "role": "tool", "content": f"see {screenshot}"}
    blocks = build_prompt_blocks(_fmt_message(row), allow_image=True)
    assert [b["type"] for b in blocks] == ["text"]
    # Control: the builder itself never reads a path out of the text any more,
    # so the unstripped row carries no picture either -- what puts a picture
    # in a prompt is the channel's structured attachment list, which no
    # consolidation prompt has. The strip remains load-bearing for the prose:
    # the rendered row carries the marker, not a dead path.
    control = build_prompt_blocks(row["content"], allow_image=True)
    assert [b["type"] for b in control] == ["text"]
    attached = build_prompt_blocks(
        row["content"],
        attachments=image_attachments([str(screenshot)]),
        allow_image=True,
    )
    assert [b["type"] for b in attached] == ["text", "image"]
    assert str(screenshot) not in _fmt_message(row)
