"""A posted note is the sidebar's preview text, and nothing more.

A tab fed by ``POST /api/chat/slots/{slot}/note`` kept an old assistant reply as
its preview, because ``summarize_rows`` read preview text from user and
assistant rows only. The note now leads the preview, but it is not the agent
speaking: ``last_conv_role``, ``waiting_for_input`` and an open ``[OPTIONS:]``
ask still read the older conversation. Other ``inject`` rows (hook
continuations, recovery prompts, tool-block notices) stay out of the preview.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from gateway_appkit_endpoints_helpers import _make_state

from kiro_crew.dashboard.chat import api_chat_slot_note
from kiro_crew.dashboard.handlers import crew_workers
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

REPLY = "I sent it to the other tab."
NOTE = "Queue changed: 3 waiting, 1 in progress."


@asynccontextmanager
async def _client(state: DashboardState):
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/note", api_chat_slot_note)
    async with TestClient(TestServer(app)) as client:
        yield client


def _slot(tmp_path: Path, *rows: tuple[str, str]) -> tuple[DashboardState, _ChatSlot]:
    state = _make_state(tmp_path)
    slot = _ChatSlot("s1")
    state._slots["s1"] = slot
    for role, content in rows:
        slot.append(role, content, "msg")
    return state, slot


async def _post_note(state: DashboardState, content: str) -> None:
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/note", json={"content": content})
        assert resp.status == 200


def _row(slot: _ChatSlot) -> dict[str, Any]:
    return slot.to_dict()


@pytest.mark.asyncio
async def test_a_posted_note_becomes_the_preview(tmp_path: Path) -> None:
    state, slot = _slot(tmp_path, ("user", "hi"), ("assistant", REPLY))
    await _post_note(state, NOTE)
    assert slot.messages[-1]["role"] == "inject"
    row = _row(slot)
    assert row["last_message"] == NOTE
    assert row["waiting_for_input"] is True


@pytest.mark.asyncio
async def test_a_note_does_not_clear_an_open_options_ask(tmp_path: Path) -> None:
    state, slot = _slot(tmp_path, ("user", "pick"), ("assistant", "Which? [OPTIONS: Left | Right]"))
    before = _row(slot)
    await _post_note(state, NOTE)
    after = _row(slot)
    assert after["last_message"] == NOTE
    for key in ("has_options", "options", "options_ts", "prompt_preview", "waiting_for_input"):
        assert after[key] == before[key], key
    assert after["has_options"] is True


def test_a_note_restored_from_disk_still_counts(tmp_path: Path) -> None:
    """On disk the row keeps ``meta.noteSession`` but not its in-memory class."""
    _, slot = _slot(tmp_path, ("assistant", REPLY))
    slot.append("inject", NOTE, "", meta={"noteSession": "dashboard:s1"})
    assert _row(slot)["last_message"] == NOTE


@pytest.mark.parametrize(
    "meta",
    [
        {"injectKind": "hook_continuation"},
        {"injectKind": "recovery"},
        {"cronLabel": "nightly", "injectKind": "cron"},
        {},
    ],
)
def test_a_silent_inject_is_not_the_preview(tmp_path: Path, meta: dict[str, str]) -> None:
    _, slot = _slot(tmp_path, ("user", "hi"), ("assistant", REPLY))
    slot.append("inject", "[Hook continuation — automatic]\nkeep going", "", meta=meta)
    assert slot.messages[-1]["role"] == "inject"
    assert _row(slot)["last_message"] == REPLY


def test_a_user_reply_after_a_note_leads_the_preview_again(tmp_path: Path) -> None:
    _, slot = _slot(tmp_path, ("assistant", REPLY))
    slot.append("inject", NOTE, "reconcile-note", meta={"noteSession": "dashboard:s1"})
    slot.append("user", "thanks", "msg")
    row = _row(slot)
    assert row["last_message"] == "thanks"
    assert row["waiting_for_input"] is False


def test_a_worker_card_shows_its_last_reply_not_a_note() -> None:
    slot = SimpleNamespace(
        messages=[
            {"role": "assistant", "content": "**Menu drafted**\nnext"},
            {"role": "inject", "content": NOTE, "meta": {"noteSession": "dashboard:w"}},
        ]
    )
    assert crew_workers._worker_line({"last_message": NOTE}, "", slot) == "Menu drafted"


def test_a_worker_card_keeps_a_reply_newer_than_a_note() -> None:
    slot = SimpleNamespace(
        messages=[
            {"role": "inject", "content": NOTE, "meta": {"noteSession": "dashboard:w"}},
            {"role": "assistant", "content": "Menu drafted"},
        ]
    )
    assert crew_workers._worker_line({"last_message": "Menu drafted"}, "", slot) == "Menu drafted"


def test_a_worker_card_skips_a_reload_notice_before_a_note() -> None:
    """The reload notice is an assistant row, but it is not the worker's reply."""
    from kiro_crew.dashboard.system_notices import SESSION_RELOAD_KIND, SESSION_RELOAD_READY_TEXT

    slot = SimpleNamespace(
        messages=[
            {"role": "assistant", "content": "Menu drafted"},
            {
                "role": "assistant",
                "content": SESSION_RELOAD_READY_TEXT,
                "meta": {"kind": SESSION_RELOAD_KIND},
            },
            {"role": "inject", "content": NOTE, "meta": {"noteSession": "dashboard:w"}},
        ]
    )
    assert crew_workers._worker_line({"last_message": NOTE}, "", slot) == "Menu drafted"
