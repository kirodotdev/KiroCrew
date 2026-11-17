"""Native build output the scratch quarantine drops: it is rebuilt from the work.

``agent_scratch`` keeps a dead tree that holds a repository in its quarantine,
minus what tools rebuild. Package installs and caches are known by name; the
output of native builds is not, because ``build`` and ``target`` are also
ordinary folder names. So each kind is known by what sits beside it or in it:

* SwiftPM's ``.build`` beside a ``Package.swift``;
* Gradle's ``.gradle`` or ``build`` beside a ``build.gradle(.kts)`` or a
  ``settings.gradle(.kts)``;
* Cargo's ``target`` beside a ``Cargo.toml``;
* an Xcode DerivedData tree: a folder named ``DerivedData``, or one directly
  inside it, holding a regular ``info.plist`` and a real ``Build``,
  ``Index.noindex``, ``Logs`` or ``SourcePackages``, with nothing beside them
  that Xcode does not write. Names are matched in exact case, so an app's own
  ``Info.plist`` or ``build`` folder does not count on a case-insensitive disk
  either.

The decision is by name and kind alone: every marker is read with ``lstat``,
no file under the tree is ever opened, so a link or junction never makes a
match and no file's content can steer it. Any doubt keeps the directory.
"""

from __future__ import annotations

import os
import stat
import types
from pathlib import Path

from kiro_crew import platform_compat

_GRADLE_FILES = ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")
#: A build-output directory name, and the build files one of which must sit
#: beside it as a regular file for it to count.
BUILD_OUTPUT_BESIDE = types.MappingProxyType(
    {
        ".build": ("Package.swift",),
        ".gradle": _GRADLE_FILES,
        "build": _GRADLE_FILES,
        "target": ("Cargo.toml",),
    }
)
_DERIVED_DATA_PLIST = "info.plist"
_DERIVED_DATA_OUTPUT = ("Build", "Index.noindex", "Logs", "SourcePackages")
#: Everything Xcode writes at the top of a DerivedData folder; anything else there
#: means the folder holds something more, and it is kept.
_DERIVED_DATA_ENTRIES = frozenset(
    {
        _DERIVED_DATA_PLIST,
        *_DERIVED_DATA_OUTPUT,
        "ModuleCache.noindex",
        "CompilationCache.noindex",
        "SDKStatCaches.noindex",
        "TextIndex",
        "Previews",
        ".DS_Store",
    }
)
_DERIVED_DATA_FILES = frozenset({_DERIVED_DATA_PLIST, ".DS_Store"})
#: The name Xcode gives the folder holding one DerivedData tree per project,
#: and the name ``-derivedDataPath`` is usually pointed at.
_DERIVED_DATA_HOME = "DerivedData"


def native_build_output(path: Path) -> bool:
    """Whether the plain directory *path* is native build output (see the module docstring)."""
    markers = BUILD_OUTPUT_BESIDE.get(path.name)
    if markers and any(_is_regular_file(path.parent / marker) for marker in markers):
        return True
    return _derived_data_shape(path)


def _derived_data_shape(path: Path) -> bool:
    """Whether *path* is a DerivedData tree, by directory shape alone.

    *path* is named ``DerivedData`` or sits directly in a folder of that name,
    and holds exactly what Xcode writes there: a regular ``info.plist`` and at
    least one real output folder, every entry of the kind Xcode gives it. No
    file is opened. Names come from ``scandir``, so on a case-insensitive disk
    an app's own ``Info.plist`` or a ``build`` folder does not pass for
    ``info.plist`` or ``Build``. Anything else keeps the folder.
    """
    if _DERIVED_DATA_HOME not in (path.name, path.parent.name):
        return False
    if not any(_is_plain_dir(path / name) for name in _DERIVED_DATA_OUTPUT):
        return False
    try:
        names = {entry.name for entry in os.scandir(path)}
    except OSError:
        return False
    if _DERIVED_DATA_PLIST not in names or not names <= _DERIVED_DATA_ENTRIES:
        return False
    # Every entry must be the kind Xcode writes: files are regular, the rest
    # real directories.
    for name in names:
        is_file = name in _DERIVED_DATA_FILES
        if not (_is_regular_file(path / name) if is_file else _is_plain_dir(path / name)):
            return False
    return True


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _is_plain_dir(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not platform_compat.lstat_is_name_surrogate(info)
