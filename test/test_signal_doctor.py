"""Tests for the ``Signal Integration`` section of ``kirocrew doctor``.

The section answers, without starting the gateway, every prerequisite the
channel fails on silently: the signal-cli binary (from the channel's own
resolver), its version, Java for the JVM build, the account and its local
store, and the allow-list. The version probe's runner is injected, so no real
signal-cli is needed; no phone number may reach the output unredacted.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.doctor_checks import channels
from kiro_crew.signal_cli.bridge_path import ENV_OVERRIDE

ACCOUNT = "+447700900001"
OWNER = "+447700900002"
OWNER_ACI = "bbbbbbbb-2222-4222-8222-222222222222"

_ELF = b"\x7fELF\x02\x01\x01"
_SCRIPT = b'#!/bin/sh\nexec java -jar signal-cli.jar "$@"\n'


def _cfg(**signal: Any) -> Any:
    section = {"enabled": True, "account": ACCOUNT, "allowed_numbers": [OWNER]}
    section.update(signal)
    return SimpleNamespace(signal=SimpleNamespace(**section))


class _Runner:
    """A stand-in for ``subprocess.run`` that records the argv it was given."""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append((argv, kwargs))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _ok(stdout: str = "signal-cli 0.14.9\n") -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A host with a native signal-cli, no Java, and an empty data home."""
    binary = tmp_path / "bin" / "signal-cli"
    binary.parent.mkdir()
    binary.write_bytes(_ELF)
    binary.chmod(0o755)
    data_home = tmp_path / "share"
    monkeypatch.setenv(ENV_OVERRIDE, str(binary))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    monkeypatch.delenv("JAVA_HOME", raising=False)
    runner = _Runner(_ok())
    monkeypatch.setattr(channels, "_run", runner)
    return SimpleNamespace(binary=binary, data_home=data_home, runner=runner, tmp=tmp_path)


def _store(data_home: Path, *accounts: dict[str, str]) -> Path:
    path = data_home / "signal-cli" / "data" / "accounts.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"accounts": list(accounts), "version": 2}), encoding="utf-8")
    return path


def _run_section(cfg: Any, capsys: pytest.CaptureFixture[str]) -> tuple[str, list[str]]:
    issues: list[str] = []
    channels._doctor_signal(cfg, issues)
    return capsys.readouterr().out, issues


def test_a_disabled_channel_is_visible_and_names_what_to_set(env, capsys) -> None:
    out, issues = _run_section(_cfg(enabled=False), capsys)

    assert "Signal Integration" in out
    assert "not enabled" in out
    assert "signal.allowed_numbers" in out
    assert issues == []
    # A disabled channel never spawns anything.
    assert env.runner.calls == []


def test_a_config_without_a_signal_section_reads_as_disabled(env, capsys) -> None:
    out, issues = _run_section(SimpleNamespace(), capsys)

    assert "not enabled" in out
    assert issues == []


def test_a_healthy_native_install_reports_clean(env, capsys) -> None:
    _store(env.data_home, {"path": "123", "number": ACCOUNT, "uuid": OWNER_ACI})

    out, issues = _run_section(_cfg(), capsys)

    assert f"signal-cli:  ✅ {env.binary}" in out
    assert "version:     ✅ signal-cli 0.14.9" in out
    assert "store:       ✅" in out
    assert "allowed:     ✅ 1 usable entry" in out
    # The native build needs no Java, so no java row at all.
    assert "java:" not in out
    assert issues == []


def test_the_version_probe_is_bounded_and_reads_no_stdin(env, capsys) -> None:
    _run_section(_cfg(), capsys)

    [(argv, kwargs)] = env.runner.calls
    assert argv == [str(env.binary), "--version"]
    assert kwargs["timeout"] == channels.SIGNAL_VERSION_TIMEOUT_S
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["encoding"] == "utf-8"


def test_a_missing_binary_is_an_issue_naming_the_override(env, monkeypatch, capsys) -> None:
    monkeypatch.setenv(ENV_OVERRIDE, str(env.tmp / "nowhere" / "signal-cli"))

    out, issues = _run_section(_cfg(), capsys)

    assert "signal-cli:  ❌ not found" in out
    assert ENV_OVERRIDE in out
    assert "signal: signal-cli not found" in issues
    assert env.runner.calls == []


def test_a_version_probe_that_times_out_is_an_issue_not_a_hang(env, capsys) -> None:
    env.runner.result = subprocess.TimeoutExpired(["signal-cli"], channels.SIGNAL_VERSION_TIMEOUT_S)

    out, issues = _run_section(_cfg(), capsys)

    assert "version:     ❌ no answer within" in out
    assert "signal: signal-cli does not run" in issues


def test_a_failing_version_probe_reports_the_last_stderr_line(env, capsys) -> None:
    env.runner.result = SimpleNamespace(
        returncode=1,
        stdout="",
        stderr="Exception in thread main\njava.lang.UnsupportedClassVersionError: x\n",
    )

    out, issues = _run_section(_cfg(), capsys)

    assert "exited 1: java.lang.UnsupportedClassVersionError: x" in out
    assert "signal: signal-cli does not run" in issues


def test_an_unrunnable_binary_is_an_issue(env, capsys) -> None:
    env.runner.result = PermissionError(13, "Permission denied")

    out, issues = _run_section(_cfg(), capsys)

    assert "could not run it (Permission denied)" in out
    assert "signal: signal-cli does not run" in issues


def test_the_jvm_build_without_java_is_an_issue(env, capsys) -> None:
    env.binary.write_bytes(_SCRIPT)

    out, issues = _run_section(_cfg(), capsys)

    assert "java:        ❌ not found" in out
    assert "signal: java missing" in issues


def test_the_jvm_build_finds_java_under_java_home(env, monkeypatch, capsys) -> None:
    env.binary.write_bytes(_SCRIPT)
    java = env.tmp / "jdk" / "bin" / "java"
    java.parent.mkdir(parents=True)
    java.write_bytes(_ELF)
    java.chmod(0o755)
    monkeypatch.setenv("JAVA_HOME", str(env.tmp / "jdk"))

    out, issues = _run_section(_cfg(), capsys)

    assert f"java:        ✅ {java} (JVM build)" in out
    assert "signal: java missing" not in issues


def test_the_jvm_build_finds_java_on_path(env, monkeypatch, capsys) -> None:
    env.binary.write_bytes(_SCRIPT)
    path_dir = env.tmp / "path-bin"
    path_dir.mkdir()
    # Windows' PATH lookup only finds a name carrying a PATHEXT extension.
    java = path_dir / ("java.exe" if sys.platform == "win32" else "java")
    java.write_bytes(_ELF)
    java.chmod(0o755)
    monkeypatch.setenv("PATH", str(path_dir))

    out, issues = _run_section(_cfg(), capsys)

    assert "java:        ✅" in out
    assert "signal: java missing" not in issues


def test_a_missing_account_is_an_issue(env, capsys) -> None:
    out, issues = _run_section(_cfg(account=""), capsys)

    assert "account:     ❌ signal.account is not set" in out
    assert "signal: account not set" in issues


def test_a_malformed_account_is_an_issue_and_is_redacted(env, capsys) -> None:
    out, issues = _run_section(_cfg(account="07700900123"), capsys)

    assert "account:     ❌ 0770***" in out
    assert "07700900123" not in out
    assert "signal: account malformed" in issues


def test_a_json_integer_account_is_read_as_e164(env, capsys) -> None:
    out, issues = _run_section(_cfg(account=447700900001), capsys)

    assert "account:     ✅ +447***" in out
    assert "signal: account malformed" not in issues


def test_an_account_missing_from_the_local_store_warns_without_an_issue(env, capsys) -> None:
    store = _store(env.data_home, {"path": "9", "number": "+447700900099"})

    out, issues = _run_section(_cfg(), capsys)

    assert f"store:       ⚠️  account not listed in {store}" in out
    assert issues == []


def test_no_local_store_warns_without_an_issue(env, capsys) -> None:
    out, issues = _run_section(_cfg(), capsys)

    assert "store:       ⚠️  no signal-cli account store at" in out
    assert issues == []


def test_an_unreadable_store_reads_as_no_store(env, capsys) -> None:
    path = env.data_home / "signal-cli" / "data" / "accounts.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    out, _issues = _run_section(_cfg(), capsys)

    assert "no signal-cli account store" in out


def test_the_store_matches_the_account_by_aci_too(env, capsys) -> None:
    _store(env.data_home, {"path": "1", "uuid": OWNER_ACI.upper()})

    out, _issues = _run_section(_cfg(account=OWNER_ACI), capsys)

    assert "store:       ✅" in out


def test_the_default_data_dir_falls_back_to_the_home_share(env, monkeypatch) -> None:
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: env.tmp / "home"))

    assert channels._signal_data_dir() == env.tmp / "home" / ".local" / "share" / "signal-cli"


def test_an_empty_allow_list_is_an_issue(env, capsys) -> None:
    out, issues = _run_section(_cfg(allowed_numbers=[]), capsys)

    assert "allowed:     ❌" in out
    assert "signal: allowed_numbers empty" in issues


def test_malformed_allow_list_entries_are_counted_and_redacted(env, capsys) -> None:
    out, issues = _run_section(_cfg(allowed_numbers=[OWNER, "07700900987", OWNER_ACI, ""]), capsys)

    assert "allowed:     ✅ 2 usable entries" in out
    assert "1 never match" in out
    assert "0770***" in out
    assert "07700900987" not in out
    assert "signal: malformed allowed_numbers" in issues


def test_an_allow_list_of_only_malformed_entries_is_both_issues(env, capsys) -> None:
    _out, issues = _run_section(_cfg(allowed_numbers=["12345"]), capsys)

    assert "signal: allowed_numbers empty" in issues
    assert "signal: malformed allowed_numbers" in issues


def test_no_full_phone_number_reaches_the_output(env, capsys) -> None:
    _store(env.data_home, {"path": "1", "number": ACCOUNT})

    out, _issues = _run_section(_cfg(allowed_numbers=[OWNER, "+44 7700 900003"]), capsys)

    for number in (ACCOUNT, OWNER, "447700900003", "1555 0000003"):
        assert number not in out


@pytest.mark.skipif(sys.platform == "win32", reason="runs a POSIX shebang script")
def test_the_real_runner_reads_a_version_from_a_child(tmp_path: Path) -> None:
    """The unpatched runner against a real child: the kwargs it passes are valid."""
    script = tmp_path / "signal-cli"
    script.write_text(f"#!{sys.executable}\nprint('signal-cli 9.9.9-test')\n", encoding="utf-8")
    script.chmod(0o755)

    version, problem = channels._signal_cli_version(str(script))

    assert (version, problem) == ("signal-cli 9.9.9-test", "")


def test_signal_is_not_listed_again_under_other_channels(env, monkeypatch, capsys) -> None:
    """Its own section reports the account, so a second row would double the issue."""
    import kiro_crew.channels as roster

    rows = [
        roster.ChannelReadiness("signal", True, (), ("account",)),
        roster.ChannelReadiness("telegram", False, (), ()),
    ]
    issues: list[str] = []
    monkeypatch.setattr(roster, "channel_readiness", lambda cfg, creds: rows)

    channels._doctor_other_channels(SimpleNamespace(), {}, issues)

    out = capsys.readouterr().out
    assert "signal:" not in out
    assert issues == []
