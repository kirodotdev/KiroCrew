"""The pipeline-conductor skill directory and the fleet-probe test harness.

``FleetProbeHarness`` is what every ``TestFleetProbe*`` class inherits: the script
loader, the transcript and config writers, the probe runner and the readers of its
fired-line fields, the handled-map reader and backdater, and a faked ``/proc`` entry.
It carries no ``test_`` member, so pytest collects nothing from it.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from pipeline_conductor_fleet_probe_helpers import LOAD_NAME
from skill_script_helpers import load_skill_script

from conftest import make_dir_link

SKILL_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "pipeline-conductor"
)


class FleetProbeHarness:
    """The fleet-probe script run over fixtures, shared by the ``TestFleetProbe*`` classes."""

    def _mod(self):
        return load_skill_script(LOAD_NAME, SKILL_DIR / "scripts" / "fleet_probe.py")

    def _session(self, sessions_dir: Path, key: str, text: str, *, age_secs: int = 0) -> None:
        path = sessions_dir / f"{key}.jsonl"
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": text},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        if age_secs:
            stamp = time.time() - age_secs
            os.utime(path, (stamp, stamp))

    def _transcript(
        self, sessions_dir: Path, key: str, rows: list[dict], *, age_secs: int = 0
    ) -> None:
        """``_session`` for a tail whose ROLES matter -- a tool card, an error
        row, a nudge -- rather than one assistant line. The rows are written as
        given so a test can pin the shape the real writers produce."""
        path = sessions_dir / f"{key}.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        if age_secs:
            stamp = time.time() - age_secs
            os.utime(path, (stamp, stamp))

    def _config(self, tmp_path: Path, monkeypatch, sessions: list[str], **extra) -> Path:
        """Config file + the env the script derives its paths from: transcripts
        under $KIROCREW_HOME/sessions, /proc behind the test-only env seam --
        neither is a config key (containment: the config is agent-authored)."""
        (tmp_path / "sessions").mkdir(exist_ok=True)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(tmp_path / "no-proc"))
        cfg = {"sessions": sessions, **extra}
        cfg_path = tmp_path / "probe-config.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        return cfg_path

    def _run(self, mod, cfg_path: Path, capsys, *extra_args: str) -> str:
        rc = mod.main(["--config", str(cfg_path), *extra_args])
        assert rc == 0
        return capsys.readouterr().out

    @staticmethod
    def _digest_of(out: str, key: str) -> str:
        """The d= field of KEY's fired line -- what mark-handled must echo back."""
        line = next(ln for ln in out.splitlines() if key in ln and "d=" in ln)
        match = re.search(r"d=(\S+)", line)
        assert match is not None, line
        return match.group(1)

    @staticmethod
    def _index_of(out: str, key: str) -> int:
        """The i= field of KEY's fired line -- the tail position."""
        line = next(ln for ln in out.splitlines() if key in ln and "i=" in ln)
        match = re.search(r"i=(\d+)", line)
        assert match is not None, line
        return int(match.group(1))

    @staticmethod
    def _handled(cfg_path: Path) -> dict:
        """The handled map out of the DERIVED state file beside the config."""
        state = json.loads(Path(f"{cfg_path}.state.json").read_text(encoding="utf-8"))
        return state["handled"]

    @staticmethod
    def _age_mark(cfg_path: Path, key: str, secs: int) -> None:
        """Backdate KEY's disposition. NOPROGRESS requires the mark to be at least
        one idle budget old, so a test that marks and probes in the same second
        has to move the clock the way real time would."""
        state_path = Path(f"{cfg_path}.state.json")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["handled"][key]["ts"] = int(time.time()) - secs
        state_path.write_text(json.dumps(state), encoding="utf-8")

    def _proc(self, tmp_path: Path, pid: str, argv: bytes, cwd: Path | None) -> Path:
        """One fake ``/proc/<pid>``. ``cwd`` is written as a reparse link (a
        junction on Windows) because that is what the kernel exposes and what
        the probe reads.

        A ``stat`` file is always written: every live process on a real system
        has one, and the probe reads its ``starttime`` (field 22) as the process
        incarnation token that brackets the per-pid reads. A stable ``stat`` here
        means one incarnation across the scan, so cwd classification is exercised
        as it is in production; omitting it would model a process that exited
        mid-scan, which is a different case with its own tests.
        """
        proc = tmp_path / "proc"
        (proc / pid).mkdir(parents=True, exist_ok=True)
        (proc / pid / "cmdline").write_bytes(argv)
        # field 1 pid, field 2 comm, field 3 state, then starttime is field 22 --
        # index 19 in the split after ') ', where index 0 is the state field.
        stat_tail = ["0"] * 18 + ["1000"] + ["0"] * 30
        (proc / pid / "stat").write_text(f"{pid} (proc) R " + " ".join(stat_tail) + "\n", "ascii")
        if cwd is not None:
            cwd.mkdir(parents=True, exist_ok=True)
            make_dir_link(proc / pid / "cwd", cwd)
        return proc
