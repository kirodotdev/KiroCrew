"""Archive segment readers pair a segment with its WHOLE stem.

``_safe_key`` keeps underscores, so the segments of a chat whose stem is
``<stem>__private`` also start with ``<stem>__``. Each reader below must still
find its own chat's segments (including a same-second ``-N`` one) and must not
take the other chat's.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.history_files import archive_segment_stem

_HEADER = {"_type": "archive", "reason": "rotate", "archived_at": "2026-01-01", "count": 1}


def _write(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _user(text: str) -> dict:
    return {"role": "user", "content": text, "ts": "t"}


@pytest.mark.parametrize(
    ("name", "stem"),
    [
        ("notes__20260101-120000.jsonl", "notes"),
        ("notes__20260101-120000-3.jsonl", "notes"),
        ("notes__private__20260101-120000.jsonl", "notes__private"),
        # A stem that ends in an underscore keeps it.
        ("notes___20260101-120000.jsonl", "notes_"),
        ("notes.jsonl", None),
        ("notes__20260101-120000.json", None),
    ],
)
def test_archive_segment_stem(name: str, stem: str | None) -> None:
    assert archive_segment_stem(name) == stem


def test_digest_reads_only_its_own_chats_segments(tmp_path: Path) -> None:
    from kiro_crew.session_digest import digest

    sessions = tmp_path / "sessions"
    archive = sessions / "archive"
    _write(sessions / "notes.jsonl", [{"_type": "metadata"}, _user("live")])
    _write(archive / "notes__20260101-000000.jsonl", [_HEADER, _user("own old")])
    _write(archive / "notes__20260102-000000-2.jsonl", [_HEADER, _user("own same second")])
    _write(sessions / "notes__private.jsonl", [{"_type": "metadata"}])
    _write(archive / "notes__private__20251231-000000.jsonl", [_HEADER, _user("other chat")])

    with (
        patch("kiro_crew.session_digest.data_home", return_value=tmp_path),
        patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=tmp_path / "cli"),
    ):
        mine = digest("notes", ("notes",), "sid-none")
        other = digest("notes__private", ("notes__private",), "sid-none")

    assert (mine.first_message, mine.turns) == ("own old", 3)
    assert (other.first_message, other.turns) == ("other chat", 1)


def test_digest_reads_both_slack_stems_and_neither_neighbour(tmp_path: Path) -> None:
    from kiro_crew.session_digest import digest

    archive = tmp_path / "sessions" / "archive"
    canonical, legacy = "slack_1700000000.000100", "1700000000.000100"
    _write(archive / f"{canonical}__20260101-000000.jsonl", [_HEADER, _user("canonical")])
    _write(archive / f"{legacy}__20251201-000000-1.jsonl", [_HEADER, _user("legacy")])
    _write(archive / f"{canonical}__x__20260101-000000.jsonl", [_HEADER, _user("neighbour a")])
    _write(archive / f"{legacy}__x__20251201-000000.jsonl", [_HEADER, _user("neighbour b")])

    with (
        patch("kiro_crew.session_digest.data_home", return_value=tmp_path),
        patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=tmp_path / "cli"),
    ):
        result = digest("slack", (canonical, legacy), "sid-none")

    assert (result.first_message, result.turns) == ("canonical", 2)


def test_person_message_check_ignores_another_chats_segments(tmp_path: Path) -> None:
    from kiro_crew.history import ConversationLog
    from kiro_crew.transcript_person_rows import transcript_has_person_message

    log = ConversationLog(tmp_path / "sessions")
    key = "dashboard:member-scout"
    log.append(key, "assistant", "a reply")
    archive = log._dir / "archive"
    _write(
        archive / "dashboard_member-scout__private__20260101-000000.jsonl", [_HEADER, _user("hi")]
    )

    assert transcript_has_person_message(log, key) is False

    _write(archive / "dashboard_member-scout__20260101-000000-1.jsonl", [_HEADER, _user("hi")])
    assert transcript_has_person_message(log, key) is True


def test_crewmate_chat_check_ignores_another_chats_segments(tmp_path, monkeypatch) -> None:
    from kiro_crew import crewmate_prune_migration as mig
    from kiro_crew.config.loader import KiroCrewConfig

    monkeypatch.setattr("kiro_crew.members.member_slug", lambda name, cfg=None: name)
    monkeypatch.setattr("kiro_crew.members.dm_binding_path", lambda slug: tmp_path / "none.json")
    sessions = tmp_path / "sessions"
    meta = {"_type": "metadata", "title": "t", "mode": "member"}
    _write(sessions / "dashboard_member-scout.jsonl", [meta])
    _write(
        sessions / "archive" / "dashboard_member-scout__x__20260101-000000.jsonl",
        [meta, _user("hi")],
    )
    cfg = KiroCrewConfig.load()

    assert mig._chatted(cfg, "scout", sessions) is False

    _write(
        sessions / "archive" / "dashboard_member-scout__20260101-000000-1.jsonl",
        [meta, _user("hi")],
    )
    assert mig._chatted(cfg, "scout", sessions) is True


def test_milestone_chat_count_keeps_two_chats_apart(tmp_path, monkeypatch) -> None:
    from kiro_crew import milestones as m

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    sessions = tmp_path / "sessions"
    meta = {"_type": "metadata", "memory_mode": "persistent"}
    _write(sessions / "dashboard_chat-1.jsonl", [meta])
    _write(sessions / "dashboard_chat-1__private.jsonl", [meta])
    # A segment of each counts once with its own chat, not as a third chat.
    _write(sessions / "archive" / "dashboard_chat-1__20260101-000000.jsonl", [meta])
    _write(sessions / "archive" / "dashboard_chat-1__20260101-000000-1.jsonl", [meta])
    _write(sessions / "archive" / "dashboard_chat-1__private__20260101-000000.jsonl", [meta])
    # A chat known only from a same-second segment still counts, under its own stem.
    _write(sessions / "archive" / "dashboard_chat-2__20260101-000000-1.jsonl", [meta])

    assert m._count_chat_transcripts() == 3


@pytest.mark.asyncio
async def test_archive_list_for_a_key_omits_another_chats_segments(tmp_path, monkeypatch) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import kiro_crew.history as history_mod
    from kiro_crew.dashboard.handlers import api_session_archive_list

    sessions = tmp_path / "sessions"
    archive = sessions / "archive"
    _write(archive / "notes__20260101-000000.jsonl", [_HEADER])
    _write(archive / "notes__20260101-000000-2.jsonl", [_HEADER])
    _write(archive / "notes__private__20260102-000000.jsonl", [_HEADER])
    _write(archive / "legacy.jsonl", [_HEADER])
    monkeypatch.setattr(history_mod, "_sessions_dir", lambda: sessions)
    app = web.Application()
    app.router.add_get("/api/session/archive", api_session_archive_list)

    async with TestClient(TestServer(app)) as client:
        mine = (await (await client.get("/api/session/archive?key=notes")).json())["archives"]
        every = (await (await client.get("/api/session/archive")).json())["archives"]

    assert sorted(e["name"] for e in mine) == [
        "notes__20260101-000000-2.jsonl",
        "notes__20260101-000000.jsonl",
    ]
    by_name = {e["name"]: (e["key"], e["stamp"]) for e in every}
    assert by_name["notes__private__20260102-000000.jsonl"] == ("notes__private", "20260102-000000")
    assert by_name["notes__20260101-000000-2.jsonl"] == ("notes", "20260101-000000-2")
    # A file with no delimiter is listed with key = its stem and no stamp.
    assert by_name["legacy.jsonl"] == ("legacy", "")


def test_rotated_read_takes_only_its_own_chats_segments(tmp_path: Path) -> None:
    from kiro_crew.history import ConversationLog

    log = ConversationLog(base_dir=tmp_path)
    archive = tmp_path / "archive"
    _write(archive / "notes__20260101-000000.jsonl", [_HEADER, _user("own old")])
    _write(archive / "notes__20260101-000000-2.jsonl", [_HEADER, _user("own same second")])
    _write(archive / "notes__x__20250101-000000.jsonl", [_HEADER, _user("other chat")])

    assert [m["content"] for m in log.read_rotated_messages("notes")] == [
        "own old",
        "own same second",
    ]
    assert [m["content"] for m in log.read_rotated_messages("notes__x")] == ["other chat"]


def test_mid_rotation_probe_ignores_another_chats_segments(tmp_path: Path) -> None:
    from kiro_crew.history import ConversationLog

    log = ConversationLog(base_dir=tmp_path)
    tab = "tabwholestem1"
    k1 = "dashboard:chat-1-100"
    k2 = "dashboard:chat-2-200"
    log.append(k1, "user", "head row", tab_id=tab)
    log.append(k2, "user", "tail row", tab_id=tab)
    archive = tmp_path / "archive"
    _write(archive / "dashboard_chat-2-200__x__20260101-000000.jsonl", [_HEADER, _user("other")])

    assert log.chain_mid_rotation(k2) is False

    _write(archive / "dashboard_chat-2-200__20260101-000000-1.jsonl", [_HEADER, _user("own")])
    assert log.chain_mid_rotation(k2) is True


def test_transient_session_delete_keeps_another_chats_segments(tmp_path: Path) -> None:
    from kiro_crew.history import ConversationLog

    log = ConversationLog(base_dir=tmp_path)
    key = "memory-consolidation:store1:run"
    archive = tmp_path / "archive"
    own = _write(archive / "memory-consolidation_store1_run__20260101-000000.jsonl", [_HEADER])
    own_n = _write(archive / "memory-consolidation_store1_run__20260101-000000-1.jsonl", [_HEADER])
    other = _write(archive / "memory-consolidation_store1_run__x__20260101-000000.jsonl", [_HEADER])

    assert log.delete_memory_consolidation_session(key, "store1") is True

    assert not own.exists() and not own_n.exists()
    assert other.exists()


def test_archive_segments_of_stops_at_the_stem_boundary(tmp_path: Path) -> None:
    from kiro_crew.history_files import archive_segments_of

    names = [
        "a__20260101-000000.jsonl",
        "a__20260101-000000-1.jsonl",
        "a___20260101-000000.jsonl",
        "a__more__20260101-000000.jsonl",
        "a__more__x__20260101-000000.jsonl",
    ]
    for name in names:
        _write(tmp_path / name, [_HEADER])

    assert sorted(p.name for p in archive_segments_of(tmp_path, "a")) == [
        "a__20260101-000000-1.jsonl",
        "a__20260101-000000.jsonl",
    ]
    assert [p.name for p in archive_segments_of(tmp_path, "a_")] == ["a___20260101-000000.jsonl"]
    assert [p.name for p in archive_segments_of(tmp_path, "a__more")] == [
        "a__more__20260101-000000.jsonl"
    ]


@pytest.mark.skipif(
    os.path.normcase("A") == "A", reason="the archive glob folds case only on Windows"
)
def test_archive_segments_of_keeps_a_differently_cased_own_segment(tmp_path: Path) -> None:
    from kiro_crew.history_files import archive_segments_of

    _write(tmp_path / "A__20260101-000000.jsonl", [_HEADER])
    _write(tmp_path / "A__more__20260101-000000.jsonl", [_HEADER])

    assert [p.name for p in archive_segments_of(tmp_path, "a")] == ["A__20260101-000000.jsonl"]
