"""Fixtures and writers shared by the member essential-context suites.

``env`` builds an isolated crew with one V2 member (``writer``), its template, rules and
memory store, and yields the handles the tests read; ``_close_skills_loaders`` is the
autouse fixture that closes the ``SkillsLoader`` ``env`` builds. ``_write_template`` and
``_write_kiro_settings`` write a template and kiro-cli's settings, ``_snapshot_bodies``
builds the member's essential envelope, and ``_declare_steering_glob_only`` declares only
the steering glob with the workspace opted out of the implicit scan. ``test_member_essential_context.py``,
``test_member_essential_context_resource_admission.py``,
``test_member_essential_context_inherit_opt_out.py`` and
``test_member_essential_context_left_out_sources.py`` bind the fixtures by name;
``env`` stays importable from ``test_member_essential_context`` for the modules that
import it from there.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kiro_crew import context as context_module
from kiro_crew.agent_sdk.provider_identity import PROVIDER_ACP
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.context import ContextBuilder
from kiro_crew.learn import LessonStore
from kiro_crew.members import slug_for_name, write_member_rules
from kiro_crew.memory import MemoryStore
from kiro_crew.memory_stores import (
    memory_store_dir_for,
    persist_member_config,
    provision_member_memory,
)
from kiro_crew.skills import SkillsLoader


@pytest.fixture(autouse=True)
def _close_skills_loaders(close_skills_loaders):
    """The ``env`` fixture builds a ``SkillsLoader`` it never closes: close it (rootdir conftest)."""


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "host-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("KIRO_HOME", str(home / ".kiro"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # The host floor pins these hooks separately from the lazy KIRO_HOME path.
    # Keep template readers on the same test-owned root as template writers.
    agents = home / ".kiro" / "agents"
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", agents)
    monkeypatch.setattr("kiro_crew.agent_discovery._KIRO_AGENTS_DIR", agents)
    cfg = KiroCrewConfig.load()
    cfg.agents["writer"] = KiroCrewAgentConfig(
        kiro_agent="writer-template", description="A careful bilingual writer"
    )
    store = provision_member_memory(cfg, "writer")
    persist_member_config(cfg, "writer", create=True)
    project = tmp_path / "project"
    (project / ".kiro" / "agents").mkdir(parents=True)
    (project / ".kiro" / "steering").mkdir()
    (project / ".kiro" / "agents" / "writer-template.json").write_text(
        json.dumps(
            {
                "name": "writer-template",
                "prompt": "Bound Soul: preserve the user's voice.",
                "resources": ["file://declared-guide.md"],
            }
        ),
        encoding="utf-8",
    )
    for template in ("critic-runtime", "task-template"):
        (project / ".kiro" / "agents" / f"{template}.json").write_text(
            json.dumps({"name": template, "prompt": "Execution task instructions."}),
            encoding="utf-8",
        )
    for path, body in {
        "AGENTS.md": "Project rules: run the review checks.",
        "SOUL.md": "Project Soul: write with empathy.",
        "declared-guide.md": "Declared guide: examples must be reproducible.",
        ".kiro/steering/always.md": "Always guide: explain assumptions.",
        ".kiro/steering/manual.md": "---\ninclusion: manual\n---\nMANUAL_SECRET",
        ".kiro/steering/match.md": "---\ninclusion: fileMatch\n---\nMATCH_SECRET",
        ".kiro/steering/auto.md": "---\ninclusion: auto\n---\nAUTO_SECRET",
    }.items():
        (project / path).write_text(body, encoding="utf-8")
    write_member_rules(slug_for_name("writer"), member="writer", text="Do not publish drafts.")
    forbidden = Mock(side_effect=AssertionError("essential context performed retrieval"))
    from kiro_crew.vector_memory import open_member_database

    tier = open_member_database(
        memory_store_dir_for(store) / "memory.db",
        member_id=cfg.agents["writer"].member_id,
        store_id=store,
    )
    monkeypatch.setattr(tier, "recall", forbidden)
    monkeypatch.setattr(tier, "get_semantic_context", forbidden)
    monkeypatch.setattr(tier, "get_episodic_context", forbidden)
    monkeypatch.setattr(tier, "get_lessons_context", lambda **kwargs: "")
    monkeypatch.setattr(context_module, "_memory_stores", {})
    monkeypatch.setattr(context_module, "_vector_stores", {store: tier})
    memory = ContextBuilder.get_memory_for(memory_store=store)
    memory.write_preferences("Preference anchor: 请保留中文原文。")
    memory.write_projects("Project anchor: the launch guide is authoritative.")
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "global"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path / "lessons"),
    )
    try:
        yield SimpleNamespace(
            builder=builder,
            store=store,
            member=cfg.agents["writer"].member_id,
            project=project,
            memory=memory,
            forbidden=forbidden,
        )
    finally:
        # ``open_member_database`` opens a process-lifetime SQLite connection
        # plus a store-use lock descriptor that nothing else closes; release
        # them so each parametrisation does not leak two descriptors.
        tier.close()


def _write_template(agents: Path, name: str, resources: list[str]) -> None:
    (agents / f"{name}.json").write_text(
        json.dumps({"name": name, "resources": resources}), encoding="utf-8"
    )


def _write_kiro_settings(kiro_dir: Path, values: dict) -> None:
    path = kiro_dir / "settings" / "cli.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values), encoding="utf-8")


def _snapshot_bodies(env, provider_type: str = PROVIDER_ACP) -> str:
    """The member's essential envelope as a session served by *provider_type* builds it.

    The builder is where the harness is known, so it is the builder that decides
    whether kiro-cli's opt-out applies; :func:`documents_for_member` only takes
    that verdict. This helper names kiro-cli explicitly because it is the harness
    the setting belongs to; the builder's own default is "harness not known to
    be kiro-cli", which never reads the opt-out.
    """
    return env.builder._build_v2_essentials(
        env.store, member=env.member, project=str(env.project), provider_type=provider_type
    )


def _declare_steering_glob_only(env) -> None:
    """Declare just the steering glob and opt the workspace out of the implicit scan.

    With the opt-out the declared glob is the only walk that can meet a link, so a
    refusal or a note can only come from it.
    """
    (env.project / ".kiro" / "agents" / "writer-template.json").write_text(
        json.dumps({"name": "writer-template", "resources": ["file://.kiro/steering/**/*.md"]}),
        encoding="utf-8",
    )
    _write_kiro_settings(env.project / ".kiro", {"chat.disableInheritingDefaultResources": True})
