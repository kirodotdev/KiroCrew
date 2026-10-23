"""The auto-nudge loop's own text is credential-scrubbed on every surface that serves it.

A loop's ``message`` and ``stopped_reason`` are agent-authored free text, and
several producers reach ``svc.add`` without passing through the arming
authorizer, so the stored value can be text nothing has scanned. These pin that
it is scrubbed on each surface that publishes it, and that the two fields a
client ADDRESSES a row by survive verbatim. Two shape invariants sit beside the
scrub, because a denylist that flattens is as much a defect as one that leaks:
numbers stay numbers, and the typed ``judge`` readings stay objects the goal
popover can read.

The last group pins the status code. A host that declares a credential policy it
cannot compose raises while RENDERING, which answered 500 -- on a write, for a
mutation that had already committed. Reads refuse 503 before the lookup; writes
refuse before the mutation.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew import autonudge_authz as authz
from kiro_crew.autonudge import NudgeLoop
from kiro_crew.dashboard.handlers import autonudge as h
from kiro_crew.platform import PlatformCompositionError

SECRET = "AKIAIOSFODNN7EXAMPLE"


def _install_broken_policy() -> None:
    """Replace the active credential policy with one that cannot compose."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import build_default_context, set_context

    class _Broken:
        def redact(self, text: str) -> str:
            raise PlatformCompositionError("companion credential policy unreadable")

    base = build_default_context(KiroCrewConfig())
    set_context(dataclasses.replace(base, credentials=_Broken()))


@pytest.fixture
def broken_policy() -> Any:
    """Install the uncomposable policy for one test and restore the real one after."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import build_default_context, set_context

    _install_broken_policy()
    yield
    set_context(build_default_context(KiroCrewConfig()))


def _loop(**kw: Any) -> NudgeLoop:
    base: dict[str, Any] = dict(
        id="loop-abc", slot_key="chat-1-111", message="keep checking", idle_secs=300
    )
    base.update(kw)
    return NudgeLoop(**base)


class _FakeSvc:
    def __init__(self, loops: list[NudgeLoop] | None = None) -> None:
        self.loops = loops or []

    def list_all(self) -> list[NudgeLoop]:
        return list(self.loops)

    def get_by_slot(self, slot_key: str) -> NudgeLoop | None:
        return next((lp for lp in self.loops if lp.slot_key == slot_key), None)


class _FakeState:
    """The three attributes ``NudgeAuthzState`` declares, and nothing else.

    The slot is PRESENT and persistent on purpose: an absent one is refused 404
    and a non-persistent one is refused as unable to host a loop, both ahead of
    the scrub preflight, so either would pass this file's 503 tests vacuously.
    """

    def __init__(self, slot_key: str = "chat-1-111") -> None:
        self._slots: dict = {slot_key: SimpleNamespace(memory_mode="persistent", mode="")}
        self.sessions: Any = None
        self.channel_transports: Any = None


class TestTheRestProjectionScrubsLoopText:
    def test_a_credential_in_message_does_not_reach_the_client(self) -> None:
        out = h._serialize(_loop(message=f"deploy with {SECRET} now"))
        assert SECRET not in out["message"], "the REST surface served an unredacted credential"

    def test_a_credential_in_stopped_reason_does_not_reach_the_client(self) -> None:
        out = h._serialize(_loop(stopped_reason=f"failed on {SECRET}"))
        assert SECRET not in out["stopped_reason"], "stopped_reason was served raw"

    def test_the_redactor_really_rewrites_this_value(self) -> None:
        """The positive control the exemption test below needs.

        Without it, asserting that a credential-shaped id SURVIVES would pass
        just as well on a redactor that rewrites nothing at all.
        """
        out = h._serialize(_loop(message=SECRET))
        assert out["message"] != SECRET, "the redactor left a known credential untouched"

    def test_the_addressing_fields_survive_verbatim(self) -> None:
        out = h._serialize(_loop(id=SECRET, slot_key=SECRET))
        assert out["id"] == SECRET, "a scrubbed id addresses no row"
        assert out["slot_key"] == SECRET, "a scrubbed slot_key addresses no row"

    def test_ordinary_text_round_trips_unchanged(self) -> None:
        out = h._serialize(_loop(message="keep checking the PR"))
        assert out["message"] == "keep checking the PR"


class TestTheScrubPreservesShape:
    def test_numeric_fields_are_not_rendered_as_text(self) -> None:
        out = h._serialize(_loop(idle_secs=300, cycle_count=4, active=True))
        assert out["idle_secs"] == 300, "a client does arithmetic on this"
        assert out["cycle_count"] == 4
        assert out["active"] is True, "a bool became something else"

    def test_judge_keeps_its_object_shape(self) -> None:
        """The popover reads ``loop.judge.wake_when``, so a string breaks it."""
        out = h._serialize(_loop(judge={"wake_when": "a review lands"}))
        assert isinstance(out["judge"], dict), "judge was flattened into text"
        assert out["judge"]["wake_when"] == "a review lands"

    def test_judge_last_verdict_keeps_its_object_shape(self) -> None:
        verdict = {"outcome": "blocked", "evidence_items": 2, "at": 1700000000}
        out = h._serialize(_loop(judge_last_verdict=verdict))
        assert isinstance(out["judge_last_verdict"], dict), "the verdict was flattened"
        assert out["judge_last_verdict"]["evidence_items"] == 2, "a nested number became text"

    def test_a_credential_nested_in_judge_is_still_scrubbed(self) -> None:
        """Walking the container must not mean skipping its leaves."""
        out = h._serialize(_loop(judge={"wake_when": f"when {SECRET} rotates"}))
        assert SECRET not in out["judge"]["wake_when"], "a nested credential was served raw"


class TestTheOtherTwoSurfacesUseTheSameRule:
    def test_the_in_session_reading_scrubs_stopped_reason(self) -> None:
        out = h._autonudge_loop_reading(_loop(stopped_reason=f"died on {SECRET}"))
        assert SECRET not in out["stopped_reason"], "the in-session reading served it raw"

    def test_a_nested_addressing_name_is_not_exempted(self) -> None:
        """``ADDRESSING_FIELDS`` names top-level fields, and nothing deeper."""
        out = h._serialize(_loop(judge={"id": SECRET}))
        assert SECRET not in out["judge"]["id"], "a nested key inherited the exemption"


class TestAReadThatCannotScrubRefusesWith503:
    @pytest.mark.asyncio
    async def test_the_list_route_refuses(self, broken_policy: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(h, "_autonudge_get", lambda: _FakeSvc([_loop()]))
        resp = await h.api_autonudge_list(make_mocked_request("GET", "/api/autonudge"))
        assert resp.status == 503, "a read that cannot scrub answered something other than 503"

    @pytest.mark.asyncio
    async def test_the_slot_route_refuses_even_on_an_EMPTY_slot(
        self, broken_policy: Any, monkeypatch: Any
    ) -> None:
        """The preflight sits ahead of the lookup, so an absent loop refuses too.

        An empty slot never reaches the projection, so a guard placed after the
        lookup answered 200 ``{"loop": null}`` on a host whose policy was broken --
        the one shape that looks healthy while the audited refusal is missing.
        """
        monkeypatch.setattr(h, "_autonudge_get", lambda: _FakeSvc([]))
        request = make_mocked_request("GET", "/api/autonudge/slot/chat-9-999")
        request.match_info["slot_key"] = "chat-9-999"
        resp = await h.api_autonudge_get(request)
        assert resp.status == 503, "an empty slot answered 200 on a broken policy"


class TestAWriteThatCannotScrubRefusesBeforeMutating:
    @pytest.mark.asyncio
    async def test_an_arm_is_refused_with_503(self, broken_policy: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(authz, "sel", MagicMock())
        svc = MagicMock()
        svc.add = AsyncMock()
        loop, error, status = await authz.authorize_and_add_nudge(
            svc=svc,
            state=_FakeState(),
            slot_key="chat-1-111",
            message="keep checking",
            idle_secs=300,
            source="dashboard",
        )
        assert status == 503, "an unscrubbable host armed a loop or answered 500"
        assert loop is None
        assert error == authz.SCRUB_UNAVAILABLE_SAVE_ERROR
        assert not svc.add.await_count, "the mutation ran before the refusal"

    @pytest.mark.asyncio
    async def test_a_save_is_refused_with_503(self, broken_policy: Any, monkeypatch: Any) -> None:
        """A save carrying NEITHER a message nor a banner.

        Those two branches already refuse 503 from inside their own echoed-projection
        checks. A save that touches only a cadence field reaches NEITHER, so without
        the preflight it committed, was audited as a success, and only then raised
        while the handler serialized the reply -- a 500 for work that had happened,
        which a retry applies a second time.
        """
        monkeypatch.setattr(authz, "sel", MagicMock())
        svc = MagicMock()
        svc.get_by_id = MagicMock(return_value=_loop())
        svc.update = AsyncMock()
        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc,
            loop_id="loop-abc",
            idle_secs=600,
            source="dashboard",
        )
        assert status == 503, "an unscrubbable host saved a goal or answered 500"
        assert error == authz.SCRUB_UNAVAILABLE_SAVE_ERROR
        assert not svc.update.await_count, "the mutation ran before the refusal"


class TestTheProbeItself:
    def test_it_reports_a_broken_policy(self, broken_policy: Any) -> None:
        assert authz.scrub_policy_unavailable() is True

    def test_it_reports_a_working_policy(self) -> None:
        """The negative control: the probe must not answer True unconditionally."""
        assert authz.scrub_policy_unavailable() is False


class TestTheSurfacesAreRegisteredInThePosture:
    def test_the_rest_inventory_is_a_declared_sink(self) -> None:
        from kiro_crew.security_posture import _REDACTION_SINKS

        names = {name for name, _path, _why in _REDACTION_SINKS}
        assert "Auto-nudge loop inventory" in names

    def test_the_sink_names_the_broadcast_it_also_covers(self) -> None:
        """The frame's rule lives in the handler, so the handler's row declares it."""
        from kiro_crew.security_posture import _REDACTION_SINKS

        why = next(w for n, _p, w in _REDACTION_SINKS if n == "Auto-nudge loop inventory")
        assert "autonudge_state" in why, "the broadcast is scrubbed but undeclared"
