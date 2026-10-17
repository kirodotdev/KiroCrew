"""The member operating-mode block follows the session's actual mount.

The block teaches ``session_create`` / ``session_send`` / ``session_stop``, which a
member DM thread only holds when its composer appended the dashboard
session-control entry to THIS session's MCP array. The configured member backend
being dispatch-capable is not enough: a session can still withhold the mount (the
server switched off, a per-tool restriction on a withhold-only backend, a
permission surface Crew does not own, an unresolved entry). These tests pin that
the context builder reads the live session's answer when there is one, and falls
back to the configured capability only when no session evidence reached it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import kiro_crew.validation  # noqa: F401 - break the legacy import cycle first
from kiro_crew import context as ctx
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.hooks import HookManager
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.skills import SkillsLoader

BLOCK = "[CREW MEMBER OPERATING MODE]"


@pytest.fixture
def builder(tmp_path, monkeypatch):
    cfg = KiroCrewConfig()
    monkeypatch.setattr(ctx.KiroCrewConfig, "load", lambda: cfg)
    monkeypatch.setattr(ctx, "agent_skill_globs", lambda agent: [])
    monkeypatch.setattr(ctx, "kiro_agents_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr(ctx, "_memory_stores", {})
    monkeypatch.setattr(ctx, "_lesson_stores", {})
    skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
    b = ctx.ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "workspace"),
        skills=skills,
        lessons=LessonStore(base_dir=tmp_path / "lessons"),
        hooks=HookManager(),
    )
    try:
        yield b
    finally:
        skills.close()


def _capable(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(ctx, "_member_backend_can_dispatch", lambda cfg=None: value)


@pytest.mark.parametrize(
    ("mounted", "configured", "expected"),
    [
        # The reported corner: a dispatch-capable backend whose session withheld
        # the mount must not be taught tools it does not hold.
        (False, True, False),
        # A session that holds the tools is taught them.
        (True, True, True),
        (True, False, True),
        (False, False, False),
        # No session evidence: the configured capability decides, as before.
        (None, True, True),
        (None, False, False),
    ],
)
def test_block_follows_the_session_mount(builder, monkeypatch, mounted, configured, expected):
    _capable(monkeypatch, configured)
    text = builder.build_session_context(
        session_key="dashboard_member-autofix",
        mode="member",
        member_dispatch_mounted=mounted,
    )
    assert (BLOCK in text) is expected, (mounted, configured)


def test_non_member_mode_never_gets_the_block(builder, monkeypatch):
    _capable(monkeypatch, True)
    text = builder.build_session_context(
        session_key="dashboard_abc", mode="", member_dispatch_mounted=True
    )
    assert BLOCK not in text


def _direct_provider(mounted: bool | None, work_dir: str = "") -> AcpProvider:
    client = object.__new__(AcpClient)
    client._work_dir = work_dir
    if mounted is not None:
        client._member_dispatch_mounted = mounted
    provider = object.__new__(AcpProvider)
    provider._client = client
    provider._native_context_documents = {}
    provider._native_context_incarnation = None
    return provider


def _runtime_provider(mounted: bool) -> AcpProvider:
    session = object.__new__(AcpSessionProvider)
    session._handle = SimpleNamespace(member_dispatch_mounted=mounted)
    provider = object.__new__(AcpProvider)
    provider._client = session
    return provider


@pytest.mark.parametrize("mounted", [True, False, None])
def test_direct_client_answer_reaches_the_provider(mounted):
    assert _direct_provider(mounted).member_dispatch_mounted is mounted


@pytest.mark.parametrize("mounted", [True, False])
def test_runtime_handle_answer_reaches_the_provider(mounted):
    assert _runtime_provider(mounted).member_dispatch_mounted is mounted


def test_a_handle_without_an_answer_is_no_evidence():
    session = object.__new__(AcpSessionProvider)
    session._handle = SimpleNamespace()
    assert session.member_dispatch_mounted is None


@pytest.mark.parametrize("mounted", [True, False])
def test_build_message_threads_the_provider_answer(builder, monkeypatch, tmp_path, mounted):
    seen: list[object] = []
    real = ctx.ContextBuilder.build_session_context

    def _capture(self, *args, **kwargs):
        seen.append(kwargs.get("member_dispatch_mounted", "<missing>"))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(ctx.ContextBuilder, "build_session_context", _capture)
    _capable(monkeypatch, True)
    text, _ = builder.build_message(
        "hello",
        True,
        "dashboard_member-autofix",
        mode="member",
        context_provider=_direct_provider(mounted, str(tmp_path)),
    )
    assert seen == [mounted]
    assert (BLOCK in text) is mounted


def test_build_message_without_a_provider_keeps_the_fallback(builder, monkeypatch):
    seen: list[object] = []
    real = ctx.ContextBuilder.build_session_context

    def _capture(self, *args, **kwargs):
        seen.append(kwargs.get("member_dispatch_mounted", "<missing>"))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(ctx.ContextBuilder, "build_session_context", _capture)
    _capable(monkeypatch, True)
    text, _ = builder.build_message("hello", True, "dashboard_member-autofix", mode="member")
    assert seen == [None]
    assert BLOCK in text
