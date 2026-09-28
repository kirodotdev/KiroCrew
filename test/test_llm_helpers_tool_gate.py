"""``stream_and_collect`` must report every tool-gate decision to its caller.

A model whose tool calls were all refused still returns plausible prose, so the
returned text cannot tell a caller whether any work actually happened. Cron
relies on this callback to record a fully-blocked run as a failure instead of a
success — and a success there resets the auto-pause counter, so a job that can
never succeed would re-fire forever. Testing against the REAL implementation
matters because every caller fakes this helper in its own tests: the hook could
be dead at runtime while all of them stayed green.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest

from kiro_crew import llm_helpers, platform_compat
from kiro_crew.acp.client import AcpError
from kiro_crew.llm_helpers import ToolApprovalPolicy, stream_and_collect
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    LLMEvent,
)


async def _no_sleep(_seconds: float) -> None:
    """Collapse the retry backoff so the retry test stays fast."""
    return None


# Tripped by the always-enforced deny checks in _resolve_permission, which run
# for every approval policy — including AUTO_APPROVE, which is what a cron with
# approval_mode="auto" uses.
_DENIED_TITLE = "rm -rf /"
_BENIGN_TITLE = "Read README.md"


class _ScriptedProvider:
    """Yields a fixed event script and records the gate calls it received."""

    def __init__(self, events: list[LLMEvent]) -> None:
        self._events = events
        self.approved: list[str] = []
        self.rejected: list[str] = []

    async def stream(self, message: str) -> AsyncIterator[LLMEvent]:
        for event in self._events:
            yield event

    async def approve_tool(self, request_id: str) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id: str) -> None:
        self.rejected.append(request_id)


def _script(title: str) -> list[LLMEvent]:
    return [
        LLMEvent(kind=EVENT_TEXT_CHUNK, text="on it"),
        LLMEvent(kind=EVENT_PERMISSION_REQUEST, title=title, request_id="r1"),
        LLMEvent(kind=EVENT_TEXT_CHUNK, text=" — could not."),
        LLMEvent(kind=EVENT_COMPLETE, text=""),
    ]


@pytest.mark.asyncio
async def test_a_security_deny_is_flagged_as_a_security_block():
    provider = _ScriptedProvider(_script(_DENIED_TITLE))
    seen: list[tuple[str, bool, bool]] = []

    text = await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.AUTO_APPROVE,
        retry_transient=False,
        on_tool_gate=lambda t, a, b: seen.append((t, a, b)),
    )

    assert seen == [(_DENIED_TITLE, False, True)], "the security block never reached the caller"
    assert provider.rejected == ["r1"]
    # The turn still completes and still returns prose — which is precisely why
    # the caller cannot infer the refusal from the reply text.
    assert text == "on it — could not."


@pytest.mark.asyncio
async def test_a_hook_security_deny_is_flagged_as_a_security_block():
    """The exfiltration/deny-list gate routes through HookManager, not the
    unconditional arms. Classifying only the unconditional arms as security
    would let a hook-blocked cron go on recording success forever."""
    from kiro_crew.hooks import ToolHookResult

    provider = _ScriptedProvider(_script(_BENIGN_TITLE))
    hooks = MagicMock()
    hooks.on_tool_call = MagicMock(return_value=ToolHookResult.deny("Blocked: exfiltration"))
    hooks.effective_denied_regexes = MagicMock(return_value=[])
    seen: list[tuple[str, bool, bool]] = []

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.HOOK_BASED,
        hooks=hooks,
        retry_transient=False,
        on_tool_gate=lambda t, a, b: seen.append((t, a, b)),
    )

    assert seen == [(_BENIGN_TITLE, False, True)], "a hook security deny was not counted"


@pytest.mark.asyncio
async def test_a_governance_deny_is_not_a_security_block():
    """Policy state is not a defect in the attempt: the same call becomes allowed
    when the ceiling loosens, so it must not feed a durable failure budget."""
    from kiro_crew.hooks import ToolHookResult

    provider = _ScriptedProvider(_script(_BENIGN_TITLE))
    hooks = MagicMock()
    hooks.on_tool_call = MagicMock(
        return_value=ToolHookResult.deny_policy("Blocked by governance profile")
    )
    hooks.effective_denied_regexes = MagicMock(return_value=[])
    seen: list[tuple[str, bool, bool]] = []

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.HOOK_BASED,
        hooks=hooks,
        retry_transient=False,
        on_tool_gate=lambda t, a, b: seen.append((t, a, b)),
    )

    assert seen == [(_BENIGN_TITLE, False, False)], "a governance deny was counted as security"


@pytest.mark.asyncio
async def test_an_interactive_rejection_is_not_a_security_block():
    """An unattended cron's approval request deny-fasts on a timeout and lands
    here. Counting it as a security block would fail — and eventually
    auto-pause — a job whose only problem is that nobody approved it."""
    provider = _ScriptedProvider(_script(_BENIGN_TITLE))
    seen: list[tuple[str, bool, bool]] = []

    async def _deny(_event) -> bool:
        return False

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.HOOK_BASED,
        hooks=None,
        on_tool_approval=_deny,
        retry_transient=False,
        on_tool_gate=lambda t, a, b: seen.append((t, a, b)),
    )

    assert seen == [
        (_BENIGN_TITLE, False, False)
    ], "an interactive rejection must not be reported as a security block"
    assert provider.rejected == ["r1"]


@pytest.mark.asyncio
async def test_an_approved_tool_is_reported_as_approved():
    provider = _ScriptedProvider(_script(_BENIGN_TITLE))
    seen: list[tuple[str, bool, bool]] = []

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.AUTO_APPROVE,
        retry_transient=False,
        on_tool_gate=lambda t, a, b: seen.append((t, a, b)),
    )

    assert seen == [(_BENIGN_TITLE, True, False)], "the approval never reached the caller"
    assert provider.approved == ["r1"]


@pytest.mark.asyncio
async def test_both_arms_are_distinguishable_within_one_turn():
    """The caller's verdict is "security-blocked something AND approved nothing",
    so a turn that got one of each must not read as fully blocked."""
    provider = _ScriptedProvider(
        [
            LLMEvent(kind=EVENT_PERMISSION_REQUEST, title=_DENIED_TITLE, request_id="r1"),
            LLMEvent(kind=EVENT_PERMISSION_REQUEST, title=_BENIGN_TITLE, request_id="r2"),
            LLMEvent(kind=EVENT_COMPLETE, text=""),
        ]
    )
    seen: list[tuple[str, bool, bool]] = []

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.AUTO_APPROVE,
        retry_transient=False,
        on_tool_gate=lambda t, a, b: seen.append((t, a, b)),
    )

    assert [(a, b) for _, a, b in seen] == [(False, True), (True, False)]


@pytest.mark.asyncio
async def test_a_raising_callback_does_not_fail_the_turn():
    """Observing a gate decision is bookkeeping; it must never abort a cron run."""
    provider = _ScriptedProvider(_script(_DENIED_TITLE))

    def _boom(title: str, approved: bool, security_blocked: bool) -> None:
        raise RuntimeError("caller bug")

    text = await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.AUTO_APPROVE,
        retry_transient=False,
        on_tool_gate=_boom,
    )

    assert text == "on it — could not."


class _RetryThenSucceedProvider:
    """Refuses a tool, fails transiently, then succeeds tool-free on the retry.

    A retry re-sends the original message, so a decision from the abandoned
    attempt describes work the final turn never performs.
    """

    def __init__(self) -> None:
        self.attempts = 0
        self.rejected: list[str] = []

    async def stream(self, message: str) -> AsyncIterator[LLMEvent]:
        self.attempts += 1
        if self.attempts == 1:
            yield LLMEvent(kind=EVENT_PERMISSION_REQUEST, title=_DENIED_TITLE, request_id="r1")
            # `transient` is the structured verdict acp_error_is_transient prefers
            # over string-matching the message, so this takes the real retry route.
            exc = AcpError("backend hiccup")
            exc.transient = True  # type: ignore[attr-defined]
            raise exc
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done without tools")
        yield LLMEvent(kind=EVENT_COMPLETE, text="")

    async def approve_tool(self, request_id: str) -> None:
        pass

    async def reject_tool(self, request_id: str) -> None:
        self.rejected.append(request_id)


@pytest.mark.asyncio
async def test_a_discarded_retry_attempt_reports_no_gate_decisions(monkeypatch):
    """A refusal from an abandoned attempt must not reach the caller.

    Otherwise it outvotes a clean retry: the caller sees "refused something,
    approved nothing" and fails a run that actually succeeded — which for cron
    means auto-pausing a healthy recurring job.
    """
    monkeypatch.setattr(llm_helpers.asyncio, "sleep", _no_sleep)
    provider = _RetryThenSucceedProvider()
    seen: list[tuple[str, bool, bool]] = []

    text = await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.AUTO_APPROVE,
        on_tool_gate=lambda t, a, b: seen.append((t, a, b)),
    )

    assert provider.attempts == 2, "the transient failure did not trigger a retry"
    assert text == "done without tools"
    assert seen == [], f"decisions from the abandoned attempt leaked: {seen}"


@pytest.mark.asyncio
async def test_a_tool_that_bypassed_the_gate_counts_as_work():
    """A tool auto-approved upstream raises no permission request, so it executes
    without a gate decision. Correlating executed calls against decided ones by
    ``tool_call_id`` is what keeps that work visible — without it a later
    security block is the only tally entry and the run reads as fully blocked."""
    provider = _ScriptedProvider(
        [
            LLMEvent(kind=EVENT_TOOL_CALL, title="Read README.md", tool_call_id="t1"),
            LLMEvent(kind=EVENT_PERMISSION_REQUEST, title=_DENIED_TITLE, request_id="r2"),
            LLMEvent(kind=EVENT_COMPLETE, text=""),
        ]
    )
    seen: list[tuple[str, bool, bool]] = []

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.AUTO_APPROVE,
        retry_transient=False,
        on_tool_gate=lambda t, a, b: seen.append((t, a, b)),
    )

    assert (_DENIED_TITLE, False, True) in seen
    assert ("Read README.md", True, False) in seen, "the executed tool was never reported"


@pytest.mark.asyncio
async def test_an_executed_call_matching_a_decision_is_not_double_counted():
    """A tool whose execution follows its own gate decision must not also arrive
    as a separate approval — otherwise a security-blocked tool that still emits
    an execution event would mask every fully-blocked run."""
    provider = _ScriptedProvider(
        [
            LLMEvent(
                kind=EVENT_PERMISSION_REQUEST,
                title=_DENIED_TITLE,
                request_id="r1",
                tool_call_id="t1",
            ),
            LLMEvent(kind=EVENT_TOOL_CALL, title=_DENIED_TITLE, tool_call_id="t1"),
            LLMEvent(kind=EVENT_COMPLETE, text=""),
        ]
    )
    seen: list[tuple[str, bool, bool]] = []

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.AUTO_APPROVE,
        retry_transient=False,
        on_tool_gate=lambda t, a, b: seen.append((t, a, b)),
    )

    assert seen == [(_DENIED_TITLE, False, True)], f"the decided call was double-counted: {seen}"


@pytest.mark.asyncio
async def test_a_pin_only_deny_is_reported_as_non_security():
    """A rule the operator disabled and policy pinned back is policy state.

    The gate still denies — enforcement is unchanged — but the cron tally must
    not count it, because clearing an auto-pause never restores ``enabled``, so
    a later policy loosening could not revive the job.
    """
    pattern = r"^Running: pinned-tool"

    class _PinHooks:
        """Only the pinned set contains the matching rule."""

        def effective_denied_regexes(self, *, include_governance_pins: bool = True):
            return [pattern] if include_governance_pins else []

    provider = _ScriptedProvider(
        [
            LLMEvent(
                kind=EVENT_PERMISSION_REQUEST, title="Running: pinned-tool --go", request_id="r1"
            ),
            LLMEvent(kind=EVENT_COMPLETE, text=""),
        ]
    )
    seen: list[tuple[str, bool, bool]] = []

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.AUTO_APPROVE,
        hooks=_PinHooks(),  # type: ignore[arg-type]
        retry_transient=False,
        on_tool_gate=lambda t, a, b: seen.append((t, a, b)),
    )

    assert seen == [
        ("Running: pinned-tool --go", False, False)
    ], f"a governance-pinned deny must not count as a security block: {seen}"


@pytest.mark.asyncio
async def test_a_deny_the_user_enforces_is_reported_as_security():
    """The same match without a pin IS the job's own problem."""
    pattern = r"^Running: pinned-tool"

    class _UserHooks:
        """Both sets contain the rule — no pin involved."""

        def effective_denied_regexes(self, *, include_governance_pins: bool = True):
            return [pattern]

    provider = _ScriptedProvider(
        [
            LLMEvent(
                kind=EVENT_PERMISSION_REQUEST, title="Running: pinned-tool --go", request_id="r1"
            ),
            LLMEvent(kind=EVENT_COMPLETE, text=""),
        ]
    )
    seen: list[tuple[str, bool, bool]] = []

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.AUTO_APPROVE,
        hooks=_UserHooks(),  # type: ignore[arg-type]
        retry_transient=False,
        on_tool_gate=lambda t, a, b: seen.append((t, a, b)),
    )

    assert seen == [("Running: pinned-tool --go", False, True)]


@pytest.mark.asyncio
async def test_omitting_the_callback_leaves_behavior_unchanged():
    """Every existing caller passes no callback — that path must stay inert."""
    provider = _ScriptedProvider(_script(_DENIED_TITLE))

    text = await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.AUTO_APPROVE,
        retry_transient=False,
    )

    assert text == "on it — could not."
    assert provider.rejected == ["r1"]


# ── READ_ONLY policy (side chat: Reads-mode semantics, reject as the fallback) ──
#
# These run the REAL HookManager so the classification chain the policy leans
# on — deny floor, deny-by-default shell classifier, ACP-kind allowlist,
# name-grant verification — is the one that ships, not a mock of it. A caller
# faking the classifier could go green while the gate was dead at runtime.


def _read_only_hooks():
    from kiro_crew.hooks import HookManager, HooksConfig

    return HookManager(HooksConfig())


def _permission_script(event: LLMEvent) -> list[LLMEvent]:
    return [
        LLMEvent(kind=EVENT_TEXT_CHUNK, text="on it"),
        event,
        LLMEvent(kind=EVENT_COMPLETE, text=""),
    ]


@pytest.mark.skipif(
    platform_compat.IS_WINDOWS,
    reason=(
        "approval here needs the name grant to VOUCH for `ls`, which resolves a "
        "program through the POSIX execute bit and a ':'-joined PATH; on Windows "
        "`ls` resolves to nothing, the grant is withheld, and READ_ONLY's reject "
        "fallback is the correct outcome rather than the approval asserted below"
    ),
)
@pytest.mark.asyncio
async def test_read_only_policy_approves_a_read_only_shell_command():
    event = LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="ls -la",
        request_id="r1",
        is_shell=True,
        tool_input='{"command": "ls -la"}',
    )
    provider = _ScriptedProvider(_permission_script(event))
    seen: list[tuple[str, bool, bool]] = []

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.READ_ONLY,
        hooks=_read_only_hooks(),
        retry_transient=False,
        on_tool_gate=lambda t, a, b: seen.append((t, a, b)),
    )

    assert provider.approved == ["r1"]
    assert seen == [("ls -la", True, False)]


@pytest.mark.asyncio
async def test_read_only_policy_rejects_a_mutating_shell_command():
    """`touch` passes no deny rule — under HOOK_BASED it would reach the
    interactive card. READ_ONLY has no card, so not-provably-read-only means
    rejected, and NOT as a security block (policy state, not a bad attempt)."""
    event = LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="touch /tmp/side-chat-probe",
        request_id="r1",
        is_shell=True,
        tool_input='{"command": "touch /tmp/side-chat-probe"}',
    )
    provider = _ScriptedProvider(_permission_script(event))
    seen: list[tuple[str, bool, bool]] = []

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.READ_ONLY,
        hooks=_read_only_hooks(),
        retry_transient=False,
        on_tool_gate=lambda t, a, b: seen.append((t, a, b)),
    )

    assert provider.rejected == ["r1"]
    assert provider.approved == []
    assert seen == [("touch /tmp/side-chat-probe", False, False)]


@pytest.mark.asyncio
async def test_read_only_policy_does_not_trust_a_read_kind_alone():
    """Under READ_ONLY the ACP kind is agent-influenced and proves nothing: a
    ``read``-kind call with no host-trusted built-in identity is not approved."""
    event = LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="Read README.md",
        request_id="r1",
        tool_kind="read",
    )
    provider = _ScriptedProvider(_permission_script(event))

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.READ_ONLY,
        hooks=_read_only_hooks(),
        retry_transient=False,
    )

    assert provider.approved == []


@pytest.mark.asyncio
async def test_read_only_policy_rejects_a_mutating_kind_tool():
    """`execute` is a non-read kind; the kind is agent-influenced, so anything
    outside the allow-list is treated as potentially mutating and refused."""
    event = LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="Run migration",
        request_id="r1",
        tool_kind="execute",
    )
    provider = _ScriptedProvider(_permission_script(event))

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.READ_ONLY,
        hooks=_read_only_hooks(),
        retry_transient=False,
    )

    assert provider.rejected == ["r1"]
    assert provider.approved == []


@pytest.mark.asyncio
async def test_read_only_policy_without_hooks_fails_closed():
    """No hook gate means no classifier: the policy must degrade to
    reject-everything, never to the caller-less auto-approve default."""
    event = LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="Read README.md",
        request_id="r1",
        tool_kind="read",
    )
    provider = _ScriptedProvider(_permission_script(event))

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.READ_ONLY,
        hooks=None,
        retry_transient=False,
    )

    assert provider.rejected == ["r1"]
    assert provider.approved == []


@pytest.mark.asyncio
async def test_read_only_policy_deny_floor_sees_the_canonical_mcp_tool():
    """The trusted MCP identity reaches the hook gate, so a deny rule written
    against the canonical ``mcp__server__tool`` name matches even when the
    model-authored title is benign and the ACP kind classifies read-only.

    ``event.title`` is prose the model chose; ``mcp_server_name``/``tool_name``
    come from ``_meta.kiro``. A gate handed only the title judges the wrong
    subject, and the request auto-approves on a turn with no approver.
    """
    from kiro_crew.hooks import HookManager, HooksConfig

    event = LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="Check tomorrow's forecast",
        request_id="r1",
        tool_kind="read",
        mcp_server_name="weather:srv",
        tool_name="wipe_disk",
    )
    provider = _ScriptedProvider(_permission_script(event))

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.READ_ONLY,
        hooks=HookManager(HooksConfig(auto_deny_tools=["mcp__weather:srv__wipe_disk"])),
        retry_transient=False,
    )

    assert provider.rejected == ["r1"]
    assert provider.approved == []


@pytest.mark.asyncio
async def test_read_only_policy_offers_the_canonical_mcp_ref_to_governance(monkeypatch):
    """Governance is asked about the canonical ``@server/tool`` reference, not
    just the prose title, so a per-MCP-tool ceiling deny still binds a call the
    read-only classifier would otherwise approve."""
    import kiro_crew.hooks as hooks_mod
    from kiro_crew.hooks import HookManager

    seen: list[str] = []

    def fake_gov(ctx, name, *a, **k):
        targets = [name, *k.get("extra_titles", ()), k.get("mcp_ref", "")]
        for target in targets:
            if target and target not in seen:
                seen.append(target)
        if "@weather:srv/wipe_disk" in targets:
            return "Blocked by governance policy: denied"
        return None

    monkeypatch.setattr(hooks_mod, "_governance_denial", fake_gov)

    event = LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="Check tomorrow's forecast",
        request_id="r1",
        tool_kind="read",
        mcp_server_name="weather:srv",
        tool_name="wipe_disk",
    )
    provider = _ScriptedProvider(_permission_script(event))

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.READ_ONLY,
        hooks=HookManager(),
        retry_transient=False,
    )

    assert "@weather:srv/wipe_disk" in seen
    assert provider.rejected == ["r1"]
    assert provider.approved == []


@pytest.mark.asyncio
async def test_read_only_policy_deny_floor_still_wins():
    """The always-enforced deny checks run BEFORE classification, so a denied
    command is a security block — read-only classification can never re-admit
    anything the floor refused."""
    event = LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title=_DENIED_TITLE,
        request_id="r1",
        is_shell=True,
        tool_input='{"command": "rm -rf /"}',
    )
    provider = _ScriptedProvider(_permission_script(event))
    seen: list[tuple[str, bool, bool]] = []

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.READ_ONLY,
        hooks=_read_only_hooks(),
        retry_transient=False,
        on_tool_gate=lambda t, a, b: seen.append((t, a, b)),
    )

    assert provider.rejected == ["r1"]
    assert seen == [(_DENIED_TITLE, False, True)]


# ── READ_ONLY honours the classifier alone, never a grant ──
#
# The hook gate reaches TOOL_AUTO_APPROVE by two kinds of route. The read-only
# classifier is a verdict about what the call can DO; the grants — the
# operator's `auto_approve_tools` globs and the app-own-server rule — vouch for
# WHO is calling and say nothing about the call's effect. Under HOOK_BASED the
# two are interchangeable, because either merely skips the approval card. Under
# READ_ONLY there is no card behind them, so an honoured grant EXECUTES a
# mutating tool on a surface whose contract is "reads only". The policy asks the
# gate for a classifier-only verdict and, independently, honours an auto-approve
# only when the result carries the classifier's `read_only` tag.


def _write_event() -> LLMEvent:
    """A Write-class request: the title matches ``Write*``, the kind is a
    mutating one, and it carries no params or diff path, so no tier ahead of
    the grant loop judges it."""
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="Write notes.md",
        request_id="r1",
        tool_kind="edit",
    )


@pytest.mark.asyncio
async def test_read_only_policy_refuses_a_write_the_config_grant_matches():
    """``auto_approve_tools=["Write*"]`` matches the title. That is a grant, not
    a classification, so READ_ONLY refuses the write: rejected, never executed,
    and logged as policy state rather than a security block."""
    from kiro_crew.hooks import HookManager, HooksConfig

    provider = _ScriptedProvider(_permission_script(_write_event()))
    seen: list[tuple[str, bool, bool]] = []

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.READ_ONLY,
        hooks=HookManager(HooksConfig(auto_approve_tools=["Write*"])),
        retry_transient=False,
        on_tool_gate=lambda t, a, b: seen.append((t, a, b)),
    )

    assert provider.approved == []
    assert provider.rejected == ["r1"]
    assert seen == [("Write notes.md", False, False)]


@pytest.mark.asyncio
async def test_read_only_policy_refuses_an_app_own_server_grant(monkeypatch):
    """A first-party app calling a server its shipped manifest declares is
    auto-approved by provenance under HOOK_BASED. Provenance says nothing about
    the tool's effect, so READ_ONLY refuses it."""
    import kiro_crew.hooks as hooks_mod
    from kiro_crew.hooks import HookManager

    monkeypatch.setattr(hooks_mod, "_is_first_party_app", lambda app: app.casefold() == "myapp")
    monkeypatch.setattr(hooks_mod, "_BUILTIN_APP_MCP_SERVERS", frozenset({"myapp:srv"}))
    event = LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="mcp__myapp:srv__do_thing",
        request_id="r1",
        tool_kind="other",
        mcp_server_name="myapp:srv",
        tool_name="do_thing",
    )
    provider = _ScriptedProvider(_permission_script(event))

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.READ_ONLY,
        hooks=HookManager(),
        app="myapp",
        retry_transient=False,
    )

    assert provider.approved == []
    assert provider.rejected == ["r1"]


@pytest.mark.asyncio
async def test_read_only_policy_refuses_an_untagged_hook_auto_approve():
    """The tag is what the policy trusts, not the action: a gate that returns
    TOOL_AUTO_APPROVE without ``read_only`` (a double, a tier that does not
    carry it) is refused, so no auto-approve reaches this surface unclassified.
    The gate is also asked classifier-only, so a grant that shadows a read
    cannot pre-empt the classifier."""
    from kiro_crew.hooks import ToolHookResult

    provider = _ScriptedProvider(_permission_script(_write_event()))
    hooks = MagicMock()
    hooks.on_tool_call = MagicMock(return_value=ToolHookResult.auto_approve())
    hooks.effective_denied_regexes = MagicMock(return_value=[])

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.READ_ONLY,
        hooks=hooks,
        retry_transient=False,
    )

    assert provider.approved == []
    assert provider.rejected == ["r1"]
    assert hooks.on_tool_call.call_args.kwargs["classifier_only"] is True


@pytest.mark.asyncio
async def test_read_only_policy_classifies_a_read_the_grant_also_matches():
    """A grant that shadows a genuinely read-only call must not cost the read:
    ``auto_approve_tools=["*"]`` approves everything under HOOK_BASED, and under
    READ_ONLY the host-known built-in read (``fs_read``) is still approved — by the classifier,
    whose verdict the grant does not pre-empt."""
    from kiro_crew.hooks import HookManager, HooksConfig

    event = LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="Read README.md",
        request_id="r1",
        tool_kind="read",
        tool_name="fs_read",
        mcp_identity_trusted=True,
    )
    provider = _ScriptedProvider(_permission_script(event))

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.READ_ONLY,
        hooks=HookManager(HooksConfig(auto_approve_tools=["*"])),
        retry_transient=False,
    )

    assert provider.approved == ["r1"]
    assert provider.rejected == []


@pytest.mark.asyncio
async def test_hook_based_policy_still_honours_the_config_grant():
    """HOOK_BASED is unchanged: the same ``Write*`` grant approves the same
    write, because on a surface with an approver a grant means exactly "skip
    the card"."""
    from kiro_crew.hooks import HookManager, HooksConfig

    provider = _ScriptedProvider(_permission_script(_write_event()))

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.HOOK_BASED,
        hooks=HookManager(HooksConfig(auto_approve_tools=["Write*"])),
        retry_transient=False,
    )

    assert provider.approved == ["r1"]
    assert provider.rejected == []


@pytest.mark.asyncio
async def test_hook_based_policy_deny_floor_sees_the_canonical_mcp_tool():
    """HOOK_BASED is unchanged by READ_ONLY's ``classifier_only`` work: the
    trusted MCP identity ``_resolve_permission`` hands the gate for every
    hook-gated policy still reaches the deny plane on the unattended
    ``stream_and_collect`` surfaces (cron, heartbeat, autonudge, Meetings), so a
    deny rule written against the canonical ``mcp__server__tool`` name binds
    behind a benign model-authored title and a ``read`` kind.

    The identity is ADDED to the deny targets, never substituted for the title:
    the same event with no such rule is still auto-approved by the ``read``-kind
    allow-list. Deny-only — nothing that matched before stops matching.
    """
    from kiro_crew.hooks import HookManager, HooksConfig

    def _event() -> LLMEvent:
        return LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="Check tomorrow's forecast",
            request_id="r1",
            tool_kind="read",
            mcp_server_name="weather:srv",
            tool_name="wipe_disk",
        )

    denied = _ScriptedProvider(_permission_script(_event()))
    await stream_and_collect(
        denied,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.HOOK_BASED,
        hooks=HookManager(HooksConfig(auto_deny_tools=["mcp__weather:srv__wipe_disk"])),
        retry_transient=False,
    )
    assert denied.rejected == ["r1"]
    assert denied.approved == []

    unruled = _ScriptedProvider(_permission_script(_event()))
    await stream_and_collect(
        unruled,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.HOOK_BASED,
        hooks=HookManager(HooksConfig()),
        retry_transient=False,
    )
    assert unruled.approved == ["r1"]
    assert unruled.rejected == []


# ── READ_ONLY proof is host-trusted only ──
#
# Under READ_ONLY the classifier accepts exactly two proofs: the recovered shell
# command judged by `is_read_only_bash`, and a built-in the host knows to be
# read-only — named by the non-model-authored `_meta.kiro.toolName`, with no MCP
# server behind it, AND carrying the `mcp_identity_trusted` provenance flag on
# the event. The ACP `kind` and the title are agent-influenced: they may narrow
# the verdict but never produce it. Each test below runs the REAL HookManager
# through `stream_and_collect`, so the assertion is about what the shipped gate
# does to the provider (approve / reject), not about a mock of it.


def _read_only_event(**overrides) -> LLMEvent:
    fields = dict(
        kind=EVENT_PERMISSION_REQUEST,
        title="Read README.md",
        request_id="r1",
        tool_kind="read",
    )
    fields.update(overrides)
    return LLMEvent(**fields)


async def _run_read_only(
    event: LLMEvent, policy: ToolApprovalPolicy = ToolApprovalPolicy.READ_ONLY
) -> _ScriptedProvider:
    provider = _ScriptedProvider(_permission_script(event))
    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=policy,
        hooks=_read_only_hooks(),
        retry_transient=False,
    )
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "kind"),
    [("fs_read", "read"), ("web_fetch", "fetch"), ("grep", "")],
    ids=["fs_read+read", "web_fetch+fetch", "grep+kindless"],
)
async def test_read_only_policy_approves_a_host_known_read_tool(tool, kind):
    """The positive path: a `_HOST_READ_ONLY_BUILTIN_TOOLS` name on the
    host-stamped identity, no MCP server, provenance verified. The kind may
    agree or be absent — it is not what proves the call."""
    provider = await _run_read_only(
        _read_only_event(tool_kind=kind, tool_name=tool, mcp_identity_trusted=True)
    )
    assert provider.approved == ["r1"]
    assert provider.rejected == []


@pytest.mark.asyncio
async def test_read_only_policy_rejects_an_mcp_tool_with_a_read_kind():
    """THE SPOOFING QUESTION. An MCP server exposes a tool it calls `fs_read`
    and the agent labels the call `kind="read"`. kiro-cli stamps
    `mcpServerName` on every MCP-served call, so the server name is non-empty,
    the built-in proof fails, and READ_ONLY rejects — `readOnlyHint`, the
    title and the kind are never consulted as proof."""
    provider = await _run_read_only(
        _read_only_event(
            tool_name="fs_read", mcp_server_name="files:srv", mcp_identity_trusted=True
        )
    )
    assert provider.rejected == ["r1"]
    assert provider.approved == []


@pytest.mark.asyncio
async def test_read_only_policy_rejects_a_host_known_read_tool_under_a_non_read_kind():
    """The kind may narrow: a genuine `fs_read` whose ACP kind says `execute`
    is refused even though the host identity alone would have proven it."""
    provider = await _run_read_only(
        _read_only_event(tool_kind="execute", tool_name="fs_read", mcp_identity_trusted=True)
    )
    assert provider.rejected == ["r1"]
    assert provider.approved == []


@pytest.mark.asyncio
async def test_read_only_policy_rejects_a_read_kind_on_a_mutating_tool():
    """`kind="read"` on a host-stamped `fs_write`: the kind is agent-influenced
    and the identity is not in the read-only set, so nothing proves the call."""
    provider = await _run_read_only(
        _read_only_event(title="Read notes.md", tool_name="fs_write", mcp_identity_trusted=True)
    )
    assert provider.rejected == ["r1"]
    assert provider.approved == []


@pytest.mark.asyncio
async def test_read_only_policy_rejects_a_read_kind_with_no_host_identity():
    """A backend that omits `_meta.kiro` leaves the cached identity empty even
    though both caches hit (provenance verified, nothing identified). An empty
    name matches nothing, and the read kind cannot stand in for it."""
    provider = await _run_read_only(_read_only_event(tool_name="", mcp_identity_trusted=True))
    assert provider.rejected == ["r1"]
    assert provider.approved == []


@pytest.mark.asyncio
async def test_read_only_policy_rejects_a_host_known_name_without_trusted_provenance():
    """THE SEAM. `toolName="fs_read"`, no `mcpServerName`, but the pair did not
    come from the provenance-verified caches (`mcp_identity_trusted=False`: an
    inline payload, a hand-built event). Absence of a server name is not proof
    of a built-in; without the positive provenance flag the name is prose and
    the call is refused. This approved before the flag was consulted."""
    provider = await _run_read_only(
        _read_only_event(tool_name="fs_read", mcp_server_name="", mcp_identity_trusted=False)
    )
    assert provider.rejected == ["r1"]
    assert provider.approved == []


@pytest.mark.asyncio
async def test_read_only_policy_rejects_a_read_looking_title_alone():
    """No kind, no identity, a title that reads like a read. The interactive
    path would take the `_is_read_only_tool` title fallback; READ_ONLY has no
    card behind it and refuses the model-authored prose."""
    provider = await _run_read_only(_read_only_event(tool_kind=""))
    assert provider.rejected == ["r1"]
    assert provider.approved == []


@pytest.mark.asyncio
async def test_hook_based_policy_still_approves_a_read_kind_tool():
    """HOOK_BASED is unchanged: a `read`-kind call with no host identity is
    still auto-approved by the ACP-kind allow-list, because a card stands
    behind that surface."""
    provider = await _run_read_only(_read_only_event(), policy=ToolApprovalPolicy.HOOK_BASED)
    assert provider.approved == ["r1"]
    assert provider.rejected == []
