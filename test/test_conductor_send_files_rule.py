"""The conductor rule ``send_files``: a file the person gave the conductor reaches
a worker as its absolute path in the message, never as a description.

The rule needs both halves: the conductor's own prompt keeps a pasted picture's
upload path visible next to its marker, and the worker's prompt builder turns that
absolute image path in a peer's message into an image block.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

from chat_test_helpers import _make_state
from PIL import Image
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _peer_target,
    _slot,
)

from kiro_crew import hooks as hooks_mod
from kiro_crew.acp.prompt_blocks import build_prompt_blocks
from kiro_crew.config.paths import data_home
from kiro_crew.dashboard import session_control as sc

#: A ceiling for a turn the send starts at once; it only bounds a lost signal.
_TURN_STARTED_CEILING_SECS = 30.0


def _png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 10, 10)).save(buf, format="PNG")
    path.write_bytes(buf.getvalue())
    return path


def _images(blocks: list[dict]) -> list[dict]:
    return [b for b in blocks if b.get("type") == "image"]


def test_a_pasted_upload_keeps_its_path_and_forwarding_it_inlines_on_the_worker(
    tmp_path, monkeypatch
):
    shot = _png(data_home() / "uploads" / "abc_screenshot.png")
    # The composer's line for a pasted picture, as the conductor's turn receives it.
    conductor_blocks = build_prompt_blocks(f"![image]({shot})\n\nfix what this shows")
    conductor_text = conductor_blocks[0]["text"]
    assert len(_images(conductor_blocks)) == 1
    # Matched as one literal, so a data home whose path holds spaces still matches.
    forwarded = str(shot.resolve())
    assert f"[image: abc_screenshot.png] {forwarded})" in conductor_text, conductor_text

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _peer_target(state, "chat-2", caller)
    ran: dict[str, str] = {}
    started = asyncio.Event()

    async def _fake_run_chat(_state, slot, prompt):
        ran[slot.key] = prompt
        started.set()

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat)

    async def _drive():
        await sc.send_to_target(
            state,
            caller_session_key=_key(caller),
            target="chat-2",
            message=f"Fix what this screenshot shows. Open it: {forwarded}",
        )
        await asyncio.wait_for(started.wait(), timeout=_TURN_STARTED_CEILING_SECS)

    asyncio.run(_drive())

    worker_images = _images(build_prompt_blocks(ran["chat-2"]))
    assert len(worker_images) == 1 and worker_images[0]["mimeType"] == "image/png"


def test_an_uploads_folder_the_path_screen_refuses_adds_no_path(monkeypatch):
    """A refused uploads folder (a UNC share on Windows) is never canonicalized."""
    shot = _png(data_home() / "uploads" / "abc_screenshot.png")
    uploads = str(data_home() / "uploads")
    real_screen = hooks_mod.validate_file_path

    def _screen(raw, **kw):
        return None if raw == uploads else real_screen(raw, **kw)

    monkeypatch.setattr("kiro_crew.hooks.validate_file_path", _screen)

    text = build_prompt_blocks(f"look {shot}")[0]["text"]

    assert text == "look [image: abc_screenshot.png]"


def test_a_picture_outside_the_uploads_folder_keeps_the_bare_marker(tmp_path):
    shot = _png(tmp_path / "elsewhere" / "shot.png")

    text = build_prompt_blocks(f"look {shot}")[0]["text"]

    assert text == "look [image: shot.png]"
