"""A colour file that cannot be used is never replaced by one pack's colours.

``AppearanceStore.load`` carries on with default colours whatever is on disk.
The next recolour must not then save that one pack's map over every other
pack's: a file that is not JSON is moved to ``<name>.corrupt-<utc>`` first, and a
file whose read fails is kept and every colour save is refused.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from kiro_crew.appearance_packs import store as store_mod
from kiro_crew.appearance_packs.store import DEFAULT_PACK, AppearanceStore

_NAME = "crew-companion-colours.json"
_SAVED = {DEFAULT_PACK: {"#fff": "#f0f"}, "cat": {"#000": "#0f0"}}


def _asides(directory: Path) -> list[Path]:
    return sorted(directory.glob(f"{_NAME}.corrupt-*"))


class TestBadBytesAreSetAside:
    def test_a_file_that_is_not_json_is_moved_aside_before_the_save(self, tmp_path, caplog):
        path = tmp_path / _NAME
        bad = b'{"kiro-ghost": {"#fff": "#f0f"}, "cat": {"#000"'
        path.write_bytes(bad)

        s = AppearanceStore(tmp_path)
        with caplog.at_level(logging.WARNING, logger=store_mod.__name__):
            s.load()
        assert s.set_colour_map("dog", {"#111": "#222"}) is True

        asides = _asides(tmp_path)
        assert len(asides) == 1
        assert asides[0].read_bytes() == bad
        assert json.loads(path.read_text("utf-8")) == {"dog": {"#111": "#222"}}
        assert any(asides[0].name in rec.getMessage() for rec in caplog.records)

    def test_a_file_that_is_not_utf8_is_moved_aside(self, tmp_path):
        (tmp_path / _NAME).write_bytes(b"\xff\xfe{}")
        AppearanceStore(tmp_path).load()
        assert [p.read_bytes() for p in _asides(tmp_path)] == [b"\xff\xfe{}"]

    def test_json_that_is_not_an_object_is_moved_aside(self, tmp_path):
        (tmp_path / _NAME).write_text('["#fff"]', "utf-8")
        s = AppearanceStore(tmp_path)
        s.load()
        assert [p.read_text("utf-8") for p in _asides(tmp_path)] == ['["#fff"]']
        assert s.colour_map(DEFAULT_PACK) == {}

    def test_a_good_file_is_not_moved(self, tmp_path):
        (tmp_path / _NAME).write_text(json.dumps(_SAVED), "utf-8")
        s = AppearanceStore(tmp_path)
        s.load()
        assert _asides(tmp_path) == []
        assert s.colour_map("cat") == {"#000": "#0f0"}


class TestByteOrderMark:
    def test_a_colour_file_saved_with_a_bom_reads_normally(self, tmp_path):
        path = tmp_path / _NAME
        path.write_bytes(b"\xef\xbb\xbf" + json.dumps(_SAVED).encode("utf-8"))
        s = AppearanceStore(tmp_path)
        s.load()
        assert s.colour_map(DEFAULT_PACK) == {"#fff": "#f0f"}
        assert s.colour_map("cat") == {"#000": "#0f0"}

        s.set_colour_map("dog", {"#111": "#222"})
        assert json.loads(path.read_text("utf-8")) == {**_SAVED, "dog": {"#111": "#222"}}
        assert _asides(tmp_path) == []


def _fail_reads_of(mp: pytest.MonkeyPatch, target: Path) -> None:
    """Make every read of ``target`` fail the way a transient EACCES does."""
    real_bytes = Path.read_bytes
    real_text = Path.read_text

    def read_bytes(self):
        if self == target:
            raise PermissionError(13, "Permission denied", str(self))
        return real_bytes(self)

    def read_text(self, *args, **kwargs):
        if self == target:
            raise PermissionError(13, "Permission denied", str(self))
        return real_text(self, *args, **kwargs)

    mp.setattr(Path, "read_bytes", read_bytes)
    mp.setattr(Path, "read_text", read_text)


class TestAFailedReadRefusesTheWrite:
    def test_a_recolour_is_refused_and_the_file_is_untouched(self, tmp_path, monkeypatch):
        path = tmp_path / _NAME
        original = json.dumps(_SAVED).encode("utf-8")
        path.write_bytes(original)
        s = AppearanceStore(tmp_path)
        with monkeypatch.context() as mp:
            _fail_reads_of(mp, path)
            s.load()  # must not raise
            with pytest.raises(store_mod.ColourMapsUnreadableError):
                s.set_colour_map("dog", {"#111": "#222"})

        assert path.read_bytes() == original
        assert _asides(tmp_path) == []
        # The refused recolour rolled memory back.
        assert s.colour_map("dog") == {}

    def test_a_later_load_that_reads_the_file_lifts_the_refusal(self, tmp_path, monkeypatch):
        path = tmp_path / _NAME
        path.write_text(json.dumps(_SAVED), "utf-8")
        s = AppearanceStore(tmp_path)
        with monkeypatch.context() as mp:
            _fail_reads_of(mp, path)
            s.load()

        s.load()
        s.set_colour_map("dog", {"#111": "#222"})
        assert json.loads(path.read_text("utf-8")) == {**_SAVED, "dog": {"#111": "#222"}}

    def test_a_failed_move_aside_refuses_the_write(self, tmp_path, monkeypatch):
        path = tmp_path / _NAME
        bad = b"{ broken"
        path.write_bytes(bad)

        def refuse(src, dst):
            raise PermissionError(13, "Permission denied", str(src))

        monkeypatch.setattr(store_mod, "replace_with_retry", refuse)
        s = AppearanceStore(tmp_path)
        s.load()
        with pytest.raises(store_mod.ColourMapsUnreadableError):
            s.set_colour_map("dog", {"#111": "#222"})
        assert path.read_bytes() == bad
