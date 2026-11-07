"""Whether this host can build the locked pillow, checked before pip is asked to.

``browser-harness`` pins ``pillow==12.3.0`` (``requirements.lock``), and that release
publishes Linux wheels for glibc 2.27 and later only (``manylinux_2_27``). On an older
glibc (Amazon Linux 2 ships 2.26) pip falls back to the sdist and compiles it, which
needs a C compiler, this interpreter's headers and the zlib and libjpeg headers.
Without them the install fails with pip's build log, whose last lines (the part the
install error keeps) end at ``Failed to build pillow`` and name no cause.
:func:`refusal_for_this_host` names what is missing before pip starts, with the
package command that installs it, so :mod:`.install` refuses at once instead.

Linux only, and read from the filesystem and the environment: ``PATH``, the build
variables pillow's ``setup.py`` reads, and the include directories they name, with
nothing launched. A host the locked wheels cover, an unknown
libc (musl, which pillow ships ``musllinux`` wheels for), and every other platform
are never checked.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
import sysconfig
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path

from kiro_crew import constants

#: The pillow ``requirements.lock`` pins; ``test_the_floor_follows_the_pillow_the_lock_pins``
#: fails when the lock moves it, so a bump re-reads the new release's wheel tags.
LOCKED_PILLOW = "12.3.0"

#: The oldest glibc :data:`LOCKED_PILLOW` publishes a Linux wheel for.
PILLOW_WHEEL_MIN_GLIBC: tuple[int, int] = (2, 27)

#: The two names in the missing list that are not a header file.
MISSING_COMPILER = "a C compiler (gcc)"
MISSING_PYTHON_HEADERS = "this Python's headers (Python.h)"

#: The C compilers pip's build can use, by the names ``PATH`` may hold, besides the
#: one ``$CC`` or the interpreter's own ``CC`` names (setuptools compiles with that).
_COMPILERS: tuple[str, ...] = ("cc", "gcc")

#: Where the system's development headers live on Linux (Linux only: never read
#: off Linux).
INCLUDE_DIRS: tuple[str, ...] = ("/usr/include", "/usr/local/include")

#: Every other place pillow 12.3.0's ``setup.py`` adds include directories from, so a
#: header found there is found: ``pathsep`` lists, the glued ``-I<dir>`` its regex
#: matches in three flag variables (a spaced ``-I <dir>`` it does not read), and a
#: library root's ``include``.
_INCLUDE_PATH_VARS: tuple[str, ...] = ("C_INCLUDE_PATH", "CPATH", "INCLUDE")
_FLAG_VARS: tuple[str, ...] = ("CFLAGS", "CPPFLAGS", "LDFLAGS")
_INCLUDE_FLAG = re.compile(r"-I([^\s]+)")
_ROOT_VARS: tuple[str, ...] = ("ZLIB_ROOT", "JPEG_ROOT")
#: Pillow also asks pkg-config, which this check does not run. The system's own
#: ``.pc`` files ship with headers in the system include directories; a custom
#: search path may point anywhere, so with one set the headers are left to pip.
_PKG_CONFIG_VARS: tuple[str, ...] = ("PKG_CONFIG_PATH", "PKG_CONFIG_LIBDIR")

#: The library headers pillow's build refuses to start without.
_HEADERS: tuple[str, ...] = ("zlib.h", "jpeglib.h")

#: Package per missing item, per package manager. Python's headers have none: the
#: gateway's interpreter is rarely the system one, so no package is guessed.
_PACKAGES: dict[str, dict[str, str]] = {
    "yum": {MISSING_COMPILER: "gcc", "zlib.h": "zlib-devel", "jpeglib.h": "libjpeg-turbo-devel"},
    "apt-get": {MISSING_COMPILER: "gcc", "zlib.h": "zlib1g-dev", "jpeglib.h": "libjpeg-dev"},
}

Which = Callable[[str], "str | None"]


def missing_build_deps(
    *,
    glibc: tuple[int, int] | None,
    system: str | None = None,
    which: Which = shutil.which,
    include_dirs: Iterable[str] = INCLUDE_DIRS,
    python_include: Path | None = None,
    env: Mapping[str, str] | None = None,
    sysconfig_cc: str | None = "",
) -> list[str]:
    """What pip needs to build :data:`LOCKED_PILLOW` here and cannot find; ``[]`` if nothing.

    Also ``[]`` wherever no build happens: off Linux, an unknown libc, and a glibc
    the locked wheels cover.
    """
    if not (system or sys.platform).startswith("linux"):
        return []
    if glibc is None or glibc >= PILLOW_WHEEL_MIN_GLIBC:
        return []
    env = os.environ if env is None else env
    if sysconfig_cc == "":
        sysconfig_cc = sysconfig.get_config_var("CC")
    missing: list[str] = []
    if not _has_compiler((env.get("CC"), sysconfig_cc), which):
        missing.append(MISSING_COMPILER)
    include = python_include if python_include is not None else Path(sysconfig.get_path("include"))
    if not _is_file(include / "Python.h"):
        missing.append(MISSING_PYTHON_HEADERS)
    if not any(env.get(var) for var in _PKG_CONFIG_VARS):
        dirs = [Path(d) for d in (*include_dirs, *_env_include_dirs(env))]
        missing.extend(h for h in _HEADERS if not any(_is_file(d / h) for d in dirs))
    return missing


def refusal(missing: Sequence[str], *, glibc: tuple[int, int], which: Which = shutil.which) -> str:
    """The install's refusal: why pillow must be built, what is missing, and the fix."""
    floor = ".".join(map(str, PILLOW_WHEEL_MIN_GLIBC))
    text = (
        f"Browser Harness needs pillow {LOCKED_PILLOW}, which has no wheel for this host's "
        f"glibc {glibc[0]}.{glibc[1]} (its wheels need glibc {floor}+), so pip has to build "
        f"it here, and this host lacks {', '.join(missing)}."
    )
    command = _install_command(missing, which)
    if command:
        text += f" Install them, then install Browser Harness again: {command}"
    if MISSING_PYTHON_HEADERS in missing:
        text += f" ({sys.executable} ships no development headers.)"
    return text


def refusal_for_this_host() -> str | None:
    """:func:`refusal` for this host, or ``None`` when pip can install the lock here."""
    if not sys.platform.startswith("linux"):
        return None
    glibc = constants._host_glibc_version()
    if glibc is None:
        return None
    missing = missing_build_deps(glibc=glibc)
    return refusal(missing, glibc=glibc) if missing else None


def _env_include_dirs(env: Mapping[str, str]) -> list[str]:
    """The include directories pillow's build adds from the environment."""
    dirs = [d for var in _INCLUDE_PATH_VARS for d in env.get(var, "").split(os.pathsep) if d]
    for var in _FLAG_VARS:
        dirs.extend(m.group(1) for m in _INCLUDE_FLAG.finditer(env.get(var, "")))
    dirs.extend(os.path.join(env[var], "include") for var in _ROOT_VARS if env.get(var))
    return dirs


def _has_compiler(specs: Iterable[str | None], which: Which) -> bool:
    """A compiler ``$CC`` or the interpreter's ``CC`` names exists, or ``cc``/``gcc`` does."""
    for spec in specs:
        try:
            program = shlex.split(spec)[0] if spec else ""
        except ValueError:
            program = ""
        if program and (which(program) or (os.path.isabs(program) and _is_file(Path(program)))):
            return True
    return any(which(name) for name in _COMPILERS)


def _install_command(missing: Sequence[str], which: Which) -> str | None:
    for manager, packages in _PACKAGES.items():
        if which(manager):
            names = [packages[item] for item in missing if item in packages]
            return f"sudo {manager} install -y {' '.join(names)}" if names else None
    return None


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False
