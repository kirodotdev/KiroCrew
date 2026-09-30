"""Every HOST deny on the eval and subagent surfaces steers the in-band notice
before it rejects.

A rejected permission reaches the model as kiro-cli's fixed "User denied tool
execution". The dashboard chat runner, ``llm_helpers``, the messaging surfaces
and the task runner steer the real reason into the running turn first; the eval
harness (``eval/runner.py`` for the scenario turns, ``eval/judge.py`` for the
scoring turn) and the subagent surface (the ``_reject_and_log`` funnel in
``subagent.py``, called from ``subagent_manager/run.py``) have no dashboard slot
and must do the same.

Two halves, mirroring ``test_taskrunner_deny_notice.py``:

* a SOURCE-LEVEL guard that enumerates every ``reject_tool(`` site and every
  call of the subagent funnel, gives each a per-site verdict, and fails when a
  host deny is not steered, when a user rejection or a run bail IS steered, or
  when the SEL row is not written before the steer;
* BEHAVIOURAL tests, one per deny reason, that drive the real ``EvalRunner`` /
  ``LLMJudge`` / ``SubagentManager._reject_and_log`` with a provider double
  recording steer/reject ORDER. Order is the mechanism: the steer must be
  written while the permission request is still unanswered, because that is
  what proves the turn is in flight and gets the notice queued instead of
  dropped.
"""

from __future__ import annotations

import inspect
import json
import pathlib
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import llm_helpers, subagent
from kiro_crew.constants import DENY_CAUSE_POLICY, DENY_CAUSE_SURFACE_POLICY
from kiro_crew.eval import judge as judge_mod
from kiro_crew.eval import runner as runner_mod
from kiro_crew.eval.judge import LLMJudge
from kiro_crew.eval.runner import EvalRunner
from kiro_crew.eval.scenario import Turn
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    LLMEvent,
)
from kiro_crew.subagent import SubagentManager

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


# ── Behavioural: the subagent funnel, one per verdict ────────────────────────


def _event(**kw) -> LLMEvent:
    return LLMEvent(kind=EVENT_PERMISSION_REQUEST, title="Run: rm -rf build", request_id="r1", **kw)


async def _funnel(provider: _Provider, **kw) -> None:
    with patch.object(subagent, "sel"):
        await SubagentManager._reject_and_log(provider, "r1", "subagent:a1", _event(), **kw)


@pytest.mark.asyncio
async def test_funnel_policy_cause_steers_the_reason_before_rejecting():
    provider = _Provider()
    await _funnel(
        provider,
        cause=DENY_CAUSE_POLICY,
        reason="Blocked by security policy: rm -rf",
        error="hook_deny",
    )
    notice = _assert_steered_then_rejected(
        provider, "safety policy", "Blocked by security policy: rm -rf", "Run: rm -rf build"
    )
    assert _SURFACE_CLAUSE not in notice


@pytest.mark.asyncio
async def test_funnel_headless_reason_steers_the_surface_cause():
    provider = _Provider()
    await _funnel(
        provider,
        cause=DENY_CAUSE_SURFACE_POLICY,
        reason=subagent._HEADLESS_DENY_REASON,
    )
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
async def test_funnel_low_fidelity_reason_steers_the_surface_cause():
    provider = _Provider()
    await _funnel(
        provider,
        cause=DENY_CAUSE_SURFACE_POLICY,
        reason=subagent._LOW_FIDELITY_DENY_REASON,
        error="child_origin_no_command_context",
    )
    notice = _assert_steered_then_rejected(
        provider, _SURFACE_CLAUSE, "no verifiable security context", "agent-authored title"
    )
    assert _POLICY_GUIDANCE not in notice


@pytest.mark.asyncio
async def test_funnel_cause_none_gets_no_notice():
    # A user rejection or a run bail: kiro-cli's wording is the truth there.
    provider = _Provider()
    await _funnel(provider, cause=None, error="child_interactive_rejected")
    assert provider.calls == ["reject"], provider.calls
    assert provider.steered == []


@pytest.mark.asyncio
async def test_funnel_backend_without_steer_only_rejects():
    provider = _Provider(supports_steer=False)
    await _funnel(provider, cause=DENY_CAUSE_POLICY, reason="denied")
    assert provider.calls == ["reject"], provider.calls


@pytest.mark.asyncio
async def test_funnel_failing_steer_still_rejects():
    provider = _Provider()

    async def _boom(message: str) -> bool:
        provider.calls.append("steer")
        raise RuntimeError("pipe closed")

    provider.steer = _boom  # type: ignore[method-assign]
    await _funnel(provider, cause=DENY_CAUSE_POLICY, reason="denied")
    assert provider.calls == ["steer", "reject"], provider.calls


@pytest.mark.asyncio
async def test_funnel_audit_lands_before_the_steer():
    order: list[str] = []
    fake_sel = MagicMock()
    fake_sel.log_tool_invocation = MagicMock(side_effect=lambda **kw: order.append("audit"))
    provider = _Provider()

    async def _steer(message: str) -> bool:
        order.append("steer")
        return True

    async def _reject(request_id) -> None:
        order.append("reject")

    provider.steer = _steer  # type: ignore[method-assign]
    provider.reject_tool = _reject  # type: ignore[method-assign]
    with patch.object(subagent, "sel", lambda: fake_sel):
        await SubagentManager._reject_and_log(
            provider,
            "r1",
            "subagent:a1",
            _event(),
            cause=DENY_CAUSE_POLICY,
            reason="denied",
            error="hook_deny",
        )
    assert order == ["audit", "steer", "reject"], order
    row = fake_sel.log_tool_invocation.call_args.kwargs
    assert row["outcome"] == "denied" and row["error"] == "hook_deny"


@pytest.mark.asyncio
async def test_funnel_failing_audit_still_steers_and_rejects():
    # A SEL audit that raises (an unloadable trust root is permanent per
    # process) must NOT skip the steer and the reject: the wire request would
    # stay unanswered and hang the turn. The audit is best-effort; answering
    # the model is the critical path.
    order: list[str] = []
    fake_sel = MagicMock()
    fake_sel.log_tool_invocation = MagicMock(side_effect=RuntimeError("SEL trust root unloadable"))
    provider = _Provider()

    async def _steer(message: str) -> bool:
        order.append("steer")
        return True

    async def _reject(request_id) -> None:
        order.append("reject")

    provider.steer = _steer  # type: ignore[method-assign]
    provider.reject_tool = _reject  # type: ignore[method-assign]
    with patch.object(subagent, "sel", lambda: fake_sel):
        await SubagentManager._reject_and_log(
            provider,
            "r1",
            "subagent:a1",
            _event(),
            cause=DENY_CAUSE_POLICY,
            reason="denied",
            error="hook_deny",
        )
    assert order == ["steer", "reject"], order


@pytest.mark.asyncio
async def test_funnel_keeps_the_child_metric_before_the_wire():
    # The hang-resilience counter for a backend child's denial is emitted with
    # the audit, ahead of the steer and the reject.
    order: list[str] = []
    provider = _Provider()

    async def _reject(request_id) -> None:
        order.append("reject")

    provider.reject_tool = _reject  # type: ignore[method-assign]
    event = SimpleNamespace(title="t", tool_kind="tool", request_id="r9", sub_session_id="child")
    with (
        patch.object(subagent, "sel"),
        patch.object(
            subagent, "emit_counter", side_effect=lambda *a, **k: order.append("metric")
        ) as counter,
    ):
        await SubagentManager._reject_and_log(
            provider, "r9", "k", event, cause=None, error="child_escalation_limit"
        )
    assert order == ["metric", "reject"], order
    assert counter.call_args.args[1] == {"surface": "subagent", "reason": "child_escalation_limit"}


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
#: The subagent surface denies through ONE funnel (``_reject_and_log`` in
#: ``subagent.py``), whose required ``cause=`` keyword carries the verdict: a
#: DENY_CAUSE_* name steers, ``None`` is the explicit "not a host deny". The
#: sites are its call sites in ``subagent_manager/run.py``.
_SUBAGENT_SITES = (
    _Site('error="child_escalation_limit"', "teardown"),
    _Site('error="turn_limit"', "teardown"),
    _Site('"reason": "spec_hook"', "host"),
    _Site("The hook judged the call itself", "host"),
    _Site('error="child_interactive_rejected"', "user"),
    _Site('error="child_origin_no_command_context"', "host"),
    _Site('"reason": "factory_rejected"', "user"),
    _Site("interactive_rejected.", "user"),
    _Site('"reason": "no_policy_deny_default"', "host"),
)

_REJECT = re.compile(r"^\s*await [\w.]+\.reject_tool\(")
_FUNNEL = re.compile(r"^\s*await self\._manager\._reject_and_log\(")
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


class TestSubagentDeniesThroughOneSteeringFunnel:
    """Coverage checkable from the source, not asserted in a PR body."""

    FUNNEL_MODULE = "subagent.py"
    SITES_MODULE = "subagent_manager/run.py"

    def test_the_funnel_module_has_exactly_one_wire_reject_and_it_is_the_funnel(self):
        lines = _lines(self.FUNNEL_MODULE)
        src = "\n".join(lines)
        assert src.count(".reject_tool(") == 1, "every reject must go through _reject_and_log"
        (i,) = _matches(_REJECT, lines)
        funnel = inspect.getsource(SubagentManager._reject_and_log)
        assert lines[i].strip() in funnel

    def test_the_sites_module_has_no_wire_reject_of_its_own(self):
        src = "\n".join(_lines(self.SITES_MODULE))
        assert ".reject_tool(" not in src, "run.py must deny through the funnel only"

    def test_the_funnel_audits_then_steers_then_rejects(self):
        body = inspect.getsource(SubagentManager._reject_and_log)
        audit = body.index("sel().log_tool_invocation(")
        steer = body.index("await _steer_host_deny(")
        reject = body.index("await client.reject_tool(")
        assert audit < steer < reject, "audit first, then the notice, then the wire"
        assert "if cause is not None:" in body, "None is the explicit not-a-host-deny verdict"

    def test_the_cause_is_a_required_keyword_with_no_default(self):
        sig = inspect.signature(SubagentManager._reject_and_log)
        cause = sig.parameters["cause"]
        assert cause.kind is inspect.Parameter.KEYWORD_ONLY
        assert cause.default is inspect.Parameter.empty

    def test_the_scan_finds_every_funnel_call_the_source_contains(self):
        lines = _lines(self.SITES_MODULE)
        src = "\n".join(lines)
        textual = src.count("_reject_and_log(")
        found = len(_matches(_FUNNEL, lines))
        assert found == textual == len(_SUBAGENT_SITES), (found, textual, len(_SUBAGENT_SITES))

    def test_every_call_carries_exactly_one_verdict(self):
        lines = _lines(self.SITES_MODULE)
        seen = sorted(site.fingerprint for _i, site in self._walk(lines))
        assert seen == sorted(s.fingerprint for s in _SUBAGENT_SITES)

    def test_every_host_deny_names_a_deny_cause_and_a_reason(self):
        lines = _lines(self.SITES_MODULE)
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

    def test_user_rejections_and_bails_are_not_steered(self):
        lines = _lines(self.SITES_MODULE)
        for i, site in self._walk(lines):
            if site.verdict == "host":
                continue
            block = "\n".join(lines[i : i + _CALL_SPAN])
            assert "cause=None" in block, (
                f"line {i + 1}: a genuine user rejection (or a run bail) must say so "
                "with cause=None, never claim it was not a user action"
            )
            assert "DENY_CAUSE_" not in block

    def test_the_user_rejections_are_allowlisted_individually(self):
        user = [s for s in _SUBAGENT_SITES if s.verdict == "user"]
        assert len(user) == 3, (
            "the allowlist must name each USER rejection individually -- a new one "
            "needs its own per-site judgement, not a wider marker"
        )

    def test_no_stray_steer_outside_the_funnel(self):
        # One spelling of the notice on the surface, inside the funnel.
        assert len(_matches(_STEER, _lines(self.FUNNEL_MODULE))) == 1
        assert not _matches(_STEER, _lines(self.SITES_MODULE))
        assert "await _steer_host_deny(" in inspect.getsource(SubagentManager._reject_and_log)

    def test_the_funnel_delegates_to_the_shared_helper(self):
        assert subagent._steer_host_deny is llm_helpers._steer_host_deny

    def test_the_headless_reason_names_every_authorizing_tier(self):
        # parent_policy=auto, the hook's name grant and its read-only
        # classification can each still authorize a call on this surface; a
        # notice naming fewer would understate what the run may call.
        reason = subagent._HEADLESS_DENY_REASON
        for tier in ("parent_policy=auto", "hooks.auto_approve_tools", "read-only"):
            assert tier in reason, tier

    def _walk(self, lines: list[str]) -> list[tuple[int, _Site]]:
        out: list[tuple[int, _Site]] = []
        for i in _matches(_FUNNEL, lines):
            # The fingerprint sits in the call's own arguments or the comment
            # right above it; never read back past the previous call.
            out.append((i, _site_for(_SUBAGENT_SITES, lines, max(0, i - 6), i + _CALL_SPAN)))
        return out
