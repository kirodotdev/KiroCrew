"""An incognito or temporary session must not be told its monitor was requested.

The authorizer denies every arm, and every update of a structured monitor, on a
dashboard slot whose memory mode is not ``persistent``. It runs when the
directive is applied, after the turn's result is processed. An arming tool that answered with a "requested" ack
and a directive would leave the agent telling the user a loop is coming while
nothing is ever watched.

The refusal is certain at tool-call time: a slot's memory mode only tightens, so
a slot that refuses now refuses at the turn boundary too. A legacy timer loop is
the exception for ``monitor_update``: its authorizer applies no memory-mode rule,
so a loop armed before the slot tightened can still be shrunk or paused. These tests require each arming tool to refuse IN BAND with the
authorizer's own text and emit no directive, and pin the session-monitor reading
that carries that text so the tool and the authorizer cannot drift apart.

Only the tool layer and the reading are under test. Whether a temporary session
should be able to host a loop at all is a separate product decision: the
authorizer's refusal is the ground truth these tests measure against.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import autonudge_authz, mcp_core, session_directive
from kiro_crew.dashboard.handlers import autonudge as handlers
from kiro_crew.mcp_tools import control

SLOT = "chat-1-123"
SESSION_KEY = f"dashboard:{SLOT}"
PR = "https://github.com/acme/widgets/pull/456"


@pytest.fixture()
def bound(monkeypatch: pytest.MonkeyPatch) -> str:
    """Make the tools believe they are on a dashboard session that resolves a binding."""
    monkeypatch.setattr(mcp_core, "_autonudge_binding_key", lambda sk: SLOT)
    monkeypatch.setattr(mcp_core, "_structured_monitor_binding_key", lambda sk: SLOT)
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda *a, **k: SESSION_KEY)
    return SESSION_KEY


@pytest.fixture()
def audits(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture the authorizer's SEL events instead of writing them."""
    events: list[dict] = []
    monkeypatch.setattr(
        autonudge_authz,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
    )
    return events


class _Loops:
    """The one service method the session-monitor reading calls."""

    def __init__(self, loop: Any = None) -> None:
        self.loop = loop

    def get_by_slot(self, slot_key: str) -> Any:
        return self.loop


def _legacy_loop() -> SimpleNamespace:
    """A plain timer loop, armed and firing: no structured ``monitor``."""
    return SimpleNamespace(
        id="loop-1",
        active=True,
        idle_secs=300,
        max_cycles=10,
        cycle_count=2,
        max_runtime_secs=3600,
        gate=False,
        monitor=None,
        last_fire_ts=0.0,
        created_ts=0.0,
        next_due_ts=0.0,
        stopped_reason="",
        approval_stalled=False,
        banner="",
    )


def _slot(memory_mode: str) -> SimpleNamespace:
    return SimpleNamespace(workspace="default", mode="", memory_mode=memory_mode, is_closing=False)


async def _authorizer_refusal(memory_mode: str) -> tuple[str | None, int]:
    """What the turn boundary answers an arm on this slot: the ground truth."""
    loop, error, status = await autonudge_authz.authorize_and_add_nudge(
        svc=SimpleNamespace(get_by_slot=lambda slot_key: None),
        state=SimpleNamespace(
            _slots={SLOT: _slot(memory_mode)}, sessions=None, channel_transports={}
        ),
        slot_key=SLOT,
        message=f"Watch {PR}",
        source="dashboard",
    )
    assert loop is None
    return error, status


async def _reading(
    monkeypatch: pytest.MonkeyPatch, memory_mode: str, loop: Any = None
) -> dict[str, Any]:
    """The real endpoint's JSON for a session holding ``loop``, on a slot in this mode."""
    monkeypatch.setattr(handlers, "_autonudge_get", lambda: _Loops(loop))
    app = web.Application()
    app["state"] = SimpleNamespace(_slots={SLOT: _slot(memory_mode)})
    request = make_mocked_request(
        "GET",
        "/api/autonudge/session-monitor",
        app=app,
        headers={"X-Session-Key": SESSION_KEY},
    )
    request["internal_auth"] = True
    response = await handlers.api_session_monitor_get(request)
    assert isinstance(response, web.Response) and response.status == 200
    assert isinstance(response.body, bytes)
    return json.loads(response.body.decode("utf-8"))


def _arm(tool: str) -> str:
    if tool == "monitor_start":
        return control.monitor_start(
            "monitor_start", {"message": f"Watch {PR} and report failures", "interval_secs": 600}
        )
    if tool == "monitor_watch":
        return control.monitor_watch(
            "monitor_watch",
            {"kind": "github_pull_request", "target": PR, "objective": "review_ready"},
        )
    return control.monitor_update("monitor_update", {"interval_secs": 900})


ARMING_TOOLS = ("monitor_start", "monitor_watch", "monitor_update")


class TestTheToolRefusesInBand:
    """Real slot -> real endpoint -> JSON -> each arming tool: a refusal, no directive."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("memory_mode", ["temporary", "incognito"])
    @pytest.mark.parametrize("tool", ARMING_TOOLS)
    async def test_a_restricted_session_gets_the_authorizers_refusal_in_the_arming_turn(
        self,
        bound: str,
        audits: list[dict],
        monkeypatch: pytest.MonkeyPatch,
        tool: str,
        memory_mode: str,
    ) -> None:
        error, status = await _authorizer_refusal(memory_mode)
        assert status == 403 and error, "ground truth: the turn boundary refuses this slot"

        reading = await _reading(monkeypatch, memory_mode)
        monkeypatch.setattr(mcp_core, "_get", lambda *a, **k: reading)
        out = _arm(tool)

        assert session_directive.decode(out, tool) is None, (
            f"{tool} still emitted a directive for an arm the authorizer is certain to "
            f"refuse, so the agent reports monitoring as started: {out!r}"
        )
        assert "requested" not in out.lower(), out
        assert error in out, (
            "the in-band refusal must carry the authorizer's own reason, so the agent "
            f"can tell the user why in the same turn: {out!r}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool", ARMING_TOOLS)
    async def test_a_persistent_session_still_emits(
        self, bound: str, monkeypatch: pytest.MonkeyPatch, tool: str
    ) -> None:
        reading = await _reading(monkeypatch, "persistent")
        monkeypatch.setattr(mcp_core, "_get", lambda *a, **k: reading)

        out = _arm(tool)

        assert session_directive.decode(out, tool) is not None, out

    @pytest.mark.parametrize("tool", ARMING_TOOLS)
    def test_an_unreadable_gateway_fails_open(
        self, bound: str, monkeypatch: pytest.MonkeyPatch, tool: str
    ) -> None:
        """The preflight is advisory: the authorizer stays the enforcement point."""

        def _down(*args: Any, **kwargs: Any) -> dict:
            raise ConnectionError("gateway unreachable")

        monkeypatch.setattr(mcp_core, "_get", _down)

        out = _arm(tool)

        assert session_directive.decode(out, tool) is not None, out


class TestAnUpdateFollowsItsOwnAuthorizer:
    """``monitor_update`` is refused in band only where its authorizer refuses."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("memory_mode", ["temporary", "incognito"])
    async def test_a_legacy_loop_on_a_restricted_slot_still_gets_its_update(
        self, bound: str, monkeypatch: pytest.MonkeyPatch, memory_mode: str
    ) -> None:
        """``authorize_and_update_nudge`` has no memory-mode rule, so this update applies."""
        reading = await _reading(monkeypatch, memory_mode, loop=_legacy_loop())
        assert reading.get("hosting_refusal"), "the slot itself still refuses new arms"
        assert isinstance(reading.get("autonudge_loop"), dict)
        monkeypatch.setattr(mcp_core, "_get", lambda *a, **k: reading)

        out = control.monitor_update("monitor_update", {"max_cycles": 3})

        assert session_directive.decode(out, "monitor_update") is not None, (
            "a firing legacy loop on a tightened slot must stay shrinkable: " f"{out!r}"
        )

    @pytest.mark.asyncio
    async def test_a_legacy_loop_on_a_restricted_slot_still_refuses_a_new_arm(
        self, bound: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reading = await _reading(monkeypatch, "temporary", loop=_legacy_loop())
        monkeypatch.setattr(mcp_core, "_get", lambda *a, **k: reading)

        out = _arm("monitor_start")

        assert session_directive.decode(out, "monitor_start") is None, out
        assert autonudge_authz.LOOP_HOSTING_REFUSED_ERROR in out

    @pytest.mark.parametrize("memory_mode", ["temporary", "incognito"])
    def test_a_structured_monitor_update_says_no_monitor_was_changed(
        self, bound: str, monkeypatch: pytest.MonkeyPatch, memory_mode: str
    ) -> None:
        reading = {
            "enabled": True,
            "active": True,
            "monitor_id": "monitor-1",
            "monitor": {"target": PR},
            "autonudge_loop": None,
            "hosting_refusal": autonudge_authz.LOOP_HOSTING_REFUSED_ERROR,
        }
        monkeypatch.setattr(mcp_core, "_get", lambda *a, **k: reading)

        out = control.monitor_update("monitor_update", {"max_cycles": 3})

        assert session_directive.decode(out, "monitor_update") is None, out
        assert "no monitor was changed" in out
        assert "armed" not in out


class TestTheReadingCarriesTheRefusal:
    """The wire contract the in-band refusal depends on."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("memory_mode", ["temporary", "incognito"])
    async def test_a_restricted_slot_reports_the_authorizers_reason(
        self, audits: list[dict], monkeypatch: pytest.MonkeyPatch, memory_mode: str
    ) -> None:
        error, _ = await _authorizer_refusal(memory_mode)

        reading = await _reading(monkeypatch, memory_mode)

        assert reading.get("hosting_refusal") == error

    @pytest.mark.asyncio
    async def test_a_persistent_slot_keeps_the_reading_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reading = await _reading(monkeypatch, "persistent")

        assert reading == {"enabled": True, "monitor": None, "autonudge_loop": None}


class TestOnePredicate:
    """The authorizer's paths and the reading answer from the same function."""

    @pytest.mark.asyncio
    async def test_the_authorizer_denies_with_whatever_the_predicate_says(
        self, audits: list[dict], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mutation pin: change the predicate and the authorizer's text follows it."""
        monkeypatch.setattr(autonudge_authz, "loop_hosting_refusal", lambda slot: "pinned reason")

        error, status = await _authorizer_refusal("persistent")

        assert (error, status) == ("pinned reason", 403)

    @pytest.mark.asyncio
    async def test_the_update_authorizer_denies_with_whatever_the_predicate_says(
        self, audits: list[dict], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same pin on the structured-monitor update path."""
        monkeypatch.setattr(autonudge_authz, "loop_hosting_refusal", lambda slot: "pinned")

        result = await autonudge_authz.authorize_and_update_monitor(
            svc=SimpleNamespace(),
            state=SimpleNamespace(_slots={SLOT: _slot("persistent")}),
            loop_id="monitor-1",
            session_key=SLOT,
            patch={"max_cycles": 3},
            source="dashboard",
        )

        assert result == (None, "pinned", 403)

    @pytest.mark.asyncio
    async def test_the_reading_reports_whatever_the_predicate_says(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(handlers, "loop_hosting_refusal", lambda slot: "pinned reason")

        reading = await _reading(monkeypatch, "persistent")

        assert reading.get("hosting_refusal") == "pinned reason"

    def test_the_predicate_admits_only_persistent(self) -> None:
        assert autonudge_authz.loop_hosting_refusal(_slot("persistent")) == ""
        assert autonudge_authz.loop_hosting_refusal(SimpleNamespace()) == ""
        for mode in ("temporary", "incognito"):
            assert (
                autonudge_authz.loop_hosting_refusal(_slot(mode))
                == autonudge_authz.LOOP_HOSTING_REFUSED_ERROR
            )
