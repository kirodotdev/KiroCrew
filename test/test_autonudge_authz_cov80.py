"""Coverage for ``kiro_crew.autonudge_authz`` guard clauses and audit fallbacks.

``test/test_workflows_nudge_wiring.py`` already pins the happy path and the
headline Discord/dashboard denials. This file targets the remaining rejection
branches of BOTH chokepoints — the ones an attacker or a broken caller reaches
first — plus the two "auditing must never break the flow" fallbacks and the
``svc`` failure paths, where a swallowed exception would hide a security event.

Every test patches ``sel`` so nothing is written to the real security event log.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from kiro_crew import autonudge_authz
from kiro_crew.autonudge import MonitorUpdateConflict
from kiro_crew.autonudge_authz import (
    MAX_RUNTIME_SECS_CEILING,
    authorize_and_add_nudge,
    authorize_and_update_nudge,
    normalize_banner,
)
from kiro_crew.constants import MAX_BANNER_CHARS
from kiro_crew.monitoring.models import MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS, MonitorState

pytestmark = pytest.mark.asyncio


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


async def test_update_without_service_is_a_503_error_event(audits: list[dict]) -> None:
    loop, error, status = await authorize_and_update_nudge(
        svc=None, loop_id="l1", message="hi", source="dashboard"
    )
    assert loop is None and status == 503 and "auto-nudge disabled" in error
    assert [a["outcome"] for a in audits] == ["error"]


async def test_update_requires_a_loop_id(audits: list[dict]) -> None:
    loop, error, status = await authorize_and_update_nudge(
        svc=RecordingSvc(), loop_id="   ", message="hi", source="dashboard"
    )
    assert loop is None and status == 400 and error == "loop_id required"
    assert audits and audits[0]["outcome"] == "denied"


async def test_update_rejects_runtime_budget_over_the_ceiling(audits: list[dict]) -> None:
    svc = RecordingSvc()
    loop, error, status = await authorize_and_update_nudge(
        svc=svc,
        loop_id="l1",
        max_runtime_secs=MAX_RUNTIME_SECS_CEILING + 1,
        source="dashboard",
    )
    assert loop is None and status == 400 and "7 days" in error
    assert svc.updated == []  # never applied


async def test_update_audit_failure_does_not_break_the_denial(broken_sel: None) -> None:
    """A dead SEL must not turn a 400 into a 500: the warn-and-continue fallback
    keeps the caller's error contract intact."""
    loop, error, status = await authorize_and_update_nudge(
        svc=RecordingSvc(), loop_id="", source="dashboard"
    )
    assert loop is None and status == 400 and error == "loop_id required"


async def test_update_audits_then_reraises_a_service_failure(audits: list[dict]) -> None:
    """``svc.update`` blowing up must leave an ``error`` event behind before the
    exception propagates — a silent failure would lose the security record."""
    svc = RecordingSvc(update_error=RuntimeError("store wedged"))
    with pytest.raises(RuntimeError, match="store wedged"):
        await authorize_and_update_nudge(svc=svc, loop_id="l1", message="hi", source="dashboard")
    errors = [a for a in audits if a["outcome"] == "error"]
    assert errors and "svc.update failed: RuntimeError" in errors[0]["error"]


# --------------------------------------------------------------------------- #
# authorize_and_add_nudge — guard clauses
# --------------------------------------------------------------------------- #


async def test_add_without_service_is_a_503_error_event(audits: list[dict]) -> None:
    loop, error, status = await authorize_and_add_nudge(
        svc=None, state=_state(), slot_key="chat-1-1", message="watch", source="dashboard"
    )
    assert loop is None and status == 503 and "auto-nudge disabled" in error
    assert [a["outcome"] for a in audits] == ["error"]


async def test_add_requires_both_slot_key_and_message(audits: list[dict]) -> None:
    svc = RecordingSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default")}),
        slot_key="chat-1-1",
        message="   ",
        source="dashboard",
    )
    assert loop is None and status == 400 and "required" in error
    assert svc.added == []


@pytest.mark.parametrize("mode", ["crew", "member"])
async def test_add_rejects_dashboard_modes_without_direct_turn_ingress(
    audits: list[dict], mode: str
) -> None:
    svc = RecordingSvc()
    slot = SimpleNamespace(workspace="default", mode=mode, memory_mode="persistent")

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


@pytest.mark.parametrize("memory_mode", ["incognito", "temporary"])
async def test_add_rejects_restricted_dashboard_sessions(
    audits: list[dict], memory_mode: str
) -> None:
    svc = RecordingSvc()
    slot = SimpleNamespace(workspace="default", mode="", memory_mode=memory_mode)

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


async def test_dashboard_admission_rechecks_mode_and_memory_boundary(
    audits: list[dict], tmp_path: Path
) -> None:
    svc = RecordingSvc()
    slot = SimpleNamespace(workspace="default", mode="", memory_mode="persistent")
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


async def test_add_rejects_a_non_integer_runtime_budget(audits: list[dict]) -> None:
    svc = RecordingSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default")}),
        slot_key="chat-1-1",
        message="watch",
        max_runtime_secs="not-a-number",  # type: ignore[arg-type]
        source="dashboard",
    )
    assert loop is None and status == 400 and error == "max_runtime_secs must be an integer"
    assert svc.added == []


async def test_add_audit_failure_does_not_break_the_denial(broken_sel: None) -> None:
    svc = RecordingSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc, state=_state(slots={}), slot_key="chat-nope", message="watch", source="dashboard"
    )
    assert loop is None and status == 404 and "unknown slot" in error
    assert svc.added == []


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


async def test_add_rejects_a_sensitive_stop_sentinel_path(audits: list[dict]) -> None:
    """``stop_sentinel_path`` is unlinked by the loop, so a credential path would
    turn an arm request into a delete of the caller's key material."""
    svc = RecordingSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default")}),
        slot_key="chat-1-1",
        message="watch",
        stop_sentinel_path=str(Path.home() / ".ssh" / "id_rsa"),
        source="dashboard",
    )
    assert loop is None and status == 400 and "sensitive" in error
    assert svc.added == []


async def test_add_defaults_the_sentinel_for_a_channel_loop(
    audits: list[dict], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Channel-bound loops get a per-session sentinel derived from the slot key
    (no dashboard slot to read a workspace from), and a stale sentinel left by a
    previous loop is cleared so the new loop is not stopped on its first tick."""
    sentinel = tmp_path / "stop-slack"
    sentinel.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(
        autonudge_authz, "resolve_stop_sentinel", lambda key, *a, **kw: str(sentinel)
    )
    svc = RecordingSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(sessions=SimpleNamespace(get_channel=lambda key: SimpleNamespace())),
        slot_key="slack:1712345.6789",
        message="watch",
        source="workflow",
    )
    assert error is None and status == 200 and loop is not None
    assert svc.added[0]["stop_sentinel_path"] == str(sentinel)
    assert not sentinel.exists()  # stale sentinel cleared before arming


async def test_add_audits_then_reraises_a_service_failure(audits: list[dict]) -> None:
    svc = RecordingSvc(add_error=OSError("store wedged"))
    with pytest.raises(OSError, match="store wedged"):
        await authorize_and_add_nudge(
            svc=svc,
            state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default")}),
            slot_key="chat-1-1",
            message="watch",
            stop_sentinel_path=str(Path.home() / "nonsense-sentinel-xyz"),
            source="dashboard",
        )
    outcomes = [a["outcome"] for a in audits]
    assert outcomes == ["invoked", "error"]  # audited BEFORE the attempt, then the failure
    assert "svc.add failed: OSError" in audits[-1]["error"]


async def test_add_monitor_returns_conflict_when_a_wake_is_inflight(
    audits: list[dict],
) -> None:
    class ConflictingSvc:
        async def add_monitor(self, **kw: Any) -> Any:
            raise MonitorUpdateConflict("existing monitor wake is in flight")

    loop, error, status = await authorize_and_add_nudge(
        svc=ConflictingSvc(),
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default")}),
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


async def test_update_monitor_conflict_records_a_denied_audit(
    audits: list[dict],
) -> None:
    class ConflictingSvc:
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
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default")}),
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
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default")}),
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
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default")}),
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
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default")}),
        slot_key="chat-1-1",
        message="legacy replacement",
        source="dashboard",
    )

    assert loop is None and status == 409
    assert error == "existing monitor cannot be replaced while a wake is in flight"
    assert svc.added == []
    assert svc.get_by_slot("chat-1-1") is existing


async def test_legacy_create_only_reaches_the_service_lock(audits: list[dict]) -> None:
    svc = RecordingSvc()

    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default")}),
        slot_key="chat-1-1",
        message="legacy fallback",
        source="dashboard",
        replace_existing=False,
    )

    assert loop is not None and error is None and status == 200
    assert svc.added[0]["replace_existing"] is False


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


async def test_update_rejects_a_non_string_message(audits: list[dict]) -> None:
    svc = RecordingSvc()
    loop, error, status = await authorize_and_update_nudge(
        svc=svc, loop_id="l1", message=123, source="dashboard"
    )
    assert loop is None and status == 400 and error == "message must be a string"
    assert svc.updated == []


async def test_update_rejects_an_oversized_message(audits: list[dict]) -> None:
    svc = RecordingSvc()
    loop, error, status = await authorize_and_update_nudge(
        svc=svc, loop_id="l1", message="q" * 8001, source="dashboard"
    )
    assert loop is None and status == 400 and "max 8000" in error
    assert svc.updated == []


async def test_update_rejects_a_fractional_idle_secs(audits: list[dict]) -> None:
    """59.9 must be refused, not silently truncated to 59."""
    svc = RecordingSvc()
    loop, error, status = await authorize_and_update_nudge(
        svc=svc, loop_id="l1", idle_secs=59.9, source="dashboard"
    )
    assert loop is None and status == 400 and error == "idle_secs must be a whole number"
    assert svc.updated == []


async def test_update_rejects_an_uncastable_numeric_field(audits: list[dict]) -> None:
    """A value int() cannot take must land as a 400, not an unhandled 500."""
    svc = RecordingSvc()
    loop, error, status = await authorize_and_update_nudge(
        svc=svc, loop_id="l1", idle_secs="not-a-number", source="dashboard"
    )
    assert loop is None and status == 400 and "must be integers" in error
    assert svc.updated == []


async def test_update_rejects_a_stringified_active_flag(audits: list[dict]) -> None:
    """bool("false") is True, so accepting a string would flip a pause into a
    resume on a loop that runs tools unattended."""
    svc = RecordingSvc()
    loop, error, status = await authorize_and_update_nudge(
        svc=svc, loop_id="l1", active="false", source="dashboard"
    )
    assert loop is None and status == 400 and error == "active must be a boolean"
    assert svc.updated == []


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
        # 10-char prefix ``AKIAIOSFOD`` because the truncated token no longer
        # matches the scanner.
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
