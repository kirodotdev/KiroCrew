"""A webhook agent turn builds its skill directory on the SERVED harness label.

The configured ``agent.provider`` is ``acp`` on every ACP backend, KAS
included. Built on that label, ``_skills_injection_plan`` lists the trees
kiro-cli lets an unmapped custom agent inherit, while ``skill_search`` scopes on
the served label (``kas``) and can load none of them. So ``/api/hooks/agent``
passes the session's provider as ``context_provider``, as chat, cron, task and
subagent turns do, and ``build_message`` builds on its served label.

These run the real ``_run_hook_inner`` against the real ``ContextBuilder`` with
a provider that reports the served label, and read the prompt it delivers.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import agent_discovery
from kiro_crew.acp import skill_projection as projection
from kiro_crew.acp.types import PROVIDER_LABEL_KAS
from kiro_crew.agent_sdk.provider_identity import PROVIDER_ACP
from kiro_crew.config import KiroCrewConfig
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard.handlers import hooks as hooks_handlers
from kiro_crew.essential_delivery import EssentialDelivery
from kiro_crew.memory import MemoryStore
from kiro_crew.providers.base import LLMProvider
from kiro_crew.skills import SkillsLoader

SKILL = "deploy-helper-21860"


class _ServedProvider(LLMProvider):
    """A real provider type (``context_provider_of`` admits only those) that
    reports *label* as its served harness and records the prompt it is sent."""

    def __init__(self, label: str, cwd: Path) -> None:
        self._label = label
        self._cwd = str(cwd)
        self._delivery = EssentialDelivery()
        self.seen: list[str] = []

    @property
    def essential_delivery(self) -> object:
        return self._delivery

    @property
    def context_provider_type(self) -> str:
        return self._label

    @property
    def cwd(self) -> str:
        return self._cwd

    @property
    def native_context_documents(self) -> dict[str, str]:
        return {}

    @property
    def native_steering(self) -> bool:
        return False

    @property
    def member_dispatch_mounted(self) -> bool | None:
        return None

    async def stream(self, message: str):
        self.seen.append(message)
        return
        yield  # pragma: no cover - makes this an async generator

    async def start(self) -> None:  # pragma: no cover - never started here
        return None

    async def shutdown(self) -> None:  # pragma: no cover
        return None

    async def approve_tool(self, *a, **k) -> None:  # pragma: no cover
        return None

    async def reject_tool(self, *a, **k) -> None:  # pragma: no cover
        return None

    def context_usage_pct(self) -> float:  # pragma: no cover
        return 0.0


class _NewSession:
    def __init__(self, provider: _ServedProvider) -> None:
        self._provider = provider

    async def get_or_create(self, key: str, agent: str | None = None):
        return self._provider, True, False

    def record_success(self, key: str) -> None:
        return None


@pytest.fixture
def unmapped_custom(tmp_path, monkeypatch):
    """A kiro-cli home holding one global skill and one unmapped custom agent."""
    monkeypatch.delenv("KIROCREW_NATIVE_SKILL_PROJECTION", raising=False)
    home = tmp_path / "kiro"
    agents = home / "agents"
    agents.mkdir(parents=True)
    skill = home / "skills" / SKILL
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {SKILL}\ndescription: Ship a deploy\n---\nBody\n", encoding="utf-8"
    )
    (agents / "custom.json").write_text(
        json.dumps({"name": "custom", "tools": ["read"]}), encoding="utf-8"
    )
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(projection, "kiro_home", lambda: home)
    monkeypatch.setattr("kiro_crew.config.paths.kiro_home", lambda: home)
    monkeypatch.setattr(agent_discovery, "_kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(
        agent_discovery,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="custom", filename="custom.json", scope="global")],
    )
    # The configured provider is the ACP label on every ACP backend, KAS included.
    monkeypatch.setattr(
        KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: _config_with_acp())
    )

    async def _no_store(ctx_builder, session_key):
        return ""

    import kiro_crew.context as _ctx

    monkeypatch.setattr(_ctx, "session_store_for_turn", _no_store)
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )
    return builder, project


def _config_with_acp() -> KiroCrewConfig:
    config = KiroCrewConfig()
    config.agent.provider = PROVIDER_ACP
    return config


def _hook_turn(builder: ContextBuilder, provider: _ServedProvider) -> str:
    state = SimpleNamespace(sessions=_NewSession(provider), context_builder=builder)
    asyncio.run(
        hooks_handlers._run_hook_inner(state, "hook:served-label", "deploy finished", "custom")
    )
    assert provider.seen, "the hook turn never reached the provider"
    return provider.seen[0]


def test_a_kas_hook_turn_lists_no_inherited_skill_tree(unmapped_custom):
    builder, project = unmapped_custom
    prompt = _hook_turn(builder, _ServedProvider(PROVIDER_LABEL_KAS, project))
    assert SKILL not in prompt, (
        "a KAS webhook turn listed an inherited kiro-cli skill tree that its "
        "skill_search scope (built on the served `kas` label) cannot load"
    )


def test_a_kiro_cli_hook_turn_still_lists_the_inherited_tree(unmapped_custom):
    """Control: the same agent on kiro-cli inherits the tree, so the KAS test
    above fails for the label and not because the prompt never lists skills."""
    builder, project = unmapped_custom
    prompt = _hook_turn(builder, _ServedProvider(PROVIDER_ACP, project))
    assert SKILL in prompt
