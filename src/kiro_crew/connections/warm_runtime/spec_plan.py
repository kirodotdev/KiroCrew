"""The desired plan: what one warm process must enumerate, and when a resident one still serves.

The plan is an immutable value. Its roster is registry-derived and blind to grant and cancel
state, because a plan tracking who needs a URL right now changed on every completed consent and
every Cancel, and each change retired a process holding other cards' consent listeners. So this
module answers three questions and nothing else:

- what authorization a provider's registry entry asks for (:func:`_registry_server_entry`,
  :func:`_auth_shape`), including the operator's pre-registered client;
- whether the warm process may activate a provider at all, given the entry the user configured
  (:func:`_warm_mintable_entry`);
- whether a RESIDENT plan can serve a wanted one without a respawn (:func:`_plan_is_servable`)
  and without also mounting a provider the scan excluded (:func:`_resident_roster_is_asked_for`).

The facade builds the value (``warm._warm_spec_plan`` reads the configured agent spec through the
hardened reader, whose call site is pinned to ``warm.py``) and stamps the spec bodies it holds;
the reuse decision itself is made in ``warm._WarmMintRuntime._ensure_locked``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kiro_crew.connections.registry import Provider, is_preregistered
from kiro_crew.mcp_utils import (
    kiro_entry_client_id,
    kiro_entry_scopes,
    kiro_oauth_wire_entry,
    mcp_server_alias,
)


def _operator_oauth_client(provider: Provider) -> Any:
    """The operator's pre-registered client for ``provider``, or ``None``.

    ``None`` both for a DCR provider (nothing to resolve) and for a pre-registered
    one the operator has not configured. Reads config and the vault on every call:
    the warm planner runs once per spawn, not per request, and a cached value would
    outlive the Settings write that is the whole reason a re-plan happens.
    """
    if not is_preregistered(provider):
        return None
    from kiro_crew.config import config_dir
    from kiro_crew.config.loader import read_config_for_update
    from kiro_crew.connections.oauth_clients import resolve_oauth_client
    from kiro_crew.secrets import SecretVault

    try:
        config = read_config_for_update()
    except Exception:  # noqa: BLE001 -- an unreadable config reads as "not configured"
        config = {}
    return resolve_oauth_client(provider, config=config, vault=SecretVault(config_dir()))


def _registry_server_entry(provider: Provider) -> dict[str, Any] | None:
    """The remote MCP entry the registry implies for ``provider``, in wire shape.

    A pre-registered provider's entry carries the operator's client as well, or
    ``None`` when the operator has not configured one: there is nothing the warm
    process could authorize against, and the card is already saying so.
    """
    entry: dict[str, Any] = {"url": provider["mcp_url"]}
    scopes = provider.get("recommended_scopes") or []
    if scopes:
        entry["scopes"] = list(scopes)
    client_id = provider.get("client_id")
    if client_id:
        entry["clientId"] = client_id
    # store_entry=None: registry-derived, so no store owns it.
    wire = kiro_oauth_wire_entry(entry, store_entry=None, server=str(provider["slug"]))
    if not is_preregistered(provider):
        return wire
    from kiro_crew.connections.oauth_clients import apply_preregistered_oauth_client

    resolved = _operator_oauth_client(provider)
    if resolved is None:
        return None
    return apply_preregistered_oauth_client(wire, resolved)


def _auth_shape(entry: dict[str, Any]) -> tuple[str, tuple[str, ...], str]:
    """The fields of an MCP entry that decide what an authorization asks for."""
    return (
        str(entry.get("url") or ""),
        tuple(kiro_entry_scopes(entry)),
        kiro_entry_client_id(entry),
    )


def _warm_mintable_entry(
    provider: Provider, configured: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The REGISTRY entry the warm process would activate, or None if it cannot.

    Registry-derived on purpose: a plan built from the user's config changed on every
    Connect click, respawning a process holding other cards' live listeners.

    None in two cases: no usable auth configuration (a pre-registered provider whose
    operator has not entered a client yet -- ``_registry_server_entry`` already answers
    None for it -- or a non-DCR provider carrying no client id at all), or a CONFIGURED
    entry asking for something different from the registry, which only the cold path
    can honour without handing back a grant the user did not ask for.
    """
    entry = _registry_server_entry(provider)
    if entry is None:
        return None
    expectations: dict[str, Any] = dict(provider.get("l0_expectations") or {})
    # Through the accessor: the wire shape nests the client id under ``oauth``, so a
    # bare ``clientId`` lookup reads every registered non-DCR provider as unregistered.
    if not bool(expectations.get("dcr")) and not kiro_entry_client_id(entry):
        return None
    if isinstance(configured, dict):
        compared = configured
        if is_preregistered(provider):
            # The store entry a Connect click writes is ``{url}`` alone; the operator's
            # client joins it only when the agent spec is emitted. Compare what the
            # runtime will actually see, or every configured pre-registered provider
            # reads as "asking for something different" and never warms.
            from kiro_crew.connections.oauth_clients import apply_preregistered_oauth_client

            resolved = _operator_oauth_client(provider)
            if resolved is not None:
                compared = apply_preregistered_oauth_client(configured, resolved)
        if _auth_shape(compared) != _auth_shape(entry):
            return None
    return entry


def _wanted_aliases(providers: list[Provider]) -> frozenset[str]:
    """The server aliases an activation must produce a challenge for."""
    return frozenset(mcp_server_alias(provider["slug"]) for provider in providers)


@dataclass(frozen=True)
class _WarmSpecPlan:
    """Every agent spec the warm process needs, plus a digest of their contents.

    ``entries`` is the plan's roster, keyed by ``mcp_server_alias`` -- the identity this whole
    module works in (``_wanted_aliases`` activates by alias, and both reuse tests compare
    these keys), so it is also what says whether a candidate survived the scan's vetoes.
    """

    all_agent: str
    specs: dict[str, dict[str, Any]]
    entries: dict[str, dict[str, Any]]
    digest: str


def _plan_is_servable(resident: _WarmSpecPlan, wanted: _WarmSpecPlan) -> bool:
    """True when the RUNNING process's specs can still serve ``wanted``.

    Digest equality is the wrong test alone: it reads a set that SHRANK as a set that
    changed. The only thing a respawn can fix is a server the process was never told
    about, so a plan whose every entry is already resident with an identical authorization
    ask is servable -- and replacing the process would strand its peers' listeners for
    nothing. A changed url/scopes/client id is genuine incompatibility: authorizing the
    resident ask would hand back the wrong grant.

    Reuse is only sound for an activation whose MODE mounts nothing this scan excluded --
    see :func:`_resident_roster_is_asked_for`. Servability answers "can this process serve
    these servers at all"; it deliberately says nothing about what else the mode mounts.
    """
    if not resident.all_agent:
        return False
    return all(resident.entries.get(alias) == entry for alias, entry in wanted.entries.items())


def _resident_roster_is_asked_for(resident: _WarmSpecPlan, wanted: _WarmSpecPlan) -> bool:
    """True when the resident ALL-AGENT mode mounts nothing ``wanted`` excluded.

    THE reason this is separate from servability. Specs are enumerated ONCE at spawn and a
    warm session injects an empty ``mcp_servers`` list, so the servers an activation
    initializes are fixed by the spec the NAMED mode carried when the process started --
    rewriting the file afterwards moves nothing, and passing the wanted subset through
    ``session/new`` kills the process with every pending verifier in it. The mounted set is
    therefore not a thing an activation can narrow: the only way to stop initializing a
    provider is to stop using the mode that lists it.

    So a plan that merely SHRANK is servable but not reusable IN BULK: the resident
    all-agent mode still lists the excluded provider, ``set_mode`` initializes its MCP
    server, and an authorization request goes out for exactly the provider
    :func:`_warm_mintable_entry` vetoed -- filtering the RESULT leaves that request made.
    A strict shrink therefore respawns, which is not the same as stranding a peer: a process
    still holding a redeemable code is PARKED, keeps its generation live, and is retired by
    the drain once its rows are gone.

    Paired with -- never a substitute for -- :func:`_plan_is_servable`. Servability alone
    reuses a shrink, which is the defect this exists to close; the two together admit reuse
    only for a roster that is neither more nor less than what the scan asked for.
    """
    return resident.entries.keys() <= wanted.entries.keys()
