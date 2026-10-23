"""Tests for the build-time edition constant (kiro_crew.edition) and its stamper.

The stamper is the real ``kiro_crew._edition_stamp`` that ``setup.py``'s
``build_py`` runs; these tests drive it on a copy of the real edition.py.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

from kiro_crew import _edition_stamp, edition

_REPO = Path(__file__).resolve().parents[1]
_EDITION_SRC = _REPO / "src" / "kiro_crew" / "edition.py"


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("_edition_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _built_copy(tmp_path: Path) -> Path:
    built = tmp_path / "build" / "kiro_crew" / "edition.py"
    built.parent.mkdir(parents=True)
    shutil.copyfile(_EDITION_SRC, built)
    return built


# --- the constant ------------------------------------------------------------


def test_checked_in_edition_is_oss() -> None:
    assert edition.EDITION == "oss"
    assert _load(_EDITION_SRC).EDITION == "oss"


def test_editions_are_oss_and_enterprise() -> None:
    assert edition.EDITIONS == frozenset({"oss", "enterprise"})
    assert edition.EDITION in edition.EDITIONS


def test_is_enterprise_false_on_default_build() -> None:
    assert edition.is_enterprise() is False


def test_monkeypatch_hook_flips_is_enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(edition, "EDITION", "enterprise")
    assert edition.is_enterprise() is True
    monkeypatch.setattr(edition, "EDITION", "oss")
    assert edition.is_enterprise() is False


def test_edition_module_reads_no_environment_or_file() -> None:
    tree = ast.parse(_EDITION_SRC.read_text(encoding="utf-8"))
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert [getattr(n, "module", None) for n in imports] == ["__future__"]
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"open", "getenv", "environ", "os"}


# --- the stamper -------------------------------------------------------------


def test_declared_editions_match_the_module() -> None:
    source = _EDITION_SRC.read_text(encoding="utf-8")
    assert _edition_stamp.declared_editions(source) == edition.EDITIONS


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, "oss"),
        ({"KIROCREW_BUILD_EDITION": ""}, "oss"),
        ({"KIROCREW_BUILD_EDITION": " enterprise\n"}, "enterprise"),
    ],
)
def test_requested_edition_defaults_to_oss(environ: dict, expected: str) -> None:
    assert _edition_stamp.requested_edition(environ) == expected


def test_stamp_writes_enterprise_into_built_copy_only(tmp_path: Path) -> None:
    before = _EDITION_SRC.read_bytes()
    built = _built_copy(tmp_path)
    _edition_stamp.stamp_file(str(built), "enterprise")
    stamped = _load(built)
    assert stamped.EDITION == "enterprise"
    assert stamped.is_enterprise() is True
    assert _EDITION_SRC.read_bytes() == before


def test_default_stamp_overwrites_a_stale_enterprise_copy(tmp_path: Path) -> None:
    built = _built_copy(tmp_path)
    _edition_stamp.stamp_file(str(built), "enterprise")
    _edition_stamp.stamp_file(str(built), "oss")
    assert _load(built).EDITION == "oss"
    assert built.read_bytes() == _EDITION_SRC.read_bytes()


def test_stamp_refuses_unknown_edition(tmp_path: Path) -> None:
    built = _built_copy(tmp_path)
    with pytest.raises(ValueError, match="unknown KIROCREW_BUILD_EDITION 'foo'"):
        _edition_stamp.stamp_file(str(built), "foo")
    assert built.read_bytes() == _EDITION_SRC.read_bytes()


def test_check_edition_accepts_declared_and_refuses_unknown() -> None:
    _edition_stamp.check_edition(str(_EDITION_SRC), "enterprise")
    _edition_stamp.check_edition(str(_EDITION_SRC), "oss")
    with pytest.raises(ValueError, match="unknown KIROCREW_BUILD_EDITION 'foo'"):
        _edition_stamp.check_edition(str(_EDITION_SRC), "foo")


def test_stamp_never_writes_through_a_hard_link(tmp_path: Path) -> None:
    source = tmp_path / "src_edition.py"
    shutil.copyfile(_EDITION_SRC, source)
    built = tmp_path / "built_edition.py"
    try:
        os.link(source, built)
    except OSError:
        pytest.skip("hard links unsupported on this filesystem")
    _edition_stamp.stamp_file(str(built), "enterprise")
    assert _load(built).EDITION == "enterprise"
    assert _load(source).EDITION == "oss"


# --- build wiring ------------------------------------------------------------


def test_build_py_runs_the_stamper() -> None:
    tree = ast.parse((_REPO / "setup.py").read_text(encoding="utf-8"))
    build = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "BuildWithFrontend"
    )
    run = next(n for n in build.body if isinstance(n, ast.FunctionDef) and n.name == "run")
    calls = [ast.unparse(n.func) for n in ast.walk(run) if isinstance(n, ast.Call)]
    assert "self._stamp_edition" in calls


def test_makefile_editions_match_the_module() -> None:
    makefile = (_REPO / "Makefile").read_text(encoding="utf-8")
    assert re.search(
        r"^override EDITION := \$\(if \$\(_EDITION_ENV\),\$\(_EDITION_ENV\),oss\)$",
        makefile,
        re.MULTILINE,
    )
    listed = re.search(r"^override EDITIONS := (.+)$", makefile, re.MULTILINE)
    assert listed and frozenset(listed.group(1).split()) == edition.EDITIONS


@pytest.mark.parametrize("target", ["wheel", "backend-bin", "desktop"])
def test_packaging_targets_check_and_pass_the_edition(target: str) -> None:
    makefile = (_REPO / "Makefile").read_text(encoding="utf-8")
    block = re.search(rf"^{re.escape(target)}:([^\n]*)\n((?:\t[^\n]*\n)+)", makefile, re.MULTILINE)
    assert block, f"no {target} rule in Makefile"
    assert "check-edition" in block.group(1).split()
    assert "KIROCREW_BUILD_EDITION=$(EDITION)" in block.group(2)


_NO_MAKE = pytest.mark.skipif(shutil.which("make") is None, reason="no make on PATH")


def _make_dry_run(
    target: str,
    *,
    edition: str | None = None,
    env_edition: str | None = None,
    ambient_edition: str | None = None,
    make_flags: tuple[str, ...] = (),
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """``make -n <target>`` with only the given edition inputs set.

    ``ambient_edition`` is an exported shell ``EDITION``, which must be ignored.
    """
    dropped = {"EDITION", "EDITIONS", "KIROCREW_BUILD_EDITION", "MAKEFLAGS", "MFLAGS"}
    env = {k: v for k, v in os.environ.items() if k not in dropped}
    if env_edition is not None:
        env["KIROCREW_BUILD_EDITION"] = env_edition
    if ambient_edition is not None:
        env["EDITION"] = ambient_edition
    env.update(extra_env or {})
    argv = ["make", "-s", "-n", *make_flags, target]
    if edition is not None:
        argv.append(f"EDITION={edition}")
    return subprocess.run(
        argv, cwd=_REPO, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120
    )


@_NO_MAKE
@pytest.mark.parametrize(
    ("edition_arg", "env_edition", "expected"),
    [
        (None, None, "oss"),
        ("enterprise", None, "enterprise"),
        (None, "enterprise", "enterprise"),
        ("enterprise", "enterprise", "enterprise"),
        ("oss", None, "oss"),
    ],
)
def test_make_check_edition_resolves(
    edition_arg: str | None, env_edition: str | None, expected: str
) -> None:
    result = _make_dry_run("check-edition", edition=edition_arg, env_edition=env_edition)
    assert result.returncode == 0, result.stderr
    assert f'edition: {expected}"' in result.stdout


@_NO_MAKE
@pytest.mark.parametrize(
    ("edition_arg", "env_edition", "error"),
    [
        ("foo", None, "unknown EDITION 'foo'"),
        ("oss enterprise", None, "unknown EDITION 'oss enterprise'"),
        ("%", None, "unknown EDITION '%'"),
        ("ent%", None, "unknown EDITION 'ent%'"),
        (None, "%", "unknown EDITION '%'"),
        ("oss", "enterprise", "conflicts with KIROCREW_BUILD_EDITION 'enterprise'"),
        ("enterprise", "oss", "conflicts with KIROCREW_BUILD_EDITION 'oss'"),
    ],
)
def test_make_check_edition_refuses(
    edition_arg: str | None, env_edition: str | None, error: str
) -> None:
    result = _make_dry_run("check-edition", edition=edition_arg, env_edition=env_edition)
    assert result.returncode != 0
    assert error in result.stderr


@_NO_MAKE
@pytest.mark.parametrize("target", ["wheel", "backend-bin", "desktop"])
@pytest.mark.parametrize(
    ("edition_arg", "env_edition", "expected"),
    [(None, None, "oss"), ("enterprise", None, "enterprise"), (None, "enterprise", "enterprise")],
)
def test_packaging_target_passes_the_resolved_edition(
    target: str, edition_arg: str | None, env_edition: str | None, expected: str
) -> None:
    result = _make_dry_run(target, edition=edition_arg, env_edition=env_edition)
    assert result.returncode == 0, result.stderr
    stamped = re.findall(r"KIROCREW_BUILD_EDITION=(\S+)", result.stdout)
    assert stamped == [expected], result.stdout


@_NO_MAKE
@pytest.mark.parametrize("target", ["check-edition", "wheel", "backend-bin", "desktop"])
@pytest.mark.parametrize(
    ("ambient", "env_edition", "expected"),
    [("enterprise", None, "oss"), ("foo", None, "oss"), ("%", None, "oss"),
     ("enterprise", "oss", "oss"), ("oss", "enterprise", "enterprise")],
)  # fmt: skip
def test_exported_shell_edition_is_ignored(
    target: str, ambient: str, env_edition: str | None, expected: str
) -> None:
    result = _make_dry_run(target, env_edition=env_edition, ambient_edition=ambient)
    assert result.returncode == 0, result.stderr
    if target == "check-edition":
        assert f'edition: {expected}"' in result.stdout
    else:
        assert re.findall(r"KIROCREW_BUILD_EDITION=(\S+)", result.stdout) == [expected]


def _resolved(result: subprocess.CompletedProcess[str], target: str) -> str:
    assert result.returncode == 0, result.stderr
    if target == "check-edition":
        (value,) = re.findall(r'edition: (\S+)"', result.stdout)
        return value
    (value,) = re.findall(r"KIROCREW_BUILD_EDITION=(\S+)", result.stdout)
    return value


@_NO_MAKE
@pytest.mark.parametrize("target", ["check-edition", "wheel"])
@pytest.mark.parametrize(
    ("flags", "extra_env", "edition_arg", "env_edition", "ambient", "expected"),
    [
        (("-e",), None, None, None, "enterprise", "oss"),
        ((), {"MAKEFLAGS": "e"}, None, None, "enterprise", "oss"),
        (("-e",), None, "enterprise", None, None, "enterprise"),
        (("-e",), None, None, "enterprise", None, "enterprise"),
        (("-e",), None, None, "oss", "enterprise", "oss"),
        (("-e",), {"_EDITION_ENV": "enterprise"}, None, None, None, "oss"),
    ],
)  # fmt: skip
def test_environment_override_mode_cannot_pick_the_edition(
    target: str,
    flags: tuple[str, ...],
    extra_env: dict[str, str] | None,
    edition_arg: str | None,
    env_edition: str | None,
    ambient: str | None,
    expected: str,
) -> None:
    result = _make_dry_run(
        target,
        edition=edition_arg,
        env_edition=env_edition,
        ambient_edition=ambient,
        make_flags=flags,
        extra_env=extra_env,
    )
    assert _resolved(result, target) == expected


@_NO_MAKE
@pytest.mark.parametrize("flags", [("-e",), ()])
def test_an_ambient_editions_list_cannot_widen_the_allowed_set(flags: tuple[str, ...]) -> None:
    extra = {"EDITIONS": "oss enterprise foo"}
    if not flags:
        extra["MAKEFLAGS"] = "e"
    result = _make_dry_run("check-edition", edition="foo", make_flags=flags, extra_env=extra)
    assert result.returncode != 0
    assert "unknown EDITION 'foo'" in result.stderr


@_NO_MAKE
def test_command_line_edition_still_wins_over_an_exported_one() -> None:
    result = _make_dry_run("wheel", edition="enterprise", ambient_edition="oss")
    assert result.returncode == 0, result.stderr
    assert re.findall(r"KIROCREW_BUILD_EDITION=(\S+)", result.stdout) == ["enterprise"]


@_NO_MAKE
@pytest.mark.parametrize("target", ["wheel", "backend-bin", "desktop"])
def test_packaging_target_refuses_a_conflict(target: str) -> None:
    result = _make_dry_run(target, edition="oss", env_edition="enterprise")
    assert result.returncode != 0
    assert "conflicts with KIROCREW_BUILD_EDITION" in result.stderr


def test_enterprise_wheel_carries_the_enterprise_stamp(tmp_path: Path) -> None:
    """Build a real wheel offline through setup.py and read edition.py out of it."""
    pytest.importorskip("setuptools.command.bdist_wheel")
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_BUILD_EDITION"}
    env["KIROCREW_BUILD_EDITION"] = "enterprise"
    dist = tmp_path / "dist"
    argv = [
        "egg_info", "--egg-base", str(tmp_path),
        "build", "--build-base", str(tmp_path / "build"),
        "bdist_wheel", "--bdist-dir", str(tmp_path / "bdist"), "--dist-dir", str(dist),
    ]  # fmt: skip
    result = subprocess.run(
        [sys.executable, "setup.py", "-q", *argv],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    (wheel,) = dist.glob("*.whl")
    with zipfile.ZipFile(wheel) as zf:
        built = zf.read("kiro_crew/edition.py").decode("utf-8")
    assert 'EDITION = "enterprise"' in built.splitlines()
    assert _load(_EDITION_SRC).EDITION == "oss"


def test_setup_py_build_py_stamps_and_refuses(tmp_path: Path) -> None:
    """End to end through the real setup.py, where setuptools is installed."""
    pytest.importorskip("setuptools")
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_BUILD_EDITION"}
    lib, egg = tmp_path / "lib", tmp_path / "egg"
    egg.mkdir()
    src_before = {p.name: p.stat().st_mtime_ns for p in (_REPO / "src").iterdir()}

    def build(value: str | None) -> subprocess.CompletedProcess[str]:
        run_env = dict(env) if value is None else {**env, "KIROCREW_BUILD_EDITION": value}
        # egg_info --egg-base keeps setuptools' metadata out of the checkout's src/.
        argv = ["egg_info", "--egg-base", str(egg), "build_py", "--build-lib", str(lib)]
        return subprocess.run(
            [sys.executable, "setup.py", "-q", *argv],
            cwd=_REPO,
            env=run_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
        )

    built = lib / "kiro_crew" / "edition.py"
    assert build("enterprise").returncode == 0
    assert _load(built).EDITION == "enterprise"
    assert build(None).returncode == 0
    assert _load(built).EDITION == "oss"
    refused = build("foo")
    assert refused.returncode != 0
    assert "unknown KIROCREW_BUILD_EDITION 'foo'" in refused.stderr
    assert _load(_EDITION_SRC).EDITION == "oss"
    assert {p.name: p.stat().st_mtime_ns for p in (_REPO / "src").iterdir()} == src_before
