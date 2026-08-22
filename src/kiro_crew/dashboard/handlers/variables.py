"""``GET``/``PUT /api/variables`` — read the variable cascade, or patch one scope.

``GET`` reports what this endpoint can EDIT — the pairs the variables store holds
for each scope — alongside the resolved map and where each winning value came from.

``PUT`` applies a PER-KEY patch: ``set`` names pairs to write, ``delete`` names keys
to remove. It deliberately does not replace a whole scope. The replace form drew a
data-loss finding in three consecutive review rounds, all from one root: the client
had to echo back a map it had read, so two clients replacing the same scope clobbered
each other's unrelated edits and a value the client could not see got dropped.
Touching only the named keys removes that: a key nobody named is never read, never
rewritten, and cannot be lost.

Deleting is therefore an explicit verb rather than "absence from the map", which also
keeps the empty string unambiguous — it is a legal value that still overrides a
broader scope, so it cannot share an encoding with "unset".

Validation refuses rather than drops. The loader deliberately drops a bad pair with a
warning so one hand-edited mistake cannot cost the rest of a scope or fail a load,
but a dashboard write is interactive: silently discarding a pair the user just typed
would look like a save that worked.

WHAT THIS ENDPOINT NO LONGER HAS TO DO
======================================

Variables used to live in ``config.json``, which has a ``config.local.json`` overlay
and a whole-file writer (``KiroCrewConfig.save()``). That placement, not this
endpoint, generated most of this feature's review history, and moving the data to its
own store deleted four mechanisms outright:

* the ``variables_overlay_owned`` refusal — the store has no overlay layer, so there
  is no class of key this endpoint can read but not write;
* the deleted-workspace resurrection guard — a store entry naming a workspace the
  config does not define is inert (the loader skips it) instead of materializing one;
* the malformed-workspace-entry and merged-vs-base distinction — the store holds only
  variables, so there is no neighbouring config shape to misread;
* ``save()`` interaction entirely — nothing but this endpoint writes the store.

See ``config/variables_store.py`` for why the three in-config alternatives were each
wrong. Malformed-container refusal survives the move and lives in the store, because
"do not replace a value whose shape you cannot interpret" is a property of the data,
not of where it is kept.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.config import variables_store as vstore
from kiro_crew.config.loader import ConfigReadError, KiroCrewConfig, resolve_variables
from kiro_crew.dashboard.chat_utils import run_config_write
from kiro_crew.sel import sel
from kiro_crew.variables import validate_pair

logger = logging.getLogger(__name__)

SCOPE_GLOBAL = vstore.SCOPE_GLOBAL
SCOPE_WORKSPACE = vstore.SCOPE_WORKSPACE
_WRITABLE_SCOPES = (SCOPE_GLOBAL, SCOPE_WORKSPACE)


def _view_inputs() -> tuple[KiroCrewConfig, dict]:
    """The merged config and the raw store document.

    Both are needed because they answer different questions: the config says what a
    session RESOLVES and which workspaces and crews exist, the store says what this
    endpoint can EDIT. Blocking; call from a thread.
    """
    return KiroCrewConfig.load(), vstore.read_store()


def _view_blocking() -> dict:
    """The rendered view, inputs included -- the whole blocking span in one thread hop.

    ``_view`` is not merely a projection of ``_view_inputs``: it calls
    ``resolve_variables``, whose per-scope ``_store_layer`` reads the store on a cache
    miss. Offloading only the inputs left that read on the event loop, reachable
    whenever a concurrent PUT invalidates the cache between the two calls -- which on
    THIS endpoint is the ordinary traffic pattern, not a rare interleaving.
    """
    return _view(*_view_inputs())


def _view(cfg: KiroCrewConfig, doc: dict) -> dict:
    """What this endpoint can edit, plus the resolved map for the active context.

    The per-scope maps come from the STORE, not from the resolved cascade. Reporting
    resolved values as editable is what let the panel show a pair it did not own.

    Workspace and crew maps are keyed off the CONFIG's names, not the store's, so a
    stale store entry for a deleted workspace is not advertised as editable. Those
    names are also what the loader resolves against, so the two agree.
    """
    resolution = resolve_variables(cfg)
    ws_stored = vstore.scoped_values(vstore.SCOPE_WORKSPACE, doc)
    crew_stored = vstore.scoped_values(vstore.SCOPE_CREW, doc)
    return {
        "global": vstore.global_values(doc),
        "workspaces": {name: dict(ws_stored.get(name, {})) for name in cfg.workspaces},
        "crews": {name: dict(crew_stored.get(name, {})) for name in cfg.agents},
        "effective": dict(resolution.values),
        "winning_scope": dict(resolution.winning_scope),
        "shadowed": {key: list(scopes) for key, scopes in resolution.shadowed.items()},
        "active_workspace": resolution.workspace_name,
        "active_agent": resolution.agent_name,
    }


async def api_variables(request: web.Request) -> web.Response:
    """GET/PUT /api/variables — read the cascade, or patch one scope."""
    if request.method != "PUT":
        return web.json_response(await asyncio.to_thread(_view_blocking))

    caller = request.get("user", "dashboard")

    def _deny(code: str, error: str) -> web.Response:
        """Refuse a malformed request.

        The status is a literal rather than a parameter: the error-code contract gate
        counts a computed ``status=`` separately precisely because hoisting it into a
        variable would defeat the static check, and every refusal here is a 400.
        """
        sel().log_api_access(
            caller=caller,
            operation="variables.update",
            outcome="denied",
            error=error,
        )
        return web.json_response({"error": error, "code": code}, status=400)

    try:
        body = await request.json()
    except Exception:
        return _deny("variables_invalid_json", "invalid JSON")
    if not isinstance(body, dict):
        return _deny("variables_invalid_body", "body must be an object")

    scope = body.get("scope")
    if scope not in _WRITABLE_SCOPES:
        return _deny(
            "variables_invalid_scope",
            f"scope must be one of {', '.join(_WRITABLE_SCOPES)}",
        )

    raw_set = body.get("set")
    raw_delete = body.get("delete")
    if raw_set is None and raw_delete is None:
        return _deny(
            "variables_invalid_values",
            "body must carry 'set' (object) and/or 'delete' (array of names)",
        )
    if raw_set is not None and not isinstance(raw_set, dict):
        return _deny("variables_invalid_values", "set must be an object")
    if raw_delete is not None and not isinstance(raw_delete, list):
        return _deny("variables_invalid_values", "delete must be an array of names")

    values: dict[str, str] = {}
    for key, value in (raw_set or {}).items():
        name, outcome = validate_pair(key, value)
        if name is None:
            sel().log_api_access(
                caller=caller,
                operation="variables.update",
                outcome="denied",
                error=f"invalid variable: {outcome}",
            )
            return web.json_response(
                {"error": outcome, "code": "variables_invalid_pair", "key": str(key)},
                status=400,
            )
        values[name] = outcome

    removals: list[str] = []
    for key in raw_delete or []:
        # A delete names a key rather than carrying a value, so it is validated by the
        # same grammar with a throwaway value: an unparseable name could not have been
        # stored by this endpoint in the first place.
        name, outcome = validate_pair(key, "")
        if name is None:
            # Through the audited helper, like every sibling refusal: the set-side
            # rejection above logs one, so a delete that returned 400 with no SEL
            # entry would leave a refusal invisible in the audit trail.
            sel().log_api_access(
                caller=caller,
                operation="variables.update",
                outcome="denied",
                error=f"invalid variable name in delete: {outcome}",
            )
            return web.json_response(
                {"error": outcome, "code": "variables_invalid_pair", "key": str(key)},
                status=400,
            )
        removals.append(name)

    overlapping = sorted(set(values) & set(removals))
    if overlapping:
        return _deny(
            "variables_conflicting_change",
            f"a key cannot be set and deleted in one request: {', '.join(overlapping)}",
        )

    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    workspace = body.get("workspace") or ""
    if scope == SCOPE_WORKSPACE:
        if not isinstance(workspace, str) or workspace not in cfg.workspaces:
            # Refused for the caller's sake rather than for data integrity: a store
            # entry for an unknown workspace is inert (the loader skips names the
            # config does not define), so accepting it would report a save that can
            # never take effect.
            return _deny(
                "variables_unknown_workspace",
                f"unknown workspace: {workspace!r}",
            )

    # Routed through run_config_write, which holds BOTH config locks: the loop-side
    # asyncio lock and, inside the worker thread, the store's own advisory flock via
    # update_config_locked. The blocking wait therefore never happens on the event
    # loop — the defect that made the in-config version of this write untenable.
    try:
        await run_config_write(
            vstore.patch_store,
            scope=scope,
            name=workspace,
            values=values,
            removals=removals,
        )
    except vstore.MalformedStore as exc:
        return _deny(
            "variables_malformed_container",
            f"{exc.path} in the variables store is not an object; refusing to "
            "replace it. Repair or remove that value by hand, then retry.",
        )
    except ConfigReadError:
        sel().log_api_access(
            caller=caller,
            operation="variables.update",
            outcome="error",
            error="the variables store is corrupt",
        )
        return web.json_response(
            {"error": "the variables store is corrupt", "code": "config_corrupt"},
            status=500,
        )

    sel().log_api_access(
        caller=caller,
        operation="variables.update",
        outcome="ok",
        resources=f"{scope}:{workspace}" if scope == SCOPE_WORKSPACE else scope,
    )
    return web.json_response({"ok": True, **(await asyncio.to_thread(_view_blocking))})
