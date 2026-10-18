"""Every channel renderer sends inline images from the session's own scratch dir.

The agent is told to stage screenshots under ``$KIROCREW_SCRATCH``, which is never
under the working directory. Each channel that uploads local images therefore
hands that one directory to extraction beside the cwd, and nothing wider: not the
managed scratch root, not another runtime's scratch, not a path that reaches
scratch by ``..`` or through a link. The extraction rules themselves are pinned in
``test_outbound_files_scratch_root.py``; this file pins that every channel's
renderer feeds them the session's scratch and keeps every refusal.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import requires_symlinks
from kiro_crew import agent_scratch

#: A real 2x2 PNG: WhatsApp decodes what it accepts, so a magic-bytes-only stub
#: would be refused there for a reason unrelated to the root.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJRU5ErkJggg=="
)

#: ``send(text)`` returns how many images the channel would upload.
Send = Callable[[str], Awaitable[int]]


def _png(directory: Path, name: str = "shot.png") -> Path:
    p = directory / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(_PNG)
    return p


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """An isolated data home, a project cwd, and two allocated scratch dirs."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.delenv("KIROCREW_SCRATCH_ROOT", raising=False)
    cwd = tmp_path / "project"
    cwd.mkdir()
    mine = agent_scratch.allocate_scratch("runtime")
    other = agent_scratch.allocate_scratch("runtime")
    assert mine != other and mine.parent == other.parent
    return cwd, mine, other


# -- one adapter per channel: authorize like its dispatcher, then extract ------


def _slack(cwd: str, scratch: str) -> Send:
    from kiro_crew.slack.renderer import SlackRenderer

    r = SlackRenderer(MagicMock(), "C1", "t1", reactions_enabled=False, show_thinking=False)
    r.authorize_upload_root(cwd, scratch_root=scratch)

    async def send(text: str) -> int:
        _, files, _ = await r._extract_uploads(text)
        return len(files)

    return send


def _discord(cwd: str, scratch: str) -> Send:
    from kiro_crew.discord.renderer import DiscordRenderer
    from kiro_crew.discord.transport import DISCORD_CAPABILITIES

    r = DiscordRenderer(
        MagicMock(), "chan1", DISCORD_CAPABILITIES, session_key="discord:u1", uploads_allowed=True
    )
    r.authorize_upload_root(cwd, scratch_root=scratch)

    async def send(text: str) -> int:
        _, files = await r._extract_uploads(text)
        return len(files)

    return send


def _telegram(cwd: str, scratch: str) -> Send:
    from kiro_crew.telegram.renderer import TelegramRenderer
    from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES

    r = TelegramRenderer(MagicMock(), 42, TELEGRAM_CAPABILITIES, session_key="telegram:1")
    r.authorize_upload_root(cwd, scratch_root=scratch)

    async def send(text: str) -> int:
        _, files = await r._extract_uploads(text)
        return len(files)

    return send


def _webex(cwd: str, scratch: str) -> Send:
    from kiro_crew.webex.renderer import WebexRenderer
    from kiro_crew.webex.transport import WEBEX_CAPABILITIES

    r = WebexRenderer(MagicMock(), "ROOM", WEBEX_CAPABILITIES, uploads_allowed=True)
    r.authorize_upload_root(cwd, scratch_root=scratch)

    async def send(text: str) -> int:
        _, files = await r._extract_uploads(text)
        return len(files)

    return send


def _teams(cwd: str, scratch: str) -> Send:
    from kiro_crew.teams.renderer import TeamsRenderer
    from kiro_crew.teams.transport import TEAMS_CAPABILITIES

    r = TeamsRenderer(
        MagicMock(),
        "conv-1",
        "https://smba.trafficmanager.net/",
        TEAMS_CAPABILITIES,
        session_key="teams:a:direct:u",
        upload_root=cwd,
    )
    r.authorize_scratch_root(scratch)

    async def send(text: str) -> int:
        _, files = await r._extract_uploads(text)
        return len(files)

    return send


class _WhatsAppTransport:
    def __init__(self) -> None:
        self.images: list[bytes] = []

    async def send_message(self, jid: str, content: str) -> str:
        return "ID1"

    async def send_image(self, jid: str, data: bytes, caption: str) -> str:
        self.images.append(data)
        return "IMG1"


def _whatsapp(cwd: str, scratch: str) -> Send:
    from kiro_crew.messaging.transport import TransportCapabilities
    from kiro_crew.whatsapp.turn_renderer import WhatsAppRenderer

    caps = TransportCapabilities(max_message_chars=4096, max_buttons=0, files_outbound=True)

    async def send(text: str) -> int:
        transport = _WhatsAppTransport()
        r = WhatsAppRenderer(
            transport,  # type: ignore[arg-type]
            AsyncMock(),
            "c@s.whatsapp.net",
            caps,
            upload_root=lambda: cwd,
            scratch_root=lambda: scratch,
        )
        r._buf = [text]
        await r._deliver_uploads()
        return len(transport.images)

    return send


CHANNELS: dict[str, Callable[[str, str], Send]] = {
    "slack": _slack,
    "discord": _discord,
    "telegram": _telegram,
    "webex": _webex,
    "teams": _teams,
    "whatsapp": _whatsapp,
}

channel = pytest.mark.parametrize("make", list(CHANNELS.values()), ids=list(CHANNELS))


@channel
@pytest.mark.asyncio
async def test_an_image_in_own_scratch_is_sent(
    make: Callable[[str, str], Send], layout: tuple[Path, Path, Path]
) -> None:
    cwd, mine, _ = layout
    shot = _png(mine, "repro.png")
    assert await make(str(cwd), str(mine))(f"see:\n\n![x]({shot})\n") == 1


@channel
@pytest.mark.asyncio
async def test_without_a_scratch_root_scratch_stays_refused(
    make: Callable[[str, str], Send], layout: tuple[Path, Path, Path]
) -> None:
    cwd, mine, _ = layout
    shot = _png(mine)
    assert await make(str(cwd), "")(f"![x]({shot})") == 0


@channel
@pytest.mark.asyncio
async def test_another_sessions_scratch_is_refused(
    make: Callable[[str, str], Send], layout: tuple[Path, Path, Path]
) -> None:
    cwd, mine, other = layout
    theirs = _png(other, "secret.png")
    assert await make(str(cwd), str(mine))(f"![x]({theirs})") == 0


@channel
@pytest.mark.asyncio
async def test_a_traversal_out_of_own_scratch_is_refused(
    make: Callable[[str, str], Send], layout: tuple[Path, Path, Path]
) -> None:
    cwd, mine, other = layout
    theirs = _png(other, "secret.png")
    spelled = os.path.join(str(mine), "..", other.name, theirs.name)
    assert await make(str(cwd), str(mine))(f"![x]({spelled})") == 0


@channel
@pytest.mark.asyncio
async def test_a_file_directly_under_the_managed_root_is_refused(
    make: Callable[[str, str], Send], layout: tuple[Path, Path, Path]
) -> None:
    cwd, mine, _ = layout
    loose = _png(mine.parent, "loose.png")
    assert await make(str(cwd), str(mine))(f"![x]({loose})") == 0


@channel
@pytest.mark.asyncio
async def test_the_managed_root_as_scratch_admits_nothing(
    make: Callable[[str, str], Send], layout: tuple[Path, Path, Path]
) -> None:
    cwd, mine, other = layout
    theirs = _png(other, "secret.png")
    assert await make(str(cwd), str(mine.parent))(f"![x]({theirs})") == 0


@channel
@pytest.mark.asyncio
async def test_scratch_never_stands_in_for_an_unauthorized_cwd(
    make: Callable[[str, str], Send], layout: tuple[Path, Path, Path]
) -> None:
    _, mine, _ = layout
    shot = _png(mine)
    assert await make("relative/cwd", str(mine))(f"![x]({shot})") == 0


@requires_symlinks
@channel
@pytest.mark.asyncio
async def test_a_linked_file_in_own_scratch_is_refused(
    make: Callable[[str, str], Send], layout: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    cwd, mine, _ = layout
    real = _png(tmp_path / "elsewhere", "secret.png")
    link = mine / "shot.png"
    link.symlink_to(real)
    assert await make(str(cwd), str(mine))(f"![x]({link})") == 0


@requires_symlinks
@channel
@pytest.mark.asyncio
async def test_a_linked_folder_escaping_own_scratch_is_refused(
    make: Callable[[str, str], Send], layout: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    cwd, mine, _ = layout
    real = _png(tmp_path / "elsewhere", "secret.png")
    (mine / "alias").symlink_to(real.parent, target_is_directory=True)
    assert await make(str(cwd), str(mine))(f"![x]({mine / 'alias' / real.name})") == 0


@requires_symlinks
@channel
@pytest.mark.asyncio
async def test_a_scratch_dir_that_is_itself_a_link_admits_nothing(
    make: Callable[[str, str], Send], layout: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    cwd, mine, _ = layout
    elsewhere = tmp_path / "elsewhere"
    shot = _png(elsewhere)
    planted = mine.parent / "runtime-deadbeef"
    planted.symlink_to(elsewhere, target_is_directory=True)
    assert await make(str(cwd), str(planted))(f"![x]({planted / shot.name})") == 0
