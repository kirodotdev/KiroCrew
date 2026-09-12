"""Token mint for the ``loopback`` transport — no ssh, no ssm, no forwarder.

The ssh and ssm minters reach a REMOTE shell and run ``kirocrew token`` there.
A loopback instance has no remote: the destination gateway is already listening
on this host's own loopback, and it exposes the mint over HTTP at
``/api/token/local``, gated on the internal secret it wrote for its own port.
So this module dials that endpoint directly — the same call
:func:`kiro_crew.pod.runtime.mint_token` makes, and deliberately modelled on it,
including its refusal to proceed on an unproven listener.

**Which credential, and why it is per port.** ``run/gateway-<port>.secret``
belongs to ONE gateway generation, is written ``0600`` inside the ``0700``
``run/`` dir, and is the value that gateway's auth middleware compares against.
This module reads only that file and never falls back to the shared
``.local_secret``: that file is last-writer-wins per data home, so on a host
running two gateways it names whichever started most recently, and sending it to
the other one both fails and puts a live credential on the wire for a listener
it does not authenticate. The per-port file is the credential paired with the
listener actually being dialled.

**When a listener must prove itself.** A port carries no evidence of who holds
it, and the credential goes on the wire before any reply comes back — so the
question "is a Kiro Crew gateway of mine listening there" has to be answered
first. There is exactly one case where it needs no answer: when the destination
is this gateway's OWN bound port AND this gateway binds the exact
``127.0.0.1`` address, the listener is this very process, and there is no second
party to prove anything about. Port equality alone does NOT establish that — a
gateway bound to one interface (``KIROCREW_BIND=192.168.1.5``) leaves
``127.0.0.1`` on its own port free for any other local process. A wildcard bind
is not enough either: on macOS/BSD a more-specific ``127.0.0.1:<port>`` listener
can coexist and wins dispatch. For every other port or bind address
:func:`kiro_crew.port_resolution.port_is_gateway_owned_on_loopback` must confirm
the listener is this user's gateway, and the mint is REFUSED when it cannot —
never
downgraded to "send it anyway and see". That is the same posture, for the same
reason, as ``pod.runtime.mint_token``: refusing a live gateway costs an error
message, while proceeding on an unproven port hands a credential to whatever
answered.

**The proof covers the address, not just the port.** A port carries no evidence
of who holds it AND no evidence of which address they hold it on, while the
request goes to an (address, port) pair. Both halves are proved:
``port_is_gateway_owned_on_loopback`` requires the recorded gateway to own a
listener that a ``127.0.0.1`` connect actually reaches — the exact loopback bind,
the IPv4 wildcard, or a dual-stack ``[::]`` socket when nothing more specific
exists — under the kernel's own most-specific-bind dispatch. That is what a
port-scoped answer cannot supply: ``KIROCREW_BIND=<interface addr>`` is supported
config, and a gateway bound that way leaves ``127.0.0.1:<port>`` free for any
local process, so the port-scoped proof would accept our gateway's pid while the
secret travelled to whatever took loopback. The destination is the fixed
``constants.LOOPBACK_HOST`` for the same reason: the address the proof speaks
for is the address dialled, and no record can name another.

The token is returned in memory only and is never logged.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from kiro_crew.config.loader import config_dir
from kiro_crew.instances.constants import DEFAULT_LOOPBACK_MINT_TIMEOUT_SECS, LOOPBACK_HOST
from kiro_crew.instances.run_marker import read_secret, secret_path
from kiro_crew.instances.token_mint import TokenMintError, _validate_ttl
from kiro_crew.port_resolution import port_is_gateway_owned_on_loopback

logger = logging.getLogger(__name__)

# Only an exact 127.0.0.1 bind proves that this process owns the listener a
# 127.0.0.1:<port> connect reaches. A wildcard socket is not enough: on
# macOS/BSD, a more-specific 127.0.0.1:<port> listener can coexist and wins
# dispatch. A wildcard-bound gateway therefore runs the ordinary ownership
# proof before any credential-bearing dial.
_LOOPBACK_COVERING_BINDS = frozenset({LOOPBACK_HOST})


def _bind_covers_loopback(bind_host: str) -> bool:
    """Return True only when *bind_host* proves ownership at loopback.

    This decides whether "the destination port is my own port" is enough to
    conclude "the destination listener is my own process". It is enough only
    when this gateway binds the exact ``127.0.0.1`` address.

    A gateway bound to ONE interface (``KIROCREW_BIND=192.168.1.5``) serves
    ``192.168.1.5:<port>`` and leaves ``127.0.0.1:<port>`` free for any other
    local process. A gateway bound to ``0.0.0.0`` is not conclusive either:
    macOS/BSD can dispatch a more-specific loopback bind ahead of its wildcard
    socket. ``::`` is excluded too because whether an IPv6 wildcard accepts
    IPv4 loopback depends on ``bindv6only``.

    Being wrong the safe way is cheap: every excluded address runs the ordinary
    ownership proof, which passes when this user's gateway genuinely holds the
    loopback listener.
    """
    return bind_host.strip() in _LOOPBACK_COVERING_BINDS


async def mint_loopback_token(
    port: int,
    *,
    own_port: int,
    own_bind_host: str = "",
    ttl: str = "20h",
    embed_parent_port: int | None = None,
    timeout_secs: float = DEFAULT_LOOPBACK_MINT_TIMEOUT_SECS,
) -> str:
    """Mint a dashboard token from the gateway listening on ``LOOPBACK_HOST``:*port*.

    *own_port* is the port THIS gateway bound and *own_bind_host* the address it
    bound it on. The ownership proof is skipped as inapplicable only when the
    two together put the destination inside this very process — the same port,
    on this process's exact :data:`LOOPBACK_HOST` bind (see
    :func:`_bind_covers_loopback`). A wildcard-bound hub runs the proof because
    a more-specific loopback listener can outrank its socket. In every other
    case, an unstated bind address included, the proof is required and a mint
    that cannot obtain it raises rather than sending the credential.

    The destination address is not a parameter: the proof attributes the
    listener a :data:`LOOPBACK_HOST` connect reaches, so that is the one address
    it can speak for, and the request goes there.

    *embed_parent_port* becomes the minted token's signed CSP frame-ancestor
    claim, exactly as it does on the ssh and ssm paths.

    Raises :class:`TokenMintError` when the credential is missing, the listener
    is unproven, the request fails, or the reply carries no token.
    """
    ttl = _validate_ttl(ttl)
    port = int(port)
    # The carve-out needs BOTH halves of "the listener is this very process":
    # the same port, and this process's exact loopback bind. Port equality alone
    # leaves room for a foreign exact loopback listener when this gateway binds
    # another interface or a wildcard address. Without the exact bind the
    # premise is unverifiable, so the proof runs.
    is_self = port == int(own_port) and _bind_covers_loopback(own_bind_host)
    if not is_self and not await asyncio.to_thread(port_is_gateway_owned_on_loopback, port):
        raise TokenMintError(
            f"refusing to mint a token on loopback port {port}: could not prove "
            f"that a Kiro Crew gateway of yours answers at "
            f"{LOOPBACK_HOST}:{port} (the pid sidecar in "
            f"{config_dir()}/run is missing, the listener could not be "
            f"attributed on this host, or your gateway holds this port on "
            f"another address only), and this call would put that gateway's "
            f"internal secret on the wire to whatever answered. Run the "
            f"destination in this data home on {LOOPBACK_HOST}, and install or "
            f"repair lsof so this POSIX host can attribute its listener. "
            f"Otherwise the loopback transport cannot connect to it."
        )
    secret = read_secret(port)
    if not secret:
        raise TokenMintError(
            f"no internal credential recorded for loopback port {port} "
            f"({secret_path(port)}). A gateway in this data home writes that "
            f"file for the port it serves, so either nothing of yours is "
            f"listening there or it belongs to a different data home."
        )

    url = f"http://{LOOPBACK_HOST}:{port}/api/token/local"
    params: dict[str, str] = {"ttl": ttl}
    if embed_parent_port:
        params["embed_parent_port"] = str(int(embed_parent_port))
    logger.info(
        "Loopback mint on port %d (ttl=%s, self=%s)", port, ttl, is_self
    )  # logs the port and ttl only, never the minted value
    timeout = aiohttp.ClientTimeout(total=timeout_secs)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Loopback-only, and the host is the fixed loopback literal, so the
            # dynamic-URL SSRF audit rule does not apply.
            async with session.get(  # nosemgrep
                url,
                params=params,
                headers={"X-Local-Secret": secret},
                allow_redirects=False,
            ) as resp:
                if resp.status != 200:
                    raise TokenMintError(
                        f"gateway on loopback port {port} refused the mint " f"(HTTP {resp.status})"
                    )
                payload = await resp.json()
    except TokenMintError:
        raise
    except Exception as e:
        # type(e).__name__ only: an aiohttp error string can carry the request
        # URL, and the credential travels in a header rather than the URL, so
        # this is belt-and-braces on top of that.
        raise TokenMintError(
            f"could not reach the gateway on loopback port {port} " f"({type(e).__name__})"
        ) from e

    token = str(payload.get("token", "")) if isinstance(payload, dict) else ""
    if not token:
        raise TokenMintError(f"gateway on loopback port {port} returned an empty token")
    return token
