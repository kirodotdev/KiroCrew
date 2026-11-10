"""Connected computers: the OS key adapter's process runner, bus checks and platform pick.

Every ``secret-tool`` call is a stand-in: ``subprocess.run`` and the trusted-binary
lookup are replaced, so no real keyring is read or written.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from kiro_crew.connected_computers import protected_store as ps
from kiro_crew.connected_computers.protected_store import ProtectionUnavailable

KEY = bytes(range(32))


class _Runner:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls: list[tuple[list[str], bytes]] = []

    def __call__(self, argv, stdin: bytes = b""):
        self.calls.append((list(argv), stdin))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _store(runner) -> ps.LibsecretStore:
    return ps.LibsecretStore("acct", runner=runner, tool="/usr/bin/secret-tool")


class TestRun:
    def test_the_key_is_handed_on_stdin_and_the_output_is_capped(self, monkeypatch):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen.update(kwargs)
            big = b"o" * (ps._MAX_OUTPUT + 10)
            return SimpleNamespace(returncode=3, stdout=big, stderr=b"e" * (ps._MAX_OUTPUT + 1))

        monkeypatch.setattr(ps.subprocess, "run", fake_run)
        code, out, err = ps._run(["/usr/bin/secret-tool", "lookup"], b"secret-on-stdin")
        assert code == 3
        assert len(out) == ps._MAX_OUTPUT and len(err) == ps._MAX_OUTPUT
        assert seen["argv"] == ["/usr/bin/secret-tool", "lookup"]
        assert seen["input"] == b"secret-on-stdin"
        assert seen["capture_output"] is True and seen["check"] is False
        assert seen["timeout"] == ps.TOOL_TIMEOUT_SECS

    def test_a_tool_that_does_not_answer_in_time_is_unavailable(self, monkeypatch):
        def fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        monkeypatch.setattr(ps.subprocess, "run", fake_run)
        with pytest.raises(ProtectionUnavailable, match="did not answer in time"):
            ps._run(["/usr/bin/secret-tool", "lookup"])

    def test_a_tool_that_cannot_start_is_unavailable(self, monkeypatch):
        def fake_run(argv, **kwargs):
            raise FileNotFoundError(argv[0])

        monkeypatch.setattr(ps.subprocess, "run", fake_run)
        with pytest.raises(ProtectionUnavailable, match="could not be started"):
            ps._run(["/usr/bin/secret-tool", "lookup"])


class TestKeyShape:
    def test_a_stored_value_of_the_wrong_length_is_unreadable(self):
        with pytest.raises(ProtectionUnavailable, match="unreadable"):
            ps._parse_key(b"abcd\n", "Secret Service")

    def test_a_valid_stored_value_is_parsed(self):
        assert ps._parse_key(KEY.hex().encode() + b"\n", "Secret Service") == KEY

    @pytest.mark.parametrize("bad", [b"short", "x" * 32, bytes(31)])
    def test_only_a_32_byte_key_is_saved(self, bad):
        with pytest.raises(ValueError, match="32 bytes"):
            ps._check_key(bad)

    def test_a_short_key_is_refused_before_the_tool_runs(self):
        runner = _Runner([])
        with pytest.raises(ValueError):
            _store(runner).save(b"short")
        assert runner.calls == []


class TestSave:
    def test_a_refused_store_is_unavailable_and_not_read_back(self):
        runner = _Runner([(1, b"", b"denied")])
        with pytest.raises(ProtectionUnavailable, match="did not store the key"):
            _store(runner).save(KEY)
        assert [argv[1] for argv, _ in runner.calls] == ["store"]

    def test_a_read_back_that_fails_is_unavailable(self):
        runner = _Runner([(0, b"", b""), (2, b"", b"locked")])
        with pytest.raises(ProtectionUnavailable, match="did not keep the key") as info:
            _store(runner).save(KEY)
        assert isinstance(info.value.__cause__, ProtectionUnavailable)
        assert [argv[1] for argv, _ in runner.calls] == ["store", "lookup"]

    def test_a_save_that_reads_back_the_same_key_succeeds(self):
        runner = _Runner([(0, b"", b""), (0, KEY.hex().encode(), b"")])
        assert _store(runner).save(KEY) is None
        store_argv, stdin = runner.calls[0]
        assert f"--label={ps.LABEL}" in store_argv
        assert stdin == KEY.hex().encode("ascii")


class TestSessionBus:
    def test_no_session_bus_is_unavailable(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ps, "trusted_system_bin_quiet", lambda _name: "/usr/bin/secret-tool")
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        with pytest.raises(ProtectionUnavailable, match="no session bus"):
            ps.LibsecretStore.for_account("acct")

    def test_no_runtime_dir_is_no_bus(self, monkeypatch):
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        assert ps._runtime_bus_exists() is False

    def test_a_runtime_bus_socket_is_enough(self, monkeypatch, tmp_path):
        (tmp_path / "bus").write_text("", encoding="utf-8")
        monkeypatch.setattr(ps, "trusted_system_bin_quiet", lambda _name: "/usr/bin/secret-tool")
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        assert ps._runtime_bus_exists() is True
        store = ps.LibsecretStore.for_account("acct-1")
        assert store._tool == "/usr/bin/secret-tool"
        assert store._attrs() == ["service", ps.SERVICE, "account", "acct-1"]

    def test_a_bus_address_in_the_environment_is_enough(self, monkeypatch):
        monkeypatch.setattr(ps, "trusted_system_bin_quiet", lambda _name: "/usr/bin/secret-tool")
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent/bus")
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        assert ps.LibsecretStore.for_account("acct")._tool == "/usr/bin/secret-tool"


class TestPlatformChoice:
    def test_linux_uses_the_secret_service_for_this_data_home(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ps.sys, "platform", "linux")
        monkeypatch.setattr(ps, "trusted_system_bin_quiet", lambda _name: "/usr/bin/secret-tool")
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent/bus")
        store = ps.protected_store_for(tmp_path)
        assert isinstance(store, ps.LibsecretStore)
        assert store._account == ps.account_for(tmp_path)

    def test_this_machine_uses_the_configured_data_home(self, monkeypatch, tmp_path):
        from kiro_crew.config import paths

        seen = []

        def chosen(config_dir):
            seen.append(config_dir)
            raise ProtectionUnavailable("stand-in refusal")

        monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(ps, "protected_store_for", chosen)
        with pytest.raises(ProtectionUnavailable, match="stand-in refusal"):
            ps.protected_store_for_this_machine()
        assert seen == [tmp_path]

    def test_this_machine_refuses_off_linux(self, monkeypatch, tmp_path):
        from kiro_crew.config import paths

        monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(ps.sys, "platform", "darwin")
        with pytest.raises(ProtectionUnavailable, match="no OS protection"):
            ps.protected_store_for_this_machine()
