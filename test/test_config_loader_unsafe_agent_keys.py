"""The loader drops a roster row whose key or member id is not one path component.

A stored config.json can carry any key. The key and the row's ``member_id`` are joined into
file paths, so a row with one that names a path rather than a file is not part of the
roster. It is dropped and the read is flagged as a roster repair; a ``member_id`` is never
blanked, because an empty one would let the upgrade mint a new identity for the row.
"""

from __future__ import annotations

import pytest
from config_loader_helpers import _load_from_dict, _load_from_dict_with_logs


@pytest.mark.parametrize("key", ["../x", "a/b", "..", ".", "a\\b", "a\nb"])
def test_a_key_that_is_not_one_path_component_is_dropped(key) -> None:
    cfg, logs = _load_from_dict_with_logs(
        {"agents": {key: {"member_id": "m1"}, "keep": {"member_id": "m2"}}}
    )
    assert set(cfg.agents) >= {"keep"}
    assert key not in cfg.agents
    assert [m for m in logs if "single path component" in m], logs
    assert not [m for m in logs if "m1" in m], "the log line must not echo the row"


@pytest.mark.parametrize("member_id", ["../x", "a/b", "..", "a\nb"])
def test_a_row_with_such_a_member_id_is_dropped_not_blanked(member_id) -> None:
    cfg = _load_from_dict({"agents": {"bad": {"member_id": member_id}, "keep": {}}})
    assert "bad" not in cfg.agents
    assert "keep" in cfg.agents


def test_the_drop_is_reported_as_a_roster_repair() -> None:
    cfg = _load_from_dict({"agents": {"../x": {}, "keep": {}}})
    assert "agents" in cfg.degraded_sections


def test_keys_with_dots_spaces_and_cjk_are_kept() -> None:
    names = {"my.pkg": {"member_id": "m.1"}, "my crew": {}, "小明": {}}
    cfg = _load_from_dict({"agents": names})
    assert set(names) <= set(cfg.agents)
    assert cfg.agents["my.pkg"].member_id == "m.1"
    assert "agents" not in cfg.degraded_sections
