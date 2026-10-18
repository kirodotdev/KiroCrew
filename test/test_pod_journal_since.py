"""The systemd backend's failure tail is bounded to the current start.

The user journal keeps a unit's earlier invocations, so ``journalctl -u <unit>
-n <lines>`` after a failed ``pod up`` would also return an earlier start's
``FATAL``. ``journal_mark`` takes the time before the start and
``recent_journal(..., since=mark)`` passes it to journalctl as ``--since``.

These run on every platform: journalctl is faked at the ``subprocess.run``
boundary with a journal that honours ``--since=@<seconds>``.
"""

from __future__ import annotations

import subprocess
from decimal import Decimal

import pytest

from kiro_crew.pod import runtime as rt
from kiro_crew.pod.config import PodConfig


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> PodConfig:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
    monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods-env"))
    monkeypatch.setenv("KIROCREW_POD_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    return PodConfig.load()


class _FakeJournal:
    """A user journal of (wall-clock microseconds, line) for one unit."""

    def __init__(self) -> None:
        self.entries: list[tuple[int, str]] = []
        self.argvs: list[list[str]] = []

    def run(self, argv, **_kw) -> subprocess.CompletedProcess:
        assert argv[0] == "journalctl"
        self.argvs.append(list(argv))
        since_usec = 0
        for arg in argv:
            if arg.startswith("--since=@"):
                since_usec = int(Decimal(arg[len("--since=@") :]) * 1_000_000)
        lines = int(argv[argv.index("-n") + 1])
        kept = [line for usec, line in self.entries if usec >= since_usec][-lines:]
        out = "\n".join(kept) + "\n" if kept else "-- No entries --\n"
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=out, stderr="")


@pytest.fixture
def journal(monkeypatch) -> _FakeJournal:
    fake = _FakeJournal()
    monkeypatch.setattr(rt, "IS_MACOS", False)
    monkeypatch.setattr(rt, "IS_WINDOWS", False)
    monkeypatch.setattr(rt, "require_systemd", lambda: None)
    monkeypatch.setattr(rt.subprocess, "run", fake.run)
    return fake


def _mark_at(monkeypatch, cfg: PodConfig, usec: int) -> dict[str, int]:
    monkeypatch.setattr(rt, "time_ns", lambda: usec * 1000)
    return rt.journal_mark(cfg, "smoke")


def test_systemd_mark_leaves_out_an_earlier_starts_fatal(cfg, journal, monkeypatch):
    """A start that logged nothing is not blamed on an earlier start's FATAL."""
    journal.entries.append((1_000_000_000_000_000, "FATAL: no built dist for smoke"))
    mark = _mark_at(monkeypatch, cfg, 1_000_000_005_000_250)
    text = rt.recent_journal(cfg, "smoke", 30, since=mark)
    assert "FATAL" not in text
    assert "--since=@1000000005.000250" in journal.argvs[-1]
    # Negative control: without the mark the same journal still yields the stale line.
    assert "FATAL: no built dist" in rt.recent_journal(cfg, "smoke", 30)
    assert not any(a.startswith("--since") for a in journal.argvs[-1])


def test_systemd_mark_keeps_this_starts_fatal(cfg, journal, monkeypatch):
    """A FATAL this start logged is still shown, and only this start's lines."""
    journal.entries.append((1_000_000_000_000_000, "FATAL: no built dist for smoke"))
    mark = _mark_at(monkeypatch, cfg, 1_000_000_005_000_000)
    journal.entries.append((1_000_000_005_000_001, "FATAL: venv missing for smoke"))
    text = rt.recent_journal(cfg, "smoke", 30, since=mark)
    assert "FATAL: venv missing for smoke" in text
    assert "no built dist" not in text


def test_a_file_backend_mark_adds_no_journal_bound(cfg, journal):
    """A mark without the systemd key bounds nothing rather than guessing."""
    rt.recent_journal(cfg, "smoke", 30, since={"pod.err.log": 3})
    assert not any(a.startswith("--since") for a in journal.argvs[-1])
