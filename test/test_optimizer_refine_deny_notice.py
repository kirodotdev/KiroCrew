"""Every HOST deny on the prompt optimizer and the task-refine turn steers the
in-band notice before it rejects.

A rejected permission reaches the model as kiro-cli's fixed "User denied tool
execution". The dashboard chat runner, ``llm_helpers``, the messaging surfaces,
the task runner, the eval harness and the subagent surface steer the real reason
into the running turn first; the two surfaces here must do the same:

* ``dashboard/handlers/optimizer.py`` -- the prompt optimizer's side-session
  runs no tools; its one reject is a surface-policy deny.
* ``dashboard/handlers/taskrunner.py`` -- ``_run_refine`` drafts a task spec with
  text only; its one reject is a surface-policy deny.

The audit-unavailable cause's shared wording is pinned here too. The unattended
runner a built-in app drives pins its own funnel in that app's tests.

Two halves, mirroring ``test_taskrunner_deny_notice.py`` and
``test_eval_subagent_deny_notice.py``:

* a SOURCE-LEVEL guard that finds every ``reject_tool(`` site and fails when a host
  deny is not steered, when a steer names no cause, or when the audit is not
  written before the steer;
* BEHAVIOURAL tests that drive the real handlers with a provider double recording
  steer/reject ORDER. Order is the mechanism: the steer must be written while the
  permission request is still unanswered, because that is what proves the turn is
  in flight and gets the notice queued instead of dropped.
"""

from __future__ import annotations

import json
import pathlib
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import llm_helpers
from kiro_crew.constants import (
    DENY_CAUSE_AUDIT_UNAVAILABLE,
    DENY_CAUSE_HOOK_ERROR,
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


@pytest.mark.asyncio
async def test_refine_drops_the_notices_steer_acknowledgement_from_the_spec(refine_sel):
    """The ack is protocol: the drafted spec is the answer."""
    client = _Provider(
        events=[
            LLMEvent(kind=EVENT_PERMISSION_REQUEST, title="Read file", request_id="r7"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="[STEERING steer-83cc1f2a: The notice"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=" named the rule.]# Task: T"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
    )
    state = _refine_state(client)
    # The status read answers while the session resets: it must already be clean.
    texts_at_reset: list[str] = []
    state.sessions.reset = AsyncMock(
        side_effect=lambda _key: texts_at_reset.append(state._refine_text)
    )
    await _run_refine(state, "x")
    assert client.calls == ["steer", "reject"]
    assert texts_at_reset == ["# Task: T"]
    assert state._refine_status == "done"
    assert state.broadcast_ws.call_args.args[1]["text"] == "# Task: T"


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
