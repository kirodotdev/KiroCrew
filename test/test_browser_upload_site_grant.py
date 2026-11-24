"""A person's "act" site grant covers ``upload`` on that host (``browser_card_ask``).

Pinned: an act grant on the chat's current host lets ``upload`` run with no card,
and the op route still checks the bound tab's real host first; a read grant, an
act grant on another host, and ``js``/``cdp``/``http_get`` keep the card.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest
from test_browser_card_ask import _State
from test_browser_ops_route import _install, _no_stream, _Supervisor

from kiro_crew import browser_host_trust as bht
from kiro_crew.browser_harness import live_hub as hub_mod
from kiro_crew.dashboard.handlers import browser_card_ask as card
from kiro_crew.dashboard.handlers import browser_ops as mod

_SESSION = "dashboard:chat-1"
_UPLOAD = {"ref": "e4", "path": "/home/u/a.pdf"}


#: A file in a folder the strict sandbox hides; not a sensitive path, so a card sends it.
_HIDDEN = "~/.docker/u2-fixture.json"


@pytest.fixture(autouse=True)
def _files(tmp_path: Any, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
    # The card expands each entry as the op would, so the files must be real.
    home = tmp_path / "home"
    (home / ".docker").mkdir(parents=True)
    (home / ".docker" / "u2-fixture.json").write_bytes(b"{}")
    (home / "a.pdf").write_bytes(b"pdf")
    _floor_monkeypatch.setenv("HOME", str(home))
    _floor_monkeypatch.setenv("USERPROFILE", str(home))
    _floor_monkeypatch.setitem(_UPLOAD, "path", str(home / "a.pdf"))


@pytest.fixture(autouse=True)
def _quiet(_floor_monkeypatch: pytest.MonkeyPatch) -> Any:
    from unittest.mock import MagicMock

    _floor_monkeypatch.setattr(mod, "_sel", lambda: MagicMock())
    _floor_monkeypatch.setattr(bht, "_pending", {})
    hub_mod.reset_live_hub()
    yield
    hub_mod.reset_live_hub()


def _granted(host: str, cls: str) -> _State:
    state = _State()
    pattern = bht.grant_pattern(bht.trust_key(host, cls))
    state._slots["chat-1"] = SimpleNamespace(
        _trusted_patterns={pattern}, _trusted_pattern_deadlines={}
    )
    return state


def _on_page(url: str) -> None:
    hub_mod.get_live_hub()._session(bht.browser_session_key(_SESSION)).url = url


@pytest.mark.asyncio
async def test_an_act_grant_on_the_host_lets_upload_run_with_no_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _Supervisor()
    sup.tab = {"targetId": "T1", "url": "https://app.example/form", "title": "t"}
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    state = _granted("app.example", "act")
    _on_page("https://app.example/form")
    answer, _ = await mod.run_browser_op(_SESSION, "upload", _UPLOAD, state=state)
    assert state.opened == []
    assert answer.get("code") not in ("needs_approval", bht.SCOPE_CHANGED_CODE)
    # The route took the note and checked the real tab before the op ran.
    assert ("current_tab", {}) in sup.calls
    assert bht._pending == {}


@pytest.mark.asyncio
async def test_a_tab_that_left_the_granted_host_gets_no_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _Supervisor()
    sup.tab = {"targetId": "T1", "url": "https://evil.example/", "title": "t"}
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    state = _granted("app.example", "act")
    _on_page("https://app.example/form")
    answer, _ = await mod.run_browser_op(_SESSION, "upload", _UPLOAD, state=state)
    assert answer["code"] == bht.SCOPE_CHANGED_CODE and state.opened == []
    assert not [h for h, _kw in sup.calls if h in ("set_files", "chooser_files", "drag")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("host", "cls", "op", "args"),
    [
        ("app.example", "read", "upload", _UPLOAD),
        ("other.example", "act", "upload", _UPLOAD),
        ("app.example", "act", "js", {"expression": "1"}),
        ("app.example", "act", "cdp", {"method": "Page.reload"}),
        ("app.example", "act", "http_get", {"url": "https://app.example/"}),
    ],
)
async def test_the_card_stays_without_an_act_grant_on_this_host(
    monkeypatch: pytest.MonkeyPatch, host: str, cls: str, op: str, args: dict[str, Any]
) -> None:
    sup = _Supervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    state = _granted(host, cls)
    _on_page("https://app.example/form")
    answer, _ = await mod.run_browser_op(_SESSION, op, args, state=state)
    assert answer["code"] == "needs_approval" and len(state.opened) == 1


def test_the_grant_check_keeps_no_state_and_skips_an_app_slot() -> None:
    state = _granted("app.example", "act")
    _on_page("https://app.example/form")
    assert card.upload_grant_host(state, _SESSION, "upload") == "app.example"
    assert card.upload_grant_host(state, _SESSION, "js") == ""
    assert bht._pending == {}
    state._slots["chat-1"]._app = "someapp"
    assert card.upload_grant_host(state, _SESSION, "upload") == ""


@pytest.mark.asyncio
async def test_a_busy_browser_leaves_nothing_behind_for_the_next_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _Supervisor()
    sup.tab = {"targetId": "T1", "url": "https://app.example/form", "title": "t"}
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    state = _granted("app.example", "act")
    _on_page("https://app.example/form")
    # The test holds the lock, so every wait times out at once.
    monkeypatch.setattr(mod, "LOCK_WAIT_S", 0)
    await mod._OP_LOCK.acquire()
    try:
        for _ in range(bht._PER_SESSION_CAP + 1):
            answer, _ = await mod.run_browser_op(_SESSION, "upload", _UPLOAD, state=state)
            assert answer["code"] == "browser_busy"
    finally:
        mod._OP_LOCK.release()
    assert bht._pending == {}
    answer, _ = await mod.run_browser_op(_SESSION, "upload", _UPLOAD, state=state)
    assert state.opened == [] and answer.get("code") != "needs_approval"


@pytest.mark.asyncio
async def test_on_an_app_slot_a_person_s_grant_does_not_lift_the_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _Supervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    state = _granted("app.example", "act")
    state._slots["chat-1"]._app = "someapp"
    _on_page("https://app.example/form")
    monkeypatch.setattr(mod, "slot_sites", _no_sites)
    answer, _ = await mod.run_browser_op(_SESSION, "upload", _UPLOAD, state=state)
    assert answer["code"] == "needs_approval" and len(state.opened) == 1


async def _no_sites(*_a: Any, **_k: Any) -> tuple[str, ...]:
    return ()


@pytest.mark.asyncio
async def test_a_strict_hidden_path_on_a_grant_goes_to_the_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _Supervisor()
    sup.tab = {"targetId": "T1", "url": "https://app.example/form", "title": "t"}
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    state = _granted("app.example", "act")
    _on_page("https://app.example/form")
    ran: list[bool] = []

    async def _op(op: str, args: dict, ctx: Any, supervisor: Any, *_caller: Any) -> dict:
        ran.append(bool(ctx.upload_grant_host))
        return {"ok": False, "code": "needs_card", "error": "hidden"}

    monkeypatch.setattr(mod.browser_sign_in, "run_op", _op)
    args = {"ref": "e4", "path": _HIDDEN}
    answer, _ = await mod.run_browser_op(_SESSION, "upload", args, state=state)
    assert ran == [True] and answer["code"] == "needs_approval" and len(state.opened) == 1
    # The person's Allow then runs it on the card's terms, with no grant in play.
    state.answer(answer["ask_id"], card.ALLOW)
    await mod.run_browser_op(_SESSION, "upload", args, state=state, approval=answer["ask_id"])
    assert ran == [True, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("turned_away", ["needs_card", bht.SCOPE_CHANGED_CODE])
async def test_a_turned_away_upload_does_not_use_up_the_run_budget(
    monkeypatch: pytest.MonkeyPatch, turned_away: str
) -> None:
    sup = _Supervisor()
    sup.tab = {"targetId": "T1", "url": "https://app.example/form", "title": "t"}
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    state = _granted("app.example", "act")
    _on_page("https://app.example/form")
    monkeypatch.setattr(mod, "_run_limits", lambda: (0, 1, 0))
    answers = iter([{"ok": False, "code": turned_away}, {"ok": True, "ref": "e4"}])

    async def _op(op: str, args: dict, ctx: Any, supervisor: Any, *_caller: Any) -> dict:
        return next(answers)

    monkeypatch.setattr(mod.browser_sign_in, "run_op", _op)
    # The chat's own route call: only that one counts against ``browser.run``.
    caller = {"user": "person"}
    args = {"ref": "e4", "path": _HIDDEN}
    first, _ = await mod.run_browser_op(_SESSION, "upload", args, state=state, caller=caller)
    if turned_away == "needs_card":
        assert first["code"] == "needs_approval"
        state.answer(first["ask_id"], card.ALLOW)
        ran, _ = await mod.run_browser_op(
            _SESSION, "upload", args, state=state, caller=caller, approval=first["ask_id"]
        )
    else:
        # The tab left the host after staging; the agent repeats the same call.
        assert first["code"] == bht.SCOPE_CHANGED_CODE
        ran, _ = await mod.run_browser_op(_SESSION, "upload", args, state=state, caller=caller)
    assert ran["ok"] is True


def test_the_upload_card_says_an_act_grant_covers_it() -> None:
    notice = card.card_notice("upload")
    assert "unless you trusted this site for reading, clicking, typing and uploading" in notice
    assert "Trust menu" in notice and "except" not in card.card_notice("js")


def test_the_card_marks_a_folder_and_a_pattern(tmp_path: Any) -> None:
    (tmp_path / "pics").mkdir()
    (tmp_path / "a.png").write_bytes(b"a")
    shown = card.card_questions(
        "upload",
        {
            "ref": "e4",
            "via": "drop",
            "path": [str(tmp_path / "a.png"), str(tmp_path / "pics"), str(tmp_path / "*.png")],
        },
    )[0]["question"]
    lines = shown.splitlines()
    assert lines[0] == "upload into e4 (drops on it):"
    assert lines[1] == str(tmp_path / "a.png")
    assert lines[2].endswith("(folder: every file inside except hidden files and links)")
    assert lines[3].endswith("(pattern: every matching file)")
    point = card.card_questions("upload", {"x": 40, "y": 60, "via": "drop", "path": "/a"})
    assert point[0]["question"].startswith(
        "upload into the point (40, 60) on the page (drops on it):"
    )


def test_an_app_s_approved_sites_never_cover_upload() -> None:
    from kiro_crew.apps import browser_sites

    call = SimpleNamespace(
        identity_trusted=True,
        mcp_server="kirocrew-core",
        mcp_tool="browser",
        raw_params={"op": "upload", "args": _UPLOAD},
    )
    facts = SimpleNamespace(
        app="someapp", session_key="dashboard:chat-1", call=call, canonical_mcp_name=True
    )
    assert browser_sites.gate_grants(facts) is False


@pytest.mark.asyncio
async def test_the_card_text_is_built_off_the_event_loop() -> None:
    import threading

    seen: list[bool] = []
    real = card._shown

    def _record(op: str, args: dict) -> str:
        seen.append(threading.current_thread() is threading.main_thread())
        return real(op, args)

    state = _State()
    try:
        card._shown = _record  # type: ignore[assignment]
        await card.open_card(state, _SESSION, "upload", _UPLOAD)
    finally:
        card._shown = real  # type: ignore[assignment]
    assert seen and not any(seen)


@pytest.mark.asyncio
async def test_the_route_hands_the_op_its_governance_read_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _Supervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    state = _granted("app.example", "act")
    _on_page("https://u/")
    got: list[Any] = []

    async def _op(op: str, args: dict, ctx: Any, supervisor: Any, *_caller: Any) -> dict:
        got.append(ctx.read_allowed)
        return {"ok": True}

    monkeypatch.setattr(mod.browser_sign_in, "run_op", _op)
    state._slots["chat-1"].agent = "researcher"
    asked: list[tuple[str, ...]] = []

    def _permitted(*call: str) -> bool:
        asked.append(call)
        return False

    monkeypatch.setattr(mod, "_read_permitted", _permitted)
    await mod.run_browser_op(_SESSION, "snapshot", {}, state=state)
    # Named as the tool gate names the caller: its agent and app narrow the profile.
    assert got and got[0]("/x") is False and asked == [(_SESSION, "researcher", "", "/x")]


def test_an_unevaluable_read_check_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.platform import governance_profiles

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("no ceiling")

    monkeypatch.setattr(governance_profiles, "governance_permits", _boom)
    assert mod._read_permitted(_SESSION, "", "", "/x") is False


@pytest.mark.asyncio
async def test_the_card_lists_the_files_and_its_allow_sends_only_those(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    sup = _Supervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    state = _State()
    photos = tmp_path / "photos"
    photos.mkdir()
    for name in ("a.png", "b.png"):
        (photos / name).write_bytes(b"12")
    sent: list[Any] = []

    async def _op(op: str, args: dict, ctx: Any, supervisor: Any, *_caller: Any) -> dict:
        sent.append(ctx.upload_groups)
        return {"ok": True, "ref": "e4"}

    monkeypatch.setattr(mod.browser_sign_in, "run_op", _op)
    args = {"ref": "e4", "path": str(photos), "via": "drop"}
    answer, _ = await mod.run_browser_op(_SESSION, "upload", args, state=state)
    assert answer["code"] == "needs_approval" and sent == []
    question = state.opened[0][0]["question"]
    a, b = os.path.join("photos", "a.png"), os.path.join("photos", "b.png")
    assert question.splitlines()[-1] == f"Sends 2 files, 4 B: {a}, {b}"
    # A file that lands in the folder after the card is not what the person allowed.
    (photos / "c.png").write_bytes(b"3")
    state.answer(answer["ask_id"], card.ALLOW)
    await mod.run_browser_op(_SESSION, "upload", args, state=state, approval=answer["ask_id"])
    ((groups, _skipped),) = sent
    assert [rel for group in groups for _src, rel in group.files] == [
        os.path.join("photos", "a.png"),
        os.path.join("photos", "b.png"),
    ]


def test_the_card_counts_the_files_past_the_first_five(tmp_path: Any) -> None:
    from kiro_crew.browser_harness import upload

    for n in range(7):
        (tmp_path / f"p{n}.png").write_bytes(b"x" * 300)
    groups, _ = upload.expand([str(tmp_path / "*.png")])
    assert upload.card_summary(groups) == (
        "Sends 7 files, 2.1 KB: p0.png, p1.png, p2.png, p3.png, p4.png and 2 more"
    )


@pytest.mark.asyncio
async def test_the_summary_line_does_not_count_against_the_call_cap(tmp_path: Any) -> None:
    import threading

    from kiro_crew.browser_harness import upload

    # A call just under the cap as written; the summary line then takes the card past it.
    first = tmp_path / ("a" * 150 + ".png")
    first.write_bytes(b"x")
    probe = str(tmp_path / "b.png")
    base = len(card._shown("upload", {"ref": "e4", "path": [str(first), probe]}))
    second = tmp_path / ("b" * (1 + card.SHOWN_MAX - 5 - base) + ".png")
    second.write_bytes(b"x")
    args = {"ref": "e4", "path": [str(first), str(second)]}
    assert card.SHOWN_MAX - 10 < len(card._shown("upload", args)) <= card.SHOWN_MAX
    seen: list[bool] = []
    real = upload.card_summary

    def _record(groups: Any) -> str:
        seen.append(threading.current_thread() is threading.main_thread())
        return real(groups)

    state = _State()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(upload, "card_summary", _record)
        answer = await card.open_card(state, _SESSION, "upload", args)
    assert answer["code"] == "needs_approval", answer
    # The summary reads each file's size on disk, so it runs off the event loop.
    assert seen == [False]
