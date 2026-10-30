"""``kirocrew doctor`` probes the ``kiro-cli-chat`` delegate, not just the launcher.

The ``kiro-cli`` launcher answers ``--version`` itself, so a stale install whose
``~/.local/bin/kiro-cli-chat`` points at a removed release passed doctor while
every session died ``process exited (rc=None)``. These tests run real files on
disk -- a healthy script, a dangling link, a script that exits non-zero -- so the
probe is exercised through an actual ``exec``, not a mocked ``subprocess``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from kiro_crew import env
from kiro_crew.doctor_checks import services

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher/delegate pair")


def _script(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``bin`` dir holding a ``kiro-cli`` launcher, with HOME and PATH isolated.

    The session search path is pinned to ``PATH`` plus one tmp ``augmented``
    dir, standing in for the well-known dirs (``/usr/local/bin`` and the like)
    that ``augmented_path`` adds, so no host install leaks into a probe.
    """
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _script(bin_dir / "kiro-cli", "echo 'kiro-cli 9.9.9'")
    augmented = tmp_path / "augmented"
    augmented.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    monkeypatch.setattr(
        env, "augmented_path", lambda base, **_: os.pathsep.join([base, str(augmented)])
    )
    return bin_dir


def _run(
    kiro: str | None, capsys: pytest.CaptureFixture[str], *, required: bool = True
) -> tuple[str, list[str]]:
    issues: list[str] = []
    services._doctor_kiro_chat_delegate(kiro, issues, required=required)
    return capsys.readouterr().out, issues


def _local_bin() -> Path:
    return Path(os.environ["HOME"]) / ".local" / "bin"


def test_healthy_delegate_passes(install: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _script(_local_bin() / "kiro-cli-chat", "echo 'kiro-cli-chat 2.33.0'")
    out, issues = _run(str(install / "kiro-cli"), capsys)
    assert issues == []
    assert "✅ kiro-cli-chat 2.33.0" in out
    assert "broken" not in out


def test_dangling_delegate_link_reports_the_reinstall_remedy(
    install: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The field case: the link survives an upgrade that removed its target.
    removed = install.parent / "toolbox" / "2.31.1" / "kiro-cli-chat"
    os.symlink(removed, Path(os.environ["HOME"]) / ".local" / "bin" / "kiro-cli-chat")
    out, issues = _run(str(install / "kiro-cli"), capsys)
    assert "❌ Kiro CLI is broken" in out
    assert services.CHAT_DELEGATE_BROKEN_FIX in out
    assert len(issues) == 1 and "reinstall Kiro CLI" in issues[0]


def test_dangling_link_is_not_skipped_for_a_healthy_copy_on_path(
    install: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The launcher stops at ~/.local/bin, so a working copy later on PATH does not
    # save the session -- the probe must not fall through to it either.
    os.symlink(tmp_path / "gone", Path(os.environ["HOME"]) / ".local" / "bin" / "kiro-cli-chat")
    on_path = tmp_path / "on-path"
    on_path.mkdir()
    _script(on_path / "kiro-cli-chat", "echo ok")
    monkeypatch.setenv("PATH", str(on_path))
    out, issues = _run(str(install / "kiro-cli"), capsys)
    assert "❌ Kiro CLI is broken" in out
    assert issues


def test_delegate_that_exits_nonzero_is_broken(
    install: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _script(_local_bin() / "kiro-cli-chat", "echo 'error: failed to launch' >&2; exit 1")
    out, issues = _run(str(install / "kiro-cli"), capsys)
    assert "--version exited 1" in out
    assert services.CHAT_DELEGATE_BROKEN_FIX in out
    assert issues


def test_delegate_that_hangs_is_broken_not_a_doctor_crash(
    install: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(services, "_CHAT_DELEGATE_PROBE_TIMEOUT_SECS", 0.5)
    _script(_local_bin() / "kiro-cli-chat", "exec /bin/sleep 30")
    out, issues = _run(str(install / "kiro-cli"), capsys)
    assert "did not answer" in out
    assert issues


def test_a_sibling_the_launcher_never_reads_does_not_mask_a_dangling_link(
    install: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The launcher does not look in its own directory, so a working chat binary
    # there leaves a session started through it just as broken.
    _script(install / "kiro-cli-chat", "echo ok")
    os.symlink(tmp_path / "gone", _local_bin() / "kiro-cli-chat")
    out, issues = _run(str(install / "kiro-cli"), capsys)
    assert "❌ Kiro CLI is broken" in out
    assert issues


def test_launcher_with_no_delegate_anywhere_is_broken(
    install: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out, issues = _run(str(install / "kiro-cli"), capsys)
    assert "no kiro-cli-chat" in out
    assert issues


def test_delegate_found_on_path(
    install: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    on_path = tmp_path / "on-path"
    on_path.mkdir()
    _script(on_path / "kiro-cli-chat", "echo 'kiro-cli-chat 2.33.0'")
    monkeypatch.setenv("PATH", str(on_path))
    out, issues = _run(str(install / "kiro-cli"), capsys)
    assert issues == []
    assert "✅" in out


def test_delegate_found_on_the_session_search_path(
    install: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Sessions launch with the augmented search path, so a healthy delegate in
    # one of its well-known dirs (``/usr/local/bin`` and the like) works even
    # when doctor's own shell leaves that dir off PATH.
    _script(tmp_path / "augmented" / "kiro-cli-chat", "echo 'kiro-cli-chat 2.33.0'")
    out, issues = _run(str(install / "kiro-cli"), capsys)
    assert issues == []
    assert "✅ kiro-cli-chat 2.33.0" in out


def test_bundled_chat_entry_is_probed_as_itself(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    chat = _script(tmp_path / "kiro-cli-chat", "echo 'kiro-cli-chat 2.33.0'")
    out, issues = _run(str(chat), capsys)
    assert issues == []
    assert "✅ kiro-cli-chat 2.33.0" in out


@pytest.mark.parametrize("kiro", [None, "", "/opt/wrappers/my-kiro"])
def test_silent_when_there_is_no_launcher_or_the_delegate_is_unknowable(
    kiro: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    out, issues = _run(kiro, capsys)
    assert out == ""
    assert issues == []


def test_unused_backend_reports_a_broken_delegate_without_failing_doctor(
    install: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Claude, a KAS bundle or any other harness never runs kiro-cli, so a stale
    # install left on disk is worth a line but not a red doctor.
    os.symlink(tmp_path / "gone", _local_bin() / "kiro-cli-chat")
    out, issues = _run(str(install / "kiro-cli"), capsys, required=False)
    assert "⚠️  Kiro CLI is broken" in out
    assert "not used by the selected backend" in out
    assert services.CHAT_DELEGATE_BROKEN_FIX in out
    assert issues == []


def test_macos_app_launcher_is_probed_through_its_own_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    macos = tmp_path / "Kiro CLI.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    _script(macos / "kiro-cli", "echo launcher")
    _script(macos / "kiro-cli-chat", "echo 'kiro-cli-chat 2.33.0'")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    monkeypatch.setattr(services.sys, "platform", "darwin")
    out, issues = _run(str(macos / "kiro-cli"), capsys)
    assert issues == []
    assert "✅ kiro-cli-chat 2.33.0" in out


def test_vault_signed_relay_with_a_working_sibling_is_not_fatal(
    install: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Relay KAS signed in through Crew's vault execs the chat binary beside the
    # launcher, so a dangling ~/.local/bin link does not stop its sessions.
    sibling = _script(install / "kiro-cli-chat", "echo 'kiro-cli-chat 2.33.0'")
    os.symlink(tmp_path / "gone", _local_bin() / "kiro-cli-chat")
    issues: list[str] = []
    services._doctor_kiro_chat_delegate(
        str(install / "kiro-cli"), issues, required=True, relay_entry=str(sibling)
    )
    out = capsys.readouterr().out
    assert issues == []
    assert "⚠️  Kiro CLI is broken" in out
    assert f"launch {sibling}, which works" in out
    assert services.CHAT_DELEGATE_BROKEN_FIX in out


def test_vault_signed_relay_with_a_broken_sibling_stays_fatal(
    install: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sibling = _script(install / "kiro-cli-chat", "exit 3")
    os.symlink(tmp_path / "gone", _local_bin() / "kiro-cli-chat")
    issues: list[str] = []
    services._doctor_kiro_chat_delegate(
        str(install / "kiro-cli"), issues, required=True, relay_entry=str(sibling)
    )
    out = capsys.readouterr().out
    assert "❌ Kiro CLI is broken" in out
    assert len(issues) == 1


def test_healthy_delegate_does_not_hide_a_broken_relay_entry(
    install: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Relay KAS sessions exec the sibling, not the healthy ~/.local/bin
    # delegate, so the sibling must be probed even when the delegate passes.
    _script(_local_bin() / "kiro-cli-chat", "echo 'kiro-cli-chat 2.33.0'")
    sibling = _script(install / "kiro-cli-chat", "exit 3")
    issues: list[str] = []
    services._doctor_kiro_chat_delegate(
        str(install / "kiro-cli"), issues, required=True, relay_entry=str(sibling)
    )
    out = capsys.readouterr().out
    assert "❌ Kiro CLI is broken" in out
    assert f"{sibling} --version exited 3" in out
    assert "✅" not in out
    assert len(issues) == 1


@pytest.mark.parametrize("broken", ["delegate", "relay"])
def test_mixed_kiro_and_relay_selection_needs_both_binaries(
    install: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], broken: str
) -> None:
    # Kiro CLI sessions run the launcher's delegate and vault-signed relay KAS
    # sessions run the sibling: either one failing breaks a selected role.
    if broken == "delegate":
        os.symlink(tmp_path / "gone", _local_bin() / "kiro-cli-chat")
        sibling = _script(install / "kiro-cli-chat", "echo ok")
    else:
        _script(_local_bin() / "kiro-cli-chat", "echo ok")
        sibling = _script(install / "kiro-cli-chat", "exit 3")
    issues: list[str] = []
    services._doctor_kiro_chat_delegate(
        str(install / "kiro-cli"),
        issues,
        required=True,
        relay_entry=str(sibling),
        kiro_role=True,
    )
    out = capsys.readouterr().out
    assert "❌ Kiro CLI is broken" in out
    assert len(issues) == 1


def test_mixed_selection_passes_when_both_binaries_launch(
    install: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _script(_local_bin() / "kiro-cli-chat", "echo 'kiro-cli-chat 2.33.0'")
    sibling = _script(install / "kiro-cli-chat", "echo 'kiro-cli-chat 2.33.0'")
    issues: list[str] = []
    services._doctor_kiro_chat_delegate(
        str(install / "kiro-cli"),
        issues,
        required=True,
        relay_entry=str(sibling),
        kiro_role=True,
    )
    out = capsys.readouterr().out
    assert issues == []
    assert "✅ kiro-cli-chat 2.33.0" in out


def _vault(monkeypatch: pytest.MonkeyPatch, holds: bool) -> None:
    from kiro_crew.auth import bridge

    monkeypatch.setattr(bridge, "vault_holds_identity", lambda *, warn=True: holds)


def test_relay_entry_is_the_launcher_sibling_when_the_vault_signs_in(
    install: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sibling = _script(install / "kiro-cli-chat", "echo ok")
    monkeypatch.delenv("KIROCREW_KIRO_BIN", raising=False)
    _vault(monkeypatch, True)
    assert services.kas_relay_entry(str(install / "kiro-cli")) == str(sibling)


def test_relay_entry_keeps_the_launcher_without_a_vault_identity(
    install: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(install / "kiro-cli-chat", "echo ok")
    monkeypatch.delenv("KIROCREW_KIRO_BIN", raising=False)
    _vault(monkeypatch, False)
    assert services.kas_relay_entry(str(install / "kiro-cli")) is None


def test_relay_entry_never_swaps_the_operator_override(
    install: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(install / "kiro-cli-chat", "echo ok")
    monkeypatch.setenv("KIROCREW_KIRO_BIN", str(install / "kiro-cli"))
    _vault(monkeypatch, True)
    assert services.kas_relay_entry(str(install / "kiro-cli")) is None


def test_relay_entry_asks_the_harness_which_binary_is_the_override(
    install: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Doctor uses ``KasHarness``'s own override rule, not a copy that can drift.

    With no ``KIROCREW_KIRO_BIN`` set, a private copy of the rule would swap to
    the sibling; only a call into the harness's rule sees this verdict.
    """
    from kiro_crew.acp.harness import kas

    _script(install / "kiro-cli-chat", "echo ok")
    monkeypatch.delenv("KIROCREW_KIRO_BIN", raising=False)
    _vault(monkeypatch, True)
    seen: list[str] = []

    def _override(binary: str, environ: object) -> bool:
        seen.append(binary)
        return True

    monkeypatch.setattr(kas, "_is_operator_override", _override)
    assert services.kas_relay_entry(str(install / "kiro-cli")) is None
    assert seen == [str(install / "kiro-cli")]
