import json

from kiro_crew import agent
from kiro_crew.platform.governance import withhold_prompt_only_wildcards


def _wildcard_config() -> dict[str, object]:
    return {"tools": ["*"], "allowedTools": ["*", "fs_read"]}


def _write_project_settings(tmp_path, servers: dict[str, object]) -> None:
    settings = tmp_path / ".kiro" / "settings" / "mcp.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def test_jsonc_global_prompt_only_server_withholds_wildcard(tmp_path, monkeypatch) -> None:
    settings = tmp_path / "global-mcp.json"
    settings.write_text(
        '{\n  // Kiro accepts comments here.\n  "mcpServers": {\n'
        '    "kirocrew-apps": {"command": "kirocrew"},\n  },\n}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(agent, "_KIRO_MCP_JSON", settings)
    config = _wildcard_config()

    assert withhold_prompt_only_wildcards(config) == ["*"]
    assert config["allowedTools"] == ["fs_read"]


def test_project_prompt_only_server_withholds_wildcard(tmp_path, monkeypatch) -> None:
    global_settings = tmp_path / "global-mcp.json"
    monkeypatch.setattr(agent, "_KIRO_MCP_JSON", global_settings)
    project_settings = tmp_path / ".kiro" / "settings" / "mcp.json"
    project_settings.parent.mkdir(parents=True)
    project_settings.write_text(
        '{\n  // Workspace settings use JSONC too.\n  "mcpServers": {\n'
        '    "kirocrew-apps": {"command": "kirocrew"},\n  },\n}\n',
        encoding="utf-8",
    )
    config = _wildcard_config()

    assert withhold_prompt_only_wildcards(config, work_dir=tmp_path) == ["*"]
    assert config["allowedTools"] == ["fs_read"]


def test_project_disabled_entry_does_not_hide_global_enabled_entry(tmp_path, monkeypatch) -> None:
    global_settings = tmp_path / "global-mcp.json"
    global_settings.write_text(
        json.dumps({"mcpServers": {"kirocrew-apps": {"command": "kirocrew"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(agent, "_KIRO_MCP_JSON", global_settings)
    _write_project_settings(tmp_path, {"kirocrew-apps": {"disabled": True}})
    config = _wildcard_config()

    assert withhold_prompt_only_wildcards(config, work_dir=tmp_path) == ["*"]
    assert config["allowedTools"] == ["fs_read"]


def test_project_settings_are_ignored_without_work_dir(tmp_path, monkeypatch) -> None:
    global_settings = tmp_path / "global-mcp.json"
    monkeypatch.setattr(agent, "_KIRO_MCP_JSON", global_settings)
    _write_project_settings(tmp_path, {"kirocrew-apps": {"command": "kirocrew"}})
    config = _wildcard_config()

    assert withhold_prompt_only_wildcards(config) == []
    assert config["allowedTools"] == ["*", "fs_read"]


def test_unreadable_existing_settings_fail_closed_but_absent_file_does_not(
    tmp_path, monkeypatch
) -> None:
    settings = tmp_path / "global-mcp.json"
    monkeypatch.setattr(agent, "_KIRO_MCP_JSON", settings)
    absent_config = _wildcard_config()

    assert withhold_prompt_only_wildcards(absent_config) == []

    settings.write_text("{", encoding="utf-8")
    unreadable_config = _wildcard_config()
    assert withhold_prompt_only_wildcards(unreadable_config) == ["*"]
    assert unreadable_config["allowedTools"] == ["fs_read"]
