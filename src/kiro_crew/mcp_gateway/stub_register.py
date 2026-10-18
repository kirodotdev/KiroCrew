"""What a stub tells the gateway daemon about itself when it registers.

Owner module of :mod:`kiro_crew.mcp_gateway.stub`, which re-exports every name here;
read and patch them there. This module owns:

* the data home and the default socket path the stub resolves without loading config
  (:func:`_crew_home`, :func:`_default_socket_path`);
* decoding the target's command, args and env from the flags the rewriter wrote
  (:func:`_resolve_target_args`, :func:`_parse_env_json`, :func:`_parse_env_file`, ...);
* the target binary's identity for the pool key (:func:`_binary_version`,
  :func:`pool_binary_version`, :func:`binary_fingerprint`);
* the caller block: which session and process tree this stub serves
  (:func:`_build_caller_block`, :func:`_ancestor_pids`);
* :func:`build_register_payload`, the Register frame built from all of the above.

It logs under the stub's logger, defined in :mod:`kiro_crew.mcp_gateway.stub_bridge`. It never imports the facade.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Optional

from kiro_crew import platform_compat
from kiro_crew.mcp_caller import CallerContext, _parent_pid
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.mcp_gateway.hashing import (
    decode_target_args,
    hash_command,
    hash_effective_env,
    runs_install_code,
)
from kiro_crew.mcp_gateway.stub_bridge import logger

# Content-hash cap for ``binary_version``. Every shipped MCP is <1 MiB, so
# 4 MiB covers them with margin. Larger binaries
# (npm/Node/Java runtimes) are NOT hashed synchronously on the cold-start
# path — a 64 MiB hash blocked the stub event loop ~150-300 ms — they fall
# back to a cheap (size, mtime) token, which is still a stable pool-split key.
_BINARY_HASH_CAP_BYTES = 4 * 1024 * 1024
# Placeholder: stub does not yet observe a config snapshot, so all
# same-session stubs agree on this value (never a false split).
# Safety note: the config divergences a reader reaches for first --
# permission escalation and sandbox escape -- are not pool concerns. The
# sandbox is not applied to a gateway-spawned backend at all, and kiro-cli
# evaluates the permission surface per agent before a call reaches this
# process; the ``pool`` module docstring carries the full argument.
# TODO: Hash relevant config fields (e.g. tool allowlists,
# hook settings) in a future iteration to detect non-security config drift
# that could cause subtle behavioral differences across pooled sessions.
_CONFIG_SNAPSHOT_PLACEHOLDER = "0" * 64


def _crew_home() -> Path:
    """Data home the stub resolves its paths under.

    ``Path.home()`` rather than ``os.environ["HOME"]``: that variable is
    normally unset on Windows (which uses ``USERPROFILE``), so the previous
    fallback evaluated to ``Path("")`` and every derived path became RELATIVE to
    the stub's cwd. For the socket that is worse than untidy on Windows, where
    the pipe name is a hash of this path -- a daemon and a stub started from
    different working directories would hash to different pipe names and never
    meet. ``Path.home()`` consults the right variable on each platform.

    Never raises. ``Path.home()`` raises ``RuntimeError`` when no home can be
    resolved at all, and both callers are on paths that must not fail: the
    socket default is an argparse default (a raise there kills the stub before
    it can degrade to a per-session exec) and the log path is used by
    ``log_fallback``, whose handler catches ``OSError`` only.

    Deliberately not ``config.paths.config_dir()``: this module is on the stub's
    cold-start path and stays import-light.
    """
    home = os.environ.get("KIROCREW_HOME")
    if home:
        return Path(home)
    try:
        base = Path.home()
    except RuntimeError:
        base = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or ".")
    return base / ".kiro" / "crew"


def _default_socket_path() -> str:
    """Resolve the default gateway socket under KIROCREW_HOME (0700 dir)."""
    home = _crew_home()
    new_path = home / "kirocrew-mcp-gateway.sock"
    # Accept legacy socket name written by older versions.
    legacy_path = home / "mc-mcp-gateway.sock"
    if not new_path.exists() and legacy_path.exists():
        return str(legacy_path)
    return str(new_path)


def _split_target_args(raw: str, sep: str) -> list[str]:
    """Read delimiter-separated arguments from legacy overlays."""
    return raw.split(sep) if raw else []


def _resolve_target_args(args: argparse.Namespace) -> list[str]:
    """Share one decode between backend launch and the registered command hash."""
    encoded = getattr(args, "target_args_b64", None)
    if encoded is not None:
        return decode_target_args(encoded)
    return _split_target_args(args.target_args, args.target_args_sep)


def _resolve_pool_identity_env(args: argparse.Namespace) -> frozenset[str]:
    """Decode names only; the daemon independently checks forwarding authority."""
    encoded = getattr(args, "pool_identity_env_b64", None)
    names = (
        decode_target_args(encoded)
        if encoded is not None
        else _split_target_args(args.pool_identity_env, args.target_args_sep)
    )
    return frozenset(n for n in names if n)


def _parse_env_csv(raw: str) -> dict[str, str]:
    """Parse ``K=V,K2=V2``; malformed fragments are skipped.

    DEPRECATED: values containing ``,`` get truncated. New callers should
    use ``--env-json`` which round-trips cleanly through
    :func:`_parse_env_json`.
    """
    if not raw:
        return {}
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        if k:
            out[k] = v
    return out


def _parse_env_json(raw: str) -> dict[str, str]:
    """Parse a JSON-encoded env dict. Returns ``{}`` on empty / malformed.

    Preferred over :func:`_parse_env_csv` because values with ``,`` or
    ``=`` round-trip intact. Non-string values are coerced via ``str()``.
    """
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("stub --env-json malformed; dropping env block")
        return {}
    if not isinstance(decoded, dict):
        logger.warning("stub --env-json not a JSON object; dropping env block")
        return {}
    return {str(k): str(v) for k, v in decoded.items() if k}


def _parse_env_file(path: str) -> dict[str, str]:
    """Read a JSON env dict from ``path`` (a 0600 sidecar). Returns ``{}``
    on missing/malformed. Keeps env secrets off argv; same coercion as
    :func:`_parse_env_json`.
    """
    if not path:
        return {}
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        logger.warning("stub --env-file unreadable; dropping env block")
        return {}
    return _parse_env_json(raw)


def _parse_auto_approve(raw: str) -> list[str]:
    if not raw:
        return []
    # New form: JSON array (escape-safe for tool names containing commas).
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(s) for s in parsed if s]
    except ValueError:
        pass
    # Back-compat: legacy CSV form.
    return [s for s in raw.split(",") if s]


def _hash_permission_profile(auto_approve: list[str], approval_mode: str, trust_all: bool) -> str:
    """Hash ``(autoApprove sorted, approval_mode, trust_all)`` for the
    ``autoapprove_set_hash`` register field.

    Wire-compat ballast for an adopted daemon that still keys on it, which is
    why the encoding stays injective. A current daemon ignores the field --
    see the ``pool`` module docstring on why a permission surface kiro-cli
    evaluates per agent, before any call reaches this process, does not
    partition the pool."""
    h = hashlib.sha256()
    for tool in sorted(auto_approve):
        h.update(tool.encode("utf-8"))
        h.update(b"\0")  # NUL delimiter: injective — cannot occur in a tool name,
        #                  so ["a,b"] and ["a","b"] cannot collide onto one key.
    h.update(b"mode=")
    h.update(approval_mode.encode("utf-8"))
    h.update(b"\0trust_all=")
    h.update(b"1" if trust_all else b"0")
    return h.hexdigest()


def _binary_version(command: str) -> str:
    """Return a stable version token for the target binary, or ``"unknown"``.

    Content-hashes binaries up to :data:`_BINARY_HASH_CAP_BYTES`; larger ones
    use a cheap ``(size, mtime)`` token so the stub cold-start path never
    blocks on a multi-MiB synchronous hash.
    """
    try:
        real = os.path.realpath(shutil.which(command) or command)
        if not os.path.isfile(real):
            return "unknown"
        st = os.stat(real)
        if st.st_size > _BINARY_HASH_CAP_BYTES:
            return f"sz{st.st_size}-mt{int(st.st_mtime)}"
        h = hashlib.sha256()
        with open(real, "rb") as f:
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()[:24]
    except OSError:
        return "unknown"


#: Kiro Crew's own MCP servers, as the subcommand a stub's target args name.
#: For these the target binary is the ``kirocrew`` console-script shim, whose
#: bytes never change across a ``git pull`` of an editable install, so its
#: hash alone let a pooled backend from a two-day-old checkout keep answering
#: stubs from a freshly restarted gateway. The package's own fingerprint is
#: folded in for exactly these, and only these: a third-party MCP binary is
#: what its bytes say it is, and re-partitioning its pool on every Kiro Crew
#: commit would cold-start it for no reason.
#:
#: Spelled out rather than imported from ``mcp_discovery``, whose module-level
#: cost the stub's timed cold-start path must not pay; a ratchet test pins this
#: set equal to ``mcp_discovery._MANAGED_SERVER_SUBCOMMANDS``'s values. The set
#: also decides which stubs run the daemon-generation check in
#: :func:`handshake`, so a managed server missing here keeps attaching to a
#: pre-fingerprint daemon after an upgrade -- and the caller-aware ones then
#: refuse every call as ``identity_unattested``.
_KIROCREW_MCP_SUBCOMMANDS = frozenset(
    {
        "mcp-core",
        "mcp-cron",
        "mcp-work",
        "mcp-computer",
        "mcp-dashboard",
        "mcp-crew-log",
        "mcp-debug",
        "mcp-panel",
        "mcp-guide",
        "mcp-mate",
    }
)


def _pool_binary_identity(command: str, target_args: list[str]) -> tuple[str, str]:
    """Return ``(pool version, argv-selected Crew generation)`` for one target.

    The second value is empty unless the target argv names a managed Crew
    subcommand. :func:`build_register_payload` separately validates ``--server``
    before putting that value on the wire; the pool version deliberately keeps
    its existing argv-based identity.
    """
    base = _binary_version(command)
    # Two ways a target runs Kiro Crew's own code: one of the reserved
    # subcommands, or the gateway interpreter pinned by ``apps/bridges_mcp.py``
    # with ``-m kiro_crew...`` / the deps_boot shim in argv
    # (``hashing.runs_install_code``). The second needs the fold here too: with
    # the versioned directory gone from ``command_args_hash``, this token is
    # the only identity dimension left that can see the code change. A
    # third-party server that merely runs ON that interpreter is excluded, so
    # its pool is not re-partitioned on every Kiro Crew commit.
    managed = any(a in _KIROCREW_MCP_SUBCOMMANDS for a in target_args)
    if not managed and not runs_install_code(command, target_args):
        return base, ""
    # Imported here, not at module top: the stub's cold-start path is timed
    # and this module is only needed on the Kiro Crew branch.
    from kiro_crew.code_fingerprint import code_fingerprint

    fingerprint = code_fingerprint()
    # The generation stays argv-selected: only a managed subcommand runs the
    # daemon-generation check, so an install-code app bridge reports none.
    return f"{base}+{fingerprint}", (fingerprint if managed else "")


def pool_binary_version(command: str, target_args: list[str]) -> str:
    """The ``binary_version`` a stub registers: the binary's hash, plus the Kiro
    Crew code fingerprint when the target is one of Kiro Crew's own servers.
    """
    return _pool_binary_identity(command, target_args)[0]


def binary_fingerprint(command: str) -> str:
    """Public alias of :func:`_binary_version`.

    The shareability cache keys on the same token the pool does, so an in-place
    binary upgrade invalidates both. One implementation, two consumers — a second
    copy would drift and let one of them keep a stale identity.
    """
    return _binary_version(command)


def _resolve_channel_id(cli_value: Optional[str]) -> Optional[str]:
    """``--channel-id`` wins; else ``KIROCREW_CHANNEL_ID`` env; else None."""
    return cli_value or os.environ.get("KIROCREW_CHANNEL_ID") or None


#: Ancestor-walk depth cap for the Register ``ancestor_pids`` chain. The real
#: tree is ~4 deep (sandbox wrapper → kiro-cli → kiro-cli-chat → stub); 10
#: leaves headroom without letting a pathological /proc loop run away.
_ANCESTOR_WALK_MAX = 10


def _ancestor_pids() -> list[int]:
    """Ancestor PID chain of this stub, nearest parent first.

    Uses :func:`kiro_crew.mcp_caller._parent_pid`, which delegates to
    ``platform_compat.get_ppid``: ``/proc`` on Linux, libproc on macOS,
    ``CreateToolhelp32Snapshot`` on Windows -- none of which spawns a
    subprocess. Stops at PID 1, a
    lookup failure, or the depth cap. Always contains at least
    ``os.getppid()`` when resolvable.
    """

    chain: list[int] = []
    pid = os.getppid()
    seen: set[int] = set()
    while pid > 1 and pid not in seen and len(chain) < _ANCESTOR_WALK_MAX:
        chain.append(pid)
        seen.add(pid)
        pid = _parent_pid(pid)
    return chain


def _build_caller_block(channel_id: Optional[str]) -> dict[str, str]:
    """Assemble caller-identity from ``KIROCREW_*`` env vars.

    ``session_key`` is resolved via :meth:`CallerContext.from_env`, which
    reads ``KIROCREW_SESSION_KEY`` first and falls back to the warm-pool PID
    file (``config_dir()/session_pid_<pid>.txt``) by walking the process
    ancestry. Warm-pool kiro-cli is pre-spawned with NO session key (the key
    is only known once the session is claimed), so a bare env read here would
    register an empty caller and gatewayd would stamp ``caller=None`` on every
    forwarded call — silently breaking state-mutating tools (``learn_add`` et
    al.) that need session identity. Sharing the backend-side resolver keeps
    both ends of the wire in agreement. If the key is still unknown at register
    (claim hasn't happened yet), the recaller loop repairs it later."""

    session_key = CallerContext.from_env().session_key
    # Diagnostic identity only — the OS user. USERNAME is the Windows spelling
    # of USER; check both so this dimension is not empty on one platform.
    principal = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    if session_key.startswith("cron:"):
        session_type = "cron"
    elif session_key.startswith("hook:"):
        session_type = "hook"
    elif session_key.startswith("dashboard:"):
        session_type = "dashboard"
    elif session_key:
        session_type = "slack-thread"
    else:
        session_type = "unknown"
    return {
        "session_key": session_key,
        "session_type": session_type,
        "principal_id": principal,
        "channel_id": channel_id or "",
    }


def build_register_payload(args: argparse.Namespace) -> dict:
    """Compute every PoolKey field and assemble the Register frame.

    The result is accepted verbatim by :meth:`PoolKey.from_register` —
    callers do not post-process."""

    target_args = _resolve_target_args(args)
    binary_version, stub_code_fingerprint = _pool_binary_identity(args.target_command, target_args)
    # Prefer --env-json when present (commas/equals round-trip intact);
    # fall back to the legacy --env CSV for overlay files written by a
    # pre-JSON rewriter that may still be on disk during the transition.
    if args.env_file:
        env_pairs = _parse_env_file(args.env_file)
    elif args.env_json:
        env_pairs = _parse_env_json(args.env_json)
    else:
        env_pairs = _parse_env_csv(args.env)
    auto_approve = _parse_auto_approve(args.auto_approve)
    channel_id = _resolve_channel_id(args.channel_id)
    identity_keys = _resolve_pool_identity_env(args)

    try:
        work_dir = str(Path(args.work_dir).resolve())
    except OSError:
        work_dir = str(args.work_dir)

    caller = _build_caller_block(channel_id)
    # Per-session token from the injected ACP entry's own env
    # (``session_servers.attach_stub_session_token``). Absent for a stub
    # launched from a hand-written config or an overlay predating the token: the
    # field is then omitted below and gatewayd keeps its PID-keyed behavior.
    session_token = os.environ.get(STUB_SESSION_TOKEN_ENV, "")

    payload = {
        "type": "register",
        "stub_uuid": str(uuid.uuid4()),
        "server_name": args.server,
        # NOT a pool dimension — the agent name never reaches the backend
        # process, so two agents declaring one server identically share it (see
        # the ``pool`` module docstring). LOAD-BEARING ANYWAY, and this list is
        # what a future removal has to answer to. A daemon reads it off the
        # frame (``daemon.connection``'s ``stub_agent``) for three things:
        #   * a daemon predating the pool-dimension removal runs a
        #     ``PoolKey.from_register`` that hard-requires the key and would
        #     reject every new stub as malformed;
        #   * it names the declared-env sidecar a connection-PRIVATE backend
        #     reads (``daemon.launch._read_declared_env_sidecar``), so dropping
        #     it would start such a backend with no declared env; and
        #   * it is stamped into an intercepted app render's spool record
        #     (``Backend.agent_for_stub`` -> ``backend._fetch_and_deliver_ui``),
        #     which is the governance identity of that render's callbacks, so
        #     dropping it would have every app callback refused.
        "agent_name": args.agent,
        "command_args_hash": hash_command(args.target_command, target_args),
        "effective_env_hash": hash_effective_env(env_pairs, identity_keys=identity_keys),
        "work_dir": work_dir,
        "binary_version": binary_version,
        # Not os.getuid(): that attribute does not exist on Windows, where an
        # AttributeError here would abort the Register frame and send every
        # session to per-session exec -- pooling would appear enabled and
        # never take effect. local_user_id() is the uid on POSIX and a
        # SID-derived int on Windows, so the PoolKey dimension keeps both its
        # type and its partitioning meaning.
        "os_uid": platform_compat.local_user_id(),
        # Wire-compat ballast, NOT pool dimensions — same treatment as
        # ``user_identity`` below, and for the same reason: an adopted daemon
        # predating their removal runs a ``PoolKey.from_register`` that
        # hard-requires all four, so omitting them would make that daemon
        # reject every register as malformed and un-pool the whole install.
        # A current daemon ignores them; the ``pool`` module docstring records
        # why none of them isolates anything (the sandbox is not applied to a
        # pooled backend, and kiro-cli decides approval per agent before a
        # call reaches this process). Safe to drop once no daemon predating
        # their removal can be adopted.
        "sandbox_mode": args.sandbox_mode,
        "autoapprove_set_hash": _hash_permission_profile(
            auto_approve, args.approval_mode, bool(args.trust_all)
        ),
        "approval_mode": args.approval_mode,
        "trust_all_tools": bool(args.trust_all),
        # Not a PoolKey dimension: it selects HOW the backend is acquired
        # (shared bucket vs this connection's own), not WHICH backends are
        # interchangeable. Keeping it out of the key is what lets a
        # per-connection backend exist without making every key
        # connection-private.
        "poolable": bool(args.poolable),
        # Wire-compat ballast, NOT a pool dimension: an adopted daemon that
        # outlived a package upgrade (the manager adopts anything answering
        # ``pong`` with no version handshake — see the capability comment in
        # ``daemon/connection.py``) still runs a ``PoolKey.from_register`` that hard-requires
        # ``user_identity``. Omitting the key would make that daemon reject
        # every new stub's register as malformed, silently un-pooling the
        # whole install until the daemon restarts. A current daemon ignores
        # the key. Safe to drop once no daemon predating the key can be adopted.
        "user_identity": caller["principal_id"] or "unknown",
        "channel_id": channel_id,
        # Wire-compat ballast on the same terms as ``user_identity`` above, and
        # inert from the start: this has always been a constant run of 64 zeros,
        # so it never partitioned anything. Deleted as a pool dimension; still
        # sent so a daemon predating the deletion keeps accepting this register.
        "config_snapshot_hash": _CONFIG_SNAPSHOT_PLACEHOLDER,
        "caller": caller,
        # Claim-push (gateway → gatewayd ``claim`` frame): the ancestor PID
        # chain of this stub, nearest first. gatewayd indexes the connection
        # under EVERY ancestor so a claim naming any level of the runtime's
        # process tree hits. The chain matters because the PID the gateway
        # records for a runtime (``AcpClient._process.pid``) can sit several
        # layers above the stub's immediate parent — e.g.
        # sandbox-wrapper → kiro-cli → kiro-cli-chat → stub — and a
        # single-PID index would never match (found live: claim frames
        # applied to 0 connections).
        "ancestor_pids": _ancestor_pids(),
        # Flat mirror — gatewayd accepts either shape; flat wins on
        # log/diff tooling legibility.
        "session_key": caller["session_key"],
        "session_type": caller["session_type"],
        "principal_id": caller["principal_id"],
    }
    if stub_code_fingerprint and args.server in KIROCREW_BIN_MCP_SERVERS:
        # Compatibility attestation, not a PoolKey dimension. The daemon answers
        # with its own fingerprint in ``registered``; an absent or different
        # value means an adopted pre-upgrade daemon cannot safely carry this
        # control plane's current per-session protocol, so handshake requests the
        # existing direct-exec fallback instead of serving subtly stale frames.
        payload["stub_code_fingerprint"] = stub_code_fingerprint
    if session_token:
        # Sibling field, deliberately NOT a PoolKey dimension: a per-connection
        # value in the key would give every session its own backend and pooling
        # would silently stop (see the ``pool`` module docstring). The token says
        # WHICH session this connection belongs to, never which backends are
        # interchangeable.
        payload["stub_session_token"] = session_token
    return payload
