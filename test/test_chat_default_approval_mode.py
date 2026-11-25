"""The owner's start mode for a plain New chat (``dashboard.default_approval_mode``).

``POST /api/chat/slots`` seeds the machine owner's plain create that asks for it
(``start_mode_default: true``, sent by the browser's New chat entry points) through
``seed_slot_approval`` with the stored value, so the chat opens on Reads with the
``mode_change:trust_reads`` record under ``chat_default_mode``. Every other creation
path keeps the Normal it starts on today, a ceiling that denies Reads leaves the chat
on Normal and says so in the reply, and a declared YOLO grant is never touched.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.loader_builders import _build_dashboard_config
from kiro_crew.dashboard import chat_default_mode, chat_handlers
from kiro_crew.testing.wait import async_wait_until

#: What the browser's New chat entry points send.
NEW_CHAT = {"start_mode_default": True}


class _SelRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def log_api_access(self, **kw: Any) -> None:
        self.calls.append(kw)

    def mode_changes(self) -> list[tuple[str, str]]:
        return [
            (c["caller"], c["operation"])
            for c in self.calls
            if str(c.get("operation", "")).startswith("mode_change:")
        ]


@pytest.fixture
def sel(_floor_monkeypatch: pytest.MonkeyPatch) -> _SelRecorder:
    import kiro_crew.sel as sel_mod

    recorder = _SelRecorder()
    _floor_monkeypatch.setattr(sel_mod, "sel", lambda: recorder)
    return recorder


@pytest.fixture
def cfg() -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.dashboard.default_approval_mode = "trust_reads"
    return cfg


@pytest.fixture
def state(tmp_path, cfg, _floor_monkeypatch: pytest.MonkeyPatch) -> Any:
    from kiro_crew import approval_mode_policy

    state = _make_state(tmp_path)
    _floor_monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    _floor_monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *a, **k: None)
    _floor_monkeypatch.setattr(approval_mode_policy, "session_mode_permitted", lambda mode: True)
    return state


async def _create(state: Any, body: dict, headers: dict | None = None) -> tuple[int, dict]:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post("/api/chat/slots", json=body, headers=headers or {})
        status, payload = response.status, await response.json()
        await async_wait_until(
            lambda: state._slots_broadcast_timer is None,
            describe=lambda: "a trailing slots push is still armed",
        )
    return status, payload


def _born(state: Any, before: set[str]) -> Any:
    (key,) = set(state._slots) - before
    return state._slots[key]


@pytest.mark.asyncio
async def test_the_owners_plain_new_chat_starts_on_reads(state, sel) -> None:
    before = set(state._slots)

    status, payload = await _create(state, NEW_CHAT)

    assert status == 200
    slot = _born(state, before)
    assert slot._trust_reads is True
    assert slot._trust is False
    assert "default_mode_refused" not in payload
    # The reply's row is what the composer's picker reads its mode from.
    assert payload["trust_reads"] is True
    assert sel.mode_changes() == [("chat_default_mode", "mode_change:trust_reads")]


@pytest.mark.asyncio
async def test_normal_seeds_nothing(state, cfg, sel) -> None:
    cfg.dashboard.default_approval_mode = "normal"
    before = set(state._slots)

    assert (await _create(state, NEW_CHAT))[0] == 200

    assert _born(state, before)._trust_reads is False
    assert sel.mode_changes() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="unmarked-create-such-as-an-app-page"),
        pytest.param({**NEW_CHAT, "name": "reopened"}, id="named-create"),
        pytest.param({**NEW_CHAT, "ephemeral": True}, id="ephemeral-helper"),
        pytest.param({**NEW_CHAT, "embed_app": "notes"}, id="app-page-chat"),
    ],
)
async def test_a_create_that_is_not_a_plain_new_chat_keeps_normal(state, sel, body) -> None:
    before = set(state._slots)

    assert (await _create(state, body))[0] == 200

    assert _born(state, before)._trust_reads is False
    assert sel.mode_changes() == []


@pytest.mark.asyncio
async def test_a_teammates_new_chat_keeps_normal(state, sel, _floor_monkeypatch) -> None:
    from kiro_crew.shared_crew import teammate_access

    _floor_monkeypatch.setattr(teammate_access, "teammate_passes_owner_gate", lambda r: True)
    before = set(state._slots)

    status, _ = await _create(state, NEW_CHAT, headers={"X-Test-User": "kiro-user.teammate-ana"})

    assert status == 200
    assert _born(state, before)._trust_reads is False
    assert sel.mode_changes() == []


@pytest.mark.asyncio
async def test_a_crewmates_chat_keeps_its_profile_seed(state, sel, _floor_monkeypatch) -> None:
    from kiro_crew.dashboard import crewmate_approval

    _floor_monkeypatch.setattr(crewmate_approval, "crewmate_name_for", lambda c, e: "mate")
    before = set(state._slots)

    assert (await _create(state, NEW_CHAT))[0] == 200

    slot = _born(state, before)
    assert slot._trust_reads is False
    # The Profile's seed at the first turn still finds an untouched slot.
    assert slot._member_approval_seeded is False


@pytest.mark.asyncio
async def test_a_ceiling_that_denies_reads_leaves_normal_and_says_why(
    state, sel, _floor_monkeypatch
) -> None:
    from kiro_crew import approval_mode_policy

    _floor_monkeypatch.setattr(
        approval_mode_policy, "session_mode_permitted", lambda mode: mode == "normal"
    )
    before = set(state._slots)

    status, payload = await _create(state, NEW_CHAT)

    assert status == 200
    assert _born(state, before)._trust_reads is False
    assert payload["default_mode_refused"] == "policy"
    assert sel.mode_changes() == []


@pytest.mark.asyncio
async def test_opening_a_chat_never_touches_a_yolo_grant(state, sel, _floor_monkeypatch) -> None:
    touched: list[str] = []
    _floor_monkeypatch.setattr(
        chat_handlers,
        "safety_override",
        lambda: SimpleNamespace(
            is_declared=True,
            is_active=lambda: True,
            deactivate=lambda *_a: touched.append("deactivate"),
            activate=lambda *_a: touched.append("activate"),
        ),
    )
    before = set(state._slots)

    assert (await _create(state, NEW_CHAT))[0] == 200

    assert _born(state, before)._trust_reads is True
    assert touched == []


@pytest.mark.parametrize(
    "raw, want",
    [
        ("trust_reads", "trust_reads"),
        ("normal", "normal"),
        ("trust", "normal"),
        ("yolo", "normal"),
        (1, "normal"),
        (None, "normal"),
    ],
)
def test_the_stored_value_reads_as_normal_unless_it_is_reads(raw: object, want: str) -> None:
    built = _build_dashboard_config(set(), {"default_approval_mode": raw})
    assert built.default_approval_mode == want
    assert chat_default_mode.configured_mode(SimpleNamespace(dashboard=built)) == want


def test_an_absent_key_reads_as_normal() -> None:
    assert _build_dashboard_config(set(), {}).default_approval_mode == "normal"
    assert chat_default_mode.configured_mode(SimpleNamespace()) == "normal"
