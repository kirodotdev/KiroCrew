"""Tests for the CI surface-test selector (``scripts/ci-surface-tests.py``).

The selector decides which tests must still run when a diff touches only ONE
surface. Its contract is **deny-by-default**: it may only skip a file it can
positively prove is single-surface, so a heuristic miss costs CI time rather
than silently dropping a cross-surface parity guard.

These tests pin that contract, because the failure mode they guard against is
invisible: a guard quietly stops running and the drift it protects against
ships green.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

# One xdist worker for the whole module: every test here derives from ONE module-cached
# scan of src/ (rglob + ast.parse, ~30s). Under `--dist loadgroup` an unmarked module is
# spread across workers and each worker re-pays that scan -- measured at 5 workers x 40-75s
# per full run for this file alone. Grouping keeps the cache single-copy per run.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_ci_surface_tests")
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "ci-surface-tests.py"


def _load_selector():
    """Import the hyphenated script by path (not a normal module name)."""
    spec = importlib.util.spec_from_file_location("ci_surface_tests", _SCRIPT)
    assert spec and spec.loader, "could not build an import spec for the selector"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def selector():
    """The selector module, with ``collect`` memoized for the module's lifetime.

    ``collect`` rglobs and regex-scans every ``test_*.py`` under all three
    testpath roots -- ~3s a call -- and this module calls it once per parametrized
    guard over only three distinct answers. The memo key carries ``_is_windows()``
    because the Windows filter is applied inside ``collect``:
    ``test_posix_scope_is_unfiltered`` patches that seam between two
    ``collect("backend")`` calls and must still see two different scopes.
    """
    module = _load_selector()
    memo: dict[tuple[str, bool], list[str]] = {}
    uncached = module.collect

    def collect(surface: str) -> list[str]:
        key = (surface, module._is_windows())
        if key not in memo:
            memo[key] = uncached(surface)
        # A copy, so a caller that sorts or filters in place cannot poison the memo.
        return list(memo[key])

    module.collect = collect
    return module


def test_script_exists_and_is_executable() -> None:
    assert _SCRIPT.is_file(), f"missing selector script: {_SCRIPT}"


# Known cross-surface parity guards. Each of these lives in one suite but
# asserts against the OTHER surface's source, so each MUST stay in the must-run
# set. Extend this list when a new guard is added.
_BACKEND_GUARDS = (
    "test/test_redaction_mirror_parity.py",
    "test/test_theme_css_security.py",
    "test/test_dashboard_security_headers.py",
    "test/test_model_registry_parity.py",
    "test/test_builtin_app_assets.py",
    "test/test_recovery_card_parity.py",
    "test/test_artifact_import_parity.py",
    "test/test_knowledge_formats_parity.py",
    "test/test_windows_signing_contract.py",
    "test/test_meetings_routes.py",
    # Under the third testpath root -- these were silently unscanned until
    # src/kiro_crew/apps/builtins was added to _BACKEND_ROOTS.
    "src/kiro_crew/apps/builtins/design_critique/tests/test_manifest.py",
    "src/kiro_crew/apps/builtins/crew_companion/tests/test_manifest.py",
    "src/kiro_crew/apps/builtins/ops_mission_control/tests/test_routes.py",
)

_FRONTEND_GUARDS = (
    "website/src/test/appManifest.test.ts",
    "website/src/test/featureRequestLabels.test.ts",
    "website/src/test/themeCssCorpus.test.tsx",
    "website/src/test/serveDist.routes.test.ts",
    "website/electron/test/packaging.test.js",
    "website/electron/test/external-scheme.test.js",
    "website/electron/test/home-dir.test.js",
)


@pytest.mark.parametrize("guard", _BACKEND_GUARDS)
def test_backend_guards_are_never_skipped(selector, guard: str) -> None:
    """A pytest file that reads frontend source must stay in the must-run set."""
    # Assert existence rather than skipping: a rename must FAIL here so this
    # audited list gets updated, instead of silently going stale as a green skip.
    assert (_REPO_ROOT / guard).exists(), (
        f"{guard} was renamed or removed -- update _BACKEND_GUARDS so the audited "
        "cross-surface list cannot go stale."
    )
    assert guard in selector.collect("backend"), (
        f"{guard} reads the frontend surface but was classified single-surface; "
        "it would be SKIPPED on a frontend-only diff, defeating the guard."
    )


@pytest.mark.parametrize("guard", _FRONTEND_GUARDS)
def test_frontend_guards_are_never_skipped(selector, guard: str) -> None:
    """A spec that reads backend source must stay in the must-run set."""
    assert (_REPO_ROOT / guard).exists(), (
        f"{guard} was renamed or removed -- update _FRONTEND_GUARDS so the audited "
        "cross-surface list cannot go stale."
    )
    assert guard in selector.collect("frontend"), (
        f"{guard} reads the backend surface but was classified single-surface; "
        "it would be SKIPPED on a backend-only diff, defeating the guard."
    )


def test_backend_roots_cover_every_configured_testpath() -> None:
    """Every setup.cfg `testpaths` entry must be a scanned root.

    This is the contract that actually keeps the selector honest. A test file
    under an unenumerated root is not "unclassified but still running" -- the
    reduced run passes explicit paths, so it never runs at all. Adding a new
    testpath without adding it here must fail loudly.
    """
    import configparser

    parser = configparser.ConfigParser()
    parser.read(_REPO_ROOT / "setup.cfg")
    configured = parser.get("tool:pytest", "testpaths").split()
    assert configured, "setup.cfg declares no testpaths -- selector cannot be verified"
    missing = [p for p in configured if p not in _load_selector()._BACKEND_ROOTS]
    assert not missing, (
        f"setup.cfg testpaths {missing} are not scanned by the selector's "
        "_BACKEND_ROOTS, so every test under them would be SKIPPED (never run) "
        "on a frontend-only diff. Add them to _BACKEND_ROOTS."
    )


def test_frontend_spec_roots_cover_vitest_include(selector) -> None:
    """Every root in vitest's `test.include` must be a scanned frontend root.

    The mirror of the backend contract above. vitest overrides `include` in
    website/vite.config.ts, so that list -- not the vitest default -- is the
    authoritative set of specs the frontend job runs. A root missing here is
    dropped wholesale on a backend-only diff.
    """
    config = (_REPO_ROOT / "website" / "vite.config.ts").read_text(encoding="utf-8")
    # The test-config include is the one listing *.test.* globs (the other
    # `include:` in this file belongs to the coverage config).
    block = re.search(r"include:\s*\[([^\]]*\.test\.[^\]]*)\]", config)
    assert block, "could not locate vitest test.include in website/vite.config.ts"
    globs = re.findall(r"['\"]([^'\"]+)['\"]", block.group(1))
    assert globs, "vitest test.include parsed empty"

    scanned = {d for d, _ in selector._FRONTEND_SPECS}
    missing = sorted(
        f"website/{g.split('/', 1)[0]}"
        for g in globs
        if f"website/{g.split('/', 1)[0]}" not in scanned
    )
    assert not missing, (
        f"vitest test.include covers {missing}, which the selector's "
        "_FRONTEND_SPECS does not scan -- those specs would be SKIPPED (never "
        "run) on a backend-only diff. Add them to _FRONTEND_SPECS."
    )


def test_selection_is_a_strict_subset(selector) -> None:
    """The must-run set must be smaller than the suite (else there is no saving)."""
    backend = selector.collect("backend")
    frontend = selector.collect("frontend")
    assert backend, "expected at least one cross-surface backend file"
    assert frontend, "expected at least one cross-surface frontend spec"
    total_backend = sum(
        len(selector._iter_files(_REPO_ROOT, d, selector._BACKEND_GLOBS))
        for d in selector._BACKEND_ROOTS
    )
    assert len(backend) < total_backend, "selector skipped nothing -- no CI saving"


def test_unreadable_file_is_treated_as_cross_surface(selector, tmp_path) -> None:
    """Fail CLOSED: an IO error must keep the file running, not drop it."""
    missing = tmp_path / "does-not-exist.py"
    assert selector._is_cross_surface(missing, selector._BACKEND_FOREIGN) is True


def test_pure_backend_file_is_classified_single_surface(selector, tmp_path) -> None:
    """A file with no other-surface reference is skippable (the actual saving)."""
    pure = tmp_path / "test_pure.py"
    pure.write_text("from kiro_crew import config\n\n\ndef test_x():\n    assert config\n")
    assert selector._is_cross_surface(pure, selector._BACKEND_FOREIGN) is False


def test_frontend_escape_patterns_are_detected(selector, tmp_path) -> None:
    """Both escape styles must be caught -- the string form AND the segment form."""
    literal = tmp_path / "a.test.ts"
    literal.write_text("import x from '../../../src/kiro_crew/connections/registry.json'\n")
    assert selector._is_cross_surface(literal, selector._FRONTEND_FOREIGN) is True

    segments = tmp_path / "b.test.js"
    segments.write_text("const R = path.resolve(__dirname, '..', '..', '..', 'test');\n")
    assert selector._is_cross_surface(segments, selector._FRONTEND_FOREIGN) is True

    inside = tmp_path / "c.test.tsx"
    inside.write_text("import { Button } from '../../components/ui'\n")
    assert selector._is_cross_surface(inside, selector._FRONTEND_FOREIGN) is False


def test_backend_owned_config_references_are_cross_surface(selector, tmp_path) -> None:
    """A spec naming a backend-owned config file must stay in the must-run set.

    pyproject.toml, setup.cfg and the other TOML/CFG/YAML configs are all
    backend-owned (the one exception, website/AUTOSDE.yaml, only makes the
    match over-broad, which costs CI time), and a spec reaches them through
    a pre-computed root constant -- no ``../../../`` escape and no
    ``kiro_crew`` marker on the referencing line:

        const version = readFileSync(join(REPO_ROOT, 'pyproject.toml'), ...)

    With only directory/name/`.py` markers in the pattern, such a spec was
    classified single-surface and SKIPPED on a backend-only diff -- the
    exact silent-drop this selector exists to prevent. Measured live guards
    that this closes: ``releaseVersion.test.ts`` (pins release.yml's version
    mapping) and ``frontendBlobReconcile.wireFormat.test.ts`` (pins the
    reconcile script ci.yml runs).
    """
    toml = tmp_path / "a.test.ts"
    toml.write_text(
        "const REPO_ROOT = resolve(__dirname, '..', '..', '..')\n"
        "const version = readFileSync(join(REPO_ROOT, 'pyproject.toml'), 'utf-8')\n"
    )
    assert selector._is_cross_surface(toml, selector._FRONTEND_FOREIGN) is True

    cfg = tmp_path / "b.test.ts"
    cfg.write_text("const defaults = readFileSync(join(REPO_ROOT, 'setup.cfg'), 'utf-8')\n")
    assert selector._is_cross_surface(cfg, selector._FRONTEND_FOREIGN) is True

    yaml = tmp_path / "c.test.ts"
    yaml.write_text("// parity: release.yml maps v0.6.0-rc.2 to the wheel version 0.6.0rc2\n")
    assert selector._is_cross_surface(yaml, selector._FRONTEND_FOREIGN) is True


def test_frontend_owned_config_reference_is_cross_surface(selector, tmp_path) -> None:
    """The mirror: a backend guard naming the frontend's config file by bare name.

    ``tsconfig.json`` lives inside website/, so a realistic reference carries
    ``website`` in its path -- but a guard that quotes the filename alone
    (a joined path built from constants, a fixture table of config names)
    had no marker at all. Bare extensions cannot close this direction the
    way they close the mirror: the backend owns .toml/.cfg/.yaml/.json
    configs of its own, so only frontend-owned config NAMES are added here.
    """
    guard = tmp_path / "test_tsconfig_parity.py"
    guard.write_text(
        "# parity: the shipped tsconfig.json must not gain references\n"
        'TS_CONFIG = REPO / "tsconfig.json"\n'
    )
    assert selector._is_cross_surface(guard, selector._BACKEND_FOREIGN) is True


def test_own_surface_config_references_stay_single_surface(selector, tmp_path) -> None:
    """The asymmetry is the point: each surface's OWN configs must not trip it.

    A backend test reading pyproject.toml or setup.cfg is reading its own
    surface, and a frontend spec reading package.json or tsconfig.json is
    doing the same -- neither is a parity guard. This pins that the new
    terms went in per-direction, not as bare extensions on both patterns
    (which would have flooded the must-run set: 51 backend files reference
    a .yaml of their own).
    """
    backend_own = tmp_path / "test_own_cfg.py"
    backend_own.write_text(
        'VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]\n'
        "PARSER = configparser.ConfigParser()\n"
        'PARSER.read(ROOT / "setup.cfg")\n'
    )
    assert selector._is_cross_surface(backend_own, selector._BACKEND_FOREIGN) is False

    frontend_own = tmp_path / "own-configs.test.ts"
    frontend_own.write_text(
        "const pkg = JSON.parse(readFileSync('package.json', 'utf-8'))\n"
        "const ts = readFileSync('tsconfig.json', 'utf-8')\n"
    )
    assert selector._is_cross_surface(frontend_own, selector._FRONTEND_FOREIGN) is False


# ---------------------------------------------------------------------------
# Windows: the reduced scope must not name a suite Windows cannot collect
# ---------------------------------------------------------------------------
#
# The reduced target list is passed to pytest as EXPLICIT file arguments, and an
# explicit argument bypasses conftest's Windows `collect_ignore`. So a POSIX-only
# suite that reaches this list gets collected on the Windows shards and fails
# there -- on every diff that takes the reduced scope, which is any
# frontend-only diff. The suites below are the ones observed failing that way.

_IGNORE_LIST = _REPO_ROOT / "test" / "windows-collect-ignore.txt"

_OBSERVED_WINDOWS_FAILURES = (
    "test_acp_client.py",
    "test_deploy_web_handlers.py",
    "test_dev_fleet_app.py",
    "test_pid_lifecycle.py",
    "test_sandbox_argv.py",
)


def _ignore_names() -> set[str]:
    names = (
        ln.split("#", 1)[0].strip() for ln in _IGNORE_LIST.read_text(encoding="utf-8").splitlines()
    )
    return {n for n in names if n}


def test_ignore_list_exists_and_is_non_empty() -> None:
    assert _IGNORE_LIST.is_file(), f"missing ignore list: {_IGNORE_LIST}"
    assert _ignore_names(), "the Windows collect-ignore list parsed to nothing"


def test_ignore_list_matches_the_names_conftest_previously_inlined() -> None:
    """The extraction into a file must be lossless.

    conftest's `collect_ignore` branch only executes on Windows, so a parsing
    typo here would silently re-enable a suite that fails at import on win32 and
    would not be caught on a POSIX dev machine. Pin the exact set.

    ``test_harness.py`` left this set when the gateway harness became
    cross-platform (reader thread instead of ``selectors`` on a pipe, tree kill
    instead of ``terminate_pgid``); it now runs on the Windows shards.

    ``test_pod_windows_boot.py`` is the one entry here for a reason other than a
    POSIX assumption: it boots a real pod under Task Scheduler, needs a built
    ``.venv`` inside the checkout that the shards never create, and costs minutes.
    Its own dedicated ci.yml job names it on the command line, which bypasses this
    list by design.

    ``test_macos_pool_ceiling_posix.py`` executes the macos-on-demand `decide` job's
    real bash against a stub ``gh``, so it needs a POSIX shell AND an executable bit
    that survives. On Windows ``shutil.which("bash")`` resolves the WSL launcher,
    which cannot read the Windows tmp paths the harness writes, and ``chmod`` is a
    no-op on NTFS so the stub is never the ``gh`` that resolves. Listing the module
    is how a POSIX-only-by-design suite is stated here; a class-level ``skipif``
    would leave a marker that proves nothing on that shard either.
    """
    assert _ignore_names() == {
        "test_macos_pool_ceiling_posix.py",
        "test_sandbox_argv.py",
        "test_sandbox_cc_mode.py",
        "test_sandbox_hardlink_scan.py",
        "test_sandbox_md_notebook_carveout.py",
        "test_sandbox_nested_tier.py",
        "test_pid_lifecycle.py",
        "test_pid_sweep_helpers.py",
        "test_process_tree_kill.py",
        "test_source_providers.py",
        "test_terminal_handler.py",
        "test_acp_client.py",
        "test_stop_kill_cancel.py",
        "test_app_backend_stale_reap.py",
        "test_env.py",
        "test_outbox_notify_broadcast.py",
        "test_outbox_binary.py",
        "test_deploy_web_handlers.py",
        "test_snapshot.py",
        "test_theme_install.py",
        "test_webapp_preview.py",
        "test_file_raw.py",
        "test_file_download.py",
        "test_file_office_preview.py",
        "test_dashboard_file_io.py",
        "test_dev_fleet_app.py",
        "test_pod_windows_boot.py",
    }


def test_every_ignored_suite_exists() -> None:
    """A stale entry silently protects nothing -- catch renames and deletions."""
    missing = sorted(n for n in _ignore_names() if not (_REPO_ROOT / "test" / n).is_file())
    assert not missing, f"ignore list names files that no longer exist: {missing}"


def test_conftest_and_selector_read_the_same_ignore_list(selector) -> None:
    """One file, two readers -- so the two cannot drift apart.

    conftest builds `collect_ignore` from it for the recursive path; the selector
    filters it out of the explicit-argument path. If a future change re-inlines
    either copy, this fails.
    """
    assert selector._windows_collect_ignore(_REPO_ROOT) == frozenset(_ignore_names())


@pytest.mark.parametrize("suite", _OBSERVED_WINDOWS_FAILURES)
def test_windows_scope_excludes_uncollectable_suites(selector, monkeypatch, suite) -> None:
    """Each suite observed failing the Windows shards must be filtered out."""
    assert suite in _ignore_names(), f"{suite} is not on the ignore list"
    monkeypatch.setattr(selector, "_is_windows", lambda: True)
    selected = selector.collect("backend")
    offenders = [rel for rel in selected if Path(rel).name == suite]
    assert not offenders, f"Windows scope still names {suite}: {offenders}"


def test_windows_scope_names_nothing_on_the_ignore_list(selector, monkeypatch) -> None:
    monkeypatch.setattr(selector, "_is_windows", lambda: True)
    ignored = _ignore_names()
    offenders = sorted({Path(rel).name for rel in selector.collect("backend")} & ignored)
    assert not offenders, f"Windows scope names uncollectable suites: {offenders}"


def test_posix_scope_is_unfiltered(selector, monkeypatch) -> None:
    """The filter is Windows-only -- POSIX coverage must not shrink."""
    monkeypatch.setattr(selector, "_is_windows", lambda: False)
    posix_selected = set(selector.collect("backend"))
    monkeypatch.setattr(selector, "_is_windows", lambda: True)
    win_selected = set(selector.collect("backend"))
    assert win_selected <= posix_selected
    # The POSIX list must still carry suites the Windows one drops, or the filter
    # is a no-op and this whole guard proves nothing.
    assert posix_selected - win_selected


def test_windows_seam_follows_os_name(selector, monkeypatch) -> None:
    """The seam must be wired to the real platform, not just patchable.

    Safe to patch ``os.name`` here specifically because ``_is_windows`` builds no
    ``Path`` -- doing it around ``collect()`` would switch ``pathlib`` to
    ``WindowsPath`` and raise on POSIX.
    """
    monkeypatch.setattr("os.name", "nt")
    assert selector._is_windows() is True
    monkeypatch.setattr("os.name", "posix")
    assert selector._is_windows() is False


def test_missing_ignore_list_fails_open(selector, tmp_path) -> None:
    """A missing list must not empty the target set -- that would drop coverage."""
    assert selector._windows_collect_ignore(tmp_path) == frozenset()


def test_explicit_cli_target_bypasses_collect_ignore(tmp_path) -> None:
    """Why the filter lives in the selector and not only in conftest.

    pytest honours `collect_ignore` when it RECURSES into a directory and ignores
    it when the file is named on the command line. That asymmetry is the entire
    bug, so pin it: if a future pytest starts honouring `collect_ignore` for
    explicit arguments, this fails and the selector-side filter can be dropped.
    """
    import os
    import subprocess
    import sys

    suite = tmp_path / "t"
    suite.mkdir()
    (suite / "conftest.py").write_text('collect_ignore = ["test_boom.py"]\n', encoding="utf-8")
    (suite / "test_boom.py").write_text(
        'raise RuntimeError("import-time failure")\n', encoding="utf-8"
    )
    (suite / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    def run(*target: str) -> int:
        # Strip PYTEST_ADDOPTS so the nested pytest does not inherit the outer
        # run's options. An inherited `--basetemp` there points at an ancestor of
        # this child's cwd (its tmp_path), which pytest rejects as a usage error
        # (exit 4) before it ever evaluates collect_ignore -- turning this
        # collection-semantics assertion into a spurious failure. The child gets
        # its own basetemp under this test's tmp_path instead, so it never shares
        # (or prunes) the per-user `pytest-of-<user>` tree with the outer run's
        # xdist workers or with another run on the host.
        child_env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-p",
                "no:randomly",
                "--no-cov",
                f"--basetemp={tmp_path / 'basetemp'}",
                *target,
            ],
            cwd=tmp_path,
            env=child_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).returncode

    assert run(str(suite)) == 0, "recursive collection should honour collect_ignore"
    assert run(str(suite / "test_boom.py")) != 0, (
        "explicit argument now honours collect_ignore -- the selector-side "
        "filter may no longer be required"
    )


# ---------------------------------------------------------------------------
# The Windows filter must be scoped the way conftest's own exclusion is
# ---------------------------------------------------------------------------


def _seed(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_windows_filter_is_scoped_to_the_test_root(tmp_path, monkeypatch) -> None:
    """The ignore list governs ``test/`` only, so the filter must too.

    The list holds BARE filenames and ``test/conftest.py`` resolves its
    ``collect_ignore`` entries relative to its own directory -- so the exclusion
    covers ``test/<name>`` and nothing else. Matching the emitted paths on their
    basename instead also drops a same-named suite under ``transfer/`` or the
    apps-builtins tree, which no conftest excludes and Windows collects fine.
    That is a silently skipped cross-surface guard, which is the one outcome
    this selector's deny-by-default contract forbids: a heuristic mistake is
    supposed to cost CI time, never coverage.

    Loads its own selector instance rather than taking the module-scoped
    ``selector`` fixture, whose ``collect`` memo is keyed only on
    ``(surface, _is_windows())`` and would otherwise be shared with -- and
    poisoned by -- this synthetic repo root.
    """
    selector = _load_selector()
    monkeypatch.setattr(selector, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(selector, "_is_windows", lambda: True)

    _seed(
        tmp_path / "test" / "windows-collect-ignore.txt",
        "test_snapshot.py    # POSIX-only replace-while-open semantics\n",
    )
    # Both name the frontend tree, so both are cross-surface and would other-
    # wise be must-run: the only difference between them is where they live.
    guard = "assert Path('website/src/utils/sanitize.ts').is_file()\n"
    _seed(tmp_path / "test" / "test_snapshot.py", guard)
    app_suite = "src/kiro_crew/apps/builtins/demo/tests/test_snapshot.py"
    _seed(tmp_path / app_suite, guard)

    selected = selector.collect("backend")

    assert (
        "test/test_snapshot.py" not in selected
    ), "the POSIX-only suite conftest names must still be filtered out"
    assert app_suite in selected, (
        "a same-named suite outside test/ is not covered by conftest's "
        f"collect_ignore and must keep running on Windows; got {selected}"
    )


# --- container image test suite: image-only-dependency collection guard ---------
#
# The crew container image's test suite lives under
# ``src/kiro_crew/apps/builtins/aws_control/crew/runtime/container_tests`` and is
# reached by ``setup.cfg``'s ``testpaths = ... src/kiro_crew/apps/builtins``. Four
# of its modules ``import httpx`` (and one ``fastapi``/``uvicorn``) at module top
# level -- deps that ``container/requirements.txt`` marks "Container runtime only.
# These must NOT become dependencies of the Kiro Crew app itself", so the app's own
# CI env does not carry them. Its conftest therefore sets ``collect_ignore_glob`` to
# skip the suite when those collection-time deps are absent, rather than raising a
# ``ModuleNotFoundError`` at collection that cascades every backend shard. These
# pin that guard: it must skip on a missing dep, and it must NOT skip when all are
# present (or the whole suite silently stops running everywhere).

_CONTAINER_CONFTEST = (
    _REPO_ROOT
    / "src"
    / "kiro_crew"
    / "apps"
    / "builtins"
    / "aws_control"
    / "crew"
    / "runtime"
    / "container_tests"
    / "conftest.py"
)


def _os_reporting(name: str):
    """A stand-in ``os`` module whose ``name`` is *name*, for ``sys.modules``.

    ``mock.patch.object(os, "name", ...)`` cannot be used here. ``pathlib.Path.__new__``
    consults ``os.name`` on EVERY instantiation to pick ``PosixPath`` or ``WindowsPath``,
    and the conftest calls ``Path(__file__).resolve()``, so patching the real attribute
    makes that line raise ``cannot instantiate 'WindowsPath' on your system`` -- in both
    directions, since a Windows runner patched to ``posix`` fails the mirror way.

    Swapping the module that the conftest's own ``import os`` resolves to keeps the
    change inside the module under test: ``pathlib`` bound the real ``os`` object when it
    was first imported and never looks it up again.
    """
    proxy = types.ModuleType("os")
    proxy.__dict__.update(vars(os))
    # Through ``__dict__`` rather than ``proxy.name``: mypy types a ``ModuleType``
    # attribute set by the stub for the real ``os``, so the direct assignment is an
    # ``attr-defined`` error on a line that is doing exactly what it means to.
    proxy.__dict__["name"] = name
    return proxy


def _run_container_conftest(*, present: set[str], os_name: str = "posix"):
    """Load the container conftest as a module with ``find_spec`` reporting only ``present``.

    Uses the same ``spec_from_file_location`` + ``exec_module`` mechanism as
    ``_load_selector`` above, so the conftest's module-level guard runs and its
    ``collect_ignore_glob`` / ``_missing_image_deps`` can be read back. Returns the
    loaded module.
    """
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name, *a, **k):
        if name in {"fastapi", "httpx", "uvicorn", "boto3"}:
            return object() if name in present else None
        return real_find_spec(name, *a, **k)

    spec = importlib.util.spec_from_file_location(
        "container_conftest_under_test", _CONTAINER_CONFTEST
    )
    assert spec and spec.loader, "could not build an import spec for the container conftest"
    module = importlib.util.module_from_spec(spec)
    saved = importlib.util.find_spec
    try:
        importlib.util.find_spec = fake_find_spec  # type: ignore[assignment]
        # Pin the platform the conftest sees, so what a caller measures is the branch it
        # asked for. The conftest tests ``os.name`` BEFORE it tests the deps, so on a
        # Windows runner ``collect_ignore_glob`` is set whatever ``present`` says, and
        # every dep-branch assertion would be reading the platform branch's answer.
        #
        # Skipping those tests on Windows was the alternative and is worse: the
        # dependency logic is not platform-specific, so a skip stops checking a live
        # property on one platform while still scoring as a pass.
        with mock.patch.dict(sys.modules, {"os": _os_reporting(os_name)}):
            spec.loader.exec_module(module)
    finally:
        importlib.util.find_spec = saved  # type: ignore[assignment]
    return module


def test_container_suite_skipped_when_a_collect_time_dep_is_missing() -> None:
    ns = _run_container_conftest(present={"fastapi", "uvicorn"})  # httpx missing
    assert ns._missing_image_deps == ["httpx"]
    assert getattr(ns, "collect_ignore_glob", None) == ["test_*.py"], (
        "conftest must set collect_ignore_glob to skip the image suite when a "
        "collection-time dep (httpx/fastapi/uvicorn) is absent"
    )


def test_container_suite_runs_when_all_collect_time_deps_present() -> None:
    ns = _run_container_conftest(present={"fastapi", "httpx", "uvicorn"})
    assert ns._missing_image_deps == []
    assert getattr(ns, "collect_ignore_glob", None) is None, (
        "conftest must NOT skip the image suite when every collection-time dep is "
        "importable, or the whole suite stops running where its subject can run"
    )


def test_the_platform_branch_wins_over_the_dep_branch() -> None:
    """On a non-POSIX host the suite is skipped even with every dep importable.

    The two branches answer different questions -- "can this subject run here at all"
    and "are its imports satisfied" -- and the platform one has to win, because the
    image is Linux-only however complete the dev env is. Ordering them the other way
    would collect the suite's POSIX-dependent tests on Windows whenever someone had
    installed ``requirements-dev.txt`` there.
    """
    ns = _run_container_conftest(present={"fastapi", "httpx", "uvicorn"}, os_name="nt")
    assert ns._missing_image_deps == [], "the dep branch had nothing to complain about"
    assert getattr(ns, "collect_ignore_glob", None) == [
        "test_*.py"
    ], "the image suite must not be collected on a non-POSIX host, whatever its deps"


def test_boto3_is_not_a_collect_time_gate() -> None:
    """boto3 is imported lazily, so a venv without it must still run the suite.

    Both ``backup/store.py`` and ``front/transcript.py`` import boto3 inside a
    function. If boto3 were in the guard list, every AWS-free dev env would skip
    the whole suite for a dependency that never blocks import.
    """
    ns = _run_container_conftest(present={"fastapi", "httpx", "uvicorn"})  # no boto3
    assert getattr(ns, "collect_ignore_glob", None) is None


# --- container image test suite: the direct-argument twin of the glob guard -----
#
# ``collect_ignore_glob`` only filters files pytest discovers by WALKING the
# directory. A file named explicitly on the command line skips that walk, and
# pytest treats direct arguments as overriding every ignore mechanism (including
# a ``pytest_ignore_collect`` hook) -- so the conftest also substitutes a
# declining Module collector in ``pytest_pycollect_makemodule``, the one
# construction step every path to a test module shares. CI's reduced
# cross-surface path passes several of this suite's files as explicit arguments
# on every single-surface diff, which is how a frontend-only PR came to fail
# ``Backend Tests`` with ``ModuleNotFoundError: No module named 'httpx'``.
# These pin the hook's decision; the collector construction
# itself is pytest plumbing, replaced with a sentinel so no live Session is
# needed.


class _SentinelDeclined:
    """Stands in for ``_DeclinedModule`` so the hook's choice is observable."""

    @classmethod
    def from_parent(cls, parent, path):
        return ("declined", parent, path)


def test_direct_argument_collection_is_declined_when_a_dep_is_missing() -> None:
    ns = _run_container_conftest(present={"fastapi", "uvicorn"})  # httpx missing
    ns._DeclinedModule = _SentinelDeclined
    made = ns.pytest_pycollect_makemodule(
        module_path=_CONTAINER_CONFTEST.parent / "test_review_findings.py",
        parent="parent-token",
    )
    assert made == (
        "declined",
        "parent-token",
        _CONTAINER_CONFTEST.parent / "test_review_findings.py",
    ), (
        "a file named directly on the command line bypasses collect_ignore_glob, "
        "so the makemodule hook must substitute the declining collector or every "
        "frontend-only PR fails the backend shard on the image-only deps"
    )


def test_direct_argument_collection_runs_when_all_deps_present() -> None:
    ns = _run_container_conftest(present={"fastapi", "httpx", "uvicorn"})
    ns._DeclinedModule = _SentinelDeclined
    made = ns.pytest_pycollect_makemodule(
        module_path=_CONTAINER_CONFTEST.parent / "test_review_findings.py",
        parent="parent-token",
    )
    assert made is None, (
        "with every collection-time dep importable the hook must hand module "
        "construction back to pytest, or the suite stops running where its "
        "subject can run"
    )


def test_the_makemodule_hook_leaves_other_directories_alone() -> None:
    ns = _run_container_conftest(present={"fastapi", "uvicorn"})  # declined
    ns._DeclinedModule = _SentinelDeclined
    made = ns.pytest_pycollect_makemodule(
        module_path=_REPO_ROOT / "test" / "test_widget_slug.py",
        parent="parent-token",
    )
    assert made is None, (
        "the decline is scoped to the container suite's own directory; a "
        "conftest hook runs for every module under it in the tree, so an "
        "unscoped decline would skip unrelated suites"
    )


# --- container image test suite: the collection-completeness checks --------------
#
# The conftest's ``pytest_collection_modifyitems`` runs only under
# ``CREW_CONTAINER_TESTS_REQUIRED`` and answers four questions about a collection
# that already happened: did every module that defines tests yield an item, did
# every test name the source declares yield an item, did the total clear the
# floor, and is the floor still close enough to the real collection to mean
# anything. The last one exists because the first three cannot see a floor going
# stale: a suite that grows while the floor stays put reports green on a run that
# lost every test in the gap.
#
# These pin the hook's decisions with fabricated items, so each check is exercised
# on its own rather than through a real 20-second collection. The reader is pinned
# separately against synthetic modules, because what it must NOT require is the
# half that a live tree cannot demonstrate.

_CONTAINER_SUITE_DIR = _CONTAINER_CONFTEST.parent
_OMIT = object()


class _FakeItem:
    """A collected item as the hook reads one: a path, a name, an originalname."""

    def __init__(self, module: str, name: str, originalname: object = _OMIT) -> None:
        self.path = _CONTAINER_SUITE_DIR / module
        self.name = name
        if originalname is not _OMIT:
            self.originalname = originalname


def _required_conftest(*, declared: dict[str, set[str]] | None = None, floor: int | None = None):
    """The conftest loaded with the requirement ON, and its reader optionally stubbed.

    ``_REQUIRED`` is read from the environment at import and every test process
    leaves the variable unset, so the hook would return at its first line. Setting
    the module global is the same switch the lane flips, and it is read at call
    time.
    """
    ns = _run_container_conftest(present={"fastapi", "httpx", "uvicorn"})
    ns._REQUIRED = True
    if declared is not None:
        ns._declared_tests = lambda: declared
    if floor is not None:
        ns._MIN_COLLECTED = floor
    return ns


def _run_hook(ns, items: list[_FakeItem]) -> None:
    ns.pytest_collection_modifyitems(session=None, config=None, items=items)


def _items(module: str, name: str, count: int) -> list[_FakeItem]:
    """*count* parametrized items for one declared function name."""
    return [_FakeItem(module, f"{name}[{i}]", name) for i in range(count)]


def test_the_collection_floor_reds_below_its_value() -> None:
    """One test short of the floor is an error, because the floor IS the collection.

    This is the property the floor's POSITION buys and the reason it is set to the
    measured collection rather than to the collection less the margin. Set below the
    collection, the same margin would be spent hiding losses of that size instead:
    the drain this check exists to catch would pass silently up to ``_FLOOR_MARGIN``
    cases, while ordinary growth would red on the very first added test.
    """
    ns = _required_conftest(declared={"test_a.py": {"test_one"}}, floor=10)
    with pytest.raises(pytest.UsageError, match="below its floor of 10"):
        _run_hook(ns, _items("test_a.py", "test_one", 9))
    # And the margin does not soften it: the floor is a hard minimum in this direction.
    assert ns._FLOOR_MARGIN > 0, "this assertion is vacuous if the margin is already zero"


def test_the_collection_floor_passes_at_its_own_value() -> None:
    """A collection exactly ON the floor is a pass, not a failure.

    The floor is a minimum, so reading it as "more than" would red a suite that
    lost nothing, and the lane's own green run sits within a couple of tests of
    this boundary.
    """
    ns = _required_conftest(declared={"test_a.py": {"test_one"}}, floor=10)
    _run_hook(ns, _items("test_a.py", "test_one", 10))


def test_a_floor_left_behind_by_a_growing_suite_is_an_error() -> None:
    """The check that makes the floor's staleness loud instead of silent.

    One side is a hand-written constant and the other is the live collection, so
    this can fail -- and it does fail on a tree whose suite has outgrown its
    floor, which is the whole point. A floor that recomputed itself from the
    collection could never trip and would report green forever.
    """
    ns = _required_conftest(declared={"test_a.py": {"test_one"}}, floor=10)
    with pytest.raises(pytest.UsageError, match="the floor stayed behind"):
        _run_hook(ns, _items("test_a.py", "test_one", 10 + ns._FLOOR_MARGIN + 1))


def test_the_floor_may_sit_exactly_its_margin_below_the_collection() -> None:
    ns = _required_conftest(declared={"test_a.py": {"test_one"}}, floor=10)
    _run_hook(ns, _items("test_a.py", "test_one", 10 + ns._FLOOR_MARGIN))


def test_the_stale_floor_message_names_both_ways_the_margin_can_be_exceeded() -> None:
    """The message must not send a reader hunting a regression that is not there.

    A margin above zero legalises growth with no floor edit, so two branches may
    each add up to ``_FLOOR_MARGIN`` tests, clear this bound separately, and compose
    past it without either one touching the constant. The lane then reds on a commit
    that did not grow the suite, and a message naming only the same-commit cause
    misattributes it. Both causes are asserted because naming one is what makes the
    other invisible.
    """
    ns = _required_conftest(declared={"test_a.py": {"test_one"}}, floor=10)
    collected = 10 + ns._FLOOR_MARGIN + 1
    with pytest.raises(pytest.UsageError) as caught:
        _run_hook(ns, _items("test_a.py", "test_one", collected))
    message = " ".join(str(caught.value).split())
    assert "in the commit that grew the suite" in message, message
    assert "concurrently merged one did" in message, message
    # The prescribed value is the COLLECTION itself, never the collection less the
    # margin: prescribing the latter re-pins the floor at maximum staleness, which
    # spends the margin on hiding losses and leaves growth no headroom at all.
    assert f"Raise _MIN_COLLECTED to {collected}" in message, message
    assert f"Raise _MIN_COLLECTED to {collected - ns._FLOOR_MARGIN}" not in message, message


def test_a_module_that_defines_tests_and_yields_nothing_is_an_error() -> None:
    ns = _required_conftest(
        declared={"test_a.py": {"test_one"}, "test_gone.py": {"test_two"}}, floor=1
    )
    with pytest.raises(pytest.UsageError, match="no collected test: test_gone.py"):
        _run_hook(ns, _items("test_a.py", "test_one", 3))


def test_a_declared_test_that_yields_no_item_is_an_error() -> None:
    """The shape a module-level presence check cannot see.

    The module is collected and contributes items, so the presence check is
    satisfied; one name the source declares produced nothing. A decorator that
    returns a non-function, a class body an import guard emptied, and a name
    shadowed by a later definition all land here.
    """
    ns = _required_conftest(declared={"test_a.py": {"test_one", "test_swallowed"}}, floor=1)
    with pytest.raises(pytest.UsageError, match=r"test_a\.py::test_swallowed"):
        _run_hook(ns, _items("test_a.py", "test_one", 3))


def test_a_parametrized_item_counts_under_the_function_that_declares_it() -> None:
    """Case ids must not make a declared name look absent.

    ``test_one[0]`` is not a name the source declares, so matching on ``name``
    would report every parametrized test as missing. The function's own name is
    on ``originalname``; an item that carries neither falls back to the part of
    the name before the case id.
    """
    ns = _required_conftest(declared={"test_a.py": {"test_one", "test_bare"}}, floor=4)
    _run_hook(
        ns,
        _items("test_a.py", "test_one", 3) + [_FakeItem("test_a.py", "test_bare[x]")],
    )


def test_items_outside_the_suite_directory_are_not_judged() -> None:
    """A wider run that sweeps this suite in must not be measured by its floor."""
    ns = _required_conftest(declared={}, floor=1)
    stranger = _FakeItem("test_a.py", "test_one", "test_one")
    stranger.path = _REPO_ROOT / "test" / "test_widget_slug.py"
    with pytest.raises(pytest.UsageError, match="below its floor of 1"):
        _run_hook(ns, [stranger])


def test_the_hook_is_silent_without_the_requirement() -> None:
    """Every check above is gated: a developer's own run is never judged by them."""
    with mock.patch.dict(os.environ):
        os.environ.pop("CREW_CONTAINER_TESTS_REQUIRED", None)
        ns = _run_container_conftest(present={"fastapi", "httpx", "uvicorn"})
    assert ns._REQUIRED is False, "an unset variable must leave the requirement off"
    ns._declared_tests = lambda: {"test_gone.py": {"test_two"}}
    _run_hook(ns, [])


def test_the_reader_excludes_names_pytest_cannot_collect(tmp_path: Path) -> None:
    """What the reader must NOT require, which a live tree cannot demonstrate.

    Requiring an item for a name pytest never collects is a red with nothing
    wrong behind it, so the scan is scoped to the two places pytest looks: a
    module-level function, and a method of a class whose name matches
    ``python_classes``. A helper nested inside another function and a method of a
    plain helper class are both named ``test*`` and neither is collected.
    """
    (tmp_path / "test_shapes.py").write_text(
        "def test_module_level():\n"
        "    pass\n"
        "\n"
        "\n"
        "async def test_async_module_level():\n"
        "    pass\n"
        "\n"
        "\n"
        "class TestGroup:\n"
        "    def test_method(self):\n"
        "        pass\n"
        "\n"
        "\n"
        "class Helper:\n"
        "    def test_not_a_pytest_class(self):\n"
        "        pass\n"
        "\n"
        "\n"
        "def build():\n"
        "    def test_nested():\n"
        "        pass\n"
        "\n"
        "    return test_nested\n",
        encoding="utf-8",
    )
    ns = _run_container_conftest(present={"fastapi", "httpx", "uvicorn"})
    ns._HERE = tmp_path
    declared = ns._declared_tests()
    assert declared == {
        "test_shapes.py": {"test_module_level", "test_async_module_level", "test_method"}
    }, (
        "the reader must name exactly what pytest's default collection reaches: a "
        "nested definition and a method of a non-Test class are not collected, so "
        "requiring an item for either reds a correct tree"
    )


def test_a_module_whose_tests_pytest_cannot_reach_is_still_required_to_yield_one(
    tmp_path: Path,
) -> None:
    """Presence stays the loose question even though the names are the strict one.

    A module whose only ``test*`` definition is somewhere pytest does not look has
    no names to require, and it must still be required to yield SOMETHING -- that
    is the check that catches a module dropping out of collection entirely, and a
    module must not escape it by holding its tests in an unusual place.
    """
    (tmp_path / "test_odd.py").write_text(
        "def build():\n    def test_nested():\n        pass\n\n    return test_nested\n",
        encoding="utf-8",
    )
    (tmp_path / "test_empty.py").write_text("X = 1\n", encoding="utf-8")
    ns = _run_container_conftest(present={"fastapi", "httpx", "uvicorn"})
    ns._HERE = tmp_path
    declared = ns._declared_tests()
    assert declared == {"test_odd.py": set()}, (
        "a module defining a test anywhere is required to yield an item with no name "
        "pinned; a module defining none at all is not required to yield anything"
    )


def test_the_margin_is_smaller_than_the_smallest_module() -> None:
    """The floor's margin is derived from the tree, not chosen for comfort.

    The margin is growth headroom, so its bound is about what may land WITHOUT the
    floor being raised. A margin at or above the smallest module's test count would
    let a whole new module arrive while the constant stays put, and the floor would
    resume drifting by exactly the mechanism this file exists to stop. Losing tests
    is not what the margin governs -- a collection below the floor reds at any size.
    """
    ns = _run_container_conftest(present={"fastapi", "httpx", "uvicorn"})
    per_module = {name: len(names) for name, names in ns._declared_tests().items() if names}
    assert per_module, "the container suite's own modules must be readable from source"
    smallest = min(per_module.values())
    assert ns._FLOOR_MARGIN < smallest, (
        f"the floor's margin is {ns._FLOOR_MARGIN} and the smallest module declares "
        f"{smallest} tests, so a whole module could be added without raising the floor"
    )


def test_the_suite_has_a_module_that_declares_no_test() -> None:
    """The reason the presence check is read from source rather than from the glob.

    ``test_supervisor_fakes.py`` matches ``test_*.py`` to sit inside one track's
    ownership and declares no test. Requiring every matching file to yield an item
    would fail on the tree as it stands, which is why the check asks the source
    what it defines.
    """
    ns = _run_container_conftest(present={"fastapi", "httpx", "uvicorn"})
    files = {path.name for path in _CONTAINER_SUITE_DIR.glob("test_*.py")}
    required = set(ns._declared_tests())
    assert files - required == {"test_supervisor_fakes.py"}, (
        "a file matching test_*.py that declares no test must not be required to "
        "yield an item; a tree where every matching file declares a test leaves this "
        "exemption unexercised and the pin stops measuring it"
    )
