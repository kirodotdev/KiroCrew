"""Phase 3 — self-protection of the governance trust-root files (KEYSTONE).

Under "secure by default, not by mandate", what prevents a prompt-injected agent
from rewriting its own ceiling is that the policy/profile files are on the
sensitive-path floor (read + write blocked on every file-tool surface via
``is_sensitive_path``) and that the OS sandbox mounts them read-only for the
agent's shell. These tests pin the ``is_sensitive_path`` half; the shell half is
the sandbox's, not a matcher over command text.
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
#
# Fenced at BOTH layers, so neither the file tools nor a spawned shell can reach
# it: the file-tool gate below (``security._CREW_SECRET_LEAVES``), and the OS
# sandbox mask (``sandbox._CREW_HIDDEN_LEAVES``) that
# ``test_the_devcontainer_trust_store_is_masked_from_the_shell`` pins. The shell
# layer matters because ``is_sensitive_bash_command`` no longer matches paths in
# command text by design, so ``tee ~/.kiro/crew/devcontainers/trust.json`` from
# an agent shell is stopped only by the mask. HIDDEN rather than READONLY: nothing
# in the sandbox reads the store (the gateway's own ``_read_trust`` opens it
# directly, host-side), and hiding it also blocks the read that would let an agent
# learn which configs are already trusted.


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


def test_the_devcontainer_trust_store_is_masked_from_the_shell():
    """The file-tool gate is not enough on its own: ``is_sensitive_bash_command``
    matches no paths, so a shell ``tee ~/.kiro/crew/devcontainers/trust.json`` is
    stopped only by the OS sandbox mask. Pin that the ``devcontainers`` leaf sits
    in ``sandbox._CREW_HIDDEN_LEAVES`` — drop it there and this fails, re-opening
    the self-approval path the file-tool test cannot see.

    HIDDEN, not READONLY: the store has no in-sandbox reader (the gateway opens it
    host-side), so it is masked entirely rather than exposed read-only.
    """
    from kiro_crew import sandbox

    assert "devcontainers" in sandbox._CREW_HIDDEN_LEAVES
    # And it must NOT be merely read-only, which would still let a read leak which
    # configs are trusted.
    assert "devcontainers" not in sandbox._CREW_READONLY_LEAVES


def test_the_named_ceiling_and_secret_leaves_are_fenced_at_the_OS_LAYER():
    """The leaves this file names are fenced where a SUBPROCESS is actually bound.

    This assertion must not run ``is_sensitive_bash_command`` over eight attached-redirect
    spellings (``>~/.kiro/crew/./<leaf>`` and the ``<`` input forms). It was testing the
    wrong layer, and the layer it tested could not hold: a spawned shell reaches a file
    through an ``open()`` that never routes through the tool gate, so a path fenced only
    there is readable in any sandbox mode whatever the text matcher recognises. Matching
    one more spelling never converged -- six review rounds produced a new one each time
    (``/./``, a glued redirect, ``pushd``, an assignment, a conditional assignment).

    What binds a subprocess is the OS layer in :mod:`kiro_crew.sandbox`, so that is what
    is pinned: every leaf named here carries a HIDDEN (bind-masked in every mode) or
    READONLY (readable in-sandbox, never writable) disposition.

    Two residuals are stated rather than asserted away, because the OS layer does not
    close either and no text matcher can:

    * READONLY permits the READ, so an in-sandbox reader can still open
      ``security_policy.json``. Only its WRITE is refused, in every mode.
    * ``sel_hmac.key`` is VISIBLE, not fenced at all: in-sandbox code needs read AND
      write, so by the sandbox's own design it "stays on the tool gate alone". Only
      ``is_sensitive_path`` on the file-tool path refuses it; no bash-layer fence
      remains. Closing that properly
      means moving its in-sandbox reader behind the gateway so the leaf can become
      HIDDEN -- a sandbox change, not another regex.

    ``test_sandbox_governance_mask.py`` pins the dispositions themselves.
    """
    from kiro_crew import sandbox

    fenced = sandbox._CREW_HIDDEN_LEAVES + sandbox._CREW_READONLY_LEAVES

    def _carries(leaf: str, entries: "tuple[str, ...]") -> bool:
        return any(entry == leaf or entry.endswith("/" + leaf) for entry in entries)

    for leaf in ("live_target.json", "security_policy.json", ".env"):
        assert _carries(leaf, fenced), leaf

    # Pinned as VISIBLE on purpose: if it ever becomes maskable this assertion fails and
    # the residual above is what should be revisited.
    assert _carries("sel_hmac.key", sandbox._CREW_SANDBOX_VISIBLE_LEAVES)
    assert not _carries("sel_hmac.key", fenced)


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
