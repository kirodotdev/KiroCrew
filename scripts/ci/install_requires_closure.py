"""Write setup.cfg's ``install_requires`` as a requirements file for one Linux arch.

The wheel-closure CI job feeds the result to ``pip download --only-binary=:all:``
with a manylinux ``--platform``. pip evaluates environment markers against the
machine it runs on, not against ``--platform``, so this script evaluates each
marker for the target Linux arch itself and writes only the entries that apply,
markers removed. Extras and version specifiers are kept as written.
"""

from __future__ import annotations

import argparse
import configparser
import sys
from pathlib import Path

from packaging.requirements import Requirement

_REPO_ROOT = Path(__file__).resolve().parents[2]


def install_requires(setup_cfg_text: str) -> list[str]:
    """Return the ``[options] install_requires`` entries, comments and blanks dropped."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(setup_cfg_text)
    raw = parser.get("options", "install_requires", fallback="")
    entries = []
    for line in raw.splitlines():
        line = line.split(" #", 1)[0].strip()
        if line and not line.startswith("#"):
            entries.append(line)
    return entries


def linux_environment(machine: str) -> dict[str, str]:
    """Marker environment of a CPython 3.12 Linux host on ``machine``."""
    return {
        "implementation_name": "cpython",
        "os_name": "posix",
        "platform_machine": machine,
        "platform_python_implementation": "CPython",
        "platform_system": "Linux",
        "python_full_version": "3.12.0",
        "python_version": "3.12",
        "sys_platform": "linux",
    }


def requirements_for(entries: list[str], machine: str) -> list[str]:
    """Entries whose marker holds on ``machine``, each written without its marker."""
    env = linux_environment(machine)
    out = []
    for entry in entries:
        req = Requirement(entry)
        if req.marker is not None and not req.marker.evaluate(env):
            continue
        req.marker = None
        out.append(str(req))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--machine", required=True, help="platform_machine, e.g. x86_64 or aarch64")
    ap.add_argument("--out", required=True, type=Path, help="requirements file to write")
    ap.add_argument("--setup-cfg", type=Path, default=_REPO_ROOT / "setup.cfg")
    args = ap.parse_args(argv)
    entries = install_requires(args.setup_cfg.read_text(encoding="utf-8"))
    if not entries:
        print(f"no install_requires found in {args.setup_cfg}", file=sys.stderr)
        return 1
    lines = requirements_for(entries, args.machine)
    args.out.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
