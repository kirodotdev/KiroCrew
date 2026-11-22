"""The one bell note that says Kiro Crew cannot confirm KAS turned its telemetry off.

the the-kas-telemetry-off-notice-is-one-bell-note-per-install entry in docs/decisions:
the first session on a data home whose KAS process was asked to turn telemetry off
posts one note, claimed with a marker first; no other session or chat posts another.
"""

from __future__ import annotations

import errno
from types import SimpleNamespace
from typing import Any

import pytest
from turn_harness import ScriptedProvider, TurnScript, run_turn

from kiro_crew.acp import kas_cli_settings as kcs
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN, AcpEvent
from kiro_crew.agent_sdk import kas_telemetry_notice as notice


class _Bell:
    """A state whose ``notify`` records each note it is given."""

    def __init__(self) -> None:
        self.notes: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def notify(self, *args: Any, **kwargs: Any) -> None:
        self.notes.append((args, kwargs))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(notice, "config_dir", lambda: tmp_path)
    return tmp_path


def _client(text: str) -> SimpleNamespace:
    return SimpleNamespace(telemetry_unconfirmed_notice=text)


@pytest.mark.asyncio
async def test_the_first_session_on_a_data_home_posts_one_note(home) -> None:
    bell = _Bell()
    assert await notice.deliver(bell, _client(kcs.TELEMETRY_UNCONFIRMED_NOTICE))
    assert bell.notes == [
        (
            ("agent", "Kiro Crew asked KAS to turn telemetry off", notice.NOTE_BODY),
            {"channel": "system.agent"},
        )
    ]
    assert "ℹ " + notice.NOTE_BODY == kcs.TELEMETRY_UNCONFIRMED_NOTICE
    assert (home / "announced-kas-telemetry-unconfirmed").is_file()
    # Another chat's new session on the same data home.
    assert not await notice.deliver(bell, _client(kcs.TELEMETRY_UNCONFIRMED_NOTICE))
    assert len(bell.notes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client",
    [
        _client(kcs.TELEMETRY_NOT_APPLIED_NOTICE),
        _client(""),
        SimpleNamespace(),  # a client that is not a KAS session
    ],
    ids=["not-applied", "empty", "not-kas"],
)
async def test_a_session_without_the_unconfirmed_notice_posts_nothing(home, client) -> None:
    bell = _Bell()
    assert not await notice.deliver(bell, client)
    assert bell.notes == []
    assert list(home.iterdir()) == []


@pytest.mark.asyncio
async def test_a_marker_that_cannot_be_written_posts_nothing(monkeypatch, caplog) -> None:
    def _read_only_home() -> Any:
        # Raised inside the claim, as creating the data home or the marker would on a
        # read-only home: an OSError that is not "the marker already exists", on every OS.
        raise PermissionError(errno.EACCES, "read-only data home")

    monkeypatch.setattr(notice, "config_dir", _read_only_home)
    bell = _Bell()
    assert not await notice.deliver(bell, _client(kcs.TELEMETRY_UNCONFIRMED_NOTICE))
    assert bell.notes == []
    assert "could not claim the KAS telemetry notice" in caplog.text


@pytest.mark.asyncio
async def test_a_note_that_fails_to_post_does_not_fail_the_turn(home, caplog) -> None:
    class _BrokenBell:
        def notify(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("bell unavailable")

    assert not await notice.deliver(_BrokenBell(), _client(kcs.TELEMETRY_UNCONFIRMED_NOTICE))
    assert "could not deliver the KAS telemetry notice" in caplog.text


class _UnconfirmedSession(ScriptedProvider):
    """A new KAS session whose process was sent ``telemetryEnabled: false``."""

    @property
    def telemetry_unconfirmed_notice(self) -> str:
        return kcs.TELEMETRY_UNCONFIRMED_NOTICE


@pytest.mark.asyncio
async def test_a_new_dashboard_session_posts_the_note_and_shows_no_row(home) -> None:
    """Through ``_run_chat``: the new session's admission awaits the note, and the
    chat's first telemetry notice is no transcript row."""
    bells: list[_Bell] = []

    def _record_bell(ctx: Any) -> None:
        bell = _Bell()
        bells.append(bell)
        ctx.state.notify = bell.notify

    record = await run_turn(
        TurnScript(
            events=[
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="pong"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ],
            provider=_UnconfirmedSession,
            setup=_record_bell,
        )
    )
    assert [row.get("role") for row in record.history_rows].count("assistant") == 1
    assert not [row for row in record.history_rows if "telemetry" in str(row.get("content", ""))]
    [bell] = bells
    assert [args[1] for args, _ in bell.notes] == [notice.NOTE_TITLE]
    assert (home / notice.MARKER_FILENAME).is_file()
