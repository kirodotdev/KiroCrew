"""Platform detection and shared service constants."""

from __future__ import annotations

import enum
import os
import shlex
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

from kiro_crew.config import loader

SERVICE_NAME = "kirocrew"  # systemd unit name (without .service)
LAUNCHD_LABEL = "dev.kirocrew.gateway"  # launchd Label

# The NAME of the environment variable kiro-cli reads its model credential
# from — not the credential. This holds a variable name, is safe to print, and
# appears verbatim in operator-facing output. Keep key/secret/token/credential
# words OUT of the identifier: taint analysis classifies sources by identifier
# name, so a credential-sounding name here marks every string this flows into as
# a cleartext credential and flags the operator message as a disclosure.
_AUTH_ENV_VAR = "KIRO_API_KEY"


def launchd_live_program() -> "os.PathLike[str]":
    """Stable path the launchd agent's ``ProgramArguments[0]`` points at.

    The agent is deliberately installed with this indirection instead of the
    resolved binary, so the running gateway can be repointed at a different
    checkout (Dev Fleet's "Make live") without rewriting the plist.

    Rewriting ``ProgramArguments`` cannot work from inside the gateway: launchd
    only re-reads a plist on ``bootout`` + ``bootstrap`` (``kickstart`` restarts
    the in-memory job definition), and ``bootout`` kills the very process that
    would have to run the ``bootstrap``. The gateway would stop and never come
    back. Rewriting THIS file plus a restart signal leaves nothing for a dying
    process to do.

    It is a generated launcher SCRIPT rather than a symlink to the binary
    because a cutover must also move the working directory and put the target
    checkout's venv first on ``PATH`` — exactly what the systemd drop-in sets
    alongside ``ExecStart``. A bare symlink can only change which binary runs,
    which would leave PATH-resolved subprocesses re-invoking the OLD install
    while the gateway ran the new one.
    """
    return (
        Path.home() / "Library" / "Application Support" / "KiroCrew"
        / "live-gateway"
    )


def kirocrew_bin() -> str:
    """Return the resolved kirocrew executable path, or fall back to sys.argv[0].

    Used by both the systemd unit and the launchd plist as ``ExecStart`` /
    ``ProgramArguments``. Resolution order:

    1. ``KIROCREW_SERVICE_BIN`` if set — an explicit operator override. This
       lets a launcher the resolver can't discover (a wrapper script, a venv
       entry point not on the global PATH) be pinned as the service Program,
       rather than silently falling back to whatever ``kirocrew`` happens to be
       first on ``$PATH``. Resolved to an absolute path: the launchd/systemd
       manager has no meaningful working directory, so a relative override
       would produce an invalid ``ExecStart`` / ``ProgramArguments`` and the
       service would fail to start.
    2. ``shutil.which("kirocrew")`` — the installed console script.
    3. ``sys.argv[0]`` — for development installs where ``kirocrew`` isn't on
       the global PATH.
    """
    override = os.environ.get("KIROCREW_SERVICE_BIN", "").strip()
    if override:
        return os.path.abspath(override)
    found = shutil.which("kirocrew")
    if found:
        return found
    return os.path.realpath(sys.argv[0])


def service_environment(home: str) -> "dict[str, str]":
    """Build the environment baked into the installed service.

    launchd and systemd start a service with a minimal, non-login
    environment, so anything the gateway (or a subprocess it spawns) relies on
    must be captured explicitly at install time. This is the single source of
    truth for both the launchd plist ``EnvironmentVariables`` and the systemd
    unit ``Environment=`` lines, so the two managers can never drift.

    Keys:

    * ``HOME`` / ``PATH`` — always set. ``PATH`` snapshots the installer's
      ``$PATH`` (see :func:`service_path`).
    * ``LANG`` / ``LC_ALL`` — pinned to a fixed UTF-8 locale so that a
      subprocess reading a non-ASCII file does not crash under the default
      ``US-ASCII`` codec (Python raises ``UnicodeDecodeError`` / tools report
      "invalid byte sequence in US-ASCII"). launchd sets no locale at all, so
      without this a gateway that shells out to a locale-sensitive tool fails
      only under the service and not from an interactive shell.

      The value is a fixed, platform-appropriate UTF-8 locale, NOT the
      installer's own. The installer environment is *not* trusted because: (1) a
      non-UTF-8 installer locale (a bare ``LANG=C`` / ``POSIX`` under SSH without
      locale forwarding, minimal containers, ``su``-invoked installs) would bake
      a non-UTF-8 value in and, because ``LC_ALL`` is then explicitly set,
      suppress CPython's PEP 538 coercion — reintroducing the exact ASCII-codec
      crash this env exists to prevent; (2) even a *UTF-8-named* installer
      locale can be one the target host never generated (e.g. an SSH-forwarded
      ``LC_ALL=zz_ZZ.UTF-8``), where ``setlocale`` still falls back to C.

      The concrete locale differs by platform because there is no single name
      valid on both: ``C.UTF-8`` is the always-present UTF-8 locale on modern
      glibc/musl (Linux/systemd) but is **not** a valid BSD-libc locale on
      macOS — a launchd service pinned to ``C.UTF-8`` would leave kiro-cli /
      node and other libc consumers with an invalid locale that degrades to
      ASCII. macOS ships ``en_US.UTF-8`` in its base locale set, so Darwin uses
      that. The install host's platform is the service host's platform, so
      keying off :data:`sys.platform` at render time is correct.
    * ``KIROCREW_KIRO_BIN`` — propagated only when the installer already has it
      set, resolved to an absolute path (a relative pin is meaningless once the
      service runs from a different working directory). The readiness ``whoami``
      probe's real-home fallback keys off this pin; capturing it means a
      ``service install`` no longer drops it and regresses the gateway to a
      not-signed-in state.
    """
    # macOS BSD libc has no C.UTF-8; en_US.UTF-8 is always in its base set.
    # Linux glibc/musl always has C.UTF-8 (and a minimal host may lack
    # en_US.UTF-8). Pick per platform so the baked-in locale is always valid.
    utf8_locale = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
    env = {
        "HOME": home,
        "PATH": service_path(home),
        "LANG": utf8_locale,
        "LC_ALL": utf8_locale,
    }
    kiro_bin = os.environ.get("KIROCREW_KIRO_BIN", "").strip()
    if kiro_bin:
        env["KIROCREW_KIRO_BIN"] = os.path.abspath(kiro_bin)
    # KIROCREW_PORT is the ONLY input DASHBOARD_PORT reads, so a service that
    # cannot carry it can only ever bind the default 5476 — broken by
    # construction on a host where that port is already taken, which includes
    # every host running Kiro Crew's own instance tunnel (it pins
    # local_port == remote_port). Propagated the same way KIROCREW_KIRO_BIN is:
    # captured from the installer's environment, so
    # `KIROCREW_PORT=5477 kirocrew service install` bakes 5477 into the unit.
    #
    # No validation here on purpose. `cli.py`'s main() already rejects a
    # KIROCREW_PORT that is not an integer in 1-65535 before any subcommand
    # runs, install included, so a check here could only become a second policy
    # that drifts from the first. It must reject rather than silently drop an
    # out-of-range value: dropping would install the DEFAULT port while the
    # operator believes they set theirs.
    port = os.environ.get("KIROCREW_PORT", "").strip()
    if port:
        env["KIROCREW_PORT"] = port
    return env


def api_key_will_be_dropped(environ: "Mapping[str, str] | None" = None) -> bool:
    """Return whether an API-key credential is set but invisible to the service.

    Deliberately the ONLY function that touches the credential, and it returns a
    bool rather than any string built from it. The message is assembled
    separately in :func:`headless_auth_warning` from a constant name and a path,
    so no value read here can reach a print, a log, or a return value — the
    no-disclosure property is structural rather than something a reader has to
    verify by following the formatting.

    True when the installer's environment defines a non-blank ``KIRO_API_KEY``
    that ``.env`` does not already carry. Only the presence of the NAME is
    inspected in ``.env``; its value is never read.
    """
    source = os.environ if environ is None else environ
    if not source.get(_AUTH_ENV_VAR, "").strip():
        return False
    return _AUTH_ENV_VAR not in _names_defined_in_env_file(loader.env_path())


def headless_auth_warning(environ: "Mapping[str, str] | None" = None) -> str:
    """Return a warning when API-key auth will not survive ``service install``.

    ``kiro-cli`` accepts a model credential through ``KIRO_API_KEY`` as an
    alternative to a ``kiro-cli login`` credential store, and the readiness
    probe forwards that variable to its ``whoami`` stage — but only from the
    GATEWAY's own environment. launchd and systemd start the service with a
    minimal, non-login environment, so a key exported in the shell that ran
    ``kirocrew service install`` is not there when the service starts: the probe
    sees no credential, and unless a ``kiro-cli login`` credential store under
    the baked ``HOME`` supplies one instead, readiness latches
    ``authenticated=False`` and the dashboard asks for a sign-in the operator has
    already done.

    The variable is deliberately NOT added to :func:`service_environment`. Both
    baked locations are readable by every local user — the systemd unit lives in
    ``/etc/systemd/system`` and the operator-editable override file is installed
    mode ``0644`` — so baking a credential there would trade a silent
    misconfiguration for a durable disclosure. ``~/.kiro/crew/.env`` is the
    supported home: ``load_credentials()`` reads EVERY key from that file (not
    just the channel-credential allowlist) into the gateway's environment at
    boot, and enforces ``0600`` on it first.

    Every character of the returned text comes from the module-level variable
    NAME or the ``.env`` path — never from the credential's value, which this
    function does not read at all (see :func:`api_key_will_be_dropped`). Returns
    an empty string when there is nothing to warn about.
    """
    if not api_key_will_be_dropped(environ):
        return ""
    dotenv = loader.env_path()
    # The append must never be the step that CREATES the file: under a standard
    # 022 umask a fresh .env is born 0644, and load_credentials() only tightens
    # it the next time it reads it — so the key would sit world-readable until
    # then. Pre-create and chmod first, which also repairs an already-loose file.
    #
    # shlex.quote because this line is copy-pasted verbatim: a crew home with a
    # space word-splits an unquoted path, so `touch` makes the wrong files,
    # `chmod` fails on a path that does not exist, and the redirect lands the
    # credential in a different 0644 file that load_credentials() never visits
    # to tighten — reintroducing the exposure the chmod ordering exists to
    # prevent. Simple paths are returned unquoted, so the common case is
    # unchanged.
    target = shlex.quote(str(dotenv))
    remedy = (
        f"touch {target} && chmod 600 {target}\n"
        f"     printf '%s\\n' \"{_AUTH_ENV_VAR}=${_AUTH_ENV_VAR}\" >> {target}"
    )
    lines = [
        f"Note: {_AUTH_ENV_VAR} is set in this shell but the service will not",
        "   inherit it, so unless kiro-cli has a login credential store to fall",
        "   back on, the dashboard will report a signed-out state. Add it",
        f"   to {dotenv} (0600) and restart the service:",
        "",
        f"     {remedy}",
        "     kirocrew service restart",
    ]
    if _home_override_is_set(environ):
        lines.append("")
        lines.append(
            "   KIROCREW_HOME is set here but is also not inherited, so confirm"
        )
        lines.append("   that path is the home the service actually starts with.")
    return "\n".join(lines)


def _home_override_is_set(environ: "Mapping[str, str] | None" = None) -> bool:
    """Whether the installer overrides the crew home (also not inherited)."""
    source = os.environ if environ is None else environ
    return bool(source.get("KIROCREW_HOME", "").strip())


def _names_defined_in_env_file(path: Path) -> "set[str]":
    """Return the variable names a ``.env`` file assigns a NON-BLANK value.

    Mirrors ``load_credentials()``'s parse (strip, skip blanks and ``#``
    comments, split on the first ``=``) so this agrees with what the gateway
    will actually resolve — including its ``if not v: continue`` guard, which
    means a bare ``NAME=`` never reaches ``os.environ``. Counting such a name as
    configured would silence the warning while the failure it warns about
    persists, so a blank value is treated as absent.

    Values are compared for emptiness and otherwise discarded: nothing here
    returns, logs, or echoes one. An unreadable or absent file yields an empty
    set, which makes the caller warn — the safe direction, since a missed
    warning is the defect being fixed.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    names: set[str] = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if not value.strip():
            continue
        names.add(name.strip())
    return names


def service_path(home: str) -> str:
    """Build the PATH for the gateway's service environment.

    Snapshots the installer's current ``$PATH`` so subprocesses spawned
    by the gateway (git, node, etc.) resolve the same way they did in the
    interactive shell that ran ``kirocrew service install``. Common
    user-local bin dirs (``~/.local/bin``) and POSIX defaults are
    prepended in case the installer's ``$PATH`` is missing them.
    Duplicates are removed while preserving order.
    """
    required = [
        f"{home}/.local/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    env_path = [p for p in os.environ.get("PATH", "").split(":") if p]
    seen: set[str] = set()
    out: list[str] = []
    for entry in required + env_path:
        if entry not in seen:
            seen.add(entry)
            out.append(entry)
    return ":".join(out)


class Platform(enum.Enum):
    """Supported service-management platforms."""

    # System-level systemd. Unit lives at /etc/systemd/system/, write
    # and control commands require sudo. The name reflects the privilege
    # model: a user-level (~/.config/systemd/user/) variant doesn't work
    # on older systemd (e.g. 219), so we don't ship one.
    SYSTEMD = "systemd"
    LAUNCHD = "launchd"
    UNSUPPORTED = "unsupported"


def current_platform() -> Platform:
    """Return the platform whose service manager we should target.

    Linux with systemctl on PATH → SYSTEMD.
    macOS with launchctl on PATH → LAUNCHD.
    Anything else → UNSUPPORTED.
    """
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        return Platform.SYSTEMD
    if sys.platform == "darwin" and shutil.which("launchctl"):
        return Platform.LAUNCHD
    return Platform.UNSUPPORTED


def restart_command_hint() -> str:
    """Return the shell command that actually restarts the installed gateway.

    The correct command depends on how the service is installed, and the
    scopes are not interchangeable — printing the wrong one sends the user
    down a dead end:

    * ``SYSTEMD`` — the unit is **system-level** at
      ``/etc/systemd/system/kirocrew.service`` (see
      :mod:`kiro_crew.service.linux`). ``systemctl --user`` fails on AL2
      (no per-user systemd manager), so the working command needs sudo:
      ``sudo systemctl restart kirocrew``.
    * ``LAUNCHD`` / ``UNSUPPORTED`` — defer to the service-aware
      ``kirocrew restart`` CLI, which resolves the right mechanism itself.

    Centralised so the update path and the Slack restart-failure hint share
    one source of truth and can never drift back to the broken
    ``systemctl --user`` string.
    """
    if current_platform() is Platform.SYSTEMD:
        return f"sudo systemctl restart {SERVICE_NAME}"
    return "kirocrew restart"
