"""Every HOST deny on the CLI chat, webhook, review-pool and auto-title surfaces
steers the in-band notice before it rejects.

A rejected permission reaches the model as kiro-cli's fixed "User denied tool
execution". The dashboard chat runner, ``llm_helpers``, the messaging surfaces,
the task runner, the eval harness, the subagent surface and the dashboard
handlers already steer the real reason into the running turn first (each pinned
by its own ``test_*deny_notice`` file). The four surfaces here answer their own
permission requests and must do the same:

* ``cli_chat.py`` -- ``kirocrew chat``. The person at the terminal reads a
  specific notice, and the model's reply printed under it must not apologise for
  a refusal they never made. Every host deny goes through ONE funnel
  (``_refuse_host_deny``); the person's own "no" stays bare, because there the
  generic wording is the truth.
* ``dashboard/handlers/hooks.py`` -- the webhook agent turn. No person is
  attached, so every deny is the host's.
* ``apps/builtins/code_review_sage/sage_lib/review_pool.py`` -- the reviewer
  pool's policy deny.
* ``messaging/auto_title.py`` -- the tool-free title-naming turn.

Two halves:

* a SOURCE-LEVEL guard that finds every wire ``reject_tool(`` in each module by
  AST, gives each a verdict (steered, or the one exempt user denial), and fails
  when a site is added that steers nothing;
* BEHAVIOURAL tests driving the real CLI and auto-title code with a provider
  double that records steer/reject ORDER. Order is the mechanism: the steer must
  be written while the permission request is still unanswered, because that is
  what proves the turn is in flight and gets the notice queued instead of
  dropped. The webhook and review-pool behaviour is pinned beside their own
  harnesses (``test_webhook_permission_requests.py``, the code-review-sage
  app's ``test_review_pool.py``).
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import re
import types

import pytest

import kiro_crew.cli_chat as cli_chat
import kiro_crew.messaging.auto_title as auto_title
from kiro_crew.dashboard.handlers.hooks import _WEBHOOK_DENY_NOTICE, _webhook_deny_notice
from kiro_crew.hooks import TOOL_ALLOW, TOOL_DENY, ToolHookResult
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    LLMEvent,
)
from kiro_crew.testing.wait import await_until

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src/kiro_crew"
_GENERIC = "User denied tool execution"
_TAG = "[Kiro Crew host notice]"
_POLICY_CLAUSE = "blocked by a Kiro Crew safety policy"
_SURFACE_CLAUSE = "tool policy of the surface"
_HOOK_ERROR_CLAUSE = "PreToolUse hook raised"
_UNDELIVERABLE_CLAUSE = "approval prompt could not be delivered"
_AUDIT_CLAUSE = "could not write the audit record"
_STEER = "_steer_host_deny"


# ── Source-level coverage guard ──────────────────────────────────────────────


def _tree(module: str) -> ast.Module:
    return ast.parse((_SRC / module).read_text(encoding="utf-8"))


def _is_call_to(node: ast.AST, name: str) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == name) or (
        isinstance(func, ast.Attribute) and func.attr == name
    )


def _awaits(stmt: ast.stmt, name: str) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Await)
        and _is_call_to(stmt.value.value, name)
    )


def _reject_sites(tree: ast.Module) -> list[tuple[list[ast.stmt], int, str]]:
    """Every ``await <x>.reject_tool(...)`` statement: (suite, index, owning def).

    The owner is the innermost def whose span holds the statement, so a reject
    inside a nested helper is attributed to that helper, not its enclosing def.
    """
    defs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    sites: list[tuple[list[ast.stmt], int, str]] = []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            stmts = getattr(node, field, None)
            if not (isinstance(stmts, list) and stmts and isinstance(stmts[0], ast.stmt)):
                continue
            for i, stmt in enumerate(stmts):
                if not _awaits(stmt, "reject_tool"):
                    continue
                holders = [d for d in defs if d.lineno <= stmt.lineno <= (d.end_lineno or d.lineno)]
                owner = min(holders, key=lambda d: (d.end_lineno or 0) - d.lineno).name
                sites.append((stmts, i, owner))
    return sorted(sites, key=lambda s: s[0][s[1]].lineno)


def _steered(stmts: list[ast.stmt], i: int) -> bool:
    """The statement before the reject, in its own suite, is the steer.

    Either the await itself, or a capability guard (``if _steer_host_deny is not
    None:``) whose only statement is that await -- the review pool's shape, where
    the standalone fallback has no helper to call.
    """
    if i == 0:
        return False
    prev = stmts[i - 1]
    if _awaits(prev, _STEER):
        return True
    return (
        isinstance(prev, ast.If)
        and not prev.orelse
        and len(prev.body) == 1
        and _awaits(prev.body[0], _STEER)
        and _STEER in ast.unparse(prev.test)
    )


def _textual_rejects(module: str) -> int:
    return (_SRC / module).read_text(encoding="utf-8").count(".reject_tool(")


class TestEveryRejectOnTheseSurfacesIsSteered:
    """Coverage checkable from the source, not asserted in a PR body."""

    #: module -> number of wire rejects it owns. Count-pinned so a new site
    #: cannot slip in without a verdict here.
    MODULES = {
        "cli_chat.py": 2,
        "dashboard/handlers/hooks.py": 2,
        "dashboard/handlers/hooks_scripts.py": 0,
        "dashboard/handlers/hooks_webhook_intake.py": 0,
        "dashboard/handlers/hooks_webhooks.py": 0,
        "apps/builtins/code_review_sage/sage_lib/review_pool.py": 1,
        "messaging/auto_title.py": 1,
    }

    @pytest.mark.parametrize("module", sorted(MODULES))
    def test_the_scan_finds_every_reject_the_source_contains(self, module):
        # Cross-checked against a plain textual count: a call shape the AST
        # walk misses would otherwise drop out of every assertion below.
        found = _reject_sites(_tree(module))
        assert len(found) == _textual_rejects(module) == self.MODULES[module], (
            module,
            [s[0][s[1]].lineno for s in found],
        )

    @pytest.mark.parametrize(
        "module",
        sorted(m for m in MODULES if m != "cli_chat.py"),
    )
    def test_every_reject_is_preceded_by_the_steer(self, module):
        bare = [s[0][s[1]].lineno for s in _reject_sites(_tree(module)) if not _steered(s[0], s[1])]
        assert not bare, (
            f"{module}: lines {bare} hand the model kiro-cli's generic 'user denied' "
            f"with nothing to correct it -- await {_STEER}(...) right before the reject"
        )

    def test_the_cli_steers_in_its_funnel_and_leaves_only_the_user_denial_bare(self):
        sites = _reject_sites(_tree("cli_chat.py"))
        owners = sorted(name for _stmts, _i, name in sites)
        assert owners == ["_answer_permission", "_refuse_host_deny"], owners
        for stmts, i, name in sites:
            if name == "_refuse_host_deny":
                assert _steered(stmts, i), "the funnel must steer before it rejects"
                # backend-security-controls: audit first, then the notice.
                assert i >= 2 and _awaits(stmts[i - 2], "_audit_refusal")
            else:
                # The person said no: kiro-cli's wording is the truth, so this
                # one stays bare -- "NOT a user action" would be a lie here.
                assert not _steered(stmts, i)
                # The only thing allowed between the audit and the reject is the
                # person's OWN `o` reply -- their words, not a host notice.
                prev = ast.unparse(stmts[i - 1])
                assert "_steer_user_reply" in prev and _STEER not in prev, prev
                audit = ast.unparse(stmts[i - 2])
                assert "_USER_DENY_CODE" in audit and "_USER_REPLY_CODE" in audit, audit

    def test_every_cli_host_deny_names_a_cause_and_a_reason(self):
        tree = _tree("cli_chat.py")
        calls = [
            n
            for n in ast.walk(tree)
            if _is_call_to(n, "_refuse_host_deny") or _is_call_to(n, "_reject_internal_failure")
        ]
        src = (_SRC / "cli_chat.py").read_text(encoding="utf-8")
        textual = src.count("await _refuse_host_deny(") + src.count(
            "await _reject_internal_failure("
        )
        assert len(calls) == textual >= 8, (len(calls), textual)
        unnamed = []
        for call in calls:
            kws = {kw.arg: kw.value for kw in call.keywords}
            cause = kws.get("cause")
            if "reason" not in kws or not isinstance(cause, ast.Name):
                unnamed.append(call.lineno)
            elif not (cause.id.startswith("DENY_CAUSE_") or cause.id == "cause"):
                unnamed.append(call.lineno)
        assert not unnamed, f"cli_chat host denies at lines {unnamed} name no cause/reason"

    def test_every_webhook_deny_code_has_a_true_host_notice(self):
        # Every SEL error code the webhook's deny branch can record maps to a
        # notice; ``hook_deny`` reads the gate's own reason instead.
        src = (_SRC / "dashboard/handlers/hooks.py").read_text(encoding="utf-8")
        body = src.split("async def _run_hook_inner(", 1)[1]
        literals = re.findall(r"deny_error = (\"[a-z_]+\")", body) + re.findall(
            r"\"hook_deny\" if decision\.action == TOOL_DENY else (\"[a-z_]+\")", body
        )
        codes = {ast.literal_eval(m) for m in literals}
        assert len(codes) >= 5, f"the scan found only {codes} -- guard is stale"
        assert codes - {"hook_deny"} == set(_WEBHOOK_DENY_NOTICE), codes


class TestWebhookDenyNotice:
    def test_a_hook_deny_carries_the_gate_reason_under_the_policy_cause(self):
        decision = ToolHookResult(action=TOOL_DENY, reason="Blocked by security policy: rm -rf")
        cause, reason = _webhook_deny_notice("hook_deny", decision)
        assert cause == "policy"
        assert reason == "Blocked by security policy: rm -rf"

    def test_a_reasonless_hook_deny_still_names_the_hook(self):
        _cause, reason = _webhook_deny_notice("hook_deny", ToolHookResult(action=TOOL_DENY))
        assert "hook" in reason

    def test_an_unknown_code_still_yields_a_host_notice(self):
        cause, reason = _webhook_deny_notice("something_new", None)
        assert cause and reason


# ── Behaviour: the CLI chat ──────────────────────────────────────────────────


class _SteeringProvider:
    """Records steer / reject / approve in one ordered trace."""

    supports_refusal_steer = True

    def __init__(self) -> None:
        self.trace: list[tuple[str, object]] = []

    async def steer(self, text: str) -> bool:
        self.trace.append(("steer", text))
        return True

    async def reject_tool(self, request_id) -> None:
        self.trace.append(("reject", request_id))

    async def approve_tool(self, request_id, *, always: bool = False) -> bool:
        self.trace.append(("approve", request_id))
        return True


class _Hooks:
    """A PreToolUse gate returning a fixed verdict, or raising."""

    def __init__(self, result: ToolHookResult | Exception) -> None:
        self._result = result

    def on_tool_call(self, _title, **_kw):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _event() -> LLMEvent:
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=7,
        title="Read notes",
        tool_kind="read",
        is_shell=False,
        shell_classified=True,
        options=[{"id": "allow", "label": "Allow Once"}],
    )


@pytest.fixture
def cli_env(monkeypatch):
    """Silence the terminal and record the audit. ``audit_broken`` makes the
    approval's audit-or-deny write fail the way a read-only SEL file does."""
    env = types.SimpleNamespace(rows=[], audit_broken=False)

    def _log(**kw):
        if kw.get("critical") and env.audit_broken:
            raise OSError("read-only SEL file")
        env.rows.append(kw)

    monkeypatch.setattr(cli_chat, "sel", lambda: types.SimpleNamespace(log_tool_invocation=_log))
    monkeypatch.setattr(cli_chat, "_stdin_poisoned", False)
    monkeypatch.setattr(cli_chat, "_print_permission_notice", lambda _msg: None)
    return env


def _answer(provider, *, hooks, interactive=True, allowed=None, monkeypatch=None):
    """Answer one request through the real ``_answer_permission``.

    *allowed* stands in for the person's keystroke (the prompt itself is
    pinned in ``test_cli_chat_permission.py``); None leaves the prompt unreached.
    """
    if allowed is not None:
        monkeypatch.setattr(cli_chat, "_can_prompt", lambda _interactive: True)

        async def _prompt(_event):
            return cli_chat._PromptAnswer(allowed=allowed)

        monkeypatch.setattr(cli_chat, "_prompt_answer", _prompt)
    gate = cli_chat._ToolGate(hooks=hooks, agent="cli-tester")

    async def _answered() -> None:
        # The answer lands in milliseconds; the bound only names a hang.
        await await_until(
            asyncio.ensure_future(
                cli_chat._answer_permission(provider, _event(), interactive=interactive, gate=gate)
            ),
            what="the permission answer",
        )

    asyncio.run(_answered())


def _steered_then_rejected(provider: _SteeringProvider, *fragments: str) -> str:
    kinds = [k for k, _ in provider.trace]
    assert kinds == ["steer", "reject"], provider.trace
    notice = str(provider.trace[0][1])
    assert notice.startswith(_TAG), notice
    assert _GENERIC in notice, "the notice must name the generic string it corrects"
    for fragment in fragments:
        assert fragment in notice, (fragment, notice)
    return notice


class TestCliHostDeniesSteerFirst:
    def test_a_policy_deny_names_the_policy_and_its_reason(self, cli_env, monkeypatch):
        provider = _SteeringProvider()
        verdict = ToolHookResult(
            action=TOOL_DENY, reason="Blocked by security policy: sensitive path"
        )
        _answer(provider, hooks=_Hooks(verdict))
        _steered_then_rejected(provider, _POLICY_CLAUSE, "sensitive path")
        assert cli_env.rows[0]["error"] == "hook_deny", "the audit still lands first"

    def test_a_raising_gate_is_a_hook_fault_not_a_verdict(self, cli_env, monkeypatch):
        provider = _SteeringProvider()
        _answer(provider, hooks=_Hooks(RuntimeError("profile unreadable")))
        _steered_then_rejected(provider, _HOOK_ERROR_CLAUSE)

    def test_a_noninteractive_run_says_the_prompt_was_never_delivered(self, cli_env, monkeypatch):
        provider = _SteeringProvider()
        _answer(provider, hooks=_Hooks(ToolHookResult(action=TOOL_ALLOW)), interactive=False)
        _steered_then_rejected(provider, _UNDELIVERABLE_CLAUSE)

    def test_an_approval_the_audit_cannot_record_says_so(self, cli_env, monkeypatch):
        provider = _SteeringProvider()
        cli_env.audit_broken = True
        _answer(
            provider,
            hooks=_Hooks(ToolHookResult(action=TOOL_ALLOW)),
            allowed=True,
            monkeypatch=monkeypatch,
        )
        notice = _steered_then_rejected(provider, _AUDIT_CLAUSE)
        assert "approved" in notice

    def test_the_person_saying_no_gets_no_host_notice(self, cli_env, monkeypatch):
        # The one path where kiro-cli's "User denied" is TRUE: a notice saying
        # "this was NOT a user action" would be the lie here.
        provider = _SteeringProvider()
        _answer(
            provider,
            hooks=_Hooks(ToolHookResult(action=TOOL_ALLOW)),
            allowed=False,
            monkeypatch=monkeypatch,
        )
        assert provider.trace == [("reject", 7)]

    def test_a_backend_without_a_steer_channel_rejects_exactly_as_before(
        self, cli_env, monkeypatch
    ):
        provider = _SteeringProvider()
        provider.supports_refusal_steer = False
        _answer(provider, hooks=_Hooks(ToolHookResult(action=TOOL_DENY, reason="no")))
        assert provider.trace == [("reject", 7)]


# ── Behaviour: the auto-title turn ───────────────────────────────────────────


class _TitleClient(_SteeringProvider):
    async def stream(self, _prompt):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Fix the build")
        yield LLMEvent(kind=EVENT_PERMISSION_REQUEST, request_id="t-1", title="Run ls")
        yield LLMEvent(kind=EVENT_COMPLETE)


def test_the_title_turn_says_the_surface_refused_not_the_person(monkeypatch):
    monkeypatch.setattr(
        auto_title, "sel", lambda: types.SimpleNamespace(log_api_access=lambda **_kw: None)
    )
    client = _TitleClient()
    text = asyncio.run(auto_title._stream_title(client, "name it", source="test"))
    assert text == "Fix the build"
    _steered_then_rejected(client, _SURFACE_CLAUSE, "tool-free")
