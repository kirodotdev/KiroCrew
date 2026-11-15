"""A pidfile that cannot be read is not rewritten from an empty read.

The pidfile is the only handle on backends a killed gateway left running. A
rewrite that treated an unreadable or cut-off file as empty published a file
holding only its own row, so every other app's backend was never reaped.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import kiro_crew.apps.backend as backend_mod

_ROWS = {
    "alpha": {"pid": 4001, "start_time": "ST-A", "port": 9101},
    "beta": {"pid": 4002, "start_time": "ST-B", "port": 9102},
}


@pytest.fixture
def pidfile(tmp_path, monkeypatch):
    path = tmp_path / "app_backends.pids.json"
    monkeypatch.setattr(backend_mod, "_pidfile_path", lambda: path)
    return path


def _cut_off(path):
    text = json.dumps(_ROWS)
    path.write_text(text[: len(text) // 2])
    return path.read_bytes()


def _unopenable(monkeypatch):
    real_open = open

    def fake_open(file, *args, **kwargs):
        if str(file).endswith("app_backends.pids.json"):
            raise PermissionError(13, "Permission denied", str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)


def test_recording_a_spawn_keeps_the_other_rows_when_the_file_cannot_be_opened(
    pidfile, monkeypatch
):
    pidfile.write_text(json.dumps(_ROWS))
    before = pidfile.read_bytes()
    with patch.object(backend_mod, "_proc_start_time", return_value="ST-C"):
        with monkeypatch.context() as m:
            _unopenable(m)
            backend_mod._record_app_pid("gamma", 4003, 9103)
    assert pidfile.read_bytes() == before


def test_recording_a_spawn_sets_a_cut_off_file_aside_instead_of_dropping_it(pidfile):
    cut = _cut_off(pidfile)
    with patch.object(backend_mod, "_proc_start_time", return_value="ST-C"):
        backend_mod._record_app_pid("gamma", 4003, 9103)
    set_aside = list(pidfile.parent.glob(pidfile.name + ".corrupt-*"))
    assert [path.read_bytes() for path in set_aside] == [cut]
    assert json.loads(pidfile.read_text()) == {
        "gamma": {"pid": 4003, "start_time": "ST-C", "port": 9103}
    }


def test_recording_a_spawn_into_a_missing_file_still_creates_it(pidfile):
    with patch.object(backend_mod, "_proc_start_time", return_value="ST-C"):
        assert backend_mod._record_app_pid("gamma", 4003, 9103) == "ST-C"
    assert list(json.loads(pidfile.read_text())) == ["gamma"]


def test_the_reap_keeps_kept_rows_when_the_file_is_cut_off_mid_scan(pidfile, monkeypatch):
    rows = {
        "old": {"pid": 4321, "start_time": "ST-1", "port": 9100},
        "kept": {"pid": 4322, "start_time": "ST-2", "port": 9101},
    }
    backend_mod._write_pidfile(rows)
    state = {"cut": None}

    def start_time(pid):
        if pid == 4321 and state["cut"] is None:
            # the file is damaged after the opening read, before the closing one
            pidfile.write_text('{"kept": {"pid": 43')
            state["cut"] = pidfile.read_bytes()
        return "ST-1" if pid == 4321 else None  # 'kept': identity unreadable, row kept

    # Mock at the platform seam the reap calls, so the same fake processes run on
    # every platform: both pids are alive, the matched one stops at its terminate.
    compat = backend_mod.platform_compat
    monkeypatch.setattr(backend_mod, "_proc_start_time", start_time)
    monkeypatch.setattr(compat, "pid_liveness", lambda _pid: compat.PID_ALIVE)
    monkeypatch.setattr(
        compat, "kill_process_tree_pinned", lambda pid, expected, sig, **kwargs: True
    )
    monkeypatch.setattr(backend_mod, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(backend_mod, "sel", lambda: None)

    backend_mod._reap_stale_app_backends()

    assert state["cut"] is not None
    assert pidfile.read_bytes() == state["cut"]
