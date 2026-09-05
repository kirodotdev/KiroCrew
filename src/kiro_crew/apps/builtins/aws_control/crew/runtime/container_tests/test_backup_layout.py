"""Layout regression tests, grounded in a real Kiro Crew backend.

Verified on 2026-09-02 by booting a real `kirocrew gateway` in an isolated
KIROCREW_HOME and inspecting disk: `config_dir()` and `data_home()` resolve to
the SAME directory, so Kiro Crew writes `session_map.json` and `open_slots.json`
at the HOME ROOT (e.g. `<home>/open_slots.json`) — never under a `config/`
subdirectory. Transcripts are `<home>/sessions/<source>_<slot>.jsonl` (the slot
key is prefixed with its source, e.g. `dashboard_`).

Consequence: `Settings.backup_unit()` only covers the two authoritative files
when `config_dir == data_home`. With the current default (`config_dir =
data_home/config`) the sidecar would back up NEITHER, producing a restore with
no resume (`session_map.json`) and no conversation list (`open_slots.json`).
These tests pin that requirement so a regression in the deployment env or in the
`common` default is caught.
"""

from __future__ import annotations

from pathlib import Path

from container.backup import layout, run_backup_cycle, run_restore
from container.backup.state import BackupState
from container.backup.store import InMemoryObjectStore
from container.common import Settings


def _settings(home: Path, config_dir: Path) -> Settings:
    return Settings(
        backend_port=8765,
        backend_run_dir=home / "run",
        front_port=8080,
        route_prefix="",
        control_secret=None,
        data_home=home,
        config_dir=config_dir,
        crew_name="crewX",
        backup_bucket="fake",
        backup_prefix="backups",
        backup_interval_secs=0,
    )


def _real_home_layout(home: Path) -> None:
    """Reproduce the verified on-disk layout of a real Kiro Crew home."""
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    (home / "sessions" / "archive").mkdir(parents=True, exist_ok=True)
    (home / "artifacts").mkdir(parents=True, exist_ok=True)
    # Authoritative files at the HOME ROOT (not under config/).
    (home / "session_map.json").write_text('{"verify-slot-1": "kiro-sid-1"}')
    (home / "open_slots.json").write_text('{"keys": ["verify-slot-1"], "ts": 1.0}')
    # Transcript carries the source prefix the backend assigns.
    (home / "sessions" / "dashboard_verify-slot-1.jsonl").write_text(
        '{"_type": "metadata"}\n{"role": "user", "content": "hi"}\n'
    )


def test_authoritative_config_files_live_at_home_root(tmp_path):
    home = tmp_path / "home"
    _real_home_layout(home)

    # config_dir == data_home == home : the unit is COMPLETE.
    ok = {rel for _, rel, _r in layout.iter_backup_files(_settings(home, home))}
    assert "config/session_map.json" in ok
    assert "config/open_slots.json" in ok
    assert "data/sessions/dashboard_verify-slot-1.jsonl" in ok

    # Default-style config_dir == data_home/config : BOTH authoritative files
    # are missed, because real Kiro Crew never writes them there. This is the
    # exact silent degradation finding #1 warns about.
    bad = {rel for _, rel, _r in layout.iter_backup_files(_settings(home, home / "config"))}
    assert "config/session_map.json" not in bad
    assert "config/open_slots.json" not in bad


def test_full_unit_survives_backup_and_restore_with_home_root_layout(tmp_path):
    src = tmp_path / "home"
    _real_home_layout(src)
    store = InMemoryObjectStore()
    run_backup_cycle(_settings(src, src), store, BackupState())

    # Restore into a fresh empty home (config_dir == data_home, the correct
    # deployment setting).
    dst = tmp_path / "home2"
    (dst).mkdir()
    res = run_restore(_settings(dst, dst), store=store)

    assert res.ok
    assert not res.partial
    assert (dst / "session_map.json").read_text() == '{"verify-slot-1": "kiro-sid-1"}'
    assert (dst / "open_slots.json").read_text() == ('{"keys": ["verify-slot-1"], "ts": 1.0}')
    # The transcript is deliberately NOT restored: a task only ever holds the
    # conversations it itself served, and loses them when it exits.
    assert not (dst / "sessions" / "dashboard_verify-slot-1.jsonl").exists()
    assert res.transcripts_restored == 0
    assert res.transcripts_available == 1


def test_home_root_transcript_key_still_maps_back_to_its_local_path(tmp_path):
    """The mapping this file was written to pin, asserted where it now lives.

    Restore no longer writes transcripts, so the round-trip above can no longer
    carry the evidence that a source-prefixed transcript at the home root gets a
    correct key and a correct local path back. The mapping is still real and
    still load-bearing -- the on-demand fetch depends on it -- so it is asserted
    against layout directly rather than deleted with the restore assertion.
    """
    src = tmp_path / "home"
    _real_home_layout(src)
    s = _settings(src, src)

    keys = {rel: local for local, rel, _r in layout.iter_backup_files(s)}
    rel = "data/sessions/dashboard_verify-slot-1.jsonl"
    assert rel in keys
    assert keys[rel] == src / "sessions" / "dashboard_verify-slot-1.jsonl"

    # And back again, into a different home.
    dst = tmp_path / "home2"
    dst.mkdir()
    assert layout.local_path_for_key(_settings(dst, dst), rel) == (
        dst / "sessions" / "dashboard_verify-slot-1.jsonl"
    )


def test_restore_reports_partial_when_run_with_wrong_config_dir(tmp_path):
    # If the sidecar was deployed with the default config_dir, the backup never
    # captured the authoritative files, so a restore is partial and says so.
    src = tmp_path / "home"
    _real_home_layout(src)
    store = InMemoryObjectStore()
    run_backup_cycle(_settings(src, src / "config"), store, BackupState())

    dst = tmp_path / "home2"
    dst.mkdir()
    res = run_restore(_settings(dst, dst), store=store)
    assert res.partial is True
    assert set(res.missing) == {"session_map", "open_slots"}


# --- key classifiers restore depends on -----------------------------------
#
# Restore writes the config namespace only and reports how many transcripts it
# restored, so these two predicates decide both what lands on disk and what the
# deploy gate reads. Pinned here because a silent widening of either one would
# reintroduce the leak with every suite still green.


def test_is_config_key_only_matches_the_config_namespace(tmp_path):
    assert layout.is_config_key("config/session_map.json") is True
    assert layout.is_config_key("config/open_slots.json") is True
    assert layout.is_config_key("data/sessions/dashboard_s1.jsonl") is False
    assert layout.is_config_key("data/artifacts/img.png") is False
    # A key that merely mentions the word is not in the namespace.
    assert layout.is_config_key("data/sessions/config/x.json") is False


def test_is_transcript_matches_live_and_archived_conversations(tmp_path):
    home = tmp_path / "home"
    _real_home_layout(home)
    s = _settings(home, home)

    assert layout.is_transcript(s, "data/sessions/dashboard_verify-slot-1.jsonl")
    # An archive segment is the same conversation, only older.
    assert layout.is_transcript(s, "data/sessions/archive/dashboard_s1--20260101.jsonl")
    # Not conversations.
    assert not layout.is_transcript(s, "config/session_map.json")
    assert not layout.is_transcript(s, "data/artifacts/report.jsonl")
    assert not layout.is_transcript(s, "data/sessions/gateway.pid")


def test_sessions_prefix_tracks_the_home_root_layout(tmp_path):
    home = tmp_path / "home"
    _real_home_layout(home)
    assert layout.sessions_prefix(_settings(home, home)) == "data/sessions/"
