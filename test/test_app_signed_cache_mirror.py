"""Signed hook preparation clears only this app's external bytecode mirror."""

from __future__ import annotations

import importlib.util
import py_compile
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from kiro_crew.apps import signed_imports
from kiro_crew.testing.links import make_dir_link


def test_external_mirror_cannot_supply_unsigned_direct_loader_bytecode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "app"
    root.mkdir()
    helper = root / "helper.py"
    helper.write_text("VALUE = 'published'\n", encoding="utf-8")
    evil = tmp_path / "evil.py"
    evil.write_text("VALUE = 'cached'\n", encoding="utf-8")
    prefix = tmp_path / "mirror"
    monkeypatch.setattr(sys, "pycache_prefix", str(prefix))
    cache = Path(importlib.util.cache_from_source(str(helper)))
    py_compile.compile(
        str(evil),
        cfile=str(cache),
        dfile=str(helper),
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
        doraise=True,
    )
    other = prefix / "unrelated" / "keep.pyc"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"unrelated")
    assert cache.is_file()
    cached_spec = importlib.util.spec_from_file_location("_mirror_control_helper", helper)
    assert cached_spec is not None and cached_spec.loader is not None
    cached_module = importlib.util.module_from_spec(cached_spec)
    cached_spec.loader.exec_module(cached_module)
    assert cached_module.VALUE == "cached"
    signed_imports.clear_app_bytecode(root)
    assert not cache.exists()
    spec = importlib.util.spec_from_file_location("_mirror_test_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.VALUE == "published"
    assert other.read_bytes() == b"unrelated"
    assert sys.pycache_prefix == str(prefix)


def test_mirror_cleanup_failure_is_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "app"
    root.mkdir()
    monkeypatch.setattr(sys, "pycache_prefix", str(tmp_path / "mirror"))
    mirror = Path(importlib.util.cache_from_source(str(root / "helper.py"))).parent
    mirror.mkdir(parents=True)
    marker = mirror / "keep.pyc"
    marker.write_bytes(b"cached")
    original_unlink = signed_imports.PinnedDirectory.unlink
    attempted = []

    def unlink(held, name):
        if held.path == str(mirror):
            attempted.append(name)
            raise OSError("mirror cleanup refused")
        return original_unlink(held, name)

    monkeypatch.setattr(signed_imports.PinnedDirectory, "unlink", unlink)
    with pytest.raises(OSError, match="mirror cleanup refused"):
        signed_imports.clear_app_bytecode(root)
    assert attempted == [marker.name]
    assert marker.read_bytes() == b"cached"


def test_linked_app_mirror_never_deletes_unrelated_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "app"
    root.mkdir()
    monkeypatch.setattr(sys, "pycache_prefix", str(tmp_path / "mirror"))
    mirror = Path(importlib.util.cache_from_source(str(root / "helper.py"))).parent
    mirror.parent.mkdir(parents=True)
    target = tmp_path / "foreign"
    target.mkdir()
    marker = target / "keep.pyc"
    marker.write_bytes(b"foreign")
    make_dir_link(mirror, target)
    with pytest.raises(OSError, match="linked directory"):
        signed_imports.clear_app_bytecode(root)
    assert marker.read_bytes() == b"foreign"


def test_without_external_prefix_existing_cache_behavior_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "pycache_prefix", None)
    root = tmp_path / "app"
    cache = root / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "old.pyc").write_bytes(b"cached")
    signed_imports.clear_app_bytecode(root)
    assert root.is_dir() and not cache.exists()


@pytest.mark.parametrize("required", [False, True])
def test_registry_script_cache_is_fresh_without_erasing_running_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, required: bool
) -> None:
    from kiro_crew.apps import admission

    monkeypatch.setattr(admission, "signature_required", lambda: required)
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    old = tmp_path / "run" / "app-pycache" / "probe" / "running" / "keep.pyc"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"running")
    first = signed_imports.registry_pycache_prefix_env("probe")
    second = signed_imports.registry_pycache_prefix_env("probe")
    if required:
        assert first != second
        for env in (first, second):
            path = Path(env["PYTHONPYCACHEPREFIX"])
            assert path.is_dir() and not list(path.iterdir())
            assert path.is_relative_to(tmp_path / "run" / "app-pycache" / "probe")
    else:
        assert first == second == {}
    assert old.read_bytes() == b"running"


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [False, True])
async def test_registry_build_commands_each_get_fresh_signed_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, required: bool
) -> None:
    import asyncio

    from apps_registry_coverage_helpers import _fake_sandbox, _FakeProc

    from kiro_crew.apps import admission, registry
    from kiro_crew.apps.manifest import AppManifest
    from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

    root = tmp_path / "source"
    root.mkdir()
    (root / "package.json").write_text('{"scripts":{"build":"python build.py"}}', encoding="utf-8")
    monkeypatch.setattr(admission, "signature_required", lambda: required)
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr(registry.shutil, "which", lambda name: str(tmp_path / "npm"))
    _fake_sandbox(monkeypatch, [])
    environments: list[dict[str, str]] = []

    async def spawn(*argv, **kwargs):
        environments.append(dict(kwargs["env"]))
        return _FakeProc()

    monkeypatch.setattr(registry, "create_subprocess_limited", spawn)
    result = await asyncio.wait_for(
        registry._run_app_build(
            root, "probe", [], manifest=AppManifest.from_dict({"name": "probe"}), self_managed=False
        ),
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    assert result == {"ok": True}
    assert len(environments) == 2
    if required:
        prefixes = [Path(env["PYTHONPYCACHEPREFIX"]) for env in environments]
        assert prefixes[0] != prefixes[1]
        assert all(path.is_dir() and not list(path.iterdir()) for path in prefixes)
    else:
        assert all("PYTHONPYCACHEPREFIX" not in env for env in environments)


@pytest.mark.parametrize("surface", ["installed", "registry"])
@pytest.mark.parametrize("required", [False, True])
def test_child_preparation_clears_bytecode_before_python_ignores_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str, required: bool
) -> None:
    import os
    import subprocess

    from kiro_crew.apps import admission
    from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

    root = tmp_path / "publisher"
    root.mkdir()
    helper = root / "helper.py"
    helper.write_text("VALUE = 'publisher'\n", encoding="utf-8")
    cached = tmp_path / "cached_input.py"
    cached.write_text("VALUE = 'cached'\n", encoding="utf-8")
    monkeypatch.setattr(sys, "pycache_prefix", None)
    cache = Path(importlib.util.cache_from_source(str(helper)))
    cache.parent.mkdir()
    py_compile.compile(
        str(cached),
        cfile=str(cache),
        dfile=str(helper),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
    server = root / "server.py"
    server.write_text("import helper\nprint(helper.VALUE)\n", encoding="utf-8")
    monkeypatch.setattr(admission, "signed_tree_applies", lambda *args: required)
    monkeypatch.setattr(admission, "signature_required", lambda: required)
    monkeypatch.setattr(admission, "_activation_root", lambda name: root)
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    env = (
        signed_imports.pycache_prefix_env("probe", root)
        if surface == "installed"
        else signed_imports.registry_pycache_prefix_env("probe", root)
    )
    assert cache.exists() is (not required)
    for key in ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP"):
        if key in os.environ:
            env[key] = os.environ[key]
    result = subprocess.run(
        [sys.executable, "-E", str(server)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("publisher" if required else "cached")


@pytest.mark.parametrize("surface", ["installed", "registry"])
def test_child_preparation_refuses_cache_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    from kiro_crew.apps import admission

    monkeypatch.setattr(admission, "signed_tree_applies", lambda *args: True)
    monkeypatch.setattr(admission, "signature_required", lambda: True)
    monkeypatch.setattr(admission, "_activation_root", lambda name: tmp_path)
    monkeypatch.setattr(
        signed_imports, "clear_app_bytecode", Mock(side_effect=OSError("cache held"))
    )
    with pytest.raises(OSError, match="cache held"):
        if surface == "installed":
            signed_imports.pycache_prefix_env("probe", tmp_path)
        else:
            signed_imports.registry_pycache_prefix_env("probe", tmp_path)


def test_in_tree_cleanup_prunes_linked_data_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "pycache_prefix", None)
    root = tmp_path / "app"
    own_cache = root / "backend" / "__pycache__"
    own_cache.mkdir(parents=True)
    (own_cache / "own.pyc").write_bytes(b"own cache")
    data = root / "data"
    data.mkdir()
    foreign = tmp_path / "external-project"
    foreign_cache = foreign / "package" / "__pycache__"
    foreign_cache.mkdir(parents=True)
    marker = foreign_cache / "keep.pyc"
    marker.write_bytes(b"external cache")
    link = data / "project"
    make_dir_link(link, foreign)
    signed_imports.clear_app_bytecode(root)
    assert not own_cache.exists()
    assert link.is_dir()
    assert marker.read_bytes() == b"external cache"


def test_linked_cache_leaf_is_removed_without_touching_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "pycache_prefix", None)
    root = tmp_path / "app"
    root.mkdir()
    foreign_cache = tmp_path / "external-cache"
    foreign_cache.mkdir()
    marker = foreign_cache / "keep.pyc"
    marker.write_bytes(b"external cache")
    cache = root / "__pycache__"
    make_dir_link(cache, foreign_cache)
    signed_imports.clear_app_bytecode(root)
    assert not cache.exists()
    assert marker.read_bytes() == b"external cache"
