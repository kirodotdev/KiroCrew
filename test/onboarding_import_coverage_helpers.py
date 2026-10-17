"""Helpers shared by the ``test_onboarding_import_coverage*.py`` modules.

``_scan`` builds a scan wired the way ``_scan_source`` wires a real one and
``_reasons`` reads its skip reasons; ``_POSIX_FS_ONLY`` skips, on Windows, the cases
that assert POSIX filesystem semantics. ``_api`` is not shared from here: each module
defines its own, so ``test_onboarding_import_refactor_contract_patch_guards.py`` still sees which
local names hold the facade when it scans that module's patches.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import onboarding_scan, onboarding_sources

# These cases encode POSIX filesystem semantics: which directory names the OS
# refuses, and how a path renders inside a returned mapping. Windows disagrees on
# both -- it accepts names POSIX rejects (so the "unnameable" fixture succeeds
# and then collides, WinError 183) and renders separators as backslashes, so the
# mapping comparison differs on characters, not behaviour.
#
# Skipping them on Windows costs ZERO coverage: ci.yml measures coverage on the
# ubuntu 3.12 shards only (the Windows lane runs --no-cov), so these lines are
# still counted on the lane that reports the number.
_POSIX_FS_ONLY = pytest.mark.skipif(
    os.name == "nt",
    reason="asserts POSIX filesystem semantics (illegal names, path separators); "
    "coverage is measured on the ubuntu 3.12 shards, so this loses none",
)


def _scan(tmp_path: Path, source_id: str = "hermes") -> Any:
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    # Wired the way `_scan_source` wires a real scan: the MCP projection asks the
    # live registry which server names are managed.
    return onboarding_scan._Scan(
        source_id=source_id,
        root=root,
        user_home=tmp_path,
        managed_mcp_names=onboarding_sources._managed_mcp_names,
    )


def _reasons(scan: Any) -> set[str]:
    return {entry["reason"] for entry in scan.skipped}
