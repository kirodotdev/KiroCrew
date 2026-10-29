"""``GET /api/models`` on the KAS bundle route: the picker with no kiro-cli.

``agents.api_models`` serves the kiro family from ``kiro-cli chat --list-models``
and gates that spawn on kiro-cli readiness. With ``agent.acp_backend == "kas"``
and ``agent.kas_bundle`` on, KAS runs Crew's own installed engine and the host
may have no kiro-cli at all, so that path answered a permanent degraded 503 and
the picker stayed on ``auto`` with no context windows and no credit multipliers.
This module is the bundle route's answer, dispatched BEFORE the kiro-cli path on
``agent_sdk.kas_bundle.kas_bundle_route`` so that path is not changed and never
spawns here:

* **Readiness is the bundle's**: the installed engine
  (``kas_bundle.installed_bundle``) and Crew's own sign-in vault
  (``auth.bridge.vault_holds_identity``, the same question the KAS spawn asks),
  never kiro-cli's latch. Either missing is a coded 503 and nothing is spawned.
* **The list is the engine's ``model`` select**, recorded by the runtime
  driver into :mod:`kiro_crew.agent_sdk.kas_model_catalog` when a KAS session
  starts -- measured, that select is the only place the bundled engine
  advertises its models (``test/fixtures/acp_frames/kas/bundle-session-live.jsonl``).
  Each row carries the select's ``rateMultiplier`` as ``rate_multiplier``; a
  ``context_window`` is attached only where :func:`model_registry.model_window`
  knows the id (operator declaration, an earlier kiro-cli catalog, the static
  registry), and is OMITTED otherwise, because the select carries none and an
  invented number would scale the context meter on a guess.
* **A cold process serves ``auto`` alone with 200**, not a degraded 503: nothing
  has failed, no session has advertised yet. The dashboard refetches the list
  when a session spawns (``useWebSocket``'s ``spawned`` invalidation), so the
  first chat fills the picker without a poll. Cold again whenever the vault
  names a different account than the one the rows were recorded for: every
  request reads the vault's identity fingerprint and
  ``recorded_catalog_snapshot`` yields the rows only to that account, so a
  sign-in or sign-out from the dashboard or from ``kirocrew kas login`` /
  ``kas logout`` in a shell turns the picker cold until that account's first
  session. The fingerprint is read again after the handler's one await (the
  advertised-store persist), so a switch landing in that await is never served
  the snapshot taken before it.
* **The ``acp`` vocabulary bucket and the credit multipliers are fed from these
  rows** through the one admission the kiro-cli catalog uses
  (``model_registry.refresh_kiro_catalog``), so a stored pin's native/foreign
  verdict and the cost badge work on this route as they do on the relay. No
  window is fed: the rows carry none.
* **No entitlement narrowing.** The kiro-cli path narrows its catalog to what a
  live session advertises because ``--list-models`` prints the same rows for
  every tier. The select is not that: the engine lists what the signed-in
  account is served, so the recording IS the entitled list. The kiro path's
  ``_entitled_kiro_models`` is deliberately not run here -- it reads the newest
  live provider in the shared ``acp`` namespace, which after a hot switch of
  ``agent.acp_backend`` can be a retained kiro-cli session whose own (possibly
  ``auto``-only) list has nothing to say about the bundle account.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew import model_registry
from kiro_crew.agent_sdk import host_auth, kas_model_catalog
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, model_registry_namespace
from kiro_crew.executors import maintenance_executor

logger = logging.getLogger(__name__)

#: ``code`` on the 503 when ``agent.kas_bundle`` is on but no engine is installed.
KAS_BUNDLE_NOT_INSTALLED_CODE = "kas_bundle_not_installed"
#: ``code`` on the 503 when the engine is installed but Crew's vault holds no
#: usable identity: the engine would refuse every session, so the list is unknown.
KAS_BUNDLE_SIGN_IN_REQUIRED_CODE = "kas_bundle_sign_in_required"


def _bundle_installed() -> bool:
    """Blocking (reads the engines directory); call off the loop."""
    from kiro_crew.agent_sdk import kas_bundle

    return kas_bundle.installed_bundle() is not None


def _vault_signed_in() -> bool:
    """Blocking (reads the vault); call off the loop. Never raises."""
    # Deferred: kiro_crew.auth pulls the ``cryptography`` wheel, which must not
    # load on the handlers' import path (see kiro_crew.acp.kas_host_auth).
    from kiro_crew.auth.bridge import vault_holds_identity

    # DEBUG diagnostics: this runs on a poll, where the spawn-time WARNING would
    # repeat on every tick.
    return vault_holds_identity(warn=False)


def _picker_row(row: dict[str, Any]) -> dict[str, Any]:
    """One catalog row as the picker wire row, with only the fields a source backs."""
    model_id = row["model_name"]
    out: dict[str, Any] = {
        "model_name": model_id,
        "display_name": row.get("display_name") or model_id,
        "description": row.get("description", ""),
    }
    if "rate_multiplier" in row:
        out["rate_multiplier"] = row["rate_multiplier"]
    window = model_registry.model_window(model_id)
    if window:
        out["context_window"] = window
    return out


async def api_models_kas_bundle(request: web.Request) -> web.Response:
    """The bundle route's ``GET /api/models``. See the module docstring."""
    if not await asyncio.to_thread(_bundle_installed):
        return web.json_response(
            {
                "error": "KAS engine bundle is not installed; run `kirocrew kas install`.",
                "code": KAS_BUNDLE_NOT_INSTALLED_CODE,
            },
            status=503,
        )
    if not await asyncio.to_thread(_vault_signed_in):
        return web.json_response(
            {
                "error": host_auth.signed_out_message(ACP_BACKEND_KAS),
                "code": KAS_BUNDLE_SIGN_IN_REQUIRED_CODE,
            },
            status=503,
        )
    # Which account the vault names NOW, whichever process wrote it last: the
    # recording is served only to the account it was recorded for.
    identity = await asyncio.to_thread(kas_model_catalog.vault_identity)
    rows = kas_model_catalog.recorded_catalog_snapshot(identity)
    if rows is None:
        return _cold_response()
    # The same admission the kiro-cli catalog goes through, into the same ``acp``
    # bucket (kiro and KAS share it): ids for the pin verdicts, prices for the
    # badge. ``windows_changed`` is dropped on purpose -- the rows carry no
    # ``context_window_tokens``, so the window cache cannot move here.
    _windows_changed, advertised_changed = model_registry.refresh_kiro_catalog(
        rows, model_registry_namespace(ACP_BACKEND_KAS)
    )
    if advertised_changed:
        await asyncio.get_running_loop().run_in_executor(
            maintenance_executor(), model_registry.persist_advertised_models
        )
        # The snapshot above was taken before an await. A sign-out or sign-in
        # that landed during it -- from any process -- made the rows the
        # previous account's, and the dashboard drops its own caches on its own
        # sign-in and sign-out; serving them now would hand it that list to
        # cache again. Cold is the answer the recording gives.
        if await asyncio.to_thread(kas_model_catalog.vault_identity) != identity:
            return _cold_response()
    return web.json_response([_picker_row(row) for row in rows])


def _cold_response() -> web.Response:
    """``auto`` alone with 200: no KAS session has advertised its select for the
    signed-in account yet. ``auto`` is the one id every partition's engine
    resolves, so it is the one safe offering."""
    return web.json_response(
        [
            {
                "model_name": "auto",
                "display_name": "Auto",
                "description": "Backend default",
                "context_window": model_registry.model_window("auto")
                or model_registry.REFERENCE_WINDOW_TOKENS,
            }
        ]
    )
