"""Code-based cron scripts — deterministic Python as cron callbacks.

Scripts under ``<config_dir>/crons/`` are LLM-writeable by design. The sandbox +
path-restriction prevents filesystem escape, but the LLM can register
self-written scripts. Mitigations: SEL audit trail on every invocation,
is_sensitive_path() blocks credential files, auto-pause after 5 consecutive
failures, concurrent execution guard prevents double-fire.

Usage:
    # <config_dir>/crons/my_monitor.py
    from kiro_crew.cron_script import Skip, Done

    def run(ctx):
        data = ctx.call_tool("kirocrew-core", "local_knowledge_search", {"query": "..."})
        if not ready(data):
            raise Skip()  # silent, retry next tick
        ctx.notify("Done: " + summary)
        raise Done()  # remove cron job
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew import platform_compat
from kiro_crew.agent_discovery import _read_agent_spec
from kiro_crew.config.loader import config_dir, read_local_secret
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.env import sanitize_spec_env
from kiro_crew.github_runner import prevalidated_gh_env
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.port_resolution import resolve_serving_port
from kiro_crew.sandbox import (
    _AGENT_DENIED_ENV_KEYS,
    SandboxUnavailableError,
    cgroup_scope_argv,
    popen_limited,
    run_limited,
    wrap_argv,
)
from kiro_crew.secrets import SecretVault
from kiro_crew.security import is_sensitive_path, redact
from kiro_crew.sel import sel

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
    """Return os.environ minus the cron env-deny set (secrets never inherited)."""
    return {
        k: v
        for k, v in os.environ.items()
        if k not in _CRON_ENV_DENY and k not in _GRANTED_ENV_KEYS
    }


# ---------------------------------------------------------------------------
# Operator-granted vault secrets for SCRIPT crons (script jobs only).
#
# A grant maps an env-var NAME -> a vault secret NAME (kiro_crew.secrets
# SecretVault). It becomes ACTIVE only through the operator-approved request
# flow (agent requests, owner approves on the Schedule page); no surface
# lets an agent grant itself vault access. At fire time the secrets are
# resolved in-memory in the runner and delivered over the child's stdin
# (never the execve env), and never persisted in plaintext.
#
# The grant is pinned to the job's code: keyed HMAC over the script body (plus
# the script spec), computed when the operator grants — SCRIPT jobs only. Scripts
# under <config_dir>/crons/ are agent-writeable by design, so without the pin
# a granted job's body could be rewritten into an exfiltrator after approval.
# A pin mismatch fails the run closed — no injection, no fallback run — until
# the operator re-approves.
# ---------------------------------------------------------------------------

#: Grant env-var names: conventional uppercase env grammar only.
_SECRET_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
#: Grammar for the VALUE half of a grant map (the vault secret name). The
#: agent supplies it and the owner view + approval errors echo it, so it must
#: never be able to carry credential-shaped or URL-shaped content.
_SECRET_ENV_VAULT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: Names a grant may never use, beyond the always-scrubbed _CRON_ENV_DENY:
#: process-behavior variables that would let an injected value alter HOW the
#: child runs (loader hijack, import shadowing, shell startup) rather than
#: merely being data the script reads.
_SECRET_ENV_DENIED_EXACT: frozenset[str] = (
    frozenset({"PATH", "HOME", "SHELL", "TMPDIR", "IFS", "ENV", "BASH_ENV"}) | _CRON_ENV_DENY
)
_SECRET_ENV_DENIED_PREFIXES: tuple[str, ...] = (
    "KIROCREW",  # product-internal, incl. _KIROCREW_* dial/secret plumbing
    "_KIROCREW",
    "LD_",  # ELF loader (LD_PRELOAD / LD_LIBRARY_PATH)
    "DYLD_",  # macOS loader
    "PYTHON",  # PYTHONPATH / PYTHONSTARTUP would shadow the launcher's imports
)

#: Cap mirrors the intent of the per-field caps in cron.py: a grant is a small
#: hand-written map, not a bulk store.
_SECRET_ENV_MAX_ENTRIES = 16


def validate_secret_env_grant(secret_env: dict[str, str]) -> None:
    """Validate a secret grant map (env-var name -> vault secret name).

    Raises ValueError naming the offending KEY only — a vault secret name is
    operator data and may be echoed, but keep messages key-first for
    consistency with the resolver's no-echo discipline.
    """
    if len(secret_env) > _SECRET_ENV_MAX_ENTRIES:
        raise ValueError(
            f"secret_env holds {len(secret_env)} entries; max {_SECRET_ENV_MAX_ENTRIES}"
        )
    for key, name in secret_env.items():
        if not isinstance(key, str) or not _SECRET_ENV_NAME_RE.match(key):
            raise ValueError(
                f"secret_env key {key!r} is not a valid env-var name " "(expected [A-Z][A-Z0-9_]*)"
            )
        if key in _SECRET_ENV_DENIED_EXACT or any(
            key.startswith(p) for p in _SECRET_ENV_DENIED_PREFIXES
        ):
            raise ValueError(f"secret_env key {key!r} is a protected env-var name")
        if (
            not isinstance(name, str)
            or not name
            or name != name.strip()
            or not _SECRET_ENV_VAULT_NAME_RE.match(name)
        ):
            # Mirrors the vault storage boundary (names are stripped on store)
            # AND constrains the value to slug grammar: the grant map is
            # agent-supplied and its vault-name half is echoed to the owner
            # view and into approval error messages, so an unconstrained
            # string would be an unredacted channel for credential-shaped or
            # URL-shaped content. A name that cannot match can never resolve
            # to a sanely-named vault entry anyway.
            raise ValueError(
                f"secret_env entry {key!r} must reference a vault secret name "
                "matching [A-Za-z0-9][A-Za-z0-9._-]* (max 128 chars)"
            )


def _grant_pin_key() -> bytes:
    """HMAC key for grant code pins, derived from the agent-fenced vault key.

    A plain hash pin can be recomputed by anything that can write the cron
    store — on a host whose OS sandbox backend degrades to "none", a cron
    child is an ordinary subprocess and the store file is reachable — so the
    pin must be unforgeable, not merely collision-resistant. The key is a
    purpose-scoped derivation of the EXISTING vault key (a keystone leaf under
    ``<config_dir>/.vault/`` the agent's tools cannot read and sandboxed cron
    children never see), so there is no second key file, no separate birth
    race, and no separate corruption mode: the vault key's exclusive-create
    birth, fsync durability, and owner-only ACL are inherited. Raises
    ``ValueError`` when the vault store exists but its key is missing —
    grants fail closed rather than mint under a fresh key.
    """
    return SecretVault(config_dir()).derive_subkey("cron-grant-pin")


def _pin_digest(payload: bytes, *, domain: str, job_id: str, epoch: int | None = None) -> str:
    """Digest a pin payload according to its domain's trust model.

    ``pending`` pins are UNKEYED sha256: they authorize nothing (only the
    owner's approval mints the pin the runners honour) and they must be
    computable by the MCP server, whose sandbox hides the ``.vault`` dir —
    a keyed pending pin would make every agent request fail at mint time.
    Their job is integrity: approval recomputes the digest against the
    CURRENT code and refuses on drift, so what the operator blesses is what
    the agent showed them.

    ``active`` pins are HMAC-SHA256 under the vault-derived grant key AND
    include the job's grant EPOCH, so neither a forged store entry (no key)
    nor a replayed formerly-valid pin (dead epoch after any grant write)
    verifies at fire time.
    """
    if domain == "pending":
        return hashlib.sha256(payload).hexdigest()

    effective_epoch = _grant_epoch(job_id) if epoch is None else epoch
    epoch_payload = b"epoch\x00" + str(effective_epoch).encode() + b"\x00" + payload
    return hmac.new(_grant_pin_key(), epoch_payload, hashlib.sha256).hexdigest()


#: Size cap for a GRANTED script's body read (pin mint, approval re-verify,
#: fire-time verify all read the whole file). Two reasons, both load-bearing:
#: the file is agent-writeable, so an unbounded read_bytes() of a
#: multi-gigabyte sparse replacement would OOM the gateway — and the cap
#: MUST NOT exceed the dashboard's script-preview ceiling
#: (handlers/cron._SCRIPT_SOURCE_MAX_BYTES), because the approval signs the
#: whole body while the owner reviews only the preview: a grantable script
#: larger than the preview would let an agent hide executable tail bytes
#: behind the truncation and have the operator approve code they never saw.
_MAX_GRANTED_SCRIPT_BYTES = 256 * 1024


def _read_script_body(file_path: str) -> bytes:
    """Read a granted script's bytes through the guarded chokepoint.

    Routes through :func:`kiro_crew.hooks.safe_read_file_bytes_nolink`: the
    open is O_NOFOLLOW and inode-pinned (no hardlink/symlink swap between
    check and read), the opened descriptor must resolve inside the crons
    dir, and the read is bounded by ``_MAX_GRANTED_SCRIPT_BYTES``. Raises
    ``ValueError`` on any rejection — every grant path fails closed rather
    than minting or verifying a pin for bytes it could not safely read.
    """
    try:
        body = safe_read_file_bytes_nolink(
            file_path,
            str(config_dir() / "crons"),
            max_bytes=_MAX_GRANTED_SCRIPT_BYTES,
        )
    except FileTooLargeError as exc:
        raise ValueError(str(exc)) from exc
    if body is None:
        raise ValueError("script unreadable, hardlinked, or outside the crons dir — grant refused")
    return body


def _grant_epochs_path() -> Path:
    return config_dir() / ".vault" / ".grant_epochs.json"


def _read_grant_epochs(path: Path) -> dict[str, Any]:
    """The epochs map; a MISSING file is ``{}``, a corrupt file RAISES.

    The distinction is load-bearing: epochs only ever go up, so a counter
    that silently restarts at 0 would let a pin an agent saved under a low
    epoch verify again once new grants cycle the counter back through it —
    replaying a revoked grant. A file that exists but cannot be parsed
    therefore fails closed (``ValueError``) instead of reading as empty;
    only the genuinely-never-granted state (no file at all) reads 0.
    """
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ValueError(f"grant-epoch state unreadable: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError(
            "grant-epoch state corrupt (invalid JSON); granted runs are "
            "refused until the owner restores or removes it"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError("grant-epoch state corrupt (not an object)")
    return data


def _grant_epoch(job_id: str) -> int:
    """Current grant epoch for a job (0 when never granted/revoked).

    Epochs live under the agent-fenced ``.vault`` dir, OUTSIDE the
    agent-writable cron store: an agent that saved a formerly-valid
    mapping+pin and writes them back into the store after a revoke gets a
    pin minted under a dead epoch, and the runner refuses it. Only the
    gateway's own grant paths can bump an epoch. Raises ``ValueError`` on
    corrupt epoch state (see :func:`_read_grant_epochs`) — every caller
    (pin verification, mint, bump) then fails closed.
    """
    data = _read_grant_epochs(_grant_epochs_path())
    try:
        return int(data.get(job_id, 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("grant-epoch state corrupt (non-integer epoch)") from exc


def peek_grant_epoch(job_id: str) -> int:
    """The epoch the NEXT grant write will commit (current + 1), no write.

    Grant flows mint the new pin under this value BEFORE touching the store,
    and commit it (:func:`commit_grant_epoch`) only after the store swap
    succeeds — so a refusal or failure anywhere in between leaves the
    existing grant state untouched, and a crash between swap and commit
    leaves the NEW grant failing closed (re-approve fixes it) rather than
    ever leaving a dead pin on a grant the operation did not replace.
    """
    return _grant_epoch(job_id) + 1


def grant_epoch_ids() -> set[str]:
    """Job ids carrying a committed grant epoch entry.

    Job-removal paths use this to decide which deleted ids must bump: an id
    with an epoch entry once had an ACTIVE pin minted under it (only the
    approval path commits entries), so a saved copy of that record stays
    replayable until the entry is bumped — even when the agent-writable
    store no longer shows grant fields. Raises on corrupt epoch state
    (fail closed, same as :func:`bump_grant_epoch`).
    """
    path = _grant_epochs_path()
    with _grant_epochs_guard():
        return set(_read_grant_epochs(path))


#: Serializes grant-epoch read-modify-writes ACROSS THREADS in this process;
#: the cross-process half of the guarantee is the flock in
#: :func:`_grant_epochs_guard`. Both are needed: job-removal paths bump
#: epochs and removals also run from the CLI (``kirocrew cron remove``), so
#: the writer set is no longer one gateway process — a thread lock alone
#: would let a gateway revoke and a CLI removal read one epoch map and
#: overwrite each other's bump, reviving a revoked pin.
_GRANT_EPOCHS_LOCK = threading.Lock()


@contextmanager
def _grant_epochs_guard() -> Any:
    """Cross-process critical section for grant-epoch read-modify-writes.

    Takes the in-process thread lock, then an exclusive flock on a dedicated
    lockfile beside the epochs file (never the epochs file itself — it is
    atomically replaced, which would orphan a lock held on the old inode).
    A fresh descriptor per entry keeps the flock non-reentrant and simple;
    the thread lock in front prevents two threads of one process from
    blocking each other inside the flock wait.
    """
    lock_path = _grant_epochs_path().with_name(".grant_epochs.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _GRANT_EPOCHS_LOCK:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            platform_compat.acquire_lock(fd, exclusive=True)
            try:
                yield
            finally:
                platform_compat.release_lock(fd)
        finally:
            os.close(fd)


def _write_grant_epochs(path: Path, data: dict[str, Any]) -> None:
    """Durably replace the epochs file (fsync file, atomic rename, fsync dir).

    The fsyncs are part of the revoke fence, not politeness: a bump that a
    crash rolls back would revive every pin minted under the old epoch, so
    the write must be on disk — and the rename reachable — before the caller
    reports the revoke/grant as done. Directory fsync is best-effort where
    the platform has no ``O_DIRECTORY`` (Windows).
    """
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    dir_flag = getattr(os, "O_DIRECTORY", None)
    if dir_flag is None:
        return
    try:
        dfd = os.open(str(path.parent), os.O_RDONLY | dir_flag)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def commit_grant_epoch(job_id: str, value: int, *, expected_current: int | None = None) -> bool:
    """Persist a job's grant epoch; returns ``False`` on a lost race.

    ``expected_current`` makes the commit compare-and-swap: pass the epoch
    :func:`peek_grant_epoch` was based on, and a stored value that has moved
    since (a concurrent revoke or job removal bumped it) REFUSES the commit
    instead of silently re-committing the very value that bump minted — the
    caller must then kill its own just-minted pin (bump once more) and
    surface the conflict. Without the check, an approval racing a removal
    could commit the removal's bumped epoch and leave the deleted grant
    record replayable. ``None`` skips the check (unconditional write).

    Corrupt epoch state raises (:func:`_read_grant_epochs`) rather than
    being replaced with a fresh map — overwriting it would reset every
    OTHER job's counter and revive their revoked pins.
    """
    path = _grant_epochs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _grant_epochs_guard():
        data = _read_grant_epochs(path)
        if expected_current is not None:
            try:
                current = int(data.get(job_id, 0))
            except (TypeError, ValueError) as exc:
                raise ValueError("grant-epoch state corrupt (non-integer epoch)") from exc
            if current != expected_current:
                return False
        data[job_id] = int(value)
        _write_grant_epochs(path, data)
    return True


def bump_grant_epoch(job_id: str) -> int:
    """Advance a job's grant epoch; every previously minted active pin dies.

    REVOKE calls this BEFORE clearing the store: if the clear then fails,
    the store still names a grant but its pin is dead under the new epoch —
    the failure direction is fail-closed, never a still-live revoked secret.
    Grant/replace/promote flows use peek+commit instead (see
    :func:`peek_grant_epoch`) so a refused operation never touches the
    epoch of a grant it did not replace. The whole read-increment-write runs
    under the cross-process epochs guard so overlapping bumps — including
    one from a CLI removal in another process — cannot collapse into one.
    Corrupt epoch state raises instead of restarting the counter at 0 —
    under that state every granted run already refuses, so the failure
    direction stays closed (see :func:`_read_grant_epochs`).
    """
    path = _grant_epochs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _grant_epochs_guard():
        data = _read_grant_epochs(path)
        try:
            value = int(data.get(job_id, 0)) + 1
        except (TypeError, ValueError) as exc:
            raise ValueError("grant-epoch state corrupt (non-integer epoch)") from exc
        data[job_id] = value
        _write_grant_epochs(path, data)
    return value


def delivery_fingerprint(
    session_key: str = "",
    silent: bool = False,
    channel: str = "",
    thread_ts: str = "",
) -> str:
    """Canonical fingerprint of a job's DELIVERY controls, for pin binding.

    Covers every agent-mutable field that decides where (session_key,
    channel, thread_ts) or whether (silent) a run's output is delivered:
    binding them one by one invites the next omission, so the pin binds
    this single canonical blob instead. Rewiring ANY of them under a
    still-valid pin fails the run closed and asks for re-approval.
    """
    return json.dumps(
        {
            "channel": channel or "",
            "session_key": session_key or "",
            "silent": bool(silent),
            "thread_ts": thread_ts or "",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _pin_payload(
    script: str,
    command: str,
    message: str,
    body: bytes | None,
    job_id: str,
    grant: dict[str, str] | None,
    domain: str,
    delivery: str = "",
) -> bytes:
    """Canonical byte payload for a grant pin (see compute_secret_env_pin).

    ``delivery`` is the job's delivery-control fingerprint
    (:func:`delivery_fingerprint`) — which session and channel receive the
    run's output, and whether delivery happens at all. It is bound into the
    pin because the store is agent-writable: without it an agent could
    rewrite the delivery controls under a still-valid pin and steer or
    surface an approved script's granted output somewhere the operator
    never blessed.
    """
    canonical_grant = json.dumps(grant or {}, sort_keys=True, separators=(",", ":")).encode()
    head = (
        b"v4\x00"
        + domain.encode()
        + b"\x00"
        + job_id.encode()
        + b"\x00"
        + delivery.encode()
        + b"\x00"
        + canonical_grant
    )
    if script:
        assert body is not None
        return (
            head + b"\x00script\x00" + script.encode() + b"\x00" + message.encode() + b"\x00" + body
        )
    # Command jobs are refused BY DESIGN: a pin over the command TEXT cannot
    # cover the bytes of a helper file the command invokes (`bash helper.sh`
    # runs whatever the agent last wrote there under a still-valid pin), so a
    # command grant offers script-grade assurance in name only.
    raise ValueError("secret grants apply only to script jobs")


def compute_secret_env_pin(
    script: str,
    command: str,
    message: str = "",
    *,
    job_id: str = "",
    grant: dict[str, str] | None = None,
    domain: str = "active",
    epoch: int | None = None,
    body: bytes | None = None,
    delivery: str = "",
) -> str:
    """Pin a grant to the job's current code; returns a hex digest.

    ``pending``-domain pins are unkeyed SHA-256 (computable by the sandboxed
    MCP server, integrity-only); ``active``-domain pins are HMAC-SHA256
    under the vault-derived grant key with the grant epoch bound in — see
    :func:`_pin_digest` for the trust model of each domain.

    The pin binds, in one digest: the DOMAIN (``pending`` for an
    agent-requested grant awaiting approval, ``active`` for an operator-minted
    grant the runners honour — so a pending pin copied verbatim into the
    active fields never verifies), the JOB ID (a pin cannot be replayed onto
    another job), the DELIVERY FINGERPRINT (session_key/silent/channel/thread_ts — rewiring where or whether the
    run's output lands under a still-valid pin fails the run instead), the
    canonical GRANT MAPPING (swapping which secrets flow
    under an existing pin breaks it), and the job's code. Script jobs pin the
    script SPEC, the job ``message``, and the file's current bytes — the
    message is included because a script reads it as its ARGUMENTS
    (``ctx.message``: a channel, a URL, a query) and it is agent-updatable via
    ``cron_update``, so leaving it unpinned would let an approved script be
    re-aimed at an unapproved destination. Command jobs are refused (see
    :func:`_pin_payload`). The digest is
    HMAC-SHA256 under the vault-fenced grant key (see :func:`_grant_pin_key`),
    so a pin cannot be forged by editing the cron store — only the product's
    own grant paths can mint one. Raises
    (FileNotFoundError/PermissionError/ValueError) when the script spec does
    not resolve — a grant must never be minted for code that cannot be read.

    ``body`` lets a caller pin SPECIFIC script bytes it already read: two pins
    derived for one decision (verify the pending, mint the active) MUST come
    from one snapshot, or an agent swapping the file between two reads would
    get code the approver never saw blessed with the active pin.
    """
    if script:
        if body is None:
            file_path, _func = resolve_script_path(script)
            body = _read_script_body(file_path)
    return _pin_digest(
        _pin_payload(script, command, message, body, job_id, grant, domain, delivery),
        domain=domain,
        job_id=job_id,
        epoch=epoch,
    )


def _resolve_secret_env(secret_env: dict[str, str]) -> dict[str, str]:
    """Resolve a validated grant map to plaintext values from the vault.

    Fail-closed: a missing vault entry raises ValueError naming the env-var
    KEY (never echoing the secret name — same CWE-117 discipline as
    mcp_gateway.secret_uri). Runs in the cron pool worker thread, so the
    blocking vault file read is off the event loop.
    """
    vault = SecretVault(config_dir())
    fetched = vault.get_many(list(secret_env.values()))
    resolved: dict[str, str] = {}
    for key, name in secret_env.items():
        value = fetched.get(name)
        if value is None:
            raise ValueError(
                f"cron secret grant for env var {key!r} references a vault "
                "secret that does not exist. Store it under Settings > Secrets "
                "in the dashboard, or update the grant."
            )
        resolved[key] = value.reveal()
    return resolved


def _filter_grant_env(resolved: dict[str, str]) -> dict[str, str]:
    """Drop protected keys from a resolved grant (defense in depth).

    Grants are validated at persistence; this re-check only guards a store
    edited outside the product. It runs on the delivery path — the script
    runner's stdin payload — so a
    grant can never name a product-internal key (``_KIROCREW*``) or a loader
    variable regardless of how it reaches the child. The skip log carries a
    COUNT only: the env-var names here flow from the same mapping as the
    secret values, so logging one keeps tripping taint scanners, and the
    operator can read the offending names from their own grant table in the
    dashboard.
    """
    kept: dict[str, str] = {}
    skipped = 0
    for key, value in resolved.items():
        if (
            not _SECRET_ENV_NAME_RE.match(key)
            or key in _SECRET_ENV_DENIED_EXACT
            or any(key.startswith(p) for p in _SECRET_ENV_DENIED_PREFIXES)
        ):
            skipped += 1
            continue
        kept[key] = value
    if skipped:
        logger.warning("cron secret grant: skipped %d protected env key(s)", skipped)
    return kept


def _scrub_grant_values(text: str, resolved: dict[str, str]) -> str:
    """Replace every granted secret VALUE occurring in ``text`` with a marker.

    Pattern-based ``redact`` only recognises known credential shapes; a vault
    value has no required shape, so a script exception that embeds one (an
    HTTP client echoing its auth header, or ``raise Exception(token)``) would
    pass through untouched. The parent resolved the exact values to build the
    child's stdin payload, so it can scrub them precisely from any diagnostic
    it is about to return — the launcher's own status JSON included. Longest
    values first, so a value that contains another as a substring cannot leave
    a recognisable fragment behind.
    """
    if not text or not resolved:
        return text
    for value in sorted(resolved.values(), key=len, reverse=True):
        # Every nonempty value is scrubbed: a short secret is still a secret,
        # and a mangled diagnostic beats a leaked credential. The vault
        # refuses empty values, so the guard below is belt-and-braces.
        if value and value in text:
            text = text.replace(value, "[redacted-grant-value]")
    return text


def _secret_env_precheck(
    secret_env: dict[str, str] | None,
    secret_env_pin: str,
    script: str = "",
    command: str = "",
    script_body: bytes | None = None,
    message: str = "",
    job_id: str = "",
    delivery: str = "",
) -> tuple[dict[str, str], str | None]:
    """Verify the grant pin and resolve secrets; ``(resolved, error)``.

    ``script_body`` carries the bytes the caller already read (and will
    execute) so the pin covers exactly what runs — never a second read of a
    file an agent could swap between check and use. The pin is verified in the
    ``active`` domain with this job's id, delivery fingerprint, and mapping bound in
    (see :func:`compute_secret_env_pin`), so a pending pin, another job's pin,
    a rewired delivery session, or
    the same pin over a different mapping all fail closed.
    """
    if not secret_env:
        return {}, None
    if not secret_env_pin:
        return {}, "secret grant has no code pin; re-approve it in the dashboard"

    try:
        current = _pin_digest(
            _pin_payload(
                script, command, message, script_body, job_id, secret_env, "active", delivery
            ),
            domain="active",
            job_id=job_id,
        )
    except ValueError as exc:  # vault store without key — never inject
        return {}, str(exc)
    if not hmac.compare_digest(current, secret_env_pin):
        return {}, (
            "cron code changed (or its delivery session was rewired) since "
            "its secret grant was approved; secrets were NOT injected. "
            "Re-approve the grant in the dashboard (Schedule > job > "
            "Secrets) to run it again."
        )
    try:
        return _resolve_secret_env(secret_env), None
    except ValueError as exc:
        return {}, str(exc)


# ── Running-subprocess registry (user-initiated cancellation) ──
#
# Script/command crons run as blocking ``subprocess`` calls inside the cron
# thread executor — cancelling the owning asyncio task cannot interrupt them.
# Each sandboxed child is registered here (keyed by job id) so that
# ``CronService.cancel()`` can SIGTERM the whole process group mid-run.
_PROCS_LOCK = threading.Lock()
_RUNNING_PROCS: dict[str, subprocess.Popen] = {}
_CANCELLED_PROC_JOBS: set[str] = set()

_KILL_ESCALATION_GRACE_SECS = 5.0


def _register_proc(job_id: str, proc: subprocess.Popen) -> None:
    with _PROCS_LOCK:
        _RUNNING_PROCS[job_id] = proc


def _unregister_proc(job_id: str, proc: subprocess.Popen) -> bool:
    """Remove the registry entry; return True if this run was cancelled."""
    with _PROCS_LOCK:
        if _RUNNING_PROCS.get(job_id) is proc:
            _RUNNING_PROCS.pop(job_id, None)
        cancelled = job_id in _CANCELLED_PROC_JOBS
        _CANCELLED_PROC_JOBS.discard(job_id)
        return cancelled


def _resolve_safe_pgid(proc: subprocess.Popen) -> int | None:
    """Resolve *proc*'s process group id with broadcast protection.

    Returns None (caller must fall back to the direct Popen handle) unless
    every check passes:

    - ``proc.pid`` must be a real ``int`` > 1. A ``MagicMock`` pid coerces to
      1 via ``__index__``, and ``os.killpg(1, sig)`` is ``kill(-1, sig)`` in
      libc — a signal broadcast to EVERY process this uid can reach, which
      SIGKILLed the whole login session (systemd --user manager included).
    - The resolved pgid must be > 1 (same ``kill(-1)`` footgun) and must not
      be our own process group (suicide / killing the gateway tree).
    """
    if not platform_compat.IS_POSIX:
        # Windows has no process groups (os.getpgid/os.killpg don't exist);
        # callers fall back to platform_compat.kill_process_tree (taskkill /T).
        return None
    pid = getattr(proc, "pid", None)
    if type(pid) is not int or pid <= 1:
        logger.error("kill guard: refusing non-int/reserved pid %r", pid)
        return None
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return None
    if pgid <= 1 or pgid == os.getpgid(0):
        logger.error("kill guard: refusing broadcast/self pgid %d for pid %d", pgid, pid)
        return None
    return pgid


def kill_running_process(job_id: str) -> bool:
    """SIGTERM the sandboxed subprocess group for a running script/command cron.

    Escalates to SIGKILL after a grace period from a daemon thread so the
    caller (the async cancel path) never blocks. Returns True when a live
    subprocess was found and signalled.
    """
    with _PROCS_LOCK:
        maybe_proc = _RUNNING_PROCS.get(job_id)
        if maybe_proc is None or maybe_proc.poll() is not None:
            return False
        proc: subprocess.Popen = maybe_proc
        _CANCELLED_PROC_JOBS.add(job_id)
    pgid = _resolve_safe_pgid(proc)
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pgid = None
    if pgid is None:
        # Already gone (or unsignallable) on POSIX; on Windows there are no
        # process groups, so reap the whole tree via taskkill /T
        # (platform_compat) before falling back to a single-process terminate.
        killed_tree = False
        if not platform_compat.IS_POSIX:
            try:
                platform_compat.kill_process_tree(proc.pid, platform_compat.SIGTERM)
                killed_tree = True
            except (OSError, ProcessLookupError):
                killed_tree = False
        if not killed_tree:
            try:
                proc.terminate()
            except Exception:
                # Signal never delivered: clear the cancelled flag so a natural
                # completion is not misreported as a cancellation.
                with _PROCS_LOCK:
                    _CANCELLED_PROC_JOBS.discard(job_id)
                return False

    def _escalate() -> None:
        time.sleep(_KILL_ESCALATION_GRACE_SECS)
        if proc.poll() is None:
            _kill_proc_group(proc)

    threading.Thread(target=_escalate, name=f"cron-cancel-{job_id}", daemon=True).start()
    logger.info("Cancel: sent SIGTERM to subprocess group of cron %s (pid %d)", job_id, proc.pid)
    return True


def _kill_proc_group(proc: subprocess.Popen) -> None:
    """Best-effort SIGKILL of a subprocess and its whole process group."""
    pgid = _resolve_safe_pgid(proc)
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    # Windows (pgid is always None there): reap the tree via taskkill /T before
    # the single-process fallback so children don't orphan.
    if not platform_compat.IS_POSIX:
        try:
            platform_compat.kill_process_tree(proc.pid, platform_compat.SIGKILL)
            return
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.kill()
    except Exception:
        pass


def _drain_after_kill(proc: subprocess.Popen, job_id: str | None) -> None:
    """Reap a SIGKILLed child's pipes without leaking fds or hijacking the result.

    ``communicate(timeout=5)`` can ITSELF raise ``TimeoutExpired``: the child
    outlived the group kill (uninterruptible I/O, or no pgid resolved so only
    ``proc.kill()`` was tried), or another process inherited the write end of
    the pipe and holds it open, so EOF never arrives. Waiting longer cannot help
    once SIGKILL has been sent, and the caller has already decided the outcome —
    so swallow that one exception rather than letting it displace the caller's
    ``raise`` / ``return``. Closing the pipes has to happen either way:
    ``Popen._communicate`` closes them as a side effect of reaching EOF, which
    is exactly the path not taken here, and nothing else ever closes them.
    """
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        logger.warning(
            "Post-kill drain timed out (5s) for cron %s (pid %s); the child "
            "outlived SIGKILL or another process holds the pipe — closing pipes",
            job_id,
            proc.pid,
        )
    finally:
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass


if TYPE_CHECKING:
    from kiro_crew.cron import CronJob

logger = logging.getLogger(__name__)


class SkipError(Exception):
    """Abort this tick silently. Cron fires again next interval."""


class DoneError(Exception):
    """Complete the cron job. Job is removed from the schedule.

    Use ctx.notify() before raising Done() to deliver a message.
    """

    def __init__(self, message: str = ""):
        self.message = message
        super().__init__(message)


class ReportError(Exception):
    """Deliver a message but keep the job running.

    Use for long-lived monitors that need to report multiple times.
    """

    def __init__(self, message: str = ""):
        self.message = message
        super().__init__(message)


# Backward-compat aliases: Skip/Done/Report are the public API used by
# user-authored cron scripts. Renamed to *Error for flake8 N818; aliases
# preserve the existing import/raise surface with zero behavior change.
Skip = SkipError
Done = DoneError
Report = ReportError


@dataclass
class ScriptContext:
    """Passed to script functions. Provides delivery and tool access."""

    job: CronJob
    _port: int = 5476
    _secret: str = ""

    def __post_init__(self) -> None:
        # The parent injects the port it minted the credential for. Preferring it
        # keeps credential and dial target from one resolution; KIROCREW_PORT is the
        # fallback for a directly-constructed context and is 5476 on a --port auto
        # gateway, which is a SIBLING rather than this instance.
        self._port = int(
            os.environ.pop("_KIROCREW_DIAL_PORT", "") or os.environ.get("KIROCREW_PORT", "5476")
        )
        # Secret injected via temp file (not inherited env) to prevent privilege escalation.
        # Pop env var and unlink file immediately so fn(ctx) cannot access the secret directly.
        secret_file = os.environ.pop("_KIROCREW_SECRET_FILE", "")
        if secret_file and Path(secret_file).exists():
            self._secret = Path(secret_file).read_text()
            try:
                Path(secret_file).unlink()
            except OSError:
                pass
        else:
            self._secret = os.environ.pop("KIROCREW_INTERNAL_SECRET", "")

    @property
    def message(self) -> str:
        """The cron job's message field (used to pass args to scripts)."""
        return getattr(self.job, "message", "")

    def notify(self, text: str, **kwargs: Any) -> dict:
        """Send a message via the gateway (same as send_message MCP tool).

        Raises RuntimeError if delivery fails.
        """
        safe_text = redact(text)
        # Redact kwargs values
        kwargs_str = json.dumps(kwargs) if kwargs else "{}"
        kwargs_str = redact(kwargs_str)
        safe_kwargs = json.loads(kwargs_str) if kwargs else {}
        payload: dict[str, Any] = {"text": safe_text, **safe_kwargs}
        # caller_session lets session="origin" resolve to the chat that created this
        # cron; hard-assigned (not setdefault) so a script cannot spoof another session
        payload["caller_session"] = f"cron:{self.job.id}"
        result = self._post("/api/send-message", payload)
        if "error" in result:
            raise RuntimeError(f"notify() failed: {result['error']}")
        return result

    def call_tool(self, server: str, tool: str, args: dict) -> str:
        """Call an MCP tool by spawning the server subprocess directly.

        Args are scanned for credential/URL leakage before passing to the
        sandboxed MCP server subprocess.
        """
        # Scan serialized args for credential patterns
        args_str = json.dumps(args)
        args_str = redact(args_str)
        safe_args = json.loads(args_str)
        client = None
        try:
            client = McpToolClient(server, session_key=f"cron:{self.job.id}")
            result = client.call_tool(tool, safe_args)
            self._audit_tool_call(server, tool, "ok")
            return result
        except Exception as exc:
            self._audit_tool_call(server, tool, "error", str(exc))
            raise
        finally:
            if client is not None:
                client.close()

    def _audit_tool_call(self, server: str, tool: str, outcome: str, error: str = "") -> None:
        """Log tool invocation for audit trail."""
        logger.info(
            "cron_script tool_call: job=%s server=%s tool=%s outcome=%s%s",
            self.job.id,
            server,
            tool,
            outcome,
            f" error={error}" if error else "",
        )
        try:
            sel().log_tool_invocation(
                session_key=f"cron:{self.job.id}",
                tool_name=f"{server}/{tool}",
                tool_kind="cron_script_tool",
                outcome=outcome,
                error=error,
            )
        except Exception:
            logger.debug("SEL audit logging failed in cron_script tool call", exc_info=True)

    def _post(self, path: str, body: dict) -> dict:
        data = json.dumps(body).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Secret": self._secret,
            "X-Session-Key": f"cron:{self.job.id}",
        }
        req = urllib.request.Request(
            f"http://localhost:{self._port}{path}",
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with loopback_urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            logger.warning("ScriptContext._post(%s) failed: %s", path, exc)
            return {"error": str(exc)}


# ── MCP Tool Bridge ──


class McpToolClient:
    """Minimal MCP JSON-RPC client. Spawns server subprocess, calls tool, closes."""

    def __init__(self, server_name: str, session_key: str = ""):
        self._server_name = server_name
        self._session_key = session_key
        resolved = _resolve_mcp_server(server_name)
        if not resolved:
            raise RuntimeError(f"MCP server '{server_name}' not found in agent config")
        argv, spec_env = resolved
        sandboxed_argv, self._sandbox_cleanup = wrap_argv(list(argv), mode="standard")
        sandboxed_argv = cgroup_scope_argv(sandboxed_argv)  # cgroup DoS ceiling
        # SECURITY: the confinement wrappers prepended above (`systemd-run` on
        # Linux, `env` -> `sandbox-exec` on macOS) are absolute paths, pinned by
        # the functions that prepend them. That matters here because Popen is
        # handed an env whose PATH is overlaid from the per-server agent config
        # below, and CPython resolves a slash-less argv[0] through THAT env's PATH
        # (os.get_exec_path) -- a bare-name wrapper would be redirectable to an
        # attacker binary running BEFORE confinement exists. Pinning belongs in
        # those producers, not here: re-pinning at the spawn site would also
        # rewrite argv[0] on the fail-open no-sandbox path, where argv[0] is the
        # OPERATOR-declared command and silently resolving it against the
        # gateway's own directories would override the very PATH selection the
        # spec `env` block exists to provide.
        #
        # Build the subprocess env: start from the secret-scrubbed cron env, then
        # overlay the per-server `env` block from the agent config (e.g. the PATH
        # that lets a launcher resolve a helper binary it shells out to). A
        # launcher that execs a binary reachable only via that env dies before the
        # initialize handshake if the env is dropped. Three filters apply, and all
        # three are load-bearing:
        #   * _clean_cron_env() strips secrets from the INHERITED env;
        #   * _CRON_ENV_DENY is re-applied to the spec overlay so a denied key
        #     cannot be reintroduced through the config;
        #   * sanitize_spec_env() drops loader/interpreter-injection keys
        #     (LD_*, DYLD_*, and the specific PYTHONPATH/HOME/STARTUP/USERBASE
        #     startup channels -- see env._SPEC_ENV_DENIED_PREFIXES; it is a prefix
        #     set, NOT all of PYTHON*). Those are NOT in _CRON_ENV_DENY, and the spec
        #     env is externally authorable, so without this an `LD_PRELOAD` in a
        #     server's `env` block would be honoured by the dynamic loader inside
        #     the confinement wrapper process -- executing attacker code before
        #     the wrapper establishes containment. Pinning argv[0] does not help:
        #     the loader acts on the pinned binary. This is the same reason
        #     mcp_discovery routes its probe's spec env through the sanitizer;
        #     PATH is deliberately NOT denied, since forwarding it is the point.
        #   * sanitize_spec_env() ALSO drops the reserved KIROCREW_ namespace
        #     (env._SPEC_ENV_RESERVED_PREFIXES), which is an authorization control
        #     rather than a containment one and is the reason the two other filters
        #     are not sufficient here. The server this bridge most often spawns is
        #     `kirocrew-cron` itself, whose ownership checks resolve the calling
        #     session from KIROCREW_SESSION_KEY / KIROCREW_HOST_PID when no
        #     gateway caller block is present -- so one of those in that server's
        #     `env` block would let a SCRIPT CRON name itself another session and
        #     reach that session's jobs. Sandboxing does not bound that:
        #     confinement limits what the child may touch, not whose jobs Kiro
        #     Crew thinks it owns. The deny lives in the shared sanitizer rather
        #     than in _CRON_ENV_DENY so the discovery probe -- which applies no
        #     cron deny-set at all -- is covered by the same control instead of a
        #     second copy of it.
        proc_env = _clean_cron_env()
        proc_env.update(
            sanitize_spec_env((k, v) for k, v in spec_env.items() if k not in _CRON_ENV_DENY)
        )
        if self._session_key:
            # Hard-assigned (not setdefault) AFTER both overlays, for the same
            # reason ScriptContext.notify() hard-assigns ``caller_session``: the
            # inherited env is the script child's own ``os.environ``, which user
            # code can rewrite before calling ``ctx.call_tool``. The identity the
            # spawned server sees must be the one the launcher gave THIS job, not
            # whatever the script put there. Best-effort friction against
            # in-process forgery, on the same footing as the rest of the env-
            # based identity contract (see session_pid_sig's threat model).
            proc_env["KIROCREW_SESSION_KEY"] = self._session_key
        # Capture stderr to a tempfile instead of DEVNULL so spawn/handshake
        # failures are legible. DEVNULL hid the real cause -- wrong
        # Node version, expired auth cookies, OOM kill, sandbox failure -- behind
        # a generic "disconnected during 'initialize'" RuntimeError.
        self._stderr_file = tempfile.NamedTemporaryFile(
            mode="w+", prefix="mcp-stderr-", suffix=".log", delete=False
        )
        try:
            self._proc = popen_limited(
                sandboxed_argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr_file,
                text=True,
                env=proc_env,
            )
        except Exception:
            self._stderr_file.close()
            Path(self._stderr_file.name).unlink(missing_ok=True)
            if self._sandbox_cleanup:
                Path(self._sandbox_cleanup).unlink(missing_ok=True)
            raise
        assert self._proc.stdin is not None
        assert self._proc.stdout is not None
        self._req_id = 0
        try:
            self._rpc(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "kirocrew-cron-script", "version": "0.1"},
                },
            )
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def _send(self, msg: dict) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()

    def _recv(self) -> dict | None:
        assert self._proc.stdout is not None
        while True:
            line = self._proc.stdout.readline()
            if not line:  # EOF
                return None
            if line.strip():
                return json.loads(line)

    def _stderr_tail(self, limit: int = 1024) -> str:
        """Return the last `limit` bytes of the subprocess's captured stderr.

        Credentials and exfiltration URLs are redacted before the tail is
        surfaced in an error so a failing spawn (e.g. an auth dump or an
        attacker-controlled MCP server) can't leak secrets or beacon URLs
        into logs, Slack, or the dashboard.
        """
        path = getattr(self, "_stderr_file", None)
        if path is None:
            return ""
        try:
            with open(path.name, errors="replace") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - limit))
                return redact(fh.read().strip())
        except Exception as exc:
            # Defensive — _stderr_tail runs inside error reporting itself, so we
            # never raise here. We DO log the exception type at debug so that a
            # silently broken tail (disk/encoding error, missing tempfile) is
            # diagnosable when investigating MCP spawn failures.
            logger.debug("_stderr_tail failed: %s", type(exc).__name__)
            return ""

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._req_id += 1
        req_id = self._req_id
        name = getattr(self, "_server_name", "?")
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        for _ in range(1000):
            msg = self._recv()
            if msg is None:
                rc = self._proc.poll()
                tail = self._stderr_tail()
                raise RuntimeError(
                    f"MCP server '{name}' disconnected during '{method}' "
                    f"(rc={rc}); stderr tail: {tail or '(empty)'}"
                )
            if msg.get("id") == req_id:
                return msg
        raise RuntimeError(
            f"MCP server '{name}' did not respond to '{method}' within 1000 messages"
        )

    def call_tool(self, name: str, arguments: dict) -> str:
        r = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if "error" in r:
            raise RuntimeError(f"MCP tool error: {r['error']}")
        result = r.get("result", {})
        if result.get("isError"):
            content = result.get("content", [])
            err_text = content[0].get("text", "unknown error") if content else "unknown error"
            raise RuntimeError(f"MCP tool error: {err_text}")
        content = result.get("content", [])
        return content[0].get("text", "") if content else ""

    def close(self) -> None:
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        except Exception:
            pass
        finally:
            stderr_file = getattr(self, "_stderr_file", None)
            if stderr_file is not None:
                try:
                    stderr_file.close()
                except Exception:
                    pass
                Path(stderr_file.name).unlink(missing_ok=True)
            if self._sandbox_cleanup:
                Path(self._sandbox_cleanup).unlink(missing_ok=True)


@lru_cache(maxsize=16)
def _resolve_mcp_server(name: str) -> tuple[tuple[str, ...], dict[str, str]] | None:
    """Read MCP server command + env from agent config (cached per process).

    Returns ``(argv, env)``. The per-server ``env`` block is required for
    launchers that shell out to a helper binary only reachable via the ``PATH``
    the config supplies: dropping it makes the spawned MCP die at the JSON-RPC
    ``initialize`` handshake. When the config has no ``env`` block -- or declares
    one that is not a JSON object -- the env dict is empty.
    """
    cfg_path = kiro_agents_dir() / "kirocrew.json"
    if not cfg_path.exists():
        # Fall back to any kirocrew-named agent spec in the same agents dir.
        for p in kiro_agents_dir().glob("*kirocrew*.json"):
            cfg_path = p
            break
    if not cfg_path.exists():
        return None
    # The agents dir is user-writable and shared with other tools, so this goes
    # through the hardened agent-spec reader (size cap, sensitive-symlink
    # screen, explicit UTF-8, non-object rejection, SEL denial event) instead of
    # a bare ``read_text`` + ``json.loads``. ``None`` covers every unusable file
    # -- including the malformed-JSON and non-UTF-8 cases the old form let
    # escape as an unhandled JSONDecodeError / UnicodeDecodeError into the cron
    # runner -- and degrades to "no such server", the same answer an absent
    # entry already produced.
    cfg = _read_agent_spec(
        cfg_path,
        operation="cron_resolve_mcp_server",
        source="cron",
    )
    if cfg is None:
        return None
    spec = cfg.get("mcpServers", {}).get(name)
    if not spec:
        return None
    argv = tuple([spec["command"]] + spec.get("args", []))
    # A hand-edited config can spell `env` as anything JSON allows. Only a
    # mapping has .items(), so a string/list/number would raise AttributeError
    # here and fail EVERY ctx.call_tool for that server with a traceback that
    # names this function rather than the malformed config. Treat a non-mapping
    # as absent and say so once: the declaration cannot be honoured either way,
    # and a silent drop reads as the env-drop bug this function exists to fix.
    raw_env = spec.get("env")
    if raw_env is not None and not isinstance(raw_env, dict):
        logger.warning(
            "MCP server %r declares a non-object 'env' (%s); ignoring it",
            name,
            type(raw_env).__name__,
        )
        raw_env = None
    env: dict[str, str] = {}
    for raw_key, raw_value in (raw_env or {}).items():
        key, value = str(raw_key), str(raw_value)
        # Stringifying is not enough: `Popen` validates the env at the execve
        # boundary and rejects the WHOLE spawn over one bad pair -- ValueError
        # "illegal environment variable name" for `=` in a name, "embedded null
        # byte" for a NUL in either half. Since that fires before the fork, a
        # single malformed entry in this block aborts EVERY ctx.call_tool for the
        # server, and the traceback names Popen rather than the config that is
        # actually wrong -- the same failure shape, for the same reason, as the
        # non-object `env` handled above. An empty name is not a crash but is
        # unreachable (`getenv("")` is always NULL), so it is dropped on the same
        # "cannot be honoured either way" ground.
        #
        # Dropped per ENTRY rather than rejected per SERVER: the sibling entries
        # are still honourable, and the whole point of this function is that a
        # server whose launcher needs a config-supplied PATH must keep working.
        # Logged for the reason the non-object branch logs -- a silent drop reads
        # as the env-drop bug this function exists to fix. The name is named so
        # the operator knows which entry to edit; the value never is, since it
        # can hold a credential.
        if not key:
            reason = "empty name"
        elif "=" in key:
            reason = "'=' in name"
        elif "\0" in key:
            reason = "NUL byte in name"
        elif "\0" in value:
            reason = "NUL byte in value"
        else:
            env[key] = value
            continue
        logger.warning(
            "MCP server %r declares an invalid 'env' entry %r (%s); ignoring it",
            name,
            key,
            reason,
        )
    return argv, env


def _split_script_spec(script_path: str) -> tuple[str, str]:
    """Split a ``"<path>:<func>"`` spec into ``(path, func)``, drive-aware.

    Splits on the LAST colon. A Windows drive letter adds a second colon at
    index 1 (``C:\\...``); taking the rightmost colon keeps the whole drive path
    and the trailing func (``C:\\crons\\job.py:run`` -> ``C:\\crons\\job.py`` +
    ``run``). The only ambiguous input is a bare drive path with no ``:func``
    suffix, which would otherwise split at the drive colon into the nonsense
    ``("C", "\\crons\\job.py")`` — so a colon that IS the drive colon does not
    count as the separator.
    """

    drive_colon = len(script_path) >= 2 and script_path[1] == ":" and script_path[0].isalpha()
    func_colon = script_path.rfind(":")
    if func_colon == -1 or (drive_colon and func_colon == 1):
        raise ValueError(f"Invalid script path '{script_path}': expected 'path.py:func'")
    return script_path[:func_colon], script_path[func_colon + 1 :]


def resolve_script_path(script_path: str) -> tuple[str, str]:
    """Validate and resolve a script path. Returns (file_path, func_name).

    Scripts must be files under ``<config_dir>/crons/``.
    Format: "<config_dir>/crons/file.py:function" or "/absolute/path.py:function"
    """
    module_part, func_name = _split_script_spec(script_path)

    file_path = Path(os.path.expanduser(module_part)).resolve()
    if not file_path.exists():
        raise FileNotFoundError(f"Script file not found: {file_path}")
    if is_sensitive_path(str(file_path)):
        raise PermissionError(f"Script path blocked by security policy: {file_path}")
    allowed_dir = (config_dir() / "crons").resolve()
    if not file_path.is_relative_to(allowed_dir):
        raise PermissionError(f"Script must be under {allowed_dir}, got: {file_path}")
    return str(file_path), func_name


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
    return read_local_secret(port)


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


def run_script_sandboxed(
    script_path: str,
    job_id: str,
    job_message: str = "",
    timeout: int = 30,
    secret_env: dict[str, str] | None = None,
    secret_env_pin: str = "",
    delivery: str = "",
) -> dict:
    """Run a cron script in a sandboxed subprocess via wrap_argv().

    Returns: {"status": "ok"|"skip"|"done"|"error", "message": "...", "error": "..."}

    ``secret_env``/``secret_env_pin`` carry an operator grant of vault secrets
    (see the grant block near ``_CRON_ENV_DENY``). When a grant is present the
    script body is read ONCE here, pin-verified, and the child executes those
    verified bytes from a private temp copy — the on-disk script (agent-
    writeable by design) is never re-read by the launcher, so a body swapped
    in after the check cannot run with the secrets. The temp copy's own dir —
    not the live ``crons/`` dir — is what goes on ``sys.path``, so a granted
    script cannot ``import helper`` from the agent-writeable dir either: a
    sibling module the operator did not approve fails the import instead of
    running with the secrets. A script that needs siblings must inline them
    (one approved body) or read them as data.
    """

    file_path_str, func_name = resolve_script_path(script_path)

    import_dir_str = os.path.dirname(file_path_str)
    resolved_secret_env: dict[str, str] = {}
    script_body: bytes | None = None
    pinned_dir: str | None = None
    if secret_env:
        try:
            script_body = _read_script_body(file_path_str)
        except ValueError as exc:
            return {"status": "error", "error": f"Script unreadable: {exc}"}
        resolved_secret_env, secret_err = _secret_env_precheck(
            secret_env,
            secret_env_pin,
            script=script_path,
            script_body=script_body,
            message=job_message,
            job_id=job_id,
            delivery=delivery,
        )
        if secret_err:
            return {"status": "error", "error": f"❌ {secret_err}"}
        # Granted scripts get an EMPTY private dir as sys.path[0] (sibling
        # imports from the agent-writeable live crons/ dir fail instead of
        # running with the secrets). The verified body itself travels over
        # STDIN — never re-read from any pathname a same-UID writer could
        # swap after verification.
        pinned_dir = tempfile.mkdtemp(prefix="kirocrew_cron_pin_")
        import_dir_str = pinned_dir

    stdin_payload: str | None = None
    if resolved_secret_env:

        stdin_payload = json.dumps(
            {
                "body_b64": base64.b64encode(script_body or b"").decode(),
                "secrets": _filter_grant_env(resolved_secret_env),
            }
        )
    # A granted child starts with ``python -I`` (isolated: PYTHONPATH and
    # user-site are never consulted), so an agent-planted ``sitecustomize.py``
    # cannot run before this launcher and intercept the secrets update below.
    # Isolation also means ``kiro_crew`` may no longer be importable via an
    # inherited PYTHONPATH (dev checkouts), so the TRUSTED package parent —
    # computed here in the gateway from kiro_crew's own location, never from
    # the environment — is seeded explicitly. ``-I`` only implies safe_path
    # (no script-dir prepend) on Python 3.11+; on the 3.10 floor sys.path[0]
    # is STILL the launcher's own directory. So the granted launcher lives in
    # the private pinned dir (never the shared temp dir, where an agent can
    # park a json.py indefinitely) AND the prelude strips that directory by
    # VALUE — a positional strip would drop a stdlib entry on 3.11+, where
    # nothing was prepended. Both spellings are stripped because CPython
    # realpaths the script dir when computing sys.path[0].
    _kiro_pkg_parent = str(Path(__file__).resolve().parent.parent)
    if stdin_payload is not None:
        assert pinned_dir is not None
        _launcher_dirs = (pinned_dir, os.path.realpath(pinned_dir))
        prelude = (
            "import sys\n"
            f"sys.path[:] = [p for p in sys.path if p not in ('', *{_launcher_dirs!r})]\n"
            f"sys.path.insert(0, {_kiro_pkg_parent!r})\n"
            "import base64, json, os, types\n"
        )
    else:
        prelude = (
            # Import sys first (builtin, unshadowable) and strip the launcher's
            # own /tmp dir from sys.path[0] before importing json/os/types/
            # kiro_crew — otherwise a stray sibling like /tmp/struct.py or
            # /tmp/os.py shadows the stdlib and crashes the cron launcher on
            # startup. The user-script dir is re-added explicitly below.
            "import sys\n"
            "sys.path[:] = [p for p in sys.path if p not in ('', sys.path[0])]\n"
            "import base64, json, os, types\n"
        )
    launcher = prelude + (
        # A granted run receives {body_b64, secrets} over STDIN, before any
        # other work: the verified bytes are executed directly (no pathname to
        # swap between verification and exec), and the secrets enter
        # os.environ AFTER this process's execve — the kernel's
        # /proc/<pid>/environ snapshot is the STARTUP environment, so a
        # same-UID reader of that file never sees them. Ungranted runs get no
        # payload and exec the live file as before.\n
        f"_payload = json.loads(sys.stdin.readline()) if {bool(stdin_payload)!r} else None\n"
        "if _payload:\n"
        "    os.environ.update(_payload['secrets'])\n"
        "from kiro_crew.config.loader import KiroCrewConfig\n"
        "from kiro_crew.platform.bootstrap import boot_platform\n"
        "boot_platform(KiroCrewConfig.load())\n"
        "from kiro_crew.cron_script import ScriptContext, Skip, Done, Report\n"
        # Record the granted key NAMES so _clean_cron_env strips them from
        # every descendant env (ctx.call_tool's MCP server subprocess): the
        # grant authorizes THIS approved body, not the binaries it calls.
        "if _payload:\n"
        "    import kiro_crew.cron_script as _kcs\n"
        "    _kcs._GRANTED_ENV_KEYS.update(_payload['secrets'])\n"
        f"sys.path.insert(0, {import_dir_str!r})\n"
        f"mod = types.ModuleType('_cron_script')\n"
        f"mod.__file__ = {file_path_str!r}\n"
        # The compile filename stays the original so tracebacks point at the
        # file the operator knows.
        "if _payload:\n"
        f"    _src = base64.b64decode(_payload['body_b64'])\n"
        "else:\n"
        f"    with open({file_path_str!r}, 'rb') as f:\n"
        "        _src = f.read()\n"
        f"exec(compile(_src, {file_path_str!r}, 'exec'), mod.__dict__)\n"
        f"fn = getattr(mod, {func_name!r}, None)\n"
        "if fn is None:\n"
        f"    print(json.dumps({{'status': 'error', 'error': 'Function not found'}}))\n"
        "    sys.exit(0)\n"
        f"job = types.SimpleNamespace(id={job_id!r}, message={job_message!r})\n"
        "ctx = ScriptContext(job=job)\n"
        "try:\n"
        "    fn(ctx)\n"
        "    print(json.dumps({'status': 'ok'}))\n"
        "except Skip:\n"
        "    print(json.dumps({'status': 'skip'}))\n"
        "except Done as d:\n"
        "    print(json.dumps({'status': 'done', 'message': d.message}))\n"
        "except Report as r:\n"
        "    print(json.dumps({'status': 'report', 'message': r.message}))\n"
        "except Exception as e:\n"
        "    print(json.dumps({'status': 'error', 'error': str(e)}))\n"
    )

    # A granted launcher is born inside the private pinned dir: on Python
    # 3.10 ``-I`` still makes the script's own directory sys.path[0], and the
    # shared temp dir is somewhere an agent can leave a json.py waiting.
    # Ungranted runs keep the shared temp dir (their prelude strips it).
    fd, launcher_path = tempfile.mkstemp(
        suffix=".py", prefix="kirocrew_cron_", dir=pinned_dir if stdin_payload else None
    )
    sandbox_cleanup: str | None = None
    # Resolve the dial port ONCE: the credential written below and the
    # _KIROCREW_DIAL_PORT the child dials must come from the same resolution, or a
    # --port auto bind between two resolutions would pair a credential with the
    # wrong port and 403 the callback.
    dial_port = _resolve_dial_port()
    # Write secret to temp file for ScriptContext (scrubbed from env)
    secret_fd, secret_path = tempfile.mkstemp(prefix="kirocrew_secret_")
    try:
        try:
            # Tighten the DACL BEFORE writing the secret bytes so the file is
            # never on disk under the parent-inherited %TEMP% DACL on Windows.
            # On POSIX mkstemp already births the file 0600 so ordering is a
            # no-op; on Windows mkstemp cannot set an owner-only DACL, so the
            # interval between create and lockdown is a real window
            # if we wrote first. Matches the fail-loud convention of the other
            # internal-secret writers (token_secret, refresh_tokens, snapshot,
            # server._write_secret_file, token_auth) — chmod_safe swallows
            # OSError and would hide a lockdown failure. Both calls stay inside
            # the outer try so a lockdown failure still hits the finally that
            # unlinks the secret + launcher (otherwise the fd leaks and temp
            # files persist).
            platform_compat.restrict_to_owner(secret_path)
            os.write(secret_fd, _resolve_internal_secret(dial_port).encode())
        finally:
            os.close(secret_fd)
        try:
            os.write(fd, launcher.encode())
        finally:
            os.close(fd)

        # Granted children run ISOLATED (-I): PYTHONPATH and user-site are
        # ignored, so agent-planted startup hooks (sitecustomize/usercustomize)
        # cannot execute before the launcher and capture the secrets it loads.
        # The launcher prelude seeds the trusted kiro_crew package parent
        # explicitly, so imports survive isolation on dev checkouts too.
        argv = (
            [sys.executable, "-I", launcher_path]
            if stdin_payload is not None
            else [sys.executable, launcher_path]
        )
        # A granted child must never see the LIVE crons dir OR the script's
        # own parent directory: the launcher's empty-sys.path isolation stops
        # accidental sibling imports, but the verified script itself could
        # re-add either directory and import a sibling the agent rewrites
        # after approval — the pin covers the script's own bytes, not code it
        # chooses to load. The verified bytes travel over STDIN, so the child
        # never needs to read the script file; hiding both trees at the
        # sandbox layer closes the mutable-sibling route regardless of what
        # the script does to sys.path. Granted runs additionally use the
        # STRICT sandbox profile (credential stores and every crew-internal
        # dir hidden), keeping the child's reachable surface as small as the
        # sandbox can make it. Ungranted scripts keep their normal view.
        if stdin_payload is not None:
            hidden = tuple(
                dict.fromkeys(
                    (
                        str(config_dir() / "crons"),
                        str(Path(file_path_str).resolve().parent),
                    )
                )
            )
        else:
            hidden = ()
        sandbox_mode = "strict" if stdin_payload is not None else "standard"
        sandboxed_argv, sandbox_cleanup = wrap_argv(
            argv, mode=sandbox_mode, extra_hidden_dirs=hidden
        )
        if stdin_payload is not None and sandboxed_argv == argv:
            # On a host with no OS sandbox backend, the unsandboxed-exec
            # opt-in makes wrap_argv hand back the argv UNMODIFIED — the
            # crons-dir hiding above silently vanishes with it, and a granted
            # script could import an agent-rewritable sibling with the
            # secrets in reach. A grant's isolation guarantees are part of
            # what the operator approved, so refuse rather than degrade.
            return {
                "status": "error",
                "error": "❌ secret-bearing cron runs require an OS sandbox "
                "backend; this host runs scripts unsandboxed "
                "(sandbox_allow_unsandboxed_exec), where a grant's isolation "
                "cannot be enforced. Revoke the grant to run this script "
                "without secrets.",
            }

        # Build clean env: secrets (Slack tokens, owner id, internal secret)
        # are never inherited; the internal secret is passed via the 0600 file.
        clean_env = _clean_cron_env()
        # Operator-granted vault secrets are NOT placed in the child's execve
        # environment: /proc/<pid>/environ is the startup-env snapshot and is
        # readable by any same-UID process, so they travel in the stdin
        # payload instead and enter os.environ only after the child starts
        # (see the launcher). The name-deny filter still applies.
        clean_env["_KIROCREW_SECRET_FILE"] = secret_path
        # The child must dial the gateway the credential above was minted for:
        # same dial_port, resolved once above, not a second resolution here.
        clean_env["_KIROCREW_DIAL_PORT"] = str(dial_port)
        # Give the child the SAME identity the gateway hands every agent
        # subprocess (acp/client.py injects KIROCREW_SESSION_KEY for agent crons
        # too): the strict resolver behind every state-mutating MCP tool
        # (mcp_core._resolve_session_key_strict) accepts exactly three sources --
        # the gateway-injected caller block, this env var, or KIROCREW_HOST_PID
        # plus its signed sidecar. A script cron had NONE of them: nothing routes
        # its direct MCP spawns through gatewayd, nobody publishes a sidecar for
        # its launcher pid, and this env was never set -- so on Linux the sandbox
        # launcher's KIROCREW_HOST_PID pointed at an unsigned pid and every write
        # was refused with "the signed pid mapping did not verify", while on
        # macOS/Windows there was no channel at all. Read-only calls were
        # unaffected, so the refusal surfaced only as a script that silently did
        # nothing. `cron:<job id>` is the key ScriptContext already presents to
        # the gateway over HTTP (X-Session-Key / caller_session) and the key
        # agent-cron sessions run under, so ownership and audit see one
        # principal per job regardless of which surface the job uses.
        clean_env["KIROCREW_SESSION_KEY"] = f"cron:{job_id}"
        # Pre-resolve gh OUTSIDE the sandbox and pin its identity for the
        # child: the sandbox's single-uid user namespace maps every root-owned
        # path component to the overflow uid, so the child's own ownership
        # walk refuses ANY gh on the host. Empty when the host has no usable
        # gh -- scripts that never call gh are unaffected either way.
        clean_env.update(prevalidated_gh_env())

        sandboxed_argv = cgroup_scope_argv(sandboxed_argv)  # cgroup DoS ceiling
        proc = popen_limited(
            sandboxed_argv,
            stdin=subprocess.PIPE if stdin_payload is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=clean_env,
            start_new_session=True,
        )
        _register_proc(job_id, proc)
        try:
            try:
                stdout, stderr = proc.communicate(
                    input=(stdin_payload + "\n") if stdin_payload is not None else None,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                # Popen.communicate does not kill the child on timeout
                # (unlike subprocess.run) — clean up before re-raising.
                _kill_proc_group(proc)
                _drain_after_kill(proc, job_id)
                raise
        finally:
            cancelled = _unregister_proc(job_id, proc)
        if cancelled:
            return {"status": "cancelled", "error": "Cancelled by user"}

        if proc.returncode != 0 and not stdout.strip():
            # Report the TERMINAL stderr context, not the head. A process that
            # dies hard leaves its diagnosis LAST -- the traceback is the final
            # thing written -- while whatever a startup path logged first (a
            # data-home migration warning, a deprecation notice, an import
            # banner) sits in front of it. Bounding from the head therefore
            # reports the noise and truncates the cause, and the operator reads
            # a cron failure whose message describes something that did not kill
            # the job.
            #
            # ``rstrip`` first so a trailing newline does not spend part of the
            # budget, and so an all-whitespace stderr still falls through to the
            # exit-code fallback rather than reporting blank text.
            #
            # Redact the WHOLE stream before bounding: slicing first would cut
            # a credential that straddles the 500-char boundary in half, and
            # ``redact`` cannot recognise the surviving fragment, so it would
            # reach logs and the persisted ``last_error`` unmasked.
            tail = redact(_scrub_grant_values(stderr.rstrip(), resolved_secret_env))
            error_text = tail[-500:] if tail else f"exit {proc.returncode}"
            return {"status": "error", "error": error_text}

        try:
            parsed = json.loads(stdout.strip().split("\n")[-1])
            # The launcher's status JSON carries str(e) from the script's own
            # exception — the one child-diagnostic path that does NOT flow
            # through the stderr redaction above. Scrub granted values from
            # every diagnostic string field before the result reaches gateway
            # logs and the persisted last_error. ``status`` is exempt: the
            # launcher emits it from a closed vocabulary (ok/skip/done/report/
            # error) and the caller only compares it against those literals,
            # so it can neither carry a secret nor be rendered — while a vault
            # value that happens to equal one of those tokens would otherwise
            # rewrite a successful run into an unrecognised (failed) status.
            if resolved_secret_env and isinstance(parsed, dict):
                for k, v in parsed.items():
                    if k != "status" and isinstance(v, str):
                        parsed[k] = _scrub_grant_values(v, resolved_secret_env)
            return parsed
        except (json.JSONDecodeError, IndexError):
            return {
                "status": "error",
                # Redact the complete stdout BEFORE truncating: slicing first
                # could cut a credential at the boundary, leaving its unredacted
                # head in the diagnostic.
                "error": f"Bad output: {redact(_scrub_grant_values(stdout, resolved_secret_env))[:200]}",
            }
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": f"Script timed out after {timeout}s"}
    except SandboxUnavailableError as exc:
        # Same reasoning as run_command_sandboxed: a host with no sandbox backend
        # must surface a failed job carrying the remedy, not an escaping
        # exception the scheduler cannot attribute to this job.
        return {"status": "error", "error": f"{_SANDBOX_UNAVAILABLE_PREFIX}{exc}"}
    finally:
        Path(launcher_path).unlink(missing_ok=True)
        Path(secret_path).unlink(missing_ok=True)
        if pinned_dir:
            shutil.rmtree(pinned_dir, ignore_errors=True)
        if sandbox_cleanup:
            Path(sandbox_cleanup).unlink(missing_ok=True)


_MAX_COMMAND_OUTPUT = 65536  # 64KB cap

# Leads a cron failure caused by the fail-closed sandbox rather than by the job
# itself. The distinction matters to the reader: the job is fine, the host cannot
# isolate it, and the remedy is a config opt-in — which the wrapped message
# carries verbatim.
_SANDBOX_UNAVAILABLE_PREFIX = "❌ Cron could not run in an OS sandbox: "


def _resolve_command_shell() -> str | None:
    """Return an absolute path to a POSIX shell for ``sh -c`` command crons.

    Command crons are authored as POSIX shell one-liners (and vetted by
    ``mcp_cron._vet_shell_command`` under POSIX quoting), so cmd.exe is NOT a
    substitute — a missing shell must fail loudly rather than silently changing
    the command language. Returns ``None`` when no POSIX-strict shell can be
    found, which makes the caller refuse the job with a legible error.

    Windows always returns ``None``: neither shipped option preserves the
    language the vet gate assumed. Verified on a Git-for-Windows install —
    ``sh.exe -c 'echo test.a{w,w}s'`` prints ``test.aws test.aws``, i.e. it IS
    bash and performs brace expansion, which is exactly how
    ``cat ~/.a{w,w}s/credentials`` hides a credential path from a gate that
    tokenizes the stored string once. Command crons are therefore unavailable on
    Windows by design; ``script`` crons (whose body is scanned in full) are the
    supported path there.
    """

    # On Windows there is no shipped shell whose language matches what
    # mcp_cron._vet_shell_command was written against: cmd.exe is not POSIX at
    # all, and Git-for-Windows's sh.exe IS bash. Refuse rather than route the
    # vetted string through a shell that widens its language.
    if platform_compat.IS_WINDOWS:
        return None
    # POSIX: NEVER consult PATH (shutil.which("sh")). PATH may contain an
    # agent-writable directory that precedes /bin — an agent can plant
    # ``~/.local/bin/sh`` that fingerprints the probe input, passes the strict
    # test, then does something different when called with the real cron. Even
    # inside `cc`-mode isolation the agent-planted binary runs, and `cc` leaves
    # ``.ssh`` reachable, so this is a private-key-exposure vector. Instead,
    # walk a small fixed list of trusted system shell paths (never a bash
    # fallback: bash brace expansion hides `cat ~/.a{w,w}s/credentials` from
    # the tokenizer). Some hosts (macOS /bin/sh is bash-in-POSIX-mode) still
    # perform brace expansion under the `sh` name, so PROBE the candidate:
    # dash / ash / a real POSIX sh preserve the literal; bash-in-any-mode fails.
    for candidate in ("/bin/sh", "/usr/bin/sh"):
        if os.path.isfile(candidate) and _shell_is_posix_strict(candidate):
            return candidate
    return None


# Per-shell-path memoization for the POSIX-strict probe. The probe itself
# spawns a child, so caching it means each candidate is fingerprinted at most
# once per gateway process; a subsequent command cron with the same resolved
# shell does no extra work.
_POSIX_STRICT_CACHE: dict[str, bool] = {}


def _shell_is_posix_strict(shell: str) -> bool:
    """Return True iff *shell* refuses brace expansion (POSIX-sh semantics).

    Runs ``<shell> -c 'echo x.{a,a}'`` in an OS sandbox (strict tier, cron env)
    and requires the OUTPUT to be the literal ``x.{a,a}``. dash / ash / a real
    POSIX sh preserve it; bash (including macOS's ``/bin/sh`` which is
    bash-in-POSIX-mode) expands to ``x.a x.a``. Refusing an expanding shell is
    the only reliable defense: the vet gate (``mcp_cron._vet_shell_command``)
    tokenizes the stored string once, so any downstream re-expansion silently
    widens what a legitimate deny-list can see.

    The probe is SANDBOX-ROUTED as a defense-in-depth belt on the fixed
    trusted-path lookup in ``_resolve_command_shell``. If a future change ever
    widens that resolver to consult PATH again, the sandbox wrap here still
    denies an agent-planted shim the un-isolated execution it would need.
    """

    cached = _POSIX_STRICT_CACHE.get(shell)
    if cached is not None:
        return cached
    sandbox_cleanup: str | None = None
    try:
        argv, sandbox_cleanup = wrap_argv([shell, "-c", "echo x.{a,a}"], mode="strict")
        # Same discipline as every other sandbox-routed spawn in this module
        # (test_every_routed_spawn_applies_resource_limits / _cgroup_scope): the
        # probe is a child process, so it observes the same fork-bomb / RSS
        # ceilings as a real command cron. run_limited applies them after exec,
        # and is a no-op on Windows where there are no POSIX rlimits.
        argv = cgroup_scope_argv(argv)
        proc = run_limited(
            argv,
            capture_output=True,
            text=True,
            timeout=5,
            env=_clean_cron_env(),
        )
        result = proc.returncode == 0 and proc.stdout.strip() == "x.{a,a}"
    except (OSError, subprocess.SubprocessError, SandboxUnavailableError):
        result = False
    finally:
        if sandbox_cleanup:
            try:
                os.unlink(sandbox_cleanup)
            except OSError:
                pass
    _POSIX_STRICT_CACHE[shell] = result
    return result


def run_command_sandboxed(
    command: str,
    timeout: int = 300,
    job_id: str | None = None,
    secret_env: dict[str, str] | None = None,
    secret_env_pin: str = "",
) -> dict:
    """Run a shell command in a sandboxed subprocess via wrap_argv().

    Returns: {"status": "ok"|"error"|"cancelled", "output": "...", "exit_code": N}

    ``secret_env``/``secret_env_pin`` exist only as a fail-closed guard:
    secret grants apply to SCRIPT jobs exclusively (a pin over the command
    TEXT cannot cover the bytes of a helper file the command invokes), and
    every product surface refuses to store one for a command job — so a
    non-empty grant here means the store was edited outside the product, and
    the run refuses rather than executing with or without the secrets.
    """
    if secret_env:
        return {
            "status": "error",
            "output": "❌ secret grants apply only to script jobs; this "
            "command job carries a grant the product could not have written — "
            "remove it from the cron store.",
            "exit_code": -1,
        }
    shell = _resolve_command_shell()
    if shell is None:
        return {
            "status": "error",
            "output": (
                "❌ No POSIX shell available to run this command cron. Command "
                "crons execute with `sh -c` under POSIX-sh semantics (what the "
                "storage-time vet gate assumes); Windows ships no such shell "
                "(Git for Windows's sh.exe is bash and would widen the language "
                "past the vet). Use a script cron or an LLM `message` cron on "
                "this platform, or run the gateway under POSIX."
            ),
            "exit_code": -1,
        }
    argv = [shell, "-c", command]
    # mode="cc" (not "standard"): the command string is fully model-supplied via
    # cron_add and executes outside the kiro-cli ACP permission/hook flow, so this
    # is a low-trust exec path. "cc" hides the credential dirs/files (.aws, .kube,
    # .netrc, .git-credentials, .npmrc, .pypirc, .kirocrew/.env) and scrubs the
    # agent-denied env keys, while deliberately leaving ~/.ssh reachable so a
    # legitimate command cron can still do git/scp/rsync over SSH. "strict" would
    # additionally hide ~/.ssh but break those workflows; the residual .ssh
    # exposure is covered by the storage-time deny-list (mcp_cron._vet_shell_command,
    # which blocks any .ssh reference) — the primary control. This sandbox is
    # defense-in-depth and is bypassed when the OS backend falls back to "none"
    # (e.g. macOS >= 26 — see _clean_cron_env).
    #
    # wrap_argv is INSIDE the try: on a host with no OS sandbox backend (every
    # Windows host) it fail-closes by raising, and outside the try that escaped
    # this function entirely — the scheduler's caller saw a bare exception
    # instead of a job it could mark failed, so the remedy never reached the user.
    sandbox_cleanup: str | None = None
    try:
        sandboxed_argv, sandbox_cleanup = wrap_argv(argv, mode="cc")
        sandboxed_argv = cgroup_scope_argv(sandboxed_argv)  # cgroup DoS ceiling
        clean_env = _clean_cron_env()
        proc = popen_limited(
            sandboxed_argv,
            stdin=None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=clean_env,
            start_new_session=True,
        )
        if job_id:
            _register_proc(job_id, proc)
        cancelled = False
        try:
            try:
                output, stderr_out = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_proc_group(proc)
                _drain_after_kill(proc, job_id)
                return {
                    "status": "error",
                    "output": f"❌ Command timed out after {timeout}s",
                    "exit_code": -1,
                }
        finally:
            if job_id:
                cancelled = _unregister_proc(job_id, proc)
        if cancelled:
            return {
                "status": "cancelled",
                "output": "Cancelled by user",
                "exit_code": proc.returncode,
            }
        if len(output) > _MAX_COMMAND_OUTPUT:
            output = output[:_MAX_COMMAND_OUTPUT] + "\n\n[truncated — output exceeded 64KB]"
        if proc.returncode != 0:
            output = f"⚠️ Exit code {proc.returncode}\n\n{output}"
            if stderr_out:
                # A command that dies hard leaves its diagnosis last: report the
                # stderr tail, not the head, so a chatty startup warning can't
                # displace the terminal error. Redact the complete stderr BEFORE
                # truncating: slicing first could cut off a credential's
                # detectable prefix (e.g. the scheme of a token-bearing URL),
                # letting the raw secret tail through redaction.
                output += f"\n\nstderr:\n{redact(stderr_out.rstrip())[-1000:]}"
        return {
            "status": "ok" if proc.returncode == 0 else "error",
            "output": output,
            "exit_code": proc.returncode,
        }
    except SandboxUnavailableError as exc:
        return {
            "status": "error",
            "output": f"{_SANDBOX_UNAVAILABLE_PREFIX}{exc}",
            "exit_code": -1,
        }
    except Exception as exc:
        return {"status": "error", "output": f"❌ Command failed: {exc}", "exit_code": -1}
    finally:
        if sandbox_cleanup:
            Path(sandbox_cleanup).unlink(missing_ok=True)
