"""How Kiro Crew reaches KAS: kiro-cli's own ACP relay, not a private spawn.

kiro-cli exposes the KAS (Kiro Agent Server) engine through its ``acp``
subcommand -- ``--agent-engine v3`` selects it, and ``--auth-method cli`` makes
the relay resolve access tokens from kiro-cli's own credential store. The relay
forwards ACP frames to KAS in both directions, so Kiro Crew speaks ordinary ACP
to a kiro-cli process and never touches KAS's bundle, its Node runtime, or its
tokens.

This replaces an earlier arrangement where Crew located kiro-cli's *extracted*
KAS bundle itself and ran ``node .../acp-server.js --auth=acp-callback``. That
worked, but it made Crew depend on kiro-cli's internal on-disk layout (a
``{data_dir}/kas/{version}-{hash}/node_modules/@kiro/agent/...`` path that
kiro-cli is free to change) and it put Crew in the credential path: KAS asked
Crew for a token over ``_kiro/auth/getAccessToken`` and Crew shelled out to a
hidden ``kiro-cli chat _ get-kas-token`` verb to answer. The relay owns both
concerns, so neither belongs here any more.

Frame parity between the two routes was measured before the switch, not assumed:
all forward methods Crew sends (``initialize``, ``session/new`` carrying
``_meta.kiro.customAgents``, ``session/set_mode``, ``session/prompt``,
``session/cancel``, ``session/load``, ``_kiro/session/delete``) behave
identically, and every reverse frame Crew consumes arrives unchanged --
``session/update`` (including the ``_meta.kiro`` display kinds
:mod:`kiro_crew.acp.kas_wire` matches on), ``session/request_permission``
round-trips, and the connection-level notifications. The relay additionally
advertises two extension methods the direct route did not, so its surface is a
superset. The one frame that stops arriving is ``_kiro/auth/getAccessToken``,
which is the point.

Sandbox posture is unchanged by the switch, and this is the reason Crew's own
seatbelt must stay on (see ``ACP_BACKENDS_INTERNAL_SANDBOX`` in
:mod:`kiro_crew.acp.types`). KAS implements its own OS sandbox -- seatbelt on
macOS, bubblewrap on Linux -- selected by an ``--sandbox`` argument on its ACP
server, and it wraps each bash command rather than the agent process. kiro-cli's
relay does NOT pass that argument (``spawn_kas_process`` builds exactly
``node --experimental-wasm-modules <acp-server.js> --transport=stdio
--auth=acp-callback``), and KAS's sandbox factory returns its no-op backend for
an absent config. So KAS runs with no OS sandbox of its own either way: the argv
kiro-cli builds is byte-for-byte the argv Crew used to build itself. Two
consequences: there is no inner sandbox that could fail to nest inside Crew's
seatbelt, and there is nothing for Crew to delegate isolation TO -- claiming the
delegation would leave KAS unconfined on macOS.
"""

from __future__ import annotations

#: kiro-cli subcommand that speaks ACP on stdio.
KAS_RELAY_SUBCMD = "acp"

#: Engine selector. kiro-cli's ``acp`` defaults to ``v2`` (its own agent loop);
#: ``v3`` is the KAS engine. Stated explicitly rather than relying on a default,
#: because the default is kiro-cli's to change and a silent fall back to v2 would
#: look like KAS working while serving a different agent entirely.
KAS_RELAY_ENGINE_FLAG = "--agent-engine"
KAS_RELAY_ENGINE = "v3"

#: Auth owner. ``cli`` keeps token resolution inside the kiro-cli process, which
#: already holds the OIDC refresh token. Without it the engine would expect its
#: host (Crew) to answer ``_kiro/auth/getAccessToken``, which is exactly the
#: credential-path involvement this module exists to remove.
KAS_RELAY_AUTH_FLAG = "--auth-method"
KAS_RELAY_AUTH_OWNER = "cli"


def build_kas_argv(kiro_bin: str) -> list[str]:
    """argv for a KAS stdio session served by kiro-cli's ACP relay.

    No ``--agent``: Crew binds its agent by sending ``_meta.kiro.customAgents``
    on ``session/new`` and then activating it with ``session/set_mode`` (see
    :mod:`kiro_crew.acp.kas_agents`), which the relay forwards. Passing an
    ``--agent`` here would name a kiro-cli mode instead, and the wire-injected
    agent is the one Crew's governance ceiling has actually filtered.

    No ``--model`` either: the model is chosen per session over the wire, so
    pinning one at process start would apply it to every session on this
    process.
    """
    if not kiro_bin:
        raise ValueError("kiro_bin must be a non-empty path to kiro-cli")
    return [
        kiro_bin,
        KAS_RELAY_SUBCMD,
        KAS_RELAY_ENGINE_FLAG,
        KAS_RELAY_ENGINE,
        KAS_RELAY_AUTH_FLAG,
        KAS_RELAY_AUTH_OWNER,
    ]
