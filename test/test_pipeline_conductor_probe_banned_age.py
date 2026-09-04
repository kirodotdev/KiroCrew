"""The banned-process line carries the running process's age.

The ``BANNED`` line the fleet probe emits carries ``age=<secs>s`` (or ``age=?s``
when the age is unavailable), derived at scan time from ``/proc/<pid>/stat`` field
22 plus ``os.sysconf`` for the clock tick rate. An age that grows across cycles
marks one process still alive; a small age under a re-appearing pid marks a fresh
violation on a recycled pid number. The age is a fact about the running process,
so it costs no state file and no second writer, and the probe stays read-only
outside ``--mark-handled``.

The field is never omitted. Where ``/proc``/``os.sysconf`` are absent (Windows)
the helper returns None and the caller emits ``age=?s``, so a reader sees an
explicit unknown instead of assuming the process is new. These tests drive the
probe over a fake ``/proc`` (the ``KIROCREW_PROBE_PROC_ROOT`` seam the script
exposes) and also pin the no-tick-rate path directly.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

#: The two banned-line tests build a fake ``/proc`` and a ``cwd`` symlink and let
#: the probe follow it. Both are POSIX process semantics: ``os.symlink`` needs a
#: privilege Windows CI does not grant, and the probe reads ``/proc/<pid>/cwd`` as
#: a symlink. The age-helper unit test below is pure file reads and runs anywhere.
_POSIX_PROC_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="fake /proc + cwd symlink are POSIX process semantics"
)

SKILL_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "pipeline-conductor"
)


def _fake_proc_with_banned_pytest(
    proc_root: Path, fleet_root: Path, pid: str, *, starttime_ticks: int, uptime_secs: float
) -> None:
    """A ``/proc`` holding one pid whose cmdline is a bare (unbounded) pytest run
    inside *fleet_root*, plus the ``stat``/``uptime`` the age helper reads.

    The cmdline is a real NUL-separated argv with ``argv[0]`` a pytest path, so it
    is neither a shell wrapper (no ``exe`` link -> no exemption) nor bounded (no
    ``-n``), and the ``cwd`` symlink into the fleet worktree makes it ``cwd=fleet``.
    """
    proc_root.mkdir(parents=True, exist_ok=True)
    fleet_root.mkdir(parents=True, exist_ok=True)
    (proc_root / "uptime").write_text(f"{uptime_secs} 0.0\n", encoding="ascii")
    pdir = proc_root / pid
    pdir.mkdir()
    argv = [str(fleet_root / ".venv" / "bin" / "pytest"), "test/test_x.py", "-q"]
    (pdir / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    # field 1 pid, field 2 comm in parens (can hold spaces), field 3 state, then
    # starttime is field 22 -- index 19 in the split AFTER the ') ', where index 0
    # is the state field. So 18 padding fields sit between state and starttime.
    tail_fields = ["0"] * 18 + [str(starttime_ticks)] + ["0"] * 30
    (pdir / "stat").write_text(
        f"{pid} (pytest) R " + " ".join(tail_fields) + "\n", encoding="ascii"
    )
    os.symlink(str(fleet_root), str(pdir / "cwd"))


def _run(tmp_path, monkeypatch, fleet_root: Path) -> str:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(tmp_path / "proc"))
    mod = load_skill_script("fleet_probe", SKILL_DIR / "scripts" / "fleet_probe.py")
    cfg_path = tmp_path / "probe-config.json"
    cfg_path.write_text(
        json.dumps({"sessions": [], "fleet_worktrees": [str(fleet_root)]}), encoding="utf-8"
    )
    assert mod.main(["--config", str(cfg_path)]) == 0
    return cfg_path, mod


class TestBannedLineCarriesAge:
    @_POSIX_PROC_ONLY
    def test_banned_line_reports_the_process_age(self, tmp_path, capsys, monkeypatch):
        """A fleet-owned unbounded pytest alive for ~600s prints ``age=600s`` on its
        BANNED line, so a re-emitted line whose age keeps growing is readable as one
        unkilled process rather than a fresh offender on a recycled pid."""
        fleet_root = tmp_path / "wt-worker"
        # Build starttime from the REAL clock tick rate so the expected age is 600s
        # whatever the host's rate is -- the helper reads the same os.sysconf, so
        # this needs no monkeypatch (patching os.sysconf is what broke on Windows,
        # where the attribute does not exist to replace).
        hz = os.sysconf("SC_CLK_TCK")
        _fake_proc_with_banned_pytest(
            tmp_path / "proc", fleet_root, "4242", starttime_ticks=400 * hz, uptime_secs=1000.0
        )
        _run(tmp_path, monkeypatch, fleet_root)
        out = capsys.readouterr().out
        assert "BANNED pid=4242" in out, out
        assert "cwd=fleet" in out, out
        assert "age=600s" in out, out

    @_POSIX_PROC_ONLY
    def test_unreadable_age_prints_question_mark(self, tmp_path, capsys, monkeypatch):
        """A pid whose ``stat`` is missing (the process exited between the cmdline
        read and the age read -- the common short-lived case) prints ``age=?s`` and
        still emits the line; the age never blocks the signal or crashes the scan."""
        fleet_root = tmp_path / "wt-worker"
        hz = os.sysconf("SC_CLK_TCK")
        _fake_proc_with_banned_pytest(
            tmp_path / "proc", fleet_root, "4243", starttime_ticks=400 * hz, uptime_secs=1000.0
        )
        (tmp_path / "proc" / "4243" / "stat").unlink()
        _run(tmp_path, monkeypatch, fleet_root)
        out = capsys.readouterr().out
        assert "BANNED pid=4243" in out, out
        assert "age=?s" in out, out

    def test_age_helper_reads_starttime_after_a_paren_heavy_comm(self, tmp_path):
        """``_proc_age_secs`` resumes the field parse after the LAST ``)``, so a
        comm containing spaces and parentheses -- ``(sh )evil)`` -- does not shift
        the starttime field and mis-read the age. Cross-platform: pure file reads,
        skipped only where ``os.sysconf`` (the tick rate) is unavailable, which is
        the same platform on which the helper returns None."""
        if not hasattr(os, "sysconf"):
            pytest.skip("os.sysconf is POSIX-only; the helper returns None without it")
        proc_root = tmp_path / "proc"
        (proc_root / "9").mkdir(parents=True)
        (proc_root / "uptime").write_text("1000.0 0.0\n", encoding="ascii")
        hz = os.sysconf("SC_CLK_TCK")
        starttime_ticks = 400 * hz  # 400s into boot, whatever the tick rate is
        tail = ["0"] * 18 + [str(starttime_ticks)] + ["0"] * 5
        (proc_root / "9" / "stat").write_text("9 (sh )evil) R " + " ".join(tail) + "\n", "ascii")
        mod = load_skill_script("fleet_probe", SKILL_DIR / "scripts" / "fleet_probe.py")
        # starttime 400s in, uptime 1000s -> age 600s, independent of clk_tck.
        # The token is required; pass the matching incarnation.
        assert mod._proc_age_secs(proc_root, "9", starttime_ticks) == 600

    def test_age_is_none_without_a_clock_tick_rate(self, tmp_path, monkeypatch):
        """Cross-platform contract: where ``os.sysconf`` is unavailable (Windows)
        the tick rate is unknown, so the age is uncomputable and the helper returns
        None rather than guessing a rate -- which the caller renders as ``age=?s``.
        The field is present as an explicit unknown, never a wrong number and never
        absent. Simulated by removing ``os.sysconf`` so the test runs everywhere."""
        proc_root = tmp_path / "proc"
        (proc_root / "9").mkdir(parents=True)
        (proc_root / "uptime").write_text("1000.0 0.0\n", encoding="ascii")
        tail = ["0"] * 18 + ["40000"] + ["0"] * 5
        (proc_root / "9" / "stat").write_text("9 (pytest) R " + " ".join(tail) + "\n", "ascii")
        mod = load_skill_script("fleet_probe", SKILL_DIR / "scripts" / "fleet_probe.py")
        monkeypatch.delattr(os, "sysconf", raising=False)
        # Token matches (40000), so None comes from the missing tick rate, not a
        # mismatch -- the property under test.
        assert mod._proc_age_secs(proc_root, "9", 40000) is None

    def test_age_refused_when_the_incarnation_token_moved(self, tmp_path):
        """cmdline and age are separate /proc reads; a pid recycled between them
        would splice one process's identity onto another's age. When the
        starttime read at emit does not match the token captured at scan, the
        helper returns None (the caller renders ``age=?s``) rather than a spliced
        age. The matching token still yields the age, so the guard only fires on a
        genuine mismatch."""
        if not hasattr(os, "sysconf"):
            pytest.skip("os.sysconf is POSIX-only; the helper returns None without it")
        proc_root = tmp_path / "proc"
        (proc_root / "9").mkdir(parents=True)
        (proc_root / "uptime").write_text("1000.0 0.0\n", encoding="ascii")
        hz = os.sysconf("SC_CLK_TCK")
        starttime_ticks = 400 * hz
        tail = ["0"] * 18 + [str(starttime_ticks)] + ["0"] * 5
        (proc_root / "9" / "stat").write_text("9 (pytest) R " + " ".join(tail) + "\n", "ascii")
        mod = load_skill_script("fleet_probe", SKILL_DIR / "scripts" / "fleet_probe.py")
        # Token captured at scan matches the live starttime -> age is trusted.
        assert mod._proc_age_secs(proc_root, "9", starttime_ticks) == 600
        # A different token means the pid was recycled between the reads -> refuse.
        assert mod._proc_age_secs(proc_root, "9", starttime_ticks + 1) is None
        # The token is required: a None token (starttime unreadable at capture)
        # also refuses, since an age with no incarnation to anchor it is untrusted.
        assert mod._proc_age_secs(proc_root, "9", None) is None

    @_POSIX_PROC_ONLY
    def test_banned_line_prints_unknown_age_when_pid_recycled_mid_scan(
        self, tmp_path, capsys, monkeypatch
    ):
        """End to end: the probe captures the incarnation token before its per-pid
        reads and re-checks it at emit. When the process's ``stat`` reports a
        different starttime at emit than at scan (a recycled pid), the WHOLE record
        is stale, so the line still fires but carries `cwd=unknown` (never a
        spliced `cwd=fleet` that would stop an innocent worker) and `age=?s`."""
        fleet_root = tmp_path / "wt-worker"
        hz = os.sysconf("SC_CLK_TCK")
        _fake_proc_with_banned_pytest(
            tmp_path / "proc", fleet_root, "4244", starttime_ticks=400 * hz, uptime_secs=1000.0
        )
        mod = load_skill_script("fleet_probe", SKILL_DIR / "scripts" / "fleet_probe.py")
        # Make the emit-time starttime read differ from the scan-time one: the
        # scan captures start_tok first, then re-reads it before emit, so flipping
        # the value after the first call models the pid being recycled between the
        # bracketing reads without racing a real process.
        real = mod._proc_starttime_ticks
        calls = {"n": 0}

        def flipping(proc_root, pid):
            calls["n"] += 1
            base = real(proc_root, pid)
            return base if calls["n"] == 1 else (None if base is None else base + 1)

        monkeypatch.setattr(mod, "_proc_starttime_ticks", flipping)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(tmp_path / "proc"))
        cfg = tmp_path / "probe-config.json"
        cfg.write_text(
            json.dumps({"sessions": [], "fleet_worktrees": [str(fleet_root)]}), encoding="utf-8"
        )
        assert mod.main(["--config", str(cfg)]) == 0
        out = capsys.readouterr().out
        assert "BANNED pid=4244" in out, out
        assert "cwd=unknown" in out, out
        assert "age=?s" in out, out
