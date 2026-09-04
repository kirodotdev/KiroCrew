"""Launching the Kiro Crew backend headless, and knowing when it is ready.

The deployed unit is Kiro Crew's own backend, run in *dashboard mode*, on
loopback, with no interface served (`CHORUS-DOC.md` 4.4, D2). Dashboard mode is
required and `--no-dashboard` is wrong: the smaller ``_init_api_server`` it
starts has neither the chat endpoints nor the slot registry this design calls,
and it quiets nothing, so headless here means "no interface exposed", not
"--no-dashboard".

Launch decisions, all from the contract and design:

- Bind ``common.BACKEND_HOST`` (127.0.0.1, not configurable). The gateway binds
  loopback by default; we also pass it explicitly so the intent is recorded.
- ``--no-crons``. Arming the scheduler fires any *overdue* job immediately, so a
  freshly deployed crew would run a stale job on boot.
- Supply no channel credentials. Every transport is gated on credentials as
  well as its flag, so supplying none keeps them all off without depending on a
  flag. We also strip any channel credential that leaked into the task env.
- ``telemetry.beacon_enabled=false``.
- The boot update check cannot be disabled by config (R4). We do not fight it;
  it is recorded as a known outbound request in the report, not suppressed here.

Readiness means the port answers AND the boot secret file exists. Process-alive
is not ready, and this check deliberately proves no more than that: a present
model key is not a working one, so a container can be "ready" here and still
fail every turn on an invalid key. We do not claim otherwise.

VERIFIED 2026-09-02 against the installed Kiro Crew (0.3.0) by booting a real
backend through this code on an isolated KIROCREW_HOME (port 8803): the argv
``python -m kiro_crew gateway --no-crons`` boots dashboard mode; ``KIROCREW_PORT``
sets the port (dashboard/urls.py:116), ``KIROCREW_BIND`` pins the address
(urls.py:208), ``KIROCREW_TELEMETRY_DISABLED`` disables the beacon (beacon.py),
the secret lands at ``<KIROCREW_HOME>/run/gateway-<port>.secret`` exactly where
``common.secret_path`` looks, and the listener came up on 127.0.0.1 only.

STILL NOT VERIFIABLE FROM HERE: the backend refuses turns with 503
``kiro_prerequisite_required`` until Kiro CLI is signed in, and this host has no
usable sandbox backend (``unshare(CLONE_NEWUSER)`` EPERM), so a real kiro-cli
worker could not be spawned -- the escaped-worker teardown is proven only by the
topology tests, not against a live worker. The boot update check also runs
regardless of config (R4): on this git install it only "notifies", but a
pip-installed container image may make an outbound probe.
"""

from __future__ import annotations

import os
import socket
import sys
import time
from collections.abc import Mapping, Sequence

from .. import common
from ..common import Settings
from .process import ProcessGroup, spawn_process_group

# --- Invocation (spelling-sensitive, see module docstring) ------------------
BACKEND_LAUNCHER: tuple[str, ...] = (sys.executable, "-m", "kiro_crew")
GATEWAY_SUBCOMMAND: str = "gateway"
FLAG_NO_CRONS: str = "--no-crons"
# The flags that turn the dashboard off. We must pass NEITHER. Verified against
# the real CLI (cli.py:539): dashboard-off is driven by --slack-only, and there
# is no --no-dashboard flag on the gateway parser at all -- the design's
# "--no-dashboard" is a description of the wrong mode, not the flag name. Both
# are named so the tests can assert their absence.
FLAG_SLACK_ONLY: str = "--slack-only"
FLAG_NO_DASHBOARD: str = "--no-dashboard"

# Approval mode. yolo auto-approves every tool so an unattended, headless crew
# can run a turn end to end with nobody to answer a prompt (cli.py:1256). The
# gateway REFUSES yolo unless KIROCREW_HOME is an isolated, non-default home
# (cli.py:498-533, sys.exit(2)); the container's home qualifies, and
# verify_layout asserts it so a bad home fails loudly here rather than as a
# turn that stalls waiting for an approval nobody sees. Cost recorded by the
# owner in the contract/design: every tool the crew calls runs unprompted.
FLAG_APPROVAL: str = "--approval"
APPROVAL_MODE: str = "yolo"

# kiro-cli's own MODEL credential (loader.py:366 CRED_KIRO_API_KEY). Supplied to
# the task from Secrets Manager as this env var; re-injected into the kiro-cli
# child (loader.py:1167/1182) and intentionally NOT denied by the sandbox env
# filter (runtime.py:1251), so forwarding it in the backend env is the whole
# auth mechanism. We forward it EXPLICITLY (below) and refuse to start without
# it (require_api_key), rather than relying on the wholesale os.environ copy.
ENV_KIRO_API_KEY: str = "KIRO_API_KEY"

# Environment variable names the backend reads. VERIFIED against the installed
# source (paths cited), replacing the earlier laptop guesses.
ENV_HOME: str = "KIROCREW_HOME"  # config/paths.py:265 config_dir() honours it
ENV_PORT: str = "KIROCREW_PORT"  # dashboard/urls.py:116 overrides the port
# Pin the bind ADDRESS. dashboard/urls.py:208 reads KIROCREW_BIND; the OFFICIAL
# image sets it to 0.0.0.0 (urls.py:218), which would put the backend on the
# network. Overriding it to loopback keeps the backend unreachable regardless of
# the base image, and a KIROCREW_BIND typo can only narrow back to loopback.
ENV_BIND: str = "KIROCREW_BIND"
# Disable the anonymous beacon. beacon.py:137 reads KIROCREW_TELEMETRY_DISABLED
# (truthy disables); this is the opt-out Kiro Crew actually honours. The config
# key telemetry.beacon_enabled=false the design names is equivalent, but the env
# form needs no config file and cannot be silently ignored. NOTE: neither this
# nor the config key stops the boot update check -- that fires regardless (R4)
# and is recorded as a known outbound request, not suppressed here.
ENV_TELEMETRY_DISABLED: str = "KIROCREW_TELEMETRY_DISABLED"
#: The front's control-plane secret. Named here so the strip below is a named
#: constant rather than a bare string, and so a reader can find every use of it.
ENV_CONTROL_SECRET: str = "SMC_CONTROL_SECRET"

# Channel-credential env vars we drop so no transport can come up. Supplying no
# credentials is the primary defence (transports are credential-gated); this
# strip stops a credential that leaked into the task environment from arming
# one. Extend rather than rely on this list -- it is defence in depth.
CHANNEL_CRED_ENV: frozenset[str] = frozenset(
    {
        "KIROCREW_TELEGRAM_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "KIROCREW_SLACK_BOT_TOKEN",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "KIROCREW_WEIXIN_TOKEN",
        "KIROCREW_FEISHU_APP_SECRET",
    }
)

# Backend boot can be slow: dashboard mode starts embeddings (hundreds of MB),
# the MCP gateway and subagents regardless of mode. Give it generous headroom.
DEFAULT_READY_TIMEOUT_SECS: float = 180.0


class BackendReadyTimeout(RuntimeError):
    """The backend did not become ready within the timeout."""


class BackendExited(RuntimeError):
    """The backend process exited before it became ready."""


def build_backend_argv(settings: Settings) -> list[str]:
    """The command that launches the backend in dashboard mode on loopback.

    Dashboard mode is the default, so no mode flag is added; the point is the
    absence of ``--slack-only`` (the flag that would turn the dashboard off, and
    with it the chat API and slot registry). ``--approval yolo`` lets the
    unattended crew run tool-calling turns with nobody to answer a prompt.
    """
    return [
        *BACKEND_LAUNCHER,
        GATEWAY_SUBCOMMAND,
        FLAG_NO_CRONS,
        FLAG_APPROVAL,
        APPROVAL_MODE,
    ]


def build_backend_env(settings: Settings, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment the backend is launched with.

    Points the backend at the shared data home and loopback port, pins the bind
    address to loopback (overriding any inherited ``KIROCREW_BIND=0.0.0.0`` from
    the base image), disables the beacon, removes any channel credential, and
    forwards the model credential (``KIRO_API_KEY``) explicitly.
    """
    env = dict(os.environ if base is None else base)
    env[ENV_HOME] = str(settings.data_home)
    env[ENV_PORT] = str(settings.backend_port)
    env[ENV_BIND] = common.BACKEND_HOST
    env[ENV_TELEMETRY_DISABLED] = "1"
    for name in CHANNEL_CRED_ENV:
        env.pop(name, None)
    # The FRONT's control-plane secret, dropped for the same defence-in-depth reason
    # and one more: the backend spawns the model subprocess, which inherits this
    # environment, and on Fargate that subprocess runs unsandboxed with an
    # auto-approved shell. So a prompt could read SMC_CONTROL_SECRET out of its own
    # environment and then call the front's control endpoints as the control plane.
    #
    # Nothing is lost by removing it: the only readers are common/config.py, which
    # loads it into Settings, and front/app.py, which validates the
    # X-SMC-Control-Secret header. The BACKEND never reads it -- the gateway's own
    # internal secret is a separate value derived from its port.
    env.pop(ENV_CONTROL_SECRET, None)
    # Forward the model credential explicitly. It is already present via the
    # wholesale copy, but naming it documents that this is the deliberate auth
    # path (require_api_key refuses to start without it) rather than an
    # accident of inheriting os.environ.
    source = os.environ if base is None else base
    if source.get(ENV_KIRO_API_KEY):
        env[ENV_KIRO_API_KEY] = source[ENV_KIRO_API_KEY]
    return env


def require_api_key(env: Mapping[str, str]) -> None:
    """Refuse to start when the model credential is absent or empty.

    Presence only. A present key is NOT a working one: an invalid key boots a
    container that answers the port and fails every turn, and validity can only
    be established by a real turn (see wait_until_ready). This check therefore
    proves the credential was supplied, nothing more.
    """
    if not (env.get(ENV_KIRO_API_KEY) or "").strip():
        raise common.ConfigError(
            f"{ENV_KIRO_API_KEY} is not set. The task injects the model "
            "credential from Secrets Manager; without it the backend boots and "
            "every turn fails. Refusing to start."
        )


def start_backend(
    settings: Settings,
    *,
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    stdout=None,
    stderr=None,
) -> ProcessGroup:
    """Launch the backend as its own process group and return the handle.

    ``argv``/``env`` default to the real invocation; tests inject a fake backend
    process through them. The child is a group leader so the whole tree can be
    drained at shutdown. The backend's run directory is created first: the
    backend writes its per-boot secret there, and if the directory is missing
    the secret write -- and readiness -- fail for a reason that looks like a
    hang rather than a missing path.
    """
    settings.backend_run_dir.mkdir(parents=True, exist_ok=True)
    return spawn_process_group(
        "backend",
        list(argv) if argv is not None else build_backend_argv(settings),
        env=dict(env) if env is not None else build_backend_env(settings),
        stdout=stdout,
        stderr=stderr,
    )


def _port_answers(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, OSError):
        return False


def _secret_present(settings: Settings) -> bool:
    try:
        common.read_boot_secret(settings.backend_run_dir, settings.backend_port)
        return True
    except common.BackendSecretUnavailable:
        return False


def wait_until_ready(
    settings: Settings,
    timeout: float = DEFAULT_READY_TIMEOUT_SECS,
    *,
    process: ProcessGroup | None = None,
    poll_interval: float = 0.25,
) -> None:
    """Block until the backend is ready, or raise.

    Ready = the loopback port accepts a connection AND the per-boot secret file
    exists and is non-empty. Both are required: the port can open before the
    secret is written, and the secret can exist from a previous boot before the
    port is up. Neither proves the backend can complete a turn -- an invalid
    model key is not detectable here -- and this function does not claim it.

    If ``process`` is supplied, an exit before readiness is reported as
    ``BackendExited`` rather than waited out to the full timeout.
    """
    host = common.BACKEND_HOST
    port = settings.backend_port
    deadline = time.monotonic() + timeout

    while True:
        if process is not None:
            code = process.poll()
            if code is not None:
                raise BackendExited(f"backend exited with code {code} before becoming ready")
        if _port_answers(host, port) and _secret_present(settings):
            return
        if time.monotonic() >= deadline:
            raise BackendReadyTimeout(
                f"backend not ready after {timeout:.0f}s: "
                f"port_open={_port_answers(host, port)} "
                f"secret_present={_secret_present(settings)} "
                f"(host={host} port={port} run_dir={settings.backend_run_dir})"
            )
        time.sleep(poll_interval)
