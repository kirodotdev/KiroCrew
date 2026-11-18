"""Every channel directive consumer sees the live sub-agent manager.

An agent-issued ``autonudge_stop`` / ``monitor_stop`` is refused while child
work is attached, and that refusal reads ``state.subagents``. A channel turn
with no dashboard state attached validates against the consumer's stand-in, so
a stand-in without the manager waves the stop through. These tests pin that
the consumer resolves the manager and that every channel gateway supplies it.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard import session_directive_apply
from kiro_crew.messaging.dispatch import build_directive_consumer

SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"


async def _consumed_state(monkeypatch, **kwargs):
    seen: list = []

    async def _apply(state, *_args, **_kwargs):
        seen.append(state)
        return SimpleNamespace(text="ok", ends_turn=False)

    monkeypatch.setattr(session_directive_apply, "apply_session_directive_outcome", _apply)
    consume = build_directive_consumer(session_key="telegram:1", sessions=MagicMock(), **kwargs)
    return consume, seen


@pytest.mark.asyncio
async def test_the_stand_in_reads_the_dispatchers_manager_late(monkeypatch):
    dispatcher = SimpleNamespace(subagent_manager=None)
    consume, seen = await _consumed_state(monkeypatch, dispatcher=dispatcher)
    manager = MagicMock(name="manager")
    # Attached after the consumer was built, as a gateway does at boot.
    dispatcher.subagent_manager = manager

    await consume("autonudge_stop", {})

    assert seen[0].subagents is manager


@pytest.mark.asyncio
async def test_an_explicit_manager_wins_over_the_dispatchers(monkeypatch):
    explicit = MagicMock(name="explicit")
    dispatcher = SimpleNamespace(subagent_manager=MagicMock(name="attached"))
    consume, seen = await _consumed_state(monkeypatch, dispatcher=dispatcher, subagents=explicit)

    await consume("monitor_stop", {})

    assert seen[0].subagents is explicit


@pytest.mark.asyncio
async def test_attached_dashboard_state_is_used_as_is(monkeypatch):
    dashboard_state = MagicMock(name="dashboard_state")
    dispatcher = SimpleNamespace(dashboard_state=dashboard_state, subagent_manager=MagicMock())
    consume, seen = await _consumed_state(monkeypatch, dispatcher=dispatcher)

    await consume("autonudge_stop", {})

    assert seen[0] is dashboard_state


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        )
    ]


def _consumer_call_sites() -> list[tuple[Path, ast.Call]]:
    sites = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "build_directive_consumer(" not in text:
            continue
        tree = ast.parse(text)
        sites.extend((path, call) for call in _calls(tree, "build_directive_consumer"))
    return sites


def _gateway_attaches_manager(gateway: Path) -> bool:
    """Whether *gateway* hands the orchestrator's manager to its dispatcher."""
    tree = ast.parse(gateway.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Attribute) and t.attr == "subagent_manager" for t in node.targets
        ):
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id.endswith("Dispatcher")
            and any(kw.arg == "subagent_manager" for kw in node.keywords)
        ):
            return True
    return False


def _channel_package(path: Path) -> str:
    """The top-level ``kiro_crew`` package a call site belongs to, however deep it sits."""
    return path.relative_to(SRC).parts[0]


def test_the_scan_finds_every_channel_consumer():
    packages = {_channel_package(path) for path, _ in _consumer_call_sites()}
    # A rename that hides the call sites from the scan would pass the loop vacuously.
    assert {"feishu", "imessage", "teams", "telegram", "wecom", "weixin", "slack"} <= packages


@pytest.mark.parametrize(
    "path,call",
    _consumer_call_sites(),
    ids=lambda v: str(v.relative_to(SRC)) if isinstance(v, Path) else str(v.lineno),
)
def test_every_consumer_call_site_supplies_the_live_manager(path, call):
    kwargs = {kw.arg: kw.value for kw in call.keywords}
    if "subagents" in kwargs:
        return
    dispatcher = kwargs.get("dispatcher")
    assert dispatcher is not None, f"{path}:{call.lineno} passes neither subagents nor dispatcher"
    package = _channel_package(path)
    gateway = SRC / package / "gateway.py"
    if package == "messaging":
        # ``ChannelTurns`` passes ``subagents`` above; anything else here is new.
        pytest.fail(f"{path}:{call.lineno} must pass subagents explicitly")
    assert gateway.exists(), f"{path}:{call.lineno} has no channel gateway to attach the manager"
    assert _gateway_attaches_manager(gateway), (
        f"{gateway} never attaches the orchestrator's subagent manager to its dispatcher, "
        f"so {path}:{call.lineno} validates agent stops without child-work state"
    )
