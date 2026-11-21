"""A native KAS sub-agent's file-tool call on a credential path meets the parent's floor.

A ``kirocrew-helper`` child's request shares the session's id; the handle marks it
(``native_child_call``) and stamps the parent's limits. Every approval, in every
approval mode, passes the security floor (``permission_floor.refusal_for``), so a
child's read, write or edit of a credential path is refused there exactly as the
parent's own is. A shell command's text is not matched for paths: that is the OS
sandbox's job, which the child shares with its parent (same engine process).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_kas_native_child_limits import _requests
from test_tool_call_classification_kas import _kas_builtin_flow

from kiro_crew import permission_floor
from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig, hook_gate_kwargs


@pytest.fixture
def secret(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    path = home / ".ssh" / "sa-placeholder"
    path.write_text("placeholder", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    # Windows reads USERPROFILE, not HOME: pin the resolver itself.
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    return path


def _call(kind: str, path: str):
    if kind == "read":
        return _kas_builtin_flow(
            "fs_read", title="Read File", kind="read", raw_input={"path": path}
        )
    if kind == "write":
        raw = {"path": path, "command": "create", "content": "x"}
        return _kas_builtin_flow("fs_write", title="Write", kind="edit", raw_input=raw)
    raw = {"path": path, "command": "strReplace", "oldStr": "placeholder", "newStr": "x"}
    return _kas_builtin_flow("fs_write", title="Edit", kind="edit", raw_input=raw)


@pytest.mark.parametrize("kind", ["read", "write", "edit"])
def test_a_childs_credential_path_file_call_is_refused_like_the_parents(kind, secret, tmp_path):
    parent, child = _requests(_call(kind, str(secret)), {}, ["fs_read", "fs_write"], tmp_path)
    for judged in (parent, child):
        verdict = HookManager(HooksConfig()).on_tool_call(judged.title, **hook_gate_kwargs(judged))
        assert verdict.action == TOOL_DENY, (kind, judged.native_child_call, verdict)
        assert permission_floor.refusal_for(judged) is not None, (kind, judged.native_child_call)
