"""Make pip's console scripts in the macOS/Linux bundle's ``bin/`` relocatable.

``build_backend`` runs ``pip install`` with the bundle's own
``bin/python3.12``, so pip writes every console script in ``bin/`` (``httpx``,
``tqdm``, ``f2py``, ...) with a header naming that interpreter by the BUILD
machine's absolute path: ``#!<abs>/bin/python3.12`` or, when that line is too
long or holds a space, distlib's ``/bin/sh`` form whose second line is
``'''exec' "<abs>/bin/python3.12" "$0" "$@"``.  Once the app is installed
anywhere else that path does not exist, and ``env._interpreter_script_dirs``
puts this ``bin/`` on every agent's ``PATH``, so an agent running ``httpx`` by
name gets "bad interpreter" instead of the tool.

For each script whose header names an interpreter under the bundle root, this
helper swaps the header for one that is valid both as ``/bin/sh`` and as
Python: sh drops ``PYTHONPATH``/``PYTHONHOME`` (the same scrub ``bin/kirocrew``
does) and execs the same-named interpreter beside the script with ``-s -P``;
Python reads those lines as one string and runs the body pip wrote.  Everything
else in ``bin/`` (the interpreter binaries, ``bin/kirocrew``, python-build-
standalone's own already-relocatable scripts, ``/usr/bin/env`` shebangs) is
left byte-identical.

``packaging/build-desktop.sh`` (``build_backend``) runs it after writing
``bin/kirocrew`` and before anything is signed: codesign seals every file under
the macOS app, so the rewrite cannot happen later.  The Windows half is
``packaging/relocate_windows_scripts.py``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SH_SHEBANG = b"#!/bin/sh"
_SH_EXEC = b"'''exec' "
_SH_ARGS = b' "$0" "$@"'


def _unquote(word: str) -> str:
    return word[1:-1] if len(word) >= 2 and word[0] == word[-1] == '"' else word


def frozen_interpreter(script: Path, roots: tuple[str, ...]) -> tuple[str, int] | None:
    """The bundle interpreter ``script``'s header names, and the header's line count.

    ``None`` when the file is not a pip-style script naming a path under one of
    ``roots`` (each an absolute bundle-root prefix ending in ``/``).
    """
    with script.open("rb") as fh:
        head = fh.read(4096)
    if not head.startswith(b"#!"):
        return None
    lines = head.split(b"\n", 3)
    first = lines[0].rstrip(b"\r")
    if first == _SH_SHEBANG and len(lines) > 2 and lines[2].rstrip(b"\r") == b"' '''":
        second = lines[1].rstrip(b"\r")
        if not (second.startswith(_SH_EXEC) and second.endswith(_SH_ARGS)):
            return None
        interpreter = _unquote(second[len(_SH_EXEC) : -len(_SH_ARGS)].decode("utf-8", "replace"))
        header_lines = 3
    else:
        interpreter = first[2:].decode("utf-8", "replace").strip().split(" ", 1)[0]
        header_lines = 1
    if not interpreter.startswith(roots):
        return None
    return interpreter, header_lines


def relocatable_header(interpreter_name: str) -> bytes:
    """A header sh runs and Python skips, starting ``interpreter_name`` beside the script.

    sh reads ``'''':`` as the no-op ``:``; Python reads everything from it to
    the closing ``'''`` as one string expression.
    """
    # shortcut: "$0" is the path the script was started by, which is the real
    # bin/ entry for a PATH lookup; a user symlink to the script elsewhere would
    # need bin/kirocrew's readlink walk.
    return (
        "#!/bin/sh\n"
        "'''':\n"
        "unset PYTHONPATH PYTHONHOME\n"
        f'exec "$(dirname -- "$0")/{interpreter_name}" -s -P "$0" "$@"\n'
        "'''\n"
    ).encode("ascii")


def relocate_scripts(root: Path) -> list[str]:
    """Rewrite each frozen console script under ``root/bin``; return their names."""
    bin_dir = root / "bin"
    if not bin_dir.is_dir():
        return []
    # pip names sys.executable as invoked; also accept the resolved spelling
    # (macOS /tmp is /private/tmp) so neither form slips through.
    roots = tuple(dict.fromkeys(f"{p}/" for p in (root.absolute(), root.resolve())))
    replaced = []
    for script in sorted(bin_dir.iterdir()):
        if script.is_symlink() or not script.is_file():
            continue
        found = frozen_interpreter(script, roots)
        if found is None:
            continue
        interpreter, header_lines = found
        body = script.read_bytes().split(b"\n", header_lines)[header_lines:]
        # write_bytes truncates the same inode, so the exec bits pip set stay.
        script.write_bytes(relocatable_header(Path(interpreter).name) + b"".join(body))
        replaced.append(script.name)
    return replaced


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True, type=Path, help="POSIX backend bundle root")
    args = parser.parse_args(argv)
    replaced = relocate_scripts(args.root)
    print(f"relocated {len(replaced)} bin/ script(s): {', '.join(replaced) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
