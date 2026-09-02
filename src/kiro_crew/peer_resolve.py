"""Kernel-attested peer identity resolution shared by gatewayd and the dashboard.

A local peer that connected over an ``AF_UNIX`` socket has a kernel-reported
PID (``SO_PEERCRED`` on Linux, ``LOCAL_PEERPID`` on macOS — see
:mod:`kiro_crew.mcp_gateway.socketsec`). Walking that PID's */proc* ancestry
and looking for the gateway-published ``session_pid_<pid>.txt`` mapping yields
a session identity the CALLER cannot forge: the pidfile is written by the
gateway on session claim, and the walk runs in the SERVER's PID namespace, so
a same-uid process cannot substitute an attacker-chosen ancestry the way it
can substitute an env var or an HTTP header.

Two consumers share this single walk:

* ``mcp_gateway.gatewayd`` resolves identities for key-less MCP stub
  registrations (and indexes the returned host chain for claim frames).
* ``dashboard.token_auth`` cross-checks the client-declared ``X-Session-Key``
  header on internal-API requests arriving over the dashboard's unix socket.

Both need identical semantics — one hardened read discipline, one ancestry
walk — which is why the walk lives here rather than being duplicated.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from kiro_crew.config.loader import config_dir as _default_config_dir
from kiro_crew.mcp_caller import _parent_pid as _default_ppid
from kiro_crew.session_pid_sig import read_session_pid_txt, verify_session_pid

logger = logging.getLogger(__name__)


def resolve_peer_identity(
    peer_pid: int,
    *,
    config_dir_fn: Callable[[], Path] | None = None,
    ppid_fn: Callable[[int], int] | None = None,
    signed_only: bool = False,
) -> tuple[str, list[int]]:
    """Walk the peer's real-PID ancestry (server-side): session key + host chain.

    Runs in the calling server's own PID namespace (real pids), so it works
    regardless of how the peer sees the world. A single /proc walk returns
    both:

    * the session_key from the first ancestor with a ``session_pid_<pid>.txt``
      file (``""`` when none matches — normal for warm-pool runtimes before
      claim, cron scripts, and pooled MCP backends), and
    * the full HOST ancestor PID chain (peer first). gatewayd indexes stub
      connections under this chain so a later ``claim`` frame — which always
      carries the runtime's HOST pid — matches even when the stub's
      self-reported ``ancestor_pids`` are namespace-local (sandbox
      PID-namespace topology).

    The walk continues past a session-key match so the chain is complete for
    claim matching at any ancestry level.

    ``config_dir_fn`` / ``ppid_fn`` are injection seams for the callers'
    existing test surfaces; they default to the shared production
    implementations. Reads go through :func:`~kiro_crew.session_pid_sig.
    read_session_pid_txt` (symlink refusal, regular-file check, size bound) —
    the caller is a trusted process reading a predictable, agent-writable
    path, the exact symlink-planting surface that hardened reader closes.

    ``signed_only=True`` additionally requires each mapping's HMAC sidecar to
    verify (:func:`~kiro_crew.session_pid_sig.verify_session_pid`, pid bound
    into the MAC, keyed by the agent-unreadable SEL trust root). REQUIRED for
    any AUTHORIZATION use of the result: the bare ``.txt`` is same-uid
    agent-writable, so an unsigned mapping proves only what a local process
    chose to write — an attacker planting ``session_pid_<own_pid>.txt`` with
    a victim's key would otherwise turn kernel peer attestation into a
    self-serve identity oracle. gatewayd's stub-registration walk stays
    lenient (``False``): there the result only ATTRIBUTES a stub for
    claim-indexing, and warm-pool mappings may legitimately predate the SEL
    key.

    Blocking /proc + filesystem I/O — async callers MUST offload this to an
    executor (both consumers do).
    """
    if config_dir_fn is None:
        config_dir_fn = _default_config_dir
    if ppid_fn is None:
        ppid_fn = _default_ppid

    session_key = ""
    chain: list[int] = []
    try:
        cfg_dir = config_dir_fn()
    except Exception:
        return "", []

    pid = peer_pid
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        chain.append(pid)
        if not session_key:
            try:
                if signed_only:
                    session_key = verify_session_pid(pid, cfg_dir)
                else:
                    session_key = read_session_pid_txt(pid, cfg_dir)
            except OSError:
                pass
        try:
            pid = ppid_fn(pid)
        except (OSError, ValueError):
            # Target exited mid-walk (/proc/<pid>/stat gone or malformed).
            break
    return session_key, chain
