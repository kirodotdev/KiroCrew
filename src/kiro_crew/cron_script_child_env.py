"""What a cron child starts with: its environment, its scratch and temp dirs, its gateway secret.

Every script and command cron child is spawned with ``_clean_cron_env()`` -- the
gateway's environment minus the cron deny set and any granted secret names -- and a
per-run scratch directory recorded to its job. A script child also gets the
gateway's internal secret through a 0600 file and the port to dial back on, and its
launcher runs ``child_persistence_preflight`` before the script body. Its MCP children
reach the gateway under the job's session key with the signed token
``_publish_script_session_token`` puts in that environment.

Part of :mod:`kiro_crew.cron_script`, the import path callers, tests and docs use: it
reaches every name here, and a patch through it reaches this module too
(:mod:`kiro_crew._facade_mirror`). Logs under the ``kiro_crew.cron_script`` logger.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from kiro_crew import agent_scratch, platform_compat
from kiro_crew.config.loader import read_local_secret
from kiro_crew.config.paths import data_home
from kiro_crew.port_resolution import resolve_serving_port
from kiro_crew.sandbox import _AGENT_DENIED_ENV_KEYS, CANONICAL_TEMP_KEYS

logger = logging.getLogger("kiro_crew.cron_script")


# Env vars stripped from EVERY cron subprocess (command and script), regardless
# of OS sandbox mode. The OS sandbox can fall back to backend "none" (e.g.
# macOS >= 26, see sandbox._probe_sandbox_exec), so env scrubbing is the only
# guaranteed control on those hosts. _AGENT_DENIED_ENV_KEYS = Slack tokens +
# KIROCREW_OWNER_ID; KIROCREW_INTERNAL_SECRET is handed to scripts via a 0600
# temp file instead of the env (defense-in-depth item 4).
_CRON_ENV_DENY: frozenset[str] = frozenset({"KIROCREW_INTERNAL_SECRET", *_AGENT_DENIED_ENV_KEYS})


#: Env-var names carrying operator-granted vault secrets IN THIS PROCESS.
#: Empty in the gateway; the granted-run launcher seeds it right after
#: applying the grant to ``os.environ``, so every descendant this process
#: spawns through :func:`_clean_cron_env` (notably ``ctx.call_tool``'s MCP
#: server subprocess) gets the secrets STRIPPED — the grant authorizes the
#: approved script body, never the arbitrary server binaries it calls.
_GRANTED_ENV_KEYS: set[str] = set()


def _clean_cron_env() -> dict[str, str]:
    """Return os.environ minus the cron env-deny set (secrets never inherited).

    The temp triple (``TMPDIR``/``TMP``/``TEMP``) is not copied verbatim: every
    key of it that is present is re-pointed at :func:`_default_temp_dir`, so a
    child never inherits a temp directory that has vanished under this
    process (see that function). Absent keys stay absent.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _CRON_ENV_DENY and k not in _GRANTED_ENV_KEYS
    }
    present = [k for k in CANONICAL_TEMP_KEYS if k in env]
    if present:
        temp_dir = _default_temp_dir()
        for k in present:
            env[k] = temp_dir
    return env


def _allocate_cron_scratch(job_id: str | None) -> Path | None:
    """A fresh per-run temp dir for one script/command cron child, or None.

    Without it the child inherits the gateway's own temp dir, which under the
    shipped systemd unit is ``/tmp`` -- a tmpfs on many distributions, so a
    large clone or venv a cron leaves there is charged to RAM. The
    directory lives under the managed ``agent_scratch`` root on the data home's
    disk and is reclaimed by the existing sweep once the child's process group
    is dead and the tree has gone idle, exactly like an agent process's.

    Scratch is hygiene, not a spawn prerequisite: an allocation failure (disk
    full, a link planted at the managed root) is logged and the run proceeds
    with the inherited temp dir, the same degradation the agent spawners take.
    """
    try:
        # A command run with no job id (preview, a direct call) still gets one.
        return agent_scratch.allocate_scratch(f"cron-{job_id or 'adhoc'}")
    except (OSError, agent_scratch.ScratchBoundaryError):
        logger.warning(
            "agent-scratch: could not allocate for cron %s; running with inherited temp",
            job_id,
            exc_info=True,
        )
        return None


def _record_cron_scratch_owner(scratch: Path | None, proc: subprocess.Popen) -> bool:
    """Hand *scratch*'s owner marker to the live child; False means reap it.

    ``refused`` (a link where the marker belongs: the child is steering this
    unsandboxed write) and ``stale`` (the marker still names the gateway, which
    the sweep would read as a dead owner over a live child) both fail the run,
    as they fail an agent spawn. ``unwritable`` is fail-open: an unowned dir is
    kept, never swept.
    """
    if scratch is None:
        return True
    outcome = agent_scratch.record_owner(scratch, proc.pid)
    if outcome in ("refused", "stale"):
        logger.warning("agent-scratch: cron child %s owner record %s; reaping", proc.pid, outcome)
        return False
    return True


def _discard_unused_cron_scratch(scratch: Path | None) -> None:
    """Remove a scratch dir no child ever got.

    Its marker still names the gateway, which is alive, so the sweep would
    keep it for the gateway's whole life.
    """
    if scratch is not None:
        shutil.rmtree(scratch, ignore_errors=True)


_SCRATCH_OWNER_ERROR = "❌ Cron run stopped: its temp dir owner marker could not be recorded safely"


def _default_temp_dir() -> str:
    """``tempfile``'s default directory, re-resolved if the cached one has vanished.

    ``tempfile`` resolves ``dir=None`` from a process-wide cache seeded ONCE
    from ``TMPDIR``/``TMP``/``TEMP`` -- an ``execve`` snapshot. The gateway's
    own value can name a per-process scratch directory (``agent_scratch``)
    inherited from whichever agent session started it: a directory owned by a
    pid this process is not, which the hourly sweep reclaims once that owner is
    dead and the tree has been idle for an hour -- exactly what a daily or
    weekly job's few-second touch guarantees. ``mkstemp`` then raises ``ENOENT``
    for a file it is trying to CREATE, and the job silently does not run until
    the gateway restarts. So the directory is checked at every run, not once:
    the value that was valid at spawn is the one that goes stale.

    A vanished directory is dropped by re-resolving through ``tempfile``'s own
    candidate chain (a ``None`` cache re-probes each candidate by creating a
    file in it, so the dead ``TMPDIR`` is skipped and the platform default
    wins). It is never recreated: a bare ``makedirs`` under the managed scratch
    root would put back a directory with no owner record, which the sweep
    never deletes on purpose -- a permanent leak in place of a skipped run.
    Nothing in this process can be using a directory that does not exist, so
    the re-resolution takes nothing from any other ``tempfile`` caller.
    """
    current = tempfile.gettempdir()
    if os.path.isdir(current):
        return current
    logger.warning("cron: temp dir %r has vanished; re-resolving the default temp dir", current)
    tempfile.tempdir = None
    return tempfile.gettempdir()


# A script child inherits its parent's seccomp filter, and seccomp survives fork /
# exec / setsid: when the sandbox that installed it is torn down underneath the
# child, every file syscall returns ENOSYS while the process looks healthy, the user
# function still returns, and the parent records a successful run for a job that
# banked nothing. So the child probes its data home with the gateway's own probe.

#: Exit code for a child that cannot persist (sysexits.h EX_CONFIG: the
#: environment is wrong, not the script). Only "non-zero" is load-bearing.
CHILD_PERSISTENCE_EXIT_CODE = 78

CHILD_PERSISTENCE_PREFIX = "❌ Cron child cannot persist state: "


def child_persistence_preflight() -> None:
    """Refuse the run when this child's own filesystem cannot persist state.

    Called from the launcher preamble, after ``boot_platform`` (so a composition
    failure still surfaces as itself) and before the script body runs. The probe
    names an inherited seccomp filter when ``errno`` says ``ENOSYS``.
    ``SystemExit``, so no handler can reshape it into a status envelope.
    """
    reason = platform_compat.probe_file_persistence(data_home())
    if reason is None:
        return
    print(f"{CHILD_PERSISTENCE_PREFIX}{reason}", file=sys.stderr, flush=True)
    raise SystemExit(CHILD_PERSISTENCE_EXIT_CODE)


def _resolve_internal_secret(port: int) -> str:
    """Internal secret for ScriptContext HTTP calls (e.g. notify -> /api/send-message).

    The gateway generates its secret at startup and publishes it per listener as
    ``run/gateway-<port>.secret`` (with ``config_dir()/.local_secret`` as the
    home-wide fallback); the ``KIROCREW_INTERNAL_SECRET`` env var is normally unset,
    so fall back to the file via the shared ``config.loader.read_local_secret``
    helper (single home for that read). Without this the sandbox sends an empty
    ``X-Internal-Secret`` and every code-cron notify gets HTTP 403.

    Takes the ALREADY-RESOLVED dial port rather than resolving its own. The caller
    resolves the port ONCE and passes the same value here and into
    ``_KIROCREW_DIAL_PORT``. Resolving twice -- once for the credential, once for the
    child -- is a TOCTOU: a ``--port auto`` gateway that binds between the two calls
    would mint the credential for one port and tell the child to dial another, and
    the mismatched credential 403s the callback. One resolution makes that
    unrepresentable, which is what the ``_KIROCREW_DIAL_PORT`` mechanism promised.
    """
    env_secret = os.environ.get("KIROCREW_INTERNAL_SECRET", "")
    if env_secret:
        return env_secret
    # v4 loopback LITERAL, matching the http://127.0.0.1 dial: a single-family
    # gateway (v4-only or wildcard/container) still authenticates, where the
    # ambiguous ``localhost`` would demand both families and refuse it.
    return read_local_secret(port, dial_host="127.0.0.1")


def _child_internal_secret(
    provider: Callable[[], str] | None,
    port: int,
) -> str:
    """The secret the script child sends as ``X-Internal-Secret``.

    A ``provider`` returns the gateway's LIVE in-memory secret and takes
    precedence: the in-process scheduler runs inside the gateway that minted
    that value, so handing back its own secret is authoritative. Only when it
    yields nothing (or no provider was given — a runner constructed outside a
    gateway process, or a gateway with no dashboard) does resolution fall back
    to the env/file derivation. The provided value is used only to write the
    0600 temp file the child reads; it is never logged, put in the env, or
    placed in an error string.
    """
    if provider is not None:
        live = provider()
        if live:
            return live
    return _resolve_internal_secret(port)


def _resolve_dial_port() -> int:
    """The ONE port this cron dials, used for both the credential and the child.

    The parent mints the credential and the child sends it, so a second independent
    resolution in the child is exactly how the two diverge: ``ScriptContext`` reads
    ``KIROCREW_PORT``, which is 5476 on a ``--port auto`` gateway, while the parent
    would have minted for the real ephemeral port -- credential for one gateway,
    request to another. One resolution, injected as ``_KIROCREW_DIAL_PORT``, makes
    that mismatch unrepresentable.

    Delegates to :func:`resolve_serving_port`, the shared gateway-side resolver that
    prefers ``KIROCREW_BOUND_PORT`` over an inherited ``KIROCREW_PORT`` -- the cron
    scheduler runs inside the gateway, so the bound port is ground truth and a
    sibling-naming ``KIROCREW_PORT`` must not win.
    """
    return resolve_serving_port()


def _publish_script_session_token(clean_env: dict[str, str], job_id: str) -> str:
    """Attach the signed token naming ``cron:<job id>`` to a script child's env.

    A script cron's MCP children reach the gateway's internal API under the job's
    session key, and that API accepts a declared key only behind a transport
    attestation. The unix-socket peer walk cannot supply one here: nothing
    publishes a signed pid mapping for the sandbox launcher's pid, so the
    ancestry walk resolves no session and the middleware attaches no kernel
    attestation. The signed token is the channel that remains, minted with the
    same primitive every ACP session uses.

    One token per run: its mapping exists for the life of the run and is removed
    when the run ends, so completed runs accumulate no mappings or orphans.
    There is no cache and nothing to evict. The caller retracts the returned
    token in its finally block; a refused unlink is reported at WARNING.

    Blocking file I/O, on the cron worker thread rather than the event loop.
    A publication failure leaves a token the verifier refuses, which costs the
    child calls that need an attested identity, never the run itself.
    """
    from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV, mint_stub_session_token
    from kiro_crew.session_token_sig import publish_session_token

    token = mint_stub_session_token()
    publish_session_token(token, f"cron:{job_id}")
    clean_env[STUB_SESSION_TOKEN_ENV] = token
    return token
