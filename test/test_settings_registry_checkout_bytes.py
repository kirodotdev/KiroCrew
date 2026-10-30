"""The committed generated files ``gen-ui-index --check`` reads keep LF bytes on every checkout.

``website/scripts/lib/ui-index.mjs`` (``checkSettingsExtraction``) compares the
committed ``src/kiro_crew/docs/settings-registry.generated.json`` with the live
extraction byte for byte, and the extraction writes LF. Under
``core.autocrlf=true`` (the Git-for-Windows default, and the Windows runners')
an unpinned JSON file is checked out with CRLF, so every Windows build that runs
``gen-ui-index --check`` fails with "settings-registry.generated.json does not
match the live settings extraction" while Linux and macOS pass.

``website/scripts/gen-ui-index.mjs --check`` does the same with the committed
``ui-index.generated.json``. Release builds set
``KC_UI_INDEX_STRICT=1``, so a CRLF checkout there failed the v0.10.0-rc.1
Windows build with "ui-index.generated.json is stale" while the index was fresh.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from git_maintenance_helpers import git_maintenance_off_entries

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PINNED = (
    "src/kiro_crew/docs/settings-registry.generated.json",
    "src/kiro_crew/docs/ui-index.generated.json",
)


@pytest.mark.parametrize("pinned", _PINNED)
@pytest.mark.parametrize("autocrlf", ["true", "input", "false"])
def test_checkout_preserves_the_generated_bytes(tmp_path: Path, autocrlf: str, pinned: str) -> None:
    committed = (_REPO_ROOT / pinned).read_bytes()
    assert b"\r\n" not in committed, f"the committed {pinned} itself must be LF"
    repo = tmp_path / "repo"
    (repo / Path(pinned).parent).mkdir(parents=True)
    (repo / pinned).write_bytes(committed)
    (repo / ".gitattributes").write_bytes((_REPO_ROOT / ".gitattributes").read_bytes())
    (repo / "newline-control.txt").write_bytes(b"control\n")
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    # Real Git conversion, without inheriting host or repository overrides.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    env.update(git_maintenance_off_entries(env))
    git = ["git", "-c", f"core.autocrlf={autocrlf}", "-c", f"core.attributesFile={os.devnull}"]
    for args in (
        ["init", "--quiet"],
        ["add", "--", ".gitattributes", pinned, "newline-control.txt"],
        [
            "checkout-index",
            f"--prefix={checkout.as_posix()}/",
            "--",
            pinned,
            "newline-control.txt",
        ],
    ):
        subprocess.run(git + args, cwd=repo, env=env, capture_output=True, check=True, timeout=30)

    # The control proves autocrlf=true really performs a CRLF checkout here.
    expected_control = b"control\r\n" if autocrlf == "true" else b"control\n"
    assert (checkout / "newline-control.txt").read_bytes() == expected_control
    assert (checkout / pinned).read_bytes() == committed
