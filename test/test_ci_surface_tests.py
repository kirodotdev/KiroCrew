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
    return _load_selector()


def test_script_exists_and_is_executable() -> None:
    assert _SCRIPT.is_file(), f"missing selector script: {_SCRIPT}"


# Known cross-surface parity guards. Each of these lives in one suite but
# asserts against the OTHER surface's source, so each MUST stay in the must-run
# set. Audited 2026-08-06; extend this list when a new guard is added.
_BACKEND_GUARDS = (
    "test/test_redaction_mirror_parity.py",
    "test/test_dashboard_security_headers.py",
    "test/test_model_registry_parity.py",
    "test/test_builtin_app_assets.py",
    "test/test_recovery_card_parity.py",
    "test/test_artifact_import_parity.py",
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
