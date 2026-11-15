"""A chat whose MCP servers wait for a sign-in says so once, without being asked.

On KAS a server that needs a sign-in exposes no tools, and its sign-in is offered
only when the user's message names it. A chat that never names it shows missing
tools and nothing that says why. ``chat_runner._notice_mcp_sign_in_needed`` adds
one row naming the servers the session reports as waiting, read from the
session's own MCP report, and starts no sign-in.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.types import METHOD_KAS_MCP_STATUS, JsonRpcMessage
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard import chat_runner

_REFUSED = "Sign-in for notes could not be refreshed (token endpoint answered 401)."


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every row appended through ``append_and_surface``, as its arguments."""
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        chat_runner,
        "append_and_surface",
        lambda state, slot, role, text, cls="", **kw: seen.append(
            {"role": role, "text": text, "cls": cls, "meta": kw.get("meta")}
        ),
    )
    return seen


def _slot(*messages: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(_mcp_sign_in_notice_shown=False, messages=list(messages), key="chat-1")


def _state(live_child: str = "") -> SimpleNamespace:
    """A dashboard whose session for the chat runs the child *live_child*, or none."""
    provider = SimpleNamespace(is_process_alive=lambda: True, process_instance=live_child)
    return SimpleNamespace(
        sessions=SimpleNamespace(get_provider=lambda key: provider if live_child else None)
    )


def _client(backend: str = ACP_BACKEND_KAS) -> SimpleNamespace:
    """The chat's provider, on *backend* (KAS, where naming a server starts its sign-in)."""
    return SimpleNamespace(capabilities=capabilities_for(backend))


def _notice(
    slot: SimpleNamespace, *waiting: str, live_child: str = "", backend: str = ACP_BACKEND_KAS
) -> None:
    chat_runner._notice_mcp_sign_in_needed(
        _state(live_child), slot, {"awaiting_auth": list(waiting)}, _client(backend)
    )


def test_a_waiting_server_is_named_once_with_how_to_sign_in(rows: list) -> None:
    slot = _slot()
    _notice(slot, "notes")
    [row] = rows
    assert row["role"] == "notice"
    assert row["cls"] == "msg msg-warn"
    assert row["meta"] == {
        "kind": "mcp_sign_in_needed",
        "servers": ["notes"],
        "more": 0,
    }
    # The leading glyph is what NoticeCard reads the warn tone from. The row
    # says what the server is before it uses the word MCP, and the message to
    # send is an example, not a phrase to type word for word.
    assert row["text"].startswith("⚠ notes (an MCP server you added) needs you to sign in")
    assert row["text"].endswith('Send a message that names notes, such as "Sign in to notes".')
    assert "MCP Servers page" not in row["text"]
    # The same chat does not say it again, whatever waits later.
    _notice(slot, "notes", "tasks")
    assert len(rows) == 1


def test_several_servers_share_one_row_and_a_long_list_is_counted(rows: list) -> None:
    names = [f"server-{n}" for n in range(7)]
    _notice(_slot(), *names)
    [row] = rows
    assert row["meta"]["servers"] == names[:5]
    # The rest are counted in meta, so the chat page can say so in its own language.
    assert row["meta"]["more"] == 2
    assert row["text"].startswith(
        "⚠ These MCP servers you added need you to sign in before their tools work in "
        "this chat: server-0, server-1, server-2, server-3, server-4 and 2 more."
    )
    # Naming several in one message still starts one sign-in at a time.
    assert (
        'Each one needs its own sign-in. Send a message that names it, such as "Sign in to '
        'server-0"' in row["text"]
    )


def test_a_server_whose_sign_in_card_is_open_in_the_chat_is_left_to_that_card(rows: list) -> None:
    card = {
        "role": "mcp_oauth",
        "meta": {"server_name": "notes", "oauth_url": "https://x/a", "child": "c1"},
    }
    slot = _slot(card)
    _notice(slot, "notes", live_child="c1")
    assert rows == []
    # Nothing was shown, so a server with no card is still named later.
    _notice(slot, "notes", "tasks", live_child="c1")
    [row] = rows
    assert row["meta"]["servers"] == ["tasks"]


@pytest.mark.parametrize("live_child", ["c2", ""])
def test_a_card_whose_child_is_gone_does_not_count(rows: list, live_child: str) -> None:
    # After a gateway restart or a child that exited, the stored card keeps its
    # link and no end flag; only the read gate judges it expired. The notice
    # applies the same gate, so the server is named.
    card = {
        "role": "mcp_oauth",
        "meta": {"server_name": "notes", "oauth_url": "https://x/a", "child": "c1"},
    }
    _notice(_slot(card), "notes", live_child=live_child)
    assert [row["meta"]["servers"] for row in rows] == [["notes"]]


def test_a_finished_or_withdrawn_card_does_not_count(rows: list) -> None:
    done = {"role": "mcp_oauth", "meta": {"server_name": "notes", "completed": True}}
    _notice(_slot(done), "notes")
    assert [row["meta"]["servers"] for row in rows] == [["notes"]]


@pytest.mark.parametrize("flag", ["completed", "failed", "superseded", "expired"])
def test_a_finished_card_that_kept_its_link_does_not_count(rows: list, flag: str) -> None:
    # Marking a card finished keeps its oauth_url, and a restored transcript
    # keeps that meta, so the link alone does not mean the card still offers a way in.
    card = {
        "role": "mcp_oauth",
        "meta": {"server_name": "notes", "oauth_url": "https://x/a", flag: True},
    }
    _notice(_slot(card), "notes")
    assert [row["meta"]["servers"] for row in rows] == [["notes"]]


@pytest.mark.parametrize("payload", [None, {}, {"awaiting_auth": []}, {"awaiting_auth": [7, ""]}])
def test_nothing_waiting_shows_nothing(rows: list, payload: Any) -> None:
    slot = _slot()
    chat_runner._notice_mcp_sign_in_needed(SimpleNamespace(), slot, payload, _client())
    assert rows == []
    assert slot._mcp_sign_in_notice_shown is False


# Naming a server starts its sign-in only where the host sends the consent link
# back (``ACP_BACKENDS_OPEN_EXTERNAL_URL``, the check ``begin_mcp_sign_in`` makes
# first). Anywhere else the row's advice would start nothing.


@pytest.mark.parametrize("client", [_client(ACP_BACKEND_KIRO), SimpleNamespace(), None])
def test_no_row_where_naming_a_server_starts_no_sign_in(rows: list, client: Any) -> None:
    slot = _slot()
    chat_runner._notice_mcp_sign_in_needed(_state(), slot, {"awaiting_auth": ["notes"]}, client)
    assert rows == []
    assert slot._mcp_sign_in_notice_shown is False


def test_on_kas_the_row_shows(rows: list) -> None:
    _notice(_slot(), "notes", backend=ACP_BACKEND_KAS)
    assert [row["meta"]["servers"] for row in rows] == [["notes"]]


def test_a_server_with_a_rejected_sign_in_link_is_left_to_that_banner(rows: list) -> None:
    # The rejected banner has no link and is marked failed, but it already tells
    # the person what went wrong with this server's sign-in.
    banner = {
        "role": "mcp_oauth",
        "meta": {"server_name": "notes", "failed": True, "rejected_url": True},
    }
    _notice(_slot(banner), "notes", "tasks")
    assert [row["meta"]["servers"] for row in rows] == [["tasks"]]


def test_a_row_that_cannot_be_built_never_fails_the_turn(rows: list) -> None:
    slot = SimpleNamespace(_mcp_sign_in_notice_shown=False)  # no transcript to read
    _notice(slot, "notes")
    assert rows == []


def test_publishing_a_report_with_a_refused_sign_in_shows_the_row(
    rows: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = McpSessionReport()
    report.record_frame(
        JsonRpcMessage(
            method=METHOD_KAS_MCP_STATUS,
            params={
                "sessionId": "s1",
                "servers": [
                    {
                        "name": "notes",
                        "status": "failed",
                        "failedAuthorization": False,
                        "errorMessage": _REFUSED,
                    }
                ],
            },
        ),
        owned=True,
    )
    slot = _slot()
    slot.key = "chat-1"
    slot.set_mcp_report = lambda payload, session_id: False
    provider = SimpleNamespace(
        mcp_session_report=lambda: report, session_id="s1", capabilities=capabilities_for("kas")
    )
    chat_runner._publish_session_mcp_report(SimpleNamespace(), slot, provider)
    assert [row["meta"]["servers"] for row in rows] == [["notes"]]
