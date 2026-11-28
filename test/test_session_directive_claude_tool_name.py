"""Claude's stamped tool name selects parked directives on dashboard turns."""

import pytest
from test_session_directive_input_digest import (  # noqa: F401  (_clean_queue fixture)
    CALL_ARGS,
    _assert_record_applied_once,
    _clean_queue,
    _drive,
    _stub_state,
)

from kiro_crew import directive_queue
from kiro_crew.acp._dispatch import parse_session_update
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_RESULT,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.session_directive_apply import person_only_directive_refuser
from kiro_crew.providers.acp import AcpProvider

_LEGACY_TITLE = "mcp__kirocrew-core__monitor_start"
_READABLE_TITLE = "kirocrew-core: monitor_start"
_LEGACY_SET_PROJECT = "mcp__kirocrew-core__set_project"


@pytest.fixture(params=["dedicated", "shared"])
def parse_frames(request):
    if request.param == "shared":

        def parse(*frames):
            return [
                event
                for frame in frames
                for event in parse_session_update(frame, harness_backend=ACP_BACKEND_CLAUDE)
            ]

    else:
        client = AcpClient(acp_backend=ACP_BACKEND_CLAUDE)

        def parse(call, *updates):
            return [
                client._extract_tool_event(_message(call)),
                *(client._extract_tool_call_refinement(_message(frame)) for frame in updates),
            ]

    return parse


def _message(frame):
    return JsonRpcMessage(method="session/update", params={"update": frame})


def _call(title, stamp, *, args=CALL_ARGS):
    frame = {
        "sessionUpdate": "tool_call",
        "toolCallId": "call-1",
        "title": title,
        "kind": "other",
        "status": "pending",
        "rawInput": args,
    }
    if stamp:
        frame["_meta"] = {"claudeCode": {"toolName": stamp}}
    return frame


def _update(title, stamp):
    return {**_call(title, stamp), "sessionUpdate": "tool_call_update"}


def _result():
    return AcpEvent(
        kind=EVENT_TOOL_RESULT, tool_call_id="call-1", tool_output="Done.", tool_final=True
    )


async def _run_dashboard_turn(tmp_path, monkeypatch, events):
    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("claude-directive")
    slot._titled = True
    apply = await _drive(
        state,
        slot,
        [
            *(AcpProvider._to_llm_event(event) for event in events),
            _result(),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="OK"),
            AcpEvent(kind=EVENT_COMPLETE),
        ],
        monkeypatch,
    )
    return apply, slot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "stamp", "applies"),
    [
        pytest.param(_READABLE_TITLE, _LEGACY_TITLE, True, id="readable"),
        pytest.param(_LEGACY_TITLE, "Bash", False, id="builtin"),
        pytest.param(_LEGACY_TITLE, "mcp__other__monitor_start", False, id="foreign-server"),
        pytest.param(_LEGACY_TITLE, "", True, id="legacy"),
    ],
)
async def test_dashboard_directive_uses_claude_stamp(
    tmp_path, monkeypatch, parse_frames, title, stamp, applies
):
    apply, slot = await _run_dashboard_turn(
        tmp_path, monkeypatch, parse_frames(_call(title, stamp))
    )
    if applies:
        _assert_record_applied_once(apply, slot)
    else:
        apply.assert_not_awaited()
        assert directive_queue.depth(effective_session_key(slot)) == 1


@pytest.mark.asyncio
async def test_dashboard_directive_uses_stamp_first_seen_on_refinement(
    tmp_path, monkeypatch, parse_frames
):
    events = parse_frames(
        _call("Tool", "", args={}),
        _update(_READABLE_TITLE, _LEGACY_TITLE),
    )
    apply, slot = await _run_dashboard_turn(tmp_path, monkeypatch, events)
    _assert_record_applied_once(apply, slot)


@pytest.mark.asyncio
async def test_dashboard_refinement_can_disqualify_a_legacy_title(
    tmp_path, monkeypatch, parse_frames
):
    events = parse_frames(_call(_LEGACY_TITLE, ""), _update(_LEGACY_TITLE, "Bash"))
    apply, slot = await _run_dashboard_turn(tmp_path, monkeypatch, events)
    apply.assert_not_awaited()
    assert directive_queue.depth(effective_session_key(slot)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "stamp", "refused"),
    [
        pytest.param(
            "kirocrew-core: set_project", "mcp__kirocrew-core__set_project", True, id="readable"
        ),
        pytest.param(_LEGACY_SET_PROJECT, "Bash", False, id="builtin"),
    ],
)
async def test_headless_directive_refusal_uses_claude_stamp(parse_frames, title, stamp, refused):
    refusals = []
    observe = person_only_directive_refuser("cron:test", refusals)
    for event in [*parse_frames(_call(title, stamp)), _result()]:
        await observe(event)
    if refused:
        assert len(refusals) == 1
        (refusal,) = refusals
        assert refusal.startswith("Error: set_project only works from a user-facing session")
    else:
        assert refusals == []


@pytest.mark.asyncio
@pytest.mark.parametrize("trusted", [False, True], ids=["legacy", "trusted-pair"])
async def test_headless_refinement_respects_identity_priority(parse_frames, trusted):
    call = _call("Tool" if trusted else _LEGACY_SET_PROJECT, "")
    if trusted:
        call["_meta"] = {"kiro": {"mcpServerName": "kirocrew-core", "toolName": "set_project"}}
    events = parse_frames(call, _update(_LEGACY_SET_PROJECT, "Bash"))
    refusals = []
    observe = person_only_directive_refuser("cron:test", refusals)
    for event in [*events, _result()]:
        await observe(event)
    assert len(refusals) == int(trusted)
    if trusted:
        assert refusals[0].startswith("Error: set_project only works from a user-facing session")
