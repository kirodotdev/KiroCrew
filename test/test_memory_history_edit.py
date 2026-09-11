"""Owner document saves preserve V2 bytes and retained daily history."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import date, timedelta
from unittest.mock import Mock

import pytest
from member_memory_helpers import document_store
from member_memory_helpers import env as _member_env
from member_memory_helpers import request

from kiro_crew.dashboard.handlers import memory

env = _member_env
pytestmark = pytest.mark.xdist_group("member_memory_api")


@pytest.mark.asyncio
@pytest.mark.parametrize("has_today", [False, True])
@pytest.mark.parametrize("line_ending", ["\n", "\r\n"], ids=["lf", "crlf"])
async def test_private_history_read_edit_save_keeps_prior_days_once(
    env, monkeypatch, has_today, line_ending
):
    store = await document_store(env, "member-alice")
    today = store._today_history_file()
    monkeypatch.setattr(store, "_today_history_file", lambda: today)
    yesterday = today.with_name(f"{date.fromisoformat(today.stem) - timedelta(days=1)}.md")
    older_bytes = b"# Prior day\nRetained yesterday sentinel.\n"
    await asyncio.to_thread(yesterday.write_bytes, older_bytes)
    original = line_ending.join(
        ["# 1999-01-01", "An owner heading is ordinary content.", "Today sentinel.", ""]
    )
    if has_today:
        await asyncio.to_thread(today.write_text, original, encoding="utf-8", newline="")
    expected = original if has_today else ""

    for visit in range(2):
        response = await memory.api_memory_history(
            request(env, query={"store": "member-alice"}, owner=True, session="dashboard:ui")
        )
        assert response.status == 200
        body = json.loads(response.text)
        assert body == {"content": expected, "content_redacted": False}
        submitted = body["content"]
        if visit == 0:
            submitted += f"Owner daily edit sentinel.{line_ending}"
        expected = submitted
        saved = await memory.api_memory_history(
            request(
                env,
                body={"content": submitted},
                query={"store": "member-alice"},
                owner=True,
                session="dashboard:ui",
            ).clone(method="PUT")
        )
        assert saved.status == 200
        assert await asyncio.to_thread(today.read_bytes) == expected.encode("utf-8")
        assert await asyncio.to_thread(yesterday.read_bytes) == older_bytes
        aggregate = await asyncio.to_thread(store.read_recent_history)
        assert aggregate.count("Retained yesterday sentinel.") == 1
        assert aggregate.count("Owner daily edit sentinel.") == 1
        if has_today:
            assert aggregate.count("Today sentinel.") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("document", ["preferences", "projects"])
@pytest.mark.parametrize("line_ending", ["\n", "\r\n"], ids=["lf", "crlf"])
async def test_private_profile_unchanged_save_preserves_line_endings(env, document, line_ending):
    # Keep the API's owner, store and config validation real; this fixture has
    # no prompt builder, so capture only its final essential-context boundary.
    essentials = Mock()
    env.state.context_builder._build_v2_essentials = essentials
    store = await document_store(env, "member-alice")
    target = getattr(store, f"_{document}_file")
    other_document = "projects" if document == "preferences" else "preferences"
    other_target = getattr(store, f"_{other_document}_file")
    other_bytes = await asyncio.to_thread(other_target.read_bytes)
    heading = "# Active Projects" if document == "projects" else "# User Preferences"
    # Projects already normalize the final newline. Keep that contract while
    # proving the interior LF/CRLF bytes survive an unchanged owner save.
    original = line_ending.join([heading, "Owner profile sentinel.", "Second line."]) + "\n"
    await asyncio.to_thread(target.write_bytes, original.encode("utf-8"))
    handler = getattr(memory, f"api_memory_{document}")

    for _ in range(2):
        response = await handler(
            request(env, query={"store": "member-alice"}, owner=True, session="dashboard:ui")
        )
        assert response.status == 200
        body = json.loads(response.text)
        assert body == {"content": original, "content_redacted": False}
        saved = await handler(
            request(
                env,
                body={"content": body["content"]},
                query={"store": "member-alice"},
                owner=True,
                session="dashboard:ui",
            ).clone(method="PUT")
        )
        assert saved.status == 200
        assert essentials.call_args.args[0] == "member-alice"
        assert essentials.call_args.kwargs["profile_overrides"] == {f"{document}.md": original}
        assert await asyncio.to_thread(target.read_bytes) == original.encode("utf-8")
        assert await asyncio.to_thread(other_target.read_bytes) == other_bytes


@pytest.mark.asyncio
async def test_global_history_keeps_its_existing_aggregate_edit_contract(env, monkeypatch):
    store = await document_store(env, "")
    today = store._today_history_file()
    monkeypatch.setattr(store, "_today_history_file", lambda: today)
    yesterday = today.with_name(f"{date.fromisoformat(today.stem) - timedelta(days=1)}.md")
    await asyncio.to_thread(yesterday.write_text, "Prior V1 day", encoding="utf-8")
    await asyncio.to_thread(today.write_text, "Current V1 day", encoding="utf-8")
    aggregate = await asyncio.to_thread(store.read_recent_history)

    response = await memory.api_memory_history(request(env, owner=True, session="dashboard:ui"))

    assert response.status == 200
    assert json.loads(response.text) == {"content": aggregate, "content_redacted": False}
    assert "Prior V1 day" in aggregate and "Current V1 day" in aggregate
    replacement = "Owner V1 replacement\n"
    saved = await memory.api_memory_history(
        request(env, body={"content": replacement}, owner=True, session="dashboard:ui").clone(
            method="PUT"
        )
    )
    assert saved.status == 200
    assert await asyncio.to_thread(today.read_text, encoding="utf-8") == replacement
    assert await asyncio.to_thread(today.read_bytes) == replacement.replace(
        "\n", os.linesep
    ).encode("utf-8")
    assert await asyncio.to_thread(yesterday.read_text, encoding="utf-8") == "Prior V1 day"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["append", "hidden_content"])
async def test_private_history_rechecks_exact_target_before_replacement(env, monkeypatch, change):
    store = await document_store(env, "member-alice")
    await asyncio.to_thread(store.append_history, "Clean initial entry")
    today = store._today_history_file()
    monkeypatch.setattr(store, "_today_history_file", lambda: today)
    original_write = store.write_today_history
    winner = b""

    def change_before_lock(content, **kwargs):
        nonlocal winner
        if change == "append":
            store.append_history("Concurrent consolidation sentinel")
        else:
            today.write_text("Preserve AKIAIOSFODNN7EXAMPLE exactly", encoding="utf-8")
        winner = today.read_bytes()
        return original_write(content, **kwargs)

    monkeypatch.setattr(store, "write_today_history", change_before_lock)
    response = await memory.api_memory_history(
        request(
            env,
            body={"content": "Must not overwrite the winner"},
            query={"store": "member-alice"},
            owner=True,
            session="dashboard:ui",
        ).clone(method="PUT")
    )

    assert response.status == 409
    expected_code = "memory_document_changed" if change == "append" else "memory_document_redacted"
    assert json.loads(response.text)["code"] == expected_code
    sentinel = (
        b"Concurrent consolidation sentinel" if change == "append" else b"AKIAIOSFODNN7EXAMPLE"
    )
    assert sentinel in winner
    assert await asyncio.to_thread(today.read_bytes) == winner
    assert "AKIAIOSFODNN7EXAMPLE" not in response.text
