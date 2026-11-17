"""An archive from another install: its crons arrive paused and reset, its skills stay out.

Every cron job of an archive is restored PAUSED, message-only jobs too, with the record
fields that widen what the job may do reset to this install's defaults. A job the user
picks (``include_crons``) is installed as written. A Replace does not install the
archive's ``skills/`` tree over a live one unless it is picked (``include_skills``); a
machine with no live skills installs it as it always did.
"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from unittest.mock import patch

from kiro_crew import portability
from kiro_crew.portability import apply_import_zip
from kiro_crew.portability_vetting import _sanitize_imported_crons

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


def _risky_job(name: str = "nightly", **extra) -> dict:
    """A message-only job that asks for every widening field."""
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
        **extra,
    }


def _store(tmp_path: Path, jobs: list[dict]) -> Path:
    path = tmp_path / "crons.json"
    path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    return path


def _read(path: Path) -> dict[str, dict]:
    return {j["name"]: j for j in json.loads(path.read_text(encoding="utf-8"))["jobs"]}


def test_helper_pauses_a_message_only_job_and_resets_its_fields(tmp_path):
    path = _store(tmp_path, [_risky_job()])

    dropped, paused = _sanitize_imported_crons(path, hold_all=True, known_agents=frozenset())

    job = _read(path)["nightly"]
    assert dropped == [] and paused == ["nightly"]
    assert job["user_paused"] is True and job["enabled"] is False
    for field in RESET_FIELDS:
        assert not job.get(field), field
    assert not job.get("agent_id")
    assert job["message"] == "check the build"


def test_helper_keeps_an_agent_that_exists_here_and_drops_one_that_does_not(tmp_path):
    path = _store(
        tmp_path,
        [_risky_job("a", agent_id="known"), _risky_job("b", agent_id="ghost-agent")],
    )

    _sanitize_imported_crons(path, hold_all=True, known_agents=frozenset({"known"}))

    jobs = _read(path)
    assert jobs["a"]["agent_id"] == "known"
    assert not jobs["b"].get("agent_id")


def _zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(str(path), "w") as zf:
        zf.writestr("snap/MANIFEST.json", json.dumps({"version": 2}))
        for name, text in files.items():
            zf.writestr(f"snap/{name}", text)
    return path


def _import(zip_path: Path, home: Path, mode: str = "replace", **kwargs) -> dict:
    with patch.object(portability, "config_dir", return_value=home):
        with patch.dict(os.environ, {"KIROCREW_HOME": str(home)}):
            return apply_import_zip(zip_path, mode=mode, **kwargs)


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


def test_import_restores_a_message_only_cron_paused_with_its_fields_reset(tmp_path):
    home = _home(tmp_path)
    z = _zip(tmp_path / "a.zip", {"crons.json": json.dumps({"jobs": [_risky_job()]})})

    summary = _import(z, home)

    job = _read(home / "crons.json")["nightly"]
    assert job["user_paused"] is True and job["enabled"] is False
    for field in RESET_FIELDS:
        assert not job.get(field), field
    assert summary["paused_crons"] == ["nightly"]


def test_import_in_merge_mode_also_pauses_and_resets(tmp_path):
    home = _home(tmp_path)
    z = _zip(tmp_path / "a.zip", {"crons.json": json.dumps({"jobs": [_risky_job()]})})

    summary = _import(z, home, mode="merge")

    job = _read(home / "crons.json")["nightly"]
    assert job["user_paused"] is True and not job.get("approval_mode")
    assert summary["paused_crons"] == ["nightly"]


def test_import_with_the_cron_pick_installs_the_job_as_written(tmp_path):
    home = _home(tmp_path)
    z = _zip(tmp_path / "a.zip", {"crons.json": json.dumps({"jobs": [_risky_job()]})})

    summary = _import(z, home, include_crons=True)

    job = _read(home / "crons.json")["nightly"]
    assert job["enabled"] is True and job["approval_mode"] == "auto"
    assert job["env"] == {"FLAG": "1"}
    assert "paused_crons" not in summary


def _live_skill(home: Path) -> Path:
    mine = home / "skills" / "mine" / "SKILL.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("live skill", encoding="utf-8")
    return mine


def _skills_zip(tmp_path: Path) -> Path:
    return _zip(tmp_path / "s.zip", {"skills/foreign/SKILL.md": "foreign skill"})


def test_replace_keeps_a_live_skills_tree_unless_it_is_picked(tmp_path):
    home = _home(tmp_path)
    mine = _live_skill(home)

    summary = _import(_skills_zip(tmp_path), home)

    assert mine.read_text(encoding="utf-8") == "live skill"
    assert not (home / "skills" / "foreign").exists()
    assert summary["skills_kept"] is True


def test_replace_installs_the_skills_tree_when_picked(tmp_path):
    home = _home(tmp_path)
    mine = _live_skill(home)

    summary = _import(_skills_zip(tmp_path), home, include_skills=True)

    assert (home / "skills" / "foreign" / "SKILL.md").read_text(encoding="utf-8") == "foreign skill"
    assert not mine.exists()
    assert "skills_kept" not in summary
