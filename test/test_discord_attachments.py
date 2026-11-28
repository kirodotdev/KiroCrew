"""A picture sent to the bot on Discord outlives the turn that downloaded it.

The turn downloads each attachment to a temp file it deletes when it ends, so the
user row has to name a copy kept with the session instead of that temp path.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from discord_helpers import _drop_live_config_snapshot  # noqa: F401
from discord_helpers import _PNG, FakeClient, FakeCtx, FakeSessions, _cfg, _prime_live

from kiro_crew.discord.transport_dispatch import DiscordDispatcher
from kiro_crew.history import ConversationLog
from kiro_crew.messaging.transport import InboundMessage

_URL = "https://cdn.discordapp.com/attachments/c/m/photo.png"


async def _picture_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, log: ConversationLog
) -> tuple[Path, str, list[str], list[str]]:
    """Run one Discord turn for a picture captioned "look" into *log*.

    Returns the transcript path, its user row ("" when none was written), the
    texts handed to the live dashboard window, and the downloaded temp paths.
    """
    from kiro_crew.dashboard import channel_slots

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    cfg = _cfg()
    _prime_live(cfg)
    d = DiscordDispatcher(
        sessions=FakeSessions(),  # type: ignore[arg-type]
        ctx_builder=FakeCtx(),  # type: ignore[arg-type]
        cfg=cfg,
        allowed_user_ids={"u1"},
        conv_log=log,
    )
    cli = FakeClient()
    d.client = cli  # type: ignore[assignment]
    cli.attachment_bodies[_URL] = _PNG
    downloaded: list[str] = []
    real_download = cli.download_attachment

    async def _download(url: str, dest: str) -> None:
        await real_download(url, dest)
        downloaded.append(dest)

    monkeypatch.setattr(cli, "download_attachment", _download)
    projected: list[str] = []
    monkeypatch.setattr(
        channel_slots,
        "project_channel_turn_live",
        lambda _state, _key, user_text, *_a, **_k: projected.append(user_text),
    )
    picture = {"filename": "photo.png", "content_type": "image/png", "size": len(_PNG), "url": _URL}

    await d.handle_message(
        InboundMessage(
            channel_type="discord",
            user_id="u1",
            conversation_id="c1",
            text="look",
            attachments=[picture],
        )
    )

    path = log._path(d._session_key("u1"))
    rows = log.read_messages(d._session_key("u1"))
    user_text = next((row["content"] for row in rows if row.get("role") == "user"), "")
    return path, user_text, projected, downloaded


class TestAPictureOutlivesTheTurn:
    @pytest.mark.asyncio
    async def test_the_row_and_the_live_window_name_a_copy_kept_with_the_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The user row names a stored copy, not the temp file the turn deletes.

        The live dashboard window is handed the same text, because it rewrites the
        row from its own copy on every save.
        """
        from kiro_crew.chat_attachments import attachments_dir

        log = ConversationLog(base_dir=tmp_path / "sessions")
        path, user_text, projected, downloaded = await _picture_turn(tmp_path, monkeypatch, log)

        store = attachments_dir(path.parent, path.stem)
        assert store.is_dir(), user_text
        (stored,) = store.iterdir()
        stored_dest = str(stored).replace(os.sep, "/")
        assert user_text.splitlines() == ["look", f"![image]({stored_dest})"]
        assert projected == [user_text]
        assert downloaded and not os.path.exists(downloaded[0])

    @pytest.mark.asyncio
    async def test_a_store_that_fails_still_writes_the_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both writers get the text as it came in, so the turn is not lost."""
        from kiro_crew.history import HistoryLockTimeout

        log = ConversationLog(base_dir=tmp_path / "sessions")

        def _timeout(*_a: Any) -> str:
            raise HistoryLockTimeout("held")

        monkeypatch.setattr(log, "store_ingested_images", _timeout)
        _path, user_text, projected, _downloaded = await _picture_turn(tmp_path, monkeypatch, log)

        assert projected == [user_text]
        assert user_text.startswith("look\n") and "until this turn ends" in user_text
