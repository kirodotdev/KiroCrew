"""Frames the engine re-sends share one copy of their large lists.

``frame_payload_sharing.RepeatedPayloads`` runs on every frame the reader loop
queues for a session. These tests pin that a shared frame stays equal to the
frame the engine sent, so every reader of the queue reads the same values, and
that only equal lists are ever shared.
"""

from __future__ import annotations

import copy

from kiro_crew.acp.frame_payload_sharing import MAX_SERVERS, RepeatedPayloads
from kiro_crew.acp.types import METHOD_KAS_MCP_STATUS, METHOD_SESSION_UPDATE, JsonRpcMessage


def _catalog(n: int, tag: str = "") -> list[dict]:
    return [
        {
            "name": f"tool{i}{tag}",
            "description": f"does thing {i}",
            "disabled": False,
            "inputSchema": {"type": "object", "properties": {"a": {"type": "string"}}},
        }
        for i in range(n)
    ]


def _status(
    session: str, servers: dict[str, list[dict]], status: str = "connected"
) -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_KAS_MCP_STATUS,
        params={
            "sessionId": session,
            "servers": [
                {"name": name, "status": status, "tools": tools, "prompts": [], "resources": []}
                for name, tools in servers.items()
            ],
        },
    )


def _options(current: str) -> list[dict]:
    return [
        {
            "id": "model",
            "type": "select",
            "currentValue": current,
            "options": [{"value": f"m{i}", "name": f"Model {i}"} for i in range(5)],
        }
    ]


def _config(session: str, options: list[dict]) -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={
            "sessionId": session,
            "update": {"sessionUpdate": "config_option_update", "configOptions": options},
        },
    )


def test_equal_catalogs_share_one_list_and_frames_stay_equal():
    sharing = RepeatedPayloads()
    first = _status("s1", {"core": _catalog(20), "web": _catalog(5)})
    second = _status("s2", {"core": _catalog(20), "web": _catalog(5)})
    sent = copy.deepcopy(second.params)

    sharing.share(first)
    sharing.share(second)

    assert second.params == sent, "sharing changed what the frame says"
    for a, b in zip(first.params["servers"], second.params["servers"]):
        assert b["tools"] is a["tools"]
    assert second.params["servers"][0]["prompts"] == []


def test_a_changed_catalog_is_kept_as_sent_and_becomes_the_shared_copy():
    sharing = RepeatedPayloads()
    first = _status("s1", {"core": _catalog(20)})
    changed = _status("s1", {"core": _catalog(20, tag="x")})
    third = _status("s2", {"core": _catalog(20, tag="x")})
    sent = copy.deepcopy(changed.params)

    for msg in (first, changed, third):
        sharing.share(msg)

    assert changed.params == sent
    assert changed.params["servers"][0]["tools"] is not first.params["servers"][0]["tools"]
    assert third.params["servers"][0]["tools"] is changed.params["servers"][0]["tools"]


def test_every_other_field_of_a_status_entry_stays_its_own():
    sharing = RepeatedPayloads()
    first = _status("s1", {"core": _catalog(3)}, status="connected")
    failed = _status("s1", {"core": _catalog(3)}, status="failed")
    failed.params["servers"][0]["failedAuthorization"] = True
    sent = copy.deepcopy(failed.params)

    sharing.share(first)
    sharing.share(failed)

    assert failed.params == sent
    assert failed.params["servers"][0]["status"] == "failed"
    assert failed.params["servers"][0]["failedAuthorization"] is True


def test_equal_config_options_share_one_list_and_different_ones_do_not():
    sharing = RepeatedPayloads()
    first, same, other = (
        _config("s1", _options("m1")),
        _config("s2", _options("m1")),
        _config("s3", _options("m2")),
    )
    sent = [copy.deepcopy(m.params) for m in (first, same, other)]

    for msg in (first, same, other):
        sharing.share(msg)

    assert [m.params for m in (first, same, other)] == sent
    assert same.params["update"]["configOptions"] is first.params["update"]["configOptions"]
    assert other.params["update"]["configOptions"] is not first.params["update"]["configOptions"]


def test_responses_and_other_frames_are_left_alone():
    sharing = RepeatedPayloads()
    response = JsonRpcMessage(id=7, result={"servers": [{"name": "core", "tools": _catalog(2)}]})
    chunk = JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={"sessionId": "s1", "update": {"sessionUpdate": "agent_message_chunk"}},
    )
    odd = JsonRpcMessage(method=METHOD_KAS_MCP_STATUS, params=["not", "a", "dict"])
    before = copy.deepcopy([response.result, chunk.params, odd.params])

    for msg in (response, chunk, odd):
        sharing.share(msg)

    assert [response.result, chunk.params, odd.params] == before


def _full_status(name: str) -> JsonRpcMessage:
    """One server carrying all four lists, each non-empty."""
    item = [{"name": "x", "description": "d"}]
    return JsonRpcMessage(
        method=METHOD_KAS_MCP_STATUS,
        params={
            "sessionId": "s1",
            "servers": [
                {
                    "name": name,
                    "tools": _catalog(1),
                    "prompts": list(item),
                    "resources": list(item),
                    "resourceTemplates": list(item),
                }
            ],
        },
    )


def test_the_held_catalogs_are_bounded_by_server_name():
    """The cap counts servers, however many lists each one carries."""
    sharing = RepeatedPayloads()
    for i in range(MAX_SERVERS + 50):
        sharing.share(_full_status(f"server{i}"))
    assert len(sharing._by_server) == MAX_SERVERS

    # The last server let in keeps sharing all four of its lists.
    last = f"server{MAX_SERVERS - 1}"
    first, again = _full_status(last), _full_status(last)
    sharing.share(first)
    sharing.share(again)
    for field in ("tools", "prompts", "resources", "resourceTemplates"):
        assert again.params["servers"][0][field] is first.params["servers"][0][field]

    # A server past the cap is left as sent.
    late, late_again = _full_status("late"), _full_status("late")
    sharing.share(late)
    sharing.share(late_again)
    assert late_again.params["servers"][0]["tools"] is not late.params["servers"][0]["tools"]
