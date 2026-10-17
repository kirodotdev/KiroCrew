"""Every HOST deny on the prompt optimizer, the task-refine turn and the
unattended auto-improvement runner steers the in-band notice before it rejects.

A rejected permission reaches the model as kiro-cli's fixed "User denied tool
execution". The dashboard chat runner, ``llm_helpers``, the messaging surfaces,
the task runner, the eval harness and the subagent surface steer the real reason
into the running turn first; the three surfaces here must do the same:

* ``dashboard/handlers/optimizer.py`` -- the prompt optimizer's side-session
  runs no tools; its one reject is a surface-policy deny.
* ``dashboard/handlers/taskrunner.py`` -- ``_run_refine`` drafts a task spec with
  text only; its one reject is a surface-policy deny.
* ``apps/builtins/auto_improvement/spine/agent_runner.py`` -- the unattended
  runner denies through ONE funnel (``SessionAgentRunner._reject``) from three
  sites (a governance verdict, the app-local shell denylist, the caller's
  allowlist) and refuses once more in ``_approve`` when the audit row its
  approval requires cannot be written. No person is attached to this surface,
  so every deny is a host deny and every one owes a notice.

Two halves, mirroring ``test_taskrunner_deny_notice.py`` and
``test_eval_subagent_deny_notice.py``:

* a SOURCE-LEVEL guard that enumerates every ``reject_tool(`` site and every
  funnel call, gives each a per-site verdict, and fails when a host deny is not
  steered, when a steer names no cause, or when the funnel's audit is not
  written before the steer;
* BEHAVIOURAL tests, one per deny reason, that drive the real handlers /
  ``SessionAgentRunner`` with a provider double recording steer/reject ORDER.
  Order is the mechanism: the steer must be written while the permission
  request is still unanswered, because that is what proves the turn is in
  flight and gets the notice queued instead of dropped.
"""

from __future__ import annotations

import inspect
import json
import pathlib
import re
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import llm_helpers
from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as R
from kiro_crew.constants import (
    DENY_CAUSE_AUDIT_UNAVAILABLE,
    DENY_CAUSE_HOOK_ERROR,
    DENY_CAUSE_POLICY,
    DENY_CAUSE_SURFACE_POLICY,
)
from kiro_crew.dashboard.handlers import optimizer as optimizer_mod
from kiro_crew.dashboard.handlers import taskrunner as taskrunner_mod
from kiro_crew.dashboard.handlers.optimizer import handle_optimize
from kiro_crew.dashboard.handlers.taskrunner import _run_refine
from kiro_crew.deny_notice import _DENY_CAUSE_TEXT, build_refusal_steer_notice
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    LLMEvent,
)

_GENERIC = "User denied tool execution"
_TAG = "[Kiro Crew host notice]"
#: Only the POLICY cause appends class-specific remediation; its guidance line
#: is the fingerprint that must be absent from every other cause's notice.
_POLICY_GUIDANCE = "allowed alternative"
_SURFACE_CLAUSE = "tool policy of the surface"
_HOOK_ERROR_CLAUSE = "PreToolUse hook raised"
_AUDIT_CLAUSE = "could not write the audit record"
_SRC = pathlib.Path(__file__).resolve().parents[1] / "src/kiro_crew"


# ── Doubles ──────────────────────────────────────────────────────────────────


class _Provider:
    """Permission-answering double recording steer/approve/reject ORDER."""

    def __init__(
        self,
        events: list[Any] | None = None,
        *,
        title: str = "execute_bash",
        request_id: str = "r1",
        supports_steer: bool = True,
    ) -> None:
        # The notice probes the NARROWER capability: a harness can take a
        # mid-turn steer and still drop one sent while a refusal is answered.
        self.supports_refusal_steer = supports_steer
        self.calls: list[str] = []
        self.steered: list[str] = []
        self.rejected: list[str] = []
        self.approved: list[str] = []
        self._events = events
        self._title = title
        self._request_id = request_id

    async def start(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def stream(self, message: str):
        events = self._events
        if events is None:
            events = [
                LLMEvent(
                    kind=EVENT_PERMISSION_REQUEST, title=self._title, request_id=self._request_id
                ),
                LLMEvent(kind=EVENT_TEXT_CHUNK, text="rewritten"),
                LLMEvent(kind=EVENT_COMPLETE),
            ]
        for ev in events:
            yield ev

    async def steer(self, message: str) -> bool:
        self.calls.append("steer")
        self.steered.append(message)
        return True

    async def approve_tool(self, request_id) -> bool:
        self.calls.append("approve")
        self.approved.append(request_id)
        return True

    async def reject_tool(self, request_id) -> None:
        self.calls.append("reject")
        self.rejected.append(request_id)


class _FakeSel:
    """Stand-in for the Security Event Log singleton: records, optionally fails.

    *order* is a shared list the steer double also appends to, so a test can
    assert the audit row landed before the steer touched the pipe.
    """

    def __init__(self, *, fail: bool = False, order: list[str] | None = None) -> None:
        self.calls: list[dict] = []
        self.fail = fail
        self.order = order if order is not None else []

    def log_tool_invocation(self, **kw) -> None:
        self.order.append("audit")
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("SEL unwritable")


@pytest.fixture
def fake_sel(monkeypatch):
    sel = _FakeSel()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel)
    return sel


@pytest.fixture
def broken_sel(monkeypatch):
    sel = _FakeSel(fail=True)
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel)
    return sel


def _assert_steered_then_rejected(provider: _Provider, *fragments: str) -> str:
    assert provider.calls == ["steer", "reject"], provider.calls
    (notice,) = provider.steered
    assert notice.startswith(_TAG)
    assert _GENERIC in notice, "the notice must name the string it is correcting"
    assert "NOT a user action" in notice
    for fragment in fragments:
        assert fragment in notice, (fragment, notice)
    return notice


def _recording_steer(client: _Provider, order: list[str]):
    async def _steer(message: str) -> bool:
        order.append("steer")
        client.calls.append("steer")
        client.steered.append(message)
        return True

    return _steer


def _sessions(client: _Provider) -> MagicMock:
    sessions = MagicMock()
    sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    return sessions


# ── The prompt optimizer ─────────────────────────────────────────────────────


class _ReadyPrerequisite:
    """The optimizer's readiness gate, already satisfied."""

    def is_ready(self) -> bool:
        return True


def _optimize_request(client: _Provider) -> MagicMock:
    state = MagicMock()
    state.sessions = _sessions(client)
    request = MagicMock()
    request.json = AsyncMock(
        return_value={"prompt": "refactor the auth module to be cleaner", "context": ""}
    )
    request.app = {"state": state, "kiro_prerequisite_service": _ReadyPrerequisite()}
    return request


@pytest.fixture
def optimizer_sel(monkeypatch):
    sel = _FakeSel()
    monkeypatch.setattr(optimizer_mod, "sel", lambda: sel)
    return sel


@pytest.fixture
def refine_sel(monkeypatch):
    sel = _FakeSel()
    monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: sel)
    return sel


@pytest.mark.asyncio
async def test_optimizer_audits_then_steers_the_surface_cause_before_rejecting(optimizer_sel):
    client = _Provider(title="Running: cat ~/.aws/credentials")
    order = optimizer_sel.order
    client.steer = _recording_steer(client, order)  # type: ignore[method-assign]
    resp = await handle_optimize(_optimize_request(client))
    assert json.loads(resp.text or "")["optimized"] == "rewritten"
    notice = _assert_steered_then_rejected(
        client, _SURFACE_CLAUSE, optimizer_mod._OPTIMIZER_DENY_REASON, "runs no tools"
    )
    # The surface refused the call without judging it, so no sanctioned
    # alternative is offered -- not even for a credential-shaped title.
    assert _POLICY_GUIDANCE not in notice
    assert client.rejected == ["r1"]
    # backend-security-controls: the denied attempt is a SEL row, written
    # BEFORE the steer touches the pipe.
    assert order == ["audit", "steer"]
    (row,) = optimizer_sel.calls
    assert row["outcome"] == "denied" and row["source"] == "optimizer"
    assert row["request_id"] == "r1" and row["tool_name"] == "Running: cat ~/.aws/credentials"


@pytest.mark.asyncio
async def test_optimizer_backend_without_steer_only_rejects(optimizer_sel):
    client = _Provider(supports_steer=False)
    await handle_optimize(_optimize_request(client))
    assert client.calls == ["reject"]
    assert client.rejected == ["r1"]


# ── The task-refine turn ─────────────────────────────────────────────────────


def _refine_state(client: _Provider) -> Any:
    return SimpleNamespace(
        task_runner=None,
        sessions=_sessions(client),
        _refine_task=None,
        _refine_text="",
        _refine_error="",
        _refine_status="idle",
        _refine_input="",
        _refine_session_key="",
        _refine_answer_future=None,
        _background_tasks=set(),
        broadcast_ws=MagicMock(),
        push_refresh=MagicMock(),
        push_slots_update=MagicMock(),
    )


@pytest.mark.asyncio
async def test_refine_audits_then_steers_the_surface_cause_before_rejecting(refine_sel):
    client = _Provider(title="Read file", request_id="r7")
    order = refine_sel.order
    client.steer = _recording_steer(client, order)  # type: ignore[method-assign]
    state = _refine_state(client)
    await _run_refine(state, "build a thing")
    assert state._refine_status == "done"
    notice = _assert_steered_then_rejected(
        client, _SURFACE_CLAUSE, taskrunner_mod._REFINE_DENY_REASON, "runs no tools"
    )
    assert _POLICY_GUIDANCE not in notice
    assert client.rejected == ["r7"]
    assert order == ["audit", "steer"]
    (row,) = refine_sel.calls
    assert row["outcome"] == "denied" and row["source"] == "taskrunner_refine"
    assert row["request_id"] == "r7" and row["tool_name"] == "Read file"
    assert row["session_key"].startswith("taskrunner:refine:")


@pytest.mark.asyncio
async def test_refine_backend_without_steer_only_rejects(refine_sel):
    client = _Provider(supports_steer=False, request_id="r7")
    state = _refine_state(client)
    await _run_refine(state, "x")
    assert state._refine_status == "done"
    assert client.calls == ["reject"]
    assert len(refine_sel.calls) == 1


@pytest.mark.asyncio
async def test_refine_a_failing_steer_still_rejects(refine_sel):
    client = _Provider(request_id="r7")

    async def _boom(message: str) -> bool:
        client.calls.append("steer")
        raise RuntimeError("pipe closed")

    client.steer = _boom  # type: ignore[method-assign]
    state = _refine_state(client)
    await _run_refine(state, "x")
    assert client.calls == ["steer", "reject"]
    assert state._refine_status == "done"


# ── The unattended auto-improvement runner ───────────────────────────────────


def _ev(**kw):
    kw.setdefault("kind", "")
    return SimpleNamespace(**kw)


async def _drive(provider: _Provider, **kw):
    kw.setdefault("cwd", "/tmp/wt")
    kw.setdefault("append_system", None)
    kw.setdefault("timeout_s", 30.0)
    kw.setdefault("t0", time.monotonic())
    runner = R.SessionAgentRunner()
    return await runner._run_async("prompt", factory=lambda key, **k: provider, **kw)


def _permission_then_complete(**kw) -> list[Any]:
    kw.setdefault("kind", EVENT_PERMISSION_REQUEST)
    kw.setdefault("tool_kind", "fsRead")
    kw.setdefault("request_id", "r1")
    kw.setdefault("title", "Read ~/.ssh/id_rsa")
    return [_ev(**kw), _ev(kind=EVENT_COMPLETE)]


@pytest.mark.asyncio
async def test_governance_deny_steers_the_hooks_reason_as_policy(monkeypatch, fake_sel):
    monkeypatch.setattr(R, "_governance_denial", lambda ev, **kw: "reads ~/.ssh")
    provider = _Provider(_permission_then_complete())
    res = await _drive(provider)
    assert res.ok is True
    notice = _assert_steered_then_rejected(
        provider, "Kiro Crew safety policy", "Read ~/.ssh/id_rsa: reads ~/.ssh"
    )
    assert _SURFACE_CLAUSE not in notice
    # Audit FIRST: the SEL row is on the record before the steer touched the pipe,
    # and it names WHICH gate refused.
    (row,) = fake_sel.calls
    assert row["outcome"] == "denied" and row["error"] == "governance_deny"


@pytest.mark.asyncio
async def test_governance_hook_layer_fault_steers_the_hook_error_cause(monkeypatch, fake_sel):
    monkeypatch.setattr(
        R,
        "_governance_denial",
        lambda ev, **kw: R._GovernanceDeny(
            "governance hook unavailable: hooks config unreadable", cause=DENY_CAUSE_HOOK_ERROR
        ),
    )
    provider = _Provider(_permission_then_complete())
    await _drive(provider)
    notice = _assert_steered_then_rejected(
        provider, _HOOK_ERROR_CLAUSE, "hooks config unreadable", "host fault"
    )
    assert "safety policy" not in notice
    assert _POLICY_GUIDANCE not in notice
    # The ledger tells a hook outage from a policy refusal.
    assert fake_sel.calls[0]["error"] == "governance_hook_unavailable"


def test_the_gate_names_the_hook_error_cause_when_the_hook_layer_raises(monkeypatch):
    monkeypatch.setattr(
        R, "KiroCrewConfig", SimpleNamespace(load=lambda: (_ for _ in ()).throw(OSError("x")))
    )
    reason = R._governance_denial(_ev(title="Bash"), session_key="s", agent="a")
    assert isinstance(reason, R._GovernanceDeny)
    assert reason.cause == DENY_CAUSE_HOOK_ERROR
    # Still a plain deny string to every caller that reads it as one.
    assert reason and "governance hook unavailable" in reason


def test_the_gate_names_the_policy_cause_when_a_hook_denies(monkeypatch):
    class _Manager:
        def __init__(self, cfg):
            pass

        def on_tool_call(self, name, **kw):
            return SimpleNamespace(action=R.TOOL_DENY, reason="reads ~/.aws")

    monkeypatch.setattr(
        R, "KiroCrewConfig", SimpleNamespace(load=lambda: SimpleNamespace(hooks={}))
    )
    monkeypatch.setattr(R, "hooks_config_from_config_dict", lambda d: d)
    monkeypatch.setattr(R, "HookManager", _Manager)
    reason = R._governance_denial(_ev(title="Read"), session_key="s", agent="a")
    assert isinstance(reason, R._GovernanceDeny)
    assert reason.cause == DENY_CAUSE_POLICY
    assert reason == "reads ~/.aws"


@pytest.mark.asyncio
async def test_shell_denylist_refusal_steers_as_policy(monkeypatch, fake_sel):
    monkeypatch.setattr(R, "_governance_denial", lambda ev, **kw: "")
    monkeypatch.setattr(R, "shell_command_refusal", lambda cmd: "gh pr merge is not permitted")
    provider = _Provider(
        _permission_then_complete(tool_kind="bash", is_shell=True, title="Running: gh pr merge")
    )
    await _drive(provider)
    notice = _assert_steered_then_rejected(
        provider, "Kiro Crew safety policy", "gh pr merge is not permitted"
    )
    assert _SURFACE_CLAUSE not in notice
    assert fake_sel.calls[0]["error"] == "shell_denylist"


@pytest.mark.asyncio
async def test_allowlist_refusal_steers_the_surface_cause_naming_the_list(monkeypatch, fake_sel):
    monkeypatch.setattr(R, "_governance_denial", lambda ev, **kw: "")
    provider = _Provider(_permission_then_complete(tool_kind="fsWrite"))
    await _drive(provider, allowed_tools=["Read", "Grep"])
    notice = _assert_steered_then_rejected(
        provider, _SURFACE_CLAUSE, "permits only the tools its caller listed", "Read, Grep"
    )
    assert _POLICY_GUIDANCE not in notice
    assert fake_sel.calls[0]["error"] == "not_in_allowed_tools"


@pytest.mark.asyncio
async def test_empty_allowlist_refusal_says_no_tools_at_all(monkeypatch, fake_sel):
    monkeypatch.setattr(R, "_governance_denial", lambda ev, **kw: "")
    provider = _Provider(_permission_then_complete(tool_kind="fsRead"))
    await _drive(provider, allowed_tools=[])
    _assert_steered_then_rejected(provider, _SURFACE_CLAUSE, "permits no tools at all")


@pytest.mark.asyncio
async def test_audit_failure_on_approve_steers_the_audit_cause(monkeypatch, broken_sel):
    monkeypatch.setattr(R, "_governance_denial", lambda ev, **kw: "")
    provider = _Provider(_permission_then_complete(tool_kind="fsRead"))
    await _drive(provider)
    notice = _assert_steered_then_rejected(
        provider, _AUDIT_CLAUSE, R._AUDIT_UNAVAILABLE_REASON, "host fault"
    )
    assert provider.approved == []
    assert _HOOK_ERROR_CLAUSE not in notice, "the audit fault is not a hook fault"
    assert _POLICY_GUIDANCE not in notice


@pytest.mark.asyncio
async def test_an_approval_that_lands_sends_no_notice(monkeypatch, fake_sel):
    monkeypatch.setattr(R, "_governance_denial", lambda ev, **kw: "")
    provider = _Provider(_permission_then_complete(tool_kind="fsRead"))
    await _drive(provider)
    assert provider.calls == ["approve"]


@pytest.mark.asyncio
async def test_backend_without_steer_only_rejects(monkeypatch, fake_sel):
    monkeypatch.setattr(R, "_governance_denial", lambda ev, **kw: "reads ~/.ssh")
    provider = _Provider(_permission_then_complete(), supports_steer=False)
    await _drive(provider)
    assert provider.calls == ["reject"]


@pytest.mark.asyncio
async def test_the_funnel_audits_before_it_steers(fake_sel):
    order = fake_sel.order
    provider = _Provider()
    provider.steer = _recording_steer(provider, order)  # type: ignore[method-assign]
    await R.SessionAgentRunner._reject(
        provider, "r9", tool="bash", session_key="s", cause=DENY_CAUSE_POLICY, reason="why"
    )
    assert order == ["audit", "steer"]
    assert provider.rejected == ["r9"]


@pytest.mark.asyncio
async def test_the_funnel_without_an_event_names_the_call_by_tool(fake_sel):
    provider = _Provider()
    await R.SessionAgentRunner._reject(
        provider, "r9", tool="bash", cause=DENY_CAUSE_SURFACE_POLICY, reason="why"
    )
    _assert_steered_then_rejected(provider, "Blocked: bash: why")


@pytest.mark.asyncio
async def test_the_funnel_with_cause_none_stays_bare(fake_sel):
    provider = _Provider()
    await R.SessionAgentRunner._reject(provider, "r9", tool="bash", cause=None)
    assert provider.calls == ["reject"]


@pytest.mark.asyncio
async def test_agent_authored_text_is_redacted_before_it_reaches_the_model(fake_sel):
    provider = _Provider()
    token = "ghp_" + "A" * 36
    await R.SessionAgentRunner._reject(
        provider,
        "r9",
        tool="bash",
        event=_ev(request_id="r9", title=f"Running: curl -H 'Authorization: {token}'"),
        cause=DENY_CAUSE_POLICY,
        reason=f"matched {token}",
    )
    (notice,) = provider.steered
    assert token not in notice


# ── The new cause's wording ──────────────────────────────────────────────────


class TestAuditUnavailableCause:
    def test_is_in_the_shared_table(self):
        assert DENY_CAUSE_AUDIT_UNAVAILABLE in _DENY_CAUSE_TEXT

    def test_names_the_audit_record_and_offers_no_remediation(self):
        out = build_refusal_steer_notice(
            "Running: cat ~/.aws/credentials", "why", cause=DENY_CAUSE_AUDIT_UNAVAILABLE
        )
        assert _AUDIT_CLAUSE in out
        assert "host fault" in out
        # Nothing judged the action, so naming a sanctioned alternative -- even
        # for a credential-shaped title -- would imply it had been refused.
        assert _POLICY_GUIDANCE not in out
        assert "How to do this properly" not in out

    def test_is_not_the_hook_fault(self):
        out = build_refusal_steer_notice("bash", "why", cause=DENY_CAUSE_AUDIT_UNAVAILABLE)
        assert _HOOK_ERROR_CLAUSE not in out
        assert DENY_CAUSE_AUDIT_UNAVAILABLE != DENY_CAUSE_HOOK_ERROR


# ── Source-level coverage guard ──────────────────────────────────────────────


@dataclass(frozen=True)
class _Site:
    """One deny site: a fingerprint unique to it and the cause it must steer."""

    fingerprint: str
    cause: str


_RUNNER_MODULE = "apps/builtins/auto_improvement/spine/agent_runner.py"
#: ``SessionAgentRunner._reject`` funnel calls in ``_run_async``, by verdict.
_RUNNER_FUNNEL_SITES = (
    _Site("governance: %s", "gov_cause"),  # policy or hook_error, named by the gate
    _Site("shell denylist judged the command", "DENY_CAUSE_POLICY"),
    _Site("caller's allowlist is what this SURFACE permits", "DENY_CAUSE_SURFACE_POLICY"),
)
#: Wire rejects outside the funnel: ``_approve``'s audit-or-deny arm.
_RUNNER_DIRECT_SITES = (_Site("rejecting instead of approving", "DENY_CAUSE_AUDIT_UNAVAILABLE"),)

_REJECT = re.compile(r"^\s*await \w+\.reject_tool\(")
_FUNNEL = re.compile(r"^\s*await self\._reject\($")
_STEER = re.compile(r"^\s*await _steer_host_deny\(")
_AUDIT = re.compile(r"^\s*_?sel\(\)\.log_tool_invocation\(")
#: Lines a call's arguments may span (black wraps the keyword list).
_CALL_SPAN = 16


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


class TestEveryDenyInTheDashboardHandlersSteersFirst:
    """The optimizer and the refine turn each deny inline, steer then reject."""

    MODULES = {
        "dashboard/handlers/optimizer.py": "_OPTIMIZER_DENY_REASON",
        "dashboard/handlers/taskrunner.py": "_REFINE_DENY_REASON",
    }
    WINDOW = 10

    @pytest.mark.parametrize("module", sorted(MODULES))
    def test_the_module_has_exactly_one_reject_and_it_is_steered(self, module):
        lines = _lines(module)
        src = "\n".join(lines)
        assert src.count(".reject_tool(") == 1, module
        (i,) = _matches(_REJECT, lines)
        steers = [j for j in range(max(0, i - self.WINDOW), i) if _STEER.match(lines[j])]
        assert steers, (
            f"{module}:{i + 1} hands the model kiro-cli's generic 'user denied' with "
            "nothing to correct it -- await _steer_host_deny(...) first"
        )
        block = "\n".join(lines[steers[-1] : i])
        assert "cause=DENY_CAUSE_SURFACE_POLICY" in block, module
        assert self.MODULES[module] in block, module
        # backend-security-controls: the SEL row is written before the steer.
        audits = [j for j in range(max(0, steers[-1] - 12), steers[-1]) if _AUDIT.match(lines[j])]
        assert audits, f"{module}: the denied attempt must be a SEL row before the steer"

    @pytest.mark.parametrize("module", sorted(MODULES))
    def test_no_stray_steer(self, module):
        assert len(_matches(_STEER, _lines(module))) == 1

    def test_the_modules_use_the_shared_helper(self):
        assert optimizer_mod._steer_host_deny is llm_helpers._steer_host_deny
        assert taskrunner_mod._steer_host_deny is llm_helpers._steer_host_deny


class TestTheAutoImprovementRunnerDeniesThroughOneSteeringFunnel:
    """Coverage checkable from the source, not asserted in a PR body."""

    def test_the_scan_finds_every_wire_reject_the_source_contains(self):
        lines = _lines(_RUNNER_MODULE)
        src = "\n".join(lines)
        textual = src.count(".reject_tool(")
        found = _matches(_REJECT, lines)
        # One inside the funnel, one in _approve's audit-or-deny arm.
        assert textual == len(found) == 1 + len(_RUNNER_DIRECT_SITES), (textual, found)

    def test_the_funnel_owns_one_wire_reject_and_the_rest_are_enumerated(self):
        lines = _lines(_RUNNER_MODULE)
        # Both rejects spell ``await provider.reject_tool(rid)``; the funnel's
        # own is told apart by its line range, not its text.
        body, start = inspect.getsourcelines(R.SessionAgentRunner._reject)
        inside = range(start - 1, start - 1 + len(body))
        outside = [i for i in _matches(_REJECT, lines) if i not in inside]
        assert len(outside) == len(_RUNNER_DIRECT_SITES), outside
        for i in outside:
            site = _site_for(_RUNNER_DIRECT_SITES, lines, max(0, i - 24), i + 1)
            steers = [j for j in range(max(0, i - 14), i) if _STEER.match(lines[j])]
            assert steers, f"line {i + 1}: a direct reject must steer first"
            block = "\n".join(lines[steers[-1] : i])
            assert f"cause={site.cause}" in block, block

    def test_the_funnel_audits_then_steers_then_rejects(self):
        body = inspect.getsource(R.SessionAgentRunner._reject)
        audit = body.index("sel().log_tool_invocation(")
        steer = body.index("await _steer_host_deny(")
        reject = body.index("await provider.reject_tool(")
        assert audit < steer < reject, "audit first, then the notice, then the wire"
        assert "if cause is not None:" in body, "None is the explicit no-notice verdict"

    def test_the_cause_is_a_required_keyword_with_no_default(self):
        sig = inspect.signature(R.SessionAgentRunner._reject)
        cause = sig.parameters["cause"]
        assert cause.kind is inspect.Parameter.KEYWORD_ONLY
        assert cause.default is inspect.Parameter.empty

    def test_the_scan_finds_every_funnel_call_the_source_contains(self):
        lines = _lines(_RUNNER_MODULE)
        src = "\n".join(lines)
        # The def line is the one non-call spelling.
        textual = src.count("._reject(") + src.count(" _reject(") - 1
        found = len(_matches(_FUNNEL, lines))
        assert found == textual == len(_RUNNER_FUNNEL_SITES), (found, textual)

    def test_every_funnel_call_carries_exactly_one_verdict(self):
        lines = _lines(_RUNNER_MODULE)
        seen = sorted(site.fingerprint for _i, site in self._walk(lines))
        assert seen == sorted(s.fingerprint for s in _RUNNER_FUNNEL_SITES)

    def test_every_funnel_call_names_a_deny_cause_a_reason_and_the_event(self):
        lines = _lines(_RUNNER_MODULE)
        bare: list[int] = []
        for i, site in self._walk(lines):
            block = "\n".join(lines[i : i + _CALL_SPAN])
            # The cause is the site's DENY_CAUSE_* (or, for the governance
            # call, the gate's own ``gov.cause``), written inline or as the
            # first line of a wrapped ``cause=(`` expression.
            named = f"cause={site.cause}" in block or f"cause=(\n{' ' * 32}{site.cause}" in block
            if not named or "reason=" not in block or "event=ev" not in block:
                bare.append(i + 1)
        assert not bare, (
            "these host denies hand the model kiro-cli's generic 'user denied' with "
            f"nothing to correct it -- pass cause=DENY_CAUSE_*, a reason and event=ev: {bare}"
        )

    def test_no_funnel_call_writes_cause_none(self):
        # Nobody is attached to this surface to say no, so every deny is a host
        # deny; a None here would be a wrong verdict, not a user rejection.
        lines = _lines(_RUNNER_MODULE)
        for i, _site in self._walk(lines):
            assert "cause=None" not in "\n".join(lines[i : i + _CALL_SPAN]), i + 1

    def test_the_governance_call_takes_its_cause_from_the_gate(self):
        lines = _lines(_RUNNER_MODULE)
        (i,) = [i for i, s in self._walk(lines) if s.fingerprint == "governance: %s"]
        block = "\n".join(lines[i : i + _CALL_SPAN])
        assert "cause=gov_cause" in block, "the gate names its own verdict; do not sniff the text"
        assert "startswith(" not in block

    def test_the_steers_are_exactly_the_funnels_and_the_audit_arms(self):
        lines = _lines(_RUNNER_MODULE)
        assert len(_matches(_STEER, lines)) == 1 + len(_RUNNER_DIRECT_SITES)

    def test_the_module_uses_the_shared_helper(self):
        assert R._steer_host_deny is llm_helpers._steer_host_deny

    def _walk(self, lines: list[str]) -> list[tuple[int, _Site]]:
        out: list[tuple[int, _Site]] = []
        for i in _matches(_FUNNEL, lines):
            # The fingerprint sits in the comment / log line right above the
            # call; never read back past the previous call.
            out.append((i, _site_for(_RUNNER_FUNNEL_SITES, lines, max(0, i - 12), i + 1)))
        return out
