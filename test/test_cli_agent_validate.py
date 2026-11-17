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
    assert f"{path}: tools[1]: '' is empty" in lines
    assert f"{path}: allowedTools: must be a list of strings" in lines
    assert f"{path}: model: must be a string" in lines
    assert len(lines) == 4
    assert out == "Checked 1 agent spec(s): 4 problem(s).\n"


def test_a_grant_every_tool_spec_and_a_whole_server_ref_are_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path, "everything", {"tools": "*", "allowedTools": ["@git/"]})
    _run(str(path))
    captured = capsys.readouterr()
    assert captured.out == "Checked 1 agent spec(s): 0 problem(s).\n"
    assert captured.err == ""


def test_a_directory_checks_json_and_markdown_specs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, "good", {})
    _write(tmp_path, "bad-json", {"colour": "red"})
    (tmp_path / "bad-md.md").write_text(
        "---\nname: bad-md\nincludeMcpJson: false\ntools: ['']\n---\nPrompt body\n",
        encoding="utf-8",
    )
    out, err = _fails(capsys, str(tmp_path))
    assert f"{tmp_path / 'bad-json.json'}: colour: unknown field" in err
    assert f"{tmp_path / 'bad-md.md'}: tools[0]: '' is empty" in err
    assert "good.json" not in err
    assert out == "Checked 3 agent spec(s): 2 problem(s).\n"


def test_a_directory_skips_plain_markdown_and_macos_sidecars(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A README or a prompt fragment with no frontmatter fence, and a ``._`` sidecar
    holding the AppleDouble magic, are not specs; the agent readers skip them too."""
    _write(tmp_path, "good", {})
    (tmp_path / "README.md").write_text("# Our agents\n\nNotes for the team.\n", encoding="utf-8")
    (tmp_path / "._good.json").write_bytes(b"\x00\x05\x16\x07" + b"\x00" * 60)
    _run(str(tmp_path))
    captured = capsys.readouterr()
    assert captured.out == "Checked 1 agent spec(s): 0 problem(s).\n"
    assert captured.err == ""


def test_a_fenced_markdown_spec_that_does_not_parse_is_still_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "cut.md").write_text("---\nname: cut\n", encoding="utf-8")
    out, err = _fails(capsys, str(tmp_path))
    assert f"{tmp_path / 'cut.md'}: cannot be parsed" in err
    assert out == "Checked 1 agent spec(s): 1 problem(s).\n"


def test_a_spec_nested_too_deeply_is_one_line_and_the_rest_are_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deep = '{"name": "deep", "resources": ' + "[" * 20000 + "]" * 20000 + "}"
    (tmp_path / "a-deep.json").write_text(deep, encoding="utf-8")
    _write(tmp_path, "b-bad", {"colour": "red"})
    out, err = _fails(capsys, str(tmp_path))
    assert f"{tmp_path / 'a-deep.json'}: cannot be parsed: it is nested too deeply" in err
    assert f"{tmp_path / 'b-bad.json'}: colour: unknown field" in err
    assert out == "Checked 2 agent spec(s): 2 problem(s).\n"


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


def test_a_network_share_outside_the_trusted_roots_is_refused_before_any_probe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """On Windows, probing a UNC path opens an SMB connection to the host it names."""
    from kiro_crew import cli_agent_validate as mod

    monkeypatch.setattr(mod, "_WINDOWS", True)
    monkeypatch.setattr(mod, "unc_probe_allowed", lambda raw: False)
    probed: list[str] = []
    real_exists = Path.exists

    def exists(self: Path, *args: object, **kwargs: object) -> bool:
        probed.append(str(self))
        return real_exists(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "exists", exists)
    out, err = _fails(capsys, "//attacker/share/spec.json")
    assert "names a network share outside the trusted roots" in err
    assert not [p for p in probed if "attacker" in p]
    assert out == ""


def test_a_file_is_read_through_the_agent_spec_readers_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each file goes through the agent readers' own gate, and its refusal is one line."""
    from kiro_crew import agent_discovery

    path = _write(tmp_path, "linked", {})
    seen: list[tuple[Path, str, str]] = []

    def refuse(target: Path, *, operation: str, source: str) -> None:
        seen.append((target, operation, source))
        raise agent_discovery.SensitiveAgentSpecPathError(f"{target} resolves to a sensitive path")

    monkeypatch.setattr(agent_discovery, "read_agent_spec_strict", refuse)
    out, err = _fails(capsys, str(path))
    assert err.splitlines() == [f"{path}: is refused: resolves to a sensitive path"]
    assert seen == [(path, "agent_validate", "cli")]
    assert out == "Checked 1 agent spec(s): 1 problem(s).\n"
