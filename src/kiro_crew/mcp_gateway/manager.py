"""Supervise the KiroCrew MCP gateway subprocess.

Lifecycle:

1. :meth:`GatewayManager.start` — spawn ``python -m
   kiro_crew.mcp_gateway.gatewayd``, wait until the unix socket appears,
   then round-trip one ping/pong to confirm the daemon is serving.
2. Background watchdog — detect exit and respawn with exponential backoff.
3. :meth:`GatewayManager.shutdown` — SIGTERM → SIGKILL the daemon on
   KiroCrew shutdown.

Gateway failures are non-fatal for KiroCrew. If the daemon crashes, the
stub's graceful-fallback path exec's the real MCP binary directly, so
sessions keep working — the only loss is the RAM sharing benefit until
the daemon recovers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket as _socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.env import resolve_krb5_ccname
from kiro_crew.mcp_gateway.pool import READ_BUFFER_LIMIT_BYTES
from kiro_crew.sandbox import _SENSITIVE_ENV_PREFIXES as _SANDBOX_SENSITIVE_ENV_PREFIXES

logger = logging.getLogger(__name__)

# Max time to wait for the gateway's unix socket to appear after spawn.
_SOCKET_READY_TIMEOUT_SECS = 5.0
# Polling interval while waiting for the socket.
_SOCKET_POLL_INTERVAL_SECS = 0.1
# Ping/pong round-trip deadline after the socket appears. The daemon is
# serving if it echoes a pong within this window; otherwise we treat the
# spawn as failed and fall back to per-session MCP.
_PING_TIMEOUT_SECS = 2.0
# Interval between liveness probes in the watchdog. A ping round-trip
# failure (detailed below) promotes the daemon from "alive" to "zombie"
# and triggers the same respawn path we use for crashes. We cannot rely
# on ``proc.wait()`` alone — the accept loop can die silently while the
# Python process stays alive, leaving the socket unreachable.
_LIVENESS_PING_INTERVAL_SECS = 30.0
# Consecutive ping failures tolerated before declaring the daemon a
# zombie. Three gives ~90s of grace — enough for transient chaos-induced
# ping timeouts (stub kill-storms, socket drains, pool-wide eviction
# cycles) to self-heal without the watchdog tripping and killing a
# daemon that would have recovered on its own. Empirically, the 2-fail
# threshold raced with run_chaos.py and produced spurious
# "gatewayd_pid_changed_unexpectedly" during legitimate chaos tests.
_LIVENESS_MAX_CONSECUTIVE_FAILURES = 3
# SIGTERM → SIGKILL grace period on shutdown.
_SHUTDOWN_GRACE_SECS = 5.0
# Respawn backoff: start here, double up to max.
_RESPAWN_BACKOFF_START_SECS = 1.0
_RESPAWN_BACKOFF_MAX_SECS = 60.0

# Python module invoked as the gateway daemon. Kept as a constant so
# tests can monkey-patch it and the spawn path stays one line.
_GATEWAYD_MODULE = "kiro_crew.mcp_gateway.gatewayd"

# Env-var prefixes scrubbed before spawning the gateway daemon and every
# pooled MCP backend it spawns. Reuse sandbox's canonical all-modes list so
# the AWS/SSH/GPG/credential-helper prefixes stay in sync going forward
# (the previous hand-maintained tuple carried a stale
# "Mirrors sandbox" comment that could silently drift). ``AWS_ACCESS`` is kept
# on top because the daemon is more exposed than a credential_process-backed
# session.
#
# We deliberately do NOT scrub ``sandbox._AGENT_DENIED_ENV_KEYS``
# (SLACK_BOT_TOKEN/APP/USER, KIROCREW_OWNER_ID). config/loader.py seeds those
# into os.environ specifically so TRUSTED children — the gateway, its pooled
# MCP backends, and cron — inherit them (a pooled slack-mcp needs its Slack
# token). The sandbox strips those keys from the LLM *agent* subprocess via
# wrap_argv(); MCP backends sit on the trusted side of that boundary in both
# per-session and pooled topologies, so scrubbing them here would break those
# servers without closing any privilege gap.
_SENSITIVE_ENV_PREFIXES: tuple[str, ...] = (
    "AWS_ACCESS",
    *_SANDBOX_SENSITIVE_ENV_PREFIXES,
)


def _scrub_sensitive_env(env: dict[str, str]) -> dict[str, str]:
    """Return a copy of ``env`` with ``_SENSITIVE_ENV_PREFIXES`` keys removed.

    Called before spawning the gateway daemon so MCP backends the daemon
    spawns do NOT inherit credential env vars. File-level sensitive paths
    (``~/.aws``, ``~/.ssh`` ...) are still reachable — those are protected
    per-session by the kiro-cli sandbox's bind-mounts and the hook layer's
    ``is_sensitive_path()`` check; the gateway daemon does not defeat
    either of those. See ``security.md``.
    """
    return {
        k: v
        for k, v in env.items()
        if not any(k.startswith(prefix) for prefix in _SENSITIVE_ENV_PREFIXES)
    }


@dataclass(frozen=True)
class GatewaySpec:
    """Immutable launch parameters for the gateway daemon."""

    socket_path: Path
    idle_timeout_secs: int = 300
    max_backends: int = 64  # keep in sync w/ McpGatewayConfig.max_backends (cover N agents x S servers)
    mcp_target_env: dict[str, str] = None  # type: ignore[assignment]
    prewarm_count: int = 0  # keep in sync w/ McpGatewayConfig.prewarm_count; 0 = disabled

    def __post_init__(self) -> None:
        # dataclass(frozen) + mutable default → use object.__setattr__.
        if self.mcp_target_env is None:
            object.__setattr__(self, "mcp_target_env", {})


class GatewayManager:
    """Supervise a single gateway daemon subprocess."""

    def __init__(self, spec: GatewaySpec) -> None:
        self._spec = spec
        self._process: asyncio.subprocess.Process | None = None
        self._watchdog: asyncio.Task[None] | None = None
        self._stopping = False
        self._adopted = False
        self._lifecycle_lock = asyncio.Lock()

    @property
    def socket_path(self) -> Path:
        return self._spec.socket_path

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> bool:
        """Spawn the daemon and wait for its socket to appear.

        Returns ``True`` on success, ``False`` on any failure. Never raises —
        callers treat a ``False`` return as "fall back to per-session MCP".
        """
        async with self._lifecycle_lock:
            return await self._start_locked()

    async def _start_locked(self) -> bool:
        """Inner start implementation, called under ``_lifecycle_lock``."""
        if self.is_running:
            return True
        # An already-adopted manager already has a live watchdog supervising
        # the incumbent (see the adoption path below). A second start() must
        # not re-enter it and overwrite self._watchdog — that would orphan the
        # first watchdog task (it keeps running but shutdown() can no longer
        # cancel it). is_running is False here because an adopted manager holds
        # no _process, so this explicit guard is required.
        if self._adopted:
            return True

        # Singleton adoption: if a healthy daemon already owns the socket
        # (a sibling manager in this process won the spawn race, or a
        # survivor from a prior gateway), adopt it instead of spawning a
        # competitor. gatewayd's flock guard already makes a duplicate spawn
        # a clean no-op, but adopting skips the wasted spawn/exit churn.
        if await self._ping_once():
            self._adopted = True
            logger.info(
                "mcp-gateway: a healthy daemon already owns %s — adopting "
                "(no spawn)", self._spec.socket_path,
            )
            # Supervise even when adopting: the adopted daemon may be a
            # prior-gateway survivor with no other watchdog in this process.
            # The watchdog's adopted branch re-checks liveness by ping and
            # re-elects (spawns a replacement) if the adopted daemon dies.
            self._watchdog = asyncio.create_task(
                self._run_watchdog(), name="mcp-gateway-watchdog"
            )
            return True

        # Clear any stale socket from a prior crash.
        await self._clear_stale_socket()
        self._spec.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # mkdir's mode is umask-masked and a no-op if the dir already exists;
        # chmod enforces the 0700 the socketsec model calls the primary access
        # boundary (a 0600 socket alone is insufficient on a shared host).
        try:
            self._spec.socket_path.parent.chmod(0o700)
        except OSError:
            pass

        try:
            await self._spawn_once()
        except Exception:
            logger.exception("mcp-gateway: initial spawn failed")
            return False

        # Re-check after spawn: shutdown() may have been called while we
        # were awaiting _spawn_once(). If so, terminate the freshly-spawned
        # daemon to avoid orphaning it.
        if self._stopping:
            logger.info("mcp-gateway: stopping flag set after spawn — aborting start")
            await self._terminate_process(grace_secs=_SHUTDOWN_GRACE_SECS)
            return False

        ok = await self._wait_for_socket(self._spec.socket_path, _SOCKET_READY_TIMEOUT_SECS)
        if not ok:
            logger.warning(
                "mcp-gateway socket did not appear within %.1fs; gateway unreachable",
                _SOCKET_READY_TIMEOUT_SECS,
            )
            await self._terminate_process(grace_secs=_SHUTDOWN_GRACE_SECS)
            return False

        # One ping/pong round-trip confirms the daemon's accept loop is
        # live before we hand control back to the caller. Without this the
        # socket appearing only proves bind() succeeded; the handler task
        # might still be wiring up when the first stub connects.
        if not await self._ping_once():
            logger.warning("mcp-gateway ping failed — treating start as failure")
            await self._terminate_process(grace_secs=_SHUTDOWN_GRACE_SECS)
            return False

        self._watchdog = asyncio.create_task(self._run_watchdog(), name="mcp-gateway-watchdog")
        logger.info(
            "mcp-gateway: started pid=%s socket=%s",
            self._process.pid if self._process else "?",
            self._spec.socket_path,
        )
        return True

    async def shutdown(self) -> None:
        """Stop the watchdog and terminate the daemon."""
        async with self._lifecycle_lock:
            await self._shutdown_locked()

    async def _shutdown_locked(self) -> None:
        """Inner shutdown implementation, called under ``_lifecycle_lock``."""
        self._stopping = True
        if self._watchdog is not None:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog = None
        # Ownership discipline: an adopted manager owns no daemon (_process
        # is None) and no socket. It MUST NOT terminate a process it didn't
        # spawn or unlink a socket a live foreign daemon (a sibling manager
        # in this process, or a prior-gateway survivor) owns.
        # _clear_stale_socket() is a connect-probe-then-unlink; in the
        # documented false-stale window it would steal a live incumbent's
        # socket — re-introducing the exact socket-theft class the flock
        # guard eliminates. Only the owning (spawning) manager tears down.
        if self._adopted:
            return
        await self._terminate_process(grace_secs=_SHUTDOWN_GRACE_SECS)
        await self._clear_stale_socket()

    async def _spawn_once(self) -> None:
        """Low-level spawn — sets ``self._process``. Raises on failure."""
        # Scrub credential env vars — the gateway daemon and every MCP
        # backend it forks would otherwise inherit AWS secrets, SSH agent
        # sockets, etc.  See ``_scrub_sensitive_env`` docstring for why the
        # sandbox's per-session env scrub is not enough on its own here.
        base_env = _scrub_sensitive_env(dict(os.environ))
        env = {**base_env, **self._spec.mcp_target_env}
        # Repair the Kerberos ccache pointer for pooled MCP backends so a
        # long-lived background daemon's forked children inherit a usable
        # ticket for any credential-gated MCP server.
        resolve_krb5_ccname(env)
        # A background daemon can inherit a minimal PATH (e.g. under
        # systemd-user), so prepend the user-local bin dir where MCP
        # server launchers are commonly installed.
        local_bin = str(Path.home() / ".local" / "bin")
        existing_path = env.get("PATH", "")
        extra_dirs = [p for p in (local_bin,) if p and p not in existing_path.split(os.pathsep)]
        if extra_dirs:
            env["PATH"] = os.pathsep.join([*extra_dirs, existing_path]) if existing_path else os.pathsep.join(extra_dirs)
        argv = [
            sys.executable,
            "-m", _GATEWAYD_MODULE,
            "--socket", str(self._spec.socket_path),
            "--idle-timeout-secs", str(self._spec.idle_timeout_secs),
            "--max-backends", str(self._spec.max_backends),
        ]
        # Only pass --prewarm-count when enabled so the daemon command line
        # stays unchanged (and tests stay byte-identical) in the default case.
        if self._spec.prewarm_count > 0:
            argv += ["--prewarm-count", str(self._spec.prewarm_count)]
        # Capture gatewayd stdout/stderr to the canonical KiroCrew log path.
        # This file persists across restarts so operators can diagnose
        # startup failures and stub rejections without attaching a debugger.
        log_path = self._gatewayd_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            log_path.parent.chmod(0o700)
        except OSError:
            pass
        # 0600 from creation: this log captures every pooled backend's
        # stdout/stderr, which routinely includes tokens / API keys in error
        # output — never world-readable on a multi-user host. fchmod also
        # tightens a pre-existing looser file.
        _log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(_log_fd, 0o600)
        except OSError:
            pass
        log_fh = os.fdopen(_log_fd, "ab", buffering=0)
        try:
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                env=env,
                start_new_session=True,
            )
        finally:
            # Subprocess inherits its own copy of the fd, so closing our
            # parent handle is correct on both success and failure paths.
            # try/finally guards the spawn-raises case (ENOENT, EACCES,
            # MemoryError) that would otherwise leak ``log_fh`` until
            # GC — a real risk under a watchdog respawn storm.
            log_fh.close()

    @staticmethod
    def _gatewayd_log_path() -> Path:
        """Return the path gatewayd stdout/stderr get redirected to."""
        home = os.environ.get("KIROCREW_HOME")
        base = Path(home) if home else Path.home() / ".kirocrew"
        return base / "logs" / "mcp-gatewayd.stdout"

    async def ping(self) -> bool:
        """Public liveness probe: ``True`` iff the daemon replies pong."""
        return await self._ping_once()

    async def _ping_once(self) -> bool:
        """Return ``True`` iff the daemon replies ``{"type":"pong"}`` within
        ``_PING_TIMEOUT_SECS``. Any transport or parse error → ``False``.
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(
                    path=str(self._spec.socket_path),
                    limit=READ_BUFFER_LIMIT_BYTES,
                ),
                timeout=_PING_TIMEOUT_SECS,
            )
        except (asyncio.TimeoutError, OSError) as exc:
            logger.warning("mcp-gateway ping connect failed: %s", exc)
            return False
        try:
            writer.write(b'{"type":"ping"}\n')
            try:
                await asyncio.wait_for(writer.drain(), timeout=_PING_TIMEOUT_SECS)
            except (asyncio.TimeoutError, ConnectionError):
                return False
            try:
                line = await asyncio.wait_for(
                    reader.readuntil(b"\n"), timeout=_PING_TIMEOUT_SECS,
                )
            except (asyncio.TimeoutError, asyncio.IncompleteReadError,
                    asyncio.LimitOverrunError):
                return False
            try:
                msg = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return False
            return isinstance(msg, dict) and msg.get("type") == "pong"
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def stats(self) -> dict:
        """Return the daemon's pool snapshot, or ``{}`` on any error."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(
                    path=str(self._spec.socket_path),
                    limit=READ_BUFFER_LIMIT_BYTES,
                ),
                timeout=_PING_TIMEOUT_SECS,
            )
        except (asyncio.TimeoutError, OSError) as exc:
            logger.warning("mcp-gateway stats connect failed: %s", exc)
            return {}
        try:
            writer.write(b'{"type":"stats"}\n')
            await asyncio.wait_for(writer.drain(), timeout=_PING_TIMEOUT_SECS)
            line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=_PING_TIMEOUT_SECS)
            msg = json.loads(line.decode("utf-8"))
            return msg if isinstance(msg, dict) and msg.get("type") == "stats" else {}
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError,
                asyncio.LimitOverrunError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _run_watchdog(self) -> None:
        """Supervise the daemon: respawn on exit or on liveness failure.

        The old implementation only watched for ``proc.wait()`` and so
        missed the silent-zombie mode (accept loop dead, Python process
        still alive). We now race two signals: (1) process exit, and
        (2) a periodic ping round-trip. Whichever fires first wins,
        after which we respawn with exponential backoff.
        """
        backoff = _RESPAWN_BACKOFF_START_SECS
        while not self._stopping:
            proc = self._process
            if proc is None:
                if self._adopted:
                    # Supervise a foreign-owned (adopted) daemon by ping —
                    # we hold no process handle. While it answers, keep
                    # watching; when it dies, surface it and re-elect: drop
                    # adoption and try to become the owner. The flock
                    # arbitrates if a sibling races us; a flock-loser exits
                    # rc=0 and we re-adopt on the next loop.
                    if await self._ping_once():
                        await asyncio.sleep(_LIVENESS_PING_INTERVAL_SECS)
                        continue
                    logger.warning(
                        "mcp-gateway: adopted daemon on %s is gone — "
                        "re-electing (spawning a replacement)",
                        self._spec.socket_path,
                    )
                    self._adopted = False
                    try:
                        await self._clear_stale_socket()
                        await self._spawn_once()
                    except Exception:
                        logger.exception(
                            "mcp-gateway: re-spawn after adopted-daemon "
                            "death failed — will retry"
                        )
                        # Restore adoption so the next iteration re-enters the
                        # adopted branch, re-pings (the daemon is still gone),
                        # and retries the spawn — otherwise _process stays None
                        # with _adopted False and the loop idles forever with
                        # no retry. Escalate backoff (mirroring the main
                        # proc-exit path) so a persistent spawn failure does not
                        # hot-loop at the floor interval.
                        self._adopted = True
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _RESPAWN_BACKOFF_MAX_SECS)
                    else:
                        # Spawned — reset backoff; the next iteration enters the
                        # wait-race to supervise the fresh process.
                        backoff = _RESPAWN_BACKOFF_START_SECS
                    continue
                # proc is None and we are NOT adopting: we are the owner but
                # our last _spawn_once() raised (e.g. a transient fork()/open()
                # error) and left _process None. Retry the spawn here — this
                # branch previously only slept + continued, so a single spawn
                # failure left the watchdog idling forever with no daemon and
                # no retry (permanent wedge).
                try:
                    await self._clear_stale_socket()
                    await self._spawn_once()
                except Exception:
                    logger.exception(
                        "mcp-gateway: owner respawn failed — will retry"
                    )
                    # Escalate backoff (mirroring the main proc-exit path) so a
                    # persistent spawn failure does not hot-loop at the floor.
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _RESPAWN_BACKOFF_MAX_SECS)
                    continue
                # Spawned — reset backoff; next iteration enters the wait-race.
                backoff = _RESPAWN_BACKOFF_START_SECS
                continue

            wait_task = asyncio.ensure_future(proc.wait())
            ping_task = asyncio.ensure_future(self._liveness_probe_loop())

            exit_reason = "unknown"
            rc: Any = None
            try:
                done, _pending = await asyncio.wait(
                    {wait_task, ping_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if wait_task in done:
                    rc = wait_task.result()
                    exit_reason = f"daemon exited rc={rc}"
                elif ping_task in done:
                    # Liveness probe decided the daemon is a zombie.
                    exit_reason = ping_task.result() or "liveness probe failed"
                    # Kill the zombie so respawn gets a clean PID. We use
                    # _terminate_process so the usual SIGTERM→SIGKILL
                    # grace applies — a zombie won't respond to SIGTERM
                    # but SIGKILL always lands.
                    logger.warning(
                        "mcp-gateway: %s — killing zombie pid=%s",
                        exit_reason, proc.pid,
                    )
                    await self._terminate_process(grace_secs=_SHUTDOWN_GRACE_SECS)
                    rc = proc.returncode
            except asyncio.CancelledError:
                for t in (wait_task, ping_task):
                    t.cancel()
                raise
            except Exception:
                logger.exception("mcp-gateway: watchdog race failed")
                exit_reason = "watchdog race exception"
                rc = -1
            finally:
                for t in (wait_task, ping_task):
                    if not t.done():
                        t.cancel()
                        with contextlib.suppress(
                            asyncio.CancelledError, Exception
                        ):
                            await t

            # Clear the handle once we've consumed the exit so a failed
            # respawn below doesn't cause the next iteration to re-wait on
            # the same dead process, re-log the same rc, and double backoff
            # spuriously.
            self._process = None
            if self._stopping:
                return
            logger.warning(
                "mcp-gateway: %s — respawning in %.1fs", exit_reason, backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _RESPAWN_BACKOFF_MAX_SECS)
            if self._stopping:
                return
            # Before respawning, check whether another daemon already owns
            # the socket healthily — a sibling manager may have won the race,
            # or gatewayd's flock guard rejected our last spawn (it exited
            # rc=0 without binding). If so, adopt the incumbent and stand the
            # watchdog down instead of respawn-looping doomed competitors.
            if await self._ping_once():
                self._adopted = True
                logger.info(
                    "mcp-gateway: socket %s already served by another daemon "
                    "— adopting; watchdog will supervise it via ping",
                    self._spec.socket_path,
                )
                # continue (NOT return): re-enter the loop so the adopted
                # branch (proc is None and self._adopted) supervises the
                # incumbent by ping and re-elects if it later dies. Returning
                # here would terminate the watchdog and leave the adopted
                # daemon unsupervised.
                continue
            try:
                await self._clear_stale_socket()
                await self._spawn_once()
            except Exception:
                logger.exception("mcp-gateway: respawn failed — will retry")
                continue
            # Reset backoff after a successful respawn that stays alive
            # for at least 30s.
            await asyncio.sleep(30.0)
            if self._process is not None and self._process.returncode is None:
                backoff = _RESPAWN_BACKOFF_START_SECS

    async def _liveness_probe_loop(self) -> str:
        """Ping the daemon every ``_LIVENESS_PING_INTERVAL_SECS``.

        Returns a human-readable reason string as soon as
        ``_LIVENESS_MAX_CONSECUTIVE_FAILURES`` consecutive ping round-trips
        fail. Never returns normally — either the coroutine is cancelled
        by the outer watchdog race (daemon exited first) or it returns a
        failure reason.
        """
        consecutive_failures = 0
        while True:
            await asyncio.sleep(_LIVENESS_PING_INTERVAL_SECS)
            if self._stopping:
                # Outer loop will notice _stopping and exit; yield a
                # benign reason that gets ignored on stop.
                return "stopping"
            ok = await self._ping_once()
            if ok:
                consecutive_failures = 0
                continue
            consecutive_failures += 1
            logger.warning(
                "mcp-gateway: liveness ping failed (%d/%d consecutive)",
                consecutive_failures, _LIVENESS_MAX_CONSECUTIVE_FAILURES,
            )
            if consecutive_failures >= _LIVENESS_MAX_CONSECUTIVE_FAILURES:
                return (
                    f"zombie detected: {consecutive_failures} consecutive "
                    f"ping failures over "
                    f"{int(consecutive_failures * _LIVENESS_PING_INTERVAL_SECS)}s"
                )

    async def _terminate_process(self, *, grace_secs: float) -> None:
        proc = self._process
        self._process = None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace_secs)
        except asyncio.TimeoutError:
            logger.warning("mcp-gateway: SIGTERM timeout, escalating to SIGKILL")
            try:
                proc.kill()
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                logger.error("mcp-gateway: SIGKILL also timed out — pid=%s", proc.pid)
            # SIGKILL skips gatewayd's pool.shutdown_all(), so its pooled MCP
            # backends (each a session leader via start_new_session) reparent to
            # init and leak. Reap the pgids gatewayd persisted out-of-band.
            self._reap_orphaned_backends()

    def _reap_orphaned_backends(self) -> None:
        """Best-effort killpg of pooled backends left orphaned by a SIGKILLed
        gatewayd, read from the ``<socket>.backends`` sidecar the daemon
        maintains. Each recorded pid is a session leader (pid == pgid)."""
        pidfile = Path(f"{self._spec.socket_path}.backends")
        try:
            raw = pidfile.read_text(encoding="utf-8")
        except OSError:
            return
        for token in raw.split():
            try:
                pid = int(token)
            except ValueError:
                continue
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        with contextlib.suppress(OSError):
            pidfile.unlink()

    @staticmethod
    def _probe_socket_live(sock_path: str) -> bool:
        """Blocking probe: return True if a daemon is listening on *sock_path*.

        Runs in a thread via :func:`asyncio.to_thread` so the event loop is
        never blocked by the up-to-1s ``connect()`` timeout.
        """
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        try:
            s.settimeout(1.0)
            s.connect(sock_path)
            return True
        except (ConnectionRefusedError, OSError):
            return False
        finally:
            s.close()

    async def _clear_stale_socket(self) -> None:
        """Remove a stale socket left behind by a prior crash.

        Before unlinking, attempts a connect to verify the socket is not
        live. If connect succeeds, another daemon is bound — leave the
        socket in place and let asyncio.start_unix_server fail with
        EADDRINUSE.

        The blocking socket probe is offloaded to a thread so it never
        stalls the event loop.
        """
        sock = self._spec.socket_path
        try:
            if not sock.exists():
                return
        except OSError:
            return
        # Probe liveness in a thread — blocking connect(timeout=1s) must
        # not stall the event loop.
        is_live = await asyncio.to_thread(self._probe_socket_live, str(sock))
        if is_live:
            logger.warning(
                "mcp-gateway: socket %s is live; refusing to unlink", sock
            )
            return
        try:
            sock.unlink()
        except OSError as exc:
            logger.debug("mcp-gateway: could not remove stale socket %s: %s", sock, exc)

    @staticmethod
    async def _wait_for_socket(path: Path, timeout: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if path.exists():
                return True
            await asyncio.sleep(_SOCKET_POLL_INTERVAL_SECS)
        return path.exists()
