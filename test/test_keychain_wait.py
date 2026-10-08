"""The gateway waits a bounded time for a locked macOS login Keychain, then fails clearly."""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REAL_RUN = subprocess.run
_LOCKED_STDERR = "security: SecKeychainCopySettings {path}: User interaction is not allowed.\n"


def _fake_security(locked: list[bool]):
    """A ``subprocess.run`` that answers ``security show-keychain-info`` only.

    ``locked`` is consumed one answer per probe; the last answer repeats.
    """
    calls: list[list[str]] = []

    def run(argv, *args, **kwargs):
        if list(argv[:2]) != ["security", "show-keychain-info"]:
            return _REAL_RUN(argv, *args, **kwargs)
        calls.append(list(argv))
        state = locked[min(len(calls) - 1, len(locked) - 1)]
        if state:
            return subprocess.CompletedProcess(argv, 36, "", _LOCKED_STDERR.format(path=argv[2]))
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run, calls


@pytest.fixture
def mac_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    keychains = home / "Library" / "Keychains"
    keychains.mkdir(parents=True)
    (keychains / "login.keychain-db").write_bytes(b"")
    monkeypatch.setenv("HOME", str(home))
    # Path.home() reads USERPROFILE on Windows.
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(sys, "platform", "darwin")
    return keychains / "login.keychain-db"


class TestGatewayEntry:
    """``kirocrew gateway`` with the login Keychain locked."""

    def _run_gateway(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["kirocrew", "gateway"])
        lock_cls = MagicMock()
        with (
            patch("kiro_crew.cli.GatewayLock", lock_cls),
            patch("kiro_crew.cli_server._gateway") as mock_gateway,
            patch("kiro_crew.cli.asyncio.run") as mock_run,
            patch("kiro_crew.cli.faulthandler.enable"),
            patch("kiro_crew.cli.maybe_reexec"),
        ):
            from kiro_crew.cli import main

            with pytest.raises(SystemExit) as excinfo:
                main()
        return excinfo.value.code, lock_cls, mock_gateway, mock_run

    def test_still_locked_exits_non_zero_without_lock_or_serve(self, mac_home, monkeypatch, capsys):
        monkeypatch.setenv("KIROCREW_KEYCHAIN_WAIT_SECS", "0")
        fake, calls = _fake_security([True])
        with patch("subprocess.run", fake):
            code, lock_cls, mock_gateway, mock_run = self._run_gateway(monkeypatch)
        assert code == 1
        assert calls, "the Keychain state must be probed"
        # Nothing is held while the Keychain is locked: no lock, no port, no serve.
        lock_cls.assert_not_called()
        mock_gateway.assert_not_called()
        mock_run.assert_not_called()
        err = capsys.readouterr().err
        assert "login Keychain" in err
        assert "locked" in err
        assert "security unlock-keychain" in err

    def test_unlocked_keychain_starts_as_before(self, mac_home, monkeypatch):
        fake, calls = _fake_security([False])
        monkeypatch.setattr(sys, "argv", ["kirocrew", "gateway"])
        with (
            patch("subprocess.run", fake),
            patch("kiro_crew.cli.GatewayLock") as lock_cls,
            patch("kiro_crew.cli_server._gateway"),
            patch("kiro_crew.cli.asyncio.run") as mock_run,
            patch("kiro_crew.cli.faulthandler.enable"),
            patch("kiro_crew.cli.maybe_reexec"),
        ):
            from kiro_crew.cli import main

            main()
        lock_cls.return_value.acquire.assert_called_once()
        mock_run.assert_called_once()

    def test_non_kiro_backend_never_probes_the_keychain(self, mac_home, monkeypatch):
        from types import SimpleNamespace

        fake, calls = _fake_security([True])
        agent = SimpleNamespace(acp_backend="claude", member_acp_backend="claude")
        monkeypatch.setattr(sys, "argv", ["kirocrew", "gateway"])
        with (
            patch("subprocess.run", fake),
            patch(
                "kiro_crew.cli.KiroCrewConfig.load",
                return_value=MagicMock(agent=agent),
            ),
            patch("kiro_crew.cli.boot_platform"),
            patch("kiro_crew.cli.GatewayLock") as lock_cls,
            patch("kiro_crew.cli_server._gateway"),
            patch("kiro_crew.cli._resolve_gateway_args", return_value={}),
            patch("kiro_crew.cli.asyncio.run") as mock_run,
            patch("kiro_crew.cli.faulthandler.enable"),
            patch("kiro_crew.cli.maybe_reexec"),
        ):
            from kiro_crew.cli import main

            main()
        assert calls == []
        lock_cls.return_value.acquire.assert_called_once()
        mock_run.assert_called_once()


class TestUsesKiroBackend:
    @pytest.mark.parametrize(
        ("main", "member", "expected"),
        [("", "", True), ("claude", "", True), ("", "claude", True), ("claude", "goose", False)],
    )
    def test_either_backend_being_kiro_counts(self, main, member, expected):
        from types import SimpleNamespace

        from kiro_crew.service.keychain_wait import uses_kiro_backend

        agent = SimpleNamespace(acp_backend=main, member_acp_backend=member)
        assert uses_kiro_backend(agent) is expected


class TestWaitForLoginKeychain:
    def _wait(self, keychain: Path, locked: list[bool], budget: str, platform="darwin"):
        answers = iter(locked)
        last = [locked[-1]]

        def is_locked(_path):
            try:
                last[0] = next(answers)
            except StopIteration:
                pass
            return last[0]

        clock = [0.0]
        sleeps: list[float] = []

        def sleep(secs):
            sleeps.append(secs)
            clock[0] += secs

        from kiro_crew.service.keychain_wait import wait_for_login_keychain

        result = wait_for_login_keychain(
            platform=platform,
            environ={"KIROCREW_KEYCHAIN_WAIT_SECS": budget},
            is_locked=is_locked,
            sleep=sleep,
            monotonic=lambda: clock[0],
            stderr=io.StringIO(),
            keychain=keychain,
        )
        return result, sleeps

    def test_not_macos_never_waits(self, mac_home):
        result, sleeps = self._wait(mac_home, [True], "300", platform="linux")
        assert result is None and sleeps == []

    def test_no_login_keychain_never_waits(self, tmp_path):
        result, sleeps = self._wait(tmp_path / "absent.keychain-db", [True], "300")
        assert result is None and sleeps == []

    def test_unlock_during_wait_lets_gateway_start(self, mac_home):
        result, sleeps = self._wait(mac_home, [True, True, False], "300")
        assert result is None
        assert len(sleeps) == 2

    def test_wait_is_bounded_then_names_the_cause(self, mac_home):
        result, sleeps = self._wait(mac_home, [True], "12")
        assert result is not None
        assert sum(sleeps) == pytest.approx(12)
        assert "login Keychain" in result and "security unlock-keychain" in result

    @pytest.mark.parametrize("raw", ["", "junk", "-5", "nan"])
    def test_bad_budget_uses_default(self, raw):
        from kiro_crew.service.keychain_wait import DEFAULT_WAIT_SECS, wait_budget_secs

        assert wait_budget_secs({"KIROCREW_KEYCHAIN_WAIT_SECS": raw}) == DEFAULT_WAIT_SECS


class TestKeychainIsLocked:
    def test_locked_marker_reads_as_locked(self, tmp_path):
        from kiro_crew.service.keychain_wait import keychain_is_locked

        fake, _ = _fake_security([True])
        with patch("kiro_crew.service.keychain_wait.subprocess.run", fake):
            assert keychain_is_locked(tmp_path / "login.keychain-db") is True

    def test_unlocked_reads_as_unlocked(self, tmp_path):
        from kiro_crew.service.keychain_wait import keychain_is_locked

        fake, _ = _fake_security([False])
        with patch("kiro_crew.service.keychain_wait.subprocess.run", fake):
            assert keychain_is_locked(tmp_path / "login.keychain-db") is False

    def test_missing_security_tool_is_not_a_lock(self, tmp_path):
        from kiro_crew.service.keychain_wait import keychain_is_locked

        with patch(
            "kiro_crew.service.keychain_wait.subprocess.run",
            side_effect=FileNotFoundError("security"),
        ):
            assert keychain_is_locked(tmp_path / "login.keychain-db") is False

    def test_other_failure_is_not_a_lock(self, tmp_path):
        from kiro_crew.service.keychain_wait import keychain_is_locked

        def run(argv, *a, **k):
            return subprocess.CompletedProcess(argv, 50, "", "security: no such keychain")

        with patch("kiro_crew.service.keychain_wait.subprocess.run", run):
            assert keychain_is_locked(tmp_path / "login.keychain-db") is False
