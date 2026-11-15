"""A request path holding a NUL byte or a lone surrogate gets the route's own refusal.

``os.path.expanduser`` and ``os.path.realpath`` raise ``ValueError`` on such a
string, which reached the client as a 500. Each site now answers as it does for any
other path it cannot use.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_slot_switch_atomicity_helpers import _make_app, _mock_state

from kiro_crew import platform_compat
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat_api import project_lists
from kiro_crew.dashboard.chat_folders_paths import _validate_project_dir
from kiro_crew.dashboard.file_api.browse import _create_browse_dir_sync
from kiro_crew.dashboard.file_api.editor import _resolve_diff_path
from kiro_crew.dashboard.file_api.project_dirs import _match_known_project
from kiro_crew.dashboard.state import _ChatSlot

BAD_PATHS = ["/tmp/a\x00b", "~\x00x", "/tmp/\ud800", "~\ud800"]

# POSIX only: Windows path functions accept these strings, so nothing raises there.
_POSIX_ONLY = pytest.mark.skipif(
    platform_compat.IS_WINDOWS, reason="Windows path calls do not raise on these strings"
)


@_POSIX_ONLY
@pytest.mark.parametrize("raw", BAD_PATHS, ids=repr)
def test_the_path_helpers_refuse_instead_of_raising(raw: str) -> None:
    assert _validate_project_dir(raw) == ("", "Project directory must be an existing directory")
    assert project_lists._probe_favorite_candidate(raw) == (raw, False, False)
    assert _match_known_project(raw, ["/tmp"]) is None
    assert _resolve_diff_path(raw) == (raw, False)
    assert _create_browse_dir_sync(raw, "new") == ("not_found", raw)


@pytest.mark.parametrize("raw", BAD_PATHS, ids=repr)
def test_removing_such_a_favourite_leaves_the_list_alone(raw: str, monkeypatch) -> None:
    writes: list[list[str]] = []
    # Owner functions run on the chat_handlers facade globals, so it is patched there.
    monkeypatch.setattr(chat_handlers, "_load_favorite_projects", lambda strict=False: ["/a"])
    monkeypatch.setattr(chat_handlers, "_write_favorite_projects", writes.append)
    monkeypatch.setattr(chat_handlers, "_read_favorite_projects", lambda: ["/a"])
    assert chat_handlers._remove_favorite_project(raw) == ["/a"]
    assert writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", BAD_PATHS, ids=repr)
async def test_a_slot_project_switch_answers_not_a_directory(raw: str) -> None:
    slot = _ChatSlot("chat-a")
    state = _mock_state(slot)
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/chat-a/project", json={"project": raw})
        assert resp.status == 400
        assert (await resp.json())["error"] == "Not a directory"
