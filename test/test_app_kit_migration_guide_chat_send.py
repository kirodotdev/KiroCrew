"""The App Kit migration guide sends a chat message the way the SDK's own send wire does.

The bare ``POST /api/chat`` answers with an event stream, which ``api.post``
cannot parse as JSON, and without a ``meta.sendId`` an immediate ``?ws=1`` send
gets no user-row echo. ``website/src/app-sdk/appSendWire.ts`` is the path
``ChatEmbed`` sends on, so the guide's example must name the same path and carry
a ``sendId`` the gateway accepts.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from kiro_crew.dashboard.chat_delivery import normalize_send_id

_ROOT = Path(__file__).resolve().parents[1]
_GUIDE = _ROOT / "docs" / "app-kit" / "migration-guide.md"
_WIRE = _ROOT / "website" / "src" / "app-sdk" / "appSendWire.ts"

#: An ``api.post`` to the chat send endpoint, not to ``/api/chat/<sub-route>``.
_CHAT_POST = re.compile(r"api\.post\('(/api/chat(?:\?[^']*)?)'")


def _guide_chat_posts() -> list[tuple[str, str]]:
    posts = []
    for line in _GUIDE.read_text(encoding="utf-8").splitlines():
        for match in _CHAT_POST.finditer(line):
            posts.append((match.group(1), line))
    return posts


def test_guide_chat_send_uses_the_sdk_send_path_with_a_send_id():
    wire = re.search(r"const APP_SEND_PATH = '([^']+)'", _WIRE.read_text(encoding="utf-8"))
    assert wire, "APP_SEND_PATH not found in appSendWire.ts"
    posts = _guide_chat_posts()
    assert posts, "the migration guide has no chat send example"
    for path, line in posts:
        assert path == wire.group(1), line
        assert "sendId: crypto.randomUUID()" in line, line


def test_the_guide_send_id_is_one_the_gateway_keeps():
    # A value the gateway refuses is treated as absent, so the echo would not go out.
    for _ in range(100):
        value = str(uuid.uuid4())
        assert normalize_send_id(value) == value
