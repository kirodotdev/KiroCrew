"""File shards cover the original suite once without importing sibling files."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath

import yaml

from scripts.ci_file_shards import file_shard

ROOT = Path(__file__).resolve().parents[1]


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

    def run_python(self, *extra):
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
        return subprocess.run(
            [sys.executable, "-B", *extra],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=45,
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

    def test_reduced_backend_scope_populates_every_ci_shard(self):
        result = self.run_python(str(ROOT / "scripts/ci-surface-tests.py"), "--surface", "backend")
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
            if name == "backend-test":
                leaf = command.split('if [ "$LEAF" = "true" ]; then', 1)[1].split("exit 0", 1)[0]
                self.assertNotIn("ci_file_shards", leaf)
                self.assertIn('"${CHANGED[@]}"', leaf)
                self.assertCountEqual(
                    [word for word in command.split() if word.startswith("--cov=")],
                    [
                        "--cov=kiro_crew",
                        "--cov=sage_lib",
                        "--cov=src/kiro_crew/apps/builtins/aws_control/crew/packaging",
                        "--cov=src/kiro_crew/apps/builtins/code_review_sage/tests",
                    ],
                )
        for name, job in workflow["jobs"].items():
            if name not in ("backend-test", "backend-test-windows"):
                self.assertNotIn("ci_file_shards", json.dumps(job))


if __name__ == "__main__":
    unittest.main()
