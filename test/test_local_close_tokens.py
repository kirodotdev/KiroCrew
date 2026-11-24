"""A close with no native session id is kept under a local token, never sent.

A Bot chat's ``session/new`` timed out with its app servers up, while another chat
holds a lease on the same shared runtime. The close is kept under a token such as
``unconfirmed-teardown:<id>``; sent to the host it would be answered "Session not
found" and read as confirmed. Each test names the change that turns it red:

* the kept handle judges the runtime's tree and never asks the host -- drop
  ``close_handle`` from ``note_unconfirmed_close``;
* a real native id that arrives later replaces the token's handle -- drop the
  replacement in ``merge_close_handle``;
* no host close verb sends a token -- drop the ``is_local_close_token`` guard in
  ``runtime_session_registry``; or build a token prefix outside the listed places.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from test_bot_chat_app_tools import APP, KEY, _fresh_record  # noqa: F401

from kiro_crew import bot_chat_app_tools as tools
from kiro_crew.acp.session_provider_shared import (
    LOCAL_CLOSE_TOKEN_PREFIXES,
    RuntimeTreeGone,
    is_local_close_token,
)


class SharedRuntime:
    """A live shared runtime whose host answers "not found" for any id."""

    def __init__(self) -> None:
        self.evicted: list[str] = []
        self.process_tree_confirmed_dead = False

    def is_alive(self) -> bool:
        return True

    async def evict_session(self, session_id: str) -> bool:
        self.evicted.append(session_id)
        return True  # "Session not found"


def _keep(rt, sid):
    tools._SESSION_APP_SNAPSHOT_PENDING[KEY] = ("atlas", frozenset({APP}))
    tools.note_unconfirmed_close(KEY, rt, sid)


def test_a_close_with_no_id_is_judged_by_the_tree_and_never_sent():
    rt = SharedRuntime()
    _keep(rt, None)
    ((handle, token),) = tools._PENDING_CLOSE[("atlas", APP)]
    assert is_local_close_token(token)
    assert isinstance(handle, RuntimeTreeGone) and handle.runtime is rt
    asyncio.run(tools._drain_pending_close())
    assert rt.evicted == []  # the token never reached the host
    assert tools._PENDING_CLOSE[("atlas", APP)] == [(handle, token)]  # still pending


def test_a_late_native_id_replaces_the_token_and_settles_on_the_host():
    # A process owner that runs this one chat's one session (``AcpClient``): the id
    # it reports later is that start's own.
    rt = SharedRuntime()
    rt._session_key = KEY
    _keep(rt, None)
    tools.note_unconfirmed_close(KEY, rt, "sess-real")  # the start got its id late
    assert tools._PENDING_CLOSE[("atlas", APP)] == [(rt, "sess-real")]
    asyncio.run(tools._drain_pending_close())
    assert rt.evicted == ["sess-real"]
    assert ("atlas", APP) not in tools._PENDING_CLOSE


def test_another_sessions_id_on_a_shared_runtime_never_takes_over_a_token():
    # A runtime many chats share: a later session's close there names some other
    # session, so the start's token keeps its tree handle. Replace by owner alone
    # and the drain settles the other session and drops this start's close.
    rt = SharedRuntime()
    _keep(rt, None)
    tools.note_unconfirmed_close(KEY, rt, "sess-other")
    held = tools._PENDING_CLOSE[("atlas", APP)]
    assert any(is_local_close_token(s) and isinstance(h, RuntimeTreeGone) for h, s in held)
    asyncio.run(tools._drain_pending_close())
    assert any(is_local_close_token(s) for _h, s in tools._PENDING_CLOSE[("atlas", APP)])


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", LOCAL_CLOSE_TOKEN_PREFIXES)
@pytest.mark.parametrize("verb", ["evict_session", "terminate_session"])
async def test_no_host_close_verb_sends_a_local_token(tmp_path, monkeypatch, prefix, verb):
    from kiro_crew.acp.runtime import AcpRuntime

    rt = AcpRuntime(work_dir=str(tmp_path))
    sent: list[object] = []

    async def _request(*a, **_kw):
        sent.append(a)
        return {}

    monkeypatch.setattr(rt, "_request", _request, raising=False)
    monkeypatch.setattr(rt, "_notify", _request, raising=False)
    result = await getattr(rt, verb)(f"{prefix}123")
    assert sent == []
    if verb == "evict_session":
        assert result is False


def test_a_token_prefix_is_built_only_where_its_handle_is_local():
    """Each token prefix is written in two places only: the prefix list itself and
    the client start's ``note_unobserved_kill`` (whose handle never asks a host).
    Anything else must go through ``close_handle``."""
    src = Path(tools.__file__).parent
    allowed = {
        ("acp/session_provider_shared.py", "unconfirmed-teardown:"),
        ("acp/session_provider_shared.py", "unconfirmed-start:"),
        ("bot_chat_ending_fence.py", "unconfirmed-start:"),
    }
    found = set()
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for prefix in LOCAL_CLOSE_TOKEN_PREFIXES:
            if re.search(re.escape(prefix), text):
                found.add((path.relative_to(src).as_posix(), prefix))
    assert found <= allowed, found - allowed
