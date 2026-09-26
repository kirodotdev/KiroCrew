"""Every HOST deny in ``llm_helpers`` steers the in-band notice before it rejects.

A rejected permission reaches the model as kiro-cli's fixed "User denied tool
execution". The dashboard chat runner steers the real reason into the running
turn first; ``llm_helpers`` is the surface behind cron, Slack, workflow,
heartbeat and background one-liner turns, none of which has a dashboard slot,
and it must do the same.

Two halves, mirroring ``test_refusal_inband_notice.py``:

* a SOURCE-LEVEL guard that walks every ``reject_tool(`` in the module and fails
  when one is not preceded by the shared steer within a few lines -- except the
  one genuine USER rejection, which must stay bare because there kiro-cli's
  wording is the truth and "this was NOT a user action" would be a lie;
* BEHAVIOURAL tests, one per deny reason, that drive the real
  ``_resolve_permission`` with a provider double recording steer/reject ORDER.
  Order is the mechanism: the steer must be written while the permission request
  is still unanswered, because that is what proves the turn is in flight and
  gets the notice queued instead of dropped.

The steer is one more await on the ACP pipe before the reject, so the guard also
pins the audit-first rule ``test_deny_audit_first`` states for the chat runner:
the SEL row is written BEFORE the steer and the reject, or a backend that stops
reading stdin would cancel the coroutine at the turn deadline with the decision
acted on and never audited.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
from unittest.mock import MagicMock

import pytest

from kiro_crew import llm_helpers
from kiro_crew.hooks import ToolHookResult
from kiro_crew.llm_helpers import ToolApprovalPolicy, _resolve_permission
from kiro_crew.name_grant import Refusal
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    LLMEvent,
)

_GENERIC = "User denied tool execution"
_TAG = "[Kiro Crew host notice]"
#: Only the POLICY cause appends class-specific remediation; its guidance line
#: is the fingerprint that must be absent from every surface-policy notice.
_POLICY_GUIDANCE = "allowed alternative"
_SURFACE_CLAUSE = "tool policy of the surface"


class _Provider:
    """Permission-answering double recording steer/reject ORDER."""

    def __init__(self, *, supports_steer: bool = True) -> None:
        self.supports_steer = supports_steer
        self.calls: list[str] = []
        self.steered: list[str] = []

    async def steer(self, message: str) -> bool:
        self.calls.append("steer")
        self.steered.append(message)
        return True

    async def approve_tool(self, request_id) -> None:
        self.calls.append("approve")

    async def reject_tool(self, request_id) -> None:
        self.calls.append("reject")


class _NoSteerProvider(_Provider):
    """A backend with no steer channel at all -- not even the attribute."""

    def __init__(self) -> None:
        super().__init__()
        del self.supports_steer


def _event(title: str = "Read README.md") -> LLMEvent:
    return LLMEvent(kind=EVENT_PERMISSION_REQUEST, title=title, request_id="r1")


def _hooks(result: ToolHookResult | None = None) -> MagicMock:
    hooks = MagicMock()
    hooks.effective_denied_regexes = MagicMock(return_value=[])
    if result is not None:
        hooks.on_tool_call = MagicMock(return_value=result)
    return hooks


def _assert_steered_then_rejected(provider: _Provider, *fragments: str) -> str:
    assert provider.calls == ["steer", "reject"], provider.calls
    (notice,) = provider.steered
    assert notice.startswith(_TAG)
    assert _GENERIC in notice, "the notice must name the string it is correcting"
    assert "NOT a user action" in notice
    for fragment in fragments:
        assert fragment in notice, (fragment, notice)
    return notice


# ── Behavioural: one per host-deny reason ──────────────────────────────────


@pytest.mark.asyncio
async def test_reject_all_policy_steers_before_rejecting():
    provider = _Provider()
    ok = await _resolve_permission(provider, _event(), ToolApprovalPolicy.REJECT_ALL, None)
    assert ok is False
    notice = _assert_steered_then_rejected(
        provider, "Read README.md", "reject-all", _SURFACE_CLAUSE
    )
    assert _POLICY_GUIDANCE not in notice


@pytest.mark.asyncio
async def test_missing_title_steers_before_rejecting():
    provider = _Provider()
    ok = await _resolve_permission(
        provider, _event(title=""), ToolApprovalPolicy.AUTO_APPROVE, None
    )
    assert ok is False
    # The empty title is the model's own malformed output -- the one deny it can
    # simply fix -- so it gets the validation wording, not a policy verdict.
    notice = _assert_steered_then_rejected(provider, "carried no title", "failed validation")
    assert _POLICY_GUIDANCE not in notice
    # The notice writes its own "Blocked: <title>: <reason>"; a title-less call is
    # named rather than rendered as a bare colon, and the lead is not doubled.
    assert "Blocked: unnamed tool call: the tool call carried no title" in notice
    assert "Blocked: :" not in notice
    assert notice.count("Blocked:") == 1


@pytest.mark.asyncio
async def test_always_deny_pattern_steers_before_rejecting():
    # The unconditional scan runs for EVERY policy, AUTO_APPROVE included -- the
    # cron ``approval_mode="auto"`` path -- so this is the deny a background turn
    # is likeliest to hit.
    provider = _Provider()
    ok = await _resolve_permission(
        provider, _event(title="rm -rf /"), ToolApprovalPolicy.AUTO_APPROVE, None
    )
    assert ok is False
    notice = _assert_steered_then_rejected(provider, "rm -rf /", "safety policy")
    # The policy wording carries the route-around guidance; the always-deny
    # pattern needs no distinct cause text.
    assert _POLICY_GUIDANCE in notice
    # The scan's reason carries its own "Blocked:" lead for the audit row; the
    # notice must not read "Blocked: rm -rf /: Blocked: ...".
    assert notice.count("Blocked:") == 1


@pytest.mark.asyncio
async def test_read_only_without_hooks_steers_before_rejecting():
    provider = _Provider()
    ok = await _resolve_permission(provider, _event(), ToolApprovalPolicy.READ_ONLY, None)
    assert ok is False
    notice = _assert_steered_then_rejected(provider, "read-only", _SURFACE_CLAUSE)
    assert _POLICY_GUIDANCE not in notice


@pytest.mark.asyncio
async def test_hook_deny_steers_the_hooks_reason_before_rejecting():
    provider = _Provider()
    hooks = _hooks(ToolHookResult.deny("Blocked: exfiltration to evil.example"))
    ok = await _resolve_permission(provider, _event(), ToolApprovalPolicy.HOOK_BASED, hooks)
    assert ok is False
    # A hook deny is a verdict on the call itself: policy wording, with guidance.
    notice = _assert_steered_then_rejected(
        provider, "exfiltration to evil.example", "safety policy"
    )
    assert _POLICY_GUIDANCE in notice
    assert "Blocked: Read README.md: exfiltration to evil.example" in notice
    assert notice.count("Blocked:") == 1


@pytest.mark.asyncio
async def test_hook_policy_deny_steers_the_same_way():
    # The governance ceiling (``deny_policy``) is still a host verdict, not a user
    # one: the mechanism label differs for the audit, the notice does not.
    provider = _Provider()
    hooks = _hooks(ToolHookResult.deny_policy("Blocked: governance ceiling"))
    ok = await _resolve_permission(provider, _event(), ToolApprovalPolicy.HOOK_BASED, hooks)
    assert ok is False
    _assert_steered_then_rejected(provider, "governance ceiling")


@pytest.mark.asyncio
async def test_read_only_unclassified_auto_approve_steers_before_rejecting():
    provider = _Provider()
    # An auto-approve WITHOUT the read_only tag under READ_ONLY is refused as
    # policy state -- no approver on this surface to hand it to.
    hooks = _hooks(ToolHookResult.auto_approve(read_only=False))
    ok = await _resolve_permission(provider, _event(), ToolApprovalPolicy.READ_ONLY, hooks)
    assert ok is False
    notice = _assert_steered_then_rejected(provider, "read-only", _SURFACE_CLAUSE)
    assert _POLICY_GUIDANCE not in notice


@pytest.mark.asyncio
async def test_headless_name_grant_refusal_steers_before_rejecting(monkeypatch):
    provider = _Provider()
    hooks = _hooks(ToolHookResult.auto_approve())

    async def _refuse(event):
        return Refusal(code="shadowed", detail="ls resolves through /tmp/bin")

    monkeypatch.setattr(llm_helpers.name_grant, "refusal_for_event", _refuse)
    ok = await _resolve_permission(provider, _event(), ToolApprovalPolicy.HOOK_BASED, hooks)
    assert ok is False
    # The withheld grant is about the command itself (its program name), so it
    # keeps the policy wording.
    _assert_steered_then_rejected(provider, "name", "safety policy")


@pytest.mark.asyncio
async def test_read_only_fallthrough_steers_before_rejecting():
    provider = _Provider()
    hooks = _hooks(ToolHookResult.allow())
    ok = await _resolve_permission(provider, _event(), ToolApprovalPolicy.READ_ONLY, hooks)
    assert ok is False
    notice = _assert_steered_then_rejected(provider, "read-only", _SURFACE_CLAUSE)
    assert _POLICY_GUIDANCE not in notice


@pytest.mark.asyncio
async def test_surface_policy_deny_never_hands_out_credential_remediation():
    """A REJECT_ALL surface must not answer a credential-shaped TITLE with the
    policy notice's class remediation ("run `aws configure list-profiles`"): no
    credential rule fired, and no tool can run there anyway -- that would be a
    second wall. The surface cause skips remediation entirely."""
    provider = _Provider()
    ok = await _resolve_permission(
        provider,
        _event(title="cat ~/.aws/credentials && cat ~/.ssh/id_rsa"),
        ToolApprovalPolicy.REJECT_ALL,
        None,
    )
    assert ok is False
    (notice,) = provider.steered
    assert "How to do this properly" not in notice
    assert "aws configure" not in notice
    assert _POLICY_GUIDANCE not in notice
    assert _SURFACE_CLAUSE in notice


# ── The one USER rejection stays bare ──────────────────────────────────────


@pytest.mark.asyncio
async def test_interactive_rejection_gets_no_notice():
    """When the person actually said no, kiro-cli's wording is the truth."""
    provider = _Provider()

    async def _say_no(event) -> bool:
        return False

    ok = await _resolve_permission(
        provider, _event(), ToolApprovalPolicy.HOOK_BASED, None, on_tool_approval=_say_no
    )
    assert ok is False
    assert provider.calls == ["reject"]
    assert provider.steered == []


@pytest.mark.asyncio
async def test_interactive_rejection_still_audits_before_the_wire(monkeypatch):
    audited: list[str] = []
    sel = MagicMock()
    sel.log_tool_invocation = MagicMock(
        side_effect=lambda **kw: audited.append(kw.get("outcome", ""))
    )
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel)

    class _StalledReject(_Provider):
        async def reject_tool(self, request_id) -> None:
            self.calls.append("reject")
            await asyncio.sleep(3600)

    async def _say_no(event) -> bool:
        return False

    provider = _StalledReject()
    task = asyncio.ensure_future(
        _resolve_permission(
            provider, _event(), ToolApprovalPolicy.HOOK_BASED, None, on_tool_approval=_say_no
        )
    )
    # The scan runs off-loop (asyncio.to_thread) before the callback, so give
    # the hop real time rather than a fixed number of loop turns.
    for _ in range(200):
        await asyncio.sleep(0.01)
        if provider.calls:
            break
    assert provider.calls == ["reject"]
    assert audited == ["rejected"], "the user's no reached the pipe before the audit trail"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ── Capability gate: no steer -> identical to today ────────────────────────


@pytest.mark.asyncio
async def test_backend_without_steer_only_rejects():
    provider = _Provider(supports_steer=False)
    ok = await _resolve_permission(provider, _event(), ToolApprovalPolicy.REJECT_ALL, None)
    assert ok is False
    assert provider.calls == ["reject"]


@pytest.mark.asyncio
async def test_backend_missing_the_attribute_only_rejects():
    provider = _NoSteerProvider()
    ok = await _resolve_permission(provider, _event(), ToolApprovalPolicy.REJECT_ALL, None)
    assert ok is False
    assert provider.calls == ["reject"]


@pytest.mark.asyncio
async def test_a_failing_steer_still_rejects():
    class _Raising(_Provider):
        async def steer(self, message: str) -> bool:
            self.calls.append("steer")
            raise RuntimeError("transport gone")

    provider = _Raising()
    ok = await _resolve_permission(provider, _event(), ToolApprovalPolicy.REJECT_ALL, None)
    assert ok is False
    assert provider.calls == ["steer", "reject"]


@pytest.mark.asyncio
async def test_agent_authored_text_is_redacted_before_it_reaches_the_model():
    provider = _Provider()
    secret = "AKIAIOSFODNN7EXAMPLE"
    hooks = _hooks(ToolHookResult.deny(f"Blocked: key {secret} in command"))
    await _resolve_permission(
        provider, _event(title=f"curl -H 'x: {secret}'"), ToolApprovalPolicy.HOOK_BASED, hooks
    )
    (notice,) = provider.steered
    assert secret not in notice


# ── Audit first: the SEL row survives a stalled pipe ───────────────────────


@pytest.mark.asyncio
async def test_audit_lands_before_the_steer_and_survives_cancellation(monkeypatch):
    """The steer is wire I/O too. A backend that stops reading stdin blocks it
    until the turn deadline cancels this coroutine; the decision must already be
    on the audit trail by then."""
    audited: list[str] = []
    sel = MagicMock()
    sel.log_tool_invocation = MagicMock(
        side_effect=lambda **kw: audited.append(kw.get("outcome", ""))
    )
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel)

    class _Stalled(_Provider):
        async def steer(self, message: str) -> bool:
            self.calls.append("steer")
            await asyncio.sleep(3600)
            return True

    provider = _Stalled()
    task = asyncio.ensure_future(
        _resolve_permission(provider, _event(), ToolApprovalPolicy.REJECT_ALL, None)
    )
    for _ in range(20):
        await asyncio.sleep(0)
        if provider.calls:
            break
    assert provider.calls == ["steer"], provider.calls
    assert audited == ["rejected"], "the decision reached the pipe before the audit trail"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The wire is still answered: a stranded permission request would block the
    # backend forever, so the cancelled steer schedules the reject itself.
    assert provider.calls == ["steer", "reject"], provider.calls


@pytest.mark.asyncio
async def test_cancelled_mid_steer_does_not_hang_on_a_stalled_reject(monkeypatch):
    """The pipe that stalled the steer is the pipe the reject drains into. The
    shielded wait is bounded so the caller's timeout stays a timeout instead of
    becoming a hang; the reject task stays referenced and keeps trying."""
    monkeypatch.setattr(llm_helpers, "STEER_NOTICE_BOUND_SECS", 0.05)

    class _StalledBoth(_Provider):
        async def steer(self, message: str) -> bool:
            self.calls.append("steer")
            await asyncio.sleep(3600)
            return True

        async def reject_tool(self, request_id) -> None:
            self.calls.append("reject-started")
            await asyncio.sleep(3600)

    provider = _StalledBoth()
    task = asyncio.ensure_future(
        _resolve_permission(provider, _event(), ToolApprovalPolicy.REJECT_ALL, None)
    )
    for _ in range(20):
        await asyncio.sleep(0)
        if provider.calls:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)
    assert provider.calls == ["steer", "reject-started"], provider.calls
    (orphan,) = [t for t in llm_helpers._orphan_rejects if not t.done()]
    orphan.cancel()
    with pytest.raises(asyncio.CancelledError):
        await orphan


@pytest.mark.asyncio
async def test_cancelled_mid_steer_still_answers_the_wire_for_the_bg_oneliner():
    class _Stalled(_Provider):
        async def steer(self, message: str) -> bool:
            self.calls.append("steer")
            await asyncio.sleep(3600)
            return True

        async def prompt(self, message: str):
            yield _event(title="bash")
            yield LLMEvent(kind=EVENT_COMPLETE, text="")

        async def destroy(self) -> None:
            self.calls.append("destroy")

    class _Sessions:
        def __init__(self, session) -> None:
            self._session = session

        async def get_bg_session(self):
            return self._session

    session = _Stalled()
    with pytest.raises(asyncio.TimeoutError):
        await llm_helpers.run_bg_oneliner(_Sessions(session), "p", timeout=0.05)
    assert session.calls[:2] == ["steer", "reject"], session.calls


# ── Background one-liner: the tool-free contract ───────────────────────────


@pytest.mark.asyncio
async def test_bg_oneliner_steers_before_rejecting():
    class _Session(_Provider):
        async def prompt(self, message: str):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield _event(title="bash")
            yield LLMEvent(kind=EVENT_COMPLETE, text="")

        async def destroy(self) -> None:
            pass

    class _Sessions:
        def __init__(self, session) -> None:
            self._session = session

        async def get_bg_session(self):
            return self._session

    session = _Session()
    text = await llm_helpers.run_bg_oneliner(_Sessions(session), "summarise", sel_source="test")
    assert text == "ok"
    notice = _assert_steered_then_rejected(session, "bash", "tool-free", _SURFACE_CLAUSE)
    assert _POLICY_GUIDANCE not in notice


# ── Source-level guard ─────────────────────────────────────────────────────


class TestEveryHostDenyInLlmHelpersSteersFirst:
    """Coverage checkable from the source, not asserted in a PR body.

    A coverage claim in prose is unverifiable; a test that walks the file is
    what turns "every host deny steers" into a property a future call site
    cannot silently break.
    """

    MODULE = pathlib.Path(__file__).resolve().parents[1] / "src/kiro_crew/llm_helpers.py"
    REJECT = re.compile(r"^\s*await \w+\.reject_tool\(")
    STEER = re.compile(r"^\s*await _steer_host_deny\(")
    AUDIT = re.compile(r"^\s*(_log\(|_sel\(\)\.log_tool_invocation\()")
    #: Lines a steer may sit above its reject. The steer is the statement
    #: IMMEDIATELY before the reject at every covered site; the slack only
    #: absorbs black wrapping the steer's arguments.
    WINDOW = 8
    #: Lines the audit call may sit above its reject: the audit opens first and
    #: its metadata dict plus the wrapped steer can span a dozen lines.
    AUDIT_WINDOW = 20
    #: The genuine USER rejections in the module, identified by the audit
    #: reason next to the reject. Exactly one today: the interactive callback
    #: answered no. A notice there would tell the model the person did not
    #: refuse when they did.
    USER_REJECTIONS = ("interactive_rejected",)

    def _lines(self) -> list[str]:
        return self.MODULE.read_text(encoding="utf-8").splitlines()

    def _reject_sites(self) -> list[int]:
        return [i for i, line in enumerate(self._lines()) if self.REJECT.match(line)]

    def _is_user_rejection(self, lines: list[str], i: int) -> bool:
        # The audit line now precedes the reject, so look a few lines either way.
        span = "\n".join(lines[max(0, i - 3) : i + 3])
        return any(marker in span for marker in self.USER_REJECTIONS)

    def _last_match(self, pattern: re.Pattern[str], lines: list[str], lo: int, hi: int) -> int:
        hits = [j for j in range(lo, hi) if pattern.match(lines[j])]
        return hits[-1] if hits else -1

    def test_the_scan_finds_every_reject_the_source_contains(self):
        # Cross-checked against an independent textual count so a call shape the
        # walker's regex misses cannot drop a site out of the coverage assertion.
        import inspect

        src = self.MODULE.read_text(encoding="utf-8")
        # The one non-awaited spelling is the cancellation fallback INSIDE the
        # steer helper -- it answers the wire for a site whose own reject the
        # cancellation skipped, so it is not a deny site of its own. Pin it to
        # that helper and exclude exactly it.
        scheduled = "ensure_future(provider.reject_tool("
        assert src.count(scheduled) == 1
        assert scheduled in inspect.getsource(llm_helpers._steer_host_deny)
        textual = src.count(".reject_tool(") - 1
        found = len(self._reject_sites())
        assert textual >= 10, textual
        assert found == textual, (found, textual)

    def test_exactly_one_user_rejection_is_allowlisted(self):
        lines = self._lines()
        user = [i + 1 for i in self._reject_sites() if self._is_user_rejection(lines, i)]
        assert len(user) == 1, (
            "the allowlist must name each USER rejection individually -- a new one "
            f"needs its own per-site judgement, not a wider marker: lines {user}"
        )

    def test_every_host_deny_is_preceded_by_the_shared_steer(self):
        lines = self._lines()
        bare: list[int] = []
        for i in self._reject_sites():
            if self._is_user_rejection(lines, i):
                continue
            if self._last_match(self.STEER, lines, max(0, i - self.WINDOW), i) < 0:
                bare.append(i + 1)
        assert not bare, (
            "these host denies hand the model kiro-cli's generic 'user denied' with "
            f"nothing to correct it -- await _steer_host_deny(...) first: lines {bare}"
        )

    def test_every_reject_audits_before_the_wire(self):
        # Every reject, the user rejection included: the steer exemption is about
        # what the model is told, the audit rule is about what the SEL trail
        # keeps, and a cancellation inside the reject must find the row written.
        lines = self._lines()
        late: list[int] = []
        previous = -1
        for i in self._reject_sites():
            # Never borrow the PREVIOUS site's audit: the search floor is the
            # line after the last reject, so two adjacent sites cannot share one.
            floor = max(0, i - self.AUDIT_WINDOW, previous + 1)
            steer = self._last_match(self.STEER, lines, max(0, i - self.WINDOW), i)
            audit = self._last_match(self.AUDIT, lines, floor, i)
            if audit < 0 or (steer >= 0 and not audit < steer):
                late.append(i + 1)
            previous = i
        assert not late, (
            "the SEL row must be written before the steer and the reject, or a "
            f"stalled pipe cancels the coroutine with the decision unaudited: lines {late}"
        )

    def test_the_user_rejection_is_not_steered(self):
        lines = self._lines()
        for i in self._reject_sites():
            if not self._is_user_rejection(lines, i):
                continue
            window = lines[max(0, i - self.WINDOW) : i]
            assert not any(
                self.STEER.match(line) for line in window
            ), f"line {i + 1}: a genuine user rejection must not claim it was not one"

    def test_the_local_helper_delegates_to_the_shared_one(self):
        # One spelling of the notice for the whole codebase (see
        # test_approval_timeout_cause.TestTheBoundedSteerSpellings): the module's
        # helper may only redact and forward, never build or send on its own.
        import inspect

        src = inspect.getsource(llm_helpers._steer_host_deny)
        assert "steer_refusal_notice(" in src
        assert "cause=cause" in src
        assert ".steer(" not in src

    def test_every_steer_site_names_its_cause_explicitly(self):
        # The cause is a required keyword: a site that forgets it fails at call
        # time, and this pins that no default creeps back in.
        import inspect

        sig = inspect.signature(llm_helpers._steer_host_deny)
        assert sig.parameters["cause"].default is inspect.Parameter.empty
        lines = self._lines()
        unnamed: list[int] = []
        for i, line in enumerate(lines):
            if not self.STEER.match(line):
                continue
            block = "\n".join(lines[i : i + self.WINDOW])
            if "cause=DENY_CAUSE_" not in block:
                unnamed.append(i + 1)
        assert not unnamed, unnamed
