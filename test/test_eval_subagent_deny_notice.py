"""Every HOST deny on the eval and subagent surfaces steers the in-band notice
before it rejects.

A rejected permission reaches the model as kiro-cli's fixed "User denied tool
execution". The dashboard chat runner, ``llm_helpers``, the messaging surfaces
and the task runner steer the real reason into the running turn first; the eval
harness (``eval/runner.py`` for the scenario turns, ``eval/judge.py`` for the
scoring turn) and the subagent surface (its permission ladder, settled by
``tool_permission.settle`` from ``subagent_manager/run.py``) have no dashboard
slot and must do the same.

* The eval modules deny inline, so a SOURCE-LEVEL guard enumerates every
  ``reject_tool(`` site there with a per-site verdict, and fails when a host deny
  is not steered or when the SEL row is not written before the steer.
* BEHAVIOURAL tests, one per deny verdict, drive the real ``EvalRunner`` /
  ``LLMJudge`` and a real subagent run (``SubagentManager._run_inner``) with a
  provider double recording steer/reject ORDER. Order is the mechanism: the steer
  must be written while the permission request is still unanswered, because that
  is what proves the turn is in flight and gets the notice queued instead of
  dropped. What the subagent's ladder guarantees in general (exactly one answer,
  audit first, a cause exactly when the host refused) is pinned once, through its
  interface, by ``test_tool_permission.py``.
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import re
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import llm_helpers, subagent
from kiro_crew.agent_sdk.spec_hooks import TurnSpecHooks
from kiro_crew.eval import judge as judge_mod
from kiro_crew.eval import runner as runner_mod
from kiro_crew.eval.judge import LLMJudge
from kiro_crew.eval.runner import EvalRunner
from kiro_crew.eval.scenario import Turn
from kiro_crew.execution_context import execution_for_store
from kiro_crew.hooks import TOOL_ALLOW, TOOL_DENY, ToolHookResult
from kiro_crew.metrics.events import CHILD_PERMISSION_DENIED
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    LLMEvent,
)
from kiro_crew.subagent import SubagentInfo, SubagentManager

_GENERIC = "User denied tool execution"
_TAG = "[Kiro Crew host notice]"
#: Only the POLICY cause appends class-specific remediation; its guidance line
#: is the fingerprint that must be absent from every surface-policy notice.
_POLICY_GUIDANCE = "allowed alternative"
_SURFACE_CLAUSE = "tool policy of the surface"
_SRC = pathlib.Path(__file__).resolve().parents[1] / "src/kiro_crew"


# ── Doubles ──────────────────────────────────────────────────────────────────


class _Provider:
    """Permission-answering double recording steer/approve/reject ORDER."""

    def __init__(
        self,
        *,
        title: str = "execute_bash",
        tool_input: str = "",
        request_id: str = "r1",
        supports_steer: bool = True,
    ) -> None:
        # The notice probes the NARROWER capability: a harness can take a
        # mid-turn steer and still drop one sent while a refusal is answered.
        self.supports_refusal_steer = supports_steer
        self.calls: list[str] = []
        self.steered: list[str] = []
        self._title = title
        self._tool_input = tool_input
        self._request_id = request_id

    @property
    def cwd(self) -> str:
        return ""

    async def start(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def stream(self, message: str):
        yield LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title=self._title,
            tool_input=self._tool_input,
            request_id=self._request_id,
        )
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text='{"score": 4, "reason": "ok"}')
        yield LLMEvent(kind=EVENT_COMPLETE)

    def context_usage_pct(self) -> float:
        return 0.0

    async def steer(self, message: str) -> bool:
        self.calls.append("steer")
        self.steered.append(message)
        return True

    async def approve_tool(self, request_id) -> None:
        self.calls.append("approve")

    async def reject_tool(self, request_id) -> None:
        self.calls.append("reject")


def _assert_steered_then_rejected(provider: _Provider, *fragments: str) -> str:
    assert provider.calls == ["steer", "reject"], provider.calls
    (notice,) = provider.steered
    assert notice.startswith(_TAG)
    assert _GENERIC in notice, "the notice must name the string it is correcting"
    assert "NOT a user action" in notice
    for fragment in fragments:
        assert fragment in notice, (fragment, notice)
    return notice


async def _run_eval_turn(provider: _Provider, monkeypatch, *, gate_reason: str | None) -> None:
    # The permission gate reads the live config; pin its verdict so each test
    # drives exactly one deny site.
    monkeypatch.setattr(runner_mod, "refusal_for", lambda event, **kw: gate_reason)
    runner = EvalRunner(provider_factory=lambda key, **kw: provider)
    await runner._run_turn(provider, Turn(user="go"), "eval-session")


# ── Behavioural: eval runner, one per host-deny reason ───────────────────────


@pytest.mark.asyncio
async def test_permission_gate_refusal_steers_the_gates_reason_before_rejecting(monkeypatch):
    provider = _Provider(title="Run: rm -rf build")
    await _run_eval_turn(
        provider, monkeypatch, gate_reason="Blocked by security policy: destructive rm"
    )
    notice = _assert_steered_then_rejected(
        provider, "safety policy", "Blocked by security policy: destructive rm", "Run: rm -rf build"
    )
    assert _SURFACE_CLAUSE not in notice


@pytest.mark.asyncio
async def test_sensitive_path_steers_the_policy_cause_before_rejecting(monkeypatch):
    sensitive = str(Path.home() / ".aws" / "credentials")
    provider = _Provider(title="read_file", tool_input=json.dumps({"path": sensitive}))
    await _run_eval_turn(provider, monkeypatch, gate_reason=None)
    notice = _assert_steered_then_rejected(
        provider, "safety policy", "sensitive credential path", "read_file"
    )
    # The policy cause keys class remediation off the reason: the secret-file
    # class names the sanctioned path instead of leaving the model to guess.
    assert "How to do this properly" in notice


@pytest.mark.asyncio
async def test_unreadable_path_steers_the_policy_cause_before_rejecting(monkeypatch):
    provider = _Provider(title="read_file", tool_input="some-opaque-input-no-path")
    await _run_eval_turn(provider, monkeypatch, gate_reason=None)
    _assert_steered_then_rejected(provider, "safety policy", "no target path could be read")


@pytest.mark.asyncio
async def test_unsafe_tool_steers_the_surface_cause_before_rejecting(monkeypatch):
    provider = _Provider(title="write_file", tool_input='{"path": "x"}')
    await _run_eval_turn(provider, monkeypatch, gate_reason=None)
    notice = _assert_steered_then_rejected(
        provider, _SURFACE_CLAUSE, "eval harness runs tools read-only", "write_file"
    )
    # The surface refused the call; nothing about it was judged, so no
    # sanctioned alternative is offered.
    assert _POLICY_GUIDANCE not in notice
    assert "How to do this properly" not in notice


@pytest.mark.asyncio
async def test_eval_backend_without_steer_only_rejects(monkeypatch):
    provider = _Provider(title="write_file", supports_steer=False)
    await _run_eval_turn(provider, monkeypatch, gate_reason=None)
    assert provider.calls == ["reject"], provider.calls


@pytest.mark.asyncio
async def test_eval_failing_steer_still_rejects(monkeypatch):
    provider = _Provider(title="write_file")

    async def _boom(message: str) -> bool:
        provider.calls.append("steer")
        raise RuntimeError("pipe closed")

    provider.steer = _boom  # type: ignore[method-assign]
    await _run_eval_turn(provider, monkeypatch, gate_reason=None)
    assert provider.calls == ["steer", "reject"], provider.calls


@pytest.mark.asyncio
async def test_eval_audit_lands_before_the_steer(monkeypatch):
    # The SEL row is written before any wire I/O for the decision, so a pipe
    # that stalls the steer cannot leave the decision acted on and unaudited.
    order: list[str] = []
    fake_sel = MagicMock()
    fake_sel.log_tool_invocation = MagicMock(side_effect=lambda **kw: order.append("audit"))
    monkeypatch.setattr(runner_mod, "sel", lambda: fake_sel)
    provider = _Provider(title="write_file")

    async def _steer(message: str) -> bool:
        order.append("steer")
        return True

    async def _reject(request_id) -> None:
        order.append("reject")

    provider.steer = _steer  # type: ignore[method-assign]
    provider.reject_tool = _reject  # type: ignore[method-assign]
    await _run_eval_turn(provider, monkeypatch, gate_reason=None)
    assert order == ["audit", "steer", "reject"], order
    assert fake_sel.log_tool_invocation.call_args.kwargs["outcome"] == "rejected"


@pytest.mark.asyncio
async def test_eval_gate_reason_is_redacted_before_it_reaches_the_model(monkeypatch):
    provider = _Provider(title="Run: aws s3 ls")
    await _run_eval_turn(
        provider, monkeypatch, gate_reason="denied: token AKIAIOSFODNN7EXAMPLE1234 in args"
    )
    (notice,) = provider.steered
    assert "AKIAIOSFODNN7EXAMPLE1234" not in notice


# ── Behavioural: eval judge ──────────────────────────────────────────────────


async def _judge_turn(provider: _Provider) -> None:
    judge = LLMJudge(provider_factory=lambda key, **kw: provider)
    await judge.start()
    await judge.judge_turn("desc", "criteria", "user", "assistant")


@pytest.mark.asyncio
async def test_judge_refusal_steers_the_surface_cause_before_rejecting():
    provider = _Provider(title="execute_bash")
    await _judge_turn(provider)
    notice = _assert_steered_then_rejected(
        provider, _SURFACE_CLAUSE, "eval judge runs no tools", "execute_bash"
    )
    assert _POLICY_GUIDANCE not in notice


@pytest.mark.asyncio
async def test_judge_backend_without_steer_only_rejects():
    provider = _Provider(supports_steer=False)
    await _judge_turn(provider)
    assert provider.calls == ["reject"], provider.calls


@pytest.mark.asyncio
async def test_judge_request_without_an_id_neither_steers_nor_rejects():
    # Nothing can be answered on the wire, so there is no in-flight refusal for
    # a notice to correct either.
    provider = _Provider(request_id="")
    await _judge_turn(provider)
    assert provider.calls == [], provider.calls


@pytest.mark.asyncio
async def test_judge_audit_lands_before_the_steer(monkeypatch):
    order: list[str] = []
    fake_sel = MagicMock()
    fake_sel.log_tool_invocation = MagicMock(side_effect=lambda **kw: order.append("audit"))
    monkeypatch.setattr(judge_mod, "sel", lambda: fake_sel)
    provider = _Provider()

    async def _steer(message: str) -> bool:
        order.append("steer")
        return True

    async def _reject(request_id) -> None:
        order.append("reject")

    provider.steer = _steer  # type: ignore[method-assign]
    provider.reject_tool = _reject  # type: ignore[method-assign]
    await _judge_turn(provider)
    assert order == ["audit", "steer", "reject"], order


# ── Behavioural: a real subagent run, one per verdict ───────────────────────

pytestmark = pytest.mark.usefixtures("close_subagent_managers")


class _SubagentProvider(_Provider):
    """The same recording double, streaming *events* into a real subagent run."""

    def __init__(self, events: list, **kw) -> None:
        super().__init__(**kw)
        self._events = events
        self.last_prompt_stats = None

    async def stream(self, message: str):
        for event in self._events:
            yield event
        yield LLMEvent(kind=EVENT_COMPLETE)

    def __getattr__(self, name: str):
        # Everything else a run asks of its provider (session ids, MCP reports).
        if name.startswith("__"):
            raise AttributeError(name)
        return MagicMock(name=name)


def _request(**kw) -> LLMEvent:
    fields: dict = {
        "kind": EVENT_PERMISSION_REQUEST,
        "title": "Run: rm -rf build",
        "request_id": "r1",
        "tool_name": "execute_bash",
        "is_shell": True,
        "shell_classified": True,
        "raw_params_trusted": True,
        "raw_tool_params": {"command": "rm -rf build"},
    }
    fields.update(kw)
    return LLMEvent(**fields)  # type: ignore[arg-type]


async def _run_subagent(
    provider: _SubagentProvider,
    *,
    hook: ToolHookResult = ToolHookResult(action=TOOL_ALLOW),
    spec: TurnSpecHooks = TurnSpecHooks([], None, False, False),
    pre_tool=None,
    turn_limit: int = 5,
    sel=None,
    **manager_kw,
) -> SubagentInfo:
    sessions = MagicMock()
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.get_approval_policy = MagicMock(return_value="ask")
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.release_subagent_runtime = AsyncMock()
    sessions._sessions = {}
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("msg", None))
    ctx.hooks.auto_approve_subagent_tools = False
    ctx.hooks.on_tool_call = MagicMock(return_value=hook)
    manager = SubagentManager(
        sessions=sessions, ctx_builder=ctx, default_turn_limit=turn_limit, **manager_kw
    )
    info = SubagentInfo(
        execution_context=execution_for_store(""),
        id="a1",
        task="t",
        parent_session_key="dashboard:chat-1",
    )
    manager._agents[info.id] = info
    manager._log_spawned(info)
    with (
        patch.object(subagent, "Stats"),
        patch.object(subagent, "sel", sel or MagicMock()),
        patch.object(subagent, "update_state"),
        patch.object(subagent, "create_agent_folder", MagicMock()),
        patch.object(subagent, "turn_spec_hooks", AsyncMock(return_value=spec)),
        patch("kiro_crew.subagent_manager.run.permission_pre_tool_block", pre_tool or AsyncMock()),
        patch.object(manager, "_write_tombstone"),
    ):
        await asyncio.wait_for(manager._run_inner(info, "subagent:a1"), 30)
    return info


@pytest.mark.asyncio
async def test_spec_hook_block_steers_the_gates_reason_before_rejecting():
    provider = _SubagentProvider([_request()])
    await _run_subagent(
        provider,
        spec=TurnSpecHooks([], None, False, True),
        pre_tool=AsyncMock(return_value="guard.sh: hook denied"),
    )
    _assert_steered_then_rejected(provider, "safety policy", "guard.sh: hook denied")


@pytest.mark.asyncio
async def test_hook_deny_steers_the_hooks_reason_before_rejecting():
    provider = _SubagentProvider([_request()])
    await _run_subagent(
        provider,
        hook=ToolHookResult(action=TOOL_DENY, reason="Blocked by security policy: rm -rf"),
    )
    notice = _assert_steered_then_rejected(
        provider, "safety policy", "Blocked by security policy: rm -rf", "Run: rm -rf build"
    )
    assert _SURFACE_CLAUSE not in notice


@pytest.mark.asyncio
async def test_headless_deny_by_default_steers_the_surface_cause():
    provider = _SubagentProvider([_request()])
    await _run_subagent(provider)
    notice = _assert_steered_then_rejected(
        provider,
        _SURFACE_CLAUSE,
        "unattended",
        "parent_policy=auto",
        "hooks.auto_approve_tools",
        # Every positive-authorization tier the surface honours, so the notice
        # never understates what the run may still call.
        "classifies as read-only",
    )
    assert _POLICY_GUIDANCE not in notice
    assert "How to do this properly" not in notice


@pytest.mark.asyncio
async def test_a_low_fidelity_child_nobody_can_ask_about_steers_the_surface_cause():
    provider = _SubagentProvider(
        [_request(sub_session_id="child-1", shell_classified=False, raw_params_trusted=False)]
    )
    await _run_subagent(provider)
    notice = _assert_steered_then_rejected(
        provider, _SURFACE_CLAUSE, "no verifiable security context", "agent-authored title"
    )
    assert _POLICY_GUIDANCE not in notice


@pytest.mark.asyncio
async def test_an_approvers_no_gets_no_notice():
    # A person said no: kiro-cli's wording is the truth there.
    provider = _SubagentProvider([_request()])
    await _run_subagent(provider, on_tool_approval=AsyncMock(return_value=False))
    assert provider.calls == ["reject"], provider.calls
    assert provider.steered == []


@pytest.mark.asyncio
async def test_a_turn_limit_bail_gets_no_notice():
    # The run ends here: there is no continuing turn for a notice to correct.
    provider = _SubagentProvider([_request(request_id="r1"), _request(request_id="r2")])
    info = await _run_subagent(
        provider, turn_limit=1, on_tool_approval=AsyncMock(return_value=True)
    )
    assert provider.calls == ["approve", "reject"], provider.calls
    assert provider.steered == [] and info.error == "turn_limit:1"


@pytest.mark.asyncio
async def test_a_backend_without_steer_only_rejects():
    provider = _SubagentProvider([_request()], supports_steer=False)
    await _run_subagent(provider)
    assert provider.calls == ["reject"], provider.calls


@pytest.mark.asyncio
async def test_a_failing_steer_still_rejects():
    provider = _SubagentProvider([_request()])

    async def _boom(message: str) -> bool:
        provider.calls.append("steer")
        raise RuntimeError("pipe closed")

    provider.steer = _boom  # type: ignore[method-assign]
    await _run_subagent(provider)
    assert provider.calls == ["steer", "reject"], provider.calls


def _ordered_sel(order: list[str], *, fail: bool = False) -> MagicMock:
    fake_sel = MagicMock()

    def _row(**row) -> None:
        if row.get("outcome") in ("denied", "rejected"):
            order.append("audit")
            if fail:
                raise RuntimeError("SEL trust root unloadable")

    fake_sel.return_value.log_tool_invocation = MagicMock(side_effect=_row)
    return fake_sel


@pytest.mark.asyncio
async def test_the_audit_lands_before_the_steer():
    order: list[str] = []
    provider = _SubagentProvider([_request()])

    async def _steer(message: str) -> bool:
        order.append("steer")
        return True

    async def _reject(request_id) -> None:
        order.append("reject")

    provider.steer = _steer  # type: ignore[method-assign]
    provider.reject_tool = _reject  # type: ignore[method-assign]
    fake_sel = _ordered_sel(order)
    await _run_subagent(
        provider, hook=ToolHookResult(action=TOOL_DENY, reason="denied"), sel=fake_sel
    )
    assert order == ["audit", "steer", "reject"], order
    row = fake_sel.return_value.log_tool_invocation.call_args.kwargs
    assert row["outcome"] == "denied" and row["error"] == "hook_deny"


@pytest.mark.asyncio
async def test_a_failing_audit_still_steers_and_rejects():
    # A SEL audit that raises (an unloadable trust root is permanent per
    # process) must NOT skip the steer and the reject on this surface: the wire
    # request would stay unanswered and hang the turn.
    order: list[str] = []
    provider = _SubagentProvider([_request()])

    async def _steer(message: str) -> bool:
        order.append("steer")
        return True

    async def _reject(request_id) -> None:
        order.append("reject")

    provider.steer = _steer  # type: ignore[method-assign]
    provider.reject_tool = _reject  # type: ignore[method-assign]
    await _run_subagent(
        provider,
        hook=ToolHookResult(action=TOOL_DENY, reason="denied"),
        sel=_ordered_sel(order, fail=True),
    )
    assert order == ["audit", "steer", "reject"], order


@pytest.mark.asyncio
async def test_a_childs_denial_is_counted_before_the_wire(monkeypatch):
    # The hang-resilience counter for a backend child's denial is emitted with
    # the audit, ahead of the steer and the reject.
    order: list[str] = []
    provider = _SubagentProvider(
        [_request(sub_session_id="child-1", shell_classified=False, raw_params_trusted=False)]
    )

    async def _reject(request_id) -> None:
        order.append("reject")

    provider.reject_tool = _reject  # type: ignore[method-assign]
    points: list = []

    def _emit(name, attrs) -> None:
        if name == CHILD_PERMISSION_DENIED:
            order.append("metric")
            points.append(attrs)

    monkeypatch.setattr("kiro_crew.metrics.events.emit_counter", _emit)
    await _run_subagent(provider)
    assert order == ["metric", "reject"], order
    assert points == [{"surface": "subagent", "reason": "child_origin_no_command_context"}]


def test_the_headless_reason_names_every_authorizing_tier():
    # parent_policy=auto, the hook's name grant and its read-only
    # classification can each still authorize a call on this surface; a
    # notice naming fewer would understate what the run may call.
    reason = subagent._HEADLESS_DENY_REASON
    for tier in ("parent_policy=auto", "hooks.auto_approve_tools", "read-only"):
        assert tier in reason, tier


# ── Source-level guards: one enumeration per module ──────────────────────────


@dataclass(frozen=True)
class _Site:
    """One deny site, identified by a fingerprint near it."""

    fingerprint: str
    verdict: str  # "host" (steered) | "user" (bare) | "teardown" (bare)


#: ``eval/runner.py`` denies inline: audit, steer, reject at each site.
_RUNNER_SITES = (
    _Site('outcome="rejected_hook_deny"', "host"),
    _Site('"rejected_sensitive" if target else "rejected_no_path"', "host"),
    _Site('outcome="rejected",', "host"),
)
#: ``eval/judge.py`` denies inline at its one site.
_JUDGE_SITES = (_Site('source="eval_judge"', "host"),)
_REJECT = re.compile(r"^\s*await [\w.]+\.reject_tool\(")
_STEER = re.compile(r"^\s*await _steer_host_deny\(")
_AUDIT = re.compile(r"^\s*sel\(\)\.log_tool_invocation\(")


def _lines(module: str) -> list[str]:
    return (_SRC / module).read_text(encoding="utf-8").splitlines()


def _matches(rx: re.Pattern[str], lines: list[str]) -> list[int]:
    return [i for i, line in enumerate(lines) if rx.match(line)]


def _site_for(sites: tuple[_Site, ...], lines: list[str], lo: int, hi: int) -> _Site:
    span = "\n".join(lines[lo:hi])
    hits = [s for s in sites if s.fingerprint in span]
    assert len(hits) == 1, (
        f"lines {lo + 1}-{hi}: a deny site must match exactly one enumerated fingerprint "
        f"(matched {[s.fingerprint for s in hits]}); a new site needs its own per-site "
        "verdict here, not a wider marker"
    )
    return hits[0]


class _InlineSurface:
    """A module that denies inline: audit, then the shared steer, then reject."""

    MODULE = ""
    SITES: tuple[_Site, ...] = ()
    WINDOW = 12
    AUDIT_WINDOW = 24

    def test_the_scan_finds_every_reject_the_source_contains(self):
        lines = _lines(self.MODULE)
        src = "\n".join(lines)
        textual = src.count(".reject_tool(")
        found = len(_matches(_REJECT, lines))
        assert found == textual == len(self.SITES), (found, textual, len(self.SITES))

    def test_every_site_carries_exactly_one_verdict(self):
        lines = _lines(self.MODULE)
        seen = sorted(site.fingerprint for _i, site in self._walk(lines))
        assert seen == sorted(s.fingerprint for s in self.SITES)

    def test_every_host_deny_is_preceded_by_the_shared_steer(self):
        lines = _lines(self.MODULE)
        bare: list[int] = []
        for i, site in self._walk(lines):
            assert site.verdict == "host"
            steers = [j for j in range(max(0, i - self.WINDOW), i) if _STEER.match(lines[j])]
            if not steers:
                bare.append(i + 1)
        assert not bare, (
            "these host denies hand the model kiro-cli's generic 'user denied' with "
            f"nothing to correct it -- await _steer_host_deny(...) first: lines {bare}"
        )

    def test_every_reject_audits_before_the_steer(self):
        lines = _lines(self.MODULE)
        late: list[int] = []
        previous = -1
        for i, _site in self._walk(lines):
            floor = max(0, i - self.AUDIT_WINDOW, previous + 1)
            steers = [j for j in range(max(0, i - self.WINDOW), i) if _STEER.match(lines[j])]
            audits = [j for j in range(floor, i) if _AUDIT.match(lines[j])]
            if not audits or not steers or not audits[-1] < steers[-1]:
                late.append(i + 1)
            previous = i
        assert not late, (
            "the SEL row must be written before the steer and the reject, or a "
            f"stalled pipe cancels the coroutine with the decision unaudited: lines {late}"
        )

    def test_every_steer_names_its_cause_explicitly(self):
        lines = _lines(self.MODULE)
        unnamed = [
            i + 1
            for i in _matches(_STEER, lines)
            if "cause=DENY_CAUSE_" not in "\n".join(lines[i : i + self.WINDOW])
        ]
        assert not unnamed, unnamed

    def _walk(self, lines: list[str]) -> list[tuple[int, _Site]]:
        out: list[tuple[int, _Site]] = []
        previous = -1
        for i in _matches(_REJECT, lines):
            lo = max(0, i - self.AUDIT_WINDOW, previous + 1)
            out.append((i, _site_for(self.SITES, lines, lo, i + 1)))
            previous = i
        return out


class TestEveryHostDenyInEvalRunnerSteersFirst(_InlineSurface):
    MODULE = "eval/runner.py"
    SITES = _RUNNER_SITES

    def test_the_module_uses_the_shared_helper(self):
        assert runner_mod._steer_host_deny is llm_helpers._steer_host_deny

    def test_the_surface_reason_names_what_the_harness_permits(self):
        # The surface-policy notice tells the model to read the reason for
        # what this surface permits, so the reason has to say it.
        assert "read-only" in runner_mod._EVAL_UNSAFE_TOOL_REASON
        assert "refused" in runner_mod._EVAL_UNSAFE_TOOL_REASON


class TestEveryHostDenyInEvalJudgeSteersFirst(_InlineSurface):
    MODULE = "eval/judge.py"
    SITES = _JUDGE_SITES

    def test_the_module_uses_the_shared_helper(self):
        assert judge_mod._steer_host_deny is llm_helpers._steer_host_deny

    def test_the_surface_reason_names_what_the_judge_permits(self):
        assert "runs no tools" in judge_mod._JUDGE_DENY_REASON


# ── The subagent surface answers only through its ladder ─────────────────────
#
# A source pin kept on purpose: no behaviour test can see a FUTURE answer path
# that bypasses ``tool_permission.settle`` (no audit row, no notice). The ladder's
# one answering adapter is pinned in ``test_tool_permission.py``.

_ANSWER_CALLS = frozenset({"approve_tool", "reject_tool", "_steer_host_deny"})
# The ``tool_permission.Wire`` port's own answers, called as methods (``wire.allow(ask)``).
_WIRE_ANSWERS = frozenset({"allow", "refuse"})


def _answer_calls_in(source: str) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and (
            getattr(node.func, "attr", None) in _ANSWER_CALLS | _WIRE_ANSWERS
            or getattr(node.func, "id", None) in _ANSWER_CALLS
        )
    ]


def _answer_calls(module: str) -> list[int]:
    return _answer_calls_in((_SRC / module).read_text(encoding="utf-8"))


def _ladder_entries(module: str) -> dict[str, int]:
    """How often *module* calls ``tool_permission.settle`` / ``tool_permission.bail``."""
    counts: dict[str, int] = {}
    for node in ast.walk(ast.parse((_SRC / module).read_text(encoding="utf-8"))):
        func = getattr(node, "func", None) if isinstance(node, ast.Call) else None
        if (
            isinstance(func, ast.Attribute)
            and func.attr in ("settle", "bail")
            and getattr(func.value, "id", None) == "tool_permission"
        ):
            counts[func.attr] = counts.get(func.attr, 0) + 1
    return counts


_SUBAGENT_MODULES = [
    "subagent.py",
    *sorted(
        path.relative_to(_SRC).as_posix() for path in (_SRC / "subagent_manager").rglob("*.py")
    ),
]


def test_the_scanned_modules_hold_the_subagent_ladder():
    # The scan below means something only while the request arm lives in the
    # files it scans: the run's one settle and its two limit bails.
    assert "subagent_manager/run.py" in _SUBAGENT_MODULES
    assert _ladder_entries("subagent_manager/run.py") == {"settle": 1, "bail": 2}


@pytest.mark.parametrize("module", _SUBAGENT_MODULES)
def test_the_subagent_surface_never_answers_the_wire_itself(module):
    assert _answer_calls(module) == [], (
        f"{module} answers a permission request outside tool_permission.settle/bail, "
        "which skips the audit row and the deny notice"
    )


def test_the_answer_scan_sees_an_inline_surface():
    # Non-vacuity: the eval runner answers inline, so the same scan finds it.
    assert len(_answer_calls("eval/runner.py")) >= len(_RUNNER_SITES)


def test_the_answer_scan_flags_the_wire_ports_own_answers():
    # Non-vacuity for the port's answers, which no inline surface calls.
    source = (
        "async def f(wire, ask):\n    await wire.allow(ask)\n    await wire.refuse(ask, None)\n"
    )
    assert _answer_calls_in(source) == [2, 3]
