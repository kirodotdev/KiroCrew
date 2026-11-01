"""A gateway started from an agent shell must not hand that shell's scratch to its children.

The agent shell exports the whole ``agent_scratch.scratch_env`` set -- ``TMPDIR``,
``TMP``, ``TEMP``, ``KIROCREW_SCRATCH`` and ``KIRO_CHAT_LOG_FILE`` -- naming its own
per-process directory under the managed scratch root. A gateway restarted from inside
such a shell keeps those values in its own environment, and every child it spawns
starts from ``{**os.environ}``. A spawn whose own allocation succeeds overwrites all
of them; one whose allocation FAILS spawns "with inherited temp", which is then a
directory owned by a pid this gateway is not and reclaimed by the hourly sweep once
that owner is dead -- so the child's shell names a dead ``TMPDIR`` for its whole life,
``tempfile`` raises ``ENOENT`` and npm spins.

The fix lives at spawn time (``agent_scratch.drop_inherited_scratch_env``, called by
``acp.launch.launch`` beside ``drop_foreign_inherited_port``): a value under the
managed root is dropped so the child falls to its platform default; a value outside
it is the operator's own temp directory and is kept.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import acp_launch_capture as capture_mod
import pytest

from kiro_crew import agent_scratch
from kiro_crew.acp import client as client_mod
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO

_SCRATCH_KEYS = ("TMPDIR", "TMP", "TEMP", "KIROCREW_SCRATCH", "KIRO_CHAT_LOG_FILE")


def _shell_exports(tree: Path) -> dict[str, str]:
    """What an agent shell running in *tree* exports, as ``scratch_env`` writes it."""
    return {
        "TMPDIR": str(tree),
        "TMP": str(tree),
        "TEMP": str(tree),
        "KIROCREW_SCRATCH": str(tree),
        "KIRO_CHAT_LOG_FILE": str(tree / "kiro-log" / "kiro-chat.log"),
    }


def test_the_key_set_is_exactly_what_scratch_env_writes(tmp_path) -> None:
    """A key ``scratch_env`` gains without joining the drop set is one a child can inherit."""
    written = agent_scratch.scratch_env(tmp_path / "own", shared=tmp_path / "tree")
    assert set(written) <= set(agent_scratch.SCRATCH_ENV_KEYS)
    assert set(agent_scratch.SCRATCH_ENV_KEYS) == set(_SCRATCH_KEYS)


def test_every_export_naming_a_tree_under_the_managed_root_is_dropped() -> None:
    tree = agent_scratch.scratch_root() / "runtime-24124d4a"
    env = {**_shell_exports(tree), "PATH": "/usr/bin", "KC_OTHER": "kept"}
    dropped = agent_scratch.drop_inherited_scratch_env(env)
    assert dropped == _SCRATCH_KEYS
    assert env == {"PATH": "/usr/bin", "KC_OTHER": "kept"}


def test_the_managed_root_itself_counts_as_under_it() -> None:
    env = {"TMPDIR": str(agent_scratch.scratch_root())}
    assert agent_scratch.drop_inherited_scratch_env(env) == ("TMPDIR",)
    assert env == {}


@pytest.mark.parametrize(
    "value",
    ["/tmp", "/var/folders/zz/T", "/srv/kc/.kiro/crew/scratch-not-the-root/x", "relative/dir"],
)
def test_an_operators_own_temp_directory_is_kept(value) -> None:
    """Outside the managed root the inherited value is the right fallback, not a hazard."""
    env = {"TMPDIR": value, "TMP": value, "TEMP": value}
    assert agent_scratch.drop_inherited_scratch_env(env) == ()
    assert env == {"TMPDIR": value, "TMP": value, "TEMP": value}


def test_only_the_keys_under_the_root_go_and_the_rest_stay() -> None:
    tree = agent_scratch.scratch_root() / "runtime-f31604de"
    env = {"TMPDIR": str(tree), "TMP": "/tmp", "KIROCREW_SCRATCH": str(tree), "TEMP": ""}
    assert agent_scratch.drop_inherited_scratch_env(env) == ("TMPDIR", "KIROCREW_SCRATCH")
    assert env == {"TMP": "/tmp", "TEMP": ""}


def test_absent_keys_are_a_no_op() -> None:
    env = {"PATH": "/usr/bin"}
    assert agent_scratch.drop_inherited_scratch_env(env) == ()
    assert env == {"PATH": "/usr/bin"}


@contextlib.contextmanager
def _gateway_started_from_an_agent_shell(parent: Callable[[Path], dict[str, str]]):
    """``os.environ`` as a gateway restarted from an agent shell carries it.

    *parent* receives the managed scratch root the capture pins -- resolved on
    entry, after the capture's own home pins are in place, so the shell's tree
    lies under the root the launch will judge against.
    """
    with patch.dict(os.environ, parent(agent_scratch.scratch_root())):
        yield


def _capture_child_env(
    tmp_path: Path, parent: Callable[[Path], dict[str, str]], *, allocation_fails: bool
):
    """The child environment the real client launch produces under *parent*."""
    recorders: list = []

    class _Spy(capture_mod._Recorder):
        def __init__(self) -> None:
            super().__init__()
            recorders.append(self)

    def _no_scratch(label: str) -> Path:
        raise OSError("no scratch root")

    def _own_window(label: str) -> Path:
        # The capture answers kiro-cli's allocation with None (no window needed
        # for the golden); this arm needs the allocated branch, so give it one.
        path = tmp_path / "scratch" / label
        path.mkdir(parents=True, exist_ok=True)
        return path

    allocate = _no_scratch if allocation_fails else _own_window
    extra: list = [
        _gateway_started_from_an_agent_shell(parent),
        patch.object(client_mod.agent_scratch, "allocate_scratch", allocate),
    ]
    with patch.object(capture_mod, "_Recorder", _Spy):
        capture_mod.capture(ACP_BACKEND_KIRO, tmp_path, extra_patches=extra)
    assert recorders, "the capture did not reach the spawn"
    return recorders[-1].env


def _dead_tree_exports(root: Path) -> dict[str, str]:
    return _shell_exports(root / "runtime-24124d4a")


def test_a_spawn_that_cannot_allocate_scratch_does_not_inherit_the_shells_tree(
    tmp_path,
) -> None:
    """Through the real client launch: the dead tree reaches no key of the child env.

    Before the fix every one of the five keys arrived naming ``runtime-24124d4a``.
    """
    child_env = _capture_child_env(tmp_path, _dead_tree_exports, allocation_fails=True)
    leaked = {k: v for k, v in child_env.items() if k in _SCRATCH_KEYS}
    assert leaked == {}, leaked


def test_a_spawn_that_cannot_allocate_scratch_keeps_the_operators_temp(tmp_path) -> None:
    """The inherited-temp fallback still inherits a temp the gateway may rely on."""
    child_env = _capture_child_env(
        tmp_path,
        lambda root: {"TMPDIR": "/var/tmp/op", "TMP": "/var/tmp/op"},
        allocation_fails=True,
    )
    assert child_env.get("TMPDIR") == "/var/tmp/op"
    assert child_env.get("TMP") == "/var/tmp/op"


def test_a_spawn_that_allocates_scratch_overwrites_the_inherited_set_as_before(
    tmp_path,
) -> None:
    """With an allocation the child's own window wins on every key, inherited or not."""
    child_env = _capture_child_env(tmp_path, _dead_tree_exports, allocation_fails=False)
    for key in ("TMPDIR", "TMP", "TEMP", "KIROCREW_SCRATCH"):
        assert child_env.get(key) == "<scratch>", key
    assert child_env.get("KIRO_CHAT_LOG_FILE") == "<scratch-log>"
