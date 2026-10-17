"""The gate that stops a real-adapter lane from passing on a skip.

``real_adapter_gate`` exists for one outcome: a lane that installs an adapter in
order to measure it must go RED when the adapter is missing, while the same test on
a developer's box still skips. That is one branch in each direction, and the branch
nobody exercises by accident is the failing one -- so it is pinned here.

The rest of this file is the part the lane cannot check about itself. The lane
selects tests by the ``real_adapter`` marker and collects two FILES, so three things
have to hold for "no contract goes unrun" to stay true: every test that consults the
gate carries the marker, every FILE that consults the gate is in the lane's
collection scope, and the pins the lane installs are the ones these tests measured.
Each is read off the tree and the workflow here, where a violation is one red test
rather than a silently narrower lane.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from real_adapter_gate import (
    CODEX_ACP_PACKAGE,
    LOCKFILE,
    MANIFEST,
    MARKER,
    MEASURED_CODEX_ACP_VERSION,
    MEASURED_OPENCODE_VERSION,
    OPENCODE_PACKAGE,
    REQUIRE_ENV,
    pinned_version,
    real_adapters_required,
    require_real_adapter,
)

ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = ROOT / "test"
WORKFLOW = ROOT / ".github/workflows/ci.yml"


def test_an_absent_adapter_skips_when_nothing_requires_it(monkeypatch):
    """The local default: a developer without the adapter gets the skip, by name."""
    monkeypatch.delenv(REQUIRE_ENV, raising=False)
    assert real_adapters_required() is False
    with pytest.raises(pytest.skip.Exception) as raised:
        require_real_adapter(None, what="codex-acp", install="npm i -g codex-acp")
    assert "codex-acp is not installed" in str(raised.value)
    assert "npm i -g codex-acp" in str(raised.value)


def test_an_absent_adapter_fails_the_job_that_declares_it_required(monkeypatch):
    """THE point of the switch: the lane reds instead of reporting a green skip.

    Without this branch the lane installs an adapter, the install quietly fails,
    every guarded test skips, and the job passes having measured nothing -- the exact
    shape the lane exists to end.
    """
    monkeypatch.setenv(REQUIRE_ENV, "1")
    assert real_adapters_required() is True
    with pytest.raises(pytest.fail.Exception) as raised:
        require_real_adapter(None, what="opencode", install="npm i -g opencode-ai@1.2.3")
    message = str(raised.value)
    assert "opencode is not installed" in message
    assert REQUIRE_ENV in message
    assert "npm i -g opencode-ai@1.2.3" in message


def test_a_resolved_adapter_is_gated_by_neither_setting(monkeypatch):
    """Present is a no-op, so the gate adds nothing to the measurement itself."""
    for value in ("", "1"):
        monkeypatch.setenv(REQUIRE_ENV, value)
        assert require_real_adapter("/opt/bin/opencode", what="o", install="i") is None


@pytest.mark.parametrize("value", ["", "0", "true", "yes", "on", " 1 ", "2"])
def test_only_the_exact_switch_value_requires_an_adapter(monkeypatch, value):
    """Parsed as the repository's other readers parse it, and safe in that direction.

    ``require`` is a POSITIVE switch, so anything other than the one spelling leaves
    it OFF: the fallback is a skip a reader can act on, never a red on a box that
    never opted in. The lane does not lean on this parse -- it asserts its own junit
    report carries no skipped case -- so the safe direction costs the lane nothing.
    """
    monkeypatch.setenv(REQUIRE_ENV, value)
    assert real_adapters_required() is False
    with pytest.raises(pytest.skip.Exception):
        require_real_adapter(None, what="codex-acp", install="npm i")


def test_the_switch_is_the_one_the_rest_of_the_repository_already_reads():
    """One name for one mechanism, rather than a second spelling of it."""
    assert REQUIRE_ENV == "KIROCREW_E2E_REQUIRE"
    sibling = (TEST_DIR / "e2e/scenarios/conftest.py").read_text(encoding="utf-8")
    assert REQUIRE_ENV in sibling


def _guarded_tests() -> dict[Path, dict[str, list[str]]]:
    """Every test under ``test/`` that reaches the gate, with its marker names.

    Read from the tree rather than from a list, because a list is the thing that
    goes stale: a contract test added later has to appear here by being written, not
    by being remembered.
    """
    found: dict[Path, dict[str, list[str]]] = {}
    for path in sorted(TEST_DIR.rglob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        if "real_adapter_gate" not in source or path.name == Path(__file__).name:
            continue
        tree = ast.parse(source)
        # A file's own one-line wrappers around the gate count as reaching it.
        reaching = {"require_real_adapter"}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and any(
                isinstance(call.func, ast.Name) and call.func.id in reaching
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
            ):
                reaching.add(node.name)
        tests: dict[str, list[str]] = {}
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
                continue
            calls = {
                call.func.id
                for call in ast.walk(node)
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            }
            if not calls & reaching:
                continue
            tests[node.name] = [ast.unparse(decorator) for decorator in node.decorator_list]
        if tests:
            found[path] = tests
    return found


def test_the_tree_carries_the_guarded_tests_this_lane_exists_for():
    """A floor under the sweep below, so it cannot pass by finding nothing."""
    guarded = _guarded_tests()
    total = sum(len(tests) for tests in guarded.values())
    assert total >= 4, guarded


def test_every_guarded_test_carries_the_marker_the_lane_selects_on():
    """A guarded test without the marker is a contract the lane silently omits.

    That is this PR's own failure shape recurring for additions: the test exists, it
    skips everywhere, and nothing is red about it.
    """
    missing = {
        f"{path.relative_to(ROOT)}::{name}"
        for path, tests in _guarded_tests().items()
        for name, decorators in tests.items()
        if not any(MARKER in decorator for decorator in decorators)
    }
    assert not missing, f"add @pytest.mark.{MARKER}: {sorted(missing)}"


def test_no_guarded_test_carries_a_skip_the_lane_cannot_see():
    """A ``skipif`` cannot fail, so it restores the silence the gate removes."""
    for path in _guarded_tests():
        source = path.read_text(encoding="utf-8")
        for absence in ("skipif(_ENTRY is None", "skipif(_BIN is None"):
            assert absence not in source, f"{path.relative_to(ROOT)} carries {absence}"


def test_the_lane_collects_every_file_that_consults_the_gate():
    """The lane's collection scope is two files; this is what keeps it complete.

    Collecting all of ``test/`` costs minutes for nothing, so the lane names files.
    A guarded test in a THIRD file would then be marked, correct, and never run -- so
    the list is checked against the tree here instead of being trusted.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    lane = workflow.split("real-adapter-contract:", 1)[1].split("\n  coverage-combine:", 1)[0]
    collected = set(re.findall(r"(test/[\w/]+\.py)", lane))
    # POSIX form on every OS: the workflow spells its paths that way, and a Windows
    # shard compares against it with backslashes otherwise.
    expected = {path.relative_to(ROOT).as_posix() for path in _guarded_tests()}
    assert expected <= collected, f"not collected by the lane: {sorted(expected - collected)}"


def test_the_lane_installs_from_the_committed_lockfile_and_copies_no_pin():
    """One pin, in the manifest; the lane installs its lockfile and reads nothing else.

    ``npm ci`` in the manifest's directory is what freezes the transitive tree, and
    a literal version anywhere in the workflow is what these assertions refuse: two
    pins drift, and the drift is invisible -- the lane measures one release while
    the assertions name another.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    lane = workflow.split("real-adapter-contract:", 1)[1].split("\n  coverage-combine:", 1)[0]
    commands = [line for line in lane.splitlines() if not line.lstrip().startswith("#")]
    assert any("npm ci" in line for line in commands)
    assert not any(
        "npm install" in line for line in commands
    ), "the lane must install from the lockfile, never ad hoc"
    assert "working-directory: test/real_adapters" in lane
    assert "cache-dependency-path: test/real_adapters/package-lock.json" in lane
    assert "real_adapter_gate" in lane
    assert MEASURED_CODEX_ACP_VERSION not in workflow
    assert MEASURED_OPENCODE_VERSION not in workflow


def test_the_manifest_pins_exact_releases_and_the_lockfile_agrees():
    """A pin is exact, and the lockfile records the same release.

    A range or a moving tag is not something to re-measure against, and a manifest
    edited without its lockfile would make ``npm ci`` refuse -- or, worse, install
    the lockfile's older release while the gate names the newer one.
    """
    import json

    for package, pin in (
        (CODEX_ACP_PACKAGE, MEASURED_CODEX_ACP_VERSION),
        (OPENCODE_PACKAGE, MEASURED_OPENCODE_VERSION),
    ):
        assert pinned_version(package) == pin
        assert len(pin.split(".")) == 3 and pin.replace(".", "").isdigit(), pin
        locked = json.loads(LOCKFILE.read_text(encoding="utf-8"))["packages"]
        assert locked[f"node_modules/{package}"]["version"] == pin, package


def test_a_range_in_the_manifest_is_refused(tmp_path, monkeypatch):
    """The reader refuses anything but an exact release, so a caret cannot ride in."""
    import json

    import real_adapter_gate

    manifest = tmp_path / "package.json"
    manifest.write_text(json.dumps({"dependencies": {"x": "^1.2.3"}}), encoding="utf-8")
    monkeypatch.setattr(real_adapter_gate, "MANIFEST", manifest)
    with pytest.raises(ValueError, match="not an exact release"):
        real_adapter_gate.pinned_version("x")


def test_the_manifest_is_not_the_product_s_and_dependabot_refreshes_it():
    """Two properties of WHERE the pin lives.

    It is not under ``website/``, so the frontend and build lanes never install the
    adapters; and Dependabot watches its directory weekly, so a new release arrives
    as its own pull request -- the run of the lane that shows adapter drift.
    """
    assert MANIFEST.parent.name == "real_adapters" and MANIFEST.parent.parent.name == "test"
    assert "website" not in MANIFEST.parts
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest.get("private") is True
    assert set(manifest["dependencies"]) == {CODEX_ACP_PACKAGE, OPENCODE_PACKAGE}

    dependabot = (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
    assert 'package-ecosystem: "npm"' in dependabot
    assert 'directory: "/test/real_adapters"' in dependabot


def test_the_marker_is_registered_so_a_typo_is_not_a_silent_deselect():
    """An unregistered marker is a filter that quietly matches nothing."""
    cfg = (ROOT / "setup.cfg").read_text(encoding="utf-8")
    assert f"\n    {MARKER}: " in cfg
