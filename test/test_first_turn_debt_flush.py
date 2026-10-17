"""A turn makes the sid's unlanded marker durable before the provider sees it.

Arming ``first_turn_history_owed`` also writes the sid's on-disk unlanded marker
on a backend whose resume ``SessionMap.get`` gates on it, but on the event loop
that write is only scheduled. The turn awaits the session map's durability point
(``SessionManager.aflush``) whenever a marker is pending, so a crash between the
arm and the prompt cannot leave the sid on disk without it. A kiro-cli session
never carries the marker, so its turns await no flush.

The turns below run against a real ``SessionManager`` and ``SessionMap``: the
marker decision comes from the backend's own provider label, not from a stub.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from turn_harness import TurnContext, TurnScript, run_turn

from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    PROVIDER_LABEL_DEFAULT,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.config import KiroCrewConfig

_REPLY = AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply")
_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
_SID = "sid-fresh"


@pytest.fixture()
def map_dir(tmp_path):
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        yield tmp_path


def _kas_label() -> str:
    from kiro_crew.providers.acp import AcpProvider, provider_label

    p = MagicMock(spec=AcpProvider)
    p.client = MagicMock()
    p.client.backend = ACP_BACKEND_KAS
    label = provider_label(p)
    assert label != PROVIDER_LABEL_DEFAULT
    return label


def _real_manager(key: str, label: str) -> Any:
    from kiro_crew.providers.acp import AcpProvider
    from kiro_crew.session import SessionManager, _Session

    mgr = SessionManager(KiroCrewConfig(), provider_factory=MagicMock())
    provider = MagicMock(spec=AcpProvider)
    provider.shutdown = AsyncMock()
    provider.client = MagicMock()
    provider.client._session_id = _SID
    mgr._sessions[mgr._fold_key(key)] = _Session(provider=provider)
    mgr._session_map.set(mgr._fold_key(key), _SID, provider=label)
    return mgr


def _wire(order: list[str], label: str, *, warm: bool = False) -> Any:
    def _setup(ctx: TurnContext) -> None:
        sessions = ctx.state.sessions
        holder: dict[str, Any] = {}
        acquire = sessions.get_or_create.side_effect

        async def _get_or_create(*args: Any, **kwargs: Any) -> tuple[Any, bool, bool]:
            provider, is_new, resumed = await acquire(*args, **kwargs)
            key = args[0] if args else kwargs["key"]
            holder["mgr"] = mgr = _real_manager(key, label)
            if warm:
                mgr.mark_first_turn_history_owed(key)
            return provider, (is_new and not warm), resumed

        def _mark(key: str) -> bool:
            order.append("mark")
            return holder["mgr"].mark_first_turn_history_owed(key)

        async def _aflush(*_args: Any, **_kwargs: Any) -> None:
            prompted = ctx.provider is not None and bool(ctx.provider.recorded("stream"))
            order.append("aflush after prompt" if prompted else "aflush")
            await holder["mgr"].aflush()

        sessions.get_or_create.side_effect = _get_or_create
        sessions.mark_first_turn_history_owed.side_effect = _mark
        sessions.unlanded_marker_pending.side_effect = lambda key: holder[
            "mgr"
        ].unlanded_marker_pending(key)
        sessions.aflush.side_effect = _aflush

    return _setup


@pytest.mark.asyncio
async def test_a_marker_honouring_backend_flushes_before_the_prompt(map_dir):
    order: list[str] = []
    record = await run_turn(TurnScript(events=[_REPLY, _DONE], setup=_wire(order, _kas_label())))
    assert record.provider_calls, "the turn reached the provider"
    assert order == ["mark", "aflush"]


@pytest.mark.asyncio
async def test_a_kiro_cli_turn_awaits_no_flush(map_dir):
    order: list[str] = []
    record = await run_turn(
        TurnScript(events=[_REPLY, _DONE], setup=_wire(order, PROVIDER_LABEL_DEFAULT))
    )
    assert record.provider_calls, "the turn reached the provider"
    assert order == ["mark"]


@pytest.mark.asyncio
async def test_a_warm_retry_with_the_marker_still_pending_flushes_before_the_prompt(map_dir):
    order: list[str] = []
    record = await run_turn(
        TurnScript(events=[_REPLY, _DONE], setup=_wire(order, _kas_label(), warm=True))
    )
    assert record.provider_calls, "the turn reached the provider"
    assert order == ["aflush"]


def test_the_marker_is_written_only_where_get_honours_it(map_dir):
    from kiro_crew.session_map import SessionMap

    sm = SessionMap()
    sm.set("dashboard:kas", _SID, provider=_kas_label())
    sm.set("dashboard:kiro", _SID, provider=PROVIDER_LABEL_DEFAULT)
    assert sm.mark_sid_unlanded("dashboard:kas", _SID) is True
    assert sm.sid_unlanded_pending("dashboard:kas") is True
    assert sm.mark_sid_unlanded("dashboard:kiro", _SID) is False
    assert sm.sid_unlanded_pending("dashboard:kiro") is False
