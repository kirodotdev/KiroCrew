"""The desktop shell's version as ``packaging/build-desktop.sh`` resolves it.

electron-builder packs ``-c.extraMetadata.version=$KC_VERSION`` into the
``package.json`` inside ``app.asar``, and that value is what
``app.getVersion()``, Settings > About and the artifact names report. An edition
that repackages one core release as several builds of its own stamps its build
into ``website/electron/package.json`` as semver (``0.13.0.11`` as
``0.13.0-11``) before it runs the script, so the script must carry that stamp
into ``KC_VERSION`` rather than replace it with the core's ``__version__``,
while still refusing a stamp that names a build this core is not.

The tests extract the version block from the shipped script (not a copy, so a
revert is what runs here) and run it against a scratch tree.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "packaging" / "build-desktop.sh"
PRODUCTION = "KiroCrew"  # brand-ok: electron-builder productName identifier

runs_the_block = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="build-desktop.sh is a bash script",
)


def _extract_block() -> str:
    """From ``ELECTRON_DIR=`` through the channel-identity ``case``: the
    ``__version__`` parse, the shell-version resolution and the product name
    derived from the result, ending before the ``log`` helper."""
    text = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r'^(ELECTRON_DIR="\$ROOT/website/electron"\n.*?)\nlog\(\) \{', text, re.M | re.S)
    assert m, "the version block was not found in packaging/build-desktop.sh"
    return m.group(1)


def _run(tmp_path: Path, core: str, stamped: str) -> tuple[subprocess.CompletedProcess, str, str]:
    root = tmp_path / "root"
    (root / "src" / "kiro_crew").mkdir(parents=True)
    (root / "src" / "kiro_crew" / "__init__.py").write_text(
        f'"""Kiro Crew."""\n\n__version__ = "{core}"\n', encoding="utf-8"
    )
    electron = root / "website" / "electron"
    electron.mkdir(parents=True)
    # The shape the edition's stamp writes: JSON.stringify(p, null, 2). A nested
    # "version" ahead of the top-level one must not be read in its place.
    pkg = {"build": {"version": "0.0.1-9"}, "name": "kirocrew-desktop", "version": stamped}
    (electron / "package.json").write_text(json.dumps(pkg, indent=2) + "\n", encoding="utf-8")
    script = (
        "set -euo pipefail\n"
        f"ROOT={shlex.quote(str(root))}\n"
        + _extract_block()
        + '\nprintf "%s\\n%s\\n" "$KC_VERSION" "$PRODUCT_NAME"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    version, product = proc.stdout.splitlines()
    return proc, version, product


@runs_the_block
@pytest.mark.parametrize("build", ["1", "11", "250"])
def test_an_edition_build_stamp_is_the_shell_version(tmp_path: Path, build: str) -> None:
    proc, version, product = _run(tmp_path, "0.13.0", f"0.13.0-{build}")
    assert version == f"0.13.0-{build}"
    assert product == PRODUCTION
    assert "WARNING" not in proc.stderr


@runs_the_block
@pytest.mark.parametrize(
    "core",
    ["0.13.0", "0.13.0-insider.4", "0.13.0-nightly.20261107"],
)
def test_a_package_json_equal_to_the_core_version_changes_nothing(
    tmp_path: Path, core: str
) -> None:
    # The public release lanes stamp __version__ and package.json to one value.
    proc, version, _ = _run(tmp_path, core, core)
    assert version == core
    assert proc.stderr == ""


@runs_the_block
@pytest.mark.parametrize(
    "stamped",
    [
        "0.12.0-11",  # another release's build
        "0.13.1-11",
        "0.13.0.11",  # not semver: electron-builder refuses it
        "0.13.0-rc.1",  # a prerelease stamp, not a build number
        "0.13.0-11-2",  # a second segment
        "0.13.0-",  # an empty build
        "0.13.0-1a",  # a non-numeric build
        "0.13.01-1",  # the base is only a prefix
    ],
)
def test_a_stamp_that_is_not_a_build_of_the_core_is_ignored(tmp_path: Path, stamped: str) -> None:
    proc, version, _ = _run(tmp_path, "0.13.0", stamped)
    assert version == "0.13.0"
    assert f"'{stamped}' is not a build of 0.13.0" in proc.stderr


@runs_the_block
@pytest.mark.parametrize(
    "core, product",
    [
        ("0.13.0-nightly.20261107", "KiroCrew Nightly"),
        ("0.13.0-insider.4", PRODUCTION),
        ("0.13.0rc7", PRODUCTION),
    ],
)
def test_a_build_suffix_over_a_prerelease_core_is_ignored(
    tmp_path: Path, core: str, product: str
) -> None:
    # The composed string would not name one release, so the core's own stamp
    # and the identity it derives stay.
    proc, version, got_product = _run(tmp_path, core, f"{core}-3")
    assert version == core
    assert got_product == product
    assert "is not a build of" in proc.stderr


def test_the_resolved_version_is_what_electron_builder_packs() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'EB_ARGS=( "-c.extraMetadata.version=$KC_VERSION" )' in text
    assert text.index('KC_VERSION="$PKG_VERSION"') < text.index("-c.extraMetadata.version=")
