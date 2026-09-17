"""Tests for the ``kirocrew skills`` command group."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import cli_commands
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

    cli_commands._skills(_args("list", pending=False, live=False, all=False, json=True))

    payload = json.loads(capsys.readouterr().out)
    assert [row["slug"] for row in payload["pending"]] == ["candidate-one"]
    assert "live" not in payload


def test_list_all_includes_pending_and_live(loader, monkeypatch, capsys):
    _stage(loader, "candidate-one")
    loader.create_skill("live-one", "---\nname: live-one\ndescription: Live one\n---\n")
    _use_loader(monkeypatch, loader)

    cli_commands._skills(_args("list", pending=False, live=False, all=True, json=True))

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

    cli_commands._skills(_args("list", pending=False, live=False, all=True, json=False))

    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "PENDING  candidate-one  [new\\x0aforged]  pending\\x0aforged" in out
    assert "LIVE     live-one  active\\x0aforged" in out


def test_list_live_decode_failure_exits_with_coded_reason(loader, monkeypatch, capsys):
    def _invalid_utf8():
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(loader, "list_skills", _invalid_utf8)
    _use_loader(monkeypatch, loader)

    with pytest.raises(SystemExit) as exc:
        cli_commands._skills(_args("list", pending=False, live=True, all=False, json=False))

    assert exc.value.code == 1
    assert capsys.readouterr().err.startswith("invalid_skill_encoding:")


def test_show_prints_content_metadata_and_validation(loader, monkeypatch, capsys):
    _stage(loader, "candidate-one")
    _use_loader(monkeypatch, loader)

    cli_commands._skills(_args("show", slug="candidate-one"))

    out = capsys.readouterr().out
    assert "--- SKILL.md ---" in out
    assert "Run it." in out
    assert "--- .meta.json ---" in out
    assert '"slug": "candidate-one"' in out
    assert '"ok": true' in out


def test_show_invalid_skill_encoding_exits_with_coded_reason(loader, monkeypatch, capsys):
    pending = loader._pending_root() / "bad-encoding"
    pending.mkdir(parents=True)
    (pending / "SKILL.md").write_bytes(b"\xff")
    _use_loader(monkeypatch, loader)

    with pytest.raises(SystemExit) as exc:
        cli_commands._skills(_args("show", slug="bad-encoding"))

    assert exc.value.code == 1
    assert capsys.readouterr().err.startswith("invalid_skill_encoding:")


def test_show_invalid_helper_encoding_exits_with_coded_reason(loader, monkeypatch, capsys):
    _stage(loader, "bad-helper-encoding")
    script = loader._pending_root() / "bad-helper-encoding" / "scripts" / "run.py"
    script.parent.mkdir()
    script.write_bytes(b"\xff")
    _use_loader(monkeypatch, loader)

    with pytest.raises(SystemExit) as exc:
        cli_commands._skills(_args("show", slug="bad-helper-encoding"))

    assert exc.value.code == 1
    assert capsys.readouterr().err.startswith("invalid_skill_encoding:")


def test_malformed_update_target_refuses_without_traceback_and_siblings_work(
    loader, monkeypatch, capsys
):
    _stage(loader, "bad-update-target")
    meta_path = loader._pending_root() / "bad-update-target" / ".meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.update({"kind": "update", "target": []})
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    _use_loader(monkeypatch, loader)

    cli_commands._skills(_args("show", slug="bad-update-target"))
    assert '"target": []' in capsys.readouterr().out

    with pytest.raises(SystemExit) as exc:
        cli_commands._skills(_args("approve", slug="bad-update-target"))

    err = capsys.readouterr().err
    assert exc.value.code == 1
    assert err.startswith("invalid_target:")
    assert "Traceback" not in err

    cli_commands._skills(_args("dismiss", slug="bad-update-target"))
    assert capsys.readouterr().out == "Dismissed: bad-update-target\n"


def test_approve_invalid_skill_encoding_exits_with_coded_reason(loader, monkeypatch, capsys):
    pending = loader._pending_root() / "bad-encoding"
    pending.mkdir(parents=True)
    (pending / "SKILL.md").write_bytes(b"\xff")
    _use_loader(monkeypatch, loader)

    with pytest.raises(SystemExit) as exc:
        cli_commands._skills(_args("approve", slug="bad-encoding"))

    assert exc.value.code == 1
    assert capsys.readouterr().err.startswith("invalid_skill_encoding:")
    assert pending.exists()


def test_approve_uses_loader_approval_helper(monkeypatch, capsys):
    calls: list[str] = []

    class _Loader:
        def approve_pending_candidate(self, slug):
            calls.append(slug)
            return "auto/live-one", ""

    _use_loader(monkeypatch, _Loader())

    cli_commands._skills(_args("approve", slug="candidate-update"))

    assert calls == ["candidate-update"]
    assert capsys.readouterr().out == "Approved: auto/live-one\n"


@pytest.mark.parametrize("action", ["approve", "dismiss"])
def test_mutations_refuse_agent_shell_with_audit(action, monkeypatch, capsys):
    events: list[dict] = []
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:test")
    monkeypatch.setattr(
        cli_commands,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
    )

    with pytest.raises(SystemExit) as exc:
        cli_commands._skills(_args(action, slug="candidate-one"))

    assert exc.value.code == 1
    assert capsys.readouterr().err.startswith("agent_shell_denied:")
    assert events[0]["outcome"] == "rejected"
    assert events[0]["metadata"]["reason"] == "agent_shell_denied"


def test_approve_unreadable_candidate_exits_with_coded_reason(loader, monkeypatch, capsys):
    _stage(loader, "candidate-one")
    pending = loader._pending_root() / "candidate-one"
    original_iterdir = Path.iterdir

    def _iterdir(path):
        if path == pending:
            raise PermissionError("not listable")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", _iterdir)
    _use_loader(monkeypatch, loader)

    with pytest.raises(SystemExit) as exc:
        cli_commands._skills(_args("approve", slug="candidate-one"))

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("candidate_unreadable:")
    assert "Traceback" not in err
    assert pending.exists()


def test_approve_invalid_script_exits_with_coded_reason(loader, monkeypatch, capsys):
    pending = loader._pending_root() / "unsafe-script"
    (pending / "scripts").mkdir(parents=True)
    (pending / "SKILL.md").write_text("# unsafe\n", encoding="utf-8")
    (pending / "scripts" / "run.py").write_text("def (: pass\n", encoding="utf-8")
    _use_loader(monkeypatch, loader)

    with pytest.raises(SystemExit) as exc:
        cli_commands._skills(_args("approve", slug="unsafe-script"))

    assert exc.value.code == 1
    assert capsys.readouterr().err.startswith("script_validation_failed:")
    assert not (loader._dir / "auto" / "unsafe-script").exists()


def test_approve_helper_uses_the_single_shared_form(loader):
    _stage(loader, "live-one")
    assert loader.approve_pending_skill("live-one") == "auto/live-one"
    loader.stage_skill_candidate(
        "live-one-update",
        description="updated candidate",
        triggers="live-one",
        procedure_md="## Steps\n\nRun the update.\n",
        provenance=AutoSkillProvenance(
            session_key="test",
            created_at=AutoSkillProvenance.now_iso(),
        ),
        kind="update",
        target="auto/live-one",
        base_version=1,
    )

    assert loader.approve_pending_candidate("live-one-update") == ("auto/live-one", "")


def test_dismiss_removes_candidate(loader, monkeypatch, capsys):
    _stage(loader, "candidate-one")
    _use_loader(monkeypatch, loader)
    events: list[dict] = []
    monkeypatch.setattr(
        cli_commands,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
    )

    cli_commands._skills(_args("dismiss", slug="candidate-one"))

    assert loader.list_pending_skills() == []
    assert events == [
        {
            "session_key": "skills",
            "agent": "cli",
            "source": "cli",
            "tool_name": "cli_skill_pending_dismiss",
            "tool_kind": "permission",
            "outcome": "invoked",
            "metadata": {"slug": "candidate-one"},
        }
    ]
    assert capsys.readouterr().out == "Dismissed: candidate-one\n"


def test_missing_candidate_exits_nonzero(loader, monkeypatch, capsys):
    _use_loader(monkeypatch, loader)

    with pytest.raises(SystemExit) as exc:
        cli_commands._skills(_args("show", slug="missing-one"))

    assert exc.value.code == 1
    assert capsys.readouterr().err.startswith("not_found:")
