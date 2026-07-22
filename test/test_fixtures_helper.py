"""Tests for ``kiro_crew.testing.fixtures``.

Covers the plain context manager and the pytest fixture, plus a
byte-for-byte check that the helper produces exactly the shipped
fixture tree (guard against ``copytree`` mode/attribute drift).
"""

from __future__ import annotations

import filecmp
import os
from pathlib import Path

import pytest

from kiro_crew import seed as seed_mod
from kiro_crew.testing.fixtures import seeded_home

# Register ``seeded_home_fixture`` as a pytest plugin rather than importing
# it directly. Importing the fixture would shadow the test-function parameter
# of the same name (flake8 F811), and renaming the parameter would break
# pytest's name-based fixture binding. ``pytest_plugins`` is the sanctioned
# way to pull fixtures into a test module without a name collision.
pytest_plugins = ["kiro_crew.testing.fixtures"]

# Fixture root used by the helper — one lookup, not repeated in every
# test, so a future move of the fixtures dir is a one-line change here.
_FIXTURES = seed_mod._fixtures_root()


# ── Plain context manager ────────────────────────────────────────────


@pytest.mark.parametrize("fixture_name", ["empty", "minimal", "rich"])
def test_seeded_home_yields_populated_path(fixture_name: str) -> None:
    """Helper yields a real path with fixture.yaml present for all fixtures."""
    with seeded_home(fixture_name) as home:
        assert home.is_dir()
        assert (home / "fixture.yaml").is_file()


@pytest.mark.parametrize("fixture_name", ["empty", "minimal", "rich"])
def test_seeded_home_byte_for_byte_match(fixture_name: str) -> None:
    """Seeded tree is byte-identical to the shipped fixture tree.

    Catches ``copytree`` attribute drift (permissions, xattrs) or any
    future helper that decides to "enrich" fixtures at seed time. The
    contract is: what you ship is what callers get, bit for bit.
    """
    src = _FIXTURES / fixture_name
    with seeded_home(fixture_name) as home:
        cmp = filecmp.dircmp(str(src), str(home))
        # ``diff_files`` = same name, different content. ``left_only`` /
        # ``right_only`` = name present in only one tree. ``funny_files``
        # = couldn't compare (permissions/read errors). All must be
        # empty for byte-for-byte equality.
        assert cmp.diff_files == [], f"differing files: {cmp.diff_files}"
        assert cmp.left_only == [], f"missing in seeded tree: {cmp.left_only}"
        assert cmp.right_only == [], f"extra in seeded tree: {cmp.right_only}"
        assert cmp.funny_files == [], f"uncomparable: {cmp.funny_files}"
        # Recurse into subdirs to cover nested content
        # (workspace/memory/history/, sessions/archive/ for rich).
        _assert_subtree_identical(cmp)


def _assert_subtree_identical(cmp: filecmp.dircmp) -> None:
    """Recursive byte-for-byte guard for nested subtrees."""
    for sub in cmp.subdirs.values():
        assert sub.diff_files == [], f"differing in subdir: {sub.diff_files}"
        assert sub.left_only == [], f"missing in subdir: {sub.left_only}"
        assert sub.right_only == [], f"extra in subdir: {sub.right_only}"
        _assert_subtree_identical(sub)


def test_seeded_home_restores_previous_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exiting the context manager restores the prior KIROCREW_HOME."""
    monkeypatch.setenv("KIROCREW_HOME", "/tmp/sentinel-previous")
    with seeded_home("empty") as home:
        # Inside: env points at the tempdir, not the sentinel.
        assert os.environ["KIROCREW_HOME"] == str(home)
    # After: sentinel is back.
    assert os.environ["KIROCREW_HOME"] == "/tmp/sentinel-previous"


def test_seeded_home_unsets_env_when_not_previously_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exiting unsets KIROCREW_HOME when it wasn't set on entry."""
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    with seeded_home("empty"):
        assert "KIROCREW_HOME" in os.environ
    assert "KIROCREW_HOME" not in os.environ


def test_seeded_home_cleans_up_tempdir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tempdir is removed on context-manager exit."""
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    with seeded_home("empty") as home:
        captured = home
        assert captured.is_dir()
    assert not captured.exists()


def test_seeded_home_propagates_unknown_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown fixture name raises ``SeedError`` (guardrails still apply)."""
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    with pytest.raises(seed_mod.SeedError) as excinfo:
        with seeded_home("not-a-real-fixture"):
            pytest.fail("context manager must not yield on bad fixture")
    assert excinfo.value.guardrail == seed_mod.SeedError.GUARDRAIL_UNKNOWN_FIXTURE


def test_seeded_home_propagates_path_traversal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path-traversal attempts are rejected by the seed-side guardrail.

    Prevents callers from using the helper to bypass ``_resolve_fixture``
    and land outside ``tests_fixtures/``.
    """
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    with pytest.raises(seed_mod.SeedError) as excinfo:
        with seeded_home("../../.ssh"):
            pytest.fail("context manager must not yield on traversal")
    assert excinfo.value.guardrail == seed_mod.SeedError.GUARDRAIL_BAD_NAME


def test_seeded_home_restores_env_on_seed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env var is still restored when seed() raises inside the CM.

    Guards the ``finally`` branch — a bare ``try:`` with ``yield`` would
    leave ``KIROCREW_HOME`` dangling if seed() raised after we set it.
    """
    monkeypatch.setenv("KIROCREW_HOME", "/tmp/sentinel-restore-on-fail")
    with pytest.raises(seed_mod.SeedError):
        with seeded_home("not-a-real-fixture"):
            pytest.fail("unreachable")
    assert os.environ["KIROCREW_HOME"] == "/tmp/sentinel-restore-on-fail"


# ── Pytest fixture ──────────────────────────────────────────────────


def test_seeded_home_fixture_default_empty(seeded_home_fixture: Path) -> None:
    """Direct use without indirect param defaults to the 'empty' fixture."""
    assert seeded_home_fixture.is_dir()
    assert (seeded_home_fixture / "fixture.yaml").is_file()
    # 'empty' only ships fixture.yaml — any other file means we got the
    # wrong default.
    files = [p for p in seeded_home_fixture.rglob("*") if p.is_file()]
    assert len(files) == 1, f"empty fixture should ship one file, got {files}"


@pytest.mark.parametrize(
    "seeded_home_fixture",
    ["minimal", "rich"],
    indirect=True,
)
def test_seeded_home_fixture_indirect_param(seeded_home_fixture: Path) -> None:
    """Indirect parameterisation wires through to the context manager."""
    assert (seeded_home_fixture / "fixture.yaml").is_file()
    assert (seeded_home_fixture / "config.json").is_file()
    # Both populated fixtures have a default workspace.
    assert (seeded_home_fixture / "workspace" / "memory").is_dir()


@pytest.mark.parametrize("fixture_name", ["minimal", "rich"])
def test_seeded_home_populated_fixture_has_extra_workspace(
    fixture_name: str,
) -> None:
    """minimal and rich both ship workspace-extra/ for multi-workspace coverage.

    rich inherits its workspace layout from minimal unchanged — both
    must populate the default workspace's history directory and the
    secondary ``workspace-extra/`` tree.
    """
    with seeded_home(fixture_name) as home:
        assert (home / "workspace-extra" / "memory" / "preferences.md").is_file()
        assert (home / "workspace" / "memory" / "history").is_dir()
        # History dir ships one dated file.
        dated = list((home / "workspace" / "memory" / "history").iterdir())
        assert len(dated) == 1
        assert dated[0].name == "2026-01-15.md"


@pytest.mark.parametrize("fixture_name", ["minimal", "rich"])
def test_seeded_home_populated_fixture_has_starter_session(
    fixture_name: str,
) -> None:
    """minimal and rich both ship a pinned starter chat session.

    The dashboard lists sessions by ``glob("*.jsonl")`` on
    ``$KIROCREW_HOME/sessions/``, so shipping a well-formed JSONL file
    there makes the sessions tab non-empty on first boot — the headline
    value of ``kirocrew gateway --seed <fixture>`` for e2e dashboard
    testing.

    Asserts the starter file exists (by name, since rich ships several
    JSONL files), the first line is a valid metadata record, and every
    subsequent line parses as a message. Also asserts the metadata
    contract required for ``restore_recent_sessions`` to promote the
    starter to an active slot on gateway boot (``closed=False`` and
    ``pinned=True``) — without this, the starter lands on disk but the
    user only sees it inside the collapsed History panel, defeating the
    fixture's purpose.
    """
    import json

    with seeded_home(fixture_name) as home:
        sessions_dir = home / "sessions"
        assert sessions_dir.is_dir(), f"{fixture_name} must ship sessions/ directory"
        # Look up by name — rich ships multiple JSONL files, so
        # ``glob("*.jsonl")[0]`` is not deterministic.
        starter = sessions_dir / "dashboard_starter.jsonl"
        assert starter.is_file(), (
            f"{fixture_name} must ship sessions/dashboard_starter.jsonl"
        )

        lines = starter.read_text(encoding="utf-8").splitlines()
        assert len(lines) >= 2, "session must have metadata + at least one message"

        # First line must be a metadata record.
        meta = json.loads(lines[0])
        assert meta.get("_type") == "metadata", "first line must be metadata"
        assert "title" in meta, "metadata must carry a title"
        assert "created_at" in meta, "metadata must carry created_at"

        # Restoration contract — keep in sync with
        # ``kiro_crew.dashboard.chat_persistence.restore_recent_sessions``:
        #  - ``closed=True`` short-circuits the restore loop (line ~85)
        #  - absent ``pinned``/``folder_id`` falls through to the 30-min
        #    ``modified`` cutoff; ``shutil.copy2`` preserves mtime from the
        #    fixture source, so anyone seeding >30 min after clone would
        #    see the file on disk but not as an active slot.
        # Pinning bypasses the cutoff deterministically.
        assert meta.get("closed") is False, (
            "starter must have closed=False so restore_recent_sessions promotes it to a slot"
        )
        assert meta.get("pinned") is True, (
            "starter must be pinned so it survives the 30-min mtime cutoff "
            "(copytree preserves fixture mtime; pinned bypasses the cutoff check)"
        )

        # Remaining lines are messages.
        for i, line in enumerate(lines[1:], start=2):
            msg = json.loads(line)
            assert msg.get("role") in ("user", "assistant"), (
                f"line {i}: role must be user or assistant, got {msg.get('role')!r}"
            )
            assert msg.get("content"), f"line {i}: content must be non-empty"


def test_seeded_home_rich_has_multi_session_coverage() -> None:
    """rich ships >= 3 slot-eligible sessions + 1 folder + 1 archive slice.

    The headline for ``rich`` (vs ``minimal``) is a fully-populated
    dashboard: Sessions panel with multiple active slots, a folder
    grouping, a closed session visible in History, and a non-empty
    archive/ directory. These assertions pin that contract so a future
    fixture refactor that reverts ``rich`` to a single-session layout
    fails loudly here instead of silently regressing demo realism.
    """
    import json

    with seeded_home("rich") as home:
        sessions_dir = home / "sessions"
        # At least 3 slot-eligible sessions (non-archive JSONL files).
        jsonl_files = [
            p for p in sessions_dir.glob("*.jsonl") if not p.is_symlink()
        ]
        assert len(jsonl_files) >= 3, (
            f"rich must ship >= 3 sessions for slot-coverage demo, "
            f"got {len(jsonl_files)}: {[p.name for p in jsonl_files]}"
        )

        # Folder definition for the sidebar's folder tree.
        folders_path = home / "folders.json"
        assert folders_path.is_file(), "rich must ship folders.json"
        folders = json.loads(folders_path.read_text(encoding="utf-8"))
        assert isinstance(folders, list) and len(folders) >= 1, (
            "rich must ship at least one folder definition"
        )
        folder_ids = {f.get("id") for f in folders}
        # At least one session must reference the seeded folder(s) so the
        # sidebar's folder tree isn't empty after restore.
        sessions_in_folder = 0
        for p in jsonl_files:
            first = p.read_text(encoding="utf-8").splitlines()[0]
            meta = json.loads(first)
            if meta.get("folder_id") in folder_ids:
                sessions_in_folder += 1
        assert sessions_in_folder >= 1, (
            "rich must ship >= 1 session referencing a folder_id from folders.json"
        )

        # Archive retention path is non-empty — demonstrates the
        # sessions/archive/ rotation directory in the seeded state.
        archive_dir = sessions_dir / "archive"
        assert archive_dir.is_dir(), "rich must ship sessions/archive/ directory"
        archive_files = list(archive_dir.glob("*.jsonl"))
        assert len(archive_files) >= 1, (
            "rich must ship at least one archive slice to exercise the archive UI"
        )


# ── pytest-optional contract ────────────────────────────────────────


def test_seeded_home_importable_without_pytest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``seeded_home`` must be importable when pytest is unavailable.

    Regression guard (rev-1 review-bot finding #1): the runtime
    wheel ships ``kiro_crew.testing`` without pytest in its dependencies,
    so any hard ``import pytest`` at module top breaks the contract that
    ``from kiro_crew.testing import seeded_home`` works from any test
    harness.

    Simulates pytest-absent by hiding ``pytest`` from ``sys.modules`` and
    importing a FRESH copy of the module into a throwaway name. Using
    ``importlib.reload`` on the cached module does NOT work because
    reload preserves attributes set on prior loads — the
    ``seeded_home_fixture`` symbol from the original (pytest-available)
    load lingers on the module object.
    """
    import importlib
    import sys

    # Hide pytest: make ``import pytest`` raise ImportError in a fresh
    # module body. Setting sys.modules["pytest"] = None is the canonical
    # incantation for this.
    monkeypatch.setitem(sys.modules, "pytest", None)  # type: ignore[arg-type]
    # Force a fresh load by removing any cached copy of the target module.
    monkeypatch.delitem(sys.modules, "kiro_crew.testing.fixtures", raising=False)
    monkeypatch.delitem(sys.modules, "kiro_crew.testing", raising=False)

    fresh = importlib.import_module("kiro_crew.testing.fixtures")

    # Context-manager entry point still exposed.
    assert hasattr(fresh, "seeded_home")
    assert "seeded_home" in fresh.__all__
    # Pytest fixture NOT defined when pytest is unavailable.
    assert not hasattr(fresh, "seeded_home_fixture"), (
        "seeded_home_fixture leaked into pytest-absent build"
    )
    assert "seeded_home_fixture" not in fresh.__all__

    # Smoke: the CM still works end-to-end.
    with fresh.seeded_home("empty") as home:
        assert (home / "fixture.yaml").is_file()
