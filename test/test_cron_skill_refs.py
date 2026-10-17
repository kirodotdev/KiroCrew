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


def test_referenced_skill_names_survives_every_corruption_class(tmp_path, monkeypatch):
    """Corrupt stores must not raise into the skill lifecycle.

    This reader shares `_read_job_records` with the doctor scan and the
    off-thread enabled count, so the corruption classes it survives are
    asserted here too -- a shared guard is only worth what each call site
    actually demonstrates. Opens with a positive control so a later empty set
    is the guard, not a token-matching regression.
    """
    monkeypatch.setattr(cron_mod, "config_dir", lambda: tmp_path)
    store = tmp_path / "crons.json"

    store.write_text(
        json.dumps({"jobs": [{"id": "1", "name": "j", "message": "run $deploy-helper"}]}),
        encoding="utf-8",
    )
    assert "deploy-helper" in cron_mod.referenced_skill_names(), "positive control"

    for payload in (
        b"not json",
        b"[]",
        b'{"jobs": null}',
        b'{"jobs": ["junk", 3]}',
        b'{"jobs": [{"id": "a", "message": "\xff\xfe"}]}',
        ("[" * 100_000 + "]" * 100_000).encode(),
    ):
        store.write_bytes(payload)
        assert cron_mod.referenced_skill_names() == set(), f"raised/leaked on {payload[:24]!r}"
