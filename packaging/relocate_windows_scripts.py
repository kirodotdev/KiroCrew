"""Replace pip's frozen console-script launchers in the Windows bundle's Scripts\\.

pip writes every ``Scripts\\<name>.exe`` as distlib's launcher stub, then a
``#!<interpreter>`` line, then a zip whose ``__main__.py`` is the script.  At
bundle build time ``<interpreter>`` is the BUILD machine's absolute
``python.exe``, so once the bundle is installed anywhere else the launcher
cannot start its interpreter and exits 1 with nothing on stdout or stderr.
``env._interpreter_script_dirs`` puts this folder on every agent's ``PATH``, so
an agent running ``httpx`` or ``pwcpp`` by name lands on such a launcher.

For each launcher whose ``#!`` names an absolute path, this helper writes the
zip's ``__main__.py`` out as ``<name>-script.py`` and a relocatable
``<name>.cmd`` that runs it under ``%~dp0..\\python.exe`` (``Scripts\\`` sits
one level below the bundle root, as ``bin\\`` does), then deletes the ``.exe``.
The ``.cmd`` carries the same environment scrub as ``bin\\kirocrew.cmd``.
Native executables such as ``uv.exe`` carry no launcher zip and are left alone.
A launcher with a frozen ``#!`` but no ``__main__.py`` fails the build rather
than shipping a command that cannot start.

Both bundle writers run it after their own ``kirocrew`` shim:
``packaging/build-desktop.sh`` (``build_backend_windows``) and the Windows
installer job in ``.github/workflows/build.yml``.
"""

from __future__ import annotations

import argparse
import ntpath
import sys
import zipfile
from pathlib import Path

_SHIM_HEAD = ("@echo off", "setlocal", 'set "PYTHONPATH="', 'set "PYTHONHOME="')


def frozen_interpreter(launcher: Path) -> str | None:
    """The absolute interpreter a pip launcher's ``#!`` names, else ``None``."""
    try:
        with zipfile.ZipFile(launcher) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile:
        return None
    if not infos:
        return None
    # zipfile shifts header_offset by the bytes prepended to the archive, so
    # the smallest one is where the zip starts inside the .exe.
    prefix = launcher.read_bytes()[: min(info.header_offset for info in infos)]
    # distlib's stub reads the shebang the same way: the last "#!" before the zip.
    at = prefix.rfind(b"#!")
    if at < 0:
        return None
    line = prefix[at + 2 :].split(b"\n", 1)[0].strip().decode("utf-8", "replace")
    interpreter = line[1:].split('"', 1)[0] if line.startswith('"') else line.split(" ", 1)[0]
    return interpreter if ntpath.isabs(interpreter) else None


def frozen_launchers(scripts: Path) -> list[Path]:
    """Every ``*.exe`` under ``scripts`` whose ``#!`` names an absolute path."""
    return [p for p in sorted(scripts.glob("*.exe")) if frozen_interpreter(p) is not None]


def relocate_scripts(root: Path) -> list[str]:
    """Replace each frozen launcher under ``root/Scripts``; return their names."""
    scripts = root / "Scripts"
    if not scripts.is_dir():
        return []
    replaced = []
    for launcher in frozen_launchers(scripts):
        name = launcher.stem
        with zipfile.ZipFile(launcher) as zf:
            if "__main__.py" not in zf.namelist():
                raise SystemExit(
                    f"ERROR: {launcher} names {frozen_interpreter(launcher)} "
                    "but holds no __main__.py to relocate"
                )
            payload = zf.read("__main__.py")
        (scripts / f"{name}-script.py").write_bytes(payload)
        run = f'"%~dp0..\\python.exe" -s -P "%~dp0{name}-script.py" %*'
        (scripts / f"{name}.cmd").write_bytes(
            "".join(f"{line}\r\n" for line in (*_SHIM_HEAD, run)).encode("ascii")
        )
        launcher.unlink()
        replaced.append(name)
    return replaced


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True, type=Path, help="Windows backend bundle root")
    args = parser.parse_args(argv)
    replaced = relocate_scripts(args.root)
    print(f"relocated {len(replaced)} Scripts launcher(s): {', '.join(replaced) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
