"""Fixtures for the ``test_skill_versioning*`` suite.

The autouse ``_simulated_windows_opened_identity`` fixture and the ``loader`` /
``uninitialized_loader`` / ``snapshot_reader`` / ``private_state_mode`` fixtures
are scoped to this package so they reach every sibling module without an import
and never touch a test outside it. The helper functions and the module-level
constants these modules share live in ``skill_versioning_support``.
"""

from __future__ import annotations

import hashlib
import os

import pytest

import kiro_crew.skills as skills_mod
from kiro_crew.skills import SkillsLoader

_HOST_WINDOWS = os.name == "nt"


@pytest.fixture()
def loader():
    loader = SkillsLoader(install_builtins=False)
    skills_mod.initialize_auto_skill_private_authority(
        skills_root=loader._dir,
        data_home=loader._private_root().parents[1],
    )
    return loader


@pytest.fixture()
def uninitialized_loader():
    return SkillsLoader(install_builtins=False)


@pytest.fixture(autouse=True)
def _simulated_windows_opened_identity(_floor_monkeypatch):
    from kiro_crew import sandbox

    with skills_mod._AUTHORITY_HOME_IDENTITIES_LOCK:
        skills_mod._AUTHORITY_HOME_IDENTITIES.clear()
    _floor_monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    _floor_monkeypatch.setattr(sandbox, "_macos_sandbox_state", lambda: None)
    real_identity = skills_mod.platform_compat.opened_file_identity
    real_fstat = os.fstat

    def opened_identity(fd):
        if skills_mod.platform_compat.IS_WINDOWS and not _HOST_WINDOWS:
            opened = real_fstat(fd)
            payload = hashlib.sha256(f"{opened.st_dev}:{opened.st_ino}".encode("ascii")).digest()[
                :16
            ]
            return opened.st_dev, b"F128" + payload
        return real_identity(fd)

    _floor_monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_file_identity",
        opened_identity,
    )
    _floor_monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        lambda: None,
    )
    _floor_monkeypatch.setattr(
        skills_mod,
        "_agent_sandbox_is_delegated",
        lambda: False,
    )


@pytest.fixture(params=("native", "fallback"))
def snapshot_reader(request, monkeypatch):
    if request.param == "fallback":
        monkeypatch.setattr(
            SkillsLoader,
            "_skill_tree_snapshot",
            staticmethod(SkillsLoader._skill_tree_snapshot_by_name),
        )
    return request.param


@pytest.fixture(params=("native", "fallback"))
def private_state_mode(request, monkeypatch):
    if request.param == "fallback":
        monkeypatch.setattr(skills_mod, "_DIR_FD_SUPPORTED", False)
    return request.param
