"""A scenario plane is reclaimed when setup fails before any CLI call."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from e2e.scenarios import conftest as scenarios

from kiro_crew.pod import runtime
from kiro_crew.testing import harness


@pytest.fixture
def plane_setup(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    (root / "src" / "kiro_crew" / "static" / "dist").mkdir(parents=True)
    basetemp = tmp_path / "pytest-base"
    basetemp.mkdir()
    # Outside the factory's basetemp, but still owned by this test's tmp_path.
    scratch = tmp_path / "plane"
    monkeypatch.setenv("KIROCREW_E2E_SCENARIOS", "1")
    monkeypatch.delenv("KIROCREW_E2E_REQUIRE", raising=False)
    monkeypatch.delenv("KIROCREW_E2E_SCENARIOS_REAL_AGENT", raising=False)
    monkeypatch.setattr(runtime, "require_backend", Mock())
    monkeypatch.setattr(scenarios, "_repo_root", lambda: root)
    monkeypatch.setattr(scenarios.prov, "venv_bin", lambda _root: Path(sys.executable))
    monkeypatch.setattr(scenarios, "_plane_root", lambda _name, _base: scratch)
    cli = Mock(side_effect=AssertionError("preconditions must not invoke the CLI"))
    remove_unit = Mock(side_effect=AssertionError("preconditions must not touch services"))
    monkeypatch.setattr(scenarios, "_run_cli", cli)
    monkeypatch.setattr(scenarios, "_remove_plane_unit_template", remove_unit)
    return SimpleNamespace(
        scratch=scratch,
        factory=SimpleNamespace(getbasetemp=lambda: basetemp),
        cli=cli,
        remove_unit=remove_unit,
    )


@pytest.mark.parametrize("required", [False, True], ids=["optional-skip", "required-fail"])
def test_missing_real_backend_reclaims_plane(plane_setup, monkeypatch, required):
    monkeypatch.setenv("KIROCREW_E2E_SCENARIOS_REAL_AGENT", "1")
    monkeypatch.setenv("KIROCREW_E2E_REQUIRE", "1" if required else "0")
    monkeypatch.setattr(scenarios.shutil, "which", lambda _name: None)
    outcome = pytest.fail.Exception if required else pytest.skip.Exception

    with pytest.raises(outcome, match="no `kiro-cli` on PATH"):
        next(scenarios.pod.__wrapped__(plane_setup.factory))

    plane_setup.cli.assert_not_called()
    plane_setup.remove_unit.assert_not_called()
    assert not plane_setup.scratch.exists()


@pytest.mark.parametrize("stage", ["marker", "launcher", "environment"])
def test_setup_error_reclaims_plane_and_propagates(plane_setup, monkeypatch, stage):
    error = OSError(f"{stage} setup failed")

    def fail_setup(*_args, **_kwargs):
        assert plane_setup.scratch.is_dir()
        raise error

    if stage == "marker":
        write_text = Path.write_text

        def write_marker(path, *args, **kwargs):
            if path == plane_setup.scratch / scenarios._PLANE_MARKER:
                return fail_setup()
            return write_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", write_marker)
    elif stage == "launcher":
        monkeypatch.setattr(harness, "fake_acp_backend_launcher", fail_setup)
    else:
        monkeypatch.setattr(scenarios, "_resolve_backend", lambda _scratch: None)
        monkeypatch.setattr(scenarios, "_plane_env", fail_setup)

    with pytest.raises(OSError) as caught:
        next(scenarios.pod.__wrapped__(plane_setup.factory))

    assert caught.value is error
    plane_setup.cli.assert_not_called()
    plane_setup.remove_unit.assert_not_called()
    assert not plane_setup.scratch.exists()


def test_preexisting_plane_is_preserved(plane_setup, monkeypatch):
    scratch = plane_setup.scratch
    scratch.mkdir()
    marker = scratch / scenarios._PLANE_MARKER
    marker.write_text("another owner", encoding="ascii")
    home = scratch / "h"
    home.mkdir()
    sentinel = home / "keep.txt"
    sentinel.write_text("preserve home", encoding="utf-8")
    backend = Mock(side_effect=AssertionError("must refuse before resolving the backend"))
    monkeypatch.setattr(scenarios, "_resolve_backend", backend)

    with pytest.raises(FileExistsError):
        next(scenarios.pod.__wrapped__(plane_setup.factory))

    backend.assert_not_called()
    plane_setup.cli.assert_not_called()
    plane_setup.remove_unit.assert_not_called()
    assert marker.read_text(encoding="ascii") == "another owner"
    assert sentinel.read_text(encoding="utf-8") == "preserve home"
