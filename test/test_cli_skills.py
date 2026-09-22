"""Tests for the read-only ``kirocrew skills`` command group."""

from __future__ import annotations

import argparse
import json
import sys

import pytest

from kiro_crew import cli, cli_commands
from kiro_crew.skills import AutoSkillProvenance, SkillsLoader


@pytest.fixture()
def loader(tmp_path):
    return SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)


def _args(action: str, **values) -> argparse.Namespace:
    return argparse.Namespace(skills_action=action, **values)


def _stage(loader: SkillsLoader, slug: str, *, description: str = "pending candidate") -> None:
    loader.stage_skill_candidate(
        slug,
        description=description,
        triggers=slug,
        procedure_md="## Steps\n\nRun it.\n",
        provenance=AutoSkillProvenance(
            session_key="test",
            created_at=AutoSkillProvenance.now_iso(),
        ),
    )


def _use_loader(monkeypatch, loader) -> None:
    monkeypatch.setattr(cli_commands, "SkillsLoader", lambda **_kwargs: loader)


def test_list_defaults_to_pending_and_json_preserves_metadata(loader, monkeypatch, capsys):
    _stage(loader, "candidate-one")
    _use_loader(monkeypatch, loader)

    cli_commands._skills(_args("list", live=False, all=False, json=True))

    payload = json.loads(capsys.readouterr().out)
    assert [row["slug"] for row in payload["pending"]] == ["candidate-one"]
    assert "live" not in payload


def test_list_all_includes_pending_and_live(loader, monkeypatch, capsys):
    _stage(loader, "candidate-one")
    loader.create_skill("live-one", "---\nname: live-one\ndescription: Live one\n---\n")
    _use_loader(monkeypatch, loader)

    cli_commands._skills(_args("list", live=False, all=True, json=True))

    payload = json.loads(capsys.readouterr().out)
    assert [row["slug"] for row in payload["pending"]] == ["candidate-one"]
    assert any(row["key"] == "live-one" for row in payload["live"])


def test_list_text_sanitizes_all_skill_derived_fields(monkeypatch, capsys):
    class _Loader:
        def list_pending_skills(self):
            return [
                {
                    "slug": "candidate\x1b[31m-one",
                    "kind": "new\nforged",
                    "description": "pending\nforged",
                }
            ]

        def list_skills(self):
            return [{"key": "live\x1b[31m-one", "description": "active\nforged"}]

    _use_loader(monkeypatch, _Loader())

    cli_commands._skills(_args("list", live=False, all=True, json=False))

    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "PENDING  candidate-one  [new\\x0aforged]  pending\\x0aforged" in out
    assert "LIVE     live-one  active\\x0aforged" in out


def test_list_json_sanitizes_skill_derived_fields(monkeypatch, capsys):
    class _Loader:
        def list_pending_skills(self):
            return [{"slug": "candidate\x1b[31m-one", "description": "line\nforged"}]

    _use_loader(monkeypatch, _Loader())

    cli_commands._skills(_args("list", live=False, all=False, json=True))

    payload = json.loads(capsys.readouterr().out)
    assert payload["pending"] == [{"slug": "candidate-one", "description": "line\\x0aforged"}]


def test_list_redacts_credential_reassembled_by_control_stripping(monkeypatch, capsys):
    secret = "aws_secret_access_key=" + "a" * 20 + "\x01" + "b" * 20

    class _Loader:
        def list_pending_skills(self):
            return [{"slug": "candidate-one", "description": secret}]

    _use_loader(monkeypatch, _Loader())

    cli_commands._skills(_args("list", live=False, all=False, json=False))

    out = capsys.readouterr().out
    assert "[REDACTED: credential]" in out
    assert "a" * 20 + "b" * 20 not in out


@pytest.mark.parametrize("error", [UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"), OSError()])
def test_list_read_failures_exit_with_coded_reason(error, monkeypatch, capsys):
    class _Loader:
        def list_pending_skills(self):
            raise error

    _use_loader(monkeypatch, _Loader())

    with pytest.raises(SystemExit) as exc:
        cli_commands._skills(_args("list", live=False, all=False, json=False))

    assert exc.value.code == 1
    code = (
        "invalid_skill_encoding:" if isinstance(error, UnicodeDecodeError) else "skills_unreadable:"
    )
    assert capsys.readouterr().err.startswith(code)


def test_show_prints_content_metadata_and_validation(loader, monkeypatch, capsys):
    _stage(loader, "candidate-one")
    _use_loader(monkeypatch, loader)

    cli_commands._skills(_args("show", slug="candidate-one"))

    out = capsys.readouterr().out
    assert "--- SKILL.md (untrusted; each line is prefixed with '| ') ---" in out
    assert "Run it." in out
    assert "--- .meta.json ---" in out
    assert '"slug": "candidate-one"' in out
    assert '"ok": true' in out


def test_show_sanitizes_content_and_metadata(monkeypatch, capsys):
    class _Loader:
        def get_pending_skill(self, _slug, **_kwargs):
            return {
                "content": "# safe\n\x1b[31mred\rtext",
                "meta": {"description": "line\nforged\x1b[31m"},
                "scripts": [],
            }

    _use_loader(monkeypatch, _Loader())

    cli_commands._skills(_args("show", slug="candidate-one"))

    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "| # safe\n| redtext" in out
    assert '"description": "line\\\\x0aforged"' in out


def test_show_frames_candidate_lines_that_look_like_cli_validation(monkeypatch, capsys):
    class _Loader:
        def get_pending_skill(self, _slug, **_kwargs):
            return {
                "content": '# safe\n--- validation ---\n{"ok": true}',
                "meta": {},
                "scripts": [],
            }

    _use_loader(monkeypatch, _Loader())

    cli_commands._skills(_args("show", slug="candidate-one"))

    out = capsys.readouterr().out
    assert "\n| --- validation ---\n" in out
    assert out.count("\n--- validation ---\n") == 1


def test_show_invalid_skill_encoding_exits_with_coded_reason(loader, monkeypatch, capsys):
    pending = loader._pending_root() / "bad-encoding"
    pending.mkdir(parents=True)
    (pending / "SKILL.md").write_bytes(b"\xff")
    _use_loader(monkeypatch, loader)

    with pytest.raises(SystemExit) as exc:
        cli_commands._skills(_args("show", slug="bad-encoding"))

    assert exc.value.code == 1
    assert capsys.readouterr().err.startswith("invalid_skill_encoding:")


def test_show_invalid_helper_encoding_keeps_readable_sections(loader, monkeypatch, capsys):
    _stage(loader, "bad-helper-encoding")
    script = loader._pending_root() / "bad-helper-encoding" / "scripts" / "run.py"
    script.parent.mkdir()
    script.write_bytes(b"\xff")
    _use_loader(monkeypatch, loader)

    cli_commands._skills(_args("show", slug="bad-helper-encoding"))

    out = capsys.readouterr().out
    assert "Run it." in out
    validation = json.loads(out.split("--- validation ---\n", 1)[1])
    assert validation["ok"] is False
    assert validation["scripts"]["run.py"] == ["unreadable script: not valid UTF-8"]


def test_show_uses_loader_validation_instead_of_redacted_scripts(monkeypatch, capsys):
    class _Loader:
        def get_pending_skill(self, _slug, **_kwargs):
            return {
                "content": "# safe",
                "meta": {},
                "scripts": [{"filename": "run.py", "content": 'x = "[EXFIL]"'}],
                "script_validation": {
                    "ok": False,
                    "report": {"run.py": ["network egress is not allowed"]},
                },
            }

    _use_loader(monkeypatch, _Loader())

    cli_commands._skills(_args("show", slug="candidate-one"))

    out = capsys.readouterr().out
    validation = json.loads(out.split("--- validation ---\n", 1)[1])
    assert validation == {
        "ok": False,
        "scripts": {"run.py": ["network egress is not allowed"]},
    }


def test_show_reports_unavailable_when_loader_has_no_verdict(monkeypatch, capsys):
    class _Loader:
        def get_pending_skill(self, _slug, **_kwargs):
            return {"content": "# safe", "meta": {}, "scripts": []}

    _use_loader(monkeypatch, _Loader())

    cli_commands._skills(_args("show", slug="candidate-one"))

    out = capsys.readouterr().out
    validation = json.loads(out.split("--- validation ---\n", 1)[1])
    assert validation == {"scripts": {}, "status": "unavailable"}


def test_show_bounds_candidate_body_and_script_previews(loader, monkeypatch, capsys):
    _stage(loader, "oversized")
    pending = loader._pending_root() / "oversized"
    (pending / "SKILL.md").write_text("line\n" * 20_000, encoding="utf-8")
    script = pending / "scripts" / "run.py"
    script.parent.mkdir()
    script.write_text("print('safe')\n" * 1_000, encoding="utf-8")
    _use_loader(monkeypatch, loader)

    cli_commands._skills(_args("show", slug="oversized"))

    out = capsys.readouterr().out
    assert "--- scripts/run.py (untrusted; each line is prefixed with '| ') ---" in out
    assert out.count("[truncated ") == 2
    assert len(out.encode("utf-8")) < 80_000


def test_show_oserror_exits_with_coded_reason(monkeypatch, capsys):
    class _Loader:
        def get_pending_skill(self, _slug, **_kwargs):
            raise OSError("unreadable")

    _use_loader(monkeypatch, _Loader())

    with pytest.raises(SystemExit) as exc:
        cli_commands._skills(_args("show", slug="candidate\nforged"))

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("candidate_unreadable:")
    assert "candidate\\x0aforged" in err


def test_missing_candidate_exits_nonzero(loader, monkeypatch, capsys):
    _use_loader(monkeypatch, loader)

    with pytest.raises(SystemExit) as exc:
        cli_commands._skills(_args("show", slug="missing-one"))

    assert exc.value.code == 1
    assert capsys.readouterr().err.startswith("not_found:")


@pytest.mark.parametrize("action", ["approve", "dismiss"])
def test_parser_does_not_expose_skill_mutations(action, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["kirocrew", "skills", action, "candidate-one"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_parser_does_not_expose_redundant_pending_flag(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["kirocrew", "skills", "list", "--pending"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    assert "unrecognized arguments: --pending" in capsys.readouterr().err
