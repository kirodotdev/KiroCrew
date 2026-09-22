"""``/api/acp-backends`` -- per-backend selectability and machine readiness.

One row per backend id in ``acp_backends.ACP_BACKENDS_KNOWN``, sorted by
``policy_id``, including ids this build cannot serve: the dashboard's backend
switch lists all of them and must be able to say which is which, so an
unservable backend needs a row rather than silent absence.

Four facts per row, from four owners that must not be conflated:

* ``selectable`` -- build capability AND deployment policy, read from
  ``handlers.core._selectable_acp_backends()``. That helper is already the
  single derivation feeding the PATCH allowlist and ``/api/config/schema``, and
  it already applies the ``agent_backend`` governance scope. Re-deriving it here
  from ``acp_backends.selectable_backend_values()`` would restore exactly the
  drift that a literal list in three places once caused: the wire would accept
  a value this endpoint calls unselectable, or the reverse.
* ``installed`` -- whether the harness is on THIS machine, from
  :mod:`kiro_crew.agent_sdk`, which asks through the spawn's own resolvers.
  Reached through the SDK rather than the ACP layer directly: this handler is
  application code, and ``scripts/check_agent_sdk_boundary.py`` is what keeps
  that true.
* ``auth`` -- how the harness signs in, projected from its one declaration in
  ``agent_sdk.host_auth``. Installed and signed-in are different questions with
  different remedies, and a panel that only had ``installed`` could show a green
  row for a harness whose every session dies at start on an authentication
  error. Nothing here READS a credential: the declaration is static data, so the
  row says which store holds the entitlement and what to run, and never that a
  particular harness is currently signed in.
* the CARD -- ``capabilities``, ``security_notes``, ``operator_notes``,
  ``tool_approval`` and ``offered_by_build``, projected from the harness's own
  capability memberships
  by :mod:`kiro_crew.agent_sdk.backend_cards`. This is the question the other
  three cannot answer: an operator is choosing between harnesses, and
  selectability plus install state say only whether a choice is available, never
  what it costs. The projection is spread into the row rather than nested under
  one key, because each of those four is a field the panel reads on its own.

  The classification -- which memberships are user-facing, which are operator
  notes, and which reach no card at all -- belongs to the projection and not to
  this handler or to the panel. A second place deciding it is a second place that
  can disagree about what a line means.

  A capability line carries THREE states, and this handler forwards all three
  untouched: ``available`` is a bool, and ``measured`` is false on the cells the
  projection declares nobody has driven yet. Nothing here collapses the pair,
  which is the one thing that would matter -- ``available`` is already false on an
  unmeasured line, so a reader of that field alone gets the fail-closed answer,
  and a handler that dropped ``measured`` would silently turn "no answer yet" into
  "this harness cannot".

Owner-only, and the snapshot is offloaded: the Claude probe shells out to mise
and walks the filesystem, and resolving the governance ceiling loads config, so
neither may run on the event loop.

## Why there are two verbs

The GET reports; it never mutates. That is why ``restart_required`` exists at
all: a harness installed AFTER this gateway cached its absence reads as
installed-but-unusable, and every ``*_cached_negative`` seam in
``agent_sdk.drivers.acp`` documents that it CONSULTS the spawn path's cache
rather than clearing it, because a replayable, unattributed read must not have a
side effect.

An operator who has just run the install command the panel printed is in exactly
that state, and telling them to restart the gateway is a real cost for a fact only
this process still believes. So the clear is offered as its OWN request:
``POST /api/acp-backends/recheck`` names one backend, drops what this process cached
about it, re-probes, and returns that one row.

The VERB is what makes the mutation legitimate. Every ``*_cached_negative`` seam
documents that it only ever consults the spawn path's cache, and the rule behind that
is about replayability: a GET is unattributed and may be retried by anything, so a
side effect inside one is a mutation nobody requested. A POST is the request that
did. It is owner-gated, validated, and audited, and the GET's contract is unchanged.

**The clear runs on the event loop; only the probe is offloaded.** That split is
load-bearing, not tidiness -- see :func:`api_acp_backend_recheck` and the driver
seam's own docstring. Doing it the other way round is a real defect: every reader on
the spawn path has no ``await`` between its check and its read, so a clear from a
worker thread can land inside that pair and either raise ``KeyError`` in a session
spawn or make an installed adapter report "not found".

``restart_required`` is not suppressed by the re-check, only unblocked. The re-probe
reads it from the spawn path again AFTER the clear, so a component this process
somehow still holds a negative for keeps saying so. There is no parameter here that
writes a verdict.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from aiohttp import web

from kiro_crew.dashboard.handlers.kiro_prerequisite import _is_dashboard_owner
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Machine-readable error code, per the dashboard error-code contract
#: (``test/test_error_code_contract.py``): the client branches on this, and the
#: English prose beside it is advisory. Spelled to match the other owner-gated
#: dashboard handlers rather than inventing a per-endpoint code.
_CODE_OWNER_REQUIRED = "dashboard_owner_required"
_OWNER_REQUIRED_MESSAGE = "dashboard owner required"

#: The re-check's own refusal, for a body that names no backend this build knows.
#: A distinct code because the remedy is distinct: the owner code means "sign in as
#: the owner", this one means "that is not a backend id".
_CODE_UNKNOWN_BACKEND = "unknown_agent_backend"
_UNKNOWN_BACKEND_MESSAGE = "unknown agent backend"

_AUDIT_OPERATION = "acp_backend_status_access"
#: Audited under its own operation name, and audited on the SUCCESS path too --
#: unlike the GET beside it, this request mutates spawn-path state, so the record
#: of who cleared what is the point rather than a denial log.
_AUDIT_RECHECK_OPERATION = "acp_backend_recheck"


async def _deny_non_owner(
    request: web.Request, *, operation: str = _AUDIT_OPERATION
) -> web.Response | None:
    """Refuse a non-owner, mirroring the prerequisite handlers' 403 shape.

    Which components are installed on the host is host-configuration state and
    belongs to the same audience as the first-run setup surface it complements,
    so it reuses that module's owner predicate rather than a second one.

    *operation* is what the denial is recorded AS. Defaulted to the read so the GET
    is unchanged, and passed explicitly by the re-check: an auditor asking who tried
    to re-probe a harness would otherwise find those attempts filed under the
    read's name, which is the one question the audit log exists to answer.
    """
    if _is_dashboard_owner(request):
        return None

    caller = str(request.get("user") or "")
    audit_caller = str(request.get("app") or caller or "unknown")

    def _audit() -> None:
        sel().log_api_access(
            caller=audit_caller,
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources=request.path,
            error=_OWNER_REQUIRED_MESSAGE,
        )

    try:
        await asyncio.to_thread(_audit)
    except Exception:
        # An unwritable audit log must not convert a denial into a 500 -- the
        # refusal is the security-relevant half and still has to land.
        logger.debug("Could not audit denied ACP backend status access", exc_info=True)
    return web.json_response(
        {"error": _OWNER_REQUIRED_MESSAGE, "code": _CODE_OWNER_REQUIRED},
        status=403,
    )


def _row(state: Any, selectable: set) -> Dict[str, Any]:
    """One backend's wire row, from its install state and the selectable set.

    Shared by both verbs on purpose. The re-check returns the SAME shape the GET
    sends, so the panel splices its answer into the list it already has rather
    than holding a second, differently-shaped notion of a row -- and a field added
    to one verb cannot go missing from the other.
    """
    from kiro_crew.agent_sdk import (
        INSTALLED,
        MISSING,
        card_payload,
        declaration_for,
        signs_in_separately,
    )

    auth = declaration_for(state.backend)
    return {
        "id": state.backend,
        "policy_id": state.policy_id,
        "selectable": state.backend in selectable,
        "installed": state.installed,
        # Enforced here, not just by the probes: the contract makes this
        # non-empty ONLY for a MISSING verdict, so an UNKNOWN row can
        # never name a component the check never confirmed was absent.
        "missing_components": (
            list(state.missing_components) if state.installed == MISSING else []
        ),
        "install_command": state.install_command,
        # Clamped to a MISSING-free verdict for the same reason as
        # ``missing_components`` above: "installed but the running gateway
        # cannot use it yet" is only meaningful once the components are
        # actually there. A MISSING or UNKNOWN row carries False.
        "restart_required": (
            bool(state.restart_required) if state.installed == INSTALLED else False
        ),
        # ``sign_in_remedy`` is rendered VERBATIM by the panel and is
        # deliberately untranslated. A per-harness sign-in string that went
        # through i18n would be a per-harness edit to thirteen locale files
        # by construction, and the harness that shipped selectable first is
        # exactly the one nobody would remember to add -- so an untranslated
        # remedy that is present beats a translated one that is missing.
        #
        # Two fields, because two are read. The panel renders the remedy and
        # branches on ``signs_in_separately``; the entitlement source's
        # identifier has no frontend reader and is not sent -- a field can
        # return with its first consumer.
        "auth": {
            "sign_in_remedy": auth.sign_in_remedy,
            "signs_in_separately": signs_in_separately(state.backend),
        },
        # The card. Every line is keyed by a machine id whose LABEL is the
        # panel's, phrased once per capability -- the opposite trade from
        # ``sign_in_remedy`` above, and the reason it can be translated at
        # all: a capability label is reused by every harness, so a new
        # harness costs no locale edit while a new LINE costs thirteen.
        **card_payload(state.backend),
    }


def _snapshot() -> List[Dict[str, Any]]:
    """Build the rows. BLOCKING -- run under ``asyncio.to_thread``.

    ``_selectable_acp_backends`` is imported here rather than at module scope:
    ``handlers.core`` is a large sibling in the same package, and a
    module-scope import from a module the package ``__init__`` also imports is
    how a cycle gets introduced later.
    """
    from kiro_crew.agent_sdk import probe_backends
    from kiro_crew.dashboard.handlers.core import _selectable_acp_backends

    selectable = set(_selectable_acp_backends())
    return [_row(state, selectable) for state in probe_backends()]


def _recheck(backend: str) -> Dict[str, Any]:
    """Probe one backend and build its row. BLOCKING -- run under ``to_thread``.

    Assumes the caches have ALREADY been dropped, on the event loop, by
    ``agent_sdk.forget_for_recheck``. The two halves are deliberately not bundled
    into one offloadable call: the clear is only thread-safe on the loop, so a
    function that did both would have to run somewhere that makes one of them
    wrong. :func:`api_acp_backend_recheck` is where the order is visible.
    """
    from kiro_crew.agent_sdk import probe_backend
    from kiro_crew.dashboard.handlers.core import _selectable_acp_backends

    selectable = set(_selectable_acp_backends())
    return _row(probe_backend(backend), selectable)


async def api_acp_backend_status(request: web.Request) -> web.Response:
    """GET /api/acp-backends -- selectability + install state for every backend."""
    denial = await _deny_non_owner(request)
    if denial is not None:
        return denial
    backends = await asyncio.to_thread(_snapshot)
    return web.json_response({"backends": backends})


async def api_acp_backend_recheck(request: web.Request) -> web.Response:
    """POST /api/acp-backends/recheck -- re-probe ONE backend, cache cleared first.

    The body names the backend: ``{"backend": "<id>"}``. The kiro backend is the
    empty string in this vocabulary and is a REAL value, so an absent key and
    ``""`` cannot be told apart by truthiness -- the key's PRESENCE is what is
    checked.

    The id is validated against ``ACP_BACKENDS_KNOWN`` before anything is cleared or
    probed. Not defensive tidying: an unvalidated id would be a caller-chosen key
    written into this process's probe cache on every request, so the check is what
    bounds that dict to the ids the build knows.

    The clear happens HERE, inline on the event loop, and only the probe is offloaded.
    Reversing that is a defect rather than a preference: readers on the spawn path
    have no ``await`` between their check and their read, so they are atomic against
    other loop tasks and NOT against a worker thread. See
    ``drivers.acp.forget_cached_resolution``.
    """
    denial = await _deny_non_owner(request, operation=_AUDIT_RECHECK_OPERATION)
    if denial is not None:
        return denial

    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict) or "backend" not in body:
        return web.json_response(
            {"error": _UNKNOWN_BACKEND_MESSAGE, "code": _CODE_UNKNOWN_BACKEND},
            status=400,
        )
    backend = body.get("backend")

    # ``acp_backends`` is the application-facing spelling of the backend
    # vocabulary -- the same module ``handlers.core`` reads the selectable set
    # from -- so the id set and the allowlist the PATCH validates against come
    # from one place. The SDK is not asked for it because its only answer would
    # be ``probe_backends()``, which probes all eight to learn eight names.
    from kiro_crew.acp_backends import ACP_BACKENDS_KNOWN
    from kiro_crew.agent_sdk import forget_for_recheck

    if not isinstance(backend, str) or backend not in ACP_BACKENDS_KNOWN:
        return web.json_response(
            {"error": _UNKNOWN_BACKEND_MESSAGE, "code": _CODE_UNKNOWN_BACKEND},
            status=400,
        )

    # On the loop, before the offload: the clear must not run in the worker thread.
    forget_for_recheck(backend)
    row = await asyncio.to_thread(_recheck, backend)

    caller = str(request.get("user") or "")
    audit_caller = str(request.get("app") or caller or "unknown")

    def _audit() -> None:
        sel().log_api_access(
            caller=audit_caller,
            operation=_AUDIT_RECHECK_OPERATION,
            outcome="success",
            source="dashboard",
            resources=backend or "kiro",
        )

    # AFTER the work, not before it. Auditing first would record a success for a
    # re-probe that then raised, and the caller would have a 500 in hand while the
    # log said it worked -- the one direction an audit record must not be wrong in.
    # A raising probe needs no record of its own: it mutated nothing but a verdict
    # this module owns and rebuilds on the next read.
    try:
        await asyncio.to_thread(_audit)
    except Exception:
        # An unwritable audit log must not turn a completed re-probe into a 500.
        # The answer is already computed and is what the operator asked for.
        logger.debug("Could not audit ACP backend re-check", exc_info=True)

    return web.json_response({"backend": row})
