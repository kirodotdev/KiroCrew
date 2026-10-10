"""Coverage for ``kiro_crew.autonudge_authz`` guard clauses and audit fallbacks.

``test/test_workflows_nudge_wiring.py`` already pins the happy path and the
headline Discord/dashboard denials. This file targets the remaining rejection
branches of BOTH chokepoints — the ones an attacker or a broken caller reaches
first — plus the two "auditing must never break the flow" fallbacks and the
``svc`` failure paths, where a swallowed exception would hide a security event.

Every test patches ``sel`` so nothing is written to the real security event log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import autonudge_authz
from kiro_crew.autonudge import (
    AutoNudgeService,
    MonitorUpdateConflict,
    NudgeAdmissionReason,
    NudgeAdmissionRefused,
    NudgeLoop,
)
from kiro_crew.autonudge_authz import (
    authorize_and_add_nudge,
    authorize_and_update_nudge,
    normalize_banner,
)
from kiro_crew.autonudge_service.model import SERVICE_SHUTTING_DOWN_MESSAGE
from kiro_crew.constants import MAX_BANNER_CHARS
from kiro_crew.dashboard.handlers import autonudge as autonudge_handler
from kiro_crew.monitoring.models import (
    MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS,
    MonitorOutcome,
    MonitorState,
)


class RecordingSvc:
    """Minimal AutoNudgeService stand-in for both chokepoints."""

    def __init__(
        self,
        *,
        loop: Any = None,
        add_error: Exception | None = None,
        update_error: Exception | None = None,
    ) -> None:
        self._loop = loop
        self._add_error = add_error
        self._update_error = update_error
        self.added: list[dict] = []
        self.updated: list[dict] = []

    def get_by_slot(self, slot_key: str) -> Any:
        return self._loop

    def get_by_id(self, loop_id: str) -> Any:
        """Present because the update chokepoint calls it DIRECTLY, not behind a probe."""
        return self._loop

    async def add(self, **kw: Any) -> Any:
        if self._add_error is not None:
            raise self._add_error
        self.added.append(kw)
        return self._loop or SimpleNamespace(
            id="loop-1",
            slot_key=kw["slot_key"],
            idle_secs=kw["idle_secs"],
            max_cycles=kw["max_cycles"],
        )

    async def update(self, loop_id: str, **kw: Any) -> Any:
        if self._update_error is not None:
            raise self._update_error
        self.updated.append({"loop_id": loop_id, **kw})
        return self._loop


def _state(
    *, slots: dict | None = None, sessions: Any = None, transports: dict | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        _slots=slots if slots is not None else {},
        sessions=sessions,
        channel_transports=transports if transports is not None else {},
    )


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture SEL events instead of writing them."""
    events: list[dict] = []
    monkeypatch.setattr(
        autonudge_authz,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
    )
    return events


@pytest.fixture
def broken_sel(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SEL whose every write raises — exercises the swallow-and-warn fallbacks."""

    def _raise() -> Any:
        raise RuntimeError("SEL unavailable")

    monkeypatch.setattr(autonudge_authz, "sel", _raise)


# --------------------------------------------------------------------------- #
# authorize_and_update_nudge
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_update_without_service_is_a_503_error_event(audits: list[dict]) -> None:
    loop, error, status = await authorize_and_update_nudge(
        svc=None, loop_id="l1", message="hi", source="dashboard"
    )
    assert loop is None and status == 503 and "auto-nudge disabled" in error
    assert [a["outcome"] for a in audits] == ["error"]


@pytest.mark.asyncio
async def test_update_requires_a_loop_id(audits: list[dict]) -> None:
    loop, error, status = await authorize_and_update_nudge(
        svc=RecordingSvc(), loop_id="   ", message="hi", source="dashboard"
    )
    assert loop is None and status == 400 and error == "loop_id required"
    assert audits and audits[0]["outcome"] == "denied"


@pytest.mark.asyncio
async def test_update_rejects_runtime_budget_over_the_ceiling(audits: list[dict]) -> None:
    svc = RecordingSvc()
    loop, error, status = await authorize_and_update_nudge(
        svc=svc,
        loop_id="l1",
        max_runtime_secs=604_801,
        source="dashboard",
    )
    assert loop is None and status == 400 and "604800" in error
    assert svc.updated == []  # never applied


@pytest.mark.asyncio
async def test_update_accepts_a_whole_number_float_budget_as_an_int(audits: list[dict]) -> None:
    """``3600.0`` is how a JSON body may spell an integer; the store receives an int."""
    svc = RecordingSvc()
    await authorize_and_update_nudge(
        svc=svc, loop_id="l1", max_runtime_secs=3600.0, source="dashboard"
    )
    assert len(svc.updated) == 1
    budget = svc.updated[0]["max_runtime_secs"]
    assert budget == 3600 and type(budget) is int


@pytest.mark.asyncio
async def test_update_forwards_fresh_run_and_leaves_it_off_by_default(audits: list[dict]) -> None:
    """The resume flag reaches the service exactly as the caller stated it: the
    dashboard route passes True, the ``monitor_update`` applier passes nothing."""
    svc = RecordingSvc()
    await authorize_and_update_nudge(svc=svc, loop_id="l1", active=True, source="dashboard")
    await authorize_and_update_nudge(
        svc=svc, loop_id="l1", active=True, fresh_run=True, source="dashboard"
    )
    assert [u["fresh_run"] for u in svc.updated] == [False, True]


@pytest.mark.asyncio
async def test_update_audit_failure_does_not_break_the_denial(broken_sel: None) -> None:
    """A dead SEL must not turn a 400 into a 500: the warn-and-continue fallback
    keeps the caller's error contract intact."""
    loop, error, status = await authorize_and_update_nudge(
        svc=RecordingSvc(), loop_id="", source="dashboard"
    )
    assert loop is None and status == 400 and error == "loop_id required"


@pytest.mark.asyncio
async def test_update_audits_then_reraises_a_service_failure(audits: list[dict]) -> None:
    """``svc.update`` blowing up must leave an ``error`` event behind before the
    exception propagates — a silent failure would lose the security record."""
    svc = RecordingSvc(update_error=RuntimeError("store wedged"))
    with pytest.raises(RuntimeError, match="store wedged"):
        await authorize_and_update_nudge(svc=svc, loop_id="l1", message="hi", source="dashboard")
    errors = [a for a in audits if a["outcome"] == "error"]
    assert errors and "svc.update failed: RuntimeError" in errors[0]["error"]


@pytest.mark.asyncio
async def test_update_maps_closed_admission_to_503(audits: list[dict], tmp_path: Path) -> None:
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.shutdown()

    loop, error, status = await authorize_and_update_nudge(
        svc=svc,
        loop_id="loop-1",
        message="new",
        source="dashboard",
    )

    assert (loop, error, status) == (None, "AutoNudge service is shutting down", 503)
    assert audits[-1]["outcome"] == "denied"


@pytest.mark.asyncio
async def test_closed_service_arm_preserves_active_loop_stop_sentinel_and_state(
    audits: list[dict], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    slot_key = "chat-1-1"
    sentinel = tmp_path / "stop-chat-1-1"
    svc = AutoNudgeService(base_dir=tmp_path / "home")
    existing = await svc.add(
        slot_key=slot_key,
        message="existing goal",
        idle_secs=300,
        stop_sentinel_path=str(sentinel),
    )
    await svc.shutdown()
    sentinel.write_text("stop", encoding="utf-8")
    before_memory = deepcopy(existing)
    before_store = await asyncio.to_thread(svc._path.read_bytes)
    trust_writes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        autonudge_authz,
        "resolve_stop_sentinel",
        lambda key, *args, **kwargs: str(sentinel),
    )
    monkeypatch.setattr(
        autonudge_authz,
        "record_self_arm",
        lambda loop_id, key: trust_writes.append((loop_id, key)),
    )
    monkeypatch.setattr(autonudge_authz, "forget_self_arm", lambda _loop_id: None)
    member_state = _state(
        slots={
            slot_key: SimpleNamespace(
                workspace="default",
                mode="member",
                memory_mode="persistent",
                is_closing=False,
            )
        }
    )
    refused, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=member_state,
        slot_key=slot_key,
        message="self-armed replacement",
        source="mcp-directive",
        initiator_slot_key=slot_key,
    )
    assert (refused, error, status) == (None, SERVICE_SHUTTING_DOWN_MESSAGE, 503)
    assert [event["outcome"] for event in audits] == ["denied"]
    assert trust_writes == [], "closed admission reached the self-arm trust store"
    audits.clear()

    monkeypatch.setattr(autonudge_handler, "_autonudge_get", lambda: svc)
    monkeypatch.setattr(
        autonudge_handler,
        "sel",
        lambda: SimpleNamespace(log_api_access=lambda **kwargs: None),
    )
    request_state = _state(
        slots={
            slot_key: SimpleNamespace(
                workspace="default",
                mode="",
                memory_mode="persistent",
                is_closing=False,
            )
        }
    )
    request_state.owner_id = ""
    app = web.Application()
    app["state"] = request_state
    request = make_mocked_request("POST", "/api/autonudge", app=app)
    request["user"] = "local-app"
    request["app"] = ""
    request.json = AsyncMock(  # type: ignore[method-assign]
        return_value={"slot_key": slot_key, "message": "replacement goal"}
    )

    try:
        response = await autonudge_handler.api_autonudge_start(request)

        assert response.status == 503
        assert isinstance(response.body, bytes)
        assert json.loads(response.body.decode("utf-8")) == {
            "error": SERVICE_SHUTTING_DOWN_MESSAGE,
            "code": "autonudge_not_armed",
        }
        assert sentinel.read_text(encoding="utf-8") == "stop"
        assert svc.get_by_slot(slot_key) == before_memory
        assert await asyncio.to_thread(svc._path.read_bytes) == before_store
        assert [event["outcome"] for event in audits] == ["denied"]
        assert trust_writes == [], "closed admission reached the self-arm trust store"
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_add_racing_shutdown_keeps_existing_default_stop_sentinel(
    audits: list[dict], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    slot_key = "chat-1-1"
    sentinel = tmp_path / "stop-chat-1-1"
    svc = AutoNudgeService(base_dir=tmp_path / "home")
    existing = await svc.add(
        slot_key=slot_key,
        message="existing goal",
        idle_secs=300,
        stop_sentinel_path=str(sentinel),
    )
    sentinel.write_text("stop", encoding="utf-8")
    monkeypatch.setattr(
        autonudge_authz,
        "resolve_stop_sentinel",
        lambda key, *args, **kwargs: str(sentinel),
    )
    admitted_add = svc.add
    retired_paths: list[Any] = []

    async def close_then_add(**kwargs: Any) -> NudgeLoop:
        retired_paths.append(kwargs.get("retire_stale_stop_sentinel"))
        await svc.shutdown()
        return await admitted_add(**kwargs)

    monkeypatch.setattr(svc, "add", close_then_add)
    state = _state(
        slots={
            slot_key: SimpleNamespace(
                workspace="default",
                mode="",
                memory_mode="persistent",
                is_closing=False,
            )
        }
    )

    try:
        loop, error, status = await authorize_and_add_nudge(
            svc=svc,
            state=state,
            slot_key=slot_key,
            message="replacement goal",
            source="dashboard",
        )

        assert (loop, error, status) == (
            None,
            SERVICE_SHUTTING_DOWN_MESSAGE,
            503,
        )
        # The path reached the transaction, which refused before its write, so the
        # removal that follows the write never ran.
        assert retired_paths == [sentinel]
        assert sentinel.read_text(encoding="utf-8") == "stop"
        assert svc.get_by_slot(slot_key) is existing
        assert [event["outcome"] for event in audits] == ["invoked", "denied"]
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_add_keeps_session_changed_admission_as_409(
    audits: list[dict], tmp_path: Path
) -> None:
    svc = RecordingSvc(
        add_error=NudgeAdmissionRefused(
            "session changed before nudge arm committed",
            reason=NudgeAdmissionReason.SESSION_CHANGED,
        )
    )

    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(
            slots={
                "chat-1-1": SimpleNamespace(
                    workspace="default",
                    mode="",
                    memory_mode="persistent",
                    is_closing=False,
                )
            }
        ),
        slot_key="chat-1-1",
        message="watch",
        stop_sentinel_path=str(tmp_path / "stop"),
        source="dashboard",
    )

    assert (loop, error, status) == (
        None,
        "session changed before nudge arm committed",
        409,
    )
    assert audits[-1]["outcome"] == "denied"


@pytest.mark.asyncio
async def test_update_monitor_maps_closed_admission_to_503(
    audits: list[dict], tmp_path: Path
) -> None:
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.shutdown()

    loop, error, status = await autonudge_authz.authorize_and_update_monitor(
        svc=svc,
        state=_state(slots={"chat-1-1": SimpleNamespace(mode="", memory_mode="persistent")}),
        loop_id="monitor-1",
        session_key="chat-1-1",
        patch={"cadence_secs": 300},
        source="dashboard",
    )

    assert (loop, error, status) == (None, "AutoNudge service is shutting down", 503)
    assert audits[-1]["outcome"] == "denied"


@pytest.mark.asyncio
async def test_stop_monitor_maps_closed_admission_to_503(
    audits: list[dict], tmp_path: Path
) -> None:
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.shutdown()

    loop, error, status = await autonudge_authz.authorize_and_stop_monitor(
        svc=svc,
        loop_id="monitor-1",
        session_key="chat-1-1",
        source="dashboard",
    )

    assert (loop, error, status) == (None, "AutoNudge service is shutting down", 503)
    assert audits[-1]["outcome"] == "denied"


@pytest.mark.asyncio
async def test_clear_monitor_maps_closed_admission_to_503(
    audits: list[dict], tmp_path: Path
) -> None:
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.shutdown()
    monitor = MonitorState(
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        created_ts=1_000.0,
        outcome=MonitorOutcome.USER_STOP,
    )
    loop = NudgeLoop(
        id="monitor-1",
        slot_key="chat-1-1",
        message="watch",
        active=False,
        monitor=monitor,
    )
    svc._loops[loop.id] = loop

    cleared, error, status = await autonudge_authz.authorize_and_clear_monitor(
        svc=svc,
        loop_id=loop.id,
        session_key=loop.slot_key,
        source="dashboard",
    )

    assert (cleared, error, status) == (False, "AutoNudge service is shutting down", 503)
    assert svc.get_by_id(loop.id) is loop
    assert audits[-1]["outcome"] == "denied"


# --------------------------------------------------------------------------- #
# authorize_and_add_nudge — guard clauses
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_add_without_service_is_a_503_error_event(audits: list[dict]) -> None:
    loop, error, status = await authorize_and_add_nudge(
        svc=None, state=_state(), slot_key="chat-1-1", message="watch", source="dashboard"
    )
    assert loop is None and status == 503 and "auto-nudge disabled" in error
    assert [a["outcome"] for a in audits] == ["error"]


@pytest.mark.asyncio
async def test_add_requires_both_slot_key_and_message(audits: list[dict]) -> None:
    svc = RecordingSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default", is_closing=False)}),
        slot_key="chat-1-1",
        message="   ",
        source="dashboard",
    )
    assert loop is None and status == 400 and "required" in error
    assert svc.added == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["crew", "member"])
async def test_add_rejects_dashboard_modes_without_direct_turn_ingress(
    audits: list[dict], mode: str
) -> None:
    svc = RecordingSvc()
    slot = SimpleNamespace(
        workspace="default", mode=mode, memory_mode="persistent", is_closing=False
    )

    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(slots={"chat-1-1": slot}),
        slot_key="chat-1-1",
        message="watch",
        source="dashboard",
    )

    assert loop is None and status == 409
    assert f"{mode}-mode" in error
    assert svc.added == []


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_mode", ["incognito", "temporary"])
async def test_add_rejects_restricted_dashboard_sessions(
    audits: list[dict], memory_mode: str
) -> None:
    svc = RecordingSvc()
    slot = SimpleNamespace(workspace="default", mode="", memory_mode=memory_mode, is_closing=False)

    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(slots={"chat-1-1": slot}),
        slot_key="chat-1-1",
        message="watch",
        source="dashboard",
    )

    assert loop is None and status == 403
    assert "incognito and temporary" in error
    assert svc.added == []


@pytest.mark.asyncio
async def test_dashboard_admission_rechecks_mode_and_memory_boundary(
    audits: list[dict], tmp_path: Path
) -> None:
    svc = RecordingSvc()
    slot = SimpleNamespace(workspace="default", mode="", memory_mode="persistent", is_closing=False)
    state = _state(slots={"chat-1-1": slot})

    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=state,
        slot_key="chat-1-1",
        message="watch",
        stop_sentinel_path=str(tmp_path / "stop"),
        source="dashboard",
    )

    assert loop is not None and error is None and status == 200
    admission_check = svc.added[0]["admission_check"]
    assert admission_check()
    slot.mode = "crew"
    assert not admission_check()
    slot.mode = ""
    slot.memory_mode = "temporary"
    assert not admission_check()


@pytest.mark.asyncio
async def test_add_rejects_a_non_integer_runtime_budget(audits: list[dict]) -> None:
    svc = RecordingSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default", is_closing=False)}),
        slot_key="chat-1-1",
        message="watch",
        max_runtime_secs="not-a-number",  # type: ignore[arg-type]
        source="dashboard",
    )
    assert (
        loop is None
        and status == 400
        and error == "max_runtime_secs must be an integer between 0 and 604800 (7 days)"
    )
    assert svc.added == []


@pytest.mark.asyncio
async def test_add_accepts_a_whole_number_float_budget_as_an_int(audits: list[dict]) -> None:
    svc = RecordingSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default", is_closing=False)}),
        slot_key="chat-1-1",
        message="watch",
        max_runtime_secs=3600.0,  # type: ignore[arg-type]
        source="dashboard",
    )
    assert error is None and status == 200
    budget = svc.added[0]["max_runtime_secs"]
    assert budget == 3600 and type(budget) is int


@pytest.mark.asyncio
async def test_add_audit_failure_does_not_break_the_denial(broken_sel: None) -> None:
    svc = RecordingSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc, state=_state(slots={}), slot_key="chat-nope", message="watch", source="dashboard"
    )
    assert loop is None and status == 404 and "unknown slot" in error
    assert svc.added == []


@pytest.mark.asyncio
async def test_add_rejects_an_unroutable_slack_session(audits: list[dict]) -> None:
    """A Slack loop with no routable session would fire into the void — and the
    ``sessions`` registry being absent entirely must deny, not crash."""
    svc = RecordingSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(sessions=None),
        slot_key="slack:1712345.6789",
        message="watch",
        source="dashboard",
    )
    assert loop is None and status == 404 and "unknown slack session" in error

    unknown = _state(sessions=SimpleNamespace(get_channel=lambda key: None))
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=unknown,
        slot_key="slack:1712345.6789",
        message="watch",
        source="dashboard",
    )
    assert loop is None and status == 404
    assert svc.added == []


@pytest.mark.asyncio
async def test_add_denies_discord_when_the_transport_is_not_running(audits: list[dict]) -> None:
    svc = RecordingSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(transports={}),
        slot_key="discord:kirocrew:direct:42",
        message="watch",
        source="dashboard",
    )
    assert loop is None and status == 404 and "discord transport not running" in error
    assert svc.added == []


@pytest.mark.asyncio
async def test_add_denies_webex_when_the_transport_is_not_running(audits: list[dict]) -> None:
    """Deny-by-default, mirroring the Discord branch: an authenticated caller must
    not be able to mint a loop that DMs an arbitrary Webex user through the agent."""
    svc = RecordingSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(transports={}),
        slot_key="webex:kirocrew:direct:user@example.com",
        message="watch",
        source="dashboard",
    )
    assert loop is None and status == 404 and "webex transport not running" in error
    assert svc.added == []


@pytest.mark.asyncio
async def test_add_denies_a_non_dm_webex_session(audits: list[dict]) -> None:
    """A space session is a shared audience, so it is never an arm target."""
    svc = RecordingSvc()
    transport = SimpleNamespace(
        dispatcher=SimpleNamespace(current_session_key=lambda _e: ""),
        is_authorized=lambda _e: True,
    )
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(transports={"webex": transport}),
        slot_key="webex:kirocrew:forum:space:ROOM1",
        message="watch",
        source="dashboard",
    )
    assert loop is None and status == 400 and "DM sessions only" in error
    assert svc.added == []


@pytest.mark.asyncio
async def test_add_denies_a_webex_user_off_the_allowlist(audits: list[dict]) -> None:
    svc = RecordingSvc()
    transport = SimpleNamespace(
        dispatcher=SimpleNamespace(current_session_key=lambda _e: ""),
        is_authorized=lambda _e: False,
    )
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(transports={"webex": transport}),
        slot_key="webex:kirocrew:direct:intruder@example.com",
        message="watch",
        source="dashboard",
    )
    assert loop is None and status == 403 and "allowed_emails" in error
    assert svc.added == []


@pytest.mark.asyncio
async def test_add_denies_a_webex_key_that_is_not_the_users_current_session(
    audits: list[dict],
) -> None:
    """Blocks spoofing another ``webex:`` session, exactly as Discord's does."""
    svc = RecordingSvc()
    transport = SimpleNamespace(
        dispatcher=SimpleNamespace(
            current_session_key=lambda _e: "webex:kirocrew:direct:user@example.com:gen3"
        ),
        is_authorized=lambda _e: True,
    )
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(transports={"webex": transport}),
        slot_key="webex:kirocrew:direct:user@example.com",
        message="watch",
        source="dashboard",
    )
    assert loop is None and status == 404 and "current session" in error
    assert svc.added == []


@pytest.mark.asyncio
async def test_add_webex_rechecks_session_ownership_at_commit_time(
    audits: list[dict], tmp_path: Path
) -> None:
    """An allow-listed Webex session removed during cleanup must not be
    recreated by an arm request that passed the initial authorization check."""
    slot_key = "webex:kirocrew:direct:user@example.com"
    dispatcher = SimpleNamespace(current_session_key=lambda _email: slot_key)
    transport = SimpleNamespace(
        dispatcher=dispatcher,
        is_authorized=lambda _email: True,
    )
    state = _state(transports={"webex": transport})
    svc = RecordingSvc()

    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=state,
        slot_key=slot_key,
        message="watch",
        stop_sentinel_path=str(tmp_path / "stop-webex"),
        source="dashboard",
    )

    assert error is None and status == 200 and loop is not None
    admission_check = svc.added[0]["admission_check"]
    assert admission_check()

    state.channel_transports["webex"] = SimpleNamespace(
        dispatcher=dispatcher,
        is_authorized=lambda _email: True,
    )
    assert not admission_check()


@pytest.mark.asyncio
async def test_add_denies_a_non_dm_discord_session(audits: list[dict]) -> None:
    """Only DM sessions are nudge-able; a guild/channel-shaped key must be
    refused before the allowlist check so it can never reach a public channel."""
    svc = RecordingSvc()
    dispatcher = SimpleNamespace(
        is_authorized=lambda uid: True,
        current_session_key=lambda uid: "discord:kirocrew:guild:42",
    )
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(transports={"discord": SimpleNamespace(dispatcher=dispatcher)}),
        slot_key="discord:kirocrew:guild:42",
        message="watch",
        source="dashboard",
    )
    assert loop is None and status == 400 and "DM sessions only" in error
    assert svc.added == []


@pytest.mark.asyncio
async def test_add_denies_discord_when_the_current_session_lookup_raises(
    audits: list[dict],
) -> None:
    """A dispatcher that throws must fail CLOSED: the unresolved current key is
    treated as empty, so the requested key cannot match it."""
    svc = RecordingSvc()

    def _boom(uid: str) -> str:
        raise RuntimeError("gateway not connected")

    dispatcher = SimpleNamespace(is_authorized=lambda uid: True, current_session_key=_boom)
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(transports={"discord": SimpleNamespace(dispatcher=dispatcher)}),
        slot_key="discord:kirocrew:direct:42",
        message="watch",
        source="dashboard",
    )
    assert loop is None and status == 404 and "current session" in error
    assert svc.added == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "slot_key",
    [
        "telegram:9001",
        "whatsapp:kirocrew:direct:15550100",
        "unified:kirocrew",
        "teams:kirocrew:direct:29:1abcdef",
        "weixin:kirocrew:direct:oUserOpenId",
        "imessage:kirocrew:direct:+15550100",
    ],
)
async def test_add_rejects_a_channel_transport_it_cannot_authorize(
    audits: list[dict], slot_key: str
) -> None:
    """``is_channel_key`` classifies every proactive-capable namespace, but only
    Slack, Discord and Webex have ownership checks here — anything else must be
    refused rather than falling through to an unvalidated arm.

    Widening the classifier deliberately does NOT widen this chokepoint: a
    namespace becomes armable only together with an ownership check here and a
    fire route in the gateway's ``_fire`` dispatcher.
    """
    svc = RecordingSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc, state=_state(), slot_key=slot_key, message="watch", source="dashboard"
    )
    assert loop is None and status == 400 and "unsupported channel session" in error
    assert svc.added == []


@pytest.mark.asyncio
async def test_add_rejects_a_sensitive_stop_sentinel_path(audits: list[dict]) -> None:
    """``stop_sentinel_path`` is unlinked by the loop, so a credential path would
    turn an arm request into a delete of the caller's key material."""
    svc = RecordingSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default", is_closing=False)}),
        slot_key="chat-1-1",
        message="watch",
        stop_sentinel_path=str(Path.home() / ".ssh" / "id_rsa"),
        source="dashboard",
    )
    assert loop is None and status == 400 and "sensitive" in error
    assert svc.added == []


@pytest.mark.asyncio
async def test_add_removes_a_stale_default_sentinel_only_after_successful_arm(
    audits: list[dict], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A new channel loop gets its per-session sentinel and clears a stale marker
    inside its add, after the row is written and before its timer is armed."""
    sentinel = tmp_path / "stop-slack"
    sentinel.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(
        autonudge_authz, "resolve_stop_sentinel", lambda key, *a, **kw: str(sentinel)
    )
    svc = AutoNudgeService(base_dir=tmp_path / "home")
    real_write = svc._write_state
    present_at_write: list[bool] = []

    def _observing_write(payload: dict) -> None:
        present_at_write.append(sentinel.exists())
        real_write(payload)

    monkeypatch.setattr(svc, "_write_state", _observing_write)
    channel = SimpleNamespace()
    try:
        loop, error, status = await authorize_and_add_nudge(
            svc=svc,
            state=_state(sessions=SimpleNamespace(get_channel=lambda key: channel)),
            slot_key="slack:1712345.6789",
            message="watch",
            source="workflow",
        )
        assert error is None and status == 200 and loop is not None
        assert loop.stop_sentinel_path == str(sentinel)
        assert present_at_write == [True], "the stale sentinel went before the row was written"
        assert not sentinel.exists(), "the successful arm left its stale sentinel in place"
        assert loop.active and loop.id in svc._timers
    finally:
        svc.stop()


#: Bound on every wait the stale-sentinel tests below make, so a regression fails
#: the run instead of hanging it.
_LOST_RUN_SECS = 10.0


def _slot_state(slot_key: str) -> SimpleNamespace:
    return _state(slots={slot_key: SimpleNamespace(workspace="default", is_closing=False)})


async def _finish_by_stop_file(
    svc: AutoNudgeService, state: SimpleNamespace, slot_key: str, sentinel: Path
) -> NudgeLoop:
    """Arm a goal on its default sentinel, write the file, and let a tick finish it."""
    from kiro_crew.autonudge import STOP_SENTINEL_REASON

    first, error, status = await authorize_and_add_nudge(
        svc=svc, state=state, slot_key=slot_key, message="first goal", source="dashboard"
    )
    assert error is None and status == 200 and first is not None
    assert first.stop_sentinel_path == str(sentinel)
    sentinel.write_text("goal met", encoding="utf-8")
    svc._cancel_timer(first.id)
    await svc._timer(first, delay=0)
    assert first.stopped_reason == STOP_SENTINEL_REASON and sentinel.exists()
    return first


@pytest.mark.asyncio
async def test_a_new_goal_after_a_stop_file_finish_is_not_killed_by_the_stale_file(
    audits: list[dict], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The stop file finishes a loop and is left on disk; the record is kept, not
    removed. The per-slot sentinel path is the same for the next goal on that
    slot, so the person's sequence -- Clear, then set a new goal -- must not
    end on the new loop's first tick. The add transaction removes the stale
    file once the replacement is written, which is what this pins end to end."""
    from kiro_crew.autonudge import STOP_SENTINEL_REASON, AutoNudgeService

    sentinel = tmp_path / ".stop-chat-1-1"
    monkeypatch.setattr(
        autonudge_authz, "resolve_stop_sentinel", lambda key, *a, **kw: str(sentinel)
    )
    svc = AutoNudgeService(base_dir=tmp_path / "home")
    await svc.start()
    state = _state(slots={"chat-1-1": SimpleNamespace(workspace="default", is_closing=False)})
    try:
        first, error, status = await authorize_and_add_nudge(
            svc=svc, state=state, slot_key="chat-1-1", message="first goal", source="dashboard"
        )
        assert error is None and status == 200 and first is not None
        assert first.stop_sentinel_path == str(sentinel)
        sentinel.write_text("goal met", encoding="utf-8")
        svc._cancel_timer(first.id)
        await svc._timer(first, delay=0)
        kept = svc.get_by_slot("chat-1-1")
        assert kept is not None and kept.stopped_reason == STOP_SENTINEL_REASON
        assert sentinel.exists()
        # The person clears the finished goal, then sets the next one.
        assert await svc.remove(first.id, stop_reason="dashboard_delete")
        second, error, status = await authorize_and_add_nudge(
            svc=svc, state=state, slot_key="chat-1-1", message="second goal", source="dashboard"
        )
        assert error is None and status == 200 and second is not None
        assert not sentinel.exists(), "the successful arm left the stale stop file in place"
        svc._cancel_timer(second.id)
        await svc._timer(second, delay=0)
        live = svc.get_by_slot("chat-1-1")
        assert live is not None and live.id == second.id and live.active
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_caller_cancelled_during_the_add_leaves_no_stale_sentinel_and_the_goal_runs(
    audits: list[dict], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A caller cancelled while its replacement add is being written (a client that
    disconnected) still gets the finished goal's stop file removed: the removal
    runs inside the add's shielded transaction, not after the caller's await, so
    the committed goal's first tick runs it instead of finishing it."""
    slot_key = "chat-1-1"
    sentinel = tmp_path / ".stop-chat-1-1"
    monkeypatch.setattr(
        autonudge_authz, "resolve_stop_sentinel", lambda key, *a, **kw: str(sentinel)
    )
    svc = AutoNudgeService(base_dir=tmp_path / "home")
    await svc.start()
    state = _slot_state(slot_key)
    try:
        first = await _finish_by_stop_file(svc, state, slot_key, sentinel)
        real_write = svc._write_state
        write_started = threading.Event()
        release_write = threading.Event()

        def _held_write(payload: dict) -> None:
            write_started.set()
            if not release_write.wait(_LOST_RUN_SECS):
                raise AssertionError("the test did not release the writer")
            real_write(payload)

        monkeypatch.setattr(svc, "_write_state", _held_write)
        caller = asyncio.create_task(
            authorize_and_add_nudge(
                svc=svc, state=state, slot_key=slot_key, message="second goal", source="dashboard"
            )
        )
        try:
            assert await asyncio.to_thread(write_started.wait, _LOST_RUN_SECS)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, timeout=_LOST_RUN_SECS)
        finally:
            release_write.set()
        await asyncio.wait_for(
            asyncio.gather(*list(svc._inflight_adds), return_exceptions=True),
            timeout=_LOST_RUN_SECS,
        )
        monkeypatch.setattr(svc, "_write_state", real_write)
        second = svc.get_by_slot(slot_key)
        assert second is not None and second.id != first.id, "the cancelled add did not commit"
        file_left = sentinel.exists()
        svc._cancel_timer(second.id)
        await svc._timer(second, delay=0)
        assert (file_left, second.active, second.stopped_reason) == (
            False,
            True,
            "",
        ), "the stale stop file outlived the cancelled caller and finished the new goal"
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_cancelled_add_still_removes_the_stale_sentinel_through_the_shutdown_drain(
    audits: list[dict], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The removal belongs to the admitted transaction ``shutdown()`` drains: a
    gateway stop that follows the cancelled caller still waits for it, so the
    store the next start loads holds the new goal active with no stop file."""
    slot_key = "chat-1-1"
    sentinel = tmp_path / ".stop-chat-1-1"
    monkeypatch.setattr(
        autonudge_authz, "resolve_stop_sentinel", lambda key, *a, **kw: str(sentinel)
    )
    svc = AutoNudgeService(base_dir=tmp_path / "home")
    await svc.start()
    state = _slot_state(slot_key)
    first = await _finish_by_stop_file(svc, state, slot_key, sentinel)
    real_write = svc._write_state
    write_started = threading.Event()
    release_write = threading.Event()

    def _held_write(payload: dict) -> None:
        write_started.set()
        if not release_write.wait(_LOST_RUN_SECS):
            raise AssertionError("the test did not release the writer")
        real_write(payload)

    monkeypatch.setattr(svc, "_write_state", _held_write)
    caller = asyncio.create_task(
        authorize_and_add_nudge(
            svc=svc, state=state, slot_key=slot_key, message="second goal", source="dashboard"
        )
    )
    try:
        assert await asyncio.to_thread(write_started.wait, _LOST_RUN_SECS)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, timeout=_LOST_RUN_SECS)
    finally:
        release_write.set()
    await asyncio.wait_for(svc.shutdown(), timeout=_LOST_RUN_SECS)
    assert not svc._inflight_adds
    assert not sentinel.exists(), "shutdown returned before the cancelled add removed the file"
    restored = AutoNudgeService(base_dir=tmp_path / "home")
    await asyncio.to_thread(restored._load)
    rows = restored.list_all()
    assert [(r.id != first.id, r.active, r.stopped_reason) for r in rows] == [(True, True, "")]


@pytest.mark.asyncio
async def test_a_failed_add_write_leaves_the_existing_stop_sentinel_in_place(
    audits: list[dict], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The removal follows the write, so a replacement whose write fails never
    reaches it: the loop it would have displaced comes back with its stop file,
    which still finishes it on its next tick."""
    from kiro_crew.autonudge import STOP_SENTINEL_REASON

    slot_key = "chat-1-1"
    sentinel = tmp_path / ".stop-chat-1-1"
    monkeypatch.setattr(
        autonudge_authz, "resolve_stop_sentinel", lambda key, *a, **kw: str(sentinel)
    )
    svc = AutoNudgeService(base_dir=tmp_path / "home")
    await svc.start()
    state = _slot_state(slot_key)
    try:
        existing, error, status = await authorize_and_add_nudge(
            svc=svc, state=state, slot_key=slot_key, message="existing goal", source="dashboard"
        )
        assert error is None and status == 200 and existing is not None
        sentinel.write_text("goal met", encoding="utf-8")
        real_write = svc._write_state

        def _failing_write(payload: dict) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(svc, "_write_state", _failing_write)
        with pytest.raises(OSError, match="disk full"):
            await authorize_and_add_nudge(
                svc=svc,
                state=state,
                slot_key=slot_key,
                message="replacement goal",
                source="dashboard",
            )
        monkeypatch.setattr(svc, "_write_state", real_write)
        assert sentinel.read_text(encoding="utf-8") == "goal met"
        assert svc.get_by_slot(slot_key) is existing and existing.active
        svc._cancel_timer(existing.id)
        await svc._timer(existing, delay=0)
        assert (existing.active, existing.stopped_reason) == (False, STOP_SENTINEL_REASON)
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_stale_sentinel_that_cannot_be_unlinked_saves_the_goal_finished(
    audits: list[dict],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stale stop entry that cannot be removed (here a directory) would finish the
    new goal on its first tick, so the add saves it finished now, with no timer: the
    row, store record, events and stop line that tick would leave, plus a warning
    naming the path. Once the entry is gone the goal can run again."""
    from kiro_crew.autonudge import STOP_SENTINEL_REASON

    slot_key = "chat-1-1"
    sentinel = tmp_path / ".stop-chat-1-1"
    sentinel.mkdir()
    monkeypatch.setattr(
        autonudge_authz, "resolve_stop_sentinel", lambda key, *a, **kw: str(sentinel)
    )
    svc = AutoNudgeService(base_dir=tmp_path / "home")
    await svc.start()
    events: list[tuple[str, bool]] = []

    def _observe(event: str, loop: NudgeLoop | None) -> None:
        if loop is not None:
            events.append((event, loop.active))

    svc.subscribe(_observe)
    try:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
            loop, error, status = await authorize_and_add_nudge(
                svc=svc,
                state=_slot_state(slot_key),
                slot_key=slot_key,
                message="new goal",
                source="dashboard",
            )
        assert error is None and status == 200 and loop is not None
        assert (loop.active, loop.stopped_reason, loop.next_due_ts) == (
            False,
            STOP_SENTINEL_REASON,
            0.0,
        )
        assert loop.id not in svc._timers
        assert events == [("added", False), ("updated", False)]
        restored = AutoNudgeService(base_dir=tmp_path / "home")
        await asyncio.to_thread(restored._load)
        stored = restored.get_by_id(loop.id)
        assert stored is not None
        assert (stored.active, stored.stopped_reason) == (False, STOP_SENTINEL_REASON)
        messages = [record.getMessage() for record in caplog.records]
        assert any(
            "could not remove the old stop file" in m and sentinel.name in m for m in messages
        )
        assert any(
            "reason='stop_sentinel'" in m and sentinel.name in m for m in messages
        ), "the stop line did not name the stop file"
        sentinel.rmdir()
        revived = await svc.update(loop.id, active=True)
        assert revived is not None and revived.active and loop.id in svc._timers
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_stale_sentinel_whose_finished_save_fails_is_left_to_its_first_tick(
    audits: list[dict], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If saving the finished state fails too, the committed active row stands with
    its timer armed, and its first tick finishes it as it always did."""
    from kiro_crew.autonudge import STOP_SENTINEL_REASON

    slot_key = "chat-1-1"
    sentinel = tmp_path / ".stop-chat-1-1"
    sentinel.mkdir()
    monkeypatch.setattr(
        autonudge_authz, "resolve_stop_sentinel", lambda key, *a, **kw: str(sentinel)
    )
    svc = AutoNudgeService(base_dir=tmp_path / "home")
    await svc.start()
    real_write = svc._write_state
    writes: list[dict] = []

    def _second_write_fails(payload: dict) -> None:
        writes.append(payload)
        if len(writes) == 2:
            raise OSError("disk full")
        real_write(payload)

    monkeypatch.setattr(svc, "_write_state", _second_write_fails)
    try:
        loop, error, status = await authorize_and_add_nudge(
            svc=svc,
            state=_slot_state(slot_key),
            slot_key=slot_key,
            message="new goal",
            source="dashboard",
        )
        assert error is None and status == 200 and loop is not None
        assert len(writes) == 2
        assert (loop.active, loop.stopped_reason) == (True, "")
        assert loop.next_due_ts > 0 and loop.id in svc._timers
        monkeypatch.setattr(svc, "_write_state", real_write)
        svc._cancel_timer(loop.id)
        await svc._timer(loop, delay=0)
        assert (loop.active, loop.stopped_reason) == (False, STOP_SENTINEL_REASON)
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_stale_sentinel_path_the_timer_cannot_see_does_not_finish_the_goal(
    audits: list[dict], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A path under a regular file holds nothing the timer's ``exists`` can find, so
    a failed removal there arms the goal as usual instead of finishing it."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    sentinel = blocker / ".stop-chat-1-1"
    monkeypatch.setattr(
        autonudge_authz, "resolve_stop_sentinel", lambda key, *a, **kw: str(sentinel)
    )
    svc = AutoNudgeService(base_dir=tmp_path / "home")
    await svc.start()
    try:
        loop, error, status = await authorize_and_add_nudge(
            svc=svc,
            state=_slot_state("chat-1-1"),
            slot_key="chat-1-1",
            message="new goal",
            source="dashboard",
        )
        assert error is None and status == 200 and loop is not None
        assert (loop.active, loop.stopped_reason) == (True, "")
        assert loop.id in svc._timers
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_stale_sentinel_finish_is_published_only_after_its_write_commits(
    audits: list[dict], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The finished state of a goal whose stale stop entry cannot be removed is
    written from a staged copy first; the live row (what a concurrent reader sees)
    changes only once that write is durable. While the write is in flight the goal
    still reads active, and the payload being written already carries the finish."""
    from kiro_crew.autonudge import STOP_SENTINEL_REASON

    slot_key = "chat-1-1"
    sentinel = tmp_path / ".stop-chat-1-1"
    sentinel.mkdir()
    monkeypatch.setattr(
        autonudge_authz, "resolve_stop_sentinel", lambda key, *a, **kw: str(sentinel)
    )
    svc = AutoNudgeService(base_dir=tmp_path / "home")
    await svc.start()
    real_write = svc._write_state
    writes: list[dict] = []
    second_write_started = threading.Event()
    release_second_write = threading.Event()

    def _hold_second_write(payload: dict) -> None:
        writes.append(payload)
        if len(writes) == 2:
            second_write_started.set()
            if not release_second_write.wait(_LOST_RUN_SECS):
                raise AssertionError("the test did not release the writer")
        real_write(payload)

    monkeypatch.setattr(svc, "_write_state", _hold_second_write)
    try:
        caller = asyncio.create_task(
            authorize_and_add_nudge(
                svc=svc,
                state=_slot_state(slot_key),
                slot_key=slot_key,
                message="new goal",
                source="dashboard",
            )
        )
        try:
            assert await asyncio.to_thread(second_write_started.wait, _LOST_RUN_SECS)
            live = svc.get_by_slot(slot_key)
            assert live is not None, "the committed row is not readable during the save"
            seen_during_write = (live.active, live.stopped_reason)
            rows = {row["id"]: row for row in writes[1]["loops"]}
            written = (rows[live.id]["active"], rows[live.id]["stopped_reason"])
        finally:
            release_second_write.set()
        loop, error, status = await asyncio.wait_for(caller, timeout=_LOST_RUN_SECS)
        assert error is None and status == 200 and loop is not None
        assert seen_during_write == (
            True,
            "",
        ), "the stop was published to readers before its write committed"
        assert written == (False, STOP_SENTINEL_REASON), "the write did not carry the finish"
        assert (loop.active, loop.stopped_reason) == (False, STOP_SENTINEL_REASON)
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_add_audits_then_reraises_a_service_failure(audits: list[dict]) -> None:
    svc = RecordingSvc(add_error=OSError("store wedged"))
    with pytest.raises(OSError, match="store wedged"):
        await authorize_and_add_nudge(
            svc=svc,
            state=_state(
                slots={"chat-1-1": SimpleNamespace(workspace="default", is_closing=False)}
            ),
            slot_key="chat-1-1",
            message="watch",
            stop_sentinel_path=str(Path.home() / "nonsense-sentinel-xyz"),
            source="dashboard",
        )
    outcomes = [a["outcome"] for a in audits]
    assert outcomes == ["invoked", "error"]  # audited BEFORE the attempt, then the failure
    assert "svc.add failed: OSError" in audits[-1]["error"]


@pytest.mark.asyncio
async def test_add_monitor_returns_conflict_when_a_wake_is_inflight(
    audits: list[dict],
) -> None:
    class ConflictingSvc:
        async def add_monitor(self, **kw: Any) -> Any:
            raise MonitorUpdateConflict("existing monitor wake is in flight")

    loop, error, status = await authorize_and_add_nudge(
        svc=ConflictingSvc(),
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default", is_closing=False)}),
        slot_key="chat-1-1",
        message="watch",
        source="dashboard",
        monitor=MonitorState(
            kind="github_pull_request",
            target="owner/repo#456",
            objective="review_ready",
            created_ts=1_000.0,
        ),
    )

    assert loop is None and status == 409
    assert error == "existing monitor wake is in flight"
    assert [event["outcome"] for event in audits] == ["invoked", "denied"]


@pytest.mark.asyncio
async def test_update_monitor_bound_failure_is_an_audited_client_error(
    audits: list[dict],
) -> None:
    """The store re-checks the effective runtime budget against the ceiling on
    every structured update; its refusal reaches the caller as a 400 quoting
    the range, matching the legacy update path."""

    class BoundedSvc:
        def get_by_id(self, _loop_id: str) -> None:
            return None

        async def update_monitor(self, *_args: Any, **_kwargs: Any) -> Any:
            raise ValueError("max_runtime_secs must be an integer between 1 and 3600 (1 hour)")

    loop, error, status = await autonudge_authz.authorize_and_update_monitor(
        svc=BoundedSvc(),
        state=_state(slots={"chat-1-1": SimpleNamespace(mode="", memory_mode="persistent")}),
        loop_id="monitor-1",
        session_key="chat-1-1",
        patch={"cadence_secs": 600},
        source="dashboard",
    )

    assert loop is None and status == 400
    assert error == "max_runtime_secs must be an integer between 1 and 3600 (1 hour)"
    assert [event["outcome"] for event in audits] == ["invoked", "denied"]


@pytest.mark.asyncio
async def test_credential_update_store_must_run_post_commit_continuation(
    audits: list[dict],
) -> None:
    prior = NudgeLoop(
        id="monitor-1",
        slot_key="chat-1-1",
        message="",
        monitor=MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
        ),
    )

    class ForeignMonitorSvc:
        def __init__(self) -> None:
            self.loop = prior
            self.updated: NudgeLoop | None = None
            self.rollback_calls: list[tuple[str, Any, Any]] = []

        def mutate_returned_update(self) -> None:
            assert self.updated is not None and self.updated.monitor is not None
            self.updated.monitor.target = "owner/repo#late-change"

        async def update_monitor(
            self,
            _loop_id: str,
            *,
            _prior_snapshot_out: list[Any],
            **patch: Any,
        ) -> Any:
            _prior_snapshot_out.append(deepcopy(self.loop))
            updated = deepcopy(self.loop)
            assert updated.monitor is not None
            updated.monitor.target = patch["target"]
            self.loop = updated
            self.updated = updated
            asyncio.get_running_loop().call_soon(self.mutate_returned_update)
            return updated

        async def rollback_monitor_update(
            self,
            monitor_id: str,
            prior_loop: Any,
            failed_update: Any,
        ) -> bool:
            assert [event["outcome"] for event in audits] == ["invoked"]
            self.rollback_calls.append((monitor_id, prior_loop, failed_update))
            self.loop = prior_loop
            return True

    svc = ForeignMonitorSvc()
    loop, error, status = await autonudge_authz.authorize_and_update_monitor(
        svc=svc,
        state=_state(slots={"chat-1-1": SimpleNamespace(mode="", memory_mode="persistent")}),
        loop_id=prior.id,
        session_key=prior.slot_key,
        patch={"target": "owner/repo#456"},
        source="dashboard",
        grant_owner_provider_credentials=True,
    )

    assert loop is None and status == 503
    assert error == "monitor credential authorization unavailable — prior monitor restored"
    assert len(svc.rollback_calls) == 1
    monitor_id, restored, failed = svc.rollback_calls[0]
    assert monitor_id == prior.id
    assert restored.monitor is not None and restored.monitor.target == "owner/repo#123"
    assert failed.monitor is not None and failed.monitor.target == "owner/repo#456"
    assert svc.updated is not None and svc.updated.monitor is not None
    assert svc.updated.monitor.target == "owner/repo#late-change"
    assert svc.loop is restored
    assert [event["outcome"] for event in audits] == ["invoked", "denied"]
    assert audits[-1]["error"] == "monitor authorization requires a rollback-capable loop store"


@pytest.mark.asyncio
async def test_update_monitor_conflict_records_a_denied_audit(
    audits: list[dict],
) -> None:
    class ConflictingSvc:
        def get_by_id(self, _loop_id: str) -> None:
            return None

        async def rollback_monitor_update(self, *_args: Any) -> bool:
            return True

        async def update_monitor(self, *_args: Any, **_kwargs: Any) -> Any:
            raise MonitorUpdateConflict("existing monitor wake is in flight")

    loop, error, status = await autonudge_authz.authorize_and_update_monitor(
        svc=ConflictingSvc(),
        state=_state(slots={"chat-1-1": SimpleNamespace(mode="", memory_mode="persistent")}),
        loop_id="monitor-1",
        session_key="chat-1-1",
        patch={"target": "owner/repo#456"},
        source="dashboard",
    )

    assert loop is None and status == 409
    assert error == "existing monitor wake is in flight"
    assert [event["outcome"] for event in audits] == ["invoked", "denied"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "status"),
    [("mode", "crew", 409), ("memory_mode", "temporary", 403)],
)
async def test_update_monitor_rechecks_current_dashboard_admission(
    audits: list[dict], field: str, value: str, status: int
) -> None:
    svc = SimpleNamespace(update_monitor=AsyncMock(return_value=SimpleNamespace(id="monitor-1")))
    slot = SimpleNamespace(mode="", memory_mode="persistent")
    setattr(slot, field, value)

    loop, error, actual_status = await autonudge_authz.authorize_and_update_monitor(
        svc=svc,
        state=_state(slots={"chat-1-1": slot}),
        loop_id="monitor-1",
        session_key="chat-1-1",
        patch={"cadence_secs": 300},
        source="dashboard",
    )

    assert loop is None and actual_status == status and error
    svc.update_monitor.assert_not_awaited()
    assert [event["outcome"] for event in audits] == ["invoked", "denied"]


@pytest.mark.asyncio
async def test_update_monitor_rejects_redaction_expansion_over_limit(
    audits: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    class RecordingMonitorSvc:
        def __init__(self) -> None:
            self.patches: list[dict[str, Any]] = []

        async def update_monitor(self, _loop_id: str, **patch: Any) -> Any:
            self.patches.append(patch)
            return SimpleNamespace(id="monitor-1")

    svc = RecordingMonitorSvc()
    expanded = "x" * (MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS + 1)
    monkeypatch.setattr(
        autonudge_authz,
        "redact_exfiltration_urls",
        lambda _value: (expanded, ["expanded"]),
    )

    loop, error, status = await autonudge_authz.authorize_and_update_monitor(
        svc=svc,
        state=_state(slots={"chat-1-1": SimpleNamespace(mode="", memory_mode="persistent")}),
        loop_id="monitor-1",
        session_key="chat-1-1",
        patch={"wake_instructions": "short"},
        source="dashboard",
    )

    assert loop is None and status == 400
    assert error == (
        "wake_instructions too long after redaction "
        f"(max {MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS} chars)"
    )
    assert svc.patches == []


@pytest.mark.asyncio
async def test_add_monitor_rejects_redaction_expansion_over_limit(
    audits: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    class RecordingMonitorSvc:
        def __init__(self) -> None:
            self.added: list[dict[str, Any]] = []

        def get_by_slot(self, _slot_key: str) -> None:
            return None

        async def add_monitor(self, **values: Any) -> Any:
            self.added.append(values)
            return SimpleNamespace(id="monitor-1")

    svc = RecordingMonitorSvc()
    expanded = "x" * (MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS + 1)
    monkeypatch.setattr(
        autonudge_authz,
        "redact_exfiltration_urls",
        lambda _value: (expanded, ["expanded"]),
    )

    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default", is_closing=False)}),
        slot_key="chat-1-1",
        message="watch",
        source="dashboard",
        monitor=MonitorState(
            kind="github_pull_request",
            target="owner/repo#456",
            objective="review_ready",
            created_ts=1_000.0,
            wake_instructions="short",
        ),
    )

    assert loop is None and status == 400
    assert error == (
        "wake_instructions too long after redaction "
        f"(max {MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS} chars)"
    )
    assert svc.added == []


@pytest.mark.asyncio
async def test_add_monitor_conflict_preserves_existing_legacy_stop_sentinel(
    audits: list[dict], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A rejected structured replacement cannot disable the legacy loop's kill switch."""
    sentinel = tmp_path / "stop-chat-1-1"
    sentinel.write_text("stop", encoding="utf-8")
    monkeypatch.setattr(
        autonudge_authz,
        "resolve_stop_sentinel",
        lambda key, *args, **kwargs: str(sentinel),
    )

    class ConflictingSvc:
        async def add_monitor(self, **kw: Any) -> Any:
            raise MonitorUpdateConflict("existing loop is not a structured monitor")

    loop, error, status = await authorize_and_add_nudge(
        svc=ConflictingSvc(),
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default", is_closing=False)}),
        slot_key="chat-1-1",
        message="watch",
        source="dashboard",
        monitor=MonitorState(
            kind="github_pull_request",
            target="owner/repo#456",
            objective="review_ready",
            created_ts=1_000.0,
        ),
        replace_existing=False,
    )

    assert loop is None and status == 409
    assert error == "existing loop is not a structured monitor"
    assert sentinel.read_text(encoding="utf-8") == "stop"


@pytest.mark.asyncio
async def test_add_monitor_forwards_conditional_restart_identity(
    audits: list[dict],
) -> None:
    captured: dict[str, Any] = {}
    expected = SimpleNamespace(id="new-monitor", slot_key="chat-1-1")

    class RecordingMonitorSvc:
        async def add_monitor(self, **kw: Any) -> Any:
            captured.update(kw)
            return expected

    loop, error, status = await authorize_and_add_nudge(
        svc=RecordingMonitorSvc(),
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default", is_closing=False)}),
        slot_key="chat-1-1",
        message="watch",
        source="dashboard",
        monitor=MonitorState(
            kind="github_pull_request",
            target="owner/repo#456",
            objective="review_ready",
            created_ts=1_000.0,
        ),
        expected_existing_monitor_id="old-monitor",
        expected_existing_config_generation=4,
    )

    assert loop is expected and error is None and status == 200
    assert captured["expected_existing_monitor_id"] == "old-monitor"
    assert captured["expected_existing_config_generation"] == 4


@pytest.mark.asyncio
async def test_legacy_add_cannot_replace_a_structured_wake_in_flight(
    audits: list[dict],
) -> None:
    monitor = MonitorState(
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/7",
        objective="review_ready",
        created_ts=1_000.0,
        wake_in_flight=True,
    )
    existing = SimpleNamespace(id="mon-1", monitor=monitor)
    svc = RecordingSvc(loop=existing)

    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default", is_closing=False)}),
        slot_key="chat-1-1",
        message="legacy replacement",
        source="dashboard",
    )

    assert loop is None and status == 409
    assert error == "existing monitor cannot be replaced while a wake is in flight"
    assert svc.added == []
    assert svc.get_by_slot("chat-1-1") is existing


@pytest.mark.asyncio
async def test_legacy_create_only_reaches_the_service_lock(audits: list[dict]) -> None:
    svc = RecordingSvc()

    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default", is_closing=False)}),
        slot_key="chat-1-1",
        message="legacy fallback",
        source="dashboard",
        replace_existing=False,
    )

    assert loop is not None and error is None and status == 200
    assert svc.added[0]["replace_existing"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "stop"])
async def test_monitor_audit_sink_is_resolved_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    loop_thread = threading.get_ident()
    factory_threads: list[int] = []
    sink_threads: list[int] = []
    sink = SimpleNamespace(
        log_tool_invocation=lambda **kw: sink_threads.append(threading.get_ident())
    )

    def _sel() -> Any:
        factory_threads.append(threading.get_ident())
        return sink

    monkeypatch.setattr(autonudge_authz, "sel", _sel)
    svc = SimpleNamespace(
        update_monitor=lambda *args, **kwargs: None,
        stop_monitor=lambda *args, **kwargs: None,
    )
    if operation == "update":
        svc.update_monitor = AsyncMock(return_value=None)
        await autonudge_authz.authorize_and_update_monitor(
            svc=svc,
            state=_state(slots={"chat-1-1": SimpleNamespace(mode="", memory_mode="persistent")}),
            loop_id="mon-1",
            session_key="chat-1-1",
            patch={"cadence_secs": 300},
            source="dashboard",
        )
    else:
        svc.stop_monitor = AsyncMock(return_value=None)
        await autonudge_authz.authorize_and_stop_monitor(
            svc=svc,
            loop_id="mon-1",
            session_key="chat-1-1",
            source="dashboard",
        )

    assert factory_threads and factory_threads[0] != loop_thread
    assert sink_threads and sink_threads[0] != loop_thread


# ── update(): the remaining payload-shape denials ──


@pytest.mark.asyncio
async def test_update_rejects_a_non_string_message(audits: list[dict]) -> None:
    svc = RecordingSvc()
    loop, error, status = await authorize_and_update_nudge(
        svc=svc, loop_id="l1", message=123, source="dashboard"
    )
    assert loop is None and status == 400 and error == "message must be a string"
    assert svc.updated == []


@pytest.mark.asyncio
async def test_update_rejects_an_oversized_message(audits: list[dict]) -> None:
    svc = RecordingSvc()
    loop, error, status = await authorize_and_update_nudge(
        svc=svc, loop_id="l1", message="q" * 8001, source="dashboard"
    )
    assert loop is None and status == 400 and "max 8000" in error
    assert svc.updated == []


@pytest.mark.asyncio
async def test_update_rejects_a_fractional_idle_secs(audits: list[dict]) -> None:
    """59.9 must be refused, not silently truncated to 59."""
    svc = RecordingSvc()
    loop, error, status = await authorize_and_update_nudge(
        svc=svc, loop_id="l1", idle_secs=59.9, source="dashboard"
    )
    assert loop is None and status == 400 and error == "idle_secs must be a whole number"
    assert svc.updated == []


@pytest.mark.asyncio
async def test_update_rejects_an_uncastable_numeric_field(audits: list[dict]) -> None:
    """A value int() cannot take must land as a 400, not an unhandled 500."""
    svc = RecordingSvc()
    loop, error, status = await authorize_and_update_nudge(
        svc=svc, loop_id="l1", idle_secs="not-a-number", source="dashboard"
    )
    assert loop is None and status == 400 and "must be integers" in error
    assert svc.updated == []


@pytest.mark.asyncio
async def test_update_rejects_a_stringified_active_flag(audits: list[dict]) -> None:
    """bool("false") is True, so accepting a string would flip a pause into a
    resume on a loop that runs tools unattended."""
    svc = RecordingSvc()
    loop, error, status = await authorize_and_update_nudge(
        svc=svc, loop_id="l1", active="false", source="dashboard"
    )
    assert loop is None and status == 400 and error == "active must be a boolean"
    assert svc.updated == []


@pytest.mark.asyncio
async def test_update_reports_a_missing_loop_as_404(audits: list[dict]) -> None:
    svc = RecordingSvc(loop=None)  # svc.update() finds nothing
    loop, error, status = await authorize_and_update_nudge(
        svc=svc, loop_id="gone", message="hi", source="dashboard"
    )
    assert loop is None and status == 404 and error == "loop not found"
    # The mutation was attempted before the not-found verdict.
    assert svc.updated and svc.updated[0]["loop_id"] == "gone"


# ── resolve_stop_sentinel() ──


class TestResolveStopSentinel:
    def test_path_lands_under_the_workspace_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(autonudge_authz, "workspace_dir_for", lambda ws: tmp_path / ws)

        out = autonudge_authz.resolve_stop_sentinel("slot-qq", workspace="wsx")

        assert out == str(tmp_path / "wsx" / ".stop-slot-qq")

    def test_separators_in_the_slot_key_cannot_escape_the_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``/`` and ``:`` are flattened, so a slot key like ``slack:C1/x`` stays a
        single filename instead of creating a nested path."""
        monkeypatch.setattr(autonudge_authz, "workspace_dir_for", lambda ws: tmp_path)

        out = Path(autonudge_authz.resolve_stop_sentinel("slack:C1/x"))

        assert out.parent == tmp_path
        assert out.name == ".stop-slack_C1_x"


# ── normalize_banner(truncate=True): the /goal path must redact BEFORE it cuts ──


class TestNormalizeBannerTruncate:
    """``truncate=True`` exists for ``/goal``, whose banner is derived from an
    arbitrarily long objective it does not control. The cut must land AFTER
    redaction so a credential straddling the cap boundary is masked whole, never
    sliced into a raw prefix that defeats full-token detection."""

    def test_a_credential_straddling_the_cap_is_masked_not_sliced(self) -> None:
        # 20-char key starts 10 chars before the cap and runs past it: a
        # slice-before-redact (the old ``objective[:cap]``) would keep the raw
        # 10-char prefix ``AKIAIOSFOD`` because the truncated token does not
        # match the scanner.
        straddling = "x" * (MAX_BANNER_CHARS - 10) + "AKIAIOSFODNN7EXAMPLE" + " tail"
        value, error = normalize_banner(straddling, absent_ok=True, truncate=True)
        assert error is None
        assert len(value) <= MAX_BANNER_CHARS
        assert "AKIA" not in value, "a raw credential prefix survived the cap cut"

    def test_a_long_credential_free_objective_truncates_instead_of_dropping(self) -> None:
        value, error = normalize_banner(
            "a" * (MAX_BANNER_CHARS + 100), absent_ok=True, truncate=True
        )
        assert error is None and len(value) == MAX_BANNER_CHARS

    def test_without_truncate_an_over_cap_banner_is_still_rejected(self) -> None:
        """The API/MCP callers keep the rejecting behaviour — a user typed it and
        can shorten it, so silently truncating would hide their input."""
        value, error = normalize_banner("a" * (MAX_BANNER_CHARS + 1), absent_ok=True)
        assert value == "" and error and "too long" in error
