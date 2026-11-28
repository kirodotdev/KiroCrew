"""The in-band card ``js``, ``cdp`` and ``http_get`` wait on (``browser_card_ask``).

The default agent spec grants the whole ``@kirocrew-core`` server, so the host
raises no permission request for a ``browser`` call. Pinned here: on that spec the
op route itself puts the card up and runs the step only on an Allow for that very
call, one answer per call, and refuses where nobody can answer.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from test_browser_ops_route import _install, _no_stream, _Supervisor

from kiro_crew.browser_harness import live_hub as hub_mod
from kiro_crew.dashboard.handlers import browser_card_ask as card
from kiro_crew.dashboard.handlers import browser_ops as mod

_DEFAULTS = Path(__file__).resolve().parents[1] / "src/kiro_crew/config/defaults.json"
_SESSION = "dashboard:chat-1"
_CDP = {"method": "Storage.getCookies"}


@pytest.fixture(autouse=True)
def _quiet(_floor_monkeypatch: pytest.MonkeyPatch) -> Any:
    _floor_monkeypatch.setattr(mod, "_sel", lambda: MagicMock())
    hub_mod.reset_live_hub()
    yield
    hub_mod.reset_live_hub()


class _State:
    """The dashboard state's agent-ask half: one slot, asks kept as the coordinator keeps them."""

    def __init__(self, *, clients: int = 1) -> None:
        self._slots: dict[str, object] = {"chat-1": object()}
        self._agent_asks: dict[str, dict[str, Any]] = {}
        self.clients = clients
        self.opened: list[list[dict[str, Any]]] = []

    async def open_agent_ask(
        self, ask_id: str, slot_key: str, session_key: str, questions: list[dict]
    ) -> int:
        self.opened.append(questions)
        if not self.clients:
            return 0
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._agent_asks[ask_id] = {
            "session_key": session_key,
            "slot": slot_key,
            "questions": questions,
            "future": future,
            "reason": None,
        }
        return self.clients

    def answer(self, ask_id: str, label: str | None) -> None:
        ask = self._agent_asks[ask_id]
        ask["reason"] = "answered" if label else "dismissed"
        question = ask["questions"][0]["question"]
        ask["future"].set_result({question: label} if label else None)


def _raw(sup: _Supervisor) -> list[str]:
    return [h for h, _kw in sup.calls if h == "cdp"]


async def _run(state: Any, args: dict[str, Any], approval: str = "") -> dict[str, Any]:
    answer, _status = await mod.run_browser_op(
        _SESSION, "cdp", args, state=state, approval=approval
    )
    return answer


def test_the_default_agent_spec_raises_no_host_card_for_the_browser_tool() -> None:
    # Why the card cannot come from the host's permission request on this spec.
    allowed = json.loads(_DEFAULTS.read_text(encoding="utf-8"))["allowedTools"]
    assert "@kirocrew-core" in allowed


@pytest.mark.asyncio
async def test_on_the_default_spec_cdp_waits_on_a_card_and_runs_only_on_allow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _Supervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    state = _State()
    first = await _run(state, _CDP)
    assert first["code"] == "needs_approval" and _raw(sup) == []
    question = state.opened[0][0]
    assert question["question"] == "cdp Storage.getCookies {}"
    assert [o["label"] for o in question["options"]] == [card.ALLOW, card.DENY]
    assert "every site's cookies" in question["options"][0]["description"]
    state.answer(first["ask_id"], card.ALLOW)
    ran = await _run(state, _CDP, approval=first["ask_id"])
    assert ran["ok"] is True and _raw(sup) == ["cdp"]
    # One answer runs one call.
    again = await _run(state, _CDP, approval=first["ask_id"])
    assert again["code"] == "approval_mismatch" and _raw(sup) == ["cdp"]


@pytest.mark.asyncio
async def test_a_deny_or_a_dismiss_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    sup = _Supervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    state = _State()
    for label in (card.DENY, None):
        opened = await _run(state, _CDP)
        state.answer(opened["ask_id"], label)
        assert (await _run(state, _CDP, approval=opened["ask_id"]))["code"] == "not_allowed"
    assert _raw(sup) == []


@pytest.mark.asyncio
async def test_an_allow_runs_only_the_call_it_was_opened_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _Supervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    state = _State()
    opened = await _run(state, {"method": "Runtime.evaluate"})
    state.answer(opened["ask_id"], card.ALLOW)
    swapped = await _run(state, _CDP, approval=opened["ask_id"])
    assert swapped["code"] == "approval_mismatch" and _raw(sup) == []
    other = await mod.run_browser_op(
        "dashboard:chat-2",
        "cdp",
        {"method": "Runtime.evaluate"},
        state=state,
        approval=opened["ask_id"],
    )
    assert other[0]["code"] == "approval_mismatch" and _raw(sup) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session", "clients"), [("dashboard:chat-1", 0), ("slack:123", 1), ("dashboard:gone", 1)]
)
async def test_where_nobody_can_answer_it_is_refused(
    monkeypatch: pytest.MonkeyPatch, session: str, clients: int
) -> None:
    sup = _Supervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    answer, _ = await mod.run_browser_op(session, "cdp", _CDP, state=_State(clients=clients))
    assert answer["code"] == "needs_person" and _raw(sup) == []


def test_the_card_shows_the_whole_call() -> None:
    cdp = card.card_questions(
        "cdp", {"method": "Runtime.evaluate", "params": {"expression": "1+1"}}
    )
    assert cdp[0]["question"] == 'cdp Runtime.evaluate {"expression": "1+1"}'
    fetch = card.card_questions(
        "http_get", {"url": "https://h.example/p?d=1", "headers": {"Accept": "text/html"}}
    )
    assert fetch[0]["question"] == "http_get https://h.example/p?d=1\nAccept: text/html"
    assert card.card_questions("js", {"expression": "document.title"})[0]["question"] == (
        "js document.title"
    )
    framed = card.card_questions("js", {"expression": "1", "target_id": "F-pay"})
    assert framed[0]["question"] == "js (in tab or frame F-pay) 1"
    files = card.card_questions("upload", {"ref": "e4", "path": ["/home/u/a.pdf", "/home/u/b.png"]})
    assert files[0]["question"] == "upload into e4:\n/home/u/a.pdf\n/home/u/b.png"


@pytest.mark.asyncio
async def test_a_call_too_long_to_show_whole_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    sup = _Supervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    state = _State()
    args = {"method": "Runtime.evaluate", "params": {"expression": "x" * card.SHOWN_MAX}}
    assert (await _run(state, args))["code"] == "too_long_for_card"
    assert state.opened == [] and _raw(sup) == []


@pytest.mark.asyncio
async def test_on_the_default_spec_an_upload_waits_on_its_card(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sup = _Supervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    state = _State()
    (tmp_path / "a.pdf").write_bytes(b"pdf")
    args = {"ref": "e4", "path": str(tmp_path / "a.pdf")}
    answer, _ = await mod.run_browser_op(_SESSION, "upload", args, state=state)
    assert answer["code"] == "needs_approval"
    assert str(tmp_path / "a.pdf") in state.opened[0][0]["question"]
    assert not [h for h, _kw in sup.calls if h == "set_files"]


@pytest.mark.asyncio
async def test_any_other_op_runs_with_no_card(monkeypatch: pytest.MonkeyPatch) -> None:
    sup = _Supervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    state = _State()
    answer, _ = await mod.run_browser_op(_SESSION, "snapshot", {}, state=state)
    assert answer["ok"] is True and state.opened == []


# --- the browser tool's half: park on the card, post again on an answer ---------


def _shim(monkeypatch: pytest.MonkeyPatch, wait_replies: list[dict[str, Any]]) -> list[Any]:
    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools import browser as tool

    posts: list[Any] = []

    def _command(key: str, op: str, args: dict, header: str, timeout: float, approval: str = ""):
        posts.append((op, args, approval))
        if not approval:
            return 200, {"ok": False, "code": "needs_approval", "ask_id": "a" * 32}
        return 200, {"ok": True, "result": {"ok": True, "result": "[]"}}

    def _post(path: str, body: dict, **_kw: Any) -> Any:
        if path.endswith("/wait"):
            return wait_replies.pop(0)
        return {}

    monkeypatch.setattr(tool, "_post_command", _command)
    monkeypatch.setattr(mcp_core, "_post", _post)
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: _SESSION)
    monkeypatch.setattr(mcp_core, "_vet_browse_governance", lambda _k: "")
    monkeypatch.setattr(mcp_core, "_session_key_header_error", lambda _k: "")
    return posts


def _drive(parked: Any) -> str:
    for _ in range(10):
        text = parked.step()
        if text is not None:
            return text
    raise AssertionError("the parked call never ended")


def test_the_tool_parks_on_the_card_and_posts_again_with_its_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.mcp_tools import browser as tool

    posts = _shim(monkeypatch, [{"status": "pending"}, {"status": "answered"}])
    parked = tool.browser("browser", {"op": "cdp", "args": _CDP})
    assert isinstance(parked, tool._CardWait)
    assert "Error" not in _drive(parked)
    assert posts == [("cdp", _CDP, ""), ("cdp", _CDP, "a" * 32)]


def test_a_card_that_ends_unanswered_posts_nothing_more(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.mcp_tools import browser as tool

    posts = _shim(monkeypatch, [{"status": "dismissed"}])
    parked = tool.browser("browser", {"op": "cdp", "args": _CDP})
    assert "not_allowed" in _drive(parked)
    assert posts == [("cdp", _CDP, "")]
