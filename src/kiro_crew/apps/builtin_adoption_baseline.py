"""What a deleted wheel copy gave E9 and the limits only a wheel copy supplies.

While a pinned app's wheel copy ships, ``register_builtin_apps`` rewrites the
builtin-owned record from it at every start: the record's ``app.json`` copy, and an
approved set of everything that copy declares (``manager.builtin_approved_grants``).
E9 (``builtin_adoption_checks.permission_refusal``) and the swap (``update_app``
under an adoption) compare the pinned tree with that record. A release that deletes
the copy ends the rewrite, so a host that never adopted keeps whatever its last
booted release wrote, which can be an older, narrower copy. The release that deletes
a copy therefore freezes the copy's E9 input into its pin row
(``builtin_adoption_pins.DELETED_COPY_KEY``, printed by
``scripts/adoption_pin.py freeze``), and with no wheel copy both comparisons read
that instead of the record: the host adopts exactly as one that booted the last
release with the copy and then upgraded.

Some core limits read their input from the wheel copy alone (app-kit-platform.md § 33,
rows 1 to 4 and 9): the stdio server ceiling and the own-server grant (the copy's
``mcpServers`` and its agents' names), the ``agentTools`` ceiling and the
``app-own-tools`` gate row (its ``agentTools``), and the built-in leg of the credential
masks and agent read-only directories. The same freeze carries each of those inputs
(:data:`builtin_adoption_pins.DELETED_COPY_LIMIT_KEYS`), and with no wheel copy each
reader reads it from the row (``builtin_adoption_pins.frozen_limit_input``), so every
limit answers as the last shipped copy made it answer, and nothing past it.

Owning spec: ``docs/system-specs/modules/app-kit-platform.md`` § Former built-in
adoption, "A pin row with no wheel copy".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtin_adoption_pins import AdoptionPin, frozen_limit_input

#: The frozen input of the gate's agent-to-app map: the names the copy's agents declare.
AGENT_NAMES_KEY = "agentNames"


@dataclass(frozen=True)
class GrantBaseline:
    """The replaced side of E9 and of the adoption swap, in place of the record's copy.

    ``permissions`` is the parsed ``permissions`` the direct checks read
    (``sessionApproval``, ``memberThreads``, ``chatSlots``, ``chatLaunch``);
    ``grants`` is ``point_grants.grant_source`` of the same copy; ``approved`` is the
    approved set the held-back check starts from. On the record path each is what the
    record holds, None where it holds nothing (no readable copy, no stored set).
    """

    permissions: Any
    grants: dict[str, list[str]] | None
    approved: dict[str, list[str]] | None


def freeze_grant_inputs(app_json: dict[str, Any]) -> dict[str, Any]:
    """E9's half of a ``deletedCopy`` object, from a wheel copy's raw ``app.json``.

    Derived through the readers E9 and ``register_builtin_apps`` run, never by naming
    keys: the copy's own ``permissions`` object verbatim, and every grant family
    ``grant_source`` reads from the copy that declares an entry, so a family
    registered later is carried with no change here. A family left out declared
    nothing, which is how :func:`deleted_copy_baseline` reads it.
    """
    from kiro_crew.apps.point_grants import grant_source

    permissions = app_json.get("permissions")
    grants = grant_source(app_json) or {}
    return {
        "permissions": permissions if isinstance(permissions, dict) else {},
        "grants": {family: list(entries) for family, entries in grants.items() if entries},
    }


def freeze_deleted_copy(copy_root: Path) -> dict[str, Any]:
    """The ``deletedCopy`` object of the wheel copy whose package directory is *copy_root*.

    :func:`freeze_grant_inputs` of its ``app.json``, and the input of each limit only a
    wheel copy supplies, read by that limit's own reader and carried only when the copy
    declares one, so a copy that supplied none adds nothing to the row:

    - ``mcpServers``: the parsed manifest's servers, the ceiling of E10 and of the
      own-server grant (``builtin_adoption_mcp.carried_server``), with
      :data:`AGENT_NAMES_KEY`, the names its agent files declare as the gate's
      agent-to-app map reads them (``execution.declared_agent_names``). The map names an
      adopted app's agents only beside a server it keeps
      (``builtin_adoption_gate._publish``), so a copy with no server freezes no names;
    - ``agentTools``: the parsed block, the ceiling and deny universe of
      ``app_agent_tools``;
    - ``credentialMasks`` and ``agentReadOnlyDirs``: the built-in leg's entries for the
      package (``credential_masks.builtin_package_entries``, which takes the app's name
      from the directory, as that leg does).

    *copy_root* is made absolute once, here, so every reader reads the same directory
    whatever the working directory; only the agent reader then resolves it, as
    ``builtin_app_agents`` resolves a shipped root before its containment check.
    """
    from kiro_crew.apps import agent_readonly_dirs, credential_masks
    from kiro_crew.apps.execution import declared_agent_names
    from kiro_crew.apps.manifest import AppManifest
    from kiro_crew.user_json import loads_user_json

    package = copy_root.absolute()
    root = package.resolve()
    raw = loads_user_json((package / "app.json").read_text(encoding="utf-8"))
    manifest = AppManifest.from_dict(raw)
    frozen = freeze_grant_inputs(raw)
    servers = dict(manifest.mcpServers)
    inputs: dict[str, Any] = {
        "mcpServers": servers,
        AGENT_NAMES_KEY: declared_agent_names(root, raw) if servers else [],
        "agentTools": manifest.agentTools.to_dict(),
    }
    for kind in (credential_masks.CREDENTIAL_MASKS, agent_readonly_dirs.KIND):
        inputs[kind.key] = list(credential_masks.builtin_package_entries(package, kind))
    frozen.update({key: value for key, value in inputs.items() if value})
    return frozen


def deleted_copy_baseline(document: dict[str, Any]) -> GrantBaseline:
    """The baseline a ``deletedCopy`` object stands for: the record its copy registered.

    ``permissions`` parses as a record copy's does, and ``approved`` is what
    ``register_builtin_apps`` stores from that copy, so the adoption ends with the
    grants the wheel-rewrite path would have carried, no more.
    """
    from kiro_crew.apps.manager import builtin_approved_grants
    from kiro_crew.apps.manifest import Permissions

    raw_permissions = document.get("permissions")
    grants = {family: list(entries) for family, entries in document.get("grants", {}).items()}
    return GrantBaseline(
        permissions=(
            Permissions.from_dict(raw_permissions)
            if isinstance(raw_permissions, dict)
            else Permissions()
        ),
        grants=grants,
        approved=builtin_approved_grants(grants),
    )


def wheel_copy_ships(name: str) -> bool:
    """Whether this wheel still ships a built-in copy named *name*."""
    from kiro_crew.apps.execution import _shipped_builtin_apps

    return any(shipped == name for _root, shipped in _shipped_builtin_apps())


def no_copy_limit_input(pin: AdoptionPin | None, key: str) -> Any:
    """``builtin_adoption_pins.frozen_limit_input`` once *pin*'s wheel copy is gone.

    None while the copy ships: its reader reads the copy then, and a frozen value
    beside it is never read. Blocking only for a row that carries one: it scans the wheel.
    """
    if pin is None or not pin.deleted_copy or wheel_copy_ships(pin.name):
        return None
    return frozen_limit_input(pin, key)


def no_copy_baseline(pin: AdoptionPin) -> GrantBaseline | None:
    """*pin*'s frozen copy once the wheel copy is gone; None while the copy ships.

    While the copy ships, the record ``register_builtin_apps`` rewrites from it is
    the baseline and a ``deletedCopy`` on the row is never read. A row with neither
    vouches for nothing: its baseline is empty, so any grant the pinned tree
    declares is held back (``permission_widening``). Blocking: it scans the wheel.
    """
    if wheel_copy_ships(pin.name):
        return None
    return deleted_copy_baseline(json.loads(pin.deleted_copy) if pin.deleted_copy else {})
