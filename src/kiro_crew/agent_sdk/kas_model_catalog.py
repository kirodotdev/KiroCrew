"""The KAS engine's model catalog, read off the ``model`` select it advertises.

Measured on the bundled engine (KAS 0.66.117, run from Kiro Crew's own install
with no kiro-cli on the host; the frame is
``test/fixtures/acp_frames/kas/bundle-session-live.jsonl``): neither the
``session/new`` response nor the ``config_option_update`` that follows it carries
a ``models`` object, and from 0.66.117 the ``model`` select comes only in that
update, not in the response. The model list is the ``configOptions``
``model`` select alone, one option per served model --
``{value, name, description, _meta.kiro.{rateMultiplier, rateUnit, hasEffort,
...}}`` -- and no option carries a structured context-window field.

On kiro-cli's relay route the dashboard picker reads ``kiro-cli chat
--list-models`` (``dashboard/handlers/agents.py``), a catalog kiro-cli prints
from its account API. On the bundle route there is no kiro-cli to ask, so the
select IS the catalog, and so it is on a relay started on Crew's sign-in alone
once kiro-cli's gate refuses. This module turns the select into rows shaped like that
``--list-models`` catalog (``model_id`` / ``model_name`` / ``display_name`` /
``description`` / ``rate_multiplier``), so the readers that already consume
catalog rows -- ``model_registry.refresh_kiro_catalog`` for the ``acp``
vocabulary bucket and the credit multipliers, the picker for its rows --
consume ONE shape whichever route fed them. The select never becomes a
session's advertised (entitlement) list; see model-selection.md, "KAS sends no
``models`` object". It is a catalog, read the way the kiro-cli catalog is.

The last select a KAS session advertised is kept in memory, process-wide, for
``GET /api/models`` on those routes (``dashboard/handlers/models_kas_bundle.py``).
In memory only, like the catalog's prices: what the engine serves is a fact
about the account signed in NOW, so a cold process has no catalog until a
session advertises one. A select is what ONE account is served, so the
recording carries the vault identity fingerprint its request went out under
(:func:`vault_identity`, read by the KAS harness as the session payload is built),
and the reader serves it only while the vault still names that account
(:func:`recorded_catalog_snapshot`). The fingerprint is read from the vault
file itself, so the fence holds whichever process changed the account -- the
dashboard's sign-in card, or ``kirocrew kas login`` / ``kas logout`` in a shell
-- and needs no notice from the writer.
Recorded by the shared-runtime driver (``AcpSessionHandle.store_session_config``;
KAS runs on ``AcpRuntime`` only, ``ACP_BACKENDS_ACP_RUNTIME``) on a positive KAS
identity, so a KAS session on either route records; the picker READS it on the
bundle route, and on the relay only where that relay starts on Crew's sign-in
alone and kiro-cli's gate refuses.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from kiro_crew import model_registry
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

logger = logging.getLogger(__name__)

#: Longest string kept on a catalog row besides its id -- the same bound the
#: advertised-id store applies, so a row cannot carry more than the id store would
#: admit for its name.
_ROW_TEXT_MAX_CHARS = model_registry.ADVERTISED_MODEL_ID_MAX_CHARS

#: The rows of the last non-empty select a KAS session advertised. Guarded by
#: ``_LOCK``: the recorders run on the event loop, the reader may run on a worker
#: thread, and a torn read would serve half a catalog.
_recorded: list[dict[str, Any]] | None = None
#: The vault identity fingerprint (``auth.bridge.vault_identity_fingerprint``)
#: read as the recording's request went out: WHICH account the rows are. Under
#: ``_LOCK`` with the rows, as one state.
_recorded_identity = ""
_LOCK = threading.Lock()


def _clamp(value: object) -> str:
    text = value if isinstance(value, str) else ""
    return text[:_ROW_TEXT_MAX_CHARS]


def _multiplier(option: dict[str, Any]) -> float | None:
    """``_meta.kiro.rateMultiplier`` as a number, or ``None`` when absent / not one.

    ``bool`` is refused (``True`` would read as a 1.0x price). The picker and
    ``model_registry._catalog_rates`` apply their own positive-finite test; this
    only decides whether the row carries the field at all.
    """
    meta = option.get("_meta")
    kiro = meta.get("kiro") if isinstance(meta, dict) else None
    rate = kiro.get("rateMultiplier") if isinstance(kiro, dict) else None
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        return None
    return float(rate)


def catalog_rows_from_session(resp: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``model`` select of a session response as ``--list-models``-shaped rows.

    ``[]`` when the response advertises no such select or an empty one. Row order
    is the select's order (the engine lists its routing entry first). A row whose
    ``value`` is over the id bound is refused, never truncated, the same rule
    :func:`model_registry.admit_catalog_rows` applies, and the list is cut at
    ``ADVERTISED_MODELS_MAX_IDS`` rows so what is retained is never more than the
    id store will admit; both refusals are counted and logged once per call. ``rate_multiplier`` is
    present only on a row whose option carried a numeric ``rateMultiplier``; a
    reader that finds it absent knows the price, not just the number, is unknown.
    No ``context_window_tokens`` is ever synthesized: the select carries none.
    """
    # Lazy: acp.session_handle imports model_registry-adjacent modules at import
    # time, and this module is imported by the session drivers themselves.
    from kiro_crew.acp.session_handle_model_selection import _select_options

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Refused, and said so ONCE per call like ``model_registry._admit_ids``: an
    # option cut for the count or the id length would otherwise look exactly like
    # a model the engine never served, in the picker, the vocabulary and the
    # prices alike. A blank or non-string ``value`` and a duplicate are malformed
    # or redundant, not refused, and are skipped silently as there.
    refused = 0
    for opt in resp.get("configOptions") or []:
        if not isinstance(opt, dict) or opt.get("id") != "model" or opt.get("type") != "select":
            continue
        for option in _select_options(opt.get("options")):
            value = option.get("value")
            if not isinstance(value, str) or not value.strip() or value in seen:
                continue
            if (
                len(value) > model_registry.ADVERTISED_MODEL_ID_MAX_CHARS
                or len(rows) >= model_registry.ADVERTISED_MODELS_MAX_IDS
            ):
                refused += 1
                continue
            seen.add(value)
            row: dict[str, Any] = {
                "model_id": value,
                "model_name": value,
                "display_name": _clamp(option.get("name")) or value,
                "description": _clamp(option.get("description")),
            }
            rate = _multiplier(option)
            if rate is not None:
                row["rate_multiplier"] = rate
            rows.append(row)
        break
    if refused:
        logger.warning(
            "KAS model select refused %d option(s): over %d ids or over %d chars",
            refused,
            model_registry.ADVERTISED_MODELS_MAX_IDS,
            model_registry.ADVERTISED_MODEL_ID_MAX_CHARS,
        )
    return rows


#: :func:`vault_identity`'s default: the engine credential a NEW spawn would get.
_CONFIGURED: Any = object()


def vault_identity(engine_auth: object = _CONFIGURED) -> str:
    """The vault identity fingerprint now; ``""`` when empty or unreadable.

    Blocking (reads the vault); call off the loop. Never raises. The one read
    both the KAS harness's stamp (``acp/harness/kas.py``, ``SessionExtras.vault_identity``)
    and the picker's match go through. On a workload-identity host it is the
    workload's own key, and the vault is not read.

    *engine_auth* is the credential a running engine was started with, which the
    harness's stamp passes (``None`` for Crew's own sign-in), so a retained engine
    keeps recording under the credential it still reads after the configured one
    changes. The picker leaves it out and matches the configured credential.
    """
    from kiro_crew import workload_identity

    # An edition engine credential keys the rows by its own identity: the vault is empty
    # there, and the engine lists what that credential is served. First, as the spawn
    # gives it precedence: on a workload host too the engine runs on that credential.
    from kiro_crew.agent_sdk.kas_engine_auth import active_engine_auth

    auth = active_engine_auth() if engine_auth is _CONFIGURED else engine_auth
    engine_key = getattr(auth, "catalog_identity", "")
    if isinstance(engine_key, str) and engine_key:
        return engine_key
    if workload_identity.active():
        return workload_identity.catalog_identity()
    # Deferred: kiro_crew.auth pulls the ``cryptography`` wheel, which must not
    # load on the session drivers' import path (see kiro_crew.acp.kas_host_auth).
    from kiro_crew.auth.bridge import vault_identity_fingerprint

    return vault_identity_fingerprint()


def record_catalog(resp: dict[str, Any], backend: str, *, identity: object) -> bool:
    """Remember the select a KAS session response advertised; ``True`` when recorded.

    Positive on KAS identity: the fact recorded here -- that the ``model`` select
    is the engine's catalog -- is a KAS wire fact, and no other harness's select
    is read as one. Another backend records nothing and nothing is cleared: a
    response that advertises no select (a ``session/load`` on some engines) must
    not wipe a catalog a ``session/new`` recorded a moment ago, and neither must
    one whose select names only the session's current model (a KAS
    ``session/load`` from kiro-cli 3.4; ``select_lists_only_its_current_model``).

    *identity* is the vault fingerprint read as the producing session's request
    went out (``AcpSessionHandle.request_vault_identity``), kept with the rows.
    Anything but a non-empty ``str`` -- an unstamped request, an empty vault --
    is refused, so an unknown origin can never pass as the signed-in account. A
    response that lands after an account switch is recorded under the account it
    was asked for, and the reader does not serve it to the next one.
    """
    global _recorded, _recorded_identity
    if backend == ACP_BACKEND_KAS:
        rows = catalog_rows_from_session(resp)
        if rows:
            # Lazy for the same import-order reason as ``catalog_rows_from_session``.
            from kiro_crew.acp.session_handle_model_selection import (
                model_select_envelope,
                select_lists_only_its_current_model,
            )

            if select_lists_only_its_current_model(model_select_envelope(resp) or {}):
                logger.debug(
                    "KAS model catalog not recorded: the select names only the session's model"
                )
                return False
            if not (isinstance(identity, str) and identity):
                logger.debug(
                    "KAS model catalog not recorded: the request carried no vault identity"
                )
                return False
            with _LOCK:
                _recorded = rows
                _recorded_identity = identity
            logger.debug(
                "KAS model catalog recorded from the session's model select (%d rows)", len(rows)
            )
            return True
    return False


def recorded_catalog_snapshot(identity: str) -> list[dict[str, Any]] | None:
    """A copy of the last recorded select's rows, if they belong to *identity*.

    *identity* is the vault fingerprint the caller just read. The rows are
    ``None`` unless they were recorded for exactly that account: when the vault
    names another one, the recording is the previous account's and cold is the
    honest answer. ``""`` (an empty or unreadable vault) never matches. Nothing
    is forgotten on a mismatch: if the vault comes back to that account, its rows
    serve again. A reader that awaits between taking this copy and serving it
    reads the vault again afterwards and discards the copy if the account moved.
    """
    with _LOCK:
        if _recorded is None or not identity or identity != _recorded_identity:
            return None
        return [dict(row) for row in _recorded]
