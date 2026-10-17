"""The ACP frame recorder on Windows: one stand-down line, nothing written.

Platform-neutral (``IS_POSIX`` is monkeypatched), so it runs on every CI
runner, unlike ``test_acp_frame_record.py`` which is skipped off POSIX.
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew.acp import _frame_record


@pytest.fixture(autouse=True)
def _isolated_recorder(monkeypatch):
    _frame_record._reset_for_tests()
    monkeypatch.delenv(_frame_record.ENV_RECORD_FRAMES, raising=False)
    yield
    _frame_record._reset_for_tests()


def _clean(monkeypatch):
    _frame_record._reset_for_tests()
    monkeypatch.delenv(_frame_record.ENV_RECORD_FRAMES, raising=False)


def test_windows_stands_down_with_a_named_reason(monkeypatch, tmp_path, caplog):
    """The recorder is POSIX-only: Windows cannot pin the destination by
    descriptor (no ``dir_fd``/``O_NOFOLLOW``, DACL applied by path after the
    open), so an owner-only recording cannot be guaranteed there. Setting the
    env var on Windows must not raise into the reader, must write nothing, and
    must say why in the one stand-down line."""
    _clean(monkeypatch)
    monkeypatch.setattr(_frame_record.platform_compat, "IS_POSIX", False)
    dest = tmp_path / "frames"
    monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(dest))
    with caplog.at_level(logging.WARNING, logger=_frame_record.logger.name):
        assert _frame_record.start_recorder() is False, "a Windows process started a writer"
        assert _frame_record._writer is None
        # And the write path refuses on its own too, should it ever be reached.
        _frame_record._stood_down = False
        _frame_record.write_frame("kas", {"jsonrpc": "2.0"}, str(dest))
    assert not dest.exists(), "a Windows process created the recording directory"
    assert _frame_record.recording_destination() == ""
    assert caplog.text.count("POSIX-only") == 2, caplog.text
