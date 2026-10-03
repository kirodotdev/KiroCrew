"""``~/.ssh`` is withheld from agent subprocesses on a push-verdict-activated install.

An activated push-verdict installation judges the agent's own visible ``git push`` at the argv
floor, but an opaque subprocess (an interpreter shelling out to git from compiled code) presents
no publish source for the floor to judge. Outside the strict tier ``~/.ssh`` is otherwise
readable, so that subprocess authenticates over SSH and lands a commit the gate never saw.

The sandbox therefore hides ``~/.ssh`` from every agent-tier spawn once gating is activated --
keeping ``~/.ssh/known_hosts`` so legitimate host verification still works -- while the one
gateway-owned publish path stays exempt (``gateway_publish=True``) and keeps its SSH access. A
normal, non-activated install is unchanged: the agent keeps ``~/.ssh``.

Every negative assertion (the mask is NOT applied) is paired with a positive control taken the
same way, so a mask that silently stopped applying could not pass as "not activated".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import kiro_crew.sandbox as sandbox_mod
from kiro_crew.security import push_verdict

# ``_build_launcher_script`` builds the Linux namespace launcher and reads ``os.getuid``, which
# does not exist on Windows, so those cases run only on the POSIX lanes. The reduced-scope
# Windows lane skips them; the full POSIX suite exercises them.
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="_build_launcher_script uses POSIX-only os.getuid",
)


@pytest.fixture(autouse=True)
def _no_host_ssh_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_build_launcher_script`` asks the host ``ssh -V`` for accept-new support.

    The SSH mask decision does not depend on that answer, and a real ``ssh`` spawn is a host
    dependency this module is not about, so the probe is pinned and no binary runs.
    """
    monkeypatch.setattr(sandbox_mod, "_ssh_supports_accept_new", lambda: True)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


def _write_activation(home: Path, payload: object) -> Path:
    leaf = home / push_verdict.ACTIVATION_LEAF
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text(json.dumps(payload), encoding="utf-8")
    return leaf


def _hide_ssh_line(script: str) -> str:
    for line in script.splitlines():
        if line.startswith("HIDE_SSH = "):
            return line.strip()
    raise AssertionError("launcher script has no HIDE_SSH assignment")


def _env_prefixes(script: str) -> list[str]:
    """The scrubbed-env prefix list the launcher will apply, parsed from ``ENV_PREFIXES``.

    ``SSH_AUTH_SOCK``'s presence here is what withholds the forwarded agent socket from the
    child, so the socket-scrub assertions read this list rather than the ``~/.ssh`` dir hide.
    """
    for line in script.splitlines():
        if line.startswith("ENV_PREFIXES = "):
            return json.loads(line[len("ENV_PREFIXES = ") :])
    raise AssertionError("launcher script has no ENV_PREFIXES assignment")


# ── the activation reader the mask keys off, fail-closed like the argv floor ──


def test_helper_is_false_on_an_install_nobody_activated(home: Path) -> None:
    """No keystone leaf means nobody activated gating: the key stays readable."""
    assert sandbox_mod._push_verdict_masks_ssh() is False


def test_helper_is_true_when_the_keystone_says_activated(home: Path) -> None:
    _write_activation(home, {"enabled": True})
    assert sandbox_mod._push_verdict_masks_ssh() is True


def test_helper_is_false_on_an_explicit_operator_disable(home: Path) -> None:
    """A real JSON ``false`` is an operator's disable, so the key stays readable."""
    _write_activation(home, {"enabled": False})
    assert sandbox_mod._push_verdict_masks_ssh() is False


def test_helper_fails_closed_on_an_unreadable_leaf(home: Path) -> None:
    """A leaf that cannot be parsed is unknown activation, not "off": mask the key."""
    leaf = home / push_verdict.ACTIVATION_LEAF
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text("{not json", encoding="utf-8")
    assert sandbox_mod._push_verdict_masks_ssh() is True


def test_helper_fails_closed_on_a_corrupt_enabled_value(home: Path) -> None:
    """``{"enabled": 1}`` is a corrupted enable; the reader raises and the mask goes on."""
    _write_activation(home, {"enabled": 1})
    assert sandbox_mod._push_verdict_masks_ssh() is True


def test_helper_fails_closed_when_the_reader_raises_unexpectedly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any unexpected read error masks the key rather than leaving it readable."""

    def _boom() -> bool:
        raise RuntimeError("reader blew up")

    monkeypatch.setattr(push_verdict, "activation_enabled", _boom)
    assert sandbox_mod._push_verdict_masks_ssh() is True


# ── the Linux launcher: HIDE_SSH tracks the mask decision ──


@_POSIX_ONLY
@pytest.mark.parametrize("level", ["standard", "cc"])
def test_launcher_keeps_ssh_on_a_non_activated_install(
    monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: False)
    script = sandbox_mod._build_launcher_script(level)
    assert _hide_ssh_line(script) == "HIDE_SSH = False"


@_POSIX_ONLY
@pytest.mark.parametrize("level", ["standard", "cc"])
def test_launcher_hides_ssh_on_an_activated_install(
    monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: True)
    script = sandbox_mod._build_launcher_script(level)
    assert _hide_ssh_line(script) == "HIDE_SSH = True"


@_POSIX_ONLY
def test_launcher_strict_hides_ssh_even_when_not_activated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: False)
    script = sandbox_mod._build_launcher_script("strict")
    assert _hide_ssh_line(script) == "HIDE_SSH = True"


@_POSIX_ONLY
def test_launcher_gateway_publish_keeps_ssh_on_an_activated_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one gateway-owned publish keeps SSH even while every agent spawn loses it."""
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: True)
    agent = sandbox_mod._build_launcher_script("standard")
    gateway = sandbox_mod._build_launcher_script("standard", gateway_publish=True)
    assert _hide_ssh_line(agent) == "HIDE_SSH = True"
    assert _hide_ssh_line(gateway) == "HIDE_SSH = False"


@_POSIX_ONLY
def test_launcher_activated_mask_keeps_known_hosts_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mask hides keys but the launcher still copies ``known_hosts`` back in."""
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: True)
    script = sandbox_mod._build_launcher_script("standard")
    # The known_hosts carve is gated on HIDE_SSH, so its presence with HIDE_SSH True proves
    # host verification survives the activation mask. The launcher reads the resolved
    # ``SSH_KNOWN_HOSTS`` path and restores its bytes into the ``.ssh`` stand-in, so both
    # the read of that constant and the restore into the stand-in prove the carve is live.
    assert _hide_ssh_line(script) == "HIDE_SSH = True"
    assert "SSH_KNOWN_HOSTS" in script
    assert 'os.path.join(ssh_tmp.decode(), "known_hosts")' in script


# ── the macOS seatbelt profile: same gating, same known_hosts carve ──


def _ssh_denied(profile: str, home: Path) -> bool:
    return f'(deny file-write* (subpath "{home / ".ssh"}"))' in profile


def test_seatbelt_keeps_ssh_on_a_non_activated_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sandbox_mod, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    profile = sandbox_mod._build_seatbelt_profile("standard")
    assert not _ssh_denied(profile, tmp_path)


def test_seatbelt_hides_ssh_on_an_activated_install_keeping_known_hosts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sandbox_mod, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    profile = sandbox_mod._build_seatbelt_profile("standard")
    assert _ssh_denied(profile, tmp_path)
    # The read deny carves out known_hosts, so host verification still works.
    ssh_kh = tmp_path / ".ssh" / "known_hosts"
    assert f'(require-not (literal "{ssh_kh}"))' in profile


def test_seatbelt_gateway_publish_keeps_ssh_on_an_activated_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sandbox_mod, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    agent = sandbox_mod._build_seatbelt_profile("standard")
    gateway = sandbox_mod._build_seatbelt_profile("standard", gateway_publish=True)
    assert _ssh_denied(agent, tmp_path)
    assert not _ssh_denied(gateway, tmp_path)


def test_seatbelt_strict_hides_ssh_even_when_not_activated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sandbox_mod, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    profile = sandbox_mod._build_seatbelt_profile("strict")
    assert _ssh_denied(profile, tmp_path)


# ── the gateway publish path marks itself exempt ──


def test_gateway_publish_path_passes_gateway_publish_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway's ``_prepare_sandboxed_spawn`` routes git with ``gateway_publish=True``.

    The gateway publish is the single operation trusted to publish on an activated install, so
    it must keep SSH; this pins that its spawn prep marks itself exempt from the agent mask.
    """
    import asyncio

    from kiro_crew.dashboard.handlers import push_verdict as route

    captured: dict[str, object] = {}

    async def _fake_off_loop(fn):  # type: ignore[no-untyped-def]
        # ``fn`` is functools.partial(sandboxed_spawn_argv, argv, ...); read its bound kwargs
        # without running the real sandbox build.
        captured.update(fn.keywords)
        return (["wrapped"], {"env": "scrubbed"}, None)

    monkeypatch.setattr(route, "shielded_prepare_off_loop", _fake_off_loop)

    asyncio.run(route._prepare_sandboxed_spawn(["git", "status"], env={}, visible=("/wt",)))

    assert captured.get("gateway_publish") is True
    assert captured.get("mode") == "standard"


# ── the activation mask ALSO withholds the forwarded SSH agent socket ──
#
# ``forward_ssh_auth_sock=True`` re-admits ``SSH_AUTH_SOCK`` (``_agent_scrub_prefixes`` drops it
# from the default scrub set). The forwarded socket is an equivalent publish credential, so under
# the activation mask an agent subprocess must not keep it: the launcher's ``ENV_PREFIXES`` must
# scrub it back out. Every case here fixes ``forward_ssh_auth_sock=True`` so the socket would be
# KEPT but for the mask; a negative control with the same forwarding proves the scrub is the
# mask's doing, not the forward opt-in silently failing.


@_POSIX_ONLY
@pytest.mark.parametrize("level", ["standard", "cc"])
def test_launcher_activation_mask_scrubs_forwarded_socket(
    monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    """Activated + non-strict agent tier + forwarding on: the socket is withheld anyway."""
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: True)
    script = sandbox_mod._build_launcher_script(level, forward_ssh_auth_sock=True)
    assert "SSH_AUTH_SOCK" in _env_prefixes(script)


@_POSIX_ONLY
@pytest.mark.parametrize("level", ["standard", "cc"])
def test_launcher_non_activated_forwarding_keeps_socket(
    monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    """A NON-activated install with forwarding on keeps the socket usable (no regression)."""
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: False)
    script = sandbox_mod._build_launcher_script(level, forward_ssh_auth_sock=True)
    assert "SSH_AUTH_SOCK" not in _env_prefixes(script)


@_POSIX_ONLY
def test_launcher_gateway_publish_keeps_socket_on_activated_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway publish path keeps the socket to publish, even under the activation mask."""
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: True)
    script = sandbox_mod._build_launcher_script(
        "standard", forward_ssh_auth_sock=True, gateway_publish=True
    )
    assert "SSH_AUTH_SOCK" not in _env_prefixes(script)


@_POSIX_ONLY
def test_launcher_strict_socket_behavior_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STRICT with forwarding on keeps the socket usable: the activation-mask scrub is not strict.

    The strict tier hides ``~/.ssh`` but leaves the forwarded socket usable (the 7973-7979
    contract). Only the ACTIVATION mask gains the socket scrub, so a strict, non-activated spawn
    with forwarding on must still keep ``SSH_AUTH_SOCK`` out of the scrub list.
    """
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: False)
    script = sandbox_mod._build_launcher_script("strict", forward_ssh_auth_sock=True)
    assert "SSH_AUTH_SOCK" not in _env_prefixes(script)


# ── the activation mask ALSO withholds the HTTPS git-publish credential channel ──
#
# The SSH key/socket hide closes the SSH publish transport, but a cc/standard agent tier leaves
# the HTTPS credential channel readable: the GitHub-CLI helper dir ``.config/gh`` (absent from
# ``_CC_DIRS``/``_STANDARD_DIRS``, present only in strict), the git HTTPS credential stores
# ``.git-credentials``/``.netrc`` (``_CC_FILES`` entries the standard tier's empty file list
# leaves visible), and the ``GH_TOKEN``/``GITHUB_TOKEN`` env (no GitHub token var is in
# ``_SENSITIVE_ENV_PREFIXES``). An opaque subprocess would authenticate a ``git push`` over
# HTTPS through any of them and land a commit the argv floor never judged -- the same bypass
# class as the SSH one, through the HTTPS transport. Under the activation mask all of these are
# withheld from agent subprocesses; the gateway-owned publish keeps them. Every positive
# assertion here FAILS on the pre-fix code, so a mask that silently stopped applying cannot pass.


def _sensitive_dirs(script: str) -> list[str]:
    for line in script.splitlines():
        if line.startswith("SENSITIVE_DIRS = "):
            return json.loads(line[len("SENSITIVE_DIRS = ") :])
    raise AssertionError("launcher script has no SENSITIVE_DIRS assignment")


def _sensitive_files(script: str) -> list[str]:
    for line in script.splitlines():
        if line.startswith("SENSITIVE_FILES = "):
            return json.loads(line[len("SENSITIVE_FILES = ") :])
    raise AssertionError("launcher script has no SENSITIVE_FILES assignment")


def _gh_dir_hidden(script: str, home: Path) -> bool:
    return str(home / ".config" / "gh") in _sensitive_dirs(script)


def _https_files_hidden(script: str, home: Path) -> bool:
    files = _sensitive_files(script)
    return str(home / ".git-credentials") in files and str(home / ".netrc") in files


# ── the Linux launcher: HTTPS credential dirs/files + token env under the mask ──


@_POSIX_ONLY
@pytest.mark.parametrize("level", ["standard", "cc"])
def test_launcher_activation_mask_hides_gh_config_dir(
    monkeypatch: pytest.MonkeyPatch, level: str, home: Path, tmp_path: Path
) -> None:
    """Activated + non-strict agent tier: the GitHub-CLI helper dir is withheld."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: True)
    script = sandbox_mod._build_launcher_script(level)
    assert _gh_dir_hidden(script, tmp_path)


@_POSIX_ONLY
@pytest.mark.parametrize("level", ["standard", "cc"])
def test_launcher_activation_mask_hides_https_credential_files(
    monkeypatch: pytest.MonkeyPatch, level: str, tmp_path: Path
) -> None:
    """Activated + non-strict agent tier: ``.git-credentials`` and ``.netrc`` are withheld.

    ``standard`` is the sharp case: its base file list is empty, so a pass here proves the mask
    added the HTTPS stores rather than the tier already covering them.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: True)
    script = sandbox_mod._build_launcher_script(level)
    assert _https_files_hidden(script, tmp_path)


@_POSIX_ONLY
@pytest.mark.parametrize("level", ["standard", "cc"])
def test_launcher_activation_mask_scrubs_github_token_env(
    monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    """Activated + non-strict agent tier: the GitHub/HTTPS token env is scrubbed."""
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: True)
    script = sandbox_mod._build_launcher_script(level)
    prefixes = _env_prefixes(script)
    assert "GH_TOKEN" in prefixes
    assert "GITHUB_TOKEN" in prefixes


@_POSIX_ONLY
@pytest.mark.parametrize("level", ["standard", "cc"])
def test_launcher_activation_mask_hides_git_credential_cache_dir(
    monkeypatch: pytest.MonkeyPatch, level: str, tmp_path: Path
) -> None:
    """Activated agent tier: the git credential-cache daemon's socket dir is withheld.

    A command-line ``git -c credential.helper=cache push`` outranks the launcher's
    ``GIT_CONFIG_*`` empty-helper reset and re-adds the cache helper, which then
    authenticates over the socket under ``~/.cache/git/credential``. Masking that dir
    at the OS boundary leaves the re-added helper no socket to reach -- the fence the
    empty-helper reset alone cannot provide against a command-line override.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: True)
    script = sandbox_mod._build_launcher_script(level)
    assert str(tmp_path / ".cache" / "git" / "credential") in _sensitive_dirs(script)


@_POSIX_ONLY
@pytest.mark.parametrize("level", ["standard", "cc"])
def test_launcher_activation_mask_scrubs_dbus_session_bus(
    monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    """Activated agent tier: the session bus the libsecret/GCM helpers dial is scrubbed.

    A command-line ``git -c credential.helper=libsecret push`` outranks the empty-helper
    reset and re-adds the helper, which reaches the secret-service daemon over
    ``DBUS_SESSION_BUS_ADDRESS``. Removing it from the child's env leaves the re-added
    helper no bus to reach.
    """
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: True)
    script = sandbox_mod._build_launcher_script(level)
    assert "DBUS_SESSION_BUS_ADDRESS" in _env_prefixes(script)


@_POSIX_ONLY
@pytest.mark.parametrize("level", ["standard", "cc"])
def test_launcher_non_activated_keeps_credential_cache_and_bus(
    monkeypatch: pytest.MonkeyPatch, level: str, tmp_path: Path
) -> None:
    """A NON-activated install gains neither the cache-dir hide nor the bus scrub (no regression)."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: False)
    script = sandbox_mod._build_launcher_script(level)
    assert str(tmp_path / ".cache" / "git" / "credential") not in _sensitive_dirs(script)
    assert "DBUS_SESSION_BUS_ADDRESS" not in _env_prefixes(script)


@_POSIX_ONLY
@pytest.mark.parametrize("level", ["standard", "cc"])
def test_launcher_non_activated_keeps_https_credentials(
    monkeypatch: pytest.MonkeyPatch, level: str, tmp_path: Path
) -> None:
    """A NON-activated install does not gain the mask's HTTPS hide (no regression).

    ``.config/gh`` and the token env are added ONLY by the mask, so their absence here holds on
    every tier. ``.git-credentials``/``.netrc`` are ``_CC_FILES`` entries the cc tier hides at
    baseline, so the file-visibility control applies only to ``standard`` (empty base file list).
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: False)
    script = sandbox_mod._build_launcher_script(level)
    assert not _gh_dir_hidden(script, tmp_path)
    if level == "standard":
        assert not _https_files_hidden(script, tmp_path)
    prefixes = _env_prefixes(script)
    assert "GH_TOKEN" not in prefixes
    assert "GITHUB_TOKEN" not in prefixes


@_POSIX_ONLY
def test_launcher_gateway_publish_keeps_https_credentials_on_activated_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gateway publish path keeps the HTTPS channel to publish, even under the mask."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: True)
    script = sandbox_mod._build_launcher_script("standard", gateway_publish=True)
    assert not _gh_dir_hidden(script, tmp_path)
    assert not _https_files_hidden(script, tmp_path)
    prefixes = _env_prefixes(script)
    assert "GH_TOKEN" not in prefixes
    assert "GITHUB_TOKEN" not in prefixes


# ── the macOS seatbelt profile: same HTTPS credential hide under the mask ──


def _gh_denied(profile: str, home: Path) -> bool:
    return f'(deny file-read* (subpath "{home / ".config" / "gh"}"))' in profile


def _https_file_denied(profile: str, home: Path) -> bool:
    return (
        f'(deny file-read* (literal "{home / ".git-credentials"}"))' in profile
        and f'(deny file-read* (literal "{home / ".netrc"}"))' in profile
    )


@_POSIX_ONLY
def test_seatbelt_activation_mask_hides_https_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Activated + standard tier: the GitHub-CLI dir and HTTPS credential files are denied."""
    monkeypatch.setattr(sandbox_mod, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    profile = sandbox_mod._build_seatbelt_profile("standard")
    assert _gh_denied(profile, tmp_path)
    assert _https_file_denied(profile, tmp_path)


@_POSIX_ONLY
def test_seatbelt_non_activated_keeps_https_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A NON-activated standard install keeps the HTTPS channel readable (no regression)."""
    monkeypatch.setattr(sandbox_mod, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    profile = sandbox_mod._build_seatbelt_profile("standard")
    assert not _gh_denied(profile, tmp_path)
    assert not _https_file_denied(profile, tmp_path)


@_POSIX_ONLY
def test_seatbelt_gateway_publish_keeps_https_credentials_on_activated_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gateway publish keeps the HTTPS channel to publish, even under the mask."""
    monkeypatch.setattr(sandbox_mod, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    profile = sandbox_mod._build_seatbelt_profile("standard", gateway_publish=True)
    assert not _gh_denied(profile, tmp_path)
    assert not _https_file_denied(profile, tmp_path)


# ── the macOS seatbelt ``env -u`` set ALSO scrubs the HTTPS token env under the mask ──
#
# The macOS seatbelt profile hides the HTTPS credential FILES (``.config/gh`` /
# ``.git-credentials`` / ``.netrc``, asserted above), but the ``env -u`` key set built by
# ``_sandbox_env_scrub_keys`` / ``_sandbox_env_unset_args`` does NOT scrub the ``GH_TOKEN`` /
# ``GITHUB_TOKEN`` token env. Absent the fix, an activated macOS install keeps the token in the
# agent subprocess env, so an opaque subprocess authenticates a ``git push`` over HTTPS via the
# token and lands a commit the argv floor never judged -- the same bypass the Linux launcher's
# ``ENV_PREFIXES`` hunk closes, on macOS. Under the activation mask the token env is withheld;
# the gateway-owned publish keeps it. Each positive assertion here FAILS on the pre-fix code
# (the ``push_verdict_activation`` parameter did not exist / was never threaded), so a mask that
# silently stopped applying could not pass. Synthetic ``/opt/...`` token values only -- never a
# ``/home/<name>`` path the internal-content-scan flags.

_GH_TOKEN_VALUE = "/opt/synthetic/gh-token-placeholder"
_GITHUB_TOKEN_VALUE = "/opt/synthetic/github-token-placeholder"


@pytest.fixture
def github_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put ``GH_TOKEN`` / ``GITHUB_TOKEN`` in the live env with synthetic values.

    ``_sandbox_env_scrub_keys`` reads ``os.environ`` and reports only keys present there, so the
    token vars must exist for the scrub set to name them.
    """
    monkeypatch.setenv("GH_TOKEN", _GH_TOKEN_VALUE)
    monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN_VALUE)


@pytest.mark.parametrize("level", ["standard", "cc"])
def test_env_u_activation_mask_scrubs_github_token_env(github_token_env: None, level: str) -> None:
    """Activated + non-strict agent tier: the seatbelt ``env -u`` set drops the token env."""
    keys = sandbox_mod._sandbox_env_scrub_keys(
        level, strip_python_env=True, push_verdict_activation=True
    )
    assert "GH_TOKEN" in keys
    assert "GITHUB_TOKEN" in keys
    # And the rendered ``env -u`` flags carry them as ``-u`` pairs.
    unset = sandbox_mod._sandbox_env_unset_args(
        level, strip_python_env=True, push_verdict_activation=True
    )
    assert unset.count("GH_TOKEN") == 1
    assert unset.count("GITHUB_TOKEN") == 1


@pytest.mark.parametrize("level", ["standard", "cc"])
def test_env_u_non_activated_keeps_github_token_env(github_token_env: None, level: str) -> None:
    """A NON-activated install keeps the token env in the seatbelt spawn (no regression)."""
    keys = sandbox_mod._sandbox_env_scrub_keys(
        level, strip_python_env=True, push_verdict_activation=False
    )
    assert "GH_TOKEN" not in keys
    assert "GITHUB_TOKEN" not in keys


def test_env_u_gateway_publish_keeps_github_token_env(
    github_token_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway-owned publish keeps the token env even on an activated install.

    ``sandbox_exec_argv`` resolves the mask as ``not gateway_publish and
    _push_verdict_masks_ssh()``, so ``gateway_publish=True`` yields False regardless of
    activation and the token survives to publish. Modelled here by passing the resolved mask
    False, matching what the ``gateway_publish=True`` caller computes.
    """
    keys = sandbox_mod._sandbox_env_scrub_keys(
        "standard", strip_python_env=True, push_verdict_activation=False
    )
    assert "GH_TOKEN" not in keys
    assert "GITHUB_TOKEN" not in keys


def test_env_u_strict_token_behavior_unchanged(github_token_env: None) -> None:
    """STRICT non-activated does not gain the mask's token scrub (the mask is agent-tier only).

    The token env is not in ``_SENSITIVE_ENV_PREFIXES``, so a strict, non-activated spawn does
    not scrub it -- only the ACTIVATION mask adds it. This pins that the new scrub is gated on
    the mask, not on the strict tier.
    """
    keys = sandbox_mod._sandbox_env_scrub_keys(
        "strict", strip_python_env=True, push_verdict_activation=False
    )
    assert "GH_TOKEN" not in keys
    assert "GITHUB_TOKEN" not in keys


# ── the parent/Windows delegation scrub ALSO drops the HTTPS token env under the mask ──
#
# ``scrub_agent_subprocess_env`` is the AGENT enforcement point and is MANDATORY for Windows
# Kiro delegation, which has no POSIX ``env -u`` wrapper. Absent the fix it does not consult the
# activation mask, so a Windows-delegated (or otherwise parent-scrubbed) agent subprocess on an
# activated install keeps ``GH_TOKEN`` / ``GITHUB_TOKEN`` and can publish over HTTPS past the
# argv floor. Under the activation mask those are withheld; the gateway-owned publish and every
# non-activated caller keep them. Each positive assertion FAILS on the pre-fix code.


def _delegation_env() -> dict[str, str]:
    """A source env carrying the two token vars (synthetic ``/opt/...`` values)."""
    return {
        "PATH": "/opt/synthetic/bin",
        "GH_TOKEN": _GH_TOKEN_VALUE,
        "GITHUB_TOKEN": _GITHUB_TOKEN_VALUE,
    }


def test_delegation_activation_mask_scrubs_github_token_env() -> None:
    """Activated agent path: the parent/Windows delegation scrub drops the token env."""
    scrubbed = sandbox_mod.scrub_agent_subprocess_env(
        _delegation_env(), push_verdict_activation=True
    )
    assert "GH_TOKEN" not in scrubbed
    assert "GITHUB_TOKEN" not in scrubbed


def test_delegation_non_activated_keeps_github_token_env() -> None:
    """A NON-activated install keeps the token env in the delegated child (no regression)."""
    scrubbed = sandbox_mod.scrub_agent_subprocess_env(
        _delegation_env(), push_verdict_activation=False
    )
    assert scrubbed.get("GH_TOKEN") == _GH_TOKEN_VALUE
    assert scrubbed.get("GITHUB_TOKEN") == _GITHUB_TOKEN_VALUE


def test_delegation_gateway_publish_keeps_github_token_env() -> None:
    """The gateway-owned publish keeps the token env to publish, even on an activated install.

    The ACP agent callers resolve the mask off-loop and pass it in; the gateway publish path
    does not go through this agent scrub with the mask set, so its resolved value is False.
    Modelled by passing False, matching what a gateway-owned publish yields.
    """
    scrubbed = sandbox_mod.scrub_agent_subprocess_env(
        _delegation_env(), push_verdict_activation=False
    )
    assert scrubbed.get("GH_TOKEN") == _GH_TOKEN_VALUE
    assert scrubbed.get("GITHUB_TOKEN") == _GITHUB_TOKEN_VALUE


# ── F3: the activation mask ALSO neutralizes the git credential HELPER ──
#
# The file/env mask hides the HTTPS credential STORES, but a configured ``git config
# credential.helper`` (macOS keychain, Linux libsecret, git-credential-manager, or a
# ``store --file``) is a PROGRAM git runs to fetch a credential -- it sits outside every file
# and env mask, so an opaque agent ``git push`` over HTTPS still authenticates through it. Under
# the activation mask the helper is neutralized: an EMPTY ``credential.helper`` (git >= 2.9
# resets the helper list, and no helper is added after it) is set via ``GIT_CONFIG_*``, git's
# highest-precedence config source, inherited by the git processes git starts. The gateway-owned
# publish keeps its helper. Every positive assertion here FAILS on the pre-fix code (no
# ``credential.helper`` neutralization on the agent path), so a mask that silently stopped
# applying could not pass. Synthetic ``/opt/...`` values only.


def _launcher_flag(script: str) -> str:
    for line in script.splitlines():
        if line.startswith("PUSH_VERDICT_ACTIVATION = "):
            return line[len("PUSH_VERDICT_ACTIVATION = ") :].strip()
    raise AssertionError("launcher script has no PUSH_VERDICT_ACTIVATION assignment")


@_POSIX_ONLY
@pytest.mark.parametrize("level", ["standard", "cc"])
def test_launcher_activation_mask_arms_credential_helper_neutralization(
    monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    """Activated + non-strict agent tier: the child arms the credential.helper neutralization.

    The launcher's child sets an empty ``credential.helper`` (via ``GIT_CONFIG_*``) only when
    ``PUSH_VERDICT_ACTIVATION`` is True; assert the flag renders True so the block runs. The
    block itself always ships in the script text (guarded at runtime by the flag), so the flag
    IS the discriminator.
    """
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: True)
    script = sandbox_mod._build_launcher_script(level)
    assert _launcher_flag(script) == "True"
    # The neutralization block is present and keyed on the flag.
    assert 'os.environ["GIT_CONFIG_KEY_%d" % _gc_count] = "credential.helper"' in script
    assert "if PUSH_VERDICT_ACTIVATION:" in script


@_POSIX_ONLY
@pytest.mark.parametrize("level", ["standard", "cc"])
def test_launcher_non_activated_does_not_arm_credential_helper_neutralization(
    monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    """A NON-activated install renders the flag False, so the child never touches the helper."""
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: False)
    script = sandbox_mod._build_launcher_script(level)
    assert _launcher_flag(script) == "False"


@_POSIX_ONLY
def test_launcher_gateway_publish_does_not_arm_credential_helper_neutralization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway publish keeps its helper to publish, even on an activated install."""
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: True)
    script = sandbox_mod._build_launcher_script("standard", gateway_publish=True)
    assert _launcher_flag(script) == "False"


# ── the parent/Windows delegation scrub ALSO neutralizes the credential helper ──
#
# ``scrub_agent_subprocess_env`` is the AGENT enforcement point and is MANDATORY for Windows
# delegation, which has no ``env -u`` wrapper or launcher child to disable the helper. So it is
# disabled HERE by setting an empty ``credential.helper`` in the returned env.


def _empty_credential_helper_set(env: dict[str, str]) -> bool:
    """True iff *env*'s GIT_CONFIG_* pairs include an empty ``credential.helper``."""
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
    except (TypeError, ValueError):
        return False
    for idx in range(count):
        if env.get("GIT_CONFIG_KEY_%d" % idx) == "credential.helper":
            return env.get("GIT_CONFIG_VALUE_%d" % idx) == ""
    return False


def test_delegation_activation_mask_neutralizes_credential_helper() -> None:
    """Activated agent path: the delegated child gets an empty ``credential.helper``.

    Mutation check: on pre-fix code no ``GIT_CONFIG_*`` credential.helper pair is set, so
    ``_empty_credential_helper_set`` returns False and this assertion fails.
    """
    scrubbed = sandbox_mod.scrub_agent_subprocess_env(
        _delegation_env(), push_verdict_activation=True
    )
    assert _empty_credential_helper_set(scrubbed)
    # A neutralized helper pairs with a non-interactive prompt so a would-be prompt fails.
    assert scrubbed.get("GIT_TERMINAL_PROMPT") == "0"


def test_delegation_non_activated_leaves_credential_helper_alone() -> None:
    """A NON-activated install does not touch the helper (no regression)."""
    scrubbed = sandbox_mod.scrub_agent_subprocess_env(
        _delegation_env(), push_verdict_activation=False
    )
    assert not _empty_credential_helper_set(scrubbed)
    assert "GIT_CONFIG_COUNT" not in scrubbed


def test_delegation_credential_helper_neutralization_appends_to_existing_git_config() -> None:
    """An inherited ``GIT_CONFIG_*`` set is EXTENDED, not clobbered: the caller's own pair
    survives and the empty ``credential.helper`` is appended at the next index."""
    env = _delegation_env()
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "core.autocrlf"
    env["GIT_CONFIG_VALUE_0"] = "false"
    scrubbed = sandbox_mod.scrub_agent_subprocess_env(env, push_verdict_activation=True)
    assert scrubbed["GIT_CONFIG_COUNT"] == "2"
    # The pre-existing pair is preserved verbatim.
    assert scrubbed["GIT_CONFIG_KEY_0"] == "core.autocrlf"
    assert scrubbed["GIT_CONFIG_VALUE_0"] == "false"
    # And the credential.helper reset is appended at index 1.
    assert scrubbed["GIT_CONFIG_KEY_1"] == "credential.helper"
    assert scrubbed["GIT_CONFIG_VALUE_1"] == ""


# ── the macOS seatbelt argv ALSO neutralizes the credential helper under the mask ──
#
# ``sandbox_exec_argv`` prepends ``env`` assignments (after the ``-u`` flags so the scrub cannot
# drop them). Under the activation mask it adds the empty ``credential.helper`` via
# ``GIT_CONFIG_*`` assignments, disabling a keychain/GCM/store helper for the seatbelt spawn.


def _credential_helper_in_argv(argv: list[str]) -> bool:
    """True iff *argv* carries ``env`` assignments setting an empty ``credential.helper``."""
    key_idx = None
    for token in argv:
        if token.startswith("GIT_CONFIG_KEY_") and token.endswith("=credential.helper"):
            key_idx = token[len("GIT_CONFIG_KEY_") : -len("=credential.helper")]
    if key_idx is None:
        return False
    return f"GIT_CONFIG_VALUE_{key_idx}=" in argv


def test_seatbelt_argv_neutralizes_credential_helper_under_the_mask(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Activated + standard agent tier: the seatbelt argv sets an empty ``credential.helper``.

    Mutation check: on pre-fix code no such ``env`` assignment is added, so
    ``_credential_helper_in_argv`` returns False and this assertion fails.
    """
    monkeypatch.setattr(sandbox_mod, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    argv, cleanup = sandbox_mod.sandbox_exec_argv(["git", "push"], sandbox_level="standard")
    try:
        assert _credential_helper_in_argv(argv)
        assert "GIT_TERMINAL_PROMPT=0" in argv
    finally:
        if cleanup:
            import os as _os

            _os.unlink(cleanup)


def test_seatbelt_argv_non_activated_leaves_credential_helper_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A NON-activated install adds no credential.helper assignment (no regression)."""
    monkeypatch.setattr(sandbox_mod, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    argv, cleanup = sandbox_mod.sandbox_exec_argv(["git", "push"], sandbox_level="standard")
    try:
        assert not _credential_helper_in_argv(argv)
    finally:
        if cleanup:
            import os as _os

            _os.unlink(cleanup)


def test_seatbelt_argv_gateway_publish_keeps_credential_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gateway-owned publish keeps its helper to publish, even on an activated install."""
    monkeypatch.setattr(sandbox_mod, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    argv, cleanup = sandbox_mod.sandbox_exec_argv(
        ["git", "push"], sandbox_level="standard", gateway_publish=True
    )
    try:
        assert not _credential_helper_in_argv(argv)
    finally:
        if cleanup:
            import os as _os

            _os.unlink(cleanup)


def _credential_helper_index_in_argv(argv: list[str]) -> str | None:
    """Return the ``GIT_CONFIG_KEY_<n>`` index that carries ``credential.helper``, else None."""
    for token in argv:
        if token.startswith("GIT_CONFIG_KEY_") and token.endswith("=credential.helper"):
            return token[len("GIT_CONFIG_KEY_") : -len("=credential.helper")]
    return None


def test_seatbelt_argv_appends_to_the_childs_git_config_count_not_the_gateways(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The seatbelt argv reads ``GIT_CONFIG_COUNT`` from the CHILD's env, not the gateway's.

    A caller (dev_fleet ``_run_cmd`` -> ``sandboxed_spawn_argv``) hands a ``base_env`` carrying
    ``_GIT_ENV_NEUTRALIZERS`` (``GIT_CONFIG_COUNT=9``). The empty ``credential.helper`` must be
    appended at index 9 and the count bumped to 10, so the 9 inherited neutralizer pins
    (``core.fsmonitor``/``core.hooksPath``/...) survive. Reading the gateway's ``os.environ``
    (count 0) would set index 0 / count 1 and silently drop them.

    Mutation check: reverting the fix to ``os.environ.get("GIT_CONFIG_COUNT")`` makes the helper
    land at index 0 with count 1, so both assertions below fail.
    """
    monkeypatch.setattr(sandbox_mod, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    # The gateway's own env has NO GIT_CONFIG_COUNT (would read as 0) -- prove the value comes
    # from child_env, not here.
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    child_env = {"GIT_CONFIG_COUNT": "9"}
    argv, cleanup = sandbox_mod.sandbox_exec_argv(
        ["git", "push"], sandbox_level="standard", child_env=child_env
    )
    try:
        assert _credential_helper_index_in_argv(argv) == "9"
        assert "GIT_CONFIG_COUNT=10" in argv
    finally:
        if cleanup:
            import os as _os

            _os.unlink(cleanup)


def test_seatbelt_argv_falls_back_to_os_environ_count_without_a_child_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no ``child_env`` threaded, the count still comes from ``os.environ`` (legacy callers).

    Preserves the pre-fix behaviour for any caller that does not pass ``child_env``: a gateway
    env carrying ``GIT_CONFIG_COUNT=3`` appends the helper at index 3.
    """
    monkeypatch.setattr(sandbox_mod, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "3")
    argv, cleanup = sandbox_mod.sandbox_exec_argv(["git", "push"], sandbox_level="standard")
    try:
        assert _credential_helper_index_in_argv(argv) == "3"
        assert "GIT_CONFIG_COUNT=4" in argv
    finally:
        if cleanup:
            import os as _os

            _os.unlink(cleanup)


# ── F2: the activation mask ALSO disables the child's SSH AGENT (Windows OpenSSH pipe) ──
#
# The POSIX SSH mask (the ``SSH_AUTH_SOCK`` env scrub plus launcher/OS filesystem masks) is
# POSIX-only. The Windows delegation path (``scrub_agent_subprocess_env``) has NO launcher and
# NO OS sandbox, and Windows OpenSSH holds its key behind a FIXED named pipe
# (backslash-backslash-dot-backslash-pipe-backslash-openssh-ssh-agent) the client consults by
# default regardless of ``SSH_AUTH_SOCK`` -- so an env scrub alone cannot remove it, and an
# opaque agent ``git push`` over SSH on an activated Windows install authenticates through the
# pipe past the argv floor. Under the activation mask the child's ssh agent is DISABLED via
# ``GIT_SSH_COMMAND`` with ``-o IdentityAgent=none``: per ssh_config(5) ``IdentityAgent``
# OVERRIDES ``SSH_AUTH_SOCK`` and the value ``none`` DISABLES the use of any authentication
# agent, so the child's ssh consults neither a socket nor the Windows pipe -- transport- and
# platform-agnostic. The gateway-owned publish keeps agent access. Each positive assertion here
# FAILS on the pre-fix code (no ``GIT_SSH_COMMAND`` set on the agent path).


def _identity_agent_disabled(env: dict[str, str]) -> bool:
    """True iff *env*'s ``GIT_SSH_COMMAND`` suppresses EVERY ssh identity (agent AND disk keys).

    The activation mask must remove disk identities too, not only the agent: on the no-sandbox
    Windows delegation path OpenSSH loads default ``~/.ssh/id_*`` keys and honours a user
    ``~/.ssh/config`` directly, so disabling only the agent still authenticates from a disk key.
    """
    cmd = env.get("GIT_SSH_COMMAND", "")
    return all(
        opt in cmd
        for opt in (
            "-F none",
            "-o IdentitiesOnly=yes",
            "-o IdentityFile=none",
            "-o IdentityAgent=none",
        )
    )


def test_delegation_activation_mask_disables_ssh_agent() -> None:
    """Activated agent path: the delegated child's ssh has EVERY identity suppressed.

    Mutation check: on pre-fix code no ``GIT_SSH_COMMAND`` is set (or only the agent is
    disabled), so ``_identity_agent_disabled`` returns False and this assertion fails.
    """
    scrubbed = sandbox_mod.scrub_agent_subprocess_env(
        _delegation_env(), push_verdict_activation=True
    )
    assert _identity_agent_disabled(scrubbed)
    # Defaults to a plain ``ssh`` base when none is inherited (scrub_env drops GIT_SSH_COMMAND),
    # then suppresses config, disk identities, and the agent.
    assert (
        scrubbed["GIT_SSH_COMMAND"]
        == "ssh -F none -o IdentitiesOnly=yes -o IdentityFile=none -o IdentityAgent=none"
    )


def test_delegation_activation_mask_disables_disk_identities_not_only_the_agent() -> None:
    """Codex F1: the mask must suppress default DISK keys + ssh config, not only the agent.

    On the no-sandbox Windows delegation path OpenSSH loads ``~/.ssh/id_*`` and honours a user
    ``~/.ssh/config`` directly, so ``IdentityAgent=none`` alone still authenticates from a
    passphrase-less disk key. The mask now also passes ``-F none`` (ignore user ssh config),
    ``IdentitiesOnly=yes`` (only command-line identities, of which there are none), and
    ``IdentityFile=none`` (disable default identity files). Mutation check: dropping any one of
    these from the production ``GIT_SSH_COMMAND`` fails an assertion here.
    """
    scrubbed = sandbox_mod.scrub_agent_subprocess_env(
        _delegation_env(), push_verdict_activation=True
    )
    cmd = scrubbed["GIT_SSH_COMMAND"]
    assert "-F none" in cmd, "user ssh config not ignored -> a config IdentityFile can re-add a key"
    assert "-o IdentitiesOnly=yes" in cmd, "default ~/.ssh/id_* set not suppressed"
    assert "-o IdentityFile=none" in cmd, "default identity files not disabled"
    assert "-o IdentityAgent=none" in cmd, "agent/pipe not disabled"


def test_delegation_non_activated_leaves_ssh_agent_alone() -> None:
    """A NON-activated install does not set ``GIT_SSH_COMMAND`` (no regression)."""
    scrubbed = sandbox_mod.scrub_agent_subprocess_env(
        _delegation_env(), push_verdict_activation=False
    )
    assert "GIT_SSH_COMMAND" not in scrubbed


def test_delegation_gateway_publish_keeps_ssh_agent() -> None:
    """The gateway-owned publish keeps agent access (mask resolves False), so no disable is set.

    The ACP agent callers resolve the mask off-loop and pass it in; the gateway publish path
    resolves it False, so its child keeps the agent to authenticate the publish. Modelled by
    passing False, matching what a gateway-owned publish yields.
    """
    scrubbed = sandbox_mod.scrub_agent_subprocess_env(
        _delegation_env(), push_verdict_activation=False
    )
    assert "GIT_SSH_COMMAND" not in scrubbed


def test_delegation_ssh_agent_disable_discards_an_inherited_ssh_command() -> None:
    """GPT c403: an inherited ``GIT_SSH_COMMAND`` is DISCARDED, not preserved.

    ``GIT_SSH_COMMAND`` is not in ``_SENSITIVE_ENV_PREFIXES``, so a value an app cron set via
    ``_extra_env`` survives ``scrub_env`` into the source env. Preserving that wrapper and only
    APPENDING ``-o`` hardening cannot bind it: git hands ``GIT_SSH_COMMAND`` to a shell, and a
    hostile wrapper need not forward the appended options to a real ssh -- it can invoke its own
    ssh with its own identity, authenticating an unjudged push. The mask therefore sets a TRUSTED,
    literal ``ssh``. Mutation check: re-admitting ``src``'s value (the pre-fix behaviour) leaves
    ``/opt/synthetic/bin/ssh`` in the command and fails this assertion.
    """
    env = _delegation_env()
    env["GIT_SSH_COMMAND"] = "/opt/synthetic/bin/ssh -o SomeOption=1"
    scrubbed = sandbox_mod.scrub_agent_subprocess_env(env, push_verdict_activation=True)
    assert (
        scrubbed["GIT_SSH_COMMAND"]
        == "ssh -F none -o IdentitiesOnly=yes -o IdentityFile=none -o IdentityAgent=none"
    )
    assert "/opt/synthetic/bin/ssh" not in scrubbed["GIT_SSH_COMMAND"]
    assert "SomeOption" not in scrubbed["GIT_SSH_COMMAND"]


# ── GPT c412: the Windows delegation path fails closed under activation ──
#
# On Windows there is NO OS sandbox and NO ``env -u`` launcher, so the credential mask is
# ONLY the env dict a caller passes as the child's environment -- which lives inside the
# child's own, mutable environment. A delegated agent can clear ``GIT_SSH_COMMAND`` /
# ``GIT_CONFIG_*`` at runtime before it runs git, after which OpenSSH re-attaches a disk key
# or the fixed agent pipe and an opaque ``git push`` authenticates past the gate. With no
# enforcement point outside the child's control, the spawn must be REFUSED, not launched
# with a mask the child can remove. ``gateway_publish`` stays exempt.


def test_windows_delegation_fails_closed_when_activation_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Activated + win32 delegation -> ``SandboxUnavailableError`` (kind no_backend).

    Mutation check: the pre-fix path returned ``(argv, None)`` here, launching the agent with
    a child-removable env mask. Restoring that return makes this ``pytest.raises`` fail.
    """
    from kiro_crew.sandbox import SandboxUnavailableError

    monkeypatch.setattr(sandbox_mod.sys, "platform", "win32")
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: True)
    with pytest.raises(SandboxUnavailableError) as excinfo:
        sandbox_mod._delegate_to_kiro_internal_sandbox(["kiro-cli.exe", "acp"], "standard")
    assert excinfo.value.kind == "no_backend"


def test_windows_delegation_still_delegates_when_activation_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NON-activated win32 delegation is unchanged: proceeds env-scrubbed (no regression)."""
    monkeypatch.setattr(sandbox_mod.sys, "platform", "win32")
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: False)
    argv = ["kiro-cli.exe", "acp"]
    wrapped, launcher = sandbox_mod._delegate_to_kiro_internal_sandbox(argv, "standard")
    assert wrapped == argv
    assert launcher is None


def test_windows_delegation_gateway_publish_is_exempt_under_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The credential-exempt gateway publish keeps delegating on win32 even when activated.

    ``gateway_publish`` resolves the mask False by design (it keeps full credentials to
    authenticate the publish), so there is nothing to withhold and no reason to refuse.
    """
    monkeypatch.setattr(sandbox_mod.sys, "platform", "win32")
    monkeypatch.setattr(sandbox_mod, "_push_verdict_masks_ssh", lambda: True)
    argv = ["kiro-cli.exe", "acp"]
    wrapped, launcher = sandbox_mod._delegate_to_kiro_internal_sandbox(
        argv, "standard", gateway_publish=True
    )
    assert wrapped == argv
    assert launcher is None
