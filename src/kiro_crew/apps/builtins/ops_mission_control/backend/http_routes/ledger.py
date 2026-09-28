"""The shared ledger: ``/ledger`` read, write and delete, ``/ledger/contradictions`` and
``/ledger/hygiene``.

``ledger.jsonl`` is the one artifact that leaves the machine — ``ledger_sync`` pushes it to
the team's shared remote — so a written entry passes the facade's ``_safe_outbound`` before
its content-addressed id is computed (``routes.py`` is the registered redaction sink for this
write), and the maintenance pass that prunes the file runs on the primary instance only.
"""

from __future__ import annotations

import asyncio
from typing import Callable

from aiohttp import web

from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, rotation, store
from kiro_crew.apps.builtins.ops_mission_control.backend.http_routes._shared import (
    AuditWriter,
    _json_body,
    logger,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry


async def _handle_get_ledger(request: web.Request) -> web.StreamResponse:
    # BOTH off-loop. `read_entries` already was; `stats()` re-scanned the same file inline
    # right after it, which was both a second full parse and a parse on the event loop.
    entries, stats = await asyncio.gather(
        asyncio.to_thread(ledger.read_entries),
        asyncio.to_thread(ledger.stats),
    )
    entries.sort(key=lambda e: (-e.use_count, e.pattern))
    return web.json_response({"entries": [e.to_dict() for e in entries], "stats": stats})


async def _handle_ledger_contradictions(request: web.Request) -> web.StreamResponse:
    """Entry pairs claiming different fixes for the same fingerprint.

    A read-only diagnostic for the hygiene SOP, which is told to "resolve contradictions"
    and cannot find them by eye across the whole ledger. Detection is
    deterministic and cheap; the resolution (split the two patterns so each names its own
    cause) needs the model, so this endpoint deliberately changes nothing.
    """
    found = await asyncio.to_thread(ledger.find_contradictions)
    return web.json_response({"contradictions": found, "count": len(found)})


async def _handle_post_ledger(
    request: web.Request, *, _audit: AuditWriter, _safe_outbound: Callable[[str], str]
) -> web.StreamResponse:
    """Add or promote a learned pattern.

    ``miss_count`` / ``last_miss`` / ``decayed_at_miss_count`` are deliberately NOT
    accepted from a body, and this is the security-shaped half of §5.9's demotion path.
    The hygiene SOP promotes ``observed`` → ``verified`` by re-POSTing the same
    pattern+fix (ids are content-addressed, so it merges) — so an accepted
    ``miss_count: 0`` on that route would make the promotion step double as a way to
    erase every recorded failure, with one curl, on the exact entries most likely to
    have them. Miss evidence is only ever produced by ``ledger.record_miss``, from an
    observed recheck, and ``upsert`` takes the MAX so a merge cannot lower it either.
    """
    body = await _json_body(request)
    if body is None:
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "body_not_object"}, status=400
        )
    pattern = str(body.get("pattern", "")).strip()
    fix = str(body.get("fix", "")).strip()
    if not pattern or not fix:
        return web.json_response(
            {"error": "pattern and fix are required", "code": "missing_required_field"}, status=400
        )

    # Redact on the WRITE path, before the id is computed.
    #
    # `ledger.jsonl` is the one artifact that leaves this machine: `ledger_sync` commits
    # and pushes it verbatim to a shared remote. Nothing sanitised it. Evidence→prompt and
    # incident→Slack both pass a chokepoint; this path did not, and a `fix` field is the
    # single likeliest place for a pasted credential because that is literally what a fix
    # looks like — a command line, a hostname, a token in a header.
    #
    # Write-path, not sync-path, for two reasons. The entry is already on local disk and in
    # the vector index by the time sync runs; and an operator who enables sync LATER would
    # otherwise retroactively publish everything written before. Recovery from the other
    # ordering is a git history rewrite across every teammate's clone.
    #
    # This changes the content-addressed id, and that is correct: two entries differing
    # only in a redacted secret SHOULD dedupe to one.
    #
    # Through the facade's `_safe_outbound`, the app's one two-pass floor: `redact_via_context`
    # rather than `security.redact` directly, so a loaded companion's declared patterns apply
    # and an enterprise host that fails to compose its companion fails CLOSED on redaction
    # instead of silently falling back to public patterns, then `redact_tokens` for the
    # provider-token shapes the core patterns miss. It lives in `routes.py` because that module
    # is the registered redaction sink for this write.
    pattern = _safe_outbound(pattern)
    fix = _safe_outbound(fix)

    raw_fps = body.get("fingerprints")
    raw_keys = body.get("provider_keys")
    entry = LedgerEntry.create(
        pattern=pattern,
        fix=fix,
        fingerprints=[str(f) for f in raw_fps] if isinstance(raw_fps, list) else [],
        # Optional and additive: an entry with no provider key still matches by shape,
        # which is every entry written before this field existed.
        provider_keys=[str(k) for k in raw_keys] if isinstance(raw_keys, list) else [],
        confidence=str(body.get("confidence", "medium")),
        trust=str(body.get("trust", "observed")),
        source=str(body.get("source", "human")),
    )
    try:
        stored = await asyncio.to_thread(ledger.upsert, entry)
    except OSError as exc:
        # Reported, not raised — the same shape ``_handle_rotation_arm`` uses for a
        # refusing store, and for the same reason its comment gives: escaping here
        # becomes aiohttp's default 500, a plain-text body with no ``code`` for the
        # UI to branch on. ``upsert`` appends or rewrites ``ledger.jsonl`` under the
        # ledger lock, so a full disk or a permission fault surfaces as ``OSError``
        # from either the lock acquisition or the write itself. 503 rather than 500
        # because the condition is transient and retrying is the correct client
        # behaviour. This was the last trio of mutating routes in this file (POST /
        # DELETE /ledger and /ledger/hygiene) still answering the bare 500; every
        # sibling store write already reports a coded refusal.
        logger.warning("ops-mission-control: ledger write refused, store unwritable")
        _audit("ledger_post", entry.entry_id, "failure", error="ledger store unwritable")
        return web.json_response(
            {"ok": False, "error": str(exc), "code": "ledger_store_unwritable"}, status=503
        )
    return web.json_response({"entry": stored.to_dict()})


async def _handle_ledger_hygiene(
    request: web.Request, *, _audit: AuditWriter, _index_ledger_safely: Callable[[], dict[str, int]]
) -> web.StreamResponse:
    """Run the deterministic ledger maintenance pass: sync, hygiene, index.

    Called by the ledger-hygiene cron. Deterministic Python rather than an agent
    judgement call, so the mechanical part costs no tokens and the SOP's model
    time goes to the parts that need reasoning (contradictions, promotions).

    **Order is load-bearing:** pull → hygiene → index → push.

    - Pull FIRST so hygiene sees teammates' entries. Deduping before the merge would
      leave freshly-arrived duplicates to sit until tomorrow's pass.
    - Index AFTER hygiene so we do not embed rows hygiene is about to prune, and so a
      promoted ``observed → verified`` entry is indexed at its new importance.
    - Push LAST, carrying hygiene's result — otherwise every instance re-derives the
      same dedupe locally and the repo never converges.

    This is also where the two halves of the git-native memory loop finally get a
    caller. ``ledger_sync`` and ``ledger_index.import_pending`` were both built,
    tested, and **wired to nothing**: sync had no caller at all, and the semantic-recall
    search in ``dispatch`` was querying an index that nothing ever populated — so recall
    silently returned zero hits forever on a real install. A daily cadence is right for
    both: shared lessons are not latency-sensitive, and embedding is the expensive step.

    Every stage is independently fault-tolerant. A missing remote, an offline network, a
    conflicted ledger, or an absent embedding model each degrade to a reported
    sub-result; none prevents the local dedupe/decay/prune from running, because local
    hygiene is the part that always works and always matters. The one exception is the
    hygiene rewrite itself: a refused read or write there answers a coded 503
    (``ledger_store_unwritable``) and skips index, prune, and push, because publishing a
    ledger the dedupe pass never committed to is worse than deferring everything to the
    next cron run.

    **Only the primary instance may run it.** This pass PRUNES a shared ledger, and on a
    team every instance reaching it means N concurrent dedupe/decay/prune passes over one
    file. That is strictly worse than the double-claim the single-owner model exists to
    prevent: a duplicate claim wastes an agent turn, a duplicate prune deletes knowledge.
    ``is_primary()`` was added for exactly this and then never wired to an enforcement
    point — while ``sops/rotation-check.md`` told operators this route "self-gates on
    ``is_primary()`` at runtime", which was not true of any code. A SOP asserting a gate
    that does not exist is worse than no gate, because it stops anyone looking for one.
    """
    from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

    # 409, not 403: the caller is authenticated and permitted, it is simply not this
    # instance's job. A 403 would read as "your credentials are wrong" and send an
    # operator looking in the wrong place.
    if not await asyncio.to_thread(rotation.is_primary):
        leader = await asyncio.to_thread(rotation.primary_owner)
        _audit("ledger_hygiene", f"leader={leader or 'unset'}", "rejected", error="not primary")
        return web.json_response(
            {
                "error": (
                    "this instance is not the primary — ledger hygiene prunes shared "
                    "knowledge, so exactly one instance may run it"
                    + (f" (currently {leader})" if leader else "")
                ),
                "code": "not_primary",
                "changed": False,
            },
            status=409,
        )

    pulled = await ledger_sync.sync_safely(direction="pull")
    try:
        summary = await asyncio.to_thread(ledger.hygiene)
    except OSError as exc:
        # ``hygiene`` is a locked read-modify-REWRITE of the whole ledger file, so a
        # refused write (full disk, permissions, a failed lock) surfaces here as
        # ``OSError``. Letting it escape gives aiohttp's default 500 — a plain-text
        # body with no ``code`` — while every other refusal on this route (not
        # primary, disabled app) answers coded JSON. The pull above already landed
        # and is harmless to repeat; the push below must NOT run, because it would
        # publish a ledger the dedupe pass never committed to. 503 rather than 500
        # because the condition is transient and the cron's next run is the retry.
        logger.warning("ops-mission-control: ledger hygiene refused, store unwritable")
        _audit("ledger_hygiene", "rewrite refused", "failure", error="ledger store unwritable")
        return web.json_response(
            {
                "ok": False,
                "error": str(exc),
                "code": "ledger_store_unwritable",
                # The sibling refusal above (409 not_primary) carries this, and the
                # hygiene SOP branches on ``changed`` to decide whether to speak at all.
                "changed": False,
            },
            status=503,
        )
    indexed = await asyncio.to_thread(_index_ledger_safely)
    # Retire old CLOSED incidents. Here rather than on the claim path because pruning is
    # maintenance: doing it in `claim` would make an ordinary claim occasionally pay for a
    # large rewrite. Open work is never pruned, whatever the age.
    #
    # Degraded rather than fatal, for the same reason `dispatch.run_cycle`'s maintenance
    # passes are: `prune_closed` now propagates a failed index read, and it sits BEFORE the
    # push below. Letting it escape would mean one EACCES on this cron skips pushing the
    # ledger that `hygiene` just deduped -- a later fault costing an earlier step's work.
    # Pruning is the most deferrable thing here (the next run retires the same incidents),
    # while the push is what other instances are waiting on. A corrupt index is deliberately
    # NOT caught: that never self-heals, so it must stop the cron loudly instead of quietly
    # skipping the prune on every future run.
    try:
        incidents_pruned = await asyncio.to_thread(store.prune_closed)
    except OSError:
        logger.exception("ops-mission-control: could not prune closed incidents; skipping")
        incidents_pruned = 0
    pushed = await ledger_sync.sync_safely(direction="push")

    changed = any(summary.get(k) for k in ("deduped", "decayed", "pruned"))
    if changed or indexed.get("written") or pulled or incidents_pruned:
        _audit(
            "ledger_hygiene",
            f"{summary} pull={pulled or 'skipped'} index={indexed}",
            "success",
        )
    return web.json_response(
        {
            "summary": summary,
            # Empty strings when sync is unconfigured, which is the common single-user
            # case — the UI shows nothing rather than a scary "not configured".
            "sync": {"pull": pulled, "push": pushed},
            "index": indexed,
            "incidents_pruned": incidents_pruned,
            # ``changed`` drives whether the cron speaks at all, so it must reflect
            # anything a human would want to hear about — including a pull that brought
            # in a teammate's lesson, which changes what the agent knows tomorrow.
            "changed": bool(changed or pulled or indexed.get("written") or incidents_pruned),
        }
    )


async def _handle_delete_ledger(request: web.Request, *, _audit: AuditWriter) -> web.StreamResponse:
    entry_id = request.query.get("id", "").strip()
    if not entry_id:
        return web.json_response(
            {"error": "id is required", "code": "missing_required_field"}, status=400
        )
    try:
        removed = await asyncio.to_thread(ledger.remove, entry_id)
    except OSError as exc:
        # ``remove`` rewrites the ledger under its lock, so a refused write raises
        # ``OSError``. The removal route needs the coded refusal for the same reason
        # the secret-revocation route does: a bare 500 leaves the operator unsure
        # whether the entry they just deleted is gone, and the honest answer is
        # "still there, retry". Same shape as ``_handle_rotation_arm``.
        logger.warning("ops-mission-control: ledger removal refused, store unwritable")
        _audit("ledger_delete", entry_id, "failure", error="ledger store unwritable")
        return web.json_response(
            {"ok": False, "error": str(exc), "code": "ledger_store_unwritable"}, status=503
        )
    if not removed:
        # Split from the success return rather than computing the status. A 404 IS an error
        # response and needs a `code` the localized UI can switch on; the previous single
        # `status=200 if removed else 404` produced one body shape for both outcomes, so the
        # error branch could not carry one without also putting it on the success branch.
        return web.json_response(
            {"error": "unknown ledger entry", "ok": False, "removed": False, "code": "not_found"},
            status=404,
        )
    return web.json_response({"ok": True, "removed": True}, status=200)
