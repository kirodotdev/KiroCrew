"""The decision-seam consent keystone is saved in place, and a failed save leaves it usable.

A running Linux sandbox bind-mounts ``decisions_consent.json`` read-only, and a rename
over that name would detach the mount there, so ``consent.save_enabled`` and
``consent.rebind_if_enabled`` write through ``atomic_write.rewrite_in_place``. These
tests pin what that writer gives the consent record: the inode survives a save, an
empty (cut-off) record is refused rather than saved over, and a failed write puts the
previous record back, or ``{}`` for a record the save created.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import atomic_write
from kiro_crew.config.sections import DECISION_PROVIDER_ENDPOINT_DEFAULT as DEFAULT_ENDPOINT
from kiro_crew.decisions import consent

CUSTOM = "https://proxy.example/v1/systemone"


@pytest.fixture
def keystone(tmp_path, monkeypatch):
    path = tmp_path / "decisions_consent.json"
    monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: path)
    return path


class TestInPlaceSave:
    def test_a_save_rewrites_the_existing_record_in_place(self, keystone):
        # A running Linux sandbox bind-mounts this inode read-only; a rename over it
        # would detach that mount and leave the new file writable inside the sandbox.
        keystone.write_text("{}", encoding="utf-8")
        inode = keystone.stat().st_ino
        consent.save_enabled(True, endpoint=CUSTOM)
        assert keystone.stat().st_ino == inode
        assert consent.permits(CUSTOM) is True
        assert consent.rebind_if_enabled(DEFAULT_ENDPOINT) is True
        assert keystone.stat().st_ino == inode
        consent.save_enabled(False, endpoint=DEFAULT_ENDPOINT)
        assert keystone.stat().st_ino == inode
        assert json.loads(keystone.read_text(encoding="utf-8"))["enabled"] is False

    def test_refuses_to_save_over_an_empty_file(self, keystone):
        # A save cut off inside its in-place rewrite leaves an empty record. A
        # re-consent written over it would drop the browser opt-outs it held.
        keystone.write_text("", encoding="utf-8")
        with pytest.raises(consent.ConsentCorruptError):
            consent.save_enabled(True, endpoint=CUSTOM)
        assert keystone.read_text() == ""

    def test_a_failed_save_keeps_the_previous_record(self, keystone, monkeypatch):
        # The record is emptied before the new bytes land, so a write that fails then
        # (a full volume) must put the old record back: a reported failure must not
        # leave the consent or its browser opt-out changed.
        consent.save_enabled(True, endpoint=CUSTOM, browser=False)
        before = keystone.read_bytes()
        real_write_all = atomic_write._write_all

        def _full_volume(fd, data, path):
            if data != before:
                raise OSError("no space left on device")
            real_write_all(fd, data, path)

        monkeypatch.setattr(atomic_write, "_write_all", _full_volume)
        with pytest.raises(OSError):
            consent.save_enabled(False, endpoint=DEFAULT_ENDPOINT, browser=True)
        assert keystone.read_bytes() == before
        assert consent.permits(CUSTOM) is True

    def test_a_failed_first_save_keeps_the_record_it_created(self, keystone, monkeypatch):
        # A sandbox spawned during the save may already have sealed the new inode, so
        # it is kept, not removed; it holds the absent document, so the next save is
        # not refused as corrupt.
        real_write_all = atomic_write._write_all

        def _full_volume(fd, data, path):
            if data != b"{}\n":
                raise OSError("no space left on device")
            real_write_all(fd, data, path)

        monkeypatch.setattr(atomic_write, "_write_all", _full_volume)
        with pytest.raises(OSError):
            consent.save_enabled(True, endpoint=CUSTOM)
        assert keystone.read_bytes() == b"{}\n"
        assert consent.permits(CUSTOM) is False
        assert consent.read_state_strict() == {}
