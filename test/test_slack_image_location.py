"""A Slack image's saved location survives the prompt rewrite.

``build_prompt_blocks`` replaces an inlined image path with ``[image: <name>]``,
so without a separate note the agent sees the picture but cannot open the file.
These tests run the Slack ingest output through the real prompt builder, the
same way ``slack/events.py`` assembles the turn text.
"""

from __future__ import annotations

import os
import struct
import zlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.prompt_blocks import build_prompt_blocks
from kiro_crew.slack.files import process_slack_files


def _png() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    body = chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
    return b"\x89PNG\r\n\x1a\n" + body + chunk(b"IEND", b"")


def _orch(content: bytes) -> MagicMock:
    orch = MagicMock()
    orch.slack = AsyncMock()

    async def _download(url: str, dest: str) -> None:
        with open(dest, "wb") as fh:
            fh.write(content)

    orch.slack.download_file = AsyncMock(side_effect=_download)
    return orch


def _slack_turn_text(paths: list[str], blocks: list[str]) -> str:
    # Mirrors slack/events.py: the paths line, then the text blocks.
    return "edit this picture\n" + "\n".join(paths) + "\n\n" + "\n\n".join(blocks)


@pytest.mark.asyncio
async def test_slack_image_turn_tells_the_agent_where_the_file_is() -> None:
    files = [
        {
            "mimetype": "image/png",
            "url_private_download": "https://files.slack.com/a.png",
            "filetype": "png",
            "name": "photo.png",
            "size": 100,
        }
    ]
    paths, blocks = await process_slack_files(_orch(_png()), files)
    try:
        assert len(paths) == 1
        saved = paths[0]
        prompt = build_prompt_blocks(_slack_turn_text(paths, blocks))
        text = prompt[0]["text"]
        folder, name = os.path.split(saved)

        # The picture is still inlined, and its whole path is still rewritten.
        assert [b["type"] for b in prompt[1:]] == ["image"]
        assert f"[image: {name}]" in text
        assert saved not in text
        # But the folder and the name both reach the model, and they join to
        # the real file -- the thing the agent's file tools need.
        assert f"saved in the folder {folder}{os.sep} " in text
        assert os.path.join(folder, name) == saved
        assert os.path.isfile(saved)
    finally:
        for p in paths:
            os.unlink(p)


@pytest.mark.asyncio
async def test_slack_opaque_file_gets_no_location_note() -> None:
    files = [
        {
            "mimetype": "application/zip",
            "url_private_download": "https://files.slack.com/a.zip",
            "filetype": "zip",
            "name": "a.zip",
            "size": 4,
        }
    ]
    paths, blocks = await process_slack_files(_orch(b"PK\x03\x04"), files)
    try:
        assert len(paths) == 1
        # The opaque path already stays in the text; no extra note is added.
        assert not any("saved in the folder" in b for b in blocks)
    finally:
        for p in paths:
            os.unlink(p)


def test_dashboard_turn_prompt_is_unchanged(tmp_path) -> None:
    # A non-Slack turn carries only the path; the builder output is the same
    # marker as before, with no location note.
    img = tmp_path / "upload.png"
    img.write_bytes(_png())
    prompt = build_prompt_blocks(f"look at this\n{img}")
    assert prompt[0]["text"] == "look at this\n[image: upload.png]"
    assert [b["type"] for b in prompt[1:]] == ["image"]
