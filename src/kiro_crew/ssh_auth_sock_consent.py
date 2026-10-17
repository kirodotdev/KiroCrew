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
``aws_service_consent.json``, ``oauth_endpoints.json`` and
``file_delivery_consent.json``, and for the same reason: this is an
authorization, not a preference. ``config.json`` is writable by any auto-approved
agent shell, so consent stored there could be minted by a prompt-injected agent
-- it would flip its own ``SSH_AUTH_SOCK`` forwarding on, and a subagent it spawns
would then authenticate as the operator with keys the sandbox exists to keep out
of its reach. The OS sandbox mounts the keystone read-only for the agent's shell
and ``is_sensitive_path`` blocks the file tools, so the consent is un-flippable
from inside the sandbox.

How the operator grants it
--------------------------
Like ``oauth_endpoints.json``, the operator writes the leaf out-of-band --
``{"enabled": true}`` in ``<config_dir>/ssh_auth_sock_consent.json`` -- from
outside the agent sandbox. There is deliberately no ``agent.*`` config field
(that would be agent-writable, the exact hole this closes) and no CLI verb (a
terminal command that records the grant on request is a grant an automated caller
can take). This module is READ-ONLY on purpose: the enable path is the operator's
own edit, and the sandbox spawn path only ever reads it, fail-closed.

Known limit, stated rather than papered over
--------------------------------------------
The grant is durable and coarse: once enabled, every later spawn forwards the
socket without asking again -- that is the point (an unattended cron must be able
to sign), and it is also the cost. Every spawn that forwards under the grant is
resolved through :func:`is_granted`, which fails closed on anything it cannot
read as an explicit enable.

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
import re
from typing import Any

from kiro_crew.config.loader import ssh_auth_sock_consent_path

logger = logging.getLogger(__name__)

#: The field naming the shared MCP gateway backends that may receive the socket.
MCP_GATEWAY_BACKENDS_FIELD = "mcp_gateway_backends"

#: A plain MCP server name. A name is never a pattern, so ``*`` is not one.
_SERVER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _read_all() -> dict[str, Any]:
    """The whole store, or ``{}`` when it is missing or unreadable.

    Failing soft is the right READ behaviour -- an authorization record that
    cannot be parsed is not an authorization, so the forward stays scrubbed.
    """
    try:
        raw = json.loads(ssh_auth_sock_consent_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.warning(
            "ssh-agent forward consent store is unreadable; treating the socket as unconfirmed"
        )
        return {}
    return raw if isinstance(raw, dict) else {}


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
        isinstance(name, str) and _SERVER_NAME.fullmatch(name) for name in names
    ):
        logger.warning(
            "ssh-agent forward consent: %r is not a list of MCP server names; "
            "no backend receives the socket",
            MCP_GATEWAY_BACKENDS_FIELD,
        )
        return frozenset()
    return frozenset(names)
