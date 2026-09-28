#!/usr/bin/env python3
"""Verify built artifacts carry the whole vendored llama.cpp native-library closure.

In-process embeddings load ``libllama`` by base name through ctypes, so one
absent file makes the runtime unusable and memory silently degrades to keyword
search behind a single WARNING. A published Linux wheel shipped exactly that
way: ``MANIFEST.in``'s ``global-exclude *.so`` strips precisely ``libllama.so``
(every other Linux lib ends ``.so.0``, and the macOS/Windows libs are
``.dylib``/``.dll``), and ``python -m build`` builds the wheel FROM the sdist.

Checked against the closure DECLARED in ``embeddings._REQUIRED_VENDORED_LIBS``,
never a glob over the checkout: a glob can only prove the files present were
shipped, so if a lib went missing from the source tree too it would pass
vacuously.

Both artifacts are inspected because the wheel is what users install while the
sdist is what it is built from, and naming which artifact lost a file turns a
"library not found" runtime mystery into a one-line diagnosis.

Usage: ``python scripts/verify_vendored_payload.py [dist-dir]`` (default
``dist``). Exits non-zero listing every missing member.
"""

from __future__ import annotations

import ast
import pathlib
import sys
import tarfile
import zipfile

_SDIST_SUFFIX = ".tar.gz"


def _read_lib_declarations(source: pathlib.Path) -> tuple[str, dict[str, tuple[str, ...]]]:
    """Read the runtime's literal declarations without loading its dependencies."""
    names = {"_LIBS_DIR_NAME", "_REQUIRED_VENDORED_LIBS"}
    declarations = {}
    for node in ast.parse(source.read_text(encoding="utf-8"), filename=str(source)).body:
        value: ast.expr | None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in names:
                if value is None:
                    raise ValueError(f"{source}: {target.id} has no literal value")
                declarations[target.id] = ast.literal_eval(value)
    # A moved or computed declaration must fail the gate, never check zero libs.
    missing = names - declarations.keys()
    if missing:
        raise ValueError(f"{source}: missing literal declarations: {sorted(missing)}")
    return declarations["_LIBS_DIR_NAME"], declarations["_REQUIRED_VENDORED_LIBS"]


def main(argv: list[str]) -> int:
    dist = pathlib.Path(argv[1] if len(argv) > 1 else "dist")
    # CI installs build tools, not runtime dependencies such as PyYAML.
    source = pathlib.Path(__file__).resolve().parents[1] / "src/kiro_crew/embeddings.py"
    libs_dir_name, required_libs = _read_lib_declarations(source)

    try:
        wheel = next(iter(sorted(dist.glob("*.whl"))))
        sdist = next(iter(sorted(dist.glob(f"*{_SDIST_SUFFIX}"))))
    except StopIteration:
        print(
            f"expected a wheel AND an sdist in {dist}/, found: "
            f"{sorted(p.name for p in dist.glob('*'))}",
            file=sys.stderr,
        )
        return 2

    wheel_names = set(zipfile.ZipFile(wheel).namelist())
    with tarfile.open(sdist) as tar:
        sdist_names = set(tar.getnames())
    prefix = sdist.name[: -len(_SDIST_SUFFIX)]

    failures: list[str] = []
    for plat, required in sorted(required_libs.items()):
        for name in required:
            rel = f"kiro_crew/_vendor/{libs_dir_name}/{plat}/{name}"
            if rel not in wheel_names:
                failures.append(f"{wheel.name}: missing {rel}")
            if f"{prefix}/src/{rel}" not in sdist_names:
                failures.append(f"{sdist.name}: missing src/{rel}")

    if failures:
        print("vendored llama.cpp payload incomplete:", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1

    print(f"vendored llama.cpp payload complete in {wheel.name} and {sdist.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
