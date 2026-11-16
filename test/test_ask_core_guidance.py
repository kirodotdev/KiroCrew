# SPDX-License-Identifier: Apache-2.0
"""One ask policy, at agent rank, for all four crew kinds.

``ASK_CORE`` (``context_assembly/output_contract.py``) states the reroute-first
rule and the five ask cases once. The conductors (Bot conductor, Conductor-mode
lead and conductor) carry it with ``CONDUCTOR_ASK_TAIL`` in their agent prompt,
the workers (Bot worker, Conductor-mode worker) with ``WORKER_ASK_TAIL``. Every
other ask text points at "(a)-(e)" instead of copying the list, and the policy
refusal texts reroute instead of telling the model to stop
(``docs/system-specs/modules/crew-mode.md``, Ask cases and reroute).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from kiro_crew import agent
from kiro_crew.agent_materialization.conductor_agents import with_conductor_ask
from kiro_crew.agent_materialization.worker_agent import worker_prompt
from kiro_crew.context import ContextBuilder
from kiro_crew.context_assembly.output_contract import (
    ASK_CORE,
    CONDUCTOR_ASK_TAIL,
    WORKER_ASK_TAIL,
)
from kiro_crew.dashboard.state_parts.injected_turns import build_refusal_recovery_prompt
from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT, mate_worker_prompt
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
#: A phrase only the case list itself says: (c) of the five ask cases.
_CASE_C = "deletes or overwrites someone else's work"


def _bc_prompt(builder: ContextBuilder) -> str:
    return builder._resolve_agent_prompt(
        MATE_CONDUCTOR_AGENT,
        project=None,
        mode="",
        session_key="dashboard:ask-core",
        is_cc=False,
        private_owner=False,
        session_start=True,
    )


@pytest.fixture
def builder(tmp_path: Path, opened, monkeypatch: pytest.MonkeyPatch) -> ContextBuilder:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(ContextBuilder, "_live_cap_figure", staticmethod(lambda: "4"))
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=opened(SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)),
    )


def _installed_prompts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The team lead's and the goal conductor's prompts as their installers write them:
    the ask policy is appended at install (``conductor_agents.with_conductor_ask``)."""
    from kiro_crew.agent_files import CONDUCTOR_AGENT_FILENAME, TEAM_LEAD_AGENT_FILENAME
    from kiro_crew.agent_materialization import service_agents

    specs = tmp_path / "agents"
    specs.mkdir()
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: specs)
    agent._install_conductor_agent()
    service_agents._install_team_lead_agent()
    return [
        json.loads((specs / name).read_text(encoding="utf-8"))["prompt"]
        for name in (TEAM_LEAD_AGENT_FILENAME, CONDUCTOR_AGENT_FILENAME)
    ]


def test_every_conductor_carries_the_core_and_the_conductor_tail(builder, tmp_path, monkeypatch):
    ask = ASK_CORE + CONDUCTOR_ASK_TAIL
    for prompt in _installed_prompts(tmp_path, monkeypatch):
        assert ask in prompt
    shipped = _bc_prompt(builder)
    assert ask in shipped
    # The Bot conductor keeps the hold against asking to loosen a rule.
    assert "ask to loosen a rule, sandbox or approval mode" in shipped
    # A user prompt.md replaces the shipped prompt, not the ask policy.
    override = Path(tmp_path / "home" / "prompt.md")
    override.write_text("USER-OVERRIDE-MARKER: you are my own prompt.\n", encoding="utf-8")
    custom = _bc_prompt(builder)
    assert "USER-OVERRIDE-MARKER" in custom
    assert ask in custom


def test_both_workers_carry_the_core_and_the_worker_tail():
    ask = ASK_CORE + WORKER_ASK_TAIL
    assert mate_worker_prompt().endswith(ask)
    assert worker_prompt().endswith(ask)
    # After the work_brief line, which must stay first.
    text = worker_prompt()
    assert text.index("Start by calling `work_brief`") < text.index(ASK_CORE)


def _python_strings(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def test_the_ask_case_list_is_written_once():
    """Every other ask text points at "(a)-(e)": no second copy can drift."""
    hits: list[str] = []
    for path in sorted(_SRC.rglob("*")):
        if path.suffix == ".py":
            raw = path.read_text(encoding="utf-8")
            # A cheap pre-filter: the phrase cannot be in a literal whose words are absent.
            texts = _python_strings(path) if "overwrites" in raw else []
        elif path.suffix == ".md":
            texts = [path.read_text(encoding="utf-8")]
        elif path.suffix == ".json":
            texts = [json.dumps(json.loads(path.read_text(encoding="utf-8")), ensure_ascii=False)]
        else:
            continue
        for text in texts:
            if _CASE_C in " ".join(text.split()):
                hits.append(path.relative_to(_SRC).as_posix())
    assert hits == ["context_assembly/output_contract.py"]
    assert _CASE_C in " ".join(ASK_CORE.split())


@pytest.mark.parametrize("answered", [False, True])
def test_the_refusal_recovery_prompt_reroutes_instead_of_stopping(answered):
    out = build_refusal_recovery_prompt([("bash", "blocked by security policy")], answered=answered)
    flat = " ".join(out.split())
    assert "does not reach the same effect" in flat
    assert "say so and stop" not in flat


@pytest.mark.parametrize(
    "phrase",
    [
        # A rerun that replaces runs the agent's own bug voided is not new spend.
        "rerunning runs your own bug voided to replace them are not (b)",
        # A done report carries no buttons, and that wins over the [OPTIONS:] rule.
        "this outranks the [OPTIONS:] rule",
        "Act on a ruling already made",
        # A refused protected-branch push takes the pull-request path.
        "a refused push to a protected branch goes to a feature branch and a pull request",
    ],
)
def test_the_core_names_what_is_not_an_ask(phrase, builder):
    flat = " ".join(ASK_CORE.split())
    assert phrase in flat
    # The same core reaches every kind: both conductor seams, the Bot conductor,
    # and both workers.
    for prompt in (
        with_conductor_ask(agent._TEAM_LEAD_SYSTEM_PROMPT),
        with_conductor_ask(agent._CONDUCTOR_SYSTEM_PROMPT),
        _bc_prompt(builder),
        mate_worker_prompt(),
        worker_prompt(),
    ):
        assert phrase in " ".join(prompt.split())
