"""The source-build preflight for the locked pillow (``browser_harness/build_deps.py``)."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from kiro_crew.browser_harness import build_deps, install

_OLD = (2, 26)


def _host(tmp_path: Path, *headers: str, python_h: bool = True, **over) -> dict:
    """Keyword arguments for a host whose include dir holds *headers*.

    The environment and the interpreter's own ``CC`` are pinned empty unless a test
    sets them, so a developer shell's ``CPATH`` or ``CC`` never decides a result.
    """
    include = tmp_path / "include"
    include.mkdir(exist_ok=True)
    for name in headers:
        (include / name).write_text("", encoding="utf-8")
    python_include = tmp_path / "python-include"
    python_include.mkdir(exist_ok=True)
    if python_h:
        (python_include / "Python.h").write_text("", encoding="utf-8")
    host = {"include_dirs": (str(include),), "python_include": python_include}
    return {**host, "env": {}, "sysconfig_cc": None, **over}


def _tools(*present: str):
    return lambda name: f"/usr/bin/{name}" if name in present else None


def test_amazon_linux_2_without_a_toolchain_lists_everything_missing(tmp_path: Path) -> None:
    missing = build_deps.missing_build_deps(
        system="linux", glibc=_OLD, which=_tools(), **_host(tmp_path, python_h=False)
    )
    assert missing == [
        build_deps.MISSING_COMPILER,
        build_deps.MISSING_PYTHON_HEADERS,
        "zlib.h",
        "jpeglib.h",
    ]


def test_a_host_that_has_gcc_and_zlib_but_not_libjpeg_lacks_only_jpeglib(tmp_path: Path) -> None:
    missing = build_deps.missing_build_deps(
        system="linux", glibc=_OLD, which=_tools("gcc"), **_host(tmp_path, "zlib.h")
    )
    assert missing == ["jpeglib.h"]


def test_a_complete_toolchain_lets_pip_build(tmp_path: Path) -> None:
    host = _host(tmp_path, "zlib.h", "jpeglib.h")
    assert (
        build_deps.missing_build_deps(system="linux", glibc=_OLD, which=_tools("cc"), **host) == []
    )


@pytest.mark.parametrize("glibc", [build_deps.PILLOW_WHEEL_MIN_GLIBC, (2, 28), (2, 34), (3, 0)])
def test_a_glibc_the_locked_wheels_cover_needs_no_build(tmp_path: Path, glibc) -> None:
    missing = build_deps.missing_build_deps(
        system="linux", glibc=glibc, which=_tools(), **_host(tmp_path, python_h=False)
    )
    assert missing == []


@pytest.mark.parametrize("system", ["darwin", "win32"])
def test_only_linux_is_checked(tmp_path: Path, system: str) -> None:
    missing = build_deps.missing_build_deps(
        system=system, glibc=_OLD, which=_tools(), **_host(tmp_path, python_h=False)
    )
    assert missing == []


def test_an_unknown_libc_is_not_checked(tmp_path: Path) -> None:
    """musl (Alpine) has no glibc version and pillow ships musllinux wheels for it."""
    missing = build_deps.missing_build_deps(
        system="linux", glibc=None, which=_tools(), **_host(tmp_path, python_h=False)
    )
    assert missing == []


def test_the_refusal_names_the_cause_what_is_missing_and_the_yum_command() -> None:
    text = build_deps.refusal(
        [build_deps.MISSING_COMPILER, "zlib.h", "jpeglib.h"], glibc=_OLD, which=_tools("yum")
    )
    assert "pillow 12.3.0" in text and "glibc 2.26" in text and "2.27" in text
    assert "a C compiler (gcc), zlib.h, jpeglib.h" in text
    assert "sudo yum install -y gcc zlib-devel libjpeg-turbo-devel" in text


def test_the_refusal_names_only_the_packages_for_what_is_missing() -> None:
    text = build_deps.refusal(["jpeglib.h"], glibc=_OLD, which=_tools("yum"))
    assert "sudo yum install -y libjpeg-turbo-devel" in text
    assert "gcc" not in text.split("sudo", 1)[1]


def test_the_refusal_uses_apt_where_there_is_no_yum() -> None:
    text = build_deps.refusal(["zlib.h", "jpeglib.h"], glibc=(2, 23), which=_tools("apt-get"))
    assert "sudo apt-get install -y zlib1g-dev libjpeg-dev" in text


def test_the_refusal_without_a_package_manager_still_names_what_is_missing() -> None:
    text = build_deps.refusal(["jpeglib.h"], glibc=_OLD, which=_tools())
    assert "jpeglib.h" in text and "sudo" not in text


def test_missing_python_headers_are_not_offered_a_package() -> None:
    """The gateway's interpreter is rarely the system one, so no package is guessed."""
    text = build_deps.refusal([build_deps.MISSING_PYTHON_HEADERS], glibc=_OLD, which=_tools("yum"))
    assert build_deps.MISSING_PYTHON_HEADERS in text and "sudo" not in text


def test_the_floor_follows_the_pillow_the_lock_pins() -> None:
    """A lock bump that moves pillow re-reads its wheel tags and moves the floor with it."""
    text = install.LOCK_FILE.read_text(encoding="utf-8")
    pinned = re.search(r"^pillow==(\S+) ", text, re.MULTILINE)
    assert pinned is not None and pinned.group(1) == build_deps.LOCKED_PILLOW


def _dir_with(tmp_path: Path, name: str, *headers: str) -> Path:
    path = tmp_path / name
    path.mkdir(parents=True)
    for header in headers:
        (path / header).write_text("", encoding="utf-8")
    return path


def _missing(tmp_path: Path, env: dict, **over) -> list[str]:
    return build_deps.missing_build_deps(
        system="linux", glibc=_OLD, which=_tools("gcc"), **_host(tmp_path, env=env, **over)
    )


def test_include_path_variables_pillows_build_reads_count(tmp_path: Path) -> None:
    """Pillow's build adds ``C_INCLUDE_PATH``, ``CPATH`` and ``INCLUDE`` to its search."""
    zlib = _dir_with(tmp_path, "z", "zlib.h")
    jpeg = _dir_with(tmp_path, "j", "jpeglib.h")
    for var in ("C_INCLUDE_PATH", "CPATH", "INCLUDE"):
        assert _missing(tmp_path, {var: f"{zlib}{os.pathsep}{jpeg}"}) == [], var
    assert _missing(tmp_path, {}) == ["zlib.h", "jpeglib.h"]


@pytest.mark.parametrize("var", ["CFLAGS", "CPPFLAGS", "LDFLAGS"])
def test_a_glued_dash_i_in_the_flags_pillow_reads_counts(tmp_path: Path, var: str) -> None:
    zlib = _dir_with(tmp_path, "z", "zlib.h")
    jpeg = _dir_with(tmp_path, "j", "jpeglib.h")
    assert _missing(tmp_path, {var: f"-O2 -I{zlib} -g -I{jpeg}"}) == []


def test_a_spaced_dash_i_does_not_count_because_pillow_does_not_read_it(tmp_path: Path) -> None:
    """Pillow matches ``-I([^\\s]+)`` only: ``-I /dir`` would pass here and fail in pip."""
    jpeg = _dir_with(tmp_path, "j", "jpeglib.h")
    assert _missing(tmp_path, {"CFLAGS": f"-I {jpeg}"}) == ["zlib.h", "jpeglib.h"]


def test_a_library_root_variable_counts_its_include_directory(tmp_path: Path) -> None:
    zlib_root = tmp_path / "zroot"
    _dir_with(zlib_root, "include", "zlib.h")
    jpeg_root = tmp_path / "jroot"
    _dir_with(jpeg_root, "include", "jpeglib.h")
    assert _missing(tmp_path, {"ZLIB_ROOT": str(zlib_root), "JPEG_ROOT": str(jpeg_root)}) == []


@pytest.mark.parametrize("var", ["PKG_CONFIG_PATH", "PKG_CONFIG_LIBDIR"])
def test_a_custom_pkg_config_search_leaves_the_headers_to_pip(tmp_path: Path, var: str) -> None:
    """Pillow asks pkg-config, which this check does not run: a custom search path may
    hold the headers, so they are not judged; the compiler and ``Python.h`` still are."""
    assert _missing(tmp_path, {var: str(tmp_path / "pc")}) == []
    no_cc = build_deps.missing_build_deps(
        system="linux", glibc=_OLD, which=_tools(), **_host(tmp_path, env={var: "x"})
    )
    assert no_cc == [build_deps.MISSING_COMPILER]


def test_the_compiler_the_build_would_use_counts(tmp_path: Path) -> None:
    """setuptools compiles with ``$CC``, else the interpreter's own ``CC``."""
    host = _host(tmp_path, "zlib.h", "jpeglib.h")
    gcc10 = tmp_path / "gcc10-gcc"
    gcc10.write_text("", encoding="utf-8")
    # ``$CC`` is split the POSIX shell way, which eats a Windows path's backslashes.
    by_path = {**host, "env": {"CC": f"{gcc10.as_posix()} -pthread"}}
    assert (
        build_deps.missing_build_deps(system="linux", glibc=_OLD, which=_tools(), **by_path) == []
    )
    by_name = {**host, "env": {"CC": "gcc10-gcc"}}
    found = build_deps.missing_build_deps(
        system="linux", glibc=_OLD, which=_tools("gcc10-gcc"), **by_name
    )
    assert found == []
    own = {**host, "sysconfig_cc": "clang -pthread"}
    assert (
        build_deps.missing_build_deps(system="linux", glibc=_OLD, which=_tools("clang"), **own)
        == []
    )
    assert build_deps.missing_build_deps(system="linux", glibc=_OLD, which=_tools(), **host) == [
        build_deps.MISSING_COMPILER
    ]


def test_off_linux_the_host_is_never_probed(monkeypatch) -> None:
    """``platform.libc_ver()`` scans the interpreter binary off glibc; it is not even asked."""
    monkeypatch.setattr(build_deps.sys, "platform", "darwin")
    monkeypatch.setattr(
        build_deps.constants, "_host_glibc_version", lambda: pytest.fail("probed off Linux")
    )
    assert build_deps.refusal_for_this_host() is None


def test_on_linux_the_refusal_composes_the_glibc_and_what_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(build_deps.sys, "platform", "linux")
    monkeypatch.setattr(build_deps.constants, "_host_glibc_version", lambda: (2, 26))
    monkeypatch.setattr(build_deps, "missing_build_deps", lambda **kw: ["jpeglib.h"])
    text = build_deps.refusal_for_this_host()
    assert text is not None and "glibc 2.26" in text and "lacks jpeglib.h" in text
    monkeypatch.setattr(build_deps, "missing_build_deps", lambda **kw: [])
    assert build_deps.refusal_for_this_host() is None
    monkeypatch.setattr(build_deps.constants, "_host_glibc_version", lambda: None)
    assert build_deps.refusal_for_this_host() is None
