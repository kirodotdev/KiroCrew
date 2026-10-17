"""Instances token mint: ttl parsing, command build, remote run, no token in logs."""

from __future__ import annotations

import asyncio
import logging
import re

import pytest

from kiro_crew import platform_compat


class TestTokenMint:
    def test_parse(self):
        from kiro_crew.instances.token_mint import parse_token_from_stdout

        assert parse_token_from_stdout("http://localhost:7777?token=eyJa.b\n") == "eyJa.b"
        assert parse_token_from_stdout("https://h/?x=1&token=TOK&y=2") == "TOK"
        assert parse_token_from_stdout("nothing here") == ""

    @pytest.mark.parametrize("bad", ["abc", "20", "0h", "-1h", "20s", "99999h"])
    def test_ttl_rejects(self, bad):
        from kiro_crew.instances.token_mint import TokenMintError, _validate_ttl

        with pytest.raises(TokenMintError):
            _validate_ttl(bad)

    def test_command_builders(self):
        from kiro_crew.instances.token_mint import build_remote_token_command

        # empty remote_bin -> candidate-ladder path (build_remote_command -> build_candidate_command)
        assert 'exec "$b" token --ttl 20h;' in build_remote_token_command("", ttl="20h")
        custom = build_remote_token_command("~/bin/kirocrew", ttl="30m")
        assert '"$HOME/bin/kirocrew" token --ttl 30m' in custom
        default = build_remote_token_command("", ttl=None)
        assert 'exec "$b" token;' in default and "--ttl" not in default
        # port is threaded through so the remote mint targets the right gateway
        # (not the default 7777) — essential for instances on a custom port.
        with_port = build_remote_token_command("~/bin/kirocrew", ttl="20h", port=7879)
        assert '"$HOME/bin/kirocrew" token --ttl 20h --port 7879' in with_port
        # invalid port is rejected (kept out of the shell command unvalidated)
        from kiro_crew.instances.token_mint import TokenMintError

        with pytest.raises(TokenMintError):
            build_remote_token_command("", ttl="20h", port=99999)

    def test_token_command_prefers_run_marker_for_port(self):
        from kiro_crew.config.paths import CONFIG_DIR_NAME, LEGACY_CONFIG_DIR_NAME
        from kiro_crew.instances.token_mint import (
            build_candidate_command,
            build_remote_token_command,
        )

        # empty remote_bin + port -> run-marker clause runs BEFORE the candidate
        # ladder, keyed by the same port, and execs the recorded launcher. The
        # marker is probed under each candidate data home (KIROCREW_HOME override,
        # the current default, then the legacy home) so a migrated remote whose
        # non-interactive SSH shell doesn't export KIROCREW_HOME still hits the
        # marker written under the new default home. The default/legacy home
        # segments are asserted via the SHARED config.paths constants (not
        # re-hardcoded literals) so that re-hardcoding — the read/write desync
        # this fix closes — fails this test loudly at PR time.
        default_marker = f'"$HOME/{CONFIG_DIR_NAME}/run/gateway-7879.bin"'
        legacy_marker = f'"$HOME/{LEGACY_CONFIG_DIR_NAME}/run/gateway-7879.bin"'
        cmd = build_remote_token_command("", ttl="20h", port=7879)
        assert '"${KIROCREW_HOME:+$KIROCREW_HOME/run/gateway-7879.bin}"' in cmd
        assert default_marker in cmd
        assert legacy_marker in cmd
        # new default home is probed before the legacy home
        assert cmd.index(default_marker) < cmd.index(legacy_marker)
        assert 'exec "$__kb" token --ttl 20h --port 7879;' in cmd
        assert cmd.index("for __mk in ") < cmd.index("for b in ")  # marker tried first
        # it still falls through to the candidate ladder (older remotes/no marker)
        assert 'exec "$b" token --ttl 20h --port 7879;' in cmd

        # no port -> no marker clause (can't key it); pure candidate ladder.
        # NOTE: guard on the sentinels the generator actually emits — the marker
        # prelude is a "for __mk in ...done" loop over "…/gateway-<port>.bin"
        # paths — NOT the retired "__mk=" token (which would make these vacuous).
        no_port = build_remote_token_command("", ttl="20h")
        assert "for __mk in" not in no_port and "gateway-" not in no_port

        # explicit custom remote_bin is never overridden by the marker.
        custom = build_remote_token_command("~/bin/kirocrew", ttl="20h", port=7879)
        assert "for __mk in" not in custom and "gateway-" not in custom
        assert '"$HOME/bin/kirocrew" token --ttl 20h --port 7879' in custom

        # generic candidate builder: marker only when a port is supplied.
        assert "for __mk in" not in build_candidate_command("restart")
        assert "gateway-" not in build_candidate_command("restart")
        assert "gateway-7880.bin" in build_candidate_command("token", marker_port=7880)

        # the sibling remote-exec path (restart, via run_remote_kirocrew) also
        # prefers the marker when the caller passes the remote port.
        from kiro_crew.instances.token_mint import build_remote_command

        restart = build_remote_command("", "restart", marker_port=7781)
        assert "gateway-7781.bin" in restart and 'exec "$__kb" restart;' in restart
        # ...unless an explicit custom remote_bin is set (never overridden).
        pinned_restart = build_remote_command("~/bin/kirocrew", "restart", marker_port=7781)
        assert "for __mk in" not in pinned_restart and "gateway-" not in pinned_restart

    def test_ssh_argv_shape(self):
        from kiro_crew.instances.token_mint import _build_ssh_argv

        argv = _build_ssh_argv("cd-1", "echo hi")
        assert argv[0] == "ssh" and argv[-2] == "cd-1"
        assert "BatchMode=yes" in argv and "AddressFamily=inet" in argv
        # -n redirects ssh's stdin from the null device. Without it the child
        # inherits the gateway's stdin, and through a ProxyCommand that channel
        # stays open after the remote command exits, so ssh waits for an EOF a
        # console-less gateway never sends and the probe reports a reachable
        # host as unreachable. It must precede the host, or ssh reads it as
        # part of the remote command.
        assert "-n" in argv
        assert argv.index("-n") < argv.index("cd-1")
        # Default fail-fast connect bound is preserved for callers that
        # don't thread a budget (e.g. run_remote_kirocrew).
        assert "ConnectTimeout=10" in argv
        # The mint threads its configurable budget into ConnectTimeout so a
        # slow ProxyCommand/banner exchange isn't killed at the 10s default
        # before mint_timeout_secs can matter (OpenSSH >= 8.6 counts the
        # banner/KEX exchange against ConnectTimeout).
        argv = _build_ssh_argv("cd-1", "echo hi", connect_timeout_secs=60.0)
        assert "ConnectTimeout=60" in argv and "ConnectTimeout=10" not in argv
        # Sub-second values clamp up to ssh's integer floor of 1.
        argv = _build_ssh_argv("cd-1", "echo hi", connect_timeout_secs=0.2)
        assert "ConnectTimeout=1" in argv

    def test_mint_success_and_no_token_in_logs(self, monkeypatch, caplog):
        from kiro_crew.instances import token_mint as tm

        class FakeProc:
            returncode = 0

            async def communicate(self):
                return b"http://localhost:7777?token=SECRETJWT\n", b""

        async def fake_exec(*a, **k):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        with caplog.at_level(logging.INFO):
            tok = asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        assert tok == "SECRETJWT"
        assert "SECRETJWT" not in caplog.text

    def test_mint_nonzero_exit_raises(self, monkeypatch):
        from kiro_crew.instances import token_mint as tm

        class FakeProc:
            returncode = 127

            async def communicate(self):
                return b"", b"kirocrew binary not found"

        async def fake_exec(*a, **k):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        with pytest.raises(tm.TokenMintError):
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))

    # ── diagnosability: an older remote prints its reason to STDOUT ──────────

    def _fake_proc(self, monkeypatch, rc: int, out: bytes, err: bytes) -> None:
        class FakeProc:
            returncode = rc

            async def communicate(self):
                return out, err

        async def fake_exec(*a, **k):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    def test_nonzero_exit_surfaces_stdout_tail_when_stderr_empty(self, monkeypatch):
        """The real reason travels with the error instead of '<no stderr>'.

        Pre-fix ``kirocrew token`` printed its failure prose to stdout, so a
        stderr-only message degraded to a useless ``<no stderr>`` and someone
        had to SSH in to find out why.
        """
        from kiro_crew.instances import token_mint as tm

        self._fake_proc(
            monkeypatch,
            1,
            b"\xe2\x9d\x8c Could not reach gateway on port 5476: <urlopen error refused>\n",
            b"",
        )
        with pytest.raises(tm.TokenMintError) as excinfo:
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        msg = str(excinfo.value)
        assert "Could not reach gateway on port 5476" in msg
        assert "stdout tail:" in msg
        assert "<no stderr>" in msg  # stderr genuinely was empty — still reported

    def test_unparseable_output_surfaces_stdout_tail(self, monkeypatch):
        from kiro_crew.instances import token_mint as tm

        self._fake_proc(monkeypatch, 0, b"Gateway returned empty token\n", b"")
        with pytest.raises(tm.TokenMintError) as excinfo:
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        msg = str(excinfo.value)
        assert "could not parse a token" in msg
        assert "Gateway returned empty token" in msg

    def test_stdout_tail_never_leaks_a_token(self, monkeypatch):
        """A URL-borne or bare token on stdout is scrubbed before it reaches the error.

        The tail is only built on failure paths, but a partially-successful
        remote (URL printed, then non-zero exit) can still put a live credential
        on stdout — so the token substitution must happen unconditionally.

        The bare-token case uses the shape this app ACTUALLY mints:
        ``generate_token`` returns ``base64url(payload).base64url(signature)`` —
        two segments, not the three of a classic JWT. A fabricated three-segment
        token here would let a two-segment-blind pattern pass while leaving real
        tokens unscrubbed, so the segment count is asserted explicitly and
        ``test_bare_token_scrubbed_at_every_segment_count`` pins the full range.
        """
        from kiro_crew.instances import token_mint as tm

        minted = "eyJzdWIiOiJvd25lciIsImV4cCI6MTIzfQ.c2lnbmF0dXJlLWJ5dGVz"
        assert minted.count(".") == 1

        self._fake_proc(
            monkeypatch,
            1,
            f"http://localhost:5476?token={minted}\nbare {minted} too\nboom\n".encode(),
            b"",
        )
        with pytest.raises(tm.TokenMintError) as excinfo:
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        msg = str(excinfo.value)
        assert minted not in msg
        # no fragment of the credential survives either — a pattern that matched
        # only part of the token would leave the remaining segment(s) behind.
        for segment in minted.split("."):
            assert segment not in msg
        assert "boom" in msg

    @pytest.mark.parametrize("segments", [2, 3, 5])
    def test_bare_token_scrubbed_at_every_segment_count(self, monkeypatch, segments):
        """Two-segment (minted), three-segment (JWT) and five-segment (JWE) all scrub.

        Five segments is the compact-JWE shape: a pattern capped lower would
        match a prefix and leave ``.ciphertext.tag`` in the surfaced error.
        """
        from kiro_crew.instances import token_mint as tm

        bare = "eyJhbGciOiJIUzI1NiJ9" + "".join(f".seg{i}" for i in range(segments - 1))
        self._fake_proc(monkeypatch, 1, f"{bare}\nwhy it died\n".encode(), b"")
        with pytest.raises(tm.TokenMintError) as excinfo:
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        msg = str(excinfo.value)
        assert bare not in msg
        assert f"seg{segments - 2}" not in msg  # last segment gone, not just a prefix
        assert "why it died" in msg

    def test_stdout_tail_is_bounded_and_absent_when_stdout_empty(self, monkeypatch):
        from kiro_crew.instances import token_mint as tm

        # bounded: only the TAIL is carried (the reason is printed last). The
        # reason sits on its own line, as a real remote prints it — the scan
        # window's left edge falls inside the preceding noise run, which is
        # dropped as potentially-clipped without touching the reason.
        long_out = ("x" * 5000 + "\nREAL-REASON\n").encode()
        self._fake_proc(monkeypatch, 1, long_out, b"")
        with pytest.raises(tm.TokenMintError) as excinfo:
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        msg = str(excinfo.value)
        assert "REAL-REASON" in msg
        assert len(msg) < 600

        # modern remote (reason on stderr, nothing on stdout) keeps the original
        # single-stream message shape — no empty "stdout tail:" noise.
        self._fake_proc(monkeypatch, 127, b"", b"kirocrew binary not found")
        with pytest.raises(tm.TokenMintError) as excinfo:
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        assert "stdout tail:" not in str(excinfo.value)
        assert "kirocrew binary not found" in str(excinfo.value)

    def test_success_path_never_builds_the_stdout_tail(self, monkeypatch):
        """The tail is built inside the failure branches only.

        On success, stdout holds a live token; running the scrub over it would be
        pointless work on credential-bearing text and would contradict the
        "only ever built on a failure path" invariant the helper documents. This
        locks the invariant to control flow instead of a comment.
        """
        from kiro_crew.instances import token_mint as tm

        calls: list[str] = []
        monkeypatch.setattr(
            tm, "_redacted_output_tail", lambda out, *a, **k: calls.append(out) or ""
        )

        self._fake_proc(monkeypatch, 0, b"http://localhost:5476?token=eyJa.b\n", b"")
        assert asyncio.run(tm.mint_remote_token("cd-1", ttl="20h")) == "eyJa.b"
        assert calls == []

        # ...but a failing mint still builds it.
        self._fake_proc(monkeypatch, 1, b"reason on stdout\n", b"")
        with pytest.raises(tm.TokenMintError):
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        assert len(calls) == 1

    def test_scrubbers_never_scan_more_than_the_bounded_window(self, monkeypatch):
        """The scrub cost must not scale with the remote's stdout size.

        `re` does not release the GIL, so scrubbing an unbounded payload blocks
        the gateway's event loop in proportion to its length — measured ~1s per
        MB (13s on 13MB), and an executor hop is no cure (~1.1s stall on the
        same payload, GIL-bound). Asserting on the *input length* handed to the
        redactors instead of on wall-clock keeps this deterministic.
        """
        from kiro_crew.instances import token_mint as tm

        seen: list[int] = []
        real = tm.redact
        monkeypatch.setattr(tm, "redact", lambda text: seen.append(len(text)) or real(text))

        huge = ("x" * 60 + " could not reach gateway\n") * 20_000  # ~1.7 MB
        self._fake_proc(monkeypatch, 1, huge.encode(), b"")
        with pytest.raises(tm.TokenMintError) as excinfo:
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))

        assert seen and max(seen) <= tm._OUTPUT_SCAN_CHARS
        # The bound must not cost diagnosability: the reason still travels.
        assert "could not reach gateway" in str(excinfo.value)

    def test_secret_clipped_by_the_window_boundary_is_never_shown(self, monkeypatch):
        """A token straddling the window start must not leak its suffix.

        The window slice can land mid-run, which would show the token regexes a
        fragment they cannot match while its suffix still lands inside the carried
        300 chars. Truncated input therefore drops the leading run of URL /
        base64url characters. The floor for that drop is low on purpose: a
        fragment that looks too far from the end to matter is still pulled into
        the tail when the scrubbers shrink the text around it, which the third
        case below pins.
        """
        from kiro_crew.instances import token_mint as tm

        secret = "eyJ" + "A" * 3000 + ".SIGNATURE-MUST-NOT-APPEAR"
        for stdout in (f"noise\n{secret}\n", secret):  # with and without a trailing newline
            self._fake_proc(monkeypatch, 1, stdout.encode(), b"")
            with pytest.raises(tm.TokenMintError) as excinfo:
                asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
            msg = str(excinfo.value)
            assert "SIGNATURE-MUST-NOT-APPEAR" not in msg
            assert "AAAA" not in msg
            assert "<clipped>" in msg

        # A clipped fragment far from the end is NOT unreachable: the scrubbers
        # shrink the window (each blob collapses to `<redacted>`), pulling earlier
        # text into the carried tail. This is why the clipped-run floor is low
        # instead of the window-minus-tail distance — with a 2100-char floor this
        # case surfaces the raw fragment.
        blob = "eyJ" + "z" * 200 + "." + "y" * 200
        redactable = f"{blob}\n" * 5
        secret_run = "MUSTNOTAPPEAR" * 39  # one unbroken run, no whitespace
        # Size the prefix so the window's left edge lands INSIDE secret_run.
        prefix_len = tm._OUTPUT_SCAN_CHARS - len(redactable) - len(secret_run) // 2
        shrinking = "p" * prefix_len + "\n" + secret_run + "\n" + redactable
        assert len(shrinking) > tm._OUTPUT_SCAN_CHARS
        self._fake_proc(monkeypatch, 1, shrinking.encode(), b"")
        with pytest.raises(tm.TokenMintError) as excinfo:
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        assert "MUSTNOTAPPEAR" not in str(excinfo.value)

        # A long stdout still carries its reason when nothing is clipped away.
        padded = "x" * 4000 + "\n" + "short-run-word " * 20 + "\nWHY-IT-FAILED\n"
        self._fake_proc(monkeypatch, 1, padded.encode(), b"")
        with pytest.raises(tm.TokenMintError) as excinfo:
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        msg = str(excinfo.value)
        assert "WHY-IT-FAILED" in msg
        assert "short-run-word" in msg


class TestTokenMintGeneric:
    def test_ttl_to_seconds(self):
        from kiro_crew.instances.token_mint import TokenMintError, ttl_to_seconds

        assert ttl_to_seconds("20h") == 72000
        assert ttl_to_seconds("30m") == 1800
        with pytest.raises(TokenMintError):
            ttl_to_seconds("bad")

    def test_generic_builders_and_token_delegation(self):
        from kiro_crew.instances.token_mint import (
            build_candidate_command,
            build_remote_command,
            build_remote_token_command,
        )

        assert 'exec "$b" restart;' in build_candidate_command("restart")
        assert '"$HOME/bin/kirocrew" restart' in build_remote_command("~/bin/kirocrew", "restart")
        # token builder emits identical strings via the generic builders it delegates to
        assert 'exec "$b" token --ttl 20h;' in build_remote_token_command("", ttl="20h")

    def test_build_candidate_command_emits_per_candidate_diagnostics(self):
        """When no candidate is executable, the snippet must explain WHY per path.

        The exit-127 incident gave the operator only "binary not found"; the real
        state (a dangling symlink into an interrupted venv rebuild, an entry point
        that never got written) was invisible. The failure branch now diagnoses
        each candidate to stderr before exiting 127.
        """
        from kiro_crew.instances.token_mint import build_candidate_command

        cmd = build_candidate_command("token")

        # Diagnosis header, symlink handling, and the distinct .venv Python probe.
        assert 'echo "candidate diagnosis:" >&2;' in cmd
        assert "DANGLING symlink" in cmd
        assert "readlink -f" in cmd
        assert "*/.venv/bin/*)" in cmd
        assert "$__v/bin/python present" in cmd
        assert "entry-point present" not in cmd
        assert "entry-point MISSING" not in cmd
        assert "symlink -> $__t (executable)" not in cmd
        assert "present, executable" not in cmd

        # Ordering (mutation check): the diagnosis runs AFTER the not-found echo
        # and BEFORE `exit 127`, i.e. only on the failure path.
        not_found = cmd.index("kirocrew binary not found")
        diagnosis = cmd.index("candidate diagnosis:")
        exit_127 = cmd.index("exit 127")
        assert not_found < diagnosis < exit_127

    def test_candidate_ladder_covers_managed_launcher_dir(self):
        """A remote provisioned by a managed installer has ``kirocrew`` under
        ``~/.toolbox/bin`` -- the launcher dir ``env._EXTRA_PATH_DIRS`` already
        searches locally -- and in neither of the two classic locations. Every
        mint on such a remote exits 127 unless ``remote_bin`` is set by hand.
        The ladder must try that dir, AFTER the two install-script locations so
        nothing moves for a remote that has those."""
        from kiro_crew.env import _EXTRA_PATH_DIRS
        from kiro_crew.instances.token_mint import REMOTE_BIN_CANDIDATES, build_candidate_command

        assert REMOTE_BIN_CANDIDATES[:2] == (
            "$HOME/.local/bin/kirocrew",
            "$HOME/.kirocrew-app/.venv/bin/kirocrew",
        )
        assert "$HOME/.toolbox/bin/kirocrew" in REMOTE_BIN_CANDIDATES
        # The ladder names the same directory the local launcher search does, so
        # the two cannot disagree about where a managed install puts the binary.
        assert "{home}/.toolbox/bin" in _EXTRA_PATH_DIRS
        cmd = build_candidate_command("token")
        assert '"$HOME/.toolbox/bin/kirocrew"' in cmd
        # Every candidate is a $HOME-rooted literal: the snippet is double-quoted
        # shell, so a candidate with a quote or a `$(` would be a new injection
        # surface on the remote command line.
        for cand in REMOTE_BIN_CANDIDATES:
            assert re.fullmatch(r"\$HOME(/[A-Za-z0-9._-]+)+/kirocrew", cand), cand

    def test_not_found_path_names_the_remote_bin_override(self):
        """When no candidate is executable the error must say what to do, not
        just what was tried: the override field is ``remote_bin``, and the
        example is in ``~/`` form because the validator refuses ``$``. The hint
        comes AFTER the per-candidate diagnosis and BEFORE ``exit 127``, so it
        is only ever printed on the failure path."""
        from kiro_crew.instances.token_mint import REMOTE_BIN_HINT, build_candidate_command

        assert "remote_bin" in REMOTE_BIN_HINT
        assert "~/" in REMOTE_BIN_HINT
        assert "$" not in REMOTE_BIN_HINT
        assert '"' not in REMOTE_BIN_HINT  # embedded in a double-quoted echo
        cmd = build_candidate_command("token")
        assert f'echo "{REMOTE_BIN_HINT}" >&2;' in cmd
        diagnosis = cmd.index("candidate diagnosis:")
        hint = cmd.index(REMOTE_BIN_HINT)
        exit_127 = cmd.index("exit 127")
        assert diagnosis < hint < exit_127
        # The success path never prints it: the hint sits after the exec loop.
        assert cmd.index('exec "$b" token;') < hint

    def test_run_remote_kirocrew(self, monkeypatch):
        from kiro_crew.instances import token_mint as tm

        class FakeProc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        async def fake_exec(*a, **k):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        rc, err = asyncio.run(tm.run_remote_kirocrew("cd-1", "restart"))
        assert rc == 0 and err == ""

    def test_run_remote_kirocrew_honors_connect_timeout_secs(self, monkeypatch):
        """The fail-fast 10s ConnectTimeout default must not silently
        override a caller-supplied budget -- a restart on a slow-proxy host
        needs the same connect budget the mint itself gets."""
        from kiro_crew.instances import token_mint as tm

        class FakeProc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        captured = {}

        async def fake_exec(*argv, **k):
            captured["argv"] = argv
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        rc, _ = asyncio.run(tm.run_remote_kirocrew("cd-1", "restart", connect_timeout_secs=45.0))
        assert rc == 0
        assert "ConnectTimeout=45" in captured["argv"]

    def test_run_remote_kirocrew_redacts_stderr(self, monkeypatch):
        # Proxy-controlled stderr carrying a credential is redacted before return,
        # so a caller logging the tail cannot leak it.
        from kiro_crew.instances import token_mint as tm

        class FakeProc:
            returncode = 255

            async def communicate(self):
                return b"", b"WSSH error AKIAIOSFODNN7EXAMPLE banner"

        async def fake_exec(*a, **k):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        rc, err = asyncio.run(tm.run_remote_kirocrew("cd-1", "restart"))
        assert rc == 255
        assert "AKIAIOSFODNN7EXAMPLE" not in err
        assert "[REDACTED: credential]" in err

    def test_run_remote_kirocrew_redacts_urls_before_credentials(self, monkeypatch):
        """Pin the ORDER of the redaction passes, not just the redaction.

        The exfiltration-URL pass keys on the token-bearing URL shape, so
        running the credential pass first substitutes a placeholder into the
        query string and disarms it — the suspicious destination host then
        survives into the returned tail. Each pass is green in isolation, so
        only an input carrying a suspicious URL whose query string also carries
        a credential distinguishes the two orders. This test fails if the
        composition is ever reversed again.
        """
        from kiro_crew.instances import token_mint as tm

        class FakeProc:
            returncode = 255

            async def communicate(self):
                return b"", b"banner https://evil.example.com/x?token=AKIAIOSFODNN7EXAMPLE end"

        async def fake_exec(*a, **k):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        rc, err = asyncio.run(tm.run_remote_kirocrew("cd-1", "restart"))
        assert rc == 255
        # The URL pass must fire: the destination must be suppressed, not just
        # the credential inside it. Under the reversed (credentials-first)
        # order the URL survives as https://evil.example.com/x?token=[...].
        assert "https://evil.example.com" not in err
        assert "[REDACTED: suspicious URL" in err
        assert "AKIAIOSFODNN7EXAMPLE" not in err

    def test_stdout_tail_url_pass_not_disarmed_by_token_prescrub(self, monkeypatch):
        """The stdout-tail site has a second disarm path — its own
        ``_TOKEN_RE`` pre-scrub. Substituting ``token=<redacted>`` into a URL's
        query string before the exfiltration-URL pass destroys the token-bearing
        shape that pass keys on, so a suspicious destination would survive into
        the raised TokenMintError even with the composed helper in place. Pins
        that the generic redactors see the window before the token scrubs.
        """
        from kiro_crew.instances import token_mint as tm

        stdout = b"fail: see https://evil.example.com/x?token=AKIAIOSFODNN7EXAMPLE now"

        class FakeProc:
            returncode = 1

            async def communicate(self):
                return stdout, b""

        async def fake_exec(*a, **k):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        with pytest.raises(tm.TokenMintError) as excinfo:
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        msg = str(excinfo.value)
        assert "https://evil.example.com" not in msg
        assert "[REDACTED: suspicious URL" in msg
        assert "AKIAIOSFODNN7EXAMPLE" not in msg

    class _HangProc:
        """First ``communicate`` times out; the reap (a SECOND communicate)
        records itself and returns. ``wait`` must never be touched: on a
        killed child blocked writing into a full stderr pipe it hangs the
        caller forever."""

        def __init__(self) -> None:
            # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a fake child, never signalled; moved verbatim
            self.pid = 4242
            self.returncode: int | None = None
            self.kill_calls = 0
            self.wait_calls = 0
            self.communicate_calls = 0

        async def communicate(self):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise asyncio.TimeoutError
            self.returncode = -9
            return b"", b""

        def kill(self) -> None:
            self.kill_calls += 1

        async def wait(self) -> int:
            self.wait_calls += 1
            return -9

    def test_mint_timeout_reaps_child_via_communicate_not_wait(self, monkeypatch):
        from kiro_crew.instances import token_mint as tm

        proc = self._HangProc()

        async def fake_exec(*a, **k):
            return proc

        killed: list[tuple[int, int]] = []

        async def _tree(pid, sig):
            killed.append((pid, sig))
            return True

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(platform_compat, "kill_process_tree_async", _tree)
        with pytest.raises(tm.TokenMintError, match="timed out minting"):
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        assert killed == [(proc.pid, platform_compat.SIGKILL)]
        assert proc.kill_calls == 1
        assert proc.communicate_calls == 2
        assert proc.wait_calls == 0

    @pytest.mark.parametrize("budget, names_setting", [(30.0, True), (120.0, False)])
    def test_mint_timeout_names_the_mint_budget_setting(self, monkeypatch, budget, names_setting):
        """An SSH mint timeout names ``instances.mint_timeout_secs`` as the next step.

        At the ceiling the setting cannot go higher, so the message points at the
        host's reachability instead of telling the user to raise it.
        """
        from kiro_crew.instances import token_mint as tm

        proc = self._HangProc()

        async def fake_exec(*a, **k):
            return proc

        async def _tree(pid, sig):
            return True

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(platform_compat, "kill_process_tree_async", _tree)
        with pytest.raises(tm.TokenMintError) as excinfo:
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h", timeout_secs=budget))
        msg = str(excinfo.value)
        assert msg.startswith(f"timed out minting token on cd-1 after {budget}s")
        assert "jump host or proxy" in msg
        assert ("instances.mint_timeout_secs" in msg) is names_setting
        assert ("applies to SSH and SSM mints alike" in msg) is names_setting
        assert ("already at its maximum" in msg) is not names_setting

    def test_run_remote_kirocrew_timeout_reaps_child_via_communicate_not_wait(self, monkeypatch):
        from kiro_crew.instances import token_mint as tm

        proc = self._HangProc()

        async def fake_exec(*a, **k):
            return proc

        killed: list[tuple[int, int]] = []

        async def _tree(pid, sig):
            killed.append((pid, sig))
            return True

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(platform_compat, "kill_process_tree_async", _tree)
        rc, err = asyncio.run(tm.run_remote_kirocrew("cd-1", "restart"))
        assert rc == -1
        assert "timed out after" in err
        assert killed == [(proc.pid, platform_compat.SIGKILL)]
        assert proc.kill_calls == 1
        assert proc.communicate_calls == 2
        assert proc.wait_calls == 0
