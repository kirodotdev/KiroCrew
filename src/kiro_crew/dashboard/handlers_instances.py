"""Instances API handlers — owner-only control plane for multi-instance mgmt.

Backs the ``/api/instances/*`` routes. Every route:

* is **gated behind ``instances.enabled``** (default off) — returns 403 when
  the feature is disabled, so toggling requires an explicit opt-in;
* is **owner-only and never reachable via the Slack path** — a request whose
  ``X-Session-Key`` indicates a Slack origin is rejected, so chat-sharing a hub
  can never pivot into SSH control;
* emits a **SEL audit event** on both reads and writes (security observability
  for an SSH-pivoting control plane).

Connect responds with the minted dashboard **token** so the browser can set the
embedded iframe's first-party cookie; that response is the *only* place a token
crosses this boundary, it is never logged, and it never appears in list/status.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import importlib as _importlib
import logging
import math
import sys as _sys
from typing import TYPE_CHECKING

from aiohttp import web

import kiro_crew  # noqa: F401
from kiro_crew import _facade_mirror
from kiro_crew.apps.version import versions_compatible  # noqa: F401
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_persistence import (  # noqa: F401
    cap_effort_capability_levels,
    register_reasoning_effort_values,
)
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers._shared import (  # noqa: F401
    SESSION_SEARCH_TEXT_FIELDS,
    _owner_denial_response,
    read_capped_response,
)
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.handlers_instances_gate import (
    _ADDRESSING_FIELDS,
    _CHAIN_MUTATION_LOCK,
    _PEER_FIELD_MAX_CHARS,
    _apply_update,
    _audit,
    _chain_refusal,
    _guard,
    _instance_view,
    _is_correlated_cloud_instance,
    _registry,
    _status_for,
)
from kiro_crew.dashboard.peer_redaction import redact_peer_text  # noqa: F401
from kiro_crew.dashboard.session_transfer import (
    SnapshotUnstable,
    TranscriptBusy,
    TranscriptWithheld,
    build_transfer_bundle_async,
    local_instance_label,
    release_bundle_files,
    write_bundle_file,
)
from kiro_crew.dashboard.state import MAX_LIVE_SLOTS  # noqa: F401
from kiro_crew.history import SEARCH_MIN_CHARS
from kiro_crew.instances.constants import (  # noqa: F401
    PEER_SLOTS_REPLY_MAX_BYTES,
    PROXY_PATH_MAX_DECODE_PASSES,
    PROXY_REDACT_BUFFER_MAX_BYTES,
    PROXY_REQUEST_BODY_MAX_BYTES,
)
from kiro_crew.instances.registry import (  # noqa: F401
    DEFAULT_REMOTE_PORT,
    MAX_CHAINED_PER_PARENT,
    MAX_VIA_HOPS,
    DuplicateInstanceError,
    InstanceDisabledError,
    InstanceNotFoundError,
    InstancesError,
    InstancesRegistry,
    InvalidInstanceError,
    ancestor_ids,
    descendant_ids,
    is_headless_provisioner,
    validate_ttl,
)
from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError, TunnelState  # noqa: F401
from kiro_crew.instances.validation import UnsupportedConnectionMethodError
from kiro_crew.instances.warm_set import resolve_warm_set_cap
from kiro_crew.loop_lock import LoopBoundLock  # noqa: F401
from kiro_crew.security import redact
from kiro_crew.sel import sel  # noqa: F401
from kiro_crew.validation import sanitize_string

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger("kiro_crew.dashboard.handlers_instances")


# ── read endpoints ───────────────────────────────────────────────────────


async def api_instances_list(request: web.Request) -> web.Response:
    """GET /api/instances — list configured instances with live status."""
    denied = _guard(request, "list")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    reg = _registry(state)
    # Registry calls read (and mutations atomically rewrite + fsync)
    # instances.json under a threading lock a to_thread worker may hold across
    # its fsync — so every registry touch in these handlers goes off the loop.
    items = [_instance_view(state, i) for i in await asyncio.to_thread(reg.list)]
    # Resolved here rather than served raw: the automatic mode (0) means "as many
    # as could be warm at once", and this is the only place that holds both the
    # stored value and the registry. The browser therefore always receives a
    # concrete integer and needs no notion of automatic.
    #
    # Counted from the REGISTRY, not from live status. A connected-count made the
    # cap race tunnel startup: a crew that finished connecting just after this
    # poll was not counted, the cap came back one short, and the viewport evicted
    # a pane to honour it -- so one crew looked broken, and which one depended on
    # connection order. Registered crews cannot race, and the count rises by
    # itself when a crew is added.
    eligible = len(items)
    _audit("list", "success")
    return web.json_response(
        {
            # ``active`` distinguishes "enabled in config" from "usable now": the
            # _guard only checks the (live) config flag, so this endpoint answers
            # 200 as soon as instances.enabled=true — but the SSH manager only
            # exists if the flag was on at gateway startup. When enabled && not
            # active, the UI shows a "restart the gateway to activate" hint.
            "active": getattr(state, "instances_manager", None) is not None,
            "instances": items,
            "warm_set_cap": resolve_warm_set_cap(
                KiroCrewConfig.load().instances.warm_set_cap, eligible
            ),
        }
    )


async def api_instances_status(request: web.Request) -> web.Response:
    """GET /api/instances/{id}/status — live tunnel status for one instance."""
    denied = _guard(request, "status")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    instance_id = request.match_info["id"]
    reg = _registry(state)
    if await asyncio.to_thread(reg.get, instance_id) is None:
        _audit("status", "denied", request_id=instance_id, error="not found")
        return web.json_response({"error": "not found"}, status=404)
    # Optional on-demand failure diagnosis: ?diagnose=1 runs the ordered probe
    # ladder and attaches the result to the returned status.
    # NOTE: for an instance that has never connected there is no live tunnel,
    # so diagnose() can't store the result on a tunnel status — it returns it
    # instead. Merge that return value into the response (otherwise Diagnose on
    # a disconnected instance silently shows nothing, which defeats its purpose).
    diag = None
    if request.query.get("diagnose"):
        mgr = getattr(state, "instances_manager", None)
        if mgr is not None:
            diag = await mgr.diagnose(instance_id)
    _audit("status", "success", request_id=instance_id)
    status = _status_for(state, instance_id)
    if diag is not None:
        status.setdefault("diagnosis", diag)
    return web.json_response(status)


# ── write endpoints ──────────────────────────────────────────────────────


async def api_instances_add(request: web.Request) -> web.Response:
    """POST /api/instances — add a configured instance.

    ``via_instance_id`` + ``via_remote_port`` add a CHAINED crew: one this gateway
    reaches by riding a hop an already-configured crew holds. ``via_remote_id`` is
    that crew's id in the PARENT's registry, which is the id the parent looks it up
    by when asked to mint its token -- an id derived from the name here would only
    coincide with it by luck. The depth cap and the parent's own suitability are
    decided here, before anything is written or dialled -- see
    :func:`_chain_refusal`.
    """
    denied = _guard(request, "add")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    reg = _registry(state)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be an object", "code": "invalid_body"}, status=400
        )
    via_instance_id = str(body.get("via_instance_id", ""))
    via_remote_id = str(body.get("via_remote_id", ""))
    # One critical section from the parent check to the insert. Validating and
    # then writing in two awaits lets a removal of that parent land between them:
    # its subtree snapshot predates this row, so the cascade never sees it and the
    # child survives its parent. It is also what makes the one-row-per-remote-crew
    # check decisive: two announcements racing would otherwise both read a list
    # without the other's row. See `_CHAIN_MUTATION_LOCK`.
    async with _CHAIN_MUTATION_LOCK:
        if via_instance_id:
            refusal = await _chain_refusal(
                reg, via_instance_id, via_remote_id, str(body.get("name", ""))
            )
            if refusal is not None:
                _audit("add", "denied", error=refusal["code"])
                # Spelled out rather than passed through: the error-code contract
                # is a static check, and a variable body is a body it cannot read.
                return web.json_response(
                    {"error": refusal["error"], "code": refusal["code"]}, status=400
                )
        try:
            inst = await asyncio.to_thread(
                reg.add,
                name=str(body.get("name", "")),
                ssh_host=str(body.get("ssh_host", "")),
                remote_port=int(body.get("remote_port", DEFAULT_REMOTE_PORT)),
                ttl=str(body.get("ttl", "20h")),
                remote_bin=str(body.get("remote_bin", "")),
                connection_method=str(body.get("connection_method", "ssh")),
                ssm_target=str(body.get("ssm_target", "")),
                ssm_run_as=str(body.get("ssm_run_as", "")),
                aws_profile=str(body.get("aws_profile", "")),
                aws_region=str(body.get("aws_region", "")),
                via_instance_id=via_instance_id,
                via_remote_port=int(body.get("via_remote_port", 0)),
                via_remote_id=via_remote_id,
                instance_id=body.get("id"),
            )
        except DuplicateInstanceError as e:
            # Split from InvalidInstanceError because the two are different user
            # actions: a name collision is resolved by renaming, a rejected field by
            # correcting it. A client that cannot tell them apart has to parse prose.
            _audit("add", "denied", error=str(e))
            return web.json_response({"error": str(e), "code": "instance_duplicate"}, status=400)
        except InvalidInstanceError as e:
            _audit("add", "denied", error=str(e))
            return web.json_response({"error": str(e), "code": "instance_invalid"}, status=400)
        except (TypeError, ValueError) as e:
            _audit("add", "denied", error=str(e))
            return web.json_response(
                {"error": f"invalid field: {e}", "code": "invalid_field"}, status=400
            )
    _audit("add", "success", request_id=inst.id)
    return web.json_response(_instance_view(state, inst), status=201)


# Declared type of every field the PATCH body may set. `remote_port` is the only
# non-string; bool is excluded explicitly because `isinstance(True, int)` is True
# and `True` would otherwise validate as port 1.
_PATCH_FIELD_TYPES: dict[str, type] = {
    "name": str,
    "ssh_host": str,
    "ttl": str,
    "remote_bin": str,
    "connection_method": str,
    "ssm_target": str,
    "ssm_run_as": str,
    "aws_profile": str,
    "aws_region": str,
    "remote_port": int,
    # Re-points an existing chained crew at its parent's NEW loopback port after
    # the parent reconnects. `via_instance_id` is deliberately NOT editable: a
    # crew's parent is chosen when it is added, and letting a PATCH re-parent one
    # would move a crew onto a hop whose depth was never checked.
    "via_remote_port": int,
    # The owner's per-crew off switch. Turning it on tears the tunnel down.
    "disabled": bool,
}


def _wrong_typed_field(changes: dict) -> str | None:
    """Return an error message for the first field whose JSON type is wrong."""
    for key, value in changes.items():
        expected = _PATCH_FIELD_TYPES.get(key)
        if expected is None:
            continue
        if expected is int and (isinstance(value, bool) or not isinstance(value, int)):
            return f"invalid {key}: expected a number"
        if expected is str and not isinstance(value, str):
            return f"invalid {key}: expected a string"
        if expected is bool and not isinstance(value, bool):
            return f"invalid {key}: expected true or false"
    return None


async def api_instances_update(request: web.Request) -> web.Response:
    """PATCH /api/instances/{id} — edit a configured instance."""
    denied = _guard(request, "update")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    reg = _registry(state)
    instance_id = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be an object"}, status=400)
    # Only allow editing user-facing config fields (not internal hints). Derived
    # from the type map rather than listed twice, so a field can never be editable
    # without a declared type to check it against.
    allowed = set(_PATCH_FIELD_TYPES)
    changes = {k: v for k, v in body.items() if k in allowed}
    # The POST path coerces every field (`str(...)` / `int(...)`); PATCH passed the
    # decoded JSON value straight into the record, so a wrong-typed value reached
    # validators that assume the declared type: `{"name": 123}` crashed
    # `name.strip()` with an AttributeError (HTTP 500), and `{"remote_port": true}`
    # slipped through as port 1 because bool IS an int. Type-check at the boundary
    # instead of coercing, so a malformed body is REFUSED rather than silently
    # reinterpreted -- a PATCH states an intent, and guessing at it is how "7777"
    # becomes a port nobody chose.
    bad = _wrong_typed_field(changes)
    if bad is not None:
        _audit("update", "denied", request_id=instance_id, error=bad)
        return web.json_response({"error": bad, "code": "instance_invalid"}, status=400)
    # A tunnel is opened from these fields, so editing one of them makes a live
    # tunnel wrong rather than merely out of date: it keeps forwarding the old
    # port to the old host under the new label. Tear it down as part of the save
    # so the next connect builds from what the user just entered.
    transport_keys = {
        "ssh_host",
        "remote_port",
        "connection_method",
        "ssm_target",
        "ssm_run_as",
        "aws_profile",
        "aws_region",
        "remote_bin",
        # The far end of a chained forward. A live tunnel holding the parent's
        # OLD port forwards to a port nothing listens on any more, which is the
        # same wrongness as an edited host.
        "via_remote_port",
    }
    current = await asyncio.to_thread(reg.get, instance_id)
    if current is None:
        _audit("update", "denied", request_id=instance_id, error="not found")
        return web.json_response({"error": "not found", "code": "instance_not_found"}, status=404)
    # Addressing fields resolve the real EC2 stack for Stop/Start/Delete, so
    # editing them on an instance Kiro Crew launched would strand a running,
    # billing instance with no dashboard path to reach it. Checked against the
    # `current` record already fetched above rather than re-reading the
    # registry, so this costs no extra (blocking) lookup.
    # Split rather than `and`-chained: mypy unifies the operand types of an
    # `and` expression, so folding the set-intersection test into the same
    # condition makes it infer to_thread's callable as returning set[str].
    correlated = False
    if _ADDRESSING_FIELDS & set(changes):
        try:
            correlated = await asyncio.to_thread(_is_correlated_cloud_instance, current.ssm_target)
        except Exception as exc:
            # The correlation check is what stands between a caller and
            # rewriting a launched instance's addressing fields out from
            # under Stop/Start/Delete, so a lookup failure here fails CLOSED
            # (refuse the edit, persist nothing) instead of falling back to
            # `correlated = False` and risking the exact stranding this lock
            # exists to prevent.
            logger.info(
                "correlation check failed for instance %r, refusing addressing edit: %s",
                instance_id,
                exc,
            )
            _audit(
                "update",
                "denied",
                request_id=instance_id,
                error=f"correlation check failed: {exc}",
            )
            return web.json_response(
                {
                    "error": (
                        "could not determine whether this instance's addressing "
                        "fields are locked (cloud launch store unreadable) — "
                        "refusing to edit connection_method/ssm_target/"
                        "aws_profile/aws_region; retry once the store is "
                        "reachable"
                    ),
                    "code": "cloud_instance_correlation_check_failed",
                },
                status=503,
            )
    if correlated:
        _audit(
            "update",
            "denied",
            request_id=instance_id,
            error="addressing fields locked: correlated cloud instance",
        )
        return web.json_response(
            {
                "error": (
                    "connection_method/ssm_target/aws_profile/aws_region cannot be "
                    "edited on an instance Kiro Crew launched — Stop/Start/Delete "
                    "resolve the real EC2 stack through these fields, so changing "
                    "them here would strand a running, billing instance with no "
                    "dashboard path to reach it"
                ),
                "code": "cloud_instance_addressing_locked",
            },
            status=400,
        )

    # Disabling is a teardown too, and it takes the same locked path: the
    # tunnel closes and the flag lands in one critical section, so a connect
    # racing it is either torn down here or reads the flag and is refused.
    # Every disable takes it, even one `current` reads as off: that read precedes
    # the lock, so an enable and a connect can land after it.
    transport_changed = changes.get("disabled") is True or any(
        k in transport_keys and v != getattr(current, k) for k, v in changes.items()
    )
    # Validate the PROPOSED record before touching the tunnel. The registry
    # validates too, but that happens after the teardown — so a rejected edit
    # would answer 400 having already disconnected a healthy crew, punishing the
    # user for a typo the save never accepted.
    try:
        dataclasses.replace(current, **changes).validate()  # type: ignore[arg-type]
        # Not part of the record invariant: a ttl is checked where it is WRITTEN,
        # so a legacy value cannot fail an unrelated hint write (see
        # registry.validate_ttl). The edit path is such a write.
        if "ttl" in changes:
            validate_ttl(str(changes["ttl"]))
    except (InvalidInstanceError, AttributeError, TypeError, ValueError) as e:
        # AttributeError is the backstop for a field added to `allowed` but not to
        # _PATCH_FIELD_TYPES: a validator calling `.strip()` on a non-string must
        # still answer 400, never 500.
        _audit("update", "denied", request_id=instance_id, error=str(e))
        return web.json_response({"error": str(e), "code": "instance_invalid"}, status=400)
    mgr = getattr(state, "instances_manager", None)

    try:
        if transport_changed and mgr is not None:
            # The teardown and the coordinate rewrite must not be observable
            # apart. With the lock released between them a `connect` can read the
            # OLD record, and whether its tunnel is already CONNECTED or still
            # CONNECTING when the write lands decides whether any after-the-fact
            # sweep would notice — so the window is closed rather than narrowed:
            # reconfigure() holds the manager lock across both, and a racing
            # connect either finishes before (and is torn down inside) or starts
            # after (and reads the new coordinates).
            inst = await mgr.reconfigure(
                instance_id, functools.partial(_apply_update, reg, instance_id, changes)
            )
        else:
            if transport_changed:
                # No manager: nothing to tear down, but say so — a silent skip
                # would look identical to a teardown that ran.
                logger.warning(
                    "instance %s transport edited with no manager running; "
                    "no tunnel teardown performed",
                    instance_id,
                )
            inst = await asyncio.to_thread(
                functools.partial(_apply_update, reg, instance_id, changes)
            )
    except InstanceNotFoundError as e:
        _audit("update", "denied", request_id=instance_id, error=str(e))
        return web.json_response({"error": str(e), "code": "instance_not_found"}, status=404)
    except (InvalidInstanceError, InstancesError) as e:
        _audit("update", "denied", request_id=instance_id, error=str(e))
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        # The teardown could not stop the old forward. Nothing was persisted, so
        # the record still matches the tunnel that is actually running; saying so
        # is better than advancing the record over a live connection to the
        # previous machine.
        _audit("update", "denied", request_id=instance_id, error=str(e))
        logger.warning(
            "refusing to reconfigure %s: its tunnel could not be torn down",
            instance_id,
            exc_info=True,
        )
        return web.json_response(
            {
                "error": (
                    "could not close the current tunnel, so the new settings were "
                    "not saved; disconnect this crew and try again"
                ),
                "code": "tunnel_teardown_failed",
            },
            status=503,
        )
    _audit("update", "success", request_id=instance_id)
    return web.json_response(_instance_view(state, inst))


async def api_instances_remove(request: web.Request) -> web.Response:
    """DELETE /api/instances/{id} — remove an instance (disconnects first).

    The pre-removal disconnect and the offloaded ``reg.remove`` are separate
    awaits, so a reconnect landing between them can re-establish a tunnel for
    the record while it is being deleted. The post-removal disconnect closes
    that window: once the record is gone, ``connect`` refuses the unknown id,
    so a final teardown after the successful remove cannot itself be raced —
    any tunnel it finds is the leftover of a reconnect that slipped in.

    Crews CHAINED behind this one go with it. Their only route is this crew's
    hop, so a record left behind would describe a forward that can never be
    opened again -- it names a port on a machine this gateway has no way to
    reach. Every captured crew is disconnected FIRST, deepest first, and a stop
    that raises refuses the whole removal with nothing deleted: rows deleted over
    a live forwarder strand it together with its minted token, and the row that
    goes carries the ``forwarder_pid`` reclaim hint with it. Only once everything
    is down do the rows go, LEAVES FIRST and this crew last, so an interrupted
    sweep can only ever leave a parent with fewer children -- a valid, connectable
    state -- never a row naming a parent that is already gone.

    A second pass follows the deletion, naming every crew CAPTURED above rather
    than just this one: ``reg.remove`` is offloaded and therefore yields, so a tab
    reconnect can re-establish a forward between the teardown and the deletion,
    and ``disconnect`` cannot find a crew's children once their rows are gone. That
    pass cannot refuse -- the removal has already happened -- so a forward it fails
    to stop is logged as a warning instead of raising a 500 over rows that are
    already gone.
    """
    denied = _guard(request, "remove")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    reg = _registry(state)
    instance_id = request.match_info["id"]
    mgr = getattr(state, "instances_manager", None)
    # Read the chain BEFORE the disconnect: the teardown does not touch registry
    # rows, but reading first keeps the list from depending on that.
    #
    # Snapshot and deletions under one critical section: a chained add that
    # validated this crew as its parent before the snapshot would otherwise insert
    # its row after it and survive the cascade. See `_CHAIN_MUTATION_LOCK`. The
    # sweep below is outside it -- it touches no rows, and an ssh teardown that
    # hangs must not hold an unrelated add.
    async with _CHAIN_MUTATION_LOCK:
        instances = await asyncio.to_thread(reg.list)
        # Whether this crew exists is settled BEFORE anything is deleted. The rows
        # still go leaves first -- a parent must never outlive its children -- but
        # deciding existence from the parent's OWN removal meant the descendants
        # were already gone by the time a missing parent answered 404, so a DELETE
        # of an id whose row another path had removed (an orphan left by an
        # unregister, which removes one row and cascades nothing) reported "not
        # found" having just deleted the crews under it and dropped the list of
        # which ones they were.
        if not any(i.id == instance_id for i in instances):
            _audit("remove", "denied", request_id=instance_id, error="not found")
            return web.json_response({"error": "not found"}, status=404)
        chained = descendant_ids(instances, instance_id)
        if mgr is not None:
            # Every captured crew comes DOWN before any row goes, deepest first,
            # and a stop that raises aborts the whole removal with nothing deleted.
            # The distinction from a status reading matters: `status` cannot tell a
            # stop that failed from a reconnect that landed after one succeeded, so
            # refusing on it turned an ordinary race into an undeletable row -- but
            # an EXCEPTION out of `stop` is unambiguous, and deleting rows over it
            # strands a live forwarder and a minted token whose row is gone, taking
            # the `forwarder_pid` reclaim hint with it. Each descendant is torn down
            # by name rather than through the parent's own cascade, which suppresses
            # a child's failure because a shutdown must not abort on one stuck crew.
            for candidate in [*reversed(chained), instance_id]:
                try:
                    # keep_intent, for the ABORT path: on the success path these rows
                    # are deleted a moment later and the flag is moot, but if a later
                    # candidate's stop raises we return "Nothing was removed" -- and
                    # the default would already have cleared the sticky connect
                    # intent on every descendant torn down before it, dropping their
                    # tabs and making that sentence false. The manager's own cascade
                    # passes it for the same reason; this loop goes by name instead,
                    # to get an unambiguous per-crew failure, so it has to say so.
                    await mgr.disconnect(candidate, keep_intent=True)
                except Exception as e:
                    _audit("remove", "denied", request_id=instance_id, error="teardown_failed")
                    logger.warning(
                        "Refusing to remove %s: tearing down %s failed (%s)",
                        instance_id,
                        candidate,
                        type(e).__name__,
                    )
                    return web.json_response(
                        {
                            "error": (
                                f"crew {candidate} could not be disconnected, so its forward "
                                f"is still running. Nothing was removed. Retry once it is down."
                            ),
                            "code": "remove_teardown_failed",
                        },
                        status=409,
                    )

        # One atomic cascade rather than a row at a time, and the SAME helper the cloud
        # destroy path calls, so the subtree rule has one definition. The teardown loop
        # above has already brought every captured crew down, deepest first; this only
        # deletes rows. The helper answers deepest-first too, so each id is audited in the
        # order it was torn down.
        for removed_id in await asyncio.to_thread(reg.remove_cascade, instance_id):
            if removed_id != instance_id:
                _audit("remove", "success", request_id=removed_id)
    if mgr is not None:
        # Second pass, for the one case the first cannot cover: `reg.remove` is
        # offloaded, so it yields, and a tab reconnect can re-establish a forward
        # between the teardown above and the deletion. By now the rows are gone, so
        # `disconnect` cannot find this crew's children -- the ids captured before
        # the deletion are the only remaining record of who was down there.
        #
        # This pass must NOT raise: the removal has already happened, and a 500 here
        # would report failure for rows that are gone. A forward it cannot stop goes
        # to the log rather than into the response -- it was created after everything
        # was confirmed down, so it is a residual rather than the defect the first
        # pass closes, and the operator who can act on it reads the log.
        for candidate in [*reversed(chained), instance_id]:
            try:
                await mgr.disconnect(candidate)
            except Exception as e:
                logger.warning(
                    "Removed %s but could not stop a forward that reappeared for %s (%s)",
                    instance_id,
                    candidate,
                    type(e).__name__,
                )
    _audit("remove", "success", request_id=instance_id)
    return web.json_response({"removed": instance_id})


def _connect_failure_code(body: dict, fallback: str) -> str:
    """Machine-readable ``code`` for a failed connect response.

    Promotes the failure-diagnosis ladder's own verdict (``ssh_unreachable``,
    ``remote_down``, ``tunnel_down``, …) to the top level, where a client reads
    it without walking into ``diagnosis``. Only a verdict that is present AND
    negative is promoted: the stored diagnosis is the last ladder RUN, so a stale
    ``ok`` from before the failure would otherwise be published as this call's
    reason. Without a usable verdict the caller's *fallback* names the stage that
    failed instead.
    """
    diagnosis = body.get("diagnosis")
    if isinstance(diagnosis, dict) and not diagnosis.get("ok"):
        code = diagnosis.get("code")
        if isinstance(code, str) and code:
            return code
    return fallback


async def api_instances_connect(request: web.Request) -> web.Response:
    """POST /api/instances/{id}/connect — open tunnel + mint token.

    On success returns the live status plus the minted ``token`` (the only
    place a token crosses this boundary; never logged, never in list/status).
    """
    denied = _guard(request, "connect")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    instance_id = request.match_info["id"]
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        _audit("connect", "denied", request_id=instance_id, error="manager unavailable")
        return web.json_response(
            {"error": "instances manager not running", "code": "instances_manager_unavailable"},
            status=503,
        )
    # `?rebuild=1` is the pane's Retry after a load watchdog fired on a document
    # that DID navigate: every probe says the tunnel is fine, yet one stream in it
    # stalled and the pane will wait on it forever. Only a fresh forwarder on a
    # different local port clears that; the
    # idempotent connect would hand
    # the same stalled tunnel straight back. Opt-in and explicit so the
    # auto-connect fan-out and plain tab clicks keep their no-op-when-up cost.
    rebuild = request.query.get("rebuild") in ("1", "true")
    # `?only_if_connected=1` is the viewport's auto-warm: pre-mount a pane for a
    # tunnel that is ALREADY up, never bring one up. Evaluated atomically under
    # the manager lock, so an auto-warm racing an explicit disconnect can never
    # re-open the tunnel (or re-persist the intent) the user just closed.
    only_if_connected = request.query.get("only_if_connected") in ("1", "true")
    if rebuild and only_if_connected:
        # A refusal, so it leaves the same `denied` SEL line as every other
        # early exit here: the audit trail must see an owner hand-crafting a
        # pair the frontend never sends, not just the connects that went through.
        _audit(
            "connect",
            "denied",
            request_id=instance_id,
            error="rebuild and only_if_connected are mutually exclusive",
        )
        return web.json_response(
            {
                "error": "rebuild and only_if_connected are mutually exclusive",
                "code": "bad_request",
            },
            status=400,
        )
    if not only_if_connected and getattr(state, "_gateway_restart_in_progress", False) is True:
        # The restart's pre-exec seam has stopped (or is stopping) every
        # forwarder, and the exec keeps this pid: a forwarder started now
        # outlives it as an orphan nothing reclaims. `only_if_connected` starts
        # nothing, so it is exempt.
        _audit("connect", "denied", request_id=instance_id, error="gateway restarting")
        return web.json_response(
            {
                "error": "the gateway is restarting; connect again once it is back",
                "code": "restarting",
            },
            status=409,
        )
    try:
        # Keyword only when asked: the default call keeps the manager's existing
        # positional contract (and every fake that implements it).
        if rebuild:
            status = await mgr.connect(instance_id, rebuild=True)
        elif only_if_connected:
            status = await mgr.connect(instance_id, only_if_connected=True)
        else:
            status = await mgr.connect(instance_id)
    except KeyError:
        _audit("connect", "denied", request_id=instance_id, error="not found")
        return web.json_response({"error": "not found", "code": "instance_not_found"}, status=404)
    except UnsupportedConnectionMethodError as e:
        _audit("connect", "denied", request_id=instance_id, error="unsupported connection method")
        return web.json_response(
            {
                "instance_id": instance_id,
                "state": "disconnected",
                "error": str(e),
                "code": UnsupportedConnectionMethodError.code,
            },
            status=409,
        )
    except InstanceDisabledError as e:
        _audit("connect", "denied", request_id=instance_id, error="disabled")
        return web.json_response(
            {
                "instance_id": instance_id,
                "state": "disconnected",
                # The crew that is off, by id: for a chained crew, the one it rides.
                "error": f"{e}; enable it to connect",
                "code": "instance_disabled",
            },
            status=409,
        )
    if rebuild:
        _audit("connect", "rebuild", request_id=instance_id)
    body = status.to_dict()
    if only_if_connected and status.state.value != "connected":
        # Declined, not failed: the tunnel is simply not up, which is the one
        # answer a connected-only caller asked to be given without side effects.
        # 200 with a non-connected state is what the shared connect step reads
        # as `warm-declined`.
        _audit("connect", "declined", request_id=instance_id, error="not connected")
        body["code"] = "instance_not_connected"
        return web.json_response(body)
    if status.state.value == "connected":
        if body.get("turn_url"):
            # A fargate forward. The status carries a turn_url only for that
            # method, and that method has no dashboard token: the connection IS
            # the forward, so the token probe below has nothing to validate and
            # a re-mint would be refused by the manager. Hand back the URL.
            _audit("connect", "success", request_id=instance_id)
            return web.json_response(body)
        token = mgr.get_token(instance_id)
        # Validate the stored token before handing it to the browser. connect()
        # is idempotent and may return a CONNECTED tunnel whose token went stale
        # (a failed self-heal re-mint, or a remote `kirocrew restart` that
        # invalidates tokens). A stale token yields a server-rendered 403 page on
        # the iframe's first load, so the SPA never boots to fire `mc-auth-expired`
        # and the reactive recovery can't help. Probe over the live tunnel (no
        # SSH); deny-by-default, so anything short of a positive confirmation
        # forces a fresh mint.
        if not token or not await mgr.token_validates(status.local_port, token):
            token = await mgr.refresh_token(instance_id) or ""
            if not token:
                # The probe couldn't confirm the token AND the re-mint failed —
                # the link is genuinely unreachable. Serving the unconfirmed
                # token would just reproduce the stuck-iframe 403, so surface a
                # clean error instead of a token we know we can't stand behind.
                _audit(
                    "connect",
                    "failure",
                    request_id=instance_id,
                    error="token unconfirmed and re-mint failed",
                )
                body["error"] = "token expired and re-mint failed"
                body["code"] = _connect_failure_code(body, "instance_token_unconfirmed")
                return web.json_response(body, status=502)
        # The token and the port it is paired with are one answer. `body` froze the
        # port before the probe and the re-mint, both of which await for seconds,
        # and `status` is the tunnel's LIVE status object -- a teardown in that
        # window pops the tunnel without zeroing the port on it, and the allocator
        # hands a just-freed port to the next connect first. So the frozen port can
        # name a forward that now belongs to a different crew, and the pane would
        # load that one while holding this crew's token. Re-read the live object and
        # refuse the pair rather than answer with one half of it; a retry gets a
        # coherent one, which is what this failure code already means.
        if status.state.value != "connected" or int(status.local_port or 0) != int(
            body.get("local_port") or 0
        ):
            _audit(
                "connect",
                "failure",
                request_id=instance_id,
                error="forward moved while the token was being confirmed",
            )
            return web.json_response(
                {
                    "instance_id": body.get("instance_id"),
                    "state": body.get("state"),
                    "local_port": body.get("local_port"),
                    "remote_port": body.get("remote_port"),
                    "error": ("the forward moved while its token was being confirmed; try again"),
                    "code": _connect_failure_code(body, "instance_token_unconfirmed"),
                },
                status=502,
            )
        body["token"] = token  # delivered to owner only
        _audit("connect", "success", request_id=instance_id)
        return web.json_response(body)
    _audit("connect", "failure", request_id=instance_id, error=status.error)
    body["code"] = _connect_failure_code(body, "instance_connect_failed")
    return web.json_response(body, status=502)


async def api_instances_refresh_token(request: web.Request) -> web.Response:
    """POST /api/instances/{id}/refresh-token — force a fresh token mint.

    Returns the newly minted ``token`` so the owner's browser can reload the
    embedded iframe with a valid credential — proactively before the gateway's
    TTL cap, or reactively when the embedded dashboard reports an expired
    session. Like ``connect``, this is the only other place a token crosses this
    boundary; it is never logged and never appears in list/status.
    """
    denied = _guard(request, "refresh_token")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    instance_id = request.match_info["id"]
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        _audit("refresh_token", "denied", request_id=instance_id, error="manager unavailable")
        return web.json_response({"error": "instances manager not running"}, status=503)
    token = await mgr.refresh_token(instance_id)
    if not token:
        _audit(
            "refresh_token",
            "failure",
            request_id=instance_id,
            error="mint failed or instance not connected",
        )
        return web.json_response(
            {"error": "could not refresh token (instance not connected?)"}, status=502
        )
    _audit("refresh_token", "success", request_id=instance_id)
    st = mgr.status(instance_id)
    body = st.to_dict() if st is not None else {"instance_id": instance_id, "state": "connected"}
    body["token"] = token  # delivered to owner only
    return web.json_response(body)


async def api_instances_embed_token(request: web.Request) -> web.Response:
    """POST /api/instances/{id}/embed-token — mint this crew's token for a HUB.

    Called by a gateway that reaches ``{id}`` by riding OUR hop to it, and that
    therefore has no key of its own for that machine. The caller names the port it
    serves its own dashboard on; we mint a fresh token over the transport we
    already hold, carrying that port as the token's embed-parent claim, so the
    crew's own CSP admits the caller's page as its pane's frame ancestor.

    Distinct from ``refresh-token``, which mints for OURSELVES and REPLACES the
    stored credential. Nothing is stored here: this token belongs to the caller's
    pane, and writing it over ours would break our own pane for the same crew.

    Refuses a crew we ourselves reach through a further hop. That is the depth cap
    seen from this end and it is the only place it can be seen: the caller counts
    hops in its own registry and cannot know that ours adds another one.
    """
    denied = _guard(request, "embed_token")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    instance_id = request.match_info["id"]
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        _audit("embed_token", "denied", request_id=instance_id, error="manager unavailable")
        return web.json_response(
            {"error": "instances manager not running", "code": "instances_manager_unavailable"},
            status=503,
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be an object", "code": "invalid_body"}, status=400
        )
    port = body.get("embed_parent_port")
    # bool is excluded explicitly: `isinstance(True, int)` is True, so True would
    # otherwise be accepted as port 1 and mint a token no page can use.
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        _audit("embed_token", "denied", request_id=instance_id, error="bad embed_parent_port")
        return web.json_response(
            {
                "error": "embed_parent_port must be a number in [1, 65535]",
                "code": "invalid_field",
            },
            status=400,
        )
    ok, payload = await mgr.mint_embed_token(instance_id, port)
    if not ok:
        status = int(payload.pop("status", 502))
        _audit("embed_token", "failure", request_id=instance_id, error=str(payload.get("code")))
        # Spelled out, not passed through. A computed status is only readable to
        # the error-code contract when the body is a literal dict carrying `code`,
        # and these are the only two keys the mint leaves once `status` is popped.
        return web.json_response(
            {
                "error": str(payload.get("error", "could not mint a token for this crew")),
                "code": str(payload.get("code", "embed_token_failed")),
            },
            status=status,
        )
    _audit("embed_token", "success", request_id=instance_id)
    return web.json_response(payload)  # token delivered to the authenticated hub only


async def api_instances_disconnect(request: web.Request) -> web.Response:
    """POST /api/instances/{id}/disconnect — tear down one tunnel."""
    denied = _guard(request, "disconnect")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    instance_id = request.match_info["id"]
    mgr = getattr(state, "instances_manager", None)
    existed = False
    if mgr is not None:
        existed = await mgr.disconnect(instance_id)
    _audit("disconnect", "success", request_id=instance_id)
    return web.json_response({"disconnected": instance_id, "was_connected": existed})


async def api_instances_restart(request: web.Request) -> web.Response:
    """POST /api/instances/{id}/restart — restart the remote gateway over SSH."""
    denied = _guard(request, "restart")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    instance_id = request.match_info["id"]
    if await asyncio.to_thread(_registry(state).get, instance_id) is None:
        _audit("restart", "denied", request_id=instance_id, error="not found")
        return web.json_response({"error": "not found"}, status=404)
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        _audit("restart", "denied", request_id=instance_id, error="manager unavailable")
        return web.json_response({"error": "instances manager not running"}, status=503)
    result = await mgr.restart_remote(instance_id)
    if result.get("ok"):
        _audit("restart", "success", request_id=instance_id)
        return web.json_response(result)
    _audit("restart", "failure", request_id=instance_id, error=result.get("message", ""))
    return web.json_response(result, status=502)


#: How each ``start_remote_update`` refusal code answers over HTTP. Every one
#: is decided before anything runs on the remote.
_UPDATE_REFUSAL_STATUS = {
    "not_found": 404,
    "invalid_transport": 400,
    "chained": 409,
    "headless": 409,
    "already_running": 409,
}


def _update_owner_denial(request: web.Request, operation: str) -> web.Response | None:
    """The explicit owner check both update routes add on top of ``_guard``.

    The update route runs a command on another machine, so it states the
    positive owner identity itself rather than leaning on ``_guard``'s copy of
    it: an app token or a Slack-minted ``!dashboard`` subject is refused here,
    before the registry is read, even if ``_guard`` admitted it.
    """
    if not is_owner_dashboard_request(request):
        _audit(operation, "denied", error="non-owner identity rejected")
        return _owner_denial_response(request, "updating a remote crew is owner-only")
    return None


async def api_instances_update_remote(request: web.Request) -> web.Response:
    """POST /api/instances/{id}/update — run the remote crew's own updater.

    Starts a background job that runs the fixed literal ``kirocrew update`` on
    the remote, over the same transport runner and refusals as
    ``/restart`` (a chained or headless crew is refused before anything runs),
    then ``kirocrew restart`` when the update succeeds. The remote's own update
    policy, install layout and updater decide what the update does. The request
    carries no body and nothing in it reaches the remote command. Answers
    ``202 {ok, job}`` at once; poll ``GET`` on the same path for progress.
    One click starts one job: a second POST while one runs is refused 409.
    """
    denied = _guard(request, "update_remote")
    if denied is not None:
        return denied
    denied = _update_owner_denial(request, "update_remote")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    instance_id = request.match_info["id"]
    if await asyncio.to_thread(_registry(state).get, instance_id) is None:
        _audit("update_remote", "denied", request_id=instance_id, error="not found")
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        _audit("update_remote", "denied", request_id=instance_id, error="manager unavailable")
        return web.json_response(
            {"error": "instances manager not running", "code": "instances_unavailable"},
            status=503,
        )
    result = await mgr.start_remote_update(instance_id)
    if result.get("ok"):
        _audit("update_remote", "success", request_id=instance_id)
        return web.json_response(result, status=202)
    code = str(result.get("code") or "")
    message = str(result.get("message") or "remote update refused")
    _audit("update_remote", "denied", request_id=instance_id, error=f"{code}: {message}")
    return web.json_response(
        {"ok": False, "code": code, "error": message},
        status=_UPDATE_REFUSAL_STATUS.get(code, 502),
    )


async def api_instances_update_remote_status(request: web.Request) -> web.Response:
    """GET /api/instances/{id}/update — the latest update job for this crew.

    ``{job: null}`` when none has run since this gateway started. The job
    survives the crew's restart and reconnect, so the dashboard reads the
    outcome here rather than from the tunnel state.
    """
    denied = _guard(request, "update_remote_status")
    if denied is not None:
        return denied
    denied = _update_owner_denial(request, "update_remote_status")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    instance_id = request.match_info["id"]
    mgr = getattr(state, "instances_manager", None)
    job = mgr.remote_update_job(instance_id) if mgr is not None else None
    _audit("update_remote_status", "success", request_id=instance_id)
    return web.json_response({"job": job})


async def api_instances_search_sessions(request: web.Request) -> web.Response:
    """GET /api/instances/search-sessions — federated session search.

    Query params mirror ``/api/sessions/search`` (``q`` min 2 chars, ``limit``
    default 50 / max 200). Fans the query out to every CONNECTED instance's own
    ``/api/sessions/search`` (concurrently, over the already-open tunnels — the
    minted tokens never leave ``SshTunnelManager``) and runs the local search in
    the same gather, then rank-interleaves the sources: position k of the reply
    cycles through each source's k-th best hit, local first. Interleaving needs
    no cross-instance score wire format (each gateway may run a different
    ranking version), keeps every source represented in the top rows, and
    preserves each source's own order.

    Rows from a peer carry ``instance_id`` + ``instance_name``; local rows carry
    neither, so the reply shape for a hub with no peers degrades to exactly the
    local search's. Unreachable/refusing peers never fail the request: they are
    reported in ``unreachable`` as ``{id, name, code}`` so the UI can say "N
    instances unreachable" instead of silently narrowing the search.

    Same gate as every instances route (owner-only, non-Slack,
    ``instances.enabled``); peers' replies are untrusted input and are re-shaped
    and re-redacted locally before they reach the browser.
    """
    denied = _guard(request, "search_sessions")
    if denied is not None:
        return denied
    # STRICTER than _guard for this route: _guard's identity check is
    # deliberately permissive enough for send-session's app path (an app token
    # sets request["user"] and the transfer confines it with a per-slot
    # ownership check downstream). A bulk read has no per-slot confinement to
    # lean on — it discloses EVERY local and remote session's titles/snippets —
    # so it requires the positively-identified OWNER: not an app token, and not
    # a Slack user who minted a dashboard token via `!dashboard` (app == "" but
    # a non-owner subject).
    from kiro_crew.dashboard.handlers._shared import _owner_denial_response
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    if not is_owner_dashboard_request(request):
        # Domain audit stays here rather than delegating to
        # ``require_owner_dashboard_request``: ``_audit`` emits
        # ``log_tool_invocation`` under ``instances_search_sessions`` /
        # ``dashboard:instances``, which is the record this module's SEL consumers
        # watch, and the shared helper's generic ``log_api_access`` /
        # ``non_owner_block`` would silently drop this route out of that stream.
        # Only the denial TAIL is shared -- see ``_owner_denial_response``.
        _audit("search_sessions", "denied", error="non-owner identity rejected")
        # Deny decision made above; only the response label changes for a signed
        # pre-owner bootstrap subject (see stale_owner_session_response).
        return _owner_denial_response(request, "federated session search is owner-only")
    state: DashboardState = request.app["state"]
    q = sanitize_string(request.query.get("q", "")).strip()[:256]
    if len(q) < SEARCH_MIN_CHARS:
        # §6: every guarded call emits an SEL event, success and denial alike.
        # A sub-threshold query still passed the permission gate, so the
        # decision is recorded even though no search runs.
        _audit("search_sessions", "success", request_id="short-query")
        return web.json_response({"sessions": [], "unreachable": []})
    try:
        limit = max(1, min(int(request.query.get("limit", "50")), 200))
    except (TypeError, ValueError):
        limit = 50

    mgr = getattr(state, "instances_manager", None)
    connected: list[str] = []
    peer_calls = []
    if mgr is not None:
        connected = [
            iid
            for iid, st in mgr.status_all().items()
            if getattr(st, "state", None) is TunnelState.CONNECTED
        ]
        peer_calls = [mgr.search_sessions_remote(iid, q, limit) for iid in connected]

    async def _local() -> list[dict]:
        if not state.conversation_log:
            return []
        return await asyncio.get_running_loop().run_in_executor(
            None, state.conversation_log.search_sessions, q, limit
        )

    results = await asyncio.gather(_local(), *peer_calls, return_exceptions=True)

    # Snapshot instance names ONCE, off the loop, before the merge. registry.get
    # acquires a threading lock and re-reads instances.json per call — a lock a
    # to_thread mutation worker may hold across its fsync — so resolving names
    # inline per peer row (up to limit x peers per keystroke) could freeze the
    # event loop for the fsync's duration. Same rule as every other registry
    # touch in these handlers (see api_instances_list).
    names: dict[str, str] = {}
    if connected:
        try:
            reg = _registry(state)
            names = {i.id: i.name for i in await asyncio.to_thread(reg.list)}
        except Exception:
            names = {}  # badge degrades to the instance id

    def _name(iid: str) -> str:
        return names.get(iid) or iid

    def _clean(row: object, iid: str) -> dict | None:
        """Re-shape one untrusted peer row: allowlist keys, coerce, re-redact."""
        if not isinstance(row, dict):
            return None
        key = row.get("key")
        if not isinstance(key, str) or not key or len(key) > _PEER_FIELD_MAX_CHARS:
            return None
        out: dict = {"key": key, "instance_id": iid, "instance_name": _name(iid)}
        for field in ("title", "snippet", "agent", "created", "folder_id", "memory_mode"):
            value = row.get(field)
            if isinstance(value, str) and value:
                # Length clamp BEFORE redaction: a hostile/broken peer must not
                # ship megabyte strings to the browser (or feed the redaction
                # regexes unbounded input).
                value = value[:_PEER_FIELD_MAX_CHARS]
                if field in SESSION_SEARCH_TEXT_FIELDS:
                    value = redact(value)
                out[field] = value
        for field in ("modified", "messages"):
            value = row.get(field)
            # bool is an int subclass and 1e309 is a float — but True/Infinity
            # in these fields is peer garbage, and a non-finite value makes
            # json.dumps emit bare `Infinity`, which the browser's JSON.parse
            # rejects, losing the WHOLE federated response to one bad row.
            # isfinite itself raises OverflowError on an int too large for
            # float (peer JSON carries arbitrary-precision ints), so that
            # garbage is dropped the same way rather than crashing the merge.
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                try:
                    if math.isfinite(value):
                        out[field] = value
                except OverflowError:
                    pass
        return out

    sources: list[list[dict]] = []
    unreachable: list[dict] = []
    local_rows = results[0]
    if isinstance(local_rows, list):
        # Local redaction: /api/sessions/search redacts title/snippet in its
        # HANDLER, and this endpoint calls conversation_log.search_sessions
        # directly, bypassing that handler — so the same redaction must run
        # here before the rows reach the browser.
        redacted_local: list[dict] = []
        for row in local_rows:
            for field in SESSION_SEARCH_TEXT_FIELDS:
                value = row.get(field)
                if isinstance(value, str) and value:
                    row[field] = redact(value)
            redacted_local.append(row)
        sources.append(redacted_local)
    for iid, result in zip(connected, results[1:]):
        if isinstance(result, BaseException):
            _audit("search_sessions", "failure", request_id=iid, error=type(result).__name__)
            unreachable.append({"id": iid, "name": _name(iid), "code": "search_unreachable"})
            continue
        ok, payload = result
        if not ok:
            code = str(payload.get("code", "search_unreachable"))
            # The code lands in the SEL audit trail too, so an operator can
            # tell a stale credential from a dead tunnel per peer after the
            # fact without reproducing the search.
            _audit("search_sessions", "failure", request_id=iid, error=code)
            unreachable.append({"id": iid, "name": _name(iid), "code": code})
            continue
        rows = payload.get("sessions")
        cleaned = []
        if isinstance(rows, list):
            for row in rows[:limit]:
                c = _clean(row, iid)
                if c is not None:
                    cleaned.append(c)
        sources.append(cleaned)

    merged: list[dict] = []
    for rank in range(limit):
        for source in sources:
            if rank < len(source):
                merged.append(source[rank])
            if len(merged) >= limit:
                break
        if len(merged) >= limit:
            break
    _audit("search_sessions", "success", request_id=f"{len(connected)} peers")
    return web.json_response({"sessions": merged, "unreachable": unreachable})


async def api_instances_send_session(request: web.Request) -> web.Response:
    """POST /api/instances/{id}/send-session — copy a local session to a peer.

    Body: ``{ "slot": "<local slot key>" }``.

    Copy semantics: the local session is left completely untouched, and the peer
    allocates a fresh key. Nothing here can delete or mutate a conversation on
    either side, which is why the route needs no confirmation step.

    The minted token is NOT part of this response — the transfer is performed
    inside ``SshTunnelManager.send_session_bundle`` precisely so that ``connect``
    and ``refresh-token`` stay the only two routes where a token crosses this
    boundary (see the module docstring and instances.md §6).
    """
    denied = _guard(request, "send_session")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    instance_id = request.match_info["id"]
    inst = await asyncio.to_thread(_registry(state).get, instance_id)
    if inst is None:
        _audit("send_session", "denied", request_id=instance_id, error="not found")
        return web.json_response({"error": "not found", "code": "instance_not_found"}, status=404)
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        _audit("send_session", "denied", request_id=instance_id, error="manager unavailable")
        return web.json_response(
            {"error": "instances manager not running", "code": "instances_manager_down"},
            status=503,
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"error": "invalid JSON body", "code": "transfer_invalid_json"}, status=400
        )
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "transfer_body_not_object"},
            status=400,
        )
    slot_key = body.get("slot")
    if not isinstance(slot_key, str) or not slot_key:
        return web.json_response(
            {"error": "slot must be a non-empty string", "code": "transfer_missing_slot"},
            status=400,
        )
    slot = state._slots.get(slot_key)
    if slot is None:
        _audit("send_session", "denied", request_id=instance_id, error="slot not found")
        return web.json_response(
            {"error": "session not found", "code": "transfer_slot_not_found"}, status=404
        )
    # App-scope ownership, mirroring chat_fork. An app token sets request["user"]
    # so it clears _guard(), and an app whose manifest declares /api/instances
    # reaches here — without this it could name ANOTHER slot's key and have that
    # transcript copied to a peer, which is an exfiltration path out of the app
    # sandbox. 404 rather than 403, like fork: a slot owned by another app must be
    # indistinguishable from one that does not exist, or the error code itself
    # enumerates slots across the isolation boundary (CWE-204). The real reason is
    # recorded server-side in the audit event.
    request_app = request.get("app", "")
    if request_app and (not getattr(slot, "_app", "") or slot._app != request_app):
        _audit(
            "send_session",
            "denied",
            request_id=instance_id,
            error=f"app {request_app!r} does not own this slot",
        )
        return web.json_response(
            {"error": "session not found", "code": "transfer_slot_not_found"}, status=404
        )
    if slot.memory_mode != "persistent":
        # An incognito/temporary session has no durable transcript by design;
        # transferring one would defeat the mode the user deliberately chose.
        _audit("send_session", "denied", request_id=instance_id, error="non-persistent slot")
        return web.json_response(
            {
                "error": "cannot transfer a non-persistent session",
                "code": "transfer_slot_not_persistent",
            },
            status=400,
        )

    # No flush HERE: the builder owns it. ``build_transfer_bundle_async`` flushes
    # a dirty slot itself (best_effort=False) and only then takes its boundary
    # slice, so by that point the tail is empty and the bundle comes wholly from
    # disk. A flush at this call site would add nothing — and the version of this
    # code that flushed here and then sliced on ``_resumed_count``, which the save
    # does NOT advance, is what re-appended the same tail from memory and landed
    # every unsaved turn twice in the copy.
    try:
        bundle = await build_transfer_bundle_async(state, slot, origin=local_instance_label())
        publication_key = slot_history_key(slot)

        def _revalidate_for_publication() -> None:
            log = state.conversation_log
            if log is not None:
                expected_keys = getattr(bundle, "publication_keys", (publication_key,))
                with log.publication_hold(publication_key, expected_keys=expected_keys):
                    pass

        # The tunnel call below awaits network I/O, so the threading lock is
        # released immediately after this off-loop revalidation. The remaining
        # race window is the transmit itself; holding across the await would
        # stall the event loop behind a cross-process transcript lock.
        #
        # A refusal here returns before the send, whose ``finally`` is the other
        # place the bundle's Layer B snapshot is removed, so it is removed here.
        try:
            await asyncio.to_thread(_revalidate_for_publication)
        except BaseException:
            release_bundle_files(bundle)
            raise
    except TranscriptBusy:
        # The seam could not take the transcript lock in time; nothing was sent
        # and the source is untouched, so this is the retryable answer.
        _audit("send_session", "failure", request_id=instance_id, error="transcript busy")
        return web.json_response(
            {
                "error": "the session could not be copied consistently right now; please retry",
                "code": "transfer_snapshot_unstable",
            },
            status=503,
        )
    except TranscriptWithheld as exc:
        # The bundle is built from the transcript on DISK, and the file's own
        # privacy contract gates it, not only the live slot's mode checked above:
        # a same-key persistent recreation of a closed restricted tab, or another
        # writer tightening the line while this slot still reads persistent in
        # memory. The builder checks the line before and after its read; nothing
        # was sent. Same refusal as the slot gate, because it is the same fact.
        _audit("send_session", "denied", request_id=instance_id, error=f"on-disk line: {exc}")
        return web.json_response(
            {
                "error": "cannot transfer a non-persistent session",
                "code": "transfer_slot_not_persistent",
            },
            status=400,
        )
    except SnapshotUnstable:
        # No consistent view of the source: either a flush landed inside every
        # retry, or a rewind/regenerate rewrite is still owed so disk is stale.
        # Retryable, and the source is untouched.
        _audit("send_session", "failure", request_id=instance_id, error="snapshot unstable")
        return web.json_response(
            {
                "error": "the session could not be copied consistently right now; please retry",
                "code": "transfer_snapshot_unstable",
            },
            status=503,
        )

    def _recheck_before_post() -> dict | None:
        # Serialising a large session takes long enough for the privacy line to
        # tighten after the check above, so it is re-checked just before the
        # request leaves. Blocking; the send runs it off the loop.
        try:
            _revalidate_for_publication()
        except TranscriptBusy:
            return {
                "error": "the session could not be copied consistently right now; please retry",
                "code": "transfer_snapshot_unstable",
            }
        except TranscriptWithheld:
            return {
                "error": "cannot transfer a non-persistent session",
                "code": "transfer_slot_not_persistent",
            }
        return None

    try:
        ok, payload = await mgr.send_session_bundle(
            instance_id,
            bundle,
            # Plain JSON, which every importer release reads, streamed from disk.
            serialise=lambda b: write_bundle_file(b, compress=False),
            recheck=_recheck_before_post,
        )
    finally:
        release_bundle_files(bundle)
    if not ok:
        _audit(
            "send_session",
            "failure",
            request_id=instance_id,
            error=str(payload.get("code", "unknown")),
        )
        code = payload.get("code", "transfer_peer_refused")
        status = {"transfer_slot_not_persistent": 400, "transfer_snapshot_unstable": 503}.get(
            code, 502
        )
        # Re-emit the peer's reason explicitly rather than forwarding *payload*
        # verbatim: the code must be statically visible in the response body
        # (test_error_code_contract.py), and spelling both fields out also
        # guarantees a code is present even if a future peer omits one.
        return web.json_response(
            {
                "error": payload.get("error", "the transfer failed"),
                "code": payload.get("code", "transfer_peer_refused"),
            },
            status=status,
        )
    _audit("send_session", "success", request_id=instance_id)
    return web.json_response(
        {
            "ok": True,
            "instance": instance_id,
            "remote_key": payload.get("key", ""),
            "messages": len(bundle.get("messages", [])),
            # Forwarded from the peer so the row can distinguish a full-fidelity
            # copy from one that degraded to the transcript-only prefix. Without
            # it a lossy transfer shows the same "Sent" as a resumable one -- the
            # silent degradation this feature exists to remove. Older peers omit
            # it; "" means unknown, which the UI treats as plain "Sent".
            "resume_mode": payload.get("resume_mode", ""),
        }
    )


# --------------------------------------------------------------------------- #
# This module is the facade of the Instances control plane: every name is reached
# as ``kiro_crew.dashboard.handlers_instances.<name>``. It keeps the registry,
# connection, remote-update, search and send routes. A read of a name an owner
# binds is served by that owner, and a write -- ``monkeypatch.setattr``,
# ``mock.patch`` -- reaches every member holding it (:mod:`kiro_crew._facade_mirror`).
# Owners never import this module. New work goes to the owner of its
# responsibility:
#
#   ``handlers_instances_gate`` -- the owner gate, audit, registry access, chain policy
#   ``handlers_instances_peer`` -- the routes that read a peer, and the proxy to it
# --------------------------------------------------------------------------- #
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.handlers_instances_gate",
    "kiro_crew.dashboard.handlers_instances_peer",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.handlers_instances_gate import _is_slack_origin  # noqa: F401
    from kiro_crew.dashboard.handlers_instances_peer import (  # noqa: F401
        _CAP_MAX_ROWS,
        _CAP_MAX_STR,
        _CAP_MAX_VERSION_STR,
        _PEER_SLOT_BOOL_FIELDS,
        _PEER_SLOT_PARENT_FIELDS,
        _PEER_SLOT_STR_FIELDS,
        _PROXY_ALLOWED_PREFIXES,
        _PROXY_METHODS,
        _PROXY_PATH_DENIED_REASON,
        _PROXY_RESP_ALLOW_CONTENT_TYPES,
        _PROXY_RESP_ALLOW_HEADERS,
        _PROXY_SEGMENT_RE,
        _URL_PATH_SEP,
        PeerSlots,
        PeerSlotsUnavailable,
        ProxyReplyUnredactable,
        _can_run_remote_commands,
        _cap_list,
        _cap_rows,
        _cap_str,
        _clean_peer_parent,
        _clean_peer_slot,
        _proxy_canonical_path,
        _redact_peer_payload,
        _redact_peer_value,
        _redact_sse_event,
        _redact_sse_event_async,
        _remote_is_behind,
        api_instances_approval_state,
        api_instances_capabilities,
        api_instances_chat_slots,
        api_instances_proxy,
        api_instances_versions,
        read_peer_slots,
    )
