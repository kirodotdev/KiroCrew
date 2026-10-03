# SPDX-License-Identifier: Apache-2.0
"""The note's source label is the AUTHENTICATED caller, stamped on the row.

A note posted through ``POST /api/chat/slots/{slot}/note`` renders as a
``reconcile-note`` bubble. The reader needs to see which app wrote it, so the
visible row's ``meta`` carries ``appLabel`` on both write paths (immediate and
the durable deferred hold), and the frontend renders it through the SAME
"Sent by app ..." pill (``components.mcpApp.from_app``) an app inject row
already uses — no parallel note-only meta key or pill.

The label is stamped from the AUTHENTICATED caller identity (``request_app``,
set by the app-token auth middleware from the validated token record), NEVER
from ``body["source"]``. The endpoint accepts app tokens, so a free-text source
would let app A post ``source="Kiro"`` or another app's name and have the bubble
present it as the author. Binding the label to ``request_app`` makes it
spoof-proof: an app can only ever be attributed as itself. A dashboard user
carries an empty ``request_app`` and gets no author pill — the note is their own
and needs no attribution. Because the label is a trusted, slug-shaped identity
rather than caller-controlled free text, it needs no credential/exfil redaction;
the restore path only re-applies the writer's length and control-character bound
on the on-disk trust boundary. Note CONTENT runs through the
exfil → credential redaction pair and the label path leaves it untouched.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_handlers import api_chat_slot_note
from kiro_crew.dashboard.slot_buffers import (
    sanitize_restored_deferred_notes,
    serialize_deferred_notes,
)
from kiro_crew.dashboard.state import DashboardState


@web.middleware
async def _stamp_app(request: web.Request, handler):
    """Stand in for the app-token auth middleware.

    The real middleware sets ``request["app"]`` from the validated token
    record; here it reads an ``X-Test-App`` header, so a test can post AS an
    app (header set) or AS a dashboard user (header absent -> empty). The
    handler reads ``request.get("app", "")`` exactly as it does in production,
    so the label it stamps is the authenticated identity and never the body.
    """
    request["app"] = request.headers.get("X-Test-App", "")
    return await handler(request)


@asynccontextmanager
async def _client(state: DashboardState):
    app = web.Application(middlewares=[_stamp_app])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/note", api_chat_slot_note)
    async with TestClient(TestServer(app)) as c:
        yield c


def _note_row(slot) -> dict:
    for message in slot.messages:
        if message.get("cls") == "reconcile-note":
            return message
    raise AssertionError("no reconcile-note row was appended")


class TestImmediatePath:
    @pytest.mark.asyncio
    async def test_label_is_the_authenticated_app_identity(self, tmp_path: Path, monkeypatch):
        # An app posts to a slot it owns; the pill reads the app's own
        # authenticated identity, not anything from the body.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1", app="board-sync")
        slot._titled = True
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/note",
                json={"content": "board is green"},
                headers={"X-Test-App": "board-sync"},
            )
            assert resp.status == 200
        row = _note_row(slot)
        assert row["meta"]["appLabel"] == "board-sync"

    @pytest.mark.asyncio
    async def test_app_cannot_spoof_another_apps_name(self, tmp_path: Path, monkeypatch):
        # THE trust fix: app "app-a" posts body source="Kiro" (another app's
        # name). The pill must read the AUTHENTICATED identity "app-a" and never
        # the forged body value, so one app can never be presented as the author
        # of another's note.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s2", app="app-a")
        slot._titled = True
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s2/note",
                json={"content": "pretending to be someone else", "source": "Kiro"},
                headers={"X-Test-App": "app-a"},
            )
            assert resp.status == 200
        row = _note_row(slot)
        assert row["meta"]["appLabel"] == "app-a"
        assert row["meta"]["appLabel"] != "Kiro"

    @pytest.mark.asyncio
    async def test_dashboard_user_gets_no_author_pill(self, tmp_path: Path, monkeypatch):
        # A dashboard user carries an empty request_app, even if the body
        # supplies a source. The note is the user's own, so the renderer shows
        # the pill only on a truthy value and the row stays unattributed.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s3")
        slot._titled = True
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s3/note",
                json={"content": "plain", "source": "would-be-label"},
            )
            assert resp.status == 200
        row = _note_row(slot)
        assert "appLabel" not in row["meta"]

    @pytest.mark.asyncio
    async def test_over_length_app_identity_is_bounded_consistently(
        self, tmp_path: Path, monkeypatch
    ):
        # An app name is admissible up to 120 chars (the import-path cap), past
        # the 64-char label bound. A deferred note from such an app must stamp a
        # value the restore sanitizer keeps unchanged — otherwise the label
        # persists verbatim, collapses to "" on restore, and the app loses its
        # pill forever. The admit path applies the SAME length bound, so an
        # over-length identity carries no pill here AND persists "" (identical
        # across the round-trip), rather than attributing then silently dropping.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        long_app = "a" * 100
        slot = state.get_or_create_slot("s9", app=long_app)
        slot._titled = True
        slot.append("user", "kick off a long turn")
        slot.drain()
        slot.task = asyncio.get_running_loop().create_future()
        try:
            async with _client(state) as client:
                resp = await client.post(
                    "/api/chat/slots/s9/note",
                    json={"content": "held by a long-named app"},
                    headers={"X-Test-App": long_app},
                )
                assert resp.status == 200
        finally:
            if not slot.task.done():
                slot.task.cancel()
        # Admit stamped "" (bounded), so the hold persists "" and the restore
        # sanitizer keeps "" — no attribution is dropped at restore because none
        # was promised.
        held = slot._deferred_notes[-1]["source"]
        assert held == ""
        assert (
            sanitize_restored_deferred_notes(serialize_deferred_notes([slot._deferred_notes[-1]]))[
                0
            ]["source"]
            == held
        )
        slot.task = None
        slot.flush_deferred_notes()
        row = _note_row(slot)
        assert "appLabel" not in row["meta"]

    @pytest.mark.asyncio
    async def test_content_credential_is_still_redacted(self, tmp_path: Path, monkeypatch):
        # Attribution scope: the SOURCE label carries no redaction (it is a
        # trusted authenticated identity), while note CONTENT keeps its
        # exfil → credential redaction, so a secret in the body never renders
        # raw in the visible row.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s4", app="cron")
        slot._titled = True
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s4/note",
                json={"content": "token AKIAIOSFODNN7EXAMPLE here"},
                headers={"X-Test-App": "cron"},
            )
            assert resp.status == 200
        row = _note_row(slot)
        assert "AKIAIOSFODNN7EXAMPLE" not in row["content"]

    @pytest.mark.asyncio
    async def test_zwj_emoji_content_survives_admission(self, tmp_path: Path, monkeypatch):
        # Attribution scope: the admit path does NOT normalize note content, so
        # a legitimate ZWJ emoji sequence persists into the visible row intact —
        # content runs through the exfil → credential redaction only,
        # which leaves a non-credential body (here an emoji) byte for byte.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s5", app="cron")
        slot._titled = True
        body = "shipped \U0001f469\u200d\U0001f4bb"
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s5/note",
                json={"content": body},
                headers={"X-Test-App": "cron"},
            )
            assert resp.status == 200
        row = _note_row(slot)
        assert row["content"] == body
        assert "\u200d" in row["content"]

    @pytest.mark.asyncio
    async def test_prefix_glued_boundary_guarded_token_is_redacted_in_content(
        self, tmp_path: Path, monkeypatch
    ):
        # Content redaction regression (unchanged by this PR): a boundary-guarded
        # Discord token is anchored by a negative lookbehind for a word char. An
        # ASCII prefix + U+200B + token must not render recoverable in the
        # visible content.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s6", app="cron")
        slot._titled = True
        token = "M" + "A" * 24 + "." + "B" * 6 + "." + "C" * 26
        body = "word\u200b" + token
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s6/note",
                json={"content": body},
                headers={"X-Test-App": "cron"},
            )
            assert resp.status == 200
        row = _note_row(slot)
        assert "C" * 26 not in row["content"]


class TestDeferredPath:
    @pytest.mark.asyncio
    async def test_held_note_carries_identity_and_flush_stamps_it(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s7", app="cron")
        slot._titled = True
        slot.append("user", "kick off a long turn")
        slot.drain()
        # A running turn holds the note instead of appending it immediately.
        slot.task = asyncio.get_running_loop().create_future()
        try:
            async with _client(state) as client:
                resp = await client.post(
                    "/api/chat/slots/s7/note",
                    json={"content": "held while running", "source": "spoofed"},
                    headers={"X-Test-App": "cron"},
                )
                assert resp.status == 200
        finally:
            if not slot.task.done():
                slot.task.cancel()
        # The hold carries the AUTHENTICATED identity, not the body source.
        assert slot._deferred_notes[-1]["source"] == "cron"
        slot.task = None
        slot.flush_deferred_notes()
        row = _note_row(slot)
        assert row["meta"]["appLabel"] == "cron"

    @pytest.mark.asyncio
    async def test_held_dashboard_note_carries_no_identity(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s8")
        slot._titled = True
        slot.append("user", "kick off a long turn")
        slot.drain()
        slot.task = asyncio.get_running_loop().create_future()
        try:
            async with _client(state) as client:
                resp = await client.post(
                    "/api/chat/slots/s8/note",
                    json={"content": "held by a dashboard user"},
                )
                assert resp.status == 200
        finally:
            if not slot.task.done():
                slot.task.cancel()
        assert slot._deferred_notes[-1]["source"] == ""
        slot.task = None
        slot.flush_deferred_notes()
        row = _note_row(slot)
        assert "appLabel" not in row["meta"]


class TestPersistenceRoundTrip:
    def test_serialize_then_sanitize_preserves_source(self):
        note = {
            "id": "abc123",
            "content": "held",
            "cls": "reconcile-note",
            "context": None,
            "session": "sess-key",
            "source": "board-sync",
        }
        wire = serialize_deferred_notes([note])
        assert wire[0]["source"] == "board-sync"
        restored = sanitize_restored_deferred_notes(wire)
        assert restored[0]["source"] == "board-sync"

    def test_zwj_emoji_content_is_preserved_on_restore(self):
        # Attribution scope: the restore path does NOT rewrite note content, so
        # a legitimate ZWJ emoji sequence (woman + laptop) round-trips byte for
        # byte — the restored content is only type/length checked, as it was
        # before this source-label PR.
        body = "shipped \U0001f469\u200d\U0001f4bb"
        raw = [
            {
                "id": "zwj1",
                "content": body,
                "cls": "reconcile-note",
                "context": None,
                "session": "sess-key",
                "source": "cron",
            }
        ]
        restored = sanitize_restored_deferred_notes(raw)
        assert restored[0]["content"] == body
        assert "\u200d" in restored[0]["content"]

    def test_non_string_source_collapses_to_empty(self):
        # On-disk metadata is a trust boundary: a tampered or legacy entry with a
        # non-string source restores with no label rather than a forged one.
        raw = [
            {
                "id": "abc123",
                "content": "held",
                "cls": "reconcile-note",
                "context": None,
                "session": "sess-key",
                "source": {"not": "a string"},
            }
        ]
        restored = sanitize_restored_deferred_notes(raw)
        assert restored[0]["source"] == ""

    def test_legacy_entry_without_source_restores_blank(self):
        # A legacy on-disk note that carries no source key must still restore
        # successfully, with an empty label.
        raw = [
            {
                "id": "abc123",
                "content": "held",
                "cls": "reconcile-note",
                "context": None,
                "session": "sess-key",
            }
        ]
        restored = sanitize_restored_deferred_notes(raw)
        assert restored[0]["source"] == ""

    def test_oversized_source_collapses_to_empty(self):
        # A persisted source over the writer's 64-char bound collapses to "" on
        # restore rather than retaining, broadcasting, and re-persisting an
        # arbitrarily large label on the on-disk trust boundary.
        raw = [
            {
                "id": "abc123",
                "content": "held",
                "cls": "reconcile-note",
                "context": None,
                "session": "sess-key",
                "source": "x" * 65,
            }
        ]
        restored = sanitize_restored_deferred_notes(raw)
        assert restored[0]["source"] == ""

    def test_control_character_source_collapses_to_empty(self):
        # On-disk metadata is a trust boundary: a tampered persisted source
        # carrying a control character is not a value any authenticated admit
        # path could stamp, so it collapses to "" rather than restoring a
        # malformed label.
        raw = [
            {
                "id": "abc123",
                "content": "held",
                "cls": "reconcile-note",
                "context": None,
                "session": "sess-key",
                "source": "board\x00sync",
            }
        ]
        restored = sanitize_restored_deferred_notes(raw)
        assert restored[0]["source"] == ""
