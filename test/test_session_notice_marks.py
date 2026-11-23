"""A chat shows each session-start spec or adapter notice once, not once per backend session.

A reconnect, a reload and a gateway restart each start a new backend session, and
the notices ``_prepare_spec_hooks`` adds there (a withheld project hook, a key
nothing carries, a too-long hook command, ...) describe the agent's spec, which
the new session did not change. The chat keeps a mark per notice it showed and
adds the row again only when the text or the folder it is about differs. The
adapter notices ``_notice_steer_upgrade`` adds (an untested release, a known
limitation, ...) take the same marks, keyed on their text and the session's
folder, and show again once no row of the chat shows them any more.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.agent_sdk.backends import ACP_BACKEND_GOOSE
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard import chat_runner

# The helper is imported per test, so a tree without it fails those tests rather
# than stopping the whole file at collection.

_AGENT = "kirocrew"


@pytest.fixture(autouse=True)
def _fresh_cache(_floor_monkeypatch):
    spec_hooks._cache.clear()
    yield
    spec_hooks._cache.clear()


@pytest.fixture
def agents_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "user-agents"
    d.mkdir()
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: d)
    return d


def _repo(parent: Path) -> Path:
    repo = parent / "repo"
    spec = repo / ".kiro" / "agents" / f"{_AGENT}.json"
    spec.parent.mkdir(parents=True)
    hooks = {"agentSpawn": [{"command": "repo-hook"}]}
    spec.write_text(json.dumps({"name": _AGENT, "prompt": "p", "hooks": hooks}), "utf-8")
    return repo


def test_a_notice_is_new_once_per_text_and_folder() -> None:
    from kiro_crew.dashboard.session_notice_marks import _session_notice_is_new

    chat = SimpleNamespace(_session_notice_marks=[])
    assert _session_notice_is_new(chat, "hooks withheld", "/a") is True
    assert _session_notice_is_new(chat, "hooks withheld", "/a") is False
    assert _session_notice_is_new(chat, "hooks withheld", "/b") is True, "another folder"
    assert _session_notice_is_new(chat, "other text", "/a") is True, "another text"
    assert len(chat._session_notice_marks) == 3


def test_surrogates_in_notice_text_and_key_keep_a_stable_mark() -> None:
    from kiro_crew.dashboard.session_notice_marks import (
        _session_notice_is_new,
        session_notice_mark,
    )

    text = json.loads('"file://notes-\\udcff.md"')
    key = "/folder-\udcff"
    mark = session_notice_mark(text, key)
    assert len(mark) == 16 and set(mark) <= set("0123456789abcdef")
    assert session_notice_mark(text, key) == mark
    chat = SimpleNamespace(_session_notice_marks=[])
    assert _session_notice_is_new(chat, text, key) is True
    assert _session_notice_is_new(chat, text, key) is False
    assert chat._session_notice_marks == [mark]


def test_a_slot_with_no_marks_always_shows_the_notice() -> None:
    from kiro_crew.dashboard.session_notice_marks import _session_notice_is_new

    assert _session_notice_is_new(SimpleNamespace(), "x") is True
    assert _session_notice_is_new(None, "x") is True


def test_the_mark_list_keeps_only_the_newest() -> None:
    from kiro_crew.dashboard.session_notice_marks import (
        SESSION_NOTICE_MARKS,
        _session_notice_is_new,
    )

    chat = SimpleNamespace(_session_notice_marks=[])
    for i in range(SESSION_NOTICE_MARKS + 5):
        assert _session_notice_is_new(chat, f"notice {i}")
    assert len(chat._session_notice_marks) == SESSION_NOTICE_MARKS
    assert _session_notice_is_new(chat, f"notice {SESSION_NOTICE_MARKS + 4}") is False
    assert _session_notice_is_new(chat, "notice 0") is True, "the oldest was dropped"


def test_the_withheld_project_hooks_notice_shows_once_per_chat(
    agents_dir, tmp_path, monkeypatch
) -> None:
    rows: list[str] = []
    monkeypatch.setattr(
        chat_runner,
        "append_and_surface",
        lambda _state, _slot, role, text, *_a, **_k: rows.append(text),
    )
    chat = SimpleNamespace(_session_notice_marks=[])

    def new_session(cwd: Path) -> int:
        rows.clear()
        client = SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_GOOSE), cwd=str(cwd))
        asyncio.run(chat_runner._prepare_spec_hooks(None, chat, client, _AGENT, is_new=True))
        return len(rows)

    first = _repo(tmp_path / "one")
    assert new_session(first) == 1
    assert new_session(first) == 0, "a second session of the same chat repeats it"
    assert new_session(_repo(tmp_path / "two")) == 1, "another folder shows it again"


_ADAPTER_NOTICES = (
    "steer_upgrade_notice",
    "agent_prompt_upgrade_notice",
    "known_limitation_notice",
    "withheld_credentials_notice",
    "kiro_cli_model_notice",
    "engine_fallback_notice",
    "mode_pinned_notice",
    "zero_tool_ban_notice",
)


def _adapter_chat(monkeypatch) -> SimpleNamespace:
    """A chat whose notice rows land in its own window, as ``append_and_surface`` puts them."""
    monkeypatch.setattr(
        chat_runner,
        "append_and_surface",
        lambda _state, slot, role, text, *_a, **_k: slot.messages.append(
            {"role": role, "content": text}
        ),
    )
    return SimpleNamespace(
        _session_notice_marks=[], messages=[], _disk_older_count=0, _regenerate_target=None
    )


def _claude_notice(version: str) -> str:
    from kiro_crew.acp.client_turn_control import untested_version_issue
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE

    return untested_version_issue(ACP_BACKEND_CLAUDE, version)


def test_each_adapter_notice_shows_once_per_chat_until_its_text_changes(monkeypatch) -> None:
    chat = _adapter_chat(monkeypatch)
    client = SimpleNamespace(
        tested_version_notice=_claude_notice("0.97.0"),
        **{name: f"ℹ {name}" for name in _ADAPTER_NOTICES},
    )
    chat_runner._notice_steer_upgrade(None, chat, client)
    assert len(chat.messages) == len(_ADAPTER_NOTICES) + 1
    chat_runner._notice_steer_upgrade(None, chat, client)
    assert len(chat.messages) == len(_ADAPTER_NOTICES) + 1, "a second session repeats them"
    client.tested_version_notice = _claude_notice("0.98.0")
    chat_runner._notice_steer_upgrade(None, chat, client)
    assert chat.messages[-1]["content"] == client.tested_version_notice
    assert len(chat.messages) == len(_ADAPTER_NOTICES) + 2, "another release shows again"


def test_a_marked_adapter_notice_shows_again_once_no_row_shows_it(monkeypatch) -> None:
    chat = _adapter_chat(monkeypatch)
    client = SimpleNamespace(tested_version_notice=_claude_notice("0.97.0"))
    chat_runner._notice_steer_upgrade(None, chat, client)

    chat.messages.clear()
    chat_runner._notice_steer_upgrade(None, chat, client)
    assert len(chat.messages) == 1, "an edit removed the row, so the next session shows it"

    chat._regenerate_target = SimpleNamespace(rows=[chat.messages[0]])
    chat_runner._notice_steer_upgrade(None, chat, client)
    assert len(chat.messages) == 2, "a Regenerate replaces the old row with its reply"

    chat._regenerate_target = None
    chat.messages[:] = [{"role": "user", "content": "hi"}]
    chat._disk_older_count = 40
    chat_runner._notice_steer_upgrade(None, chat, client)
    assert chat.messages == [{"role": "user", "content": "hi"}], "older rows on disk: the mark"


def test_older_rows_on_disk_do_not_hide_a_notice_a_regenerate_retires(monkeypatch) -> None:
    chat = _adapter_chat(monkeypatch)
    client = SimpleNamespace(tested_version_notice=_claude_notice("0.97.0"))
    chat_runner._notice_steer_upgrade(None, chat, client)

    chat._disk_older_count = 40
    chat._regenerate_target = SimpleNamespace(rows=[chat.messages[0]])
    chat_runner._notice_steer_upgrade(None, chat, client)
    assert len(chat.messages) == 2, "the window's only row of it is being retired"


def test_an_adapter_notice_shows_again_for_another_folder(monkeypatch) -> None:
    chat = _adapter_chat(monkeypatch)
    pinned = "ℹ This chat runs in Default mode. This folder has a .claude/settings.local.json."
    client = SimpleNamespace(mode_pinned_notice=pinned, cwd="/work/a")
    chat_runner._notice_steer_upgrade(None, chat, client)
    chat_runner._notice_steer_upgrade(None, chat, client)
    assert len(chat.messages) == 1
    client.cwd = "/work/b"
    chat_runner._notice_steer_upgrade(None, chat, client)
    assert len(chat.messages) == 2, "a workspace switch to a folder with the same problem"
