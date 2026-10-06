"""Every notification producer is attributed, or the bridge refuses its note.

Two halves of one rule. ``producer_identity_meta`` resolves the agent a session runs
as from the trusted sources -- the slot, subagent and cron registries, the live
session, the execution record bound to the key -- so an agent-produced note can name
it. And every call site that publishes a notification is accounted for here: each one
either carries producer identity or is explicitly tagged system-originated, so a new
publishing path that does neither fails this suite rather than a review.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.dashboard.handlers._shared import _session_agent_names, producer_identity_meta
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.notifications.attribution import SYSTEM_ORIGIN_KEY, system_origin

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"


def _state(*, live: dict[str, str] | None = None) -> SimpleNamespace:
    """A dashboard state with no slot, subagent or cron match for any key."""
    live = live or {}
    sessions = SimpleNamespace(
        has_session=lambda key: key in live,
        _get_session_agent=lambda key: live.get(key, ""),
    )
    return SimpleNamespace(
        sessions=sessions,
        subagents=SimpleNamespace(running=[], _agents={}),
        _slots={},
        crons=SimpleNamespace(_jobs=[]),
    )


def _execution(template: str) -> ExecutionContext:
    return ExecutionContext(
        member_id=None,
        store=MemoryStoreRef("default"),
        selection_kind="template",
        template_id=template,
    )


class TestAnUnlinkedSessionNamesItsAgent:
    """GPT 6.1 (v73 F1): a Slack thread selects its agent without a dashboard slot,
    so the slot lookup misses it; the live session and its execution record do not."""

    KEY = "slack:T1:C1:1712793600.000100"

    def test_the_live_sessions_agent_is_named(self) -> None:
        state = _state(live={self.KEY: "slack-denied-agent"})
        with patch("kiro_crew.execution_context.read_session_execution", return_value=None):
            meta = producer_identity_meta(state, self.KEY)  # type: ignore[arg-type]
        assert meta["producer_agent"] == "slack-denied-agent"

    def test_the_execution_records_template_is_named(self) -> None:
        with patch(
            "kiro_crew.execution_context.read_session_execution",
            return_value=_execution("recorded-tmpl"),
        ):
            names = _session_agent_names(_state(), self.KEY)  # type: ignore[arg-type]
        assert names == ["recorded-tmpl"]

    def test_a_live_session_naming_no_agent_names_the_default(self) -> None:
        state = _state(live={self.KEY: ""})
        with (
            patch("kiro_crew.execution_context.read_session_execution", return_value=None),
            patch(
                "kiro_crew.notifications.attribution.default_agent_names",
                return_value=["default", "kirocrew"],
            ),
        ):
            names = _session_agent_names(state, self.KEY)  # type: ignore[arg-type]
        assert names == ["default", "kirocrew"]

    def test_an_unknown_key_names_nothing_so_the_bridge_refuses_it(self) -> None:
        with (
            patch("kiro_crew.execution_context.read_session_execution", return_value=None),
            patch(
                "kiro_crew.notifications.attribution.default_agent_names",
                return_value=["default"],
            ),
        ):
            assert _session_agent_names(_state(), self.KEY) == []  # type: ignore[arg-type]

    def test_an_unreadable_registry_names_nothing_rather_than_raising(self) -> None:
        state = _state()
        state.sessions = MagicMock(has_session=MagicMock(side_effect=RuntimeError("x")))
        with patch(
            "kiro_crew.execution_context.read_session_execution",
            side_effect=RuntimeError("y"),
        ):
            assert _session_agent_names(state, self.KEY) == []  # type: ignore[arg-type]


def test_system_origin_marks_the_note_and_keeps_its_meta() -> None:
    assert system_origin(kind="k", count=2) == {SYSTEM_ORIGIN_KEY: "1", "kind": "k", "count": 2}


# ── Every publishing call site is accounted for ──

#: (module path under src/kiro_crew, enclosing function) -> how the site attributes
#: its notes. "identity": its meta names the producing session, run, job or agent, and
#: the dashboard sink resolves the agent from it. "system": no agent produced it, and
#: its meta is built with ``system_origin``. "app": an app's own push, attributed by
#: its server-set ``app:<name>`` source. "adapter": forwards a caller's payload.
_SITES: dict[tuple[str, str], str] = {
    ("apps/builtins/issue_radar/backend/watch.py", "_notify_new_issues"): "system",
    ("apps/builtins/ops_mission_control/backend/notify_out.py", "_push"): "app",
    ("dashboard/chat_handlers.py", "_report_lost_queued_prompts"): "identity",
    ("dashboard/chat_voice.py", "_sandbox_refusal_response"): "identity",
    ("dashboard/handlers/hooks.py", "_run_hook_agent"): "identity",
    ("dashboard/handlers/messaging.py", "api_notification_agent_push"): "identity",
    ("dashboard/handlers/notifications_push.py", "api_push_notification"): "app",
    ("dashboard/handlers/terminal.py", "_notify_shell_failed"): "system",
    ("dashboard/handlers/updates.py", "_arm_packaged_app"): "system",
    ("dashboard/handlers/updates.py", "_apply"): "system",
    ("dashboard/messaging_api/proactive_send.py", "_deliver_send_message_fallback"): "identity",
    ("dashboard/notification_coordinator.py", "notify"): "adapter",
    ("dashboard/notification_coordinator.py", "notify_returning_handle"): "adapter",
    ("dashboard/server_runtime/config_watch.py", "_apply_default_model"): "system",
    ("dashboard/server_runtime/heartbeat.py", "_report_prior_crash_dump"): "system",
    ("dashboard/server_runtime/safety_grants.py", "_notify_unattended_expiry"): "system",
    ("dashboard/server_runtime/safety_grants.py", "_notify_restart_dropped_grant"): "system",
    ("dashboard/server_runtime/skill_learning.py", "_emit"): "system",
    ("dashboard/state.py", "__init__"): "identity",
    ("notifications/resource_pressure.py", "_push_slice_oom"): "system",
    ("notifications/resource_pressure.py", "_push_critical"): "system",
    ("notifications/resource_pressure.py", "_push_tight"): "system",
    ("notifications/resource_pressure.py", "_push_recovery"): "system",
    ("slack/gateway.py", "_deliver_script_result"): "identity",
    ("slack/gateway.py", "_alert_cron_failure"): "identity",
    ("slack/gateway.py", "_cron_callback"): "identity",
    ("slack/gateway.py", "_announce_cron_quarantine"): "system",
    ("slack/gateway.py", "_notify_nudge_expired"): "identity",
    ("slack/gateway.py", "_notify_consolidation_abandoned"): "system",
    ("slack/gateway.py", "_deliver_result"): "identity",
    ("slack/gateway.py", "_subagent_done"): "identity",
    ("slack/gateway.py", "_orphan_dm"): "identity",
    ("slack/gateway.py", "_task_notify"): "identity",
    ("slack/gateway.py", "_notice_wheel_update_once"): "system",
}


def _publishing_calls() -> list[tuple[str, str, ast.Call]]:
    """Every call that hands a note to the notification bus, with its site."""
    found: list[tuple[str, str, ast.Call]] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if "/tests/" in f"/{rel}" or "_tests/" in rel:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def visit(node: ast.AST, fn: str) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn = node.name
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                recv = ast.unparse(node.func.value)
                attr = node.func.attr
                legacy = (
                    attr in ("notify", "notify_awaiting_persist")
                    and len(node.args) >= 3
                    and not recv.startswith("_notifications_for")
                    and recv != "self.publisher"
                )
                pushed = attr == "push" and (
                    recv.endswith("bus") or recv.endswith("notification_bus")
                )
                if legacy or pushed:
                    found.append((rel, fn, node))
            for child in ast.iter_child_nodes(node):
                visit(child, fn)

        visit(tree, "<module>")
    return found


def _meta_source(call: ast.Call) -> str:
    """The source of the ``meta=`` a call passes, directly or on its payload."""
    for keyword in call.keywords:
        if keyword.arg == "meta":
            return ast.unparse(keyword.value)
    if call.args and isinstance(call.args[0], ast.Call):
        for keyword in call.args[0].keywords:
            if keyword.arg == "meta":
                return ast.unparse(keyword.value)
    return ""


def test_every_publishing_site_is_attributed_or_tagged_system() -> None:
    calls = _publishing_calls()
    assert calls, "the scan found no publishing call at all; it is not looking"
    unknown = sorted({(rel, fn) for rel, fn, _ in calls} - set(_SITES))
    assert not unknown, (
        "new notification publishing site(s) with no attribution decision: "
        f"{unknown}. Give each note producer identity (session/run/job/agent meta) "
        "or build its meta with system_origin(...), then record the choice in _SITES."
    )
    for rel, fn, call in calls:
        kind = _SITES[(rel, fn)]
        meta = _meta_source(call)
        where = f"{rel}:{call.lineno} ({fn})"
        if kind == "system":
            assert "system_origin(" in meta, f"{where} is listed system but its meta is {meta!r}"
        elif kind == "identity":
            if call.args and isinstance(call.args[0], ast.Name) and not meta:
                # A payload built a few lines up (the agent push names its session
                # there); the bridge's runtime rule refuses it if that ever stops.
                continue
            assert meta and meta != "None", f"{where} is listed identity but passes no meta"
            assert "system_origin(" not in meta, f"{where} tags an agent note as system"


def test_every_listed_site_still_exists() -> None:
    """A stale entry would let a renamed site's replacement go unreviewed."""
    present = {(rel, fn) for rel, fn, _ in _publishing_calls()}
    assert not sorted(set(_SITES) - present)


@pytest.mark.parametrize(
    "call",
    [
        'state.notify("update", "t", "b")',
        'state.notify("agent", "t", "b", meta=None)',
    ],
)
def test_the_scan_classifies_a_bare_call_as_unattributed(call: str) -> None:
    node = ast.parse(call).body[0].value  # type: ignore[attr-defined]
    meta = _meta_source(node)
    assert "system_origin(" not in meta and (not meta or meta == "None")
