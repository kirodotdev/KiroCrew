"""A VAPID key file that exists but cannot be read is kept, never replaced.

Every browser push subscription is bound to the public key in
``web-push/vapid_keys.json``. Replacing that key makes every device stop getting
pushes, so only a missing file gets a fresh keypair. A file that fails to read
(an OS error, bytes that are not UTF-8) raises ``VapidKeysUnreadable`` and stays
byte-for-byte as it was; the next call reads it again. A file saved with a
UTF-8 byte-order mark reads as its JSON.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from kiro_crew.notifications import vapid_keys


@pytest.fixture
def key_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr("kiro_crew.notifications.vapid_keys.config_dir", lambda: tmp_path)
    vapid_keys.reset_cache_for_tests()
    vapid_keys.public_key_b64url()  # first use writes the keypair
    vapid_keys.reset_cache_for_tests()
    yield tmp_path / "web-push" / "vapid_keys.json"
    vapid_keys.reset_cache_for_tests()


def _error_of(call: Callable[[], object]) -> Exception | None:
    """The exception *call* raised, or ``None`` when it returned."""
    try:
        call()
    except Exception as exc:  # the caller asserts the file first, then the type
        return exc
    return None


def test_a_read_error_keeps_the_key_file(key_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = key_file.read_bytes()
    original_public = json.loads(before)["public_b64url"]
    real_read_text = Path.read_text

    def _failing_read_text(self: Path, *args, **kwargs):
        if self == key_file:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _failing_read_text)

    error = _error_of(vapid_keys.public_key_b64url)
    assert key_file.read_bytes() == before, "the key file was replaced"
    assert isinstance(error, vapid_keys.VapidKeysUnreadable)

    # Once the file reads again, the same key comes back.
    monkeypatch.setattr(Path, "read_text", real_read_text)
    assert vapid_keys.public_key_b64url() == original_public


def test_bytes_that_are_not_utf8_keep_the_key_file(key_file: Path) -> None:
    key_file.write_bytes(b"\xff\xfe" + key_file.read_bytes())
    before = key_file.read_bytes()

    error = _error_of(vapid_keys.public_key_b64url)
    assert key_file.read_bytes() == before, "the key file was replaced"
    assert isinstance(error, vapid_keys.VapidKeysUnreadable)


def test_a_byte_order_mark_reads_as_the_same_key(key_file: Path) -> None:
    original_public = json.loads(key_file.read_text(encoding="utf-8"))["public_b64url"]
    key_file.write_bytes(b"\xef\xbb\xbf" + key_file.read_bytes())
    before = key_file.read_bytes()

    assert vapid_keys.public_key_b64url() == original_public
    assert key_file.read_bytes() == before


def test_a_missing_file_gets_a_new_keypair(key_file: Path) -> None:
    key_file.unlink()

    assert vapid_keys.public_key_b64url()
    assert key_file.is_file()
