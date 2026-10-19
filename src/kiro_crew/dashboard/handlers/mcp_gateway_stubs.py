"""Opting servers into the gateway stub.

Which servers may be stubbed, recording each stub decision, and the set-stub route.

Part of the ``kiro_crew.dashboard.handlers.mcp`` family: callers and tests reach every
name here as ``kiro_crew.dashboard.handlers.mcp.<name>``, and a patch there reaches this
module too (:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from aiohttp import web

from kiro_crew.config.loader import (
    FORWARD_DECLARED_ENV_DEFAULT,
    _resolve_stub_overrides,
    _resolve_stub_roster,
)
from kiro_crew.config.sections import MANAGED_STUB_FOLLOWERS
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.handlers.mcp_config_files import _is_valid_mcp_name, _offload_config_write
from kiro_crew.dashboard.handlers.mcp_gateway_routes import (
    _MCP_GATEWAY_APPLY_LOCK,
    _assess_server,
    _collect_server_rows,
    _freeze_stub_servers,
    _launch_specs_for,
    _local_overlay_section,
    _overlay_shadowed_keys,
)
from kiro_crew.dashboard.handlers.mcp_probe import _probe_currency
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.mcp_gateway import hazards, launch_resolve
from kiro_crew.mcp_gateway.launch_approval import (
    approve,
    display_launches,
    load_approvals,
    refused_servers,
    restore,
    revoke,
    target_stem,
)
from kiro_crew.mcp_gateway.rewriter import records_dir
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.dashboard.handlers.mcp")


# Max server names accepted by one /api/mcp-gateway/servers/stub call. The
# batch form exists for the UI's "toggle all", whose upper bound is the number of
# configured servers, so this only fences a hand-rolled request from turning one
# config write into an unbounded one.
_MAX_STUB_BATCH = 200


def _record_stub_decisions(section: dict, names: list[str], stub: bool) -> None:
    """Record a toggle as a DECISION in ``stub_overrides``, not as a new stub set.

    Call AFTER :func:`_freeze_stub_servers`, which settles what the roster is;
    this writes only the operator's deviation from it.

    Rewriting ``stub_servers`` instead would make the roster un-shippable. That
    key is the layer a distribution owns, so a handler that persists the resulting
    set into it takes ownership away on the first click: every later roster change
    arrives into a file that already answers the question, and the addition
    silently never takes effect. Writing the decision
    instead leaves the roster alone, so the two layers can both keep moving.

    A decision that AGREES with the roster deletes the override rather than
    storing it. The two are identical in effect today and differ tomorrow: stored,
    the entry would pin that server against a later roster change — so an
    operator toggling a server back to the roster's answer is asking to follow the
    roster again, not to freeze its current value. That prune is also what keeps
    the map sparse under the batch action, which would otherwise write an entry
    for every eligible server at once and shadow the roster wholesale.

    Sorted on write so the file has one canonical form and two writers cannot
    produce a spurious diff.
    """
    roster = set(_resolve_stub_roster(section))
    # A follower's default moves with the leader (``_resolve_stub_base``), so a
    # decision that agrees with it today can disagree after the leader's next
    # toggle -- including one later in this same call. Pruning it would let that
    # toggle silently undo the operator's choice, so a follower decision is always
    # stored. Everything else is pruned against the roster, as before.
    overrides = dict(_resolve_stub_overrides(section))
    for name in names:
        if name in MANAGED_STUB_FOLLOWERS:
            overrides[name] = stub
        elif (name in roster) == stub:
            overrides.pop(name, None)
        else:
            overrides[name] = stub
    # Normalize the roster's FORM, never its content. The old write path rebuilt
    # this key from a set on every toggle, which incidentally deduplicated it and
    # dropped non-string junk; recording decisions elsewhere would silently retire
    # that, and a duplicate here makes the dashboard's ``stub_count`` overcount
    # (the resolver preserves what the file holds). Membership is untouched, so
    # the layer stays the distribution's to ship and to grow.
    section["stub_servers"] = sorted(roster)
    if overrides:
        section["stub_overrides"] = {name: overrides[name] for name in sorted(overrides)}
    else:
        # Drop the key rather than leave `{}` behind: absent and empty mean the
        # same thing to the resolver, and an operator who has reverted every
        # deviation should not be left with a residue suggesting otherwise.
        section.pop("stub_overrides", None)


def _stub_eligibility(
    names: list[str],
    *,
    sharing_on: bool,
    forward_declared_env: bool,
    probe_current: Mapping[str, bool] | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    """Decide which of *names* the evidence allows stubbing. BLOCKING.

    THE eligibility rule, and the only copy of it. The caller passes the sharing
    state it read under the config lock, so the answer describes the fleet as the
    write about to happen will find it -- not as a client saw it when a person
    started composing the batch. A client that resolved this itself could only
    ever answer for an earlier moment: sharing is a separate switch another
    dashboard or the CLI can flip, and every guard against that window closes the
    window rather than the gap it comes from.

    Returns ``(eligible, skipped)`` where each skip carries the reason code the
    operator needs to understand a count they did not expect. Reasons:

    - ``unknown`` -- not in any agent spec, so there is nothing to stub
    - ``cannot_stub`` -- HTTP/SSE (no stdio pipe to interpose on) or denylisted
    - ``pooling_blocked_by_env`` -- sharing is on and the rewriter would leave this
      entry unwrapped to avoid withholding a declared key, so stubbing it would
      report work the broker never does
    - ``evidence_insufficient`` -- the verdict does not recommend the operation
      being asked for, INCLUDING when the stored measurement describes a program
      other than the one this name launches

    With sharing ON the question is ``recommend_share``, because a stub joins a
    pooled backend and co-tenancy is what needs supporting. With sharing OFF a
    stub pools nothing, so ``recommend_stub`` is the honest bar; asking for the
    stricter one there would make the gesture stub nothing at all in the
    configuration most operators are in.

    The two kinds of evidence are read differently, because they push opposite
    ways. A PREFLIGHT can promote a server and enable co-tenancy, so it is read
    through ``VerdictCache.get`` with the server's CURRENT identity, and only when
    every definition under that name resolves to the SAME identity: a row is keyed
    by name, so a replaced command keeps the previous measurement until the next
    probe overwrites it, and two agents declaring one name differently mean no
    single row describes what this write would stub. Either way a mismatch reads
    as no measurement, which is the same fail-closed answer as never having
    probed. HAZARDS only ever demote, so they are read name-wide rather than
    identity-filtered -- restricting them to the current identity would DISCARD a
    recorded objection and read as promotion.
    """
    from kiro_crew.config.loader import KiroCrewConfig  # noqa: F811
    from kiro_crew.mcp_gateway.evaluate import identity_for
    from kiro_crew.mcp_gateway.rewriter import (
        UNPOOLABLE_SERVERS,
        _withheld_env_count,
        pool_identity_env_keys,
    )
    from kiro_crew.mcp_gateway.verdict_cache import load_cache

    rows = _collect_server_rows()
    specs = _launch_specs_for(set(names))
    try:
        runtime = records_dir(KiroCrewConfig.load().mcp_gateway.socket_path)
        observed = hazards.load_ledger(runtime).as_dict()
        cache = load_cache(runtime)
    except OSError:
        # Unreadable records are not a claim of safety: no hazards and no
        # measurements is exactly how the verdict engine treats a fresh install.
        observed, cache = {}, None

    eligible: list[str] = []
    skipped: list[dict[str, str]] = []
    for name in names:
        row = rows.get(name)
        if row is None:
            skipped.append({"name": name, "reason": "unknown"})
            continue
        is_stdio = row["transport"] == "stdio"
        if not is_stdio or name in UNPOOLABLE_SERVERS:
            skipped.append({"name": name, "reason": "cannot_stub"})
            continue
        if sharing_on and _withheld_env_count(
            {k: "" for k in row["env_names"]},
            forward_declared_env,
            pool_identity_env_keys(),
        ):
            skipped.append({"name": name, "reason": "pooling_blocked_by_env"})
            continue
        preflight: tuple[bool, bool] | None = None
        # Identity is computed for EVERY definition under this name, and they must
        # all agree before any measurement counts. The row builder's ``launch_ids``
        # is not consulted here: it hashes command+args only, so two agents
        # declaring one name with different env read as a single launch, and the
        # measurement of one would authorise pooling the other. Comparing full
        # identities makes the agreement test the same notion the cache is keyed
        # against, rather than a display-side approximation of it.
        candidates = specs.get(name) or []
        identities = {identity_for(s).as_str() for s in candidates} if candidates else set()
        if cache is not None and len(identities) == 1:
            hit = cache.get(name, identity_for(candidates[0]))
            if hit is not None:
                preflight = (hit.ran, hit.caller_sensitive)
        verdict = _assess_server(
            name,
            is_stdio=is_stdio,
            env_names=tuple(sorted(row["env_names"])),
            observed_hazards=observed.get(name, ()),
            preflight=preflight,
            identity_keys=pool_identity_env_keys(),
            probe_current=(probe_current or {}).get(name, True),
        )
        allowed = verdict.recommend_share if sharing_on else verdict.recommend_stub
        if not allowed:
            skipped.append({"name": name, "reason": "evidence_insufficient"})
            continue
        eligible.append(name)
    return eligible, skipped


async def api_mcp_gateway_set_stub(request: web.Request) -> web.Response:
    """POST /api/mcp-gateway/servers/stub — toggle servers' stub flag.

    Body ``{"name": "slack-mcp", "stub": true}`` for one server, or
    ``{"names": ["a-mcp", "b-mcp"], "stub": true}`` for several.  Adds or
    removes those names from ``mcp_gateway.stub_servers`` in config.json
    (same config lock + atomic write as the enable toggle).  The change is
    RECORDED, not applied: the running broker is left alone and the response
    carries ``restart_required``, because the daemon's routing is built with the
    agent-spec rewrite at startup and a session's MCP toolset is fixed at
    ``session/new``.  When the gateway is disabled, the allowlist is persisted
    only (it takes effect when the gateway is enabled).

    The batch form exists because the UI's "toggle all" would otherwise issue
    one request per server: N config rewrites for a single user gesture, each
    one racing the others for the config lock.  One request
    means one write and one apply, so the allowlist can never land half-flipped.

    ``resolve_eligibility: true`` (with ``stub: true``) hands the POLICY to this
    handler: the caller sends candidate names and the server decides which ones
    the evidence allows, inside the same lock hold that writes them. The response
    then reports ``stubbed`` and ``skipped`` (with a reason per name) instead of
    echoing the request, because the two deliberately differ.

    Returns ``{ok, name, stub, ...}`` for the single form and
    ``{ok, names, stub, ...}`` for the batch form.
    """
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "mcp_gateway_set_stub")
    if denied is not None:
        return denied
    from kiro_crew.config.loader import (  # noqa: F811
        ConfigReadError,
        ConfigWriteRefused,
        config_path,
        update_config_locked,
    )

    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    name = str(body.get("name", "")).strip()
    raw_names = body.get("names")
    stub = body.get("stub")
    batch = raw_names is not None
    if batch:
        if not isinstance(raw_names, list) or not all(isinstance(n, str) for n in raw_names):
            return web.json_response(
                {
                    "error": "names must be a list of strings",
                    "code": "names_not_string_list",
                },
                status=400,
            )
        names = [n.strip() for n in raw_names if n.strip()]
        if not names:
            return web.json_response(
                {"error": "names is required", "code": "names_required"}, status=400
            )
        if len(names) > _MAX_STUB_BATCH:
            return web.json_response(
                {
                    "error": f"names must hold at most {_MAX_STUB_BATCH} servers",
                    "code": "names_too_many",
                },
                status=400,
            )
        if any(not _is_valid_mcp_name(n) for n in names):
            return web.json_response(
                {"error": "invalid server name", "code": "invalid_server_name"},
                status=400,
            )
    else:
        if not name:
            return web.json_response({"error": "name is required"}, status=400)
        if not _is_valid_mcp_name(name):
            return web.json_response({"error": "invalid server name"}, status=400)
        names = [name]
    if not isinstance(stub, bool):
        return web.json_response({"error": "stub must be a boolean"}, status=400)
    if batch and stub:
        return web.json_response(
            {
                "error": "stub=true requires one displayed launch per request",
                "code": "batch_stub_requires_individual",
            },
            status=400,
        )
    # Opt-in: "stub only the ones the evidence allows, and decide that yourself".
    #
    # The alternative -- a client filtering the rows it already has -- can only
    # answer for the moment it read them. Sharing is a separate switch another
    # dashboard or the CLI can flip, and the verdicts themselves move as
    # measurements land, so any client-side answer is a snapshot that the write
    # may not match by then. Resolving it HERE, inside the same lock hold that
    # performs the write, means the decision and the write see one state.
    #
    # Only meaningful for ``stub: true``: unstubbing needs no evidence, and
    # refusing to unstub a server whose verdict has since weakened would strand
    # the operator with a stub they explicitly asked to remove.
    resolve_eligibility = body.get("resolve_eligibility", False)
    if not isinstance(resolve_eligibility, bool):
        return web.json_response(
            {
                "error": "resolve_eligibility must be a boolean",
                "code": "resolve_eligibility_not_bool",
            },
            status=400,
        )

    def _log_stub_rejection(code: str) -> None:
        sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation=f"mcp_stub_rejected_{code}",
            outcome="denied",
            source="dashboard",
            resources=f"code={code} names={','.join(names)}"[:256],
        )

    def _launch_over_cap_response(over_cap: list[str]) -> web.Response:
        _log_stub_rejection("launch_over_cap")
        return web.json_response(
            {
                "error": (
                    "more launches resolve for "
                    + ", ".join(over_cap)
                    + " than one approval can record. Nothing was changed."
                ),
                "code": "launch_over_cap",
                "over_cap": over_cap,
            },
            status=409,
        )

    expected_launch = body.get("expected_launch")
    if expected_launch is not None and (batch or not stub or not isinstance(expected_launch, str)):
        _log_stub_rejection("expected_launch_invalid")
        return web.json_response(
            {
                "error": "expected_launch is only valid as a string for one stub approval",
                "code": "expected_launch_invalid",
            },
            status=400,
        )

    # The toggle approves a NAME; what that name launches is approved by content
    # (``mcp_gateway.launch_approval``). Resolve it NOW, from the inputs the
    # gateway's own rewrite reads, and approve exactly that: the rewrite that
    # would otherwise observe it runs at the next gateway start, and an agent
    # can edit ``mcp.json`` or a spec in between. Before any write, so a name
    # with nothing to approve changes nothing.
    launches: dict[str, list[Any]] = {}
    if stub:
        try:
            launches = await asyncio.to_thread(launch_resolve.resolve_launches, names)
        except Exception as exc:
            logger.warning("mcp launch resolution failed: %s", exc)
            _log_stub_rejection("launch_resolve_failed")
            return web.json_response(
                {
                    "error": "could not resolve the launch to approve",
                    "code": "launch_resolve_failed",
                },
                status=503,
            )
        over_cap = [n for n in names if n in getattr(launches, "over_cap", ())]
        if over_cap:
            return _launch_over_cap_response(over_cap)
        unresolved = [n for n in names if not launches.get(n)]
        if unresolved and not resolve_eligibility:
            _log_stub_rejection("launch_unresolved")
            return web.json_response(
                {
                    "error": (
                        "no agent declares a launch the gateway would run for "
                        + ", ".join(unresolved)
                        + ", so there is nothing to approve. Nothing was changed."
                    ),
                    "code": "launch_unresolved",
                    "unresolved": unresolved,
                },
                status=409,
            )

        # Every approval is a compare-and-set on a complete launch display,
        # including the first time a name is stubbed.
        current_display = display_launches(names[0], launches.get(names[0], ()))
        if not current_display["complete"]:
            _log_stub_rejection("launch_display_incomplete")
            return web.json_response(
                {
                    "error": "the launch display is incomplete and cannot be approved",
                    "code": "launch_display_incomplete",
                },
                status=409,
            )
        if expected_launch is None:
            _log_stub_rejection("expected_launch_required")
            return web.json_response(
                {
                    "error": "display the launch before approving it",
                    "code": "expected_launch_required",
                },
                status=409,
            )
        stored_refusals = await asyncio.to_thread(refused_servers)
        stored = stored_refusals.get(names[0])
        current_expected = current_display.get("expected_launch")
        if expected_launch != current_expected or (
            stored is not None and expected_launch != stored.get("expected_launch")
        ):
            _log_stub_rejection("launch_changed_since_display")
            return web.json_response(
                {
                    "error": "the launch changed after it was shown; refresh before approving",
                    "code": "launch_changed_since_display",
                },
                status=409,
            )

        approval_snapshot = await asyncio.to_thread(load_approvals)
        over_cap = [
            name
            for name in names
            if launches.get(name) and approval_snapshot.over_cap(target_stem(name))
        ]
        if over_cap:
            return _launch_over_cap_response(over_cap)

    path = config_path()
    # ``_MCP_GATEWAY_APPLY_LOCK`` outermost, in the SAME order the sharing toggle
    # takes it, so the two handlers serialize against each other in THIS process
    # and cannot deadlock. Inside it, BOTH locks are needed and neither implies
    # the other: ``update_config_locked``'s advisory FILE lock is what excludes
    # other processes, while ``_get_config_lock`` is what excludes this process's
    # agent-CRUD writers -- those call ``cfg.save()`` -> ``write_config_atomically``,
    # which takes no file lock, so the file lock alone would let an agent write
    # and a stub write clobber each other.
    #
    # Imported here rather than at module scope because agents imports mcp.
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    async with _MCP_GATEWAY_APPLY_LOCK:
        overlay = _local_overlay_section()
        # BOTH keys this handler can write. The decision lands in
        # ``stub_overrides``, and ``config.local.json`` wins the deep merge
        # per-key, so an overlay that names it would shadow the base write: the
        # click would answer 200 while the gateway kept routing the previous way.
        # That is the same silent never-takes-effect failure the guard prevents
        # for the roster, so the check covers this key too.
        shadowed = _overlay_shadowed_keys(overlay, ("stub_servers", "stub_overrides"))
        if shadowed:
            return web.json_response(
                {
                    "error": (
                        "config.local.json defines "
                        + " and ".join(f"mcp_gateway.{k}" for k in shadowed)
                        + ", which overrides config.json. Edit that file instead — "
                        "writing here would not change anything the gateway reads."
                    ),
                    "code": "overlay_owns_stub_servers",
                },
                status=409,
            )

        revoked_snapshot = None
        if not stub:
            try:
                revoked_snapshot = await asyncio.to_thread(revoke, names)
            except OSError as exc:
                logger.warning("mcp launch approval store write failed: %s", exc)
                _log_stub_rejection("approval_write_failed")
                return web.json_response(
                    {
                        "error": "could not record the launch approval",
                        "code": "approval_write_failed",
                    },
                    status=503,
                )

        async def _restore_revoked() -> None:
            if revoked_snapshot is not None:
                await asyncio.to_thread(restore, revoked_snapshot)

        # Read here, off the loop, so ``list_servers()`` and its own config reads
        # stay out of the locked read-modify-write below.
        probe_current = (
            await asyncio.to_thread(_probe_currency) if resolve_eligibility and stub else {}
        )

        # Carries the compare-and-set outcome out of the mutate callback. Raising
        # through ``update_config_locked`` would abort the write, which is the
        # behaviour wanted, but it would also lose the value the caller must be
        # told; returning ``None`` from mutate skips the write just as cleanly and
        # keeps the reason addressable here.
        refused: dict[str, Any] = {}
        # Same shape for the eligibility decision: it is made inside the lock, and
        # the response has to report what was actually written rather than what was
        # asked for.
        resolved: dict[str, Any] = {}

        def _mutate(data: dict) -> dict | None:
            section = data.setdefault("mcp_gateway", {})
            if not isinstance(section, dict):
                refused["code"] = "mcp_gateway_not_object"
                return None
            written = list(names)
            if resolve_eligibility and stub:
                # Effective value, so the overlay's `enabled` wins the same way the
                # deep merge gives it to the runtime. The overlay is a hand-edited
                # file with no programmatic writer, so it is read outside the lock;
                # the lock's job is the config.json read-modify-write this handler
                # races with (the sharing toggle, the CLI's `config set`).
                sharing_on = bool(
                    overlay["enabled"] if "enabled" in overlay else section.get("enabled", False)
                )
                written, skipped = _stub_eligibility(
                    names,
                    sharing_on=sharing_on,
                    probe_current=probe_current,
                    # Effective value, read the same way as ``enabled`` just above:
                    # the overlay wins, because that is what the rewriter will see.
                    # Taking the base value alone would let a base ``true`` plus an
                    # overlay ``false`` report a stub whose backend the rewriter
                    # then leaves direct. The absent-key fallback comes from the
                    # config field's own default (FORWARD_DECLARED_ENV_DEFAULT),
                    # not a literal: this reader and the rewriter must agree, or
                    # the batch skips servers the rewrite pools perfectly well.
                    forward_declared_env=bool(
                        overlay["forward_declared_env"]
                        if "forward_declared_env" in overlay
                        else section.get(
                            "forward_declared_env",
                            FORWARD_DECLARED_ENV_DEFAULT,
                        )
                    ),
                )
                # A name with no launch to approve would be stubbed with nothing
                # the gateway may run for it.
                skipped = [
                    *skipped,
                    *(
                        {"name": n, "reason": "launch_unresolved"}
                        for n in written
                        if not launches.get(n)
                    ),
                ]
                written = [n for n in written if launches.get(n)]
                resolved["skipped"] = skipped
                resolved["sharing_on"] = sharing_on
                if not written:
                    # Nothing qualified. Returning None skips the write entirely
                    # rather than rewriting the file with an unchanged set.
                    resolved["eligible"] = []
                    return None
            resolved["eligible"] = written
            # Freeze the alias through the SAME helper the sharing toggle uses, so
            # both writers leave the file in one shape. On a legacy install the
            # effective set comes from the deprecated `poolable_servers`, and reading
            # the raw `stub_servers` here would see nothing: the first toggle would
            # then persist only the server just clicked and silently unstub
            # everything the migration was preserving.
            _freeze_stub_servers(section, overlay)
            # The click is a DECISION about these names, recorded over the roster
            # rather than replacing it -- see `_record_stub_decisions`. Freezing
            # first is load-bearing: the prune compares against the roster, so it
            # has to run after the roster is settled or a legacy install's
            # migrated names would all read as deviations.
            _record_stub_decisions(section, written, stub)
            return data

        try:
            # Blocking: an advisory file lock plus config IO, and the lock can be
            # held by another process, so this must not run on the event loop.
            # ``_get_config_lock`` is held ACROSS the offload because the await
            # yields the event loop -- without it an agent-CRUD save could land
            # between this read and its write.
            #
            # Offloaded through the shielded helper rather than a bare
            # ``asyncio.to_thread``: a worker thread cannot be cancelled, so a
            # cancelled request would unwind both locks while the thread was still
            # mid-write, which is the same interleaving the locks exist to prevent.
            async with _get_config_lock():
                await _offload_config_write(update_config_locked, path, mutate=_mutate)
        except ConfigReadError:
            await _restore_revoked()
            return web.json_response({"error": "config.json is corrupt"}, status=500)
        except ConfigWriteRefused as exc:
            await _restore_revoked()
            return web.json_response(
                {"error": str(exc), "code": "config_write_refused"}, status=400
            )
        except OSError as exc:
            await _restore_revoked()
            # OSError can carry a filesystem path; keep it server-side and send
            # the client a generic message (rendered verbatim into a localized UI).
            logger.warning("mcp config lock failed: %s", exc)
            return web.json_response(
                {"error": "could not lock config.json", "code": "config_lock_failed"},
                status=503,
            )

        if refused.get("code") == "mcp_gateway_not_object":
            await _restore_revoked()
            return web.json_response({"error": "mcp_gateway is not an object"}, status=500)

        # Turning a stub on approves the launches resolved above for the names written.
        decided = list(resolved.get("eligible") or [])
        if stub and decided:
            approved = {n: launches[n] for n in decided if launches.get(n)}
            try:
                await asyncio.to_thread(approve, approved)
            except OSError as exc:
                logger.warning("mcp launch approval store write failed: %s", exc)
                _log_stub_rejection("approval_write_failed")
                return web.json_response(
                    {
                        "error": "could not record the launch approval",
                        "code": "approval_write_failed",
                    },
                    status=503,
                )

        state: DashboardState = request.app["state"]
        apply = getattr(state, "_mcp_gateway_apply_stub", None)
        applied: dict[str, Any] = {"applied": False}
        # One apply for the whole batch: the allowlist is already fully written, so a
        # single call records every name at once.
        audited = f"names={','.join(names)}" if batch else f"name={name}"
        if resolve_eligibility and stub:
            # Audit what was WRITTEN, not what was asked for -- the two differ by
            # design here, and the asked-for list is already in the request log.
            audited += f" written={','.join(resolved.get('eligible') or [])}"
        # Nothing qualified means nothing was written, so there is no new link for
        # an apply to pick up.
        nothing_written = bool(resolve_eligibility and stub and not resolved.get("eligible"))
        if apply is not None and not nothing_written:
            try:
                applied = await apply()
            except Exception as exc:
                sel().log_api_access(
                    caller=request.get("user", "dashboard"),
                    operation="mcp_gateway_set_stub",
                    outcome="error",
                    source="dashboard",
                    resources=f"{audited} stub={stub} error={exc}",
                )
                # Detail is in the SEL log above; the verbatim-rendered client
                # body gets a generic message.
                return web.json_response(
                    {"error": "apply failed", "code": "mcp_apply_failed"}, status=500
                )
        elif not nothing_written:
            # No callback means no gateway wired this process -- but the allowlist
            # was already persisted above, so the change WAS recorded and takes
            # effect at the next start, which is exactly what the callback would
            # have reported. Answering it here keeps the client off its
            # ``applied: false`` fault branch, which would otherwise tell the
            # operator the gateway could not start a change that is safely saved.
            applied = {"applied": False, "restart_required": True}

    sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="mcp_gateway_set_stub",
        outcome="ok",
        source="dashboard",
        resources=f"{audited} stub={stub}",
    )
    subject: dict[str, Any] = {"names": names} if batch else {"name": name}
    outcome: dict[str, Any] = {}
    if resolve_eligibility and stub:
        # The caller asked the server to decide, so the answer has to say what it
        # decided -- a bare ``ok`` would let the UI report a stub the write never
        # made. ``skipped`` carries a reason per name so the operator can act on a
        # count that surprised them.
        outcome = {
            "stubbed": resolved.get("eligible") or [],
            "skipped": resolved.get("skipped") or [],
            "sharing_on": resolved.get("sharing_on"),
        }
    return web.json_response({"ok": True, **subject, "stub": stub, **outcome, **applied})
