"""Tests for ``atomic_write``'s bytes and owner-only capabilities (issue #1105).

These two gaps are why a set of hand-rolled temp-write-and-rename sites could
not adopt the shared helper, and so silently missed the Windows rename retry:

* binary payloads (a compiled helper binary, an archive) could not pass through
  a ``content: str`` signature at all;
* secret-bearing payloads need an owner-only DACL applied BEFORE the content is
  written, and ``mode=`` cannot deliver that because ``fchmod_safe`` is a
  documented no-op on Windows.

The ordering assertions are the load-bearing ones. A lockdown applied after the
content is written, or a ``fchmod`` that widens the file back to the umask
default afterwards, would both still pass a naive "final mode is 0600" check
while leaving the secret exposed for the duration of the write.
"""

from __future__ import annotations

import logging
import os
import stat

import pytest

from kiro_crew import atomic_write as aw
from kiro_crew import platform_compat


def test_bytes_content_lands_verbatim(tmp_path):
    """Binary payloads must survive byte-for-byte, with no encoding applied."""
    target = tmp_path / "helper.bin"
    payload = bytes(range(256))

    aw.atomic_write(target, payload)

    assert target.read_bytes() == payload


def test_bytes_content_is_not_newline_translated(tmp_path):
    """The bug that text mode would introduce: CRLF rewriting inside a binary."""
    target = tmp_path / "payload.bin"

    aw.atomic_write(target, b"\r\n\n\r")

    assert target.read_bytes() == b"\r\n\n\r"


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="descriptor-relative writes are POSIX")
def test_atomic_write_at_replaces_inside_pinned_directory(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        aw.atomic_write_at(dir_fd, "value.txt", "one", mode=0o600)
        aw.atomic_write_at(dir_fd, "value.txt", "two", fsync=True, mode=0o600)
    finally:
        os.close(dir_fd)
    assert (root / "value.txt").read_text() == "two"


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="descriptor-relative writes are POSIX")
def test_atomic_write_at_replaces_symlink_without_touching_target(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    (root / "value.txt").symlink_to(victim)
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        aw.atomic_write_at(dir_fd, "value.txt", "managed", mode=0o600)
    finally:
        os.close(dir_fd)
    assert victim.read_text() == "keep"
    assert not (root / "value.txt").is_symlink()
    assert (root / "value.txt").read_text() == "managed"


def test_atomic_write_at_rejects_non_leaf_name(tmp_path):
    if not platform_compat.IS_POSIX:
        pytest.skip("descriptor-relative writes are POSIX")
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError, match="one leaf name"):
            aw.atomic_write_at(dir_fd, "nested/value.txt", "nope")
    finally:
        os.close(dir_fd)


def test_str_content_still_writes_utf8(tmp_path):
    """The pre-existing text path must be untouched by the bytes addition."""
    target = tmp_path / "notes.txt"

    aw.atomic_write(target, "héllo")

    assert target.read_text(encoding="utf-8") == "héllo"


def test_newline_with_bytes_is_rejected(tmp_path):
    """Silently ignoring a meaningless argument would hide a caller bug."""
    with pytest.raises(TypeError, match="text-mode concept"):
        aw.atomic_write(tmp_path / "x.bin", b"data", newline="")


def test_restrict_to_owner_runs_before_any_content_is_written(tmp_path, monkeypatch):
    """The ordering IS the security property, so assert the sequence itself.

    A lockdown applied after the write leaves the secret readable for the whole
    write on Windows, where fchmod_safe does nothing.

    The seam is ``os.write``: the helper writes through an explicit raw loop
    rather than a buffered file object, because a buffered writer retries a raw
    write reporting 0 bytes forever instead of failing.
    """
    events: list[str] = []
    real_restrict = platform_compat.restrict_to_owner

    def _spy(path, **_kw):
        events.append("restrict")
        return real_restrict(path)

    monkeypatch.setattr(platform_compat, "restrict_to_owner", _spy)

    target = tmp_path / "secret.key"
    real_os_write = os.write

    def _tracking_write(fd, data):
        events.append("write")
        return real_os_write(fd, data)

    monkeypatch.setattr(os, "write", _tracking_write)

    aw.atomic_write(target, b"hmac-key-material", restrict_to_owner=True)

    assert events == ["restrict", "write"], events
    assert target.read_bytes() == b"hmac-key-material"


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX permission bits")
def test_restrict_to_owner_is_not_widened_by_the_default_mode(tmp_path):
    """Regression guard: fchmod must not undo the lockdown it runs after.

    ``restrict_to_owner`` applies 0600 to the temp, then the helper fchmods it.
    Feeding the umask default in there would widen the file straight back.
    """
    target = tmp_path / "token.json"

    aw.atomic_write(target, "{}", restrict_to_owner=True)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX permission bits")
def test_explicit_owner_only_mode_is_accepted_alongside_the_flag(tmp_path):
    """The combination the real call sites use must not trip the guard."""
    target = tmp_path / "creds.json"

    aw.atomic_write(target, "{}", mode=0o600, restrict_to_owner=True)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_a_wider_mode_alongside_restrict_to_owner_is_rejected(tmp_path):
    """Narrowing it silently would hide the contradiction in the call."""
    with pytest.raises(ValueError, match="implies 0o600"):
        aw.atomic_write(tmp_path / "x", "data", mode=0o644, restrict_to_owner=True)


def test_a_failing_lockdown_leaves_no_temp_and_no_target(tmp_path, monkeypatch):
    """Fail-loud: a secret we cannot protect must not be written at all."""

    def _boom(path, **_kw):
        raise OSError("cannot set DACL")

    monkeypatch.setattr(platform_compat, "restrict_to_owner", _boom)

    target = tmp_path / "secret.key"
    with pytest.raises(OSError, match="cannot set DACL"):
        aw.atomic_write(target, b"key", restrict_to_owner=True)

    assert not target.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def _failing_restrict(monkeypatch):
    """Make the owner-only lockdown fail the way a read-only FS or icacls would."""

    def _boom(path, **_kw):
        raise OSError("cannot set DACL")

    monkeypatch.setattr(platform_compat, "restrict_to_owner", _boom)


def test_restrict_on_error_warn_publishes_the_file_anyway(tmp_path, monkeypatch):
    """The counterpart to the fail-loud default, for callers that need the write.

    ``sel.py`` hard-fails every ``SecurityEventLog()`` init if its HMAC key is
    missing, and ``dashboard/refresh_tokens.py`` loses refresh-token
    reuse-detection state if its store is not persisted. For those two, dropping
    the write is the worse outcome, so the lockdown failure must not abort it.
    """
    _failing_restrict(monkeypatch)

    target = tmp_path / "hmac.key"
    aw.atomic_write(target, b"k" * 32, restrict_to_owner=True, restrict_on_error="warn")

    assert target.read_bytes() == b"k" * 32
    assert list(tmp_path.glob("*.tmp")) == [], "the temp must still be cleaned up"


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX permission bits")
def test_restrict_on_error_warn_still_applies_the_posix_mode(tmp_path, monkeypatch):
    """A warn is NOT the same exposure on both platforms, and this pins which.

    On POSIX ``restrict_to_owner`` is ``chmod(0o600)``, and the ``fchmod_safe``
    that follows applies the same 0600 independently — so a warn still publishes
    an owner-only file here. On Windows ``fchmod_safe`` is a no-op, so there the
    same warn genuinely publishes under the inherited ACL. Asserting the POSIX
    half stops the docstring's claim from drifting away from the code.
    """
    _failing_restrict(monkeypatch)

    target = tmp_path / "hmac.key"
    aw.atomic_write(target, b"key", restrict_to_owner=True, restrict_on_error="warn")

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_restrict_on_error_warn_does_not_log_the_payload(tmp_path, monkeypatch, caplog):
    """The warning fires on a secret write, so it must name the path, not the key."""
    _failing_restrict(monkeypatch)

    secret = b"correct-horse-battery-staple"
    target = tmp_path / "hmac.key"
    with caplog.at_level(logging.WARNING, logger="kiro_crew.atomic_write"):
        aw.atomic_write(target, secret, restrict_to_owner=True, restrict_on_error="warn")

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert logged, "premise: the warn path logged at all"
    assert secret.decode() not in logged, "the payload must never reach the log"
    assert target.name in logged, "the operator needs the destination path"


def test_restrict_on_error_without_restrict_to_owner_is_rejected(tmp_path):
    """Ignoring it would read as 'permissions handled' at a call site that has none."""
    with pytest.raises(ValueError, match="meaningless without restrict_to_owner"):
        aw.atomic_write(tmp_path / "x", "data", restrict_on_error="warn")


def test_a_raw_write_making_no_progress_fails_instead_of_hanging(tmp_path, monkeypatch):
    """A raw layer stuck at 0 bytes must raise, not spin.

    ``io.BufferedWriter`` retries a raw write reporting 0 bytes forever, so
    routing through one would convert a stalled write into an unkillable hang.
    ``sel.py`` hand-rolled this guard for the SEL HMAC key precisely so a stall
    could not wedge ``SecurityEventLog`` init; the helper owes every migrated
    site the same promise.
    """
    target = tmp_path / "stalled.json"
    monkeypatch.setattr(os, "write", lambda fd, data: 0)

    with pytest.raises(OSError, match="reported 0 bytes"):
        aw.atomic_write(target, "payload")

    assert not target.exists(), "a stalled write must not publish anything"
    assert list(tmp_path.glob("*.tmp")) == [], "the temp file must be cleaned up"


def test_a_short_raw_write_still_persists_every_byte(tmp_path, monkeypatch):
    """``write(2)`` may transfer fewer bytes than asked and report it with no error.

    Ignoring that count publishes a truncated file over a good one, so the loop
    must keep going rather than trust a single call.
    """
    real_os_write = os.write
    calls: list[int] = []

    def _short_write(fd, data):
        capped = bytes(data)[:8]
        written = real_os_write(fd, capped)
        calls.append(written)
        return written

    monkeypatch.setattr(os, "write", _short_write)

    payload = "x" * 100
    target = tmp_path / "short.txt"
    aw.atomic_write(target, payload)

    assert target.read_text(encoding="utf-8") == payload
    assert len(calls) > 1, f"premise: the write was actually fragmented ({calls})"


def test_newline_translation_survives_the_encoder(tmp_path):
    """Regression guard for the write path bypassing ``open()``.

    The bytes now come from an explicit encode rather than a text-mode file
    object, so the ``newline=`` contract has to be reproduced rather than
    inherited. A plain ``str.encode`` would drop translation entirely and
    silently stop emitting CRLF for callers that ask for it.
    """
    explicit = tmp_path / "crlf.txt"
    aw.atomic_write(explicit, "a\nb\n", newline="\r\n")
    assert explicit.read_bytes() == b"a\r\nb\r\n"

    verbatim = tmp_path / "lf.txt"
    aw.atomic_write(verbatim, "a\nb\n", newline="")
    assert verbatim.read_bytes() == b"a\nb\n"


def test_an_interrupt_mid_write_still_removes_the_temp_file(tmp_path, monkeypatch):
    """Ctrl-C during a write must not leave a temp file behind.

    ``agent._atomic_json_write`` cleaned up under ``except BaseException``, so a
    helper catching only ``Exception`` would silently regress every caller
    migrated onto it: the temp file would outlive the interrupt where the
    hand-rolled version removed it. The cleanup clause is widened to
    ``BaseException`` for that reason.

    The interrupt itself must still reach the caller, so this pins propagation
    as well as cleanup. A clause that swallowed it would turn Ctrl-C into a
    silent no-op.
    """
    target = tmp_path / "config.json"

    def _interrupt(fd, data):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "write", _interrupt)

    with pytest.raises(KeyboardInterrupt):
        aw.atomic_write(target, "payload")

    assert not target.exists(), "an interrupted write must not publish anything"
    assert list(tmp_path.glob("*.tmp")) == [], "the temp file must be cleaned up"
