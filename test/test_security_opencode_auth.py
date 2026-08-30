"""OpenCode's independent auth stores follow its XDG data root, not config."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import make_dir_link
from kiro_crew import security
from kiro_crew.agent_sdk import host_auth, tool_gate
from kiro_crew.agent_sdk.backends import ACP_BACKEND_OPENCODE


@pytest.fixture()
def auth_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """No credential lookup may reach the operator's home or exported roots."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for env_var in (*host_auth.home_override_env_vars(), "XDG_DATA_HOME"):
        monkeypatch.delenv(env_var, raising=False)
    security._home_targets_cache.clear()
    yield home
    security._home_targets_cache.clear()


@pytest.mark.parametrize("filename", ["auth.json", "mcp-auth.json"])
def test_default_store_is_fenced_and_masked(auth_home: Path, filename: str) -> None:
    credential = auth_home / ".local" / "share" / "opencode" / filename
    assert security.is_sensitive_path(str(credential))
    assert security.is_sensitive_path(str(credential).upper())
    assert str(credential) in security.sandbox_credential_targets()
    assert security.path_contains_sensitive(str(credential.parent))


@pytest.mark.parametrize("filename", ["auth.json", "mcp-auth.json"])
def test_xdg_store_keeps_nested_suffix_and_default_floor(
    auth_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    data_home = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    credential = data_home / "opencode" / filename
    default = auth_home / ".local" / "share" / "opencode" / filename
    assert security.is_sensitive_path(str(credential))
    assert security.is_sensitive_path(str(default))
    assert security.is_sensitive_path(str(credential).upper())
    mask = security.sandbox_credential_targets()
    assert str(credential) in mask
    assert str(default) in mask
    assert str(data_home / filename) not in mask
    assert not security.is_sensitive_path(str(data_home / filename))


@pytest.mark.parametrize("filename", ["auth.json.bak", "opencode.json", "config.json"])
def test_noncredential_siblings_remain_readable(
    auth_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    data_home = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    for root in (auth_home / ".local" / "share", data_home):
        sibling = root / "opencode" / filename
        assert not security.is_sensitive_path(str(sibling))
        assert str(sibling) not in security.sandbox_credential_targets()
        assert not security.is_sensitive_path(str(root / "opencode-other" / "auth.json"))


def test_config_and_test_home_do_not_relocate_auth(
    auth_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for env_var in ("OPENCODE_CONFIG_DIR", "OPENCODE_TEST_HOME"):
        root = tmp_path / env_var.lower()
        monkeypatch.setenv(env_var, str(root))
        assert not security.is_sensitive_path(str(root / "auth.json"))
        assert not security.is_sensitive_path(str(root / "opencode" / "auth.json"))
    assert security.is_sensitive_path(str(auth_home / ".local/share/opencode/auth.json"))


def test_xdg_change_rekeys_warm_floor(
    auth_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "data-first"
    second = tmp_path / "data-second"
    monkeypatch.setenv("XDG_DATA_HOME", str(first))
    assert security.is_sensitive_path(str(first / "opencode/auth.json"))
    first_roots = security._resolve_root_anchors(str(auth_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(second))
    second_roots = security._resolve_root_anchors(str(auth_home))
    assert first_roots != second_roots
    assert security.is_sensitive_path(str(second / "opencode/auth.json"))
    assert not security.is_sensitive_path(str(first / "opencode/auth.json"))
    assert str(second / "opencode/auth.json") in security.sandbox_credential_targets()


@pytest.mark.parametrize("filename", ["auth.json", "mcp-auth.json"])
def test_symlinked_xdg_root_and_workspace_alias_are_fenced(
    auth_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    data_home = tmp_path / "data-real"
    store = data_home / "opencode"
    store.mkdir(parents=True)
    (store / filename).write_text("fake test credential", encoding="utf-8")
    data_link = tmp_path / "data-link"
    alias = tmp_path / "workspace-alias"
    make_dir_link(data_link, data_home)
    make_dir_link(alias, store)
    monkeypatch.setenv("XDG_DATA_HOME", str(data_link))
    for credential in (store / filename, data_link / "opencode" / filename, alias / filename):
        assert security.is_sensitive_path(str(credential))
    assert str(store / filename) in security.sandbox_credential_targets()


def test_exclusion_removes_both_anchors_without_opening_read_gate(
    auth_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    leaf = ".local/share/opencode/auth.json"
    mask = security.sandbox_credential_targets(exclude_leaves=(leaf,))
    for credential in (auth_home / leaf, data_home / "opencode/auth.json"):
        assert str(credential) not in mask
        assert security.is_sensitive_path(str(credential))
    assert str(data_home / "opencode/mcp-auth.json") in mask


def test_dormant_harness_declares_auth_without_mask_exemption(auth_home: Path) -> None:
    declaration = host_auth.declaration_for(ACP_BACKEND_OPENCODE)
    assert declaration.credential_leaves == (
        ".local/share/opencode/auth.json",
        ".local/share/opencode/mcp-auth.json",
    )
    assert declaration.home_override_env_vars == ("XDG_DATA_HOME",)
    assert declaration.entitlement_source == host_auth.ENTITLEMENT_OWN_CREDENTIAL_FILE
    assert declaration.adapter_own_leaves == ()
    assert declaration.host_logout_retires_children is False
    assert "opencode auth login" in declaration.sign_in_remedy
    assert ACP_BACKEND_OPENCODE not in tool_gate.ADAPTER_OWN_CREDENTIAL_LEAVES


@pytest.mark.parametrize(
    ("leaf", "env_var", "filename"),
    [
        (".codex/auth.json", "CODEX_HOME", "auth.json"),
        (".claude/.credentials.json", "CLAUDE_CONFIG_DIR", ".credentials.json"),
        (".claude/.credentials.json", "CLAUDE_HOME", ".credentials.json"),
    ],
)
def test_existing_overrides_keep_basename_semantics(
    auth_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    leaf: str,
    env_var: str,
    filename: str,
) -> None:
    override = tmp_path / "existing-harness"
    monkeypatch.setenv(env_var, str(override))
    credential = override / filename
    assert host_auth.override_leaf_suffix(leaf, env_var) == filename
    assert security.is_sensitive_path(str(credential))
    assert str(credential) in security.sandbox_credential_targets()
    excluded = security.sandbox_credential_targets(exclude_leaves=(leaf,))
    assert str(credential) not in excluded
    assert str(auth_home / leaf) not in excluded
