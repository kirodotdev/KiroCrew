"""Exercise the opt-in reporter with real pytest workers and fixture-owned children."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "ci_progress_test_helper", ROOT / "scripts/ci_pytest_progress.py"
)
PLUGIN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLUGIN)
CANARY = "PARAMETER_PRIVATE_CANARY"


class TestCIProgress(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ci-progress-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        self.logs = self.root / "records"

    def command(self, *extra, enabled=True):
        args = [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "-p",
            "xdist.plugin",
            "-p",
            "scripts.ci_pytest_progress",
            "--dist=loadgroup",
            "-q",
            "--tb=no",
        ]
        if enabled:
            args += ["--ci-progress-dir", str(self.logs)]
        return args + list(extra)

    def environment(self):
        env = dict(os.environ)
        # No inherited CLI options, plugins, application home, or checkout conftest.
        env.pop("PYTEST_ADDOPTS", None)
        env.pop("PYTEST_PLUGINS", None)
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = str(ROOT)
        env["TMPDIR"] = env["TMP"] = env["TEMP"] = str(self.root)
        return env

    def run_pytest(self, *extra, enabled=True):
        return subprocess.run(
            self.command(*extra, enabled=enabled),
            cwd=self.root,
            env=self.environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=45,
        )

    def records(self):
        return {
            p.name: [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]
            for p in self.logs.glob("*.jsonl")
        }

    def test_disabled_registers_nothing_and_does_no_io(self):
        config = SimpleNamespace(getoption=lambda _: None)
        with (
            patch.object(Path, "mkdir", side_effect=AssertionError("unexpected mkdir")),
            patch.object(Path, "open", side_effect=AssertionError("unexpected open")),
        ):
            PLUGIN.pytest_configure(config)
        (self.root / "test_small.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
        result = self.run_pytest("-n0", enabled=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.logs.exists())
        self.assertNotIn("CI_PROGRESS", result.stdout)

    def test_two_workers_phases_privacy_and_failure_exit(self):
        (self.root / "test_small.py").write_text(
            "import pytest\n"
            "class TestCases:\n"
            f"    @pytest.mark.parametrize('value', [1, 2], ids=['{CANARY}', '{CANARY}_two'])\n"
            "    def test_case(self, value, record_property):\n"
            f"        record_property('kirocrew_ci_progress', {{'private': '{CANARY}'}})\n"
            f"        print('{CANARY}_captured')\n"
            "        assert value == 1\n",
            encoding="utf-8",
        )
        result = self.run_pytest("-n2")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        logs = self.records()
        self.assertEqual(len(logs), 2)
        self.assertTrue(any(name.startswith("gw0-") for name in logs))
        self.assertTrue(any(name.startswith("gw1-") for name in logs))
        rows = [row for records in logs.values() for row in records]
        starts = [r for r in rows if r["event"] == "test_start"]
        self.assertEqual(len(starts), 2)
        self.assertEqual({r["test"] for r in starts}, {"TestCases::test_case"})
        self.assertEqual({r["case"] for r in starts}, {0, 1})
        self.assertEqual({r["module"] for r in starts}, {"test_small.py"})
        phases = [r for r in rows if r["event"] == "phase_end"]
        self.assertEqual(len(phases), 6)
        self.assertEqual({r["phase"] for r in phases}, {"setup", "call", "teardown"})
        self.assertTrue(any(r["outcome"] == "failed" for r in phases))
        for records in logs.values():
            self.assertEqual(records[0]["event"], "collection_start")
            self.assertEqual(
                next(r["selected"] for r in records if r["event"] == "collection_end"), 2
            )
            self.assertGreaterEqual(records[1]["elapsed"], 0)
        summaries = [
            line[line.index("CI_PROGRESS ") + 12 :]
            for line in result.stdout.splitlines()
            if "CI_PROGRESS " in line
        ]
        self.assertTrue(summaries)
        encoded = json.dumps(rows) + "".join(summaries) + "".join(logs)
        self.assertNotIn(CANARY, encoded)
        self.assertNotIn(str(self.root), encoded)
        self.assertLess(max(len(json.dumps(r)) for r in rows), 2048)

    def test_dynamic_node_names_never_supply_identity(self):
        (self.root / "conftest.py").write_text(
            "def pytest_collection_modifyitems(items):\n"
            "    for i, item in enumerate(items):\n"
            f"        item.name = item.originalname = '{CANARY}'\n"
            f"        item._nodeid = '{CANARY}/escape::' + str(i)\n",
            encoding="utf-8",
        )
        (self.root / "test_small.py").write_text("def test_original(): pass\n", encoding="utf-8")
        result = self.run_pytest("-n0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = next(iter(self.records().values()))
        self.assertNotIn(CANARY, json.dumps(rows))
        self.assertEqual(
            next(r for r in rows if r["event"] == "test_start")["test"], "test_original"
        )

    def test_cancel_preserves_flushed_events_without_session_finish(self):
        (self.root / "test_small.py").write_text(
            "import time\nfrom pathlib import Path\n"
            "def test_wait():\n"
            "    Path('ready').write_text('ready')\n"
            "    time.sleep(30)\n",
            encoding="utf-8",
        )
        proc = subprocess.Popen(
            self.command("-n0"),
            cwd=self.root,
            env=self.environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )
        try:
            deadline = time.monotonic() + 15
            while not (self.root / "ready").exists():
                self.assertIsNone(proc.poll())
                self.assertLess(time.monotonic(), deadline, "child never reached its fixture gate")
                time.sleep(0.01)
            rows = next(iter(self.records().values()))
            self.assertTrue(any(r["event"] == "test_start" for r in rows))
            self.assertTrue(any(r.get("phase") == "setup" for r in rows))
            proc.terminate()
            output, _ = proc.communicate(timeout=10)
            self.assertNotEqual(proc.returncode, 0)
            rows = next(iter(self.records().values()))
            self.assertFalse(any(r["event"] == "session_end" for r in rows))
            self.assertIn("CI_PROGRESS", output)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=10)

    def test_split_records_only_selected_cases(self):
        (self.root / "test_split.py").write_text(
            "import pytest\n@pytest.mark.parametrize('n', range(8))\n"
            "def test_case(n): assert n >= 0\n",
            encoding="utf-8",
        )
        result = self.run_pytest("-n2", "-p", "pytest_split.plugin", "--splits=4", "--group=3")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        logs = self.records()
        for rows in logs.values():
            self.assertEqual(next(r["selected"] for r in rows if r["event"] == "collection_end"), 2)
        starts = [r for rows in logs.values() for r in rows if r["event"] == "test_start"]
        self.assertEqual(len(starts), 2)
        self.assertEqual({r["case"] for r in starts}, {0, 1})

    def test_worker_exit_stays_failed(self):
        (self.root / "test_crash.py").write_text(
            "import os\ndef test_exit(): os._exit(7)\ndef test_ok(): pass\n",
            encoding="utf-8",
        )
        result = self.run_pytest("-n2", "--max-worker-restart=0")
        self.assertNotEqual(result.returncode, 0)
        logs = self.records()
        self.assertTrue(
            any(
                any(r["event"] == "test_start" for r in rows)
                and not any(r["event"] == "session_end" for r in rows)
                for rows in logs.values()
            )
        )

    def test_nested_runs_share_directory_without_overwriting(self):
        child = self.root / "nested"
        child.mkdir()
        (child / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        (child / "test_child.py").write_text("def test_child(): pass\n", encoding="utf-8")
        command = self.command("-n0", "test_child.py")
        (self.root / "test_outer.py").write_text(
            "import subprocess\n"
            "def test_nested():\n"
            f"    result = subprocess.run({command!r}, cwd={str(child)!r}, timeout=20, "
            "capture_output=True)\n"
            "    assert result.returncode == 0\n",
            encoding="utf-8",
        )
        result = self.run_pytest("-n0", "test_outer.py")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        logs = self.records()
        self.assertEqual(len(logs), 2)
        self.assertEqual(len({name.split("-")[1] for name in logs}), 2)
        self.assertTrue(all(rows[-1]["event"] == "session_end" for rows in logs.values()))

    def recorder(self, directory=None):
        writer = SimpleNamespace(write_line=lambda _: None, _tw=SimpleNamespace(flush=lambda: None))
        config = SimpleNamespace(
            rootpath=self.root,
            option=SimpleNamespace(numprocesses=0),
            pluginmanager=SimpleNamespace(getplugin=lambda _: writer),
        )
        recorder = PLUGIN.Progress(config, directory or self.logs)
        self.addCleanup(recorder.close)
        return recorder

    def test_same_process_runs_have_unique_files(self):
        first, second = self.recorder(), self.recorder()
        self.assertNotEqual(first.run, second.run)
        self.assertEqual(len(list(self.logs.glob("*.jsonl"))), 2)

    def test_event_loop_uses_open_stream_and_console_is_rate_limited(self):
        recorder = self.recorder()
        report = SimpleNamespace(
            when="call",
            duration=0.1,
            outcome="passed",
            kirocrew_ci_progress={"case": 0, "test": "test_ok"},
        )
        with (
            patch.object(Path, "mkdir", side_effect=AssertionError("per-event mkdir")),
            patch.object(Path, "open", side_effect=AssertionError("per-event open")),
            patch.object(Path, "stat", side_effect=AssertionError("per-event stat")),
            patch.object(os, "fsync", side_effect=AssertionError("per-event fsync")),
            patch.object(os, "chmod", side_effect=AssertionError("per-event chmod")),
            patch.object(os, "replace", side_effect=AssertionError("per-event replace")),
            patch.object(PLUGIN.time, "perf_counter", return_value=recorder.started),
            patch.object(recorder, "notice") as notice,
        ):
            for _ in range(1000):
                recorder.pytest_runtest_logreport(report)
            self.assertEqual(notice.call_count, 1)
        self.assertEqual(len(next(iter(self.records().values()))), 1000)

    def test_io_failure_does_not_replace_test_outcome(self):
        blocked = self.root / "not_a_directory"
        blocked.write_text("occupied", encoding="utf-8")
        recorder = self.recorder(blocked)
        self.assertIsNone(recorder.stream)
        recorder.emit("session_end", exitstatus=1)
        # Real write errors are OSError; no exception body goes into diagnostics.
        with patch.object(recorder, "stream") as failing:
            failing.write.side_effect = OSError(CANARY)
            recorder.emit("phase_end", outcome="failed")
        self.assertIsNone(recorder.stream)

    def test_windows_wiring_keeps_acceptance_and_other_jobs_unchanged(self):
        import yaml

        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
        job = workflow["jobs"]["backend-test-windows"]
        self.assertEqual(job["timeout-minutes"], 40)
        self.assertEqual(job["strategy"]["matrix"]["group"], [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(job["env"]["SHARD_COUNT"], 8)
        command = next(s["run"] for s in job["steps"] if s.get("name", "").startswith("Run tests"))
        for flag in [
            "-n auto",
            "--timeout=180",
            "--no-cov",
            "--max-worker-restart=0",
            '--splits "$SHARD_COUNT"',
        ]:
            self.assertIn(flag, command)
        self.assertIn("-p scripts.ci_pytest_progress", command)
        self.assertTrue(
            all(
                "ci_pytest_progress" not in json.dumps(value)
                for name, value in workflow["jobs"].items()
                if name != "backend-test-windows"
            )
        )


if __name__ == "__main__":
    unittest.main()
