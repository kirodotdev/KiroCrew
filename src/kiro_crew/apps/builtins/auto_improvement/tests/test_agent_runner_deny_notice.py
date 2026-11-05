"""Every HOST deny of the unattended agent runner steers the in-band notice first.

A rejected permission reaches the model as kiro-cli's fixed "User denied tool
execution". ``spine/agent_runner.py`` denies through ONE funnel
(``SessionAgentRunner._reject``) from three sites (a governance verdict, the
app-local shell denylist, the caller's allowlist) and refuses once more in
``_approve`` when the audit row its approval requires cannot be written. No person
is attached to this surface, so every deny is a host deny and every one owes a
notice, through core's ``llm_helpers._steer_host_deny``.

Two halves:

* BEHAVIOURAL tests, one per deny reason, that drive the real
  ``SessionAgentRunner`` with a provider double recording steer/reject ORDER. Order
  is the mechanism: the steer must be written while the permission request is still
  unanswered, because that is what proves the turn is in flight and gets the notice
  queued instead of dropped;
* a SOURCE-LEVEL guard that enumerates every ``reject_tool(`` site and every funnel
  call, gives each a per-site verdict, and fails when a host deny is not steered,
  when a steer names no cause, or when the funnel's audit is not written before the
  steer.
"""

from __future__ import annotations

import inspect
import pathlib
import re
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import llm_helpers
from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as R
from kiro_crew.constants import (
    DENY_CAUSE_HOOK_ERROR,
    DENY_CAUSE_POLICY,
    DENY_CAUSE_SURFACE_POLICY,
)
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
        self._supports_refusal_steer = supports_steer
        self.calls: list[str] = []
        self.steered: list[str] = []
        self.rejected: list[str] = []
        self.approved: list[str] = []
        self._events = events
        self._title = title
        self._request_id = request_id

    @property
    def supports_refusal_steer(self) -> bool:
        # The notice probes the NARROWER capability: a harness can take a
        # mid-turn steer and still drop one sent while a refusal is answered.
        return self._supports_refusal_steer

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


# ── The runner's deny reasons ────────────────────────────────────────────────


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


# ── Source-level coverage guard ──────────────────────────────────────────────


@dataclass(frozen=True)
class _Site:
    """One deny site: a fingerprint unique to it and the cause it must steer."""

    fingerprint: str
    cause: str


_RUNNER_MODULE = pathlib.Path(R.__file__)
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


def _lines(module: pathlib.Path) -> list[str]:
    return module.read_text(encoding="utf-8").splitlines()


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


class TestTheRunnerDeniesThroughOneSteeringFunnel:
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
