"""The wake judge's brief at its edges, and the reading a monitor tick hands it.

No Jev key is spent anywhere here. The provider is stubbed at ``decisions.decide``,
which is the seam the point calls, so these tests exercise the real state builder,
the real mapping and the real tick without a network request.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from autonudge_wake_judge_helpers import _restore_judge_process_globals  # noqa: F401 - fixture
from autonudge_wake_judge_helpers import _owner_dashboard_identity

from kiro_crew import autonudge_judge as judge
from kiro_crew.autonudge import (
    _JUDGE_QUIET_STREAK_FLOOR_DEFAULT,
    _MAX_QUIET_STREAK,
    AutoNudgeService,
    MonitorState,
    NudgeLoop,
)
from kiro_crew.irq import Outcome


class TestTheBriefIsCredentialScrubbed:
    """A criterion is free text, and free text can name a secret.

    The brief reaches the loop store on disk and the loop serialization the dashboard
    reads. Allowlisting keys and clipping lengths bounds the SHAPE of what arrives; it
    reads every value as opaque, so a criterion naming a bearer token is kept whole and
    merely shorter. Each entrance is asserted on its own because they are independent: a
    brief from a tool call is published without ever passing the decode path.
    """

    SECRET = 'curl -H "Authorization: Bearer sk-live-AbCd1234567890abcdefghij" ok'

    def test_the_arm_entrance_scrubs(self, tmp_path: pathlib.Path) -> None:
        async def main() -> None:
            base = tmp_path / "nudges"
            base.mkdir()
            svc = AutoNudgeService(base_dir=base)
            loop = await svc.add(
                "chat-1-1", "watch it", idle_secs=60, judge={"wake_when": self.SECRET}
            )
            assert "sk-live-AbCd1234567890abcdefghij" not in loop.judge["wake_when"]
            assert "REDACTED" in loop.judge["wake_when"]

        asyncio.run(main())

    def test_the_update_entrance_scrubs(self, tmp_path: pathlib.Path) -> None:
        async def main() -> None:
            base = tmp_path / "nudges"
            base.mkdir()
            svc = AutoNudgeService(base_dir=base)
            loop = await svc.add("chat-1-1", "watch it", idle_secs=60)
            revised = await svc.update(loop.id, judge={"wake_when": self.SECRET})
            assert revised is not None
            assert "sk-live-AbCd1234567890abcdefghij" not in revised.judge["wake_when"]

        asyncio.run(main())

    def test_the_decode_entrance_scrubs(self) -> None:
        from kiro_crew import autonudge as _autonudge

        out = _autonudge._bounded_judge_spec({"wake_when": self.SECRET})
        assert "sk-live-AbCd1234567890abcdefghij" not in out["wake_when"]

    def test_a_target_carrying_a_credential_is_scrubbed(self) -> None:
        from kiro_crew import autonudge as _autonudge

        out = _autonudge.scrubbed_judge_spec(
            {"targets": ["https://api.example.com/x?token=ghp_AbCd1234567890abcdefghijklmn"]}
        )
        assert "ghp_AbCd1234567890abcdefghijklmn" not in out["targets"][0]

    def test_a_clean_forge_target_is_untouched(self) -> None:
        """The scrub cannot be allowed to break a legitimate watch."""
        from kiro_crew import autonudge as _autonudge

        url = "https://github.com/kirodotdev/KiroCrew/pull/12787"
        out = _autonudge.scrubbed_judge_spec({"targets": [url], "wake_when": "a check goes red"})
        assert out["targets"] == [url]
        assert out["wake_when"] == "a check goes red"

    def test_the_scrub_runs_before_the_clip(self) -> None:
        """A redaction marker can be longer than the secret it replaces.

        Clipping first would leave the marker free to land over the bound, so the order
        is load-bearing and the bound is asserted on the scrubbed value.
        """
        from kiro_crew import autonudge as _autonudge

        bound = _autonudge._JUDGE_MAX_CRITERION_CHARS
        raw = ("x" * (bound - 20)) + " Bearer sk-live-AbCd1234567890abcdefghij"
        out = _autonudge.scrubbed_judge_spec({"wake_when": raw})
        assert len(out["wake_when"]) <= bound
        assert "sk-live-AbCd1234567890abcdefghij" not in out["wake_when"]


class TestThePublishedKeysReachTheDashboardReader:
    """The list route's key names and the popover's reader must agree.

    These two sides are edited in different languages and different files, and
    nothing in either one refers to the other. A field renamed or dropped on the
    publishing side leaves the reader looking up a name nobody sends: that is silent,
    because the reader treats an absent brief as "this loop has no judge", which is
    also the honest answer for most loops. The popover then draws no judge line and
    every test on both sides still passes.

    So the names are asserted against each other here. The Python side is exercised
    through the REAL projection rather than a copied key list, because the projection
    is where a field is dropped.
    """

    _TS = pathlib.Path(__file__).resolve().parents[1] / "website/src/components/autoNudgeLoop.ts"

    @staticmethod
    def _published(**loop_kwargs: Any) -> dict:
        from kiro_crew.autonudge import NudgeLoop, new_goal_token
        from kiro_crew.dashboard.handlers.autonudge import _serialize_for_legacy_reader

        loop = NudgeLoop(
            id="l1",
            slot_key="chat-1-1",
            message="go",
            idle_secs=60,
            max_cycles=5,
            created_ts=1.0,
            goal_token=new_goal_token(),
            stop_sentinel_path="/x",
            max_runtime_secs=0,
            next_due_ts=61.0,
            **loop_kwargs,
        )
        loop.judge_quiet_streak = 3
        loop.judge_last_verdict = {"outcome": "quiet", "evidence_items": 4, "at": 1234.0}
        loop.judge_cursors = {"chat-2-2": 7}
        return _serialize_for_legacy_reader(loop)

    def test_every_judge_field_the_reader_declares_is_published(self) -> None:
        source = self._TS.read_text(encoding="utf-8")
        declared = set(re.findall(r"^\s{2}(judge[a-z_]*)\??:", source, re.MULTILINE))
        assert declared, "no judge field found; this test is looking in the wrong place"
        published = self._published(judge={"wake_when": "a line starts with RULING"})
        missing = sorted(name for name in declared if name not in published)
        assert not missing, f"the dashboard reads {missing}, which the list route does not publish"

    def test_the_criterion_the_reader_looks_up_is_the_one_sent(self) -> None:
        """``judgeReading`` keys into the brief, so the inner names matter too."""
        source = self._TS.read_text(encoding="utf-8")
        inner = set(re.findall(r"loop\?\.judge\?\.([a-z_]+)", source))
        assert inner, "no criterion lookup found in the reader"
        published = self._published(
            judge={"wake_when": "a line starts with RULING", "quiet_when": "WORKING"}
        )
        for name in inner:
            assert name in published["judge"], f"judge.{name} is read but not sent"

    def test_the_read_cursors_stay_unpublished(self) -> None:
        """The counterpart bound: the reader declares no cursor and must be sent none.

        A cursor names each watched target, so publishing it would disclose the
        subject list on a route with no owner gate.
        """
        published = self._published(judge={"wake_when": "x"})
        assert "judge_cursors" not in published
        assert "judge_cursors" not in self._TS.read_text(encoding="utf-8")


class TestNudgeWakeConfigSection:
    """``decisions.nudge_wake`` -- the two knobs the tick reads, and their bounds.

    The tick reads both through ``getattr`` chains with their own fallbacks, so it
    worked before this section existed. What these tests pin is that the section is
    now REACHABLE -- an operator who writes the key gets the behaviour -- and that
    registering it did not introduce a second spelling of the floor.
    """

    def test_the_shipped_default_is_the_lane_resolver_s_own_fallback(self) -> None:
        """An install that never wrote the key takes ``auto``."""
        from kiro_crew.config.sections import DecisionsConfig

        assert DecisionsConfig().nudge_wake.provider == "auto"
        assert DecisionsConfig.from_raw({}).nudge_wake.provider == "auto"

    def test_the_stored_floor_default_is_a_sentinel_not_a_copied_number(self) -> None:
        """0 is stored, and 0 is what the engine reads as "inherit".

        The floor is deliberately NOT spelled in the config section. The engine owns
        the number because its probe path answers the same question, and a copy here
        would be a second literal to keep in step.
        """
        from kiro_crew.config.sections import DecisionsConfig

        assert DecisionsConfig().nudge_wake.quiet_streak_floor == 0
        # The binding this section relies on: the engine's default IS the probe's.
        assert _JUDGE_QUIET_STREAK_FLOOR_DEFAULT == _MAX_QUIET_STREAK

    def test_the_engine_resolves_the_sentinel_to_the_shipped_floor(self) -> None:
        """A stored 0 becomes the probe's floor, through the engine's real reader."""
        from kiro_crew.config.sections import DecisionsConfig

        stored = DecisionsConfig.from_raw({}).nudge_wake

        class _Snapshot:
            decisions = DecisionsConfig(nudge_wake=stored)

        service = AutoNudgeService.__new__(AutoNudgeService)
        with patch("kiro_crew.config.live.snapshot", return_value=_Snapshot()):
            assert service._judge_quiet_streak_floor() == _MAX_QUIET_STREAK

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ({"provider": "jev"}, "jev"),
            ({"provider": "  Jev  "}, "jev"),
            ({"provider": "LLM"}, "llm"),
            # An unknown name folds to ``auto``. The lane vocabulary is closed, so a
            # typo must not read as a third lane, and it must not stop the gateway
            # booting either -- which is why it normalises rather than raising.
            ({"provider": "jevv"}, "auto"),
            ({"provider": ""}, "auto"),
            ({"provider": None}, "auto"),
            ({"provider": 7}, "auto"),
        ],
    )
    def test_the_lane_is_normalised_so_the_saved_config_says_what_is_in_force(
        self, raw: dict, expected: str
    ) -> None:
        from kiro_crew.config.sections import DecisionsConfig

        assert DecisionsConfig.from_raw({"nudge_wake": raw}).nudge_wake.provider == expected

    @pytest.mark.parametrize(
        "raw, stored",
        [
            ({"quiet_streak_floor": 3}, 3),
            ({"quiet_streak_floor": 0}, 0),
            # Negative and malformed both read as inherit rather than as unbounded.
            ({"quiet_streak_floor": -5}, 0),
            ({"quiet_streak_floor": "lots"}, 0),
            ({"quiet_streak_floor": None}, 0),
            # Stored as written; the engine clamps to its own ceiling on read,
            # because the ceiling is the engine's constant.
            ({"quiet_streak_floor": 9999}, 9999),
        ],
    )
    def test_the_floor_is_coerced_without_ever_reading_as_unbounded(
        self, raw: dict, stored: int
    ) -> None:
        from kiro_crew.config.sections import DecisionsConfig

        assert DecisionsConfig.from_raw({"nudge_wake": raw}).nudge_wake.quiet_streak_floor == stored

    def test_an_over_large_stored_floor_is_clamped_by_the_engine(self) -> None:
        """The knob only ever shortens the window, so the shipped floor IS the ceiling.

        ``config.json`` is agent-writable, and a floor that could be raised would be a
        second way to silence a watch with no new code -- only a number.
        """
        from kiro_crew.config.sections import DecisionsConfig

        stored = DecisionsConfig.from_raw({"nudge_wake": {"quiet_streak_floor": 9999}}).nudge_wake

        class _Snapshot:
            decisions = DecisionsConfig(nudge_wake=stored)

        service = AutoNudgeService.__new__(AutoNudgeService)
        with patch("kiro_crew.config.live.snapshot", return_value=_Snapshot()):
            assert service._judge_quiet_streak_floor() == _JUDGE_QUIET_STREAK_FLOOR_DEFAULT

    @pytest.mark.parametrize("section", [None, "jev", 7, [], True])
    def test_a_non_object_section_reads_as_the_defaults(self, section: object) -> None:
        """A hand-edited config.json must not stop the gateway booting."""
        from kiro_crew.config.sections import DecisionsConfig

        got = DecisionsConfig.from_raw({"nudge_wake": section}).nudge_wake
        assert got.provider == "auto"
        assert got.quiet_streak_floor == 0

    def test_registering_the_section_did_not_disturb_the_rest_of_decisions(self) -> None:
        """The sibling keys still parse, including alongside a judge section."""
        from kiro_crew.config.sections import DecisionsConfig

        got = DecisionsConfig.from_raw(
            {
                "bucket": 42,
                "history_budget_chars": 1234,
                "provider": {"model": "some-model"},
                "nudge_wake": {"provider": "jev"},
            }
        )
        assert got.bucket == 42
        assert got.history_budget_chars == 1234
        assert got.provider.model == "some-model"
        assert got.nudge_wake.provider == "jev"


class TestTheHttpArmingRouteCarriesTheBrief:
    """``POST /api/autonudge`` -- the dashboard's plain-HTTP arming route.

    The MCP tool surface is not the only way a loop is armed, so this route has to
    carry a brief too. A route that read no ``judge`` and called
    ``authorize_and_add_nudge`` without one would answer 200 and tell the caller a
    loop was armed while the judge was not -- the exact failure
    ``session_directive_apply``'s own comment warns about, and the reason the route
    is tested here rather than only at the tool.
    """

    def _app(self, monkeypatch, fake_svc):
        from aiohttp import web

        from kiro_crew.dashboard.handlers import autonudge as _handler

        monkeypatch.setattr(_handler, "_autonudge_get", lambda: fake_svc)
        state = MagicMock()
        state._slots = {
            "chat-1-123": MagicMock(workspace="default", memory_mode="persistent", mode="chat")
        }
        app = web.Application(middlewares=[_owner_dashboard_identity()])
        app["state"] = state
        app.router.add_post("/api/autonudge", _handler.api_autonudge_start)
        return app

    def _svc(self):
        svc = MagicMock()
        loop = NudgeLoop(id="loop-1", slot_key="chat-1-123", message="go")
        svc.add = AsyncMock(return_value=loop)
        svc.list_all = lambda: [loop]
        svc.get_by_id = lambda _id, _rows=[loop]: next(
            (r for r in _rows if getattr(r, "id", None) == _id), None
        )
        return svc

    @pytest.mark.asyncio
    async def test_a_body_carrying_a_judge_arms_a_loop_that_holds_the_spec(
        self, monkeypatch
    ) -> None:
        """The brief reaches the loop record rather than being dropped."""
        from aiohttp.test_utils import TestClient, TestServer

        brief = {
            "wake_when": "a worker line starts with RULING",
            "quiet_when": "workers report WORKING with no new status",
        }
        svc = self._svc()
        async with TestClient(TestServer(self._app(monkeypatch, svc))) as client:
            resp = await client.post(
                "/api/autonudge",
                json={"slot_key": "chat-1-123", "message": "go", "judge": brief},
            )
            assert resp.status == 200
        assert svc.add.await_args.kwargs["judge"] == brief

    @pytest.mark.asyncio
    async def test_an_invalid_judge_is_refused_with_400_and_arms_nothing(self, monkeypatch) -> None:
        """A refusal names the field, and no loop is armed carrying a bad brief.

        The refusal has to happen HERE: the chokepoint takes the brief through
        unchanged by design, so a route that forwarded an unchecked object would be
        the way around ``validate_judge_spec``'s bounds.
        """
        from aiohttp.test_utils import TestClient, TestServer

        svc = self._svc()
        async with TestClient(TestServer(self._app(monkeypatch, svc))) as client:
            resp = await client.post(
                "/api/autonudge",
                json={
                    "slot_key": "chat-1-123",
                    "message": "go",
                    "judge": {"wake_when": "x", "not_a_real_key": "y"},
                },
            )
            assert resp.status == 400
            payload = await resp.json()
            assert payload["code"] == "invalid_judge_spec"
            assert "not_a_real_key" in payload["error"]
        svc.add.assert_not_awaited(), "an invalid judge brief still armed the loop"

    @pytest.mark.asyncio
    async def test_a_body_with_no_judge_arms_a_loop_with_no_brief(self, monkeypatch) -> None:
        """Negative control: the route must not invent a brief for a caller.

        Without this, returning ``{}`` as a truthy-looking value would give every
        loop armed from the goal popover an empty judge and a judge tick it never
        asked for.
        """
        from aiohttp.test_utils import TestClient, TestServer

        svc = self._svc()
        async with TestClient(TestServer(self._app(monkeypatch, svc))) as client:
            resp = await client.post(
                "/api/autonudge",
                json={"slot_key": "chat-1-123", "message": "go"},
            )
            assert resp.status == 200
        assert "judge" not in svc.add.await_args.kwargs

    @pytest.mark.asyncio
    async def test_a_non_object_judge_is_400_not_500(self, monkeypatch) -> None:
        """A hand-written request body must not reach a traceback."""
        from aiohttp.test_utils import TestClient, TestServer

        svc = self._svc()
        async with TestClient(TestServer(self._app(monkeypatch, svc))) as client:
            resp = await client.post(
                "/api/autonudge",
                json={"slot_key": "chat-1-123", "message": "go", "judge": "jev"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_judge_spec"
        svc.add.assert_not_awaited()


class TestTheLoopPayloadPublishesNoEvidence:
    """What `GET /api/autonudge` may carry about a judge.

    The route has no per-owner gate -- its own docstring says it publishes presence,
    cadence, liveness and state, never what is being watched -- and `_serialize`
    is `asdict`, so a field joins these reads simply by existing on the dataclass.
    That is how `judge_cursors` reached them: not by a decision, but by being added.
    """

    def _payload(self, **judge_kw):
        from kiro_crew.dashboard.handlers.autonudge import _serialize_for_legacy_reader

        loop = NudgeLoop(id="l1", slot_key="chat-1-1", message="patrol chat-2-2", **judge_kw)
        return _serialize_for_legacy_reader(loop)

    def test_read_cursors_are_never_published(self) -> None:
        """They name every watched target and no reader can act on them."""
        payload = self._payload(judge_cursors={"chat-2-2": 41, "chat-3-3": 7})
        assert "judge_cursors" not in payload

    def test_the_verdict_carries_counts_and_never_evidence_text(self) -> None:
        """The stored verdict is text-free by construction, and stays that way.

        `verdict_record` builds it from the outcome, an item COUNT and a timestamp,
        so a transcript line the judge read cannot reach a reader of this list even
        though the verdict it produced can.
        """
        import json as _json

        secret = "SECRET-TRANSCRIPT-LINE-do-not-publish"
        verdict = judge.verdict_record(
            type("V", (), {"outcome": type("O", (), {"value": "progress_only"})()})(), 3
        )
        assert secret not in _json.dumps(verdict)
        assert set(verdict) == {"outcome", "evidence_items", "at"}
        assert verdict["evidence_items"] == 3

        payload = self._payload(judge_last_verdict=verdict, judge_quiet_streak=2)
        blob = _json.dumps(payload)
        assert secret not in blob
        # The two readings the popover needs DO survive, so withholding the cursor
        # is not a blanket refusal to report the judge.
        assert payload["judge_last_verdict"]["outcome"] == "progress_only"
        assert payload["judge_last_verdict"]["evidence_items"] == 3
        assert payload["judge_quiet_streak"] == 2

    def test_no_judge_field_carries_a_nested_evidence_list(self) -> None:
        """A shape check rather than a string check: the record must stay flat.

        A future verdict that carried its evidence for context would pass a
        substring test against this test's own literal while publishing every
        transcript line it read.
        """
        payload = self._payload(
            judge={"wake_when": "a RULING line", "quiet_when": "still WORKING"},
            judge_last_verdict=judge.verdict_record(
                type("V", (), {"outcome": type("O", (), {"value": "quiet"})()})(), 5
            ),
        )
        for key, value in payload.items():
            if not key.startswith("judge"):
                continue
            if isinstance(value, dict):
                assert not any(
                    isinstance(inner, (list, tuple)) for inner in value.values()
                ), f"{key} carries a nested sequence, which is how evidence would travel"


class TestTheReadingHappensBeforeTheJudge:
    """The subject is READ first, the core maps a terminal, and the judge gets the rest.

    The order matters because the judge emits no terminal of its own, by design: it
    reads prose a third party wrote, and ending a watch is the one verdict an owner
    cannot recover by waiting. So the merged-or-closed mapping is taken from a typed
    fact by the core, before the judge is consulted -- otherwise a judged pull-request
    loop would answer first, nothing would notice a finished subject, and the watch
    would outlive the thing it watches, bounded only by its cycle cap.

    Everything that is not terminal reaches the judge, including a failing check: what
    a red lane means for THIS loop is the owner's criterion, not the reader's.
    """

    @staticmethod
    def _judged_pr_loop() -> NudgeLoop:
        """A loop that watches a pull request AND carries a judge brief.

        This pair is the configuration the defect needed, and it is first-class:
        ``monitor_start`` accepts a brief on any monitor, so nothing about it is rare.
        """
        loop = NudgeLoop(
            id="judged-pr",
            slot_key="chat-1-123",
            message="Watch https://github.com/acme/widgets/pull/42 until green",
            idle_secs=30,
            monitor=MonitorState(
                kind="gh-pr",
                target="acme/widgets#42",
                objective="review_ready",
                created_ts=1_000.0,
            ),
            gate=True,
        )
        loop.judge = {"wake_when": "a review asks for a change", "quiet_when": "nothing did"}
        return loop

    @staticmethod
    def _spy(service: AutoNudgeService, answer: bool | None) -> list[str]:
        """Record whether the judge was consulted at all, and with which loop."""
        calls: list[str] = []

        async def _judge(loop: NudgeLoop) -> bool | None:
            calls.append(loop.id)
            return answer

        service._judge_tick_is_quiet = _judge  # type: ignore[method-assign]
        return calls

    def _two_ticks(self, tmp_path: Any, monkeypatch: Any, first: Any, second: Any) -> bool:
        """Drive two ticks on ONE service and return the second tick's answer.

        One service, because the comparison under test is between a reading and the
        one stored by the tick before it -- a fresh service per tick has no previous
        reading and could only ever answer "changed".
        """
        import kiro_crew.autonudge as _an

        async def on_fire(loop: NudgeLoop) -> bool:
            return True

        readings = [first, second]

        def _poll(identity, message, probe):
            probe.observation = readings.pop(0) if readings else second
            return _an.irq.Verdict(Outcome.QUIET, "pinned")

        monkeypatch.setattr(_an.irq, "poll", _poll)
        service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
        loop = self._judged_pr_loop()
        service._loops[loop.id] = loop
        self._spy(service, None)
        try:
            asyncio.run(service._monitor_tick_is_quiet(loop))
            return asyncio.run(service._monitor_tick_is_quiet(loop))
        finally:
            service.stop()

    @staticmethod
    def _observation(**fields: Any):
        from kiro_crew.probes import gh_pr

        base: dict[str, Any] = {
            "repo": "acme/widgets",
            "pr": 42,
            "host": "github.com",
            "status": gh_pr.STATUS_OK,
            "observed_at": 1_000.0,
            "state": "OPEN",
            "head": "a" * 40,
        }
        base.update(fields)
        return gh_pr.PrObservation(**base)

    def _drive(
        self,
        tmp_path: Any,
        monkeypatch: Any,
        outcome: Outcome,
        answer: bool | None,
        observation: Any = None,
    ):
        """Run one tick with the reading and the kernel verdict both pinned.

        The fake stands in for what the real call does: ``poll`` runs ``observe``,
        which publishes the reading on the probe, and then returns the kernel's own
        verdict. Pinning only one of the two would test a state the code cannot reach.
        """
        import kiro_crew.autonudge as _an

        async def on_fire(loop: NudgeLoop) -> bool:
            return True

        def _poll(identity, message, probe):
            probe.observation = observation
            return _an.irq.Verdict(outcome, "pinned")

        monkeypatch.setattr(_an.irq, "poll", _poll)
        service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
        loop = self._judged_pr_loop()
        service._loops[loop.id] = loop
        calls = self._spy(service, answer)
        try:
            quiet = asyncio.run(service._monitor_tick_is_quiet(loop))
        finally:
            service.stop()
        return quiet, loop, calls

    def test_a_merged_pull_request_deactivates_a_judged_loop(self, tmp_path, monkeypatch) -> None:
        """The core's own mapping, and it must still land on a JUDGED loop."""
        _quiet, loop, calls = self._drive(
            tmp_path,
            monkeypatch,
            Outcome.QUIET,
            True,
            self._observation(state="MERGED", merged_at="2026-09-25T00:00:00Z"),
        )
        assert loop.monitor is not None
        assert loop.monitor.outcome is not None, "a merged subject must be recorded terminal"
        assert calls == [], "the terminal mapping wins outright -- the judge is not asked"

    def test_a_closed_pull_request_is_not_recorded_as_a_success(
        self, tmp_path, monkeypatch
    ) -> None:
        """A close without a merge ends on a question, so it must not read as done."""
        from kiro_crew.monitoring.models import MonitorOutcome

        _quiet, loop, _calls = self._drive(
            tmp_path, monkeypatch, Outcome.QUIET, True, self._observation(state="CLOSED")
        )
        assert loop.monitor is not None
        assert loop.monitor.outcome is MonitorOutcome.BLOCKED

    def test_a_blind_streak_report_fires_without_asking_the_judge(
        self, tmp_path, monkeypatch
    ) -> None:
        """The kernel's own report that the watch cannot see its subject is typed."""
        quiet, _loop, calls = self._drive(tmp_path, monkeypatch, Outcome.WAKE, True, None)
        assert quiet is False, "a watch that cannot observe its subject spends the turn"
        assert calls == [], "the judge must not be able to suppress a typed report"

    def test_a_live_reading_is_the_tick_the_judge_screens(self, tmp_path, monkeypatch) -> None:
        """A failing check is not a wake any more -- the owner's criterion decides."""
        quiet, loop, calls = self._drive(
            tmp_path, monkeypatch, Outcome.QUIET, False, self._observation()
        )
        assert calls == [loop.id], "a live reading is the tick the judge screens"
        assert quiet is False, "the judge may wake on evidence no typed reading produces"

    def test_the_reading_is_published_for_the_judge_to_collect(self, tmp_path, monkeypatch) -> None:
        """The channel: the record carries the facts, the stash carries the bodies.

        Without this the judge has no pull-request evidence at all on a gated loop --
        the record's observation field has no other writer on this path -- so an
        owner's criterion about their pull request would be read against nothing.
        """
        from kiro_crew import autonudge_judge as _judge
        from kiro_crew.probes import gh_pr

        remark = gh_pr.Remark(
            kind="review",
            ident="review:R1",
            author="a-reviewer",
            at="2026-09-25T06:00:00Z",
            age_s=120.0,
            verdict="CHANGES_REQUESTED",
            body="please guard the windows branch",
        )
        _quiet, loop, _calls = self._drive(
            tmp_path,
            monkeypatch,
            Outcome.QUIET,
            False,
            self._observation(remarks=(remark,), remarks_total=1),
        )
        assert loop.monitor is not None
        facts = loop.monitor.last_observation
        assert facts["state"] == "OPEN"
        assert facts["remarks"][0]["author"] == "a-reviewer"
        assert facts["remarks"][0]["first_seen_this_tick"] is True
        assert "body" not in facts["remarks"][0], "a durable record carries no prose"
        stashed, dropped = _judge.take_pr_bodies(loop.id)
        assert stashed == {"review:R1": "please guard the windows branch"}
        assert dropped is False, "nothing was evicted, so the tick is owed no forced fire"

    def test_an_unjudged_fetcher_tick_delivers_rather_than_counting_quiet(
        self, tmp_path, monkeypatch
    ) -> None:
        """A quiet nobody owns must not suppress a turn.

        The fetcher raises no observations, so the kernel's QUIET says only that
        nothing was DECIDED. ``None`` from the judge means no judge ran -- an explicit
        bypass, a build with no collector, or this point's fail-closed egress scope,
        which a stock install has not granted. Counting that as quiet withholds a
        failing check or a reviewer's request until the streak floor with nothing on
        screen, so the tick delivers instead.
        """
        quiet, loop, calls = self._drive(
            tmp_path, monkeypatch, Outcome.QUIET, None, self._observation()
        )
        assert calls == [loop.id]
        assert quiet is False, "an unowned quiet delivers rather than being charged as quiet"

    def test_an_update_accepted_mid_tick_is_not_reverted(self, tmp_path) -> None:
        """The staged copy is taken UNDER the lock, and that placement is the argument.

        ``_apply_staged_monitor`` copies every field of the staged loop back over the
        live one, so a copy taken before the lock reverts an update accepted in between
        -- in memory and on disk alike, since the snapshot written carries the stale
        value too and leaves nothing to recover from.
        """

        async def on_fire(loop: NudgeLoop) -> bool:
            return True

        service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
        loop = self._judged_pr_loop()
        service._loops[loop.id] = loop
        state = loop.monitor
        assert state is not None

        async def scenario() -> None:
            # The ONE window an update can land in is while this tick waits for the
            # lock: the update path takes the same lock, so it is otherwise strictly
            # before or after. Hold the lock the way ``update`` does, let the tick
            # block on it, apply the change, and release.
            entered = asyncio.Event()
            proceed = asyncio.Event()
            applied: list[str] = []

            async def updater() -> None:
                async with service._lock:
                    entered.set()
                    await proceed.wait()
                    loop.idle_secs = 4242
                    applied.append("idle_secs")

            updating = asyncio.ensure_future(updater())
            await entered.wait()
            publishing = asyncio.ensure_future(
                service._publish_pr_observation(loop, state, self._observation())
            )
            # Nothing in that method awaits before the lock, so a waiter ON the lock
            # is positive proof the tick reached it and got no further. Waiting for
            # that rather than spinning a fixed number of times is the difference
            # between pinning the window and releasing before the tick arrives.
            for _ in range(200):
                if getattr(service._lock, "_waiters", None):
                    break
                await asyncio.sleep(0)
            assert getattr(
                service._lock, "_waiters", None
            ), "the tick must be waiting for the lock before the update lands"
            proceed.set()
            published = await publishing
            await updating
            assert applied == ["idle_secs"], "the update must have landed while the tick waited"
            assert loop.idle_secs == 4242, "an update accepted mid-tick is not reverted"
            assert published is not None, "and the reading is still kept"
            assert state.last_observation, "and published to live readers"

        try:
            asyncio.run(scenario())
        finally:
            service.stop()

    def test_a_retarget_mid_tick_keeps_no_reading(self, tmp_path) -> None:
        """Revalidated UNDER the lock, because the subject can change while it waits.

        A retarget makes this reading one of a subject the loop does not watch, so
        keeping it would screen the new subject against the old one's board. Nothing
        is kept and the tick fires, the answer every uncertain path here resolves to.
        """

        async def on_fire(loop: NudgeLoop) -> bool:
            return True

        service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
        loop = self._judged_pr_loop()
        service._loops[loop.id] = loop
        state = loop.monitor
        assert state is not None
        assert not state.last_observation, "nothing is published before the tick runs"

        async def scenario() -> None:
            entered = asyncio.Event()
            proceed = asyncio.Event()
            applied: list[str] = []

            async def retarget() -> None:
                async with service._lock:
                    entered.set()
                    await proceed.wait()
                    state.target = "acme/widgets#43"
                    applied.append("target")

            retargeting = asyncio.ensure_future(retarget())
            await entered.wait()
            publishing = asyncio.ensure_future(
                service._publish_pr_observation(loop, state, self._observation())
            )
            for _ in range(200):
                if getattr(service._lock, "_waiters", None):
                    break
                await asyncio.sleep(0)
            assert getattr(
                service._lock, "_waiters", None
            ), "the tick must be waiting for the lock before the retarget lands"
            proceed.set()
            published = await publishing
            await retargeting
            assert applied == ["target"], "the retarget must have landed while it waited"
            assert published is None, "a reading of a dropped subject is not kept"
            assert not state.last_observation, "so nothing reaches live readers either"

        try:
            asyncio.run(scenario())
        finally:
            service.stop()

    def test_a_replaced_question_drops_the_pr_baseline(self, tmp_path) -> None:
        """The baseline is the reading a VERDICT was reached on, so it dies with it.

        That verdict answered the question an update has just replaced. Carrying the
        digest forward lets an unchanged board screen the NEW criteria quiet without
        ever putting them to the judge, bounded only by the streak floor. An update
        that changes neither the instruction nor the criteria leaves it alone.
        """
        baseline = {"digest": "d" * 16, "remarks": ["r1"]}

        async def main() -> None:
            svc = AutoNudgeService(base_dir=tmp_path)
            try:
                loop = await svc.add(
                    "chat-1-1",
                    "Watch https://github.com/acme/widgets/pull/42 until green",
                    idle_secs=60,
                    judge={"wake_when": "a review asks for a change"},
                )

                loop.judge_pr_seen = dict(baseline)
                revised = await svc.update(loop.id, judge={"wake_when": "a check goes red"})
                assert revised is not None
                assert revised.judge_pr_seen == {}, "replaced criteria drop the baseline"

                loop.judge_pr_seen = dict(baseline)
                moved = await svc.update(
                    loop.id,
                    message="Watch https://github.com/acme/widgets/pull/43 until green",
                )
                assert moved is not None
                assert moved.judge_pr_seen == {}, "a replaced instruction drops it too"

                loop.judge_pr_seen = dict(baseline)
                untouched = await svc.update(loop.id, idle_secs=120)
                assert untouched is not None
                assert untouched.judge_pr_seen == baseline, "an interval change leaves it"
            finally:
                svc.stop()

        asyncio.run(main())

    def test_a_failed_persist_publishes_nothing_and_fires(self, tmp_path, monkeypatch) -> None:
        """The durable record owns this state, so the write is a precondition.

        A restart restores the record and the dashboard reads it, so publishing in
        memory beside a fire-and-forget write would let a kill inside that window leave
        the record on the previous tick's facts while readers had already seen the new
        ones. The write is awaited on a staged copy; a write that will not land
        publishes nothing and fires, because a reading the record could not keep is not
        ground for staying quiet.
        """
        import kiro_crew.autonudge as _an

        async def on_fire(loop: NudgeLoop) -> bool:
            return True

        reading = self._observation()

        def _poll(identity, message, probe):
            probe.observation = reading
            return _an.irq.Verdict(Outcome.QUIET, "pinned")

        monkeypatch.setattr(_an.irq, "poll", _poll)
        service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
        loop = self._judged_pr_loop()
        service._loops[loop.id] = loop
        self._spy(service, True)

        async def _refuse(*_a, **_k):
            raise OSError("disk went away")

        monkeypatch.setattr(service, "_persist_staged_monitor_locked", _refuse)
        try:
            assert (
                asyncio.run(service._monitor_tick_is_quiet(loop)) is False
            ), "a reading the record could not keep must not hold the loop quiet"
            state = loop.monitor
            assert state is not None
            assert not state.last_observation, "nothing is published when the write fails"
            assert loop.judge_pr_seen == {}, "and no baseline is consumed either"
        finally:
            service.stop()

    def test_a_cancelled_judge_await_consumes_nothing(self, tmp_path, monkeypatch) -> None:
        """The reading is published before the judge is asked, and that ask is awaited.

        Ordinary user input cancels the tick at exactly that await, because it runs
        before the loop enters ``_firing``. If the baseline advanced with the PUBLISH, a
        new comment would be marked seen on a tick that reached no verdict -- and the
        next tick would read it as old while the digest matched too, so both the delta
        and the unchanged check would suppress a wake nobody ever judged. So the
        baseline advances with the VERDICT, and a cancelled tick leaves it untouched.
        """
        import kiro_crew.autonudge as _an

        async def on_fire(loop: NudgeLoop) -> bool:
            return True

        reading = self._observation()

        def _poll(identity, message, probe):
            probe.observation = reading
            return _an.irq.Verdict(Outcome.QUIET, "pinned")

        monkeypatch.setattr(_an.irq, "poll", _poll)
        service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
        loop = self._judged_pr_loop()
        service._loops[loop.id] = loop

        async def _cancelled(_loop: NudgeLoop) -> bool | None:
            raise asyncio.CancelledError()

        service._judge_tick_is_quiet = _cancelled  # type: ignore[method-assign]
        try:
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(service._monitor_tick_is_quiet(loop))
            assert (
                loop.judge_pr_seen == {}
            ), "a tick that reached no verdict must consume no remark and no digest"
            self._spy(service, None)
            assert (
                asyncio.run(service._monitor_tick_is_quiet(loop)) is False
            ), "the same reading must still deliver, since nothing judged it yet"
            assert loop.judge_pr_seen, "the verdict this time commits the baseline"
        finally:
            service.stop()

    def test_a_retarget_during_the_judge_leaves_the_baseline_cleared(
        self, tmp_path, monkeypatch
    ) -> None:
        """The baseline answers a question an update is allowed to replace mid-tick.

        A retarget clears ``judge_pr_seen`` deliberately, because a digest earned under
        the old question would screen the new one quiet. The commit runs past an await
        the update lands inside, so an unconditional commit puts the cleared value back
        and suppresses the watch the owner just re-aimed until the streak floor. Same
        comparison the judge call already makes twice, applied to the baseline.
        """
        import kiro_crew.autonudge as _an

        async def on_fire(loop: NudgeLoop) -> bool:
            return True

        reading = self._observation()

        def _poll(identity, message, probe):
            probe.observation = reading
            return _an.irq.Verdict(Outcome.QUIET, "pinned")

        monkeypatch.setattr(_an.irq, "poll", _poll)
        service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
        loop = self._judged_pr_loop()
        service._loops[loop.id] = loop

        async def _retarget_then_answer(inner: NudgeLoop) -> bool | None:
            # What ``update`` does to a retargeted loop, at the point it really lands:
            # inside the await, after the reading was staged against the old message.
            inner.message = "Watch https://github.com/acme/widgets/pull/43 until green"
            inner.judge_pr_seen = {}
            return None

        service._judge_tick_is_quiet = _retarget_then_answer  # type: ignore[method-assign]
        try:
            assert asyncio.run(service._monitor_tick_is_quiet(loop)) is False
            assert loop.judge_pr_seen == {}, "a re-aimed loop keeps its baseline cleared"
        finally:
            service.stop()

    @staticmethod
    def _stored_baseline(tmp_path: Any, loop_id: str) -> dict:
        """What the record on disk holds for this loop's baseline."""
        import json

        rows = json.loads((tmp_path / "autonudge.json").read_text())["loops"]
        return next(row for row in rows if row["id"] == loop_id)["judge_pr_seen"]

    @staticmethod
    def _tap_commit_writes(
        service: AutoNudgeService,
        loop: NudgeLoop,
        answers: list[bool],
        *,
        on_refuse: Any = None,
    ) -> list[tuple[dict, dict]]:
        """Intercept the writes that CARRY a baseline memory does not yet hold.

        Every store write goes through ``_write_monitor_snapshot_locked`` on this path,
        and the reading's own write carries whatever baseline memory already has. A
        write whose row disagrees with memory is therefore a commit, and that is the
        property under test -- the baseline reaches disk before it reaches memory. Each
        commit pops one answer: ``True`` lets the real write through, ``False`` refuses
        it after calling *on_refuse*. Returns ``(written, in_memory)`` per commit.
        """
        real = service._write_monitor_snapshot_locked
        seen: list[tuple[dict, dict]] = []

        async def _write(payload: dict | None = None, **kwargs: Any) -> None:
            # A payload-less call serializes live state, which by definition carries the
            # baseline memory already holds; only a staged payload can be a commit.
            row = None
            if payload is not None:
                row = next((r for r in payload["loops"] if r["id"] == loop.id), None)
            if row is not None and row["judge_pr_seen"] != loop.judge_pr_seen:
                seen.append((dict(row["judge_pr_seen"]), dict(loop.judge_pr_seen or {})))
                if not answers.pop(0):
                    if on_refuse is not None:
                        on_refuse()
                    raise OSError("disk went away")
            await real(payload, **kwargs)

        service._write_monitor_snapshot_locked = _write  # type: ignore[method-assign]
        return seen

    def test_the_baseline_is_committed_with_an_awaited_write(self, tmp_path, monkeypatch) -> None:
        """The stored record is this baseline's authority, so memory may not run ahead of it.

        The awaited judge write inside the judge call has already landed a snapshot
        holding the OLD baseline. A scheduled write that has not landed when the process
        stops therefore leaves disk claiming the old baseline while this tick has already
        screened against the new one, and every remark it passed on reads as new after
        the restart.

        Pinned by WHAT THE WRITE SEES rather than by counting calls: the judge call makes
        awaited writes of its own, so a call count cannot tell them from this one. A
        write that carries the committed baseline while memory still holds the old one
        can only be the commit, and that ordering -- disk first -- is exactly what is at
        issue.
        """
        import kiro_crew.autonudge as _an

        async def on_fire(loop: NudgeLoop) -> bool:
            return True

        reading = self._observation()

        def _poll(identity, message, probe):
            probe.observation = reading
            return _an.irq.Verdict(Outcome.QUIET, "pinned")

        monkeypatch.setattr(_an.irq, "poll", _poll)
        service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
        loop = self._judged_pr_loop()
        service._loops[loop.id] = loop

        async def _answer(inner: NudgeLoop) -> bool | None:
            return None

        service._judge_tick_is_quiet = _answer  # type: ignore[method-assign]
        commits = self._tap_commit_writes(service, loop, [True])
        try:
            asyncio.run(service._monitor_tick_is_quiet(loop))
            assert loop.judge_pr_seen, "the tick staged a baseline to commit"
            assert len(commits) == 1, "exactly one write carried the new baseline"
            written, in_memory = commits[0]
            assert written == loop.judge_pr_seen, "the write carries what memory now holds"
            assert in_memory == {}, "and memory still held the old baseline while it was written"
            assert self._stored_baseline(tmp_path, loop.id) == loop.judge_pr_seen
        finally:
            service.stop()

    def test_a_baseline_whose_write_does_not_land_is_never_published(
        self, tmp_path, monkeypatch
    ) -> None:
        """Memory may not claim what disk does not.

        Publishing before the write would let this tick screen against a baseline the
        record does not hold, so a restart re-reads the remarks it passed on -- except
        nothing ever fires to reveal it, because memory believes they are seen. So the
        commit reaches memory only once it has reached disk, and a refused write leaves
        both exactly as they were.
        """
        import kiro_crew.autonudge as _an

        async def on_fire(loop: NudgeLoop) -> bool:
            return True

        reading = self._observation()

        def _poll(identity, message, probe):
            probe.observation = reading
            return _an.irq.Verdict(Outcome.QUIET, "pinned")

        monkeypatch.setattr(_an.irq, "poll", _poll)
        service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
        loop = self._judged_pr_loop()
        service._loops[loop.id] = loop

        async def _answer(inner: NudgeLoop) -> bool | None:
            return None

        service._judge_tick_is_quiet = _answer  # type: ignore[method-assign]
        commits = self._tap_commit_writes(service, loop, [False])
        try:
            asyncio.run(service._monitor_tick_is_quiet(loop))
            assert len(commits) == 1, "the commit was attempted"
            assert (
                loop.judge_pr_seen == {}
            ), "a baseline whose write was refused must never reach memory"
            assert self._stored_baseline(tmp_path, loop.id) == {}
        finally:
            service.stop()

    def test_a_refused_write_leaves_the_prior_baseline_in_place(
        self, tmp_path, monkeypatch
    ) -> None:
        """Refusing the commit means keeping what the record holds, not emptying it.

        The awaited judge write inside the judge call has already landed a snapshot
        holding the PRIOR baseline, so that is the value memory must still carry once
        this tick's write is refused. An empty one matches nothing on disk: the next tick
        would call every remark in the horizon new, spend a turn on remarks the record
        already knows, and keep doing so until a write lands.
        """
        import kiro_crew.autonudge as _an

        async def on_fire(loop: NudgeLoop) -> bool:
            return True

        reading = self._observation()

        def _poll(identity, message, probe):
            probe.observation = reading
            return _an.irq.Verdict(Outcome.QUIET, "pinned")

        monkeypatch.setattr(_an.irq, "poll", _poll)
        service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
        loop = self._judged_pr_loop()
        prior = {"digest": "earlier-reading", "remarks": ["review:R0"]}
        loop.judge_pr_seen = dict(prior)
        service._loops[loop.id] = loop

        async def _answer(inner: NudgeLoop) -> bool | None:
            return None

        service._judge_tick_is_quiet = _answer  # type: ignore[method-assign]
        commits = self._tap_commit_writes(service, loop, [False])
        try:
            asyncio.run(service._monitor_tick_is_quiet(loop))
            assert len(commits) == 1, "the commit was attempted"
            assert (
                loop.judge_pr_seen == prior
            ), "a refused write must leave the baseline the stored record holds, not an empty one"
            assert self._stored_baseline(tmp_path, loop.id) == prior
        finally:
            service.stop()

    def test_memory_and_disk_agree_however_a_refusal_interleaves(
        self, tmp_path, monkeypatch
    ) -> None:
        """The invariant a rollback cannot give: no writer ever sees a half-committed baseline.

        A commit that published to memory first and rolled back on refusal would leave a
        window in which another writer -- an update, a delivery label, a scheduled
        write -- serializes the loop with the NEW baseline and lands it, after which the
        rollback rewinds memory alone and the two disagree. Here the commit is the only
        thing that changes memory and it does so after the write, so the latest snapshot
        any other writer can take before the refusal still carries the prior baseline,
        and memory, that snapshot, and the record all agree afterwards.
        """
        import kiro_crew.autonudge as _an

        async def on_fire(loop: NudgeLoop) -> bool:
            return True

        reading = self._observation()

        def _poll(identity, message, probe):
            probe.observation = reading
            return _an.irq.Verdict(Outcome.QUIET, "pinned")

        monkeypatch.setattr(_an.irq, "poll", _poll)
        service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
        loop = self._judged_pr_loop()
        prior = {"digest": "earlier-reading", "remarks": ["review:R0"]}
        loop.judge_pr_seen = dict(prior)
        service._loops[loop.id] = loop

        async def _answer(inner: NudgeLoop) -> bool | None:
            return None

        concurrent: list[dict] = []

        def _another_writer_snapshots() -> None:
            # What any other writer serializes at the last instant before the refusal.
            rows = service._serialize_state()["loops"]
            concurrent.append(next(r for r in rows if r["id"] == loop.id)["judge_pr_seen"])

        service._judge_tick_is_quiet = _answer  # type: ignore[method-assign]
        self._tap_commit_writes(service, loop, [False], on_refuse=_another_writer_snapshots)
        try:
            asyncio.run(service._monitor_tick_is_quiet(loop))
            assert concurrent == [prior], "no other writer can ever snapshot the new baseline"
            assert loop.judge_pr_seen == prior
            assert self._stored_baseline(tmp_path, loop.id) == prior
        finally:
            service.stop()

    def test_a_streak_cleared_during_the_commit_write_stays_cleared(
        self, tmp_path, monkeypatch
    ) -> None:
        """The commit publishes ONE field, so it cannot revert another writer's change.

        ``notify_cycle_landed`` clears the start-failure streak on the live loop
        synchronously and without the lock, from the turn-completion path, so it can
        land while the baseline write is in flight. A commit that copied the whole
        staged snapshot back over the live loop would put the streak back silently, and
        the loop would back off or stand down on a failure a completed turn had just
        disproved.
        """
        import kiro_crew.autonudge as _an

        async def on_fire(loop: NudgeLoop) -> bool:
            return True

        reading = self._observation()

        def _poll(identity, message, probe):
            probe.observation = reading
            return _an.irq.Verdict(Outcome.QUIET, "pinned")

        monkeypatch.setattr(_an.irq, "poll", _poll)
        service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
        loop = self._judged_pr_loop()
        loop.consecutive_start_failures = 3
        service._loops[loop.id] = loop

        async def _answer(inner: NudgeLoop) -> bool | None:
            return None

        real = service._write_monitor_snapshot_locked

        async def _turn_lands_mid_write(payload: dict | None = None, **kwargs: Any) -> None:
            await real(payload, **kwargs)
            if payload is None:
                return
            row = next(r for r in payload["loops"] if r["id"] == loop.id)
            if row["judge_pr_seen"] != loop.judge_pr_seen:
                # What the turn-completion hook does, at the point it really can: after
                # the commit's snapshot was taken and before the commit publishes.
                service.notify_cycle_landed(loop.slot_key)

        service._judge_tick_is_quiet = _answer  # type: ignore[method-assign]
        service._write_monitor_snapshot_locked = _turn_lands_mid_write  # type: ignore[method-assign]
        try:
            asyncio.run(service._monitor_tick_is_quiet(loop))
            assert loop.judge_pr_seen, "the baseline was committed"
            assert (
                loop.consecutive_start_failures == 0
            ), "a streak cleared while the baseline was written must stay cleared"
        finally:
            service.stop()

    def test_a_commit_write_that_lands_and_is_then_cancelled_still_publishes(
        self, tmp_path, monkeypatch
    ) -> None:
        """Disk holds the new baseline, so memory must match it before the cancel goes on.

        The snapshot writer absorbs cancellation until the executor write settles and
        raises it only afterwards, so at that point the record already carries the
        committed baseline. Leaving memory on the old one would hand the next tick a
        record it does not match, which is the disagreement this whole commit exists to
        rule out.
        """
        import kiro_crew.autonudge as _an

        async def on_fire(loop: NudgeLoop) -> bool:
            return True

        reading = self._observation()

        def _poll(identity, message, probe):
            probe.observation = reading
            return _an.irq.Verdict(Outcome.QUIET, "pinned")

        monkeypatch.setattr(_an.irq, "poll", _poll)
        service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
        loop = self._judged_pr_loop()
        service._loops[loop.id] = loop

        async def _answer(inner: NudgeLoop) -> bool | None:
            return None

        real = service._write_monitor_snapshot_locked

        async def _lands_then_cancelled(payload: dict | None = None, **kwargs: Any) -> None:
            await real(payload, **kwargs)
            # A payload-less write serializes live state; the reading's own write stages
            # a payload too. The commit is the one whose row carries a baseline memory
            # does not yet hold.
            if payload is None:
                return
            row = next(r for r in payload["loops"] if r["id"] == loop.id)
            if row["judge_pr_seen"] != loop.judge_pr_seen:
                raise asyncio.CancelledError()

        service._judge_tick_is_quiet = _answer  # type: ignore[method-assign]
        service._write_monitor_snapshot_locked = _lands_then_cancelled  # type: ignore[method-assign]
        try:
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(service._monitor_tick_is_quiet(loop))
            assert loop.judge_pr_seen, "a write that landed is published before the cancel goes on"
            assert self._stored_baseline(tmp_path, loop.id) == loop.judge_pr_seen
        finally:
            service.stop()

    def test_the_tick_after_a_refused_write_stamps_only_the_new_remark(
        self, tmp_path, monkeypatch
    ) -> None:
        """What keeping the prior baseline is FOR: the next reading is screened against it.

        Three ticks on one service. The first commits a baseline holding one remark. The
        second reads a new remark beside it but its write is refused. The third reads
        the same pair and must stamp ``first_seen_this_tick`` only on the remark the
        stored baseline has never held -- an emptied baseline would stamp both.
        """
        import kiro_crew.autonudge as _an
        from kiro_crew.probes import gh_pr

        async def on_fire(loop: NudgeLoop) -> bool:
            return True

        def _remark(ident: str) -> Any:
            return gh_pr.Remark(
                kind="comment",
                ident=ident,
                author="a-reviewer",
                at="2026-09-25T06:00:00Z",
                age_s=120.0,
                verdict="",
                body="please guard the windows branch",
            )

        first_only = self._observation(remarks=(_remark("comment:C1"),), remarks_total=1)
        both = self._observation(
            remarks=(_remark("comment:C1"), _remark("comment:C2")), remarks_total=2
        )
        readings = [first_only, both, both]

        def _poll(identity, message, probe):
            probe.observation = readings.pop(0)
            return _an.irq.Verdict(Outcome.QUIET, "pinned")

        monkeypatch.setattr(_an.irq, "poll", _poll)
        service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
        loop = self._judged_pr_loop()
        service._loops[loop.id] = loop

        async def _answer(inner: NudgeLoop) -> bool | None:
            return None

        service._judge_tick_is_quiet = _answer  # type: ignore[method-assign]
        commits = self._tap_commit_writes(service, loop, [True, False, True])
        try:
            for _ in range(3):
                asyncio.run(service._monitor_tick_is_quiet(loop))
            assert len(commits) == 3, "every tick reached a verdict and tried to commit"
            assert loop.monitor is not None
            remarks = loop.monitor.last_observation["remarks"]
            assert isinstance(remarks, list)
            stamped = {row["id"]: row["first_seen_this_tick"] for row in remarks}
            assert stamped == {
                "comment:C1": False,
                "comment:C2": True,
            }, "only the remark the stored baseline never held reads as new"
            assert self._stored_baseline(tmp_path, loop.id) == loop.judge_pr_seen
        finally:
            service.stop()

    def test_an_unchanged_reading_is_quiet_even_with_no_judge(self, tmp_path, monkeypatch) -> None:
        """The one judge-less quiet that is earned rather than assumed.

        No criterion ABOUT the subject can have become true while the subject did not
        change, so an identical reading withholds nothing -- which is what keeps an
        unchanged board free on a machine where no judge runs. The streak floor still
        covers a criterion about elapsed time, the one kind a digest cannot see.
        """
        first = self._observation()
        quiet_one, _loop, _calls = self._drive(tmp_path, monkeypatch, Outcome.QUIET, None, first)
        assert quiet_one is False, "the first reading has nothing to compare against"
        assert (
            self._two_ticks(tmp_path, monkeypatch, first, self._observation()) is True
        ), "a reading identical to the one before it is quiet without a judge"

    def test_a_changed_reading_still_delivers_with_no_judge(self, tmp_path, monkeypatch) -> None:
        """A digest that moved is a subject that moved, so the turn is spent."""
        from kiro_crew.probes import gh_pr

        first = self._observation()
        changed = self._observation(
            checks=(gh_pr.CheckRow("CI / Lint", "Lint", "failing", "2026-09-25T01:00:00Z"),)
        )
        assert (
            self._two_ticks(tmp_path, monkeypatch, first, changed) is False
        ), "a reading whose board moved must not be charged as quiet"

    def test_two_identical_partial_readings_are_not_an_earned_quiet(
        self, tmp_path, monkeypatch
    ) -> None:
        """A digest match means an unchanged SUBJECT only if the subject was read whole.

        A fetch that fails the same way twice produces byte-identical facts describing
        only the rows it reached, so the match says the read half did not change and
        nothing at all about the half that was not. A red required status sitting there
        would be withheld to the streak floor. Wholeness is decided by the same
        ``pr_target_is_unread`` rule the drop path uses, so the two cannot disagree.
        """
        from kiro_crew.probes import gh_pr

        partial = self._observation(
            status=gh_pr.STATUS_PARTIAL,
            incomplete=("check runs: page 2 of 3 unread",),
        )
        again = self._observation(
            status=gh_pr.STATUS_PARTIAL,
            incomplete=("check runs: page 2 of 3 unread",),
        )
        assert (
            self._two_ticks(tmp_path, monkeypatch, partial, again) is False
        ), "an incomplete reading cannot earn the judge-less quiet however stable it is"

    def test_the_digest_ignores_when_the_reading_was_taken(self) -> None:
        """Otherwise the comparison never holds once.

        ``observed_at`` is the clock and a remark's ``age_s`` grows every tick, so a
        digest carrying either would call every reading a change.
        """
        import kiro_crew.autonudge as _an

        base = {
            "target": "o/r#1",
            "observation_status": "ok",
            "observed_at": 100.0,
            "remarks": [{"id": "c1", "age_s": 10.0, "author": "someone"}],
        }
        later = {
            "target": "o/r#1",
            "observation_status": "ok",
            "observed_at": 999.0,
            "remarks": [
                {"id": "c1", "age_s": 900.0, "author": "someone", "first_seen_this_tick": False}
            ],
        }
        assert _an._pr_facts_digest(base) == _an._pr_facts_digest(later)
        moved = {**base, "observation_status": "partial"}
        assert _an._pr_facts_digest(base) != _an._pr_facts_digest(
            moved
        ), "a reading that went short of whole is a change"

    def test_a_probe_that_published_no_reading_keeps_its_own_quiet(
        self, tmp_path, monkeypatch
    ) -> None:
        """The other direction, and why the discriminator is the published reading.

        A probe that classifies reaches the same line having already found nothing,
        and that quiet is a finding of its own. Keying on the loop's shape instead
        would turn every judge-less watch into a plain timer, including the ones whose
        probe still does the judging.
        """
        quiet, loop, calls = self._drive(tmp_path, monkeypatch, Outcome.QUIET, None, None)
        assert calls == [loop.id]
        assert quiet is True, "a classifying probe's own quiet still stands"
