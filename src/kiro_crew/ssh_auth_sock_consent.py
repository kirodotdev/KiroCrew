"""Owner consent to forward the ssh-agent socket into a sandbox that hides ~/.ssh.

Most agent spawns do not need it. The ``standard`` (``auto``), ``cc`` and
``off`` tiers leave ``~/.ssh`` readable to the child, and there
``sandbox._forward_ssh_auth_sock`` keeps ``SSH_AUTH_SOCK`` by default. This
consent decides the remaining spawns: the ``strict`` tier, and an enforced
harness whose credential mask hides ``~/.ssh``.

Keeping ``SSH_AUTH_SOCK`` in the agent subprocess environment lets git commit
signing and git-over-SSH inside the sandbox reach the ssh-agent the operator
runs outside it. The socket grants USE of the operator's keys, not possession:
the private key material never enters the sandbox and, under the strict tier,
``~/.ssh`` stays read-denied. But USE is enough -- any code the agent runs can
authenticate as the operator through the socket for the lifetime of the session,
not commit signing alone.

Where the consent lives, and why not ``config.json``
----------------------------------------------------
``ssh_auth_sock_consent.json`` sits on the KEYSTONE floor
(``security._CREW_SECRET_LEAVES``), the same placement as ``computer_use.json``,
``aws_service_consent.json`` and ``oauth_endpoints.json``, and for the same reason: this is an
authorization, not a preference. ``config.json`` is writable by any auto-approved
agent shell, so consent stored there could be minted by a prompt-injected agent
-- it would flip its own ``SSH_AUTH_SOCK`` forwarding on, and a subagent it spawns
would then authenticate as the operator with keys the sandbox exists to keep out
of its reach. The OS sandbox mounts the keystone read-only for the agent's shell
and ``is_sensitive_path`` blocks the file tools, so the consent is un-flippable
from inside the sandbox.

How the operator grants it
--------------------------
From Settings > Security: the owner-gated ``POST /api/ssh-agent/consent``
(``dashboard/handlers/ssh_auth_sock_consent.py``) records the grant directly,
the same one-act shape ``handlers/aws_consent.py`` uses for
``aws_service_consent.json``. The handler's owner gate refuses app tokens and
allow-listed messaging non-owners; the keystone keeps the agent from writing
the file. There is deliberately no ``agent.*`` config field (that would be
agent-writable, the exact hole this closes) and no CLI verb (a terminal command
that records the grant on request is a grant an automated caller can take).
A hand-written ``{"enabled": true}`` in ``<config_dir>/ssh_auth_sock_consent.json``
keeps working: :func:`is_granted` reads exactly that field, so the hand edit and
the dashboard grant are indistinguishable at spawn. Withdrawal is the same
owner-gated route's DELETE.

Known limit, stated rather than papered over
--------------------------------------------
The grant is durable and coarse: once enabled, every later spawn forwards the
socket without asking again -- that is the point (an unattended cron must be able
to sign), and it is also the cost. The grant is read at agent SPAWN, so a
session already running when consent is granted or revoked keeps the
environment it started with; the change takes effect for new sessions. Every
spawn that forwards under the grant is resolved through :func:`is_granted`,
which fails closed on anything it cannot read as an explicit enable.

The second grant: named shared MCP gateway backends
---------------------------------------------------
``{"mcp_gateway_backends": ["<server>", ...]}`` in the same leaf names the pooled
MCP backends that may receive the ``SSH_AUTH_SOCK`` the gateway was STARTED with
(``mcp_gateway.agent_socket``). It is a separate field because it is a separate
grant: ``enabled`` reaches agent spawns inside a sandbox; this list reaches
backends that run as the operator OUTSIDE every sandbox and serve every session.
Neither field implies the other. Each named backend, and anything it runs, can
use the operator's agent -- ssh, git over ssh, and any sign-in the agent reaches
-- for as long as it lives.

A name receives the socket only when it is listed here exactly, its launch is
operator-approved (``mcp_gateway.launch_approval``), and it is not a Kiro Crew
control-plane backend. The gateway reads the list when it spawns gatewayd, and
gatewayd reads it again before each backend launch, so removing a name stops
every later launch from receiving the socket; a running backend keeps it until it
exits (idle eviction, or a gateway restart).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import ssh_auth_sock_consent_path

logger = logging.getLogger(__name__)

#: The field naming the shared MCP gateway backends that may receive the socket.
MCP_GATEWAY_BACKENDS_FIELD = "mcp_gateway_backends"

#: The punctuation an MCP server name may carry after its first character.
_SERVER_NAME_PUNCT = frozenset(".-_")


def _is_server_name(name: str) -> bool:
    """Whether *name* is a plain MCP server name.

    A name is never a pattern, so ``*`` is not one. It is non-empty, its first
    character is an ASCII letter or digit, and every character is an ASCII letter,
    an ASCII digit, or one of ``.``, ``-``, ``_``. ``str.isalnum`` alone accepts
    non-ASCII letters, so it is paired with ``str.isascii`` to match exactly.
    """
    if not name or not (name[0].isascii() and name[0].isalnum()):
        return False
    return all((ch.isascii() and ch.isalnum()) or ch in _SERVER_NAME_PUNCT for ch in name)


#: SEL operation prefix. Outcomes: ``granted`` / ``revoked`` / ``refused``.
AUDIT_EVENT = "ssh_auth_sock_forward_consent"

#: Serialises the read-modify-write of the store. An IN-PROCESS lock and
#: deliberately NOT a lock file: a sibling lock file is agent-reachable through a
#: runtime-constructed shell path, and an agent holding it could block the owner's REVOKE
#: -- a consent whose withdrawal the constrained party can deny is defective. One
#: writing process (the gateway's owner-gated handler) makes a process-local lock
#: sufficient.
_STORE_LOCK = threading.Lock()


@dataclass(frozen=True)
class Grant:
    """A recorded consent to forward ``SSH_AUTH_SOCK`` into the sandbox."""

    granted_at: str

    def to_dict(self) -> dict[str, Any]:
        # ``enabled`` is the field :func:`is_granted` reads, byte-for-byte what a
        # hand-written store carries; ``granted_at`` is the only addition.
        return {"enabled": True, "granted_at": self.granted_at}


def _read_all() -> dict[str, Any]:
    """The whole store, or ``{}`` when it is missing or unreadable.

    Failing soft is the right READ behaviour -- an authorization record that
    cannot be parsed is not an authorization, so the forward stays scrubbed.
    """
    try:
        raw = json.loads(ssh_auth_sock_consent_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError):
        logger.warning(
            "ssh-agent forward consent store is unreadable; treating the socket as unconfirmed"
        )
        return {}
    return raw if isinstance(raw, dict) else {}


def _read_for_write() -> dict[str, Any]:
    """The store a write rewrites: ``{}`` when it is missing or malformed.

    Unlike :func:`_read_all`, a read error (``OSError``) propagates. A store that
    could not be read may hold a valid backend grant, and writing over it would
    drop that grant, so the caller decides. A malformed store holds none, since
    :func:`mcp_gateway_backends_granted` already reads it as empty.
    """
    try:
        raw = json.loads(ssh_auth_sock_consent_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (ValueError, RecursionError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_all(data: dict[str, Any]) -> None:
    # Fail-loud lockdown BEFORE any content lands, same as the sibling keystone
    # stores: restrict_to_owner=True applies the owner-only mode to the temp file
    # before the payload reaches it, and the default restrict_on_error="raise"
    # refuses to write a record it cannot protect.
    atomic_write(
        ssh_auth_sock_consent_path(),
        json.dumps(data, indent=2, sort_keys=True),
        restrict_to_owner=True,
    )


def is_granted() -> bool:
    """Whether the operator has confirmed forwarding ``SSH_AUTH_SOCK``.

    Fails closed to False on a missing, unreadable, or malformed store, and only
    an explicit boolean ``True`` in the ``enabled`` field counts -- a truthy
    string or number does not, so a partially written or hand-edited store cannot
    forward the socket by accident. LOCAL only -- no network, no probe.
    """
    return _read_all().get("enabled") is True


def mcp_gateway_backends_granted() -> frozenset[str]:
    """The shared MCP backend names the operator lets receive the gateway's socket.

    Fails closed to an empty set on a missing, unreadable or malformed store, on
    an absent field, and on a field that is not a list of plain server names. One
    bad entry voids the whole field, so a typo can never grant more than the
    operator wrote. Names match an MCP server name exactly. LOCAL only.
    """
    names = _read_all().get(MCP_GATEWAY_BACKENDS_FIELD)
    if names is None:
        return frozenset()
    if not isinstance(names, list) or not all(
        isinstance(name, str) and _is_server_name(name) for name in names
    ):
        logger.warning(
            "ssh-agent forward consent: %r is not a list of MCP server names; "
            "no backend receives the socket",
            MCP_GATEWAY_BACKENDS_FIELD,
        )
        return frozenset()
    return frozenset(names)


def _kept_grants(data: dict[str, Any]) -> dict[str, Any]:
    """The grants a write of ``enabled`` must keep: the named-backend field.

    ``enabled`` and :data:`MCP_GATEWAY_BACKENDS_FIELD` are separate grants, so
    granting or withdrawing one never touches the other.
    """
    if MCP_GATEWAY_BACKENDS_FIELD in data:
        return {MCP_GATEWAY_BACKENDS_FIELD: data[MCP_GATEWAY_BACKENDS_FIELD]}
    return {}


def read_grant() -> Grant | None:
    """The recorded grant, or ``None`` when forwarding is not granted.

    Decided by :func:`is_granted` alone, so a hand-written ``{"enabled": true}``
    with no ``granted_at`` is a grant with an empty timestamp, never a refusal.
    """
    data = _read_all()
    if data.get("enabled") is not True:
        return None
    granted_at = data.get("granted_at")
    return Grant(granted_at=granted_at if isinstance(granted_at, str) else "")


def record_grant(*, granted_at: str) -> Grant:
    """Persist the operator's consent to forward the socket."""
    grant = Grant(granted_at=granted_at)
    with _STORE_LOCK:
        # The agent-spawn decision is REPLACED, so a stale foreign key cannot
        # survive a re-grant; the separate backend grant in the same leaf is kept.
        # No corrupt-sidecar preservation: a malformed store grants no backend
        # either, and a sidecar would be an unfenced artifact.
        _write_all({**_kept_grants(_read_for_write()), **grant.to_dict()})
    audit_decision(outcome="granted")
    return grant


def revoke() -> bool:
    """Withdraw consent. True when a grant was removed.

    Writes an explicit ``{"enabled": false}`` rather than unlinking the file: the
    leaf is precreated (as ``{}``) and sealed read-only for the sandbox before
    every spawn, so an absent file would only be recreated, and an explicit false
    reads unambiguously to a human inspecting the store by hand.

    The write happens WHETHER OR NOT the current record could be read. ``_read_all``
    fails soft to ``{}`` on an unreadable store, which is right for a read (no
    consent) and wrong for a withdrawal: skipping the write there would answer
    "not granted" while an enabled record stays on disk for the next spawn to
    read once the fault clears (``persist-before-you-publish``). So the only
    thing the read decides is the return value (whether a grant WAS held); a
    write failure propagates as ``OSError`` for the caller to report.
    """
    with _STORE_LOCK:
        try:
            data = _read_for_write()
            lost_backends = False
        except OSError:
            # The withdrawal must land, so it is written even though the
            # backend grant cannot be read back to keep. That loss is logged
            # and audited, so the owner knows to restore it.
            logger.warning(
                "ssh-agent forward consent store is unreadable; the withdrawal is "
                "written and any %r grant in it is not kept",
                MCP_GATEWAY_BACKENDS_FIELD,
            )
            data = {}
            lost_backends = True
        was_granted = data.get("enabled") is True
        _write_all({**_kept_grants(data), "enabled": False})
    if lost_backends:
        audit_decision(
            outcome="revoked",
            detail=f"store unreadable; any {MCP_GATEWAY_BACKENDS_FIELD} grant was not kept",
        )
    elif was_granted:
        audit_decision(outcome="revoked")
    return was_granted


def _harness_masks() -> dict[str, tuple[str, ...]]:
    """Each selectable harness's declared credential mask, as its spawn applies it.

    The same source ``kirocrew doctor`` reads for the withheld-socket notice:
    every backend in ``ACP_BACKEND_ROUTING`` with ``adapter_hidden_credential_dirs``.
    """
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_ROUTING
    from kiro_crew.agent_sdk.tool_gate import adapter_hidden_credential_dirs

    return {b: tuple(adapter_hidden_credential_dirs(b)) for b in sorted(ACP_BACKEND_ROUTING)}


def forwarding_harnesses() -> list[str]:
    """The harnesses whose new sessions keep the socket WITHOUT this grant, sorted.

    The spawn keeps ``SSH_AUTH_SOCK`` by default where the effective tier leaves
    ``~/.ssh`` readable AND the harness's credential mask does not hide it
    (``sandbox._forwards_without_consent``); for every other harness this consent
    decides. The Settings panel names these harnesses so "Not allowed" and
    Withdraw are never read as stopping a forward those sessions still get, and
    an empty list means the grant decides for every harness.

    Names are in the config-file spelling (``backend_to_stored``): Kiro CLI's
    runtime id is the empty string, which the panel could not name.

    Asks the spawn path's own predicate with each harness's declared mask, the
    same inputs the withheld-socket notice reads. Fails the way the spawn does:
    on Windows, or when the tier or a mask cannot be resolved, the spawn
    forwards nothing by default, so this reads as an empty list.
    Read-only; call it off the event loop (it may read the governance floor).
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:
        return []
    try:
        from kiro_crew.agent_sdk.backends import backend_to_stored
        from kiro_crew.sandbox import (  # circular import: sandbox imports this module lazily
            _forwards_without_consent,
            configured_sandbox_mode,
            effective_sandbox_mode,
        )

        effective = effective_sandbox_mode(configured_sandbox_mode())
        return sorted(
            backend_to_stored(name)
            for name, mask in _harness_masks().items()
            if _forwards_without_consent(effective, mask)
        )
    except Exception:
        logger.debug("ssh_auth_sock_consent: tier forward verdict unavailable", exc_info=True)
        return []


def harness_labels() -> dict[str, str]:
    """Every harness the forward verdict covers: config-file id -> display name.

    The Settings panel names the harnesses this grant decides (every key not in
    :func:`forwarding_harnesses`) from it. Keyed like that list, so the two
    compare directly; empty when the harness set cannot be resolved, which the
    panel reads as "name none".
    """
    try:
        from kiro_crew.agent_sdk.backends import backend_to_stored
        from kiro_crew.agent_sdk.tool_gate import label_for

        return {backend_to_stored(b): label_for(b) for b in _harness_masks()}
    except Exception:
        logger.debug("ssh_auth_sock_consent: harness labels unavailable", exc_info=True)
        return {}


def forwarded_without_consent(harnesses: list[str] | None = None) -> bool:
    """Whether EVERY harness's new sessions keep the socket without this grant.

    True only when :func:`forwarding_harnesses` names every selectable harness,
    so the grant decides for none of them on the configured tier. Pass the list
    already computed to avoid resolving it twice.
    """
    names = forwarding_harnesses() if harnesses is None else harnesses
    try:
        total = len(_harness_masks())
    except Exception:
        return False
    return bool(names) and len(names) == total


def socket_present() -> bool:
    """Whether a spawn under the grant would have an ssh-agent socket to forward.

    Answered the way the spawn path answers it, not by the gateway's own
    environment alone: the SDK's ``resolve_ssh_auth_sock`` repairs a missing or
    stale ``SSH_AUTH_SOCK`` from the well-known agent socket locations (launchd
    listeners on macOS, ``/tmp/ssh-*/agent.*``, the systemd user
    ``ssh-agent.socket`` and the keyring socket on Linux), so a gateway started
    as a launchd/systemd service with no variable in its environment still
    forwards a live socket. The Settings panel holds the Allow control on a False
    here, so it must read False only when a spawn would really forward nothing.
    Reached through ``kiro_crew.agent_sdk`` rather than the ACP layer (the
    agent-sdk-boundary gate refuses a new edge). Runs on a COPY of the
    environment: resolving must not mutate the gateway's own. Filesystem globs
    and stats, no connect; call it off the event loop.
    """
    from kiro_crew.agent_sdk.drivers.acp import resolve_ssh_auth_sock  # heavy module; lazy

    env = dict(os.environ)
    resolve_ssh_auth_sock(env)
    sock = env.get("SSH_AUTH_SOCK", "")
    # The resolver leaves a value it cannot replace in place, so a variable
    # pointing at a socket from an SSH login that has since ended survives it.
    # A path that is not there is nothing to forward: an ``exists`` check, no
    # connect. On Windows the variable is absent and the resolver is a no-op.
    return bool(sock) and os.path.exists(sock)


def audit_decision(*, outcome: str, detail: str = "") -> None:
    """Record a grant, a revocation or a refusal in the SEL.

    ``outcome`` is one of ``granted`` / ``revoked`` / ``refused``; a refusal's
    ``detail`` says what was refused (a non-owner caller, a failed write). A
    refused caller gets an error string
    back, but that string goes to the CALLER, while this log is what the owner
    reads, so the refusal is recorded here to reach them at all.

    Never raises: an audit failure must not be what stops a refusal from being
    enforced. Imported lazily because this module is reached from the sandbox
    spawn path, which must not pull the security-event stack for a read.
    """
    try:
        from kiro_crew.platform.context import redact_log_via_context
        from kiro_crew.sel import sel

        # Caller text bound for a durable audit field: redact over the FULL text
        # before the clip, so a credential straddling the boundary cannot survive
        # as an unmatchable prefix.
        sel().log_api_access(
            caller="owner" if outcome in ("granted", "revoked") else "gateway",
            operation=f"{AUDIT_EVENT}.{outcome}",
            outcome=outcome,
            source="ssh-auth-sock-consent",
            resources=redact_log_via_context(detail)[:200] if detail else "SSH_AUTH_SOCK",
        )
    except Exception:  # pragma: no cover - audit must never break the gate
        logger.debug("could not write the ssh-agent consent audit event", exc_info=True)
