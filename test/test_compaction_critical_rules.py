# SPDX-License-Identifier: Apache-2.0
"""The turn after a compaction gets the session's ``[CRITICAL RULES]`` back, once.

The block is the reply contract (the ``[OPTIONS:]`` rule, diff blocks, path and
link rules, the ask policy) and session-start context, so a compaction drops it.
The post-compaction parts re-send the session's own variant, with the session-start
opt-out (``includeCrewContext: false``); a minimal turn, whose minimal start sent
none, gets none, and neither does a session whose head holds that exact text.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import agent_sdk
from kiro_crew import context as ctx
from kiro_crew import session_head
from kiro_crew.acp.types import PROVIDER_LABEL_KAS
from kiro_crew.context import ContextBuilder
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

KEY = "dashboard:chat-rules"


def _folded(block: str) -> str:
    """*block* as it leaves in the message: every message is folded on its way out."""
    return block.translate(ctx._MULTIBYTE_TABLE)


def _turn(tmp_path, is_new_session: bool = False, **kw) -> str:
    kw.setdefault("needs_reinjection", True)
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )
    msg, _ = builder.build_message("next", is_new_session, KEY, runtime_source="dashboard", **kw)
    return msg


def _head_session(monkeypatch, recorded_rules: str):
    """A KAS session whose launch record holds *recorded_rules* as its head's rules."""
    monkeypatch.setattr(agent_sdk, "context_provider_of", lambda value: value)
    held = {session_head.RULES_KEY: session_head.fold(recorded_rules)}
    return SimpleNamespace(
        essential_delivery=None,
        member_dispatch_mounted=None,
        native_context_documents=held,
        native_steering=True,
        served_model="",
        cwd="",
    )


@pytest.mark.parametrize(
    ("is_new_session", "resumed"),
    [(False, False), (True, True)],
    ids=["warm-compaction", "resume-of-a-compacted-chat"],
)
def test_a_compaction_turn_gets_the_sessions_rules_back_once(tmp_path, is_new_session, resumed):
    msg = _turn(tmp_path, is_new_session, resumed=resumed)
    assert msg.count(_folded(ctx._critical_rules_for(KEY, "dashboard"))) == 1
    assert _folded(ctx._CRITICAL_RULES_CHANNEL) not in msg
    assert "[CRITICAL RULES" not in _turn(tmp_path, needs_reinjection=False)


def test_an_opted_out_agent_and_a_minimal_turn_get_none(tmp_path, monkeypatch):
    assert "[CRITICAL RULES" not in _turn(tmp_path, minimal_context=True)
    monkeypatch.setattr(ctx, "_agent_includes_crew_context", lambda agent: False)
    assert "[CRITICAL RULES" not in _turn(tmp_path)


def test_a_session_head_holding_the_rules_gets_none(tmp_path, monkeypatch):
    rules = ctx._critical_rules_for(KEY, None)
    held = _turn(
        tmp_path,
        provider_type=PROVIDER_LABEL_KAS,
        context_provider=_head_session(monkeypatch, rules),
    )
    assert "[CRITICAL RULES" not in held
    other = _turn(
        tmp_path,
        provider_type=PROVIDER_LABEL_KAS,
        context_provider=_head_session(monkeypatch, "[CRITICAL RULES]\nolder text\n"),
    )
    assert other.count(_folded(rules)) == 1
