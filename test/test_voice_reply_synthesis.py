"""Tests for each voice_reply provider's synthesis call.

``_synthesize_system`` and ``_synthesize_polly``: the argv
each one builds inside the sandbox wrap, the consent gate in front of Polly, the
failure exits, and the temp file each one owns until every exit removes it
(cancellation included).
"""

from __future__ import annotations

import asyncio
import base64
import os
import tempfile
import threading
from unittest.mock import AsyncMock, patch

import pytest
from voice_reply_helpers import _no_argv_prefixers  # noqa: F401
from voice_reply_helpers import _polly_consented_fixture  # noqa: F401
from voice_reply_helpers import (
    _capture_mkstemp,
    _mock_subprocess,
    _patch_aws_on_path,
    _prepared,
)

from kiro_crew.voice_reply import (
    SYSTEM_ENGINE_ESPEAK,
    SYSTEM_ENGINE_SAPI,
    SYSTEM_ENGINE_SAY,
    _produced_audio,
    _synthesize_polly,
    _synthesize_system,
)

# Stands in for the remedy prose sandbox.wrap_argv builds for kind="no_backend".
# The handlers under test must RELAY this string, not compose their own copy —
# only this kind names the opt-in, so a hardcoded remedy would be wrong for the
# "transient" and "foreign_sandbox" kinds.
_SANDBOX_REMEDY = (
    "No OS-level sandbox backend is available on this host. If this host "
    "genuinely lacks a sandbox backend, set "
    "agent.sandbox_allow_unsandboxed_exec=true in ~/.kiro/crew/config.json."
)


# ── _synthesize_system() ─────────────────────────────────────────────────


def _writing_exec(flag: str, *, size: int = 200):
    """A ``create_subprocess_exec`` stub that writes a WAV at the *flag* argument."""
    captured: dict[str, object] = {}

    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = dict(kwargs)
        out_path = cmd[cmd.index(flag) + 1]
        with open(out_path, "wb") as fh:
            fh.write(b"RIFF" + b"x" * size)
        return _mock_subprocess(returncode=0)

    return fake_exec, captured


def _passthrough_wrap():
    """Patch the sandbox wrap to return its argv unchanged.

    The built-in engine is confined on macOS and Linux, so on those hosts the
    real wrap prepends the launcher and ``cmd[0]`` is not the engine.
    These tests assert how the ENGINE's own argv is built, which is what sits
    inside the wrap either way, so they pin that rather than the host's backend.
    """

    async def identity(cmd, *_a, **_kw):
        return list(cmd), {}, None

    return patch("kiro_crew.voice_reply.sandboxed_spawn_argv_async", side_effect=identity)


class TestSynthesizeSystem:
    @pytest.mark.asyncio
    async def test_no_engine_returns_none(self) -> None:
        with patch("kiro_crew.voice_reply.resolve_system_tts", return_value=None):
            assert await _synthesize_system("hi") is None

    @pytest.mark.asyncio
    async def test_say_argv_and_stdin(self) -> None:
        fake_exec, captured = _writing_exec("-o")
        with (
            patch(
                "kiro_crew.voice_reply.resolve_system_tts",
                return_value=(SYSTEM_ENGINE_SAY, "/usr/bin/say"),
            ),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            _passthrough_wrap(),
        ):
            result = await _synthesize_system("hello", voice="Alex", rate="150%")
        assert result is not None and result.endswith(".wav")
        os.unlink(result)
        cmd = captured["cmd"]
        assert cmd[0] == "/usr/bin/say"
        assert "--file-format=WAVE" in cmd
        assert "--data-format=LEI16@22050" in cmd
        assert cmd[cmd.index("-r") + 1] == "262"
        assert cmd[cmd.index("-v") + 1] == "Alex"
        # The spoken text is piped, never spelled on the command line.
        assert "hello" not in cmd

    @pytest.mark.asyncio
    async def test_a_non_string_voice_degrades_to_the_engine_default(self) -> None:
        """A hand-edited `"system_voice": []` must not reach any engine.

        The value arrives raw from config.json and each engine breaks on it
        differently: SAPI's `_ps_quote` raises AttributeError, and the two argv
        engines raise TypeError inside the spawn. Coerced at this one consumer,
        so the Slack loader and the `_SYNTHESIS_KEYS` table are both covered.
        """
        for bad in ([], {}, 7, None):
            fake_exec, captured = _writing_exec("-o")
            with (
                patch(
                    "kiro_crew.voice_reply.resolve_system_tts",
                    return_value=(SYSTEM_ENGINE_SAY, "/usr/bin/say"),
                ),
                patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
                _passthrough_wrap(),
            ):
                result = await _synthesize_system("hello", voice=bad)  # type: ignore[arg-type]
            assert result is not None, f"voice={bad!r} produced no audio"
            os.unlink(result)
            assert "-v" not in captured["cmd"], f"voice={bad!r} reached argv"

    @pytest.mark.asyncio
    async def test_say_omits_voice_flag_when_unset(self) -> None:
        fake_exec, captured = _writing_exec("-o")
        with (
            patch(
                "kiro_crew.voice_reply.resolve_system_tts",
                return_value=(SYSTEM_ENGINE_SAY, "/usr/bin/say"),
            ),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            _passthrough_wrap(),
        ):
            result = await _synthesize_system("hello")
        assert result is not None
        os.unlink(result)
        assert "-v" not in captured["cmd"]

    @pytest.mark.asyncio
    async def test_espeak_argv(self) -> None:
        fake_exec, captured = _writing_exec("-w")
        with (
            patch(
                "kiro_crew.voice_reply.resolve_system_tts",
                return_value=(SYSTEM_ENGINE_ESPEAK, "/usr/bin/espeak-ng"),
            ),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            _passthrough_wrap(),
        ):
            result = await _synthesize_system("hallo", voice="de", rate="100%")
        assert result is not None
        os.unlink(result)
        cmd = captured["cmd"]
        assert cmd[0] == "/usr/bin/espeak-ng"
        assert cmd[cmd.index("-s") + 1] == "175"
        assert cmd[cmd.index("-v") + 1] == "de"
        assert "hallo" not in cmd

    @pytest.mark.asyncio
    async def test_sapi_writes_text_to_a_file_and_removes_it(self, monkeypatch) -> None:
        allocated = _capture_mkstemp(monkeypatch)
        text_seen: list[str] = []

        async def fake_exec(*cmd, **kwargs):
            payload = cmd[cmd.index("-EncodedCommand") + 1]
            script = base64.b64decode(payload).decode("utf-16-le")
            # Recover the input path the script would read, and the output path.
            in_path = [p for p in allocated if p.endswith(".txt")][0]
            out_path = [p for p in allocated if p.endswith(".wav")][0]
            assert in_path in script and out_path in script
            with open(in_path, encoding="utf-8") as fh:
                text_seen.append(fh.read())
            with open(out_path, "wb") as fh:
                fh.write(b"RIFF" + b"x" * 200)
            return _mock_subprocess(returncode=0)

        with (
            patch(
                "kiro_crew.voice_reply.resolve_system_tts",
                return_value=(SYSTEM_ENGINE_SAPI, "powershell.exe"),
            ),
            patch("kiro_crew.voice_reply.sandboxed_spawn_argv_async", side_effect=_prepared()),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            result = await _synthesize_system("你好 world", rate="100%")
        assert result is not None
        os.unlink(result)
        assert text_seen == ["你好 world"]
        # The text file is scratch: leaving it behind would persist a decoded
        # copy of the reply in the temp dir.
        assert not any(os.path.exists(p) for p in allocated if p.endswith(".txt"))

    @pytest.mark.asyncio
    async def test_every_engine_is_wrapped_and_only_sapi_claims_first_party(self) -> None:
        """No engine skips the wrap; the carve-out is per-engine, not per-platform.

        Every engine parses text it did not author, so confinement is the
        default. The Windows branch alone claims ``first_party_fixed_argv``,
        which is what keeps a backend-less host from fail-closing into having no
        built-in voice — and it still runs env-scrubbed, warned and audited.
        ``say``/``espeak-ng`` carry the configured voice on argv, so they must
        NOT claim it.
        """
        seen: list[bool] = []

        async def identity(cmd, *_a, **kw):
            seen.append(bool(kw.get("first_party_fixed_argv")))
            return list(cmd), {}, None

        for engine, binp, flag in (
            (SYSTEM_ENGINE_SAY, "/usr/bin/say", "-o"),
            (SYSTEM_ENGINE_ESPEAK, "/usr/bin/espeak-ng", "-w"),
        ):
            seen.clear()
            fake_exec, _captured = _writing_exec(flag)
            with (
                patch("kiro_crew.voice_reply.resolve_system_tts", return_value=(engine, binp)),
                patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
                patch("kiro_crew.voice_reply.sandboxed_spawn_argv_async", side_effect=identity),
            ):
                result = await _synthesize_system("hello", voice="Zira")
            assert result is not None, f"{engine} produced no audio"
            os.unlink(result)
            assert seen == [False], f"{engine} must not claim the carve-out: {seen}"

    @pytest.mark.asyncio
    async def test_spawn_uses_the_scrubbed_env_not_the_gateway_s(self) -> None:
        """The prepared env must reach the child, or the scrub buys nothing.

        ``sandboxed_spawn_argv`` returns the sandbox wrap AND a scrubbed env as
        two separate values; passing the argv while dropping the env leaves the
        child holding the gateway's credentials. That is the whole protection on
        a host with no sandbox backend, where the wrap itself is inert, so this
        pins the handoff rather than trusting the call site to look right.
        """
        scrubbed = {"PATH": "/usr/bin", "HOME": "/home/x"}

        async def prepare(cmd, *_a, **_kw):
            return list(cmd), dict(scrubbed), None

        fake_exec, captured = _writing_exec("-o")
        with (
            patch(
                "kiro_crew.voice_reply.resolve_system_tts",
                return_value=(SYSTEM_ENGINE_SAY, "/usr/bin/say"),
            ),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch("kiro_crew.voice_reply.sandboxed_spawn_argv_async", side_effect=prepare),
        ):
            result = await _synthesize_system("hello", voice="Alex")
        assert result is not None
        os.unlink(result)
        assert (
            captured["kwargs"].get("env") == scrubbed
        ), "the child must run under the prepared env, not the inherited one"

    @pytest.mark.asyncio
    async def test_every_filesystem_step_runs_off_the_event_loop(self, monkeypatch) -> None:
        """No stat on the synthesis path may block the loop.

        Three of them sit here and each stats a different tree: the sandbox probe
        walks PATH, the cgroup wrap ensures the parent slice's limits, and the
        output check stats TMPDIR — which an operator can point at a network or
        FUSE mount. A stalled stat in any of them freezes every session and
        heartbeat this loop serves. Thread identity is asserted because it is the
        part a reader of the call site cannot otherwise tell from an inline call.
        """
        loop_thread = threading.get_ident()
        threads: dict[str, int] = {}

        def record_cgroup(argv: list[str]) -> list[str]:
            threads["cgroup"] = threading.get_ident()
            return list(argv)

        def record_output(path: str) -> bool:
            threads["output"] = threading.get_ident()
            return _produced_audio(path)

        monkeypatch.setattr("kiro_crew.voice_reply.cgroup_scope_argv", record_cgroup)
        monkeypatch.setattr("kiro_crew.voice_reply._produced_audio", record_output)
        fake_exec, _captured = _writing_exec("-o")
        with (
            patch(
                "kiro_crew.voice_reply.resolve_system_tts",
                return_value=(SYSTEM_ENGINE_SAY, "/usr/bin/say"),
            ),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch("kiro_crew.voice_reply.sandboxed_spawn_argv_async", side_effect=_prepared()),
        ):
            result = await _synthesize_system("hello", voice="Alex")
        assert result is not None
        os.unlink(result)
        assert set(threads) == {"cgroup", "output"}, f"a step was never reached: {threads}"
        for step, tid in threads.items():
            assert tid != loop_thread, f"{step} ran on the event-loop thread"

    @pytest.mark.asyncio
    async def test_sapi_claims_the_first_party_carve_out(self, monkeypatch) -> None:
        """Windows alone claims it, so a backend-less host keeps a voice.

        Its argv after the two spills is a System32 binary plus module constants
        and internally-derived temp paths, which is the property the carve-out
        requires; it still runs env-scrubbed, warned and SEL-audited, and a
        governance sandbox floor still refuses it.
        """
        allocated = _capture_mkstemp(monkeypatch)
        seen: list[bool] = []
        argvs: list[list[str]] = []

        async def identity(cmd, *_a, **kw):
            seen.append(bool(kw.get("first_party_fixed_argv")))
            return list(cmd), {}, None

        async def fake_exec(*cmd, **_kw):
            argvs.append(list(cmd))
            out = [p for p in allocated if p.endswith(".wav")][0]
            with open(out, "wb") as fh:
                fh.write(b"RIFF" + b"x" * 200)
            return _mock_subprocess(returncode=0)

        with (
            patch(
                "kiro_crew.voice_reply.resolve_system_tts",
                return_value=(SYSTEM_ENGINE_SAPI, "powershell.exe"),
            ),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch("kiro_crew.voice_reply.sandboxed_spawn_argv_async", side_effect=identity),
        ):
            result = await _synthesize_system("hello", voice="Zira")
        assert result is not None
        os.unlink(result)
        assert seen == [True]

        # The claim is about the REAL argv, so assert it there rather than on the
        # script builder in isolation: neither the configured voice nor the reply
        # text may appear anywhere in it, including inside the base64 payload.
        assert argvs, "the engine was never spawned"
        argv = argvs[0]
        payload = argv[argv.index("-EncodedCommand") + 1]
        script = base64.b64decode(payload).decode("utf-16-le")
        for secret in ("Zira", "hello"):
            assert secret not in script, f"{secret!r} reached the argv payload"
            assert not any(secret in a for a in argv), f"{secret!r} reached argv"
        # Both spills are scratch, so neither survives the call.
        assert not any(os.path.exists(p) for p in allocated if p.endswith(".txt"))

    @pytest.mark.asyncio
    async def test_nonzero_exit_discards_the_temp_file(self, monkeypatch) -> None:
        allocated = _capture_mkstemp(monkeypatch)

        async def fake_exec(*cmd, **kwargs):
            return _mock_subprocess(returncode=1, stderr=b"boom")

        with (
            patch(
                "kiro_crew.voice_reply.resolve_system_tts",
                return_value=(SYSTEM_ENGINE_SAY, "/usr/bin/say"),
            ),
            patch("kiro_crew.voice_reply.sandboxed_spawn_argv_async", side_effect=_prepared()),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            assert await _synthesize_system("hello") is None
        assert allocated and not any(os.path.exists(p) for p in allocated)

    @pytest.mark.asyncio
    async def test_undersized_output_discards_the_temp_file(self, monkeypatch) -> None:
        allocated = _capture_mkstemp(monkeypatch)
        fake_exec, _captured = _writing_exec("-o", size=1)

        with (
            patch(
                "kiro_crew.voice_reply.resolve_system_tts",
                return_value=(SYSTEM_ENGINE_SAY, "/usr/bin/say"),
            ),
            patch("kiro_crew.voice_reply.sandboxed_spawn_argv_async", side_effect=_prepared()),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            assert await _synthesize_system("hello") is None
        assert allocated and not any(os.path.exists(p) for p in allocated)

    @pytest.mark.asyncio
    async def test_timeout_kills_and_discards(self, monkeypatch) -> None:
        """A timed-out child is killed and its temp file discarded.

        The prepare seam is patched because this asserts the TIMEOUT path: on a
        host whose sandbox refuses (a CI container with no usable backend) the
        synthesis returns None before it ever spawns, which satisfies the
        ``is None`` assertion for the wrong reason and leaves ``kill`` uncalled.
        """
        allocated = _capture_mkstemp(monkeypatch)
        proc = _mock_subprocess(returncode=0)
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)

        async def fake_exec(*cmd, **kwargs):
            return proc

        with (
            patch(
                "kiro_crew.voice_reply.resolve_system_tts",
                return_value=(SYSTEM_ENGINE_SAY, "/usr/bin/say"),
            ),
            patch("kiro_crew.voice_reply.sandboxed_spawn_argv_async", side_effect=_prepared()),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            assert await _synthesize_system("hello") is None
        proc.kill.assert_called_once()
        assert allocated and not any(os.path.exists(p) for p in allocated)


# ── _run_tts_subprocess() through the system engine ───────────────────────


class TestSystemSubprocessOwnership:
    """The shared spawn helper's exits, driven through the system engine.

    Temp ownership, kill-and-reap on cancellation and the sandbox refusal live in
    ``_run_tts_subprocess``, which every WAV provider goes through.
    """

    @pytest.mark.asyncio
    async def test_timeout_survives_process_lookup_error(self) -> None:
        """If proc.kill() raises ProcessLookupError, synthesize still returns None cleanly."""
        import asyncio as _asyncio

        proc = _mock_subprocess(returncode=0)
        proc.kill.side_effect = ProcessLookupError

        async def hang_wait_for(coro, timeout=None):
            coro.close()
            raise _asyncio.TimeoutError()

        with (
            patch(
                "kiro_crew.voice_reply.resolve_system_tts",
                return_value=(SYSTEM_ENGINE_ESPEAK, "/usr/bin/espeak-ng"),
            ),
            patch(
                "kiro_crew.voice_reply.sandboxed_spawn_argv_async",
                side_effect=_prepared(),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                return_value=proc,
            ),
            patch("asyncio.wait_for", side_effect=hang_wait_for),
        ):
            assert await _synthesize_system("hello") is None

    @pytest.mark.asyncio
    async def test_cancellation_kills_child_and_removes_owned_temp(
        self, tmp_path, monkeypatch
    ) -> None:
        # CancelledError is a BaseException: it bypasses ``except Exception``,
        # so only the finally-based invariant discards the owned temp file —
        # and the cancellation must still propagate to the caller.

        allocated = _capture_mkstemp(monkeypatch)
        proc = _mock_subprocess(returncode=0)
        proc.communicate = AsyncMock(side_effect=asyncio.CancelledError)

        async def fake_exec(*cmd, **kwargs):
            return proc

        with (
            patch(
                "kiro_crew.voice_reply.resolve_system_tts",
                return_value=(SYSTEM_ENGINE_ESPEAK, "/usr/bin/espeak-ng"),
            ),
            patch(
                "kiro_crew.voice_reply.sandboxed_spawn_argv_async",
                side_effect=_prepared(),
            ),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            with pytest.raises(asyncio.CancelledError):
                await _synthesize_system("hello")

        # wait_for cancels communicate() but does not terminate the child;
        # the child must be killed and reaped, and the owned temp discarded.
        # The reap goes through communicate(), not wait(): wait_for already
        # cancelled the pipe readers, so wait() on a full-PIPE child hangs.
        proc.kill.assert_called_once()
        assert proc.communicate.await_count >= 2
        proc.wait.assert_not_awaited()
        assert len(allocated) == 1
        assert not os.path.exists(allocated[0])

    @pytest.mark.asyncio
    async def test_prespawn_cancellation_removes_owned_temp(self, tmp_path, monkeypatch) -> None:
        # A cancellation delivered BEFORE the child exists (here: from the
        # subprocess spawn itself) bypasses the kill/reap branch entirely —
        # only the ``finally`` invariant discards the owned temp file.

        allocated = _capture_mkstemp(monkeypatch)

        async def cancelled_exec(*cmd, **kwargs):
            raise asyncio.CancelledError

        with (
            patch(
                "kiro_crew.voice_reply.resolve_system_tts",
                return_value=(SYSTEM_ENGINE_ESPEAK, "/usr/bin/espeak-ng"),
            ),
            patch(
                "kiro_crew.voice_reply.sandboxed_spawn_argv_async",
                side_effect=_prepared(),
            ),
            patch("asyncio.create_subprocess_exec", side_effect=cancelled_exec),
        ):
            with pytest.raises(asyncio.CancelledError):
                await _synthesize_system("hello")

        assert len(allocated) == 1
        assert not os.path.exists(allocated[0])

    @pytest.mark.asyncio
    async def test_exec_exception_returns_none(self) -> None:

        with (
            patch(
                "kiro_crew.voice_reply.resolve_system_tts",
                return_value=(SYSTEM_ENGINE_ESPEAK, "/usr/bin/espeak-ng"),
            ),
            patch(
                "kiro_crew.voice_reply.sandboxed_spawn_argv_async",
                side_effect=_prepared(),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=OSError("boom"),
            ),
        ):
            assert await _synthesize_system("hello") is None

    @pytest.mark.asyncio
    async def test_sandbox_unavailable_propagates_and_unlinks(
        self,
        tmp_path,
        monkeypatch,
        caplog,
    ) -> None:
        """A fail-closed sandbox is reported with its remedy, not as a generic error.

        Mirrors the Polly counterpart: no OS sandbox backend (every Windows host,
        Linux without user namespaces) makes wrap_argv raise, and synthesis must
        propagate, unlink the temp WAV, and relay the sandbox layer's own
        remedy prose rather than logging a stack trace that reads as an
        engine fault.
        """
        from kiro_crew.sandbox import SandboxUnavailableError

        created: list[str] = []
        real_mkstemp = tempfile.mkstemp

        def tracking_mkstemp(*a, **k):
            fd, p = real_mkstemp(*a, **k)
            created.append(p)
            return fd, p

        monkeypatch.setattr("kiro_crew.voice_reply.tempfile.mkstemp", tracking_mkstemp)

        async def refuse(cmd, *_a, **_kw):
            raise SandboxUnavailableError(_SANDBOX_REMEDY, "no_backend", "not Linux")

        monkeypatch.setattr("kiro_crew.voice_reply.sandboxed_spawn_argv_async", refuse)
        monkeypatch.setattr(
            "kiro_crew.voice_reply.resolve_system_tts",
            lambda: (SYSTEM_ENGINE_ESPEAK, "/usr/bin/espeak-ng"),
        )

        with caplog.at_level("ERROR", logger="kiro_crew.voice_reply"):
            with pytest.raises(SandboxUnavailableError):
                await _synthesize_system("hello")

        assert created, "synthesis should have allocated a temp wav"
        assert not os.path.exists(created[0]), "temp wav must be unlinked"
        assert _SANDBOX_REMEDY in caplog.text
        assert "no_backend" in caplog.text
        assert "synthesis error" not in caplog.text

    @pytest.mark.asyncio
    async def test_sandbox_cleanup_unlinked(self, tmp_path) -> None:
        """If wrap_argv returns a cleanup path, it must be unlinked after exit."""
        cleanup_path = tmp_path / "sandbox-profile"
        cleanup_path.write_text("profile")

        proc = _mock_subprocess(returncode=0)

        async def fake_exec(*cmd, **kwargs):
            out_path = cmd[cmd.index("-w") + 1]
            with open(out_path, "wb") as f:
                f.write(b"x" * 200)
            return proc

        with (
            patch(
                "kiro_crew.voice_reply.resolve_system_tts",
                return_value=(SYSTEM_ENGINE_ESPEAK, "/usr/bin/espeak-ng"),
            ),
            patch(
                "kiro_crew.voice_reply.sandboxed_spawn_argv_async",
                side_effect=_prepared(cleanup=str(cleanup_path)),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=fake_exec,
            ),
        ):
            result = await _synthesize_system("hello")

        assert result is not None
        os.unlink(result)
        assert not cleanup_path.exists(), "sandbox cleanup file should be removed"


# ── _synthesize_polly() ──────────────────────────────────────────────────


class TestSynthesizePolly:
    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _passthrough_sandbox(self, monkeypatch, _polly_consented):
        # _synthesize_polly() calls wrap_argv before create_subprocess_exec.
        # wrap_argv fail-closes on any host with no OS sandbox backend (macOS 26,
        # every Windows host), which is caught and returns None. Patch to
        # passthrough so the existing create_subprocess_exec mocks run.
        monkeypatch.setattr("kiro_crew.voice_reply.wrap_argv", lambda argv, **k: (list(argv), None))
        # cgroup_scope_argv is neutralized module-wide by _no_cgroup_scope.
        _patch_aws_on_path(monkeypatch)

    @pytest.mark.asyncio
    async def test_invalid_engine_falls_back_to_default(self, tmp_path) -> None:
        proc = _mock_subprocess(returncode=0)

        captured_cmd: list[str] = []

        async def fake_exec(*cmd, **kwargs):
            captured_cmd.extend(cmd)
            # Write fake MP3 to the final positional path arg.
            with open(cmd[-1], "wb") as f:
                f.write(b"x" * 200)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await _synthesize_polly(
                "<speak>hi</speak>",
                engine="invalid-engine",
            )
        assert result is not None
        os.unlink(result)
        # Engine defaults to 'generative' (DEFAULT_ENGINE) on invalid input.
        assert "generative" in captured_cmd

    @pytest.mark.asyncio
    async def test_profile_and_region_passed_through(self, tmp_path) -> None:
        # The class fixture consents for the DEFAULT profile+region, and a grant
        # is keyed on both -- so this case has to consent for the pair it
        # actually uses. That is the gate working: consent for one account does
        # not silently transfer to another profile or region.
        from kiro_crew import aws_consent

        aws_consent.record_grant(
            aws_consent.SERVICE_POLLY,
            profile="my-profile",
            region="us-east-2",
            account="111122223333",
            arn="arn:aws:iam::111122223333:user/test",
            granted_at="2026-08-21T00:00:00+00:00",
        )
        proc = _mock_subprocess(returncode=0)

        captured: list[str] = []

        async def fake_exec(*cmd, **kwargs):
            captured.extend(cmd)
            with open(cmd[-1], "wb") as f:
                f.write(b"x" * 200)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await _synthesize_polly(
                "<speak>hi</speak>",
                aws_profile="my-profile",
                region="us-east-2",
            )
        assert result is not None
        os.unlink(result)
        assert "--profile" in captured and "my-profile" in captured
        assert "--region" in captured and "us-east-2" in captured

    @pytest.mark.asyncio
    async def test_nonzero_rc_returns_none(self) -> None:
        proc = _mock_subprocess(returncode=1, stderr=b"denied")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            assert await _synthesize_polly("<speak>hi</speak>") is None

    @pytest.mark.asyncio
    async def test_output_too_small_returns_none(self) -> None:
        proc = _mock_subprocess(returncode=0)

        async def fake_exec(*cmd, **kwargs):
            with open(cmd[-1], "wb") as f:
                f.write(b"tiny")
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            assert await _synthesize_polly("<speak>hi</speak>") is None

    @pytest.mark.asyncio
    async def test_exception_returns_none(self) -> None:
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=OSError("no aws"),
        ):
            assert await _synthesize_polly("<speak>hi</speak>") is None

    @pytest.mark.asyncio
    async def test_applies_wrap_argv_sandbox(self, tmp_path) -> None:
        """``aws polly`` consumes LLM-derived SSML on argv -- must be sandboxed."""
        proc = _mock_subprocess(returncode=0)
        wrap_called = {"n": 0}

        def fake_wrap(cmd, mode):
            wrap_called["n"] += 1
            assert mode == "standard", "polly should use standard sandbox mode"
            return cmd, None  # no cleanup file

        async def fake_exec(*cmd, **kwargs):
            with open(cmd[-1], "wb") as f:
                f.write(b"x" * 200)
            return proc

        with (
            patch(
                "kiro_crew.voice_reply.wrap_argv",
                side_effect=fake_wrap,
            ),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            result = await _synthesize_polly("<speak>hi</speak>")
        assert result is not None
        os.unlink(result)
        assert wrap_called["n"] == 1, "wrap_argv must be invoked exactly once"

    @pytest.mark.asyncio
    async def test_timeout_kills_and_returns_none(self, tmp_path) -> None:
        """Polly timeout must kill the subprocess so it doesn't linger."""
        import asyncio as _asyncio

        proc = _mock_subprocess(returncode=0)

        async def hang_wait_for(coro, timeout=None):
            coro.close()
            raise _asyncio.TimeoutError()

        with (
            patch(
                "kiro_crew.voice_reply.wrap_argv",
                side_effect=lambda c, mode: (c, None),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                return_value=proc,
            ),
            patch("asyncio.wait_for", side_effect=hang_wait_for),
        ):
            assert await _synthesize_polly("<speak>hi</speak>") is None

        proc.kill.assert_called_once()
        # The reap goes through communicate(), not wait(): wait_for already
        # cancelled the pipe readers, so wait() on a full-PIPE child hangs.
        proc.communicate.assert_awaited()
        proc.wait.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_timeout_survives_process_lookup_error(self, tmp_path) -> None:
        """Polly timeout path tolerates ProcessLookupError on kill (already-exited child)."""
        import asyncio as _asyncio

        proc = _mock_subprocess(returncode=0)
        proc.kill.side_effect = ProcessLookupError

        async def hang_wait_for(coro, timeout=None):
            coro.close()
            raise _asyncio.TimeoutError()

        with (
            patch(
                "kiro_crew.voice_reply.wrap_argv",
                side_effect=lambda c, mode: (c, None),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                return_value=proc,
            ),
            patch("asyncio.wait_for", side_effect=hang_wait_for),
        ):
            assert await _synthesize_polly("<speak>hi</speak>") is None

    @pytest.mark.asyncio
    async def test_cancellation_kills_child_and_removes_owned_temp(
        self, tmp_path, monkeypatch
    ) -> None:
        # CancelledError is a BaseException: it bypasses ``except Exception``,
        # so only the finally-based invariant discards the owned temp file —
        # and the cancellation must still propagate to the caller.
        allocated = _capture_mkstemp(monkeypatch)
        proc = _mock_subprocess(returncode=0)
        proc.communicate = AsyncMock(side_effect=asyncio.CancelledError)

        async def fake_exec(*cmd, **kwargs):
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            with pytest.raises(asyncio.CancelledError):
                await _synthesize_polly("<speak>hi</speak>")

        # wait_for cancels communicate() but does not terminate the child;
        # the child must be killed and reaped, and the owned temp discarded.
        # The reap goes through communicate(), not wait(): wait_for already
        # cancelled the pipe readers, so wait() on a full-PIPE child hangs.
        proc.kill.assert_called_once()
        assert proc.communicate.await_count >= 2
        proc.wait.assert_not_awaited()
        assert len(allocated) == 1
        assert not os.path.exists(allocated[0])

    @pytest.mark.asyncio
    async def test_prespawn_cancellation_removes_owned_temp(self, tmp_path, monkeypatch) -> None:
        # A cancellation delivered BEFORE the child exists (here: from the
        # subprocess spawn itself) bypasses the kill/reap branch entirely —
        # only the ``finally`` invariant discards the owned temp file.
        allocated = _capture_mkstemp(monkeypatch)

        async def cancelled_exec(*cmd, **kwargs):
            raise asyncio.CancelledError

        with patch("asyncio.create_subprocess_exec", side_effect=cancelled_exec):
            with pytest.raises(asyncio.CancelledError):
                await _synthesize_polly("<speak>hi</speak>")

        assert len(allocated) == 1
        assert not os.path.exists(allocated[0])

    @pytest.mark.asyncio
    async def test_sandbox_cleanup_unlinked(self, tmp_path) -> None:
        """If wrap_argv returns a cleanup path, polly must unlink it after exit."""
        cleanup_path = tmp_path / "polly-sandbox-profile"
        cleanup_path.write_text("profile")

        proc = _mock_subprocess(returncode=0)

        async def fake_exec(*cmd, **kwargs):
            with open(cmd[-1], "wb") as f:
                f.write(b"x" * 200)
            return proc

        with (
            patch(
                "kiro_crew.voice_reply.wrap_argv",
                side_effect=lambda c, mode: (c, str(cleanup_path)),
            ),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            result = await _synthesize_polly("<speak>hi</speak>")
        assert result is not None
        os.unlink(result)
        assert not cleanup_path.exists(), "polly sandbox cleanup file should be removed"

    @pytest.mark.asyncio
    async def test_aws_cli_missing_short_circuits_before_spawn(self, monkeypatch) -> None:
        """Absent ``aws`` CLI degrades to None without attempting a spawn.

        The guard must run BEFORE create_subprocess_exec: reaching the spawn
        would raise FileNotFoundError instead of degrading gracefully.
        """
        monkeypatch.setattr("kiro_crew.voice_reply.shutil.which", lambda name, *a, **k: None)
        spawned = {"n": 0}

        async def fake_exec(*cmd, **kwargs):
            spawned["n"] += 1
            raise AssertionError("must not spawn when the aws CLI is absent")

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            assert await _synthesize_polly("<speak>hi</speak>") is None
        assert spawned["n"] == 0

    @pytest.mark.asyncio
    async def test_sandbox_unavailable_propagates_and_unlinks(
        self,
        monkeypatch,
        caplog,
    ) -> None:
        """A fail-closed sandbox is reported with its remedy, not as a generic error.

        Every Windows host (and Linux without user namespaces) has no OS sandbox
        backend, so wrap_argv raises SandboxUnavailableError. Polly must degrade to
        None, unlink the temp MP3, and relay the sandbox layer's remedy prose — the
        generic handler's "Polly synthesis error" stack trace misattributes this to
        Polly or AWS credentials.
        """
        from kiro_crew.sandbox import SandboxUnavailableError

        created: list[str] = []
        real_mkstemp = tempfile.mkstemp

        def tracking_mkstemp(*a, **k):
            fd, p = real_mkstemp(*a, **k)
            created.append(p)
            return fd, p

        monkeypatch.setattr("kiro_crew.voice_reply.tempfile.mkstemp", tracking_mkstemp)

        def refuse(cmd, mode):
            raise SandboxUnavailableError(_SANDBOX_REMEDY, "no_backend", "not Linux")

        monkeypatch.setattr("kiro_crew.voice_reply.wrap_argv", refuse)

        with caplog.at_level("ERROR", logger="kiro_crew.voice_reply"):
            with pytest.raises(SandboxUnavailableError):
                await _synthesize_polly("<speak>hi</speak>")

        assert created, "polly should have allocated a temp mp3"
        assert not os.path.exists(created[0]), "temp mp3 must be unlinked"
        assert _SANDBOX_REMEDY in caplog.text
        assert "no_backend" in caplog.text
        assert "Polly synthesis error" not in caplog.text

    @pytest.mark.asyncio
    async def test_transient_sandbox_refusal_does_not_advise_disabling(
        self,
        monkeypatch,
        caplog,
    ) -> None:
        """A ``transient`` refusal must relay retry advice, not the opt-in key.

        SandboxUnavailableError.kind is the contract: for ``"transient"`` the
        sandbox layer's own prose says retry and explicitly says callers must NOT
        advise disabling the sandbox. Hardcoding the
        ``sandbox_allow_unsandboxed_exec`` remedy in this handler would tell an
        operator to permanently drop isolation to work around momentary resource
        pressure, so the handler must relay ``str(exc)`` rather than its own copy.
        """
        from kiro_crew.sandbox import SandboxUnavailableError

        transient_prose = (
            "This probe failure looks TRANSIENT (momentary resource pressure) "
            "— it is not cached. Do NOT disable the sandbox for this; retry."
        )

        def refuse(cmd, mode):
            raise SandboxUnavailableError(transient_prose, "transient", "fork: EAGAIN")

        monkeypatch.setattr("kiro_crew.voice_reply.wrap_argv", refuse)

        with caplog.at_level("ERROR", logger="kiro_crew.voice_reply"):
            with pytest.raises(SandboxUnavailableError):
                await _synthesize_polly("<speak>hi</speak>")

        assert transient_prose in caplog.text
        assert "transient" in caplog.text
        assert "sandbox_allow_unsandboxed_exec" not in caplog.text


# ---------------------------------------------------------------------------
# _synthesize_polly temp ownership under cancellation
# ---------------------------------------------------------------------------


class _CancelOnceProc:
    """Process double: first ``communicate`` raises ``CancelledError``, the
    second (the reap) records itself and returns."""

    def __init__(self, events: list[str]):
        self._events = events
        self._calls = 0

    async def communicate(self, _input: bytes | None = None):
        self._calls += 1
        if self._calls == 1:
            raise asyncio.CancelledError()
        self._events.append("reaped")
        return b"", b""

    def kill(self):
        self._events.append("killed")


class _CancelAlwaysProc:
    """Process double: EVERY ``communicate`` raises ``CancelledError`` — the
    second raise is the repeat cancellation landing on the reap await."""

    def __init__(self, events: list[str]):
        self._events = events

    async def communicate(self, _input: bytes | None = None):
        raise asyncio.CancelledError()

    def kill(self):
        self._events.append("killed")


def _pin_mkstemp(monkeypatch, owned) -> None:
    """Pin ``tempfile.mkstemp`` to a known file so the tests can watch it."""

    def fake_mkstemp(suffix: str = ""):
        return os.open(str(owned), os.O_WRONLY | os.O_CREAT), str(owned)

    monkeypatch.setattr("kiro_crew.voice_reply.tempfile.mkstemp", fake_mkstemp)


def _track_unlink(monkeypatch, owned, events: list[str]) -> None:
    real_unlink = os.unlink

    def tracked(path, *args, **kwargs):
        if str(path) == str(owned):
            events.append("unlinked")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr("kiro_crew.voice_reply.os.unlink", tracked)


def _lock_unlink(monkeypatch, owned, events: list[str]) -> None:
    """Make the unlink of *owned* fail as if the child still held it open.

    Every OTHER call reaches the real ``os.unlink`` with its full signature.
    ``kiro_crew.voice_reply.os`` IS the stdlib module, so this patch is
    process-global, and it outlives the ``tmp_path`` teardown: the module
    autouse fixture requests ``monkeypatch`` before ``tmp_path`` exists, so
    ``monkeypatch`` is undone last. Under ``tmp_path_retention_policy =
    failed`` that teardown is a ``shutil.rmtree`` whose fd-walk calls
    ``os.unlink(leaf, dir_fd=fd)``. A wrapper that forwarded ``os.remove(path)``
    dropped ``dir_fd``, re-resolved ``owned.wav``
    against the CWD -- the checkout -- and left the tmp dir behind.
    """
    real_unlink = os.unlink

    def locked(path, *args, **kwargs):
        if str(path) == str(owned):
            events.append("unlink_attempted")
            raise PermissionError("file is locked by the child")
        # A bare relative name here would be resolved against the CWD, i.e.
        # the checkout; only fd-relative (rmtree) or absolute paths may pass.
        assert (
            os.path.isabs(os.fspath(path)) or "dir_fd" in kwargs
        ), f"relative unlink {path!r} without dir_fd would touch the CWD"
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr("kiro_crew.voice_reply.os.unlink", locked)


class TestSynthesizePollyCancelOwnership:
    """``_synthesize_polly`` owns the ``.mp3`` until every exit removes it.

    The cancellation contract: kill AND reap the AWS
    CLI child before the unlink, remove the ``.mp3``, re-raise the original
    cancellation.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _sandbox_and_consent(self, monkeypatch, _polly_consented):
        monkeypatch.setattr("kiro_crew.voice_reply.wrap_argv", lambda argv, **k: (list(argv), None))
        _patch_aws_on_path(monkeypatch)

    @staticmethod
    def _owned_mp3(tmp_path, monkeypatch):
        owned = tmp_path / "owned.mp3"
        _pin_mkstemp(monkeypatch, owned)
        return owned

    @pytest.mark.asyncio
    async def test_cancellation_reaps_the_cli_before_removing_the_mp3(self, tmp_path, monkeypatch):
        owned = self._owned_mp3(tmp_path, monkeypatch)
        events: list[str] = []
        _track_unlink(monkeypatch, owned, events)

        with patch("asyncio.create_subprocess_exec", return_value=_CancelOnceProc(events)):
            with pytest.raises(asyncio.CancelledError):
                await _synthesize_polly("<speak>hi</speak>")
        assert events == ["killed", "reaped", "unlinked"]
        assert not owned.exists()

    @pytest.mark.asyncio
    async def test_repeat_cancellation_on_the_reap_still_unlinks(self, tmp_path, monkeypatch):
        owned = self._owned_mp3(tmp_path, monkeypatch)
        events: list[str] = []
        _track_unlink(monkeypatch, owned, events)

        with patch("asyncio.create_subprocess_exec", return_value=_CancelAlwaysProc(events)):
            with pytest.raises(asyncio.CancelledError):
                await _synthesize_polly("<speak>hi</speak>")
        assert events == ["killed", "unlinked"]
        assert not owned.exists()

    @pytest.mark.asyncio
    async def test_locked_mp3_does_not_replace_the_cancellation(self, tmp_path, monkeypatch):
        owned = self._owned_mp3(tmp_path, monkeypatch)
        events: list[str] = []
        _lock_unlink(monkeypatch, owned, events)

        with patch("asyncio.create_subprocess_exec", return_value=_CancelOnceProc(events)):
            with pytest.raises(asyncio.CancelledError):
                await _synthesize_polly("<speak>hi</speak>")
        assert events == ["killed", "reaped", "unlink_attempted"]
        # The locked unlink never removed the file — the guarantee under test
        # is exception identity, not removal.
        assert owned.exists()

    @pytest.mark.asyncio
    async def test_cancellation_during_spawn_still_removes_the_mp3(self, tmp_path, monkeypatch):
        owned = self._owned_mp3(tmp_path, monkeypatch)

        with patch("asyncio.create_subprocess_exec", side_effect=asyncio.CancelledError()):
            with pytest.raises(asyncio.CancelledError):
                await _synthesize_polly("<speak>hi</speak>")
        assert not owned.exists()
