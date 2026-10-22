"""Signal files and status reactions end to end, against the FAKE ``signal-cli``.

Same harness as ``test_signal_e2e.py`` (its fixtures are reused): the channel
boots through ``maybe_start_signal``, so the real peer framing, client,
transport, dispatcher and renderer run, and only the agent turn is a stand-in.
The fake serves ``getAttachment`` from a directory standing in for signal-cli's
``<data-dir>/attachments``, and records every request, so each claim is read
off what was actually put on the wire.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote

import pytest
from test_signal_e2e import (  # noqa: F401  (fixtures are discovered by name)
    ACCOUNT,
    OWNER,
    _boot,
    _orch,
    _receive,
    _requests,
    _sends,
    fake_cli,
    receipts,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the fake binary is a POSIX wrapper")

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJRU5ErkJggg=="
)

WORKING = "\N{HOURGLASS WITH FLOWING SAND}"
DONE = "\N{WHITE HEAVY CHECK MARK}"


@pytest.fixture
def cli(request: pytest.FixtureRequest) -> dict[str, Path]:
    """``test_signal_e2e``'s fake binary, reached by name (imported above)."""
    return request.getfixturevalue("fake_cli")


@pytest.fixture
def rcpt(request: pytest.FixtureRequest) -> dict[str, Any]:
    """``test_signal_e2e``'s receive counter, reached by name (imported above)."""
    return request.getfixturevalue("receipts")


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """signal-cli's attachment store, as the fake reads it."""
    directory = tmp_path / "attachments"
    directory.mkdir()
    monkeypatch.setenv("FAKE_SIGNAL_ATTACHMENTS", str(directory))
    return directory


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    directory = tmp_path / "work"
    directory.mkdir()
    return directory


@pytest.fixture
def turns(monkeypatch: pytest.MonkeyPatch, workdir: Path) -> list[dict[str, Any]]:
    """A stand-in turn that records what it was handed and answers with a picture."""
    seen: list[dict[str, Any]] = []

    async def _yes(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def fake_drive_turn(turn: Any, **_kwargs: Any) -> None:
        paths = [ln for ln in turn.user_text.splitlines() if ln.startswith("/")]
        seen.append(
            {
                "user_text": turn.user_text,
                "spool": turn.inbound_route.text if turn.inbound_route else None,
                "paths": paths,
                "bytes": [Path(p).read_bytes() for p in paths],
            }
        )
        renderer = turn.renderer
        await renderer.on_turn_start()
        await renderer.on_text_chunk(f"Here it is.\n\n![a chart]({workdir / 'out.png'})")
        await renderer.on_done("end_turn")
        await renderer.close()

    monkeypatch.setattr("kiro_crew.signal_cli.transport_dispatch.inbound_permitted", _yes)
    monkeypatch.setattr("kiro_crew.signal_cli.transport.inbound_permitted", _yes)
    monkeypatch.setattr("kiro_crew.signal_cli.transport_dispatch.drive_turn", fake_drive_turn)
    return seen


def _orch_with_cwd(workdir: Path, allowed: list[str]) -> Any:
    orch = _orch(allowed=allowed)
    provider = SimpleNamespace(cwd=str(workdir))
    orch.sessions.get_provider = lambda _key: provider
    return orch


def _calls(log: Path, method: str) -> list[dict[str, Any]]:
    return [r["params"] for r in _requests(log) if r.get("method") == method]


class TestFilesEndToEnd:
    @pytest.mark.asyncio
    async def test_a_picture_in_and_a_picture_out_with_status_reactions(
        self,
        cli: dict[str, Path],
        rcpt: dict[str, Any],
        turns: list[dict[str, Any]],
        store: Path,
        workdir: Path,
    ) -> None:
        (store / "Xq3vT0aZ9bQ1.png").write_bytes(_PNG)
        (workdir / "out.png").write_bytes(_PNG)
        cli["notify"].write_text(json.dumps([_receive("receive_dm_attachments")]), encoding="utf-8")
        orch = _orch_with_cwd(workdir, [OWNER])
        client = await _boot(orch, rcpt, 1)
        try:
            # Inbound: fetched by id through getAttachment, staged, handed to the
            # turn with the person's words, and the spool kept the words alone.
            assert _calls(cli["log"], "getAttachment") == [
                {"id": "Xq3vT0aZ9bQ1.png", "recipient": OWNER}
            ]
            assert len(turns) == 1
            assert turns[0]["user_text"].startswith("what is in this picture?\n")
            assert turns[0]["bytes"] == [_PNG]
            assert turns[0]["spool"] == "what is in this picture?"
            # ... and the staged copy is gone once the turn is over.
            assert not Path(turns[0]["paths"][0]).exists()

            # Outbound: the text, then the picture as a data-URI attachment.
            sends = _sends(cli["log"])
            assert sends[0] == {"recipient": [OWNER], "message": "Here it is."}
            assert sends[1]["message"] == "a chart"
            head, payload = sends[1]["attachments"][0].split(",", 1)
            assert head == "data:image/png;filename=out.png;base64"
            assert base64.b64decode(payload) == _PNG
            assert unquote(head.split("filename=")[1].split(";")[0]) == "out.png"

            # Status reactions on the person's message: working, then done.
            reactions = _calls(cli["log"], "sendReaction")
            assert [r["emoji"] for r in reactions] == [WORKING, DONE]
            assert all(
                r["recipient"] == [OWNER]
                and r["targetAuthor"] == OWNER
                and r["targetTimestamp"] == 1700000000300
                for r in reactions
            )
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_a_missing_stored_copy_is_told_to_the_agent(
        self,
        cli: dict[str, Path],
        rcpt: dict[str, Any],
        turns: list[dict[str, Any]],
        store: Path,
        workdir: Path,
    ) -> None:
        cli["notify"].write_text(json.dumps([_receive("receive_dm_attachments")]), encoding="utf-8")
        orch = _orch_with_cwd(workdir, [OWNER])
        client = await _boot(orch, rcpt, 1)
        try:
            assert len(turns) == 1 and turns[0]["paths"] == []
            assert "[Attachment chart.png — signal-cli has no stored copy of it]" in (
                turns[0]["user_text"]
            )
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_the_own_reaction_synced_back_in_note_to_self_is_not_a_turn(
        self,
        cli: dict[str, Path],
        rcpt: dict[str, Any],
        turns: list[dict[str, Any]],
        workdir: Path,
    ) -> None:
        cli["notify"].write_text(
            json.dumps(
                [_receive("receive_note_to_self"), _receive("receive_reaction_note_to_self")]
            ),
            encoding="utf-8",
        )
        orch = _orch_with_cwd(workdir, [ACCOUNT])
        client = await _boot(orch, rcpt, 2)
        try:
            # One turn for the note; the reaction envelope (the shape this
            # channel's own ✅ takes when another device syncs it) is dropped.
            assert len(turns) == 1
            reactions = _calls(cli["log"], "sendReaction")
            assert [r["emoji"] for r in reactions] == [WORKING, DONE]
            assert all(r.get("noteToSelf") is True for r in reactions)
        finally:
            await client.close()
