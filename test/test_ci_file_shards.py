"""File shards cover the original suite once without importing sibling files."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
import yaml

from scripts import ci_file_shard_pins
from scripts.ci_file_shards import file_shard, hash_shard, read_pins

ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_SUBPROCESS_TIMEOUT_SECONDS = 45
# Lost-run ceiling for the Windows `--surface backend` scan (testing-conventions
# class 6). The slowest passing call in a windows-pytest-progress-7 artifact took
# 42.251775s (run 39911553168); 10x is 422.5s, rounded up to 425s. Fifteen
# consecutive green main runs (41234261776..41278128347) measured 18.2s-25.2s.
_WINDOWS_BACKEND_SCOPE_TIMEOUT_SECONDS = 425
# The same ceiling off Windows. The slowest of 117 passing calls in
# linux-pytest-progress-8 artifacts (runs 42132423515..42955291680) took
# 45.704918s (run 42938841346); 10x is 457.0s, rounded up to 460s.
_LINUX_BACKEND_SCOPE_TIMEOUT_SECONDS = 460
# At most half the module timeout, so an expired scan fails by name instead of
# pytest-timeout killing the xdist worker (Windows has no SIGALRM).
pytestmark = pytest.mark.timeout(
    2 * max(_WINDOWS_BACKEND_SCOPE_TIMEOUT_SECONDS, _LINUX_BACKEND_SCOPE_TIMEOUT_SECONDS)
)


def _backend_scope_timeout_seconds(*, windows: bool) -> int:
    """Bound the scan from the shard time measured on Windows or Linux runners."""
    return (
        _WINDOWS_BACKEND_SCOPE_TIMEOUT_SECONDS if windows else _LINUX_BACKEND_SCOPE_TIMEOUT_SECONDS
    )


def _tail(output: str | bytes | None, limit: int = 2000) -> str:
    """Last ``limit`` characters a timed-out child wrote, for the failure message."""
    if output is None:
        return ""
    if isinstance(output, bytes):
        output = output.decode("utf-8", "replace")
    return output[-limit:]


class TestFileShards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="file-shards-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.write("pytest.ini", "[pytest]\ntestpaths = test apps\n")
        (self.root / "test").mkdir()
        (self.root / "apps").mkdir()
        self.write(
            "conftest.py",
            "import json\n"
            "def pytest_collection_finish(session):\n"
            "    worker = getattr(session.config, 'workerinput', {}).get('workerid', 'main')\n"
            "    path = session.config.rootpath / ('items-' + worker + '.json')\n"
            "    path.write_text(json.dumps([i.nodeid for i in session.items]), encoding='utf-8')\n",
        )

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def run_python(self, *extra, timeout=_DEFAULT_SUBPROCESS_TIMEOUT_SECONDS):
        env = dict(os.environ)
        env.pop("PYTEST_ADDOPTS", None)
        env.pop("PYTEST_PLUGINS", None)
        # Nested coverage runs own their data, never the outer pytest's file.
        env.pop("COV_CORE_SOURCE", None)
        env.pop("COV_CORE_DATAFILE", None)
        env.pop("COVERAGE_PROCESS_START", None)
        env["COVERAGE_FILE"] = str(self.root / ".coverage")
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = str(ROOT)
        env["TMPDIR"] = env["TMP"] = env["TEMP"] = str(self.root)
        try:
            return subprocess.run(
                [sys.executable, "-B", *extra],
                cwd=self.root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            self.fail(
                f"{extra} still running when its {exc.timeout}s ceiling expired; "
                f"stdout tail: {_tail(exc.stdout)!r}; stderr tail: {_tail(exc.stderr)!r}"
            )

    def run_pytest(self, *extra):
        return self.run_python("-m", "pytest", "-p", "scripts.ci_file_shards", "-q", *extra)

    def items(self, worker="main"):
        return json.loads((self.root / f"items-{worker}.json").read_text(encoding="utf-8"))

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_covered_sources(self, data, filenames):
        recorded = {}
        for key in data.measured_files():
            normalized = key.replace("\\", "/")
            self.assertNotIn(normalized, recorded, "duplicate source identity")
            recorded[normalized] = key
        for filename in filenames:
            self.assertIn(filename, recorded)
            self.assertEqual(sorted(data.lines(recorded[filename])), [1, 2, 3, 4])
        self.assertFalse(any(name.startswith("sage_lib/") for name in recorded))

    def seed_suite(self):
        for index in range(18):
            self.write(
                f"{'test/nested' if index % 2 else 'apps/demo'}/test_case_{index}.py",
                "import pytest\n"
                "@pytest.mark.parametrize('value', [0, 1, 2])\n"
                "def test_case(value): assert value >= 0\n",
            )
        # Ignored files would fail at import if discovery or excludes were lost.
        self.write("test/conftest.py", "collect_ignore = ['test_ignored.py']\n")
        for name in ("test/test_ignored.py", "test/data/test_bad.py", "apps/helper.py"):
            self.write(name, "raise RuntimeError('must not import fixture data')\n")
        self.write("test/data/conftest.py", "collect_ignore_glob = ['test_*.py']\n")

    def test_union_matches_unsharded_items_including_parameters_and_ignores(self):
        self.seed_suite()
        self.assert_ok(self.run_pytest("--collect-only"))
        baseline = self.items()
        self.assertEqual(len(baseline), 54)
        all_items = []
        for index in range(1, 4):
            self.assert_ok(self.run_pytest("--file-shards=3", f"--file-shard={index}"))
            shard_items = self.items()
            self.assertTrue(shard_items)
            all_items.extend(shard_items)
        self.assertCountEqual(all_items, baseline)
        self.assertEqual(len(all_items), len(set(all_items)))

    def test_eight_shards_preserve_files_items_parameters_and_ignores(self):
        self.seed_suite()
        # Guarantee an executable file per bucket without relying on hash luck.
        for owner in range(1, 9):
            name = next(
                f"test/test_owner_{i}.py"
                for i in range(1000)
                if file_shard(self.root / f"test/test_owner_{i}.py", self.root, 8) == owner
            )
            self.write(name, "def test_owned(): pass\n")
        self.assert_ok(self.run_pytest("--collect-only"))
        baseline = self.items()
        self.assertEqual(len(baseline), 62)
        all_items = []
        all_files = []
        for shard in range(1, 9):
            self.assert_ok(self.run_pytest("--file-shards=8", f"--file-shard={shard}"))
            items = self.items()
            self.assertTrue(items)
            all_items.extend(items)
            all_files.extend({item.split("::")[0] for item in items})
        self.assertCountEqual(all_items, baseline)
        self.assertEqual(len(all_items), len(set(all_items)))
        self.assertCountEqual(all_files, {item.split("::")[0] for item in baseline})
        self.assertEqual(len(all_files), 26)

    def write_pins(self, name, count, pins):
        lines = [f"# shards: {count}"] + [f"{owner} {path}" for path, owner in pins.items()]
        return self.write(name, "\n".join(lines) + "\n")

    def test_pinned_union_matches_unsharded_items_and_moves_only_pinned_files(self):
        self.seed_suite()
        self.write("test/a space/test_spaced.py", "def test_one(): pass\n")
        self.assert_ok(self.run_pytest("--collect-only"))
        baseline = self.items()
        self.assertEqual(len(baseline), 55)
        files = sorted({item.split("::")[0] for item in baseline})
        hashed = {name: file_shard(self.root / name, self.root, 3) for name in files}
        # Move every other file off its hash shard, the spaced path included. A
        # stale pin (a deleted file) and the unpinned files must not matter.
        pins = {name: hashed[name] % 3 + 1 for name in files[::2]}
        pins["test/a space/test_spaced.py"] = hashed["test/a space/test_spaced.py"] % 3 + 1
        pins["test/test_deleted.py"] = 1
        pin_file = self.write_pins("pins.txt", 3, pins)
        all_items = []
        for index in range(1, 4):
            self.assert_ok(
                self.run_pytest(
                    "--file-shards=3", f"--file-shard={index}", f"--file-shard-pins={pin_file}"
                )
            )
            shard_items = self.items()
            for item in shard_items:
                name = item.split("::")[0]
                self.assertEqual(pins.get(name, hashed[name]), index, item)
            all_items.extend(shard_items)
        self.assertCountEqual(all_items, baseline)
        self.assertEqual(len(all_items), len(set(all_items)))

    def test_pinned_xdist_workers_collect_identical_shards(self):
        for index in range(6):
            self.write(f"test/test_x_{index}.py", "def test_a(): pass\ndef test_b(): pass\n")
        pins = {}
        for index in range(0, 6, 2):
            name = f"test/test_x_{index}.py"
            pins[name] = file_shard(self.root / name, self.root, 2) % 2 + 1
        pin_file = self.write_pins("pins.txt", 2, pins)
        collected = []
        for index in (1, 2):
            self.assert_ok(
                self.run_pytest(
                    "-p",
                    "xdist.plugin",
                    "-n2",
                    "--file-shards=2",
                    f"--file-shard={index}",
                    f"--file-shard-pins={pin_file}",
                )
            )
            self.assertEqual(self.items("gw0"), self.items("gw1"))
            collected.extend(self.items("gw0"))
        self.assertEqual(len(collected), 12)
        self.assertEqual(len(set(collected)), 12)

    def test_malformed_pin_files_fail_instead_of_guessing_an_owner(self):
        self.write("test/test_ok.py", "def test_ok(): pass\n")
        cases = {
            "count mismatch": self.write_pins("count.txt", 3, {"test/test_ok.py": 1}),
            "no header": self.write("no-header.txt", "1 test/test_ok.py\n"),
            "bad header": self.write("bad-header.txt", "# shards: eight\n"),
            "owner out of range": self.write("range.txt", "# shards: 2\n3 test/test_ok.py\n"),
            "owner zero": self.write("zero.txt", "# shards: 2\n0 test/test_ok.py\n"),
            "no path": self.write("nopath.txt", "# shards: 2\n1\n"),
            "pinned twice": self.write(
                "twice.txt", "# shards: 2\n1 test/test_ok.py\n2 test/test_ok.py\n"
            ),
            "missing file": self.root / "absent.txt",
        }
        for label, path in cases.items():
            with self.subTest(label):
                result = self.run_pytest(
                    "--file-shards=2", "--file-shard=1", f"--file-shard-pins={path}"
                )
                self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        good = self.write_pins("good.txt", 2, {"test/test_ok.py": 1})
        result = self.run_pytest(f"--file-shard-pins={good}")
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)

    def test_committed_pin_file_matches_the_workflow_shard_count(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
        for job, name in (
            ("backend-test", "ci_file_shard_pins.txt"),
            ("backend-test-windows", "ci_file_shard_pins_windows.txt"),
        ):
            count = workflow["jobs"][job]["env"]["SHARD_COUNT"]
            pins = read_pins(ROOT / "scripts" / name, count)
            # The pins balance the shards only if every shard holds some of them.
            self.assertEqual(set(pins.values()), set(range(1, count + 1)), name)

    def test_pin_generator_reads_only_the_named_platforms_artifacts(self):
        run = self.root / "runs" / "a"
        for platform, module in (("linux", "test/test_lin.py"), ("windows", "test/test_win.py")):
            shard_dir = run / f"{platform}-pytest-progress-1"
            shard_dir.mkdir(parents=True)
            record = {"event": "phase_end", "module": module, "phase": "call", "elapsed": 5.0}
            (shard_dir / "gw0.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
            self.write(module, "def test_ok(): pass\n")
        out = self.root / "pins.txt"
        argv = [str(run), "--platform=windows", "--shards=2", "--pins=5"]
        self.assertEqual(ci_file_shard_pins.main(argv + [f"--out={out}", f"--root={self.root}"]), 0)
        self.assertEqual(set(read_pins(out, 2)), {"test/test_win.py"})

    def test_pin_generator_balances_measured_seconds_and_is_deterministic(self):
        runs = self.root / "runs"
        heavy = {f"test/test_heavy_{i}.py": 100.0 - i for i in range(10)}
        light = {f"test/test_light_{i}.py": 1.0 for i in range(40)}
        weights = {**heavy, **light}
        for run, factor in (("a", 1.0), ("b", 2.0), ("c", 1.0)):
            shard_dir = runs / run / "linux-pytest-progress-1"
            shard_dir.mkdir(parents=True)
            records = [{"event": "collection_end", "elapsed": 9.0}]
            for name, seconds in weights.items():
                for phase in ("setup", "call", "teardown"):
                    records.append(
                        {
                            "event": "phase_end",
                            "module": name,
                            "phase": phase,
                            "elapsed": seconds * factor / 3,
                        }
                    )
            # A record with no module (an unattributable test) is not a file.
            records.append({"event": "phase_end", "test": "dynamic", "elapsed": 99.0})
            (shard_dir / "gw0.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
            )
        for name in weights:
            self.write(name, "def test_ok(): pass\n")
        # Measured but since deleted: never pinned, however heavy it was.
        records = [
            {"event": "phase_end", "module": "test/test_gone.py", "phase": "call", "elapsed": 500.0}
        ]
        for run in "abc":
            with (runs / run / "linux-pytest-progress-1" / "gw0.jsonl").open("a") as stream:
                stream.write("".join(json.dumps(r) + "\n" for r in records))
        out = self.root / "pins.txt"
        argv = [str(runs / run) for run in "abc"]
        argv += ["--shards=4", "--pins=10", f"--out={out}", f"--root={self.root}"]
        self.assertEqual(ci_file_shard_pins.main(argv), 0)
        first = out.read_text(encoding="utf-8")
        self.assertEqual(ci_file_shard_pins.main(argv), 0)
        self.assertEqual(out.read_text(encoding="utf-8"), first)
        pins = read_pins(out, 4)
        self.assertEqual(set(pins), set(heavy))
        self.assertNotIn("test/test_gone.py", pins)
        pinned = [0.0] * 4
        hashed = [0.0] * 4
        for name, seconds in weights.items():
            pinned[pins.get(name, hash_shard(name, 4)) - 1] += seconds
            hashed[hash_shard(name, 4) - 1] += seconds
        self.assertLess(max(pinned) - min(pinned), max(hashed) - min(hashed))
        self.assertLessEqual(max(pinned) - min(pinned), 100.0)

    def test_pin_generator_every_file_plans_each_measured_file_and_evens_the_rest(self):
        runs = self.root / "runs"
        # The names are chosen by hash: every light file's hash owner is shard 1,
        # so a plan that stops at the two heavy files cannot even the shards.
        light = [
            n for n in (f"test/test_light_{i}.py" for i in range(200)) if hash_shard(n, 2) == 1
        ]
        light = light[:30]
        weights = {"test/test_heavy_0.py": 100.0, "test/test_heavy_1.py": 100.0}
        weights.update(dict.fromkeys(light, 10.0))
        # Measured in one run of three: its median share is zero, so it has no weight.
        unweighted = "test/test_new.py"
        for run in "abc":
            shard_dir = runs / run / "linux-pytest-progress-1"
            shard_dir.mkdir(parents=True)
            seconds = {**weights, unweighted: 50.0} if run == "c" else weights
            records = [
                {"event": "phase_end", "module": name, "phase": "call", "elapsed": value}
                for name, value in seconds.items()
            ]
            (shard_dir / "gw0.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
            )
        for name in (*weights, unweighted):
            self.write(name, "def test_ok(): pass\n")
        out = self.root / "pins.txt"
        argv = [str(runs / run) for run in "abc"]
        argv += ["--shards=2", "--pins=2", f"--out={out}", f"--root={self.root}"]

        def spread() -> float:
            pins = read_pins(out, 2)
            load = [0.0, 0.0]
            for name, seconds in weights.items():
                load[pins.get(name, hash_shard(name, 2)) - 1] += seconds
            return abs(load[0] - load[1])

        self.assertEqual(ci_file_shard_pins.main(argv), 0)
        self.assertEqual(set(read_pins(out, 2)), {"test/test_heavy_0.py", "test/test_heavy_1.py"})
        self.assertEqual(spread(), 100.0)
        self.assertEqual(ci_file_shard_pins.main(argv + ["--every-file"]), 0)
        # Every weighted file is planned; the unweighted one keeps its hash shard.
        self.assertEqual(set(read_pins(out, 2)), set(weights))
        self.assertEqual(spread(), 0.0)

    def test_reduced_backend_scope_timeout_accounts_for_windows(self):
        self.assertEqual(
            _backend_scope_timeout_seconds(windows=False), _LINUX_BACKEND_SCOPE_TIMEOUT_SECONDS
        )
        self.assertEqual(
            _backend_scope_timeout_seconds(windows=True), _WINDOWS_BACKEND_SCOPE_TIMEOUT_SECONDS
        )

    def test_reduced_backend_scope_populates_every_ci_shard(self):
        timeout = _backend_scope_timeout_seconds(windows=os.name == "nt")
        result = self.run_python(
            str(ROOT / "scripts/ci-surface-tests.py"), "--surface", "backend", timeout=timeout
        )
        self.assert_ok(result)
        files = result.stdout.splitlines()
        self.assertTrue(files)
        self.assertEqual(len(files), len(set(files)))
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
        count = workflow["jobs"]["backend-test"]["env"]["SHARD_COUNT"]
        buckets = {shard: [] for shard in range(1, count + 1)}
        for name in files:
            self.assertTrue((ROOT / name).is_file(), name)
            buckets[file_shard(ROOT / name, ROOT, count)].append(name)
        for shard, bucket in buckets.items():
            self.assertTrue(bucket, f"reduced backend shard {shard}/{count} is empty")
        self.assertCountEqual([name for bucket in buckets.values() for name in bucket], files)

    def test_unassigned_file_is_not_imported_even_as_an_explicit_target(self):
        good = self.write("test/test_good.py", "def test_ok(): pass\n")
        owner = file_shard(good, self.root, 2)
        bad = next(
            self.root / f"test/test_bad_{i}.py"
            for i in range(100)
            if file_shard(self.root / f"test/test_bad_{i}.py", self.root, 2) != owner
        )
        self.write(bad.relative_to(self.root), "raise RuntimeError('poison import')\n")
        args = ("--file-shards=2", f"--file-shard={owner}")
        self.assert_ok(self.run_pytest(*args))
        self.assert_ok(self.run_pytest(*args, str(good), str(bad)))
        # Negative control: removing the shard filter exposes the import failure.
        result = self.run_pytest(str(good), str(bad))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("poison import", result.stdout)

    def test_xdist_workers_collect_identical_shards_and_keep_loadgroup(self):
        self.seed_suite()
        result = self.run_pytest(
            "-p", "xdist.plugin", "-n2", "--dist=loadgroup", "--file-shards=3", "--file-shard=2"
        )
        self.assert_ok(result)
        self.assertEqual(self.items("gw0"), self.items("gw1"))
        self.assertTrue(self.items("gw0"))
        for item in self.items("gw0"):
            self.assertEqual(file_shard(self.root / item.split("::")[0], self.root, 3), 2)

    def check_import_time_metric_guard(self, *worker_args):
        import ast

        source = (ROOT / "conftest.py").read_text(encoding="utf-8")
        names = {
            "_metrics_provider_module",
            "_pin_telemetry_off_for_the_process",
            "pytest_make_collect_report",
        }
        hooks = "\n\n".join(
            ast.unparse(node)
            for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef) and node.name in names
        )
        # Execute the actual root guard, without unrelated host-floor fixtures.
        self.write(
            "conftest.py",
            "import contextlib, json, os, sys, pytest\n"
            f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
            "IMPORT_TIME_METRIC_EMITTERS = []\n"
            + hooks
            + "\n_pin_telemetry_off_for_the_process()\n"
            "def pytest_collection_finish(session):\n"
            "    from kiro_crew.metrics import provider\n"
            "    worker = getattr(session.config, 'workerinput', {}).get('workerid', 'main')\n"
            "    data = [IMPORT_TIME_METRIC_EMITTERS, provider._ever_built,\n"
            "            [i.nodeid for i in session.items]]\n"
            "    (session.config.rootpath / (worker + '.json')).write_text(\n"
            "        json.dumps(data), encoding='utf-8')\n",
        )
        guard = self.root / "test/test_host_isolation_floor.py"
        owner = file_shard(guard, self.root, 4)
        shard = owner % 4 + 1
        self.write(guard.relative_to(self.root), "raise AssertionError('guard shard imported')\n")
        targets = [
            self.root / f"test/test_emitter_{i}.py"
            for i in range(100)
            if file_shard(self.root / f"test/test_emitter_{i}.py", self.root, 4) == shard
        ][:2]
        self.assertEqual(len(targets), 2)
        clean = (
            "from kiro_crew.metrics import provider\n"
            "def test_runtime_emission():\n"
            "    assert not provider._ever_built\n"
            "    try:\n"
            "        provider.get_recorder()\n"
            "        assert provider._ever_built\n"
            "    finally:\n"
            "        provider.reset_for_testing()\n"
            "    assert not provider._ever_built\n"
        )
        args = (*worker_args, "--file-shards=4", f"--file-shard={shard}")
        for target in targets:
            self.write(target.relative_to(self.root), clean)
        self.assert_ok(self.run_pytest(*args))
        for target in targets:
            self.write(target.relative_to(self.root), clean + "provider.get_recorder()\n")
        result = self.run_pytest(*args)
        self.assertEqual(result.returncode, 1 if worker_args else 2, result.stdout + result.stderr)
        self.assertIn("Import-time metric emission", result.stdout)
        self.assertNotIn("INTERNALERROR", result.stdout)
        expected = sorted(t.relative_to(self.root).as_posix() for t in targets)
        for worker in (("gw0", "gw1") if worker_args else ("main",)):
            emitters, built, items = json.loads(
                (self.root / f"{worker}.json").read_text(encoding="utf-8")
            )
            self.assertEqual(sorted(emitters), expected)
            self.assertFalse(built, "each import must reset the recorder before the next module")
            self.assertFalse(any("test_host_isolation_floor.py" in item for item in items))

    def test_import_time_metric_fails_without_guard_file(self):
        self.check_import_time_metric_guard()

    def test_import_time_metric_fails_xdist_controller_without_guard_file(self):
        self.check_import_time_metric_guard("-p", "xdist.plugin", "-n2", "--dist=loadgroup")

    def test_reduced_scope_is_partitioned_without_expanding_to_other_roots(self):
        files = [self.write(f"test/test_{i}.py", "def test_ok(): pass\n") for i in range(12)]
        self.write("apps/test_outside.py", "raise RuntimeError('outside reduced scope')\n")
        baseline = [f"test/test_{i}.py::test_ok" for i in range(12)]
        actual = []
        for index in (1, 2):
            self.assert_ok(
                self.run_pytest("--file-shards=2", f"--file-shard={index}", *map(str, files))
            )
            actual.extend(self.items())
        self.assertCountEqual(actual, baseline)

    def test_targets_file_collects_only_listed_files_without_putting_them_on_argv(self):
        self.write("test/conftest.py", "import pytest\n@pytest.fixture\ndef seeded(): return 1\n")
        self.write("test/test_listed.py", "def test_ok(seeded): assert seeded == 1\n")
        self.write("test/nested/test_deep.py", "def test_ok(): pass\n")
        # Neither the sibling nor a directory holding no target may be imported.
        self.write("test/test_sibling.py", "raise RuntimeError('sibling imported')\n")
        self.write("apps/demo/conftest.py", "raise RuntimeError('untargeted directory walked')\n")
        targets = self.write("targets.txt", "\ntest/test_listed.py\ntest/nested/test_deep.py\n")
        args = ("--file-shards=1", "--file-shard=1", f"--file-shard-targets={targets}")
        self.assert_ok(self.run_pytest(*args))
        self.assertCountEqual(
            self.items(), ["test/test_listed.py::test_ok", "test/nested/test_deep.py::test_ok"]
        )
        # A listed path that is not a file fails, as it did as an argument.
        missing = self.write("missing.txt", "test/test_listed.py\ntest/test_gone.py\n")
        result = self.run_pytest(*args[:2], f"--file-shard-targets={missing}")
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        # Without sharding the targets would silently widen to the whole suite.
        result = self.run_pytest(f"--file-shard-targets={targets}")
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)

    def test_custom_filename_pattern_norecursedirs_and_cli_ignore_survive(self):
        self.write(
            "pytest.ini",
            "[pytest]\ntestpaths = test apps\npython_files = check_*.py\nnorecursedirs = data\n",
        )
        self.write("test/check_ok.py", "def test_ok(): pass\n")
        for name in ("test/test_bad.py", "test/data/check_bad.py", "apps/check_bad.py"):
            self.write(name, "raise RuntimeError('excluded')\n")
        self.assert_ok(
            self.run_pytest("--file-shards=1", "--file-shard=1", "--ignore=apps/check_bad.py")
        )
        self.assertEqual(self.items(), ["test/check_ok.py::test_ok"])

    def test_disabled_plugin_preserves_explicit_node_ids_and_spaced_paths(self):
        target = self.write(
            "test/a space/test_ok.py", "def test_one(): pass\ndef test_two(): assert False\n"
        )
        self.assert_ok(self.run_pytest(str(target) + "::test_one"))
        self.assertEqual(self.items(), ["test/a space/test_ok.py::test_one"])
        self.assert_ok(
            self.run_pytest("--file-shards=1", "--file-shard=1", str(target) + "::test_one")
        )

    def test_invalid_and_empty_shards_fail_instead_of_running_the_full_suite(self):
        self.write("test/test_ok.py", "def test_ok(): pass\n")
        for args in (
            ("--file-shards=0", "--file-shard=1"),
            ("--file-shards=2", "--file-shard=0"),
            ("--file-shards=2", "--file-shard=3"),
            ("--file-shards=2",),
            ("--file-shard=1",),
        ):
            with self.subTest(args=args):
                self.assertEqual(self.run_pytest(*args).returncode, 4)
        owner = file_shard(self.root / "test/test_ok.py", self.root, 2)
        result = self.run_pytest("--file-shards=2", f"--file-shard={3 - owner}")
        self.assertEqual(result.returncode, 5, result.stdout + result.stderr)
        self.assertEqual(self.items(), [])

    def test_owned_import_error_and_test_failure_still_fail(self):
        self.write("test/test_bad.py", "raise RuntimeError('owned import failed')\n")
        self.assertEqual(self.run_pytest("--file-shards=1", "--file-shard=1").returncode, 2)
        self.write("test/test_bad.py", "def test_bad(): assert False\n")
        self.assertEqual(self.run_pytest("--file-shards=1", "--file-shard=1").returncode, 1)

    def test_cannot_apply_item_splitting_on_top_of_file_splitting(self):
        result = self.run_pytest(
            "-p",
            "pytest_split.plugin",
            "--splits=2",
            "--group=1",
            "--file-shards=2",
            "--file-shard=1",
        )
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn("cannot be combined", result.stderr)

    def test_coverage_union_matches_the_unsharded_run(self):
        from coverage import CoverageData

        self.write(
            "logic.py",
            "def choose(value):\n"
            "    if value % 2:\n"
            "        return 'odd'\n"
            "    return 'even'\n",
        )
        self.write("pytest.ini", "[pytest]\ntestpaths = test apps\npythonpath = .\n")
        for i in range(12):
            expected = "odd" if i % 2 else "even"
            self.write(
                f"test/test_cov_{i}.py",
                f"from logic import choose\ndef test_value(): assert choose({i}) == {expected!r}\n",
            )
        args = ("-p", "pytest_cov.plugin", "--cov=logic", "--cov-branch", "--cov-report=")
        self.assert_ok(self.run_pytest(*args))
        (self.root / ".coverage").rename(self.root / "baseline.coverage")
        baseline = CoverageData(basename=str(self.root / "baseline.coverage"))
        baseline.read()
        staged = self.root / "shards"
        staged.mkdir()
        for index in (1, 2):
            self.assert_ok(self.run_pytest(*args, "--file-shards=2", f"--file-shard={index}"))
            (self.root / ".coverage").rename(staged / f".coverage.shard{index}")
        self.assertEqual(len(list(staged.iterdir())), 2)
        self.assert_ok(self.run_python("-m", "coverage", "combine", str(staged)))
        combined = CoverageData(basename=str(self.root / ".coverage"))
        combined.read()
        self.assertEqual(combined.measured_files(), baseline.measured_files())
        self.assertTrue(baseline.measured_files())
        for filename in baseline.measured_files():
            self.assertEqual(
                sorted(combined.arcs(filename)), sorted(baseline.arcs(filename)), filename
            )

    def test_source_directory_measures_exec_variants_without_import_priming(self):
        self.write("src/kiro_crew/__init__.py", "")
        self.write(
            "src/kiro_crew/builder.py",
            "def choose(value):\n    if value:\n        return 'yes'\n    return 'no'\n",
        )
        probe = (
            "import importlib, json, sys, types\n"
            "from pathlib import Path\n"
            "from coverage import Coverage\n"
            "sys.path.insert(0, str(Path('src').resolve()))\n"
            "path = Path('src/kiro_crew/builder.py').resolve()\n"
            "cov = Coverage(source=[sys.argv[1]], branch=True, config_file=False)\n"
            "cov.start()\n"
            "if sys.argv[2] == 'prime':\n"
            "    importlib.import_module('kiro_crew.builder')\n"
            "mod = types.ModuleType('smc_build_v1')\n"
            "mod.__file__ = str(path)\n"
            "exec(compile(path.read_text(encoding='utf-8'), str(path), 'exec'), mod.__dict__)\n"
            "assert [mod.choose(True), mod.choose(False)] == ['yes', 'no']\n"
            "cov.stop()\n"
            "print(json.dumps(sorted(cov.get_data().lines(str(path)) or [])))\n"
        )
        for source, priming, expected in (
            ("kiro_crew", "none", []),
            ("kiro_crew", "prime", [1, 2, 3, 4]),
            ("src/kiro_crew", "none", [1, 2, 3, 4]),
            ("src/kiro_crew", "prime", [1, 2, 3, 4]),
        ):
            with self.subTest(source=source, priming=priming):
                result = self.run_python("-c", probe, source, priming)
                self.assert_ok(result)
                self.assertEqual(json.loads(result.stdout), expected)

    def test_ci_coverage_union_includes_exec_variants_and_sage_aliases(self):
        from coverage import CoverageData

        # Read CI's selectors so reverting the workflow fails on missing exec lines.
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
        command = next(
            step["run"]
            for step in workflow["jobs"]["backend-test"]["steps"]
            if step.get("name", "").startswith("Run tests")
        )
        selectors = tuple(word for word in command.split() if word.startswith("--cov="))
        self.assertTrue(selectors)
        self.write("src/kiro_crew/__init__.py", "")
        sage = "src/kiro_crew/apps/builtins/code_review_sage/sage_lib"
        for package in ("apps", "apps/builtins", "apps/builtins/code_review_sage"):
            self.write(f"src/kiro_crew/{package}/__init__.py", "")
        self.write(f"{sage}/__init__.py", "")
        code = "def choose(value):\n    if value:\n        return 'yes'\n    return 'no'\n"
        builder_name = "src/kiro_crew/apps/builtins/aws_control/crew/packaging/build.py"
        builder = self.write(builder_name, code)
        self.write(f"{sage}/logic.py", code)
        fixture_name = "src/kiro_crew/apps/builtins/code_review_sage/tests/fixtures.py"
        self.write("src/kiro_crew/apps/builtins/code_review_sage/tests/__init__.py", "")
        self.write(fixture_name, code)
        outside_name = "src/kiro_crew/builtin_skills/standalone/probe.py"
        outside = self.write(outside_name, "VALUE = 7\n")
        self.write(
            "pytest.ini",
            "[pytest]\ntestpaths = test apps\n"
            "pythonpath = src src/kiro_crew/apps/builtins/code_review_sage\n",
        )
        # Keep the real path aliases, relative-file setting and every omit pattern.
        self.write("setup.cfg", (ROOT / "setup.cfg").read_text(encoding="utf-8"))
        targets = []
        for shard in range(1, 5):
            name = next(
                f"test/test_exec_{i}.py"
                for i in range(100)
                if file_shard(self.root / f"test/test_exec_{i}.py", self.root, 4) == shard
            )
            targets.append(name + "::test_value")
            self.write(
                name,
                "import types\nfrom pathlib import Path\n"
                "from sage_lib import logic as alias\n"
                "from kiro_crew.apps.builtins.code_review_sage.sage_lib import logic as canonical\n"
                "from tests import fixtures\n"
                "def test_value():\n"
                f"    outside = Path({str(outside)!r})\n"
                "    script = types.ModuleType('standalone_script')\n"
                "    exec(compile(outside.read_text(encoding='utf-8'), str(outside), 'exec'), script.__dict__)\n"
                "    assert script.VALUE == 7\n"
                f"    path = Path({str(builder)!r})\n"
                "    text = path.read_text(encoding='utf-8')\n"
                "    for mutated in (False, True):\n"
                "        mod = types.ModuleType('smc_build_v' + str(mutated))\n"
                "        mod.__file__ = str(path)\n"
                "        variant = text.replace(\"'yes'\", \"'changed'\") if mutated else text\n"
                "        exec(compile(variant, str(path), 'exec'), mod.__dict__)\n"
                f"        value = {bool(shard % 2)!r}\n"
                "        expected = ('changed' if mutated else 'yes') if value else 'no'\n"
                "        assert mod.choose(value) == expected\n"
                "    assert alias.choose(value) == canonical.choose(value) == ('yes' if value else 'no')\n"
                "    assert fixtures.choose(value) == ('yes' if value else 'no')\n",
            )
        args = ("-p", "pytest_cov.plugin", *selectors, "--cov-branch", "--cov-report=")
        self.assert_ok(self.run_pytest(*args))
        self.assertCountEqual(self.items(), targets)
        (self.root / ".coverage").rename(self.root / "baseline.coverage")
        baseline = CoverageData(basename=str(self.root / "baseline.coverage"))
        baseline.read()
        # pytest-cov erases sibling .coverage.* files when the next run starts.
        # Keep staged artifacts outside that glob until every shard has finished.
        staged = self.root / "shards"
        staged.mkdir()
        actual = []
        for shard in range(1, 5):
            self.assert_ok(self.run_pytest(*args, "--file-shards=4", f"--file-shard={shard}"))
            actual.extend(self.items())
            (self.root / ".coverage").rename(staged / f".coverage.shard{shard}")
        self.assertCountEqual(actual, targets)
        self.assertEqual(len(actual), len(set(actual)))
        self.assertEqual(len(list(staged.iterdir())), 4)
        self.assert_ok(self.run_python("-m", "coverage", "combine", str(staged)))
        combined = CoverageData(basename=str(self.root / ".coverage"))
        combined.read()
        self.assertEqual(combined.measured_files(), baseline.measured_files())
        for filename in baseline.measured_files():
            self.assertEqual(
                sorted(combined.arcs(filename)), sorted(baseline.arcs(filename)), filename
            )
        self.assert_covered_sources(combined, (builder_name, f"{sage}/logic.py", fixture_name))
        self.assertNotIn(
            outside_name, {key.replace("\\", "/") for key in combined.measured_files()}
        )

    def test_coverage_data_mixed_keys_preserve_presence_lines_arcs_and_uniqueness(self):
        from coverage import CoverageData

        builder = "src/kiro_crew/builder.py"
        sage = "src/kiro_crew/apps/builtins/code_review_sage/sage_lib/logic.py"
        # A Windows run remaps Sage to POSIX paths but leaves builder keys native.
        data = CoverageData(no_disk=True)
        native_builder = str(PureWindowsPath(builder))
        arcs = [(-1, 1), (1, 2), (2, 3), (3, 4), (4, -1)]
        data.add_arcs({native_builder: arcs, sage: arcs})
        self.assert_covered_sources(data, (builder, sage))
        self.assertIsNone(data.lines(builder), "the API itself must still query exact keys")
        self.assertEqual(sorted(data.arcs(native_builder)), sorted(arcs))
        self.assertEqual(sorted(data.arcs(sage)), sorted(arcs))
        with self.assertRaises(AssertionError):
            self.assert_covered_sources(data, ("src/kiro_crew/missing.py",))
        data.add_arcs({builder: arcs})
        with self.assertRaisesRegex(AssertionError, "duplicate source identity"):
            self.assert_covered_sources(data, (builder, sage))

    def test_assignment_is_stable_across_checkout_roots_and_path_flavours(self):
        unix_root = PurePosixPath("/checkout")
        win_root = PureWindowsPath("C:/different checkout")
        for i in range(100):
            name = f"test/sub dir/test_{i}.py"
            self.assertEqual(
                file_shard(unix_root / name, unix_root, 8),
                file_shard(win_root / name, win_root, 8),
            )

    def test_workflow_only_changes_linux_and_windows_non_leaf_partitioning(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
        for name in ("backend-test", "backend-test-windows"):
            job = workflow["jobs"][name]
            count = job["env"]["SHARD_COUNT"]
            self.assertEqual(count, 8)
            self.assertEqual(job["strategy"]["matrix"]["group"], list(range(1, count + 1)))
            command = next(
                s["run"] for s in job["steps"] if s.get("name", "").startswith("Run tests")
            )
            self.assertIn("-p scripts.ci_file_shards", command)
            self.assertIn('--file-shards "$SHARD_COUNT" --file-shard ${{ matrix.group }}', command)
            self.assertNotIn("--splits", command)
            pins = (
                "ci_file_shard_pins.txt"
                if name == "backend-test"
                else "ci_file_shard_pins_windows.txt"
            )
            leaf = command.split('if [ "$LEAF" = "true" ]; then', 1)[1].split("exit 0", 1)[0]
            # Corpus gates are sharded; changed files are not (they repeat on every job).
            self.assertIn("ci_file_shards", leaf)
            self.assertIn("leaf changed files, pass", leaf)
            changed_loop = leaf.split("leaf changed files, pass", 1)[1]
            self.assertNotIn("ci_file_shards", changed_loop)
            self.assertIn('"${CHANGED[@]}"', leaf)
            # Subset lists reach pytest as a targets file, never as argv. The
            # `=` keeps the out-of-checkout path out of rootdir discovery.
            gates_run = leaf.split("leaf changed files, pass", 1)[0]
            self.assertIn('--file-shard-targets="$RUNNER_TEMP/leaf-gates.txt"', gates_run)
            reduced = command.rsplit('if [ "$REDUCED" = "true" ]; then', 1)[1]
            reduced = reduced.split("exit 0", 1)[0]
            self.assertIn('--file-shard-targets="$TARGETS_FILE"', reduced)
            self.assertNotIn("TARGETS[@]", reduced)
            # Pins are measured on the full suite and apply to it alone; the
            # leaf and reduced invocations keep pure hash ownership.
            head, full = command.rsplit("exit 0", 1)
            self.assertNotIn("--file-shard-pins", head)
            self.assertNotIn("--outer-pins", head)
            if name == "backend-test":
                # The Linux full suite runs this shard as parts: the script gets
                # the shard and its pin file, and loads the plugin in each part.
                self.assertIn("python scripts/run_sharded_suite.py", full)
                self.assertIn(
                    '--outer-shards "$SHARD_COUNT" --outer-shard ${{ matrix.group }}', full
                )
                self.assertIn(f"--outer-pins scripts/{pins}", full)
                self.assertNotIn("--file-shard", full)
            else:
                self.assertIn(f"--file-shard-pins scripts/{pins}", full)
            run_env = next(s for s in job["steps"] if s.get("name", "").startswith("Run tests"))[
                "env"
            ]
            self.assertEqual(run_env["REDUCED"], "${{ steps.scope.outputs.reduced }}")
            self.assertEqual(run_env["LEAF"], "${{ steps.scope.outputs.leaf }}")
            if name == "backend-test":
                self.assertEqual(run_env["LEAF_REPEAT"], "3")
                self.assertCountEqual(
                    [word for word in command.split() if word.startswith("--cov=")],
                    [
                        "--cov=kiro_crew",
                        "--cov=sage_lib",
                        "--cov=src/kiro_crew/apps/builtins/aws_control/crew/packaging",
                        "--cov=src/kiro_crew/apps/builtins/code_review_sage/tests",
                    ],
                )
            else:
                # Changed files run unsharded on all eight Windows shards; one
                # pass each keeps a heavy leaf diff inside the 60-minute job.
                self.assertEqual(run_env["LEAF_REPEAT"], "1")
                # The ~45 KB gate list would pass Windows' 32,767-character env
                # var limit and break every test that spawns a child.
                self.assertNotIn("LEAF_GATES", run_env)
                self.assertIn('done < "$GATES_FILE"', leaf)
                self.assertNotIn("--cov=", command)
                self.assertIn("--no-cov", command)
        for name, job in workflow["jobs"].items():
            if name not in ("backend-test", "backend-test-windows"):
                self.assertNotIn("ci_file_shards", json.dumps(job))

    def run_windows_scope(
        self,
        selector="printf 'test/test_a.py\\n'",
        timeout=_DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
        **env,
    ):
        """Execute backend-test-windows' "Select test scope" step; return (reduced, leaf)."""
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
        step = next(
            s for s in workflow["jobs"]["backend-test-windows"]["steps"] if s.get("id") == "scope"
        )
        call = "python scripts/ci-surface-tests.py --surface backend --summary"
        self.assertEqual(step["run"].count(call), 1)
        out = self.root / "github-output"
        out.write_text("", encoding="utf-8")
        caps = {k: v for k, v in step["env"].items() if not v.startswith("${{")}
        inputs = {
            "EVENT": "pull_request",
            "WINDOWS_PATHS": "false",
            "ONLY_FRONTEND": "false",
            "LEAF_TESTS": "false",
            "LEAF_TARGETS": "",
            "LEAF_GATES": "",
            **env,
        }
        result = subprocess.run(
            # The shell flags Actions uses for `shell: bash`.
            [
                "bash",
                "--noprofile",
                "--norc",
                "-eo",
                "pipefail",
                "-c",
                step["run"].replace(call, selector),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            env={
                "PATH": os.environ.get("PATH", ""),
                "GITHUB_OUTPUT": str(out),
                "TARGETS_FILE": str(self.root / "targets.txt"),
                "GATES_FILE": str(self.root / "gates.txt"),
                **caps,
                **inputs,
            },
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        outputs = dict(
            line.split("=", 1)
            for line in out.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        return outputs.get("reduced"), outputs.get("leaf")

    # The step is OS-independent bash; on a Windows host `bash` on PATH can be the
    # WSL launcher (test/ai_review_workflows_helpers.py), so the Linux shards run it.
    @unittest.skipIf(os.name == "nt", "runs the workflow's bash; PATH bash may be WSL on Windows")
    def test_windows_scope_reduces_only_where_linux_does_and_no_windows_path_hit(self):
        leaf = {
            "LEAF_TESTS": "true",
            "LEAF_TARGETS": "test/test_a.py\ntest/test_gate.py\n",
            "LEAF_GATES": "test/test_gate.py\n",
        }
        frontend = {"ONLY_FRONTEND": "true"}
        # Frontend-only and leaf-only diffs take the reduced and leaf scopes.
        self.assertEqual(self.run_windows_scope(**frontend), ("true", None))
        self.assertEqual(self.run_windows_scope(**leaf), ("true", "true"))
        # A hit (or an absent) `windows` bucket keeps the full suite on both.
        for paths in ("true", ""):
            self.assertEqual(
                self.run_windows_scope(WINDOWS_PATHS=paths, **frontend), ("false", None)
            )
            self.assertEqual(self.run_windows_scope(WINDOWS_PATHS=paths, **leaf), ("false", None))
        # A push (main, release/**) always runs the full Windows suite.
        self.assertEqual(self.run_windows_scope(EVENT="push", **frontend), ("false", None))
        self.assertEqual(self.run_windows_scope(EVENT="push", **leaf), ("false", None))
        # A backend change, and `ci-full-run` (which forces only_frontend and
        # leaf_tests false in the `changes` job), run the full suite.
        self.assertEqual(self.run_windows_scope(), ("false", None))
        # Selector failure or empty output falls back to the full suite.
        self.assertEqual(self.run_windows_scope("false", **frontend), ("false", None))
        self.assertEqual(self.run_windows_scope("true", **frontend), ("false", None))
        # Empty leaf targets fall through to the frontend/backend decision.
        self.assertEqual(
            self.run_windows_scope(LEAF_TESTS="true", LEAF_TARGETS=" \n"), ("false", None)
        )
        # Leaf weight budget: past either cap the full suite runs instead.
        many = "".join(f"test/test_leaf_{i}.py\n" for i in range(41))
        self.assertEqual(
            self.run_windows_scope(LEAF_TESTS="true", LEAF_TARGETS=many), ("false", None)
        )
        heavy = sorted(read_pins(ROOT / "scripts/ci_file_shard_pins_windows.txt", 8))
        five = "".join(f"{name}\n" for name in heavy[:5])
        self.assertEqual(
            self.run_windows_scope(LEAF_TESTS="true", LEAF_TARGETS=five), ("false", None)
        )
        four = "".join(f"{name}\n" for name in heavy[:4])
        self.assertEqual(
            self.run_windows_scope(LEAF_TESTS="true", LEAF_TARGETS=four), ("true", "true")
        )
        # A file Windows never collects is dropped from the explicit changed list
        # (explicit paths bypass pytest_ignore_collect); none left runs full.
        listed = (ROOT / "test/windows-collect-ignore.txt").read_text(encoding="utf-8")
        ignored = next(n for n in (ln.split("#", 1)[0].strip() for ln in listed.splitlines()) if n)
        both = f"test/{ignored}\ntest/test_a.py\n"
        self.assertEqual(
            self.run_windows_scope(LEAF_TESTS="true", LEAF_TARGETS=both), ("true", "true")
        )
        kept = (self.root / "targets.txt").read_text(encoding="utf-8").split()
        self.assertEqual(kept, ["test/test_a.py"])
        self.assertEqual(
            self.run_windows_scope(LEAF_TESTS="true", LEAF_TARGETS=f"test/{ignored}\n"),
            ("false", None),
        )

    def test_full_run_and_push_leave_the_windows_scope_inputs_false(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
        changes = workflow["jobs"]["changes"]
        self.assertEqual(changes["outputs"]["windows_paths"], "${{ steps.filter.outputs.windows }}")
        steps = {s.get("id"): s for s in changes["steps"]}
        full_run = "${{ contains(github.event.pull_request.labels.*.name, 'ci-full-run') }}"
        for step_id in ("decide", "leaf"):
            self.assertEqual(steps[step_id]["env"]["FULL_RUN"], full_run)
        self.assertIn('[ "$FULL_RUN" != "true" ]', steps["decide"]["run"])
        self.assertIn('[ "$FULL_RUN" = "true" ] || [ -z "${BASE_SHA:-}" ]', steps["leaf"]["run"])
        windows = workflow["jobs"]["backend-test-windows"]
        scope = next(s for s in windows["steps"] if s.get("id") == "scope")
        self.assertEqual(
            scope["env"]["WINDOWS_PATHS"], "${{ needs.changes.outputs.windows_paths }}"
        )
        self.assertEqual(scope["env"]["EVENT"], "${{ github.event_name }}")


if __name__ == "__main__":
    unittest.main()
