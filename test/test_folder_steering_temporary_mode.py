"""Folder steering under Temporary memory mode, as the feature map states it.

A private-member chat in Temporary mode receives NO folder steering: the
essentials envelope is built with reads blocked, exactly as that mode already
withholds the member's project documents. A non-member Temporary chat still
receives it. These pins hold the documented asymmetry to the code so the
feature-map row cannot drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_member_essential_context import env as _member_env

from kiro_crew import folder_steering
from kiro_crew.context import ContextBuilder
from kiro_crew.folder_steering import FOLDER_STEERING_HEADER as _RAW_HEADER
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

member_env = _member_env

FOLDER_STEERING_HEADER = _RAW_HEADER.replace("\u2014", "--")
MARKER = "TEMP-MODE-FOLDER-RULE-7c2a"

_needs_pinned_walk = pytest.mark.skipif(
    not folder_steering.pinned_fs.supports_pinned_tree_walk(),
    reason="descriptor-relative directory opens are unavailable on this host",
)


@pytest.fixture
def standards(tmp_path):
    std = tmp_path / "org-standards"
    std.mkdir()
    (std / "rule.md").write_text(f"# Standards\n{MARKER}\n", encoding="utf-8")
    return std


@_needs_pinned_walk
def test_temporary_member_chat_receives_no_folder_steering(member_env, standards):
    env = member_env
    normal, _ = env.builder.build_message(
        "Continue",
        True,
        "dashboard:member",
        memory_store=env.store,
        member=env.member,
        project=str(env.project),
        steering_dirs=(str(standards),),
    )
    assert MARKER in normal, "control: a persistent member turn carries the folder rule"
    temporary, _ = env.builder.build_message(
        "Continue",
        True,
        "dashboard:member-temporary",
        memory_store=env.store,
        member=env.member,
        project=str(env.project),
        steering_dirs=(str(standards),),
        blocks_reads=True,
    )
    assert MARKER not in temporary
    env.forbidden.assert_not_called()


@_needs_pinned_walk
def test_temporary_non_member_chat_still_receives_folder_steering(tmp_path, monkeypatch, standards):
    home = tmp_path / "host-home"
    (home / ".kiro" / "steering").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )
    msg, _ = builder.build_message(
        "hello",
        True,
        "dashboard:temporary",
        steering_dirs=(str(standards),),
        blocks_reads=True,
    )
    assert FOLDER_STEERING_HEADER in msg
    assert MARKER in msg
