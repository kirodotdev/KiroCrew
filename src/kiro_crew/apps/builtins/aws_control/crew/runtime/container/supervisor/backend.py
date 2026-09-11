"""Launching the Kiro Crew backend headless, and knowing when it is ready.

The deployed unit is Kiro Crew's own backend, run in *dashboard mode*, on
loopback, with no interface served (`docs/system-specs/modules/aws-control.md`,
"Three processes, one task"). Dashboard mode is
required and `--no-dashboard` is wrong: the smaller ``_init_api_server`` it
starts has neither the chat endpoints nor the slot registry this design calls,
and it quiets nothing, so headless here means "no interface exposed", not
"--no-dashboard".

Launch decisions, all from the contract and design:

- Bind ``common.BACKEND_HOST`` (127.0.0.1, not configurable). The gateway binds
  loopback by default; we also pass it explicitly so the intent is recorded.
- ``--no-crons``. Arming the scheduler fires any *overdue* job immediately, so a
  freshly deployed crew would run a stale job on boot.
- Serve no messaging channel. ``write_backend_config`` writes a config the
  container owns that turns every transport off by name, and the launch env carries
  no channel credential. Both halves are needed: two transports need no credential
  and start on their config flag, and Slack has no config flag and starts on its
  tokens.
- ``telemetry.beacon_enabled=false``.
- The boot update check cannot be disabled by config (R4). We do not fight it;
  it is recorded as a known outbound request in the report, not suppressed here.

Readiness means the port answers AND the boot secret file exists. Process-alive
is not ready, and this check deliberately proves no more than that: a present
model key is not a working one, so a container can be "ready" here and still
fail every turn on an invalid key. We do not claim otherwise.

VERIFIED against the installed Kiro Crew (0.3.0) by booting a real
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

import json
import logging
import os
import socket
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from .. import common
from ..common import Settings
from . import bundle
from .process import ProcessGroup, spawn_process_group

log = logging.getLogger("container.supervisor.backend")

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

#: Every messaging transport the gateway can start, by the name of its config
#: section. ``write_backend_config`` turns each one OFF in a file the container owns.
#:
#: A container has nobody to talk to on a messaging channel: the only caller it
#: serves is the customer's HTTP turn through the front process. A transport that
#: comes up gives a crew an outbound channel to an owner's workspace that nobody
#: chose to grant it.
#:
#: Written positively rather than by removing what arms a transport. Two of these
#: need no credential at all (``imessage``, ``whatsapp`` -- their registry
#: descriptors carry an empty credential tuple), so they start on their config flag
#: alone and no amount of secret-stripping reaches them.
#:
#: ``test/test_crew_container_config_isolation.py`` ratchets this tuple against
#: ``kiro_crew.channels.builtin_channel_descriptors()``, the gateway's own registry,
#: so a channel added there reds CI until it is turned off here. That test reads
#: THIS FILE'S SOURCE with ``ast`` rather than importing it, because nothing may
#: import the image's tree.
CHANNEL_SECTIONS: tuple[str, ...] = (
    "discord",
    "feishu",
    "imessage",
    "slack",
    "teams",
    "telegram",
    "webex",
    "wecom",
    "weixin",
    "whatsapp",
)

# Channel-credential env vars we drop, so a credential that leaked into the task
# environment cannot arm a transport whose config flag is off -- and so ``slack``,
# whose config section has no ``enabled`` key at all and which starts on its tokens
# alone, has nothing to start from.
#
# These names are the ones the gateway ACTUALLY reads, taken from its channel
# registry (each descriptor's ``credentials``), and the same ratchet test keeps them
# in step with it. A name the gateway does not read is worse than a missing one: it
# reads as coverage while stripping nothing.
#
# This half of the isolation is still a list of names, which is why it is not the
# whole of it: it cannot cover a credential spelling nobody has thought of. The
# config file above is what does not depend on knowing a name.
CHANNEL_CRED_ENV: frozenset[str] = frozenset(
    {
        "SLACK_APP_TOKEN",
        "SLACK_BOT_TOKEN",
        "WECOM_BOT_ID",
        "WECOM_SECRET",
        "TELEGRAM_BOT_TOKEN",
        "DISCORD_BOT_TOKEN",
        "WEBEX_BOT_TOKEN",
        "MICROSOFT_APP_ID",
        "MICROSOFT_APP_PASSWORD",
        "WEIXIN_TOKEN",
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
    }
)

#: The variables that hand an AWS credential to anything that reads the environment.
#:
#: On Fargate the task role is not a file or a key: it is an HTTP endpoint named by
#: ``AWS_CONTAINER_CREDENTIALS_RELATIVE_URI``, which every AWS SDK resolves without being
#: asked. The backend spawns the model subprocess with this environment, and that
#: subprocess auto-approves every tool it calls -- the same premise that
#: already removes ``SMC_CONTROL_SECRET`` below. So a turn could read the URI from its own
#: environment, curl it, and act as the task role: read the transcript bucket, and reach
#: whatever else the role is granted. Keeping it out of the environment closes that
#: independently of the sandbox.
#:
#: Removing them is not a loss for the backend. It talks to the model over
#: ``KIRO_API_KEY`` and to nothing in AWS; the SUPERVISOR is what uses the task role, and
#: it keeps its own ``os.environ`` untouched. A container that genuinely needs an AWS call
#: on the turn path should be given a narrower credential explicitly rather than inherit
#: the task's.
#:
#: ``AWS_CONTAINER_AUTHORIZATION_TOKEN`` and the full-URI form are listed too: the newer
#: agent protocol uses them, and a list that only covers the shape in front of us today
#: fails open the day the platform adds another.
AWS_CRED_ENV: frozenset[str] = frozenset(
    {
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_ROLE_ARN",
        "AWS_PROFILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE",
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


#: The `agent` settings the container WRITES rather than inherits, and the reason
#: they are here rather than left to their defaults.
#:
#: `verify_sandbox` refuses to start on a host that cannot sandbox the model
#: subprocess, and this container offers no unsandboxed posture at all. That refusal
#: is worth nothing if a config file can turn the sandbox off underneath it: the
#: gateway reads these three keys, `sandbox="off"` skips its own OS-level isolation,
#: and either fallback flag set true lets `wrap_argv` proceed with no backend instead
#: of raising. So they are forced exactly as the channel sections are -- written, not
#: merged, not defaulted.
#:
#: Defaults are not a substitute for writing them. `sandbox_allow_unsandboxed_exec`
#: resolves an UNDECLARED value through `unsandboxed_exec_platform_default()`, so
#: what silence means is a property of the platform rather than a constant, and
#: `config.json` arrives in the task from outside this code.
#:
#: `test/test_crew_container_config_isolation.py` ratchets these keys against
#: `AgentConfig`, so a sandbox knob added to the gateway reds CI until the container
#: decides what to write for it.
FORCED_AGENT_SETTINGS: Mapping[str, object] = {
    "sandbox": "auto",
    "sandbox_allow_no_isolation": False,
    "sandbox_allow_unsandboxed_exec": False,
}


def build_backend_config(existing: Mapping[str, object] | None = None) -> dict[str, object]:
    """The config the backend boots with: no messaging transport, no sandbox opt-out.

    Merges over *existing* rather than replacing it, so a crew bundle that ships
    config keeps them, and a section already present keeps its other settings with
    only the forced keys overwritten. Forcing rather than defaulting is the point: a
    file that arrives with ``telegram.enabled`` true, or with ``agent.sandbox`` set to
    ``off``, must lose, or the container's posture is a suggestion.

    A non-dict where a section should be is REPLACED, not merged. The gateway coerces
    such a section to defaults, and defaults are not what this function is for.
    """
    config: dict[str, object] = dict(existing or {})
    for section in CHANNEL_SECTIONS:
        current = config.get(section)
        merged = dict(current) if isinstance(current, dict) else {}
        merged["enabled"] = False
        config[section] = merged
    agent = config.get("agent")
    agent_merged = dict(agent) if isinstance(agent, dict) else {}
    agent_merged.update(FORCED_AGENT_SETTINGS)
    config["agent"] = agent_merged
    return config


def write_backend_config(settings: Settings) -> Path:
    """Write the container-owned config to ``<config dir>/config.json``.

    Called before the backend starts, because the gateway reads this file at boot and
    a transport it starts there is already connected by the time anything else could
    object.

    ``config_dir`` equals ``data_home`` (see the spec's "Three processes, one task").

    Written through a sibling temp and one atomic replace, because the failure this
    file has is in the FUTURE: nothing reads it during this write, and a truncated
    write is read at the NEXT start, by which time the run that produced it is gone
    and the container boots on a config it cannot parse. The temp is opened
    ``O_EXCL | O_NOFOLLOW`` in the destination's own directory (a replace across
    filesystems is not atomic), fsynced so the bytes are on disk before the name
    moves, and the directory is fsynced after so the name change itself survives a
    power loss.

    A symlink at the destination is REFUSED rather than replaced. ``os.replace``
    would not write through it -- ``rename`` unlinks the link rather than following
    it -- but a link there is a signal, and quietly consuming it hides that something
    planted one. This matches ``bundle._write_nofollow``, which every other write
    into the data home goes through.
    """
    bundle._mkdir_or_refuse(settings.config_dir, what="the config directory")
    path = settings.config_dir / "config.json"
    if path.is_symlink():
        raise common.ConfigError(
            f"{path} is a symlink. The container writes its own configuration there and "
            "refuses to replace a link rather than a file, because a link means "
            "something else chose that path. Refusing to start."
        )
    existing: Mapping[str, object] | None = None
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            existing = raw
    payload = json.dumps(build_backend_config(existing), indent=2, sort_keys=True) + "\n"
    _atomic_write_nofollow(path, payload.encode("utf-8"))
    log.info(
        "backend config: %d messaging transports disabled, %d agent settings forced",
        len(CHANNEL_SECTIONS),
        len(FORCED_AGENT_SETTINGS),
    )
    return path


def _atomic_write_nofollow(dst: Path, data: bytes) -> None:
    """Write *data* to *dst* atomically, through a sibling temp in the same directory.

    Separate from ``bundle._write_nofollow`` rather than replacing it: that one is a
    truncating write, which is the right shape for a file read only by the process
    that just wrote it. This one is for a file another process reads at a LATER start,
    where a half-written state is the whole problem.

    The temp carries the pid so two supervisors in one data home cannot collide on the
    name, and ``O_EXCL`` means a pre-planted file at that name is an error rather than
    something written through.
    """
    tmp = dst.with_name(f"{dst.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(tmp), flags, 0o600)
    except OSError as exc:
        raise common.ConfigError(
            f"container config write failed [temporary file]: {tmp} could not be created "
            f"({exc})."
        ) from exc
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dst)
    except OSError as exc:
        raise common.ConfigError(
            f"container config write failed [publish]: {dst} could not be written ({exc})."
        ) from exc
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass  # the replace consumed it, which is the success path
        except OSError:
            log.warning("container config: could not remove %s", tmp)
    dir_fd = os.open(str(dst.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


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
    # The task role, for the reason spelled out on AWS_CRED_ENV: on Fargate the
    # credential is an endpoint any SDK finds by itself, so carrying it in the env
    # buys nothing while adding a secret the auto-approved worker does not need.
    for name in AWS_CRED_ENV:
        env.pop(name, None)
    # The FRONT's control-plane secret, dropped for the same defence-in-depth
    # reason and one more: the backend spawns the model subprocess, which inherits
    # this environment and auto-approves every tool it calls. Keeping the secret
    # out of that environment means a prompt cannot read SMC_CONTROL_SECRET and
    # call the front's control endpoints as the control plane, independently of
    # the sandbox.
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
    #
    # Unlike the two secrets above, this one CANNOT be popped: kiro-cli re-injects
    # it into the worker and it is the whole model-auth mechanism, so the worker
    # must carry it. That is precisely why the worker must run sandboxed -- see
    # verify_sandbox, which refuses to start when it cannot. There is no
    # unsandboxed posture: a worker that both holds this credential and runs an
    # auto-approved shell on untrusted prompt content would be a
    # prompt-injection-to-credential-exfiltration path, so the container refuses
    # rather than offer it. Brokering the credential out of the worker's
    # environment (which would let an unsandboxed posture be safe) is future work.
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


def _refuse_planted_links(settings: Settings) -> None:
    """Refuse to start when a symlink sits where the boot secret is written.

    The gateway writes ``run/gateway-<port>.secret`` itself, with an ordinary open
    that follows a link. A link planted at that path -- or at the run directory --
    therefore turns the gateway's own secret write into a truncating write to
    wherever the link points, and the crew's model worker can write inside the data
    home, so the path is reachable by prompt content.

    Nothing here can make the gateway's writer safe, so this refuses instead: the
    task starts on a real directory and a real file path, or it does not start. The
    checks are LSTAT-based on purpose, since ``exists()`` follows the link it is
    looking for. This is the guard ``config.json`` gets from
    ``bundle._write_nofollow``, applied to a file another process writes.
    """
    run_dir = settings.backend_run_dir
    if run_dir.is_symlink():
        raise common.ConfigError(
            f"backend run directory {run_dir} is a symlink. The backend writes its "
            "per-boot secret inside it with a link-following open, so a link here "
            "redirects that write outside the data home. Refusing to start."
        )
    secret = common.secret_path(run_dir, settings.backend_port)
    if secret.is_symlink():
        raise common.ConfigError(
            f"boot secret path {secret} is a symlink. The backend truncates and "
            "rewrites this file on every start, so a link here makes it overwrite the "
            "link's target instead. Refusing to start."
        )
    if secret.exists() and not secret.is_file():
        raise common.ConfigError(
            f"boot secret path {secret} exists and is not a regular file. The backend "
            "expects to write a file there. Refusing to start."
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
    hang rather than a missing path. Creating it is also where the secret path is
    checked for a planted link (see ``_refuse_planted_links``), because that is the
    last moment before the process that follows such a link exists.
    """
    bundle._prepare_dir_nofollow(settings.backend_run_dir)
    _refuse_planted_links(settings)
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
