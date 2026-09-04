"""Sidecar tests. Real temporary files, a fake in-memory S3, no AWS.

Each of the three on-disk traps has a test that FAILS against the naive
implementation, not merely one that passes against ours. The naive behaviour is
spelled out inline so the contrast is visible.
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from pathlib import Path

from container.backup import layout, run_backup_cycle, run_sidecar
from container.backup.state import BackupState, backup_status, state_path
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


def _fk(settings: Settings, rel: str) -> str:
    return layout.full_key(settings, rel)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# --- Trap 1: transcript is not append-only (atomic replace, sometimes shorter) --


def test_trap1_shorter_replacement_is_whole_object_not_spliced(tmp_path):
    s = make_settings(tmp_path)
    store = InMemoryObjectStore()
    state = BackupState()

    live = s.sessions_dir / "s1.jsonl"
    long_content = b'{"turn": 1}\n{"turn": 2}\n{"turn": 3}\n'
    _write(live, long_content)
    run_backup_cycle(s, store, state)

    rel = "data/sessions/s1.jsonl"
    assert store.get(_fk(s, rel)) == long_content

    # Compaction atomically replaces the file with a SHORTER one.
    short_content = b'{"turn": 3}\n'
    _write(live, short_content)

    # The naive offset/append uploader can only ever grow an object; it has no
    # way to represent a file that got shorter. Show that its result diverges.
    naive_incremental = long_content + short_content  # append the "new" bytes
    assert naive_incremental != short_content

    run_backup_cycle(s, store, state)

    # Whole-object upload reflects the new shorter content exactly, no splice.
    assert store.get(_fk(s, rel)) == short_content


# --- Trap 2: mtime is restored after every rewrite ------------------------


def test_trap2_same_mtime_same_size_different_content_is_detected(tmp_path):
    s = make_settings(tmp_path)
    store = InMemoryObjectStore()
    state = BackupState()

    live = s.sessions_dir / "s1.jsonl"
    original = b"AAAAAAAAAAAAAAAA"  # 16 bytes
    _write(live, original)
    frozen_ns = 1_600_000_000 * 10**9
    os.utime(live, ns=(frozen_ns, frozen_ns))
    run_backup_cycle(s, store, state)

    rel = "data/sessions/s1.jsonl"
    assert store.get(_fk(s, rel)) == original

    # Rewrite with DIFFERENT bytes of the SAME length, then restore the mtime
    # exactly as Kiro Crew's _restore_mtime does.
    rewritten = b"BBBBBBBBBBBBBBBB"  # 16 bytes, different content
    _write(live, rewritten)
    os.utime(live, ns=(frozen_ns, frozen_ns))

    # A naive mtime poller sees nothing: mtime is identical.
    assert os.stat(live).st_mtime_ns == frozen_ns
    # And a size-only detector sees nothing either: same length.
    assert len(rewritten) == len(original)

    res = run_backup_cycle(s, store, state)

    # Only size+hash detection catches this. The sidecar re-uploaded.
    assert res.uploaded == 1
    assert store.get(_fk(s, rel)) == rewritten


# --- Trap 3: copying without the lock races a rewrite ---------------------


def test_trap3_defers_while_writer_holds_lock(tmp_path):
    s = make_settings(tmp_path)
    store = InMemoryObjectStore()
    state = BackupState()

    live = s.sessions_dir / "s1.jsonl"
    _write(live, b'{"turn": 1}\n')
    lock_path = s.sessions_dir / "s1.jsonl.lock"
    rel = "data/sessions/s1.jsonl"

    holder_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(holder_fd, fcntl.LOCK_EX)  # the writer holds the lock
    try:
        # A short budget: the sidecar must give up rather than read through.
        res = run_backup_cycle(s, store, state, lock_wait_secs=0.3)
        assert res.deferred_locked == 1
        assert _fk(s, rel) not in store._objects  # nothing torn was uploaded
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)


def test_trap3_waits_then_uploads_after_release(tmp_path):
    s = make_settings(tmp_path)
    store = InMemoryObjectStore()
    state = BackupState()

    live = s.sessions_dir / "s1.jsonl"
    content = b'{"turn": 1}\n{"turn": 2}\n'
    _write(live, content)
    lock_path = s.sessions_dir / "s1.jsonl.lock"
    rel = "data/sessions/s1.jsonl"

    holder_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(holder_fd, fcntl.LOCK_EX)

    box: list = []
    t = threading.Thread(
        target=lambda: box.append(run_backup_cycle(s, store, state, lock_wait_secs=5.0))
    )
    t.start()
    try:
        time.sleep(0.4)
        # Still blocked on the held lock: the transcript is NOT uploaded yet.
        assert _fk(s, rel) not in store._objects
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)

    t.join(timeout=5)
    assert not t.is_alive()
    # Once the writer released, the sidecar proceeded and captured the file.
    assert store.get(_fk(s, rel)) == content
    assert box[0].deferred_locked == 0


# --- Write-once artifacts: uploaded once, not re-hashed every cycle -------


def test_artifacts_uploaded_once(tmp_path):
    s = make_settings(tmp_path)
    store = InMemoryObjectStore()
    state = BackupState()

    art = s.artifacts_dir / "img1.png"
    _write(art, b"\x89PNG\r\n" + b"x" * 100)
    rel = "data/artifacts/img1.png"

    r1 = run_backup_cycle(s, store, state)
    assert r1.uploaded >= 1
    assert store.put_count[_fk(s, rel)] == 1

    # Nothing changed: the artifact is skipped without a second upload.
    r2 = run_backup_cycle(s, store, state)
    assert r2.skipped_artifact >= 1
    assert store.put_count[_fk(s, rel)] == 1  # still one PUT total


def test_unchanged_transcript_not_reuploaded(tmp_path):
    s = make_settings(tmp_path)
    store = InMemoryObjectStore()
    state = BackupState()

    _write(s.sessions_dir / "s1.jsonl", b'{"turn": 1}\n')
    run_backup_cycle(s, store, state)
    r2 = run_backup_cycle(s, store, state)
    assert r2.uploaded == 0
    assert r2.skipped_unchanged >= 1


# --- Exclusions: PID files, subagent state, lock sidecars -----------------


def test_excluded_paths_are_not_uploaded(tmp_path):
    s = make_settings(tmp_path)
    store = InMemoryObjectStore()
    state = BackupState()

    _write(s.sessions_dir / "s1.jsonl", b'{"turn": 1}\n')
    _write(s.sessions_dir / "gateway.pid", b"12345")
    _write(s.sessions_dir / "s1.jsonl.lock", b"")
    _write(s.sessions_dir / "subagents" / "a1" / "state.json", b'{"pid": 999}')

    run_backup_cycle(s, store, state)

    keys = set(store._objects)
    assert _fk(s, "data/sessions/s1.jsonl") in keys
    assert _fk(s, "data/sessions/gateway.pid") not in keys
    assert _fk(s, "data/sessions/s1.jsonl.lock") not in keys
    assert _fk(s, "data/sessions/subagents/a1/state.json") not in keys


# --- Backup lag is a metric the owner can read ----------------------------


def test_lag_metric_is_visible(tmp_path):
    s = make_settings(tmp_path)
    store = InMemoryObjectStore()

    # Before any cycle, the exposure is the whole conversation, reported as
    # None rather than a misleading zero.
    assert backup_status(s)["lag_secs"] is None

    _write(s.sessions_dir / "s1.jsonl", b'{"turn": 1}\n')
    run_sidecar(s, store=store, max_cycles=2)

    status = backup_status(s)
    assert status["cycles"] == 2
    assert status["objects"] >= 1
    assert status["lag_secs"] is not None
    assert status["lag_secs"] >= 0.0
    assert state_path(s).exists()


def test_sidecar_disabled_without_bucket(tmp_path):
    s = make_settings(tmp_path, bucket=None)
    # No bucket: the sidecar logs and returns instead of crashing the task.
    run_sidecar(s, max_cycles=1)
    assert not state_path(s).exists()


def test_sidecar_seeds_from_bucket_to_avoid_reupload(tmp_path):
    # A restarted task should not re-upload write-once artifacts already in S3.
    s = make_settings(tmp_path)
    store = InMemoryObjectStore()
    art = s.artifacts_dir / "img1.png"
    _write(art, b"payload")
    run_sidecar(s, store=store, max_cycles=1)
    rel = "data/artifacts/img1.png"
    assert store.put_count[_fk(s, rel)] == 1

    # Fresh state (as after a restart) but same bucket: seeding must recognise
    # the artifact and skip the re-upload.
    os.unlink(state_path(s))
    run_sidecar(s, store=store, max_cycles=1)
    assert store.put_count[_fk(s, rel)] == 1
