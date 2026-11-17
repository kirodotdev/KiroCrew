"""The macOS/Linux bundle's pip console scripts start wherever the app is installed.

``build_backend`` in ``packaging/build-desktop.sh`` runs ``pip install`` with the
bundle's own ``bin/python3.12``, so pip writes each console script in ``bin/``
with a header naming the BUILD machine's absolute interpreter path.
``packaging/relocate_posix_scripts.py`` rewrites those headers; these tests pin
the rewrite against both header forms distlib writes, prove a rewritten script
runs after the bundle moves, and pin that ``build_backend`` calls it before
anything downstream (gates, signing) sees the tree.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "packaging" / "build-desktop.sh"
RELOCATE = ROOT / "packaging" / "relocate_posix_scripts.py"

_BODY = (
    "import os\nimport sys\n"
    "print('ran', sys.flags.no_user_site, sys.flags.safe_path, 'PYTHONPATH' in os.environ)\n"
)


def _load_relocate():
    spec = importlib.util.spec_from_file_location("relocate_posix_scripts", RELOCATE)
    assert spec is not None and spec.loader is not None, f"{RELOCATE} is missing"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _simple(interpreter: Path) -> str:
    return f"#!{interpreter}\n"


def _long(interpreter: Path) -> str:
    # distlib's form for a shebang that is too long or holds a space.
    return f"#!/bin/sh\n'''exec' \"{interpreter}\" \"$0\" \"$@\"\n' '''\n"


def _write_script(path: Path, header: str, body: str = _BODY) -> None:
    path.write_text(header + body, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bundle layout")
def test_rewrites_only_scripts_naming_the_bundle_interpreter(tmp_path: Path) -> None:
    relocate = _load_relocate()
    root = tmp_path / "kirocrew-backend"
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    frozen = bin_dir / "python3.12"
    frozen.write_bytes(b"\x7fELF not a script")
    _write_script(bin_dir / "httpx", _simple(frozen))
    _write_script(bin_dir / "tqdm", _long(frozen))
    untouched = {
        "kirocrew": '#!/bin/bash\nexec "$DIR/python3.12" -s -P -m kiro_crew "$@"\n',
        "envtool": "#!/usr/bin/env python3\nprint('x')\n",
        "pip3": "#!/bin/sh\n'''exec' \"$(dirname -- \"$0\")/python3.12\" \"$0\" \"$@\"\n' '''\n",
        "other-python": "#!/opt/elsewhere/bin/python3.12\nprint('x')\n",
    }
    for name, text in untouched.items():
        _write_script(bin_dir / name, text, body="")
    (bin_dir / "python3").symlink_to("python3.12")

    replaced = relocate.relocate_scripts(root)

    assert replaced == ["httpx", "tqdm"]
    for name in replaced:
        script = bin_dir / name
        text = script.read_text(encoding="utf-8")
        assert str(root) not in text, text
        assert text == relocate.relocatable_header("python3.12").decode() + _BODY
        assert os.access(script, os.X_OK)
        compile(text, name, "exec")
    for name, text in untouched.items():
        assert (bin_dir / name).read_text(encoding="utf-8") == text
    assert frozen.read_bytes() == b"\x7fELF not a script"
    assert relocate.relocate_scripts(root) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bundle layout and /bin/sh")
# distlib only writes the simple form for a path with no space in it.
@pytest.mark.parametrize(
    ("header", "build_dir"),
    [(_simple, "build-machine"), (_long, "build machine")],
    ids=["simple", "long"],
)
def test_rewritten_script_runs_after_the_bundle_moves(
    tmp_path: Path, header, build_dir: str
) -> None:
    relocate = _load_relocate()
    build = tmp_path / build_dir / "kirocrew-backend"
    bin_dir = build / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python3.12").symlink_to(sys.executable)
    _write_script(bin_dir / "demo", header(bin_dir / "python3.12"))

    assert relocate.relocate_scripts(build) == ["demo"]
    installed = tmp_path / "Installed App" / "kirocrew-backend"
    installed.parent.mkdir()
    build.rename(installed)

    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env["PYTHONPATH"] = str(tmp_path / "foreign")
    result = subprocess.run(
        [str(installed / "bin" / "demo")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ran 1 True False"


def test_build_backend_relocates_bin_scripts_after_the_kirocrew_launcher() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    start = script.index("\nbuild_backend() {")
    body = script[start : script.index("\n}\n", start)]
    call = '"$out/bin/python3.12" -B -s "$ROOT/packaging/relocate_posix_scripts.py" --root "$out"'
    assert call in body, "build_backend must relocate every bin/ console script"
    assert body.index('chmod +x "$out/bin/kirocrew"') < body.index(call)
    # Before the gates that read the tree and long before electron-builder signs it.
    assert body.index(call) < body.index("Verifying self-containment")
