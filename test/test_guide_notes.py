"""Agent-authored guide text: the guide's ``intro`` and each action's ``note``.

The dashboard stays the source of truth for where and which control; the agent
may only add its own plain words. These pin the gateway's refusals (never a
silent truncation) and that the text reaches the published guide intact.
"""

import pytest

from kiro_crew import guide_catalog as catalog
from kiro_crew.dashboard.guide_runs import GuideError, GuideStore
from kiro_crew.mcp_guide import _tool_definitions

SLOT = "chat-fixture"
SESSION = "dashboard:chat-fixture"
CREWMATE = {"id": "crewmate.create", "params": {"name": "Scout"}}


def _start(**kwargs):
    store = GuideStore(clock=lambda: 1000.0)
    return store.start(slot_key=SLOT, session_key=SESSION, **kwargs)


def test_absent_intro_and_note_are_fine_and_publish_empty():
    guide = _start(actions=[CREWMATE])
    assert guide["intro"] == ""
    assert "note" not in guide["actions"][0]


def test_intro_and_note_reach_the_published_guide():
    guide = _start(
        actions=[{**CREWMATE, "note": "Scout will watch your builds overnight."}],
        intro="Two clicks and Scout is on your crew.",
    )
    assert guide["intro"] == "Two clicks and Scout is on your crew."
    assert guide["actions"][0]["note"] == "Scout will watch your builds overnight."


def test_line_breaks_and_tabs_collapse_to_one_line():
    guide = _start(actions=[{**CREWMATE, "note": " a\r\n\tb  c "}], intro="x\ny")
    assert guide["actions"][0]["note"] == "a b c"
    assert guide["intro"] == "x y"


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("see https://example.com", "no links"),
        ("see HTTP://example.com", "no links"),
        ("go to www.example.com", "no links"),
        ("<b>bold</b>", "plain text only"),
        ("a > b", "plain text only"),
        ("run `rm`", "plain text only"),
        ("the [docs](page)", "plain text only"),
        ("bell\x07", "control"),
        ("flip \u202e txet", "control"),
    ],
)
def test_note_content_rules_refuse(text, fragment):
    with pytest.raises(GuideError) as exc:
        _start(actions=[{**CREWMATE, "note": text}])
    assert exc.value.status == 400
    assert exc.value.code == "invalid_text"
    assert fragment in exc.value.message
    assert "action 0 note" in exc.value.message


@pytest.mark.parametrize("text", ["https://example.com", "<i>x</i>", "x\x00y"])
def test_intro_content_rules_refuse(text):
    with pytest.raises(GuideError) as exc:
        _start(actions=[CREWMATE], intro=text)
    assert exc.value.code == "invalid_text"
    assert exc.value.message.startswith("intro")


def test_plain_brackets_and_emoji_are_ordinary_text():
    # Only a [text](target) pair is markup; a lone bracket or an emoji joiner is not.
    guide = _start(actions=[{**CREWMATE, "note": "Scout [beta] 👩\u200d💻 is ready"}])
    assert guide["actions"][0]["note"] == "Scout [beta] 👩\u200d💻 is ready"


def test_length_caps_refuse_rather_than_truncate():
    at_cap = "a" * catalog.MAX_NOTE_CHARS
    assert _start(actions=[{**CREWMATE, "note": at_cap}])["actions"][0]["note"] == at_cap
    with pytest.raises(GuideError) as exc:
        _start(actions=[{**CREWMATE, "note": at_cap + "b"}])
    assert f"is {catalog.MAX_NOTE_CHARS + 1} characters" in exc.value.message
    assert f"at most {catalog.MAX_NOTE_CHARS}" in exc.value.message

    intro = "i" * catalog.MAX_INTRO_CHARS
    assert _start(actions=[CREWMATE], intro=intro)["intro"] == intro
    with pytest.raises(GuideError, match=f"at most {catalog.MAX_INTRO_CHARS}"):
        _start(actions=[CREWMATE], intro=intro + "i")
    assert catalog.MAX_INTRO_CHARS == 200 and catalog.MAX_NOTE_CHARS == 160


def test_one_note_per_action_so_a_notes_array_is_refused():
    # A note is shown under the action's final step, so it can never outnumber
    # the steps; a per-step array is not a shape the gateway accepts.
    with pytest.raises(GuideError, match="unknown field 'notes'"):
        _start(actions=[{**CREWMATE, "notes": ["a", "b", "c", "d", "e"]}])


def test_non_string_text_is_refused():
    with pytest.raises(GuideError, match="note must be a string"):
        _start(actions=[{**CREWMATE, "note": ["x"]}])
    with pytest.raises(GuideError, match="intro must be a string"):
        _start(actions=[CREWMATE], intro=5)


def test_a_refused_intro_stores_nothing():
    store = GuideStore(clock=lambda: 1000.0)
    with pytest.raises(GuideError):
        store.start(slot_key=SLOT, session_key=SESSION, actions=[CREWMATE], intro="<x>")
    assert store._guides == {}


def test_tool_schema_advertises_intro_and_note():
    start = next(t for t in _tool_definitions() if t["name"] == "guide_start")
    props = start["inputSchema"]["properties"]
    assert props["intro"]["maxLength"] == catalog.MAX_INTRO_CHARS
    assert props["actions"]["items"]["properties"]["note"]["maxLength"] == catalog.MAX_NOTE_CHARS


def test_the_start_route_accepts_intro_and_refuses_bad_text():
    from guide_route_helpers import CREWMATE as ROUTE_ACTIONS
    from guide_route_helpers import FakeState, agent, run_guide_app

    state = FakeState()
    state.open_slot("chat-1")

    async def go(c):
        bad = await c.post(
            "/api/guide/agent/start",
            json={"actions": ROUTE_ACTIONS, "intro": "see www.example.com"},
            headers=agent("dashboard:chat-1"),
        )
        good = await c.post(
            "/api/guide/agent/start",
            json={"actions": ROUTE_ACTIONS, "intro": "Quick one."},
            headers=agent("dashboard:chat-1"),
        )
        return bad.status, await bad.json(), good.status, await good.json()

    bad_status, bad_body, status, body = run_guide_app(go, state)
    assert bad_status == 400 and bad_body.get("code") == "invalid_text", bad_body
    assert status == 200, body
    assert body["intro"] == "Quick one."


def test_the_mcp_tool_forwards_intro_and_notes_to_the_gateway(monkeypatch):
    from kiro_crew import mcp_guide

    sent: list[dict] = []
    monkeypatch.setattr(mcp_guide, "_strict_session_key", lambda: ("dashboard:c", ""))
    monkeypatch.setattr(
        mcp_guide, "_post", lambda path, body, **k: sent.append(body) or {"delivered_clients": 1}
    )
    actions = [{**CREWMATE, "note": "Why it matters."}]
    mcp_guide._call_tool_inner("guide_start", {"actions": actions, "intro": "Short one."})
    mcp_guide._call_tool_inner("guide_start", {"actions": actions})
    assert sent == [{"actions": actions, "intro": "Short one."}, {"actions": actions}]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("[name](target)", True),
        ("[[name](target)", True),
        ("[x](a[y](b)", True),
        ("[x] stray (target)", False),
        ("]()[text]", False),
        ("[" * 100_000, False),
        ("[](" * 30_000, False),
        ("[](" * 30_000 + ")", True),
    ],
    ids=["link", "nested", "nested-target", "spaced", "stray", "openers", "targets", "closed"],
)
def test_markdown_link_detection_scans_repeated_delimiters(text, expected):
    assert catalog._has_markdown_link(text) is expected
