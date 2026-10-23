"""The code-channel store sits behind the existing ``routing`` directory fence.

The store decides which dashboard session a code channel's messages run in, so
an agent must be able neither to read nor to write it -- by file tool or by a
sandboxed shell -- and must not be able to delete or corrupt it to break routing.
It gets that from where it lives, not from entries of its own: the data home's
``routing`` directory is fenced as a whole on every layer. These tests pin each
layer against the store's real default path, so moving the file out of that
directory fails here.
"""

from __future__ import annotations

import os
import sys

import pytest

from kiro_crew import sandbox, sandbox_plan, sandbox_precreate
from kiro_crew.security import is_sensitive_path, is_sensitive_write_path
from kiro_crew.slack.code_channels import store as scc_store


def _store_path() -> str:
    return str(scc_store._default_path())


def test_the_store_lives_in_the_routing_directory() -> None:
    path = scc_store._default_path()
    assert path.name == "slack-code-channels.json"
    assert path.parent.name == "routing"


def test_agent_file_tools_can_neither_read_nor_write_the_store() -> None:
    path = _store_path()
    assert is_sensitive_path(path)
    assert is_sensitive_write_path(path)


def test_the_atomic_write_temp_sibling_is_fenced_too() -> None:
    # ``atomic_write`` publishes through a mkstemp sibling in the same directory.
    sibling = os.path.join(os.path.dirname(_store_path()), "tmpabcd1234.tmp")
    assert is_sensitive_path(sibling)
    assert is_sensitive_write_path(sibling)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows skips the namespace wrap")
@pytest.mark.parametrize("tier", ["standard", "cc", "strict"])
def test_every_sandbox_tier_masks_the_store_by_its_directory(monkeypatch, tier: str) -> None:
    monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: True)
    plan = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, tier)
    masked = set(plan.sensitive_dirs + plan.sensitive_files)
    # An enclosing directory mask, so a host-side atomic replace of the file
    # never appears inside a live namespace.
    assert os.path.dirname(_store_path()) in masked


def test_the_directory_is_precreated_so_the_mask_holds_on_a_fresh_install() -> None:
    assert scc_store.STORE_DIRNAME in sandbox_precreate._CREW_PRECREATE_HIDDEN_DIR_LEAVES


def test_a_write_creates_the_directory(tmp_path) -> None:
    target = tmp_path / "routing" / "slack-code-channels.json"
    store = scc_store.CodeChannelStore(target)
    store.load()
    store.put_binding(
        scc_store.Binding(channel_id="C1", team_id="T1", session_key="chat-1", anchor_ts="1.0")
    )
    assert target.is_file()
