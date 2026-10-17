"""The secret vault under a relocated ``KIROCREW_HOME`` is masked like the default-home vault.

The tier lists name the vault only at its two ``$HOME``-joined spellings, so a data home
outside ``$HOME`` needs the resolved ``config_dir()/.vault`` added the way the relocated
``kas`` leaf is. Every case uses a tmp home and a fake secret; no real vault is read.
"""

from __future__ import annotations

import os
import sys

import pytest

from kiro_crew import sandbox, sandbox_plan

_MODES = ("standard", "cc", "strict")
_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sandbox backends only")


@pytest.fixture(autouse=True)
def _no_host_ssh_probe(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: True)


def _tmp_home(monkeypatch: pytest.MonkeyPatch, tmp_path, *, relocated: bool) -> str:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    if relocated:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "relocated-crew"))
    else:
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
    return str(home)


def _store_fake_secret() -> str:
    """Store a fake secret through the real vault and return the vault directory."""
    from kiro_crew.config.paths import config_dir
    from kiro_crew.secrets.vault import SecretVault

    SecretVault(config_dir()).set_sync("r18_fake", "not-a-real-secret")
    vault_dir = os.path.join(str(config_dir()), ".vault")
    assert os.path.isfile(os.path.join(vault_dir, ".vault_key"))
    return vault_dir


def _covered(target: str, masked: list[str]) -> bool:
    target = os.path.normpath(target)
    for entry in masked:
        entry = os.path.normpath(entry)
        if target == entry or target.startswith(entry.rstrip(os.sep) + os.sep):
            return True
    return False


def _masked(backend: str, mode: str) -> list[str]:
    return list(sandbox._spawn_plan(backend, mode).sensitive_dirs)


@_POSIX_ONLY
@pytest.mark.parametrize("backend", [sandbox_plan.BACKEND_NAMESPACE, sandbox_plan.BACKEND_SEATBELT])
@pytest.mark.parametrize("mode", _MODES)
def test_relocated_vault_is_masked_on_every_backend(backend, mode, tmp_path, monkeypatch) -> None:
    home = _tmp_home(monkeypatch, tmp_path, relocated=True)
    vault_dir = _store_fake_secret()
    assert not vault_dir.startswith(home + os.sep), "precondition: the data home is relocated"
    masked = _masked(backend, mode)
    assert _covered(vault_dir, masked), f"{backend}/{mode}: relocated vault {vault_dir} unmasked"


@_POSIX_ONLY
@pytest.mark.parametrize("mode", _MODES)
def test_relocated_vault_seatbelt_profile_denies_read_and_write(mode, tmp_path, monkeypatch):
    _tmp_home(monkeypatch, tmp_path, relocated=True)
    vault_dir = _store_fake_secret()
    profile = sandbox._build_seatbelt_profile(mode)
    assert f'(deny file-read* (subpath "{vault_dir}"))' in profile
    assert f'(deny file-write* (subpath "{vault_dir}"))' in profile


@_POSIX_ONLY
@pytest.mark.parametrize("backend", [sandbox_plan.BACKEND_NAMESPACE, sandbox_plan.BACKEND_SEATBELT])
@pytest.mark.parametrize("mode", _MODES)
def test_default_home_vault_masks_are_unchanged(backend, mode, tmp_path, monkeypatch) -> None:
    home = _tmp_home(monkeypatch, tmp_path, relocated=False)
    vault_dir = _store_fake_secret()
    masked = _masked(backend, mode)
    assert _covered(vault_dir, masked)
    # The vault appears only at the two $HOME-joined spellings the tier lists carry.
    assert sorted(m for m in masked if os.path.basename(m) == ".vault") == sorted(
        os.path.join(home, prefix, ".vault") for prefix in sandbox._CREW_HOME_PREFIXES
    )
    # A vault leaf re-anchored at the data home adds nothing when that home is under $HOME.
    assert sandbox._relocated_crew_targets((".vault",)) == []


@_POSIX_ONLY
def test_vault_save_and_load_round_trip_under_relocated_home(tmp_path, monkeypatch) -> None:
    from kiro_crew.config.paths import config_dir
    from kiro_crew.secrets.vault import SecretVault

    _tmp_home(monkeypatch, tmp_path, relocated=True)
    _store_fake_secret()
    for backend in (sandbox_plan.BACKEND_NAMESPACE, sandbox_plan.BACKEND_SEATBELT):
        for mode in _MODES:
            sandbox._spawn_plan(backend, mode)
    value = SecretVault(config_dir()).get("r18_fake")
    assert value is not None and value.reveal() == "not-a-real-secret"
    SecretVault(config_dir()).set_sync("r18_fake", "second-fake-value")
    again = SecretVault(config_dir()).get("r18_fake")
    assert again is not None and again.reveal() == "second-fake-value"
