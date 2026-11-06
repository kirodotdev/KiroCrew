"""Agent files that existed before the KAS mcp.json exclusion keep KAS's earlier behaviour."""

from __future__ import annotations

import json

import pytest

from kiro_crew.acp import kas_mcp_json_grandfather as gf


@pytest.fixture
def agents(tmp_path):
    root = tmp_path / "agents"
    root.mkdir()
    return root


def _write(path, data):
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def test_a_file_without_the_key_is_kept_by_stem_and_declared_name(agents):
    _write(agents / "pkg-helper.json", {"name": "helper", "tools": ["*"]})
    assert gf.grandfathered("helper", agents)
    assert gf.grandfathered("pkg-helper", agents)


@pytest.mark.parametrize(
    "key", [{"includeMcpJson": False}, {"includeMcpJson": True}, {"useLegacyMcpJson": False}]
)
def test_a_file_that_states_the_key_is_not_kept(agents, key):
    _write(agents / "a.json", {"name": "a", **key})
    assert not gf.grandfathered("a", agents)


def test_the_list_is_recorded_once(agents):
    _write(agents / "old.json", {"name": "old"})
    assert gf.grandfathered("old", agents)
    recorded = gf.list_path().read_text(encoding="utf-8")
    _write(agents / "new.json", {"name": "new"})
    assert not gf.grandfathered("new", agents)
    assert gf.grandfathered("old", agents)
    assert gf.list_path().read_text(encoding="utf-8") == recorded


def test_a_bad_file_is_skipped_and_the_rest_recorded(agents):
    _write(agents / "broken.json", "{ not json")
    _write(agents / "good.json", {"name": "good"})
    assert gf.grandfathered("good", agents)
    assert not gf.grandfathered("broken", agents)


def test_a_malformed_list_keeps_nobody_and_is_not_rebuilt(agents):
    _write(agents / "old.json", {"name": "old"})
    gf.list_path().parent.mkdir(parents=True, exist_ok=True)
    _write(gf.list_path(), "[]")
    assert not gf.grandfathered("old", agents)
    assert gf.list_path().read_text(encoding="utf-8") == "[]"


def test_no_agents_directory_records_an_empty_list(tmp_path):
    assert not gf.grandfathered("a", tmp_path / "absent")
    assert json.loads(gf.list_path().read_text(encoding="utf-8")) == {"agents": []}
