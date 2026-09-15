"""Exercise the Linux E2E scheduler with processes, not fake E2E proof.

These tests also run via unittest without installing a gateway or browser.
Only the workflow wiring check needs PyYAML. Pytest collects the same cases.
"""

from __future__ import annotations

import json
import os
import runpy
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "ci_e2e_parallel.py"
# Load only the CI helper: no application import or private-MCP test stand-in.
DRIVE = """
import json, runpy, sys
m = runpy.run_path(sys.argv[1])
raise SystemExit(m['_supervise'](json.loads(sys.argv[2]), **json.loads(sys.argv[3])))
"""
PROBE = """
import os, pathlib, signal, subprocess, sys, time
root, name, rc, gated, descendant = sys.argv[1:]
root = pathlib.Path(root)
if descendant == 'yes':
    child = subprocess.Popen([
        sys.executable, '-c',
        'import os, pathlib, signal, sys, time; '
        'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
        'pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)',
        str(root / (name + '.child')),
    ], start_new_session=True)
    deadline = time.monotonic() + 10
    while not (root / (name + '.child')).exists():
        assert time.monotonic() < deadline, 'detached child never became ready'
        time.sleep(.01)
(root / (name + '.started')).write_text(str(os.getpid()))
deadline = time.monotonic() + 15
while not all((root / (n + '.started')).exists() for n in ('a', 'b')):
    assert time.monotonic() < deadline, 'sibling never started: execution is serial'
    time.sleep(.01)
while gated == 'yes' and not (root / 'release').exists():
    assert time.monotonic() < deadline, 'test never released lane'
    time.sleep(.01)
(root / (name + '.done')).write_text(rc)
raise SystemExit(int(rc))
"""


@unittest.skipUnless(sys.platform == "linux", "scheduler belongs to the Ubuntu-only E2E job")
class TestE2eScheduling(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="e2e-scheduling-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def wait_file(self, name):
        deadline = time.monotonic() + 10
        path = self.root / name
        while not path.exists() or path.stat().st_size == 0:
            self.assertLess(time.monotonic(), deadline, f"missing {name}")
            time.sleep(0.01)
        return path

    def command(self, name, rc=0, gated=False, descendant=False):
        return [
            sys.executable,
            "-c",
            PROBE,
            str(self.root),
            name,
            str(rc),
            "yes" if gated else "no",
            "yes" if descendant else "no",
        ]

    def drive(self, commands, **kwargs):
        return [sys.executable, "-c", DRIVE, str(HELPER), json.dumps(commands), json.dumps(kwargs)]

    def start(self, commands, **kwargs):
        env = dict(os.environ)
        env.pop("PYTEST_ADDOPTS", None)
        proc = subprocess.Popen(
            self.drive(commands, **kwargs),
            cwd=self.root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            start_new_session=True,
        )

        def cleanup():
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
            proc.communicate(timeout=15)

        self.addCleanup(cleanup)
        return proc

    def test_overlap_and_both_awaited_with_either_failure_order(self):
        for early, first_rc, last_rc in (
            ("a", 0, 0),
            ("b", 0, 0),
            ("a", 7, 0),
            ("b", 9, 0),
            ("a", 0, 11),
            ("b", 0, 13),
            ("a", 7, 11),
            ("b", 9, 13),
        ):
            with self.subTest(early=early, first_rc=first_rc, last_rc=last_rc):
                # Unique directories prevent an earlier case's barrier from
                # satisfying the next one, including on a slow test host.
                case = self.root / f"{early}-{first_rc}-{last_rc}"
                case.mkdir()
                original = self.root
                self.root = case
                late = "b" if early == "a" else "a"
                proc = self.start(
                    {
                        early: self.command(early, first_rc),
                        late: self.command(late, last_rc, gated=True),
                    }
                )
                self.wait_file(early + ".done")
                # The early process cannot finish unless BOTH have started.
                # The late process cannot finish until we explicitly release it.
                self.assertIsNone(proc.poll())
                (self.root / "release").touch()
                output, _ = proc.communicate(timeout=10)
                failures = {rc for rc in (first_rc, last_rc) if rc}
                self.assertIn(proc.returncode, failures or {0}, output)
                self.assertTrue((self.root / (late + ".done")).exists())
                self.assertIn(f"[e2e:{early}] exit={first_rc}", output)
                self.assertIn(f"[e2e:{late}] exit={last_rc}", output)
                self.root = original

    def test_cancel_drains_both_lanes_and_detached_grandchildren(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=int(sig)):
                case = self.root / str(sig)
                case.mkdir()
                original = self.root
                self.root = case
                # Match CI: two lane supervisors, each owning its pytest-like
                # command, which itself spawns a setsid gateway-like child.
                commands = {
                    name: self.drive({name: self.command(name, gated=True, descendant=True)})
                    for name in ("a", "b")
                }
                proc = self.start(commands, grace=5)
                pids = [
                    int(self.wait_file(name + suffix).read_text())
                    for name in ("a", "b")
                    for suffix in (".started", ".child")
                ]
                proc.send_signal(sig)
                output, _ = proc.communicate(timeout=12)
                self.assertEqual(proc.returncode, 128 + sig, output)
                for pid in pids:
                    # Linux only, and absence (not zombie state) proves reaping.
                    self.assertFalse(Path(f"/proc/{pid}").exists(), f"unreaped child {pid}")
                self.root = original

    def test_lane_timeout_is_failure_and_sibling_still_finishes(self):
        proc = self.start(
            {
                "a": self.drive({"a": self.command("a", gated=True, descendant=True)}, timeout=5),
                "b": self.command("b", gated=True),
            }
        )
        child = int(self.wait_file("a.child").read_text())
        self.wait_file("b.started")
        # Release the healthy sibling only after the timed-out tree is reaped.
        deadline = time.monotonic() + 10
        while Path(f"/proc/{child}").exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.assertIsNone(proc.poll())
        (self.root / "release").touch()
        output, _ = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 124, output)
        self.assertTrue((self.root / "b.done").exists())

    def test_successful_parent_cannot_leave_detached_child_and_pass(self):
        proc = self.start(
            {
                "a": self.command("a", descendant=True),
                "b": self.command("b"),
            }
        )
        child = int(self.wait_file("a.child").read_text())
        output, _ = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 1, output)
        self.assertFalse(Path(f"/proc/{child}").exists())

    def test_adopted_dead_children_do_not_fail_clean_lane_or_hide_sibling_failure(self):
        orphan = """
import os, pathlib, sys, time
root, detached = pathlib.Path(sys.argv[1]), int(sys.argv[2])
pid = os.fork()
if pid == 0:
    if detached:
        os.setsid()
    os._exit(0)
# Observe exit WITHOUT reaping: the supervisor must adopt this exact zombie.
os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
(root / 'zombie').write_text(str(pid))
(root / 'a.started').write_text(str(os.getpid()))
deadline = time.monotonic() + 10
while not (root / 'release').exists():
    assert time.monotonic() < deadline, 'test never released zombie parent'
    time.sleep(.01)
os._exit(0)
"""
        for detached in (0, 1):
            for sibling_rc in (0, 17):
                with self.subTest(detached=detached, sibling_rc=sibling_rc):
                    original = self.root
                    self.root = original / f"zombie-{detached}-{sibling_rc}"
                    self.root.mkdir()
                    try:
                        proc = self.start(
                            {
                                "a": [sys.executable, "-c", orphan, str(self.root), str(detached)],
                                "b": self.command("b", sibling_rc),
                            }
                        )
                        pid = int(self.wait_file("zombie").read_text())
                        # A zombie still appears in /proc, but cannot execute.
                        stat = Path(f"/proc/{pid}/stat").read_text()
                        self.assertEqual(stat.rsplit(")", 1)[1].split()[0], "Z")
                        self.wait_file("b.done")
                        (self.root / "release").touch()
                        output, _ = proc.communicate(timeout=10)
                        self.assertEqual(proc.returncode, sibling_rc, output)
                        self.assertIn("[e2e:a] exit=0", output)
                        self.assertIn(f"[e2e:b] exit={sibling_rc}", output)
                        self.assertNotIn("left children alive", output)
                        self.assertFalse(Path(f"/proc/{pid}").exists())
                    finally:
                        self.root = original

    def test_reaps_adopted_zombie_while_its_lane_is_still_running(self):
        probe = """
import os, pathlib, sys, time
root = pathlib.Path(sys.argv[1])
parent = os.fork()
if parent == 0:
    child = os.fork()
    if child == 0:
        os.setsid()
        os._exit(0)
    os.waitid(os.P_PID, child, os.WEXITED | os.WNOWAIT)
    (root / 'zombie').write_text(str(child))
    os._exit(0)
os.waitpid(parent, 0)
# Like harness teardown, await disappearance before allowing the lane to exit.
pid = int((root / 'zombie').read_text())
deadline = time.monotonic() + 3
while pathlib.Path(f'/proc/{pid}').exists():
    assert time.monotonic() < deadline, 'adopted zombie blocks teardown'
    time.sleep(.01)
print('adopted zombie reaped before lane exit', flush=True)
"""
        proc = self.start({"probe": [sys.executable, "-c", probe, str(self.root)]})
        output, _ = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, output)
        self.assertIn("adopted zombie reaped before lane exit", output)
        pid = int(self.wait_file("zombie").read_text())
        self.assertFalse(Path(f"/proc/{pid}").exists())

    def test_final_drain_reaps_dead_child_without_live_residue(self):
        probe = """
import os, pathlib, runpy, sys
m = runpy.run_path(sys.argv[1])
pid = os.fork()
if pid == 0:
    os._exit(0)
try:
    os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
    assert not m['_drain'](.1, {}), 'dead child counted as live residue'
    assert not pathlib.Path(f'/proc/{pid}').exists(), 'zombie was not reaped'
finally:
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
"""
        proc = self.start({"probe": [sys.executable, "-c", probe, str(HELPER)]})
        output, _ = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, output)

    def test_drain_preserves_direct_popen_exit_status(self):
        probe = """
import pathlib, runpy, subprocess, sys, time
m = runpy.run_path(sys.argv[1])
ready = pathlib.Path('ready')
code = '''import pathlib, signal, sys, time
signal.signal(signal.SIGTERM, lambda *_: sys.exit(23))
pathlib.Path('ready').touch()
time.sleep(60)
'''
popen = subprocess.Popen
children = []
def spawn(*args, **kwargs):
    child = popen(*args, **kwargs)
    children.append(child)
    deadline = time.monotonic() + 10
    while not ready.exists():
        assert time.monotonic() < deadline
        time.sleep(.01)
    return child
subprocess.Popen = spawn
try:
    result = m['_supervise']({'child': [sys.executable, '-c', code]}, timeout=.1, grace=.5)
    print('result=', result, 'status=', children[0].returncode, flush=True)
finally:
    for child in children:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)
"""
        proc = self.start({"probe": [sys.executable, "-c", probe, str(HELPER)]})
        output, _ = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, output)
        self.assertIn("result= 124 status= 23", output)

    def test_spawn_failure_drains_the_already_started_lane(self):
        proc = self.start(
            {
                "a": [sys.executable, "-c", "import time; time.sleep(60)"],
                "b": [str(self.root / "missing-executable")],
            }
        )
        output, _ = proc.communicate(timeout=10)
        self.assertNotEqual(proc.returncode, 0, output)
        self.assertIn("FileNotFoundError", output)

    def test_lane_entrypoints_keep_environment_and_temp_roots_separate(self):
        boot = """
import os, runpy, sys
m = runpy.run_path(sys.argv[1])
lane, root = sys.argv[2:]
os.environ['RUNNER_TEMP'] = root
os.environ.pop('KIROCREW_E2E', None)
os.environ['KIROCREW_E2E_REQUIRE'] = '1'
os.environ['KIROCREW_STRICT_ON_LOOP_PERSIST'] = '1'
probe = '''import json, os, pathlib, sys
pathlib.Path(sys.argv[1]).write_text(json.dumps({k: os.environ.get(k) for k in
('TMPDIR', 'KIROCREW_E2E', 'KIROCREW_E2E_REQUIRE', 'KIROCREW_STRICT_ON_LOOP_PERSIST')}))'''
m['COMMANDS'][lane] = [sys.executable, '-c', probe, root + '/' + lane + '.json']
sys.argv = [sys.argv[1], lane]
raise SystemExit(m['main']())
"""
        proc = self.start(
            {
                lane: [sys.executable, "-c", boot, str(HELPER), lane, str(self.root)]
                for lane in ("smoke", "memory-ui", "i18n")
            }
        )
        output, _ = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, output)
        smoke = json.loads((self.root / "smoke.json").read_text())
        ui = json.loads((self.root / "memory-ui.json").read_text())
        self.assertEqual(smoke["TMPDIR"], str(self.root / "e2e-s"))
        self.assertEqual(ui["TMPDIR"], str(self.root / "e2e-u"))
        self.assertIsNone(smoke["KIROCREW_E2E"])
        self.assertEqual(ui["KIROCREW_E2E"], "1")
        i18n = json.loads((self.root / "i18n.json").read_text())
        self.assertEqual(i18n["TMPDIR"], str(self.root / "e2e-i"))
        self.assertIsNone(i18n["KIROCREW_E2E"])
        for env in (smoke, ui, i18n):
            self.assertEqual(env["KIROCREW_E2E_REQUIRE"], "1")
            self.assertEqual(env["KIROCREW_STRICT_ON_LOOP_PERSIST"], "1")

    def test_i18n_failure_is_not_hidden_by_both_other_lanes(self):
        proc = self.start(
            {
                "a": self.command("a"),
                "b": self.command("b"),
                "i18n": self.command("i18n", rc=7, gated=True),
            }
        )
        self.wait_file("a.done")
        self.wait_file("b.done")
        self.wait_file("i18n.started")
        self.assertIsNone(proc.poll(), "i18n was not awaited")
        (self.root / "release").touch()
        output, _ = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 7, output)
        self.assertTrue((self.root / "i18n.done").exists())

    def test_workflow_keeps_private_gate_and_original_commands(self):
        import yaml

        job = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["e2e"]
        self.assertEqual(job["timeout-minutes"], 25)
        steps = job["steps"]
        parallel = next(
            s for s in steps if s.get("run") == "exec python scripts/ci_e2e_parallel.py"
        )
        private = steps[steps.index(parallel) - 1]
        self.assertEqual(
            private["run"],
            "python -m pytest -q -n0 --no-cov --timeout=300 "
            "test/e2e/test_private_workflow_memory.py",
        )
        self.assertNotIn("if", parallel)
        self.assertNotIn("continue-on-error", parallel)
        self.assertNotIn("continue-on-error", private)
        self.assertEqual(private["env"]["KIROCREW_E2E_REQUIRE"], "1")
        self.assertEqual(parallel["env"]["KIROCREW_E2E_REQUIRE"], "1")
        self.assertEqual(parallel["env"]["KIROCREW_STRICT_ON_LOOP_PERSIST"], "1")
        module = runpy.run_path(str(HELPER))
        self.assertEqual(module["COMMANDS"]["smoke"], [sys.executable, "setup.py", "test_e2e"])
        self.assertEqual(
            module["COMMANDS"]["memory-ui"],
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-n0",
                "--no-cov",
                "-p",
                "no:cacheprovider",
                "--timeout=600",
                "test/e2e/test_memory_ui_evidence.py",
            ],
        )
        self.assertEqual(
            module["COMMANDS"]["i18n"], ["npm", "--prefix", "website", "run", "i18n:render"]
        )
        base = next(
            s
            for s in steps
            if s.get("name") == "Resolve base for the parallel i18n render-time gate"
        )
        self.assertLess(steps.index(base), steps.index(private))
        self.assertIn("resolve-i18n-base.sh", base["run"])
        self.assertIn('"$GITHUB_ENV"', base["run"])
        self.assertIn("I18N_BASE_REF", base["env"])
        self.assertNotIn("continue-on-error", base)
        self.assertNotIn("--no-vs-base", str(module["COMMANDS"]))
        self.assertEqual(module["MEMORY_UI_TIMEOUT"], 720)
        for step in steps:
            if step.get("name") in (
                "Upload member memory UI evidence",
                "Upload read-aloud recovery evidence",
            ):
                self.assertEqual(step["if"], "${{ always() }}")


if __name__ == "__main__":
    unittest.main()
