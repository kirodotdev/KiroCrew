"""Installers install pinned versions, never whatever the registry serves today.

``install.sh``, ``setup.sh`` and ``minimal_install.sh`` put the optional
claude-agent-acp backend on PATH with a global npm install. That install runs
the package's code as the user, so it takes the exact version recorded in
``packaging/acp-adapters/package.json`` (which Dependabot bumps after a 7-day
cooldown) and skips the install when the pin cannot be read, instead of
falling back to an unpinned name.

Static checks read the shipped scripts; the behavioural checks source the real
``scripts/lib/acp-adapter-pin.sh`` rather than a copy, so they cannot drift.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from kiro_crew.acp.client_claude_session import CLAUDE_ACP_SETTING_SOURCES_MIN_VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]
ACP_PIN_JSON = REPO_ROOT / "packaging" / "acp-adapters" / "package.json"
ACP_PIN_LIB = REPO_ROOT / "scripts" / "lib" / "acp-adapter-pin.sh"
ACP_PKG = "@agentclientprotocol/claude-agent-acp"
INSTALLERS = ("install.sh", "setup.sh", "minimal_install.sh")

# An npm global install that actually runs (not text inside echo/detail/warn).
_NPM_GLOBAL_RUN = re.compile(r"^\s*(?:if\s+!?\s*)?npm\s+(?:install|i)\s+(?:-g|--global)\s+(\S+)")


def _pinned_version() -> str:
    data = json.loads(ACP_PIN_JSON.read_text(encoding="utf-8"))
    return data["dependencies"][ACP_PKG]


@pytest.mark.parametrize("name", INSTALLERS)
def test_installer_global_npm_installs_use_the_pinned_spec(name: str) -> None:
    text = (REPO_ROOT / name).read_text(encoding="utf-8")
    runs = [
        m.group(1)
        for line in text.splitlines()
        if not line.lstrip().startswith("#") and (m := _NPM_GLOBAL_RUN.match(line))
    ]
    assert runs, f"{name}: no global npm install found; did the step move?"
    assert all(
        arg == '"$ACP_NPM_SPEC"' for arg in runs
    ), f"{name}: global npm install must take the pinned spec from acp_adapter_spec, got {runs}"
    assert "scripts/lib/acp-adapter-pin.sh" in text, f"{name} must source the shared pin helper"


def test_pinned_acp_version_is_exact_and_above_the_settings_floor() -> None:
    version = _pinned_version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"pin must be an exact x.y.z, got {version!r}"
    assert tuple(int(p) for p in version.split(".")) >= CLAUDE_ACP_SETTING_SOURCES_MIN_VERSION


def test_dependabot_refreshes_the_acp_pin_with_a_cooldown() -> None:
    text = (REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    block = text.split('directory: "/packaging/acp-adapters"', 1)
    assert len(block) == 2, "dependabot must watch packaging/acp-adapters"
    entry = block[1].split("- package-ecosystem:", 1)[0]
    assert re.search(r"default-days:\s*7\b", entry), "the ACP pin bump needs the 7-day cooldown"


def _spec(tmp_path: Path, version: str | None) -> subprocess.CompletedProcess[str]:
    """Run ``acp_adapter_spec`` against a repo root whose manifest pins *version*.

    ``version=None`` uses the real repo root and its committed manifest.
    """
    root = REPO_ROOT
    if version is not None:
        root = tmp_path / "repo"
        (root / "packaging" / "acp-adapters").mkdir(parents=True)
        (root / "packaging" / "acp-adapters" / "package.json").write_text(
            json.dumps({"dependencies": {ACP_PKG: version}}), encoding="utf-8"
        )
    return subprocess.run(
        ["sh", "-c", '. "$1" && acp_adapter_spec "$2"', "sh", str(ACP_PIN_LIB), str(root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )


_needs_node = pytest.mark.skipif(
    os.name == "nt" or shutil.which("node") is None or shutil.which("sh") is None,
    reason="sources a POSIX shell helper that reads the manifest with node",
)


@_needs_node
def test_spec_helper_prints_the_committed_pin(tmp_path: Path) -> None:
    proc = _spec(tmp_path, None)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"{ACP_PKG}@{_pinned_version()}"


@_needs_node
@pytest.mark.parametrize("bad", ["^0.93.0", "~0.93.0", "latest", "", ">=0.84.0", "0.93.0 || 1"])
def test_spec_helper_refuses_anything_but_an_exact_version(tmp_path: Path, bad: str) -> None:
    proc = _spec(tmp_path, bad)
    assert proc.returncode != 0
    assert proc.stdout == ""
