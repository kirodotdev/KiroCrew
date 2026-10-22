"""Fixtures and helpers shared by the ``test_session_control_revive*.py`` modules.

The autouse fixtures turn session control on, reset the stop-retry and create
rate-limit windows, and wait for each metadata rewrite's renamed file to open
before the test reads the line back. ``_slot`` gets or creates a slot and
``_folder`` adds a folder to the state, ``_key`` names a slot's history key,
``_archive`` closes a peer through the real close verb and ``_revive`` runs
``revive_session``. ``test_session_control_revive.py`` and
``test_session_control_revive_containment_hook.py`` both import from it.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew import history_projection as _history_projection
from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import stop_retry
from kiro_crew.dashboard.chat_utils import slot_history_key


@pytest.fixture(autouse=True)
# flake-ok: pre-existing autouse fixture moved verbatim; no importing module calls monkeypatch.undo()
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no importing module calls monkeypatch.undo()
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _fresh_windows():
    stop_retry.reset_for_tests()
    create_rate_limit.reset_for_tests()
    yield
    stop_retry.reset_for_tests()
    create_rate_limit.reset_for_tests()


@pytest.fixture(autouse=True)
# flake-ok: pre-existing autouse fixture moved verbatim; no importing module calls monkeypatch.undo()
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no importing module calls monkeypatch.undo()
def _settle_metadata_rewrite_before_read(monkeypatch):
    """Await each metadata rewrite's file transition before the test reads it.

    These tests drive a REAL on-disk ``ConversationLog`` through metadata
    rewrites (close, ``clear_closed``, ``update_metadata_if``) and then read the
    line straight back, all synchronously on the event loop via ``asyncio.run``.
    Every such rewrite lands atomically through ``replace_with_retry`` (a temp
    file renamed over the target). On Windows the freshly-renamed file is briefly
    unopenable while an indexer or AV scanner holds it (ERROR_SHARING_VIOLATION →
    PermissionError). Production tolerates this off the loop, where the metadata
    read retry sleeps; on the loop that pause is a deliberate no-op (a live server
    must not block the loop inside a read), so a synchronous on-loop test read
    races the hold and reports it as ``resume_conflict`` — a failure decided by
    OS file-sharing timing, not by what the test set up.

    The production READ path is left EXACTLY as it is — no attempt count,
    timeout, or poll budget is raised and the retry pause is not patched, because
    doing so would hide the race rather than resolve it. Instead the filesystem
    transition is synchronized on the WRITE side: after each metadata-line rename
    lands, the test confirms the destination is openable again before the writer
    returns. Because every metadata rewrite funnels through ``replace_with_retry``
    and every real caller reads only after its write returns, the file the
    subsequent read opens is always settled — the test awaits the rewrite before
    reading, deterministically. It reads the TRUE current line every time, so a
    line another writer changes concurrently (a link written during the read) is
    still seen; nothing is served from a stale snapshot.

    The wait is bounded and, on POSIX where no hold occurs, the first probe
    succeeds (a no-op), so it changes nothing on the platform these tests already
    pass on. It wraps only the shared rename primitive, so every injected
    condition the tests rely on is preserved: facade-level stubs replace whole
    methods above it, and a write-path ``OSError`` injection raises before the
    rename is reached.
    """
    import time

    from kiro_crew import atomic_write as _aw

    real_replace = _aw.replace_with_retry

    def replace_with_retry(src, dst):
        real_replace(src, dst)
        dst_path = str(dst)
        if not dst_path.endswith(".jsonl"):
            return
        # flake-ok: pre-existing 2 s bound on the wait for the renamed file to open; moved verbatim
        deadline = time.monotonic() + 2.0
        while True:
            try:
                with open(dst_path, encoding="utf-8"):
                    return
            except FileNotFoundError:
                return
            except OSError:
                if time.monotonic() >= deadline:
                    return
                # The rename primitive runs wherever its caller does, loop thread
                # included, where wait_until() refuses to block; on POSIX the first
                # probe succeeds and this pacing is never reached.
                time.sleep(0.005)  # flake-ok: paces a probe that may run on the loop thread

    # Both metadata writers reach the rename through this module-level name:
    # ``clear_closed``/close via ``atomic_write`` (which calls it) and
    # ``_update_metadata_locked`` directly. Patch it where each looks it up.
    monkeypatch.setattr(_aw, "replace_with_retry", replace_with_retry)
    monkeypatch.setattr(
        _history_projection, "replace_with_retry", replace_with_retry, raising=False
    )


def _slot(state, name: str, **kwargs):
    return state.get_or_create_slot(name, **kwargs)


def _key(slot) -> str:
    return slot_history_key(slot)


def _archive(state, caller, peer, *, messages=2, title="") -> str:
    """Close *peer* through the real close verb and return its slot key.

    The transcript is written first so the revive has something to bring back;
    the close is the production one, so the metadata line carries exactly what a
    ✕ leaves behind (``closed``, ``closed_at``, title, workspace, creator).
    """
    for i in range(messages):
        peer.messages.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"})
    if title:
        peer.title = title
        peer._titled = True
    peer._dirty = True
    key = peer.key
    asyncio.run(sc.close_target(state, caller_session_key=_key(caller), target=key))
    assert key not in state._slots
    meta = state.conversation_log.get_metadata(f"dashboard:{key}")
    assert meta.get("closed")
    # Backdate the close so it is strictly BEFORE any resume the caller then runs.
    #
    # `clear_closed`'s compare-and-clear refuses when `closed_at >= resume_started_at`,
    # conservative on purpose so a close landing at the resume's own boundary keeps its
    # marker. Both values come from `time.time()`, whose resolution is the platform's:
    # on Windows that is the ~15.6 ms interrupt clock, so a close and a resume a few
    # microseconds apart return the IDENTICAL float, the guard reads equal, and the
    # marker stays. Linux returns a finer value per call, so the same pair compares
    # strictly ordered there and the platform decides whether these tests see a clear.
    # The precondition they mean is "this session is closed BEFORE the resume begins",
    # so state it here rather than leaving clock granularity to imply it.
    state.conversation_log.update_metadata(
        f"dashboard:{key}", {"closed_at": float(meta["closed_at"]) - 60.0}
    )
    return key


def _revive(state, caller, target: str, **kwargs):
    return asyncio.run(
        sc.revive_session(state, caller_session_key=_key(caller), target=target, **kwargs)
    )


def _folder(state, fid: str, name: str):
    state._folders.append({"id": fid, "name": name, "parent_id": None, "position": 0})
