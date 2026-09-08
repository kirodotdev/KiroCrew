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
first. There is exactly one case where it needs no answer: when the port is this
gateway's OWN bound port, the listener is this very process, and there is no
second party to prove anything about. For any other port
:func:`kiro_crew.port_resolution.port_is_gateway_owned` must confirm the
listener is this user's gateway, and the mint is REFUSED when it cannot — never
downgraded to "send it anyway and see". That is the same posture, for the same
reason, as ``pod.runtime.mint_token``: refusing a live gateway costs an error
message, while proceeding on an unproven port hands a credential to whatever
answered.

The token is returned in memory only and is never logged.
"""

from __future__ import annotations

import logging

import aiohttp

from kiro_crew.config.loader import config_dir
from kiro_crew.instances.constants import DEFAULT_LOOPBACK_MINT_TIMEOUT_SECS
from kiro_crew.instances.run_marker import read_secret, secret_path
from kiro_crew.instances.token_mint import TokenMintError, _validate_ttl
from kiro_crew.instances.validation import DEFAULT_LOOPBACK_HOST
from kiro_crew.port_resolution import port_is_gateway_owned

logger = logging.getLogger(__name__)


async def mint_loopback_token(
    port: int,
    *,
    own_port: int,
    loopback_host: str = DEFAULT_LOOPBACK_HOST,
    ttl: str = "20h",
    embed_parent_port: int | None = None,
    timeout_secs: float = DEFAULT_LOOPBACK_MINT_TIMEOUT_SECS,
) -> str:
    """Mint a dashboard token from the gateway listening on *loopback_host*:*port*.

    *own_port* is the port THIS gateway bound. When it equals *port* the
    destination is this process and the ownership proof is skipped as
    inapplicable (see the module docstring); otherwise the proof is required and
    a mint that cannot obtain it raises rather than sending the credential.

    *loopback_host* must already be validated by
    :func:`kiro_crew.instances.validation.validate_loopback_host`.

    *embed_parent_port* becomes the minted token's signed CSP frame-ancestor
    claim, exactly as it does on the ssh and ssm paths.

    Raises :class:`TokenMintError` when the credential is missing, the listener
    is unproven, the request fails, or the reply carries no token.
    """
    ttl = _validate_ttl(ttl)
    port = int(port)
    is_self = port == int(own_port)
    if not is_self and not port_is_gateway_owned(port):
        raise TokenMintError(
            f"refusing to mint a token on loopback port {port}: could not prove "
            f"that a Kiro Crew gateway of yours holds it (the pid sidecar in "
            f"{config_dir()}/run is missing, or the listener could not be "
            f"attributed on this host), and this call would put that gateway's "
            f"internal secret on the wire to whatever answered. Point the "
            f"instance at a gateway running in this data home, or use the ssh "
            f"transport."
        )
    secret = read_secret(port)
    if not secret:
        raise TokenMintError(
            f"no internal credential recorded for loopback port {port} "
            f"({secret_path(port)}). A gateway in this data home writes that "
            f"file for the port it serves, so either nothing of yours is "
            f"listening there or it belongs to a different data home."
        )

    url = f"http://{loopback_host}:{port}/api/token/local"
    params: dict[str, str] = {"ttl": ttl}
    if embed_parent_port:
        params["embed_parent_port"] = str(int(embed_parent_port))
    logger.info(
        "Minting token on loopback port %d (ttl=%s, self=%s)", port, ttl, is_self
    )  # no token, no secret in logs
    timeout = aiohttp.ClientTimeout(total=timeout_secs)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Loopback-only, and the host is a validated loopback literal, so the
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
