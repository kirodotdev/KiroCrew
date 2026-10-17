"""``GET/PUT /api/agent/config``: the installed agent spec's paths and the PUT's single commit unit (governance floor, the ``removedTools`` record, the bookkeeping lift and the spec write)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.agents import (
        AGENT_FILENAME,
        AppOwnershipUnreadable,
        ConfigReadError,
        _agent_file_lock,
        _drop_unbacked_app_entries,
        _err500,
        _get_config_lock,
        _merge_unowned_servers,
        _on_disk_mcp_servers,
        _require_owner,
        agent_state,
        get_shipped_tools,
        kiro_agents_dir_path,
        loads_user_json,
        logger,
        read_bounded_json,
        resolve_agent_config_path,
        sanitize_agent_config_governance,
        update_config_locked,
        write_config_atomically,
    )


def _find_agent_config() -> Path:
    """Find agents/defaults.json — delegates to centralized resolver."""
    return resolve_agent_config_path()


def _installed_agent_config() -> Path:
    """Return the installed agent config path (~/.kiro/agents/kirocrew.json).

    This is the live config that kiro-cli reads.  Dashboard MCP toggle
    and sync operations write here — NOT to agents/defaults.json.
    """
    return kiro_agents_dir_path() / AGENT_FILENAME


def _write_installed_config(path: Path, config: dict[str, Any]) -> None:
    """Write the installed agent spec. The CALLER holds bridges' file lock.

    The lock lives in :func:`_commit_agent_config`, which holds it across the
    merge's on-disk READ as well as this write -- reacquiring it here would
    deadlock, because ``flock`` is per open file description and a second fd on
    the same file blocks against the first from the same thread.

    Runs in a worker thread, which is what makes the caller's synchronous
    flock legal -- on the event loop it would stall the gateway whenever app
    registration held it.
    """
    write_config_atomically(path, config)


def _commit_agent_config(
    *,
    config: dict[str, Any],
    name: str,
    mc_cfg_path: Path,
    removed_per_key: dict[str, list[str]],
    installed_path: Path,
) -> bool:
    """Perform the one fallible read and EVERY durable write of one PUT, as one unit.

    This function is the commit half of the invariant stated at the
    :func:`api_agent_config` PUT branch: it is the ONLY place that branch
    persists application state, it is purely synchronous, and it is dispatched
    exactly once through the shielded ``_offload_config_write``. Those three
    properties make a PUT **non-cancellable but not rollback-atomic**:

    * Purely synchronous — there is no await between two writes, so no
      cancellation and no other task can be interleaved into the sequence. A
      worker thread cannot be cancelled at all, so once this starts it runs to
      completion.
    * Dispatched once, shielded — the caller cannot unwind (and so cannot
      release the transaction lock) until this has returned. The alternative
      shape, awaiting each write separately under the lock, puts a cancellation
      point between writes however wide the lock is.
    * The only writer — nothing durable happens before the call, so every
      failure earlier in the handler leaves the three targets byte-identical.

    What that does NOT buy is rollback: an I/O failure (permission, quota, disk
    full, lock-open, a failed atomic rename) stops the sequence where it is, and
    the writes already committed stay committed. The honest failure prefixes, in
    order, are:

    0. the governance filter raises — nothing durable, and the caller's 500 is
       exact (it fails closed, so a raise withholds rather than grants). The
       merge-on-write step ahead of it (0a) adds one prefix of its own, and it is
       the harmless end: it can raise :class:`AppOwnershipUnreadable` while
       having mutated only the in-memory *config*, so the caller's 500 is exact
       and all three targets are byte-identical (see
       :func:`_app_or_host_owned` for why refusing beats guessing). The
       stale-snapshot rule shares that step and adds NO prefix of its own: it
       performs no I/O beyond the one read they share and cannot raise, so its
       only effect is on the in-memory *config*;
    1. the ``config.json`` read raises :class:`ConfigReadError` — nothing
       durable, and the caller's 500 is exact;
    2. the ``config.json`` write fails — nothing durable;
    3. the first bookkeeping write fails — ``config.json`` updated;
    4. the second bookkeeping write fails (the lift can write twice, once per
       key) — ``config.json`` updated plus one bookkeeping key;
    5. the installed-spec write fails — ``config.json`` and bookkeeping
       updated, the spec unchanged.

    Order inside the unit is chosen to make the *earliest* prefixes the *least*
    harmful, and four steps are load-bearing rather than incidental:

    * The merge (0a) precedes the governance filter, so the entries it re-adds
      are governed like any other — see step (0a). It is in the unit at all for
      the same reason as the read at (1): its on-disk read must be adjacent to
      the write it feeds, or an app registration landing during the flock wait
      is clobbered.
    * The governance filter is FIRST among the steps that decide what is
      persisted, and it is in here at all so that the grant decision cannot be
      made against a ceiling that changes before the write publishes it — see
      step (0). Ahead of every write because it persists nothing, so its own
      fail-closed raise costs no partial write.
    * The read is FIRST among the writes' own inputs. It is the only
      fallible-by-decision I/O step, and running it here — immediately adjacent
      to the write it feeds — is what closes the lost-update window: reading the
      baseline in the caller and writing it back one executor hop later leaves a
      gap in which a concurrent writer's unrelated fields are silently
      reverted.
    * The bookkeeping lift runs AFTER the ``config.json`` write (so prefix 2
      leaves the sidecar untouched) but BEFORE the spec write, because it STRIPS
      Kiro Crew keys (``model_managed`` / ``cc_model``) out of the same *config*
      dict the spec write then persists — reverse the two and the spec lands with
      fields kiro-cli's ``deny_unknown_fields`` rejects.

    Everything else fallible has already been decided by the caller: *config* is
    parsed and validated, *removed_per_key* is the computed ``removedTools`` map,
    and both paths are resolved.

    Returns whatever the bookkeeping lift returns (True when it stripped a
    key), so the caller can log it after the lock is released — logging is not
    durable state and has no business inside the unit.
    """
    # (0) Governance floor, immediately before the writes it governs and inside
    # the same synchronous unit, which is what its own contract asks for
    # ("every whole-config writer MUST call this immediately before it
    # persists"). Running it in phase 1 would decide against a profile snapshot
    # taken BEFORE two lock acquisitions, so a contended transaction flock —
    # unbounded, cross-process — could let the ceiling change during the wait and
    # the PUT would persist a grant governance had since withheld. Here no
    # await, no lock release and no other task can land between the decision and
    # the write that publishes it. Same reasoning that put the read at (1).
    #
    # First in the unit, so a raise from the filter (``may_skip_gate_now`` fails
    # closed) leaves all three targets byte-identical, exactly as a phase-1
    # failure does. Its SEL withhold record is infrastructure, not payload, and
    # is best-effort inside the filter — it cannot fail this unit.
    #
    # Imported lazily: platform.governance is not a module-level dependency of
    # the dashboard handlers.
    # ── THE BRIDGE-FILE LOCK SPANS THE WHOLE UNIT ─────────────────────────────
    # Acquired here rather than at the spec write, because merge-on-write reads
    # this same file and that read is only meaningful if no app writer can
    # commit between it and the write it feeds. ``_deregister_mcp_servers``
    # (app disable / uninstall / health demotion) read-modify-writes the spec
    # under exactly this flock, so an unlocked read let a PUT resurrect a bridge
    # that had just been removed -- and that direction does NOT self-heal,
    # because ``reconcile_enabled_app_resources`` only re-registers ENABLED apps
    # and skips the one whose bridge came back.
    #
    # LOCK ORDER: transaction -> config -> bridge-file. The caller already holds
    # the outer two before dispatching this unit, so widening the innermost hold
    # adds no edge and inverts nothing. The cost is that app registration waits
    # on the ``config.json`` and bookkeeping writes too -- the same accepted
    # trade ``remove_provider_entry`` documents for holding the MCP lock across
    # its unlinks, and the alternative (a second, later lock hold for just the
    # spec write) is what reopens the window above.
    #
    # Taken once. ``_write_installed_config`` deliberately does not lock: with
    # ``flock`` being per open file description, a nested reacquisition from this
    # same thread would block against this hold forever.

    with _agent_file_lock(target=installed_path):
        return _commit_agent_config_locked(
            config=config,
            name=name,
            mc_cfg_path=mc_cfg_path,
            removed_per_key=removed_per_key,
            installed_path=installed_path,
            sanitize=sanitize_agent_config_governance,
        )


def _commit_agent_config_locked(
    *,
    config: dict[str, Any],
    name: str,
    mc_cfg_path: Path,
    removed_per_key: dict[str, list[str]],
    installed_path: Path,
    sanitize: Any,
) -> bool:
    """The commit unit's steps, with bridges' file lock already held.

    Split out only so the lock acquisition reads as one statement; every
    invariant documented on :func:`_commit_agent_config` applies here, and this
    is never called from anywhere else.
    """
    # (0a) ON-DISK STATE DECIDES BOTH DIRECTIONS, immediately before the filter
    # that governs the map they produce. Inside the unit for the same reason as
    # (0) and (1): the on-disk read has to be adjacent to the write it feeds, or a
    # bridge registration landing during the (unbounded, cross-process) flock wait
    # is clobbered. BEFORE the filter, not after, so the
    # entries the merge re-adds are governed too -- re-injecting them afterwards
    # would hand an ``autoApprove`` on a preserved entry a path around step (0).
    #
    # ONE read for the two rules, so they cannot disagree about their baseline:
    # the merge decides names ABSENT from the submission, and the drop rule
    # decides namespaced names PRESENT in a stale one. Their order is immaterial (see
    # :func:`_drop_unbacked_app_entries`).
    existing = _on_disk_mcp_servers(installed_path)
    # The GET masks every ``oauth.clientSecret``; an editor round-trips the
    # marker. Restore it from the SAME on-disk read the merge below uses, so the
    # value written back is the one this locked unit observed -- a rotation
    # that landed during the flock wait is what gets kept, never a snapshot
    # taken in the handler before the lock. A marker with no on-disk value is
    # dropped rather than written.
    from kiro_crew.mcp_utils import restore_redacted_oauth_client_secrets

    restored = restore_redacted_oauth_client_secrets(
        config, {"mcpServers": existing} if isinstance(existing, dict) else {}
    )
    if isinstance(restored.get("mcpServers"), dict):
        # In place: the caller and every step below hold THIS dict.
        config["mcpServers"] = restored["mcpServers"]
    dropped = _drop_unbacked_app_entries(config, existing)
    if dropped:
        # WARNING, not info: the client submitted these and they are not being
        # persisted, which is the one outcome here a user could be surprised by.
        logger.warning(
            "agent-config PUT: dropped %d app-namespaced mcpServers entry/entries the "
            "installed spec does not hold, so a stale snapshot cannot resurrect them: %s",
            len(dropped),
            ", ".join(dropped),
        )
    preserved = _merge_unowned_servers(config, existing)
    if preserved:
        logger.info(
            "agent-config PUT: kept %d mcpServers entry/entries the client does not own: %s",
            len(preserved),
            ", ".join(preserved),
        )
    sanitize(config)

    # (1)+(2) config.json, read AND written inside one hold of the
    # ``<config>.json.lock`` sidecar. The caller's ``_get_config_lock()`` is an
    # asyncio lock: it serializes this against sibling handlers on this event
    # loop and nothing else. The ~69 ``update_config_locked`` writers -- the CLI,
    # the boot refresh, another gateway process -- take the advisory lock
    # instead, so without this the two families could interleave and whichever
    # renamed second published a document that never saw the other's change.
    #
    # Still the one fallible READ, and it still precedes every write: with the
    # default ``on_corrupt="fail"`` an unreadable config raises
    # :class:`ConfigReadError` out of here before anything durable happens, so
    # the caller's 500 stays exact. Failing closed is the point -- writing back a
    # {} baseline would drop every other setting just to record removedTools
    # (see read_config_for_update).
    def _record_removed_tools(mc_cfg: dict) -> dict:
        if removed_per_key:
            mc_cfg["removedTools"] = removed_per_key
        else:
            mc_cfg.pop("removedTools", None)
        return mc_cfg

    update_config_locked(mc_cfg_path, mutate=_record_removed_tools, stamp_meta=False)
    # (3) agent_model_state.json bookkeeping — after (2), before (4).
    changed = agent_state.lift_and_strip_bookkeeping(config, name)
    # (4) the installed spec, under the caller's bridge-file lock.
    _write_installed_config(installed_path, config)
    return changed


async def api_agent_config(request: web.Request) -> web.Response:
    """GET/PUT /api/agent/config — read or write the installed agent config.

    Reads/writes ``~/.kiro/agents/kirocrew.json`` — the live config that
    kiro-cli actually uses at runtime.  Falls back to ``agents/defaults.json``
    if the installed config doesn't exist yet.
    """
    import kiro_crew.dashboard.handlers as _h  # noqa: F811

    installed_path = _h._installed_agent_config()
    defaults_path = _h._find_agent_config()
    # Prefer installed config (what kiro-cli reads); fall back to defaults
    agent_config_path = installed_path if installed_path.is_file() else defaults_path

    if request.method == "PUT":
        denied = await _require_owner(request, "agent_config.write")
        if denied is not None:
            return denied
        body, body_err = await read_bounded_json(request, max_bytes=None)
        if body_err is not None:
            return body_err
        assert body is not None  # read_bounded_json returns (dict, None) on success
        config = body.get("config")
        if not isinstance(config, dict):
            return web.json_response({"error": "config must be an object"}, status=400)
        # The GET above masks every ``oauth.clientSecret``. The marker an editor
        # sends back is restored inside the locked commit unit
        # (``_commit_agent_config_locked``), adjacent to the on-disk read it
        # feeds -- not here, where a read would both block the loop and take a
        # baseline a concurrent rotation could make stale before the lock.
        try:
            # ── THE INVARIANT THIS BRANCH ENFORCES ────────────────────────────
            # Every validation completes BEFORE the first durable write, and the
            # one fallible read plus all durable APPLICATION/CONFIG writes of one
            # PUT execute as a single non-cancellable unit that the transaction
            # lock strictly contains — the lock cannot release while any write of
            # the unit is in flight.
            #
            # "Application/config writes" is the exact scope, and deliberately so:
            # the transaction lock's own sidecar (``_McpFileLock.__aenter__``
            # creates ~/.kiro/settings/mcp.lock) and the SEL audit record on the
            # owner-denial path above are INFRASTRUCTURE, not payload. Both can
            # become durable outside the unit, and neither is a half-applied PUT:
            # a lock file records no user setting and the audit log is required to
            # outlive the request it describes.
            #
            # The unit is non-cancellable but NOT rollback-atomic: an I/O failure
            # part-way through leaves the earlier writes committed. The prefixes
            # are enumerated in :func:`_commit_agent_config`, which also explains
            # why the order makes the earliest prefix the least harmful.
            #
            # Structurally that is two phases with nothing in between:
            #
            #   PHASE 1 (below, off the locks) — GATHER AND DECIDE. Parse, diff,
            #   resolve every path. Persists nothing, so any failure or
            #   cancellation here leaves all three target files byte-identical
            #   and the 4xx/5xx it returns is honest.
            #
            #   PHASE 2 — COMMIT. Take the transaction lock, then the config
            #   lock, then hand the GOVERNANCE FILTER, the ``config.json`` READ
            #   and ALL THREE durable writes to :func:`_commit_agent_config`
            #   through the shielded ``_offload_config_write``, exactly once.
            #   The read is inside the unit rather than in front of it: adjacent
            #   to the write it feeds, it cannot capture a baseline that a
            #   concurrent writer then updates before the worker publishes it
            #   back. The filter is inside for the same reason in the other
            #   direction: in front of the locks its verdict could go stale
            #   during a contended, unbounded flock wait, and the write would
            #   publish a grant governance had already withheld.
            #
            # Why this shape and not "the lock covers more": three prior fixes
            # widened the lock and each time the next defect was a SEQUENCING or
            # CANCELLATION fault inside the widened span — a fallible read placed
            # after a write, an await between two writes, a worker outliving the
            # await that dispatched it. Widening a span cannot fix those, because
            # they are properties of what happens INSIDE it. Collapsing the writes
            # to a single synchronous unit removes the interleaving points instead
            # of trying to cover them: there is no "between two writes" to land in.
            #
            # The three lock layers, transaction lock outermost:
            #
            # 1. ``_get_mcp_lock`` (~/.kiro/settings/mcp.lock) is the MCP
            #    TRANSACTION lock. Agent spec files are a census source for the
            #    MCP config transactions in handlers/mcp.py, which read the
            #    current state and then act on it while holding this lock. An
            #    unlocked write can land inside that read-then-act window, so the
            #    transaction commits a decision about spec contents that changed
            #    underneath it.
            # 2. ``_get_config_lock`` is the in-process lock every other
            #    ``config.json`` read-modify-writer in the dashboard takes
            #    (messaging channel savers, security, the MCP handlers, agent
            #    create/update/delete). This PUT's own RMW spans an executor
            #    hop, so the event loop does not serialize it for free: without
            #    this lock a sibling RMW can read the same baseline and the last
            #    atomic rename silently reverts the other side's unrelated
            #    settings. Held ACROSS the offload for exactly the reason
            #    api_mcp_gateway_set_stub holds it across its own offload.
            # 3. ``bridges._mcp_lock(target=installed_path)``
            #    (~/.kiro/agents/kirocrew.lock) is the FILE lock. The transaction
            #    lock does not cover it: apps/bridges.py does whole-file
            #    read-modify-writes of THIS SAME file under that separate flock
            #    (app enable/disable, MCP (de)registration). Holding only the
            #    transaction lock leaves a concurrent app enable and this PUT
            #    each writing the whole file, and the last atomic rename silently
            #    discards the other side's changes.
            #
            # Order is transaction → config → file and must stay that way. Each
            # edge already exists in the tree and none is inverted anywhere:
            # transaction→config at api_mcp_toggle / api_mcp_toggle_all /
            # api_mcp_remove in handlers/mcp.py, config→file wherever
            # ``_sync_mcp_to_agent`` runs under the config lock (api_mcp_remove,
            # api_mcp_server_detail, mcp_discover, api_capability_mcp_install),
            # and no config-lock or file-lock holder in the tree acquires the
            # transaction lock inside, so there is no ABBA cycle. The file lock is
            # taken inside the worker thread (by ``_commit_agent_config``, which
            # holds it across the whole unit so merge-on-write's on-disk READ and
            # the spec write cannot be split by an app writer) — a blocking flock
            # on the event loop would freeze the gateway while app registration
            # holds it. Widening that innermost hold changes no ORDER: the outer
            # two are already held before the unit is dispatched.
            # Nothing else in this branch takes a cross-process lock: governance +
            # SEL and agent_state take none, so running the filter inside the
            # worker adds no lock edge to the transaction → config → file order.
            #
            # PHASE 1 ── gather and decide. Nothing below is durable.
            #
            # Track tools the user intentionally removed from shipped defaults
            # so they don't reappear on upgrade.  Stored in ~/.kiro/crew/config.json
            # (NOT kirocrew.json — kiro-cli rejects unknown fields).
            # Per-key dict so removing from allowedTools only doesn't affect tools.
            #
            # Computed HERE, from the SUBMITTED config, because the governance
            # filter has not run yet: it runs in the commit unit (step 0), so
            # this diff still sees the pre-governance map. A ceiling-withheld
            # allowedTools ref is not a user removal, and diffing after the
            # filter would record it as one and suppress that tool on every
            # future upgrade. Keep this before the offload.
            shipped = get_shipped_tools()
            removed_per_key: dict[str, list[str]] = {}
            for key in ("tools", "allowedTools"):
                diff = sorted(set(shipped.get(key, [])) - set(config.get(key, [])))
                if diff:
                    removed_per_key[key] = diff
            mc_cfg_path = _h.config_path()  # type: ignore[operator]
            # Only trust a submitted name when it is a non-empty string — any
            # other JSON type (list, dict, number) would flow into the sidecar
            # helper as a dict key and crash the endpoint with a 500.
            raw_name = config.get("name")
            name = (
                raw_name if isinstance(raw_name, str) and raw_name.strip() else installed_path.stem
            )

            # Governance floor on the WHOLE-object write path: this handler
            # persists the request's config as submitted (plus the ``mcpServers``
            # entries merge-on-write re-adds, which the filter therefore also
            # governs — see ``_commit_agent_config`` step (0a)), so a dashboard
            # PUT could otherwise restore a ceiling-governed @denied grant or a
            # governed server's autoApprove that the per-ref writers strip.
            #
            # NOT in phase 1. Running the filter here would place the grant
            # decision BEFORE both lock acquisitions: the transaction flock is
            # cross-process and its wait is unbounded, so a ceiling revoked
            # during a contended wait is already stale by the time the write
            # lands, and the PUT would restore a grant governance had withheld.
            # ``_commit_agent_config`` step (0) runs it synchronously adjacent
            # to the writes it governs — see that docstring. It costs a
            # per-ref directory scan inside the locks, which is the price of the
            # decision being current; and one call, not two, so the filter is
            # never applied to a config it already filtered.

            from kiro_crew.dashboard.handlers.mcp import (
                _get_mcp_lock,
                _offload_config_write,
            )

            # PHASE 2 ── commit. Both locks are acquired ahead of every durable
            # write, so a cancellation at the (unbounded, contended) flock wait
            # inside ``__aenter__`` still tears nothing.
            async with _get_mcp_lock():
                # The config lock spans the whole read-modify-write, not just the
                # write: the read now happens in the worker thread, so this is
                # the only thing serializing this PUT's RMW against the sibling
                # ``config.json`` writers that take the same lock.
                async with _get_config_lock():
                    # THE one durable step: the config.json read + removedTools
                    # sidecar + bookkeeping sidecar + installed spec, in a worker
                    # thread, behind the shield.
                    #
                    # ``_offload_config_write`` is what binds the unit to the
                    # locks: a worker thread cannot be cancelled, and the shield's
                    # drain loop keeps re-absorbing cancellations until the worker
                    # is done, so this await cannot return or raise — and
                    # therefore ``async with`` cannot run ``__aexit__`` — while a
                    # write is still in flight. A bare ``to_thread`` per write
                    # would instead give every write boundary a cancellation point
                    # at which the locks are released with the worker still
                    # writing.
                    try:
                        changed = await _offload_config_write(
                            _commit_agent_config,
                            config=config,
                            name=name,
                            mc_cfg_path=mc_cfg_path,
                            removed_per_key=removed_per_key,
                            installed_path=installed_path,
                        )
                    except AppOwnershipUnreadable:
                        # Step (0a), ahead of every durable write, so all three
                        # targets are byte-identical and this 500 is exact.
                        # Refusing is the only honest answer: guessing preserved
                        # would make entries undeletable, guessing deleted would
                        # clobber live app bridges. The client can retry.
                        logger.exception("Refusing agent-config PUT: app ownership unreadable")
                        return web.json_response(
                            {
                                "error": "cannot determine app-owned MCP entries",
                                "code": "app_ownership_unreadable",
                            },
                            status=500,
                        )
                    except ConfigReadError:
                        # The unit's FIRST step, so this 500 is exact: no write of
                        # the unit has run and all three targets are unchanged.
                        logger.exception("Refusing to record removedTools: config unreadable")
                        return web.json_response(
                            {"error": "failed to read config file", "code": "config_unreadable"},
                            status=500,
                        )
            if changed:
                logger.info(
                    "Stripped Kiro Crew bookkeeping keys from a PUT to agent config for %r",
                    name,
                )
            # Restart kiro-cli sessions so new config takes effect
            await _h._reset_all_sessions(request)
            return web.json_response({"ok": True, "applied": True})
        except Exception as exc:
            return _err500(exc)
    # GET
    try:
        data = loads_user_json(agent_config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    # A pre-registered Connections client projects its secret into the installed
    # spec for kiro-cli; this read is not kiro-cli, and any dashboard subject can
    # make it. Mask the value (the PUT branch restores the marker from disk).
    from kiro_crew.mcp_utils import redact_oauth_client_secrets

    return web.json_response(redact_oauth_client_secrets(data))
