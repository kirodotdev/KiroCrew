"""Owner consent to forward the ssh-agent socket into the agent sandbox.

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
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kiro_crew.config.loader import ssh_auth_sock_consent_path

logger = logging.getLogger(__name__)


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
