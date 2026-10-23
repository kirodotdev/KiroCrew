"""What push_guard.py says, and refuses, when git fails.

A failed fetch never prints a remote's credentials, for URL and SCP-style remotes
alike; every git error in the patch-id replay check fails closed; tracked content
that is not UTF-8 does not crash the guard; a fetch error is classified without
echoing raw stderr; and a child the guard started is stopped with the guard.
``TestPushGuardCredentialRedaction`` calls ``TestReplayFailClosed``'s fake-git builder
and in-process runner by class name, and ``TestPushGuardChildren`` its fake-git
builder, so the three share this module.
"""

from __future__ import annotations

import io
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import push_guard_helpers
import pytest
from push_guard_helpers import (
    _SUBPROCESS_TIMEOUT_S,
    PUSH_GUARD,
    _bless,
    _git,
    _inprocess,
    _load_push_guard,
    _run_push_guard,
    _run_push_guard_inprocess,
)

from kiro_crew import platform_compat
from kiro_crew.testing.wait import wait_until

# pytest resolves a fixture in the requesting test's module, so each shared
# fixture this module uses is bound here by name.
_no_inherited_git_c = push_guard_helpers._no_inherited_git_c
_repo_pair_template = push_guard_helpers._repo_pair_template
repo_pair = push_guard_helpers.repo_pair


class TestPushGuardCredentialRedaction:
    """Regression: fetch-failure diagnostics must never expose URL credentials.

    When a git-fetch fails against a credential-bearing remote URL (e.g.
    https://user:token@host/repo), the raw stderr contains the full URL.
    The refusal message printed by push_guard must redact the userinfo so
    tokens/passwords never reach agent transcripts or logs.
    """

    @staticmethod
    def _run_fetch_failure(
        monkeypatch, tmp_path, repo_dir, *, prefix="fatal: Authentication failed for "
    ):
        """Emit raw synthetic fetch stderr through the real guard subprocess path."""
        remote = _git(repo_dir, "remote", "get-url", "origin")
        fake_cmd = TestReplayFailClosed._make_fake_git_cmd(
            tmp_path,
            "args and args[0] == 'fetch'",
            failure_message=prefix + remote,
        )
        result = TestReplayFailClosed._run_push_guard_inprocess(monkeypatch, repo_dir, fake_cmd)
        assert "error class:" in result[2], "The fetch diagnostic did not reach classification"
        return result

    def test_fetch_error_redacts_credentials(self, tmp_path, monkeypatch):
        """Fetch stderr containing https://user:token@host → refusal redacts the token."""
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        # Record a synthetic remote. Only the injected fetch emits its raw URL.
        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "https://user:someSecretToken123@example.com/repo.git",
        )

        rc, stdout, stderr = self._run_fetch_failure(monkeypatch, tmp_path, repo_dir)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        # The token must NOT appear in any output — regardless of whether git
        # itself stripped it or our redaction layer did.
        assert (
            "someSecretToken123" not in stderr
        ), "Credential leaked in stderr: token was not redacted"
        assert (
            "someSecretToken123" not in stdout
        ), "Credential leaked in stdout: token was not redacted"
        assert "user:someSecretToken123" not in stderr
        assert "user:someSecretToken123" not in stdout

    def test_fetch_error_redacts_bare_token_url(self, tmp_path, monkeypatch):
        """Fetch stderr containing https://ghp_token@host → redacts the token."""
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        # PAT-style URL (no colon separator, just token@host).
        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "https://ghp_aBcDeFgHiJkLmNoPqRsT@github.com/org/repo.git",
        )

        rc, stdout, stderr = self._run_fetch_failure(monkeypatch, tmp_path, repo_dir)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        # The PAT must NOT appear in any output.
        assert (
            "ghp_aBcDeFgHiJkLmNoPqRsT" not in stderr
        ), "PAT token leaked in stderr: token was not redacted"
        assert (
            "ghp_aBcDeFgHiJkLmNoPqRsT" not in stdout
        ), "PAT token leaked in stdout: token was not redacted"

    def test_fetch_error_preserves_diagnostic_without_credentials(self, tmp_path):
        """Fetch failure without credentials keeps the full diagnostic intact."""
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        # Non-credential-bearing URL — diagnostic should pass through unchanged.
        _git(repo_dir, "remote", "add", "origin", "/nonexistent/repo.git")

        rc, stdout, stderr = _run_push_guard(repo_dir)
        assert rc == 40
        assert "REFUSED" in stderr
        assert "fetch" in stderr.lower()
        # No redaction needed — no credentials to strip.
        assert "<redacted>" not in stderr

    def test_fetch_error_redacts_query_string_credentials(self, tmp_path, monkeypatch):
        """Fetch stderr with query-string credentials → refusal redacts the secret.

        Regression: query-string tokens (private_token=, access_token=,
        x-access-token=) are common on self-hosted forges (GitLab, Gitea) and
        CI job tokens.  The redaction layer must strip the entire query string
        so the secret never reaches agent transcripts or logs.
        """
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        # Query-string credential URL — a common self-hosted forge pattern.
        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "https://git.example.com/team/Repo?private_token=secret123",
        )

        rc, stdout, stderr = self._run_fetch_failure(monkeypatch, tmp_path, repo_dir)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        # The query-string token must NOT appear in any output.
        assert (
            "secret123" not in stderr
        ), "Query-string credential leaked in stderr: token was not redacted"
        assert (
            "secret123" not in stdout
        ), "Query-string credential leaked in stdout: token was not redacted"
        assert "private_token" not in stderr, "Query parameter name leaked in stderr"
        assert "private_token" not in stdout, "Query parameter name leaked in stdout"
        # The derived diagnostic shows only the classified error label —
        # no raw stderr (even redacted) is passed through.
        assert "error class:" in stderr, "Classified error diagnostic not found in refusal output"

    def test_fetch_error_redacts_access_token_query(self, tmp_path, monkeypatch):
        """access_token= query parameter → redacted."""
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "https://git.example.com/org/project.git?access_token=ghp_TopSecret99",
        )

        rc, stdout, stderr = self._run_fetch_failure(monkeypatch, tmp_path, repo_dir)
        assert rc == 40
        assert "REFUSED" in stderr
        assert "ghp_TopSecret99" not in stderr, "access_token value leaked"
        assert "ghp_TopSecret99" not in stdout, "access_token value leaked"

    def test_fetch_error_redacts_path_embedded_credentials(self, tmp_path, monkeypatch):
        """Fetch stderr with path-embedded token → refusal redacts the secret.

        Regression: some forges and CI proxies embed PATs or deploy tokens
        directly in the URL path (e.g. https://host/<token>/repo.git).  The
        authority-only redaction policy must strip the entire path so the token
        never reaches agent transcripts or logs — while still preserving the
        host for diagnostic value.
        """
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        # Path-embedded credential URL — the token sits in a URL path segment.
        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "https://git.example.com/tok_secret123/Repo.git",
        )

        rc, stdout, stderr = self._run_fetch_failure(monkeypatch, tmp_path, repo_dir)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        # The path-embedded token must NOT appear in any output.
        assert (
            "tok_secret123" not in stderr
        ), "Path-embedded credential leaked in stderr: token was not redacted"
        assert (
            "tok_secret123" not in stdout
        ), "Path-embedded credential leaked in stdout: token was not redacted"
        # The exact redacted form: scheme://host/<redacted> (also proves
        # The derived diagnostic shows only the classified error label —
        # no raw stderr (even redacted) is passed through.
        assert "error class:" in (
            stdout + stderr
        ), "Classified error diagnostic not found in refusal output"

    def test_fetch_error_redacts_query_only_credentials(self, tmp_path, monkeypatch):
        """Fetch stderr with query-only URL (no path) → refusal redacts the secret.

        Regression: a URL like https://host?private_token=x has no path
        component; the prior regex required a literal '/' after the authority
        so the query-string credential passed through unredacted.
        """
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "https://git.example.com?private_token=qsecret1",
        )

        rc, stdout, stderr = self._run_fetch_failure(monkeypatch, tmp_path, repo_dir)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert (
            "qsecret1" not in stderr
        ), "Query-only credential leaked in stderr: token was not redacted"
        assert (
            "qsecret1" not in stdout
        ), "Query-only credential leaked in stdout: token was not redacted"
        # The derived diagnostic shows only the classified error label —
        # no raw stderr (even redacted) is passed through.
        assert "error class:" in (
            stdout + stderr
        ), "Classified error diagnostic not found in refusal output"

    def test_fetch_error_redacts_ipv6_path_credentials(self, tmp_path, monkeypatch):
        """Fetch stderr with bracketed IPv6 authority → refusal redacts the secret.

        Regression: the prior regex used [^\\s/:\"']+ for the host charset,
        which excludes ':', so a bracketed IPv6 authority ([2001:db8::7]) could
        never match — its path/query credentials passed through unredacted.
        """
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "https://[2001:db8::7]:8443/tok_v6secret/Repo.git",
        )

        rc, stdout, stderr = self._run_fetch_failure(monkeypatch, tmp_path, repo_dir)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert (
            "tok_v6secret" not in stderr
        ), "IPv6 path credential leaked in stderr: token was not redacted"
        assert (
            "tok_v6secret" not in stdout
        ), "IPv6 path credential leaked in stdout: token was not redacted"
        # The derived diagnostic shows only the classified error label —
        # no raw stderr (even redacted) is passed through.
        assert "error class:" in (
            stdout + stderr
        ), "Classified error diagnostic not found in refusal output"

    def test_fetch_error_redacts_credential_at_truncation_boundary(self, tmp_path, monkeypatch):
        """Token straddling the old 300-byte slice boundary must still be redacted.

        Redaction must run before truncation. Doing ``redact_credentials(fetch_err[:300])``
        means that if the credential
        URL started before byte 300 but the '@host' portion landed after it,
        the slice would break the URL into an unmatchable prefix and the token
        would print raw.  After the fix (``redact_credentials(fetch_err)[:300]``)
        the full string is redacted first, so the token never appears.
        """
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        # Construct a credential-bearing URL where the token starts well before
        # byte 300 but the '@' separator lands AFTER byte 300 so that
        # fetch_err[:300] would split the URL mid-credential.
        # Use a long PAT (fine-grained token style) that pushes '@' past 300.
        # The "remote: " preamble + URL scheme + token need to exceed 300 chars
        # before the '@'.  A 260-char preamble + "https://" (8) + 40-char token
        # puts the '@' at offset ~308.
        token = "ghp_" + "A" * 36  # 40 chars total
        cred_url = "https://{}@git.example.com/org/repo.git".format(token)
        # The injected fetch emits this full raw URL after a known preamble.
        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            cred_url,
        )

        prefix = "remote: " + "X" * 252
        assert (prefix + cred_url).index(token) < 300 < (prefix + cred_url).index("@")
        rc, stdout, stderr = self._run_fetch_failure(monkeypatch, tmp_path, repo_dir, prefix=prefix)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        # The token MUST NOT appear anywhere in the output.
        combined = stdout + stderr
        assert token not in combined, (
            "Split-boundary regression: credential token '{}' leaked in "
            "output — redaction ran AFTER truncation".format(token[:10] + "...")
        )
        # The derived diagnostic shows only the error class label — raw stderr
        # (even post-redaction) is never passed through.  Verify the classified
        # diagnostic is present.
        assert "error class:" in combined, "Classified error diagnostic not found in refusal output"


class TestPushGuardScpCredentialRedaction:
    """Regression: scheme-less SCP-style remotes must not expose userinfo.

    When a git-fetch/ssh fails against a credential-bearing SCP-style remote
    (e.g. tok_secret@git.example.com:team/Repo.git), the raw stderr contains
    the userinfo verbatim. The refusal message printed by push_guard must
    redact the userinfo so tokens/secrets never reach agent transcripts.

    These tests fail against the code at e89d975b (which only scrubs
    scheme-bearing URLs with ://) and pass after the scp-style pass is added.
    """

    @pytest.fixture
    def fake_ssh(self, tmp_path):
        """Create a fake SSH (Python script) that fails echoing the remote.

        Uses a Python script instead of a shell script so the fixture works
        on Windows (where #!/bin/sh scripts set as GIT_SSH_COMMAND fail
        because Git's bundled sh cannot resolve bare Windows paths with
        backslashes).  GIT_SSH_COMMAND is parsed by sh on every platform, so
        both the interpreter and script paths use forward slashes and are
        individually quoted.
        """
        ssh_script = tmp_path / "fake_ssh.py"
        # The script extracts user@host from argv (git passes host as argv[1]
        # or user@host as part of the arguments) and prints it to stderr,
        # simulating what real ssh does on auth failure.
        ssh_script.write_text(
            "import sys, re\n"
            "args = ' '.join(sys.argv[1:])\n"
            "m = re.search(r'[^\\s]*@[^\\s]*', args)\n"
            "if m:\n"
            "    print(f'{m.group(0)}: Permission denied (publickey).', file=sys.stderr)\n"
            "else:\n"
            "    print('Permission denied (publickey).', file=sys.stderr)\n"
            "sys.exit(255)\n"
        )
        # Build a GIT_SSH_COMMAND with quoted forward-slash paths so it works
        # on all platforms (Git parses GIT_SSH_COMMAND through sh everywhere).
        interp = Path(sys.executable).as_posix()
        script = ssh_script.as_posix()
        return f'"{interp}" "{script}"'

    def _run_push_guard_with_ssh(
        self, cwd: str, fake_ssh: str, extra_args: list[str] | None = None
    ) -> tuple[int, str, str]:
        """Run push_guard.py with a fake SSH to avoid real network calls."""
        return _run_push_guard(cwd, extra_args, env_extra={"GIT_SSH_COMMAND": fake_ssh})

    def test_scp_permission_denied_redacts_userinfo(self, tmp_path, fake_ssh):
        """SSH failure echoing tok_secret123@git.example.com → userinfo redacted.

        Simulates: git fetch fails and ssh prints
        "tok_secret123@git.example.com: Permission denied (publickey)."
        The token must not appear in the refusal output; the host must.
        """
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        # SCP-style remote with a credential in the userinfo position.
        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "tok_secret123@git.example.com:team/Repo.git",
        )

        rc, stdout, stderr = self._run_push_guard_with_ssh(repo_dir, fake_ssh)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        combined = stdout + stderr
        # The credential (userinfo) must NOT appear anywhere in the output.
        assert (
            "tok_secret123" not in combined
        ), "SCP userinfo credential leaked: tok_secret123 was not redacted"
        # The host must still be visible for diagnostic value, verified via
        # the exact redacted form (not a bare substring — CodeQL lesson).
        # The derived diagnostic shows only the error class — no raw/redacted
        # stderr is passed through.
        assert "error class:" in combined, "Classified error diagnostic not found in refusal output"

    def test_scp_fetch_url_redacts_userinfo(self, tmp_path, fake_ssh):
        """git fetch tok_secret123@git.example.com:team/Repo.git → userinfo redacted.

        Simulates: fetch stderr echoes the full SCP remote URL.
        The token must not appear; the host must be preserved.
        """
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        # SCP-style remote — the full remote URL appears in fetch stderr.
        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "tok_secret123@git.example.com:team/Repo.git",
        )

        rc, stdout, stderr = self._run_push_guard_with_ssh(repo_dir, fake_ssh)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        combined = stdout + stderr
        assert (
            "tok_secret123" not in combined
        ), "SCP fetch URL credential leaked: tok_secret123 was not redacted"
        # Exact redacted form proves both redaction AND host preservation.
        # The derived diagnostic shows only the error class — no raw/redacted
        # stderr is passed through.
        assert "error class:" in combined, "Classified error diagnostic not found in refusal output"

    def test_scp_bare_user_at_host_redacts_userinfo(self, tmp_path, fake_ssh):
        """SCP remote without :path (bare user@host) → userinfo redacted.

        Some SSH failure messages echo just "user@host" without the trailing
        colon+path. The redaction must still catch this form.
        """
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        # Use a hostname with a dot to ensure the regex recognizes it as a host.
        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "deploy_key_abc@git.example.com:org/project.git",
        )

        rc, stdout, stderr = self._run_push_guard_with_ssh(repo_dir, fake_ssh)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        combined = stdout + stderr
        assert (
            "deploy_key_abc" not in combined
        ), "SCP bare userinfo credential leaked: deploy_key_abc was not redacted"
        # The derived diagnostic shows only the error class — no raw/redacted
        # stderr is passed through.
        assert "error class:" in combined, "Classified error diagnostic not found in refusal output"

    def test_scp_dotless_host_colon_path_redacts_userinfo(self, tmp_path, fake_ssh):
        """SCP remote with dotless host + colon-path → userinfo redacted.

        Regression: at 44a9fe6a the host regex required a dot, so
        ``tok_secret123@forge:team/repo.git`` passed unredacted.
        """
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "tok_secret123@forge:team/repo.git",
        )

        rc, stdout, stderr = self._run_push_guard_with_ssh(repo_dir, fake_ssh)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        combined = stdout + stderr
        assert (
            "tok_secret123" not in combined
        ), "SCP dotless-host credential leaked: tok_secret123 was not redacted"
        # The dotless host is preserved for diagnostic value.
        # The derived diagnostic shows only the error class — no raw/redacted
        # stderr is passed through.
        assert "error class:" in combined, "Classified error diagnostic not found in refusal output"

    def test_scp_bracketed_ipv6_host_redacts_userinfo(self, tmp_path, fake_ssh):
        """SCP remote with bracketed IPv6 host → userinfo redacted.

        Regression: at 44a9fe6a the host regex could not match IPv6 addresses,
        so ``tok_secret123@[::1]:team/repo.git`` passed unredacted.
        Note: git strips brackets when calling SSH, so the actual stderr
        contains ``tok_secret123@::1:`` (un-bracketed).
        """
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "tok_secret123@[::1]:team/repo.git",
        )

        rc, stdout, stderr = self._run_push_guard_with_ssh(repo_dir, fake_ssh)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        combined = stdout + stderr
        assert (
            "tok_secret123" not in combined
        ), "SCP bracketed-IPv6 credential leaked: tok_secret123 was not redacted"
        # Git strips brackets when calling SSH, so the host appears un-bracketed
        # in SSH stderr.  The redacted form preserves the bare IPv6 address.
        # The derived diagnostic shows only the error class — no raw/redacted
        # stderr is passed through.
        assert "error class:" in combined, "Classified error diagnostic not found in refusal output"


class TestReplayFailClosed:
    """Regression: patch-id replay check must fail CLOSED on git errors.

    At b94a9448 the replay check's git-error paths printed "SAFE TO PUSH"
    and returned 0, allowing a push to proceed when a transient fetch failure
    (e.g. blobless/partial clone lazy-fetch timeout) prevented verification.
    The fix makes every git subprocess failure exit 40 with a diagnostic.

    Tests use a module-level monkeypatch on push_guard._GIT_CMD (a Python
    fake-git script) instead of the former _PUSH_GUARD_GIT_CMD environment
    variable — this eliminates the Semgrep dangerous-subprocess-use-tainted-
    env-args finding while remaining fully portable (Windows + POSIX).
    """

    @staticmethod
    def _make_fake_git_cmd(
        tmp_path: Path,
        fail_condition: str,
        *,
        failure_message: str = "fatal: bad revision/object",
    ) -> list[str]:
        """Create a cross-platform fake git that fails on a specific condition.

        Uses a Python script assigned directly to push_guard._GIT_CMD (no
        PATH manipulation, no shell launchers, no environment variables).
        This avoids all Windows compatibility issues and Semgrep tainted-env
        findings.

        Args:
            tmp_path: pytest tmp dir for writing script files.
            fail_condition: Python expression evaluated against ``args``
                (the list of git arguments) that triggers the failure.
            failure_message: Raw stderr emitted by the failing subprocess.

        Returns:
            Command list suitable for assignment to push_guard._GIT_CMD.
        """
        real_git = shutil.which("git")
        assert real_git, "git must be on PATH for these tests"

        fake_git_script = tmp_path / "fake_git.py"
        # The fake git delegates to real git for all commands except those
        # matching the fail condition.  Real git path is embedded as a
        # string literal (no env lookup) to avoid any tainted-env concerns.
        fake_git_script.write_text(
            "import subprocess, sys\n"
            "args = sys.argv[1:]\n"
            f"if {fail_condition}:\n"
            f"    print({failure_message!r}, file=sys.stderr)\n"
            "    sys.exit(128)\n"
            "real_git = {}\n".format(repr(real_git))
            + "r = subprocess.run([real_git] + args, timeout={})\n".format(_SUBPROCESS_TIMEOUT_S)
            + "sys.exit(r.returncode)\n"
        )

        return [sys.executable, str(fake_git_script)]

    # The body is ``push_guard_helpers._run_push_guard_inprocess``: classes in other
    # modules call it too, as ``_inprocess``.
    _run_push_guard_inprocess = staticmethod(_run_push_guard_inprocess)

    def test_revlist_ahead_failure_refuses(self, repo_pair, tmp_path, monkeypatch):
        """rev-list origin/<base>..HEAD failure → exit 40, not SAFE."""
        clone_dir, _ = repo_pair

        _git(clone_dir, "checkout", "-b", "feature/revlist-fail-test")
        Path(clone_dir, "change.py").write_text("# change\n")
        _git(clone_dir, "add", "change.py")
        _git(clone_dir, "commit", "-m", "feat: test commit")

        fake_cmd = self._make_fake_git_cmd(
            tmp_path, "args[:1] == ['rev-list'] and any('..' in a for a in args)"
        )

        rc, stdout, stderr = self._run_push_guard_inprocess(monkeypatch, clone_dir, fake_cmd)
        assert rc == 40, (
            f"Expected refused (40), got {rc}. A failing "
            f"rev-list must not produce SAFE TO PUSH.\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )
        assert "SAFE TO PUSH" not in stdout
        combined = (stdout + stderr).lower()
        assert "refused" in combined, (
            "Expected a REFUSED diagnostic on rev-list failure.\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )

    def test_difftree_failure_refuses(self, repo_pair, tmp_path, monkeypatch):
        """diff-tree -p failure → exit 40, not skip-and-SAFE."""
        clone_dir, _ = repo_pair

        _git(clone_dir, "checkout", "-b", "feature/difftree-fail-test")
        Path(clone_dir, "change.py").write_text("# change\n")
        _git(clone_dir, "add", "change.py")
        _git(clone_dir, "commit", "-m", "feat: test commit")

        fake_cmd = self._make_fake_git_cmd(tmp_path, "'diff-tree' in args")

        rc, stdout, stderr = self._run_push_guard_inprocess(monkeypatch, clone_dir, fake_cmd)
        assert rc == 40, (
            f"Expected refused (40), got {rc}. A failing "
            f"diff-tree must not produce SAFE TO PUSH.\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )
        assert "SAFE TO PUSH" not in stdout
        combined = (stdout + stderr).lower()
        assert "refused" in combined and "diff-tree" in combined, (
            "Expected a REFUSED diagnostic mentioning diff-tree.\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )

    def test_revlist_base_failure_refuses(self, repo_pair, tmp_path, monkeypatch):
        """rev-list --max-count base-history failure → exit 40, not SAFE."""
        clone_dir, _ = repo_pair

        _git(clone_dir, "checkout", "-b", "feature/base-revlist-fail-test")
        Path(clone_dir, "change.py").write_text("# change\n")
        _git(clone_dir, "add", "change.py")
        _git(clone_dir, "commit", "-m", "feat: test commit")

        fake_cmd = self._make_fake_git_cmd(
            tmp_path,
            "any('--max-count' in a or a.startswith('--max-count=') for a in args)",
        )

        rc, stdout, stderr = self._run_push_guard_inprocess(monkeypatch, clone_dir, fake_cmd)
        assert rc == 40, (
            f"Expected refused (40), got {rc}. A failing base "
            f"rev-list must not produce SAFE TO PUSH.\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )
        assert "SAFE TO PUSH" not in stdout
        combined = (stdout + stderr).lower()
        assert "refused" in combined and "rev-list" in combined, (
            "Expected a REFUSED diagnostic mentioning rev-list.\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )

    def test_patchid_failure_refuses(self, repo_pair, tmp_path, monkeypatch):
        """patch-id --stable failure → exit 40, and it is the INJECTED git that fails.

        Both ``patch-id`` calls go through ``run()`` and so through ``_GIT_CMD``,
        which is what lets this fake reach them: a bare ``git`` spawn of their
        own would be untestable here and would cost every run of the class a
        real host ``git``. Failing the fake on ``patch-id`` and getting the refusal proves
        the calls now go through ``run()`` like every other command.
        """
        clone_dir, _ = repo_pair

        _git(clone_dir, "checkout", "-b", "feature/patchid-fail-test")
        Path(clone_dir, "change.py").write_text("# change\n")
        _git(clone_dir, "add", "change.py")
        _git(clone_dir, "commit", "-m", "feat: test commit")

        fake_cmd = self._make_fake_git_cmd(tmp_path, "'patch-id' in args")

        rc, stdout, stderr = self._run_push_guard_inprocess(monkeypatch, clone_dir, fake_cmd)
        assert rc == 40, (
            f"Expected refused (40), got {rc}. A failing "
            f"patch-id must not produce SAFE TO PUSH.\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )
        assert "SAFE TO PUSH" not in stdout
        combined = (stdout + stderr).lower()
        assert "refused" in combined and "patch-id" in combined, (
            "Expected a REFUSED diagnostic mentioning patch-id.\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )

    def test_empty_ahead_set_on_success_is_still_safe(self, repo_pair):
        """rev-list SUCCEEDS with empty output (0 ahead commits) → still SAFE."""
        clone_dir, _ = repo_pair

        # Stay on main — 0 commits ahead of origin/main.
        rc, stdout, stderr = _run_push_guard(clone_dir)
        assert rc == 0, (
            f"Expected safe (0), got {rc}. No ahead commits should be "
            f"safe.\nstdout: {stdout}\nstderr: {stderr}"
        )
        assert "SAFE TO PUSH" in stdout


class TestNonUtf8Decoding:
    """Regression: non-UTF-8 tracked content must not crash the guard.


    At 8ed873cf the shared run() helper used subprocess.run(..., text=True)
    with strict decoding.  Any non-UTF-8 byte in git diff-tree output (which
    carries raw patch content for the patch-id replay check) raised an
    uncaught UnicodeDecodeError, preventing the push workflow.

    Fix: errors="replace" on all subprocess.run(..., text=True) calls.
    """

    def test_ahead_commit_with_non_utf8_content(self, repo_pair):
        """Ahead-commit adds a file with non-UTF-8 bytes → guard completes."""
        clone_dir, _ = repo_pair

        _git(clone_dir, "checkout", "-b", "feature/binary-content")
        # Write raw bytes that are invalid UTF-8.
        Path(clone_dir, "binary_data.bin").write_bytes(b"\xff\xfe latin \xe9 end")
        _git(clone_dir, "add", "binary_data.bin")
        _git(clone_dir, "commit", "-m", "feat: add binary data file")

        _bless(clone_dir)
        rc, stdout, stderr = _run_push_guard(clone_dir)
        assert rc == 0, (
            f"Expected safe (0), got {rc}. Guard must not crash on "
            f"non-UTF-8 content.\nstdout: {stdout}\nstderr: {stderr}"
        )
        assert "SAFE TO PUSH" in stdout

    def test_base_history_with_non_utf8_content(self, repo_pair):
        """Base-history commit (within replay window) has non-UTF-8 → guard completes."""
        clone_dir, origin_dir = repo_pair

        # Push a commit with non-UTF-8 bytes to origin/main (base history).
        work2 = os.path.dirname(clone_dir) + "/work2"
        _git(os.path.dirname(clone_dir), "clone", origin_dir, "work2")
        Path(work2, "upstream_binary.bin").write_bytes(b"\xff\xfe\x80\x81 raw bytes")
        _git(work2, "add", "upstream_binary.bin")
        _git(work2, "commit", "-m", "chore: add binary asset")
        _git(work2, "push", "origin", "main")

        # Fetch so origin/main is current, then branch from the fresh tip.
        _git(clone_dir, "fetch", "origin")
        _git(clone_dir, "checkout", "-b", "feature/after-binary-base", "origin/main")
        Path(clone_dir, "novel_fix.py").write_text("# novel fix\n")
        _git(clone_dir, "add", "novel_fix.py")
        _git(clone_dir, "commit", "-m", "fix: novel fix after binary base")

        _bless(clone_dir)
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "5"])
        assert rc == 0, (
            f"Expected safe (0), got {rc}. Guard must not crash on "
            f"non-UTF-8 base history.\nstdout: {stdout}\nstderr: {stderr}"
        )
        assert "SAFE TO PUSH" in stdout


class TestClassifyFetchError:
    """Regression tests for _classify_fetch_error (round-13 derived diagnostic).

    Contract: raw stderr content NEVER reaches refusal output.  Only
    hardcoded class labels from the allowlist are surfaced.  When no pattern
    matches, a generic withholding message is returned.
    """

    @staticmethod
    def _import_classify():
        return _load_push_guard()._classify_fetch_error

    def test_known_class_could_not_resolve_host(self):
        classify = self._import_classify()
        result = classify(
            "fatal: unable to access 'https://example.com/repo.git/': "
            "Could not resolve host: example.com"
        )
        assert result == "could not resolve host"

    def test_known_class_permission_denied(self):
        classify = self._import_classify()
        assert classify("Permission denied, please try again.") == "permission denied"
        # Under BatchMode an unloaded key reads the same way; name the real fix.
        assert "ssh-add" in classify("Permission denied (publickey).")

    def test_known_class_repository_not_found(self):
        classify = self._import_classify()
        result = classify("remote: Repository not found.\nfatal: ...")
        assert result == "repository not found"

    def test_known_class_not_a_git_repository(self):
        classify = self._import_classify()
        result = classify("fatal: '/tmp/notgit' does not appear to be a git repository")
        assert result == "not a git repository"

    def test_known_class_timeout(self):
        classify = self._import_classify()
        result = classify("fatal: unable to access: Failed to connect: Connection timed out")
        assert result == "connection timed out"

    def test_known_class_authentication_failed(self):
        classify = self._import_classify()
        result = classify(
            "remote: Invalid username or password.\n"
            "fatal: Authentication failed for 'https://github.com/org/repo.git/'"
        )
        assert result == "authentication failed"

    def test_known_class_remote_ref_not_found(self):
        classify = self._import_classify()
        result = classify("fatal: couldn't find remote ref refs/heads/nonexistent")
        assert result == "remote ref not found"

    def test_known_class_connection_refused(self):
        classify = self._import_classify()
        result = classify("fatal: unable to access: Failed to connect: Connection refused")
        assert result == "connection refused"

    def test_known_class_tls_error(self):
        classify = self._import_classify()
        result = classify(
            "fatal: unable to access: SSL certificate problem: "
            "unable to get local issuer certificate"
        )
        assert result == "TLS/certificate error"

    def test_bare_token_in_stderr_withheld(self):
        """A bare token (no URL shape) in stderr must NEVER reach output.

        This is the core round-13 regression: an ext:: remote helper can echo
        arbitrary command lines including bare credential strings that no
        URL-shape scrubber can redact.
        """
        classify = self._import_classify()
        stderr_with_bare_token = (
            "external helper failed: /usr/lib/git-core/git-remote-ext "
            "--run tok_secret123 --endpoint api.internal.corp"
        )
        result = classify(stderr_with_bare_token)
        # Must not contain the token
        assert "tok_secret123" not in result
        # Must be the generic withholding message
        assert "details withheld" in result

    def test_unrecognized_free_text_withheld(self):
        """Completely novel stderr text is withheld entirely."""
        classify = self._import_classify()
        result = classify("some-custom-hook: unexpected error code 42 from internal endpoint")
        assert "details withheld" in result
        assert "some-custom-hook" not in result

    def test_empty_stderr_withheld(self):
        """Empty stderr returns the withholding message."""
        classify = self._import_classify()
        result = classify("")
        assert "details withheld" in result


class TestFetchDiagnosticIntegration:
    """Integration test: fetch failure refusal output never leaks raw stderr."""

    @staticmethod
    def _import_push_guard():
        return _load_push_guard()

    def test_fetch_failure_with_bare_token_no_leak(self, tmp_path, monkeypatch):
        """When git fetch fails with stderr containing a bare token,
        the refusal output must not contain that token."""
        push_guard = self._import_push_guard()

        # Monkeypatch run() to simulate a fetch failure with bare token stderr
        original_run = push_guard.run

        def fake_run(args, **kwargs):
            if "fetch" in args:
                return (
                    128,
                    "",
                    "ext::helper --token ghp_SUPERSECRET123abc --host internal.corp",
                )
            if "symbolic-ref" in args:
                return (0, "origin/main", "")
            if "rev-parse" in args and "--is-inside-work-tree" in args:
                return (0, "true", "")
            return original_run(args, **kwargs)

        monkeypatch.setattr(push_guard, "run", fake_run)

        # Capture stderr from _fetch_base
        stderr_buf = io.StringIO()
        monkeypatch.setattr(sys, "stderr", stderr_buf)

        with pytest.raises(push_guard.Refused):
            push_guard._fetch_base("main")
        rc = 40
        stderr_output = stderr_buf.getvalue()

        # Guard must refuse (fetch failure)
        assert rc == 40
        # The bare token must NOT appear in stderr output
        assert "ghp_SUPERSECRET123abc" not in stderr_output
        # But it should mention the error class or withholding
        assert "details withheld" in stderr_output or "error class" in stderr_output

    def test_fetch_failure_known_class_shows_label(self, tmp_path, monkeypatch):
        """When fetch fails with a known error pattern, the label is shown."""
        push_guard = self._import_push_guard()

        def fake_run(args, **kwargs):
            if "fetch" in args:
                return (128, "", "fatal: Could not resolve host: github.com")
            return (0, "", "")

        monkeypatch.setattr(push_guard, "run", fake_run)

        stderr_buf = io.StringIO()
        monkeypatch.setattr(sys, "stderr", stderr_buf)

        with pytest.raises(push_guard.Refused):
            push_guard._fetch_base("main")
        stderr_output = stderr_buf.getvalue()

        assert "could not resolve host" in stderr_output
        # Raw URL should not appear
        assert "github.com" not in stderr_output


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX process groups and signals")
class TestPushGuardChildren:
    """A child is stopped with the guard, however the guard is stopped."""

    @staticmethod
    def _sleeping_fetch(tmp_path: Path) -> tuple[dict[str, str], Path]:
        """PATH whose git sleeps in fetch (writing its pid first), else is the real git."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        pid_file = tmp_path / "fetch.pid"
        fake = bin_dir / "git"
        fake.write_text(
            "#!{}\nimport os, sys, time\n"
            "if 'fetch' in sys.argv:\n"
            "    open({!r}, 'w').write(str(os.getpid()))\n"
            # flake-ok: the fake git's 60 s sleep is the hung fetch under test; the test kills it
            "    time.sleep(60)\n"
            "os.execv({!r}, [{!r}] + sys.argv[1:])\n".format(
                sys.executable, str(pid_file), shutil.which("git"), "git"
            )
        )
        fake.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        return env, pid_file

    @staticmethod
    def _gone(pid: int) -> bool:
        return wait_until(
            lambda: not platform_compat.pid_exists(pid),
            timeout=10,
            describe=lambda: f"pid {pid} still exists",
        )

    @pytest.mark.parametrize("how", ["SIGKILL-the-group", "SIGHUP"])
    def test_the_child_dies_with_the_guard(self, repo_pair, tmp_path, how):
        clone_dir, _ = repo_pair
        env, pid_file = self._sleeping_fetch(tmp_path)
        proc = subprocess.Popen(
            [sys.executable, PUSH_GUARD, "--base", "main"],
            cwd=clone_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        child = 0
        try:
            child = int(
                wait_until(
                    lambda: pid_file.exists() and pid_file.read_text(),
                    timeout=30,
                    describe=lambda: f"pid file {pid_file} not written yet",
                )
            )
            if how == "SIGHUP":
                os.kill(proc.pid, getattr(signal, "SIGHUP"))
            else:
                os.killpg(proc.pid, getattr(signal, "SIGKILL"))
            _, err = proc.communicate(timeout=_SUBPROCESS_TIMEOUT_S)
            assert self._gone(child), "the fetch outlived the guard"
            if how == "SIGHUP":
                assert proc.returncode == 40 and b"interrupted" in err, err
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, getattr(signal, "SIGKILL"))
                # flake-ok: reaps the guard this finally block just SIGKILLed; moved verbatim
                proc.wait()
            if child and platform_compat.pid_exists(child):
                os.kill(child, getattr(signal, "SIGKILL"))  # only the sleeper this test planted

    def test_an_exit_code_of_124_is_not_a_timeout(self, repo_pair, tmp_path, monkeypatch):
        clone_dir, _ = repo_pair
        fake = TestReplayFailClosed._make_fake_git_cmd(
            tmp_path, "'merge-base' in args", failure_message="fatal: odd"
        )
        Path(fake[1]).write_text(
            Path(fake[1]).read_text().replace("sys.exit(128)", "sys.exit(124)")
        )
        rc, _, stderr = _inprocess(monkeypatch, clone_dir, fake)
        assert rc == 40 and "failed (exit 124)" in stderr and "timed out" not in stderr, stderr

    def test_windows_stops_politely_before_it_forces(self, monkeypatch):
        pg = _load_push_guard()
        calls: list[list[str]] = []
        monkeypatch.setattr(pg.sys, "platform", "win32")
        monkeypatch.setattr(pg, "_taskkill", lambda: "C:\\Windows\\System32\\taskkill.exe")
        monkeypatch.setattr(pg.subprocess, "run", lambda argv, **kw: calls.append(argv))

        class _Proc:
            pid = 4242
            waits = 0

            def wait(self, timeout=None):
                _Proc.waits += 1
                if _Proc.waits == 1:
                    raise subprocess.TimeoutExpired("git", timeout)
                return 1

        pg._stop(_Proc())
        assert [c[1:-2] for c in calls] == [["/T"], ["/T", "/F"]]
