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
import re
from pathlib import Path

import pytest

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
# set. Audited 2026-08-06; extend this list when a new guard is added.
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
        ln.split("#", 1)[0].strip()
        for ln in _IGNORE_LIST.read_text(encoding="utf-8").splitlines()
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
    """
    assert _ignore_names() == {
        "test_harness.py",
        "test_sandbox_argv.py",
        "test_sandbox_cc_mode.py",
        "test_sandbox_hardlink_scan.py",
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
    import subprocess
    import sys

    suite = tmp_path / "t"
    suite.mkdir()
    (suite / "conftest.py").write_text('collect_ignore = ["test_boom.py"]\n', encoding="utf-8")
    (suite / "test_boom.py").write_text('raise RuntimeError("import-time failure")\n', encoding="utf-8")
    (suite / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    def run(*target: str) -> int:
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "-p", "no:randomly", "--no-cov", *target],
            cwd=tmp_path, capture_output=True, text=True,
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
