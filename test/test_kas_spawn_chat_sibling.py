"""A Crew-owned KAS spawn enters through ``kiro-cli-chat``, not the ``kiro-cli`` launcher.

The POSIX ``kiro-cli`` is a launcher that checks its OWN sign-in before it execs
``kiro-cli-chat`` for ``acp`` -- with or without ``--auth-method cli``. A Crew-owned
spawn exists for the host where kiro-cli is signed out (Crew's vault holds the
identity), so through the launcher it died with ``You are not logged in`` and the
engine never asked Crew for the credential. ``kiro-cli-chat acp --agent-engine v3``
skips the gate (measured on kiro-cli 2.25.0 in a container with kiro-cli signed out
and an IdC identity in Crew's vault: the launcher exits 1, the chat binary starts KAS
in ``--auth=acp-callback`` and the turn completes). Windows ships ONE self-contained
``kiro-cli.exe`` that IS the chat-cli crate (no q_cli launcher in the MSI), so there
is nothing to swap there and :func:`chat_sibling` answers ``None``.

Two layers are pinned here: :func:`kiro_crew.kiro_cli.chat_sibling` (pure path work,
asked with an explicit platform) and the KAS harness plan, which consults it on the
Crew-owned branch only. The harness tests stub ``chat_sibling`` so they describe the
plumbing on every CI host rather than inheriting the host's ``sys.platform``; the
execute-bit and symlink cases are POSIX facts and are skipped where the host has
neither (Windows reports every file executable and its CI account cannot symlink).
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew import kiro_cli
from kiro_crew.acp import client as client_mod
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.harness import kas as kas_mod
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp.kas_transport import KAS_RELAY_AUTH_FLAG
from kiro_crew.acp.types import ACP_BACKEND_KAS

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX execute bit; Windows reports every file executable"
)


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def install(tmp_path: Path) -> Path:
    """An upstream POSIX layout: launcher and chat binary side by side."""
    _executable(tmp_path / "kiro-cli")
    _executable(tmp_path / "kiro-cli-chat")
    return tmp_path


# ── chat_sibling ──


def test_launcher_with_a_chat_binary_beside_it_yields_the_chat_binary(install):
    assert kiro_cli.chat_sibling(str(install / "kiro-cli"), "linux") == str(
        install / "kiro-cli-chat"
    )


def test_launcher_alone_yields_nothing(tmp_path):
    """A wrapper directory that ships only ``kiro-cli`` keeps the wrapper."""
    _executable(tmp_path / "kiro-cli")
    assert kiro_cli.chat_sibling(str(tmp_path / "kiro-cli"), "linux") is None


@posix_only
def test_a_non_executable_chat_file_is_not_a_sibling(tmp_path):
    _executable(tmp_path / "kiro-cli")
    (tmp_path / "kiro-cli-chat").write_text("x", encoding="utf-8")
    assert kiro_cli.chat_sibling(str(tmp_path / "kiro-cli"), "linux") is None


def test_an_empty_executable_is_not_a_sibling(tmp_path):
    """Zero bytes cannot be exec'd (ENOEXEC); the launcher is the safer of two failures."""
    _executable(tmp_path / "kiro-cli")
    stub = tmp_path / "kiro-cli-chat"
    stub.write_text("", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    assert kiro_cli.chat_sibling(str(tmp_path / "kiro-cli"), "linux") is None


@pytest.mark.parametrize("name", ["kiro-cli-chat", "my-kiro", "kiro-cli.exe"])
def test_only_a_binary_named_kiro_cli_is_swapped(install, name):
    """The bundled entry is already the chat binary; any other name is kept as named."""
    _executable(install / name)
    assert kiro_cli.chat_sibling(str(install / name), "linux") is None


def test_windows_is_one_self_contained_executable(install):
    assert kiro_cli.chat_sibling(str(install / "kiro-cli"), "win32") is None


def test_empty_binary_yields_nothing():
    assert kiro_cli.chat_sibling("", "linux") is None


@requires_symlinks
def test_the_directory_is_the_resolved_paths_own_not_its_realpath(tmp_path):
    """``~/.local/bin/kiro-cli -> elsewhere`` is joined by ``~/.local/bin/kiro-cli-chat``.

    The launch-in-place rule keeps the path the caller resolved; a symlinked
    launcher whose target directory holds no chat binary still finds the one the
    install linked beside it.
    """
    target_dir = tmp_path / "vendor"
    target_dir.mkdir()
    _executable(target_dir / "kiro-cli")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "kiro-cli").symlink_to(target_dir / "kiro-cli")
    _executable(bin_dir / "kiro-cli-chat")
    assert kiro_cli.chat_sibling(str(bin_dir / "kiro-cli"), "linux") == str(
        bin_dir / "kiro-cli-chat"
    )


# ── KAS harness plan ──

LAUNCHER = str(Path("/opt/kiro/bin/kiro-cli"))
CHAT = str(Path("/opt/kiro/bin/kiro-cli-chat"))


def _ctx(tmp_path: Path, environ: dict[str, str] | None = None) -> SpawnContext:
    return SpawnContext(
        agent="a", work_dir=str(tmp_path), model=None, environ=environ or {}, home=tmp_path
    )


@pytest.fixture
def resolves_launcher(monkeypatch):
    async def _bin(*, environ, home):
        return LAUNCHER

    monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", _bin)


@pytest.fixture
def sibling_present(monkeypatch):
    """The install ships a chat binary beside the launcher, on any CI host."""
    calls: list[str] = []

    def _sibling(binary, platform_name=None):
        calls.append(binary)
        return CHAT if binary == LAUNCHER else None

    monkeypatch.setattr(kas_mod, "chat_sibling", _sibling)
    return calls


def _vault(answer: bool):
    async def _probe():
        return answer

    return _probe


@pytest.mark.asyncio
async def test_crew_owned_spawn_enters_through_the_chat_binary(
    resolves_launcher, sibling_present, monkeypatch, tmp_path
):
    monkeypatch.setattr(kas_mod, "vault_holds_identity_off_loop", _vault(True))
    plan = await harness_for(ACP_BACKEND_KAS).resolve_spawn(_ctx(tmp_path))
    assert plan.host_auth is True
    assert plan.argv[0] == CHAT
    assert KAS_RELAY_AUTH_FLAG not in plan.argv


@pytest.mark.asyncio
async def test_cli_owned_spawn_keeps_the_launcher_and_never_looks(
    resolves_launcher, sibling_present, monkeypatch, tmp_path
):
    """kiro-cli signed in is what this spawn needs anyway, so the gate costs it nothing."""
    monkeypatch.setattr(kas_mod, "vault_holds_identity_off_loop", _vault(False))
    plan = await harness_for(ACP_BACKEND_KAS).resolve_spawn(_ctx(tmp_path))
    assert plan.host_auth is False
    assert plan.argv[0] == LAUNCHER
    assert KAS_RELAY_AUTH_FLAG in plan.argv
    assert sibling_present == []


@pytest.mark.asyncio
async def test_crew_owned_spawn_without_a_sibling_keeps_the_resolved_binary(
    resolves_launcher, monkeypatch, tmp_path
):
    """No chat binary beside the launcher: the plan is what it was, never a guess."""
    monkeypatch.setattr(kas_mod, "chat_sibling", lambda binary, platform_name=None: None)
    monkeypatch.setattr(kas_mod, "vault_holds_identity_off_loop", _vault(True))
    plan = await harness_for(ACP_BACKEND_KAS).resolve_spawn(_ctx(tmp_path))
    assert plan.argv[0] == LAUNCHER
    assert plan.host_auth is True


@pytest.mark.asyncio
async def test_an_operator_override_named_kiro_cli_is_never_swapped(
    resolves_launcher, sibling_present, monkeypatch, tmp_path
):
    """``KIROCREW_KIRO_BIN`` is exactly what the operator asked for, wrapper included."""
    monkeypatch.setattr(kas_mod, "vault_holds_identity_off_loop", _vault(True))
    plan = await harness_for(ACP_BACKEND_KAS).resolve_spawn(
        _ctx(tmp_path, {"KIROCREW_KIRO_BIN": LAUNCHER})
    )
    assert plan.host_auth is True
    assert plan.argv[0] == LAUNCHER
    assert sibling_present == []


@pytest.mark.asyncio
async def test_an_override_naming_a_different_binary_does_not_pin_this_one(
    resolves_launcher, sibling_present, monkeypatch, tmp_path
):
    monkeypatch.setattr(kas_mod, "vault_holds_identity_off_loop", _vault(True))
    plan = await harness_for(ACP_BACKEND_KAS).resolve_spawn(
        _ctx(tmp_path, {"KIROCREW_KIRO_BIN": str(Path("/elsewhere/kiro-cli"))})
    )
    assert plan.argv[0] == CHAT
