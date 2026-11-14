"""A chat shows each session-start spec notice once, not once per backend session.

A reconnect, a reload and a gateway restart each start a new backend session, and
the notices ``_prepare_spec_hooks`` adds there (a withheld project hook, a key
nothing carries, a too-long hook command, ...) describe the agent's spec, which
the new session did not change. The chat keeps a mark per notice it showed and
adds the row again only when the text or the folder it is about differs.
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
