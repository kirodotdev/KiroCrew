"""SSH_AUTH_SOCK forward opt-in (issue #8104).

The agent sandbox scrubs SSH_AUTH_SOCK by default, which breaks git commit
signing with a passphrase-protected SSH key held by the operator's ssh-agent
(the agent socket is the only reachable path to the key). This module pins the
opt-in as a MECHANICAL property.

The forward decision is a boolean resolved ONCE off the event loop on the agent
spawn path (via _forward_ssh_auth_sock) and threaded into the scrub sites as an
explicit parameter (never read from config inside the generic launchers). Two
consequences this module pins:
  - config I/O stays off the loop (the scrubbers take a bool, they do not read
    config);
  - the forward is scoped to agent spawns: the generic launcher builders and
    scrub_env default the flag off, so a non-agent caller (a third-party app
    openCommand going through the generic wrap_argv launcher, a
    sandboxed_spawn_argv spawn) never re-admits the socket.

Every negative assertion (a key removed / not forwarded) is paired with a
positive control taken with the SAME method, so a scrub that silently stopped
working could not pass as an SSH forward.
"""

from __future__ import annotations

import sys

import pytest

import kiro_crew.sandbox as sb

# --- _agent_scrub_prefixes: the shared prefix filter (takes an explicit bool) ---


def test_agent_scrub_prefixes_off_is_identity():
    """forward=False: the prefix list is returned unchanged."""
    base = list(sb._SENSITIVE_ENV_PREFIXES)
    assert sb._agent_scrub_prefixes(base, False) == base
    # Control: the socket key IS present in the unchanged list.
    assert "SSH_AUTH_SOCK" in sb._agent_scrub_prefixes(base, False)


def test_agent_scrub_prefixes_on_drops_only_the_socket():
    """forward=True: SSH_AUTH_SOCK is dropped and NOTHING else is."""
    base = list(sb._SENSITIVE_ENV_PREFIXES)
    out = sb._agent_scrub_prefixes(base, True)
    # Negative: the socket prefix is gone.
    assert "SSH_AUTH_SOCK" not in out
    # Control (same method): every other credential prefix survives, so the
    # opt-in cannot have widened into a general passthrough.
    for prefix in base:
        if prefix != "SSH_AUTH_SOCK":
            assert prefix in out, prefix
    assert set(base) - set(out) == {"SSH_AUTH_SOCK"}


def test_shared_constant_never_mutated():
    """The opt-in must never mutate the shared _SENSITIVE_ENV_PREFIXES constant.

    mcp_gateway.manager imports it to refuse credential keys in MCP declared-env
    forwarding; mutating it would leak the socket into pooled MCP backends.
    """
    before = list(sb._SENSITIVE_ENV_PREFIXES)
    sb._agent_scrub_prefixes(list(sb._SENSITIVE_ENV_PREFIXES), True)
    assert list(sb._SENSITIVE_ENV_PREFIXES) == before
    assert "SSH_AUTH_SOCK" in sb._SENSITIVE_ENV_PREFIXES


# --- Site 3: parent-side scrub_agent_subprocess_env (ACP agent enforcement) ---


def _env_with_socket() -> dict[str, str]:
    return {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "AWS_SECRET_ACCESS_KEY": "sk",
    }


def test_scrub_env_always_strips_socket_regardless_of_opt_in():
    """The GENERIC scrub_env has no opt-in knob and always drops SSH_AUTH_SOCK.

    scrub_env serves non-agent callers (tailscale host children, every
    sandboxed_spawn_argv spawn), which must keep the socket scrubbed. The opt-in
    lives one layer up in scrub_agent_subprocess_env, so the forward's blast
    radius is exactly the agent child.
    """
    out = sb.scrub_env(_env_with_socket())
    # Negative: the generic scrubber removes the socket.
    assert "SSH_AUTH_SOCK" not in out
    # Control: a benign key survives, proving the scrub ran.
    assert out["PATH"] == "/usr/bin"


def test_scrub_agent_subprocess_env_honours_explicit_flag():
    """The ACP parent enforcement point forwards the socket when the resolved
    boolean is True and strips it when False -- and takes the flag as a
    parameter (no config read on this path)."""
    stripped = sb.scrub_agent_subprocess_env(_env_with_socket(), forward_ssh_auth_sock=False)
    assert "SSH_AUTH_SOCK" not in stripped
    kept = sb.scrub_agent_subprocess_env(_env_with_socket(), forward_ssh_auth_sock=True)
    # Positive: the socket is forwarded with its exact value to the agent child.
    assert kept["SSH_AUTH_SOCK"] == "/tmp/agent.sock"
    # Control: a genuine credential is STILL removed, so the forward is scoped to
    # the socket and did not disable the scrub.
    assert "AWS_SECRET_ACCESS_KEY" not in kept


def test_scrub_agent_subprocess_env_defaults_to_scrubbing():
    """Default (no flag passed): the socket is scrubbed, so a caller that does
    not opt in is unaffected."""
    out = sb.scrub_agent_subprocess_env(_env_with_socket())
    assert "SSH_AUTH_SOCK" not in out


# --- Site 2: seatbelt env -u renderer (macOS, the reporter's path) ---


def test_seatbelt_scrub_keys_off_lists_socket(monkeypatch):
    """forward=False (default): SSH_AUTH_SOCK is among the env -u keys."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "sk")
    keys = sb._sandbox_env_scrub_keys("strict", strip_python_env=True)
    assert "SSH_AUTH_SOCK" in keys
    # Control: another sensitive key is also scrubbed by the same call.
    assert "AWS_SECRET_ACCESS_KEY" in keys


def test_seatbelt_scrub_keys_on_omits_socket_only(monkeypatch):
    """forward=True: SSH_AUTH_SOCK leaves the env -u set, others stay."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "sk")
    keys = sb._sandbox_env_scrub_keys("strict", strip_python_env=True, forward_ssh_auth_sock=True)
    # Positive: the socket is no longer unset, so it reaches the child.
    assert "SSH_AUTH_SOCK" not in keys
    # Control (same call): a real credential is STILL unset.
    assert "AWS_SECRET_ACCESS_KEY" in keys


# --- Site 1: Linux namespace launcher script (POSIX-only) ---

# _build_launcher_script builds the Linux user-namespace launcher and calls
# os.getuid(), which does not exist on Windows; the launcher never runs there.
_linux_only = pytest.mark.skipif(
    sys.platform == "win32", reason="Linux namespace launcher is POSIX-only (os.getuid)"
)


@_linux_only
def test_launcher_off_scrubs_socket():
    """forward=False (default): the launcher lists SSH_AUTH_SOCK to delete."""
    script = sb._build_launcher_script("strict", strip_python_env=True)
    # Negative + control in one string: the socket prefix AND another credential
    # prefix both appear in the launcher's ENV_PREFIXES payload.
    assert "SSH_AUTH_SOCK" in script
    assert "AWS_SECRET" in script


@_linux_only
def test_launcher_on_keeps_socket_only():
    """forward=True: the launcher no longer lists SSH_AUTH_SOCK, but still lists
    the other credential prefixes."""
    script = sb._build_launcher_script("strict", strip_python_env=True, forward_ssh_auth_sock=True)
    # Positive: socket prefix dropped from the delete set.
    assert '"SSH_AUTH_SOCK"' not in script
    # Control (same script): a real credential prefix is STILL in the delete set,
    # proving the launcher scrub itself is intact.
    assert "AWS_SECRET" in script


# --- Scope: the forward is agent-only; generic launchers keep scrubbing ---


@_linux_only
def test_generic_launcher_default_never_forwards_socket():
    """A generic (non-agent) launcher build defaults forward_ssh_auth_sock=False,
    so an app openCommand / sandboxed_spawn_argv spawn through the same builder
    keeps scrubbing the socket even if an operator opted in for AGENT spawns.

    This is the F1 scoping guarantee: the opt-in is threaded from the agent path
    only, never read from config inside the generic builder.
    """
    # No forward_ssh_auth_sock argument == the generic caller's default.
    script = sb._build_launcher_script("strict", strip_python_env=True)
    assert "SSH_AUTH_SOCK" in script  # socket still scrubbed for generic callers


# --- Platform gate: the forward is a no-op where the socket concept is absent ---


def test_forward_is_noop_on_windows(monkeypatch):
    """On Windows there is no SSH_AUTH_SOCK (Win32 OpenSSH uses a named pipe),
    so _forward_ssh_auth_sock returns False even when the config flag is set.

    The opt-in must not be OFFERED on a platform where it cannot work. Pins the
    platform behaviour as a property.
    """
    monkeypatch.setattr(sb.platform_compat, "IS_WINDOWS", True)

    class _Agent:
        sandbox_forward_ssh_auth_sock = True

    class _Cfg:
        agent = _Agent()

    from kiro_crew.config import loader as _loader

    monkeypatch.setattr(_loader.KiroCrewConfig, "load", classmethod(lambda cls: _Cfg()))
    # Negative: on Windows the forward is refused despite the flag being True.
    assert sb._forward_ssh_auth_sock() is False
    # Control (same reader, non-Windows): with the guard off, the True flag is
    # honoured, proving the reader itself works and only the platform gate
    # suppressed it above.
    monkeypatch.setattr(sb.platform_compat, "IS_WINDOWS", False)
    assert sb._forward_ssh_auth_sock() is True
