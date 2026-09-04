"""Restore tests. Real temporary files, a fake in-memory S3, no AWS.

Restore puts the two authority files in place and NOTHING else. Two risks, so
two clusters of tests:

* A restore that quietly widens back to the whole prefix. That is the leak this
  change removes, and it would come back green under a test that only checks
  the authority files landed, so the tests assert on what did NOT land as well.
* A SILENTLY partial restore. A missing authority file is still a degraded boot
  and must still be reported, and "no transcripts" must never be mistaken for
  it.

The property, stated the way it has to be stated: a task only ever holds the
conversations it itself served, and loses them when it exits. Not "the task
holds nothing" -- a served conversation is on disk while it is served.
"""

from __future__ import annotations

import logging
from pathlib import Path

from container.backup import layout, run_backup_cycle, run_restore
from container.backup.restore import SUMMARY_TOKEN
from container.backup.state import BackupState
from container.backup.store import InMemoryObjectStore
from container.common import Settings


def make_settings(root: Path, *, bucket="bkt", crew="crew1", prefix="crews") -> Settings:
    data_home = root / "data"
    config_dir = data_home / "config"
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
        backup_interval_secs=0,
    )
    s.sessions_dir.mkdir(parents=True, exist_ok=True)
    s.archive_dir.mkdir(parents=True, exist_ok=True)
    s.artifacts_dir.mkdir(parents=True, exist_ok=True)
    s.config_dir.mkdir(parents=True, exist_ok=True)
    return s


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _populate(s: Settings, *, with_map=True, with_slots=True) -> None:
    """Write a representative unit: two customers' conversations plus config."""
    _write(s.sessions_dir / "dashboard_s1.jsonl", b'{"turn": 1}\n{"turn": 2}\n')
    _write(s.sessions_dir / "dashboard_s2.jsonl", b'{"turn": 1}\n')
    _write(s.archive_dir / "dashboard_s1--20260101.jsonl", b'{"archived": true}\n')
    _write(s.artifacts_dir / "img1.png", b"\x89PNG\r\nHELLO")
    if with_map:
        _write(s.session_map_path, b'{"key": "sid"}')
    if with_slots:
        _write(s.open_slots_path, b'{"keys": ["s1"]}')


def _summary_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(SUMMARY_TOKEN)]


# --- the property: no conversation this task did not serve ----------------


def test_no_transcript_is_restored(tmp_path):
    src = make_settings(tmp_path / "src")
    store = InMemoryObjectStore()
    _populate(src)
    run_backup_cycle(src, store, BackupState())

    dst = make_settings(tmp_path / "dst")
    res = run_restore(dst, store=store)

    # Counted, not assumed: this is the number the deploy gate reads.
    assert res.transcripts_restored == 0
    # And nothing on disk, which is what catches a bulk restore reintroduced
    # without the counter (the counter alone would stay 0 and lie).
    assert list(dst.data_home.rglob("*.jsonl")) == []
    # The zero is only meaningful because the bucket HAD transcripts to leave:
    # two live conversations plus one archive segment.
    assert res.transcripts_available == 3
    # They are still in the bucket. Not restoring is not deleting.
    assert layout.full_key(dst, "data/sessions/dashboard_s1.jsonl") in store.list("")


def test_only_the_config_namespace_is_written(tmp_path):
    src = make_settings(tmp_path / "src")
    store = InMemoryObjectStore()
    _populate(src)
    run_backup_cycle(src, store, BackupState())

    dst = make_settings(tmp_path / "dst")
    res = run_restore(dst, store=store)

    assert res.ok
    assert res.restored == 2
    on_disk = sorted(
        p.relative_to(dst.data_home).as_posix() for p in dst.data_home.rglob("*") if p.is_file()
    )
    assert on_disk == ["config/open_slots.json", "config/session_map.json"]
    assert dst.session_map_path.read_bytes() == b'{"key": "sid"}'
    assert dst.open_slots_path.read_bytes() == b'{"keys": ["s1"]}'


def test_artifacts_are_not_restored_and_stay_in_the_bucket(tmp_path):
    # Decision, pinned: artifacts are one customer's content too, and the
    # backend's artifact store enumerates its whole directory with no
    # per-customer scope, so restoring the crew's artifacts would leave a
    # cross-customer read reachable by a tool call. They stay in S3.
    src = make_settings(tmp_path / "src")
    store = InMemoryObjectStore()
    _populate(src)
    run_backup_cycle(src, store, BackupState())

    dst = make_settings(tmp_path / "dst")
    run_restore(dst, store=store)

    assert not (dst.artifacts_dir / "img1.png").exists()
    assert layout.full_key(dst, "data/artifacts/img1.png") in store.list("")


# --- completeness: a missing authority file is still degraded --------------


def test_partial_reported_when_open_slots_missing(tmp_path):
    src = make_settings(tmp_path / "src")
    store = InMemoryObjectStore()
    _populate(src, with_slots=False)
    run_backup_cycle(src, store, BackupState())

    dst = make_settings(tmp_path / "dst")
    res = run_restore(dst, store=store)

    assert res.partial is True
    assert "open_slots" in res.missing
    assert not res.ok
    # The conversation list is what was lost. The absent transcripts are not
    # part of that: they are absent by design.
    assert res.transcripts_restored == 0
    assert not (dst.sessions_dir / "dashboard_s1.jsonl").exists()


def test_partial_reported_when_session_map_missing(tmp_path):
    src = make_settings(tmp_path / "src")
    store = InMemoryObjectStore()
    _populate(src, with_map=False)
    run_backup_cycle(src, store, BackupState())

    dst = make_settings(tmp_path / "dst")
    res = run_restore(dst, store=store)

    assert res.partial is True
    assert "session_map" in res.missing
    assert not res.ok


def test_bucket_with_transcripts_but_no_config_is_partial_not_empty(tmp_path):
    # Why restore still LISTs the whole prefix instead of only config/: a
    # narrowed list would see nothing and call this a clean first boot, hiding
    # a boot with no resume and no conversation list.
    dst = make_settings(tmp_path / "dst")
    store = InMemoryObjectStore()
    store.put(layout.full_key(dst, "data/sessions/dashboard_s1.jsonl"), b'{"t": 1}\n')

    res = run_restore(dst, store=store)

    assert res.empty is False
    assert res.partial is True
    assert set(res.missing) == {"session_map", "open_slots"}
    assert res.transcripts_available == 1
    assert res.transcripts_restored == 0


def test_empty_bucket_is_clean_first_boot_not_partial(tmp_path):
    dst = make_settings(tmp_path / "dst")
    res = run_restore(dst, store=InMemoryObjectStore())
    assert res.empty is True
    assert res.partial is False
    assert res.restored == 0
    assert res.transcripts_restored == 0
    assert res.ok


def test_restore_disabled_without_bucket(tmp_path):
    dst = make_settings(tmp_path / "dst", bucket=None)
    res = run_restore(dst)
    assert res.disabled is True
    assert res.restored == 0
    assert res.transcripts_restored == 0


# --- untrusted keys in the namespace restore still writes -----------------


def test_config_traversal_key_is_dropped(tmp_path):
    # The traversal guard now matters on the config namespace, because that is
    # the only namespace restore writes.
    dst = make_settings(tmp_path / "dst")
    store = InMemoryObjectStore()
    store.put(layout.full_key(dst, "config/session_map.json"), b"{}")
    store.put(layout.full_key(dst, "config/open_slots.json"), b'{"keys": []}')
    store.put(layout.full_key(dst, "config/../../evil.txt"), b"pwned")

    res = run_restore(dst, store=store)

    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path / "dst" / "evil.txt").exists()
    assert not (dst.data_home / "evil.txt").exists()
    assert res.ok
    assert res.skipped >= 1


def test_excluded_config_objects_are_not_restored(tmp_path):
    dst = make_settings(tmp_path / "dst")
    store = InMemoryObjectStore()
    store.put(layout.full_key(dst, "config/session_map.json"), b"{}")
    store.put(layout.full_key(dst, "config/open_slots.json"), b'{"keys": []}')
    # Host-local junk an older or mis-behaving backup could have left in the
    # namespace restore writes.
    store.put(layout.full_key(dst, "config/gateway.pid"), b"111")
    store.put(layout.full_key(dst, "config/session.lock"), b"")

    res = run_restore(dst, store=store)

    assert res.ok
    assert not (dst.config_dir / "gateway.pid").exists()
    assert not (dst.config_dir / "session.lock").exists()
    assert res.skipped >= 2


def test_data_namespace_junk_is_skipped_not_written(tmp_path):
    dst = make_settings(tmp_path / "dst")
    store = InMemoryObjectStore()
    store.put(layout.full_key(dst, "config/session_map.json"), b"{}")
    store.put(layout.full_key(dst, "config/open_slots.json"), b'{"keys": []}')
    store.put(layout.full_key(dst, "data/sessions/gateway.pid"), b"111")
    store.put(layout.full_key(dst, "data/sessions/subagents/a1/state.json"), b'{"pid": 1}')
    store.put(layout.full_key(dst, "data/../../evil.txt"), b"pwned")

    res = run_restore(dst, store=store)

    assert res.ok
    assert not (dst.sessions_dir / "gateway.pid").exists()
    assert not (dst.sessions_dir / "subagents" / "a1" / "state.json").exists()
    assert not (tmp_path / "evil.txt").exists()
    assert res.skipped >= 3


# --- trap 1 on the restore path: a config file can get SHORTER ------------


def test_shorter_config_file_survives_roundtrip(tmp_path):
    # open_slots.json shrinks when a conversation closes. Whole-object restore,
    # so the shorter content wins; a splice or an offset write would leave the
    # tail of the longer version behind.
    src = make_settings(tmp_path / "src")
    store = InMemoryObjectStore()
    state = BackupState()
    _write(src.session_map_path, b"{}")
    _write(src.open_slots_path, b'{"keys": ["s1", "s2", "s3"]}')
    run_backup_cycle(src, store, state)
    _write(src.open_slots_path, b'{"keys": ["s3"]}')  # atomic replace, shorter
    run_backup_cycle(src, store, state)

    dst = make_settings(tmp_path / "dst")
    res = run_restore(dst, store=store)

    assert res.ok
    assert dst.open_slots_path.read_bytes() == b'{"keys": ["s3"]}'


# --- the summary line the deploy gate reads -------------------------------


def test_summary_line_is_emitted_once_and_reports_zero(tmp_path, caplog):
    src = make_settings(tmp_path / "src")
    store = InMemoryObjectStore()
    _populate(src)
    run_backup_cycle(src, store, BackupState())

    dst = make_settings(tmp_path / "dst")
    with caplog.at_level(logging.INFO, logger="smc.backup.restore"):
        run_restore(dst, store=store)

    lines = _summary_lines(caplog)
    assert len(lines) == 1
    assert lines[0] == (
        "restore: SUMMARY state=ok transcripts_restored=0 transcripts_available=3 "
        "config_restored=2 restored_bytes=30 skipped=4 missing=none"
    )


def test_summary_line_is_emitted_for_every_outcome(tmp_path, caplog):
    # The gate must be able to tell "restore reported zero" from "restore never
    # ran", so the line is unconditional. An absent line is a different failure.
    dst_empty = make_settings(tmp_path / "empty")
    dst_off = make_settings(tmp_path / "off", bucket=None)
    dst_partial = make_settings(tmp_path / "partial")
    store = InMemoryObjectStore()
    store.put(layout.full_key(dst_partial, "config/session_map.json"), b"{}")
    store.put(layout.full_key(dst_partial, "data/sessions/dashboard_s1.jsonl"), b'{"t": 1}\n')

    with caplog.at_level(logging.INFO, logger="smc.backup.restore"):
        run_restore(dst_empty, store=InMemoryObjectStore())
        run_restore(dst_off)
        run_restore(dst_partial, store=store)

    lines = _summary_lines(caplog)
    assert len(lines) == 3
    assert "state=empty transcripts_restored=0 transcripts_available=0" in lines[0]
    assert "state=disabled transcripts_restored=0" in lines[1]
    assert (
        "state=partial transcripts_restored=0 transcripts_available=1 " "config_restored=1"
    ) in lines[2]
    assert lines[2].endswith("missing=open_slots")
