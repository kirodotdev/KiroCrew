"""A server renamed or removed in a scope global leaves the generated specs.

The rebuild starts from its own previous ``kirocrew.json``, so a copy it made from
``~/.kiro/settings/mcp.json`` stays unless the copy says the rebuild made it. These
run the REAL rebuild repeatedly against one on-disk spec, changing only the Kiro
global between runs, and read both generated specs.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from mcp_merge_helpers import bundled_defaults, run_install_mcp_merge

from kiro_crew import user_json
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION
from kiro_crew.mcp_provenance import (
    DERIVED_KEY,
    copied_key,
    is_copied,
    mark_copied,
    record_derived,
    recorded_source,
)


@pytest.fixture(autouse=True)
def _pinned_kiro_cli_version(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the spec ``permissions`` gate from spawning the host's ``kiro-cli``."""
    _floor_monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: SPEC_PERMISSIONS_MIN_VERSION
    )


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    monkeypatch.setenv("PATH", "/usr/bin")
    binary = tmp_path / "bin" / "srv"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    return bundled_defaults(tmp_path), str(binary)


def _rebuild(tmp_path: Path, cfg_dir: Path, kiro_servers: dict) -> dict[str, dict]:
    """Both generated specs' ``mcpServers``, by file name."""
    run_install_mcp_merge(tmp_path, cfg_dir, cc_servers={}, kiro_servers=kiro_servers)
    agents = tmp_path / "kiro_agents"
    return {
        name: json.loads((agents / name).read_text(encoding="utf-8"))["mcpServers"]
        for name in ("kirocrew.json", "kirocrew-worker.json")
    }


def _hand_write(tmp_path: Path, name: str, entry: dict) -> None:
    path = tmp_path / "kiro_agents" / "kirocrew.json"
    spec = json.loads(path.read_text(encoding="utf-8"))
    spec["mcpServers"][name] = entry
    path.write_text(json.dumps(spec), encoding="utf-8")


def test_a_renamed_then_removed_server_leaves_both_specs(
    tmp_path: Path, rig: tuple[Path, str]
) -> None:
    cfg_dir, binary = rig
    first = _rebuild(tmp_path, cfg_dir, {"old-name": {"command": binary}})
    for servers in first.values():
        assert "old-name" in servers
    assert is_copied(first["kirocrew.json"]["old-name"])

    renamed = _rebuild(tmp_path, cfg_dir, {"new-name": {"command": binary}})
    for file, servers in renamed.items():
        assert "new-name" in servers, file
        assert "old-name" not in servers, f"{file} kept the copy of a renamed server"

    removed = _rebuild(tmp_path, cfg_dir, {})
    for file, servers in removed.items():
        assert "new-name" not in servers, f"{file} kept the copy of a removed server"


def test_a_numeric_suffixed_source_renamed_to_its_base_leaves(
    tmp_path: Path, rig: tuple[Path, str]
) -> None:
    """``srv-2`` is a plain source key, not a collision alias, so renaming it drops it."""
    cfg_dir, binary = rig
    _rebuild(tmp_path, cfg_dir, {"srv-2": {"command": binary}})
    for _ in range(2):
        for file, servers in _rebuild(tmp_path, cfg_dir, {"srv": {"command": binary}}).items():
            assert "srv" in servers, file
            assert "srv-2" not in servers, f"{file} kept the copy of a renamed server"


def test_a_removed_slash_key_leaves_beside_a_neighbour_family(
    tmp_path: Path, rig: tuple[Path, str]
) -> None:
    """``vendor/srv-2`` removed leaves even while a distinct ``vendor/srv`` stays."""
    cfg_dir, binary = rig
    other = tmp_path / "bin" / "other"
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)
    both = {"vendor/srv": {"command": binary}, "vendor/srv-2": {"command": str(other)}}
    for _ in range(2):
        assert "vendor-srv-2" in _rebuild(tmp_path, cfg_dir, both)["kirocrew.json"]
    for _ in range(2):
        for file, servers in _rebuild(
            tmp_path, cfg_dir, {"vendor/srv": both["vendor/srv"]}
        ).items():
            assert servers["vendor-srv"]["command"] == binary, file
            assert "vendor-srv-2" not in servers, f"{file} kept the removed vendor/srv-2"


def test_a_collision_alias_stays_while_its_slash_key_is_declared(
    tmp_path: Path, rig: tuple[Path, str]
) -> None:
    """A slash key landing on ``vendor-srv-2`` (its alias held) keeps that copy."""
    cfg_dir, binary = rig
    other = tmp_path / "bin" / "other"
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)
    both = {"vendor-srv": {"command": str(other)}, "vendor/srv": {"command": binary}}
    for _ in range(3):
        servers = _rebuild(tmp_path, cfg_dir, both)["kirocrew.json"]
        assert servers["vendor-srv-2"]["command"] == binary
        assert is_copied(servers["vendor-srv-2"])


def test_a_copy_stays_while_its_source_declares_it(tmp_path: Path, rig: tuple[Path, str]) -> None:
    cfg_dir, binary = rig
    for _ in range(3):
        servers = _rebuild(tmp_path, cfg_dir, {"keep": {"command": binary}})["kirocrew.json"]
        assert is_copied(servers["keep"])
        assert servers["keep"]["command"] == binary


def test_a_server_the_user_wrote_into_the_spec_stays(tmp_path: Path, rig: tuple[Path, str]) -> None:
    cfg_dir, binary = rig
    _rebuild(tmp_path, cfg_dir, {})
    _hand_write(tmp_path, "mine", {"command": binary})
    for _ in range(2):
        servers = _rebuild(tmp_path, cfg_dir, {})["kirocrew.json"]
        assert "mine" in servers
        assert not is_copied(servers["mine"])


def test_a_copy_left_by_an_older_build_is_not_removed(
    tmp_path: Path, rig: tuple[Path, str]
) -> None:
    """An unstamped copy cannot be told from the user's own entry, so it stays.

    The entry is what an older build left: the source still declares the name, and
    the spec's copy of it carries no stamp. Removing the source must not remove it.
    """
    cfg_dir, binary = rig
    _rebuild(tmp_path, cfg_dir, {"legacy": {"command": binary}})
    _hand_write(tmp_path, "legacy", {"command": binary})
    declared = _rebuild(tmp_path, cfg_dir, {"legacy": {"command": binary}})["kirocrew.json"]
    assert not is_copied(declared["legacy"]), "an existing unstamped entry was stamped"
    assert "legacy" in _rebuild(tmp_path, cfg_dir, {})["kirocrew.json"]


def test_an_alias_keyed_copy_keeps_its_stamp_and_leaves(
    tmp_path: Path, rig: tuple[Path, str]
) -> None:
    """A slash-keyed source renders under its alias; the stamp must follow it there."""
    cfg_dir, binary = rig
    for _ in range(2):
        servers = _rebuild(tmp_path, cfg_dir, {"vendor/srv": {"command": binary}})
        assert is_copied(servers["kirocrew.json"]["vendor-srv"])
    for file, servers in _rebuild(tmp_path, cfg_dir, {}).items():
        assert "vendor-srv" not in servers, f"{file} kept the alias-keyed copy"


def test_a_copy_folding_into_the_users_alias_entry_never_claims_it(
    tmp_path: Path, rig: tuple[Path, str]
) -> None:
    """An equivalent slash-keyed copy dedups onto the user's alias entry; it stays theirs."""
    cfg_dir, binary = rig
    _rebuild(tmp_path, cfg_dir, {})
    _hand_write(tmp_path, "vendor-srv", {"command": binary})
    for _ in range(2):
        servers = _rebuild(tmp_path, cfg_dir, {"vendor/srv": {"command": binary}})
        assert not is_copied(servers["kirocrew.json"]["vendor-srv"])
    assert "vendor-srv" in _rebuild(tmp_path, cfg_dir, {})["kirocrew.json"]


def test_a_users_duplicate_sibling_keeps_the_folded_alias_theirs(
    tmp_path: Path, rig: tuple[Path, str]
) -> None:
    """A stamped alias that a user's equivalent ``vendor-srv-2`` folds into is theirs."""
    cfg_dir, binary = rig
    _rebuild(tmp_path, cfg_dir, {"vendor/srv": {"command": binary}})
    _hand_write(tmp_path, "vendor-srv-2", {"command": binary})
    servers = _rebuild(tmp_path, cfg_dir, {"vendor/srv": {"command": binary}})["kirocrew.json"]
    assert not any(is_copied(servers[k]) for k in servers if k.startswith("vendor-srv"))
    left = _rebuild(tmp_path, cfg_dir, {})["kirocrew.json"]
    assert any(k.startswith("vendor-srv") for k in left), "the user's server was removed"


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
def test_the_dashboards_copy_is_stamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch: bool
) -> None:
    from kiro_crew.dashboard.handlers import agents
    from kiro_crew.dashboard.handlers import mcp as mcp_mod
    from kiro_crew.dashboard.handlers import mcp_agent_sync

    spec = tmp_path / "kirocrew.json"
    spec.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    kiro_global = tmp_path / "mcp.json"
    kiro_global.write_text(json.dumps({"mcpServers": {"srv": {"command": "x"}}}), "utf-8")
    monkeypatch.setattr(agents, "_installed_agent_config", lambda: spec)
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", kiro_global)
    monkeypatch.setattr(mcp_mod, "may_skip_gate_now", lambda _ref: True)
    monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
    if batch:
        mcp_agent_sync._sync_mcp_to_agent_batch_unlocked(["srv"], True)
    else:
        mcp_agent_sync._sync_mcp_to_agent_unlocked("srv", True)
    assert is_copied(json.loads(spec.read_text(encoding="utf-8"))["mcpServers"]["srv"])


def test_nothing_is_removed_while_an_mcp_config_cannot_be_read(
    tmp_path: Path, rig: tuple[Path, str]
) -> None:
    cfg_dir, binary = rig
    _rebuild(tmp_path, cfg_dir, {"old-name": {"command": binary}})
    store = tmp_path / "kirocrew_home" / "mcp.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("{not json", encoding="utf-8")
    assert "old-name" in _rebuild(tmp_path, cfg_dir, {})["kirocrew.json"]

    store.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    assert "old-name" not in _rebuild(tmp_path, cfg_dir, {})["kirocrew.json"]


def test_a_directory_in_place_of_a_config_blocks_removal(
    tmp_path: Path, rig: tuple[Path, str]
) -> None:
    cfg_dir, binary = rig
    _rebuild(tmp_path, cfg_dir, {"old-name": {"command": binary}})
    store = tmp_path / "kirocrew_home" / "mcp.json"
    store.mkdir(parents=True)
    assert "old-name" in _rebuild(tmp_path, cfg_dir, {})["kirocrew.json"]


def test_a_copy_the_store_still_declares_stays(tmp_path: Path, rig: tuple[Path, str]) -> None:
    cfg_dir, binary = rig
    run_install_mcp_merge(
        tmp_path,
        cfg_dir,
        cc_servers={},
        kiro_servers={"both": {"command": binary}},
        kirocrew_servers={"both": {"command": binary}},
    )
    out = run_install_mcp_merge(
        tmp_path,
        cfg_dir,
        cc_servers={},
        kiro_servers={},
        kirocrew_servers={"both": {"command": binary}},
    )
    assert "both" in out["mcpServers"]


class TestStampShape:
    def test_the_stamp_keeps_a_command_record(self) -> None:
        entry = mark_copied(record_derived({"command": "/abs/npx"}, ("npx", "/abs/npx")))
        assert is_copied(entry)
        assert recorded_source(entry) == ("npx", "/abs/npx")
        assert copied_key(entry) is None
        assert copied_key(mark_copied(entry, "vendor/srv")) == "vendor/srv"

    @pytest.mark.parametrize(
        "entry",
        [
            {"command": "x"},
            {DERIVED_KEY: {"copied": "yes"}},
            {DERIVED_KEY: True},
            None,
        ],
    )
    def test_anything_but_the_exact_flag_reads_as_not_copied(self, entry: object) -> None:
        assert not is_copied(entry)


class TestReadMcpServers:
    def test_a_missing_file_is_an_empty_readable_config(self, tmp_path: Path) -> None:
        assert user_json.read_mcp_servers(tmp_path / "absent.json") == {}

    @pytest.mark.parametrize("text", ["{not json", "[]", '{"mcpServers": []}'])
    def test_a_present_file_that_cannot_be_read_is_none(self, tmp_path: Path, text: str) -> None:
        path = tmp_path / "mcp.json"
        path.write_text(text, encoding="utf-8")
        assert user_json.read_mcp_servers(path) is None
        assert user_json.load_mcp_servers(path) == {}

    def test_a_directory_in_the_files_place_is_none(self, tmp_path: Path) -> None:
        path = tmp_path / "mcp.json"
        path.mkdir()
        assert user_json.read_mcp_servers(path) is None
        assert user_json.load_mcp_servers(path) == {}
