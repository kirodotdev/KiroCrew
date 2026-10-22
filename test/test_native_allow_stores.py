"""Ratchet: every harness's own permission files are write-protected.

Each ``agent_sdk.native_allow_stores`` entry must be refused for the agent's
file-edit tools at every spelling and stay readable. A harness with no row and no
stated reason fails here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path, PurePosixPath

import pytest

from kiro_crew.agent_sdk import host_auth
from kiro_crew.agent_sdk.backends import ACP_BACKEND_OPENCODE, ACP_BACKENDS_KNOWN
from kiro_crew.agent_sdk.native_allow_stores import (
    NATIVE_ALLOW_STORES,
    NATIVE_ALLOW_STORES_NONE,
    PROJECT_ALLOW_STORES,
    NativeAllowStore,
)
from kiro_crew.security import (
    is_sensitive_path,
    is_sensitive_write_path,
    write_protected_home_paths,
)

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths only")

_STORES = [
    pytest.param(store, id=f"{backend or 'kiro'}:{store.leaf}")
    for backend, stores in NATIVE_ALLOW_STORES.items()
    for store in stores
]


def _under(root: Path, rel: str) -> str:
    return str(root.joinpath(*PurePosixPath(rel).parts))


def test_every_known_harness_has_a_row_or_a_reason() -> None:
    rows, none = set(NATIVE_ALLOW_STORES), set(NATIVE_ALLOW_STORES_NONE)
    assert rows | none == set(ACP_BACKENDS_KNOWN)
    assert not rows & none
    assert all(NATIVE_ALLOW_STORES.values())
    assert all(reason.strip() for reason in NATIVE_ALLOW_STORES_NONE.values())


def test_each_store_moves_with_the_home_variables_host_auth_declares() -> None:
    # opencode keeps config under XDG_CONFIG_HOME and its token under XDG_DATA_HOME.
    for declaration in host_auth.AGENT_AUTH_DECLARATIONS:
        if (
            declaration.backend not in NATIVE_ALLOW_STORES
            or declaration.backend == ACP_BACKEND_OPENCODE
        ):
            continue
        for store in NATIVE_ALLOW_STORES[declaration.backend]:
            envs = {env for env, _ in store.overrides}
            assert set(declaration.home_override_env_vars) <= envs, store.leaf


@_POSIX_ONLY
@pytest.mark.parametrize("store", _STORES)
def test_every_store_is_refused_for_file_edits_and_stays_readable(
    store: NativeAllowStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for env, _ in store.overrides:
        monkeypatch.delenv(env, raising=False)
    # On the write-only tier, so every spelling that tier anchors (pod home etc.) holds.
    assert store.leaf in write_protected_home_paths()
    target = _under(home, store.leaf)
    assert is_sensitive_write_path(target)
    assert is_sensitive_write_path(os.path.join(target, "inner.json"))
    assert not is_sensitive_path(target)
    for env, spelling in store.overrides:
        root = tmp_path / f"moved-{env}"
        monkeypatch.setenv(env, str(root))
        assert is_sensitive_write_path(_under(root, spelling)), env
        monkeypatch.delenv(env)


@_POSIX_ONLY
def test_a_symlinked_harness_dir_is_refused_at_its_real_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, dotfiles = tmp_path / "home", tmp_path / "dotfiles" / "claude"
    home.mkdir()
    dotfiles.mkdir(parents=True)
    (home / ".claude").symlink_to(dotfiles)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert is_sensitive_write_path(str(dotfiles / "settings.json"))
    assert not is_sensitive_write_path(str(dotfiles / "notes.md"))


@_POSIX_ONLY
@pytest.mark.parametrize("rel", PROJECT_ALLOW_STORES)
def test_a_project_claude_settings_file_is_refused_in_any_directory(
    rel: str, tmp_path: Path
) -> None:
    project = tmp_path / "some" / "project"
    assert is_sensitive_write_path(_under(project, rel))
    assert is_sensitive_write_path(rel, str(project))
    assert not is_sensitive_write_path(_under(project, ".claude/notes.json"))
    assert not is_sensitive_write_path(_under(project, "settings.json"))


def test_agent_env_refuses_every_variable_that_moves_a_store() -> None:
    # A store moved for the child alone would sit outside the gate's anchors.
    from kiro_crew.config.sections_agent_env import agent_env_refusal

    for store in (s for stores in NATIVE_ALLOW_STORES.values() for s in stores):
        for env, _ in store.overrides:
            assert agent_env_refusal(env), env
