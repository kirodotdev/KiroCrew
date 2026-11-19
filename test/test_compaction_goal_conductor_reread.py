# SPDX-License-Identifier: Apache-2.0
"""A conductor is told to re-read goal-conductor on the turn after a compaction.

The compaction drops the skill body a conductor read; the re-injection brings back
only the skills index. Two halves bring the body back: the shared ``skill_first``
rule both conductors carry says to read the skill again when a turn carries a
re-injection block, and the re-injection itself carries a one-line pointer for a
conductor chat and for a crewmate's chat running its ledger patrol. A plain chat,
a worker and an ordinary turn get no pointer.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import agent
from kiro_crew import context as ctx
from kiro_crew import crewmate_harness, member_mate_tools
from kiro_crew.context import ContextBuilder
from kiro_crew.context_assembly import member
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

POINTER = "You were compacted: re-read the goal-conductor skill before your next action."
RULE = "skill text you read is gone: read it again before you act."


def test_skill_first_tells_both_conductors_to_reread_after_a_compaction() -> None:
    rule = " ".join(agent._LEDGER_CONDUCTOR_RULES["skill_first"].split())
    assert RULE in rule
    root = " ".join(agent._CONDUCTOR_SYSTEM_PROMPT.split())
    bot = " ".join(member.ledger_rules_block("conductor.patrol").split())
    assert RULE in root
    assert RULE in bot


@pytest.fixture
def _home(tmp_path, _floor_monkeypatch):
    mp = _floor_monkeypatch
    h = tmp_path / "host-home"
    (h / ".kiro" / "steering").mkdir(parents=True)
    mp.setenv("HOME", str(h))
    mp.setenv("USERPROFILE", str(h))
    mp.setattr(Path, "home", classmethod(lambda cls: h))
    mp.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    return mp


def _turn(tmp_path, session_key: str, **kw) -> str:
    kw.setdefault("needs_reinjection", True)
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )
    msg, _ = builder.build_message("next", False, session_key, **kw)
    return msg


def _bot(mp, *, ledger: bool) -> None:
    """A crewmate ``Atlas`` whose DM thread holds the dispatch tools."""
    cfg = ctx.KiroCrewConfig()
    cfg.agents["Atlas"] = SimpleNamespace(
        description="", triggers="", kiro_agent="", workspace="", member_id=""
    )
    mp.setattr(ctx.KiroCrewConfig, "load", staticmethod(lambda *a, **k: cfg))
    mp.setattr(crewmate_harness, "crewmate_for_session", lambda _c, _k: "Atlas")
    mp.setattr(ctx, "_member_backend_can_dispatch", lambda *_a: True)
    mp.setattr(
        member_mate_tools, "binding_ledger_off_reason", lambda _b: "" if ledger else "config"
    )


@pytest.mark.parametrize("conductor", ["kirocrew-conductor", "kirocrew-ledger-conductor"])
def test_a_conductor_gets_the_pointer_after_a_compaction(tmp_path, _home, conductor) -> None:
    assert POINTER in _turn(tmp_path, "dashboard:chat-c1", agent=conductor)


def test_a_conductor_ordinary_turn_gets_no_pointer(tmp_path, _home) -> None:
    msg = _turn(tmp_path, "dashboard:chat-c1", agent="kirocrew-conductor", needs_reinjection=False)
    assert POINTER not in msg


def test_a_minimal_conductor_turn_gets_no_pointer(tmp_path, _home) -> None:
    msg = _turn(tmp_path, "dashboard:chat-c1", agent="kirocrew-conductor", minimal_context=True)
    assert POINTER not in msg


def test_a_bot_on_its_ledger_patrol_gets_the_pointer(tmp_path, _home) -> None:
    _bot(_home, ledger=True)
    msg = _turn(tmp_path, "dashboard:member-atlas", agent="kirocrew-mate-conductor", mode="member")
    assert POINTER in msg
    ordinary = _turn(
        tmp_path,
        "dashboard:member-atlas",
        agent="kirocrew-mate-conductor",
        mode="member",
        needs_reinjection=False,
    )
    assert POINTER not in ordinary


def test_a_bot_without_the_ledger_gets_no_pointer(tmp_path, _home) -> None:
    _bot(_home, ledger=False)
    msg = _turn(tmp_path, "dashboard:member-atlas", agent="kirocrew-mate-conductor", mode="member")
    assert POINTER not in msg


@pytest.mark.parametrize(
    "agent_name, mode",
    [
        (None, ""),
        ("kirocrew", ""),
        ("kirocrew-worker", ""),
        ("kirocrew-mate-worker", ""),
        # Conductors that run their own skill, not goal-conductor.
        ("kirocrew-pipeline-conductor", ""),
        ("kirocrew-security-conductor", ""),
        ("kirocrew-team-lead", ""),
    ],
)
def test_plain_chats_and_workers_get_no_pointer(tmp_path, _home, agent_name, mode) -> None:
    _bot(_home, ledger=True)  # even next to a ledger crewmate: only its DM thread counts
    msg = _turn(tmp_path, "dashboard:chat-p1", agent=agent_name, mode=mode)
    assert POINTER not in msg
