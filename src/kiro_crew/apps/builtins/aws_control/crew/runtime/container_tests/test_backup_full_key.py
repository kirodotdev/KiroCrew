"""The full S3 key a live crew actually writes.

Every other backup test asserts *rel* keys (``data/...``, ``config/...``) or
that backup round-trips through the same ``object_prefix()`` on both sides. None
of them asserts the FULL key, so nothing caught the deployment writing one level
too deep: ``deploy/templates/crew.yaml`` set ``SMC_BACKUP_PREFIX`` to
``crews/<crew>/`` while ``object_prefix()`` (container/backup/layout.py) already
appends ``SMC_CREW_NAME``, doubling the crew segment to
``crews/<crew>/<crew>/data/...``. IAM on ``crews/<crew>/*`` still covered the
deeper key and backup/restore both went through the same function, so it
round-tripped and every gate stayed green while the fetcher's intended layout
was wrong.

This test pins the exact keys the fetcher reads, using the real deployed
values: ``backup_prefix="crews/"`` (crew.yaml after the fix) plus
``crew_name="baymax"`` must yield ``crews/baymax/data/...`` and
``crews/baymax/config/...`` -- never a nested ``crews/baymax/baymax/...``.
"""

from __future__ import annotations

from pathlib import Path

from container.backup import run_backup_cycle
from container.backup.state import BackupState
from container.backup.store import InMemoryObjectStore
from container.common import Settings

# The deployed values: crew.yaml sets SMC_BACKUP_PREFIX='crews/' and
# SMC_CREW_NAME=<crew>; the container reads them into these two fields.
_DEPLOYED_BACKUP_PREFIX = "crews/"
_CREW = "baymax"


def _deployed_settings(home: Path) -> Settings:
    """Settings as a real Fargate crew is constructed (config_dir == data_home)."""
    return Settings(
        backend_port=8765,
        backend_run_dir=home / "run",
        front_port=8080,
        route_prefix="/c/baymax",
        control_secret=None,
        data_home=home,
        config_dir=home,
        crew_name=_CREW,
        backup_bucket="smc-111122223333-us-west-2",
        backup_prefix=_DEPLOYED_BACKUP_PREFIX,
        backup_interval_secs=0,
    )


def _seed_home(home: Path) -> str:
    """Write the verified real Kiro Crew on-disk layout. Returns the sid."""
    sid = "dashboard_baymax-main"
    (home / "sessions" / "archive").mkdir(parents=True, exist_ok=True)
    (home / "artifacts").mkdir(parents=True, exist_ok=True)
    (home / "session_map.json").write_text('{"baymax-main": "kiro-sid-1"}')
    (home / "open_slots.json").write_text('{"keys": ["baymax-main"], "ts": 1.0}')
    (home / "sessions" / f"{sid}.jsonl").write_text(
        '{"_type": "metadata"}\n{"role": "user", "content": "hi"}\n'
    )
    return sid


def test_live_crew_writes_intended_full_keys(tmp_path):
    home = tmp_path / "home"
    sid = _seed_home(home)
    store = InMemoryObjectStore()

    run_backup_cycle(_deployed_settings(home), store, BackupState())

    keys = set(store.list(""))

    transcript_key = f"crews/{_CREW}/data/sessions/{sid}.jsonl"
    open_slots_key = f"crews/{_CREW}/config/open_slots.json"
    session_map_key = f"crews/{_CREW}/config/session_map.json"

    # The exact keys the fetcher reads, end to end.
    assert transcript_key in keys, sorted(keys)
    assert open_slots_key in keys, sorted(keys)
    assert session_map_key in keys, sorted(keys)

    # And nothing under a DOUBLED crew segment -- the shipped defect.
    doubled = f"crews/{_CREW}/{_CREW}/"
    assert not any(k.startswith(doubled) for k in keys), sorted(keys)


def test_no_key_repeats_the_crew_name_segment(tmp_path):
    """Guards the doubling directly: the crew name appears exactly once."""
    home = tmp_path / "home"
    _seed_home(home)
    store = InMemoryObjectStore()

    run_backup_cycle(_deployed_settings(home), store, BackupState())

    for key in store.list(""):
        segments = key.split("/")
        assert segments[:2] == ["crews", _CREW], key
        # The crew name must not appear a second time as its own path segment.
        assert segments.count(_CREW) == 1, key
