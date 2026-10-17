"""Credential-shaped pending context is refused at admission.

Pending context reaches the model in-band, as prompt text, so it is not a
confidential channel. ``/context`` refuses a payload the credential redactor
would rewrite with a 400 ``context_secret_shaped`` and leaves the queue
unchanged; ``/note`` writes its redacted visible line and seats no context half.
Ordinary context is accepted and drains byte-for-byte.
"""

import base64
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_handlers import (
    _enqueue_pending_context,
    api_chat_slot_context,
    api_chat_slot_note,
)
from kiro_crew.dashboard.chat_turn.turn_context import drain_pending_context

# Fixture values are assembled at runtime so no literal credential shape sits
# in the source file.
_AWS_KEY_ID = "AKIA" + "QZSHAPETEST01234"
_GITHUB_TOKEN = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"
_PRIVATE_KEY = (
    "-----BEGIN "
    + "RSA PRIVATE KEY-----\n"
    + "MIIB"
    + "x9Kq" * 15
    + "\n"
    + "-----END "
    + "RSA PRIVATE KEY-----"
)
_URL_WITH_PASSWORD = "https://deploy:" + "s3cr3tpassw0rd@git.example.com/org/repo.git"

SECRET_SHAPED = [
    pytest.param(f"deploy with key {_AWS_KEY_ID} today", id="aws-key-id"),
    pytest.param(f"token: {_GITHUB_TOKEN}", id="github-token"),
    pytest.param(f"here is the key\n{_PRIVATE_KEY}\n", id="private-key"),
    pytest.param(f"clone {_URL_WITH_PASSWORD}", id="url-userinfo"),
]

ORDINARY = [
    pytest.param(
        "def total(items):\n    return sum(i.price for i in items)\n",
        id="code",
    ),
    pytest.param(
        "Thread backfill: <https://github.com/org/repo/pull/12|PR 12> landed, "
        "docs at https://example.com/docs?page=2#setup",
        id="slack-text-with-urls",
    ),
    pytest.param(
        'Companion chat for artifact `release-notes` ("Release notes", kind=html, '
        "v3, source_path=none, 2 open comments).\n"
        "Use artifact_get / artifact_update / artifact_get_comments with this slug.",
        id="artifact-companion",
    ),
    pytest.param(
        "Commit 3e92a4228945c4c6edfbb1160dfda48ac9b84a33 is the base.",
        id="git-sha",
    ),
    pytest.param(
        "payload: "
        + base64.b64encode(b"The quick brown fox jumps over the lazy dog. " * 4).decode(),
        id="base64-prose-blob",
    ),
    pytest.param(
        "state: "
        + base64.urlsafe_b64encode(b'{"view": "board", "columns": ["todo", "done"]}').decode(),
        id="base64-json-single-segment",
    ),
    pytest.param(
        "digest sha256:" + hashlib.sha256(b"artifact").hexdigest(),
        id="sha256-digest",
    ),
]


def _seeded(state, name: str):
    slot = state.get_or_create_slot(name)
    slot._titled = True
    slot.append(role="user", content="a real message", cls="msg msg-u")
    slot.drain()
    return slot


@asynccontextmanager
async def _client(state):
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/context", api_chat_slot_context)
    app.router.add_post("/api/chat/slots/{slot}/note", api_chat_slot_note)
    async with TestClient(TestServer(app)) as c:
        yield c


def _inject_rows(slot) -> list:
    return [m for m in slot.messages if m.get("role") == "inject"]


class TestContextEndpoint:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("content", SECRET_SHAPED)
    async def test_secret_shaped_content_is_refused(self, tmp_path: Path, monkeypatch, content):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded(state, "s-ctx-secret")
        before = list(slot._pending_context)

        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s-ctx-secret/context",
                json={"content": content, "source": "app"},
            )
            assert resp.status == 400
            raw = await resp.text()

        body = json.loads(raw)
        assert body["code"] == "context_secret_shaped"
        assert content not in raw
        for value in (_AWS_KEY_ID, _GITHUB_TOKEN, "s3cr3tpassw0rd", "x9Kqx9Kq"):
            assert value not in raw, "the refusal must not echo the payload"
        assert slot._pending_context == before, "a refusal leaves the queue unchanged"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("content", ORDINARY)
    async def test_ordinary_content_is_accepted_and_drains_verbatim(
        self, tmp_path: Path, monkeypatch, content
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded(state, "s-ctx-ok")

        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s-ctx-ok/context",
                json={"content": content, "source": "app"},
            )
            assert resp.status == 200

        assert [e["content"] for e in slot._pending_context] == [content]
        assert content in drain_pending_context(slot)


class TestEnqueueChokepoint:
    """A direct caller of the enqueue helper gets the same refusal as HTTP."""

    @pytest.mark.parametrize("content", SECRET_SHAPED)
    def test_direct_enqueue_refuses(self, tmp_path: Path, monkeypatch, content):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded(state, "s-direct")

        resp = _enqueue_pending_context(slot, content, "app", True, None)

        assert resp is not None
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "context_secret_shaped"
        assert slot._pending_context == []

    def test_direct_enqueue_accepts_ordinary_content(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded(state, "s-direct-ok")

        assert _enqueue_pending_context(slot, "plain context", "app", True, None) is None
        assert [e["content"] for e in slot._pending_context] == ["plain context"]


class TestNoteEndpoint:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("content", SECRET_SHAPED)
    async def test_secret_shaped_note_seats_no_context_half(
        self, tmp_path: Path, monkeypatch, content
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded(state, "s-note-secret")

        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s-note-secret/note", json={"content": content}
            )
            assert resp.status == 200
            body = await resp.json()

        assert body["contextSkipped"] is True
        assert body["appended"] is True, "the redacted visible line is still written"
        assert slot._pending_context == [], "no context half reaches the model"
        rows = _inject_rows(slot)
        assert len(rows) == 1
        for value in (_AWS_KEY_ID, _GITHUB_TOKEN, "s3cr3tpassw0rd", "x9Kqx9Kq"):
            assert value not in rows[0]["content"]

    @pytest.mark.asyncio
    async def test_held_note_carries_no_context_half(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded(state, "s-note-running")
        monkeypatch.setattr(type(slot), "running", property(lambda self: True))

        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s-note-running/note",
                json={"content": f"token {_GITHUB_TOKEN}"},
            )
            assert resp.status == 200
            body = await resp.json()

        assert body["visibleDeferred"] is True, "precondition: the note was held"
        assert body["contextSkipped"] is True
        held = slot._deferred_notes[-1]
        assert held["context"] is None, "the flush must have no context half to seat"
        assert _GITHUB_TOKEN not in held["content"]
        assert slot._pending_context == []

    @pytest.mark.asyncio
    async def test_ordinary_note_is_still_accepted(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded(state, "s-note-ok")

        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s-note-ok/note",
                json={"content": "Board sync moved 3 cards to Done."},
            )
            assert resp.status == 200

        assert [e["content"] for e in slot._pending_context] == [
            "Board sync moved 3 cards to Done."
        ]
        assert len(_inject_rows(slot)) == 1
