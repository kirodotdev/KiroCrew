"""``kirocrew restore`` of a bundle from another install: crons paused and reset, skills kept out.

Every cron job of the bundle is restored PAUSED, message-only jobs too, with the record
fields that widen what the job may do reset to this install's defaults, and the summary
names each job. ``--include-crons`` installs them as written. A Replace does not install
the bundle's ``skills/`` over a live tree unless ``--include-skills`` is given.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import snapshot_helpers
from snapshot_helpers import unpinnable_argv

from kiro_crew.snapshot import restore_main

_no_gateway = snapshot_helpers._no_gateway

RESET_FIELDS = (
    "approval_mode",
    "project_path",
    "env",
    "secret_env",
    "execution_context",
    "agent_sequence",
    "channel",
    "model",
)


def _risky_job(name: str = "nightly") -> dict:
    return {
        "id": f"id-{name}",
        "name": name,
        "message": "check the build",
        "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
        "enabled": True,
        "approval_mode": "auto",
        "project_path": "/srv/other-projects",
        "env": {"FLAG": "1"},
        "secret_env": {"TOKEN": "x"},
        "execution_context": "ctx",
        "agent_sequence": ["one", "two"],
        "channel": "C123",
        "model": "some-model",
        "agent_id": "ghost-agent",
    }


def _bundle(tmp_path: Path, files: dict[str, str]) -> Path:
    root = "kirocrew-snapshot-20260101T000000Z"
    manifest = {
        "version": 4,
        "purpose": "backup",
        "components": {"crons": "unresolved", "skills": "unresolved"},
    }
    members = {"MANIFEST.json": json.dumps(manifest), **files}
    path = tmp_path / "bundle.tar.gz"
    with tarfile.open(str(path), "w:gz") as tar:
        d = tarfile.TarInfo(root)
        d.type = tarfile.DIRTYPE
        d.mode = 0o700
        tar.addfile(d)
        for name, text in members.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(data)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
    return path


def _restore(bundle: Path, home: Path, monkeypatch, *extra: str) -> int:
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return restore_main([str(bundle), "--force", *extra] + unpinnable_argv())


def _jobs(home: Path) -> dict[str, dict]:
    return {j["name"]: j for j in json.loads((home / "crons.json").read_text())["jobs"]}


def _crons_bundle(tmp_path: Path) -> Path:
    return _bundle(tmp_path, {"crons.json": json.dumps({"jobs": [_risky_job()]})})


def _assert_paused_and_reset(home: Path) -> None:
    job = _jobs(home)["nightly"]
    assert job["user_paused"] is True and job["enabled"] is False
    for field in RESET_FIELDS:
        assert not job.get(field), field
    assert not job.get("agent_id")


def test_replace_restores_a_message_only_cron_paused_and_reset(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"

    assert _restore(_crons_bundle(tmp_path), home, monkeypatch, "--mode", "replace") == 0

    _assert_paused_and_reset(home)
    out = capsys.readouterr().out
    assert "Restored PAUSED" in out and "nightly" in out.split("Restored PAUSED", 1)[1]


def test_merge_restores_a_message_only_cron_paused_and_reset(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"

    assert _restore(_crons_bundle(tmp_path), home, monkeypatch, "--mode", "merge") == 0

    _assert_paused_and_reset(home)
    assert "nightly" in capsys.readouterr().out.split("Restored PAUSED", 1)[1]


def test_the_cron_pick_installs_the_job_as_written(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"

    rc = _restore(
        _crons_bundle(tmp_path), home, monkeypatch, "--mode", "replace", "--include-crons"
    )

    assert rc == 0
    job = _jobs(home)["nightly"]
    assert job["enabled"] is True and job["approval_mode"] == "auto"
    assert job["env"] == {"FLAG": "1"} and job["model"] == "some-model"
    assert "Restored PAUSED" not in capsys.readouterr().out


def _skills_bundle(tmp_path: Path) -> Path:
    return _bundle(tmp_path, {"skills/foreign/SKILL.md": "foreign skill"})


def _live_skill(home: Path) -> Path:
    mine = home / "skills" / "mine" / "SKILL.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("live skill", encoding="utf-8")
    return mine


def test_replace_keeps_a_live_skills_tree_unless_it_is_picked(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    mine = _live_skill(home)

    assert _restore(_skills_bundle(tmp_path), home, monkeypatch, "--mode", "replace") == 0

    assert mine.read_text(encoding="utf-8") == "live skill"
    assert not (home / "skills" / "foreign").exists()
    assert "Kept your live skills" in capsys.readouterr().out


def test_replace_installs_the_skills_tree_when_picked(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    mine = _live_skill(home)

    rc = _restore(
        _skills_bundle(tmp_path), home, monkeypatch, "--mode", "replace", "--include-skills"
    )

    assert rc == 0
    assert (home / "skills" / "foreign" / "SKILL.md").read_text(encoding="utf-8") == "foreign skill"
    assert not mine.exists()
    assert "Kept your live skills" not in capsys.readouterr().out


def test_replace_installs_the_skills_tree_on_a_machine_with_no_live_skills(
    tmp_path, monkeypatch, capsys
):
    home = tmp_path / "home"

    assert _restore(_skills_bundle(tmp_path), home, monkeypatch, "--mode", "replace") == 0

    assert (home / "skills" / "foreign" / "SKILL.md").is_file()
    assert "Kept your live skills" not in capsys.readouterr().out
