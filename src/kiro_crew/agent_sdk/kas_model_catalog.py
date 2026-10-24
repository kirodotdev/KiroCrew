"""The KAS engine's model catalog, read off the ``model`` select it advertises.

Measured on the bundled engine (KAS 0.66.33, run from Kiro Crew's own install
with no kiro-cli on the host; the frame is
``test/fixtures/acp_frames/kas/bundle-session-live.jsonl``): a ``session/new``
response carries NO ``models`` object. The model list is the ``configOptions``
``model`` select alone, one option per served model --
``{value, name, description, _meta.kiro.{rateMultiplier, rateUnit, hasEffort,
...}}`` -- and no option carries a structured context-window field.

On kiro-cli's relay route the dashboard picker reads ``kiro-cli chat
--list-models`` (``dashboard/handlers/agents.py``), a catalog kiro-cli prints
from its account API. On the bundle route there is no kiro-cli to ask, so the
select IS the catalog. This module turns the select into rows shaped like that
``--list-models`` catalog (``model_id`` / ``model_name`` / ``display_name`` /
``description`` / ``rate_multiplier``), so the readers that already consume
catalog rows -- ``model_registry.refresh_kiro_catalog`` for the ``acp``
vocabulary bucket and the credit multipliers, the picker for its rows --
consume ONE shape whichever route fed them. The select never becomes a
session's advertised (entitlement) list; see model-selection.md, "KAS sends no
``models`` object". It is a catalog, read the way the kiro-cli catalog is.

The last select a KAS session advertised is kept in memory, process-wide, for
``GET /api/models`` on the bundle route (``dashboard/handlers/models_kas_bundle.py``).
In memory only, like the catalog's prices: what the engine serves is a fact
about the account signed in NOW, so a cold process has no catalog until a
session advertises one, and a Crew sign-out or sign-in forgets it
(:func:`forget_catalog`, the sign-in service's identity-change hook) until the
next session.
Recorded by the shared-runtime driver (``AcpSessionHandle.store_session_config``;
KAS runs on ``AcpRuntime`` only, ``ACP_BACKENDS_ACP_RUNTIME``) on a positive KAS
identity, so a KAS session on either route records; only the bundle-route picker
READS it.
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
#: The Crew sign-in generation: bumped by :func:`forget_catalog` on every sign-out
#: and authorized sign-in. A runtime notes the generation it LAUNCHED under
#: (``AcpRuntime.sign_in_generation``), and a recording is accepted only while
#: that generation is still current -- so a session that opened under the
#: previous account and answers after the switch cannot restore that account's
#: catalog. Under ``_LOCK`` with the rows, as one state.
_generation = 0
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


def sign_in_generation() -> int:
    """The current Crew sign-in generation, for a runtime to note at launch."""
    with _LOCK:
        return _generation


def record_catalog(resp: dict[str, Any], backend: str, *, generation: object) -> bool:
    """Remember the select a KAS session response advertised; ``True`` when recorded.

    Positive on KAS identity: the fact recorded here -- that the ``model`` select
    is the engine's catalog -- is a KAS wire fact, and no other harness's select
    is read as one. Another backend records nothing and nothing is cleared: a
    response that advertises no select (a ``session/load`` on some engines) must
    not wipe a catalog a ``session/new`` recorded a moment ago.

    *generation* is the sign-in generation the producing runtime launched under
    (``AcpRuntime.sign_in_generation``). A recording is refused unless it equals
    the current one: the engine answered with the catalog of the account whose
    credential it was launched with, and a sign-out or sign-in since then makes
    that catalog the previous account's. Anything that is not the current
    generation's ``int`` -- an older one, or an unstamped runtime -- is refused,
    so an unknown origin can never pass as the signed-in account.
    """
    global _recorded
    if backend == ACP_BACKEND_KAS:
        rows = catalog_rows_from_session(resp)
        if rows:
            with _LOCK:
                if not (type(generation) is int and generation == _generation):
                    logger.debug(
                        "KAS model catalog not recorded: the session's runtime launched under "
                        "sign-in generation %r, the current one is %d",
                        generation,
                        _generation,
                    )
                    return False
                _recorded = rows
            logger.debug(
                "KAS model catalog recorded from the session's model select (%d rows)", len(rows)
            )
            return True
    return False


def forget_catalog() -> None:
    """Drop the recording: the account it was served for is not the one signed in.

    Handed to the sign-in service as its identity-change hook
    (``dashboard/handlers/kas_login.py`` -> ``KasLoginService(on_identity_change=)``),
    which runs it under its own lock right after a credential is saved and right
    after a sign-out's deletes -- the vault's only writes, with no await between
    the write and this call. A select is what ONE account is served, so a
    recording outliving the account would offer the previous user's models and
    prices to the next one until that account's first session replaced it; cold
    (``auto`` alone) is the honest answer in between. Bumping the generation is what also refuses a
    recording from a session that opened under the previous account and
    answers after the switch (see :func:`record_catalog`).
    """
    global _recorded, _generation
    with _LOCK:
        _recorded = None
        _generation += 1


def recorded_catalog() -> list[dict[str, Any]] | None:
    """A copy of the last recorded select's rows, or ``None`` when none was recorded."""
    with _LOCK:
        if _recorded is None:
            return None
        return [dict(row) for row in _recorded]
