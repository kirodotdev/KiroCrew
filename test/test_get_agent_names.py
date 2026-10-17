"""Tests for _get_agent_names() — verifies agent dropdown returns JSON name field, not file stem."""

from __future__ import annotations

import json

import pytest

from kiro_crew.slack.events import _get_agent_names


def _write_agent(agents_dir, filename: str, data: dict) -> None:
    (agents_dir / filename).write_text(json.dumps(data))


@pytest.fixture()
def agents_dir(tmp_path, monkeypatch):
    # KIRO_HOME is honored by kiro_home() on every OS; HOME alone does not
    # isolate the agents dir on Windows (Path.home() reads USERPROFILE there),
    # so the test would read the real ~/.kiro/agents and see installed agents.
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / ".kiro"))
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / ".kiro" / "agents"
    d.mkdir(parents=True)
    return d


def test_returns_name_field_not_stem(agents_dir):
    """Core fix: dropdown values should be the JSON 'name' field."""
    _write_agent(agents_dir, "local-MyPkg-cool-agent.json", {"name": "cool-agent"})
    assert _get_agent_names() == ["cool-agent"]


def test_multiple_agents_sorted(agents_dir):
    _write_agent(agents_dir, "local-B-zebra.json", {"name": "zebra"})
    _write_agent(agents_dir, "local-A-alpha.json", {"name": "alpha"})
    _write_agent(agents_dir, "kirocrew.json", {"name": "kirocrew"})
    assert _get_agent_names() == ["alpha", "kirocrew", "zebra"]


def test_falls_back_to_stem_when_name_missing(agents_dir):
    _write_agent(agents_dir, "legacy-agent.json", {"mcpServers": {}})
    assert _get_agent_names() == ["legacy-agent"]


def test_falls_back_to_stem_on_invalid_json(agents_dir):
    (agents_dir / "broken.json").write_text("{invalid json")
    assert _get_agent_names() == ["broken"]


def test_empty_when_no_agents_dir(tmp_path, monkeypatch):
    # KIRO_HOME isolates the agents dir cross-platform (see agents_dir fixture).
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / ".kiro"))
    monkeypatch.setenv("HOME", str(tmp_path))
    # No .kiro/agents directory exists
    assert _get_agent_names() == []


def test_ignores_non_json_files(agents_dir):
    _write_agent(agents_dir, "real-agent.json", {"name": "real"})
    (agents_dir / "README.md").write_text("not an agent")
    assert _get_agent_names() == ["real"]


def test_falls_back_to_stem_on_non_dict_json(agents_dir):
    """Regression: list/scalar JSON must not raise AttributeError.

    ``data.get("name")`` on a non-dict crashes the dropdown; we must
    fall back to the filename stem instead.
    """
    (agents_dir / "array-root.json").write_text(json.dumps([1, 2, 3]))
    (agents_dir / "scalar-root.json").write_text(json.dumps("hello"))
    assert _get_agent_names() == ["array-root", "scalar-root"]


def test_falls_back_to_stem_when_name_is_null(agents_dir):
    """Regression: ``{"name": null}`` must not produce None in the result.

    ``None`` in the names list would crash ``sorted()`` with TypeError.
    """
    _write_agent(agents_dir, "null-name.json", {"name": None})
    assert _get_agent_names() == ["null-name"]


def test_non_utf8_agent_file_does_not_crash(agents_dir):
    """Regression (sibling of the 90e3cccc agent.py fix): a non-UTF-8 *.json
    (e.g. a macOS AppleDouble "._foo.json" resource-fork stub) must be skipped,
    not raise UnicodeDecodeError.

    safe_read_file opens with encoding='utf-8'; the read raises
    UnicodeDecodeError (a ValueError subclass, NOT an OSError), which escaped the
    old (json.JSONDecodeError, OSError) except and killed the `/kirocrew
    channels` handler / channel-modal refresh task before the modal opened.
    """
    _write_agent(agents_dir, "real-agent.json", {"name": "real"})
    (agents_dir / "._bad.json").write_bytes(
        b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X\xa3\xff\xfe" + b"\x00" * 16
    )
    # Must not raise; the bad file falls back to its stem, valid one resolves.
    assert _get_agent_names() == ["._bad", "real"]
