"""Tests for the ``apple`` STT provider (kiro_crew.apple_speech).

The provider is macOS-only and its real work happens in a compiled Swift helper, so
the tests split three ways: platform-gating logic (runs everywhere, with the platform
faked), the Python-side plumbing (subprocess contract, JSON parsing, transcode
fallback), and one genuine end-to-end run marked macOS-only.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import platform
import sys
import threading
from contextlib import ExitStack
from unittest.mock import AsyncMock, Mock, patch

import pytest

from kiro_crew import apple_speech, platform_compat
from kiro_crew.config.loader import SttConfig, _validated_stt_provider

_IS_MACOS = platform.system() == "Darwin"


class TestProviderRegistration:
    def test_apple_is_a_valid_provider(self):
        assert _validated_stt_provider("apple") == "apple"

    def test_unknown_provider_still_falls_back(self):
        assert _validated_stt_provider("nope") == "whisper"

    def test_default_provider_unchanged(self):
        """Adding a provider must not move the default off whisper — `apple` is
        macOS-only, and the default has to work on all three platforms."""
        assert SttConfig().provider == "whisper"


class TestAvailability:
    def test_refuses_off_darwin(self):
        with patch("platform.system", return_value="Linux"):
            avail = apple_speech.availability()
        assert not avail.ok
        assert "macOS" in avail.reason
        assert not avail.needs_toolchain

    def test_refuses_old_macos(self):
        with (
            patch("platform.system", return_value="Darwin"),
            patch("platform.mac_ver", return_value=("15.4", ("", "", ""), "arm64")),
        ):
            avail = apple_speech.availability()
        assert not avail.ok
        assert "26" in avail.reason

    def test_missing_toolchain_is_distinguishable(self):
        """A missing Swift toolchain is user-fixable, so it must not look like an
        unsupported OS — the UI shows a different call to action."""
        with (
            patch("platform.system", return_value="Darwin"),
            patch("platform.mac_ver", return_value=("27.0", ("", "", ""), "arm64")),
            patch.object(apple_speech, "_swiftc_fast", return_value=None),
        ):
            avail = apple_speech.availability()
        assert not avail.ok
        assert avail.needs_toolchain
        assert "xcode-select" in avail.reason

    def test_available_when_all_preconditions_hold(self):
        with (
            patch("platform.system", return_value="Darwin"),
            patch("platform.mac_ver", return_value=("27.0", ("", "", ""), "arm64")),
            patch.object(apple_speech, "_swiftc_fast", return_value="/usr/bin/swiftc"),
        ):
            assert apple_speech.availability().ok


def _fake_native_audio(path):
    """Stub `_to_native_audio` so the sandbox path is reached without ffmpeg."""

    async def _inner(*_a, **_k):
        return path, False

    return _inner


class TestHelperBuild:
    @pytest.mark.asyncio
    async def test_no_sandbox_backend_fails_with_a_remedy_not_an_exception(self, monkeypatch):
        """A host without an OS sandbox backend must fail cleanly, never unsandboxed.

        `sandboxed_spawn_argv` is fail-closed: on a container with no user
        namespaces it raises rather than degrading. That is the behaviour we want —
        running the helper unsandboxed as a fallback would silently undo the
        isolation — but the exception must not escape to the caller, which is what
        broke Backend Tests shard 1 on all three platforms. Each of the three
        entry points surfaces the message (which carries the remedy) in its own
        result shape.
        """
        from kiro_crew import sandbox as sb

        def boom(*a, **kw):
            raise sb.SandboxUnavailableError(
                "no backend: unshare(CLONE_NEWNS) EPERM", "no_backend", "EPERM"
            )

        monkeypatch.setattr(sb, "sandboxed_spawn_argv", boom)
        monkeypatch.setattr(apple_speech, "helper_path", lambda *a, **k: "/tmp/fake-helper")
        monkeypatch.setattr(
            apple_speech, "stream_helper_path", lambda *a, **k: "/tmp/fake-stream-helper"
        )
        monkeypatch.setattr(apple_speech, "availability", lambda: apple_speech.Availability(True))
        monkeypatch.setattr(apple_speech, "_to_native_audio", _fake_native_audio("/tmp/fake.wav"))

        text, meta = await apple_speech.transcribe("/tmp/fake.wav", locale="en-US")
        assert text is None
        assert "no backend" in meta["error"]

        inv = await apple_speech.inventory()
        assert "no backend" in inv["error"]

        session = apple_speech.StreamingSession(locale="en-US", sample_rate=16000)
        reason = await session.start()
        assert "no backend" in reason

    def test_sandbox_wrapping_is_offloaded_off_the_event_loop(self):
        """`_sandboxed` spawns, so no async caller may invoke it inline.

        The backend probe runs `sandbox-exec` on macOS (measured 17.7ms cold, 0.3ms
        cached) and all three call sites are `async`. Keeping the probe out of
        `availability()` was necessary but NOT sufficient. Every async caller must
        route through `_sandboxed_off_loop`, which owns both halves of the
        invariant: the worker-thread hop (loop safety) and the shield around it
        (a cancelled awaiter can still recover and drop the launcher the thread
        made). Pinned at the source level so a new async call site that reaches
        `_sandboxed` any other way fails here instead of stalling the gateway's
        loop — or leaking one launcher per cancelled call — in production.
        """
        hop = inspect.getsource(apple_speech._sandboxed_off_loop)
        assert "to_thread" in hop, "the hop must stay off the event loop"
        assert "shield" in hop, "the hop must stay recoverable under cancellation"
        for fn in (
            apple_speech.transcribe,
            apple_speech.inventory,
            apple_speech.StreamingSession.start,
        ):
            body = inspect.getsource(fn)
            if "_sandboxed" not in body:
                continue
            assert "_sandboxed_off_loop(" in body, fn.__name__
            for line in body.splitlines():
                stripped = line.strip()
                if "_sandboxed(" in stripped:
                    raise AssertionError(f"{fn.__name__} calls _sandboxed inline: {stripped}")

    def test_every_helper_execution_is_sandboxed(self):
        """The gateway must not exec the compiled helper unwrapped.

        The helper is built on demand from Swift that ships in the package. Even
        though that source sits at the same trust level as the surrounding Python,
        routing the exec through the repo's own chokepoint is strictly better than
        arguing the boundary — and `mode="strict"` was verified to leave batch,
        inventory and streaming all fully functional.

        Pinned by source inspection: a new `create_subprocess_exec` on the helper
        that skips `_sandboxed` would silently reopen this.
        """
        src = inspect.getsource(apple_speech)
        # Every helper spawn goes through the wrapper, and the wrapper asks for strict.
        assert 'sandbox.sandboxed_spawn_argv(argv, mode="strict"' in src
        for fn in (apple_speech.transcribe, apple_speech.inventory):
            body = inspect.getsource(fn)
            if "create_subprocess_exec" in body:
                assert "_sandboxed" in body, fn.__name__
        start = inspect.getsource(apple_speech.StreamingSession.start)
        assert "_sandboxed" in start

    def test_sandbox_wrapper_scrubs_the_toolchain_env(self):
        """The wrapper must layer the sandbox ON TOP of the scrubbed build env.

        Passing `os.environ` here would undo `_build_env`'s stripping of
        `DEVELOPER_DIR`/`SDKROOT`/`TOOLCHAINS`/`SWIFT_EXEC`.
        """
        src = inspect.getsource(apple_speech._sandboxed)
        assert "env=_build_env()" in src

    def test_untrusted_toolchain_locations_are_refused(self):
        """A compiler the agent can write must never be selected.

        `_build_helper` compiles with whatever the resolver returns and the gateway
        executes the product, so an agent-writable `swiftc` is the same escalation
        as an agent-writable output directory. `~/.local/bin` is on PATH for a
        shell-launched gateway (mlx-whisper already installs there), which is what
        makes this reachable rather than theoretical.
        """
        untrusted = [
            os.path.expanduser("~/.local/bin/swiftc"),
            os.path.expanduser("~/bin/swiftc"),
            "/tmp/swiftc",
            "/opt/homebrew/bin/swiftc",
        ]
        for path in untrusted:
            assert not apple_speech._is_trusted_toolchain(path), path

    def test_resolver_never_consults_path(self):
        """The loop-safe resolver must return a fixed trusted path or nothing.

        Pinned by source inspection as well as behaviour: re-adding a
        `shutil.which("swiftc")` here would silently reopen the escalation above,
        and no behavioural assertion catches that on a host whose PATH happens to
        hold only trusted entries.
        """
        src = inspect.getsource(apple_speech._swiftc_fast)
        # Strip the docstring: it legitimately discusses `shutil.which` (and uses
        # the English word "which"), so scanning the raw source is a false positive.
        body = src.split('"""')[-1]
        assert "which(" not in body, "PATH lookup reopens the untrusted-compiler hole"
        resolved = apple_speech._swiftc_fast()
        assert resolved is None or resolved in apple_speech._SWIFTC_FIXED_PATHS

    def test_trusted_toolchain_accepts_a_real_install(self):
        """The tightened check must not refuse a genuine toolchain."""
        real = [p for p in apple_speech._SWIFTC_FIXED_PATHS if os.path.isfile(p)]
        if not real:
            pytest.skip("no Swift toolchain on this host")
        for path in real:
            assert apple_speech._is_trusted_toolchain(path), path
        assert apple_speech._swiftc_fast() in real

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX,
        reason="POSIX mode bits: chmod_safe is a documented no-op on Windows, where "
        "the directory is protected by an ACL rather than a mode",
    )
    def test_dir_mode_is_repaired_on_a_cache_hit(self, monkeypatch, tmp_path):
        """The 0700 repair must run even when the helper is already built.

        Placed after the cache-hit early return it would never run in the steady
        state, leaving the directory that holds a gateway-executed binary at
        whatever the umask gave it. This pins the ordering, not just the mode.
        """
        cache = tmp_path / "run" / "apple-speech"
        cache.mkdir(parents=True)
        # 0o755 is the CONDITION UNDER TEST, not a permission this code wants: the
        # assertion below proves the repair tightens it to 0o700.
        os.chmod(cache, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
        binary = cache / apple_speech._HELPER_NAME
        binary.write_text("#!/bin/sh\ntrue\n")
        # Owner-only is correct for an executable; semgrep's 0o644 suggestion would
        # strip the execute bit. Same rationale as `sandbox.py::_ensure_run_dir`.
        os.chmod(binary, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
        stamp = cache / f"{apple_speech._HELPER_NAME}.stamp"
        stamp.write_text(
            apple_speech._source_fingerprint("AppleTranscribe.swift"), encoding="utf-8"
        )
        monkeypatch.setattr(apple_speech, "_cache_dir", lambda: cache)

        # A cache hit: returns the existing binary without compiling anything.
        got = apple_speech._build_helper(
            apple_speech._HELPER_NAME, "AppleTranscribe.swift", build=False
        )
        assert got == str(binary)
        assert oct(os.stat(cache).st_mode & 0o777) == "0o700"

    def test_the_xcrun_shim_is_never_trusted_as_a_compiler(self):
        """`/usr/bin/swiftc` must not be a candidate.

        It shares an inode with `/usr/bin/clang` because it is the `xcrun` shim, not
        a compiler: it reads `DEVELOPER_DIR` and delegates. Trusting it verifies the
        shim while executing `$DEVELOPER_DIR/usr/bin/swiftc` unchecked — the same
        escalation as the PATH lookup, reached through an env var instead. Every
        fixed candidate must be a concrete toolchain binary.
        """
        assert "/usr/bin/swiftc" not in apple_speech._SWIFTC_FIXED_PATHS
        for path in apple_speech._SWIFTC_FIXED_PATHS:
            assert "/Developer" in path, path

    def test_toolchain_redirect_env_is_stripped_from_children(self):
        """A child must not inherit the vars that redirect the toolchain.

        `subprocess.run` without `env=` inherits `os.environ`, so an agent that can
        write a shell rc file exports `DEVELOPER_DIR` and the next shell-launched
        gateway compiles — then executes — with a user-writable toolchain.
        """
        for var in apple_speech._TOOLCHAIN_ENV_OVERRIDES:
            os.environ[var] = "/tmp/attacker-toolchain"
        try:
            env = apple_speech._build_env()
            for var in apple_speech._TOOLCHAIN_ENV_OVERRIDES:
                assert var not in env, var
            assert env["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"
        finally:
            for var in apple_speech._TOOLCHAIN_ENV_OVERRIDES:
                os.environ.pop(var, None)

    def test_every_toolchain_subprocess_passes_the_scrubbed_env(self):
        """Pinned by source: an added call site that omits `env=` reopens the hole."""
        for fn in (apple_speech._swiftc, apple_speech._sdk_path, apple_speech._build_helper):
            src = inspect.getsource(fn)
            if "subprocess.run" in src:
                assert "env=_build_env()" in src, fn.__name__

    def test_the_compiler_is_given_an_explicit_sdk(self):
        """A concrete `swiftc` cannot find the standard library without `-sdk`.

        This is the real reason the shim looked necessary; pinning it keeps the fix
        from being "reverted" back to trusting the shim.
        """
        src = inspect.getsource(apple_speech._build_helper)
        assert '"-sdk"' in src

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX,
        reason="os.getuid and root ownership are POSIX concepts; apple_speech is "
        "macOS-only anyway",
    )
    def test_a_planted_bundle_under_a_trusted_prefix_is_refused(self, monkeypatch):
        """Ownership, not path shape, is what makes a prefix trustworthy.

        `/Applications` is `775 root:admin` on macOS, so an agent can create an
        `Xcode.app` there when none exists. A prefix match alone would then accept
        it. What separates real from planted is that a genuine bundle is
        `root:wheel` while anything the agent creates is owned by the invoking
        user — so a user-owned bundle must be refused even at the right path.
        """
        real_lstat = os.lstat
        planted = "/Applications/Xcode.app"

        class _UserOwned:
            st_uid = os.getuid()
            st_mode = 0o40755

        def fake_lstat(path, *a, **kw):
            return _UserOwned() if str(path).startswith(planted) else real_lstat(path, *a, **kw)

        monkeypatch.setattr(os, "lstat", fake_lstat)
        xcode = next(p for p in apple_speech._SWIFTC_FIXED_PATHS if p.startswith(planted))
        assert not apple_speech._is_trusted_toolchain(xcode)

    def test_helper_lives_behind_the_sensitive_path_fence(self):
        """The gateway EXECUTES the compiled helper, so its directory must not be
        agent-writable — otherwise "the agent can write a file" becomes "the agent
        can run code as the gateway". `run` is on `_CREW_SECRET_LEAVES`; `cache` is
        not, which is what made the original location a privilege escalation."""
        from kiro_crew.security import is_sensitive_path

        cache = apple_speech._cache_dir()
        assert is_sensitive_path(str(cache)), cache
        assert is_sensitive_path(str(cache / apple_speech._HELPER_NAME))
        assert cache.parent.name == "run", cache

    def test_swift_source_ships_with_the_package(self):
        """The helper is shipped as source and compiled on the host, so the file must
        exist inside the installed package — a missing entry in setup.cfg's
        package_data would make the provider silently unavailable on every wheel."""
        assert apple_speech._source_path().is_file()

    def test_fingerprint_changes_with_toolchain(self):
        with patch.object(apple_speech, "_swiftc", return_value="/a/swiftc"):
            first = apple_speech._source_fingerprint()
        with patch.object(apple_speech, "_swiftc", return_value="/b/swiftc"):
            second = apple_speech._source_fingerprint()
        assert first != second

    def test_no_build_when_build_disabled(self, tmp_path):
        with patch.object(apple_speech, "_cache_dir", return_value=tmp_path):
            assert apple_speech.helper_path(build=False) is None


def _passthrough_sandbox():
    """Stub `_sandboxed` to a no-op wrapper.

    These tests exercise how `transcribe` handles the HELPER's output; the sandbox
    is not their subject. Without this they fail on Linux CI, where no OS sandbox
    backend exists and the fail-closed early return fires before the logic under
    test is ever reached — the wrapper must be neutralised, not the assertion
    loosened.
    """
    return patch.object(apple_speech, "_sandboxed", side_effect=lambda argv: (argv, {}, None))


def _cleanup_file_sandbox(tmp_path):
    """Stub `_sandboxed` like a host WITH a sandbox backend: a real cleanup file.

    `sandboxed_spawn_argv` returns a real temp launcher/profile as its third
    element on any host with a backend, and the caller must unlink it after the
    child exits. Returns ``(patcher, created)`` where *created* collects every
    file handed out, so a test can assert the call site dropped each one on the
    exit path under test.
    """
    created: list = []

    def _fake(argv):
        launcher = tmp_path / f"sb-launcher-{len(created)}"
        launcher.write_text("# fake sandbox launcher/profile")
        created.append(launcher)
        return argv, {}, str(launcher)

    return patch.object(apple_speech, "_sandboxed", side_effect=_fake), created


class TestTranscribePlumbing:
    @pytest.mark.asyncio
    async def test_unavailable_returns_reason_not_exception(self):
        with patch.object(
            apple_speech,
            "availability",
            return_value=apple_speech.Availability(False, "nope"),
        ):
            text, meta = await apple_speech.transcribe("/tmp/x.wav")
        assert text is None
        assert meta["error"] == "nope"

    @pytest.mark.asyncio
    async def test_native_suffix_skips_ffmpeg(self):
        """A format AVAudioFile reads natively must not pay for a transcode."""
        path, is_temp = await apple_speech._to_native_audio("/tmp/voice.wav")
        assert (path, is_temp) == ("/tmp/voice.wav", False)

    @pytest.mark.asyncio
    async def test_webm_without_ffmpeg_degrades_instead_of_refusing(self):
        """The dashboard records .webm, which the framework cannot read. With no
        ffmpeg we still hand the original path over so the caller surfaces the
        framework's own error rather than a silent unavailability."""
        with (
            patch("kiro_crew.transcribe._find_ffmpeg", return_value=None),
            patch("kiro_crew.transcribe.ensure_ffmpeg_in_path"),
        ):
            path, is_temp = await apple_speech._to_native_audio("/tmp/voice.webm")
        assert (path, is_temp) == ("/tmp/voice.webm", False)

    @pytest.mark.asyncio
    async def test_helper_error_json_is_propagated(self):
        """The helper reports failures as JSON on stdout; that payload is the
        diagnostic the caller logs, so it must survive intact."""
        proc = AsyncMock()
        proc.communicate = AsyncMock(
            return_value=(json.dumps({"error": "locale xx-YY is not supported"}).encode(), b"")
        )
        proc.returncode = 1
        with (
            patch.object(
                apple_speech, "availability", return_value=apple_speech.Availability(True)
            ),
            patch.object(apple_speech, "helper_path", return_value="/fake/helper"),
            patch("asyncio.create_subprocess_exec", return_value=proc),
            _passthrough_sandbox(),
        ):
            text, meta = await apple_speech.transcribe("/tmp/x.wav", locale="xx-YY")
        assert text is None
        assert "not supported" in meta["error"]

    @pytest.mark.asyncio
    async def test_empty_stdout_is_an_error_not_an_empty_transcript(self):
        """A crashed helper must not read as 'the audio was silent'."""
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b"dyld: symbol not found"))
        proc.returncode = 0
        with (
            patch.object(
                apple_speech, "availability", return_value=apple_speech.Availability(True)
            ),
            patch.object(apple_speech, "helper_path", return_value="/fake/helper"),
            patch("asyncio.create_subprocess_exec", return_value=proc),
            _passthrough_sandbox(),
        ):
            text, meta = await apple_speech.transcribe("/tmp/x.wav")
        assert text is None
        assert "no output" in meta["error"]

    @pytest.mark.asyncio
    async def test_successful_payload_returns_text_and_metrics(self):
        payload = {
            "text": "hello there",
            "locale": "en-US",
            "audio_secs": 2.0,
            "transcribe_secs": 0.15,
        }
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(json.dumps(payload).encode(), b""))
        proc.returncode = 0
        with (
            patch.object(
                apple_speech, "availability", return_value=apple_speech.Availability(True)
            ),
            patch.object(apple_speech, "helper_path", return_value="/fake/helper"),
            patch("asyncio.create_subprocess_exec", return_value=proc),
            _passthrough_sandbox(),
        ):
            text, meta = await apple_speech.transcribe("/tmp/x.wav")
        assert text == "hello there"
        assert meta["transcribe_secs"] == 0.15


class TestTranscodeTempOwnership:
    """The transcode temp is invocation-owned until explicitly handed over.

    `_to_native_audio` creates the `.wav` with `mkstemp`, so it owns the file
    until the success return transfers it to the caller; `transcribe` then owns
    the received temp until its cleanup `finally`. Every failure exit on either
    side must remove the owned temp, while an original input path — not created
    by these invocations — is never removed on any path.
    """

    @staticmethod
    def _owned_temp(tmp_path, monkeypatch):
        """Pin `_mkstemp_path` to a known file so the tests can watch it."""
        owned = tmp_path / "owned.wav"
        owned.write_bytes(b"")
        monkeypatch.setattr(apple_speech, "_mkstemp_path", lambda suffix: str(owned))
        return owned

    @pytest.mark.asyncio
    async def test_spawn_failure_removes_the_owned_temp(self, tmp_path, monkeypatch):
        """An ffmpeg that fails to spawn never ran, so nothing else will ever
        remove the mkstemp output — the invocation must."""
        owned = self._owned_temp(tmp_path, monkeypatch)
        src = tmp_path / "voice.webm"
        src.write_bytes(b"data")
        with (
            patch("kiro_crew.transcribe._find_ffmpeg", return_value="/fake/ffmpeg"),
            patch("kiro_crew.transcribe.ensure_ffmpeg_in_path"),
            patch("asyncio.create_subprocess_exec", side_effect=OSError("spawn failed")),
        ):
            with pytest.raises(OSError):
                await apple_speech._to_native_audio(str(src))
        assert not owned.exists()
        assert src.exists()

    @pytest.mark.asyncio
    async def test_cancellation_reaps_ffmpeg_before_removing_the_owned_temp(
        self, tmp_path, monkeypatch
    ):
        """`CancelledError` is a `BaseException`, so an `except Exception` guard
        would miss it. The child must also be killed AND reaped before the
        unlink: Windows keeps the output file locked until the child fully
        exits, so an unlink issued earlier fails and the temp survives."""
        owned = self._owned_temp(tmp_path, monkeypatch)
        src = tmp_path / "voice.webm"
        src.write_bytes(b"data")
        events: list[str] = []

        class _Proc:
            def __init__(self):
                self._calls = 0

            async def communicate(self):
                self._calls += 1
                if self._calls == 1:
                    raise asyncio.CancelledError()
                events.append("reaped")
                return b"", b""

            def kill(self):
                events.append("killed")

        real_unlink = os.unlink

        def tracked_unlink(path, *args, **kwargs):
            if str(path) == str(owned):
                events.append("unlinked")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(apple_speech.os, "unlink", tracked_unlink)
        with (
            patch("kiro_crew.transcribe._find_ffmpeg", return_value="/fake/ffmpeg"),
            patch("kiro_crew.transcribe.ensure_ffmpeg_in_path"),
            patch("asyncio.create_subprocess_exec", return_value=_Proc()),
        ):
            with pytest.raises(asyncio.CancelledError):
                await apple_speech._to_native_audio(str(src))
        assert events == ["killed", "reaped", "unlinked"]
        assert not owned.exists()
        assert src.exists()

    @pytest.mark.asyncio
    async def test_successful_transcode_still_hands_the_temp_to_the_caller(
        self, tmp_path, monkeypatch
    ):
        """The cleanup must not eat the success path: the caller's existing
        cleanup relies on receiving the temp path with ownership."""
        owned = self._owned_temp(tmp_path, monkeypatch)
        src = tmp_path / "voice.webm"
        src.write_bytes(b"data")
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        with (
            patch("kiro_crew.transcribe._find_ffmpeg", return_value="/fake/ffmpeg"),
            patch("kiro_crew.transcribe.ensure_ffmpeg_in_path"),
            patch("asyncio.create_subprocess_exec", return_value=proc),
        ):
            path, is_temp = await apple_speech._to_native_audio(str(src))
        assert (path, is_temp) == (str(owned), True)
        assert owned.exists()
        assert src.exists()

    @pytest.mark.asyncio
    async def test_sandbox_rejection_removes_the_owned_native_temp(self, tmp_path, monkeypatch):
        """The fail-closed sandbox refusal returns after `transcribe` received an
        owned temp but used to exit before the cleanup `finally` was armed."""
        from kiro_crew import sandbox as sb

        owned = tmp_path / "native.wav"
        owned.write_bytes(b"x")

        async def fake_native(_path):
            return str(owned), True

        def boom(argv):
            raise sb.SandboxUnavailableError(
                "no backend: unshare(CLONE_NEWNS) EPERM", "no_backend", "EPERM"
            )

        monkeypatch.setattr(apple_speech, "_to_native_audio", fake_native)
        monkeypatch.setattr(apple_speech, "availability", lambda: apple_speech.Availability(True))
        monkeypatch.setattr(apple_speech, "helper_path", lambda *a, **k: "/fake/helper")
        monkeypatch.setattr(apple_speech, "_sandboxed", boom)
        text, meta = await apple_speech.transcribe(str(tmp_path / "orig.webm"))
        assert text is None
        assert "no backend" in meta["error"]
        assert not owned.exists()

    @pytest.mark.asyncio
    async def test_sandbox_rejection_never_removes_an_original_input(self, tmp_path, monkeypatch):
        """A non-temp native path is the caller's own file; the widened cleanup
        must stay keyed on the ownership flag, not on reaching the exit."""
        from kiro_crew import sandbox as sb

        original = tmp_path / "voice.wav"
        original.write_bytes(b"x")

        def boom(argv):
            raise sb.SandboxUnavailableError(
                "no backend: unshare(CLONE_NEWNS) EPERM", "no_backend", "EPERM"
            )

        monkeypatch.setattr(apple_speech, "availability", lambda: apple_speech.Availability(True))
        monkeypatch.setattr(apple_speech, "helper_path", lambda *a, **k: "/fake/helper")
        monkeypatch.setattr(apple_speech, "_sandboxed", boom)
        text, meta = await apple_speech.transcribe(str(original))
        assert text is None
        assert "no backend" in meta["error"]
        assert original.exists()


class TestStreamingSession:
    """The live path. Plumbing only here; real audio is the macOS-only test below."""

    @pytest.mark.asyncio
    async def test_start_reports_unavailability_instead_of_raising(self):
        session = apple_speech.StreamingSession()
        with patch.object(
            apple_speech,
            "availability",
            return_value=apple_speech.Availability(False, "macOS only"),
        ):
            assert await session.start() == "macOS only"

    @pytest.mark.asyncio
    async def test_start_reports_unbuildable_helper(self):
        session = apple_speech.StreamingSession()
        with (
            patch.object(
                apple_speech, "availability", return_value=apple_speech.Availability(True)
            ),
            patch.object(apple_speech, "stream_helper_path", return_value=None),
        ):
            problem = await session.start()
        assert "could not be built" in problem

    @pytest.mark.asyncio
    async def test_feed_on_dead_process_returns_false(self):
        """The caller uses the return value to stop pumping audio; a dead helper must
        not raise into the WebSocket loop."""
        session = apple_speech.StreamingSession()
        assert await session.feed(b"\x00\x01") is False

    @pytest.mark.asyncio
    async def test_finals_accumulate_across_events(self):
        """`finish()` returns the concatenated finals — the frontend also joins them,
        but the backend value is what a non-browser caller gets."""
        session = apple_speech.StreamingSession()
        for payload in (
            {"type": "ready"},
            {"type": "partial", "text": "hel"},
            {"type": "final", "text": "hello "},
            {"type": "final", "text": "world"},
        ):
            session._queue.put_nowait(payload)
        session._queue.put_nowait(None)
        kinds = [e["type"] async for e in session.events()]
        assert kinds == ["ready", "partial", "final", "final"]
        assert session._final_text == "hello world"

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self):
        session = apple_speech.StreamingSession()
        await session.close()
        await session.close()


class TestSandboxCleanupPathIsDropped:
    """Every `_sandboxed` call site must unlink the returned cleanup path (#5776).

    The third tuple element is a real temp file on any host with a sandbox
    backend (Linux namespace launcher / macOS ``.sb`` profile), and the
    ``sandboxed_spawn_argv`` contract makes the CALLER unlink it after the child
    exits. A site that discards it leaks one file per call, forever — and the
    leak is per-call, so a launcher dropped only on the success path still
    accumulates. Each test hands back a real file and asserts it is gone on the
    exit path under test.
    """

    @staticmethod
    def _availability_ok():
        return patch.object(
            apple_speech, "availability", return_value=apple_speech.Availability(True)
        )

    @pytest.mark.asyncio
    async def test_transcribe_drops_launcher_on_success(self, tmp_path):
        sandbox_patch, created = _cleanup_file_sandbox(tmp_path)
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(json.dumps({"text": "hi"}).encode(), b""))
        proc.returncode = 0
        with (
            self._availability_ok(),
            patch.object(apple_speech, "helper_path", return_value="/fake/helper"),
            patch("asyncio.create_subprocess_exec", return_value=proc),
            sandbox_patch,
        ):
            text, _ = await apple_speech.transcribe("/tmp/x.wav")
        assert text == "hi"
        assert created and not any(f.exists() for f in created)

    @pytest.mark.asyncio
    async def test_transcribe_drops_launcher_on_timeout(self, tmp_path):
        """The timeout exit kills the child, and must drop the launcher too."""
        sandbox_patch, created = _cleanup_file_sandbox(tmp_path)
        killed = asyncio.Event()
        proc = AsyncMock()

        async def communicate():
            # Hangs until kill(), like a wedged helper; returns once killed, so
            # the reap in the timeout handler completes instead of hanging.
            # No timing flake: the wait is unbounded-until-kill, and the small
            # positive timeout (NOT 0: wait_for's `timeout <= 0` fast path
            # cancels before the first step) only decides when kill() happens.
            if not killed.is_set():
                await killed.wait()
            return b"", b""

        proc.communicate = communicate
        proc.kill = Mock(side_effect=killed.set)
        proc.returncode = None
        with (
            self._availability_ok(),
            patch.object(apple_speech, "helper_path", return_value="/fake/helper"),
            patch("asyncio.create_subprocess_exec", return_value=proc),
            sandbox_patch,
        ):
            text, meta = await apple_speech.transcribe("/tmp/x.wav", timeout_secs=0.01)
        assert text is None
        assert "timed out" in meta["error"]
        proc.kill.assert_called_once()
        assert created and not any(f.exists() for f in created)

    @pytest.mark.asyncio
    async def test_transcribe_drops_launcher_on_spawn_failure(self, tmp_path):
        """OSError from the spawn means no child ever held the launcher."""
        sandbox_patch, created = _cleanup_file_sandbox(tmp_path)
        with (
            self._availability_ok(),
            patch.object(apple_speech, "helper_path", return_value="/fake/helper"),
            patch("asyncio.create_subprocess_exec", side_effect=OSError("boom")),
            sandbox_patch,
        ):
            text, meta = await apple_speech.transcribe("/tmp/x.wav")
        assert text is None
        assert "could not run speech helper" in meta["error"]
        assert created and not any(f.exists() for f in created)

    @pytest.mark.asyncio
    async def test_inventory_drops_launcher_on_success(self, tmp_path):
        sandbox_patch, created = _cleanup_file_sandbox(tmp_path)
        proc = AsyncMock()
        proc.communicate = AsyncMock(
            return_value=(json.dumps({"supported": ["en-US"], "installed": []}).encode(), b"")
        )
        with (
            patch.object(apple_speech, "helper_path", return_value="/fake/helper"),
            patch("asyncio.create_subprocess_exec", return_value=proc),
            sandbox_patch,
        ):
            result = await apple_speech.inventory()
        assert result == {"supported": ["en-US"], "installed": []}
        assert created and not any(f.exists() for f in created)

    @pytest.mark.asyncio
    async def test_inventory_drops_launcher_on_spawn_failure(self, tmp_path):
        sandbox_patch, created = _cleanup_file_sandbox(tmp_path)
        with (
            patch.object(apple_speech, "helper_path", return_value="/fake/helper"),
            patch("asyncio.create_subprocess_exec", side_effect=OSError("boom")),
            sandbox_patch,
        ):
            result = await apple_speech.inventory()
        assert "error" in result
        assert created and not any(f.exists() for f in created)

    @pytest.mark.asyncio
    async def test_inventory_timeout_kills_then_drops_launcher(self, tmp_path):
        """The timeout exit must reap the wedged helper BEFORE the finally
        unlinks — the contract is 'after the child exits', and a helper past
        the ceiling must not be left running with nobody waiting on it."""
        sandbox_patch, created = _cleanup_file_sandbox(tmp_path)
        killed = asyncio.Event()
        proc = AsyncMock()

        async def communicate():
            if not killed.is_set():
                raise asyncio.TimeoutError  # stands in for the wait_for ceiling
            return b"", b""

        proc.communicate = communicate
        proc.kill = Mock(side_effect=killed.set)
        proc.returncode = None
        with (
            patch.object(apple_speech, "helper_path", return_value="/fake/helper"),
            patch("asyncio.create_subprocess_exec", return_value=proc),
            sandbox_patch,
        ):
            result = await apple_speech.inventory()
        assert "error" in result
        proc.kill.assert_called_once()
        assert created and not any(f.exists() for f in created)

    @pytest.mark.asyncio
    async def test_streaming_start_failure_drops_launcher(self, tmp_path):
        """The spawn-OSError exit has a launcher but no child; it must drop it."""
        sandbox_patch, created = _cleanup_file_sandbox(tmp_path)
        session = apple_speech.StreamingSession()
        with (
            self._availability_ok(),
            patch.object(apple_speech, "stream_helper_path", return_value="/fake/helper"),
            patch("asyncio.create_subprocess_exec", side_effect=OSError("boom")),
            sandbox_patch,
        ):
            problem = await session.start()
        assert "could not start streaming helper" in problem
        assert created and not any(f.exists() for f in created)
        assert session._sb_cleanup is None

    @pytest.mark.asyncio
    async def test_streaming_launcher_survives_start_and_drops_on_close(self, tmp_path):
        """The streaming child OUTLIVES start(): unlinking there would pull the
        profile/launcher out from under a live process. It is held on the
        session and dropped in close(), after the process teardown — and a
        double close must not trip over the already-removed file."""
        sandbox_patch, created = _cleanup_file_sandbox(tmp_path)
        proc = AsyncMock()
        proc.stdout.readline = AsyncMock(side_effect=[b'{"type": "ready"}\n', b""])
        proc.kill = Mock()
        proc.returncode = None
        session = apple_speech.StreamingSession()
        with (
            self._availability_ok(),
            patch.object(apple_speech, "stream_helper_path", return_value="/fake/helper"),
            patch("asyncio.create_subprocess_exec", return_value=proc),
            sandbox_patch,
        ):
            assert await session.start() == ""
            assert created and all(f.exists() for f in created), "unlinked under a live child"
            await session.close()
            assert not any(f.exists() for f in created)
            await session.close()  # idempotent: the swap keeps this from re-unlinking

    @pytest.mark.asyncio
    async def test_streaming_finish_then_close_drops_launcher_once(self, tmp_path):
        """The production teardown order (stt_stream.py) is finish() then
        close(). finish() may leave the process draining, so the unlink belongs
        to close() alone — a refactor moving it into finish() fails here."""
        sandbox_patch, created = _cleanup_file_sandbox(tmp_path)
        proc = AsyncMock()
        proc.stdout.readline = AsyncMock(side_effect=[b'{"type": "ready"}\n', b""])
        proc.kill = Mock()
        proc.returncode = None
        session = apple_speech.StreamingSession()
        with (
            self._availability_ok(),
            patch.object(apple_speech, "stream_helper_path", return_value="/fake/helper"),
            patch("asyncio.create_subprocess_exec", return_value=proc),
            sandbox_patch,
        ):
            assert await session.start() == ""
            await session.finish()
            assert created and all(f.exists() for f in created), "finish() must not unlink"
            await session.close()
            assert not any(f.exists() for f in created)

    @pytest.mark.asyncio
    async def test_streaming_ready_timeout_drops_launcher(self, tmp_path, monkeypatch):
        """The readiness-timeout exit reaches the unlink through the close()
        that start() already performs there."""
        monkeypatch.setattr(apple_speech, "_READY_TIMEOUT_SECS", 0.05)
        sandbox_patch, created = _cleanup_file_sandbox(tmp_path)
        proc = AsyncMock()

        async def never_ready(*_a, **_k):
            await asyncio.sleep(60)
            return b""

        proc.stdout.readline = never_ready
        proc.kill = Mock()
        proc.returncode = None
        session = apple_speech.StreamingSession()
        with (
            self._availability_ok(),
            patch.object(apple_speech, "stream_helper_path", return_value="/fake/helper"),
            patch("asyncio.create_subprocess_exec", return_value=proc),
            sandbox_patch,
        ):
            problem = await session.start()
        assert problem == "streaming helper did not become ready"
        assert created and not any(f.exists() for f in created)


class TestSandboxCancellationWindows:
    """Cancellation around the launcher lifecycle must not orphan the temp.

    `asyncio.CancelledError` is a `BaseException`, so the `except Exception` /
    `except OSError` paths that own the unlink never see it. Two window shapes
    are pinned here, at each await point: (1) cancellation DURING the
    worker-thread `_sandboxed` hop — the thread still creates the launcher, but
    the returned tuple is never bound, so no `finally` and no session field can
    ever reach it; (2) `StreamingSession.start()` cancelled AFTER the tuple is
    bound (mid-spawn, or anywhere in the readiness wait). The caller-side
    guarantee at the `await session.start()` call site is pinned in
    test_stt_stream.py.
    """

    @staticmethod
    def _availability_ok():
        return patch.object(
            apple_speech, "availability", return_value=apple_speech.Availability(True)
        )

    @staticmethod
    def _blocking_sandbox(tmp_path):
        """A `_sandboxed` stub that parks the worker thread until released.

        Reproduces the race deterministically: the awaiting coroutine is
        cancelled while the thread is still inside `sandboxed_spawn_argv`, and
        the launcher only comes into being AFTER the cancellation landed.
        """
        entered = threading.Event()
        release = threading.Event()
        created: list = []

        def _fake(argv):
            entered.set()
            assert release.wait(timeout=10), "test never released the worker thread"
            launcher = tmp_path / f"sb-launcher-{len(created)}"
            launcher.write_text("# fake sandbox launcher/profile")
            created.append(launcher)
            return argv, {}, str(launcher)

        patcher = patch.object(apple_speech, "_sandboxed", side_effect=_fake)
        return patcher, entered, release, created

    @staticmethod
    async def _cancel_during_hop(task, entered, release):
        """Cancel *task* while the worker thread is parked inside `_sandboxed`.

        The thread entering the stub implies the awaiter is already suspended at
        the shielded hop: the executor submission happens inside the inner task,
        which only runs once the awaiting coroutine has yielded.
        """
        await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_transcribe_cancelled_in_sandbox_hop_drops_launcher(self, tmp_path):
        sandbox_patch, entered, release, created = self._blocking_sandbox(tmp_path)
        with (
            self._availability_ok(),
            patch.object(apple_speech, "helper_path", return_value="/fake/helper"),
            sandbox_patch,
        ):
            task = asyncio.create_task(apple_speech.transcribe("/tmp/x.wav"))
            await self._cancel_during_hop(task, entered, release)
        assert created and not any(f.exists() for f in created)

    @pytest.mark.asyncio
    async def test_inventory_cancelled_in_sandbox_hop_drops_launcher(self, tmp_path):
        sandbox_patch, entered, release, created = self._blocking_sandbox(tmp_path)
        with (
            patch.object(apple_speech, "helper_path", return_value="/fake/helper"),
            sandbox_patch,
        ):
            task = asyncio.create_task(apple_speech.inventory())
            await self._cancel_during_hop(task, entered, release)
        assert created and not any(f.exists() for f in created)

    @pytest.mark.asyncio
    async def test_streaming_start_cancelled_in_sandbox_hop_drops_launcher(self, tmp_path):
        sandbox_patch, entered, release, created = self._blocking_sandbox(tmp_path)
        session = apple_speech.StreamingSession()
        with (
            self._availability_ok(),
            patch.object(apple_speech, "stream_helper_path", return_value="/fake/helper"),
            sandbox_patch,
        ):
            task = asyncio.create_task(session.start())
            await self._cancel_during_hop(task, entered, release)
        assert created and not any(f.exists() for f in created)
        assert session._sb_cleanup is None

    @pytest.mark.asyncio
    async def test_streaming_start_cancelled_mid_spawn_drops_launcher(self, tmp_path):
        """After the tuple is bound, a cancelled spawn must still drop the
        launcher: `CancelledError` is not `OSError`, so the in-place drop on the
        spawn-failure path never sees it."""
        sandbox_patch, created = _cleanup_file_sandbox(tmp_path)
        spawn_entered = asyncio.Event()

        async def hanging_spawn(*_a, **_k):
            spawn_entered.set()
            await asyncio.sleep(60)

        session = apple_speech.StreamingSession()
        with (
            self._availability_ok(),
            patch.object(apple_speech, "stream_helper_path", return_value="/fake/helper"),
            patch("asyncio.create_subprocess_exec", side_effect=hanging_spawn),
            sandbox_patch,
        ):
            task = asyncio.create_task(session.start())
            await spawn_entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert created and not any(f.exists() for f in created)
        assert session._sb_cleanup is None

    @pytest.mark.asyncio
    async def test_streaming_start_cancelled_in_ready_wait_reaps_then_drops(self, tmp_path):
        """Cancellation in the ~20s readiness wait leaves a LIVE helper riding
        along with the launcher; the teardown must kill+reap it before the
        unlink (Windows keeps the file locked until the child exits)."""
        sandbox_patch, created = _cleanup_file_sandbox(tmp_path)
        proc = AsyncMock()

        async def never_ready(*_a, **_k):
            await asyncio.sleep(60)
            return b""

        proc.stdout.readline = never_ready
        proc.kill = Mock()
        proc.returncode = None
        session = apple_speech.StreamingSession()
        with (
            self._availability_ok(),
            patch.object(apple_speech, "stream_helper_path", return_value="/fake/helper"),
            patch("asyncio.create_subprocess_exec", return_value=proc),
            sandbox_patch,
        ):
            task = asyncio.create_task(session.start())
            # The pump task is created immediately before the readiness wait, so
            # its appearance means start() is suspended at that wait (create_task
            # does not yield; the wait_for is the next suspension point).
            while session._pump is None:
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        proc.kill.assert_called_once()
        assert created and not any(f.exists() for f in created)
        assert session._sb_cleanup is None


class TestStreamingEndpointGate:
    def test_apple_is_an_accepted_streaming_provider(self):
        """The WS endpoint gates on this tuple; `apple` must be in it or the live path
        is unreachable no matter what the provider implements."""
        from kiro_crew.dashboard import stt_stream

        assert "apple" in stt_stream._STREAMING_PROVIDERS
        assert "transcribe" in stt_stream._STREAMING_PROVIDERS

    def test_batch_only_providers_stay_out(self):
        """whisper/mlx are whole-file CLIs with no partial-result channel — offering
        them on the streaming endpoint would hang the client until end of audio."""
        from kiro_crew.dashboard import stt_stream

        assert "whisper" not in stt_stream._STREAMING_PROVIDERS
        assert "mlx" not in stt_stream._STREAMING_PROVIDERS


class TestNoBlockingCallOnEventLoop:
    """The loop-reachable probes must never spawn a process.

    `transcribe.is_available` runs synchronously on the asyncio loop from the
    `/api/config/stt` GET, `api_stt_transcribe`, and the Slack voice path — its
    sibling `_stt_prereq_commands` is `asyncio.to_thread`'d for exactly this
    reason. An earlier revision reached `helper_path()` from here, which runs
    `swiftc` with a 180s timeout: that freezes chat turns and the liveness
    heartbeat for the whole compile. These tests are the guard.
    """

    @staticmethod
    def _no_spawn():
        """Patch every spawn primitive to raise, so any use fails loudly.

        Also patches the *blocking* resolver and forces a supported-Darwin verdict.
        Without both, these tests are vacuous everywhere CI runs: off macOS
        `availability()` returns at the platform check before touching a resolver,
        and on a macOS runner with the Command Line Tools present `_swiftc_fast`
        short-circuits so `subprocess.run` is never a candidate either. The point
        is to execute the real body on every platform.
        """

        def boom(*args, **kwargs):
            raise AssertionError(f"spawned a subprocess on the event-loop path: {args[:1]}")

        return (
            patch("subprocess.run", boom),
            patch("subprocess.Popen", boom),
            patch("subprocess.check_output", boom),
            patch("kiro_crew.apple_speech._swiftc", boom),
            patch("platform.system", lambda: "Darwin"),
            patch("platform.mac_ver", lambda: ("27.0", ("", "", ""), "arm64")),
            patch.object(apple_speech, "_swiftc_fast", lambda: "/usr/bin/swiftc"),
        )

    def test_availability_spawns_nothing(self):
        with ExitStack() as stack:
            for cm in self._no_spawn():
                stack.enter_context(cm)
            assert apple_speech.availability().ok

    def test_is_available_does_not_build(self):
        """`is_available` answers "can this work", not "is it already compiled" —
        the second question can only be answered by compiling."""
        with ExitStack() as stack:
            for cm in self._no_spawn():
                stack.enter_context(cm)
            assert apple_speech.is_available() is True

    def test_transcribe_is_available_spawns_nothing(self):
        from kiro_crew.transcribe import is_available

        with ExitStack() as stack:
            for cm in self._no_spawn():
                stack.enter_context(cm)
            assert is_available(SttConfig(enabled=True, provider="apple")) is True

    def test_provider_list_spawns_nothing(self):
        """`_stt_providers` is called from the config GET handler on the loop."""
        from kiro_crew.dashboard.handlers import core

        with ExitStack() as stack:
            for cm in self._no_spawn():
                stack.enter_context(cm)
            stack.enter_context(patch.object(core, "_is_apple_silicon", lambda: True))
            assert "apple" in core._stt_providers()

    def test_fast_swiftc_resolver_never_spawns(self):
        def boom(*args, **kwargs):
            raise AssertionError("the loop-safe resolver spawned a subprocess")

        with (
            patch("subprocess.run", boom),
            patch("subprocess.Popen", boom),
            patch("subprocess.check_output", boom),
        ):
            apple_speech._swiftc_fast()

    def test_blocking_resolver_is_only_reached_from_the_build_path(self):
        """`_swiftc` MAY spawn (`xcrun`), so it must not appear in a loop-safe
        function. Guard the seam by construction rather than by convention."""
        import inspect

        for fn in (apple_speech.availability, apple_speech.is_available):
            src = inspect.getsource(fn)
            assert "_swiftc()" not in src, f"{fn.__name__} reaches the spawning resolver"
        assert "_swiftc()" in inspect.getsource(apple_speech._build_helper)


@pytest.mark.skipif(
    not _IS_MACOS or sys.platform != "darwin",
    reason="Apple on-device speech is macOS-only",
)
class TestEndToEndMacOS:
    """One real run: build the helper, synthesize speech, transcribe it.

    Skipped rather than mocked on other platforms because the point is to catch a
    Swift-side regression (an API rename, a signature change) that no mock can see.
    """

    @pytest.mark.asyncio
    async def test_round_trip(self, tmp_path):
        import asyncio
        import shutil

        if apple_speech._macos_major() < apple_speech.MIN_MACOS_MAJOR:
            pytest.skip("host macOS predates the SpeechAnalyzer API")
        if apple_speech._swiftc() is None:
            pytest.skip("no Swift toolchain on this host")
        if not shutil.which("say"):
            pytest.skip("no `say` to synthesize a fixture")

        audio = tmp_path / "sample.aiff"
        proc = await asyncio.create_subprocess_exec("say", "-o", str(audio), "the build is green")
        await proc.wait()
        assert audio.is_file()

        text, meta = await apple_speech.transcribe(str(audio), locale="en-US")
        assert text, f"no transcript: {meta}"
        assert "green" in text.lower()
        assert meta["transcribe_secs"] > 0

    @pytest.mark.asyncio
    async def test_streaming_emits_partials_before_the_audio_ends(self, tmp_path):
        """The whole point of the live path: text must appear WHILE the user speaks.

        Audio is fed at real-time pace, and the assertion is that a partial arrives
        strictly before the last chunk is written. A batching implementation (which is
        what SpeechTranscriber does even with `.fastResults`) fails this.
        """
        import asyncio
        import shutil
        import time
        import wave

        if apple_speech._macos_major() < apple_speech.MIN_MACOS_MAJOR:
            pytest.skip("host macOS predates the SpeechAnalyzer API")
        if apple_speech._swiftc() is None:
            pytest.skip("no Swift toolchain on this host")
        for tool in ("say", "afconvert"):
            if not shutil.which(tool):
                pytest.skip(f"no `{tool}` to build a 16 kHz fixture")

        aiff = tmp_path / "s.aiff"
        proc = await asyncio.create_subprocess_exec(
            "say",
            "-o",
            str(aiff),
            "the continuous integration build is green and the tests all pass",
        )
        await proc.wait()
        wav = tmp_path / "s.wav"
        # LEI16 @ 16 kHz mono — the format the dashboard's audio worklet produces.
        proc = await asyncio.create_subprocess_exec(
            "afconvert", str(aiff), str(wav), "-f", "WAVE", "-d", "LEI16@16000", "-c", "1"
        )
        await proc.wait()
        if not wav.is_file():
            pytest.skip("afconvert did not produce a fixture")

        with wave.open(str(wav), "rb") as w:
            pcm = w.readframes(w.getnframes())
        assert len(pcm) > 32000, "fixture too short to test liveness"

        session = apple_speech.StreamingSession(locale="en-US")
        problem = await session.start()
        if problem:
            pytest.skip(f"streaming helper unavailable: {problem}")

        first_partial_at: list[float] = []

        async def consume() -> None:
            async for event in session.events():
                if event.get("type") == "partial" and not first_partial_at:
                    first_partial_at.append(time.perf_counter())

        try:
            reader = asyncio.create_task(consume())
            t0 = time.perf_counter()
            chunk = 3200  # 100 ms
            for offset in range(0, len(pcm), chunk):
                await session.feed(pcm[offset : offset + chunk])
                target = t0 + (offset + chunk) / 32000
                now = time.perf_counter()
                if target > now:
                    await asyncio.sleep(target - now)
            last_audio_at = time.perf_counter()
            text = await session.finish()
            await asyncio.wait_for(reader, timeout=5)
        finally:
            await session.close()

        assert first_partial_at, "no partial results at all — the live path is broken"
        assert first_partial_at[0] < last_audio_at, (
            "the first partial arrived only after all audio was fed, which means the "
            "module is batching rather than streaming"
        )
        assert "green" in text.lower(), text
