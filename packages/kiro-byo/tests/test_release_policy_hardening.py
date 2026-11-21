"""Release archives must not leak local state or follow external symlinks."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_script(filename: str):
    spec = importlib.util.spec_from_file_location(f"_policy_test_{filename}", ROOT / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", [
    ".env.test", ".env.staging", ".env.production.local", ".ENV.private",
    ".npmrc", ".pypirc", ".netrc", ".git-credentials",
    "nested/.aws/credentials", "nested/.ssh/id_ed25519",
    "backup/credentials.json", "token.json", "private.p12", "secret.keystore",
    "snapshot.bak-2026", "data/custom_private_state.json",
])
def test_shared_policy_blocks_sensitive_paths(name):
    from scripts.release_file_policy import unsafe_release_path
    assert unsafe_release_path(Path(name)) is not None


@pytest.mark.parametrize("name", [
    "config/providers.example.json", "src/kiro_byo/acp_server.py",
    "tests/test_credentials.py", "config/architecture_budget.json",
])
def test_shared_policy_keeps_legitimate_project_files(name):
    from scripts.release_file_policy import unsafe_release_path
    assert unsafe_release_path(Path(name)) is None


def test_release_check_and_builder_share_policy(tmp_path, monkeypatch):
    builder = _load_script("build_release.py")
    gate = _load_script("release_check.py")
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "good.py").write_text("print('safe')")
    (tmp_path / "src" / ".env.staging").write_text("synthetic-not-a-real-secret")
    assert "sensitive artifacts" in gate._source_hygiene_release_check()
    assert Path("src/.env.staging") not in builder._relative_files()
    (tmp_path / "src" / ".env.staging").unlink()
    assert gate._source_hygiene_release_check() is None
    assert builder._relative_files() == [Path("src/good.py")]


def test_release_refuses_symlink_even_if_it_points_outside_project(tmp_path, monkeypatch):
    builder = _load_script("build_release.py")
    gate = _load_script("release_check.py")
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    (tmp_path / "external").write_text("synthetic fixture")
    (tmp_path / "src").mkdir()
    link = tmp_path / "src" / "innocent.txt"
    try:
        link.symlink_to(tmp_path / "external")
    except OSError as exc:
        pytest.skip(f"symlink creation not supported: {exc}")
    assert "symbolic link" in gate._source_hygiene_release_check()
    with pytest.raises(RuntimeError, match="symbolic link"):
        builder._relative_files()
