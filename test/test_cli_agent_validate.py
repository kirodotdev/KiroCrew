"""``kirocrew agent validate``: offline spec checks, one line per problem, exit 1 on any.

Runs the real parser and dispatcher (``kiro_crew.cli.main``) over spec files in a
temporary directory. A spec states ``includeMcpJson: false`` unless a test is about
the ``mcp.json`` servers, so no host settings file decides the outcome.
"""

from __future__ import annotations

import json
import unittest.mock
from pathlib import Path

import pytest

from kiro_crew.cli import main
from kiro_crew.config.paths import kiro_agents_dir


def _run(*argv: str) -> None:
    with unittest.mock.patch("sys.argv", ["kirocrew", "agent", "validate", *argv]):
        main()


def _write(directory: Path, name: str, spec: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps({"name": name, "includeMcpJson": False, **spec}), encoding="utf-8")
    return path


def _fails(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[str, str]:
    with pytest.raises(SystemExit) as exc:
        _run(*argv)
    assert exc.value.code == 1
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_a_clean_spec_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write(tmp_path, "clean", {"mcpServers": {"git": {"command": "g"}}, "tools": ["@git"]})
    _run(str(path))
    captured = capsys.readouterr()
    assert captured.out == "Checked 1 agent spec(s): 0 problem(s).\n"
    assert captured.err == ""


def test_each_problem_is_one_line_naming_file_and_field(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(
        tmp_path,
        "broken",
        {"colour": "red", "tools": ["@ghost", ""], "allowedTools": "read", "model": 3},
    )
    out, err = _fails(capsys, str(path))
    lines = err.splitlines()
    assert f"{path}: colour: unknown field" in lines
    assert f"{path}: tools: names @ghost, which no server this spec can reach defines" in lines
    assert f"{path}: tools[1]: '' is empty" in lines
    assert f"{path}: allowedTools: must be a list of strings" in lines
    assert f"{path}: model: must be a string" in lines
    assert len(lines) == 5
    assert out == "Checked 1 agent spec(s): 5 problem(s).\n"


def test_servers_crew_adds_to_a_custom_agent_are_not_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path, "uses-core", {"tools": ["@kirocrew-core"]})
    _run(str(path))
    assert capsys.readouterr().err == ""


def test_a_directory_checks_json_and_markdown_specs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, "good", {})
    _write(tmp_path, "bad-json", {"colour": "red"})
    (tmp_path / "bad-md.md").write_text(
        "---\nname: bad-md\nincludeMcpJson: false\ntools: '@ghost'\n---\nPrompt body\n",
        encoding="utf-8",
    )
    out, err = _fails(capsys, str(tmp_path))
    assert f"{tmp_path / 'bad-json.json'}: colour: unknown field" in err
    assert (
        f"{tmp_path / 'bad-md.md'}: tools: names @ghost, which no server this spec can reach defines"
        in err
    )
    assert "good.json" not in err
    assert out == "Checked 3 agent spec(s): 2 problem(s).\n"


def test_no_path_checks_the_user_level_agents_directory(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write(kiro_agents_dir(), "mine", {"colour": "red"})
    _, err = _fails(capsys)
    assert f"{kiro_agents_dir() / 'mine.json'}: colour: unknown field" in err


def test_a_spec_that_will_not_parse_is_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "torn.json"
    path.write_text('{"name": ', encoding="utf-8")
    out, err = _fails(capsys, str(path))
    assert err.startswith(f"{path}: cannot be parsed: ")
    assert len(err.splitlines()) == 1
    assert out == "Checked 1 agent spec(s): 1 problem(s).\n"


def test_a_missing_path_is_an_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, err = _fails(capsys, str(tmp_path / "absent.json"))
    assert err.startswith("Error: ")
    assert "does not exist" in err


def test_allow_unknown_fields_lets_a_newer_field_through(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path, "newer", {"someNewField": 1})
    _fails(capsys, str(path))
    _run(str(path), "--allow-unknown-fields")
    assert capsys.readouterr().err == ""


def test_a_control_sequence_in_a_field_name_is_not_echoed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path, "sneaky", {"bad\x1b]0;owned\x07field": 1})
    _, err = _fails(capsys, str(path))
    assert "\x1b" not in err
    assert "\x07" not in err


def test_validate_runs_when_the_config_does_not_load(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path, "offline", {})
    with unittest.mock.patch(
        "kiro_crew.cli_commands_agent.KiroCrewConfig.load", side_effect=RuntimeError("no config")
    ):
        _run(str(path))
    assert "0 problem(s)" in capsys.readouterr().out


def test_print_schema_prints_the_published_schema(capsys: pytest.CaptureFixture[str]) -> None:
    from kiro_crew.agent_spec_schema import FIELDS

    _run("--print-schema")
    published = json.loads(capsys.readouterr().out)
    assert set(published["properties"]) == {f.name for f in FIELDS}
    assert published["additionalProperties"] is False
