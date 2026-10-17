"""How Kiro Crew reaches KAS: kiro-cli's own ACP relay, not a private spawn.

kiro-cli exposes the KAS (Kiro Agent Server) engine through its ``acp``
subcommand -- ``--agent-engine v3`` selects it. The relay forwards ACP frames to
KAS in both directions, so Kiro Crew speaks ordinary ACP to a kiro-cli process
and never touches KAS's bundle or its Node runtime.

Who answers the engine's credential callback is a per-spawn choice:

- ``--auth-method cli`` makes kiro-cli resolve access tokens from its own
  credential store and consume the engine's ``_kiro/auth/getAccessToken``
  request itself. This is the default here, and the whole story for an operator
  who signs in with ``kiro-cli login``.
- With the flag OMITTED, kiro-cli's v3 launcher leaves the wire untouched: the
  same request travels upstream to the ACP client, i.e. to Crew. Crew takes this
  shape when its own vault (:mod:`kiro_crew.auth`, the sign-in that lives in the
  dashboard) holds an identity, and answers from
  :meth:`kiro_crew.auth.provider.KasAuthProvider.get_access_token_callback`. The
  refresh token never leaves Crew; the engine sees an access token and its
  expiry.

Crew does NOT locate kiro-cli's *extracted* KAS bundle and run it: that would
make Crew depend on kiro-cli's internal on-disk layout (a
``{data_dir}/kas/{version}-{hash}/node_modules/@kiro/agent/...`` path that
kiro-cli is free to change). The relay owns that layout.

The second route is Crew's OWN bundle (``agent.kas_bundle = true``,
:mod:`kiro_crew.acp.kas_bundle`): a pinned ``@kiro/agent`` build plus a Node
runtime that ``kirocrew kas install`` fetched from a sha256-pinned manifest and
laid down under Crew's data home, where Crew controls the layout. The harness
then spawns ``node --experimental-wasm-modules <acp-server.js> --transport=stdio
--auth=acp-callback`` itself -- byte-for-byte the argv kiro-cli's relay builds
for the engine, minus kiro-cli. There is no ``--auth-method`` on this route:
the engine's ``_kiro/auth/getAccessToken`` always reaches Crew, and Crew's
vault is the only credential (:func:`build_kas_bundle_argv`).

Frame parity between the two routes is measured, not assumed:
all forward methods Crew sends (``initialize``, ``session/new`` carrying
``_meta.kiro.customAgents``, ``session/set_mode``, ``session/prompt``,
``session/cancel``, ``session/load``, ``_kiro/session/delete``) behave
identically, and every reverse frame Crew consumes arrives unchanged --
``session/update`` (including the ``_meta.kiro`` display kinds
:mod:`kiro_crew.acp.kas_wire` matches on), ``session/request_permission``
round-trips, and the connection-level notifications. The relay additionally
advertises two extension methods the direct route does not, so its surface is a
superset. ``_kiro/auth/getAccessToken`` is the one frame whose presence depends
on the auth owner chosen above.

Neither route brings a sandbox of its own, and this is the reason Crew's own
seatbelt must stay on (see ``ACP_BACKENDS_INTERNAL_SANDBOX`` in
:mod:`kiro_crew.acp.types`). KAS implements its own OS sandbox -- seatbelt on
macOS, bubblewrap on Linux -- selected by an ``--sandbox`` argument on its ACP
server, and it wraps each bash command rather than the agent process. kiro-cli's
relay does NOT pass that argument (``spawn_kas_process`` builds exactly
``node --experimental-wasm-modules <acp-server.js> --transport=stdio
--auth=acp-callback``), and KAS's sandbox factory returns its no-op backend for
an absent config. So KAS runs with no OS sandbox of its own either way: the argv
kiro-cli builds is byte-for-byte the argv Crew would build itself. Two
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

#: Auth owner flag. ``cli`` keeps token resolution inside the kiro-cli process,
#: which already holds that store's OIDC refresh token. OMITTING the flag is
#: kiro-cli's client-owned default: the engine's ``_kiro/auth/getAccessToken``
#: request reaches the ACP client, and Crew answers it from its own vault. There
#: is deliberately no third spelling here -- host ownership is the absence of the
#: flag, which is what every kiro-cli release that has the flag at all does.
KAS_RELAY_AUTH_FLAG = "--auth-method"
KAS_RELAY_AUTH_OWNER = "cli"

#: The connection-level request the engine raises for a credential when Crew is
#: the auth owner. Covenant method name only; the response shape Crew returns is
#: :meth:`kiro_crew.auth.provider.KasAuthProvider.get_access_token_callback`.
METHOD_KAS_AUTH_GET_ACCESS_TOKEN = "_kiro/auth/getAccessToken"

#: JSON-RPC error code Crew answers the callback with when it holds no usable
#: credential or cannot refresh one. Measured against kiro-cli 2.21.0's relay
#: (isolated HOME, host answering every callback with this code): the engine
#: does not wedge -- ``initialize`` and ``session/new`` still complete, it
#: re-asks on each attempt, and ``session/prompt`` fails with its own
#: ``-32000`` "not signed in ... Please sign in and retry"
#: (``ModelRegistryUnauthenticatedError`` / ``TokenExpiredError``), which is
#: the auth-failure vocabulary the runtime already maps to a sign-in prompt.
KAS_AUTH_CALLBACK_ERROR_CODE = -32000


def build_kas_argv(kiro_bin: str, *, host_auth: bool = False) -> list[str]:
    """argv for a KAS stdio session served by kiro-cli's ACP relay.

    ``host_auth=False`` (default) pins ``--auth-method cli``: kiro-cli owns the
    credential and Crew never sees the engine's token callback. This is
    byte-for-byte the argv Crew has always built, so an operator with no Crew
    sign-in gets exactly the previous behavior.

    ``host_auth=True`` omits the flag. kiro-cli then leaves the engine's
    ``_kiro/auth/getAccessToken`` request on the wire for Crew to answer from
    :mod:`kiro_crew.auth`. The caller decides this from whether the vault holds
    an identity (see ``AcpRuntime._resolve_spawn_plan``); this function only
    renders the choice.

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
    argv = [
        kiro_bin,
        KAS_RELAY_SUBCMD,
        KAS_RELAY_ENGINE_FLAG,
        KAS_RELAY_ENGINE,
    ]
    if not host_auth:
        argv += [KAS_RELAY_AUTH_FLAG, KAS_RELAY_AUTH_OWNER]
    return argv


#: Node flag the engine needs: its tree-sitter and z3 dependencies ship as wasm
#: modules imported with ESM syntax, which Node still gates behind this flag.
KAS_BUNDLE_NODE_FLAG = "--experimental-wasm-modules"
#: The engine's own CLI: stdio transport, and the token asked of the ACP client.
KAS_BUNDLE_TRANSPORT_ARG = "--transport=stdio"
KAS_BUNDLE_AUTH_ARG = "--auth=acp-callback"


def build_kas_bundle_argv(node_bin: str, acp_server: str) -> list[str]:
    """argv for a KAS stdio session served by Crew's own installed bundle.

    Crew is ALWAYS the auth owner on this route -- there is no kiro-cli to hand
    the credential to -- so the plan built from this argv carries
    ``host_auth=True`` unconditionally, and a vault with no identity surfaces as
    the engine's own "not signed in" prompt failure (the same vocabulary the
    runtime already maps to a sign-in prompt), never as a silent fallback to a
    credential store Crew does not own.

    No ``--agent`` and no ``--model``, for the reasons :func:`build_kas_argv`
    gives: both travel over the wire per session.
    """
    if not node_bin:
        raise ValueError("node_bin must be a non-empty path to a Node runtime")
    if not acp_server:
        raise ValueError("acp_server must be a non-empty path to the engine's acp-server.js")
    return [
        node_bin,
        KAS_BUNDLE_NODE_FLAG,
        acp_server,
        KAS_BUNDLE_TRANSPORT_ARG,
        KAS_BUNDLE_AUTH_ARG,
    ]
