"""Two writers that both find the stop log full keep the archive."""

from __future__ import annotations

import json

from kiro_crew import autonudge_stop_log as stoplog

_ROW = 15  # len('{"n": "abcde"}\n'); every name below is five characters


def _names(path):
    return [json.loads(line)["n"] for line in path.read_text().splitlines()]


def test_second_rotator_does_not_overwrite_the_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(stoplog, "STOP_LOG_MAX_BYTES", 2 * _ROW)
    path = tmp_path / "logs" / stoplog.STOP_LOG_FILE
    archive = path.with_name(path.name + ".1")
    stoplog.append_records(path, [{"n": "old-1"}, {"n": "old-2"}])
    assert _names(path) == ["old-1", "old-2"]  # the live file is at its cap

    real_append = stoplog.append_line
    raced = {"done": False}

    def racing_append(p, line, *, max_bytes=None):
        # Writer B saw the full file. Before B reaches the rotation, writer A
        # rotates and appends; B then reports its stale LogFull.
        if not raced["done"]:
            raced["done"] = True
            monkeypatch.setattr(stoplog, "append_line", real_append)
            stoplog.append_records(p, [{"n": "wr-aa"}])
            monkeypatch.setattr(stoplog, "append_line", racing_append)
            raise stoplog.LogFull("stale full reading")
        return real_append(p, line, max_bytes=max_bytes)

    monkeypatch.setattr(stoplog, "append_line", racing_append)
    stoplog.append_records(path, [{"n": "wr-bb"}])

    assert _names(archive) == ["old-1", "old-2"]
    assert _names(path) == ["wr-aa", "wr-bb"]


def test_rotation_uses_the_windows_safe_replace(tmp_path, monkeypatch):
    # A plain os.replace fails on Windows while any handle is open on the log;
    # the rotation must go through the retrying helper instead.
    monkeypatch.setattr(stoplog, "STOP_LOG_MAX_BYTES", 2 * _ROW)
    path = tmp_path / "logs" / stoplog.STOP_LOG_FILE
    calls = []
    real = stoplog.replace_with_retry

    def spy(src, dst):
        calls.append((src, dst))
        real(src, dst)

    monkeypatch.setattr(stoplog, "replace_with_retry", spy)
    stoplog.append_records(path, [{"n": "old-1"}, {"n": "old-2"}, {"n": "new-1"}])
    assert calls == [(path, path.with_name(path.name + ".1"))]
    assert _names(path) == ["new-1"]
