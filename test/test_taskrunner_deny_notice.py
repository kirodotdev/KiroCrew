"""Every HOST deny on the task runner steers the in-band notice before it rejects.

A rejected permission reaches the model as kiro-cli's fixed "User denied tool
execution". The dashboard chat runner, ``llm_helpers`` and the messaging surfaces
steer the real reason into the running turn first; the task runner
(``task_executor`` for the step turns, ``task_planner`` for the decomposition
turn) is the surface behind autonomous projects and cron-launched runs, which
have no dashboard slot, and it must do the same.

Two halves, mirroring ``test_llm_helpers_deny_notice.py`` and
``test_messaging_deny_notice.py``:

* a SOURCE-LEVEL guard that enumerates every ``reject_tool(`` site and every
  call of the task runner's reject funnel, gives each a per-site verdict, and
  fails when a host deny is not steered, when a user rejection or a teardown IS
  steered, or when the SEL row is not written before the steer;
* BEHAVIOURAL tests, one per deny reason, that drive the real ``execute_task``
  / ``decompose`` with a provider double recording steer/reject ORDER. Order is
  the mechanism: the steer must be written while the permission request is
  still unanswered, because that is what proves the turn is in flight and gets
  the notice queued instead of dropped.
"""

from __future__ import annotations

import asyncio
import inspect
import pathlib
import re
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import llm_helpers, task_executor, task_planner
from kiro_crew.agent_sdk.spec_hooks import TurnSpecHooks
from kiro_crew.context import ContextBuilder
from kiro_crew.hooks import TOOL_ALLOW, TOOL_DENY, HookManager, ToolHookResult
from kiro_crew.providers.base import LLMEvent
from kiro_crew.task_models import Project, Task

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

    def __init__(self, *, supports_steer: bool = True, pct: list[float] | None = None) -> None:
        # The notice probes the NARROWER capability: a harness can take a
        # mid-turn steer and still drop one sent while a refusal is answered.
        self.supports_refusal_steer = supports_steer
        self.calls: list[str] = []
        self.steered: list[str] = []
        self._pct = list(pct or [])
        self.compact = AsyncMock()
        self.wait_for_compaction = AsyncMock(return_value={"type": "completed"})

    async def stream(self, message: str):
        yield LLMEvent(
            kind="permission_request",
            title="Run: rm -rf build",
            request_id="req-1",
            tool_kind="tool",
        )
        yield LLMEvent(kind="text_chunk", text="done")
        yield LLMEvent(kind="complete")

    def context_usage_pct(self) -> float:
        return self._pct.pop(0) if self._pct else 0.0

    async def steer(self, message: str) -> bool:
        self.calls.append("steer")
        self.steered.append(message)
        return True

    async def approve_tool(self, request_id) -> None:
        self.calls.append("approve")

    async def reject_tool(self, request_id) -> None:
        self.calls.append("reject")


class _PlannerProvider(_Provider):
    async def stream(self, message: str):
        yield LLMEvent(kind="permission_request", title="execute_bash", request_id="r1")
        yield LLMEvent(kind="text_chunk", text='{"steps": []}')
        yield LLMEvent(kind="complete")


def _sessions(provider) -> MagicMock:
    s = MagicMock()
    s.get_or_create = AsyncMock(return_value=(provider, True, False))

    async def _open_task_session(
        _pk, session_key, *, agent=None, cwd=None, approval_policy="", start_priority=None
    ):
        return await s.get_or_create(session_key, agent=agent, cwd=cwd)

    s.open_task_session = _open_task_session
    s.release_subagent_runtime = AsyncMock()
    s.release = MagicMock()
    s.reset = AsyncMock()
    s.record_success = MagicMock()
    return s


def _ctx(result: ToolHookResult) -> ContextBuilder:
    hooks = MagicMock(spec=HookManager)
    hooks.on_tool_call = MagicMock(return_value=result)
    ctx = MagicMock(spec=ContextBuilder)
    ctx.hooks = hooks
    ctx.build_message = MagicMock(return_value=("prompt", None))
    return ctx


def _planner_ctx(result: ToolHookResult) -> MagicMock:
    ctx = MagicMock()
    ctx.conversation_log.get_metadata_status.return_value = ({}, True)
    ctx.memory_mode_for_session = AsyncMock(return_value="persistent")
    ctx.hooks.on_tool_call = MagicMock(return_value=result)
    ctx.build_message = MagicMock(return_value=("prompt", None))
    return ctx


async def _run_step(provider, ctx, *, on_tool_approval=None, tmp_path: Path) -> bool:
    run = Project(spec_path="t.md", spec_content="s", status="running", task_id="tid")
    task = Task(index=1, title="T", description="d")
    run.tasks = [task]
    with patch.object(task_executor.KiroCrewConfig, "load") as cfg:
        cfg.return_value.agent.provider = "acp"
        return await task_executor.execute_task(
            run=run,
            task=task,
            sessions=_sessions(provider),
            ctx=ctx,
            agent="",
            on_tool_approval=on_tool_approval,
            auto_test=False,
            test_cmd=None,
            work_dir=tmp_path,
            on_notify=AsyncMock(),
            session_key="k",
        )


def _assert_steered_then_rejected(provider: _Provider, *fragments: str) -> str:
    assert provider.calls == ["steer", "reject"], provider.calls
    (notice,) = provider.steered
    assert notice.startswith(_TAG)
    assert _GENERIC in notice, "the notice must name the string it is correcting"
    assert "NOT a user action" in notice
    for fragment in fragments:
        assert fragment in notice, (fragment, notice)
    return notice


# ── Behavioural: task_executor, one per host-deny reason ────────────────────


@pytest.mark.asyncio
async def test_hook_deny_steers_the_hooks_reason_before_rejecting(tmp_path):
    provider = _Provider()
    ctx = _ctx(ToolHookResult(action=TOOL_DENY, reason="Blocked by security policy: rm -rf"))
    await _run_step(provider, ctx, on_tool_approval=AsyncMock(return_value=True), tmp_path=tmp_path)
    notice = _assert_steered_then_rejected(
        provider, "safety policy", "Blocked by security policy: rm -rf", "Run: rm -rf build"
    )
    assert _SURFACE_CLAUSE not in notice


@pytest.mark.asyncio
async def test_spec_hook_block_steers_the_gates_reason_before_rejecting(tmp_path):
    provider = _Provider()
    ctx = _ctx(ToolHookResult(action=TOOL_ALLOW))
    gated = TurnSpecHooks([], None, False, True)
    with (
        patch.object(task_executor, "turn_spec_hooks", AsyncMock(return_value=gated)),
        patch.object(
            task_executor,
            "permission_pre_tool_block",
            AsyncMock(return_value="guard.sh: hook denied"),
        ),
    ):
        await _run_step(
            provider, ctx, on_tool_approval=AsyncMock(return_value=True), tmp_path=tmp_path
        )
    _assert_steered_then_rejected(provider, "safety policy", "guard.sh: hook denied")


@pytest.mark.asyncio
async def test_unreadable_spec_hooks_block_steers_before_rejecting(tmp_path):
    # A gate with no verdict blocks; the model is told why, not "user denied".
    provider = _Provider()
    ctx = _ctx(ToolHookResult(action=TOOL_ALLOW))
    unreadable = TurnSpecHooks([], None, True, True)
    with patch.object(task_executor, "turn_spec_hooks", AsyncMock(return_value=unreadable)):
        await _run_step(
            provider, ctx, on_tool_approval=AsyncMock(return_value=True), tmp_path=tmp_path
        )
    _assert_steered_then_rejected(provider, "the agent spec's hooks could not be read")


@pytest.mark.asyncio
async def test_headless_deny_by_default_steers_the_surface_cause(tmp_path):
    provider = _Provider()
    await _run_step(provider, _ctx(ToolHookResult(action=TOOL_ALLOW)), tmp_path=tmp_path)
    notice = _assert_steered_then_rejected(
        provider, _SURFACE_CLAUSE, "unattended", "hooks.auto_approve_tools"
    )
    # The surface refused the call; nothing about it was judged, so no
    # sanctioned alternative is offered even for a credential-shaped title.
    assert _POLICY_GUIDANCE not in notice
    assert "How to do this properly" not in notice


@pytest.mark.asyncio
async def test_interactive_rejection_gets_no_notice(tmp_path):
    # The handler said no: kiro-cli's wording is the truth there.
    provider = _Provider()
    await _run_step(
        provider,
        _ctx(ToolHookResult(action=TOOL_ALLOW)),
        on_tool_approval=AsyncMock(return_value=False),
        tmp_path=tmp_path,
    )
    assert provider.calls == ["reject"], provider.calls
    assert provider.steered == []


@pytest.mark.asyncio
async def test_mid_stream_compaction_reject_gets_no_notice(tmp_path):
    # The turn is abandoned and re-run after compaction: there is no continuing
    # turn for a notice to correct. Second attempt (context back under the
    # threshold) approves normally.
    provider = _Provider(pct=[95.0, 0.0])
    ok = await _run_step(
        provider,
        _ctx(ToolHookResult(action=TOOL_ALLOW)),
        on_tool_approval=AsyncMock(return_value=True),
        tmp_path=tmp_path,
    )
    assert ok is True
    assert provider.calls == ["reject", "approve"], provider.calls
    assert provider.steered == []
    provider.compact.assert_awaited_once()


@pytest.mark.asyncio
async def test_backend_without_steer_only_rejects(tmp_path):
    provider = _Provider(supports_steer=False)
    await _run_step(provider, _ctx(ToolHookResult(action=TOOL_ALLOW)), tmp_path=tmp_path)
    assert provider.calls == ["reject"], provider.calls


@pytest.mark.asyncio
async def test_a_failing_steer_still_rejects(tmp_path):
    provider = _Provider()

    async def _boom(message: str) -> bool:
        provider.calls.append("steer")
        raise RuntimeError("pipe closed")

    provider.steer = _boom  # type: ignore[method-assign]
    await _run_step(provider, _ctx(ToolHookResult(action=TOOL_ALLOW)), tmp_path=tmp_path)
    assert provider.calls == ["steer", "reject"], provider.calls


@pytest.mark.asyncio
async def test_audit_lands_before_the_steer(tmp_path, monkeypatch):
    # The SEL row is written before any wire I/O for the decision, so a pipe
    # that stalls the steer cannot leave the decision acted on and unaudited.
    order: list[str] = []
    fake_sel = MagicMock()
    fake_sel.log_tool_invocation = MagicMock(side_effect=lambda **kw: order.append("audit"))
    monkeypatch.setattr(task_executor, "sel", lambda: fake_sel)
    provider = _Provider()

    async def _steer(message: str) -> bool:
        order.append("steer")
        return True

    async def _reject(request_id) -> None:
        order.append("reject")

    provider.steer = _steer  # type: ignore[method-assign]
    provider.reject_tool = _reject  # type: ignore[method-assign]
    ctx = _ctx(ToolHookResult(action=TOOL_DENY, reason="denied"))
    await _run_step(provider, ctx, on_tool_approval=AsyncMock(return_value=True), tmp_path=tmp_path)
    assert order == ["audit", "steer", "reject"], order
    row = fake_sel.log_tool_invocation.call_args.kwargs
    assert row["outcome"] == "denied" and row["error"] == "hook_deny"


@pytest.mark.asyncio
async def test_agent_authored_text_is_redacted_before_it_reaches_the_model(tmp_path):
    provider = _Provider()
    ctx = _ctx(
        ToolHookResult(action=TOOL_DENY, reason="denied: token AKIAIOSFODNN7EXAMPLE1234 in args")
    )
    await _run_step(provider, ctx, on_tool_approval=AsyncMock(return_value=True), tmp_path=tmp_path)
    (notice,) = provider.steered
    assert "AKIAIOSFODNN7EXAMPLE1234" not in notice


# ── Behavioural: task_planner (decomposition) ────────────────────────────────


def test_decomposition_hook_deny_steers_before_rejecting():
    provider = _PlannerProvider()
    ctx = _planner_ctx(ToolHookResult(action=TOOL_DENY, reason="planning denies shells"))
    asyncio.run(task_planner.decompose("spec", _sessions(provider), ctx=ctx, task_id="t1"))
    _assert_steered_then_rejected(
        provider, "safety policy", "planning denies shells", "execute_bash"
    )


def test_decomposition_without_a_hook_store_steers_the_surface_cause():
    provider = _PlannerProvider()
    asyncio.run(task_planner.decompose("spec", _sessions(provider), ctx=None, task_id="t1"))
    notice = _assert_steered_then_rejected(
        provider, _SURFACE_CLAUSE, "planning phase runs no tools"
    )
    assert _POLICY_GUIDANCE not in notice


def test_decomposition_backend_without_steer_only_rejects():
    provider = _PlannerProvider(supports_steer=False)
    asyncio.run(task_planner.decompose("spec", _sessions(provider), ctx=None, task_id="t1"))
    assert provider.calls == ["reject"], provider.calls


# ── Source-level guards: one enumeration per module ──────────────────────────


@dataclass(frozen=True)
class _Site:
    """One deny site, identified by a fingerprint near it."""

    fingerprint: str
    verdict: str  # "host" (steered) | "user" (bare) | "teardown" (bare)


#: ``task_executor`` denies through ONE funnel (``_reject_and_log``), whose
#: required ``cause=`` keyword carries the verdict: a DENY_CAUSE_* name steers,
#: ``None`` is the explicit "not a host deny". The sites are its call sites.
_EXECUTOR_SITES = (
    _Site('metadata={"reason": "spec_hook_deny"}', "host"),
    _Site('error="hook_deny"', "host"),
    _Site('metadata={"reason": "context_overflow"', "teardown"),
    _Site("interactive_rejected", "user"),
    _Site('metadata={"reason": "headless_no_authorization"}', "host"),
)
#: ``task_planner`` denies inline: audit, steer, reject at each site.
_PLANNER_SITES = (
    _Site('error="hook_deny"', "host"),
    _Site('error="no_hook_store"', "host"),
)

_REJECT = re.compile(r"^\s*await \w+\.reject_tool\(")
_FUNNEL = re.compile(r"^\s*await _reject_and_log\(")
_STEER = re.compile(r"^\s*await _steer_host_deny\(")
_AUDIT = re.compile(r"^\s*sel\(\)\.log_tool_invocation\(")
#: Lines a call's arguments may span (black wraps the metadata dict).
_CALL_SPAN = 14


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


class TestTaskExecutorDeniesThroughOneSteeringFunnel:
    """Coverage checkable from the source, not asserted in a PR body."""

    MODULE = "task_executor.py"

    def test_the_module_has_exactly_one_wire_reject_and_it_is_the_funnel(self):
        lines = self._lines()
        src = "\n".join(lines)
        assert src.count(".reject_tool(") == 1, "every reject must go through _reject_and_log"
        (i,) = _matches(_REJECT, lines)
        funnel = inspect.getsource(task_executor._reject_and_log)
        assert lines[i].strip() in funnel

    def test_the_funnel_audits_then_steers_then_rejects(self):
        body = inspect.getsource(task_executor._reject_and_log)
        audit = body.index("history.log_tool_invocation(")
        steer = body.index("await _steer_host_deny(")
        reject = body.index("await client.reject_tool(")
        assert audit < steer < reject, "audit first, then the notice, then the wire"
        assert "if cause is not None:" in body, "None is the explicit not-a-host-deny verdict"

    def test_the_cause_is_a_required_keyword_with_no_default(self):
        sig = inspect.signature(task_executor._reject_and_log)
        cause = sig.parameters["cause"]
        assert cause.kind is inspect.Parameter.KEYWORD_ONLY
        assert cause.default is inspect.Parameter.empty

    def test_the_scan_finds_every_funnel_call_the_source_contains(self):
        lines = self._lines()
        src = "\n".join(lines)
        # The def line is the one non-call spelling.
        textual = src.count("_reject_and_log(") - 1
        found = len(_matches(_FUNNEL, lines))
        assert found == textual == len(_EXECUTOR_SITES), (found, textual, len(_EXECUTOR_SITES))

    def test_every_call_carries_exactly_one_verdict(self):
        lines = self._lines()
        seen = sorted(site.fingerprint for _i, site in self._walk(lines))
        assert seen == sorted(s.fingerprint for s in _EXECUTOR_SITES)

    def test_every_host_deny_names_a_deny_cause_and_a_reason(self):
        lines = self._lines()
        bare: list[int] = []
        for i, site in self._walk(lines):
            if site.verdict != "host":
                continue
            block = "\n".join(lines[i : i + _CALL_SPAN])
            if "cause=DENY_CAUSE_" not in block or "reason=" not in block:
                bare.append(i + 1)
        assert not bare, (
            "these host denies hand the model kiro-cli's generic 'user denied' with "
            f"nothing to correct it -- pass cause=DENY_CAUSE_* and a reason: lines {bare}"
        )

    def test_user_rejections_and_teardown_are_not_steered(self):
        lines = self._lines()
        for i, site in self._walk(lines):
            if site.verdict == "host":
                continue
            block = "\n".join(lines[i : i + _CALL_SPAN])
            assert "cause=None" in block, (
                f"line {i + 1}: a genuine user rejection (or a teardown reject) must say so "
                "with cause=None, never claim it was not a user action"
            )
            assert "DENY_CAUSE_" not in block

    def test_exactly_one_user_rejection_is_allowlisted(self):
        user = [s for s in _EXECUTOR_SITES if s.verdict == "user"]
        assert len(user) == 1, (
            "the allowlist must name each USER rejection individually -- a new one "
            "needs its own per-site judgement, not a wider marker"
        )

    def test_no_stray_steer_outside_the_funnel(self):
        # One spelling of the notice in the module, inside the funnel.
        lines = self._lines()
        assert len(_matches(_STEER, lines)) == 1
        assert "await _steer_host_deny(" in inspect.getsource(task_executor._reject_and_log)

    def test_the_funnel_delegates_to_the_shared_helper(self):
        assert task_executor._steer_host_deny is llm_helpers._steer_host_deny

    def _lines(self) -> list[str]:
        return _lines(self.MODULE)

    def _walk(self, lines: list[str]) -> list[tuple[int, _Site]]:
        out: list[tuple[int, _Site]] = []
        for i in _matches(_FUNNEL, lines):
            # The fingerprint sits in the call's own arguments or the comment
            # right above it; never read back past the previous call.
            out.append((i, _site_for(_EXECUTOR_SITES, lines, max(0, i - 6), i + _CALL_SPAN)))
        return out


class TestEveryHostDenyInTaskPlannerSteersFirst:
    MODULE = "task_planner.py"
    WINDOW = 8
    AUDIT_WINDOW = 20

    def test_the_scan_finds_every_reject_the_source_contains(self):
        lines = _lines(self.MODULE)
        src = "\n".join(lines)
        textual = src.count(".reject_tool(")
        found = len(_matches(_REJECT, lines))
        assert found == textual == len(_PLANNER_SITES), (found, textual, len(_PLANNER_SITES))

    def test_every_site_carries_exactly_one_verdict(self):
        lines = _lines(self.MODULE)
        seen = sorted(site.fingerprint for _i, site in self._walk(lines))
        assert seen == sorted(s.fingerprint for s in _PLANNER_SITES)

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

    def test_the_module_uses_the_shared_helper(self):
        assert task_planner._steer_host_deny is llm_helpers._steer_host_deny

    def _walk(self, lines: list[str]) -> list[tuple[int, _Site]]:
        out: list[tuple[int, _Site]] = []
        previous = -1
        for i in _matches(_REJECT, lines):
            lo = max(0, i - self.AUDIT_WINDOW, previous + 1)
            out.append((i, _site_for(_PLANNER_SITES, lines, lo, i + 1)))
            previous = i
        return out
