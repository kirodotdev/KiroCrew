"""Instances token mint: ttl parsing, command build, remote run, no token in logs."""

from __future__ import annotations

import asyncio
import re

import pytest

from kiro_crew import platform_compat


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
