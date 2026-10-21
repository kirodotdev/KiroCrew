"""Which kiro-cli is probed, with what environment, and what makes it launchable.

Candidate discovery delegates to :mod:`kiro_crew.kiro_cli`; the probe environment is a
fixed allowlist (plus the identity probe's credential and proxy keys); trust is "the CLI
runs", with the explicit ``KIROCREW_KIRO_BIN`` override pinned to its first observed
digest for the life of the process.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import sys
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.config.loader import CRED_KIRO_API_KEY
from kiro_crew.kiro_cli import find_kiro_cli_candidates, known_kiro_cli_dirs

_MAX_AUTH_EXECUTABLE_BYTES = 512 * 1024 * 1024


# Process-lifetime pins for explicit operator overrides. The prerequisite
# service records the canonical path + digest before any agent session starts;
# ACP spawn consumes the same pin so a later agent write cannot turn a stale
# readiness result into execution of replacement bytes.
_OPERATOR_OVERRIDE_ATTESTATIONS: dict[str, str] = {}

_PROBE_ENV_KEYS = frozenset(
    {
        "APPDATA",
        # Session bus + runtime dir: some Kiro CLI builds connect to the D-Bus
        # secret-service keyring at startup — even for ``--version`` — so the
        # probe must pass these through when the host sets them (e.g. AL2023,
        # where without them the CLI exits "Failed to connect to bus"). They are
        # only forwarded when present; hosts without a session bus (macOS, AL2,
        # headless) simply don't set them, so this is a no-op there.
        "DBUS_SESSION_BUS_ADDRESS",
        "HOME",
        "KIRO_NO_AUTO_UPDATE",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "LOGNAME",
        "PATH",
        "ProgramFiles",
        "PROGRAMFILES",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SystemRoot",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "USERPROFILE",
        "WINDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
    }
)

# Kiro CLI's OWN model credential, forwarded to the identity probe only.
#
# ``kiro-cli`` accepts an API key through the environment as an alternative to a
# ``kiro-cli login`` token store, and ``whoami`` reports "Authenticated with API
# key" only when it can see that variable. Filtered out, the probe answers "Not
# logged in" (exit 1) on a host where an ACP session authenticates fine, because
# ACP inherits the real environment — so dropping it latches
# ``authenticated=False`` while chat still works, which is the worst of both: the
# first-run gate demands a sign-in the user has already done, and every
# ``verified_ready`` caller (``/api/models``, usage polling, the destructive
# reruns) answers 503.
#
# SECURITY: the exposure delta is one probe's argv, not a new surface. The value
# reaches the same resolved binary this same probe already executes, in the same
# standard sandbox posture, against the same real home (see
# :meth:`KiroPrerequisiteService._run_auth_command`'s ``isolate_home=False`` note),
# on a fixed ``whoami`` argv. It is kept OUT of :data:`_PROBE_ENV_KEYS` because
# ``--version`` is the FIRST execution of a candidate that has not yet answered
# anything, and nothing about resolving a version needs a key.
#
# The CLI's exit status reports which credential kind is CONFIGURED, not whether
# the credential is accepted, so a stale or mistyped key reads as signed in. That
# is the same answer an ACP session acts on, which is the point — but it means
# presence of this variable must never become a shortcut that skips the probe.
#
# In a post-scrub Docker container the variable lives only in the data home's
# .env (the entrypoint removed it from the environ), so the identity probe also
# falls back to reading it from that file — see _audited_identity_probe.
_IDENTITY_PROBE_ENV_KEYS = frozenset({CRED_KIRO_API_KEY})

# Proxy configuration, forwarded to the ``whoami`` identity probe only.
#
# On a host that reaches the network only through an HTTP proxy, ``whoami``
# must talk to the IdP the same way the user's own shell does — filtered out,
# the child cannot connect, the probe answers "Not logged in", and the setup
# gate latches unauthenticated on a host where sign-in actually works. Both
# case spellings are listed because matching is exact on POSIX and the HTTP
# stacks disagree on which case they honour (curl-family reads the lowercase
# names, Rust/Python stacks read either), so forwarding only one case
# reproduces the bug on half of hosts. ALL_PROXY is included deliberately: a
# SOCKS-only host sets it INSTEAD of the scheme-specific pair, and curl and
# reqwest both honour it as the fallback, so omitting it keeps the exact same
# defect for that host class.
#
# These are kept OUT of :data:`_PROBE_ENV_KEYS` for the same reason as the
# credential above: a proxy URL can embed credentials, and ``--version`` is
# the FIRST execution of a candidate that has not yet answered anything — and
# nothing about resolving a version needs the network. ``whoami`` runs only
# after ``--version`` succeeds, against the same resolved binary an ACP
# session already runs with the full inherited environment, so the exposure
# delta is confined to a candidate that has already passed the version gate.
_IDENTITY_PROXY_ENV_KEYS = frozenset(
    {
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)


@dataclass(frozen=True)
class TrustedAcpExecutableSnapshot:
    """The resolved Kiro CLI path for one ACP launch.

    ``launch_path`` is ALWAYS the user's installed binary, launched in place —
    Kiro Crew never copies the CLI and runs the copy, because a multi-call Kiro
    CLI resolves its sibling subcommand executable relative to its own path.
    """

    launch_path: str


def _canonical_candidate(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _is_runnable_executable(path: str, platform_name: str | None = None) -> bool:
    """True if *path* resolves to an executable regular file on this platform.

    The single trust primitive for the "runs + valid login" model: a Kiro CLI
    that can be executed is eligible for sign-in and ACP launch, regardless of
    install source, owner, or fixed path.
    """

    return platform_compat.is_executable_file(
        _canonical_candidate(path),
        platform_name=platform_name,
    )


def _binary_sha256(path: str) -> str:
    """Hash one regular executable without following a final symlink."""

    canonical = _canonical_candidate(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(canonical, flags)
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_AUTH_EXECUTABLE_BYTES
        ):
            raise OSError("Kiro CLI candidate is not a bounded regular executable")
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def _register_operator_override_attestation(path: str, digest: str | None) -> None:
    """Remember the first gateway-start digest for one explicit override."""

    if not path or not digest:
        return
    key = os.path.normcase(_canonical_candidate(path))
    # First observation wins for the lifetime of this process. Reconstructing a
    # service after the file changes must not silently bless the new bytes.
    _OPERATOR_OVERRIDE_ATTESTATIONS.setdefault(key, digest)


def _acp_executable_is_runnable(
    executable: str,
    *,
    platform_name: str,
) -> bool:
    """Return whether ACP may launch *executable*.

    Trust is "it runs" — install source, owner, and path do not gate ACP launch,
    so a toolbox / Homebrew / self-updated CLI is accepted like any other
    (Kiro Crew is not the authority on where Kiro CLI is installed).

    A zero-byte candidate is still refused. An interrupted install or
    self-update can leave a truncated but executable ``kiro-cli``; exec'ing it
    dies without an ACP frame, surfacing as the same opaque
    "process exited (rc=None)" this module works to avoid. Rejecting it here
    turns that into a readable prerequisite error instead. (The retired copy
    path got this for free from the snapshot's ``fstat`` size guard.)
    """

    canonical = _canonical_candidate(executable)
    if not _is_runnable_executable(canonical, platform_name):
        return False
    try:
        return os.path.getsize(canonical) > 0
    except OSError:
        return False


def snapshot_trusted_acp_executable(
    executable: str,
    *,
    data_home: Path | None = None,
    platform_name: str | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> TrustedAcpExecutableSnapshot:
    """Return the user's installed Kiro CLI, to be launched IN PLACE.

    Kiro Crew always executes the Kiro CLI the user installed, at the path it was
    resolved from. It never copies the binary elsewhere and runs the copy.

    Kiro CLI 2.15+ is a **multi-call binary**: it dispatches subcommands by
    exec'ing a SIBLING executable (e.g. ``kiro-cli-chat``) that it locates
    relative to its own path — on macOS by finding ``.app/Contents/MacOS/`` in
    that path. Copying the binary into a flat private directory destroys the
    sibling layout, so every dispatch fails with ``No such file or directory (os
    error 2)`` and ACP dies at handshake with ``process exited (rc=None)``. The
    same breaks any launcher that resolves adjacent resources: a multiplexer
    dispatching on ``argv[0]``, a wrapper reading a sibling registry, or a
    self-updating install whose payload lives beside it.

    Copying the bytes into a private snapshot (sealed memfd on Linux, verified
    copy on macOS) would close the resolve-to-exec window in which the file could
    be swapped. That is deliberately **not** done: it
    defends against an attacker who already has write access to the user's own
    machine — a threat the rest of the product does not defend against either —
    and it breaks every multi-call and multiplexer install outright.

    Trust is "the CLI runs": install source, owner, and path do not gate launch,
    so a toolbox / Homebrew / self-updated Kiro CLI launches like any other.
    Raises ``ValueError`` when the candidate is not a runnable executable.
    """

    platform_name = platform_name or sys.platform
    del data_home, environ  # neither provenance nor a data home gates ACP launch
    if platform_name == "win32":
        return TrustedAcpExecutableSnapshot(launch_path=_canonical_candidate(executable))
    if not _acp_executable_is_runnable(executable, platform_name=platform_name):
        raise ValueError("Kiro CLI is not a runnable executable for ACP execution")

    # Launch the path the caller resolved, NOT its realpath: a multiplexer
    # launcher (e.g. ``~/.toolbox/bin/kiro-cli`` → ``toolbox-exec``) dispatches
    # on its own argv[0] basename, and a multi-call binary resolves its sibling
    # subcommand relative to the invoked path. Both break under the realpath.
    #
    # ``abspath``, NOT ``realpath``: it anchors a relative candidate (an operator
    # ``KIROCREW_KIRO_BIN=./kiro-cli`` resolves against the GATEWAY's cwd, but ACP
    # spawns with ``cwd=<session work_dir>``, so a relative launch path would die
    # with ENOENT there) while leaving symlinks intact, so the multiplexer and
    # sibling-layout properties above still hold.
    return TrustedAcpExecutableSnapshot(launch_path=os.path.abspath(executable))


def _existing_binary_digest(path: str) -> str | None:
    try:
        return _binary_sha256(path)
    except (OSError, ValueError):
        return None


def register_process_start_override_attestation(
    *,
    platform_name: str | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> tuple[str, str | None]:
    """Pin the first observed POSIX override bytes for this process."""

    active_platform = platform_name or sys.platform
    active_environ = environ if environ is not None else os.environ
    override = active_environ.get("KIROCREW_KIRO_BIN", "")
    if not override or active_platform == "win32":
        return "", None

    canonical = _canonical_candidate(override)
    key = os.path.normcase(canonical)
    digest = _OPERATOR_OVERRIDE_ATTESTATIONS.get(key)
    if digest is None:
        digest = _existing_binary_digest(canonical)
        _register_operator_override_attestation(canonical, digest)
    return canonical, _OPERATOR_OVERRIDE_ATTESTATIONS.get(key)


def _allowlisted_env(
    environ: MutableMapping[str, str],
    allowed: frozenset[str],
) -> dict[str, str]:
    """Filter *environ* down to *allowed*, honoring Windows' case-insensitive env.

    Thin wrapper binding this module's allowlists to the shared matching
    convention — exact on POSIX, case-folded on Windows. The rationale (why a
    literal membership test silently drops ``SystemRoot`` on Windows, killing
    probe and sign-in spawns with an unrelated-looking error, and why POSIX
    must stay exact) lives on :func:`platform_compat.env_key_allowed`.
    """

    return {
        key: value
        for key, value in environ.items()
        if platform_compat.env_key_allowed(key, allowed)
    }


def _probe_env(environ: MutableMapping[str, str], search_path: str) -> dict[str, str]:
    """Build a non-interactive probe environment from the fixed allowlist.

    Network-reachability configuration (TLS trust, the session bus where a CLI
    build needs it) passes through; proxy settings join only the ``whoami``
    stage via :func:`_identity_probe_env`, because a proxy URL can embed
    credentials and ``--version`` executes a candidate that has not yet
    answered anything. Ambient credentials (cloud, Slack, SSH agent,
    application secrets) never pass.
    """

    result = _allowlisted_env(environ, _PROBE_ENV_KEYS)
    result["PATH"] = search_path
    result["NO_COLOR"] = "1"
    result["TERM"] = "dumb"
    return result


def _identity_probe_env(
    environ: MutableMapping[str, str], probe_environment: dict[str, str]
) -> dict[str, str]:
    """Add Kiro CLI's own credential and proxy env to a probe environment.

    Separate from :func:`_probe_env` so only the identity probe carries the
    credential and the (possibly credentialed) proxy configuration; see
    :data:`_IDENTITY_PROBE_ENV_KEYS` and :data:`_IDENTITY_PROXY_ENV_KEYS` for
    why ``whoami`` needs them and why ``--version`` must not get them.
    """

    result = dict(probe_environment)
    result.update(_allowlisted_env(environ, _IDENTITY_PROXY_ENV_KEYS))
    result.update(_allowlisted_env(environ, _IDENTITY_PROBE_ENV_KEYS))
    return result


def _probe_filesystem_state(
    platform_name: str,
    home: Path,
    environ: MutableMapping[str, str],
) -> tuple[dict[str, str], list[str]]:
    """Collect path/candidate state on a worker thread."""

    separator = ";" if platform_name == "win32" else os.pathsep
    # Setup discovery matches ACP resolution on every OS: a runnable Kiro CLI is
    # recognized wherever it lives (PATH, Scripts, override, package-manager
    # dir), since trust is "the CLI runs". Windows is not restricted to the
    # Program Files tree — a winget/scoop/user install that ACP would launch must
    # also be recognized by setup, or the two disagree and a user who already has
    # the CLI is sent back to Kiro's setup page.
    search_path = separator.join(
        known_kiro_cli_dirs(
            platform_name,
            home,
            environ,
            include_inherited_path=True,
        )
    )
    probe_environment = _probe_env(environ, search_path)
    candidates = find_kiro_cli_candidates(
        platform_name,
        home,
        environ,
        include_inherited_path=True,
    )
    return probe_environment, candidates


# The system bin directories a login shell's PATH carries on Linux (Debian and
# Ubuntu's /etc/environment, Fedora's /etc/profile), where every distro package
# installs ``unzip``.
_SYSTEM_BIN_DIRS = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")


def _installer_lacks_unzip(platform_name: str, environ: MutableMapping[str, str]) -> bool:
    """Whether the Linux Kiro CLI installer would refuse this host for lacking ``unzip``.

    The same question the installer asks (``command -v unzip``), on the gateway's
    own PATH plus the system bin directories. The installer runs in the user's
    login shell, which always has those directories, while a gateway started by
    systemd or a desktop launcher can inherit a narrower PATH; searching only the
    gateway's PATH would report a host that has ``unzip`` as lacking it.

    Off-loop like the candidate scan: ``shutil.which`` stats every directory, and
    a stalled mount there must not freeze the event loop. Other platforms answer
    False, because the installer asks for ``unzip`` only on Linux.
    """

    if not platform_name.startswith("linux"):
        return False
    search = [part for part in environ.get("PATH", "").split(os.pathsep) if part]
    search.extend(_SYSTEM_BIN_DIRS)
    return shutil.which("unzip", path=os.pathsep.join(search)) is None
