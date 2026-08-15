"""Coverage for ``kiro_crew.autonudge_authz`` guard clauses and audit fallbacks.

``test/test_workflows_nudge_wiring.py`` already pins the happy path and the
headline Discord/dashboard denials. This file targets the remaining rejection
branches of BOTH chokepoints — the ones an attacker or a broken caller reaches
first — plus the two "auditing must never break the flow" fallbacks and the
``svc`` failure paths, where a swallowed exception would hide a security event.

Every test patches ``sel`` so nothing is written to the real security event log.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import autonudge_authz
from kiro_crew.autonudge_authz import (
    MAX_RUNTIME_SECS_CEILING,
    authorize_and_add_nudge,
    authorize_and_update_nudge,
)

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


async def test_add_rejects_a_channel_transport_it_cannot_authorize(audits: list[dict]) -> None:
    """``is_channel_key`` accepts telegram/whatsapp/unified prefixes too, but only
    Slack and Discord have ownership checks here — anything else must be refused
    rather than falling through to an unvalidated arm."""
    svc = RecordingSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc, state=_state(), slot_key="telegram:9001", message="watch", source="dashboard"
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
