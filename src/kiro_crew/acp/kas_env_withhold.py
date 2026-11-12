"""Which ``env`` and header values a KAS projection withholds from an MCP server entry,
and which variable references it relays instead.

Split out of :mod:`kiro_crew.acp.kas_agents`, which re-exports every name here, so
``kas_agents`` stays the import path callers use.
"""

from __future__ import annotations

import re
from typing import Any

from kiro_crew.acp.kas_env_literals import _loggable_key_names, _relayable_literal

#: The ONLY env key that survives for one of Crew's own managed servers. It pins
#: the data home, so dropping it would have the shims read a different one than
#: the gateway — that is the whole reason managed ``env`` is not simply withheld.
#:
#: Everything else is withheld even on a managed server. "Crew authored this
#: server" is not "Crew authored every key now in its env": the entry lives in a
#: user-editable agent file, so a hand-added secret is reachable under a managed
#: name and would otherwise be the one credential path left onto the wire.
_MANAGED_ENV_KEYS_KEPT = frozenset({"KIROCREW_HOME"})


#: An ``env`` value that is exactly one variable reference, ``${NAME}``. KAS
#: starts a stdio server with a fixed minimal environment (``HOME``, ``PATH``,
#: ``SHELL`` and a few more) plus the entry's own ``env``, and expands
#: ``${NAME}`` there from its own environment, so a reference is the only way a
#: launcher's per-process value reaches an MCP child, and the wire carries the
#: name, never the value. Only names the edition lists through
#: ``McpToolingProvider.kas_relayed_env_references`` are relayed: the KAS
#: environment holds credentials of its own, the CLI's ``KIRO_API_KEY`` among
#: them. The public default lists none.
_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _withhold_credential_fields(
    projected: dict[str, Any],
    *,
    managed: bool,
    relayed: frozenset[str] = frozenset(),
    dropped_env: list[str] | None = None,
) -> list[str]:
    """Strip credential-bearing fields from one projected server entry in place.

    *dropped_env*, when given, is extended with the ``env`` key names withheld.

    Returns the FIELD NAMES something was withheld from, for the caller's log —
    never a value. For a server Crew did not author the ``env`` entry also names
    the withheld keys (:func:`_loggable_key_names`), so a missing setting can be
    found; a key name that is not a plain word the redactor leaves alone is
    counted, never printed.

    A value that is exactly ``${NAME}`` with *NAME* in *relayed* survives on
    every server (:data:`_ENV_REFERENCE`); *managed* also keeps ``_MANAGED_ENV_KEYS_KEPT``, but only as a
    literal: KAS expands ``${...}`` wherever it appears in a value, so a kept
    key holding ``${KIRO_API_KEY}`` would carry the credential under a managed
    name. A server Crew did not author also keeps every literal
    :func:`_relayable_literal` passes (a boolean flag), so such a flag reaches
    it as it does on kiro-cli, which reads the spec off disk.
    Everything else goes either way, ``headers`` included. A managed server has
    no legitimate ``headers`` (all four are local stdio processes), so retaining
    it would only forward whatever a hand edit put there. ``env`` is reported
    withheld when any key was dropped, so a server that kept its references but
    lost a literal is still named in the caller's log.
    """
    withheld: list[str] = []
    if projected.get("headers"):
        projected.pop("headers", None)
        withheld.append("headers")

    env = projected.get("env")
    if not env:
        projected.pop("env", None)
        return withheld
    if not isinstance(env, dict):
        # A malformed env cannot be filtered key-by-key.
        projected.pop("env", None)
        withheld.append("env")
        return withheld

    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for k, v in env.items():
        if not isinstance(k, str):
            continue
        if managed and k in _MANAGED_ENV_KEYS_KEPT:
            keep = isinstance(v, str) and "${" not in v
        else:
            keep = _is_relayed_reference(v, relayed) or (not managed and _relayable_literal(k, v))
        if keep:
            kept[k] = v
        else:
            dropped.append(k)
    if dropped_env is not None:
        dropped_env.extend(dropped)
    if len(kept) != len(env):
        # A third-party server's withheld keys are named so the operator can see
        # which setting went missing; a name that is not itself a plain word the
        # redactor leaves alone is counted instead. A managed server's are not:
        # any extra key on Crew's own entry is a hand edit.
        withheld.append(
            f"env ({_loggable_key_names(dropped)})" if dropped and not managed else "env"
        )
    if kept:
        projected["env"] = kept
    else:
        projected.pop("env", None)
    return withheld


def _is_relayed_reference(value: Any, relayed: frozenset[str]) -> bool:
    """Whether *value* is exactly ``${NAME}`` for a *NAME* in *relayed*."""
    if not relayed or not isinstance(value, str):
        return False
    match = _ENV_REFERENCE.fullmatch(value)
    return match is not None and match.group(1) in relayed
