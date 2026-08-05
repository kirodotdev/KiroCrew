"""Side-effect-free Kiro CLI discovery shared by setup and ACP launch paths."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.env import augmented_path

KIRO_CLI_NAME = "kiro-cli"


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _windows_program_files(environ: Mapping[str, str]) -> str:
    return environ.get("ProgramFiles") or environ.get("PROGRAMFILES") or r"C:\Program Files"


def known_kiro_cli_dirs(
    platform_name: str,
    home: Path,
    environ: Mapping[str, str],
    *,
    include_inherited_path: bool = True,
) -> list[str]:
    """Return fixed and inherited directories where Kiro CLI may be installed."""

    if platform_name == "win32":
        dirs = [str(Path(_windows_program_files(environ)) / "Kiro-Cli")]
    else:
        dirs = [
            str(home / ".local" / "bin"),
            str(home / ".cargo" / "bin"),
        ]
    if platform_name == "darwin":
        dirs.extend(
            [
                "/Applications/Kiro CLI.app/Contents/MacOS",
                str(home / "Applications" / "Kiro CLI.app" / "Contents" / "MacOS"),
                "/opt/homebrew/bin",
                "/usr/local/bin",
            ]
        )
    if include_inherited_path and platform_name == "win32":
        dirs.extend(part for part in environ.get("PATH", "").split(";") if part)
        # A GUI-launched Windows gateway may omit its venv's Scripts directory
        # from PATH. ACP launch still needs the console-script fallback that
        # existed before setup and ACP adopted this shared resolver.
        dirs.append(str(Path(sys.executable).parent))
    elif include_inherited_path:
        dirs.extend(
            part for part in augmented_path(environ.get("PATH", "")).split(os.pathsep) if part
        )
    return _unique(dirs)


def find_kiro_cli_candidates(
    platform_name: str,
    home: Path,
    environ: Mapping[str, str],
    *,
    include_inherited_path: bool = True,
) -> list[str]:
    """Enumerate executable Kiro CLI candidates without mutating the environment."""

    name = f"{KIRO_CLI_NAME}.exe" if platform_name == "win32" else KIRO_CLI_NAME
    candidates: list[str] = []
    override = environ.get("KIROCREW_KIRO_BIN", "")
    if override:
        candidates.append(override)
    candidates.extend(
        str(Path(directory) / name)
        for directory in known_kiro_cli_dirs(
            platform_name,
            home,
            environ,
            include_inherited_path=include_inherited_path,
        )
    )
    result: list[str] = []
    for candidate in _unique(candidates):
        if platform_compat.is_executable_file(candidate, platform_name=platform_name):
            result.append(os.path.realpath(candidate) if platform_name == "win32" else candidate)
    return result


def resolve_kiro_cli(
    *,
    platform_name: str | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Return the first executable Kiro CLI candidate, if one exists."""

    resolved_platform = platform_name or sys.platform
    resolved_home = home or Path.home()
    resolved_environ = environ if environ is not None else os.environ
    candidates = find_kiro_cli_candidates(
        resolved_platform,
        resolved_home,
        resolved_environ,
        include_inherited_path=True,
    )
    return candidates[0] if candidates else None
