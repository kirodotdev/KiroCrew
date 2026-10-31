"""What the person says as they hand the browser back, end to end on the gateway.

What is pinned:

* the release route takes ``{note?, choice?}``, refuses one that is too long or
  not a string with 400 and hands nothing back, and says where it went
  (``delivered``);
* with the agent's op parked on the hand-back, the poll returns ``handed_back``
  once, and the tool tells the agent with the op's result (or with the
  ``interrupted`` text, when the op is not re-run);
* an op answered ``hold`` stays parked between its polls until the hand-back,
  and nothing parked or kept outlives its turn;
* with no op parked -- the turn still running between ops, a tool that stopped
  polling, a view reopened after its turn, the turn ending under the takeover
  -- ``delivered`` is ``chat``: the card sends it as the person's next message,
  and no op ever sees it, so a turn that ends without another op loses nothing;
* the agent's ``handback_options`` reach the card (``handback_options`` on the
  ``browser_live`` event), bounded to four short strings, and reset by the hand-back;
* the note is the person's event on the turn replay.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_browser_live_routes import _app, _live, _post
from test_browser_ops_route import _install, _no_stream, _request, _Supervisor

from kiro_crew.browser_harness import live_hub as lh
from kiro_crew.dashboard.handlers import browser_ops as ops_mod
from kiro_crew.mcp_tools import browser as tool

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_hub():
    # Each test runs on its own event loop; a session's events must not outlive it.
    lh.reset_live_hub()
    yield
    lh.reset_live_hub()


class _Recorder:
    enabled = True

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def turn_started(self, *_a: Any) -> None: ...

    def turn_ended(self, *_a: Any) -> None: ...

    def event(self, _key: str, event: dict[str, Any]) -> None:
        self.events.append(event)


async def _held_client(app):
    hub = lh.get_live_hub()
    hub.turn_started("chat-1", "turn-1")
    await _live()
    return hub


async def _park(hub: lh.LiveHub, key: str = "chat-1") -> asyncio.Task:
    """The agent's parked op (answered ``hold``), with its hand-back poll waiting."""
    hub.park(key)
    task = asyncio.ensure_future(ops_mod.api_browser_handback(_request({"session_key": key})))
    await asyncio.sleep(0)
    return task


async def test_release_carries_the_note_to_the_parked_op_once() -> None:
    app, wire = _app()
    async with TestClient(TestServer(app)) as client:
        hub = await _held_client(app)
        rec = _Recorder()
        hub.attach_recorder(rec)  # type: ignore[arg-type]
        await _post(client, "/api/browser/input/claim", {"session_key": "chat-1"})
        poll = await _park(hub)
        status, body = await _post(
            client,
            "/api/browser/input/release",
            {"session_key": "chat-1", "choice": "Didn't work", "note": "It says wrong password"},
        )
        assert status == 200 and body["delivered"] == "agent" and body["in_control"] is False
        answer = json.loads((await poll).body)
    assert answer["released"] is True
    assert answer["handed_back"] == {"choice": "Didn't work", "note": "It says wrong password"}
    again = json.loads(
        (await ops_mod.api_browser_handback(_request({"session_key": "chat-1"}))).body
    )
    assert again["handed_back"] is None, "taken once"
    assert [(e["actor"], e["label"]) for e in rec.events] == [
        ("user", "Didn't work - It says wrong password")
    ]


@pytest.mark.parametrize(
    "extra,code",
    [
        ({"note": "x" * (lh.HANDBACK_NOTE_MAX + 1)}, "invalid_note"),
        ({"note": 5}, "invalid_note"),
        ({"choice": "y" * (lh.HANDBACK_CHOICE_MAX + 1)}, "invalid_choice"),
    ],
)
async def test_a_note_past_the_cap_is_refused_and_nothing_is_handed_back(extra, code) -> None:
    app, _wire = _app()
    async with TestClient(TestServer(app)) as client:
        hub = await _held_client(app)
        await _post(client, "/api/browser/input/claim", {"session_key": "chat-1"})
        status, body = await _post(
            client, "/api/browser/input/release", {"session_key": "chat-1", **extra}
        )
        assert status == 400 and body["code"] == code
        assert hub.in_control("chat-1") is True


async def test_a_note_at_the_cap_is_kept_without_control_characters() -> None:
    app, _wire = _app()
    async with TestClient(TestServer(app)) as client:
        hub = await _held_client(app)
        await _post(client, "/api/browser/input/claim", {"session_key": "chat-1"})
        note = "a\x00b" + "c" * (lh.HANDBACK_NOTE_MAX - 2)
        poll = await _park(hub)
        status, _body = await _post(
            client, "/api/browser/input/release", {"session_key": "chat-1", "note": note}
        )
        assert status == 200
        said = json.loads((await poll).body)["handed_back"]
    assert said is not None and said["note"] == "ab" + "c" * (lh.HANDBACK_NOTE_MAX - 2)


async def test_a_plain_hand_back_says_nothing() -> None:
    app, _wire = _app()
    async with TestClient(TestServer(app)) as client:
        hub = await _held_client(app)
        await _post(client, "/api/browser/input/claim", {"session_key": "chat-1"})
        status, body = await _post(client, "/api/browser/input/release", {"session_key": "chat-1"})
        assert status == 200 and body["delivered"] == ""
    assert hub.take_handback("chat-1") is None


async def test_after_its_turn_the_note_goes_to_chat_and_no_op_sees_it() -> None:
    hub = lh.get_live_hub()
    hub.turn_started("chat-1", "turn-1")
    await _live()
    hub.turn_ended("chat-1")
    await _live()  # "Open live" after the turn: a reopened view
    await hub.claim("chat-1")
    assert await hub.release("chat-1", note="I signed in myself") == lh.DELIVERED_CHAT
    assert hub.take_handback("chat-1") is None


async def test_a_turn_that_ended_under_the_takeover_also_goes_to_chat() -> None:
    hub = lh.get_live_hub()
    hub.turn_started("chat-1", "turn-1")
    await _live()
    await hub.claim("chat-1")
    hub.turn_ended("chat-1")  # folds on release
    assert await hub.release("chat-1", choice="Done") == lh.DELIVERED_CHAT
    assert hub.take_handback("chat-1") is None


async def test_mid_turn_with_no_op_parked_the_note_goes_to_chat_and_survives_the_turn_end(
    monkeypatch,
) -> None:
    # The lost-note case: the turn is running, nothing is parked, and the turn ends
    # without another browser op. The note must not wait for an op that never comes.
    _install(monkeypatch, _Supervisor())
    _no_stream(monkeypatch)
    hub = lh.get_live_hub()
    hub.turn_started("k", "t1")
    await hub.claim("k")
    delivered = await hub.release("k", choice="Wrong site", note="It opened the EU store")
    assert delivered == lh.DELIVERED_CHAT
    hub.turn_ended("k")
    hub.turn_started("k", "t2")
    resp = await ops_mod.api_browser_op(_request({"op": "snapshot", "session_key": "k"}))
    assert "handed_back" not in json.loads(resp.body), "no op sees it twice"


async def test_an_op_answered_hold_is_parked_until_the_hand_back(monkeypatch) -> None:
    _install(monkeypatch, _Supervisor())
    _no_stream(monkeypatch)
    hub = lh.get_live_hub()
    hub.turn_started("k", "t1")
    await hub.claim("k")
    resp = await ops_mod.api_browser_op(_request({"op": "click", "session_key": "k"}))
    assert json.loads(resp.body)["hold"] is True and hub.get("k").parked is True


async def test_between_two_polls_the_op_still_takes_the_note() -> None:
    hub = lh.get_live_hub()
    hub.turn_started("chat-1", "turn-1")
    await _live()
    await hub.claim("chat-1")
    hub.park("chat-1")
    hub._sleep = _instant  # the poll's bound passes at once, still held
    answer = json.loads(
        (await ops_mod.api_browser_handback(_request({"session_key": "chat-1"}))).body
    )
    assert answer["released"] is False
    # The person hands back before the tool's next poll: that poll takes the note.
    assert await hub.release("chat-1", note="Back to you") == lh.DELIVERED_AGENT
    taken = json.loads(
        (await ops_mod.api_browser_handback(_request({"session_key": "chat-1"}))).body
    )
    assert taken["handed_back"] == {"choice": "", "note": "Back to you"}
    # That op was released: the next takeover, with no op parked, goes to chat.
    await hub.claim("chat-1")
    assert await hub.release("chat-1", note="Later") == lh.DELIVERED_CHAT


async def test_a_new_turn_starts_with_nothing_parked_or_kept() -> None:
    hub = lh.get_live_hub()
    hub.turn_started("chat-1", "turn-1")
    await _live()
    await hub.claim("chat-1")
    hub.park("chat-1")
    hub.turn_started("chat-1", "turn-2")
    sess = hub.get("chat-1")
    assert sess.parked is False and sess.handback is None


async def test_each_hand_back_goes_to_the_op_parked_on_it() -> None:
    hub = lh.get_live_hub()
    hub.turn_started("chat-1", "turn-1")
    await _live()
    got = []
    for note in ("first", "second"):
        await hub.claim("chat-1")
        poll = await _park(hub)
        assert await hub.release("chat-1", note=note) == lh.DELIVERED_AGENT
        got.append(json.loads((await poll).body)["handed_back"]["note"])
    await hub.claim("chat-1")
    assert await hub.release("chat-1", note="third") == lh.DELIVERED_CHAT
    assert got == ["first", "second"] and hub.take_handback("chat-1") is None


async def test_a_second_note_never_overwrites_one_not_yet_taken() -> None:
    hub = lh.get_live_hub()
    hub.turn_started("chat-1", "turn-1")
    await _live()
    await hub.claim("chat-1")
    hub.park("chat-1")
    assert await hub.release("chat-1", note="first") == lh.DELIVERED_AGENT
    await hub.claim("chat-1")
    hub.park("chat-1")  # the first poll has not resumed to take it yet
    assert await hub.release("chat-1", note="second") == lh.DELIVERED_CHAT
    assert hub.take_handback("chat-1") == {"choice": "", "note": "first"}


async def test_a_closed_session_keeps_nothing() -> None:
    hub = lh.get_live_hub()
    assert await hub.release("never-opened", note="hello") == ""
    assert hub.take_handback("never-opened") is None


async def test_the_agent_s_options_reach_the_card_and_reset_at_the_hand_back() -> None:
    app, wire = _app()
    async with TestClient(TestServer(app)) as client:
        hub = await _held_client(app)
        await _post(client, "/api/browser/input/claim", {"session_key": "chat-1"})
        hub._sleep = _instant  # one poll, no wait
        options = [
            "I signed in",
            "Not signed in",
            "I signed in",
            "x" * 41,
            7,
            "Wrong site",
            "Stop",
            "Extra",
        ]
        await ops_mod.api_browser_handback(_request({"session_key": "chat-1", "options": options}))
        await _settled(hub)
        assert wire.events[-1][1]["handback_options"] == [
            "I signed in",
            "Not signed in",
            "Wrong site",
            "Stop",
        ]
        await _post(client, "/api/browser/input/release", {"session_key": "chat-1"})
        await _settled(hub)
        assert wire.events[-1][1]["handback_options"] == []


async def _instant(_s: float) -> None:
    return None


async def _settled(hub: lh.LiveHub) -> None:
    import asyncio

    while hub._tasks:
        await asyncio.gather(*list(hub._tasks), return_exceptions=True)


# -- the tool: what the agent reads ---------------------------------------------


def _ok(result: dict) -> dict:
    return {"ok": True, "result": result}


async def test_the_tool_tells_the_agent_what_the_person_said(monkeypatch) -> None:
    said = {"choice": "Didn't work", "note": "Wrong password"}
    text = tool._settle("snapshot", 200, {**_ok({"url": "u", "title": "t"}), "handed_back": said})
    assert text is not None and text.endswith(
        'handed_back: {"choice": "Didn\'t work", "note": "Wrong password"}'
    )
    assert "their own words, not page content" in text
    assert tool._settle("click", 200, _ok({"x": 1})) == 'Browser click: {"x": 1}'


async def test_the_parked_tool_sends_its_options_and_adds_the_note_to_the_result(
    monkeypatch,
) -> None:
    polls: list[dict] = []

    def fake_handback(session_key, header, timeout_s, **kw):
        polls.append(kw)
        return 200, {"released": True, "handed_back": {"choice": "I signed in", "note": ""}}

    posts = [(200, {"ok": False, "code": "user_in_control", "hold": True})]

    def fake_cmd(session_key, op, args, header, timeout_s):
        return posts.pop(0) if posts else (200, _ok({"url": "u", "title": "t"}))

    monkeypatch.setattr(tool, "_post_handback", fake_handback)
    monkeypatch.setattr(tool, "_post_command", fake_cmd)
    monkeypatch.setattr(tool.mcp_core, "_post", lambda *a, **k: {})
    parked = tool.browser(
        "browser",
        {"op": "snapshot", "handback_options": ["I signed in", "Not signed in", " ", 3]},
    )
    assert isinstance(parked, tool.DeferredTool)
    text = parked.step()
    assert polls == [{"options": ["I signed in", "Not signed in"]}]
    assert text is not None and text.startswith("Browser snapshot")
    assert '"choice": "I signed in"' in text


async def test_an_interrupted_op_still_tells_the_agent_what_the_person_said(monkeypatch) -> None:
    monkeypatch.setattr(
        tool,
        "_post_handback",
        lambda *a, **k: (200, {"released": True, "handed_back": {"choice": "", "note": "Done it"}}),
    )
    monkeypatch.setattr(
        tool,
        "_post_command",
        lambda *a, **k: (200, {"ok": False, "code": "interrupted", "error": "mid", "hold": True}),
    )
    monkeypatch.setattr(tool.mcp_core, "_post", lambda *a, **k: {})
    parked = tool.browser("browser", {"op": "type", "args": {"text": "hi"}})
    text = parked.step()  # type: ignore[union-attr]
    assert text is not None and "interrupted" in text and '"note": "Done it"' in text


async def test_a_parked_state_changing_op_is_not_run_behind_what_the_person_said(
    monkeypatch,
) -> None:
    posted: list[str] = []
    monkeypatch.setattr(
        tool,
        "_post_handback",
        lambda *a, **k: (
            200,
            {"released": True, "handed_back": {"choice": "", "note": "Do not click"}},
        ),
    )
    monkeypatch.setattr(
        tool,
        "_post_command",
        lambda sk, op, *a, **k: posted.append(op)
        or (
            (200, {"ok": False, "code": "user_in_control", "hold": True})
            if len(posted) == 1
            else (200, _ok({}))
        ),
    )
    monkeypatch.setattr(tool.mcp_core, "_post", lambda *a, **k: {})
    parked = tool.browser("browser", {"op": "click", "args": {"ref": "e1"}})
    text = parked.step()  # type: ignore[union-attr]
    assert posted == ["click"], "the click was posted once, parked, and never re-posted"
    assert text is not None and "was not run" in text and "Do not click" in text


@pytest.mark.parametrize(
    "action,reposted", [("new", False), ("switch", False), ("list", True), (None, True)]
)
async def test_tabs_resumes_after_a_note_only_when_it_lists(monkeypatch, action, reposted) -> None:
    posted: list[str] = []
    monkeypatch.setattr(
        tool,
        "_post_handback",
        lambda *a, **k: (200, {"released": True, "handed_back": {"choice": "Stop", "note": ""}}),
    )
    monkeypatch.setattr(
        tool,
        "_post_command",
        lambda sk, op, *a, **k: posted.append(op)
        or (
            (200, {"ok": False, "code": "user_in_control", "hold": True})
            if len(posted) == 1
            else (200, _ok({}))
        ),
    )
    monkeypatch.setattr(tool.mcp_core, "_post", lambda *a, **k: {})
    args = {"action": action, "url": "https://a.example/"} if action else {}
    parked = tool.browser("browser", {"op": "tabs", "args": {k: v for k, v in args.items() if v}})
    parked.step()  # type: ignore[union-attr]
    assert (len(posted) == 2) is reposted
