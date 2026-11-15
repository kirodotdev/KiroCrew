"""The two ``subagent_parts`` owners that also run before ``kiro_crew.subagent`` is loaded.

A stdio tool server reads the sub-agent ceiling and the parent's spawn allowlists for a tool
description it writes before it loads its handlers, through ``kiro_crew.subagent_standalone``.
So ``host_memory`` and ``spawn_allowlists`` bind every global their functions read, answer
with the facade unloaded, and are reached through one object on both sides of its load.
What the composition promises for every owner is in
``test_subagent_parts_composition_contract.py``.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import subprocess
import sys
import textwrap
import types

import pytest
from test_subagent_parts_composition_contract import _STANDALONE_OWNERS as STANDALONE_OWNERS
from test_subagent_parts_composition_contract import _global_names as global_names

import kiro_crew.subagent as subagent
from kiro_crew import subagent_parts
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


def test_a_standalone_owner_binds_every_global_its_functions_read() -> None:
    """Before the facade loads, a standalone owner's functions run on its own globals."""
    for short in STANDALONE_OWNERS:
        owner = importlib.import_module(f"{subagent_parts.__name__}.{short}")
        functions = [
            value
            for value in vars(owner).values()
            if isinstance(value, types.FunctionType)
            and value.__code__.co_filename == owner.__file__
        ]
        assert functions, short
        unbound = sorted(
            {
                name
                for fn in functions
                for name in global_names(fn.__code__)
                if name not in vars(owner) and not hasattr(builtins, name)
            }
        )
        assert unbound == [], f"{short} reads names only the facade binds"


def test_the_standalone_readers_answer_without_loading_the_facade() -> None:
    probe = textwrap.dedent("""
        import sys
        from types import SimpleNamespace
        from kiro_crew import subagent_standalone as early
        cfg = SimpleNamespace(agent=SimpleNamespace(max_subagents=7))
        before = (
            early.resolve_max_subagents(cfg),
            early.visible_agent_names(["scout", "kirocrew", "bad name"]),
            early.agent_matches_allowlist("docs-writer", ["docs-*"]),
            early.parent_spawn_allowlists(""),
            "kiro_crew.subagent" in sys.modules,
        )
        import kiro_crew.subagent as facade
        facade.resolve_max_subagents = lambda cfg: 99
        after = (
            early.resolve_max_subagents(cfg),
            facade.visible_agent_names is sys.modules[
                "kiro_crew.subagent_parts.spawn_allowlists"
            ].visible_agent_names,
        )
        print((before, after))
        """)
    done = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        check=True,
        timeout=FALLBACK_TIMEOUT_SECS,
        **UTF8_TEXT,
    )
    before, after = ast.literal_eval(done.stdout.strip().splitlines()[-1])
    assert before == (7, (["scout"], 0), True, (), False)
    # The facade's own binding is the facade's to patch; the owner's follows the compose.
    assert after == (7, True)


def test_each_roster_reader_answers_as_the_facade_does_once_it_is_loaded() -> None:
    from kiro_crew import subagent_standalone as early

    roster = ["scout", "kirocrew", "bad name"]
    assert early.visible_agent_names(roster) == subagent.visible_agent_names(roster)
    assert early.visible_agent_names(roster) == (["scout"], 0)
    assert early.agent_matches_allowlist("docs-writer", ["docs-*"]) is True
    assert early.agent_matches_allowlist("scout", ["docs-*"]) is False
    assert early.parent_spawn_allowlists("") == subagent.parent_spawn_allowlists("") == ()


def test_a_patch_of_the_facade_reaches_a_standalone_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew import subagent_standalone as early

    cfg = types.SimpleNamespace(
        agent=types.SimpleNamespace(max_subagents=0, subagent_auto_max=11, spawn_min_memory_gb=4.0)
    )
    monkeypatch.setattr(subagent, "_available_memory_gb", lambda **_kw: 64.0)
    assert early.resolve_max_subagents(cfg) == 11
    monkeypatch.setattr(subagent, "_available_memory_gb", lambda **_kw: 0.0)
    assert early.resolve_max_subagents(cfg) == subagent._LEGACY_DEFAULT_MAX
