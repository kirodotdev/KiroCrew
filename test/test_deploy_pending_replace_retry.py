"""The pending-deploy store's atomic replace must survive Windows contention.

``deploy/pending.py::_save_raw`` does the whole durable-write dance —
``NamedTemporaryFile`` in the destination's directory, ``flush``, ``fsync``,
close, then an atomic rename — and finishes on a bare ``os.replace``. On Windows
that rename raises ``PermissionError`` while any other handle is open on either
path (an indexer, an AV scanner, a concurrent reader), which is the transient
window ``atomic_write.replace_with_retry`` exists to absorb.

All three public writers funnel through ``_save_raw`` under the same
cross-process ``file_lock``: ``add_pending`` (a preview asking for
confirmation), ``claim_pending`` (the human confirm) and ``remove_pending``
(dismiss). One faulted rename aborts whichever of those was in flight.

The retry is only useful where it is allowed to sleep, and it is: every
dashboard caller runs the store through ``asyncio.to_thread`` — six
``add_pending``, one ``claim_pending``, one ``remove_pending`` in
``deploy/handlers.py`` — so ``replace_with_retry``'s off-the-event-loop gate
leaves the retry enabled on the path that matters.

These drive ``add_pending`` because the three writers share the one rename;
duplicating the same scenario per public function would pin nothing extra.
"""

from __future__ import annotations

import json

import pytest
from windows_sim import replace_sharing_violation

from kiro_crew import atomic_write as aw
from kiro_crew import platform_compat
from kiro_crew.deploy import pending


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """Keep the bounded retry loop instant; attempt COUNT is what these pin."""
    monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "deploy" / "pending-deploys.json"
    monkeypatch.setattr(pending, "_store_path", lambda: path)
    return path


@pytest.fixture
def _windows(monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)


def _params(site: str = "site-1"):
    return {
        "site_id": site,
        "artifact_slug": "deck",
        "local_dir": "/tmp/x",
        "profile": "default",
        "region": "us-east-1",
    }


def test_a_contended_rename_retries_and_the_entry_is_persisted(store, _windows):
    """One faulted rename, then one that succeeds: the confirmation survives."""
    with replace_sharing_violation(match="pending-deploys.json", times=1) as state:
        entry = pending.add_pending(_params())

    assert entry["id"]
    stored = json.loads(store.read_text(encoding="utf-8"))
    assert [e["id"] for e in stored] == [entry["id"]]
    assert state["n"] == 2, (
        "the simulator must have FAULTED the store's own rename -- n < 2 means "
        "the retry was never on the production path"
    )


def test_a_permanently_contended_rename_still_fails_and_leaves_the_store_intact(store, _windows):
    """Bounded, not infinite — and a failed write must not damage what is there."""
    pending.add_pending(_params("site-first"))
    before = store.read_text(encoding="utf-8")

    with replace_sharing_violation(match="pending-deploys.json", times=10_000) as state:
        with pytest.raises(PermissionError):
            pending.add_pending(_params("site-second"))

    assert state["n"] == aw._REPLACE_MAX_ATTEMPTS, "the retry must be bounded"
    assert store.read_text(encoding="utf-8") == before, "the previous store was damaged"
    assert sorted(p.name for p in store.parent.glob("*.tmp")) == [], (
        "the temp file was left behind; _save_raw's BaseException cleanup must "
        "still run when the retry gives up"
    )


def test_posix_permission_errors_are_not_slept_over(store, monkeypatch):
    """On POSIX the OS permits replacing an open file, so a PermissionError is a
    REAL access fault. This is the non-vacuity proof for the platform gate: the
    same simulator settings recover in the first test and must not here."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)

    with replace_sharing_violation(match="pending-deploys.json", times=1) as state:
        with pytest.raises(PermissionError):
            pending.add_pending(_params())

    assert state["n"] == 1, "it must not have tried a second time"


@pytest.mark.skipif(
    not platform_compat.IS_WINDOWS,
    reason="POSIX permits replacing a file that another handle holds open",
)
def test_a_real_windows_reader_no_longer_defeats_the_pending_write(store, monkeypatch):
    """No simulator: a real open handle on the destination, on a real Windows
    host. The only test here that evidences the OS behaviour itself.

    Deterministic without wall-clock: the handle is released from inside the
    retry's own backoff, so attempt 1 fails against a genuinely locked file and
    attempt 2 succeeds against a genuinely free one.
    """
    first = pending.add_pending(_params("site-first"))

    handle = open(store, "rb")  # e.g. an indexer or a concurrent reader
    releases = {"n": 0}

    class _ReleaseOnBackoff:
        """Stands in for the ``time`` module inside ``atomic_write`` only."""

        @staticmethod
        def sleep(_seconds):
            releases["n"] += 1
            if not handle.closed:
                handle.close()

    monkeypatch.setattr(aw, "time", _ReleaseOnBackoff)
    try:
        second = pending.add_pending(_params("site-second"))
    finally:
        if not handle.closed:
            handle.close()

    stored = json.loads(store.read_text(encoding="utf-8"))
    assert [e["id"] for e in stored] == [first["id"], second["id"]]
    assert releases["n"] == 1, "exactly one backoff, i.e. the first rename really failed"
