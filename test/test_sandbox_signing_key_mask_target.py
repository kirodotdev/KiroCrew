"""A home with no token-signing key gets the gateway's own key before a namespace spawn.

The gateway creates the key at its first start, and ``mount(2)`` cannot mask an absent
path in a data-home root that is writable in-sandbox, so a sandbox spawned before then
could plant the bytes the gateway adopts. A short stub would refuse the boot, so the
launcher runs the gateway's create path instead. Every path is under ``tmp_path``.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from kiro_crew import sandbox
from kiro_crew.dashboard import token_secret


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "crew"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return home


def test_an_absent_key_is_created_as_the_key_the_gateway_signs_with(crew_home: Path) -> None:
    key = crew_home / "token_signing.key"
    established: list[str] = []

    assert sandbox._materialize_signing_key_mask_target(established) == str(key)

    created = key.read_bytes()
    assert len(created) >= token_secret._MIN_KEY_BYTES
    # POSIX-only: Windows locks the key down with an owner DACL, not st_mode bits.
    if os.name != "nt":
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert established == [str(key)]
    assert token_secret._load_or_create_secret() == created


def test_an_existing_key_is_kept_and_required(crew_home: Path) -> None:
    key = crew_home / "token_signing.key"
    key.write_bytes(b"k" * token_secret._MIN_KEY_BYTES)
    established: list[str] = []

    assert sandbox._materialize_signing_key_mask_target(established) is None

    assert key.read_bytes() == b"k" * token_secret._MIN_KEY_BYTES
    assert established == [str(key)]


def test_a_legacy_staging_temp_holds_off_the_create(crew_home: Path) -> None:
    temp = crew_home / ".token_signing.key.123.abcd.tmp"
    temp.write_bytes(b"o" * token_secret._MIN_KEY_BYTES)
    established: list[str] = []

    assert sandbox._materialize_signing_key_mask_target(established) is None

    assert not (crew_home / "token_signing.key").exists()
    assert established == []
