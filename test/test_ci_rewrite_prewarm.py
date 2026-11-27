"""The CI rewrite prewarm writes the pyc pytest reads, and imports nothing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest_floor_configure import _redirect_bytecode_cache, _shared_cache_root

from kiro_crew.testing.wait import default_timeout
from scripts import ci_rewrite_prewarm
from scripts.ci_file_shards import file_shard

ROOT = Path(__file__).resolve().parents[1]
# One worker runs this module, so its one interpreter child (and that child's
# two pool processes) never starts beside another copy of itself.
pytestmark = pytest.mark.xdist_group(name="ci_rewrite_prewarm_child")

_IMPORT_MARKER = "open(__file__ + '.imported', 'w', encoding='utf-8').close()\n"
_TEST_FILES = {"test_alpha.py", "test_beta.py", "test_gamma.py"}

# Runs every scenario in ONE interpreter: the prewarm through its own ``main``,
# then pytest in-process on the same tree. Guarded, because the prewarm's
# spawned pool processes import this file as their main module.
_DRIVER = r"""
import contextlib
import hashlib
import io
import json
import os
import sys
from pathlib import Path

_WATCHED = ("kiro_crew", "conftest", "test_alpha", "test_beta", "test_gamma", "prewarm_pkg")


def loaded():
    return sorted(name for name in sys.modules if name.split(".")[0] in _WATCHED)


def snapshot(root, prefix):
    base = Path(prefix)
    return {
        "markers": sorted(path.name for path in Path(root).rglob("*.imported")),
        "pycs": {
            path.relative_to(base).as_posix(): [
                path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            ]
            for path in sorted(base.rglob("*.pyc"))
        },
    }


def prewarm(step):
    from scripts import ci_rewrite_prewarm

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = ci_rewrite_prewarm.main(
            ["--file-shards", "1", "--file-shard", "1", "--jobs", str(step["jobs"])]
            + ["--pycache-prefix", step["prefix"]]
            + step.get("more", [])
        )
    return {"exit": code, "stdout": out.getvalue(), "loaded": loaded()}


def collect(step):
    import pytest

    out = io.StringIO()
    previous = sys.pycache_prefix
    sys.pycache_prefix = step["prefix"]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = int(pytest.main(["-q", "-p", "no:cacheprovider", "--debug=" + step["log"]]))
    finally:
        sys.pycache_prefix = previous
        # The next scenario's tree reuses these module names.
        for name in loaded():
            del sys.modules[name]
    return {"exit": code, "stdout": out.getvalue()}


if __name__ == "__main__":
    results = {}
    for step in json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")):
        os.chdir(step["root"])
        result = {"prewarm": prewarm, "collect": collect}[step["do"]](step)
        result.update(snapshot(step["root"], step["prefix"]))
        results[step["key"]] = result
    Path(sys.argv[2]).write_text(json.dumps(results), encoding="utf-8")
"""


def _write(root: Path, name: str, content: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _tree(tmp_path: Path) -> Path:
    """A checkout-shaped tree, spelled as the OS reports the working directory."""
    root = Path(os.path.realpath(tmp_path)) / "checkout"
    _write(root, "setup.cfg", "[tool:pytest]\ntestpaths = test apps\n")
    _write(root, "conftest.py", "ROOT_CONFTEST = True\n")
    _write(root, "test/conftest.py", "TEST_CONFTEST = True\n")
    _write(root, "test/test_alpha.py", "def test_alpha():\n    assert 1 + 1 == 2\n")
    _write(root, "test/nested/test_beta.py", "def test_beta():\n    assert 'b' in 'abc'\n")
    _write(root, "apps/demo/tests/test_gamma.py", "def test_gamma():\n    assert [1] == [1]\n")
    return root


def _traced(log: Path) -> tuple[set[str], set[str]]:
    """The test files pytest's rewriter (read from cache, rewrote), from its trace."""
    cached: set[str] = set()
    rewritten: set[str] = set()
    for line in log.read_text(encoding="utf-8").splitlines():
        for name in _TEST_FILES:
            if name in line and "found cached rewritten pyc for" in line:
                cached.add(name)
            if name in line and "rewriting " in line:
                rewritten.add(name)
    return cached, rewritten


@pytest.fixture(scope="module")
def scenarios(tmp_path_factory):
    """Run all four scenarios in one interpreter child; return each step's result.

    ``hit`` prewarms a tree whose modules leave a marker when executed (two pool
    processes), then runs pytest on it. ``cold`` runs pytest with no prewarm.
    ``elsewhere`` prewarms under one cache root and runs pytest with another.
    ``broken`` prewarms a tree holding a file that does not parse. The child's
    own bytecode goes to a decoy root, so a pyc under a scenario's cache root
    got there through the root the prewarm was given.
    """
    base = Path(os.path.realpath(tmp_path_factory.mktemp("rewrite-prewarm")))
    names = ("hit", "cold", "elsewhere", "broken", "product")
    roots = {name: _tree(base / name) for name in names}
    for name in ("conftest.py", "test/conftest.py", "test/test_alpha.py"):
        _write(roots["hit"], name, _IMPORT_MARKER + "def test_alpha():\n    assert True\n")
    broken = _write(roots["broken"], "test/test_broken.py", "def test_broken(:\n")
    # A product package on the ini's pythonpath: one module a test imports, one nothing
    # imports, one that does not compile.
    product = roots["product"]
    _write(product, "setup.cfg", "[tool:pytest]\ntestpaths = test apps\npythonpath = src\n")
    _write(product, "src/prewarm_pkg/__init__.py", "")
    _write(product, "src/prewarm_pkg/used.py", _IMPORT_MARKER + "VALUE = 2\n")
    _write(product, "src/prewarm_pkg/unused.py", _IMPORT_MARKER + "VALUE = 3\n")
    uncompilable = _write(product, "src/prewarm_pkg/bad.py", "def bad(:\n")
    _write(
        product,
        "test/test_alpha.py",
        "from prewarm_pkg import used\n\n\ndef test_alpha():\n    assert used.VALUE == 2\n",
    )

    def step(do: str, name: str, cache: str = "cache", **extra: object) -> dict[str, object]:
        return {
            "do": do,
            "key": f"{name}.{do}",
            "root": str(roots[name]),
            "prefix": str(base / name / cache),
            "log": str(base / name / "trace.log"),
            **extra,
        }

    plan = [
        step("prewarm", "hit", jobs=2),
        step("collect", "hit"),
        step("collect", "cold"),
        step("prewarm", "elsewhere", cache="other", jobs=1),
        step("collect", "elsewhere"),
        step("prewarm", "broken", jobs=1),
        step("prewarm", "product", cache="unasked", jobs=1),
        step("prewarm", "product", jobs=2, more=["--compile-product"]),
        step("collect", "product"),
    ]
    driver = _write(base, "driver.py", _DRIVER)
    plan_file = _write(base, "plan.json", json.dumps(plan))
    env = dict(os.environ)
    for name in (
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "PYTHONDONTWRITEBYTECODE",
        "COV_CORE_SOURCE",
        "COV_CORE_DATAFILE",
        "COVERAGE_PROCESS_START",
    ):
        env.pop(name, None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONPYCACHEPREFIX"] = str(base / "decoy-cache")
    env["TMPDIR"] = env["TMP"] = env["TEMP"] = str(base)
    try:
        child = subprocess.run(
            [sys.executable, str(driver), str(plan_file), str(base / "results.json")],
            cwd=base,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=default_timeout(),
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"scenario child still running at its {exc.timeout}s ceiling: {exc.stdout!r}")
    assert child.returncode == 0, child.stdout + child.stderr
    results = json.loads((base / "results.json").read_text(encoding="utf-8"))
    assert not list((base / "decoy-cache").rglob("*-pytest-*.pyc"))
    return {"results": results, "base": base, "broken": broken, "uncompilable": uncompilable}


def _stems(pycs: dict[str, object]) -> set[str]:
    return {name.rsplit("/", 1)[-1].split(".", 1)[0] for name in pycs}


def test_pytest_reads_the_prewarmed_pyc_instead_of_rewriting(scenarios):
    prewarm = scenarios["results"]["hit.prewarm"]
    collect = scenarios["results"]["hit.collect"]
    assert prewarm["exit"] == 0
    assert "5 of 5 files cached, 0 left to pytest, 2 processes" in prewarm["stdout"]
    assert len(prewarm["pycs"]) == 5
    assert _stems(prewarm["pycs"]) == {"conftest", "test_alpha", "test_beta", "test_gamma"}
    assert collect["exit"] == 0 and "3 passed" in collect["stdout"], collect["stdout"]

    cached, rewritten = _traced(scenarios["base"] / "hit" / "trace.log")

    assert cached == _TEST_FILES
    assert rewritten == set()
    # pytest replaces a pyc it does not accept, a conftest's included.
    assert {name: collect["pycs"][name] for name in prewarm["pycs"]} == prewarm["pycs"]


def test_without_the_prewarm_pytest_rewrites_every_file(scenarios):
    collect = scenarios["results"]["cold.collect"]
    assert collect["exit"] == 0 and "3 passed" in collect["stdout"], collect["stdout"]

    cached, rewritten = _traced(scenarios["base"] / "cold" / "trace.log")

    assert cached == set()
    assert rewritten == _TEST_FILES


def test_a_pyc_under_another_cache_root_is_not_read(scenarios):
    prewarm = scenarios["results"]["elsewhere.prewarm"]
    collect = scenarios["results"]["elsewhere.collect"]
    assert "5 of 5 files cached, 0 left to pytest, 1 processes" in prewarm["stdout"]
    assert len(prewarm["pycs"]) == 5
    assert collect["exit"] == 0 and "3 passed" in collect["stdout"], collect["stdout"]

    cached, rewritten = _traced(scenarios["base"] / "elsewhere" / "trace.log")

    assert cached == set()
    assert rewritten == _TEST_FILES


def test_prewarm_executes_no_test_module_and_no_conftest(scenarios):
    prewarm = scenarios["results"]["hit.prewarm"]
    assert prewarm["exit"] == 0
    assert prewarm["loaded"] == []
    assert prewarm["markers"] == []
    # Positive control: pytest does execute them, so a marker would have been seen.
    assert set(scenarios["results"]["hit.collect"]["markers"]) == {
        "conftest.py.imported",
        "test_alpha.py.imported",
    }


def test_a_file_that_cannot_be_rewritten_is_left_to_pytest(scenarios):
    prewarm = scenarios["results"]["broken.prewarm"]

    assert prewarm["exit"] == 0
    assert "5 of 6 files cached, 1 left to pytest" in prewarm["stdout"], prewarm["stdout"]
    assert f"left to pytest: {scenarios['broken']}: SyntaxError" in prewarm["stdout"]
    assert _stems(prewarm["pycs"]) == {"conftest", "test_alpha", "test_beta", "test_gamma"}


def _product_pycs(pycs: dict[str, object]) -> dict[str, object]:
    """The import-cache pycs of the scenario's product package, by module name."""
    return {
        name.rsplit("/", 1)[-1].split(".", 1)[0]: stamp
        for name, stamp in pycs.items()
        if "/src/prewarm_pkg/" in name and "-pytest-" not in name
    }


def test_product_modules_are_compiled_without_being_executed(scenarios):
    prewarm = scenarios["results"]["product.prewarm"]

    assert prewarm["exit"] == 0
    assert "3 of 4 product modules compiled, 1 left to the importer" in prewarm["stdout"]
    assert f"left to the importer: {scenarios['uncompilable']}: " in prewarm["stdout"]
    assert set(_product_pycs(prewarm["pycs"])) == {"__init__", "used", "unused"}
    assert prewarm["loaded"] == []
    assert prewarm["markers"] == []


def test_import_reads_the_compiled_product_module_instead_of_compiling_it(scenarios):
    before = _product_pycs(scenarios["results"]["product.prewarm"]["pycs"])
    collect = scenarios["results"]["product.collect"]

    assert collect["exit"] == 0 and "3 passed" in collect["stdout"], collect["stdout"]
    # The module ran, so it was imported; an importer that did not accept the pyc
    # would have written its own over it, with a new mtime.
    assert "used.py.imported" in collect["markers"]
    assert "unused.py.imported" not in collect["markers"]
    assert _product_pycs(collect["pycs"]) == before


def test_product_modules_are_compiled_only_when_asked(scenarios):
    """The Linux step does not pass the option, and must keep doing what it did."""
    # The first prewarm of the product tree ran without --compile-product, under its
    # own cache root; the keyed result is the second one's, so read the first's cache.
    unasked = scenarios["base"] / "product" / "unasked"

    pycs = [path.name for path in unasked.rglob("*.pyc")]

    assert pycs and all("-pytest-" in name for name in pycs)
    assert not [path for path in unasked.rglob("*.pyc") if "prewarm_pkg" in path.parts]


def test_a_tree_without_a_pythonpath_compiles_no_product_module(tmp_path, capsys):
    root = _tree(tmp_path)
    prefix = tmp_path / "cache"

    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(root)
        argv = ["--file-shards", "1", "--file-shard", "1", "--jobs", "1", "--compile-product"]
        code = ci_rewrite_prewarm.main([*argv, "--pycache-prefix", str(prefix)])

    assert code == 0
    assert "product modules" not in capsys.readouterr().out


def test_product_files_are_every_module_under_the_ini_pythonpath(tmp_path):
    root = _tree(tmp_path)
    _write(root, "setup.cfg", "[tool:pytest]\ntestpaths = test\npythonpath = src missing\n")
    for name in ("src/pkg/__init__.py", "src/pkg/deep/mod.py", "src/top.py"):
        _write(root, name, "")
    _write(root, "src/pkg/data.json", "{}")
    _write(root, "src/pkg/__pycache__/stale.py", "")
    _write(root, "other/outside.py", "")

    found = sorted(
        path.relative_to(root).as_posix() for path in ci_rewrite_prewarm.product_files(root)
    )

    assert found == ["src/pkg/__init__.py", "src/pkg/deep/mod.py", "src/top.py"]


def test_unusable_arguments_never_fail_the_step(tmp_path, capsys):
    missing = tmp_path / "no-such-pins.txt"
    for argv in (
        ["--file-shards", "many", "--file-shard", "1"],
        ["--file-shards", "8", "--file-shard", "9"],
        ["--file-shards", "8", "--file-shard", "1", "--file-shard-pins", str(missing)],
    ):
        assert ci_rewrite_prewarm.main(argv) == 0
        assert "skipped" in capsys.readouterr().out


def test_each_shard_rewrites_the_files_the_test_step_assigns_it(tmp_path):
    root = _tree(tmp_path)
    for index in range(40):
        _write(root, f"test/test_file_{index}.py", "def test_ok():\n    assert True\n")
    tests, conftests = ci_rewrite_prewarm.discover(root)
    assert len(tests) == 43
    assert conftests == [root / "conftest.py", root / "test" / "conftest.py"]
    pinned = "test/test_alpha.py"
    pins = {pinned: 1 + file_shard(root / pinned, root, 4) % 4}

    owned = {
        index: ci_rewrite_prewarm.shard_files(root, 4, index, pins)[: -len(conftests)]
        for index in range(1, 5)
    }

    assert sorted(path for paths in owned.values() for path in paths) == sorted(tests)
    for index, paths in owned.items():
        assert paths, f"shard {index} owns nothing"
        assert all(file_shard(path, root, 4, pins) == index for path in paths)
        assert ci_rewrite_prewarm.shard_files(root, 4, index, pins)[-len(conftests) :] == conftests
    assert root / pinned in owned[pins[pinned]]


def test_discovery_follows_the_ini_and_skips_what_pytest_never_enters(tmp_path):
    root = tmp_path / "checkout"
    _write(
        root,
        "setup.cfg",
        "[tool:pytest]\npython_files = check_*.py\ntestpaths =\n    # a comment line\n    suite\n",
    )
    kept = _write(root, "suite/deep/check_kept.py", "")
    for name in (
        "suite/test_other_pattern.py",
        "suite/.hidden/check_hidden.py",
        "suite/node_modules/check_vendored.py",
        "suite/__pycache__/check_cached.py",
        "suite/env/check_in_virtualenv.py",
        "elsewhere/check_outside_testpaths.py",
    ):
        _write(root, name, "")
    _write(root, "suite/env/pyvenv.cfg", "")

    assert ci_rewrite_prewarm.discover(root) == ([kept], [])
    assert not ci_rewrite_prewarm.assertion_pass_hook_enabled(root)
    _write(root, "setup.cfg", "[tool:pytest]\nenable_assertion_pass_hook = true\n")
    assert ci_rewrite_prewarm.assertion_pass_hook_enabled(root)


def test_the_repository_ini_keeps_the_plain_rewrite():
    assert not ci_rewrite_prewarm.assertion_pass_hook_enabled(ROOT)
    tests, conftests = ci_rewrite_prewarm.discover(ROOT)
    assert ROOT / "test" / "test_ci_rewrite_prewarm.py" in tests
    assert ROOT / "conftest.py" in conftests


def test_cache_root_is_the_one_the_rootdir_conftest_sets(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    monkeypatch.setattr(sys, "pycache_prefix", sys.pycache_prefix)
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", os.environ.get("PYTHONPYCACHEPREFIX", ""))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    _redirect_bytecode_cache()

    assert sys.pycache_prefix == os.path.join(str(tmp_path), "kirocrew", "pycache")
    assert ci_rewrite_prewarm.pycache_prefix() == sys.pycache_prefix
    # Without XDG_CACHE_HOME both fall back to the account's real home.
    monkeypatch.delenv("XDG_CACHE_HOME")
    assert ci_rewrite_prewarm.pycache_prefix() == os.path.join(
        _shared_cache_root(), "kirocrew", "pycache"
    )
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    assert ci_rewrite_prewarm.pycache_prefix() is None


def test_workflow_prewarms_only_the_full_suite_right_before_the_tests():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    for name, pins in (
        ("backend-test", "scripts/ci_file_shard_pins.txt"),
        ("backend-test-windows", "scripts/ci_file_shard_pins_windows.txt"),
    ):
        steps = workflow["jobs"][name]["steps"]
        names = [step.get("name", "") for step in steps]
        prewarm = [i for i, n in enumerate(names) if n.startswith("Prewarm assertion rewrites")]
        tests = [i for i, n in enumerate(names) if n.startswith("Run tests")]
        assert len(prewarm) == 1 and len(tests) == 1, names
        assert prewarm[0] + 1 == tests[0]
        step, run_tests = steps[prewarm[0]], steps[tests[0]]
        # The scope step writes reduced=false for the full suite and for nothing else.
        assert step["if"] == "${{ steps.scope.outputs.reduced == 'false' }}"
        # Same user, same working directory, same shard as the test step.
        assert step.get("shell") == run_tests.get("shell")
        assert "working-directory" not in step and "working-directory" not in run_tests
        command = " ".join(step["run"].replace("\\\n", " ").split())
        # The product modules are compiled on Windows only: there it shortens a
        # worker's collection, on Linux the coverage tracer hides the compile time.
        product = " --compile-product" if name == "backend-test-windows" else ""
        assert command == (
            "python -m scripts.ci_rewrite_prewarm "
            '--file-shards "$SHARD_COUNT" --file-shard ${{ matrix.group }} '
            f"--file-shard-pins {pins}{product}"
        )
        assert f"--file-shard-pins {pins}" in run_tests["run"]
        # A cache-root override in either step would send the two to different trees.
        for variable in ("XDG_CACHE_HOME", "PYTHONPYCACHEPREFIX", "PYTHONDONTWRITEBYTECODE"):
            assert variable not in step.get("env", {})
            assert variable not in run_tests.get("env", {})
        assert step["continue-on-error"] is True
        assert step["timeout-minutes"] <= 5
        # Bytecode is produced on the runner each run, never restored from a cache.
        assert not [s for s in steps if "actions/cache" in s.get("uses", "")]
    for name, job in workflow["jobs"].items():
        if name not in ("backend-test", "backend-test-windows"):
            assert "ci_rewrite_prewarm" not in str(job)
