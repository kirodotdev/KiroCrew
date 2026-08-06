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
from kiro_crew.config.loader import config_dir
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


# ── Dev Container trust store (grants authorize container builds) ─────────────
# ``devcontainer.is_trusted()`` compares a project's current ``.devcontainer/``
# digest against a grant recorded in ``<data home>/devcontainers/trust.json``,
# and a grant authorizes ``devcontainer up`` to build and run that config —
# lifecycle commands, ``runArgs``, ``privileged``, ``mounts`` and all. An agent
# that could WRITE the store would record a matching digest for a config it just
# authored and self-approve arbitrary container execution, bypassing the human
# trust prompt the whole feature rests on. Pinned as the whole directory.


def test_devcontainer_trust_store_is_sensitive_under_the_live_data_home():
    """REVERT-VERIFIED against the ``"devcontainers"`` entry in
    ``security._CREW_SECRET_LEAVES``: drop the leaf and both assertions below
    flip to False, leaving the trust store agent-writable.

    Anchored on ``config_dir()`` rather than a ``~/`` literal so it follows the
    ``KIROCREW_HOME`` re-anchoring in ``_home_dir_targets`` — this is the path
    the gateway actually writes, whatever the home resolves to."""
    home = config_dir()
    assert security.is_sensitive_path(str(home / "devcontainers" / "trust.json"))
    # The directory itself, so a future sidecar beside trust.json is covered.
    assert security.is_sensitive_path(str(home / "devcontainers"))
    assert security.is_sensitive_path(str(home / "devcontainers" / "future-sidecar.json"))


def test_a_neighbouring_data_home_path_is_still_readable():
    """Proves the leaf was ADDED rather than the data home being blanket-blocked:
    a sibling directory under the same home stays accessible."""
    home = config_dir()
    assert not security.is_sensitive_path(str(home / "devcontainer-notes.json"))
    assert not security.is_sensitive_path(str(home / "sessions.db"))
    assert not security.is_sensitive_path(str(home / "workspace" / "notes.md"))


_DEVCONTAINER_TRUST_PATHS = (
    "~/.kiro/crew/devcontainers",
    "~/.kiro/crew/devcontainers/trust.json",
    # Legacy pre-move home is gated too — the leaf expands under every prefix.
    "~/.kirocrew/devcontainers",
    "~/.kirocrew/devcontainers/trust.json",
)


@pytest.mark.parametrize("path", _DEVCONTAINER_TRUST_PATHS)
def test_devcontainer_trust_paths_are_sensitive_under_both_home_prefixes(path):
    assert security.is_sensitive_path(path)


@pytest.mark.parametrize("path", _DEVCONTAINER_TRUST_PATHS)
def test_validate_file_path_rejects_the_devcontainer_trust_store(path):
    assert validate_file_path(path) is None


def test_agent_fs_write_to_devcontainer_trust_denied_at_gate():
    hooks = HookManager()
    home = os.path.expanduser("~")
    result = hooks.on_tool_call(f"{home}/.kiro/crew/devcontainers/trust.json")
    assert result.action == TOOL_DENY


@pytest.mark.parametrize(
    "cmd",
    [
        "tee ~/.kiro/crew/devcontainers/trust.json",
        "cp /tmp/forged.json ~/.kiro/crew/devcontainers/trust.json",
        "cat ~/.kiro/crew/devcontainers/trust.json",
    ],
)
def test_bash_access_to_the_devcontainer_trust_store_is_blocked(cmd):
    assert security.is_sensitive_bash_command(cmd) is not None


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
