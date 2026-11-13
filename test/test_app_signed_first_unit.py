"""In-process unit tests for the signed-first shim (``kiro_crew.apps.signed_first``).

The child tests run the shim as a real subprocess; these call its functions directly,
so its own lines are covered. Every test runs under ``_isolated_import_state``, which
puts back ``sys.path_hooks``, ``sys.meta_path``, ``sys.path``, ``sys.modules``,
``sys.path_importer_cache``, the shim's signed roots and its environment channels.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from conftest import make_dir_link, make_escaping_link
from kiro_crew.apps import signed_first

APP = "unit-app"


@pytest.fixture(autouse=True)
def _isolated_import_state(_floor_monkeypatch):
    """Undo everything a shim call can leave in the process, and check none is left."""
    _floor_monkeypatch.setattr(sys, "path_hooks", list(sys.path_hooks))
    _floor_monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    _floor_monkeypatch.setattr(sys, "path", list(sys.path))
    _floor_monkeypatch.setattr(sys, "argv", list(sys.argv))
    _floor_monkeypatch.setattr(sys, signed_first.VERIFIED_SCRIPT_ATTR, None, raising=False)
    _floor_monkeypatch.delenv(signed_first.FD_ENV, raising=False)
    _floor_monkeypatch.delenv(signed_first.LIST_ENV, raising=False)
    roots = dict(signed_first._source_only_roots)
    cache = dict(sys.path_importer_cache)
    modules = set(sys.modules)
    yield
    signed_first._source_only_roots.clear()
    signed_first._source_only_roots.update(roots)
    sys.path_importer_cache.clear()
    sys.path_importer_cache.update(cache)
    for name in set(sys.modules) - modules:
        del sys.modules[name]  # flake-ok: drops only the app modules this test imported


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _checked(root: Path, files: dict[str, str], **extra: object) -> str:
    return json.dumps({"app": APP, "root": os.path.realpath(root), "files": files, **extra})


def _hand_list(monkeypatch, tmp_path: Path, text: str) -> Path:
    """Write *text* as a stdio checked-list file and name it in the environment."""
    path = tmp_path / "checked-list.json"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv(signed_first.LIST_ENV, str(path))
    return path


# read_checked


def test_read_checked_returns_the_inline_list_and_drops_the_channel(tmp_path, monkeypatch):
    body = b"def run(ctx):\n    return None\n"
    payload = _checked(
        tmp_path,
        {"a.py": "1" * 64},
        unchecked=["ui/x.mjs"],
        script=base64.b64encode(body).decode("ascii"),
    )
    _hand_list(monkeypatch, tmp_path, payload)
    root, files, unchecked, script = signed_first.read_checked(APP)
    assert root == os.path.realpath(tmp_path)
    assert files == {"a.py": "1" * 64}
    assert unchecked == frozenset({"ui/x.mjs"})
    assert script == body
    assert signed_first.LIST_ENV not in os.environ


def test_read_checked_reads_an_inherited_pipe_once(tmp_path, monkeypatch):
    read_fd, write_fd = os.pipe()
    os.write(write_fd, _checked(tmp_path, {"a.py": "2" * 64}).encode("utf-8"))
    os.close(write_fd)
    if sys.platform == "win32":
        import msvcrt

        value = str(msvcrt.get_osfhandle(read_fd))
    else:
        value = str(read_fd)
    monkeypatch.setenv(signed_first.FD_ENV, value)
    try:
        _, files, unchecked, script = signed_first.read_checked(APP)
    finally:
        # The shim closed the descriptor (on Windows, the handle behind it).
        with contextlib.suppress(OSError):
            os.close(read_fd)
    assert files == {"a.py": "2" * 64} and unchecked == frozenset() and script is None
    assert signed_first.FD_ENV not in os.environ


def test_read_checked_refuses_a_launch_with_no_list():
    with pytest.raises(SystemExit, match="refusing to run 'unit-app'"):
        signed_first.read_checked(APP)


def test_read_checked_refuses_another_apps_list(tmp_path, monkeypatch):
    other = json.dumps({"app": "other-app", "root": str(tmp_path), "files": {}})
    _hand_list(monkeypatch, tmp_path, other)
    with pytest.raises(SystemExit, match="no checked list for this app"):
        signed_first.read_checked(APP)


@pytest.mark.parametrize(
    "forged",
    [
        "not json",
        json.dumps({"app": APP, "root": "/x", "files": ["a.py"]}),
        json.dumps({"app": APP, "files": {}}),
        json.dumps({"app": APP, "root": "/x", "files": {}, "script": "%%%not base64"}),
    ],
)
def test_read_checked_refuses_a_forged_or_malformed_list(forged, monkeypatch, tmp_path):
    _hand_list(monkeypatch, tmp_path, forged)
    with pytest.raises(SystemExit, match="refusing to run"):
        signed_first.read_checked(APP)


def test_read_checked_refuses_a_forged_descriptor(monkeypatch):
    """An fd number the launch never handed over (here: not open) is a refusal."""
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    os.close(write_fd)
    monkeypatch.setenv(signed_first.FD_ENV, str(read_fd))
    with pytest.raises(SystemExit, match="refusing to run"):
        signed_first.read_checked(APP)


# the finder and the path hook


def _signed_app(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    root.mkdir()
    (root / "helpers.py").write_text("VALUE = 'signed'\n", encoding="utf-8")
    return root


def _finder(root: Path, listed: set[str]) -> signed_first._SignedFirstFinder:
    real = os.path.realpath(root)
    signed_first.install(real, frozenset(listed))
    return signed_first._SignedFirstFinder(str(root), real)


def test_a_signed_module_wins_over_an_unlisted_package_of_the_same_name(tmp_path):
    root = _signed_app(tmp_path)
    (root / "helpers").mkdir()
    (root / "helpers" / "__init__.py").write_text("VALUE = 'unlisted'\n", encoding="utf-8")
    spec = _finder(root, {"helpers.py"}).find_spec("helpers")
    assert spec is not None and Path(spec.origin).name == "helpers.py"
    assert isinstance(spec.loader, signed_first._SourceOnlyLoader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # compiles the source, never a cached .pyc
    assert module.VALUE == "signed"


def test_a_signed_module_wins_over_an_unlisted_extension(tmp_path):
    root = _signed_app(tmp_path)
    (root / f"helpers{importlib.machinery.EXTENSION_SUFFIXES[0]}").write_bytes(b"\0")
    spec = _finder(root, {"helpers.py"}).find_spec("helpers")
    assert spec is not None and Path(spec.origin).name == "helpers.py"


def test_a_listed_package_loads_as_a_package(tmp_path):
    root = tmp_path / "app"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    spec = _finder(root, {"pkg/__init__.py"}).find_spec("pkg")
    assert spec is not None and spec.submodule_search_locations
    assert Path(spec.origin).name == "__init__.py"


def test_an_unlisted_module_that_shadows_nothing_takes_the_stock_search(tmp_path):
    root = _signed_app(tmp_path)
    (root / "extra.py").write_text("VALUE = 'extra'\n", encoding="utf-8")
    spec = _finder(root, {"helpers.py"}).find_spec("extra")
    assert spec is not None and Path(spec.origin).name == "extra.py"


def test_an_unlisted_package_beside_a_signed_module_is_reported(tmp_path):
    root = _signed_app(tmp_path)
    (root / "helpers" / "deep").mkdir(parents=True)
    (root / "helpers" / "__init__.py").write_text("", encoding="utf-8")
    (root / "helpers" / "deep" / "mod.py").write_text("", encoding="utf-8")
    (root / "helpers" / "notes.txt").write_text("", encoding="utf-8")
    (root / "helpers" / "__pycache__").mkdir()
    (root / "helpers" / "__pycache__" / "x.pyc").write_bytes(b"")
    real = os.path.realpath(root)
    assert signed_first.unlisted_package_files(real, frozenset({"helpers.py"})) == [
        "helpers/__init__.py",
        "helpers/deep/mod.py",
    ]
    listed = frozenset({"helpers.py", "helpers/__init__.py", "helpers/deep/mod.py"})
    assert signed_first.unlisted_package_files(real, listed) == []


def test_a_linked_package_dir_and_a_plain_dir_are_not_packages(tmp_path):
    root = _signed_app(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "__init__.py").write_text("", encoding="utf-8")
    make_dir_link(root / "helpers", elsewhere)
    (root / "other.py").write_text("", encoding="utf-8")
    (root / "other").mkdir()  # no __init__: a namespace directory, not a package
    real = os.path.realpath(root)
    assert signed_first.unlisted_package_files(real, frozenset({"helpers.py", "other.py"})) == []
    assert not signed_first._is_package_dir(str(tmp_path / "missing"))


def test_the_path_hook_takes_directories_inside_a_signed_root_only(tmp_path):
    root = _signed_app(tmp_path)
    (root / "sub").mkdir()
    real = os.path.realpath(root)
    signed_first.install(real, frozenset({"helpers.py"}))
    assert signed_first._source_only_path_hook in sys.path_hooks
    finder = signed_first._source_only_path_hook(str(root / "sub"))
    assert isinstance(finder, signed_first._SignedFirstFinder)
    with pytest.raises(ImportError, match="only directories"):
        signed_first._source_only_path_hook(str(root / "helpers.py"))
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ImportError, match="not inside a signed app"):
        signed_first._source_only_path_hook(str(outside))
    assert signed_first._owning_root(str(outside)) is None


def test_install_drops_the_finders_cached_for_the_root_and_adds_the_hook_once(tmp_path):
    root = _signed_app(tmp_path)
    real = os.path.realpath(root)
    sys.path_importer_cache[str(root)] = None
    sys.path_importer_cache[str(tmp_path / "unrelated")] = None
    signed_first.install(real, frozenset({"helpers.py"}))
    signed_first.install(real, frozenset({"helpers.py"}))
    assert str(root) not in sys.path_importer_cache
    assert str(tmp_path / "unrelated") in sys.path_importer_cache
    assert sys.path_hooks.count(signed_first._source_only_path_hook) == 1


# the per-file hash and the launch-time recheck


def test_unchanged_refusal_passes_matching_files_and_skips_unchecked_ones(tmp_path):
    root = tmp_path / "app"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.py").write_text("print(1)\n", encoding="utf-8")
    files = {"sub/a.py": _sha(root / "sub" / "a.py"), "ui/gone.mjs": "0" * 64}
    assert signed_first._file_sha256(str(root), "sub/a.py") == files["sub/a.py"]
    assert signed_first.unchanged_refusal(str(root), files, frozenset({"ui/gone.mjs"})) is None


def test_unchanged_refusal_refuses_a_changed_or_missing_file(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "a.py").write_text("print('changed')\n", encoding="utf-8")
    reason = signed_first.unchanged_refusal(str(root), {"a.py": "0" * 64})
    assert reason is not None and "does not match" in reason
    reason = signed_first.unchanged_refusal(str(root), {"missing.py": "0" * 64})
    assert reason is not None and "missing" in reason


def test_unchanged_refusal_refuses_a_listed_file_reached_through_a_link(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("print('elsewhere')\n", encoding="utf-8")
    rel = make_escaping_link(root, outside)
    reason = signed_first.unchanged_refusal(str(root), {rel: _sha(outside / "secret.py")})
    assert reason is not None and rel in reason


def test_unchanged_refusal_refuses_a_listed_file_under_a_linked_directory(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "mod.py").write_text("print(1)\n", encoding="utf-8")
    make_dir_link(root / "linked", elsewhere)
    reason = signed_first.unchanged_refusal(
        str(root), {"linked/mod.py": _sha(elsewhere / "mod.py")}
    )
    assert reason is not None and "linked/mod.py" in reason


def test_unchanged_refusal_refuses_a_listed_path_that_is_not_a_regular_file(tmp_path):
    root = tmp_path / "app"
    (root / "a.py").mkdir(parents=True)
    reason = signed_first.unchanged_refusal(str(root), {"a.py": "0" * 64})
    assert reason is not None and "not a regular file" in reason


@pytest.mark.skipif(sys.platform == "win32", reason="FIFOs are POSIX")
def test_a_fifo_in_a_listed_files_place_is_refused_without_a_writer(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    os.mkfifo(root / "a.py")
    reason = signed_first.unchanged_refusal(str(root), {"a.py": "0" * 64})
    assert reason is not None and "not a regular file" in reason


@pytest.mark.skipif(sys.platform == "win32", reason="FIFOs are POSIX")
def test_a_fifo_as_the_checked_list_file_is_refused_without_a_writer(tmp_path):
    os.mkfifo(tmp_path / "list.json")
    with pytest.raises(ValueError, match="not a regular file"):
        signed_first._read_list_file(str(tmp_path / "list.json"))


# main


def test_main_without_a_target_prints_usage_and_exits_2(capsys):
    with pytest.raises(SystemExit) as raised:
        signed_first.main([APP])
    assert raised.value.code == 2
    assert "usage" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        signed_first.main([APP, "-m"])


def test_main_refuses_without_a_list(tmp_path):
    with pytest.raises(SystemExit, match="refusing to run"):
        signed_first.main([APP, str(tmp_path / "main.py")])
    assert not signed_first._source_only_roots.get(os.path.realpath(tmp_path))


def test_main_refuses_a_changed_signed_file_before_the_target_runs(tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    marker = tmp_path / "ran"
    (root / "main.py").write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")
    _hand_list(monkeypatch, tmp_path, _checked(root, {"main.py": "0" * 64}))
    with pytest.raises(SystemExit, match="does not match the signed inventory"):
        signed_first.main([APP, str(root / "main.py")])
    assert not marker.exists()
    assert os.path.realpath(root) not in signed_first._source_only_roots


def test_main_refuses_an_unsigned_package_shadowing_a_signed_module(tmp_path, monkeypatch):
    root = _signed_app(tmp_path)
    (root / "helpers").mkdir()
    (root / "helpers" / "__init__.py").write_text("", encoding="utf-8")
    _hand_list(monkeypatch, tmp_path, _checked(root, {"helpers.py": _sha(root / "helpers.py")}))
    with pytest.raises(SystemExit, match="shadows a signed module"):
        signed_first.main([APP, str(root / "helpers.py")])


def test_main_runs_a_verified_target_with_the_finder_installed(tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    marker = tmp_path / "ran"
    (root / "main.py").write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")
    body = b"def run(ctx):\n    return None\n"
    _hand_list(
        monkeypatch,
        tmp_path,
        _checked(
            root,
            {"main.py": _sha(root / "main.py")},
            script=base64.b64encode(body).decode("ascii"),
        ),
    )
    sys.path.insert(0, os.path.dirname(os.path.abspath(signed_first.__file__)))
    signed_first.main([APP, str(root / "main.py")])
    assert marker.exists()
    assert signed_first._source_only_roots[os.path.realpath(root)] == frozenset({"main.py"})
    assert getattr(sys, signed_first.VERIFIED_SCRIPT_ATTR) == body
    assert sys.path[0] != os.path.dirname(os.path.abspath(signed_first.__file__))


def test_read_checked_refuses_a_list_path_that_is_a_link_or_not_a_file(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text(_checked(tmp_path, {}), encoding="utf-8")
    holder = tmp_path / "holder"
    holder.mkdir()
    rel = make_escaping_link(holder, outside)
    if sys.platform != "win32":  # a file link is refused at the name itself
        monkeypatch.setenv(signed_first.LIST_ENV, str(holder / rel))
        with pytest.raises(SystemExit, match="refusing to run"):
            signed_first.read_checked(APP)
    monkeypatch.setenv(signed_first.LIST_ENV, str(tmp_path))  # a directory
    with pytest.raises(SystemExit, match="refusing to run"):
        signed_first.read_checked(APP)
    monkeypatch.setenv(signed_first.LIST_ENV, str(tmp_path / "missing.json"))
    with pytest.raises(SystemExit, match="refusing to run"):
        signed_first.read_checked(APP)


# Interpreters older than 3.12 and modules the dispatch caches


def test_is_link_works_without_os_path_isjunction(tmp_path, monkeypatch):
    """``os.path.isjunction`` is 3.12+; an app may name an older interpreter."""
    monkeypatch.delattr(os.path, "isjunction", raising=False)
    target = tmp_path / "target"
    target.mkdir()
    make_dir_link(tmp_path / "linked", target)
    assert signed_first._is_link(str(tmp_path / "linked"))
    assert not signed_first._is_link(str(target))
    assert not signed_first._is_link(str(tmp_path / "missing"))
    # A linked package dir is still not followed.
    root = _signed_app(tmp_path)
    (target / "__init__.py").write_text("", encoding="utf-8")
    make_dir_link(root / "helpers", target)
    real = os.path.realpath(root)
    assert signed_first.unlisted_package_files(real, frozenset({"helpers.py"})) == []


def test_yield_names_drops_only_modules_cached_since_the_shim_started(tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    fake = type(sys)("kc_unit_cached")
    monkeypatch.setitem(sys.modules, "kc_unit_cached", fake)
    monkeypatch.setitem(sys.modules, "kc_unit_cached.sub", type(sys)("kc_unit_cached.sub"))
    monkeypatch.setitem(sys.modules, "kc_unit_other", type(sys)("kc_unit_other"))
    listed = frozenset({"kc_unit_cached.py", "sys.py", "main.py"})
    signed_first._yield_names(str(root), listed, [str(root / "main.py")])
    assert "kc_unit_cached" not in sys.modules and "kc_unit_cached.sub" not in sys.modules
    assert "kc_unit_other" in sys.modules  # nothing signed provides that name
    assert sys.modules["sys"] is sys  # never dropped, even when an app file names it
