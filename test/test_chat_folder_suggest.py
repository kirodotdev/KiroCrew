"""Tests for the post-titling folder suggestion (chat_folder_suggest)."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew.dashboard import chat_folder_suggest as fs

# ── _parse_choice ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "reply,count,expected",
    [
        ("2", 3, 1),
        ("  2  ", 3, 1),
        ("#2", 3, 1),
        ("2.", 3, 1),
        ("2) Kiro Crew", 3, 1),
        ("2\nbecause it is about i18n", 3, 1),
        ("1", 1, 0),
        # NONE is the explicit escape and must win over anything after it.
        ("NONE", 3, None),
        ("none", 3, None),
        ("NONE — nothing fits", 3, None),
        # Out of range in either direction is a miss, not a clamp: guessing
        # would file the session in an arbitrary folder.
        ("0", 3, None),
        ("4", 3, None),
        ("99", 3, None),
        # Anything unparseable stays silent.
        ("", 3, None),
        ("   ", 3, None),
        ("the second one", 3, None),
        ("Kiro Crew", 3, None),
    ],
)
def test_parse_choice(reply: str, count: int, expected: int | None) -> None:
    assert fs._parse_choice(reply, count) == expected


def test_parse_choice_ignores_number_after_first_line() -> None:
    """A leading prose line does not get rescued by a number further down.

    The prompt asks for the number FIRST; honouring a number on line 3 would
    make "I think none of these fit, but if forced, 2" resolve to folder 2.
    """
    assert fs._parse_choice("Let me think.\n\n2", 3) is None


# ── _eligible_folders ───────────────────────────────────────────────────────


def _state(folders: list[dict], slots: dict | None = None, log: object = None) -> object:
    return SimpleNamespace(
        _folders=folders,
        _slots=slots or {},
        conversation_log=log,
        folder_breadcrumb=lambda fid, sep=" › ": next(
            (f["name"] for f in folders if f.get("id") == fid), ""
        ),
    )


def test_eligible_folders_orders_by_sidebar_order() -> None:
    state = _state(
        [
            {"id": "b", "name": "Beta", "order": 2},
            {"id": "a", "name": "Alpha", "order": 1},
        ]
    )
    assert [f["id"] for f in fs._eligible_folders(state)] == ["a", "b"]


def test_eligible_folders_skips_hidden_unnamed_and_idless() -> None:
    """Hidden folders are excluded, and malformed entries cannot crash the walk.

    ``load_folders`` does no validation, so a hand-edited folders.json can hold a
    dict with no ``id`` — the same tolerance ``folder_breadcrumb`` documents.
    """
    state = _state(
        [
            {"id": "keep", "name": "Keep", "order": 0},
            {"id": "hid", "name": "Hidden", "order": 1, "hidden": True},
            {"id": "blank", "name": "   ", "order": 2},
            {"name": "No id", "order": 3},
            "not-a-dict",
        ]
    )
    assert [f["id"] for f in fs._eligible_folders(state)] == ["keep"]


def test_eligible_folders_caps_the_list() -> None:
    state = _state([{"id": f"f{i}", "name": f"F{i}", "order": i} for i in range(200)])
    assert len(fs._eligible_folders(state)) == fs._MAX_FOLDERS


# ── grounding samples ───────────────────────────────────────────────────────


def test_folder_sample_titles_caps_per_folder_and_keeps_newest() -> None:
    """list_sessions() is newest-first, so the cap keeps the most recent titles."""
    sessions = [{"folder_id": "a", "title": f"T{i}"} for i in range(10)]
    state = _state([], log=SimpleNamespace(list_sessions=lambda: sessions))
    samples = fs._folder_sample_titles(state)
    assert samples["a"] == [f"T{i}" for i in range(fs._MAX_SAMPLES_PER_FOLDER)]


def test_folder_sample_titles_ignores_unfiled_and_untitled() -> None:
    sessions = [
        {"folder_id": "", "title": "Unfiled"},
        {"title": "No folder key"},
        {"folder_id": "a", "title": "   "},
        {"folder_id": "a", "title": "Real"},
    ]
    state = _state([], log=SimpleNamespace(list_sessions=lambda: sessions))
    assert fs._folder_sample_titles(state) == {"a": ["Real"]}


def test_folder_sample_titles_without_conversation_log() -> None:
    assert fs._folder_sample_titles(_state([], log=None)) == {}


@pytest.mark.parametrize("mode", sorted(fs.INCOGNITO_MEMORY_MODES))
def test_archived_scan_excludes_private_sessions(mode: str) -> None:
    """A filed temporary/incognito session's title must never ground the prompt.

    INCOGNITO_MEMORY_MODES is documented as "never
    searchable/listable/summarizable"; sampling a title to ground a folder pick
    ships it to a remote model, which is exactly that. A temporary session CAN be
    filed — api_chat_slot_folder does not gate on memory_mode — so the folder
    filter alone would not stop it.
    """
    sessions = [
        {"folder_id": "a", "title": "SECRET private topic", "memory_mode": mode},
        {"folder_id": "a", "title": "Public topic", "memory_mode": "persistent"},
    ]
    state = _state([], log=SimpleNamespace(list_sessions=lambda: sessions))
    assert fs._folder_sample_titles(state) == {"a": ["Public topic"]}


def test_archived_scan_treats_missing_memory_mode_as_persistent() -> None:
    """Old sessions predate the marker; excluding them would empty the grounding."""
    sessions = [{"folder_id": "a", "title": "Legacy"}]
    state = _state([], log=SimpleNamespace(list_sessions=lambda: sessions))
    assert fs._folder_sample_titles(state) == {"a": ["Legacy"]}


@pytest.mark.parametrize("mode", sorted(fs.INCOGNITO_MEMORY_MODES))
def test_live_slot_titles_exclude_private_slots(mode: str) -> None:
    """A LIVE private slot can be filed too — same rule as the archived scan."""
    slots = {
        "me": SimpleNamespace(key="me", folder_id="a", title="Mine", memory_mode="persistent"),
        "priv": SimpleNamespace(key="priv", folder_id="a", title="SECRET", memory_mode=mode),
        "pub": SimpleNamespace(key="pub", folder_id="a", title="Sibling", memory_mode="persistent"),
    }
    titles = fs._live_slot_titles(_state([], slots=slots), exclude_key="me")
    assert titles == {"a": ["Sibling"]}


def test_merge_samples_prefers_live_and_dedupes() -> None:
    merged = fs._merge_samples(
        {"a": ["Live one"]},
        {"a": ["live ONE", "Archived"], "b": ["Only archived"]},
    )
    assert merged["a"] == ["Live one", "Archived"]
    assert merged["b"] == ["Only archived"]


def test_live_slot_titles_excludes_self_and_unfiled() -> None:
    slots = {
        "me": SimpleNamespace(key="me", folder_id="a", title="Mine", memory_mode="persistent"),
        "sib": SimpleNamespace(key="sib", folder_id="a", title="Sibling", memory_mode="persistent"),
        "loose": SimpleNamespace(key="loose", folder_id="", title="Loose", memory_mode="persistent"),
    }
    titles = fs._live_slot_titles(_state([], slots=slots), exclude_key="me")
    assert titles == {"a": ["Sibling"]}


# ── project_dir shortcut ────────────────────────────────────────────────────


def test_match_by_project_dir_unique_hit(tmp_path) -> None:
    folders = [
        {"id": "a", "name": "A", "project_dir": str(tmp_path)},
        {"id": "b", "name": "B", "project_dir": ""},
    ]
    assert fs._match_by_project_dir(folders, str(tmp_path))["id"] == "a"


def test_match_by_project_dir_ambiguous_defers_to_model(tmp_path) -> None:
    """Two folders on the same repo cannot be decided by the directory."""
    folders = [
        {"id": "a", "name": "A", "project_dir": str(tmp_path)},
        {"id": "b", "name": "B", "project_dir": str(tmp_path)},
    ]
    assert fs._match_by_project_dir(folders, str(tmp_path)) is None


def test_match_by_project_dir_no_project() -> None:
    assert fs._match_by_project_dir([{"id": "a", "name": "A", "project_dir": "/x"}], "") is None


def test_project_dir_match_runs_off_the_loop_thread(tmp_path) -> None:
    """``realpath`` is blocking and a folder can live on a network mount.

    Resolving it on the loop thread would stall every chat, WS push and heartbeat
    behind one unresponsive mount, so the match must be handed to an executor.
    The assertion is the thread identity, not the call graph: a future refactor
    that inlines the syscall back onto the loop fails here.
    """
    folders = [{"id": "a", "name": "A", "project_dir": str(tmp_path)}]
    loop_thread: list[int] = []
    ran_on: list[int] = []
    real_normalize = fs._normalize_dir

    def spy(raw: str) -> str:
        ran_on.append(threading.get_ident())
        return real_normalize(raw)

    async def go():
        loop_thread.append(threading.get_ident())
        with patch.object(fs, "_normalize_dir", spy):
            return await fs._match_by_project_dir_off_loop(folders, str(tmp_path))

    assert asyncio.run(go())["id"] == "a"
    assert ran_on, "the matcher never resolved a directory"
    assert all(t != loop_thread[0] for t in ran_on)


def test_project_dir_match_off_loop_swallows_executor_failure(tmp_path) -> None:
    """A failed match must degrade to 'let the model decide', not raise."""

    def boom(_folders, _project):
        raise OSError("mount gone")

    async def go():
        with patch.object(fs, "_match_by_project_dir", boom):
            return await fs._match_by_project_dir_off_loop([], str(tmp_path))

    assert asyncio.run(go()) is None


# ── prompt shape ────────────────────────────────────────────────────────────


def test_build_prompt_numbers_folders_and_marks_empty_ones() -> None:
    prompt = fs._build_prompt(
        title="Fix the render gate flake",
        message="the artifacts surface keeps failing",
        labels=["Kiro Crew › i18n", "Errands"],
        samples=[["Pseudolocale gate", "Catalog sync"], []],
    )
    assert "1. Kiro Crew › i18n — contains: \"Pseudolocale gate\"; \"Catalog sync\"" in prompt
    assert "2. Errands — (no sessions yet)" in prompt
    assert "Fix the render gate flake" in prompt
    # The NONE escape must always be offered, or the model is forced to guess.
    assert "NONE" in prompt


# ── maybe_suggest_folder ────────────────────────────────────────────────────


class _Slot:
    """Minimal stand-in for the _ChatSlot fields the suggester reads."""

    def __init__(self, **kw):
        self.key = kw.get("key", "dashboard_chat-1")
        self.folder_id = kw.get("folder_id", "")
        self._folder_suggested = kw.get("suggested", False)
        self.memory_mode = kw.get("memory_mode", "persistent")
        self.title = kw.get("title", "Fix the render gate flake")
        self.project = kw.get("project", "")
        self.messages = kw.get("messages", [{"role": "user", "content": "the gate keeps failing"}])

    @property
    def blocks_reads(self) -> bool:
        return self.memory_mode == "temporary"


class _Recorder:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def deliver_ws_owners(self, msg_type, data):
        self.sent.append((msg_type, data))
        return 1


def _suggest_state(folders, slot, *, log=None):
    rec = _Recorder()
    state = SimpleNamespace(
        _folders=folders,
        _slots={slot.key: slot},
        conversation_log=log,
        sessions=object(),
        folder_breadcrumb=lambda fid, sep=" › ": next(
            (f["name"] for f in folders if f.get("id") == fid), ""
        ),
        deliver_ws_owners=rec.deliver_ws_owners,
    )
    return state, rec


def _run(state, slot, *, reply="1", enabled=True):
    cfg = SimpleNamespace(dashboard=SimpleNamespace(folder_suggestions_enabled=enabled))
    calls: list[str] = []

    async def fake_oneliner(_sessions, prompt, **_kw):
        calls.append(prompt)
        return reply

    with (
        patch.object(fs.KiroCrewConfig, "load", staticmethod(lambda: cfg)),
        patch.object(fs, "run_bg_oneliner", fake_oneliner),
    ):
        asyncio.run(fs.maybe_suggest_folder(state, slot))
    return calls


_FOLDERS = [{"id": "f1", "name": "Kiro Crew", "order": 0}]


def test_suggests_and_delivers_to_owner_sockets() -> None:
    slot = _Slot()
    state, rec = _suggest_state(_FOLDERS, slot)
    calls = _run(state, slot, reply="1")
    assert len(calls) == 1
    assert [t for t, _ in rec.sent] == ["slot_folder_suggestion"]
    payload = rec.sent[0][1]
    assert payload["slot"] == slot.key
    assert payload["folder_id"] == "f1"
    assert payload["folder_name"] == "Kiro Crew"
    assert isinstance(payload["ts"], float)


def test_none_reply_delivers_nothing() -> None:
    slot = _Slot()
    state, rec = _suggest_state(_FOLDERS, slot)
    _run(state, slot, reply="NONE")
    assert rec.sent == []
    # The one shot is still spent: a model that saw the whole transcript and
    # declined must not be re-asked on the next turn.
    assert slot._folder_suggested is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"folder_id": "already"},   # already filed
        {"suggested": True},        # one shot already spent
        {"memory_mode": "temporary"},  # blank-slate session
    ],
)
def test_guards_skip_before_any_model_call(kwargs: dict) -> None:
    slot = _Slot(**kwargs)
    state, rec = _suggest_state(_FOLDERS, slot)
    assert _run(state, slot) == []
    assert rec.sent == []


def test_no_folders_means_no_call() -> None:
    slot = _Slot()
    state, rec = _suggest_state([], slot)
    assert _run(state, slot) == []
    assert rec.sent == []
    # Nothing was offered, so the slot keeps its shot for a later titling.
    assert slot._folder_suggested is False


def test_disabled_by_config() -> None:
    slot = _Slot()
    state, rec = _suggest_state(_FOLDERS, slot)
    assert _run(state, slot, enabled=False) == []
    assert rec.sent == []
    assert slot._folder_suggested is False


def test_project_dir_match_skips_the_model(tmp_path) -> None:
    folders = [{"id": "f1", "name": "Repo", "order": 0, "project_dir": str(tmp_path)}]
    slot = _Slot(project=str(tmp_path))
    state, rec = _suggest_state(folders, slot)
    assert _run(state, slot) == []  # no prompt built at all
    assert rec.sent[0][1]["folder_id"] == "f1"


def test_manual_filing_during_the_model_call_wins() -> None:
    """The user filing the session by hand mid-call must not be overridden."""
    slot = _Slot()
    state, rec = _suggest_state(_FOLDERS, slot)
    cfg = SimpleNamespace(dashboard=SimpleNamespace(folder_suggestions_enabled=True))

    async def racing_oneliner(_sessions, _prompt, **_kw):
        slot.folder_id = "picked-by-hand"
        return "1"

    with (
        patch.object(fs.KiroCrewConfig, "load", staticmethod(lambda: cfg)),
        patch.object(fs, "run_bg_oneliner", racing_oneliner),
    ):
        asyncio.run(fs.maybe_suggest_folder(state, slot))
    assert rec.sent == []


def test_rejected_reply_is_never_echoed_into_the_logs(caplog) -> None:
    """The dashboard log ring is streamed over /api/ws, which an App Kit
    credential can subscribe to. A prompt-injected model reply must therefore
    never reach a log record — only its shape may be logged.
    """
    secret = "AKIAIOSFODNN7EXAMPLE totally-not-a-folder-number"
    slot = _Slot()
    state, rec = _suggest_state(_FOLDERS, slot)
    with caplog.at_level("DEBUG", logger=fs.logger.name):
        _run(state, slot, reply=secret)
    assert rec.sent == []
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "AKIA" not in blob
    assert "totally-not-a-folder-number" not in blob
    # The diagnostic shape survives, so "why no pick" is still debuggable.
    assert "no confident pick" in blob


def test_model_failure_is_silent_and_not_retried() -> None:
    slot = _Slot()
    state, rec = _suggest_state(_FOLDERS, slot)
    cfg = SimpleNamespace(dashboard=SimpleNamespace(folder_suggestions_enabled=True))

    async def boom(_sessions, _prompt, **_kw):
        raise RuntimeError("model unavailable")

    with (
        patch.object(fs.KiroCrewConfig, "load", staticmethod(lambda: cfg)),
        patch.object(fs, "run_bg_oneliner", boom),
    ):
        asyncio.run(fs.maybe_suggest_folder(state, slot))
    assert rec.sent == []
    assert slot._folder_suggested is True
