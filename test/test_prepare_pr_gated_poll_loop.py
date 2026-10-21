"""The prepare-pr and babysit pull-request poll loops are gated, with the judge off.

Both recipes arm ``monitor_start`` on one pull request with ``judge=False`` and the
gate left at its default. These tests arm each recipe's OWN call, as its skill spells
it, through the MCP tool and the session-directive applier, then drive ticks through
the real gate and the real fire cycle and count the turns delivered:

- an unchanged reading starts no turn, and no judge is consulted even on a host whose
  judge lane would answer, because ``judge=False`` turns it off at the loop;
- each change the recipes act on -- a new review, a new comment, an edited comment, a
  review lane's check finishing, a red check, a conflict, a new head -- starts exactly
  one turn, and the unchanged tick after it starts none;
- a merge or a close ends the loop without a turn.

``monitor_update``'s description of ``judge: false`` is pinned against the same gate.
"""

from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import autonudge as _an
from kiro_crew import autonudge_judge, decisions, session_directive, validation
from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.dashboard.session_directive_apply import apply_session_directive
from kiro_crew.mcp_tools import control
from kiro_crew.monitoring.models import MonitorOutcome
from kiro_crew.probes import gh_pr

_SKILLS = Path(__file__).resolve().parents[1] / "src/kiro_crew/builtin_skills/kirocrew-dev"
_SESSION_KEY = "dashboard:chat-1"
_BINDING = "chat-1"
_URL = "https://github.com/acme/widgets/pull/42"
_SUBJECT = "acme/widgets#42"


def _prepare_pr_recipe() -> dict[str, Any]:
    """The ``monitor_start(...)`` call prepare-pr's SKILL.md tells an agent to make.

    SKILL.md points at ``references/monitor-loop.md``, which carries the call.
    """
    text = (_SKILLS / "kirocrew-prepare-pr" / "references" / "monitor-loop.md").read_text(
        encoding="utf-8"
    )
    block = next(
        textwrap.dedent(chunk).strip()
        for chunk in text.split("```")
        if textwrap.dedent(chunk).strip().startswith("monitor_start(")
    )
    call = ast.parse(block, mode="eval").body
    assert isinstance(call, ast.Call)
    args = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords}
    args["message"] = (
        args["message"]
        .replace("https://github.com/owner/repo/pull/123", _URL)
        .replace("--pr 123", "--pr 42")
    )
    args["banner"] = args["banner"].replace("#123", "#42")
    return args


def _babysit_recipe() -> dict[str, Any]:
    """The comment-aware ``monitor_start({...})`` example in babysit's SKILL.md."""
    text = (_SKILLS / "babysit" / "SKILL.md").read_text(encoding="utf-8")
    example = text.index("## Example")
    args, _ = json.JSONDecoder().raw_decode(text[text.index("monitor_start(", example) + 14 :])
    args["message"] = args["message"].replace(
        "https://github.com/kirodotdev/KiroCrew/pull/123", _URL
    )
    return args


_RECIPES = {"prepare-pr": _prepare_pr_recipe, "babysit": _babysit_recipe}


def _reading(**fields: Any) -> gh_pr.PrObservation:
    """One whole reading of acme/widgets#42 while CI runs, unless *fields* say otherwise."""
    base: dict[str, Any] = {
        "repo": "acme/widgets",
        "pr": 42,
        "host": "github.com",
        "status": gh_pr.STATUS_OK,
        "observed_at": 1_000.0,
        "state": "OPEN",
        "mergeability": "MERGEABLE",
        "merge_state": "BLOCKED",
        "head": "a" * 40,
        "checks": (
            gh_pr.CheckRow("CI / Lint", "Lint", "passing", "2026-10-20T01:00:00Z"),
            gh_pr.CheckRow(
                "CI / Backend Tests", "Backend Tests", "pending", "2026-10-20T01:00:00Z"
            ),
            gh_pr.CheckRow(
                "Codex Review / codex-review", "codex-review", "pending", "2026-10-20T01:00:00Z"
            ),
        ),
        "checks_declared": 3,
        "checks_read": 3,
        "remarks": (_remark("comment", "comment:C0", "an earlier question", "d0"),),
        "remarks_total": 1,
    }
    base.update(fields)
    return gh_pr.PrObservation(**base)


def _remark(kind: str, ident: str, body: str, digest: str, verdict: str = "") -> gh_pr.Remark:
    return gh_pr.Remark(
        kind=kind,
        ident=ident,
        author="a-reviewer",
        at="2026-10-20T01:30:00Z",
        age_s=600.0,
        verdict=verdict,
        body=body,
        whole_body_digest=digest,
    )


def _with_check(name: str, bucket: str) -> tuple[gh_pr.CheckRow, ...]:
    """The base board with the one check named *name* moved into *bucket*."""
    return tuple(
        gh_pr.CheckRow(row.name, row.bare, bucket, row.started_at) if row.name == name else row
        for row in _reading().checks
    )


#: Every change the recipes must wake on, as the reading carries it.
_CHANGES: dict[str, dict[str, Any]] = {
    "a-new-review": {
        "remarks": (
            _remark("comment", "comment:C0", "an earlier question", "d0"),
            _remark("review", "review:R1", "please split this", "r1", "CHANGES_REQUESTED"),
        ),
        "remarks_total": 2,
        "review_decision": "CHANGES_REQUESTED",
    },
    "a-new-comment": {
        "remarks": (
            _remark("comment", "comment:C0", "an earlier question", "d0"),
            _remark("comment", "comment:C1", "why is this gated?", "c1"),
        ),
        "remarks_total": 2,
    },
    "an-edited-comment": {
        "remarks": (_remark("comment", "comment:C0", "an earlier question, edited", "d1"),),
    },
    "a-review-lane-finishes": {"checks": _with_check("Codex Review / codex-review", "passing")},
    "a-check-turns-red": {"checks": _with_check("CI / Backend Tests", "failing")},
    "a-conflict": {"mergeability": "CONFLICTING", "merge_state": "DIRTY"},
    "a-new-head": {"head": "b" * 40},
}


class _Harness:
    """A service, one loop armed from a recipe, a fake forge and a count of turns."""

    def __init__(self, tmp_path: Any, monkeypatch: Any, readings: list[gh_pr.PrObservation]):
        self.delivered: list[str] = []
        self.judge_consulted: list[str] = []
        self.expired: list[str] = []
        self._readings = list(readings)
        self._last: gh_pr.PrObservation | None = None
        monkeypatch.setattr(_an.irq, "poll", self._poll)

        async def on_fire(loop: NudgeLoop) -> bool:
            self.delivered.append(loop.id)
            return True

        self.service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
        self.service.subscribe(
            lambda event, loop: (
                self.expired.append(loop.id) if event == "expired" and loop is not None else None
            )
        )
        # A host whose judge lane WOULD answer: an evidence collector is installed and
        # both grants read true. A loop that reaches the judge leaves a mark here.
        self.service._collect_judge_evidence = AsyncMock(  # type: ignore[assignment]
            side_effect=self._consult("collect", ([], 0, {}))
        )
        monkeypatch.setattr(decisions, "judge_evidence_scope_granted", self._consult("scope", True))
        monkeypatch.setattr(decisions, "is_enabled", self._consult("lane", True))

    def _consult(self, name: str, answer: Any):
        def record(*_args: Any, **_kwargs: Any) -> Any:
            self.judge_consulted.append(name)
            return answer

        return record

    def _poll(self, _identity: str, _message: str, probe: Any) -> Any:
        # The fetcher publishes its reading on the probe and raises no observations,
        # so the kernel's own verdict is QUIET: the shape every live reading has.
        if self._readings:
            self._last = self._readings.pop(0)
        probe.observation = self._last
        return _an.irq.Verdict(_an.irq.Outcome.QUIET, "fetched", ())

    async def arm(self, recipe: dict[str, Any]) -> NudgeLoop:
        """Arm *recipe* the way the agent's call is applied: tool, directive, applier."""
        with patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_SESSION_KEY):
            acknowledgement = control.monitor_start("monitor_start", recipe)
        directive = session_directive.decode(acknowledgement, "monitor_start")
        assert directive is not None, acknowledgement
        state = SimpleNamespace(
            _slots={_BINDING: SimpleNamespace(workspace="default", is_closing=False)},
            sessions=None,
            channel_transports={},
        )
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=self.service),
            patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()),
        ):
            applied = await apply_session_directive(
                state,
                SimpleNamespace(key=_BINDING, _app=""),
                _SESSION_KEY,
                "monitor_start",
                directive,
                producer_is_user_facing=True,
            )
        loop = self.service.get_by_slot(_BINDING)
        assert loop is not None, applied
        return loop

    async def tick(self, loop: NudgeLoop) -> bool:
        """One interval: the gate decides, and a tick it does not keep quiet is fired."""
        quiet = await self.service._monitor_tick_is_quiet(loop)
        if not quiet:
            await self.service._run_fire_cycle(loop)
        return quiet

    def stop(self) -> None:
        self.service.stop()


@pytest.fixture(autouse=True)
def _enable(_floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.mark.asyncio
@pytest.mark.parametrize("recipe", sorted(_RECIPES))
async def test_the_recipe_arms_a_gated_pull_request_loop_with_the_judge_off(
    tmp_path, monkeypatch, recipe
):
    harness = _Harness(tmp_path, monkeypatch, [])
    try:
        loop = await harness.arm(_RECIPES[recipe]())
        assert loop.gate is True
        assert validation.judge_is_off(autonudge_judge.spec_of(loop))
        assert loop.monitor is not None
        assert (loop.monitor.kind, loop.monitor.target) == ("gh-pr", _SUBJECT)
        # The runtime, not the cycle cap, still ends the loop.
        assert loop.idle_secs * loop.max_cycles >= loop.max_runtime_secs
    finally:
        harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("recipe", sorted(_RECIPES))
async def test_an_unchanged_pull_request_starts_no_turn_and_consults_no_judge(
    tmp_path, monkeypatch, recipe
):
    harness = _Harness(tmp_path, monkeypatch, [_reading() for _ in range(4)])
    try:
        loop = await harness.arm(_RECIPES[recipe]())
        assert await harness.tick(loop) is False, "the first reading has nothing to compare to"
        for unchanged in range(1, 4):
            assert await harness.tick(loop) is True, f"unchanged tick {unchanged} is quiet"
        assert harness.delivered == [loop.id], "an unchanged pull request starts no turn"
        assert harness.judge_consulted == [], "judge=False keeps every judge lane out"
        assert loop.monitor is not None and loop.monitor.quiet_ticks == 3
    finally:
        harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("recipe", sorted(_RECIPES))
@pytest.mark.parametrize("change", sorted(_CHANGES))
async def test_each_change_starts_exactly_one_turn(tmp_path, monkeypatch, recipe, change):
    changed = _reading(**_CHANGES[change])
    readings = [_reading(), _reading(), changed, changed]
    harness = _Harness(tmp_path, monkeypatch, readings)
    try:
        loop = await harness.arm(_RECIPES[recipe]())
        await harness.tick(loop)
        assert await harness.tick(loop) is True
        assert await harness.tick(loop) is False, "the tick that sees the change wakes the chat"
        assert await harness.tick(loop) is True, "and the unchanged tick after it does not"
        assert harness.delivered == [loop.id, loop.id]
        assert harness.judge_consulted == []
        assert loop.active is True
    finally:
        harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("recipe", sorted(_RECIPES))
@pytest.mark.parametrize(
    ("ending", "outcome"),
    [
        ({"state": "MERGED", "merged_at": "2026-10-20T02:00:00Z"}, MonitorOutcome.SUCCESS),
        ({"state": "CLOSED"}, MonitorOutcome.BLOCKED),
    ],
    ids=["merged", "closed"],
)
async def test_a_merge_or_close_ends_the_loop_without_a_turn(
    tmp_path, monkeypatch, recipe, ending, outcome
):
    harness = _Harness(tmp_path, monkeypatch, [_reading(), _reading(**ending)])
    try:
        loop = await harness.arm(_RECIPES[recipe]())
        await harness.tick(loop)
        assert await harness.tick(loop) is True, "the ending is settled, not delivered"
        assert harness.delivered == [loop.id]
        assert loop.active is False
        assert loop.monitor is not None and loop.monitor.outcome is outcome
        assert harness.expired == [loop.id], "the dashboard notice still goes out"
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_judge_off_on_a_loop_with_nothing_to_read_fires_every_cycle(tmp_path, monkeypatch):
    """The other half of ``monitor_update``'s ``judge: false`` sentence."""
    harness = _Harness(tmp_path, monkeypatch, [])
    loop = NudgeLoop(
        id="heartbeat",
        slot_key=_BINDING,
        message="refresh the heartbeat file",
        idle_secs=300,
        gate=True,
        judge={validation.JUDGE_OFF_KEY: True},
    )
    harness.service._loops[loop.id] = loop
    try:
        for _ in range(3):
            assert await harness.tick(loop) is False
        assert harness.delivered == [loop.id] * 3
        assert harness.judge_consulted == []
    finally:
        harness.stop()


def test_monitor_update_describes_judge_off_as_the_gate_above_shows() -> None:
    descriptor = next(item for item in control.schemas() if item["name"] == "monitor_update")
    text = " ".join(descriptor["inputSchema"]["properties"]["judge"]["description"].split())
    assert "pull-request loop then costs no turn while its reading is unchanged" in text
    assert "a loop with nothing to read fires every cycle" in text
    assert "plain timer again" not in text
