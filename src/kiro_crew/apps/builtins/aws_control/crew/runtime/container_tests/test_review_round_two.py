"""Two fixes from a review round, each pinned so reverting it reddens.

Both were found by a review bot on code that already had a guard nearby, which
is why they are worth naming precisely rather than filing as "hardening".

* Restore chose which objects to write with a NAMESPACE predicate. The task role
  can write to this bucket in persistent mode, so any object it uploads under
  ``config/`` was restored on the next boot -- including one that governs the
  agent itself. The sibling test ``test_only_the_config_namespace_is_written``
  says "namespace" in its own name and passed the whole time, because a
  namespace was exactly what the code enforced.

* The transcript write ran on the event loop, one line after a comment
  explaining why the FETCH must not.
"""

from __future__ import annotations

import asyncio
import inspect

from container.backup import layout, run_restore
from container.backup.store import InMemoryObjectStore
from container.front import transcript as tr

from .test_backup_restore import _populate, make_settings


def _seed(src, dst, extra: dict[str, bytes]) -> InMemoryObjectStore:
    """Upload the real authority files plus whatever else a caller wants there."""
    _populate(src)
    store = InMemoryObjectStore()
    run_restore  # imported for symmetry with the sibling module's usage
    from container.backup import run_backup_cycle
    from container.backup.state import BackupState

    run_backup_cycle(src, store, BackupState())
    for rel, data in extra.items():
        store.put(f"{layout.object_prefix(dst)}{rel}", data)
    return store


def test_a_config_object_the_task_uploaded_is_not_restored(tmp_path):
    """The governance ceiling is not something the bucket gets to set.

    A prompt injection that talks the agent into uploading a policy file must not
    have that file installed for it on the next boot, and the backend reads this
    directory before it loads.
    """
    src = make_settings(tmp_path / "src")
    dst = make_settings(tmp_path / "dst")
    hostile = b'{"allow_everything": true}'
    store = _seed(src, dst, {"config/security_policy.json": hostile})

    run_restore(dst, store=store)

    planted = dst.config_dir / "security_policy.json"
    assert not planted.exists(), (
        "an object the task itself uploaded was restored into the config "
        "directory the backend reads at boot"
    )


def test_the_two_authority_files_still_land(tmp_path):
    """The fix must not be a restore that writes nothing at all."""
    src = make_settings(tmp_path / "src")
    dst = make_settings(tmp_path / "dst")
    store = _seed(src, dst, {})

    result = run_restore(dst, store=store)

    assert result.restored == 2, f"expected both authority files, got {result.restored}"
    for rel in layout.config_keys(dst).values():
        local = layout.local_path_for_key(dst, rel)
        assert local is not None and local.exists(), f"{rel} was not restored"


def test_a_plausible_looking_config_name_is_still_refused(tmp_path):
    """Not a denylist of known-bad names: anything unenumerated is refused.

    A name close to a real one is the interesting case, because a denylist would
    wave it through and this must not.
    """
    src = make_settings(tmp_path / "src")
    dst = make_settings(tmp_path / "dst")
    real = sorted(layout.config_keys(dst).values())[0]
    lookalike = real.replace(".json", ".backup.json")
    assert lookalike != real
    store = _seed(src, dst, {lookalike: b"{}"})

    run_restore(dst, store=store)

    planted = layout.local_path_for_key(dst, lookalike)
    assert (
        planted is None or not planted.exists()
    ), f"{lookalike} was restored because its name resembles an authority file"


def test_the_transcript_write_is_not_on_the_event_loop():
    """The write is offloaded, for the same reason the fetch above it is.

    Asserted on the source rather than by timing: a timing test on a fast tmpfs
    would pass with the blocking call in place, which is the failure mode that
    let this survive.
    """
    src = inspect.getsource(tr.ensure_local_transcript)
    assert "_write_without_clobbering" in src, "test is anchored to a call that moved"
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "_write_without_clobbering" in line and "def " not in line:
            assert "to_thread" in line, (
                "the transcript write runs inline on the event loop: "
                f"{line.strip()!r}. It fsyncs a multi-megabyte payload, which "
                "stalls every other conversation's turn -- the exact reason the "
                "fetch on the preceding line is already offloaded."
            )
            break
    else:
        raise AssertionError("no call site found to check")


def test_the_offloaded_write_still_writes(tmp_path):
    """Offloading must not have turned the write into a no-op."""
    path = tmp_path / "sess" / "c.jsonl"
    payload = b'{"role":"user"}\n'

    asyncio.run(asyncio.to_thread(tr._write_without_clobbering, path, payload))

    assert path.read_bytes() == payload
