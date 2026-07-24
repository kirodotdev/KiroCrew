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

_GOV_FILES = (
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
    assert security.is_sensitive_path("~/.kirocrew/profiles")
    assert security.is_sensitive_path("~/.kirocrew/profiles/anything.json")
    assert security.is_sensitive_path("~/.kirocrew/profiles/nested/deep.json")


def test_non_governance_kirocrew_paths_still_readable():
    # The ~/.kirocrew dir itself is NOT blanket-sensitive — only the trust-root
    # files are.  A normal state file under it must remain accessible.
    assert not security.is_sensitive_path("~/.kirocrew/sessions.db")
    assert not security.is_sensitive_path("~/.kirocrew/config.json")


def test_agent_fs_write_to_policy_denied_at_gate():
    # The PreToolUse host gate treats a path-like title via is_sensitive_path.
    hooks = HookManager()
    home = os.path.expanduser("~")
    result = hooks.on_tool_call(f"{home}/.kirocrew/security_policy.json")
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
        "tee ~/.kirocrew/security_policy.json",
        "mv /tmp/evil.json ~/.kirocrew/security_policy.json",
        "sed -i s/deny/allow/ ~/.kirocrew/security_policy.json",
        "ln -sf /tmp/evil ~/.kirocrew/profiles/app.json",
        "truncate -s0 ~/.kirocrew/admission_policy.json",
    ],
)
def test_bash_write_verbs_to_keystone_are_blocked(cmd):
    # The CRITICAL fix: write verbs (not just reads/redirects) to the governance
    # trust-root must be blocked by the shared bash gate.
    assert security.is_sensitive_bash_command(cmd) is not None


def test_benign_write_verbs_not_overblocked():
    for cmd in ["tee /tmp/out.txt", "mv a.txt b.txt", "rm /tmp/junk", "sed -i s/a/b/ README.md"]:
        assert security.is_sensitive_bash_command(cmd) is None


@pytest.mark.parametrize(
    "cmd",
    [
        "git checkout -- ~/.kirocrew/security_policy.json",
        "git restore ~/.kirocrew/security_policy.json",
        "cp evil /home/someuser/.kirocrew/security_policy.json",
        "unzip evil.zip -d ~/.kirocrew/profiles/",
        "tar -xf evil.tar -C ~/.kirocrew/",
        "tar xzf x -C /home/u/.kirocrew/",
        "curl x | tar xf - -C ~/.aws",
    ],
)
def test_archive_and_vcs_keystone_writes_blocked(cmd):
    # Write-verb allowlist was bypassable via extraction/checkout verbs and the
    # /home/<user> literal anchor; the verb-independent + extraction-destination
    # backstops must block these.
    assert security.is_sensitive_bash_command(cmd) is not None


def test_benign_archive_and_vcs_not_overblocked():
    for cmd in [
        "tar -xf release.tar -C /tmp/build",
        "git checkout -- src/main.py",
        "unzip data.zip -d /tmp/data",
        "git commit -m 'update'",
        "tar -cf out.tar ~/.kirocrew/sessions.db",  # reading a non-sensitive .kirocrew file
        "cat ~/.kirocrew/config.json",
    ]:
        assert security.is_sensitive_bash_command(cmd) is None, cmd


def test_case_variant_policy_path_is_sensitive():
    # Case-fold keystone: an alternate-case policy path (the same file on a
    # case-insensitive FS) must still be treated as sensitive.
    assert security.is_sensitive_path("~/.kirocrew/Security_Policy.json")
    assert security.is_sensitive_path("~/.KIROCREW/profiles/x.json")


def test_boot_assertion_passes_with_paths_present():
    assert_governance_paths_protected()  # no raise — default list has them


def test_boot_assertion_fails_if_paths_dropped(monkeypatch):
    # Simulate a refactor that dropped the governance entries → fail closed.
    monkeypatch.setattr(security, "_SENSITIVE_HOME_DIRS", [".aws", ".ssh"])
    with pytest.raises(PlatformCompositionError):
        assert_governance_paths_protected()
