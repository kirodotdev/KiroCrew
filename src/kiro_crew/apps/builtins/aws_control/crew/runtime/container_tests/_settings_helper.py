"""A single-arg ``Settings`` factory shared by the runtime tests.

This lived in ``test_backup_restore`` until the backup subsystem was extracted from the PR;
the tests that borrowed it (backend env, secret retry, transport failure, review findings)
are runtime tests, not backup ones, so the factory moved here rather than leaving with the
deleted module. It is a plain helper, not a test module, so pytest does not collect it.
"""

from __future__ import annotations

from pathlib import Path

from container.common import Settings


def make_settings(root: Path, *, bucket="bkt", crew="crew1", prefix="crews") -> Settings:
    data_home = root / "data"
    config_dir = data_home
    s = Settings(
        backend_port=8765,
        backend_run_dir=data_home / "run",
        front_port=8080,
        route_prefix="",
        control_secret=None,
        data_home=data_home,
        config_dir=config_dir,
        crew_name=crew,
        backup_bucket=bucket,
        backup_prefix=prefix,
    )
    s.sessions_dir.mkdir(parents=True, exist_ok=True)
    s.archive_dir.mkdir(parents=True, exist_ok=True)
    s.artifacts_dir.mkdir(parents=True, exist_ok=True)
    s.config_dir.mkdir(parents=True, exist_ok=True)
    return s
