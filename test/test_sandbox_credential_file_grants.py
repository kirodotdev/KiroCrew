"""``SandboxPolicy.credential_file_grants`` — the edition seam for granting a
credential FILE to an agent spawn.

Pins the contract the platform-context spec describes:

* the Default adapter grants nothing, and a companion that predates the method is
  treated as granting nothing;
* the resolver validates every entry and refuses the crew data home and the kiro
  trees whatever the edition asks for, failing closed (no grant) on any error;
* grants are Linux-namespace only and dropped on every other platform;
* the launcher stages granted files on a namespace-private tmpfs (``SECRET_FILES``),
  never through the host-visible ``EXPOSE_FILES`` copy;
* ``wrap_argv`` / ``wrap_argv_async`` thread the grant to the namespace builder.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import List

import pytest

import kiro_crew.sandbox as sb
from kiro_crew.platform.defaults import DefaultSandboxPolicy

# The mechanism is the Linux namespace launcher; the macOS cases below only need a
# POSIX path layout, which Windows does not have.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="credential file grants are a POSIX mechanism"
)


class _GrantingPolicy:
    def __init__(self, grants: object) -> None:
        self._grants = grants

    def strict_dirs(self) -> List[str]:
        return list(sb._STRICT_DIRS)

    def cc_dirs(self) -> List[str]:
        return list(sb._CC_DIRS)

    def credential_file_grants(self) -> object:
        return self._grants


class _LegacyPolicy:
    """A companion written before ``credential_file_grants`` existed."""

    def strict_dirs(self) -> List[str]:
        return list(sb._STRICT_DIRS)

    def cc_dirs(self) -> List[str]:
        return list(sb._CC_DIRS)


class _RaisingPolicy(_LegacyPolicy):
    def credential_file_grants(self) -> List[str]:
        raise RuntimeError("grant store unreadable")


@pytest.fixture
def on_linux(monkeypatch):
    monkeypatch.setattr(sb.sys, "platform", "linux")


def _use_policy(monkeypatch, policy) -> None:
    monkeypatch.setattr(sb, "_sandbox_policy", lambda: policy)


def _home(*parts: str) -> str:
    return os.path.join(str(Path.home()), *parts)


# --- Default and legacy adapters grant nothing ---


def test_default_policy_grants_nothing():
    assert DefaultSandboxPolicy().credential_file_grants() == []


def test_default_policy_yields_no_grants(monkeypatch, on_linux):
    _use_policy(monkeypatch, DefaultSandboxPolicy())
    assert sb.credential_file_grants() == ()


def test_a_companion_without_the_method_grants_nothing(monkeypatch, on_linux):
    _use_policy(monkeypatch, _LegacyPolicy())
    assert sb.credential_file_grants() == ()


def test_an_adapter_failure_grants_nothing(monkeypatch, on_linux):
    _use_policy(monkeypatch, _RaisingPolicy())
    assert sb.credential_file_grants() == ()


# --- Validation ---


def test_a_valid_grant_resolves_under_home(monkeypatch, on_linux):
    _use_policy(monkeypatch, _GrantingPolicy([".docker/config.json"]))
    assert sb.credential_file_grants() == (_home(".docker", "config.json"),)


def test_duplicate_grants_collapse(monkeypatch, on_linux):
    _use_policy(monkeypatch, _GrantingPolicy([".docker/config.json", ".docker/config.json"]))
    assert sb.credential_file_grants() == (_home(".docker", "config.json"),)


@pytest.mark.parametrize(
    "entry",
    [
        "/etc/shadow",
        "~/.docker/config.json",
        ".docker/../.ssh/id_rsa",
        "../outside",
        ".docker//config.json",
        "./.docker/config.json",
        "",
        ".docker/config.json\x00",
    ],
)
def test_malformed_entries_are_dropped(monkeypatch, on_linux, entry):
    _use_policy(monkeypatch, _GrantingPolicy([entry, ".kube/config"]))
    assert sb.credential_file_grants() == (_home(".kube", "config"),)


@pytest.mark.parametrize(
    "entry",
    [
        ".kiro/crew/.vault/.vault_key",
        ".kiro/crew/security_policy.json",
        ".kiro/agents/kirocrew.json",
        ".kirocrew/.env",
    ],
)
def test_the_crew_home_and_kiro_trees_are_never_granted(monkeypatch, on_linux, entry):
    _use_policy(monkeypatch, _GrantingPolicy([entry]))
    assert sb.credential_file_grants() == ()


def test_a_relocated_crew_home_is_never_granted(monkeypatch, on_linux):
    relocated = Path.home() / "relocated-crew-home"
    monkeypatch.setattr(sb, "config_dir", lambda: relocated)
    _use_policy(monkeypatch, _GrantingPolicy(["relocated-crew-home/sel_hmac.key"]))
    assert sb.credential_file_grants() == ()


@pytest.mark.parametrize("raw", [".docker/config.json", b".docker/config.json", 42, None])
def test_a_non_sequence_answer_grants_nothing(monkeypatch, on_linux, raw):
    _use_policy(monkeypatch, _GrantingPolicy(raw))
    assert sb.credential_file_grants() == ()


def test_non_string_entries_are_dropped(monkeypatch, on_linux):
    _use_policy(monkeypatch, _GrantingPolicy([42, None, ".kube/config"]))
    assert sb.credential_file_grants() == (_home(".kube", "config"),)


# --- Platform gate ---


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_grants_are_linux_only(monkeypatch, platform):
    monkeypatch.setattr(sb.sys, "platform", platform)
    _use_policy(monkeypatch, _GrantingPolicy([".docker/config.json"]))
    assert sb.credential_file_grants() == ()


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_wrap_argv_drops_grants_off_linux(monkeypatch, platform):
    monkeypatch.setattr(sb.sys, "platform", platform)
    assert sb._secret_files_for_backend((_home(".docker", "config.json"),), "standard") == ()


def test_wrap_argv_drops_grants_when_the_sandbox_is_off(on_linux):
    assert sb._secret_files_for_backend((_home(".docker", "config.json"),), "off") == ()


def test_wrap_argv_keeps_grants_on_linux(on_linux):
    path = _home(".docker", "config.json")
    assert sb._secret_files_for_backend((path,), "standard") == (path,)


# --- Launcher ---


def test_no_grant_leaves_the_launcher_without_secret_files():
    script = sb._build_launcher_script("standard")
    assert "SECRET_FILES = []" in script


def test_a_grant_is_embedded_as_a_secret_file_not_an_expose_copy():
    """The grant must ride the private-tmpfs path, never ``EXPOSE_FILES``.

    ``EXPOSE_FILES`` writes its copy into the hidden parent's stand-in, a directory
    on a host-visible tmpfs, which is acceptable for ``.aws/config`` and not for a
    registry login.
    """
    path = _home(".docker", "config.json")
    script = sb._build_launcher_script("standard", extra_secret_files=(path,))
    assert f"SECRET_FILES = {json.dumps([path])}" in script
    assert "EXPOSE_FILES = []" in script
    assert f"SECRET_FILE_MAX_BYTES = {sb._SECRET_FILE_MAX_BYTES}" in script


def test_a_path_in_both_lists_is_staged_only_as_a_secret():
    path = _home(".docker", "config.json")
    script = sb._build_launcher_script(
        "standard", extra_expose_files=(path,), extra_secret_files=(path,)
    )
    assert json.dumps([path, "config.json"]) not in script
    assert f"SECRET_FILES = {json.dumps([path])}" in script


@pytest.mark.parametrize("level", ["standard", "cc", "strict"])
def test_the_launcher_with_a_grant_is_valid_python(level):
    """The launcher is an f-string template; a stray brace breaks every spawn."""
    script = sb._build_launcher_script(level, extra_secret_files=(_home(".docker", "config.json"),))
    compile(script, "<launcher>", "exec")


def test_the_launcher_stages_secrets_on_a_private_tmpfs():
    script = sb._build_launcher_script(
        "standard", extra_secret_files=(_home(".docker", "config.json"),)
    )
    assert "creating the private tmpfs for granted credential files" in script
    assert "sealing granted credential file %s read-only" in script
    assert "os.O_NOFOLLOW" in script
    assert "_libc.umount2(_secret_root.encode(), _MNT_DETACH)" in script


# --- Threading through wrap_argv / wrap_argv_async ---


@pytest.mark.skipif(sys.platform != "linux", reason="grants ride the Linux namespace launcher")
def test_wrap_argv_threads_grants_to_the_namespace_builder(monkeypatch):
    captured: dict = {}
    script = "launcher.py"
    stub = ["launcher", *sb._LAUNCHER_INTERPRETER_FLAGS, script, "/bin/true"]

    def _fake_namespace_argv(argv, level, **kwargs):
        captured.update(kwargs)
        return list(stub)

    monkeypatch.setattr(sb, "detect_backend", lambda config_mode="auto": "namespace")
    monkeypatch.setattr(sb, "namespace_argv", _fake_namespace_argv)
    path = _home(".docker", "config.json")

    assert sb.wrap_argv(["/bin/true"], mode="standard", extra_secret_files=(path,)) == (
        stub,
        script,
    )
    assert captured.get("extra_secret_files") == (path,)


@pytest.mark.asyncio
async def test_wrap_argv_async_forwards_grants():
    captured: dict = {}

    def _prepare(argv, **options):
        captured.update(options)
        return list(argv), None

    path = _home(".docker", "config.json")
    await sb.wrap_argv_async(["/bin/true"], extra_secret_files=(path,), _prepare=_prepare)
    assert captured["extra_secret_files"] == (path,)


@pytest.mark.asyncio
async def test_wrap_argv_async_omits_an_empty_grant():
    captured: dict = {}

    def _prepare(argv, **options):
        captured.update(options)
        return list(argv), None

    await sb.wrap_argv_async(["/bin/true"], _prepare=_prepare)
    assert "extra_secret_files" not in captured
