"""Phase-0/1 test: cron.referenced_skill_names + lifecycle cron exemption."""

from __future__ import annotations

import json

from kiro_crew import cron as cron_mod


def test_referenced_skill_names_reads_dollar_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_mod, "config_dir", lambda: tmp_path)
    (tmp_path / "crons.json").write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {"id": "1", "name": "j1", "message": "run $deploy-helper now"},
                    {"id": "2", "name": "j2", "message": "no tokens here"},
                    {"id": "3", "name": "j3", "message": "use $auto/rotate-logs please"},
                ],
            }
        ),
        encoding="utf-8",
    )
    refs = cron_mod.referenced_skill_names()
    assert "deploy-helper" in refs
    assert "auto/rotate-logs" in refs and "rotate-logs" in refs


def test_referenced_skill_names_missing_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_mod, "config_dir", lambda: tmp_path)
    assert cron_mod.referenced_skill_names() == set()
